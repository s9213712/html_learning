#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import os
import random
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.db_stress_probe import ResourceMonitor  # noqa: E402
from scripts.testing.operation_coverage import (  # noqa: E402
    ACCOUNT_SUCCESS_REQUIRED_OPERATIONS,
    GLOBAL_SUCCESS_REQUIRED_OPERATIONS,
)


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DEFENSIVE_LATENCY_OPS = {
    "bad_login",
    "bt_reject",
    "chat_bad_message",
    "community_bad_thread",
    "hf_generate",
    "remote_direct_reject",
    "qos_version",
}
WORKER_TELEMETRY_SCHEMA_VERSION = "hackme.system-stress-worker-telemetry.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return float(sorted_values[idx])


class InflightWorkerTelemetry:
    """Measure workers inside the actual operation critical section."""

    def __init__(self, configured_workers: int, *, sample_interval_seconds: float = 0.02):
        self.configured_workers = max(1, int(configured_workers))
        self.sample_interval_seconds = max(0.005, float(sample_interval_seconds))
        self._lock = threading.Lock()
        self._active = 0
        self._peak = 0
        self._started = 0
        self._completed = 0
        self._histogram: Counter = Counter()
        self._sample_count = 0
        self._first_operation = threading.Event()
        self._sample_observed = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("worker telemetry already started")

        def sample_loop() -> None:
            while not self._stop.is_set():
                if not self._first_operation.wait(self.sample_interval_seconds):
                    continue
                if self._stop.is_set():
                    break
                # Do not sample the executor's thread-start ramp as sustained
                # load.  The first full interval gives submitted workers a
                # chance to enter their actual operation section.
                if self._stop.wait(self.sample_interval_seconds):
                    break
                with self._lock:
                    active = self._active
                    self._histogram[active] += 1
                    self._sample_count += 1
                self._sample_observed.set()

        self._thread = threading.Thread(
            target=sample_loop,
            daemon=True,
            name="system-stress-inflight-worker-sampler",
        )
        self._thread.start()

    def begin_operation(self) -> None:
        with self._lock:
            self._active += 1
            self._started += 1
            self._peak = max(self._peak, self._active)
        self._first_operation.set()

    def end_operation(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("worker telemetry active count underflow")
            self._active -= 1
            self._completed += 1

    def wait_until_sampled(self, timeout: float) -> bool:
        """Wait until the sampler has recorded at least one active-window sample."""

        return self._sample_observed.wait(timeout)

    @staticmethod
    def _histogram_percentile(histogram: Counter, sample_count: int, pct: float) -> int:
        if sample_count <= 0:
            return 0
        target = min(sample_count - 1, max(0, int((sample_count - 1) * pct)))
        observed = 0
        for value in sorted(histogram):
            observed += int(histogram[value])
            if observed > target:
                return int(value)
        return 0

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            histogram = Counter(self._histogram)
            sample_count = int(self._sample_count)
            active = int(self._active)
            started = int(self._started)
            completed = int(self._completed)
            peak = int(self._peak)
        worker_floor = int((self.configured_workers * 0.85) + 0.999999)
        samples_at_floor = sum(
            int(count) for value, count in histogram.items() if int(value) >= worker_floor
        )
        return {
            "schema_version": WORKER_TELEMETRY_SCHEMA_VERSION,
            "method": "native_inflight_operation_counter_time_samples",
            "configured_workers": self.configured_workers,
            "sample_interval_seconds": self.sample_interval_seconds,
            "sample_count": sample_count,
            "active_worker_histogram": {
                str(int(value)): int(count) for value, count in sorted(histogram.items())
            },
            "active_workers_peak": peak,
            "active_workers_p10": self._histogram_percentile(histogram, sample_count, 0.10),
            "active_workers_p50": self._histogram_percentile(histogram, sample_count, 0.50),
            "active_workers_p95": self._histogram_percentile(histogram, sample_count, 0.95),
            "sustained_active_workers": self._histogram_percentile(histogram, sample_count, 0.10),
            "samples_at_or_above_85_percent": samples_at_floor,
            "active_worker_time_ratio_at_or_above_85_percent": round(
                samples_at_floor / sample_count if sample_count else 0.0,
                6,
            ),
            "idle_workers_p95": max(
                0,
                self.configured_workers
                - self._histogram_percentile(histogram, sample_count, 0.10),
            ),
            "operations_started": started,
            "operations_completed": completed,
            "active_workers_at_stop": active,
            "complete": bool(sample_count > 0 and active == 0 and started == completed),
        }


def make_tiny_png() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6360000002000100ffff03000006000557bfabcc0000000049454e44ae426082"
    )


