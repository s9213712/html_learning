#!/usr/bin/env python3
"""Real Level-0 cgroup/watchdog SIGSTOP fault injection.

This is intentionally separate from the campaign supervisor.  It creates one
real delegated cgroup with the production limits, starts a tiny orchestrator
and load worker inside that cgroup, starts the production external watchdog
outside it, and then stops only the orchestrator with SIGSTOP.  The watchdog
must observe a genuinely stale 120-second heartbeat, close load admission,
freeze the continuous clock, preserve incident evidence, and kill the whole
managed scope.

The public CLI has no shortened-timeout or development mode.  Unit tests use
the pure evidence assessor instead of pretending that a simulated clock is a
formal proof.  No Markdown is generated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.campaign_cgroup import (  # noqa: E402
    DEFAULT_IO_WEIGHT,
    GIB,
    CampaignCgroup,
    CampaignCgroupError,
    CampaignCgroupLimits,
)
from scripts.testing.campaign_control_channel import (  # noqa: E402
    ControlChannelError,
    PeerIdentity,
    authenticate_connection,
    create_server,
    derive_runner_auth_key,
    send_hello,
    sign_authenticated_payload,
    socket_permissions,
    verify_authenticated_payload,
)
from scripts.testing.campaign_state import (  # noqa: E402
    CampaignState,
    CampaignStateMachine,
)
from scripts.testing.campaign_watchdog import (  # noqa: E402
    DEFAULT_STALE_SECONDS,
    INCIDENT_EXIT_CODE,
    CgroupIdentity,
    ProcessIdentityError,
    WatchdogConfig,
    WatchdogPaths,
    atomic_write_json,
    build_watchdog_command,
    capture_process_identity,
    load_json,
    locked_path,
)


SCHEMA_VERSION = "hackme.watchdog-sigstop-e2e.v2"
ARTIFACT_INDEX_SCHEMA_VERSION = "hackme.watchdog-sigstop-artifacts.v1"
ORCHESTRATOR_FIXTURE_SCHEMA_VERSION = "hackme.watchdog-sigstop-fixture.v2"
CHECKPOINT_SCHEMA_VERSION = "hackme.watchdog-sigstop-checkpoint.v1"
CONTROL_SCHEMA_VERSION = "hackme.campaign-control.v1"
EXACT_STALE_SECONDS = 120.0
INCIDENT_WAIT_SECONDS = 150.0
READY_WAIT_SECONDS = 20.0
PROCESS_STOP_WAIT_SECONDS = 15.0
AUTHENTICATION_WAIT_SECONDS = 20.0
WATCHDOG_LIVENESS_MAX_AGE_SECONDS = 10.0

EXPECTED_LIMITS = {
    "memory.high": 5 * GIB,
    "memory.max": 6 * GIB,
    "memory.swap.max": 512 * 1024**2,
    "cpu.quota_percent": 300,
    "pids.max": 384,
    "io.weight": DEFAULT_IO_WEIGHT,
}

REQUIRED_ASSERTIONS = (
    "source_at_commit_clean",
    "exact_cgroup_limits",
    "supervisor_outside_scope",
    "orchestrator_in_scope",
    "load_in_scope",
    "watchdog_outside_scope",
    "authenticated_control_channel",
    "role_separated_auth_keys",
    "signed_runner_streams",
    "reciprocal_watchdog_liveness",
    "sigstop_delivered",
    "stale_timeout_120_observed",
    "admission_closed",
    "load_stop_requested",
    "continuous_time_stopped",
    "evidence_preserved",
    "managed_scope_terminated",
    "watchdog_survived_scope_kill",
    "cgroup_empty_after",
    "artifact_hashes_valid",
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class SigstopE2EError(RuntimeError):
    """The injection could not produce complete, trustworthy evidence."""


class SigstopE2EInfraError(SigstopE2EError):
    """The real host cannot supply a required process/cgroup capability."""


class SigstopE2ELivenessError(SigstopE2EError):
    """The external watchdog identity or signed liveness became untrustworthy."""


class AuthenticatedControlRuntime:
    """Private, pinned one-shot control server for the Level-0 E2E driver.

    The driver is the supervisor for this standalone harness.  It delivers the
    derived runner key only to the exact in-scope orchestrator PID and the
    master watchdog key only to the exact out-of-scope watchdog PID.  Neither
    key is placed in argv, environment variables, files, logs, or evidence.
    """

    def __init__(self, campaign_uuid: str):
        digest = hashlib.sha256(str(campaign_uuid).encode("utf-8")).hexdigest()[:20]
        self.campaign_uuid = str(campaign_uuid)
        self.directory = Path("/tmp") / f".hws-fi-{digest}"
        self.path = self.directory / "control.sock"
        self.server: socket.socket | None = None
        self.socket_fd: int | None = None
        self.directory_fd: int | None = None
        self.socket_evidence: dict[str, Any] = {}
        self.rejections: list[dict[str, str]] = []
        self.watchdog_auth_key = secrets.token_bytes(32)
        self.runner_auth_key = derive_runner_auth_key(self.watchdog_auth_key)
        if secrets.compare_digest(self.runner_auth_key, self.watchdog_auth_key):
            raise SigstopE2EError("runner and watchdog authentication keys are not separated")

    def open(self) -> dict[str, Any]:
        if self.server is not None or self.directory.exists() or self.directory.is_symlink():
            raise SigstopE2EError("authenticated control socket path is already in use")
        if not hasattr(os, "O_PATH"):
            raise SigstopE2EInfraError("authenticated control channel requires Linux O_PATH pinning")
        self.directory.mkdir(mode=0o700)
        os.chmod(self.directory, 0o700)
        metadata = self.directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or int(metadata.st_uid) != os.getuid()
            or int(metadata.st_gid) != os.getgid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise SigstopE2EError("authenticated control socket directory is not private and owned")
        try:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            self.directory_fd = os.open(self.directory, directory_flags)
            pinned_directory = os.fstat(self.directory_fd)
            if (
                int(pinned_directory.st_dev) != int(metadata.st_dev)
                or int(pinned_directory.st_ino) != int(metadata.st_ino)
            ):
                raise SigstopE2EError("authenticated control directory changed while pinning")
            self.server = create_server(self.path)
            socket_metadata = self.path.lstat()
            socket_flags = (
                os.O_PATH
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            self.socket_fd = os.open(self.path, socket_flags)
            pinned_socket = os.fstat(self.socket_fd)
            if (
                int(pinned_socket.st_dev) != int(socket_metadata.st_dev)
                or int(pinned_socket.st_ino) != int(socket_metadata.st_ino)
            ):
                raise SigstopE2EError("authenticated control socket changed while pinning")
            self.socket_evidence = {
                **socket_permissions(self.path),
                "directory_mode": "0o700",
                "directory_device": int(metadata.st_dev),
                "directory_inode": int(metadata.st_ino),
                "directory_path_pinned": True,
                "socket_path_pinned": True,
                "ok": True,
            }
            return dict(self.socket_evidence)
        except Exception:
            self.close()
            raise

    def authenticate(
        self,
        *,
        process: subprocess.Popen[Any],
        expected_identity: Any,
        role: str,
        session_secret: bytes,
        placement_check: Callable[[int, Any], Mapping[str, Any]],
        expected_inside: bool,
        timeout: float = AUTHENTICATION_WAIT_SECONDS,
    ) -> dict[str, Any]:
        server = self.server
        if server is None:
            raise SigstopE2EError("authenticated control server is not listening")
        expected_peer = PeerIdentity(int(expected_identity.pid), os.getuid(), os.getgid())
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SigstopE2EError(
                    f"{role} exited before authenticated handshake: returncode={process.returncode}"
                )
            remaining = max(0.01, deadline - time.monotonic())
            server.settimeout(min(0.1, remaining))
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                raise SigstopE2EError("authenticated control socket accept failed") from exc
            try:
                handshake = authenticate_connection(
                    connection,
                    expected_campaign=self.campaign_uuid,
                    expected_peer=expected_peer,
                    expected_role=role,
                    session_secret=session_secret,
                    timeout=min(2.0, remaining),
                )
            except ControlChannelError as exc:
                self.rejections.append({
                    "role": role,
                    "error_code": exc.__class__.__name__,
                    "reason": str(exc)[:240],
                })
                continue
            finally:
                connection.close()

            actual = capture_process_identity(expected_identity.pid)
            identity_mismatches = [
                field
                for field in ("pid", "start_ticks", "boot_id", "cgroup_path")
                if getattr(actual, field) != getattr(expected_identity, field)
            ]
            if identity_mismatches:
                raise SigstopE2EError(
                    f"{role} process identity changed after SO_PEERCRED handshake: "
                    + ", ".join(identity_mismatches)
                )
            placement = dict(placement_check(actual.pid, actual))
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
            if (
                placement_mismatches
                or placement.get("ok") is not True
                or placement.get("inside_campaign_scope") is not expected_inside
            ):
                raise SigstopE2EError(
                    f"{role} authenticated placement is not proven: "
                    + ", ".join(placement_mismatches or ["scope_membership"])
                )
            expected_hash = hashlib.sha256(session_secret).hexdigest()
            anti_replay = bool(
                handshake.get("one_time") is True
                and handshake.get("acknowledged") is True
                and handshake.get("challenge_bytes") == 32
                and handshake.get("client_nonce_bytes") == 32
                and handshake.get("session_secret_delivered") is True
                and handshake.get("session_secret_sha256") == expected_hash
            )
            if not anti_replay:
                raise SigstopE2EError(f"{role} challenge/nonce/session proof is incomplete")
            return {
                "role": role,
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
                "session_secret_sha256": expected_hash,
                "session_secret_persisted": False,
                "anti_replay_verified": True,
                "ok": True,
            }
        raise SigstopE2EError(f"{role} did not authenticate before timeout")

    def close(self) -> dict[str, Any]:
        errors: list[str] = []
        if self.server is not None:
            try:
                self.server.close()
            except Exception as exc:
                errors.append(f"server_close:{exc.__class__.__name__}")
            self.server = None
        pinned_socket = None
        if self.socket_fd is not None:
            try:
                pinned_socket = os.fstat(self.socket_fd)
            except Exception as exc:
                errors.append(f"socket_pin_stat:{exc.__class__.__name__}")
        try:
            metadata = self.path.lstat()
            if (
                pinned_socket is None
                or not stat.S_ISSOCK(metadata.st_mode)
                or int(metadata.st_dev) != int(pinned_socket.st_dev)
                or int(metadata.st_ino) != int(pinned_socket.st_ino)
            ):
                errors.append("socket_identity_changed")
            else:
                self.path.unlink()
        except FileNotFoundError:
            if pinned_socket is not None:
                errors.append("socket_missing")
        except Exception as exc:
            errors.append(f"socket_unlink:{exc.__class__.__name__}")
        finally:
            if self.socket_fd is not None:
                try:
                    os.close(self.socket_fd)
                except Exception as exc:
                    errors.append(f"socket_pin_close:{exc.__class__.__name__}")
                self.socket_fd = None

        pinned_directory = None
        if self.directory_fd is not None:
            try:
                pinned_directory = os.fstat(self.directory_fd)
            except Exception as exc:
                errors.append(f"directory_pin_stat:{exc.__class__.__name__}")
        try:
            metadata = self.directory.lstat()
            if (
                pinned_directory is None
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or int(metadata.st_dev) != int(pinned_directory.st_dev)
                or int(metadata.st_ino) != int(pinned_directory.st_ino)
            ):
                errors.append("socket_directory_identity_changed")
            elif not self.path.exists() and not self.path.is_symlink():
                self.directory.rmdir()
        except FileNotFoundError:
            if pinned_directory is not None:
                errors.append("socket_directory_missing")
        except Exception as exc:
            errors.append(f"socket_directory_remove:{exc.__class__.__name__}")
        finally:
            if self.directory_fd is not None:
                try:
                    os.close(self.directory_fd)
                except Exception as exc:
                    errors.append(f"directory_pin_close:{exc.__class__.__name__}")
                self.directory_fd = None
        return {
            "ok": not errors,
            "socket_removed": not self.path.exists() and not self.path.is_symlink(),
            "directory_removed": not self.directory.exists() and not self.directory.is_symlink(),
            "errors": errors,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_campaign_root(path: Path) -> Path:
    """Require an isolated path strictly below the real ``/tmp`` directory."""

    root = Path(path).expanduser().resolve(strict=False)
    tmp = Path("/tmp").resolve(strict=True)
    if root == tmp or tmp not in root.parents:
        raise SigstopE2EError(f"campaign root must be strictly below /tmp: {root}")
    return root


def _prepare_campaign_root(path: Path) -> Path:
    root = validate_campaign_root(path)
    if root.exists() and any(root.iterdir()):
        raise SigstopE2EError(f"campaign root must be absent or empty: {root}")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    for name in ("artifacts", "checkpoint", "logs"):
        (root / name).mkdir(mode=0o700, exist_ok=False)
    return root


def _git_source_proof(repo_root: Path, expected_commit: str) -> dict[str, Any]:
    expected = str(expected_commit or "").strip().lower()
    if not _COMMIT_RE.fullmatch(expected):
        raise SigstopE2EError("expected commit must be an exact lowercase 40-character SHA")
    repo = Path(repo_root).resolve(strict=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    actual = str(head.stdout or "").strip().lower()
    clean_rows = [row for row in str(status.stdout or "").splitlines() if row.strip()]
    proof = {
        "repo_root": str(repo),
        "expected_commit": expected,
        "actual_commit": actual,
        "head_returncode": int(head.returncode),
        "status_returncode": int(status.returncode),
        "worktree_clean": not clean_rows,
        "dirty_entry_count": len(clean_rows),
        "commit_matches": actual == expected,
    }
    if head.returncode != 0 or status.returncode != 0 or actual != expected or clean_rows:
        raise SigstopE2EError(f"source is not a clean exact-commit checkout: {proof}")
    return proof


def _process_payload(identity: Any, *, role: str, placement: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "role": role,
        "pid": int(identity.pid),
        "start_ticks": int(identity.start_ticks),
        "boot_id": str(identity.boot_id),
        "cgroup": str(identity.cgroup_path),
        "state_at_capture": str(identity.state),
        "placement": dict(placement),
        "terminated": False,
    }


def _control_payload(*, campaign_uuid: str, revision: int, state: str, admit: bool) -> dict[str, Any]:
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "campaign_uuid": campaign_uuid,
        "revision": int(revision),
        "state": state,
        "admit_new_jobs": bool(admit),
        "load_generator_should_run": bool(admit),
        "preserve_evidence_requested": not bool(admit),
        "updated_at": utc_now(),
    }


def _checkpoint_payload(*, campaign_uuid: str, revision: int, phase: str) -> dict[str, Any]:
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "campaign_uuid": campaign_uuid,
        "revision": int(revision),
        "phase": phase,
        "updated_at": utc_now(),
    }


def _write_signed_runner_artifacts(
    *,
    heartbeat_path: Path,
    checkpoint_path: Path,
    campaign_uuid: str,
    identity: Any,
    revision: int,
    runner_auth_key: bytes,
) -> dict[str, Any]:
    """Publish checkpoint before heartbeat so every admitted revision is durable."""

    sequence = int(revision)
    if sequence <= 0:
        raise SigstopE2EError("runner artifact revision must be positive")
    checkpoint_ns = time.monotonic_ns()
    checkpoint = sign_authenticated_payload(
        _checkpoint_payload(
            campaign_uuid=campaign_uuid,
            revision=sequence,
            phase="ACTIVE",
        ),
        session_secret=runner_auth_key,
        campaign_uuid=campaign_uuid,
        stream="runner_checkpoint",
        sequence=sequence,
        monotonic_ns=checkpoint_ns,
    )
    atomic_write_json(checkpoint_path, checkpoint)
    heartbeat_ns = time.monotonic_ns()
    heartbeat = sign_authenticated_payload(
        {
            "schema_version": ORCHESTRATOR_FIXTURE_SCHEMA_VERSION,
            "campaign_uuid": campaign_uuid,
            "orchestrator_pid": int(identity.pid),
            "orchestrator_start_ticks": int(identity.start_ticks),
            "orchestrator_monotonic_ns": heartbeat_ns,
            "checkpoint_revision": sequence,
            "updated_at": utc_now(),
        },
        session_secret=runner_auth_key,
        campaign_uuid=campaign_uuid,
        stream="runner_heartbeat",
        sequence=sequence,
        monotonic_ns=heartbeat_ns,
    )
    atomic_write_json(heartbeat_path, heartbeat)
    return {
        "revision": sequence,
        "checkpoint_authentication": dict(checkpoint["authentication"]),
        "heartbeat_authentication": dict(heartbeat["authentication"]),
        "ok": True,
    }


def _verify_signed_runner_artifacts(
    *,
    heartbeat_path: Path,
    checkpoint_path: Path,
    campaign_uuid: str,
    expected_identity: Any,
    runner_auth_key: bytes,
) -> dict[str, Any]:
    heartbeat = load_json(heartbeat_path)
    checkpoint = load_json(checkpoint_path)
    heartbeat_proof = verify_authenticated_payload(
        heartbeat,
        session_secret=runner_auth_key,
        expected_campaign_uuid=campaign_uuid,
        expected_stream="runner_heartbeat",
    )
    checkpoint_proof = verify_authenticated_payload(
        checkpoint,
        session_secret=runner_auth_key,
        expected_campaign_uuid=campaign_uuid,
        expected_stream="runner_checkpoint",
    )
    expected = {
        "orchestrator_pid": int(expected_identity.pid),
        "orchestrator_start_ticks": int(expected_identity.start_ticks),
    }
    mismatches = [name for name, value in expected.items() if heartbeat.get(name) != value]
    heartbeat_ns = int(heartbeat.get("orchestrator_monotonic_ns") or 0)
    if int(heartbeat_proof.get("monotonic_ns") or 0) != heartbeat_ns:
        mismatches.append("heartbeat_monotonic_binding")
    heartbeat_revision = int(heartbeat.get("checkpoint_revision") or 0)
    checkpoint_revision = int(checkpoint.get("revision") or 0)
    if heartbeat_revision <= 0 or checkpoint_revision < heartbeat_revision:
        mismatches.append("checkpoint_revision")
    if mismatches:
        raise SigstopE2EError(
            "signed runner heartbeat/checkpoint contract mismatch: "
            + ", ".join(sorted(set(mismatches)))
        )
    return {
        "heartbeat": heartbeat_proof,
        "checkpoint": checkpoint_proof,
        "orchestrator_pid": int(expected_identity.pid),
        "orchestrator_start_ticks": int(expected_identity.start_ticks),
        "checkpoint_revision": checkpoint_revision,
        "role_key": "runner_derived_key",
        "ok": True,
    }


def _verify_watchdog_liveness(
    *,
    path: Path,
    campaign_uuid: str,
    expected_identity: Any,
    watchdog_auth_key: bytes,
    previous: Mapping[str, Any] | None = None,
    require_live_process: bool = True,
    require_fresh: bool = True,
) -> dict[str, Any]:
    payload = load_json(path)
    earlier = previous or {}
    authentication = verify_authenticated_payload(
        payload,
        session_secret=watchdog_auth_key,
        expected_campaign_uuid=campaign_uuid,
        expected_stream="watchdog_liveness",
        previous_sequence=int(earlier.get("sequence") or 0),
        previous_payload_sha256=str(earlier.get("payload_sha256") or ""),
    )
    watchdog = payload.get("watchdog")
    if not isinstance(watchdog, Mapping):
        raise SigstopE2EError("signed watchdog liveness identity is missing")
    expected = {
        "pid": int(expected_identity.pid),
        "start_ticks": int(expected_identity.start_ticks),
        "boot_id": str(expected_identity.boot_id),
        "cgroup": str(expected_identity.cgroup_path),
    }
    mismatches = [name for name, value in expected.items() if watchdog.get(name) != value]
    liveness_ns = int(watchdog.get("monotonic_ns") or 0)
    if int(authentication.get("monotonic_ns") or 0) != liveness_ns:
        mismatches.append("monotonic_binding")
    now_ns = time.monotonic_ns()
    if liveness_ns <= 0 or liveness_ns > now_ns:
        age_seconds = float("inf")
        mismatches.append("monotonic_range")
    else:
        age_seconds = (now_ns - liveness_ns) / 1_000_000_000
        if require_fresh and age_seconds >= WATCHDOG_LIVENESS_MAX_AGE_SECONDS:
            mismatches.append("stale")
    if require_live_process:
        actual = capture_process_identity(expected_identity.pid)
        for field in ("pid", "start_ticks", "boot_id", "cgroup_path"):
            if getattr(actual, field) != getattr(expected_identity, field):
                mismatches.append(f"process_{field}")
    if mismatches:
        raise SigstopE2EError(
            "authenticated watchdog liveness mismatch: "
            + ", ".join(sorted(set(mismatches)))
        )
    return {
        **authentication,
        "watchdog": dict(watchdog),
        "age_seconds": round(age_seconds, 6),
        "deadline_seconds": WATCHDOG_LIVENESS_MAX_AGE_SECONDS,
        "process_identity_reverified": bool(require_live_process),
        "freshness_required": bool(require_fresh),
        "role_key": "watchdog_master_key",
        "ok": True,
    }


def _build_authenticated_watchdog_config(
    *,
    campaign_uuid: str,
    paths: WatchdogPaths,
    orchestrator_identity: Any,
    scope_identity: Mapping[str, Any],
    supervisor_identity: Any,
    auth_socket: Path,
) -> WatchdogConfig:
    """Build the production-only contract used by the real SIGSTOP injection."""

    return WatchdogConfig(
        campaign_uuid=campaign_uuid,
        paths=paths,
        orchestrator_pid=int(orchestrator_identity.pid),
        orchestrator_start_ticks=int(orchestrator_identity.start_ticks),
        orchestrator_boot_id=str(orchestrator_identity.boot_id),
        orchestrator_cgroup=str(orchestrator_identity.cgroup_path),
        campaign_cgroup=CgroupIdentity(
            path=str(scope_identity["path"]),
            device=int(scope_identity["device"]),
            inode=int(scope_identity["inode"]),
        ),
        stale_after_seconds=DEFAULT_STALE_SECONDS,
        poll_seconds=1.0,
        kill_verify_seconds=10.0,
        production=True,
        auth_socket=Path(auth_socket),
        supervisor_pid=int(supervisor_identity.pid),
        supervisor_start_ticks=int(supervisor_identity.start_ticks),
        supervisor_boot_id=str(supervisor_identity.boot_id),
        supervisor_cgroup=str(supervisor_identity.cgroup_path),
    )


def _wait_for_json(
    path: Path,
    predicate: Callable[[Mapping[str, Any]], bool],
    *,
    timeout: float,
    label: str,
    process: subprocess.Popen[Any] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + float(timeout)
    last_error = "file not present"
    while time.monotonic() < deadline:
        try:
            payload = load_json(path)
            if predicate(payload):
                return payload
            last_error = f"predicate rejected payload keys={sorted(payload)}"
        except Exception as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
        if process is not None and process.poll() is not None:
            raise SigstopE2EError(f"{label} process exited early: returncode={process.returncode}; {last_error}")
        time.sleep(0.1)
    raise SigstopE2EError(f"timed out waiting for {label}: {last_error}")


def _wait_for_watchdog_incident(
    *,
    status_path: Path,
    liveness_path: Path,
    process: subprocess.Popen[Any],
    campaign_uuid: str,
    expected_identity: Any,
    watchdog_auth_key: bytes,
    initial_liveness: Mapping[str, Any],
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Wait for the injected incident while continuously proving watchdog life."""

    deadline = time.monotonic() + float(timeout)
    previous = dict(initial_liveness)
    first_sequence = int(previous.get("sequence") or 0)
    samples_verified = 0
    maximum_age = float(previous.get("age_seconds") or 0.0)
    last_status_error = "status file not present"
    while time.monotonic() < deadline:
        try:
            status = load_json(status_path)
            if status.get("incident_id"):
                if status.get("reason") != "HEARTBEAT_STALE":
                    raise SigstopE2EError(
                        "watchdog reported an unexpected incident while awaiting SIGSTOP proof: "
                        + str(status.get("reason") or "reason_missing")
                    )
                monitor = {
                    "samples_verified": samples_verified,
                    "first_sequence": first_sequence,
                    "last_sequence": int(previous.get("sequence") or 0),
                    "maximum_age_seconds": round(maximum_age, 6),
                    "deadline_seconds": WATCHDOG_LIVENESS_MAX_AGE_SECONDS,
                    "fail_closed_on_invalid": True,
                    "ok": True,
                }
                return status, previous, monitor
            last_status_error = f"status not terminal: keys={sorted(status)}"
        except FileNotFoundError:
            last_status_error = "status file not present"
        except SigstopE2EError:
            raise
        except Exception as exc:
            last_status_error = f"{exc.__class__.__name__}: {exc}"

        if process.poll() is not None:
            try:
                terminal = load_json(status_path)
            except Exception:
                terminal = {}
            if terminal.get("incident_id") and terminal.get("reason") == "HEARTBEAT_STALE":
                monitor = {
                    "samples_verified": samples_verified,
                    "first_sequence": first_sequence,
                    "last_sequence": int(previous.get("sequence") or 0),
                    "maximum_age_seconds": round(maximum_age, 6),
                    "deadline_seconds": WATCHDOG_LIVENESS_MAX_AGE_SECONDS,
                    "fail_closed_on_invalid": True,
                    "ok": True,
                }
                return terminal, previous, monitor
            raise SigstopE2ELivenessError(
                "watchdog exited before durable stale-heartbeat evidence: "
                f"returncode={process.returncode}; {last_status_error}"
            )
        try:
            current = _verify_watchdog_liveness(
                path=liveness_path,
                campaign_uuid=campaign_uuid,
                expected_identity=expected_identity,
                watchdog_auth_key=watchdog_auth_key,
                previous=previous,
            )
        except Exception as exc:
            raise SigstopE2ELivenessError(
                "watchdog reciprocal liveness failed closed while load was active: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        previous = current
        samples_verified += 1
        maximum_age = max(maximum_age, float(current.get("age_seconds") or 0.0))
        time.sleep(0.25)
    raise SigstopE2EError(
        "timed out waiting for 120-second stale-heartbeat incident while watchdog remained live: "
        + last_status_error
    )


def _wait_for_process_identity(
    process: subprocess.Popen[Any],
    *,
    expected_cgroup: str,
    timeout: float,
) -> Any:
    """Wait until the cgroup entry helper has exec'd without trusting a new PID."""

    deadline = time.monotonic() + float(timeout)
    last_cgroup = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SigstopE2EError(
                "managed process exited before exact cgroup identity capture: "
                f"returncode={process.returncode}"
            )
        try:
            identity = capture_process_identity(process.pid)
            last_cgroup = str(identity.cgroup_path)
            if last_cgroup == str(expected_cgroup):
                return identity
        except ProcessIdentityError:
            pass
        time.sleep(0.02)
    raise SigstopE2EError(
        "managed process did not enter the exact campaign cgroup before timeout: "
        f"expected={expected_cgroup},actual={last_cgroup or 'unavailable'}"
    )


@contextmanager
def _timed_path_lock(path: Path, *, timeout: float) -> Iterator[None]:
    deadline = time.monotonic() + float(timeout)
    lock_context: Any | None = None
    while time.monotonic() < deadline:
        candidate = locked_path(path, nonblocking=True)
        try:
            candidate.__enter__()
            lock_context = candidate
            break
        except BlockingIOError:
            time.sleep(0.02)
    if lock_context is None:
        raise SigstopE2EError("timed out acquiring the state lock before SIGSTOP")
    try:
        yield
    finally:
        lock_context.__exit__(None, None, None)


def _wait_for_stopped_process(pid: int, start_ticks: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        try:
            identity = capture_process_identity(pid)
        except ProcessIdentityError:
            return True
        if int(identity.start_ticks) != int(start_ticks):
            return True
        time.sleep(0.1)
    return False


def _wait_for_process_state(pid: int, start_ticks: int, states: set[str], *, timeout: float) -> Any:
    deadline = time.monotonic() + float(timeout)
    last_state = "missing"
    while time.monotonic() < deadline:
        identity = capture_process_identity(pid)
        if identity.start_ticks != int(start_ticks):
            raise SigstopE2EError("orchestrator PID identity changed after SIGSTOP")
        last_state = identity.state
        if identity.state in states:
            return identity
        time.sleep(0.02)
    raise SigstopE2EError(f"SIGSTOP was not observed in procfs; final state={last_state}")


def _read_scope_pids(cgroup_root: Path, scope_path: str) -> list[int]:
    scope = Path(cgroup_root) / str(scope_path).lstrip("/")
    if not scope.exists():
        return []
    pids: set[int] = set()
    for path in scope.rglob("cgroup.procs"):
        try:
            pids.update(int(row) for row in path.read_text(encoding="ascii").splitlines() if row.strip())
        except FileNotFoundError:
            continue
    return sorted(pids)


def _artifact_record(path: Path, *, campaign_root: Path, artifact_id: str) -> dict[str, Any]:
    resolved = Path(path).resolve(strict=True)
    root = Path(campaign_root).resolve(strict=True)
    if root not in resolved.parents:
        raise SigstopE2EError(f"artifact escaped campaign root: {resolved}")
    size = resolved.stat().st_size
    if size <= 0:
        raise SigstopE2EError(f"artifact is empty: {resolved}")
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    suffix = resolved.suffix.lower()
    media_type = "application/json" if suffix == ".json" else "text/plain"
    schema_version: str | None = None
    if suffix == ".json":
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SigstopE2EError(f"JSON artifact must contain an object: {resolved}")
        value = payload.get("schema_version") or payload.get("sample_schema_version")
        schema_version = str(value) if value else "json-object/unspecified"
    return {
        "artifact_id": artifact_id,
        "path": str(resolved),
        "relative_path": str(resolved.relative_to(root)),
        "media_type": media_type,
        "schema_version": schema_version or "text/plain",
        "sha256": digest.hexdigest(),
        "size": int(size),
        "validated": True,
    }


def _assertion(ok: bool, evidence: Mapping[str, Any]) -> dict[str, Any]:
    safe_evidence = dict(evidence) or {"reason": "evidence_missing"}
    return {"status": "PASS" if bool(ok) else "FAIL", "evidence": safe_evidence}


def _limit_assertion(cgroup: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    limits = cgroup.get("limits") if isinstance(cgroup.get("limits"), dict) else {}
    checks = limits.get("checks") if isinstance(limits.get("checks"), dict) else {}
    actual = {
        "memory.high": (checks.get("memory.high") or {}).get("actual"),
        "memory.max": (checks.get("memory.max") or {}).get("actual"),
        "memory.swap.max": (checks.get("memory.swap.max") or {}).get("actual"),
        "pids.max": (checks.get("pids.max") or {}).get("actual"),
        "cpu.quota_percent": (checks.get("cpu.max") or {}).get("actual_percent"),
        "io.weight": (checks.get("io.weight") or {}).get("actual"),
    }
    ok = bool(limits.get("ok")) and limits.get("hard_limit_state") == "verified" and actual == EXPECTED_LIMITS
    return ok, {"expected": EXPECTED_LIMITS, "actual": actual, "hard_limit_state": limits.get("hard_limit_state")}


def assess_e2e_evidence(
    evidence: Mapping[str, Any],
    *,
    real_external_execution: bool = False,
) -> dict[str, Any]:
    """Build strict assertions; simulations can never become gate candidates."""

    source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    cgroup = evidence.get("cgroup") if isinstance(evidence.get("cgroup"), dict) else {}
    processes = evidence.get("processes") if isinstance(evidence.get("processes"), dict) else {}
    authentication = evidence.get("authentication") if isinstance(evidence.get("authentication"), dict) else {}
    watchdog = evidence.get("watchdog") if isinstance(evidence.get("watchdog"), dict) else {}
    timings = evidence.get("timings") if isinstance(evidence.get("timings"), dict) else {}
    state = evidence.get("state") if isinstance(evidence.get("state"), dict) else {}
    at_sigstop = state.get("at_sigstop") if isinstance(state.get("at_sigstop"), dict) else {}
    final_state = state.get("final") if isinstance(state.get("final"), dict) else {}
    final_control = state.get("final_control") if isinstance(state.get("final_control"), dict) else {}
    before_clock = at_sigstop.get("clock") if isinstance(at_sigstop.get("clock"), dict) else {}
    final_clock = final_state.get("clock") if isinstance(final_state.get("clock"), dict) else {}
    final_watchdog = watchdog.get("final") if isinstance(watchdog.get("final"), dict) else {}
    initial_watchdog = watchdog.get("initial") if isinstance(watchdog.get("initial"), dict) else {}
    cgroup_stop = final_watchdog.get("cgroup_stop") if isinstance(final_watchdog.get("cgroup_stop"), dict) else {}
    artifacts = evidence.get("artifacts") if isinstance(evidence.get("artifacts"), list) else []

    assertions: dict[str, dict[str, Any]] = {}
    source_ok = bool(source.get("worktree_clean")) and bool(source.get("commit_matches")) and bool(source.get("actual_commit"))
    assertions["source_at_commit_clean"] = _assertion(source_ok, source or {"source": "missing"})

    limits_ok, limits_evidence = _limit_assertion(cgroup)
    assertions["exact_cgroup_limits"] = _assertion(limits_ok, limits_evidence)

    supervisor = processes.get("supervisor") if isinstance(processes.get("supervisor"), dict) else {}
    supervisor_placement = supervisor.get("placement") if isinstance(supervisor.get("placement"), dict) else {}
    supervisor_outside = (
        supervisor_placement.get("ok") is True
        and supervisor_placement.get("inside_campaign_scope") is False
        and int(supervisor.get("pid") or 0) > 1
        and int(supervisor.get("start_ticks") or 0) > 0
        and bool(supervisor.get("boot_id"))
        and bool(supervisor.get("cgroup"))
    )
    assertions["supervisor_outside_scope"] = _assertion(
        supervisor_outside,
        supervisor or {"role": "sigstop_e2e_supervisor", "reason": "missing"},
    )

    for assertion_id, role in (("orchestrator_in_scope", "orchestrator"), ("load_in_scope", "load")):
        row = processes.get(role) if isinstance(processes.get(role), dict) else {}
        placement = row.get("placement") if isinstance(row.get("placement"), dict) else {}
        ok = bool(placement.get("ok")) and placement.get("inside_campaign_scope") is True
        assertions[assertion_id] = _assertion(ok, row or {"role": role, "reason": "missing"})

    watchdog_process = processes.get("watchdog") if isinstance(processes.get("watchdog"), dict) else {}
    watchdog_placement = watchdog_process.get("placement") if isinstance(watchdog_process.get("placement"), dict) else {}
    watchdog_outside = (
        bool(watchdog_placement.get("ok"))
        and watchdog_placement.get("inside_campaign_scope") is False
        and initial_watchdog.get("watchdog_outside_campaign_cgroup") is True
        and initial_watchdog.get("external_process") is True
    )
    assertions["watchdog_outside_scope"] = _assertion(
        watchdog_outside,
        {"process": watchdog_process, "watchdog_startup": initial_watchdog},
    )

    socket_proof = authentication.get("socket") if isinstance(authentication.get("socket"), dict) else {}
    auth_cleanup = authentication.get("cleanup") if isinstance(authentication.get("cleanup"), dict) else {}
    runner_auth = authentication.get("runner") if isinstance(authentication.get("runner"), dict) else {}
    watchdog_auth = authentication.get("watchdog") if isinstance(authentication.get("watchdog"), dict) else {}
    runner_client = (
        authentication.get("runner_client")
        if isinstance(authentication.get("runner_client"), dict)
        else {}
    )
    watchdog_client = (
        authentication.get("watchdog_client")
        if isinstance(authentication.get("watchdog_client"), dict)
        else {}
    )
    runner_auth_placement = (
        runner_auth.get("placement") if isinstance(runner_auth.get("placement"), dict) else {}
    )
    watchdog_auth_placement = (
        watchdog_auth.get("placement") if isinstance(watchdog_auth.get("placement"), dict) else {}
    )
    runner_server_process = (
        runner_client.get("server_process")
        if isinstance(runner_client.get("server_process"), dict)
        else {}
    )
    watchdog_server_process = (
        watchdog_client.get("server_process")
        if isinstance(watchdog_client.get("server_process"), dict)
        else {}
    )
    supervisor_contract = (
        authentication.get("supervisor_identity")
        if isinstance(authentication.get("supervisor_identity"), dict)
        else {}
    )
    control_channel_ok = (
        socket_proof.get("ok") is True
        and socket_proof.get("transport") == "unix_sock_seqpacket"
        and socket_proof.get("mode") == "0o600"
        and socket_proof.get("directory_mode") == "0o700"
        and socket_proof.get("directory_path_pinned") is True
        and socket_proof.get("socket_path_pinned") is True
        and runner_auth.get("ok") is True
        and runner_auth.get("anti_replay_verified") is True
        and (runner_auth.get("handshake") or {}).get("role") == "runner"
        and runner_auth_placement.get("inside_campaign_scope") is True
        and watchdog_auth.get("ok") is True
        and watchdog_auth.get("anti_replay_verified") is True
        and (watchdog_auth.get("handshake") or {}).get("role") == "watchdog"
        and watchdog_auth_placement.get("inside_campaign_scope") is False
        and runner_client.get("server_identity_verified") is True
        and runner_client.get("session_secret_received") is True
        and runner_client.get("role") == "runner"
        and watchdog_client.get("server_identity_verified") is True
        and watchdog_client.get("session_secret_received") is True
        and watchdog_client.get("role") == "watchdog"
        and initial_watchdog.get("verified") is True
        and (initial_watchdog.get("initial_health") or {}).get("ok") is True
        and supervisor_contract.get("pid") == supervisor.get("pid")
        and supervisor_contract.get("start_ticks") == supervisor.get("start_ticks")
        and supervisor_contract.get("boot_id") == supervisor.get("boot_id")
        and supervisor_contract.get("cgroup_path") == supervisor.get("cgroup")
        and runner_server_process.get("pid") == supervisor.get("pid")
        and runner_server_process.get("start_ticks") == supervisor.get("start_ticks")
        and runner_server_process.get("boot_id") == supervisor.get("boot_id")
        and runner_server_process.get("cgroup_path") == supervisor.get("cgroup")
        and watchdog_server_process.get("pid") == supervisor.get("pid")
        and watchdog_server_process.get("start_ticks") == supervisor.get("start_ticks")
        and watchdog_server_process.get("boot_id") == supervisor.get("boot_id")
        and watchdog_server_process.get("cgroup_path") == supervisor.get("cgroup")
        and auth_cleanup.get("ok") is True
        and auth_cleanup.get("socket_removed") is True
        and auth_cleanup.get("directory_removed") is True
        and authentication.get("session_keys_persisted") is False
    )
    assertions["authenticated_control_channel"] = _assertion(
        control_channel_ok,
        {
            "socket": socket_proof,
            "supervisor_identity": supervisor_contract,
            "runner": runner_auth,
            "watchdog": watchdog_auth,
            "runner_client": runner_client,
            "watchdog_client": watchdog_client,
            "cleanup": auth_cleanup,
            "session_keys_persisted": authentication.get("session_keys_persisted"),
        },
    )

    runner_key_hash = str(authentication.get("runner_key_sha256") or "")
    watchdog_key_hash = str(authentication.get("watchdog_key_sha256") or "")
    separated_keys_ok = (
        authentication.get("role_separated_keys") is True
        and bool(re.fullmatch(r"[0-9a-f]{64}", runner_key_hash))
        and bool(re.fullmatch(r"[0-9a-f]{64}", watchdog_key_hash))
        and runner_key_hash != watchdog_key_hash
        and runner_auth.get("session_secret_sha256") == runner_key_hash
        and runner_client.get("session_secret_sha256") == runner_key_hash
        and watchdog_auth.get("session_secret_sha256") == watchdog_key_hash
        and watchdog_client.get("session_secret_sha256") == watchdog_key_hash
        and runner_auth.get("session_secret_persisted") is False
        and watchdog_auth.get("session_secret_persisted") is False
    )
    assertions["role_separated_auth_keys"] = _assertion(
        separated_keys_ok,
        {
            "runner_key_sha256": runner_key_hash,
            "watchdog_key_sha256": watchdog_key_hash,
            "role_separated_keys": authentication.get("role_separated_keys"),
            "session_keys_persisted": authentication.get("session_keys_persisted"),
        },
    )

    runner_streams = (
        authentication.get("signed_runner_streams_at_sigstop")
        if isinstance(authentication.get("signed_runner_streams_at_sigstop"), dict)
        else authentication.get("signed_runner_streams")
        if isinstance(authentication.get("signed_runner_streams"), dict)
        else {}
    )
    heartbeat_auth = runner_streams.get("heartbeat") if isinstance(runner_streams.get("heartbeat"), dict) else {}
    checkpoint_auth = runner_streams.get("checkpoint") if isinstance(runner_streams.get("checkpoint"), dict) else {}
    signed_streams_ok = (
        runner_streams.get("ok") is True
        and runner_streams.get("role_key") == "runner_derived_key"
        and heartbeat_auth.get("mac_verified") is True
        and heartbeat_auth.get("replay_checked") is True
        and heartbeat_auth.get("stream") == "runner_heartbeat"
        and checkpoint_auth.get("mac_verified") is True
        and checkpoint_auth.get("replay_checked") is True
        and checkpoint_auth.get("stream") == "runner_checkpoint"
        and int(runner_streams.get("orchestrator_pid") or 0) == int((processes.get("orchestrator") or {}).get("pid") or 0)
        and int(runner_streams.get("orchestrator_start_ticks") or 0)
        == int((processes.get("orchestrator") or {}).get("start_ticks") or 0)
    )
    assertions["signed_runner_streams"] = _assertion(
        signed_streams_ok,
        runner_streams or {"reason": "signed runner streams missing"},
    )

    initial_liveness = (
        watchdog.get("liveness_initial")
        if isinstance(watchdog.get("liveness_initial"), dict)
        else {}
    )
    final_liveness = (
        watchdog.get("liveness_final")
        if isinstance(watchdog.get("liveness_final"), dict)
        else {}
    )
    liveness_monitor = (
        watchdog.get("liveness_monitor")
        if isinstance(watchdog.get("liveness_monitor"), dict)
        else {}
    )
    liveness_identity = initial_liveness.get("watchdog") if isinstance(initial_liveness.get("watchdog"), dict) else {}
    reciprocal_liveness_ok = (
        initial_liveness.get("ok") is True
        and initial_liveness.get("mac_verified") is True
        and initial_liveness.get("replay_checked") is True
        and initial_liveness.get("stream") == "watchdog_liveness"
        and initial_liveness.get("role_key") == "watchdog_master_key"
        and initial_liveness.get("process_identity_reverified") is True
        and float(
            initial_liveness.get("age_seconds")
            if initial_liveness.get("age_seconds") is not None
            else WATCHDOG_LIVENESS_MAX_AGE_SECONDS
        )
        < WATCHDOG_LIVENESS_MAX_AGE_SECONDS
        and final_liveness.get("ok") is True
        and final_liveness.get("mac_verified") is True
        and final_liveness.get("replay_checked") is True
        and final_liveness.get("stream") == "watchdog_liveness"
        and final_liveness.get("role_key") == "watchdog_master_key"
        and int(final_liveness.get("sequence") or 0) > int(initial_liveness.get("sequence") or 0) > 0
        and liveness_monitor.get("ok") is True
        and liveness_monitor.get("fail_closed_on_invalid") is True
        and int(liveness_monitor.get("samples_verified") or 0) >= 2
        and int(liveness_monitor.get("first_sequence") or 0) == int(initial_liveness.get("sequence") or 0)
        and int(liveness_monitor.get("last_sequence") or 0) == int(final_liveness.get("sequence") or 0)
        and float(
            liveness_monitor.get("maximum_age_seconds")
            if liveness_monitor.get("maximum_age_seconds") is not None
            else WATCHDOG_LIVENESS_MAX_AGE_SECONDS
        )
        < WATCHDOG_LIVENESS_MAX_AGE_SECONDS
        and liveness_identity.get("pid") == watchdog_process.get("pid")
        and liveness_identity.get("start_ticks") == watchdog_process.get("start_ticks")
        and liveness_identity.get("cgroup") == watchdog_process.get("cgroup")
        and watchdog_placement.get("inside_campaign_scope") is False
    )
    assertions["reciprocal_watchdog_liveness"] = _assertion(
        reciprocal_liveness_ok,
        {"initial": initial_liveness, "final": final_liveness, "monitor": liveness_monitor},
    )

    signal_state = str((processes.get("orchestrator") or {}).get("state_after_sigstop") or "")
    sigstop_ok = (
        bool(timings.get("sigstop_monotonic_ns"))
        and signal_state in {"T", "t"}
        and timings.get("state_lock_guarded_sigstop") is True
    )
    assertions["sigstop_delivered"] = _assertion(
        sigstop_ok,
        {
            "signal": "SIGSTOP",
            "observed_process_state": signal_state,
            "sent_at_ns": timings.get("sigstop_monotonic_ns"),
            "state_lock_guarded_sigstop": timings.get("state_lock_guarded_sigstop"),
        },
    )

    observed_stale = float(timings.get("stale_observed_seconds") or 0.0)
    heartbeat_to_detection = float(timings.get("heartbeat_to_detection_seconds") or 0.0)
    stale_ok = (
        float(evidence.get("stale_timeout_seconds") or 0.0) == EXACT_STALE_SECONDS
        and observed_stale >= EXACT_STALE_SECONDS
        and heartbeat_to_detection >= EXACT_STALE_SECONDS
        and str(final_watchdog.get("reason") or "") == "HEARTBEAT_STALE"
    )
    assertions["stale_timeout_120_observed"] = _assertion(
        stale_ok,
        {
            "configured_seconds": evidence.get("stale_timeout_seconds"),
            "watchdog_observed_seconds": observed_stale,
            "heartbeat_to_detection_seconds": heartbeat_to_detection,
            "reason": final_watchdog.get("reason"),
        },
    )

    final_state_control = final_state.get("control") if isinstance(final_state.get("control"), dict) else {}
    admission_ok = final_state_control.get("admit_new_jobs") is False and final_control.get("admit_new_jobs") is False
    assertions["admission_closed"] = _assertion(
        admission_ok,
        {"durable_control": final_state_control, "mirror_control": final_control},
    )
    load_stop_ok = (
        final_state_control.get("load_generator_should_run") is False
        and final_control.get("load_generator_should_run") is False
    )
    assertions["load_stop_requested"] = _assertion(
        load_stop_ok,
        {"durable_control": final_state_control, "mirror_control": final_control},
    )

    before_continuous = float(before_clock.get("continuous_active_seconds") or 0.0)
    final_continuous = float(final_clock.get("continuous_active_seconds") or 0.0)
    clock_ok = (
        before_continuous > 0
        and abs(before_continuous - final_continuous) <= 0.000001
        and final_clock.get("formal_segment_valid") is False
        and final_clock.get("clock_pause_reason") == "HEARTBEAT_STALE"
        and bool(final_clock.get("active_finished_at"))
    )
    assertions["continuous_time_stopped"] = _assertion(
        clock_ok,
        {
            "at_sigstop_seconds": before_continuous,
            "final_seconds": final_continuous,
            "formal_segment_valid": final_clock.get("formal_segment_valid"),
            "clock_pause_reason": final_clock.get("clock_pause_reason"),
            "active_finished_at": final_clock.get("active_finished_at"),
        },
    )

    incident = evidence.get("incident") if isinstance(evidence.get("incident"), dict) else {}
    evidence_ok = (
        bool(final_watchdog.get("evidence_path"))
        and incident.get("schema_version") == "hackme.campaign-watchdog.v1"
        and incident.get("reason") == "HEARTBEAT_STALE"
        and incident.get("credential_material_collected") is False
    )
    assertions["evidence_preserved"] = _assertion(
        evidence_ok,
        {
            "evidence_path": final_watchdog.get("evidence_path"),
            "incident_id": incident.get("incident_id"),
            "reason": incident.get("reason"),
            "credential_material_collected": incident.get("credential_material_collected"),
        },
    )

    orchestrator = processes.get("orchestrator") if isinstance(processes.get("orchestrator"), dict) else {}
    load = processes.get("load") if isinstance(processes.get("load"), dict) else {}
    scope_terminated = (
        cgroup_stop.get("freeze_written") is True
        and cgroup_stop.get("kill_written") is True
        and cgroup_stop.get("population_cleared") is True
        and orchestrator.get("terminated") is True
        and load.get("terminated") is True
    )
    assertions["managed_scope_terminated"] = _assertion(
        scope_terminated,
        {"cgroup_stop": cgroup_stop, "orchestrator_terminated": orchestrator.get("terminated"), "load_terminated": load.get("terminated")},
    )

    watchdog_survived = (
        watchdog_process.get("terminated_after_result") is True
        and int(watchdog_process.get("returncode") if watchdog_process.get("returncode") is not None else -999) == INCIDENT_EXIT_CODE
        and bool(final_watchdog.get("finished_at"))
        and cgroup_stop.get("population_cleared") is True
    )
    assertions["watchdog_survived_scope_kill"] = _assertion(
        watchdog_survived,
        {
            "watchdog_pid": watchdog_process.get("pid"),
            "watchdog_start_ticks": watchdog_process.get("start_ticks"),
            "returncode": watchdog_process.get("returncode"),
            "result_finished_at": final_watchdog.get("finished_at"),
            "population_cleared": cgroup_stop.get("population_cleared"),
        },
    )

    cleanup = cgroup.get("cleanup") if isinstance(cgroup.get("cleanup"), dict) else {}
    empty_ok = cleanup.get("cgroup_empty") is True and not (cgroup.get("after_pids") or [])
    assertions["cgroup_empty_after"] = _assertion(
        empty_ok,
        {"after_pids": cgroup.get("after_pids"), "cleanup": cleanup},
    )

    artifact_ok = bool(artifacts) and all(
        isinstance(row, dict)
        and row.get("validated") is True
        and int(row.get("size") or 0) > 0
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256") or "")))
        and bool(row.get("schema_version"))
        for row in artifacts
    )
    assertions["artifact_hashes_valid"] = _assertion(
        artifact_ok,
        {"artifact_count": len(artifacts), "artifact_ids": [row.get("artifact_id") for row in artifacts if isinstance(row, dict)]},
    )

    missing = [name for name in REQUIRED_ASSERTIONS if assertions.get(name, {}).get("status") != "PASS"]
    actual_wait = bool(real_external_execution) and bool(evidence.get("actual_external_execution"))
    candidate = actual_wait and not missing
    status = "PASS_CANDIDATE" if candidate else ("PARTIAL_PASS" if not missing and not actual_wait else "FAIL_HARNESS")
    return {
        "assertions": assertions,
        "failed_assertions": missing,
        "status": status,
        "classification": None if status in {"PASS_CANDIDATE", "PARTIAL_PASS"} else "FAIL_HARNESS",
        "machine_verified": candidate,
        "formal_gate_candidate": candidate,
        "verification_scope": "end_to_end" if actual_wait else "component_only",
    }


def _orchestrator_fixture(args: argparse.Namespace) -> int:
    root = validate_campaign_root(Path(args.campaign_root))
    campaign_uuid = str(args.campaign_uuid or "")
    if not campaign_uuid:
        raise SigstopE2EError("internal orchestrator requires campaign UUID")
    expected_scope = str(os.environ.get("HACKME_CAMPAIGN_CGROUP_PATH") or "")
    identity = capture_process_identity(os.getpid())
    if not expected_scope or identity.cgroup_path != expected_scope:
        raise SigstopE2EError("orchestrator is not in the exact inherited campaign cgroup")
    if (
        not args.auth_socket
        or int(args.supervisor_pid or 0) <= 1
        or int(args.supervisor_start_ticks or 0) <= 0
        or not args.supervisor_boot_id
        or not args.supervisor_cgroup
    ):
        raise SigstopE2EError("orchestrator authenticated supervisor contract is incomplete")
    authentication = send_hello(
        Path(args.auth_socket),
        campaign_uuid=campaign_uuid,
        role="runner",
        require_session_secret=True,
        expected_server_peer=PeerIdentity(int(args.supervisor_pid), os.getuid(), os.getgid()),
        expected_server_process={
            "pid": int(args.supervisor_pid),
            "start_ticks": int(args.supervisor_start_ticks),
            "boot_id": str(args.supervisor_boot_id),
            "cgroup_path": str(args.supervisor_cgroup),
        },
    )
    if not isinstance(authentication, tuple):
        raise SigstopE2EError("orchestrator control handshake did not deliver a runner key")
    authentication_evidence, runner_auth_key = authentication

    state_path = root / "checkpoint" / "campaign.state.json"
    control_path = root / "checkpoint" / "campaign.control.json"
    checkpoint_path = root / "checkpoint" / "campaign.checkpoint.json"
    heartbeat_path = root / "checkpoint" / "runner.heartbeat.json"
    ready_path = root / "checkpoint" / "orchestrator.ready.json"
    load_ready_path = root / "checkpoint" / "load.ready.json"
    machine = CampaignStateMachine(state_path)
    machine.initialize(
        campaign_uuid=campaign_uuid,
        required_active_seconds=86_400,
        orchestrator_pid=os.getpid(),
        orchestrator_start_ticks=identity.start_ticks,
    )
    machine.transition(CampaignState.PREFLIGHT, reason="level0_sigstop_fixture_preflight")
    machine.mark_frozen(
        source={"verified": True, "fixture_only": True},
        containment={"verified": True, "cgroup_path": expected_scope},
    )
    machine.start_active({
        "source_frozen": True,
        "primary_ready": True,
        "recovery_ready": True,
        "watchdog_alive": True,
        "monitor_alive": True,
        "load_generator_alive": True,
        "no_hard_stop": True,
    })
    first_state = machine.snapshot()
    atomic_write_json(
        control_path,
        _control_payload(
            campaign_uuid=campaign_uuid,
            revision=int(first_state.get("revision") or 1),
            state="ACTIVE",
            admit=True,
        ),
    )

    load_command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--campaign-root", str(root),
        "--expected-commit", str(args.expected_commit),
        "--campaign-uuid", campaign_uuid,
        "--internal-role", "load",
    ]
    load_process = subprocess.Popen(
        load_command,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        load_ready = _wait_for_json(
            load_ready_path,
            lambda payload: int(payload.get("pid") or 0) == load_process.pid and payload.get("ready") is True,
            timeout=10.0,
            label="load fixture",
            process=load_process,
        )
        revision = 1
        machine.heartbeat(
            orchestrator_pid=os.getpid(),
            orchestrator_start_ticks=identity.start_ticks,
            checkpoint_revision=revision,
        )
        signed_artifacts = _write_signed_runner_artifacts(
            heartbeat_path=heartbeat_path,
            checkpoint_path=checkpoint_path,
            campaign_uuid=campaign_uuid,
            identity=identity,
            revision=revision,
            runner_auth_key=runner_auth_key,
        )
        atomic_write_json(ready_path, {
            "schema_version": ORCHESTRATOR_FIXTURE_SCHEMA_VERSION,
            "campaign_uuid": campaign_uuid,
            "ready": True,
            "pid": os.getpid(),
            "start_ticks": identity.start_ticks,
            "cgroup": identity.cgroup_path,
            "load": load_ready,
            "authenticated_control_channel": authentication_evidence,
            "signed_runner_artifacts": signed_artifacts,
            "ready_at": utc_now(),
        })
        conditions = {
            "source_frozen": True,
            "primary_ready": True,
            "recovery_ready": True,
            "watchdog_alive": True,
            "monitor_alive": True,
            "load_generator_alive": True,
            "no_hard_stop": True,
        }
        while True:
            if load_process.poll() is not None:
                machine.hard_stop(
                    reason_code="LOAD_FIXTURE_EXITED",
                    classification="FAIL_HARNESS",
                    evidence={"returncode": load_process.returncode},
                )
                atomic_write_json(control_path, _control_payload(
                    campaign_uuid=campaign_uuid,
                    revision=int(machine.snapshot().get("revision") or revision),
                    state="STOPPING_LOAD",
                    admit=False,
                ))
                return 2
            time.sleep(0.25)
            machine.tick_active(conditions)
            revision += 1
            machine.heartbeat(
                orchestrator_pid=os.getpid(),
                orchestrator_start_ticks=identity.start_ticks,
                checkpoint_revision=revision,
            )
            _write_signed_runner_artifacts(
                heartbeat_path=heartbeat_path,
                checkpoint_path=checkpoint_path,
                campaign_uuid=campaign_uuid,
                identity=identity,
                revision=revision,
                runner_auth_key=runner_auth_key,
            )
    finally:
        if load_process.poll() is None:
            load_process.terminate()
            try:
                load_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                load_process.kill()


def _load_fixture(args: argparse.Namespace) -> int:
    root = validate_campaign_root(Path(args.campaign_root))
    campaign_uuid = str(args.campaign_uuid or "")
    identity = capture_process_identity(os.getpid())
    expected_scope = str(os.environ.get("HACKME_CAMPAIGN_CGROUP_PATH") or "")
    if not campaign_uuid or not expected_scope or identity.cgroup_path != expected_scope:
        raise SigstopE2EError("load fixture is not in the exact inherited campaign cgroup")
    ready = root / "checkpoint" / "load.ready.json"
    pulse = root / "checkpoint" / "load.pulse.json"
    control = root / "checkpoint" / "campaign.control.json"
    atomic_write_json(ready, {
        "schema_version": ORCHESTRATOR_FIXTURE_SCHEMA_VERSION,
        "campaign_uuid": campaign_uuid,
        "role": "load_generator",
        "ready": True,
        "pid": os.getpid(),
        "start_ticks": identity.start_ticks,
        "cgroup": identity.cgroup_path,
        "ready_at": utc_now(),
    })
    sequence = 0
    while True:
        payload = load_json(control)
        if payload.get("admit_new_jobs") is not True or payload.get("load_generator_should_run") is not True:
            atomic_write_json(pulse, {
                "schema_version": ORCHESTRATOR_FIXTURE_SCHEMA_VERSION,
                "campaign_uuid": campaign_uuid,
                "sequence": sequence,
                "load_stop_observed": True,
                "observed_at": utc_now(),
            })
            return 0
        sequence += 1
        atomic_write_json(pulse, {
            "schema_version": ORCHESTRATOR_FIXTURE_SCHEMA_VERSION,
            "campaign_uuid": campaign_uuid,
            "sequence": sequence,
            "load_stop_observed": False,
            "updated_at": utc_now(),
        })
        time.sleep(0.25)


def _write_fail_closed_control(
    root: Path,
    campaign_uuid: str,
    reason: str,
    *,
    revision: int = 0,
) -> None:
    atomic_write_json(root / "checkpoint" / "campaign.control.json", _control_payload(
        campaign_uuid=campaign_uuid,
        revision=int(revision),
        state="FAILED",
        admit=False,
    ) | {"reason": reason})


def _force_fail_closed(root: Path, campaign_uuid: str, reason: str) -> None:
    state_path = root / "checkpoint" / "campaign.state.json"
    try:
        machine = CampaignStateMachine(state_path)

        def mutate(payload: dict[str, Any]) -> None:
            clock = payload.setdefault("clock", {})
            clock.update({
                "formal_segment_valid": False,
                "clock_pause_reason": reason,
                "active_finished_at": clock.get("active_finished_at") or utc_now(),
            })
            payload["state"] = "FAILED"
            payload["classification"] = "FAIL_HARNESS"
            payload["reason"] = reason
            payload["control"] = {
                "admit_new_jobs": False,
                "load_generator_should_run": False,
                "preserve_evidence_requested": True,
            }

        state = machine.store.update(mutate)
        revision = int(state.get("revision") or 0)
    except Exception:
        revision = 0
    try:
        _write_fail_closed_control(
            root,
            campaign_uuid,
            reason,
            revision=revision,
        )
    except Exception:
        pass


def _collect_artifacts(root: Path, explicit: Mapping[str, Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[Path] = set()
    candidates = dict(explicit)
    for path in sorted((root / "artifacts" / "cgroup" / "entries").glob("*.json")):
        candidates[f"cgroup_entry_{path.stem}"] = path
    for artifact_id, path in sorted(candidates.items()):
        candidate = Path(path)
        if not candidate.is_file():
            continue
        # Logs are optional supporting evidence and are commonly empty when
        # every helper succeeds.  An empty file is never indexed as a valid
        # artifact; mandatory JSON evidence is enforced by the assertions.
        if candidate.stat().st_size <= 0:
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        rows.append(_artifact_record(resolved, campaign_root=root, artifact_id=artifact_id))
    return rows


def run_real_sigstop_e2e(
    *,
    campaign_root: Path,
    repo_root: Path,
    expected_commit: str,
) -> dict[str, Any]:
    """Run the production 120-second injection.  This requires sandbox-out."""

    root = _prepare_campaign_root(campaign_root)
    started_ns = time.monotonic_ns()
    started_at = utc_now()
    campaign_uuid = f"watchdog-sigstop-{uuid.uuid4()}"
    report_path = root / "artifacts" / "watchdog_sigstop_e2e.json"
    cgroup: CampaignCgroup | None = None
    auth_runtime: AuthenticatedControlRuntime | None = None
    orchestrator: subprocess.Popen[Any] | None = None
    watchdog_process: subprocess.Popen[Any] | None = None
    orchestrator_log_handle: Any | None = None
    watchdog_log_handle: Any | None = None
    errors: list[str] = []
    failure_classification = "FAIL_HARNESS"
    failure_reason = "SIGSTOP_E2E_INCOMPLETE"
    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "campaign_uuid": campaign_uuid,
        "commit": str(expected_commit).strip().lower(),
        "stale_timeout_seconds": EXACT_STALE_SECONDS,
        "actual_external_execution": True,
        "started_at": started_at,
        "paths": {
            "campaign_root": str(root),
            "report": str(report_path),
        },
        "source": {},
        "cgroup": {},
        "processes": {},
        "authentication": {},
        "watchdog": {},
        "timings": {},
        "state": {},
        "incident": {},
        "artifacts": [],
        "collector_errors": errors,
    }
    state_path = root / "checkpoint" / "campaign.state.json"
    control_path = root / "checkpoint" / "campaign.control.json"
    checkpoint_path = root / "checkpoint" / "campaign.checkpoint.json"
    heartbeat_path = root / "checkpoint" / "runner.heartbeat.json"
    orchestrator_ready_path = root / "checkpoint" / "orchestrator.ready.json"
    watchdog_status_path = root / "checkpoint" / "watchdog.status.json"
    watchdog_liveness_path = root / "checkpoint" / "watchdog.liveness.json"
    orchestrator_log = root / "logs" / "orchestrator.log"
    watchdog_log = root / "logs" / "watchdog.log"
    incident_path: Path | None = None
    try:
        evidence["source"] = _git_source_proof(repo_root, expected_commit)
        limits = CampaignCgroupLimits()
        cgroup = CampaignCgroup(
            campaign_id=campaign_uuid,
            evidence_root=root / "artifacts" / "cgroup",
            limits=limits,
        )
        creation = cgroup.create_scope()
        limits_proof = cgroup.verify_limits()
        scope_identity = cgroup.capture_scope_identity()
        evidence["cgroup"] = {
            "path": cgroup.scope_path,
            "scope_unit": cgroup.unit_name,
            "identity": scope_identity,
            "limits": limits_proof,
            "creation": creation,
            "before_pids": _read_scope_pids(cgroup.cgroup_root, cgroup.scope_path),
            "after_pids": [],
            "cleanup": {},
        }

        supervisor_identity = capture_process_identity(os.getpid())
        supervisor_placement = cgroup.assert_pid_outside(
            supervisor_identity.pid,
            role="sigstop_e2e_supervisor",
            expected_identity=supervisor_identity,
        )
        evidence["processes"]["supervisor"] = _process_payload(
            supervisor_identity,
            role="sigstop_e2e_supervisor",
            placement=supervisor_placement,
        )
        auth_runtime = AuthenticatedControlRuntime(campaign_uuid)
        evidence["authentication"] = {
            "socket": auth_runtime.open(),
            "supervisor_identity": {
                "pid": supervisor_identity.pid,
                "start_ticks": supervisor_identity.start_ticks,
                "boot_id": supervisor_identity.boot_id,
                "cgroup_path": supervisor_identity.cgroup_path,
            },
            "runner_key_sha256": hashlib.sha256(auth_runtime.runner_auth_key).hexdigest(),
            "watchdog_key_sha256": hashlib.sha256(auth_runtime.watchdog_auth_key).hexdigest(),
            "role_separated_keys": not secrets.compare_digest(
                auth_runtime.runner_auth_key,
                auth_runtime.watchdog_auth_key,
            ),
            "session_keys_persisted": False,
            "runner": {},
            "watchdog": {},
            "cleanup": {},
        }

        orchestrator_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--campaign-root", str(root),
            "--expected-commit", str(expected_commit),
            "--campaign-uuid", campaign_uuid,
            "--internal-role", "orchestrator",
            "--auth-socket", str(auth_runtime.path),
            "--supervisor-pid", str(supervisor_identity.pid),
            "--supervisor-start-ticks", str(supervisor_identity.start_ticks),
            "--supervisor-boot-id", supervisor_identity.boot_id,
            "--supervisor-cgroup", supervisor_identity.cgroup_path,
        ]
        orchestrator_log_handle = orchestrator_log.open("ab", buffering=0)
        orchestrator_environment = os.environ.copy()
        orchestrator_environment["HACKME_CAMPAIGN_CGROUP_PATH"] = cgroup.scope_path
        orchestrator = subprocess.Popen(
            cgroup.wrap_command(orchestrator_command, role="scenario"),
            cwd=str(Path(repo_root).resolve()),
            env=orchestrator_environment,
            stdin=subprocess.DEVNULL,
            stdout=orchestrator_log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        orchestrator_identity = _wait_for_process_identity(
            orchestrator,
            expected_cgroup=cgroup.scope_path,
            timeout=READY_WAIT_SECONDS,
        )
        runner_authentication = auth_runtime.authenticate(
            process=orchestrator,
            expected_identity=orchestrator_identity,
            role="runner",
            session_secret=auth_runtime.runner_auth_key,
            placement_check=lambda pid, identity: cgroup.assert_pid_membership(
                pid,
                role="sigstop_e2e_runner_control_peer",
                expected_identity=identity,
            ),
            expected_inside=True,
        )
        evidence["authentication"]["runner"] = runner_authentication
        ready = _wait_for_json(
            orchestrator_ready_path,
            lambda payload: payload.get("ready") is True and int(payload.get("pid") or 0) == orchestrator.pid,
            timeout=READY_WAIT_SECONDS,
            label="in-scope orchestrator",
            process=orchestrator,
        )
        ready_authentication = (
            ready.get("authenticated_control_channel")
            if isinstance(ready.get("authenticated_control_channel"), dict)
            else {}
        )
        if (
            ready_authentication.get("server_identity_verified") is not True
            or ready_authentication.get("session_secret_received") is not True
            or ready_authentication.get("session_secret_sha256")
            != evidence["authentication"]["runner_key_sha256"]
        ):
            raise SigstopE2EError("runner reciprocal supervisor authentication proof is incomplete")
        evidence["authentication"]["runner_client"] = ready_authentication
        runner_stream_proof = _verify_signed_runner_artifacts(
            heartbeat_path=heartbeat_path,
            checkpoint_path=checkpoint_path,
            campaign_uuid=campaign_uuid,
            expected_identity=orchestrator_identity,
            runner_auth_key=auth_runtime.runner_auth_key,
        )
        evidence["authentication"]["signed_runner_streams"] = runner_stream_proof
        load_ready = ready.get("load") if isinstance(ready.get("load"), dict) else {}
        load_pid = int(load_ready.get("pid") or 0)
        load_identity = capture_process_identity(load_pid)
        orchestrator_placement = cgroup.register_pid("scenario", orchestrator.pid)
        load_placement = cgroup.register_pid("load_generator", load_pid)
        evidence["processes"]["orchestrator"] = _process_payload(
            orchestrator_identity,
            role="orchestrator",
            placement=orchestrator_placement,
        )
        evidence["processes"]["load"] = _process_payload(
            load_identity,
            role="load_generator",
            placement=load_placement,
        )
        evidence["cgroup"]["before_pids"] = _read_scope_pids(cgroup.cgroup_root, cgroup.scope_path)

        watchdog_paths = WatchdogPaths(
            campaign_root=root,
            state=state_path,
            control=control_path,
            heartbeat=heartbeat_path,
            checkpoint=checkpoint_path,
            ready=watchdog_status_path,
            evidence=root / "artifacts" / "watchdog",
            process_lock=root / "checkpoint" / "watchdog.process.lock",
            liveness=watchdog_liveness_path,
        )
        watchdog_config = _build_authenticated_watchdog_config(
            campaign_uuid=campaign_uuid,
            paths=watchdog_paths,
            orchestrator_identity=orchestrator_identity,
            scope_identity=scope_identity,
            supervisor_identity=supervisor_identity,
            auth_socket=auth_runtime.path,
        )
        watchdog_log_handle = watchdog_log.open("ab", buffering=0)
        watchdog_process = subprocess.Popen(
            build_watchdog_command(watchdog_config),
            cwd=str(Path(repo_root).resolve()),
            stdin=subprocess.DEVNULL,
            stdout=watchdog_log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        watchdog_identity = capture_process_identity(watchdog_process.pid)
        watchdog_authentication = auth_runtime.authenticate(
            process=watchdog_process,
            expected_identity=watchdog_identity,
            role="watchdog",
            session_secret=auth_runtime.watchdog_auth_key,
            placement_check=lambda pid, identity: cgroup.assert_pid_outside(
                pid,
                role="external_watchdog_control_peer",
                expected_identity=identity,
            ),
            expected_inside=False,
        )
        evidence["authentication"]["watchdog"] = watchdog_authentication
        watchdog_initial = _wait_for_json(
            watchdog_status_path,
            lambda payload: payload.get("verified") is True and payload.get("initial_health", {}).get("ok") is True,
            timeout=READY_WAIT_SECONDS,
            label="external watchdog startup",
            process=watchdog_process,
        )
        watchdog_placement = cgroup.assert_watchdog_outside(watchdog_process.pid)
        evidence["processes"]["watchdog"] = _process_payload(
            watchdog_identity,
            role="external_watchdog",
            placement=watchdog_placement,
        )
        evidence["watchdog"]["initial"] = watchdog_initial
        watchdog_client_authentication = (
            watchdog_initial.get("authenticated_control_channel")
            if isinstance(watchdog_initial.get("authenticated_control_channel"), dict)
            else {}
        )
        if (
            watchdog_client_authentication.get("server_identity_verified") is not True
            or watchdog_client_authentication.get("session_secret_received") is not True
            or watchdog_client_authentication.get("role") != "watchdog"
            or watchdog_client_authentication.get("session_secret_sha256")
            != evidence["authentication"]["watchdog_key_sha256"]
        ):
            raise SigstopE2EError("watchdog reciprocal supervisor authentication proof is incomplete")
        evidence["authentication"]["watchdog_client"] = watchdog_client_authentication
        watchdog_liveness = _verify_watchdog_liveness(
            path=watchdog_liveness_path,
            campaign_uuid=campaign_uuid,
            expected_identity=watchdog_identity,
            watchdog_auth_key=auth_runtime.watchdog_auth_key,
        )
        evidence["watchdog"]["liveness_initial"] = watchdog_liveness
        evidence["authentication"]["rejected_connections"] = list(auth_runtime.rejections[-20:])
        auth_cleanup = auth_runtime.close()
        evidence["authentication"]["cleanup"] = auth_cleanup
        if auth_cleanup.get("ok") is not True:
            raise SigstopE2EError(
                "authenticated control socket cleanup failed: "
                + ", ".join(str(row) for row in auth_cleanup.get("errors") or [])
            )
        if supervisor_identity.cgroup_path == cgroup.scope_path:
            raise SigstopE2EError("E2E driver unexpectedly entered the managed scope")

        state_lock_path = state_path.with_suffix(state_path.suffix + ".lock")
        with _timed_path_lock(state_lock_path, timeout=5.0):
            # Never SIGSTOP the runner while it owns the state flock.  Holding
            # the same lock here forces any concurrent update to finish first
            # and prevents the watchdog from deadlocking on a stranded lock.
            pre_signal = load_json(heartbeat_path)
            pre_signal_heartbeat = pre_signal
            if int(pre_signal_heartbeat.get("orchestrator_monotonic_ns") or 0) <= 0:
                raise SigstopE2EError("orchestrator heartbeat lacks monotonic timestamp")
            sigstop_ns = time.monotonic_ns()
            os.kill(orchestrator.pid, signal.SIGSTOP)
            stopped_identity = _wait_for_process_state(
                orchestrator.pid,
                orchestrator_identity.start_ticks,
                {"T", "t"},
                timeout=5.0,
            )
        state_at_sigstop = load_json(state_path)
        stopped_runner_proof = _verify_signed_runner_artifacts(
            heartbeat_path=heartbeat_path,
            checkpoint_path=checkpoint_path,
            campaign_uuid=campaign_uuid,
            expected_identity=orchestrator_identity,
            runner_auth_key=auth_runtime.runner_auth_key,
        )
        evidence["authentication"]["signed_runner_streams_at_sigstop"] = stopped_runner_proof
        stopped_heartbeat = load_json(heartbeat_path)
        heartbeat_ns = int(stopped_heartbeat.get("orchestrator_monotonic_ns") or 0)
        sigstop_observed_ns = time.monotonic_ns()
        if heartbeat_ns <= 0 or heartbeat_ns > sigstop_observed_ns:
            raise SigstopE2EError("stopped orchestrator heartbeat timestamp is invalid")
        stable_check = load_json(state_path)
        if float((state_at_sigstop.get("clock") or {}).get("continuous_active_seconds") or 0.0) != float(
            (stable_check.get("clock") or {}).get("continuous_active_seconds") or 0.0
        ):
            raise SigstopE2EError("continuous clock changed after procfs confirmed SIGSTOP")
        evidence["processes"]["orchestrator"]["state_after_sigstop"] = stopped_identity.state
        evidence["timings"].update({
            "heartbeat_last_monotonic_ns": heartbeat_ns,
            "sigstop_monotonic_ns": sigstop_ns,
            "sigstop_observed_monotonic_ns": sigstop_observed_ns,
            "state_lock_guarded_sigstop": True,
        })
        evidence["state"]["at_sigstop"] = state_at_sigstop

        watchdog_final, final_liveness, liveness_monitor = _wait_for_watchdog_incident(
            status_path=watchdog_status_path,
            liveness_path=watchdog_liveness_path,
            process=watchdog_process,
            campaign_uuid=campaign_uuid,
            expected_identity=watchdog_identity,
            watchdog_auth_key=auth_runtime.watchdog_auth_key,
            initial_liveness=watchdog_liveness,
            timeout=INCIDENT_WAIT_SECONDS,
        )
        evidence["watchdog"]["liveness_final"] = final_liveness
        evidence["watchdog"]["liveness_monitor"] = liveness_monitor
        watchdog_returncode = watchdog_process.wait(timeout=PROCESS_STOP_WAIT_SECONDS)
        evidence["processes"]["watchdog"].update({
            "terminated_after_result": True,
            "returncode": int(watchdog_returncode),
        })
        evidence["watchdog"]["final"] = watchdog_final
        incident_path = Path(str(watchdog_final.get("evidence_path") or ""))
        incident = load_json(incident_path)
        evidence["incident"] = incident
        details = incident.get("details") if isinstance(incident.get("details"), dict) else {}
        detected_ns = int((load_json(state_path).get("hard_stop") or {}).get("detected_monotonic_ns") or 0)
        observed_stale = float(details.get("heartbeat_age_seconds") or 0.0)
        evidence["timings"].update({
            "admission_closed_monotonic_ns": detected_ns,
            "timer_stopped_monotonic_ns": detected_ns,
            "scope_empty_monotonic_ns": time.monotonic_ns(),
            "stale_observed_seconds": observed_stale,
            "heartbeat_to_detection_seconds": round((detected_ns - heartbeat_ns) / 1_000_000_000, 6) if detected_ns >= heartbeat_ns else -1,
            "sigstop_to_detection_seconds": round((detected_ns - sigstop_ns) / 1_000_000_000, 6) if detected_ns >= sigstop_ns else -1,
        })
        evidence["state"]["final"] = load_json(state_path)
        evidence["state"]["final_control"] = load_json(control_path)

        evidence["processes"]["orchestrator"]["terminated"] = _wait_for_stopped_process(
            orchestrator_identity.pid,
            orchestrator_identity.start_ticks,
            timeout=PROCESS_STOP_WAIT_SECONDS,
        )
        evidence["processes"]["load"]["terminated"] = _wait_for_stopped_process(
            load_identity.pid,
            load_identity.start_ticks,
            timeout=PROCESS_STOP_WAIT_SECONDS,
        )
        try:
            orchestrator_returncode = orchestrator.wait(timeout=2)
        except subprocess.TimeoutExpired:
            orchestrator_returncode = None
        evidence["processes"]["orchestrator"]["returncode"] = orchestrator_returncode
        cleanup = cgroup.stop_scope()
        evidence["cgroup"]["cleanup"] = cleanup
        evidence["cgroup"]["after_pids"] = _read_scope_pids(cgroup.cgroup_root, cgroup.scope_path)
        evidence["timings"]["scope_empty_monotonic_ns"] = time.monotonic_ns()
    except CampaignCgroupError as exc:
        failure_classification = "FAIL_INFRA"
        errors.append(f"{exc.__class__.__name__}: {exc}")
    except (PermissionError, subprocess.SubprocessError, OSError) as exc:
        failure_classification = "FAIL_INFRA"
        errors.append(f"{exc.__class__.__name__}: {exc}")
    except SigstopE2ELivenessError as exc:
        failure_reason = "WATCHDOG_LIVENESS_INVALID"
        errors.append(f"{exc.__class__.__name__}: {exc}")
    except Exception as exc:
        errors.append(f"{exc.__class__.__name__}: {exc}")
    finally:
        if errors:
            try:
                # Close the mirror gate without taking the state lock.  The
                # orchestrator may have been SIGSTOP'd while holding it.
                _write_fail_closed_control(
                    root,
                    campaign_uuid,
                    failure_reason,
                )
            except Exception as exc:
                errors.append(f"close_admission_mirror: {exc.__class__.__name__}: {exc}")
        if orchestrator is not None and orchestrator.poll() is None:
            try:
                os.kill(orchestrator.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        if cgroup is not None and cgroup.scope_path and not cgroup.stopped:
            try:
                cleanup = cgroup.stop_scope()
                evidence.setdefault("cgroup", {})["cleanup"] = cleanup
                evidence["cgroup"]["after_pids"] = _read_scope_pids(cgroup.cgroup_root, cgroup.scope_path)
            except Exception as exc:
                errors.append(f"cleanup_cgroup: {exc.__class__.__name__}: {exc}")
        if errors:
            # Cgroup teardown releases any lock held by a stopped/dead worker;
            # only then mutate the durable state so failure cleanup cannot hang.
            _force_fail_closed(root, campaign_uuid, failure_reason)
        if watchdog_process is not None and watchdog_process.poll() is None:
            try:
                watchdog_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                watchdog_process.terminate()
                try:
                    watchdog_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    watchdog_process.kill()
                    watchdog_process.wait(timeout=3)
        if orchestrator is not None and orchestrator.poll() is None:
            orchestrator.kill()
            orchestrator.wait(timeout=3)
        if auth_runtime is not None and (
            auth_runtime.server is not None
            or auth_runtime.socket_fd is not None
            or auth_runtime.directory_fd is not None
            or auth_runtime.path.exists()
            or auth_runtime.path.is_symlink()
            or auth_runtime.directory.exists()
            or auth_runtime.directory.is_symlink()
        ):
            authentication_cleanup = auth_runtime.close()
            evidence.setdefault("authentication", {})["cleanup"] = authentication_cleanup
            if authentication_cleanup.get("ok") is not True:
                errors.append(
                    "authenticated_control_cleanup: "
                    + ",".join(str(row) for row in authentication_cleanup.get("errors") or [])
                )
        if orchestrator_log_handle is not None:
            orchestrator_log_handle.close()
        if watchdog_log_handle is not None:
            watchdog_log_handle.close()

    try:
        explicit_artifacts = {
            "campaign_state": state_path,
            "campaign_control": control_path,
            "campaign_checkpoint": checkpoint_path,
            "runner_heartbeat": heartbeat_path,
            "orchestrator_ready": orchestrator_ready_path,
            "load_ready": root / "checkpoint" / "load.ready.json",
            "load_pulse": root / "checkpoint" / "load.pulse.json",
            "watchdog_status": watchdog_status_path,
            "watchdog_liveness": watchdog_liveness_path,
            "watchdog_incident": incident_path or Path("/nonexistent"),
            "cgroup_scope": root / "artifacts" / "cgroup" / "cgroup_scope.json",
            "orchestrator_log": orchestrator_log,
            "watchdog_log": watchdog_log,
        }
        evidence["artifacts"] = _collect_artifacts(root, explicit_artifacts)
    except Exception as exc:
        errors.append(f"artifact_validation: {exc.__class__.__name__}: {exc}")

    assessment = assess_e2e_evidence(evidence, real_external_execution=True)
    if errors:
        assessment.update({
            "status": failure_classification,
            "classification": failure_classification,
            "machine_verified": False,
            "formal_gate_candidate": False,
        })
    finished_ns = time.monotonic_ns()
    evidence.update(assessment)
    evidence.update({
        "finished_at": utc_now(),
        "duration_seconds": round((finished_ns - started_ns) / 1_000_000_000, 6),
        "failure_reason": failure_reason if errors else None,
        "collector_errors": errors,
    })
    atomic_write_json(report_path, evidence)
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", required=True, help="New isolated run directory strictly below /tmp")
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--campaign-uuid", help=argparse.SUPPRESS)
    parser.add_argument("--internal-role", choices=("orchestrator", "load"), help=argparse.SUPPRESS)
    parser.add_argument("--auth-socket", help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-start-ticks", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-boot-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-cgroup", default="", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_campaign_root(Path(args.campaign_root))
        if args.internal_role == "orchestrator":
            return _orchestrator_fixture(args)
        if args.internal_role == "load":
            return _load_fixture(args)
        result = run_real_sigstop_e2e(
            campaign_root=Path(args.campaign_root),
            repo_root=Path(args.repo_root),
            expected_commit=str(args.expected_commit),
        )
        print(json.dumps({
            "schema_version": result.get("schema_version"),
            "status": result.get("status"),
            "formal_gate_candidate": result.get("formal_gate_candidate"),
            "report": result.get("paths", {}).get("report"),
            "failed_assertions": result.get("failed_assertions"),
            "collector_errors": result.get("collector_errors"),
        }, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("formal_gate_candidate") is True else 1
    except Exception as exc:
        print(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "status": "FAIL_HARNESS",
            "formal_gate_candidate": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
