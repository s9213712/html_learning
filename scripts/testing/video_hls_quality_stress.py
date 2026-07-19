#!/usr/bin/env python3
"""Stress long-video upload, HLS preparation, and quality variant serving.

This probe is intentionally external to the app. It can:

1. Log in with multiple accounts and concurrently upload the same long video.
2. Confirm overloaded upload slots return an explicit `server_busy` response.
3. Wait for HLS background jobs to finish.
4. Measure each generated quality variant by fetching playlists and HLS
   segments while sampling `/api/version` latency.

Example:

    export HACKME_HLS_STRESS_ACCOUNTS_JSON
    python3 scripts/testing/video_hls_quality_stress.py \
      --base-url http://127.0.0.1:5017 \
      --video /tmp/hackme_video_quality_sample.mp4 \
      --db /tmp/hackme_video_quality_direct_5017/runtime/database/database.db \
      --runtime-marker /tmp/hackme_video_quality_direct_5017 \
      --upload --wait --measure

The script does not start or stop the server. Run it only against an isolated QA
runtime unless you intentionally want to stress a shared environment.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import urllib3


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
BROWSER_LATENCY_SCHEMA_VERSION = "hackme.browser-video-latency/v1"
BROWSER_FIRST_FRAME_SLA_MS = 8_000.0
BROWSER_SEEK_SLA_MS = 5_000.0
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class StreamingMultipartBody:
    """Small multipart/form-data stream that does not buffer large files."""

    def __init__(
        self,
        *,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
        content_type: str,
    ) -> None:
        self.boundary = f"----hackme-probe-{uuid.uuid4().hex}"
        self.content_type = f"multipart/form-data; boundary={self.boundary}"
        self._file_path = file_path
        self._file = None
        prefix_parts: list[bytes] = []
        boundary = self.boundary
        for name, value in fields.items():
            prefix_parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{_quote_header(name)}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        filename = _quote_header(file_path.name)
        prefix_parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{_quote_header(file_field)}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        self._prefix = b"".join(prefix_parts)
        self._suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
        self._prefix_offset = 0
        self._suffix_offset = 0
        self._phase = "prefix"
        self._length = len(self._prefix) + file_path.stat().st_size + len(self._suffix)

    def __len__(self) -> int:
        return self._length

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _read_prefix(self, limit: int) -> bytes:
        chunk = self._prefix[self._prefix_offset : self._prefix_offset + limit]
        self._prefix_offset += len(chunk)
        if self._prefix_offset >= len(self._prefix):
            self._phase = "file"
        return chunk

    def _read_file(self, limit: int) -> bytes:
        if self._file is None:
            self._file = self._file_path.open("rb")
        chunk = self._file.read(limit)
        if not chunk:
            self.close()
            self._phase = "suffix"
            return b""
        return chunk

    def _read_suffix(self, limit: int) -> bytes:
        chunk = self._suffix[self._suffix_offset : self._suffix_offset + limit]
        self._suffix_offset += len(chunk)
        if self._suffix_offset >= len(self._suffix):
            self._phase = "done"
        return chunk

    def read(self, size: int = -1) -> bytes:
        if self._phase == "done":
            return b""
        if size is None or size < 0:
            size = 1024 * 1024
        remaining = size
        chunks: list[bytes] = []
        while remaining > 0 and self._phase != "done":
            if self._phase == "prefix":
                chunk = self._read_prefix(remaining)
            elif self._phase == "file":
                chunk = self._read_file(remaining)
                if not chunk:
                    continue
            else:
                chunk = self._read_suffix(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


def _quote_header(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def utc_ms() -> int:
    return int(time.time() * 1000)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    index = min(len(data) - 1, max(0, int(math.ceil(len(data) * pct) - 1)))
    return round(data[index], 2)


def summarize_latencies(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0, "min": None, "p50": None, "p95": None, "max": None}
    return {
        "samples": len(values),
        "min": round(min(values), 2),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": round(max(values), 2),
        "mean": round(statistics.mean(values), 2),
    }


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    **kwargs: Any,
) -> tuple[int, Any, float]:
    started = time.perf_counter()
    try:
        response = session.request(method, url, **kwargs)
        elapsed = time.perf_counter() - started
        try:
            payload: Any = response.json()
        except Exception:
            payload = response.text[:1000]
        return response.status_code, payload, elapsed
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return 0, {"exception": exc.__class__.__name__, "message": str(exc)}, elapsed


def login(base_url: str, username: str, password: str) -> dict[str, Any]:
    session = requests.Session()
    if base_url.startswith("https://"):
        session.verify = False
    csrf_status, csrf_payload, csrf_elapsed = request_json(
        session,
        "GET",
        f"{base_url}/api/csrf-token",
        timeout=10,
    )
    token = ""
    if isinstance(csrf_payload, dict):
        token = str(csrf_payload.get("csrf_token") or csrf_payload.get("token") or "")
    login_status, login_payload, login_elapsed = request_json(
        session,
        "POST",
        f"{base_url}/api/login",
        json={"username": username, "password": password},
        headers={"X-CSRF-Token": token},
        timeout=20,
    )
    # Direct-gunicorn HTTP QA runs still receive cookies configured for the
    # normal HTTPS deployment. Loosen only the local requests session so this
    # probe can exercise authenticated routes without changing app behavior.
    if base_url.startswith("http://"):
        for cookie in session.cookies:
            cookie.secure = False
    token = str(session.cookies.get("csrf_token") or token)
    return {
        "session": session,
        "token": token,
        "ok": csrf_status == 200 and login_status == 200 and bool(token),
        "username": username,
        "csrf": {"status": csrf_status, "elapsed_s": csrf_elapsed, "payload": csrf_payload},
        "login": {"status": login_status, "elapsed_s": login_elapsed, "payload": login_payload},
    }


def upload_video(
    *,
    base_url: str,
    username: str,
    password: str,
    video_path: Path,
    privacy_mode: str,
    timeout_seconds: int,
    visibility: str = "public",
    share_password: str = "",
    share_max_views: int = 0,
) -> dict[str, Any]:
    auth = login(base_url, username, password)
    result: dict[str, Any] = {
        "username": username,
        "ok": False,
        "status": 0,
        "elapsed_s": 0.0,
        "csrf": auth["csrf"],
        "login": auth["login"],
    }
    if not auth["ok"]:
        result["error"] = "login_failed"
        return result
    title = f"stress-{username}-{utc_ms()}"
    upload_started_at_ms = utc_ms()
    started = time.perf_counter()
    mime_type = mimetypes.guess_type(video_path.name)[0] or "application/octet-stream"
    fields = {
            "title": title,
            "description": "Long video quality stress probe",
            "visibility": visibility,
            "privacy_mode": privacy_mode,
        }
    if visibility == "unlisted":
        fields["share_password"] = share_password
        fields["share_max_views"] = str(max(0, int(share_max_views)))
    body = StreamingMultipartBody(
        fields=fields,
        file_field="video",
        file_path=video_path,
        content_type=mime_type,
    )
    try:
        response = auth["session"].post(
            f"{base_url}/api/videos/upload",
            data=body,
            headers={
                "Content-Type": body.content_type,
                "Content-Length": str(len(body)),
                "X-CSRF-Token": auth["token"],
            },
            timeout=timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        try:
            payload: Any = response.json()
        except Exception:
            payload = response.text[:1000]
        result.update({
            "ok": response.status_code == 200 and isinstance(payload, dict) and bool(payload.get("ok")),
            "status": response.status_code,
            "elapsed_s": elapsed,
            "upload_started_at_ms": upload_started_at_ms,
            "upload_finished_at_ms": utc_ms(),
            "payload": payload,
        })
        if isinstance(payload, dict):
            video = payload.get("video") or {}
            file_info = payload.get("file") or {}
            stream_asset = payload.get("stream_asset") or {}
            result.update({
                "video_id": video.get("id"),
                "file_id": file_info.get("file_id") or video.get("cloud_file_id"),
                "stream_status": stream_asset.get("status"),
                "stream_warning": payload.get("stream_warning") or "",
                "share_url": video.get("share_url") or ((video.get("share_link") or {}).get("url")),
                "share_password_required": bool(video.get("share_password_required")),
            })
        return result
    except Exception as exc:
        result.update({
            "elapsed_s": time.perf_counter() - started,
            "upload_started_at_ms": upload_started_at_ms,
            "upload_finished_at_ms": utc_ms(),
            "error": exc.__class__.__name__,
            "message": str(exc),
        })
        return result
    finally:
        body.close()


def ps_snapshot(runtime_marker: str) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,ppid,pcpu,pmem,rss,nlwp,stat,comm,args"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        pid, ppid, pcpu, pmem, rss, nlwp, stat, comm, args = parts
        if (
            runtime_marker not in args
            and "hls_prepare_worker.py" not in args
            and comm != "ffmpeg"
        ):
            continue
        try:
            rows.append({
                "pid": int(pid),
                "ppid": int(ppid),
                "cpu_percent": float(pcpu),
                "mem_percent": float(pmem),
                "rss_kb": int(rss),
                "threads": int(nlwp),
                "stat": stat,
                "comm": comm,
                "args": args[:500],
            })
        except Exception:
            continue
    return rows


def db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def db_state(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"error": "db_missing", "db_path": str(db_path)}
    conn = db_connect(db_path)
    try:
        state: dict[str, Any] = {}
        for table in (
            "uploaded_files",
            "videos",
            "job_center_jobs",
            "media_stream_assets",
            "media_stream_variants",
            "media_stream_subtitles",
        ):
            try:
                state[f"{table}_count"] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            except Exception as exc:
                state[f"{table}_error"] = str(exc)
        try:
            state["videos"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, owner_user_id, title, cloud_file_id, status,
                           duration_seconds, created_at, updated_at
                    FROM videos
                    WHERE title LIKE 'stress-%'
                    ORDER BY id
                    """
                ).fetchall()
            ]
        except Exception as exc:
            state["videos_error"] = str(exc)
        try:
            state["jobs"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, job_uuid, owner_user_id, status, progress_percent,
                           stage, stage_detail, source_module, source_ref,
                           updated_at, error_message
                    FROM job_center_jobs
                    WHERE source_module='media_hls_prepare'
                    ORDER BY id
                    """
                ).fetchall()
            ]
        except Exception as exc:
            state["jobs_error"] = str(exc)
        try:
            state["assets"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, uploaded_file_id, status, master_manifest_path,
                           duration_seconds, error_message, updated_at
                    FROM media_stream_assets
                    ORDER BY id
                    """
                ).fetchall()
            ]
        except Exception as exc:
            state["assets_error"] = str(exc)
        try:
            state["variants"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT v.asset_id, a.uploaded_file_id, v.name, v.width,
                           v.height, v.bitrate, v.codec, COUNT(s.id) AS segments,
                           COALESCE(SUM(s.byte_size), 0) AS bytes
                    FROM media_stream_variants v
                    JOIN media_stream_assets a ON a.id=v.asset_id
                    LEFT JOIN media_stream_segments s ON s.variant_id=v.id
                    GROUP BY v.id
                    ORDER BY v.asset_id, v.id
                    """
                ).fetchall()
            ]
        except Exception as exc:
            state["variants_error"] = str(exc)
        try:
            state["subtitles"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT st.asset_id, a.uploaded_file_id, st.name, st.label,
                           st.language, st.codec, st.path, st.is_default
                    FROM media_stream_subtitles st
                    JOIN media_stream_assets a ON a.id=st.asset_id
                    ORDER BY st.asset_id, st.id
                    """
                ).fetchall()
            ]
        except Exception as exc:
            state["subtitles_error"] = str(exc)
        return state
    finally:
        conn.close()


