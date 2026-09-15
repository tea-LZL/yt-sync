# yt-sync Home Screen and UI Polish Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Give the interactive `yt-sync` TUI a first-run/setup home screen for entering a playlist URL and choosing a destination folder, then refresh the existing sync dashboard with a clearer modern visual hierarchy and restrained motion.

**Architecture:** Keep the current single-file Python/Textual architecture and preserve the existing playlist diff, failure-state, download, and trash semantics. Add a reusable setup result/validation boundary, a modal `DirectoryPickerScreen`, and a modal `HomeScreen` over the existing dashboard so the dashboard implementation does not need to be split into new modules. The interactive path will no longer fetch on startup until the user submits the setup form; CLI-only modes keep their current non-interactive behavior.

**Tech Stack:** Python 3.11+, Textual, yt-dlp, stdlib (`dataclasses`, `pathlib`, `tomllib`, `urllib.parse`, `unittest`); no new runtime dependencies.

---

## Investigated baseline

### Verified repository state

- Repository root: `/home/tea/repos/music_script`.
- Branch: `master`, tracking `origin/master`.
- Working tree is not clean: seven existing changes are staged, including the current `ytsync/yt_sync.py`, `ytsync/test_yt_sync.py`, README/handover updates, generated `__pycache__` files, and `ytsync/yt-sync.png`. Do not discard, restage, or commit those changes while implementing this plan.
- No project-local `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, package manifest, lockfile, or existing `docs/plans/` directory was found.
- The install contract is documented in `README.md:5-12`: `textual` and `yt-dlp` are installed with pip and `ffmpeg` is supplied by the system.
- `ytsync/yt-sync.png` is a stylized red/white yt-sync logo, not a screenshot of the current TUI. Textual cannot use it as a raster background without adding a separate rendering path; keep branding text/icon-based in this pass.

### Current runtime flow

```text
main() [ytsync/yt_sync.py:967-1048]
  -> load_config()
  -> hard-fail when playlist_url is empty
  -> cleanup_stale_artifacts()
  -> YTSyncApp(cfg)
  -> YTSyncApp.on_mount() [574-583]
  -> _do_refresh() [604-612]
  -> fetch_playlist() + scan_local() + build_diff() [614-641]
  -> DataTable/RichLog dashboard [643-711]
