#!/usr/bin/env python3
"""Run a multi-account, full-feature operational soak against an isolated server."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable
from urllib.parse import urlparse

import requests


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.operation_coverage import (  # noqa: E402
    ACCOUNT_SUCCESS_REQUIRED_OPERATIONS,
    GLOBAL_SUCCESS_REQUIRED_OPERATIONS,
)
from scripts.testing.campaign_load import (  # noqa: E402
    EffectiveLoadWindow,
    summarize_target_load,
)
from scripts.testing.campaign_activation import (  # noqa: E402
    CORE_ACK_SCHEMA_VERSION,
    CORE_ACTIVATION_SCHEMA_VERSION,
    CORE_READY_SCHEMA_VERSION,
    ActivationArtifactError,
    artifact_exists,
    assert_fresh_artifact_paths,
    canonical_digest,
    secure_read_json,
    secure_write_once_json,
)
from scripts.testing.campaign_state import process_start_ticks  # noqa: E402
try:
    from scripts.testing.operational_campaign_24h import (  # noqa: E402
        EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION,
        OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
        SUPERVISED_LOAD_POLICIES,
        SUPERVISED_RUNNER_PROFILES,
    )
    _IMPORT_ERROR: ModuleNotFoundError | None = None
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by bare runners
    # Keep --help and the operator-facing preflight usable even when the QA
    # environment has not installed the runtime dependency set yet.
    _IMPORT_ERROR = exc
    EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION = "unavailable"
    OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION = "unavailable"
    SUPERVISED_LOAD_POLICIES = {
        "formal": {
            "ramp_levels": [],
            "minimum_ramp_stage_seconds": {},
            "minimum_target_load_coverage": 0.0,
        }
    }
    SUPERVISED_RUNNER_PROFILES = {}

SYSTEM_STRESS = ROOT / "scripts" / "testing" / "system_stress_probe.py"
POINTS_STRESS = ROOT / "scripts" / "testing" / "points_chain_destructive_stress.py"
PLAYWRIGHT_DEEP = ROOT / "scripts" / "testing" / "playwright_deep_site_check.py"
DB_STRESS = ROOT / "scripts" / "testing" / "db_stress_probe.py"
OPERATION_COVERAGE = ROOT / "scripts" / "testing" / "operation_coverage.py"
CAMPAIGN_ACTIVATION = ROOT / "scripts" / "testing" / "campaign_activation.py"
MIN_SIGNOFF_SECONDS = 8 * 60 * 60
HARNESS_FILES = (
    Path(__file__).resolve(),
    SYSTEM_STRESS,
    POINTS_STRESS,
    PLAYWRIGHT_DEEP,
    DB_STRESS,
    OPERATION_COVERAGE,
    CAMPAIGN_ACTIVATION,
)
SENSITIVE_COMMAND_FLAGS = {
    "--root-password",
    "--manager-password",
    "--test-password",
    "--account-password",
    "--accounts",
}
SUPERVISOR_COMPLETION_REASONS = frozenset({"required_continuous_active_duration_completed"})
FORMAL_RAMP_LEVELS = tuple(SUPERVISED_LOAD_POLICIES["formal"]["ramp_levels"])
RAMP_MINIMUM_STAGE_SECONDS = {
    level: {
        int(stage): float(seconds)
        for stage, seconds in policy["minimum_ramp_stage_seconds"].items()
    }
    for level, policy in SUPERVISED_LOAD_POLICIES.items()
}
MINIMUM_TARGET_LOAD_COVERAGE = float(
    SUPERVISED_LOAD_POLICIES["formal"]["minimum_target_load_coverage"]
)
SYSTEM_STRESS_NON_WORKER_THREADS = 3
CORE_ACTIVATION_MAX_FUTURE_SECONDS = 30.0
CORE_ACTIVATION_LATE_TOLERANCE_SECONDS = 0.25
SETUP_RETRY_ATTEMPTS = 8
SETUP_RETRY_MAX_SECONDS = 30.0
SOAK_STORAGE_QUOTA_MB = 1024
SOAK_STORAGE_MAX_FILE_SIZE_MB = 512
SOAK_STORAGE_UPLOAD_RATE_LIMIT_PER_DAY = 10_000
# These resource-heavy positive paths have dedicated campaign scenarios with
# stronger evidence than the synchronized core rotation can provide.  The core
# still dispatches their status/playback operations, but must not relabel a
# fallback status request as a successful generation or ready HLS stream.
SOAK_DEFERRED_SUCCESS_OPERATIONS = frozenset({"hf_generate", "hls_master"})
SOAK_REQUIRED_SUCCESS_OPERATIONS = (
    GLOBAL_SUCCESS_REQUIRED_OPERATIONS - SOAK_DEFERRED_SUCCESS_OPERATIONS
)


class CoreActivationStopped(RuntimeError):
    pass


def campaign_load_policy(campaign_level: str) -> dict[str, Any]:
    """Return a bounded policy for supervised and standalone probe runs."""

    level = str(campaign_level or "").strip().lower()
    if level == "standalone":
        # Standalone is intentionally non-signoff evidence.  It still needs a
        # complete, non-ramping policy so the default CLI mode can run instead
        # of indexing the supervised-only policy table with an unsupported key.
        return dict(SUPERVISED_LOAD_POLICIES["smoke"])
    return dict(SUPERVISED_LOAD_POLICIES[level])


def validate_run_policy(base_url: str, runtime_root: Path, *, owns_target: bool) -> None:
    parsed = urlparse(str(base_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base URL must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base URL must not contain credentials")
    hostname = parsed.hostname.lower()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if not loopback and not owns_target:
        raise ValueError("non-loopback destructive soak target requires --i-own-this-target")
    tmp_root = Path("/tmp").resolve()
    resolved_runtime = runtime_root.resolve()
    if resolved_runtime != tmp_root and tmp_root not in resolved_runtime.parents:
        raise ValueError("operational soak runtime and reports must remain under /tmp")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * pct)))
    return round(ordered[index], 3)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "path": str(path)}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def activation_contract(args: argparse.Namespace, runtime_root: Path) -> dict[str, Any]:
    required = args.campaign_level in {"rehearsal", "soak", "formal"}
    if not required:
        return {"required": False}
    fields = {
        "campaign_uuid": str(args.campaign_uuid or ""),
        "campaign_commit": str(args.campaign_commit or ""),
        "runner_profile_digest": str(args.runner_profile_digest or ""),
        "campaign_runner_pid": int(args.campaign_runner_pid or 0),
        "campaign_runner_start_ticks": int(args.campaign_runner_start_ticks or 0),
        "ready_file": str(args.activation_ready_file or ""),
        "activation_file": str(args.activation_file or ""),
        "ack_file": str(args.activation_ack_file or ""),
        "nonce": str(os.environ.get("HACKME_CORE_ACTIVATION_NONCE") or ""),
    }
    missing = sorted(name for name, value in fields.items() if not value)
    if missing:
        raise ActivationArtifactError(
            "formal core activation contract is incomplete: " + ", ".join(missing)
        )
    try:
        parsed_uuid = str(uuid.UUID(fields["campaign_uuid"]))
    except (ValueError, AttributeError) as exc:
        raise ActivationArtifactError("core activation campaign UUID is invalid") from exc
    if parsed_uuid != fields["campaign_uuid"]:
        raise ActivationArtifactError("core activation campaign UUID is not canonical")
    if len(fields["campaign_commit"]) != 40 or any(
        value not in "0123456789abcdef" for value in fields["campaign_commit"]
    ):
        raise ActivationArtifactError("core activation commit is invalid")
    if len(fields["nonce"]) != 64 or any(
        value not in "0123456789abcdef" for value in fields["nonce"]
    ):
        raise ActivationArtifactError("core activation nonce is invalid")
    expected_profile_digest = canonical_digest(
        SUPERVISED_RUNNER_PROFILES[args.campaign_level]
    )
    if fields["runner_profile_digest"] != expected_profile_digest:
        raise ActivationArtifactError("core activation runner profile digest mismatch")
    expected_profile = SUPERVISED_RUNNER_PROFILES[args.campaign_level]
    runtime_values = {
        "account_count": int(args.account_count),
        "round_ops": int(args.round_ops),
        "concurrency": int(args.concurrency),
        "session_pool": int(args.session_pool),
    }
    mismatches = sorted(
        name
        for name, value in runtime_values.items()
        if float(value) != float(expected_profile[name])
    )
    if mismatches:
        raise ActivationArtifactError(
            "core activation runtime profile mismatch: " + ", ".join(mismatches)
        )
    if fields["campaign_runner_pid"] != os.getppid():
        raise ActivationArtifactError("core activation campaign runner PID mismatch")
    if fields["campaign_runner_start_ticks"] != process_start_ticks(os.getppid()):
        raise ActivationArtifactError("core activation campaign runner identity mismatch")
    paths = {
        name: Path(fields[name]).absolute()
        for name in ("ready_file", "activation_file", "ack_file")
    }
    if len({path.parent for path in paths.values()}) != 1:
        raise ActivationArtifactError("core activation artifacts must share one directory")
    for name, path in paths.items():
        try:
            path.relative_to(runtime_root)
        except ValueError as exc:
            raise ActivationArtifactError(
                f"core activation {name} is outside runtime root"
            ) from exc
    assert_fresh_artifact_paths(list(paths.values()))
    return {
        "required": True,
        **fields,
        **paths,
        "authority_root": runtime_root,
        "expected_profile_digest": expected_profile_digest,
        "timeout_seconds": max(1.0, float(args.activation_timeout_seconds)),
    }


def publish_ready_and_wait_for_activation(
    contract: dict[str, Any],
    *,
    duration_seconds: int,
    stop_file: Path | None,
) -> dict[str, Any]:
    if not contract.get("required"):
        return {"required": False}
    child_start_ticks = process_start_ticks(os.getpid())
    ready_monotonic_ns = time.monotonic_ns()
    ready_payload = {
        "schema_version": CORE_READY_SCHEMA_VERSION,
        "campaign_uuid": contract["campaign_uuid"],
        "campaign_commit": contract["campaign_commit"],
        "runner_profile_digest": contract["runner_profile_digest"],
        "activation_nonce": contract["nonce"],
        "campaign_runner_pid": contract["campaign_runner_pid"],
        "campaign_runner_start_ticks": contract["campaign_runner_start_ticks"],
        "child_pid": os.getpid(),
        "child_start_ticks": child_start_ticks,
        "ready_sequence": 1,
        "ready_monotonic_ns": ready_monotonic_ns,
        "ready_at": utc_now(),
    }
    ready_sha256 = secure_write_once_json(
        contract["ready_file"],
        ready_payload,
        authority_root=contract["authority_root"],
    )
    deadline = time.monotonic() + float(contract["timeout_seconds"])
    activation_payload: dict[str, Any] = {}
    activation_sha256 = ""
    while time.monotonic() < deadline:
        if stop_file is not None and artifact_exists(stop_file):
            raise CoreActivationStopped("core activation stopped before release")
        if artifact_exists(contract["activation_file"]):
            current_ready, current_ready_sha256 = secure_read_json(
                contract["ready_file"],
                authority_root=contract["authority_root"],
            )
            if current_ready != ready_payload or current_ready_sha256 != ready_sha256:
                raise ActivationArtifactError("core ready artifact changed before activation")
            activation_payload, activation_sha256 = secure_read_json(
                contract["activation_file"],
                authority_root=contract["authority_root"],
            )
            break
        time.sleep(0.05)
    if not activation_payload:
        raise ActivationArtifactError("core activation timed out before one-shot release")
    expected = {
        "schema_version": CORE_ACTIVATION_SCHEMA_VERSION,
        "campaign_uuid": contract["campaign_uuid"],
        "campaign_commit": contract["campaign_commit"],
        "runner_profile_digest": contract["runner_profile_digest"],
        "activation_nonce": contract["nonce"],
        "campaign_runner_pid": contract["campaign_runner_pid"],
        "campaign_runner_start_ticks": contract["campaign_runner_start_ticks"],
        "child_pid": os.getpid(),
        "child_start_ticks": child_start_ticks,
        "ready_sha256": ready_sha256,
        "duration_seconds": int(duration_seconds),
        "activation_sequence": 1,
    }
    mismatched = sorted(
        name for name, expected_value in expected.items()
        if activation_payload.get(name) != expected_value
    )
    if mismatched:
        raise ActivationArtifactError(
            "core activation binding mismatch: " + ", ".join(mismatched)
        )
    activation_monotonic_ns = activation_payload.get("activation_monotonic_ns")
    activation_epoch_ns = activation_payload.get("activation_epoch_ns")
    if (
        isinstance(activation_monotonic_ns, bool)
        or not isinstance(activation_monotonic_ns, int)
        or isinstance(activation_epoch_ns, bool)
        or not isinstance(activation_epoch_ns, int)
    ):
        raise ActivationArtifactError("core activation timestamps are invalid")
    observed_monotonic_ns = time.monotonic_ns()
    if activation_monotonic_ns < ready_monotonic_ns:
        raise ActivationArtifactError("core activation predates child readiness")
    if (
        activation_monotonic_ns
        < observed_monotonic_ns - int(CORE_ACTIVATION_LATE_TOLERANCE_SECONDS * 1e9)
    ):
        raise ActivationArtifactError("core activation schedule was already stale")
    if (
        activation_monotonic_ns - observed_monotonic_ns
        > int(CORE_ACTIVATION_MAX_FUTURE_SECONDS * 1e9)
    ):
        raise ActivationArtifactError("core activation schedule is too far in the future")
    expected_epoch_ns = time.time_ns() + (activation_monotonic_ns - observed_monotonic_ns)
    if abs(int(activation_epoch_ns) - expected_epoch_ns) > 2_000_000_000:
        raise ActivationArtifactError("core activation wall/monotonic clocks disagree")
    ack_payload = {
        "schema_version": CORE_ACK_SCHEMA_VERSION,
        "campaign_uuid": contract["campaign_uuid"],
        "campaign_commit": contract["campaign_commit"],
        "runner_profile_digest": contract["runner_profile_digest"],
        "activation_nonce": contract["nonce"],
        "campaign_runner_pid": contract["campaign_runner_pid"],
        "campaign_runner_start_ticks": contract["campaign_runner_start_ticks"],
        "child_pid": os.getpid(),
        "child_start_ticks": child_start_ticks,
        "ready_sha256": ready_sha256,
        "activation_sha256": activation_sha256,
        "activation_monotonic_ns": activation_monotonic_ns,
        "ack_sequence": 1,
        "acknowledged_monotonic_ns": time.monotonic_ns(),
        "acknowledged_at": utc_now(),
    }
    ack_sha256 = secure_write_once_json(
        contract["ack_file"],
        ack_payload,
        authority_root=contract["authority_root"],
    )
    while time.monotonic_ns() < activation_monotonic_ns:
        if stop_file is not None and artifact_exists(stop_file):
            raise CoreActivationStopped("core activation stopped after acknowledgement")
        if process_start_ticks(int(contract["campaign_runner_pid"])) != int(
            contract["campaign_runner_start_ticks"]
        ):
            raise ActivationArtifactError(
                "campaign runner identity vanished before synchronized activation"
            )
        remaining = (activation_monotonic_ns - time.monotonic_ns()) / 1e9
        time.sleep(max(0.001, min(0.05, remaining)))
    return {
        "required": True,
        "ok": True,
        "campaign_uuid": contract["campaign_uuid"],
        "campaign_commit": contract["campaign_commit"],
        "runner_profile_digest": contract["runner_profile_digest"],
        "child_pid": os.getpid(),
        "child_start_ticks": child_start_ticks,
        "ready_sha256": ready_sha256,
        "activation_sha256": activation_sha256,
        "ack_sha256": ack_sha256,
        "activation_monotonic_ns": activation_monotonic_ns,
        "activation_epoch_ns": activation_epoch_ns,
        "activated_at": datetime.fromtimestamp(
            activation_epoch_ns / 1_000_000_000,
            tz=timezone.utc,
        ).replace(microsecond=0).isoformat(),
    }


def harness_hashes() -> dict[str, str]:
    result = {}
    for path in HARNESS_FILES:
        resolved = Path(path).resolve()
        result[str(resolved.relative_to(ROOT))] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return result


def harness_drift(expected: dict[str, str]) -> dict[str, dict[str, str]]:
    current = harness_hashes()
    return {
        name: {"expected": digest, "actual": current.get(name, "missing")}
        for name, digest in expected.items()
        if current.get(name) != digest
    }


def _process_thread_count(pid: int) -> int:
    try:
        return sum(1 for path in Path(f"/proc/{int(pid)}/task").iterdir() if path.name.isdigit())
    except Exception:
        return 0


def measured_active_workers(
    run: dict[str, Any],
    scheduled_load_level: int,
    payload: dict[str, Any] | None = None,
) -> int:
    """Return only native sustained in-flight workers; `/proc` is corroboration."""

    telemetry = (payload or {}).get("worker_telemetry")
    if not isinstance(telemetry, dict):
        return 0
    if (
        telemetry.get("schema_version")
        != "hackme.system-stress-worker-telemetry.v1"
        or telemetry.get("method") != "native_inflight_operation_counter_time_samples"
        or int(telemetry.get("configured_workers") or 0) != int(scheduled_load_level)
        or int(telemetry.get("sample_count") or 0) <= 0
        or telemetry.get("complete") is not True
        or int(
            telemetry.get("active_workers_at_stop")
            if telemetry.get("active_workers_at_stop") is not None
            else -1
        ) != 0
    ):
        return 0
    sustained = int(telemetry.get("sustained_active_workers") or 0)
    return min(int(scheduled_load_level), max(0, sustained))


def normalized_32_throughput(
    *,
    operations_completed: int,
    window_seconds: float,
    scheduled_load_level: int,
) -> float:
    if window_seconds <= 0 or scheduled_load_level <= 0:
        return 0.0
    observed_per_minute = float(operations_completed) / float(window_seconds) * 60.0
    return observed_per_minute * 32.0 / float(scheduled_load_level)


def normalized_degradation_reason(reasons: list[str]) -> str:
    joined = " ".join(str(value).lower() for value in reasons)
    if any(marker in joined for marker in ("p95", "p99", "latency", "server_busy", "qos")):
        return "LATENCY_HIGH"
    if "memory" in joined or "oom" in joined:
        return "MEMORY_PRESSURE"
    if "database" in joined or "db_lock" in joined or "sqlite" in joined:
        return "DB_LOCK_PRESSURE"
    if "disk" in joined:
        return "DISK_LOW" if "free" in joined or "space" in joined else "IO_PRESSURE"
    if "gpu" in joined or "vram" in joined:
        return "GPU_PRESSURE"
    return ""


def build_effective_load_sample(
    *,
    payload: dict[str, Any],
    run: dict[str, Any],
    scheduled_load_level: int,
    expected_operations: int,
    baseline_32_operations_per_minute: float,
    window_started_at: str,
) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    completed = int(summary.get("total_ops") or 0)
    attempts = max(completed, int(payload.get("total_ops_requested") or expected_operations))
    active_workers = measured_active_workers(run, scheduled_load_level, payload)
    operational_reasons = [str(value) for value in payload.get("degraded_reasons") or []]
    operation_window_seconds = float(payload.get("elapsed_seconds") or 0.0)
    if operation_window_seconds <= 0:
        operation_window_seconds = max(0.001, float(run.get("elapsed_seconds") or 0.0))
    evidence = EffectiveLoadWindow(
        window_started_at=window_started_at,
        window_seconds=operation_window_seconds,
        scheduled_load_level=int(scheduled_load_level),
        active_workers=active_workers,
        inflight_requests=active_workers,
        operations_completed=completed,
        expected_operations=float(max(1, int(expected_operations))),
        blocked_workers=min(active_workers, int(summary.get("server_busy_503") or 0)),
        idle_workers=max(0, int(scheduled_load_level) - active_workers),
        queue_depth=max(0, int(expected_operations) - completed),
        retries=0,
        attempts=attempts,
        baseline_32_operations_per_minute=float(baseline_32_operations_per_minute),
        degradation_reason=normalized_degradation_reason(operational_reasons),
    ).evidence()
    evidence["worker_measurement"] = {
        "method": "native_inflight_operation_counter_time_samples",
        "native": payload.get("worker_telemetry") or {},
        "process_thread_count_peak": int(run.get("process_thread_count_peak") or 0),
        "process_thread_sample_count": int(run.get("process_thread_sample_count") or 0),
        "proc_task_active_worker_upper_bound": min(
            int(scheduled_load_level),
            max(
                0,
                int(run.get("process_thread_count_peak") or 0)
                - SYSTEM_STRESS_NON_WORKER_THREADS,
            ),
        ),
        "measured_active_workers": active_workers,
        "configured_concurrency_not_used_as_measurement": True,
    }
    evidence["operational_degradation_reasons"] = operational_reasons
    evidence["round_terminal_status"] = run.get("terminal_status")
    evidence["window_measurement"] = {
        "method": "system_stress_native_operation_elapsed",
        "operation_elapsed_seconds": operation_window_seconds,
        "process_wall_elapsed_seconds": float(run.get("elapsed_seconds") or 0.0),
    }
    evidence["round_ok"] = payload.get("ok") is True and int(run.get("returncode") or 0) == 0
    if not evidence["round_ok"]:
        evidence["at_target_load"] = False
        failures = list(evidence.get("target_failure_reasons") or [])
        failures.append("ROUND_NOT_OK")
        evidence["target_failure_reasons"] = sorted(set(failures))
    return evidence


class ApiClient:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = float(timeout)
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""
        # A 401 recovery calls login() while request() still owns this lock.
        self.lock = threading.RLock()

    def refresh_csrf(self) -> bool:
        response = self.session.get(f"{self.base_url}/api/csrf-token", timeout=self.timeout)
        if response.status_code != 200:
            return False
        try:
            self.csrf = str(response.json().get("csrf_token") or self.session.cookies.get("csrf_token") or "")
        except Exception:
            self.csrf = str(self.session.cookies.get("csrf_token") or "")
        return bool(self.csrf)

    def login(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self.refresh_csrf()
            response = self.session.post(
                f"{self.base_url}/api/login",
                json={"username": self.username, "password": self.password},
                headers={"X-CSRF-Token": self.csrf},
                timeout=self.timeout,
            )
            self.refresh_csrf()
            return self.capture(response, started)
        except Exception as exc:
            return {
                "ok": False,
                "status": 0,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
            }

    def capture(self, response: requests.Response, started: float) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:500]}
        if not isinstance(payload, dict):
            payload = {"body": payload}
        retry_after_raw = response.headers.get("Retry-After") or payload.get("retry_after_seconds")
        try:
            retry_after_seconds = max(0.0, float(retry_after_raw or 0.0))
        except (TypeError, ValueError):
            retry_after_seconds = 0.0
        return {
            "ok": 200 <= response.status_code < 300 and payload.get("ok") is not False,
            "status": int(response.status_code),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "body": payload,
            "backpressure_rejected": response.headers.get("X-Hackme-Backpressure-Rejected") == "1",
            "retry_after_seconds": retry_after_seconds,
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        retry_csrf: bool = True,
    ) -> dict[str, Any]:
        method = method.upper()
        with self.lock:
            started = time.perf_counter()
            csrf_retried = False
            try:
                headers: dict[str, str] = {}
                if method not in {"GET", "HEAD", "OPTIONS"}:
                    if not self.csrf:
                        self.refresh_csrf()
                    headers["X-CSRF-Token"] = self.csrf
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
                rotated_csrf = self.session.cookies.get("csrf_token")
                if rotated_csrf:
                    self.csrf = str(rotated_csrf)
                if response.status_code == 401:
                    self.login()
                    if method not in {"GET", "HEAD", "OPTIONS"}:
                        headers["X-CSRF-Token"] = self.csrf
                    started = time.perf_counter()
                    response = self.session.request(
                        method,
                        f"{self.base_url}{path}",
                        json=json_body,
                        headers=headers,
                        timeout=self.timeout,
                    )
                    rotated_csrf = self.session.cookies.get("csrf_token")
                    if rotated_csrf:
                        self.csrf = str(rotated_csrf)
                if (
                    retry_csrf
                    and method not in {"GET", "HEAD", "OPTIONS"}
                    and response.status_code == 403
                ):
                    try:
                        rejection = response.json()
                    except Exception:
                        rejection = {}
                    if isinstance(rejection, dict) and rejection.get("error") == "csrf_invalid":
                        if self.refresh_csrf():
                            headers["X-CSRF-Token"] = self.csrf
                            started = time.perf_counter()
                            response = self.session.request(
                                method,
                                f"{self.base_url}{path}",
                                json=json_body,
                                headers=headers,
                                timeout=self.timeout,
                            )
                            csrf_retried = True
                            rotated_csrf = self.session.cookies.get("csrf_token")
                            if rotated_csrf:
                                self.csrf = str(rotated_csrf)
                captured = self.capture(response, started)
                if csrf_retried:
                    captured["csrf_retried"] = True
                    captured["initial_status"] = 403
                return captured
            except Exception as exc:
                return {
                    "ok": False,
                    "status": 0,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error": f"{exc.__class__.__name__}: {str(exc)[:400]}",
                    "body": {},
                }


def _setup_retry_delay(result: dict[str, Any], attempt: int) -> float | None:
    status = int(result.get("status") or 0)
    controlled = status == 429 or (
        status == 503 and bool(result.get("backpressure_rejected"))
    )
    if not controlled:
        return None
    advertised = float(result.get("retry_after_seconds") or 0.0)
    exponential = min(SETUP_RETRY_MAX_SECONDS, float(2 ** max(0, attempt - 1)))
    return min(SETUP_RETRY_MAX_SECONDS, max(1.0, advertised, exponential))


def login_with_setup_backoff(
    client: ApiClient,
    *,
    attempts: int = SETUP_RETRY_ATTEMPTS,
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "status": 0}
    total_wait = 0.0
    for attempt in range(1, max(1, int(attempts)) + 1):
        result = client.login()
        result["attempts"] = attempt
        result["setup_retry_wait_seconds"] = round(total_wait, 3)
        if result.get("ok") is True:
            return result
        delay = _setup_retry_delay(result, attempt)
        if delay is None or attempt >= attempts:
            return result
        time.sleep(delay)
        total_wait += delay
    return result


def setup_request_with_backoff(
    client: ApiClient,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    attempts: int = SETUP_RETRY_ATTEMPTS,
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "status": 0}
    total_wait = 0.0
    for attempt in range(1, max(1, int(attempts)) + 1):
        result = client.request(method, path, json_body=json_body)
        result["attempts"] = attempt
        result["setup_retry_wait_seconds"] = round(total_wait, 3)
        delay = _setup_retry_delay(result, attempt)
        if delay is None or attempt >= attempts:
            return result
        time.sleep(delay)
        total_wait += delay
    return result


class SentinelStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts: Counter = Counter()
        self.statuses: dict[str, Counter] = defaultdict(Counter)
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self.body_not_ready: Counter = Counter()
        self.server_busy: Counter = Counter()
        self.errors: list[dict[str, Any]] = []

    def record(self, role: str, path: str, result: dict[str, Any]) -> None:
        key = f"{role}:{path}"
        status = int(result.get("status") or 0)
        http_only = path == "/api/root/server-mode/requirements"
        body = result.get("body") if isinstance(result.get("body"), dict) else {}
        controlled_busy = (
            status == 503
            and bool(result.get("backpressure_rejected"))
            and str(body.get("error") or "").strip().lower() == "server_busy"
        )
        with self.lock:
            self.counts[key] += 1
            self.statuses[key][str(status)] += 1
            self.latencies[key].append(float(result.get("elapsed_ms") or 0.0))
            if status == 200 and not result.get("ok") and http_only:
                self.body_not_ready[key] += 1
            if controlled_busy:
                self.server_busy[key] += 1
            if (status != 200 and not controlled_busy) or (not result.get("ok") and not http_only and not controlled_busy):
                if len(self.errors) < 100:
                    self.errors.append({
                        "at": utc_now(),
                        "role": role,
                        "path": path,
                        "status": status,
                        "error": str(result.get("error") or (result.get("body") or {}).get("msg") or "")[:400],
                    })

    def summary(self) -> dict[str, Any]:
        with self.lock:
            checks = {}
            for key, count in sorted(self.counts.items()):
                values = list(self.latencies.get(key) or [])
                checks[key] = {
                    "count": int(count),
                    "statuses": dict(sorted(self.statuses.get(key, Counter()).items())),
                    "p95_ms": percentile(values, 0.95),
                    "max_ms": round(max(values), 3) if values else 0.0,
                    "body_not_ready": int(self.body_not_ready.get(key, 0)),
                    "server_busy": int(self.server_busy.get(key, 0)),
                }
            total_checks = int(sum(self.counts.values()))
            server_busy = int(sum(self.server_busy.values()))
            all_latencies = [value for values in self.latencies.values() for value in values]
            return {
                "checks": checks,
                "errors": list(self.errors),
                "total_checks": total_checks,
                "server_busy": server_busy,
                "server_busy_rate": round(server_busy / total_checks, 6) if total_checks else 0.0,
                "p95_ms": percentile(all_latencies, 0.95),
                "max_ms": round(max(all_latencies), 3) if all_latencies else 0.0,
            }


ROOT_SENTINELS = (
    "/api/admin/health/readiness",
    "/api/admin/security-center",
    "/api/root/server-mode/requirements",
    "/api/root/server-mode/logs/verify",
    "/api/ai-agent/readonly?scope=resources&limit=10",
)
MANAGER_SENTINELS = (
    "/api/admin/users?page_size=10",
    "/api/admin/reports?status=pending&page=1",
    "/api/community/boards",
    "/api/notifications/unread-count",
)


def sentinel_loop(
    stop: threading.Event,
    start: threading.Event,
    stats: SentinelStats,
    root: ApiClient,
    manager: ApiClient,
    interval_seconds: float,
) -> None:
    start.wait()
    while not stop.is_set():
        cycle_started = time.monotonic()
        for role, client, paths in (("root", root, ROOT_SENTINELS), ("manager", manager, MANAGER_SENTINELS)):
            for path in paths:
                if stop.is_set():
                    return
                stats.record(role, path, client.request("GET", path))
        elapsed = time.monotonic() - cycle_started
        stop.wait(max(0.2, float(interval_seconds) - elapsed))


def configure_soak_storage_quota(root: ApiClient, user_id: int) -> dict[str, Any]:
    result = setup_request_with_backoff(
        root,
        "PUT",
        f"/api/root/storage/users/{int(user_id)}/quota-override",
        json_body={
            "quota_mb": SOAK_STORAGE_QUOTA_MB,
            "max_file_size_mb": SOAK_STORAGE_MAX_FILE_SIZE_MB,
            "upload_rate_limit_per_day": SOAK_STORAGE_UPLOAD_RATE_LIMIT_PER_DAY,
            "can_upload": True,
            "enabled": True,
            "reason": "isolated operational soak high-load account",
        },
    )
    body = result.get("body") if isinstance(result.get("body"), dict) else {}
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    expected_quota = SOAK_STORAGE_QUOTA_MB * 1024 * 1024
    expected_max_file = SOAK_STORAGE_MAX_FILE_SIZE_MB * 1024 * 1024
    return {
        "ok": bool(
            int(result.get("status") or 0) == 200
            and body.get("ok") is True
            and int(user.get("total_bytes") or 0) >= expected_quota
            and int(user.get("max_file_size_bytes") or 0) >= expected_max_file
            and int(user.get("upload_rate_limit_per_day") or 0)
            >= SOAK_STORAGE_UPLOAD_RATE_LIMIT_PER_DAY
            and user.get("can_upload") is True
        ),
        "status": int(result.get("status") or 0),
        "user_id": int(user_id),
        "quota_bytes": int(user.get("total_bytes") or 0),
        "max_file_size_bytes": int(user.get("max_file_size_bytes") or 0),
        "upload_rate_limit_per_day": int(user.get("upload_rate_limit_per_day") or 0),
        "can_upload": user.get("can_upload") is True,
    }


def provision_accounts(root: ApiClient, *, prefix: str, count: int, password: str) -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    for index in range(1, max(1, int(count)) + 1):
        username = f"{prefix}{index:02d}"
        search = setup_request_with_backoff(
            root,
            "GET",
            f"/api/admin/users?q={username}&page_size=100",
        )
        users = (search.get("body") or {}).get("users") or []
        exact = next((item for item in users if str(item.get("username") or "") == username), None)
        if exact is None:
            created = setup_request_with_backoff(
                root,
                "POST",
                "/api/admin/users",
                json_body={
                    "username": username,
                    "password": password,
                    "password_confirm": password,
                    "nickname": f"Operational Sim {index:02d}",
                    "role": "user",
                    "status": "active",
                    "member_level": "normal",
                },
            )
            if int(created.get("status") or 0) not in {200, 201, 409}:
                raise RuntimeError(f"failed to provision {username}: {created}")
            search = setup_request_with_backoff(
                root,
                "GET",
                f"/api/admin/users?q={username}&page_size=100",
            )
            users = (search.get("body") or {}).get("users") or []
            exact = next((item for item in users if str(item.get("username") or "") == username), None)
        user_id = int((exact or {}).get("id") or 0)
        if user_id <= 0:
            raise RuntimeError(f"provisioned account lookup was inconclusive: {username}: {search}")
        quota = configure_soak_storage_quota(root, user_id)
        if not quota.get("ok"):
            raise RuntimeError(f"failed to configure soak storage quota for {username}: {quota}")
        probe = ApiClient(root.base_url, username, password)
        login = login_with_setup_backoff(probe)
        if not login.get("ok"):
            raise RuntimeError(f"provisioned account cannot login: {username}: {login}")
        accounts.append((username, password))
    return accounts


def sanitized_command(command: list[str]) -> list[str]:
    redacted = []
    hide_next = False
    for value in command:
        if hide_next:
            redacted.append("[redacted]")
            hide_next = False
            continue
        matched_flag = next((flag for flag in SENSITIVE_COMMAND_FLAGS if value.startswith(f"{flag}=")), "")
        if matched_flag:
            redacted.append(f"{matched_flag}=[redacted]")
            continue
        redacted.append(value)
        if value in SENSITIVE_COMMAND_FLAGS:
            hide_next = True
    return redacted


def safe_api_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep control-plane evidence without persisting response bodies or credentials."""
    return {
        "ok": bool(result.get("ok")),
        "status": int(result.get("status") or 0),
        "elapsed_ms": float(result.get("elapsed_ms") or 0.0),
        "error": str(result.get("error") or "")[:400],
    }


