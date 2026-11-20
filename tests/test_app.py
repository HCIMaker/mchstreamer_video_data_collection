import time
from fastapi.testclient import TestClient
from data_collection.app import app, engine


def test_demo_lifecycle_and_saved_take(tmp_path):
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        denied = client.post('/api/preview/start', json={'demo':True}, headers={'Origin':'https://untrusted.test'})
        assert denied.status_code == 403
        assert client.post('/api/record/start', json={}).status_code == 400
        response = client.post('/api/preview/start', json={'demo':True})
        assert response.status_code == 200, response.text
        assert client.post('/api/record/start', json={'channels':[], 'directory':str(tmp_path)}).status_code == 400
        response = client.post('/api/record/start', json={'channels':[0,15], 'directory':str(tmp_path), 'filename':'test_take'})
        assert response.status_code == 200, response.text
        time.sleep(.6)
        result = client.post('/api/record/stop').json()
        assert result['last_take']['status'] == 'complete', result
        assert result['last_take']['channel_order'] == [0,15]
        assert result['last_take']['audio_samples'] > 10000
        assert result['last_take']['video_frames'] > 5
        assert client.post('/api/record/start', json={'directory':str(tmp_path),'filename':'test_take'}).status_code == 400
        assert client.post('/api/preview/stop').json()['running'] is False
