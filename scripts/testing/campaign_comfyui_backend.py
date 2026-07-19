#!/usr/bin/env python3
"""Campaign-owned ComfyUI backend lifecycle with fail-closed Linux proofs.

The formal campaign must launch the backend it tests.  This module accepts
only canonical, caller-supplied paths and a numeric loopback origin, launches
one new process group through :class:`CampaignCgroup`, proves the resulting
process/listener/readiness authority, and tears down the complete backend
process group.  It deliberately has no external-PID adoption or shell entry
point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import uuid

from scripts.testing.campaign_cgroup import capture_process_identity
from scripts.testing.campaign_comfyui_sandbox import (
    HOST_TRANSITION_SCHEMA_VERSION,
    SANDBOX_PROOF_SCHEMA_VERSION,
)


COMFYUI_BACKEND_LIFECYCLE_SCHEMA_VERSION = "hackme.campaign-comfyui-backend-lifecycle.v1"
COMFYUI_BACKEND_READY_SCHEMA_VERSION = "hackme.campaign-comfyui-backend-ready.v1"
_MAX_READINESS_BYTES = 1024 * 1024
_MAX_PROC_PIDS = 1_000_000
_SAFE_INHERITED_ENVIRONMENT = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "LD_LIBRARY_PATH",
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
    "NVIDIA_DRIVER_CAPABILITIES",
)


class ComfyUIBackendError(RuntimeError):
    """The backend configuration, authority, or cleanup could not be proven."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ComfyUIBackendError(message)


