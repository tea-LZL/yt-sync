#!/usr/bin/env python3
"""Regression tests for yt-sync selection → download/trash targeting and cursor."""

from __future__ import annotations

import io
import sys
import time
import types
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_sync
from textual.color import Color
from textual.widgets import Button, DataTable, Input, ListView, Static
from yt_sync import (
    DEFAULTS,
    DiffResult,
    LocalFile,
    RowEntry,
    Track,
    YTSyncApp,
    diff,
    resolve_download_targets,
    resolve_trash_targets,
)


class StateIsolatedMixin:
    """Point every on-disk state file at a per-test temp dir, never $HOME."""

    def setUp(self) -> None:
        super().setUp()
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        for attribute, filename in (
            ("CONFIG_PATH", "config.toml"),
            ("FAILED_PATH", "failed.json"),
            ("VIEW_CACHE_PATH", "view-cache.json"),
        ):
            previous = getattr(yt_sync, attribute)
            setattr(yt_sync, attribute, Path(state_dir.name) / filename)
            self.addCleanup(setattr, yt_sync, attribute, previous)


def _sample():
    synced = Track("s1", "Synced", "http://x/s1")
    m1 = Track("m1", "Missing One", "http://x/m1")
    m2 = Track("m2", "Missing Two", "http://x/m2")
    f1 = Track("f1", "Failed One", "http://x/f1")
    orphan = LocalFile(Path("/tmp/orphan.opus"), "Orphan")
    row_map = {
        "1": RowEntry(synced.title, "✔ synced", "matched", track=synced),
        "2": RowEntry(m1.title, "⬇ missing", "missing", track=m1),
        "3": RowEntry(m2.title, "⬇ missing", "missing", track=m2),
        "4": RowEntry(f1.title, "✗ err", "failed", track=f1),
        "5": RowEntry(orphan.stem, "✗ orphan", "orphan", local=orphan),
    }
    return row_map, [m1, m2], [(f1, "err")], [orphan], synced, m1, m2, f1, orphan


class SetupValidationTests(StateIsolatedMixin, unittest.TestCase):
    def test_rejects_blank_configuration_name(self):
        values, error = yt_sync.validate_setup_values(
            "  ", "https://example.test/list", "/tmp/music"
        )
        self.assertIsNone(values)
        self.assertEqual(error, "Enter a configuration name.")

    def test_rejects_blank_url(self):
        values, error = yt_sync.validate_setup_values("Library", "  ", "/tmp/music")
        self.assertIsNone(values)
        self.assertEqual(error, "Enter a playlist URL.")

    def test_rejects_non_http_url(self):
        values, error = yt_sync.validate_setup_values(
            "Library", "ftp://example.test/list", "/tmp/music"
        )
        self.assertIsNone(values)
        self.assertEqual(error, "Enter a valid HTTP(S) playlist URL.")

    def test_normalizes_name_and_expands_destination_without_resolving_it(self):
        values, error = yt_sync.validate_setup_values(
            "  Road trips  ",
            "https://music.youtube.com/playlist?list=abc",
            "~/Music/New Playlist",
        )
        self.assertEqual(error, "")
        self.assertIsNotNone(values)
        assert values is not None
        self.assertEqual(values.name, "Road trips")
        self.assertEqual(values.playlist_url, "https://music.youtube.com/playlist?list=abc")
        self.assertEqual(values.music_dir, str(Path.home() / "Music/New Playlist"))

    def test_rejects_blank_destination(self):
        values, error = yt_sync.validate_setup_values("Library", "https://example.test/list", " ")
        self.assertIsNone(values)
        self.assertEqual(error, "Enter a destination folder.")


class ConfigPersistenceTests(StateIsolatedMixin, unittest.TestCase):
    def test_save_config_keeps_the_previous_contents_as_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            first = {
                **DEFAULTS,
                "playlist_url": "https://example.test/one",
                "music_dir": str(Path(tmp) / "one"),
            }
            second = {**first, "playlist_url": "https://example.test/two"}
            with patch("yt_sync.CONFIG_PATH", config_path):
                yt_sync.save_config(dict(first))
                yt_sync.save_config(dict(second))
            backup = config_path.with_name("config.toml.bak")
            self.assertTrue(backup.exists())
            self.assertIn("https://example.test/one", backup.read_text())
            self.assertIn("https://example.test/two", config_path.read_text())

    def test_load_config_leaves_an_unreadable_file_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("this is not toml = = =")
            stderr = io.StringIO()
            with (
                patch("yt_sync.CONFIG_PATH", config_path),
                patch.object(sys, "stderr", stderr),
            ):
                cfg = yt_sync.load_config()
            self.assertEqual(cfg["playlist_url"], DEFAULTS["playlist_url"])
            self.assertIn("could not read", stderr.getvalue())
            self.assertTrue(config_path.exists())
            self.assertIn("this is not toml", config_path.read_text())

    def test_save_config_round_trips_setup_values_and_existing_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            cfg = {
                **DEFAULTS,
                "playlist_url": "https://music.youtube.com/playlist?list=abc",
                "music_dir": str(Path(tmp) / "Music"),
                "real_delete": True,
                "animations": "basic",
            }
            with patch("yt_sync.CONFIG_PATH", config_path):
                yt_sync.save_config(cfg)
                loaded = yt_sync.load_config()
        self.assertEqual(loaded["playlist_url"], cfg["playlist_url"])
        self.assertEqual(loaded["music_dir"], cfg["music_dir"])
        self.assertTrue(loaded["real_delete"])
        self.assertEqual(loaded["animations"], "basic")


class StateIsolationTests(StateIsolatedMixin, unittest.TestCase):
    def test_state_files_never_point_at_the_real_home_directory(self):
        for path in (
            yt_sync.CONFIG_PATH,
            yt_sync.FAILED_PATH,
            yt_sync.VIEW_CACHE_PATH,
        ):
            self.assertTrue(
                str(path).startswith(tempfile.gettempdir()),
                f"{path} escapes the per-test temp dir",
            )


class SavedConfigurationPersistenceTests(StateIsolatedMixin, unittest.TestCase):
    def test_save_config_persists_configuration_name(self):
        saved = {
            "name": "Road trips",
            "playlist_url": "https://music.youtube.com/playlist?list=road-trips",
            "music_dir": "/tmp/road-trips",
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            cfg = {**DEFAULTS, **saved, "saved_configs": [saved]}
            with patch("yt_sync.CONFIG_PATH", config_path):
                yt_sync.save_config(cfg)
            with config_path.open("rb") as config_file:
                section = tomllib.load(config_file)["yt-sync"]

        self.assertEqual(section["saved_configs"], [saved])

    def test_load_config_migrates_legacy_active_pair_to_named_saved_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[yt-sync]\n"
                'playlist_url = "https://music.youtube.com/playlist?list=legacy"\n'
                'music_dir = "/tmp/legacy-music"\n',
                encoding="utf-8",
            )
            with patch("yt_sync.CONFIG_PATH", config_path):
                loaded = yt_sync.load_config()

        self.assertEqual(
            loaded["saved_configs"],
            [
                {
                    "name": "legacy-music",
                    "playlist_url": "https://music.youtube.com/playlist?list=legacy",
                    "music_dir": "/tmp/legacy-music",
                }
            ],
        )
        self.assertEqual(loaded["playlist_url"], "https://music.youtube.com/playlist?list=legacy")
        self.assertEqual(loaded["music_dir"], "/tmp/legacy-music")

    def test_load_config_names_existing_saved_entry_without_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[yt-sync]\n"
                'playlist_url = "https://music.youtube.com/playlist?list=saved"\n'
                'music_dir = "/tmp/saved-music"\n\n'
                "[[yt-sync.saved_configs]]\n"
                'playlist_url = "https://music.youtube.com/playlist?list=saved"\n'
                'music_dir = "/tmp/saved-music"\n',
                encoding="utf-8",
            )
            with patch("yt_sync.CONFIG_PATH", config_path):
                loaded = yt_sync.load_config()

        self.assertEqual(
            loaded["saved_configs"],
            [
                {
                    "name": "saved-music",
                    "playlist_url": "https://music.youtube.com/playlist?list=saved",
                    "music_dir": "/tmp/saved-music",
                }
            ],
        )

    def test_save_config_persists_name_url_and_directory_for_saved_configurations(self):
        saved = {
            "name": "Saved collection",
            "playlist_url": "https://music.youtube.com/playlist?list=saved",
            "music_dir": "/tmp/saved-music",
        }
        active = {
            "name": "Active collection",
            "playlist_url": "https://music.youtube.com/playlist?list=active",
            "music_dir": "/tmp/active-music",
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            cfg = {
                **DEFAULTS,
                "playlist_url": active["playlist_url"],
                "music_dir": active["music_dir"],
                "saved_configs": [saved, active],
            }
            with patch("yt_sync.CONFIG_PATH", config_path):
                yt_sync.save_config(cfg)
            with config_path.open("rb") as config_file:
                section = tomllib.load(config_file)["yt-sync"]

        self.assertEqual(section["playlist_url"], active["playlist_url"])
        self.assertEqual(section["music_dir"], active["music_dir"])
        self.assertEqual(section["saved_configs"], [saved, active])
        self.assertEqual(
            [set(saved_config) for saved_config in section["saved_configs"]],
            [
                {"name", "playlist_url", "music_dir"},
                {"name", "playlist_url", "music_dir"},
            ],
        )


class DownloadDirectoryTests(StateIsolatedMixin, unittest.TestCase):
    def test_creates_missing_destination_before_yt_dlp(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "nested" / "Music"
            completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
            track = Track("new", "New Track", "https://example.test/new")
            with patch("yt_sync.load_failed", return_value={}):
                with patch("yt_sync.subprocess.run", return_value=completed) as run:
                    result = yt_sync.download_track(track, str(destination), "opus")
            self.assertTrue(result.success)
            self.assertTrue(destination.is_dir())
            self.assertEqual(run.call_args.args[0][-1], track.url)

    def test_reports_destination_creation_error_without_running_yt_dlp(self):
        track = Track("blocked", "Blocked", "https://example.test/blocked")
        with patch.object(Path, "mkdir", side_effect=OSError("permission denied")):
            with patch("yt_sync.subprocess.run") as run:
                result = yt_sync.download_track(track, "/tmp/blocked", "opus")
        self.assertFalse(result.success)
        self.assertIn("music directory", result.error)
        run.assert_not_called()


class YtDlpInvocationTests(StateIsolatedMixin, unittest.TestCase):
    def test_fetch_playlist_runs_yt_dlp_as_the_app_python_module(self):
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch("yt_sync.subprocess.run", return_value=completed) as run:
            yt_sync.fetch_playlist("https://example.test/list")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], [sys.executable, "-m", "yt_dlp"])
        self.assertEqual(cmd[-1], "https://example.test/list")

    def test_download_track_runs_yt_dlp_as_the_app_python_module(self):
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        track = Track("new", "New Track", "https://example.test/new")
        with tempfile.TemporaryDirectory() as tmp:
            with patch("yt_sync.load_failed", return_value={}):
                with patch("yt_sync.subprocess.run", return_value=completed) as run:
                    yt_sync.download_track(track, tmp, "opus")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], [sys.executable, "-m", "yt_dlp"])
        self.assertIn("--embed-thumbnail", cmd)
        self.assertEqual(cmd[-1], track.url)


