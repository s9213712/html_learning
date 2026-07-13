#!/usr/bin/env python3
"""Build and validate raw-authority-bound formal qualification bundles.

Gate evidence is an index, never an assertion authority.  A PASS is derived
from immutable references to gate-specific native artifacts.  Hand-written
``assertions``/``measurements`` summaries, component probes, simulations, and
raw-artifact reuse fail closed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import sqlite3
import stat
import tarfile
import tempfile
import time
import unicodedata
from typing import Any, Callable, Mapping, Sequence

try:
    from scripts.testing.campaign_dependency_preflight import (
        BACKUP_RESTORE_MANIFEST_SCHEMA_VERSION,
        BACKUP_SNAPSHOT_MARKER_TABLE,
        BACKUP_SQLITE_CHECK_SCHEMA_VERSION,
        REVIEWED_BACKUP_SNAPSHOT_METHODS,
    )
    from scripts.testing.campaign_observability import ResourceCollector
    from scripts.testing.campaign_scenario_binding import (
        FORMAL_SCENARIO_BINDINGS,
        RUNTIME_RECEIPT_SCHEMA_VERSION,
        validate_scenario_runtime_receipt,
    )
    from scripts.testing.campaign_source_freeze import REVIEWED_PROTECTED_IGNORED_PATHS
except ModuleNotFoundError:  # Direct ``python scripts/testing/...`` execution.
    from campaign_dependency_preflight import (
        BACKUP_RESTORE_MANIFEST_SCHEMA_VERSION,
        BACKUP_SNAPSHOT_MARKER_TABLE,
        BACKUP_SQLITE_CHECK_SCHEMA_VERSION,
        REVIEWED_BACKUP_SNAPSHOT_METHODS,
    )
    from campaign_observability import ResourceCollector
    from campaign_scenario_binding import (
        FORMAL_SCENARIO_BINDINGS,
        RUNTIME_RECEIPT_SCHEMA_VERSION,
        validate_scenario_runtime_receipt,
    )
    from campaign_source_freeze import REVIEWED_PROTECTED_IGNORED_PATHS


GATE_BUNDLE_SCHEMA_VERSION = "hackme.harness-gate-bundle.v3"
GATE_EVIDENCE_SCHEMA_VERSION = "hackme.formal-gate-evidence.v3"
GATE_ARTIFACT_REFERENCE_SCHEMA_VERSION = "hackme.formal-gate-attempt-reference.v4"
RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION = "hackme.formal-raw-artifact-reference.v1"
RAW_ARTIFACT_BINDING_SCHEMA_VERSION = "hackme.formal-raw-artifact-binding.v1"
QUALIFICATION_ATTEMPT_SCHEMA_VERSION = "hackme.formal-qualification-attempt.v2"
NATIVE_EXECUTION_RECEIPT_SCHEMA_VERSION = "hackme.formal-native-execution.v1"
CAPTURE_PRODUCER_KIND = "qualification_capture_writer"
NATIVE_PRODUCER_KIND = "qualification_native_runner"

REQUIRED_FORMAL_GATES = (
    "cgroup_limits_verified",
    "external_watchdog_verified",
    "hard_stop_injection_verified",
    "checkpoint_recovery_verified",
    "source_drift_detection_verified",
    "sample_schema_completeness_verified",
    "production_security_sentinel_verified",
    "all_mandatory_dependencies_verified",
    "180_second_smoke_passed",
    "60_minute_rehearsal_passed",
    "worktree_clean_and_frozen",
)

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUIDISH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_FUTURE_SKEW = timedelta(minutes=5)
_GIB = 1024**3
_MIB = 1024**2
_MAX_CONTROL_JSON_BYTES = 64 * _MIB
_MAX_RAW_JSON_BYTES = 16 * _MIB
_MAX_RAW_NDJSON_BYTES = 128 * _MIB
_MAX_RAW_NDJSON_LINE_BYTES = 1 * _MIB
_MAX_RAW_NDJSON_ROWS = 100_000
_MAX_GATE_STRUCTURED_BYTES = 128 * _MIB
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 1_000_000
_MAX_JSON_STRING_BYTES = 4 * _MIB
_MAX_RAW_DECODED_NODES = 5_000_000
_MAX_GATE_DECODED_NODES = 5_000_000
_MAX_RAW_DECODED_STRING_BYTES = 256 * _MIB
_MAX_GATE_DECODED_STRING_BYTES = 256 * _MIB
_MAX_SMALL_NATIVE_BYTES = 64 * _MIB
_MAX_HLS_PLAYLIST_BYTES = 4 * _MIB
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * _GIB
_MAX_ARCHIVE_EXTENSION_PAYLOAD_BYTES = 1 * _MIB
_MAX_ARCHIVE_NAME_BYTES = 4096
_MAX_ARCHIVE_METADATA_BYTES = 64 * _MIB
_MAX_ARCHIVE_TRAILING_PADDING_BYTES = 1 * _MIB
_ARCHIVE_VALIDATION_TIMEOUT_SECONDS = 300.0
_SQLITE_VALIDATION_TIMEOUT_SECONDS = 30.0
_SQLITE_PROGRESS_OPCODES = 1000
_MINIMUM_FREE_RESERVE_BYTES = 20 * _GIB
_MAX_PNG_DIMENSION = 16_384
_MAX_PNG_PIXELS = 64_000_000
_PNG_VALIDATION_TIMEOUT_SECONDS = 30.0
_MAX_NATIVE_RECEIPT_AGE_SECONDS = 900.0
_EXECUTION_WALL_MONOTONIC_TOLERANCE_SECONDS = 5.0
_MAX_NATIVE_ARTIFACT_BYTES = 64 * _GIB
_NATIVE_ROLE_MAX_BYTES: Mapping[tuple[str, str], int] = {
    ("all_mandatory_dependencies_verified", "hls_playlist"): 4 * _MIB,
    ("all_mandatory_dependencies_verified", "hls_segment"): 512 * _MIB,
    ("all_mandatory_dependencies_verified", "comfyui_output"): 64 * _MIB,
    ("worktree_clean_and_frozen", "git_status"): 64 * _MIB,
    ("worktree_clean_and_frozen", "git_diff_binary"): 64 * _MIB,
    ("worktree_clean_and_frozen", "git_ls_files"): 64 * _MIB,
    ("worktree_clean_and_frozen", "git_submodule_status"): 64 * _MIB,
}
_PERSISTENT_CHECKPOINT_ROOT = (
    Path.home() / "logs" / "hackme_web_campaign_24h"
).resolve(strict=False)
_EXPECTED_LIMITS = {
    "memory.high": 7 * _GIB,
    "memory.max": 8 * _GIB,
    "memory.swap.max": 1 * _GIB,
    "cpu.quota_percent": 600,
    "pids.max": 768,
}
_MANDATORY_ROLES = {
    "primary", "recovery", "security_sentinel", "load_generator", "browser",
    "ffmpeg", "bt", "comfyui", "scenario",
}
_MANDATORY_DEPENDENCIES = {
    "browser_chromium", "browser_firefox", "browser_webkit", "ffmpeg_hls",
    "bt_seed_download", "comfyui_terminal", "ai_provider_terminal",
    "backup_restore", "production_security_sentinel",
}
_EXTERNAL_DEPENDENCIES = {
    "bt_receipt": "bt_seed_download",
    "comfyui_receipt": "comfyui_terminal",
    "ai_receipt": "ai_provider_terminal",
    "backup_receipt": "backup_restore",
    "security_receipt": "production_security_sentinel",
}
_MANDATORY_REHEARSAL_SCENARIOS = (
    "media_long_hls_share",
    "cloud_drive_share_stream",
    "bt_download_stream_restart",
    "ai_agent_positive_operations",
    "comfyui_real_workflows",
    "trading_background_custom_workflow",
    "pointschain_hft_invariants",
    "wallet_incident_governance",
    "backup_restore_restart",
    "server_emergency_incident",
    "media_proxy_cross_browser",
    "community_governance_operations",
    "final_ui_mobile_prelaunch",
)
_MANDATORY_REHEARSAL_FEATURES = {
    "planned_restart", "runtime_backup_restore", "comfyui_real_workflow",
    "bt_terminal_download", "cross_browser_mobile_ui",
}
_SECURITY_CHECKS = {
    "production_launcher_contract", "transport", "anonymous_root_denied",
    "login_missing_csrf_denied", "root_login", "manager_login", "user_login",
    "production_mode_active", "manager_root_boundary_denied",
    "user_root_boundary_denied", "authenticated_missing_csrf_denied",
    "dangerous_confirmation_required", "production_security_controls",
    "audit_log_chain", "cross_worker_session_consistency",
}
_PROTECTED_ENTRY_SCHEMA_VERSION = "hackme.source-protected-ignored-entry/v1"
_PROTECTED_ENTRY_FIELDS = {
    "path", "kind", "working_sha256", "symlink_target", "filesystem_mode",
    "size", "mtime_ns", "ctime_ns", "inode", "device",
}
_RESOURCE_PROCESS_METRICS = (
    "process_count", "rss_bytes", "threads", "fd_count", "cpu_ticks",
)
_FORMAL_RESOURCE_ALWAYS_REQUIRED = frozenset({
    "gpu.0.utilization_percent",
    "gpu.0.memory_used_mib",
    "gpu.0.memory_total_mib",
    "gpu.0.temperature_c",
    "comfyui_queue.running",
    "comfyui_queue.pending",
    "comfyui_queue.status",
    *(
        f"health.{target}.{metric}"
        for target in ("primary", "recovery", "security_sentinel")
        for metric in ("status_code", "latency_ms", "semantic_ready")
    ),
})
_DEPENDENCY_RAW_SCHEMAS = {
    "browser_launch": "hackme.browser-launch-observation/v1",
    "ai_exchange": "hackme.ai-provider-exchange/v1",
    "bt_trace": "hackme.bt-protocol-trace/v1",
    "comfyui_history": "hackme.comfyui-history-observation/v1",
    "hls_ffprobe": "hackme.ffprobe-media-observation/v1",
    "backup_manifest": BACKUP_RESTORE_MANIFEST_SCHEMA_VERSION,
    "backup_quick_check": BACKUP_SQLITE_CHECK_SCHEMA_VERSION,
    "security_request": "hackme.security-request-observation/v1",
    "security_audit": "hackme.security-audit-entry/v1",
}
_SECURITY_RAW_REQUEST_CASES: Mapping[str, set[int]] = {
    "root_login_success": {200},
    "anonymous_root_denied": {401, 403},
    "login_missing_csrf_denied": {400, 403, 419},
    "manager_root_boundary_denied": {403},
    "user_root_boundary_denied": {403},
    "authenticated_missing_csrf_denied": {400, 403, 419},
    "dangerous_confirmation_required": {400},
    "cross_worker_session_success": {200},
}
_SECURITY_AUDIT_EVENTS = {
    "login_success", "csrf_denied", "rbac_denied", "confirmation_denied",
}


class GateBundleError(RuntimeError):
    """A bundle or referenced authority is not formal-ready."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GateBundleError(f"{label} must be a non-empty UTC timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise GateBundleError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise GateBundleError(f"{label} must include the UTC offset")
    return parsed.astimezone(timezone.utc)


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def protected_source_identity_digest(
    manifest_digest: str,
    content_digest: str,
) -> str:
    """Combine metadata and content authority for reviewed ignored inputs."""

    manifest = str(manifest_digest or "").lower()
    content = str(content_digest or "").lower()
    _require(_SHA256.fullmatch(manifest) is not None, "protected manifest digest is not lowercase SHA-256")
    _require(_SHA256.fullmatch(content) is not None, "protected content digest is not lowercase SHA-256")
    return sha256_bytes(canonical_json_bytes({
        "protected_ignored_manifest_digest": manifest,
        "protected_ignored_content_digest": content,
    }))


