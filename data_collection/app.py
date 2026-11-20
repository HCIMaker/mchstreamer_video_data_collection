import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import threading
import time

import io
from PIL import Image, ImageDraw
import numpy as np
import sounddevice as sd
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .recording import Recording

ROOT = Path(__file__).parent
DEFAULT_DIR = ROOT / 'data'


class Settings(BaseModel):
    audio_device: int | None = None
    camera_id: str = ''
    sample_rate: int = Field(default=44100, ge=8000, le=192000)
    demo: bool = False


class Take(BaseModel):
    channels: list[int] = Field(default_factory=lambda: list(range(16)))
    directory: str = str(DEFAULT_DIR)
    filename: str = ''


class Engine:
    def __init__(self):
        self.control = threading.RLock()
        self.guard = threading.Lock()
        self.running = False
        self.recording = None
        self.last_take = None
        self.stream = self.camera = None
        self.audio = deque(maxlen=64)
        self.latest_video = None
        self.audio_time = self.video_time = 0
        self.audio_count = self.video_count = 0
        self.error = None
        self.issues = deque(maxlen=100)
        self.clock_info = {}
        self.settings = Settings()
        self.demo_stop = threading.Event()

    def fault(self, message):
        self.error = message
        if not self.issues or self.issues[-1] != message:
            self.issues.append(message)

    def on_audio(self, data, timestamp, raw=0, flags=''):
        with self.guard:
            self.audio.append(data)
            self.audio_time = timestamp + len(data) / self.settings.sample_rate
            self.audio_count += len(data)
            if flags:
                self.fault(f'Audio capture: {flags}')
            if self.recording:
                self.recording.submit('audio', data, timestamp, raw, flags)

    def on_video(self, frame, timestamp):
        with self.guard:
            self.latest_video = frame
            self.video_time = timestamp
            self.video_count += 1
            if self.recording:
                self.recording.submit('video', frame, timestamp)

    def start(self, settings):
        with self.control:
            if self.running:
                raise ValueError('Stop preview before changing devices or sample rate.')
            self.settings = settings
            self.error = None
            self.issues.clear()
            self.audio.clear()
            self.latest_video = None
            self.audio_time = self.video_time = 0
            self.audio_count = self.video_count = 0
            try:
                if settings.demo:
                    self.clock_info = {'method': 'Synthetic shared monotonic clock', 'hardware': False}
                    self.demo_stop.clear()
                    self.demo_thread = threading.Thread(target=self._demo, daemon=True)
                    self.running = True
                    self.demo_thread.start()
                else:
                    if settings.audio_device is None:
                        raise ValueError('Select the MCHStreamer audio input.')
                    d = sd.query_devices(settings.audio_device)
                    if d['max_input_channels'] < 16:
                        raise ValueError('Selected device must provide at least 16 input channels.')
                    sd.check_input_settings(device=settings.audio_device, channels=16,
                                            samplerate=settings.sample_rate, dtype='float32')
                    def callback(data, frames, timing, status):
                        stamp = timing.inputBufferAdcTime
                        if not np.isfinite(stamp) or stamp <= 0:
                            self.fault('Invalid audio ADC timestamp. Recording is unsafe; restart preview.')
                            return
                        self.on_audio(data.copy(), stamp + self.audio_offset, stamp, str(status))
                    self.stream = sd.InputStream(device=settings.audio_device, channels=16,
                                                 samplerate=settings.sample_rate, dtype='float32',
                                                 blocksize=1024, callback=callback)
                    probes = []
                    for _ in range(30):
                        a = time.monotonic()
                        stream_time = self.stream.time
                        b = time.monotonic()
                        probes.append((b-a, (a+b)/2-stream_time))
                    uncertainty, self.audio_offset = min(probes)
                    from .camera import Camera
                    self.camera = Camera(settings.camera_id, self.on_video, self.fault)
                    self.clock_info = {'method': 'PortAudio ADC timestamps and AVFoundation PTS converted to host clock, calibrated to monotonic',
                                       'audio_clock_offset_seconds': self.audio_offset,
                                       'audio_clock_mapping_uncertainty_seconds': uncertainty/2,
                                       'camera_clock_mapping_uncertainty_seconds': self.camera.clock_uncertainty,
                                       'hardware': True, 'hardware_genlock': False}
                    self.stream.start()
                    self.camera.start()
                    self.running = True
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and (not self.audio_time or not self.video_time):
                    time.sleep(.05)
                if not self.audio_time or not self.video_time:
                    raise RuntimeError(self.error or 'No audio or video arrived. Check permissions and device connections.')
            except Exception:
                self.stop()
                raise
        return self.status()

    def _demo(self):
        rate = self.settings.sample_rate
        start, n, v = time.monotonic(), 0, 0
        while not self.demo_stop.is_set():
            now = time.monotonic()
            if (n + 1024) / rate <= now - start:
                t = (np.arange(1024) + n) / rate
                data = (.18 * np.sin(2*np.pi*t[:, None]*(180+np.arange(16)*70))).astype('float32')
                self.on_audio(data, start+n/rate)
                n += 1024
            if v / 30 <= now - start:
                image = Image.new('RGB', (640, 360), (19, 27, 31))
                draw = ImageDraw.Draw(image)
                draw.text((45, 150), 'DEMO / synthetic camera', fill=(180,220,100), font_size=28)
                draw.text((45, 200), f'{v/30:08.2f} s', fill='white', font_size=24)
                x = int(320+200*np.sin(v/30))
                draw.ellipse((x-12,253,x+12,277), fill=(180,220,100))
                frame = np.asarray(image)[:, :, ::-1].copy()
                self.on_video(frame, start+v/30)
                v += 1
            self.demo_stop.wait(.002)

    def start_take(self, take):
        with self.control:
            if not self.running or self.recording:
                raise ValueError('Start preview first; only one recording can run.')
            channels = sorted(set(take.channels))
            if not channels or any(c < 0 or c > 15 for c in channels):
                raise ValueError('Select at least one channel from 0–15.')
            if self.error or max(time.monotonic()-self.audio_time, time.monotonic()-self.video_time) > 2:
                raise ValueError('Capture is unhealthy. Restart preview before recording.')
            config = {**self.settings.model_dump(), 'channels': channels}
            name = take.filename or datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
            rec = Recording(take.directory, name, config, self.latest_video.shape,
                            time.monotonic(), self.clock_info)
            with self.guard:
                self.issues.clear()
                self.start_camera_drops = getattr(self.camera, 'dropped', 0)
                self.recording = rec
        return self.status()

    def stop_take(self):
        with self.control:
            with self.guard:
                rec = self.recording
                end = time.monotonic()
                if rec:
                    rec.end = end
            # Drain capture buffers that represent times before the Stop boundary.
            # USB delivery latency must not shorten one track relative to the other.
            if rec:
                deadline = time.monotonic() + .75
                while time.monotonic() < deadline and (self.audio_time < end or self.video_time < end):
                    time.sleep(.01)
                with self.guard:
                    self.recording = None
            if rec:
                issues = list(self.issues)
                if end - self.audio_time > 2 or end - self.video_time > 2:
                    issues.append('Capture stalled before the take stopped.')
                drops = getattr(self.camera, 'dropped', 0) - self.start_camera_drops
                if drops:
                    issues.append(f'AVFoundation dropped {drops} frames.')
                self.last_take = rec.close(end, issues)
        return self.status()

    def stop(self):
        with self.control:
            self.stop_take()
            self.running = False
            self.demo_stop.set()
            if hasattr(self, 'demo_thread') and self.demo_thread.is_alive():
                self.demo_thread.join(timeout=2)
            if self.stream:
                self.stream.close()
                self.stream = None
            if self.camera:
                self.camera.close()
                self.camera = None
        return self.status()

    def status(self):
        now, rec = time.monotonic(), self.recording
        stale = self.running and (now-self.audio_time > 2 or now-self.video_time > 2)
        return {'running': self.running, 'recording': rec is not None, 'demo': self.settings.demo,
                'error': self.error or (rec.error if rec else None) or ('Capture stalled. Stop and check connections.' if stale else None),
                'settings': self.settings.model_dump(), 'elapsed': now-rec.epoch if rec else 0,
                'path': str(rec.path) if rec else None, 'last_take': self.last_take,
                'recorded_channels': rec.config['channels'] if rec else None,
                'camera_resolution': [int(self.latest_video.shape[1]), int(self.latest_video.shape[0])] if self.latest_video is not None else None,
                'audio_samples': rec.audio_samples if rec else self.audio_count,
                'video_frames': rec.video_frames if rec else self.video_count,
                'dropped_audio': rec.dropped_audio if rec else 0,
                'dropped_video': (rec.dropped_video if rec else 0) + getattr(self.camera, 'dropped', 0),
                'audio_age_ms': max(0, (now-self.audio_time)*1000) if self.audio_time else None,
                'video_age_ms': max(0, (now-self.video_time)*1000) if self.video_time else None,
                'clock': self.clock_info}

    def preview(self, spectral_channel=None):
        with self.guard:
            chunks = list(self.audio)
            frame = self.latest_video
        result = self.status()
        if chunks:
            rate = self.settings.sample_rate
            signal = np.concatenate(chunks)[-int(rate*.12):]
            # Min/max envelopes preserve short peaks while bounding browser traffic.
            bins = min(240, len(signal))
            n = len(signal)//bins
            view = signal[-bins*n:].reshape(bins, n, 16)
            result['wave_min'] = view.min(axis=1).T.tolist()
            result['wave_max'] = view.max(axis=1).T.tolist()
            result['peaks'] = np.max(np.abs(signal), axis=0).tolist()
            if spectral_channel is not None and 0 <= spectral_channel < 16 and len(signal) >= 1024:
                x = signal[-1024:, spectral_channel] * np.hanning(1024)
                db = 20*np.log10(np.maximum(np.abs(np.fft.rfft(x))/256, 1e-6))
                result['spectrum'] = np.clip(db, -100, 0).tolist()
        if frame is not None:
            image = Image.fromarray(frame[:, :, ::-1])
            image.thumbnail((640, 480))
            jpeg = io.BytesIO()
            image.save(jpeg, format='JPEG', quality=72)
            result['jpeg'] = base64.b64encode(jpeg.getvalue()).decode()
        return result


