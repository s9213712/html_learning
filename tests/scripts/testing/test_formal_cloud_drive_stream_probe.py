import shutil
from pathlib import Path

import pytest

from scripts.testing.formal_cloud_drive_stream_probe import make_fixture


def test_fixture_meets_the_probe_duration_and_track_contract(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for the real media fixture")

    media, summary = make_fixture(tmp_path)

    assert media.is_file()
    assert summary["duration_seconds"] >= 10
    assert summary["video_streams"] == 1
    assert summary["audio_streams"] == 2
    assert summary["subtitle_streams"] == 1