def make_tiny_mp4(path: Path) -> bool:
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=160x90:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-shortest",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=True,
        )
        return path.exists() and path.stat().st_size > 0
    except Exception:
        return False


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self.statuses: dict[str, Counter] = defaultdict(Counter)
        self.classes: dict[str, Counter] = defaultdict(Counter)
        self.errors: list[dict[str, Any]] = []
        self.error_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.samples: list[dict[str, Any]] = []
        self.bytes_received = 0
        self.account_ops: dict[str, Counter] = defaultdict(Counter)
        self.account_successes: dict[str, Counter] = defaultdict(Counter)
        self.account_failures: Counter = Counter()

    def record(self, name: str, *, status: int = 0, elapsed_ms: float = 0.0, ok: bool = False, error: str = "", body_sample: str = "", bytes_received: int = 0, account: str = "", backpressure_rejected: bool = False) -> None:
        body = str(body_sample or error or "")
        sample_class = self._sample_class(
            status=status,
            ok=ok,
            body=body,
            backpressure_rejected=backpressure_rejected,
        )
        error_sample = {
            "op": name,
            "status": status,
            "elapsed_ms": round(float(elapsed_ms), 3),
            "error": str(error or "")[:400],
            "class": sample_class,
        }
        with self._lock:
            self.latencies[name].append(float(elapsed_ms))
            self.statuses[name][str(status)] += 1
            self.classes[name][sample_class] += 1
            self.bytes_received += int(bytes_received or 0)
            if account:
                self.account_ops[str(account)][name] += 1
                if 200 <= int(status or 0) < 300:
                    self.account_successes[str(account)][name] += 1
                if error or not ok:
                    self.account_failures[str(account)] += 1
            if error or not ok:
                self.errors.append(error_sample)
            bucket = self._error_bucket(status=status, ok=ok, body=body, sample_class=sample_class)
            if bucket and len(self.error_buckets[bucket]) < 20:
                self.error_buckets[bucket].append(error_sample)

    @staticmethod
    def _sample_class(*, status: int, ok: bool, body: str, backpressure_rejected: bool = False) -> str:
        status = int(status or 0)
        if status == 503:
            lowered = str(body or "").lower()
            parsed: dict[str, Any] = {}
            try:
                parsed_obj = json.loads(body) if body else {}
                parsed = parsed_obj if isinstance(parsed_obj, dict) else {}
            except Exception:
                parsed = {}
            error_code = str(parsed.get("error") or "").strip().lower()
            code = str(parsed.get("code") or error_code).strip().lower()
            server_busy_body = (
                error_code == "server_busy"
                or '"error":"server_busy"' in lowered.replace(" ", "")
            )
            if backpressure_rejected and server_busy_body:
                return "server_busy_503"
            if (
                parsed.get("feature")
                or parsed.get("feature_label")
                or parsed.get("feature_description")
                or '"feature":' in lowered
                or '"feature_label":' in lowered
                or '"feature_description":' in lowered
                or "feature_" in lowered
                or "此功能目前已由 root 關閉" in body
            ):
                return "feature_disabled_503"
            if code.endswith("_disabled") or code in {"maintenance_mode", "points_chain_disabled", "trading_disabled"}:
                return "application_limited_503"
            if ok:
                return "expected_503"
            return "unexpected_503"
        if status == 0:
            return "transport_error"
        if status >= 500:
            return "http_5xx"
        if ok:
            return "ok"
        return "unexpected_status"

    @staticmethod
    def _error_bucket(*, status: int, ok: bool, body: str, sample_class: str) -> str:
        if int(status or 0) == 503:
            return f"{sample_class}_samples"
        if int(status or 0) >= 500:
            return "http_5xx_samples"
        if int(status or 0) == 0:
            lowered = str(body or "").lower()
            if "timeout" in lowered:
                return "timeout_samples"
            if any(marker in lowered for marker in ("connection", "reset", "refused", "remote disconnected")):
                return "connection_error_samples"
            return "transport_error_samples"
        if not ok:
            return "unexpected_status_samples"
        return ""

    def add_sample(self, sample: dict[str, Any]) -> None:
        with self._lock:
            self.samples.append(dict(sample))

    def summary(self) -> dict[str, Any]:
        op_summary: dict[str, Any] = {}
        total = 0
        failed = 0
        hard_failed = 0
        server_busy = 0
        accepted = 0
        all_latencies: list[float] = []
        ordinary_latencies: list[float] = []
        for name, values in sorted(self.latencies.items()):
            values = sorted(float(v) for v in values)
            count = len(values)
            total += count
            all_latencies.extend(values)
            if name not in DEFENSIVE_LATENCY_OPS:
                ordinary_latencies.extend(values)
            status_counter = self.statuses.get(name, Counter())
            class_counter = self.classes.get(name, Counter())
            op_server_busy = int(class_counter.get("server_busy_503", 0))
            op_feature_disabled = int(class_counter.get("feature_disabled_503", 0))
            op_application_limited = int(class_counter.get("application_limited_503", 0))
            op_expected_503 = int(class_counter.get("expected_503", 0))
            op_unexpected_503 = int(class_counter.get("unexpected_503", 0))
            op_failed = sum(
                count_value
                for status, count_value in status_counter.items()
                if status == "0" or (status.startswith("5") and status != "503")
            )
            op_failed += op_server_busy + op_unexpected_503
            op_hard_failed = sum(
                count_value
                for status, count_value in status_counter.items()
                if status == "0" or (status.startswith("5") and status != "503")
            )
            op_hard_failed += op_unexpected_503
            failed += op_failed
            hard_failed += op_hard_failed
            server_busy += op_server_busy
            accepted += max(0, count - op_server_busy - op_hard_failed)
            op_summary[name] = {
                "count": count,
                "status": dict(sorted(status_counter.items())),
                "successful_2xx": sum(
                    int(count_value)
                    for status, count_value in status_counter.items()
                    if str(status).isdigit() and 200 <= int(status) < 300
                ),
                "p50_ms": round(float(median(values)), 3) if values else 0.0,
                "p95_ms": round(percentile(values, 0.95), 3),
                "p99_ms": round(percentile(values, 0.99), 3),
                "max_ms": round(values[-1], 3) if values else 0.0,
                "transport_or_5xx_failures": op_failed,
                "hard_failures_excluding_503": op_hard_failed,
                "hard_failures_excluding_controlled_503": op_hard_failed,
                "server_busy_503": op_server_busy,
                "feature_disabled_503": op_feature_disabled,
                "application_limited_503": op_application_limited,
                "expected_503": op_expected_503,
                "unexpected_503": op_unexpected_503,
            }
        all_latencies = sorted(all_latencies)
        ordinary_latencies = sorted(ordinary_latencies)
        account_summary = {
            account: {
                "total_ops": int(sum(ops.values())),
                "failed_ops": int(self.account_failures.get(account, 0)),
                "operations": dict(sorted(ops.items())),
                "successful_operations": dict(sorted(self.account_successes.get(account, Counter()).items())),
            }
            for account, ops in sorted(self.account_ops.items())
        }
        return {
            "total_ops": total,
            "accepted_ops_excluding_server_busy_and_hard_failure": accepted,
            "transport_or_5xx_failures": failed,
            "transport_or_5xx_failure_rate": round((failed / total) if total else 0.0, 6),
            "hard_failures_excluding_503": hard_failed,
            "hard_failures_excluding_controlled_503": hard_failed,
            "hard_failure_rate_excluding_503": round((hard_failed / total) if total else 0.0, 6),
            "hard_failure_rate_excluding_controlled_503": round((hard_failed / total) if total else 0.0, 6),
            "server_busy_503": server_busy,
            "server_busy_503_rate": round((server_busy / total) if total else 0.0, 6),
            "bytes_received": self.bytes_received,
            "overall_latency": {
                "p50_ms": round(float(median(all_latencies)), 3) if all_latencies else 0.0,
                "p95_ms": round(percentile(all_latencies, 0.95), 3),
                "p99_ms": round(percentile(all_latencies, 0.99), 3),
                "max_ms": round(all_latencies[-1], 3) if all_latencies else 0.0,
            },
            "ordinary_latency": {
                "count": len(ordinary_latencies),
                "p50_ms": round(float(median(ordinary_latencies)), 3) if ordinary_latencies else 0.0,
                "p95_ms": round(percentile(ordinary_latencies, 0.95), 3),
                "p99_ms": round(percentile(ordinary_latencies, 0.99), 3),
                "max_ms": round(ordinary_latencies[-1], 3) if ordinary_latencies else 0.0,
                "excluded_ops": sorted(DEFENSIVE_LATENCY_OPS),
            },
            "ops": op_summary,
            "sample_errors": self.errors[:100],
            "sample_error_buckets": {key: list(value) for key, value in sorted(self.error_buckets.items())},
            "accounts": account_summary,
        }


