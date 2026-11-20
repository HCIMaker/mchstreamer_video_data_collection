"""Bounded asynchronous writer: raw WAV, lossless synchronized MKV, audit CSVs."""
import csv
import json
import math
import queue
import threading
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path
import av
import numpy as np
import soundfile as sf


class Recording:
    def __init__(self, directory, name, config, shape, epoch, clock_info):
        if not name or name in {'.', '..'} or Path(name).name != name or any(c in name for c in '/\\\0'):
            raise ValueError('Use a filename without folders.')
        base = Path(directory).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / name
        self.path.mkdir()  # Refuse to overwrite any earlier take.
        self.config, self.epoch = config, epoch
        self.end = math.inf
        self.accepting = True
        self.q = queue.Queue(maxsize=256)
        self.video_slots = threading.BoundedSemaphore(8)
        self.error = None
        self.audio_samples = self.video_frames = self.dropped_audio = self.dropped_video = 0
        self.last_audio_pts = self.last_video_pts = -1
        self.first_audio = self.first_video = None
        self.meta = {'version': 1, 'started_at': datetime.now().astimezone().isoformat(),
                     'epoch_monotonic_seconds': epoch, 'config': config, 'clock': clock_info,
                     'status': 'recording', 'channel_order': config['channels'],
                     'video_resolution': [int(shape[1]), int(shape[0])],
                     'notes': 'MKV timestamps share the recording epoch. WAV is contiguous; use audio_timestamps.csv for gaps and offsets. Independent USB devices are not hardware genlocked.'}
        self._metadata()
        try:
            self.wav = sf.SoundFile(str(self.path / 'audio.wav'), 'w', samplerate=config['sample_rate'],
                                    channels=len(config['channels']), subtype='PCM_24', format='WAV')
            self.container = av.open(str(self.path / 'synchronized.mkv'), 'w')
            self.video = self.container.add_stream('ffv1', rate=30)
            self.video.width, self.video.height = shape[1], shape[0]
            self.video.pix_fmt = 'bgr0'
            self.video.time_base = self.video.codec_context.time_base = Fraction(1, 1000000)
            self.audio = self.container.add_stream('pcm_s24le', rate=config['sample_rate'])
            self.audio.layout = f"{len(config['channels'])} channels"
            self.audio.time_base = Fraction(1, config['sample_rate'])
            self.af = open(self.path / 'audio_timestamps.csv', 'w', newline='')
            self.vf = open(self.path / 'video_timestamps.csv', 'w', newline='')
            self.acsv, self.vcsv = csv.writer(self.af), csv.writer(self.vf)
            self.acsv.writerow(['wav_start_sample', 'sample_count', 'time_seconds', 'adc_time_seconds', 'callback_status'])
            self.vcsv.writerow(['frame', 'time_seconds'])
            self.thread = threading.Thread(target=self._worker, name='disk-writer', daemon=True)
            self.thread.start()
        except Exception:
            for name in ['wav', 'container', 'af', 'vf']:
                resource = getattr(self, name, None)
                if resource is not None:
                    resource.close()
            self.meta['status'] = 'failed_to_start'
            self._metadata()
            raise

    def _metadata(self):
        tmp = self.path / 'metadata.json.tmp'
        tmp.write_text(json.dumps(self.meta, indent=2))
        tmp.replace(self.path / 'metadata.json')

    def submit(self, kind, data, timestamp, raw_adc=0, flags=''):
        if not self.accepting or self.error:
            return
        if kind == 'video' and not self.video_slots.acquire(blocking=False):
            self.dropped_video += 1
            return
        try:
            self.q.put_nowait((kind, data, timestamp, raw_adc, flags))
        except queue.Full:
            if kind == 'video':
                self.video_slots.release()
                self.dropped_video += 1
            else:
                self.dropped_audio += len(data)

    def _write(self, item):
        kind, data, timestamp, raw_adc, flags = item
        rate = self.config['sample_rate']
        if kind == 'audio':
            left = max(0, math.ceil((self.epoch - timestamp) * rate - 1e-6))
            right = min(len(data), math.ceil((self.end - timestamp) * rate - 1e-6)) if math.isfinite(self.end) else len(data)
            if right <= left:
                return
            data = np.ascontiguousarray(data[left:right, self.config['channels']])
            rel = timestamp + left / rate - self.epoch
            pts = round(rel * rate)
            if pts <= self.last_audio_pts:
                raise RuntimeError('Non-monotonic audio clock; take marked incomplete.')
            self.last_audio_pts = pts
            if self.first_audio is None:
                self.first_audio = rel
            self.wav.write(data)
            # Packed float input -> encoder converts to 24-bit PCM, preserving selected channel order.
            frame = av.AudioFrame.from_ndarray(data.reshape(1, -1), format='flt', layout=self.audio.layout)
            frame.sample_rate, frame.pts, frame.time_base = rate, pts, Fraction(1, rate)
            for packet in self.audio.encode(frame):
                self.container.mux(packet)
            self.acsv.writerow([self.audio_samples, len(data), f'{rel:.9f}', f'{raw_adc:.9f}', flags])
            self.audio_samples += len(data)
        elif self.epoch <= timestamp < self.end:
            rel = timestamp - self.epoch
            pts = round(rel * 1000000)
            if pts <= self.last_video_pts:
                raise RuntimeError('Non-monotonic camera clock; take marked incomplete.')
            self.last_video_pts = pts
            if self.first_video is None:
                self.first_video = rel
            frame = av.VideoFrame.from_ndarray(data, format='bgr24')
            frame.pts, frame.time_base = pts, Fraction(1, 1000000)
            for packet in self.video.encode(frame):
                self.container.mux(packet)
            self.vcsv.writerow([self.video_frames, f'{rel:.9f}'])
            self.video_frames += 1

    def _worker(self):
        try:
            while True:
                item = self.q.get()
                if item is None:
                    break
                try:
                    self._write(item)
                finally:
                    if item[0] == 'video':
                        self.video_slots.release()
            for stream in [self.audio, self.video]:
                for packet in stream.encode(None):
                    self.container.mux(packet)
        except Exception as exc:
            self.error = f'Disk/encoder error: {exc}'
        finally:
            for resource in [self.wav, self.container, self.af, self.vf]:
                try:
                    resource.close()
                except Exception as exc:
                    self.error = self.error or str(exc)

    def close(self, end=None, capture_errors=None):
        self.accepting = False
        self.end = end or time.monotonic()
        while self.thread.is_alive():
            try:
                self.q.put(None, timeout=.1)
                break
            except queue.Full:
                pass
        self.thread.join()
        issues = list(capture_errors or [])
        if self.error:
            issues.append(self.error)
        if self.dropped_audio or self.dropped_video:
            issues.append('Writer queue overflow: samples/frames were dropped.')
        if not self.audio_samples or not self.video_frames:
            issues.append('One or both streams contain no captured data.')
        self.meta.update(status='complete' if not issues else 'incomplete', errors=issues,
                         stopped_at=datetime.now().astimezone().isoformat(),
                         duration_seconds=self.end-self.epoch, audio_samples=self.audio_samples,
                         video_frames=self.video_frames, dropped_audio_samples=self.dropped_audio,
                         dropped_video_frames=self.dropped_video, first_audio_seconds=self.first_audio,
                         first_video_seconds=self.first_video)
        self._metadata()
        return {'path': str(self.path), **self.meta}