def _canonical_existing_path(
    value: Path,
    *,
    label: str,
    require_directory: bool = False,
    require_executable: bool = False,
) -> Path:
    raw = Path(value).expanduser()
    _require(raw.is_absolute(), f"{label} must be absolute")
    _require("\x00" not in str(raw), f"{label} contains a NUL byte")
    try:
        resolved = raw.resolve(strict=True)
        metadata = raw.lstat()
    except Exception as exc:
        raise ComfyUIBackendError(
            f"{label} is unavailable: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(raw == resolved, f"{label} must be its exact canonical realpath")
    _require(not stat.S_ISLNK(metadata.st_mode), f"{label} cannot be a symlink")
    if require_directory:
        _require(stat.S_ISDIR(metadata.st_mode), f"{label} must be a directory")
    else:
        _require(stat.S_ISREG(metadata.st_mode), f"{label} must be a regular file")
    if require_executable:
        _require(os.access(raw, os.X_OK), f"{label} is not executable")
    return raw


def _normalise_cgroup_path(value: str, *, label: str) -> str:
    raw = str(value or "").strip()
    _require(raw.startswith("/"), f"{label} is not absolute")
    parts = raw.split("/")
    _require(".." not in parts and "\x00" not in raw, f"{label} is unsafe")
    result = "/" + "/".join(part for part in parts if part not in {"", "."})
    return result.rstrip("/") or "/"


def _models_binding(working_root: Path, models_root: Path) -> dict[str, Any]:
    entry = working_root / "models"
    try:
        metadata = entry.lstat()
        resolved = entry.resolve(strict=True)
    except Exception as exc:
        raise ComfyUIBackendError(
            f"ComfyUI working_root/models is unavailable: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(not stat.S_ISLNK(metadata.st_mode), "ComfyUI working_root/models cannot be a symlink")
    _require(stat.S_ISDIR(metadata.st_mode), "ComfyUI working_root/models must be a directory")
    _require(
        resolved == models_root,
        "ComfyUI models root must be exactly working_root/models",
    )
    return {
        "entry_path": str(entry),
        "realpath": str(resolved),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "symlink": False,
        "ok": True,
    }


def _read_tcp_listeners(proc_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for filename, family in (("tcp", "ipv4"), ("tcp6", "ipv6")):
        path = proc_root / "net" / filename
        try:
            lines = path.read_text(encoding="ascii").splitlines()[1:]
        except Exception as exc:
            raise ComfyUIBackendError(
                f"cannot inspect {family} TCP listeners: {exc}"
            ) from exc
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                address_hex, port_hex = fields[1].split(":", 1)
                if family == "ipv4":
                    address = str(
                        ipaddress.IPv4Address(bytes.fromhex(address_hex)[::-1])
                    )
                else:
                    raw = bytes.fromhex(address_hex)
                    network_order = b"".join(
                        raw[offset : offset + 4][::-1]
                        for offset in range(0, 16, 4)
                    )
                    address = str(ipaddress.IPv6Address(network_order))
                port = int(port_hex, 16)
                inode = int(fields[9])
            except Exception:
                continue
            rows.append({
                "family": family,
                "address": address,
                "port": port,
                "inode": inode,
            })
    return rows


def _pid_socket_inodes(proc_root: Path, pid: int) -> set[int]:
    result: set[int] = set()
    fd_root = proc_root / str(pid) / "fd"
    try:
        entries = list(fd_root.iterdir())
    except Exception:
        return result
    for entry in entries:
        try:
            target = os.readlink(entry)
        except Exception:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            try:
                result.add(int(target[8:-1]))
            except ValueError:
                pass
    return result


def _system_stats_payload(url: str, timeout: float) -> Mapping[str, Any]:
    opener = build_opener(ProxyHandler({}))
    with opener.open(url, timeout=max(0.05, float(timeout))) as response:
        _require(
            int(response.status) == 200,
            "ComfyUI /system_stats did not return HTTP 200",
        )
        body = response.read(_MAX_READINESS_BYTES + 1)
    _require(
        len(body) <= _MAX_READINESS_BYTES,
        "ComfyUI /system_stats response is too large",
    )
    try:
        payload = json.loads(body)
    except Exception as exc:
        raise ComfyUIBackendError(
            f"ComfyUI /system_stats is not JSON: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(
        isinstance(payload, Mapping),
        "ComfyUI /system_stats must be a JSON object",
    )
    return payload


def _validate_system_stats_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    system = payload.get("system")
    devices = payload.get("devices")
    _require(isinstance(system, Mapping), "ComfyUI /system_stats has no system object")
    _require(
        isinstance(system.get("python_version"), str)
        and bool(str(system.get("python_version") or "").strip()),
        "ComfyUI /system_stats has no Python version authority",
    )
    _require(isinstance(devices, list), "ComfyUI /system_stats has no devices list")
    _require(bool(devices), "ComfyUI /system_stats has no device authority")
    _require(
        all(
            isinstance(device, Mapping)
            and bool(str(device.get("name") or "").strip())
            for device in devices
        ),
        "ComfyUI /system_stats devices are malformed",
    )
    return {
        "system_fields": sorted(str(key) for key in system),
        "device_count": len(devices),
        "device_names": [str(device.get("name")) for device in devices],
        "ok": True,
    }


def read_stable_ready_receipt(
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the immutable ready receipt through one pinned nofollow fd."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except Exception as exc:
        raise ComfyUIBackendError(
            f"cannot open ready receipt with nofollow: {exc.__class__.__name__}: {exc}"
        ) from exc
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode), "ready receipt is not a regular file")
        _require(int(before.st_nlink) == 1, "ready receipt is hard-linked")
        _require(int(before.st_uid) == os.geteuid(), "ready receipt owner differs")
        _require(
            stat.S_IMODE(before.st_mode) & 0o077 == 0,
            "ready receipt permissions are not private",
        )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            _require(total <= _MAX_READINESS_BYTES, "ready receipt is too large")
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    path_after = path.lstat()
    identity_before = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
        int(before.st_ctime_ns),
    )
    identity_after = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_ctime_ns),
    )
    identity_path = (
        int(path_after.st_dev),
        int(path_after.st_ino),
        int(path_after.st_size),
        int(path_after.st_mtime_ns),
        int(path_after.st_ctime_ns),
    )
    _require(
        identity_before == identity_after == identity_path,
        "ready receipt changed during nofollow hash readback",
    )
    authority = {
        "sha256": digest.hexdigest(),
        "device": int(after.st_dev),
        "inode": int(after.st_ino),
        "size": int(after.st_size),
        "mode": stat.S_IMODE(after.st_mode),
        "uid": int(after.st_uid),
        "gid": int(after.st_gid),
        "link_count": int(after.st_nlink),
        "mtime_ns": int(after.st_mtime_ns),
        "ctime_ns": int(after.st_ctime_ns),
        "nofollow_stable": True,
    }
    try:
        payload = json.loads(b"".join(chunks))
    except Exception as exc:
        raise ComfyUIBackendError(
            f"ready receipt is not valid JSON: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(isinstance(payload, dict), "ready receipt must be a JSON object")
    return payload, authority


def _stable_receipt_authority(path: Path) -> dict[str, Any]:
    _payload, authority = read_stable_ready_receipt(path)
    return authority


def _atomic_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    encoded = (
        json.dumps(dict(payload), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            _require(written > 0, "private JSON write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _write_private_once(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    encoded = (
        json.dumps(dict(payload), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            _require(written > 0, "private receipt write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o400)


@dataclass(frozen=True)
class ComfyUIBackendConfig:
    python_executable: Path
    main_path: Path
    working_root: Path
    models_root: Path
    api_url: str
    port: int
    readiness_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 0.25

    def __post_init__(self) -> None:
        python = _canonical_existing_path(
            self.python_executable,
            label="ComfyUI Python executable",
            require_executable=True,
        )
        working = _canonical_existing_path(
            self.working_root,
            label="ComfyUI working root",
            require_directory=True,
        )
        main = _canonical_existing_path(self.main_path, label="ComfyUI main.py")
        models = _canonical_existing_path(
            self.models_root,
            label="ComfyUI models root",
            require_directory=True,
        )
        _require(main.name == "main.py", "ComfyUI entry point must be named main.py")
        _require(main.parent == working, "ComfyUI main.py must be directly inside its working root")
        _models_binding(working, models)
        _require(
            isinstance(self.port, int)
            and not isinstance(self.port, bool)
            and 1024 <= self.port <= 65535,
            "ComfyUI port must be an unprivileged TCP port",
        )
        parsed = urlsplit(str(self.api_url).strip())
        _require(parsed.scheme == "http", "ComfyUI API URL must use http")
        _require(
            not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"},
            "ComfyUI API URL must be an origin without credentials/path/query/fragment",
        )
        try:
            address = ipaddress.ip_address(str(parsed.hostname or ""))
        except ValueError as exc:
            raise ComfyUIBackendError(
                "ComfyUI API host must be a numeric loopback address"
            ) from exc
        _require(
            isinstance(address, ipaddress.IPv4Address) and address.is_loopback,
            "ComfyUI API host must be an IPv4 loopback address",
        )
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise ComfyUIBackendError("ComfyUI API URL has an invalid port") from exc
        _require(parsed_port == self.port, "ComfyUI API URL and explicit port differ")
        timeout = float(self.readiness_timeout_seconds)
        poll = float(self.poll_interval_seconds)
        _require(0.05 <= timeout <= 1800.0, "ComfyUI readiness timeout is outside the reviewed range")
        _require(0.01 <= poll <= 5.0, "ComfyUI poll interval is outside the reviewed range")
        object.__setattr__(self, "python_executable", python)
        object.__setattr__(self, "main_path", main)
        object.__setattr__(self, "working_root", working)
        object.__setattr__(self, "models_root", models)
        object.__setattr__(self, "api_url", f"http://{address.compressed}:{self.port}")
        object.__setattr__(self, "readiness_timeout_seconds", timeout)
        object.__setattr__(self, "poll_interval_seconds", poll)

    @property
    def host(self) -> str:
        return str(urlsplit(self.api_url).hostname or "")


def validate_live_comfyui_backend_authority(
    backend_contract: Mapping[str, Any],
    ready_payload: Mapping[str, Any],
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    readiness_timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    """Independently re-prove a supervisor receipt from the live kernel state."""

    _require(isinstance(backend_contract, Mapping), "ComfyUI backend contract is malformed")
    _require(isinstance(ready_payload, Mapping), "ComfyUI ready receipt is malformed")
    try:
        parsed_port = urlsplit(str(backend_contract.get("api_url") or "")).port
    except ValueError as exc:
        raise ComfyUIBackendError("ComfyUI backend contract has an invalid URL") from exc
    _require(isinstance(parsed_port, int), "ComfyUI backend contract has no explicit port")
    config = ComfyUIBackendConfig(
        python_executable=Path(str(backend_contract.get("python_executable") or "")),
        main_path=Path(str(backend_contract.get("main_path") or "")),
        working_root=Path(str(backend_contract.get("working_root") or "")),
        models_root=Path(str(backend_contract.get("models_root") or "")),
        api_url=str(backend_contract.get("api_url") or ""),
        port=parsed_port,
    )
    expected_command = [
        str(config.python_executable),
        str(config.main_path),
        "--listen",
        config.host,
        "--port",
        str(config.port),
        "--disable-auto-launch",
    ]
    command_sha256 = hashlib.sha256(
        b"\0".join(value.encode() for value in expected_command)
    ).hexdigest()
    _require(
        backend_contract.get("command") == expected_command
        and backend_contract.get("command_sha256") == command_sha256,
        "ComfyUI backend contract command is not the fixed reviewed command",
    )
    for name, expected in (
        ("api_url", config.api_url),
        ("python_executable", str(config.python_executable)),
        ("main_path", str(config.main_path)),
        ("working_root", str(config.working_root)),
        ("models_root", str(config.models_root)),
        ("command", expected_command),
        ("command_sha256", command_sha256),
    ):
        _require(
            ready_payload.get(name) == expected,
            f"ComfyUI ready receipt {name} differs from its contract",
        )

    process = ready_payload.get("process")
    placement = ready_payload.get("placement")
    managed_leaf = ready_payload.get("managed_leaf")
    leaf_snapshot = ready_payload.get("managed_leaf_state")
    listener_snapshot = ready_payload.get("listener")
    readiness_snapshot = ready_payload.get("readiness")
    models_snapshot = ready_payload.get("models_binding")
    confinement = ready_payload.get("confinement")
    sandbox_snapshot = ready_payload.get("sandbox")
    sandbox_live_snapshot = ready_payload.get("sandbox_live")
    launcher_snapshot = ready_payload.get("launcher")
    for label, value in (
        ("process", process),
        ("placement", placement),
        ("managed leaf", managed_leaf),
        ("managed leaf state", leaf_snapshot),
        ("listener", listener_snapshot),
        ("readiness", readiness_snapshot),
        ("models binding", models_snapshot),
        ("confinement", confinement),
        ("sandbox", sandbox_snapshot),
        ("sandbox live", sandbox_live_snapshot),
        ("launcher", launcher_snapshot),
    ):
        _require(isinstance(value, Mapping), f"ComfyUI ready {label} is malformed")

    backend_pid = backend_contract.get("backend_pid")
    backend_start_ticks = backend_contract.get("backend_start_ticks")
    backend_boot_id = str(backend_contract.get("backend_boot_id") or "")
    backend_cgroup = _normalise_cgroup_path(
        str(backend_contract.get("backend_cgroup") or ""),
        label="ComfyUI backend cgroup",
    )
    launcher_pid = backend_contract.get("launcher_pid")
    process_group = backend_contract.get("process_group")
    _require(
        isinstance(backend_pid, int)
        and not isinstance(backend_pid, bool)
        and backend_pid > 0
        and isinstance(backend_start_ticks, int)
        and not isinstance(backend_start_ticks, bool)
        and backend_start_ticks > 0
        and bool(backend_boot_id),
        "ComfyUI backend process identity is malformed",
    )
    _require(
        isinstance(launcher_pid, int)
        and not isinstance(launcher_pid, bool)
        and launcher_pid > 0
        and process_group == launcher_pid
        and launcher_snapshot.get("pid") == launcher_pid
        and launcher_snapshot.get("process_group") == process_group
        and launcher_snapshot.get("session") == process_group
        and launcher_snapshot.get("ok") is True,
        "ComfyUI sandbox launcher receipt is malformed",
    )
    _require(
        process.get("pid") == backend_pid
        and process.get("start_ticks") == backend_start_ticks
        and process.get("boot_id") == backend_boot_id
        and process.get("cgroup_path") == backend_cgroup
        and process.get("process_group") == process_group
        and process.get("no_new_privileges") is True
        and process.get("seccomp_mode") == 2
        and CampaignComfyUIBackend._zero_capability_sets(
            process.get("capability_sets")
        )
        and isinstance(process.get("namespace_pids"), list)
        and len(process.get("namespace_pids")) >= 2
        and process.get("namespace_pids")[0] == backend_pid
        and process.get("executable") == str(config.python_executable)
        and process.get("cwd") == str(config.working_root)
        and process.get("ok") is True,
        "ComfyUI ready process proof is incomplete",
    )
    _require(
        placement.get("pid") == backend_pid
        and placement.get("start_ticks") == backend_start_ticks
        and placement.get("campaign_cgroup") == backend_cgroup
        and placement.get("ok") is True,
        "ComfyUI ready placement proof is incomplete",
    )
    _require(
        managed_leaf.get("cgroup_path") == backend_cgroup
        and "delegated" not in managed_leaf
        and managed_leaf.get("subtree_controllers_enabled") is False
        and managed_leaf.get("descendant_cgroups") == 0
        and managed_leaf.get("host_leaf_state_before_sandbox") == "pending_sandbox"
        and managed_leaf.get("workload_delegation_capability") is False
        and managed_leaf.get("ok") is True
        and isinstance(managed_leaf.get("device"), int)
        and isinstance(managed_leaf.get("inode"), int),
        "ComfyUI managed leaf receipt is incomplete",
    )
    leaf_snapshot_pids = leaf_snapshot.get("pids")
    _require(
        isinstance(leaf_snapshot_pids, list)
        and backend_pid in leaf_snapshot_pids
        and leaf_snapshot.get("cgroup_path") == backend_cgroup
        and leaf_snapshot.get("populated") == 1
        and leaf_snapshot.get("consistent") is True
        and leaf_snapshot.get(
            "topology_intact", leaf_snapshot.get("delegation_intact")
        ) is True
        and leaf_snapshot.get("descendant_cgroups") == 0
        and leaf_snapshot.get("subtree_control") == []
        and leaf_snapshot.get("workload_delegation_capability") is False
        and leaf_snapshot.get("ok") is True,
        "ComfyUI managed leaf state receipt is incomplete",
    )
    sandbox_launcher = confinement.get("launcher")
    sandbox_transition = confinement.get("host_transition")
    sandbox_mounts = confinement.get("mounts")
    sandbox_privileges = confinement.get("privileges")
    sandbox_delegation = confinement.get("workload_delegation_confinement")
    sandbox_denial = confinement.get("cgroup_write_denial")
    _require(
        backend_contract.get("confinement") == confinement
        and backend_contract.get("sandbox") == confinement
        and sandbox_snapshot == confinement
        and confinement.get("schema_version") == SANDBOX_PROOF_SCHEMA_VERSION
        and confinement.get("actual_execution") is True
        and confinement.get("simulated") is False
        and confinement.get("adopted_external_process") is False
        and confinement.get("shell") is False
        and confinement.get("fixed_command") == expected_command
        and confinement.get("environment_keys")
        == ready_payload.get("environment_keys")
        and confinement.get("expected_host_cgroup_path") == backend_cgroup
        and isinstance(sandbox_launcher, Mapping)
        and sandbox_launcher.get("host_pid") == launcher_pid
        and sandbox_launcher.get("host_process_group") == process_group
        and sandbox_launcher.get("host_session") == process_group
        and isinstance(sandbox_transition, Mapping)
        and sandbox_transition.get("schema_version") == HOST_TRANSITION_SCHEMA_VERSION
        and sandbox_transition.get("pid") == launcher_pid
        and sandbox_transition.get("cgroup_path") == backend_cgroup
        and sandbox_transition.get("ok") is True
        and isinstance(sandbox_mounts, Mapping)
        and sandbox_mounts.get("cgroup_namespace_path") == "/"
        and sandbox_mounts.get("leaf_kernel_objects_match") is True
        and sandbox_mounts.get("ok") is True
        and isinstance(sandbox_privileges, Mapping)
        and CampaignComfyUIBackend._zero_capability_sets(
            sandbox_privileges.get("capability_sets")
        )
        and sandbox_privileges.get("securebits_locked") is True
        and sandbox_privileges.get("no_new_privileges") is True
        and sandbox_privileges.get("seccomp", {}).get("mode") == 2
        and isinstance(sandbox_denial, Mapping)
        and sandbox_denial.get("write_open_succeeded") is False
        and sandbox_denial.get("errno") in {errno.EPERM, errno.EACCES, errno.EROFS}
        and sandbox_denial.get("ok") is True
        and confinement.get("workload_delegation_capability") is False
        and isinstance(sandbox_delegation, Mapping)
        and sandbox_delegation.get("workload_delegation_capability") is False
        and sandbox_delegation.get("namespace_rooted_cgroup2") is True
        and sandbox_delegation.get("cgroup2_read_only") is True
        and sandbox_delegation.get("capability_sets_zero") is True
        and sandbox_delegation.get("namespace_and_mount_syscalls_denied") is True
        and sandbox_delegation.get("ok") is True
        and confinement.get("proof_written_before_exec") is True
        and confinement.get("outer_launcher_preserves_process_group") is True
        and confinement.get("reaper_preserves_wait_status") is True
        and confinement.get("ok") is True,
        "ComfyUI namespace sandbox receipt is incomplete",
    )
    expected_models = _models_binding(config.working_root, config.models_root)
    _require(
        all(models_snapshot.get(name) == value for name, value in expected_models.items()),
        "ComfyUI model directory identity differs from the ready receipt",
    )
    process_models = process.get("models_binding")
    _require(
        isinstance(process_models, Mapping)
        and all(process_models.get(name) == value for name, value in expected_models.items()),
        "ComfyUI process model binding differs from the filesystem",
    )
    allowed_environment_keys = set(_SAFE_INHERITED_ENVIRONMENT) | {
        "PYTHONUNBUFFERED",
        "HACKME_CAMPAIGN_COMFYUI_INSTANCE_ID",
        "HACKME_CAMPAIGN_COMFYUI_API_URL",
        "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT",
    }
    environment_keys = ready_payload.get("environment_keys")
    _require(
        isinstance(environment_keys, list)
        and len(environment_keys) == len(set(environment_keys))
        and set(environment_keys) <= allowed_environment_keys
        and {
            "PYTHONUNBUFFERED",
            "HACKME_CAMPAIGN_COMFYUI_INSTANCE_ID",
            "HACKME_CAMPAIGN_COMFYUI_API_URL",
            "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT",
        }
        <= set(environment_keys),
        "ComfyUI ready environment allowlist is invalid",
    )

    proc_root = Path(proc_root).resolve(strict=True)
    live_identity = capture_process_identity(proc_root, backend_pid)
    _require(
        live_identity.start_ticks == backend_start_ticks
        and live_identity.boot_id == backend_boot_id
        and _normalise_cgroup_path(
            live_identity.cgroup_path,
            label="live ComfyUI cgroup",
        )
        == backend_cgroup,
        "live ComfyUI process identity differs from the receipt",
    )
    live_executable = (proc_root / str(backend_pid) / "exe").resolve(strict=True)
    live_cwd = (proc_root / str(backend_pid) / "cwd").resolve(strict=True)
    live_command = [
        value.decode("utf-8", errors="surrogateescape")
        for value in (proc_root / str(backend_pid) / "cmdline").read_bytes().split(b"\0")
        if value
    ]
    live_status = {
        name: value.strip()
        for row in (proc_root / str(backend_pid) / "status")
        .read_text(encoding="utf-8")
        .splitlines()
        if ":" in row
        for name, value in [row.split(":", 1)]
    }
    live_capabilities = {
        name: str(live_status.get(name) or "").lower()
        for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    }
    live_namespace_pids = [
        int(value) for value in str(live_status.get("NSpid") or "").split()
    ]
    live_namespace_links = {
        name: os.readlink(proc_root / str(backend_pid) / "ns" / name)
        for name in ("user", "mnt", "cgroup", "pid")
    }
    _require(
        live_executable == config.python_executable
        and live_cwd == config.working_root
        and live_command == expected_command
        and os.getpgid(backend_pid) == process_group
        and live_status.get("NoNewPrivs") == "1",
        "live ComfyUI executable/cwd/argv/process-group proof differs",
    )
    _require(
        live_status.get("Seccomp") == "2"
        and CampaignComfyUIBackend._zero_capability_sets(live_capabilities)
        and live_namespace_pids == process.get("namespace_pids")
        and live_namespace_links == process.get("namespace_links")
        and live_namespace_links == confinement.get("namespace_links"),
        "live ComfyUI sandbox privilege/namespace proof differs",
    )
    launcher_identity = capture_process_identity(proc_root, launcher_pid)
    _require(
        launcher_identity.cgroup_path == backend_cgroup
        and launcher_identity.start_ticks == sandbox_transition.get("start_ticks")
        and launcher_identity.boot_id == sandbox_transition.get("boot_id")
        and os.getpgid(launcher_pid) == process_group
        and os.getsid(launcher_pid) == process_group,
        "live ComfyUI sandbox launcher identity differs",
    )

    cgroup_root = Path(cgroup_root).resolve(strict=True)
    leaf_path = cgroup_root / backend_cgroup.lstrip("/")
    resolved_leaf = leaf_path.resolve(strict=True)
    _require(
        resolved_leaf == leaf_path
        and resolved_leaf != cgroup_root
        and cgroup_root in resolved_leaf.parents,
        "live ComfyUI cgroup leaf escaped its root",
    )
    leaf_metadata = leaf_path.lstat()
    _require(
        stat.S_ISDIR(leaf_metadata.st_mode)
        and not stat.S_ISLNK(leaf_metadata.st_mode)
        and int(leaf_metadata.st_dev) == managed_leaf.get("device")
        and int(leaf_metadata.st_ino) == managed_leaf.get("inode"),
        "live ComfyUI cgroup leaf identity differs",
    )
    procs_files = list(leaf_path.rglob("cgroup.procs"))
    _require(
        procs_files == [leaf_path / "cgroup.procs"],
        "live ComfyUI cgroup leaf contains delegated descendants",
    )
    live_leaf_pids = {
        int(row)
        for row in (leaf_path / "cgroup.procs").read_text(encoding="utf-8").splitlines()
        if row.strip()
    }
    events = {
        fields[0]: int(fields[1])
        for row in (leaf_path / "cgroup.events").read_text(encoding="utf-8").splitlines()
        if len(fields := row.split()) == 2
    }
    _require(
        backend_pid in live_leaf_pids
        and launcher_pid in live_leaf_pids
        and int(events.get("populated", -1)) == 1
        and not (leaf_path / "cgroup.subtree_control").read_text(encoding="utf-8").strip(),
        "live ComfyUI cgroup PID/populated/delegation authority is invalid",
    )
    _require(
        sandbox_live_snapshot.get("launcher_pid") == launcher_pid
        and sandbox_live_snapshot.get("backend_host_pid") == backend_pid
        and sandbox_live_snapshot.get("process_group") == process_group
        and sandbox_live_snapshot.get("namespace_links") == live_namespace_links
        and sandbox_live_snapshot.get("namespace_pid") == live_namespace_pids[-1]
        and sandbox_live_snapshot.get("leaf_pids") == sorted(live_leaf_pids)
        and sandbox_live_snapshot.get("workload_delegation_capability") is False
        and sandbox_live_snapshot.get("ok") is True,
        "live ComfyUI sandbox receipt differs from the kernel",
    )

    listeners_before = [
        row
        for row in _read_tcp_listeners(proc_root)
        if row["port"] == config.port
    ]
    _require(
        len(listeners_before) == 1
        and listeners_before[0]["family"] == "ipv4"
        and listeners_before[0]["address"] == config.host,
        "live ComfyUI listener is missing, ambiguous, or not loopback-only",
    )
    listener_inode = int(listeners_before[0]["inode"])
    listener_owners = sorted(
        pid
        for pid in live_leaf_pids
        if listener_inode in _pid_socket_inodes(proc_root, pid)
    )
    _require(listener_owners, "live ComfyUI listener is not owned by its cgroup leaf")
    _require(
        listener_snapshot.get("family") == "ipv4"
        and listener_snapshot.get("address") == config.host
        and listener_snapshot.get("port") == config.port
        and listener_snapshot.get("socket_inode") == listener_inode
        and listener_snapshot.get("owner_pids") == listener_owners
        and listener_snapshot.get("loopback_only") is True
        and listener_snapshot.get("ok") is True
        and set(listener_owners) <= set(leaf_snapshot_pids),
        "live ComfyUI listener differs from the ready receipt",
    )
    live_stats = _validate_system_stats_payload(
        _system_stats_payload(
            f"{config.api_url}/system_stats",
            readiness_timeout_seconds,
        )
    )
    listeners_after = [
        row
        for row in _read_tcp_listeners(proc_root)
        if row["port"] == config.port
    ]
    _require(
        listeners_after == listeners_before
        and ready_payload.get("listener_stable_across_readiness") is True,
        "live ComfyUI listener changed across runner readiness verification",
    )
    _require(
        readiness_snapshot.get("endpoint") == f"{config.api_url}/system_stats"
        and readiness_snapshot.get("ok") is True
        and "python_version" in set(readiness_snapshot.get("system_fields") or [])
        and isinstance(readiness_snapshot.get("device_count"), int)
        and readiness_snapshot.get("device_count") > 0,
        "ComfyUI ready readiness proof is incomplete",
    )
    return {
        "pid": backend_pid,
        "launcher_pid": launcher_pid,
        "process_group": process_group,
        "start_ticks": backend_start_ticks,
        "boot_id": backend_boot_id,
        "cgroup_path": backend_cgroup,
        "cgroup_leaf_device": int(leaf_metadata.st_dev),
        "cgroup_leaf_inode": int(leaf_metadata.st_ino),
        "leaf_pids": sorted(live_leaf_pids),
        "listener": {
            **listeners_after[0],
            "owner_pids": listener_owners,
        },
        "readiness": live_stats,
        "command_sha256": command_sha256,
        "workload_delegation_capability": False,
        "ok": True,
    }


class CampaignComfyUIBackend:
    """Launch, prove, monitor, and stop one campaign-owned backend."""

    def __init__(
        self,
        *,
        config: ComfyUIBackendConfig,
        campaign_cgroup: Any,
        evidence_root: Path,
        proc_root: Path = Path("/proc"),
        popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        readiness_probe: Callable[[str, float], Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.cgroup = campaign_cgroup
        self.evidence_root = Path(evidence_root).expanduser().resolve(strict=False)
        self.proc_root = Path(proc_root).expanduser().resolve(strict=False)
        self.popen = popen
        self.readiness_probe = readiness_probe or self._http_system_stats
        self.instance_id = f"comfyui:{uuid.uuid4().hex}"
        self.process: subprocess.Popen[Any] | None = None
        self.process_identity: Any | None = None
        self.launcher_identity: Any | None = None
        self.backend_pid = 0
        self.process_group = 0
        self.leaf_role = "comfyui"
        self.leaf_evidence: dict[str, Any] = {}
        self.leaf_cgroup_path = ""
        self.log_handle: Any | None = None
        self.ready = False
        self.events: list[dict[str, Any]] = []
        self.lifecycle_path = self.evidence_root / "lifecycle.json"
        self.ready_receipt_path = self.evidence_root / "ready.json"
        self.stdout_path = self.evidence_root / "backend.stdout"
        self._ready_evidence: dict[str, Any] = {}
        self.confinement_evidence: dict[str, Any] = {}

    def command(self) -> tuple[str, ...]:
        return (
            str(self.config.python_executable),
            str(self.config.main_path),
            "--listen",
            self.config.host,
            "--port",
            str(self.config.port),
            "--disable-auto-launch",
        )

    def controlled_environment(self) -> dict[str, str]:
        environment = {
            name: str(os.environ[name])
            for name in _SAFE_INHERITED_ENVIRONMENT
            if str(os.environ.get(name) or "")
        }
        environment.update({
            "PYTHONUNBUFFERED": "1",
            "HACKME_CAMPAIGN_COMFYUI_INSTANCE_ID": self.instance_id,
            "HACKME_CAMPAIGN_COMFYUI_API_URL": self.config.api_url,
            "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT": str(self.config.models_root),
        })
        return environment

    def runner_environment(self) -> dict[str, str]:
        _require(self.ready and self.process_identity is not None, "ComfyUI backend is not ready")
        return {
            "HACKME_CAMPAIGN_COMFYUI_API_URL": self.config.api_url,
            "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT": str(self.config.models_root),
            "HACKME_CAMPAIGN_COMFYUI_BACKEND_PID": str(self.process_identity.pid),
        }

    def _sandbox_write_roots(self) -> tuple[Path, ...]:
        candidates = [
            self.config.working_root,
            Path("/tmp"),
            Path("/var/tmp"),
            Path("/dev"),
        ]
        result: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                metadata = candidate.lstat()
            except Exception:
                continue
            if (
                candidate != resolved
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or resolved in seen
            ):
                continue
            seen.add(resolved)
            result.append(resolved)
        _require(
            self.config.working_root in result and Path("/tmp") in result,
            "ComfyUI sandbox write roots are incomplete",
        )
        return tuple(result)

    @staticmethod
    def _path_identity(path: Path) -> dict[str, Any]:
        metadata = path.lstat()
        return {
            "path": str(path),
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "mode": int(metadata.st_mode),
            "uid": int(metadata.st_uid),
            "gid": int(metadata.st_gid),
        }

    @staticmethod
    def _zero_capability_sets(value: Any) -> bool:
        return bool(
            isinstance(value, Mapping)
            and value
            and all(
                item == "0000000000000000"
                for item in value.values()
            )
        )

    def _read_sandbox_pipe(
        self,
        descriptor: int,
        *,
        nonce: str,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, float(timeout))
        chunks: list[bytes] = []
        total = 0
        try:
            while time.monotonic() < deadline:
                readable, _writable, _errors = select.select(
                    [descriptor], [], [], min(0.25, deadline - time.monotonic())
                )
                if not readable:
                    if self.process is not None and self.process.poll() is not None:
                        break
                    continue
                chunk = os.read(descriptor, 32 * 1024 - total + 1)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                _require(total <= 64 * 1024, "ComfyUI sandbox proof is too large")
                if b"\n" in chunk:
                    break
        finally:
            os.close(descriptor)
        try:
            payload = json.loads(b"".join(chunks))
        except Exception as exc:
            raise ComfyUIBackendError(
                f"ComfyUI sandbox proof is invalid: {exc.__class__.__name__}: {exc}"
            ) from exc
        _require(isinstance(payload, dict), "ComfyUI sandbox proof must be an object")
        _require(self.process is not None, "ComfyUI sandbox launcher disappeared")
        command = list(self.command())
        command_hash = hashlib.sha256(
            json.dumps(command, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        launcher = payload.get("launcher")
        transition = payload.get("host_transition")
        namespace = payload.get("namespace")
        mounts = payload.get("mounts")
        landlock = payload.get("landlock")
        denial = payload.get("cgroup_write_denial")
        privileges = payload.get("privileges")
        reaper = payload.get("reaper")
        reaper_privileges = (
            reaper.get("privileges") if isinstance(reaper, Mapping) else None
        )
        delegation = payload.get("workload_delegation_confinement")
        descriptor_contract = payload.get("descriptor_contract")
        payload_process = payload.get("payload")
        expected_root_records = [
            self._path_identity(path) for path in self._sandbox_write_roots()
        ]
        expected_environment_keys = sorted(self.controlled_environment())
        denied_syscalls = (
            privileges.get("seccomp", {}).get("unconditional_denied_syscalls")
            if isinstance(privileges, Mapping)
            and isinstance(privileges.get("seccomp"), Mapping)
            else None
        )
        hidden = mounts.get("hidden_runtime_paths") if isinstance(mounts, Mapping) else None
        cgroup_mount = mounts.get("cgroup2") if isinstance(mounts, Mapping) else None
        proc_mount = mounts.get("proc") if isinstance(mounts, Mapping) else None
        proof_pipe = (
            descriptor_contract.get("proof_pipe")
            if isinstance(descriptor_contract, Mapping)
            else None
        )
        stdio = (
            descriptor_contract.get("stdio")
            if isinstance(descriptor_contract, Mapping)
            else None
        )
        _require(
            payload.get("schema_version") == SANDBOX_PROOF_SCHEMA_VERSION
            and payload.get("nonce") == nonce
            and payload.get("actual_execution") is True
            and payload.get("simulated") is False
            and payload.get("adopted_external_process") is False
            and payload.get("shell") is False
            and payload.get("fixed_command") == command
            and payload.get("fixed_command_sha256") == command_hash
            and payload.get("environment_keys") == expected_environment_keys
            and payload.get("expected_host_cgroup_path") == self.leaf_cgroup_path
            and payload.get("allowed_write_roots") == expected_root_records
            and isinstance(launcher, Mapping)
            and launcher.get("host_pid") == self.process.pid
            and launcher.get("host_process_group") == self.process.pid
            and launcher.get("host_session") == self.process.pid
            and launcher.get("process_group_leader") is True
            and isinstance(transition, Mapping)
            and transition.get("schema_version") == HOST_TRANSITION_SCHEMA_VERSION
            and transition.get("nonce") == nonce
            and transition.get("pid") == self.process.pid
            and transition.get("cgroup_path") == self.leaf_cgroup_path
            and transition.get("allowed_write_roots") == expected_root_records
            and transition.get("ok") is True
            and isinstance(namespace, Mapping)
            and namespace.get("setgroups") == "deny"
            and namespace.get("uid_map") == [[0, os.geteuid(), 1]]
            and namespace.get("gid_map") == [[0, os.getegid(), 1]]
            and namespace.get("ok") is True
            and isinstance(proc_mount, Mapping)
            and proc_mount.get("filesystem_type") == "proc"
            and proc_mount.get("root") == "/"
            and isinstance(cgroup_mount, Mapping)
            and cgroup_mount.get("filesystem_type") == "cgroup2"
            and cgroup_mount.get("root") == "/"
            and "ro" in set(cgroup_mount.get("mount_options") or [])
            and "nsdelegate" in set(cgroup_mount.get("super_options") or [])
            and mounts.get("cgroup_namespace_path") == "/"
            and mounts.get("leaf_kernel_objects_match") is True
            and isinstance(hidden, list)
            and {row.get("path") for row in hidden if isinstance(row, Mapping)}
            == {"/run", "/mnt/wslg/run"}
            and all(row.get("hidden") is True for row in hidden if isinstance(row, Mapping))
            and mounts.get("ok") is True
            and isinstance(landlock, Mapping)
            and isinstance(landlock.get("abi"), int)
            and landlock.get("abi") >= 3
            and landlock.get("allowed_write_roots") == expected_root_records
            and landlock.get("irreversible") is True
            and landlock.get("ok") is True
            and isinstance(denial, Mapping)
            and denial.get("write_open_succeeded") is False
            and denial.get("errno") in {errno.EPERM, errno.EACCES, errno.EROFS}
            and denial.get("ok") is True
            and self._zero_capability_sets(
                privileges.get("capability_sets") if isinstance(privileges, Mapping) else None
            )
            and privileges.get("securebits_locked") is True
            and privileges.get("no_new_privileges") is True
            and privileges.get("seccomp", {}).get("mode") == 2
            and privileges.get("seccomp", {}).get("ok") is True
            and isinstance(denied_syscalls, Mapping)
            and {
                "io_uring_setup", "io_uring_enter", "io_uring_register",
                "setns", "unshare", "mount", "umount2", "move_mount",
                "mount_setattr", "pidfd_getfd", "ptrace", "recvmsg",
            } <= set(denied_syscalls)
            and isinstance(reaper, Mapping)
            and reaper.get("trusted_pid1_reaper") is True
            and reaper.get("namespace_pid") == 1
            and reaper.get("open_fds_after_sync") == [0, 1, 2]
            and self._zero_capability_sets(
                reaper_privileges.get("capability_sets")
                if isinstance(reaper_privileges, Mapping)
                else None
            )
            and reaper.get("ok") is True
            and isinstance(payload_process, Mapping)
            and isinstance(payload_process.get("namespace_pid"), int)
            and payload_process.get("namespace_pid") > 1
            and isinstance(descriptor_contract, Mapping)
            and isinstance(proof_pipe, Mapping)
            and proof_pipe.get("is_fifo") is True
            and proof_pipe.get("is_socket") is False
            and isinstance(stdio, list)
            and len(stdio) == 3
            and all(
                isinstance(row, Mapping) and row.get("is_socket") is False
                for row in stdio
            )
            and payload.get("workload_delegation_capability") is False
            and isinstance(delegation, Mapping)
            and delegation.get("workload_delegation_capability") is False
            and delegation.get("namespace_rooted_cgroup2") is True
            and delegation.get("cgroup2_read_only") is True
            and delegation.get("capability_sets_zero") is True
            and delegation.get("namespace_and_mount_syscalls_denied") is True
            and delegation.get("ok") is True
            and payload.get("proof_written_before_exec") is True
            and payload.get("outer_launcher_preserves_process_group") is True
            and payload.get("reaper_preserves_wait_status") is True
            and payload.get("ok") is True,
            "ComfyUI namespace sandbox proof is incomplete: "
            + json.dumps(payload, ensure_ascii=True, sort_keys=True)[:4000],
        )
        return payload

    def _record(self, action: str, **values: Any) -> dict[str, Any]:
        event = {"action": action, "at": _utc_now(), **values}
        self.events.append(event)
        _atomic_private_json(self.lifecycle_path, {
            "schema_version": COMFYUI_BACKEND_LIFECYCLE_SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "api_url": self.config.api_url,
            "models_root": str(self.config.models_root),
            "ready": self.ready,
            "events": self.events,
            "updated_at": _utc_now(),
        })
        return event

    def _http_system_stats(self, url: str, timeout: float) -> Mapping[str, Any]:
        return _system_stats_payload(url, timeout)

    def _proc_pid_paths(self) -> Sequence[Path]:
        rows: list[Path] = []
        for entry in self.proc_root.iterdir():
            if entry.name.isdigit():
                rows.append(entry)
                _require(len(rows) <= _MAX_PROC_PIDS, "procfs PID scan exceeded its safety bound")
        return rows

    def _stat_fields(self, pid: int) -> tuple[int, int]:
        text = (self.proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        tail = text.rsplit(") ", 1)[1].split()
        return int(tail[1]), int(tail[2])  # ppid, process group

    def _process_group_pids(self, process_group: int) -> set[int]:
        result: set[int] = set()
        for path in self._proc_pid_paths():
            try:
                if self._stat_fields(int(path.name))[1] == int(process_group):
                    result.add(int(path.name))
            except Exception:
                continue
        return result

    def _instance_pids(self) -> set[int]:
        marker = f"HACKME_CAMPAIGN_COMFYUI_INSTANCE_ID={self.instance_id}".encode()
        result: set[int] = set()
        for path in self._proc_pid_paths():
            try:
                values = (path / "environ").read_bytes().split(b"\0")
            except Exception:
                continue
            if marker in values:
                result.add(int(path.name))
        return result

    def _tcp_listeners(self) -> list[dict[str, Any]]:
        return _read_tcp_listeners(self.proc_root)

    def _socket_inodes(self, pid: int) -> set[int]:
        return _pid_socket_inodes(self.proc_root, pid)

    def _listener_proof(self, pid: int) -> dict[str, Any]:
        on_port = [row for row in self._tcp_listeners() if row["port"] == self.config.port]
        exact = [row for row in on_port if row["address"] == self.config.host]
        _require(len(on_port) == 1 and len(exact) == 1, "ComfyUI listener is missing, ambiguous, or not loopback-only")
        inode = int(exact[0]["inode"])
        leaf_state = self.cgroup.managed_leaf_state(self.leaf_role)
        _require(
            leaf_state.get("ok") is True
            and leaf_state.get("populated") == 1,
            "ComfyUI managed leaf has no populated PID authority",
        )
        candidates = {int(value) for value in leaf_state.get("pids") or []}
        _require(pid in candidates, "ComfyUI process leader is absent from its managed leaf")
        owners = sorted(candidate for candidate in candidates if inode in self._socket_inodes(candidate))
        _require(owners, "ComfyUI listener is not owned by the launched backend tree")
        return {
            "family": "ipv4",
            "address": self.config.host,
            "port": self.config.port,
            "socket_inode": inode,
            "owner_pids": owners,
            "loopback_only": True,
            "ok": True,
        }

    def _process_contract(self, pid: int) -> dict[str, Any]:
        identity = capture_process_identity(self.proc_root, int(pid))
        _require(
            self.leaf_cgroup_path
            and _normalise_cgroup_path(
                identity.cgroup_path,
                label="ComfyUI backend cgroup",
            )
            == _normalise_cgroup_path(
                self.leaf_cgroup_path,
                label="ComfyUI managed leaf",
            ),
            "ComfyUI backend is outside its dedicated managed cgroup leaf",
        )
        try:
            executable = (self.proc_root / str(pid) / "exe").resolve(strict=True)
            cwd = (self.proc_root / str(pid) / "cwd").resolve(strict=True)
            command = [
                value.decode("utf-8", errors="surrogateescape")
                for value in (self.proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
                if value
            ]
            status_fields = {
                name: value.strip()
                for row in (self.proc_root / str(pid) / "status")
                .read_text(encoding="utf-8")
                .splitlines()
                if ":" in row
                for name, value in [row.split(":", 1)]
            }
            process_group = os.getpgid(pid)
        except Exception as exc:
            raise ComfyUIBackendError(f"cannot inspect ComfyUI backend process: {exc}") from exc
        _require(executable == self.config.python_executable, "ComfyUI backend executable differs from the configured Python")
        _require(cwd == self.config.working_root, "ComfyUI backend cwd differs from the configured working root")
        _require(
            command == list(self.command()),
            "ComfyUI backend cmdline differs from the fixed reviewed command",
        )
        _require(
            self.process_group > 0 and process_group == self.process_group,
            "ComfyUI backend left its campaign-owned process group",
        )
        _require(
            status_fields.get("NoNewPrivs") == "1",
            "ComfyUI backend lost no_new_privileges confinement",
        )
        _require(
            status_fields.get("Seccomp") == "2",
            "ComfyUI backend lost seccomp filter confinement",
        )
        capability_sets = {
            name: str(status_fields.get(name) or "").lower()
            for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        }
        _require(
            self._zero_capability_sets(capability_sets),
            "ComfyUI backend retained a Linux capability set",
        )
        namespace_pids = [
            int(value) for value in str(status_fields.get("NSpid") or "").split()
        ]
        _require(
            len(namespace_pids) >= 2
            and namespace_pids[0] == int(pid)
            and namespace_pids[-1] > 1,
            "ComfyUI backend is not a payload in a fresh PID namespace",
        )
        namespace_links = {
            name: os.readlink(self.proc_root / str(pid) / "ns" / name)
            for name in ("user", "mnt", "cgroup", "pid")
        }
        return {
            "pid": identity.pid,
            "start_ticks": identity.start_ticks,
            "boot_id": identity.boot_id,
            "cgroup_path": identity.cgroup_path,
            "cwd": str(cwd),
            "executable": str(executable),
            "process_group": process_group,
            "no_new_privileges": True,
            "seccomp_mode": 2,
            "capability_sets": capability_sets,
            "namespace_pids": namespace_pids,
            "namespace_links": namespace_links,
            "models_binding": _models_binding(
                self.config.working_root,
                self.config.models_root,
            ),
            "ok": True,
        }

    def _discover_backend_pid(self) -> tuple[int, dict[str, Any]]:
        candidates: list[tuple[int, dict[str, Any]]] = []
        for pid in sorted(self._process_group_pids(self.process_group)):
            try:
                evidence = self._process_contract(pid)
            except Exception:
                continue
            candidates.append((pid, evidence))
        _require(candidates, "ComfyUI sandbox has not execed its fixed backend payload")
        _require(
            len(candidates) == 1,
            "ComfyUI sandbox has multiple indistinguishable backend payloads",
        )
        return candidates[0]

    def _live_sandbox_authority(
        self,
        *,
        backend_pid: int,
        process_evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        proof = self.confinement_evidence
        launcher = proof.get("launcher")
        payload = proof.get("payload")
        proof_links = proof.get("namespace_links")
        _require(self.process is not None, "ComfyUI sandbox launcher is unavailable")
        _require(
            self.process.poll() is None
            and isinstance(launcher, Mapping)
            and launcher.get("host_pid") == self.process.pid
            and os.getpgid(self.process.pid) == self.process_group
            and os.getsid(self.process.pid) == self.process_group,
            "ComfyUI sandbox launcher authority changed",
        )
        _require(
            isinstance(proof_links, Mapping)
            and process_evidence.get("namespace_links") == dict(proof_links),
            "live ComfyUI namespace identities differ from the sandbox proof",
        )
        namespace_pids = process_evidence.get("namespace_pids")
        _require(
            isinstance(payload, Mapping)
            and isinstance(namespace_pids, list)
            and namespace_pids[-1] == payload.get("namespace_pid"),
            "live ComfyUI PID namespace identity differs from the sandbox proof",
        )
        leaf_state = self.cgroup.managed_leaf_state(self.leaf_role)
        leaf_pids = {int(value) for value in leaf_state.get("pids") or []}
        _require(
            self.process.pid in leaf_pids
            and backend_pid in leaf_pids
            and leaf_state.get("descendant_cgroups") == 0
            and leaf_state.get("subtree_control") == []
            and leaf_state.get("topology_intact", leaf_state.get("delegation_intact")) is True,
            "live ComfyUI launcher/backend cgroup topology differs from sandbox authority",
        )
        return {
            "launcher_pid": self.process.pid,
            "backend_host_pid": backend_pid,
            "process_group": self.process_group,
            "namespace_links": dict(proof_links),
            "namespace_pid": namespace_pids[-1],
            "leaf_pids": sorted(leaf_pids),
            "workload_delegation_capability": False,
            "ok": True,
        }

    def _readiness(self, *, timeout: float) -> dict[str, Any]:
        payload = self.readiness_probe(
            f"{self.config.api_url}/system_stats",
            timeout,
        )
        evidence = _validate_system_stats_payload(payload)
        return {
            "endpoint": f"{self.config.api_url}/system_stats",
            **evidence,
        }

    def start(self) -> dict[str, Any]:
        _require(self.process is None, "ComfyUI backend was already launched")
        self.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.evidence_root, 0o700)
        proof_read_fd = -1
        proof_write_fd = -1
        sandbox_nonce = uuid.uuid4().hex
        try:
            _require(
                bool(getattr(self.cgroup, "created", False))
                and not bool(getattr(self.cgroup, "stopped", False))
                and bool(str(getattr(self.cgroup, "scope_path", "") or "")),
                "ComfyUI backend requires an active verified campaign cgroup",
            )
            conflicting = [
                row
                for row in self._tcp_listeners()
                if row["port"] == self.config.port
            ]
            _require(
                not conflicting,
                "ComfyUI port already has a listener before launch",
            )
            models_binding = _models_binding(
                self.config.working_root,
                self.config.models_root,
            )
            self.leaf_evidence = dict(
                self.cgroup.create_managed_leaf(self.leaf_role)
            )
            self.leaf_cgroup_path = str(
                self.leaf_evidence.get("cgroup_path") or ""
            )
            _require(
                self.leaf_evidence.get("ok") is True
                and self.leaf_evidence.get("subtree_controllers_enabled") is False
                and self.leaf_evidence.get("descendant_cgroups") == 0
                and self.leaf_evidence.get("workload_delegation_capability")
                == "pending_sandbox"
                and bool(self.leaf_cgroup_path),
                "ComfyUI managed cgroup leaf host transition is incomplete",
            )
            environment = self.controlled_environment()
            command = self.command()
            proof_read_fd, proof_write_fd = os.pipe()
            wrapped = self.cgroup.wrap_command(
                command,
                role="comfyui",
                managed_leaf=self.leaf_role,
                sandbox_allow_write_roots=self._sandbox_write_roots(),
                sandbox_proof_fd=proof_write_fd,
                sandbox_nonce=sandbox_nonce,
            )
            descriptor = os.open(
                self.stdout_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            self.log_handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except Exception as exc:
            for descriptor in (proof_read_fd, proof_write_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            self._record(
                "preflight_failed",
                error=f"{exc.__class__.__name__}: {exc}",
                ok=False,
            )
            if isinstance(exc, ComfyUIBackendError):
                raise
            raise ComfyUIBackendError(
                f"ComfyUI backend launch preflight failed: {exc}"
            ) from exc
        started_at = _utc_now()
        started_monotonic_ns = time.monotonic_ns()
        try:
            self.process = self.popen(
                list(wrapped),
                cwd=str(self.config.working_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                close_fds=True,
                pass_fds=(proof_write_fd,),
            )
            os.close(proof_write_fd)
            proof_write_fd = -1
            self.process_group = int(self.process.pid)
            self.launcher_identity = capture_process_identity(
                self.proc_root,
                self.process.pid,
            )
            sandbox_read_fd = proof_read_fd
            proof_read_fd = -1
            self.confinement_evidence = self._read_sandbox_pipe(
                sandbox_read_fd,
                nonce=sandbox_nonce,
                timeout=min(10.0, self.config.readiness_timeout_seconds),
            )
            self.leaf_evidence = {
                **self.leaf_evidence,
                "host_leaf_state_before_sandbox": "pending_sandbox",
                "workload_delegation_capability": False,
                "sandbox_proof_schema_version": SANDBOX_PROOF_SCHEMA_VERSION,
            }
            deadline = time.monotonic() + self.config.readiness_timeout_seconds
            last_error = "backend process has not reached readiness"
            placement: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ComfyUIBackendError(
                        f"ComfyUI backend exited before readiness with {self.process.returncode}"
                    )
                try:
                    backend_pid, process_evidence = self._discover_backend_pid()
                    if self.backend_pid:
                        _require(
                            backend_pid == self.backend_pid,
                            "ComfyUI sandbox backend PID changed during startup",
                        )
                    sandbox_before = self._live_sandbox_authority(
                        backend_pid=backend_pid,
                        process_evidence=process_evidence,
                    )
                    if placement is None:
                        placement = self.cgroup.register_pid("comfyui", backend_pid)
                    leaf_state = self.cgroup.managed_leaf_state(self.leaf_role)
                    _require(
                        leaf_state.get("ok") is True
                        and leaf_state.get("populated") == 1
                        and self.process.pid in set(leaf_state.get("pids") or [])
                        and backend_pid in set(leaf_state.get("pids") or []),
                        "ComfyUI process is not populated in its managed cgroup leaf",
                    )
                    listener_before = self._listener_proof(backend_pid)
                    readiness = self._readiness(timeout=min(2.0, max(0.05, deadline - time.monotonic())))
                    process_after = self._process_contract(backend_pid)
                    sandbox_after = self._live_sandbox_authority(
                        backend_pid=backend_pid,
                        process_evidence=process_after,
                    )
                    leaf_after = self.cgroup.managed_leaf_state(self.leaf_role)
                    listener = self._listener_proof(backend_pid)
                    _require(
                        process_after == process_evidence,
                        "ComfyUI process authority changed across readiness",
                    )
                    _require(
                        leaf_after.get("ok") is True
                        and leaf_after.get("populated") == 1
                        and self.process.pid in set(leaf_after.get("pids") or [])
                        and backend_pid in set(leaf_after.get("pids") or []),
                        "ComfyUI managed leaf changed across readiness",
                    )
                    _require(
                        listener == listener_before,
                        "ComfyUI listener ownership changed across readiness",
                    )
                    _require(
                        sandbox_after == sandbox_before,
                        "ComfyUI sandbox authority changed across readiness",
                    )
                    self.backend_pid = backend_pid
                    self.process_identity = capture_process_identity(
                        self.proc_root,
                        backend_pid,
                    )
                    self.ready = True
                    self._ready_evidence = {
                        "schema_version": COMFYUI_BACKEND_READY_SCHEMA_VERSION,
                        "instance_id": self.instance_id,
                        "started_at": started_at,
                        "ready_at": _utc_now(),
                        "started_monotonic_ns": started_monotonic_ns,
                        "ready_monotonic_ns": time.monotonic_ns(),
                        "api_url": self.config.api_url,
                        "python_executable": str(self.config.python_executable),
                        "models_root": str(self.config.models_root),
                        "main_path": str(self.config.main_path),
                        "working_root": str(self.config.working_root),
                        "models_binding": models_binding,
                        "command": list(command),
                        "command_sha256": hashlib.sha256(b"\0".join(value.encode() for value in command)).hexdigest(),
                        "environment_keys": sorted(environment),
                        "process": process_after,
                        "placement": placement,
                        "managed_leaf": self.leaf_evidence,
                        "managed_leaf_state": {
                            **leaf_after,
                            "workload_delegation_capability": False,
                        },
                        "confinement": self.confinement_evidence,
                        "sandbox": self.confinement_evidence,
                        "sandbox_live": sandbox_after,
                        "launcher": {
                            "pid": self.process.pid,
                            "process_group": self.process_group,
                            "session": self.process_group,
                            "ok": True,
                        },
                        "listener": listener,
                        "listener_stable_across_readiness": True,
                        "readiness": readiness,
                        "actual_execution": True,
                        "simulated": False,
                        "adopted_external_pid": False,
                        "ok": True,
                    }
                    _write_private_once(self.ready_receipt_path, self._ready_evidence)
                    self._record(
                        "ready",
                        receipt=str(self.ready_receipt_path),
                        pid=backend_pid,
                        launcher_pid=self.process.pid,
                        ok=True,
                    )
                    return self.contract_evidence()
                except Exception as exc:
                    last_error = f"{exc.__class__.__name__}: {exc}"
                time.sleep(self.config.poll_interval_seconds)
            raise ComfyUIBackendError(
                "ComfyUI backend readiness timed out: " + last_error
            )
        except Exception as exc:
            for descriptor in (proof_read_fd, proof_write_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            self._record(
                "startup_failed",
                error=f"{exc.__class__.__name__}: {exc}",
                ok=False,
            )
            self.stop(reason="startup_failure")
            if isinstance(exc, ComfyUIBackendError):
                raise
            raise ComfyUIBackendError(f"ComfyUI backend launch failed: {exc}") from exc

    def contract_evidence(self) -> dict[str, Any]:
        _require(self.ready and self.process_identity is not None, "ComfyUI backend has no ready authority")
        receipt = _stable_receipt_authority(self.ready_receipt_path)
        return {
            "status": "ready",
            "api_url": self.config.api_url,
            "python_executable": str(self.config.python_executable),
            "main_path": str(self.config.main_path),
            "working_root": str(self.config.working_root),
            "models_root": str(self.config.models_root),
            "command": list(self.command()),
            "command_sha256": hashlib.sha256(
                b"\0".join(value.encode() for value in self.command())
            ).hexdigest(),
            "backend_pid": self.process_identity.pid,
            "backend_start_ticks": self.process_identity.start_ticks,
            "backend_boot_id": self.process_identity.boot_id,
            "backend_cgroup": self.process_identity.cgroup_path,
            "launcher_pid": int(self.process.pid) if self.process is not None else 0,
            "process_group": self.process_group,
            "managed_leaf": dict(self.leaf_evidence),
            "confinement": dict(self.confinement_evidence),
            "sandbox": dict(self.confinement_evidence),
            "ready_receipt": str(self.ready_receipt_path),
            "ready_receipt_sha256": receipt["sha256"],
            "ready_receipt_identity": receipt,
            "lifecycle_path": str(self.lifecycle_path),
            "stdout_path": str(self.stdout_path),
            "actual_execution": True,
            "simulated": False,
            "adopted_external_pid": False,
            "ok": True,
        }

    def check_live(self) -> dict[str, Any]:
        _require(self.ready and self.process is not None and self.process_identity is not None, "ComfyUI backend was not ready")
        _require(self.process.poll() is None, "ComfyUI backend exited during campaign")
        _require(self.backend_pid > 0, "ComfyUI backend host PID is unavailable")
        current = capture_process_identity(self.proc_root, self.backend_pid)
        for name in ("pid", "start_ticks", "boot_id", "cgroup_path"):
            _require(
                getattr(current, name) == getattr(self.process_identity, name),
                f"ComfyUI backend {name} identity changed",
            )
        process_before = self._process_contract(self.backend_pid)
        sandbox_before = self._live_sandbox_authority(
            backend_pid=self.backend_pid,
            process_evidence=process_before,
        )
        leaf_before = self.cgroup.managed_leaf_state(self.leaf_role)
        _require(
            leaf_before.get("ok") is True
            and leaf_before.get("populated") == 1
            and self.process.pid in set(leaf_before.get("pids") or [])
            and self.backend_pid in set(leaf_before.get("pids") or []),
            "ComfyUI managed cgroup leaf lost its process authority",
        )
        listener_before = self._listener_proof(self.backend_pid)
        readiness = self._readiness(timeout=2.0)
        process = self._process_contract(self.backend_pid)
        sandbox = self._live_sandbox_authority(
            backend_pid=self.backend_pid,
            process_evidence=process,
        )
        leaf_state = self.cgroup.managed_leaf_state(self.leaf_role)
        listener = self._listener_proof(self.backend_pid)
        _require(
            process == process_before,
            "ComfyUI process authority changed across liveness readiness",
        )
        _require(
            leaf_state.get("ok") is True
            and leaf_state.get("populated") == 1
            and self.process.pid in set(leaf_state.get("pids") or [])
            and self.backend_pid in set(leaf_state.get("pids") or []),
            "ComfyUI managed leaf changed across liveness readiness",
        )
        _require(
            listener == listener_before,
            "ComfyUI listener ownership changed across liveness readiness",
        )
        _require(
            sandbox == sandbox_before,
            "ComfyUI sandbox authority changed across liveness readiness",
        )
        return {
            "checked_at": _utc_now(),
            "process": process,
            "managed_leaf": leaf_state,
            "listener": listener,
            "readiness": readiness,
            "sandbox": sandbox,
            "ok": True,
        }

    def stop(self, *, reason: str) -> dict[str, Any]:
        process = self.process
        initial_pid = int(process.pid) if process is not None else 0
        was_ready = bool(self.ready)
        pre_stop_returncode = process.poll() if process is not None else None
        backend_alive_pre_stop = False
        if was_ready and self.backend_pid > 0 and self.process_identity is not None:
            try:
                current_backend = capture_process_identity(
                    self.proc_root,
                    self.backend_pid,
                )
                backend_alive_pre_stop = bool(
                    current_backend.start_ticks == self.process_identity.start_ticks
                    and current_backend.boot_id == self.process_identity.boot_id
                )
            except Exception:
                backend_alive_pre_stop = False
        unexpected_pre_stop_exit = bool(
            was_ready
            and (
                process is None
                or pre_stop_returncode is not None
                or not backend_alive_pre_stop
            )
        )
        if process is not None and pre_stop_returncode is None:
            try:
                os.killpg(self.process_group, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
        remaining_group = (
            self._process_group_pids(self.process_group)
            if self.process_group
            else set()
        )
        if remaining_group:
            try:
                os.killpg(self.process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5.0
        while (
            time.monotonic() < deadline
            and self.process_group
            and self._process_group_pids(self.process_group)
        ):
            time.sleep(0.05)
        remaining_group = (
            self._process_group_pids(self.process_group)
            if self.process_group
            else set()
        )
        leaf_before: dict[str, Any] = {
            "pids": [],
            "populated": 0,
            "consistent": True,
            "ok": not self.leaf_evidence,
        }
        leaf_cleanup: dict[str, Any] = {
            "ok": not self.leaf_evidence,
            "not_created": not self.leaf_evidence,
        }
        leaf_after = dict(leaf_before)
        escaped_leaf_pids: set[int] = set()
        leaf_error = ""
        if self.leaf_evidence:
            try:
                leaf_before = self.cgroup.managed_leaf_state(self.leaf_role)
                escaped_leaf_pids = {
                    int(value) for value in leaf_before.get("pids") or []
                }
                leaf_cleanup = self.cgroup.kill_managed_leaf(self.leaf_role)
                leaf_after = self.cgroup.managed_leaf_state(self.leaf_role)
            except Exception as exc:
                leaf_error = f"{exc.__class__.__name__}: {exc}"
                try:
                    leaf_after = self.cgroup.managed_leaf_state(self.leaf_role)
                except Exception:
                    leaf_after = {
                        "pids": [],
                        "populated": -1,
                        "consistent": False,
                        "ok": False,
                    }
                leaf_cleanup = {"ok": False, "error": leaf_error}
        if process is not None:
            process.poll()
        remaining_instance = self._instance_pids()
        listeners = [row for row in self._tcp_listeners() if row["port"] == self.config.port]
        root_alive = False
        if initial_pid:
            try:
                current = capture_process_identity(self.proc_root, initial_pid)
                root_alive = bool(
                    self.launcher_identity is None
                    or (
                        current.start_ticks == self.launcher_identity.start_ticks
                        and current.boot_id == self.launcher_identity.boot_id
                    )
                )
            except Exception:
                root_alive = False
        if self.log_handle is not None:
            try:
                self.log_handle.flush()
                self.log_handle.close()
            finally:
                self.log_handle = None
        self.ready = False
        ok = bool(
            not root_alive
            and not remaining_group
            and not listeners
            and leaf_cleanup.get("ok") is True
            and leaf_after.get("ok") is True
            and leaf_after.get("populated") == 0
            and not leaf_after.get("pids")
            and not escaped_leaf_pids
            and not remaining_instance
            and not unexpected_pre_stop_exit
        )
        return self._record(
            "stopped",
            reason=str(reason),
            pid=initial_pid,
            was_ready=was_ready,
            pre_stop_returncode=pre_stop_returncode,
            backend_pid=self.backend_pid,
            backend_alive_pre_stop=backend_alive_pre_stop,
            unexpected_pre_stop_exit=unexpected_pre_stop_exit,
            returncode=(process.returncode if process is not None else None),
            root_alive=root_alive,
            escaped_managed_leaf_pids=sorted(escaped_leaf_pids),
            managed_leaf_before=leaf_before,
            managed_leaf_cleanup=leaf_cleanup,
            managed_leaf_after=leaf_after,
            managed_leaf_error=leaf_error,
            remaining_process_group_pids=sorted(remaining_group),
            remaining_instance_pids=sorted(remaining_instance),
            remaining_listeners=listeners,
            process_group_empty=not remaining_group,
            port_released=not listeners,
            managed_leaf_empty=bool(
                leaf_after.get("ok") is True
                and leaf_after.get("populated") == 0
                and not leaf_after.get("pids")
            ),
            orphan_free=not escaped_leaf_pids and not leaf_after.get("pids"),
            ok=ok,
        )
