#!/usr/bin/env python3
"""Run the isolated 24-hour operational, recovery, and usability campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Mapping
from urllib.parse import quote

import requests
import urllib3


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.campaign_readiness import LayeredReadinessProbe, ReadinessConfig
from scripts.testing.campaign_activation import (
    CORE_ACK_SCHEMA_VERSION,
    CORE_ACTIVATION_SCHEMA_VERSION,
    CORE_READY_SCHEMA_VERSION,
    ActivationArtifactError,
    artifact_exists,
    assert_fresh_artifact_paths,
    canonical_digest,
    prepare_private_directory,
    secure_read_json,
    secure_write_once_json,
)
from scripts.testing.campaign_load import (
    ALLOWED_MAINTENANCE_REASONS,
    LOAD_SAMPLE_SCHEMA_VERSION,
)
from scripts.testing.campaign_cgroup import MANDATORY_MANAGED_ROLES
from scripts.testing.campaign_scenario_binding import (
    FORMAL_BINDING_GATE_SCHEMA_VERSION,
    FORMAL_SCENARIO_BINDINGS,
    ScenarioRunnerRegistration,
    build_and_validate_formal_scenario_bindings,
)
from scripts.testing.operation_coverage import CAMPAIGN_SCENARIO_CONTRACTS
from scripts.testing.campaign_observability import (
    ProcessRoleRegistry,
    ResourceCollector as StructuredResourceCollector,
    ResourceCollectorConfig as StructuredResourceCollectorConfig,
    ResourceMonitor as StructuredResourceMonitor,
)
from scripts.testing.campaign_security_sentinel import (
    ProductionSecuritySentinel,
    SecuritySentinelConfig,
    atomic_write_result as write_security_sentinel_result,
)
from scripts.testing.campaign_secret_scan import (
    ControlSnapshotConfig,
    SecretScanConfig,
    build_sensitive_needle_inventory,
    scan_campaign_secrets,
    snapshot_control_evidence,
)
from scripts.testing.campaign_source_freeze import GitSourceFreezer, SOURCE_FREEZE_SCHEMA_VERSION
from scripts.testing.campaign_state import CampaignState, CampaignStateError, CampaignStateMachine, process_start_ticks
from scripts.testing.campaign_watchdog import atomic_write_json as durable_atomic_write_json

LAUNCHER = ROOT / "test_for_develop.sh"
SOAK = ROOT / "scripts" / "testing" / "operational_soak_probe.py"
SMOKE_LOAD = ROOT / "scripts" / "testing" / "campaign_smoke_load.py"
MIN_FORMAL_SECONDS = 24 * 60 * 60
SMOKE_REQUIRED_MANAGED_ROLES = frozenset({
    "primary",
    "recovery",
    "security_sentinel",
    "load_generator",
    "scenario",
})
LOG_SCAN_CHUNK_CHARACTERS = 1024 * 1024
LOG_SCAN_OVERLAP_CHARACTERS = 512
LOG_SCAN_PROGRESS_CHARACTERS = 32 * 1024 * 1024
LOG_SCAN_PROGRESS_SECONDS = 30.0
LOG_SCAN_MAX_FILES = 10_000
LOG_SCAN_MAX_DIRECTORY_ENTRIES = 50_000
LOG_SCAN_DISCOVERY_PROGRESS_ENTRIES = 1_024
LOG_SCAN_DISCOVERY_PROGRESS_SECONDS = 15.0
LAUNCHER_LOG_CHUNK_BYTES = 1024 * 1024
LAUNCHER_LOG_DIAGNOSTIC_BYTES = 256 * 1024
LAUNCHER_LOG_PROGRESS_BYTES = 32 * 1024 * 1024
LAUNCHER_LOG_PROGRESS_SECONDS = 30.0
CAMPAIGN_SECRET_SCAN_PROGRESS_BYTES = 32 * 1024 * 1024
CAMPAIGN_SECRET_SCAN_PROGRESS_ENTRIES = 4096
CAMPAIGN_SECRET_SCAN_PROGRESS_SECONDS = 30.0
CAMPAIGN_CONTROL_SNAPSHOT_PROGRESS_BYTES = 32 * 1024 * 1024
CAMPAIGN_CONTROL_SNAPSHOT_PROGRESS_ENTRIES = 4096
CAMPAIGN_CONTROL_SNAPSHOT_PROGRESS_SECONDS = 30.0
PREFLIGHT_SCAN_MAX_ENTRIES = 500_000
PREFLIGHT_SCAN_MAX_DEPTH = 64
PREFLIGHT_SCAN_MAX_RUNTIME_PATHS = 10_000
PREFLIGHT_SCAN_PROGRESS_ENTRIES = 4_096
PREFLIGHT_SCAN_PROGRESS_SECONDS = 15.0
PREFLIGHT_RUNTIME_NAMES = frozenset({"runtime", "__pycache__", ".pytest_cache"})
PREFLIGHT_PRUNE_NAMES = frozenset({".git", ".hg", ".svn", ".venv", "venv", "node_modules"})
CORE_REPORT_VALIDATION_SCHEMA_VERSION = "hackme.campaign-child-report-validation.v1"
OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION = "hackme.operational-soak-report.v1"
SMOKE_LOAD_REPORT_SCHEMA_VERSION = "hackme.campaign-smoke-load.v2"
CORE_REPORT_MAX_BYTES = 64 * 1024 * 1024
CORE_CHECKPOINT_MAX_BYTES = 8 * 1024 * 1024
CORE_REPORT_READ_CHUNK_BYTES = 1024 * 1024
CORE_REPORT_PROGRESS_BYTES = 8 * 1024 * 1024
CORE_REPORT_PROGRESS_SECONDS = 15.0
CORE_REPORT_CARDINALITY_LIMITS = {
    "accounts": 10_000,
    "round_runs": 25_000,
    "partial_round_runs": 25_000,
    "browser_runs": 10_000,
    "target_load_samples": 50_000,
    "ramp_stages": 4,
    "ramp_round_evidence": 50_000,
    "smoke_load_samples": 10_000,
    "smoke_workers": 256,
}
SUPERVISED_LEVEL_DURATIONS = {
    "smoke": 180,
    "rehearsal": 3_600,
    "formal": MIN_FORMAL_SECONDS,
}
# The supervisor writes this exact profile into its signed runtime contract and
# also supplies every value explicitly on the runner argv.  Re-validating the
# same values inside the managed runner prevents a hand-edited activation file
# or a late argv option from weakening the campaign after preflight.
SUPERVISED_RUNNER_PROFILES: dict[str, dict[str, int | float]] = {
    level: {
        "workers": 4,
        "threads": 8,
        "account_count": 10,
        "round_ops": 1_000,
        "concurrency": 32,
        "session_pool": 20,
        "browser_interval_seconds": 3 * 60 * 60,
        "resource_interval": 5.0,
        "heartbeat_interval": 60.0,
        "scenario_join_timeout_seconds": 8 * 60 * 60,
        "minimum_free_gb": 20.0,
        "max_server_busy_rate": 0.05,
        "max_ordinary_p95_ms": 3_000.0,
        "max_ordinary_p99_ms": 8_000.0,
        "max_sentinel_p95_ms": 3_000.0,
    }
    for level in SUPERVISED_LEVEL_DURATIONS
}
SUPERVISED_RUNNER_PROFILE_OPTIONS = {
    "workers": "--workers",
    "threads": "--threads",
    "account_count": "--account-count",
    "round_ops": "--round-ops",
    "concurrency": "--concurrency",
    "session_pool": "--session-pool",
    "browser_interval_seconds": "--browser-interval-seconds",
    "resource_interval": "--resource-interval",
    "heartbeat_interval": "--heartbeat-interval",
    "scenario_join_timeout_seconds": "--scenario-join-timeout-seconds",
    "minimum_free_gb": "--minimum-free-gb",
    "max_server_busy_rate": "--max-server-busy-rate",
    "max_ordinary_p95_ms": "--max-ordinary-p95-ms",
    "max_ordinary_p99_ms": "--max-ordinary-p99-ms",
    "max_sentinel_p95_ms": "--max-sentinel-p95-ms",
}
SUPERVISED_LOAD_POLICIES: dict[str, dict[str, Any]] = {
    level: {
        "ramp_required": level in {"rehearsal", "formal"},
        "ramp_levels": [4, 8, 16, 32],
        "minimum_ramp_stage_seconds": (
            {"4": 600.0, "8": 1_200.0, "16": 1_800.0, "32": 0.0}
            if level == "formal"
            else {"4": 60.0, "8": 120.0, "16": 180.0, "32": 0.0}
            if level == "rehearsal"
            else {"4": 0.0, "8": 0.0, "16": 0.0, "32": 0.0}
        ),
        "minimum_target_load_coverage": 0.90,
        "minimum_active_workers_at_32": 28,
        "minimum_baseline_throughput_ratio": 0.80,
        "minimum_effective_operation_ratio": 0.85,
        "maximum_stage_boundary_lag_seconds": 15.0,
        "ramp_completion_deadline_seconds": (
            3_600.0 if level == "formal" else 360.0 if level == "rehearsal" else 0.0
        ),
        "minimum_post_ramp_seconds": (
            82_800.0 if level == "formal" else 3_240.0 if level == "rehearsal" else 0.0
        ),
    }
    for level in SUPERVISED_LEVEL_DURATIONS
}
HEARTBEAT_PUMP_INTERVAL_SECONDS = 15.0
EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION = "hackme.operational-effective-load.v1"
CORE_READY_TIMEOUT_SECONDS = 600.0
CORE_ACK_TIMEOUT_SECONDS = 15.0
CORE_ACTIVATION_LEAD_SECONDS = 5.0
SENSITIVE_FLAGS = {
    "--password",
    "--root-password",
    "--manager-password",
    "--test-password",
    "--user-password",
    "--member-password",
    "--account-password",
    "--accounts",
}
PROTECTED_FINANCIAL_DATABASES = ("finance.db", "points_chain.db", "trading.db")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "path": str(path)}


def bounded_launcher_log_snapshot(
    path: Path,
    secret_values: Mapping[str, str],
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Stream one immutable launcher-log snapshot while retaining only a tail.

    The launcher has already exited when this is called.  Binding the read to
    the opened descriptor's inode and initial length prevents a path rotation,
    truncate, or replacement from silently changing the evidence underneath
    the reader.  Credential detection still covers the complete snapshot,
    including values split across chunk boundaries.
    """

    errors: list[dict[str, Any]] = []
    leaked: set[str] = set()
    tail = bytearray()
    scanned_bytes = 0
    initial_size = 0
    secrets_bytes = {
        str(label): str(value).encode("utf-8")
        for label, value in secret_values.items()
        if str(value or "")
    }
    overlap = max((len(value) - 1 for value in secrets_bytes.values()), default=0)
    carry = b""
    next_progress_at = time.monotonic() + LAUNCHER_LOG_PROGRESS_SECONDS
    bytes_since_progress = 0
    try:
        if path.is_symlink():
            raise OSError("launcher log symlink rejected")
        with path.open("rb") as handle:
            initial = os.fstat(handle.fileno())
            initial_size = int(initial.st_size)
            if not stat.S_ISREG(initial.st_mode):
                raise OSError("launcher log is not a regular file")
            opened_path = path.stat()
            if (opened_path.st_dev, opened_path.st_ino) != (initial.st_dev, initial.st_ino):
                raise OSError("launcher log path changed while opening snapshot")
            while scanned_bytes < initial_size:
                requested = min(LAUNCHER_LOG_CHUNK_BYTES, initial_size - scanned_bytes)
                chunk = handle.read(requested)
                if not chunk:
                    errors.append({
                        "code": "launcher_log_truncated_during_snapshot",
                        "expected_bytes": initial_size,
                        "scanned_bytes": scanned_bytes,
                    })
                    break
                scanned_bytes += len(chunk)
                bytes_since_progress += len(chunk)
                window = carry + chunk
                for label, value in secrets_bytes.items():
                    if value in window:
                        leaked.add(label)
                carry = window[-overlap:] if overlap else b""
                tail.extend(chunk)
                if len(tail) > LAUNCHER_LOG_DIAGNOSTIC_BYTES:
                    del tail[:-LAUNCHER_LOG_DIAGNOSTIC_BYTES]
                if (
                    bytes_since_progress >= LAUNCHER_LOG_PROGRESS_BYTES
                    or time.monotonic() >= next_progress_at
                ):
                    if progress_callback is not None:
                        progress_callback(f"launcher_log_scan_progress:{path.name}:{scanned_bytes}")
                    bytes_since_progress = 0
                    next_progress_at = time.monotonic() + LAUNCHER_LOG_PROGRESS_SECONDS
            final_fd = os.fstat(handle.fileno())
            if (final_fd.st_dev, final_fd.st_ino) != (initial.st_dev, initial.st_ino):
                errors.append({"code": "launcher_log_fd_identity_changed"})
            if int(final_fd.st_size) != initial_size:
                errors.append({
                    "code": "launcher_log_size_changed_during_snapshot",
                    "initial_bytes": initial_size,
                    "final_bytes": int(final_fd.st_size),
                })
            if (
                int(final_fd.st_mtime_ns) != int(initial.st_mtime_ns)
                or int(final_fd.st_ctime_ns) != int(initial.st_ctime_ns)
            ):
                errors.append({"code": "launcher_log_metadata_changed_during_snapshot"})
            try:
                final_path = path.stat()
            except OSError as exc:
                errors.append({
                    "code": "launcher_log_path_missing_after_snapshot",
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
            else:
                if (final_path.st_dev, final_path.st_ino) != (initial.st_dev, initial.st_ino):
                    errors.append({"code": "launcher_log_replaced_or_rotated_during_snapshot"})
                if int(final_path.st_size) != int(final_fd.st_size):
                    errors.append({"code": "launcher_log_path_fd_size_mismatch"})
    except Exception as exc:
        errors.append({
            "code": "launcher_log_snapshot_error",
            "error": f"{exc.__class__.__name__}: {exc}",
        })

    diagnostic = tail.decode("utf-8", errors="replace")
    for label, value in secret_values.items():
        if value:
            diagnostic = diagnostic.replace(str(value), f"[redacted:{label}]")
    return {
        "schema_version": "hackme.launcher-log-snapshot.v1",
        "ok": not errors and scanned_bytes == initial_size,
        "path": str(path),
        "initial_bytes": initial_size,
        "scanned_bytes": scanned_bytes,
        "diagnostic_tail": diagnostic,
        "diagnostic_bytes": len(tail),
        "diagnostic_truncated": initial_size > len(tail),
        "secret_leak_labels": sorted(leaked),
        "errors": errors,
    }


def _git_ignored_repo_paths(root: Path, relative_paths: list[str]) -> set[str]:
    if not relative_paths:
        return set()
    encoded = b"\0".join(os.fsencode(value) for value in relative_paths) + b"\0"
    completed = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--stdin", "-z"],
        input=encoded,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode not in {0, 1}:
        detail = os.fsdecode(completed.stderr or b"").strip()[:500]
        raise RuntimeError(
            f"git check-ignore failed with {completed.returncode}: {detail}"
        )
    return {
        os.fsdecode(value)
        for value in completed.stdout.split(b"\0")
        if value
    }


def bounded_repo_runtime_scan(
    root: Path,
    *,
    progress_callback: Callable[[str], None] | None = None,
    ignored_classifier: Callable[[Path, list[str]], set[str]] | None = None,
    max_entries: int = PREFLIGHT_SCAN_MAX_ENTRIES,
) -> dict[str, Any]:
    """Find unignored runtime pollution without recursively walking runtime data."""

    errors: list[dict[str, Any]] = []
    candidates: set[str] = set()
    pruned_samples: list[str] = []
    pruned_count = 0
    entries_scanned = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    next_progress_at = time.monotonic() + PREFLIGHT_SCAN_PROGRESS_SECONDS
    entries_since_progress = 0
    stopped = False

    def publish(detail: str) -> bool:
        if progress_callback is None:
            return True
        try:
            progress_callback(detail)
            return True
        except Exception as exc:
            errors.append({
                "code": "preflight_scan_progress_callback_failed",
                "error": f"{exc.__class__.__name__}: {exc}",
            })
            return False

    if not publish("preflight_runtime_pollution_scan_started"):
        stopped = True
    while stack and not stopped:
        directory, depth = stack.pop()
        if depth > PREFLIGHT_SCAN_MAX_DEPTH:
            errors.append({
                "code": "preflight_scan_depth_limit_exceeded",
                "path": str(directory),
                "maximum_depth": PREFLIGHT_SCAN_MAX_DEPTH,
            })
            continue
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries_scanned += 1
                    entries_since_progress += 1
                    if entries_scanned > max(1, int(max_entries)):
                        errors.append({
                            "code": "preflight_scan_entry_limit_exceeded",
                            "maximum_entries": int(max_entries),
                        })
                        stopped = True
                        break
                    if (
                        entries_since_progress >= PREFLIGHT_SCAN_PROGRESS_ENTRIES
                        or time.monotonic() >= next_progress_at
                    ):
                        if not publish(
                            f"preflight_runtime_pollution_scan_progress:{entries_scanned}"
                        ):
                            stopped = True
                            break
                        entries_since_progress = 0
                        next_progress_at = time.monotonic() + PREFLIGHT_SCAN_PROGRESS_SECONDS
                    try:
                        is_symlink = entry.is_symlink()
                        is_directory = entry.is_dir(follow_symlinks=False)
                    except OSError as exc:
                        errors.append({
                            "code": "preflight_scan_entry_stat_failed",
                            "path": entry.path,
                            "error": f"{exc.__class__.__name__}: {exc}",
                        })
                        continue
                    if is_symlink and entry.name in PREFLIGHT_RUNTIME_NAMES:
                        errors.append({
                            "code": "preflight_runtime_path_symlink_rejected",
                            "path": entry.path,
                        })
                        continue
                    if not is_directory:
                        continue
                    try:
                        relative = Path(entry.path).relative_to(root).as_posix()
                    except ValueError as exc:
                        errors.append({
                            "code": "preflight_scan_path_escape",
                            "path": entry.path,
                            "error": str(exc),
                        })
                        continue
                    if entry.name in PREFLIGHT_RUNTIME_NAMES:
                        if len(candidates) >= PREFLIGHT_SCAN_MAX_RUNTIME_PATHS:
                            errors.append({
                                "code": "preflight_runtime_path_limit_exceeded",
                                "maximum_paths": PREFLIGHT_SCAN_MAX_RUNTIME_PATHS,
                            })
                            stopped = True
                            break
                        candidates.add(relative)
                        pruned_count += 1
                        if len(pruned_samples) < 100:
                            pruned_samples.append(relative)
                        continue
                    if entry.name in PREFLIGHT_PRUNE_NAMES:
                        pruned_count += 1
                        if len(pruned_samples) < 100:
                            pruned_samples.append(relative)
                        continue
                    stack.append((Path(entry.path), depth + 1))
        except OSError as exc:
            errors.append({
                "code": "preflight_scan_directory_failed",
                "path": str(directory),
                "error": f"{exc.__class__.__name__}: {exc}",
            })

    ignored: set[str] = set()
    ordered_candidates = sorted(candidates)
    if ordered_candidates:
        try:
            publish("preflight_runtime_pollution_ignore_classification_started")
            ignored = (ignored_classifier or _git_ignored_repo_paths)(
                root,
                ordered_candidates,
            )
            publish("preflight_runtime_pollution_ignore_classification_completed")
        except Exception as exc:
            errors.append({
                "code": "preflight_ignored_path_classification_failed",
                "error": f"{exc.__class__.__name__}: {exc}",
            })
    pollution = sorted(candidates - ignored)
    publish("preflight_runtime_pollution_scan_completed")
    return {
        "schema_version": "hackme.preflight-runtime-scan.v1",
        "ok": not errors,
        "complete": not errors,
        "entries_scanned": entries_scanned,
        "maximum_entries": int(max_entries),
        "runtime_candidates": ordered_candidates,
        "ignored_runtime_paths": sorted(candidates & ignored),
        "repo_runtime_pollution": pollution,
        "pruned_directory_count": pruned_count,
        "pruned_directory_samples": pruned_samples,
        "errors": errors,
    }


def load_bounded_child_report(
    path: Path,
    *,
    expected_schema: str | None,
    progress_callback: Callable[[str], None] | None = None,
    max_bytes: int = CORE_REPORT_MAX_BYTES,
    validate_cardinality: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a child JSON report within an immutable, hard-bounded snapshot."""

    errors: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    raw = bytearray()
    initial_size = 0
    scanned_bytes = 0
    next_progress_at = time.monotonic() + CORE_REPORT_PROGRESS_SECONDS
    bytes_since_progress = 0
    try:
        if path.is_symlink():
            raise OSError("child report symlink rejected")
        with path.open("rb") as handle:
            initial = os.fstat(handle.fileno())
            initial_size = int(initial.st_size)
            if not stat.S_ISREG(initial.st_mode):
                raise OSError("child report is not a regular file")
            opened_path = path.stat()
            if (opened_path.st_dev, opened_path.st_ino) != (initial.st_dev, initial.st_ino):
                raise OSError("child report path changed while opening snapshot")
            if initial_size <= 0:
                errors.append({"code": "child_report_empty"})
            elif initial_size > max(1, int(max_bytes)):
                errors.append({
                    "code": "child_report_size_limit_exceeded",
                    "size_bytes": initial_size,
                    "maximum_bytes": int(max_bytes),
                })
            else:
                while scanned_bytes < initial_size:
                    requested = min(
                        CORE_REPORT_READ_CHUNK_BYTES,
                        initial_size - scanned_bytes,
                    )
                    chunk = handle.read(requested)
                    if not chunk:
                        errors.append({
                            "code": "child_report_truncated_during_snapshot",
                            "expected_bytes": initial_size,
                            "scanned_bytes": scanned_bytes,
                        })
                        break
                    raw.extend(chunk)
                    scanned_bytes += len(chunk)
                    bytes_since_progress += len(chunk)
                    if (
                        bytes_since_progress >= CORE_REPORT_PROGRESS_BYTES
                        or time.monotonic() >= next_progress_at
                    ):
                        if progress_callback is not None:
                            progress_callback(
                                f"child_report_read_progress:{path.name}:{scanned_bytes}"
                            )
                        bytes_since_progress = 0
                        next_progress_at = time.monotonic() + CORE_REPORT_PROGRESS_SECONDS
            final_fd = os.fstat(handle.fileno())
            if (final_fd.st_dev, final_fd.st_ino) != (initial.st_dev, initial.st_ino):
                errors.append({"code": "child_report_fd_identity_changed"})
            if int(final_fd.st_size) != initial_size:
                errors.append({
                    "code": "child_report_size_changed_during_snapshot",
                    "initial_bytes": initial_size,
                    "final_bytes": int(final_fd.st_size),
                })
            if (
                int(final_fd.st_mtime_ns) != int(initial.st_mtime_ns)
                or int(final_fd.st_ctime_ns) != int(initial.st_ctime_ns)
            ):
                errors.append({"code": "child_report_metadata_changed_during_snapshot"})
            try:
                final_path = path.stat()
            except OSError as exc:
                errors.append({
                    "code": "child_report_path_missing_after_snapshot",
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
            else:
                if (final_path.st_dev, final_path.st_ino) != (initial.st_dev, initial.st_ino):
                    errors.append({"code": "child_report_replaced_during_snapshot"})
                if int(final_path.st_size) != int(final_fd.st_size):
                    errors.append({"code": "child_report_path_fd_size_mismatch"})
    except Exception as exc:
        errors.append({
            "code": "child_report_snapshot_error",
            "error": f"{exc.__class__.__name__}: {exc}",
        })

    payload: dict[str, Any] = {}
    if not errors and scanned_bytes == initial_size:
        try:
            decoded = bytes(raw).decode("utf-8", errors="strict")
            parsed = json.loads(decoded)
            if not isinstance(parsed, dict):
                errors.append({"code": "child_report_top_level_not_object"})
            else:
                payload = parsed
        except Exception as exc:
            errors.append({
                "code": "child_report_json_invalid",
                "error": f"{exc.__class__.__name__}: {exc}",
            })
    raw.clear()

    if payload and expected_schema is not None:
        actual_schema = payload.get("schema_version")
        if actual_schema != expected_schema:
            errors.append({
                "code": "child_report_schema_mismatch",
                "expected": expected_schema,
                "actual": actual_schema,
            })
    if payload and len(payload) > 256:
        errors.append({
            "code": "child_report_top_level_cardinality_exceeded",
            "actual": len(payload),
            "maximum": 256,
        })

    def bounded_list(
        value: Any,
        *,
        name: str,
        limit_name: str,
        required: bool = False,
    ) -> list[Any]:
        limit = int(CORE_REPORT_CARDINALITY_LIMITS[limit_name])
        if value is None and not required:
            return []
        if not isinstance(value, list):
            errors.append({"code": "child_report_cardinality_field_invalid", "field": name})
            return []
        counts[name] = len(value)
        if len(value) > limit:
            errors.append({
                "code": "child_report_cardinality_limit_exceeded",
                "field": name,
                "actual": len(value),
                "maximum": limit,
            })
        return value

    if payload and validate_cardinality:
        report_ok = payload.get("ok") is True
        if expected_schema == SMOKE_LOAD_REPORT_SCHEMA_VERSION:
            metrics = payload.get("metrics")
            if not isinstance(metrics, Mapping):
                if report_ok:
                    errors.append({"code": "child_report_schema_field_missing", "field": "metrics"})
                metrics = {}
            bounded_list(
                metrics.get("load_samples"),
                name="metrics.load_samples",
                limit_name="smoke_load_samples",
                required=report_ok,
            )
            workers = payload.get("workers")
            if workers is None and not report_ok:
                workers = {}
            if not isinstance(workers, Mapping):
                errors.append({"code": "child_report_cardinality_field_invalid", "field": "workers"})
            else:
                counts["workers"] = len(workers)
                if len(workers) > CORE_REPORT_CARDINALITY_LIMITS["smoke_workers"]:
                    errors.append({
                        "code": "child_report_cardinality_limit_exceeded",
                        "field": "workers",
                        "actual": len(workers),
                        "maximum": CORE_REPORT_CARDINALITY_LIMITS["smoke_workers"],
                    })
        else:
            bounded_list(
                payload.get("accounts"),
                name="accounts",
                limit_name="accounts",
                required=report_ok,
            )
            bounded_list(
                payload.get("round_runs"),
                name="round_runs",
                limit_name="round_runs",
                required=report_ok,
            )
            bounded_list(
                payload.get("partial_round_runs"),
                name="partial_round_runs",
                limit_name="partial_round_runs",
            )
            bounded_list(
                payload.get("browser_runs"),
                name="browser_runs",
                limit_name="browser_runs",
                required=report_ok,
            )
            effective = payload.get("effective_load")
            if effective is None and not report_ok:
                effective = {}
            if not isinstance(effective, Mapping):
                errors.append({"code": "child_report_schema_field_invalid", "field": "effective_load"})
                effective = {}
            bounded_list(
                effective.get("target_load_samples"),
                name="effective_load.target_load_samples",
                limit_name="target_load_samples",
                required=report_ok,
            )
            ramp = effective.get("ramp") if isinstance(effective, Mapping) else None
            if ramp is None and not report_ok:
                ramp = {}
            if not isinstance(ramp, Mapping):
                errors.append({"code": "child_report_schema_field_invalid", "field": "effective_load.ramp"})
                ramp = {}
            stages = ramp.get("stages")
            if stages is None and not report_ok:
                stages = {}
            elif stages is None:
                errors.append({"code": "child_report_schema_field_missing", "field": "effective_load.ramp.stages"})
                stages = {}
            if not isinstance(stages, Mapping):
                errors.append({"code": "child_report_schema_field_invalid", "field": "effective_load.ramp.stages"})
            else:
                counts["effective_load.ramp.stages"] = len(stages)
                stage_limit = CORE_REPORT_CARDINALITY_LIMITS["ramp_stages"]
                if len(stages) > stage_limit:
                    errors.append({
                        "code": "child_report_cardinality_limit_exceeded",
                        "field": "effective_load.ramp.stages",
                        "actual": len(stages),
                        "maximum": stage_limit,
                    })
                round_evidence_total = 0
                for stage_name, stage in stages.items():
                    if not isinstance(stage, Mapping):
                        errors.append({
                            "code": "child_report_schema_field_invalid",
                            "field": f"effective_load.ramp.stages.{stage_name}",
                        })
                        continue
                    evidence = stage.get("round_evidence")
                    if evidence is None:
                        continue
                    if not isinstance(evidence, list):
                        errors.append({
                            "code": "child_report_cardinality_field_invalid",
                            "field": f"effective_load.ramp.stages.{stage_name}.round_evidence",
                        })
                        continue
                    round_evidence_total += len(evidence)
                counts["effective_load.ramp.round_evidence"] = round_evidence_total
                limit = CORE_REPORT_CARDINALITY_LIMITS["ramp_round_evidence"]
                if round_evidence_total > limit:
                    errors.append({
                        "code": "child_report_cardinality_limit_exceeded",
                        "field": "effective_load.ramp.round_evidence",
                        "actual": round_evidence_total,
                        "maximum": limit,
                    })

    validation = {
        "schema_version": CORE_REPORT_VALIDATION_SCHEMA_VERSION,
        "ok": not errors,
        "path": str(path),
        "expected_report_schema": expected_schema,
        "actual_report_schema": payload.get("schema_version") if payload else None,
        "size_bytes": initial_size,
        "maximum_bytes": int(max_bytes),
        "scanned_bytes": scanned_bytes,
        "cardinality": counts,
        "limits": dict(CORE_REPORT_CARDINALITY_LIMITS),
        "errors": errors,
    }
    if not payload:
        payload = {
            "ok": False,
            "classification": "FAIL_HARNESS",
            "error": "child_report_validation_failed",
            "path": str(path),
        }
    return payload, validation


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * pct)))
    return round(ordered[index], 3)


def validate_tmp_path(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    tmp = Path("/tmp").resolve()
    if resolved != tmp and tmp not in resolved.parents:
        raise ValueError(f"{label} must remain under /tmp: {resolved}")
    return resolved


def validate_control_root(campaign_root: Path, control_root: Path) -> Path:
    """Require the live control plane to be a private sibling of artifacts."""

    campaign = validate_tmp_path(campaign_root, label="campaign root")
    control = validate_tmp_path(control_root, label="campaign control root")
    if control == Path("/tmp").resolve() or control == campaign:
        raise ValueError("campaign control root must be a distinct directory below /tmp")
    if control.parent != campaign.parent:
        raise ValueError("campaign control root must be a sibling of campaign root")
    return control


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sanitized_command(command: list[str]) -> list[str]:
    result: list[str] = []
    hide_next = False
    for value in command:
        text = str(value)
        if hide_next:
            result.append("[redacted]")
            hide_next = False
            continue
        matched = next((flag for flag in SENSITIVE_FLAGS if text.startswith(f"{flag}=")), "")
        if matched:
            result.append(f"{matched}=[redacted]")
            continue
        result.append(text)
        if text in SENSITIVE_FLAGS:
            hide_next = True
    return result


def _validated_native_worker_count(
    telemetry: object,
    *,
    configured_workers: int,
) -> tuple[bool, int]:
    if not isinstance(telemetry, Mapping):
        return False, 0
    histogram = telemetry.get("active_worker_histogram")
    if not isinstance(histogram, Mapping):
        return False, 0
    try:
        parsed = sorted(
            (int(value), int(count)) for value, count in histogram.items()
        )
    except (TypeError, ValueError):
        return False, 0
    if not parsed or any(value < 0 or count < 0 for value, count in parsed):
        return False, 0
    histogram_count = sum(count for _value, count in parsed)
    sample_count = int(telemetry.get("sample_count") or 0)
    if histogram_count <= 0 or histogram_count != sample_count:
        return False, 0
    target_index = int((histogram_count - 1) * 0.10)
    cumulative = 0
    p10 = 0
    for value, count in parsed:
        cumulative += count
        if cumulative > target_index:
            p10 = value
            break
    started = int(telemetry.get("operations_started") or 0)
    completed = int(telemetry.get("operations_completed") or 0)
    valid = bool(
        telemetry.get("schema_version") == "hackme.system-stress-worker-telemetry.v1"
        and telemetry.get("method") == "native_inflight_operation_counter_time_samples"
        and int(telemetry.get("configured_workers") or 0) == configured_workers
        and int(telemetry.get("sustained_active_workers") or -1) == p10
        and telemetry.get("complete") is True
        and int(
            telemetry.get("active_workers_at_stop")
            if telemetry.get("active_workers_at_stop") is not None
            else -1
        ) == 0
        and started > 0
        and started == completed
    )
    return valid, p10


def validate_effective_load_evidence(
    payload: Mapping[str, Any],
    *,
    campaign_level: str,
) -> dict[str, Any]:
    """Re-derive the supervised ramp/target gate from child evidence."""

    required = campaign_level in {"rehearsal", "formal"}
    evidence = payload.get("effective_load")
    if not required:
        return {"required": False, "ok": True, "errors": []}
    errors: list[str] = []
    if not isinstance(evidence, Mapping):
        return {"required": True, "ok": False, "errors": ["effective_load_missing"]}
    if evidence.get("schema_version") != EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION:
        errors.append("schema_version")
    if evidence.get("required") is not True:
        errors.append("required")
    if evidence.get("campaign_level") != campaign_level:
        errors.append("campaign_level")
    ramp = evidence.get("ramp")
    if not isinstance(ramp, Mapping):
        errors.append("ramp")
        ramp = {}
    if ramp.get("required_levels") != [4, 8, 16, 32]:
        errors.append("ramp_required_levels")
    if ramp.get("completed_levels") != [4, 8, 16, 32]:
        errors.append("ramp_completed_levels")
    if ramp.get("ok") is not True:
        errors.append("ramp_ok")
    policy = SUPERVISED_LOAD_POLICIES[campaign_level]
    if ramp.get("minimum_stage_seconds") != policy["minimum_ramp_stage_seconds"]:
        errors.append("ramp_minimum_stage_seconds")
    scheduled_start = 0.0
    expected_schedule: list[dict[str, float | int]] = []
    for scheduled_level in (4, 8, 16):
        window_seconds = float(policy["minimum_ramp_stage_seconds"][str(scheduled_level)])
        expected_schedule.append({
            "level": scheduled_level,
            "start_seconds": scheduled_start,
            "end_seconds": scheduled_start + window_seconds,
        })
        scheduled_start += window_seconds
    if ramp.get("schedule") != expected_schedule:
        errors.append("ramp_schedule")
    if float(ramp.get("completion_deadline_seconds") or -1.0) != float(
        policy["ramp_completion_deadline_seconds"]
    ):
        errors.append("ramp_completion_deadline_contract")
    if float(ramp.get("maximum_stage_boundary_lag_seconds") or -1.0) != float(
        policy["maximum_stage_boundary_lag_seconds"]
    ):
        errors.append("ramp_boundary_lag_contract")
    if float(evidence.get("minimum_post_ramp_seconds") or -1.0) != float(
        policy["minimum_post_ramp_seconds"]
    ):
        errors.append("minimum_post_ramp_contract")
    ramp_completion = ramp.get("completion_elapsed_seconds")
    if (
        isinstance(ramp_completion, bool)
        or not isinstance(ramp_completion, (int, float))
        or float(ramp_completion)
        < float(policy["ramp_completion_deadline_seconds"])
        or float(ramp_completion)
        > float(policy["ramp_completion_deadline_seconds"])
        + float(policy["maximum_stage_boundary_lag_seconds"])
    ):
        errors.append("ramp_completion_deadline")
    if ramp.get("schedule_failure") not in ("", None):
        errors.append("ramp_schedule_failure")
    stages = ramp.get("stages")
    baseline_candidates: list[float] = []
    if not isinstance(stages, Mapping):
        errors.append("ramp_stages")
        stages = {}
    for level in (4, 8, 16):
        stage = stages.get(str(level)) if isinstance(stages, Mapping) else None
        if not isinstance(stage, Mapping):
            errors.append(f"ramp_stage:{level}")
            continue
        if stage.get("completed") is not True:
            errors.append(f"ramp_stage_completed:{level}")
        required_stage_seconds = float(policy["minimum_ramp_stage_seconds"][str(level)])
        if float(stage.get("minimum_stage_seconds") or 0.0) != required_stage_seconds:
            errors.append(f"ramp_stage_contract:{level}")
        schedule_row = expected_schedule[(4, 8, 16).index(level)]
        if (
            float(stage.get("scheduled_start_seconds") or 0.0)
            != float(schedule_row["start_seconds"])
            or float(stage.get("scheduled_end_seconds") or 0.0)
            != float(schedule_row["end_seconds"])
        ):
            errors.append(f"ramp_stage_schedule:{level}")
        completed_elapsed = stage.get("completed_elapsed_seconds")
        if (
            isinstance(completed_elapsed, bool)
            or not isinstance(completed_elapsed, (int, float))
            or float(completed_elapsed) < float(schedule_row["end_seconds"])
            or float(completed_elapsed)
            > float(schedule_row["end_seconds"])
            + float(policy["maximum_stage_boundary_lag_seconds"])
        ):
            errors.append(f"ramp_stage_deadline:{level}")
        if int(stage.get("valid_terminal_rounds") or 0) <= 0:
            errors.append(f"ramp_stage_terminal_round:{level}")
        if int(stage.get("measured_active_workers_peak") or 0) < int(math.ceil(level * 0.85)):
            errors.append(f"ramp_stage_workers:{level}")
        stage_rates = stage.get("normalized_32_throughput_samples")
        if not isinstance(stage_rates, list) or not stage_rates:
            errors.append(f"ramp_stage_throughput:{level}")
        else:
            for rate in stage_rates:
                if isinstance(rate, bool) or not isinstance(rate, (int, float)) or float(rate) <= 0:
                    errors.append(f"ramp_stage_throughput:{level}")
                else:
                    baseline_candidates.append(float(rate))
        round_evidence = stage.get("round_evidence")
        native_valid_rounds = 0
        if not isinstance(round_evidence, list) or not round_evidence:
            errors.append(f"ramp_stage_round_evidence:{level}")
        else:
            for row in round_evidence:
                if not isinstance(row, Mapping):
                    continue
                native_valid, native_workers = _validated_native_worker_count(
                    row.get("worker_telemetry"),
                    configured_workers=level,
                )
                row_valid = bool(
                    native_valid
                    and native_workers == int(row.get("measured_active_workers") or -1)
                    and row.get("terminal_valid") is True
                    and row.get("round_ok") is True
                    and not row.get("partial")
                    and int(row.get("returncode") or 0) == 0
                    and float(row.get("window_seconds") or 0.0) > 0
                    and int(row.get("operations_completed") or 0)
                    >= int(math.ceil(int(row.get("expected_operations") or 0) * 0.85))
                    and native_workers >= int(math.ceil(level * 0.85))
                    and float(
                        row.get("window_started_elapsed_seconds")
                        if row.get("window_started_elapsed_seconds") is not None
                        else -1.0
                    )
                    >= float(schedule_row["start_seconds"])
                    and float(row.get("window_finished_elapsed_seconds") or 0.0)
                    <= float(schedule_row["end_seconds"])
                )
                if row_valid:
                    native_valid_rounds += 1
        if native_valid_rounds != int(stage.get("valid_terminal_rounds") or 0):
            errors.append(f"ramp_stage_native_rounds:{level}")
    baseline = evidence.get("baseline_32_operations_per_minute")
    if isinstance(baseline, bool) or not isinstance(baseline, (int, float)) or float(baseline) <= 0:
        errors.append("baseline_32_operations_per_minute")
        baseline_value = 0.0
    else:
        baseline_value = float(baseline)
    if baseline_candidates and not math.isclose(
        baseline_value,
        float(median(baseline_candidates)),
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        errors.append("baseline_derivation")
    samples = evidence.get("target_load_samples")
    derived_target_seconds = 0.0
    derived_maintenance_seconds = 0.0
    previous_window_finished = float(policy["ramp_completion_deadline_seconds"])
    campaign_duration = float(SUPERVISED_LEVEL_DURATIONS[campaign_level])
    if not isinstance(samples, list) or not samples:
        errors.append("target_load_samples")
        samples = []
    for index, sample in enumerate(samples):
        prefix = f"target_sample:{index}"
        if not isinstance(sample, Mapping):
            errors.append(prefix)
            continue
        if sample.get("sample_schema_version") != LOAD_SAMPLE_SCHEMA_VERSION:
            errors.append(f"{prefix}:schema")
        seconds = sample.get("window_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(float(seconds)) or float(seconds) <= 0:
            errors.append(f"{prefix}:window_seconds")
            continue
        sample_started = sample.get("window_started_elapsed_seconds")
        sample_finished = sample.get("window_finished_elapsed_seconds")
        fixed_window_valid = bool(
            not isinstance(sample_started, bool)
            and isinstance(sample_started, (int, float))
            and math.isfinite(float(sample_started))
            and float(sample_started) >= float(policy["ramp_completion_deadline_seconds"])
            and not isinstance(sample_finished, bool)
            and isinstance(sample_finished, (int, float))
            and math.isfinite(float(sample_finished))
            and float(sample_finished) > float(sample_started)
            and float(sample_finished) <= campaign_duration + 0.001
        )
        if (
            not fixed_window_valid
            or not math.isclose(
                float(seconds),
                float(sample_finished) - float(sample_started),
                rel_tol=1e-6,
                abs_tol=0.001,
            )
        ):
            errors.append(f"{prefix}:fixed_target_window")
        if fixed_window_valid:
            if float(sample_started) < previous_window_finished - 0.001:
                errors.append(f"{prefix}:overlapping_window")
            previous_window_finished = max(
                previous_window_finished,
                float(sample_finished),
            )
        if sample.get("maintenance_window") is True:
            if sample.get("maintenance_reason") not in ALLOWED_MAINTENANCE_REASONS:
                errors.append(f"{prefix}:maintenance_reason")
            elif fixed_window_valid:
                derived_maintenance_seconds += float(seconds)
            continue
        active_workers = int(sample.get("active_workers") or 0)
        throughput = float(sample.get("operations_per_minute") or 0.0)
        effective_ratio = float(sample.get("effective_load_ratio") or 0.0)
        measurement = sample.get("worker_measurement")
        native = measurement.get("native") if isinstance(measurement, Mapping) else None
        native_valid, native_p10 = _validated_native_worker_count(
            native,
            configured_workers=32,
        )
        measurement_valid = bool(
            isinstance(measurement, Mapping)
            and measurement.get("method") == "native_inflight_operation_counter_time_samples"
            and native_valid
            and native_p10 == active_workers
            and int(measurement.get("measured_active_workers") or -1) == active_workers
            and measurement.get("configured_concurrency_not_used_as_measurement") is True
        )
        at_target = bool(
            int(sample.get("scheduled_load_level") or 0) == 32
            and active_workers >= int(policy["minimum_active_workers_at_32"])
            and baseline_value > 0
            and throughput >= baseline_value * float(policy["minimum_baseline_throughput_ratio"])
            and effective_ratio >= float(policy["minimum_effective_operation_ratio"])
            and not str(sample.get("degradation_reason") or "")
            and sample.get("round_ok") is True
            and measurement_valid
        )
        if sample.get("at_target_load") is not at_target:
            errors.append(f"{prefix}:at_target_mismatch")
        if not measurement_valid:
            errors.append(f"{prefix}:worker_measurement")
        if at_target and fixed_window_valid:
            derived_target_seconds += float(seconds)
    summary = evidence.get("target_load_summary")
    if not isinstance(summary, Mapping):
        errors.append("target_load_summary")
        summary = {}
    if summary.get("ok") is not True:
        errors.append("target_load_summary_ok")
    coverage = summary.get("target_load_coverage")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or float(coverage) < 0.90:
        errors.append("target_load_coverage")
    if summary.get("invalid_samples") not in ([], ()):  # strict, no malformed windows
        errors.append("invalid_samples")
    expected_post_ramp_wall = float(policy["minimum_post_ramp_seconds"])
    derived_eligible_wall = max(0.0, expected_post_ramp_wall - derived_maintenance_seconds)
    if not math.isclose(
        float(summary.get("post_ramp_wall_seconds") or 0.0),
        expected_post_ramp_wall,
        rel_tol=1e-6,
        abs_tol=0.001,
    ):
        errors.append("post_ramp_wall_seconds_derivation")
    if not math.isclose(
        float(summary.get("maintenance_seconds_excluded") or 0.0),
        derived_maintenance_seconds,
        rel_tol=1e-6,
        abs_tol=0.001,
    ):
        errors.append("maintenance_seconds_derivation")
    eligible_wall = float(summary.get("eligible_post_ramp_wall_seconds") or 0.0)
    if not math.isclose(
        eligible_wall,
        derived_eligible_wall,
        rel_tol=1e-6,
        abs_tol=0.001,
    ):
        errors.append("eligible_post_ramp_wall_seconds_derivation")
    if eligible_wall + 1.0 < float(policy["minimum_post_ramp_seconds"]):
        errors.append("minimum_post_ramp_seconds")
    derived_coverage = derived_target_seconds / eligible_wall if eligible_wall > 0 else 0.0
    if not math.isclose(
        float(summary.get("target_load_seconds") or 0.0),
        derived_target_seconds,
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        errors.append("target_load_seconds_derivation")
    if not math.isclose(
        float(summary.get("target_load_coverage") or 0.0),
        derived_coverage,
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        errors.append("target_load_coverage_derivation")
    if evidence.get("ok") is not True:
        errors.append("effective_load_ok")
    return {"required": True, "ok": not errors, "errors": sorted(set(errors))}


def git_metadata() -> dict[str, Any]:
    def output(*args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(ROOT), *args],
                text=True,
                capture_output=True,
                timeout=15,
                check=True,
            )
            return completed.stdout.strip()
        except Exception:
            return ""

    status = output("status", "--porcelain", "--untracked-files=all")
    return {
        "target_commit": output("rev-parse", "HEAD"),
        "target_branch": output("rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_dirty": bool(status),
        "worktree_change_count": len(status.splitlines()),
    }


def source_manifest() -> dict[str, str]:
    paths: set[Path] = {Path(__file__).resolve(), LAUNCHER.resolve()}
    for base in (
        ROOT / ".github",
        ROOT / "deploy",
        ROOT / "hooks",
        ROOT / "routes",
        ROOT / "services",
        ROOT / "scripts",
        ROOT / "public",
        ROOT / "tests",
        ROOT / "workflows",
    ):
        if not base.exists():
            continue
        for pattern in ("*.py", "*.sh", "*.js", "*.html", "*.css", "*.json", "*.yml", "*.yaml", "*.sql"):
            paths.update(path.resolve() for path in base.rglob(pattern) if path.is_file())
    for pattern in ("*.py", "*.sh", "*.sql", "requirements*.txt", "pytest.ini"):
        paths.update(path.resolve() for path in ROOT.glob(pattern) if path.is_file())
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
        if path.exists() and path.is_file()
    }


def manifest_digest(manifest: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(manifest.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def manifest_drift(expected: dict[str, str]) -> dict[str, dict[str, str]]:
    current = source_manifest()
    return {
        name: {"expected": expected.get(name, "missing"), "actual": current.get(name, "missing")}
        for name in sorted(set(expected) | set(current))
        if expected.get(name) != current.get(name)
    }


@dataclass(frozen=True)
class Credentials:
    root: str
    manager: str
    test: str
    member: str

    @classmethod
    def load(cls, *, managed_servers: bool) -> "Credentials":
        names = {
            "root": "HACKME_CAMPAIGN_ROOT_PASSWORD",
            "manager": "HACKME_CAMPAIGN_MANAGER_PASSWORD",
            "test": "HACKME_CAMPAIGN_TEST_PASSWORD",
            "member": "HACKME_CAMPAIGN_MEMBER_PASSWORD",
        }
        values = {key: str(os.environ.get(name) or "") for key, name in names.items()}
        if not managed_servers:
            missing = [name for key, name in names.items() if not values[key]]
            if missing:
                raise ValueError("existing targets require credential environment variables: " + ", ".join(missing))
        for key in values:
            values[key] = values[key] or f"Campaign-{key}-{secrets.token_urlsafe(24)}"
        return cls(**values)

    def child_env(self) -> dict[str, str]:
        return {
            "HACKME_PROBE_ROOT_PASSWORD": self.root,
            "HACKME_PROBE_MANAGER_PASSWORD": self.manager,
            "HACKME_PROBE_USER_PASSWORD": self.test,
            "HACKME_ROOT_PASSWORD": self.root,
            "HACKME_MANAGER_PASSWORD": self.manager,
            "HACKME_TEST_PASSWORD": self.test,
            "PLAYWRIGHT_ROOT_PASSWORD": self.root,
            "PLAYWRIGHT_MANAGER_PASSWORD": self.manager,
            "PLAYWRIGHT_TEST_PASSWORD": self.test,
            "PENTEST_ROOT_PASSWORD": self.root,
            "PENTEST_MANAGER_PASSWORD": self.manager,
            "PENTEST_TEST_PASSWORD": self.test,
            "PENTEST_STRESS_USER_PASSWORD": self.member,
            "HACKME_QA_ROOT_PASSWORD": self.root,
            "HACKME_QA_TEST_PASSWORD": self.test,
            "HACKME_TRADING_PROBE_USER_PASSWORD": self.member,
        }


class WebClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
        progress_callback: Callable[[str], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.progress_callback = progress_callback
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""

    def _publish_request_progress(self, detail: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(detail)

    def refresh_csrf(self) -> dict[str, Any]:
        try:
            response = self.session.get(f"{self.base_url}/api/csrf-token", timeout=self.timeout)
        except Exception as exc:
            self._publish_request_progress(f"csrf_request_error:{exc.__class__.__name__}")
            raise
        self._publish_request_progress(f"csrf_request_completed:{response.status_code}")
        body = response.json() if response.content else {}
        self.csrf = str(body.get("csrf_token") or self.session.cookies.get("csrf_token") or "")
        return {"ok": response.status_code == 200 and bool(self.csrf), "status": response.status_code}

    def login(self) -> dict[str, Any]:
        self.refresh_csrf()
        login = self.request(
            "POST",
            "/api/login",
            json_body={"username": self.username, "password": self.password},
            retry_login=False,
        )
        if not login.get("ok"):
            return login
        rotated = self.refresh_csrf()
        if not rotated.get("ok"):
            return {
                "ok": False,
                "status": int(rotated.get("status") or 0),
                "error": "authenticated_csrf_rotation_failed",
            }
        login["authenticated_csrf_rotated"] = True
        return login

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry_login: bool = True,
    ) -> dict[str, Any]:
        method = method.upper()
        headers: dict[str, str] = {}
        if method not in {"GET", "HEAD", "OPTIONS"}:
            if not self.csrf:
                self.refresh_csrf()
            headers["X-CSRF-Token"] = self.csrf
        started = time.perf_counter()
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            self._publish_request_progress(
                f"request_completed:{method}:{response.status_code}"
            )
            if response.status_code == 401 and retry_login:
                self.login()
                return self.request(method, path, json_body=json_body, params=params, retry_login=False)
            try:
                body: Any = response.json()
            except Exception:
                body = {"raw": response.text[:1000]}
            return {
                "ok": 200 <= response.status_code < 300 and (not isinstance(body, dict) or body.get("ok") is not False),
                "status": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "body": body,
            }
        except Exception as exc:
            self._publish_request_progress(f"request_error:{method}:{exc.__class__.__name__}")
            return {
                "ok": False,
                "status": 0,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
            }


class ServerController:
    def __init__(
        self,
        *,
        name: str,
        run_root: Path,
        port: int,
        credentials: Credentials,
        workers: int,
        threads: int,
        planned_outage: threading.Event,
        security: str = "off",
        server_mode: str = "dev_ready",
        strict_readiness: bool = False,
        process_registry: Any | None = None,
        process_role: str = "",
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.name = name
        self.run_root = validate_tmp_path(run_root, label=f"{name} run root")
        self.runtime_root = self.run_root / "runtime"
        self.port = int(port)
        self.base_url = f"https://127.0.0.1:{self.port}"
        self.credentials = credentials
        self.workers = max(1, int(workers))
        self.threads = max(1, int(threads))
        self.planned_outage = planned_outage
        self.security = str(security)
        self.server_mode = str(server_mode)
        self.strict_readiness = bool(strict_readiness)
        self.process_registry = process_registry
        self.process_role = str(process_role or name)
        self.progress_callback = progress_callback
        self.registered_identity: Any | None = None
        self.launch_count = 0
        self.events: list[dict[str, Any]] = []

    def _report_progress(self, detail: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(f"{self.name}:{detail}")

    def _launcher_observation(
        self,
        process: subprocess.Popen[Any],
        log: Path,
    ) -> tuple[Any, ...]:
        """Return only externally observable launcher progress indicators."""

        rows = proc_rows()
        tree = descendants(rows, process.pid)
        tree_cpu_ticks = sum(int(rows.get(pid, {}).get("cpu_ticks") or 0) for pid in tree)
        try:
            log_stat = log.stat()
            log_state = (int(log_stat.st_size), int(log_stat.st_mtime_ns))
        except OSError:
            log_state = (0, 0)
        try:
            pid_stat = self.pid_file.stat()
            pid_file_state = (int(pid_stat.st_size), int(pid_stat.st_mtime_ns), self.pid())
        except OSError:
            pid_file_state = (0, 0, 0)
        return (
            process.poll(),
            tuple(sorted(tree)),
            tree_cpu_ticks,
            log_state,
            pid_file_state,
        )

    def _wait_launcher(
        self,
        process: subprocess.Popen[Any],
        log: Path,
        *,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Poll launcher progress without manufacturing watchdog heartbeats."""

        started = time.monotonic()
        deadline = started + max(0.1, float(timeout))
        last_observation: tuple[Any, ...] | None = None
        observations = 0
        while True:
            observation = self._launcher_observation(process, log)
            if observation != last_observation:
                observations += 1
                last_observation = observation
                self._report_progress(f"launcher_observed_progress:{observations}")
            returncode = process.poll()
            if returncode is not None:
                self._report_progress(f"launcher_completed:{returncode}")
                return {
                    "returncode": int(returncode),
                    "timed_out": False,
                    "observations": observations,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            now = time.monotonic()
            if now >= deadline:
                terminate_process_group(process, grace_seconds=2.0)
                try:
                    returncode = int(process.wait(timeout=5))
                except subprocess.TimeoutExpired:
                    returncode = 124
                return {
                    "returncode": returncode if returncode != 0 else 124,
                    "timed_out": True,
                    "observations": observations,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            time.sleep(min(0.5, max(0.01, deadline - now)))

    @property
    def pid_file(self) -> Path:
        return self.runtime_root / "server.pid"

    def pid(self) -> int:
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Capacity recommendations may point at mutable files outside the
        # frozen repository.  A supervised campaign supplies its exact
        # gunicorn profile on argv, so inherited redirects must never be able
        # to rewrite that profile before launcher argument processing.
        env.pop("HACKME_DEV_CAPACITY_DEFAULTS_FILE", None)
        env.pop("HACKME_DEV_CAPACITY_REPORT_FILE", None)
        env.update({
            "ROOT_PASSWORD": self.credentials.root,
            "MANAGER_PASSWORD": self.credentials.manager,
            "TEST_PASSWORD": self.credentials.test,
            "PYTHONPYCACHEPREFIX": str(self.run_root / "pycache"),
            "HACKME_MEDIA_REALTIME_PROXY_ENABLED": "1",
            "HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT": "2",
            "HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE": "host",
            "HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR": str(self.run_root / "locks" / "realtime_proxy"),
            "HACKME_DEV_BACKTEST_PROBE_ON_STARTUP": "0",
            "HACKME_DEV_BTC_TRADE_AUTOSTART": "0",
            "HACKME_DEV_USE_CAPACITY_DEFAULTS": "0",
        })
        return env

    def launcher_command(self) -> list[str]:
        command = [
            str(LAUNCHER),
            "--cli",
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--port-conflict", "fail",
            "--feature-mode", "all",
            "--security", self.security,
            "--server-mode", self.server_mode,
            "--server-runner", "gunicorn",
            "--gunicorn-workers", str(self.workers),
            "--gunicorn-threads", str(self.threads),
            "--gunicorn-timeout", "900",
            "--gunicorn-max-requests", "10000",
            "--no-capacity-probe",
            "--no-hls-slot-probe",
            "--no-btc-trade-autostart",
            "--max-content-mb", "4096",
            "--run-root", str(self.run_root),
            "--runtime-root", str(self.runtime_root),
            "--in-place",
            "--tmp-runtime",
            "--skip-install",
        ]
        if self.server_mode == "dev_ready":
            command.append("--trading-background-dev-ready")
        return command

    def wait_ready(self, *, timeout: float = 180.0) -> dict[str, Any]:
        if self.strict_readiness:
            session = requests.Session()
            original_request = session.request

            def progress_request(method: str, url: str, **kwargs: Any) -> requests.Response:
                try:
                    response = original_request(method, url, **kwargs)
                except BaseException as exc:
                    self._report_progress(
                        f"readiness_request_completed:{str(method).upper()}:error:{exc.__class__.__name__}"
                    )
                    raise
                self._report_progress(
                    f"readiness_request_completed:{str(method).upper()}:{response.status_code}"
                )
                return response

            session.request = progress_request  # type: ignore[method-assign]
            probe = LayeredReadinessProbe(ReadinessConfig(
                base_url=self.base_url,
                username="root",
                password=self.credentials.root,
                runtime_root=self.runtime_root,
                request_timeout_seconds=15,
                async_timeout_seconds=min(120.0, max(30.0, timeout - 30.0)),
            ), session=session)
            deadline = time.monotonic() + max(0.1, float(timeout))
            attempts: list[dict[str, Any]] = []
            consecutive = 0
            while time.monotonic() < deadline:
                attempt = probe.probe_once()
                attempts.append(attempt)
                consecutive = consecutive + 1 if attempt.get("overall") else 0
                self._report_progress(
                    f"readiness_probe_completed:{len(attempts)}:{int(bool(attempt.get('overall')))}"
                )
                if consecutive >= 2:
                    break
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            result = {
                "schema_version": "hackme.layered-readiness.v1",
                "overall": consecutive >= 2,
                "consecutive_passes": consecutive,
                "required_consecutive_passes": 2,
                "attempts": attempts,
                "final": attempts[-1] if attempts else {},
            }
            return {
                "ok": bool(result.get("overall")),
                "layered": result,
                "elapsed_seconds": sum(float((row or {}).get("elapsed_seconds") or 0.0) for row in result.get("attempts") or []),
            }
        deadline = time.monotonic() + timeout
        attempts = 0
        started = time.monotonic()
        last_error = ""
        while time.monotonic() < deadline:
            attempts += 1
            try:
                response = requests.get(f"{self.base_url}/api/version", verify=False, timeout=5)
                self._report_progress(
                    f"readiness_request_completed:GET:{response.status_code}"
                )
                if response.status_code == 200:
                    return {
                        "ok": True,
                        "attempts": attempts,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "status": response.status_code,
                    }
                last_error = f"status={response.status_code}"
            except Exception as exc:
                self._report_progress(
                    f"readiness_request_completed:GET:error:{exc.__class__.__name__}"
                )
                last_error = f"{exc.__class__.__name__}: {exc}"
            time.sleep(0.5)
        return {
            "ok": False,
            "attempts": attempts,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": last_error,
        }

    def start(self) -> dict[str, Any]:
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.launch_count += 1
        log = self.run_root / "campaign" / f"launcher_{self.launch_count:03d}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        command = self.launcher_command()
        with log.open("w", encoding="utf-8") as output_handle:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                env=self._env(),
                stdout=output_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            launcher_identity: Any | None = None
            try:
                if self.process_registry is not None:
                    launcher_identity = self.process_registry.register(
                        self.process_role,
                        process.pid,
                        required=True,
                    )
                completed = self._wait_launcher(process, log, timeout=300.0)
            except Exception:
                terminate_process_group(process, grace_seconds=2.0)
                raise
            finally:
                if self.process_registry is not None and launcher_identity is not None:
                    self.process_registry.unregister(
                        self.process_role, launcher_identity
                    )
        launcher_log = bounded_launcher_log_snapshot(
            log,
            {
                "root": self.credentials.root,
                "manager": self.credentials.manager,
                "test": self.credentials.test,
                "member": self.credentials.member,
            },
            progress_callback=self._report_progress,
        )
        leaked = list(launcher_log["secret_leak_labels"])
        launcher_evidence_ok = bool(launcher_log.get("ok"))
        ready = (
            self.wait_ready()
            if completed["returncode"] == 0 and not leaked and launcher_evidence_ok
            else {
                "ok": False,
                "error": "launcher_failed_secret_leak_or_log_snapshot_invalid",
            }
        )
        event = {
            "action": "start",
            "name": self.name,
            "at": utc_now(),
            "returncode": completed["returncode"],
            "launcher_timed_out": completed["timed_out"],
            "launcher_progress_observations": completed["observations"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "pid": self.pid(),
            "ready": ready,
            "secret_leak_labels": leaked,
            "log": str(log),
            "launcher_log_bytes": launcher_log.get("initial_bytes"),
            "launcher_log_scanned_bytes": launcher_log.get("scanned_bytes"),
            "launcher_log_diagnostic_truncated": launcher_log.get("diagnostic_truncated"),
            "launcher_log_tail": launcher_log.get("diagnostic_tail"),
            "launcher_log_errors": launcher_log.get("errors"),
            "command": sanitized_command(command),
            "ok": (
                completed["returncode"] == 0
                and not leaked
                and launcher_evidence_ok
                and bool(ready.get("ok"))
                and self.pid() > 0
            ),
        }
        if event["ok"]:
            self.planned_outage.clear()
            if self.process_registry is not None:
                if self.registered_identity is not None:
                    self.process_registry.unregister(self.process_role, self.registered_identity)
                self.registered_identity = self.process_registry.register(
                    self.process_role,
                    int(event["pid"]),
                    required=True,
                )
        self.events.append(event)
        return event

    def _pid_matches_runtime(self, pid: int) -> bool:
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except Exception:
            return False
        expected = f"HACKME_RUNTIME_DIR={self.runtime_root}".encode()
        return expected in environ

    def stop(self, *, reason: str = "campaign") -> dict[str, Any]:
        self.planned_outage.set()
        pid = self.pid()
        started = time.monotonic()
        event: dict[str, Any] = {"action": "stop", "name": self.name, "at": utc_now(), "pid": pid, "reason": reason}
        self._report_progress(f"stop_started:{reason}")

        def process_group_alive(pgid: int) -> bool:
            if pgid <= 0:
                return False
            try:
                os.killpg(pgid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True

        try:
            if pid > 0 and Path(f"/proc/{pid}").exists():
                if not self._pid_matches_runtime(pid):
                    raise RuntimeError(f"refusing to stop pid {pid}: runtime ownership mismatch")
                pgid = os.getpgid(pid)
                event["pgid"] = pgid
                os.killpg(pgid, signal.SIGTERM)
                self._report_progress(f"stop_sigterm_sent:{reason}:{pgid}")
                deadline = time.monotonic() + 20
                next_progress_at = time.monotonic() + 2.0
                while time.monotonic() < deadline and process_group_alive(pgid):
                    if time.monotonic() >= next_progress_at:
                        self._report_progress(f"stop_waiting_for_process_group:{reason}:{pgid}")
                        next_progress_at = time.monotonic() + 2.0
                    time.sleep(0.2)
                if process_group_alive(pgid):
                    os.killpg(pgid, signal.SIGKILL)
                    self._report_progress(f"stop_sigkill_sent:{reason}:{pgid}")
                    kill_deadline = time.monotonic() + 5.0
                    while time.monotonic() < kill_deadline and process_group_alive(pgid):
                        time.sleep(0.1)
                process_group_remaining = process_group_alive(pgid)
            else:
                process_group_remaining = False
            master_remaining = bool(pid > 0 and Path(f"/proc/{pid}").exists())
            event.update({
                "master_process_remaining": master_remaining,
                "process_group_remaining": process_group_remaining,
                "ok": not master_remaining and not process_group_remaining,
            })
        except Exception as exc:
            event.update({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
        event["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if event.get("ok") and self.process_registry is not None and self.registered_identity is not None:
            self.process_registry.unregister(self.process_role, self.registered_identity)
            self.registered_identity = None
        self._report_progress(f"stop_completed:{reason}:{int(bool(event.get('ok')))}")
        self.events.append(event)
        return event

    def restart(self, *, reason: str) -> dict[str, Any]:
        started = time.monotonic()
        stopped = self.stop(reason=reason)
        start = self.start() if stopped.get("ok") else {"ok": False, "error": "stop_failed"}
        self.planned_outage.clear()
        result = {
            "action": "restart",
            "name": self.name,
            "reason": reason,
            "stopped": stopped,
            "started": start,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "ok": bool(stopped.get("ok") and start.get("ok")),
        }
        self.events.append(result)
        return result


def proc_rows() -> dict[int, dict[str, int]]:
    rows: dict[int, dict[str, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status: dict[str, str] = {}
            for line in (entry / "status").read_text(encoding="utf-8", errors="ignore").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
            stat_tail = (entry / "stat").read_text(encoding="utf-8", errors="ignore").rsplit(") ", 1)[1].split()
            rows[int(entry.name)] = {
                "ppid": int(status.get("PPid", "0").split()[0]),
                "rss_kb": int(status.get("VmRSS", "0 kB").split()[0]),
                "threads": int(status.get("Threads", "0").split()[0]),
                "cpu_ticks": int(stat_tail[11]) + int(stat_tail[12]),
            }
        except Exception:
            continue
    return rows


def descendants(rows: dict[int, dict[str, int]], root_pid: int) -> set[int]:
    found = {root_pid} if root_pid in rows else set()
    changed = True
    while changed:
        changed = False
        for pid, row in rows.items():
            if pid not in found and row["ppid"] in found:
                found.add(pid)
                changed = True
    return found


class ResourceMonitor(threading.Thread):
    def __init__(self, controllers: list[ServerController], out: Path, *, interval: float):
        super().__init__(daemon=True)
        self.controllers = controllers
        self.out = out
        self.interval = max(1.0, float(interval))
        self.stop_event = threading.Event()
        self.samples: list[dict[str, Any]] = []

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        session = requests.Session()
        session.verify = False
        with self.out.open("a", encoding="utf-8") as handle:
            while not self.stop_event.is_set():
                processes = proc_rows()
                sample: dict[str, Any] = {"at": utc_now(), "monotonic": round(time.monotonic(), 3), "servers": {}}
                try:
                    load = Path("/proc/loadavg").read_text(encoding="utf-8").split()
                    mem = {}
                    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                        if ":" in line:
                            key, value = line.split(":", 1)
                            mem[key] = int(value.strip().split()[0])
                    sample["host"] = {
                        "load1": float(load[0]),
                        "load5": float(load[1]),
                        "load15": float(load[2]),
                        "mem_available_kb": int(mem.get("MemAvailable", 0)),
                        "mem_total_kb": int(mem.get("MemTotal", 0)),
                    }
                except Exception as exc:
                    sample["host"] = {"error": str(exc)}
                for controller in self.controllers:
                    pid = controller.pid()
                    tree = descendants(processes, pid)
                    started = time.perf_counter()
                    try:
                        response = session.get(f"{controller.base_url}/api/version", timeout=5)
                        health_status = response.status_code
                        health_error = ""
                    except Exception as exc:
                        health_status = 0
                        health_error = f"{exc.__class__.__name__}: {exc}"
                    database_dir = controller.runtime_root / "database"
                    db_sizes = {
                        path.name: path.stat().st_size
                        for path in database_dir.glob("*.db*")
                        if path.is_file()
                    } if database_dir.exists() else {}
                    sample["servers"][controller.name] = {
                        "pid": pid,
                        "process_count": len(tree),
                        "rss_kb": sum(processes[item]["rss_kb"] for item in tree),
                        "threads": sum(processes[item]["threads"] for item in tree),
                        "cpu_ticks": sum(processes[item]["cpu_ticks"] for item in tree),
                        "health_status": health_status,
                        "health_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                        "health_error": health_error,
                        "planned_outage": controller.planned_outage.is_set(),
                        "database_sizes": db_sizes,
                    }
                self.samples.append(sample)
                handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                self.stop_event.wait(self.interval)

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {"samples": len(self.samples), "servers": {}}
        for controller in self.controllers:
            rows = [(sample.get("servers") or {}).get(controller.name) or {} for sample in self.samples]
            latencies = [float(row.get("health_elapsed_ms") or 0) for row in rows if int(row.get("health_status") or 0) == 200]
            unplanned = [row for row in rows if int(row.get("health_status") or 0) != 200 and not row.get("planned_outage")]
            result["servers"][controller.name] = {
                "samples": len(rows),
                "health_200": sum(1 for row in rows if int(row.get("health_status") or 0) == 200),
                "unplanned_health_failures": len(unplanned),
                "unplanned_failure_samples": unplanned[:20],
                "health_latency_ms": {
                    "p50": percentile(latencies, 0.50),
                    "p95": percentile(latencies, 0.95),
                    "p99": percentile(latencies, 0.99),
                    "max": round(max(latencies), 3) if latencies else 0.0,
                },
                "max_rss_mb": round(max((int(row.get("rss_kb") or 0) for row in rows), default=0) / 1024, 3),
                "max_threads": max((int(row.get("threads") or 0) for row in rows), default=0),
                "max_processes": max((int(row.get("process_count") or 0) for row in rows), default=0),
            }
        available = [int((sample.get("host") or {}).get("mem_available_kb") or 0) for sample in self.samples]
        result["host"] = {
            "minimum_mem_available_mb": round(min((value for value in available if value > 0), default=0) / 1024, 3),
            "maximum_load1": max((float((sample.get("host") or {}).get("load1") or 0) for sample in self.samples), default=0.0),
        }
        return result


def terminate_process_group(process: subprocess.Popen[Any], *, grace_seconds: float = 10.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.1, grace_seconds)
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    category: str
    target: str
    fraction: float
    runner: Callable[[], dict[str, Any]]
    mandatory: bool = True


class Campaign:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = validate_tmp_path(Path(args.campaign_root), label="campaign root")
        self.reports = self.root / "reports"
        self.supervised = bool(args.supervised)
        self.campaign_uuid = str(args.campaign_uuid or os.environ.get("HACKME_CAMPAIGN_UUID") or "")
        self.control_root = (
            validate_control_root(self.root, Path(args.control_root))
            if args.control_root
            else self.root
        )
        self.state_path = Path(args.state_path).resolve(strict=False) if args.state_path else self.root / "checkpoint" / "campaign.state.json"
        self.control_path = Path(args.control_path).resolve(strict=False) if args.control_path else self.root / "checkpoint" / "campaign.control.json"
        self.heartbeat_path = Path(args.heartbeat_path).resolve(strict=False) if args.heartbeat_path else self.root / "checkpoint" / "campaign.heartbeat.json"
        self.checkpoint_path = Path(args.checkpoint_path).resolve(strict=False) if args.checkpoint_path else self.root / "checkpoint" / "campaign.checkpoint.json"
        self.checkpoint_mirror_path = (
            Path(args.checkpoint_mirror_path).resolve(strict=False)
            if args.checkpoint_mirror_path
            else None
        )
        self.watchdog_ready_path = self.control_root / "checkpoint" / "watchdog.status.json"
        self.supervisor_contract_path = Path(args.supervisor_contract).resolve(strict=False) if args.supervisor_contract else Path()
        self.activation_gate_path = Path(args.activation_gate).resolve(strict=False) if args.activation_gate else Path()
        self.source_freeze_path = Path(args.source_freeze_path).resolve(strict=False) if args.source_freeze_path else Path()
        self.supervisor_contract = load_json(self.supervisor_contract_path) if self.supervised else {}
        self.campaign_level = str(self.supervisor_contract.get("level") or ("smoke" if args.allow_short_duration else "formal"))
        self.cgroup_path = str(args.cgroup_path or "")
        self.state_machine = CampaignStateMachine(self.state_path) if self.supervised else None
        self.checkpoint_revision = 1
        self.final_path = self.reports / "operational_campaign_24h.json"
        self.credentials = Credentials.load(managed_servers=True)
        self.primary_outage = threading.Event()
        self.recovery_outage = threading.Event()
        self.security_outage = threading.Event()
        required_roles = (
            MANDATORY_MANAGED_ROLES
            if self.supervised and self.campaign_level in {"rehearsal", "formal"}
            else SMOKE_REQUIRED_MANAGED_ROLES
            if self.supervised
            else ()
        )
        self.process_registry = ProcessRoleRegistry(
            expected_cgroup=self.cgroup_path if self.supervised else "",
            required_roles=required_roles,
        )
        self.runner_role_identity: Any | None = None
        if self.supervised:
            self.runner_role_identity = self.process_registry.register(
                "scenario", os.getpid(), required=True
            )
        self.lock = threading.RLock()
        self.main_thread_ident = threading.get_ident()
        self.main_progress_revision = 0
        self.main_progress_monotonic_ns = 0
        self.main_progress_phase = "runner_initializing"
        self.active_event = threading.Event()
        self.stop_event = threading.Event()
        self.heartbeat_pump_stop = threading.Event()
        primary_port = int(args.primary_port or free_port())
        recovery_port = int(args.recovery_port or free_port())
        if primary_port == recovery_port:
            recovery_port = free_port()
        security_port = int(args.security_port or free_port())
        while security_port in {primary_port, recovery_port}:
            security_port = free_port()
        self.primary = ServerController(
            name="primary",
            run_root=self.root / "primary",
            port=primary_port,
            credentials=self.credentials,
            workers=args.workers,
            threads=args.threads,
            planned_outage=self.primary_outage,
            strict_readiness=self.supervised,
            process_registry=self.process_registry if self.supervised else None,
            process_role="primary",
            progress_callback=self._server_progress if self.supervised else None,
        )
        self.recovery = ServerController(
            name="recovery",
            run_root=self.root / "recovery",
            port=recovery_port,
            credentials=self.credentials,
            workers=max(2, args.workers // 2),
            threads=args.threads,
            planned_outage=self.recovery_outage,
            strict_readiness=self.supervised,
            process_registry=self.process_registry if self.supervised else None,
            process_role="recovery",
            progress_callback=self._server_progress if self.supervised else None,
        )
        self.security_sentinel = ServerController(
            name="security_sentinel",
            run_root=self.root / "security_sentinel",
            port=security_port,
            credentials=self.credentials,
            workers=max(2, args.workers // 2),
            threads=args.threads,
            planned_outage=self.security_outage,
            security="on",
            server_mode="production",
            strict_readiness=False,
            process_registry=self.process_registry if self.supervised else None,
            process_role="security_sentinel",
            progress_callback=self._server_progress if self.supervised else None,
        )
        self.heartbeat_pump_thread: threading.Thread | None = None
        self.heartbeat_phase = "runner_initializing"
        self.heartbeat_pump_error = ""
        self.active_started = 0.0
        self.active_started_at = ""
        self.scenario_results: dict[str, dict[str, Any]] = {}
        self.scenario_threads: list[threading.Thread] = []
        self.step_processes: set[subprocess.Popen[Any]] = set()
        self.accounts: list[tuple[str, str]] = []
        self.account_inventory: list[dict[str, Any]] = []
        self.account_cleanup: dict[str, Any] = {}
        self.source_hashes = source_manifest()
        self.source_digest = manifest_digest(self.source_hashes)
        self.source_git = git_metadata()
        self.drift: dict[str, dict[str, str]] = {}
        self.source_freezer: GitSourceFreezer | None = None
        if self.supervised:
            self.source_freezer = GitSourceFreezer(ROOT, self.root / "artifacts" / "source")
            self.source_freezer.load_baseline(Path(args.source_freeze_path))
        self.core_process: subprocess.Popen[Any] | None = None
        self.core_identity: Any | None = None
        self.core_stdout_handle: Any = None
        self.core_command: list[str] = []
        self.core_root = self.root / "core_soak"
        self.core_report = self.core_root / "operational_soak.json"
        self.core_stop_file = self.core_root / "campaign_load.stop.json"
        self.core_activation_dir = self.core_root / "activation"
        self.core_ready_file = self.core_activation_dir / "core_soak.ready.json"
        self.core_activation_file = self.core_activation_dir / "core_soak.activation.json"
        self.core_activation_ack_file = self.core_activation_dir / "core_soak.activation_ack.json"
        self.core_activation_nonce = secrets.token_hex(32)
        self.core_profile_digest = canonical_digest(
            SUPERVISED_RUNNER_PROFILES.get(self.campaign_level, {})
        )
        self.core_process_started_monotonic_ns = 0
        self.core_ready_evidence: dict[str, Any] = {}
        self.core_activation_evidence: dict[str, Any] = {}
        if self.supervised:
            comfyui_url = str(os.environ.get("HACKME_CAMPAIGN_COMFYUI_API_URL") or "").rstrip("/")
            collector = StructuredResourceCollector(StructuredResourceCollectorConfig(
                cgroup_path=Path("/sys/fs/cgroup") / self.cgroup_path.lstrip("/"),
                sample_path=self.reports / "resources" / "resource_samples.jsonl",
                runtime_roots={
                    "primary": self.primary.runtime_root,
                    "recovery": self.recovery.runtime_root,
                    "security_sentinel": self.security_sentinel.runtime_root,
                },
                campaign_data_root=self.root,
                process_registry=self.process_registry,
                require_gpu=not bool(args.allow_short_duration),
                comfyui_queue_url=f"{comfyui_url}/queue" if comfyui_url else "",
                require_comfyui_queue=not bool(args.allow_short_duration),
                minimum_disk_free_bytes=int(args.minimum_free_gb * 1024**3),
                expected_process_cgroup=self.cgroup_path,
                cgroup_event_baseline=(
                    self.supervisor_contract.get("cgroup_event_baseline")
                    if isinstance(
                        self.supervisor_contract.get("cgroup_event_baseline"),
                        Mapping,
                    )
                    else {}
                ),
                health_targets={
                    "primary": self.primary.base_url,
                    "recovery": self.recovery.base_url,
                    "security_sentinel": self.security_sentinel.base_url,
                },
            ))

            def hard_stop_from_resource(sample: dict[str, Any]) -> None:
                hard_limit = sample.get("hard_limit_state") or {}
                reasons = hard_limit.get("tripped") or ["RESOURCE_COLLECTOR_FAILED"]
                self.request_hard_stop(
                    reason=str(reasons[0]),
                    classification="FAIL_INFRA",
                    evidence={"resource_sample": sample},
                )

            self.resource_monitor = StructuredResourceMonitor(
                collector,
                interval_seconds=args.resource_interval,
                hard_stop=hard_stop_from_resource,
            )
        else:
            self.resource_monitor = ResourceMonitor(
                [self.primary, self.recovery, self.security_sentinel],
                self.reports / "resources" / "resource_samples.jsonl",
                interval=args.resource_interval,
            )

    def base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.credentials.child_env())
        env.update({
            "PYTHONPATH": str(ROOT),
            "PYTHONPYCACHEPREFIX": str(self.root / "pycache"),
            "HACKME_TEST_ARTIFACT_ROOT": str(self.root / "test_artifacts"),
        })
        return env

    def elapsed(self) -> float:
        if self.state_machine is not None:
            try:
                return float((self.state_machine.snapshot().get("clock") or {}).get("continuous_active_seconds") or 0.0)
            except Exception:
                return 0.0
        return max(0.0, time.monotonic() - self.active_started) if self.active_started else 0.0

    def required_duration_completed(self) -> bool:
        return self.elapsed() + 1 >= int(self.args.duration_seconds)

    def check_drift(self) -> dict[str, dict[str, str]]:
        if self.source_freezer is not None:
            result = self.source_freezer.lightweight_drift_check()
            drift = result.get("tracked_changes") or {}
            if not result.get("status_unchanged"):
                for row in (result.get("status") or {}).get("blocked_changes") or []:
                    drift[f"git_status:{row.get('path')}"] = {
                        "expected": "frozen",
                        "actual": str(row.get("status") or "changed"),
                    }
            if drift:
                with self.lock:
                    self.drift.update(drift)
            return drift
        drift = manifest_drift(self.source_hashes)
        if drift:
            with self.lock:
                self.drift.update(drift)
        return drift

    def write_checkpoint(self, phase: str) -> None:
        with self.lock:
            self.heartbeat_phase = str(phase or self.heartbeat_phase)
            self.checkpoint_revision += 1
            main_loop_progress = threading.get_ident() == self.main_thread_ident
            if main_loop_progress:
                self.main_progress_revision += 1
                self.main_progress_monotonic_ns = time.monotonic_ns()
                self.main_progress_phase = self.heartbeat_phase
            payload = {
                "schema_version": "hackme.campaign-checkpoint.v1",
                "campaign_uuid": self.campaign_uuid,
                "revision": self.checkpoint_revision,
                "status": "running",
                "phase": phase,
                "updated_at": utc_now(),
                "active_started_at": self.active_started_at,
                "active_test_seconds": round(self.elapsed(), 3),
                "required_active_test_seconds": int(self.args.duration_seconds),
                "primary": {
                    "base_url": self.primary.base_url,
                    "runtime_root": str(self.primary.runtime_root),
                    "pid": self.primary.pid(),
                },
                "recovery": {
                    "base_url": self.recovery.base_url,
                    "runtime_root": str(self.recovery.runtime_root),
                    "pid": self.recovery.pid(),
                    "planned_outage": self.recovery_outage.is_set(),
                },
                "accounts": [username for username, _password in self.accounts],
                "scenario_results": self.scenario_results,
                "source_manifest_digest": self.source_digest,
                "source_git": self.source_git,
                "source_drift": self.drift,
                "core_soak": {
                    "pid": self.core_process.pid if self.core_process else 0,
                    "returncode": self.core_process.poll() if self.core_process else None,
                    "report": str(self.core_report),
                    "ready_verified": bool(self.core_ready_evidence.get("ok")),
                    "activation_verified": bool(self.core_activation_evidence.get("ok")),
                    "activation_monotonic_ns": self.core_activation_evidence.get(
                        "activation_monotonic_ns"
                    ),
                },
                "report": str(self.final_path),
            }
            self._commit_checkpoint(payload)
            if self.state_machine is not None and main_loop_progress:
                self._persist_main_progress_heartbeat()

    def mark_main_loop_progress(self, phase: str) -> None:
        """Publish a liveness token that only the main campaign loop may move."""

        if threading.get_ident() != self.main_thread_ident:
            raise RuntimeError("main-loop progress cannot be advanced by a helper thread")
        with self.lock:
            self.main_progress_revision += 1
            self.main_progress_monotonic_ns = time.monotonic_ns()
            self.main_progress_phase = str(phase or self.main_progress_phase)

    def _server_progress(self, phase: str) -> None:
        """Bridge genuine synchronous server progress into the watchdog token."""

        self.mark_main_loop_progress(phase)
        self._persist_main_progress_heartbeat()

    def _persist_main_progress_heartbeat(self, *, update_state: bool = True) -> None:
        """Durably mirror the latest main-loop token without refreshing its age.

        The helper is allowed to rewrite this proof after fsync failures, but
        ``orchestrator_monotonic_ns`` always remains the time at which the main
        thread last advanced.  Therefore a healthy helper cannot hide a stuck
        main loop from the external 120-second watchdog.
        """

        if self.state_machine is None:
            return
        with self.lock:
            progress_ns = int(self.main_progress_monotonic_ns)
            progress_revision = int(self.main_progress_revision)
            checkpoint_revision = int(self.checkpoint_revision)
            phase = str(self.main_progress_phase)
        if progress_ns <= 0 or progress_revision <= 0:
            raise RuntimeError("main-loop progress has not been initialized")
        identity_ticks = process_start_ticks(os.getpid())
        if update_state:
            self.state_machine.heartbeat(
                orchestrator_pid=os.getpid(),
                orchestrator_start_ticks=identity_ticks,
                checkpoint_revision=checkpoint_revision,
                now_ns=progress_ns,
            )
        durable_atomic_write_json(self.heartbeat_path, {
            "schema_version": "hackme.campaign-heartbeat.v1",
            "campaign_uuid": self.campaign_uuid,
            "heartbeat": {
                "orchestrator_pid": os.getpid(),
                "orchestrator_start_ticks": identity_ticks,
                "orchestrator_monotonic_ns": progress_ns,
                "checkpoint_revision": checkpoint_revision,
                "main_progress_revision": progress_revision,
                "main_progress_phase": phase,
                "updated_at": utc_now(),
            },
        })

    def _commit_checkpoint(self, payload: Mapping[str, Any]) -> None:
        """Atomically persist and read back both volatile and reboot-safe copies."""

        durable_atomic_write_json(self.checkpoint_path, payload)
        if self.checkpoint_mirror_path is None:
            return
        mirror_parent = self.checkpoint_mirror_path.parent
        mirror_parent.mkdir(parents=True, exist_ok=True)
        os.chmod(mirror_parent, 0o700)
        durable_atomic_write_json(self.checkpoint_mirror_path, payload)
        primary = load_json(self.checkpoint_path)
        mirror = load_json(self.checkpoint_mirror_path)
        if primary != dict(payload) or mirror != dict(payload) or primary != mirror:
            raise RuntimeError("campaign checkpoint primary/mirror readback mismatch")
        if self.checkpoint_mirror_path.stat().st_mode & 0o077:
            raise RuntimeError("campaign checkpoint mirror permissions are not private")

    def start_heartbeat_pump(self) -> None:
        """Keep startup/preflight heartbeat durable before ACTIVE begins.

        Starting three isolated targets and running layered readiness can take
        longer than the production watchdog's 120 second stale threshold.  A
        dedicated runner thread only mirrors a token issued by the main
        campaign thread.  It never advances that token or its monotonic time,
        so a main-loop deadlock becomes stale even if this helper stays alive.
        """

        if not self.supervised or self.heartbeat_pump_thread is not None:
            return
        self.write_checkpoint(self.heartbeat_phase)

        def pump() -> None:
            while not self.heartbeat_pump_stop.wait(HEARTBEAT_PUMP_INTERVAL_SECONDS):
                try:
                    self._persist_main_progress_heartbeat(update_state=False)
                except Exception as exc:
                    self.heartbeat_pump_error = f"{exc.__class__.__name__}: {exc}"
                    return

        self.heartbeat_pump_thread = threading.Thread(
            target=pump,
            daemon=True,
            name="campaign-startup-heartbeat",
        )
        self.heartbeat_pump_thread.start()

    def stop_heartbeat_pump(self) -> None:
        self.heartbeat_pump_stop.set()
        thread = self.heartbeat_pump_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _write_control_from_state(self, state: Mapping[str, Any]) -> None:
        control = state.get("control") or {}
        durable_atomic_write_json(self.control_path, {
            "schema_version": "hackme.campaign-control.v1",
            "campaign_uuid": self.campaign_uuid,
            "revision": int(state.get("revision") or 0),
            "state": state.get("state"),
            "admit_new_jobs": bool(control.get("admit_new_jobs")),
            "load_generator_should_run": bool(control.get("load_generator_should_run")),
            "preserve_evidence_requested": bool(control.get("preserve_evidence_requested")),
            "reason": state.get("reason"),
            "updated_at": utc_now(),
        })

    def _active_conditions(self, *, activating: bool = False) -> dict[str, bool]:
        watchdog = load_json(self.watchdog_ready_path) if self.watchdog_ready_path.exists() else {}
        watchdog_pid = int(watchdog.get("watchdog_pid") or 0)
        watchdog_start_ticks = int(watchdog.get("watchdog_start_ticks") or 0)
        try:
            watchdog_identity_alive = bool(
                watchdog_pid > 1
                and watchdog_start_ticks > 0
                and process_start_ticks(watchdog_pid) == watchdog_start_ticks
            )
        except Exception:
            watchdog_identity_alive = False
        durable_state: Mapping[str, Any] = {}
        if self.state_machine is not None:
            try:
                durable_state = self.state_machine.snapshot()
            except Exception:
                durable_state = {}
        durable_state_name = str(durable_state.get("state") or "")
        durable_control = durable_state.get("control") or {}
        campaign_state_active = (
            durable_state_name == CampaignState.ACTIVE.value
            or (activating and durable_state_name == CampaignState.FROZEN.value)
            if self.state_machine is not None
            else True
        )
        hard_stop_value = durable_state.get("hard_stop")
        no_hard_stop = (
            campaign_state_active
            and (hard_stop_value is None or hard_stop_value is False)
            and (
                activating
                or durable_control.get("admit_new_jobs") is True
            )
            if self.state_machine is not None
            else True
        )
        latest_sample: Mapping[str, Any] = {}
        sample_fresh = True
        sampled_health: Mapping[str, Any] = {}
        if self.supervised:
            samples = list(getattr(self.resource_monitor, "samples", []) or [])
            latest_sample = samples[-1] if samples else {}
            sampled_health = (
                latest_sample.get("health")
                if isinstance(latest_sample.get("health"), Mapping)
                else {}
            )
            sampled_ns = int(latest_sample.get("monotonic_ns") or 0)
            maximum_age_seconds = max(
                15.0,
                float(self.args.resource_interval) * 2.5,
            )
            sample_fresh = bool(
                sampled_ns > 0
                and time.monotonic_ns() >= sampled_ns
                and (time.monotonic_ns() - sampled_ns) / 1_000_000_000
                <= maximum_age_seconds
            )

        def target_ready(controller: ServerController) -> bool:
            process_alive = controller.pid() > 0 and Path(
                f"/proc/{controller.pid()}"
            ).exists()
            if not self.supervised:
                return process_alive
            row = sampled_health.get(controller.name)
            return bool(
                process_alive
                and sample_fresh
                and isinstance(row, Mapping)
                and row.get("semantic_ready") is True
            )

        return {
            "source_frozen": not bool(self.drift),
            "primary_ready": target_ready(self.primary),
            "recovery_ready": target_ready(self.recovery),
            "security_sentinel_ready": target_ready(self.security_sentinel),
            "watchdog_alive": bool(
                watchdog.get("verified") is True
                and watchdog_identity_alive
                and str(watchdog.get("status") or "")
                not in {"campaign_terminal", "stopped_by_signal"}
            ),
            "monitor_alive": self.resource_monitor.is_alive(),
            "resource_sample_fresh": sample_fresh,
            "load_generator_alive": bool(self.core_process and self.core_process.poll() is None),
            "core_activation_intact": self.core_activation_artifacts_intact(),
            "no_hard_stop": no_hard_stop,
            "campaign_state_active": campaign_state_active,
        }

    def wait_for_initial_resource_sample(self, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        if not self.supervised:
            return {"ok": True, "mode": "unsupervised"}
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            samples = list(getattr(self.resource_monitor, "samples", []) or [])
            if samples:
                sample = samples[-1]
                hard_limits = sample.get("hard_limit_state") or {}
                if hard_limits.get("ok") is not True:
                    raise RuntimeError(
                        "initial resource sample tripped hard limit: "
                        + ",".join(str(value) for value in hard_limits.get("tripped") or [])
                    )
                health = sample.get("health") or {}
                missing = sorted(
                    name
                    for name in ("primary", "recovery", "security_sentinel")
                    if not isinstance(health.get(name), Mapping)
                    or health[name].get("semantic_ready") is not True
                )
                if missing:
                    raise RuntimeError(
                        "initial readiness samples failed: " + ",".join(missing)
                    )
                return {
                    "ok": True,
                    "sample_monotonic_ns": sample.get("monotonic_ns"),
                    "field_completeness_ratio": sample.get("field_completeness_ratio"),
                }
            if not self.resource_monitor.is_alive():
                raise RuntimeError("resource monitor exited before first sample")
            time.sleep(0.1)
        raise RuntimeError("resource monitor produced no initial sample before timeout")

    def request_hard_stop(self, *, reason: str, classification: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        if self.state_machine is None:
            self.stop_event.set()
            return {"state": "STOPPING_LOAD", "reason": reason}
        try:
            state = self.state_machine.hard_stop(
                reason_code=reason,
                classification=classification,
                evidence=evidence,
            )
        except CampaignStateError:
            state = self.state_machine.snapshot()
        self._write_control_from_state(state)
        durable_atomic_write_json(self.core_stop_file, {
            "schema_version": "hackme.campaign-load-stop.v1",
            "campaign_uuid": self.campaign_uuid,
            "reason": reason,
            "classification": classification,
            "requested_at": utc_now(),
        })
        self.stop_event.set()
        return state

    def mark_failed(self, *, reason: str, classification: str = "FAIL_HARNESS") -> None:
        if self.state_machine is None:
            return
        try:
            current = CampaignState(self.state_machine.snapshot()["state"])
            if current in {CampaignState.PREPARING, CampaignState.PREFLIGHT, CampaignState.FROZEN}:
                state = self.state_machine.transition(
                    CampaignState.FAILED,
                    reason=reason,
                    classification=classification,
                )
                self._write_control_from_state(state)
            elif current in {CampaignState.ACTIVE, CampaignState.DEGRADED}:
                self.request_hard_stop(reason=reason, classification=classification, evidence={})
        except Exception:
            pass

    def run_step(
        self,
        scenario_id: str,
        step_id: str,
        command: list[str],
        *,
        timeout: int,
        artifact: Path | None = None,
        env: dict[str, str] | None = None,
        cwd: Path = ROOT,
        payload_ok: Callable[[dict[str, Any]], bool] | None = None,
        process_role: str = "scenario",
    ) -> dict[str, Any]:
        if self.supervised:
            control = load_json(self.control_path)
            if control.get("admit_new_jobs") is not True:
                return {
                    "step_id": step_id,
                    "started_at": utc_now(),
                    "finished_at": utc_now(),
                    "elapsed_seconds": 0.0,
                    "returncode": 125,
                    "timed_out": False,
                    "command": sanitized_command(command),
                    "stdout": "",
                    "artifact": str(artifact) if artifact else "",
                    "artifact_summary": {},
                    "secret_leak_labels": [],
                    "error": "campaign_load_admission_closed",
                    "ok": False,
                }
        out_dir = self.reports / "scenarios" / scenario_id
        out_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = out_dir / f"{step_id}.stdout"
        started_at = utc_now()
        started = time.monotonic()
        full_env = self.base_env()
        full_env.update(env or {})
        with stdout_path.open("w", encoding="utf-8") as stdout:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=full_env,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            with self.lock:
                self.step_processes.add(process)
            process_identity: Any | None = None
            if self.supervised:
                try:
                    process_identity = self.process_registry.register(
                        process_role,
                        process.pid,
                        required=True,
                    )
                except Exception:
                    terminate_process_group(process)
                    with self.lock:
                        self.step_processes.discard(process)
                    raise
            timed_out = False
            try:
                returncode = process.wait(timeout=max(1, int(timeout)))
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_group(process)
                returncode = 124
                stdout.write(f"\n[TIMEOUT] {step_id} exceeded {timeout}s\n")
            finally:
                if process_identity is not None:
                    self.process_registry.unregister(process_role, process_identity)
                with self.lock:
                    self.step_processes.discard(process)
        output = stdout_path.read_text(encoding="utf-8", errors="replace")
        leaked = [label for label, value in (
            ("root", self.credentials.root),
            ("manager", self.credentials.manager),
            ("test", self.credentials.test),
            ("member", self.credentials.member),
        ) if value and value in output]
        payload = load_json(artifact) if artifact and artifact.exists() else {}
        evidence_errors: list[str] = []
        artifact_ok = False
        if artifact is None:
            evidence_errors.append("machine_success_evidence_required")
        elif not artifact.exists():
            evidence_errors.append("declared_artifact_missing")
        elif payload_ok is not None:
            try:
                artifact_ok = payload_ok(payload) is True
            except Exception as exc:
                evidence_errors.append(
                    f"payload_validator_error:{exc.__class__.__name__}:{exc}"
                )
            if not artifact_ok and not evidence_errors:
                evidence_errors.append("declared_artifact_validator_rejected")
        else:
            # Exit code 0, an empty JSON object, HTTP 200/202, or the absence
            # of an explicit failure is never product-success evidence.
            artifact_ok = payload.get("ok") is True
            if not artifact_ok:
                evidence_errors.append("declared_artifact_missing_explicit_ok_true")
        return {
            "step_id": step_id,
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "returncode": returncode,
            "timed_out": timed_out,
            "command": sanitized_command(command),
            "stdout": str(stdout_path),
            "artifact": str(artifact) if artifact else "",
            "artifact_summary": {
                "ok": payload.get("ok"),
                "verdict": payload.get("verdict"),
                "error": payload.get("error") or payload.get("msg") or "",
            } if payload else {},
            "evidence_errors": evidence_errors,
            "secret_leak_labels": leaked,
            "ok": returncode == 0 and not leaked and artifact_ok,
        }

    def stop_managed_steps(self) -> list[dict[str, Any]]:
        with self.lock:
            processes = list(self.step_processes)
        results: list[dict[str, Any]] = []
        for process in processes:
            terminate_process_group(process, grace_seconds=2)
            results.append({"pid": process.pid, "returncode": process.poll()})
        return results

    def run_group(self, scenario_id: str, steps: list[Callable[[], dict[str, Any]]]) -> dict[str, Any]:
        started_at = utc_now()
        started = time.monotonic()
        results: list[dict[str, Any]] = []
        for step in steps:
            if self.stop_event.is_set():
                results.append({"ok": False, "error": "campaign_stopping"})
                break
            results.append(step())
        return {
            "ok": bool(results) and all(item.get("ok") for item in results),
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "steps": results,
        }

    def scenario_media_long(self) -> dict[str, Any]:
        scenario_id = "media_long_hls_share"
        out = self.reports / "scenarios" / scenario_id / "video_hls_quality_stress.json"
        fixture = self.root / "fixtures" / "campaign_long_video.mkv"
        fixture_seconds = 45 if self.args.allow_short_duration else 3900
        minimum_duration = 30 if self.args.allow_short_duration else 3600
        minimum_segments = 3 if self.args.allow_short_duration else 100
        account_rows = [{"username": username, "password": password} for username, password in self.accounts[:3]]
        command = [
            sys.executable,
            str(ROOT / "scripts" / "testing" / "video_hls_quality_stress.py"),
            "--base-url", self.primary.base_url,
            "--video", str(fixture),
            "--db", str(self.primary.runtime_root / "database" / "database.db"),
            "--runtime-marker", str(self.primary.run_root),
            "--out", str(out),
            "--generate-fixture-duration-seconds", str(fixture_seconds),
            "--fixture-timeout-seconds", "1800",
            "--minimum-source-duration-seconds", str(minimum_duration),
            "--visibility", "unlisted",
            "--privacy-mode", "server_encrypted",
            "--upload",
            "--wait",
            "--measure",
            "--verify-share",
            "--browser-seek",
            "--browser-mobile",
            "--expect-audio-tracks", "2",
            "--expect-subtitles",
            "--minimum-segments-per-variant", str(minimum_segments),
            "--segment-concurrency", "8",
            "--max-segments-per-variant", "16",
            "--post-upload-observe-seconds", "5",
            "--upload-timeout-seconds", "1800",
            "--wait-timeout-seconds", "21600",
            "--wait-interval-seconds", "15",
            "--orphan-grace-seconds", "900",
        ]
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "long_video_hls_share",
                command,
                timeout=8 * 60 * 60,
                artifact=out,
                process_role="ffmpeg",
                env={
                    "HACKME_HLS_STRESS_ACCOUNTS_JSON": json.dumps(account_rows),
                    "HACKME_HLS_SHARE_PASSWORD": secrets.token_urlsafe(24),
                    "HACKME_PROBE_ROOT_PASSWORD": self.credentials.root,
                },
            )
        ])

    def scenario_ai_agent(self) -> dict[str, Any]:
        scenario_id = "ai_agent_operations"
        scripts = [
            ("frontend_full", "ai_agent_frontend_full_probe.py", []),
            ("drive_share", "ai_agent_drive_share_task_probe.py", []),
            ("server_ops", "ai_agent_server_ops_probe.py", []),
            ("governance", "ai_agent_governance_capability_probe.py", []),
            ("trading", "ai_agent_trading_capability_probe.py", []),
            ("media", "ai_agent_media_downloader_probe.py", ["--fixture", str(self.root / "fixtures" / "ai_agent_media_probe.mp4")]),
            ("capability_boundary", "ai_agent_capability_boundary_probe.py", ["--comfyui-api-url", "http://127.0.0.1:1"]),
        ]
        comfyui_url = str(os.environ.get("HACKME_CAMPAIGN_COMFYUI_API_URL") or "").strip()
        if comfyui_url:
            scripts.append(("real_image_generation", "ai_agent_image_generation_probe.py", ["--comfyui-api-url", comfyui_url]))
        steps: list[Callable[[], dict[str, Any]]] = []
        for step_id, script_name, extra in scripts:
            artifact = self.reports / "scenarios" / scenario_id / f"{step_id}.json"
            command = [
                sys.executable,
                str(ROOT / "scripts" / "testing" / script_name),
                "--base-url", self.primary.base_url,
                "--out", str(artifact),
                *extra,
            ]
            steps.append(lambda step_id=step_id, command=command, artifact=artifact: self.run_step(
                scenario_id,
                step_id,
                command,
                timeout=1800,
                artifact=artifact,
                process_role=("comfyui" if "image_generation" in step_id else "browser" if step_id == "frontend_full" else "scenario"),
            ))
        return self.run_group(scenario_id, steps)

    def scenario_trading(self) -> dict[str, Any]:
        scenario_id = "trading_background_and_abuse"
        background_dir = self.reports / "scenarios" / scenario_id / "background"
        pentest_dir = self.reports / "scenarios" / scenario_id / "pentest"
        stress_orders = 20 if self.args.allow_short_duration else 150
        users = 3 if self.args.allow_short_duration else 16
        orders = 10 if self.args.allow_short_duration else 180
        background_artifact = background_dir / "trading_background_correctness.json"
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "background_correctness",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "playwright_trading_background_correctness.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-dir", str(self.primary.runtime_root),
                    "--out", str(background_dir),
                    "--trigger-mode", "auto",
                    "--stress-orders", str(stress_orders),
                ],
                timeout=3600,
                artifact=background_artifact,
                process_role="browser",
            ),
            lambda: self.run_step(
                scenario_id,
                "trading_stress_pentest",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "security" / "pentest" / "trading_stress_pentest.py"),
                    "--base-url", self.primary.base_url,
                    "--users", str(users),
                    "--orders-per-user", str(orders),
                    "--concurrency", "16",
                    "--rate", "60",
                    "--out", str(pentest_dir),
                ],
                timeout=3600,
            ),
        ])

    def scenario_points_hft(self) -> dict[str, Any]:
        scenario_id = "pointschain_hft_invariants"
        out_dir = self.reports / "scenarios" / scenario_id
        stress = out_dir / "points_chain_destructive_stress.json"
        post = out_dir / "points_chain_post_stress.json"
        direct_ops = 200 if self.args.allow_short_duration else 12000
        transfer_ops = 20 if self.args.allow_short_duration else 1200
        trading_ops = 10 if self.args.allow_short_duration else 600
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "high_frequency_chain_and_trading",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "points_chain_destructive_stress.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-root", str(self.primary.runtime_root),
                    "--out", str(stress),
                    "--accounts", "24",
                    "--grant-points", "20000",
                    "--transfer-ops", str(transfer_ops),
                    "--direct-transfer-ops", str(direct_ops),
                    "--trading-ops", str(trading_ops),
                    "--concurrency", "32",
                    "--external-transfer-every", "7",
                    "--max-external-transfers", "40",
                    "--server-pids", str(self.primary.pid()),
                ],
                timeout=4 * 60 * 60,
                artifact=stress,
                env={"HACKME_POINTS_STRESS_ROOT_PASSWORD": self.credentials.root},
            ),
            lambda: self.run_step(
                scenario_id,
                "post_stress_frontend",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "points_chain_post_stress_playwright.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(post),
                    "--member-username", "admin",
                ],
                timeout=1200,
                artifact=post,
                process_role="browser",
            ),
        ])

    def scenario_points_incident(self) -> dict[str, Any]:
        scenario_id = "pointschain_incident_governance"
        out_dir = self.reports / "scenarios" / scenario_id
        attacks = out_dir / "real_incident_attacks.json"
        dispute = out_dir / "dispute_api.json"
        frontend = out_dir / "incident_frontend.json"
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "real_incident_attack_regressions",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_real_incident_attack_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(attacks),
                ],
                timeout=5400,
                artifact=attacks,
            ),
            lambda: self.run_step(
                scenario_id,
                "live_dispute_api",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_dispute_api_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-root", str(self.primary.runtime_root),
                    "--out", str(dispute),
                ],
                timeout=1800,
                artifact=dispute,
            ),
            lambda: self.run_step(
                scenario_id,
                "incident_frontend",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_real_incident_frontend_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(frontend),
                ],
                timeout=1800,
                artifact=frontend,
                process_role="browser",
            ),
        ])

    def scenario_media_compatibility(self) -> dict[str, Any]:
        scenario_id = "media_proxy_cross_browser"
        out_dir = self.reports / "scenarios" / scenario_id
        service_out = out_dir / "realtime_proxy_service.json"
        http_root = out_dir / "http_concurrency"
        browser_root = out_dir / "browser_compat"
        browser_artifact = browser_root / "reports" / "qa" / "browser_video_compat.json"
        chat_out = out_dir / "chat_video_share.json"
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "realtime_proxy_service",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "realtime_proxy_stress_probe.py"),
                    "--runtime-root", str(out_dir / "service_runtime"),
                    "--json-out", str(service_out),
                    "--duration", "12" if self.args.allow_short_duration else "90",
                    "--max-concurrent", "2",
                ],
                timeout=1200,
                artifact=service_out,
                process_role="ffmpeg",
            ),
            lambda: self.run_step(
                scenario_id,
                "realtime_proxy_http_concurrency",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "realtime_proxy_http_concurrency_probe.py"),
                    "--runtime-root", str(http_root),
                    "--json-out", str(http_root / "result.json"),
                    "--duration", "8" if self.args.allow_short_duration else "60",
                    "--max-concurrent", "2",
                    "--server-runner", "gunicorn",
                    "--gunicorn-workers", "3",
                    "--gunicorn-threads", "2",
                ],
                timeout=1800,
                artifact=http_root / "result.json",
                process_role="ffmpeg",
            ),
            lambda: self.run_step(
                scenario_id,
                "cross_browser_video",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "playwright_browser_video_compat.py"),
                    "--runtime-root", str(browser_root),
                    "--browsers", "chromium" if self.args.allow_short_duration else "chromium,firefox,webkit",
                ],
                timeout=3600,
                artifact=browser_artifact,
                process_role="browser",
            ),
            lambda: self.run_step(
                scenario_id,
                "chat_video_share_embed",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "chat_video_share_link_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(chat_out),
                ],
                timeout=1200,
                artifact=chat_out,
                process_role="browser",
            ),
        ])

    def scenario_final_ui_prelaunch(self) -> dict[str, Any]:
        scenario_id = "final_ui_mobile_prelaunch"
        out_dir = self.reports / "scenarios" / scenario_id
        deep_root = out_dir / "deep_site"
        member = out_dir / "member_probe.json"
        gate = out_dir / "production_gate"
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "member_real_behavior",
                [
                    sys.executable,
                    str(ROOT / "docs" / "AGENTS" / "skills" / "hackme-web-qa" / "scripts" / "member_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(member),
                ],
                timeout=1800,
                artifact=member,
                process_role="browser",
            ),
            lambda: self.run_step(
                scenario_id,
                "desktop_mobile_deep_site",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "playwright_deep_site_check.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-root", str(deep_root),
                    "--max-chess-human-moves", "8",
                ],
                timeout=5400,
                process_role="browser",
            ),
            lambda: self.run_step(
                scenario_id,
                "whole_site_production_gate",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "security" / "gate" / "whole_site_production_gate.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(gate),
                    "--skip-full-pytest",
                    "--stress-requests", "80" if self.args.allow_short_duration else "400",
                    "--stress-concurrency", "16",
                ],
                timeout=3 * 60 * 60,
                process_role="browser",
            ),
        ])

    def _create_user(self, root: WebClient, username: str, password: str, *, nickname: str = "Campaign User") -> dict[str, Any]:
        result = root.request(
            "POST",
            "/api/admin/users",
            json_body={
                "username": username,
                "password": password,
                "password_confirm": password,
                "nickname": nickname,
                "role": "user",
                "status": "active",
                "member_level": "normal",
            },
        )
        search = root.request("GET", "/api/admin/users", params={"q": username, "page_size": 100})
        users = ((search.get("body") or {}).get("users") or []) if isinstance(search.get("body"), dict) else []
        exact = next((item for item in users if str(item.get("username") or "") == username), None)
        return {
            "ok": int(result.get("status") or 0) in {200, 201, 409} and exact is not None,
            "create_status": result.get("status"),
            "search_status": search.get("status"),
            "user_id": int((exact or {}).get("id") or 0),
            "username": username,
        }

    def _user_exists(self, root: WebClient, username: str) -> bool:
        search = root.request("GET", "/api/admin/users", params={"q": username, "page_size": 100})
        users = ((search.get("body") or {}).get("users") or []) if isinstance(search.get("body"), dict) else []
        return any(str(item.get("username") or "") == username for item in users)

    def _wallet_transfer_between_builtin_users(self, base_url: str, *, reference: str) -> dict[str, Any]:
        sender = WebClient(base_url, "test", self.credentials.test, timeout=60)
        recipient = WebClient(base_url, "admin", self.credentials.manager, timeout=60)
        sender_login = sender.login()
        recipient_login = recipient.login()
        sender_wallet = sender.request("GET", "/api/points/wallet", params={"hydrate": "1"})
        recipient_wallet = recipient.request("GET", "/api/points/wallet", params={"hydrate": "1"})
        sender_row = (sender_wallet.get("body") or {}).get("wallet") or {}
        recipient_row = (recipient_wallet.get("body") or {}).get("wallet") or {}
        source = str(sender_row.get("active_wallet_address") or "")
        destination = str(recipient_row.get("active_wallet_address") or "")
        transfer = sender.request(
            "POST",
            "/api/points/transactions/submit",
            json_body={
                "source_wallet_address": source,
                "destination_wallet_address": destination,
                "amount_points": 1,
                "fee_points": 0,
                "request_uuid": reference,
                "memo": "24h campaign restore-boundary transfer",
                "compact": True,
            },
        ) if source and destination else {"ok": False, "status": 0, "error": "wallet_address_missing"}
        body = transfer.get("body") or {}
        tx_hash = str(body.get("transaction_hash") or body.get("tx_group_hash") or "")
        return {
            "ok": bool(sender_login.get("ok") and recipient_login.get("ok") and transfer.get("ok") and tx_hash),
            "sender_login": sender_login.get("status"),
            "recipient_login": recipient_login.get("status"),
            "sender_balance_before": sender_row.get("points_balance"),
            "source_wallet_present": bool(source),
            "destination_wallet_present": bool(destination),
            "transfer_status": transfer.get("status"),
            "transaction_hash": tx_hash,
        }

    def _snapshot_restore_boundary_cycle(self) -> dict[str, Any]:
        started = time.monotonic()
        root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=180)
        login = root.login()
        if not login.get("ok"):
            return {"ok": False, "error": "root_login_failed", "login": login.get("status")}
        storage_marker = self.recovery.runtime_root / "storage" / "campaign_snapshot_marker.txt"
        storage_marker.parent.mkdir(parents=True, exist_ok=True)
        storage_marker.write_text("snapshot-baseline\n", encoding="utf-8")
        # Hydrate stable built-in wallets before the snapshot so the later
        # append-only transfer never references a user absent from restored core state.
        pre_transfer_wallet = self._wallet_transfer_between_builtin_users(
            self.recovery.base_url,
            reference=f"campaign-pre-snapshot-{int(time.time())}",
        )
        snapshot = root.request(
            "POST",
            "/api/admin/snapshots",
            json_body={"type": "manual", "notes": "24h campaign ordinary-state restore boundary"},
        )
        snapshot_id = str((snapshot.get("body") or {}).get("snapshot_id") or "")
        marker_username = f"restore_dirty_{int(time.time())}"
        marker = self._create_user(root, marker_username, self.credentials.member, nickname="Restore Dirty Marker")
        storage_marker.write_text("snapshot-dirty-after-create\n", encoding="utf-8")
        transfer = self._wallet_transfer_between_builtin_users(
            self.recovery.base_url,
            reference=f"campaign-post-snapshot-{int(time.time())}",
        )
        restore = root.request(
            "POST",
            f"/api/admin/snapshots/{quote(snapshot_id)}/restore",
            json_body={"confirm": "RESTORE", "reason": "24h campaign restore-boundary verification"},
        ) if snapshot_id else {"ok": False, "status": 0, "error": "snapshot_id_missing"}
        root.login()
        marker_absent = not self._user_exists(root, marker_username)
        tx_hash = str(transfer.get("transaction_hash") or "")
        explorer = root.request("GET", f"/api/points/explorer/tx/{quote(tx_hash, safe='')}") if tx_hash else {"ok": False, "status": 0}
        verify = root.request("GET", "/api/root/points/chain/verify")
        restore_body = restore.get("body") or {}
        skipped = {
            str(item.get("label") or ""): str(item.get("reason") or "")
            for item in ((restore_body.get("database_restore") or {}).get("skipped") or [])
        }
        protected_skips = {
            label: skipped.get(label)
            for label in ("finance", "points_chain", "trading")
            if label in skipped
        }
        storage_restored = storage_marker.exists() and storage_marker.read_text(encoding="utf-8") == "snapshot-baseline\n"
        ok = bool(
            pre_transfer_wallet.get("ok")
            and snapshot.get("ok")
            and snapshot_id
            and marker.get("ok")
            and transfer.get("ok")
            and restore.get("ok")
            and marker_absent
            and explorer.get("ok")
            and storage_restored
            and protected_skips.get("finance") == "append_only_financial_restore_disabled"
            and int(verify.get("status") or 0) in {200, 202}
        )
        return {
            "ok": ok,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "snapshot_status": snapshot.get("status"),
            "snapshot_id_present": bool(snapshot_id),
            "dirty_marker_created": marker.get("ok"),
            "dirty_marker_absent_after_restore": marker_absent,
            "append_only_transfer": transfer,
            "transfer_survived_restore": explorer.get("ok"),
            "restore_status": restore.get("status"),
            "protected_database_skips": protected_skips,
            "storage_restored": storage_restored,
            "chain_verify_status": verify.get("status"),
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""

    @staticmethod
    def _sqlite_checks(database_dir: Path) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for path in sorted(database_dir.glob("*.db")):
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
                row = conn.execute("PRAGMA quick_check").fetchone()
                conn.close()
                results[path.name] = {"ok": bool(row and row[0] == "ok"), "result": row[0] if row else "missing"}
            except Exception as exc:
                results[path.name] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
        return results

    def _cli_backup_restore_cycle(self, scenario_id: str) -> dict[str, Any]:
        started = time.monotonic()
        archive = self.root / "backups" / "recovery_runtime.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            archive.unlink()
        stop_before_backup = self.recovery.stop(reason="campaign_cli_backup")
        backup_step = self.run_step(
            scenario_id,
            "cli_runtime_backup",
            [
                str(LAUNCHER),
                "--cli",
                "--run-root", str(self.recovery.run_root),
                "--runtime-root", str(self.recovery.runtime_root),
                "--in-place",
                "--tmp-runtime",
                "--skip-install",
                "--backup", str(archive),
            ],
            timeout=1800,
        ) if stop_before_backup.get("ok") else {"ok": False, "error": "stop_before_backup_failed"}
        backup_size = archive.stat().st_size if archive.exists() else 0
        start_after_backup = self.recovery.start() if backup_step.get("ok") else {"ok": False, "error": "backup_failed"}
        root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
        root_login = root.login() if start_after_backup.get("ok") else {"ok": False}
        marker_username = f"cli_restore_dirty_{int(time.time())}"
        marker = self._create_user(root, marker_username, self.credentials.member, nickname="CLI Restore Dirty") if root_login.get("ok") else {"ok": False}
        transfer = self._wallet_transfer_between_builtin_users(
            self.recovery.base_url,
            reference=f"campaign-cli-post-backup-{int(time.time())}",
        ) if root_login.get("ok") else {"ok": False}
        storage_marker = self.recovery.runtime_root / "storage" / "campaign_cli_storage_marker.txt"
        storage_marker.parent.mkdir(parents=True, exist_ok=True)
        storage_marker.write_text("live-storage-after-backup\n", encoding="utf-8")
        stop_before_restore = self.recovery.stop(reason="campaign_cli_restore")
        finance = self.recovery.runtime_root / "database" / "finance.db"
        protected_hash_before = self._sha256(finance)
        restore_step = self.run_step(
            scenario_id,
            "cli_runtime_restore",
            [
                str(LAUNCHER),
                "--cli",
                "--run-root", str(self.recovery.run_root),
                "--runtime-root", str(self.recovery.runtime_root),
                "--in-place",
                "--tmp-runtime",
                "--skip-install",
                "--restore", str(archive),
            ],
            timeout=1800,
        ) if stop_before_restore.get("ok") else {"ok": False, "error": "stop_before_restore_failed"}
        protected_hash_after = self._sha256(finance)
        storage_preserved = storage_marker.exists() and storage_marker.read_text(encoding="utf-8") == "live-storage-after-backup\n"
        policy_path = self.recovery.runtime_root / "logs" / "runtime_restore_policy.json"
        policy = load_json(policy_path) if policy_path.exists() else {}
        sqlite_checks = self._sqlite_checks(self.recovery.runtime_root / "database")
        start_after_restore = self.recovery.start() if restore_step.get("ok") else {"ok": False, "error": "restore_failed"}
        root_after = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
        root_after_login = root_after.login() if start_after_restore.get("ok") else {"ok": False}
        marker_absent = not self._user_exists(root_after, marker_username) if root_after_login.get("ok") else False
        tx_hash = str(transfer.get("transaction_hash") or "")
        explorer = root_after.request("GET", f"/api/points/explorer/tx/{quote(tx_hash, safe='')}") if tx_hash and root_after_login.get("ok") else {"ok": False}
        verify = root_after.request("GET", "/api/root/points/chain/verify") if root_after_login.get("ok") else {"ok": False, "status": 0}
        ok = bool(
            stop_before_backup.get("ok")
            and backup_step.get("ok")
            and backup_size > 0
            and start_after_backup.get("ok")
            and marker.get("ok")
            and transfer.get("ok")
            and stop_before_restore.get("ok")
            and restore_step.get("ok")
            and protected_hash_before
            and protected_hash_before == protected_hash_after
            and storage_preserved
            and policy.get("policy") == "append_only_financial_restore_disabled"
            and all(item.get("ok") for item in sqlite_checks.values())
            and start_after_restore.get("ok")
            and marker_absent
            and explorer.get("ok")
            and int(verify.get("status") or 0) in {200, 202}
        )
        return {
            "ok": ok,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stop_before_backup": stop_before_backup,
            "backup": backup_step,
            "backup_size_bytes": backup_size,
            "start_after_backup": start_after_backup,
            "dirty_marker_created": marker.get("ok"),
            "append_only_transfer": transfer,
            "stop_before_restore": stop_before_restore,
            "restore": restore_step,
            "protected_finance_hash_preserved": protected_hash_before == protected_hash_after and bool(protected_hash_before),
            "storage_preserved": storage_preserved,
            "restore_policy": policy,
            "sqlite_quick_checks": sqlite_checks,
            "start_after_restore": start_after_restore,
            "dirty_marker_absent_after_restore": marker_absent,
            "transfer_survived_restore": explorer.get("ok"),
            "chain_verify_status": verify.get("status"),
        }

    def scenario_recovery_backup(self) -> dict[str, Any]:
        scenario_id = "recovery_backup_restart"
        out_dir = self.reports / "scenarios" / scenario_id
        realistic = out_dir / "realistic_wallet_incident.json"
        branch = out_dir / "governed_recovery_branch.json"
        restore_drill = out_dir / "rc1_restore_drill.json"

        def realistic_step() -> dict[str, Any]:
            stop = self.recovery.stop(reason="realistic_wallet_incident")
            if not stop.get("ok"):
                return {"ok": False, "stop": stop}
            probe = self.run_step(
                scenario_id,
                "realistic_wallet_incident",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_realistic_recovery_drill.py"),
                    "--runtime-root", str(self.recovery.runtime_root),
                    "--out", str(realistic),
                    "--mode", "dev_ready",
                ],
                timeout=3600,
                artifact=realistic,
            )
            start = self.recovery.start() if probe.get("ok") else {"ok": False, "error": "incident_probe_failed"}
            return {"ok": bool(stop.get("ok") and probe.get("ok") and start.get("ok")), "stop": stop, "probe": probe, "start": start}

        def branch_step() -> dict[str, Any]:
            incident = load_json(realistic)
            tx_hash = str((incident.get("incident") or {}).get("theft_tx_hash") or "")
            victim_wallet = str(((incident.get("users") or {}).get("victim") or {}).get("wallet") or "")
            if not tx_hash or not victim_wallet:
                return {"ok": False, "error": "realistic incident evidence missing"}
            return self.run_step(
                scenario_id,
                "governed_recovery_branch",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_live_branch_drill.py"),
                    "--base-url", self.recovery.base_url,
                    "--incident-tx-hash", tx_hash,
                    "--victim-wallet", victim_wallet,
                    "--claim-amount", "60",
                    "--out", str(branch),
                ],
                timeout=3600,
                artifact=branch,
            )

        return self.run_group(scenario_id, [
            self._snapshot_restore_boundary_cycle,
            realistic_step,
            branch_step,
            lambda: self.run_step(
                scenario_id,
                "isolated_snapshot_boundary_drill",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "ops" / "rc1_restore_drill.py"),
                    "--out", str(restore_drill),
                ],
                timeout=1800,
                artifact=restore_drill,
            ),
            lambda: self._cli_backup_restore_cycle(scenario_id),
            lambda: self.recovery.restart(reason="final_recovery_restart"),
        ])

    def provision_accounts(self) -> list[tuple[str, str]]:
        root = WebClient(
            self.primary.base_url,
            "root",
            self.credentials.root,
            timeout=60,
            progress_callback=lambda detail: self._server_progress(
                f"provision_accounts:root:{detail}"
            ),
        )
        login = root.login()
        self._server_progress("audit_account_cleanup_root_login_completed")
        if not login.get("ok"):
            raise RuntimeError(f"primary root login failed: status={login.get('status')}")
        prefix = f"campaign{datetime.now(timezone.utc).strftime('%m%d%H%M')}"
        accounts: list[tuple[str, str]] = []
        for index in range(1, max(4, int(self.args.account_count)) + 1):
            username = f"{prefix}{index:02d}"
            created = self._create_user(root, username, self.credentials.member, nickname=f"Campaign {index:02d}")
            if not created.get("ok"):
                raise RuntimeError(f"campaign account provisioning failed: {created}")
            self.account_inventory.append({
                "username": username,
                "user_id": int(created.get("user_id") or 0),
                "source": "campaign_runner",
                "created_or_reused": int(created.get("create_status") or 0) in {200, 201, 409},
            })
            member = WebClient(
                self.primary.base_url,
                username,
                self.credentials.member,
                timeout=60,
                progress_callback=lambda detail, account=username: self._server_progress(
                    f"provision_accounts:{account}:{detail}"
                ),
            )
            if not member.login().get("ok"):
                raise RuntimeError(f"campaign account login failed: {username}")
            accounts.append((username, self.credentials.member))
        self.accounts = accounts
        return accounts

    def cleanup_campaign_accounts(self, *, additional_usernames: list[str] | None = None) -> dict[str, Any]:
        """Delete isolated test accounts and prove the original names vanished."""

        inventory = {str(row.get("username") or ""): dict(row) for row in self.account_inventory if row.get("username")}
        for username in additional_usernames or []:
            value = str(username or "").strip()
            if value and value not in {"root", "admin", "test"}:
                inventory.setdefault(value, {"username": value, "user_id": 0, "source": "core_soak"})
        result: dict[str, Any] = {
            "schema_version": "hackme.campaign-account-cleanup.v1",
            "checked_at": utc_now(),
            "inventory": list(inventory.values()),
            "records": [],
            "ok": True,
        }
        if not inventory:
            atomic_write_json(self.reports / "account_cleanup.json", result)
            self.account_cleanup = result
            return result
        root = WebClient(
            self.primary.base_url,
            "root",
            self.credentials.root,
            timeout=60,
            progress_callback=lambda detail: self._server_progress(
                f"audit_account_cleanup:{detail}"
            ),
        )
        login = root.login()
        if not login.get("ok"):
            result.update({"ok": False, "error": "cleanup_root_login_failed", "login_status": login.get("status")})
            atomic_write_json(self.reports / "account_cleanup.json", result)
            self.account_cleanup = result
            return result
        for username, row in sorted(inventory.items()):
            user_id = int(row.get("user_id") or 0)
            if user_id <= 0:
                lookup = root.request("GET", "/api/admin/users", params={"q": username, "page_size": 100})
                self._server_progress(f"audit_account_lookup_completed:{username}")
                users = ((lookup.get("body") or {}).get("users") or []) if isinstance(lookup.get("body"), dict) else []
                exact = next((item for item in users if str(item.get("username") or "") == username), None)
                user_id = int((exact or {}).get("id") or 0)
            deleted = root.request("DELETE", f"/api/admin/users/{user_id}") if user_id > 0 else {
                "ok": False,
                "status": 404,
                "body": {"msg": "account_not_found_before_cleanup"},
            }
            self._server_progress(f"audit_account_delete_completed:{username}")
            verify = root.request("GET", "/api/admin/users", params={"q": username, "page_size": 100})
            self._server_progress(f"audit_account_verify_completed:{username}")
            verify_users = ((verify.get("body") or {}).get("users") or []) if isinstance(verify.get("body"), dict) else []
            residual = [item for item in verify_users if str(item.get("username") or "") == username]
            cleanup_detail = (deleted.get("body") or {}).get("cleanup") or {}
            warnings = cleanup_detail.get("warnings") or []
            record_ok = bool(
                int(deleted.get("status") or 0) == 200
                and not residual
                and not warnings
                and int(verify.get("status") or 0) == 200
            )
            result["records"].append({
                "username": username,
                "user_id": user_id,
                "source": row.get("source"),
                "delete_status": deleted.get("status"),
                "verify_status": verify.get("status"),
                "residual_exact_count": len(residual),
                "cleanup_warnings": warnings,
                "ok": record_ok,
            })
        result["ok"] = bool(result["records"]) and all(row.get("ok") for row in result["records"])
        atomic_write_json(self.reports / "account_cleanup.json", result)
        self.account_cleanup = result
        return result

    def formal_scenario_binding_preflight(self) -> tuple[dict[str, Any], bool]:
        """Return the strict scenario binding artifact and whether it is required.

        Level-0 smoke deliberately does not execute the product scenario matrix.
        Every longer supervised level must have exact native runner, evidence
        adapter, and validator registrations before any server is started.
        """

        if self.campaign_level == "smoke":
            return ({
                "schema_version": FORMAL_BINDING_GATE_SCHEMA_VERSION,
                "status": "NOT_APPLICABLE",
                "gate_pass": False,
                "formal_campaign_pass": False,
                "reason": "level_0_lifecycle_smoke_does_not_execute_or_claim_formal_scenarios",
            }, False)
        return build_and_validate_formal_scenario_bindings(
            runner_registry=self.native_scenario_runner_registry(),
        ).to_dict(), True

    def native_scenario_runner_registry(self) -> Mapping[str, ScenarioRunnerRegistration]:
        """Return only exact-ID legacy runners that are genuinely executable.

        These four registrations are deliberately partial.  They do not make
        a reviewed binding complete because the audited evidence, terminal,
        cleanup, and artifact handlers are still absent.  Old methods whose
        IDs or domains differ from the reviewed catalogue are not aliased.
        """

        handlers: Mapping[str, Callable[[], dict[str, Any]]] = {
            "media_long_hls_share": self.scenario_media_long,
            "pointschain_hft_invariants": self.scenario_points_hft,
            "media_proxy_cross_browser": self.scenario_media_compatibility,
            "final_ui_mobile_prelaunch": self.scenario_final_ui_prelaunch,
        }
        registrations: dict[str, ScenarioRunnerRegistration] = {}
        for scenario_id, handler in handlers.items():
            binding = FORMAL_SCENARIO_BINDINGS[scenario_id]
            registrations[binding.runner_id] = ScenarioRunnerRegistration(
                runner_id=binding.runner_id,
                scenario_id=scenario_id,
                implementation_ref=f"{handler.__module__}:{handler.__name__}",
                handler=handler,
            )
        return registrations

    def run_formal_native_scenario(self, scenario_id: str) -> dict[str, Any]:
        """Fail closed until the exact reviewed binding has a runtime pipeline."""

        gate = build_and_validate_formal_scenario_bindings(
            runner_registry=self.native_scenario_runner_registry(),
        )
        coverage = gate.registration_coverage.get(scenario_id) or {}
        return {
            "ok": False,
            "classification": "FAIL_HARNESS",
            "error": "formal_native_binding_incomplete",
            "scenario_id": scenario_id,
            "registration_coverage": dict(coverage),
            "binding_blockers": list(gate.binding_blockers.get(scenario_id) or ()),
        }

    def preflight(self) -> dict[str, Any]:
        commands = {
            "ffmpeg": ["ffmpeg", "-version"],
            "ffprobe": ["ffprobe", "-version"],
            "playwright": [sys.executable, "-c", "from playwright.sync_api import sync_playwright; print('ok')"],
            "gunicorn": [sys.executable, "-c", "import gunicorn; print(gunicorn.__version__)"],
        }
        dependencies: dict[str, Any] = {}
        for name, command in commands.items():
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(ROOT),
                    env=self.base_env(),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                dependencies[name] = {
                    "ok": completed.returncode == 0,
                    "returncode": completed.returncode,
                    "output": (completed.stdout or completed.stderr or "")[:500],
                }
            except Exception as exc:
                dependencies[name] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            if self.supervised:
                self._server_progress(f"preflight_dependency_completed:{name}")
        disk = os.statvfs(self.root.parent)
        free_bytes = int(disk.f_bavail * disk.f_frsize)
        runtime_scan = bounded_repo_runtime_scan(
            ROOT,
            progress_callback=self._server_progress if self.supervised else None,
        )
        runtime_pollution = list(runtime_scan.get("repo_runtime_pollution") or [])
        process_inheritance = self.verify_role_inheritance() if self.supervised else {"ok": True, "mode": "unsupervised_short_test"}
        if self.supervised:
            self._server_progress("preflight_process_inheritance_completed")
        formal_scenario_binding, formal_scenario_binding_required = (
            self.formal_scenario_binding_preflight()
        )
        if self.supervised:
            self._server_progress("preflight_scenario_binding_completed")
        atomic_write_json(
            self.reports / "formal_scenario_binding_gate.json",
            formal_scenario_binding,
        )
        result = {
            "ok": (
                all(item.get("ok") for item in dependencies.values())
                and free_bytes >= int(self.args.minimum_free_gb * 1024**3)
                and runtime_scan.get("ok") is True
                and not runtime_pollution
                and process_inheritance.get("ok") is True
                and (
                    not formal_scenario_binding_required
                    or formal_scenario_binding.get("gate_pass") is True
                )
            ),
            "dependencies": dependencies,
            "free_bytes": free_bytes,
            "minimum_free_bytes": int(self.args.minimum_free_gb * 1024**3),
            "repo_runtime_pollution": runtime_pollution,
            "repo_runtime_scan": runtime_scan,
            "source_manifest_files": len(self.source_hashes),
            "source_manifest_digest": self.source_digest,
            "source_git": self.source_git,
            "process_inheritance": process_inheritance,
            "formal_scenario_binding_required": formal_scenario_binding_required,
            "formal_scenario_binding": formal_scenario_binding,
        }
        atomic_write_json(self.reports / "preflight.json", result)
        return result

    def verify_role_inheritance(self) -> dict[str, Any]:
        roles = tuple(sorted(MANDATORY_MANAGED_ROLES))
        processes: dict[str, subprocess.Popen[Any]] = {}
        placements: dict[str, Any] = {}
        errors: list[str] = []
        expected = "/" + self.cgroup_path.strip().lstrip("/")
        try:
            for role in roles:
                processes[role] = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=str(ROOT),
                    env=self.base_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            deadline = time.monotonic() + 10
            pending = set(roles)
            while pending and time.monotonic() < deadline:
                for role in list(pending):
                    process = processes[role]
                    if process.poll() is not None:
                        errors.append(f"{role}:probe_exited:{process.returncode}")
                        pending.remove(role)
                        continue
                    try:
                        actual = _current_unified_cgroup(process.pid)
                    except Exception:
                        continue
                    inside = actual == expected or actual.startswith(expected.rstrip("/") + "/")
                    placements[role] = {
                        "pid": process.pid,
                        "expected_cgroup": expected,
                        "actual_cgroup": actual,
                        "inside_campaign_scope": inside,
                        "ok": inside,
                    }
                    if not inside:
                        errors.append(f"{role}:outside_campaign_scope:{actual}")
                    pending.remove(role)
                time.sleep(0.05)
            errors.extend(f"{role}:membership_unobservable" for role in sorted(pending))
        finally:
            for process in processes.values():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            for process in processes.values():
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        result = {
            "schema_version": "hackme.campaign-process-inheritance.v1",
            "required_roles": list(roles),
            "placements": placements,
            "errors": errors,
            "ok": not errors and set(placements) == set(roles) and all(row.get("ok") for row in placements.values()),
        }
        durable_atomic_write_json(self.reports / "preflight_process_inheritance.json", result)
        return result

    def production_security_sentinel_check(self, *, phase: str) -> dict[str, Any]:
        def observed_session_factory() -> requests.Session:
            session = requests.Session()
            original_request = session.request

            def observed_request(method: str, url: str, **kwargs: Any) -> requests.Response:
                try:
                    response = original_request(method, url, **kwargs)
                except BaseException as exc:
                    self._server_progress(
                        f"security_{phase}:request_completed:{str(method).upper()}:error:{exc.__class__.__name__}"
                    )
                    raise
                self._server_progress(
                    f"security_{phase}:request_completed:{str(method).upper()}:{response.status_code}"
                )
                return response

            session.request = observed_request  # type: ignore[method-assign]
            return session

        probe = ProductionSecuritySentinel(SecuritySentinelConfig(
            base_url=self.security_sentinel.base_url,
            runtime_root=self.security_sentinel.runtime_root,
            load_target_runtime_root=self.primary.runtime_root,
            credentials={
                "root": self.credentials.root,
                "manager": self.credentials.manager,
                "user": self.credentials.test,
            },
            launcher_command=tuple(self.security_sentinel.launcher_command()),
            cross_worker_requests=12,
        ), session_factory=observed_session_factory)
        result = probe.run_once()
        path = self.reports / "security" / f"production_security_sentinel_{phase}.json"
        write_security_sentinel_result(path, result)
        result["artifact"] = str(path)
        return result

    def start_core_soak(self) -> dict[str, Any]:
        if artifact_exists(self.core_root) and self.core_root.is_symlink():
            raise ActivationArtifactError("core soak runtime root symlink rejected")
        self.core_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.core_root, 0o700)
        activation_required = bool(
            self.supervised and self.campaign_level in {"rehearsal", "formal"}
        )
        if activation_required:
            prepare_private_directory(
                self.core_activation_dir,
                authority_root=self.core_root,
            )
            assert_fresh_artifact_paths([
                self.core_ready_file,
                self.core_activation_file,
                self.core_activation_ack_file,
            ])
        stdout_path = self.core_root / "operational_soak.stdout"
        if self.supervised and self.campaign_level == "smoke":
            self.core_report = self.core_root / "campaign_smoke_load.json"
            self.core_command = [
                sys.executable,
                str(SMOKE_LOAD),
                "--base-url", self.primary.base_url,
                "--report", str(self.core_report),
                "--stop-file", str(self.core_stop_file),
            ]
            env = self.base_env()
            env["HACKME_SMOKE_TEST_PASSWORD"] = self.credentials.test
            env["HACKME_SMOKE_USERNAME"] = "test"
            self.core_stdout_handle = stdout_path.open("w", encoding="utf-8")
            self.core_process = subprocess.Popen(
                self.core_command,
                cwd=str(ROOT),
                env=env,
                stdout=self.core_stdout_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                self.core_identity = self.process_registry.register(
                    "load_generator",
                    self.core_process.pid,
                    required=True,
                )
            except Exception:
                terminate_process_group(self.core_process, grace_seconds=2.0)
                raise
            time.sleep(1)
            if self.core_process.poll() is not None and self.core_identity is not None:
                self.process_registry.unregister("load_generator", self.core_identity)
                self.core_identity = None
            return {
                "ok": self.core_process.poll() is None,
                "pid": self.core_process.pid,
                "stdout": str(stdout_path),
                "report": str(self.core_report),
                "command": sanitized_command(self.core_command),
                "scope": "harness_lifecycle_only",
            }
        self.core_command = [
            sys.executable,
            str(SOAK),
            "--base-url", self.primary.base_url,
            "--runtime-root", str(self.core_root),
            "--server-runtime-root", str(self.primary.runtime_root),
            "--out", str(self.core_report),
            "--duration-seconds", str(int(self.args.duration_seconds)),
            "--campaign-level", self.campaign_level,
            "--account-count", str(int(self.args.account_count)),
            "--account-prefix", f"soak{datetime.now(timezone.utc).strftime('%m%d%H%M')}",
            "--round-ops", str(int(self.args.round_ops)),
            "--concurrency", str(int(self.args.concurrency)),
            "--session-pool", str(max(int(self.args.account_count), int(self.args.session_pool))),
            "--round-timeout-seconds", "2400",
            "--sentinel-interval-seconds", "5",
            "--browser-interval-seconds", str(int(self.args.browser_interval_seconds)),
            "--max-server-busy-rate", str(float(self.args.max_server_busy_rate)),
            "--max-ordinary-p95-ms", str(float(self.args.max_ordinary_p95_ms)),
            "--max-ordinary-p99-ms", str(float(self.args.max_ordinary_p99_ms)),
            "--max-sentinel-p95-ms", str(float(self.args.max_sentinel_p95_ms)),
            "--server-pids", str(self.primary.pid()),
        ]
        if self.supervised:
            self.core_command.extend(["--stop-file", str(self.core_stop_file)])
        if activation_required:
            self.core_command.extend([
                "--campaign-uuid", self.campaign_uuid,
                "--campaign-commit", str(self.supervisor_contract.get("commit") or ""),
                "--runner-profile-digest", self.core_profile_digest,
                "--campaign-runner-pid", str(os.getpid()),
                "--campaign-runner-start-ticks", str(process_start_ticks(os.getpid())),
                "--activation-ready-file", str(self.core_ready_file),
                "--activation-file", str(self.core_activation_file),
                "--activation-ack-file", str(self.core_activation_ack_file),
                "--activation-timeout-seconds", str(CORE_READY_TIMEOUT_SECONDS),
            ])
        if self.args.allow_short_duration:
            self.core_command.append("--allow-short-duration")
        env = self.base_env()
        env.update({
            "HACKME_SOAK_ROOT_PASSWORD": self.credentials.root,
            "HACKME_SOAK_MANAGER_PASSWORD": self.credentials.manager,
            "HACKME_SOAK_TEST_PASSWORD": self.credentials.test,
            "HACKME_SOAK_ACCOUNT_PASSWORD": self.credentials.member,
            "HACKME_SERVER_PIDS": str(self.primary.pid()),
        })
        if activation_required:
            env["HACKME_CORE_ACTIVATION_NONCE"] = self.core_activation_nonce
        self.core_stdout_handle = stdout_path.open("w", encoding="utf-8")
        self.core_process_started_monotonic_ns = time.monotonic_ns()
        self.core_process = subprocess.Popen(
            self.core_command,
            cwd=str(ROOT),
            env=env,
            stdout=self.core_stdout_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        if self.supervised:
            try:
                self.core_identity = self.process_registry.register(
                    "load_generator",
                    self.core_process.pid,
                    required=True,
                )
            except Exception:
                terminate_process_group(self.core_process, grace_seconds=2.0)
                raise
        time.sleep(2)
        if (
            self.supervised
            and self.core_process.poll() is not None
            and self.core_identity is not None
        ):
            self.process_registry.unregister("load_generator", self.core_identity)
            self.core_identity = None
        return {
            "ok": self.core_process.poll() is None,
            "pid": self.core_process.pid,
            "stdout": str(stdout_path),
            "report": str(self.core_report),
            "command": sanitized_command(self.core_command),
        }

    def _core_observation(self) -> tuple[Any, ...]:
        process = self.core_process
        if process is None:
            return ("not_started",)
        rows = proc_rows()
        tree = descendants(rows, process.pid)
        cpu_ticks = sum(int(rows.get(pid, {}).get("cpu_ticks") or 0) for pid in tree)

        def path_state(path: Path) -> tuple[Any, ...]:
            try:
                info = path.lstat()
                return (
                    int(info.st_dev),
                    int(info.st_ino),
                    int(info.st_size),
                    int(info.st_mtime_ns),
                    int(info.st_mode),
                )
            except FileNotFoundError:
                return (0, 0, 0, 0, 0)

        stdout = self.core_root / "operational_soak.stdout"
        return (
            process.poll(),
            tuple(sorted(tree)),
            cpu_ticks,
            path_state(stdout),
            path_state(self.core_ready_file),
            path_state(self.core_activation_file),
            path_state(self.core_activation_ack_file),
        )

    def _stop_core_before_active(self, reason: str) -> None:
        durable_atomic_write_json(self.core_stop_file, {
            "schema_version": "hackme.campaign-load-stop.v1",
            "campaign_uuid": self.campaign_uuid,
            "reason": str(reason),
            "requested_at": utc_now(),
        })
        if self.core_process is not None and self.core_process.poll() is None:
            try:
                self.core_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminate_process_group(self.core_process, grace_seconds=2.0)
        if self.core_identity is not None:
            self.process_registry.unregister("load_generator", self.core_identity)
            self.core_identity = None

    def wait_for_core_ready(
        self,
        *,
        timeout_seconds: float = CORE_READY_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        if not (self.supervised and self.campaign_level in {"rehearsal", "formal"}):
            return {"required": False, "ok": True}
        if self.core_process is None:
            raise RuntimeError("core soak process was not launched")
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        last_observation: tuple[Any, ...] | None = None
        try:
            while time.monotonic() < deadline:
                observation = self._core_observation()
                if observation != last_observation:
                    last_observation = observation
                    self._server_progress("core_soak_preparation_observed_progress")
                if self.core_process.poll() is not None:
                    raise ActivationArtifactError(
                        f"core soak exited before readiness: {self.core_process.returncode}"
                    )
                if artifact_exists(self.core_activation_file) or artifact_exists(
                    self.core_activation_ack_file
                ):
                    raise ActivationArtifactError(
                        "core activation/ack artifact appeared before readiness acceptance"
                    )
                if artifact_exists(self.core_ready_file):
                    ready, ready_sha256 = secure_read_json(
                        self.core_ready_file,
                        authority_root=self.core_root,
                    )
                    child_start_ticks = process_start_ticks(self.core_process.pid)
                    expected = {
                        "schema_version": CORE_READY_SCHEMA_VERSION,
                        "campaign_uuid": self.campaign_uuid,
                        "campaign_commit": str(self.supervisor_contract.get("commit") or ""),
                        "runner_profile_digest": self.core_profile_digest,
                        "activation_nonce": self.core_activation_nonce,
                        "campaign_runner_pid": os.getpid(),
                        "campaign_runner_start_ticks": process_start_ticks(os.getpid()),
                        "child_pid": self.core_process.pid,
                        "child_start_ticks": child_start_ticks,
                        "ready_sequence": 1,
                    }
                    mismatched = sorted(
                        name
                        for name, expected_value in expected.items()
                        if ready.get(name) != expected_value
                    )
                    ready_ns = ready.get("ready_monotonic_ns")
                    if (
                        isinstance(ready_ns, bool)
                        or not isinstance(ready_ns, int)
                        or ready_ns < self.core_process_started_monotonic_ns
                        or ready_ns > time.monotonic_ns()
                    ):
                        mismatched.append("ready_monotonic_ns")
                    if mismatched:
                        raise ActivationArtifactError(
                            "core ready binding mismatch: " + ", ".join(sorted(set(mismatched)))
                        )
                    result = {
                        "required": True,
                        "ok": True,
                        "payload": ready,
                        "sha256": ready_sha256,
                        "path": str(self.core_ready_file),
                    }
                    self.core_ready_evidence = result
                    self._server_progress("core_soak_ready_verified")
                    return result
                time.sleep(0.05)
        except Exception:
            self._stop_core_before_active("core_ready_rejected")
            raise
        self._stop_core_before_active("core_ready_timeout")
        raise ActivationArtifactError("core soak did not publish readiness before timeout")

    def activate_core_soak(
        self,
        ready_evidence: Mapping[str, Any],
        *,
        ack_timeout_seconds: float = CORE_ACK_TIMEOUT_SECONDS,
        lead_seconds: float = CORE_ACTIVATION_LEAD_SECONDS,
    ) -> dict[str, Any]:
        if not (self.supervised and self.campaign_level in {"rehearsal", "formal"}):
            return {"required": False, "ok": True}
        if self.core_process is None or self.core_process.poll() is not None:
            raise ActivationArtifactError("core soak is not alive at activation")
        ready = ready_evidence.get("payload")
        ready_sha256 = str(ready_evidence.get("sha256") or "")
        if not isinstance(ready, Mapping) or len(ready_sha256) != 64:
            raise ActivationArtifactError("verified core readiness evidence is missing")
        now_monotonic_ns = time.monotonic_ns()
        now_epoch_ns = time.time_ns()
        activation_monotonic_ns = now_monotonic_ns + int(
            max(1.0, float(lead_seconds)) * 1_000_000_000
        )
        activation_epoch_ns = now_epoch_ns + (
            activation_monotonic_ns - now_monotonic_ns
        )
        child_start_ticks = process_start_ticks(self.core_process.pid)
        activation = {
            "schema_version": CORE_ACTIVATION_SCHEMA_VERSION,
            "campaign_uuid": self.campaign_uuid,
            "campaign_commit": str(self.supervisor_contract.get("commit") or ""),
            "runner_profile_digest": self.core_profile_digest,
            "activation_nonce": self.core_activation_nonce,
            "campaign_runner_pid": os.getpid(),
            "campaign_runner_start_ticks": process_start_ticks(os.getpid()),
            "child_pid": self.core_process.pid,
            "child_start_ticks": child_start_ticks,
            "ready_sha256": ready_sha256,
            "duration_seconds": int(self.args.duration_seconds),
            "activation_sequence": 1,
            "activation_monotonic_ns": activation_monotonic_ns,
            "activation_epoch_ns": activation_epoch_ns,
            "released_at": utc_now(),
        }
        try:
            activation_sha256 = secure_write_once_json(
                self.core_activation_file,
                activation,
                authority_root=self.core_root,
            )
            self._server_progress("core_soak_activation_published")
            deadline = min(
                activation_monotonic_ns / 1_000_000_000,
                time.monotonic() + max(1.0, float(ack_timeout_seconds)),
            )
            last_observation: tuple[Any, ...] | None = None
            while time.monotonic() < deadline:
                observation = self._core_observation()
                if observation != last_observation:
                    last_observation = observation
                    self._server_progress("core_soak_activation_observed_progress")
                if self.core_process.poll() is not None:
                    raise ActivationArtifactError(
                        f"core soak exited before activation acknowledgement: {self.core_process.returncode}"
                    )
                if artifact_exists(self.core_activation_ack_file):
                    ack, ack_sha256 = secure_read_json(
                        self.core_activation_ack_file,
                        authority_root=self.core_root,
                    )
                    current_ready, current_ready_sha256 = secure_read_json(
                        self.core_ready_file,
                        authority_root=self.core_root,
                    )
                    current_activation, current_activation_sha256 = secure_read_json(
                        self.core_activation_file,
                        authority_root=self.core_root,
                    )
                    if current_ready != dict(ready) or current_ready_sha256 != ready_sha256:
                        raise ActivationArtifactError("core ready artifact changed during activation")
                    if current_activation != activation or current_activation_sha256 != activation_sha256:
                        raise ActivationArtifactError("core activation artifact changed before acknowledgement")
                    expected_ack = {
                        "schema_version": CORE_ACK_SCHEMA_VERSION,
                        "campaign_uuid": self.campaign_uuid,
                        "campaign_commit": activation["campaign_commit"],
                        "runner_profile_digest": self.core_profile_digest,
                        "activation_nonce": self.core_activation_nonce,
                        "campaign_runner_pid": os.getpid(),
                        "campaign_runner_start_ticks": process_start_ticks(os.getpid()),
                        "child_pid": self.core_process.pid,
                        "child_start_ticks": child_start_ticks,
                        "ready_sha256": ready_sha256,
                        "activation_sha256": activation_sha256,
                        "activation_monotonic_ns": activation_monotonic_ns,
                        "ack_sequence": 1,
                    }
                    mismatched = sorted(
                        name
                        for name, expected_value in expected_ack.items()
                        if ack.get(name) != expected_value
                    )
                    ack_ns = ack.get("acknowledged_monotonic_ns")
                    if (
                        isinstance(ack_ns, bool)
                        or not isinstance(ack_ns, int)
                        or ack_ns < int(ready.get("ready_monotonic_ns") or 0)
                        or ack_ns > activation_monotonic_ns
                    ):
                        mismatched.append("acknowledged_monotonic_ns")
                    if mismatched:
                        raise ActivationArtifactError(
                            "core activation acknowledgement mismatch: "
                            + ", ".join(sorted(set(mismatched)))
                        )
                    result = {
                        "required": True,
                        "ok": True,
                        "ready_sha256": ready_sha256,
                        "activation_sha256": activation_sha256,
                        "ack_sha256": ack_sha256,
                        "activation_monotonic_ns": activation_monotonic_ns,
                        "activation_epoch_ns": activation_epoch_ns,
                        "activated_at": datetime.fromtimestamp(
                            activation_epoch_ns / 1_000_000_000,
                            tz=timezone.utc,
                        ).replace(microsecond=0).isoformat(),
                        "paths": {
                            "ready": str(self.core_ready_file),
                            "activation": str(self.core_activation_file),
                            "ack": str(self.core_activation_ack_file),
                        },
                    }
                    self.core_activation_evidence = result
                    self._server_progress("core_soak_activation_ack_verified")
                    return result
                time.sleep(0.05)
        except Exception:
            self._stop_core_before_active("core_activation_rejected")
            raise
        self._stop_core_before_active("core_activation_ack_timeout")
        raise ActivationArtifactError(
            "core soak did not acknowledge activation before its scheduled start"
        )

    def core_activation_artifacts_intact(self) -> bool:
        if not (self.supervised and self.campaign_level in {"rehearsal", "formal"}):
            return True
        expected = self.core_activation_evidence
        if not expected.get("ok"):
            return False
        try:
            _ready, ready_sha256 = secure_read_json(
                self.core_ready_file,
                authority_root=self.core_root,
            )
            _activation, activation_sha256 = secure_read_json(
                self.core_activation_file,
                authority_root=self.core_root,
            )
            _ack, ack_sha256 = secure_read_json(
                self.core_activation_ack_file,
                authority_root=self.core_root,
            )
        except ActivationArtifactError:
            return False
        return bool(
            ready_sha256 == expected.get("ready_sha256")
            and activation_sha256 == expected.get("activation_sha256")
            and ack_sha256 == expected.get("ack_sha256")
        )

    def scenario_specs(self) -> list[ScenarioSpec]:
        # The fixed 180-second smoke is a Level-0 lifecycle gate.  It proves
        # supervisor/cgroup/watchdog/readiness/load/monitor teardown, but must
        # never claim the full product matrix completed in three minutes.
        # Rehearsal and formal campaigns retain mandatory 100% coverage.
        if self.supervised and self.campaign_level == "smoke":
            return []
        targets = {
            "backup_restore_restart": "recovery",
            "media_proxy_cross_browser": "isolated",
        }
        return [
            ScenarioSpec(
                scenario_id,
                contract.category,
                targets.get(scenario_id, "primary"),
                contract.scheduled_fraction,
                lambda scenario_id=scenario_id: self.run_formal_native_scenario(scenario_id),
            )
            for scenario_id, contract in CAMPAIGN_SCENARIO_CONTRACTS.items()
        ]

    def scenario_worker(self, spec: ScenarioSpec) -> None:
        self.active_event.wait()
        delay = max(0.0, float(self.args.duration_seconds) * max(0.0, min(1.0, spec.fraction)))
        while not self.stop_event.is_set() and self.elapsed() < delay:
            self.stop_event.wait(min(5.0, max(0.1, delay - self.elapsed())))
        if self.stop_event.is_set():
            result = {"ok": False, "error": "campaign_stopped_before_scenario", "scheduled_fraction": spec.fraction}
        else:
            drift_before = self.check_drift()
            started_at = utc_now()
            started_elapsed = self.elapsed()
            try:
                result = spec.runner() if not drift_before else {"ok": False, "error": "source_drift_before_scenario"}
            except Exception as exc:
                result = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            drift_after = self.check_drift()
            result.update({
                "scenario_id": spec.scenario_id,
                "category": spec.category,
                "target": spec.target,
                "mandatory": spec.mandatory,
                "scheduled_fraction": spec.fraction,
                "started_at": started_at,
                "started_active_seconds": round(started_elapsed, 3),
                "finished_active_seconds": round(self.elapsed(), 3),
                "source_drift_before": drift_before,
                "source_drift_after": drift_after,
                "ok": bool(result.get("ok") and not drift_before and not drift_after),
            })
        with self.lock:
            self.scenario_results[spec.scenario_id] = result
        self.write_checkpoint(f"scenario_{spec.scenario_id}_complete")

    def start_scenarios(self) -> None:
        for spec in self.scenario_specs():
            thread = threading.Thread(target=self.scenario_worker, args=(spec,), daemon=True, name=f"campaign-{spec.scenario_id}")
            self.scenario_threads.append(thread)
            thread.start()

    def scan_server_logs(self, controllers: list[ServerController]) -> dict[str, Any]:
        patterns = {
            "database_locked": re.compile(b"database is locked|database table is locked", re.I),
            "traceback": re.compile(b"Traceback \\(most recent call last\\):"),
            "uncaught": re.compile(b"uncaught exception|unhandled exception", re.I),
            "oom": re.compile(b"out of memory|oom-kill|killed process", re.I),
        }
        result: dict[str, Any] = {}
        for controller in controllers:
            counts = {name: 0 for name in patterns}
            samples = {name: [] for name in patterns}
            errors: list[dict[str, Any]] = []
            snapshots: list[dict[str, Any]] = []
            scanned_bytes = 0
            log_dir = controller.runtime_root / "logs"
            log_paths: list[Path] = []
            discovered_identities: dict[Path, tuple[int, int]] = {}
            seen_identities: dict[tuple[int, int], Path] = {}
            duplicate_paths: list[dict[str, str]] = []
            discovery_errors: list[dict[str, Any]] = []
            discovery_entries = 0
            discovery_since_progress = 0
            discovery_next_progress = (
                time.monotonic() + LOG_SCAN_DISCOVERY_PROGRESS_SECONDS
            )
            try:
                with os.scandir(log_dir) as iterator:
                    for entry in iterator:
                        discovery_entries += 1
                        discovery_since_progress += 1
                        if discovery_entries > LOG_SCAN_MAX_DIRECTORY_ENTRIES:
                            discovery_errors.append({
                                "path": str(log_dir),
                                "code": "server_log_directory_entry_limit_exceeded",
                                "maximum_entries": LOG_SCAN_MAX_DIRECTORY_ENTRIES,
                            })
                            break
                        if (
                            discovery_since_progress >= LOG_SCAN_DISCOVERY_PROGRESS_ENTRIES
                            or time.monotonic() >= discovery_next_progress
                        ):
                            self._server_progress(
                                f"audit_log_discovery_progress:{controller.name}:{discovery_entries}"
                            )
                            discovery_since_progress = 0
                            discovery_next_progress = (
                                time.monotonic() + LOG_SCAN_DISCOVERY_PROGRESS_SECONDS
                            )
                        if not entry.name.endswith((".log", ".out")):
                            continue
                        try:
                            if entry.is_symlink():
                                discovery_errors.append({
                                    "path": entry.path,
                                    "code": "server_log_symlink_rejected",
                                })
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                discovery_errors.append({
                                    "path": entry.path,
                                    "code": "server_log_candidate_not_regular_file",
                                })
                                continue
                            info = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            discovery_errors.append({
                                "path": entry.path,
                                "code": "server_log_discovery_stat_failed",
                                "error": f"{exc.__class__.__name__}: {exc}",
                            })
                            continue
                        identity = (int(info.st_dev), int(info.st_ino))
                        candidate = Path(entry.path)
                        if identity in seen_identities:
                            if len(duplicate_paths) < 100:
                                duplicate_paths.append({
                                    "path": str(candidate),
                                    "same_inode_as": str(seen_identities[identity]),
                                })
                            continue
                        if len(log_paths) >= LOG_SCAN_MAX_FILES:
                            discovery_errors.append({
                                "path": str(log_dir),
                                "code": "server_log_file_limit_exceeded",
                                "maximum_files": LOG_SCAN_MAX_FILES,
                            })
                            break
                        seen_identities[identity] = candidate
                        discovered_identities[candidate] = identity
                        log_paths.append(candidate)
            except Exception as exc:
                discovery_errors.append({
                    "path": str(log_dir),
                    "code": "server_log_directory_scan_failed",
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
            log_paths.sort(key=lambda value: str(value))
            errors.extend(discovery_errors)
            for path in log_paths:
                snapshot: dict[str, Any] = {
                    "path": str(path),
                    "initial_bytes": 0,
                    "scanned_bytes": 0,
                }
                try:
                    if path.is_symlink():
                        raise OSError("server log symlink rejected")
                    bytes_since_progress = 0
                    next_progress_at = time.monotonic() + LOG_SCAN_PROGRESS_SECONDS
                    carry = b""
                    file_bytes = 0
                    finalized_match_start = 0
                    with path.open("rb") as handle:
                        initial = os.fstat(handle.fileno())
                        initial_size = int(initial.st_size)
                        snapshot.update({
                            "device": int(initial.st_dev),
                            "inode": int(initial.st_ino),
                            "initial_bytes": initial_size,
                            "initial_mtime_ns": int(initial.st_mtime_ns),
                            "initial_ctime_ns": int(initial.st_ctime_ns),
                        })
                        if not stat.S_ISREG(initial.st_mode):
                            raise OSError("server log is not a regular file")
                        if (initial.st_dev, initial.st_ino) != discovered_identities[path]:
                            raise OSError(
                                "server log changed between discovery and opened snapshot"
                            )
                        opened_path = path.stat()
                        if (opened_path.st_dev, opened_path.st_ino) != (
                            initial.st_dev,
                            initial.st_ino,
                        ):
                            raise OSError("server log path changed while opening snapshot")
                        while file_bytes < initial_size:
                            chunk = handle.read(
                                min(
                                    LOG_SCAN_CHUNK_CHARACTERS,
                                    initial_size - file_bytes,
                                )
                            )
                            if not chunk:
                                errors.append({
                                    "path": str(path),
                                    "code": "server_log_truncated_during_snapshot",
                                    "expected_bytes": initial_size,
                                    "scanned_bytes": file_bytes,
                                })
                                break
                            window_start = file_bytes - len(carry)
                            file_bytes += len(chunk)
                            window = carry + chunk
                            finalize_before = (
                                file_bytes
                                if file_bytes >= initial_size
                                else max(
                                    finalized_match_start,
                                    file_bytes - LOG_SCAN_OVERLAP_CHARACTERS,
                                )
                            )
                            for name, pattern in patterns.items():
                                for match in pattern.finditer(window):
                                    absolute_start = window_start + match.start()
                                    if not (
                                        finalized_match_start
                                        <= absolute_start
                                        < finalize_before
                                    ):
                                        continue
                                    counts[name] += 1
                                    if len(samples[name]) < 10:
                                        start = max(0, match.start() - 120)
                                        end = min(len(window), match.end() + 240)
                                        samples[name].append({
                                            "path": str(path),
                                            "text": window[start:end].decode(
                                                "utf-8", errors="replace"
                                            ).replace("\n", " ")[:500],
                                        })
                            scanned_bytes += len(chunk)
                            bytes_since_progress += len(chunk)
                            finalized_match_start = finalize_before
                            carry = window[finalize_before - window_start:]
                            if (
                                bytes_since_progress >= LOG_SCAN_PROGRESS_CHARACTERS
                                or time.monotonic() >= next_progress_at
                            ):
                                self._server_progress(
                                    f"audit_log_scan_progress:{controller.name}:{path.name}"
                                )
                                bytes_since_progress = 0
                                next_progress_at = time.monotonic() + LOG_SCAN_PROGRESS_SECONDS
                        snapshot["scanned_bytes"] = file_bytes
                        final_fd = os.fstat(handle.fileno())
                        snapshot["final_fd_bytes"] = int(final_fd.st_size)
                        if (final_fd.st_dev, final_fd.st_ino) != (
                            initial.st_dev,
                            initial.st_ino,
                        ):
                            errors.append({
                                "path": str(path),
                                "code": "server_log_fd_identity_changed",
                            })
                        if int(final_fd.st_size) < initial_size:
                            errors.append({
                                "path": str(path),
                                "code": "server_log_truncated_during_snapshot",
                                "initial_bytes": initial_size,
                                "final_bytes": int(final_fd.st_size),
                            })
                        elif int(final_fd.st_size) > initial_size:
                            errors.append({
                                "path": str(path),
                                "code": "server_log_appended_during_snapshot",
                                "initial_bytes": initial_size,
                                "final_bytes": int(final_fd.st_size),
                            })
                        if (
                            int(final_fd.st_mtime_ns) != int(initial.st_mtime_ns)
                            or int(final_fd.st_ctime_ns) != int(initial.st_ctime_ns)
                        ):
                            errors.append({
                                "path": str(path),
                                "code": "server_log_metadata_changed_during_snapshot",
                                "initial_mtime_ns": int(initial.st_mtime_ns),
                                "final_mtime_ns": int(final_fd.st_mtime_ns),
                                "initial_ctime_ns": int(initial.st_ctime_ns),
                                "final_ctime_ns": int(final_fd.st_ctime_ns),
                            })
                        try:
                            final_path = path.stat()
                        except OSError as exc:
                            errors.append({
                                "path": str(path),
                                "code": "server_log_path_missing_after_snapshot",
                                "error": f"{exc.__class__.__name__}: {exc}",
                            })
                        else:
                            snapshot["final_path_bytes"] = int(final_path.st_size)
                            if (final_path.st_dev, final_path.st_ino) != (
                                initial.st_dev,
                                initial.st_ino,
                            ):
                                errors.append({
                                    "path": str(path),
                                    "code": "server_log_replaced_or_rotated_during_snapshot",
                                })
                            if int(final_path.st_size) != initial_size:
                                errors.append({
                                    "path": str(path),
                                    "code": "server_log_path_size_changed_during_snapshot",
                                    "initial_bytes": initial_size,
                                    "final_bytes": int(final_path.st_size),
                                })
                except Exception as exc:
                    errors.append({
                        "path": str(path),
                        "code": "server_log_snapshot_error",
                        "error": f"{exc.__class__.__name__}: {exc}",
                    })
                    self._server_progress(
                        f"audit_log_scan_error:{controller.name}:{path.name}"
                    )
                    snapshots.append(snapshot)
                    continue
                snapshots.append(snapshot)
                self._server_progress(f"audit_log_scanned:{controller.name}:{path.name}")
            result[controller.name] = {
                "counts": counts,
                "samples": samples,
                "paths": [str(path) for path in log_paths],
                "discovery": {
                    "entries_scanned": discovery_entries,
                    "maximum_directory_entries": LOG_SCAN_MAX_DIRECTORY_ENTRIES,
                    "files": len(log_paths),
                    "maximum_files": LOG_SCAN_MAX_FILES,
                    "duplicate_paths": duplicate_paths,
                    "errors": discovery_errors,
                    "ok": not discovery_errors,
                },
                "scanned_bytes": scanned_bytes,
                # Compatibility for existing machine consumers; offsets are
                # now byte-based so evidence remains bounded and immutable.
                "scanned_characters": scanned_bytes,
                "snapshots": snapshots,
                "errors": errors,
            }
        return result

    def final_control_checks(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for controller in (self.primary, self.recovery):
            self._server_progress(f"audit_readiness_started:{controller.name}")
            evidence = controller.wait_ready(timeout=180.0)
            self._server_progress(f"audit_readiness_completed:{controller.name}")
            result[controller.name] = {
                "ok": bool(evidence.get("ok")),
                "layered_readiness": evidence.get("layered") or evidence,
            }
        return result

    def seal_artifact_writers_for_secret_scan(self) -> dict[str, Any]:
        """Prove that scan progress can no longer mutate the artifact tree."""

        errors: list[dict[str, Any]] = []
        external_paths: dict[str, str] = {}
        if self.source_freezer is not None:
            try:
                self.source_freezer.close()
            except Exception as exc:
                errors.append({
                    "code": "runner_source_monitor_close_failed",
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
        if self.resource_monitor.is_alive():
            errors.append({"code": "resource_monitor_still_writing"})
        live_scenarios = [thread.name for thread in self.scenario_threads if thread.is_alive()]
        if live_scenarios:
            errors.append({
                "code": "scenario_writer_still_alive",
                "threads": live_scenarios,
            })
        live_steps = [process.pid for process in self.step_processes if process.poll() is None]
        if live_steps:
            errors.append({"code": "step_process_writer_still_alive", "pids": live_steps})
        if self.core_process is not None and self.core_process.poll() is None:
            errors.append({
                "code": "core_load_writer_still_alive",
                "pid": self.core_process.pid,
            })
        if self.core_stdout_handle is not None:
            errors.append({"code": "core_stdout_writer_still_open"})
        live_servers = {
            controller.name: controller.pid()
            for controller in (self.primary, self.recovery, self.security_sentinel)
            if controller.pid() > 0
        }
        if live_servers:
            errors.append({"code": "server_writer_still_alive", "servers": live_servers})
        if self.supervised:
            try:
                expected_control_root = validate_control_root(self.root, self.control_root)
                root_info = expected_control_root.lstat()
                if (
                    not stat.S_ISDIR(root_info.st_mode)
                    or stat.S_ISLNK(root_info.st_mode)
                    or int(root_info.st_uid) != os.getuid()
                    or stat.S_IMODE(root_info.st_mode) & 0o077
                ):
                    raise RuntimeError("control root is not an owned private directory")
            except Exception as exc:
                errors.append({
                    "code": "external_control_root_invalid",
                    "path": str(self.control_root),
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
            controlled = {
                "state": self.state_path,
                "control": self.control_path,
                "heartbeat": self.heartbeat_path,
                "checkpoint": self.checkpoint_path,
                "watchdog_ready": self.watchdog_ready_path,
                "activation_gate": self.activation_gate_path,
                "supervisor_contract": self.supervisor_contract_path,
                "source_freeze": self.source_freeze_path,
            }
            for name, path in controlled.items():
                resolved = Path(path).resolve(strict=False)
                external_paths[name] = str(resolved)
                if (
                    resolved == self.root
                    or self.root in resolved.parents
                    or (
                        resolved != self.control_root
                        and self.control_root not in resolved.parents
                    )
                ):
                    errors.append({
                        "code": "live_control_writer_inside_artifact_root",
                        "writer": name,
                        "path": str(resolved),
                    })
            for name in ("runner_stdout", "watchdog_stdout", "supervisor_source_root"):
                raw = str(self.supervisor_contract.get(name) or "")
                if not raw:
                    errors.append({"code": "supervisor_writer_path_missing", "writer": name})
                    continue
                resolved = Path(raw).resolve(strict=False)
                external_paths[name] = str(resolved)
                if resolved != self.control_root and self.control_root not in resolved.parents:
                    errors.append({
                        "code": "supervisor_writer_not_external",
                        "writer": name,
                        "path": str(resolved),
                    })
        return {
            "schema_version": "hackme.campaign-artifact-writer-seal.v1",
            "ok": not errors,
            "artifact_root": str(self.root),
            "control_root": str(self.control_root),
            "external_live_writer_paths": external_paths,
            "heartbeat_pump_external": bool(
                not self.supervised
                or (
                    self.heartbeat_path != self.root
                    and self.root not in self.heartbeat_path.parents
                )
            ),
            "errors": errors,
        }

    def secret_scan(self) -> dict[str, Any]:
        values = build_sensitive_needle_inventory({
            "root": self.credentials.root,
            "manager": self.credentials.manager,
            "test": self.credentials.test,
            "member": self.credentials.member,
        }, environment=os.environ)
        writer_seal = self.seal_artifact_writers_for_secret_scan()
        checkpoint_mirror_snapshot: dict[str, Any] = {
            "schema_version": "hackme.campaign-control-snapshot.v1",
            "ok": self.checkpoint_mirror_path is None,
            "required": bool(self.supervised and self.checkpoint_mirror_path is not None),
        }
        control_snapshot: dict[str, Any] = {
            "schema_version": "hackme.campaign-control-snapshot.v1",
            "ok": not self.supervised,
            "required": bool(self.supervised),
        }
        if self.supervised and writer_seal.get("ok"):
            if self.checkpoint_mirror_path is not None:
                checkpoint_mirror_snapshot = snapshot_control_evidence(
                    ControlSnapshotConfig(
                        source_root=self.checkpoint_mirror_path.parent,
                        snapshot_root=(
                            self.root
                            / "artifacts"
                            / "runner_checkpoint_mirror_snapshot"
                        ),
                    ),
                    progress_callback=self._server_progress,
                )
                checkpoint_mirror_snapshot["required"] = True
            control_snapshot = snapshot_control_evidence(
                ControlSnapshotConfig(
                    source_root=self.control_root,
                    snapshot_root=self.root / "artifacts" / "runner_control_snapshot",
                    progress_bytes=CAMPAIGN_CONTROL_SNAPSHOT_PROGRESS_BYTES,
                    progress_entries=CAMPAIGN_CONTROL_SNAPSHOT_PROGRESS_ENTRIES,
                    progress_seconds=CAMPAIGN_CONTROL_SNAPSHOT_PROGRESS_SECONDS,
                ),
                progress_callback=self._server_progress,
            )
            control_snapshot["required"] = True
        result = scan_campaign_secrets(
            SecretScanConfig(
                artifact_root=self.root,
                needles=values,
                controlled_runtime_roots=(
                    self.primary.runtime_root,
                    self.recovery.runtime_root,
                    self.security_sentinel.runtime_root,
                ),
                progress_bytes=CAMPAIGN_SECRET_SCAN_PROGRESS_BYTES,
                progress_entries=CAMPAIGN_SECRET_SCAN_PROGRESS_ENTRIES,
                progress_seconds=CAMPAIGN_SECRET_SCAN_PROGRESS_SECONDS,
            ),
            progress_callback=self._server_progress if self.supervised else None,
        )
        artifact_cutoff_at = utc_now()
        contract_paths = {
            name: str(self.supervisor_contract.get(name) or "")
            for name in (
                "runner_stdout",
                "watchdog_stdout",
                "supervisor_source_root",
                "supervisor_final_result",
                "authoritative_secret_scan_receipt",
            )
            if str(self.supervisor_contract.get(name) or "")
        }
        post_scan_artifacts = [
            {
                "path": str(self.final_path),
                "kind": "runner_final_report",
                "coverage": "not_yet_created; authoritative supervisor scan required",
            },
            *[
                {
                    "path": str(path),
                    "kind": f"live_control:{name}",
                    "coverage": "snapshot_at_runner_cutoff_only; may change after cutoff",
                }
                for name, path in (
                    ("state", self.state_path),
                    ("control", self.control_path),
                    ("heartbeat", self.heartbeat_path),
                    ("checkpoint", self.checkpoint_path),
                    ("checkpoint_mirror", self.checkpoint_mirror_path),
                    ("watchdog_ready", self.watchdog_ready_path),
                )
                if path is not None
            ],
            *[
                {
                    "path": path,
                    "kind": name,
                    "coverage": "external writer; authoritative supervisor scan required",
                }
                for name, path in sorted(contract_paths.items())
            ],
        ]
        result.update({
            "ok": bool(
                result.get("ok")
                and writer_seal.get("ok")
                and control_snapshot.get("ok")
                and checkpoint_mirror_snapshot.get("ok")
            ),
            "authoritative_final_root_scan": False,
            "artifact_cutoff_at": artifact_cutoff_at,
            "writer_seal": writer_seal,
            "control_snapshot": control_snapshot,
            "checkpoint_mirror_snapshot": checkpoint_mirror_snapshot,
            "post_scan_artifacts": post_scan_artifacts,
        })
        return result

    def run(self) -> int:
        self.root.mkdir(parents=True, exist_ok=self.supervised)
        self.reports.mkdir(parents=True, exist_ok=True)
        self.start_heartbeat_pump()
        self.write_checkpoint("runner_preflight")
        preflight = self.preflight()
        if not preflight.get("ok"):
            self.mark_failed(reason="PREFLIGHT_FAILED")
            payload = {"ok": False, "verdict": "FAIL_HARNESS", "classification": "FAIL_HARNESS", "phase": "preflight", "preflight": preflight}
            atomic_write_json(self.final_path, payload)
            return 2
        self.write_checkpoint("starting_primary")
        primary_start = self.primary.start()
        self.write_checkpoint("starting_recovery")
        recovery_start = self.recovery.start()
        self.write_checkpoint("starting_security_sentinel")
        security_start = self.security_sentinel.start()
        if not primary_start.get("ok") or not recovery_start.get("ok") or not security_start.get("ok"):
            self.mark_failed(reason="SERVER_START_FAILED")
            payload = {
                "ok": False,
                "verdict": "FAIL_HARNESS",
                "classification": "FAIL_HARNESS",
                "phase": "server_start",
                "primary_start": primary_start,
                "recovery_start": recovery_start,
                "security_start": security_start,
            }
            atomic_write_json(self.final_path, payload)
            self.primary.stop(reason="start_failure_cleanup")
            self.recovery.stop(reason="start_failure_cleanup")
            self.security_sentinel.stop(reason="start_failure_cleanup")
            return 2
        self.write_checkpoint("production_security_sentinel_preflight")
        security_preflight = self.production_security_sentinel_check(phase="preflight")
        if not security_preflight.get("ok"):
            self.mark_failed(reason="PRODUCTION_SECURITY_SENTINEL_FAILED")
            payload = {
                "ok": False,
                "verdict": "FAIL_HARNESS",
                "classification": "FAIL_HARNESS",
                "phase": "production_security_sentinel",
                "security_preflight": security_preflight,
            }
            atomic_write_json(self.final_path, payload)
            self.primary.stop(reason="security_preflight_failure")
            self.recovery.stop(reason="security_preflight_failure")
            self.security_sentinel.stop(reason="security_preflight_failure")
            return 2
        self.write_checkpoint("provisioning_campaign_accounts")
        self.provision_accounts()
        self.write_checkpoint("starting_continuous_load")
        core_start = self.start_core_soak()
        if not core_start.get("ok"):
            raise RuntimeError(f"core soak failed to start: {core_start}")
        core_ready = self.wait_for_core_ready()
        self.write_checkpoint("core_soak_ready_verified")
        self.resource_monitor.start()
        initial_resource_sample = self.wait_for_initial_resource_sample()
        atomic_write_json(
            self.reports / "resources" / "initial_resource_gate.json",
            initial_resource_sample,
        )
        self.start_scenarios()
        core_activation = self.activate_core_soak(core_ready)
        activation_monotonic_ns = int(
            core_activation.get("activation_monotonic_ns") or time.monotonic_ns()
        )
        self.active_started = activation_monotonic_ns / 1_000_000_000
        self.active_started_at = str(core_activation.get("activated_at") or utc_now())
        supervised_segment_completed = False
        if self.state_machine is not None:
            conditions = self._active_conditions(activating=True)
            try:
                state = self.state_machine.start_active(
                    conditions,
                    now_ns=activation_monotonic_ns,
                )
            except CampaignStateError as exc:
                self.mark_failed(reason="ACTIVE_CONDITIONS_NOT_VERIFIED")
                raise RuntimeError(f"formal active conditions failed closed: {exc}") from exc
            self._write_control_from_state(state)
        last_activation_observation: tuple[Any, ...] | None = None
        while time.monotonic_ns() < activation_monotonic_ns:
            if self.stop_event.is_set():
                raise RuntimeError("campaign stopped before synchronized activation edge")
            if self.state_machine is not None:
                activation_state = self.state_machine.snapshot()
                activation_control = activation_state.get("control") or {}
                if (
                    activation_state.get("state") != CampaignState.ACTIVE.value
                    or activation_control.get("admit_new_jobs") is not True
                    or activation_control.get("load_generator_should_run") is not True
                ):
                    self._stop_core_before_active(
                        "campaign_control_closed_before_activation_edge"
                    )
                    raise RuntimeError(
                        "campaign control closed before synchronized activation edge"
                    )
            if self.core_process is None or self.core_process.poll() is not None:
                raise RuntimeError("core soak exited before synchronized activation edge")
            if not self.core_activation_artifacts_intact():
                raise RuntimeError("core activation artifacts changed before ACTIVE edge")
            observation = self._core_observation()
            if observation != last_activation_observation:
                last_activation_observation = observation
                self._server_progress("synchronized_activation_wait_observed_progress")
            remaining = (activation_monotonic_ns - time.monotonic_ns()) / 1e9
            time.sleep(max(0.001, min(0.05, remaining)))
        if self.core_process is None or self.core_process.poll() is not None:
            raise RuntimeError("core soak was not alive at synchronized activation edge")
        if not self.core_activation_artifacts_intact():
            self._stop_core_before_active("core_activation_changed_at_active_edge")
            raise RuntimeError("core activation artifacts changed at ACTIVE edge")
        if self.state_machine is not None:
            activation_state = self.state_machine.snapshot()
            activation_control = activation_state.get("control") or {}
            if (
                activation_state.get("state") != CampaignState.ACTIVE.value
                or activation_control.get("admit_new_jobs") is not True
                or activation_control.get("load_generator_should_run") is not True
            ):
                self._stop_core_before_active(
                    "campaign_control_closed_at_activation_edge"
                )
                raise RuntimeError("campaign control closed at synchronized ACTIVE edge")
        self.mark_main_loop_progress("synchronized_active_edge_reached")
        self.active_event.set()
        self.write_checkpoint("active_campaign_started")
        next_heartbeat = 0.0
        while self.core_process and self.core_process.poll() is None and not self.stop_event.is_set():
            self.mark_main_loop_progress("active_campaign_loop")
            if self.state_machine is not None:
                external_control = load_json(self.control_path)
                if (
                    external_control.get("admit_new_jobs") is False
                    and external_control.get("state") in {"STOPPING_LOAD", "PRESERVING_EVIDENCE", "INTERRUPTED", "FAILED"}
                ):
                    self.stop_event.set()
                    break
                now_ns = time.monotonic_ns()
                conditions = self._active_conditions()
                failed_conditions = sorted(name for name, value in conditions.items() if not value)
                if failed_conditions:
                    self.request_hard_stop(
                        reason="ACTIVE_CONDITION_LOST",
                        classification="FAIL_HARNESS",
                        evidence={"failed_conditions": failed_conditions},
                    )
                    break
                state = self.state_machine.tick_active(conditions, now_ns=now_ns)
                active_clock = float((state.get("clock") or {}).get("continuous_active_seconds") or 0.0)
                if active_clock >= int(self.args.duration_seconds):
                    unfinished_at_deadline = [thread.name for thread in self.scenario_threads if thread.is_alive()]
                    missing_at_deadline = [
                        spec.scenario_id
                        for spec in self.scenario_specs()
                        if spec.mandatory and spec.scenario_id not in self.scenario_results
                    ]
                    if self.campaign_level in {"formal", "rehearsal"} and (unfinished_at_deadline or missing_at_deadline):
                        self.request_hard_stop(
                            reason="MANDATORY_SCENARIO_DEADLINE",
                            classification="FAIL_PRODUCT",
                            evidence={
                                "unfinished_threads": unfinished_at_deadline,
                                "missing_scenarios": missing_at_deadline,
                            },
                        )
                        break
                    state = self.state_machine.finish_active(now_ns=now_ns)
                    self._write_control_from_state(state)
                    durable_atomic_write_json(self.core_stop_file, {
                        "schema_version": "hackme.campaign-load-stop.v1",
                        "campaign_uuid": self.campaign_uuid,
                        "reason": "required_continuous_active_duration_completed",
                        "requested_at": utc_now(),
                    })
                    supervised_segment_completed = True
                    if self.campaign_level == "smoke":
                        self.stop_event.set()
                        self.stop_managed_steps()
                    break
            if self.elapsed() >= next_heartbeat:
                self.check_drift()
                if self.drift and self.state_machine is not None:
                    self.request_hard_stop(
                        reason="SOURCE_DRIFT",
                        classification="INVALIDATED",
                        evidence={"source_drift": self.drift},
                    )
                    break
                self.write_checkpoint("core_soak_running")
                print(json.dumps({
                    "event": "campaign_heartbeat",
                    "active_test_seconds": round(self.elapsed(), 1),
                    "remaining_seconds": round(max(0.0, int(self.args.duration_seconds) - self.elapsed()), 1),
                    "scenarios_completed": len(self.scenario_results),
                    "core_pid": self.core_process.pid,
                }, ensure_ascii=False), flush=True)
                next_heartbeat = self.elapsed() + max(10.0, float(self.args.heartbeat_interval))
            self.stop_event.wait(2)

        if supervised_segment_completed and self.core_process and self.core_process.poll() is None:
            try:
                self.core_process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                self.request_hard_stop(
                    reason="LOAD_GENERATOR_DID_NOT_FINALIZE",
                    classification="FAIL_HARNESS",
                    evidence={"pid": self.core_process.pid},
                )

        core_returncode = self.core_process.poll() if self.core_process else 127
        if self.state_machine is not None and not supervised_segment_completed and not self.stop_event.is_set():
            self.request_hard_stop(
                reason="LOAD_GENERATOR_EXITED_EARLY",
                classification="FAIL_HARNESS",
                evidence={"returncode": core_returncode},
            )
        if not self.required_duration_completed():
            # Wake delayed scenarios immediately when the continuous load driver
            # dies early; otherwise a failed smoke can wait for the full join timeout.
            self.stop_event.set()
        if self.core_process and core_returncode is None:
            terminate_process_group(self.core_process)
            core_returncode = self.core_process.poll()
        if self.core_identity is not None:
            self.process_registry.unregister("load_generator", self.core_identity)
            self.core_identity = None
        if self.core_stdout_handle:
            self.core_stdout_handle.close()
            self.core_stdout_handle = None

        scenario_join_deadline = time.monotonic() + int(self.args.scenario_join_timeout_seconds)
        for thread in self.scenario_threads:
            remaining = max(0.0, scenario_join_deadline - time.monotonic())
            thread.join(timeout=remaining)
        unfinished = [thread.name for thread in self.scenario_threads if thread.is_alive()]
        if unfinished:
            self.stop_event.set()
        self.check_drift()
        self.write_checkpoint("collecting_final_evidence")
        if self.state_machine is not None:
            current_state = CampaignState(self.state_machine.snapshot()["state"])
            if current_state == CampaignState.COMPLETED:
                audit_state = self.state_machine.transition(
                    CampaignState.AUDITING,
                    reason="collecting_and_validating_final_evidence",
                )
                self._write_control_from_state(audit_state)

        control_checks = self.final_control_checks()
        self._server_progress("audit_control_checks_complete")
        security_final = self.production_security_sentinel_check(phase="final")
        self._server_progress("audit_security_sentinel_complete")
        self.resource_monitor.stop()
        self.resource_monitor.join(timeout=30)
        resources = self.resource_monitor.summary()
        self._server_progress("audit_resource_summary_complete")
        active_seconds = self.elapsed()
        if self.state_machine is not None:
            authoritative_clock = self.state_machine.snapshot().get("clock") or {}
            active_seconds = float(
                authoritative_clock.get("continuous_active_seconds") or 0.0
            )
        expected_core_schema = (
            SMOKE_LOAD_REPORT_SCHEMA_VERSION
            if self.supervised and self.campaign_level == "smoke"
            else OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION
        )
        core_payload, core_report_validation = load_bounded_child_report(
            self.core_report,
            expected_schema=expected_core_schema,
            progress_callback=self._server_progress if self.supervised else None,
        )
        self._server_progress("audit_core_report_validation_complete")
        core_accounts = core_payload.get("accounts") if isinstance(core_payload.get("accounts"), list) else []
        core_checkpoint_validation: dict[str, Any] = {
            "schema_version": CORE_REPORT_VALIDATION_SCHEMA_VERSION,
            "ok": True,
            "used": False,
        }
        if not core_accounts and expected_core_schema == OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION:
            core_checkpoint, core_checkpoint_validation = load_bounded_child_report(
                self.core_root / "reports" / "operational_soak" / "operational_soak.checkpoint.json",
                expected_schema=None,
                progress_callback=self._server_progress if self.supervised else None,
                max_bytes=CORE_CHECKPOINT_MAX_BYTES,
                validate_cardinality=False,
            )
            core_checkpoint_validation["used"] = True
            if isinstance(core_checkpoint.get("accounts"), list):
                core_accounts = core_checkpoint["accounts"]
        account_cleanup = self.cleanup_campaign_accounts(
            additional_usernames=[str(value) for value in core_accounts],
        )
        self._server_progress("audit_account_cleanup_complete")
        log_seal_stops: dict[str, dict[str, Any]] = {}
        for controller in (self.primary, self.recovery, self.security_sentinel):
            self._server_progress(f"audit_log_seal_stop_started:{controller.name}")
            try:
                stopped = controller.stop(reason="final_evidence_log_seal")
            except Exception as exc:
                stopped = {
                    "ok": False,
                    "name": controller.name,
                    "reason": "final_evidence_log_seal",
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            log_seal_stops[controller.name] = stopped
            self._server_progress(
                f"audit_log_seal_stop_completed:{controller.name}:{int(bool(stopped.get('ok')))}"
            )
        # This must remain the final server-log snapshot: every final readiness,
        # security, and account-cleanup request above can emit the very errors
        # the audit is intended to catch.  All writers are sealed first so an
        # append after the descriptor's initial length cannot evade inspection.
        server_logs = self.scan_server_logs(
            [self.primary, self.recovery, self.security_sentinel]
        )
        self._server_progress("audit_server_log_scan_complete")
        secret_scan = self.secret_scan()
        self._server_progress("audit_secret_scan_complete")
        specs = self.scenario_specs()
        missing_scenarios = [spec.scenario_id for spec in specs if spec.scenario_id not in self.scenario_results]
        failed_scenarios = [spec.scenario_id for spec in specs if not (self.scenario_results.get(spec.scenario_id) or {}).get("ok")]
        failed_scenarios_by_classification: dict[str, list[str]] = {}
        allowed_scenario_classifications = {
            "FAIL_PRODUCT",
            "FAIL_HARNESS",
            "FAIL_INFRA",
            "FAIL_EXTERNAL",
            "BLOCKED",
            "INVALIDATED",
            "INTERRUPTED",
        }
        for scenario_id in failed_scenarios:
            declared = str(
                (self.scenario_results.get(scenario_id) or {}).get("classification")
                or "FAIL_PRODUCT"
            )
            classification = (
                declared if declared in allowed_scenario_classifications else "FAIL_HARNESS"
            )
            failed_scenarios_by_classification.setdefault(classification, []).append(
                scenario_id
            )
        findings: list[dict[str, Any]] = []
        if active_seconds + 1 < int(self.args.duration_seconds):
            findings.append({"severity": "critical", "classification": "FAIL_HARNESS", "title": "required active campaign duration was not completed", "actual": active_seconds})
        if not core_report_validation.get("ok"):
            findings.append({
                "severity": "critical",
                "classification": "FAIL_HARNESS",
                "title": "core child report failed bounded schema or cardinality validation",
                "errors": core_report_validation.get("errors"),
            })
        if core_checkpoint_validation.get("used") and not core_checkpoint_validation.get("ok"):
            findings.append({
                "severity": "high",
                "classification": "FAIL_HARNESS",
                "title": "core checkpoint fallback could not be read within its hard limit",
                "errors": core_checkpoint_validation.get("errors"),
            })
        if int(core_returncode or 0) != 0 or not core_payload.get("ok"):
            findings.append({
                "severity": "critical",
                "classification": str(core_payload.get("classification") or "FAIL_HARNESS"),
                "title": "continuous primary operational soak failed",
                "returncode": core_returncode,
            })
        if (
            self.campaign_level == "formal"
            and core_payload.get("production_signoff_eligible") is not True
        ):
            findings.append({
                "severity": "critical",
                "classification": "FAIL_HARNESS",
                "title": "formal core soak did not produce sign-off-eligible terminal evidence",
            })
        activation_required = self.supervised and self.campaign_level in {
            "rehearsal",
            "formal",
        }
        child_activation = core_payload.get("activation")
        activation_errors: list[str] = []
        if activation_required:
            if not self.core_activation_artifacts_intact():
                activation_errors.append("activation_artifacts_changed")
            if not isinstance(child_activation, Mapping):
                activation_errors.append("child_activation_missing")
                child_activation = {}
            for name in (
                "ready_sha256",
                "activation_sha256",
                "ack_sha256",
                "activation_monotonic_ns",
                "activation_epoch_ns",
            ):
                if child_activation.get(name) != self.core_activation_evidence.get(name):
                    activation_errors.append(f"child_activation_mismatch:{name}")
            if child_activation.get("campaign_uuid") != self.campaign_uuid:
                activation_errors.append("child_activation_mismatch:campaign_uuid")
            if child_activation.get("runner_profile_digest") != self.core_profile_digest:
                activation_errors.append("child_activation_mismatch:runner_profile_digest")
            if float(core_payload.get("actual_duration_seconds") or 0.0) + 1.0 < float(
                active_seconds
            ):
                activation_errors.append("core_active_duration_shorter_than_campaign")
        if activation_errors:
            findings.append({
                "severity": "critical",
                "classification": "FAIL_HARNESS",
                "title": "core soak activation barrier evidence failed",
                "errors": sorted(set(activation_errors)),
            })
        effective_load_validation = validate_effective_load_evidence(
            core_payload,
            campaign_level=self.campaign_level,
        )
        if not effective_load_validation.get("ok"):
            child_load_evidence = core_payload.get("effective_load")
            evidence_well_formed = bool(
                isinstance(child_load_evidence, Mapping)
                and child_load_evidence.get("schema_version")
                == EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION
                and isinstance(child_load_evidence.get("target_load_summary"), Mapping)
            )
            findings.append({
                "severity": "critical",
                "classification": "FAIL_PRODUCT" if evidence_well_formed else "FAIL_HARNESS",
                "title": "required load ramp or effective target-load coverage was not proven",
                "errors": effective_load_validation.get("errors"),
            })
        if missing_scenarios:
            findings.append({"severity": "critical", "classification": "FAIL_HARNESS", "title": "mandatory campaign scenarios did not run", "scenarios": missing_scenarios})
        for classification, scenario_ids in sorted(failed_scenarios_by_classification.items()):
            findings.append({
                "severity": "critical" if classification == "FAIL_HARNESS" else "high",
                "classification": classification,
                "title": "mandatory campaign scenarios failed",
                "scenarios": scenario_ids,
            })
        if unfinished:
            findings.append({"severity": "critical", "classification": "FAIL_HARNESS", "title": "scenario workers did not finish", "threads": unfinished})
        if self.drift:
            findings.append({"severity": "critical", "classification": "INVALIDATED", "title": "source or test harness changed during campaign", "files": self.drift})
        minimum_samples = max(2, int(int(self.args.duration_seconds) / max(1.0, float(self.args.resource_interval)) * 0.95))
        if int(resources.get("samples") or 0) < minimum_samples:
            findings.append({"severity": "high", "classification": "FAIL_HARNESS", "title": "resource evidence sample coverage is incomplete", "samples": resources.get("samples"), "required": minimum_samples})
        if self.supervised and not resources.get("ok"):
            findings.append({
                "severity": "critical",
                "classification": "FAIL_INFRA" if resources.get("hard_stop_samples") else "FAIL_HARNESS",
                "title": "mandatory resource fields did not maintain 95% valid coverage",
                "completeness": resources.get("mandatory_field_completeness"),
                "fields_below_threshold": resources.get("mandatory_fields_below_threshold"),
                "hard_stop_samples": resources.get("hard_stop_samples"),
            })
        for name, evidence in (resources.get("servers") or {}).items():
            if int(evidence.get("unplanned_health_failures") or 0) > 0:
                findings.append({"severity": "high", "classification": "FAIL_INFRA", "title": f"{name} sentinel observed unplanned transport failure", "count": evidence.get("unplanned_health_failures")})
            if float((evidence.get("health_latency_ms") or {}).get("p95") or 0) > float(self.args.max_sentinel_p95_ms):
                findings.append({"severity": "high", "classification": "FAIL_PRODUCT", "title": f"{name} sentinel p95 exceeded SLA", "p95_ms": (evidence.get("health_latency_ms") or {}).get("p95")})
            if float(evidence.get("max_rss_mb") or 0) <= 0:
                findings.append({"severity": "high", "classification": "FAIL_HARNESS", "title": f"{name} resource sampler captured no process RSS"})
        for name, evidence in server_logs.items():
            counts = evidence.get("counts") or {}
            if evidence.get("errors"):
                findings.append({
                    "severity": "high",
                    "classification": "FAIL_HARNESS",
                    "title": f"{name} server log evidence could not be read completely",
                    "errors": evidence.get("errors"),
                })
            if int(counts.get("database_locked") or 0) > 0:
                findings.append({"severity": "high", "classification": "FAIL_PRODUCT", "title": f"{name} logged SQLite lock failures", "count": counts.get("database_locked")})
            if int(counts.get("traceback") or 0) > 0 or int(counts.get("uncaught") or 0) > 0 or int(counts.get("oom") or 0) > 0:
                findings.append({"severity": "high", "classification": "FAIL_PRODUCT", "title": f"{name} logged unhandled server failures", "counts": counts})
        if not all(item.get("ok") for item in control_checks.values()):
            findings.append({"severity": "high", "classification": "FAIL_HARNESS", "title": "final control-plane verification failed"})
        if not security_final.get("ok"):
            findings.append({"severity": "critical", "classification": "FAIL_PRODUCT", "title": "production security sentinel failed"})
        if not secret_scan.get("ok"):
            findings.append({"severity": "critical", "classification": "FAIL_PRODUCT", "title": "campaign artifacts contain credential material", "hits": secret_scan.get("hits")})
        if not account_cleanup.get("ok"):
            findings.append({
                "severity": "high",
                "classification": "FAIL_HARNESS",
                "title": "isolated campaign account cleanup was incomplete",
                "records": account_cleanup.get("records"),
                "error": account_cleanup.get("error"),
            })
        if not all(result.get("ok") for result in log_seal_stops.values()):
            findings.append({
                "severity": "critical",
                "classification": "FAIL_HARNESS",
                "title": "server process groups could not be sealed before final log snapshot",
                "stops": log_seal_stops,
            })

        formal = not self.args.allow_short_duration and int(self.args.duration_seconds) >= MIN_FORMAL_SECONDS
        ok = not findings
        classification = "PASS"
        if not ok:
            observed = {str(item.get("classification") or "FAIL_HARNESS") for item in findings}
            classification = next(
                (
                    candidate
                    for candidate in (
                        "INVALIDATED",
                        "FAIL_HARNESS",
                        "FAIL_INFRA",
                        "FAIL_EXTERNAL",
                        "FAIL_PRODUCT",
                    )
                    if candidate in observed
                ),
                "FAIL_HARNESS",
            )
        if self.state_machine is not None:
            state_snapshot = self.state_machine.snapshot()
            current_state = CampaignState(state_snapshot["state"])
            if ok and current_state == CampaignState.AUDITING:
                state_snapshot = self.state_machine.transition(
                    CampaignState.PASS,
                    reason="all_machine_evidence_gates_passed",
                    classification="PASS",
                )
            elif not ok and current_state == CampaignState.AUDITING:
                state_snapshot = self.state_machine.transition(
                    CampaignState.FAILED,
                    reason="final_evidence_gate_failed",
                    classification=classification,
                    evidence={"finding_count": len(findings)},
                )
            else:
                ok = False
                classification = str(state_snapshot.get("classification") or "FAIL_HARNESS")
            self._write_control_from_state(state_snapshot)
        payload = {
            "ok": ok,
            "verdict": "PASS" if ok else classification,
            "classification": classification,
            "production_signoff_eligible": bool(ok and formal and self.supervised),
            "formal_campaign": formal,
            "started_at": self.active_started_at,
            "finished_at": utc_now(),
            "required_active_test_seconds": int(self.args.duration_seconds),
            "active_test_seconds": round(active_seconds, 3),
            "authorization_wait_seconds_included": 0,
            "preflight": preflight,
            "primary_start": primary_start,
            "recovery_start": recovery_start,
            "security_start": security_start,
            "security_preflight": security_preflight,
            "security_final": security_final,
            "core_soak": {
                "returncode": core_returncode,
                "report": str(self.core_report),
                "result": core_payload,
                "report_validation": core_report_validation,
                "checkpoint_validation": core_checkpoint_validation,
                "command": sanitized_command(self.core_command),
                "ready": self.core_ready_evidence,
                "activation": self.core_activation_evidence,
            },
            "effective_load_validation": effective_load_validation,
            "scenario_scope": (
                "harness_lifecycle_only"
                if self.supervised and self.campaign_level == "smoke"
                else "mandatory_full_feature_matrix"
            ),
            "scenarios": self.scenario_results,
            "account_inventory": self.account_inventory,
            "account_cleanup": account_cleanup,
            "resources": resources,
            "resource_samples": str(self.resource_monitor.out),
            "server_logs": server_logs,
            "final_log_seal_stops": log_seal_stops,
            "control_checks": control_checks,
            "secret_scan": secret_scan,
            "server_events": {
                "primary": self.primary.events,
                "recovery": self.recovery.events,
                "security_sentinel": self.security_sentinel.events,
            },
            "source_manifest_digest": self.source_digest,
            "source_git": self.source_git,
            "source_drift": self.drift,
            "findings": findings,
        }
        atomic_write_json(self.final_path, payload)
        self._commit_checkpoint({
            "schema_version": "hackme.campaign-checkpoint.v1",
            "campaign_uuid": self.campaign_uuid,
            "revision": self.checkpoint_revision + 1,
            "status": "complete",
            "verdict": payload["verdict"],
            "production_signoff_eligible": payload["production_signoff_eligible"],
            "active_test_seconds": payload["active_test_seconds"],
            "report": str(self.final_path),
        })
        print(json.dumps({
            "ok": ok,
            "verdict": payload["verdict"],
            "production_signoff_eligible": payload["production_signoff_eligible"],
            "active_test_seconds": payload["active_test_seconds"],
            "report": str(self.final_path),
            "findings": findings,
        }, ensure_ascii=False, indent=2), flush=True)
        return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--campaign-root", required=True, help="New campaign directory below /tmp.")
    parser.add_argument("--duration-seconds", type=int, default=MIN_FORMAL_SECONDS)
    parser.add_argument("--allow-short-duration", action="store_true", help="Development harness validation only; never sign-off evidence.")
    parser.add_argument("--primary-port", type=int, default=0)
    parser.add_argument("--recovery-port", type=int, default=0)
    parser.add_argument("--security-port", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--account-count", type=int, default=10)
    parser.add_argument("--round-ops", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--session-pool", type=int, default=20)
    parser.add_argument("--browser-interval-seconds", type=int, default=3 * 60 * 60)
    parser.add_argument("--resource-interval", type=float, default=5.0)
    parser.add_argument("--heartbeat-interval", type=float, default=60.0)
    parser.add_argument("--scenario-join-timeout-seconds", type=int, default=8 * 60 * 60)
    parser.add_argument("--minimum-free-gb", type=float, default=20.0)
    parser.add_argument("--max-server-busy-rate", type=float, default=0.05)
    parser.add_argument("--max-ordinary-p95-ms", type=float, default=3000.0)
    parser.add_argument("--max-ordinary-p99-ms", type=float, default=8000.0)
    parser.add_argument("--max-sentinel-p95-ms", type=float, default=3000.0)
    parser.add_argument("--keep-servers", action="store_true")
    parser.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--campaign-uuid", default="", help=argparse.SUPPRESS)
    parser.add_argument("--control-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--state-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--control-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--heartbeat-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--checkpoint-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--checkpoint-mirror-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--source-freeze-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--cgroup-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--activation-gate", default="", help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-contract", default="", help=argparse.SUPPRESS)
    return parser


def _current_unified_cgroup(pid: int | None = None) -> str:
    process_id = int(pid or os.getpid())
    try:
        rows = Path(f"/proc/{process_id}/cgroup").read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        raise RuntimeError(f"cannot read runner cgroup: {exc.__class__.__name__}: {exc}") from exc
    for row in rows:
        parts = row.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            return "/" + parts[2].strip().lstrip("/")
    raise RuntimeError("runner has no unified cgroup v2 membership")


def validate_supervised_runtime_contract(
    args: argparse.Namespace,
    contract: Mapping[str, Any],
    source_freeze: Mapping[str, Any],
) -> None:
    """Reject any divergence between supervisor authority and runner argv."""

    errors: list[str] = []
    level = str(contract.get("level") or "")
    expected_duration = SUPERVISED_LEVEL_DURATIONS.get(level)
    if expected_duration is None:
        errors.append("level")
    else:
        if int(contract.get("duration_seconds") or 0) != expected_duration:
            errors.append("contract_duration_seconds")
        if int(args.duration_seconds) != expected_duration:
            errors.append("runner_duration_seconds")
        expected_short_mode = level != "formal"
        if bool(args.allow_short_duration) is not expected_short_mode:
            errors.append("allow_short_duration")
        expected_profile = SUPERVISED_RUNNER_PROFILES[level]
        contract_profile = contract.get("runner_profile")
        if not isinstance(contract_profile, Mapping):
            errors.append("runner_profile")
            contract_profile = {}
        if set(contract_profile) != set(expected_profile):
            errors.append("runner_profile_shape")
        for name, expected_value in expected_profile.items():
            contract_value = contract_profile.get(name)
            runner_value = getattr(args, name, None)
            if (
                isinstance(contract_value, bool)
                or not isinstance(contract_value, (int, float))
                or float(contract_value) != float(expected_value)
            ):
                errors.append(f"contract_runner_profile:{name}")
            if (
                isinstance(runner_value, bool)
                or not isinstance(runner_value, (int, float))
                or float(runner_value) != float(expected_value)
            ):
                errors.append(f"runner_profile:{name}")
        if bool(args.keep_servers):
            errors.append("keep_servers")
        if contract.get("load_policy") != SUPERVISED_LOAD_POLICIES[level]:
            errors.append("load_policy")
    campaign_root = validate_tmp_path(Path(args.campaign_root), label="campaign root")
    if str(contract.get("campaign_root") or "") != str(campaign_root):
        errors.append("campaign_root")
    try:
        control_root = validate_control_root(campaign_root, Path(args.control_root))
    except Exception:
        control_root = Path("/invalid-campaign-control-root")
        errors.append("control_root")
    if str(contract.get("control_root") or "") != str(control_root):
        errors.append("contract_control_root")
    for name in (
        "state_path",
        "control_path",
        "heartbeat_path",
        "checkpoint_path",
        "source_freeze_path",
        "activation_gate",
        "supervisor_contract",
    ):
        raw = str(getattr(args, name, "") or "")
        resolved = Path(raw).resolve(strict=False) if raw else Path("/missing")
        if not raw or (resolved != control_root and control_root not in resolved.parents):
            errors.append(f"control_path:{name}")
    mirror_path = Path(str(args.checkpoint_mirror_path or "")).resolve(strict=False)
    mirror_root = (Path.home() / "logs" / "hackme_web_campaign_24h").resolve(strict=False)
    if (
        not str(args.checkpoint_mirror_path or "").strip()
        or str(contract.get("checkpoint_mirror_path") or "") != str(mirror_path)
        or mirror_root not in mirror_path.parents
    ):
        errors.append("checkpoint_mirror_path")
    expected_cgroup = "/" + str(args.cgroup_path or "").strip().lstrip("/")
    contract_cgroup = "/" + str(contract.get("cgroup_path") or "").strip().lstrip("/")
    if expected_cgroup == "/" or contract_cgroup != expected_cgroup:
        errors.append("cgroup_path")
    event_baseline = contract.get("cgroup_event_baseline")
    if not isinstance(event_baseline, Mapping):
        errors.append("cgroup_event_baseline")
        event_baseline = {}
    mandatory_event_keys = {
        "memory.events": {"max", "oom", "oom_kill"},
        "pids.events": {"max"},
    }
    for filename, required_names in mandatory_event_keys.items():
        values = event_baseline.get(filename)
        if not isinstance(values, Mapping):
            errors.append(f"cgroup_event_baseline:{filename}")
            continue
        if set(values) < required_names:
            errors.append(f"cgroup_event_baseline_shape:{filename}")
        for name in required_names:
            value = values.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(f"cgroup_event_baseline_value:{filename}.{name}")
    if source_freeze.get("schema_version") != SOURCE_FREEZE_SCHEMA_VERSION:
        errors.append("source_freeze_schema")
    if source_freeze.get("verified") is not True or source_freeze.get("label") != "H0":
        errors.append("source_freeze_verified")
    if Path(str(source_freeze.get("repo_root") or "")).resolve(strict=False) != ROOT:
        errors.append("source_repo_root")
    if source_freeze.get("commit") != contract.get("commit"):
        errors.append("source_commit")
    if source_freeze.get("tracked_content_digest") != contract.get("source_digest"):
        errors.append("source_digest")
    if level == "formal" and source_freeze.get("require_clean") is not True:
        errors.append("formal_source_not_clean")
    gates = contract.get("gates")
    if not isinstance(gates, Mapping):
        errors.append("supervisor_gates")
        gates = {}
    required_gates = {
        "cgroup_limits_verified",
        "external_watchdog_verified",
        "runner_and_watchdog_placement_verified",
        "cgroup_event_baseline_verified",
    }
    required_gates.add(
        "worktree_clean_and_frozen" if level == "formal" else "source_baseline_frozen"
    )
    if level == "formal":
        required_gates.update({
            "formal_authorization_verified",
            "prior_harness_gate_bundle_verified",
        })
    for gate_name in sorted(required_gates):
        row = gates.get(gate_name) if isinstance(gates, Mapping) else None
        if not isinstance(row, Mapping) or row.get("status") != "PASS" or row.get("machine_verified") is not True:
            errors.append(f"gate:{gate_name}")
    if errors:
        raise RuntimeError(
            "supervisor/runner contract mismatch: " + ", ".join(sorted(set(errors)))
        )


def wait_for_supervisor_activation(args: argparse.Namespace) -> dict[str, Any]:
    if not args.supervised:
        return {}
    required = {
        "campaign_uuid": args.campaign_uuid,
        "control_root": args.control_root,
        "state_path": args.state_path,
        "control_path": args.control_path,
        "heartbeat_path": args.heartbeat_path,
        "checkpoint_path": args.checkpoint_path,
        "checkpoint_mirror_path": args.checkpoint_mirror_path,
        "source_freeze_path": args.source_freeze_path,
        "cgroup_path": args.cgroup_path,
        "activation_gate": args.activation_gate,
        "supervisor_contract": args.supervisor_contract,
    }
    missing = sorted(name for name, value in required.items() if not str(value or "").strip())
    if missing:
        raise RuntimeError("supervised runner contract is incomplete: " + ", ".join(missing))
    activation_path = Path(args.activation_gate).resolve(strict=False)
    contract_path = Path(args.supervisor_contract).resolve(strict=False)
    campaign_root = validate_tmp_path(Path(args.campaign_root), label="campaign root")
    control_root = validate_control_root(campaign_root, Path(args.control_root))
    source_freeze_path = Path(args.source_freeze_path).resolve(strict=False)
    for label, path in (
        ("activation gate", activation_path),
        ("supervisor contract", contract_path),
        ("source freeze", source_freeze_path),
    ):
        if control_root not in path.parents:
            raise RuntimeError(f"{label} is outside campaign control root")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if activation_path.exists() and contract_path.exists():
            activation = load_json(activation_path)
            contract = load_json(contract_path)
            if (
                activation.get("verified") is True
                and contract.get("verified") is True
                and activation.get("campaign_uuid") == args.campaign_uuid
                and contract.get("campaign_uuid") == args.campaign_uuid
                and activation.get("supervisor_contract") == str(contract_path)
            ):
                actual_cgroup = _current_unified_cgroup()
                expected_cgroup = "/" + str(args.cgroup_path).strip().lstrip("/")
                if actual_cgroup != expected_cgroup and not actual_cgroup.startswith(expected_cgroup.rstrip("/") + "/"):
                    raise RuntimeError(
                        f"supervised runner is outside managed cgroup: expected={expected_cgroup}, actual={actual_cgroup}"
                    )
                if contract.get("runner_pid") != os.getpid():
                    raise RuntimeError("supervisor contract runner PID mismatch")
                if contract.get("state_path") != str(Path(args.state_path).resolve(strict=False)):
                    raise RuntimeError("supervisor state path mismatch")
                source_freeze = load_json(source_freeze_path)
                validate_supervised_runtime_contract(args, contract, source_freeze)
                return contract
        time.sleep(0.1)
    raise RuntimeError("supervisor did not release the campaign runner within 120 seconds")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.supervised and not args.cgroup_path:
        args.cgroup_path = str(os.environ.get("HACKME_CAMPAIGN_CGROUP_PATH") or "")
    if int(args.duration_seconds) < MIN_FORMAL_SECONDS and not args.allow_short_duration:
        raise SystemExit(f"formal campaign requires at least {MIN_FORMAL_SECONDS} active seconds")
    if not args.allow_short_duration and not args.supervised:
        raise SystemExit("formal campaign must be launched by operational_campaign_supervisor.py")
    root = validate_tmp_path(Path(args.campaign_root), label="campaign root")
    if root.exists() and not args.supervised:
        raise SystemExit(f"campaign root must not already exist: {root}")
    if args.supervised and not root.is_dir():
        raise SystemExit(f"supervisor-prepared campaign root is missing: {root}")
    try:
        wait_for_supervisor_activation(args)
    except Exception as exc:
        raise SystemExit(f"supervisor activation failed: {exc}") from exc
    campaign = Campaign(args)

    def stop_handler(_signum: int, _frame: Any) -> None:
        campaign.stop_event.set()
        if campaign.core_process:
            terminate_process_group(campaign.core_process)
        if campaign.core_identity is not None:
            campaign.process_registry.unregister("load_generator", campaign.core_identity)
            campaign.core_identity = None

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        return campaign.run()
    except Exception as exc:
        campaign.stop_event.set()
        campaign.stop_managed_steps()
        campaign.mark_failed(reason="CAMPAIGN_RUNNER_EXCEPTION")
        campaign.resource_monitor.stop()
        if campaign.resource_monitor.is_alive():
            campaign.resource_monitor.join(timeout=10)
        if campaign.core_process:
            try:
                durable_atomic_write_json(campaign.core_stop_file, {
                    "schema_version": "hackme.campaign-load-stop.v1",
                    "campaign_uuid": campaign.campaign_uuid,
                    "reason": "campaign_runner_exception",
                    "requested_at": utc_now(),
                })
                campaign.core_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminate_process_group(campaign.core_process)
        if campaign.core_stdout_handle:
            campaign.core_stdout_handle.close()
        account_cleanup: dict[str, Any] = {}
        if campaign.account_inventory and campaign.primary.pid() > 0:
            try:
                core_checkpoint = load_json(
                    campaign.core_root / "reports" / "operational_soak" / "operational_soak.checkpoint.json"
                )
                extra_accounts = core_checkpoint.get("accounts") if isinstance(core_checkpoint.get("accounts"), list) else []
                account_cleanup = campaign.cleanup_campaign_accounts(
                    additional_usernames=[str(value) for value in extra_accounts],
                )
            except Exception as cleanup_exc:
                account_cleanup = {"ok": False, "error": f"{cleanup_exc.__class__.__name__}: {cleanup_exc}"}
        if not args.keep_servers:
            campaign.primary.stop(reason="campaign_exception")
            campaign.recovery.stop(reason="campaign_exception")
            campaign.security_sentinel.stop(reason="campaign_exception")
        payload = {
            "ok": False,
            "verdict": "FAIL",
            "phase": "exception",
            "error": f"{exc.__class__.__name__}: {exc}",
            "account_inventory": campaign.account_inventory,
            "account_cleanup": account_cleanup,
            "at": utc_now(),
        }
        campaign.reports.mkdir(parents=True, exist_ok=True)
        atomic_write_json(campaign.final_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return 2
    finally:
        campaign.stop_heartbeat_pump()
        if campaign.source_freezer is not None:
            campaign.source_freezer.close()


if __name__ == "__main__":
    raise SystemExit(main())
