#!/usr/bin/env python3
"""Outside-cgroup supervisor for the managed operational campaign.

This module is entered only through operational_campaign_admission.py for
rehearsal, reliability-soak, and formal campaigns.
It freezes source, creates and verifies a delegated cgroup, launches the
campaign runner inside that scope, starts an independent watchdog outside the
scope, and releases the runner only after every startup proof is durable.
"""

from __future__ import annotations

import sys


if __name__ == "__main__":
    # Direct execution would load every supervisor dependency before the
    # pre-import block-I/O gate.  The dormant admission entrypoint is required.
    raise SystemExit(2)


import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.campaign_cgroup import (  # noqa: E402
    MANDATORY_MANAGED_ROLES,
    CampaignCgroup,
    CampaignCgroupError,
)
from scripts.testing.audit_evidence_triad import (  # noqa: E402
    SCHEMA_PATH as AUDIT_EVIDENCE_SCHEMA_PATH,
    SCHEMA_VERSION as AUDIT_EVIDENCE_SCHEMA_VERSION,
    AuditEvidencePaths,
    capture_audit_evidence,
    validate_audit_evidence_receipt,
)
from scripts.testing.campaign_comfyui_backend import (  # noqa: E402
    CampaignComfyUIBackend,
    ComfyUIBackendConfig,
)
from scripts.testing.campaign_control_channel import (  # noqa: E402
    ControlChannelError,
    PeerIdentity,
    authenticate_connection,
    create_server as create_control_server,
    derive_runner_auth_key,
    sign_authenticated_payload,
    socket_permissions,
    verify_authenticated_payload,
)
from scripts.testing.campaign_gate_bundle import (  # noqa: E402
    GATE_BUNDLE_SCHEMA_VERSION,
    REQUIRED_FORMAL_GATES,
    GateBundleError,
    validate_gate_bundle as validate_hardening_gate_bundle,
)
from scripts.testing.campaign_source_freeze import (  # noqa: E402
    FULL_CONTENT_EVIDENCE,
    METADATA_CONTENT_EVIDENCE,
    GitSourceFreezer,
    SourceFreezeError,
)
from scripts.testing.campaign_observability import (  # noqa: E402
    HostStartupBlockIoSampler,
    collect_host_startup_safety_preflight,
    wait_for_host_safety_preflight,
)
from scripts.testing.campaign_secret_scan import (  # noqa: E402
    CONTROL_SNAPSHOT_DURABILITY_MANIFEST,
    CONTROL_SNAPSHOT_DURABILITY_PER_FILE,
    ControlSnapshotConfig,
    SecretScanConfig,
    build_sensitive_needle_inventory,
    scan_campaign_secret_files,
    scan_campaign_secrets,
    snapshot_control_evidence,
)
from scripts.testing.campaign_state import (  # noqa: E402
    CampaignState,
    CampaignStateError,
    CampaignStateMachine,
)
from scripts.testing.campaign_watchdog import (  # noqa: E402
    INCIDENT_EXIT_CODE,
    CgroupIdentity,
    WatchdogConfig,
    WatchdogPaths,
    atomic_write_json,
    build_watchdog_command,
    capture_process_identity,
    load_json,
)
from scripts.testing.campaign_runtime_contract import (  # noqa: E402
    MIN_FORMAL_SECONDS,
    Credentials,
    SUPERVISED_LOAD_POLICIES,
    SUPERVISED_RUNNER_PROFILE_OPTIONS,
    SUPERVISED_RUNNER_PROFILES,
    validate_control_root,
)
from scripts.testing.operational_campaign_runner_admission import (  # noqa: E402
    DEFAULT_POLL_SECONDS as STAGED_IMPORT_POLL_SECONDS,
    DEFAULT_STAGE_TIMEOUT_SECONDS as STAGED_IMPORT_STAGE_TIMEOUT_SECONDS,
    HARD_IO_EXIT_CODE as STAGED_IMPORT_HARD_IO_EXIT_CODE,
    HARD_IO_PRESSURE_MAXIMUM as STAGED_IMPORT_HARD_IO_PRESSURE_MAXIMUM,
    IMPORT_PACING_SECONDS as STAGED_IMPORT_PACING_SECONDS,
    NESTED_IMPORT_GUARD_MODE as STAGED_IMPORT_NESTED_IMPORT_GUARD_MODE,
    PRE_RECEIPT_BARRIER_MODE as STAGED_IMPORT_PRE_RECEIPT_BARRIER_MODE,
    POST_RECEIPT_BARRIER_MODE as STAGED_IMPORT_POST_RECEIPT_BARRIER_MODE,
    PROFILE_MODULES as STAGED_IMPORT_PROFILE_MODULES,
    SCHEMA_VERSION as STAGED_IMPORT_SCHEMA_VERSION,
    SOFT_IO_PRESSURE_MAXIMUM as STAGED_IMPORT_SOFT_IO_PRESSURE_MAXIMUM,
    TARGET_MODULES as STAGED_IMPORT_TARGET_MODULES,
)


SUPERVISOR_SCHEMA_VERSION = "hackme.campaign-supervisor.v1"
FORMAL_AUTHORIZATION_SCHEMA_VERSION = "hackme.formal-24h-authorization.v1"
SAFETY_STOP_GRACE_SECONDS = 15.0
WATCHDOG_LIVENESS_TIMEOUT_SECONDS = 10.0
HOST_SAFETY_PREFLIGHT_TIMEOUT_SECONDS = 120.0
HOST_SAFETY_ACTIVATION_TIMEOUT_SECONDS = 120.0
HOST_SAFETY_RUNNER_LAUNCH_TIMEOUT_SECONDS = 120.0
HOST_SAFETY_STARTUP_SETTLE_CONSECUTIVE_SAMPLES = 60
GATED_HEARTBEAT_MINIMUM_INTERVAL_SECONDS = 30.0
SOURCE_CAPTURE_SAFETY_TIMEOUT_SECONDS = 120.0
SOURCE_CAPTURE_CHECKPOINT_EVIDENCE_LIMIT = 512
SOURCE_CAPTURE_IO_PACING_SECONDS = 0.1
STAGED_IMPORT_EVIDENCE_MAX_BYTES = 4 * 1024 * 1024
STAGED_IMPORT_SUPERVISOR_POLL_SECONDS = 1.0
POST_HARD_IO_QUIESCENCE_TIMEOUT_SECONDS = 600.0
POST_HARD_IO_REQUIRED_SAFE_SAMPLES = 60
AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED = True
HARD_IO_MINIMAL_GATE_ALLOWLIST = frozenset({
    "authenticated_control_channel_verified",
    "cgroup_event_baseline_verified",
    "cgroup_limits_verified",
    "comfyui_backend_cleanup_verified",
    "comfyui_backend_lifecycle_verified",
    "ephemeral_tls_private_keys_purged",
    "exception_authoritative_finalizer_completed",
    "exception_finalizer_fail_closed_policy_verified",
    "external_watchdog_verified",
    "formal_authorization_verified",
    "hard_io_failure_finalizer_load_suppressed",
    "host_safety_activation_verified",
    "host_safety_backend_startup_settled",
    "host_safety_preflight_verified",
    "host_safety_runner_import_settled",
    "host_safety_runner_launch_verified",
    "host_safety_state_initialization_settled",
    "managed_runner_exec_released",
    "prior_harness_gate_bundle_verified",
    "runner_and_watchdog_placement_verified",
    "runner_control_channel_authenticated",
    "runner_import_staged_verified",
    "source_baseline_frozen",
    "source_capture_host_safety_verified",
    "source_runtime_monitor_verified",
    "supervisor_forced_scope_stop",
    "watchdog_import_staged_verified",
    "watchdog_reciprocal_liveness_verified",
    "worktree_clean_and_frozen",
})
HARD_IO_MINIMAL_CLEANUP_ALLOWLIST = frozenset({
    "authenticated_control_channel",
    "comfyui_backend",
    "ephemeral_tls_private_keys",
    "hard_io_after_failure_receipt_quiescence",
    "hard_io_after_minimal_report_quiescence",
    "hard_io_immediate_termination",
    "post_hard_io_quiescence",
    "scope",
    "source_monitor",
    "watchdog",
})
SUPERVISOR_AUDIT_EVIDENCE_SCHEMA_VERSION = (
    "hackme.supervisor-audit-evidence-triad-index/v1"
)
SUPERVISOR_OWNED_RUNNER_OPTIONS = frozenset({
    "--campaign-root",
    "--duration-seconds",
    "--supervised",
    "--allow-short-duration",
    "--campaign-uuid",
    "--control-root",
    "--state-path",
    "--control-path",
    "--heartbeat-path",
    "--auth-socket",
    "--supervisor-pid",
    "--supervisor-start-ticks",
    "--supervisor-boot-id",
    "--supervisor-cgroup",
    "--checkpoint-path",
    "--checkpoint-mirror-path",
    "--source-freeze-path",
    "--activation-gate",
    "--supervisor-contract",
    "--cgroup-path",
    "--keep-servers",
    *SUPERVISED_RUNNER_PROFILE_OPTIONS.values(),
})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_campaign_root(path: Path) -> Path:
    root = Path(path).expanduser().resolve(strict=False)
    tmp = Path("/tmp").resolve()
    if root == tmp or tmp not in root.parents:
        raise ValueError(f"campaign root must be a new directory below /tmp: {root}")
    return root


def git_commit(repo_root: Path = ROOT) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or len(value) != 40:
        raise RuntimeError("cannot resolve frozen Git commit")
    return value


class SupervisorError(RuntimeError):
    """A formal startup proof or managed campaign invariant failed."""


