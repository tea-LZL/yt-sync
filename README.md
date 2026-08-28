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
# Active configuration used by CLI modes.
playlist_url = "https://music.youtube.com/playlist?list=..."
music_dir = "/home/tea/Music/Youtube Music"
audio_format = "opus"
real_delete = false       # false = move orphans to trash dir
theme = "textual-dark"

# Every saved configuration has a display name plus its playlist and folder.
[[yt-sync.saved_configs]]
name = "Road trips"
playlist_url = "https://music.youtube.com/playlist?list=..."
music_dir = "/home/tea/Music/Youtube Music"
```

Downloads are converted to the configured audio format with metadata and the
thumbnail embedded. Files are named `Artist - Song name.opus` (or the
configured audio extension).

## Interactive setup

Running `yt-sync` opens a keyboard-first configuration home before the first
playlist fetch. It lists saved configurations by **Name**, followed by the
playlist URL and download folder; use arrow keys and Enter to activate one, or
Tab to **Create new configuration**. The creation form requires a configuration
name, accepts a typed folder path or **Browse**, and creates missing destination
folders automatically. Activating or creating a configuration saves it and
mirrors its URL/directory to the top-level active pair used by CLI modes. Older
URL/directory-only records remain usable and receive a display name from their
download folder when next saved. Press `h` on the dashboard to reopen the
configuration home without changing anything until a configuration is selected.
The interface uses a compact, high-contrast dark layout with stable first
renders and an operational loading indicator. The dashboard keeps `1 All`, `2
Missing`, `3 Orphans`, and `4 Failed` visible as a persistent filter hint. On
the initial configuration home, `Escape` keeps it open; use Tab to reach the
explicit **Quit** action and press Enter when you intend to leave.

## Usage

```
yt-sync              # TUI
yt-sync --diff-only  # print missing + orphan lists, no network unless playlist fetch needed
yt-sync --download-only  # fetch + download missing, no TUI
yt-sync --auto       # non-interactive: download missing + trash orphans
yt-sync --retry-failed  # add to --download-only / --auto to also retry failed tracks
```

## TUI keys

| Key | Action |
|-----|--------|
| `j` / `k` | cursor down / up |
| `g` / `G` | top / bottom |
| `space` | toggle row selection |
| `escape` | clear selection |
| `1` / `2` / `3` / `4` | filter All / Missing / Orphans / Failed |
| `d` | download (selected missing/failed rows, or all missing if none selected; a non-empty selection never falls back to all) |
| `t` | trash/delete (selected orphans, or all if none selected; a non-empty selection never falls back to all) |
| `x` | toggle trash ↔ real-delete |
| `r` | refresh |
| `h` | open saved configurations |
| `q` | quit |

Failed downloads are recorded in `~/.local/share/yt-sync/failed.json` and shown
under the **Failed** filter (`4`) — they are **not** re-attempted by batch
downloads. Retry one with `enter` on its row, or retry the whole batch with
`--retry-failed` (CLI only). A successful retry clears the failure record.
Leftover yt-dlp artifacts (`.temp.*` files, stray thumbnails) are cleaned up
automatically on start and after each failed download.

Trash dir: `~/.local/share/yt-sync/trash/`. Set `real_delete = true` in config or press `x` in TUI to delete files permanently.

## old/

Archived: `dlsync` (bash + gum downloader), `download_script.txt` (one-liner yt-dlp command).