```

- Config defaults and loading are at `ytsync/yt_sync.py:19-63`; `playlist_url` defaults to empty and `music_dir` defaults to `~/Music`.
- `download_track()` at `ytsync/yt_sync.py:119-155` sends the configured music directory to yt-dlp but does not create that directory itself.
- The current dashboard is composed at `ytsync/yt_sync.py:566-572`; visual CSS and the help modal occupy `ytsync/yt_sync.py:358-531`.
- The current `on_mount()` immediately fetches using `self.cfg["playlist_url"]`, so an empty first-run config cannot reach a setup UI.
- Existing tests at `ytsync/test_yt_sync.py:38-160` cover selection targeting and cursor stability, but no setup form, config persistence, startup lifecycle, or visual states.

### Baseline verification

- `python -m py_compile yt_sync.py test_yt_sync.py` passes from `ytsync/`.
- `python -m unittest test_yt_sync -v` cannot collect tests in the active environment: `ModuleNotFoundError: No module named 'textual'` while importing `yt_sync.py`.
- `python --version` reports Python 3.11.15. `python -m pip show textual yt-dlp` reports neither package in this interpreter.
- Before executing implementation tasks, provision the documented dependencies with `python -m pip install textual yt-dlp` (and system `ffmpeg` if it is not already present). This is an environment preflight, not a repository dependency change.

## Scope and non-goals

### In scope

- Interactive home/setup screen shown before the first refresh.
- Editable playlist URL input, editable destination-folder input, and a Textual `DirectoryTree` browse flow.
- Inline validation for blank/invalid HTTP(S) URLs and blank destinations; allow a new destination path and create it before syncing.
- Persist the submitted URL and destination in the existing TOML config so the next launch is prefilled; preserve the existing audio format, trash, delete, and theme settings.
- `h`/Home or Setup action from the dashboard to reopen the form without losing the running dashboard; canceling a reopened form leaves the dashboard unchanged.
- Modern, high-contrast dark TUI styling with semantic palette constants, crisp borders, clear focus/hover/disabled states, and short one-shot opacity/offset animations.
- Regression tests, README updates, and handover documentation.

### Explicitly out of scope

- Changing yt-dlp arguments, playlist matching, failure-state semantics, retry rules, trash behavior, or CLI flags.
- Adding a native GTK/Qt file chooser, a browser UI, a second frontend framework, or new runtime dependencies.
- Rewriting the single-file data layer into modules or introducing a settings subsystem beyond the existing TOML keys.
- Implementing a full light/dark theme selector. The existing `theme` key remains compatible; this pass improves the current dark presentation.
- Adding parallel downloads, resume support, restore-from-trash, or the deferred progress-bar redesign.

## Invariants and design decisions

- Interactive startup must be usable with an empty `playlist_url`; only `--diff-only`, `--download-only`, and `--auto` may require a configured URL before doing work.
- The home form never performs network I/O. It only validates and returns values; `_do_refresh()` remains the sole path that starts playlist fetching.
- A submitted destination is expanded with `Path.expanduser()`, created with `mkdir(parents=True, exist_ok=True)`, and then used by the existing scan/download flow. Do not call `resolve()` because the path may not exist yet.
- A failed form submission stays on the home screen and shows an actionable inline error. It must not mutate `self.cfg` or rewrite the config file.
- Cancel does not write anything. The initial home screen's cancel/quit action exits; a later home/settings screen's cancel action returns to the current dashboard.
- Existing non-empty selection behavior, cursor position, failure filtering, and real-delete safety remain unchanged.
- Motion is limited to short opacity/offset transitions for surfaces and existing operational loading feedback. Do not add decorative infinite animations, glow, blur, or per-row animation.

---

## Task 1: Add pure setup-value validation

**Objective:** Introduce a typed setup result and deterministic validation without touching the Textual lifecycle.

**Files:**
- Modify: `ytsync/yt_sync.py:11-17, 19-63, 66-88`
- Test: `ytsync/test_yt_sync.py` (add a `SetupValidationTests` class near the existing pure target-resolution tests)

**Step 1: Write failing tests**

Add tests for the exact contract:

```python
class SetupValidationTests(unittest.TestCase):
    def test_rejects_blank_url(self):
        values, error = validate_setup_values("  ", "/tmp/music")
        self.assertIsNone(values)
        self.assertEqual(error, "Enter a playlist URL.")

    def test_rejects_non_http_url(self):
        values, error = validate_setup_values("ftp://example.test/list", "/tmp/music")
        self.assertIsNone(values)
        self.assertEqual(error, "Enter a valid HTTP(S) playlist URL.")

    def test_expands_destination_without_resolving_it(self):
        values, error = validate_setup_values(
            "https://music.youtube.com/playlist?list=abc", "~/Music/New Playlist"
        )
        self.assertEqual(error, "")
        self.assertEqual(values.playlist_url, "https://music.youtube.com/playlist?list=abc")
        self.assertEqual(values.music_dir, str(Path.home() / "Music/New Playlist"))

    def test_rejects_blank_destination(self):
        values, error = validate_setup_values("https://example.test/list", " ")
        self.assertIsNone(values)
        self.assertEqual(error, "Enter a destination folder.")
```

Use `urllib.parse.urlparse`; validation should require `http` or `https` plus a non-empty network location, but should not over-restrict yt-dlp-supported hosts.

**Step 2: Run the focused test to verify failure**

Run: `python -m unittest test_yt_sync.SetupValidationTests -v`

Expected: FAIL with `NameError: name 'validate_setup_values' is not defined` (after the Textual dependency preflight has been completed).

**Step 3: Write the minimal implementation**

Add `from urllib.parse import urlparse`, then add this data boundary beside the other dataclasses:

```python
@dataclass(frozen=True)
class SetupValues:
    playlist_url: str
    music_dir: str


def validate_setup_values(url: str, music_dir: str) -> tuple[SetupValues | None, str]:
    url = url.strip()
    music_dir = music_dir.strip()
    if not url:
        return None, "Enter a playlist URL."
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "Enter a valid HTTP(S) playlist URL."
    if not music_dir:
        return None, "Enter a destination folder."
    return SetupValues(url, str(Path(music_dir).expanduser())), ""
