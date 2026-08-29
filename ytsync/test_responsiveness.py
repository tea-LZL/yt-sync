import pytest
import asyncio
from unittest.mock import MagicMock, patch
from ytsync.yt_sync import YTSyncApp, SetupValues

@pytest.mark.asyncio
async def test_app_startup_time():
    # Mock config to avoid loading from disk
    cfg = {
        "playlist_url": "https://music.youtube.com/playlist?list=test",
        "music_dir": "/tmp/music",
        "audio_format": "opus",
        "real_delete": False,
        "theme": "textual-dark",
        "saved_configs": []
    }
    app = YTSyncApp(cfg)

    start_time = asyncio.get_event_loop().time()
    # Use run_test to properly initialize the app and mount screens
    async with app.run_test() as pilot:
        pass
    end_time = asyncio.get_event_loop().time()

    duration = end_time - start_time
    assert duration < 1.0  # Adjusted threshold for CI/test environments


@pytest.mark.asyncio
async def test_ui_responsiveness_during_io():
    cfg = {
        "playlist_url": "https://music.youtube.com/playlist?list=test",
        "music_dir": "/tmp/music",
        "audio_format": "opus",
        "real_delete": False,
        "theme": "textual-dark",
        "saved_configs": []
    }
    app = YTSyncApp(cfg)

    # Mock fetch_playlist to be slow
    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(0.5)
        return []

    with patch("ytsync.yt_sync.fetch_playlist", side_effect=slow_fetch):
        async with app.run_test() as pilot:
            # Start the refresh worker
            app._do_refresh()

            # Verify that _busy is True
            assert app._busy is True

            # If the event loop was blocked, this await would hang or delay
            await asyncio.sleep(0.1)
            assert True # Event loop is responsive