class RuntimeDependencyTests(StateIsolatedMixin, unittest.TestCase):
    def test_reports_missing_python_modules_and_ffmpeg(self):
        with patch.object(yt_sync.shutil, "which", return_value=None):
            missing = yt_sync.missing_runtime_dependencies(
                modules=("textual", "yt_sync_missing_module")
            )
        self.assertEqual(missing, ["yt_sync_missing_module", "ffmpeg"])
        self.assertNotIn("textual", missing)

    def test_dependency_error_points_at_install_script(self):
        message = yt_sync.format_dependency_error(["mutagen", "ffmpeg"])
        self.assertIn("mutagen", message)
        self.assertIn("ffmpeg", message)
        self.assertIn("./install.sh", message)

    def test_require_runtime_dependencies_exits_when_anything_is_missing(self):
        stderr = io.StringIO()
        with patch.object(yt_sync, "missing_runtime_dependencies", return_value=["mutagen"]):
            with patch.object(sys, "stderr", stderr):
                with self.assertRaises(SystemExit) as raised:
                    yt_sync.require_runtime_dependencies()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("./install.sh", stderr.getvalue())
        self.assertIn("mutagen", stderr.getvalue())


class DirectoryPickerTests(StateIsolatedMixin, unittest.TestCase):
    def test_uses_existing_directory_as_picker_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(yt_sync.nearest_existing_directory(str(root)), root)

    def test_uses_nearest_existing_parent_for_new_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            new_path = root / "Music" / "New Playlist"
            self.assertEqual(yt_sync.nearest_existing_directory(str(new_path)), root)


