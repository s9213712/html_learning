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
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Mapping
from urllib.parse import quote, urljoin, urlsplit

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
from scripts.testing.campaign_comfyui_sandbox import (
    HOST_TRANSITION_SCHEMA_VERSION,
    SANDBOX_PROOF_SCHEMA_VERSION,
)
from scripts.testing.campaign_comfyui_backend import (
    COMFYUI_BACKEND_READY_SCHEMA_VERSION,
    read_stable_ready_receipt,
    validate_live_comfyui_backend_authority,
)
from scripts.testing.campaign_scenario_binding import (
    FORMAL_BINDING_GATE_SCHEMA_VERSION,
    FORMAL_SCENARIO_BINDINGS,
    NATIVE_RUNNER_RESULT_SCHEMA_VERSION,
    NativeEvidenceAdapterRegistration,
    ScenarioRunnerRegistration,
    ScenarioValidatorRegistration,
    build_and_validate_formal_scenario_bindings,
    build_strict_native_adapter_registry,
    build_strict_native_validator_registry,
    execute_registered_native_scenario,
    strict_native_runtime_pipeline_verified,
    validate_scenario_runtime_receipt,
)
from scripts.testing.campaign_gate_bundle import protected_source_identity_digest
from scripts.testing.campaign_qualification_capture import (
    REHEARSAL_PROJECTION_CONTEXT_ENV,
    REHEARSAL_PROJECTION_CONTEXT_SHA256_ENV,
    read_sealed_rehearsal_projection_context,
)
from scripts.testing.audit_evidence_triad import (
    ARCHIVE_SCHEMA_VERSION as AUDIT_EVIDENCE_ARCHIVE_SCHEMA_VERSION,
    SCHEMA_PATH as AUDIT_EVIDENCE_SCHEMA_PATH,
    SCHEMA_VERSION as AUDIT_EVIDENCE_SCHEMA_VERSION,
    AuditEvidencePaths,
    capture_audit_evidence,
    validate_audit_evidence_archive,
    validate_audit_evidence_receipt,
)
from scripts.testing.campaign_native_evidence import attach_native_evidence
from scripts.testing.bt_formal_local_probe import (
    MANDATORY_CHECK_IDS as BT_MANDATORY_CHECK_IDS,
    validate_machine_report as validate_bt_machine_report,
)
from scripts.testing.campaign_native_selectors import (
    ai_agent_positive_assertions,
    backup_restore_assertions,
    bt_download_assertions,
    cloud_drive_stream_assertions,
    comfyui_workflow_assertions,
    community_governance_assertions,
    final_ui_assertions,
    media_long_assertions,
    media_proxy_assertions,
    pointschain_hft_assertions,
    server_emergency_assertions,
    trading_workflow_assertions,
    wallet_incident_assertions,
)
from scripts.testing.operation_coverage import CAMPAIGN_SCENARIO_CONTRACTS
from scripts.testing.campaign_observability import (
    ProcessRoleRegistry,
    ResourceCollector as StructuredResourceCollector,
    ResourceCollectorConfig as StructuredResourceCollectorConfig,
    ResourceMonitor as StructuredResourceMonitor,
    STARTUP_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG10,
    STARTUP_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG60,
    collect_host_startup_safety_preflight,
    wait_for_host_safety_preflight,
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
from scripts.testing.campaign_source_freeze import (
    FULL_CONTENT_EVIDENCE,
    METADATA_CONTENT_EVIDENCE,
    GitSourceFreezer,
    SOURCE_FREEZE_SCHEMA_VERSION,
)
from scripts.testing.campaign_runtime_contract import (
    MIN_FORMAL_SECONDS,
    SUPERVISED_LEVEL_DURATIONS,
    SUPERVISED_LOAD_POLICIES,
    SUPERVISED_RUNNER_PROFILE_OPTIONS,
    SUPERVISED_RUNNER_PROFILES,
    Credentials,
    validate_control_root,
    validate_tmp_path,
)
from scripts.testing.campaign_state import CampaignState, CampaignStateError, CampaignStateMachine, process_start_ticks
from scripts.testing.campaign_control_channel import (
    PeerIdentity,
    send_hello,
    sign_authenticated_payload,
    verify_authenticated_payload,
)
from scripts.testing.campaign_watchdog import atomic_write_json as durable_atomic_write_json

LAUNCHER = ROOT / "test_for_develop.sh"
SOAK = ROOT / "scripts" / "testing" / "operational_soak_probe.py"
SMOKE_LOAD = ROOT / "scripts" / "testing" / "campaign_smoke_load.py"
RUNNER_HOST_SAFETY_TIMEOUT_SECONDS = 90.0
# Covers all dormant supervisor settle windows (runner import, state writes,
# staged watchdog bootstrap, readiness, and final activation) with margin.
RUNNER_SUPERVISOR_ACTIVATION_TIMEOUT_SECONDS = 900.0
RUNNER_STARTUP_IO_PRESSURE_AVG10_MAXIMUM = (
    STARTUP_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG10
)
RUNNER_STARTUP_IO_PRESSURE_AVG60_MAXIMUM = (
    STARTUP_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG60
)
FINAL_AUDIT_EVIDENCE_INDEX_SCHEMA_VERSION = "hackme.audit-evidence-triad-index/v1"
FINAL_AUDIT_EVIDENCE_MANIFEST_SCHEMA_VERSION = "hackme.audit-evidence-triad-hash-manifest/v1"
FINAL_AUDIT_EVIDENCE_SEAL_SCHEMA_VERSION = "hackme.audit-writer-seal-verification/v1"
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
CAMPAIGN_STORAGE_QUOTA_MB = 1024
CAMPAIGN_STORAGE_MAX_FILE_SIZE_MB = 512
CAMPAIGN_STORAGE_UPLOAD_RATE_LIMIT_PER_DAY = 100
CAMPAIGN_CONTROLLED_BACKPRESSURE_MAX_ATTEMPTS = 12
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
# These are evidence mappings, not labels inferred from a campaign level.  A
# feature is emitted into the rehearsal receipt only when the strict native
# scenario pipeline for the owning scenario returned an exact ``ok is True``.
# The formal gate independently reopens each scenario receipt afterwards.
REHEARSAL_FEATURE_SCENARIOS: Mapping[str, str] = {
    "planned_restart": "backup_restore_restart",
    "runtime_backup_restore": "backup_restore_restart",
    "comfyui_real_workflow": "comfyui_real_workflows",
    "bt_terminal_download": "bt_download_stream_restart",
    "cross_browser_mobile_ui": "final_ui_mobile_prelaunch",
}
_EXECUTION_GAP_KEYS: Mapping[str, frozenset[str]] = {
    "skips": frozenset({"skip", "skipped", "skips"}),
    "fallbacks": frozenset({
        "fallback",
        "fallbacks",
        "fallback_error",
        "fallback_used",
        "used_fallback",
    }),
    "expected_gaps": frozenset({"expected_gap", "expected_gaps"}),
}
_EXECUTION_GAP_SCAN_MAX_NODES = 250_000
_EXECUTION_GAP_SCAN_MAX_DEPTH = 64


def _declared_gap_is_active(value: Any) -> bool:
    """Treat only explicit, non-empty gap declarations as active evidence."""

    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "none", "no"}
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return bool(value)
    return True


