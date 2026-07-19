#!/usr/bin/env python3
"""Fail-closed cgroup v2 isolation for the formal operational campaign.

The campaign supervisor and its external watchdog intentionally remain outside
the managed scope.  Every workload command is wrapped with ``wrap_command``;
the wrapper moves itself into the delegated scope, proves the move through both
procfs and cgroupfs, records machine-readable evidence, and only then execs the
real command.  Descendants inherit the same cgroup placement.

This module deliberately does not fall back to an unconstrained subprocess.  A
missing user systemd manager, unavailable controller, unreadable cgroup file,
limit mismatch, or unverifiable PID placement is a hard error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


GIB = 1024**3
MIB = 1024**2
CGROUP_SCHEMA_VERSION = "hackme.campaign-cgroup/v2"
DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")
DEFAULT_PROC_ROOT = Path("/proc")
EXEC_FAILURE = 125
SYSTEMD_IO_WEIGHT_MIN = 1
SYSTEMD_IO_WEIGHT_MAX = 10_000
# systemd defaults to 100.  Half of that lowers campaign I/O priority without
# approaching the starvation-prone minimum weight.
DEFAULT_IO_WEIGHT = 10
HOST_TRANSITION_SCHEMA_VERSION = "hackme.campaign-comfyui-host-transition.v1"
COMFYUI_SANDBOX_MODULE_NAME = "campaign_comfyui_sandbox.py"
MAX_HOST_TRANSITION_JSON_BYTES = 64 * 1024
_BOOT_ID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")
_INVOCATION_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
MANDATORY_EVENT_COUNTERS: Mapping[str, tuple[str, ...]] = {
    "memory.events": ("max", "oom", "oom_kill"),
    "pids.events": ("max",),
}
OPTIONAL_EVENT_COUNTERS: Mapping[str, tuple[str, ...]] = {
    "memory.swap.events": ("max", "fail"),
}

MANDATORY_MANAGED_ROLES = frozenset({
    "primary",
    "recovery",
    "security_sentinel",
    "load_generator",
    "browser",
    "ffmpeg",
    "bt",
    "comfyui",
    "scenario",
})
LATE_BOUND_MANAGED_ENVIRONMENT_KEYS = frozenset({
    "HACKME_CAMPAIGN_COMFYUI_API_URL",
    "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT",
    "HACKME_CAMPAIGN_COMFYUI_BACKEND_PID",
})

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCOPE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


class CampaignCgroupError(RuntimeError):
    """Base error for cgroup setup or verification failures."""


class CgroupUnavailableError(CampaignCgroupError):
    """Raised when a real, delegated cgroup v2 scope cannot be established."""


class CgroupVerificationError(CampaignCgroupError):
    """Raised when a limit or process placement cannot be proven."""


def _native_identity_record(
    path: Path,
    *,
    reported_path: Path,
    directory: bool,
    label: str,
) -> dict[str, Any]:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except Exception as exc:
        raise CgroupVerificationError(f"cannot inspect {label}: {exc}") from exc
    if not candidate.is_absolute() or candidate != resolved:
        raise CgroupVerificationError(f"{label} is not a canonical path: {candidate}")
    if stat.S_ISLNK(metadata.st_mode):
        raise CgroupVerificationError(f"{label} cannot be a symlink: {candidate}")
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        kind = "directory" if directory else "regular control file"
        raise CgroupVerificationError(f"{label} is not a {kind}: {candidate}")
    return {
        "path": str(reported_path),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "mode": int(metadata.st_mode),
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
    }


def _count_native_cgroup_descendants(root: Path) -> int:
    pending = [Path(root)]
    count = 0
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except Exception as exc:
            raise CgroupVerificationError(
                f"cannot enumerate cgroup descendants below {current}: {exc}"
            ) from exc
        for entry in entries:
            if entry.is_symlink():
                raise CgroupVerificationError(
                    f"cgroup leaf contains a symlinked entry: {entry.path}"
                )
            if not entry.is_dir(follow_symlinks=False):
                continue
            count += 1
            if count > 1024:
                raise CgroupVerificationError(
                    "cgroup descendant enumeration exceeded its safety bound"
                )
            pending.append(Path(entry.path))
    return count


def _sandbox_write_root_records(values: Sequence[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for value in values:
        candidate = Path(value)
        record = _native_identity_record(
            candidate,
            reported_path=candidate,
            directory=True,
            label="sandbox write root",
        )
        if candidate in seen:
            raise CgroupVerificationError(
                f"sandbox write roots contain a duplicate: {candidate}"
            )
        seen.add(candidate)
        records.append(record)
    if not records:
        raise CgroupVerificationError("sandbox requires at least one write root")
    return records


def _host_transition_leaf_identity(
    *,
    scope_fs: Path,
    scope_path: str,
    pid: int,
) -> dict[str, Any]:
    native_root = Path(f"/sys/fs/cgroup{scope_path}")
    procs_path = scope_fs / "cgroup.procs"
    events_path = scope_fs / "cgroup.events"
    try:
        members = {
            int(row.strip())
            for row in procs_path.read_text(encoding="ascii").splitlines()
            if row.strip()
        }
        cgroup_type = (scope_fs / "cgroup.type").read_text(
            encoding="ascii"
        ).strip()
        subtree_control = (scope_fs / "cgroup.subtree_control").read_text(
            encoding="ascii"
        ).split()
    except Exception as exc:
        raise CgroupVerificationError(
            f"cannot inspect ComfyUI cgroup leaf controls: {exc}"
        ) from exc
    if pid not in members:
        raise CgroupVerificationError(
            "ComfyUI host transition leaf does not contain the launcher"
        )
    if cgroup_type not in {"domain", "domain threaded", "threaded"}:
        raise CgroupVerificationError(
            f"ComfyUI host transition cgroup type is invalid: {cgroup_type!r}"
        )
    if subtree_control:
        raise CgroupVerificationError(
            "ComfyUI host transition leaf has enabled subtree controllers"
        )
    descendants = _count_native_cgroup_descendants(scope_fs)
    if descendants:
        raise CgroupVerificationError(
            "ComfyUI host transition leaf has descendant cgroups"
        )
    return {
        "root": _native_identity_record(
            scope_fs,
            reported_path=native_root,
            directory=True,
            label="ComfyUI cgroup leaf",
        ),
        "cgroup_procs": _native_identity_record(
            procs_path,
            reported_path=native_root / "cgroup.procs",
            directory=False,
            label="ComfyUI cgroup.procs",
        ),
        "cgroup_events": _native_identity_record(
            events_path,
            reported_path=native_root / "cgroup.events",
            directory=False,
            label="ComfyUI cgroup.events",
        ),
        "cgroup_type": cgroup_type,
        "subtree_control": [],
        "subtree_controllers_enabled": False,
        "descendant_cgroups": 0,
        "workload_delegation_capability": "pending_sandbox",
        "current_pid_present": True,
        "ok": True,
    }


def _build_host_transition_payload(
    *,
    nonce: str,
    pid: int,
    scope_path: str,
    scope_fs: Path,
    placement: Mapping[str, Any],
    allowed_write_roots: Sequence[Path],
) -> dict[str, Any]:
    if (
        placement.get("pid") != pid
        or placement.get("actual_cgroup") != scope_path
        or placement.get("campaign_cgroup") != scope_path
        or placement.get("ok") is not True
    ):
        raise CgroupVerificationError(
            "ComfyUI sandbox requires exact verified cgroup leaf placement"
        )
    native_root = Path(f"/sys/fs/cgroup{scope_path}")
    exact_placement = {
        **dict(placement),
        "campaign_cgroup": scope_path,
        "exact_leaf": True,
        "ok": True,
    }
    return {
        "schema_version": HOST_TRANSITION_SCHEMA_VERSION,
        "nonce": nonce,
        "pid": pid,
        "role": "comfyui",
        "cgroup_path": scope_path,
        "leaf_identity": _host_transition_leaf_identity(
            scope_fs=scope_fs,
            scope_path=scope_path,
            pid=pid,
        ),
        "process": {
            "pid": pid,
            "start_ticks": int(placement["start_ticks"]),
            "boot_id": str(placement["boot_id"]),
            "cgroup_path": scope_path,
        },
        "placement": exact_placement,
        "cgroup_write": {
            "target": str(native_root / "cgroup.procs"),
            "attempted": True,
            "completed": True,
            "verified_after_write": True,
            "written_pid": pid,
        },
        "allowed_write_roots": _sandbox_write_root_records(
            allowed_write_roots
        ),
        "created_monotonic_ns": time.monotonic_ns(),
        "actual_execution": True,
        "simulated": False,
        "ok": True,
    }


@dataclass(frozen=True)
class ProcessIdentity:
    """PID identity that remains meaningful across PID reuse."""

    pid: int
    start_ticks: int
    boot_id: str
    cgroup_path: str
    state: str


@dataclass(frozen=True)
class ScopeIdentity:
    """Exact transient-unit and cgroup directory identity."""

    unit_name: str
    invocation_id: str
    cgroup_path: str
    device: int
    inode: int


@dataclass(frozen=True)
class CampaignCgroupLimits:
    """Required hard limits for the formal campaign scope."""

    memory_high_bytes: int = 5 * GIB
    memory_max_bytes: int = 6 * GIB
    memory_swap_max_bytes: int = 512 * MIB
    cpu_quota_percent: int = 300
    tasks_max: int = 384
    io_weight: int = DEFAULT_IO_WEIGHT

    def __post_init__(self) -> None:
        if isinstance(self.io_weight, bool) or not isinstance(self.io_weight, int):
            raise ValueError("io_weight must be an integer")
        values = {
            "memory_high_bytes": self.memory_high_bytes,
            "memory_max_bytes": self.memory_max_bytes,
            "memory_swap_max_bytes": self.memory_swap_max_bytes,
            "cpu_quota_percent": self.cpu_quota_percent,
            "tasks_max": self.tasks_max,
            "io_weight": self.io_weight,
        }
        invalid = [name for name, value in values.items() if isinstance(value, bool) or int(value) <= 0]
        if invalid:
            raise ValueError("cgroup limits must be positive integers: " + ", ".join(invalid))
        if self.memory_high_bytes > self.memory_max_bytes:
            raise ValueError("memory_high_bytes cannot exceed memory_max_bytes")
        if not SYSTEMD_IO_WEIGHT_MIN <= int(self.io_weight) <= SYSTEMD_IO_WEIGHT_MAX:
            raise ValueError(
                "io_weight must be within systemd's inclusive range "
                f"{SYSTEMD_IO_WEIGHT_MIN}..{SYSTEMD_IO_WEIGHT_MAX}"
            )

    def systemd_properties(self) -> tuple[str, ...]:
        return (
            "Delegate=yes",
            f"MemoryHigh={self.memory_high_bytes}",
            f"MemoryMax={self.memory_max_bytes}",
            f"MemorySwapMax={self.memory_swap_max_bytes}",
            f"CPUQuota={self.cpu_quota_percent}%",
            f"TasksMax={self.tasks_max}",
            f"IOWeight={self.io_weight}",
        )

    def expected_files(self) -> dict[str, int]:
        return {
            "memory.high": self.memory_high_bytes,
            "memory.max": self.memory_max_bytes,
            "memory.swap.max": self.memory_swap_max_bytes,
            "pids.max": self.tasks_max,
            "io.weight": self.io_weight,
        }


class SubprocessRunner:
    """Small injectable subprocess boundary used by unit tests."""

    def popen(self, command: Sequence[str], **kwargs: Any) -> subprocess.Popen[Any]:
        return subprocess.Popen(list(command), **kwargs)

    def run(self, command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(command), **kwargs)


def _normalise_cgroup_path(raw: str, *, label: str) -> str:
    value = str(raw or "").strip()
    if not value.startswith("/"):
        raise CgroupVerificationError(f"{label} is not an absolute cgroup path: {value!r}")
    if "\x00" in value:
        raise CgroupVerificationError(f"{label} contains a NUL byte")
    raw_parts = value.split("/")
    if ".." in raw_parts:
        raise CgroupVerificationError(f"{label} contains parent traversal: {value!r}")
    normalised = "/" + "/".join(part for part in raw_parts if part not in {"", "."})
    if normalised == "/":
        return normalised
    return normalised.rstrip("/")


def _path_within(actual: str, expected_parent: str) -> bool:
    actual_path = _normalise_cgroup_path(actual, label="process cgroup")
    parent_path = _normalise_cgroup_path(expected_parent, label="campaign cgroup")
    return actual_path == parent_path or actual_path.startswith(parent_path.rstrip("/") + "/")


def _read_boot_id(proc_root: Path) -> str:
    path = proc_root / "sys" / "kernel" / "random" / "boot_id"
    try:
        value = path.read_text(encoding="ascii").strip()
    except Exception as exc:
        raise CgroupVerificationError(f"cannot read kernel boot identity from {path}: {exc}") from exc
    if not _BOOT_ID_RE.fullmatch(value):
        raise CgroupVerificationError(f"kernel boot identity is invalid: {value!r}")
    return value.lower()


def _read_process_stat(proc_root: Path, pid: int) -> tuple[int, str]:
    path = proc_root / str(int(pid)) / "stat"
    try:
        tail = path.read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
        state = str(tail[0])
        start_ticks = int(tail[19])
    except Exception as exc:
        raise CgroupVerificationError(f"cannot read process identity for pid {pid}: {exc}") from exc
    if start_ticks <= 0 or len(state) != 1:
        raise CgroupVerificationError(
            f"invalid process identity for pid {pid}: start_ticks={start_ticks}, state={state!r}"
        )
    return start_ticks, state


def _read_pid_cgroup(proc_root: Path, pid: int) -> str:
    if isinstance(pid, bool) or int(pid) <= 0:
        raise CgroupVerificationError(f"invalid pid: {pid!r}")
    path = proc_root / str(int(pid)) / "cgroup"
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        raise CgroupVerificationError(f"cannot read cgroup membership for pid {pid}: {exc}") from exc
    for row in rows:
        fields = row.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            return _normalise_cgroup_path(fields[2], label=f"pid {pid} cgroup")
    raise CgroupVerificationError(f"pid {pid} has no unified cgroup v2 membership")


def capture_process_identity(proc_root: Path, pid: int) -> ProcessIdentity:
    """Capture PID/starttime/boot/cgroup while closing the PID-reuse race."""

    if isinstance(pid, bool) or int(pid) <= 0:
        raise CgroupVerificationError(f"invalid pid: {pid!r}")
    first_start, first_state = _read_process_stat(proc_root, int(pid))
    boot_id = _read_boot_id(proc_root)
    cgroup_path = _read_pid_cgroup(proc_root, int(pid))
    second_start, second_state = _read_process_stat(proc_root, int(pid))
    if first_start != second_start:
        raise CgroupVerificationError(
            f"pid {pid} identity changed during inspection: {first_start} -> {second_start}"
        )
    if first_state == "Z" or second_state == "Z":
        raise CgroupVerificationError(f"pid {pid} is a zombie")
    return ProcessIdentity(int(pid), second_start, boot_id, cgroup_path, second_state)


def _assert_same_process_identity(
    expected: ProcessIdentity,
    actual: ProcessIdentity,
    *,
    role: str,
) -> None:
    mismatches: list[str] = []
    if actual.pid != expected.pid:
        mismatches.append(f"pid={actual.pid} expected={expected.pid}")
    if actual.start_ticks != expected.start_ticks:
        mismatches.append(
            f"start_ticks={actual.start_ticks} expected={expected.start_ticks}"
        )
    if actual.boot_id != expected.boot_id:
        mismatches.append(f"boot_id={actual.boot_id} expected={expected.boot_id}")
    if mismatches:
        raise CgroupVerificationError(
            f"{role} process identity changed: " + ", ".join(mismatches)
        )


def _read_cgroup_pids(cgroup_root: Path, cgroup_path: str) -> set[int]:
    relative = _normalise_cgroup_path(cgroup_path, label="cgroup path").lstrip("/")
    path = cgroup_root / relative / "cgroup.procs"
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
        return {int(row.strip()) for row in rows if row.strip()}
    except Exception as exc:
        raise CgroupVerificationError(f"cannot read {path}: {exc}") from exc


def _assert_pid_placement(
    *,
    cgroup_root: Path,
    proc_root: Path,
    scope_path: str,
    pid: int,
    role: str,
    expected_inside: bool,
    expected_identity: ProcessIdentity | None = None,
) -> dict[str, Any]:
    before = capture_process_identity(proc_root, pid)
    if expected_identity is not None:
        _assert_same_process_identity(expected_identity, before, role=role)
    within = _path_within(before.cgroup_path, scope_path)
    if within != expected_inside:
        expectation = "inside" if expected_inside else "outside"
        raise CgroupVerificationError(
            f"{role} pid {pid} must be {expectation} campaign scope {scope_path}; "
            f"actual={before.cgroup_path}"
        )
    actual_members = _read_cgroup_pids(cgroup_root, before.cgroup_path)
    if int(pid) not in actual_members:
        raise CgroupVerificationError(
            f"{role} pid {pid} procfs/cgroupfs disagreement: "
            f"{before.cgroup_path}/cgroup.procs does not contain pid"
        )
    after = capture_process_identity(proc_root, pid)
    _assert_same_process_identity(before, after, role=role)
    if after.cgroup_path != before.cgroup_path:
        raise CgroupVerificationError(
            f"{role} pid {pid} changed cgroup during inspection: "
            f"{before.cgroup_path} -> {after.cgroup_path}"
        )
    return {
        "role": role,
        "pid": int(pid),
        "start_ticks": after.start_ticks,
        "boot_id": after.boot_id,
        "process_state": after.state,
        "actual_cgroup": after.cgroup_path,
        "campaign_cgroup": scope_path,
        "inside_campaign_scope": within,
        "procfs_cgroupfs_agree": True,
        "pid_identity_stable": True,
        "ok": True,
    }


class CampaignCgroup:
    """Create, validate, populate, and stop one delegated campaign scope."""

    def __init__(
        self,
        *,
        campaign_id: str,
        evidence_root: Path,
        limits: CampaignCgroupLimits | None = None,
        cgroup_root: Path = DEFAULT_CGROUP_ROOT,
        proc_root: Path = DEFAULT_PROC_ROOT,
        runner: Any | None = None,
        systemd_run: str = "systemd-run",
        systemctl: str = "systemctl",
        ionice: str = "ionice",
        python_executable: str = sys.executable,
        module_path: Path | None = None,
        start_timeout: float = 15.0,
        stop_timeout: float = 15.0,
        poll_interval: float = 0.1,
        managed_command: Sequence[str] | None = None,
        managed_cwd: Path | None = None,
        managed_stdout: Path | None = None,
        activation_gate: Path | None = None,
        managed_environment: Mapping[str, str] | None = None,
        allow_idle_io_fallback: bool = False,
    ) -> None:
        safe_id = _SCOPE_ID_RE.sub("-", str(campaign_id or "").strip()).strip(".-")
        if not safe_id:
            raise ValueError("campaign_id must contain at least one safe unit-name character")
        self.campaign_id = safe_id[:96]
        self.unit_name = f"hackme-web-campaign-{self.campaign_id}.scope"
        self.evidence_root = Path(evidence_root).expanduser().resolve(strict=False)
        self.limits = limits or CampaignCgroupLimits()
        self.cgroup_root = Path(cgroup_root).expanduser().resolve(strict=False)
        self.proc_root = Path(proc_root).expanduser().resolve(strict=False)
        self.runner = runner or SubprocessRunner()
        self.systemd_run = str(systemd_run)
        self.systemctl = str(systemctl)
        self.ionice = str(ionice)
        self.allow_idle_io_fallback = bool(allow_idle_io_fallback)
        self.python_executable = str(python_executable)
        self.module_path = Path(module_path or __file__).expanduser().resolve(strict=False)
        self.start_timeout = max(0.01, float(start_timeout))
        self.stop_timeout = max(0.01, float(stop_timeout))
        self.poll_interval = max(0.001, float(poll_interval))
        self.managed_command = tuple(str(value) for value in (managed_command or ()))
        self.managed_cwd = Path(managed_cwd).expanduser().resolve(strict=False) if managed_cwd else self.evidence_root
        self.managed_stdout = (
            Path(managed_stdout).expanduser().resolve(strict=False)
            if managed_stdout
            else self.evidence_root / "managed.stdout"
        )
        self.activation_gate = Path(activation_gate).expanduser().resolve(strict=False) if activation_gate else self.evidence_root / "managed.activate.json"
        self.managed_environment = {str(key): str(value) for key, value in (managed_environment or {}).items()}
        self.scope_path = ""
        self.scope_identity: ScopeIdentity | None = None
        self.unit_properties: dict[str, str] = {}
        self.event_baseline: dict[str, dict[str, int]] = {}
        self.anchor_process: Any | None = None
        self.anchor_pid = 0
        self.anchor_identity: ProcessIdentity | None = None
        self.created = False
        self.stopped = False
        self.registered_pids: dict[str, set[int]] = {}
        self.registered_identities: dict[str, dict[int, ProcessIdentity]] = {}
        self.managed_leaves: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.anchor_pid_file = self.evidence_root / "scope_anchor.pid"
        self.anchor_ready_file = self.evidence_root / "scope_anchor.ready.json"
        self.anchor_stop_file = self.evidence_root / "scope_anchor.stop"
        self.systemd_log = self.evidence_root / "systemd_scope.log"
        self.managed_command_file = self.evidence_root / "managed_command.json"
        self.evidence_file = self.evidence_root / "cgroup_scope.json"
        self.entry_evidence_dir = self.evidence_root / "entries"

    def configure_managed_command(
        self,
        command: Sequence[str],
        *,
        activation_gate: Path,
        cwd: Path,
        stdout: Path,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Configure the command the in-scope anchor will exec after release."""

        if self.anchor_process is not None or self.created:
            raise CampaignCgroupError("managed command must be configured before scope creation")
        values = tuple(str(value) for value in command)
        if not values:
            raise ValueError("managed command cannot be empty")
        self.managed_command = values
        self.activation_gate = Path(activation_gate).expanduser().resolve(strict=False)
        self.managed_cwd = Path(cwd).expanduser().resolve(strict=False)
        self.managed_stdout = Path(stdout).expanduser().resolve(strict=False)
        self.managed_environment = {str(key): str(value) for key, value in (environment or {}).items()}

    def _write_managed_command(self) -> None:
        if not self.managed_command:
            raise CampaignCgroupError("managed command is unavailable")
        payload = {
            "schema_version": CGROUP_SCHEMA_VERSION,
            "command": list(self.managed_command),
            "cwd": str(self.managed_cwd),
            "stdout": str(self.managed_stdout),
            "environment": self.managed_environment,
        }
        _atomic_write_json(self.managed_command_file, payload)
        try:
            readback = json.loads(self.managed_command_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CampaignCgroupError(
                f"managed command readback failed: {exc.__class__.__name__}: {exc}"
            ) from exc
        if readback != payload:
            raise CampaignCgroupError("managed command readback differs from its authority")

    def update_managed_environment_before_activation(
        self,
        values: Mapping[str, str],
    ) -> dict[str, Any]:
        """Add reviewed non-secret values before releasing the managed anchor.

        The anchor reads ``managed_command.json`` only after the activation
        gate appears.  This narrow API lets the supervisor bind a newly
        launched dependency PID into the runner environment without placing
        credentials in argv or adopting an external process.
        """

        if not self.created or self.stopped or self.anchor_process is None:
            raise CampaignCgroupError(
                "managed environment can only change in an active campaign scope"
            )
        if self.activation_gate.exists() or self.anchor_process.poll() is not None:
            raise CampaignCgroupError(
                "managed environment cannot change after activation or anchor exit"
            )
        updates = {str(key): str(value) for key, value in values.items()}
        unexpected = sorted(set(updates) - LATE_BOUND_MANAGED_ENVIRONMENT_KEYS)
        if unexpected:
            raise CampaignCgroupError(
                "managed environment update contains unreviewed keys: "
                + ", ".join(unexpected)
            )
        invalid = sorted(
            key
            for key, value in updates.items()
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)
            or "\x00" in value
            or "\x00" in key
        )
        if invalid:
            raise CampaignCgroupError(
                "managed environment update contains invalid names/values: "
                + ", ".join(invalid)
            )
        self.managed_environment.update(updates)
        self._write_managed_command()
        return self._record(
            "update_managed_environment_before_activation",
            ok=True,
            environment_keys=sorted(updates),
            # Values are intentionally excluded from cgroup lifecycle logs.
            values_logged=False,
        )

    def release_managed_command(self) -> dict[str, Any]:
        """Release the in-scope anchor to exec the still-gated runner."""

        if not self.created or self.stopped or self.anchor_process is None:
            raise CampaignCgroupError(
                "managed command can only be released in an active campaign scope"
            )
        if self.anchor_process.poll() is not None:
            raise CampaignCgroupError("managed anchor exited before command release")
        if self.activation_gate.exists():
            raise CampaignCgroupError("managed command release gate already exists")
        if not self.managed_command_file.is_file():
            raise CampaignCgroupError("managed command authority is unavailable")
        payload = {
            "schema_version": CGROUP_SCHEMA_VERSION,
            "released_at": utc_now(),
            "anchor_pid": self.anchor_pid,
            "managed_command_sha256": _sha256_file(self.managed_command_file),
            "ok": True,
        }
        _atomic_write_json(self.activation_gate, payload)
        try:
            readback = json.loads(self.activation_gate.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CampaignCgroupError(
                f"managed command release readback failed: {exc.__class__.__name__}: {exc}"
            ) from exc
        if readback != payload:
            raise CampaignCgroupError("managed command release readback differs")
        return self._record(
            "release_managed_command",
            ok=True,
            gate=str(self.activation_gate),
            managed_command_sha256=payload["managed_command_sha256"],
        )

    def _record(self, action: str, **payload: Any) -> dict[str, Any]:
        event = {"action": action, "at": utc_now(), **payload}
        self.events.append(event)
        self._write_evidence()
        return event

    def _write_evidence(self) -> None:
        payload = {
            "sample_schema_version": CGROUP_SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "unit_name": self.unit_name,
            "cgroup_path": self.scope_path,
            "created": self.created,
            "stopped": self.stopped,
            "anchor_pid": self.anchor_pid,
            "anchor_identity": (
                {
                    "pid": self.anchor_identity.pid,
                    "start_ticks": self.anchor_identity.start_ticks,
                    "boot_id": self.anchor_identity.boot_id,
                    "cgroup_path": self.anchor_identity.cgroup_path,
                    "state": self.anchor_identity.state,
                }
                if self.anchor_identity is not None
                else None
            ),
            "unit_properties": dict(self.unit_properties),
            "scope_identity": (
                {
                    "unit_name": self.scope_identity.unit_name,
                    "invocation_id": self.scope_identity.invocation_id,
                    "cgroup_path": self.scope_identity.cgroup_path,
                    "device": self.scope_identity.device,
                    "inode": self.scope_identity.inode,
                }
                if self.scope_identity is not None
                else None
            ),
            "event_baseline": self.event_baseline,
            "expected_limits": {
                **self.limits.expected_files(),
                "cpu.quota_percent": self.limits.cpu_quota_percent,
                "allow_idle_io_fallback": self.allow_idle_io_fallback,
            },
            "registered_pids": {role: sorted(pids) for role, pids in sorted(self.registered_pids.items())},
            "managed_leaves": {
                role: dict(evidence)
                for role, evidence in sorted(self.managed_leaves.items())
            },
            "events": self.events,
            "updated_at": utc_now(),
        }
        _atomic_write_json(self.evidence_file, payload)

    def _check_v2_controllers(self) -> dict[str, Any]:
        mountinfo_path = self.proc_root / "self" / "mountinfo"
        try:
            mount_rows = mountinfo_path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            raise CgroupUnavailableError(f"cannot prove cgroup v2 mount from {mountinfo_path}: {exc}") from exc
        mount_evidence: dict[str, Any] | None = None
        for row in mount_rows:
            fields = row.split()
            try:
                separator = fields.index("-")
                filesystem_type = fields[separator + 1]
                encoded_mountpoint = fields[4]
            except (ValueError, IndexError):
                continue
            mountpoint = (
                encoded_mountpoint.replace("\\040", " ")
                .replace("\\011", "\t")
                .replace("\\012", "\n")
                .replace("\\134", "\\")
            )
            if Path(mountpoint).resolve(strict=False) == self.cgroup_root:
                mount_evidence = {
                    "mountpoint": mountpoint,
                    "filesystem_type": filesystem_type,
                    "ok": filesystem_type == "cgroup2",
                }
                break
        if mount_evidence is None or not mount_evidence["ok"]:
            actual = (mount_evidence or {}).get("filesystem_type", "missing")
            raise CgroupUnavailableError(
                f"{self.cgroup_root} is not proven to be a cgroup2 mount (actual={actual})"
            )
        controllers_path = self.cgroup_root / "cgroup.controllers"
        try:
            controllers = set(controllers_path.read_text(encoding="utf-8").split())
        except Exception as exc:
            raise CgroupUnavailableError(f"cgroup v2 is unavailable at {self.cgroup_root}: {exc}") from exc
        required = {"cpu", "io", "memory", "pids"}
        missing = sorted(required - controllers)
        if missing:
            raise CgroupUnavailableError("mandatory cgroup v2 controllers unavailable: " + ", ".join(missing))
        return {
            "mount": mount_evidence,
            "controllers": sorted(controllers),
            "required": sorted(required),
            "ok": True,
        }

    def _scope_command(self) -> list[str]:
        command = [
            self.systemd_run,
            "--user",
            "--scope",
            "--quiet",
            f"--unit={self.unit_name}",
        ]
        for prop in self.limits.systemd_properties():
            command.extend(["--property", prop])
        command.append("--")
        if self.allow_idle_io_fallback:
            command.extend([self.ionice, "-c", "3"])
        command.extend([
            self.python_executable,
            str(self.module_path),
            "_anchor",
            "--pid-file",
            str(self.anchor_pid_file),
            "--ready-file",
            str(self.anchor_ready_file),
            "--stop-file",
            str(self.anchor_stop_file),
        ])
        if self.managed_command:
            command.extend([
                "--command-file", str(self.managed_command_file),
                "--activation-gate", str(self.activation_gate),
                "--cwd", str(self.managed_cwd),
                "--stdout", str(self.managed_stdout),
            ])
        return command

    def _systemctl(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        return self.runner.run(
            [self.systemctl, "--user", *args],
            text=True,
            capture_output=True,
            timeout=self.stop_timeout if timeout is None else timeout,
            check=False,
        )

    def _query_unit_properties(self) -> dict[str, str]:
        completed = self._systemctl(
            "show",
            self.unit_name,
            "--property=Id",
            "--property=Names",
            "--property=ActiveState",
            "--property=SubState",
            "--property=ControlGroup",
            "--property=InvocationID",
            "--property=Delegate",
            "--property=IOWeight",
            timeout=min(5.0, self.start_timeout),
        )
        if completed.returncode != 0:
            return {}
        result: dict[str, str] = {}
        for row in str(completed.stdout or "").splitlines():
            if "=" not in row:
                continue
            name, value = row.split("=", 1)
            result[name.strip()] = value.strip()
        return result

    def _validated_systemd_io_weight(
        self,
        properties: Mapping[str, str],
    ) -> int:
        raw = properties.get("IOWeight")
        try:
            actual = int(str(raw))
        except (TypeError, ValueError) as exc:
            raise CgroupVerificationError(
                f"systemd IOWeight authority is invalid: {raw!r}"
            ) from exc
        if actual != self.limits.io_weight:
            raise CgroupVerificationError(
                "systemd IOWeight authority mismatch: "
                f"expected {self.limits.io_weight}, got {actual}"
            )
        return actual

    def _control_group(self) -> str:
        properties = self._query_unit_properties()
        if not properties:
            return ""
        errors: list[str] = []
        if properties.get("Id") != self.unit_name:
            errors.append(f"Id={properties.get('Id')!r}")
        if self.unit_name not in set(properties.get("Names", "").split()):
            errors.append(f"Names={properties.get('Names')!r}")
        if properties.get("ActiveState") not in {"active", "activating"}:
            errors.append(f"ActiveState={properties.get('ActiveState')!r}")
        if properties.get("Delegate", "").lower() not in {"yes", "true"}:
            errors.append(f"Delegate={properties.get('Delegate')!r}")
        try:
            self._validated_systemd_io_weight(properties)
        except CgroupVerificationError as exc:
            errors.append(str(exc))
        invocation_id = properties.get("InvocationID", "")
        if not _INVOCATION_ID_RE.fullmatch(invocation_id):
            errors.append(f"InvocationID={invocation_id!r}")
        if errors:
            raise CgroupVerificationError(
                "systemd transient scope identity is not proven: " + ", ".join(errors)
            )
        value = properties.get("ControlGroup", "")
        if not value:
            raise CgroupVerificationError("systemd transient scope has no ControlGroup")
        normalised = _normalise_cgroup_path(value, label="systemd ControlGroup")
        if normalised == "/":
            raise CgroupVerificationError("systemd returned the root cgroup instead of a dedicated campaign scope")
        self.unit_properties = properties
        return normalised

    def _scope_fs_path(self, cgroup_path: str | None = None) -> Path:
        value = _normalise_cgroup_path(cgroup_path or self.scope_path, label="campaign cgroup")
        if value == "/":
            raise CgroupVerificationError("campaign scope cannot be the cgroup root")
        candidate = (self.cgroup_root / value.lstrip("/")).resolve(strict=False)
        if candidate == self.cgroup_root or self.cgroup_root not in candidate.parents:
            raise CgroupVerificationError(f"campaign cgroup escapes cgroup root: {value}")
        return candidate

    def create_scope(self) -> dict[str, Any]:
        """Create the transient scope and prove all configured limits.

        There is intentionally no best-effort mode.  Any setup or evidence
        failure stops the transient unit and raises ``CampaignCgroupError``.
        """

        if self.created and not self.stopped:
            raise CampaignCgroupError("campaign scope is already active")
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        for stale in (self.anchor_pid_file, self.anchor_ready_file, self.anchor_stop_file, self.activation_gate):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
        if self.managed_command:
            self._write_managed_command()
        controller_evidence: dict[str, Any] = {}
        command = self._scope_command()
        try:
            controller_evidence = self._check_v2_controllers()
            with self.systemd_log.open("a", encoding="utf-8") as systemd_log:
                self.anchor_process = self.runner.popen(
                    command,
                    cwd=str(self.evidence_root),
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=systemd_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            deadline = time.monotonic() + self.start_timeout
            last_scope_error: CgroupVerificationError | None = None
            while time.monotonic() < deadline:
                if self.anchor_process.poll() is not None:
                    raise CgroupUnavailableError(
                        f"systemd-run exited before the campaign scope became ready: returncode={self.anchor_process.poll()}"
                    )
                if not self.scope_path:
                    try:
                        self.scope_path = self._control_group()
                    except CgroupVerificationError as exc:
                        # systemd can briefly expose an inactive placeholder
                        # before Delegate, IOWeight, InvocationID, and the
                        # ControlGroup are committed.  Keep the campaign inert
                        # and retry only inside the bounded startup deadline.
                        last_scope_error = exc
                if self.scope_path and self.anchor_pid_file.exists() and self.anchor_ready_file.exists():
                    break
                time.sleep(self.poll_interval)
            if not self.scope_path:
                if last_scope_error is not None:
                    raise CgroupVerificationError(
                        "systemd did not establish a verified campaign scope "
                        f"before the startup deadline: {last_scope_error}"
                    ) from last_scope_error
                raise CgroupUnavailableError("systemd did not expose a ControlGroup for the campaign scope")
            try:
                self.anchor_pid = int(self.anchor_pid_file.read_text(encoding="utf-8").strip())
            except Exception as exc:
                raise CgroupVerificationError(f"scope anchor pid evidence is unavailable: {exc}") from exc
            try:
                ready_payload = json.loads(self.anchor_ready_file.read_text(encoding="utf-8"))
            except Exception as exc:
                raise CgroupVerificationError(f"scope anchor ready evidence is invalid: {exc}") from exc
            if (
                not isinstance(ready_payload, dict)
                or ready_payload.get("ok") is not True
                or int(ready_payload.get("pid") or 0) != self.anchor_pid
            ):
                raise CgroupVerificationError("scope anchor ready evidence does not match its PID")
            scope_identity = self.capture_scope_identity()
            limit_evidence = self.verify_limits()
            self.anchor_identity = capture_process_identity(self.proc_root, self.anchor_pid)
            anchor_evidence = self.assert_pid_membership(
                self.anchor_pid,
                role="scope_anchor",
                expected_identity=self.anchor_identity,
            )
            supervisor_evidence = self.assert_pid_outside(os.getpid(), role="campaign_supervisor")
            event_baseline = self.capture_event_baseline()
            self.created = True
            self.stopped = False
            return self._record(
                "create_scope",
                ok=True,
                command=command,
                systemd_log=str(self.systemd_log),
                controller_evidence=controller_evidence,
                limit_evidence=limit_evidence,
                scope_identity=scope_identity,
                anchor_evidence=anchor_evidence,
                supervisor_evidence=supervisor_evidence,
                event_baseline=event_baseline,
            )
        except Exception as exc:
            cleanup = self._best_effort_stop()
            self.created = False
            self.stopped = True
            self._record(
                "create_scope",
                ok=False,
                command=command,
                systemd_log=str(self.systemd_log),
                controller_evidence=controller_evidence,
                error=f"{exc.__class__.__name__}: {exc}",
                cleanup=cleanup,
            )
            if isinstance(exc, CampaignCgroupError):
                raise
            raise CgroupUnavailableError(f"failed to create campaign cgroup: {exc}") from exc

    @staticmethod
    def _parse_io_weight_authority(raw: str) -> int:
        rows = raw.splitlines()
        if len(rows) != 1:
            raise ValueError(
                "io.weight must contain exactly one default authority row"
            )
        fields = rows[0].split()
        if len(fields) != 2 or fields[0] != "default":
            raise ValueError(
                "io.weight authority must use the exact 'default WEIGHT' format"
            )
        weight = int(fields[1])
        if not SYSTEMD_IO_WEIGHT_MIN <= weight <= SYSTEMD_IO_WEIGHT_MAX:
            raise ValueError(
                "io.weight authority is outside systemd's valid range"
            )
        return weight

    def _verify_anchor_idle_io_priority(self) -> dict[str, Any]:
        if self.anchor_pid <= 0:
            return {
                "ok": False,
                "pid": self.anchor_pid,
                "error": "scope anchor pid is unavailable",
            }
        try:
            completed = self.runner.run(
                [self.ionice, "-p", str(self.anchor_pid)],
                text=True,
                capture_output=True,
                timeout=min(5.0, self.start_timeout),
                check=False,
            )
        except Exception as exc:
            return {
                "ok": False,
                "pid": self.anchor_pid,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        stdout = str(completed.stdout or "").strip()
        stderr = str(completed.stderr or "").strip()
        idle = completed.returncode == 0 and bool(
            re.search(r"(?:^|\s)idle(?:\s|:|$)", stdout, flags=re.IGNORECASE)
        )
        return {
            "ok": idle,
            "pid": self.anchor_pid,
            "command": [self.ionice, "-p", str(self.anchor_pid)],
            "returncode": completed.returncode,
            "stdout": stdout[:500],
            "stderr": stderr[:500],
            "expected_class": "idle",
        }

    def verify_limits(self) -> dict[str, Any]:
        """Read actual cgroup v2 files and prove every configured limit."""

        if not self.scope_path:
            raise CgroupVerificationError("campaign cgroup path is not known")
        scope = self._scope_fs_path()
        checks: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        io_safety_mode = ""
        for filename, expected in self.limits.expected_files().items():
            path = scope / filename
            try:
                raw = path.read_text(encoding="utf-8").strip()
                actual = (
                    self._parse_io_weight_authority(raw)
                    if filename == "io.weight"
                    else int(raw)
                )
                ok = actual == expected
                checks[filename] = {"expected": expected, "actual": actual, "raw": raw, "ok": ok}
                if not ok:
                    errors.append(f"{filename}: expected {expected}, got {raw}")
                elif filename == "io.weight":
                    io_safety_mode = "cgroup_weight"
            except FileNotFoundError as exc:
                if filename == "io.weight" and self.allow_idle_io_fallback:
                    fallback = self._verify_anchor_idle_io_priority()
                    fallback_ok = fallback.get("ok") is True
                    checks[filename] = {
                        "expected": expected,
                        "actual": None,
                        "ok": fallback_ok,
                        "cgroup_controller_available": False,
                        "fallback": fallback,
                    }
                    if fallback_ok:
                        io_safety_mode = "process_idle"
                    else:
                        errors.append(
                            "io.weight: unavailable and live idle I/O priority is not proven"
                        )
                    continue
                checks[filename] = {
                    "expected": expected,
                    "actual": None,
                    "ok": False,
                    "error": str(exc),
                }
                errors.append(f"{filename}: {exc}")
            except Exception as exc:
                checks[filename] = {"expected": expected, "actual": None, "ok": False, "error": str(exc)}
                errors.append(f"{filename}: {exc}")
        cpu_path = scope / "cpu.max"
        try:
            raw_cpu = cpu_path.read_text(encoding="utf-8").strip()
            quota_text, period_text = raw_cpu.split()
            if quota_text == "max":
                raise ValueError("quota is unlimited")
            quota = int(quota_text)
            period = int(period_text)
            actual_percent = quota * 100 / period if period > 0 else 0.0
            cpu_ok = period > 0 and quota * 100 == self.limits.cpu_quota_percent * period
            checks["cpu.max"] = {
                "expected_percent": self.limits.cpu_quota_percent,
                "actual_percent": actual_percent,
                "quota_us": quota,
                "period_us": period,
                "raw": raw_cpu,
                "ok": cpu_ok,
            }
            if not cpu_ok:
                errors.append(
                    f"cpu.max: expected {self.limits.cpu_quota_percent}%, got {actual_percent:g}% ({raw_cpu})"
                )
        except Exception as exc:
            checks["cpu.max"] = {
                "expected_percent": self.limits.cpu_quota_percent,
                "actual_percent": None,
                "ok": False,
                "error": str(exc),
            }
            errors.append(f"cpu.max: {exc}")
        for required_file in ("cgroup.procs", "cgroup.events", "memory.events"):
            path = scope / required_file
            readable = False
            error = ""
            try:
                path.read_text(encoding="utf-8")
                readable = True
            except Exception as exc:
                error = str(exc)
                errors.append(f"{required_file}: {exc}")
            checks[required_file] = {"required_readable": True, "ok": readable, "error": error}
        for control_file in ("cgroup.freeze", "cgroup.kill"):
            path = scope / control_file
            writable = False
            error = ""
            descriptor = -1
            try:
                if not path.is_file():
                    raise FileNotFoundError(path)
                descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
                writable = True
            except Exception as exc:
                error = str(exc)
                errors.append(f"{control_file}: {exc}")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            checks[control_file] = {"required_writable": True, "ok": writable, "error": error}
        evidence = {
            "sample_schema_version": CGROUP_SCHEMA_VERSION,
            "cgroup_path": self.scope_path,
            "checks": checks,
            "missing_or_mismatched": errors,
            "io_safety_mode": io_safety_mode,
            "hard_limit_state": "verified" if not errors else "unverified",
            "ok": not errors,
        }
        self._record("verify_limits", **evidence)
        if errors:
            raise CgroupVerificationError("campaign cgroup limits are not proven: " + "; ".join(errors))
        return evidence

    @staticmethod
    def _parse_counter_file(path: Path) -> dict[str, int]:
        try:
            rows = path.read_text(encoding="ascii").splitlines()
        except Exception as exc:
            raise CgroupVerificationError(f"cannot read cgroup counter file {path}: {exc}") from exc
        result: dict[str, int] = {}
        for row in rows:
            fields = row.split()
            if len(fields) != 2:
                raise CgroupVerificationError(f"invalid cgroup counter row in {path}: {row!r}")
            try:
                value = int(fields[1])
            except ValueError as exc:
                raise CgroupVerificationError(
                    f"invalid cgroup counter value in {path}: {row!r}"
                ) from exc
            if value < 0:
                raise CgroupVerificationError(f"negative cgroup counter in {path}: {row!r}")
            result[fields[0]] = value
        return result

    def _read_event_counters(self) -> dict[str, dict[str, int]]:
        scope = self._scope_fs_path()
        counters: dict[str, dict[str, int]] = {}
        for filename, required_keys in MANDATORY_EVENT_COUNTERS.items():
            values = self._parse_counter_file(scope / filename)
            missing = sorted(set(required_keys) - set(values))
            if missing:
                raise CgroupVerificationError(
                    f"{filename} is missing mandatory counters: {', '.join(missing)}"
                )
            counters[filename] = {name: values[name] for name in required_keys}
        for filename, selected_keys in OPTIONAL_EVENT_COUNTERS.items():
            path = scope / filename
            if not path.exists():
                continue
            values = self._parse_counter_file(path)
            counters[filename] = {
                name: values[name] for name in selected_keys if name in values
            }
        return counters

    def capture_event_baseline(self) -> dict[str, Any]:
        """Pin fresh-scope OOM and PID-exhaustion counters before release."""

        counters = self._read_event_counters()
        nonzero = {
            f"{filename}.{name}": value
            for filename, values in counters.items()
            for name, value in values.items()
            if value != 0
        }
        evidence = {
            "sample_schema_version": CGROUP_SCHEMA_VERSION,
            "captured_at": utc_now(),
            "counters": counters,
            "nonzero_fresh_scope_counters": nonzero,
            "ok": not nonzero,
        }
        self.event_baseline = counters
        self._record("capture_event_baseline", **evidence)
        if nonzero:
            raise CgroupVerificationError(
                "fresh campaign scope already contains resource-failure events: "
                + ", ".join(f"{name}={value}" for name, value in sorted(nonzero.items()))
            )
        return evidence

    def verify_event_counters_unchanged(self) -> dict[str, Any]:
        """Fail if OOM, pids.max, or swap failure counters changed since H0."""

        if not self.event_baseline:
            raise CgroupVerificationError("campaign cgroup event baseline is unavailable")
        current = self._read_event_counters()
        deltas: dict[str, int] = {}
        errors: list[str] = []
        for filename, baseline_values in self.event_baseline.items():
            current_values = current.get(filename)
            if current_values is None:
                errors.append(f"{filename}:missing")
                continue
            for name, baseline in baseline_values.items():
                if name not in current_values:
                    errors.append(f"{filename}.{name}:missing")
                    continue
                delta = int(current_values[name]) - int(baseline)
                deltas[f"{filename}.{name}"] = delta
                if delta != 0:
                    errors.append(f"{filename}.{name}:delta={delta}")
        evidence = {
            "sample_schema_version": CGROUP_SCHEMA_VERSION,
            "baseline": self.event_baseline,
            "current": current,
            "deltas": deltas,
            "errors": errors,
            "ok": not errors,
            "checked_at": utc_now(),
        }
        self._record("verify_event_counters_unchanged", **evidence)
        if errors:
            raise CgroupVerificationError(
                "campaign cgroup resource-failure counters changed: " + "; ".join(errors)
            )
        return evidence

    def capture_scope_identity(self) -> dict[str, Any]:
        """Pin the exact cgroup directory identity for the external watchdog."""

        if not self.scope_path:
            raise CgroupVerificationError("campaign cgroup path is not known")
        scope = self._scope_fs_path()
        try:
            metadata = scope.lstat()
        except Exception as exc:
            raise CgroupVerificationError(f"cannot stat campaign cgroup {scope}: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CgroupVerificationError(f"campaign cgroup is not a real directory: {scope}")
        invocation_id = str(self.unit_properties.get("InvocationID") or "")
        if not _INVOCATION_ID_RE.fullmatch(invocation_id):
            raise CgroupVerificationError("systemd InvocationID is not pinned")
        self.scope_identity = ScopeIdentity(
            unit_name=self.unit_name,
            invocation_id=invocation_id,
            cgroup_path=self.scope_path,
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
        )
        return {
            "path": self.scope_path,
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "unit_name": self.unit_name,
            "invocation_id": invocation_id,
            "active_state": self.unit_properties.get("ActiveState"),
            "sub_state": self.unit_properties.get("SubState"),
            "delegate": self.unit_properties.get("Delegate"),
            "io_weight": self._validated_systemd_io_weight(
                self.unit_properties
            ),
            "ok": True,
        }

    def assert_pid_membership(
        self,
        pid: int,
        *,
        role: str,
        expected_identity: ProcessIdentity | None = None,
    ) -> dict[str, Any]:
        """Prove that a live managed PID is inside this scope."""

        if not self.scope_path:
            raise CgroupVerificationError("campaign cgroup path is not known")
        return _assert_pid_placement(
            cgroup_root=self.cgroup_root,
            proc_root=self.proc_root,
            scope_path=self.scope_path,
            pid=int(pid),
            role=str(role),
            expected_inside=True,
            expected_identity=expected_identity,
        )

    def assert_pid_outside(
        self,
        pid: int,
        *,
        role: str,
        expected_identity: ProcessIdentity | None = None,
    ) -> dict[str, Any]:
        """Prove that a supervisor-side PID is outside this scope."""

        if not self.scope_path:
            raise CgroupVerificationError("campaign cgroup path is not known")
        return _assert_pid_placement(
            cgroup_root=self.cgroup_root,
            proc_root=self.proc_root,
            scope_path=self.scope_path,
            pid=int(pid),
            role=str(role),
            expected_inside=False,
            expected_identity=expected_identity,
        )

    def assert_watchdog_outside(self, watchdog_pid: int) -> dict[str, Any]:
        """Fail unless the external watchdog is provably outside the scope."""
        try:
            evidence = self.assert_pid_outside(int(watchdog_pid), role="external_watchdog")
            self._record("assert_watchdog_outside", ok=True, placement=evidence)
            return evidence
        except CgroupVerificationError as exc:
            self._record(
                "assert_watchdog_outside",
                ok=False,
                watchdog_pid=int(watchdog_pid),
                error=str(exc),
            )
            raise

    def register_pid(self, role: str, pid: int) -> dict[str, Any]:
        role_name = str(role).strip().lower()
        if not _ROLE_RE.fullmatch(role_name):
            raise ValueError(f"invalid campaign process role: {role!r}")
        identity = capture_process_identity(self.proc_root, int(pid))
        evidence = self.assert_pid_membership(
            int(pid), role=role_name, expected_identity=identity
        )
        self.registered_pids.setdefault(role_name, set()).add(int(pid))
        self.registered_identities.setdefault(role_name, {})[int(pid)] = identity
        self._record("register_pid", ok=True, placement=evidence)
        return evidence

    def verify_process_placement(
        self,
        role_pids: Mapping[str, Iterable[int]] | None = None,
        *,
        watchdog_pid: int,
        required_roles: Iterable[str] = MANDATORY_MANAGED_ROLES,
    ) -> dict[str, Any]:
        """Prove every mandatory workload role is scoped and watchdog is not.

        The default contract requires explicit evidence for primary, recovery,
        load generator, browser, ffmpeg, BT, ComfyUI, and a scenario worker.
        Callers may pass a phase-specific ``required_roles`` subset for interim
        checks, but the formal preflight must use the default set.
        """

        combined: dict[str, set[int]] = {role: set(pids) for role, pids in self.registered_pids.items()}
        for role, pids in (role_pids or {}).items():
            role_name = str(role).strip().lower()
            if not _ROLE_RE.fullmatch(role_name):
                raise ValueError(f"invalid campaign process role: {role!r}")
            combined.setdefault(role_name, set()).update(int(pid) for pid in pids)
        required = {str(role).strip().lower() for role in required_roles}
        missing = sorted(role for role in required if not combined.get(role))
        placements: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        if missing:
            errors.append("missing mandatory roles: " + ", ".join(missing))
        for role in sorted(required):
            rows: list[dict[str, Any]] = []
            for pid in sorted(combined.get(role) or set()):
                try:
                    rows.append(self.assert_pid_membership(
                        pid,
                        role=role,
                        expected_identity=(self.registered_identities.get(role) or {}).get(pid),
                    ))
                except CgroupVerificationError as exc:
                    rows.append({"role": role, "pid": pid, "ok": False, "error": str(exc)})
                    errors.append(str(exc))
            placements[role] = rows
        try:
            watchdog = self.assert_watchdog_outside(watchdog_pid)
        except CgroupVerificationError as exc:
            watchdog = {"role": "external_watchdog", "pid": int(watchdog_pid), "ok": False, "error": str(exc)}
            errors.append(str(exc))
        evidence = {
            "sample_schema_version": CGROUP_SCHEMA_VERSION,
            "required_roles": sorted(required),
            "missing_roles": missing,
            "placements": placements,
            "watchdog": watchdog,
            "errors": errors,
            "ok": not errors,
        }
        self._record("verify_process_placement", **evidence)
        if errors:
            raise CgroupVerificationError("campaign PID placement is not proven: " + "; ".join(errors))
        return evidence

    def _managed_leaf_path(self, role: str) -> Path:
        role_name = str(role).strip().lower()
        evidence = self.managed_leaves.get(role_name)
        if not isinstance(evidence, Mapping):
            raise CgroupVerificationError(
                f"managed cgroup leaf is unavailable for role {role_name!r}"
            )
        path = Path(str(evidence.get("fs_path") or ""))
        expected = self._scope_fs_path() / role_name
        if path != expected:
            raise CgroupVerificationError(
                f"managed cgroup leaf path escaped its pinned scope for {role_name}"
            )
        try:
            metadata = path.lstat()
        except Exception as exc:
            raise CgroupVerificationError(
                f"managed cgroup leaf disappeared for {role_name}: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or int(metadata.st_dev) != int(evidence.get("device") or -1)
            or int(metadata.st_ino) != int(evidence.get("inode") or -1)
        ):
            raise CgroupVerificationError(
                f"managed cgroup leaf identity changed for {role_name}"
            )
        return path

    def create_managed_leaf(self, role: str) -> dict[str, Any]:
        """Create and pin a factual workload leaf below the delegated scope.

        The leaf inherits the campaign limits.  At this host stage it proves
        only that no subtree controllers or descendant cgroups exist;
        delegation capability remains pending until the ComfyUI sandbox has
        completed.  The leaf still gives teardown a kernel-owned
        PID/populated authority when descendants call ``setsid`` or scrub
        their environments.
        """

        if not self.created or self.stopped:
            raise CampaignCgroupError(
                "managed leaf requires an active verified campaign scope"
            )
        role_name = str(role).strip().lower()
        if not _ROLE_RE.fullmatch(role_name):
            raise ValueError(f"invalid campaign process role: {role!r}")
        if role_name in self.managed_leaves:
            raise CampaignCgroupError(
                f"managed cgroup leaf already exists for {role_name}"
            )
        self._assert_pinned_scope_identity()
        scope = self._scope_fs_path()
        leaf = scope / role_name
        try:
            leaf.mkdir(mode=0o755)
            metadata = leaf.lstat()
            required_files = {
                "cgroup.procs",
                "cgroup.events",
                "cgroup.kill",
                "cgroup.type",
                "cgroup.subtree_control",
            }
            missing = sorted(
                filename
                for filename in required_files
                if not (leaf / filename).is_file()
            )
            if missing:
                raise CgroupVerificationError(
                    "managed cgroup leaf lacks kernel controls: "
                    + ", ".join(missing)
                )
            subtree_control = (leaf / "cgroup.subtree_control").read_text(
                encoding="utf-8"
            ).strip()
            if subtree_control:
                raise CgroupVerificationError(
                    "managed cgroup leaf unexpectedly delegates subtree controllers"
                )
            cgroup_type = (leaf / "cgroup.type").read_text(
                encoding="utf-8"
            ).strip()
            if cgroup_type not in {"domain", "domain threaded"}:
                raise CgroupVerificationError(
                    f"managed cgroup leaf has unsupported type {cgroup_type!r}"
                )
            if (leaf / "cgroup.procs").read_text(encoding="utf-8").strip():
                raise CgroupVerificationError(
                    "managed cgroup leaf was populated before launch"
                )
            events = self._parse_counter_file(leaf / "cgroup.events")
            if int(events.get("populated", -1)) != 0:
                raise CgroupVerificationError(
                    "managed cgroup leaf populated state is not zero before launch"
                )
            descendant_cgroups = _count_native_cgroup_descendants(leaf)
            if descendant_cgroups:
                raise CgroupVerificationError(
                    "managed cgroup leaf has descendants before launch"
                )
            cgroup_path = _normalise_cgroup_path(
                f"{self.scope_path.rstrip('/')}/{role_name}",
                label=f"{role_name} managed cgroup leaf",
            )
            evidence = {
                "role": role_name,
                "cgroup_path": cgroup_path,
                "fs_path": str(leaf),
                "device": int(metadata.st_dev),
                "inode": int(metadata.st_ino),
                "cgroup_type": cgroup_type,
                "subtree_control": [],
                "subtree_controllers_enabled": False,
                "descendant_cgroups": 0,
                "workload_delegation_capability": "pending_sandbox",
                "initial_populated": 0,
                "ok": True,
            }
            self.managed_leaves[role_name] = evidence
            return self._record("create_managed_leaf", **evidence)
        except Exception as exc:
            try:
                leaf.rmdir()
            except Exception:
                pass
            self._record(
                "create_managed_leaf",
                role=role_name,
                fs_path=str(leaf),
                ok=False,
                error=f"{exc.__class__.__name__}: {exc}",
            )
            if isinstance(exc, CampaignCgroupError):
                raise
            raise CgroupVerificationError(
                f"cannot create managed cgroup leaf for {role_name}: {exc}"
            ) from exc

    def managed_leaf_pids(self, role: str) -> set[int]:
        """Return all PIDs in the pinned leaf and any unexpected descendants."""

        leaf = self._managed_leaf_path(role)
        pids: set[int] = set()
        files = list(leaf.rglob("cgroup.procs"))
        if not files or len(files) > 1024:
            raise CgroupVerificationError(
                f"managed cgroup leaf PID authority is invalid for {role}"
            )
        for path in files:
            try:
                for row in path.read_text(encoding="utf-8").splitlines():
                    if row.strip():
                        pid = int(row.strip())
                        if pid <= 0:
                            raise ValueError(pid)
                        pids.add(pid)
            except Exception as exc:
                raise CgroupVerificationError(
                    f"cannot enumerate managed cgroup leaf PIDs for {role}: {exc}"
                ) from exc
        return pids

    def managed_leaf_state(self, role: str) -> dict[str, Any]:
        role_name = str(role).strip().lower()
        leaf = self._managed_leaf_path(role_name)
        pids = self.managed_leaf_pids(role_name)
        events = self._parse_counter_file(leaf / "cgroup.events")
        populated = int(events.get("populated", -1))
        if populated not in {0, 1}:
            raise CgroupVerificationError(
                f"managed cgroup leaf populated value is invalid for {role_name}"
            )
        subtree_control = (leaf / "cgroup.subtree_control").read_text(
            encoding="utf-8"
        ).strip()
        cgroup_procs_files = list(leaf.rglob("cgroup.procs"))
        descendant_cgroups = max(0, len(cgroup_procs_files) - 1)
        consistent = bool(populated == (1 if pids else 0))
        topology_intact = not subtree_control and descendant_cgroups == 0
        return {
            "role": role_name,
            "cgroup_path": self.managed_leaves[role_name]["cgroup_path"],
            "pids": sorted(pids),
            "populated": populated,
            "consistent": consistent,
            "subtree_control": subtree_control.split() if subtree_control else [],
            "subtree_controllers_enabled": bool(subtree_control),
            "descendant_cgroups": descendant_cgroups,
            "topology_intact": topology_intact,
            "workload_delegation_capability": "pending_sandbox",
            "ok": bool(consistent and topology_intact),
        }

    def kill_managed_leaf(self, role: str) -> dict[str, Any]:
        """Kill the pinned leaf and prove recursive PID/populated emptiness."""

        role_name = str(role).strip().lower()
        leaf = self._managed_leaf_path(role_name)
        before = self.managed_leaf_state(role_name)
        controls: list[dict[str, Any]] = []
        freeze = leaf / "cgroup.freeze"
        if freeze.is_file():
            try:
                freeze.write_text("1", encoding="utf-8")
                controls.append({"control": "cgroup.freeze", "ok": True})
            except Exception as exc:
                controls.append({
                    "control": "cgroup.freeze",
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
        try:
            (leaf / "cgroup.kill").write_text("1", encoding="utf-8")
            controls.append({"control": "cgroup.kill", "ok": True})
        except Exception as exc:
            controls.append({
                "control": "cgroup.kill",
                "ok": False,
                "error": f"{exc.__class__.__name__}: {exc}",
            })
            raise CgroupVerificationError(
                f"managed cgroup leaf kill failed for {role_name}: {exc}"
            ) from exc
        deadline = time.monotonic() + self.stop_timeout
        after = self.managed_leaf_state(role_name)
        while time.monotonic() < deadline and (
            after["pids"] or after["populated"] != 0
        ):
            time.sleep(self.poll_interval)
            after = self.managed_leaf_state(role_name)
        ok = bool(
            not after["pids"]
            and after["populated"] == 0
            and after["consistent"] is True
            and all(row.get("ok") for row in controls)
        )
        evidence = self._record(
            "kill_managed_leaf",
            role=role_name,
            before=before,
            after=after,
            controls=controls,
            ok=ok,
        )
        if not ok:
            raise CgroupVerificationError(
                f"managed cgroup leaf is not empty after kill for {role_name}"
            )
        return evidence

    def wrap_command(
        self,
        command: Sequence[str],
        *,
        role: str,
        managed_leaf: str | None = None,
        sandbox_allow_write_roots: Sequence[Path] | None = None,
        sandbox_proof_fd: int | None = None,
        sandbox_nonce: str | None = None,
    ) -> list[str]:
        """Return an argv that enters the scope before execing ``command``."""

        if not self.created or self.stopped or not self.scope_path:
            raise CampaignCgroupError("cannot wrap a command without an active verified campaign scope")
        role_name = str(role).strip().lower()
        if not _ROLE_RE.fullmatch(role_name):
            raise ValueError(f"invalid campaign process role: {role!r}")
        argv = [str(value) for value in command]
        if not argv:
            raise ValueError("managed command cannot be empty")
        target_scope = self.scope_path
        leaf_role: str | None = None
        if managed_leaf is not None:
            leaf_role = str(managed_leaf).strip().lower()
            self._managed_leaf_path(leaf_role)
            target_scope = str(
                self.managed_leaves[leaf_role].get("cgroup_path") or ""
            )
        wrapper = [
            self.python_executable,
            str(self.module_path),
            "_exec",
            "--cgroup-root",
            str(self.cgroup_root),
            "--proc-root",
            str(self.proc_root),
            "--scope-path",
            target_scope,
            "--role",
            role_name,
            "--evidence-dir",
            str(self.entry_evidence_dir),
        ]
        sandbox_requested = any((
            sandbox_allow_write_roots is not None,
            sandbox_proof_fd is not None,
            sandbox_nonce is not None,
        ))
        if sandbox_requested:
            if (
                role_name != "comfyui"
                or leaf_role != role_name
                or not sandbox_allow_write_roots
                or not isinstance(sandbox_proof_fd, int)
                or isinstance(sandbox_proof_fd, bool)
                or sandbox_proof_fd < 3
                or not isinstance(sandbox_nonce, str)
                or not re.fullmatch(r"[0-9a-f]{32}", sandbox_nonce)
            ):
                raise ValueError(
                    "ComfyUI sandbox wrapper requires its managed leaf, write roots, "
                    "proof fd, and 128-bit hexadecimal nonce"
                )
            wrapper.extend([
                "--sandbox-proof-fd",
                str(sandbox_proof_fd),
                "--sandbox-nonce",
                sandbox_nonce,
            ])
            for path in sandbox_allow_write_roots:
                wrapper.extend(["--sandbox-allow-write", str(path)])
        return [*wrapper, "--", *argv]

    def _assert_pinned_scope_identity(self) -> dict[str, Any]:
        identity = self.scope_identity
        if identity is None:
            raise CgroupVerificationError("campaign cgroup directory identity was not pinned")
        if identity.unit_name != self.unit_name or identity.cgroup_path != self.scope_path:
            raise CgroupVerificationError("campaign cgroup identity contract changed")
        scope = self._scope_fs_path()
        try:
            metadata = scope.lstat()
        except Exception as exc:
            raise CgroupVerificationError(f"cannot revalidate campaign cgroup identity: {exc}") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or int(metadata.st_dev) != identity.device
            or int(metadata.st_ino) != identity.inode
        ):
            raise CgroupVerificationError("campaign cgroup directory device/inode identity changed")
        properties = self._query_unit_properties()
        if properties:
            if properties.get("Id") != identity.unit_name:
                raise CgroupVerificationError("systemd unit Id changed before cgroup action")
            if properties.get("InvocationID") != identity.invocation_id:
                raise CgroupVerificationError("systemd InvocationID changed before cgroup action")
            control_group = _normalise_cgroup_path(
                properties.get("ControlGroup", ""), label="systemd ControlGroup"
            )
            if control_group != identity.cgroup_path:
                raise CgroupVerificationError("systemd ControlGroup changed before cgroup action")
            self._validated_systemd_io_weight(properties)
        return {
            "unit_name": identity.unit_name,
            "invocation_id": identity.invocation_id,
            "path": identity.cgroup_path,
            "device": identity.device,
            "inode": identity.inode,
            "active_state": properties.get("ActiveState") if properties else "not_found",
            "io_weight": (
                self._validated_systemd_io_weight(properties)
                if properties
                else None
            ),
            "ok": True,
        }

    def force_kill_scope(self) -> dict[str, Any]:
        """Use the pinned cgroup.kill control and prove recursive emptiness."""

        identity = self._assert_pinned_scope_identity()
        scope = self._scope_fs_path()
        actions: list[dict[str, Any]] = []
        for filename in ("cgroup.freeze", "cgroup.kill"):
            path = scope / filename
            descriptor = -1
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                written = os.write(descriptor, b"1")
                if written != 1:
                    raise OSError(f"short write: {written}")
                actions.append({"control": filename, "written": written, "ok": True})
            except Exception as exc:
                actions.append({
                    "control": filename,
                    "written": 0,
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
                if filename == "cgroup.kill":
                    raise CgroupVerificationError(f"pinned cgroup.kill failed: {exc}") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        deadline = time.monotonic() + self.stop_timeout
        while time.monotonic() < deadline and not self._scope_empty():
            time.sleep(self.poll_interval)
        empty = self._scope_empty()
        terminal_population = self._scope_terminal_population()
        result = {
            "sample_schema_version": CGROUP_SCHEMA_VERSION,
            "identity": identity,
            "actions": actions,
            "cgroup_empty": empty,
            "terminal_population": terminal_population,
            "ok": (
                empty
                and terminal_population.get("ok") is True
                and all(row.get("ok") for row in actions)
            ),
        }
        self._record("force_kill_scope", **result)
        if not result["ok"]:
            raise CgroupVerificationError(
                f"pinned cgroup.kill did not empty campaign scope: cgroup_empty={empty}"
            )
        return result

    def emergency_kill_scope_without_durable_evidence(self) -> dict[str, Any]:
        """Kill the pinned scope without invoking systemd or artifact writers.

        This narrow path is reserved for a host-I/O hard-limit trip.  Writes
        are limited to cgroup-v2 kernel control files; no evidence file,
        subprocess, journal query, or filesystem fsync is performed.
        """

        if not self.scope_path:
            return {
                "ok": True,
                "stopped": True,
                "not_created": True,
                "durable_writes_performed": False,
            }
        identity = self.scope_identity
        if identity is None:
            raise CgroupVerificationError(
                "campaign cgroup directory identity was not pinned"
            )
        if (
            identity.unit_name != self.unit_name
            or identity.cgroup_path != self.scope_path
        ):
            raise CgroupVerificationError(
                "campaign cgroup identity contract changed"
            )
        scope = self._scope_fs_path()
        metadata = scope.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or int(metadata.st_dev) != identity.device
            or int(metadata.st_ino) != identity.inode
        ):
            raise CgroupVerificationError(
                "campaign cgroup directory device/inode identity changed"
            )
        actions: list[dict[str, Any]] = []
        for filename in ("cgroup.freeze", "cgroup.kill"):
            descriptor = -1
            try:
                descriptor = os.open(
                    scope / filename,
                    os.O_WRONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                written = os.write(descriptor, b"1")
                actions.append({
                    "control": filename,
                    "written": written,
                    "ok": written == 1,
                })
            except Exception as exc:
                actions.append({
                    "control": filename,
                    "written": 0,
                    "ok": False,
                    "error_code": exc.__class__.__name__,
                })
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        deadline = time.monotonic() + min(2.0, max(0.1, self.stop_timeout))
        while time.monotonic() < deadline and not self._scope_empty():
            time.sleep(min(self.poll_interval, 0.1))
        empty = self._scope_empty()
        kill_succeeded = any(
            row.get("control") == "cgroup.kill" and row.get("ok") is True
            for row in actions
        )
        return {
            "ok": bool(empty and kill_succeeded),
            "stopped": bool(empty),
            "cgroup_empty": bool(empty),
            "actions": actions,
            "identity_revalidated": True,
            "durable_writes_performed": False,
        }

    def _scope_empty(self) -> bool:
        try:
            scope = self._scope_fs_path()
        except CgroupVerificationError:
            return False
        if not scope.exists():
            return True
        try:
            for procs in scope.rglob("cgroup.procs"):
                if any(row.strip() for row in procs.read_text(encoding="utf-8").splitlines()):
                    return False
            return True
        except Exception:
            return False

    def _scope_terminal_population(self) -> dict[str, Any]:
        """Prove terminal population from cgroup.events or scope removal."""

        scope = self._scope_fs_path()
        if not scope.exists():
            return {
                "path": self.scope_path,
                "scope_path_absent": True,
                "populated": 0,
                "ok": True,
            }
        try:
            events = self._parse_counter_file(scope / "cgroup.events")
            populated = int(events.get("populated", -1))
        except Exception as exc:
            return {
                "path": self.scope_path,
                "scope_path_absent": False,
                "populated": -1,
                "error": f"{exc.__class__.__name__}: {exc}",
                "ok": False,
            }
        return {
            "path": self.scope_path,
            "scope_path_absent": False,
            "populated": populated,
            "events": events,
            "ok": populated == 0,
        }

    def _best_effort_stop(self) -> dict[str, Any]:
        details: dict[str, Any] = {"unit_name": self.unit_name, "ok": False}
        process = self.anchor_process
        # Never stop a same-named pre-existing unit when setup failed before
        # systemd-run was invoked.  Once popen succeeded, stopping this unique
        # transient unit is the safest fail-closed cleanup.
        if process is not None:
            try:
                completed = self._systemctl("stop", self.unit_name)
                details.update({
                    "systemctl_returncode": completed.returncode,
                    "systemctl_stdout": str(completed.stdout or "")[-1000:],
                    "systemctl_stderr": str(completed.stderr or "")[-1000:],
                })
            except Exception as exc:
                details["systemctl_error"] = f"{exc.__class__.__name__}: {exc}"
        if self.scope_path:
            deadline = time.monotonic() + self.stop_timeout
            while time.monotonic() < deadline and not self._scope_empty():
                time.sleep(self.poll_interval)
            details["cgroup_empty_after_systemctl"] = self._scope_empty()
            if not details["cgroup_empty_after_systemctl"] and self.scope_identity is not None:
                try:
                    details["pinned_kill_fallback"] = self.force_kill_scope()
                except Exception as exc:
                    details["pinned_kill_fallback"] = {
                        "ok": False,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        scope_safe = not self.scope_path or bool(
            details.get("cgroup_empty_after_systemctl")
            or (details.get("pinned_kill_fallback") or {}).get("ok")
        )
        details["ok"] = bool(
            (process is None or details.get("systemctl_returncode") == 0 or scope_safe)
            and scope_safe
        )
        return details

    def stop_scope(self) -> dict[str, Any]:
        """Stop the transient unit and prove no process remains in its cgroup."""

        if not self.scope_path:
            raise CgroupVerificationError("cannot stop an unverified campaign scope")
        scope_path = self._scope_fs_path()
        identity_evidence = (
            self._assert_pinned_scope_identity()
            if scope_path.exists()
            else {
                "unit_name": self.unit_name,
                "path": self.scope_path,
                "scope_already_gone": True,
                "ok": True,
            }
        )
        # Stop the systemd scope directly.  Signalling the anchor through its
        # sentinel first could let the transient unit disappear before
        # systemctl addresses it, losing authoritative whole-cgroup teardown.
        completed = self._systemctl("stop", self.unit_name)
        deadline = time.monotonic() + self.stop_timeout
        while time.monotonic() < deadline and not self._scope_empty():
            time.sleep(self.poll_interval)
        empty = self._scope_empty()
        fallback: dict[str, Any] = {}
        if (completed.returncode not in {0, 5} or not empty) and scope_path.exists():
            try:
                fallback = self.force_kill_scope()
            except Exception as exc:
                fallback = {
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            empty = self._scope_empty()
        terminal_population = self._scope_terminal_population()
        process = self.anchor_process
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=min(2.0, self.stop_timeout))
            except Exception:
                try:
                    process.terminate()
                except Exception:
                    pass
        unit_already_gone = completed.returncode == 5 and empty and (
            self.anchor_process is None or self.anchor_process.poll() is not None
        )
        ok = empty and terminal_population.get("ok") is True and (
            completed.returncode == 0
            or unit_already_gone
            or fallback.get("ok") is True
        )
        self.stopped = True
        event = self._record(
            "stop_scope",
            ok=ok,
            systemctl_returncode=completed.returncode,
            systemctl_stdout=str(completed.stdout or "")[-1000:],
            systemctl_stderr=str(completed.stderr or "")[-1000:],
            cgroup_empty=empty,
            terminal_population=terminal_population,
            unit_already_gone=unit_already_gone,
            identity=identity_evidence,
            pinned_kill_fallback=fallback,
        )
        if not ok:
            raise CgroupVerificationError(
                f"campaign scope shutdown is not proven: systemctl={completed.returncode}, cgroup_empty={empty}"
            )
        return event


def _anchor_main(args: argparse.Namespace) -> int:
    pid_file = Path(args.pid_file).expanduser().resolve(strict=False)
    ready_file = Path(args.ready_file).expanduser().resolve(strict=False)
    stop_file = Path(args.stop_file).expanduser().resolve(strict=False)
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    _atomic_write_json(ready_file, {"pid": os.getpid(), "ready_at": utc_now(), "ok": True})
    activation_gate = Path(args.activation_gate).expanduser().resolve(strict=False) if args.activation_gate else None
    while not stop_event.is_set() and not stop_file.exists() and not (activation_gate and activation_gate.exists()):
        stop_event.wait(0.5)
    if activation_gate and activation_gate.exists() and not stop_event.is_set() and not stop_file.exists():
        try:
            payload = json.loads(Path(args.command_file).read_text(encoding="utf-8"))
            command = [str(value) for value in payload.get("command") or []]
            if not command:
                raise ValueError("managed command is empty")
            cwd = Path(args.cwd).expanduser().resolve(strict=True)
            stdout_path = Path(args.stdout).expanduser().resolve(strict=False)
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.dup2(descriptor, 1)
            os.dup2(descriptor, 2)
            if descriptor > 2:
                os.close(descriptor)
            env = os.environ.copy()
            environment = payload.get("environment") or {}
            if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()):
                raise ValueError("managed environment must be a string map")
            env.update(environment)
            env["HACKME_CAMPAIGN_CGROUP_PATH"] = _read_pid_cgroup(Path("/proc"), os.getpid())
            os.chdir(cwd)
            os.execvpe(command[0], command, env)
        except Exception as exc:
            print(
                f"campaign cgroup anchor refused managed exec: {exc.__class__.__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return EXEC_FAILURE
    return 0


def _exec_main(args: argparse.Namespace) -> int:
    command = [str(value) for value in (args.command or [])]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("campaign cgroup exec wrapper: missing command", file=sys.stderr)
        return EXEC_FAILURE
    role = str(args.role).strip().lower()
    if not _ROLE_RE.fullmatch(role):
        print(f"campaign cgroup exec wrapper: invalid role {role!r}", file=sys.stderr)
        return EXEC_FAILURE
    sandbox_proof_fd = getattr(args, "sandbox_proof_fd", None)
    sandbox_nonce = getattr(args, "sandbox_nonce", None)
    sandbox_allow_write = [
        Path(value) for value in (getattr(args, "sandbox_allow_write", None) or [])
    ]
    sandbox_requested = any((
        sandbox_proof_fd is not None,
        sandbox_nonce is not None,
        bool(sandbox_allow_write),
    ))
    cgroup_root = Path(args.cgroup_root).expanduser().resolve(strict=False)
    proc_root = Path(args.proc_root).expanduser().resolve(strict=False)
    try:
        scope_path = _normalise_cgroup_path(args.scope_path, label="campaign cgroup")
        scope_fs = (cgroup_root / scope_path.lstrip("/")).resolve(strict=False)
        if scope_fs == cgroup_root or cgroup_root not in scope_fs.parents:
            raise CgroupVerificationError("campaign scope escapes cgroup root")
        if not (cgroup_root / "cgroup.controllers").is_file():
            raise CgroupUnavailableError("cgroup v2 controller file is unavailable")
        pid = os.getpid()
        cgroup_write_target = scope_fs / "cgroup.procs"
        try:
            cgroup_write_target.write_text(str(pid), encoding="utf-8")
        except Exception as exc:
            raise CgroupVerificationError(
                f"cannot move {role} pid {pid} into {cgroup_write_target}: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        placement = _assert_pid_placement(
            cgroup_root=cgroup_root,
            proc_root=proc_root,
            scope_path=scope_path,
            pid=pid,
            role=role,
            expected_inside=True,
        )
        host_transition: dict[str, Any] | None = None
        sandbox_argv: list[str] | None = None
        sandbox_python = ""
        if sandbox_requested:
            if (
                role != "comfyui"
                or sandbox_proof_fd is None
                or isinstance(sandbox_proof_fd, bool)
                or int(sandbox_proof_fd) < 3
                or not isinstance(sandbox_nonce, str)
                or not re.fullmatch(r"[0-9a-f]{32}", sandbox_nonce)
                or not sandbox_allow_write
                or scope_path.rsplit("/", 1)[-1] != role
            ):
                raise CgroupVerificationError(
                    "ComfyUI sandbox arguments are incomplete or do not target its exact leaf"
                )
            if (
                cgroup_root != DEFAULT_CGROUP_ROOT.resolve(strict=True)
                or proc_root != DEFAULT_PROC_ROOT.resolve(strict=True)
            ):
                raise CgroupVerificationError(
                    "ComfyUI host transition requires the native cgroupfs and procfs roots"
                )
            if placement.get("actual_cgroup") != scope_path:
                raise CgroupVerificationError(
                    "ComfyUI sandbox launcher is not in the exact managed leaf"
                )
            host_transition = _build_host_transition_payload(
                nonce=sandbox_nonce,
                pid=pid,
                scope_path=scope_path,
                scope_fs=scope_fs,
                placement=placement,
                allowed_write_roots=sandbox_allow_write,
            )
            encoded_transition = json.dumps(
                host_transition,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(encoded_transition.encode("utf-8")) > MAX_HOST_TRANSITION_JSON_BYTES:
                raise CgroupVerificationError(
                    "ComfyUI host transition receipt exceeds the sandbox bound"
                )
            sandbox_module = Path(__file__).with_name(
                COMFYUI_SANDBOX_MODULE_NAME
            ).resolve(strict=True)
            module_metadata = sandbox_module.lstat()
            if (
                stat.S_ISLNK(module_metadata.st_mode)
                or not stat.S_ISREG(module_metadata.st_mode)
            ):
                raise CgroupVerificationError(
                    "ComfyUI sandbox module is not a canonical regular file"
                )
            sandbox_python_path = Path(sys.executable).resolve(strict=True)
            python_metadata = sandbox_python_path.lstat()
            if (
                stat.S_ISLNK(python_metadata.st_mode)
                or not stat.S_ISREG(python_metadata.st_mode)
                or not python_metadata.st_mode & 0o111
            ):
                raise CgroupVerificationError(
                    "ComfyUI sandbox interpreter is not a canonical executable"
                )
            sandbox_cwd = Path.cwd()
            if sandbox_cwd != sandbox_cwd.resolve(strict=True):
                raise CgroupVerificationError(
                    "ComfyUI sandbox working directory is not canonical"
                )
            sandbox_python = str(sandbox_python_path)
            sandbox_argv = [
                sandbox_python,
                str(sandbox_module),
                "--host-transition-json",
                encoded_transition,
                "--nonce",
                sandbox_nonce,
                "--expected-cgroup-path",
                scope_path,
            ]
            for path in sandbox_allow_write:
                sandbox_argv.extend(["--allow-write-root", str(path)])
            sandbox_argv.extend([
                "--cwd",
                str(sandbox_cwd),
                "--proof-fd",
                str(int(sandbox_proof_fd)),
                "--",
                *command,
            ])
        evidence_dir = Path(args.evidence_dir).expanduser().resolve(strict=False)
        entry_evidence: dict[str, Any] = {
            "sample_schema_version": CGROUP_SCHEMA_VERSION,
            "entered_at": utc_now(),
            "placement": placement,
            "command_executable": command[0],
            "ok": True,
        }
        if host_transition is not None:
            entry_evidence["host_transition"] = host_transition
        _atomic_write_json(
            evidence_dir / f"{role}_{pid}.json",
            entry_evidence,
        )
        if sandbox_argv is not None:
            os.execve(sandbox_python, sandbox_argv, os.environ.copy())
        os.execvpe(command[0], command, os.environ.copy())
    except Exception as exc:
        print(f"campaign cgroup exec wrapper refused to launch {role}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return EXEC_FAILURE
    return EXEC_FAILURE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal campaign cgroup v2 helpers")
    subparsers = parser.add_subparsers(dest="action", required=True)
    anchor = subparsers.add_parser("_anchor")
    anchor.add_argument("--pid-file", required=True)
    anchor.add_argument("--ready-file", required=True)
    anchor.add_argument("--stop-file", required=True)
    anchor.add_argument("--command-file")
    anchor.add_argument("--activation-gate")
    anchor.add_argument("--cwd")
    anchor.add_argument("--stdout")
    execute = subparsers.add_parser("_exec")
    execute.add_argument("--cgroup-root", default=str(DEFAULT_CGROUP_ROOT))
    execute.add_argument("--proc-root", default=str(DEFAULT_PROC_ROOT))
    execute.add_argument("--scope-path", required=True)
    execute.add_argument("--role", required=True)
    execute.add_argument("--evidence-dir", required=True)
    execute.add_argument("--sandbox-proof-fd", type=int)
    execute.add_argument("--sandbox-nonce")
    execute.add_argument("--sandbox-allow-write", action="append", default=[])
    execute.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "_anchor":
        return _anchor_main(args)
    if args.action == "_exec":
        return _exec_main(args)
    return EXEC_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