class HomeScreenTests(StateIsolatedMixin, unittest.IsolatedAsyncioTestCase):
    def _app(self, cfg=None):
        cfg = cfg or {
            **DEFAULTS,
            "playlist_url": "https://music.youtube.com/playlist?list=abc",
            "music_dir": "/tmp/yt-sync-test-music",
        }
        app = YTSyncApp(cfg)
        app._do_refresh = types.MethodType(lambda self: None, app)
        return app

    async def test_prefills_url_and_destination(self):
        app = self._app()
        results = []
        async with app.run_test(size=(100, 40)) as pilot:
            app.push_screen(yt_sync.HomeScreen(app.cfg, initial=True), results.append)
            await pilot.pause()
            screen = app.screen
            self.assertEqual(
                screen.query_one("#playlist-url").value,
                app.cfg["playlist_url"],
            )
            self.assertEqual(screen.query_one("#configuration-name", Input).value, "")
            self.assertEqual(screen.query_one("#music-dir").value, app.cfg["music_dir"])
            self.assertFalse(results)

    async def test_configuration_name_is_the_first_focused_input(self):
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            app.push_screen(yt_sync.HomeScreen(app.cfg, initial=True))
            await pilot.pause()
            screen = app.screen
            self.assertEqual(
                [input_widget.id for input_widget in screen.query(Input)],
                ["configuration-name", "playlist-url", "music-dir"],
            )
            self.assertEqual(getattr(screen.focused, "id", None), "configuration-name")

    async def test_name_form_keeps_actions_visible_at_compact_terminal_size(self):
        app = self._app({**DEFAULTS, "playlist_url": ""})
        async with app.run_test(size=(80, 24)) as pilot:
            app.screen.query_one("#create-configuration", Button).press()
            await pilot.pause()
            screen = app.screen
            name = screen.query_one("#configuration-name", Input)
            start = screen.query_one("#start-sync", Button)
            cancel = screen.query_one("#cancel-home", Button)
            self.assertEqual(name.content_region.height, 1)
            self.assertLessEqual(start.region.y + start.region.height, 24)
            self.assertLessEqual(cancel.region.y + cancel.region.height, 24)

    async def test_invalid_submission_stays_on_home_with_error(self):
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            app.push_screen(yt_sync.HomeScreen(app.cfg, initial=True))
            await pilot.pause()
            screen = app.screen
            screen.query_one("#configuration-name", Input).value = "Library"
            screen.query_one("#playlist-url").value = "not-a-url"
            screen.query_one("#start-sync").press()
            await pilot.pause()
            self.assertIs(screen, app.screen)
            self.assertIn("valid", str(screen.query_one("#home-error").render()))

    async def test_valid_submission_creates_folder_and_returns_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "Music" / "New Playlist"
            cfg = {
                **DEFAULTS,
                "name": "New playlist",
                "playlist_url": "https://example.test/list",
                "music_dir": "/tmp",
            }
            app = self._app(cfg)
            results = []
            async with app.run_test(size=(100, 40)) as pilot:
                app.push_screen(yt_sync.HomeScreen(app.cfg, initial=True), results.append)
                await pilot.pause()
                screen = app.screen
                screen.query_one("#music-dir").value = str(destination)
                screen.query_one("#start-sync").press()
                await pilot.pause()
            self.assertTrue(destination.is_dir())
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].name, "New playlist")
            self.assertEqual(results[0].music_dir, str(destination))

    async def test_browse_result_updates_destination_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "Picked"
            destination.mkdir()
            app = self._app()
            async with app.run_test(size=(100, 40)) as pilot:
                app.push_screen(yt_sync.HomeScreen(app.cfg, initial=True))
                await pilot.pause()
                home = app.screen
                home.query_one("#browse-folder").press()
                await pilot.pause()
                self.assertIsInstance(app.screen, yt_sync.DirectoryPickerScreen)
                app.screen.dismiss(destination)
                await pilot.pause()
                self.assertEqual(home.query_one("#music-dir").value, str(destination))

    async def test_escape_returns_none_without_mutating_config(self):
        app = self._app()
        original = dict(app.cfg)
        results = []
        async with app.run_test(size=(100, 40)) as pilot:
            app.push_screen(yt_sync.HomeScreen(app.cfg, initial=False), results.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        self.assertEqual(results, [None])
        self.assertEqual(app.cfg, original)


class ConfigurationHomeScreenTests(StateIsolatedMixin, unittest.IsolatedAsyncioTestCase):
    async def test_configuration_home_keeps_actions_visible_at_compact_terminal_size(self):
        saved = {
            "name": "Compact collection",
            "playlist_url": "https://music.youtube.com/playlist?list=compact",
            "music_dir": "/tmp/compact",
        }
        app = YTSyncApp({**DEFAULTS, **saved, "saved_configs": [saved]})
        app._do_refresh = lambda: None

        async with app.run_test(size=(80, 24)):
            create = app.screen.query_one("#create-configuration", Button)
            cancel = app.screen.query_one("#cancel-home", Button)
            self.assertEqual(create.content_region.height, 3)
            self.assertEqual(cancel.content_region.height, 3)
            self.assertLessEqual(create.region.y + create.region.height, 24)
            self.assertLessEqual(cancel.region.y + cancel.region.height, 24)

    async def test_configuration_home_buttons_carry_keybind_hints(self):
        saved = {
            "name": "Library",
            "playlist_url": "https://example.test/list",
            "music_dir": "/tmp/library",
        }
        cfg = {
            **DEFAULTS,
            "playlist_url": saved["playlist_url"],
            "music_dir": saved["music_dir"],
            "saved_configs": [saved],
        }
        app = YTSyncApp(cfg)
        app._do_refresh = lambda: None

        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 40)) as pilot:
                home = app.screen
                self.assertIsInstance(home, yt_sync.ConfigurationHomeScreen)
                self.assertIn("[n]", str(home.query_one("#create-configuration", Button).label))
                self.assertIn("[e]", str(home.query_one("#edit-configuration", Button).label))
                self.assertIn("[q]", str(home.query_one("#cancel-home", Button).label))

                await pilot.press("enter")
                await pilot.pause()
                dashboard = app.screen
                await pilot.press("h")
                await pilot.pause()
                self.assertIsInstance(app.screen, yt_sync.ConfigurationHomeScreen)
                self.assertIn("[esc]", str(app.screen.query_one("#cancel-home", Button).label))
                self.assertIsNot(app.screen, dashboard)

    async def test_configuration_home_hint_line_lists_every_shortcut(self):
        saved = {
            "name": "Library",
            "playlist_url": "https://example.test/list",
            "music_dir": "/tmp/library",
        }
        app = YTSyncApp(
            {
                **DEFAULTS,
                "playlist_url": saved["playlist_url"],
                "music_dir": saved["music_dir"],
                "saved_configs": [saved],
            }
        )
        app._do_refresh = lambda: None

        async with app.run_test(size=(100, 40)):
            hint = app.screen.query_one("#config-home-hint", Static)
            text = str(hint.render())
            for token in ("j", "enter", "e", "n", "q"):
                self.assertIn(token, text)
            # height: 1 + border-box must leave one visible content line
            self.assertEqual(hint.content_region.height, 1)
            self.assertIn("move", app.export_screenshot())

    async def test_j_and_k_move_the_saved_configuration_selection(self):
        first = {
            "name": "First",
            "playlist_url": "https://example.test/first",
            "music_dir": "/tmp/first",
        }
        second = {
            "name": "Second",
            "playlist_url": "https://example.test/second",
            "music_dir": "/tmp/second",
        }
        app = YTSyncApp(
            {
                **DEFAULTS,
                "playlist_url": first["playlist_url"],
                "music_dir": first["music_dir"],
                "saved_configs": [first, second],
            }
        )
        app._do_refresh = lambda: None

        async with app.run_test(size=(100, 40)) as pilot:
            home = app.screen
            saved_list = home.query_one("#saved-config-list", ListView)
            dashboard_table = app.query_one("#table", DataTable)
            dashboard_table.add_row("  1", "stale row", "✔", key="1")
            dashboard_table.move_cursor(row=0)
            self.assertEqual(saved_list.index, 0)

            await pilot.press("j")
            await pilot.pause()
            self.assertEqual(saved_list.index, 1)
            self.assertEqual(getattr(home.focused, "id", None), "saved-config-list")
            self.assertEqual(dashboard_table.cursor_row, 0)

            await pilot.press("k")
            await pilot.pause()
            self.assertEqual(saved_list.index, 0)
            self.assertEqual(dashboard_table.cursor_row, 0)

    async def test_n_opens_the_create_configuration_form(self):
        saved = {
            "name": "Library",
            "playlist_url": "https://example.test/list",
            "music_dir": "/tmp/library",
        }
        app = YTSyncApp(
            {
                **DEFAULTS,
                "playlist_url": saved["playlist_url"],
                "music_dir": saved["music_dir"],
                "saved_configs": [saved],
            }
        )
        app._do_refresh = lambda: None

        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("n")
            await pilot.pause()
            self.assertIsInstance(app.screen, yt_sync.HomeScreen)
            self.assertEqual(getattr(app.screen.focused, "id", None), "configuration-name")

    async def test_q_closes_the_configuration_home_without_changing_config(self):
        active = {
            "name": "Library",
            "playlist_url": "https://example.test/list",
            "music_dir": "/tmp",
        }
        cfg = {
            **DEFAULTS,
            "playlist_url": active["playlist_url"],
            "music_dir": active["music_dir"],
            "saved_configs": [active],
        }
        app = YTSyncApp(cfg)
        app._do_refresh = lambda: None
        expected = {**cfg, "saved_configs": list(cfg["saved_configs"])}

        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.press("enter")
                await pilot.pause()
                dashboard = app.screen
                await pilot.press("h")
                await pilot.pause()
                self.assertIsInstance(app.screen, yt_sync.ConfigurationHomeScreen)
                await pilot.press("q")
                await pilot.pause()
                self.assertIs(app.screen, dashboard)
        self.assertEqual(app.cfg, expected)

    async def test_saved_configuration_name_is_the_visible_identity(self):
        saved = {
            "name": "Gym mix",
            "playlist_url": "https://music.youtube.com/playlist?list=gym",
            "music_dir": "/tmp/gym",
        }
        app = YTSyncApp({**DEFAULTS, **saved, "saved_configs": [saved]})
        app._do_refresh = lambda: None

        async with app.run_test(size=(100, 40)):
            title = app.screen.query_one("#saved-config-0 .saved-config-title", Static)
            url = app.screen.query_one("#saved-config-0 .saved-config-url", Static)
            directory = app.screen.query_one("#saved-config-0 .saved-config-dir", Static)
            self.assertIn("Gym mix", str(title.render()))
            self.assertTrue(str(title.render()).startswith("●"))
            self.assertIn(saved["playlist_url"], str(url.render()))
            self.assertEqual(str(directory.render()), saved["music_dir"])
            # Title is big header: bold and takes vertical space (height 2) while url/dir are 1 each
            self.assertEqual(title.content_region.height, 2)

    async def test_active_marker_matches_the_active_url_and_directory(self):
        first = {
            "name": "First collection",
            "playlist_url": "https://music.youtube.com/playlist?list=first",
            "music_dir": "/tmp/first",
        }
        second = {
            "name": "Second collection",
            "playlist_url": "https://music.youtube.com/playlist?list=second",
            "music_dir": "/tmp/second",
        }
        app = YTSyncApp(
            {
                **DEFAULTS,
                "playlist_url": second["playlist_url"],
                "music_dir": second["music_dir"],
                "saved_configs": [first, second],
            }
        )
        app._do_refresh = lambda: None

        async with app.run_test(size=(100, 40)):
            first_title = app.screen.query_one("#saved-config-0 .saved-config-title", Static)
            second_title = app.screen.query_one("#saved-config-1 .saved-config-title", Static)
            self.assertFalse(str(first_title.render()).startswith("●"))
            self.assertTrue(str(second_title.render()).startswith("●"))
            self.assertIn("Second collection", str(second_title.render()))

    async def test_keyboard_selection_activates_a_saved_configuration(self):
        first = {
            "name": "First collection",
            "playlist_url": "https://music.youtube.com/playlist?list=first",
            "music_dir": "/tmp/first",
        }
        second = {
            "name": "Second collection",
            "playlist_url": "https://music.youtube.com/playlist?list=second",
            "music_dir": "/tmp/second",
        }
        cfg = {
            **DEFAULTS,
            "playlist_url": first["playlist_url"],
            "music_dir": first["music_dir"],
            "saved_configs": [first, second],
        }
        app = YTSyncApp(cfg)
        refresh_calls = []
        app._do_refresh = lambda: refresh_calls.append(True)

        with patch("yt_sync.save_config") as save:
            async with app.run_test(size=(100, 40)) as pilot:
                self.assertIsInstance(app.screen, yt_sync.ConfigurationHomeScreen)
                self.assertEqual(len(list(app.screen.query("ListItem"))), 2)
                self.assertEqual(getattr(app.screen.focused, "id", None), "saved-config-list")
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()

        self.assertEqual(app.cfg["playlist_url"], second["playlist_url"])
        self.assertEqual(app.cfg["music_dir"], second["music_dir"])
        self.assertEqual(app.cfg["saved_configs"], [first, second])
        save.assert_called_once_with(app.cfg)
        self.assertEqual(refresh_calls, [True])

    async def test_saved_config_row_keeps_url_and_directory_on_two_lines(self):
        active = {
            "name": "My collection",
            "playlist_url": "https://music.youtube.com/playlist?list=PLIIlrAvOGW9yv-Y8jI5tHOSMaywVTuRGA",
            "music_dir": "/home/tea/Music/Youtube Music",
        }
        app = YTSyncApp({**DEFAULTS, **active, "saved_configs": [active]})
        app._do_refresh = lambda: None

        async with app.run_test(size=(80, 24)):
            title = app.screen.query_one("#saved-config-0 .saved-config-title", Static)
            url = app.screen.query_one("#saved-config-0 .saved-config-url", Static)
            directory = app.screen.query_one("#saved-config-0 .saved-config-dir", Static)
            self.assertEqual(title.content_region.height, 2)
            self.assertEqual(url.content_region.height, 1)
            self.assertEqual(directory.content_region.height, 1)
            self.assertEqual(directory.region.y, url.region.y + 1)
            # Title is big header on left, url/dir stacked on right
            self.assertLess(title.region.x, url.region.x)
            self.assertEqual(url.region.x, directory.region.x)

    async def test_create_new_configuration_saves_and_activates_the_new_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            created = {
                "name": "Created collection",
                "playlist_url": "https://music.youtube.com/playlist?list=created",
                "music_dir": str(Path(tmp) / "Created Music"),
            }
            cfg = {**DEFAULTS, "playlist_url": "", "saved_configs": []}
            app = YTSyncApp(cfg)
            refresh_calls = []
            app._do_refresh = lambda: refresh_calls.append(True)

            with patch("yt_sync.save_config") as save:
                async with app.run_test(size=(100, 40)) as pilot:
                    app.screen.query_one("#create-configuration").press()
                    await pilot.pause()
                    self.assertIsInstance(app.screen, yt_sync.HomeScreen)
                    self.assertEqual(app.screen.query_one("#playlist-url").value, "")
                    app.screen.query_one("#configuration-name", Input).value = created["name"]
                    app.screen.query_one("#playlist-url").value = created["playlist_url"]
                    app.screen.query_one("#music-dir").value = created["music_dir"]
                    app.screen.query_one("#start-sync").press()
                    await pilot.pause()
                    await pilot.pause()

            self.assertTrue(Path(created["music_dir"]).is_dir())
        self.assertEqual(app.cfg["playlist_url"], created["playlist_url"])
        self.assertEqual(app.cfg["music_dir"], created["music_dir"])
        self.assertEqual(app.cfg["saved_configs"], [created])
        save.assert_called_once_with(app.cfg)
        self.assertEqual(refresh_calls, [True])

    async def test_edit_configuration_is_keyboard_reachable_and_prefills_form(self):
        saved = {
            "name": "Gym mix",
            "playlist_url": "https://music.youtube.com/playlist?list=gym",
            "music_dir": "/tmp/gym",
        }
        app = YTSyncApp({**DEFAULTS, **saved, "saved_configs": [saved]})
        app._do_refresh = lambda: None

        async with app.run_test(size=(100, 40)) as pilot:
            self.assertIsInstance(app.screen, yt_sync.ConfigurationHomeScreen)
            # Edit button exists and is not disabled when a config is present
            edit = app.screen.query_one("#edit-configuration", Button)
            self.assertFalse(edit.disabled)
            # Focus list, press 'e' to edit
            app.screen.query_one("#saved-config-list", ListView).focus()
            await pilot.press("e")
            await pilot.pause()
            self.assertIsInstance(app.screen, yt_sync.HomeScreen)
            self.assertEqual(app.screen.query_one("#configuration-name", Input).value, saved["name"])
            self.assertEqual(app.screen.query_one("#playlist-url", Input).value, saved["playlist_url"])
            self.assertEqual(app.screen.query_one("#music-dir", Input).value, saved["music_dir"])
            # Edit form shows Save action, not Create semantics
            self.assertIn("Save", str(app.screen.query_one("#start-sync", Button).label))

    async def test_edit_configuration_updates_saved_entry_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = {
                "name": "Gym mix",
                "playlist_url": "https://music.youtube.com/playlist?list=gym",
                "music_dir": str(Path(tmp) / "gym"),
            }
            cfg = {**DEFAULTS, **original, "saved_configs": [original]}
            app = YTSyncApp(cfg)
            app._do_refresh = lambda: None

            updated_dir = str(Path(tmp) / "gym-edited")
            with patch("yt_sync.save_config") as save:
                async with app.run_test(size=(100, 40)) as pilot:
                    app.screen.query_one("#edit-configuration", Button).press()
                    await pilot.pause()
                    self.assertIsInstance(app.screen, yt_sync.HomeScreen)
                    app.screen.query_one("#configuration-name", Input).value = "Gym edited"
                    app.screen.query_one("#music-dir", Input).value = updated_dir
                    app.screen.query_one("#start-sync", Button).press()
                    await pilot.pause()
                    await pilot.pause()
                    # Still on configuration home after save (edit stays in chooser)
                    self.assertIsInstance(app.screen, yt_sync.ConfigurationHomeScreen)
                    title = app.screen.query_one("#saved-config-0 .saved-config-title", Static)
                    self.assertIn("Gym edited", str(title.render()))
                    directory = app.screen.query_one("#saved-config-0 .saved-config-dir", Static)
                    self.assertEqual(str(directory.render()), updated_dir)

            self.assertEqual(app.cfg["saved_configs"][0]["name"], "Gym edited")
            self.assertEqual(app.cfg["saved_configs"][0]["music_dir"], updated_dir)
            # Active config was edited, so active follows
            self.assertEqual(app.cfg["playlist_url"], original["playlist_url"])
            self.assertEqual(app.cfg["music_dir"], updated_dir)
            save.assert_called_once()
            self.assertTrue(Path(updated_dir).is_dir())

    async def test_edit_duplicate_url_and_directory_is_rejected(self):
        first = {
            "name": "First",
            "playlist_url": "https://music.youtube.com/playlist?list=first",
            "music_dir": "/tmp/first",
        }
        second = {
            "name": "Second",
            "playlist_url": "https://music.youtube.com/playlist?list=second",
            "music_dir": "/tmp/second",
        }
        cfg = {**DEFAULTS, **first, "saved_configs": [first, second]}
        app = YTSyncApp(cfg)
        app._do_refresh = lambda: None

        with patch("yt_sync.save_config") as save:
            async with app.run_test(size=(100, 40)) as pilot:
                # Highlight second item
                await pilot.press("down")
                await pilot.pause()
                app.screen.query_one("#edit-configuration", Button).press()
                await pilot.pause()
                self.assertIsInstance(app.screen, yt_sync.HomeScreen)
                # Try to make second collide with first
                app.screen.query_one("#playlist-url", Input).value = first["playlist_url"]
                app.screen.query_one("#music-dir", Input).value = first["music_dir"]
                app.screen.query_one("#start-sync", Button).press()
                await pilot.pause()
                # Should stay on edit form with validation error
                self.assertIsInstance(app.screen, yt_sync.HomeScreen)
                self.assertIn("already exists", str(app.screen.query_one("#home-error", Static).render()).lower())
                save.assert_not_called()

    async def test_edit_cancel_keeps_original_configuration(self):
        saved = {
            "name": "Gym mix",
            "playlist_url": "https://music.youtube.com/playlist?list=gym",
            "music_dir": "/tmp/gym",
        }
        cfg = {**DEFAULTS, **saved, "saved_configs": [saved]}
        app = YTSyncApp(cfg)
        app._do_refresh = lambda: None

        with patch("yt_sync.save_config") as save:
            async with app.run_test(size=(100, 40)) as pilot:
                app.screen.query_one("#edit-configuration", Button).press()
                await pilot.pause()
                self.assertIsInstance(app.screen, yt_sync.HomeScreen)
                app.screen.query_one("#configuration-name", Input).value = "Changed"
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, yt_sync.ConfigurationHomeScreen)
                title = app.screen.query_one("#saved-config-0 .saved-config-title", Static)
                self.assertIn("Gym mix", str(title.render()))
                self.assertNotIn("Changed", str(title.render()))

        save.assert_not_called()
        self.assertEqual(app.cfg["saved_configs"][0]["name"], "Gym mix")