def derive_rehearsal_execution_contract(
    scenario_results: Mapping[str, Any],
    state_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Project gate fields from authoritative runtime state, fail closed.

    The projection never declares all features merely because a rehearsal was
    requested.  It requires the corresponding strict native scenario result,
    reads invalid time from the durable state-machine clock, and recursively
    detects any explicit skip/fallback/expected-gap marker.
    """

    errors: list[str] = []
    clock = state_snapshot.get("clock")
    invalid_seconds: float | None = None
    if not isinstance(clock, Mapping):
        errors.append("state_clock_missing")
    else:
        raw_invalid = clock.get("invalid_seconds")
        if (
            isinstance(raw_invalid, bool)
            or not isinstance(raw_invalid, (int, float))
            or not math.isfinite(float(raw_invalid))
            or float(raw_invalid) < 0.0
        ):
            errors.append("invalid_seconds_missing_or_invalid")
        else:
            invalid_seconds = round(float(raw_invalid), 6)

    features = sorted(
        feature
        for feature, scenario_id in REHEARSAL_FEATURE_SCENARIOS.items()
        if isinstance(scenario_results.get(scenario_id), Mapping)
        and scenario_results[scenario_id].get("ok") is True
    )

    declarations: dict[str, list[dict[str, str]]] = {
        name: [] for name in _EXECUTION_GAP_KEYS
    }
    stack: list[tuple[str, str, Any, int]] = [
        (str(scenario_id), str(scenario_id), payload, 0)
        for scenario_id, payload in scenario_results.items()
    ]
    visited: set[int] = set()
    nodes = 0
    while stack:
        scenario_id, path, value, depth = stack.pop()
        nodes += 1
        if nodes > _EXECUTION_GAP_SCAN_MAX_NODES:
            errors.append("execution_gap_scan_node_budget_exceeded")
            break
        if depth > _EXECUTION_GAP_SCAN_MAX_DEPTH:
            errors.append("execution_gap_scan_depth_exceeded")
            break
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in visited:
                errors.append("execution_gap_scan_cycle_detected")
                break
            visited.add(identity)
            for raw_key, child in value.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                child_path = f"{path}.{raw_key}"
                for bucket, keys in _EXECUTION_GAP_KEYS.items():
                    if key in keys and _declared_gap_is_active(child):
                        declarations[bucket].append({
                            "scenario_id": scenario_id,
                            "path": child_path,
                            "marker": key,
                        })
                stack.append((scenario_id, child_path, child, depth + 1))
        elif isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in visited:
                errors.append("execution_gap_scan_cycle_detected")
                break
            visited.add(identity)
            for index, child in enumerate(value):
                stack.append((scenario_id, f"{path}[{index}]", child, depth + 1))

    for rows in declarations.values():
        rows.sort(key=lambda row: (row["scenario_id"], row["path"], row["marker"]))
    return {
        "invalid_seconds": invalid_seconds,
        "mandatory_features_executed": features,
        "skips": declarations["skips"],
        "fallbacks": declarations["fallbacks"],
        "expected_gaps": declarations["expected_gaps"],
        "errors": sorted(set(errors)),
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
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
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


def preflight_dependency_commands(
    campaign_level: str,
) -> dict[str, list[str]]:
    """Probe only capabilities exercised by a level before server startup."""

    commands = {
        "gunicorn": [
            sys.executable,
            "-c",
            "import gunicorn; print(gunicorn.__version__)",
        ],
    }
    if campaign_level != "smoke":
        commands = {
            "ffmpeg": ["ffmpeg", "-version"],
            "ffprobe": ["ffprobe", "-version"],
            "playwright": [
                sys.executable,
                "-c",
                "from playwright.sync_api import sync_playwright; print('ok')",
            ],
            **commands,
        }
    return commands


def preflight_repo_runtime_scan(
    root: Path,
    *,
    campaign_level: str,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Keep the repo-scale pollution scan out of level-0 lifecycle smoke."""

    if campaign_level == "smoke":
        return {
            "schema_version": "hackme.preflight-runtime-scan.v1",
            "status": "NOT_APPLICABLE",
            "reason": (
                "level_0_smoke_uses_supervisor_source_freeze_and_isolated_run_root"
            ),
            "required": False,
            "ok": True,
            "complete": False,
            "entries_scanned": 0,
            "repo_runtime_pollution": [],
            "errors": [],
        }
    result = bounded_repo_runtime_scan(
        root,
        progress_callback=progress_callback,
    )
    return {
        **result,
        "status": "EVALUATED",
        "required": True,
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
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
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


def supervised_source_identity(
    source_h0: Mapping[str, Any],
) -> tuple[dict[str, str], str, int, dict[str, Any]]:
    """Reuse the supervisor's authenticated H0 instead of rescanning source."""

    status = source_h0.get("status") or {}
    blocked_changes = (
        status.get("blocked_changes") if isinstance(status, Mapping) else []
    ) or []
    return (
        {},
        str(source_h0.get("tracked_content_digest") or ""),
        int(source_h0.get("tracked_file_count") or 0),
        {
            "target_commit": str(source_h0.get("commit") or ""),
            "target_branch": str(source_h0.get("branch") or ""),
            "worktree_dirty": not bool(source_h0.get("git_status_empty")),
            "worktree_change_count": len(blocked_changes),
            "authority": "supervisor_h0",
        },
    )


def wait_for_runner_host_safety_preflight() -> dict[str, Any]:
    """Require extra I/O headroom before each cold-start stage."""

    return wait_for_host_safety_preflight(
        timeout_seconds=RUNNER_HOST_SAFETY_TIMEOUT_SECONDS,
        collector=collect_runner_startup_headroom,
    )


def collect_runner_startup_headroom() -> dict[str, Any]:
    return collect_host_startup_safety_preflight()


def managed_auxiliary_worker_count(
    *,
    requested_workers: int,
    supervised: bool,
    campaign_level: str,
) -> int:
    if supervised and campaign_level == "smoke":
        return 1
    return max(2, int(requested_workers) // 2)


def managed_strict_readiness(*, supervised: bool, campaign_level: str) -> bool:
    return bool(supervised and campaign_level != "smoke")


def manifest_drift(expected: dict[str, str]) -> dict[str, dict[str, str]]:
    current = source_manifest()
    return {
        name: {"expected": expected.get(name, "missing"), "actual": current.get(name, "missing")}
        for name in sorted(set(expected) | set(current))
        if expected.get(name) != current.get(name)
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
        retry_csrf: bool = True,
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
            # Authenticated write responses rotate the CSRF cookie.  Keep the
            # explicit header in sync so a long campaign does not continue
            # sending the login-time token until the server prunes it.
            rotated_csrf = self.session.cookies.get("csrf_token")
            if rotated_csrf:
                self.csrf = str(rotated_csrf)
            self._publish_request_progress(
                f"request_completed:{method}:{response.status_code}"
            )
            if response.status_code == 401 and retry_login:
                self.login()
                return self.request(
                    method,
                    path,
                    json_body=json_body,
                    params=params,
                    retry_login=False,
                    retry_csrf=retry_csrf,
                )
            try:
                body: Any = response.json()
            except Exception:
                body = {"raw": response.text[:1000]}
            if (
                retry_csrf
                and method not in {"GET", "HEAD", "OPTIONS"}
                and response.status_code == 403
                and isinstance(body, Mapping)
                and body.get("error") == "csrf_invalid"
            ):
                refreshed = self.refresh_csrf()
                if refreshed.get("ok"):
                    retried = self.request(
                        method,
                        path,
                        json_body=json_body,
                        params=params,
                        retry_login=retry_login,
                        retry_csrf=False,
                    )
                    retried["csrf_retried"] = True
                    retried["initial_status"] = 403
                    return retried
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
        post_bootstrap_safety_callback: Callable[[], dict[str, Any]] | None = None,
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
        self.post_bootstrap_safety_callback = post_bootstrap_safety_callback
        self.registered_identity: Any | None = None
        self.launch_count = 0
        self.final_evidence_restart_disabled = False
        self.events: list[dict[str, Any]] = []
        self.post_bootstrap_nonce = ""
        self.post_bootstrap_ready_file: Path | None = None
        self.post_bootstrap_release_file: Path | None = None

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

    def _prepare_post_bootstrap_gate(self) -> None:
        if self.post_bootstrap_safety_callback is None:
            self.post_bootstrap_nonce = ""
            self.post_bootstrap_ready_file = None
            self.post_bootstrap_release_file = None
            return
        gate_root = self.run_root / "control" / "post_bootstrap"
        gate_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(gate_root, 0o700)
        nonce = secrets.token_hex(32)
        ready = gate_root / f"launcher_{self.launch_count:03d}.ready"
        release = gate_root / f"launcher_{self.launch_count:03d}.release"
        for path in (ready, release):
            if path.exists() or path.is_symlink():
                raise RuntimeError(f"post-bootstrap gate path already exists: {path}")
        self.post_bootstrap_nonce = nonce
        self.post_bootstrap_ready_file = ready
        self.post_bootstrap_release_file = release

    def _verify_post_bootstrap_ready(self) -> dict[str, Any]:
        path = self.post_bootstrap_ready_file
        if path is None or not self.post_bootstrap_nonce:
            raise RuntimeError("post-bootstrap gate was not prepared")
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or int(before.st_nlink) != 1
            or int(before.st_uid) != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise RuntimeError("post-bootstrap ready evidence metadata is unsafe")
        value = path.read_text(encoding="ascii", errors="strict").strip()
        after = os.lstat(path)
        if (
            value != self.post_bootstrap_nonce
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise RuntimeError("post-bootstrap ready evidence is invalid or unstable")
        return {
            "ok": True,
            "path": str(path),
            "mode": oct(stat.S_IMODE(after.st_mode)),
            "nonce_sha256": hashlib.sha256(value.encode("ascii")).hexdigest(),
        }

    def _release_post_bootstrap_gate(self) -> dict[str, Any]:
        path = self.post_bootstrap_release_file
        if path is None or not self.post_bootstrap_nonce:
            raise RuntimeError("post-bootstrap release gate was not prepared")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        payload = (self.post_bootstrap_nonce + "\n").encode("ascii")
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short post-bootstrap release write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {
            "ok": True,
            "path": str(path),
            "nonce_sha256": hashlib.sha256(
                self.post_bootstrap_nonce.encode("ascii")
            ).hexdigest(),
        }

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
        gate_required = self.post_bootstrap_safety_callback is not None
        gate_released = not gate_required
        post_bootstrap: dict[str, Any] = {
            "required": gate_required,
            "ready": {},
            "host_safety": {},
            "release": {},
            "ok": not gate_required,
        }
        while True:
            observation = self._launcher_observation(process, log)
            if observation != last_observation:
                observations += 1
                last_observation = observation
                self._report_progress(f"launcher_observed_progress:{observations}")
            ready_path = self.post_bootstrap_ready_file
            if (
                gate_required
                and not gate_released
                and ready_path is not None
                and (ready_path.exists() or ready_path.is_symlink())
            ):
                try:
                    post_bootstrap["ready"] = self._verify_post_bootstrap_ready()
                    self._report_progress("post_bootstrap_ready_verified")
                    callback = self.post_bootstrap_safety_callback
                    if callback is None:
                        raise RuntimeError("post-bootstrap safety callback disappeared")
                    post_bootstrap["host_safety"] = callback()
                    if post_bootstrap["host_safety"].get("ok") is not True:
                        raise RuntimeError("post-bootstrap host safety gate failed")
                    post_bootstrap["release"] = self._release_post_bootstrap_gate()
                    post_bootstrap["ok"] = True
                    gate_released = True
                    self._report_progress("post_bootstrap_safety_released")
                except Exception as exc:
                    post_bootstrap["ok"] = False
                    post_bootstrap["error"] = f"{exc.__class__.__name__}: {exc}"
                    terminate_process_group(process, grace_seconds=2.0)
                    try:
                        returncode = int(process.wait(timeout=5))
                    except subprocess.TimeoutExpired:
                        returncode = 125
                    return {
                        "returncode": returncode if returncode != 0 else 125,
                        "timed_out": False,
                        "observations": observations,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "post_bootstrap": post_bootstrap,
                        "host_safety_blocked": True,
                    }
            returncode = process.poll()
            if returncode is not None:
                if gate_required and not gate_released and returncode == 0:
                    returncode = 125
                    post_bootstrap["error"] = (
                        "launcher exited before post-bootstrap safety release"
                    )
                self._report_progress(f"launcher_completed:{returncode}")
                return {
                    "returncode": int(returncode),
                    "timed_out": False,
                    "observations": observations,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "post_bootstrap": post_bootstrap,
                    "host_safety_blocked": False,
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
                    "post_bootstrap": post_bootstrap,
                    "host_safety_blocked": False,
                }
            time.sleep(min(0.5, max(0.01, deadline - now)))

    @property
    def pid_file(self) -> Path:
        return self.runtime_root / "server.pid"

    @property
    def restart_request_root(self) -> Path:
        return self.run_root / "control" / "restart_requests"

    @property
    def restart_request_file(self) -> Path:
        return self.restart_request_root / "server_restart.json"

    def pid(self) -> int:
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("PYTHONPYCACHEPREFIX", None)
        # Capacity recommendations may point at mutable files outside the
        # frozen repository.  A supervised campaign supplies its exact
        # gunicorn profile on argv, so inherited redirects must never be able
        # to rewrite that profile before launcher argument processing.
        env.pop("HACKME_DEV_CAPACITY_DEFAULTS_FILE", None)
        env.pop("HACKME_DEV_CAPACITY_REPORT_FILE", None)
        self.restart_request_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.restart_request_root, 0o700)
        env.update({
            "ROOT_PASSWORD": self.credentials.root,
            "MANAGER_PASSWORD": self.credentials.manager,
            "TEST_PASSWORD": self.credentials.test,
            # The campaign itself already runs from a dependency-validated
            # interpreter.  Reuse that exact environment for each fresh
            # isolated runtime; --skip-install must not silently fall back to
            # an unrelated system python and then demand a reinstall.
            "PYTHON_BIN": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "HACKME_MEDIA_REALTIME_PROXY_ENABLED": "1",
            "HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT": "2",
            "HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE": "host",
            "HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR": str(self.run_root / "locks" / "realtime_proxy"),
            "HACKME_DEV_BACKTEST_PROBE_ON_STARTUP": "0",
            "HACKME_DEV_BTC_TRADE_AUTOSTART": "0",
            "HACKME_DEV_USE_CAPACITY_DEFAULTS": "0",
            "HACKME_SUPERVISED_RESTART_REQUEST_ROOT": str(self.restart_request_root),
            "HACKME_SUPERVISED_RESTART_REQUEST_FILE": str(self.restart_request_file),
        })
        if self.post_bootstrap_safety_callback is not None:
            if (
                self.post_bootstrap_ready_file is None
                or self.post_bootstrap_release_file is None
                or not self.post_bootstrap_nonce
            ):
                raise RuntimeError("post-bootstrap safety gate was not prepared")
            env.update({
                "HACKME_DEV_POST_BOOTSTRAP_READY_FILE": str(
                    self.post_bootstrap_ready_file
                ),
                "HACKME_DEV_POST_BOOTSTRAP_RELEASE_FILE": str(
                    self.post_bootstrap_release_file
                ),
                "HACKME_DEV_POST_BOOTSTRAP_NONCE": self.post_bootstrap_nonce,
                "HACKME_DEV_POST_BOOTSTRAP_TIMEOUT_SECONDS": "180",
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

    def wait_ready(
        self,
        *,
        timeout: float = 180.0,
        strict: bool | None = None,
    ) -> dict[str, Any]:
        use_strict = self.strict_readiness if strict is None else bool(strict)
        if use_strict:
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
        if self.final_evidence_restart_disabled:
            event = {
                "action": "start",
                "name": self.name,
                "at": utc_now(),
                "pid": 0,
                "restart_disabled": True,
                "error": "final_evidence_restart_barrier_active",
                "ok": False,
            }
            self.events.append(event)
            return event
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.launch_count += 1
        self._prepare_post_bootstrap_gate()
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
        post_launch_host_safety: dict[str, Any] = {
            "required": self.post_bootstrap_safety_callback is not None,
            "ok": self.post_bootstrap_safety_callback is None,
        }
        if completed["returncode"] == 0 and not leaked and launcher_evidence_ok:
            basic_ready = self.wait_ready(strict=False)
            if (
                basic_ready.get("ok") is True
                and self.post_bootstrap_safety_callback is not None
            ):
                try:
                    post_launch_host_safety = self.post_bootstrap_safety_callback()
                except Exception as exc:
                    post_launch_host_safety = {
                        "required": True,
                        "ok": False,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
            if basic_ready.get("ok") is not True:
                ready = basic_ready
            elif post_launch_host_safety.get("ok") is not True:
                ready = {
                    "ok": False,
                    "error": "post-launch host safety gate failed",
                    "basic": basic_ready,
                }
            elif self.strict_readiness:
                ready = self.wait_ready(strict=True)
                ready["basic"] = basic_ready
            else:
                ready = basic_ready
            ready["post_launch_host_safety"] = post_launch_host_safety
        else:
            ready = {
                "ok": False,
                "error": "launcher_failed_secret_leak_or_log_snapshot_invalid",
            }
        host_safety_blocked = bool(
            completed.get("host_safety_blocked")
            or (
                post_launch_host_safety.get("required") is True
                and post_launch_host_safety.get("ok") is not True
            )
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
            "post_bootstrap": completed.get("post_bootstrap") or {},
            "post_launch_host_safety": post_launch_host_safety,
            "classification": "FAIL_INFRA" if host_safety_blocked else "FAIL_HARNESS",
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
            event["classification"] = "PASS"
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
        if reason == "final_evidence_log_seal":
            self.final_evidence_restart_disabled = True
        self.planned_outage.set()
        pid = self.pid()
        started = time.monotonic()
        event: dict[str, Any] = {
            "action": "stop",
            "name": self.name,
            "at": utc_now(),
            "pid": pid,
            "reason": reason,
            "restart_disabled": self.final_evidence_restart_disabled,
            "launch_generation": self.launch_count,
        }
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
        if self.final_evidence_restart_disabled:
            result = {
                "action": "restart",
                "name": self.name,
                "reason": reason,
                "restart_disabled": True,
                "error": "final_evidence_restart_barrier_active",
                "ok": False,
            }
            self.events.append(result)
            return result
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


def database_file_sizes(database_dir: Path) -> dict[str, int]:
    """Snapshot transient SQLite files without losing the monitor to a race."""

    if not database_dir.exists():
        return {}
    sizes: dict[str, int] = {}
    for path in database_dir.glob("*.db*"):
        try:
            if path.is_file():
                sizes[path.name] = path.stat().st_size
        except FileNotFoundError:
            # WAL/SHM sidecars may disappear between glob(), is_file(), and
            # stat() when SQLite checkpoints or closes a connection.
            continue
    return sizes


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
                    db_sizes = database_file_sizes(database_dir)
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
        raw_runner_auth_key = getattr(args, "_runner_auth_key", None)
        self.runner_auth_key = (
            bytes(raw_runner_auth_key)
            if isinstance(raw_runner_auth_key, (bytes, bytearray))
            else None
        )
        if self.supervised and (
            self.runner_auth_key is None or len(self.runner_auth_key) != 32
        ):
            raise RuntimeError("supervised runner session authentication is unavailable")
        self.control_auth_sequences: dict[str, int] = {}
        self.control_auth_lock = threading.Lock()
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
        if self.runner_auth_key is not None:
            authenticated_baselines = (
                ("runner_heartbeat", self.heartbeat_path),
                ("runner_checkpoint", self.checkpoint_path),
            )
            for stream, path in authenticated_baselines:
                payload = load_json(path)
                evidence = verify_authenticated_payload(
                    payload,
                    session_secret=self.runner_auth_key,
                    expected_campaign_uuid=self.campaign_uuid,
                    expected_stream=stream,
                )
                if stream == "runner_heartbeat":
                    heartbeat = payload.get("heartbeat")
                    if (
                        not isinstance(heartbeat, Mapping)
                        or int(heartbeat.get("orchestrator_monotonic_ns") or 0)
                        != int(evidence.get("monotonic_ns") or 0)
                    ):
                        raise RuntimeError("authenticated runner heartbeat monotonic binding is invalid")
                self.control_auth_sequences[stream] = int(evidence["sequence"])
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
            if self.supervised and self.campaign_level in {"rehearsal", "soak", "formal"}
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
        auxiliary_workers = managed_auxiliary_worker_count(
            requested_workers=args.workers,
            supervised=self.supervised,
            campaign_level=self.campaign_level,
        )
        strict_server_readiness = managed_strict_readiness(
            supervised=self.supervised,
            campaign_level=self.campaign_level,
        )
        server_safety_callback = (
            wait_for_runner_host_safety_preflight if self.supervised else None
        )
        self.primary = ServerController(
            name="primary",
            run_root=self.root / "primary",
            port=primary_port,
            credentials=self.credentials,
            workers=args.workers,
            threads=args.threads,
            planned_outage=self.primary_outage,
            strict_readiness=strict_server_readiness,
            process_registry=self.process_registry if self.supervised else None,
            process_role="primary",
            progress_callback=self._server_progress if self.supervised else None,
            post_bootstrap_safety_callback=server_safety_callback,
        )
        self.recovery = ServerController(
            name="recovery",
            run_root=self.root / "recovery",
            port=recovery_port,
            credentials=self.credentials,
            workers=auxiliary_workers,
            threads=args.threads,
            planned_outage=self.recovery_outage,
            strict_readiness=strict_server_readiness,
            process_registry=self.process_registry if self.supervised else None,
            process_role="recovery",
            progress_callback=self._server_progress if self.supervised else None,
            post_bootstrap_safety_callback=server_safety_callback,
        )
        self.security_sentinel = ServerController(
            name="security_sentinel",
            run_root=self.root / "security_sentinel",
            port=security_port,
            credentials=self.credentials,
            workers=auxiliary_workers,
            threads=args.threads,
            planned_outage=self.security_outage,
            security="on",
            server_mode="production",
            strict_readiness=False,
            process_registry=self.process_registry if self.supervised else None,
            process_role="security_sentinel",
            progress_callback=self._server_progress if self.supervised else None,
            post_bootstrap_safety_callback=server_safety_callback,
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
        self.drift: dict[str, dict[str, str]] = {}
        self.source_freezer: GitSourceFreezer | None = None
        self.source_h0_authority: dict[str, Any] = {}
        if self.supervised:
            self.source_freezer = GitSourceFreezer(ROOT, self.root / "artifacts" / "source")
            self.source_h0_authority = dict(
                self.source_freezer.load_baseline(Path(args.source_freeze_path))
            )
            (
                self.source_hashes,
                self.source_digest,
                self.source_manifest_file_count,
                self.source_git,
            ) = supervised_source_identity(self.source_h0_authority)
        else:
            self.source_hashes = source_manifest()
            self.source_digest = manifest_digest(self.source_hashes)
            self.source_manifest_file_count = len(self.source_hashes)
            self.source_git = git_metadata()
        self.rehearsal_projection_context: dict[str, Any] = {}
        self.native_outer_authority_identity: dict[str, Any] = {}
        self.native_scenario_authority_identities: dict[str, dict[str, Any]] = {}
        self._initialize_native_scenario_authorities()
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
                require_gpu=self.campaign_level in {"rehearsal", "soak", "formal"},
                comfyui_queue_url=f"{comfyui_url}/queue" if comfyui_url else "",
                require_comfyui_queue=self.campaign_level in {"rehearsal", "soak", "formal"},
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

    def _initialize_native_scenario_authorities(self) -> None:
        """Pin the eight caller-owned fields before any scenario can execute."""

        locator = str(os.environ.get(REHEARSAL_PROJECTION_CONTEXT_ENV) or "")
        declared_digest = str(
            os.environ.get(REHEARSAL_PROJECTION_CONTEXT_SHA256_ENV) or ""
        )
        if bool(locator) != bool(declared_digest):
            raise RuntimeError("rehearsal projection locator/digest pair is incomplete")
        projection: dict[str, Any] = {}
        if locator:
            if not (self.supervised and self.campaign_level == "rehearsal"):
                raise RuntimeError(
                    "formal rehearsal projection is only valid in a supervised rehearsal"
                )
            projection = read_sealed_rehearsal_projection_context(
                locator,
                declared_digest,
            )
            capture = projection["capture_context"]
            if self.source_h0_authority:
                protected_digest = protected_source_identity_digest(
                    str(
                        self.source_h0_authority.get(
                            "protected_ignored_manifest_digest"
                        )
                        or ""
                    ),
                    str(
                        self.source_h0_authority.get(
                            "protected_ignored_content_digest"
                        )
                        or ""
                    ),
                )
                if (
                    capture.get("commit") != self.source_h0_authority.get("commit")
                    or capture.get("source_digest")
                    != self.source_h0_authority.get("tracked_content_digest")
                    or capture.get("protected_source_digest") != protected_digest
                ):
                    raise RuntimeError(
                        "sealed rehearsal projection differs from the supervisor H0 source"
                    )
            expected_native_root = (
                self.root / "artifacts" / "formal_native_rehearsal"
            ).resolve(strict=False)
            for role, raw_path in projection["native_artifact_paths"].items():
                path = Path(str(raw_path)).resolve(strict=False)
                if path.parent != expected_native_root:
                    raise RuntimeError(
                        f"rehearsal native artifact escapes the reviewed root: {role}"
                    )
            self.rehearsal_projection_context = projection
            outer = {
                "qualification_campaign_uuid": capture[
                    "qualification_campaign_uuid"
                ],
                "campaign_uuid": self.campaign_uuid,
                "campaign_attempt_uuid": projection["campaign_attempt_uuid"],
                "native_invocation_id": projection["outer_native_invocation_id"],
                "commit": capture["commit"],
                "source_digest": capture["source_digest"],
                "protected_source_digest": capture[
                    "protected_source_digest"
                ],
            }
            scenario_authorities = projection["scenario_authorities"]
        else:
            h0 = self.source_h0_authority
            commit = str(
                h0.get("commit") or self.source_git.get("target_commit") or ""
            )
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                commit = hashlib.sha1(
                    canonical_digest(self.source_git).encode("ascii")
                ).hexdigest()
            source_digest = str(
                h0.get("tracked_content_digest") or self.source_digest
            )
            if not re.fullmatch(r"[0-9a-f]{64}", source_digest):
                source_digest = hashlib.sha256(
                    source_digest.encode("utf-8", errors="surrogatepass")
                ).hexdigest()
            protected_digest = (
                protected_source_identity_digest(
                    str(h0.get("protected_ignored_manifest_digest") or ""),
                    str(h0.get("protected_ignored_content_digest") or ""),
                )
                if h0
                else hashlib.sha256(b"local-unqualified-protected-source").hexdigest()
            )
            campaign = self.campaign_uuid or f"local-campaign:{secrets.token_hex(16)}"
            outer = {
                "qualification_campaign_uuid": f"qualification:{campaign}",
                "campaign_uuid": campaign,
                "campaign_attempt_uuid": f"campaign-attempt:{secrets.token_hex(16)}",
                "native_invocation_id": f"operational:{secrets.token_hex(16)}",
                "commit": commit,
                "source_digest": source_digest,
                "protected_source_digest": protected_digest,
            }
            scenario_authorities = {
                scenario_id: {
                    "scenario_attempt_uuid": (
                        f"scenario-attempt:{secrets.token_hex(16)}"
                    ),
                    "native_invocation_id": (
                        f"scenario-invocation:{secrets.token_hex(16)}"
                    ),
                }
                for scenario_id in FORMAL_SCENARIO_BINDINGS
            }
        self.native_outer_authority_identity = dict(outer)
        self.native_scenario_authority_identities = {
            scenario_id: {
                **outer,
                "scenario_attempt_uuid": str(
                    scenario_authorities[scenario_id]["scenario_attempt_uuid"]
                ),
                "native_invocation_id": str(
                    scenario_authorities[scenario_id]["native_invocation_id"]
                ),
            }
            for scenario_id in FORMAL_SCENARIO_BINDINGS
        }

    def base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("PYTHONPYCACHEPREFIX", None)
        env.update(self.credentials.child_env())
        env.update({
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
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
        payload = {
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
        }
        if self.runner_auth_key is None:
            durable_atomic_write_json(self.heartbeat_path, payload)
            return
        with self.control_auth_lock:
            sequence = int(self.control_auth_sequences.get("runner_heartbeat") or 0) + 1
            signed = sign_authenticated_payload(
                payload,
                session_secret=self.runner_auth_key,
                campaign_uuid=self.campaign_uuid,
                stream="runner_heartbeat",
                sequence=sequence,
                monotonic_ns=progress_ns,
            )
            durable_atomic_write_json(self.heartbeat_path, signed)
            self.control_auth_sequences["runner_heartbeat"] = sequence

    def _commit_checkpoint(self, payload: Mapping[str, Any]) -> None:
        """Atomically persist and read back both volatile and reboot-safe copies."""

        def commit(committed_payload: Mapping[str, Any]) -> None:
            durable_atomic_write_json(self.checkpoint_path, committed_payload)
            if self.checkpoint_mirror_path is None:
                return
            mirror_parent = self.checkpoint_mirror_path.parent
            mirror_parent.mkdir(parents=True, exist_ok=True)
            os.chmod(mirror_parent, 0o700)
            durable_atomic_write_json(self.checkpoint_mirror_path, committed_payload)
            primary = load_json(self.checkpoint_path)
            mirror = load_json(self.checkpoint_mirror_path)
            if primary != dict(committed_payload) or mirror != dict(committed_payload) or primary != mirror:
                raise RuntimeError("campaign checkpoint primary/mirror readback mismatch")
            if self.checkpoint_mirror_path.stat().st_mode & 0o077:
                raise RuntimeError("campaign checkpoint mirror permissions are not private")

        if getattr(self, "runner_auth_key", None) is None:
            commit(payload)
            return
        with self.control_auth_lock:
            sequence = int(self.control_auth_sequences.get("runner_checkpoint") or 0) + 1
            signed = sign_authenticated_payload(
                payload,
                session_secret=self.runner_auth_key,
                campaign_uuid=self.campaign_uuid,
                stream="runner_checkpoint",
                sequence=sequence,
                monotonic_ns=time.monotonic_ns(),
            )
            commit(signed)
            self.control_auth_sequences["runner_checkpoint"] = sequence

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

    def run_group(
        self,
        scenario_id: str,
        steps: list[Callable[[], dict[str, Any]]],
        *,
        formal_evidence_manifest: Path | None = None,
    ) -> dict[str, Any]:
        started_at = utc_now()
        started = time.monotonic()
        results: list[dict[str, Any]] = []
        for step in steps:
            if self.stop_event.is_set():
                results.append({"ok": False, "error": "campaign_stopping"})
                break
            step_result = step()
            results.append(step_result)
            # Formal scenario steps are ordered dependencies.  Continuing
            # after a failed producer can trigger an unsafe consumer (for
            # example consuming a restart receipt from a probe that did not
            # reach its terminal cleanup/audit state).  Every individual
            # step owns its failure cleanup; the group itself therefore
            # fails closed and never starts later steps after a non-PASS.
            if step_result.get("ok") is not True:
                break
        execution_succeeded = bool(results) and all(item.get("ok") for item in results)
        artifacts: list[dict[str, str]] = []
        for index, result in enumerate(results):
            artifact_path = str(result.get("artifact") or "").strip()
            if not artifact_path:
                continue
            raw_step_id = str(result.get("step_id") or f"step_{index}")
            step_id = re.sub(r"[^a-z0-9_.-]+", "_", raw_step_id.lower()).strip("_.-")
            artifacts.append({
                "artifact_id": f"native.source.{scenario_id}.{step_id or f'step_{index}'}",
                "path": str(Path(artifact_path).expanduser().resolve(strict=False)),
                "artifact_type": "auto",
            })
        return {
            "schema_version": NATIVE_RUNNER_RESULT_SCHEMA_VERSION,
            "scenario_id": scenario_id,
            "ok": execution_succeeded,
            "execution_succeeded": execution_succeeded,
            "terminal_state": "success" if execution_succeeded else "failed",
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "steps": results,
            "artifacts": artifacts,
            "formal_evidence_manifest": (
                str(formal_evidence_manifest.expanduser().resolve(strict=False))
                if formal_evidence_manifest is not None
                else ""
            ),
        }

    def run_native_callable_step(
        self,
        scenario_id: str,
        step_id: str,
        artifact: Path,
        callback: Callable[[], dict[str, Any]],
        *,
        payload_ok: Callable[[Mapping[str, Any]], bool],
    ) -> dict[str, Any]:
        """Run an in-process domain operation with the same evidence shape as a probe."""

        started_at = utc_now()
        started = time.monotonic()
        payload: dict[str, Any]
        try:
            value = callback()
            payload = dict(value) if isinstance(value, Mapping) else {
                "error": "domain_callback_result_invalid",
            }
        except Exception as exc:
            payload = {
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        try:
            semantic_pass = payload_ok(payload) is True
        except Exception as exc:
            semantic_pass = False
            payload["validation_error"] = f"{exc.__class__.__name__}: {exc}"
        payload["semantic_pass"] = semantic_pass
        atomic_write_json(artifact, payload)
        return {
            "step_id": step_id,
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "returncode": 0 if semantic_pass else 1,
            "timed_out": False,
            "command": ["internal_domain_operation", step_id],
            "stdout": "",
            "artifact": str(artifact),
            "artifact_summary": {"semantic_pass": semantic_pass},
            "evidence_errors": [] if semantic_pass else ["domain_assertion_failed"],
            "secret_leak_labels": [],
            "ok": semantic_pass,
        }

    def _cleanup_exact_scenario_users(
        self,
        usernames: list[str],
        *,
        target: Any | None = None,
    ) -> dict[str, Any]:
        """Delete only the exact scenario-owned users and prove their names vanished."""

        exact_names = sorted({
            str(username or "").strip()
            for username in usernames
            if str(username or "").strip() not in {"", "root", "admin", "test"}
        })
        controller = target or self.primary
        root = WebClient(controller.base_url, "root", self.credentials.root, timeout=60)
        login = root.login()
        records: list[dict[str, Any]] = []
        if not login.get("ok"):
            return {
                "login_succeeded": False,
                "requested_usernames": exact_names,
                "records": records,
            }
        for username in exact_names:
            lookup = root.request(
                "GET",
                "/api/admin/users",
                params={"q": username, "page_size": 100},
            )
            rows = (
                ((lookup.get("body") or {}).get("users") or [])
                if isinstance(lookup.get("body"), Mapping)
                else []
            )
            exact = next(
                (row for row in rows if str(row.get("username") or "") == username),
                None,
            )
            user_id = int((exact or {}).get("id") or 0)
            deleted = (
                root.request("DELETE", f"/api/admin/users/{user_id}")
                if user_id > 0
                else {"status": 0}
            )
            verify = root.request(
                "GET",
                "/api/admin/users",
                params={"q": username, "page_size": 100},
            )
            verify_rows = (
                ((verify.get("body") or {}).get("users") or [])
                if isinstance(verify.get("body"), Mapping)
                else []
            )
            residual = [
                row for row in verify_rows
                if str(row.get("username") or "") == username
            ]
            records.append({
                "username": username,
                "user_id": user_id,
                "delete_status": int(deleted.get("status") or 0),
                "verify_status": int(verify.get("status") or 0),
                "deleted": int(deleted.get("status") or 0) == 200,
                "residual_exact_count": len(residual),
            })
        return {
            "login_succeeded": True,
            "requested_usernames": exact_names,
            "records": records,
        }

    def native_points_hft_invariants(self) -> dict[str, Any]:
        """Execute the exact reviewed PointsChain HFT scenario and bind its artifacts."""

        scenario_id = "pointschain_hft_invariants"
        out_dir = self.reports / "scenarios" / scenario_id
        stress = out_dir / "points_chain_destructive_stress.json"
        dispute = out_dir / "pointschain_dispute_api.json"
        frontend = out_dir / "points_chain_post_stress.json"
        cleanup = out_dir / "fixture_account_cleanup.json"
        direct_ops = 12000
        transfer_ops = 1200
        trading_ops = 600

        def cleanup_step() -> dict[str, Any]:
            stress_payload = load_json(stress)
            dispute_payload = load_json(dispute)
            usernames = list(stress_payload.get("fixture_usernames") or [])
            usernames.extend(dispute_payload.get("fixture_usernames") or [])
            return self._cleanup_exact_scenario_users([str(value) for value in usernames])

        result = self.run_group(scenario_id, [
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
                "branch_and_dispute_api",
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
                "post_stress_desktop_mobile",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "points_chain_post_stress_playwright.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(frontend),
                    "--member-username", "admin",
                ],
                timeout=1200,
                artifact=frontend,
                process_role="browser",
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "fixture_account_cleanup",
                cleanup,
                cleanup_step,
                payload_ok=lambda payload: bool(
                    payload.get("login_succeeded") is True
                    and payload.get("records")
                    and all(
                        row.get("deleted") is True
                        and int(row.get("residual_exact_count") or 0) == 0
                        for row in payload.get("records") or []
                    )
                ),
            ),
        ])
        selected = pointschain_hft_assertions(
            load_json(stress),
            load_json(dispute),
            load_json(frontend),
            load_json(cleanup),
        )
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    def native_wallet_incident_governance(self) -> dict[str, Any]:
        """Run a real compromise, freeze, vote, compensation, and branch cycle."""

        scenario_id = "wallet_incident_governance"
        out_dir = self.reports / "scenarios" / scenario_id
        stop_out = out_dir / "recovery_stop.json"
        realistic_out = out_dir / "realistic_wallet_incident.json"
        start_out = out_dir / "recovery_start.json"
        replay_out = out_dir / "replay_and_wrong_branch_rejection.json"
        branch_out = out_dir / "governed_recovery_branch.json"
        cleanup_out = out_dir / "fixture_account_cleanup.json"
        final_out = out_dir / "post_cleanup_chain_state.json"

        def stop_recovery() -> dict[str, Any]:
            old_pid = self.recovery.pid()
            stopped = self.recovery.stop(reason="formal_wallet_incident_offline_drill")
            return {
                "old_pid": old_pid,
                "stop_succeeded": stopped.get("ok") is True,
                "master_process_remaining": stopped.get("master_process_remaining"),
                "process_group_remaining": stopped.get("process_group_remaining"),
            }

        def start_recovery() -> dict[str, Any]:
            started = self.recovery.start()
            ready = started.get("ready") if isinstance(started.get("ready"), Mapping) else {}
            return {
                "new_pid": int(started.get("pid") or 0),
                "start_succeeded": started.get("ok") is True,
                "readiness_succeeded": ready.get("ok") is True,
            }

        def branch_command() -> list[str]:
            realistic = load_json(realistic_out)
            incident = realistic.get("incident") if isinstance(realistic.get("incident"), Mapping) else {}
            users = realistic.get("users") if isinstance(realistic.get("users"), Mapping) else {}
            victim = users.get("victim") if isinstance(users.get("victim"), Mapping) else {}
            return [
                sys.executable,
                str(ROOT / "scripts" / "testing" / "pointschain_live_branch_drill.py"),
                "--base-url", self.recovery.base_url,
                "--incident-tx-hash", str(incident.get("theft_tx_hash") or ""),
                "--victim-wallet", str(victim.get("wallet") or ""),
                "--claim-amount", str(int(incident.get("claimed_amount") or 0)),
                "--out", str(branch_out),
            ]

        def cleanup_users() -> dict[str, Any]:
            realistic = load_json(realistic_out)
            replay = load_json(replay_out)
            users = realistic.get("users") if isinstance(realistic.get("users"), Mapping) else {}
            usernames = [
                str(row.get("username") or "")
                for row in users.values()
                if isinstance(row, Mapping)
            ]
            usernames.extend(str(value) for value in (replay.get("fixture_usernames") or []))
            return self._cleanup_exact_scenario_users(usernames, target=self.recovery)

        def final_chain_state() -> dict[str, Any]:
            realistic = load_json(realistic_out)
            incident = realistic.get("incident") if isinstance(realistic.get("incident"), Mapping) else {}
            theft_hash = str(incident.get("theft_tx_hash") or "")
            root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=90)
            login = root.login()
            if not login.get("ok"):
                return {"root_login_succeeded": False}
            started = root.request("POST", "/api/root/points/chain/verify/jobs", json_body={})
            job = self._await_management_job(root, started, timeout_seconds=900)
            return {
                "root_login_succeeded": True,
                "readiness": self.recovery.wait_ready(timeout=180.0),
                "points_verify_job": job,
                "points_verify_latest": root.request("GET", "/api/root/points/chain/verify/latest"),
                "theft_explorer": (
                    root.request("GET", f"/api/points/explorer/tx/{quote(theft_hash, safe='')}")
                    if theft_hash else {"status": 0}
                ),
            }

        result = self.run_group(scenario_id, [
            lambda: self.run_native_callable_step(
                scenario_id,
                "stop_recovery_for_offline_incident",
                stop_out,
                stop_recovery,
                payload_ok=lambda payload: bool(
                    payload.get("stop_succeeded") is True
                    and payload.get("master_process_remaining") is False
                    and payload.get("process_group_remaining") is False
                ),
            ),
            lambda: self.run_step(
                scenario_id,
                "realistic_wallet_incident",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_realistic_recovery_drill.py"),
                    "--runtime-root", str(self.recovery.runtime_root),
                    "--out", str(realistic_out),
                    "--mode", "dev_ready",
                ],
                timeout=3600,
                artifact=realistic_out,
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "restart_recovery_after_incident",
                start_out,
                start_recovery,
                payload_ok=lambda payload: bool(
                    payload.get("start_succeeded") is True
                    and payload.get("readiness_succeeded") is True
                    and int(payload.get("new_pid") or 0) > 0
                ),
            ),
            lambda: self.run_step(
                scenario_id,
                "replay_and_wrong_branch_rejection",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_dispute_api_probe.py"),
                    "--base-url", self.recovery.base_url,
                    "--runtime-root", str(self.recovery.runtime_root),
                    "--out", str(replay_out),
                ],
                timeout=1800,
                artifact=replay_out,
            ),
            lambda: self.run_step(
                scenario_id,
                "governed_recovery_branch",
                branch_command(),
                timeout=3600,
                artifact=branch_out,
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "fixture_account_cleanup",
                cleanup_out,
                cleanup_users,
                payload_ok=lambda payload: bool(
                    payload.get("login_succeeded") is True
                    and payload.get("records")
                    and all(
                        row.get("deleted") is True
                        and int(row.get("residual_exact_count") or 0) == 0
                        for row in payload.get("records") or []
                    )
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "post_cleanup_chain_state",
                final_out,
                final_chain_state,
                payload_ok=lambda payload: bool(
                    payload.get("root_login_succeeded") is True
                    and (payload.get("readiness") or {}).get("ok") is True
                    and (payload.get("points_verify_job") or {}).get("terminal_status") == "succeeded"
                    and int((payload.get("theft_explorer") or {}).get("status") or 0) == 200
                ),
            ),
        ])
        selected = wallet_incident_assertions(
            load_json(realistic_out),
            load_json(replay_out),
            load_json(branch_out),
            load_json(final_out),
            load_json(cleanup_out),
        )
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    @staticmethod
    def _shared_hls_observation(
        session: requests.Session,
        *,
        base_url: str,
        share_token: str,
        share_session: str,
    ) -> dict[str, Any]:
        playback = session.get(
            f"{base_url}/api/videos/shared/{share_token}/playback",
            params={"share_session": share_session},
            timeout=60,
            verify=False,
        )
        try:
            payload = playback.json()
        except Exception:
            payload = {}
        master_url = str(payload.get("master_url") or "") if isinstance(payload, Mapping) else ""
        master = session.get(urljoin(base_url + "/", master_url), timeout=60, verify=False) if master_url else None
        master_text = master.text if master is not None and master.status_code == 200 else ""
        master_paths = [
            line.strip()
            for line in master_text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        variant_url = urljoin(master.url if master is not None else base_url + "/", master_paths[0]) if master_paths else ""
        variant = session.get(variant_url, timeout=60, verify=False) if variant_url else None
        variant_text = variant.text if variant is not None and variant.status_code == 200 else ""
        segment_paths = [
            line.strip()
            for line in variant_text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        segment_url = urljoin(variant.url if variant is not None else base_url + "/", segment_paths[-1]) if segment_paths else ""
        segment = session.get(segment_url, timeout=60, verify=False) if segment_url else None
        return {
            "playback_status": playback.status_code,
            "mode": payload.get("mode") if isinstance(payload, Mapping) else "",
            "streaming_ready": payload.get("streaming_ready") is True if isinstance(payload, Mapping) else False,
            "duration_seconds": (
                payload.get("duration_seconds")
                or (_mapping_status.get("duration_seconds") if isinstance((_mapping_status := payload.get("status")), Mapping) else None)
            ) if isinstance(payload, Mapping) else None,
            "variant_names": [
                str(row.get("name") or "")
                for row in (payload.get("variants") or [])
                if isinstance(row, Mapping)
            ] if isinstance(payload, Mapping) else [],
            "audio_track_count": len(payload.get("audio_tracks") or []) if isinstance(payload, Mapping) else 0,
            "subtitle_count": len(payload.get("subtitles") or []) if isinstance(payload, Mapping) else 0,
            "master_status": master.status_code if master is not None else 0,
            "master_extm3u": "#EXTM3U" in master_text,
            "variant_status": variant.status_code if variant is not None else 0,
            "variant_segment_count": len(segment_paths),
            "sample_segment_status": segment.status_code if segment is not None else 0,
            "sample_segment_bytes": len(segment.content) if segment is not None else 0,
        }

    def _media_restart_continuity(self, stress_path: Path, share_password: str) -> dict[str, Any]:
        stress = load_json(stress_path)
        upload_phase = next(
            (
                phase for phase in stress.get("phases") or []
                if isinstance(phase, Mapping) and phase.get("phase") == "upload"
            ),
            {},
        )
        uploads = [
            row for row in upload_phase.get("uploads") or []
            if isinstance(row, Mapping) and row.get("ok") is True
        ]
        passwords = {username: password for username, password in self.accounts}
        continuity_upload = uploads[0] if uploads else {}
        username = str(continuity_upload.get("username") or "")
        video_id = int(continuity_upload.get("video_id") or 0)
        owner = WebClient(self.primary.base_url, username, passwords.get(username, ""), timeout=90)
        result: dict[str, Any] = {
            "upload_video_count": len(uploads),
            "before_restart": {},
            "after_restart": {},
            "restart": {},
            "cleanup": {},
        }
        if not uploads:
            result["cleanup"] = {
                "continuity_share_revoked": False,
                "post_revoke_denied": False,
                "expected_video_count": 0,
                "deleted_video_count": 0,
                "all_videos_absent": False,
            }
            return result
        share_token = ""
        anonymous = requests.Session()
        anonymous.verify = False
        share_session = ""
        try:
            owner_login = owner.login()
            share = owner.request(
                "PUT",
                f"/api/videos/{video_id}/share-link",
                json_body={
                    "regenerate": True,
                    "share_password": share_password,
                },
            ) if owner_login.get("ok") and video_id > 0 else {"body": {}}
            share_link = (share.get("body") or {}).get("share_link") or {}
            share_token = str(share_link.get("token") or "")
            if not share_token:
                share_url = str(share_link.get("url") or "")
                if "/shared/videos/" in share_url:
                    share_token = share_url.split("/shared/videos/", 1)[1].split("?", 1)[0].split("#", 1)[0]
            csrf_response = anonymous.get(
                f"{self.primary.base_url}/api/csrf-token",
                timeout=30,
                verify=False,
            )
            csrf_body = csrf_response.json() if csrf_response.content else {}
            csrf = str(csrf_body.get("csrf_token") or anonymous.cookies.get("csrf_token") or "")
            unlock = anonymous.post(
                f"{self.primary.base_url}/api/videos/shared/{share_token}/unlock",
                json={"password": share_password},
                headers={"X-CSRF-Token": csrf},
                timeout=30,
                verify=False,
            ) if share_token else None
            unlock_body = unlock.json() if unlock is not None and unlock.content else {}
            share_session = str(unlock_body.get("share_session_id") or "")
            result["share_created"] = bool(
                share.get("status") == 200
                and share_token
                and unlock is not None
                and unlock.status_code == 200
                and share_session
            )
            if not result["share_created"]:
                return result
            result["before_restart"] = self._shared_hls_observation(
                anonymous,
                base_url=self.primary.base_url,
                share_token=share_token,
                share_session=share_session,
            )
            old_pid = self.primary.pid()
            restart = self.primary.restart(reason="formal_long_media_continuity")
            start_event = restart.get("started") if isinstance(restart.get("started"), Mapping) else {}
            stop_event = restart.get("stopped") if isinstance(restart.get("stopped"), Mapping) else {}
            result["restart"] = {
                "stopped": {
                    "old_pid": old_pid,
                    "master_process_remaining": stop_event.get("master_process_remaining"),
                    "process_group_remaining": stop_event.get("process_group_remaining"),
                },
                "started": {
                    "new_pid": int(start_event.get("pid") or 0),
                    "ready": bool((start_event.get("ready") or {}).get("ok")),
                },
                "elapsed_seconds": restart.get("elapsed_seconds"),
            }
            if restart.get("ok"):
                result["after_restart"] = self._shared_hls_observation(
                    anonymous,
                    base_url=self.primary.base_url,
                    share_token=share_token,
                    share_session=share_session,
                )
        finally:
            if self.primary.pid() <= 0:
                recovery_start = self.primary.start()
                result["server_recovery_start_succeeded"] = recovery_start.get("ok") is True
            deleted = 0
            all_absent = True
            revoke_succeeded = False
            post_revoke_denied = False
            for row in uploads:
                account = str(row.get("username") or "")
                target_id = int(row.get("video_id") or 0)
                client = WebClient(self.primary.base_url, account, passwords.get(account, ""), timeout=60)
                if not client.login().get("ok"):
                    all_absent = False
                    continue
                if target_id == video_id:
                    revoke = client.request("DELETE", f"/api/videos/{target_id}/share-link")
                    revoke_succeeded = revoke.get("status") == 200
                    if share_token:
                        denied = anonymous.get(
                            f"{self.primary.base_url}/api/videos/shared/{share_token}/playback",
                            params={"share_session": share_session},
                            timeout=30,
                            verify=False,
                        )
                        post_revoke_denied = denied.status_code in {404, 410}
                removed = client.request("DELETE", f"/api/videos/{target_id}/manage")
                verify = client.request("GET", f"/api/videos/{target_id}")
                absent = removed.get("status") == 200 and verify.get("status") == 404
                deleted += int(absent)
                all_absent = all_absent and absent
            result["cleanup"] = {
                "continuity_share_revoked": revoke_succeeded,
                "post_revoke_denied": post_revoke_denied,
                "expected_video_count": len(uploads),
                "deleted_video_count": deleted,
                "all_videos_absent": all_absent and bool(uploads),
            }
        return result

    def native_media_long_hls_share(self) -> dict[str, Any]:
        """Execute the 3900-second multi-account HLS scenario with planned restart."""

        scenario_id = "media_long_hls_share"
        out_dir = self.reports / "scenarios" / scenario_id
        stress_out = out_dir / "video_hls_quality_stress.json"
        restart_out = out_dir / "planned_restart_continuity.json"
        fixture = self.root / "fixtures" / "campaign_long_video.mkv"
        share_password = secrets.token_urlsafe(24)
        account_rows = [
            {"username": username, "password": password}
            for username, password in self.accounts[:3]
        ]
        result = self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "long_video_hls_share",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "video_hls_quality_stress.py"),
                    "--base-url", self.primary.base_url,
                    "--video", str(fixture),
                    "--db", str(self.primary.runtime_root / "database" / "database.db"),
                    "--runtime-marker", str(self.primary.run_root),
                    "--out", str(stress_out),
                    "--generate-fixture-duration-seconds", "3900",
                    "--fixture-timeout-seconds", "1800",
                    "--minimum-source-duration-seconds", "3600",
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
                    "--minimum-segments-per-variant", "100",
                    "--segment-concurrency", "8",
                    "--max-segments-per-variant", "16",
                    "--post-upload-observe-seconds", "5",
                    "--upload-timeout-seconds", "1800",
                    "--wait-timeout-seconds", "21600",
                    "--wait-interval-seconds", "15",
                    "--orphan-grace-seconds", "900",
                ],
                timeout=8 * 60 * 60,
                artifact=stress_out,
                process_role="ffmpeg",
                env={
                    "HACKME_HLS_STRESS_ACCOUNTS_JSON": json.dumps(account_rows),
                    "HACKME_HLS_SHARE_PASSWORD": share_password,
                    "HACKME_PROBE_ROOT_PASSWORD": self.credentials.root,
                },
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "planned_restart_hls_continuity_and_cleanup",
                restart_out,
                lambda: self._media_restart_continuity(stress_out, share_password),
                payload_ok=lambda payload: bool(
                    (payload.get("after_restart") or {}).get("streaming_ready") is True
                    and (payload.get("cleanup") or {}).get("all_videos_absent") is True
                ),
            ),
        ])
        selected = media_long_assertions(load_json(stress_out), load_json(restart_out))
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    def native_bt_download_stream_restart(self) -> dict[str, Any]:
        """Run local-only BT lifecycle then stream that exact download via HLS."""

        scenario_id = "bt_download_stream_restart"
        out_dir = self.reports / "scenarios" / scenario_id
        bt_out = out_dir / "bt_formal_local_probe.json"
        bt_artifact_parent = out_dir / "bt_retained_artifacts"
        bt_runtime_parent = self.root / "bt_runtime"
        stress_out = out_dir / "downloaded_video_hls.json"
        restart_out = out_dir / "downloaded_video_restart_continuity.json"
        share_password = secrets.token_urlsafe(24)
        account_rows = [
            {"username": username, "password": password}
            for username, password in self.accounts[:1]
        ]

        def bt_report_passes(payload: Mapping[str, Any]) -> bool:
            try:
                validation_errors = validate_bt_machine_report(dict(payload))
            except (TypeError, ValueError, OSError):
                return False
            checks = payload.get("checks")
            return bool(
                not validation_errors
                and payload.get("ok") is True
                and payload.get("terminal_state") == "success"
                and isinstance(payload.get("errors"), list)
                and not payload.get("errors")
                and isinstance(checks, Mapping)
                and set(checks) == set(BT_MANDATORY_CHECK_IDS)
                and all(
                    isinstance(checks.get(check_id), Mapping)
                    and checks[check_id].get("mandatory") is True
                    and checks[check_id].get("ok") is True
                    for check_id in BT_MANDATORY_CHECK_IDS
                )
            )

        def magnet_download_path() -> str:
            payload = load_json(bt_out)
            raw = payload.get("raw") if isinstance(payload.get("raw"), Mapping) else {}
            magnet = raw.get("magnet") if isinstance(raw.get("magnet"), Mapping) else {}
            return str(magnet.get("download_path") or "")

        result = self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "controlled_local_bt_lifecycle",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "bt_formal_local_probe.py"),
                    "--out", str(bt_out),
                    "--artifact-dir", str(bt_artifact_parent),
                    "--runtime-root", str(bt_runtime_parent),
                    "--timeout-seconds", "600",
                    "--payload-bytes", str(8 * 1024 * 1024),
                    "--download-limit-kib-per-second", "192",
                    "--pause-after-bytes", str(256 * 1024),
                    "--pause-observation-seconds", "2",
                ],
                timeout=30 * 60,
                artifact=bt_out,
                payload_ok=bt_report_passes,
                process_role="bt",
            ),
            lambda: self.run_step(
                scenario_id,
                "same_download_video_hls_share",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "video_hls_quality_stress.py"),
                    "--base-url", self.primary.base_url,
                    "--video", magnet_download_path(),
                    "--db", str(self.primary.runtime_root / "database" / "database.db"),
                    "--runtime-marker", str(self.primary.run_root),
                    "--out", str(stress_out),
                    "--visibility", "unlisted",
                    "--privacy-mode", "server_encrypted",
                    "--upload",
                    "--wait",
                    "--measure",
                    "--verify-share",
                    "--minimum-segments-per-variant", "1",
                    "--segment-concurrency", "4",
                    "--max-segments-per-variant", "12",
                    "--post-upload-observe-seconds", "5",
                    "--upload-timeout-seconds", "1800",
                    "--wait-timeout-seconds", "7200",
                    "--wait-interval-seconds", "10",
                    "--orphan-grace-seconds", "600",
                ],
                timeout=4 * 60 * 60,
                artifact=stress_out,
                payload_ok=lambda payload: bool(
                    payload.get("ok") is True
                    and payload.get("verdict") == "PASS"
                    and {str(row.get("phase") or "") for row in payload.get("phases") or [] if isinstance(row, Mapping)}
                    == {"upload", "wait", "measure", "share"}
                    and all(
                        row.get("ok") is True
                        for row in payload.get("phases") or []
                        if isinstance(row, Mapping)
                    )
                ),
                process_role="ffmpeg",
                env={
                    "HACKME_HLS_STRESS_ACCOUNTS_JSON": json.dumps(account_rows),
                    "HACKME_HLS_SHARE_PASSWORD": share_password,
                    "HACKME_PROBE_ROOT_PASSWORD": self.credentials.root,
                },
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "same_download_primary_restart_continuity_cleanup",
                restart_out,
                lambda: self._media_restart_continuity(stress_out, share_password),
                payload_ok=lambda payload: bool(
                    payload.get("share_created") is True
                    and (payload.get("after_restart") or {}).get("streaming_ready") is True
                    and (payload.get("cleanup") or {}).get("continuity_share_revoked") is True
                    and (payload.get("cleanup") or {}).get("post_revoke_denied") is True
                    and (payload.get("cleanup") or {}).get("all_videos_absent") is True
                ),
            ),
        ])

        # Retain and hash the actual .torrent, both downloaded payloads,
        # ffprobe records, trace, and daemon logs in addition to the three
        # step JSON reports.  Missing items remain a selector failure instead
        # of being represented by an unverifiable manifest declaration.
        bt_payload = load_json(bt_out)
        declared_ids = {
            str(row.get("artifact_id") or "")
            for row in result.get("artifacts") or []
            if isinstance(row, Mapping)
        }
        for row in bt_payload.get("artifacts") or []:
            if not isinstance(row, Mapping):
                continue
            source_id = re.sub(
                r"[^a-z0-9_.-]+",
                "_",
                str(row.get("artifact_id") or "").lower(),
            ).strip("_.-")
            artifact_id = f"native.source.{scenario_id}.bt.{source_id}"
            path = Path(str(row.get("path") or "")).expanduser()
            if (
                source_id
                and artifact_id not in declared_ids
                and row.get("exists") is True
                and row.get("validated") is True
                and path.is_absolute()
                and path.is_file()
                and not path.is_symlink()
            ):
                result["artifacts"].append({
                    "artifact_id": artifact_id,
                    "path": str(path.resolve(strict=True)),
                    # The probe index uses MIME labels; the formal artifact
                    # validator uses its own enum and performs stronger
                    # suffix-driven parsing when declared as ``auto``.
                    "artifact_type": "auto",
                })
                declared_ids.add(artifact_id)

        selected = bt_download_assertions(
            bt_payload,
            load_json(stress_out),
            load_json(restart_out),
        )
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    def native_media_proxy_cross_browser(self) -> dict[str, Any]:
        """Execute all proxy/browser combinations with exact terminal cleanup evidence."""

        scenario_id = "media_proxy_cross_browser"
        out_dir = self.reports / "scenarios" / scenario_id
        service_out = out_dir / "realtime_proxy_service.json"
        http_root = out_dir / "http_concurrency"
        http_out = http_root / "result.json"
        browser_root = out_dir / "browser_compat"
        browser_out = browser_root / "reports" / "qa" / "browser_video_compat.json"
        chat_out = out_dir / "chat_video_share.json"
        result = self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "realtime_proxy_service",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "realtime_proxy_stress_probe.py"),
                    "--runtime-root", str(out_dir / "service_runtime"),
                    "--json-out", str(service_out),
                    "--duration", "90",
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
                    "--json-out", str(http_out),
                    "--duration", "60",
                    "--max-concurrent", "2",
                    "--server-runner", "gunicorn",
                    "--gunicorn-workers", "3",
                    "--gunicorn-threads", "2",
                ],
                timeout=1800,
                artifact=http_out,
                process_role="ffmpeg",
            ),
            lambda: self.run_step(
                scenario_id,
                "cross_browser_video",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "playwright_browser_video_compat.py"),
                    "--runtime-root", str(browser_root),
                    "--browsers", "chromium,firefox,webkit",
                    "--require-all-browsers",
                ],
                timeout=3600,
                artifact=browser_out,
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
        selected = media_proxy_assertions(
            load_json(service_out),
            load_json(http_out),
            load_json(browser_out),
            load_json(chat_out),
        )
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    @staticmethod
    def _await_management_job(
        client: WebClient,
        started: Mapping[str, Any],
        *,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        body = _mapping_body = (
            started.get("body")
            if isinstance(started.get("body"), Mapping)
            else {}
        )
        status_url = str(_mapping_body.get("status_url") or "")
        job_uuid = str(_mapping_body.get("job_uuid") or _mapping_body.get("job_id") or "")
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        last: Mapping[str, Any] = {}
        while status_url and time.monotonic() < deadline:
            response = client.request("GET", status_url)
            response_body = response.get("body") if isinstance(response.get("body"), Mapping) else {}
            job = response_body.get("job") if isinstance(response_body.get("job"), Mapping) else {}
            last = job
            state = str(job.get("status") or "").lower()
            if state in {"succeeded", "failed", "cancelled", "error"}:
                return {
                    "job_uuid": job_uuid,
                    "terminal_status": state,
                    "stage": str(job.get("stage") or ""),
                    "progress_percent": job.get("progress_percent"),
                    "error_code": str(job.get("error_code") or ""),
                    "error_message": str(job.get("error_message") or "")[:500],
                }
            time.sleep(0.5)
        return {
            "job_uuid": job_uuid,
            "terminal_status": "timeout" if status_url else "status_url_missing",
            "stage": str(last.get("stage") or ""),
            "progress_percent": last.get("progress_percent"),
            "error_code": str(last.get("error_code") or ""),
            "error_message": str(last.get("error_message") or "")[:500],
        }

    def _final_ui_invariants(self) -> dict[str, Any]:
        root = WebClient(self.primary.base_url, "root", self.credentials.root, timeout=60)
        login = root.login()
        if not login.get("ok"):
            return {"root_login_succeeded": False}
        points_started = root.request("POST", "/api/root/points/chain/verify/jobs", json_body={})
        points_job = self._await_management_job(root, points_started)
        points_latest = root.request("GET", "/api/root/points/chain/verify/latest")
        trading_started = root.request("POST", "/api/root/trading/verify/jobs", json_body={})
        trading_job = self._await_management_job(root, trading_started)
        trading_latest = root.request("GET", "/api/root/trading/verify/latest")
        return {
            "root_login_succeeded": True,
            "readiness": self.primary.wait_ready(timeout=180.0),
            "audit_integrity": root.request("GET", "/api/admin/health/audit-chain"),
            "database_integrity": root.request("GET", "/api/admin/health/db-integrity"),
            "mode_log_chain": root.request("GET", "/api/root/server-mode/logs/verify"),
            "points_verify_job": points_job,
            "points_verify_latest": points_latest,
            "trading_verify_job": trading_job,
            "trading_verify_latest": trading_latest,
            "sqlite_quick_checks": self._sqlite_checks(self.primary.runtime_root / "database"),
        }

    def _formal_load_context(self) -> dict[str, Any]:
        checkpoint = load_json(
            self.core_root / "reports" / "operational_soak" / "operational_soak.checkpoint.json"
        )
        effective = checkpoint.get("effective_load") if isinstance(checkpoint.get("effective_load"), Mapping) else {}
        ramp = effective.get("ramp") if isinstance(effective.get("ramp"), Mapping) else {}
        target_summary = (
            effective.get("target_load_summary")
            if isinstance(effective.get("target_load_summary"), Mapping)
            else {}
        )
        target_samples = effective.get("target_load_samples")
        target_samples = target_samples if isinstance(target_samples, list) else []
        latest_target = target_samples[-1] if target_samples and isinstance(target_samples[-1], Mapping) else {}
        resource_samples = list(getattr(self.resource_monitor, "samples", []) or [])
        latest_resource = resource_samples[-1] if resource_samples else {}
        hard_limit = latest_resource.get("hard_limit_state") if isinstance(latest_resource, Mapping) else {}
        state = ""
        if self.state_machine is not None:
            try:
                state = str(self.state_machine.snapshot().get("state") or "")
            except Exception:
                state = ""
        return {
            "campaign_active": bool(
                self.active_event.is_set()
                and not self.stop_event.is_set()
                and (not state or state == "ACTIVE")
            ),
            "campaign_state": state,
            "core_load_process_alive": bool(
                self.core_process is not None and self.core_process.poll() is None
            ),
            "resource_monitor_alive": bool(self.resource_monitor.is_alive()),
            "resource_sample_count": len(resource_samples),
            "latest_resource_hard_limit_ok": (
                hard_limit.get("ok") is True if isinstance(hard_limit, Mapping) else False
            ),
            "ramp_completed_levels": list(ramp.get("completed_levels") or []),
            "target_load_coverage": target_summary.get("target_load_coverage"),
            "latest_target_sample_at_load": latest_target.get("at_target_load") is True,
            "latest_effective_load_ratio": latest_target.get("effective_load_ratio"),
        }

    def native_final_ui_mobile_prelaunch(self) -> dict[str, Any]:
        """Run the reviewed read-only UI sweep, launch gate, and final invariants."""

        scenario_id = "final_ui_mobile_prelaunch"
        out_dir = self.reports / "scenarios" / scenario_id
        ui_out = out_dir / "formal_ui_sweep.json"
        screenshot_dir = out_dir / "screenshots"
        gate_dir = out_dir / "production_gate"
        gate_out = gate_dir / "whole_site_production_gate.json"
        invariants_out = out_dir / "final_invariants.json"
        load_out = out_dir / "load_context.json"
        process_out = out_dir / "process_cleanup.json"
        baseline_rows = proc_rows()
        baseline_descendants = descendants(baseline_rows, os.getpid())

        def process_cleanup() -> dict[str, Any]:
            rows = proc_rows()
            current = descendants(rows, os.getpid())
            allowed: set[int] = {os.getpid()}
            for pid in (
                self.primary.pid(),
                self.recovery.pid(),
                self.security_sentinel.pid(),
                self.core_process.pid if self.core_process is not None else 0,
            ):
                if int(pid or 0) > 0:
                    allowed.update(descendants(rows, int(pid)))
            new_descendants = sorted(current - baseline_descendants - allowed)
            return {
                "baseline_descendant_count": len(baseline_descendants),
                "current_descendant_count": len(current),
                "new_descendant_pids": new_descendants,
            }

        result = self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "formal_read_only_ui_sweep",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "playwright_formal_ui_sweep.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(ui_out),
                    "--screenshot-dir", str(screenshot_dir),
                ],
                timeout=5400,
                artifact=ui_out,
                payload_ok=lambda payload: payload.get("terminal_pass") is True,
                process_role="browser",
            ),
            lambda: self.run_step(
                scenario_id,
                "whole_site_production_gate",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "security" / "gate" / "whole_site_production_gate.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(gate_dir),
                    "--json-out", str(gate_out),
                    "--stress-requests", "400",
                    "--stress-concurrency", "16",
                ],
                timeout=3 * 60 * 60,
                artifact=gate_out,
                payload_ok=lambda payload: bool(
                    _mapping := payload.get("WHOLE_SITE_PRODUCTION_GATE_SUMMARY")
                ) and isinstance(_mapping, Mapping)
                and _mapping.get("production_readiness") == "YES",
                process_role="browser",
                env={"WHOLE_SITE_ROOT_PASSWORD": self.credentials.root},
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "final_db_log_chain_finance_pointschain_invariants",
                invariants_out,
                self._final_ui_invariants,
                payload_ok=lambda payload: bool(
                    payload.get("root_login_succeeded") is True
                    and (payload.get("readiness") or {}).get("ok") is True
                    and (payload.get("points_verify_job") or {}).get("terminal_status") == "succeeded"
                    and (payload.get("trading_verify_job") or {}).get("terminal_status") == "succeeded"
                    and payload.get("sqlite_quick_checks")
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "effective_load_context",
                load_out,
                self._formal_load_context,
                payload_ok=lambda payload: bool(
                    payload.get("campaign_active") is True
                    and payload.get("core_load_process_alive") is True
                    and payload.get("resource_monitor_alive") is True
                    and payload.get("latest_target_sample_at_load") is True
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "process_cleanup",
                process_out,
                process_cleanup,
                payload_ok=lambda payload: isinstance(payload.get("new_descendant_pids"), list)
                and not payload.get("new_descendant_pids"),
            ),
        ])
        ui_payload = load_json(ui_out)
        artifacts = list(result.get("artifacts") or [])
        for index, screenshot in enumerate(ui_payload.get("screenshots") or []):
            if not isinstance(screenshot, Mapping):
                continue
            screenshot_path = Path(str(screenshot.get("path") or "")).expanduser().resolve(strict=False)
            artifacts.append({
                "artifact_id": f"native.source.{scenario_id}.screenshot_{index:03d}",
                "path": str(screenshot_path),
                "artifact_type": "image",
            })
        result["artifacts"] = artifacts
        selected = final_ui_assertions(
            ui_payload,
            load_json(gate_out),
            load_json(invariants_out),
            load_json(load_out),
            load_json(process_out),
        )
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    def native_cloud_drive_share_stream(self) -> dict[str, Any]:
        """Run a real cloud upload, HLS/share/proxy/UI/revoke lifecycle."""

        scenario_id = "cloud_drive_share_stream"
        out_dir = self.reports / "scenarios" / scenario_id
        probe_out = out_dir / "formal_cloud_drive_stream.json"
        screenshot_dir = out_dir / "screenshots"
        result = self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "cloud_upload_hls_share_realtime_ui_revoke",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "formal_cloud_drive_stream_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-root", str(self.primary.runtime_root),
                    "--out", str(probe_out),
                    "--screenshot-dir", str(screenshot_dir),
                ],
                timeout=3600,
                artifact=probe_out,
                process_role="ffmpeg",
                env={"HACKME_CLOUD_PROBE_SHARE_PASSWORD": secrets.token_urlsafe(24)},
                payload_ok=lambda payload: bool(
                    payload.get("schema_version") == "hackme.formal-cloud-drive-stream-probe/v1"
                    and payload.get("ok") is True
                    and not payload.get("errors")
                    and (payload.get("hls_worker") or {}).get("returncode") == 0
                    and (payload.get("stream") or {}).get("status") == "ready"
                    and (payload.get("cleanup") or {}).get("owner_preview_after_purge_status") == 404
                ),
            ),
        ])
        payload = load_json(probe_out)
        artifacts = list(result.get("artifacts") or [])
        fixture = payload.get("fixture") if isinstance(payload.get("fixture"), Mapping) else {}
        fixture_path = Path(str(fixture.get("path") or "")).expanduser().resolve(strict=False)
        if fixture_path.is_file() and not fixture_path.is_symlink():
            artifacts.append({
                "artifact_id": "native.source.cloud_drive_share_stream.fixture_video",
                "path": str(fixture_path.resolve(strict=True)),
                "artifact_type": "video",
            })
        browser = payload.get("browser") if isinstance(payload.get("browser"), Mapping) else {}
        for index, row in enumerate(browser.get("rows") or []):
            if not isinstance(row, Mapping):
                continue
            screenshot = Path(str(row.get("screenshot") or "")).expanduser().resolve(strict=False)
            if screenshot.is_file() and not screenshot.is_symlink():
                artifacts.append({
                    "artifact_id": f"native.source.cloud_drive_share_stream.screenshot_{index:02d}",
                    "path": str(screenshot.resolve(strict=True)),
                    "artifact_type": "image",
                })
        result["artifacts"] = artifacts
        selected = cloud_drive_stream_assertions(payload)
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    def native_community_governance_operations(self) -> dict[str, Any]:
        """Run exact forum/chat/friend/moderation/governance lifecycle proof."""

        scenario_id = "community_governance_operations"
        out_dir = self.reports / "scenarios" / scenario_id
        probe_out = out_dir / "formal_community_governance.json"
        screenshot_dir = out_dir / "screenshots"
        if len(self.accounts) < 2:
            return {
                "ok": False,
                "classification": "FAIL_HARNESS",
                "error": "community_governance_requires_two_campaign_accounts",
                "scenario_id": scenario_id,
            }
        user_one, user_two = self.accounts[:2]
        result = self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "forum_chat_friend_governance_ui_lifecycle",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "formal_community_governance_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--root-password", self.credentials.root,
                    "--manager-username", "admin",
                    "--manager-password", self.credentials.manager,
                    "--user-one", user_one[0],
                    "--user-one-password", user_one[1],
                    "--user-two", user_two[0],
                    "--user-two-password", user_two[1],
                    "--out", str(probe_out),
                    "--screenshot-dir", str(screenshot_dir),
                ],
                timeout=1800,
                artifact=probe_out,
                process_role="browser",
                payload_ok=lambda payload: bool(
                    payload.get("schema_version") == "hackme.formal-community-governance-probe/v1"
                    and payload.get("ok") is True
                    and not payload.get("errors")
                    and (payload.get("forum") or {}).get("terminal_report", {}).get("status") == "rejected"
                    and (payload.get("governance") or {}).get("terminal_proposal", {}).get("status") == "executed"
                    and (payload.get("boundaries") or {}).get("chat_rate_limit", {}).get("terminal", {}).get("status") == 429
                    and (payload.get("cleanup") or {}).get("settings_restored") is True
                    and (payload.get("cleanup") or {}).get("notifications_dismissed") is True
                ),
            ),
        ])
        payload = load_json(probe_out)
        artifacts = list(result.get("artifacts") or [])
        browser = payload.get("browser") if isinstance(payload.get("browser"), Mapping) else {}
        for index, row in enumerate(browser.get("rows") or []):
            if not isinstance(row, Mapping):
                continue
            screenshot = Path(str(row.get("screenshot") or "")).expanduser().resolve(strict=False)
            if screenshot.is_file() and not screenshot.is_symlink():
                artifacts.append({
                    "artifact_id": f"native.source.{scenario_id}.screenshot_{index:02d}",
                    "path": str(screenshot.resolve(strict=True)),
                    "artifact_type": "image",
                })
        result["artifacts"] = artifacts
        selected = community_governance_assertions(payload)
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    def native_ai_agent_positive_operations(self) -> dict[str, Any]:
        """Run exact AI write operations and consume one supervised restart."""

        scenario_id = "ai_agent_positive_operations"
        out_dir = self.reports / "scenarios" / scenario_id
        probe_out = out_dir / "formal_ai_agent_positive_operations.json"
        restart_out = out_dir / "supervised_restart.json"
        artifact_dir = out_dir / "artifacts"
        request_file = self.recovery.restart_request_file
        if len(self.accounts) < 2:
            return {
                "ok": False,
                "classification": "FAIL_HARNESS",
                "error": "ai_agent_positive_operations_requires_two_campaign_accounts",
                "scenario_id": scenario_id,
            }
        user_one, user_two = self.accounts[:2]

        def consume_restart_request() -> dict[str, Any]:
            before_pid = self.recovery.pid()
            if before_pid <= 1 or not Path(f"/proc/{before_pid}").exists():
                raise RuntimeError("recovery_master_missing_before_ai_restart")
            if not request_file.is_file() or request_file.is_symlink():
                raise RuntimeError("supervised_restart_request_missing_or_symlink")
            request_root = self.recovery.restart_request_root
            request_mode = stat.S_IMODE(request_file.stat().st_mode)
            root_mode = stat.S_IMODE(request_root.stat().st_mode)
            receipt = json.loads(request_file.read_text(encoding="utf-8"))
            probe = load_json(probe_out)
            probe_request = probe.get("restart_request") if isinstance(probe.get("restart_request"), Mapping) else {}
            receipt_nonce = str(receipt.get("nonce") or "")
            probe_nonce = str(probe_request.get("request_nonce") or "")
            requesting_pid = int(receipt.get("requesting_pid") or 0)
            rows = proc_rows()
            old_tree = descendants(rows, before_pid)
            old_identities: dict[int, int] = {}
            for pid in old_tree:
                try:
                    old_identities[pid] = process_start_ticks(pid)
                except Exception:
                    old_identities[pid] = 0
            requesting_pid_in_old_tree = requesting_pid in old_tree
            requesting_pid_runtime_owned = bool(
                requesting_pid_in_old_tree
                and self.recovery._pid_matches_runtime(requesting_pid)
            )
            receipt_valid = bool(
                receipt.get("schema_version") == "hackme.supervised-restart-request/v1"
                and receipt_nonce
                and receipt_nonce == probe_nonce
                and requesting_pid_runtime_owned
                and request_mode == 0o600
                and root_mode & 0o077 == 0
            )
            if not receipt_valid:
                raise RuntimeError(
                    "supervised_restart_receipt_invalid:"
                    f"schema={receipt.get('schema_version')},nonce={bool(receipt_nonce)},"
                    f"pid_in_tree={requesting_pid_in_old_tree},runtime={requesting_pid_runtime_owned},"
                    f"file_mode={oct(request_mode)},root_mode={oct(root_mode)}"
                )
            request_file.unlink()
            directory_fd = os.open(request_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            if request_file.exists() or request_file.is_symlink():
                raise RuntimeError("supervised_restart_request_unlink_failed")

            samples: list[dict[str, Any]] = []
            poll_stop = threading.Event()

            def observe() -> None:
                while not poll_stop.is_set():
                    started = time.monotonic()
                    status = 0
                    error = ""
                    try:
                        response = requests.get(
                            f"{self.recovery.base_url}/api/version",
                            verify=False,
                            timeout=0.5,
                        )
                        status = int(response.status_code)
                    except Exception as exc:
                        error = exc.__class__.__name__
                    samples.append({
                        "monotonic": time.monotonic(),
                        "status": status,
                        "error": error,
                        "latency_seconds": round(time.monotonic() - started, 4),
                    })
                    poll_stop.wait(0.05)

            observer = threading.Thread(
                target=observe,
                daemon=True,
                name="campaign-ai-supervised-restart-observer",
            )
            observer.start()
            initial_deadline = time.monotonic() + 3
            while time.monotonic() < initial_deadline and not any(row.get("status") == 200 for row in samples):
                time.sleep(0.05)
            restart = self.recovery.restart(reason="ai_agent_supervised_restart_request")
            if restart.get("ok"):
                time.sleep(0.25)
            poll_stop.set()
            observer.join(timeout=3)
            after_pid = self.recovery.pid()
            readiness = self.recovery.wait_ready(timeout=120) if restart.get("ok") else {"ok": False}
            old_tree_gone = True
            for pid, start_ticks in old_identities.items():
                try:
                    if Path(f"/proc/{pid}").exists() and process_start_ticks(pid) == start_ticks:
                        old_tree_gone = False
                        break
                except Exception:
                    continue
            unavailable = [row for row in samples if int(row.get("status") or 0) != 200]
            ready_samples = [row for row in samples if int(row.get("status") or 0) == 200]
            return {
                "schema_version": "hackme.formal-ai-agent-supervised-restart/v1",
                "receipt_valid": receipt_valid,
                "receipt_nonce_matches_probe": receipt_nonce == probe_nonce,
                "requesting_pid": requesting_pid,
                "requesting_pid_in_old_tree": requesting_pid_in_old_tree,
                "requesting_pid_runtime_owned": requesting_pid_runtime_owned,
                "request_file_mode": oct(request_mode),
                "request_root_mode": oct(root_mode),
                "restart_request_removed": not request_file.exists() and not request_file.is_symlink(),
                "before_pid": before_pid,
                "after_pid": after_pid,
                "old_tree_size": len(old_tree),
                "old_tree_gone": old_tree_gone,
                "outage_observed": bool(unavailable),
                "outage_sample_count": len(unavailable),
                "ready_sample_count": len(ready_samples),
                "sample_count": len(samples),
                "maximum_probe_latency_seconds": max(
                    (float(row.get("latency_seconds") or 0) for row in samples),
                    default=0.0,
                ),
                "restart": restart,
                "post_restart_readiness": readiness,
                "post_restart_ready": bool(
                    restart.get("ok")
                    and readiness.get("ok")
                    and after_pid > 1
                    and after_pid != before_pid
                ),
            }

        result = self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "ai_write_operations_and_restart_request",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "formal_ai_agent_positive_operations_probe.py"),
                    "--base-url", self.recovery.base_url,
                    "--runtime-root", str(self.recovery.runtime_root),
                    "--manager-username", "admin",
                    "--user-one", user_one[0],
                    "--user-two", user_two[0],
                    "--restart-request-file", str(request_file),
                    "--artifact-dir", str(artifact_dir),
                    "--out", str(probe_out),
                ],
                timeout=3600,
                artifact=probe_out,
                process_role="ffmpeg",
                env={
                    "HACKME_PROBE_ROOT_PASSWORD": self.credentials.root,
                    "HACKME_PROBE_MANAGER_PASSWORD": self.credentials.manager,
                    "HACKME_PROBE_USER_ONE_PASSWORD": user_one[1],
                    "HACKME_PROBE_USER_TWO_PASSWORD": user_two[1],
                },
                payload_ok=lambda payload: bool(
                    payload.get("schema_version") == "hackme.formal-ai-agent-positive-operations-probe/v1"
                    and payload.get("ok") is True
                    and not payload.get("errors")
                    and (payload.get("restart_request") or {}).get("mode") == "supervised-request"
                    and (payload.get("cleanup") or {}).get("settings_restored") is True
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "consume_supervised_restart_and_verify_readiness",
                restart_out,
                consume_restart_request,
                payload_ok=lambda payload: bool(
                    payload.get("schema_version") == "hackme.formal-ai-agent-supervised-restart/v1"
                    and payload.get("receipt_valid") is True
                    and payload.get("requesting_pid_in_old_tree") is True
                    and payload.get("old_tree_gone") is True
                    and payload.get("outage_observed") is True
                    and payload.get("post_restart_ready") is True
                    and payload.get("restart_request_removed") is True
                ),
            ),
        ])
        payload = load_json(probe_out)
        restart_payload = load_json(restart_out)
        artifacts = list(result.get("artifacts") or [])
        fixture = payload.get("video") if isinstance(payload.get("video"), Mapping) else {}
        fixture = fixture.get("fixture") if isinstance(fixture.get("fixture"), Mapping) else {}
        fixture_path = Path(str(fixture.get("path") or "")).expanduser().resolve(strict=False)
        if fixture_path.is_file() and not fixture_path.is_symlink():
            artifacts.append({
                "artifact_id": "native.source.ai_agent_positive_operations.video_fixture",
                "path": str(fixture_path.resolve(strict=True)),
                "artifact_type": "video",
            })
        result["artifacts"] = artifacts
        selected = ai_agent_positive_assertions(payload, restart_payload)
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    def native_comfyui_real_workflows(self) -> dict[str, Any]:
        """Run the strict real-backend, official/custom workflow lifecycle."""

        scenario_id = "comfyui_real_workflows"
        out_dir = self.reports / "scenarios" / scenario_id
        probe_dir = out_dir / "probe"
        probe_out = probe_dir / "formal_comfyui_workflows_probe.json"
        artifact_index_out = probe_dir / "artifact_index.json"
        comfyui_url = str(
            os.environ.get("HACKME_CAMPAIGN_COMFYUI_API_URL") or ""
        ).strip()
        if not comfyui_url:
            return {
                "schema_version": NATIVE_RUNNER_RESULT_SCHEMA_VERSION,
                "scenario_id": scenario_id,
                "ok": False,
                "classification": "FAIL_HARNESS",
                "error": "campaign_comfyui_api_url_missing",
                "artifacts": [],
            }

        result = self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "real_backend_feature_official_custom_agent_ui_offline_cleanup",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "formal_comfyui_workflows_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--out-dir", str(probe_dir),
                    "--feature-timeout", "900",
                    "--official-timeout", "1200",
                    "--insecure",
                ],
                timeout=16 * 60 * 60,
                artifact=probe_out,
                process_role="comfyui",
                env={
                    "HACKME_CAMPAIGN_COMFYUI_API_URL": comfyui_url,
                    "HACKME_PROBE_ROOT_PASSWORD": self.credentials.root,
                },
                payload_ok=lambda payload: bool(
                    payload.get("schema_version")
                    == "hackme.formal-comfyui-workflows-probe/v1"
                    and payload.get("ok") is True
                    and isinstance(payload.get("errors"), list)
                    and not payload.get("errors")
                    and isinstance(payload.get("contract"), Mapping)
                    and set(payload.get("contract") or {}) == {
                        "real_backend_required",
                        "feature_probe",
                        "official_templates_execute",
                        "custom_workflow_create_import_run_output_delete",
                        "ai_agent_generation_terminal_output",
                        "desktop_mobile_workflow_ui",
                        "offline_and_dependency_failure_visible",
                    }
                    and all(
                        value is True
                        for value in (payload.get("contract") or {}).values()
                    )
                    and isinstance(payload.get("cleanup"), Mapping)
                    and (payload.get("cleanup") or {}).get("exact") is True
                    and artifact_index_out.is_file()
                ),
            ),
        ])
        if not probe_out.is_file() or not artifact_index_out.is_file():
            return result

        payload = load_json(probe_out)
        artifact_index = load_json(artifact_index_out)
        artifacts = list(result.get("artifacts") or [])
        artifacts.append({
            "artifact_id": "native.source.comfyui_real_workflows.artifact_index",
            "path": str(artifact_index_out.resolve(strict=True)),
            "artifact_type": "json",
        })
        probe_root = probe_dir.resolve(strict=False)
        report_resolved = probe_out.resolve(strict=False)
        seen_paths = {
            Path(str(row.get("path") or "")).resolve(strict=False)
            for row in artifacts
            if isinstance(row, Mapping) and str(row.get("path") or "")
        }
        for index, row in enumerate(artifact_index.get("artifacts") or []):
            if not isinstance(row, Mapping):
                continue
            path = Path(str(row.get("path") or "")).expanduser().resolve(strict=False)
            if (
                path == report_resolved
                or path in seen_paths
                or path == probe_root
                or probe_root not in path.parents
                or not path.is_file()
                or path.is_symlink()
            ):
                continue
            seen_paths.add(path)
            artifacts.append({
                "artifact_id": f"native.source.comfyui_real_workflows.indexed_{index:03d}",
                "path": str(path.resolve(strict=True)),
                "artifact_type": "auto",
            })
        result["artifacts"] = artifacts
        selected = comfyui_workflow_assertions(payload, artifact_index)
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    def native_trading_background_custom_workflow(self) -> dict[str, Any]:
        """Run exact lending, background-bot, race, and workflow lifecycle proof."""

        scenario_id = "trading_background_custom_workflow"
        out_dir = self.reports / "scenarios" / scenario_id
        background_dir = out_dir / "background"
        background_out = background_dir / "trading_background_correctness.json"
        cancel_out = out_dir / "cancel_race.json"
        custom_out = out_dir / "custom_workflow_lifecycle.json"
        restart_out = out_dir / "custom_workflow_restart_persistence.json"
        cleanup_out = out_dir / "trading_fixture_cleanup.json"
        final_out = out_dir / "post_cleanup_invariants.json"

        def background_user(role: str) -> str:
            payload = load_json(background_out)
            scenario = payload.get("scenario") if isinstance(payload.get("scenario"), Mapping) else {}
            users = scenario.get("users") if isinstance(scenario.get("users"), Mapping) else {}
            row = users.get(role) if isinstance(users.get(role), Mapping) else {}
            return str(row.get("username") or "")

        def overview_locked(response: Mapping[str, Any]) -> int:
            body = response.get("body") if isinstance(response.get("body"), Mapping) else {}
            overview = body.get("overview") if isinstance(body.get("overview"), Mapping) else {}
            return int(overview.get("locked_points") or 0)

        def cancel_race() -> dict[str, Any]:
            username = background_user("limit")
            member = WebClient(self.primary.base_url, username, self.credentials.member, timeout=90)
            login = member.login()
            before = member.request("GET", "/api/trading/asset-overview") if login.get("ok") else {"status": 0}
            created = member.request(
                "POST",
                "/api/trading/orders",
                json_body={
                    "market_symbol": "ETH/POINTS",
                    "side": "buy",
                    "order_type": "limit",
                    "quantity": "1",
                    "limit_price_points": 1,
                },
            ) if login.get("ok") else {"status": 0, "body": {}}
            order = (created.get("body") or {}).get("order") if isinstance(created.get("body"), Mapping) else {}
            order = order if isinstance(order, Mapping) else {}
            order_uuid = str(order.get("order_uuid") or "")
            after_create = member.request("GET", "/api/trading/asset-overview") if order_uuid else {"status": 0}

            clients = [
                WebClient(self.primary.base_url, username, self.credentials.member, timeout=90)
                for _ in range(2)
            ]
            client_logins = [client.login() for client in clients]
            barrier = threading.Barrier(2)
            rows: list[dict[str, Any]] = []
            rows_lock = threading.Lock()

            def worker(index: int) -> None:
                try:
                    barrier.wait(timeout=30)
                    response = clients[index].request(
                        "POST",
                        f"/api/trading/orders/{quote(order_uuid, safe='')}/cancel",
                        json_body={},
                        retry_login=False,
                    )
                except Exception as exc:
                    response = {
                        "ok": False,
                        "status": 0,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                with rows_lock:
                    rows.append({"worker": index, **response})

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)
            alive = [index for index, thread in enumerate(threads) if thread.is_alive()]
            dashboard = member.request("GET", "/api/trading/dashboard")
            dashboard_body = dashboard.get("body") if isinstance(dashboard.get("body"), Mapping) else {}
            trading = dashboard_body.get("trading") if isinstance(dashboard_body.get("trading"), Mapping) else {}
            final_order = next(
                (
                    dict(row) for row in (trading.get("orders") or [])
                    if isinstance(row, Mapping) and str(row.get("order_uuid") or "") == order_uuid
                ),
                {},
            )
            after_cancel = member.request("GET", "/api/trading/asset-overview")
            before_locked = overview_locked(before)
            created_locked = overview_locked(after_create)
            cancelled_locked = overview_locked(after_cancel)
            return {
                "username": username,
                "login_status": int(login.get("status") or 0),
                "client_login_statuses": [int(row.get("status") or 0) for row in client_logins],
                "order_create": created,
                "order_uuid": order_uuid,
                "order_initial_status": str(order.get("status") or ""),
                "cancel_results": sorted(rows, key=lambda row: int(row.get("worker") or 0)),
                "worker_threads_alive": alive,
                "final_order": final_order,
                "locked_points_before": before_locked,
                "locked_points_after_create": created_locked,
                "locked_points_after_cancel": cancelled_locked,
                "locked_points_increased": created_locked > before_locked,
                "locked_points_restored_exactly": cancelled_locked == before_locked,
            }

        def workflow_graph(*, percent: int, name: str) -> dict[str, Any]:
            return {
                "version": 2,
                "strategy_kind": "workflow_graph",
                "source": "workflow_editor",
                "name": name,
                "start_node_id": "start",
                "nodes": [
                    {"id": "start", "type": "start", "label": "Start", "x": 0, "y": 0},
                    {
                        "id": "buy_once",
                        "type": "action",
                        "label": f"Buy {percent}% once",
                        "x": 240,
                        "y": 0,
                        "priority": 10,
                        "action": {
                            "type": "buy_percent",
                            "percent": percent,
                            "step": 1,
                            "order_type": "market",
                        },
                    },
                ],
                "edges": [
                    {
                        "id": "start_to_buy",
                        "from": "start",
                        "from_port": "out",
                        "to": "buy_once",
                        "to_port": "in",
                    },
                ],
            }

        def custom_workflow() -> dict[str, Any]:
            username = background_user("workflow_bot")
            suffix = re.sub(r"[^a-z0-9_-]+", "_", self.campaign_uuid.lower()).strip("_")[-32:]
            template_id = f"formal_trade_{suffix}"
            initial_label = f"Formal workflow {suffix} v1"
            edited_label = f"Formal workflow {suffix} v2"
            custom_root = (self.primary.runtime_root / "workflows" / "custom").resolve(strict=False)
            matching_before = sorted(custom_root.glob(f"**/{template_id}.json")) if custom_root.exists() else []
            member = WebClient(self.primary.base_url, username, self.credentials.member, timeout=180)
            login = member.login()
            initial_workflow = workflow_graph(percent=1, name=initial_label)
            edited_workflow = workflow_graph(percent=2, name=edited_label)
            initial_save = member.request(
                "POST",
                "/api/trading/workflow-templates/custom",
                json_body={
                    "id": template_id,
                    "label": initial_label,
                    "description": "formal campaign initial custom workflow",
                    "workflow_json": initial_workflow,
                },
            ) if login.get("ok") else {"status": 0, "body": {}}
            edited_save = member.request(
                "POST",
                "/api/trading/workflow-templates/custom",
                json_body={
                    "id": template_id,
                    "label": edited_label,
                    "description": "formal campaign edited custom workflow",
                    "workflow_json": edited_workflow,
                },
            )
            listing = member.request("GET", "/api/trading/workflow-templates")
            listing_body = listing.get("body") if isinstance(listing.get("body"), Mapping) else {}
            edited_template = next(
                (
                    dict(row) for row in (listing_body.get("custom") or [])
                    if isinstance(row, Mapping) and str(row.get("id") or "") == template_id
                ),
                {},
            )
            backtest = member.request(
                "POST",
                "/api/trading/workflow-editor/backtest",
                json_body={
                    "market_symbol": "BTC/USDT",
                    "timeframe": "15m",
                    "candle_limit": 50,
                    "initial_cash_points": 10000,
                    "workflow_json": edited_workflow,
                },
            )
            bot_payload = {
                "bot_type": "conditional",
                "name": f"Formal custom workflow bot {suffix}",
                "market_symbol": "ETH/POINTS",
                "side": "buy",
                "order_type": "market",
                "quantity": "0.00000001",
                "trigger_type": "always",
                "enabled": False,
                "max_runs": 1,
                "cooldown_seconds": 0,
                "workflow_json": edited_workflow,
            }
            bot_create = member.request("POST", "/api/trading/bots", json_body=bot_payload)
            bot_body = bot_create.get("body") if isinstance(bot_create.get("body"), Mapping) else {}
            created_bot = bot_body.get("bot") if isinstance(bot_body.get("bot"), Mapping) else {}
            bot_uuid = str(created_bot.get("bot_uuid") or "")
            bot_enable = member.request(
                "PUT",
                f"/api/trading/bots/{quote(bot_uuid, safe='')}",
                json_body={**bot_payload, "enabled": True},
            ) if bot_uuid else {"status": 0, "body": {}}
            scan = member.request("POST", "/api/trading/bots/scan", json_body={"limit": 50})
            scan_body = scan.get("body") if isinstance(scan.get("body"), Mapping) else {}
            scan_trigger = next(
                (
                    dict(row) for row in (scan_body.get("triggered") or [])
                    if isinstance(row, Mapping) and str(row.get("bot_uuid") or "") == bot_uuid
                ),
                {},
            )
            order_uuid = str(scan_trigger.get("order_uuid") or "")
            bots_after = member.request("GET", "/api/trading/bots")
            dashboard = member.request("GET", "/api/trading/dashboard")
            dashboard_body = dashboard.get("body") if isinstance(dashboard.get("body"), Mapping) else {}
            trading = dashboard_body.get("trading") if isinstance(dashboard_body.get("trading"), Mapping) else {}
            trade_order = next(
                (
                    dict(row) for row in (trading.get("orders") or [])
                    if isinstance(row, Mapping) and str(row.get("order_uuid") or "") == order_uuid
                ),
                {},
            )
            matching_after = sorted(custom_root.glob(f"**/{template_id}.json")) if custom_root.exists() else []
            safe_matches = [
                path.resolve(strict=True)
                for path in matching_after
                if path.is_file()
                and not path.is_symlink()
                and path.resolve(strict=True).is_relative_to(custom_root)
            ]
            template_file = safe_matches[0] if len(safe_matches) == 1 else None
            return {
                "username": username,
                "login_status": int(login.get("status") or 0),
                "template_id": template_id,
                "initial_label": initial_label,
                "edited_label": edited_label,
                "template_absent_before": not matching_before,
                "initial_save": initial_save,
                "edited_save": edited_save,
                "template_listing": listing,
                "edited_template_visible": bool(
                    edited_template.get("label") == edited_label
                    and _mapping_workflow.get("name") == edited_label
                    if isinstance((_mapping_workflow := edited_template.get("workflow")), Mapping)
                    else False
                ),
                "backtest": backtest,
                "bot_create": bot_create,
                "bot_enable": bot_enable,
                "scan": scan,
                "scan_trigger": scan_trigger,
                "bot_uuid": bot_uuid,
                "bots_after": bots_after,
                "trade_order": trade_order,
                "template_file": str(template_file) if template_file is not None else "",
                "template_file_sha256": self._sha256(template_file) if template_file is not None else "",
                "template_file_unique": len(safe_matches) == 1,
            }

        def restart_persistence() -> dict[str, Any]:
            custom = load_json(custom_out)
            old_pid = self.primary.pid()
            restarted = self.primary.restart(reason="formal_trading_workflow_persistence")
            stopped = restarted.get("stopped") if isinstance(restarted.get("stopped"), Mapping) else {}
            started = restarted.get("started") if isinstance(restarted.get("started"), Mapping) else {}
            readiness = started.get("ready") if isinstance(started.get("ready"), Mapping) else {}
            username = str(custom.get("username") or "")
            member = WebClient(self.primary.base_url, username, self.credentials.member, timeout=180)
            login = member.login() if started.get("ok") else {"status": 0}
            templates = member.request("GET", "/api/trading/workflow-templates") if login.get("ok") else {"body": {}}
            bots = member.request("GET", "/api/trading/bots") if login.get("ok") else {"body": {}}
            dashboard = member.request("GET", "/api/trading/dashboard") if login.get("ok") else {"body": {}}
            template_rows = ((templates.get("body") or {}).get("custom") or []) if isinstance(templates.get("body"), Mapping) else []
            bot_rows = ((bots.get("body") or {}).get("bots") or []) if isinstance(bots.get("body"), Mapping) else []
            dashboard_trading = ((dashboard.get("body") or {}).get("trading") or {}) if isinstance(dashboard.get("body"), Mapping) else {}
            order_rows = dashboard_trading.get("orders") or [] if isinstance(dashboard_trading, Mapping) else []
            template_file = Path(str(custom.get("template_file") or ""))
            template_hash = self._sha256(template_file) if template_file.is_file() and not template_file.is_symlink() else ""
            order_uuid = str((custom.get("scan_trigger") or {}).get("order_uuid") or "") if isinstance(custom.get("scan_trigger"), Mapping) else ""
            return {
                "old_pid": old_pid,
                "new_pid": int(started.get("pid") or 0),
                "old_master_remaining": stopped.get("master_process_remaining"),
                "old_process_group_remaining": stopped.get("process_group_remaining"),
                "readiness": readiness,
                "login_status": int(login.get("status") or 0),
                "template_found": any(
                    isinstance(row, Mapping) and str(row.get("id") or "") == custom.get("template_id")
                    for row in template_rows
                ),
                "bot_found": any(
                    isinstance(row, Mapping) and str(row.get("bot_uuid") or "") == custom.get("bot_uuid")
                    and int(row.get("run_count") or 0) >= 1
                    for row in bot_rows
                ),
                "trade_order_found": any(
                    isinstance(row, Mapping) and str(row.get("order_uuid") or "") == order_uuid
                    and row.get("status") == "filled"
                    for row in order_rows
                ),
                "template_file_hash_after_restart": template_hash,
                "template_file_hash_preserved": bool(
                    template_hash and template_hash == custom.get("template_file_sha256")
                ),
            }

        def cleanup() -> dict[str, Any]:
            background = load_json(background_out)
            custom = load_json(custom_out)
            scenario = background.get("scenario") if isinstance(background.get("scenario"), Mapping) else {}
            users = scenario.get("users") if isinstance(scenario.get("users"), Mapping) else {}
            usernames = [
                str(row.get("username") or "")
                for row in users.values()
                if isinstance(row, Mapping)
            ]
            member = WebClient(
                self.primary.base_url,
                str(custom.get("username") or ""),
                self.credentials.member,
                timeout=120,
            )
            login = member.login()
            bot_uuid = str(custom.get("bot_uuid") or "")
            deleted = member.request(
                "DELETE", f"/api/trading/bots/{quote(bot_uuid, safe='')}"
            ) if login.get("ok") and bot_uuid else {"status": 0}
            listed_bots = member.request("GET", "/api/trading/bots") if login.get("ok") else {"body": {}}
            bot_rows = ((listed_bots.get("body") or {}).get("bots") or []) if isinstance(listed_bots.get("body"), Mapping) else []
            bot_absent = bool(bot_uuid) and not any(
                isinstance(row, Mapping) and str(row.get("bot_uuid") or "") == bot_uuid
                for row in bot_rows
            )

            custom_root = (self.primary.runtime_root / "workflows" / "custom").resolve(strict=False)
            template_file = Path(str(custom.get("template_file") or ""))
            template_parent: Path | None = None
            path_safe = False
            if str(template_file) not in {"", "."}:
                resolved = template_file.resolve(strict=False)
                path_safe = resolved.is_relative_to(custom_root) and resolved.name == f"{custom.get('template_id')}.json"
                if path_safe:
                    template_parent = resolved.parent
                    resolved.unlink(missing_ok=True)
            templates_after = member.request("GET", "/api/trading/workflow-templates") if login.get("ok") else {"body": {}}
            custom_rows = ((templates_after.get("body") or {}).get("custom") or []) if isinstance(templates_after.get("body"), Mapping) else []
            template_absent = not any(
                isinstance(row, Mapping) and str(row.get("id") or "") == custom.get("template_id")
                for row in custom_rows
            )
            if template_parent is not None and template_parent.is_dir():
                try:
                    template_parent.rmdir()
                except OSError:
                    pass
            accounts = self._cleanup_exact_scenario_users(usernames)
            return {
                "member_login_status": int(login.get("status") or 0),
                "bot_delete_status": int(deleted.get("status") or 0),
                "bot_deleted": int(deleted.get("status") or 0) == 200,
                "bot_absent": bot_absent,
                "template_path_safe": path_safe,
                "template_file_removed": path_safe and not template_file.exists(),
                "template_absent_from_api": template_absent,
                "custom_user_directory_absent": bool(
                    template_parent is not None and not template_parent.exists()
                ),
                "account_login_succeeded": accounts.get("login_succeeded"),
                "account_records": accounts.get("records") or [],
            }

        def final_state() -> dict[str, Any]:
            root = WebClient(self.primary.base_url, "root", self.credentials.root, timeout=180)
            login = root.login()
            if not login.get("ok"):
                return {"root_login_succeeded": False}
            trading_started = root.request("POST", "/api/root/trading/verify/jobs", json_body={})
            trading_job = self._await_management_job(root, trading_started, timeout_seconds=900)
            points_started = root.request("POST", "/api/root/points/chain/verify/jobs", json_body={})
            points_job = self._await_management_job(root, points_started, timeout_seconds=900)
            report = root.request("GET", "/api/admin/trading/report")
            report_body = report.get("body") if isinstance(report.get("body"), Mapping) else {}
            report_payload = report_body.get("report") if isinstance(report_body.get("report"), Mapping) else {}
            reserve = report_payload.get("reserve_pool") if isinstance(report_payload.get("reserve_pool"), Mapping) else {}
            return {
                "root_login_succeeded": True,
                "readiness": self.primary.wait_ready(timeout=180.0),
                "trading_verify_job": trading_job,
                "trading_verify_latest": root.request("GET", "/api/root/trading/verify/latest"),
                "points_verify_job": points_job,
                "points_verify_latest": root.request("GET", "/api/root/points/chain/verify/latest"),
                "reserve_balance_points": int(reserve.get("balance_points") or 0),
            }

        result = self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "background_lending_bots_and_stress",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "playwright_trading_background_correctness.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-dir", str(self.primary.runtime_root),
                    "--out", str(background_dir),
                    "--trigger-mode", "auto",
                    "--stress-orders", "150",
                ],
                timeout=5400,
                artifact=background_out,
                process_role="browser",
                payload_ok=lambda payload: bool(
                    payload.get("ok") is True
                    and (payload.get("scenario") or {}).get("runtime_settings_restored") is True
                    and (payload.get("scenario") or {}).get("feature_flags_restored") is True
                    and int(((payload.get("scenario") or {}).get("concurrent_stress") or {}).get("request_count") or 0) >= 300
                    and (payload.get("scenario") or {}).get("domain_terminal")
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "spot_cancel_race",
                cancel_out,
                cancel_race,
                payload_ok=lambda payload: bool(
                    payload.get("order_initial_status") in {"open", "partially_filled"}
                    and not payload.get("worker_threads_alive")
                    and len(payload.get("cancel_results") or []) == 2
                    and sum(1 for row in payload.get("cancel_results") or [] if int(row.get("status") or 0) == 200) == 1
                    and sum(1 for row in payload.get("cancel_results") or [] if int(row.get("status") or 0) == 400) == 1
                    and (payload.get("final_order") or {}).get("status") == "cancelled"
                    and payload.get("locked_points_restored_exactly") is True
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "custom_workflow_create_edit_backtest_enable_trade",
                custom_out,
                custom_workflow,
                payload_ok=lambda payload: bool(
                    payload.get("template_absent_before") is True
                    and payload.get("edited_template_visible") is True
                    and payload.get("template_file_unique") is True
                    and int((payload.get("backtest") or {}).get("status") or 0) == 200
                    and int(((payload.get("backtest") or {}).get("body") or {}).get("trade_count") or 0) >= 1
                    and str((payload.get("scan_trigger") or {}).get("order_uuid") or "")
                    and (payload.get("trade_order") or {}).get("status") == "filled"
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "custom_workflow_restart_persistence",
                restart_out,
                restart_persistence,
                payload_ok=lambda payload: bool(
                    int(payload.get("old_pid") or 0) > 0
                    and int(payload.get("new_pid") or 0) > 0
                    and int(payload.get("new_pid") or 0) != int(payload.get("old_pid") or 0)
                    and payload.get("old_master_remaining") is False
                    and payload.get("old_process_group_remaining") is False
                    and (payload.get("readiness") or {}).get("ok") is True
                    and payload.get("template_found") is True
                    and payload.get("bot_found") is True
                    and payload.get("trade_order_found") is True
                    and payload.get("template_file_hash_preserved") is True
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "trading_fixture_cleanup",
                cleanup_out,
                cleanup,
                payload_ok=lambda payload: bool(
                    payload.get("bot_deleted") is True
                    and payload.get("bot_absent") is True
                    and payload.get("template_path_safe") is True
                    and payload.get("template_file_removed") is True
                    and payload.get("template_absent_from_api") is True
                    and payload.get("custom_user_directory_absent") is True
                    and payload.get("account_records")
                    and all(
                        row.get("deleted") is True
                        and int(row.get("residual_exact_count") or 0) == 0
                        for row in payload.get("account_records") or []
                    )
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "post_cleanup_trading_and_pointschain_invariants",
                final_out,
                final_state,
                payload_ok=lambda payload: bool(
                    payload.get("root_login_succeeded") is True
                    and (payload.get("readiness") or {}).get("ok") is True
                    and (payload.get("trading_verify_job") or {}).get("terminal_status") == "succeeded"
                    and (payload.get("points_verify_job") or {}).get("terminal_status") == "succeeded"
                    and int(payload.get("reserve_balance_points") or 0) >= 0
                ),
            ),
        ])
        selected = trading_workflow_assertions(
            load_json(background_out),
            load_json(cancel_out),
            load_json(custom_out),
            load_json(restart_out),
            load_json(final_out),
            load_json(cleanup_out),
        )
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

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

    @staticmethod
    def _is_controlled_backpressure(result: Mapping[str, Any]) -> bool:
        body = result.get("body")
        if not isinstance(body, Mapping):
            return False
        status = int(result.get("status") or 0)
        error = str(body.get("error") or "")
        return bool(
            (status == 429 and error == "edge_rate_limited")
            or (status == 503 and error == "server_busy")
        )

    def _request_with_controlled_backpressure_retry(
        self,
        client: WebClient,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        max_attempts: int = CAMPAIGN_CONTROLLED_BACKPRESSURE_MAX_ATTEMPTS,
    ) -> dict[str, Any]:
        attempts = max(1, int(max_attempts))
        result: dict[str, Any] = {}
        for attempt in range(1, attempts + 1):
            result = client.request(
                method,
                path,
                json_body=json_body,
                params=params,
            )
            if not self._is_controlled_backpressure(result) or attempt >= attempts:
                return result
            body = result.get("body")
            raw_retry_after = body.get("retry_after_seconds") if isinstance(body, Mapping) else None
            try:
                retry_after = float(raw_retry_after)
            except (TypeError, ValueError):
                retry_after = 0.0
            delay = min(10.0, max(0.1, retry_after, 0.25 * attempt))
            self._server_progress(
                f"controlled_backpressure_retry:{method.upper()}:{path}:"
                f"attempt={attempt}:status={int(result.get('status') or 0)}"
            )
            time.sleep(delay)
        return result

    def _create_user(self, root: WebClient, username: str, password: str, *, nickname: str = "Campaign User") -> dict[str, Any]:
        result = self._request_with_controlled_backpressure_retry(
            root,
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
        search = self._request_with_controlled_backpressure_retry(
            root,
            "GET",
            "/api/admin/users",
            params={"q": username, "page_size": 100},
        )
        users = ((search.get("body") or {}).get("users") or []) if isinstance(search.get("body"), dict) else []
        exact = next((item for item in users if str(item.get("username") or "") == username), None)
        return {
            "ok": int(result.get("status") or 0) in {200, 201, 409} and exact is not None,
            "create_status": result.get("status"),
            "search_status": search.get("status"),
            "user_id": int((exact or {}).get("id") or 0),
            "username": username,
        }

    def _configure_campaign_storage_quota(
        self,
        root: WebClient,
        user_id: int,
    ) -> dict[str, Any]:
        payload = {
            "quota_mb": CAMPAIGN_STORAGE_QUOTA_MB,
            "max_file_size_mb": CAMPAIGN_STORAGE_MAX_FILE_SIZE_MB,
            "upload_rate_limit_per_day": CAMPAIGN_STORAGE_UPLOAD_RATE_LIMIT_PER_DAY,
            "can_upload": True,
            "enabled": True,
            "reason": "isolated 24h campaign HLS and high-load upload fixture",
        }
        result = self._request_with_controlled_backpressure_retry(
            root,
            "PUT",
            f"/api/root/storage/users/{int(user_id)}/quota-override",
            json_body=payload,
        )
        body = result.get("body") if isinstance(result.get("body"), Mapping) else {}
        user = body.get("user") if isinstance(body, Mapping) else {}
        user = user if isinstance(user, Mapping) else {}
        expected_quota = CAMPAIGN_STORAGE_QUOTA_MB * 1024 * 1024
        expected_max_file = CAMPAIGN_STORAGE_MAX_FILE_SIZE_MB * 1024 * 1024
        ok = bool(
            int(result.get("status") or 0) == 200
            and body.get("ok") is True
            and int(user.get("total_bytes") or 0) >= expected_quota
            and int(user.get("max_file_size_bytes") or 0) >= expected_max_file
            and user.get("can_upload") is True
            and int(user.get("upload_rate_limit_per_day") or 0)
            >= CAMPAIGN_STORAGE_UPLOAD_RATE_LIMIT_PER_DAY
        )
        return {
            "ok": ok,
            "status": int(result.get("status") or 0),
            "user_id": int(user_id),
            "quota_bytes": int(user.get("total_bytes") or 0),
            "max_file_size_bytes": int(user.get("max_file_size_bytes") or 0),
            "upload_rate_limit_per_day": int(
                user.get("upload_rate_limit_per_day") or 0
            ),
            "can_upload": user.get("can_upload") is True,
        }

    def _user_exists(self, root: WebClient, username: str) -> bool:
        search = self._request_with_controlled_backpressure_retry(
            root,
            "GET",
            "/api/admin/users",
            params={"q": username, "page_size": 100},
        )
        if int(search.get("status") or 0) != 200:
            # An inconclusive lookup must not be interpreted as proof that a
            # restore marker or incident account vanished.
            return True
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
            "snapshot_id": snapshot_id,
            "dirty_marker_created": marker.get("ok"),
            "dirty_marker_absent_after_restore": marker_absent,
            "append_only_transfer": transfer,
            "transfer_survived_restore": explorer.get("ok"),
            "restore_status": restore.get("status"),
            "protected_database_skips": protected_skips,
            "storage_restored": storage_restored,
            "storage_marker_path": str(storage_marker),
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

    @staticmethod
    def _runtime_backup_file_allowed(relative: Path) -> bool:
        volatile_parts = {"venv", "pycache", "__pycache__", "tmp", "temp"}
        if any(part in volatile_parts for part in relative.parts):
            return False
        if relative.name == "server.pid" or relative.suffix in {".sock", ".lock"}:
            return False
        return True

    def _portable_full_runtime_cycle(
        self,
        *,
        out_dir: Path,
        archive: Path,
        manifest_path: Path,
    ) -> dict[str, Any]:
        """Create, fully read, extract, hash-check, and remove a runtime archive."""

        restore_root = out_dir / "portable_restore_tree"
        storage_marker = self.recovery.runtime_root / "storage" / "formal_full_backup_marker.bin"
        payload: dict[str, Any] = {
            "archive": {},
            "extracted_restore": {},
            "restore_root_removed": False,
            "source_storage_marker_removed": False,
            "server_restarted": False,
        }
        if archive.exists() or archive.is_symlink() or manifest_path.exists():
            payload["preexisting_output_rejected"] = True
            return payload
        storage_marker.parent.mkdir(parents=True, exist_ok=True)
        storage_marker.write_bytes(secrets.token_bytes(4096))
        old_pid = self.recovery.pid()
        stopped = self.recovery.stop(reason="formal_portable_full_runtime_backup")
        payload["stop"] = {
            "old_pid": old_pid,
            "succeeded": stopped.get("ok") is True,
            "master_process_remaining": stopped.get("master_process_remaining"),
            "process_group_remaining": stopped.get("process_group_remaining"),
        }
        try:
            if not stopped.get("ok"):
                return payload
            files: list[dict[str, Any]] = []
            symlinks: list[str] = []
            for path in sorted(self.recovery.runtime_root.rglob("*")):
                relative = path.relative_to(self.recovery.runtime_root)
                if not self._runtime_backup_file_allowed(relative):
                    continue
                if path.is_symlink():
                    symlinks.append(relative.as_posix())
                    continue
                if not path.is_file():
                    continue
                files.append({
                    "path": relative.as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": self._sha256(path),
                })
            manifest = {
                "schema_version": "hackme.campaign.portable-runtime-manifest/v1",
                "created_at": utc_now(),
                "runtime_role": "recovery",
                "excluded_volatile_names": [
                    "server.pid", "*.sock", "*.lock", "venv", "pycache",
                    "__pycache__", "tmp", "temp",
                ],
                "unsupported_symlinks": symlinks,
                "files": files,
            }
            atomic_write_json(manifest_path, manifest)
            archive.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive, "w:gz") as handle:
                handle.add(manifest_path, arcname="backup_manifest.json", recursive=False)
                for row in files:
                    source = self.recovery.runtime_root / str(row["path"])
                    handle.add(source, arcname=str(row["path"]), recursive=False)

            unsafe_members: list[str] = []
            regular_members: list[tarfile.TarInfo] = []
            readable = True
            try:
                with tarfile.open(archive, "r:gz") as handle:
                    for member in handle.getmembers():
                        member_path = Path(member.name)
                        if (
                            member_path.is_absolute()
                            or ".." in member_path.parts
                            or member.issym()
                            or member.islnk()
                        ):
                            unsafe_members.append(member.name)
                        if member.isfile():
                            regular_members.append(member)
                            extracted = handle.extractfile(member)
                            if extracted is None:
                                readable = False
                            else:
                                while extracted.read(1024 * 1024):
                                    pass
            except Exception as exc:
                readable = False
                payload["archive_read_error"] = f"{exc.__class__.__name__}: {exc}"

            payload["archive"] = {
                "path": str(archive),
                "size_bytes": archive.stat().st_size if archive.exists() else 0,
                "sha256": self._sha256(archive),
                "readable": readable,
                "manifest_file_count": len(files),
                "archive_regular_file_count": len(regular_members),
                "database_file_count": sum(
                    str(row["path"]).startswith("database/") for row in files
                ),
                "storage_file_count": sum(
                    str(row["path"]).startswith("storage/") for row in files
                ),
                "unsafe_members": unsafe_members,
                "unsupported_symlinks": symlinks,
            }
            if unsafe_members or symlinks or not readable:
                return payload

            restore_root.mkdir(parents=True, exist_ok=False)
            with tarfile.open(archive, "r:gz") as handle:
                handle.extractall(restore_root)
            mismatches: list[str] = []
            missing: list[str] = []
            for row in files:
                restored = restore_root / str(row["path"])
                if not restored.is_file():
                    missing.append(str(row["path"]))
                elif self._sha256(restored) != row["sha256"]:
                    mismatches.append(str(row["path"]))
            payload["extracted_restore"] = {
                "all_manifest_files_present": not missing,
                "missing_files": missing,
                "hash_mismatches": mismatches,
                "sqlite_quick_checks": self._sqlite_checks(restore_root / "database"),
            }
            return payload
        finally:
            shutil.rmtree(restore_root, ignore_errors=True)
            payload["restore_root_removed"] = not restore_root.exists()
            try:
                storage_marker.unlink(missing_ok=True)
            except Exception:
                pass
            payload["source_storage_marker_removed"] = not storage_marker.exists()
            if self.recovery.pid() <= 0:
                started = self.recovery.start()
                ready = started.get("ready") if isinstance(started.get("ready"), Mapping) else {}
                payload["server_restarted"] = bool(
                    started.get("ok") is True and ready.get("ok") is True
                )
            else:
                payload["server_restarted"] = True

    def _run_launcher_cli(self, command: list[str], *, timeout: int = 1800) -> dict[str, Any]:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                env=self.base_env(),
                capture_output=True,
                text=False,
                timeout=timeout,
                check=False,
            )
            output = (completed.stdout or b"") + (completed.stderr or b"")
            return {
                "returncode": completed.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "output_bytes": len(output),
                "output_sha256": hashlib.sha256(output).hexdigest(),
            }
        except Exception as exc:
            return {
                "returncode": 124 if isinstance(exc, subprocess.TimeoutExpired) else 125,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
            }

    def _formal_live_restore_cycle(self, *, archive: Path) -> dict[str, Any]:
        """Exercise launcher restore while proving live storage/finance protection."""

        result: dict[str, Any] = {
            "backup_command_returncode": -1,
            "restore_command_returncode": -1,
            "archive_size_bytes": 0,
            "archive_readable": False,
            "pre_restore_runtime_removed": False,
            "storage_marker_removed": False,
        }
        if archive.exists() or archive.is_symlink():
            result["preexisting_archive_rejected"] = True
            return result
        stop_before_backup = self.recovery.stop(reason="formal_cli_runtime_backup")
        result["stop_before_backup"] = stop_before_backup
        if not stop_before_backup.get("ok"):
            return result
        backup = self._run_launcher_cli([
            str(LAUNCHER),
            "--cli",
            "--run-root", str(self.recovery.run_root),
            "--runtime-root", str(self.recovery.runtime_root),
            "--in-place",
            "--tmp-runtime",
            "--skip-install",
            "--backup", str(archive),
        ])
        result["backup_command_returncode"] = int(backup.get("returncode") or 0)
        result["backup_command"] = backup
        result["archive_size_bytes"] = archive.stat().st_size if archive.exists() else 0
        if archive.exists():
            try:
                with tarfile.open(archive, "r:gz") as handle:
                    members = [member for member in handle.getmembers() if member.isfile()]
                    readable = bool(members)
                    for member in members:
                        extracted = handle.extractfile(member)
                        if extracted is None:
                            readable = False
                            break
                        while extracted.read(1024 * 1024):
                            pass
                    result["archive_readable"] = readable
                    result["archive_regular_file_count"] = len(members)
            except Exception as exc:
                result["archive_error"] = f"{exc.__class__.__name__}: {exc}"
        started = self.recovery.start() if int(backup.get("returncode", -1)) == 0 else {}
        result["start_after_backup"] = started
        root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
        root_login = root.login() if started.get("ok") else {"ok": False}
        marker_username = f"formal_cli_restore_dirty_{int(time.time())}"
        marker = (
            self._create_user(root, marker_username, self.credentials.member, nickname="Formal CLI Restore Dirty")
            if root_login.get("ok") else {"ok": False}
        )
        transfer = (
            self._wallet_transfer_between_builtin_users(
                self.recovery.base_url,
                reference=f"formal-cli-post-backup-{int(time.time())}",
            )
            if root_login.get("ok") else {"ok": False}
        )
        storage_marker = self.recovery.runtime_root / "storage" / "formal_cli_storage_marker.bin"
        storage_marker.parent.mkdir(parents=True, exist_ok=True)
        storage_marker.write_bytes(secrets.token_bytes(4096))
        storage_hash = self._sha256(storage_marker)
        stopped = self.recovery.stop(reason="formal_cli_runtime_restore")
        result["stop_before_restore"] = stopped
        finance = self.recovery.runtime_root / "database" / "finance.db"
        protected_hash_before = self._sha256(finance)
        restore = self._run_launcher_cli([
            str(LAUNCHER),
            "--cli",
            "--run-root", str(self.recovery.run_root),
            "--runtime-root", str(self.recovery.runtime_root),
            "--in-place",
            "--tmp-runtime",
            "--skip-install",
            "--restore", str(archive),
        ]) if stopped.get("ok") else {"returncode": 125}
        result["restore_command_returncode"] = int(restore.get("returncode") or 0)
        result["restore_command"] = restore
        protected_hash_after = self._sha256(finance)
        policy_path = self.recovery.runtime_root / "logs" / "runtime_restore_policy.json"
        policy = load_json(policy_path) if policy_path.exists() else {}
        result.update({
            "dirty_marker_created": marker.get("ok") is True,
            "append_only_transfer": transfer,
            "protected_finance_hash_preserved": bool(
                protected_hash_before and protected_hash_before == protected_hash_after
            ),
            "storage_preserved": bool(
                storage_marker.is_file() and self._sha256(storage_marker) == storage_hash
            ),
            "restore_policy": policy,
            "sqlite_quick_checks": self._sqlite_checks(self.recovery.runtime_root / "database"),
        })
        after = self.recovery.start() if int(restore.get("returncode", -1)) == 0 else {}
        result["start_after_restore"] = after
        root_after = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
        login_after = root_after.login() if after.get("ok") else {"ok": False}
        result["dirty_marker_absent_after_restore"] = bool(
            login_after.get("ok") and not self._user_exists(root_after, marker_username)
        )
        tx_hash = str(transfer.get("transaction_hash") or "")
        explorer = (
            root_after.request("GET", f"/api/points/explorer/tx/{quote(tx_hash, safe='')}")
            if tx_hash and login_after.get("ok") else {"status": 0}
        )
        result["transfer_survived_restore"] = int(explorer.get("status") or 0) == 200
        pre_restore = Path(str(policy.get("pre_restore_runtime") or "")).expanduser()
        try:
            if pre_restore.exists() and pre_restore.resolve().is_relative_to(self.root.resolve()):
                shutil.rmtree(pre_restore)
        except Exception as exc:
            result["pre_restore_cleanup_error"] = f"{exc.__class__.__name__}: {exc}"
        result["pre_restore_runtime_removed"] = bool(
            str(pre_restore) not in {"", "."} and not pre_restore.exists()
        )
        try:
            storage_marker.unlink(missing_ok=True)
        except Exception:
            pass
        result["storage_marker_removed"] = not storage_marker.exists()
        if self.recovery.pid() <= 0:
            emergency = self.recovery.start()
            result["emergency_recovery_start"] = emergency
        return result

    def _formal_planned_restart(self) -> dict[str, Any]:
        old_pid = self.recovery.pid()
        restarted = self.recovery.restart(reason="formal_backup_final_restart")
        stopped = restarted.get("stopped") if isinstance(restarted.get("stopped"), Mapping) else {}
        started = restarted.get("started") if isinstance(restarted.get("started"), Mapping) else {}
        ready = started.get("ready") if isinstance(started.get("ready"), Mapping) else {}
        return {
            "stopped": {
                "old_pid": old_pid,
                "master_process_remaining": stopped.get("master_process_remaining"),
                "process_group_remaining": stopped.get("process_group_remaining"),
            },
            "started": {
                "new_pid": int(started.get("pid") or 0),
                "readiness_succeeded": ready.get("ok") is True,
            },
        }

    def native_server_emergency_incident(self) -> dict[str, Any]:
        """Enter containment, prove restrictions, repair, resolve, and restore."""

        scenario_id = "server_emergency_incident"
        out_dir = self.reports / "scenarios" / scenario_id
        enter_out = out_dir / "incident_enter_and_restrictions.json"
        diagnostics_out = out_dir / "incident_diagnostics_and_repair.json"
        restore_out = out_dir / "incident_resolve_and_mode_restore.json"
        final_out = out_dir / "post_incident_invariants.json"

        def enter_and_restrict() -> dict[str, Any]:
            root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=90)
            member = WebClient(self.recovery.base_url, "test", self.credentials.test, timeout=90)
            root_login = root.login()
            member_login = member.login()
            before = root.request("GET", "/api/root/server-mode") if root_login.get("ok") else {"body": {}}
            before_body = before.get("body") if isinstance(before.get("body"), Mapping) else {}
            before_mode = before_body.get("mode") if isinstance(before_body.get("mode"), Mapping) else {}
            entered = root.request(
                "POST",
                "/api/root/incident/enter",
                json_body={
                    "confirm": "ENTER_INCIDENT_LOCKDOWN",
                    "trigger_type": "formal_campaign_emergency_drill",
                    "reason": "formal isolated recovery-target incident response verification",
                    "verification": {"campaign_scenario": scenario_id},
                },
            ) if root_login.get("ok") else {"status": 0, "body": {}}
            root_relogin = root.login()
            status_during = (
                root.request("GET", "/api/root/incident/status")
                if root_relogin.get("ok") else {"status": 0, "body": {}}
            )
            return {
                "root_login_status": root_login.get("status"),
                "member_login_status": member_login.get("status"),
                "mode_before": str(before_mode.get("current_mode") or ""),
                "enter": entered,
                "root_relogin_status": root_relogin.get("status"),
                "status_during": status_during,
                "member_restricted_operation": member.request("GET", "/api/jobs"),
                "root_restricted_operation": root.request(
                    "POST",
                    "/api/trading/orders",
                    json_body={"market_symbol": "BTC/POINTS", "side": "buy"},
                ),
                "root_recovery_operation": root.request("GET", "/api/admin/health"),
            }

        def diagnostics_and_repair() -> dict[str, Any]:
            root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
            login = root.login()
            if not login.get("ok"):
                return {"root_login_succeeded": False}
            return {
                "root_login_succeeded": True,
                "database_before": root.request("GET", "/api/admin/health/db-integrity"),
                "audit_before": root.request("GET", "/api/admin/health/audit-chain"),
                "integrity_report": root.request("GET", "/api/root/integrity/report"),
                "integrity_repair": root.request(
                    "POST", "/api/admin/integrity/repair", json_body={}
                ),
                "database_after": root.request("GET", "/api/admin/health/db-integrity"),
                "audit_after": root.request("GET", "/api/admin/health/audit-chain"),
                "mode_log_after": root.request("GET", "/api/root/server-mode/logs/verify"),
            }

        def resolve_and_restore() -> dict[str, Any]:
            enter = load_json(enter_out)
            original_mode = str(enter.get("mode_before") or "dev_ready")
            confirm = {
                "dev_ready": "SWITCH_TO_DEV_READY",
                "preprod": "SWITCH_TO_DEV_READY",
                "internal_test": "SWITCH_TO_INTERNAL_TEST",
                "test": "SWITCH_TO_TEST",
                "maintenance": "ENTER_MAINTENANCE",
                "production": "GO_LIVE",
            }.get(original_mode, "SWITCH_TO_DEV_READY")
            root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=180)
            login = root.login()
            before = root.request("GET", "/api/root/incident/status") if login.get("ok") else {"body": {}}
            incident = (before.get("body") or {}).get("incident") or {}
            resolved = root.request(
                "POST",
                "/api/root/incident/resolve",
                json_body={
                    "confirm": "RESOLVE_INCIDENT",
                    "notes": "formal campaign diagnostics and integrity verification completed",
                    "verification": {"diagnostics_artifact": diagnostics_out.name},
                },
            ) if incident else {"status": 0, "body": {}}
            switched = root.request(
                "POST",
                "/api/root/server-mode/switch",
                json_body={
                    "mode": original_mode,
                    "confirm": confirm,
                    "reason": "restore pre-incident mode after formal emergency drill",
                },
            )
            root.login()
            return {
                "original_mode": original_mode,
                "incident_before_resolve": before,
                "resolve": resolved,
                "switch": switched,
                "mode_after": root.request("GET", "/api/root/server-mode"),
                "incident_after": root.request("GET", "/api/root/incident/status"),
                "mode_log_after": root.request("GET", "/api/root/server-mode/logs/verify"),
            }

        def final_state() -> dict[str, Any]:
            root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
            login = root.login()
            if not login.get("ok"):
                return {"root_login_succeeded": False}
            points_started = root.request("POST", "/api/root/points/chain/verify/jobs", json_body={})
            points_job = self._await_management_job(root, points_started, timeout_seconds=900)
            trading_started = root.request("POST", "/api/root/trading/verify/jobs", json_body={})
            trading_job = self._await_management_job(root, trading_started, timeout_seconds=900)
            site = root.request("GET", "/api/site-config")
            return {
                "root_login_succeeded": True,
                "readiness": self.recovery.wait_ready(timeout=180.0),
                "audit_integrity": root.request("GET", "/api/admin/health/audit-chain"),
                "database_integrity": root.request("GET", "/api/admin/health/db-integrity"),
                "mode_log_chain": root.request("GET", "/api/root/server-mode/logs/verify"),
                "points_verify_job": points_job,
                "points_verify_latest": root.request("GET", "/api/root/points/chain/verify/latest"),
                "trading_verify_job": trading_job,
                "trading_verify_latest": root.request("GET", "/api/root/trading/verify/latest"),
                "site_config": (
                    (site.get("body") or {}).get("site_config")
                    if isinstance(site.get("body"), Mapping) else {}
                ),
            }

        result = self.run_group(scenario_id, [
            lambda: self.run_native_callable_step(
                scenario_id,
                "incident_enter_and_restrictions",
                enter_out,
                enter_and_restrict,
                payload_ok=lambda payload: bool(
                    int((payload.get("enter") or {}).get("status") or 0) == 200
                    and ((payload.get("status_during") or {}).get("body") or {}).get("incident")
                    and int((payload.get("root_restricted_operation") or {}).get("status") or 0) == 503
                    and int((payload.get("root_recovery_operation") or {}).get("status") or 0) == 200
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "incident_diagnostics_and_repair",
                diagnostics_out,
                diagnostics_and_repair,
                payload_ok=lambda payload: bool(
                    payload.get("root_login_succeeded") is True
                    and int((payload.get("integrity_repair") or {}).get("status") or 0) == 200
                    and int((payload.get("database_after") or {}).get("status") or 0) == 200
                    and int((payload.get("audit_after") or {}).get("status") or 0) == 200
                    and int((payload.get("mode_log_after") or {}).get("status") or 0) == 200
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "incident_resolve_and_mode_restore",
                restore_out,
                resolve_and_restore,
                payload_ok=lambda payload: bool(
                    int((payload.get("resolve") or {}).get("status") or 0) == 200
                    and int((payload.get("switch") or {}).get("status") or 0) == 200
                    and ((payload.get("mode_after") or {}).get("body") or {}).get("mode", {}).get("current_mode")
                    == payload.get("original_mode")
                    and ((payload.get("incident_after") or {}).get("body") or {}).get("incident") is None
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "post_incident_invariants",
                final_out,
                final_state,
                payload_ok=lambda payload: bool(
                    payload.get("root_login_succeeded") is True
                    and (payload.get("readiness") or {}).get("ok") is True
                    and (payload.get("points_verify_job") or {}).get("terminal_status") == "succeeded"
                    and (payload.get("trading_verify_job") or {}).get("terminal_status") == "succeeded"
                    and (payload.get("site_config") or {}).get("maintenance_mode") is False
                ),
            ),
        ])
        selected = server_emergency_assertions(
            load_json(enter_out),
            load_json(diagnostics_out),
            load_json(restore_out),
            load_json(final_out),
        )
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

    def native_backup_restore_restart(self) -> dict[str, Any]:
        """Run exact snapshot, portable archive, live restore, and restart proof."""

        scenario_id = "backup_restore_restart"
        out_dir = self.reports / "scenarios" / scenario_id
        portable_out = out_dir / "portable_full_runtime_cycle.json"
        portable_archive = out_dir / "portable_full_runtime.tar.gz"
        portable_manifest = out_dir / "portable_full_runtime_manifest.json"
        snapshot_out = out_dir / "snapshot_restore_boundary.json"
        live_out = out_dir / "live_runtime_restore.json"
        ordinary_archive = out_dir / "ordinary_runtime_backup.tar.gz"
        restart_out = out_dir / "planned_restart.json"
        cleanup_out = out_dir / "backup_fixture_cleanup.json"
        final_out = out_dir / "post_restart_invariants.json"

        def cleanup() -> dict[str, Any]:
            snapshot = load_json(snapshot_out)
            snapshot_id = str(snapshot.get("snapshot_id") or "")
            root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
            login = root.login()
            deleted = (
                root.request(
                    "DELETE",
                    f"/api/admin/snapshots/{quote(snapshot_id, safe='')}",
                    params={"reason": "formal backup fixture cleanup"},
                )
                if login.get("ok") and snapshot_id else {"status": 0}
            )
            listed = root.request("GET", "/api/admin/snapshots") if login.get("ok") else {"body": {}}
            rows = (listed.get("body") or {}).get("snapshots") or []
            absent = bool(snapshot_id) and not any(
                str(row.get("snapshot_id") or row.get("id") or "") == snapshot_id
                for row in rows if isinstance(row, Mapping)
            )
            marker_path = Path(str(snapshot.get("storage_marker_path") or ""))
            if str(marker_path) not in {"", "."}:
                try:
                    marker_path.unlink(missing_ok=True)
                except Exception:
                    pass
            prefix = f"{self.recovery.runtime_root.name}.pre-restore-"
            unexpected = [
                str(path) for path in self.recovery.runtime_root.parent.glob(f"{prefix}*")
                if path.exists()
            ]
            return {
                "snapshot_delete_status": int(deleted.get("status") or 0),
                "snapshot_deleted": int(deleted.get("status") or 0) == 200,
                "snapshot_absent": absent,
                "snapshot_storage_marker_removed": not marker_path.exists(),
                "unexpected_pre_restore_paths": unexpected,
            }

        def final_state() -> dict[str, Any]:
            snapshot = load_json(snapshot_out)
            live = load_json(live_out)
            snapshot_hash = str(_mapping_transfer.get("transaction_hash") or "") if isinstance(
                (_mapping_transfer := snapshot.get("append_only_transfer")), Mapping
            ) else ""
            cli_hash = str(_mapping_cli.get("transaction_hash") or "") if isinstance(
                (_mapping_cli := live.get("append_only_transfer")), Mapping
            ) else ""
            root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
            login = root.login()
            if not login.get("ok"):
                return {"root_login_succeeded": False}
            started = root.request("POST", "/api/root/points/chain/verify/jobs", json_body={})
            job = self._await_management_job(root, started, timeout_seconds=900)
            return {
                "root_login_succeeded": True,
                "readiness": self.recovery.wait_ready(timeout=180.0),
                "points_verify_job": job,
                "points_verify_latest": root.request("GET", "/api/root/points/chain/verify/latest"),
                "snapshot_transfer_explorer": (
                    root.request("GET", f"/api/points/explorer/tx/{quote(snapshot_hash, safe='')}")
                    if snapshot_hash else {"status": 0}
                ),
                "cli_transfer_explorer": (
                    root.request("GET", f"/api/points/explorer/tx/{quote(cli_hash, safe='')}")
                    if cli_hash else {"status": 0}
                ),
                "sqlite_quick_checks": self._sqlite_checks(self.recovery.runtime_root / "database"),
            }

        result = self.run_group(scenario_id, [
            lambda: self.run_native_callable_step(
                scenario_id,
                "portable_full_runtime_cycle",
                portable_out,
                lambda: self._portable_full_runtime_cycle(
                    out_dir=out_dir,
                    archive=portable_archive,
                    manifest_path=portable_manifest,
                ),
                payload_ok=lambda payload: bool(
                    (payload.get("archive") or {}).get("readable") is True
                    and not (payload.get("archive") or {}).get("unsafe_members")
                    and (payload.get("extracted_restore") or {}).get("all_manifest_files_present") is True
                    and not (payload.get("extracted_restore") or {}).get("hash_mismatches")
                    and payload.get("restore_root_removed") is True
                    and payload.get("source_storage_marker_removed") is True
                    and payload.get("server_restarted") is True
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "snapshot_restore_boundary",
                snapshot_out,
                self._snapshot_restore_boundary_cycle,
                payload_ok=lambda payload: payload.get("ok") is True,
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "live_runtime_restore",
                live_out,
                lambda: self._formal_live_restore_cycle(archive=ordinary_archive),
                payload_ok=lambda payload: bool(
                    int(payload.get("backup_command_returncode", -1)) == 0
                    and int(payload.get("restore_command_returncode", -1)) == 0
                    and payload.get("archive_readable") is True
                    and payload.get("protected_finance_hash_preserved") is True
                    and payload.get("storage_preserved") is True
                    and payload.get("dirty_marker_absent_after_restore") is True
                    and payload.get("transfer_survived_restore") is True
                    and payload.get("pre_restore_runtime_removed") is True
                    and payload.get("storage_marker_removed") is True
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "planned_restart",
                restart_out,
                self._formal_planned_restart,
                payload_ok=lambda payload: bool(
                    int((payload.get("stopped") or {}).get("old_pid") or 0) > 0
                    and (payload.get("stopped") or {}).get("master_process_remaining") is False
                    and (payload.get("stopped") or {}).get("process_group_remaining") is False
                    and int((payload.get("started") or {}).get("new_pid") or 0) > 0
                    and (payload.get("started") or {}).get("readiness_succeeded") is True
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "backup_fixture_cleanup",
                cleanup_out,
                cleanup,
                payload_ok=lambda payload: bool(
                    payload.get("snapshot_deleted") is True
                    and payload.get("snapshot_absent") is True
                    and payload.get("snapshot_storage_marker_removed") is True
                    and payload.get("unexpected_pre_restore_paths") == []
                ),
            ),
            lambda: self.run_native_callable_step(
                scenario_id,
                "post_restart_invariants",
                final_out,
                final_state,
                payload_ok=lambda payload: bool(
                    payload.get("root_login_succeeded") is True
                    and (payload.get("readiness") or {}).get("ok") is True
                    and (payload.get("points_verify_job") or {}).get("terminal_status") == "succeeded"
                    and int((payload.get("snapshot_transfer_explorer") or {}).get("status") or 0) == 200
                    and int((payload.get("cli_transfer_explorer") or {}).get("status") or 0) == 200
                    and payload.get("sqlite_quick_checks")
                    and all(
                        row.get("ok") is True
                        for row in (payload.get("sqlite_quick_checks") or {}).values()
                    )
                ),
            ),
        ])
        for artifact_id, path, artifact_type in (
            ("native.source.backup_restore_restart.portable_manifest", portable_manifest, "json"),
            ("native.source.backup_restore_restart.portable_archive", portable_archive, "archive"),
            ("native.source.backup_restore_restart.ordinary_archive", ordinary_archive, "archive"),
        ):
            if path.is_file() and not path.is_symlink():
                result["artifacts"].append({
                    "artifact_id": artifact_id,
                    "path": str(path.resolve(strict=True)),
                    "artifact_type": artifact_type,
                })
        selected = backup_restore_assertions(
            load_json(portable_out),
            load_json(snapshot_out),
            load_json(live_out),
            load_json(restart_out),
            load_json(final_out),
            load_json(cleanup_out),
        )
        return attach_native_evidence(
            result,
            scenario_id=scenario_id,
            output_dir=out_dir,
            scenario_assertions=selected["scenario_assertions"],
            terminal_assertions=selected["terminal_assertions"],
            cleanup_assertions=selected["cleanup_assertions"],
            details=selected["details"],
        )

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
            quota = self._configure_campaign_storage_quota(
                root,
                int(created.get("user_id") or 0),
            )
            if not quota.get("ok"):
                raise RuntimeError(
                    f"campaign account storage quota provisioning failed: {quota}"
                )
            self.account_inventory.append({
                "username": username,
                "user_id": int(created.get("user_id") or 0),
                "source": "campaign_runner",
                "created_or_reused": int(created.get("create_status") or 0) in {200, 201, 409},
                "storage_quota": quota,
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
            inventory_user_id = int(row.get("user_id") or 0)
            lookup = self._request_with_controlled_backpressure_retry(
                root,
                "GET",
                "/api/admin/users",
                params={"q": username, "page_size": 100},
            )
            self._server_progress(f"audit_account_lookup_completed:{username}")
            users = ((lookup.get("body") or {}).get("users") or []) if isinstance(lookup.get("body"), dict) else []
            exact = next((item for item in users if str(item.get("username") or "") == username), None)
            matched_user_id = int((exact or {}).get("id") or 0)
            user_id = matched_user_id or inventory_user_id
            absent_before_cleanup = bool(
                int(lookup.get("status") or 0) == 200 and exact is None
            )
            if absent_before_cleanup:
                deleted: dict[str, Any] = {
                    "ok": True,
                    "status": None,
                    "body": {"cleanup": {"warnings": []}},
                }
                verify = lookup
            elif matched_user_id > 0:
                deleted = self._request_with_controlled_backpressure_retry(
                    root,
                    "DELETE",
                    f"/api/admin/users/{matched_user_id}",
                )
                self._server_progress(f"audit_account_delete_completed:{username}")
                verify = self._request_with_controlled_backpressure_retry(
                    root,
                    "GET",
                    "/api/admin/users",
                    params={"q": username, "page_size": 100},
                )
            else:
                deleted = {
                    "ok": False,
                    "status": None,
                    "body": {"msg": "account_lookup_failed_before_cleanup"},
                }
                verify = lookup
            self._server_progress(f"audit_account_verify_completed:{username}")
            verify_users = ((verify.get("body") or {}).get("users") or []) if isinstance(verify.get("body"), dict) else []
            residual = [item for item in verify_users if str(item.get("username") or "") == username]
            cleanup_detail = (deleted.get("body") or {}).get("cleanup") or {}
            warnings = cleanup_detail.get("warnings") or []
            record_ok = bool(
                (absent_before_cleanup or int(deleted.get("status") or 0) == 200)
                and not residual
                and not warnings
                and int(verify.get("status") or 0) == 200
            )
            result["records"].append({
                "username": username,
                "user_id": user_id,
                "inventory_user_id": inventory_user_id,
                "source": row.get("source"),
                "lookup_status": lookup.get("status"),
                "absent_before_cleanup": absent_before_cleanup,
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
        adapter_registry = self.native_scenario_evidence_adapter_registry()
        validator_registry = self.native_scenario_validator_registry()
        return build_and_validate_formal_scenario_bindings(
            adapter_registry=adapter_registry,
            runner_registry=self.native_scenario_runner_registry(),
            validator_registry=validator_registry,
            runtime_execution_pipeline_verified=strict_native_runtime_pipeline_verified(
                adapter_registry=adapter_registry,
                validator_registry=validator_registry,
            ),
        ).to_dict(), True

    def native_scenario_evidence_adapter_registry(
        self,
    ) -> Mapping[str, NativeEvidenceAdapterRegistration]:
        """Return all 91 strict artifact-backed evidence adapters.

        Registration does not waive the per-scenario audited blockers.  Each
        adapter requires a native evidence manifest and re-evaluates its JSON
        pointers against independently reopened probe artifacts.
        """

        return build_strict_native_adapter_registry()

    def native_scenario_validator_registry(
        self,
    ) -> Mapping[str, ScenarioValidatorRegistration]:
        """Return the 13 terminal, cleanup, and artifact validator trios."""

        return build_strict_native_validator_registry()

    def native_scenario_runner_registry(self) -> Mapping[str, ScenarioRunnerRegistration]:
        """Return the audited exact-ID native runners currently executable.

        A runner is registered only after its machine artifacts, semantic
        selectors, terminal state, cleanup, and strict artifact validators are
        wired through the formal native pipeline.  Unreviewed legacy methods
        are never aliased to a reviewed scenario ID.
        """

        handlers: Mapping[str, Callable[[], dict[str, Any]]] = {
            "ai_agent_positive_operations": self.native_ai_agent_positive_operations,
            "bt_download_stream_restart": self.native_bt_download_stream_restart,
            "cloud_drive_share_stream": self.native_cloud_drive_share_stream,
            "comfyui_real_workflows": self.native_comfyui_real_workflows,
            "community_governance_operations": self.native_community_governance_operations,
            "media_long_hls_share": self.native_media_long_hls_share,
            "pointschain_hft_invariants": self.native_points_hft_invariants,
            "wallet_incident_governance": self.native_wallet_incident_governance,
            "backup_restore_restart": self.native_backup_restore_restart,
            "server_emergency_incident": self.native_server_emergency_incident,
            "trading_background_custom_workflow": self.native_trading_background_custom_workflow,
            "media_proxy_cross_browser": self.native_media_proxy_cross_browser,
            "final_ui_mobile_prelaunch": self.native_final_ui_mobile_prelaunch,
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
        """Execute only a fully reviewed native binding through the strict pipeline."""

        if scenario_id not in FORMAL_SCENARIO_BINDINGS:
            return {
                "ok": False,
                "classification": "FAIL_HARNESS",
                "error": "formal_native_scenario_unknown",
                "scenario_id": scenario_id,
            }
        adapter_registry = self.native_scenario_evidence_adapter_registry()
        runner_registry = self.native_scenario_runner_registry()
        validator_registry = self.native_scenario_validator_registry()
        pipeline_verified = strict_native_runtime_pipeline_verified(
            adapter_registry=adapter_registry,
            validator_registry=validator_registry,
        )
        gate = build_and_validate_formal_scenario_bindings(
            adapter_registry=adapter_registry,
            runner_registry=runner_registry,
            validator_registry=validator_registry,
            runtime_execution_pipeline_verified=pipeline_verified,
        )
        coverage = gate.registration_coverage.get(scenario_id) or {}
        if coverage.get("fully_bound") is not True:
            return {
                "ok": False,
                "classification": "FAIL_HARNESS",
                "error": "formal_native_binding_incomplete",
                "scenario_id": scenario_id,
                "registration_coverage": dict(coverage),
                "binding_blockers": list(gate.binding_blockers.get(scenario_id) or ()),
                "runtime_execution_pipeline_verified": pipeline_verified,
            }
        result = execute_registered_native_scenario(
            binding=FORMAL_SCENARIO_BINDINGS[scenario_id],
            runner_registry=runner_registry,
            adapter_registry=adapter_registry,
            validator_registry=validator_registry,
            artifact_root=self.reports / "scenarios" / scenario_id,
            known_secret_values={
                "root": self.credentials.root,
                "manager": self.credentials.manager,
                "test": self.credentials.test,
                "member": self.credentials.member,
            },
            authority=self.native_scenario_authority_identities[scenario_id],
        )
        result["registration_coverage"] = dict(coverage)
        return result

    def preflight(self) -> dict[str, Any]:
        commands = preflight_dependency_commands(self.campaign_level)
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
        runtime_scan = preflight_repo_runtime_scan(
            ROOT,
            campaign_level=self.campaign_level,
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
            "dependency_scope": {
                "campaign_level": self.campaign_level,
                "required": sorted(commands),
                "smoke_omits_unused_formal_capabilities": (
                    self.campaign_level == "smoke"
                ),
            },
            "free_bytes": free_bytes,
            "minimum_free_bytes": int(self.args.minimum_free_gb * 1024**3),
            "repo_runtime_pollution": runtime_pollution,
            "repo_runtime_scan": runtime_scan,
            "source_manifest_files": self.source_manifest_file_count,
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
        process: subprocess.Popen[Any] | None = None
        placement: dict[str, Any] = {}
        errors: list[str] = []
        expected = "/" + self.cgroup_path.strip().lstrip("/")
        try:
            process = subprocess.Popen(
                ["/bin/sleep", "30"],
                cwd=str(ROOT),
                env=self.base_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 10
            while not placement and time.monotonic() < deadline:
                if process.poll() is not None:
                    errors.append(f"direct_child:probe_exited:{process.returncode}")
                    break
                try:
                    actual = _current_unified_cgroup(process.pid)
                except Exception:
                    time.sleep(0.05)
                    continue
                inside = actual == expected or actual.startswith(
                    expected.rstrip("/") + "/"
                )
                placement = {
                    "pid": process.pid,
                    "expected_cgroup": expected,
                    "actual_cgroup": actual,
                    "inside_campaign_scope": inside,
                    "ok": inside,
                }
                if not inside:
                    errors.append(f"direct_child:outside_campaign_scope:{actual}")
                time.sleep(0.05)
            if not placement and not errors:
                errors.append("direct_child:membership_unobservable")
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        result = {
            "schema_version": "hackme.campaign-process-inheritance.v2",
            "probe_mode": "single_direct_child_kernel_inheritance",
            "probe_command": ["/bin/sleep", "30"],
            "probe_count": 1,
            "managed_roles_covered": list(roles),
            "placement": placement,
            "errors": errors,
            "ok": not errors and placement.get("ok") is True,
        }
        durable_atomic_write_json(self.reports / "preflight_process_inheritance.json", result)
        return result

    @staticmethod
    def _stream_file_metadata(path: Path) -> dict[str, Any]:
        candidate = Path(path)
        before = os.lstat(candidate)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or int(before.st_nlink) != 1
        ):
            raise RuntimeError(f"evidence path is not a single-link regular file: {candidate}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        digest = hashlib.sha256()
        size = 0
        final_fd: os.stat_result | None = None
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError(f"evidence path changed before open: {candidate}")
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
            final_fd = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if final_fd is None:
            raise RuntimeError(f"evidence path could not be hashed: {candidate}")
        after = os.lstat(candidate)
        if (
            size != int(before.st_size)
            or (final_fd.st_dev, final_fd.st_ino, final_fd.st_size, final_fd.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            raise RuntimeError(f"evidence path changed while hashing: {candidate}")
        return {
            "path": str(candidate.resolve(strict=True)),
            "size_bytes": size,
            "sha256": digest.hexdigest(),
        }

    def validate_online_security_audit_evidence(
        self,
        result: Mapping[str, Any],
        *,
        output_dir: Path,
    ) -> dict[str, Any]:
        errors: list[str] = []
        reference = result.get("audit_evidence")
        if not isinstance(reference, Mapping):
            errors.append("audit_evidence_reference_missing")
            reference = {}
        receipt_path = output_dir / "receipt.json"
        archive_path = output_dir.with_name(f"{output_dir.name}.tar")
        receipt: dict[str, Any] = {}
        metadata: dict[str, Any] = {}
        archive_metadata: dict[str, Any] = {}
        try:
            metadata = self._stream_file_metadata(receipt_path)
            loaded = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise RuntimeError("receipt root is not an object")
            receipt = loaded
        except Exception as exc:
            errors.append(f"receipt_read_failed:{exc.__class__.__name__}")
        contract = validate_audit_evidence_receipt(
            receipt,
            required_mode="online",
            required_target="security_sentinel",
            artifact_root=output_dir if receipt else None,
        )
        errors.extend(f"receipt_contract:{code}" for code in contract.get("errors") or [])
        try:
            archive_metadata = self._stream_file_metadata(archive_path)
            archive_contract = validate_audit_evidence_archive(
                archive_path,
                required_mode="online",
                required_target="security_sentinel",
                expected_sha256=str(archive_metadata.get("sha256") or ""),
                expected_size=int(archive_metadata.get("size_bytes") or 0),
            )
        except Exception as exc:
            archive_contract = {
                "schema_version": "hackme.audit-evidence-triad-archive-validation/v1",
                "ok": False,
                "classification": "FAIL_HARNESS",
                "errors": [{"code": f"archive_read_failed:{exc.__class__.__name__}"}],
            }
        for row in archive_contract.get("errors") or []:
            code = row.get("code") if isinstance(row, Mapping) else str(row)
            errors.append(f"archive_contract:{code}")
        if archive_contract.get("ok") is not True and not archive_contract.get("errors"):
            errors.append("archive_contract:archive_validation_failed_without_error")
        if reference:
            if set(reference) != {
                "schema_version",
                "receipt_schema_version",
                "mode",
                "target",
                "receipt_path",
                "receipt_sha256",
                "receipt_size_bytes",
                "receipt",
                "validation",
                "archive_schema_version",
                "archive_path",
                "archive_sha256",
                "archive_size_bytes",
                "archive_validation",
            }:
                errors.append("audit_evidence_reference_shape_mismatch")
            if (
                reference.get("schema_version")
                != "hackme.audit-evidence-triad-reference/v1"
                or reference.get("receipt_schema_version")
                != AUDIT_EVIDENCE_SCHEMA_VERSION
                or reference.get("mode") != "online"
                or reference.get("target") != "security_sentinel"
            ):
                errors.append("audit_evidence_reference_identity_mismatch")
            if reference.get("receipt") != receipt:
                errors.append("embedded_receipt_mismatch")
            if (
                reference.get("receipt_path") != metadata.get("path")
                or reference.get("receipt_sha256") != metadata.get("sha256")
                or reference.get("receipt_size_bytes") != metadata.get("size_bytes")
            ):
                errors.append("audit_evidence_reference_hash_mismatch")
            if reference.get("validation") != contract:
                errors.append("audit_evidence_reference_validation_mismatch")
            if (
                reference.get("archive_schema_version")
                != AUDIT_EVIDENCE_ARCHIVE_SCHEMA_VERSION
                or reference.get("archive_path") != archive_metadata.get("path")
                or reference.get("archive_sha256") != archive_metadata.get("sha256")
                or reference.get("archive_size_bytes")
                != archive_metadata.get("size_bytes")
            ):
                errors.append("audit_evidence_archive_reference_hash_mismatch")
            if reference.get("archive_validation") != archive_contract:
                errors.append("audit_evidence_archive_reference_validation_mismatch")
        checks = result.get("checks")
        check = next(
            (
                row
                for row in checks
                if isinstance(row, Mapping)
                and row.get("name") == "audit_evidence_triad_online"
            ),
            None,
        ) if isinstance(checks, list) else None
        if not isinstance(check, Mapping) or check.get("ok") is not True:
            errors.append("audit_evidence_check_missing_or_failed")
        else:
            detail = check.get("detail")
            if not isinstance(detail, Mapping) or set(detail) != {
                "receipt_schema_version",
                "mode",
                "target",
                "receipt_sha256",
                "receipt_size_bytes",
                "artifact_files_verified",
                "validation_classification",
                "validation_errors",
                "archive_schema_version",
                "archive_sha256",
                "archive_size_bytes",
                "archive_validation_classification",
                "archive_validation_errors",
            }:
                errors.append("audit_evidence_check_shape_mismatch")
            elif (
                detail.get("receipt_schema_version")
                != AUDIT_EVIDENCE_SCHEMA_VERSION
                or detail.get("mode") != "online"
                or detail.get("target") != "security_sentinel"
                or detail.get("receipt_sha256") != metadata.get("sha256")
                or detail.get("receipt_size_bytes") != metadata.get("size_bytes")
                or detail.get("artifact_files_verified") is not True
                or detail.get("validation_classification") != "PASS"
                or detail.get("validation_errors") != []
                or detail.get("archive_schema_version")
                != AUDIT_EVIDENCE_ARCHIVE_SCHEMA_VERSION
                or detail.get("archive_sha256") != archive_metadata.get("sha256")
                or detail.get("archive_size_bytes")
                != archive_metadata.get("size_bytes")
                or detail.get("archive_validation_classification") != "PASS"
                or detail.get("archive_validation_errors") != []
            ):
                errors.append("audit_evidence_check_binding_mismatch")
        wiring_errors = [
            code
            for code in errors
            if not code.startswith(("receipt_contract:", "archive_contract:"))
        ]
        classification = "PASS"
        if errors:
            classification = (
                "FAIL_HARNESS"
                if wiring_errors
                else (
                    "FAIL_PRODUCT"
                    if contract.get("classification") == "FAIL_PRODUCT"
                    or archive_contract.get("classification") == "FAIL_PRODUCT"
                    else "FAIL_HARNESS"
                )
            )
            if classification not in {"FAIL_PRODUCT", "FAIL_HARNESS"}:
                classification = "FAIL_HARNESS"
        return {
            "schema_version": "hackme.audit-evidence-triad-online-wiring/v1",
            "ok": not errors and contract.get("ok") is True,
            "classification": classification,
            "receipt": metadata,
            "contract": contract,
            "archive": archive_metadata,
            "archive_contract": archive_contract,
            "errors": sorted(set(errors)),
        }

    def production_security_sentinel_check(self, *, phase: str) -> dict[str, Any]:
        if phase not in {"preflight", "final"}:
            return {
                "schema_version": "hackme.production-security-sentinel.v1",
                "ok": False,
                "classification": "FAIL_HARNESS",
                "failed_checks": ["invalid_phase"],
                "checks": [],
            }

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

        audit_evidence_output = (
            self.reports / "security" / f"audit_evidence_{phase}"
        ).resolve(strict=False)
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
            audit_evidence_output_dir=audit_evidence_output,
            audit_evidence_target="security_sentinel",
        ), session_factory=observed_session_factory)
        result = probe.run_once()
        audit_validation = self.validate_online_security_audit_evidence(
            result,
            output_dir=audit_evidence_output,
        )
        result["audit_evidence_validation"] = audit_validation
        if audit_validation.get("ok") is not True:
            result["ok"] = False
            result["classification"] = str(
                audit_validation.get("classification") or "FAIL_HARNESS"
            )
            failed = list(result.get("failed_checks") or [])
            if "audit_evidence_triad_online" not in failed:
                failed.append("audit_evidence_triad_online")
            result["failed_checks"] = failed
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
            self.supervised and self.campaign_level in {"rehearsal", "soak", "formal"}
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
        if not (self.supervised and self.campaign_level in {"rehearsal", "soak", "formal"}):
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
        if not (self.supervised and self.campaign_level in {"rehearsal", "soak", "formal"}):
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
        if not (self.supervised and self.campaign_level in {"rehearsal", "soak", "formal"}):
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
            "ai_agent_positive_operations": "recovery",
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
            if result.get("ok") is True:
                try:
                    binding = FORMAL_SCENARIO_BINDINGS[spec.scenario_id]
                    receipt = result.get("runtime_receipt")
                    validation = validate_scenario_runtime_receipt(receipt, binding)
                    if (
                        not isinstance(receipt, Mapping)
                        or validation.valid is not True
                        or validation.contract_pass is not True
                        or validation.status.value != "PASS"
                    ):
                        raise RuntimeError(
                            "strict runtime receipt cannot be persisted: "
                            + ",".join(validation.errors)
                        )
                    receipt_path = (
                        self.reports
                        / "scenario_receipts"
                        / f"{spec.scenario_id}.json"
                    )
                    if receipt_path.exists() or receipt_path.is_symlink():
                        raise RuntimeError(
                            f"scenario runtime receipt path already exists: {receipt_path}"
                        )
                    atomic_write_json(receipt_path, dict(receipt))
                    reopened = load_json(receipt_path)
                    reopened_validation = validate_scenario_runtime_receipt(
                        reopened,
                        binding,
                    )
                    if (
                        reopened != dict(receipt)
                        or reopened_validation.valid is not True
                        or reopened_validation.contract_pass is not True
                        or reopened_validation.status.value != "PASS"
                    ):
                        raise RuntimeError(
                            "persisted runtime receipt failed exact readback validation"
                        )
                    scenario_artifact_root = (
                        self.reports / "scenarios" / spec.scenario_id
                    ).resolve(strict=True)
                    bundle_path = Path(
                        str(result.get("artifact_bundle_path") or "")
                    ).resolve(strict=True)
                    archive_path = Path(
                        str(result.get("artifact_archive_path") or "")
                    ).resolve(strict=True)
                    bundle_path.relative_to(scenario_artifact_root)
                    archive_path.relative_to(scenario_artifact_root)
                    bundle_sha256 = self._sha256(bundle_path)
                    archive_sha256 = self._sha256(archive_path)
                    if (
                        bundle_sha256 != result.get("artifact_bundle_sha256")
                        or archive_sha256
                        != result.get("artifact_archive_sha256")
                        or bundle_path.stat().st_size
                        != int(result.get("artifact_bundle_size_bytes") or -1)
                        or archive_path.stat().st_size
                        != int(result.get("artifact_archive_size_bytes") or -1)
                    ):
                        raise RuntimeError(
                            "native bundle/archive exact readback identity mismatch"
                        )
                    bundle_payload = load_json(bundle_path)
                    archive_reference = bundle_payload.get("artifact_archive")
                    receipt_bundle = reopened.get("artifact_bundle")
                    if (
                        bundle_payload.get("authority") != reopened.get("authority")
                        or not isinstance(archive_reference, Mapping)
                        or not isinstance(receipt_bundle, Mapping)
                        or archive_reference.get("path") != str(archive_path)
                        or archive_reference.get("sha256") != archive_sha256
                        or archive_reference.get("size_bytes")
                        != archive_path.stat().st_size
                        or receipt_bundle.get("path") != str(bundle_path)
                        or receipt_bundle.get("sha256") != bundle_sha256
                        or receipt_bundle.get("size_bytes")
                        != bundle_path.stat().st_size
                        or receipt_bundle.get("artifact_archive_sha256")
                        != archive_sha256
                        or receipt_bundle.get("artifact_archive_size_bytes")
                        != archive_path.stat().st_size
                    ):
                        raise RuntimeError(
                            "runtime receipt/bundle/archive authority chain mismatch"
                        )
                    result["runtime_receipt_path"] = str(receipt_path.resolve(strict=True))
                    result["runtime_receipt_sha256"] = self._sha256(receipt_path)
                except Exception as exc:
                    result = {
                        **result,
                        "ok": False,
                        "classification": "FAIL_HARNESS",
                        "error": (
                            "runtime_receipt_persistence_failed:"
                            f"{exc.__class__.__name__}:{exc}"
                        ),
                    }
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

    @staticmethod
    def _runtime_writer_pids(runtime_root: Path) -> list[int]:
        roots = {
            str(runtime_root),
            str(runtime_root.resolve(strict=False)),
        }
        markers = {
            f"HACKME_RUNTIME_DIR={root}".encode("utf-8") for root in roots
        }
        observed: list[int] = []
        proc_root = Path("/proc")
        try:
            entries = list(proc_root.iterdir())
        except OSError:
            return [-1]
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                process_info = entry.stat()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                return [-1]
            if int(process_info.st_uid) != os.geteuid():
                continue
            try:
                environment = (entry / "environ").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (PermissionError, OSError):
                return [-1]
            if markers.intersection(environment.split(b"\0")):
                observed.append(int(entry.name))
        return sorted(observed)

    def verify_final_audit_writer_seal(
        self,
        log_seal_stops: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        controllers = {
            controller.name: controller
            for controller in (self.primary, self.recovery, self.security_sentinel)
        }
        errors: list[str] = []
        targets: dict[str, Any] = {}
        if set(log_seal_stops) != set(controllers):
            errors.append("stop_receipt_target_set_mismatch")
        for name, controller in controllers.items():
            stop = log_seal_stops.get(name)
            target_errors: list[str] = []
            if not isinstance(stop, Mapping):
                target_errors.append("stop_receipt_missing")
                stop = {}
            if (
                stop.get("ok") is not True
                or stop.get("name") != name
                or stop.get("reason") != "final_evidence_log_seal"
            ):
                target_errors.append("stop_receipt_identity_or_verdict_invalid")
            if stop.get("master_process_remaining") is not False:
                target_errors.append("master_process_not_proven_stopped")
            if stop.get("process_group_remaining") is not False:
                target_errors.append("process_group_not_proven_stopped")
            if (
                stop.get("restart_disabled") is not True
                or stop.get("launch_generation") != controller.launch_count
                or controller.final_evidence_restart_disabled is not True
            ):
                target_errors.append("final_evidence_restart_barrier_missing")
            if controller.planned_outage.is_set() is not True:
                target_errors.append("planned_outage_barrier_missing")
            if controller.registered_identity is not None:
                target_errors.append("process_registry_identity_still_registered")
            live_pids = self._runtime_writer_pids(controller.runtime_root)
            if live_pids:
                target_errors.append("runtime_writer_process_still_alive")
            targets[name] = {
                "ok": not target_errors,
                "runtime_root": str(controller.runtime_root),
                "stop_receipt": dict(stop),
                "live_runtime_pids": live_pids,
                "errors": target_errors,
            }
            errors.extend(f"{name}:{code}" for code in target_errors)
        return {
            "schema_version": FINAL_AUDIT_EVIDENCE_SEAL_SCHEMA_VERSION,
            "ok": not errors,
            "verified_at": utc_now(),
            "required_targets": sorted(controllers),
            "targets": targets,
            "errors": sorted(set(errors)),
        }

    def capture_final_audit_evidence(
        self,
        log_seal_stops: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        evidence_root = (
            self.reports / "audit_evidence" / "sealed_final"
        ).resolve(strict=False)
        controllers = {
            controller.name: controller
            for controller in (self.primary, self.recovery, self.security_sentinel)
        }
        seal = self.verify_final_audit_writer_seal(log_seal_stops)
        errors: list[str] = []
        target_results: dict[str, dict[str, Any]] = {}
        try:
            evidence_root.mkdir(parents=True, mode=0o700, exist_ok=False)
            os.chmod(evidence_root, 0o700)
        except Exception as exc:
            return {
                "schema_version": FINAL_AUDIT_EVIDENCE_INDEX_SCHEMA_VERSION,
                "ok": False,
                "classification": "FAIL_HARNESS",
                "capture_attempted": False,
                "writer_seal": seal,
                "targets": {},
                "errors": [f"evidence_root_create_failed:{exc.__class__.__name__}"],
            }

        schema_destination = evidence_root / AUDIT_EVIDENCE_SCHEMA_PATH.name
        try:
            schema_bytes = AUDIT_EVIDENCE_SCHEMA_PATH.read_bytes()
            schema_destination.write_bytes(schema_bytes)
            os.chmod(schema_destination, 0o600)
        except Exception as exc:
            errors.append(f"receipt_schema_copy_failed:{exc.__class__.__name__}")

        capture_attempted = seal.get("ok") is True and not errors
        if capture_attempted:
            for name, controller in controllers.items():
                self._server_progress(f"audit_triad_sealed_capture_started:{name}")
                output_dir = evidence_root / name
                receipt: dict[str, Any] = {}
                receipt_metadata: dict[str, Any] = {}
                contract: dict[str, Any] = {}
                try:
                    live_before_capture = self._runtime_writer_pids(
                        controller.runtime_root
                    )
                    if live_before_capture:
                        raise RuntimeError(
                            "runtime_writer_detected_immediately_before_sealed_capture"
                        )
                    capture_audit_evidence(
                        paths=AuditEvidencePaths.for_runtime(controller.runtime_root),
                        output_dir=output_dir,
                        target=name,
                        mode="sealed",
                    )
                    receipt_path = output_dir / "receipt.json"
                    receipt_metadata = self._stream_file_metadata(receipt_path)
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if not isinstance(receipt, dict):
                        raise RuntimeError("sealed receipt root is not an object")
                    contract = validate_audit_evidence_receipt(
                        receipt,
                        required_mode="sealed",
                        required_target=name,
                        artifact_root=output_dir,
                    )
                    live_after_capture = self._runtime_writer_pids(
                        controller.runtime_root
                    )
                    if live_after_capture:
                        raise RuntimeError(
                            "runtime_writer_detected_during_sealed_capture"
                        )
                    recheck_dir = output_dir / "post_seal_recheck"
                    capture_audit_evidence(
                        paths=AuditEvidencePaths.for_runtime(controller.runtime_root),
                        output_dir=recheck_dir,
                        target=name,
                        mode="online",
                    )
                    recheck_path = recheck_dir / "receipt.json"
                    recheck_metadata = self._stream_file_metadata(recheck_path)
                    recheck_receipt = json.loads(
                        recheck_path.read_text(encoding="utf-8")
                    )
                    if not isinstance(recheck_receipt, dict):
                        raise RuntimeError("post_seal_recheck_receipt_not_object")
                    recheck_contract = validate_audit_evidence_receipt(
                        recheck_receipt,
                        required_mode="online",
                        required_target=name,
                        artifact_root=recheck_dir,
                    )
                    if (
                        recheck_contract.get("ok") is not True
                        or recheck_receipt.get("counts") != receipt.get("counts")
                        or recheck_receipt.get("heads") != receipt.get("heads")
                    ):
                        raise RuntimeError("post_seal_live_head_changed")
                    live_after_recheck = self._runtime_writer_pids(
                        controller.runtime_root
                    )
                    if live_after_recheck:
                        raise RuntimeError(
                            "runtime_writer_detected_after_post_seal_recheck"
                        )
                    target_ok = contract.get("ok") is True
                    classification = (
                        "PASS"
                        if target_ok
                        else "FAIL_PRODUCT"
                        if receipt.get("verdict") == "FAIL_PRODUCT"
                        else "FAIL_HARNESS"
                    )
                    target_results[name] = {
                        "ok": target_ok,
                        "classification": classification,
                        "receipt_verdict": receipt.get("verdict"),
                        "receipt": {
                            **receipt_metadata,
                            "path": str(receipt_path.relative_to(evidence_root)),
                        },
                        "artifacts": receipt.get("artifacts"),
                        "counts": receipt.get("counts"),
                        "heads": receipt.get("heads"),
                        "contract_validation": contract,
                        "post_seal_recheck": {
                            "ok": True,
                            "receipt": {
                                **recheck_metadata,
                                "path": str(
                                    recheck_path.relative_to(evidence_root)
                                ),
                            },
                            "contract_validation": recheck_contract,
                            "counts": recheck_receipt.get("counts"),
                            "heads": recheck_receipt.get("heads"),
                            "live_runtime_pids_after": live_after_recheck,
                        },
                        "errors": list(contract.get("errors") or []),
                    }
                except Exception as exc:
                    detail = str(exc)
                    error_code = (
                        detail
                        if detail.startswith("runtime_writer_detected_")
                        else f"capture_failed:{exc.__class__.__name__}"
                    )
                    target_results[name] = {
                        "ok": False,
                        "classification": (
                            "FAIL_PRODUCT"
                            if receipt.get("verdict") == "FAIL_PRODUCT"
                            else "FAIL_HARNESS"
                        ),
                        "receipt_verdict": receipt.get("verdict") or "FAIL_HARNESS",
                        "receipt": (
                            {
                                **receipt_metadata,
                                "path": str(
                                    (output_dir / "receipt.json").relative_to(
                                        evidence_root
                                    )
                                ),
                            }
                            if receipt_metadata
                            else None
                        ),
                        "artifacts": receipt.get("artifacts"),
                        "counts": receipt.get("counts"),
                        "heads": receipt.get("heads"),
                        "contract_validation": contract or None,
                        "post_seal_recheck": None,
                        "errors": [error_code],
                    }
                self._server_progress(
                    f"audit_triad_sealed_capture_completed:{name}:"
                    f"{int(bool(target_results[name].get('ok')))}"
                )
        else:
            errors.extend(str(code) for code in seal.get("errors") or [])
            for name in controllers:
                target_results[name] = {
                    "ok": False,
                    "classification": "FAIL_HARNESS",
                    "receipt_verdict": "BLOCKED_BY_WRITER_SEAL",
                    "receipt": None,
                    "artifacts": None,
                    "counts": None,
                    "heads": None,
                    "contract_validation": None,
                    "post_seal_recheck": None,
                    "errors": ["sealed_capture_forbidden_without_writer_seal"],
                }

        for name, result in target_results.items():
            if result.get("ok") is not True:
                errors.extend(
                    f"{name}:{code}" for code in result.get("errors") or ["receipt_failed"]
                )
        classification = "PASS"
        if errors:
            classifications = {
                str(result.get("classification") or "FAIL_HARNESS")
                for result in target_results.values()
                if result.get("ok") is not True
            }
            classification = (
                "FAIL_HARNESS"
                if "FAIL_HARNESS" in classifications or seal.get("ok") is not True
                else "FAIL_PRODUCT"
            )

        schema_metadata: dict[str, Any] = {}
        try:
            schema_metadata = self._stream_file_metadata(schema_destination)
            schema_metadata["path"] = str(schema_destination.relative_to(evidence_root))
            schema_metadata["receipt_schema_version"] = AUDIT_EVIDENCE_SCHEMA_VERSION
        except Exception as exc:
            errors.append(f"receipt_schema_hash_failed:{exc.__class__.__name__}")
            classification = "FAIL_HARNESS"

        index_payload = {
            "schema_version": FINAL_AUDIT_EVIDENCE_INDEX_SCHEMA_VERSION,
            "created_at": utc_now(),
            "mode": "sealed",
            "required_targets": sorted(controllers),
            "capture_attempted": capture_attempted,
            "writer_seal": seal,
            "receipt_schema": schema_metadata,
            "targets": target_results,
            "ok": not errors and all(
                result.get("ok") is True for result in target_results.values()
            ),
            "classification": classification,
            "errors": sorted(set(errors)),
        }
        permission_errors: list[str] = []
        for path in sorted(evidence_root.rglob("*"), reverse=True):
            try:
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    raise RuntimeError("symlink evidence path")
                os.chmod(path, 0o500 if stat.S_ISDIR(info.st_mode) else 0o400)
            except Exception as exc:
                permission_errors.append(
                    f"{path.relative_to(evidence_root)}:{exc.__class__.__name__}"
                )
        if permission_errors:
            index_payload["ok"] = False
            index_payload["classification"] = "FAIL_HARNESS"
            index_payload["errors"] = sorted(set(
                list(index_payload["errors"])
                + [f"artifact_permission:{code}" for code in permission_errors]
            ))
        index_path = evidence_root / "artifact_index.json"
        atomic_write_json(index_path, index_payload)
        os.chmod(index_path, 0o400)

        manifest_errors: list[str] = []
        manifest_files: list[dict[str, Any]] = []
        for path in sorted(evidence_root.rglob("*")):
            try:
                info = os.lstat(path)
            except OSError as exc:
                manifest_errors.append(
                    f"{path.relative_to(evidence_root)}:{exc.__class__.__name__}"
                )
                continue
            if path.name == "hash_manifest.json" or stat.S_ISDIR(info.st_mode):
                continue
            try:
                metadata = self._stream_file_metadata(path)
                manifest_files.append({
                    "path": str(path.relative_to(evidence_root)),
                    "size_bytes": metadata["size_bytes"],
                    "sha256": metadata["sha256"],
                })
                self._server_progress(
                    f"audit_triad_manifest_hashed:{path.relative_to(evidence_root)}"
                )
            except Exception as exc:
                manifest_errors.append(
                    f"{path.relative_to(evidence_root)}:{exc.__class__.__name__}"
                )
        if manifest_errors:
            index_payload["ok"] = False
            index_payload["classification"] = "FAIL_HARNESS"
            index_payload["errors"] = sorted(
                set(index_payload["errors"] + [
                    f"hash_manifest:{code}" for code in manifest_errors
                ])
            )
            atomic_write_json(index_path, index_payload)
            os.chmod(index_path, 0o400)
            refreshed_index = self._stream_file_metadata(index_path)
            for row in manifest_files:
                if row.get("path") == index_path.name:
                    row["size_bytes"] = refreshed_index["size_bytes"]
                    row["sha256"] = refreshed_index["sha256"]
                    break

        manifest_payload = {
            "schema_version": FINAL_AUDIT_EVIDENCE_MANIFEST_SCHEMA_VERSION,
            "created_at": utc_now(),
            "root": str(evidence_root),
            "file_count": len(manifest_files),
            "files": manifest_files,
            "errors": manifest_errors,
            "ok": not manifest_errors,
        }
        manifest_path = evidence_root / "hash_manifest.json"
        atomic_write_json(manifest_path, manifest_payload)
        os.chmod(manifest_path, 0o400)
        index_metadata = self._stream_file_metadata(index_path)
        manifest_metadata = self._stream_file_metadata(manifest_path)
        index_metadata["path"] = str(index_path)
        manifest_metadata["path"] = str(manifest_path)

        os.chmod(evidence_root, 0o500)
        return {
            **index_payload,
            "artifact_index": index_metadata,
            "hash_manifest": manifest_metadata,
            "artifact_root": str(evidence_root),
        }

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
                "watchdog_liveness": self.control_root / "checkpoint" / "watchdog.liveness.json",
                "activation_gate": self.activation_gate_path,
                "supervisor_contract": self.supervisor_contract_path,
                "source_freeze": self.source_freeze_path,
            }
            backend_contract = self.supervisor_contract.get("comfyui_backend")
            if isinstance(backend_contract, Mapping):
                for name in ("ready_receipt", "lifecycle_path", "stdout_path"):
                    raw = str(backend_contract.get(name) or "")
                    if raw:
                        controlled[f"comfyui_backend_{name}"] = Path(raw)
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
        backend_contract = self.supervisor_contract.get("comfyui_backend")
        if isinstance(backend_contract, Mapping):
            for name in ("ready_receipt", "lifecycle_path", "stdout_path"):
                raw = str(backend_contract.get(name) or "")
                if raw:
                    contract_paths[f"comfyui_backend_{name}"] = raw
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

    def _start_managed_servers_with_host_safety(self) -> dict[str, Any]:
        starts: dict[str, dict[str, Any]] = {}
        host_safety: dict[str, dict[str, Any]] = {}
        for name, controller in (
            ("primary", self.primary),
            ("recovery", self.recovery),
            ("security_sentinel", self.security_sentinel),
        ):
            self.write_checkpoint(f"starting_{name}")
            start = controller.start()
            starts[name] = start
            if start.get("ok") is not True:
                classification = str(
                    start.get("classification") or "FAIL_HARNESS"
                )
                return {
                    "ok": False,
                    "classification": classification,
                    "failed_stage": f"{name}_start",
                    "starts": starts,
                    "host_safety": host_safety,
                }
            self.write_checkpoint(f"waiting_for_{name}_host_safety")
            evidence = wait_for_runner_host_safety_preflight()
            atomic_write_json(
                self.reports / f"host_safety_after_{name}.json",
                evidence,
            )
            host_safety[name] = evidence
            if evidence.get("ok") is not True:
                return {
                    "ok": False,
                    "classification": "FAIL_INFRA",
                    "failed_stage": f"{name}_host_safety",
                    "starts": starts,
                    "host_safety": host_safety,
                }
        return {
            "ok": True,
            "classification": "PASS",
            "failed_stage": "",
            "starts": starts,
            "host_safety": host_safety,
        }

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
        host_safety = wait_for_runner_host_safety_preflight()
        atomic_write_json(
            self.reports / "host_safety_preflight.json",
            host_safety,
        )
        if host_safety.get("ok") is not True:
            self.mark_failed(reason="HOST_SAFETY_PREFLIGHT_FAILED")
            payload = {
                "ok": False,
                "verdict": "FAIL_INFRA",
                "classification": "FAIL_INFRA",
                "phase": "host_safety_preflight",
                "preflight": preflight,
                "host_safety": host_safety,
            }
            atomic_write_json(self.final_path, payload)
            return 2
        server_startup = self._start_managed_servers_with_host_safety()
        starts = server_startup.get("starts") or {}
        primary_start = starts.get("primary") or {"ok": False, "not_started": True}
        recovery_start = starts.get("recovery") or {"ok": False, "not_started": True}
        security_start = starts.get("security_sentinel") or {"ok": False, "not_started": True}
        if server_startup.get("ok") is not True:
            classification = str(
                server_startup.get("classification") or "FAIL_HARNESS"
            )
            self.mark_failed(
                reason=(
                    "HOST_SAFETY_AFTER_SERVER_START_FAILED"
                    if classification == "FAIL_INFRA"
                    else "SERVER_START_FAILED"
                )
            )
            payload = {
                "ok": False,
                "verdict": classification,
                "classification": classification,
                "phase": "server_start",
                "server_startup": server_startup,
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
            security_classification = str(
                security_preflight.get("classification") or "FAIL_HARNESS"
            )
            if security_classification not in {"FAIL_PRODUCT", "FAIL_HARNESS"}:
                security_classification = "FAIL_HARNESS"
            payload = {
                "ok": False,
                "verdict": security_classification,
                "classification": security_classification,
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
                    if self.campaign_level in {"formal", "soak", "rehearsal"} and (unfinished_at_deadline or missing_at_deadline):
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
        final_audit_evidence = self.capture_final_audit_evidence(log_seal_stops)
        self._server_progress(
            "audit_triad_sealed_final_complete:"
            f"{int(bool(final_audit_evidence.get('ok')))}"
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
            "soak",
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
        if not final_audit_evidence.get("ok"):
            audit_classification = str(
                final_audit_evidence.get("classification") or "FAIL_HARNESS"
            )
            if audit_classification not in {"FAIL_PRODUCT", "FAIL_HARNESS"}:
                audit_classification = "FAIL_HARNESS"
            findings.append({
                "severity": "critical",
                "classification": audit_classification,
                "title": "final sealed audit DB/log/anchor evidence failed",
                "errors": final_audit_evidence.get("errors"),
                "targets": final_audit_evidence.get("targets"),
            })

        execution_contract_state = (
            self.state_machine.snapshot() if self.state_machine is not None else {}
        )
        rehearsal_execution_contract = derive_rehearsal_execution_contract(
            self.scenario_results,
            execution_contract_state,
        )
        if self.supervised and self.campaign_level in {"rehearsal", "soak", "formal"}:
            contract_errors = list(rehearsal_execution_contract.get("errors") or [])
            if contract_errors:
                findings.append({
                    "severity": "critical",
                    "classification": "FAIL_HARNESS",
                    "title": "rehearsal execution contract could not be derived",
                    "errors": contract_errors,
                })
            invalid_seconds = rehearsal_execution_contract.get("invalid_seconds")
            if isinstance(invalid_seconds, (int, float)) and not isinstance(invalid_seconds, bool):
                if float(invalid_seconds) != 0.0:
                    findings.append({
                        "severity": "critical",
                        "classification": "INVALIDATED",
                        "title": "campaign contains invalid active time",
                        "invalid_seconds": float(invalid_seconds),
                    })
            for bucket in ("skips", "fallbacks", "expected_gaps"):
                rows = list(rehearsal_execution_contract.get(bucket) or [])
                if rows:
                    findings.append({
                        "severity": "critical",
                        "classification": "FAIL_HARNESS",
                        "title": f"mandatory scenario evidence contains {bucket}",
                        "markers": rows,
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
        runner_finished_at = utc_now()
        runner_finished_monotonic_ns = time.monotonic_ns()
        runner_started_monotonic_ns = max(
            1,
            int(self.active_started * 1_000_000_000),
        )
        if runner_finished_monotonic_ns <= runner_started_monotonic_ns:
            runner_finished_monotonic_ns = runner_started_monotonic_ns + 1
        payload = {
            "schema_version": "hackme.campaign-operational-result/v1",
            "ok": ok,
            "verdict": "PASS" if ok else classification,
            "classification": classification,
            "production_signoff_eligible": bool(ok and formal and self.supervised),
            "formal_campaign": formal,
            "started_at": self.active_started_at,
            "finished_at": runner_finished_at,
            "started_monotonic_ns": runner_started_monotonic_ns,
            "finished_monotonic_ns": runner_finished_monotonic_ns,
            **self.native_outer_authority_identity,
            "required_active_test_seconds": int(self.args.duration_seconds),
            "active_test_seconds": round(active_seconds, 3),
            "invalid_seconds": rehearsal_execution_contract.get("invalid_seconds"),
            "mandatory_features_executed": list(
                rehearsal_execution_contract.get("mandatory_features_executed") or []
            ),
            "skips": list(rehearsal_execution_contract.get("skips") or []),
            "fallbacks": list(rehearsal_execution_contract.get("fallbacks") or []),
            "expected_gaps": list(
                rehearsal_execution_contract.get("expected_gaps") or []
            ),
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
            "scenario_receipts": {
                scenario_id: {
                    "scenario_attempt_uuid": (
                        (result.get("runtime_receipt") or {}).get("authority") or {}
                    ).get("scenario_attempt_uuid"),
                    "native_invocation_id": (
                        (result.get("runtime_receipt") or {}).get("authority") or {}
                    ).get("native_invocation_id"),
                    "receipt": {
                        "path": result.get("runtime_receipt_path"),
                        "sha256": result.get("runtime_receipt_sha256"),
                        "size_bytes": (
                            Path(str(result.get("runtime_receipt_path"))).stat().st_size
                            if result.get("runtime_receipt_path")
                            else 0
                        ),
                    },
                    "artifact_bundle": {
                        "path": result.get("artifact_bundle_path"),
                        "sha256": result.get("artifact_bundle_sha256"),
                        "size_bytes": result.get("artifact_bundle_size_bytes"),
                    },
                    "artifact_archive": {
                        "path": result.get("artifact_archive_path"),
                        "sha256": result.get("artifact_archive_sha256"),
                        "size_bytes": result.get("artifact_archive_size_bytes"),
                    },
                }
                for scenario_id, result in sorted(self.scenario_results.items())
                if isinstance(result, Mapping)
                and result.get("runtime_receipt_path")
                and result.get("runtime_receipt_sha256")
            },
            "account_inventory": self.account_inventory,
            "account_cleanup": account_cleanup,
            "resources": resources,
            "resource_samples": str(self.resource_monitor.out),
            "server_logs": server_logs,
            "final_log_seal_stops": log_seal_stops,
            "final_audit_evidence": final_audit_evidence,
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
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--account-count", type=int, default=4)
    parser.add_argument("--round-ops", type=int, default=250)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--session-pool", type=int, default=4)
    parser.add_argument("--browser-interval-seconds", type=int, default=3 * 60 * 60)
    parser.add_argument("--resource-interval", type=float, default=2.0)
    parser.add_argument("--heartbeat-interval", type=float, default=30.0)
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
    parser.add_argument("--auth-socket", default="", help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-start-ticks", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-boot-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-cgroup", default="", help=argparse.SUPPRESS)
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
    expected_liveness_path = control_root / "checkpoint" / "watchdog.liveness.json"
    if Path(str(contract.get("watchdog_liveness_path") or "")).resolve(strict=False) != expected_liveness_path:
        errors.append("watchdog_liveness_path")
    runner_auth_key = getattr(args, "_runner_auth_key", None)
    control_auth_evidence = getattr(args, "_control_auth_evidence", None)
    contract_session_hash = str(contract.get("runner_auth_key_sha256") or "")
    watchdog_session_hash = str(contract.get("watchdog_auth_key_sha256") or "")
    if (
        not isinstance(runner_auth_key, (bytes, bytearray))
        or len(runner_auth_key) != 32
        or len(contract_session_hash) != 64
        or hashlib.sha256(bytes(runner_auth_key)).hexdigest() != contract_session_hash
        or not isinstance(control_auth_evidence, Mapping)
        or control_auth_evidence.get("session_secret_sha256") != contract_session_hash
        or len(watchdog_session_hash) != 64
        or watchdog_session_hash == contract_session_hash
        or contract.get("role_separated_auth_keys") is not True
    ):
        errors.append("runner_auth_session_binding")
    supervisor_identity = contract.get("supervisor_identity")
    expected_supervisor_identity = {
        "pid": int(getattr(args, "supervisor_pid", 0) or 0),
        "start_ticks": int(getattr(args, "supervisor_start_ticks", 0) or 0),
        "boot_id": str(getattr(args, "supervisor_boot_id", "") or ""),
        "cgroup_path": str(getattr(args, "supervisor_cgroup", "") or ""),
    }
    authenticated_server = (
        control_auth_evidence.get("server_process")
        if isinstance(control_auth_evidence, Mapping)
        else None
    )
    if (
        not isinstance(supervisor_identity, Mapping)
        or dict(supervisor_identity) != expected_supervisor_identity
        or not isinstance(authenticated_server, Mapping)
        or any(
            authenticated_server.get(name) != value
            for name, value in expected_supervisor_identity.items()
        )
        or control_auth_evidence.get("server_identity_verified") is not True
    ):
        errors.append("supervisor_process_identity_binding")
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
    comfyui_backend = contract.get("comfyui_backend")
    if level in {"rehearsal", "soak", "formal"}:
        if not isinstance(comfyui_backend, Mapping):
            errors.append("comfyui_backend")
            comfyui_backend = {}
        backend_pid = comfyui_backend.get("backend_pid")
        backend_start_ticks = comfyui_backend.get("backend_start_ticks")
        backend_boot_id = str(comfyui_backend.get("backend_boot_id") or "")
        launcher_pid = comfyui_backend.get("launcher_pid")
        backend_process_group = comfyui_backend.get("process_group")
        backend_url = str(comfyui_backend.get("api_url") or "")
        backend_models = str(comfyui_backend.get("models_root") or "")
        managed_leaf = (
            comfyui_backend.get("managed_leaf")
            if isinstance(comfyui_backend.get("managed_leaf"), Mapping)
            else {}
        )
        backend_leaf_cgroup = str(managed_leaf.get("cgroup_path") or "")
        expected_leaf_cgroup = f"{expected_cgroup.rstrip('/')}/comfyui"
        if (
            comfyui_backend.get("status") != "ready"
            or comfyui_backend.get("ok") is not True
            or comfyui_backend.get("actual_execution") is not True
            or comfyui_backend.get("simulated") is not False
            or comfyui_backend.get("adopted_external_pid") is not False
            or isinstance(backend_pid, bool)
            or not isinstance(backend_pid, int)
            or backend_pid <= 0
            or isinstance(backend_start_ticks, bool)
            or not isinstance(backend_start_ticks, int)
            or backend_start_ticks <= 0
            or not backend_boot_id
            or isinstance(launcher_pid, bool)
            or not isinstance(launcher_pid, int)
            or launcher_pid <= 0
            or backend_process_group != launcher_pid
            or backend_leaf_cgroup != expected_leaf_cgroup
            or "delegated" in managed_leaf
            or managed_leaf.get("subtree_controllers_enabled") is not False
            or managed_leaf.get("descendant_cgroups") != 0
            or managed_leaf.get("host_leaf_state_before_sandbox")
            != "pending_sandbox"
            or managed_leaf.get("workload_delegation_capability") is not False
            or managed_leaf.get("ok") is not True
            or str(comfyui_backend.get("backend_cgroup") or "")
            != backend_leaf_cgroup
        ):
            errors.append("comfyui_backend_identity")
        if (
            os.environ.get("HACKME_CAMPAIGN_COMFYUI_API_URL") != backend_url
            or os.environ.get("HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT") != backend_models
            or os.environ.get("HACKME_CAMPAIGN_COMFYUI_BACKEND_PID") != str(backend_pid)
        ):
            errors.append("comfyui_backend_environment")
        for name in ("ready_receipt", "lifecycle_path", "stdout_path"):
            raw = str(comfyui_backend.get(name) or "")
            resolved = Path(raw).resolve(strict=False) if raw else Path("/missing")
            if (
                not raw
                or raw != str(resolved)
                or resolved == control_root
                or control_root not in resolved.parents
            ):
                errors.append(f"comfyui_backend_path:{name}")
        ready_path = Path(
            str(comfyui_backend.get("ready_receipt") or "/missing")
        ).resolve(strict=False)
        try:
            ready_payload, actual_receipt_identity = (
                read_stable_ready_receipt(ready_path)
            )
        except Exception:
            errors.append("comfyui_backend_ready_receipt")
            ready_payload = {}
            actual_receipt_identity = {}
        ready_process = (
            ready_payload.get("process")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("process"), Mapping)
            else {}
        )
        ready_listener = (
            ready_payload.get("listener")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("listener"), Mapping)
            else {}
        )
        ready_readiness = (
            ready_payload.get("readiness")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("readiness"), Mapping)
            else {}
        )
        ready_models_binding = (
            ready_payload.get("models_binding")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("models_binding"), Mapping)
            else {}
        )
        ready_leaf = (
            ready_payload.get("managed_leaf")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("managed_leaf"), Mapping)
            else {}
        )
        ready_leaf_state = (
            ready_payload.get("managed_leaf_state")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("managed_leaf_state"), Mapping)
            else {}
        )
        ready_placement = (
            ready_payload.get("placement")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("placement"), Mapping)
            else {}
        )
        ready_confinement = (
            ready_payload.get("confinement")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("confinement"), Mapping)
            else {}
        )
        ready_sandbox = (
            ready_payload.get("sandbox")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("sandbox"), Mapping)
            else {}
        )
        ready_sandbox_live = (
            ready_payload.get("sandbox_live")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("sandbox_live"), Mapping)
            else {}
        )
        ready_launcher = (
            ready_payload.get("launcher")
            if isinstance(ready_payload, Mapping)
            and isinstance(ready_payload.get("launcher"), Mapping)
            else {}
        )
        receipt_identity = (
            comfyui_backend.get("ready_receipt_identity")
            if isinstance(
                comfyui_backend.get("ready_receipt_identity"),
                Mapping,
            )
            else {}
        )
        backend_python = str(comfyui_backend.get("python_executable") or "")
        backend_main = str(comfyui_backend.get("main_path") or "")
        backend_working = str(comfyui_backend.get("working_root") or "")
        backend_command = comfyui_backend.get("command")
        try:
            backend_host = str(urlsplit(backend_url).hostname or "")
            backend_port = urlsplit(backend_url).port
        except ValueError:
            backend_host = ""
            backend_port = None
        expected_backend_command = [
            backend_python,
            backend_main,
            "--listen",
            backend_host,
            "--port",
            str(backend_port),
            "--disable-auto-launch",
        ]
        expected_command_sha256 = hashlib.sha256(
            b"\0".join(value.encode() for value in expected_backend_command)
        ).hexdigest()
        ready_owner_pids = ready_listener.get("owner_pids")
        ready_leaf_pids = ready_leaf_state.get("pids")
        ready_environment_keys = ready_payload.get("environment_keys")
        sandbox_launcher = ready_confinement.get("launcher")
        sandbox_transition = ready_confinement.get("host_transition")
        sandbox_mounts = ready_confinement.get("mounts")
        sandbox_privileges = ready_confinement.get("privileges")
        sandbox_denial = ready_confinement.get("cgroup_write_denial")
        sandbox_delegation = ready_confinement.get(
            "workload_delegation_confinement"
        )
        ready_process_capabilities = (
            ready_process.get("capability_sets")
            if isinstance(ready_process.get("capability_sets"), Mapping)
            else {}
        )
        sandbox_capabilities = (
            sandbox_privileges.get("capability_sets")
            if isinstance(sandbox_privileges, Mapping)
            and isinstance(sandbox_privileges.get("capability_sets"), Mapping)
            else {}
        )
        sandbox_seccomp = (
            sandbox_privileges.get("seccomp")
            if isinstance(sandbox_privileges, Mapping)
            and isinstance(sandbox_privileges.get("seccomp"), Mapping)
            else {}
        )
        required_capability_names = {
            "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"
        }
        if (
            actual_receipt_identity.get("sha256")
            != str(comfyui_backend.get("ready_receipt_sha256") or "")
            or receipt_identity.get("sha256")
            != str(comfyui_backend.get("ready_receipt_sha256") or "")
            or dict(receipt_identity) != dict(actual_receipt_identity)
            or not isinstance(ready_payload, Mapping)
            or ready_payload.get("schema_version")
            != COMFYUI_BACKEND_READY_SCHEMA_VERSION
            or ready_payload.get("ok") is not True
            or ready_payload.get("actual_execution") is not True
            or ready_payload.get("simulated") is not False
            or ready_payload.get("adopted_external_pid") is not False
            or ready_payload.get("api_url") != backend_url
            or ready_payload.get("python_executable") != backend_python
            or ready_payload.get("main_path") != backend_main
            or ready_payload.get("working_root") != backend_working
            or ready_payload.get("models_root") != backend_models
            or backend_command != expected_backend_command
            or ready_payload.get("command") != expected_backend_command
            or comfyui_backend.get("command_sha256")
            != expected_command_sha256
            or ready_payload.get("command_sha256")
            != expected_command_sha256
            or ready_process.get("pid") != backend_pid
            or ready_process.get("start_ticks") != backend_start_ticks
            or ready_process.get("boot_id") != backend_boot_id
            or ready_process.get("cgroup_path") != backend_leaf_cgroup
            or ready_process.get("cwd") != backend_working
            or ready_process.get("executable") != backend_python
            or ready_process.get("process_group") != backend_process_group
            or ready_process.get("no_new_privileges") is not True
            or ready_process.get("seccomp_mode") != 2
            or set(ready_process_capabilities) != required_capability_names
            or not all(
                value == "0000000000000000"
                for value in ready_process_capabilities.values()
            )
            or not isinstance(ready_process.get("namespace_pids"), list)
            or len(ready_process.get("namespace_pids") or []) < 2
            or (ready_process.get("namespace_pids") or [None])[0] != backend_pid
            or ready_process.get("ok") is not True
            or ready_launcher.get("pid") != launcher_pid
            or ready_launcher.get("process_group") != backend_process_group
            or ready_launcher.get("session") != backend_process_group
            or ready_launcher.get("ok") is not True
            or ready_placement.get("pid") != backend_pid
            or ready_placement.get("start_ticks") != backend_start_ticks
            or ready_placement.get("campaign_cgroup") != backend_leaf_cgroup
            or ready_placement.get("ok") is not True
            or ready_models_binding.get("entry_path") != backend_models
            or ready_models_binding.get("realpath") != backend_models
            or ready_models_binding.get("symlink") is not False
            or ready_models_binding.get("ok") is not True
            or not isinstance(ready_process.get("models_binding"), Mapping)
            or dict(ready_process.get("models_binding") or {})
            != dict(ready_models_binding)
            or ready_leaf.get("cgroup_path") != backend_leaf_cgroup
            or "delegated" in ready_leaf
            or ready_leaf.get("subtree_controllers_enabled") is not False
            or ready_leaf.get("descendant_cgroups") != 0
            or ready_leaf.get("host_leaf_state_before_sandbox")
            != "pending_sandbox"
            or ready_leaf.get("workload_delegation_capability") is not False
            or ready_leaf.get("ok") is not True
            or not isinstance(ready_leaf.get("device"), int)
            or not isinstance(ready_leaf.get("inode"), int)
            or not isinstance(ready_leaf_pids, list)
            or not all(
                isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
                for pid in (ready_leaf_pids or [])
            )
            or backend_pid not in (ready_leaf_pids or [])
            or ready_leaf_state.get("cgroup_path") != backend_leaf_cgroup
            or ready_leaf_state.get("populated") != 1
            or ready_leaf_state.get("consistent") is not True
            or ready_leaf_state.get("topology_intact") is not True
            or ready_leaf_state.get("descendant_cgroups") != 0
            or ready_leaf_state.get("subtree_control") != []
            or ready_leaf_state.get("workload_delegation_capability") is not False
            or ready_leaf_state.get("ok") is not True
            or comfyui_backend.get("confinement") != ready_confinement
            or comfyui_backend.get("sandbox") != ready_confinement
            or ready_sandbox != ready_confinement
            or ready_confinement.get("schema_version")
            != SANDBOX_PROOF_SCHEMA_VERSION
            or ready_confinement.get("actual_execution") is not True
            or ready_confinement.get("simulated") is not False
            or ready_confinement.get("adopted_external_process") is not False
            or ready_confinement.get("shell") is not False
            or ready_confinement.get("fixed_command")
            != expected_backend_command
            or ready_confinement.get("expected_host_cgroup_path")
            != backend_leaf_cgroup
            or not isinstance(sandbox_launcher, Mapping)
            or sandbox_launcher.get("host_pid") != launcher_pid
            or sandbox_launcher.get("host_process_group")
            != backend_process_group
            or sandbox_launcher.get("host_session") != backend_process_group
            or not isinstance(sandbox_transition, Mapping)
            or sandbox_transition.get("schema_version")
            != HOST_TRANSITION_SCHEMA_VERSION
            or sandbox_transition.get("pid") != launcher_pid
            or sandbox_transition.get("cgroup_path") != backend_leaf_cgroup
            or sandbox_transition.get("ok") is not True
            or not isinstance(sandbox_mounts, Mapping)
            or sandbox_mounts.get("cgroup_namespace_path") != "/"
            or sandbox_mounts.get("leaf_kernel_objects_match") is not True
            or sandbox_mounts.get("ok") is not True
            or not isinstance(sandbox_privileges, Mapping)
            or set(sandbox_capabilities) != required_capability_names
            or not all(
                value == "0000000000000000"
                for value in sandbox_capabilities.values()
            )
            or sandbox_privileges.get("securebits_locked") is not True
            or sandbox_privileges.get("no_new_privileges") is not True
            or sandbox_seccomp.get("mode") != 2
            or not isinstance(sandbox_denial, Mapping)
            or sandbox_denial.get("write_open_succeeded") is not False
            or sandbox_denial.get("errno") not in {1, 13, 30}
            or sandbox_denial.get("ok") is not True
            or ready_confinement.get("workload_delegation_capability") is not False
            or not isinstance(sandbox_delegation, Mapping)
            or sandbox_delegation.get("workload_delegation_capability") is not False
            or sandbox_delegation.get("namespace_rooted_cgroup2") is not True
            or sandbox_delegation.get("cgroup2_read_only") is not True
            or sandbox_delegation.get("capability_sets_zero") is not True
            or sandbox_delegation.get("namespace_and_mount_syscalls_denied") is not True
            or sandbox_delegation.get("ok") is not True
            or ready_confinement.get("proof_written_before_exec") is not True
            or ready_confinement.get("outer_launcher_preserves_process_group")
            is not True
            or ready_confinement.get("reaper_preserves_wait_status") is not True
            or ready_confinement.get("ok") is not True
            or ready_sandbox_live.get("launcher_pid") != launcher_pid
            or ready_sandbox_live.get("backend_host_pid") != backend_pid
            or ready_sandbox_live.get("process_group") != backend_process_group
            or ready_sandbox_live.get("workload_delegation_capability") is not False
            or ready_sandbox_live.get("ok") is not True
            or ready_listener.get("family") != "ipv4"
            or ready_listener.get("address") != backend_host
            or ready_listener.get("port") != backend_port
            or not isinstance(ready_listener.get("socket_inode"), int)
            or not isinstance(ready_owner_pids, list)
            or not ready_owner_pids
            or not all(
                isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
                for pid in (ready_owner_pids or [])
            )
            or not set(ready_owner_pids) <= set(ready_leaf_pids or [])
            or ready_listener.get("loopback_only") is not True
            or ready_listener.get("ok") is not True
            or ready_payload.get("listener_stable_across_readiness") is not True
            or ready_readiness.get("endpoint")
            != f"{backend_url}/system_stats"
            or "python_version"
            not in set(ready_readiness.get("system_fields") or [])
            or not isinstance(ready_readiness.get("device_count"), int)
            or ready_readiness.get("device_count") <= 0
            or ready_readiness.get("ok") is not True
            or not isinstance(ready_environment_keys, list)
            or "HACKME_CAMPAIGN_COMFYUI_INSTANCE_ID"
            not in ready_environment_keys
        ):
            errors.append("comfyui_backend_ready_authority")
        try:
            live_backend_authority = validate_live_comfyui_backend_authority(
                comfyui_backend,
                ready_payload,
            )
        except Exception:
            live_backend_authority = {}
        if live_backend_authority.get("ok") is not True:
            errors.append("comfyui_backend_live_authority")
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
    expected_source_evidence_mode = (
        METADATA_CONTENT_EVIDENCE if level == "smoke" else FULL_CONTENT_EVIDENCE
    )
    if source_freeze.get("content_evidence_mode") != expected_source_evidence_mode:
        errors.append("source_content_evidence_mode")
    if level == "formal" and source_freeze.get("require_clean") is not True:
        errors.append("formal_source_not_clean")
    gates = contract.get("gates")
    if not isinstance(gates, Mapping):
        errors.append("supervisor_gates")
        gates = {}
    required_gates = {
        "authenticated_control_channel_verified",
        "runner_control_channel_authenticated",
        "watchdog_reciprocal_liveness_verified",
        "runner_import_staged_verified",
        "watchdog_import_staged_verified",
        "host_safety_runner_import_settled",
        "host_safety_state_initialization_settled",
        "host_safety_activation_verified",
        "cgroup_limits_verified",
        "external_watchdog_verified",
        "runner_and_watchdog_placement_verified",
        "cgroup_event_baseline_verified",
    }
    if level in {"rehearsal", "soak", "formal"}:
        required_gates.add("comfyui_backend_lifecycle_verified")
        required_gates.add("host_safety_backend_startup_settled")
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
        "auth_socket": args.auth_socket,
        "supervisor_pid": args.supervisor_pid,
        "supervisor_start_ticks": args.supervisor_start_ticks,
        "supervisor_boot_id": args.supervisor_boot_id,
        "supervisor_cgroup": args.supervisor_cgroup,
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
    deadline = time.monotonic() + RUNNER_SUPERVISOR_ACTIVATION_TIMEOUT_SECONDS
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
    raise RuntimeError(
        "supervisor did not release the campaign runner within "
        f"{int(RUNNER_SUPERVISOR_ACTIVATION_TIMEOUT_SECONDS)} seconds"
    )


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
    if args.supervised:
        try:
            authentication = send_hello(
                Path(args.auth_socket),
                campaign_uuid=str(args.campaign_uuid),
                role="runner",
                require_session_secret=True,
                timeout=10.0,
                expected_server_peer=PeerIdentity(
                    int(args.supervisor_pid),
                    os.getuid(),
                    os.getgid(),
                ),
                expected_server_process={
                    "pid": int(args.supervisor_pid),
                    "start_ticks": int(args.supervisor_start_ticks),
                    "boot_id": str(args.supervisor_boot_id),
                    "cgroup_path": str(args.supervisor_cgroup),
                },
            )
            if not isinstance(authentication, tuple):
                raise RuntimeError("runner control handshake did not deliver a session key")
            auth_evidence, runner_auth_key = authentication
            if auth_evidence.get("session_secret_received") is not True:
                raise RuntimeError("runner control session proof is incomplete")
            setattr(args, "_runner_auth_key", runner_auth_key)
            setattr(args, "_control_auth_evidence", auth_evidence)
        except Exception as exc:
            raise SystemExit(f"runner authentication failed: {exc}") from exc
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
