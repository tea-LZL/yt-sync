#!/usr/bin/env python3
"""Regression tests for yt-sync selection → download/trash targeting and cursor."""

from __future__ import annotations

import types
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_sync
from textual.color import Color
from textual.widgets import Button, Input, ListView, Static
from yt_sync import (
    DEFAULTS,
    DiffResult,
    LocalFile,
    RowEntry,
    Track,
    YTSyncApp,
    resolve_download_targets,
    resolve_trash_targets,
)


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


class SetupValidationTests(unittest.TestCase):
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
                yt_sync.save_config(cfg)
                loaded = yt_sync.load_config()
        self.assertEqual(loaded["playlist_url"], cfg["playlist_url"])
        self.assertEqual(loaded["music_dir"], cfg["music_dir"])
        self.assertTrue(loaded["real_delete"])


class SavedConfigurationPersistenceTests(unittest.TestCase):
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


class DownloadDirectoryTests(unittest.TestCase):
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


class DirectoryPickerTests(unittest.TestCase):
    def test_uses_existing_directory_as_picker_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(yt_sync.nearest_existing_directory(str(root)), root)

    def test_uses_nearest_existing_parent_for_new_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            new_path = root / "Music" / "New Playlist"
            self.assertEqual(yt_sync.nearest_existing_directory(str(new_path)), root)


class HomeScreenTests(unittest.IsolatedAsyncioTestCase):
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


class ConfigurationHomeScreenTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_saved_configuration_name_is_the_visible_identity(self):
        saved = {
            "name": "Gym mix",
            "playlist_url": "https://music.youtube.com/playlist?list=gym",
            "music_dir": "/tmp/gym",
        }
        app = YTSyncApp({**DEFAULTS, **saved, "saved_configs": [saved]})
        app._do_refresh = lambda: None

        async with app.run_test(size=(100, 40)):
            identity = app.screen.query_one("#saved-config-0 .saved-config-url", Static)
            self.assertTrue(str(identity.render()).startswith("● Active  Gym mix  ·  "))
            self.assertIn(saved["playlist_url"], str(identity.render()))

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
            first_identity = app.screen.query_one("#saved-config-0 .saved-config-url", Static)
            second_identity = app.screen.query_one("#saved-config-1 .saved-config-url", Static)
            self.assertFalse(str(first_identity.render()).startswith("● Active"))
            self.assertTrue(str(second_identity.render()).startswith("● Active  Second collection"))

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
            "playlist_url": "https://music.youtube.com/playlist?list=PLIIlrAvOGW9yv-Y8jI5tHOSMaywVTuRGA",
            "music_dir": "/home/tea/Music/Youtube Music",
        }
        app = YTSyncApp({**DEFAULTS, **active, "saved_configs": [active]})
        app._do_refresh = lambda: None

        async with app.run_test(size=(80, 24)):
            url = app.screen.query_one("#saved-config-0 .saved-config-url", Static)
            directory = app.screen.query_one("#saved-config-0 .saved-config-dir", Static)
            self.assertEqual(url.content_region.height, 1)
            self.assertEqual(directory.content_region.height, 1)
            self.assertEqual(directory.region.y, url.region.y + 1)

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


class StartupFlowTests(unittest.IsolatedAsyncioTestCase):
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


class VisualContractTests(unittest.TestCase):
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
        self.assertIn("DataTable:hover", YTSyncApp.CSS)
        self.assertIn("border: round", YTSyncApp.CSS)
        self.assertIn("#console", YTSyncApp.CSS)


class VisualRuntimeTests(unittest.IsolatedAsyncioTestCase):
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


class MainModeTests(unittest.TestCase):
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


class ResolveDownloadTests(unittest.TestCase):
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


class ResolveTrashTests(unittest.TestCase):
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


class CursorSelectTests(unittest.IsolatedAsyncioTestCase):
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
                self.assertEqual(app.selected, {"2"})

                await pilot.press("j")
                await pilot.press("space")
                self.assertEqual(table.cursor_row, 2)
                self.assertEqual(app.selected, {"2", "3"})

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


if __name__ == "__main__":
    unittest.main()