class StartupFlowTests(StateIsolatedMixin, unittest.IsolatedAsyncioTestCase):
    def _app(self, cfg):
        app = YTSyncApp(cfg)
        app._do_refresh = lambda: None
        return app

    async def test_empty_url_mounts_configuration_home_without_fetching(self):
        app = self._app({**DEFAULTS, "playlist_url": ""})
        with patch("yt_sync.fetch_playlist") as fetch:
            async with app.run_test(size=(100, 40)):
                self.assertIsInstance(app.screen, yt_sync.ConfigurationHomeScreen)
                self.assertIn("No saved", str(app.screen.query_one("#saved-config-empty").render()))
                self.assertEqual(getattr(app.screen.focused, "id", None), "create-configuration")
                fetch.assert_not_called()

    async def test_create_configuration_updates_config_saves_and_refreshes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = str(Path(tmp) / "Music")
            cfg = {
                **DEFAULTS,
                "playlist_url": "",
                "music_dir": str(Path(tmp) / "old"),
            }
            app = self._app(cfg)
            refresh_calls = []
            app._do_refresh = lambda: refresh_calls.append(True)
            with patch("yt_sync.save_config") as save:
                async with app.run_test(size=(100, 40)) as pilot:
                    app.screen.query_one("#create-configuration", Button).press()
                    await pilot.pause()
                    screen = app.screen
                    screen.query_one("#configuration-name", Input).value = "Library"
                    screen.query_one("#playlist-url", Input).value = "https://example.test/list"
                    screen.query_one("#music-dir", Input).value = destination
                    screen.query_one("#start-sync", Button).press()
                    await pilot.pause()
                    await pilot.pause()
            self.assertEqual(app.cfg["playlist_url"], "https://example.test/list")
            self.assertEqual(app.cfg["music_dir"], destination)
            self.assertEqual(
                app.cfg["saved_configs"],
                [
                    {
                        "name": "Library",
                        "playlist_url": "https://example.test/list",
                        "music_dir": destination,
                    }
                ],
            )
            save.assert_called_once_with(app.cfg)
            self.assertEqual(refresh_calls, [True])

    async def test_initial_escape_keeps_configuration_home_open(self):
        app = self._app({**DEFAULTS, "playlist_url": ""})
        with patch.object(app, "exit") as exit_app:
            async with app.run_test(size=(100, 40)) as pilot:
                home = app.screen
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(home, app.screen)
                self.assertIn("Choose a configuration", str(home.query_one("#config-home-error").render()))
                self.assertEqual(getattr(home.focused, "id", None), "create-configuration")
        exit_app.assert_not_called()

    async def test_initial_quit_requests_exit(self):
        app = self._app({**DEFAULTS, "playlist_url": ""})
        with patch.object(app, "exit") as exit_app:
            async with app.run_test(size=(100, 40)) as pilot:
                app.screen.query_one("#cancel-home", Button).press()
                await pilot.pause()
        exit_app.assert_called_once_with()

    async def test_initial_quit_is_keyboard_reachable(self):
        app = self._app({**DEFAULTS, "playlist_url": ""})
        with patch.object(app, "exit") as exit_app:
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.press("tab")
                self.assertEqual(getattr(app.screen.focused, "id", None), "cancel-home")
                await pilot.press("enter")
                await pilot.pause()
        exit_app.assert_called_once_with()

    async def test_h_opens_configuration_home_and_cancel_keeps_dashboard_config(self):
        active = {
            "name": "Library",
            "playlist_url": "https://example.test/list",
            "music_dir": "/tmp",
        }
        cfg = {
            **DEFAULTS,
            "playlist_url": active["playlist_url"],
            "music_dir": active["music_dir"],
            "saved_configs": [active],
        }
        app = self._app(cfg)
        expected = {**cfg, "saved_configs": list(cfg["saved_configs"])}
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 40)) as pilot:
                self.assertIsInstance(app.screen, yt_sync.ConfigurationHomeScreen)
                await pilot.press("enter")
                await pilot.pause()
                dashboard = app.screen
                await pilot.press("h")
                await pilot.pause()
                self.assertIsInstance(app.screen, yt_sync.ConfigurationHomeScreen)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, dashboard)
        self.assertEqual(app.cfg, expected)

    def test_h_is_rejected_while_busy(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "https://example.test/list"})
        app._busy = True
        with patch.object(app, "notify") as notify:
            app.action_open_home()
        notify.assert_called_once_with("⏳ Already working…", severity="warning", timeout=2)


