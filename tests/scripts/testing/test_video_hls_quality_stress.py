from __future__ import annotations

import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.testing import video_hls_quality_stress as probe


def test_hls_probe_help_runs_from_outside_repo_without_pythonpath(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")

    completed = subprocess.run(
        [sys.executable, str(Path(probe.__file__).resolve()), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--verify-share" in completed.stdout


def test_share_token_from_url_handles_absolute_and_relative_links() -> None:
    assert probe.share_token_from_url("/shared/videos/abc123") == "abc123"
    assert probe.share_token_from_url("https://localhost/shared/videos/token-9?x=1#fragment") == "token-9"
    assert probe.share_token_from_url("/api/videos/1") == ""


def test_parse_accounts_accepts_csv_and_preserves_password_commas() -> None:
    assert probe.parse_accounts(["alice:A1,bob:B2", "carol:hello,world"]) == [
        ("alice", "A1"),
        ("bob", "B2"),
        ("carol", "hello,world"),
    ]


def test_write_result_redacts_session_credentials_and_share_urls(tmp_path: Path) -> None:
    report = {
        "csrf_token": "csrf-secret",
        "nested": {
            "token": "share-token",
            "password_required": True,
            "url": "/api/videos/shared/path-secret/hls/master.m3u8?share_session=session-secret",
        },
        "share_url": "/shared/videos/url-secret",
    }
    path = tmp_path / "report.json"

    safe = probe.write_result(path, report)
    written = path.read_text(encoding="utf-8")

    assert safe["csrf_token"] == "[redacted]"
    assert safe["nested"]["token"] == "[redacted]"
    assert safe["nested"]["password_required"] is True
    assert safe["nested"]["url"] == "/api/videos/shared/[redacted]/hls/master.m3u8?share_session=[redacted]"
    assert safe["share_url"] == "/shared/videos/[redacted]"
    for secret in ("csrf-secret", "share-token", "path-secret", "session-secret", "url-secret"):
        assert secret not in written


def test_browser_console_errors_ignore_only_expected_password_gate_401() -> None:
    expected_401 = "console.error:Failed to load resource: the server responded with a status of 401 (UNAUTHORIZED)"
    playback_404 = "console.error:Failed to load resource: the server responded with a status of 404 (NOT FOUND)"

    fatal, expected = probe.classify_browser_console_errors(
        [expected_401, playback_404, expected_401],
        password_prompt_seen=True,
    )

    assert expected == [expected_401]
    assert fatal == [playback_404, expected_401]
    assert probe.classify_browser_console_errors(
        [expected_401],
        password_prompt_seen=False,
    ) == ([expected_401], [])


def test_filter_db_state_limits_jobs_and_media_to_this_upload() -> None:
    upload = {"uploads": [{"video_id": 2, "file_id": "file-b", "ok": True}]}
    state = {
        "videos": [{"id": 1}, {"id": 2}],
        "jobs": [
            {"id": 1, "source_ref": "media_stream:file-a"},
            {"id": 2, "source_ref": "media_stream:file-b"},
        ],
        "assets": [{"uploaded_file_id": "file-a"}, {"uploaded_file_id": "file-b"}],
        "variants": [{"uploaded_file_id": "file-a"}, {"uploaded_file_id": "file-b"}],
        "subtitles": [{"uploaded_file_id": "file-a"}, {"uploaded_file_id": "file-b"}],
    }
    filtered = probe.filter_db_state_for_uploads(state, upload)
    assert filtered["videos"] == [{"id": 2}]
    assert filtered["jobs"] == [{"id": 2, "source_ref": "media_stream:file-b"}]
    assert filtered["assets"] == [{"uploaded_file_id": "file-b"}]
    assert filtered["variants"] == [{"uploaded_file_id": "file-b"}]
    assert filtered["subtitles"] == [{"uploaded_file_id": "file-b"}]

    failed_upload = probe.filter_db_state_for_uploads(state, {"uploads": [{"ok": False}]})
    assert failed_upload["videos"] == []
    assert failed_upload["jobs"] == []


def test_upload_phase_requires_every_parallel_upload_to_succeed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    outcomes = iter([{"ok": True, "username": "a"}, {"ok": False, "username": "b"}])
    monkeypatch.setenv("HACKME_HLS_STRESS_ACCOUNTS_JSON", '[{"username":"a","password":"x"},{"username":"b","password":"y"}]')
    monkeypatch.setattr(probe, "upload_video", lambda **_kwargs: next(outcomes))
    monkeypatch.setattr(probe, "monitor_loop", lambda **_kwargs: None)
    args = Namespace(
        video=str(video),
        accounts=[],
        base_url="https://127.0.0.1:1",
        db=str(tmp_path / "database.db"),
        runtime_marker=str(tmp_path),
        monitor_interval=0.01,
        privacy_mode="server_encrypted",
        upload_timeout_seconds=2,
        post_upload_observe_seconds=0,
        visibility="unlisted",
        share_password="secret",
        share_max_views=0,
    )
    result = probe.run_upload_phase(args)
    assert result["ok"] is False
    assert len(result["uploads"]) == 2


def test_upload_phase_records_every_account_when_parallel_wait_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    monkeypatch.setenv(
        "HACKME_HLS_STRESS_ACCOUNTS_JSON",
        '[{"username":"a","password":"x"},{"username":"b","password":"y"}]',
    )
    monkeypatch.setattr(probe, "upload_video", lambda **kwargs: {"ok": True, "username": kwargs["username"]})
    monkeypatch.setattr(probe, "monitor_loop", lambda **_kwargs: None)
    monkeypatch.setattr(
        probe.concurrent.futures,
        "as_completed",
        lambda *args, **kwargs: (_ for _ in ()).throw(probe.concurrent.futures.TimeoutError()),
    )
    args = Namespace(
        video=str(video),
        accounts=[],
        base_url="https://127.0.0.1:1",
        db=str(tmp_path / "database.db"),
        runtime_marker=str(tmp_path),
        monitor_interval=0.01,
        privacy_mode="server_encrypted",
        upload_timeout_seconds=2,
        post_upload_observe_seconds=0,
        visibility="unlisted",
        share_password="secret",
        share_max_views=0,
    )

    result = probe.run_upload_phase(args)

    assert result["ok"] is False
    assert {item["username"] for item in result["uploads"]} == {"a", "b"}
    assert all(item["error"] == "upload_phase_timeout" for item in result["uploads"])


def test_hls_upload_explicitly_requests_prepared_hls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"fixture")
    captured: dict[str, bytes] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"ok": True, "video": {"id": 1}, "file": {"file_id": "file-1"}}

    class FakeSession:
        def post(self, _url: str, *, data, **_kwargs) -> FakeResponse:
            chunks = []
            while chunk := data.read(1024):
                chunks.append(chunk)
            captured["body"] = b"".join(chunks)
            return FakeResponse()

    monkeypatch.setattr(
        probe,
        "login",
        lambda *_args, **_kwargs: {
            "ok": True,
            "session": FakeSession(),
            "token": "csrf",
            "csrf": {},
            "login": {},
        },
    )

    result = probe.upload_video(
        base_url="https://127.0.0.1:1",
        username="member",
        password="secret",
        video_path=video,
        privacy_mode="standard_plain",
        timeout_seconds=2,
    )

    assert result["ok"] is True
    assert b'name="streaming_modes"' in captured["body"]
    assert b'prepared_hls' in captured["body"]


def test_wait_for_hls_rejects_failed_terminal_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe,
        "db_state",
        lambda _path: {"jobs": [{"id": 7, "status": "failed", "stage": "transcode"}]},
    )
    monkeypatch.setattr(probe, "ps_snapshot", lambda _marker: [])
    args = Namespace(
        db=str(tmp_path / "database.db"),
        runtime_marker=str(tmp_path),
        print_wait_status=False,
    )

    result = probe.wait_for_hls(args)

    assert result["ok"] is False
    assert result["error"] == "hls_job_terminal_failure"
    assert result["failed_jobs"][0]["id"] == 7


