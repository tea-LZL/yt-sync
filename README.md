# yt-sync

YouTube Music playlist ↔ local file sync tool. Fetches a playlist via yt-dlp, diffs against local audio files, downloads missing tracks, trashes/deletes orphans.

## Install

```bash
pip install textual yt-dlp
# Install ffmpeg with your system package manager as well.
mkdir -p ~/.config/yt-sync
ln -s "$PWD/ytsync/yt_sync.py" ~/.local/bin/yt-sync
```

## Config

`~/.config/yt-sync/config.toml`:

```toml
[yt-sync]
playlist_url = "https://music.youtube.com/playlist?list=..."
music_dir = "/home/tea/Music/Youtube Music"
audio_format = "opus"
real_delete = false       # false = move orphans to trash dir
theme = "textual-dark"
```

Downloads are converted to the configured audio format with metadata and the
thumbnail embedded. Files are named `Artist - Song name.opus` (or the
configured audio extension).

## Usage

```
yt-sync              # TUI
yt-sync --diff-only  # print missing + orphan lists
yt-sync --download-only  # fetch + download missing, no TUI
yt-sync --auto       # download missing + trash orphans, no TUI
```

## TUI keys

| Key | Action |
|-----|--------|
| `j` / `k` | cursor down / up |
| `g` / `G` | top / bottom |
| `space` | toggle row selection |
| `escape` | clear selection |
| `1` / `2` / `3` | filter All / Missing / Orphans |
| `d` | download (selected missing, or all) |
| `t` | trash/delete (selected orphans, or all) |
| `x` | toggle trash ↔ real-delete |
| `r` | refresh |
| `q` | quit |

Trash dir: `~/.local/share/yt-sync/trash/`. Set `real_delete = true` in config or press `x` in TUI to delete files permanently.

## old/

Archived: `dlsync` (bash + gum downloader), `download_script.txt` (one-liner yt-dlp command).
