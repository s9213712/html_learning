#!/usr/bin/env python3
"""Outside-cgroup supervisor for the managed operational campaign.

This is the only supported entry point for rehearsal and formal campaigns.
It freezes source, creates and verifies a delegated cgroup, launches the
campaign runner inside that scope, starts an independent watchdog outside the
scope, and releases the runner only after every startup proof is durable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
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
from scripts.testing.campaign_gate_bundle import (  # noqa: E402
    GATE_BUNDLE_SCHEMA_VERSION,
    REQUIRED_FORMAL_GATES,
    GateBundleError,
    validate_gate_bundle as validate_hardening_gate_bundle,
)
from scripts.testing.campaign_source_freeze import GitSourceFreezer, SourceFreezeError  # noqa: E402
from scripts.testing.campaign_secret_scan import (  # noqa: E402
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
    CgroupIdentity,
    WatchdogConfig,
    WatchdogPaths,
    atomic_write_json,
    build_watchdog_command,
    capture_process_identity,
    load_json,
)
from scripts.testing.operational_campaign_24h import (  # noqa: E402
    Credentials,
    SUPERVISED_LOAD_POLICIES,
    SUPERVISED_RUNNER_PROFILE_OPTIONS,
    SUPERVISED_RUNNER_PROFILES,
    validate_control_root,
)


SUPERVISOR_SCHEMA_VERSION = "hackme.campaign-supervisor.v1"
FORMAL_AUTHORIZATION_SCHEMA_VERSION = "hackme.formal-24h-authorization.v1"
MIN_FORMAL_SECONDS = 86_400
SAFETY_STOP_GRACE_SECONDS = 15.0
AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED = False
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
    activation_timeout_seconds: float = 120.0
    keep_scope_on_failure: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "campaign_root", validate_campaign_root(self.campaign_root))
        if self.level not in {"smoke", "rehearsal", "formal"}:
            raise ValueError("level must be smoke, rehearsal, or formal")
        required = {"smoke": 180, "rehearsal": 3600, "formal": MIN_FORMAL_SECONDS}[self.level]
        if int(self.duration_seconds) != required:
            raise ValueError(f"{self.level} duration must be exactly {required} seconds")
        if self.level == "formal" and (self.authorization_file is None or self.gate_bundle_file is None):
            raise ValueError("formal campaign requires authorization_file and gate_bundle_file")
        if self.level == "formal" and self.keep_scope_on_failure:
            raise ValueError("formal campaign cannot keep its cgroup scope after failure")
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
        self.checkpoint_mirror_path = (
            Path.home()
            / "logs"
            / "hackme_web_campaign_24h"
            / self.campaign_uuid
            / "campaign.checkpoint.json"
        )
        self.watchdog_ready_path = self.checkpoint_dir / "watchdog.status.json"
        self.watchdog_lock_path = self.checkpoint_dir / "watchdog.process.lock"
        self.activation_gate_path = self.checkpoint_dir / "campaign.activation.json"
        self.contract_path = self.checkpoint_dir / "supervisor.contract.json"
        self.final_path = self.artifact_dir / "campaign_supervisor.json"
        self.final_secret_scan_receipt = (
            self.control_root / "receipts" / "authoritative_final_secret_scan.json"
        )
        self.runner_stdout = self.log_dir / "campaign_runner.stdout"
        self.watchdog_stdout = self.log_dir / "campaign_watchdog.stdout"
        self.commit = ""
        self.state_machine = CampaignStateMachine(self.state_path)
        self.cgroup = CampaignCgroup(
            campaign_id=self.campaign_uuid,
            evidence_root=self.artifact_dir / "cgroup",
        )
        self.freezer = GitSourceFreezer(ROOT, self.control_artifact_dir / "source")
        self.credentials = Credentials.load(managed_servers=True)
        self.runner: subprocess.Popen[Any] | None = None
        self.runner_pid = 0
        self.runner_log_handle: Any = None
        self.watchdog: subprocess.Popen[Any] | None = None
        self.watchdog_log_handle: Any = None
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
            "error": str(error),
        }
        self.gates[name] = row
        return row

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
            passed=AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED,
            evidence={
                "required": True,
                "implemented": AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED,
                "required_transport": "unix_sock_seqpacket",
                "required_peer_binding": ["pid", "start_ticks", "boot_id"],
                "watchdog_requires_supervisor_authenticated_heartbeat": True,
            },
            error=(
                "authenticated supervisor IPC is not implemented; campaign "
                "activation is fail-closed"
                if not AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED
                else ""
            ),
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

    def _capture_source(self) -> None:
        try:
            self.source_h0 = self.freezer.capture(label="H0", require_clean=self.config.level == "formal")
            drift_monitor = self.freezer.lightweight_drift_check()
            monitor_health = drift_monitor.get("monitor") if isinstance(drift_monitor, Mapping) else None
            monitor_verified = bool(
                drift_monitor.get("verified") is True
                and isinstance(monitor_health, Mapping)
                and monitor_health.get("machine_verified") is True
            )
            formal_monitor_required = self.config.level in {"rehearsal", "formal"}
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
                "protected_ignored_manifest_digest": self.source_h0.get("protected_ignored_manifest_digest"),
                "protected_ignored_content_digest": self.source_h0.get("protected_ignored_content_digest"),
                "artifact_root": self.source_h0.get("artifact_root"),
                "git_status_empty": self.source_h0.get("git_status_empty"),
                "require_clean": self.config.level == "formal",
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

    def _clean_repo_caches(self) -> dict[str, Any]:
        removed: list[str] = []
        errors: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_dir() or path.name not in {"__pycache__", ".pytest_cache"}:
                continue
            if ROOT / ".git" == path or ROOT / ".git" in path.parents:
                continue
            try:
                relative = str(path.relative_to(ROOT))
                shutil.rmtree(path)
                removed.append(relative)
            except Exception as exc:
                errors.append(f"{path}:{exc.__class__.__name__}:{exc}")
        result = {"removed": sorted(removed), "errors": errors, "ok": not errors}
        self._gate("repo_runtime_cache_cleanup", passed=not errors, evidence=result, error="; ".join(errors))
        if errors:
            raise SupervisorError("repo runtime cache cleanup failed")
        return result

    def _runner_command(self) -> list[str]:
        profile = SUPERVISED_RUNNER_PROFILES[self.config.level]
        profile_args = [
            value
            for name, option in SUPERVISED_RUNNER_PROFILE_OPTIONS.items()
            for value in (option, str(profile[name]))
        ]
        command = [
            sys.executable,
            str(ROOT / "scripts" / "testing" / "operational_campaign_24h.py"),
            "--campaign-root", str(self.root),
            "--duration-seconds", str(self.config.duration_seconds),
            "--supervised",
            "--campaign-uuid", self.campaign_uuid,
            "--control-root", str(self.control_root),
            "--state-path", str(self.state_path),
            "--control-path", str(self.control_path),
            "--heartbeat-path", str(self.heartbeat_path),
            "--checkpoint-path", str(self.checkpoint_path),
            "--checkpoint-mirror-path", str(self.checkpoint_mirror_path),
            "--source-freeze-path", str(Path(self.source_h0["artifact_root"]) / "source_freeze.json"),
            "--activation-gate", str(self.activation_gate_path),
            "--supervisor-contract", str(self.contract_path),
            *profile_args,
            *self.config.runner_extra_args,
        ]
        if self.config.level != "formal":
            command.append("--allow-short-duration")
        return command

    def _launch_runner_gated(self) -> Any:
        self.runner = self.cgroup.anchor_process
        if self.runner is None:
            raise SupervisorError("campaign cgroup did not retain its managed anchor process")
        deadline = time.monotonic() + self.config.activation_timeout_seconds
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
        initial_checkpoint = {
            "schema_version": "hackme.campaign-checkpoint.v1",
            "campaign_uuid": self.campaign_uuid,
            "revision": checkpoint_revision,
            "phase": "supervisor_preflight",
            "state_revision": state["revision"],
            "updated_at": utc_now(),
        }
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
        atomic_write_json(self.heartbeat_path, {
            "schema_version": "hackme.campaign-heartbeat.v1",
            "campaign_uuid": self.campaign_uuid,
            "heartbeat": {
                "orchestrator_pid": self.runner_pid,
                "orchestrator_start_ticks": identity.start_ticks,
                "orchestrator_monotonic_ns": time.monotonic_ns(),
                "checkpoint_revision": checkpoint_revision,
                "updated_at": utc_now(),
            },
        })

    def _refresh_gated_heartbeat(self, identity: Any, *, checkpoint_revision: int = 1) -> None:
        if self.runner is None or self.runner.poll() is not None:
            raise SupervisorError("cannot heartbeat a missing campaign runner")
        now_ns = time.monotonic_ns()
        self.state_machine.heartbeat(
            orchestrator_pid=self.runner_pid,
            orchestrator_start_ticks=identity.start_ticks,
            checkpoint_revision=checkpoint_revision,
            now_ns=now_ns,
        )
        atomic_write_json(self.heartbeat_path, {
            "schema_version": "hackme.campaign-heartbeat.v1",
            "campaign_uuid": self.campaign_uuid,
            "heartbeat": {
                "orchestrator_pid": self.runner_pid,
                "orchestrator_start_ticks": identity.start_ticks,
                "orchestrator_monotonic_ns": now_ns,
                "checkpoint_revision": checkpoint_revision,
                "updated_at": utc_now(),
            },
        })

    def _launch_watchdog(self, identity: Any) -> dict[str, Any]:
        cgroup_identity_row = self.cgroup.capture_scope_identity()
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
        )
        self.watchdog_log_handle = self.watchdog_stdout.open("w", encoding="utf-8")
        self.watchdog = subprocess.Popen(
            build_watchdog_command(config),
            cwd=str(ROOT),
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "PYTHONPYCACHEPREFIX": str(self.control_root / "pycache"),
            },
            stdin=subprocess.DEVNULL,
            stdout=self.watchdog_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
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
                ):
                    placement = self.cgroup.assert_watchdog_outside(self.watchdog.pid)
                    return {"watchdog": last, "placement": placement, "command": build_watchdog_command(config)}
            time.sleep(0.1)
        raise SupervisorError(f"external watchdog readiness was not proven: {last}")

    def _release_runner(self, *, cgroup_evidence: Mapping[str, Any], watchdog_evidence: Mapping[str, Any], placement: Mapping[str, Any]) -> None:
        event_gate = self.gates.get("cgroup_event_baseline_verified") or {}
        if (
            event_gate.get("status") != "PASS"
            or event_gate.get("machine_verified") is not True
            or not self.cgroup.event_baseline
        ):
            raise SupervisorError(
                "campaign runner cannot be released without a verified cgroup event baseline"
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
            "checkpoint_path": str(self.checkpoint_path),
            "checkpoint_mirror_path": str(self.checkpoint_mirror_path),
            "runner_stdout": str(self.runner_stdout),
            "watchdog_stdout": str(self.watchdog_stdout),
            "supervisor_source_root": str(self.control_artifact_dir / "source"),
            "supervisor_final_result": str(self.final_path),
            "authoritative_secret_scan_receipt": str(self.final_secret_scan_receipt),
            "watchdog_pid": self.watchdog.pid if self.watchdog else 0,
            "runner_pid": self.runner_pid,
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
        return {"ok": self.watchdog.returncode == 0, "returncode": self.watchdog.returncode, "status": load_json(self.watchdog_ready_path) if self.watchdog_ready_path.exists() else {}}

    def _cleanup(self, *, normal: bool) -> dict[str, Any]:
        try:
            self.freezer.close()
            source_monitor = {"ok": True, "closed": True}
        except Exception as exc:
            source_monitor = {
                "ok": False,
                "closed": False,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
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
        return {"source_monitor": source_monitor, "watchdog": watchdog, "scope": scope}

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

        checkpoint_mirror_snapshot = snapshot_control_evidence(
            ControlSnapshotConfig(
                source_root=self.checkpoint_mirror_path.parent,
                snapshot_root=self.control_root / "checkpoint_mirror_snapshot",
            ),
            progress_callback=progress,
        )
        control_snapshot = snapshot_control_evidence(
            ControlSnapshotConfig(
                source_root=self.control_root,
                snapshot_root=self.artifact_dir / "supervisor_control_snapshot",
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
            if self.config.level == "formal":
                self._require_authenticated_control_channel()
            self._clean_repo_caches()
            self._capture_source()
            self.cgroup.configure_managed_command(
                self._runner_command(),
                activation_gate=self.activation_gate_path,
                cwd=ROOT,
                stdout=self.runner_stdout,
                environment={
                    "PYTHONPATH": str(ROOT),
                    "PYTHONPYCACHEPREFIX": str(self.control_root / "pycache"),
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
            runner_identity = self._launch_runner_gated()
            self.cgroup.register_pid("scenario", self.runner_pid)
            self._initialize_state_files(runner_identity)
            watchdog_evidence = self._launch_watchdog(runner_identity)
            self._gate("external_watchdog_verified", passed=True, evidence=watchdog_evidence)
            assert self.watchdog is not None
            placement = {
                "runner": self.cgroup.assert_pid_membership(self.runner_pid, role="campaign_runner"),
                "watchdog": self.cgroup.assert_watchdog_outside(self.watchdog.pid),
                "role_inheritance_gate": "runner_preflight_required_before_ACTIVE",
                "ok": True,
            }
            self._gate("runner_and_watchdog_placement_verified", passed=True, evidence=placement)
            event_evidence = self.cgroup.verify_event_counters_unchanged()
            self._gate(
                "cgroup_event_baseline_verified",
                passed=True,
                evidence=event_evidence,
            )
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
            writers_stopped = bool(
                cleanup.get("source_monitor", {}).get("ok")
                and cleanup.get("watchdog", {}).get("ok")
                and cleanup.get("scope", {}).get("ok")
            )
            ok = bool(runner_ok and source_final.get("verified") and writers_stopped)
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
                "classification": "PASS" if ok else str(runner_payload.get("classification") or "FAIL_HARNESS"),
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
        except Exception as exc:
            self.failure = self.failure or f"{exc.__class__.__name__}: {exc}"
            if self.runner is not None and self.runner.poll() is None:
                self._request_hard_stop(reason="SUPERVISOR_FAILURE", evidence={"error": self.failure})
                try:
                    self.runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.runner.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            else:
                self._mark_failed_before_active(reason="RUNNER_EXITED_BEFORE_REPORT", error=self.failure)
            try:
                source_final = self.freezer.verify_final(require_clean=self.config.level == "formal") if self.source_h0 else {}
            except Exception as source_exc:
                source_final = {
                    "verified": False,
                    "error_code": source_exc.__class__.__name__,
                    "error_sha256": hashlib.sha256(
                        str(source_exc).encode("utf-8")
                    ).hexdigest(),
                }
            cleanup = self._cleanup(normal=False)
            writers_stopped = bool(
                cleanup.get("source_monitor", {}).get("ok")
                and cleanup.get("watchdog", {}).get("ok")
                and cleanup.get("scope", {}).get("ok")
            )
            self._gate(
                "exception_finalizer_fail_closed_policy_verified",
                passed=True,
                evidence={
                    "authoritative_scan_required": True,
                    "external_failure_receipt_required": True,
                    "campaign_root_initialized": self.campaign_root_initialized,
                    "writers_stopped": writers_stopped,
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
    parser.add_argument("--level", choices=("smoke", "rehearsal", "formal"), required=True)
    parser.add_argument("--duration-seconds", type=int, default=0)
    parser.add_argument("--authorization-file")
    parser.add_argument("--gate-bundle-file")
    parser.add_argument("--source-poll-seconds", type=float, default=5.0)
    parser.add_argument("--keep-scope-on-failure", action="store_true")
    parser.add_argument("runner_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    default_duration = {"smoke": 180, "rehearsal": 3600, "formal": MIN_FORMAL_SECONDS}[args.level]
    config = SupervisorConfig(
        campaign_root=Path(args.campaign_root),
        level=args.level,
        duration_seconds=int(args.duration_seconds or default_duration),
        authorization_file=Path(args.authorization_file) if args.authorization_file else None,
        gate_bundle_file=Path(args.gate_bundle_file) if args.gate_bundle_file else None,
        runner_extra_args=tuple(value for value in args.runner_args if value != "--"),
        source_poll_seconds=float(args.source_poll_seconds),
        keep_scope_on_failure=bool(args.keep_scope_on_failure),
    )
    return OperationalCampaignSupervisor(config).run()


if __name__ == "__main__":
    raise SystemExit(main())