class Client:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = float(timeout)
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""
        # The task runner acquires this slot before marking an operation
        # in-flight; request() re-enters it for the actual HTTP exchange.
        self.lock = threading.RLock()

    def refresh_csrf(self) -> bool:
        res = self.session.get(f"{self.base_url}/api/csrf-token", timeout=self.timeout)
        if res.status_code >= 400:
            return False
        try:
            self.csrf = str(res.json().get("csrf_token") or "")
        except Exception:
            self.csrf = ""
        return bool(self.csrf)

    def login(self, *, name: str = "login", expected: set[int] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        expected = expected or {200}
        try:
            self.refresh_csrf()
            res = self.session.post(
                f"{self.base_url}/api/login",
                json={"username": self.username, "password": self.password},
                headers={"X-CSRF-Token": self.csrf},
                timeout=self.timeout,
            )
            self.refresh_csrf()
            return self.capture(name, res, started=started, expected=expected)
        except Exception as exc:
            return {"op": name, "ok": False, "status": 0, "error": f"{exc.__class__.__name__}: {exc}", "elapsed_ms": (time.perf_counter() - started) * 1000}

    def clone_auth_from(self, other: "Client") -> None:
        self.session.cookies.update(other.session.cookies)
        self.csrf = other.csrf

    def capture(self, name: str, res: requests.Response, *, started: float, expected: set[int] | None = None) -> dict[str, Any]:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        expected = expected or {200}
        body_sample = ""
        try:
            body_sample = res.text[:240]
        except Exception:
            body_sample = ""
        return {
            "op": name,
            "ok": res.status_code in expected,
            "status": int(res.status_code),
            "elapsed_ms": elapsed_ms,
            "bytes": len(res.content or b""),
            "error": "" if res.status_code in expected else body_sample,
            "body_sample": body_sample,
            "backpressure_rejected": res.headers.get("X-Hackme-Backpressure-Rejected") == "1",
        }

    def request(
        self,
        name: str,
        method: str,
        path: str,
        *,
        expected: set[int] | None = None,
        retry_csrf: bool = True,
        **kwargs,
    ) -> dict[str, Any]:
        method = method.upper()
        expected = expected or {200}
        with self.lock:
            started = time.perf_counter()
            try:
                headers = dict(kwargs.pop("headers", {}) or {})
                if method in UNSAFE_METHODS:
                    if not self.csrf:
                        self.refresh_csrf()
                    headers.setdefault("X-CSRF-Token", self.csrf)
                res = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    timeout=self.timeout,
                    **kwargs,
                )
                rotated_csrf = self.session.cookies.get("csrf_token")
                if rotated_csrf:
                    self.csrf = str(rotated_csrf)
                if retry_csrf and method in UNSAFE_METHODS and res.status_code in {400, 403}:
                    text = res.text[:300].lower()
                    if "csrf" in text:
                        self.refresh_csrf()
                        headers["X-CSRF-Token"] = self.csrf
                        started = time.perf_counter()
                        res = self.session.request(
                            method,
                            f"{self.base_url}{path}",
                            headers=headers,
                            timeout=self.timeout,
                            **kwargs,
                        )
                        rotated_csrf = self.session.cookies.get("csrf_token")
                        if rotated_csrf:
                            self.csrf = str(rotated_csrf)
                return self.capture(name, res, started=started, expected=expected)
            except Exception as exc:
                return {
                    "op": name,
                    "ok": False,
                    "status": 0,
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                    "bytes": 0,
                    "error": f"{exc.__class__.__name__}: {str(exc)[:300]}",
                }


class OperationBudget:
    def __init__(self, limits: dict[str, int]):
        self._limits = {str(k): int(v) for k, v in limits.items()}
        self._counts: Counter = Counter()
        self._lock = threading.Lock()

    def claim(self, key: str) -> bool:
        key = str(key)
        limit = self._limits.get(key)
        if limit is None or limit < 0:
            return True
        with self._lock:
            if self._counts[key] >= limit:
                return False
            self._counts[key] += 1
            return True

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


def record_operation_result(
    stats: Stats,
    *,
    requested_operation: str,
    result: dict[str, Any],
    account: str,
) -> str:
    """Record the operation that actually ran, preserving fallback identity."""

    actual_operation = str(result.get("op") or "").strip() or str(requested_operation)
    stats.record(
        actual_operation,
        status=int(result.get("status") or 0),
        elapsed_ms=float(result.get("elapsed_ms") or 0.0),
        ok=bool(result.get("ok")),
        error=str(result.get("error") or ""),
        body_sample=str(result.get("body_sample") or ""),
        bytes_received=int(result.get("bytes") or 0),
        account=account,
        backpressure_rejected=bool(result.get("backpressure_rejected")),
    )
    return actual_operation


def db_paths_from_runtime(runtime_root: str) -> dict[str, Path]:
    if not runtime_root:
        return {}
    root = Path(runtime_root)
    candidates = [
        root / "runtime" / "database",
        root / "hackme_web" / "runtime" / "database",
        root / "database",
    ]
    for base in candidates:
        if (base / "database.db").exists() or base.exists():
            return {
                "main": base / "database.db",
                "auth": base / "auth.db",
                "audit": base / "audit.db",
                "control": base / "control.db",
            }
    return {}


def parse_pids(value: str) -> list[int]:
    pids = []
    for item in str(value or "").replace(",", " ").split():
        try:
            pids.append(int(item))
        except Exception:
            pass
    return pids


def resolve_server_pids(value: str, runtime_root: str) -> tuple[list[int], str]:
    explicit = parse_pids(value)
    if explicit:
        return explicit, "explicit"
    root = Path(str(runtime_root or "")).resolve(strict=False) if runtime_root else None
    if root is None:
        return [], "none"
    candidates = (
        root / "server.pid",
        root / "runtime" / "server.pid",
        root / "hackme_web" / "runtime" / "server.pid",
    )
    for candidate in candidates:
        try:
            discovered = parse_pids(candidate.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError):
            continue
        if discovered:
            return discovered, f"pidfile:{candidate}"
    return [], "none"


