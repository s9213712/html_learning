import pytest
import shutil
import socket
from pathlib import Path

from services.storage.remote_downloads import (
    RemoteDownloadError,
    _read_tail,
    download_magnet_with_aria2,
    download_remote_url,
    download_torrent_file_with_aria2,
    download_torrent_url_with_aria2,
    inspect_torrent_file_trackers,
    validate_remote_url,
    validate_torrent_file_trackers,
)


def test_remote_download_log_tail_reads_only_tail_window(tmp_path):
    log_path = tmp_path / "aria2.log"
    log_path.write_text("\n".join(f"line-{idx}" for idx in range(20)), encoding="utf-8")

    tail = _read_tail(log_path, max_lines=3, max_chars=200)

    assert tail == "line-17\nline-18\nline-19"
    assert "line-0" not in tail


def test_magnet_download_reports_aria2_log_tail(monkeypatch):
    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.returncode = None
            self.stdout = ""
            self.stderr = ""
            log_path = cmd[cmd.index("--log") + 1]
            assert "--bt-stop-timeout=600" in cmd
            assert "--enable-dht=true" in cmd
            assert "--enable-peer-exchange=true" in cmd
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("notice\n")
                fh.write("errorCode=19 URI=magnet:?xt=urn:btih:bad\n")
                fh.write("Tracker returned failure reason\n")

        def poll(self):
            self.returncode = 19
            return self.returncode

        def communicate(self, timeout=None):
            return "If there are any errors, then see the log file.", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("services.storage.remote_downloads.shutil.which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr("services.storage.remote_downloads.subprocess.Popen", FakePopen)

    with pytest.raises(RemoteDownloadError) as exc:
        download_magnet_with_aria2("magnet:?xt=urn:btih:bad")

    message = str(exc.value)
    assert "BT/magnet 下載失敗" in message
    assert "Tracker returned failure reason" in message
    assert "If there are any errors" not in message


def test_magnet_metadata_timeout_reports_human_message(monkeypatch):
    class TimeoutPopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = None
            self.stdout = ""
            self.stderr = ""
            log_path = cmd[cmd.index("--log") + 1]
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("[NOTICE] Downloading 1 item(s)\n")
                fh.write("[NOTICE] Stop downloading torrent due to --bt-stop-timeout option.\n")
                fh.write("[NOTICE] Download GID not complete: [METADATA]deadbeef\n")

        def poll(self):
            self.returncode = 1
            return self.returncode

        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("services.storage.remote_downloads.shutil.which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr("services.storage.remote_downloads.subprocess.Popen", TimeoutPopen)

    with pytest.raises(RemoteDownloadError) as exc:
        download_magnet_with_aria2("magnet:?xt=urn:btih:bad")

    message = str(exc.value)
    assert "抓不到 torrent metadata" in message
    assert "[NOTICE]" not in message


def test_magnet_bind_failure_reports_host_network_message(monkeypatch):
    class BindFailurePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = None
            self.stdout = ""
            self.stderr = "If there are any errors, then see the log file."
            log_path = cmd[cmd.index("--log") + 1]
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("IPv4 DHT: failed to bind UDP port 6978\n")
                fh.write("Exception: Failed to bind a socket, cause: Operation not permitted\n")
                fh.write("Exception caught\n")
                fh.write("Errors occurred while binding port.\n")
                fh.write("Download GID not complete: [METADATA]deadbeef\n")

        def poll(self):
            self.returncode = 1
            return self.returncode

        def communicate(self, timeout=None):
            return "", self.stderr

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("services.storage.remote_downloads.shutil.which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr("services.storage.remote_downloads.subprocess.Popen", BindFailurePopen)

    with pytest.raises(RemoteDownloadError) as exc:
        download_magnet_with_aria2("magnet:?xt=urn:btih:bad")

    message = str(exc.value)
    assert "無法綁定 BT/DHT 連接埠" in message
    assert "抓不到 torrent metadata" not in message


def test_bt_download_kills_when_temp_size_exceeds_limit(monkeypatch):
    class SlowPopen:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.returncode = None
            log_path = cmd[cmd.index("--log") + 1]
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("downloading\n")
            payload = cmd[cmd.index("--dir") + 1] + "/large.bin"
            with open(payload, "wb") as fh:
                fh.write(b"x" * 2048)

        def poll(self):
            return None

        def communicate(self, timeout=None):
            self.returncode = -9
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("services.storage.remote_downloads.shutil.which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr("services.storage.remote_downloads.subprocess.Popen", SlowPopen)

    with pytest.raises(RemoteDownloadError) as exc:
        download_magnet_with_aria2("magnet:?xt=urn:btih:bad", max_bytes=1024)

    assert "超過容量限制" in str(exc.value)


def test_bt_active_progress_survives_idle_timeout(monkeypatch):
    class ActivePopen:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.returncode = None
            self.stdout = ""
            self.stderr = ""
            self.calls = 0
            self.payload = Path(cmd[cmd.index("--dir") + 1]) / "progress.bin"
            log_path = cmd[cmd.index("--log") + 1]
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("downloading\n")

        def poll(self):
            self.calls += 1
            if self.calls <= 4:
                self.payload.write_bytes(b"x" * self.calls)
                return None
            self.returncode = 0
            return self.returncode

        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            self.returncode = -9

    now = {"value": 0}

    def fake_monotonic():
        now["value"] += 1
        return now["value"]

    monkeypatch.setattr("services.storage.remote_downloads.shutil.which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr("services.storage.remote_downloads.subprocess.Popen", ActivePopen)
    monkeypatch.setattr("services.storage.remote_downloads.time.monotonic", fake_monotonic)
    monkeypatch.setattr("services.storage.remote_downloads.time.sleep", lambda seconds: None)

    downloaded = download_magnet_with_aria2("magnet:?xt=urn:btih:good", timeout_seconds=1)
    try:
        assert downloaded.filename == "progress.bin"
        assert Path(downloaded.path).read_bytes() == b"xxxx"
    finally:
        shutil.rmtree(downloaded.cleanup_dir, ignore_errors=True)


def test_bt_stalled_download_uses_idle_timeout_message(monkeypatch):
    killed = {"value": False}

    class StalledPopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = None
            self.stdout = ""
            self.stderr = ""
            log_path = cmd[cmd.index("--log") + 1]
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("waiting for peers\n")

        def poll(self):
            return None

        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            killed["value"] = True
            self.returncode = -9

    now = {"value": 0}

    def fake_monotonic():
        now["value"] += 1
        return now["value"]

    monkeypatch.setattr("services.storage.remote_downloads.shutil.which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr("services.storage.remote_downloads.subprocess.Popen", StalledPopen)
    monkeypatch.setattr("services.storage.remote_downloads.time.monotonic", fake_monotonic)
    monkeypatch.setattr("services.storage.remote_downloads.time.sleep", lambda seconds: None)

    with pytest.raises(RemoteDownloadError) as exc:
        download_magnet_with_aria2("magnet:?xt=urn:btih:stalled", timeout_seconds=1)

    assert killed["value"] is True
    assert "停滯逾時" in str(exc.value)


def test_magnet_trackers_exclude_private_hosts_without_blocking_bt(monkeypatch):
    def fake_getaddrinfo(host, port, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", int(port or 80)))]

    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)

    parsed = validate_remote_url("magnet:?xt=urn:btih:abc&tr=http%3A%2F%2Flocalhost%3A8080%2Fannounce")
    assert parsed["kind"] == "magnet"


def test_magnet_download_excludes_private_trackers(monkeypatch):
    tracker = "http://localhost:8080/announce"
    magnet = "magnet:?xt=urn:btih:abc&tr=http%3A%2F%2Flocalhost%3A8080%2Fannounce"

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.returncode = None
            self.stdout = ""
            self.stderr = ""
            assert "--bt-exclude-tracker" in cmd
            assert cmd[cmd.index("--bt-exclude-tracker") + 1] == tracker
            target = Path(cmd[cmd.index("--dir") + 1]) / "magnet-result.bin"
            target.write_bytes(b"ok")
            log_path = cmd[cmd.index("--log") + 1]
            Path(log_path).write_text("done\n", encoding="utf-8")

        def poll(self):
            self.returncode = 0
            return self.returncode

        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("services.storage.remote_downloads.shutil.which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr("services.storage.remote_downloads.subprocess.Popen", FakePopen)

    downloaded = download_magnet_with_aria2(magnet)
    try:
        assert downloaded.filename == "magnet-result.bin"
        assert Path(downloaded.path).read_bytes() == b"ok"
    finally:
        shutil.rmtree(downloaded.cleanup_dir, ignore_errors=True)


def test_magnet_tracker_dns_failure_does_not_block_bt(monkeypatch):
    def fake_getaddrinfo(host, port, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "name or service not known")

    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)

    parsed = validate_remote_url(
        "magnet:?xt=urn:btih:abc&tr=udp%3A%2F%2Ftracker.openbittorrent.com%3A80%2Fannounce"
    )
    assert parsed["kind"] == "magnet"


def test_torrent_url_is_classified_as_bt(monkeypatch):
    def fake_getaddrinfo(host, port=None, **kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", int(port or 80)))]

    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)

    parsed = validate_remote_url("https://downloads.example/file.torrent?token=abc")
    assert parsed["kind"] == "torrent_url"


def test_resolver_accepts_public_candidate_when_private_candidate_exists(monkeypatch):
    def fake_getaddrinfo(host, port=None, **kwargs):
        return [
            (2, 1, 6, "", ("10.0.0.5", int(port or 80))),
            (2, 1, 6, "", ("8.8.8.8", int(port or 80))),
        ]

    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)

    parsed = validate_remote_url("https://downloads.example/file.torrent?token=abc")
    assert parsed["kind"] == "torrent_url"


def test_direct_download_still_rejects_private_only_dns(monkeypatch):
    def fake_getaddrinfo(host, port=None, **kwargs):
        return [(2, 1, 6, "", ("10.0.0.5", int(port or 80)))]

    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(RemoteDownloadError, match="保留位址"):
        validate_remote_url("https://downloads.example/file.zip")


def test_direct_mode_saves_torrent_file_itself(monkeypatch, tmp_path):
    def fake_getaddrinfo(host, port=None, **kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", int(port or 80)))]

    source = tmp_path / "file.torrent"
    source.write_bytes(b"d8:announce0:e")

    class FakeDownloaded:
        path = str(source)
        filename = "file.torrent"
        mimetype = "application/x-bittorrent"
        cleanup_dir = None

    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("services.storage.remote_downloads.download_direct_link", lambda url, **kwargs: FakeDownloaded())
    monkeypatch.setattr(
        "services.storage.remote_downloads.download_torrent_file_with_aria2",
        lambda *args, **kwargs: pytest.fail("direct mode must not run aria2 for .torrent URLs"),
    )

    downloaded = download_remote_url("https://downloads.example/file.torrent", treat_torrent_as_bt=False)
    assert downloaded.filename == "file.torrent"


def test_torrent_url_mode_downloads_payload_with_aria2(monkeypatch, tmp_path):
    def fake_getaddrinfo(host, port=None, **kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", int(port or 80)))]

    torrent = tmp_path / "payload.torrent"
    torrent.write_bytes(b"d8:announce0:e")
    result = tmp_path / "payload.txt"
    result.write_text("payload", encoding="utf-8")
    captured = {}

    class FakeTorrent:
        path = str(torrent)
        filename = "payload.torrent"
        mimetype = "application/x-bittorrent"
        cleanup_dir = None

    class FakeDownloaded:
        path = str(result)
        filename = "payload.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("services.storage.remote_downloads.download_direct_link", lambda url, **kwargs: FakeTorrent())

    def fake_torrent_download(path, **kwargs):
        captured["path"] = path
        captured["display_name"] = kwargs.get("display_name")
        return FakeDownloaded()

    monkeypatch.setattr("services.storage.remote_downloads.download_torrent_file_with_aria2", fake_torrent_download)

    downloaded = download_torrent_url_with_aria2("https://downloads.example/payload.torrent")
    assert downloaded.filename == "payload.txt"
    assert captured["path"] == str(torrent)
    assert captured["display_name"] == "payload.torrent"


def test_torrent_file_trackers_report_private_hosts_without_rejecting(tmp_path, monkeypatch):
    def fake_getaddrinfo(host, port, **kwargs):
        return [(2, 1, 6, "", ("10.0.0.5", int(port or 80)))]

    torrent = tmp_path / "bad.torrent"
    torrent.write_bytes(
        b"d8:announce31:http://tracker.example/announce4:infod4:name4:test12:piece lengthi16384e6:pieces0:ee"
    )
    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)

    report = validate_torrent_file_trackers(torrent)
    assert report["blocked"][0]["url"] == "http://tracker.example/announce"
    assert "保留位址" in report["blocked"][0]["reason"]


def test_torrent_file_allows_udp_tracker_when_dns_is_unavailable(tmp_path, monkeypatch):
    tracker = b"udp://tracker.openbittorrent.com:80/announce"
    torrent = tmp_path / "udp-tracker.torrent"
    torrent.write_bytes(
        b"d8:announce" + str(len(tracker)).encode("ascii") + b":" + tracker +
        b"4:infod4:name4:test12:piece lengthi16384e6:pieces0:ee"
    )

    def fake_getaddrinfo(host, port, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "name or service not known")

    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)

    report = validate_torrent_file_trackers(torrent)
    assert report["blocked"] == []
    assert report["trackers"] == [tracker.decode("ascii")]


def test_torrent_download_excludes_private_trackers(tmp_path, monkeypatch):
    tracker = b"http://tracker.example/announce"
    torrent = tmp_path / "bad-tracker.torrent"
    torrent.write_bytes(
        b"d8:announce" + str(len(tracker)).encode("ascii") + b":" + tracker +
        b"4:infod4:name4:test12:piece lengthi16384e6:pieces0:ee"
    )
    def fake_getaddrinfo(host, port, **kwargs):
        if host == "tracker.example":
            return [(2, 1, 6, "", ("10.255.255.254", int(port or 80)))]
        return [(2, 1, 6, "", ("8.8.8.8", int(port or 80)))]

    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)

    report = inspect_torrent_file_trackers(torrent)
    assert report["blocked"][0]["url"] == tracker.decode("ascii")
    result = tmp_path / "result.bin"
    result.write_bytes(b"ok")

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.returncode = None
            self.stdout = ""
            self.stderr = ""
            assert "--bt-exclude-tracker" in cmd
            assert cmd[cmd.index("--bt-exclude-tracker") + 1] == tracker.decode("ascii")
            target = Path(cmd[cmd.index("--dir") + 1]) / "result.bin"
            target.write_bytes(result.read_bytes())
            log_path = cmd[cmd.index("--log") + 1]
            Path(log_path).write_text("done\n", encoding="utf-8")

        def poll(self):
            self.returncode = 0
            return self.returncode

        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("services.storage.remote_downloads.shutil.which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr("services.storage.remote_downloads.subprocess.Popen", FakePopen)
    monkeypatch.setenv("HACKME_BT_BACKEND", "aria2")

    downloaded = download_torrent_file_with_aria2(torrent)
    try:
        assert downloaded.filename == "result.bin"
        assert Path(downloaded.path).read_bytes() == b"ok"
    finally:
        shutil.rmtree(downloaded.cleanup_dir, ignore_errors=True)


def test_torrent_file_bdecode_depth_is_limited(tmp_path):
    torrent = tmp_path / "deep.torrent"
    torrent.write_bytes(b"l" * 80 + b"0:" + b"e" * 80)

    with pytest.raises(RemoteDownloadError, match="格式無法解析"):
        validate_torrent_file_trackers(torrent)
