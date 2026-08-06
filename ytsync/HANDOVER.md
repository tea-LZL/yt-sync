# yt-sync handover

## What

Single-file Python TUI (`~/.local/bin/yt-sync` → `/home/tea/repos/music_script/ytsync/yt_sync.py`) that diffs a YouTube Music playlist against local opus/mp3/m4a/ogg files and syncs gaps.

## Stack

Python 3.11+, Textual, yt-dlp, stdlib (tomllib, difflib, asyncio, subprocess).

## Modes

```
yt-sync              # Textual TUI (main interface)
yt-sync --diff-only  # print missing + orphan lists, no network unless playlist fetch needed
yt-sync --download-only  # fetch + download missing, no TUI
yt-sync --auto       # non-interactive: download missing + trash orphans
```

## Config

`~/.config/yt-sync/config.toml`:

```toml
[yt-sync]
playlist_url = "https://music.youtube.com/playlist?list=PLIIlrAvOGW9yv-Y8jI5tHOSMaywVTuRGA"
music_dir = "/home/tea/Music/Youtube Music"
audio_format = "opus"
real_delete = false
theme = "textual-dark"
```

Defaults for all keys live in `DEFAULTS` dict at top of file. Trash dir: `~/.local/share/yt-sync/trash/`.

Downloads use yt-dlp to embed metadata and the thumbnail in the audio file.
Output files follow `Artist - Song name.opus` (or the configured audio format).

## TUI keybindings

| Key | Action |
|-----|--------|
| `j`/`k` | cursor down/up |
| `g`/`G` | top/bottom of list |
| `space` | toggle row selection |
| `escape` | clear selection |
| `1`/`2`/`3` | filter: All / Missing / Orphans |
| `d` | download (selected missing, or all if none selected) |
| `t` | trash/delete (selected orphans, or all if none selected) |
| `x` | toggle delete mode (trash ↔ real delete) |
| `r` | refresh (re-fetch playlist + rescan local) |
| `q` | quit |

## How matching works

1. Normalize both sides: lowercase, strip punctuation, collapse whitespace
2. Exact normalized match on full stem (`Artist - Title.opus` ↔ playlist title `Artist - Title`)
3. Title-portion match: if local filename contains ` - `, match on the last segment only (handles `Artist - Title.opus` ↔ playlist title `Title`)
4. Fallback: `difflib.SequenceMatcher` ratio > 0.85 catches near-misses

Current state: 17 missing, ~0 orphans (down from 49 after matching improvements).

## Architecture (ponytail)

Single file, no classes for data logic, just module-level functions + one Textual `App` subclass. Downloads run via `asyncio.to_thread` through Textual workers — TUI stays responsive during yt-dlp calls.

## What was skipped

- **Restore from trash**: `~/.local/share/yt-sync/trash/` exists but no restore command. Move files back manually.
- **Per-item confirm dialog**: space+action is the confirm. Add `enter` keybinding for confirm dialog if safe defaults aren't enough.
- **Visual theme switching**: config key `theme` exists but is a no-op. Textual is dark by default. Wire CSS variable overrides to it.
- **Artist column in DataTable**: only Title column. Parse `Artist - Title` split in the table display if visual grouping matters.
- **Multi-category selection**: selection clears on filter switch (1/2/3). Cross-category batch ops would need selection persistence.
- **Download resume / skip-existing**: `--no-overwrites` is enabled, so existing output files are skipped.
- **Progress bar widget**: replaced by RichLog line-by-line output. Add `ProgressBar` widget back if visual progress matters more than log history.
- **Tests**: ponytail self-check only (`yt-sync --diff-only`). Add `test_yt_sync.py` if matching logic grows complex.
