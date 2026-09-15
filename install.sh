#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
VENV="$DATA_HOME/yt-sync/venv"

if ! command -v python3 >/dev/null; then
  echo "python3 is required." >&2
  exit 1
fi

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -e "$ROOT"

mkdir -p "$BIN_HOME"
ln -sfn "$VENV/bin/yt-sync" "$BIN_HOME/yt-sync"

if ! command -v ffmpeg >/dev/null; then
  echo "ffmpeg is not on PATH. Install it with your package manager, e.g.:"
  if command -v pacman >/dev/null; then
    echo "  sudo pacman -S ffmpeg"
  elif command -v apt-get >/dev/null; then
    echo "  sudo apt-get install ffmpeg"
  elif command -v dnf >/dev/null; then
    echo "  sudo dnf install ffmpeg"
  else
    echo "  ffmpeg"
  fi
else
  echo "ffmpeg: $(command -v ffmpeg)"
fi

echo "Installed yt-sync -> $BIN_HOME/yt-sync"
if [[ ":$PATH:" != *":$BIN_HOME:"* ]]; then
  echo "Add $BIN_HOME to PATH to run: yt-sync"
else
  echo "Run: yt-sync"
fi
