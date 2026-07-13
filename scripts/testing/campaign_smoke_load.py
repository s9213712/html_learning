#!/usr/bin/env python3
"""Bounded Level-0 lifecycle load for the campaign harness.

This probe deliberately does *not* claim full-feature coverage.  Its command
line contract is fixed at 180 seconds and 32 concurrent workers.  Half of the
workers exercise public liveness/readiness endpoints and half establish an
authenticated session (including proof of post-login CSRF rotation) before
polling read-only authenticated sentinels.

Credentials are accepted from environment variables only.  The final report
is always written with fsync + atomic replace after a graceful deadline,
stop-file, SIGTERM, or handled failure.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import tempfile
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

import requests
import urllib3


SMOKE_LOAD_SCHEMA_VERSION = "hackme.campaign-smoke-load.v2"
SMOKE_DURATION_SECONDS = 180.0
SMOKE_CONCURRENCY = 32
SMOKE_REQUEST_TIMEOUT_SECONDS = 10.0
ORDINARY_P95_MAX_MS = 3_000.0
ORDINARY_P99_MAX_MS = 8_000.0
AUTH_LOGIN_P99_MAX_MS = 8_000.0
MAX_CONTROLLED_BACKPRESSURE_RATE = 0.05
MAX_BACKPRESSURE_RETRIES = 2
LOAD_SAMPLE_CADENCE_SECONDS = 1.0
DEFAULT_MINIMUM_RUNTIME_SECONDS = 179.0
DEFAULT_MINIMUM_OPERATIONS = 320
PASSWORD_ENV_NAMES = (
    "HACKME_SMOKE_TEST_PASSWORD",
    "HACKME_CAMPAIGN_TEST_PASSWORD",
    "HACKME_TEST_PASSWORD",
    "HTML_LEARNING_TEST_PASSWORD",
    "TEST_PASSWORD",
)
USERNAME_ENV_NAMES = (
    "HACKME_SMOKE_USERNAME",
    "HACKME_CAMPAIGN_TEST_USERNAME",
)
PUBLIC_SENTINELS = ("/api/version", "/api/readyz")
AUTHENTICATED_SENTINELS = ("/api/me", "/api/points/wallet")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Clock(Protocol):
    """Small injectable clock surface used by deterministic unit tests."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    def utc_now(self) -> str: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def utc_now(self) -> str:
        return _utc_now()


@dataclass(frozen=True)
class SmokeCredentials:
    username: str
    password: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "SmokeCredentials":
        env = os.environ if environ is None else environ
        username = next((str(env.get(name) or "").strip() for name in USERNAME_ENV_NAMES if env.get(name)), "test")
        password = next((str(env.get(name) or "") for name in PASSWORD_ENV_NAMES if env.get(name)), "")
        if not password:
            raise ValueError("test credential missing from the approved environment variables")
        return cls(username=username, password=password)


