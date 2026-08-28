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
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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
    "saved_configs": [],
}

AUDIO_EXTS = {".opus", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".wav", ".webm"}
STATE_DIR = Path.home() / ".local" / "share" / "yt-sync"
FAILED_PATH = STATE_DIR / "failed.json"


def _saved_config_entry(value: object) -> dict[str, str] | None:
    """Return a persisted named configuration, ignoring malformed entries."""
    if not isinstance(value, dict):
        return None
    playlist_url = value.get("playlist_url")
    music_dir = value.get("music_dir")
    if not isinstance(playlist_url, str) or not playlist_url.strip():
        return None
    if not isinstance(music_dir, str) or not music_dir.strip():
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        name = Path(music_dir).expanduser().name or "Saved configuration"
    return {"name": name.strip(), "playlist_url": playlist_url, "music_dir": music_dir}


def saved_configurations(cfg: dict) -> list[dict[str, str]]:
    """Normalize saved configurations and retain the active legacy pair."""
    normalized: list[dict[str, str]] = []
    raw_configs = cfg.get("saved_configs", [])
    if isinstance(raw_configs, list):
        for raw_config in raw_configs:
            saved = _saved_config_entry(raw_config)
            if saved is not None and not any(
                saved["playlist_url"] == existing["playlist_url"]
                and saved["music_dir"] == existing["music_dir"]
                for existing in normalized
            ):
                normalized.append(saved)

    active = _saved_config_entry(
        {
            "playlist_url": cfg.get("playlist_url", ""),
            "music_dir": cfg.get("music_dir", DEFAULTS["music_dir"]),
        }
    )
    if active is not None and not any(
        active["playlist_url"] == existing["playlist_url"]
        and active["music_dir"] == existing["music_dir"]
        for existing in normalized
    ):
        normalized.append(active)
    return normalized