def stop_control_reason(stop_file: Path | None) -> str:
    """Return only a conservative reason token from an external stop document."""
    if stop_file is None or not Path(stop_file).exists():
        return ""
    try:
        path = Path(stop_file)
        if path.stat().st_size > 64 * 1024:
            return "unrecognized"
        payload = json.loads(path.read_text(encoding="utf-8"))
        reason = str(payload.get("reason") or "") if isinstance(payload, dict) else ""
        if not reason or len(reason) > 128:
            return "unrecognized"
        if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for character in reason):
            return "unrecognized"
        return reason
    except Exception:
        return "unrecognized"


def _proc_identity(pid: int) -> tuple[int, str] | None:
    """Return Linux process start ticks and state, avoiding PID-reuse mistakes."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2 :].split()
        return int(fields[19]), str(fields[0])
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
        return None


def _descendant_identities(root_pid: int) -> dict[int, int]:
    children: dict[int, list[int]] = defaultdict(list)
    identities: dict[int, int] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2 :].split()
            state = str(fields[0])
            ppid = int(fields[1])
            start_ticks = int(fields[19])
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue
        if state == "Z":
            continue
        identities[pid] = start_ticks
        children[ppid].append(pid)
    found: dict[int, int] = {}
    pending = list(children.get(int(root_pid), ()))
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        if pid in identities:
            found[pid] = identities[pid]
        pending.extend(children.get(pid, ()))
    return found


def _identity_alive(pid: int, start_ticks: int) -> bool:
    identity = _proc_identity(pid)
    return bool(identity is not None and identity[0] == int(start_ticks) and identity[1] != "Z")


def _signal_identity(pid: int, start_ticks: int, sig: signal.Signals) -> None:
    if not _identity_alive(pid, start_ticks):
        return
    try:
        os.kill(int(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def _signal_process_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(int(pgid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate_managed_process(
    process: subprocess.Popen,
    descendants: dict[int, int],
    *,
    force: bool,
) -> tuple[int, list[int]]:
    """Stop the session and any observed descendants, including escaped sessions."""
    descendants.update(_descendant_identities(process.pid))
    first_signal = signal.SIGKILL if force else signal.SIGTERM
    _signal_process_group(process.pid, first_signal)
    for pid, start_ticks in descendants.items():
        _signal_identity(pid, start_ticks, first_signal)

    grace_deadline = time.monotonic() + (0.0 if force else 2.0)
    while process.poll() is None and time.monotonic() < grace_deadline:
        time.sleep(0.05)
    if process.poll() is None:
        _signal_process_group(process.pid, signal.SIGKILL)
    for pid, start_ticks in descendants.items():
        _signal_identity(pid, start_ticks, signal.SIGKILL)
    try:
        returncode = int(process.wait(timeout=5))
    except subprocess.TimeoutExpired:
        _signal_process_group(process.pid, signal.SIGKILL)
        try:
            returncode = int(process.wait(timeout=2))
        except subprocess.TimeoutExpired:
            returncode = -signal.SIGKILL

    # A child can call setsid() and outlive the leader. Re-check observed
    # identities after the leader has been reaped and kill them once more.
    cleanup_deadline = time.monotonic() + 1.0
    alive = [pid for pid, ticks in descendants.items() if _identity_alive(pid, ticks)]
    while alive and time.monotonic() < cleanup_deadline:
        for pid in alive:
            _signal_identity(pid, descendants[pid], signal.SIGKILL)
        time.sleep(0.05)
        alive = [pid for pid, ticks in descendants.items() if _identity_alive(pid, ticks)]
    return returncode, alive


def request_command_stop(state: dict[str, Any] | None, reason: str) -> None:
    """Signal a managed process tree immediately; finish_command will reap it."""
    if state is None or state.get("forced_stop_reason"):
        return
    process: subprocess.Popen = state["process"]
    if process.poll() is not None:
        return
    state["forced_stop_reason"] = str(reason or "external_control")
    descendants: dict[int, int] = state.setdefault("observed_descendants", {})
    descendants.update(_descendant_identities(process.pid))
    _signal_process_group(process.pid, signal.SIGKILL)
    for pid, start_ticks in descendants.items():
        _signal_identity(pid, start_ticks, signal.SIGKILL)


def run_command(
    command: list[str],
    *,
    stdout_path: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    stop_at_monotonic: float | None = None,
    stop_at_reason: str = "campaign_deadline",
    stop_file: Path | None = None,
    on_stop: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    state = start_command(command, stdout_path=stdout_path, env=env)
    try:
        return finish_command(
            state,
            timeout=timeout,
            stop_at_monotonic=stop_at_monotonic,
            stop_at_reason=stop_at_reason,
            stop_file=stop_file,
            on_stop=on_stop,
        )
    except BaseException:
        request_command_stop(state, "finish_command_exception")
        try:
            _terminate_managed_process(
                state["process"],
                state.setdefault("observed_descendants", {}),
                force=True,
            )
        except Exception:
            pass
        try:
            state["handle"].close()
        except Exception:
            pass
        raise


def start_command(command: list[str], *, stdout_path: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT) + (os.pathsep + merged_env["PYTHONPATH"] if merged_env.get("PYTHONPATH") else "")
    if env:
        merged_env.update(env)
    handle = stdout_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=merged_env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except BaseException:
        handle.close()
        raise
    return {
        "process": process,
        "handle": handle,
        "stdout": str(stdout_path),
        "command": sanitized_command(command),
        "started_monotonic": time.monotonic(),
        "observed_descendants": {},
        "forced_stop_reason": "",
        "process_thread_count_peak": 0,
        "process_thread_samples": [],
    }


def finish_command(
    state: dict[str, Any],
    *,
    timeout: int,
    stop_at_monotonic: float | None = None,
    stop_at_reason: str = "campaign_deadline",
    stop_file: Path | None = None,
    cancel_reason: str = "",
    on_stop: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    process: subprocess.Popen = state["process"]
    timed_out = False
    forced_stop_reason = str(cancel_reason or state.get("forced_stop_reason") or "")
    stopped_by_control = bool(forced_stop_reason)
    stop_reason = forced_stop_reason
    descendants: dict[int, int] = state.setdefault("observed_descendants", {})
    callback_error = ""
    next_descendant_scan = 0.0
    next_thread_scan = 0.0
    timeout_deadline = time.monotonic() + max(1, int(timeout))
    while process.poll() is None:
        now = time.monotonic()
        if now >= next_descendant_scan:
            descendants.update(_descendant_identities(process.pid))
            next_descendant_scan = now + 1.0
        if now >= next_thread_scan:
            thread_count = _process_thread_count(process.pid)
            if thread_count > 0:
                samples: list[int] = state.setdefault("process_thread_samples", [])
                samples.append(thread_count)
                state["process_thread_count_peak"] = max(
                    int(state.get("process_thread_count_peak") or 0),
                    thread_count,
                )
            next_thread_scan = now + 0.05
        forced_stop_reason = str(cancel_reason or state.get("forced_stop_reason") or "")
        if forced_stop_reason:
            stopped_by_control = True
            stop_reason = forced_stop_reason
            break
        if stop_file is not None and Path(stop_file).exists():
            stopped_by_control = True
            stop_reason = "external_stop_file"
            break
        if stop_at_monotonic is not None and now >= float(stop_at_monotonic):
            stopped_by_control = True
            stop_reason = str(stop_at_reason or "campaign_deadline")
            break
        if now >= timeout_deadline:
            timed_out = True
            stop_reason = "command_timeout"
            break
        wakeups = [0.1, max(0.0, timeout_deadline - now)]
        if stop_at_monotonic is not None:
            wakeups.append(max(0.0, float(stop_at_monotonic) - now))
        time.sleep(max(0.001, min(wakeups)))
    if (stopped_by_control or timed_out) and on_stop is not None:
        try:
            on_stop(stop_reason)
        except Exception as exc:
            callback_error = exc.__class__.__name__
    orphan_pids: list[int] = []
    if process.poll() is None:
        returncode, orphan_pids = _terminate_managed_process(
            process,
            descendants,
            force=stopped_by_control,
        )
        if timed_out:
            with open(state["stdout"], "a", encoding="utf-8") as handle:
                handle.write(f"\n[TIMEOUT] exceeded {timeout}s; process group terminated\n")
            returncode = 124
    else:
        # The leader may exit while helpers are still alive. A managed command
        # is terminal only after its observed process tree is gone.
        returncode, orphan_pids = _terminate_managed_process(process, descendants, force=True)
    try:
        state["handle"].close()
    except Exception:
        pass
    partial = bool(stopped_by_control or timed_out)
    return {
        "returncode": returncode,
        "elapsed_seconds": round(time.monotonic() - float(state["started_monotonic"]), 3),
        "stdout": state["stdout"],
        "command": state["command"],
        "timed_out": timed_out,
        "stopped_by_control": stopped_by_control,
        "stop_reason": stop_reason,
        "partial": partial,
        "terminal_status": (
            "NOT_EVALUATED" if stopped_by_control else "TIMEOUT" if timed_out else "COMPLETED"
        ),
        "orphan_pids": orphan_pids,
        "stop_callback_error": callback_error,
        "process_thread_count_peak": int(state.get("process_thread_count_peak") or 0),
        "process_thread_sample_count": len(state.get("process_thread_samples") or []),
    }


def aggregate_rounds(round_payloads: list[dict[str, Any]], configured_accounts: list[str]) -> dict[str, Any]:
    total_ops = 0
    hard_failures = 0
    server_busy = 0
    observed_operations: set[str] = set()
    registered_operations: set[str] = set()
    account_ops: Counter = Counter()
    operation_successes: Counter = Counter()
    account_successes: dict[str, Counter] = defaultdict(Counter)
    round_failures = []
    for index, payload in enumerate(round_payloads, start=1):
        summary = payload.get("summary") or {}
        total_ops += int(summary.get("total_ops") or 0)
        hard_failures += int(summary.get("hard_failures_excluding_controlled_503", summary.get("hard_failures_excluding_503")) or 0)
        server_busy += int(summary.get("server_busy_503") or 0)
        observed_operations.update((summary.get("ops") or {}).keys())
        registered_operations.update(payload.get("registered_operations") or [])
        for operation, evidence in (summary.get("ops") or {}).items():
            operation_successes[str(operation)] += int((evidence or {}).get("successful_2xx") or 0)
        for account, count in (payload.get("account_operation_counts") or {}).items():
            account_ops[str(account)] += int(count or 0)
        for account, evidence in (summary.get("accounts") or {}).items():
            for operation, count in ((evidence or {}).get("successful_operations") or {}).items():
                account_successes[str(account)][str(operation)] += int(count or 0)
        if payload.get("ok") is False:
            round_failures.append({"round": index, "degraded_reasons": payload.get("degraded_reasons") or [], "error": payload.get("error") or ""})
    account_success_gaps = {
        account: sorted(
            operation
            for operation in ACCOUNT_SUCCESS_REQUIRED_OPERATIONS
            if int(account_successes.get(account, Counter()).get(operation, 0)) <= 0
        )
        for account in configured_accounts
    }
    account_success_gaps = {account: gaps for account, gaps in account_success_gaps.items() if gaps}
    return {
        "rounds": len(round_payloads),
        "round_failures": round_failures,
        "total_ops": total_ops,
        "hard_failures": hard_failures,
        "hard_failure_rate": round(hard_failures / total_ops, 6) if total_ops else 0.0,
        "server_busy": server_busy,
        "server_busy_rate": round(server_busy / total_ops, 6) if total_ops else 0.0,
        "registered_operations": sorted(registered_operations),
        "observed_operations": sorted(observed_operations),
        "missing_operations": sorted(registered_operations - observed_operations),
        "account_operation_counts": {account: int(account_ops.get(account, 0)) for account in configured_accounts},
        "accounts_without_operations": [account for account in configured_accounts if int(account_ops.get(account, 0)) <= 0],
        "successful_operation_counts": {
            operation: int(operation_successes.get(operation, 0))
            for operation in sorted(registered_operations)
        },
        "operations_without_success": sorted(
            operation
            for operation in SOAK_REQUIRED_SUCCESS_OPERATIONS
            if int(operation_successes.get(operation, 0)) <= 0
        ),
        "required_success_operations": sorted(SOAK_REQUIRED_SUCCESS_OPERATIONS),
        "deferred_success_operations": sorted(SOAK_DEFERRED_SUCCESS_OPERATIONS),
        "account_success_counts": {
            account: dict(sorted(account_successes.get(account, Counter()).items()))
            for account in configured_accounts
        },
        "account_success_gaps": account_success_gaps,
    }


def round_rotation_offset(round_index: int, round_ops: int) -> int:
    """Continue deterministic account coverage across subprocess rounds."""

    return max(0, int(round_index) - 1) * max(1, int(round_ops))


def aggregate_resource_evidence(round_payloads: list[dict[str, Any]], server_pids: str) -> dict[str, Any]:
    summaries = [
        payload.get("resource_monitor")
        for payload in round_payloads
        if isinstance(payload.get("resource_monitor"), dict)
    ]
    summaries = [summary for summary in summaries if summary]
    monitored_pids_seen = sorted({
        int(pid)
        for summary in summaries
        for pid in (summary.get("monitored_pids_seen") or [])
    })
    first_sample = next((summary.get("first_sample") for summary in summaries if summary.get("first_sample")), {})
    last_sample = next((summary.get("last_sample") for summary in reversed(summaries) if summary.get("last_sample")), {})
    db_peak: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        for label, evidence in (summary.get("db_peak") or {}).items():
            target = db_peak.setdefault(str(label), {})
            for key in ("max_db_mb", "max_wal_mb", "max_shm_mb", "max_page_count", "max_freelist_count"):
                target[key] = max(float(target.get(key) or 0), float((evidence or {}).get(key) or 0))
            if (evidence or {}).get("last"):
                target["last"] = evidence["last"]
    return {
        "server_pids": [part for part in str(server_pids or "").replace(",", " ").split() if part],
        "rounds_with_resource_evidence": len(summaries),
        "sample_count": sum(int(summary.get("sample_count") or 0) for summary in summaries),
        "monitored_rss_first_mb": float((first_sample or {}).get("monitored_rss_mb") or 0),
        "monitored_rss_last_mb": float((last_sample or {}).get("monitored_rss_mb") or 0),
        "monitored_rss_max_mb": max((float(summary.get("monitored_rss_max_mb") or 0) for summary in summaries), default=0.0),
        "monitored_pid_count_max": max((int(summary.get("monitored_pid_count_max") or 0) for summary in summaries), default=0),
        "monitored_pids_seen": monitored_pids_seen,
        "mem_available_min_mb": min((float(summary.get("mem_available_min_mb")) for summary in summaries if summary.get("mem_available_min_mb") is not None), default=None),
        "runtime_disk_free_min_mb": min((float(summary.get("runtime_disk_free_min_mb")) for summary in summaries if summary.get("runtime_disk_free_min_mb") is not None), default=None),
        "db_peak": db_peak,
        "first_sample": first_sample or {},
        "last_sample": last_sample or {},
    }


def final_control_request(client: ApiClient, path: str, *, attempts: int = 3) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "status": 0, "body": {}}
    for attempt in range(1, max(1, int(attempts)) + 1):
        result = client.request("GET", path)
        result["attempts"] = attempt
        body = result.get("body") if isinstance(result.get("body"), dict) else {}
        controlled_busy = (
            int(result.get("status") or 0) == 503
            and bool(result.get("backpressure_rejected"))
            and body.get("error") == "server_busy"
        )
        if not controlled_busy or attempt >= attempts:
            return result
        retry_after = float(body.get("retry_after_seconds") or 0.25)
        time.sleep(max(0.25, min(3.0, retry_after)))
    return result


def latest_playwright_report(runtime_root: Path) -> Path | None:
    reports = sorted(runtime_root.glob("reports/qa/playwright_deep_site_check_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a configured-duration multi-account synchronous operational simulation.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--server-runtime-root", default="", help="Actual isolated server runtime containing database/, logs/, and secrets/")
    parser.add_argument("--out", default="")
    parser.add_argument("--duration-seconds", type=int, default=MIN_SIGNOFF_SECONDS)
    parser.add_argument(
        "--campaign-level",
        choices=("standalone", "smoke", "rehearsal", "soak", "formal"),
        default="standalone",
    )
    parser.add_argument("--allow-short-duration", action="store_true", help="Development smoke only; short runs are never production sign-off evidence")
    parser.add_argument("--account-count", type=int, default=8)
    parser.add_argument("--account-prefix", default="opsim")
    parser.add_argument("--account-password", default=os.environ.get("HACKME_SOAK_ACCOUNT_PASSWORD", ""))
    parser.add_argument("--root-username", default="root")
    parser.add_argument("--root-password", default=os.environ.get("HACKME_SOAK_ROOT_PASSWORD", ""))
    parser.add_argument("--manager-username", default="admin")
    parser.add_argument("--manager-password", default=os.environ.get("HACKME_SOAK_MANAGER_PASSWORD", ""))
    parser.add_argument("--test-password", default=os.environ.get("HACKME_SOAK_TEST_PASSWORD", os.environ.get("PLAYWRIGHT_TEST_PASSWORD", "")))
    parser.add_argument("--server-pids", default=os.environ.get("HACKME_SERVER_PIDS", ""), help="Comma/space separated server master and worker PIDs for RSS evidence")
    parser.add_argument("--round-ops", type=int, default=800)
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--session-pool", type=int, default=16)
    parser.add_argument("--round-timeout-seconds", type=int, default=1800)
    parser.add_argument("--sentinel-interval-seconds", type=float, default=10.0)
    parser.add_argument("--max-server-busy-rate", type=float, default=0.05)
    parser.add_argument("--max-ordinary-p95-ms", type=float, default=3000.0)
    parser.add_argument("--max-ordinary-p99-ms", type=float, default=8000.0)
    parser.add_argument("--max-sentinel-p95-ms", type=float, default=3000.0)
    parser.add_argument("--browser-interval-seconds", type=int, default=4 * 60 * 60)
    parser.add_argument("--stop-file", default="", help="External campaign stop request below runtime-root")
    parser.add_argument("--campaign-uuid", default="", help=argparse.SUPPRESS)
    parser.add_argument("--campaign-commit", default="", help=argparse.SUPPRESS)
    parser.add_argument("--runner-profile-digest", default="", help=argparse.SUPPRESS)
    parser.add_argument("--campaign-runner-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--campaign-runner-start-ticks", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--activation-ready-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--activation-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--activation-ack-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--activation-timeout-seconds", type=float, default=600.0, help=argparse.SUPPRESS)
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--skip-points-stress", action="store_true")
    parser.add_argument("--i-own-this-target", action="store_true", help="Required for destructive testing against a non-loopback target")
    return parser.parse_args()


def main() -> int:
    process_started = time.monotonic()
    args = parse_args()
    if _IMPORT_ERROR is not None:
        missing = getattr(_IMPORT_ERROR, "name", None) or str(_IMPORT_ERROR)
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "DEPENDENCY_MISSING",
                    "missing_module": missing,
                    "remediation": "python3 -m pip install -r requirements.txt",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    requests.packages.urllib3.disable_warnings()
    if args.duration_seconds < MIN_SIGNOFF_SECONDS and not args.allow_short_duration:
        raise SystemExit(f"production operational soak requires at least {MIN_SIGNOFF_SECONDS} seconds; use --allow-short-duration only for development smoke")
    if args.account_count < 2:
        raise SystemExit("operational soak requires at least two member accounts")
    missing_secrets = [
        name
        for name, value in (
            ("HACKME_SOAK_ROOT_PASSWORD/--root-password", args.root_password),
            ("HACKME_SOAK_MANAGER_PASSWORD/--manager-password", args.manager_password),
            ("HACKME_SOAK_ACCOUNT_PASSWORD/--account-password", args.account_password),
            ("HACKME_SOAK_TEST_PASSWORD/--test-password", args.test_password),
        )
        if not str(value or "")
    ]
    if missing_secrets:
        raise SystemExit("missing required credentials: " + ", ".join(missing_secrets))

    runtime_root = Path(args.runtime_root).resolve()
    try:
        validate_run_policy(args.base_url, runtime_root, owns_target=bool(args.i_own_this_target))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not args.server_runtime_root and not args.skip_points_stress:
        raise SystemExit("--server-runtime-root is required when PointsChain stress is enabled")
    server_runtime_root = Path(args.server_runtime_root or args.runtime_root).resolve()
    try:
        validate_run_policy(args.base_url, server_runtime_root, owns_target=bool(args.i_own_this_target))
    except ValueError as exc:
        raise SystemExit(f"invalid --server-runtime-root: {exc}") from exc
    report_dir = runtime_root / "reports" / "operational_soak"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out).resolve() if args.out else report_dir / f"operational_soak_{int(time.time())}.json"
    if out_path != runtime_root and runtime_root not in out_path.parents:
        raise SystemExit("--out must remain under the selected /tmp runtime root")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stop_file = Path(args.stop_file).resolve(strict=False) if args.stop_file else None
    if stop_file is not None and stop_file != runtime_root and runtime_root not in stop_file.parents:
        raise SystemExit("--stop-file must remain under the selected /tmp runtime root")
    checkpoint_path = report_dir / "operational_soak.checkpoint.json"
    source_harness_hashes = harness_hashes()
    try:
        core_activation_contract = activation_contract(args, runtime_root)
    except ActivationArtifactError as exc:
        payload = {
            "schema_version": OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
            "ok": False,
            "verdict": "FAIL",
            "terminal_status": "FAIL_HARNESS",
            "classification": "FAIL_HARNESS",
            "termination_reason": "core_activation_contract_rejected",
            "production_signoff_eligible": False,
            "error": str(exc),
            "finished_at": utc_now(),
        }
        atomic_write_json(out_path, payload)
        atomic_write_json(checkpoint_path, {**payload, "status": "terminal", "report": str(out_path)})
        return 2

    if stop_file is not None and stop_file.exists():
        control_reason = stop_control_reason(stop_file)
        payload = {
            "schema_version": OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
            "ok": False,
            "verdict": "INTERRUPTED",
            "terminal_status": "INTERRUPTED",
            "termination_reason": "external_stop_file_preexisting",
            "external_control_reason": control_reason,
            "production_signoff_eligible": False,
            "base_url": args.base_url,
            "runtime_root": str(runtime_root),
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "requested_duration_seconds": int(args.duration_seconds),
            "actual_duration_seconds": 0.0,
            "accounts": [],
            "round_runs": [],
            "browser_runs": [],
            "points_stress": {"run": {"skipped": True}, "result": {"skipped": True}},
            "findings": [],
        }
        atomic_write_json(out_path, payload)
        atomic_write_json(checkpoint_path, {**payload, "status": "terminal", "report": str(out_path)})
        print(json.dumps({"ok": False, "verdict": "INTERRUPTED", "report": str(out_path)}, ensure_ascii=False))
        return 3

    root = ApiClient(args.base_url, args.root_username, args.root_password)
    manager = ApiClient(args.base_url, args.manager_username, args.manager_password)
    root_login = login_with_setup_backoff(root)
    manager_login = login_with_setup_backoff(manager)
    if not root_login.get("ok") or not manager_login.get("ok"):
        payload = {
            "schema_version": OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
            "ok": False,
            "verdict": "FAIL",
            "terminal_status": "FAIL_INFRA",
            "production_signoff_eligible": False,
            "error": "privileged login failed",
            "root_login": safe_api_result(root_login),
            "manager_login": safe_api_result(manager_login),
            "finished_at": utc_now(),
        }
        atomic_write_json(out_path, payload)
        atomic_write_json(checkpoint_path, {**payload, "status": "terminal", "report": str(out_path)})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    try:
        accounts = provision_accounts(
            root,
            prefix=str(args.account_prefix),
            count=max(2, int(args.account_count)),
            password=str(args.account_password),
        )
    except Exception as exc:
        payload = {
            "schema_version": OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
            "ok": False,
            "verdict": "FAIL",
            "terminal_status": "FAIL_INFRA",
            "production_signoff_eligible": False,
            "error": "account provisioning failed",
            "error_type": exc.__class__.__name__,
            "finished_at": utc_now(),
        }
        atomic_write_json(out_path, payload)
        atomic_write_json(checkpoint_path, {**payload, "status": "terminal", "report": str(out_path)})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    account_names = [username for username, _password in accounts]
    account_spec = ",".join(f"{username}:{password}" for username, password in accounts)

    stop_sentinel = threading.Event()
    start_sentinel = threading.Event()
    sentinel_stats = SentinelStats()
    sentinel_thread = threading.Thread(
        target=sentinel_loop,
        args=(stop_sentinel, start_sentinel, sentinel_stats, root, manager, args.sentinel_interval_seconds),
        daemon=True,
    )
    sentinel_thread.start()

    try:
        activation_evidence = publish_ready_and_wait_for_activation(
            core_activation_contract,
            duration_seconds=int(args.duration_seconds),
            stop_file=stop_file,
        )
    except (ActivationArtifactError, CoreActivationStopped) as exc:
        stop_sentinel.set()
        start_sentinel.set()
        stopped = isinstance(exc, CoreActivationStopped)
        payload = {
            "schema_version": OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
            "ok": False,
            "verdict": "INTERRUPTED" if stopped else "FAIL",
            "terminal_status": "INTERRUPTED" if stopped else "FAIL_HARNESS",
            "classification": "INTERRUPTED" if stopped else "FAIL_HARNESS",
            "termination_reason": (
                "external_stop_during_activation_wait"
                if stopped
                else "core_activation_rejected"
            ),
            "production_signoff_eligible": False,
            "error": str(exc),
            "campaign_uuid": core_activation_contract.get("campaign_uuid"),
            "accounts": account_names,
            "finished_at": utc_now(),
            "requested_duration_seconds": int(args.duration_seconds),
            "actual_duration_seconds": 0.0,
            "activation": {"required": bool(core_activation_contract.get("required"))},
        }
        atomic_write_json(out_path, payload)
        atomic_write_json(checkpoint_path, {**payload, "status": "terminal", "report": str(out_path)})
        print(json.dumps({"ok": False, "verdict": payload["verdict"], "report": str(out_path)}, ensure_ascii=False))
        return 3 if stopped else 2

    started_at = str(activation_evidence.get("activated_at") or utc_now())
    started = (
        int(activation_evidence["activation_monotonic_ns"]) / 1_000_000_000
        if activation_evidence.get("required")
        else time.monotonic()
    )
    deadline = started + max(1, int(args.duration_seconds))

    def external_stop_requested() -> bool:
        return bool(stop_file is not None and stop_file.exists())
    start_sentinel.set()
    round_payloads: list[dict[str, Any]] = []
    round_runs: list[dict[str, Any]] = []
    browser_runs: list[dict[str, Any]] = []
    browser_state: dict[str, Any] | None = None
    next_browser_at = started
    points_state: dict[str, Any] | None = None
    points_run: dict[str, Any] | None = None
    points_payload: dict[str, Any] | None = None
    points_report = report_dir / "points_stress.json"
    detected_harness_drift: dict[str, dict[str, str]] = {}
    termination_reason = ""
    external_control_reason = ""
    loop_error: BaseException | None = None
    effective_load_required = args.campaign_level in {"rehearsal", "formal"}
    load_policy = campaign_load_policy(args.campaign_level)
    ramp_minimum_stage_seconds = dict(
        RAMP_MINIMUM_STAGE_SECONDS.get(
            args.campaign_level,
            {level: 0.0 for level in FORMAL_RAMP_LEVELS},
        )
    )
    ramp_schedule: list[dict[str, float | int]] = []
    ramp_schedule_cursor = 0.0
    for level in FORMAL_RAMP_LEVELS[:-1]:
        stage_seconds = float(ramp_minimum_stage_seconds.get(level, 0.0))
        ramp_schedule.append({
            "level": level,
            "start_seconds": ramp_schedule_cursor,
            "end_seconds": ramp_schedule_cursor + stage_seconds,
        })
        ramp_schedule_cursor += stage_seconds
    ramp_completion_deadline_seconds = float(
        load_policy["ramp_completion_deadline_seconds"]
    )
    maximum_stage_boundary_lag_seconds = float(
        load_policy["maximum_stage_boundary_lag_seconds"]
    )
    minimum_post_ramp_seconds = float(load_policy["minimum_post_ramp_seconds"])
    ramp_index = 0
    ramp_completed_levels: list[int] = []
    ramp_stages: dict[str, dict[str, Any]] = {
        str(level): {
            "scheduled_load_level": level,
            "minimum_stage_seconds": (
                float(ramp_minimum_stage_seconds.get(level, 0.0))
            ),
            "observed_seconds": 0.0,
            "rounds": 0,
            "valid_terminal_rounds": 0,
            "measured_active_workers_peak": 0,
            "operations_completed": 0,
            "normalized_32_throughput_samples": [],
            "round_evidence": [],
            "completed": False,
            "scheduled_start_seconds": (
                next(
                    float(row["start_seconds"])
                    for row in ramp_schedule
                    if int(row["level"]) == level
                )
                if level < 32
                else ramp_completion_deadline_seconds
            ),
            "scheduled_end_seconds": (
                next(
                    float(row["end_seconds"])
                    for row in ramp_schedule
                    if int(row["level"]) == level
                )
                if level < 32
                else float(args.duration_seconds)
            ),
            "completed_elapsed_seconds": None,
        }
        for level in FORMAL_RAMP_LEVELS
    }
    baseline_candidates: list[float] = []
    baseline_32_operations_per_minute = 0.0
    target_load_samples: list[dict[str, Any]] = []
    target_stage_started_monotonic: float | None = (
        started + ramp_completion_deadline_seconds if effective_load_required else None
    )
    ramp_completion_elapsed_seconds: float | None = None
    ramp_schedule_failure = ""
    load_termination_monotonic: float | None = None

    def effective_load_evidence() -> dict[str, Any]:
        target_summary = summarize_target_load(
            target_load_samples,
            minimum_coverage=MINIMUM_TARGET_LOAD_COVERAGE,
        )
        sample_only_coverage = float(target_summary.get("target_load_coverage") or 0.0)
        target_seconds = float(target_summary.get("target_load_seconds") or 0.0)
        maintenance_seconds = float(
            target_summary.get("maintenance_seconds_excluded") or 0.0
        )
        target_end = min(
            deadline,
            float(load_termination_monotonic or time.monotonic()),
        )
        post_ramp_wall_seconds = (
            max(0.0, target_end - target_stage_started_monotonic)
            if target_stage_started_monotonic is not None
            else 0.0
        )
        eligible_post_ramp_wall_seconds = max(
            0.0,
            post_ramp_wall_seconds - maintenance_seconds,
        )
        wall_coverage = (
            min(1.0, target_seconds / eligible_post_ramp_wall_seconds)
            if eligible_post_ramp_wall_seconds > 0
            else 0.0
        )
        target_summary["sample_only_target_load_coverage"] = round(sample_only_coverage, 6)
        target_summary["post_ramp_wall_seconds"] = round(post_ramp_wall_seconds, 6)
        target_summary["eligible_post_ramp_wall_seconds"] = round(
            eligible_post_ramp_wall_seconds,
            6,
        )
        target_summary["unmeasured_or_non_target_seconds"] = round(
            max(0.0, eligible_post_ramp_wall_seconds - target_seconds),
            6,
        )
        target_summary["target_load_coverage"] = round(wall_coverage, 6)
        target_summary["ok"] = bool(
            target_summary.get("invalid_samples") == []
            and eligible_post_ramp_wall_seconds > 0
            and wall_coverage >= MINIMUM_TARGET_LOAD_COVERAGE
        )
        ramp_ok = bool(
            not effective_load_required
            or (
                ramp_completed_levels == list(FORMAL_RAMP_LEVELS)
                and not ramp_schedule_failure
                and ramp_completion_elapsed_seconds is not None
                and ramp_completion_elapsed_seconds
                <= ramp_completion_deadline_seconds + maximum_stage_boundary_lag_seconds
                and eligible_post_ramp_wall_seconds + 1.0 >= minimum_post_ramp_seconds
            )
        )
        ok = bool(
            not effective_load_required
            or (
                ramp_ok
                and baseline_32_operations_per_minute > 0
                and target_summary.get("ok") is True
            )
        )
        return {
            "schema_version": EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION,
            "required": effective_load_required,
            "campaign_level": args.campaign_level,
            "ramp": {
                "required_levels": list(FORMAL_RAMP_LEVELS),
                "completed_levels": list(ramp_completed_levels),
                "minimum_stage_seconds": {
                    str(level): float(ramp_minimum_stage_seconds.get(level, 0.0))
                    for level in FORMAL_RAMP_LEVELS
                },
                "schedule": ramp_schedule,
                "completion_elapsed_seconds": (
                    round(ramp_completion_elapsed_seconds, 6)
                    if ramp_completion_elapsed_seconds is not None
                    else None
                ),
                "completion_deadline_seconds": ramp_completion_deadline_seconds,
                "maximum_stage_boundary_lag_seconds": maximum_stage_boundary_lag_seconds,
                "schedule_failure": ramp_schedule_failure,
                "stages": ramp_stages,
                "ok": ramp_ok,
            },
            "minimum_post_ramp_seconds": minimum_post_ramp_seconds,
            "baseline_method": "median_ramp_throughput_normalized_to_concurrency_32",
            "baseline_32_operations_per_minute": round(
                baseline_32_operations_per_minute,
                6,
            ),
            "target_load_samples": target_load_samples,
            "target_load_summary": target_summary,
            "ok": ok,
        }

    def record_effective_load_round(
        *,
        payload: dict[str, Any],
        run: dict[str, Any],
        scheduled_load_level: int,
        window_started_at: str,
        window_started_monotonic: float,
    ) -> None:
        nonlocal baseline_32_operations_per_minute
        if not effective_load_required:
            return
        level = int(scheduled_load_level)
        stage = ramp_stages[str(level)]
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        completed = int(summary.get("total_ops") or 0)
        measured_workers = measured_active_workers(run, level, payload)
        seconds = max(0.0, float(payload.get("elapsed_seconds") or 0.0))
        expected_operations = max(int(args.round_ops), int(args.account_count))
        worker_floor = int(math.ceil(level * 0.85))
        window_started_elapsed = max(0.0, window_started_monotonic - started)
        window_finished_elapsed = max(0.0, time.monotonic() - started)
        within_fixed_stage_window = bool(
            level == 32
            or (
                window_started_elapsed >= float(stage["scheduled_start_seconds"])
                and window_finished_elapsed <= float(stage["scheduled_end_seconds"])
            )
        )
        terminal_valid = bool(
            not run.get("partial")
            and int(run.get("returncode") or 0) == 0
            and payload.get("ok") is True
            and completed >= int(math.ceil(expected_operations * 0.85))
            and measured_workers >= worker_floor
            and within_fixed_stage_window
        )
        normalized = normalized_32_throughput(
            operations_completed=completed,
            window_seconds=seconds,
            scheduled_load_level=level,
        )
        stage["round_evidence"].append({
            "scheduled_load_level": level,
            "window_seconds": round(seconds, 6),
            "window_started_elapsed_seconds": round(window_started_elapsed, 6),
            "window_finished_elapsed_seconds": round(window_finished_elapsed, 6),
            "expected_operations": expected_operations,
            "operations_completed": completed,
            "returncode": int(run.get("returncode") or 0),
            "partial": bool(run.get("partial")),
            "round_ok": payload.get("ok") is True,
            "terminal_valid": terminal_valid,
            "measured_active_workers": measured_workers,
            "worker_telemetry": payload.get("worker_telemetry") or {},
            "process_thread_count_peak": int(run.get("process_thread_count_peak") or 0),
            "process_thread_sample_count": int(run.get("process_thread_sample_count") or 0),
            "normalized_32_operations_per_minute": round(normalized, 6),
            "degradation_reasons": [
                str(value) for value in payload.get("degraded_reasons") or []
            ],
        })
        stage["observed_seconds"] = round(
            float(stage.get("observed_seconds") or 0.0) + seconds,
            6,
        )
        stage["rounds"] = int(stage.get("rounds") or 0) + 1
        stage["measured_active_workers_peak"] = max(
            int(stage.get("measured_active_workers_peak") or 0),
            measured_workers,
        )
        stage["operations_completed"] = int(stage.get("operations_completed") or 0) + completed
        if terminal_valid:
            stage["valid_terminal_rounds"] = int(stage.get("valid_terminal_rounds") or 0) + 1
            if normalized > 0:
                samples = stage["normalized_32_throughput_samples"]
                samples.append(round(normalized, 6))
                if level < 32:
                    baseline_candidates.append(normalized)

        if level < 32:
            return

        sample = build_effective_load_sample(
            payload=payload,
            run=run,
            scheduled_load_level=level,
            expected_operations=expected_operations,
            baseline_32_operations_per_minute=baseline_32_operations_per_minute,
            window_started_at=window_started_at,
        )
        sample_window_seconds = float(sample.get("window_seconds") or 0.0)
        operation_window_started = max(
            float(ramp_completion_deadline_seconds),
            window_finished_elapsed - sample_window_seconds,
        )
        sample["window_started_elapsed_seconds"] = round(operation_window_started, 6)
        sample["window_finished_elapsed_seconds"] = round(
            operation_window_started + sample_window_seconds,
            6,
        )
        target_load_samples.append(sample)
        if sample.get("at_target_load") is True and not stage["completed"]:
            stage["completed"] = True
            stage["completed_elapsed_seconds"] = round(window_finished_elapsed, 6)
            ramp_completed_levels.append(level)

    def advance_due_ramp_stages(observed_monotonic: float) -> bool:
        """Advance only at immutable wall-clock boundaries, failing closed."""
        nonlocal ramp_index, baseline_32_operations_per_minute
        nonlocal ramp_completion_elapsed_seconds, ramp_schedule_failure
        if not effective_load_required:
            return True
        elapsed = max(0.0, observed_monotonic - started)
        while ramp_index < len(ramp_schedule):
            schedule_row = ramp_schedule[ramp_index]
            boundary = float(schedule_row["end_seconds"])
            if elapsed < boundary:
                break
            level = int(schedule_row["level"])
            stage = ramp_stages[str(level)]
            lag = elapsed - boundary
            worker_floor = int(math.ceil(level * 0.85))
            if lag > maximum_stage_boundary_lag_seconds:
                ramp_schedule_failure = f"ramp_stage_boundary_lag:{level}"
                return False
            if (
                int(stage.get("valid_terminal_rounds") or 0) <= 0
                or int(stage.get("measured_active_workers_peak") or 0) < worker_floor
                or not stage.get("normalized_32_throughput_samples")
            ):
                ramp_schedule_failure = f"ramp_stage_missing_native_terminal_evidence:{level}"
                return False
            stage["completed"] = True
            stage["completed_elapsed_seconds"] = round(elapsed, 6)
            ramp_completed_levels.append(level)
            ramp_index += 1
        if ramp_index == len(ramp_schedule) and ramp_completion_elapsed_seconds is None:
            if not baseline_candidates:
                ramp_schedule_failure = "ramp_baseline_missing"
                return False
            baseline_32_operations_per_minute = float(median(baseline_candidates))
            ramp_completion_elapsed_seconds = elapsed
        return True

    def stop_background_children(reason: str) -> None:
        request_command_stop(points_state, reason)
        request_command_stop(browser_state, reason)

    def finish_background_child(state: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        nonlocal loop_error, termination_reason
        try:
            return finish_command(
                state,
                timeout=timeout,
                stop_file=stop_file,
                cancel_reason=termination_reason,
            )
        except BaseException as exc:
            if loop_error is None:
                loop_error = exc
            termination_reason = "harness_exception"
            request_command_stop(state, termination_reason)
            orphan_pids: list[int] = []
            try:
                returncode, orphan_pids = _terminate_managed_process(
                    state["process"],
                    state.setdefault("observed_descendants", {}),
                    force=True,
                )
            except Exception:
                returncode = -signal.SIGKILL
            try:
                state["handle"].close()
            except Exception:
                pass
            return {
                "returncode": int(returncode),
                "elapsed_seconds": round(time.monotonic() - float(state["started_monotonic"]), 3),
                "stdout": state["stdout"],
                "command": state["command"],
                "timed_out": False,
                "stopped_by_control": True,
                "stop_reason": "harness_exception",
                "partial": True,
                "terminal_status": "FAIL_HARNESS",
                "orphan_pids": orphan_pids,
                "error_type": exc.__class__.__name__,
            }

    def write_checkpoint(phase: str) -> None:
        elapsed = time.monotonic() - started
        atomic_write_json(checkpoint_path, {
            "status": "running",
            "phase": phase,
            "production_signoff_eligible": False,
            "started_at": started_at,
            "updated_at": utc_now(),
            "elapsed_seconds": round(elapsed, 3),
            "remaining_seconds": round(max(0.0, deadline - time.monotonic()), 3),
            "accounts": account_names,
            "rounds_completed": len(round_payloads),
            "aggregate": aggregate_rounds(round_payloads, account_names),
            "effective_load": effective_load_evidence(),
            "sentinel": sentinel_stats.summary(),
            "points_stress": {
                "started": points_state is not None or points_run is not None,
                "completed": points_run is not None,
                "returncode": (points_run or {}).get("returncode"),
            },
            "browser_runs_completed": len(browser_runs),
            "browser_running": browser_state is not None,
            "source_harness_hashes": source_harness_hashes,
            "harness_drift": detected_harness_drift,
            "termination_reason": termination_reason,
            "activation": activation_evidence,
            "report": str(out_path),
        })

    write_checkpoint("initial_setup")

    round_index = 0
    try:
        if not args.skip_points_stress:
            points_command = [
                sys.executable,
                str(POINTS_STRESS),
                "--base-url", args.base_url,
                "--runtime-root", str(server_runtime_root),
                "--out", str(points_report),
                "--accounts", str(max(4, min(12, args.account_count))),
                "--transfer-ops", "36",
                "--direct-transfer-ops", "120",
                "--trading-ops", "24",
                "--concurrency", str(max(4, min(12, args.concurrency))),
            ]
            if args.server_pids:
                points_command.extend(["--server-pids", str(args.server_pids)])
            points_state = start_command(
                points_command,
                stdout_path=report_dir / "points_stress.stdout",
                env={"HACKME_POINTS_STRESS_ROOT_PASSWORD": args.root_password},
            )

        while time.monotonic() < deadline and not external_stop_requested():
            detected_harness_drift = harness_drift(source_harness_hashes)
            if detected_harness_drift:
                write_checkpoint("harness_drift_detected")
                break
            now = time.monotonic()
            if not advance_due_ramp_stages(now):
                termination_reason = ramp_schedule_failure
                write_checkpoint("ramp_schedule_failed")
                break
            if not args.skip_browser and browser_state is None and now >= next_browser_at:
                browser_root = report_dir / f"browser_{len(browser_runs) + 1:03d}"
                browser_state = start_command(
                    [
                        sys.executable,
                        str(PLAYWRIGHT_DEEP),
                        "--base-url", args.base_url,
                        "--runtime-root", str(browser_root),
                        "--max-chess-human-moves", "6",
                    ],
                    stdout_path=browser_root / "playwright.stdout",
                    env={
                        "PLAYWRIGHT_ROOT_PASSWORD": args.root_password,
                        "PLAYWRIGHT_MANAGER_PASSWORD": args.manager_password,
                        "PLAYWRIGHT_TEST_PASSWORD": args.test_password,
                    },
                )
                next_browser_at = now + max(300, int(args.browser_interval_seconds))

            round_index += 1
            scheduled_load_level = (
                FORMAL_RAMP_LEVELS[ramp_index]
                if effective_load_required
                else max(2, int(args.concurrency))
            )
            round_window_started_at = utc_now()
            round_window_started_monotonic = time.monotonic()
            round_path = report_dir / f"system_round_{round_index:05d}.json"
            command = [
                sys.executable,
                str(SYSTEM_STRESS),
                "--base-url", args.base_url,
                "--runtime-root", str(server_runtime_root),
                "--out", str(round_path),
                "--session-mode", "clone",
                "--session-pool", str(max(args.account_count, int(args.session_pool))),
                "--logical-users", str(max(args.round_ops, args.account_count)),
                "--ops", str(max(args.round_ops, args.account_count)),
                "--concurrency", str(scheduled_load_level),
                "--operation-mode", "rotation",
                "--rotation-offset", str(round_rotation_offset(round_index, max(args.round_ops, args.account_count))),
                "--require-all-accounts",
                "--require-operation-coverage",
                "--allow-server-busy",
                "--max-server-busy-rate", str(max(0.0, min(1.0, args.max_server_busy_rate))),
                "--max-ordinary-p95-ms", str(max(1.0, float(args.max_ordinary_p95_ms))),
                "--max-ordinary-p99-ms", str(max(1.0, float(args.max_ordinary_p99_ms))),
                "--max-hf-generates", "0",
            ]
            if args.server_pids:
                command.extend(["--server-pids", str(args.server_pids)])
            stage_deadline = deadline
            stage_stop_reason = "campaign_deadline"
            if effective_load_required and ramp_index < len(ramp_schedule):
                stage_deadline = min(
                    deadline,
                    started + float(ramp_schedule[ramp_index]["end_seconds"]),
                )
                stage_stop_reason = "load_stage_deadline"

            def handle_round_stop(reason: str) -> None:
                if reason != "load_stage_deadline":
                    stop_background_children(reason)

            run = run_command(
                command,
                stdout_path=report_dir / f"system_round_{round_index:05d}.stdout",
                timeout=max(60, int(args.round_timeout_seconds)),
                env={
                    "HACKME_STRESS_ACCOUNTS": account_spec,
                    "HACKME_STRESS_TEST_PASSWORD": args.account_password,
                },
                stop_at_monotonic=stage_deadline,
                stop_at_reason=stage_stop_reason,
                stop_file=stop_file,
                on_stop=handle_round_stop,
            )
            run["scheduled_load_level"] = scheduled_load_level
            round_runs.append(run)
            if run.get("partial"):
                record_effective_load_round(
                    payload={"ok": False, "summary": {"total_ops": 0}},
                    run=run,
                    scheduled_load_level=scheduled_load_level,
                    window_started_at=round_window_started_at,
                    window_started_monotonic=round_window_started_monotonic,
                )
                if run.get("stop_reason") == "load_stage_deadline":
                    if not advance_due_ramp_stages(time.monotonic()):
                        termination_reason = ramp_schedule_failure
                        write_checkpoint("ramp_schedule_failed")
                        break
                    write_checkpoint(f"round_{round_index:05d}_stage_boundary")
                    continue
                termination_reason = str(run.get("stop_reason") or "round_timeout")
                write_checkpoint(f"round_{round_index:05d}_partial")
                break
            payload = load_json(round_path)
            payload["_artifact_path"] = str(round_path)
            payload["_returncode"] = run["returncode"]
            payload["_scheduled_load_level"] = scheduled_load_level
            round_payloads.append(payload)
            record_effective_load_round(
                payload=payload,
                run=run,
                scheduled_load_level=scheduled_load_level,
                window_started_at=round_window_started_at,
                window_started_monotonic=round_window_started_monotonic,
            )

            if points_state is not None and points_state["process"].poll() is not None:
                points_run = finish_command(points_state, timeout=5)
                points_payload = load_json(points_report)
                points_state = None

            if browser_state is not None and browser_state["process"].poll() is not None:
                state = finish_command(browser_state, timeout=5)
                browser_root = Path(state["stdout"]).parent
                report_path = latest_playwright_report(browser_root)
                state["report"] = str(report_path) if report_path else ""
                state["result"] = load_json(report_path) if report_path else {"ok": False, "error": "playwright report missing"}
                browser_runs.append(state)
                browser_state = None

            write_checkpoint(f"round_{round_index:05d}_completed")

            elapsed = time.monotonic() - started
            print(
                json.dumps(
                    {
                        "event": "operational_soak_heartbeat",
                        "elapsed_seconds": round(elapsed, 1),
                        "remaining_seconds": round(max(0.0, deadline - time.monotonic()), 1),
                        "round": round_index,
                        "round_ok": payload.get("ok"),
                        "round_ops": (payload.get("summary") or {}).get("total_ops"),
                        "scheduled_load_level": scheduled_load_level,
                        "measured_active_workers": measured_active_workers(run, scheduled_load_level, payload),
                        "browser_runs_completed": len(browser_runs),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    except BaseException as exc:
        loop_error = exc
        termination_reason = "interrupted_by_signal" if isinstance(exc, KeyboardInterrupt) else "harness_exception"
    finally:
        stop_sentinel.set()

    termination_observed = time.monotonic()
    load_termination_monotonic = termination_observed
    external_control_reason = stop_control_reason(stop_file)
    if loop_error is not None:
        pass
    elif detected_harness_drift:
        termination_reason = "harness_drift"
    elif external_stop_requested():
        termination_reason = (
            "supervisor_completion_stop"
            if external_control_reason in SUPERVISOR_COMPLETION_REASONS
            else "external_stop_file"
        )
    elif termination_observed >= deadline:
        termination_reason = "campaign_deadline"
    elif not termination_reason:
        termination_reason = "unexpected_loop_exit"

    # No child is allowed to outlive any campaign terminal condition. Explicit
    # cancellation is used for drift/exception too; waiting for the old campaign
    # deadline here could strand points or browser workers for hours.
    stop_background_children(termination_reason)
    if points_state is not None:
        points_run = finish_background_child(
            points_state,
            timeout=max(60, int(args.round_timeout_seconds)),
        )
        points_payload = (
            {"ok": False, "terminal_status": "NOT_EVALUATED", "partial": True}
            if points_run.get("partial")
            else load_json(points_report)
        )
        points_state = None
    elif points_run is None:
        points_run = {"returncode": 0, "skipped": True, "partial": False, "orphan_pids": []}
        points_payload = {"ok": True, "skipped": True}

    if browser_state is not None:
        state = finish_background_child(
            browser_state,
            timeout=max(300, int(args.round_timeout_seconds)),
        )
        browser_root = Path(state["stdout"]).parent
        report_path = latest_playwright_report(browser_root)
        state["report"] = str(report_path) if report_path else ""
        state["result"] = (
            {"ok": False, "terminal_status": "NOT_EVALUATED", "partial": True}
            if state.get("partial")
            else load_json(report_path) if report_path else {"ok": False, "error": "playwright report missing"}
        )
        browser_runs.append(state)
        browser_state = None

    # Child teardown happens before waiting for HTTP sentinel threads so a slow
    # request cannot extend points/browser activity beyond the control edge.
    sentinel_thread.join(timeout=2 if external_stop_requested() else 30)

    wall_duration = time.monotonic() - process_started
    active_duration = max(0.0, min(termination_observed, deadline) - started)
    window_completed = termination_reason in {"campaign_deadline", "supervisor_completion_stop"}
    aggregate = aggregate_rounds(round_payloads, account_names)
    resource_evidence = aggregate_resource_evidence(round_payloads, args.server_pids)
    effective_load = effective_load_evidence()
    sentinel = sentinel_stats.summary()
    if termination_reason == "campaign_deadline" and loop_error is None and not detected_harness_drift:
        final_checks = {
            "health_readiness": final_control_request(root, "/api/admin/health/readiness"),
            "security_center": final_control_request(root, "/api/admin/security-center"),
            "log_chain": final_control_request(root, "/api/root/server-mode/logs/verify"),
            "points_wallet": final_control_request(root, "/api/points/wallet"),
            "ai_agent_status": final_control_request(root, "/api/ai-agent/status"),
        }
    else:
        final_checks = {
            "skipped": True,
            "terminal_status": "NOT_EVALUATED",
            "reason": termination_reason,
        }

    findings = []
    if termination_reason == "campaign_deadline" and active_duration + 1 < int(args.duration_seconds):
        findings.append({"severity": "critical", "title": "requested soak duration was not completed"})
    if loop_error is not None:
        findings.append({
            "severity": "critical",
            "title": "operational soak harness raised an exception",
            "error_type": loop_error.__class__.__name__,
        })
    timed_out_rounds = [item for item in round_runs if item.get("timed_out")]
    if timed_out_rounds:
        findings.append({"severity": "critical", "title": "system stress round timed out", "count": len(timed_out_rounds)})
    if detected_harness_drift:
        findings.append({"severity": "critical", "title": "test harness source changed during the run", "files": detected_harness_drift})
    if aggregate["round_failures"]:
        findings.append({"severity": "high", "title": "one or more synchronized system rounds failed", "count": len(aggregate["round_failures"])})
    if effective_load_required and not effective_load.get("ok"):
        findings.append({
            "severity": "critical",
            "classification": "FAIL_PRODUCT",
            "title": "required 4-8-16-32 ramp or effective target-load coverage failed",
            "ramp_completed_levels": (effective_load.get("ramp") or {}).get("completed_levels"),
            "baseline_32_operations_per_minute": effective_load.get("baseline_32_operations_per_minute"),
            "target_load_coverage": (effective_load.get("target_load_summary") or {}).get("target_load_coverage"),
        })
    if aggregate["hard_failures"]:
        findings.append({"severity": "high", "title": "transport or HTTP 5xx failures occurred", "count": aggregate["hard_failures"]})
    if aggregate["server_busy_rate"] > max(0.0, min(1.0, args.max_server_busy_rate)):
        findings.append({"severity": "high", "title": "server_busy rate exceeded configured SLA", "rate": aggregate["server_busy_rate"]})
    if window_completed and aggregate["missing_operations"]:
        findings.append({"severity": "high", "title": "full-function operation rotation incomplete", "missing": aggregate["missing_operations"]})
    if window_completed and aggregate["accounts_without_operations"]:
        findings.append({"severity": "high", "title": "configured accounts received no operations", "accounts": aggregate["accounts_without_operations"]})
    if window_completed and aggregate["operations_without_success"]:
        findings.append({"severity": "high", "title": "required positive-path operations never returned 2xx", "operations": aggregate["operations_without_success"]})
    if window_completed and aggregate["account_success_gaps"]:
        findings.append({"severity": "high", "title": "one or more accounts missed required positive-path success", "accounts": aggregate["account_success_gaps"]})
    if sentinel["errors"]:
        findings.append({"severity": "high", "title": "root/manager sentinel observed failures", "count": len(sentinel["errors"])})
    if float(sentinel.get("server_busy_rate") or 0.0) > max(0.0, min(1.0, args.max_server_busy_rate)):
        findings.append({"severity": "high", "title": "root/manager sentinel server-busy rate exceeded SLA", "rate": sentinel.get("server_busy_rate")})
    slow_sentinels = {
        name: evidence.get("p95_ms")
        for name, evidence in (sentinel.get("checks") or {}).items()
        if float(evidence.get("p95_ms") or 0.0) > max(1.0, float(args.max_sentinel_p95_ms))
    }
    if slow_sentinels:
        findings.append({"severity": "high", "title": "root/manager sentinel p95 exceeded SLA", "checks": slow_sentinels})
    if window_completed and not args.allow_short_duration and not resource_evidence["server_pids"]:
        findings.append({"severity": "high", "title": "server PID RSS evidence was not configured"})
    if window_completed and resource_evidence["server_pids"] and resource_evidence["monitored_rss_max_mb"] <= 0:
        findings.append({"severity": "high", "title": "configured server PIDs produced no RSS evidence"})
    if window_completed and not args.skip_points_stress and points_run.get("partial"):
        findings.append({"severity": "high", "title": "PointsChain/economy stress produced no terminal evidence"})
    elif not points_run.get("partial") and (int(points_run.get("returncode") or 0) != 0 or points_payload.get("ok") is False):
        findings.append({"severity": "high", "title": "concurrent PointsChain/economy stress failed"})
    completed_browser_runs = [item for item in browser_runs if not item.get("partial")]
    failed_browser_runs = [
        item
        for item in completed_browser_runs
        if int(item.get("returncode") or 0) != 0 or not (item.get("result") or {}).get("ok")
    ]
    if failed_browser_runs:
        findings.append({"severity": "high", "title": "browser full-feature rotation failed", "count": len(failed_browser_runs)})
    if window_completed and not args.skip_browser and not completed_browser_runs:
        findings.append({"severity": "high", "title": "browser rotation produced no terminal evidence"})
    orphan_evidence = {
        "rounds": sorted({pid for item in round_runs for pid in (item.get("orphan_pids") or [])}),
        "points": list(points_run.get("orphan_pids") or []),
        "browser": sorted({pid for item in browser_runs for pid in (item.get("orphan_pids") or [])}),
    }
    if any(orphan_evidence.values()):
        findings.append({"severity": "critical", "title": "managed child processes remained alive", "pids": orphan_evidence})
    if not final_checks.get("skipped"):
        for name, result in final_checks.items():
            if int(result.get("status") or 0) != 200 or not result.get("ok"):
                findings.append({"severity": "high", "title": f"final control-plane check failed: {name}", "status": result.get("status")})

    if loop_error is not None and not isinstance(loop_error, KeyboardInterrupt):
        terminal_status = "FAIL_HARNESS"
        verdict = "FAIL"
    elif termination_reason == "harness_drift":
        terminal_status = "INVALIDATED"
        verdict = "INVALIDATED"
    elif termination_reason in {"external_stop_file", "external_stop_file_preexisting", "interrupted_by_signal", "unexpected_loop_exit"}:
        terminal_status = "INTERRUPTED"
        verdict = "INTERRUPTED"
    elif termination_reason in {"round_timeout", "command_timeout"}:
        terminal_status = "FAIL_HARNESS"
        verdict = "FAIL"
    else:
        terminal_status = "PASS" if not findings else "FAIL"
        verdict = terminal_status
    ok = verdict == "PASS"
    classification = (
        "PASS"
        if ok
        else "FAIL_HARNESS"
        if terminal_status == "FAIL_HARNESS"
        else "INVALIDATED"
        if terminal_status == "INVALIDATED"
        else "INTERRUPTED"
        if terminal_status == "INTERRUPTED"
        else "FAIL_PRODUCT"
    )

    payload = {
        "schema_version": OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
        "ok": ok,
        "verdict": verdict,
        "terminal_status": terminal_status,
        "classification": classification,
        "termination_reason": termination_reason,
        "external_control_reason": external_control_reason,
        "window_completed": window_completed,
        "production_signoff_eligible": (
            termination_reason
            in {"campaign_deadline", "supervisor_completion_stop"}
            and not args.allow_short_duration
            and int(args.duration_seconds) >= MIN_SIGNOFF_SECONDS
            and (
                not effective_load_required
                or activation_evidence.get("ok") is True
            )
            and effective_load.get("ok") is True
            and ok
        ),
        "base_url": args.base_url,
        "runtime_root": str(runtime_root),
        "server_runtime_root": str(server_runtime_root),
        "started_at": started_at,
        "finished_at": utc_now(),
        "requested_duration_seconds": int(args.duration_seconds),
        "actual_duration_seconds": round(active_duration, 3),
        "wall_duration_seconds": round(wall_duration, 3),
        "allow_short_duration": bool(args.allow_short_duration),
        "campaign_level": args.campaign_level,
        "activation": activation_evidence,
        "concurrency": int(args.concurrency),
        "accounts": account_names,
        "aggregate": aggregate,
        "effective_load": effective_load,
        "resource_evidence": resource_evidence,
        "sentinel": sentinel,
        "points_stress": {"run": points_run, "result": points_payload, "report": str(points_report)},
        "browser_runs": browser_runs,
        "final_checks": final_checks,
        "round_runs": round_runs,
        "partial_round_runs": [item for item in round_runs if item.get("partial")],
        "orphan_evidence": orphan_evidence,
        "source_harness_hashes": source_harness_hashes,
        "harness_drift": detected_harness_drift,
        "checkpoint": str(checkpoint_path),
        "findings": findings,
    }
    atomic_write_json(out_path, payload)
    atomic_write_json(checkpoint_path, {
        "status": "terminal",
        "phase": "terminal",
        "terminal_status": terminal_status,
        "termination_reason": termination_reason,
        "external_control_reason": external_control_reason,
        "production_signoff_eligible": payload["production_signoff_eligible"],
        "activation": activation_evidence,
        "verdict": payload["verdict"],
        "started_at": started_at,
        "finished_at": payload["finished_at"],
        "actual_duration_seconds": payload["actual_duration_seconds"],
        "aggregate": aggregate,
        "effective_load": effective_load,
        "sentinel": sentinel,
        "resource_evidence": resource_evidence,
        "source_harness_hashes": source_harness_hashes,
        "harness_drift": detected_harness_drift,
        "report": str(out_path),
    })
    print(json.dumps({"ok": payload["ok"], "verdict": payload["verdict"], "terminal_status": terminal_status, "report": str(out_path), "findings": findings}, ensure_ascii=False, indent=2))
    if ok:
        return 0
    if terminal_status == "FAIL_HARNESS":
        return 2
    if terminal_status == "INTERRUPTED":
        return 3
    if terminal_status == "INVALIDATED":
        return 4
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