def monitor_loop(
    *,
    base_url: str,
    db_path: Path,
    runtime_marker: str,
    interval: float,
    stop_event: threading.Event,
    samples: list[dict[str, Any]],
) -> None:
    session = requests.Session()
    if base_url.startswith("https://"):
        session.verify = False
    while not stop_event.is_set():
        sample: dict[str, Any] = {"t_ms": utc_ms()}
        started = time.perf_counter()
        try:
            response = session.get(f"{base_url}/api/version", timeout=5)
            sample["version_status"] = response.status_code
            sample["version_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        except Exception as exc:
            sample["version_status"] = 0
            sample["version_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            sample["version_error"] = f"{exc.__class__.__name__}: {exc}"
        sample["db"] = db_state(db_path)
        sample["processes"] = ps_snapshot(runtime_marker)
        samples.append(sample)
        stop_event.wait(interval)


def summarize_monitor(samples: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(s["version_elapsed_ms"]) for s in samples if s.get("version_status") == 200]
    failures = [s for s in samples if s.get("version_status") != 200]
    max_rss_kb = 0
    max_threads = 0
    ffmpeg_sample_count = 0
    worker_sample_count = 0
    for sample in samples:
        for proc in sample.get("processes") or []:
            max_rss_kb = max(max_rss_kb, int(proc.get("rss_kb") or 0))
            max_threads = max(max_threads, int(proc.get("threads") or 0))
            if proc.get("comm") == "ffmpeg":
                ffmpeg_sample_count += 1
            if "hls_prepare_worker.py" in str(proc.get("args") or ""):
                worker_sample_count += 1
    return {
        "samples": len(samples),
        "version_latency_ms": summarize_latencies(latencies),
        "version_failures": len(failures),
        "version_failure_samples": failures[:5],
        "max_rss_kb_seen_per_process": max_rss_kb,
        "max_threads_seen_per_process": max_threads,
        "ffmpeg_process_sample_count": ffmpeg_sample_count,
        "hls_worker_process_sample_count": worker_sample_count,
        "last_db": samples[-1].get("db") if samples else {},
    }


def uploaded_target_ids(upload_phase: dict[str, Any] | None) -> tuple[set[int], set[str]]:
    video_ids: set[int] = set()
    file_ids: set[str] = set()
    for item in (upload_phase or {}).get("uploads") or []:
        try:
            video_id = int(item.get("video_id") or 0)
        except (TypeError, ValueError):
            video_id = 0
        file_id = str(item.get("file_id") or "").strip()
        if video_id > 0:
            video_ids.add(video_id)
        if file_id:
            file_ids.add(file_id)
    return video_ids, file_ids


def filter_db_state_for_uploads(state: dict[str, Any], upload_phase: dict[str, Any] | None) -> dict[str, Any]:
    video_ids, file_ids = uploaded_target_ids(upload_phase)
    if not video_ids and not file_ids:
        if upload_phase is None:
            return state
        filtered = dict(state)
        for key in ("videos", "jobs", "assets", "variants", "subtitles"):
            filtered[key] = []
        filtered["target_video_ids"] = []
        filtered["target_file_ids"] = []
        return filtered
    filtered = dict(state)
    filtered["videos"] = [row for row in state.get("videos") or [] if int(row.get("id") or 0) in video_ids]
    filtered["jobs"] = [
        row
        for row in state.get("jobs") or []
        if str(row.get("source_ref") or "").removeprefix("media_stream:") in file_ids
    ]
    filtered["assets"] = [row for row in state.get("assets") or [] if str(row.get("uploaded_file_id") or "") in file_ids]
    filtered["variants"] = [row for row in state.get("variants") or [] if str(row.get("uploaded_file_id") or "") in file_ids]
    filtered["subtitles"] = [row for row in state.get("subtitles") or [] if str(row.get("uploaded_file_id") or "") in file_ids]
    filtered["target_video_ids"] = sorted(video_ids)
    filtered["target_file_ids"] = sorted(file_ids)
    return filtered


def run_upload_phase(args: argparse.Namespace) -> dict[str, Any]:
    video_path = Path(args.video)
    accounts = parse_accounts(args.accounts, os.environ.get("HACKME_HLS_STRESS_ACCOUNTS_JSON", ""))
    if not video_path.exists():
        raise SystemExit(f"video not found: {video_path}")
    samples: list[dict[str, Any]] = []
    stop_event = threading.Event()
    monitor = threading.Thread(
        target=monitor_loop,
        kwargs={
            "base_url": args.base_url,
            "db_path": Path(args.db),
            "runtime_marker": args.runtime_marker,
            "interval": args.monitor_interval,
            "stop_event": stop_event,
            "samples": samples,
        },
        daemon=True,
    )
    monitor.start()
    started_ms = utc_ms()
    uploads: list[dict[str, Any]] = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(accounts)))
    futures = {
        executor.submit(
            upload_video,
            base_url=args.base_url,
            username=username,
            password=password,
            video_path=video_path,
            privacy_mode=args.privacy_mode,
            timeout_seconds=args.upload_timeout_seconds,
            visibility=args.visibility,
            share_password=args.share_password,
            share_max_views=args.share_max_views,
        ): username
        for username, password in accounts
    }
    pending = set(futures)
    try:
        for future in concurrent.futures.as_completed(futures, timeout=args.upload_timeout_seconds + 60):
            pending.discard(future)
            try:
                uploads.append(future.result())
            except Exception as exc:
                uploads.append({
                    "username": futures[future],
                    "ok": False,
                    "error": exc.__class__.__name__,
                    "message": str(exc),
                })
    except concurrent.futures.TimeoutError:
        for future in pending:
            future.cancel()
            uploads.append({
                "username": futures[future],
                "ok": False,
                "error": "upload_phase_timeout",
                "message": f"parallel upload did not complete within {args.upload_timeout_seconds + 60}s",
            })
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if args.post_upload_observe_seconds > 0:
        time.sleep(args.post_upload_observe_seconds)
    stop_event.set()
    monitor.join(timeout=5)
    result = {
        "phase": "upload",
        "ok": len(uploads) == len(accounts) and bool(uploads) and all(bool(item.get("ok")) for item in uploads),
        "base_url": args.base_url,
        "video": str(video_path),
        "video_size_bytes": video_path.stat().st_size,
        "privacy_mode": args.privacy_mode,
        "visibility": args.visibility,
        "accounts": [username for username, _ in accounts],
        "started_at_ms": started_ms,
        "finished_at_ms": utc_ms(),
        "uploads": uploads,
        "monitor_summary": summarize_monitor(samples),
        "monitor_samples_tail": samples[-8:],
    }
    return result