def bundle_sha256(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("bundle_sha256", None)
    return sha256_bytes(canonical_json_bytes(unsigned))


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise GateBundleError(message)


def _exact_canonical_path(
    path: Path | str,
    *,
    label: str,
    must_exist: bool,
) -> Path:
    """Keep one lexical path authority and reject aliases before validation.

    ``Path.resolve()`` is used only as a comparison oracle.  The returned path
    is always the caller's original canonical lexical path, so a descriptor
    pinned from that name can never be rebound to a different resolved name.
    """

    try:
        raw_path = os.fspath(path)
    except TypeError as exc:
        raise GateBundleError(f"{label} must be a filesystem path") from exc
    _require(
        isinstance(raw_path, str) and bool(raw_path),
        f"{label} must be a non-empty path string",
    )
    candidate = Path(raw_path)
    _require(candidate.is_absolute(), f"{label} must be an absolute path")
    _require(
        raw_path == str(candidate),
        f"{label} must use its exact canonical absolute path string",
    )
    try:
        leaf = candidate.lstat()
    except FileNotFoundError:
        _require(not must_exist, f"{label} does not exist")
    except OSError as exc:
        raise GateBundleError(
            f"cannot inspect {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    else:
        _require(not stat.S_ISLNK(leaf.st_mode), f"{label} must not be a symlink")
        if must_exist:
            _require(stat.S_ISREG(leaf.st_mode), f"{label} is not a regular file")
    try:
        resolved = candidate.resolve(strict=must_exist)
    except Exception as exc:
        raise GateBundleError(
            f"cannot canonicalize {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(
        resolved == candidate,
        f"{label} must use its exact canonical absolute path string",
    )
    return candidate


def _object(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateBundleError(f"{label} must be a JSON object")
    return dict(value)


def _number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateBundleError(f"{label} must be numeric")
    result = float(value)
    _require(math.isfinite(result), f"{label} must be finite")
    return result


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GateBundleError(f"{label} must be a positive integer")
    return value


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        candidate = _exact_canonical_path(
            path,
            label=label,
            must_exist=True,
        )
        content, identity = _stable_read(
            candidate,
            label=label,
            maximum_bytes=_MAX_CONTROL_JSON_BYTES,
        )
        value = json.loads(content.decode("utf-8", errors="strict"))
    except Exception as exc:
        raise GateBundleError(f"cannot reparse {label}: {exc.__class__.__name__}: {exc}") from exc
    result = _object(value, label=label)
    _validate_json_shape(result, label=label)
    final_content, final_identity = _stable_read(
        candidate,
        label=f"{label} final readback",
        maximum_bytes=_MAX_CONTROL_JSON_BYTES,
    )
    _require(
        _stat_identity(final_identity) == _stat_identity(identity)
        and final_content == content,
        f"{label} changed during validation",
    )
    return result


def _native(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("formal_binding", None)
    return result


def _nested_get(payload: Mapping[str, Any], dotted: str) -> Any:
    value: Any = payload
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _valid_sample_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return not isinstance(value, float) or value == value


def _validate_json_shape(value: Any, *, label: str) -> tuple[int, int]:
    """Bound nested JSON work after decode and reject non-finite numbers."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    string_bytes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        _require(nodes <= _MAX_JSON_NODES, f"{label} JSON node count exceeds limit")
        _require(depth <= _MAX_JSON_DEPTH, f"{label} JSON nesting exceeds limit")
        if isinstance(current, str):
            encoded_length = len(current.encode("utf-8", errors="surrogatepass"))
            _require(
                encoded_length <= _MAX_JSON_STRING_BYTES,
                f"{label} JSON string exceeds limit",
            )
            string_bytes += encoded_length
        elif isinstance(current, float):
            _require(math.isfinite(current), f"{label} contains a non-finite number")
        elif isinstance(current, Mapping):
            for key, child in current.items():
                _require(isinstance(key, str), f"{label} JSON object key is not text")
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        else:
            _require(
                current is None or isinstance(current, (bool, int)),
                f"{label} contains a non-JSON value",
            )
    return nodes, string_bytes


@dataclass(frozen=True)
class RawSpec:
    media_type: str
    content_schema_version: str
    allow_empty: bool = False


def _scenario_specs() -> dict[str, RawSpec]:
    return {
        f"scenario_{scenario_id}": RawSpec(
            "application/json", RUNTIME_RECEIPT_SCHEMA_VERSION
        )
        for scenario_id in _MANDATORY_REHEARSAL_SCENARIOS
    }


GATE_RAW_SPECS: Mapping[str, Mapping[str, RawSpec]] = {
    "cgroup_limits_verified": {
        "cgroup_readback": RawSpec("application/json", "hackme.campaign-cgroup/v1"),
        "pid_placement": RawSpec("application/json", "hackme.campaign-cgroup-placement-set/v1"),
    },
    "external_watchdog_verified": {
        "watchdog_startup": RawSpec("application/json", "hackme.campaign-watchdog.v1"),
        "watchdog_incident": RawSpec("application/json", "hackme.campaign-watchdog.v1"),
        "watchdog_terminal": RawSpec("application/json", "hackme.campaign-watchdog.v1"),
    },
    "hard_stop_injection_verified": {
        "state_before": RawSpec("application/json", "hackme.campaign-state.v1"),
        "state_after": RawSpec("application/json", "hackme.campaign-state.v1"),
        "control_after": RawSpec("application/json", "hackme.campaign-control.v1"),
        "cgroup_stop": RawSpec("application/json", "hackme.campaign-cgroup-stop/v1"),
    },
    "checkpoint_recovery_verified": {
        "checkpoint_before": RawSpec("application/json", "hackme.campaign-checkpoint.v1"),
        "checkpoint_primary": RawSpec("application/json", "hackme.campaign-checkpoint.v1"),
        "checkpoint_mirror": RawSpec("application/json", "hackme.campaign-checkpoint.v1"),
        "tamper_rejection": RawSpec("application/json", "hackme.campaign-checkpoint-tamper-trial/v1"),
    },
    "source_drift_detection_verified": {
        "source_h0": RawSpec("application/json", "hackme.source-freeze.v3"),
        "drift_incident": RawSpec("application/json", "hackme.source-drift.v4"),
        "source_h24": RawSpec("application/json", "hackme.source-freeze.v3"),
        "source_restored": RawSpec("application/json", "hackme.source-freeze.v3"),
        "terminal_state": RawSpec("application/json", "hackme.campaign-state.v1"),
    },
    "sample_schema_completeness_verified": {
        "resource_samples": RawSpec("application/x-ndjson", "hackme.resource-sample.v1"),
        "negative_collector_trials": RawSpec("application/json", "hackme.resource-negative-trials/v1"),
    },
    "production_security_sentinel_verified": {
        "security_sentinel": RawSpec("application/json", "hackme.production-security-sentinel.v1"),
    },
    "all_mandatory_dependencies_verified": {
        "dependency_preflight": RawSpec("application/json", "hackme.campaign.dependency-preflight/v1"),
        "browser_chromium_launch": RawSpec("application/json", _DEPENDENCY_RAW_SCHEMAS["browser_launch"]),
        "browser_firefox_launch": RawSpec("application/json", _DEPENDENCY_RAW_SCHEMAS["browser_launch"]),
        "browser_webkit_launch": RawSpec("application/json", _DEPENDENCY_RAW_SCHEMAS["browser_launch"]),
        "bt_receipt": RawSpec("application/json", "hackme.campaign.external-dependency-probe/v1"),
        "bt_protocol_trace": RawSpec("application/json", _DEPENDENCY_RAW_SCHEMAS["bt_trace"]),
        "comfyui_receipt": RawSpec("application/json", "hackme.campaign.external-dependency-probe/v1"),
        "comfyui_history": RawSpec("application/json", _DEPENDENCY_RAW_SCHEMAS["comfyui_history"]),
        "ai_receipt": RawSpec("application/json", "hackme.campaign.external-dependency-probe/v1"),
        "ai_provider_exchange": RawSpec("application/json", _DEPENDENCY_RAW_SCHEMAS["ai_exchange"]),
        "backup_receipt": RawSpec("application/json", "hackme.campaign.external-dependency-probe/v1"),
        "backup_restore_manifest": RawSpec("application/json", _DEPENDENCY_RAW_SCHEMAS["backup_manifest"]),
        "backup_sqlite_check": RawSpec("application/json", _DEPENDENCY_RAW_SCHEMAS["backup_quick_check"]),
        "security_receipt": RawSpec("application/json", "hackme.campaign.external-dependency-probe/v1"),
        "security_requests": RawSpec("application/x-ndjson", _DEPENDENCY_RAW_SCHEMAS["security_request"]),
        "security_audit_chain": RawSpec("application/x-ndjson", _DEPENDENCY_RAW_SCHEMAS["security_audit"]),
        "hls_playlist": RawSpec("application/vnd.apple.mpegurl", "native.hls-playlist/v1"),
        "hls_segment": RawSpec("video/mp2t", "native.hls-segment/v1"),
        "hls_ffprobe": RawSpec("application/json", _DEPENDENCY_RAW_SCHEMAS["hls_ffprobe"]),
        "bt_payload": RawSpec("application/octet-stream", "native.bt-payload/v1"),
        "comfyui_output": RawSpec("image/png", "native.png/v1"),
        "backup_archive": RawSpec("application/x-tar", "native.tar/v1"),
        "backup_restored_database": RawSpec("application/vnd.sqlite3", "native.sqlite3/v1"),
    },
    "180_second_smoke_passed": {
        "supervisor_result": RawSpec("application/json", "hackme.campaign-supervisor.v1"),
        "smoke_runner": RawSpec("application/json", "hackme.campaign-smoke-load.v2"),
    },
    "60_minute_rehearsal_passed": {
        "supervisor_result": RawSpec("application/json", "hackme.campaign-supervisor.v1"),
        "runner_result": RawSpec("application/json", "hackme.campaign-operational-result/v1"),
        **_scenario_specs(),
    },
    "worktree_clean_and_frozen": {
        "source_h0": RawSpec("application/json", "hackme.source-freeze.v3"),
        "git_status": RawSpec("text/plain", "native.git-status-porcelain-v1", allow_empty=True),
        "git_diff_binary": RawSpec("application/octet-stream", "native.git-diff-binary-v1", allow_empty=True),
        "git_ls_files": RawSpec("text/plain", "native.git-ls-files-stage-v1"),
        "git_submodule_status": RawSpec("text/plain", "native.git-submodule-status-v1", allow_empty=True),
        "tracked_manifest": RawSpec("application/x-ndjson", "hackme.source-tracked-entry/v1"),
        "protected_ignored_manifest": RawSpec("application/x-ndjson", _PROTECTED_ENTRY_SCHEMA_VERSION),
    },
}


@dataclass
class ValidationRegistry:
    bundle_path: Path | None = None
    evidence_paths: dict[Path, str] = field(default_factory=dict)
    raw_paths: dict[Path, tuple[str, str]] = field(default_factory=dict)
    artifact_ids: dict[str, tuple[str, str]] = field(default_factory=dict)
    fingerprints: dict[tuple[int, str], str] = field(default_factory=dict)

    def register_evidence(self, path: Path, gate: str) -> None:
        canonical = Path(path)
        _require(canonical != self.bundle_path, f"{gate} evidence cannot reuse the bundle file")
        _require(canonical not in self.raw_paths, f"{gate} evidence path reuses a raw artifact")
        owner = self.evidence_paths.get(canonical)
        _require(owner in {None, gate}, f"evidence path reused across gates: {owner} -> {gate}")
        self.evidence_paths[canonical] = gate

    def register_raw(self, *, path: Path, artifact_id: str, gate: str, role: str, size: int, sha: str) -> None:
        canonical = Path(path)
        _require(canonical != self.bundle_path, f"{gate}.{role} raw artifact reuses the bundle file")
        _require(canonical not in self.evidence_paths, f"{gate}.{role} raw artifact reuses gate evidence")
        owner = self.raw_paths.get(canonical)
        if owner is not None:
            raise GateBundleError(
                f"raw artifact path reused: {owner[0]}.{owner[1]} -> {gate}.{role}"
            )
        id_owner = self.artifact_ids.get(artifact_id)
        _require(id_owner is None, f"raw artifact id reused: {artifact_id}")
        fingerprint = (size, sha)
        fingerprint_gate = self.fingerprints.get(fingerprint)
        _require(
            fingerprint_gate in {None, gate},
            f"raw artifact content reused across gates: {fingerprint_gate} -> {gate}.{role}",
        )
        self.raw_paths[canonical] = (gate, role)
        self.artifact_ids[artifact_id] = (gate, role)
        self.fingerprints[fingerprint] = gate


@dataclass(frozen=True)
class RawArtifact:
    role: str
    path: Path
    reference: Mapping[str, Any]
    data: Any
    content_sha256: str
    size_bytes: int
    stat_identity: tuple[int, ...]
    bytes: bytes | None = None
    descriptor: int | None = None
    decoded_nodes: int = 0
    decoded_string_bytes: int = 0

    def require_bytes(self, *, maximum_bytes: int, label: str | None = None) -> bytes:
        if self.size_bytes > maximum_bytes:
            raise GateBundleError(
                f"{label or self.role} exceeds the bounded in-memory size limit"
            )
        if self.bytes is not None:
            return self.bytes
        if self.descriptor is None:
            content, current = _stable_read(
                self.path,
                label=label or self.role,
                maximum_bytes=maximum_bytes,
            )
        else:
            before = os.fstat(self.descriptor)
            _require(
                _stat_identity(before) == self.stat_identity,
                f"{label or self.role} pinned descriptor identity changed",
            )
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = int(before.st_size)
            while remaining > 0:
                block = os.read(self.descriptor, min(_MIB, remaining))
                _require(block, f"{label or self.role} pinned artifact was truncated")
                chunks.append(block)
                remaining -= len(block)
            current = os.fstat(self.descriptor)
            content = b"".join(chunks)
        _require(
            len(content) == self.size_bytes
            and sha256_bytes(content) == self.content_sha256
            and _stat_identity(current) == self.stat_identity,
            f"{label or self.role} changed after qualification capture",
        )
        return content

    def require_prefix(self, *, length: int, label: str | None = None) -> bytes:
        """Read a bounded prefix from the already-pinned opaque authority."""

        prefix_label = label or self.role
        _require(
            isinstance(length, int) and not isinstance(length, bool) and length > 0,
            f"{prefix_label} prefix length is invalid",
        )
        expected_length = min(self.size_bytes, length)
        if self.bytes is not None:
            prefix = self.bytes[:expected_length]
            _require(
                len(prefix) == expected_length,
                f"{prefix_label} captured prefix was truncated",
            )
            return prefix
        _require(
            self.descriptor is not None,
            f"{prefix_label} has no pinned descriptor for prefix validation",
        )
        before = os.fstat(self.descriptor)
        _require(
            _stat_identity(before) == self.stat_identity,
            f"{prefix_label} pinned descriptor identity changed",
        )
        chunks: list[bytes] = []
        offset = 0
        while offset < expected_length:
            block = os.pread(
                self.descriptor,
                expected_length - offset,
                offset,
            )
            _require(block, f"{prefix_label} pinned artifact was truncated")
            chunks.append(block)
            offset += len(block)
        after = os.fstat(self.descriptor)
        _require(
            _stat_identity(after) == self.stat_identity,
            f"{prefix_label} pinned descriptor identity changed",
        )
        prefix = b"".join(chunks)
        _require(
            len(prefix) == expected_length,
            f"{prefix_label} captured prefix was truncated",
        )
        return prefix

    def pinned_path(self) -> Path:
        _require(self.descriptor is not None, f"{self.role} has no pinned descriptor")
        return Path(f"/proc/self/fd/{self.descriptor}")

    def close(self) -> None:
        if self.descriptor is not None:
            try:
                os.close(self.descriptor)
            except OSError:
                pass


def _stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_uid),
        int(info.st_gid),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _stable_scan(
    path: Path,
    *,
    label: str,
    collect: bool,
    maximum_bytes: int | None = None,
) -> tuple[bytes | None, str, os.stat_result]:
    candidate = Path(path)
    try:
        path_before = candidate.lstat()
    except Exception as exc:
        raise GateBundleError(
            f"cannot inspect {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(stat.S_ISREG(path_before.st_mode), f"{label} is not a regular file")
    _require(path_before.st_nlink == 1, f"{label} must have exactly one hard link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except Exception as exc:
        raise GateBundleError(
            f"cannot securely open {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        _require(
            _stat_identity(opened) == _stat_identity(path_before),
            f"{label} changed before it was opened",
        )
        if maximum_bytes is not None:
            _require(
                opened.st_size <= maximum_bytes,
                f"{label} exceeds the bounded size limit",
            )
        remaining = int(opened.st_size)
        while remaining > 0:
            block = os.read(descriptor, min(_MIB, remaining))
            _require(block, f"{label} was truncated while it was being read")
            digest.update(block)
            total += len(block)
            remaining -= len(block)
            if collect:
                chunks.append(block)
        after_fd = os.fstat(descriptor)
        _require(total == opened.st_size, f"{label} read length changed")
        _require(
            _stat_identity(after_fd) == _stat_identity(opened),
            f"{label} changed while it was being read",
        )
    finally:
        os.close(descriptor)
    try:
        path_after = candidate.lstat()
    except Exception as exc:
        raise GateBundleError(
            f"cannot re-inspect {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(
        _stat_identity(path_after) == _stat_identity(opened),
        f"{label} path identity changed while it was being validated",
    )
    return (b"".join(chunks) if collect else None), digest.hexdigest(), opened


def _stable_read(
    path: Path,
    *,
    label: str,
    maximum_bytes: int = _MAX_CONTROL_JSON_BYTES,
) -> tuple[bytes, os.stat_result]:
    content, _digest, opened = _stable_scan(
        path,
        label=label,
        collect=True,
        maximum_bytes=maximum_bytes,
    )
    assert content is not None
    return content, opened


def _stable_hash(path: Path, *, label: str) -> tuple[str, os.stat_result]:
    _content, digest, opened = _stable_scan(
        path,
        label=label,
        collect=False,
    )
    return digest, opened


def _pin_stable_artifact(
    path: Path,
    *,
    expected: os.stat_result,
    label: str,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except Exception as exc:
        raise GateBundleError(
            f"cannot pin {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        _require(
            _stat_identity(opened) == _stat_identity(expected),
            f"{label} changed before its descriptor was pinned",
        )
        path_after = path.lstat()
        _require(
            _stat_identity(path_after) == _stat_identity(expected),
            f"{label} path changed while its descriptor was pinned",
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


_PROCESS_RECEIPT_FIELDS = {
    "kind", "pid", "start_ticks", "boot_id", "cgroup_path", "invocation_id",
}


def _validate_process_receipt(
    value: object,
    *,
    label: str,
    required_kind: str | None = None,
) -> dict[str, Any]:
    producer = _object(value, label=label)
    _require(set(producer) == _PROCESS_RECEIPT_FIELDS, f"{label} shape mismatch")
    kind = producer.get("kind")
    _require(isinstance(kind, str) and kind.strip(), f"{label}.kind is missing")
    if required_kind is not None:
        _require(kind == required_kind, f"{label}.kind is not the reviewed producer")
    _positive_int(producer.get("pid"), label=f"{label}.pid")
    _positive_int(producer.get("start_ticks"), label=f"{label}.start_ticks")
    _require(
        isinstance(producer.get("boot_id"), str) and bool(producer["boot_id"].strip()),
        f"{label}.boot_id is missing",
    )
    _require(
        isinstance(producer.get("cgroup_path"), str)
        and producer["cgroup_path"].startswith("/"),
        f"{label}.cgroup_path is invalid",
    )
    _require(
        _UUIDISH.fullmatch(str(producer.get("invocation_id") or "")) is not None,
        f"{label}.invocation_id is invalid",
    )
    return producer


def _validate_native_execution_receipt(
    value: object,
    *,
    gate: str,
    commit: str,
    source_digest: str,
    protected_source_digest: str,
    campaign_uuid: str,
    checked_at: datetime,
    expected_roles: set[str],
) -> dict[str, Any]:
    receipt = _object(value, label=f"{gate} native execution receipt")
    expected_fields = {
        "schema_version", "gate_name", "qualification_campaign_uuid",
        "commit", "source_digest", "protected_source_digest",
        "invocation_id", "activation_nonce", "actual_execution", "simulated",
        "component_only", "started_at", "finished_at",
        "started_monotonic_ns", "finished_monotonic_ns", "producer",
        "source_authority_sha256", "artifacts",
    }
    _require(set(receipt) == expected_fields, f"{gate} native execution receipt shape mismatch")
    _require(
        receipt.get("schema_version") == NATIVE_EXECUTION_RECEIPT_SCHEMA_VERSION,
        f"{gate} native execution receipt schema mismatch",
    )
    _require(receipt.get("gate_name") == gate, f"{gate} native execution gate mismatch")
    _require(
        receipt.get("qualification_campaign_uuid") == campaign_uuid
        and receipt.get("commit") == commit
        and receipt.get("source_digest") == source_digest
        and receipt.get("protected_source_digest") == protected_source_digest,
        f"{gate} native execution source/campaign binding mismatch",
    )
    invocation_id = str(receipt.get("invocation_id") or "")
    activation_nonce = str(receipt.get("activation_nonce") or "")
    _require(_UUIDISH.fullmatch(invocation_id) is not None, f"{gate} native invocation is invalid")
    _require(_UUIDISH.fullmatch(activation_nonce) is not None, f"{gate} activation nonce is invalid")
    _require(receipt.get("actual_execution") is True, f"{gate} native execution is not actual")
    _require(receipt.get("simulated") is False, f"{gate} native execution is simulated")
    _require(receipt.get("component_only") is False, f"{gate} native execution is component-only")
    producer = _validate_process_receipt(
        receipt.get("producer"),
        label=f"{gate} native execution producer",
        required_kind=NATIVE_PRODUCER_KIND,
    )
    _require(
        producer["invocation_id"] == invocation_id,
        f"{gate} native producer invocation mismatch",
    )
    started = parse_utc(receipt.get("started_at"), label=f"{gate}.native.started_at")
    finished = parse_utc(receipt.get("finished_at"), label=f"{gate}.native.finished_at")
    started_ns = receipt.get("started_monotonic_ns")
    finished_ns = receipt.get("finished_monotonic_ns")
    _require(
        type(started_ns) is int and type(finished_ns) is int
        and started_ns > 0 and finished_ns > started_ns,
        f"{gate} native monotonic boundary is invalid",
    )
    wall_seconds = (finished - started).total_seconds()
    monotonic_seconds = (finished_ns - started_ns) / 1_000_000_000.0
    _require(wall_seconds > 0, f"{gate} native wall-clock boundary is invalid")
    _require(
        abs(wall_seconds - monotonic_seconds)
        <= _EXECUTION_WALL_MONOTONIC_TOLERANCE_SECONDS,
        f"{gate} native wall/monotonic duration mismatch",
    )
    _require(finished <= checked_at + _FUTURE_SKEW, f"{gate} native execution ends after capture")
    _require(
        (checked_at - finished).total_seconds() <= _MAX_NATIVE_RECEIPT_AGE_SECONDS,
        f"{gate} native execution receipt is stale",
    )
    minimum_seconds = 180.0 if gate == "180_second_smoke_passed" else 3600.0 if gate == "60_minute_rehearsal_passed" else 0.0
    _require(
        monotonic_seconds >= minimum_seconds,
        f"{gate} native execution did not satisfy its measured duration boundary",
    )
    source_authority_sha = str(receipt.get("source_authority_sha256") or "")
    _require(_SHA256.fullmatch(source_authority_sha) is not None, f"{gate} native source authority SHA is invalid")
    artifacts = _object(receipt.get("artifacts"), label=f"{gate} native execution artifacts")
    _require(set(artifacts) == expected_roles, f"{gate} native execution artifact role set mismatch")
    return {
        "receipt": receipt,
        "producer": producer,
        "duration_seconds": monotonic_seconds,
        "source_authority_sha256": source_authority_sha,
        "artifacts": artifacts,
    }


def _validate_binding(
    payload: Mapping[str, Any], *, gate: str, role: str, commit: str,
    source_digest: str, protected_source_digest: str,
    campaign_uuid: str, checked_at: datetime,
    capture_producer: Mapping[str, Any], capture_created_at: str,
) -> None:
    binding = _object(payload.get("formal_binding"), label=f"{gate}.{role}.formal_binding")
    _require(binding.get("schema_version") == RAW_ARTIFACT_BINDING_SCHEMA_VERSION, f"{gate}.{role} raw binding schema mismatch")
    _require(binding.get("gate_name") == gate, f"{gate}.{role} raw binding gate mismatch")
    _require(binding.get("artifact_role") == role, f"{gate}.{role} raw binding role mismatch")
    _require(binding.get("qualification_campaign_uuid") == campaign_uuid, f"{gate}.{role} raw binding campaign mismatch")
    _require(binding.get("commit") == commit, f"{gate}.{role} raw binding commit mismatch")
    _require(binding.get("source_digest") == source_digest, f"{gate}.{role} raw binding source mismatch")
    _require(
        binding.get("protected_source_digest") == protected_source_digest,
        f"{gate}.{role} raw binding protected source mismatch",
    )
    _require(binding.get("actual_execution") is True, f"{gate}.{role} is not actual execution evidence")
    _require(binding.get("simulated") is False, f"{gate}.{role} simulated raw evidence is forbidden")
    _require(binding.get("component_only") is False, f"{gate}.{role} component raw evidence is forbidden")
    producer = _validate_process_receipt(
        binding.get("producer"),
        label=f"{gate}.{role}.formal_binding.producer",
        required_kind=CAPTURE_PRODUCER_KIND,
    )
    _require(producer == dict(capture_producer), f"{gate}.{role} capture producer identity mismatch")
    captured = parse_utc(binding.get("captured_at"), label=f"{gate}.{role}.formal_binding.captured_at")
    _require(
        binding.get("captured_at") == capture_created_at,
        f"{gate}.{role} capture timestamp differs from its sealed attempt",
    )
    _require(captured <= checked_at + _FUTURE_SKEW, f"{gate}.{role} raw artifact was captured after its gate evidence")


def _load_raw_artifact(
    reference: Mapping[str, Any], *, gate: str, role: str, spec: RawSpec,
    commit: str, source_digest: str, protected_source_digest: str,
    campaign_uuid: str, checked_at: datetime,
    registry: ValidationRegistry,
    capture_producer: Mapping[str, Any] | None = None,
    capture_created_at: str | None = None,
) -> RawArtifact:
    label = f"{gate}.{role}"
    expected_reference_fields = {
        "schema_version", "artifact_id", "gate_name", "artifact_role", "path",
        "sha256", "size_bytes", "media_type", "content_schema_version",
        "qualification_campaign_uuid", "commit", "source_digest",
        "protected_source_digest",
    }
    _require(set(reference) == expected_reference_fields, f"{label} raw reference shape mismatch")
    _require(reference.get("schema_version") == RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION, f"{label} raw reference schema mismatch")
    _require(reference.get("gate_name") == gate and reference.get("artifact_role") == role, f"{label} raw reference identity mismatch")
    _require(reference.get("qualification_campaign_uuid") == campaign_uuid, f"{label} raw reference campaign mismatch")
    _require(reference.get("commit") == commit, f"{label} raw reference commit mismatch")
    _require(reference.get("source_digest") == source_digest, f"{label} raw reference source mismatch")
    _require(
        reference.get("protected_source_digest") == protected_source_digest,
        f"{label} raw reference protected source mismatch",
    )
    artifact_id = str(reference.get("artifact_id") or "")
    _require(_UUIDISH.fullmatch(artifact_id) is not None, f"{label} raw artifact id is invalid")
    raw_path = reference.get("path")
    _require(isinstance(raw_path, str) and raw_path, f"{label} raw path is missing")
    path = _exact_canonical_path(
        raw_path,
        label=f"{label} raw path",
        must_exist=True,
    )
    _require(reference.get("media_type") == spec.media_type, f"{label} raw media type mismatch")
    _require(reference.get("content_schema_version") == spec.content_schema_version, f"{label} raw content schema declaration mismatch")
    size = reference.get("size_bytes")
    _require(isinstance(size, int) and not isinstance(size, bool) and size >= 0, f"{label} raw size declaration is invalid")
    if spec.media_type not in {"application/json", "application/x-ndjson"}:
        role_limit = _NATIVE_ROLE_MAX_BYTES.get(
            (gate, role),
            _MAX_NATIVE_ARTIFACT_BYTES,
        )
        _require(size <= role_limit, f"{label} raw artifact exceeds role size limit")
    expected_sha = str(reference.get("sha256") or "")
    _require(_SHA256.fullmatch(expected_sha) is not None, f"{label} raw SHA-256 declaration is invalid")
    content: bytes | None = None
    if spec.media_type == "application/json":
        content, stat_result = _stable_read(
            path,
            label=f"{label} raw artifact",
            maximum_bytes=_MAX_RAW_JSON_BYTES,
        )
        actual_sha = sha256_bytes(content)
    elif spec.media_type == "application/x-ndjson":
        content, stat_result = _stable_read(
            path,
            label=f"{label} raw artifact",
            maximum_bytes=_MAX_RAW_NDJSON_BYTES,
        )
        actual_sha = sha256_bytes(content)
    else:
        actual_sha, stat_result = _stable_hash(
            path,
            label=f"{label} raw artifact",
        )
    _require(stat_result.st_size == size, f"{label} raw artifact size mismatch")
    _require(spec.allow_empty or size > 0, f"{label} raw artifact is empty")
    _require(actual_sha == expected_sha, f"{label} raw artifact SHA-256 mismatch")
    _require(stat_result.st_mode & 0o022 == 0, f"{label} raw artifact is group/world writable")
    _require(stat_result.st_uid == os.getuid(), f"{label} raw artifact owner mismatch")
    registry.register_raw(path=path, artifact_id=artifact_id, gate=gate, role=role, size=size, sha=expected_sha)

    data: Any = None
    decoded_nodes = 0
    decoded_string_bytes = 0
    if spec.media_type == "application/json":
        assert content is not None
        try:
            data = json.loads(content.decode("utf-8"))
        except Exception as exc:
            raise GateBundleError(f"cannot reparse {label} raw JSON: {exc.__class__.__name__}: {exc}") from exc
        data = _object(data, label=f"{label} raw JSON")
        decoded_nodes, decoded_string_bytes = _validate_json_shape(
            data,
            label=f"{label} raw JSON",
        )
        _require(data.get("schema_version") == spec.content_schema_version, f"{label} raw JSON schema mismatch")
        _validate_binding(
            data, gate=gate, role=role, commit=commit,
            source_digest=source_digest,
            protected_source_digest=protected_source_digest,
            campaign_uuid=campaign_uuid, checked_at=checked_at,
            capture_producer=(capture_producer or {}),
            capture_created_at=str(capture_created_at or ""),
        )
    elif spec.media_type == "application/x-ndjson":
        assert content is not None
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(io.BytesIO(content), start=1):
            if not line.strip():
                continue
            _require(
                len(line) <= _MAX_RAW_NDJSON_LINE_BYTES,
                f"{label} raw JSONL line {line_number} exceeds size limit",
            )
            _require(
                len(rows) < _MAX_RAW_NDJSON_ROWS,
                f"{label} raw JSONL row count exceeds limit",
            )
            try:
                row = _object(json.loads(line), label=f"{label} raw JSONL line {line_number}")
            except Exception as exc:
                if isinstance(exc, GateBundleError):
                    raise
                raise GateBundleError(f"cannot reparse {label} raw JSONL line {line_number}: {exc.__class__.__name__}: {exc}") from exc
            row_nodes, row_string_bytes = _validate_json_shape(
                row,
                label=f"{label} raw JSONL line {line_number}",
            )
            decoded_nodes += row_nodes
            decoded_string_bytes += row_string_bytes
            _require(
                decoded_nodes <= _MAX_RAW_DECODED_NODES,
                f"{label} raw JSONL decoded node budget exceeded",
            )
            _require(
                decoded_string_bytes <= _MAX_RAW_DECODED_STRING_BYTES,
                f"{label} raw JSONL decoded string budget exceeded",
            )
            native_schema = row.get("sample_schema_version") or row.get("schema_version")
            _require(native_schema == spec.content_schema_version, f"{label} raw JSONL schema mismatch at line {line_number}")
            _validate_binding(
                row, gate=gate, role=role, commit=commit,
                source_digest=source_digest,
                protected_source_digest=protected_source_digest,
                campaign_uuid=campaign_uuid, checked_at=checked_at,
                capture_producer=(capture_producer or {}),
                capture_created_at=str(capture_created_at or ""),
            )
            rows.append(row)
        _require(rows, f"{label} raw JSONL contains no records")
        data = rows
    descriptor = (
        _pin_stable_artifact(
            path,
            expected=stat_result,
            label=f"{label} raw artifact",
        )
        if spec.media_type not in {"application/json", "application/x-ndjson"}
        else None
    )
    return RawArtifact(
        role=role,
        path=path,
        reference=dict(reference),
        data=data,
        content_sha256=actual_sha,
        size_bytes=size,
        stat_identity=_stat_identity(stat_result),
        descriptor=descriptor,
        decoded_nodes=decoded_nodes,
        decoded_string_bytes=decoded_string_bytes,
    )


def _json(raw: Mapping[str, RawArtifact], role: str) -> dict[str, Any]:
    value = raw[role].data
    _require(isinstance(value, Mapping), f"{role} authority must be JSON")
    return _native(value)


def _verify_raw_artifact_unchanged(artifact: RawArtifact) -> None:
    if artifact.descriptor is not None:
        pinned = os.fstat(artifact.descriptor)
        _require(
            _stat_identity(pinned) == artifact.stat_identity,
            f"{artifact.role} pinned raw artifact changed during semantic validation",
        )
    digest, current = _stable_hash(
        artifact.path,
        label=f"{artifact.role} raw artifact final readback",
    )
    _require(
        digest == artifact.content_sha256
        and current.st_size == artifact.size_bytes
        and _stat_identity(current) == artifact.stat_identity,
        f"{artifact.role} raw artifact changed during semantic validation",
    )


def _close_raw_artifacts(raw: Mapping[str, RawArtifact]) -> None:
    for artifact in raw.values():
        artifact.close()


def _file_identity_mapping(info: os.stat_result) -> dict[str, int]:
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mode": stat.S_IMODE(info.st_mode),
        "uid": int(info.st_uid),
        "gid": int(info.st_gid),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
        "link_count": int(info.st_nlink),
    }


def _same_campaign(*payloads: Mapping[str, Any], label: str) -> str:
    values = {str(item.get("campaign_uuid") or item.get("campaign_id") or "") for item in payloads}
    _require(len(values) == 1 and next(iter(values)), f"{label} native campaign identity mismatch")
    return next(iter(values))


def _derive_cgroup(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    readback = _json(raw, "cgroup_readback")
    placement = _json(raw, "pid_placement")
    _same_campaign(readback, placement, label="cgroup")
    _require(readback.get("created") is True, "cgroup scope was not created")
    _require(str(readback.get("cgroup_path") or "").startswith("/"), "cgroup path is invalid")
    _require(readback.get("expected_limits") == _EXPECTED_LIMITS, "cgroup expected limits were weakened")
    _require(readback.get("actual_limits") == _EXPECTED_LIMITS, "cgroup kernel limit readback mismatch")
    controllers = {str(item) for item in (readback.get("controllers_verified") or [])}
    _require({"cpu", "memory", "pids"}.issubset(controllers), "cgroup controller proof is incomplete")
    rows = placement.get("placements")
    _require(isinstance(rows, list), "cgroup placement rows are missing")
    by_role: dict[str, dict[str, Any]] = {}
    for value in rows:
        row = _object(value, label="cgroup placement row")
        role = str(row.get("role") or "")
        _positive_int(row.get("pid"), label=f"cgroup placement {role} pid")
        _require(row.get("inside_scope") is True, f"cgroup role is outside scope: {role}")
        _require(row.get("cgroup_path") == readback.get("cgroup_path"), f"cgroup role path mismatch: {role}")
        by_role[role] = row
    _require(_MANDATORY_ROLES.issubset(by_role), "cgroup mandatory role coverage mismatch")
    watchdog = _object(placement.get("watchdog"), label="cgroup watchdog placement")
    _positive_int(watchdog.get("pid"), label="cgroup watchdog pid")
    _require(watchdog.get("inside_scope") is False, "watchdog is inside campaign scope")
    _require(watchdog.get("cgroup_path") != readback.get("cgroup_path"), "watchdog cgroup path equals campaign scope")
    return {"limits": _EXPECTED_LIMITS, "roles": sorted(by_role), "watchdog_pid": watchdog["pid"]}


def _derive_watchdog(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    startup = _json(raw, "watchdog_startup")
    incident = _json(raw, "watchdog_incident")
    terminal = _json(raw, "watchdog_terminal")
    _same_campaign(startup, incident, terminal, label="watchdog")
    _require(startup.get("verified") is True and startup.get("external_process") is True, "watchdog startup identity was not externally verified")
    _require(startup.get("watchdog_outside_campaign_cgroup") is True, "watchdog startup placement is not external")
    _require(_number(startup.get("stale_after_seconds"), label="watchdog stale_after_seconds") == 120.0, "watchdog stale timeout is not 120 seconds")
    watchdog_pid = _positive_int(startup.get("watchdog_pid"), label="watchdog pid")
    orchestrator_pid = _positive_int(startup.get("orchestrator_pid"), label="orchestrator pid")
    _require(watchdog_pid != orchestrator_pid, "watchdog shares the orchestrator PID")
    _require(incident.get("reason") == "HEARTBEAT_STALE", "watchdog incident was not a heartbeat-stale injection")
    details = _object(incident.get("details"), label="watchdog incident details")
    _require(_number(details.get("heartbeat_age_seconds"), label="watchdog heartbeat age") >= 120.0, "watchdog fired before 120 seconds")
    _require(incident.get("watchdog", {}).get("pid") == watchdog_pid, "watchdog incident PID mismatch")
    process = _object(incident.get("orchestrator_process"), label="watchdog orchestrator process evidence")
    _require(process.get("pid") == orchestrator_pid and process.get("identity_verified") is True, "watchdog orchestrator identity evidence mismatch")
    _require(incident.get("watchdog_survived_orchestrator_stop") is True, "watchdog did not survive orchestrator stop")
    _require(terminal.get("ok") is True and terminal.get("incident_id") == incident.get("incident_id"), "watchdog terminal result does not match incident")
    _require(terminal.get("admit_new_jobs") is False, "watchdog terminal result left admission open")
    stop = _object(terminal.get("cgroup_stop"), label="watchdog cgroup stop")
    _require(stop.get("freeze_written") is True and stop.get("kill_written") is True and stop.get("population_cleared") is True, "watchdog did not stop the managed scope")
    _require(not terminal.get("collector_errors"), "watchdog terminal result has collector errors")
    return {"watchdog_pid": watchdog_pid, "orchestrator_pid": orchestrator_pid, "incident_id": incident.get("incident_id")}


def _derive_hard_stop(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    before = _json(raw, "state_before")
    after = _json(raw, "state_after")
    control = _json(raw, "control_after")
    stop = _json(raw, "cgroup_stop")
    _same_campaign(before, after, control, stop, label="hard stop")
    _require(before.get("state") == "ACTIVE" and after.get("state") == "STOPPING_LOAD", "hard stop did not transition ACTIVE to STOPPING_LOAD")
    before_clock = _object(before.get("clock"), label="hard stop before clock")
    after_clock = _object(after.get("clock"), label="hard stop after clock")
    active_before = _number(before_clock.get("continuous_active_seconds"), label="hard stop active time before")
    active_after = _number(after_clock.get("continuous_active_seconds"), label="hard stop active time after")
    _require(active_after == active_before, "hard stop credited active time after detection")
    _require(after_clock.get("formal_segment_valid") is False and after_clock.get("active_finished_at"), "hard stop did not freeze the formal clock")
    after_control = _object(after.get("control"), label="hard stop state control")
    for value in (after_control, control):
        _require(value.get("admit_new_jobs") is False and value.get("load_generator_should_run") is False, "hard stop left load admission open")
        _require(value.get("preserve_evidence_requested") is True, "hard stop did not request evidence preservation")
    _require(control.get("state") == "STOPPING_LOAD", "hard stop control file state mismatch")
    _require(stop.get("freeze_written") is True and stop.get("kill_written") is True and stop.get("population_cleared") is True, "hard stop did not empty the managed scope")
    hard_stop = _object(after.get("hard_stop"), label="hard stop authority")
    _require(hard_stop.get("injected") is True and hard_stop.get("fault_kind"), "hard stop lacks a real injected fault identity")
    return {"fault_kind": hard_stop["fault_kind"], "active_seconds": active_after}


def _checkpoint_native(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in _native(payload).items() if key not in {"captured_at"}}


def _derive_checkpoint(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    before = _json(raw, "checkpoint_before")
    primary = _json(raw, "checkpoint_primary")
    mirror = _json(raw, "checkpoint_mirror")
    tamper = _json(raw, "tamper_rejection")
    _same_campaign(before, primary, mirror, tamper, label="checkpoint recovery")
    before_revision = _positive_int(before.get("revision"), label="checkpoint before revision")
    primary_revision = _positive_int(primary.get("revision"), label="checkpoint primary revision")
    mirror_revision = _positive_int(mirror.get("revision"), label="checkpoint mirror revision")
    _require(primary_revision == mirror_revision >= before_revision, "checkpoint primary/mirror revision mismatch")
    _require(_checkpoint_native(primary) == _checkpoint_native(mirror), "checkpoint primary and mirror payloads differ")
    mirror_path = raw["checkpoint_mirror"].path
    _require(mirror_path != _PERSISTENT_CHECKPOINT_ROOT and mirror_path.is_relative_to(_PERSISTENT_CHECKPOINT_ROOT), "checkpoint mirror is outside the reboot-safe root")
    _require(mirror_path.stat().st_mode & 0o777 == 0o600, "checkpoint mirror is not mode 0600")
    _require(tamper.get("candidate_accepted") is False, "tampered checkpoint was accepted")
    _require(tamper.get("classification") == "FAIL_HARNESS" and tamper.get("rejection_reason"), "checkpoint tamper rejection is not fail closed")
    _require(tamper.get("formal_time_resumed") is False, "checkpoint recovery resumed formal time")
    _require(tamper.get("revalidated") == {"pid_identity": True, "cgroup_identity": True, "source_identity": True}, "checkpoint recovery did not revalidate all identities")
    return {"revision": primary_revision, "mirror_sha256": raw["checkpoint_mirror"].reference["sha256"]}


def _source_digest(payload: Mapping[str, Any]) -> str:
    return str(payload.get("tracked_content_digest") or "")


def _derive_source_drift(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    h0 = _json(raw, "source_h0")
    incident = _json(raw, "drift_incident")
    h24 = _json(raw, "source_h24")
    restored = _json(raw, "source_restored")
    state = _json(raw, "terminal_state")
    _require(h0.get("label") == "H0" and h0.get("verified") is True, "source drift H0 was not verified")
    _require(h24.get("label") == "H24" and h24.get("verified") is True, "source drift H24 capture is invalid")
    _require(restored.get("label") in {"RESTORED", "H0"} and restored.get("verified") is True, "source was not restored after drift injection")
    _require(h0.get("commit") == h24.get("commit") == restored.get("commit"), "source drift changed the Git commit identity")
    _require(_source_digest(h0) != _source_digest(h24), "source drift H0/H24 content did not differ")
    _require(_source_digest(h0) == _source_digest(restored), "source was not restored to the H0 digest")
    _require(incident.get("incident") is True and incident.get("verified") is False and incident.get("incident_evidence_preserved") is True, "source drift incident was not preserved")
    changes = dict(incident.get("tracked_changes") or {}) | dict(incident.get("untracked_changes") or {})
    _require(bool(changes), "source drift incident contains no changed path")
    monitor = _object(incident.get("monitor"), label="source drift monitor")
    _require(monitor.get("machine_verified") is True and monitor.get("formal_eligible") is True, "source drift monitor was not machine verified")
    _require(state.get("state") in {"STOPPING_LOAD", "INVALIDATED", "FAILED"}, "source drift did not invalidate the campaign state")
    control = _object(state.get("control"), label="source drift terminal control")
    _require(control.get("admit_new_jobs") is False and control.get("load_generator_should_run") is False, "source drift left new load enabled")
    return {"changed_paths": sorted(changes), "h0_digest": _source_digest(h0), "h24_digest": _source_digest(h24)}


def _derive_samples(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    rows = raw["resource_samples"].data
    _require(isinstance(rows, list) and len(rows) >= 2, "resource evidence has fewer than two live samples")
    counts: dict[str, list[int]] = {}
    total_expected = 0
    total_valid = 0
    observed_roles: set[str] = set()
    for index, bound_row in enumerate(rows):
        row = _native(bound_row)
        normalized: dict[str, Any] = dict(row)
        if isinstance(row.get("gpu"), list):
            normalized["gpu"] = {
                str(gpu_index): value
                for gpu_index, value in enumerate(row["gpu"])
            }
        expected = row.get("expected_fields")
        valid = row.get("valid_fields")
        missing = row.get("missing_fields")
        _require(isinstance(expected, list) and expected and all(isinstance(item, str) and item for item in expected), f"resource sample {index} expected_fields is invalid")
        _require(isinstance(valid, list) and isinstance(missing, list), f"resource sample {index} validity lists are invalid")
        expected_set, valid_set, missing_set = set(expected), set(valid), set(missing)
        process_roles = row.get("process_roles")
        _require(isinstance(process_roles, Mapping), f"resource sample {index} process_roles is invalid")
        role_fields: set[str] = set()
        for role, role_payload in process_roles.items():
            _require(
                isinstance(role, str) and role and isinstance(role_payload, Mapping),
                f"resource sample {index} process role is invalid",
            )
            observed_roles.add(role)
            role_fields.update(
                f"process_roles.{role}.{metric}"
                for metric in _RESOURCE_PROCESS_METRICS
            )
        contract_expected = (
            set(ResourceCollector.BASE_EXPECTED_FIELDS)
            | set(_FORMAL_RESOURCE_ALWAYS_REQUIRED)
            | role_fields
        )
        _require(
            expected_set == contract_expected,
            f"resource sample {index} expected_fields differ from the formal collector contract",
        )
        _require(len(expected_set) == len(expected) and valid_set <= expected_set and missing_set == expected_set - valid_set, f"resource sample {index} validity accounting mismatch")
        for field_name in valid_set:
            _require(_valid_sample_value(_nested_get(normalized, field_name)), f"resource sample {index} marks missing value valid: {field_name}")
        _require(isinstance(row.get("collector_errors"), Mapping), f"resource sample {index} collector_errors is invalid")
        _require(isinstance(row.get("hard_limit_state"), Mapping), f"resource sample {index} hard_limit_state is missing")
        _require(
            row["hard_limit_state"].get("ok") is True
            and not row["hard_limit_state"].get("tripped"),
            f"resource sample {index} contains a hard-limit violation",
        )
        declared_ratio = _number(row.get("field_completeness_ratio"), label=f"resource sample {index} ratio")
        actual_ratio = len(valid_set) / len(expected_set)
        _require(abs(declared_ratio - round(actual_ratio, 6)) <= 0.000001, f"resource sample {index} completeness declaration mismatch")
        total_expected += len(expected_set)
        total_valid += len(valid_set)
        for field_name in expected_set:
            pair = counts.setdefault(field_name, [0, 0])
            pair[0] += 1
            pair[1] += int(field_name in valid_set)
    overall = total_valid / total_expected
    below = sorted(name for name, (expected, valid) in counts.items() if valid / expected < 0.95)
    _require(
        _MANDATORY_ROLES.issubset(observed_roles),
        "resource samples do not cover every mandatory process role",
    )
    _require(overall >= 0.95 and not below, "resource mandatory field completeness is below 95%")
    trials = _json(raw, "negative_collector_trials").get("trials")
    _require(isinstance(trials, list), "resource negative trials are missing")
    by_case = {str(item.get("case") or ""): item for item in trials if isinstance(item, Mapping)}
    for case in ("empty_collector", "schema_only_sample"):
        row = _object(by_case.get(case), label=f"resource negative trial {case}")
        candidates = row.get("candidate_samples")
        _require(isinstance(candidates, list), f"resource negative trial input is missing: {case}")
        # Derive rejection from the captured input, not its declared verdict.
        candidate_valid = len(candidates) >= 2
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                candidate_valid = False
                continue
            expected = candidate.get("expected_fields")
            valid = candidate.get("valid_fields")
            if not isinstance(expected, list) or not expected or not isinstance(valid, list):
                candidate_valid = False
                continue
            if any(not _valid_sample_value(_nested_get(candidate, str(name))) for name in valid):
                candidate_valid = False
            if len(set(valid)) / len(set(expected)) < 0.95:
                candidate_valid = False
        _require(candidate_valid is False, f"resource negative trial input is not actually invalid: {case}")
        _require(row.get("accepted") is False and row.get("classification") == "FAIL_HARNESS", f"resource validator did not fail closed for {case}")
    return {
        "sample_count": len(rows),
        "expected_values": total_expected,
        "valid_values": total_valid,
        "completeness": round(overall, 6),
        "process_roles": sorted(observed_roles),
    }


def _derive_security(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    report = _json(raw, "security_sentinel")
    _require(report.get("ok") is True and report.get("failed_checks") == [], "production security sentinel did not PASS")
    rows = report.get("checks")
    _require(isinstance(rows, list), "production security check receipts are missing")
    checks = {str(item.get("name") or ""): item for item in rows if isinstance(item, Mapping)}
    _require(set(checks) == _SECURITY_CHECKS, "production security check set is incomplete or contains unknown checks")
    expected_statuses: Mapping[str, set[int]] = {
        "anonymous_root_denied": {401, 403},
        "login_missing_csrf_denied": {400, 403, 419},
        "manager_root_boundary_denied": {403},
        "user_root_boundary_denied": {403},
        "authenticated_missing_csrf_denied": {400, 403, 419},
        "dangerous_confirmation_required": {400},
    }
    for name, item in checks.items():
        allowed = expected_statuses.get(name, {200})
        _require(item.get("status") in allowed, f"production security raw status contradicts check: {name}")
        _require(item.get("ok") is True, f"production security check contains a failure: {name}")
    launcher = _object(checks["production_launcher_contract"].get("detail"), label="production launcher detail")
    _require(launcher.get("security") == "on" and launcher.get("server_mode") == "production", "security sentinel did not use production security")
    _require(int(launcher.get("gunicorn_workers") or 0) >= 2 and launcher.get("isolated_runtime") is True, "security sentinel worker/runtime boundary is invalid")
    controls = _object(checks["production_security_controls"].get("detail"), label="production security controls")
    required_settings = _object(controls.get("required_settings"), label="production security required settings")
    _require(required_settings and all(value is True for value in required_settings.values()), "production security settings were weakened")
    session = _object(checks["cross_worker_session_consistency"].get("detail"), label="cross-worker security detail")
    statuses = session.get("statuses")
    _require(isinstance(statuses, list) and len(statuses) >= 2 and all(value == 200 for value in statuses), "cross-worker session consistency failed")
    return {"checks": sorted(checks), "cross_worker_requests": len(statuses)}


def _external_receipt_errors(dependency: str, receipt: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if receipt.get("dependency") != dependency or receipt.get("available") is not True or receipt.get("synthetic") is not False:
        errors.append("identity_or_availability")
    if str(receipt.get("terminal_state") or "").lower() not in {"completed", "success", "passed"}:
        errors.append("terminal_state")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, Mapping):
        return errors + ["evidence"]
    required: Mapping[str, tuple[str, ...]] = {
        "bt_seed_download": ("seed_started", "torrent_created", "peer_observed", "download_terminal", "payload_sha256_match", "downloaded_via_bt"),
        "comfyui_terminal": ("job_submitted", "terminal_polled", "history_terminal", "output_exists", "output_decodable"),
        "ai_provider_terminal": ("provider_called", "terminal_polled", "response_nonempty", "usage_reported"),
        "backup_restore": (
            "archive_created", "archive_readable", "restore_completed",
            "source_restore_digest_match", "sqlite_quick_check",
            "manifest_validated", "consistent_snapshot_created",
            "wal_checkpoint_completed", "snapshot_marker_verified",
            "backup_api_completed",
        ),
        "production_security_sentinel": ("production_mode", "csrf_enforced", "rbac_enforced", "confirmation_enforced", "audit_chain_verified", "cross_worker_session_verified"),
    }
    if not all(evidence.get(name) is True for name in required[dependency]):
        errors.append("side_effects")
    if dependency == "backup_restore":
        if evidence.get("snapshot_method") not in REVIEWED_BACKUP_SNAPSHOT_METHODS:
            errors.append("snapshot_method")
        if _UUIDISH.fullmatch(str(evidence.get("snapshot_marker_id") or "")) is None:
            errors.append("snapshot_marker_id")
    return errors


def _derive_browser_launch(
    raw: Mapping[str, RawArtifact],
    checks: Mapping[str, Mapping[str, Any]],
    engine: str,
) -> dict[str, Any]:
    role = f"browser_{engine}_launch"
    observation = _json(raw, role)
    required_fields = {
        "schema_version", "engine", "browser_version", "executable_path",
        "browser_pid", "process_start_ticks", "dom_marker_expected",
        "dom_marker_observed", "page_url", "console_errors", "page_errors",
        "closed_cleanly", "started_at", "finished_at",
    }
    _require(set(observation) == required_fields, f"{engine} browser launch observation shape mismatch")
    _require(observation.get("engine") == engine, f"{engine} browser engine identity mismatch")
    _require(
        all(
            isinstance(observation.get(name), str) and observation[name].strip()
            for name in (
                "browser_version", "executable_path", "dom_marker_expected",
                "dom_marker_observed", "page_url", "started_at", "finished_at",
            )
        ),
        f"{engine} browser launch identity is incomplete",
    )
    _positive_int(observation.get("browser_pid"), label=f"{engine} browser pid")
    _positive_int(observation.get("process_start_ticks"), label=f"{engine} browser start ticks")
    _require(
        observation["dom_marker_expected"] == observation["dom_marker_observed"]
        and observation.get("console_errors") == []
        and observation.get("page_errors") == []
        and observation.get("closed_cleanly") is True,
        f"{engine} browser did not complete a clean DOM launch",
    )
    detail = _object(checks[f"browser_{engine}"].get("details"), label=f"{engine} browser preflight detail")
    evidence = _object(detail.get("evidence"), label=f"{engine} browser preflight evidence")
    _require(
        evidence.get("engine") == engine
        and evidence.get("version") == observation["browser_version"]
        and evidence.get("dom_marker") == observation["dom_marker_observed"]
        and evidence.get("raw_authority_path") == str(raw[role].path)
        and evidence.get("raw_authority_sha256") == raw[role].reference["sha256"],
        f"{engine} browser preflight differs from raw launch authority",
    )
    return {"engine": engine, "version": observation["browser_version"]}


def _derive_security_requests(rows: object) -> dict[str, int]:
    _require(isinstance(rows, list) and rows, "security request authority is empty")
    counts: dict[str, int] = {}
    for index, bound_row in enumerate(rows):
        row = _native(bound_row)
        row.pop("schema_version", None)
        required_fields = {
            "case", "request_id", "role", "method", "path", "csrf_mode",
            "status", "response_semantic", "started_at", "finished_at",
        }
        _require(set(row) == required_fields, f"security request row {index} shape mismatch")
        case = str(row.get("case") or "")
        _require(case in _SECURITY_RAW_REQUEST_CASES, f"unknown security request case: {case}")
        _require(
            type(row.get("status")) is int
            and row["status"] in _SECURITY_RAW_REQUEST_CASES[case],
            f"security request status contradicts case: {case}",
        )
        _require(
            all(isinstance(row.get(name), str) and row[name].strip() for name in required_fields - {"status"}),
            f"security request row is incomplete: {case}",
        )
        counts[case] = counts.get(case, 0) + 1
    _require(
        set(counts) == set(_SECURITY_RAW_REQUEST_CASES),
        "security request authority does not cover the exact required case set",
    )
    _require(
        counts.get("cross_worker_session_success", 0) >= 2,
        "security request authority lacks cross-worker repetitions",
    )
    return counts


def _derive_security_audit(rows: object) -> dict[str, Any]:
    _require(isinstance(rows, list) and rows, "security audit authority is empty")
    previous = "0" * 64
    event_types: set[str] = set()
    for index, bound_row in enumerate(rows, start=1):
        row = _native(bound_row)
        row.pop("schema_version", None)
        required_fields = {
            "sequence", "event_type", "actor", "previous_hash", "event_hash", "payload",
        }
        _require(set(row) == required_fields, f"security audit row {index} shape mismatch")
        _require(row.get("sequence") == index, f"security audit sequence mismatch at row {index}")
        _require(row.get("previous_hash") == previous, f"security audit chain link mismatch at row {index}")
        _require(
            isinstance(row.get("event_type"), str)
            and row["event_type"] in _SECURITY_AUDIT_EVENTS
            and isinstance(row.get("actor"), str)
            and row["actor"].strip()
            and isinstance(row.get("payload"), Mapping),
            f"security audit row {index} identity is invalid",
        )
        unsigned = {key: value for key, value in row.items() if key != "event_hash"}
        expected_hash = sha256_bytes(canonical_json_bytes(unsigned))
        _require(row.get("event_hash") == expected_hash, f"security audit hash mismatch at row {index}")
        previous = expected_hash
        event_types.add(row["event_type"])
    _require(
        event_types == _SECURITY_AUDIT_EVENTS,
        "security audit chain does not cover every required event type",
    )
    return {"entries": len(rows), "head": previous}


@dataclass(frozen=True)
class _TarScannedMember:
    name: str
    kind: str
    size: int


def _archive_deadline_check(deadline: float) -> None:
    _require(
        time.monotonic() <= deadline,
        "backup archive validation exceeded its monotonic deadline",
    )


def _tar_exact_pread(
    descriptor: int,
    offset: int,
    size: int,
    *,
    deadline: float,
    label: str,
) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    cursor = offset
    while remaining:
        _archive_deadline_check(deadline)
        block = os.pread(descriptor, min(_MIB, remaining), cursor)
        _require(block, f"backup archive is truncated while reading {label}")
        chunks.append(block)
        cursor += len(block)
        remaining -= len(block)
    return b"".join(chunks)


def _tar_octal(field: bytes, *, label: str, allow_empty: bool = True) -> int:
    _require(not (field and field[0] & 0x80), f"backup archive {label} uses noncanonical binary encoding")
    stripped = field.rstrip(b"\0 ").lstrip(b" ")
    if not stripped:
        _require(allow_empty, f"backup archive {label} is empty")
        return 0
    _require(
        all(byte in b"01234567" for byte in stripped),
        f"backup archive {label} is not canonical octal",
    )
    return int(stripped, 8)


def _validate_tar_checksum(header: bytes) -> None:
    _require(len(header) == 512, "backup archive header is truncated")
    declared = _tar_octal(header[148:156], label="header checksum", allow_empty=False)
    computed = sum(header[:148]) + (8 * ord(" ")) + sum(header[156:])
    _require(declared == computed, "backup archive header checksum mismatch")


def _decode_tar_text(value: bytes, *, label: str) -> str:
    raw = value.split(b"\0", 1)[0]
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GateBundleError(f"backup archive {label} is not UTF-8") from exc


def _canonical_tar_member_name(name: str, *, kind: str) -> str:
    _require(name != "", "backup archive member name is empty")
    _require(kind in {"file", "directory"}, "backup archive member kind is unsupported")
    _require("\\" not in name, f"backup archive member uses a backslash path: {name}")
    _require("//" not in name, f"backup archive member uses an empty path segment: {name}")
    _require(not name.startswith("/"), f"backup archive member is absolute: {name}")
    _require(
        re.match(r"^[A-Za-z]:", name) is None,
        f"backup archive member uses a Windows drive path: {name}",
    )
    _require(
        not any(ord(character) < 32 or ord(character) == 127 for character in name),
        f"backup archive member contains a control character: {name!r}",
    )
    _require(
        unicodedata.normalize("NFC", name) == name,
        f"backup archive member name is not NFC canonical: {name}",
    )
    _require(
        len(name.encode("utf-8")) <= _MAX_ARCHIVE_NAME_BYTES,
        "backup archive member name exceeds limit",
    )
    # POSIX tar writers commonly spell directory headers with one terminal
    # slash.  That slash is a type marker, not a second canonical alias; the
    # manifest always records the normalized no-slash name.  Files never get
    # this exception, and duplicate normalized paths still fail closed.
    normalized_name = name[:-1] if kind == "directory" and name.endswith("/") else name
    _require(normalized_name != "", "backup archive root directory entries are forbidden")
    _require(
        kind == "directory" or not name.endswith("/"),
        f"backup archive file has a directory-style path: {name}",
    )
    path = PurePosixPath(normalized_name)
    _require(
        all(part not in {"", ".", ".."} for part in path.parts),
        f"backup archive member has a traversal/noncanonical segment: {name}",
    )
    canonical = path.as_posix()
    _require(
        normalized_name == canonical,
        f"backup archive member path is not canonical: {name}",
    )
    return canonical


def _parse_pax_records(payload: bytes) -> dict[str, str]:
    records: dict[str, str] = {}
    cursor = 0
    allowed_keys = {
        "path", "mtime", "atime", "ctime", "uid", "gid", "uname", "gname",
    }
    while cursor < len(payload):
        separator = payload.find(b" ", cursor)
        _require(separator > cursor, "backup archive PAX record length is malformed")
        length_field = payload[cursor:separator]
        _require(length_field.isdigit(), "backup archive PAX record length is not decimal")
        length = int(length_field)
        _require(length > separator - cursor + 2, "backup archive PAX record is too short")
        end = cursor + length
        _require(end <= len(payload), "backup archive PAX record exceeds extension payload")
        record = payload[separator + 1:end]
        _require(record.endswith(b"\n"), "backup archive PAX record lacks newline terminator")
        key_value = record[:-1]
        equals = key_value.find(b"=")
        _require(equals > 0, "backup archive PAX record lacks a key/value separator")
        try:
            key = key_value[:equals].decode("ascii", errors="strict")
            value = key_value[equals + 1:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GateBundleError("backup archive PAX metadata encoding is invalid") from exc
        _require(key in allowed_keys, f"backup archive PAX key is not reviewed: {key}")
        _require(key not in records, f"backup archive PAX key is duplicated: {key}")
        records[key] = value
        cursor = end
    _require(cursor == len(payload), "backup archive PAX payload has trailing bytes")
    return records


def _prescan_tar_headers(
    archive_artifact: RawArtifact,
    *,
    deadline: float,
) -> list[_TarScannedMember]:
    _require(archive_artifact.descriptor is not None, "backup archive is not pinned")
    descriptor = archive_artifact.descriptor
    archive_size = int(os.fstat(descriptor).st_size)
    magic = os.pread(descriptor, 6, 0)
    _require(
        not magic.startswith(b"\x1f\x8b")
        and not magic.startswith(b"BZh")
        and not magic.startswith(b"\x28\xb5\x2f\xfd")
        and not magic.startswith(b"\xfd7zXZ\x00"),
        "formal backup authority must be an uncompressed tar archive",
    )
    _require(archive_size >= 1024, "backup archive is too small to contain a terminal tar")

    members: list[_TarScannedMember] = []
    canonical_kinds: dict[str, str] = {}
    metadata_bytes = 0
    total_uncompressed = 0
    pending_path: str | None = None
    pending_extension = False
    offset = 0
    terminated = False
    while offset + 512 <= archive_size:
        _archive_deadline_check(deadline)
        header = _tar_exact_pread(
            descriptor,
            offset,
            512,
            deadline=deadline,
            label="tar header",
        )
        if header == b"\0" * 512:
            second = _tar_exact_pread(
                descriptor,
                offset + 512,
                512,
                deadline=deadline,
                label="second tar terminator",
            )
            _require(second == b"\0" * 512, "backup archive has only one zero terminator block")
            _require(not pending_extension, "backup archive ends with unapplied extension metadata")
            trailing_offset = offset + 1024
            trailing_size = archive_size - trailing_offset
            _require(
                0 <= trailing_size <= _MAX_ARCHIVE_TRAILING_PADDING_BYTES,
                "backup archive trailing padding exceeds limit",
            )
            if trailing_size:
                trailing = _tar_exact_pread(
                    descriptor,
                    trailing_offset,
                    trailing_size,
                    deadline=deadline,
                    label="tar trailing padding",
                )
                _require(not trailing.strip(b"\0"), "backup archive has nonzero data after terminator")
            terminated = True
            break

        _validate_tar_checksum(header)
        _require(
            header[257:263] in {b"ustar\0", b"ustar "},
            "backup archive header is not reviewed USTAR/GNU/PAX format",
        )
        metadata_bytes += 512
        _require(
            metadata_bytes <= _MAX_ARCHIVE_METADATA_BYTES,
            "backup archive metadata exceeds aggregate limit",
        )
        size = _tar_octal(header[124:136], label="member size")
        typeflag = header[156:157] or b"\0"
        name = _decode_tar_text(header[0:100], label="header name")
        prefix = _decode_tar_text(header[345:500], label="header prefix")
        if prefix:
            name = f"{prefix}/{name}"
        payload_offset = offset + 512
        padded_size = ((size + 511) // 512) * 512
        next_offset = payload_offset + padded_size
        _require(
            next_offset <= archive_size,
            "backup archive member payload exceeds archive size",
        )

        if typeflag in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK}:
            _require(
                size <= _MAX_ARCHIVE_EXTENSION_PAYLOAD_BYTES,
                "backup archive extension payload exceeds limit",
            )
            _require(typeflag != tarfile.XGLTYPE, "backup archive global PAX headers are forbidden")
            _require(typeflag != tarfile.GNUTYPE_LONGLINK, "backup archive GNU longlink headers are forbidden")
            if typeflag == tarfile.GNUTYPE_LONGNAME:
                _require(
                    size <= _MAX_ARCHIVE_NAME_BYTES + 1,
                    "backup archive GNU longname payload exceeds name limit",
                )
            metadata_bytes += size
            _require(
                metadata_bytes <= _MAX_ARCHIVE_METADATA_BYTES,
                "backup archive metadata exceeds aggregate limit",
            )
            extension = _tar_exact_pread(
                descriptor,
                payload_offset,
                size,
                deadline=deadline,
                label="tar extension payload",
            )
            pending_extension = True
            if typeflag == tarfile.GNUTYPE_LONGNAME:
                _require(extension.endswith(b"\0"), "backup archive GNU longname is not NUL terminated")
                _require(b"\0" not in extension[:-1], "backup archive GNU longname contains embedded NUL")
                _require(pending_path is None, "backup archive has multiple pending path overrides")
                pending_path = _decode_tar_text(extension, label="GNU longname")
            elif typeflag == tarfile.XHDTYPE:
                pax = _parse_pax_records(extension)
                if "path" in pax:
                    _require(pending_path is None, "backup archive has multiple pending path overrides")
                    pending_path = pax["path"]
            offset = next_offset
            continue

        _require(
            typeflag in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE},
            f"backup archive contains an unsupported member type: {name}",
        )
        kind = "directory" if typeflag == tarfile.DIRTYPE else "file"
        effective_name = pending_path if pending_path is not None else name
        canonical = _canonical_tar_member_name(effective_name, kind=kind)
        pending_path = None
        pending_extension = False
        _require(
            canonical not in canonical_kinds,
            f"backup archive canonical member path is duplicated: {canonical}",
        )
        if kind == "directory":
            _require(size == 0, f"backup archive directory has a payload: {canonical}")
        else:
            _require(
                total_uncompressed + size <= _MAX_ARCHIVE_UNCOMPRESSED_BYTES,
                "backup archive uncompressed size exceeds limit",
            )
            total_uncompressed += size
        _require(
            len(members) < _MAX_ARCHIVE_MEMBERS,
            "backup archive member count exceeds limit",
        )
        canonical_kinds[canonical] = kind
        members.append(_TarScannedMember(canonical, kind, size))
        offset = next_offset

    _require(terminated, "backup archive lacks a complete two-block terminator")
    _require(members and any(member.kind == "file" for member in members), "backup archive has no file members")
    for member in members:
        parts = PurePosixPath(member.name).parts
        for index in range(1, len(parts)):
            parent = PurePosixPath(*parts[:index]).as_posix()
            _require(
                canonical_kinds.get(parent) != "file",
                f"backup archive file is also used as a parent path: {parent}",
            )
    return members


def _safe_tar_members(
    archive_artifact: RawArtifact,
    *,
    sqlite_extract_root: Path | None = None,
    sqlite_extract_paths: dict[str, Path] | None = None,
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + _ARCHIVE_VALIDATION_TIMEOUT_SECONDS
    scanned = _prescan_tar_headers(archive_artifact, deadline=deadline)
    result: dict[str, dict[str, Any]] = {}
    extracted_sqlite_count = 0
    if sqlite_extract_root is not None:
        _require(
            sqlite_extract_root.is_dir() and not sqlite_extract_root.is_symlink(),
            "SQLite archive extraction root is unsafe",
        )
    _require(
        sqlite_extract_paths is None or sqlite_extract_root is not None,
        "SQLite extraction paths require a private extraction root",
    )
    if sqlite_extract_paths is not None:
        _require(not sqlite_extract_paths, "SQLite extraction path output must start empty")
    _require(archive_artifact.descriptor is not None, "backup archive is not pinned")
    duplicate = os.dup(archive_artifact.descriptor)
    os.lseek(duplicate, 0, os.SEEK_SET)
    with os.fdopen(duplicate, "rb", closefd=True) as archive_handle:
        with tarfile.open(fileobj=archive_handle, mode="r:") as archive:
            observed = 0
            for member in archive:
                _archive_deadline_check(deadline)
                _require(observed < len(scanned), "backup archive yielded an unscanned member")
                expected = scanned[observed]
                observed += 1
                kind = "directory" if member.isdir() else "file" if member.isfile() else "unsupported"
                canonical = _canonical_tar_member_name(member.name, kind=kind)
                _require(
                    canonical == expected.name
                    and kind == expected.kind
                    and int(member.size) == expected.size,
                    f"backup archive member differs after extension decoding: {member.name}",
                )
                if kind == "directory":
                    result[canonical] = {
                        "kind": "directory",
                        "sha256": sha256_bytes(b""),
                        "size_bytes": 0,
                    }
                    continue

                extracted = archive.extractfile(member)
                _require(extracted is not None, f"backup archive member cannot be read: {canonical}")
                sqlite_output = None
                if sqlite_extract_root is not None and canonical.endswith(".db"):
                    _require_temporary_disk_reserve(
                        sqlite_extract_root,
                        pending_bytes=member.size,
                        label=f"backup archive SQLite extraction: {canonical}",
                    )
                    extracted_sqlite_count += 1
                    sqlite_path = sqlite_extract_root / f"archive_database_{extracted_sqlite_count}.sqlite3"
                    descriptor = os.open(
                        sqlite_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                    )
                    sqlite_output = os.fdopen(descriptor, "wb", closefd=True)
                digest = hashlib.sha256()
                extracted_size = 0
                try:
                    while True:
                        _archive_deadline_check(deadline)
                        block = extracted.read(_MIB)
                        if not block:
                            break
                        extracted_size += len(block)
                        _require(
                            extracted_size <= member.size,
                            f"backup archive member expanded past its header size: {canonical}",
                        )
                        digest.update(block)
                        if sqlite_output is not None:
                            sqlite_output.write(block)
                            if extracted_size % (256 * _MIB) < len(block):
                                _require_temporary_disk_reserve(
                                    sqlite_extract_root,
                                    pending_bytes=max(0, member.size - extracted_size),
                                    label=f"backup archive SQLite extraction: {canonical}",
                                )
                    if sqlite_output is not None:
                        sqlite_output.flush()
                        os.fsync(sqlite_output.fileno())
                finally:
                    if sqlite_output is not None:
                        sqlite_output.close()
                _require(
                    extracted_size == member.size,
                    f"backup archive member was truncated: {canonical}",
                )
                result[canonical] = {
                    "kind": "file",
                    "sha256": digest.hexdigest(),
                    "size_bytes": extracted_size,
                }
                if sqlite_output is not None and sqlite_extract_paths is not None:
                    sqlite_extract_paths[canonical] = sqlite_path
            _require(observed == len(scanned), "backup archive omitted a pre-scanned member")
    return result


def _require_temporary_disk_reserve(
    root: Path,
    *,
    pending_bytes: int,
    label: str,
) -> None:
    """Keep the reviewed free-space reserve throughout temporary extraction."""

    _require(
        type(pending_bytes) is int and pending_bytes >= 0,
        f"{label} pending size is invalid",
    )
    try:
        filesystem = os.statvfs(root)
    except OSError as exc:
        raise GateBundleError(
            f"cannot measure temporary disk reserve for {label}: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    free_bytes = int(filesystem.f_bavail) * int(filesystem.f_frsize)
    _require(
        free_bytes >= _MINIMUM_FREE_RESERVE_BYTES + pending_bytes,
        f"{label} would breach the 20 GiB temporary disk reserve",
    )


def _validate_png_artifact(artifact: RawArtifact, *, label: str) -> dict[str, int]:
    """Fully decode a bounded PNG from the pinned descriptor."""

    deadline = time.monotonic() + _PNG_VALIDATION_TIMEOUT_SECONDS
    try:
        from PIL import Image

        with Image.open(artifact.pinned_path()) as image:
            width, height = image.size
            _require(image.format == "PNG", f"{label} is not PNG")
            _require(
                type(width) is int and type(height) is int
                and 0 < width <= _MAX_PNG_DIMENSION
                and 0 < height <= _MAX_PNG_DIMENSION
                and width * height <= _MAX_PNG_PIXELS,
                f"{label} dimensions/pixel count exceed the reviewed cap",
            )
            image.verify()
        _require(time.monotonic() <= deadline, f"{label} validation deadline expired")
        # ``verify`` only checks container integrity.  Re-open and force pixel
        # decoding so truncated/corrupt IDAT streams cannot manufacture PASS.
        with Image.open(artifact.pinned_path()) as image:
            _require(
                image.format == "PNG" and image.size == (width, height),
                f"{label} identity changed between verification and decode",
            )
            image.load()
        _require(time.monotonic() <= deadline, f"{label} validation deadline expired")
        return {"width": width, "height": height, "pixels": width * height}
    except GateBundleError:
        raise
    except Exception as exc:
        raise GateBundleError(
            f"{label} cannot be fully decoded: {exc.__class__.__name__}: {exc}"
        ) from exc


def _snapshot_marker_contract(
    value: object,
    *,
    snapshot_id: str,
    label: str,
) -> dict[str, str]:
    marker = _object(value, label=label)
    _require(
        set(marker) == {"table", "snapshot_id", "marker_value", "committed_at"},
        f"{label} shape mismatch",
    )
    _require(
        marker.get("table") == BACKUP_SNAPSHOT_MARKER_TABLE,
        f"{label} table is not the reviewed snapshot marker table",
    )
    _require(
        marker.get("snapshot_id") == snapshot_id
        and _UUIDISH.fullmatch(snapshot_id) is not None,
        f"{label} snapshot identity mismatch",
    )
    marker_value = str(marker.get("marker_value") or "")
    _require(
        _UUIDISH.fullmatch(marker_value) is not None,
        f"{label} marker value is invalid",
    )
    committed_at = str(marker.get("committed_at") or "")
    parse_utc(committed_at, label=f"{label}.committed_at")
    return {
        "table": BACKUP_SNAPSHOT_MARKER_TABLE,
        "snapshot_id": snapshot_id,
        "marker_value": marker_value,
        "committed_at": committed_at,
    }


def _sqlite_snapshot_check(
    path: Path,
    *,
    marker: Mapping[str, str],
    label: str,
    deadline: float,
) -> dict[str, Any]:
    timed_out = False

    def progress() -> int:
        nonlocal timed_out
        if time.monotonic() > deadline:
            timed_out = True
            return 1
        return 0

    try:
        _require(time.monotonic() <= deadline, f"{label} validation deadline expired")
        _require(path.is_absolute(), f"{label} path is not absolute")
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=0.0,
        )
        try:
            connection.set_progress_handler(progress, _SQLITE_PROGRESS_OPCODES)
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            quick_rows = [
                str(row[0])
                for row in connection.execute("PRAGMA quick_check").fetchall()
            ]
            _require(quick_rows == ["ok"], f"{label} SQLite quick_check is not ok")
            schema_rows = connection.execute(
                "SELECT type FROM sqlite_schema WHERE name = ?",
                (BACKUP_SNAPSHOT_MARKER_TABLE,),
            ).fetchall()
            _require(
                schema_rows == [("table",)],
                f"{label} snapshot marker table is missing or ambiguous",
            )
            columns = [
                (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
                for row in connection.execute(
                    f'PRAGMA table_info("{BACKUP_SNAPSHOT_MARKER_TABLE}")'
                ).fetchall()
            ]
            _require(
                columns == [
                    ("snapshot_id", "TEXT", 1, 1),
                    ("marker_value", "TEXT", 1, 0),
                    ("committed_at", "TEXT", 1, 0),
                ],
                f"{label} snapshot marker table schema mismatch",
            )
            marker_rows = connection.execute(
                (
                    f'SELECT snapshot_id, marker_value, committed_at '
                    f'FROM "{BACKUP_SNAPSHOT_MARKER_TABLE}" '
                    "WHERE snapshot_id = ? LIMIT 2"
                ),
                (marker["snapshot_id"],),
            ).fetchall()
            _require(
                marker_rows == [(
                    marker["snapshot_id"],
                    marker["marker_value"],
                    marker["committed_at"],
                )],
                f"{label} committed snapshot marker is missing or differs",
            )
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
            _require(
                page_count > 0
                and page_size >= 512
                and page_size <= 65536
                and page_size & (page_size - 1) == 0,
                f"{label} SQLite page geometry is invalid",
            )
            database_bytes = int(path.stat().st_size)
            _require(
                database_bytes == page_count * page_size,
                f"{label} SQLite file size differs from page geometry",
            )
            _require(time.monotonic() <= deadline, f"{label} validation deadline expired")
            return {
                "quick_check_rows": quick_rows,
                "page_count": page_count,
                "page_size": page_size,
                "database_bytes": database_bytes,
                "schema_version": schema_version,
                "snapshot_marker": dict(marker),
            }
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()
    except GateBundleError:
        raise
    except Exception as exc:
        if timed_out or time.monotonic() > deadline:
            raise GateBundleError(f"{label} validation exceeded its monotonic deadline") from exc
        raise GateBundleError(
            f"{label} SQLite validation failed: {exc.__class__.__name__}: {exc}"
        ) from exc


def _validate_hls_segment_prefix(segment_artifact: RawArtifact) -> bytes:
    segment = segment_artifact.require_prefix(
        length=376,
        label="HLS segment",
    )
    _require(
        segment_artifact.size_bytes >= 188
        and len(segment) >= 188
        and segment[0] == 0x47,
        "HLS segment is not MPEG-TS framed",
    )
    if segment_artifact.size_bytes >= 376:
        _require(
            segment[188] == 0x47,
            "HLS segment second MPEG-TS packet is malformed",
        )
    return segment


def _derive_dependencies(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    preflight = _json(raw, "dependency_preflight")
    _require(preflight.get("status") == "PASS" and preflight.get("ok") is True and preflight.get("failed_checks") == [], "dependency preflight did not PASS")
    checks = preflight.get("checks")
    _require(isinstance(checks, list), "dependency preflight check receipts are missing")
    by_name = {str(item.get("name") or ""): item for item in checks if isinstance(item, Mapping)}
    _require(set(by_name) == _MANDATORY_DEPENDENCIES, "dependency preflight set is incomplete or contains unknown checks")
    _require(all(item.get("status") == "PASS" and item.get("ok") is True for item in by_name.values()), "dependency preflight contains non-PASS work")
    browser_results = {
        engine: _derive_browser_launch(raw, by_name, engine)
        for engine in ("chromium", "firefox", "webkit")
    }
    for role, dependency in _EXTERNAL_DEPENDENCIES.items():
        receipt = _json(raw, role)
        _require(not _external_receipt_errors(dependency, receipt), f"dependency receipt is invalid: {dependency}")
        embedded = _object(by_name[dependency].get("details"), label=f"dependency preflight {dependency} details").get("evidence")
        _require(isinstance(embedded, Mapping) and _native(embedded) == receipt, f"dependency receipt differs from the preflight authority: {dependency}")

    playlist = raw["hls_playlist"]
    text = playlist.require_bytes(
        maximum_bytes=_MAX_HLS_PLAYLIST_BYTES,
        label="HLS playlist",
    ).decode("utf-8", errors="strict")
    _require(text.startswith("#EXTM3U") and "#EXT-X-ENDLIST" in text, "HLS playlist is not terminal/readable")
    segment_uris = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    _require(len(segment_uris) == 1, "HLS raw authority must contain exactly one reviewed segment")
    segment_uri = segment_uris[0]
    segment_relative_path = Path(segment_uri)
    _require(
        not segment_relative_path.is_absolute()
        and segment_uri == str(segment_relative_path)
        and all(part not in {".", ".."} for part in segment_relative_path.parts),
        "HLS segment URI must be an exact canonical relative path",
    )
    segment_path = _exact_canonical_path(
        playlist.path.parent / segment_relative_path,
        label="HLS playlist segment path",
        must_exist=True,
    )
    _require(segment_path == raw["hls_segment"].path, "HLS playlist does not reference the raw segment authority")
    segment_artifact = raw["hls_segment"]
    _validate_hls_segment_prefix(segment_artifact)
    ffprobe = _json(raw, "hls_ffprobe")
    _require(
        ffprobe.get("input_path") == str(playlist.path)
        and ffprobe.get("returncode") == 0
        and ffprobe.get("segment_count") == 1
        and ffprobe.get("segment_sha256") == segment_artifact.content_sha256,
        "HLS ffprobe authority identity mismatch",
    )
    streams = ffprobe.get("streams")
    _require(
        isinstance(streams, list)
        and any(isinstance(row, Mapping) and row.get("codec_type") == "video" for row in streams)
        and _number((ffprobe.get("format") or {}).get("duration"), label="HLS duration") > 0,
        "HLS ffprobe authority did not prove playable video",
    )
    hls_evidence = _object(by_name["ffmpeg_hls"].get("details"), label="ffmpeg HLS details").get("evidence")
    _require(
        isinstance(hls_evidence, Mapping)
        and hls_evidence.get("playlist") == str(playlist.path)
        and hls_evidence.get("segment_path") == str(raw["hls_segment"].path)
        and hls_evidence.get("ffprobe_path") == str(raw["hls_ffprobe"].path),
        "HLS preflight paths differ from raw authorities",
    )

    bt = _json(raw, "bt_receipt").get("evidence") or {}
    comfy = _json(raw, "comfyui_receipt").get("evidence") or {}
    backup = _json(raw, "backup_receipt").get("evidence") or {}
    identities = (
        ("bt_payload", bt, "download_path", "payload_sha256"),
        ("comfyui_output", comfy, "output_path", "output_sha256"),
        ("backup_archive", backup, "archive_path", "archive_sha256"),
    )
    for artifact_role, evidence, path_key, hash_key in identities:
        artifact = raw[artifact_role]
        _require(evidence.get(path_key) == str(artifact.path), f"{artifact_role} receipt path mismatch")
        _require(
            str(evidence.get(hash_key) or "").lower()
            == artifact.content_sha256,
            f"{artifact_role} receipt SHA-256 mismatch",
        )

    bt_trace = _json(raw, "bt_protocol_trace")
    bt_receipt = _json(raw, "bt_receipt").get("evidence") or {}
    _require(
        bt_trace.get("protocol") == "bittorrent"
        and bt_trace.get("terminal_state") == "completed"
        and bt_trace.get("peer_handshake_observed") is True
        and bt_trace.get("piece_hashes_verified") is True
        and bt_trace.get("info_hash") == bt_receipt.get("info_hash")
        and bt_trace.get("payload_path") == str(raw["bt_payload"].path)
        and bt_trace.get("payload_sha256") == raw["bt_payload"].content_sha256
        and int(bt_trace.get("payload_size_bytes") or -1) == raw["bt_payload"].size_bytes,
        "BT protocol trace does not prove the payload authority",
    )
    _positive_int(bt_trace.get("seed_pid"), label="BT seed pid")
    _positive_int(bt_trace.get("client_pid"), label="BT client pid")
    _require(bt_trace["seed_pid"] != bt_trace["client_pid"], "BT seed/client are not independent processes")
    bt_events = bt_trace.get("events")
    _require(
        isinstance(bt_events, list)
        and [row.get("event") for row in bt_events if isinstance(row, Mapping)]
        == ["torrent_created", "peer_handshake", "piece_verified", "download_completed"],
        "BT protocol event sequence is incomplete",
    )

    ai = _json(raw, "ai_provider_exchange")
    ai_receipt = _json(raw, "ai_receipt").get("evidence") or {}
    usage = _object(ai.get("usage"), label="AI provider raw usage")
    _require(
        ai.get("synthetic") is False
        and ai.get("terminal_state") == "completed"
        and ai.get("provider") == ai.get("configured_provider") == ai_receipt.get("provider")
        and ai.get("model") == ai.get("configured_model") == ai_receipt.get("model")
        and ai.get("request_id") == ai_receipt.get("request_id")
        and isinstance(ai.get("response_text"), str)
        and ai["response_text"].strip()
        and type(usage.get("input_tokens")) is int
        and type(usage.get("output_tokens")) is int
        and type(usage.get("total_tokens")) is int
        and usage["input_tokens"] >= 0
        and usage["output_tokens"] > 0
        and usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"],
        "AI provider raw exchange/usage identity is invalid",
    )

    comfy_history = _json(raw, "comfyui_history")
    comfy_receipt = _json(raw, "comfyui_receipt").get("evidence") or {}
    _require(
        comfy_history.get("terminal_state") == "success"
        and comfy_history.get("prompt_id") == comfy_receipt.get("prompt_id")
        and comfy_history.get("job_id") == comfy_receipt.get("job_id")
        and comfy_history.get("output_path") == str(raw["comfyui_output"].path)
        and comfy_history.get("output_sha256") == raw["comfyui_output"].content_sha256
        and int(comfy_history.get("executed_node_count") or 0) > 0
        and comfy_history.get("errors") == [],
        "ComfyUI history does not prove terminal output",
    )
    comfyui_png = _validate_png_artifact(
        raw["comfyui_output"],
        label="ComfyUI output PNG",
    )
    _require(
        False,
        "backup live-source database authority is not implemented; dependency gate fails closed",
    )
    backup_manifest = _json(raw, "backup_restore_manifest")
    backup_receipt = _json(raw, "backup_receipt").get("evidence") or {}
    expected_manifest_fields = {
        "schema_version", "snapshot_id", "archive_path", "archive_sha256",
        "restore_completed", "archive_entries", "restored_database_path",
        "source_database_sha256", "restored_database_sha256",
        "sqlite_snapshot",
    }
    _require(
        set(backup_manifest) == expected_manifest_fields,
        "backup restore manifest shape mismatch",
    )
    snapshot_id = str(backup_manifest.get("snapshot_id") or "")
    _require(
        snapshot_id == backup_receipt.get("snapshot_id")
        and _UUIDISH.fullmatch(snapshot_id) is not None
        and backup_manifest.get("archive_path") == str(raw["backup_archive"].path)
        and backup_manifest.get("archive_sha256") == raw["backup_archive"].content_sha256
        and backup_manifest.get("restore_completed") is True,
        "backup restore manifest identity mismatch",
    )
    sqlite_snapshot = _object(
        backup_manifest.get("sqlite_snapshot"),
        label="backup restore sqlite_snapshot",
    )
    _require(
        set(sqlite_snapshot) == {
            "snapshot_method", "source_journal_mode", "wal_checkpoint",
            "backup_completion", "snapshot_marker",
        },
        "backup restore sqlite_snapshot shape mismatch",
    )
    snapshot_method = sqlite_snapshot.get("snapshot_method")
    _require(
        snapshot_method in REVIEWED_BACKUP_SNAPSHOT_METHODS
        and backup_receipt.get("snapshot_method") == snapshot_method,
        "backup snapshot method is not reviewed or differs from receipt",
    )
    _require(
        str(sqlite_snapshot.get("source_journal_mode") or "").lower() == "wal",
        "backup source database was not observed in WAL journal mode",
    )
    wal_checkpoint = _object(
        sqlite_snapshot.get("wal_checkpoint"),
        label="backup WAL checkpoint",
    )
    _require(
        set(wal_checkpoint) == {
            "mode", "busy", "log_frames", "checkpointed_frames", "completed",
        },
        "backup WAL checkpoint shape mismatch",
    )
    wal_busy = wal_checkpoint.get("busy")
    wal_frames = wal_checkpoint.get("log_frames")
    checkpointed_frames = wal_checkpoint.get("checkpointed_frames")
    _require(
        wal_checkpoint.get("mode") == "FULL"
        and wal_checkpoint.get("completed") is True
        and type(wal_busy) is int
        and wal_busy == 0
        and type(wal_frames) is int
        and wal_frames > 0
        and type(checkpointed_frames) is int
        and checkpointed_frames == wal_frames,
        "backup WAL checkpoint did not prove complete frame checkpointing",
    )
    backup_completion = _object(
        sqlite_snapshot.get("backup_completion"),
        label="backup API completion",
    )
    _require(
        set(backup_completion) == {
            "method", "completed", "source_page_count", "result_page_count",
            "result_database_bytes",
        },
        "backup API completion shape mismatch",
    )
    _require(
        backup_completion.get("method") == snapshot_method
        and backup_completion.get("completed") is True
        and type(backup_completion.get("source_page_count")) is int
        and backup_completion["source_page_count"] > 0
        and type(backup_completion.get("result_page_count")) is int
        and backup_completion["result_page_count"] > 0
        and type(backup_completion.get("result_database_bytes")) is int
        and backup_completion["result_database_bytes"] > 0,
        "backup API completion evidence is invalid",
    )
    if snapshot_method == "sqlite_backup_api":
        _require(
            backup_completion["source_page_count"]
            == backup_completion["result_page_count"],
            "SQLite backup API page counts differ",
        )
    else:
        _require(
            backup_completion["source_page_count"]
            >= backup_completion["result_page_count"],
            "VACUUM INTO result has more pages than its source",
        )
    marker = _snapshot_marker_contract(
        sqlite_snapshot.get("snapshot_marker"),
        snapshot_id=snapshot_id,
        label="backup snapshot marker",
    )
    _require(
        backup_receipt.get("snapshot_marker_id") == marker["marker_value"],
        "backup receipt snapshot marker identity mismatch",
    )
    quick = _json(raw, "backup_sqlite_check")
    _require(
        set(quick) == {
            "schema_version", "snapshot_id", "database_path",
            "quick_check_rows", "source_sha256", "restored_sha256",
            "snapshot_method", "snapshot_marker",
        },
        "backup SQLite check authority shape mismatch",
    )
    quick_marker = _snapshot_marker_contract(
        quick.get("snapshot_marker"),
        snapshot_id=snapshot_id,
        label="backup SQLite check snapshot marker",
    )
    _require(
        quick_marker == marker and quick.get("snapshot_method") == snapshot_method,
        "backup SQLite check snapshot contract mismatch",
    )

    extracted_sqlite_paths: dict[str, Path] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="hackme-gate-backup-") as extract_text:
            extract_root = Path(extract_text)
            archive_members = _safe_tar_members(
                raw["backup_archive"],
                sqlite_extract_root=extract_root,
                sqlite_extract_paths=extracted_sqlite_paths,
            )
            entries = backup_manifest.get("archive_entries")
            _require(isinstance(entries, list) and entries, "backup restore manifest entries are empty")
            manifest_entries: dict[str, Mapping[str, Any]] = {}
            for entry in entries:
                _require(isinstance(entry, Mapping), "backup restore manifest entry is invalid")
                _require(
                    set(entry) == {"path", "kind", "sha256", "size_bytes"},
                    "backup restore manifest entry shape mismatch",
                )
                member_kind = str(entry.get("kind") or "")
                declared_member_path = str(entry.get("path") or "")
                member_path = _canonical_tar_member_name(
                    declared_member_path,
                    kind=member_kind,
                )
                _require(
                    declared_member_path == member_path,
                    "backup restore manifest path is not canonical",
                )
                _require(
                    member_path not in manifest_entries,
                    "backup restore manifest path is duplicated",
                )
                manifest_entries[member_path] = entry
            _require(set(manifest_entries) == set(archive_members), "backup archive differs from restore manifest")
            for member_path, member_evidence in archive_members.items():
                entry = manifest_entries[member_path]
                _require(
                    entry.get("kind") == member_evidence["kind"]
                    and entry.get("sha256") == member_evidence["sha256"]
                    and entry.get("size_bytes") == member_evidence["size_bytes"],
                    f"backup archive member differs from restore manifest: {member_path}",
                )
            database_members = [
                name for name, evidence in archive_members.items()
                if evidence["kind"] == "file" and name.endswith(".db")
            ]
            _require(len(database_members) == 1, "backup archive must contain exactly one reviewed SQLite database")
            database_member = database_members[0]
            _require(
                set(extracted_sqlite_paths) == {database_member},
                "backup archive SQLite extraction identity mismatch",
            )
            sqlite_deadline = time.monotonic() + _SQLITE_VALIDATION_TIMEOUT_SECONDS
            archive_database_observation = _sqlite_snapshot_check(
                extracted_sqlite_paths[database_member],
                marker=marker,
                label="archived SQLite snapshot",
                deadline=sqlite_deadline,
            )
            restored_database = raw["backup_restored_database"]
            restored_database_observation = _sqlite_snapshot_check(
                restored_database.pinned_path(),
                marker=marker,
                label="restored SQLite snapshot",
                deadline=sqlite_deadline,
            )

            database_evidence = archive_members[database_member]
            _require(
                backup_manifest.get("restored_database_path") == str(restored_database.path)
                and backup_manifest.get("source_database_sha256") == database_evidence["sha256"]
                and backup_manifest.get("restored_database_sha256") == restored_database.content_sha256
                and database_evidence["sha256"] == restored_database.content_sha256
                and database_evidence["size_bytes"] == restored_database.size_bytes,
                "restored SQLite database differs from backup snapshot",
            )
            _require(
                archive_database_observation == restored_database_observation,
                "archived and restored SQLite invariant observations differ",
            )
            _require(
                backup_completion["result_page_count"]
                == archive_database_observation["page_count"]
                and backup_completion["result_database_bytes"]
                == archive_database_observation["database_bytes"],
                "backup API completion differs from the archived SQLite snapshot",
            )
            _require(
                quick.get("snapshot_id") == snapshot_id
                and quick.get("database_path") == str(restored_database.path)
                and quick.get("quick_check_rows")
                == archive_database_observation["quick_check_rows"]
                == ["ok"]
                and quick.get("source_sha256")
                == quick.get("restored_sha256")
                == restored_database.content_sha256,
                "backup SQLite check authority mismatch",
            )
    except GateBundleError:
        raise
    except Exception as exc:
        raise GateBundleError(f"backup archive cannot be reparsed: {exc.__class__.__name__}: {exc}") from exc

    security_counts = _derive_security_requests(raw["security_requests"].data)
    audit = _derive_security_audit(raw["security_audit_chain"].data)
    security_receipt = _json(raw, "security_receipt").get("evidence") or {}
    _require(
        all(security_receipt.get(name) is True for name in (
            "production_mode", "csrf_enforced", "rbac_enforced",
            "confirmation_enforced", "audit_chain_verified",
            "cross_worker_session_verified",
        )),
        "security receipt contradicts raw request/audit authorities",
    )
    return {
        "checks": sorted(by_name),
        "browsers": browser_results,
        "security_request_counts": security_counts,
        "security_audit": audit,
        "comfyui_png": comfyui_png,
        "live_artifacts": [
            "hls_playlist", "hls_segment", "bt_payload", "comfyui_output",
            "backup_archive", "backup_restored_database",
        ],
    }


def _derive_smoke(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    supervisor = _json(raw, "supervisor_result")
    runner = _json(raw, "smoke_runner")
    _require(supervisor.get("level") == "smoke" and supervisor.get("ok") is True and supervisor.get("classification") == "PASS", "smoke supervisor did not PASS")
    _require(supervisor.get("runner_returncode") == 0 and supervisor.get("runner_verdict") == "PASS", "smoke supervisor runner result failed")
    _require(supervisor.get("source_final", {}).get("verified") is True, "smoke source changed")
    cleanup = _object(supervisor.get("cleanup"), label="smoke cleanup")
    _require(cleanup.get("source_monitor", {}).get("ok") is True and cleanup.get("watchdog", {}).get("ok") is True and cleanup.get("scope", {}).get("ok") is True, "smoke supervisor cleanup failed")
    gates = _object(supervisor.get("gates"), label="smoke supervisor gates")
    for name in ("cgroup_limits_verified", "external_watchdog_verified", "runner_and_watchdog_placement_verified"):
        _require(gates.get(name, {}).get("status") == "PASS", f"smoke supervisor gate failed: {name}")
    _require(runner.get("ok") is True and runner.get("classification") == "PASS", "smoke load runner did not PASS")
    contract = _object(runner.get("contract"), label="smoke runner contract")
    metrics = _object(runner.get("metrics"), label="smoke runner metrics")
    _require(int(contract.get("configured_duration_seconds") or 0) == 180, "smoke duration is not exactly 180 seconds")
    _require(int(contract.get("configured_concurrency") or 0) == 32, "smoke did not configure 32 workers")
    _require(_number(runner.get("runtime_seconds"), label="smoke runtime") >= 180.0, "smoke did not complete 180 seconds")
    _require(int(metrics.get("max_active_workers") or 0) >= 28 and int(metrics.get("operations_completed") or 0) > 0, "smoke effective load was insufficient")
    _require(not metrics.get("transport_errors") and not runner.get("unexpected_errors") and not runner.get("silent_failures"), "smoke contains transport, unexpected, or silent failures")
    _require(all(value is True for value in (runner.get("gates") or {}).values()), "smoke native runner gate failed")
    return {"runtime_seconds": runner["runtime_seconds"], "operations_completed": metrics["operations_completed"], "max_active_workers": metrics["max_active_workers"]}


def _receipt_has_shortcut(value: Any) -> bool:
    forbidden_keys = {"skip", "skipped", "fallback", "expected_gap", "http_status", "status_code", "ok"}
    if isinstance(value, Mapping):
        return any(str(key).lower() in forbidden_keys or _receipt_has_shortcut(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_receipt_has_shortcut(item) for item in value)
    return False


def _derive_scenario_receipt(payload: Mapping[str, Any], scenario_id: str) -> dict[str, Any]:
    receipt = _native(payload)
    binding = FORMAL_SCENARIO_BINDINGS.get(scenario_id)
    _require(binding is not None, f"rehearsal scenario has no reviewed binding: {scenario_id}")
    validation = validate_scenario_runtime_receipt(receipt, binding)
    _require(
        validation.valid and validation.contract_pass and validation.status.value == "PASS",
        f"rehearsal scenario canonical receipt validation failed: {scenario_id}: "
        + ",".join(validation.errors),
    )
    return {
        "terminal_state": receipt["terminal_state"],
        "runner_id": binding.runner_id,
        "artifact_count": len(receipt["artifact_ids"]),
    }


def _derive_rehearsal(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    supervisor = _json(raw, "supervisor_result")
    runner = _json(raw, "runner_result")
    _require(supervisor.get("level") == "rehearsal" and supervisor.get("ok") is True and supervisor.get("classification") == "PASS", "rehearsal supervisor did not PASS")
    _require(supervisor.get("runner_returncode") == 0 and supervisor.get("runner_verdict") == "PASS", "rehearsal supervisor runner failed")
    _require(supervisor.get("source_final", {}).get("verified") is True, "rehearsal source changed")
    cleanup = _object(supervisor.get("cleanup"), label="rehearsal cleanup")
    _require(cleanup.get("source_monitor", {}).get("ok") is True and cleanup.get("watchdog", {}).get("ok") is True and cleanup.get("scope", {}).get("ok") is True, "rehearsal cleanup failed")
    _require(runner.get("ok") is True and runner.get("verdict") == "PASS" and runner.get("classification") == "PASS", "rehearsal runner did not PASS")
    _require(int(runner.get("required_active_test_seconds") or 0) == 3600, "rehearsal duration is not exactly 3600 seconds")
    _require(_number(runner.get("active_test_seconds"), label="rehearsal active seconds") >= 3600.0, "rehearsal did not complete 3600 active seconds")
    _require(_number(runner.get("invalid_seconds"), label="rehearsal invalid seconds") == 0.0, "rehearsal contains invalid active time")
    _require(runner.get("scenario_scope") == "mandatory_full_feature_matrix", "rehearsal did not execute the mandatory matrix")
    features = runner.get("mandatory_features_executed")
    _require(isinstance(features, list) and _MANDATORY_REHEARSAL_FEATURES.issubset(set(features)), "rehearsal mandatory heavy features are incomplete")
    _require(runner.get("skips") == [] and runner.get("fallbacks") == [] and runner.get("expected_gaps") == [], "rehearsal contains skipped/fallback/gap work")
    derived = {
        scenario_id: _derive_scenario_receipt(
            _object(raw[f"scenario_{scenario_id}"].data, label=f"scenario {scenario_id}"),
            scenario_id,
        )
        for scenario_id in _MANDATORY_REHEARSAL_SCENARIOS
    }
    return {"active_seconds": runner["active_test_seconds"], "scenarios": derived, "features": sorted(set(features))}


def _manifest_digests(rows: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    native_rows: list[dict[str, Any]] = []
    for row in rows:
        native = _native(row)
        # The formal JSONL capture adds a record schema; the native source
        # freezer hashes the TrackedEntry payload itself.
        native.pop("schema_version", None)
        native_rows.append(native)
    sorted_rows = sorted(native_rows, key=lambda item: str(item.get("path") or ""))
    manifest = hashlib.sha256()
    content = hashlib.sha256()
    for row in sorted_rows:
        path = str(row.get("path") or "")
        mode = str(row.get("index_mode") or "")
        working = str(row.get("working_sha256") or "")
        _require(path and mode and _SHA256.fullmatch(working) is not None, "tracked manifest row identity is invalid")
        manifest.update(canonical_json_bytes(row))
        manifest.update(b"\n")
        content.update(path.encode("utf-8", errors="surrogateescape"))
        content.update(b"\0")
        content.update(mode.encode("ascii"))
        content.update(b"\0")
        content.update(working.encode("ascii"))
        content.update(b"\n")
    return manifest.hexdigest(), content.hexdigest()


def _protected_manifest_digests(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, str, list[dict[str, Any]]]:
    native_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        native = _native(row)
        native.pop("schema_version", None)
        _require(
            set(native) == _PROTECTED_ENTRY_FIELDS,
            f"protected ignored manifest row {index} shape mismatch",
        )
        path = native.get("path")
        kind = native.get("kind")
        _require(
            isinstance(path, str)
            and path in REVIEWED_PROTECTED_IGNORED_PATHS
            and path not in seen,
            f"protected ignored manifest row {index} path is invalid",
        )
        seen.add(path)
        _require(kind in {"file", "missing"}, f"protected ignored path has unsafe type: {path}")
        _require(native.get("symlink_target") == "", f"protected ignored path has a symlink target: {path}")
        integer_fields = (
            "filesystem_mode", "size", "mtime_ns", "ctime_ns", "inode", "device",
        )
        _require(
            all(type(native.get(name)) is int for name in integer_fields),
            f"protected ignored path metadata types are invalid: {path}",
        )
        if kind == "missing":
            _require(
                native.get("working_sha256") == ""
                and all(native.get(name) == -1 for name in integer_fields),
                f"protected ignored missing-path sentinel is invalid: {path}",
            )
        else:
            _require(
                _SHA256.fullmatch(str(native.get("working_sha256") or "")) is not None,
                f"protected ignored file digest is invalid: {path}",
            )
            _require(
                int(native["filesystem_mode"]) >= 0
                and int(native["size"]) >= 0
                and int(native["mtime_ns"]) >= 0
                and int(native["ctime_ns"]) >= 0
                and int(native["inode"]) > 0
                and int(native["device"]) >= 0,
                f"protected ignored file metadata is invalid: {path}",
            )
        native_rows.append(native)
    _require(
        seen == set(REVIEWED_PROTECTED_IGNORED_PATHS),
        "protected ignored manifest does not contain the exact reviewed path set",
    )
    sorted_rows = sorted(native_rows, key=lambda item: str(item["path"]))
    manifest = hashlib.sha256()
    content = hashlib.sha256()
    for row in sorted_rows:
        manifest.update(canonical_json_bytes(row))
        manifest.update(b"\n")
        content.update(str(row["path"]).encode("utf-8", errors="surrogateescape"))
        content.update(b"\0")
        content.update(str(row["kind"]).encode("ascii"))
        content.update(b"\0")
        content.update(str(row["filesystem_mode"]).encode("ascii"))
        content.update(b"\0")
        content.update(str(row["working_sha256"]).encode("ascii"))
        content.update(b"\0")
        content.update(str(row["symlink_target"]).encode("utf-8", errors="surrogateescape"))
        content.update(b"\n")
    return manifest.hexdigest(), content.hexdigest(), sorted_rows


def _parse_jsonl_authority(
    path: Path,
    *,
    label: str,
) -> tuple[list[dict[str, Any]], str, tuple[int, ...]]:
    content, snapshot_stat = _stable_read(
        path,
        label=label,
        maximum_bytes=_MAX_RAW_NDJSON_BYTES,
    )
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(io.BytesIO(content), start=1):
        if not line.strip():
            continue
        _require(
            len(line) <= _MAX_RAW_NDJSON_LINE_BYTES,
            f"{label} line {line_number} exceeds size limit",
        )
        _require(
            len(rows) < _MAX_RAW_NDJSON_ROWS,
            f"{label} row count exceeds limit",
        )
        try:
            rows.append(_object(json.loads(line), label=f"{label} line {line_number}"))
        except Exception as exc:
            if isinstance(exc, GateBundleError):
                raise
            raise GateBundleError(
                f"cannot reparse {label} line {line_number}: {exc.__class__.__name__}: {exc}"
            ) from exc
        _validate_json_shape(rows[-1], label=f"{label} line {line_number}")
    _require(rows, f"{label} contains no records")
    return rows, sha256_bytes(content), _stat_identity(snapshot_stat)


def _source_identity_from_authority(
    source_authority: Mapping[str, Any],
) -> tuple[str, str]:
    authority = _native(source_authority)
    _require(
        authority.get("schema_version") == "hackme.source-freeze.v3"
        and authority.get("label") == "H0"
        and authority.get("verified") is True,
        "current source authority is not a verified H0 freeze",
    )
    artifacts = _object(authority.get("artifacts"), label="current source authority artifacts")
    tracked_value = artifacts.get("tracked_manifest")
    protected_value = artifacts.get("protected_ignored_manifest")
    _require(
        isinstance(tracked_value, str) and bool(tracked_value),
        "current tracked manifest path is missing",
    )
    _require(
        isinstance(protected_value, str) and bool(protected_value),
        "current protected ignored manifest path is missing",
    )
    tracked_path = _exact_canonical_path(
        tracked_value,
        label="current tracked manifest",
        must_exist=True,
    )
    protected_path = _exact_canonical_path(
        protected_value,
        label="current protected ignored manifest",
        must_exist=True,
    )
    tracked_rows, tracked_sha, tracked_stat = _parse_jsonl_authority(
        tracked_path,
        label="current tracked manifest",
    )
    tracked_manifest, tracked_content = _manifest_digests(tracked_rows)
    _require(
        tracked_manifest == authority.get("tracked_manifest_digest")
        and tracked_content == authority.get("tracked_content_digest")
        and int(authority.get("tracked_file_count") or -1) == len(tracked_rows),
        "current tracked source authority digest mismatch",
    )
    protected_rows, protected_sha, protected_stat = _parse_jsonl_authority(
        protected_path,
        label="current protected ignored manifest",
    )
    protected_manifest, protected_content, _ = _protected_manifest_digests(protected_rows)
    _require(
        protected_manifest == authority.get("protected_ignored_manifest_digest")
        and protected_content == authority.get("protected_ignored_content_digest")
        and int(authority.get("protected_ignored_file_count") or -1) == len(protected_rows),
        "current protected ignored source authority digest mismatch",
    )
    for path, label, initial_sha, initial_stat in (
        (tracked_path, "current tracked manifest", tracked_sha, tracked_stat),
        (
            protected_path,
            "current protected ignored manifest",
            protected_sha,
            protected_stat,
        ),
    ):
        final_sha, final_stat = _stable_hash(path, label=f"{label} final readback")
        _require(
            final_sha == initial_sha and _stat_identity(final_stat) == initial_stat,
            f"{label} changed during source identity validation",
        )
    return tracked_content, protected_source_identity_digest(
        protected_manifest,
        protected_content,
    )


def _resolve_source_identity(
    *,
    source_authority: Mapping[str, Any] | None,
    source_digest: str | None,
    protected_source_digest: str | None,
) -> tuple[str, str]:
    if source_authority is not None:
        derived_source, derived_protected = _source_identity_from_authority(source_authority)
        if source_digest is not None:
            _require(source_digest == derived_source, "explicit tracked source digest differs from source authority")
        if protected_source_digest is not None:
            _require(
                protected_source_digest == derived_protected,
                "explicit protected source digest differs from source authority",
            )
        return derived_source, derived_protected
    tracked = str(source_digest or "").lower()
    protected = str(protected_source_digest or "").lower()
    _require(_SHA256.fullmatch(tracked) is not None, "expected source digest is not lowercase SHA-256")
    _require(
        _SHA256.fullmatch(protected) is not None,
        "expected protected source digest is required and must be lowercase SHA-256",
    )
    return tracked, protected


def _derive_clean_source(raw: Mapping[str, RawArtifact]) -> dict[str, Any]:
    source = _json(raw, "source_h0")
    _require(source.get("label") == "H0" and source.get("verified") is True and source.get("require_clean") is True, "clean source H0 was not captured in strict mode")
    _require(source.get("git_status_empty") is True and source.get("git_diff_binary_empty") is True, "source H0 declares a dirty worktree")
    status_bytes = raw["git_status"].require_bytes(
        maximum_bytes=_MAX_SMALL_NATIVE_BYTES,
        label="Git porcelain status authority",
    )
    diff_bytes = raw["git_diff_binary"].require_bytes(
        maximum_bytes=_MAX_SMALL_NATIVE_BYTES,
        label="Git binary diff authority",
    )
    index_bytes = raw["git_ls_files"].require_bytes(
        maximum_bytes=_MAX_SMALL_NATIVE_BYTES,
        label="Git ls-files authority",
    )
    submodule_bytes = raw["git_submodule_status"].require_bytes(
        maximum_bytes=_MAX_SMALL_NATIVE_BYTES,
        label="Git submodule authority",
    )
    _require(status_bytes == b"", "Git porcelain status authority is not empty")
    _require(diff_bytes == b"", "Git binary diff authority is not empty")
    _require(raw["git_status"].content_sha256 == source.get("git_status_sha256"), "Git status authority digest mismatch")
    _require(raw["git_diff_binary"].content_sha256 == source.get("git_diff_binary_sha256"), "Git diff authority digest mismatch")
    _require(raw["git_ls_files"].content_sha256 == source.get("git_ls_files_sha256"), "Git ls-files authority digest mismatch")
    _require(raw["git_submodule_status"].content_sha256 == source.get("git_submodule_status_sha256"), "Git submodule authority digest mismatch")
    artifact_paths = _object(source.get("artifacts"), label="source H0 authority artifact paths")
    for source_name, role in (
        ("git_status", "git_status"),
        ("git_diff_binary", "git_diff_binary"),
        ("git_ls_files", "git_ls_files"),
        ("git_submodule_status", "git_submodule_status"),
        ("tracked_manifest", "tracked_manifest"),
        ("protected_ignored_manifest", "protected_ignored_manifest"),
    ):
        _require(
            artifact_paths.get(source_name) == str(raw[role].path),
            f"source H0 authority path mismatch: {source_name}",
        )
    manifest_rows = raw["tracked_manifest"].data
    manifest_digest, content_digest = _manifest_digests(manifest_rows)
    _require(manifest_digest == source.get("tracked_manifest_digest"), "tracked manifest digest mismatch")
    _require(content_digest == source.get("tracked_content_digest"), "tracked content digest mismatch")
    _require(int(source.get("tracked_file_count") or -1) == len(manifest_rows), "tracked manifest file count mismatch")
    _require(not source.get("nonzero_index_stage_paths"), "source H0 has nonzero Git index stages")
    _require(not source.get("submodule_dirty") and not source.get("submodule_worktree_changes"), "source H0 has dirty submodules")
    _require(not source.get("missing_tracked_paths") and not source.get("unsupported_tracked_paths"), "source H0 tracked tree is incomplete")
    protected_rows = raw["protected_ignored_manifest"].data
    protected_manifest_digest, protected_content_digest, protected_native_rows = (
        _protected_manifest_digests(protected_rows)
    )
    _require(
        protected_manifest_digest == source.get("protected_ignored_manifest_digest"),
        "protected ignored manifest digest mismatch",
    )
    _require(
        protected_content_digest == source.get("protected_ignored_content_digest"),
        "protected ignored content digest mismatch",
    )
    _require(
        int(source.get("protected_ignored_file_count") or -1) == len(protected_native_rows)
        and int(source.get("protected_ignored_present_count") or -1)
        == sum(row["kind"] != "missing" for row in protected_native_rows),
        "protected ignored manifest count mismatch",
    )
    _require(
        source.get("unsafe_protected_ignored_paths") in ([], ()),
        "source H0 contains unsafe protected ignored paths",
    )
    protected_policy = _object(
        source.get("protected_ignored_policy"),
        label="protected ignored policy",
    )
    _require(
        protected_policy.get("policy") == "explicit_reviewed_list"
        and protected_policy.get("broad_ignored_runtime_is_excluded") is True,
        "protected ignored source policy is invalid",
    )
    policy_rows = protected_policy.get("paths")
    _require(isinstance(policy_rows, list), "protected ignored source policy paths are invalid")
    by_path = {
        str(row.get("path") or ""): row
        for row in policy_rows
        if isinstance(row, Mapping)
    }
    _require(
        set(by_path) == set(REVIEWED_PROTECTED_IGNORED_PATHS)
        and all(
            row.get("reviewed") is True
            and row.get("git_ignored") is True
            and row.get("authority_class") == "protected_ignored_launcher_input"
            for row in by_path.values()
        ),
        "protected ignored source policy does not match the reviewed path set",
    )
    index_paths: set[str] = set()
    index_records = (
        [record for record in index_bytes.split(b"\0") if record]
        if b"\0" in index_bytes
        else index_bytes.splitlines()
    )
    for encoded_line in index_records:
        line = encoded_line.decode("utf-8", errors="surrogateescape")
        prefix, separator, path = line.partition("\t")
        fields = prefix.split()
        _require(separator == "\t" and len(fields) == 3, "Git ls-files authority row is malformed")
        mode, oid, stage = fields
        _require(mode.isdigit() and re.fullmatch(r"[0-9a-f]{40,64}", oid) is not None, "Git ls-files index identity is malformed")
        _require(stage == "0", f"Git ls-files contains a nonzero stage: {path}")
        index_paths.add(path)
    manifest_paths = {str(_native(row).get("path") or "") for row in manifest_rows}
    _require(index_paths == manifest_paths, "Git index paths differ from the tracked manifest")
    for line in submodule_bytes.decode("utf-8", errors="strict").splitlines():
        _require(not line.startswith(("+", "-", "U")), "Git submodule authority contains a dirty/unavailable submodule")
    _require(source.get("commit") and _SHA40.fullmatch(str(source["commit"])) is not None, "source H0 commit is invalid")
    return {
        "commit": source["commit"],
        "tracked_file_count": len(manifest_rows),
        "tracked_content_digest": content_digest,
        "protected_ignored_manifest_digest": protected_manifest_digest,
        "protected_ignored_content_digest": protected_content_digest,
        "protected_source_digest": protected_source_identity_digest(
            protected_manifest_digest,
            protected_content_digest,
        ),
    }


@dataclass(frozen=True)
class GatePolicy:
    verification_scope: str
    maximum_validity: timedelta
    semantic_validator: Callable[[Mapping[str, RawArtifact]], dict[str, Any]]


GATE_POLICIES: Mapping[str, GatePolicy] = {
    "cgroup_limits_verified": GatePolicy("end_to_end_real_host", timedelta(hours=24), _derive_cgroup),
    "external_watchdog_verified": GatePolicy("end_to_end_real_host", timedelta(hours=24), _derive_watchdog),
    "hard_stop_injection_verified": GatePolicy("end_to_end_real_host", timedelta(hours=24), _derive_hard_stop),
    "checkpoint_recovery_verified": GatePolicy("end_to_end_supervised_recovery", timedelta(hours=24), _derive_checkpoint),
    "source_drift_detection_verified": GatePolicy("end_to_end_supervised_drift", timedelta(hours=24), _derive_source_drift),
    "sample_schema_completeness_verified": GatePolicy("end_to_end_supervised_live_samples", timedelta(hours=6), _derive_samples),
    "production_security_sentinel_verified": GatePolicy("end_to_end_production_security", timedelta(hours=1), _derive_security),
    "all_mandatory_dependencies_verified": GatePolicy("end_to_end_live_dependencies", timedelta(hours=1), _derive_dependencies),
    "180_second_smoke_passed": GatePolicy("end_to_end_supervised_180s", timedelta(hours=2), _derive_smoke),
    "60_minute_rehearsal_passed": GatePolicy("end_to_end_supervised_60m_full_feature", timedelta(hours=4), _derive_rehearsal),
    "worktree_clean_and_frozen": GatePolicy("git_clean_source_freeze", timedelta(minutes=10), _derive_clean_source),
}


def _validate_unsealed_gate_evidence(
    path: Path, *, gate_name: str, commit: str, source_digest: str,
    protected_source_digest: str,
    qualification_campaign_uuid: str, now: datetime | None = None,
    registry: ValidationRegistry | None = None,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    capture_authority: Mapping[str, Any],
) -> dict[str, Any]:
    policy = GATE_POLICIES[gate_name]
    instant = (now or utc_now()).astimezone(timezone.utc)
    candidate = _exact_canonical_path(
        path,
        label=f"{gate_name} evidence path",
        must_exist=True,
    )
    active_registry = registry or ValidationRegistry()
    active_registry.register_evidence(candidate, gate_name)
    evidence_bytes, evidence_stat = _stable_read(candidate, label=f"{gate_name} evidence")
    evidence_sha = sha256_bytes(evidence_bytes)
    _require(evidence_stat.st_mode & 0o022 == 0, f"{gate_name} evidence is group/world writable")
    if expected_sha256 is not None:
        _require(evidence_sha == expected_sha256, f"{gate_name} evidence SHA-256 mismatch")
    if expected_size is not None:
        _require(len(evidence_bytes) == expected_size, f"{gate_name} evidence size mismatch")
    try:
        payload = _object(json.loads(evidence_bytes.decode("utf-8")), label=f"{gate_name} evidence")
    except Exception as exc:
        if isinstance(exc, GateBundleError):
            raise
        raise GateBundleError(f"cannot reparse {gate_name} evidence: {exc.__class__.__name__}: {exc}") from exc
    _validate_json_shape(payload, label=f"{gate_name} evidence")
    _require(payload.get("schema_version") == GATE_EVIDENCE_SCHEMA_VERSION, f"{gate_name} evidence schema is unsupported")
    _require(payload.get("gate_name") == gate_name, f"{gate_name} evidence gate identity mismatch")
    _require(payload.get("status") == "PASS" and payload.get("machine_verified") is True, f"{gate_name} evidence is not machine PASS")
    _require(payload.get("verification_scope") == policy.verification_scope, f"{gate_name} evidence verification scope mismatch")
    _require(payload.get("actual_execution") is True, f"{gate_name} evidence did not run an actual execution")
    _require(payload.get("simulated") is False, f"{gate_name} simulated evidence is forbidden")
    _require(payload.get("component_only") is False, f"{gate_name} component-only evidence is forbidden")
    _require("assertions" not in payload and "measurements" not in payload, f"{gate_name} self-declared assertions/measurements are forbidden")
    _require(payload.get("commit") == commit and payload.get("source_digest") == source_digest, f"{gate_name} evidence source identity mismatch")
    _require(
        payload.get("protected_source_digest") == protected_source_digest,
        f"{gate_name} evidence protected source identity mismatch",
    )
    _require(payload.get("qualification_campaign_uuid") == qualification_campaign_uuid, f"{gate_name} evidence campaign identity mismatch")
    checked_at = parse_utc(payload.get("checked_at"), label=f"{gate_name}.checked_at")
    valid_until = parse_utc(payload.get("valid_until"), label=f"{gate_name}.valid_until")
    _require(checked_at <= instant + _FUTURE_SKEW, f"{gate_name} evidence timestamp is in the future")
    _require(valid_until > instant and valid_until > checked_at, f"{gate_name} evidence is expired or has an empty validity window")
    _require(valid_until - checked_at <= policy.maximum_validity, f"{gate_name} evidence validity exceeds policy")
    references = _object(payload.get("raw_artifacts"), label=f"{gate_name}.raw_artifacts")
    specs = GATE_RAW_SPECS[gate_name]
    _require(set(references) == set(specs), f"{gate_name} raw artifact role set mismatch")
    capture_producer = _validate_process_receipt(
        capture_authority.get("producer"),
        label=f"{gate_name} sealed capture producer",
        required_kind=CAPTURE_PRODUCER_KIND,
    )
    capture_created_at = str(capture_authority.get("created_at") or "")
    parse_utc(capture_created_at, label=f"{gate_name} sealed capture created_at")
    native_execution = _validate_native_execution_receipt(
        capture_authority.get("native_execution"),
        gate=gate_name,
        commit=commit,
        source_digest=source_digest,
        protected_source_digest=protected_source_digest,
        campaign_uuid=qualification_campaign_uuid,
        checked_at=checked_at,
        expected_roles=set(specs),
    )
    structured_bytes = 0
    for role, spec in specs.items():
        reference = _object(
            references[role],
            label=f"{gate_name}.{role} raw reference",
        )
        declared_size = reference.get("size_bytes")
        _require(
            isinstance(declared_size, int)
            and not isinstance(declared_size, bool)
            and declared_size >= 0,
            f"{gate_name}.{role} raw size declaration is invalid",
        )
        if spec.media_type == "application/json":
            _require(
                declared_size <= _MAX_RAW_JSON_BYTES,
                f"{gate_name}.{role} raw JSON exceeds size limit",
            )
            structured_bytes += declared_size
        elif spec.media_type == "application/x-ndjson":
            _require(
                declared_size <= _MAX_RAW_NDJSON_BYTES,
                f"{gate_name}.{role} raw JSONL exceeds size limit",
            )
            structured_bytes += declared_size
    _require(
        structured_bytes <= _MAX_GATE_STRUCTURED_BYTES,
        f"{gate_name} aggregate structured raw authority exceeds size limit",
    )
    raw: dict[str, RawArtifact] = {}
    gate_decoded_nodes = 0
    gate_decoded_string_bytes = 0
    try:
        for role, spec in specs.items():
            raw[role] = _load_raw_artifact(
                _object(references[role], label=f"{gate_name}.{role} raw reference"),
                gate=gate_name,
                role=role,
                spec=spec,
                commit=commit,
                source_digest=source_digest,
                protected_source_digest=protected_source_digest,
                campaign_uuid=qualification_campaign_uuid,
                checked_at=checked_at,
                registry=active_registry,
                capture_producer=capture_producer,
                capture_created_at=capture_created_at,
            )
            gate_decoded_nodes += raw[role].decoded_nodes
            gate_decoded_string_bytes += raw[role].decoded_string_bytes
            _require(
                gate_decoded_nodes <= _MAX_GATE_DECODED_NODES,
                f"{gate_name} aggregate decoded JSON node budget exceeded",
            )
            _require(
                gate_decoded_string_bytes <= _MAX_GATE_DECODED_STRING_BYTES,
                f"{gate_name} aggregate decoded JSON string budget exceeded",
            )
        derived = policy.semantic_validator(raw)
        derived["native_execution"] = {
            "invocation_id": native_execution["receipt"]["invocation_id"],
            "activation_nonce": native_execution["receipt"]["activation_nonce"],
            "duration_seconds": native_execution["duration_seconds"],
            "producer": native_execution["producer"],
            "source_authority_sha256": native_execution["source_authority_sha256"],
        }
        if gate_name == "180_second_smoke_passed":
            _require(
                float(derived.get("runtime_seconds") or 0.0)
                <= native_execution["duration_seconds"] + _EXECUTION_WALL_MONOTONIC_TOLERANCE_SECONDS,
                "smoke runner duration exceeds supervised native execution boundary",
            )
        elif gate_name == "60_minute_rehearsal_passed":
            _require(
                float(derived.get("active_seconds") or 0.0)
                <= native_execution["duration_seconds"] + _EXECUTION_WALL_MONOTONIC_TOLERANCE_SECONDS,
                "rehearsal active duration exceeds supervised native execution boundary",
            )
        if gate_name == "worktree_clean_and_frozen":
            _require(derived.get("tracked_content_digest") == source_digest, "clean Git authority is bound to a different source digest")
            _require(
                derived.get("protected_source_digest") == protected_source_digest,
                "clean source authority is bound to different protected ignored inputs",
            )
            _require(derived.get("commit") == commit, "clean Git authority is bound to a different commit")
        if gate_name == "source_drift_detection_verified":
            _require(derived.get("h0_digest") == source_digest, "source-drift H0 is bound to a different source digest")
        for artifact in raw.values():
            _verify_raw_artifact_unchanged(artifact)
        result = dict(payload)
        result["_derived"] = derived
        result["_derived_sha256"] = sha256_bytes(canonical_json_bytes(derived))
        result["_raw_paths"] = [str(item.path) for item in raw.values()]
        final_bytes, final_stat = _stable_read(candidate, label=f"{gate_name} evidence final readback")
        _require(
            _stat_identity(final_stat) == _stat_identity(evidence_stat)
            and len(final_bytes) == len(evidence_bytes)
            and sha256_bytes(final_bytes) == evidence_sha,
            f"{gate_name} evidence changed during semantic validation",
        )
        result["_evidence_sha256"] = evidence_sha
        result["_evidence_size"] = len(evidence_bytes)
        return result
    finally:
        _close_raw_artifacts(raw)


def _exact_private_directory(path: Path | str, *, label: str) -> Path:
    try:
        raw_path = os.fspath(path)
    except TypeError as exc:
        raise GateBundleError(f"{label} must be a filesystem path") from exc
    _require(isinstance(raw_path, str) and bool(raw_path), f"{label} is missing")
    candidate = Path(raw_path)
    _require(candidate.is_absolute() and raw_path == str(candidate), f"{label} must be an exact absolute path")
    try:
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except Exception as exc:
        raise GateBundleError(f"cannot inspect {label}: {exc.__class__.__name__}: {exc}") from exc
    _require(resolved == candidate, f"{label} must be canonical")
    _require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), f"{label} is not a real directory")
    _require(info.st_uid == os.getuid(), f"{label} owner mismatch")
    _require(info.st_mode & 0o077 == 0, f"{label} is not private")
    return candidate


def _validate_capture_execution(
    value: object,
    *,
    producer: Mapping[str, Any],
    checked_at: datetime,
    label: str,
) -> dict[str, Any]:
    execution = _object(value, label=label)
    _require(
        set(execution) == {
            "started_at", "finished_at", "started_monotonic_ns",
            "finished_monotonic_ns", "producer",
        },
        f"{label} shape mismatch",
    )
    _require(
        _validate_process_receipt(
            execution.get("producer"), label=f"{label}.producer",
            required_kind=CAPTURE_PRODUCER_KIND,
        ) == dict(producer),
        f"{label} producer mismatch",
    )
    started = parse_utc(execution.get("started_at"), label=f"{label}.started_at")
    finished = parse_utc(execution.get("finished_at"), label=f"{label}.finished_at")
    started_ns = execution.get("started_monotonic_ns")
    finished_ns = execution.get("finished_monotonic_ns")
    _require(
        type(started_ns) is int and type(finished_ns) is int
        and 0 < started_ns < finished_ns,
        f"{label} monotonic boundary is invalid",
    )
    wall_seconds = (finished - started).total_seconds()
    monotonic_seconds = (finished_ns - started_ns) / 1_000_000_000.0
    _require(
        wall_seconds > 0
        and abs(wall_seconds - monotonic_seconds)
        <= _EXECUTION_WALL_MONOTONIC_TOLERANCE_SECONDS,
        f"{label} wall/monotonic boundary mismatch",
    )
    _require(finished <= checked_at + _FUTURE_SKEW, f"{label} finishes after gate capture")
    return execution


def validate_gate_attempt(
    attempt_manifest_path: Path,
    *,
    gate_name: str,
    commit: str,
    source_digest: str,
    protected_source_digest: str,
    qualification_campaign_uuid: str,
    now: datetime | None = None,
    registry: ValidationRegistry | None = None,
    expected_manifest_sha256: str | None = None,
    expected_manifest_size: int | None = None,
) -> dict[str, Any]:
    """Validate one sealed writer attempt; loose evidence paths are forbidden."""

    instant = (now or utc_now()).astimezone(timezone.utc)
    manifest_path = _exact_canonical_path(
        attempt_manifest_path,
        label=f"{gate_name} qualification attempt manifest",
        must_exist=True,
    )
    manifest_bytes, manifest_stat = _stable_read(
        manifest_path,
        label=f"{gate_name} qualification attempt manifest",
        maximum_bytes=_MAX_CONTROL_JSON_BYTES,
    )
    manifest_sha = sha256_bytes(manifest_bytes)
    _require(manifest_stat.st_uid == os.getuid(), f"{gate_name} attempt manifest owner mismatch")
    _require(manifest_stat.st_mode & 0o077 == 0, f"{gate_name} attempt manifest is not private")
    if expected_manifest_sha256 is not None:
        _require(manifest_sha == expected_manifest_sha256, f"{gate_name} attempt manifest SHA mismatch")
    if expected_manifest_size is not None:
        _require(len(manifest_bytes) == expected_manifest_size, f"{gate_name} attempt manifest size mismatch")
    try:
        manifest = _object(json.loads(manifest_bytes), label=f"{gate_name} qualification attempt manifest")
    except Exception as exc:
        if isinstance(exc, GateBundleError):
            raise
        raise GateBundleError(f"cannot parse {gate_name} attempt manifest: {exc.__class__.__name__}: {exc}") from exc
    _validate_json_shape(manifest, label=f"{gate_name} qualification attempt manifest")
    expected_manifest_fields = {
        "schema_version", "status", "machine_verified", "gate_name",
        "attempt_root", "context", "native_execution", "capture_execution",
        "evidence_path", "evidence_sha256", "evidence_size_bytes",
        "derived_sha256", "raw_artifacts", "finished_at",
    }
    _require(set(manifest) == expected_manifest_fields, f"{gate_name} attempt manifest shape mismatch")
    _require(manifest.get("schema_version") == QUALIFICATION_ATTEMPT_SCHEMA_VERSION, f"{gate_name} attempt manifest schema mismatch")
    _require(
        manifest.get("status") == "PASS" and manifest.get("machine_verified") is True,
        f"{gate_name} attempt manifest is not PASS",
    )
    _require(manifest.get("gate_name") == gate_name, f"{gate_name} attempt manifest gate mismatch")
    attempt_root = _exact_private_directory(manifest.get("attempt_root"), label=f"{gate_name} attempt root")
    _require(manifest_path == attempt_root / "attempt.json", f"{gate_name} attempt manifest is outside its sealed root")
    context = _object(manifest.get("context"), label=f"{gate_name} attempt context")
    _require(
        set(context) == {
            "schema_version", "qualification_campaign_uuid", "commit",
            "source_digest", "protected_source_digest", "source_authority",
            "producer", "created_at",
        },
        f"{gate_name} attempt context shape mismatch",
    )
    _require(context.get("schema_version") == "hackme.formal-qualification-context.v1", f"{gate_name} attempt context schema mismatch")
    _require(
        context.get("qualification_campaign_uuid") == qualification_campaign_uuid
        and context.get("commit") == commit
        and context.get("source_digest") == source_digest
        and context.get("protected_source_digest") == protected_source_digest,
        f"{gate_name} attempt context source/campaign mismatch",
    )
    producer = _validate_process_receipt(
        context.get("producer"),
        label=f"{gate_name} attempt capture producer",
        required_kind=CAPTURE_PRODUCER_KIND,
    )
    created_at = str(context.get("created_at") or "")
    parse_utc(created_at, label=f"{gate_name} attempt context created_at")
    source_authority = _object(context.get("source_authority"), label=f"{gate_name} attempt source authority")
    _require(
        set(source_authority) == {"path", "sha256", "size_bytes", "file_identity"},
        f"{gate_name} attempt source authority shape mismatch",
    )
    source_path = _exact_canonical_path(
        source_authority.get("path"),
        label=f"{gate_name} attempt source authority path",
        must_exist=True,
    )
    source_bytes, source_stat = _stable_read(
        source_path,
        label=f"{gate_name} attempt source authority",
        maximum_bytes=_MAX_RAW_JSON_BYTES,
    )
    _require(
        source_authority.get("sha256") == sha256_bytes(source_bytes)
        and source_authority.get("size_bytes") == len(source_bytes)
        and source_authority.get("file_identity") == _file_identity_mapping(source_stat),
        f"{gate_name} attempt source authority changed",
    )
    _validate_capture_execution(
        manifest.get("capture_execution"),
        producer=producer,
        checked_at=instant,
        label=f"{gate_name} capture execution",
    )
    evidence_value = manifest.get("evidence_path")
    _require(isinstance(evidence_value, str) and evidence_value, f"{gate_name} attempt evidence path is missing")
    evidence_path = _exact_canonical_path(
        evidence_value,
        label=f"{gate_name} sealed evidence path",
        must_exist=True,
    )
    _require(evidence_path == attempt_root / "evidence" / f"{gate_name}.json", f"{gate_name} evidence is outside its sealed attempt")
    raw_records_value = manifest.get("raw_artifacts")
    _require(isinstance(raw_records_value, list), f"{gate_name} attempt raw records are missing")
    raw_records: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(raw_records_value):
        record = _object(value, label=f"{gate_name} attempt raw record {index}")
        _require(
            set(record) == {
                "role", "native_path", "native_identity", "native_sha256",
                "captured_path", "captured_sha256", "captured_size", "reference",
            },
            f"{gate_name} attempt raw record shape mismatch",
        )
        role = str(record.get("role") or "")
        _require(role in GATE_RAW_SPECS[gate_name] and role not in raw_records, f"{gate_name} attempt raw role is invalid")
        raw_records[role] = record
    _require(set(raw_records) == set(GATE_RAW_SPECS[gate_name]), f"{gate_name} attempt raw role set mismatch")
    native_execution = _validate_native_execution_receipt(
        manifest.get("native_execution"),
        gate=gate_name,
        commit=commit,
        source_digest=source_digest,
        protected_source_digest=protected_source_digest,
        campaign_uuid=qualification_campaign_uuid,
        checked_at=parse_utc(manifest.get("finished_at"), label=f"{gate_name} attempt finished_at"),
        expected_roles=set(GATE_RAW_SPECS[gate_name]),
    )
    _require(
        native_execution["source_authority_sha256"] == source_authority["sha256"],
        f"{gate_name} native execution used another source authority",
    )
    for role, record in raw_records.items():
        reference = _object(record.get("reference"), label=f"{gate_name}.{role} attempt reference")
        _require(reference.get("artifact_role") == role, f"{gate_name}.{role} attempt reference role mismatch")
        _require(record.get("captured_path") == reference.get("path"), f"{gate_name}.{role} captured/reference path mismatch")
        _require(record.get("captured_sha256") == reference.get("sha256"), f"{gate_name}.{role} captured/reference SHA mismatch")
        _require(record.get("captured_size") == reference.get("size_bytes"), f"{gate_name}.{role} captured/reference size mismatch")
        native_record = _object(native_execution["artifacts"].get(role), label=f"{gate_name}.{role} native execution artifact")
        _require(
            set(native_record) == {"path", "file_identity", "sha256"},
            f"{gate_name}.{role} native execution artifact shape mismatch",
        )
        _require(
            native_record.get("path") == record.get("native_path")
            and native_record.get("file_identity") == record.get("native_identity")
            and native_record.get("sha256") == record.get("native_sha256"),
            f"{gate_name}.{role} native execution/capture mismatch",
        )
    validated = _validate_unsealed_gate_evidence(
        evidence_path,
        gate_name=gate_name,
        commit=commit,
        source_digest=source_digest,
        protected_source_digest=protected_source_digest,
        qualification_campaign_uuid=qualification_campaign_uuid,
        now=instant,
        registry=registry,
        expected_sha256=str(manifest.get("evidence_sha256") or ""),
        expected_size=int(manifest.get("evidence_size_bytes") or -1),
        capture_authority={
            "producer": producer,
            "created_at": created_at,
            "native_execution": manifest["native_execution"],
        },
    )
    _require(validated.get("_derived_sha256") == manifest.get("derived_sha256"), f"{gate_name} attempt derived digest mismatch")
    final_bytes, final_stat = _stable_read(
        manifest_path,
        label=f"{gate_name} attempt manifest final readback",
        maximum_bytes=_MAX_CONTROL_JSON_BYTES,
    )
    _require(
        final_bytes == manifest_bytes and _stat_identity(final_stat) == _stat_identity(manifest_stat),
        f"{gate_name} attempt manifest changed during validation",
    )
    validated["_attempt_manifest_sha256"] = manifest_sha
    validated["_attempt_manifest_size"] = len(manifest_bytes)
    validated["_attempt_manifest_path"] = str(manifest_path)
    validated["_evidence_path"] = str(evidence_path)
    return validated


def validate_gate_evidence(
    path: Path,
    *,
    attempt_manifest_path: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility entrypoint that refuses loose, hand-written evidence."""

    _require(attempt_manifest_path is not None, "sealed qualification attempt manifest is required")
    result = validate_gate_attempt(attempt_manifest_path, **kwargs)
    _require(
        _exact_canonical_path(
            path,
            label=f"{kwargs['gate_name']} evidence path",
            must_exist=True,
        ) == Path(result["_evidence_path"]),
        "loose evidence path does not belong to the sealed attempt",
    )
    return result


def _validate_evidence_reference(
    reference: Mapping[str, Any], *, gate_name: str, commit: str,
    source_digest: str, protected_source_digest: str,
    qualification_campaign_uuid: str, now: datetime,
    registry: ValidationRegistry,
) -> dict[str, Any]:
    expected_fields = {
        "schema_version", "path", "sha256", "size_bytes", "artifact_schema_version",
        "gate_name", "qualification_campaign_uuid", "commit", "source_digest",
        "protected_source_digest",
    }
    _require(set(reference) == expected_fields, f"{gate_name} evidence reference shape mismatch")
    _require(reference.get("schema_version") == GATE_ARTIFACT_REFERENCE_SCHEMA_VERSION, f"{gate_name} evidence reference schema mismatch")
    raw_path = reference.get("path")
    _require(isinstance(raw_path, str) and raw_path, f"{gate_name} evidence reference path is missing")
    path = _exact_canonical_path(
        raw_path,
        label=f"{gate_name} evidence reference path",
        must_exist=True,
    )
    content, stat_result = _stable_read(path, label=f"{gate_name} evidence")
    _require(isinstance(reference.get("size_bytes"), int) and not isinstance(reference.get("size_bytes"), bool), f"{gate_name} evidence size declaration is invalid")
    _require(len(content) > 0 and reference["size_bytes"] == len(content) == stat_result.st_size, f"{gate_name} evidence size mismatch")
    _require(reference.get("sha256") == sha256_bytes(content), f"{gate_name} evidence SHA-256 mismatch")
    _require(reference.get("artifact_schema_version") == QUALIFICATION_ATTEMPT_SCHEMA_VERSION, f"{gate_name} attempt declared schema mismatch")
    _require(reference.get("gate_name") == gate_name, f"{gate_name} evidence reference gate mismatch")
    _require(reference.get("qualification_campaign_uuid") == qualification_campaign_uuid, f"{gate_name} evidence reference campaign mismatch")
    _require(reference.get("commit") == commit and reference.get("source_digest") == source_digest, f"{gate_name} evidence reference source mismatch")
    _require(
        reference.get("protected_source_digest") == protected_source_digest,
        f"{gate_name} evidence reference protected source mismatch",
    )
    return validate_gate_attempt(
        path, gate_name=gate_name, commit=commit, source_digest=source_digest,
        protected_source_digest=protected_source_digest,
        qualification_campaign_uuid=qualification_campaign_uuid, now=now,
        registry=registry, expected_manifest_sha256=str(reference["sha256"]),
        expected_manifest_size=int(reference["size_bytes"]),
    )


def validate_gate_bundle(
    path: Path,
    *,
    commit: str,
    source_authority: Mapping[str, Any] | None = None,
    source_digest: str | None = None,
    protected_source_digest: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Re-derive all formal gates from immutable native artifacts."""

    instant = (now or utc_now()).astimezone(timezone.utc)
    expected_commit = str(commit or "").lower()
    expected_digest, expected_protected_digest = _resolve_source_identity(
        source_authority=source_authority,
        source_digest=source_digest,
        protected_source_digest=protected_source_digest,
    )
    _require(_SHA40.fullmatch(expected_commit) is not None, "expected commit is not a full lowercase Git SHA")
    bundle_path = _exact_canonical_path(
        path,
        label="formal gate bundle path",
        must_exist=True,
    )
    bundle_bytes, bundle_stat = _stable_read(
        bundle_path,
        label="formal gate bundle",
        maximum_bytes=_MAX_CONTROL_JSON_BYTES,
    )
    try:
        payload = _object(
            json.loads(bundle_bytes.decode("utf-8", errors="strict")),
            label="formal gate bundle",
        )
    except Exception as exc:
        if isinstance(exc, GateBundleError):
            raise
        raise GateBundleError(
            f"cannot reparse formal gate bundle: {exc.__class__.__name__}: {exc}"
        ) from exc
    _validate_json_shape(payload, label="formal gate bundle")
    _require(payload.get("schema_version") == GATE_BUNDLE_SCHEMA_VERSION, "formal gate bundle schema is unsupported")
    _require(payload.get("commit") == expected_commit and payload.get("source_digest") == expected_digest, "formal gate bundle source identity mismatch")
    _require(
        payload.get("protected_source_digest") == expected_protected_digest,
        "formal gate bundle protected source identity mismatch",
    )
    campaign_uuid = str(payload.get("qualification_campaign_uuid") or "")
    _require(_UUIDISH.fullmatch(campaign_uuid) is not None, "formal gate bundle qualification campaign UUID is invalid")
    generated_at = parse_utc(payload.get("generated_at"), label="formal gate bundle generated_at")
    valid_until = parse_utc(payload.get("valid_until"), label="formal gate bundle valid_until")
    _require(generated_at <= instant + _FUTURE_SKEW and valid_until > instant and valid_until > generated_at, "formal gate bundle time window is invalid")
    declared_sha = str(payload.get("bundle_sha256") or "")
    _require(_SHA256.fullmatch(declared_sha) is not None and bundle_sha256(payload) == declared_sha, "formal gate bundle digest mismatch")
    _require(payload.get("ok") is True, "formal gate bundle aggregate status is not PASS")
    _require(payload.get("required_gates") == list(REQUIRED_FORMAL_GATES), "formal gate bundle required gate order mismatch")
    gates = _object(payload.get("gates"), label="formal gate bundle gates")
    _require(set(gates) == set(REQUIRED_FORMAL_GATES), "formal gate bundle must contain exactly the reviewed qualification gates")
    registry = ValidationRegistry(bundle_path=bundle_path)
    expiries: list[datetime] = []
    for gate_name in REQUIRED_FORMAL_GATES:
        row = _object(gates[gate_name], label=f"gate {gate_name}")
        _require(row.get("status") == "PASS" and row.get("machine_verified") is True, f"{gate_name} row is not machine PASS")
        _require(row.get("verification_scope") == GATE_POLICIES[gate_name].verification_scope, f"{gate_name} row scope mismatch")
        _require(row.get("qualification_campaign_uuid") == campaign_uuid, f"{gate_name} row campaign mismatch")
        _require(row.get("commit") == expected_commit and row.get("source_digest") == expected_digest, f"{gate_name} row source mismatch")
        _require(
            row.get("protected_source_digest") == expected_protected_digest,
            f"{gate_name} row protected source mismatch",
        )
        evidence = _validate_evidence_reference(
            _object(row.get("artifact"), label=f"{gate_name}.artifact"),
            gate_name=gate_name, commit=expected_commit, source_digest=expected_digest,
            protected_source_digest=expected_protected_digest,
            qualification_campaign_uuid=campaign_uuid, now=instant, registry=registry,
        )
        checked_at = parse_utc(row.get("checked_at"), label=f"{gate_name}.checked_at")
        expires = parse_utc(row.get("valid_until"), label=f"{gate_name}.valid_until")
        _require(checked_at == parse_utc(evidence["checked_at"], label=f"{gate_name}.evidence.checked_at"), f"{gate_name} checked_at differs from evidence")
        _require(expires == parse_utc(evidence["valid_until"], label=f"{gate_name}.evidence.valid_until"), f"{gate_name} valid_until differs from evidence")
        _require(row.get("derived_sha256") == evidence["_derived_sha256"], f"{gate_name} derived result digest mismatch")
        _require(generated_at >= checked_at - _FUTURE_SKEW, f"{gate_name} evidence was created after its bundle")
        expiries.append(expires)
    _require(valid_until == min(expiries), "formal gate bundle expiry must equal its earliest gate expiry")
    final_bundle_bytes, final_bundle_stat = _stable_read(
        bundle_path,
        label="formal gate bundle final readback",
        maximum_bytes=_MAX_CONTROL_JSON_BYTES,
    )
    _require(
        _stat_identity(final_bundle_stat) == _stat_identity(bundle_stat)
        and final_bundle_bytes == bundle_bytes,
        "formal gate bundle changed during semantic validation",
    )
    return payload


def _evidence_reference(
    path: Path, *, gate_name: str, commit: str, source_digest: str,
    protected_source_digest: str,
    qualification_campaign_uuid: str, expected_sha256: str,
    expected_size: int,
) -> dict[str, Any]:
    canonical = _exact_canonical_path(
        path,
        label=f"{gate_name} evidence reference path",
        must_exist=True,
    )
    content, stat_result = _stable_read(canonical, label=f"{gate_name} attempt reference")
    _require(
        len(content) == expected_size
        and stat_result.st_size == expected_size
        and sha256_bytes(content) == expected_sha256,
        f"{gate_name} attempt changed before bundle composition",
    )
    return {
        "schema_version": GATE_ARTIFACT_REFERENCE_SCHEMA_VERSION,
        "path": str(canonical),
        "sha256": expected_sha256,
        "size_bytes": expected_size,
        "artifact_schema_version": QUALIFICATION_ATTEMPT_SCHEMA_VERSION,
        "gate_name": gate_name,
        "qualification_campaign_uuid": qualification_campaign_uuid,
        "commit": commit,
        "source_digest": source_digest,
        "protected_source_digest": protected_source_digest,
    }


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def build_gate_bundle(
    output_path: Path, *, commit: str,
    source_authority: Mapping[str, Any] | None = None,
    source_digest: str | None = None,
    protected_source_digest: str | None = None,
    qualification_campaign_uuid: str, evidence_paths: Mapping[str, Path],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose a bundle only after re-deriving every gate from raw authority."""

    instant = (now or utc_now()).astimezone(timezone.utc)
    expected_source_digest, expected_protected_digest = _resolve_source_identity(
        source_authority=source_authority,
        source_digest=source_digest,
        protected_source_digest=protected_source_digest,
    )
    _require(set(evidence_paths) == set(REQUIRED_FORMAL_GATES), "attempt manifest set must contain exactly the reviewed qualification gates")
    destination = _exact_canonical_path(
        output_path,
        label="formal gate bundle output path",
        must_exist=False,
    )
    registry = ValidationRegistry(bundle_path=destination)
    gates: dict[str, dict[str, Any]] = {}
    expiries: list[datetime] = []
    for gate_name in REQUIRED_FORMAL_GATES:
        attempt_path = _exact_canonical_path(
            evidence_paths[gate_name],
            label=f"{gate_name} attempt manifest input path",
            must_exist=True,
        )
        evidence = validate_gate_attempt(
            attempt_path, gate_name=gate_name, commit=commit,
            source_digest=expected_source_digest,
            protected_source_digest=expected_protected_digest,
            qualification_campaign_uuid=qualification_campaign_uuid,
            now=instant, registry=registry,
        )
        checked_at = parse_utc(evidence["checked_at"], label=f"{gate_name}.checked_at")
        valid_until = parse_utc(evidence["valid_until"], label=f"{gate_name}.valid_until")
        expiries.append(valid_until)
        gates[gate_name] = {
            "status": "PASS",
            "machine_verified": True,
            "verification_scope": GATE_POLICIES[gate_name].verification_scope,
            "qualification_campaign_uuid": qualification_campaign_uuid,
            "commit": commit,
            "source_digest": expected_source_digest,
            "protected_source_digest": expected_protected_digest,
            "checked_at": format_utc(checked_at),
            "valid_until": format_utc(valid_until),
            "derived_sha256": evidence["_derived_sha256"],
            "artifact": _evidence_reference(
                attempt_path, gate_name=gate_name, commit=commit,
                source_digest=expected_source_digest,
                protected_source_digest=expected_protected_digest,
                qualification_campaign_uuid=qualification_campaign_uuid,
                expected_sha256=evidence["_attempt_manifest_sha256"],
                expected_size=evidence["_attempt_manifest_size"],
            ),
        }
    payload: dict[str, Any] = {
        "schema_version": GATE_BUNDLE_SCHEMA_VERSION,
        "qualification_campaign_uuid": qualification_campaign_uuid,
        "generated_at": format_utc(instant),
        "valid_until": format_utc(min(expiries)),
        "commit": commit,
        "source_digest": expected_source_digest,
        "protected_source_digest": expected_protected_digest,
        "required_gates": list(REQUIRED_FORMAL_GATES),
        "gates": gates,
        "ok": True,
    }
    payload["bundle_sha256"] = bundle_sha256(payload)
    atomic_write_json(destination, payload)
    return validate_gate_bundle(
        destination,
        commit=commit,
        source_digest=expected_source_digest,
        protected_source_digest=expected_protected_digest,
        now=instant,
    )


def _parse_evidence(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        name, separator, path = str(value).partition("=")
        if not separator or name not in REQUIRED_FORMAL_GATES or not path:
            raise GateBundleError(f"invalid --evidence value: {value!r}")
        if name in result:
            raise GateBundleError(f"duplicate --evidence gate: {name}")
        result[name] = Path(path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--source-authority", type=Path)
    parser.add_argument("--source-digest")
    parser.add_argument("--protected-source-digest")
    parser.add_argument("--qualification-campaign-uuid", required=True)
    parser.add_argument("--evidence", action="append", default=[], metavar="GATE=PATH")
    args = parser.parse_args(argv)
    try:
        result = build_gate_bundle(
            args.output,
            commit=str(args.commit),
            source_authority=(
                _load_json_object(args.source_authority, label="source authority")
                if args.source_authority else None
            ),
            source_digest=str(args.source_digest) if args.source_digest else None,
            protected_source_digest=(
                str(args.protected_source_digest)
                if args.protected_source_digest else None
            ),
            qualification_campaign_uuid=str(args.qualification_campaign_uuid),
            evidence_paths=_parse_evidence(args.evidence),
        )
    except Exception as exc:
        print(json.dumps({
            "schema_version": GATE_BUNDLE_SCHEMA_VERSION,
            "ok": False,
            "classification": "FAIL_HARNESS",
            "error": f"{exc.__class__.__name__}: {exc}",
        }, sort_keys=True))
        return 1
    print(json.dumps({
        "schema_version": result["schema_version"],
        "bundle_sha256": result["bundle_sha256"],
        "valid_until": result["valid_until"],
        "ok": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