def _load_required_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise SupervisorError(f"cannot read {label}: {exc.__class__.__name__}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SupervisorError(f"{label} must be a JSON object")
    return payload


def validate_formal_authorization(path: Path, *, commit: str, campaign_uuid: str) -> dict[str, Any]:
    payload = _load_required_json(path, label="formal authorization")
    errors: list[str] = []
    if payload.get("schema_version") != FORMAL_AUTHORIZATION_SCHEMA_VERSION:
        errors.append("schema_version")
    if payload.get("formal_24h_authorized") is not True:
        errors.append("formal_24h_authorized")
    if payload.get("commit") != commit:
        errors.append("commit")
    if int(payload.get("duration_seconds") or 0) != MIN_FORMAL_SECONDS:
        errors.append("duration_seconds")
    if not str(payload.get("authorized_by") or "").strip():
        errors.append("authorized_by")
    if not str(payload.get("authorized_at") or "").strip():
        errors.append("authorized_at")
    bound_uuid = str(payload.get("campaign_uuid") or "")
    if bound_uuid and bound_uuid != campaign_uuid:
        errors.append("campaign_uuid")
    if errors:
        raise SupervisorError("formal authorization is missing or mismatched: " + ", ".join(errors))
    return payload


def validate_gate_bundle(
    path: Path,
    *,
    commit: str,
    source_authority: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate v3 raw authority against the complete current H0 freeze."""

    try:
        return validate_hardening_gate_bundle(
            path,
            commit=commit,
            source_authority=source_authority,
        )
    except GateBundleError as exc:
        raise SupervisorError(f"harness gate bundle is not formal-ready: {exc}") from exc


@dataclass(frozen=True)
class SupervisorConfig:
    campaign_root: Path
    level: str
    duration_seconds: int
    authorization_file: Path | None = None
    gate_bundle_file: Path | None = None
    runner_extra_args: tuple[str, ...] = ()
    source_poll_seconds: float = 5.0
    watchdog_ready_timeout_seconds: float = 30.0
    watchdog_bootstrap_timeout_seconds: float = 300.0
    activation_timeout_seconds: float = 120.0
    # This separate deadline includes staged imports before the authenticated
    # runner hello.  The managed process remains dormant throughout it.
    runner_bootstrap_timeout_seconds: float = 600.0
    keep_scope_on_failure: bool = False
    comfyui_backend: ComfyUIBackendConfig | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "campaign_root", validate_campaign_root(self.campaign_root))
        if self.level not in {"smoke", "rehearsal", "soak", "formal"}:
            raise ValueError("level must be smoke, rehearsal, soak, or formal")
        required = {
            "smoke": 180,
            "rehearsal": 3600,
            "soak": MIN_FORMAL_SECONDS,
            "formal": MIN_FORMAL_SECONDS,
        }[self.level]
        if int(self.duration_seconds) != required:
            raise ValueError(f"{self.level} duration must be exactly {required} seconds")
        if self.level == "formal" and (self.authorization_file is None or self.gate_bundle_file is None):
            raise ValueError("formal campaign requires authorization_file and gate_bundle_file")
        if self.level in {"soak", "formal"} and self.keep_scope_on_failure:
            raise ValueError("soak/formal campaign cannot keep its cgroup scope after failure")
        if self.level in {"rehearsal", "soak", "formal"} and self.comfyui_backend is None:
            raise ValueError(
                f"{self.level} campaign requires an explicit managed ComfyUI backend configuration"
            )
        requested_options = {
            str(value).split("=", 1)[0]
            for value in self.runner_extra_args
            if str(value).startswith("--")
        }
        protected = sorted({
            option
            for option in requested_options
            if any(owned == option or owned.startswith(option) for owned in SUPERVISOR_OWNED_RUNNER_OPTIONS)
        })
        if protected:
            raise ValueError(
                "runner_extra_args cannot override supervisor-owned options: "
                + ", ".join(protected)
            )


class OperationalCampaignSupervisor:
    def __init__(self, config: SupervisorConfig):
        self.config = config
        self.root = config.campaign_root
        self.campaign_uuid = str(uuid.uuid4())
        self.control_root = validate_control_root(
            self.root,
            self.root.parent / f".{self.root.name}.control-{self.campaign_uuid}",
        )
        self.checkpoint_dir = self.control_root / "checkpoint"
        self.artifact_dir = self.root / "artifacts"
        self.control_artifact_dir = self.control_root / "artifacts"
        self.log_dir = self.control_root / "logs"
        self.state_path = self.checkpoint_dir / "campaign.state.json"
        self.control_path = self.checkpoint_dir / "campaign.control.json"
        self.heartbeat_path = self.checkpoint_dir / "campaign.heartbeat.json"
        self.checkpoint_path = self.checkpoint_dir / "campaign.checkpoint.json"
        self.runner_import_evidence_path = (
            self.checkpoint_dir / "runner.staged-import.json"
        )
        self.watchdog_import_evidence_path = (
            self.checkpoint_dir / "watchdog.staged-import.json"
        )
        self.checkpoint_mirror_path = (
            Path.home()
            / "logs"
            / "hackme_web_campaign_24h"
            / self.campaign_uuid
            / "campaign.checkpoint.json"
        )
        self.watchdog_ready_path = self.checkpoint_dir / "watchdog.status.json"
        self.watchdog_liveness_path = self.checkpoint_dir / "watchdog.liveness.json"
        self.watchdog_lock_path = self.checkpoint_dir / "watchdog.process.lock"
        self.auth_socket_dir = Path("/tmp") / f".hackme-web-ipc-{self.campaign_uuid}"
        self.auth_socket_path = self.auth_socket_dir / "watchdog.sock"
        self.activation_gate_path = self.checkpoint_dir / "campaign.activation.json"
        self.managed_exec_gate_path = self.checkpoint_dir / "campaign.exec.json"
        self.contract_path = self.checkpoint_dir / "supervisor.contract.json"
        self.final_path = self.artifact_dir / "campaign_supervisor.json"
        self.final_secret_scan_receipt = (
            self.control_root / "receipts" / "authoritative_final_secret_scan.json"
        )
        self.runner_stdout = self.log_dir / "campaign_runner.stdout"
        self.watchdog_stdout = self.log_dir / "campaign_watchdog.stdout"
        self.commit = ""
        self.supervisor_process_identity = capture_process_identity(os.getpid())
        self.state_machine = CampaignStateMachine(self.state_path)
        self.cgroup = CampaignCgroup(
            campaign_id=self.campaign_uuid,
            evidence_root=self.artifact_dir / "cgroup",
            # WSL commonly withholds the io controller from user.slice.
            # Reliability levels may use a live-verified idle I/O class;
            # rehearsal/formal capacity evidence still requires io.weight.
            allow_idle_io_fallback=config.level in {"smoke", "soak"},
        )
        self.comfyui_backend = (
            CampaignComfyUIBackend(
                config=config.comfyui_backend,
                campaign_cgroup=self.cgroup,
                evidence_root=self.control_artifact_dir / "comfyui_backend",
            )
            if config.comfyui_backend is not None
            else None
        )
        self.source_capture_safety_checkpoint_count = 0
        self.source_capture_safety_checkpoints: list[dict[str, Any]] = []
        self.freezer = GitSourceFreezer(
            ROOT,
            self.control_artifact_dir / "source",
            content_evidence_mode=(
                METADATA_CONTENT_EVIDENCE
                if config.level == "smoke"
                else FULL_CONTENT_EVIDENCE
            ),
            io_safety_checkpoint=self._source_capture_safety_checkpoint,
        )
        self.credentials = Credentials.load(managed_servers=True)
        self.runner: subprocess.Popen[Any] | None = None
        self.runner_pid = 0
        self.runner_log_handle: Any = None
        self.watchdog: subprocess.Popen[Any] | None = None
        self.watchdog_log_handle: Any = None
        self.auth_server: socket.socket | None = None
        self.auth_socket_path_fd: int | None = None
        self.auth_socket_dir_fd: int | None = None
        self.auth_socket_evidence: dict[str, Any] = {}
        self.auth_rejections: list[dict[str, str]] = []
        self.runner_auth_key: bytes | None = None
        self.watchdog_auth_key: bytes | None = None
        self.control_auth_sequences: dict[str, int] = {}
        self.watchdog_process_identity: Any | None = None
        self.watchdog_liveness_evidence: dict[str, Any] = {}
        self.last_gated_heartbeat_monotonic_ns = 0
        self.last_staged_import_safety_sample: dict[str, float] = {}
        self.source_h0: dict[str, Any] = {}
        self.authorization: dict[str, Any] = {}
        self.gate_bundle: dict[str, Any] = {}
        self.gates: dict[str, dict[str, Any]] = {}
        self.failure: str = ""
        self.campaign_root_initialized = False

    def _gate(self, name: str, *, passed: bool, evidence: Any = None, error: str = "") -> dict[str, Any]:
        row = {
            "status": "PASS" if passed else "FAIL",
            "machine_verified": bool(passed),
            "checked_at": utc_now(),
            "evidence": evidence,
            "error": "" if passed else str(error),
        }
        self.gates[name] = row
        return row

    def _load_private_startup_evidence(
        self,
        path: Path,
        *,
        label: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read one private startup receipt without following path swaps."""

        path = Path(path)
        try:
            expected_parent = self.checkpoint_dir.resolve(strict=True)
            actual_parent = path.parent.resolve(strict=True)
            before = os.lstat(path)
        except Exception as exc:
            raise SupervisorError(
                f"cannot inspect {label}: {exc.__class__.__name__}: {exc}"
            ) from exc
        if actual_parent != expected_parent:
            raise SupervisorError(f"{label} is outside the private checkpoint directory")
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or int(before.st_uid) != os.getuid()
            or int(before.st_nlink) != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or int(before.st_size) <= 0
            or int(before.st_size) > STAGED_IMPORT_EVIDENCE_MAX_BYTES
        ):
            raise SupervisorError(f"{label} metadata is unsafe")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        content = bytearray()
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise SupervisorError(f"{label} changed before secure open")
            while len(content) <= STAGED_IMPORT_EVIDENCE_MAX_BYTES:
                block = os.read(descriptor, min(
                    64 * 1024,
                    STAGED_IMPORT_EVIDENCE_MAX_BYTES + 1 - len(content),
                ))
                if not block:
                    break
                content.extend(block)
            after_fd = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = os.lstat(path)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        if (
            len(content) != int(before.st_size)
            or len(content) > STAGED_IMPORT_EVIDENCE_MAX_BYTES
            or not stat.S_ISREG(after_fd.st_mode)
            or int(after_fd.st_uid) != os.getuid()
            or int(after_fd.st_nlink) != 1
            or stat.S_IMODE(after_fd.st_mode) != 0o600
            or not stat.S_ISREG(after.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or int(after.st_uid) != os.getuid()
            or int(after.st_nlink) != 1
            or stat.S_IMODE(after.st_mode) != 0o600
            or (
                after_fd.st_dev,
                after_fd.st_ino,
                after_fd.st_size,
                after_fd.st_mtime_ns,
            )
            != identity_before
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            != identity_before
        ):
            raise SupervisorError(f"{label} changed while being verified")
        try:
            payload = json.loads(bytes(content).decode("utf-8"))
        except Exception as exc:
            raise SupervisorError(
                f"cannot decode {label}: {exc.__class__.__name__}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise SupervisorError(f"{label} must be a JSON object")
        return payload, {
            "path": str(path),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "mode": "0o600",
            "uid": int(before.st_uid),
            "link_count": int(before.st_nlink),
            "nofollow_stable": True,
        }

    @staticmethod
    def _finite_nonnegative_number(value: Any) -> bool:
        return bool(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) == float(value)
            and float(value) not in {float("inf"), float("-inf")}
            and float(value) >= 0.0
        )

    def _staged_import_command(
        self,
        *,
        profile: str,
        evidence_path: Path,
        target_args: Sequence[str],
    ) -> list[str]:
        if profile not in STAGED_IMPORT_PROFILE_MODULES:
            raise SupervisorError(f"unknown staged import profile: {profile}")
        return [
            sys.executable,
            "-S",
            str(
                ROOT
                / "scripts"
                / "testing"
                / "operational_campaign_runner_admission.py"
            ),
            "--profile",
            profile,
            "--campaign-uuid",
            self.campaign_uuid,
            "--evidence-path",
            str(evidence_path),
            "--stage-timeout-seconds",
            str(STAGED_IMPORT_STAGE_TIMEOUT_SECONDS),
            "--poll-seconds",
            str(STAGED_IMPORT_POLL_SECONDS),
            "--",
            *[str(value) for value in target_args],
        ]

    def _verify_staged_import_evidence(
        self,
        *,
        profile: str,
        process_identity: Any,
    ) -> dict[str, Any]:
        gate_name = f"{profile}_import_staged_verified"
        evidence_path = {
            "runner": self.runner_import_evidence_path,
            "watchdog": self.watchdog_import_evidence_path,
        }.get(profile)
        if evidence_path is None or profile not in STAGED_IMPORT_PROFILE_MODULES:
            raise SupervisorError(f"unknown staged import profile: {profile}")
        metadata: dict[str, Any] = {}
        try:
            payload, metadata = self._load_private_startup_evidence(
                evidence_path,
                label=f"{profile} staged import evidence",
            )
            actual_identity = capture_process_identity(
                int(process_identity.pid)
            )
            identity_mismatches = [
                name
                for name in ("pid", "start_ticks", "boot_id", "cgroup_path")
                if getattr(actual_identity, name) != getattr(process_identity, name)
            ]
            if identity_mismatches:
                raise SupervisorError(
                    f"{profile} process identity changed during staged import "
                    "verification: " + ",".join(identity_mismatches)
                )
            expected_order = (
                "site",
                *tuple(STAGED_IMPORT_PROFILE_MODULES[profile]),
            )
            order_digest = hashlib.sha256(
                json.dumps(
                    list(expected_order),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest()
            binding = {
                "campaign_uuid": self.campaign_uuid,
                "profile": profile,
                "pid": int(process_identity.pid),
                "process_start_ticks": int(process_identity.start_ticks),
                "target_module": STAGED_IMPORT_TARGET_MODULES[profile],
                "module_order_sha256": order_digest,
            }
            binding_digest = hashlib.sha256(
                json.dumps(
                    binding,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest()
            expected_scalars = {
                "schema_version": STAGED_IMPORT_SCHEMA_VERSION,
                "verified": True,
                "status": "PASS",
                "campaign_uuid": self.campaign_uuid,
                "profile": profile,
                "pid": int(process_identity.pid),
                "process_start_ticks": int(process_identity.start_ticks),
                "python_no_site": True,
                "bootstrap_collector": "direct_proc_pressure_io",
                "site_initialization_mode": (
                    "site_paths_only_no_pth_or_customization"
                ),
                "target_module": STAGED_IMPORT_TARGET_MODULES[profile],
                "module_order_sha256": order_digest,
                "binding_sha256": binding_digest,
                "completed_module_count": len(expected_order),
                "soft_io_pressure_maximum": (
                    STAGED_IMPORT_SOFT_IO_PRESSURE_MAXIMUM
                ),
                "hard_io_pressure_maximum": (
                    STAGED_IMPORT_HARD_IO_PRESSURE_MAXIMUM
                ),
                "import_pacing_seconds": STAGED_IMPORT_PACING_SECONDS,
                "stage_timeout_seconds": STAGED_IMPORT_STAGE_TIMEOUT_SECONDS,
                "poll_seconds": STAGED_IMPORT_POLL_SECONDS,
                "runner_main_invoked": False,
                "pre_receipt_io_barrier": (
                    STAGED_IMPORT_PRE_RECEIPT_BARRIER_MODE
                ),
                "post_receipt_io_barrier": (
                    STAGED_IMPORT_POST_RECEIPT_BARRIER_MODE
                ),
                "failure_reason": "",
                "failed_module": "",
            }
            mismatches = [
                name
                for name, expected in expected_scalars.items()
                if payload.get(name) != expected
            ]
            strict_scalar_types = {
                "verified": bool,
                "pid": int,
                "process_start_ticks": int,
                "python_no_site": bool,
                "completed_module_count": int,
                "soft_io_pressure_maximum": float,
                "hard_io_pressure_maximum": float,
                "import_pacing_seconds": float,
                "stage_timeout_seconds": float,
                "poll_seconds": float,
                "runner_main_invoked": bool,
            }
            for name, expected_type in strict_scalar_types.items():
                if type(payload.get(name)) is not expected_type:
                    mismatches.append(f"type:{name}")
            if payload.get("module_order") != list(expected_order):
                mismatches.append("module_order")
            expected_collector = (
                "campaign_observability"
                if "scripts.testing.campaign_observability" in expected_order
                else "direct_proc_pressure_io"
            )
            if payload.get("collector_mode") != expected_collector:
                mismatches.append("collector_mode")
            nested_guard = payload.get("nested_import_guard")
            nested_guard_calls = 0
            nested_guard_loading_calls = 0
            nested_guard_maximum_avg10 = 0.0
            nested_guard_maximum_avg60 = 0.0
            if not isinstance(nested_guard, Mapping):
                mismatches.append("nested_import_guard")
            else:
                nested_guard_calls_value = nested_guard.get("call_count")
                nested_guard_loading_value = nested_guard.get(
                    "calls_loading_modules"
                )
                nested_guard_maximum = nested_guard.get(
                    "maximum_io_pressure"
                )
                if (
                    nested_guard.get("mode")
                    != STAGED_IMPORT_NESTED_IMPORT_GUARD_MODE
                    or type(nested_guard_calls_value) is not int
                    or int(nested_guard_calls_value) < 1
                    or type(nested_guard_loading_value) is not int
                    or int(nested_guard_loading_value) < 0
                    or int(nested_guard_loading_value)
                    > int(nested_guard_calls_value)
                    or type(nested_guard.get("pacing_seconds")) is not float
                    or nested_guard.get("pacing_seconds")
                    != STAGED_IMPORT_PACING_SECONDS
                    or type(nested_guard.get("restored_before_receipt"))
                    is not bool
                    or nested_guard.get("restored_before_receipt") is not True
                    or not isinstance(nested_guard_maximum, Mapping)
                ):
                    mismatches.append("nested_import_guard_shape")
                elif not all(
                    type(value) is float
                    and self._finite_nonnegative_number(value)
                    for value in (
                        nested_guard_maximum.get("avg10"),
                        nested_guard_maximum.get("avg60"),
                    )
                ):
                    mismatches.append("nested_import_guard_value")
                else:
                    nested_guard_calls = int(nested_guard_calls_value)
                    nested_guard_loading_calls = int(
                        nested_guard_loading_value
                    )
                    nested_guard_maximum_avg10 = float(
                        nested_guard_maximum["avg10"]
                    )
                    nested_guard_maximum_avg60 = float(
                        nested_guard_maximum["avg60"]
                    )
                    if (
                        nested_guard_maximum_avg10
                        > STAGED_IMPORT_HARD_IO_PRESSURE_MAXIMUM
                        or nested_guard_maximum_avg60
                        > STAGED_IMPORT_HARD_IO_PRESSURE_MAXIMUM
                    ):
                        mismatches.append("nested_import_guard_threshold")
            if payload.get("time_module_bootstrap") not in {
                "preloaded_by_interpreter",
                "explicit_import_directly_bracketed",
            }:
                mismatches.append("time_module_bootstrap")
            stages = payload.get("stages")
            if not isinstance(stages, list) or len(stages) != len(expected_order):
                mismatches.append("stages")
                stages = []
            maximum_avg10 = 0.0
            maximum_avg60 = 0.0
            total_waited_seconds = 0.0
            for index, expected_module in enumerate(expected_order):
                if index >= len(stages) or not isinstance(stages[index], Mapping):
                    mismatches.append(f"stage:{index}")
                    continue
                row = stages[index]
                if (
                    type(row.get("sequence")) is not int
                    or row.get("sequence") != index
                    or row.get("module") != expected_module
                ):
                    mismatches.append(f"stage_identity:{index}")
                phase_waited_seconds: list[float] = []
                for phase in ("pre_admission", "post_admission"):
                    admission = row.get(phase)
                    if not isinstance(admission, Mapping):
                        mismatches.append(f"stage_{phase}:{index}")
                        continue
                    sample_count = admission.get("sample_count")
                    waited = admission.get("waited_seconds")
                    maximum = admission.get("maximum")
                    admitted = admission.get("admitted")
                    if (
                        not isinstance(sample_count, int)
                        or isinstance(sample_count, bool)
                        or sample_count < 1
                        or sample_count > int(
                            STAGED_IMPORT_STAGE_TIMEOUT_SECONDS
                            / STAGED_IMPORT_POLL_SECONDS
                        ) + 2
                        or type(waited) is not float
                        or not self._finite_nonnegative_number(waited)
                        or not isinstance(maximum, Mapping)
                        or not isinstance(admitted, Mapping)
                    ):
                        mismatches.append(f"stage_{phase}_shape:{index}")
                        continue
                    values = (
                        maximum.get("avg10"),
                        maximum.get("avg60"),
                        admitted.get("avg10"),
                        admitted.get("avg60"),
                    )
                    if not all(
                        type(value) is float
                        and self._finite_nonnegative_number(value)
                        for value in values
                    ):
                        mismatches.append(f"stage_{phase}_value:{index}")
                        continue
                    maximum_avg10 = max(maximum_avg10, float(values[0]))
                    maximum_avg60 = max(maximum_avg60, float(values[1]))
                    total_waited_seconds += float(waited)
                    phase_waited_seconds.append(float(waited))
                    if (
                        float(values[0]) > STAGED_IMPORT_HARD_IO_PRESSURE_MAXIMUM
                        or float(values[1]) > STAGED_IMPORT_HARD_IO_PRESSURE_MAXIMUM
                        or float(values[2]) > STAGED_IMPORT_SOFT_IO_PRESSURE_MAXIMUM
                        or float(values[3]) > STAGED_IMPORT_SOFT_IO_PRESSURE_MAXIMUM
                        or float(values[0]) < float(values[2])
                        or float(values[1]) < float(values[3])
                        or float(waited)
                        > (
                            STAGED_IMPORT_STAGE_TIMEOUT_SECONDS
                            + STAGED_IMPORT_POLL_SECONDS
                        )
                    ):
                        mismatches.append(f"stage_{phase}_threshold:{index}")
                    if (
                        sample_count == 1
                        and (
                            float(values[0]) != float(values[2])
                            or float(values[1]) != float(values[3])
                        )
                    ) or (
                        sample_count > 1
                        and float(values[0])
                        <= STAGED_IMPORT_SOFT_IO_PRESSURE_MAXIMUM
                        and float(values[1])
                        <= STAGED_IMPORT_SOFT_IO_PRESSURE_MAXIMUM
                    ):
                        mismatches.append(
                            f"stage_{phase}_sample_consistency:{index}"
                        )
                if (
                    len(phase_waited_seconds) == 2
                    and sum(phase_waited_seconds)
                    > (
                        STAGED_IMPORT_STAGE_TIMEOUT_SECONDS
                        + STAGED_IMPORT_POLL_SECONDS
                    )
                ):
                    mismatches.append(f"stage_shared_deadline:{index}")
            if mismatches:
                raise SupervisorError(
                    f"{profile} staged import evidence mismatch: "
                    + ",".join(sorted(set(mismatches)))
                )
            gate_evidence = {
                **metadata,
                "schema_version": payload["schema_version"],
                "profile": profile,
                "pid": int(process_identity.pid),
                "process_start_ticks": int(process_identity.start_ticks),
                "target_module": payload["target_module"],
                "module_order_sha256": order_digest,
                "binding_sha256": binding_digest,
                "stage_count": len(expected_order),
                "nested_import_guard": {
                    "mode": STAGED_IMPORT_NESTED_IMPORT_GUARD_MODE,
                    "call_count": nested_guard_calls,
                    "calls_loading_modules": nested_guard_loading_calls,
                    "maximum_io_pressure": {
                        "avg10": round(nested_guard_maximum_avg10, 6),
                        "avg60": round(nested_guard_maximum_avg60, 6),
                    },
                    "restored_before_receipt": True,
                },
                "maximum_io_pressure": {
                    "avg10": round(maximum_avg10, 6),
                    "avg60": round(maximum_avg60, 6),
                },
                "total_waited_seconds": round(total_waited_seconds, 6),
                "hard_limit_preserved": True,
                "target_main_not_invoked_at_receipt": True,
                "ok": True,
            }
            self._gate(gate_name, passed=True, evidence=gate_evidence)
            return gate_evidence
        except Exception as exc:
            self._gate(
                gate_name,
                passed=False,
                evidence={
                    **metadata,
                    "profile": profile,
                    "path": str(evidence_path),
                    "verification_state": "failed",
                },
                error=f"{exc.__class__.__name__}: {exc}",
            )
            if isinstance(exc, SupervisorError):
                raise
            raise SupervisorError(
                f"{profile} staged import evidence verification failed"
            ) from exc

    def _poll_staged_import_host_safety(self, *, profile: str) -> None:
        """Observe long individual imports from the outside at most once/sec."""

        now = time.monotonic()
        previous = float(
            self.last_staged_import_safety_sample.get(profile) or 0.0
        )
        if previous and now - previous < STAGED_IMPORT_SUPERVISOR_POLL_SECONDS:
            return
        self.last_staged_import_safety_sample[profile] = now
        gate_name = f"{profile}_import_staged_verified"
        try:
            evidence = collect_host_startup_safety_preflight()
        except Exception as exc:
            self._gate(
                gate_name,
                passed=False,
                evidence={
                    "profile": profile,
                    "verification_state": "supervisor_telemetry_failed",
                },
                error=f"{exc.__class__.__name__}: {exc}",
            )
            raise SupervisorError(
                f"{profile} staged import host telemetry failed"
            ) from exc
        tripped = [str(item) for item in evidence.get("tripped") or ()]
        non_waitable = [
            reason for reason in tripped if reason != "HOST_IO_PRESSURE_HIGH"
        ]
        if evidence.get("errors") or non_waitable:
            failure_evidence = {
                **evidence,
                "profile": profile,
                "verification_state": "supervisor_safety_stop",
                "non_waitable": non_waitable,
            }
            self._gate(
                gate_name,
                passed=False,
                evidence=failure_evidence,
                error=",".join(non_waitable or ["HOST_TELEMETRY_INCOMPLETE"]),
            )
            raise SupervisorError(
                f"{profile} staged import supervisor safety stop: "
                + ",".join(non_waitable or ["HOST_TELEMETRY_INCOMPLETE"])
            )

    def prepare(self) -> None:
        if self.root.exists():
            raise SupervisorError(f"campaign root already exists: {self.root}")
        if self.control_root.exists() or self.control_root.is_symlink():
            raise SupervisorError(f"campaign control root already exists: {self.control_root}")
        self.control_root.mkdir(parents=False, mode=0o700)
        os.chmod(self.control_root, 0o700)
        for path in (
            self.checkpoint_dir,
            self.artifact_dir,
            self.control_artifact_dir,
            self.log_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.campaign_root_initialized = True
        self._gate(
            "authenticated_control_channel_verified",
            passed=False,
            evidence={
                "required": True,
                "implemented": AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED,
                "verification_state": "not_started",
                "required_transport": "unix_sock_seqpacket",
                "required_peer_binding": ["pid", "uid", "gid", "start_ticks", "boot_id", "cgroup"],
                "anti_replay": ["server_challenge", "client_nonce", "one_time_ack"],
                "session_authentication": ["runner", "watchdog", "hmac_sha256"],
            },
            error="authenticated watchdog handshake has not completed",
        )
        self._gate(
            "runner_control_channel_authenticated",
            passed=False,
            evidence={"verification_state": "not_started", "required": True},
            error="runner control handshake has not completed",
        )
        self._gate(
            "watchdog_reciprocal_liveness_verified",
            passed=False,
            evidence={"verification_state": "not_started", "required": True},
            error="authenticated watchdog liveness has not been observed",
        )
        for gate_name, profile in (
            ("runner_import_staged_verified", "runner"),
            ("watchdog_import_staged_verified", "watchdog"),
        ):
            self._gate(
                gate_name,
                passed=False,
                evidence={
                    "verification_state": "not_started",
                    "required": True,
                    "profile": profile,
                },
                error=f"{profile} staged import has not been verified",
            )
        if self.comfyui_backend is None:
            self.gates["comfyui_backend_lifecycle_verified"] = {
                "status": "NOT_EVALUATED",
                "machine_verified": False,
                "checked_at": utc_now(),
                "evidence": {
                    "configured": False,
                    "level": self.config.level,
                    "formal_eligible": False,
                },
                "error": "short smoke explicitly omitted the managed ComfyUI backend",
            }
        else:
            self._gate(
                "comfyui_backend_lifecycle_verified",
                passed=False,
                evidence={"configured": True, "verification_state": "not_started"},
                error="managed ComfyUI backend has not reached verified readiness",
            )
        self.commit = git_commit()
        if self.config.level == "formal":
            assert self.config.authorization_file is not None
            self.authorization = validate_formal_authorization(
                self.config.authorization_file,
                commit=self.commit,
                campaign_uuid=self.campaign_uuid,
            )
            self._gate("formal_authorization_verified", passed=True, evidence={"path": str(self.config.authorization_file)})

    def _require_authenticated_control_channel(self) -> None:
        gate = self.gates.get("authenticated_control_channel_verified") or {}
        if (
            gate.get("status") != "PASS"
            or gate.get("machine_verified") is not True
            or not AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED
        ):
            raise SupervisorError(
                "campaign activation blocked: authenticated supervisor control "
                "channel is not machine-verified"
            )

    def _open_authenticated_control_server(self) -> dict[str, Any]:
        if (
            self.auth_server is not None
            or self.auth_socket_path_fd is not None
            or self.auth_socket_dir_fd is not None
        ):
            raise SupervisorError("authenticated control server is already open")
        if self.auth_socket_dir.exists() or self.auth_socket_dir.is_symlink():
            raise SupervisorError("authenticated control socket directory already exists")
        if self.watchdog_auth_key is None:
            self.watchdog_auth_key = secrets.token_bytes(32)
        if len(self.watchdog_auth_key) != 32:
            raise SupervisorError("authenticated control session key is invalid")
        self.runner_auth_key = derive_runner_auth_key(self.watchdog_auth_key)
        if self.runner_auth_key == self.watchdog_auth_key:
            raise SupervisorError("runner and watchdog authentication keys are not separated")
        self.auth_socket_dir.mkdir(mode=0o700)
        directory = self.auth_socket_dir.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or stat.S_ISLNK(directory.st_mode)
            or int(directory.st_uid) != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise SupervisorError("authenticated control socket directory is not private")
        try:
            if not hasattr(os, "O_PATH"):
                raise SupervisorError("authenticated control socket requires Linux O_PATH pinning")
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            self.auth_socket_dir_fd = os.open(self.auth_socket_dir, directory_flags)
            pinned_directory = os.fstat(self.auth_socket_dir_fd)
            if (
                int(pinned_directory.st_dev) != int(directory.st_dev)
                or int(pinned_directory.st_ino) != int(directory.st_ino)
            ):
                raise SupervisorError("authenticated control socket directory changed while pinning")
            self.auth_socket_evidence = {
                "directory_mode": "0o700",
                "directory_device": int(directory.st_dev),
                "directory_inode": int(directory.st_ino),
                "directory_path_pinned": True,
            }
            self.auth_server = create_control_server(self.auth_socket_path)
            socket_evidence = socket_permissions(self.auth_socket_path)
            socket_flags = (
                os.O_PATH
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            self.auth_socket_path_fd = os.open(self.auth_socket_path, socket_flags)
            pinned_socket = os.fstat(self.auth_socket_path_fd)
            if (
                not stat.S_ISSOCK(pinned_socket.st_mode)
                or int(pinned_socket.st_dev) != int(socket_evidence["device"])
                or int(pinned_socket.st_ino) != int(socket_evidence["inode"])
            ):
                raise SupervisorError("authenticated control socket changed while pinning")
            self.auth_socket_evidence.update(socket_evidence)
            self.auth_socket_evidence["socket_path_pinned"] = True
            return dict(self.auth_socket_evidence)
        except Exception:
            self._close_authenticated_control_server()
            raise

    def _close_authenticated_control_server(self) -> dict[str, Any]:
        errors: list[str] = []
        expected = dict(self.auth_socket_evidence)
        server = self.auth_server
        socket_path_fd = self.auth_socket_path_fd
        socket_dir_fd = self.auth_socket_dir_fd
        self.auth_server = None
        self.auth_socket_path_fd = None
        self.auth_socket_dir_fd = None
        if server is not None:
            try:
                server.close()
            except Exception as exc:
                errors.append(f"close:{exc.__class__.__name__}")
        try:
            socket_present = self.auth_socket_path.exists() or self.auth_socket_path.is_symlink()
            if socket_present:
                metadata = self.auth_socket_path.lstat()
                if socket_path_fd is None:
                    errors.append("socket_path_pin_missing")
                    pinned_socket = None
                else:
                    pinned_socket = os.fstat(socket_path_fd)
                expected_device = int(expected.get("device") or 0)
                expected_inode = int(expected.get("inode") or 0)
                if (
                    pinned_socket is None
                    or not stat.S_ISSOCK(metadata.st_mode)
                    or int(metadata.st_uid) != os.getuid()
                    or (
                        pinned_socket is not None
                        and (
                            int(metadata.st_dev) != int(pinned_socket.st_dev)
                            or int(metadata.st_ino) != int(pinned_socket.st_ino)
                        )
                    )
                    or (expected_device and int(metadata.st_dev) != expected_device)
                    or (expected_inode and int(metadata.st_ino) != expected_inode)
                ):
                    errors.append("socket_path_identity_changed")
                else:
                    self.auth_socket_path.unlink()
            elif expected:
                errors.append("socket_path_missing")
        except Exception as exc:
            errors.append(f"unlink:{exc.__class__.__name__}")
        finally:
            if socket_path_fd is not None:
                try:
                    os.close(socket_path_fd)
                except Exception as exc:
                    errors.append(f"socket_pin_close:{exc.__class__.__name__}")
        try:
            directory_present = self.auth_socket_dir.exists() or self.auth_socket_dir.is_symlink()
            if directory_present:
                metadata = self.auth_socket_dir.lstat()
                if socket_dir_fd is None:
                    errors.append("socket_directory_pin_missing")
                    pinned_directory = None
                else:
                    pinned_directory = os.fstat(socket_dir_fd)
                if (
                    pinned_directory is None
                    or not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or int(metadata.st_uid) != os.getuid()
                    or (
                        pinned_directory is not None
                        and (
                            int(metadata.st_dev) != int(pinned_directory.st_dev)
                            or int(metadata.st_ino) != int(pinned_directory.st_ino)
                        )
                    )
                    or (
                        int(expected.get("directory_device") or 0)
                        and int(metadata.st_dev) != int(expected["directory_device"])
                    )
                    or (
                        int(expected.get("directory_inode") or 0)
                        and int(metadata.st_ino) != int(expected["directory_inode"])
                    )
                ):
                    errors.append("socket_directory_identity_changed")
                else:
                    self.auth_socket_dir.rmdir()
            elif expected:
                errors.append("socket_directory_missing")
        except Exception as exc:
            errors.append(f"rmdir:{exc.__class__.__name__}")
        finally:
            if socket_dir_fd is not None:
                try:
                    os.close(socket_dir_fd)
                except Exception as exc:
                    errors.append(f"directory_pin_close:{exc.__class__.__name__}")
        result = {
            "ok": not errors,
            "closed": server is not None,
            "socket_removed": not self.auth_socket_path.exists(),
            "directory_removed": not self.auth_socket_dir.exists(),
            "errors": errors,
        }
        if result["ok"]:
            self.auth_socket_evidence = {}
        return result

    def _fail_authenticated_control_channel(
        self,
        *,
        error: BaseException | str,
        reason: str,
    ) -> None:
        error_name = error.__class__.__name__ if isinstance(error, BaseException) else str(error)
        self._gate(
            "authenticated_control_channel_verified",
            passed=False,
            evidence={
                "required": True,
                "implemented": AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED,
                "verification_state": "failed",
                "reason": reason,
                "socket": dict(self.auth_socket_evidence),
                "rejected_connections": list(self.auth_rejections[-20:]),
            },
            error=error_name,
        )

    def _authenticate_runner_control(
        self,
        *,
        runner_identity: Any,
        deadline: float,
    ) -> dict[str, Any]:
        server = self.auth_server
        process = self.runner
        session_secret = self.runner_auth_key
        if server is None or process is None or session_secret is None:
            error = SupervisorError("authenticated runner channel is not listening")
            self._gate(
                "runner_control_channel_authenticated",
                passed=False,
                evidence={"verification_state": "failed", "reason": "server_not_listening"},
                error=str(error),
            )
            raise error
        expected_peer = PeerIdentity(int(runner_identity.pid), os.getuid(), os.getgid())
        while time.monotonic() < deadline:
            self._poll_staged_import_host_safety(profile="runner")
            if process.poll() is not None:
                raise SupervisorError(
                    "campaign runner exited before authenticated control handshake: "
                    f"returncode={process.returncode}"
                )
            remaining = max(0.01, deadline - time.monotonic())
            server.settimeout(min(0.1, remaining))
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                raise SupervisorError("authenticated runner socket accept failed") from exc
            try:
                handshake = authenticate_connection(
                    connection,
                    expected_campaign=self.campaign_uuid,
                    expected_peer=expected_peer,
                    expected_role="runner",
                    session_secret=session_secret,
                    timeout=min(2.0, remaining),
                )
            except ControlChannelError as exc:
                self.auth_rejections.append({
                    "role": "runner",
                    "error_code": exc.__class__.__name__,
                    "reason": str(exc)[:240],
                })
                continue
            finally:
                connection.close()
            actual = capture_process_identity(runner_identity.pid)
            mismatches = [
                field
                for field in ("pid", "start_ticks", "boot_id", "cgroup_path")
                if getattr(actual, field) != getattr(runner_identity, field)
            ]
            if mismatches:
                raise SupervisorError(
                    "runner /proc identity changed after SO_PEERCRED handshake: "
                    + ", ".join(mismatches)
                )
            placement = self.cgroup.assert_pid_membership(
                runner_identity.pid,
                role="campaign_runner_control_peer",
            )
            placement_mismatches = [
                field
                for field, expected in (
                    ("pid", actual.pid),
                    ("start_ticks", actual.start_ticks),
                    ("boot_id", actual.boot_id),
                    ("actual_cgroup", actual.cgroup_path),
                )
                if placement.get(field) != expected
            ]
            secret_hash = hashlib.sha256(session_secret).hexdigest()
            if (
                placement_mismatches
                or placement.get("inside_campaign_scope") is not True
                or handshake.get("session_secret_delivered") is not True
                or handshake.get("session_secret_sha256") != secret_hash
            ):
                raise SupervisorError(
                    "runner authenticated session binding is incomplete: "
                    + ", ".join(placement_mismatches or ["session_or_placement"])
                )
            evidence = {
                "verification_state": "verified",
                "peer_credentials": {
                    "pid": expected_peer.pid,
                    "uid": expected_peer.uid,
                    "gid": expected_peer.gid,
                },
                "process_identity": {
                    "pid": actual.pid,
                    "start_ticks": actual.start_ticks,
                    "boot_id": actual.boot_id,
                    "cgroup_path": actual.cgroup_path,
                },
                "placement": placement,
                "handshake": handshake,
                "session_secret_sha256": secret_hash,
                "session_secret_persisted": False,
                "ok": True,
            }
            self._gate(
                "runner_control_channel_authenticated",
                passed=True,
                evidence=evidence,
            )
            return evidence
        error = SupervisorError("runner did not connect to authenticated control socket before timeout")
        self._gate(
            "runner_control_channel_authenticated",
            passed=False,
            evidence={"verification_state": "failed", "reason": "runner_connection_timeout"},
            error=str(error),
        )
        raise error

    def _authenticate_watchdog_control(
        self,
        *,
        runner_identity: Any | None,
        watchdog_identity: Any,
        deadline: float,
    ) -> dict[str, Any]:
        server = self.auth_server
        process = self.watchdog
        session_secret = self.watchdog_auth_key
        if server is None or process is None or session_secret is None:
            error = SupervisorError("authenticated watchdog channel is not listening")
            self._fail_authenticated_control_channel(error=error, reason="server_not_listening")
            raise error
        expected_peer = PeerIdentity(int(process.pid), os.getuid(), os.getgid())
        while time.monotonic() < deadline:
            self._poll_staged_import_host_safety(profile="watchdog")
            if runner_identity is not None:
                self._refresh_gated_heartbeat(runner_identity)
            if process.poll() is not None:
                error = SupervisorError(
                    "external watchdog exited before authenticated control handshake: "
                    f"returncode={process.returncode}"
                )
                self._fail_authenticated_control_channel(
                    error=error,
                    reason="watchdog_exited_before_handshake",
                )
                raise error
            remaining = max(0.01, deadline - time.monotonic())
            server.settimeout(min(0.1, remaining))
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                self._fail_authenticated_control_channel(
                    error=exc,
                    reason="socket_accept_failed",
                )
                raise SupervisorError("authenticated control socket accept failed") from exc
            try:
                handshake = authenticate_connection(
                    connection,
                    expected_campaign=self.campaign_uuid,
                    expected_peer=expected_peer,
                    expected_role="watchdog",
                    session_secret=session_secret,
                    timeout=min(2.0, remaining),
                )
            except ControlChannelError as exc:
                self.auth_rejections.append({
                    "role": "watchdog",
                    "error_code": exc.__class__.__name__,
                    "reason": str(exc)[:240],
                })
                continue
            finally:
                connection.close()

            try:
                actual = capture_process_identity(process.pid)
                identity_mismatches: list[str] = []
                for field in ("pid", "start_ticks", "boot_id", "cgroup_path"):
                    if getattr(actual, field) != getattr(watchdog_identity, field):
                        identity_mismatches.append(field)
                if identity_mismatches:
                    raise SupervisorError(
                        "watchdog /proc identity changed after SO_PEERCRED handshake: "
                        + ", ".join(identity_mismatches)
                    )
                placement = self.cgroup.assert_watchdog_outside(process.pid)
                placement_mismatches = [
                    field
                    for field, expected in (
                        ("pid", actual.pid),
                        ("start_ticks", actual.start_ticks),
                        ("boot_id", actual.boot_id),
                        ("actual_cgroup", actual.cgroup_path),
                    )
                    if placement.get(field) != expected
                ]
                if placement_mismatches or placement.get("inside_campaign_scope") is not False:
                    raise SupervisorError(
                        "watchdog cgroup placement is not bound to authenticated identity: "
                        + ", ".join(placement_mismatches or ["inside_campaign_scope"])
                    )
                socket_evidence = socket_permissions(self.auth_socket_path)
            except Exception as exc:
                self._fail_authenticated_control_channel(
                    error=exc,
                    reason="post_handshake_identity_verification_failed",
                )
                if isinstance(exc, SupervisorError):
                    raise
                raise SupervisorError("authenticated watchdog identity verification failed") from exc

            evidence = {
                "required": True,
                "implemented": AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED,
                "verification_state": "verified",
                "socket": socket_evidence,
                "handshake": handshake,
                "peer_credentials": {
                    "pid": expected_peer.pid,
                    "uid": expected_peer.uid,
                    "gid": expected_peer.gid,
                },
                "process_identity": {
                    "pid": actual.pid,
                    "start_ticks": actual.start_ticks,
                    "boot_id": actual.boot_id,
                    "cgroup_path": actual.cgroup_path,
                    "state": actual.state,
                },
                "placement": placement,
                "rejected_connections": list(self.auth_rejections[-20:]),
                "anti_replay_verified": bool(
                    handshake.get("one_time") is True
                    and handshake.get("acknowledged") is True
                    and handshake.get("challenge_bytes") == 32
                    and handshake.get("client_nonce_bytes") == 32
                    and handshake.get("session_secret_delivered") is True
                    and handshake.get("session_secret_sha256")
                    == hashlib.sha256(session_secret).hexdigest()
                    and self.runner_auth_key is not None
                    and not secrets.compare_digest(self.runner_auth_key, session_secret)
                ),
                "role_separated_keys": True,
                "runner_control_channel": self.gates.get(
                    "runner_control_channel_authenticated"
                ),
                "ok": True,
            }
            runner_gate = self.gates.get("runner_control_channel_authenticated") or {}
            if (
                evidence["anti_replay_verified"] is not True
                or runner_gate.get("status") != "PASS"
                or runner_gate.get("machine_verified") is not True
            ):
                error = SupervisorError("watchdog challenge/nonce anti-replay proof is incomplete")
                self._fail_authenticated_control_channel(
                    error=error,
                    reason="anti_replay_proof_failed",
                )
                raise error
            self._gate(
                "authenticated_control_channel_verified",
                passed=True,
                evidence=evidence,
            )
            return evidence

        error = SupervisorError("watchdog did not connect to authenticated control socket before timeout")
        self._fail_authenticated_control_channel(error=error, reason="watchdog_connection_timeout")
        raise error

    def _verify_watchdog_liveness(self) -> dict[str, Any]:
        process = self.watchdog
        identity = self.watchdog_process_identity
        session_secret = self.watchdog_auth_key
        if process is None or identity is None or session_secret is None:
            raise SupervisorError("authenticated watchdog liveness prerequisites are missing")
        if process.poll() is not None:
            raise SupervisorError(
                f"watchdog exited before liveness verification: {process.returncode}"
            )
        try:
            payload = load_json(self.watchdog_liveness_path)
            previous = self.watchdog_liveness_evidence
            authentication = verify_authenticated_payload(
                payload,
                session_secret=session_secret,
                expected_campaign_uuid=self.campaign_uuid,
                expected_stream="watchdog_liveness",
                previous_sequence=int(previous.get("sequence") or 0),
                previous_payload_sha256=str(previous.get("payload_sha256") or ""),
            )
            watchdog = payload.get("watchdog")
            if not isinstance(watchdog, Mapping):
                raise SupervisorError("watchdog liveness identity is missing")
            expected = {
                "pid": identity.pid,
                "start_ticks": identity.start_ticks,
                "boot_id": identity.boot_id,
                "cgroup": identity.cgroup_path,
            }
            mismatches = [
                name
                for name, value in expected.items()
                if watchdog.get(name) != value
            ]
            liveness_ns = int(watchdog.get("monotonic_ns") or 0)
            if int(authentication.get("monotonic_ns") or 0) != liveness_ns:
                mismatches.append("monotonic_binding")
            now_ns = time.monotonic_ns()
            if liveness_ns <= 0 or liveness_ns > now_ns:
                mismatches.append("monotonic_range")
                age_seconds = float("inf")
            else:
                age_seconds = (now_ns - liveness_ns) / 1_000_000_000
                if age_seconds >= WATCHDOG_LIVENESS_TIMEOUT_SECONDS:
                    mismatches.append("stale")
            actual = capture_process_identity(process.pid)
            for field in ("pid", "start_ticks", "boot_id", "cgroup_path"):
                if getattr(actual, field) != getattr(identity, field):
                    mismatches.append(f"process_{field}")
            if mismatches:
                raise SupervisorError(
                    "authenticated watchdog liveness mismatch: "
                    + ", ".join(sorted(set(mismatches)))
                )
        except SupervisorError:
            raise
        except Exception as exc:
            raise SupervisorError(
                "authenticated watchdog liveness verification failed: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        evidence = {
            **authentication,
            "watchdog": dict(watchdog),
            "age_seconds": round(age_seconds, 6),
            "deadline_seconds": WATCHDOG_LIVENESS_TIMEOUT_SECONDS,
            "process_identity_reverified": True,
            "ok": True,
        }
        self.watchdog_liveness_evidence = evidence
        return evidence

    def _source_capture_safety_summary(self) -> dict[str, Any]:
        checkpoints = list(self.source_capture_safety_checkpoints)
        avg10_values = [
            float(row["io"]["avg10"])
            for row in checkpoints
            if isinstance(row.get("io"), Mapping)
            and isinstance(row["io"].get("avg10"), (int, float))
        ]
        avg60_values = [
            float(row["io"]["avg60"])
            for row in checkpoints
            if isinstance(row.get("io"), Mapping)
            and isinstance(row["io"].get("avg60"), (int, float))
        ]
        tripped = list(dict.fromkeys(
            reason
            for row in checkpoints
            for reason in row.get("tripped", [])
        ))
        return {
            "schema_version": "hackme.source-capture-host-safety.v1",
            "checkpoint_count": self.source_capture_safety_checkpoint_count,
            "retained_checkpoint_count": len(checkpoints),
            "checkpoints_truncated": (
                self.source_capture_safety_checkpoint_count > len(checkpoints)
            ),
            "all_safe": bool(
                self.source_capture_safety_checkpoint_count > 0
                and all(row.get("ok") is True for row in checkpoints)
                and not tripped
            ),
            "maximum_io_pressure": {
                "avg10": max(avg10_values) if avg10_values else None,
                "avg60": max(avg60_values) if avg60_values else None,
            },
            "waited_seconds": round(sum(
                float(row.get("waited_seconds") or 0.0)
                for row in checkpoints
            ), 6),
            "tripped": tripped,
            "checkpoints": checkpoints,
        }

    def _source_capture_safety_checkpoint(self, stage: str) -> None:
        evidence = wait_for_host_safety_preflight(
            timeout_seconds=SOURCE_CAPTURE_SAFETY_TIMEOUT_SECONDS,
            required_consecutive_safe=1,
            collector=collect_host_startup_safety_preflight,
        )
        checks = evidence.get("checks") or {}
        io_check = checks.get("host_io_pressure") or {}
        hard_check = checks.get("host_io_pressure_hard_limit") or {}
        admission_wait = evidence.get("admission_wait") or {}
        record = {
            "stage": str(stage),
            "at": evidence.get("at"),
            "ok": evidence.get("ok") is True,
            "io": io_check.get("value"),
            "hard_limit_exceeded": hard_check.get("exceeded") is True,
            "waited_seconds": float(admission_wait.get("waited_seconds") or 0.0),
            "sample_count": int(admission_wait.get("sample_count") or 0),
            "tripped": [str(item) for item in evidence.get("tripped") or ()],
        }
        self.source_capture_safety_checkpoint_count += 1
        if (
            len(self.source_capture_safety_checkpoints)
            < SOURCE_CAPTURE_CHECKPOINT_EVIDENCE_LIMIT
            or record["ok"] is not True
        ):
            self.source_capture_safety_checkpoints.append(record)
        if evidence.get("ok") is not True:
            summary = self._source_capture_safety_summary()
            reasons = record["tripped"] or ["UNKNOWN_HOST_RISK"]
            self._gate(
                "source_capture_host_safety_verified",
                passed=False,
                evidence=summary,
                error=",".join(reasons),
            )
            raise SourceFreezeError(
                f"source capture host safety checkpoint {stage} blocked: "
                + ",".join(reasons)
            )
        time.sleep(SOURCE_CAPTURE_IO_PACING_SECONDS)

    def _host_io_hard_limit_was_exceeded(self) -> bool:
        reason = "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED"
        for process in (self.runner, self.watchdog):
            if (
                process is not None
                and process.poll() == STAGED_IMPORT_HARD_IO_EXIT_CODE
            ):
                return True
        for gate in self.gates.values():
            evidence = gate.get("evidence")
            if not isinstance(evidence, Mapping):
                continue
            if reason in [str(item) for item in evidence.get("tripped") or ()]:
                return True
        return reason in self.failure

    def _terminate_processes_after_hard_io(self) -> dict[str, Any]:
        """Kill campaign bootstrap groups without writing control artifacts."""

        rows: dict[str, Any] = {}
        backend_process = (
            self.comfyui_backend.process
            if self.comfyui_backend is not None
            else None
        )
        targets = [
            ("runner", self.runner, self.runner.pid if self.runner is not None else 0),
            (
                "watchdog",
                self.watchdog,
                self.watchdog.pid if self.watchdog is not None else 0,
            ),
            (
                "comfyui_backend",
                backend_process,
                int(
                    getattr(self.comfyui_backend, "process_group", 0) or 0
                ),
            ),
        ]
        scope_anchor = self.cgroup.anchor_process
        if scope_anchor is not None and scope_anchor is not self.runner:
            targets.append(("scope_anchor", scope_anchor, scope_anchor.pid))
        process_rows: list[tuple[str, Any, Any, int, str, str]] = []
        # Phase 1: signal every independently managed process group without
        # waiting on any one process.  A D-state task must not delay the kill
        # signal for the backend, watchdog, or dormant scope anchor.
        for name, process, process_group in targets:
            if process is None:
                rows[name] = {"started": False, "stopped": True}
                continue
            before = process.poll()
            group = int(process_group or process.pid)
            signal_method = "not_signaled"
            signal_error = ""
            try:
                # A dead leader does not prove its process group is empty.
                # Signal every known managed PGID; ESRCH is the empty-group
                # result.
                os.killpg(group, signal.SIGKILL)
                signal_method = "killpg_sigkill"
            except ProcessLookupError:
                signal_method = "process_group_absent"
            except OSError as exc:
                if before is None:
                    try:
                        os.kill(process.pid, signal.SIGKILL)
                        signal_method = "kill_sigkill"
                    except OSError as fallback_exc:
                        signal_method = "failed"
                        signal_error = (
                            f"{exc.__class__.__name__}/"
                            f"{fallback_exc.__class__.__name__}"
                        )
                else:
                    signal_method = "killpg_failed_leader_exited"
                    signal_error = exc.__class__.__name__
            process_rows.append((
                name,
                process,
                before,
                group,
                signal_method,
                signal_error,
            ))
        # Phase 2: immediately freeze and kill the entire pinned campaign
        # scope.  This remains ahead of all child reaping and performs no
        # durable evidence write.
        try:
            rows["campaign_scope"] = (
                self.cgroup.emergency_kill_scope_without_durable_evidence()
            )
        except Exception as exc:
            rows["campaign_scope"] = {
                "ok": False,
                "stopped": False,
                "error_code": exc.__class__.__name__,
                "durable_writes_performed": False,
            }
        # Phase 3: reap all direct children under one shared two-second
        # deadline.  Reaping never delays a kill signal for another writer.
        reap_deadline = time.monotonic() + 2.0
        for (
            name,
            process,
            before,
            process_group,
            signal_method,
            signal_error,
        ) in process_rows:
            reap_error_code = ""
            if before is None:
                try:
                    process.wait(
                        timeout=max(0.0, reap_deadline - time.monotonic())
                    )
                except subprocess.TimeoutExpired:
                    pass
                except Exception as exc:
                    reap_error_code = exc.__class__.__name__
            after = process.poll()
            try:
                os.killpg(process_group, 0)
                process_group_empty = False
            except ProcessLookupError:
                process_group_empty = True
            except OSError:
                process_group_empty = False
            rows[name] = {
                "started": True,
                "pid": int(process.pid),
                "returncode_before": before,
                "returncode_after": after,
                "signal_method": signal_method,
                "signal_error": signal_error,
                "reap_error_code": reap_error_code,
                "process_group_empty": process_group_empty,
                "stopped": bool(
                    after is not None and process_group_empty
                ),
            }
        rows["ok"] = all(
            row.get("stopped") is True and row.get("ok", True) is True
            for row in rows.values()
            if isinstance(row, Mapping)
        )
        rows["durable_writes_performed"] = False
        return rows

    def _wait_for_post_hard_io_quiescence(self) -> dict[str, Any]:
        """After all campaign work is stopped, prove PSI and block-I/O quiet."""

        started = time.monotonic()
        block_io_sampler = HostStartupBlockIoSampler(data_root=ROOT)
        safe_streak = 0
        samples: list[dict[str, Any]] = []
        maximum_avg10 = 0.0
        maximum_avg60 = 0.0
        waitable_io_reasons = {
            "HOST_IO_PRESSURE_HIGH",
            "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
            "HOST_BLOCK_DEVICE_NOT_QUIET",
        }
        while True:
            try:
                evidence = collect_host_startup_safety_preflight()
            except Exception as exc:
                return {
                    "ok": False,
                    "reason": "POST_HARD_IO_TELEMETRY_FAILED",
                    "error_code": exc.__class__.__name__,
                    "waited_seconds": round(time.monotonic() - started, 6),
                    "samples": samples,
                }
            checks = evidence.get("checks") or {}
            io_value = (checks.get("host_io_pressure") or {}).get("value") or {}
            avg10 = io_value.get("avg10")
            avg60 = io_value.get("avg60")
            if self._finite_nonnegative_number(avg10):
                maximum_avg10 = max(maximum_avg10, float(avg10))
            if self._finite_nonnegative_number(avg60):
                maximum_avg60 = max(maximum_avg60, float(avg60))
            tripped = [str(item) for item in evidence.get("tripped") or ()]
            block_io = block_io_sampler.sample()
            if block_io.get("status") == "unavailable":
                tripped = list(dict.fromkeys([
                    *tripped,
                    "HOST_BLOCK_DEVICE_TELEMETRY_INCOMPLETE",
                ]))
            elif block_io.get("safe") is not True:
                tripped = list(dict.fromkeys([
                    *tripped,
                    "HOST_BLOCK_DEVICE_NOT_QUIET",
                ]))
            non_io = [reason for reason in tripped if reason not in waitable_io_reasons]
            sample_ok = bool(
                evidence.get("ok") is True
                and block_io.get("safe") is True
            )
            row = {
                "at": evidence.get("at"),
                "ok": sample_ok,
                "tripped": tripped,
                "io": {"avg10": avg10, "avg60": avg60},
                "block_io": block_io,
            }
            if len(samples) < 64 or sample_ok:
                samples.append(row)
            elif samples:
                samples[-1] = row
            if evidence.get("errors") or non_io:
                errors = dict(evidence.get("errors") or {})
                if block_io.get("status") == "unavailable":
                    errors["host.block_io_safety"] = str(
                        block_io.get("error_type") or "TelemetryUnavailable"
                    )
                return {
                    "ok": False,
                    "reason": "NON_IO_SAFETY_FAILURE",
                    "non_io": non_io,
                    "errors": errors,
                    "waited_seconds": round(time.monotonic() - started, 6),
                    "samples": samples,
                }
            if sample_ok:
                safe_streak += 1
                if safe_streak >= POST_HARD_IO_REQUIRED_SAFE_SAMPLES:
                    return {
                        "ok": True,
                        "required_consecutive_safe": (
                            POST_HARD_IO_REQUIRED_SAFE_SAMPLES
                        ),
                        "waited_seconds": round(
                            time.monotonic() - started,
                            6,
                        ),
                        "maximum_io_pressure": {
                            "avg10": round(maximum_avg10, 6),
                            "avg60": round(maximum_avg60, 6),
                        },
                        "samples": samples,
                    }
            else:
                safe_streak = 0
            if (
                time.monotonic() - started
                >= POST_HARD_IO_QUIESCENCE_TIMEOUT_SECONDS
            ):
                return {
                    "ok": False,
                    "reason": "POST_HARD_IO_QUIESCENCE_TIMEOUT",
                    "waited_seconds": round(time.monotonic() - started, 6),
                    "maximum_io_pressure": {
                        "avg10": round(maximum_avg10, 6),
                        "avg60": round(maximum_avg60, 6),
                    },
                    "samples": samples,
                }
            time.sleep(1.0)

    def _hard_io_minimal_failure_result(
        self,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project a fixed, raw-error-free report without scanning the tree."""

        allowed_statuses = {"PASS", "FAIL", "NOT_EVALUATED"}
        gate_statuses = {
            str(name): {
                "status": (
                    str(row.get("status"))
                    if isinstance(row, Mapping)
                    and str(row.get("status")) in allowed_statuses
                    else "UNKNOWN"
                ),
                "machine_verified": bool(
                    isinstance(row, Mapping)
                    and row.get("machine_verified") is True
                ),
            }
            for name, row in sorted(self.gates.items())
            if str(name) in HARD_IO_MINIMAL_GATE_ALLOWLIST
        }
        cleanup = result.get("cleanup")
        cleanup_statuses: dict[str, dict[str, bool]] = {}
        if isinstance(cleanup, Mapping):
            for name, row in sorted(cleanup.items()):
                if (
                    str(name) not in HARD_IO_MINIMAL_CLEANUP_ALLOWLIST
                    or not isinstance(row, Mapping)
                ):
                    continue
                cleanup_statuses[str(name)] = {
                    key: row.get(key) is True
                    for key in (
                        "ok",
                        "closed",
                        "stopped",
                        "not_created",
                        "deferred",
                    )
                }
        source = result.get("source_final")
        raw_verification_state = (
            str(source.get("verification_state") or "")
            if isinstance(source, Mapping)
            else ""
        )
        raw_reason_code = (
            str(source.get("reason_code") or "")
            if isinstance(source, Mapping)
            else ""
        )
        source_minimal = {
            "verified": bool(
                isinstance(source, Mapping) and source.get("verified") is True
            ),
            "verification_state": (
                raw_verification_state
                if raw_verification_state in {"", "NOT_EVALUATED"}
                else "UNKNOWN"
            ),
            "reason_code": (
                raw_reason_code
                if raw_reason_code
                in {"", "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED"}
                else "UNKNOWN"
            ),
            "h24_capture_skipped": bool(
                isinstance(source, Mapping)
                and source.get("h24_capture_skipped") is True
            ),
        }
        scan = result.get("authoritative_secret_scan")
        raw_scan_status = (
            str(scan.get("status") or "")
            if isinstance(scan, Mapping)
            else ""
        )
        raw_receipt_digest = (
            str(scan.get("external_failure_receipt_payload_sha256") or "")
            if isinstance(scan, Mapping)
            else ""
        )
        scan_minimal = {
            "required": bool(
                isinstance(scan, Mapping) and scan.get("required") is True
            ),
            "exception_path": bool(
                isinstance(scan, Mapping)
                and scan.get("exception_path") is True
            ),
            "status": (
                raw_scan_status
                if raw_scan_status
                in {"", "SKIPPED_DUE_TO_HOST_IO_HARD_LIMIT"}
                else "UNKNOWN"
            ),
            "root_scan_verified": bool(
                isinstance(scan, Mapping)
                and scan.get("root_scan_verified") is True
            ),
            "final_report_scan_verified": bool(
                isinstance(scan, Mapping)
                and scan.get("final_report_scan_verified") is True
            ),
            "fail_closed": bool(
                isinstance(scan, Mapping) and scan.get("fail_closed") is True
            ),
            "external_failure_receipt_required": bool(
                isinstance(scan, Mapping)
                and scan.get("external_failure_receipt_required") is True
            ),
            "external_failure_receipt_verified": bool(
                isinstance(scan, Mapping)
                and scan.get("external_failure_receipt_verified") is True
            ),
            "external_failure_receipt_payload_sha256": (
                raw_receipt_digest
                if len(raw_receipt_digest) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in raw_receipt_digest
                )
                else ""
            ),
            "minimal_report_written": bool(
                isinstance(scan, Mapping)
                and scan.get("minimal_report_written") is True
            ),
            "post_minimal_report_quiescence_required": bool(
                isinstance(scan, Mapping)
                and scan.get("post_minimal_report_quiescence_required") is True
            ),
        }
        raw_error_code = str(result.get("error_code") or "")
        error_code = (
            raw_error_code
            if raw_error_code
            in {
                "CampaignCgroupError",
                "KeyboardInterrupt",
                "SourceFreezeError",
                "SupervisorError",
            }
            else "SupervisorError"
        )
        raw_commit = str(self.commit or "")
        commit = (
            raw_commit
            if len(raw_commit) in {40, 64}
            and all(character in "0123456789abcdef" for character in raw_commit)
            else ""
        )

        def safe_timestamp(value: Any) -> str:
            text = str(value or "")
            allowed = "0123456789T:.-Z+"
            return (
                text
                if 20 <= len(text) <= 40
                and all(character in allowed for character in text)
                else ""
            )

        return {
            "schema_version": SUPERVISOR_SCHEMA_VERSION,
            "report_mode": "hard_io_minimal_allowlisted",
            "campaign_uuid": self.campaign_uuid,
            "level": self.config.level,
            "started_at": safe_timestamp(result.get("started_at")),
            "finished_at": safe_timestamp(result.get("finished_at")),
            "commit": commit,
            "classification": "FAIL_HARNESS",
            "error_code": error_code,
            "error_sha256": (
                str(result.get("error_sha256") or "")
                if len(str(result.get("error_sha256") or "")) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in str(result.get("error_sha256") or "")
                )
                else ""
            ),
            "source_final": source_minimal,
            "gate_statuses": gate_statuses,
            "cleanup_statuses": cleanup_statuses,
            "authoritative_secret_scan": scan_minimal,
            "contains_raw_error_text": False,
            "ok": False,
        }

    def _exception_source_final(self) -> dict[str, Any]:
        """Capture failure-path source evidence without compounding hard I/O."""

        if not self.source_h0:
            return {}
        if self._host_io_hard_limit_was_exceeded():
            self.freezer.close()
            return {
                "verified": False,
                "verification_state": "NOT_EVALUATED",
                "reason_code": "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
                "h0_verified": self.source_h0.get("verified") is True,
                "h24_capture_skipped": True,
            }
        try:
            return self.freezer.verify_final(
                require_clean=self.config.level == "formal"
            )
        except Exception as source_exc:
            return {
                "verified": False,
                "error_code": source_exc.__class__.__name__,
                "error_sha256": hashlib.sha256(
                    str(source_exc).encode("utf-8")
                ).hexdigest(),
            }

    def _capture_source(self) -> None:
        try:
            self.source_h0 = self.freezer.capture(label="H0", require_clean=self.config.level == "formal")
            source_safety = self._source_capture_safety_summary()
            if source_safety.get("all_safe") is not True:
                raise SourceFreezeError(
                    "source capture completed without a continuous safe host checkpoint chain"
                )
            self._gate(
                "source_capture_host_safety_verified",
                passed=True,
                evidence=source_safety,
            )
            expected_evidence_mode = (
                METADATA_CONTENT_EVIDENCE
                if self.config.level == "smoke"
                else FULL_CONTENT_EVIDENCE
            )
            if self.source_h0.get("content_evidence_mode") != expected_evidence_mode:
                raise SourceFreezeError(
                    "source evidence mode does not match the campaign level"
                )
            drift_monitor = self.freezer.lightweight_drift_check()
            monitor_health = drift_monitor.get("monitor") if isinstance(drift_monitor, Mapping) else None
            monitor_verified = bool(
                drift_monitor.get("verified") is True
                and isinstance(monitor_health, Mapping)
                and monitor_health.get("machine_verified") is True
            )
            formal_monitor_required = self.config.level in {"rehearsal", "soak", "formal"}
            monitor_formal_eligible = bool(
                isinstance(monitor_health, Mapping)
                and monitor_health.get("formal_eligible") is True
            )
            self._gate(
                "source_runtime_monitor_verified",
                passed=monitor_verified and (
                    monitor_formal_eligible or not formal_monitor_required
                ),
                evidence=drift_monitor,
                error=(
                    "kernel source monitor is not formal-eligible"
                    if formal_monitor_required and not monitor_formal_eligible
                    else "source monitor self-check failed"
                ),
            )
            if not monitor_verified or (formal_monitor_required and not monitor_formal_eligible):
                raise SourceFreezeError(
                    "runtime source monitor is not machine-verified/formal-eligible"
                )
            evidence = {
                "commit": self.source_h0.get("commit"),
                "tracked_content_digest": self.source_h0.get("tracked_content_digest"),
                "content_evidence_mode": self.source_h0.get("content_evidence_mode"),
                "protected_ignored_manifest_digest": self.source_h0.get("protected_ignored_manifest_digest"),
                "protected_ignored_content_digest": self.source_h0.get("protected_ignored_content_digest"),
                "artifact_root": self.source_h0.get("artifact_root"),
                "git_status_empty": self.source_h0.get("git_status_empty"),
                "require_clean": self.config.level == "formal",
                "host_safety": source_safety,
            }
            if self.config.level == "formal":
                self._gate("worktree_clean_and_frozen", passed=True, evidence=evidence)
                assert self.config.gate_bundle_file is not None
                self.gate_bundle = validate_gate_bundle(
                    self.config.gate_bundle_file,
                    commit=self.commit,
                    source_authority=self.source_h0,
                )
                self._gate(
                    "prior_harness_gate_bundle_verified",
                    passed=True,
                    evidence={
                        "path": str(self.config.gate_bundle_file),
                        "schema_version": self.gate_bundle.get("schema_version"),
                        "bundle_sha256": self.gate_bundle.get("bundle_sha256"),
                        "qualification_campaign_uuid": self.gate_bundle.get("qualification_campaign_uuid"),
                        "commit": self.gate_bundle.get("commit"),
                        "source_digest": self.gate_bundle.get("source_digest"),
                        "protected_source_digest": self.gate_bundle.get("protected_source_digest"),
                    },
                )
            else:
                self._gate("source_baseline_frozen", passed=True, evidence=evidence)
                self.gates["worktree_clean_and_frozen"] = {
                    "status": "NOT_EVALUATED",
                    "machine_verified": False,
                    "checked_at": utc_now(),
                    "evidence": evidence,
                    "error": "non-formal dirty-baseline freeze cannot prove the formal clean-worktree gate",
                }
        except SourceFreezeError as exc:
            self._gate("worktree_clean_and_frozen", passed=False, error=str(exc))
            raise SupervisorError(str(exc)) from exc

    def _verify_host_safety_preflight(self) -> dict[str, Any]:
        evidence = wait_for_host_safety_preflight(
            timeout_seconds=HOST_SAFETY_PREFLIGHT_TIMEOUT_SECONDS,
            required_consecutive_safe=(
                HOST_SAFETY_STARTUP_SETTLE_CONSECUTIVE_SAMPLES
            ),
            collector=collect_host_startup_safety_preflight,
            block_io_sampler=HostStartupBlockIoSampler(data_root=ROOT),
        )
        passed = evidence.get("ok") is True
        self._gate(
            "host_safety_preflight_verified",
            passed=passed,
            evidence=evidence,
            error="" if passed else ",".join(evidence.get("tripped") or []),
        )
        if not passed:
            raise SupervisorError(
                "host safety preflight blocked campaign startup: "
                + ",".join(evidence.get("tripped") or ["UNKNOWN_HOST_RISK"])
            )
        return evidence

    def _verify_host_safety_runner_launch(self) -> dict[str, Any]:
        """Require fresh cold-start headroom after source and backend setup."""

        evidence = wait_for_host_safety_preflight(
            timeout_seconds=HOST_SAFETY_RUNNER_LAUNCH_TIMEOUT_SECONDS,
            required_consecutive_safe=(
                HOST_SAFETY_STARTUP_SETTLE_CONSECUTIVE_SAMPLES
            ),
            collector=collect_host_startup_safety_preflight,
            block_io_sampler=HostStartupBlockIoSampler(data_root=ROOT),
        )
        passed = evidence.get("ok") is True
        self._gate(
            "host_safety_runner_launch_verified",
            passed=passed,
            evidence=evidence,
            error="" if passed else ",".join(evidence.get("tripped") or []),
        )
        if not passed:
            raise SupervisorError(
                "host safety runner launch gate blocked managed exec: "
                + ",".join(evidence.get("tripped") or ["UNKNOWN_HOST_RISK"])
            )
        return evidence

    def _release_managed_runner_exec(self) -> dict[str, Any]:
        safety_gate = self.gates.get("host_safety_runner_launch_verified") or {}
        if safety_gate.get("status") != "PASS" or safety_gate.get(
            "machine_verified"
        ) is not True:
            raise SupervisorError(
                "managed runner exec requires verified startup headroom"
            )
        return self.cgroup.release_managed_command()

    def _verify_host_safety_startup_settle(
        self,
        *,
        gate_name: str,
        failure_context: str,
        runner_identity: Any | None = None,
    ) -> dict[str, Any]:
        """Require a quiet window after one bounded startup-I/O phase.

        Sixty consecutive one-second samples are intentional.  Each
        sample includes a rolling block-device window, covering delayed WSL
        writeback that has appeared roughly 25 seconds after a small read or
        write.  A single fresh PSI sample is not an adequate release authority.
        """

        heartbeat_refreshes: list[dict[str, Any]] = []
        block_io_sampler = HostStartupBlockIoSampler(data_root=ROOT)

        def collect_with_heartbeat() -> dict[str, Any]:
            if runner_identity is not None:
                refresh = self._refresh_gated_heartbeat(runner_identity)
                if refresh.get("refreshed") is True:
                    heartbeat_refreshes.append(refresh)
            return collect_host_startup_safety_preflight()

        evidence = wait_for_host_safety_preflight(
            timeout_seconds=self.config.activation_timeout_seconds,
            required_consecutive_safe=(
                HOST_SAFETY_STARTUP_SETTLE_CONSECUTIVE_SAMPLES
            ),
            collector=(
                collect_with_heartbeat
                if runner_identity is not None
                else collect_host_startup_safety_preflight
            ),
            block_io_sampler=block_io_sampler,
        )
        if heartbeat_refreshes:
            evidence = {
                **evidence,
                "gated_heartbeat_refreshes": heartbeat_refreshes,
            }
        passed = evidence.get("ok") is True
        self._gate(
            gate_name,
            passed=passed,
            evidence=evidence,
            error="" if passed else ",".join(evidence.get("tripped") or []),
        )
        if not passed:
            raise SupervisorError(
                f"host safety {failure_context} blocked campaign startup: "
                + ",".join(evidence.get("tripped") or ["UNKNOWN_HOST_RISK"])
            )
        return evidence

    def _verify_host_safety_runner_import_settled(self) -> dict[str, Any]:
        return self._verify_host_safety_startup_settle(
            gate_name="host_safety_runner_import_settled",
            failure_context="runner import settle gate",
        )

    def _verify_host_safety_state_initialization_settled(
        self,
        runner_identity: Any,
    ) -> dict[str, Any]:
        return self._verify_host_safety_startup_settle(
            gate_name="host_safety_state_initialization_settled",
            failure_context="state initialization settle gate",
            runner_identity=runner_identity,
        )

    def _verify_host_safety_backend_startup_settled(self) -> dict[str, Any]:
        return self._verify_host_safety_startup_settle(
            gate_name="host_safety_backend_startup_settled",
            failure_context="managed backend settle gate",
        )

    def _verify_host_safety_activation(
        self,
        runner_identity: Any | None = None,
    ) -> dict[str, Any]:
        """Re-check host headroom after watchdog startup, before ACTIVE."""

        return self._verify_host_safety_startup_settle(
            gate_name="host_safety_activation_verified",
            failure_context="activation gate",
            runner_identity=runner_identity,
        )

    def _runner_command(self) -> list[str]:
        profile = SUPERVISED_RUNNER_PROFILES[self.config.level]
        profile_args = [
            value
            for name, option in SUPERVISED_RUNNER_PROFILE_OPTIONS.items()
            for value in (option, str(profile[name]))
        ]
        target_args = [
            "--campaign-root", str(self.root),
            "--duration-seconds", str(self.config.duration_seconds),
            "--supervised",
            "--campaign-uuid", self.campaign_uuid,
            "--control-root", str(self.control_root),
            "--state-path", str(self.state_path),
            "--control-path", str(self.control_path),
            "--heartbeat-path", str(self.heartbeat_path),
            "--auth-socket", str(self.auth_socket_path),
            "--supervisor-pid", str(self.supervisor_process_identity.pid),
            "--supervisor-start-ticks", str(self.supervisor_process_identity.start_ticks),
            "--supervisor-boot-id", self.supervisor_process_identity.boot_id,
            "--supervisor-cgroup", self.supervisor_process_identity.cgroup_path,
            "--checkpoint-path", str(self.checkpoint_path),
            "--checkpoint-mirror-path", str(self.checkpoint_mirror_path),
            "--source-freeze-path", str(Path(self.source_h0["artifact_root"]) / "source_freeze.json"),
            "--activation-gate", str(self.activation_gate_path),
            "--supervisor-contract", str(self.contract_path),
            *profile_args,
            *self.config.runner_extra_args,
        ]
        if self.config.level != "formal":
            target_args.append("--allow-short-duration")
        return self._staged_import_command(
            profile="runner",
            evidence_path=self.runner_import_evidence_path,
            target_args=target_args,
        )

    def _launch_runner_gated(self) -> Any:
        self.runner = self.cgroup.anchor_process
        if self.runner is None:
            raise SupervisorError("campaign cgroup did not retain its managed anchor process")
        deadline = (
            time.monotonic() + self.config.runner_bootstrap_timeout_seconds
        )
        last_error = ""
        while time.monotonic() < deadline:
            if self.runner.poll() is not None:
                raise SupervisorError(f"campaign runner exited before activation: {self.runner.returncode}")
            try:
                identity = capture_process_identity(self.cgroup.anchor_pid)
                self.runner_pid = identity.pid
                return identity
            except Exception as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
            time.sleep(0.05)
        raise SupervisorError("campaign runner identity was not observable: " + last_error)

    def _sign_control_payload(
        self,
        payload: Mapping[str, Any],
        *,
        stream: str,
        monotonic_ns: int,
    ) -> dict[str, Any]:
        session_secret = self.runner_auth_key
        if session_secret is None:
            raise SupervisorError("control session key is unavailable for authenticated state")
        sequence = int(self.control_auth_sequences.get(stream) or 0) + 1
        signed = sign_authenticated_payload(
            payload,
            session_secret=session_secret,
            campaign_uuid=self.campaign_uuid,
            stream=stream,
            sequence=sequence,
            monotonic_ns=monotonic_ns,
        )
        self.control_auth_sequences[stream] = sequence
        return signed

    def _initialize_state_files(self, identity: Any) -> None:
        state = self.state_machine.initialize(
            campaign_uuid=self.campaign_uuid,
            required_active_seconds=self.config.duration_seconds,
            orchestrator_pid=self.runner_pid,
            orchestrator_start_ticks=identity.start_ticks,
        )
        state = self.state_machine.transition(CampaignState.PREFLIGHT, reason="supervisor_preflight_started")
        checkpoint_revision = 1
        atomic_write_json(self.control_path, {
            "schema_version": "hackme.campaign-control.v1",
            "campaign_uuid": self.campaign_uuid,
            "state": state["state"],
            "admit_new_jobs": False,
            "load_generator_should_run": False,
            "preserve_evidence_requested": False,
            "updated_at": utc_now(),
        })
        checkpoint_monotonic_ns = time.monotonic_ns()
        initial_checkpoint = self._sign_control_payload({
            "schema_version": "hackme.campaign-checkpoint.v1",
            "campaign_uuid": self.campaign_uuid,
            "revision": checkpoint_revision,
            "phase": "supervisor_preflight",
            "state_revision": state["revision"],
            "updated_at": utc_now(),
        }, stream="runner_checkpoint", monotonic_ns=checkpoint_monotonic_ns)
        atomic_write_json(self.checkpoint_path, initial_checkpoint)
        self.checkpoint_mirror_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.checkpoint_mirror_path.parent, 0o700)
        atomic_write_json(self.checkpoint_mirror_path, initial_checkpoint)
        if (
            load_json(self.checkpoint_path) != initial_checkpoint
            or load_json(self.checkpoint_mirror_path) != initial_checkpoint
            or self.checkpoint_mirror_path.stat().st_mode & 0o077
        ):
            raise SupervisorError("initial checkpoint primary/mirror verification failed")
        heartbeat_monotonic_ns = time.monotonic_ns()
        atomic_write_json(self.heartbeat_path, self._sign_control_payload({
            "schema_version": "hackme.campaign-heartbeat.v1",
            "campaign_uuid": self.campaign_uuid,
            "heartbeat": {
                "orchestrator_pid": self.runner_pid,
                "orchestrator_start_ticks": identity.start_ticks,
                "orchestrator_monotonic_ns": heartbeat_monotonic_ns,
                "checkpoint_revision": checkpoint_revision,
                "updated_at": utc_now(),
            },
        }, stream="runner_heartbeat", monotonic_ns=heartbeat_monotonic_ns))
        self.last_gated_heartbeat_monotonic_ns = heartbeat_monotonic_ns

    def _refresh_gated_heartbeat(
        self,
        identity: Any,
        *,
        checkpoint_revision: int = 1,
        force: bool = False,
    ) -> dict[str, Any]:
        if self.runner is None or self.runner.poll() is not None:
            raise SupervisorError("cannot heartbeat a missing campaign runner")
        now_ns = time.monotonic_ns()
        elapsed_seconds = (
            (now_ns - self.last_gated_heartbeat_monotonic_ns) / 1_000_000_000
            if self.last_gated_heartbeat_monotonic_ns > 0
            else None
        )
        if (
            not force
            and elapsed_seconds is not None
            and elapsed_seconds < GATED_HEARTBEAT_MINIMUM_INTERVAL_SECONDS
        ):
            return {
                "refreshed": False,
                "reason": "rate_limited",
                "elapsed_seconds": round(max(0.0, elapsed_seconds), 6),
                "minimum_interval_seconds": (
                    GATED_HEARTBEAT_MINIMUM_INTERVAL_SECONDS
                ),
            }
        self.state_machine.heartbeat(
            orchestrator_pid=self.runner_pid,
            orchestrator_start_ticks=identity.start_ticks,
            checkpoint_revision=checkpoint_revision,
            now_ns=now_ns,
        )
        atomic_write_json(self.heartbeat_path, self._sign_control_payload({
            "schema_version": "hackme.campaign-heartbeat.v1",
            "campaign_uuid": self.campaign_uuid,
            "heartbeat": {
                "orchestrator_pid": self.runner_pid,
                "orchestrator_start_ticks": identity.start_ticks,
                "orchestrator_monotonic_ns": now_ns,
                "checkpoint_revision": checkpoint_revision,
                "updated_at": utc_now(),
            },
        }, stream="runner_heartbeat", monotonic_ns=now_ns))
        self.last_gated_heartbeat_monotonic_ns = now_ns
        return {
            "refreshed": True,
            "monotonic_ns": now_ns,
            "checkpoint_revision": checkpoint_revision,
            "minimum_interval_seconds": GATED_HEARTBEAT_MINIMUM_INTERVAL_SECONDS,
        }

    def _launch_watchdog(self, identity: Any) -> dict[str, Any]:
        cgroup_identity_row = self.cgroup.capture_scope_identity()
        if (
            self.auth_server is None
            or self.runner_auth_key is None
            or self.watchdog_auth_key is None
        ):
            raise SupervisorError("authenticated control server was not opened before watchdog launch")
        config = WatchdogConfig(
            campaign_uuid=self.campaign_uuid,
            paths=WatchdogPaths(
                campaign_root=self.control_root,
                state=self.state_path,
                control=self.control_path,
                heartbeat=self.heartbeat_path,
                checkpoint=self.checkpoint_path,
                ready=self.watchdog_ready_path,
                evidence=self.control_artifact_dir / "watchdog",
                process_lock=self.watchdog_lock_path,
                liveness=self.watchdog_liveness_path,
            ),
            orchestrator_pid=self.runner_pid,
            orchestrator_start_ticks=identity.start_ticks,
            orchestrator_boot_id=identity.boot_id,
            orchestrator_cgroup=identity.cgroup_path,
            campaign_cgroup=CgroupIdentity(
                cgroup_identity_row["path"],
                cgroup_identity_row["device"],
                cgroup_identity_row["inode"],
            ),
            auth_socket=self.auth_socket_path,
            supervisor_pid=self.supervisor_process_identity.pid,
            supervisor_start_ticks=self.supervisor_process_identity.start_ticks,
            supervisor_boot_id=self.supervisor_process_identity.boot_id,
            supervisor_cgroup=self.supervisor_process_identity.cgroup_path,
        )
        authentication_deadline = (
            time.monotonic() + self.config.watchdog_bootstrap_timeout_seconds
        )
        authentication: dict[str, Any] = {}
        raw_watchdog_command = build_watchdog_command(config)
        if len(raw_watchdog_command) < 2:
            raise SupervisorError("watchdog command is incomplete")
        watchdog_command = self._staged_import_command(
            profile="watchdog",
            evidence_path=self.watchdog_import_evidence_path,
            target_args=raw_watchdog_command[2:],
        )
        try:
            self.watchdog_log_handle = self.watchdog_stdout.open("w", encoding="utf-8")
            watchdog_environment = os.environ.copy()
            watchdog_environment.pop("PYTHONPYCACHEPREFIX", None)
            watchdog_environment.update({
                "PYTHONPATH": str(ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            self.watchdog = subprocess.Popen(
                watchdog_command,
                cwd=str(ROOT),
                env=watchdog_environment,
                stdin=subprocess.DEVNULL,
                stdout=self.watchdog_log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            watchdog_identity = capture_process_identity(self.watchdog.pid)
            self.watchdog_process_identity = watchdog_identity
            authentication = self._authenticate_watchdog_control(
                runner_identity=identity,
                watchdog_identity=watchdog_identity,
                deadline=authentication_deadline,
            )
            self._verify_staged_import_evidence(
                profile="watchdog",
                process_identity=watchdog_identity,
            )
        except Exception as exc:
            gate = self.gates.get("authenticated_control_channel_verified") or {}
            gate_evidence = gate.get("evidence") if isinstance(gate.get("evidence"), Mapping) else {}
            if gate_evidence.get("verification_state") != "failed":
                self._fail_authenticated_control_channel(
                    error=exc,
                    reason="watchdog_launch_or_handshake_failed",
                )
            raise
        finally:
            if self._host_io_hard_limit_was_exceeded():
                # Closing the one-shot socket unlinks two filesystem entries.
                # Defer those writes to the post-hard quiescent cleanup path.
                channel_cleanup = {
                    "ok": False,
                    "deferred": True,
                    "reason_code": "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
                }
            else:
                channel_cleanup = self._close_authenticated_control_server()
        if channel_cleanup.get("ok") is not True:
            error = SupervisorError("authenticated control socket one-shot cleanup failed")
            self._fail_authenticated_control_channel(
                error=error,
                reason="one_time_socket_cleanup_failed",
            )
            raise error
        deadline = time.monotonic() + self.config.watchdog_ready_timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            self._refresh_gated_heartbeat(identity)
            if self.watchdog.poll() is not None:
                raise SupervisorError(f"external watchdog exited before readiness: {self.watchdog.returncode}")
            if self.watchdog_ready_path.exists():
                try:
                    last = load_json(self.watchdog_ready_path)
                except Exception:
                    last = {}
                if (
                    last.get("verified") is True
                    and last.get("external_process") is True
                    and last.get("watchdog_outside_campaign_cgroup") is True
                    and int(last.get("watchdog_pid") or 0) == watchdog_identity.pid
                    and int(last.get("watchdog_start_ticks") or 0) == watchdog_identity.start_ticks
                    and str(last.get("watchdog_boot_id") or "") == watchdog_identity.boot_id
                    and str(last.get("watchdog_cgroup") or "") == watchdog_identity.cgroup_path
                ):
                    placement = self.cgroup.assert_watchdog_outside(self.watchdog.pid)
                    liveness = self._verify_watchdog_liveness()
                    self._gate(
                        "watchdog_reciprocal_liveness_verified",
                        passed=True,
                        evidence=liveness,
                    )
                    return {
                        "watchdog": last,
                        "placement": placement,
                        "authenticated_control": authentication,
                        "authenticated_liveness": liveness,
                        "command": watchdog_command,
                    }
            time.sleep(0.1)
        raise SupervisorError(f"external watchdog readiness was not proven: {last}")

    def _release_runner(self, *, cgroup_evidence: Mapping[str, Any], watchdog_evidence: Mapping[str, Any], placement: Mapping[str, Any]) -> None:
        self._require_authenticated_control_channel()
        for required_gate in (
            "runner_control_channel_authenticated",
            "watchdog_reciprocal_liveness_verified",
            "runner_import_staged_verified",
            "watchdog_import_staged_verified",
            "host_safety_runner_import_settled",
            "host_safety_state_initialization_settled",
        ):
            row = self.gates.get(required_gate) or {}
            if row.get("status") != "PASS" or row.get("machine_verified") is not True:
                raise SupervisorError(
                    f"campaign runner cannot be released without {required_gate}"
                )
        if self.comfyui_backend is not None:
            backend_safety = self.gates.get(
                "host_safety_backend_startup_settled"
            ) or {}
            if (
                backend_safety.get("status") != "PASS"
                or backend_safety.get("machine_verified") is not True
            ):
                raise SupervisorError(
                    "campaign runner cannot be released without "
                    "host_safety_backend_startup_settled"
                )
        event_gate = self.gates.get("cgroup_event_baseline_verified") or {}
        if (
            event_gate.get("status") != "PASS"
            or event_gate.get("machine_verified") is not True
            or not self.cgroup.event_baseline
        ):
            raise SupervisorError(
                "campaign runner cannot be released without a verified cgroup event baseline"
            )
        activation_safety = self.gates.get("host_safety_activation_verified") or {}
        if (
            activation_safety.get("status") != "PASS"
            or activation_safety.get("machine_verified") is not True
        ):
            raise SupervisorError(
                "campaign runner cannot be released without the host safety activation gate"
            )
        containment = {
            "verified": True,
            "cgroup": dict(cgroup_evidence),
            "watchdog": dict(watchdog_evidence),
            "process_placement": dict(placement),
        }
        state = self.state_machine.mark_frozen(
            source={
                "verified": True,
                "commit": self.source_h0["commit"],
                "digest": self.source_h0["tracked_content_digest"],
                "artifact": str(Path(self.source_h0["artifact_root"]) / "source_freeze.json"),
            },
            containment=containment,
        )
        contract = {
            "schema_version": SUPERVISOR_SCHEMA_VERSION,
            "campaign_uuid": self.campaign_uuid,
            "level": self.config.level,
            "duration_seconds": self.config.duration_seconds,
            "runner_profile": dict(SUPERVISED_RUNNER_PROFILES[self.config.level]),
            "load_policy": dict(SUPERVISED_LOAD_POLICIES[self.config.level]),
            "campaign_root": str(self.root),
            "control_root": str(self.control_root),
            "commit": self.commit,
            "source_digest": self.source_h0["tracked_content_digest"],
            "cgroup_path": self.cgroup.scope_path,
            "cgroup_event_baseline": self.cgroup.event_baseline,
            "state_path": str(self.state_path),
            "control_path": str(self.control_path),
            "heartbeat_path": str(self.heartbeat_path),
            "watchdog_liveness_path": str(self.watchdog_liveness_path),
            "runner_auth_key_sha256": hashlib.sha256(
                self.runner_auth_key or b""
            ).hexdigest(),
            "watchdog_auth_key_sha256": hashlib.sha256(
                self.watchdog_auth_key or b""
            ).hexdigest(),
            "role_separated_auth_keys": self.runner_auth_key != self.watchdog_auth_key,
            "checkpoint_path": str(self.checkpoint_path),
            "checkpoint_mirror_path": str(self.checkpoint_mirror_path),
            "runner_stdout": str(self.runner_stdout),
            "watchdog_stdout": str(self.watchdog_stdout),
            "supervisor_source_root": str(self.control_artifact_dir / "source"),
            "supervisor_final_result": str(self.final_path),
            "authoritative_secret_scan_receipt": str(self.final_secret_scan_receipt),
            "watchdog_pid": self.watchdog.pid if self.watchdog else 0,
            "runner_pid": self.runner_pid,
            "comfyui_backend": (
                self.comfyui_backend.contract_evidence()
                if self.comfyui_backend is not None
                else {
                    "status": "not_configured",
                    "level": self.config.level,
                    "formal_eligible": False,
                    "ok": False,
                }
            ),
            "supervisor_identity": {
                "pid": self.supervisor_process_identity.pid,
                "start_ticks": self.supervisor_process_identity.start_ticks,
                "boot_id": self.supervisor_process_identity.boot_id,
                "cgroup_path": self.supervisor_process_identity.cgroup_path,
            },
            "state_revision": state["revision"],
            "gates": self.gates,
            "verified": True,
            "released_at": utc_now(),
        }
        atomic_write_json(self.contract_path, contract)
        atomic_write_json(self.activation_gate_path, {
            "schema_version": "hackme.campaign-activation.v1",
            "campaign_uuid": self.campaign_uuid,
            "supervisor_contract": str(self.contract_path),
            "verified": True,
            "released_at": utc_now(),
        })

    def _request_hard_stop(self, *, reason: str, evidence: Mapping[str, Any]) -> None:
        try:
            state = self.state_machine.hard_stop(
                reason_code=reason,
                classification="INVALIDATED" if reason == "SOURCE_DRIFT" else "FAIL_HARNESS",
                evidence=evidence,
            )
        except CampaignStateError:
            state = self.state_machine.snapshot()
        atomic_write_json(self.control_path, {
            "schema_version": "hackme.campaign-control.v1",
            "campaign_uuid": self.campaign_uuid,
            "revision": state.get("revision"),
            "state": "STOPPING_LOAD",
            "admit_new_jobs": False,
            "load_generator_should_run": False,
            "preserve_evidence_requested": True,
            "reason": reason,
            "evidence": dict(evidence),
            "updated_at": utc_now(),
        })

    def _mark_failed_before_active(self, *, reason: str, error: str) -> None:
        try:
            snapshot = self.state_machine.snapshot()
            current = CampaignState(snapshot["state"])
            if current in {CampaignState.PREPARING, CampaignState.PREFLIGHT, CampaignState.FROZEN}:
                snapshot = self.state_machine.transition(
                    CampaignState.FAILED,
                    reason=reason,
                    classification="FAIL_HARNESS",
                    evidence={"error": error},
                )
            control = snapshot.get("control") or {}
            atomic_write_json(self.control_path, {
                "schema_version": "hackme.campaign-control.v1",
                "campaign_uuid": self.campaign_uuid,
                "revision": snapshot.get("revision"),
                "state": snapshot.get("state"),
                "admit_new_jobs": bool(control.get("admit_new_jobs")),
                "load_generator_should_run": bool(control.get("load_generator_should_run")),
                "preserve_evidence_requested": bool(control.get("preserve_evidence_requested")),
                "reason": reason,
                "updated_at": utc_now(),
            })
        except Exception:
            pass

    def _monitor_runner(self) -> int:
        assert self.runner is not None
        next_source_check = time.monotonic() + self.config.source_poll_seconds
        next_comfyui_check = time.monotonic() + min(
            5.0,
            self.config.source_poll_seconds,
        )
        safety_stop_deadline: float | None = None
        while self.runner.poll() is None:
            if self.watchdog is not None and self.watchdog.poll() is not None and not self.failure:
                self.failure = (
                    "external watchdog exited before campaign runner terminal state "
                    f"with {self.watchdog.returncode}"
                )
                self._request_hard_stop(
                    reason="EXTERNAL_WATCHDOG_EXITED",
                    evidence={
                        "watchdog_returncode": self.watchdog.returncode,
                        "detected_at": utc_now(),
                    },
                )
                safety_stop_deadline = time.monotonic() + SAFETY_STOP_GRACE_SECONDS
            elif self.watchdog is not None and not self.failure:
                try:
                    liveness = self._verify_watchdog_liveness()
                    self._gate(
                        "watchdog_reciprocal_liveness_verified",
                        passed=True,
                        evidence=liveness,
                    )
                except Exception as exc:
                    evidence = {
                        "error": f"{exc.__class__.__name__}: {exc}",
                        "last_verified": dict(self.watchdog_liveness_evidence),
                        "detected_at": utc_now(),
                    }
                    self.failure = "authenticated watchdog liveness was lost: " + str(exc)
                    self._gate(
                        "watchdog_reciprocal_liveness_verified",
                        passed=False,
                        evidence=evidence,
                        error=str(exc),
                    )
                    self._request_hard_stop(
                        reason="WATCHDOG_LIVENESS_INVALID",
                        evidence=evidence,
                    )
                    safety_stop_deadline = time.monotonic() + SAFETY_STOP_GRACE_SECONDS
            if (
                self.comfyui_backend is not None
                and not self.failure
                and time.monotonic() >= next_comfyui_check
            ):
                try:
                    self.comfyui_backend.check_live()
                except Exception as exc:
                    evidence = {
                        "error": f"{exc.__class__.__name__}: {exc}",
                        "detected_at": utc_now(),
                    }
                    self.failure = "managed ComfyUI backend authority was lost: " + str(exc)
                    self._gate(
                        "comfyui_backend_lifecycle_verified",
                        passed=False,
                        evidence=evidence,
                        error=str(exc),
                    )
                    self._request_hard_stop(
                        reason="COMFYUI_BACKEND_INVALID",
                        evidence=evidence,
                    )
                    safety_stop_deadline = time.monotonic() + SAFETY_STOP_GRACE_SECONDS
                next_comfyui_check = time.monotonic() + min(
                    5.0,
                    self.config.source_poll_seconds,
                )
            if time.monotonic() >= next_source_check:
                drift = self.freezer.lightweight_drift_check()
                if not drift.get("verified"):
                    self._request_hard_stop(reason="SOURCE_DRIFT", evidence=drift)
                    self.failure = "source drift invalidated the campaign"
                    safety_stop_deadline = safety_stop_deadline or (
                        time.monotonic() + SAFETY_STOP_GRACE_SECONDS
                    )
                next_source_check = time.monotonic() + self.config.source_poll_seconds
            if safety_stop_deadline is not None and time.monotonic() >= safety_stop_deadline:
                forced_stop = self.cgroup.stop_scope()
                self._gate(
                    "supervisor_forced_scope_stop",
                    passed=bool(forced_stop.get("ok")),
                    evidence=forced_stop,
                    error="campaign runner exceeded the hard-stop grace period",
                )
                break
            time.sleep(0.5)
        return int(self.runner.returncode or 0)

    def _stop_watchdog(self) -> dict[str, Any]:
        if self.watchdog is None:
            return {"ok": False, "error": "not_started"}
        if self.watchdog.poll() is None:
            try:
                os.killpg(self.watchdog.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.watchdog.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.watchdog.pid, signal.SIGKILL)
                self.watchdog.wait(timeout=5)
        status = (
            load_json(self.watchdog_ready_path)
            if self.watchdog_ready_path.exists()
            else {}
        )
        handled_incident = bool(
            self.watchdog.returncode == INCIDENT_EXIT_CODE
            and status.get("ok") is True
            and status.get("incident_id")
            and status.get("reason")
        )
        return {
            "ok": self.watchdog.returncode == 0 or handled_incident,
            "returncode": self.watchdog.returncode,
            "handled_incident": handled_incident,
            "status": status,
        }

    def _cleanup(self, *, normal: bool) -> dict[str, Any]:
        auth_channel = self._close_authenticated_control_server()
        try:
            self.freezer.close()
            source_monitor = {"ok": True, "closed": True}
        except Exception as exc:
            source_monitor = {
                "ok": False,
                "closed": False,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        if self.comfyui_backend is None:
            comfyui_backend = {"ok": True, "not_configured": True}
        else:
            try:
                comfyui_backend = self.comfyui_backend.stop(
                    reason="planned_campaign_stop" if normal else "campaign_failure_stop"
                )
            except Exception as exc:
                comfyui_backend = {
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            self._gate(
                "comfyui_backend_cleanup_verified",
                passed=comfyui_backend.get("ok") is True,
                evidence=comfyui_backend,
                error=(
                    ""
                    if comfyui_backend.get("ok") is True
                    else "managed ComfyUI backend teardown was not proven"
                ),
            )
        scope: dict[str, Any]
        if self.config.keep_scope_on_failure and not normal:
            scope = {"ok": False, "preserved_for_diagnosis": True, "cgroup_path": self.cgroup.scope_path}
            watchdog = self._stop_watchdog()
        else:
            try:
                scope = self.cgroup.stop_scope() if self.cgroup.scope_path else {"ok": True, "not_created": True}
            except Exception as exc:
                scope = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            if scope.get("ok") is True:
                watchdog = self._stop_watchdog()
            else:
                watchdog = {
                    "ok": False,
                    "not_stopped": True,
                    "reason": "campaign scope was not proven empty",
                }
        for handle_name in ("runner_log_handle", "watchdog_log_handle"):
            handle = getattr(self, handle_name, None)
            process = self.watchdog if handle_name == "watchdog_log_handle" else self.runner
            if handle and (process is None or process.poll() is not None):
                handle.close()
                setattr(self, handle_name, None)
        return {
            "authenticated_control_channel": auth_channel,
            "source_monitor": source_monitor,
            "comfyui_backend": comfyui_backend,
            "watchdog": watchdog,
            "scope": scope,
        }

    def _purge_stopped_server_tls_private_keys(
        self,
        cleanup: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Remove exact ephemeral TLS keys only after the scope is proven empty."""

        scope = cleanup.get("scope") or {}
        scope_empty = bool(
            scope.get("ok") is True
            and (
                scope.get("cgroup_empty") is True
                or scope.get("not_created") is True
            )
        )
        if not scope_empty:
            result = {
                "ok": False,
                "scope_empty": False,
                "removed": [],
                "error": "campaign scope was not proven empty",
            }
            self._gate(
                "ephemeral_tls_private_keys_purged",
                passed=False,
                evidence=result,
                error=result["error"],
            )
            return result

        root = self.root.resolve(strict=True)
        removed: list[str] = []
        absent: list[str] = []
        errors: list[str] = []
        for server_name in ("primary", "recovery", "security_sentinel"):
            relative = Path(server_name) / "runtime" / "key.pem"
            path = root / relative
            try:
                before = os.lstat(path)
            except FileNotFoundError:
                absent.append(str(relative))
                continue
            except Exception as exc:
                errors.append(f"{relative}:{exc.__class__.__name__}")
                continue
            try:
                parent = path.parent.resolve(strict=True)
                if root not in parent.parents:
                    raise SupervisorError("TLS key parent escapes campaign root")
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_ISLNK(before.st_mode)
                    or int(before.st_nlink) != 1
                    or int(before.st_uid) != os.getuid()
                ):
                    raise SupervisorError("unsafe ephemeral TLS key metadata")
                os.unlink(path)
                directory = os.open(
                    parent,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                if path.exists() or path.is_symlink():
                    raise SupervisorError("ephemeral TLS key remained after unlink")
                removed.append(str(relative))
            except Exception as exc:
                errors.append(f"{relative}:{exc.__class__.__name__}")
        result = {
            "ok": not errors,
            "scope_empty": True,
            "removed": removed,
            "absent": absent,
            "errors": errors,
        }
        self._gate(
            "ephemeral_tls_private_keys_purged",
            passed=not errors,
            evidence=result,
            error=",".join(errors),
        )
        return result

    @staticmethod
    def _audit_artifact_metadata(path: Path) -> dict[str, Any]:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or int(before.st_nlink) != 1
        ):
            raise SupervisorError(f"unsafe audit evidence artifact: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        digest = hashlib.sha256()
        size = 0
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise SupervisorError(f"audit evidence artifact changed before open: {path}")
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
            after_fd = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = os.lstat(path)
        if (
            size != int(before.st_size)
            or (after_fd.st_dev, after_fd.st_ino, after_fd.st_size, after_fd.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            raise SupervisorError(f"audit evidence artifact changed while hashing: {path}")
        return {"size_bytes": size, "sha256": digest.hexdigest()}

    def _capture_post_scope_audit_evidence(
        self,
        cleanup: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Capture the authoritative H24 triad only after scope population is zero."""

        if self.config.level != "formal":
            return {
                "schema_version": SUPERVISOR_AUDIT_EVIDENCE_SCHEMA_VERSION,
                "required": False,
                "ok": True,
                "classification": "PASS",
            }
        scope = cleanup.get("scope") if isinstance(cleanup.get("scope"), Mapping) else {}
        terminal_population = (
            scope.get("terminal_population")
            if isinstance(scope.get("terminal_population"), Mapping)
            else {}
        )
        try:
            control = load_json(self.control_path)
        except Exception:
            control = {}
        barrier_errors: list[str] = []
        if self.runner is None or self.runner.poll() is None:
            barrier_errors.append("runner_not_terminal")
        if self.cgroup.stopped is not True:
            barrier_errors.append("campaign_scope_not_marked_stopped")
        if (
            scope.get("ok") is not True
            or scope.get("cgroup_empty") is not True
            or terminal_population.get("ok") is not True
            or terminal_population.get("populated") != 0
        ):
            barrier_errors.append("campaign_scope_population_not_zero")
        for component in (
            "authenticated_control_channel",
            "source_monitor",
            "comfyui_backend",
            "watchdog",
        ):
            value = cleanup.get(component)
            if not isinstance(value, Mapping) or value.get("ok") is not True:
                barrier_errors.append(f"cleanup_component_not_stopped:{component}")
        if (
            control.get("admit_new_jobs") is not False
            or control.get("load_generator_should_run") is not False
        ):
            barrier_errors.append("durable_admission_not_closed")

        evidence_root = (
            self.artifact_dir / "audit_evidence" / "supervisor_sealed_final"
        ).resolve(strict=False)
        evidence_root.mkdir(parents=True, mode=0o700, exist_ok=False)
        schema_path = evidence_root / AUDIT_EVIDENCE_SCHEMA_PATH.name
        schema_path.write_bytes(AUDIT_EVIDENCE_SCHEMA_PATH.read_bytes())
        os.chmod(schema_path, 0o600)
        barrier = {
            "schema_version": "hackme.supervisor-audit-writer-barrier/v1",
            "verified_at": utc_now(),
            "runner_returncode": self.runner.returncode if self.runner else None,
            "control": {
                "state": control.get("state"),
                "admit_new_jobs": control.get("admit_new_jobs"),
                "load_generator_should_run": control.get("load_generator_should_run"),
            },
            "scope": dict(scope),
            "cleanup": {
                name: dict(cleanup.get(name) or {})
                for name in (
                    "authenticated_control_channel",
                    "source_monitor",
                    "comfyui_backend",
                    "watchdog",
                )
            },
            "errors": sorted(set(barrier_errors)),
            "ok": not barrier_errors,
        }
        barrier_path = evidence_root / "writer_barrier.json"
        atomic_write_json(barrier_path, barrier)
        targets: dict[str, Any] = {}
        errors = list(barrier_errors)
        runtime_roots = {
            "primary": self.root / "primary" / "runtime",
            "recovery": self.root / "recovery" / "runtime",
            "security_sentinel": self.root / "security_sentinel" / "runtime",
        }
        if not barrier_errors:
            for name, runtime_root in runtime_roots.items():
                output_dir = evidence_root / name
                try:
                    receipt = capture_audit_evidence(
                        paths=AuditEvidencePaths.for_runtime(runtime_root),
                        output_dir=output_dir,
                        target=name,
                        mode="sealed",
                    )
                    persisted = load_json(output_dir / "receipt.json")
                    validation = validate_audit_evidence_receipt(
                        persisted,
                        required_mode="sealed",
                        required_target=name,
                        artifact_root=output_dir,
                    )
                    target_ok = (
                        receipt == persisted and validation.get("ok") is True
                    )
                    classification = (
                        "PASS"
                        if target_ok
                        else "FAIL_PRODUCT"
                        if persisted.get("verdict") == "FAIL_PRODUCT"
                        else "FAIL_HARNESS"
                    )
                    targets[name] = {
                        "ok": target_ok,
                        "classification": classification,
                        "receipt_verdict": persisted.get("verdict"),
                        "receipt_path": str(
                            (output_dir / "receipt.json").relative_to(evidence_root)
                        ),
                        "counts": persisted.get("counts"),
                        "heads": persisted.get("heads"),
                        "validation": validation,
                        "errors": list(validation.get("errors") or []),
                    }
                except Exception as exc:
                    targets[name] = {
                        "ok": False,
                        "classification": "FAIL_HARNESS",
                        "receipt_verdict": "FAIL_HARNESS",
                        "errors": [f"capture_failed:{exc.__class__.__name__}"],
                    }
                if targets[name].get("ok") is not True:
                    errors.extend(
                        f"{name}:{code}"
                        for code in targets[name].get("errors") or ["receipt_failed"]
                    )
        else:
            for name in runtime_roots:
                targets[name] = {
                    "ok": False,
                    "classification": "FAIL_HARNESS",
                    "receipt_verdict": "BLOCKED_BY_WRITER_BARRIER",
                    "errors": ["sealed_capture_forbidden_without_scope_barrier"],
                }

        failing_classifications = {
            str(value.get("classification") or "FAIL_HARNESS")
            for value in targets.values()
            if value.get("ok") is not True
        }
        classification = (
            "PASS"
            if not errors
            else "FAIL_HARNESS"
            if "FAIL_HARNESS" in failing_classifications or barrier_errors
            else "FAIL_PRODUCT"
        )
        index = {
            "schema_version": SUPERVISOR_AUDIT_EVIDENCE_SCHEMA_VERSION,
            "receipt_schema_version": AUDIT_EVIDENCE_SCHEMA_VERSION,
            "required": True,
            "mode": "sealed",
            "created_at": utc_now(),
            "writer_barrier": barrier,
            "required_targets": sorted(runtime_roots),
            "targets": targets,
            "errors": sorted(set(errors)),
            "classification": classification,
            "ok": not errors and all(
                value.get("ok") is True for value in targets.values()
            ),
        }
        index_path = evidence_root / "artifact_index.json"
        atomic_write_json(index_path, index)
        files: list[dict[str, Any]] = []
        manifest_errors: list[str] = []
        for path in sorted(evidence_root.rglob("*")):
            if path.name == "hash_manifest.json":
                continue
            try:
                info = os.lstat(path)
                if stat.S_ISDIR(info.st_mode):
                    continue
                metadata = self._audit_artifact_metadata(path)
                files.append({
                    "path": str(path.relative_to(evidence_root)),
                    **metadata,
                })
            except Exception as exc:
                manifest_errors.append(
                    f"{path.relative_to(evidence_root)}:{exc.__class__.__name__}"
                )
        if manifest_errors:
            index["ok"] = False
            index["classification"] = "FAIL_HARNESS"
            index["errors"] = sorted(set(
                list(index["errors"])
                + [f"hash_manifest:{code}" for code in manifest_errors]
            ))
            atomic_write_json(index_path, index)
            index_metadata = self._audit_artifact_metadata(index_path)
            for row in files:
                if row.get("path") == "artifact_index.json":
                    row.update(index_metadata)
                    break
        manifest_path = evidence_root / "hash_manifest.json"
        atomic_write_json(manifest_path, {
            "schema_version": "hackme.supervisor-audit-evidence-hash-manifest/v1",
            "created_at": utc_now(),
            "file_count": len(files),
            "files": files,
            "errors": manifest_errors,
            "ok": not manifest_errors,
        })
        for path in sorted(evidence_root.rglob("*"), reverse=True):
            info = os.lstat(path)
            os.chmod(path, 0o500 if stat.S_ISDIR(info.st_mode) else 0o400)
        os.chmod(evidence_root, 0o500)
        return {
            **index,
            "artifact_root": str(evidence_root),
            "artifact_index": {
                "path": str(index_path),
                **self._audit_artifact_metadata(index_path),
            },
            "hash_manifest": {
                "path": str(manifest_path),
                **self._audit_artifact_metadata(manifest_path),
            },
        }

    @staticmethod
    def _scan_receipt_summary(scan: Mapping[str, Any]) -> dict[str, Any]:
        def path_digest(value: Any) -> str:
            return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

        return {
            "schema_version": scan.get("schema_version"),
            "scope": scan.get("scope"),
            "ok": scan.get("ok") is True,
            "files": int(scan.get("files") or 0),
            "files_scanned": int(scan.get("files_scanned") or 0),
            "bytes_scanned": int(scan.get("bytes_scanned") or 0),
            "verification_bytes": int(scan.get("verification_bytes") or 0),
            "io_bytes_read": int(scan.get("io_bytes_read") or 0),
            "entries": int(scan.get("entries") or 0),
            "enumeration_complete": scan.get("enumeration_complete") is True,
            "hit_count": int(scan.get("hit_count") or 0),
            "error_count": int(scan.get("error_count") or 0),
            "symlink_count": int(scan.get("symlink_count") or 0),
            "protected_files": int(scan.get("protected_files") or 0),
            "inventory": [
                {
                    "path_sha256": str(
                        row.get("path_sha256") or path_digest(row.get("path"))
                    ),
                    "size_bytes": int(row.get("size_bytes") or 0),
                    "sha256": str(row.get("sha256") or ""),
                    "mode": str(row.get("mode") or ""),
                    "owner_uid": int(row.get("owner_uid") or 0),
                    "link_count": int(row.get("link_count") or 0),
                }
                for row in (scan.get("file_inventory") or [])
                if isinstance(row, Mapping)
            ],
            "inventory_truncated": scan.get("file_inventory_truncated") is True,
            "hits": [
                {
                    "label": str(row.get("label") or ""),
                    "path_sha256": str(
                        row.get("path_sha256") or path_digest(row.get("path"))
                    ),
                    "byte_offset": int(row.get("byte_offset") or 0),
                }
                for row in (scan.get("hits") or [])
                if isinstance(row, Mapping)
            ],
            "errors": [
                {
                    "code": str(row.get("code") or ""),
                    "path_sha256": str(
                        row.get("path_sha256") or path_digest(row.get("path"))
                    ),
                }
                for row in (scan.get("errors") or [])
                if isinstance(row, Mapping)
            ],
        }

    def _publish_fail_closed_secret_scan_receipt(
        self,
        *,
        reason_code: str,
        writers_stopped: bool,
        finalizer_error: BaseException | None = None,
    ) -> dict[str, Any]:
        """Persist a fixed-schema external receipt when full finalization fails."""

        if (
            self.final_secret_scan_receipt == self.root
            or self.root in self.final_secret_scan_receipt.parents
        ):
            raise SupervisorError(
                "fail-closed secret scan receipt must be outside the artifact root"
            )
        error_class = finalizer_error.__class__.__name__ if finalizer_error else ""
        error_sha256 = (
            hashlib.sha256(str(finalizer_error).encode("utf-8")).hexdigest()
            if finalizer_error is not None
            else ""
        )
        receipt = {
            "schema_version": "hackme.authoritative-final-secret-scan-receipt.v1",
            "campaign_uuid": self.campaign_uuid,
            "artifact_root_sha256": hashlib.sha256(
                str(self.root).encode("utf-8")
            ).hexdigest(),
            "artifact_cutoff_at": utc_now(),
            "all_root_writers_stopped": bool(writers_stopped),
            "control_snapshot": {"ok": False, "files": 0, "bytes": 0, "error_count": 1},
            "checkpoint_mirror_snapshot": {
                "ok": False,
                "files": 0,
                "bytes": 0,
                "error_count": 1,
            },
            "root_scan": {
                "schema_version": None,
                "scope": "recursive_tree",
                "ok": False,
                "files": 0,
                "files_scanned": 0,
                "bytes_scanned": 0,
                "enumeration_complete": False,
                "hit_count": 0,
                "error_count": 1,
                "errors": [{
                    "code": str(reason_code),
                    "path_sha256": hashlib.sha256(
                        str(self.root).encode("utf-8")
                    ).hexdigest(),
                }],
            },
            "post_scan_artifacts": [],
            "receipt_storage": {
                "path_sha256": hashlib.sha256(
                    str(self.final_secret_scan_receipt).encode("utf-8")
                ).hexdigest(),
                "outside_artifact_root": True,
                "fixed_schema_contains_secret_values": False,
            },
            "finalizer": {
                "status": "FAIL",
                "reason_code": str(reason_code),
                "error_class": error_class,
                "error_sha256": error_sha256,
            },
            "ok": False,
        }
        atomic_write_json(self.final_secret_scan_receipt, receipt)
        info = self.final_secret_scan_receipt.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or int(info.st_uid) != os.getuid()
            or int(info.st_nlink) != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise SupervisorError("fail-closed secret scan receipt metadata is invalid")
        readback = load_json(self.final_secret_scan_receipt)
        if readback != receipt:
            raise SupervisorError("fail-closed secret scan receipt readback mismatch")
        return receipt

    def _authoritative_final_scan_and_publish(
        self,
        result: dict[str, Any],
        *,
        base_ok: bool,
        writers_stopped: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Scan the sealed final tree and separately verify the final report."""

        def progress(detail: str) -> None:
            print(json.dumps({
                "event": "authoritative_secret_scan_progress",
                "campaign_uuid": self.campaign_uuid,
                "detail": detail,
            }, ensure_ascii=False), flush=True)

        snapshot_durability = (
            CONTROL_SNAPSHOT_DURABILITY_MANIFEST
            if self.config.level == "smoke"
            else CONTROL_SNAPSHOT_DURABILITY_PER_FILE
        )

        checkpoint_mirror_snapshot = snapshot_control_evidence(
            ControlSnapshotConfig(
                source_root=self.checkpoint_mirror_path.parent,
                snapshot_root=self.control_root / "checkpoint_mirror_snapshot",
                durability_mode=snapshot_durability,
            ),
            progress_callback=progress,
        )
        control_snapshot = snapshot_control_evidence(
            ControlSnapshotConfig(
                source_root=self.control_root,
                snapshot_root=self.artifact_dir / "supervisor_control_snapshot",
                durability_mode=snapshot_durability,
            ),
            progress_callback=progress,
        )
        needles = build_sensitive_needle_inventory({
            "root": self.credentials.root,
            "manager": self.credentials.manager,
            "test": self.credentials.test,
            "member": self.credentials.member,
        }, environment=os.environ)
        runtime_roots = (
            self.root / "primary" / "runtime",
            self.root / "recovery" / "runtime",
            self.root / "security_sentinel" / "runtime",
        )
        root_scan = scan_campaign_secrets(
            SecretScanConfig(
                artifact_root=self.root,
                needles=needles,
                controlled_runtime_roots=runtime_roots,
            ),
            progress_callback=progress,
        )
        artifact_cutoff_at = utc_now()
        root_summary = self._scan_receipt_summary(root_scan)
        scan_ok = bool(
            writers_stopped
            and checkpoint_mirror_snapshot.get("ok")
            and control_snapshot.get("ok")
            and root_scan.get("ok")
        )
        result.update({
            "ok": bool(base_ok and scan_ok),
            "classification": (
                "PASS"
                if base_ok and scan_ok
                else (
                    "FAIL_PRODUCT"
                    if int(root_scan.get("hit_count") or 0) > 0
                    else str(result.get("classification") or "FAIL_HARNESS")
                )
            ),
            "authoritative_secret_scan": {
                "required": True,
                "artifact_cutoff_at": artifact_cutoff_at,
                "root_scan": root_summary,
                "control_snapshot": {
                    "ok": control_snapshot.get("ok") is True,
                    "files": int(control_snapshot.get("files") or 0),
                    "bytes": int(control_snapshot.get("bytes") or 0),
                    "rounds": int(control_snapshot.get("rounds") or 0),
                    "error_count": int(control_snapshot.get("error_count") or 0),
                },
                "checkpoint_mirror_snapshot": {
                    "ok": checkpoint_mirror_snapshot.get("ok") is True,
                    "files": int(checkpoint_mirror_snapshot.get("files") or 0),
                    "bytes": int(checkpoint_mirror_snapshot.get("bytes") or 0),
                    "error_count": int(
                        checkpoint_mirror_snapshot.get("error_count") or 0
                    ),
                },
                "receipt": str(self.final_secret_scan_receipt),
                "post_scan_artifacts": [
                    {
                        "path": str(self.final_path),
                        "policy": "exact-file stable raw-byte scan after publication",
                    },
                    {
                        "path": str(self.final_secret_scan_receipt),
                        "policy": "external fixed-schema secret-safe receipt",
                    },
                ],
            },
        })
        atomic_write_json(self.final_path, result)
        final_report_scan = scan_campaign_secret_files(
            SecretScanConfig(
                artifact_root=self.root,
                needles=needles,
                controlled_runtime_roots=runtime_roots,
            ),
            (self.final_path,),
            progress_callback=progress,
        )
        if not final_report_scan.get("ok"):
            result["ok"] = False
            result["classification"] = (
                "FAIL_PRODUCT"
                if int(final_report_scan.get("hit_count") or 0) > 0
                else "FAIL_HARNESS"
            )
            result["authoritative_secret_scan"]["final_report_first_scan"] = (
                self._scan_receipt_summary(final_report_scan)
            )
            atomic_write_json(self.final_path, result)
            final_report_scan = scan_campaign_secret_files(
                SecretScanConfig(
                    artifact_root=self.root,
                    needles=needles,
                    controlled_runtime_roots=runtime_roots,
                ),
                (self.final_path,),
                progress_callback=progress,
            )
        final_report_summary = self._scan_receipt_summary(final_report_scan)
        overall_ok = bool(result.get("ok") and final_report_scan.get("ok"))
        if not overall_ok:
            result["ok"] = False
            if result.get("classification") == "PASS":
                result["classification"] = "FAIL_HARNESS"
        receipt = {
            "schema_version": "hackme.authoritative-final-secret-scan-receipt.v1",
            "campaign_uuid": self.campaign_uuid,
            "artifact_root_sha256": hashlib.sha256(
                str(self.root).encode("utf-8")
            ).hexdigest(),
            "artifact_cutoff_at": artifact_cutoff_at,
            "all_root_writers_stopped": bool(writers_stopped),
            "control_snapshot": {
                "ok": control_snapshot.get("ok") is True,
                "files": int(control_snapshot.get("files") or 0),
                "bytes": int(control_snapshot.get("bytes") or 0),
                "rounds": int(control_snapshot.get("rounds") or 0),
                "error_count": int(control_snapshot.get("error_count") or 0),
            },
            "checkpoint_mirror_snapshot": {
                "ok": checkpoint_mirror_snapshot.get("ok") is True,
                "files": int(checkpoint_mirror_snapshot.get("files") or 0),
                "bytes": int(checkpoint_mirror_snapshot.get("bytes") or 0),
                "error_count": int(
                    checkpoint_mirror_snapshot.get("error_count") or 0
                ),
            },
            "root_scan": root_summary,
            "post_scan_artifacts": [
                {
                    "path_sha256": hashlib.sha256(
                        str(self.final_path).encode("utf-8")
                    ).hexdigest(),
                    "scan": final_report_summary,
                }
            ],
            "receipt_storage": {
                "path_sha256": hashlib.sha256(
                    str(self.final_secret_scan_receipt).encode("utf-8")
                ).hexdigest(),
                "outside_artifact_root": True,
                "fixed_schema_contains_secret_values": False,
            },
            "ok": overall_ok,
        }
        atomic_write_json(self.final_secret_scan_receipt, receipt)
        return result, receipt

    def run(self) -> int:
        started_at = utc_now()
        cleanup: dict[str, Any] = {}
        runner_returncode = 2
        source_final: dict[str, Any] = {}
        runner_payload: dict[str, Any] = {}
        try:
            self.prepare()
            self._verify_host_safety_preflight()
            self._capture_source()
            try:
                self._open_authenticated_control_server()
            except Exception as exc:
                self._fail_authenticated_control_channel(
                    error=exc,
                    reason="socket_server_creation_failed",
                )
                raise SupervisorError(
                    "cannot create authenticated campaign control socket"
                ) from exc
            self.cgroup.configure_managed_command(
                self._runner_command(),
                activation_gate=self.managed_exec_gate_path,
                cwd=ROOT,
                stdout=self.runner_stdout,
                environment={
                    "PYTHONPATH": str(ROOT),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "HACKME_CAMPAIGN_UUID": self.campaign_uuid,
                    "HACKME_CAMPAIGN_SUPERVISOR_CONTRACT": str(self.contract_path),
                },
            )
            credential_environment = {
                "HACKME_CAMPAIGN_ROOT_PASSWORD": self.credentials.root,
                "HACKME_CAMPAIGN_MANAGER_PASSWORD": self.credentials.manager,
                "HACKME_CAMPAIGN_TEST_PASSWORD": self.credentials.test,
                "HACKME_CAMPAIGN_MEMBER_PASSWORD": self.credentials.member,
            }
            previous_environment = {
                name: os.environ.get(name) for name in credential_environment
            }
            os.environ.update(credential_environment)
            try:
                # The managed anchor inherits credentials directly.  They are
                # deliberately absent from managed_command.json and argv.
                cgroup_evidence = self.cgroup.create_scope()
            finally:
                for name, previous in previous_environment.items():
                    if previous is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = previous
            self._gate("cgroup_limits_verified", passed=True, evidence=cgroup_evidence)
            if self.comfyui_backend is not None:
                try:
                    comfyui_evidence = self.comfyui_backend.start()
                    environment_update = (
                        self.cgroup.update_managed_environment_before_activation(
                            self.comfyui_backend.runner_environment()
                        )
                    )
                    self._gate(
                        "comfyui_backend_lifecycle_verified",
                        passed=True,
                        evidence={
                            **comfyui_evidence,
                            "runner_environment": {
                                "keys": sorted(
                                    self.comfyui_backend.runner_environment()
                                ),
                                "contains_secret_values": False,
                                "update": environment_update,
                            },
                        },
                    )
                    self._verify_host_safety_backend_startup_settled()
                except Exception as exc:
                    self._gate(
                        "comfyui_backend_lifecycle_verified",
                        passed=False,
                        evidence={"configured": True},
                        error=f"{exc.__class__.__name__}: {exc}",
                    )
                    raise SupervisorError(
                        "managed ComfyUI backend failed before runner activation"
                    ) from exc
            self._verify_host_safety_runner_launch()
            managed_exec_evidence = self._release_managed_runner_exec()
            self._gate(
                "managed_runner_exec_released",
                passed=True,
                evidence=managed_exec_evidence,
            )
            runner_identity = self._launch_runner_gated()
            self.cgroup.register_pid("scenario", self.runner_pid)
            self._authenticate_runner_control(
                runner_identity=runner_identity,
                deadline=(
                    time.monotonic()
                    + self.config.runner_bootstrap_timeout_seconds
                ),
            )
            self._verify_staged_import_evidence(
                profile="runner",
                process_identity=runner_identity,
            )
            self._verify_host_safety_runner_import_settled()
            self._initialize_state_files(runner_identity)
            self._verify_host_safety_state_initialization_settled(
                runner_identity
            )
            watchdog_evidence = self._launch_watchdog(runner_identity)
            if self.config.level in {"soak", "formal"}:
                self._require_authenticated_control_channel()
            self._gate("external_watchdog_verified", passed=True, evidence=watchdog_evidence)
            assert self.watchdog is not None
            placement = {
                "runner": self.cgroup.assert_pid_membership(self.runner_pid, role="campaign_runner"),
                "watchdog": self.cgroup.assert_watchdog_outside(self.watchdog.pid),
                "role_inheritance_gate": (
                    "single_direct_child_kernel_inheritance_verified_before_ACTIVE"
                ),
                "ok": True,
            }
            if self.comfyui_backend is not None:
                comfyui_live = self.comfyui_backend.check_live()
                placement["comfyui_backend"] = {
                    "lifecycle": comfyui_live,
                    "cgroup": self.cgroup.assert_pid_membership(
                        int(self.comfyui_backend.process_identity.pid),
                        role="comfyui",
                        expected_identity=self.comfyui_backend.process_identity,
                    ),
                }
            self._gate("runner_and_watchdog_placement_verified", passed=True, evidence=placement)
            event_evidence = self.cgroup.verify_event_counters_unchanged()
            self._gate(
                "cgroup_event_baseline_verified",
                passed=True,
                evidence=event_evidence,
            )
            self._verify_host_safety_activation(runner_identity)
            self._release_runner(
                cgroup_evidence=cgroup_evidence,
                watchdog_evidence=watchdog_evidence,
                placement=placement,
            )
            runner_returncode = self._monitor_runner()
            if self.failure:
                raise SupervisorError(self.failure)
            source_final = self.freezer.verify_final(require_clean=self.config.level == "formal")
            if not source_final.get("verified"):
                raise SupervisorError("H24 source verification differs from H0")
            runner_report = self.root / "reports" / "operational_campaign_24h.json"
            runner_payload = _load_required_json(runner_report, label="campaign runner report")
            runner_ok = runner_returncode == 0 and runner_payload.get("ok") is True
            cleanup = self._cleanup(normal=runner_ok)
            cleanup["ephemeral_tls_private_keys"] = (
                self._purge_stopped_server_tls_private_keys(cleanup)
            )
            writers_stopped = bool(
                cleanup.get("authenticated_control_channel", {}).get("ok")
                and cleanup.get("source_monitor", {}).get("ok")
                and cleanup.get("comfyui_backend", {}).get("ok")
                and cleanup.get("watchdog", {}).get("ok")
                and cleanup.get("scope", {}).get("ok")
                and cleanup.get("ephemeral_tls_private_keys", {}).get("ok")
            )
            supervisor_audit_evidence = self._capture_post_scope_audit_evidence(
                cleanup
            )
            ok = bool(
                runner_ok
                and source_final.get("verified")
                and writers_stopped
                and supervisor_audit_evidence.get("ok") is True
            )
            result = {
                "schema_version": SUPERVISOR_SCHEMA_VERSION,
                "campaign_uuid": self.campaign_uuid,
                "level": self.config.level,
                "started_at": started_at,
                "finished_at": utc_now(),
                "commit": self.commit,
                "source_digest": self.source_h0.get("tracked_content_digest"),
                "runner_returncode": runner_returncode,
                "runner_report": str(runner_report),
                "runner_verdict": runner_payload.get("verdict"),
                "source_final": source_final,
                "gates": self.gates,
                "cleanup": cleanup,
                "supervisor_audit_evidence": supervisor_audit_evidence,
                "classification": (
                    "PASS"
                    if ok
                    else str(
                        supervisor_audit_evidence.get("classification")
                        if supervisor_audit_evidence.get("ok") is not True
                        else runner_payload.get("classification")
                        or "FAIL_HARNESS"
                    )
                ),
                "ok": ok,
            }
            result, _secret_scan_receipt = self._authoritative_final_scan_and_publish(
                result,
                base_ok=ok,
                writers_stopped=writers_stopped,
            )
            try:
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            except OSError:
                # stdout is outside the artifact authority; a closed caller
                # pipe must not rewrite the already scanned final root.
                pass
            return 0 if result.get("ok") else 1
        except (Exception, KeyboardInterrupt) as exc:
            self.failure = self.failure or f"{exc.__class__.__name__}: {exc}"
            hard_io_abort = self._host_io_hard_limit_was_exceeded()
            hard_io_termination: dict[str, Any] = {}
            if hard_io_abort:
                hard_io_termination = self._terminate_processes_after_hard_io()
            elif self.runner is not None and self.runner.poll() is None:
                try:
                    self._request_hard_stop(
                        reason="SUPERVISOR_FAILURE",
                        evidence={"error": self.failure},
                    )
                except Exception:
                    # State may not exist yet when a staged import fails.  A
                    # finalizer failure must never leave the managed scope live.
                    try:
                        os.killpg(self.runner.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    self.runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.runner.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            else:
                self._mark_failed_before_active(
                    reason="RUNNER_EXITED_BEFORE_REPORT",
                    error=self.failure,
                )
            source_final = self._exception_source_final()
            pre_cleanup_quiescence: dict[str, Any] = {}
            post_hard_io_quiescence: dict[str, Any] = {}
            if hard_io_abort:
                pre_cleanup_quiescence = (
                    self._wait_for_post_hard_io_quiescence()
                )
                if pre_cleanup_quiescence.get("ok") is True:
                    try:
                        cleanup = self._cleanup(normal=False)
                    except Exception as cleanup_exc:
                        cleanup = {
                            name: {
                                "ok": False,
                                "deferred": False,
                                "error_code": cleanup_exc.__class__.__name__,
                            }
                            for name in (
                                "authenticated_control_channel",
                                "source_monitor",
                                "comfyui_backend",
                                "watchdog",
                                "scope",
                            )
                        }
                    after_cleanup_quiescence = (
                        self._wait_for_post_hard_io_quiescence()
                    )
                else:
                    # Do not perform filesystem/cgroup cleanup while delayed
                    # hard I/O has not demonstrably settled.  Exact managed
                    # process groups were already SIGKILLed above; remaining
                    # descriptors are released by process exit.
                    cleanup = {
                        name: {
                            "ok": False,
                            "deferred": True,
                            "reason_code": "POST_HARD_IO_QUIESCENCE_NOT_PROVEN",
                        }
                        for name in (
                            "authenticated_control_channel",
                            "source_monitor",
                            "comfyui_backend",
                            "watchdog",
                            "scope",
                        )
                    }
                    after_cleanup_quiescence = {
                        "ok": False,
                        "not_attempted": True,
                        "reason_code": "PRE_CLEANUP_QUIESCENCE_NOT_PROVEN",
                    }
                cleanup["hard_io_immediate_termination"] = hard_io_termination
                post_hard_io_quiescence = {
                    "ok": bool(
                        pre_cleanup_quiescence.get("ok") is True
                        and after_cleanup_quiescence.get("ok") is True
                    ),
                    "before_cleanup": pre_cleanup_quiescence,
                    "after_cleanup": after_cleanup_quiescence,
                }
                cleanup["post_hard_io_quiescence"] = post_hard_io_quiescence
            else:
                cleanup = self._cleanup(normal=False)
            if not hard_io_abort or post_hard_io_quiescence.get("ok") is True:
                cleanup["ephemeral_tls_private_keys"] = (
                    self._purge_stopped_server_tls_private_keys(cleanup)
                )
                if hard_io_abort:
                    after_tls_purge = self._wait_for_post_hard_io_quiescence()
                    post_hard_io_quiescence["after_tls_purge"] = after_tls_purge
                    post_hard_io_quiescence["ok"] = bool(
                        post_hard_io_quiescence.get("ok") is True
                        and after_tls_purge.get("ok") is True
                    )
            else:
                cleanup["ephemeral_tls_private_keys"] = {
                    "ok": False,
                    "deferred": True,
                    "reason": "post-hard-I/O quiescence was not proven",
                }
            writers_stopped = bool(
                cleanup.get("authenticated_control_channel", {}).get("ok")
                and cleanup.get("source_monitor", {}).get("ok")
                and cleanup.get("comfyui_backend", {}).get("ok")
                and cleanup.get("watchdog", {}).get("ok")
                and cleanup.get("scope", {}).get("ok")
                and cleanup.get("ephemeral_tls_private_keys", {}).get("ok")
            )
            self._gate(
                "exception_finalizer_fail_closed_policy_verified",
                passed=True,
                evidence={
                    "authoritative_scan_required": not hard_io_abort,
                    "full_tree_scan_suppressed_for_hard_io": hard_io_abort,
                    "external_failure_receipt_required": True,
                    "campaign_root_initialized": self.campaign_root_initialized,
                    "writers_stopped": writers_stopped,
                },
            )
            if hard_io_abort:
                self._gate(
                    "hard_io_failure_finalizer_load_suppressed",
                    passed=True,
                    evidence={
                        "h24_source_capture_skipped": True,
                        "full_tree_secret_scan_skipped": True,
                        "post_hard_io_quiescence": post_hard_io_quiescence,
                        "minimal_writes_allowed_only_after_quiescence": True,
                    },
                )
            result = {
                "schema_version": SUPERVISOR_SCHEMA_VERSION,
                "campaign_uuid": self.campaign_uuid,
                "level": self.config.level,
                "started_at": started_at,
                "finished_at": utc_now(),
                "commit": self.commit,
                "gates": self.gates,
                "source_final": source_final,
                "cleanup": cleanup,
                "classification": "INVALIDATED" if "source drift" in self.failure.lower() else "FAIL_HARNESS",
                "error_code": exc.__class__.__name__,
                "error_sha256": hashlib.sha256(
                    str(exc).encode("utf-8")
                ).hexdigest(),
                "authoritative_secret_scan": {
                    "required": True,
                    "exception_path": True,
                    "status": "PENDING",
                },
                "ok": False,
            }
            if hard_io_abort:
                result["gates"] = self.gates
                result["authoritative_secret_scan"] = {
                    "required": True,
                    "exception_path": True,
                    "status": "SKIPPED_DUE_TO_HOST_IO_HARD_LIMIT",
                    "root_scan_verified": False,
                    "final_report_scan_verified": False,
                    "fail_closed": True,
                    "external_failure_receipt_required": True,
                    "minimal_report_contains_raw_error": False,
                }
                if (
                    post_hard_io_quiescence.get("ok") is True
                    and self.campaign_root_initialized
                    and writers_stopped
                ):
                    finalizer_error = SupervisorError(
                        "host I/O hard limit suppressed scan-heavy finalization"
                    )
                    try:
                        receipt = self._publish_fail_closed_secret_scan_receipt(
                            reason_code="host_io_hard_limit_finalizer_suppressed",
                            writers_stopped=writers_stopped,
                            finalizer_error=finalizer_error,
                        )
                        result["authoritative_secret_scan"].update({
                            "external_failure_receipt_verified": True,
                            "external_failure_receipt_payload_sha256": hashlib.sha256(
                                json.dumps(
                                    receipt,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest(),
                        })
                    except Exception as receipt_exc:
                        result["authoritative_secret_scan"].update({
                            "external_failure_receipt_verified": False,
                            "receipt_error_code": receipt_exc.__class__.__name__,
                        })
                    after_receipt_quiescence = (
                        self._wait_for_post_hard_io_quiescence()
                    )
                    cleanup["hard_io_after_failure_receipt_quiescence"] = (
                        after_receipt_quiescence
                    )
                    if after_receipt_quiescence.get("ok") is True:
                        result["authoritative_secret_scan"][
                            "minimal_report_written"
                        ] = True
                        result["authoritative_secret_scan"][
                            "post_minimal_report_quiescence_required"
                        ] = True
                        try:
                            self.artifact_dir.mkdir(parents=True, exist_ok=True)
                            minimal_result = (
                                self._hard_io_minimal_failure_result(result)
                            )
                            atomic_write_json(self.final_path, minimal_result)
                            result = minimal_result
                        except Exception as report_exc:
                            result["authoritative_secret_scan"][
                                "minimal_report_written"
                            ] = False
                            result["authoritative_secret_scan"][
                                "minimal_report_write_error_code"
                            ] = report_exc.__class__.__name__
                        after_report_quiescence = (
                            self._wait_for_post_hard_io_quiescence()
                        )
                        cleanup["hard_io_after_minimal_report_quiescence"] = (
                            after_report_quiescence
                        )
                        result["authoritative_secret_scan"][
                            "post_minimal_report_quiescence_verified"
                        ] = after_report_quiescence.get("ok") is True
                    else:
                        result["authoritative_secret_scan"][
                            "minimal_report_written"
                        ] = False
                        result["authoritative_secret_scan"][
                            "write_suppression_reason"
                        ] = "failure receipt I/O did not return to safe headroom"
                else:
                    if not self.campaign_root_initialized:
                        write_suppression_reason = (
                            "campaign root not initialized"
                        )
                    elif post_hard_io_quiescence.get("ok") is not True:
                        write_suppression_reason = (
                            "post-hard-I/O quiescence not proven"
                        )
                    else:
                        write_suppression_reason = (
                            "campaign writers not proven stopped"
                        )
                    result["authoritative_secret_scan"].update({
                        "external_failure_receipt_verified": False,
                        "minimal_report_written": False,
                        "write_suppression_reason": write_suppression_reason,
                    })
                if result.get("report_mode") != "hard_io_minimal_allowlisted":
                    result = self._hard_io_minimal_failure_result(result)
                # The hard-I/O path deliberately emits no stdout/stderr.  A
                # redirected stream is another write after the final safety
                # sample and would invalidate the quiescence barrier.
                return 2
            receipt: dict[str, Any] | None = None
            finalizer_error: BaseException | None = None
            if self.campaign_root_initialized:
                try:
                    result, receipt = self._authoritative_final_scan_and_publish(
                        result,
                        base_ok=False,
                        writers_stopped=writers_stopped,
                    )
                except Exception as scan_exc:
                    finalizer_error = scan_exc
            else:
                finalizer_error = SupervisorError(
                    "campaign root was not initialized by this supervisor"
                )
            if finalizer_error is not None:
                self._gate(
                    "exception_authoritative_finalizer_completed",
                    passed=False,
                    evidence={
                        "campaign_root_initialized": self.campaign_root_initialized,
                        "writers_stopped": writers_stopped,
                    },
                    error=finalizer_error.__class__.__name__,
                )
                result["gates"] = self.gates
                result["authoritative_secret_scan"] = {
                    "required": True,
                    "exception_path": True,
                    "status": "FAIL_CLOSED",
                    "root_scan_verified": False,
                    "final_report_scan_verified": False,
                    "external_failure_receipt_required": True,
                    "finalizer_error_code": finalizer_error.__class__.__name__,
                    "finalizer_error_sha256": hashlib.sha256(
                        str(finalizer_error).encode("utf-8")
                    ).hexdigest(),
                }
                if self.campaign_root_initialized:
                    try:
                        self.artifact_dir.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(self.final_path, result)
                    except Exception as report_exc:
                        result["authoritative_secret_scan"][
                            "failure_report_write_error_code"
                        ] = report_exc.__class__.__name__
                try:
                    receipt = self._publish_fail_closed_secret_scan_receipt(
                        reason_code="exception_authoritative_finalizer_failed",
                        writers_stopped=writers_stopped,
                        finalizer_error=finalizer_error,
                    )
                except Exception as receipt_exc:
                    result["authoritative_secret_scan"][
                        "external_failure_receipt_verified"
                    ] = False
                    result["authoritative_secret_scan"][
                        "receipt_error_code"
                    ] = receipt_exc.__class__.__name__
            else:
                # The authoritative receipt is the immutable completion proof;
                # do not mutate the already exact-scanned in-root report here.
                assert receipt is not None
            try:
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            except OSError:
                pass
            return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--campaign-root", required=True)
    parser.add_argument("--level", choices=("smoke", "rehearsal", "soak", "formal"), required=True)
    parser.add_argument("--duration-seconds", type=int, default=0)
    parser.add_argument("--authorization-file")
    parser.add_argument("--gate-bundle-file")
    parser.add_argument("--source-poll-seconds", type=float, default=5.0)
    parser.add_argument("--keep-scope-on-failure", action="store_true")
    parser.add_argument("--comfyui-python-executable")
    parser.add_argument("--comfyui-main")
    parser.add_argument("--comfyui-working-root")
    parser.add_argument("--comfyui-models-root")
    parser.add_argument("--comfyui-api-url")
    parser.add_argument("--comfyui-port", type=int)
    parser.add_argument("--comfyui-readiness-timeout-seconds", type=float, default=300.0)
    parser.add_argument("runner_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    default_duration = {
        "smoke": 180,
        "rehearsal": 3600,
        "soak": MIN_FORMAL_SECONDS,
        "formal": MIN_FORMAL_SECONDS,
    }[args.level]
    comfyui_values = (
        args.comfyui_python_executable,
        args.comfyui_main,
        args.comfyui_working_root,
        args.comfyui_models_root,
        args.comfyui_api_url,
        args.comfyui_port,
    )
    configured_values = sum(value is not None for value in comfyui_values)
    if configured_values not in {0, len(comfyui_values)}:
        raise SystemExit(
            "managed ComfyUI requires all of --comfyui-python-executable, "
            "--comfyui-main, --comfyui-working-root, --comfyui-models-root, "
            "--comfyui-api-url, and --comfyui-port"
        )
    comfyui_backend = (
        ComfyUIBackendConfig(
            python_executable=Path(args.comfyui_python_executable),
            main_path=Path(args.comfyui_main),
            working_root=Path(args.comfyui_working_root),
            models_root=Path(args.comfyui_models_root),
            api_url=str(args.comfyui_api_url),
            port=int(args.comfyui_port),
            readiness_timeout_seconds=float(
                args.comfyui_readiness_timeout_seconds
            ),
        )
        if configured_values
        else None
    )
    config = SupervisorConfig(
        campaign_root=Path(args.campaign_root),
        level=args.level,
        duration_seconds=int(args.duration_seconds or default_duration),
        authorization_file=Path(args.authorization_file) if args.authorization_file else None,
        gate_bundle_file=Path(args.gate_bundle_file) if args.gate_bundle_file else None,
        runner_extra_args=tuple(value for value in args.runner_args if value != "--"),
        source_poll_seconds=float(args.source_poll_seconds),
        keep_scope_on_failure=bool(args.keep_scope_on_failure),
        comfyui_backend=comfyui_backend,
    )
    return OperationalCampaignSupervisor(config).run()


if __name__ == "__main__":
    raise SystemExit(main())