class VisualContractTests(StateIsolatedMixin, unittest.TestCase):
    def test_setup_and_configuration_home_have_explicit_interaction_states(self):
        self.assertIn("#home-card", yt_sync.HomeScreen.DEFAULT_CSS)
        self.assertIn("Input:focus", yt_sync.HomeScreen.DEFAULT_CSS)
        self.assertIn("Button.-style-default:hover", yt_sync.HomeScreen.DEFAULT_CSS)
        self.assertIn("Button.-style-default:disabled", yt_sync.HomeScreen.DEFAULT_CSS)
        self.assertIn("Button.-style-default", yt_sync.HomeScreen.DEFAULT_CSS)
        self.assertIn("border: none !important", yt_sync.HomeScreen.DEFAULT_CSS)
        self.assertIn("#config-home-card", yt_sync.ConfigurationHomeScreen.DEFAULT_CSS)
        self.assertIn("#saved-config-list", yt_sync.ConfigurationHomeScreen.DEFAULT_CSS)
        self.assertIn("ListItem.-highlight", yt_sync.ConfigurationHomeScreen.DEFAULT_CSS)
        self.assertIn("Button.-style-default:hover", yt_sync.ConfigurationHomeScreen.DEFAULT_CSS)
        self.assertIn("Button.-style-default:disabled", yt_sync.ConfigurationHomeScreen.DEFAULT_CSS)
        self.assertIn("#loader.visible", YTSyncApp.CSS)
        self.assertIn("#app-header", YTSyncApp.CSS)
        self.assertIn("#filter-hint", YTSyncApp.CSS)
        self.assertIn("DataTable > .datatable--cursor", YTSyncApp.CSS)
        self.assertIn("DataTable:focus", YTSyncApp.CSS)
        self.assertIn("border: round", YTSyncApp.CSS)
        self.assertIn("#console", YTSyncApp.CSS)


class VisualRuntimeTests(StateIsolatedMixin, unittest.IsolatedAsyncioTestCase):
    async def test_primary_buttons_have_their_fill_before_hover(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "https://example.test/list"})
        app._do_refresh = lambda: None
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 40)) as pilot:
                create_button = app.screen.query_one("#create-configuration", Button)
                self.assertEqual(create_button.styles.background, Color.parse(yt_sync.C_BLUE))
                self.assertEqual(create_button.visual_style.background, Color.parse(yt_sync.C_BLUE))
                create_button.press()
                await pilot.pause()
                start_button = app.screen.query_one("#start-sync", Button)
                self.assertEqual(start_button.styles.background, Color.parse(yt_sync.C_BLUE))
                self.assertEqual(start_button.visual_style.background, Color.parse(yt_sync.C_BLUE))

    async def test_dashboard_has_explicit_header_and_persistent_filter_hint(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "https://example.test/list"})
        app._do_refresh = lambda: None
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 40)) as pilot:
                app.screen.dismiss(
                    yt_sync.SetupValues("Library", "https://example.test/list", "/tmp")
                )
                await pilot.pause()
                header = app.query_one("#app-header")
                hint = app.query_one("#filter-hint")
                self.assertIn("yt-sync", str(header.render()))
                self.assertEqual(header.visual_style.background, Color.parse(yt_sync.C_BG_DARK))
                self.assertEqual(hint.visual_style.background, Color.parse(yt_sync.C_BG_DARK))
                self.assertEqual(header.content_region.height, 1)
                self.assertEqual(hint.content_region.height, 1)
                self.assertEqual(str(hint.render()), "1 All · 2 Missing · 3 Orphans · 4 Failed")

    async def test_picker_primary_button_has_its_fill_before_hover(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "https://example.test/list"})
        app._do_refresh = lambda: None
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 40)) as pilot:
                app.push_screen(yt_sync.DirectoryPickerScreen("/tmp"))
                await pilot.pause()
                button = app.screen.query_one("#picker-choose")
                self.assertEqual(button.visual_style.background, Color.parse(yt_sync.C_BLUE))

    async def test_configuration_and_setup_buttons_do_not_keep_textual_default_chrome(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "https://example.test/list"})
        app._do_refresh = lambda: None
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 40)) as pilot:
                for button_id in ("create-configuration", "cancel-home"):
                    button = app.screen.query_one(f"#{button_id}", Button)
                    self.assertNotEqual(str(button.styles.border.top[0]).lower(), "tall")
                    self.assertNotEqual(str(button.styles.border.bottom[0]).lower(), "tall")
                app.screen.query_one("#create-configuration", Button).press()
                await pilot.pause()
                for button_id in ("browse-folder", "start-sync", "cancel-home"):
                    button = app.screen.query_one(f"#{button_id}", Button)
                    self.assertNotEqual(str(button.styles.border.top[0]).lower(), "tall")
                    self.assertNotEqual(str(button.styles.border.bottom[0]).lower(), "tall")

    async def test_saved_configuration_list_does_not_change_width_on_focus(self):
        active = {"playlist_url": "https://example.test/list", "music_dir": "/tmp"}
        app = YTSyncApp({**DEFAULTS, **active, "saved_configs": [active]})
        app._do_refresh = lambda: None
        async with app.run_test(size=(100, 40)) as pilot:
            saved_list = app.screen.query_one("#saved-config-list", ListView)
            app.screen.query_one("#create-configuration", Button).focus()
            await pilot.pause()
            before = saved_list.content_region.width
            saved_list.focus()
            await pilot.pause()
            self.assertEqual(saved_list.content_region.width, before)

    async def test_list_border_does_not_change_content_width_on_focus(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "https://example.test/list"})
        app._do_refresh = lambda: None
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 40)) as pilot:
                app.screen.dismiss(
                    yt_sync.SetupValues("Library", "https://example.test/list", "/tmp")
                )
                await pilot.pause()
                table = app.query_one("#table")
                app.query_one("#console").focus()
                await pilot.pause()
                before = table.content_region.width
                table.focus()
                await pilot.pause()
                self.assertEqual(table.content_region.width, before)