@pytest.mark.parametrize(("mobile", "expected_viewport"), ((False, "desktop"), (True, "mobile")))
def test_browser_seek_records_machine_readable_frame_and_random_seek_latency(
    monkeypatch: pytest.MonkeyPatch,
    mobile: bool,
    expected_viewport: str,
) -> None:
    context_options: list[dict] = []

    class FakeLocator:
        def __init__(self, *, visible: bool = False) -> None:
            self.visible = visible

        def count(self) -> int:
            return int(self.visible)

        def click(self) -> None:
            return None

    class FakePage:
        def on(self, *_args) -> None:
            return None

        def goto(self, *_args, **_kwargs) -> None:
            return None

        def wait_for_selector(self, *_args, **_kwargs) -> None:
            return None

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(visible=selector == "#share-password-form:not(.hidden)")

        def fill(self, *_args) -> None:
            return None

        def wait_for_function(self, *_args, **_kwargs) -> None:
            return None

        def evaluate(self, source: str, *_args):
            if "playingObserved" in source:
                return {
                    "terminal_event": "playing_and_video_frame",
                    "playing_observed": True,
                    "frame_observed": True,
                    "frame_observation_method": "requestVideoFrameCallback",
                    "frame_metadata": {"presentedFrames": 2, "width": 1280, "height": 720},
                    "play_to_frame_latency_ms": 450,
                    "current_time": 0.25,
                    "ready_state": 4,
                    "network_state": 1,
                    "paused": False,
                    "play_error": "",
                }
            if "randomValues" in source:
                return {
                    "duration": 3900,
                    "target": 2100,
                    "target_ratio": 0.538,
                    "random_source": "crypto.getRandomValues",
                    "random_sample_uint32": 2_381_000_000,
                    "currentTime": 2100,
                    "readyState": 4,
                    "networkState": 1,
                    "paused": False,
                    "terminal_event": "seeked_and_video_frame",
                    "terminal_latency_ms": 1800,
                    "seeked_observed": True,
                    "frame_observed": True,
                    "frame_observation_method": "requestVideoFrameCallback",
                    "frame_metadata": {"presentedFrames": 3, "width": 1280, "height": 720},
                    "play_error": "",
                }
            return {
                "viewportWidth": 390 if mobile else 1366,
                "scrollWidth": 390 if mobile else 1366,
                "playerWidth": 390 if mobile else 960,
                "playerHeight": 219 if mobile else 540,
            }

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()

        def new_page(self) -> FakePage:
            return self.page

        def close(self) -> None:
            return None

    class FakeBrowser:
        def new_context(self, **kwargs) -> FakeContext:
            context_options.append(kwargs)
            return FakeContext()

        def close(self) -> None:
            return None

    class FakePlaywrightManager:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_kwargs: FakeBrowser()))

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakePlaywrightManager()),
    )
    clock = iter((10.0, 11.0, 11.5))
    monkeypatch.setattr(probe.time, "perf_counter", lambda: next(clock))

    result = probe.browser_seek_shared_video(
        base_url="https://127.0.0.1:1",
        share_url="/shared/videos/formal-token",
        share_password="private",
        mobile=mobile,
        minimum_duration_seconds=3600,
    )

    assert result["ok"] is True
    assert result["schema_version"] == "hackme.browser-video-latency/v1"
    assert result["viewport"] == expected_viewport
    assert result["emulation"]["is_mobile"] is mobile
    assert result["emulation"]["has_touch"] is mobile
    assert context_options[0]["is_mobile"] is mobile
    assert context_options[0]["has_touch"] is mobile
    assert result["latency_thresholds_ms"] == {
        "first_frame": 8000,
        "random_seek_terminal": 5000,
    }
    assert result["first_frame"]["origin"] == "unlock_submit"
    assert result["first_frame"]["elapsed_ms"] == 500
    assert result["first_frame"]["terminal_event"] == "playing_and_video_frame"
    assert result["seek"]["terminal_event"] == "seeked_and_video_frame"
    assert result["seek"]["random_source"] == "crypto.getRandomValues"
    assert result["seek"]["terminal_latency_ms"] == 1800


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg toolchain unavailable")
def test_generated_fixture_has_duration_two_audio_tracks_and_subtitles(tmp_path: Path) -> None:
    path = tmp_path / "long-fixture.mkv"
    generated = probe.generate_long_fixture(path, duration_seconds=12, ffmpeg_bin="ffmpeg", timeout_seconds=120)
    assert generated["ok"] is True
    media = probe.probe_media_file(path)
    assert media["ok"] is True
    assert media["duration_seconds"] >= 11
    assert media["video_streams"] == 1
    assert media["audio_streams"] == 2
    assert media["subtitle_streams"] == 1
