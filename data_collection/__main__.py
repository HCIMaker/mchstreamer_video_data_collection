import argparse
import uvicorn

parser = argparse.ArgumentParser(description='Local MCHStreamer + camera collection console')
parser.add_argument('--port', type=int, default=8000)
args = parser.parse_args()
uvicorn.run('data_collection.app:app', host='127.0.0.1', port=args.port)