def setup_seed(client: Client, artifact_dir: Path) -> dict[str, Any]:
    seed: dict[str, Any] = {"started_at": utc_now(), "errors": []}
    artifact_dir.mkdir(parents=True, exist_ok=True)
    text_payload = b"stress fixture\n" * 16
    png_payload = make_tiny_png()

    def remember(name: str, result: dict[str, Any]) -> None:
        seed[name] = {k: result.get(k) for k in ("ok", "status", "elapsed_ms", "error")}

    result = client.request(
        "seed_drive_upload",
        "POST",
        "/api/cloud-drive/upload",
        files={"file": ("seed.txt", io.BytesIO(text_payload), "text/plain")},
        data={"privacy_mode": "standard_plain", "display_name": "seed.txt", "virtual_path": "/Stress/seed.txt"},
        expected={200},
    )
    remember("drive_upload", result)
    if result.get("ok"):
        try:
            body = client.session.get(f"{client.base_url}/api/cloud-drive/files", timeout=client.timeout).json()
            files = body.get("files") or body.get("items") or []
            if isinstance(files, list) and files:
                first = files[0]
                seed["file_id"] = first.get("file_id") or first.get("id")
        except Exception as exc:
            seed["errors"].append(f"drive file lookup failed: {exc}")

    result = client.request(
        "seed_png_upload",
        "POST",
        "/api/cloud-drive/upload",
        files={"file": ("seed.png", io.BytesIO(png_payload), "image/png")},
        data={"privacy_mode": "standard_plain", "display_name": "seed.png", "virtual_path": "/Stress/seed.png"},
        expected={200},
    )
    remember("png_upload", result)

    mp4_path = artifact_dir / "seed.mp4"
    if make_tiny_mp4(mp4_path):
        with mp4_path.open("rb") as fh:
            result = client.request(
                "seed_video_upload",
                "POST",
                "/api/videos/upload",
                files={"video": ("seed.mp4", fh, "video/mp4")},
                data={"title": "stress seed video", "visibility": "unlisted", "share_password": "StressVideo123!"},
                expected={200, 400, 409, 500},
            )
        remember("video_upload", result)
        if result.get("status") == 200:
            try:
                body = client.session.get(f"{client.base_url}/api/videos/manage", timeout=client.timeout).json()
                videos = body.get("videos") or []
                if videos:
                    seed["video_id"] = videos[0].get("id")
            except Exception as exc:
                seed["errors"].append(f"video lookup failed: {exc}")
    else:
        seed["errors"].append("ffmpeg unavailable; video seed skipped")

    result = client.request(
        "seed_chat_room",
        "POST",
        "/api/chat/rooms",
        json={"name": f"stress-room-{int(time.time())}", "room_type": "group", "allow_anonymous": True},
        expected={200, 201, 400, 403, 409},
    )
    remember("chat_room", result)
    try:
        rooms = client.session.get(f"{client.base_url}/api/chat/rooms", timeout=client.timeout).json().get("rooms") or []
        if rooms:
            seed["room_id"] = rooms[0].get("id")
    except Exception as exc:
        seed["errors"].append(f"chat room lookup failed: {exc}")

    return seed


def choose_operation(rng: random.Random, weighted_ops: list[tuple[str, int]]) -> str:
    total = sum(weight for _name, weight in weighted_ops)
    needle = rng.randint(1, max(1, total))
    seen = 0
    for name, weight in weighted_ops:
        seen += weight
        if needle <= seen:
            return name
    return weighted_ops[-1][0]


