import pytest
import time
from pathlib import Path
from ytsync.yt_sync import Track, LocalFile, diff, scan_local, normalize

def generate_mock_playlist(n):
    return [Track(id=f"id_{i}", title=f"Song {i}", url=f"https://example.com/{i}") for i in range(n)]

def generate_mock_local_files(n, music_dir):
    music_dir = Path(music_dir)
    music_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(n):
        # Mix of exact matches and slightly different names to trigger fuzzy matching
        name = f"Song {i}.opus" if i % 2 == 0 else f"Song {i} - Edited.opus"
        p = music_dir / name
        p.write_text("mock content")
        files.append(LocalFile(path=p, stem=p.stem))
    return files

@pytest.mark.benchmark(parametrize=[(100,), (1000,)])
def test_diff_performance(benchmark, tmp_path, param):
    n = param[0]
    playlist = generate_mock_playlist(n)
    local = generate_mock_local_files(n, tmp_path)

    # We benchmark the diff function
    result = benchmark(diff, playlist, local, {})

    assert len(result.matched) > 0

@pytest.mark.benchmark(parametrize=[(100,), (1000,)])
def test_scan_local_performance(benchmark, tmp_path, param):
    n = param[0]
    generate_mock_local_files(n, tmp_path)

    # We benchmark the scan_local function
    result = benchmark(scan_local, str(tmp_path))

    assert len(result) >= n

def test_fuzzy_match_threshold():
    # Test the 0.85 ratio specifically
    playlist = [Track(id="1", title="The Quick Brown Fox", url="url")]
    local = [LocalFile(path=Path("The Quick Brown Fox.opus"), stem="The Quick Brown Fox")]

    # Exact match
    res = diff(playlist, local, {})
    assert len(res.matched) == 1

    # Slightly different - should still match (ratio > 0.85)
    local_fuzzy = [LocalFile(path=Path("The Quick Brown Fox 1.opus"), stem="The Quick Brown Fox 1")]
    res = diff(playlist, local_fuzzy, {})
    assert len(res.matched) == 1

    # Very different - should not match (ratio < 0.85)
    local_diff = [LocalFile(path=Path("Something Else.opus"), stem="Something Else")]
    res = diff(playlist, local_diff, {})
    assert len(res.matched) == 0
