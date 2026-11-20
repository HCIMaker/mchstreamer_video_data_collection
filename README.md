# MCHStreamer capture console

Local web GUI for the MCHStreamer PDM16 and a macOS camera. Python 3.12+ and `uv` are required. This implementation uses native AVFoundation camera capture and is intended for macOS.

```sh
cd /Users/peilab/Desktop/mchstreamer
uv sync
uv run python -m data_collection
```

Open http://127.0.0.1:8000. Use `--port 8001` if needed. Allow camera and microphone access for the launching terminal/app when macOS asks. If access was denied, enable it in System Settings → Privacy & Security, then restart the server.

1. Choose the MCHStreamer audio input and camera. All channels 0–15 and 44.1 kHz are selected by default. Only rates accepted by the input device are offered.
2. Click **Start preview**. Check live camera and per-channel waveforms. Channel buttons add/remove waveform panels and choose which channels are saved. Auto scale affects display only; turn it off for a fixed ±1 scale. Peaks are shown in dBFS and clipping is flagged.
3. Enable **Spectrogram** if desired and choose one selected channel. It uses a 1024-point Hann FFT and a rolling linear-frequency display. Display work is throttled and separate from disk recording.
4. Set a session name or leave it blank for the current local datetime. Set a save folder or retain `/Users/peilab/Desktop/mchstreamer/data_collection/data`.
5. Click **Save / Record** or press **Z**, then click **Stop & save** or press **Z** again. The shortcut is ignored while editing a field or using a dropdown. Preview stays active between takes. Device/rate and recording channel settings are locked during a take.
6. Click **Close preview** when finished, then Ctrl+C in the server terminal. Closing the browser does **not** stop an ongoing recording; reconnect to stop it, or stop the server gracefully.

Use **Demo mode** to test the interface without hardware. All demo metadata is marked synthetic. Demo mode does not validate device performance or synchronization.

## Saved session

Each take gets its own folder; existing names are never overwritten.

- `audio.wav`: original selected audio channels in ascending hardware channel order, 24-bit PCM at the requested rate, without display scaling or resampling.
- `synchronized.mkv`: lossless FFV1 video and 24-bit PCM audio, both timestamped on the recording timeline. Use a player supporting FFV1 and multichannel PCM, such as VLC. Unusual channel counts may need a scientific decoder such as PyAV rather than consumer playback software.
- `audio_timestamps.csv`: starting sample in the WAV, number of samples, first-sample time relative to the take epoch, raw ADC timestamp, and capture flags for every audio block.
- `video_timestamps.csv`: saved video frame index and capture time relative to the take epoch.
- `metadata.json`: settings, physical channel order, clock mapping, timestamps, sample/frame counts, drops, errors, and completion state. `incomplete` means an error or a drop was detected; inspect before using the take.

WAV is contiguous and does not itself express clock offsets or gaps. For aligned playback use the MKV; for analysis use the timestamp CSVs. The MKV retains frame timing rather than assuming a constant frame rate (Matroska timestamps are quantized to milliseconds). WAV stores channels sequentially: WAV column 0 corresponds to the first entry in `channel_order`.

## Synchronization and limits

Audio timestamps come from PortAudio's **ADC first-sample timestamps**, not callback arrival times. A bracketed clock calibration maps PortAudio time to Python monotonic time. Camera frames use native **AVFoundation sample presentation timestamps**, converted from the capture session clock to the macOS host clock and then to that same monotonic clock. Both tracks are encoded using their actual relative timestamps, preserving initial offsets and observed gaps. Clock calibration uncertainty is saved in metadata; this is not a measurement of physical AV alignment accuracy.

The MCHStreamer and USB camera have independent oscillators and device/driver latency. Software timestamp alignment cannot promise sample-accurate physical synchronization or hardware genlock. Before precision experiments, record an audible/visible calibration event (e.g. a clap with visible contact), measure residual offset, and repeat near the end of long sessions to assess drift. Exposure duration, rolling shutter, USB/driver latency, and device clock accuracy remain factors. Do not interpret preview arrival ages as measured AV synchronization error.

The camera requests 1280×720; delivered frames depend on device support. Native late-frame drops, audio overflow flags, stale streams and disk/encoder errors are reported. A bounded writer queue avoids unbounded memory growth. Preview receives reduced waveforms and JPEGs at approximately 8 Hz; this does not reduce recorded audio or camera frame rates. Spectrogram computation only runs when enabled. FFV1 is lossless but can produce large files and use CPU. Monitor free disk space and reported drops during long recordings. A stalled stream or writer failure is displayed; stop the take and inspect its integrity metadata.

The server binds only to `127.0.0.1`; keep it local. Run one server process/worker to own the capture devices. Avoid force killing the process during capture: graceful Stop or Ctrl+C finalizes files and metadata. An abrupt power loss may leave an unfinished take.

## Development checks

```sh
uv run pytest -q
```

Tests encode/decode real WAV/MKV outputs, verify selected channel order, non-default channel counts, frame timestamps, boundary cropping, collision protection, failure metadata and the demo API lifecycle.

Timestamp API references: [python-sounddevice stream timestamps](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html), [AVFoundation session clock](https://developer.apple.com/documentation/avfoundation/avcapturesession/synchronizationclock), [PyAV timestamps](https://pyav.basswood.io/docs/stable/api/time.html).
