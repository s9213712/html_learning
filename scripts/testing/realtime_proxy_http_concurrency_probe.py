#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
import urllib3


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.playwright_deep_site_check import (  # noqa: E402
    MANAGER_PASSWORD,
    ROOT_PASSWORD,
    TEST_PASSWORD,
    free_port,
    mkdirs,
    start_server,
    wait_for_server,
)
from scripts.testing.realtime_proxy_stress_probe import generate_multiaudio_fixture  # noqa: E402


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
SHARE_PASSWORD = "ProxyHttpShare123!"
FEATURE_KEYS = [
    "feature_accounts_enabled",
    "feature_chat_enabled",
    "feature_community_enabled",
    "feature_appeals_enabled",
    "feature_audit_log_enabled",
    "feature_violation_center_enabled",
    "feature_reports_enabled",
    "feature_system_health_enabled",
    "feature_identity_governance_enabled",
    "feature_account_security_enabled",
    "feature_member_governance_enabled",
    "feature_server_modes_enabled",
    "feature_snapshot_restore_enabled",
    "feature_health_center_enabled",
    "feature_forum_core_enabled",
    "feature_ui_rebuild_enabled",
    "feature_reports_notifications_enabled",
    "feature_attachments_enabled",
    "feature_privacy_uploads_enabled",
    "feature_storage_albums_enabled",
    "feature_personalization_enabled",
    "feature_social_search_enabled",
    "feature_advanced_security_enabled",
    "feature_comfyui_enabled",
    "feature_economy_enabled",
    "feature_trading_enabled",
    "feature_games_enabled",
    "feature_videos_enabled",
]


