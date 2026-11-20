"""macOS camera capture with AVFoundation presentation timestamps on the host clock."""
import threading
import time
import numpy as np
import AVFoundation as AV
import CoreMedia as CM
import Quartz as Q
import dispatch
import objc
from Foundation import NSObject


def devices():
    return [{"id": str(d.uniqueID()), "name": str(d.localizedName())}
            for d in AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeVideo)]


class CameraDelegate(NSObject):
    @objc.python_method
    def configure(self, camera):
        self.camera = camera
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sample, connection):
        camera = self.camera
        if not camera.running:
            return
        try:
            pts = CM.CMSampleBufferGetPresentationTimeStamp(sample)
            clock = camera.session.masterClock()
            host = CM.CMSyncConvertTime(pts, clock, CM.CMClockGetHostTimeClock())
            timestamp = CM.CMTimeGetSeconds(host) + camera.host_offset
            pixel = CM.CMSampleBufferGetImageBuffer(sample)
            Q.CVPixelBufferLockBaseAddress(pixel, 1)
            try:
                w, h = Q.CVPixelBufferGetWidth(pixel), Q.CVPixelBufferGetHeight(pixel)
                stride = Q.CVPixelBufferGetBytesPerRow(pixel)
                pointer = Q.CVPixelBufferGetBaseAddress(pixel)
                frame = np.frombuffer(pointer.as_buffer(h * stride), dtype=np.uint8).reshape(h, stride)
                frame = frame[:, :w * 4].reshape(h, w, 4)[:, :, :3].copy()
            finally:
                Q.CVPixelBufferUnlockBaseAddress(pixel, 1)
            camera.callback(frame, timestamp)
        except Exception as exc:
            camera.error(str(exc))

    def captureOutput_didDropSampleBuffer_fromConnection_(self, output, sample, connection):
        self.camera.dropped += 1


class Camera:
    def __init__(self, device_id, callback, error):
        self.callback, self.error = callback, error
        self.dropped = 0
        self.running = False
        status = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeVideo)
        if status == 0:
            granted = threading.Event()
            AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AV.AVMediaTypeVideo, lambda ok: granted.set())
            granted.wait(30)
            status = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeVideo)
        if status != 3:
            raise RuntimeError('Camera access denied. Enable camera access for your terminal/app in System Settings > Privacy & Security > Camera, then restart.')
        device = next((d for d in AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeVideo)
                       if str(d.uniqueID()) == device_id), None)
        if device is None:
            raise RuntimeError('Selected camera is no longer connected.')
        self.session = AV.AVCaptureSession.alloc().init()
        self.session.setSessionPreset_(AV.AVCaptureSessionPreset1280x720)
        inp, err = AV.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if inp is None or not self.session.canAddInput_(inp):
            raise RuntimeError(f'Cannot open camera: {err}')
        self.session.addInput_(inp)
        out = AV.AVCaptureVideoDataOutput.alloc().init()
        out.setVideoSettings_({Q.kCVPixelBufferPixelFormatTypeKey: Q.kCVPixelFormatType_32BGRA})
        out.setAlwaysDiscardsLateVideoFrames_(True)
        self.delegate = CameraDelegate.alloc().init().configure(self)
        self.queue = dispatch.dispatch_queue_create(b'mchstreamer.camera', None)
        out.setSampleBufferDelegate_queue_(self.delegate, self.queue)
        if not self.session.canAddOutput_(out):
            raise RuntimeError('Cannot add camera output.')
        self.session.addOutput_(out)
        self.output = out
        # Calibrate host clock to Python monotonic using the shortest of 20 brackets.
        samples = []
        for _ in range(20):
            before = time.monotonic()
            host = CM.CMTimeGetSeconds(CM.CMClockGetTime(CM.CMClockGetHostTimeClock()))
            after = time.monotonic()
            samples.append((after - before, (before + after) / 2 - host))
        self.clock_uncertainty = min(samples)[0] / 2
        self.host_offset = min(samples)[1]

    def start(self):
        self.running = True
        self.session.startRunning()
        if not self.session.isRunning():
            raise RuntimeError('Camera failed to start.')

    def close(self):
        self.running = False
        self.session.stopRunning()
        self.output.setSampleBufferDelegate_queue_(None, None)
