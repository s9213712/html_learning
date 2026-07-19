#!/usr/bin/env python3
"""Independent fail-closed watchdog for a managed operational campaign.

This program is intentionally a separate OS process.  It shares no Python
objects with the campaign orchestrator: the only live inputs are a durable
heartbeat/state JSON file, a checkpoint JSON file, a PID identity, and an
exact cgroup-v2 identity.  When the orchestrator disappears or its heartbeat
is stale, the watchdog closes load admission, freezes the formal timer,
captures a small secret-safe evidence bundle, and kills the managed campaign
cgroup.  The watchdog must itself remain outside that cgroup.

The module does not import ``campaign_state``.  It follows that file's v1 JSON
and ``<state>.lock`` contract so either component can be tested or deployed
independently and both can safely update the same state file.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.campaign_control_channel import (
    ControlChannelError,
    PeerIdentity,
    derive_runner_auth_key,
    send_hello,
    sign_authenticated_payload,
    verify_authenticated_payload,
)


STATE_SCHEMA_VERSION = "hackme.campaign-state.v1"
CONTROL_SCHEMA_VERSION = "hackme.campaign-control.v1"
WATCHDOG_SCHEMA_VERSION = "hackme.campaign-watchdog.v1"
WATCHDOG_LIVENESS_SCHEMA_VERSION = "hackme.campaign-watchdog-liveness.v1"
DEFAULT_STALE_SECONDS = 120.0
INCIDENT_EXIT_CODE = 10
DURABLE_HEARTBEAT_INTERVAL_SECONDS = 5.0
MAX_EVIDENCE_FILE_BYTES = 16 * 1024 * 1024


class WatchdogError(RuntimeError):
    """The watchdog cannot prove that continuing is safe."""


class ProcessIdentityError(WatchdogError):
    """A PID no longer represents the process captured before launch."""


class CgroupIdentityError(WatchdogError):
    """The managed cgroup cannot be safely identified or controlled."""


class DuplicateWatchdogError(WatchdogError):
    """Another external watchdog already owns this campaign."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Commit private JSON with fsync + atomic replace."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        content = _json_bytes(payload)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while committing watchdog JSON")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise WatchdogError(f"cannot read {path}: {exc.__class__.__name__}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WatchdogError(f"JSON object required: {path}")
    return payload


def sha256_file(path: Path) -> str:
    size = Path(path).stat().st_size
    if size > MAX_EVIDENCE_FILE_BYTES:
        raise WatchdogError(f"evidence input exceeds {MAX_EVIDENCE_FILE_BYTES} bytes: {path}")
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _proc_stat_fields(text: str) -> tuple[str, list[str]]:
    """Parse /proc/PID/stat even when the command contains spaces or ')'."""

    try:
        prefix, suffix = text.rstrip().rsplit(") ", 1)
        command = prefix.split("(", 1)[1]
        fields = suffix.split()
    except Exception as exc:
        raise ProcessIdentityError(f"malformed proc stat: {exc.__class__.__name__}: {exc}") from exc
    if len(fields) <= 19:
        raise ProcessIdentityError("malformed proc stat: starttime field is missing")
    return command, fields


def parse_unified_cgroup(text: str) -> str:
    """Return a normalized cgroup-v2 path from /proc/PID/cgroup."""

    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            value = "/" + parts[2].strip().lstrip("/")
            return value.rstrip("/") or "/"
    raise ProcessIdentityError("unified cgroup-v2 membership is missing")


def normalize_cgroup_path(value: str) -> str:
    path = "/" + str(value or "").strip().lstrip("/")
    normalized = os.path.normpath(path)
    if normalized == "/" or ".." in Path(normalized).parts:
        raise CgroupIdentityError("campaign cgroup must be a non-root cgroup-v2 scope")
    return normalized


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    boot_id: str
    cgroup_path: str
    state: str


def capture_process_identity(pid: int, *, proc_root: Path = Path("/proc")) -> ProcessIdentity:
    pid = int(pid)
    if pid <= 1:
        raise ProcessIdentityError(f"unsafe or invalid process id: {pid}")
    process_root = Path(proc_root) / str(pid)
    try:
        stat_path = process_root / "stat"
        _command, fields = _proc_stat_fields(stat_path.read_text(encoding="utf-8"))
        state = str(fields[0])
        start_ticks = int(fields[19])
        cgroup_path = parse_unified_cgroup((process_root / "cgroup").read_text(encoding="utf-8"))
        boot_id = (Path(proc_root) / "sys" / "kernel" / "random" / "boot_id").read_text(encoding="ascii").strip()
        # Close the PID-reuse race between reading stat and cgroup membership.
        _command_after, fields_after = _proc_stat_fields(stat_path.read_text(encoding="utf-8"))
        if int(fields_after[19]) != start_ticks:
            raise ProcessIdentityError(f"process {pid} identity changed during inspection")
    except FileNotFoundError as exc:
        raise ProcessIdentityError(f"process {pid} does not exist") from exc
    except ProcessIdentityError:
        raise
    except Exception as exc:
        raise ProcessIdentityError(f"cannot capture process {pid}: {exc.__class__.__name__}: {exc}") from exc
    if state in {"Z", "X", "x"}:
        raise ProcessIdentityError(f"process {pid} is not alive (state={state})")
    if not boot_id:
        raise ProcessIdentityError("kernel boot identity is empty")
    return ProcessIdentity(pid, start_ticks, boot_id, cgroup_path, state)


def verify_process_identity(
    *,
    pid: int,
    expected_start_ticks: int,
    expected_boot_id: str,
    expected_cgroup_path: str,
    proc_root: Path = Path("/proc"),
) -> ProcessIdentity:
    actual = capture_process_identity(pid, proc_root=proc_root)
    failures: list[str] = []
    if actual.start_ticks != int(expected_start_ticks):
        failures.append(f"start_ticks={actual.start_ticks} expected={expected_start_ticks}")
    if actual.boot_id != str(expected_boot_id):
        failures.append("boot_id_changed")
    expected_cgroup = "/" + str(expected_cgroup_path).strip().lstrip("/")
    expected_cgroup = expected_cgroup.rstrip("/") or "/"
    if actual.cgroup_path != expected_cgroup:
        failures.append(f"cgroup={actual.cgroup_path} expected={expected_cgroup}")
    if failures:
        raise ProcessIdentityError("orchestrator identity mismatch: " + ", ".join(failures))
    return actual


@dataclass(frozen=True)
class CgroupIdentity:
    path: str
    device: int
    inode: int


