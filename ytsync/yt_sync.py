#!/usr/bin/env python3
"""yt-sync — YouTube Playlist ↔ Local Music Sync TUI."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from difflib import SequenceMatcher

# ── config ──────────────────────────────────────────────────────────
CONFIG_PATH = Path.home() / ".config" / "yt-sync" / "config.toml"
DEFAULTS = {
    "playlist_url": "",
    "music_dir": str(Path.home() / "Music"),
    "audio_format": "opus",
    "trash_dir": str(Path.home() / ".local" / "share" / "yt-sync" / "trash"),
    "real_delete": False,
    "theme": "textual-dark",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            return {**DEFAULTS, **tomllib.load(f).get("yt-sync", {})}
    return dict(DEFAULTS)


# ── data ────────────────────────────────────────────────────────────
@dataclass
class Track:
    id: str
    title: str
    url: str


@dataclass
class LocalFile:
    path: Path
    stem: str


# ── normalize ───────────────────────────────────────────────────────
def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ── yt-dlp wrappers ─────────────────────────────────────────────────
def fetch_playlist(url: str) -> list[Track]:
    proc = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", url],
        capture_output=True, text=True, timeout=120,
    )
    tracks = []
    for line in proc.stdout.strip().splitlines():
        if not line:
            continue
        d = json.loads(line)
        tracks.append(Track(id=d["id"], title=d.get("title", d["id"]), url=d["url"]))
    return tracks


@dataclass
class DownloadResult:
    success: bool
    error: str = ""


def download_track(track: Track, music_dir: str, fmt: str) -> DownloadResult:
    proc = subprocess.run(
        [
            "yt-dlp", "-x", "--audio-format", fmt,
            "--no-overwrites",
            "-o", f"{music_dir}/%(title)s.%(ext)s",
            "--no-playlist", track.url,
        ],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode == 0:
        return DownloadResult(success=True)
    # Extract meaningful error from stderr
    err = ""
    for line in proc.stderr.strip().splitlines():
        if line.startswith("ERROR:"):
            err = line.split("ERROR:", 1)[1].strip()
            break
    if not err:
        err = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "Unknown error"
    return DownloadResult(success=False, error=err)


# ── scan ────────────────────────────────────────────────────────────
def scan_local(music_dir: str) -> list[LocalFile]:
    exts = {".opus", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".wav", ".webm"}
    p = Path(music_dir)
    if not p.exists():
        return []
    return [
        LocalFile(path=f, stem=f.stem)
        for f in p.iterdir()
        if f.is_file() and f.suffix.lower() in exts
    ]


# ── match ───────────────────────────────────────────────────────────
@dataclass
class DiffResult:
    matched: list[tuple[Track, LocalFile]]
    missing: list[Track]
    orphans: list[LocalFile]


def diff(playlist: list[Track], local: list[LocalFile]) -> DiffResult:
    norm_local = {normalize(f.stem): f for f in local}
    title_index: dict[str, LocalFile] = {}
    for f in local:
        if " - " in f.stem:
            parts = f.stem.rsplit(" - ", 1)
            title_index[normalize(parts[1])] = f

    matched: list[tuple[Track, LocalFile]] = []
    missing: list[Track] = []
    used = set()

    for t in playlist:
        nt = normalize(t.title)
        if nt in norm_local:
            matched.append((t, norm_local[nt]))
            used.add(norm_local[nt].path)
        elif nt in title_index and title_index[nt].path not in used:
            matched.append((t, title_index[nt]))
            used.add(title_index[nt].path)
        else:
            best = None
            best_ratio = 0.0
            for f in local:
                if f.path in used:
                    continue
                ratio = SequenceMatcher(None, nt, normalize(f.stem)).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = f
            if best and best_ratio > 0.85:
                matched.append((t, best))
                used.add(best.path)
            else:
                missing.append(t)

    orphans = [f for f in local if f.path not in used]
    return DiffResult(matched=matched, missing=missing, orphans=orphans)


# ── actions ─────────────────────────────────────────────────────────
def trash_file(local: LocalFile, trash_dir: str) -> None:
    dest = Path(trash_dir)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(local.path), str(dest / local.path.name))


def delete_file(local: LocalFile) -> None:
    local.path.unlink()


# ── TUI ─────────────────────────────────────────────────────────────
from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import (  # noqa: E402
    DataTable, Footer, Header, Label, LoadingIndicator, RichLog, Static,
)
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual import events, on  # noqa: E402

# ── Colors ──────────────────────────────────────────────────────────
# Tokyo Night inspired palette
C_BG         = "#1a1b26"
C_BG_DARK    = "#16161e"
C_BG_HL      = "#292e42"
C_FG         = "#c0caf5"
C_FG_DIM     = "#565f89"
C_BLUE       = "#7aa2f7"
C_CYAN       = "#7dcfff"
C_GREEN      = "#9ece6a"
C_YELLOW     = "#e0af68"
C_RED        = "#f7768e"
C_MAGENTA    = "#bb9af7"
C_ORANGE     = "#ff9e64"
C_TEAL       = "#73daca"


class HelpScreen(ModalScreen):
    """Help overlay showing keybindings."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
    ]

    DEFAULT_CSS = f"""
    HelpScreen {{
        align: center middle;
    }}
    HelpScreen #help-panel {{
        width: 64;
        max-height: 80%;
        background: {C_BG_DARK};
        border: thick {C_BLUE};
        padding: 1 2;
    }}
    HelpScreen #help-title {{
        text-align: center;
        text-style: bold;
        color: {C_BLUE};
        padding-bottom: 1;
    }}
    HelpScreen .help-section {{
        color: {C_MAGENTA};
        text-style: bold;
        padding-top: 1;
    }}
    HelpScreen .help-row {{
        color: {C_FG};
    }}
    HelpScreen .help-key {{
        color: {C_CYAN};
        text-style: bold;
    }}
    HelpScreen #help-footer {{
        text-align: center;
        color: {C_FG_DIM};
        padding-top: 1;
    }}
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-panel"):
            yield Static("♫  yt-sync keybindings", id="help-title")
            yield Static("── Navigation ──", classes="help-section")
            yield Static("  [bold cyan]j[/] / [bold cyan]k[/]          cursor down / up", classes="help-row")
            yield Static("  [bold cyan]g[/] / [bold cyan]G[/]          top / bottom of list", classes="help-row")
            yield Static("── Selection ──", classes="help-section")
            yield Static("  [bold cyan]space[/]        toggle row selection", classes="help-row")
            yield Static("  [bold cyan]escape[/]       clear all selections", classes="help-row")
            yield Static("── Filters ──", classes="help-section")
            yield Static("  [bold cyan]1[/]            show all tracks", classes="help-row")
            yield Static("  [bold cyan]2[/]            show missing only", classes="help-row")
            yield Static("  [bold cyan]3[/]            show orphans only", classes="help-row")
            yield Static("── Actions ──", classes="help-section")
            yield Static("  [bold cyan]enter[/]        download / trash current row", classes="help-row")
            yield Static("  [bold cyan]d[/]            batch download (selected / all missing)", classes="help-row")
            yield Static("  [bold cyan]t[/]            batch trash (selected / all orphans)", classes="help-row")
            yield Static("  [bold cyan]x[/]            toggle delete mode (trash ↔ real)", classes="help-row")
            yield Static("  [bold cyan]r[/]            refresh (re-fetch playlist)", classes="help-row")
            yield Static("  [bold cyan]q[/]            quit", classes="help-row")
            yield Static("  [bold cyan]?[/]            this help screen", classes="help-row")
            yield Static("press [bold]esc[/] or [bold]?[/] to close", id="help-footer")


class YTSyncApp(App):
    CSS = f"""
    /* ── Screen ─────────────────────────────────────────────── */
    Screen {{
        layout: vertical;
        background: {C_BG};
    }}

    /* ── Header ─────────────────────────────────────────────── */
    Header {{
        background: {C_BG_DARK};
        color: {C_BLUE};
        dock: top;
        height: 1;
    }}

    /* ── Status bar ─────────────────────────────────────────── */
    #status-bar {{
        height: 1;
        dock: top;
        background: {C_BG_HL};
        color: {C_FG};
        padding: 0 1;
    }}
    #status-bar.busy {{
        color: {C_YELLOW};
    }}

    /* ── Loading indicator ──────────────────────────────────── */
    #loader {{
        height: 1;
        dock: top;
        display: none;
        background: {C_BG_DARK};
        color: {C_BLUE};
    }}
    #loader.visible {{
        display: block;
    }}

    /* ── DataTable ──────────────────────────────────────────── */
    DataTable {{
        height: 1fr;
        background: {C_BG};
        color: {C_FG};
        scrollbar-color: {C_FG_DIM};
        scrollbar-color-hover: {C_BLUE};
        scrollbar-color-active: {C_CYAN};
    }}
    DataTable > .datatable--header {{
        background: {C_BG_DARK};
        color: {C_BLUE};
        text-style: bold;
    }}
    DataTable > .datatable--cursor {{
        background: {C_BG_HL};
        color: {C_FG};
    }}
    DataTable > .datatable--even-row {{
        background: {C_BG};
    }}
    DataTable > .datatable--odd-row {{
        background: {C_BG_DARK};
    }}
    DataTable:focus {{
        border: tall {C_BLUE};
    }}

    /* ── Console ────────────────────────────────────────────── */
    #console {{
        height: 10;
        background: {C_BG_DARK};
        color: {C_FG_DIM};
        border: tall {C_FG_DIM}40;
        padding: 0 1;
        scrollbar-color: {C_FG_DIM};
        scrollbar-color-hover: {C_BLUE};
    }}
    #console:focus {{
        border: tall {C_BLUE};
    }}
    #console.op-download {{
        border: tall {C_GREEN};
    }}
    #console.op-trash {{
        border: tall {C_ORANGE};
    }}
    #console.op-refresh {{
        border: tall {C_CYAN};
    }}

    /* ── Footer ─────────────────────────────────────────────── */
    Footer {{
        background: {C_BG_DARK};
        color: {C_FG_DIM};
    }}
    Footer > .footer--key {{
        background: {C_BG_HL};
        color: {C_BLUE};
        text-style: bold;
    }}
    Footer > .footer--description {{
        color: {C_FG_DIM};
    }}
    """

    TITLE = "♫ yt-sync"
    SUB_TITLE = "YouTube Playlist ↔ Local Music Sync"

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
        Binding("space", "toggle_select", "Select", show=False),
        Binding("escape", "clear_select", "Clear", show=False),
        Binding("1", "filter_all", "All"),
        Binding("2", "filter_missing", "Missing"),
        Binding("3", "filter_orphans", "Orphans"),
        Binding("enter", "act_on_current", "Act", show=True),
        Binding("d", "download", "Download"),
        Binding("t", "trash", "Trash"),
        Binding("x", "toggle_delete", "Del-mode"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "show_help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.diff_result = DiffResult(matched=[], missing=[], orphans=[])
        self.mode = "all"
        self.delete_mode = cfg.get("real_delete", False)
        self.selected: set[str] = set()  # row keys (str indices)
        self._busy = False
        self._row_map: dict[str, tuple[str, str, str]] = {}  # key -> (title, status, row_type)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("", id="status-bar")
        yield LoadingIndicator(id="loader")
        yield DataTable(id="table", zebra_stripes=True)
        yield RichLog(id="console", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        table.add_columns("", "Title", "Status")
        table.focus()
        self._log(f"[bold {C_BLUE}]♫ yt-sync ready[/]")
        del_status = f"[bold {C_RED}]real delete[/]" if self.delete_mode else f"[bold {C_GREEN}]trash[/]"
        self._log(f"  delete mode: {del_status}")
        self._log(f"  press [bold {C_CYAN}]?[/] for keybindings")
        self._do_refresh()

    def _log(self, msg: str) -> None:
        self.query_one("#console", RichLog).write(msg)

    def _set_loading(self, on: bool, op: str = "") -> None:
        """Show/hide loading indicator and set console border style."""
        loader = self.query_one("#loader", LoadingIndicator)
        console = self.query_one("#console", RichLog)
        if on:
            loader.add_class("visible")
            console.remove_class("op-download", "op-trash", "op-refresh")
            if op:
                console.add_class(f"op-{op}")
            self.query_one("#status-bar", Label).add_class("busy")
        else:
            loader.remove_class("visible")
            console.remove_class("op-download", "op-trash", "op-refresh")
            self.query_one("#status-bar", Label).remove_class("busy")

    # ── non-blocking data load ──────────────────────────────────
    def _do_refresh(self) -> None:
        """Kick off a non-blocking data load."""
        if self._busy:
            self._log(f"[{C_YELLOW}]Already working…[/]")
            return
        self._busy = True
        self._set_loading(True, "refresh")
        self._log(f"[dim]Fetching playlist…[/]")
        self.run_worker(self._async_load_data(), exclusive=True)

    async def _async_load_data(self) -> None:
        """Fetch playlist and scan local files off the main thread."""
        try:
            playlist = await asyncio.to_thread(
                fetch_playlist, self.cfg["playlist_url"]
            )
            self._log(f"  playlist: [bold]{len(playlist)}[/] tracks")
            local = await asyncio.to_thread(
                scan_local, self.cfg["music_dir"]
            )
            self._log(f"  local:    [bold]{len(local)}[/] files")
            self.diff_result = diff(playlist, local)
            d = self.diff_result
            self._log(
                f"  result:   [{C_GREEN}]{len(d.matched)} matched[/]  "
                f"[{C_YELLOW}]{len(d.missing)} missing[/]  "
                f"[{C_RED}]{len(d.orphans)} orphans[/]"
            )
            self.selected.clear()
            self._populate_table()
        except Exception as e:
            self._log(f"[bold {C_RED}]Error: {e}[/]")
        finally:
            self._busy = False
            self._set_loading(False)

    def _populate_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        self._row_map.clear()
        d = self.diff_result

        key = 0

        if self.mode in ("all",):
            for t, _f in d.matched:
                key += 1
                k = str(key)
                self._row_map[k] = (t.title, "✔ synced", "matched")
        if self.mode in ("all", "missing"):
            for t in d.missing:
                key += 1
                k = str(key)
                self._row_map[k] = (t.title, "⬇ missing", "missing")
        if self.mode in ("all", "orphans"):
            for f in d.orphans:
                key += 1
                k = str(key)
                self._row_map[k] = (f.stem, "✗ orphan", "orphan")

        for k, (title, status, rtype) in self._row_map.items():
            sel_marker = f"[bold {C_MAGENTA}]▶[/] " if k in self.selected else "  "

            # Color the status text
            if rtype == "matched":
                styled_status = f"[{C_GREEN}]{status}[/]"
            elif rtype == "missing":
                styled_status = f"[{C_YELLOW}]{status}[/]"
            else:
                styled_status = f"[{C_RED}]{status}[/]"

            table.add_row(sel_marker + k, title, styled_status, key=k)

        self._update_status_bar()

    def _update_status_bar(self) -> None:
        d = self.diff_result
        del_icon = f"[bold {C_RED}]⊘ DEL[/]" if self.delete_mode else f"[bold {C_GREEN}]♻ TRASH[/]"
        sel_info = f"  [{C_MAGENTA}]▶ {len(self.selected)} selected[/]" if self.selected else ""

        filter_labels = {
            "all": f"[bold {C_BLUE}]ALL[/]",
            "missing": f"[bold {C_YELLOW}]MISSING[/]",
            "orphans": f"[bold {C_RED}]ORPHANS[/]",
        }
        filter_label = filter_labels.get(self.mode, "ALL")

        self.query_one("#status-bar", Label).update(
            f" {filter_label}"
            f"  [{C_GREEN}]✔ {len(d.matched)}[/]"
            f"  [{C_YELLOW}]⬇ {len(d.missing)}[/]"
            f"  [{C_RED}]✗ {len(d.orphans)}[/]"
            f"{sel_info}"
            f"  {del_icon}"
        )

    def _key_type(self, key: str) -> str:
        """Map a row key back to its row type."""
        if key in self._row_map:
            return self._row_map[key][2]
        return "matched"

    def _selected_of_type(self, row_type: str) -> set[str]:
        """Return selected row keys that belong to the given row_type."""
        return {k for k in self.selected if self._key_type(k) == row_type}

    # ── vim navigation (bypass DataTable text input) ─────────────
    def on_key(self, event: events.Key) -> None:
        table = self.query_one("#table", DataTable)
        if event.key == "j":
            table.action_cursor_down()
            event.prevent_default()
        elif event.key == "k":
            table.action_cursor_up()
            event.prevent_default()

    # ── selection ────────────────────────────────────────────────
    def action_toggle_select(self) -> None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0 or table.cursor_row >= len(table.ordered_rows):
            return
        row_key = table.ordered_rows[table.cursor_row].key
        if row_key is None or row_key.value is None:
            return
        k = str(row_key.value)
        if k in self.selected:
            self.selected.discard(k)
        else:
            self.selected.add(k)
        self._populate_table()

    def action_clear_select(self) -> None:
        self.selected.clear()
        self._populate_table()

    # ── filters ──────────────────────────────────────────────────
    def action_filter_all(self) -> None:
        self.mode = "all"
        self.selected.clear()
        self._populate_table()
        self.notify("Filter: All tracks", severity="information", timeout=2)

    def action_filter_missing(self) -> None:
        self.mode = "missing"
        self.selected.clear()
        self._populate_table()
        self.notify("Filter: Missing only", severity="information", timeout=2)

    def action_filter_orphans(self) -> None:
        self.mode = "orphans"
        self.selected.clear()
        self._populate_table()
        self.notify("Filter: Orphans only", severity="information", timeout=2)

    def action_refresh(self) -> None:
        self._do_refresh()

    def action_quit(self) -> None:
        self.exit()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle_delete(self) -> None:
        self.delete_mode = not self.delete_mode
        self._update_status_bar()
        if self.delete_mode:
            self.notify("⊘ Delete mode: REAL DELETE", severity="warning", timeout=3)
            self._log(f"[bold {C_RED}]Delete mode: ON (real delete)[/]")
        else:
            self.notify("♻ Delete mode: Trash", severity="information", timeout=3)
            self._log(f"[bold {C_GREEN}]Delete mode: OFF (trash)[/]")

    # ── single-item action (enter key) ───────────────────────────
    def action_act_on_current(self) -> None:
        """Download or trash the track under the cursor."""
        if self._busy:
            self.notify("⏳ Already working…", severity="warning", timeout=2)
            return
        table = self.query_one("#table", DataTable)
        if table.row_count == 0 or table.cursor_row >= len(table.ordered_rows):
            return
        row_key = table.ordered_rows[table.cursor_row].key
        if row_key is None or row_key.value is None:
            return
        k = str(row_key.value)
        rtype = self._key_type(k)

        if rtype == "matched":
            self.notify("✔ Track is already synced", severity="information", timeout=2)
            return
        elif rtype == "missing":
            track = self._track_for_key(k)
            if track:
                self._busy = True
                self._set_loading(True, "download")
                self.notify(f"⬇ Downloading: {track.title}", severity="information", timeout=3)
                self.run_worker(self._do_download_single(track), exclusive=True)
        elif rtype == "orphan":
            lf = self._localfile_for_key(k)
            if lf:
                self._busy = True
                self._set_loading(True, "trash")
                verb = "Deleting" if self.delete_mode else "Trashing"
                self.notify(f"{'⊘' if self.delete_mode else '♻'} {verb}: {lf.stem}", severity="information", timeout=3)
                self.run_worker(self._do_trash_single(lf), exclusive=True)

    def _track_for_key(self, key: str) -> Track | None:
        """Resolve a row key to the corresponding missing Track."""
        d = self.diff_result
        # Calculate the index into d.missing
        matched_offset = len(d.matched) if self.mode in ("all",) else 0
        idx = int(key) - 1 - matched_offset
        if 0 <= idx < len(d.missing):
            return d.missing[idx]
        return None

    def _localfile_for_key(self, key: str) -> LocalFile | None:
        """Resolve a row key to the corresponding orphan LocalFile."""
        d = self.diff_result
        matched_offset = len(d.matched) if self.mode in ("all",) else 0
        missing_offset = len(d.missing) if self.mode in ("all", "missing") else 0
        idx = int(key) - 1 - matched_offset - missing_offset
        if 0 <= idx < len(d.orphans):
            return d.orphans[idx]
        return None

    async def _do_download_single(self, track: Track) -> None:
        """Download a single track."""
        self._log(f"  [{C_CYAN}]⬇[/] {track.title}")
        result = await asyncio.to_thread(
            download_track, track, self.cfg["music_dir"], self.cfg["audio_format"]
        )
        if result.success:
            self._log(f"    [{C_GREEN}]✔ downloaded[/]")
            self.notify(f"✔ Downloaded: {track.title}", severity="information", timeout=3)
        else:
            self._log(f"    [{C_RED}]✗ {result.error}[/]")
            self.notify(f"✗ Failed: {result.error}", severity="error", timeout=5)
        self._busy = False
        self._set_loading(False)
        await self._async_load_data()

    async def _do_trash_single(self, lf: LocalFile) -> None:
        """Trash or delete a single orphan."""
        verb = "Deleting" if self.delete_mode else "Trashing"
        self._log(f"  [{C_ORANGE}]{'⊘' if self.delete_mode else '♻'}[/] {verb} {lf.stem}")
        if self.delete_mode:
            await asyncio.to_thread(delete_file, lf)
        else:
            await asyncio.to_thread(trash_file, lf, self.cfg["trash_dir"])
        self._log(f"    [{C_GREEN}]✔ done[/]")
        self.notify(f"✔ {verb[:-3]}ed: {lf.stem}", severity="information", timeout=3)
        self._busy = False
        self._set_loading(False)
        await self._async_load_data()

    # ── batch download (async, non-blocking) ─────────────────────
    def action_download(self) -> None:
        if self._busy:
            self.notify("⏳ Already working…", severity="warning", timeout=2)
            return
        d = self.diff_result
        # if selection exists, only download selected missing
        sel = self._selected_of_type("missing")
        targets = [t for i, t in enumerate(d.missing) if not sel or str(self._missing_key(i)) in sel]
        if not targets:
            self.notify("Nothing to download", severity="warning", timeout=2)
            self._log(f"[{C_YELLOW}]Nothing to download.[/]")
            return
        self._busy = True
        self._set_loading(True, "download")
        self.notify(f"⬇ Downloading {len(targets)} tracks…", severity="information", timeout=3)
        self.run_worker(self._do_download(targets), exclusive=True)

    def _missing_key(self, idx: int) -> str:
        """Row key for the idx-th missing track."""
        base = len(self.diff_result.matched) if self.mode in ("all",) else 0
        return str(base + idx + 1)

    async def _do_download(self, targets: list[Track]) -> None:
        n = len(targets)
        ok_count = 0
        fail_count = 0
        self._log(f"[bold {C_BLUE}]Downloading {n} tracks…[/]")
        for i, t in enumerate(targets, 1):
            self._log(f"  [{C_FG_DIM}][{i}/{n}][/] {t.title}")
            result = await asyncio.to_thread(
                download_track, t, self.cfg["music_dir"], self.cfg["audio_format"]
            )
            if result.success:
                self._log(f"    [{C_GREEN}]✔ downloaded[/]")
                ok_count += 1
            else:
                self._log(f"    [{C_RED}]✗ {result.error}[/]")
                fail_count += 1
        # Summary
        summary = f"[bold {C_GREEN}]Downloads complete:[/] {ok_count} ok"
        if fail_count:
            summary += f", [{C_RED}]{fail_count} failed[/]"
        self._log(summary)
        severity = "information" if fail_count == 0 else "warning"
        self.notify(f"Done: {ok_count} downloaded, {fail_count} failed", severity=severity, timeout=5)
        self._busy = False
        self._set_loading(False)
        await self._async_load_data()

    # ── batch trash (async) ──────────────────────────────────────
    def action_trash(self) -> None:
        if self._busy:
            self.notify("⏳ Already working…", severity="warning", timeout=2)
            return
        d = self.diff_result
        sel = self._selected_of_type("orphan")
        targets = [f for i, f in enumerate(d.orphans) if not sel or str(self._orphan_key(i)) in sel]
        if not targets:
            self.notify("No orphans to act on", severity="warning", timeout=2)
            self._log(f"[{C_YELLOW}]No orphans to act on.[/]")
            return
        self._busy = True
        verb = "Deleting" if self.delete_mode else "Trashing"
        self._set_loading(True, "trash")
        self.notify(f"{'⊘' if self.delete_mode else '♻'} {verb} {len(targets)} orphans…", severity="information", timeout=3)
        self.run_worker(self._do_trash(targets), exclusive=True)

    def _orphan_key(self, idx: int) -> str:
        base = len(self.diff_result.matched) if self.mode in ("all",) else 0
        base += len(self.diff_result.missing)
        return str(base + idx + 1)

    async def _do_trash(self, targets: list[LocalFile]) -> None:
        n = len(targets)
        verb = "Deleting" if self.delete_mode else "Trashing"
        self._log(f"[bold {C_ORANGE}]{verb} {n} orphans…[/]")
        for i, f in enumerate(targets, 1):
            self._log(f"  [{C_FG_DIM}][{i}/{n}][/] {f.stem}")
            if self.delete_mode:
                await asyncio.to_thread(delete_file, f)
            else:
                await asyncio.to_thread(trash_file, f, self.cfg["trash_dir"])
            self._log(f"    [{C_GREEN}]✔ done[/]")
        self._log(f"[bold {C_GREEN}]{verb} complete.[/]")
        self.notify(f"✔ {verb} {n} orphans complete", severity="information", timeout=3)
        self._busy = False
        self._set_loading(False)
        await self._async_load_data()


# ── CLI ─────────────────────────────────────────────────────────────
def main() -> None:
    cfg = load_config()

    if not cfg["playlist_url"]:
        print("Error: playlist_url not set in", CONFIG_PATH)
        sys.exit(1)

    if "--diff-only" in sys.argv:
        playlist = fetch_playlist(cfg["playlist_url"])
        local = scan_local(cfg["music_dir"])
        d = diff(playlist, local)
        if d.missing:
            print(f"{len(d.missing)} MISSING:")
            for i, t in enumerate(d.missing, 1):
                print(f"  {i:3d}. {t.title}")
        if d.orphans:
            print(f"\n{len(d.orphans)} ORPHANS (local-only):")
            for i, f in enumerate(d.orphans, 1):
                print(f"  {i:3d}. {f.stem}")
        if not d.missing and not d.orphans:
            print(f"All {len(d.matched)} tracks synced. Nothing to do.")
        return

    if "--download-only" in sys.argv:
        playlist = fetch_playlist(cfg["playlist_url"])
        local = scan_local(cfg["music_dir"])
        d = diff(playlist, local)
        if not d.missing:
            print("Nothing to download.")
            return
        print(f"Downloading {len(d.missing)} tracks...")
        for i, t in enumerate(d.missing, 1):
            print(f"  [{i}/{len(d.missing)}] {t.title}")
            result = download_track(t, cfg["music_dir"], cfg["audio_format"])
            if not result.success:
                print(f"    FAILED: {result.error}")
        print("Done.")
        return

    if "--auto" in sys.argv:
        playlist = fetch_playlist(cfg["playlist_url"])
        local = scan_local(cfg["music_dir"])
        d = diff(playlist, local)
        if d.missing:
            print(f"Downloading {len(d.missing)} missing...")
            for i, t in enumerate(d.missing, 1):
                print(f"  [{i}/{len(d.missing)}] {t.title}")
                result = download_track(t, cfg["music_dir"], cfg["audio_format"])
                if not result.success:
                    print(f"    FAILED: {result.error}")
        if d.orphans:
            print(f"Trashing {len(d.orphans)} orphans...")
            for i, f in enumerate(d.orphans, 1):
                print(f"  [{i}/{len(d.orphans)}] {f.stem}")
                if cfg["real_delete"]:
                    delete_file(f)
                else:
                    trash_file(f, cfg["trash_dir"])
        if not d.missing and not d.orphans:
            print("Already synced.")
        else:
            print("Done.")
        return

    app = YTSyncApp(cfg)
    app.run()


if __name__ == "__main__":
    main()