def run_operation(name: str, client: Client, seed: dict[str, Any], budget: OperationBudget, logical_user_id: int) -> dict[str, Any]:
    unique = f"{logical_user_id}-{time.time_ns()}"
    if name == "version":
        return client.request(name, "GET", "/api/version", expected={200})
    if name == "me":
        return client.request(name, "GET", "/api/me", expected={200})
    if name == "profile":
        return client.request(name, "GET", "/api/users/me/profile", expected={200, 403})
    if name == "friends":
        return client.request(name, "GET", "/api/friends", expected={200, 403})
    if name == "notifications":
        return client.request(name, "GET", "/api/notifications/unread-count", expected={200, 401, 403})
    if name == "jobs":
        return client.request(name, "GET", "/api/jobs", expected={200})
    if name == "drive_list":
        return client.request(name, "GET", "/api/cloud-drive/files", expected={200})
    if name == "drive_upload":
        if not budget.claim("drive_upload"):
            return client.request("drive_upload_fallback_list", "GET", "/api/cloud-drive/files", expected={200})
        return client.request(
            name,
            "POST",
            "/api/cloud-drive/upload",
            files={"file": (f"stress-{unique}.txt", io.BytesIO(b"x" * 2048), "text/plain")},
            data={"privacy_mode": "standard_plain", "display_name": f"stress-{unique}.txt", "virtual_path": f"/Stress/{unique}.txt"},
            expected={200, 400, 409, 413, 429},
        )
    if name == "drive_download":
        file_id = seed.get("file_id")
        if not file_id:
            return client.request("drive_download_no_seed", "GET", "/api/cloud-drive/files", expected={200})
        return client.request(name, "GET", f"/api/cloud-drive/files/{file_id}/download", expected={200, 403, 404})
    if name == "resumable_start":
        if not budget.claim("resumable_start"):
            return client.request("resumable_list", "GET", "/api/cloud-drive/resumable-upload/sessions", expected={200})
        return client.request(
            name,
            "POST",
            "/api/cloud-drive/resumable-upload/start",
            json={"filename": f"chunk-{unique}.bin", "total_bytes": 4096, "chunk_size": 4096, "privacy_mode": "standard_plain"},
            expected={200, 400, 409, 413, 429},
        )
    if name == "video_list":
        return client.request(name, "GET", "/api/videos", expected={200})
    if name == "video_playback":
        video_id = seed.get("video_id")
        if not video_id:
            return client.request("video_playback_no_seed", "GET", "/api/videos", expected={200})
        return client.request(name, "GET", f"/api/videos/{video_id}/playback", expected={200, 403, 404, 409})
    if name == "hls_master":
        video_id = seed.get("video_id")
        if not video_id:
            return client.request("hls_no_seed", "GET", "/api/videos", expected={200})
        return client.request(name, "GET", f"/api/videos/{video_id}/hls/master.m3u8", expected={200, 403, 404, 409})
    if name == "share_manage":
        return client.request(name, "GET", "/api/shares", expected={200, 403})
    if name == "albums":
        return client.request(name, "GET", "/api/storage/albums", expected={200, 403})
    if name == "appeals":
        return client.request(name, "GET", "/api/appeals", expected={200, 403})
    if name == "hf_status":
        return client.request(name, "GET", "/api/comfyui/status", expected={200, 401, 403, 503})
    if name == "hf_quote":
        return client.request(
            name,
            "POST",
            "/api/comfyui/billing-quote",
            json={"prompt": "stress test", "backend": "diffusers", "huggingface_model_repo": "hf-internal-testing/tiny-stable-diffusion-pipe", "skip_asset_validation": True},
            expected={200, 400, 409, 503},
        )
    if name == "hf_generate":
        if not budget.claim("hf_generate"):
            return client.request("hf_generate_fallback_status", "GET", "/api/comfyui/status", expected={200, 401, 403, 503})
        return client.request(
            name,
            "POST",
            "/api/comfyui/generate",
            json={
                "prompt": "stress test",
                "backend": "diffusers",
                "huggingface_model_repo": "hf-internal-testing/tiny-stable-diffusion-pipe",
                "width": 64,
                "height": 64,
                "steps": 1,
                "batch_size": 1,
                "confirm_billing": True,
                "timeout_seconds": 1,
            },
            expected={200, 400, 409, 429, 503},
        )
    if name == "remote_direct_reject":
        if not budget.claim("remote_direct_reject"):
            return client.request("remote_capabilities", "GET", "/api/cloud-drive/remote-download/capabilities", expected={200, 403, 404})
        return client.request(
            name,
            "POST",
            "/api/cloud-drive/remote-download/tasks",
            json={"url": "http://127.0.0.1:1/blocked", "download_mode": "direct"},
            expected={400, 403, 404, 409, 429},
        )
    if name == "bt_reject":
        if not budget.claim("bt_reject"):
            return client.request("remote_capabilities", "GET", "/api/cloud-drive/remote-download/capabilities", expected={200, 403, 404})
        return client.request(
            name,
            "POST",
            "/api/cloud-drive/remote-download/tasks",
            json={"url": "http://127.0.0.1/blocked.torrent", "download_mode": "bt"},
            expected={400, 403, 404, 409, 429},
        )
    if name == "trading_markets":
        return client.request(name, "GET", "/api/trading/markets", expected={200, 403, 503})
    if name == "trading_dashboard":
        return client.request(name, "GET", "/api/trading/dashboard", expected={200, 403, 503})
    if name == "trading_asset_overview":
        return client.request(name, "GET", "/api/trading/asset-overview", expected={200, 403, 503})
    if name == "trading_bots":
        return client.request(name, "GET", "/api/trading/bots", expected={200, 403, 503})
    if name == "trading_grid_bots":
        return client.request(name, "GET", "/api/trading/grid-bots", expected={200, 403, 503})
    if name == "trading_workflows":
        return client.request(name, "GET", "/api/trading/workflow-templates", expected={200, 403, 503})
    if name == "trading_grid_preview":
        return client.request(
            name,
            "POST",
            "/api/trading/grid/preview",
            json={"market_symbol": "BTC/USDT", "lower_price_points": 70000, "upper_price_points": 80000, "grid_count": 3, "order_amount_points": 100},
            expected={200, 400, 403, 409, 503},
        )
    if name == "games_catalog":
        return client.request(name, "GET", "/api/games/catalog", expected={200, 403})
    if name == "chess_leaderboard":
        return client.request(name, "GET", "/api/games/chess/leaderboard", expected={200, 403})
    if name == "community_boards":
        return client.request(name, "GET", "/api/community/boards", expected={200, 403})
    if name == "community_announcements":
        return client.request(name, "GET", "/api/community/announcements", expected={200, 403})
    if name == "community_bad_thread":
        if not budget.claim("community_bad_thread"):
            return client.request("community_boards", "GET", "/api/community/boards", expected={200, 403})
        return client.request(
            name,
            "POST",
            "/api/community/boards/999999/threads",
            json={"title": "", "content": ""},
            expected={400, 403, 404, 429},
        )
    if name == "chat_rooms":
        return client.request(name, "GET", "/api/chat/rooms", expected={200, 403})
    if name == "points_wallet":
        return client.request(name, "GET", "/api/points/wallet", expected={200, 403, 503})
    if name == "points_ledger":
        return client.request(name, "GET", "/api/points/ledger?limit=20", expected={200, 403, 503})
    if name == "points_catalog":
        return client.request(name, "GET", "/api/points/catalog", expected={200, 403, 503})
    if name == "points_governance":
        return client.request(name, "GET", "/api/points/governance/proposals?limit=20", expected={200, 403, 503})
    if name == "ai_agent_status":
        return client.request(name, "GET", "/api/ai-agent/status", expected={200, 403, 503})
    if name == "ai_agent_tools":
        return client.request(name, "GET", "/api/ai-agent/write-tools", expected={200, 403, 503})
    if name == "chat_bad_message":
        if not budget.claim("chat_bad_message"):
            return client.request("chat_rooms", "GET", "/api/chat/rooms", expected={200, 403})
        return client.request(name, "POST", "/api/chat/rooms/999999/messages", json={"content": "stress"}, expected={400, 403, 404, 429})
    if name == "bad_login":
        if not budget.claim("bad_login"):
            return client.request("version", "GET", "/api/version", expected={200})
        temp = Client(client.base_url, f"bad-{unique}", "wrong", timeout=client.timeout)
        return temp.login(name="bad_login", expected={401, 403, 429})
    return client.request("version", "GET", "/api/version", expected={200})


def qos_monitor(base_url: str, stats: Stats, stop: threading.Event, interval: float) -> None:
    session = requests.Session()
    session.verify = False
    while not stop.wait(max(0.2, float(interval))):
        started = time.perf_counter()
        try:
            res = session.get(f"{base_url.rstrip()}/api/version", timeout=5)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            stats.record("qos_version", status=res.status_code, elapsed_ms=elapsed_ms, ok=res.status_code == 200, bytes_received=len(res.content or b""))
            stats.add_sample({"ts": time.time(), "qos_status": res.status_code, "qos_elapsed_ms": round(elapsed_ms, 3)})
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            stats.record("qos_version", status=0, elapsed_ms=elapsed_ms, ok=False, error=f"{exc.__class__.__name__}: {exc}")
            stats.add_sample({"ts": time.time(), "qos_status": 0, "qos_elapsed_ms": round(elapsed_ms, 3), "error": str(exc)[:200]})


