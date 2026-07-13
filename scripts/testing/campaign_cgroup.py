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
CGROUP_SCHEMA_VERSION = "hackme.campaign-cgroup/v1"
DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")
DEFAULT_PROC_ROOT = Path("/proc")
EXEC_FAILURE = 125
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

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCOPE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class CampaignCgroupError(RuntimeError):
    """Base error for cgroup setup or verification failures."""


class CgroupUnavailableError(CampaignCgroupError):
    """Raised when a real, delegated cgroup v2 scope cannot be established."""


class CgroupVerificationError(CampaignCgroupError):
    """Raised when a limit or process placement cannot be proven."""


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

    memory_high_bytes: int = 7 * GIB
    memory_max_bytes: int = 8 * GIB
    memory_swap_max_bytes: int = 1 * GIB
    cpu_quota_percent: int = 600
    tasks_max: int = 768

    def __post_init__(self) -> None:
        values = {
            "memory_high_bytes": self.memory_high_bytes,
            "memory_max_bytes": self.memory_max_bytes,
            "memory_swap_max_bytes": self.memory_swap_max_bytes,
            "cpu_quota_percent": self.cpu_quota_percent,
            "tasks_max": self.tasks_max,
        }
        invalid = [name for name, value in values.items() if isinstance(value, bool) or int(value) <= 0]
        if invalid:
            raise ValueError("cgroup limits must be positive integers: " + ", ".join(invalid))
        if self.memory_high_bytes > self.memory_max_bytes:
            raise ValueError("memory_high_bytes cannot exceed memory_max_bytes")

    def systemd_properties(self) -> tuple[str, ...]:
        return (
            "Delegate=yes",
            f"MemoryHigh={self.memory_high_bytes}",
            f"MemoryMax={self.memory_max_bytes}",
            f"MemorySwapMax={self.memory_swap_max_bytes}",
            f"CPUQuota={self.cpu_quota_percent}%",
            f"TasksMax={self.tasks_max}",
        )

    def expected_files(self) -> dict[str, int]:
        return {
            "memory.high": self.memory_high_bytes,
            "memory.max": self.memory_max_bytes,
            "memory.swap.max": self.memory_swap_max_bytes,
            "pids.max": self.tasks_max,
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
            },
            "registered_pids": {role: sorted(pids) for role, pids in sorted(self.registered_pids.items())},
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
        required = {"cpu", "memory", "pids"}
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
        command.extend([
            "--",
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
            _atomic_write_json(self.managed_command_file, {
                "schema_version": CGROUP_SCHEMA_VERSION,
                "command": list(self.managed_command),
                "cwd": str(self.managed_cwd),
                "stdout": str(self.managed_stdout),
                "environment": self.managed_environment,
            })
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
            while time.monotonic() < deadline:
                if self.anchor_process.poll() is not None:
                    raise CgroupUnavailableError(
                        f"systemd-run exited before the campaign scope became ready: returncode={self.anchor_process.poll()}"
                    )
                if not self.scope_path:
                    self.scope_path = self._control_group()
                if self.scope_path and self.anchor_pid_file.exists() and self.anchor_ready_file.exists():
                    break
                time.sleep(self.poll_interval)
            if not self.scope_path:
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

    def verify_limits(self) -> dict[str, Any]:
        """Read actual cgroup v2 files and prove every configured limit."""

        if not self.scope_path:
            raise CgroupVerificationError("campaign cgroup path is not known")
        scope = self._scope_fs_path()
        checks: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for filename, expected in self.limits.expected_files().items():
            path = scope / filename
            try:
                raw = path.read_text(encoding="utf-8").strip()
                actual = int(raw)
                ok = actual == expected
                checks[filename] = {"expected": expected, "actual": actual, "raw": raw, "ok": ok}
                if not ok:
                    errors.append(f"{filename}: expected {expected}, got {raw}")
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

    def wrap_command(self, command: Sequence[str], *, role: str) -> list[str]:
        """Return an argv that enters the scope before execing ``command``."""

        if not self.created or self.stopped or not self.scope_path:
            raise CampaignCgroupError("cannot wrap a command without an active verified campaign scope")
        role_name = str(role).strip().lower()
        if not _ROLE_RE.fullmatch(role_name):
            raise ValueError(f"invalid campaign process role: {role!r}")
        argv = [str(value) for value in command]
        if not argv:
            raise ValueError("managed command cannot be empty")
        return [
            self.python_executable,
            str(self.module_path),
            "_exec",
            "--cgroup-root",
            str(self.cgroup_root),
            "--proc-root",
            str(self.proc_root),
            "--scope-path",
            self.scope_path,
            "--role",
            role_name,
            "--evidence-dir",
            str(self.entry_evidence_dir),
            "--",
            *argv,
        ]

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
        return {
            "unit_name": identity.unit_name,
            "invocation_id": identity.invocation_id,
            "path": identity.cgroup_path,
            "device": identity.device,
            "inode": identity.inode,
            "active_state": properties.get("ActiveState") if properties else "not_found",
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
        result = {
            "sample_schema_version": CGROUP_SCHEMA_VERSION,
            "identity": identity,
            "actions": actions,
            "cgroup_empty": empty,
            "ok": empty and all(row.get("ok") for row in actions),
        }
        self._record("force_kill_scope", **result)
        if not result["ok"]:
            raise CgroupVerificationError(
                f"pinned cgroup.kill did not empty campaign scope: cgroup_empty={empty}"
            )
        return result

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
        ok = empty and (
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
        try:
            (scope_fs / "cgroup.procs").write_text(str(pid), encoding="utf-8")
        except Exception as exc:
            raise CgroupVerificationError(
                f"cannot move {role} pid {pid} into {scope_fs / 'cgroup.procs'}: "
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
        evidence_dir = Path(args.evidence_dir).expanduser().resolve(strict=False)
        _atomic_write_json(
            evidence_dir / f"{role}_{pid}.json",
            {
                "sample_schema_version": CGROUP_SCHEMA_VERSION,
                "entered_at": utc_now(),
                "placement": placement,
                "command_executable": command[0],
                "ok": True,
            },
        )
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