```

Do not create directories or write config in this pure helper.

**Step 4: Run the focused test to verify the pass**

Run: `python -m unittest test_yt_sync.SetupValidationTests -v`

Expected: all setup-validation tests pass.

---

## Task 2: Persist submitted setup values safely

**Objective:** Save the home-screen URL and destination using the existing TOML schema without adding a TOML-writing dependency.

**Files:**
- Modify: `ytsync/yt_sync.py:19-63`
- Test: `ytsync/test_yt_sync.py` (add `ConfigPersistenceTests`)

**Step 1: Write failing tests**

Patch the module-level `CONFIG_PATH` to a temporary file, save a config containing the submitted values, reload it, and assert the existing settings survive:

```python
class ConfigPersistenceTests(unittest.TestCase):
    def test_save_config_round_trips_setup_values_and_existing_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            cfg = {
                **DEFAULTS,
                "playlist_url": "https://music.youtube.com/playlist?list=abc",
                "music_dir": str(Path(tmp) / "Music"),
                "real_delete": True,
            }
            with patch("yt_sync.CONFIG_PATH", config_path):
                save_config(cfg)
                loaded = load_config()
        self.assertEqual(loaded["playlist_url"], cfg["playlist_url"])
        self.assertEqual(loaded["music_dir"], cfg["music_dir"])
        self.assertTrue(loaded["real_delete"])
```

Add `tempfile` and `unittest.mock.patch` imports only where needed.

**Step 2: Run the focused test to verify failure**

Run: `python -m unittest test_yt_sync.ConfigPersistenceTests -v`

Expected: FAIL because `save_config` is not defined.

**Step 3: Write the minimal implementation**

Add a small TOML serializer for the known `DEFAULTS` keys. Use `json.dumps(..., ensure_ascii=False)` for quoted strings, lowercase TOML booleans, create the config parent directory, write to a sibling temporary file, and replace the target atomically. Do not write `failed.json` data into the config and do not read or print credentials.

The serialized section should have this shape, with values filled from `cfg`:

```toml
[yt-sync]
playlist_url = "..."
music_dir = "..."
audio_format = "opus"
trash_dir = "..."
real_delete = false
theme = "textual-dark"
```

Keep the existing config keys and defaults intact; the home screen only changes `playlist_url` and `music_dir` in the in-memory dict before calling `save_config`.

**Step 4: Run the focused test to verify the pass**

Run: `python -m unittest test_yt_sync.ConfigPersistenceTests -v`

Expected: the round-trip test passes and the temporary config is removed with the temporary directory.

---

## Task 3: Add the directory picker screen

**Objective:** Let the user browse existing directories while retaining the editable path input as the source of truth.

**Files:**
- Modify: `ytsync/yt_sync.py:330-340` for imports and immediately before `HelpScreen` for the new screen/helper
- Test: `ytsync/test_yt_sync.py` (add pure tests for the picker-root helper)

**Step 1: Write failing tests**

Test a helper such as `nearest_existing_directory()` with an existing temporary directory and a not-yet-created child path. The helper must return the nearest existing directory, falling back to `Path.home()` only when no candidate exists.

**Step 2: Run the focused test to verify failure**

Run: `python -m unittest test_yt_sync.DirectoryPickerTests -v`

Expected: FAIL because the helper is not defined.

**Step 3: Write the minimal implementation**

Import `Button`, `DirectoryTree`, and `Input` from `textual.widgets` as needed. Add:

```python
def nearest_existing_directory(path: str) -> Path:
    candidate = Path(path).expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate if candidate.is_dir() else Path.home()
```

Add `DirectoryPickerScreen(ModalScreen)` with:

- a `DirectoryTree(nearest_existing_directory(initial_path), id="directory-tree")`;
- a `#picker-selection` label showing the currently selected directory;
- `Choose` and `Cancel` buttons with explicit IDs;
- a `DirectoryTree.DirectorySelected` handler that updates the selected path and label;
- a `Choose` handler that dismisses with `Path | None`, and a cancel/escape handler that dismisses with `None`.

Use Textual's documented `DirectorySelected(node, path)` event; do not treat a file selection as a directory choice.

