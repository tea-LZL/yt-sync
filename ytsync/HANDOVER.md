# yt-sync handover

## What

Single-file Python TUI (`~/.local/bin/yt-sync` → `/home/tea/repos/music_script/ytsync/yt_sync.py`) that diffs a YouTube Music playlist against local opus/mp3/m4a/ogg files and syncs gaps.

## Stack

Python 3.11+, Textual, yt-dlp[default] (mutagen), ffmpeg, stdlib (tomllib, difflib, asyncio, subprocess).
Install with `./install.sh` from the repo root. Runtime calls yt-dlp as
`sys.executable -m yt_dlp` so mutagen comes from the same environment.

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
# "full" (default), "basic" (no CSS transitions) or "none" (no transitions, static
# spinner). Use basic/none for tmux or ssh sessions with a slow link.
animations = "full"

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
explicit Quit exits it and is reachable with Tab followed by Enter. Every
configuration-home action shows its shortcut in the button label — `New
configuration [n]`, `Edit [e]`, and `Quit [q]` (initial) or `Cancel [esc]`
(later) — with a hint line beneath the buttons (`↑↓/jk move · enter open · e
edit · n new · q quit`). `j`/`k` (or the arrow keys) move the saved list, `enter`
opens the highlighted entry, `n` opens the create form from anywhere on the
home, and `q` quits from the initial home or cancels a later one.

Downloads use yt-dlp to embed metadata and the thumbnail in the audio file.
Output files follow `Artist - Song name.opus` (or the configured audio format).

## TUI keybindings

### Configuration home

| Key | Action |
|-----|--------|
| `j`/`k`, arrows | move the saved-configuration highlight |
| `enter` | open (activate) the highlighted configuration |
| `n` | create a new configuration |
| `e` | edit the highlighted configuration |
| `q` | quit (initial home) / cancel a later home |
| `escape` | back; on the initial home it keeps the chooser open with a reminder |
| `tab`/`shift+tab` | move focus between the list and the action buttons |

### Dashboard

| Key | Action |
|-----|--------|
| `j`/`k` | cursor down/up |
| `g`/`G` | top/bottom of list |
| `space` | toggle row selection |
| `escape` | clear selection; while a batch runs, stop it after the current track |
| `1`/`2`/`3`/`4` | filter: All / Missing / Orphans / Failed (selection and cursor follow the same tracks across filters; selection clears only when new data is loaded) |
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

The fuzzy fallback is the only super-linear part of a refresh. It is gated by two
provable upper bounds (length bound, then `quick_ratio`) and reuses one
`SequenceMatcher` per local file, so an unmatched playlist entry only pays for the
pairs that could still clear 0.85. A 100-unmatched × 1000-file rescan diffs in
about 0.26 s; the pathological 1000 × 4000 fully-disjoint case still takes ~10 s
(≈2.6 µs per comparison).

## Refresh scoping

`r`, the first load and a configuration change fetch the playlist. Downloading,
trashing and retrying a row only rescan the folder and re-diff against the cached
playlist — no yt-dlp round trip — and the loader plus `busy` state stay on until
the table reflects the result. The table keeps its cursor row and scroll offset
across rebuilds. `escape` during a batch requests a stop; the current track
finishes, then the summary reports `stopped at k/n` and a local reconcile runs.

Rows are keyed by identity, not position: `<track id>#<occurrence>` for playlist
entries (duplicate videos get distinct keys) and the file path for orphans. That
is why the cursor and the `▶` selection stay on the same tracks when you switch
filters or rebuild the table; the visible number column is derived from display
order. Tables over 1000 rows are committed in 500-row chunks off the event loop,
so a filter switch on a huge playlist never blocks for the whole insert.
`d`/`t` only act on selected rows that are visible in the current filter — a
selection hidden by another filter is reported as "Nothing to …", never acted on
sight unseen.

The last rendered view per configuration is cached in
`~/.local/share/yt-sync/view-cache.json` (8 newest entries, atomic replace).
Startup paints those rows immediately, the status bar marks them
`cached 12m · refreshing…`, and `d`/`t`/`enter` stay paused until the first real
refresh lands. If that refresh fails the cached view stays, the status bar switches
to `cached … · refresh failed — r to retry`, and the action guards say the same
instead of pretending a refresh is running; `r` retries and re-enables actions.
Corrupt or foreign cache files are ignored, and the cache is only read by the TUI
(CLI modes never touch it).

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
- **Cross-filter batch actions**: selection persists across filters, but `d`/`t`
  only target selected rows visible in the current filter. If the whole selection
  is hidden by the filter, the app says so (and does not fall back to "all", which
  would act on rows the user cannot see). Acting on hidden rows would need an
  explicit "apply to selection across filters" action.
- **Download resume / skip-existing**: `--no-overwrites` is enabled, so existing output files are skipped.
- **Progress bar widget**: replaced by RichLog line-by-line output. Add `ProgressBar` widget back if visual progress matters more than log history.
- **Tests**: `python -m unittest test_yt_sync` covers setup validation/persistence, picker and startup lifecycle, selection→download/trash targeting, and Space keeping the cursor. Matching logic is still exercised via `yt-sync --diff-only`.