class MainModeTests(StateIsolatedMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # main() checks runtime deps first; these tests cover dispatch only, so
        # do not depend on mutagen/ffmpeg being installed in the test runner.
        patcher = patch("yt_sync.require_runtime_dependencies")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_mode_without_url_keeps_existing_error(self):
        cfg = {**DEFAULTS, "playlist_url": ""}
        with patch("yt_sync.load_config", return_value=cfg):
            with patch("yt_sync.CONFIG_PATH", Path("/tmp/config.toml")):
                with patch("yt_sync.sys.argv", ["yt-sync", "--diff-only"]):
                    with self.assertRaises(SystemExit) as raised:
                        yt_sync.main()
        self.assertEqual(raised.exception.code, 1)

    def test_interactive_mode_allows_empty_url(self):
        cfg = {**DEFAULTS, "playlist_url": ""}
        with patch("yt_sync.load_config", return_value=cfg):
            with patch("yt_sync.sys.argv", ["yt-sync"]):
                with patch("yt_sync.YTSyncApp") as app_class:
                    yt_sync.main()
        app_class.assert_called_once_with(cfg)
        app_class.return_value.run.assert_called_once_with()


class ResolveDownloadTests(StateIsolatedMixin, unittest.TestCase):
    def test_empty_selection_downloads_all_missing(self):
        row_map, missing, failed, _orphans, _synced, _m1, _m2, _f1, _orphan = _sample()
        got = resolve_download_targets(set(), row_map, missing, failed, "all")
        self.assertEqual([t.id for t in got], ["m1", "m2"])

    def test_empty_selection_on_failed_filter_retries_all_failed(self):
        row_map, missing, failed, _orphans, _synced, _m1, _m2, _f1, _orphan = _sample()
        got = resolve_download_targets(set(), row_map, missing, failed, "failed")
        self.assertEqual([t.id for t in got], ["f1"])

    def test_selected_missing_downloads_only_those(self):
        row_map, missing, failed, _orphans, _synced, m1, _m2, _f1, _orphan = _sample()
        got = resolve_download_targets({"2"}, row_map, missing, failed, "all")
        self.assertEqual(got, [m1])

    def test_selected_matched_only_does_not_fall_back_to_all(self):
        row_map, missing, failed, _orphans, _synced, _m1, _m2, _f1, _orphan = _sample()
        got = resolve_download_targets({"1"}, row_map, missing, failed, "all")
        self.assertEqual(got, [])

    def test_mixed_selection_keeps_only_downloadable(self):
        row_map, missing, failed, _orphans, _synced, m1, m2, f1, _orphan = _sample()
        got = resolve_download_targets({"1", "2", "4", "5"}, row_map, missing, failed, "all")
        self.assertEqual(got, [m1, f1])
        self.assertNotIn(m2, got)

    def test_selected_failed_downloads_that_track(self):
        row_map, missing, failed, _orphans, _synced, _m1, _m2, f1, _orphan = _sample()
        got = resolve_download_targets({"4"}, row_map, missing, failed, "all")
        self.assertEqual(got, [f1])


class ResolveTrashTests(StateIsolatedMixin, unittest.TestCase):
    def test_empty_selection_trashes_all_orphans(self):
        row_map, _m, _f, orphans, _synced, _m1, _m2, _f1, _orphan = _sample()
        got = resolve_trash_targets(set(), row_map, orphans)
        self.assertEqual(got, orphans)

    def test_selected_orphan_only_that_file(self):
        row_map, _m, _f, orphans, _synced, _m1, _m2, _f1, orphan = _sample()
        got = resolve_trash_targets({"5"}, row_map, orphans)
        self.assertEqual(got, [orphan])

    def test_selected_non_orphan_does_not_fall_back_to_all(self):
        row_map, _m, _f, orphans, _synced, _m1, _m2, _f1, _orphan = _sample()
        got = resolve_trash_targets({"2"}, row_map, orphans)
        self.assertEqual(got, [])

    def test_orphan_resolution_ignores_missing_and_failed_counts(self):
        """Old _orphan_key added len(missing) even in the orphans filter."""
        o1 = LocalFile(Path("/tmp/o1.opus"), "O1")
        o2 = LocalFile(Path("/tmp/o2.opus"), "O2")
        row_map = {
            "1": RowEntry("O1", "✗ orphan", "orphan", local=o1),
            "2": RowEntry("O2", "✗ orphan", "orphan", local=o2),
        }
        # Plenty of missing/failed exist in the diff, but they are not in this view.
        got = resolve_trash_targets({"1"}, row_map, [o1, o2])
        self.assertEqual(got, [o1])


def _stub_refresh(matched, missing, failed=None, orphans=None):
    failed = failed or []
    orphans = orphans or []

    def _do_refresh(self):
        self.diff_result = DiffResult(
            matched=matched, missing=missing, orphans=orphans, failed=failed
        )
        self._populate_table()

    return _do_refresh


class ActionPipelineTests(StateIsolatedMixin, unittest.IsolatedAsyncioTestCase):
    async def test_single_download_reconciles_without_refetching_playlist(self):
        track = Track("id1", "Artist - Song", "http://x/1")
        state = {"downloaded": False}

        def fake_scan(_music_dir):
            if not state["downloaded"]:
                return []
            return [LocalFile(Path("/tmp/Artist - Song.opus"), "Artist - Song")]

        def fake_download(_track, _music_dir, _fmt):
            state["downloaded"] = True
            return yt_sync.DownloadResult(success=True)

        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        with (
            patch("yt_sync.save_config"),
            patch("yt_sync.load_failed", return_value={}),
            patch("yt_sync.fetch_playlist", return_value=[track]) as fetch,
            patch("yt_sync.scan_local", side_effect=fake_scan),
            patch("yt_sync.download_track", side_effect=fake_download),
        ):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(app.query_one("#table").row_count, 1)

                app.action_act_on_current()
                await app.workers.wait_for_complete()
                await pilot.pause()

        self.assertEqual(
            fetch.call_count, 1, "acting on a row must not refetch the playlist"
        )
        self.assertEqual([t.id for t, _f in app.diff_result.matched], ["id1"])
        self.assertEqual(app.diff_result.missing, [])
        self.assertFalse(app._busy)

    async def test_escape_stops_a_batch_after_the_current_track(self):
        tracks = [Track(f"id{i}", f"Artist - Song {i}", f"http://x/{i}") for i in range(5)]
        calls = []

        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})

        def fake_download(track, _music_dir, _fmt):
            calls.append(track.id)
            app._cancel_requested = True
            return yt_sync.DownloadResult(success=True)

        with (
            patch("yt_sync.save_config"),
            patch("yt_sync.load_failed", return_value={}),
            patch("yt_sync.fetch_playlist", return_value=tracks),
            patch("yt_sync.scan_local", return_value=[]),
            patch("yt_sync.download_track", side_effect=fake_download),
        ):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(len(app.diff_result.missing), 5)

                app.action_download()
                await app.workers.wait_for_complete()
                await pilot.pause()

        self.assertEqual(calls, ["id0"], "cancel is checked between tracks")
        self.assertFalse(app._busy)
        self.assertFalse(app._batch_active)

    async def test_dashboard_top_rows_do_not_overlap(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = lambda: None
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                header = app.query_one("#app-header")
                status = app.query_one("#status-bar")
                slot = app.query_one("#loader-slot")
                table = app.query_one("#table")

                self.assertEqual(header.region.y, 0)
                self.assertEqual(status.region.y, header.region.y + header.region.height)
                self.assertEqual(slot.region.y, status.region.y + status.region.height)
                self.assertEqual(table.region.y, slot.region.y + slot.region.height)
                self.assertEqual(status.region.width, header.region.width)

    async def test_spinner_animation_is_idle_unless_an_operation_runs(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = lambda: None
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                loader = app.query_one("#loader")
                self.assertIsNone(loader.auto_refresh)

                app._set_loading(True, "download")
                await pilot.pause()
                self.assertEqual(loader.auto_refresh, 1 / 16)
                self.assertTrue(loader.has_class("visible"))

                app._set_loading(False)
                await pilot.pause()
                self.assertIsNone(loader.auto_refresh)
                self.assertFalse(loader.has_class("visible"))

    async def test_mouse_moves_over_the_table_do_not_restyle_it(self):
        from textual.css.stylesheet import Stylesheet

        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = lambda: None
        styled_nodes: list[str] = []
        original_apply = Stylesheet.apply

        def counting_apply(stylesheet, node, *args, **kwargs):
            styled_nodes.append(getattr(node, "id", type(node).__name__))
            return original_apply(stylesheet, node, *args, **kwargs)

        with patch("yt_sync.save_config"), patch.object(Stylesheet, "apply", counting_apply):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                app.diff_result = DiffResult(
                    matched=[], missing=[Track(f"id{i}", f"Artist {i}", "u") for i in range(50)], orphans=[]
                )
                app._populate_table()
                await pilot.pause()

                await pilot.hover("#table", offset=(2, 2))
                styled_nodes.clear()
                for row in range(5):
                    await pilot.hover("#table", offset=(2, 2 + row))

        self.assertLessEqual(
            styled_nodes.count("table"),
            1,
            "a :hover style on DataTable re-applies its stylesheet on every mouse move",
        )

    async def test_loader_toggle_does_not_move_the_table(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = lambda: None
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                table = app.query_one("#table")
                loader = app.query_one("#loader")
                before = table.region

                app._set_loading(True, "download")
                await pilot.pause()
                self.assertTrue(loader.has_class("visible"))
                self.assertEqual(table.region, before)

                app._set_loading(False)
                await pilot.pause()
                self.assertFalse(loader.has_class("visible"))
                self.assertEqual(table.region, before)

    async def test_selection_survives_a_filter_switch(self):
        lf = LocalFile(Path("/tmp/a.opus"), "Artist - A")
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = types.MethodType(
            _stub_refresh(
                matched=[(Track("id1", "Artist - A", "http://x/1"), lf)],
                missing=[
                    Track("id2", "Artist - B", "http://x/2"),
                    Track("id3", "Artist - C", "http://x/3"),
                ],
            ),
            app,
        )
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                table = app.query_one("#table")
                await pilot.press("j")
                await pilot.press("space")
                self.assertEqual(app.selected, {"id2#1"})

                await pilot.press("2")
                await pilot.pause()
                self.assertEqual(app.mode, "missing")
                self.assertEqual(table.row_count, 2)
                self.assertEqual(app.selected, {"id2#1"}, "selection is not cleared")
                self.assertIn("▶", str(table.get_row_at(0)[0]))
                self.assertEqual(app._row_key_at_cursor(table), "id2#1")

                await pilot.press("1")
                await pilot.pause()
                self.assertEqual(app.mode, "all")
                self.assertEqual(app._row_key_at_cursor(table), "id2#1")
                self.assertEqual(table.cursor_row, 1)

    async def test_duplicate_playlist_entries_get_distinct_row_keys(self):
        first = Track("same", "Artist - Song", "http://x/1")
        second = Track("same", "Artist - Song", "http://x/2")
        lf = LocalFile(Path("/tmp/Artist - Song.opus"), "Artist - Song")
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = types.MethodType(
            _stub_refresh(matched=[(first, lf)], missing=[second]),
            app,
        )
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                self.assertEqual(app.query_one("#table").row_count, 2)
                self.assertEqual(set(app._row_map), {"same#1", "same#2"})

    async def test_large_row_set_commits_without_blocking_the_caller(self):
        missing = [Track(f"id{i}", f"Artist - Song {i}", f"http://x/{i}") for i in range(2500)]
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = types.MethodType(_stub_refresh(matched=[], missing=missing), app)
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                table = app.query_one("#table")
                table.move_cursor(row=1200)

                started = time.perf_counter()
                app._populate_table()
                elapsed = time.perf_counter() - started
                # Generous vs the ~15 ms local build so slow CI runners still
                # pass, while a synchronous 2500-row commit (~70 ms local) would
                # blow well past it on any machine.
                self.assertLess(elapsed, 0.25, f"filter commit blocked for {elapsed:.3f}s")

                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(table.row_count, 2500)
                self.assertEqual(table.cursor_row, 1200)

    async def test_cursor_follows_the_track_when_it_moves_to_matched(self):
        t1 = Track("id1", "Artist - A", "http://x/1")
        t2 = Track("id2", "Artist - B", "http://x/2")
        t3 = Track("id3", "Artist - C", "http://x/3")
        lf = LocalFile(Path("/tmp/Artist - B.opus"), "Artist - B")
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = types.MethodType(_stub_refresh(matched=[(t1, lf)], missing=[t2, t3]), app)
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                table = app.query_one("#table")
                table.move_cursor(row=1)
                self.assertEqual(app._row_key_at_cursor(table), "id2#1")

                # id2 finished downloading: it moves from missing to matched.
                app.diff_result = DiffResult(matched=[(t1, lf), (t2, lf)], missing=[t3], orphans=[])
                app._populate_table()
                await pilot.pause()

                self.assertEqual(app._row_key_at_cursor(table), "id2#1")
                self.assertEqual(table.cursor_row, 1)

    def test_animations_config_maps_to_animation_level(self):
        for value, expected in (
            ("none", "none"),
            ("basic", "basic"),
            ("full", "full"),
            ("nonsense", "full"),
        ):
            app = YTSyncApp({**DEFAULTS, "animations": value})
            self.assertEqual(app.animation_level, expected, value)

    async def test_status_bar_shows_batch_progress(self):
        tracks = [Track(f"id{i}", f"Artist - Song {i}", f"http://x/{i}") for i in range(3)]
        seen = []
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})

        def fake_download(track, _music_dir, _fmt):
            status = str(app.query_one("#status-bar").render())
            seen.append((app._progress, status))
            return yt_sync.DownloadResult(success=True)

        with (
            patch("yt_sync.save_config"),
            patch("yt_sync.load_failed", return_value={}),
            patch("yt_sync.fetch_playlist", return_value=tracks),
            patch("yt_sync.scan_local", return_value=[]),
            patch("yt_sync.download_track", side_effect=fake_download),
        ):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await app.workers.wait_for_complete()
                await pilot.pause()
                app.action_download()
                await app.workers.wait_for_complete()
                await pilot.pause()

        self.assertEqual([progress for progress, _s in seen], ["⬇ 1/3", "⬇ 2/3", "⬇ 3/3"])
        self.assertIn("⬇ 1/3", seen[0][1])
        self.assertIsNone(app._progress)

    async def test_hidden_selection_warns_instead_of_acting(self):
        matched = Track("id1", "Artist - A", "http://x/1")
        missing = Track("id2", "Artist - B", "http://x/2")
        lf = LocalFile(Path("/tmp/Artist - A.opus"), "Artist - A")
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = types.MethodType(_stub_refresh(matched=[(matched, lf)], missing=[missing]), app)
        with patch("yt_sync.save_config"), patch("yt_sync.download_track") as download:
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                await pilot.press("space")  # select the matched row
                self.assertEqual(app.selected, {"id1#1"})
                await pilot.press("2")      # Missing filter hides that selection
                with patch.object(app, "notify") as notify:
                    await pilot.press("d")
                    await pilot.pause()
                messages = [call.args[0] for call in notify.call_args_list]
                self.assertTrue(
                    any("hidden by this filter" in message for message in messages), messages
                )
                download.assert_not_called()
                self.assertFalse(app._busy)

    async def test_stale_large_commit_cannot_wipe_a_newer_populate(self):
        missing = [Track(f"id{i}", f"Artist - Song {i}", f"http://x/{i}") for i in range(2500)]
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = types.MethodType(_stub_refresh(matched=[], missing=missing), app)
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                table = app.query_one("#table")

                app._populate_table()  # schedules the chunked 2500-row commit
                app.diff_result = DiffResult(
                    matched=[], missing=[Track("only", "Artist - Only", "http://x/only")], orphans=[]
                )
                app._populate_table()  # newer, synchronous populate wins
                await app.workers.wait_for_complete()
                await pilot.pause()

                self.assertEqual(table.row_count, 1)
                self.assertEqual(set(app._row_map), {"only#1"})

    async def test_refresh_rebuild_keeps_the_cursor_row(self):
        lf = LocalFile(Path("/tmp/a.opus"), "Artist - A")
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = types.MethodType(
            _stub_refresh(
                matched=[(Track("id1", "Artist - A", "http://x/1"), lf)],
                missing=[
                    Track(f"id{i}", f"Artist - Song {i}", f"http://x/{i}")
                    for i in range(10)
                ],
            ),
            app,
        )
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                table = app.query_one("#table")
                self.assertEqual(table.row_count, 11)
                table.move_cursor(row=6)

                app._populate_table()
                await pilot.pause()

                self.assertEqual(table.cursor_row, 6)


class DiffPerformanceTests(StateIsolatedMixin, unittest.TestCase):
    def test_unmatched_diff_stays_within_budget(self):
        playlist = [Track(f"p{i}", f"Playlist Song {i}", "http://x") for i in range(100)]
        local = [
            LocalFile(Path(f"/m/Local Tune {i}.opus"), f"Local Tune {i}")
            for i in range(1000)
        ]
        started = time.perf_counter()
        result = diff(playlist, local, {})
        elapsed = time.perf_counter() - started

        self.assertEqual(len(result.missing), 100)
        self.assertEqual(len(result.orphans), 1000)
        self.assertLess(elapsed, 2.0, f"unmatched diff took {elapsed:.2f}s")


class ViewCacheTests(StateIsolatedMixin, unittest.IsolatedAsyncioTestCase):
    def _cfg(self):
        return {
            **DEFAULTS,
            "playlist_url": "http://example",
            "music_dir": "/tmp/music",
            "saved_configs": [],
        }

    def test_view_cache_round_trips_the_rendered_diff(self):
        track = Track("id1", "Artist - A", "http://x/1")
        lf = LocalFile(Path("/tmp/Artist - A.opus"), "Artist - A")
        payload = DiffResult(
            matched=[(track, lf)],
            missing=[Track("id2", "Artist - B", "http://x/2")],
            orphans=[LocalFile(Path("/tmp/Orphan.opus"), "Orphan")],
            failed=[(Track("id3", "Artist - C", "http://x/3"), "boom")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "view-cache.json"
            with patch.object(yt_sync, "VIEW_CACHE_PATH", path):
                yt_sync.save_view_cache([track], payload, "http://example", "/tmp/music")
                loaded = yt_sync.load_view_cache("http://example", "/tmp/music")

                self.assertIsNotNone(loaded)
                saved_at, got = loaded
                self.assertGreater(saved_at, 0)
                self.assertEqual(
                    [(t.id, str(f.path)) for t, f in got.matched],
                    [("id1", "/tmp/Artist - A.opus")],
                )
                self.assertEqual([t.id for t in got.missing], ["id2"])
                self.assertEqual([str(f.path) for f in got.orphans], ["/tmp/Orphan.opus"])
                self.assertEqual([(t.id, err) for t, err in got.failed], [("id3", "boom")])
                self.assertIsNone(yt_sync.load_view_cache("other", "/tmp/music"))

    async def test_cached_view_paints_before_network_and_pauses_actions(self):
        cfg = self._cfg()
        cached = DiffResult(
            matched=[],
            missing=[Track("cached1", "Artist - Cached", "http://x/c")],
            orphans=[],
        )
        fresh = [Track("new", "Artist - New", "http://x/n")]
        fetch_calls = {"n": 0}
        gate = threading.Event()

        def fetch(_url):
            fetch_calls["n"] += 1
            if fetch_calls["n"] == 1:
                gate.wait(timeout=5)
                raise RuntimeError("offline")
            return fresh

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "view-cache.json"
            with (
                patch.object(yt_sync, "VIEW_CACHE_PATH", path),
                patch("yt_sync.save_config"),
                patch("yt_sync.load_failed", return_value={}),
                patch("yt_sync.fetch_playlist", side_effect=fetch),
                patch("yt_sync.scan_local", return_value=[]),
            ):
                yt_sync.save_view_cache(
                    cached.missing, cached, cfg["playlist_url"], cfg["music_dir"]
                )
                app = YTSyncApp(cfg)
                async with app.run_test(size=(100, 30)) as pilot:
                    app.screen.dismiss(
                        yt_sync.SetupValues("Library", cfg["playlist_url"], cfg["music_dir"])
                    )
                    await pilot.pause()
                    table = app.query_one("#table")
                    self.assertEqual(table.row_count, 1)
                    self.assertEqual(app._row_map["cached1#1"].title, "Artist - Cached")
                    self.assertTrue(app._stale)
                    self.assertIn("cached", str(app.query_one("#status-bar").render()))

                    with (
                        patch("yt_sync.download_track") as download,
                        patch.object(app, "notify") as notify,
                    ):
                        await pilot.press("d")
                        await pilot.pause()
                    download.assert_not_called()
                    self.assertTrue(
                        any(
                            "Refreshing" in call.args[0]
                            for call in notify.call_args_list
                        )
                    )

                    gate.set()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertTrue(app._stale, "a failed refresh keeps the cached view")
                    self.assertEqual(table.row_count, 1)
                    self.assertEqual(app._stale_error, "offline")
                    self.assertIn(
                        "refresh failed — r to retry",
                        str(app.query_one("#status-bar").render()),
                    )

                    with patch.object(app, "notify") as notify_failed:
                        await pilot.press("d")
                        await pilot.pause()
                    self.assertTrue(
                        any(
                            "press r to retry" in call.args[0]
                            for call in notify_failed.call_args_list
                        )
                    )

                    await pilot.press("r")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertFalse(app._stale)
                    self.assertIsNone(app._stale_error)
                    self.assertEqual(set(app._row_map), {"new#1"})

    async def test_corrupt_view_cache_is_ignored(self):
        cfg = self._cfg()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "view-cache.json"
            path.write_text("{not valid json")
            with (
                patch.object(yt_sync, "VIEW_CACHE_PATH", path),
                patch("yt_sync.save_config"),
                patch("yt_sync.load_failed", return_value={}),
                patch("yt_sync.fetch_playlist", return_value=[]),
                patch("yt_sync.scan_local", return_value=[]),
            ):
                app = YTSyncApp(cfg)
                async with app.run_test(size=(100, 30)) as pilot:
                    app.screen.dismiss(
                        yt_sync.SetupValues("Library", cfg["playlist_url"], cfg["music_dir"])
                    )
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertFalse(app._stale)
                    self.assertEqual(app.query_one("#table").row_count, 0)

    async def test_successful_refresh_replaces_the_cached_view(self):
        cfg = self._cfg()
        cached = DiffResult(
            matched=[],
            missing=[Track("old", "Artist - Old", "http://x/o")],
            orphans=[],
        )
        fresh = [Track("new", "Artist - New", "http://x/n")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "view-cache.json"
            with (
                patch.object(yt_sync, "VIEW_CACHE_PATH", path),
                patch("yt_sync.save_config"),
                patch("yt_sync.load_failed", return_value={}),
                patch("yt_sync.fetch_playlist", return_value=fresh),
                patch("yt_sync.scan_local", return_value=[]),
            ):
                yt_sync.save_view_cache(
                    cached.missing, cached, cfg["playlist_url"], cfg["music_dir"]
                )
                app = YTSyncApp(cfg)
                async with app.run_test(size=(100, 30)) as pilot:
                    app.screen.dismiss(
                        yt_sync.SetupValues("Library", cfg["playlist_url"], cfg["music_dir"])
                    )
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertFalse(app._stale)
                    self.assertEqual(set(app._row_map), {"new#1"})
                    self.assertNotIn("cached", str(app.query_one("#status-bar").render()))
                    stored = yt_sync.load_view_cache(cfg["playlist_url"], cfg["music_dir"])
                    self.assertEqual([t.id for t in stored[1].missing], ["new"])


class CursorSelectTests(StateIsolatedMixin, unittest.IsolatedAsyncioTestCase):
    async def test_space_keeps_cursor_on_toggled_row(self):
        lf = LocalFile(Path("/tmp/a.opus"), "Artist - A")
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = types.MethodType(
            _stub_refresh(
                matched=[(Track("id1", "Artist - A", "http://x/1"), lf)],
                missing=[
                    Track("id2", "Artist - B", "http://x/2"),
                    Track("id3", "Artist - C", "http://x/3"),
                    Track("id4", "Artist - D", "http://x/4"),
                ],
            ),
            app,
        )
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                table = app.query_one("#table")
                self.assertEqual(table.row_count, 4)
                self.assertEqual(table.cursor_row, 0)

                await pilot.press("j")
                self.assertEqual(table.cursor_row, 1)

                await pilot.press("space")
                self.assertEqual(
                    table.cursor_row, 1, "space must not jump the cursor to row 0"
                )
                self.assertEqual(app.selected, {"id2#1"})

                await pilot.press("j")
                await pilot.press("space")
                self.assertEqual(table.cursor_row, 2)
                self.assertEqual(app.selected, {"id2#1", "id3#1"})

                # Mixed selection: matched row 0 + missing. Download must not
                # fall back to every missing track (id4 is unselected).
                targets = resolve_download_targets(
                    app.selected,
                    app._row_map,
                    app.diff_result.missing,
                    app.diff_result.failed,
                    app.mode,
                )
                self.assertEqual([t.id for t in targets], ["id2", "id3"])

    async def test_g_and_shift_g_jump_to_list_ends(self):
        app = YTSyncApp({**DEFAULTS, "playlist_url": "http://example", "music_dir": "/tmp"})
        app._do_refresh = types.MethodType(
            _stub_refresh(
                matched=[],
                missing=[
                    Track(f"id{i}", f"Artist - Song {i}", f"http://x/{i}")
                    for i in range(4)
                ],
            ),
            app,
        )
        with patch("yt_sync.save_config"):
            async with app.run_test(size=(100, 30)) as pilot:
                app.screen.dismiss(yt_sync.SetupValues("Library", "http://example", "/tmp"))
                await pilot.pause()
                table = app.query_one("#table")
                self.assertEqual(table.row_count, 4)
                self.assertEqual(table.cursor_row, 0)

                await pilot.press("G")
                await pilot.pause()
                self.assertEqual(table.cursor_row, 3)

                await pilot.press("g")
                await pilot.pause()
                self.assertEqual(table.cursor_row, 0)

                await pilot.press("k")
                await pilot.pause()
                self.assertEqual(table.cursor_row, 0, "cursor clamps at the top")

                await pilot.press("j")
                await pilot.pause()
                self.assertEqual(table.cursor_row, 1)


if __name__ == "__main__":
    unittest.main()