def format_wait_status(state: dict[str, Any], processes: list[dict[str, Any]], elapsed_s: int) -> str:
    jobs = [
        f"job#{job.get('id')} {job.get('status')} {job.get('progress_percent')}% {job.get('stage')}"
        for job in state.get("jobs") or []
    ]
    variants = [
        f"{str(item.get('uploaded_file_id') or '')[:6]}:{item.get('name')} {item.get('height')}p "
        f"{round(int(item.get('bytes') or 0) / 1024 / 1024, 1)}MB"
        for item in state.get("variants") or []
    ]
    ffmpeg = [
        {
            "pid": proc["pid"],
            "cpu_percent": proc["cpu_percent"],
            "rss_mb": round(proc["rss_kb"] / 1024, 1),
            "threads": proc["threads"],
        }
        for proc in processes
        if proc.get("comm") == "ffmpeg"
    ]
    return json.dumps({"elapsed_s": elapsed_s, "jobs": jobs, "variants": variants, "ffmpeg": ffmpeg}, ensure_ascii=False)


def wait_for_hls(args: argparse.Namespace, upload_phase: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.time()
    history: list[dict[str, Any]] = []
    last_signature = ""
    last_change_at = time.time()
    while True:
        state = filter_db_state_for_uploads(db_state(Path(args.db)), upload_phase)
        processes = ps_snapshot(args.runtime_marker)
        elapsed_s = int(time.time() - started)
        history.append({"t_ms": utc_ms(), "state": state, "processes": processes})
        if args.print_wait_status:
            print(format_wait_status(state, processes, elapsed_s), flush=True)
        jobs = state.get("jobs") or []
        if not jobs:
            return {
                "phase": "wait",
                "ok": False,
                "error": "no_hls_jobs",
                "elapsed_s": elapsed_s,
                "final_state": state,
                "final_processes": processes,
                "history_tail": history[-10:],
            }
        if jobs and all(str(job.get("status") or "") in TERMINAL_JOB_STATUSES for job in jobs):
            failed_jobs = [job for job in jobs if str(job.get("status") or "") != "succeeded"]
            return {
                "phase": "wait",
                "ok": not failed_jobs,
                "error": "hls_job_terminal_failure" if failed_jobs else "",
                "failed_jobs": failed_jobs,
                "elapsed_s": elapsed_s,
                "final_state": state,
                "final_processes": processes,
                "history_tail": history[-10:],
            }
        active_jobs = [
            job for job in jobs
            if str(job.get("status") or "") not in TERMINAL_JOB_STATUSES
        ]
        signature = json.dumps(
            [
                {
                    "id": job.get("id"),
                    "status": job.get("status"),
                    "progress_percent": job.get("progress_percent"),
                    "stage": job.get("stage"),
                    "updated_at": job.get("updated_at"),
                }
                for job in active_jobs
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        if signature != last_signature:
            last_signature = signature
            last_change_at = time.time()
        active_processes = [
            proc for proc in processes
            if proc.get("comm") == "ffmpeg" or "hls_prepare_worker.py" in str(proc.get("args") or "")
        ]
        if (
            active_jobs
            and not active_processes
            and time.time() - last_change_at >= max(60, int(args.orphan_grace_seconds))
        ):
            return {
                "phase": "wait",
                "ok": False,
                "error": "orphaned_hls_job",
                "elapsed_s": elapsed_s,
                "final_state": state,
                "final_processes": processes,
                "history_tail": history[-10:],
            }
        if elapsed_s >= args.wait_timeout_seconds:
            return {
                "phase": "wait",
                "ok": False,
                "error": "timeout",
                "elapsed_s": elapsed_s,
                "final_state": state,
                "final_processes": processes,
                "history_tail": history[-10:],
            }
        time.sleep(args.wait_interval_seconds)


def parse_playlist(text: str) -> list[str]:
    paths: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                paths.append(match.group(1))
            continue
        if line.startswith("#"):
            continue
        paths.append(line)
    return paths


def timed_get(session: requests.Session, url: str, token: str, timeout: int = 30) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = session.get(url, headers={"X-CSRF-Token": token}, timeout=timeout)
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "ok": response.status_code == 200,
            "status": response.status_code,
            "elapsed_ms": round(elapsed, 2),
            "bytes": len(response.content),
            "text": response.text[:300] if response.status_code != 200 else "",
        }
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "ok": False,
            "status": 0,
            "elapsed_ms": round(elapsed, 2),
            "bytes": 0,
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def sample_version_latency(base_url: str, stop_time: float, samples: list[float], errors: list[str]) -> None:
    session = requests.Session()
    if base_url.startswith("https://"):
        session.verify = False
    while time.time() < stop_time:
        started = time.perf_counter()
        try:
            response = session.get(f"{base_url}/api/version", timeout=5)
            elapsed = (time.perf_counter() - started) * 1000
            if response.status_code == 200:
                samples.append(elapsed)
            else:
                errors.append(f"status:{response.status_code}")
        except Exception as exc:
            errors.append(f"{exc.__class__.__name__}: {exc}")
        time.sleep(0.25)


def choose_segment_paths(paths: list[str], max_segments: int) -> list[str]:
    media_paths = [path for path in paths if path != "init.mp4"]
    chosen: list[str] = []
    if "init.mp4" in paths:
        chosen.append("init.mp4")
    if media_paths:
        indexes = sorted({0, len(media_paths) // 2, max(0, len(media_paths) - 1)})
        chosen.extend(media_paths[index] for index in indexes)
        for path in media_paths:
            if len(chosen) >= max_segments:
                break
            if path not in chosen:
                chosen.append(path)
    return chosen[:max_segments]


def measure_variant_burst(
    *,
    base_url: str,
    session: requests.Session,
    token: str,
    video_id: int,
    variant_name: str,
    paths: list[str],
    concurrency: int,
    max_segments: int,
) -> dict[str, Any]:
    chosen_paths = choose_segment_paths(paths, max_segments)
    urls = [f"{base_url}/api/videos/{video_id}/hls/{variant_name}/{path}" for path in chosen_paths]
    version_samples: list[float] = []
    version_errors: list[str] = []
    stop_time = time.time() + 15
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as monitor_pool:
        monitor_pool.submit(sample_version_latency, base_url, stop_time, version_samples, version_errors)
        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            segment_results = list(pool.map(lambda url: timed_get(session, url, token, timeout=60), urls))
        elapsed_ms = (time.perf_counter() - started) * 1000
    successful_latencies = [float(item["elapsed_ms"]) for item in segment_results if item.get("status") == 200]
    return {
        "requested_segments": len(urls),
        "ok_segments": sum(1 for item in segment_results if item.get("status") == 200),
        "bytes_total": sum(int(item.get("bytes") or 0) for item in segment_results),
        "burst_elapsed_ms": round(elapsed_ms, 2),
        "segment_latency_ms": summarize_latencies(successful_latencies),
        "version_latency_during_burst_ms": {
            **summarize_latencies(version_samples),
            "errors": version_errors[:5],
        },
        "segment_samples": segment_results,
    }


def measure_subtitle_tracks(
    *,
    base_url: str,
    session: requests.Session,
    token: str,
    tracks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for track in tracks:
        url = str(track.get("url") or "")
        item = {
            "name": track.get("name"),
            "label": track.get("label"),
            "language": track.get("language"),
            "is_default": bool(track.get("is_default")),
            "url": url,
        }
        if not url:
            item.update({"ok": False, "error": "missing_url"})
            results.append(item)
            continue
        started = time.perf_counter()
        try:
            response = session.get(f"{base_url}{url}", headers={"X-CSRF-Token": token}, timeout=30)
            elapsed_ms = (time.perf_counter() - started) * 1000
            content = response.content
            preview = content[:256].decode("utf-8", errors="replace")
            looks_like_webvtt = preview.lstrip("\ufeff\r\n\t ").startswith("WEBVTT")
            item.update({
                "ok": response.status_code == 200 and looks_like_webvtt,
                "status": response.status_code,
                "elapsed_ms": round(elapsed_ms, 2),
                "bytes": len(content),
                "looks_like_webvtt": looks_like_webvtt,
                "preview": preview[:120],
            })
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            item.update({
                "ok": False,
                "status": 0,
                "elapsed_ms": round(elapsed_ms, 2),
                "bytes": 0,
                "looks_like_webvtt": False,
                "error": f"{exc.__class__.__name__}: {exc}",
            })
        if not item["ok"]:
            item.setdefault("error", "subtitle_fetch_or_format_failed")
        results.append(item)
    return results


def measure_hls_variants(args: argparse.Namespace, upload_phase: dict[str, Any] | None = None) -> dict[str, Any]:
    auth = login(args.base_url, args.measure_username, args.measure_password)
    if not auth["ok"]:
        return {"phase": "measure", "ok": False, "error": "login_failed", "login": auth["login"]}
    session = auth["session"]
    token = auth["token"]
    state = filter_db_state_for_uploads(db_state(Path(args.db)), upload_phase)
    measurements: list[dict[str, Any]] = []
    phase_ok = True
    videos = state.get("videos") or []
    if not videos:
        phase_ok = False
    for video in videos:
        video_id = int(video["id"])
        playback = timed_get(session, f"{args.base_url}/api/videos/{video_id}/playback", token, timeout=20)
        entry: dict[str, Any] = {
            "video_id": video_id,
            "title": video.get("title"),
            "playback": playback,
            "variants": [],
            "subtitles": [],
        }
        variants: list[dict[str, Any]] = []
        if playback.get("status") != 200:
            entry["error"] = "playback_not_available"
            phase_ok = False
        else:
            status, payload, elapsed = request_json(
                session,
                "GET",
                f"{args.base_url}/api/videos/{video_id}/playback",
                headers={"X-CSRF-Token": token},
                timeout=20,
            )
            entry["playback_json"] = {
                "status": status,
                "elapsed_ms": round(elapsed * 1000, 2),
                "payload": {
                    "mode": payload.get("mode") if isinstance(payload, dict) else None,
                    "streaming_ready": payload.get("streaming_ready") if isinstance(payload, dict) else None,
                    "duration_seconds": (
                        payload.get("duration_seconds") or ((payload.get("status") or {}).get("duration_seconds"))
                        if isinstance(payload, dict)
                        else None
                    ),
                    "variants": payload.get("variants") if isinstance(payload, dict) else [],
                    "audio_tracks": payload.get("audio_tracks") if isinstance(payload, dict) else [],
                    "subtitles": payload.get("subtitles") if isinstance(payload, dict) else [],
                },
            }
            if isinstance(payload, dict):
                variants = list(payload.get("variants") or [])
                audio_tracks = list(payload.get("audio_tracks") or [])
                subtitle_tracks = list(payload.get("subtitles") or [])
                if not payload.get("streaming_ready") or not variants:
                    entry["stream_error"] = "streaming_not_ready_or_variants_missing"
                    phase_ok = False
                if len(audio_tracks) < max(0, int(args.expect_audio_tracks)):
                    entry["audio_track_error"] = {
                        "expected": int(args.expect_audio_tracks),
                        "actual": len(audio_tracks),
                    }
                    phase_ok = False
                entry["subtitles"] = measure_subtitle_tracks(
                    base_url=args.base_url,
                    session=session,
                    token=token,
                    tracks=subtitle_tracks,
                )
                if args.expect_subtitles and not entry["subtitles"]:
                    entry["subtitle_error"] = "expected_subtitles_missing"
                    phase_ok = False
                if any(not item.get("ok") or not item.get("looks_like_webvtt") for item in entry["subtitles"]):
                    phase_ok = False
        for variant in variants:
            name = str(variant.get("name") or "")
            playlist_url = str(variant.get("playlist_url") or "")
            playlist = timed_get(session, f"{args.base_url}{playlist_url}", token, timeout=20)
            variant_entry: dict[str, Any] = {
                "name": name,
                "label": variant.get("label"),
                "height": variant.get("height"),
                "declared_bitrate": variant.get("bitrate"),
                "playlist": playlist,
            }
            if playlist.get("status") == 200:
                response = session.get(f"{args.base_url}{playlist_url}", headers={"X-CSRF-Token": token}, timeout=20)
                paths = parse_playlist(response.text)
                variant_entry["playlist_paths"] = len(paths)
                media_segment_count = len([path for path in paths if path != "init.mp4"])
                variant_entry["media_segment_count"] = media_segment_count
                if media_segment_count < max(1, int(args.minimum_segments_per_variant)):
                    variant_entry["segment_count_error"] = {
                        "expected_minimum": int(args.minimum_segments_per_variant),
                        "actual": media_segment_count,
                    }
                    phase_ok = False
                variant_entry["burst"] = measure_variant_burst(
                    base_url=args.base_url,
                    session=session,
                    token=token,
                    video_id=video_id,
                    variant_name=name,
                    paths=paths,
                    concurrency=args.segment_concurrency,
                    max_segments=args.max_segments_per_variant,
                )
                burst = variant_entry["burst"]
                if int(burst.get("ok_segments") or 0) < int(burst.get("requested_segments") or 0):
                    phase_ok = False
            else:
                phase_ok = False
            entry["variants"].append(variant_entry)
        measurements.append(entry)
    return {
        "phase": "measure",
        "ok": phase_ok,
        "state": state,
        "measurements": measurements,
        "processes_after_measure": ps_snapshot(args.runtime_marker),
    }


def share_token_from_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    path = parsed.path or str(value or "").split("?", 1)[0].split("#", 1)[0]
    marker = "/shared/videos/"
    if marker not in path:
        return ""
    return path.split(marker, 1)[1].split("/", 1)[0].strip()


def anonymous_session_with_csrf(base_url: str) -> tuple[requests.Session, str, dict[str, Any]]:
    session = requests.Session()
    session.verify = not base_url.startswith("https://")
    status, payload, elapsed = request_json(session, "GET", f"{base_url}/api/csrf-token", timeout=20)
    token = ""
    if isinstance(payload, dict):
        token = str(payload.get("csrf_token") or payload.get("token") or "")
    token = str(session.cookies.get("csrf_token") or token)
    return session, token, {"status": status, "elapsed_ms": round(elapsed * 1000, 2), "ok": status == 200 and bool(token)}


def fetch_text_result(session: requests.Session, url: str, *, timeout: int = 30) -> tuple[dict[str, Any], str]:
    started = time.perf_counter()
    try:
        response = session.get(url, timeout=timeout)
        text = response.text
        return {
            "ok": response.status_code == 200,
            "status": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "bytes": len(response.content),
            "content_type": response.headers.get("Content-Type") or "",
        }, text
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "bytes": 0,
            "error": f"{exc.__class__.__name__}: {exc}",
        }, ""


def browser_seek_shared_video(
    *,
    base_url: str,
    share_url: str,
    share_password: str,
    mobile: bool,
    minimum_duration_seconds: float,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {"ok": False, "error": f"playwright_import_failed:{exc}"}
    viewport = {"width": 390, "height": 844} if mobile else {"width": 1366, "height": 768}
    errors: list[str] = []
    result: dict[str, Any] = {
        "schema_version": BROWSER_LATENCY_SCHEMA_VERSION,
        "ok": False,
        "viewport": "mobile" if mobile else "desktop",
        "emulation": {
            "is_mobile": bool(mobile),
            "has_touch": bool(mobile),
            "viewport": viewport,
        },
        "latency_thresholds_ms": {
            "first_frame": BROWSER_FIRST_FRAME_SLA_MS,
            "random_seek_terminal": BROWSER_SEEK_SLA_MS,
        },
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=True,
            viewport=viewport,
            is_mobile=mobile,
            has_touch=mobile,
            device_scale_factor=2 if mobile else 1,
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(f"pageerror:{error}"))
        page.on(
            "console",
            lambda message: errors.append(f"console.{message.type}:{message.text}")
            if message.type == "error" and "favicon" not in message.text.lower()
            else None,
        )
        try:
            target = f"{base_url}{share_url}" if share_url.startswith("/") else share_url
            latency_origin = "share_page_navigation"
            first_frame_origin_started = time.perf_counter()
            page.goto(target, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("#share-password-form:not(.hidden), #player-host:not(.hidden)", timeout=30_000)
            if page.locator("#share-password-form:not(.hidden)").count():
                page.fill("#share-password", share_password)
                latency_origin = "unlock_submit"
                first_frame_origin_started = time.perf_counter()
                page.locator("#share-password-form button[type=submit]").click()
            page.wait_for_selector("#player-host:not(.hidden) #shared-player", timeout=60_000)
            page.wait_for_function(
                """minimum => {
                    const video = document.querySelector('#shared-player');
                    return !!video && Number.isFinite(video.duration) && video.duration >= minimum && video.readyState >= 1;
                }""",
                arg=max(1.0, float(minimum_duration_seconds)),
                timeout=90_000,
            )
            first_frame = page.evaluate(
                """async ({ timeoutMs }) => {
                    const video = document.querySelector('#shared-player');
                    const startedAt = performance.now();
                    let playingObserved = false;
                    let frameObserved = false;
                    let frameMetadata = null;
                    let playError = '';
                    let terminalEvent = 'timeout';
                    let resolveTerminal;
                    const terminal = new Promise(resolve => { resolveTerminal = resolve; });
                    const maybeFinish = () => {
                        if (!playingObserved || !frameObserved) return;
                        terminalEvent = 'playing_and_video_frame';
                        resolveTerminal();
                    };
                    const frameCallbackSupported = typeof video.requestVideoFrameCallback === 'function';
                    video.addEventListener('playing', () => {
                        playingObserved = true;
                        if (frameCallbackSupported) {
                            video.requestVideoFrameCallback((_now, metadata) => {
                                frameObserved = true;
                                frameMetadata = {
                                    mediaTime: Number(metadata?.mediaTime || 0),
                                    presentedFrames: Number(metadata?.presentedFrames || 0),
                                    width: Number(metadata?.width || 0),
                                    height: Number(metadata?.height || 0),
                                };
                                maybeFinish();
                            });
                        }
                        maybeFinish();
                    }, { once: true });
                    const timer = setTimeout(() => resolveTerminal(), timeoutMs);
                    video.muted = true;
                    try {
                        const playResult = video.play();
                        if (playResult && typeof playResult.catch === 'function') {
                            playResult.catch(error => { playError = String(error?.message || error); });
                        }
                    } catch (error) {
                        playError = String(error?.message || error);
                    }
                    await terminal;
                    clearTimeout(timer);
                    return {
                        terminal_event: terminalEvent,
                        playing_observed: playingObserved,
                        frame_observed: frameObserved,
                        frame_observation_method: frameCallbackSupported ? 'requestVideoFrameCallback' : 'unsupported',
                        frame_metadata: frameMetadata,
                        play_to_frame_latency_ms: Math.round((performance.now() - startedAt) * 100) / 100,
                        current_time: Number(video.currentTime || 0),
                        ready_state: Number(video.readyState || 0),
                        network_state: Number(video.networkState || 0),
                        paused: !!video.paused,
                        play_error: playError,
                    };
                }""",
                {"timeoutMs": 12_000},
            )
            first_frame["origin"] = latency_origin
            first_frame["elapsed_ms"] = round(
                (time.perf_counter() - first_frame_origin_started) * 1000,
                2,
            )
            seek = page.evaluate(
                """async () => {
                    const video = document.querySelector('#shared-player');
                    video.muted = true;
                    const before = Number(video.currentTime || 0);
                    const duration = Number(video.duration || 0);
                    const randomValues = new Uint32Array(1);
                    crypto.getRandomValues(randomValues);
                    const randomUnit = Number(randomValues[0]) / 0xffffffff;
                    const targetRatio = 0.15 + (randomUnit * 0.70);
                    const target = Math.max(5, Math.min(duration - 2, duration * targetRatio));
                    const startedAt = performance.now();
                    let terminalEvent = 'timeout';
                    let seekedObserved = false;
                    let frameObserved = false;
                    let frameMetadata = null;
                    let playError = '';
                    let terminalSettled = false;
                    let resolveTerminal;
                    const waited = new Promise(resolve => { resolveTerminal = resolve; });
                    const frameCallbackSupported = typeof video.requestVideoFrameCallback === 'function';
                    const waitForTargetFrame = () => {
                        if (!frameCallbackSupported || terminalSettled) return;
                        video.requestVideoFrameCallback((_now, metadata) => {
                            if (terminalSettled) return;
                            const current = Number(video.currentTime || 0);
                            if (Math.abs(current - target) < 20) {
                                frameObserved = true;
                                frameMetadata = {
                                    mediaTime: Number(metadata?.mediaTime || 0),
                                    presentedFrames: Number(metadata?.presentedFrames || 0),
                                    width: Number(metadata?.width || 0),
                                    height: Number(metadata?.height || 0),
                                };
                                terminalEvent = 'seeked_and_video_frame';
                                terminalSettled = true;
                                resolveTerminal();
                                return;
                            }
                            waitForTargetFrame();
                        });
                    };
                    video.addEventListener('seeked', () => {
                        seekedObserved = true;
                        waitForTargetFrame();
                    }, {once: true});
                    const timer = setTimeout(() => {
                        terminalSettled = true;
                        resolveTerminal();
                    }, 30_000);
                    video.currentTime = target;
                    if (video.paused) {
                        try {
                            const playResult = video.play();
                            if (playResult && typeof playResult.catch === 'function') {
                                playResult.catch(error => { playError = String(error?.message || error); });
                            }
                        } catch (error) { playError = String(error?.message || error); }
                    }
                    await waited;
                    clearTimeout(timer);
                    return {
                        before,
                        duration,
                        target,
                        target_ratio: targetRatio,
                        random_source: 'crypto.getRandomValues',
                        random_sample_uint32: Number(randomValues[0]),
                        currentTime: Number(video.currentTime || 0),
                        readyState: Number(video.readyState || 0),
                        networkState: Number(video.networkState || 0),
                        paused: !!video.paused,
                        terminal_event: terminalEvent,
                        terminal_latency_ms: Math.round((performance.now() - startedAt) * 100) / 100,
                        seeked_observed: seekedObserved,
                        frame_observed: frameObserved,
                        frame_observation_method: frameCallbackSupported ? 'requestVideoFrameCallback' : 'unsupported',
                        frame_metadata: frameMetadata,
                        play_error: playError,
                    };
                }"""
            )
            layout = page.evaluate(
                """() => ({
                    viewportWidth: document.documentElement.clientWidth,
                    scrollWidth: document.documentElement.scrollWidth,
                    playerWidth: Math.round(document.querySelector('#shared-player')?.getBoundingClientRect().width || 0),
                    playerHeight: Math.round(document.querySelector('#shared-player')?.getBoundingClientRect().height || 0),
                })"""
            )
            fatal_errors = list(errors)
            first_frame_metadata = first_frame.get("frame_metadata") or {}
            first_frame_ok = (
                first_frame.get("terminal_event") == "playing_and_video_frame"
                and first_frame.get("playing_observed") is True
                and first_frame.get("frame_observed") is True
                and first_frame.get("frame_observation_method") == "requestVideoFrameCallback"
                and 0 < float(first_frame.get("elapsed_ms") or 0) <= BROWSER_FIRST_FRAME_SLA_MS
                and 0 < float(first_frame.get("play_to_frame_latency_ms") or 0) <= BROWSER_FIRST_FRAME_SLA_MS
                and int(first_frame.get("ready_state") or 0) >= 2
                and first_frame.get("paused") is False
                and int(first_frame_metadata.get("presentedFrames") or 0) > 0
                and int(first_frame_metadata.get("width") or 0) > 0
                and int(first_frame_metadata.get("height") or 0) > 0
                and not str(first_frame.get("play_error") or "")
            )
            seek_frame_metadata = seek.get("frame_metadata") or {}
            seek_ok = (
                float(seek.get("duration") or 0) >= max(1.0, float(minimum_duration_seconds))
                and abs(float(seek.get("currentTime") or 0) - float(seek.get("target") or 0)) < 20
                and int(seek.get("readyState") or 0) >= 2
                and seek.get("terminal_event") == "seeked_and_video_frame"
                and seek.get("seeked_observed") is True
                and seek.get("frame_observed") is True
                and seek.get("frame_observation_method") == "requestVideoFrameCallback"
                and seek.get("random_source") == "crypto.getRandomValues"
                and 0.15 <= float(seek.get("target_ratio") or -1) <= 0.85
                and 0 < float(seek.get("terminal_latency_ms") or 0) <= BROWSER_SEEK_SLA_MS
                and seek.get("paused") is False
                and int(seek_frame_metadata.get("presentedFrames") or 0) > 0
                and int(seek_frame_metadata.get("width") or 0) > 0
                and int(seek_frame_metadata.get("height") or 0) > 0
                and not str(seek.get("play_error") or "")
            )
            layout_ok = (
                int(layout.get("playerWidth") or 0) > 0
                and int(layout.get("playerHeight") or 0) > 0
                and int(layout.get("scrollWidth") or 0) <= int(layout.get("viewportWidth") or 0) + 2
            )
            result.update({
                "ok": bool(first_frame_ok and seek_ok and layout_ok and not fatal_errors),
                "first_frame": first_frame,
                "seek": seek,
                "layout": layout,
                "fatal_errors": fatal_errors[:20],
                "console_errors": errors[:50],
            })
            return result
        except Exception as exc:
            result.update({"error": f"{exc.__class__.__name__}: {exc}", "console_errors": errors[:50]})
            return result
        finally:
            context.close()
            browser.close()


def verify_share_links(args: argparse.Namespace, upload_phase: dict[str, Any] | None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    phase_ok = True
    successful = [item for item in (upload_phase or {}).get("uploads") or [] if item.get("ok")]
    for index, upload in enumerate(successful):
        share_url = str(upload.get("share_url") or "")
        share_token = share_token_from_url(share_url)
        video_id = int(upload.get("video_id") or 0)
        username = str(upload.get("username") or "")
        row: dict[str, Any] = {
            "username": username,
            "video_id": video_id,
            "share_token_present": bool(share_token),
        }
        if not share_token or video_id <= 0:
            row["error"] = "upload_missing_share_token_or_video_id"
            row["ok"] = False
            rows.append(row)
            phase_ok = False
            continue

        anonymous, csrf, csrf_result = anonymous_session_with_csrf(args.base_url)
        row["csrf"] = csrf_result
        locked_status, locked_payload, _ = request_json(
            anonymous,
            "GET",
            f"{args.base_url}/api/videos/shared/{share_token}/playback",
            timeout=30,
        )
        wrong_status, wrong_payload, _ = request_json(
            anonymous,
            "POST",
            f"{args.base_url}/api/videos/shared/{share_token}/unlock",
            json={"password": "not-the-campaign-password"},
            headers={"X-CSRF-Token": csrf},
            timeout=30,
        )
        unlock_status, unlock_payload, unlock_elapsed = request_json(
            anonymous,
            "POST",
            f"{args.base_url}/api/videos/shared/{share_token}/unlock",
            json={"password": args.share_password},
            headers={"X-CSRF-Token": csrf},
            timeout=30,
        )
        share_session = str(unlock_payload.get("share_session_id") or "") if isinstance(unlock_payload, dict) else ""
        playback_status, playback_payload, playback_elapsed = request_json(
            anonymous,
            "GET",
            f"{args.base_url}/api/videos/shared/{share_token}/playback",
            params={"share_session": share_session},
            timeout=30,
        )
        playback_payload = playback_payload if isinstance(playback_payload, dict) else {}
        master_url = str(playback_payload.get("master_url") or "")
        master_result, master_text = fetch_text_result(
            anonymous,
            f"{args.base_url}{master_url}" if master_url.startswith("/") else master_url,
            timeout=60,
        ) if master_url else ({"ok": False, "status": 0, "error": "master_url_missing"}, "")
        variant_result: dict[str, Any] = {"ok": False, "error": "variant_missing"}
        segment_results: list[dict[str, Any]] = []
        variants = list(playback_payload.get("variants") or [])
        if variants:
            playlist_url = str(variants[0].get("playlist_url") or "")
            absolute_playlist = f"{args.base_url}{playlist_url}" if playlist_url.startswith("/") else playlist_url
            variant_result, variant_text = fetch_text_result(anonymous, absolute_playlist, timeout=60)
            segment_paths = parse_playlist(variant_text)
            chosen = choose_segment_paths(segment_paths, 5)
            for relative in chosen:
                parsed_playlist = urlparse(absolute_playlist)
                base_path = parsed_playlist.path.rsplit("/", 1)[0]
                if relative.startswith("/"):
                    segment_url = f"{parsed_playlist.scheme}://{parsed_playlist.netloc}{relative}"
                else:
                    segment_url = f"{parsed_playlist.scheme}://{parsed_playlist.netloc}{base_path}/{relative}"
                segment_results.append(timed_get(anonymous, segment_url, "", timeout=60))
            variant_result["playlist_paths"] = len(segment_paths)
            variant_result["sampled_segments"] = len(segment_results)

        subtitle_results = measure_subtitle_tracks(
            base_url=args.base_url,
            session=anonymous,
            token="",
            tracks=list(playback_payload.get("subtitles") or []),
        )
        browser_checks: list[dict[str, Any]] = []
        if args.browser_seek and index == 0:
            browser_checks.append(browser_seek_shared_video(
                base_url=args.base_url,
                share_url=share_url,
                share_password=args.share_password,
                mobile=False,
                minimum_duration_seconds=args.minimum_source_duration_seconds,
            ))
            if args.browser_mobile:
                browser_checks.append(browser_seek_shared_video(
                    base_url=args.base_url,
                    share_url=share_url,
                    share_password=args.share_password,
                    mobile=True,
                    minimum_duration_seconds=args.minimum_source_duration_seconds,
                ))

        owner = login(args.base_url, username, next(
            (password for account_name, password in parse_accounts(args.accounts, os.environ.get("HACKME_HLS_STRESS_ACCOUNTS_JSON", "")) if account_name == username),
            "",
        ))
        revoke_status, revoke_payload, revoke_elapsed = request_json(
            owner["session"],
            "DELETE",
            f"{args.base_url}/api/videos/{video_id}/share-link",
            headers={"X-CSRF-Token": owner["token"]},
            timeout=30,
        ) if owner.get("ok") else (0, {"error": "owner_login_failed"}, 0.0)
        revoked_status, revoked_payload, _ = request_json(
            anonymous,
            "GET",
            f"{args.base_url}/api/videos/shared/{share_token}/playback",
            params={"share_session": share_session},
            timeout=30,
        )
        revoked_master, _ = fetch_text_result(
            anonymous,
            f"{args.base_url}{master_url}" if master_url.startswith("/") else master_url,
            timeout=30,
        ) if master_url else ({"status": 0}, "")

        locked_ok = locked_status in {401, 403}
        wrong_ok = wrong_status in {401, 403}
        unlock_ok = unlock_status == 200 and bool(share_session)
        playback_duration = playback_payload.get("duration_seconds") or ((playback_payload.get("status") or {}).get("duration_seconds"))
        playback_ok = (
            playback_status == 200
            and playback_payload.get("mode") == "hls"
            and bool(playback_payload.get("streaming_ready"))
            and float(playback_duration or 0) >= max(0.0, float(args.minimum_source_duration_seconds))
            and len(playback_payload.get("audio_tracks") or []) >= max(0, int(args.expect_audio_tracks))
        )
        subtitle_ok = not args.expect_subtitles or bool(subtitle_results) and all(item.get("ok") for item in subtitle_results)
        segment_ok = bool(segment_results) and all(item.get("status") == 200 and int(item.get("bytes") or 0) > 0 for item in segment_results)
        browser_ok = not browser_checks or all(item.get("ok") for item in browser_checks)
        revoke_ok = revoke_status == 200 and revoked_status in {404, 410} and int(revoked_master.get("status") or 0) in {404, 410}
        row_ok = all((
            locked_ok,
            wrong_ok,
            unlock_ok,
            playback_ok,
            master_result.get("ok") and "#EXTM3U" in master_text,
            variant_result.get("ok"),
            segment_ok,
            subtitle_ok,
            browser_ok,
            revoke_ok,
        ))
        row.update({
            "ok": bool(row_ok),
            "locked_without_password": {"status": locked_status, "error": (locked_payload or {}).get("error") if isinstance(locked_payload, dict) else ""},
            "wrong_password": {"status": wrong_status, "error": (wrong_payload or {}).get("error") if isinstance(wrong_payload, dict) else ""},
            "unlock": {"status": unlock_status, "elapsed_ms": round(unlock_elapsed * 1000, 2), "share_session_present": bool(share_session)},
            "playback": {
                "status": playback_status,
                "elapsed_ms": round(playback_elapsed * 1000, 2),
                "mode": playback_payload.get("mode"),
                "streaming_ready": playback_payload.get("streaming_ready"),
                "duration_seconds": playback_duration,
                "variants": len(playback_payload.get("variants") or []),
                "audio_tracks": len(playback_payload.get("audio_tracks") or []),
                "subtitles": len(playback_payload.get("subtitles") or []),
            },
            "master": {**master_result, "extm3u": "#EXTM3U" in master_text},
            "variant": variant_result,
            "segments": segment_results,
            "subtitles": subtitle_results,
            "browser": browser_checks,
            "revoke": {
                "status": revoke_status,
                "elapsed_ms": round(revoke_elapsed * 1000, 2),
                "post_revoke_playback_status": revoked_status,
                "post_revoke_master_status": revoked_master.get("status"),
                "error": (revoked_payload or {}).get("error") if isinstance(revoked_payload, dict) else "",
            },
        })
        rows.append(row)
        phase_ok = phase_ok and bool(row_ok)
    if not successful:
        phase_ok = False
    return {"phase": "share", "ok": phase_ok, "shares": rows}


def parse_accounts(raw_accounts: list[str], accounts_json: str = "") -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    if not raw_accounts and str(accounts_json or "").strip():
        try:
            payload = json.loads(accounts_json)
        except json.JSONDecodeError as exc:
            raise ValueError("HACKME_HLS_STRESS_ACCOUNTS_JSON must be valid JSON") from exc
        if not isinstance(payload, list):
            raise ValueError("HACKME_HLS_STRESS_ACCOUNTS_JSON must be a JSON list")
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("each HLS stress account must be an object")
            username = str(item.get("username") or "").strip()
            password = str(item.get("password") or "")
            if not username or not password:
                raise ValueError("each HLS stress account requires username and password")
            accounts.append((username, password))
        return accounts
    for raw in raw_accounts:
        username, sep, password = raw.partition(":")
        username = username.strip()
        if not username:
            continue
        accounts.append((username, password if sep else username))
    if not accounts:
        raise ValueError(
            "upload phase requires HACKME_HLS_STRESS_ACCOUNTS_JSON or explicit --accounts"
        )
    return accounts


def probe_media_file(path: Path, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    executable = shutil.which(ffprobe_bin) or ffprobe_bin
    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration,size:stream=index,codec_type,codec_name,width,height,channels:stream_tags=language,title",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        payload = json.loads(completed.stdout or "{}") if completed.returncode == 0 else {}
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "path": str(path)}
    streams = list(payload.get("streams") or [])
    format_row = payload.get("format") or {}
    return {
        "ok": completed.returncode == 0,
        "path": str(path),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "duration_seconds": round(float(format_row.get("duration") or 0.0), 3),
        "video_streams": len([row for row in streams if row.get("codec_type") == "video"]),
        "audio_streams": len([row for row in streams if row.get("codec_type") == "audio"]),
        "subtitle_streams": len([row for row in streams if row.get("codec_type") == "subtitle"]),
        "streams": streams,
        "stderr": (completed.stderr or "")[-1000:],
    }


def generate_long_fixture(path: Path, *, duration_seconds: int, ffmpeg_bin: str, timeout_seconds: int) -> dict[str, Any]:
    executable = shutil.which(ffmpeg_bin) or ffmpeg_bin
    path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_path = path.with_suffix(".campaign.srt")
    duration = max(10, int(duration_seconds))

    def srt_time(seconds: float) -> str:
        millis = max(0, int(seconds * 1000))
        hours, remainder = divmod(millis, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, ms = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

    cues = [
        (1, 1.0, min(duration - 1.0, 8.0), "campaign start"),
        (2, max(2.0, duration * 0.50), min(duration - 1.0, duration * 0.50 + 8.0), "campaign midpoint"),
        (3, max(2.0, duration - 12.0), max(3.0, duration - 2.0), "campaign end"),
    ]
    subtitle_path.write_text(
        "\n\n".join(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}" for index, start, end, text in cues) + "\n",
        encoding="utf-8",
    )
    subtitle_codec = "mov_text" if path.suffix.lower() in {".mp4", ".m4v", ".mov"} else "srt"
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration}:size=640x360:rate=2",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=16000:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=880:sample_rate=16000:duration={duration}",
        "-i",
        str(subtitle_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:a:0",
        "-map",
        "3:s:0",
        "-metadata:s:a:0",
        "language=jpn",
        "-metadata:s:a:0",
        "title=Japanese",
        "-metadata:s:a:1",
        "language=eng",
        "-metadata:s:a:1",
        "title=English",
        "-metadata:s:s:0",
        "language=zho",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-crf",
        "34",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "12",
        "-c:a",
        "aac",
        "-b:a",
        "32k",
        "-ac",
        "1",
        "-c:s",
        subtitle_codec,
        "-t",
        str(duration),
        str(path),
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=max(60, int(timeout_seconds)))
        result = {
            "ok": completed.returncode == 0 and path.exists() and path.stat().st_size > 0,
            "returncode": completed.returncode,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "requested_duration_seconds": duration,
            "path": str(path),
            "stderr": (completed.stderr or "")[-2000:],
        }
    except Exception as exc:
        result = {
            "ok": False,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "requested_duration_seconds": duration,
            "path": str(path),
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    finally:
        try:
            subtitle_path.unlink()
        except FileNotFoundError:
            pass
    if result["ok"]:
        result["media"] = probe_media_file(path)
    return result


def write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5017")
    parser.add_argument("--video", default="/tmp/hackme_video_quality_sample.mp4")
    parser.add_argument("--db", default="/tmp/hackme_video_quality_direct_5017/runtime/database/database.db")
    parser.add_argument("--runtime-marker", default="/tmp/hackme_video_quality_direct_5017")
    parser.add_argument("--out", default="/tmp/hackme_video_hls_quality_stress_result.json")
    parser.add_argument(
        "--accounts",
        nargs="*",
        default=[],
        help="Legacy username:password entries. Prefer HACKME_HLS_STRESS_ACCOUNTS_JSON so secrets do not enter argv.",
    )
    parser.add_argument("--privacy-mode", default="server_encrypted", choices=["standard_plain", "server_encrypted"])
    parser.add_argument("--visibility", default="public", choices=["public", "unlisted"])
    parser.add_argument("--share-max-views", type=int, default=0)
    parser.add_argument("--verify-share", action="store_true", help="Verify password unlock, shared HLS, and revocation for unlisted uploads.")
    parser.add_argument("--browser-seek", action="store_true", help="Use Chromium to seek through the first shared long video.")
    parser.add_argument("--browser-mobile", action="store_true", help="Also run the shared seek and layout check at a mobile viewport.")
    parser.add_argument("--generate-fixture-duration-seconds", type=int, default=0)
    parser.add_argument("--fixture-timeout-seconds", type=int, default=1200)
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument("--minimum-source-duration-seconds", type=float, default=0.0)
    parser.add_argument("--upload-timeout-seconds", type=int, default=900)
    parser.add_argument("--post-upload-observe-seconds", type=int, default=180)
    parser.add_argument("--monitor-interval", type=float, default=2.0)
    parser.add_argument("--wait-timeout-seconds", type=int, default=10800)
    parser.add_argument("--wait-interval-seconds", type=int, default=30)
    parser.add_argument("--orphan-grace-seconds", type=int, default=600)
    parser.add_argument("--print-wait-status", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--measure-username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(
        parser,
        "--measure-password",
        help_text="Measurement-account password for the existing target server.",
    )
    parser.add_argument("--segment-concurrency", type=int, default=4)
    parser.add_argument("--max-segments-per-variant", type=int, default=12)
    parser.add_argument("--minimum-segments-per-variant", type=int, default=1)
    parser.add_argument("--expect-audio-tracks", type=int, default=0)
    parser.add_argument("--expect-subtitles", action="store_true", help="Fail measure phase when playback has no usable subtitle tracks.")
    parser.add_argument("--upload", action="store_true", help="Run concurrent upload phase.")
    parser.add_argument("--wait", action="store_true", help="Wait for HLS jobs to finish.")
    parser.add_argument("--measure", action="store_true", help="Measure generated HLS quality variants.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verify_share and args.visibility != "unlisted":
        raise SystemExit("--verify-share requires --visibility unlisted")
    if args.browser_mobile and not args.browser_seek:
        raise SystemExit("--browser-mobile requires --browser-seek")
    args.share_password = os.environ.get("HACKME_HLS_SHARE_PASSWORD") or secrets.token_urlsafe(24)
    if not args.upload and not args.wait and not args.measure:
        args.upload = True
        args.wait = True
        args.measure = True
    fixture_generation: dict[str, Any] | None = None
    video_path = Path(args.video).resolve()
    if int(args.generate_fixture_duration_seconds) > 0:
        fixture_generation = generate_long_fixture(
            video_path,
            duration_seconds=int(args.generate_fixture_duration_seconds),
            ffmpeg_bin=args.ffmpeg_bin,
            timeout_seconds=int(args.fixture_timeout_seconds),
        )
        if not fixture_generation.get("ok"):
            result = {
                "ok": False,
                "verdict": "FAIL",
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "base_url": args.base_url,
                "fixture_generation": fixture_generation,
                "phases": [],
            }
            write_result(Path(args.out), result)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 1
    source_media = probe_media_file(video_path, args.ffprobe_bin) if video_path.exists() else {"ok": False, "error": "video_missing"}
    source_checks = {
        "probe": bool(source_media.get("ok")),
        "duration": float(source_media.get("duration_seconds") or 0) >= max(0.0, float(args.minimum_source_duration_seconds)),
        "audio_tracks": int(source_media.get("audio_streams") or 0) >= max(0, int(args.expect_audio_tracks)),
        "subtitles": not args.expect_subtitles or int(source_media.get("subtitle_streams") or 0) >= 1,
    }
    phases: list[dict[str, Any]] = []
    upload_phase: dict[str, Any] | None = None
    if args.upload:
        upload_phase = run_upload_phase(args)
        phases.append(upload_phase)
    if args.wait:
        phases.append(wait_for_hls(args, upload_phase))
    if args.measure:
        phases.append(measure_hls_variants(args, upload_phase))
    if args.verify_share:
        phases.append(verify_share_links(args, upload_phase))
    ok = all(source_checks.values()) and all(bool(phase.get("ok", True)) for phase in phases)
    result = {
        "ok": ok,
        "verdict": "PASS" if ok else "FAIL",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": args.base_url,
        "db": args.db,
        "runtime_marker": args.runtime_marker,
        "fixture_generation": fixture_generation,
        "source_media": source_media,
        "source_checks": source_checks,
        "phases": phases,
    }
    write_result(Path(args.out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