def capture_cgroup_identity(
    cgroup_path: str,
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> CgroupIdentity:
    normalized = normalize_cgroup_path(cgroup_path)
    root = Path(cgroup_root).resolve(strict=True)
    target = (root / normalized.lstrip("/")).resolve(strict=True)
    if root not in target.parents:
        raise CgroupIdentityError(f"campaign cgroup escapes cgroup root: {target}")
    metadata = target.stat()
    return CgroupIdentity(normalized, int(metadata.st_dev), int(metadata.st_ino))


def _is_inside_cgroup(*, member: str, target: str) -> bool:
    member = "/" + member.strip().lstrip("/")
    member = member.rstrip("/") or "/"
    target = normalize_cgroup_path(target)
    return member == target or member.startswith(target + "/")


class CgroupHandle:
    """Pinned handles for one exact cgroup-v2 directory.

    File descriptors are opened during startup and kept until the incident.
    A later path replacement therefore cannot redirect a kill to another
    cgroup.  Directory device/inode identity is also rechecked before action.
    """

    def __init__(
        self,
        *,
        identity: CgroupIdentity,
        cgroup_root: Path,
        require_scope_suffix: bool,
    ) -> None:
        self.identity = identity
        self.root = Path(cgroup_root).resolve(strict=True)
        self.target = (self.root / identity.path.lstrip("/")).resolve(strict=True)
        if self.root not in self.target.parents:
            raise CgroupIdentityError("campaign cgroup target escapes cgroup root")
        if require_scope_suffix and not self.target.name.endswith(".scope"):
            raise CgroupIdentityError("production campaign cgroup must be a dedicated .scope")
        self._verify_directory_identity()
        required = ("cgroup.kill", "cgroup.freeze", "cgroup.procs", "cgroup.events")
        missing = [name for name in required if not (self.target / name).is_file()]
        if missing:
            raise CgroupIdentityError("campaign cgroup controls missing: " + ", ".join(missing))
        try:
            self.kill_fd = os.open(self.target / "cgroup.kill", os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
            self.freeze_fd = os.open(self.target / "cgroup.freeze", os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
        except Exception as exc:
            for descriptor in (getattr(self, "kill_fd", -1), getattr(self, "freeze_fd", -1)):
                if descriptor >= 0:
                    os.close(descriptor)
            raise CgroupIdentityError(f"campaign cgroup controls are not writable: {exc.__class__.__name__}: {exc}") from exc

    def _verify_directory_identity(self) -> None:
        metadata = self.target.stat()
        if int(metadata.st_dev) != self.identity.device or int(metadata.st_ino) != self.identity.inode:
            raise CgroupIdentityError("campaign cgroup device/inode identity changed")

    @staticmethod
    def _write_control(descriptor: int, value: bytes) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        written = os.write(descriptor, value)
        if written != len(value):
            raise CgroupIdentityError("short write to cgroup control")

    def freeze(self) -> None:
        self._verify_directory_identity()
        self._write_control(self.freeze_fd, b"1")

    def kill(self) -> None:
        self._verify_directory_identity()
        self._write_control(self.kill_fd, b"1")

    def populated(self) -> bool:
        # systemd may reap an empty transient scope immediately after
        # cgroup.kill.  The directory disappearing after a verified kill write
        # is equivalent to population reaching zero.
        if not self.target.exists():
            return False
        try:
            events = (self.target / "cgroup.events").read_text(encoding="ascii")
            for line in events.splitlines():
                key, _, value = line.partition(" ")
                if key == "populated":
                    return value.strip() == "1"
            return bool((self.target / "cgroup.procs").read_text(encoding="ascii").strip())
        except Exception as exc:
            raise CgroupIdentityError(f"cannot verify cgroup population: {exc.__class__.__name__}: {exc}") from exc

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": self.identity.path,
            "device": self.identity.device,
            "inode": self.identity.inode,
        }
        for name in (
            "cgroup.events",
            "cgroup.freeze",
            "cgroup.procs",
            "memory.current",
            "memory.events",
            "memory.max",
            "memory.high",
            "memory.swap.max",
            "cpu.max",
            "pids.current",
            "pids.max",
        ):
            try:
                value = (self.target / name).read_text(encoding="ascii", errors="replace")[:65536]
                result[name] = value.strip()
            except Exception as exc:
                result[name] = {"collector_error": f"{exc.__class__.__name__}: {exc}"}
        return result

    def close(self) -> None:
        for name in ("kill_fd", "freeze_fd"):
            descriptor = getattr(self, name, -1)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)


@contextmanager
def locked_path(path: Path, *, nonblocking: bool = False) -> Iterator[None]:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(descriptor, operation)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class WatchdogPaths:
    campaign_root: Path
    state: Path
    control: Path
    heartbeat: Path
    checkpoint: Path
    ready: Path
    evidence: Path
    process_lock: Path
    liveness: Path | None = None


@dataclass(frozen=True)
class WatchdogConfig:
    campaign_uuid: str
    paths: WatchdogPaths
    orchestrator_pid: int
    orchestrator_start_ticks: int
    orchestrator_boot_id: str
    orchestrator_cgroup: str
    campaign_cgroup: CgroupIdentity
    proc_root: Path = Path("/proc")
    cgroup_root: Path = Path("/sys/fs/cgroup")
    stale_after_seconds: float = DEFAULT_STALE_SECONDS
    poll_seconds: float = 1.0
    kill_verify_seconds: float = 10.0
    production: bool = True
    auth_socket: Path | None = None
    supervisor_pid: int = 0
    supervisor_start_ticks: int = 0
    supervisor_boot_id: str = ""
    supervisor_cgroup: str = ""


def validate_runtime_path(path: Path, *, root: Path, label: str) -> Path:
    resolved_root = Path(root).resolve(strict=False)
    resolved = Path(path).resolve(strict=False)
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise WatchdogError(f"{label} must be below campaign root: {resolved}")
    tmp = Path("/tmp").resolve()
    if resolved_root == tmp or tmp not in resolved_root.parents:
        raise WatchdogError(f"campaign root must remain below /tmp: {resolved_root}")
    return resolved


def extract_heartbeat(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    heartbeat = payload.get("heartbeat")
    if isinstance(heartbeat, dict):
        return heartbeat
    return payload


def _state_lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


def _event(payload: dict[str, Any], *, state: str, reason: str, evidence: Any = None) -> None:
    item: dict[str, Any] = {"at": utc_now(), "state": state, "reason": reason}
    if evidence is not None:
        item["evidence"] = evidence
    events = payload.setdefault("events", [])
    if not isinstance(events, list):
        events = payload["events"] = []
    events.append(item)
    if len(events) > 500:
        del events[:-500]


class ExternalCampaignWatchdog:
    def __init__(
        self,
        config: WatchdogConfig,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.monotonic_ns = monotonic_ns
        self.sleep = sleep
        self.stop_requested = False
        self.signal_received = 0
        self.cgroup: CgroupHandle | None = None
        self.watchdog_identity: ProcessIdentity | None = None
        self.incident_id = ""
        self._process_lock_context: Any = None
        self.paths_validated = False
        self.terminal_state_seen = ""
        self.runner_auth_key: bytes | None = None
        self.watchdog_auth_key: bytes | None = None
        self.control_authentication_evidence: dict[str, Any] = {}
        self.authenticated_streams: dict[str, dict[str, Any]] = {}
        self.watchdog_liveness_sequence = 0
        self.last_watchdog_heartbeat_ns = 0

    def _liveness_path(self) -> Path:
        return self.config.paths.liveness or self.config.paths.ready.with_name(
            "watchdog.liveness.json"
        )

    def _validate_paths(self) -> None:
        paths = self.config.paths
        root = Path(paths.campaign_root).resolve(strict=False)
        resolved = {
            name: validate_runtime_path(path, root=root, label=name)
            for name, path in {
                "state path": paths.state,
                "control path": paths.control,
                "heartbeat path": paths.heartbeat,
                "checkpoint path": paths.checkpoint,
                "ready path": paths.ready,
                "liveness path": self._liveness_path(),
                "evidence path": paths.evidence,
                "process lock": paths.process_lock,
            }.items()
        }
        if resolved["state path"] == resolved["control path"]:
            raise WatchdogError("state and control paths must be distinct")
        for label in ("state path", "control path", "heartbeat path", "checkpoint path"):
            if resolved["ready path"] == resolved[label]:
                raise WatchdogError(f"ready path must not overwrite {label}")
            if resolved["liveness path"] == resolved[label]:
                raise WatchdogError(f"liveness path must not overwrite {label}")
        if resolved["ready path"] == resolved["liveness path"]:
            raise WatchdogError("ready and liveness paths must be distinct")
        self.paths_validated = True

    def _validate_configuration(self) -> None:
        self._validate_paths()
        if not self.config.campaign_uuid:
            raise WatchdogError("campaign UUID is required")
        if self.config.orchestrator_pid <= 1 or self.config.orchestrator_start_ticks <= 0:
            raise WatchdogError("positive orchestrator PID and starttime identity are required")
        if not self.config.orchestrator_boot_id or not self.config.orchestrator_cgroup:
            raise WatchdogError("orchestrator boot and cgroup identity are required")
        if self.config.campaign_cgroup.device <= 0 or self.config.campaign_cgroup.inode <= 0:
            raise WatchdogError("positive campaign cgroup device/inode identity is required")
        if self.config.production:
            if Path(self.config.proc_root).resolve() != Path("/proc"):
                raise WatchdogError("production watchdog must use the real /proc")
            if Path(self.config.cgroup_root).resolve() != Path("/sys/fs/cgroup"):
                raise WatchdogError("production watchdog must use the real cgroup-v2 filesystem")
            if float(self.config.stale_after_seconds) != DEFAULT_STALE_SECONDS:
                raise WatchdogError(f"production heartbeat timeout must be exactly {DEFAULT_STALE_SECONDS:g}s")
            if not (0.1 <= float(self.config.poll_seconds) <= 5.0):
                raise WatchdogError("production poll interval must be between 0.1s and 5s")
            if not (1.0 <= float(self.config.kill_verify_seconds) <= 30.0):
                raise WatchdogError("production cgroup kill verification must be between 1s and 30s")
            if (
                int(self.config.supervisor_pid) <= 1
                or int(self.config.supervisor_start_ticks) <= 0
                or not self.config.supervisor_boot_id
                or not self.config.supervisor_cgroup
            ):
                raise WatchdogError("production supervisor process identity is required")
        elif float(self.config.stale_after_seconds) < 0:
            raise WatchdogError("heartbeat timeout cannot be negative")
        if int(self.config.orchestrator_pid) == os.getpid():
            raise WatchdogError("watchdog must run as an external process, not inside the orchestrator")

    def validate_startup(self) -> dict[str, Any]:
        self._validate_configuration()
        if self.config.production and (
            self.runner_auth_key is None or self.watchdog_auth_key is None
        ):
            raise WatchdogError("production watchdog session authentication is unavailable")

        state = load_json(self.config.paths.state)
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise WatchdogError("campaign state schema is missing or unsupported")
        if str(state.get("campaign_uuid") or "") != self.config.campaign_uuid:
            raise WatchdogError("campaign state UUID mismatch")
        control = load_json(self.config.paths.control)
        heartbeat = load_json(self.config.paths.heartbeat)
        checkpoint = load_json(self.config.paths.checkpoint)
        if self.config.production:
            for label, payload in (("control", control), ("heartbeat", heartbeat), ("checkpoint", checkpoint)):
                if str(payload.get("campaign_uuid") or "") != self.config.campaign_uuid:
                    raise WatchdogError(f"{label} campaign UUID is missing or mismatched")

        self.watchdog_identity = capture_process_identity(os.getpid(), proc_root=self.config.proc_root)
        if _is_inside_cgroup(
            member=self.watchdog_identity.cgroup_path,
            target=self.config.campaign_cgroup.path,
        ):
            raise WatchdogError("watchdog is inside the managed campaign cgroup")
        self.cgroup = CgroupHandle(
            identity=self.config.campaign_cgroup,
            cgroup_root=self.config.cgroup_root,
            require_scope_suffix=self.config.production,
        )
        actual = capture_cgroup_identity(
            self.config.campaign_cgroup.path,
            cgroup_root=self.config.cgroup_root,
        )
        if actual != self.config.campaign_cgroup:
            raise CgroupIdentityError("campaign cgroup identity does not match startup contract")
        return {
            "schema_version": WATCHDOG_SCHEMA_VERSION,
            "campaign_uuid": self.config.campaign_uuid,
            "verified": True,
            "production": self.config.production,
            "external_process": os.getpid() != self.config.orchestrator_pid,
            "watchdog_pid": os.getpid(),
            "watchdog_start_ticks": self.watchdog_identity.start_ticks,
            "watchdog_boot_id": self.watchdog_identity.boot_id,
            "watchdog_cgroup": self.watchdog_identity.cgroup_path,
            "watchdog_outside_campaign_cgroup": True,
            "campaign_cgroup": {
                "path": actual.path,
                "device": actual.device,
                "inode": actual.inode,
                "kill_control_open": True,
                "freeze_control_open": True,
            },
            "stale_after_seconds": self.config.stale_after_seconds,
            "authenticated_control_channel": dict(self.control_authentication_evidence),
            "validated_at": utc_now(),
        }

    def _read_state(self) -> dict[str, Any]:
        with locked_path(_state_lock_path(self.config.paths.state)):
            payload = load_json(self.config.paths.state)
        if payload.get("schema_version") != STATE_SCHEMA_VERSION:
            raise WatchdogError("campaign state schema changed while watchdog was running")
        if str(payload.get("campaign_uuid") or "") != self.config.campaign_uuid:
            raise WatchdogError("campaign state UUID changed while watchdog was running")
        return payload

    def _update_state(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with locked_path(_state_lock_path(self.config.paths.state)):
            payload = load_json(self.config.paths.state)
            if payload.get("schema_version") != STATE_SCHEMA_VERSION:
                raise WatchdogError("campaign state schema changed while watchdog was running")
            if str(payload.get("campaign_uuid") or "") != self.config.campaign_uuid:
                raise WatchdogError("campaign state UUID changed while watchdog was running")
            mutate(payload)
            payload["schema_version"] = STATE_SCHEMA_VERSION
            payload["revision"] = int(payload.get("revision") or 0) + 1
            payload["updated_at"] = utc_now()
            atomic_write_json(self.config.paths.state, payload)
            return copy.deepcopy(payload)

    def _record_watchdog_heartbeat(self, *, update_state: bool = True) -> None:
        identity = self.watchdog_identity
        if identity is None:
            raise WatchdogError("watchdog identity was not captured")
        now_ns = self.monotonic_ns()

        def mutate(payload: dict[str, Any]) -> None:
            heartbeat = payload.setdefault("heartbeat", {})
            heartbeat.update({
                "watchdog_pid": os.getpid(),
                "watchdog_start_ticks": identity.start_ticks,
                "watchdog_at": utc_now(),
                "watchdog_monotonic_ns": now_ns,
                "watchdog_cgroup": identity.cgroup_path,
                "watchdog_outside_campaign_cgroup": True,
            })

        if update_state:
            self._update_state(mutate)
        liveness = {
            "schema_version": WATCHDOG_LIVENESS_SCHEMA_VERSION,
            "campaign_uuid": self.config.campaign_uuid,
            "watchdog": {
                "pid": os.getpid(),
                "start_ticks": identity.start_ticks,
                "boot_id": identity.boot_id,
                "cgroup": identity.cgroup_path,
                "monotonic_ns": now_ns,
            },
            "updated_at": utc_now(),
        }
        if self.watchdog_auth_key is not None:
            self.watchdog_liveness_sequence += 1
            liveness = sign_authenticated_payload(
                liveness,
                session_secret=self.watchdog_auth_key,
                campaign_uuid=self.config.campaign_uuid,
                stream="watchdog_liveness",
                sequence=self.watchdog_liveness_sequence,
                monotonic_ns=now_ns,
            )
        elif self.config.production:
            raise WatchdogError("cannot publish unsigned production watchdog liveness")
        atomic_write_json(self._liveness_path(), liveness)
        self.last_watchdog_heartbeat_ns = now_ns

    def _watchdog_heartbeat_due(self) -> bool:
        if self.last_watchdog_heartbeat_ns <= 0:
            return True
        elapsed_ns = self.monotonic_ns() - self.last_watchdog_heartbeat_ns
        return elapsed_ns >= int(DURABLE_HEARTBEAT_INTERVAL_SECONDS * 1_000_000_000)

    def _verify_authenticated_stream(
        self,
        payload: Mapping[str, Any],
        *,
        stream: str,
    ) -> dict[str, Any]:
        if self.runner_auth_key is None:
            if self.config.production:
                raise ControlChannelError("production control session key is unavailable")
            return {"required": False, "ok": True}
        previous = self.authenticated_streams.get(stream) or {}
        return verify_authenticated_payload(
            payload,
            session_secret=self.runner_auth_key,
            expected_campaign_uuid=self.config.campaign_uuid,
            expected_stream=stream,
            previous_sequence=int(previous.get("sequence") or 0),
            previous_payload_sha256=str(previous.get("payload_sha256") or ""),
        )

    def _heartbeat_health(self) -> tuple[bool, str, dict[str, Any]]:
        heartbeat_payload = load_json(self.config.paths.heartbeat)
        checkpoint = load_json(self.config.paths.checkpoint)
        heartbeat = extract_heartbeat(heartbeat_payload)
        details: dict[str, Any] = {}
        if str(heartbeat_payload.get("campaign_uuid") or self.config.campaign_uuid) != self.config.campaign_uuid:
            return False, "HEARTBEAT_CAMPAIGN_UUID_MISMATCH", details
        try:
            heartbeat_pid = int(heartbeat.get("orchestrator_pid") or 0)
            heartbeat_ticks = int(heartbeat.get("orchestrator_start_ticks") or 0)
            heartbeat_ns = int(heartbeat.get("orchestrator_monotonic_ns") or heartbeat.get("monotonic_ns") or 0)
        except (TypeError, ValueError):
            return False, "HEARTBEAT_IDENTITY_INVALID", details
        details.update({
            "heartbeat_pid": heartbeat_pid,
            "heartbeat_start_ticks": heartbeat_ticks,
            "heartbeat_monotonic_ns": heartbeat_ns,
        })
        try:
            heartbeat_auth = self._verify_authenticated_stream(
                heartbeat_payload,
                stream="runner_heartbeat",
            )
            checkpoint_auth = self._verify_authenticated_stream(
                checkpoint,
                stream="runner_checkpoint",
            )
        except ControlChannelError as exc:
            details["authentication_error"] = str(exc)
            return False, "HEARTBEAT_AUTHENTICATION_INVALID", details
        if heartbeat_auth.get("required") is not False:
            if int(heartbeat_auth.get("monotonic_ns") or 0) != heartbeat_ns:
                details["authentication_error"] = "heartbeat monotonic binding mismatch"
                return False, "HEARTBEAT_AUTHENTICATION_INVALID", details
            checkpoint_auth_ns = int(checkpoint_auth.get("monotonic_ns") or 0)
            if checkpoint_auth_ns <= 0 or checkpoint_auth_ns > self.monotonic_ns():
                details["authentication_error"] = "checkpoint monotonic binding invalid"
                return False, "HEARTBEAT_AUTHENTICATION_INVALID", details
            self.authenticated_streams["runner_heartbeat"] = heartbeat_auth
            self.authenticated_streams["runner_checkpoint"] = checkpoint_auth
            details["heartbeat_authentication"] = heartbeat_auth
            details["checkpoint_authentication"] = checkpoint_auth
        if heartbeat_pid != self.config.orchestrator_pid or heartbeat_ticks != self.config.orchestrator_start_ticks:
            return False, "HEARTBEAT_IDENTITY_MISMATCH", details
        now_ns = self.monotonic_ns()
        if heartbeat_ns <= 0 or heartbeat_ns > now_ns:
            return False, "HEARTBEAT_MONOTONIC_INVALID", details
        age_seconds = (now_ns - heartbeat_ns) / 1_000_000_000
        details["heartbeat_age_seconds"] = round(age_seconds, 6)
        if age_seconds >= float(self.config.stale_after_seconds):
            return False, "HEARTBEAT_STALE", details

        try:
            expected_revision = int(heartbeat.get("checkpoint_revision") or 0)
            actual_revision = int(checkpoint.get("revision") or checkpoint.get("checkpoint_revision") or 0)
        except (TypeError, ValueError):
            return False, "CHECKPOINT_REVISION_INVALID", details
        details.update({"heartbeat_checkpoint_revision": expected_revision, "checkpoint_revision": actual_revision})
        if expected_revision <= 0 or actual_revision < expected_revision:
            return False, "CHECKPOINT_NOT_DURABLE", details
        return True, "HEALTHY", details

    def _external_hard_stop_request(
        self,
        durable_state: Mapping[str, Any],
    ) -> tuple[bool, str, dict[str, Any]]:
        """Inspect both durable and mirror controls before checking liveness.

        A closed gate is normal during PREPARING/PREFLIGHT/FROZEN and after a
        terminal result.  Only STOPPING_LOAD/PRESERVING_EVIDENCE is an active
        request for this external process to take over evidence preservation
        and cgroup teardown.
        """

        mirror = load_json(self.config.paths.control)
        mirror_uuid = str(mirror.get("campaign_uuid") or "")
        if (self.config.production and mirror_uuid != self.config.campaign_uuid) or (
            mirror_uuid and mirror_uuid != self.config.campaign_uuid
        ):
            return True, "CONTROL_CAMPAIGN_UUID_MISMATCH", {
                "expected_campaign_uuid": self.config.campaign_uuid,
                "actual_campaign_uuid": mirror_uuid,
            }
        durable_name = str(durable_state.get("state") or "")
        durable_control = durable_state.get("control") or {}
        mirror_control = mirror.get("control") if isinstance(mirror.get("control"), dict) else mirror
        mirror_name = str(mirror.get("state") or "")
        durable_admit = bool(durable_control.get("admit_new_jobs"))
        mirror_admit_value = mirror_control.get("admit_new_jobs")
        mirror_admit = bool(mirror_admit_value) if mirror_admit_value is not None else None
        stopping_states = {"STOPPING_LOAD", "PRESERVING_EVIDENCE"}
        requested = (
            durable_name in stopping_states and not durable_admit
        ) or (
            mirror_name in stopping_states and mirror_admit is False
        )
        details = {
            "durable_state": durable_name,
            "durable_admit_new_jobs": durable_admit,
            "mirror_state": mirror_name or None,
            "mirror_admit_new_jobs": mirror_admit,
            "original_reason": durable_state.get("reason") or mirror.get("reason"),
            "original_classification": durable_state.get("classification"),
            "original_hard_stop": copy.deepcopy(durable_state.get("hard_stop")),
        }
        if requested:
            return True, "EXTERNAL_HARD_STOP_REQUESTED", details
        if mirror_name in stopping_states or durable_name in stopping_states:
            return True, "HARD_STOP_CONTROL_INCONSISTENT", details
        return False, "NO_EXTERNAL_HARD_STOP", details

    def evaluate(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            identity = verify_process_identity(
                pid=self.config.orchestrator_pid,
                expected_start_ticks=self.config.orchestrator_start_ticks,
                expected_boot_id=self.config.orchestrator_boot_id,
                expected_cgroup_path=self.config.orchestrator_cgroup,
                proc_root=self.config.proc_root,
            )
        except ProcessIdentityError as exc:
            return False, "ORCHESTRATOR_IDENTITY_LOST", {"identity_error": str(exc)}
        healthy, reason, details = self._heartbeat_health()
        details["orchestrator_state"] = identity.state
        details["orchestrator_cgroup"] = identity.cgroup_path
        try:
            current_watchdog = capture_process_identity(os.getpid(), proc_root=self.config.proc_root)
        except ProcessIdentityError as exc:
            return False, "WATCHDOG_IDENTITY_LOST", {**details, "watchdog_identity_error": str(exc)}
        if self.watchdog_identity and current_watchdog.start_ticks != self.watchdog_identity.start_ticks:
            return False, "WATCHDOG_IDENTITY_LOST", details
        self.watchdog_identity = current_watchdog
        if _is_inside_cgroup(
            member=current_watchdog.cgroup_path,
            target=self.config.campaign_cgroup.path,
        ):
            return False, "WATCHDOG_CONTAINMENT_VIOLATION", details
        details["watchdog_cgroup"] = current_watchdog.cgroup_path
        return healthy, reason, details

    def _close_admission(self, *, incident_id: str, reason: str, details: Mapping[str, Any]) -> dict[str, Any]:
        detected_ns = self.monotonic_ns()

        def mutate(payload: dict[str, Any]) -> None:
            now = utc_now()
            current = str(payload.get("state") or "")
            takeover = current in {"STOPPING_LOAD", "PRESERVING_EVIDENCE"}
            clock = payload.setdefault("clock", {})
            # Do not credit time after the last verified orchestrator tick.
            previous_ns = int(clock.get("last_tick_monotonic_ns") or detected_ns)
            if detected_ns >= previous_ns and clock.get("active_started_at"):
                invalid_delta = (detected_ns - previous_ns) / 1_000_000_000
                clock["wall_clock_seconds"] = round(
                    float(clock.get("wall_clock_seconds") or 0.0) + invalid_delta,
                    6,
                )
                clock["invalid_seconds"] = round(
                    float(clock.get("invalid_seconds") or 0.0) + invalid_delta,
                    6,
                )
            clock.update({
                "active_finished_at": clock.get("active_finished_at") or now,
                "formal_segment_valid": False,
                "clock_pause_reason": reason,
                "last_tick_monotonic_ns": detected_ns,
            })
            if current in {"FROZEN", "ACTIVE", "DEGRADED", "STOPPING_LOAD"}:
                next_state = "STOPPING_LOAD"
            elif current == "PRESERVING_EVIDENCE":
                next_state = current
            elif current in {"PREPARING", "PREFLIGHT"}:
                next_state = "INTERRUPTED"
            else:
                next_state = "FAILED"
            payload["state"] = next_state
            if not takeover:
                payload["state_entered_at"] = now
                payload["classification"] = "FAIL_HARNESS"
                payload["reason"] = reason
            payload["control"] = {
                "admit_new_jobs": False,
                "load_generator_should_run": False,
                "preserve_evidence_requested": True,
            }
            if takeover and isinstance(payload.get("hard_stop"), dict):
                payload["watchdog_takeover"] = {
                    "at": now,
                    "detected_monotonic_ns": detected_ns,
                    "reason_code": reason,
                    "incident_id": incident_id,
                    "evidence": dict(details),
                }
            else:
                payload["hard_stop"] = {
                    "at": now,
                    "detected_monotonic_ns": detected_ns,
                    "reason_code": reason,
                    "classification": "FAIL_HARNESS",
                    "incident_id": incident_id,
                    "evidence": dict(details),
                }
            _event(payload, state=str(payload["state"]), reason=reason, evidence={"incident_id": incident_id})

        state = self._update_state(mutate)
        atomic_write_json(self.config.paths.control, {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "campaign_uuid": self.config.campaign_uuid,
            "revision": int(state.get("revision") or 0),
            "state": state.get("state"),
            "admit_new_jobs": False,
            "load_generator_should_run": False,
            "preserve_evidence_requested": True,
            "reason": reason,
            "incident_id": incident_id,
            "updated_at": utc_now(),
        })
        return state

    def _transition(self, target: str, *, reason: str, incident_id: str, evidence: Any = None) -> dict[str, Any]:
        def mutate(payload: dict[str, Any]) -> None:
            current = str(payload.get("state") or "")
            if current in {"FAILED", "PASS", "INTERRUPTED"}:
                return
            payload["state"] = target
            payload["state_entered_at"] = utc_now()
            payload["reason"] = reason
            payload.setdefault("control", {}).update({
                "admit_new_jobs": False,
                "load_generator_should_run": False,
                "preserve_evidence_requested": True,
            })
            _event(payload, state=target, reason=reason, evidence=evidence or {"incident_id": incident_id})

        return self._update_state(mutate)

    @staticmethod
    def _file_metadata(path: Path) -> dict[str, Any]:
        try:
            metadata = Path(path).stat()
            payload = load_json(path)
            selected = {
                key: payload.get(key)
                for key in (
                    "schema_version",
                    "campaign_uuid",
                    "revision",
                    "state",
                    "status",
                    "phase",
                    "updated_at",
                    "continuous_active_seconds",
                    "active_test_seconds",
                )
                if key in payload
            }
            clock = payload.get("clock")
            if isinstance(clock, dict):
                selected["clock"] = {
                    key: clock.get(key)
                    for key in (
                        "continuous_active_seconds",
                        "wall_clock_seconds",
                        "formal_segment_valid",
                        "clock_pause_reason",
                        "active_finished_at",
                    )
                }
            return {
                "path": str(path),
                "size": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "sha256": sha256_file(path),
                "selected_fields": selected,
            }
        except Exception as exc:
            return {"path": str(path), "collector_error": f"{exc.__class__.__name__}: {exc}"}

    def _process_evidence(self) -> dict[str, Any]:
        root = Path(self.config.proc_root) / str(self.config.orchestrator_pid)
        result: dict[str, Any] = {"pid": self.config.orchestrator_pid}
        for name in ("stat", "status", "cgroup"):
            try:
                text = (root / name).read_text(encoding="utf-8", errors="replace")
                if name == "status":
                    allowed = ("Name:", "State:", "Pid:", "PPid:", "Threads:", "VmRSS:", "FDSize:")
                    text = "\n".join(line for line in text.splitlines() if line.startswith(allowed))
                result[name] = text[:65536]
            except Exception as exc:
                result[name] = {"collector_error": f"{exc.__class__.__name__}: {exc}"}
        return result

    def _capture_evidence(self, *, incident_id: str, reason: str, details: Mapping[str, Any]) -> Path:
        if self.cgroup is None:
            raise WatchdogError("campaign cgroup was not pinned")
        evidence_dir = self.config.paths.evidence / incident_id
        evidence_dir.mkdir(parents=True, exist_ok=False)
        payload = {
            "schema_version": WATCHDOG_SCHEMA_VERSION,
            "campaign_uuid": self.config.campaign_uuid,
            "incident_id": incident_id,
            "reason": reason,
            "captured_at": utc_now(),
            "watchdog": {
                "pid": os.getpid(),
                "start_ticks": self.watchdog_identity.start_ticks if self.watchdog_identity else 0,
                "cgroup": self.watchdog_identity.cgroup_path if self.watchdog_identity else "",
            },
            "details": dict(details),
            "orchestrator_process": self._process_evidence(),
            "campaign_cgroup_before_stop": self.cgroup.snapshot(),
            "files": {
                "state": self._file_metadata(self.config.paths.state),
                "heartbeat": self._file_metadata(self.config.paths.heartbeat),
                "checkpoint": self._file_metadata(self.config.paths.checkpoint),
                "control": self._file_metadata(self.config.paths.control),
            },
            "credential_material_collected": False,
        }
        path = evidence_dir / "watchdog_incident.json"
        atomic_write_json(path, payload)
        return path

    def _stop_campaign_cgroup(self) -> dict[str, Any]:
        if self.cgroup is None:
            raise WatchdogError("campaign cgroup was not pinned")
        started = time.monotonic()
        result: dict[str, Any] = {"freeze_written": False, "kill_written": False, "population_cleared": False}
        self.cgroup.freeze()
        result["freeze_written"] = True
        self.cgroup.kill()
        result["kill_written"] = True
        deadline = time.monotonic() + max(0.0, float(self.config.kill_verify_seconds))
        while True:
            if not self.cgroup.populated():
                result["population_cleared"] = True
                break
            if time.monotonic() >= deadline:
                break
            self.sleep(0.05)
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        if not result["population_cleared"]:
            raise CgroupIdentityError("campaign cgroup remained populated after cgroup.kill")
        return result

    def trigger_incident(self, *, reason: str, details: Mapping[str, Any]) -> dict[str, Any]:
        self.incident_id = f"watchdog-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
        collector_errors: list[str] = []
        state: dict[str, Any] = {}
        evidence_path: Path | None = None
        stop_result: dict[str, Any] = {}
        transition_reason = (
            str(details.get("original_reason") or reason)
            if reason == "EXTERNAL_HARD_STOP_REQUESTED"
            else reason
        )
        try:
            state = self._close_admission(incident_id=self.incident_id, reason=reason, details=details)
        except Exception as exc:
            collector_errors.append(f"state_hard_stop: {exc.__class__.__name__}: {exc}")
            # A separate gate remains useful even if the main state was corrupt.
            atomic_write_json(self.config.paths.control, {
                "schema_version": CONTROL_SCHEMA_VERSION,
                "campaign_uuid": self.config.campaign_uuid,
                "state": "STOPPING_LOAD",
                "admit_new_jobs": False,
                "load_generator_should_run": False,
                "preserve_evidence_requested": True,
                "reason": reason,
                "incident_id": self.incident_id,
                "updated_at": utc_now(),
            })
        try:
            if str(state.get("state") or "") == "STOPPING_LOAD":
                self._transition(
                    "PRESERVING_EVIDENCE",
                    reason=transition_reason,
                    incident_id=self.incident_id,
                )
        except Exception as exc:
            collector_errors.append(f"state_preserving: {exc.__class__.__name__}: {exc}")
        try:
            # Freeze first so no helper can begin more work while evidence is read.
            if self.cgroup is None:
                raise WatchdogError("campaign cgroup was not pinned")
            self.cgroup.freeze()
            evidence_path = self._capture_evidence(
                incident_id=self.incident_id,
                reason=reason,
                details=details,
            )
        except Exception as exc:
            collector_errors.append(f"evidence: {exc.__class__.__name__}: {exc}")
        try:
            stop_result = self._stop_campaign_cgroup()
        except Exception as exc:
            collector_errors.append(f"cgroup_stop: {exc.__class__.__name__}: {exc}")
        final_state = "INTERRUPTED" if not collector_errors else "FAILED"
        try:
            state = self._transition(
                final_state,
                reason=transition_reason if not collector_errors else "WATCHDOG_INCIDENT_HANDLING_FAILED",
                incident_id=self.incident_id,
                evidence={
                    "incident_id": self.incident_id,
                    "evidence_path": str(evidence_path or ""),
                    "cgroup_stop": stop_result,
                    "collector_errors": collector_errors,
                },
            )
        except Exception as exc:
            collector_errors.append(f"state_terminal: {exc.__class__.__name__}: {exc}")
        result = {
            "schema_version": WATCHDOG_SCHEMA_VERSION,
            "ok": not collector_errors,
            "campaign_uuid": self.config.campaign_uuid,
            "incident_id": self.incident_id,
            "reason": reason,
            "state": state.get("state") or final_state,
            "admit_new_jobs": False,
            "evidence_path": str(evidence_path or ""),
            "cgroup_stop": stop_result,
            "collector_errors": collector_errors,
            "finished_at": utc_now(),
        }
        atomic_write_json(self.config.paths.ready, result)
        return result

    def fail_closed_startup(self, exc: BaseException) -> dict[str, Any]:
        """Close admission even when full watchdog startup proof failed."""

        error = f"{exc.__class__.__name__}: {exc}"
        if not self.paths_validated:
            return {
                "schema_version": WATCHDOG_SCHEMA_VERSION,
                "ok": False,
                "verified": False,
                "classification": "FAIL_HARNESS",
                "reason": "WATCHDOG_PATH_VALIDATION_FAILED",
                "error": error,
                "admit_new_jobs": False,
            }
        try:
            existing = self._read_state()
            existing_control = existing.get("control") or {}
            if str(existing.get("state") or "") in {"INTERRUPTED", "FAILED", "PASS"} and not bool(
                existing_control.get("admit_new_jobs")
            ):
                return {
                    "schema_version": WATCHDOG_SCHEMA_VERSION,
                    "ok": False,
                    "verified": False,
                    "campaign_uuid": self.config.campaign_uuid,
                    "incident_id": str((existing.get("hard_stop") or {}).get("incident_id") or self.incident_id),
                    "state": existing.get("state"),
                    "classification": existing.get("classification") or "FAIL_HARNESS",
                    "reason": existing.get("reason") or "WATCHDOG_FAIL_CLOSED",
                    "error": error,
                    "admit_new_jobs": False,
                }
        except Exception:
            pass
        if self.cgroup is not None and self.watchdog_identity is not None:
            return self.trigger_incident(
                reason="WATCHDOG_STARTUP_FAILURE",
                details={"startup_error": error},
            )
        incident_id = f"watchdog-startup-{uuid.uuid4().hex[:12]}"
        collector_errors: list[str] = []
        state: dict[str, Any] = {}
        try:
            state = self._close_admission(
                incident_id=incident_id,
                reason="WATCHDOG_STARTUP_FAILURE",
                details={"startup_error": error},
            )
            if state.get("state") == "STOPPING_LOAD":
                state = self._transition(
                    "PRESERVING_EVIDENCE",
                    reason="WATCHDOG_STARTUP_FAILURE",
                    incident_id=incident_id,
                )
                state = self._transition(
                    "FAILED",
                    reason="WATCHDOG_STARTUP_FAILURE",
                    incident_id=incident_id,
                    evidence={"startup_error": error, "campaign_cgroup_stopped": False},
                )
        except Exception as state_exc:
            collector_errors.append(f"state: {state_exc.__class__.__name__}: {state_exc}")
        result = {
            "schema_version": WATCHDOG_SCHEMA_VERSION,
            "ok": False,
            "verified": False,
            "campaign_uuid": self.config.campaign_uuid,
            "incident_id": incident_id,
            "state": state.get("state") or "FAILED",
            "classification": "FAIL_HARNESS",
            "reason": "WATCHDOG_FAIL_CLOSED",
            "error": error,
            "admit_new_jobs": False,
            "campaign_cgroup_stopped": False,
            "collector_errors": collector_errors,
            "finished_at": utc_now(),
        }
        try:
            atomic_write_json(self.config.paths.ready, result)
        except Exception:
            pass
        return result

    def run_once(self) -> int:
        state = self._read_state()
        control = state.get("control") or {}
        current = str(state.get("state") or "")
        if current in {"INTERRUPTED", "FAILED", "PASS"} and not bool(control.get("admit_new_jobs")):
            self.terminal_state_seen = current
            self.stop_requested = True
            return 0
        requested, reason, details = self._external_hard_stop_request(state)
        if requested:
            self.trigger_incident(reason=reason, details=details)
            return INCIDENT_EXIT_CODE
        healthy, reason, details = self.evaluate()
        if healthy:
            if self._watchdog_heartbeat_due():
                # The authenticated liveness artifact is the recurring proof.
                # State already contains the startup identity; rewriting both
                # files every interval doubles durable fsync load on WSL.
                self._record_watchdog_heartbeat(update_state=False)
            return 0
        self.trigger_incident(reason=reason, details=details)
        return INCIDENT_EXIT_CODE

    def run(self, *, once: bool = False) -> int:
        # Preserve the public validation order: path safety and the production
        # --once prohibition must be evaluated before either broader production
        # configuration checks or the authenticated-socket requirement.
        self._validate_paths()
        if once and self.config.production:
            raise WatchdogError("--once is forbidden for a production watchdog")
        self._validate_configuration()
        if self.config.production and self.config.auth_socket is None:
            raise WatchdogError("production watchdog requires authenticated control socket")
        if self.config.auth_socket is not None:
            authentication = send_hello(
                self.config.auth_socket,
                campaign_uuid=self.config.campaign_uuid,
                role="watchdog",
                require_session_secret=True,
                expected_server_peer=PeerIdentity(
                    self.config.supervisor_pid,
                    os.getuid(),
                    os.getgid(),
                ),
                expected_server_process={
                    "pid": self.config.supervisor_pid,
                    "start_ticks": self.config.supervisor_start_ticks,
                    "boot_id": self.config.supervisor_boot_id,
                    "cgroup_path": self.config.supervisor_cgroup,
                },
            )
            if not isinstance(authentication, tuple):
                raise WatchdogError("watchdog control handshake did not deliver a session key")
            authentication_evidence, self.watchdog_auth_key = authentication
            self.control_authentication_evidence = dict(authentication_evidence)
            self.runner_auth_key = derive_runner_auth_key(self.watchdog_auth_key)
            if self.runner_auth_key == self.watchdog_auth_key:
                raise WatchdogError("runner and watchdog authentication keys are not separated")
        try:
            self._process_lock_context = locked_path(self.config.paths.process_lock, nonblocking=True)
            self._process_lock_context.__enter__()
        except BlockingIOError as exc:
            raise DuplicateWatchdogError("another watchdog already holds the campaign process lock") from exc
        try:
            try:
                ready = self.validate_startup()
                initial_state = self._read_state()
                initial_control = initial_state.get("control") or {}
                initial_state_name = str(initial_state.get("state") or "")
                terminal = initial_state_name in {"INTERRUPTED", "FAILED", "PASS"} and not bool(
                    initial_control.get("admit_new_jobs")
                )
                if not terminal:
                    requested, reason, details = self._external_hard_stop_request(initial_state)
                    if requested:
                        self.trigger_incident(reason=reason, details=details)
                        return INCIDENT_EXIT_CODE
                    healthy, reason, details = self.evaluate()
                    if not healthy:
                        self.trigger_incident(reason=reason, details=details)
                        return INCIDENT_EXIT_CODE
                    self._record_watchdog_heartbeat()
                    ready["initial_health"] = {"ok": True, "reason": reason, **details}
                else:
                    ready["initial_health"] = {
                        "ok": True,
                        "reason": "CAMPAIGN_ALREADY_TERMINAL",
                        "campaign_state": initial_state_name,
                    }
                atomic_write_json(self.config.paths.ready, ready)
                if once:
                    result = self.run_once()
                    if self.terminal_state_seen:
                        stopped = dict(ready)
                        stopped.update({
                            "verified": False,
                            "status": "campaign_terminal",
                            "campaign_state": self.terminal_state_seen,
                            "stopped_at": utc_now(),
                        })
                        atomic_write_json(self.config.paths.ready, stopped)
                    return result
                while not self.stop_requested:
                    result = self.run_once()
                    if result != 0:
                        return result
                    self.sleep(max(0.05, float(self.config.poll_seconds)))
                if not self.terminal_state_seen:
                    stopped_state = self._read_state()
                    stopped_control = stopped_state.get("control") or {}
                    stopped_name = str(stopped_state.get("state") or "")
                    terminal_now = stopped_name in {"INTERRUPTED", "FAILED", "PASS"} and not bool(
                        stopped_control.get("admit_new_jobs")
                    )
                    if terminal_now:
                        self.terminal_state_seen = stopped_name
                    else:
                        self.trigger_incident(
                            reason="WATCHDOG_SIGNALLED",
                            details={"signal": self.signal_received or None},
                        )
                        return INCIDENT_EXIT_CODE
                stopped = dict(ready)
                status = "campaign_terminal" if self.terminal_state_seen else "stopped_by_signal"
                stopped.update({
                    "verified": False,
                    "status": status,
                    "campaign_state": self.terminal_state_seen or None,
                    "stopped_at": utc_now(),
                })
                atomic_write_json(self.config.paths.ready, stopped)
                return 0
            except Exception as exc:
                if self.cgroup is not None and self.watchdog_identity is not None:
                    try:
                        self.trigger_incident(
                            reason="WATCHDOG_INTERNAL_FAILURE",
                            details={"watchdog_error": f"{exc.__class__.__name__}: {exc}"},
                        )
                    except Exception:
                        pass
                raise
        finally:
            if self.cgroup is not None:
                self.cgroup.close()
                self.cgroup = None
            if self._process_lock_context is not None:
                self._process_lock_context.__exit__(None, None, None)


def _default_paths(root: Path) -> WatchdogPaths:
    return WatchdogPaths(
        campaign_root=root,
        state=root / "checkpoint" / "campaign.state.json",
        control=root / "checkpoint" / "campaign.control.json",
        heartbeat=root / "checkpoint" / "campaign.state.json",
        checkpoint=root / "checkpoint" / "campaign.checkpoint.json",
        ready=root / "checkpoint" / "watchdog.status.json",
        evidence=root / "artifacts" / "watchdog",
        process_lock=root / "checkpoint" / "watchdog.process.lock",
        liveness=root / "checkpoint" / "watchdog.liveness.json",
    )


def build_watchdog_command(
    config: WatchdogConfig,
    *,
    python_executable: str = sys.executable,
    module_path: Path = Path(__file__),
    once: bool = False,
) -> list[str]:
    """Return the secret-free argv a supervisor should launch with Popen.

    The caller must use ``start_new_session=True`` and must launch this command
    from the supervisor side, before wrapping any workload command for entry
    into the managed campaign scope.
    """

    paths = config.paths
    command = [
        str(python_executable),
        str(Path(module_path).resolve(strict=False)),
        "--campaign-root", str(paths.campaign_root),
        "--campaign-uuid", config.campaign_uuid,
        "--state-path", str(paths.state),
        "--control-path", str(paths.control),
        "--heartbeat-path", str(paths.heartbeat),
        "--checkpoint-path", str(paths.checkpoint),
        "--ready-path", str(paths.ready),
        "--liveness-path", str(paths.liveness or paths.ready.with_name("watchdog.liveness.json")),
        "--evidence-path", str(paths.evidence),
        "--process-lock-path", str(paths.process_lock),
        "--orchestrator-pid", str(config.orchestrator_pid),
        "--orchestrator-start-ticks", str(config.orchestrator_start_ticks),
        "--orchestrator-boot-id", config.orchestrator_boot_id,
        "--orchestrator-cgroup", config.orchestrator_cgroup,
        "--campaign-cgroup", config.campaign_cgroup.path,
        "--campaign-cgroup-device", str(config.campaign_cgroup.device),
        "--campaign-cgroup-inode", str(config.campaign_cgroup.inode),
        "--stale-after-seconds", str(config.stale_after_seconds),
        "--poll-seconds", str(config.poll_seconds),
        "--kill-verify-seconds", str(config.kill_verify_seconds),
    ]
    if not config.production:
        command.extend([
            "--development-mode",
            "--proc-root", str(config.proc_root),
            "--cgroup-root", str(config.cgroup_root),
        ])
    if config.auth_socket is not None:
        command.extend(["--auth-socket", str(config.auth_socket)])
    if config.supervisor_pid > 0:
        command.extend([
            "--supervisor-pid", str(config.supervisor_pid),
            "--supervisor-start-ticks", str(config.supervisor_start_ticks),
            "--supervisor-boot-id", config.supervisor_boot_id,
            "--supervisor-cgroup", config.supervisor_cgroup,
        ])
    if once:
        command.append("--once")
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", required=True)
    parser.add_argument("--campaign-uuid", required=True)
    parser.add_argument("--state-path")
    parser.add_argument("--control-path")
    parser.add_argument("--heartbeat-path")
    parser.add_argument("--checkpoint-path")
    parser.add_argument("--ready-path")
    parser.add_argument("--liveness-path")
    parser.add_argument("--evidence-path")
    parser.add_argument("--process-lock-path")
    parser.add_argument("--orchestrator-pid", required=True, type=int)
    parser.add_argument("--orchestrator-start-ticks", required=True, type=int)
    parser.add_argument("--orchestrator-boot-id", required=True)
    parser.add_argument("--orchestrator-cgroup", required=True)
    parser.add_argument("--campaign-cgroup", required=True)
    parser.add_argument("--campaign-cgroup-device", required=True, type=int)
    parser.add_argument("--campaign-cgroup-inode", required=True, type=int)
    parser.add_argument("--auth-socket")
    parser.add_argument("--supervisor-pid", type=int, default=0)
    parser.add_argument("--supervisor-start-ticks", type=int, default=0)
    parser.add_argument("--supervisor-boot-id", default="")
    parser.add_argument("--supervisor-cgroup", default="")
    parser.add_argument("--stale-after-seconds", type=float, default=DEFAULT_STALE_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--kill-verify-seconds", type=float, default=10.0)
    parser.add_argument("--once", action="store_true", help="Evaluate one sample; Level-0 harness verification only.")
    parser.add_argument("--development-mode", action="store_true")
    parser.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    parser.add_argument("--cgroup-root", default="/sys/fs/cgroup", help=argparse.SUPPRESS)
    return parser


def config_from_args(args: argparse.Namespace) -> WatchdogConfig:
    root = Path(args.campaign_root)
    defaults = _default_paths(root)
    paths = WatchdogPaths(
        campaign_root=root,
        state=Path(args.state_path) if args.state_path else defaults.state,
        control=Path(args.control_path) if args.control_path else defaults.control,
        heartbeat=Path(args.heartbeat_path) if args.heartbeat_path else defaults.heartbeat,
        checkpoint=Path(args.checkpoint_path) if args.checkpoint_path else defaults.checkpoint,
        ready=Path(args.ready_path) if args.ready_path else defaults.ready,
        evidence=Path(args.evidence_path) if args.evidence_path else defaults.evidence,
        process_lock=Path(args.process_lock_path) if args.process_lock_path else defaults.process_lock,
        liveness=Path(args.liveness_path) if args.liveness_path else defaults.liveness,
    )
    return WatchdogConfig(
        campaign_uuid=str(args.campaign_uuid),
        paths=paths,
        orchestrator_pid=int(args.orchestrator_pid),
        orchestrator_start_ticks=int(args.orchestrator_start_ticks),
        orchestrator_boot_id=str(args.orchestrator_boot_id),
        orchestrator_cgroup=str(args.orchestrator_cgroup),
        campaign_cgroup=CgroupIdentity(
            normalize_cgroup_path(args.campaign_cgroup),
            int(args.campaign_cgroup_device),
            int(args.campaign_cgroup_inode),
        ),
        proc_root=Path(args.proc_root),
        cgroup_root=Path(args.cgroup_root),
        stale_after_seconds=float(args.stale_after_seconds),
        poll_seconds=float(args.poll_seconds),
        kill_verify_seconds=float(args.kill_verify_seconds),
        production=not bool(args.development_mode),
        auth_socket=Path(args.auth_socket) if args.auth_socket else None,
        supervisor_pid=int(args.supervisor_pid),
        supervisor_start_ticks=int(args.supervisor_start_ticks),
        supervisor_boot_id=str(args.supervisor_boot_id),
        supervisor_cgroup=str(args.supervisor_cgroup),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    watchdog: ExternalCampaignWatchdog | None = None

    try:
        watchdog = ExternalCampaignWatchdog(config_from_args(args))
    except Exception as exc:
        # Configuration failures happen before a watchdog object exists.  Only
        # touch a control path after proving that it remains in the /tmp run.
        try:
            root = Path(args.campaign_root)
            defaults = _default_paths(root)
            control = Path(args.control_path) if args.control_path else defaults.control
            validate_runtime_path(control, root=root, label="control path")
            atomic_write_json(control, {
                "schema_version": CONTROL_SCHEMA_VERSION,
                "campaign_uuid": str(args.campaign_uuid),
                "state": "FAILED",
                "admit_new_jobs": False,
                "load_generator_should_run": False,
                "preserve_evidence_requested": True,
                "reason": "WATCHDOG_FAIL_CLOSED",
                "error": f"{exc.__class__.__name__}: {exc}",
                "updated_at": utc_now(),
            })
        except Exception:
            pass
        print(json.dumps({
            "ok": False,
            "classification": "FAIL_HARNESS",
            "reason": "WATCHDOG_FAIL_CLOSED",
            "error": f"{exc.__class__.__name__}: {exc}",
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        return 2

    def stop(_signum: int, _frame: Any) -> None:
        assert watchdog is not None
        watchdog.signal_received = int(_signum)
        watchdog.stop_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        assert watchdog is not None
        return watchdog.run(once=bool(args.once))
    except DuplicateWatchdogError as exc:
        print(json.dumps({
            "ok": False,
            "classification": "BLOCKED",
            "reason": "WATCHDOG_ALREADY_RUNNING",
            "error": str(exc),
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        return 3
    except Exception as exc:
        # A startup/collector failure must never leave a previously-open load
        # gate looking safe.  Preserve the original state file for diagnosis.
        failure = watchdog.fail_closed_startup(exc)
        try:
            atomic_write_json(watchdog.config.paths.control, {
                "schema_version": CONTROL_SCHEMA_VERSION,
                "campaign_uuid": watchdog.config.campaign_uuid,
                "state": str(failure.get("state") or "FAILED"),
                "admit_new_jobs": False,
                "load_generator_should_run": False,
                "preserve_evidence_requested": True,
                "reason": str(failure.get("reason") or "WATCHDOG_FAIL_CLOSED"),
                "incident_id": str(failure.get("incident_id") or ""),
                "error": f"{exc.__class__.__name__}: {exc}",
                "updated_at": utc_now(),
            })
        except Exception:
            pass
        print(json.dumps({
            "ok": False,
            "classification": "FAIL_HARNESS",
            "reason": "WATCHDOG_FAIL_CLOSED",
            "error": f"{exc.__class__.__name__}: {exc}",
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
