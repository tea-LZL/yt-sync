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
yt-sync --retry-failed  # add to --download-only / --auto to also retry failed tracks
```

## Failure state

`~/.local/share/yt-sync/failed.json` maps track id → `{title, url, error, failed_at}`.
A failed download records the track; a successful download clears it. `diff`
routes recorded failures into a `failed` list (TUI filter `4`, red rows) instead
of `missing`, so they are never silently re-attempted by batch downloads. Retry
explicitly: `enter` on a failed row, or `--retry-failed` in CLI modes. Entries
for tracks no longer in the playlist are pruned on every refresh.

yt-dlp leaves garbage on failed downloads (0-byte `*.temp.opus`, stray `*.webp`
thumbnails) — this tool deletes `*.temp.*` files and thumbnail-images next to
matching audio files at startup (`cleanup_stale_artifacts`), after each failed
download (`cleanup_track_artifacts`), and `scan_local` ignores `.temp.` names.

## Config

`~/.config/yt-sync/config.toml`:

```toml
[yt-sync]
# Active configuration used by CLI modes.
playlist_url = "https://music.youtube.com/playlist?list=..."
music_dir = "/home/tea/Music/Youtube Music"
audio_format = "opus"
real_delete = false
theme = "textual-dark"

# Each saved entry has a visible name plus URL + directory.
[[yt-sync.saved_configs]]
name = "Road trips"
playlist_url = "https://music.youtube.com/playlist?list=..."
music_dir = "/home/tea/Music/Youtube Music"
```

Legacy single-pair configs and earlier saved URL/directory-only entries migrate
in memory to named entries. Their fallback name is the download-folder basename
(or `Saved configuration` for a root-like path). Activating or creating an entry
keeps the top-level URL/directory in sync so all existing CLI modes continue to
use the selected configuration.

Defaults for all keys live in `DEFAULTS` dict at top of file. Trash dir: `~/.local/share/yt-sync/trash/`.

Interactive `yt-sync` starts on a keyboard-first configuration home before
fetching the playlist. Each saved row leads with its name, then presents its URL
and directory; the active marker matches the active URL/directory pair. The
list is focused when entries exist, and Enter activates the highlighted
configuration. **Create new configuration** opens a required Name/URL/folder
form; the name receives first keyboard focus, while the directory picker and
typed paths remain available. **Edit** (button or `e` when the list is focused)
opens the same form prefilled with the selected configuration and shows
**Save changes**; the name still receives first focus, duplicate URL/directory
pairs are rejected with an inline error, and saving updates the saved entry
in place (creating the folder if needed) and keeps the chooser open on the
edited row. Saving an edit that changes the active configuration also updates
the top-level active URL/directory. Missing folders are created before saving.
Selecting or creating an entry saves it, promotes its URL/directory to the
top-level active pair, and only then refreshes. Press `h` from the dashboard to
reopen the configuration home; Escape from a later home leaves the dashboard
and config unchanged. CLI modes remain non-interactive and still require the
active playlist URL. The TUI uses a high-contrast dark slate palette, explicit
focus/hover states, stable first rendering, and an operational loader. The
dashboard keeps `1 All`, `2 Missing`, `3 Orphans`, and `4 Failed` visible as a
persistent filter hint. Escape keeps the initial configuration home open;
explicit Quit exits it and is reachable with Tab followed by Enter.

Downloads use yt-dlp to embed metadata and the thumbnail in the audio file.
Output files follow `Artist - Song name.opus` (or the configured audio format).

## TUI keybindings

| Key | Action |
|-----|--------|
| `j`/`k` | cursor down/up |
| `g`/`G` | top/bottom of list |
| `space` | toggle row selection |
| `escape` | clear selection |
| `1`/`2`/`3`/`4` | filter: All / Missing / Orphans / Failed |
| `d` | download (selected missing/failed rows, or all missing if none selected; a non-empty selection never falls back to all) |
| `t` | trash/delete (selected orphans, or all if none selected; a non-empty selection never falls back to all) |
| `x` | toggle delete mode (trash ↔ real delete) |
| `r` | refresh (re-fetch playlist + rescan local) |
| `h` | open saved configurations |
| `q` | quit |

## How matching works

1. Normalize both sides: lowercase, strip punctuation, collapse whitespace
2. Exact normalized match on full stem (`Artist - Title.opus` ↔ playlist title `Artist - Title`)
3. Title-portion match: if local filename contains ` - `, match on the last segment only (handles `Artist - Title.opus` ↔ playlist title `Title`)
4. Fallback: `difflib.SequenceMatcher` ratio > 0.85 catches near-misses

Current state: 17 missing, ~0 orphans (down from 49 after matching improvements).

## Architecture (ponytail)

Single file, no classes for data logic, just module-level functions + one Textual
`App` subclass with `ConfigurationHomeScreen`, `HomeScreen` (create form), and
`DirectoryPickerScreen` modals. Interactive startup picks or creates a saved
configuration before the first refresh; downloads run via `asyncio.to_thread`
through Textual workers — TUI stays responsive during yt-dlp calls.

## What was skipped

- **Restore from trash**: `~/.local/share/yt-sync/trash/` exists but no restore command. Move files back manually.
- **Per-item confirm dialog**: space+action is the confirm. Add `enter` keybinding for confirm dialog if safe defaults aren't enough.
- **Visual theme switching**: config key `theme` exists but is a no-op. Textual is dark by default. Wire CSS variable overrides to it.
- **Artist column in DataTable**: only Title column. Parse `Artist - Title` split in the table display if visual grouping matters.
- **Multi-category selection**: selection clears on filter switch (1/2/3). Cross-category batch ops would need selection persistence.
- **Download resume / skip-existing**: `--no-overwrites` is enabled, so existing output files are skipped.
- **Progress bar widget**: replaced by RichLog line-by-line output. Add `ProgressBar` widget back if visual progress matters more than log history.
- **Tests**: `python -m unittest test_yt_sync` covers setup validation/persistence, picker and startup lifecycle, selection→download/trash targeting, and Space keeping the cursor. Matching logic is still exercised via `yt-sync --diff-only`.
