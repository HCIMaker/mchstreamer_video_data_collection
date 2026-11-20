#!/bin/zsh
set -e
cd -- "$(dirname -- "$0")"
export PATH="$HOME/.local/bin:$PATH"
echo 'MCHStreamer capture console'
echo 'Open http://127.0.0.1:8000 in your browser after the server starts.'
echo 'Press Ctrl+C here to stop the server safely.'
uv run python -m data_collection