**Step 4: Run the focused test to verify the pass**

Run: `python -m unittest test_yt_sync.DirectoryPickerTests -v`

Expected: all picker-root tests pass. The mounted picker behavior will be exercised in Task 5's Textual integration tests and manual smoke matrix.

---

## Task 4: Build the home/setup screen

**Objective:** Add the first-run form that accepts the playlist URL and destination folder, validates it, and returns a setup result without fetching.

**Files:**
- Modify: `ytsync/yt_sync.py` immediately before `HelpScreen` and in the app CSS section at `427-531`
- Test: `ytsync/test_yt_sync.py` (add `HomeScreenTests`)

**Step 1: Write failing UI tests**

Using Textual's `run_test()` pilot, cover:

1. The URL and destination `Input` values are prefilled from a supplied config.
2. Pressing Start with an invalid URL keeps the screen active and updates `#home-error`.
3. Pressing Start with valid values returns `SetupValues` and creates a nested destination directory under a temporary path.
4. Pressing Browse opens `DirectoryPickerScreen`; choosing a directory updates the destination input.
5. Escape returns `None` and does not alter the supplied config dict.

Keep network calls out of these tests; use the existing app test style at `test_yt_sync.py:113-157` and patch the refresh boundary in the lifecycle test.

**Step 2: Run the focused test to verify failure**

Run: `python -m unittest test_yt_sync.HomeScreenTests -v`

Expected: FAIL because `HomeScreen` and its widget IDs do not exist.

**Step 3: Write the minimal implementation**

Add `HomeScreen(ModalScreen)` with this stable widget contract:

- `#home-card` — centered panel;
- `#home-title`, `#home-subtitle` — brand and explanation;
- `#playlist-url` — `Input` for the playlist URL;
- `#music-dir` — `Input` for the destination path;
- `#browse-folder`, `#start-sync`, `#cancel-home` — buttons;
- `#home-error` — inline error label, initially empty.

The screen should:

- accept the current config and an `initial` flag in its constructor;
- prefill both inputs from `cfg`;
- focus the URL input on mount;
- move focus from URL to folder on URL `Input.Submitted`, and submit from the folder input without requiring a mouse;
- open `DirectoryPickerScreen` from Browse and copy a returned path into `#music-dir`;
- call `validate_setup_values()` on Start;
- create the expanded destination with `mkdir(parents=True, exist_ok=True)` and show `Cannot create destination folder: ...` if that raises `OSError`;
- dismiss with `SetupValues` only after validation and directory creation succeed;
- dismiss with `None` for Cancel/Escape. The parent app decides whether that means quit or return to dashboard.

Keep all labels explicit (`Playlist URL`, `Download folder`, `Browse…`, `Start sync`, `Cancel`), use `type="button"` semantics where Textual exposes them, and do not bind `Enter` globally in a way that breaks text editing.

**Step 4: Run the focused test to verify the pass**

Run: `python -m unittest test_yt_sync.HomeScreenTests -v`

Expected: all home-screen tests pass, including the no-network property and temporary-folder creation.

---

## Task 5: Integrate startup, dashboard settings, and directory readiness

**Objective:** Make the home screen the interactive entry point while preserving the current dashboard and CLI flow.

**Files:**
- Modify: `ytsync/yt_sync.py:533-643, 795-803, 967-1044`
- Test: `ytsync/test_yt_sync.py` (add `StartupFlowTests` and extend the existing app test setup)

**Step 1: Write failing lifecycle tests**

Add tests that assert:

- an interactive app with `playlist_url == ""` mounts `HomeScreen` and does not call `fetch_playlist` before Start;
- submitting valid home values updates `app.cfg`, calls `save_config`, and calls `_do_refresh()` exactly once;
- an initial cancel exits the app, while canceling a later `h`-opened home screen leaves the current dashboard/config unchanged;
- `h` opens the home/settings screen when the app is idle and is rejected with the existing busy warning while a worker is active.

Patch `_do_refresh`, `fetch_playlist`, and `save_config`; do not hit YouTube in tests.

**Step 2: Run the focused test to verify failure**

Run: `python -m unittest test_yt_sync.StartupFlowTests -v`