engine = Engine()


@asynccontextmanager
async def lifespan(app):
    yield
    engine.stop()


app = FastAPI(lifespan=lifespan)
from starlette.middleware.trustedhost import TrustedHostMiddleware
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])


@app.middleware('http')
async def local_only(request: Request, call_next):
    # Reject cross-origin writes to the local filesystem/hardware service.
    origin = request.headers.get('origin')
    if request.method != 'GET' and origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': 'Cross-origin writes are disabled.'}, status_code=403)
    return await call_next(request)


@app.get('/api/devices')
def devices():
    from .camera import devices as cameras
    audio = []
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] >= 16:
            rates = []
            for rate in [16000, 22050, 32000, 44100, 48000, 88200, 96000, 192000]:
                try:
                    sd.check_input_settings(device=i, channels=16, samplerate=rate, dtype='float32')
                    rates.append(rate)
                except sd.PortAudioError:
                    pass
            audio.append({'id': i, 'name': d['name'], 'channels': d['max_input_channels'], 'rates': rates})
    return {'audio': audio, 'cameras': cameras(), 'default_directory': str(DEFAULT_DIR),
            'filename': datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')}


def action(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get('/api/status')
def status():
    return engine.status()


@app.post('/api/preview/start')
def start(settings: Settings):
    return action(engine.start, settings)


@app.post('/api/preview/stop')
def stop():
    return action(engine.stop)


@app.post('/api/record/start')
def record(take: Take):
    return action(engine.start_take, take)


@app.post('/api/record/stop')
def finish():
    return action(engine.stop_take)


@app.websocket('/ws')
async def live(ws: WebSocket):
    if ws.headers.get('origin') not in {f"http://{ws.headers.get('host')}", f"https://{ws.headers.get('host')}"}:
        await ws.close(code=1008)
        return
    await ws.accept()
    channel = None
    try:
        while True:
            try:
                opts = await asyncio.wait_for(ws.receive_json(), timeout=.08)
                channel = opts.get('spectral_channel')
                if not isinstance(channel, int):
                    channel = None
            except asyncio.TimeoutError:
                pass
            await ws.send_json(await asyncio.to_thread(engine.preview, channel))
            await asyncio.sleep(.04)
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.get('/')
def index():
    return FileResponse(ROOT / 'static' / 'index.html')


app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