# ── failed-track state ─────────────────────────────────────────────
def load_failed() -> dict[str, dict]:
    """Previously-failed downloads, keyed by track id."""
    if FAILED_PATH.exists():
        try:
            return json.loads(FAILED_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_failed(failed: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_PATH.write_text(json.dumps(failed, indent=2, ensure_ascii=False))


def prune_failed(failed: dict[str, dict], valid_ids: set[str]) -> int:
    """Drop entries for tracks no longer in the playlist; return count removed."""
    stale = [tid for tid in failed if tid not in valid_ids]
    for tid in stale:
        del failed[tid]
    return len(stale)


def load_config() -> dict:
    cfg = {**DEFAULTS, "saved_configs": []}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            section = tomllib.load(f).get("yt-sync", {})
        if isinstance(section, dict):
            cfg.update({key: value for key, value in section.items() if key != "saved_configs"})
            cfg["saved_configs"] = section.get("saved_configs", [])
    cfg["saved_configs"] = saved_configurations(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    """Persist settings plus named saved configurations atomically."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg["saved_configs"] = saved_configurations(cfg)

    def quote(value: object) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    lines = [
        "[yt-sync]",
        f"playlist_url = {quote(cfg.get('playlist_url', ''))}",
        f"music_dir = {quote(cfg.get('music_dir', DEFAULTS['music_dir']))}",
        f"audio_format = {quote(cfg.get('audio_format', DEFAULTS['audio_format']))}",
        f"trash_dir = {quote(cfg.get('trash_dir', DEFAULTS['trash_dir']))}",
        f"real_delete = {'true' if cfg.get('real_delete', False) else 'false'}",
        f"theme = {quote(cfg.get('theme', DEFAULTS['theme']))}",
    ]
    for saved in cfg["saved_configs"]:
        lines.extend(
            [
                "",
                "[[yt-sync.saved_configs]]",
                f"name = {quote(saved['name'])}",
                f"playlist_url = {quote(saved['playlist_url'])}",
                f"music_dir = {quote(saved['music_dir'])}",
            ]
        )
    content = "\n".join(lines) + "\n"
    temporary_path = CONFIG_PATH.with_name(f".{CONFIG_PATH.name}.tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(CONFIG_PATH)


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


@dataclass
class RowEntry:
    """One displayed table row: identity lives here, not in reconstructed indexes."""
    title: str
    status: str
    row_type: str  # matched | missing | failed | orphan
    track: Track | None = None
    local: LocalFile | None = None


@dataclass(frozen=True)
class SetupValues:
    name: str
    playlist_url: str
    music_dir: str


def validate_setup_values(
    name: str, url: str, music_dir: str
) -> tuple[SetupValues | None, str]:
    """Validate and normalize values submitted by the setup screen."""
    name = name.strip()
    url = url.strip()
    music_dir = music_dir.strip()
    if not name:
        return None, "Enter a configuration name."
    if not url:
        return None, "Enter a playlist URL."
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "Enter a valid HTTP(S) playlist URL."
    if not music_dir:
        return None, "Enter a destination folder."
    return SetupValues(name, url, str(Path(music_dir).expanduser())), ""


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
    try:
        Path(music_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DownloadResult(success=False, error=f"Cannot create music directory: {exc}")
    proc = subprocess.run(
        [
            "yt-dlp", "-x", "-f", "bestaudio/best", "--audio-format", fmt,
            "--embed-metadata", "--embed-thumbnail",
            "--no-overwrites",
            "-o", f"{music_dir}/%(artist)s - %(track,title)s.%(ext)s",
            "--no-playlist", track.url,
        ],
        capture_output=True, text=True, timeout=600,
    )
    failed = load_failed()
    if proc.returncode == 0:
        # Clear any previous failure; the track is on disk (or already was).
        if track.id in failed:
            del failed[track.id]
            save_failed(failed)
        return DownloadResult(success=True)
    # Extract meaningful error from stderr
    err = ""
    for line in proc.stderr.strip().splitlines():
        if line.startswith("ERROR:"):
            err = line.split("ERROR:", 1)[1].strip()
            break
    if not err:
        err = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "Unknown error"
    # Record the failure so the track stops being re-attempted as "missing",
    # and clean up yt-dlp leftovers (.temp.* files, stray thumbnails).
    failed[track.id] = {
        "title": track.title,
        "url": track.url,
        "error": err,
        "failed_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_failed(failed)
    cleanup_track_artifacts(track, music_dir)
    return DownloadResult(success=False, error=err)


def cleanup_track_artifacts(track: Track, music_dir: str) -> None:
    """Remove yt-dlp garbage left by a failed download for this track."""
    nt = normalize(track.title)
    if not nt:
        return
    p = Path(music_dir)
    for f in p.iterdir():
        if not f.is_file():
            continue
        name = f.name
        if ".temp." in name and nt in normalize(name):
            try:
                f.unlink()
            except OSError:
                pass
        elif f.suffix.lower() in {".webp", ".png", ".jpg", ".jpeg"} and nt in normalize(name):
            try:
                f.unlink()
            except OSError:
                pass


# ── scan ────────────────────────────────────────────────────────────
def scan_local(music_dir: str) -> list[LocalFile]:
    p = Path(music_dir)
    if not p.exists():
        return []
    return [
        LocalFile(path=f, stem=f.stem)
        for f in p.iterdir()
        if f.is_file()
        and f.suffix.lower() in AUDIO_EXTS
        and ".temp." not in f.name  # yt-dlp conversion leftovers
    ]


def cleanup_stale_artifacts(music_dir: str) -> int:
    """Remove old yt-dlp leftovers: any *.temp.* file and stray thumbnail
    images standing next to a matching audio file. Returns count removed."""
    p = Path(music_dir)
    if not p.exists():
        return 0
    removed = 0
    stems = {f.stem for f in p.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS}
    for f in p.iterdir():
        if not f.is_file():
            continue
        try:
            if ".temp." in f.name:
                f.unlink()
                removed += 1
            elif f.suffix.lower() in {".webp", ".png", ".jpg", ".jpeg"} and f.stem in stems:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


# ── match ───────────────────────────────────────────────────────────
@dataclass
class DiffResult:
    matched: list[tuple[Track, LocalFile]]
    missing: list[Track]
    orphans: list[LocalFile]
    failed: list[tuple[Track, str]] = field(default_factory=list)


def build_diff(playlist: list[Track], local: list[LocalFile]) -> DiffResult:
    """Load failed-track state, prune it to the playlist, then diff."""
    failed = load_failed()
    if prune_failed(failed, {t.id for t in playlist}):
        save_failed(failed)
    return diff(playlist, local, failed)


def diff(playlist: list[Track], local: list[LocalFile],
         failed_map: dict[str, dict] | None = None) -> DiffResult:
    failed_map = failed_map or {}
    norm_local = {normalize(f.stem): f for f in local}
    title_index: dict[str, LocalFile] = {}
    for f in local:
        if " - " in f.stem:
            parts = f.stem.rsplit(" - ", 1)
            title_index[normalize(parts[1])] = f

    matched: list[tuple[Track, LocalFile]] = []
    missing: list[Track] = []
    failed: list[tuple[Track, str]] = []
    used = set()

    for t in playlist:
        nt = normalize(t.title)
        if nt in norm_local:
            matched.append((t, norm_local[nt]))
            used.add(norm_local[nt].path)
        elif nt in title_index:
            # A file may satisfy multiple playlist entries (duplicate songs).
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
            elif t.id in failed_map:
                failed.append((t, failed_map[t.id]["error"]))
            else:
                missing.append(t)

    orphans = [f for f in local if f.path not in used]
    return DiffResult(matched=matched, missing=missing, orphans=orphans, failed=failed)


# ── actions ─────────────────────────────────────────────────────────
def trash_file(local: LocalFile, trash_dir: str) -> None:
    dest = Path(trash_dir)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(local.path), str(dest / local.path.name))


def delete_file(local: LocalFile) -> None:
    local.path.unlink()


def resolve_download_targets(
    selected: set[str],
    row_map: dict[str, RowEntry],
    missing: list[Track],
    failed: list[tuple[Track, str]],
    mode: str,
) -> list[Track]:
    """Tracks `d` should download.

    Empty selection: all missing, or all failed when the Failed filter is on.
    Non-empty selection: only selected missing/failed rows — never fall back
    to the full list.
    """
    if selected:
        return [
            e.track
            for k, e in row_map.items()
            if k in selected and e.row_type in ("missing", "failed") and e.track is not None
        ]
    if mode == "failed":
        return [t for t, _err in failed]
    return list(missing)


def resolve_trash_targets(
    selected: set[str],
    row_map: dict[str, RowEntry],
    orphans: list[LocalFile],
) -> list[LocalFile]:
    """Orphans `t` should trash/delete. Same empty vs selected rules as download."""
    if selected:
        return [
            e.local
            for k, e in row_map.items()
            if k in selected and e.row_type == "orphan" and e.local is not None
        ]
    return list(orphans)


def nearest_existing_directory(path: str | Path) -> Path:
    """Find a usable tree root for an existing or future directory path."""
    candidate = Path(path).expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate if candidate.is_dir() else Path.home()


# ── TUI ─────────────────────────────────────────────────────────────
from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import (  # noqa: E402
    Button, DataTable, DirectoryTree, Input, Label, ListItem, ListView,
    LoadingIndicator, RichLog, Static,
)
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.coordinate import Coordinate  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual import events, on  # noqa: E402

# ── Colors ──────────────────────────────────────────────────────────
# Semantic slate palette: surfaces stay quiet; status colors carry meaning.
C_BG         = "#0f1117"
C_BG_DARK    = "#171a23"
C_BG_HL      = "#252b3a"
C_FG         = "#e6eaf2"
C_FG_DIM     = "#9aa4b2"
C_BLUE       = "#79a8ff"
C_CYAN       = "#8bd5ff"
C_GREEN      = "#8fd694"
C_YELLOW     = "#f2c777"
C_RED        = "#ff7a90"
C_MAGENTA    = "#c7a4ff"
C_ORANGE     = "#ffae78"
C_TEAL       = "#72dbc4"


class DirectoryPickerScreen(ModalScreen):
    """Modal directory browser used by the setup screen."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = f"""
    DirectoryPickerScreen {{
        align: center middle;
        background: {C_BG} 92%;
    }}
    DirectoryPickerScreen #picker-card {{
        width: 82;
        max-width: 94%;
        height: 80%;
        max-height: 30;
        min-height: 12;
        background: {C_BG_DARK};
        border: round {C_BG_HL};
        padding: 1 2;
    }}
    DirectoryPickerScreen #picker-title {{
        color: {C_BLUE};
        text-style: bold;
        padding-bottom: 1;
    }}
    DirectoryPickerScreen #picker-selection {{
        color: {C_FG_DIM};
        height: 2;
        padding-bottom: 1;
    }}
    DirectoryPickerScreen #directory-tree {{
        height: 1fr;
        background: {C_BG};
        border: round {C_BG_HL};
        color: {C_FG};
        scrollbar-color: {C_FG_DIM};
        scrollbar-color-hover: {C_BLUE};
    }}
    DirectoryPickerScreen #picker-actions {{
        height: auto;
        align-horizontal: right;
        padding-top: 1;
    }}
    DirectoryPickerScreen Button {{
        min-width: 14;
        height: 3;
        margin-left: 1;
    }}
    DirectoryPickerScreen Button.-style-default {{
        background: {C_BG_HL};
        color: {C_FG};
        border: none !important;
    }}
    DirectoryPickerScreen Button.-style-default.-primary {{
        background: {C_BLUE} !important;
        color: {C_BG} !important;
    }}
    DirectoryPickerScreen Button.-style-default:hover {{
        background: {C_BLUE} !important;
        color: {C_BG} !important;
        border: none !important;
    }}
    DirectoryPickerScreen Button.-style-default.-primary:hover {{
        background: {C_CYAN} !important;
        border: none !important;
    }}
    DirectoryPickerScreen Button.-style-default:focus {{
        border: round {C_CYAN} !important;
        text-style: bold;
    }}
    DirectoryPickerScreen Button.-style-default:disabled {{
        color: {C_FG_DIM} !important;
        background: {C_BG} !important;
        border: none !important;
    }}
    """

    def __init__(self, initial_path: str | Path):
        super().__init__()
        self._selected_path = nearest_existing_directory(initial_path)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-card"):
            yield Static("Choose download folder", id="picker-title")
            yield Label(str(self._selected_path), id="picker-selection")
            yield DirectoryTree(self._selected_path, id="directory-tree")
            with Horizontal(id="picker-actions"):
                yield Button("Choose", id="picker-choose", variant="primary")
                yield Button("Cancel", id="picker-cancel")

    def on_mount(self) -> None:
        self.query_one("#directory-tree", DirectoryTree).focus()

    @on(DirectoryTree.DirectorySelected)
    def on_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self._selected_path = event.path
        self.query_one("#picker-selection", Label).update(str(event.path))

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "picker-choose":
            self.dismiss(self._selected_path)
        elif event.button.id == "picker-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfigurationHomeScreen(ModalScreen):
    """Keyboard-first chooser for saved URL/directory configurations."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = f"""
    ConfigurationHomeScreen {{
        align: center middle;
        background: {C_BG} 92%;
    }}
    ConfigurationHomeScreen #config-home-card {{
        width: 88;
        max-width: 94%;
        height: auto;
        max-height: 90%;
        background: {C_BG_DARK};
        border: round {C_BG_HL};
        padding: 1 2;
    }}
    ConfigurationHomeScreen #config-home-title {{
        color: {C_BLUE};
        text-style: bold;
        padding-bottom: 0;
    }}
    ConfigurationHomeScreen #config-home-subtitle {{
        color: {C_FG_DIM};
        padding-bottom: 0;
    }}
    ConfigurationHomeScreen #saved-config-label {{
        color: {C_FG};
        text-style: bold;
        padding-bottom: 0;
    }}
    ConfigurationHomeScreen #saved-config-list {{
        width: 100%;
        height: 6;
        background: {C_BG};
        border: round {C_BG_HL};
    }}
    ConfigurationHomeScreen #saved-config-list:hover {{
        border: round {C_BLUE};
    }}
    ConfigurationHomeScreen #saved-config-list:focus {{
        border: round {C_CYAN};
    }}
    ConfigurationHomeScreen #saved-config-list > ListItem {{
        height: 2;
        padding: 0 1;
        background: {C_BG};
    }}
    ConfigurationHomeScreen #saved-config-list > ListItem.-hovered {{
        background: {C_BG_HL};
    }}
    ConfigurationHomeScreen #saved-config-list > ListItem.-highlight {{
        background: {C_BG_HL};
    }}
    ConfigurationHomeScreen #saved-config-list:focus > ListItem.-highlight {{
        background: {C_BLUE};
    }}
    ConfigurationHomeScreen .saved-config-url {{
        color: {C_FG};
        text-style: bold;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    ConfigurationHomeScreen .saved-config-dir {{
        color: {C_FG_DIM};
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    ConfigurationHomeScreen #saved-config-list:focus > ListItem.-highlight .saved-config-url,
    ConfigurationHomeScreen #saved-config-list:focus > ListItem.-highlight .saved-config-dir {{
        color: {C_BG};
    }}
    ConfigurationHomeScreen .active-configuration .saved-config-url {{
        color: {C_CYAN};
    }}
    ConfigurationHomeScreen #saved-config-empty {{
        color: {C_FG_DIM};
        background: {C_BG};
        border: round {C_BG_HL};
        padding: 1 2;
    }}
    ConfigurationHomeScreen #config-home-error {{
        color: {C_RED};
        height: auto;
        min-height: 1;
        padding-top: 0;
    }}
    ConfigurationHomeScreen #config-home-actions {{
        height: auto;
        align-horizontal: right;
        padding-top: 0;
    }}
    ConfigurationHomeScreen #config-home-actions Button {{
        min-width: 18;
        height: 3;
        margin-left: 1;
    }}
    ConfigurationHomeScreen Button.-style-default {{
        background: {C_BG_HL} !important;
        color: {C_FG} !important;
        border: none !important;
    }}
    ConfigurationHomeScreen Button.-style-default.-primary {{
        background: {C_BLUE} !important;
        color: {C_BG} !important;
    }}
    ConfigurationHomeScreen Button.-style-default:hover {{
        background: {C_BLUE} !important;
        color: {C_BG} !important;
        border: none !important;
    }}
    ConfigurationHomeScreen Button.-style-default.-primary:hover {{
        background: {C_CYAN} !important;
        border: none !important;
    }}
    ConfigurationHomeScreen Button.-style-default:focus {{
        border: round {C_CYAN} !important;
        text-style: bold;
    }}
    ConfigurationHomeScreen Button.-style-default:disabled {{
        color: {C_FG_DIM} !important;
        background: {C_BG} !important;
        border: none !important;
    }}
    """

    def __init__(self, cfg: dict, initial: bool = False):
        super().__init__()
        self.cfg = cfg
        self.initial = initial
        self._saved_configs = saved_configurations(cfg)
        active_url = cfg.get("playlist_url", "")
        active_dir = cfg.get("music_dir", DEFAULTS["music_dir"])
        self._active_index = next(
            (
                index
                for index, saved in enumerate(self._saved_configs)
                if saved["playlist_url"] == active_url and saved["music_dir"] == active_dir
            ),
            0,
        )

    def compose(self) -> ComposeResult:
        cancel_label = "Quit" if self.initial else "Cancel"
        with Vertical(id="config-home-card"):
            yield Static("♫  yt-sync", id="config-home-title")
            yield Static(
                "Choose a saved playlist and download folder, or create a new one.",
                id="config-home-subtitle",
            )
            yield Label("Saved configurations", id="saved-config-label")
            if self._saved_configs:
                items = []
                for index, saved in enumerate(self._saved_configs):
                    active = index == self._active_index
                    url_label = f"{saved['name']}  ·  {saved['playlist_url']}"
                    if active:
                        url_label = f"● Active  {url_label}"
                    items.append(
                        ListItem(
                            Static(url_label, classes="saved-config-url", markup=False),
                            Static(saved["music_dir"], classes="saved-config-dir", markup=False),
                            id=f"saved-config-{index}",
                            classes="active-configuration" if active else None,
                        )
                    )
                yield ListView(
                    *items,
                    initial_index=self._active_index,
                    id="saved-config-list",
                )
            else:
                yield Static(
                    "No saved configurations yet. Create one to begin.",
                    id="saved-config-empty",
                )
            yield Label("", id="config-home-error")
            with Horizontal(id="config-home-actions"):
                yield Button(
                    "Create new configuration",
                    id="create-configuration",
                    variant="primary",
                )
                yield Button(cancel_label, id="cancel-home")

    def on_mount(self) -> None:
        if self._saved_configs:
            self.query_one("#saved-config-list", ListView).focus()
        else:
            self.query_one("#create-configuration", Button).focus()

    @on(ListView.Selected)
    def on_saved_config_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "saved-config-list":
            return
        saved = self._saved_configs[event.index]
        self.dismiss(SetupValues(saved["name"], saved["playlist_url"], saved["music_dir"]))

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create-configuration":
            form_cfg = {
                **self.cfg,
                "name": "",
                "playlist_url": "",
                "music_dir": DEFAULTS["music_dir"],
            }
            self.app.push_screen(HomeScreen(form_cfg), self._on_new_configuration)
        elif event.button.id == "cancel-home":
            self.dismiss(None)

    def _on_new_configuration(self, values: SetupValues | None) -> None:
        if values is not None:
            self.dismiss(values)

    def action_cancel(self) -> None:
        if self.initial:
            self.query_one("#config-home-error", Label).update(
                "Choose a configuration or create one — use Quit to exit without syncing."
            )
            if self._saved_configs:
                self.query_one("#saved-config-list", ListView).focus()
            else:
                self.query_one("#create-configuration", Button).focus()
            return
        self.dismiss(None)


class HomeScreen(ModalScreen):
    """Creation form for one playlist URL and local download destination."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = f"""
    HomeScreen {{
        align: center middle;
        background: {C_BG} 92%;
    }}
    HomeScreen #home-card {{
        width: 76;
        max-width: 92%;
        height: auto;
        background: {C_BG_DARK};
        border: round {C_BG_HL};
        padding: 1 2;
    }}
    HomeScreen #home-title {{
        color: {C_BLUE};
        text-style: bold;
        padding-bottom: 0;
    }}
    HomeScreen #home-subtitle {{
        color: {C_FG_DIM};
        padding-bottom: 1;
    }}
    HomeScreen .home-label {{
        color: {C_FG};
        text-style: bold;
        padding-top: 0;
        padding-bottom: 0;
    }}
    HomeScreen Input {{
        width: 100%;
        height: 3;
        background: {C_BG};
        border: round {C_BG_HL};
        color: {C_FG};
    }}
    HomeScreen Input:hover {{
        border: round {C_FG_DIM};
    }}
    HomeScreen Input:focus {{
        border: round {C_CYAN};
        text-style: bold;
    }}
    HomeScreen Input:disabled {{
        color: {C_FG_DIM};
        background: {C_BG_DARK};
        border: round {C_BG_HL};
    }}
    HomeScreen #destination-row {{
        width: 100%;
        height: 3;
    }}
    HomeScreen #destination-row Input {{
        width: 1fr;
    }}
    HomeScreen #browse-folder {{
        width: 14;
        height: 3;
        margin-left: 1;
    }}
    HomeScreen #home-error {{
        color: {C_RED};
        height: auto;
        min-height: 1;
        padding-top: 0;
    }}
    HomeScreen #home-actions {{
        height: auto;
        align-horizontal: right;
        padding-top: 0;
    }}
    HomeScreen #home-actions Button {{
        min-width: 14;
        height: 3;
        margin-left: 1;
    }}
    HomeScreen Button.-style-default {{
        background: {C_BG_HL} !important;
        color: {C_FG} !important;
        border: none !important;
    }}
    HomeScreen Button.-style-default.-primary {{
        background: {C_BLUE} !important;
        color: {C_BG} !important;
    }}
    HomeScreen Button.-style-default:hover {{
        background: {C_BLUE} !important;
        color: {C_BG} !important;
        border: none !important;
    }}
    HomeScreen Button.-style-default.-primary:hover {{
        background: {C_CYAN} !important;
        border: none !important;
    }}
    HomeScreen Button.-style-default:focus {{
        border: round {C_CYAN} !important;
        text-style: bold;
    }}
    HomeScreen Button.-style-default:disabled {{
        color: {C_FG_DIM} !important;
        background: {C_BG} !important;
        border: none !important;
    }}
    """

    def __init__(self, cfg: dict, initial: bool = False):
        super().__init__()
        self.cfg = cfg
        self.initial = initial

    def compose(self) -> ComposeResult:
        cancel_label = "Quit" if self.initial else "Cancel"
        with Vertical(id="home-card"):
            yield Static("♫  yt-sync", id="home-title")
            yield Static(
                "Create a configuration with one playlist URL and one download folder.",
                id="home-subtitle",
            )
            yield Label("Configuration name", classes="home-label")
            yield Input(
                value=self.cfg.get("name", ""),
                placeholder="e.g. Road trips",
                id="configuration-name",
            )
            yield Label("Playlist URL", classes="home-label")
            yield Input(
                value=self.cfg.get("playlist_url", ""),
                placeholder="https://music.youtube.com/playlist?list=...",
                id="playlist-url",
            )
            yield Label("Download folder", classes="home-label")
            with Horizontal(id="destination-row"):
                yield Input(
                    value=self.cfg.get("music_dir", DEFAULTS["music_dir"]),
                    placeholder=str(Path.home() / "Music"),
                    id="music-dir",
                )
                yield Button("Browse…", id="browse-folder")
            yield Label("", id="home-error")
            with Horizontal(id="home-actions"):
                yield Button("Start sync", id="start-sync", variant="primary")
                yield Button(cancel_label, id="cancel-home")

    def on_mount(self) -> None:
        self.query_one("#configuration-name", Input).focus()

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "configuration-name":
            self.query_one("#playlist-url", Input).focus()
        elif event.input.id == "playlist-url":
            self.query_one("#music-dir", Input).focus()
        elif event.input.id == "music-dir":
            self._submit()

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browse-folder":
            self.app.push_screen(
                DirectoryPickerScreen(self.query_one("#music-dir", Input).value),
                self._on_folder_picked,
            )
        elif event.button.id == "start-sync":
            self._submit()
        elif event.button.id == "cancel-home":
            self.dismiss(None)

    def _on_folder_picked(self, path: Path | None) -> None:
        if path is None:
            return
        destination = self.query_one("#music-dir", Input)
        destination.value = str(path)
        destination.focus()

    def _submit(self) -> None:
        values, error = validate_setup_values(
            self.query_one("#configuration-name", Input).value,
            self.query_one("#playlist-url", Input).value,
            self.query_one("#music-dir", Input).value,
        )
        if values is None:
            self.query_one("#home-error", Label).update(error)
            return
        try:
            Path(values.music_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.query_one("#home-error", Label).update(
                f"Cannot create destination folder: {exc}"
            )
            return
        self.dismiss(values)

    def action_cancel(self) -> None:
        if self.initial:
            self.query_one("#home-error", Label).update(
                "Setup is still open — use Quit to exit without syncing."
            )
            self.query_one("#configuration-name", Input).focus()
            return
        self.dismiss(None)


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
        border: round {C_BG_HL};
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
            yield Static("  [bold cyan]4[/]            show failed downloads", classes="help-row")
            yield Static("── Actions ──", classes="help-section")
            yield Static("  [bold cyan]enter[/]        download / retry / trash current row", classes="help-row")
            yield Static("  [bold cyan]d[/]            batch download (selected, or all if none)", classes="help-row")
            yield Static("  [bold cyan]t[/]            batch trash (selected, or all if none)", classes="help-row")
            yield Static("  [bold cyan]x[/]            toggle delete mode (trash ↔ real)", classes="help-row")
            yield Static("  [bold cyan]r[/]            refresh (re-fetch playlist)", classes="help-row")
            yield Static("  [bold cyan]h[/]            open configurations", classes="help-row")
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
    #app-header {{
        background: {C_BG_DARK};
        color: {C_FG};
        dock: top;
        height: 1;
        padding: 0 1;
        text-style: bold;
    }}

    /* ── Status bar ─────────────────────────────────────────── */
    #status-bar {{
        height: 1;
        dock: top;
        background: {C_BG_DARK};
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
        background: {C_BG_HL};
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
        border: round {C_BG_HL};
        scrollbar-color: {C_FG_DIM};
        scrollbar-color-hover: {C_BLUE};
        scrollbar-color-active: {C_CYAN};
    }}
    DataTable > .datatable--header {{
        background: {C_BG_DARK};
        color: {C_BLUE};
        text-style: bold;
        border-bottom: solid {C_BG_HL};
    }}
    DataTable > .datatable--cursor {{
        background: {C_BG_HL};
        color: {C_FG};
        text-style: bold;
    }}
    DataTable > .datatable--even-row {{
        background: {C_BG};
    }}
    DataTable > .datatable--odd-row {{
        background: {C_BG_DARK};
    }}
    DataTable:focus {{
        border: round {C_BLUE};
    }}
    DataTable:hover {{
        border: round {C_BLUE};
    }}

    /* ── Console ────────────────────────────────────────────── */
    #console {{
        height: 8;
        background: {C_BG_DARK};
        color: {C_FG_DIM};
        border: round {C_BG_HL};
        padding: 0 1;
        scrollbar-color: {C_FG_DIM};
        scrollbar-color-hover: {C_BLUE};
    }}
    #console:focus {{
        border: round {C_BLUE};
    }}
    #console.op-download {{
        border: round {C_GREEN};
    }}
    #console.op-trash {{
        border: round {C_ORANGE};
    }}
    #console.op-refresh {{
        border: round {C_CYAN};
    }}

    /* ── Persistent filter keys ─────────────────────────────── */
    #filter-hint {{
        height: 1;
        dock: bottom;
        background: {C_BG_DARK};
        color: {C_FG_DIM};
        padding: 0 1;
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
        Binding("4", "filter_failed", "Failed"),
        Binding("enter", "act_on_current", "Act", show=True),
        Binding("d", "download", "Download"),
        Binding("t", "trash", "Trash"),
        Binding("x", "toggle_delete", "Del-mode"),
        Binding("r", "refresh", "Refresh"),
        Binding("h", "open_home", "Configurations"),
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
        self._row_map: dict[str, RowEntry] = {}

    def compose(self) -> ComposeResult:
        yield Static("♫ yt-sync  ·  YouTube Playlist ↔ Local Music Sync", id="app-header")
        yield Label("", id="status-bar")
        yield LoadingIndicator(id="loader")
        yield DataTable(id="table", zebra_stripes=True)
        yield RichLog(id="console", highlight=True, markup=True)
        yield Static("1 All · 2 Missing · 3 Orphans · 4 Failed", id="filter-hint")

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        table.add_columns("", "Title", "Status")
        table.focus()
        self._log(f"[bold {C_BLUE}]♫ yt-sync ready[/]")
        del_status = f"[bold {C_RED}]real delete[/]" if self.delete_mode else f"[bold {C_GREEN}]trash[/]"
        self._log(f"  delete mode: {del_status}")
        self._log(f"  press [bold {C_CYAN}]?[/] for keybindings")
        self._open_home(initial=True)

    def _open_home(self, initial: bool = False) -> None:
        self.push_screen(
            ConfigurationHomeScreen(self.cfg, initial=initial),
            lambda result: self._on_home_result(result, initial),
        )

    def _on_home_result(self, result: SetupValues | None, initial: bool) -> None:
        if result is None:
            if initial:
                self.exit()
            return

        previous = {
            "playlist_url": self.cfg.get("playlist_url", ""),
            "music_dir": self.cfg.get("music_dir", DEFAULTS["music_dir"]),
            "saved_configs": list(self.cfg.get("saved_configs", [])),
        }
        self.cfg["playlist_url"] = result.playlist_url
        self.cfg["music_dir"] = result.music_dir
        self.cfg["saved_configs"] = saved_configurations(
            {
                **self.cfg,
                "saved_configs": [
                    *self.cfg.get("saved_configs", []),
                    {
                        "name": result.name,
                        "playlist_url": result.playlist_url,
                        "music_dir": result.music_dir,
                    },
                ],
            }
        )
        try:
            save_config(self.cfg)
        except OSError as exc:
            self.cfg.update(previous)
            self._log(f"[bold {C_RED}]Could not save config: {exc}[/]")
            self.notify("Could not save setup; please try again", severity="error", timeout=5)
            self._open_home(initial=initial)
            return

        self.mode = "all"
        self.selected.clear()
        self._log(f"  destination: [bold]{self.cfg['music_dir']}[/]")
        self._do_refresh()

    def action_open_home(self) -> None:
        if self._busy:
            self.notify("⏳ Already working…", severity="warning", timeout=2)
            return
        self._open_home()

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
            self.diff_result = await asyncio.to_thread(
                build_diff, playlist, local
            )
            d = self.diff_result
            self._log(
                f"  result:   [{C_GREEN}]{len(d.matched)} matched[/]  "
                f"[{C_YELLOW}]{len(d.missing)} missing[/]  "
                f"[{C_RED}]{len(d.failed)} failed[/]  "
                f"[{C_ORANGE}]{len(d.orphans)} orphans[/]"
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
                self._row_map[k] = RowEntry(t.title, "✔ synced", "matched", track=t)
        if self.mode in ("all", "missing"):
            for t in d.missing:
                key += 1
                k = str(key)
                self._row_map[k] = RowEntry(t.title, "⬇ missing", "missing", track=t)
        if self.mode in ("all", "failed"):
            for t, err in d.failed:
                key += 1
                k = str(key)
                short = err if len(err) <= 42 else err[:39] + "…"
                self._row_map[k] = RowEntry(t.title, f"✗ {short}", "failed", track=t)
        if self.mode in ("all", "orphans"):
            for f in d.orphans:
                key += 1
                k = str(key)
                self._row_map[k] = RowEntry(f.stem, "✗ orphan", "orphan", local=f)

        for k, e in self._row_map.items():
            sel_marker = self._select_marker(k)

            # Color the status text
            if e.row_type == "matched":
                styled_status = f"[{C_GREEN}]{e.status}[/]"
            elif e.row_type == "missing":
                styled_status = f"[{C_YELLOW}]{e.status}[/]"
            elif e.row_type == "failed":
                styled_status = f"[{C_RED}]{e.status}[/]"
            else:
                styled_status = f"[{C_ORANGE}]{e.status}[/]"

            table.add_row(sel_marker + k, e.title, styled_status, key=k)

        self._update_status_bar()

    def _update_status_bar(self) -> None:
        d = self.diff_result
        del_icon = f"[bold {C_RED}]⊘ DEL[/]" if self.delete_mode else f"[bold {C_GREEN}]♻ TRASH[/]"
        sel_info = f"  [{C_MAGENTA}]▶ {len(self.selected)} selected[/]" if self.selected else ""

        filter_labels = {
            "all": f"[bold {C_BLUE}]ALL[/]",
            "missing": f"[bold {C_YELLOW}]MISSING[/]",
            "failed": f"[bold {C_RED}]FAILED[/]",
            "orphans": f"[bold {C_ORANGE}]ORPHANS[/]",
        }
        filter_label = filter_labels.get(self.mode, "ALL")

        self.query_one("#status-bar", Label).update(
            f" {filter_label}"
            f"  [{C_GREEN}]✔ {len(d.matched)}[/]"
            f"  [{C_YELLOW}]⬇ {len(d.missing)}[/]"
            f"  [{C_RED}]✗ {len(d.failed)} failed[/]"
            f"  [{C_ORANGE}]⊘ {len(d.orphans)}[/]"
            f"{sel_info}"
            f"  {del_icon}"
        )

    def _key_type(self, key: str) -> str:
        """Map a row key back to its row type."""
        e = self._row_map.get(key)
        return e.row_type if e else "matched"

    def _select_marker(self, k: str) -> str:
        return f"[bold {C_MAGENTA}]▶[/] " if k in self.selected else "  "

    def _refresh_select_marker(self, table: DataTable, k: str, row: int) -> None:
        """Update the ▶ marker in place so cursor and scroll stay put."""
        table.update_cell_at(Coordinate(row, 0), self._select_marker(k) + k)

    def _row_key_at_cursor(self, table: DataTable) -> str | None:
        if table.row_count == 0 or table.cursor_row >= len(table.ordered_rows):
            return None
        row_key = table.ordered_rows[table.cursor_row].key
        if row_key is None or row_key.value is None:
            return None
        return str(row_key.value)

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
        k = self._row_key_at_cursor(table)
        if k is None:
            return
        if k in self.selected:
            self.selected.discard(k)
        else:
            self.selected.add(k)
        self._refresh_select_marker(table, k, table.cursor_row)
        self._update_status_bar()

    def action_clear_select(self) -> None:
        if not self.selected:
            return
        table = self.query_one("#table", DataTable)
        previously = set(self.selected)
        self.selected.clear()
        for i, row in enumerate(table.ordered_rows):
            if row.key is None or row.key.value is None:
                continue
            k = str(row.key.value)
            if k in previously:
                self._refresh_select_marker(table, k, i)
        self._update_status_bar()

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

    def action_filter_failed(self) -> None:
        self.mode = "failed"
        self.selected.clear()
        self._populate_table()
        self.notify("Filter: Failed only (enter to retry)", severity="information", timeout=2)

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
        k = self._row_key_at_cursor(table)
        if k is None:
            return
        rtype = self._key_type(k)

        if rtype == "matched":
            self.notify("✔ Track is already synced", severity="information", timeout=2)
            return
        elif rtype in ("missing", "failed"):
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
        e = self._row_map.get(key)
        return e.track if e else None

    def _localfile_for_key(self, key: str) -> LocalFile | None:
        e = self._row_map.get(key)
        return e.local if e else None

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
        # Selection wins: download every selected missing/failed row.
        # With nothing selected, download all missing (previously-failed
        # tracks are only retried explicitly). A non-empty selection never
        # falls back to the full missing list.
        targets = resolve_download_targets(
            self.selected, self._row_map, d.missing, d.failed, self.mode
        )
        if not targets:
            self.notify("Nothing to download", severity="warning", timeout=2)
            self._log(f"[{C_YELLOW}]Nothing to download.[/]")
            return
        self._busy = True
        self._set_loading(True, "download")
        self.notify(f"⬇ Downloading {len(targets)} tracks…", severity="information", timeout=3)
        self.run_worker(self._do_download(targets), exclusive=True)

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
        targets = resolve_trash_targets(self.selected, self._row_map, d.orphans)
        if not targets:
            self.notify("No orphans to act on", severity="warning", timeout=2)
            self._log(f"[{C_YELLOW}]No orphans to act on.[/]")
            return
        self._busy = True
        verb = "Deleting" if self.delete_mode else "Trashing"
        self._set_loading(True, "trash")
        self.notify(f"{'⊘' if self.delete_mode else '♻'} {verb} {len(targets)} orphans…", severity="information", timeout=3)
        self.run_worker(self._do_trash(targets), exclusive=True)

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

    cli_mode = any(
        flag in sys.argv for flag in ("--diff-only", "--download-only", "--auto")
    )
    if cli_mode and not cfg["playlist_url"]:
        print("Error: playlist_url not set in", CONFIG_PATH)
        sys.exit(1)

    if cli_mode:
        removed = cleanup_stale_artifacts(cfg["music_dir"])
        if removed:
            print(f"Cleaned {removed} leftover yt-dlp artifact(s) from music dir.")

    if "--diff-only" in sys.argv:
        playlist = fetch_playlist(cfg["playlist_url"])
        local = scan_local(cfg["music_dir"])
        d = build_diff(playlist, local)
        if d.missing:
            print(f"{len(d.missing)} MISSING:")
            for i, t in enumerate(d.missing, 1):
                print(f"  {i:3d}. {t.title}")
        if d.failed:
            print(f"\n{len(d.failed)} FAILED (use --retry-failed to retry):")
            for i, (t, err) in enumerate(d.failed, 1):
                print(f"  {i:3d}. {t.title}  [{err}]")
        if d.orphans:
            print(f"\n{len(d.orphans)} ORPHANS (local-only):")
            for i, f in enumerate(d.orphans, 1):
                print(f"  {i:3d}. {f.stem}")
        if not d.missing and not d.failed and not d.orphans:
            print(f"All {len(d.matched)} tracks synced. Nothing to do.")
        return

    if "--download-only" in sys.argv or "--auto" in sys.argv:
        playlist = fetch_playlist(cfg["playlist_url"])
        local = scan_local(cfg["music_dir"])
        d = build_diff(playlist, local)
        retry_failed = "--retry-failed" in sys.argv

        targets = list(d.missing)
        if retry_failed:
            targets += [t for t, _err in d.failed]
        if not targets:
            print("Nothing to download.")
        else:
            print(f"Downloading {len(targets)} tracks...")
            ok = fail = 0
            for i, t in enumerate(targets, 1):
                print(f"  [{i}/{len(targets)}] {t.title}")
                result = download_track(t, cfg["music_dir"], cfg["audio_format"])
                if result.success:
                    ok += 1
                else:
                    fail += 1
                    print(f"    FAILED: {result.error}")
            print(f"Downloads complete: {ok} ok, {fail} failed.")
        if d.failed and not retry_failed:
            print(
                f"Skipped {len(d.failed)} previously-failed "
                f"(pass --retry-failed to retry): "
                + ", ".join(t.title for t, _e in d.failed[:5])
                + ("…" if len(d.failed) > 5 else "")
            )
        if "--auto" in sys.argv:
            if d.orphans:
                print(f"Trashing {len(d.orphans)} orphans...")
                for i, f in enumerate(d.orphans, 1):
                    print(f"  [{i}/{len(d.orphans)}] {f.stem}")
                    if cfg["real_delete"]:
                        delete_file(f)
                    else:
                        trash_file(f, cfg["trash_dir"])
                print("Orphans done.")
            if not d.missing and not d.orphans:
                print("Already synced.")
        return

    YTSyncApp(cfg).run()


if __name__ == "__main__":
    main()