def build_weighted_ops() -> list[tuple[str, int]]:
    return [
        ("version", 10),
        ("me", 10),
        ("profile", 3),
        ("friends", 3),
        ("notifications", 5),
        ("jobs", 5),
        ("drive_list", 8),
        ("drive_upload", 2),
        ("drive_download", 5),
        ("resumable_start", 1),
        ("video_list", 5),
        ("video_playback", 2),
        ("hls_master", 2),
        ("share_manage", 2),
        ("albums", 3),
        ("appeals", 2),
        ("hf_status", 3),
        ("hf_quote", 2),
        ("hf_generate", 1),
        ("remote_direct_reject", 1),
        ("bt_reject", 1),
        ("trading_markets", 5),
        ("trading_dashboard", 5),
        ("trading_asset_overview", 3),
        ("trading_bots", 2),
        ("trading_grid_bots", 2),
        ("trading_workflows", 2),
        ("trading_grid_preview", 2),
        ("games_catalog", 4),
        ("chess_leaderboard", 3),
        ("community_boards", 5),
        ("community_announcements", 3),
        ("community_bad_thread", 1),
        ("chat_rooms", 5),
        ("points_wallet", 4),
        ("points_ledger", 3),
        ("points_catalog", 2),
        ("points_governance", 2),
        ("ai_agent_status", 3),
        ("ai_agent_tools", 2),
        ("chat_bad_message", 1),
        ("bad_login", 1),
    ]


def resolve_session_pool_size(*, requested: int, session_mode: str, account_count: int, concurrency: int, logical_users: int) -> tuple[int, str]:
    requested = int(requested or 0)
    if requested > 0:
        return requested, "explicit"
    concurrency = max(1, int(concurrency or 1))
    logical_users = max(1, int(logical_users or 1))
    account_count = max(1, int(account_count or 1))
    if str(session_mode or "clone") == "login":
        return max(1, min(account_count, concurrency, logical_users)), "auto_login_account_capped"
    return max(1, min(256, max(concurrency, min(logical_users, 256)))), "auto_clone"