Expected: FAIL because `on_mount()` still refreshes immediately and there is no Home/Setup binding.

**Step 3: Write the minimal lifecycle implementation**

In `YTSyncApp`:

1. Add a Home/Setup binding, preferably `Binding("h", "open_home", "Setup")`.
2. Keep the existing dashboard composed behind the modal, but change `on_mount()` so it initializes the table/log and calls `_open_home(initial=True)` instead of `_do_refresh()`.
3. Implement `_open_home(initial: bool)` to push `HomeScreen(self.cfg, initial=initial)` with a result callback and record whether the current modal is the required initial screen.
4. In the result callback:
   - on `None`, exit only when the initial home screen was canceled; otherwise do nothing;
   - on `SetupValues`, update `self.cfg["playlist_url"]` and `self.cfg["music_dir"]`, call `save_config(self.cfg)`, reset `mode` to `"all"` and clear selections, log the selected destination, then call `_do_refresh()`.
5. Implement `action_open_home()` with the existing `_busy` guard and open a non-initial home screen.
6. Keep all existing filter/action/selection methods unchanged unless a widget ID or CSS selector requires a mechanical update.

In `main()`:

- Determine whether one of `--diff-only`, `--download-only`, or `--auto` is present before enforcing `playlist_url`.
- Retain the current error for those CLI modes when no URL is configured.
- For the interactive/no-CLI path, instantiate `YTSyncApp(cfg)` even when the URL is empty.
- Run `cleanup_stale_artifacts()` in the CLI path as before; the selected interactive destination is created by `HomeScreen` before its first refresh.

Add a defensive `Path(music_dir).mkdir(parents=True, exist_ok=True)` before the yt-dlp subprocess in `download_track()` so a configured CLI destination also works when absent. Convert an `OSError` into a failed `DownloadResult` rather than crashing the worker.

**Step 4: Run the focused test to verify the pass**

Run: `python -m unittest test_yt_sync.StartupFlowTests -v`

Expected: all startup/settings tests pass, and existing selection/cursor tests remain green.

---

## Task 6: Refresh the visual system and motion

**Objective:** Replace the current flat/thick Tokyo Night presentation with a clear, crisp setup/dashboard hierarchy and useful short animations.

**Files:**
- Modify: `ytsync/yt_sync.py:341-531` and the `HomeScreen`/`DirectoryPickerScreen` CSS added in Tasks 3-4
- Test: `ytsync/test_yt_sync.py` may add a small `VisualContractTests` class for required selector strings; visual acceptance is manual

**Step 1: Write the visual contract checks**

If static checks are added, assert that `YTSyncApp.CSS` and the screen CSS contain the home-card, focus, hover, disabled, loader, and dashboard selectors. Do not assert exact color hex values in behavior tests.

**Step 2: Run the checks to verify failure**

Run: `python -m unittest test_yt_sync.VisualContractTests -v`

Expected: FAIL until the new selectors are present.

**Step 3: Implement the visual refresh**

Use a small semantic palette at the existing color-constant section, for example:

- deep background and panel surfaces for separation;
- one cool accent for focus/primary actions;
- high-contrast text and muted text that remain readable in a small terminal;
- green/yellow/red/orange reserved for sync status and destructive warnings.

Apply it consistently:

- `HomeScreen`: centered panel with `border: round`, restrained padding, readable title/subtitle, aligned labels, full-width inputs, a compact browse button, clear primary/cancel actions, and a dedicated error row.
- Inputs/buttons: visible `:focus`, `:hover`, and `:disabled` states; do not rely on color alone for the destructive mode.
- Dashboard: reduce heavy `tall` borders, use crisp separators/panel fills, make the status bar read as a compact summary, keep the table cursor and selected-row marker obvious, and keep the log visually subordinate.
- Help modal: reuse the same palette and spacing rather than a separate visual language.
- Do not use blur, glow, large shadows, or a new image-rendering dependency.

Add short Textual animator calls in `HomeScreen.on_mount()` (and optionally the directory picker) for a one-shot card entrance: initialize card opacity/offset, then animate opacity to `1.0` and offset to `(0, 0)` over roughly `0.2–0.25` seconds with an ease-out curve. Use the documented `styles.animate()` API. Keep the existing `LoadingIndicator` as operational feedback for network/file work; do not add a constantly moving decorative background. Ensure the animation does not change form focus order or delay keyboard input.