class BackgroundPidServer:
    def __init__(self, pid: int, *, log_path: Path):
        self.pid = int(pid)
        self.log_path = log_path

    def poll(self):
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return 0
        except Exception:
            return None
        return None

    def terminate(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def wait(self, timeout=8):
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if self.poll() is not None:
                return 0
            time.sleep(0.2)
        raise subprocess.TimeoutExpired(str(self.pid), timeout)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def absolute_url(base_url: str, value: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", str(value or ""))


def with_query(url: str, **params: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value is not None and str(value) != "":
            query[key] = [str(value)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def token_from_share_url(share_url: str) -> str:
    match = re.search(r"/shared/videos/([^/?#]+)", str(share_url or ""))
    return match.group(1) if match else ""


def request_json(session: requests.Session, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = session.request(method, url, **kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            body: Any = response.json()
        except Exception:
            body = response.text[:1000]
        return {"status": response.status_code, "ok": response.ok, "elapsed_ms": round(elapsed_ms, 3), "body": body}
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "status": 0,
            "ok": False,
            "elapsed_ms": round(elapsed_ms, 3),
            "error": exc.__class__.__name__,
            "message": str(exc),
        }


def new_session(base_url: str) -> requests.Session:
    session = requests.Session()
    if base_url.startswith("https://"):
        session.verify = False
    return session


def login_root(base_url: str) -> dict[str, Any]:
    session = new_session(base_url)
    csrf = request_json(session, "GET", absolute_url(base_url, "/api/csrf-token"), timeout=10)
    token = ""
    if isinstance(csrf.get("body"), dict):
        token = str(csrf["body"].get("csrf_token") or csrf["body"].get("token") or "")
    login = request_json(
        session,
        "POST",
        absolute_url(base_url, "/api/login"),
        json={"username": "root", "password": ROOT_PASSWORD},
        headers={"X-CSRF-Token": token},
        timeout=20,
    )
    token = str(session.cookies.get("csrf_token") or token)
    return {
        "ok": csrf["status"] == 200 and login["status"] == 200 and bool(token),
        "session": session,
        "csrf_token": token,
        "csrf": csrf,
        "login": login,
    }


def enable_features(base_url: str, auth: dict[str, Any]) -> dict[str, Any]:
    payload = {key: True for key in FEATURE_KEYS}
    return request_json(
        auth["session"],
        "PUT",
        absolute_url(base_url, "/api/admin/features"),
        json=payload,
        headers={"X-CSRF-Token": auth["csrf_token"]},
        timeout=20,
    )


def upload_video(base_url: str, auth: dict[str, Any], video_path: Path) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/x-matroska"
    started = time.perf_counter()
    try:
        with video_path.open("rb") as handle:
            response = auth["session"].post(
                absolute_url(base_url, "/api/videos/upload"),
                data={
                    "title": "Realtime Proxy HTTP Concurrency QA",
                    "description": "Standard 即時轉封裝 HTTP 併發壓測 fixture。",
                    "visibility": "unlisted",
                    "privacy_mode": "standard_plain",
                    "share_password": SHARE_PASSWORD,
                    "share_max_views": "0",
                },
                files={"video": (video_path.name, handle, mime_type)},
                headers={"X-CSRF-Token": auth["csrf_token"]},
                timeout=120,
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            body: Any = response.json()
        except Exception:
            body = response.text[:1000]
        return {"status": response.status_code, "ok": response.ok and bool(isinstance(body, dict) and body.get("ok")), "elapsed_ms": round(elapsed_ms, 3), "body": body}
    except Exception as exc:
        return {"status": 0, "ok": False, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "error": exc.__class__.__name__, "message": str(exc)}


def unlock_share(base_url: str, token: str) -> dict[str, Any]:
    session = new_session(base_url)
    csrf = request_json(session, "GET", absolute_url(base_url, "/api/csrf-token"), timeout=10)
    csrf_token = ""
    if isinstance(csrf.get("body"), dict):
        csrf_token = str(csrf["body"].get("csrf_token") or csrf["body"].get("token") or "")
    unlock = request_json(
        session,
        "POST",
        absolute_url(base_url, f"/api/videos/shared/{token}/unlock"),
        json={"password": SHARE_PASSWORD},
        headers={"X-CSRF-Token": csrf_token},
        timeout=20,
    )
    share_session = ""
    if isinstance(unlock.get("body"), dict):
        share_session = str(unlock["body"].get("share_session_id") or "")
    return {"ok": unlock["status"] == 200 and bool(share_session), "session": session, "csrf": csrf, "unlock": unlock, "share_session": share_session}


def wait_for_playback(base_url: str, session: requests.Session, token: str, share_session: str, *, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last: dict[str, Any] = {}
    path = f"/api/videos/shared/{token}/playback"
    while time.time() < deadline:
        result = request_json(
            session,
            "GET",
            with_query(absolute_url(base_url, path), share_session=share_session),
            timeout=20,
        )
        last = result
        body = result.get("body") if isinstance(result.get("body"), dict) else {}
        if result.get("status") == 200 and body.get("realtime_proxy_url") and body.get("mode") == "hls" and body.get("master_url"):
            return {**result, "ready": True}
        time.sleep(1)
    return {**last, "ready": False}


def first_chunk_fetch(session: requests.Session, url: str, *, timeout: int = 20, max_bytes: int = 4096) -> dict[str, Any]:
    started = time.perf_counter()
    response = None
    try:
        response = session.get(url, stream=True, timeout=timeout)
        header_ms = (time.perf_counter() - started) * 1000
        if response.status_code >= 400:
            body_bytes = response.content or b""
            elapsed_ms = (time.perf_counter() - started) * 1000
            return {
                "status": response.status_code,
                "ok": response.ok,
                "header_ms": round(header_ms, 3),
                "first_chunk_ms": round(elapsed_ms, 3),
                "first_chunk_bytes": len(body_bytes),
                "content_type": response.headers.get("Content-Type") or "",
                "body_sample": body_bytes[:500].decode("utf-8", errors="replace"),
            }
        first = b""
        for chunk in response.iter_content(chunk_size=max_bytes):
            first = chunk or b""
            if first:
                break
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "status": response.status_code,
            "ok": response.ok,
            "header_ms": round(header_ms, 3),
            "first_chunk_ms": round(elapsed_ms, 3),
            "first_chunk_bytes": len(first),
            "content_type": response.headers.get("Content-Type") or "",
            "body_sample": "",
        }
    except Exception as exc:
        return {
            "status": 0,
            "ok": False,
            "header_ms": 0,
            "first_chunk_ms": round((time.perf_counter() - started) * 1000, 3),
            "first_chunk_bytes": 0,
            "error": exc.__class__.__name__,
            "message": str(exc),
        }
    finally:
        if response is not None:
            response.close()


def parse_first_hls_child(playlist_url: str, text: str) -> str:
    for line in str(text or "").splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue
        return urljoin(playlist_url, clean)
    return ""


def fetch_hls_first_segment(base_url: str, session: requests.Session, playback_body: dict[str, Any]) -> dict[str, Any]:
    master_url = absolute_url(base_url, playback_body.get("master_url") or "")
    master = request_json(session, "GET", master_url, timeout=20)
    result: dict[str, Any] = {"master": master, "playlist": {}, "segment": {}}
    if master.get("status") != 200:
        return result
    master_text = str(master.get("body") or "")
    playlist_url = ""
    variants = playback_body.get("variants") if isinstance(playback_body.get("variants"), list) else []
    if variants:
        playlist_url = absolute_url(base_url, str((variants[0] or {}).get("playlist_url") or ""))
    if not playlist_url:
        playlist_url = parse_first_hls_child(master_url, master_text)
    if not playlist_url:
        return result
    playlist = request_json(session, "GET", playlist_url, timeout=20)
    result["playlist"] = playlist
    if playlist.get("status") != 200:
        return result
    segment_url = parse_first_hls_child(playlist_url, str(playlist.get("body") or ""))
    if segment_url:
        result["segment"] = first_chunk_fetch(session, segment_url, timeout=20, max_bytes=4096)
    return result


def realtime_proxy_metrics_path(runtime_root: Path) -> Path:
    return runtime_root / "reports" / "qa" / "realtime_proxy_stream_metrics.jsonl"


def load_realtime_proxy_metrics(runtime_root: Path) -> list[dict[str, Any]]:
    path = realtime_proxy_metrics_path(runtime_root)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def wait_realtime_proxy_metrics(runtime_root: Path, *, timeout_seconds: float = 5.0) -> list[dict[str, Any]]:
    deadline = time.time() + float(timeout_seconds)
    rows: list[dict[str, Any]] = []
    while time.time() < deadline:
        rows = load_realtime_proxy_metrics(runtime_root)
        if rows:
            return rows
        time.sleep(0.2)
    return rows


def summarize_server_metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"count": 0, "latest": {}, "checks": {"present": False}}
    latest = rows[-1]
    metrics = latest.get("metrics") if isinstance(latest.get("metrics"), dict) else {}
    checks = {
        "present": True,
        "finished": bool(metrics.get("finished")),
        "bytes_sent": int(metrics.get("bytes_sent") or 0) > 0,
        "rss_peak_bytes": "rss_peak_bytes" in metrics,
        "cpu_time_seconds": "cpu_time_seconds" in metrics,
        "closed_by_client": bool(metrics.get("closed_by_client")),
        "host_global_scope": (latest.get("runtime") or {}).get("scope") == "global"
        and metrics.get("runtime_scope") == "global",
    }
    compact_latest = {
        "request_id": latest.get("request_id"),
        "path": latest.get("path"),
        "route_kind": latest.get("route_kind"),
        "selected_audio": latest.get("selected_audio"),
        "runtime": latest.get("runtime"),
        "metrics": {
            key: metrics.get(key)
            for key in (
                "bytes_sent",
                "chunks_sent",
                "first_chunk_latency_ms",
                "duration_ms",
                "rss_peak_bytes",
                "cpu_time_seconds",
                "runtime_scope",
                "runtime_slot_index",
                "closed_by_client",
                "returncode",
                "finished",
            )
        },
    }
    return {"count": len(rows), "latest": compact_latest, "checks": checks}


def start_gunicorn_server(run_root: Path, runtime_root: Path, port: int, args: argparse.Namespace) -> BackgroundPidServer:
    run_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / "logs").mkdir(parents=True, exist_ok=True)
    launcher_log = runtime_root / "logs" / "realtime_proxy_gunicorn_launcher.out"
    command = [
        str(ROOT / "test_for_develop.sh"),
        "--cli",
        "--run-root",
        str(run_root),
        "--in-place",
        "--tmp-runtime",
        "--skip-install",
        "--port",
        str(port),
        "--port-conflict",
        "fail",
        "--server-runner",
        "gunicorn",
        "--gunicorn-workers",
        str(int(args.gunicorn_workers)),
        "--gunicorn-threads",
        str(int(args.gunicorn_threads)),
        "--gunicorn-timeout",
        str(int(args.gunicorn_timeout)),
        "--gunicorn-max-requests",
        "0",
        "--gunicorn-max-requests-jitter",
        "0",
        "--feature-mode",
        "all",
        "--security",
        "off",
        "--no-capacity-probe",
    ]
    env = os.environ.copy()
    env.update({
        "ROOT_PASSWORD": ROOT_PASSWORD,
        "MANAGER_PASSWORD": MANAGER_PASSWORD,
        "TEST_PASSWORD": TEST_PASSWORD,
    })
    env.setdefault("HACKME_DEV_BACKTEST_PROBE_ON_STARTUP", "0")
    completed = subprocess.run(command, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=90)
    launcher_log.write_text(
        "\n".join([
            "$ " + " ".join(command),
            "",
            "## stdout",
            completed.stdout or "",
            "",
            "## stderr",
            completed.stderr or "",
        ]),
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"gunicorn launcher failed rc={completed.returncode}; see {launcher_log}")
    pid_path = runtime_root / "server.pid"
    deadline = time.time() + 20
    while time.time() < deadline:
        if pid_path.exists():
            raw = pid_path.read_text(encoding="utf-8", errors="replace").strip()
            if raw.isdigit():
                return BackgroundPidServer(int(raw), log_path=launcher_log)
        time.sleep(0.2)
    raise RuntimeError(f"gunicorn pid file not found: {pid_path}")


def start_probe_server(run_root: Path, runtime_root: Path, port: int, args: argparse.Namespace):
    if str(args.server_runner or "flask").strip().lower() == "gunicorn":
        return start_gunicorn_server(run_root, runtime_root, port, args)
    return start_server(runtime_root, port)


def hold_standard_stream(session: requests.Session, url: str) -> dict[str, Any]:
    started = time.perf_counter()
    response = session.get(url, stream=True, timeout=30)
    return {
        "response": response,
        "status": response.status_code,
        "header_ms": round((time.perf_counter() - started) * 1000, 3),
        "content_type": response.headers.get("Content-Type") or "",
    }


def run_http_phase(base_url: str, token: str, share_session: str, playback_body: dict[str, Any]) -> dict[str, Any]:
    proxy_url = absolute_url(base_url, playback_body.get("realtime_proxy_url") or (playback_body.get("realtime_proxy") or {}).get("url") or "")
    direct_url = absolute_url(base_url, playback_body.get("stream_url") or playback_body.get("fallback_url") or "")
    first_proxy = with_query(proxy_url, audio="audio_01_jpn")
    busy_proxy = with_query(proxy_url, audio="audio_02_eng")
    shared = new_session(base_url)
    shared.cookies.set("csrf_token", "")
    held = None
    first_response = None
    result: dict[str, Any] = {"proxy_url": proxy_url, "direct_url": direct_url}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(hold_standard_stream, shared, first_proxy)
            held = future.result(timeout=30)
        first_response = held.pop("response")
        result["held_standard"] = held

        concurrent_started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                "busy_standard": pool.submit(first_chunk_fetch, new_session(base_url), busy_proxy, timeout=20, max_bytes=4096),
                "basic_direct": pool.submit(first_chunk_fetch, new_session(base_url), direct_url, timeout=20, max_bytes=4096),
                "premium_hls": pool.submit(fetch_hls_first_segment, base_url, new_session(base_url), playback_body),
            }
            concurrent_results = {name: future.result(timeout=35) for name, future in futures.items()}
        result["concurrent_elapsed_ms"] = round((time.perf_counter() - concurrent_started) * 1000, 3)
        result.update(concurrent_results)

        started = time.perf_counter()
        first_chunk = b""
        for chunk in first_response.iter_content(chunk_size=4096):
            first_chunk = chunk or b""
            if first_chunk:
                break
        result["held_standard_first_chunk"] = {
            "first_chunk_bytes": len(first_chunk),
            "first_chunk_ms_after_concurrent": round((time.perf_counter() - started) * 1000, 3),
        }
    finally:
        if first_response is not None:
            first_response.close()
    busy_body = str((result.get("busy_standard") or {}).get("body_sample") or "")
    hls = result.get("premium_hls") or {}
    segment = hls.get("segment") or {}
    checks = {
        "held_standard_200": (result.get("held_standard") or {}).get("status") == 200,
        "busy_standard_429": (result.get("busy_standard") or {}).get("status") == 429 and "realtime_proxy_busy" in busy_body,
        "basic_direct_ok": (result.get("basic_direct") or {}).get("status") in {200, 206} and int((result.get("basic_direct") or {}).get("first_chunk_bytes") or 0) > 0,
        "premium_hls_ok": (hls.get("master") or {}).get("status") == 200
        and (hls.get("playlist") or {}).get("status") == 200
        and segment.get("status") in {200, 206}
        and int(segment.get("first_chunk_bytes") or 0) > 0,
    }
    result["checks"] = checks
    result["ok"] = all(checks.values())
    return result


def write_reports(result: dict[str, Any], json_path: Path) -> tuple[Path, Path]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path = json_path.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    http = result.get("http_phase") or {}
    lines = [
        "# Realtime Proxy HTTP Concurrency Probe",
        "",
        f"- OK: `{result.get('ok')}`",
        f"- Base URL: `{result.get('base_url')}`",
        f"- Server runner: `{result.get('server_runner')}`",
        f"- Gunicorn workers/threads: `{result.get('gunicorn_workers')}/{result.get('gunicorn_threads')}`",
        f"- Run root: `{result.get('run_root')}`",
        f"- Runtime root: `{result.get('runtime_root')}`",
        f"- Video ID: `{result.get('video_id')}`",
        f"- Share token: `{result.get('share_token')}`",
        "",
        "## Checks",
        "",
    ]
    for key, value in (http.get("checks") or {}).items():
        lines.append(f"- {key}: `{value}`")
    server_metrics = result.get("server_metrics_summary") or {}
    for key, value in (server_metrics.get("checks") or {}).items():
        lines.append(f"- server_metrics_{key}: `{value}`")
    lines.extend([
        "",
        "## Measurements",
        "",
        f"- Held Standard: `{http.get('held_standard')}`",
        f"- Busy Standard: `{http.get('busy_standard')}`",
        f"- Basic Direct: `{http.get('basic_direct')}`",
        f"- Premium HLS: `{http.get('premium_hls')}`",
        f"- Held first chunk after concurrent checks: `{http.get('held_standard_first_chunk')}`",
        f"- Server metrics latest: `{server_metrics.get('latest')}`",
        "",
    ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    run_root = Path(args.runtime_root).resolve()
    server_runner = str(args.server_runner or "flask").strip().lower()
    runtime_root = (run_root / "runtime").resolve() if server_runner == "gunicorn" else run_root
    if server_runner == "gunicorn":
        run_root.mkdir(parents=True, exist_ok=True)
    mkdirs(runtime_root)
    report_dir = runtime_root / "reports" / "qa"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = Path(args.json_out).resolve() if args.json_out else report_dir / "realtime_proxy_http_concurrency_probe.json"

    os.environ["HACKME_MEDIA_REALTIME_PROXY_ENABLED"] = "1"
    os.environ["HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT"] = str(int(args.max_concurrent))
    os.environ["HACKME_MEDIA_REALTIME_PROXY_TIMEOUT_SECONDS"] = "60"

    fixture_path = report_dir / "realtime_proxy_http_fixture.mkv"
    print(f"[phase] generating fixture {fixture_path}", flush=True)
    fixture = generate_multiaudio_fixture(
        fixture_path,
        ffmpeg_bin=args.ffmpeg_bin,
        duration=args.duration,
        size=args.fixture_size,
        rate=args.fixture_rate,
        video_bitrate=args.fixture_video_bitrate,
    )

    port = free_port()
    server = start_probe_server(run_root, runtime_root, port, args)
    result: dict[str, Any] = {
        "ok": False,
        "created_at": utc_now(),
        "runtime_root": str(runtime_root),
        "run_root": str(run_root),
        "server_runner": server_runner,
        "gunicorn_workers": int(args.gunicorn_workers) if server_runner == "gunicorn" else 0,
        "gunicorn_threads": int(args.gunicorn_threads) if server_runner == "gunicorn" else 0,
        "fixture": fixture,
        "max_concurrent": int(args.max_concurrent),
    }
    try:
        base_url = wait_for_server(port)
        result["base_url"] = base_url
        print(f"[phase] server ready {base_url}", flush=True)

        auth = login_root(base_url)
        result["login"] = {"ok": auth["ok"], "csrf": auth["csrf"], "login": auth["login"]}
        if not auth["ok"]:
            result["error"] = "root_login_failed"
            return result

        features = enable_features(base_url, auth)
        result["features"] = features
        if features.get("status") != 200:
            result["error"] = "feature_enable_failed"
            return result

        print("[phase] uploading shared multi-audio video", flush=True)
        upload = upload_video(base_url, auth, fixture_path)
        result["upload"] = upload
        body = upload.get("body") if isinstance(upload.get("body"), dict) else {}
        video = body.get("video") if isinstance(body.get("video"), dict) else {}
        share_url = str(video.get("share_url") or "")
        token = token_from_share_url(share_url)
        result["video_id"] = video.get("id")
        result["share_url"] = share_url
        result["share_token"] = token
        if not upload.get("ok") or not token:
            result["error"] = "upload_or_share_token_failed"
            return result

        print("[phase] unlocking share and waiting for playback", flush=True)
        share = unlock_share(base_url, token)
        result["share_unlock"] = {"ok": share["ok"], "csrf": share["csrf"], "unlock": share["unlock"], "share_session": share["share_session"]}
        if not share["ok"]:
            result["error"] = "share_unlock_failed"
            return result

        playback = wait_for_playback(base_url, share["session"], token, share["share_session"], timeout_seconds=args.wait_hls_seconds)
        result["playback"] = playback
        playback_body = playback.get("body") if isinstance(playback.get("body"), dict) else {}
        if not playback.get("ready"):
            result["error"] = "playback_not_ready"
            return result

        print("[phase] running HTTP concurrency checks", flush=True)
        http_phase = run_http_phase(base_url, token, share["share_session"], playback_body)
        result["http_phase"] = http_phase
        server_metrics = wait_realtime_proxy_metrics(runtime_root)
        server_metrics_summary = summarize_server_metric(server_metrics)
        result["server_metrics_path"] = str(realtime_proxy_metrics_path(runtime_root))
        result["server_metrics"] = server_metrics
        result["server_metrics_summary"] = server_metrics_summary
        result["ok"] = bool(http_phase.get("ok")) and all((server_metrics_summary.get("checks") or {}).values())
        return result
    except Exception as exc:
        result["error"] = exc.__class__.__name__
        result["message"] = str(exc)
        return result
    finally:
        was_running = server.poll() is None
        terminate_attempted = bool(was_running and not args.keep_server)
        if server.poll() is None and not args.keep_server:
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        result["cleanup"] = {
            "server_was_running": was_running,
            "terminate_attempted": terminate_attempted,
            "server_stopped": server.poll() is not None,
            "keep_server_requested": bool(args.keep_server),
        }
        json_report, md_report = write_reports(result, json_path)
        result["json"] = str(json_report)
        result["md"] = str(md_report)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live HTTP concurrency checks for Standard realtime proxy.")
    parser.add_argument("--runtime-root", default="/tmp/hackme_web_realtime_proxy_http_concurrency")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--fixture-size", default="1280x720")
    parser.add_argument("--fixture-rate", type=int, default=30)
    parser.add_argument("--fixture-video-bitrate", default="lossless")
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--wait-hls-seconds", type=int, default=90)
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--server-runner", choices=["flask", "gunicorn"], default="flask")
    parser.add_argument("--gunicorn-workers", type=int, default=2)
    parser.add_argument("--gunicorn-threads", type=int, default=1)
    parser.add_argument("--gunicorn-timeout", type=int, default=90)
    parser.add_argument("--keep-server", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run_probe(args)
    print(json.dumps({"ok": result.get("ok"), "json": result.get("json"), "md": result.get("md"), "base_url": result.get("base_url")}, ensure_ascii=False), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
