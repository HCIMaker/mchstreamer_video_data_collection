import csv
import json
import time
from fractions import Fraction
import av
import numpy as np
import pytest
import soundfile as sf
from data_collection.recording import Recording


def test_recording_channel_order_timestamps_and_mux(tmp_path):
    config = {'sample_rate': 44100, 'channels': [0, 3, 15], 'demo': True}
    rec = Recording(tmp_path, 'take', config, (64, 96, 3), 100., {})
    data = np.tile(np.arange(16, dtype=np.float32)/32, (1024, 1))
    for n in range(12):
        rec.submit('audio', data, 100+n*1024/44100)
        if n % 2 == 0:
            rec.submit('video', np.zeros((64, 96, 3), dtype='uint8'), 100+.015+n*1024/44100)
    result = rec.close(101.)
    assert result['status'] == 'complete', result
    samples, rate = sf.read(tmp_path/'take/audio.wav')
    assert rate == 44100 and samples.shape == (12288, 3)
    np.testing.assert_allclose(samples[0], np.array([0, 3, 15])/32, atol=1e-6)
    with av.open(str(tmp_path/'take/synchronized.mkv')) as container:
        assert len(container.streams.audio) == len(container.streams.video) == 1
        video = list(container.decode(video=0))
        assert len(video) == 6
        assert abs(float(video[0].pts*video[0].time_base)-.015) < .002
        assert float(video[-1].pts*video[-1].time_base) > .24
    with av.open(str(tmp_path/'take/synchronized.mkv')) as container:
        audio = list(container.decode(audio=0))
        assert sum(f.samples for f in audio) == 12288
        assert len(audio[0].layout.channels) == 3
    assert json.loads((tmp_path/'take/metadata.json').read_text())['channel_order'] == [0,3,15]
    with pytest.raises(FileExistsError):
        Recording(tmp_path, 'take', config, (64,96,3), 100, {})


@pytest.mark.parametrize('n', [1, 9, 16])
def test_arbitrary_channel_count(tmp_path, n):
    rec = Recording(tmp_path, f'ch{n}', {'sample_rate':48000, 'channels':list(range(n))}, (32,32,3), 10., {})
    rec.submit('audio', np.zeros((1024,16),dtype='float32'), 10.)
    rec.submit('video', np.zeros((32,32,3),dtype='uint8'), 10.)
    assert rec.close(11.)['status'] == 'complete'


def test_crop_and_capture_failure(tmp_path):
    rec = Recording(tmp_path, 'cropped', {'sample_rate':10000, 'channels':[1]}, (32,32,3), 10., {})
    rec.end = 10.05
    rec.submit('audio', np.zeros((1024,16),dtype='float32'), 9.99)
    rec.submit('video', np.zeros((32,32,3),dtype='uint8'), 10.01)
    result = rec.close(10.05, ['Input overflow'])
    assert result['status'] == 'incomplete'
    assert result['audio_samples'] == 500
    with open(tmp_path/'cropped/audio_timestamps.csv') as f:
        row=list(csv.DictReader(f))[0]
        assert float(row['time_seconds']) == pytest.approx(0)


def test_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError):
        Recording(tmp_path, '../outside', {}, (32,32,3), 0, {})