**Step 4: Run the focused/static checks to verify the pass**

Run: `python -m unittest test_yt_sync.VisualContractTests -v` (if added), then run the full suite in Task 7. Visual pass/fail is covered by the manual matrix below.

---

## Task 7: Update documentation and perform the completion gate

**Objective:** Document the new interactive flow and verify that all current behavior plus the new setup path is covered.

**Files:**
- Modify: `README.md:14-63`
- Modify: `ytsync/HANDOVER.md:35-90`

**Step 1: Update the documentation**

Document that:

- `yt-sync` opens a setup home screen before the first fetch;
- the playlist URL and download folder are prefilled from config when available;
- the folder can be typed or selected through Browse, and new folders are created;
- submitted values are saved to `~/.config/yt-sync/config.toml`;
- `h` reopens setup from the dashboard;
- CLI modes remain non-interactive and still require a configured URL;
- the visual pass uses a compact dark theme and short operational/surface motion.

Add `h` to both keybinding tables. Update the handover architecture and remove any statement that says the app always fetches immediately or has no setup flow. Keep the existing deferred-feature list honest.

**Step 2: Run syntax and the complete test suite**

From `ytsync/`, run:

```bash
python -m py_compile yt_sync.py test_yt_sync.py
python -m unittest test_yt_sync -v
```

Expected: compilation succeeds and every test passes. No real playlist or download is required for the suite.

**Step 3: Run repository hygiene checks**

From the repository root, run:

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors; the pre-existing staged changes remain present; the new plan/documentation/code changes are clearly visible and no secrets or generated test artifacts are newly added.

## Acceptance and manual smoke matrix

Run the interactive app in a PTY with a disposable config/home when possible. Do not use the real music directory for destructive smoke tests.

| Scenario | Expected result |
|---|---|
| Empty config, launch `yt-sync` | Home screen appears; no network request occurs before Start; Cancel/Quit exits cleanly. |
| Existing config | URL and folder fields are prefilled; Start reaches the dashboard and begins the normal refresh. |
| Blank or malformed URL | Inline error appears; home screen remains active; config and filesystem are unchanged. |
| Typed nested destination that does not exist | Start creates the directory, saves the expanded path, then refreshes against it. |
| Browse flow | Directory picker opens from Browse; selecting a directory and choosing it updates the editable folder input. |
| Dashboard `h` | Setup screen reopens; Cancel returns to the same dashboard without changing settings. |
| Busy refresh/download followed by `h` | Existing busy guard prevents conflicting setup changes. |
| Existing filters/selection/cursor tests | Behavior remains unchanged, including non-empty selection never falling back to all rows. |
| Terminal sizes around 80x24 and 120x36 | Home controls, error row, table, log, footer, and modal borders do not clip or become unreachable. |
| Home/picker enter and escape keys | Keyboard-only flow works; focus remains in the active input/control; no accidental global action fires while editing. |
| Animation pass | Setup card enters once with subtle opacity/offset motion; loading animation communicates work; no decorative infinite motion or glow is introduced. |

## Execution notes for the implementer

1. Complete tasks in order. The lifecycle task depends on the setup and picker result contracts; visual work must not alter the download/diff state machine.
2. Use the repository's existing `unittest` style. If Textual is unavailable, stop at the documented environment preflight instead of weakening imports or faking test output.
3. Do not run a real YouTube download as a test. Patch `fetch_playlist`, `download_track`, `save_config`, and filesystem paths in unit tests.
4. Preserve the current `RowEntry` mapping and selection rules. Do not reconstruct row identity from integer offsets while touching the dashboard.
5. Keep config writes explicit and limited to the known `[yt-sync]` settings. Never touch `failed.json` or credential files.
6. Do not commit or push as part of this plan without explicit user authorization. If commits are later requested, commit after each verified logical phase and do not include the pre-existing generated/staged artifacts accidentally.

## Completion gate

The change is complete only when the full suite passes, the interactive smoke matrix passes at both terminal sizes, `git diff --check` is clean, and the final status distinguishes pre-existing staged work from the new implementation. This plan itself does not implement the feature or create a commit.