def rotation_operation_account(task_id: int, operation_names: list[str], account_names: list[str]) -> tuple[str, str]:
    if not operation_names or not account_names:
        raise ValueError("rotation requires operations and accounts")
    task_id = max(0, int(task_id))
    # Interleave accounts so the executor's first worker wave does not queue a
    # whole operation matrix behind one account's session lock.
    operation = operation_names[(task_id // len(account_names)) % len(operation_names)]
    account = account_names[task_id % len(account_names)]
    return operation, account


def rotation_client_index(task_id: int, account_count: int, client_count: int) -> int:
    """Spread one account's consecutive rotation operations over its clones."""

    task_id = max(0, int(task_id))
    account_count = max(1, int(account_count))
    client_count = max(1, int(client_count))
    operation_index = task_id // account_count
    return operation_index % client_count


def run_in_client_slot(
    client: Client,
    telemetry: InflightWorkerTelemetry,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Count only work holding a real client slot, excluding lock waiters."""

    with client.lock:
        telemetry.begin_operation()
        try:
            return operation()
        finally:
            telemetry.end_operation()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--server-pids", default=os.environ.get("HACKME_SERVER_PIDS", ""))
    parser.add_argument("--logical-users", type=int, default=10000)
    parser.add_argument("--ops", type=int, default=10000)
    parser.add_argument("--concurrency", type=int, default=512)
    parser.add_argument("--session-pool", type=int, default=0, help="Authenticated client pool size. 0=auto; login mode caps to account count to avoid polluting live QA with login-rate-limit noise.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--qos-interval", type=float, default=1.0)
    parser.add_argument("--resource-interval", type=float, default=1.0)
    parser.add_argument("--root-password", default=os.environ.get("HACKME_STRESS_ROOT_PASSWORD", ""), help="Deprecated compatibility option; this probe authenticates configured member accounts only.")
    parser.add_argument("--test-password", default=os.environ.get("HACKME_STRESS_TEST_PASSWORD", ""))
    parser.add_argument("--accounts", default=os.environ.get("HACKME_STRESS_ACCOUNTS", ""))
    parser.add_argument("--session-mode", choices=["clone", "login"], default="clone")
    parser.add_argument("--operation-mode", choices=["random", "rotation"], default="random", help="rotation deterministically covers every operation for every active account before repeating")
    parser.add_argument("--rotation-offset", type=int, default=0, help="Global task offset for rotation mode so repeated bounded rounds continue account/operation coverage")
    parser.add_argument("--require-all-accounts", action="store_true", help="Fail when any configured account cannot authenticate or receives no operation")
    parser.add_argument("--require-operation-coverage", action="store_true", help="Fail when any registered operation was not exercised")
    parser.add_argument("--require-operation-success", action="store_true", help="Fail when any required positive-path operation has no HTTP 2xx result")
    parser.add_argument("--require-account-success", action="store_true", help="Fail when any configured account lacks a 2xx result for a required account-safe operation")
    parser.add_argument("--allow-server-busy", action="store_true", help="Treat HTTP 503 server_busy as controlled degradation instead of a hard failure")
    parser.add_argument("--max-server-busy-rate", type=float, default=1.0, help="Maximum accepted 503 server_busy ratio when --allow-server-busy is enabled")
    parser.add_argument("--max-ordinary-p95-ms", type=float, default=1500.0)
    parser.add_argument("--max-ordinary-p99-ms", type=float, default=5000.0)
    parser.add_argument("--max-drive-uploads", type=int, default=200)
    parser.add_argument("--max-resumable-starts", type=int, default=150)
    parser.add_argument("--max-hf-generates", type=int, default=20)
    parser.add_argument("--max-remote-rejects", type=int, default=250)
    parser.add_argument("--max-bt-rejects", type=int, default=250)
    parser.add_argument("--max-bad-logins", type=int, default=100)
    parser.add_argument("--max-bad-community", type=int, default=150)
    parser.add_argument("--max-bad-chat", type=int, default=150)
    args = parser.parse_args()

    requests.packages.urllib3.disable_warnings()
    try:
        ssl._create_default_https_context = ssl._create_unverified_context
    except Exception:
        pass

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir = out_path.parent / "system_stress_artifacts"

    accounts: list[tuple[str, str]] = []
    for spec in str(args.accounts or "").split(","):
        if not spec.strip() or ":" not in spec:
            continue
        username, password = spec.split(":", 1)
        accounts.append((username.strip(), password.strip()))
    if not accounts:
        if not args.test_password:
            parser.error(
                "HACKME_STRESS_ACCOUNTS/--accounts or "
                "HACKME_STRESS_TEST_PASSWORD/--test-password is required"
            )
        accounts = [("test", args.test_password)]
    requested_session_pool = int(args.session_pool or 0)
    session_pool, session_pool_mode = resolve_session_pool_size(
        requested=requested_session_pool,
        session_mode=args.session_mode,
        account_count=len(accounts),
        concurrency=args.concurrency,
        logical_users=args.logical_users,
    )

    account_seeds: dict[str, Client] = {}
    account_login_results: dict[str, dict[str, Any]] = {}

    def login_account(account: tuple[str, str]) -> tuple[str, Client, dict[str, Any]]:
        username, password = account
        client = Client(args.base_url, username, password, timeout=args.timeout)
        return username, client, client.login()

    with ThreadPoolExecutor(max_workers=min(max(1, len(accounts)), 16)) as pool:
        for username, client, result in pool.map(login_account, accounts):
            account_login_results[username] = {
                key: result.get(key)
                for key in ("ok", "status", "elapsed_ms", "error")
            }
            if result.get("ok"):
                account_seeds[username] = client

    seed_client = account_seeds.get(accounts[0][0]) or next(iter(account_seeds.values()), Client(args.base_url, accounts[0][0], accounts[0][1], timeout=args.timeout))
    seed_login = account_login_results.get(seed_client.username) or {"ok": False, "status": 0, "error": "no account authenticated"}
    seed = setup_seed(seed_client, artifact_dir) if seed_login.get("ok") else {"errors": ["seed login failed"], "login": seed_login}

    clients: list[Client] = []
    login_stats = Stats()
    login_started = time.perf_counter()

    def make_client(idx: int) -> Client:
        username, password = accounts[idx % len(accounts)]
        client = Client(args.base_url, username, password, timeout=args.timeout)
        account_seed = account_seeds.get(username)
        if args.session_mode == "clone" and account_seed is not None:
            client.clone_auth_from(account_seed)
            result = {"ok": True, "status": 200, "elapsed_ms": 0.0, "error": ""}
        else:
            result = client.login()
        login_stats.record("login", status=result.get("status", 0), elapsed_ms=result.get("elapsed_ms", 0.0), ok=bool(result.get("ok")), error=result.get("error", ""), account=username, backpressure_rejected=bool(result.get("backpressure_rejected")))
        if not result.get("ok"):
            client.csrf = ""
        return client

    with ThreadPoolExecutor(max_workers=min(max(1, session_pool), 128)) as pool:
        for client in pool.map(make_client, range(max(1, int(session_pool)))):
            if client.csrf:
                clients.append(client)
    login_elapsed_seconds = time.perf_counter() - login_started

    if not clients:
        payload = {
            "ok": False,
            "error": "no authenticated stress clients could be created",
            "seed": seed,
            "login_summary": login_stats.summary(),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    stats = Stats()
    # Rotation coverage is about dispatching every mandatory operation, not
    # silently losing evidence when an operation fails before its result is
    # recorded.  Keep an independent, thread-safe-by-CPython set of planned
    # dispatches and report it alongside result statistics.
    attempted_operations: set[str] = set()
    stop_qos = threading.Event()
    qos_thread = threading.Thread(target=qos_monitor, args=(args.base_url, stats, stop_qos, args.qos_interval), daemon=True)
    qos_thread.start()

    db_paths = db_paths_from_runtime(args.runtime_root)
    monitor = None
    resource_summary = {}
    if db_paths:
        monitor_pids, monitor_pid_source = resolve_server_pids(args.server_pids, args.runtime_root)
        monitor = ResourceMonitor(
            runtime_root=Path(args.runtime_root),
            paths=db_paths,
            interval=float(args.resource_interval),
            pids=monitor_pids,
        )
        monitor.start()

    budget = OperationBudget(
        {
            "drive_upload": args.max_drive_uploads,
            "resumable_start": args.max_resumable_starts,
            "hf_generate": args.max_hf_generates,
            "remote_direct_reject": args.max_remote_rejects,
            "bt_reject": args.max_bt_rejects,
            "bad_login": args.max_bad_logins,
            "community_bad_thread": args.max_bad_community,
            "chat_bad_message": args.max_bad_chat,
        }
    )
    weighted_ops = build_weighted_ops()
    operation_names = [name for name, _weight in weighted_ops]
    total_ops = max(1, int(args.ops or args.logical_users))
    concurrency = max(1, int(args.concurrency))
    start_event = threading.Event()
    worker_telemetry = InflightWorkerTelemetry(concurrency)

    def task(task_id: int) -> None:
        rng = random.Random((task_id + 1) * 7919)
        if args.operation_mode == "rotation":
            rotation_task_id = max(0, int(args.rotation_offset or 0)) + task_id
            op, desired_account = rotation_operation_account(
                rotation_task_id,
                operation_names,
                [username for username, _password in accounts],
            )
            account_clients = [item for item in clients if item.username == desired_account]
            client = account_clients[
                rotation_client_index(
                    rotation_task_id,
                    len(accounts),
                    len(account_clients),
                )
            ] if account_clients else clients[task_id % len(clients)]
        else:
            client = clients[task_id % len(clients)]
            op = choose_operation(rng, weighted_ops)
        attempted_operations.add(op)
        start_event.wait()
        result = run_in_client_slot(
            client,
            worker_telemetry,
            lambda: run_operation(op, client, seed, budget, task_id),
        )
        record_operation_result(
            stats,
            requested_operation=op,
            result=result,
            account=client.username,
        )

    started_at = utc_now()
    started = time.perf_counter()
    worker_telemetry.start()
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(task, idx) for idx in range(total_ops)]
            start_event.set()
            for _future in as_completed(futures):
                pass
    finally:
        worker_telemetry_summary = worker_telemetry.stop()
        stop_qos.set()
        qos_thread.join(timeout=3)
        if monitor:
            resource_summary = monitor.stop()
            resource_summary["configured_root_pids"] = monitor_pids
            resource_summary["server_pid_source"] = monitor_pid_source

    elapsed_seconds = time.perf_counter() - started
    summary = stats.summary()
    qos = summary.get("ops", {}).get("qos_version", {})
    ordinary_latency = summary.get("ordinary_latency") or summary.get("overall_latency") or {}
    degraded_reasons = []
    failure_rate_key = "hard_failure_rate_excluding_controlled_503" if args.allow_server_busy else "transport_or_5xx_failure_rate"
    if summary.get(failure_rate_key, 0) > 0.01:
        degraded_reasons.append(
            "hard_failure_excluding_503_rate_gt_1_percent"
            if args.allow_server_busy
            else "transport_or_5xx_failure_rate_gt_1_percent"
        )
    max_ordinary_p95_ms = max(1.0, float(args.max_ordinary_p95_ms))
    max_ordinary_p99_ms = max(max_ordinary_p95_ms, float(args.max_ordinary_p99_ms))
    if ordinary_latency.get("p95_ms", 0) > max_ordinary_p95_ms:
        degraded_reasons.append("ordinary_p95_above_configured_limit")
    if ordinary_latency.get("p99_ms", 0) > max_ordinary_p99_ms:
        degraded_reasons.append("ordinary_p99_above_configured_limit")
    if qos and int(qos.get("count") or 0) >= 10 and (qos.get("p95_ms") or 0) > 1000:
        degraded_reasons.append("qos_version_p95_gt_1000ms")
    if resource_summary.get("mem_available_min_mb") is not None and float(resource_summary.get("mem_available_min_mb") or 0) < 512:
        degraded_reasons.append("available_memory_below_512mb")
    hard_failure_count = int(summary.get("hard_failures_excluding_controlled_503", summary.get("hard_failures_excluding_503", 0)) or 0)
    transport_failure_count = int(summary.get("transport_or_5xx_failures", 0) or 0)
    summary_total_ops = int(summary.get("total_ops", total_ops) or total_ops)
    accepted_ops = int(summary.get("accepted_ops_excluding_server_busy_and_hard_failure", 0) or 0)
    server_busy_ops = int(summary.get("server_busy_503", 0) or 0)
    server_busy_rate = float(summary.get("server_busy_503_rate") or 0.0)
    max_server_busy_rate = max(0.0, min(1.0, float(args.max_server_busy_rate)))
    if args.allow_server_busy and server_busy_rate > max_server_busy_rate:
        degraded_reasons.append("server_busy_rate_above_configured_limit")
    configured_account_names = [username for username, _password in accounts]
    active_account_names = sorted({client.username for client in clients})
    account_operation_counts = {
        username: int((summary.get("accounts") or {}).get(username, {}).get("total_ops") or 0)
        for username in configured_account_names
    }
    missing_accounts = [username for username in configured_account_names if username not in active_account_names]
    accounts_without_operations = [username for username, count in account_operation_counts.items() if count <= 0]
    observed_operation_names = set((summary.get("ops") or {}).keys()) | attempted_operations
    missing_operations = sorted(set(operation_names) - observed_operation_names)
    successful_operation_counts = {
        name: int((summary.get("ops") or {}).get(name, {}).get("successful_2xx") or 0)
        for name in operation_names
    }
    operations_without_success = sorted(
        name
        for name in GLOBAL_SUCCESS_REQUIRED_OPERATIONS
        if successful_operation_counts.get(name, 0) <= 0
    )
    account_success_counts = {
        username: dict(
            ((summary.get("accounts") or {}).get(username, {}).get("successful_operations") or {})
        )
        for username in configured_account_names
    }
    account_success_gaps = {
        username: sorted(
            name
            for name in ACCOUNT_SUCCESS_REQUIRED_OPERATIONS
            if int(account_success_counts.get(username, {}).get(name) or 0) <= 0
        )
        for username in configured_account_names
    }
    account_success_gaps = {
        username: gaps
        for username, gaps in account_success_gaps.items()
        if gaps
    }
    if args.require_all_accounts and (missing_accounts or accounts_without_operations):
        degraded_reasons.append("configured_account_coverage_incomplete")
    if args.require_operation_coverage and missing_operations:
        degraded_reasons.append("operation_rotation_coverage_incomplete")
    if args.require_operation_success and operations_without_success:
        degraded_reasons.append("operation_positive_path_coverage_incomplete")
    if args.require_account_success and account_success_gaps:
        degraded_reasons.append("account_positive_path_coverage_incomplete")
    total_ops_per_second = round(summary_total_ops / elapsed_seconds, 2) if elapsed_seconds > 0 else 0.0
    accepted_ops_per_second = round(accepted_ops / elapsed_seconds, 2) if elapsed_seconds > 0 else 0.0
    server_busy_ops_per_second = round(server_busy_ops / elapsed_seconds, 2) if elapsed_seconds > 0 else 0.0
    worker_telemetry_summary.update({
        "throughput_operations_per_minute": round(total_ops_per_second * 60.0, 6),
        "accepted_operations_per_minute": round(accepted_ops_per_second * 60.0, 6),
        "blocked_worker_events": server_busy_ops,
        "blocked_worker_event_rate": round(server_busy_ops / summary_total_ops, 6) if summary_total_ops else 0.0,
    })

    payload = {
        "ok": not degraded_reasons if args.allow_server_busy else (not degraded_reasons and transport_failure_count == 0),
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "started_at": started_at,
        "finished_at": utc_now(),
        "base_url": args.base_url,
        "logical_users": int(args.logical_users),
        "total_ops_requested": total_ops,
        "concurrency": concurrency,
        "session_pool_requested": requested_session_pool,
        "session_pool_resolved": int(session_pool),
        "session_pool_mode": session_pool_mode,
        "session_pool_created": len(clients),
        "session_mode": args.session_mode,
        "operation_mode": args.operation_mode,
        "rotation_offset": max(0, int(args.rotation_offset or 0)),
        "configured_accounts": configured_account_names,
        "active_accounts": active_account_names,
        "account_login_results": account_login_results,
        "account_operation_counts": account_operation_counts,
        "missing_accounts": missing_accounts,
        "accounts_without_operations": accounts_without_operations,
        "registered_operations": operation_names,
        "missing_operations": missing_operations,
        "successful_operation_counts": successful_operation_counts,
        "operations_without_success": operations_without_success,
        "account_success_counts": account_success_counts,
        "account_success_gaps": account_success_gaps,
        "require_all_accounts": bool(args.require_all_accounts),
        "require_operation_coverage": bool(args.require_operation_coverage),
        "require_operation_success": bool(args.require_operation_success),
        "require_account_success": bool(args.require_account_success),
        "allow_server_busy": bool(args.allow_server_busy),
        "max_server_busy_rate": max_server_busy_rate,
        "max_ordinary_p95_ms": max_ordinary_p95_ms,
        "max_ordinary_p99_ms": max_ordinary_p99_ms,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "throughput_ops_per_second": total_ops_per_second,
        "total_ops_per_second": total_ops_per_second,
        "accepted_ops_per_second": accepted_ops_per_second,
        "server_busy_ops_per_second": server_busy_ops_per_second,
        "worker_telemetry": worker_telemetry_summary,
        "hard_failure_rate": summary.get("hard_failure_rate_excluding_503", 0),
        "seed": seed,
        "login_elapsed_seconds": round(login_elapsed_seconds, 3),
        "login_summary": login_stats.summary(),
        "budget_counts": budget.counts(),
        "summary": summary,
        "resource_monitor": resource_summary,
        "qos_samples": stats.samples[-60:],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