@dataclass(frozen=True)
class SmokeLoadConfig:
    base_url: str
    report_path: Path
    stop_file: Path
    duration_seconds: float = SMOKE_DURATION_SECONDS
    concurrency: int = SMOKE_CONCURRENCY
    minimum_runtime_seconds: float = DEFAULT_MINIMUM_RUNTIME_SECONDS
    minimum_operations: int = DEFAULT_MINIMUM_OPERATIONS
    request_timeout_seconds: float = SMOKE_REQUEST_TIMEOUT_SECONDS
    operation_interval_seconds: float = 0.02
    monitor_interval_seconds: float = 0.10
    silent_worker_seconds: float = 15.0
    enforce_level0_contract: bool = True
    supervisor_controlled: bool = True
    tls_verify: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        numeric = {
            "duration_seconds": self.duration_seconds,
            "minimum_runtime_seconds": self.minimum_runtime_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "operation_interval_seconds": self.operation_interval_seconds,
            "monitor_interval_seconds": self.monitor_interval_seconds,
            "silent_worker_seconds": self.silent_worker_seconds,
        }
        for name, value in numeric.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.duration_seconds <= 0 or self.request_timeout_seconds <= 0 or self.monitor_interval_seconds <= 0:
            raise ValueError("duration, request timeout, and monitor interval must be positive")
        if isinstance(self.concurrency, bool) or not isinstance(self.concurrency, int) or self.concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        if isinstance(self.minimum_operations, bool) or not isinstance(self.minimum_operations, int) or self.minimum_operations < 0:
            raise ValueError("minimum_operations must be a non-negative integer")
        if self.minimum_runtime_seconds > self.duration_seconds:
            raise ValueError("minimum runtime cannot exceed duration")
        if self.enforce_level0_contract and (
            self.duration_seconds != SMOKE_DURATION_SECONDS
            or self.concurrency != SMOKE_CONCURRENCY
            or self.minimum_runtime_seconds < DEFAULT_MINIMUM_RUNTIME_SECONDS
            or self.minimum_operations < DEFAULT_MINIMUM_OPERATIONS
            or self.request_timeout_seconds != SMOKE_REQUEST_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "Level-0 smoke load contract is fixed at 180 seconds, 32 workers, "
                "a 10-second transport ceiling, and its minimum gates"
            )


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably publish one complete JSON document without a partial target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class SmokeLoadRunner:
    """Runs the fixed lifecycle mix and emits fail-closed evidence."""

    def __init__(
        self,
        config: SmokeLoadConfig,
        credentials: SmokeCredentials,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.session_factory = session_factory
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._start_workers = threading.Event()
        self._stop_reason = ""
        self._terminal_error = ""
        self._run_started = 0.0
        self._active_workers = 0
        self._max_active_workers = 0
        self._inflight = 0
        self._max_inflight = 0
        self._workers_ready = 0
        self._workers_completed = 0
        self._attempts = 0
        self._logical_requests = 0
        self._logical_failures = 0
        self._successful_operations = 0
        self._status_counts: Counter[str] = Counter()
        self._transport_errors: Counter[str] = Counter()
        self._latencies_ms: list[float] = []
        self._logical_latencies_ms: list[float] = []
        self._throughput_buckets: Counter[int] = Counter()
        self._load_samples: list[dict[str, Any]] = []
        self._load_sample_next_elapsed = LOAD_SAMPLE_CADENCE_SECONDS
        self._load_interval_started_elapsed = 0.0
        self._load_interval_operations_started = 0
        self._load_interval_observations = 0
        self._load_interval_active_min: int | None = None
        self._load_interval_active_max: int | None = None
        self._load_interval_inflight_min: int | None = None
        self._load_interval_inflight_max: int | None = None
        self._load_samples_overflowed = False
        self._monitor_iterations = 0
        self._operation_metrics: dict[str, Counter[str]] = defaultdict(Counter)
        self._operation_latencies_ms: dict[str, list[float]] = defaultdict(list)
        self._operation_logical_latencies_ms: dict[str, list[float]] = defaultdict(list)
        self._worker_state: dict[int, dict[str, Any]] = {}
        self._csrf_rotations = 0
        self._backpressure_rejections = 0
        self._backpressure_retry_logical_requests = 0
        self._backpressure_retry_successes = 0
        self._backpressure_retry_failures = 0
        self._silent_failures: set[str] = set()
        self._unexpected_errors: list[str] = []

    def request_stop(self, reason: str) -> None:
        normalized = str(reason or "EXTERNAL_STOP").strip().upper()
        with self._lock:
            if not self._stop_reason:
                self._stop_reason = normalized
        self._stop.set()
        self._start_workers.set()

    def _record_error(self, message: str, *, worker_id: int | None = None) -> None:
        compact = " ".join(str(message).split()).replace(self.credentials.password, "[redacted]")[:400]
        with self._lock:
            if len(self._unexpected_errors) < 100:
                self._unexpected_errors.append(compact)
            if worker_id is not None and worker_id in self._worker_state:
                self._worker_state[worker_id]["error"] = compact

    def _mark_progress(self, worker_id: int, *, success: bool) -> None:
        with self._lock:
            row = self._worker_state[worker_id]
            row["last_progress_monotonic"] = self.clock.monotonic()
            row["logical_requests"] += 1
            if success:
                row["logical_successes"] += 1

    def _semantic_success(
        self,
        *,
        path: str,
        operation: str,
        body: object,
    ) -> tuple[bool, str]:
        """Validate endpoint-specific effects; an HTTP status is not success."""

        if not isinstance(body, dict) or body.get("ok") is not True:
            return False, "explicit_ok_true_missing"
        if operation in {"auth.csrf_before_login", "auth.csrf_after_login"}:
            token = body.get("csrf_token")
            return (
                (True, "")
                if isinstance(token, str) and bool(token.strip())
                else (False, "csrf_token_missing")
            )
        if operation == "auth.login":
            return (
                (True, "")
                if isinstance(body.get("msg"), str) and bool(str(body.get("msg")).strip())
                else (False, "login_confirmation_missing")
            )
        if path == "/api/version":
            required = ("app", "release_id", "version", "started_at")
            missing = [name for name in required if not str(body.get(name) or "").strip()]
            if missing or not isinstance(body.get("server_time"), dict):
                return False, "version_identity_missing:" + ",".join(missing or ["server_time"])
            return True, ""
        if path == "/api/readyz":
            checks = body.get("checks")
            db = checks.get("db") if isinstance(checks, dict) else None
            if (
                body.get("status") != "ready"
                or not isinstance(db, dict)
                or db.get("ok") is not True
                or not isinstance(body.get("backpressure"), dict)
            ):
                return False, "layered_readiness_invariant_failed"
            return True, ""
        if path == "/api/me":
            if (
                str(body.get("username") or "") != self.credentials.username
                or isinstance(body.get("id"), bool)
                or not isinstance(body.get("id"), int)
                or int(body.get("id") or 0) <= 0
                or not str(body.get("role") or "").strip()
                or not str(body.get("status") or "").strip()
            ):
                return False, "authenticated_identity_invariant_failed"
            return True, ""
        if path == "/api/points/wallet":
            wallet = body.get("wallet")
            if not isinstance(wallet, dict):
                return False, "wallet_missing"
            for name in ("points_balance", "points_frozen"):
                value = wallet.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return False, f"wallet_{name}_invalid"
            return True, ""
        return False, "unregistered_semantic_contract"

    def _request(
        self,
        session: requests.Session,
        worker_id: int,
        method: str,
        path: str,
        *,
        csrf_token: str = "",
        json_body: Mapping[str, Any] | None = None,
        operation: str,
    ) -> tuple[bool, dict[str, Any]]:
        headers = {"Connection": "close"}
        if csrf_token:
            headers["X-CSRF-Token"] = csrf_token
        logical_started = self.clock.monotonic()
        retry_count = 0
        with self._lock:
            self._logical_requests += 1
            self._operation_metrics[operation]["logical_requests"] += 1

        def finish_logical(success: bool) -> None:
            elapsed_ms = max(
                0.0,
                (self.clock.monotonic() - logical_started) * 1000.0,
            )
            with self._lock:
                self._logical_latencies_ms.append(elapsed_ms)
                self._operation_logical_latencies_ms[operation].append(elapsed_ms)
                if success:
                    self._successful_operations += 1
                    self._operation_metrics[operation]["logical_successes"] += 1
                    second = max(0, int(self.clock.monotonic() - self._run_started))
                    self._throughput_buckets[second] += 1
                    if retry_count:
                        self._backpressure_retry_successes += 1
                else:
                    self._logical_failures += 1
                    self._operation_metrics[operation]["logical_failures"] += 1
                    if retry_count:
                        self._backpressure_retry_failures += 1
            self._mark_progress(worker_id, success=success)

        while True:
            started = self.clock.monotonic()
            retry_delay_after_attempt: float | None = None
            with self._lock:
                self._attempts += 1
                self._inflight += 1
                self._max_inflight = max(self._max_inflight, self._inflight)
                self._operation_metrics[operation]["wire_attempts"] += 1
            try:
                response = session.request(
                    method,
                    f"{self.config.base_url.rstrip('/')}{path}",
                    headers=headers,
                    json=dict(json_body) if json_body is not None else None,
                    timeout=self.config.request_timeout_seconds,
                )
                status = int(response.status_code)
                try:
                    body = response.json() if getattr(response, "content", b"x") else {}
                except Exception as exc:
                    body = {}
                    self._record_error(
                        f"worker {worker_id} {operation} invalid JSON: {exc.__class__.__name__}",
                        worker_id=worker_id,
                    )
                response_headers = getattr(response, "headers", {}) or {}
                controlled_backpressure = bool(
                    status == 503
                    and isinstance(body, dict)
                    and body.get("error") == "server_busy"
                    and str(response_headers.get("X-Hackme-Backpressure-Rejected") or "") == "1"
                )
                semantic_ok, semantic_reason = self._semantic_success(
                    path=path,
                    operation=operation,
                    body=body,
                )
                success = status == 200 and semantic_ok
                with self._lock:
                    self._status_counts[str(status)] += 1
                    if success:
                        self._operation_metrics[operation]["wire_semantic_successes"] += 1
                    elif controlled_backpressure:
                        self._operation_metrics[operation]["wire_controlled_rejections"] += 1
                    else:
                        self._operation_metrics[operation]["wire_uncontrolled_failures"] += 1
                    if controlled_backpressure:
                        self._backpressure_rejections += 1
                if controlled_backpressure and retry_count < MAX_BACKPRESSURE_RETRIES:
                    if retry_count == 0:
                        with self._lock:
                            self._backpressure_retry_logical_requests += 1
                    retry_count += 1
                    retry_after = body.get("retry_after_seconds") if isinstance(body, dict) else 0
                    try:
                        retry_after_seconds = max(0.0, min(2.0, float(retry_after or 0.0)))
                    except (TypeError, ValueError):
                        retry_after_seconds = 0.0
                    retry_delay_after_attempt = retry_after_seconds
                if retry_delay_after_attempt is None:
                    if not success:
                        self._record_error(
                            f"worker {worker_id} {operation} returned status={status} "
                            f"semantic_ok={semantic_ok} semantic_reason={semantic_reason}",
                            worker_id=worker_id,
                        )
                    finish_logical(success)
                    return success, body if isinstance(body, dict) else {}
            except Exception as exc:
                name = exc.__class__.__name__
                with self._lock:
                    self._transport_errors[name] += 1
                    self._operation_metrics[operation]["wire_transport_errors"] += 1
                self._record_error(
                    f"worker {worker_id} {operation} transport {name}: {exc}",
                    worker_id=worker_id,
                )
                finish_logical(False)
                return False, {}
            finally:
                elapsed_ms = max(0.0, (self.clock.monotonic() - started) * 1000.0)
                with self._lock:
                    self._latencies_ms.append(elapsed_ms)
                    self._operation_latencies_ms[operation].append(elapsed_ms)
                    self._inflight = max(0, self._inflight - 1)
            if retry_delay_after_attempt is not None:
                self.clock.sleep(retry_delay_after_attempt)

    def _authenticate(self, session: requests.Session, worker_id: int) -> bool:
        ok, public_body = self._request(
            session,
            worker_id,
            "GET",
            "/api/csrf-token",
            operation="auth.csrf_before_login",
        )
        public_token = str(public_body.get("csrf_token") or "")
        if not ok or not public_token:
            self._record_error(f"worker {worker_id} public CSRF bootstrap failed", worker_id=worker_id)
            return False
        ok, _ = self._request(
            session,
            worker_id,
            "POST",
            "/api/login",
            csrf_token=public_token,
            json_body={"username": self.credentials.username, "password": self.credentials.password},
            operation="auth.login",
        )
        if not ok:
            return False
        ok, user_body = self._request(
            session,
            worker_id,
            "GET",
            "/api/csrf-token",
            operation="auth.csrf_after_login",
        )
        user_token = str(user_body.get("csrf_token") or "")
        if not ok or not user_token or user_token == public_token:
            self._record_error(f"worker {worker_id} post-login CSRF token did not rotate", worker_id=worker_id)
            return False
        with self._lock:
            self._csrf_rotations += 1
            self._worker_state[worker_id]["csrf_rotated"] = True
        return True

    def _worker(self, worker_id: int, authenticated: bool) -> None:
        session = self.session_factory()
        # The isolated load target is created with a per-run self-signed
        # certificate. Production security boundaries are tested separately
        # by the sentinel target; this client must still exercise HTTPS rather
        # than rejecting every operation before it reaches the application.
        try:
            session.verify = bool(self.config.tls_verify)
        except Exception:
            pass
        with self._lock:
            self._workers_ready += 1
            self._active_workers += 1
            self._max_active_workers = max(self._max_active_workers, self._active_workers)
            self._worker_state[worker_id] = {
                "kind": "authenticated" if authenticated else "public",
                "logical_requests": 0,
                "logical_successes": 0,
                "csrf_rotated": False,
                "last_progress_monotonic": self.clock.monotonic(),
                "error": "",
                "terminal_state": "STARTING",
            }
        try:
            self._start_workers.wait()
            if self._stop.is_set():
                return
            with self._lock:
                self._worker_state[worker_id]["terminal_state"] = "ACTIVE"
            if authenticated and not self._authenticate(session, worker_id):
                return
            paths = AUTHENTICATED_SENTINELS if authenticated else PUBLIC_SENTINELS
            cursor = worker_id % len(paths)
            while not self._stop.is_set():
                path = paths[cursor % len(paths)]
                cursor += 1
                operation = ("authenticated" if authenticated else "public") + ":" + path
                self._request(session, worker_id, "GET", path, operation=operation)
                if self.config.operation_interval_seconds:
                    self.clock.sleep(self.config.operation_interval_seconds)
                else:
                    self.clock.sleep(0)
        except Exception as exc:
            self._record_error(f"worker {worker_id} uncaught {exc.__class__.__name__}: {exc}", worker_id=worker_id)
        finally:
            try:
                close = getattr(session, "close", None)
                if callable(close):
                    close()
            except Exception as exc:
                self._record_error(f"worker {worker_id} session close failed: {exc.__class__.__name__}", worker_id=worker_id)
            with self._lock:
                row = self._worker_state[worker_id]
                if not self._stop.is_set() and not row.get("error"):
                    self._silent_failures.add(f"worker_{worker_id}_exited_without_stop")
                row["terminal_state"] = "STOPPED" if self._stop.is_set() else "FAILED"
                self._active_workers = max(0, self._active_workers - 1)
                self._workers_completed += 1

    def _wait_for_workers(self) -> bool:
        deadline = time.monotonic() + max(5.0, self.config.request_timeout_seconds * 2)
        while time.monotonic() < deadline:
            with self._lock:
                if self._workers_ready == self.config.concurrency:
                    return True
            if self._stop.is_set():
                return False
            time.sleep(0.005)
        self._record_error(f"only {self._workers_ready}/{self.config.concurrency} workers became ready")
        return False

    def _maximum_load_samples(self) -> int:
        """Return a hard evidence bound, including one terminal sample.

        A one-second aggregate remains detailed enough to establish worker and
        throughput continuity.  It also caps a 24-hour run at 86,402 rows
        instead of allowing monitor polling speed to control artifact size.
        """

        horizon = self.config.duration_seconds + (
            60.0 if self.config.supervisor_controlled else 0.0
        )
        return int(math.ceil(horizon / LOAD_SAMPLE_CADENCE_SECONDS)) + 2

    def _record_load_observation(self, elapsed: float, *, terminal: bool = False) -> None:
        """Aggregate high-frequency monitor observations at a fixed cadence."""

        elapsed = max(0.0, float(elapsed))
        with self._lock:
            active = int(self._active_workers)
            inflight = int(self._inflight)
            completed = int(self._successful_operations)
            self._load_interval_observations += 1
            self._load_interval_active_min = (
                active
                if self._load_interval_active_min is None
                else min(self._load_interval_active_min, active)
            )
            self._load_interval_active_max = (
                active
                if self._load_interval_active_max is None
                else max(self._load_interval_active_max, active)
            )
            self._load_interval_inflight_min = (
                inflight
                if self._load_interval_inflight_min is None
                else min(self._load_interval_inflight_min, inflight)
            )
            self._load_interval_inflight_max = (
                inflight
                if self._load_interval_inflight_max is None
                else max(self._load_interval_inflight_max, inflight)
            )
            if not terminal and elapsed < self._load_sample_next_elapsed:
                return

            maximum = self._maximum_load_samples()
            regular_limit = max(0, maximum - 1)
            if (not terminal and len(self._load_samples) >= regular_limit) or len(self._load_samples) >= maximum:
                self._load_samples_overflowed = True
                return

            self._load_samples.append({
                "interval_started_seconds": round(self._load_interval_started_elapsed, 6),
                "elapsed_seconds": round(elapsed, 6),
                "interval_seconds": round(max(0.0, elapsed - self._load_interval_started_elapsed), 6),
                "observation_count": self._load_interval_observations,
                # Current-value aliases are retained for existing evidence
                # consumers; min/max fields make the interval proof explicit.
                "active_workers": active,
                "active_workers_min": self._load_interval_active_min,
                "active_workers_max": self._load_interval_active_max,
                "inflight_requests": inflight,
                "inflight_requests_min": self._load_interval_inflight_min,
                "inflight_requests_max": self._load_interval_inflight_max,
                "operations_completed": completed,
                "operations_completed_delta": max(
                    0,
                    completed - self._load_interval_operations_started,
                ),
                "terminal": terminal,
            })
            self._load_interval_started_elapsed = elapsed
            self._load_interval_operations_started = completed
            self._load_interval_observations = 0
            self._load_interval_active_min = None
            self._load_interval_active_max = None
            self._load_interval_inflight_min = None
            self._load_interval_inflight_max = None
            self._load_sample_next_elapsed = (
                math.floor(elapsed / LOAD_SAMPLE_CADENCE_SECONDS) + 1
            ) * LOAD_SAMPLE_CADENCE_SECONDS

    def _monitor(self) -> None:
        while not self._stop.is_set():
            elapsed = max(0.0, self.clock.monotonic() - self._run_started)
            self._monitor_iterations += 1
            self._record_load_observation(elapsed)
            if self.config.stop_file.exists():
                self.request_stop("STOP_FILE")
                break
            if elapsed >= self.config.duration_seconds:
                if not self.config.supervisor_controlled:
                    self.request_stop("DURATION_COMPLETE")
                    break
                if elapsed >= self.config.duration_seconds + 60.0:
                    self.request_stop("SUPERVISOR_STOP_TIMEOUT")
                    break
            with self._lock:
                for worker_id, row in self._worker_state.items():
                    if row.get("terminal_state") != "ACTIVE":
                        continue
                    gap = self.clock.monotonic() - float(row.get("last_progress_monotonic") or self._run_started)
                    if gap > self.config.silent_worker_seconds:
                        self._silent_failures.add(f"worker_{worker_id}_no_progress_{round(gap, 3)}s")
            # Never derive the sleep from ``duration - elapsed``.  In
            # supervisor mode the runner deliberately stays alive after the
            # nominal duration while waiting for the stop file; a zero sleep
            # here previously caused a tight loop and tens of thousands of
            # duplicate-density samples during that short hand-off window.
            self.clock.sleep(self.config.monitor_interval_seconds)

    @staticmethod
    def _percentile(values: Sequence[float], quantile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(float(value) for value in values)
        index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
        return round(ordered[index], 3)

    def _report(self, started_at: str, finished_at: str, runtime_seconds: float) -> dict[str, Any]:
        with self._lock:
            worker_rows = {
                str(worker_id): {
                    key: value
                    for key, value in row.items()
                    if key != "last_progress_monotonic"
                }
                for worker_id, row in sorted(self._worker_state.items())
            }
            no_work_workers = [
                worker_id
                for worker_id, row in self._worker_state.items()
                if int(row.get("logical_successes") or 0) == 0
            ]
            if no_work_workers:
                self._silent_failures.add("workers_with_zero_success:" + ",".join(map(str, sorted(no_work_workers))))
            terminal_reason = self._stop_reason or "FAILED"
            graceful = terminal_reason in {"DURATION_COMPLETE", "STOP_FILE", "SIGTERM"}
            normal_terminal = terminal_reason in {"DURATION_COMPLETE", "STOP_FILE"}
            latency_p95 = self._percentile(self._logical_latencies_ms, 0.95)
            latency_p99 = self._percentile(self._logical_latencies_ms, 0.99)
            login_latencies = self._operation_logical_latencies_ms.get("auth.login") or []
            login_p99 = self._percentile(login_latencies, 0.99)
            backpressure_rate = (
                self._backpressure_rejections / self._attempts
                if self._attempts
                else 0.0
            )
            gates = {
                "minimum_runtime": runtime_seconds >= self.config.minimum_runtime_seconds,
                "minimum_operations": self._successful_operations >= self.config.minimum_operations,
                "all_workers_started": self._workers_ready == self.config.concurrency,
                "all_workers_stopped": self._workers_completed == self.config.concurrency and self._active_workers == 0,
                "full_concurrency_observed": self._max_active_workers == self.config.concurrency,
                "authenticated_csrf_rotation": self._csrf_rotations == self.config.concurrency // 2,
                "zero_transport_errors": not self._transport_errors,
                "zero_unexpected_errors": not self._unexpected_errors,
                "zero_silent_failures": not self._silent_failures,
                "ordinary_latency_p95_within_3s": bool(self._logical_latencies_ms) and latency_p95 <= ORDINARY_P95_MAX_MS,
                "ordinary_latency_p99_within_8s": bool(self._logical_latencies_ms) and latency_p99 <= ORDINARY_P99_MAX_MS,
                "auth_login_p99_within_8s": (
                    len(login_latencies) == self.config.concurrency // 2
                    and login_p99 <= AUTH_LOGIN_P99_MAX_MS
                ),
                "controlled_backpressure_rate_within_5pct": backpressure_rate <= MAX_CONTROLLED_BACKPRESSURE_RATE,
                "controlled_backpressure_retries_terminal": (
                    self._backpressure_retry_failures == 0
                    and self._backpressure_retry_successes
                    == self._backpressure_retry_logical_requests
                ),
                "load_samples_bounded": (
                    not self._load_samples_overflowed
                    and len(self._load_samples) <= self._maximum_load_samples()
                ),
                "normal_terminal": normal_terminal,
            }
            expected_terminal = (
                terminal_reason == "STOP_FILE"
                if self.config.supervisor_controlled
                else terminal_reason == "DURATION_COMPLETE"
            )
            ok = all(gates.values()) and expected_terminal
            if ok:
                classification = "PASS"
            elif terminal_reason in {
                "RUNNER_FAILURE",
                "WORKER_START_FAILURE",
                "SUPERVISOR_STOP_TIMEOUT",
            }:
                classification = "FAIL_HARNESS"
            elif self._transport_errors:
                classification = "FAIL_INFRA"
            else:
                classification = "FAIL_PRODUCT"
            return {
                "schema_version": SMOKE_LOAD_SCHEMA_VERSION,
                "probe": "campaign_level0_lifecycle_load",
                "scope": {
                    "level": 0,
                    "full_feature_coverage_claimed": False,
                    "description": "public/authenticated lifecycle sentinel mix only",
                },
                "started_at": started_at,
                "finished_at": finished_at,
                "runtime_seconds": round(runtime_seconds, 6),
                "terminal": {
                    "state": "COMPLETED" if terminal_reason == "DURATION_COMPLETE" else ("STOPPED" if terminal_reason == "STOP_FILE" else "INTERRUPTED"),
                    "reason": terminal_reason,
                    "graceful": graceful,
                    "normal": normal_terminal,
                    "error": self._terminal_error,
                },
                "contract": {
                    "configured_duration_seconds": self.config.duration_seconds,
                    "configured_concurrency": self.config.concurrency,
                    "public_workers": (self.config.concurrency + 1) // 2,
                    "authenticated_workers": self.config.concurrency // 2,
                    "minimum_runtime_seconds": self.config.minimum_runtime_seconds,
                    "minimum_operations": self.config.minimum_operations,
                    "ordinary_p95_max_ms": ORDINARY_P95_MAX_MS,
                    "ordinary_p99_max_ms": ORDINARY_P99_MAX_MS,
                    "auth_login_p99_max_ms": AUTH_LOGIN_P99_MAX_MS,
                    "maximum_controlled_backpressure_rate": MAX_CONTROLLED_BACKPRESSURE_RATE,
                    "maximum_backpressure_retries": MAX_BACKPRESSURE_RETRIES,
                    "supervisor_controlled": self.config.supervisor_controlled,
                    "failsafe_duration_seconds": (
                        self.config.duration_seconds + 60.0
                        if self.config.supervisor_controlled
                        else self.config.duration_seconds
                    ),
                    "credentials_transport": "environment_only",
                    "transport_security": "https_isolated_self_signed",
                    "tls_verify": self.config.tls_verify,
                },
                "metrics": {
                    "workers_ready": self._workers_ready,
                    "workers_completed": self._workers_completed,
                    "active_workers_final": self._active_workers,
                    "max_active_workers": self._max_active_workers,
                    "inflight_final": self._inflight,
                    "max_inflight": self._max_inflight,
                    "attempts": self._attempts,
                    "wire_attempts": self._attempts,
                    "logical_requests": self._logical_requests,
                    "logical_successes": self._successful_operations,
                    "logical_failures": self._logical_failures,
                    "operations_completed": self._successful_operations,
                    "operations_per_second": round(self._successful_operations / runtime_seconds, 6) if runtime_seconds > 0 else 0.0,
                    "throughput_by_second": {str(key): value for key, value in sorted(self._throughput_buckets.items())},
                    "load_samples": list(self._load_samples),
                    "load_sample_policy": {
                        "cadence_seconds": LOAD_SAMPLE_CADENCE_SECONDS,
                        "maximum_samples": self._maximum_load_samples(),
                        "actual_samples": len(self._load_samples),
                        "monitor_iterations": self._monitor_iterations,
                        "overflowed": self._load_samples_overflowed,
                        "aggregation": "interval_min_max_and_terminal_throughput",
                    },
                    "status_counts": dict(sorted(self._status_counts.items())),
                    "transport_errors": dict(sorted(self._transport_errors.items())),
                    "controlled_backpressure": {
                        "rejections": self._backpressure_rejections,
                        "wire_rejections": self._backpressure_rejections,
                        "retry_logical_requests": self._backpressure_retry_logical_requests,
                        "logical_requests_retried": self._backpressure_retry_logical_requests,
                        "retry_successes": self._backpressure_retry_successes,
                        "terminal_successes": self._backpressure_retry_successes,
                        "retry_failures": self._backpressure_retry_failures,
                        "terminal_failures": self._backpressure_retry_failures,
                        "rejection_rate": round(backpressure_rate, 8),
                        "wire_rejection_rate": round(backpressure_rate, 8),
                        "semantics": "wire rejections are pressure evidence; only terminal logical failures are product failures",
                    },
                    "csrf_rotations": self._csrf_rotations,
                    "latency_ms": {
                        "samples": len(self._logical_latencies_ms),
                        "p50": self._percentile(self._logical_latencies_ms, 0.50),
                        "p95": latency_p95,
                        "p99": latency_p99,
                        "max": round(max(self._logical_latencies_ms), 3) if self._logical_latencies_ms else 0.0,
                    },
                    "wire_attempt_latency_ms": {
                        "samples": len(self._latencies_ms),
                        "p50": self._percentile(self._latencies_ms, 0.50),
                        "p95": self._percentile(self._latencies_ms, 0.95),
                        "p99": self._percentile(self._latencies_ms, 0.99),
                        "max": round(max(self._latencies_ms), 3) if self._latencies_ms else 0.0,
                    },
                    "operations": {
                        name: {
                            **dict(sorted(counts.items())),
                            "latency_ms": {
                                "samples": len(self._operation_logical_latencies_ms.get(name) or []),
                                "p50": self._percentile(self._operation_logical_latencies_ms.get(name) or [], 0.50),
                                "p95": self._percentile(self._operation_logical_latencies_ms.get(name) or [], 0.95),
                                "p99": self._percentile(self._operation_logical_latencies_ms.get(name) or [], 0.99),
                                "max": round(max(self._operation_logical_latencies_ms.get(name) or [0.0]), 3),
                            },
                            "wire_attempt_latency_ms": {
                                "samples": len(self._operation_latencies_ms.get(name) or []),
                                "p50": self._percentile(self._operation_latencies_ms.get(name) or [], 0.50),
                                "p95": self._percentile(self._operation_latencies_ms.get(name) or [], 0.95),
                                "p99": self._percentile(self._operation_latencies_ms.get(name) or [], 0.99),
                                "max": round(max(self._operation_latencies_ms.get(name) or [0.0]), 3),
                            },
                        }
                        for name, counts in sorted(self._operation_metrics.items())
                    },
                },
                "workers": worker_rows,
                "unexpected_errors": list(self._unexpected_errors),
                "silent_failures": sorted(self._silent_failures),
                "gates": gates,
                "classification": classification,
                "ok": ok,
            }

    def run(self) -> dict[str, Any]:
        started_at = self.clock.utc_now()
        threads: list[threading.Thread] = []
        self._run_started = self.clock.monotonic()
        try:
            if self.config.stop_file.exists():
                self.request_stop("STOP_FILE")
            for worker_id in range(self.config.concurrency):
                thread = threading.Thread(
                    target=self._worker,
                    args=(worker_id, bool(worker_id % 2)),
                    name=f"campaign-smoke-{worker_id:02d}",
                    daemon=True,
                )
                threads.append(thread)
                thread.start()
            if not self._wait_for_workers():
                if not self._stop_reason:
                    self.request_stop("WORKER_START_FAILURE")
            self._run_started = self.clock.monotonic()
            self._start_workers.set()
            if not self._stop.is_set():
                self._monitor()
        except BaseException as exc:
            self._terminal_error = f"{exc.__class__.__name__}: {exc}".replace(
                self.credentials.password,
                "[redacted]",
            )[:400]
            self._record_error(f"runner failure {self._terminal_error}")
            self.request_stop("RUNNER_FAILURE")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                # Preserve the report first; callers can interpret terminal
                # state rather than losing evidence to an immediate re-raise.
                pass
        finally:
            self._stop.set()
            self._start_workers.set()
            real_join_deadline = time.monotonic() + self.config.request_timeout_seconds + 2.0
            for thread in threads:
                thread.join(timeout=max(0.0, real_join_deadline - time.monotonic()))
            for thread in threads:
                if thread.is_alive():
                    self._silent_failures.add(f"thread_alive_after_join:{thread.name}")
            runtime = max(0.0, self.clock.monotonic() - self._run_started)
            self._record_load_observation(runtime, terminal=True)
            finished_at = self.clock.utc_now()
            report = self._report(started_at, finished_at, runtime)
            atomic_write_json(self.config.report_path, report)
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the fixed 180-second Level-0 campaign lifecycle load")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=SMOKE_REQUEST_TIMEOUT_SECONDS,
    )
    parser.add_argument("--operation-interval-seconds", type=float, default=0.02)
    return parser


def _write_bootstrap_failure(args: argparse.Namespace, exc: BaseException) -> None:
    now = _utc_now()
    payload = {
        "schema_version": SMOKE_LOAD_SCHEMA_VERSION,
        "probe": "campaign_level0_lifecycle_load",
        "scope": {"level": 0, "full_feature_coverage_claimed": False},
        "started_at": now,
        "finished_at": now,
        "runtime_seconds": 0.0,
        "terminal": {
            "state": "FAILED",
            "reason": "BOOTSTRAP_FAILURE",
            "graceful": True,
            "normal": False,
            "error": f"{exc.__class__.__name__}: {exc}"[:400],
        },
        "gates": {},
        "classification": "FAIL_HARNESS",
        "ok": False,
    }
    atomic_write_json(args.report, payload)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = SmokeLoadConfig(
            base_url=args.base_url,
            report_path=args.report,
            stop_file=args.stop_file,
            request_timeout_seconds=args.request_timeout_seconds,
            operation_interval_seconds=args.operation_interval_seconds,
        )
        credentials = SmokeCredentials.from_environment()
        runner = SmokeLoadRunner(config, credentials)
    except BaseException as exc:
        _write_bootstrap_failure(args, exc)
        return 2

    previous_handlers: dict[int, Any] = {}

    def handle_signal(signum: int, _frame: Any) -> None:
        reason = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        runner.request_stop(reason)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, handle_signal)
    try:
        report = runner.run()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
