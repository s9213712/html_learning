#!/usr/bin/env python3
"""Fail-closed namespace sandbox stage for a campaign-owned ComfyUI backend.

This module is deliberately a separate exec stage.  Its caller must first move
the launcher into the dedicated campaign cgroup leaf, whose workload
delegation capability remains pending until this sandbox is complete, and create
the host-transition receipt described by :data:`HOST_TRANSITION_SCHEMA_VERSION`.
The stage never adopts a process, invokes a shell, searches ``PATH``, or accepts
an assertion that a sandbox already exists.

The outer launcher remains in the caller's process group and mirrors the exact
wait status of a PID-namespace reaper.  The reaper mounts a fresh procfs and a
fresh, read-only cgroup2 view rooted at the cgroup namespace root, hides host
runtime sockets, installs a write-only Landlock policy, removes every
capability, locks securebits, and installs an x86-64-only seccomp filter.  A
payload child writes one bounded machine proof to the caller-supplied pipe and
then performs one exact ``execve``.

The real namespace transition is intentionally not used by unit tests.  Tests
exercise the receipt contract, syscall policy, descriptor contract, and
negative validation without starting ComfyUI or touching a GPU.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any, Mapping, NoReturn, Sequence


SANDBOX_PROOF_SCHEMA_VERSION = "hackme.campaign-comfyui-sandbox.v1"
HOST_TRANSITION_SCHEMA_VERSION = "hackme.campaign-comfyui-host-transition.v1"
SANDBOX_EXIT_FAILURE = 125
MAX_TRANSITION_JSON_BYTES = 64 * 1024
MAX_PROOF_JSON_BYTES = 64 * 1024
MAX_REAPER_SYNC_BYTES = 16 * 1024
MAX_HOST_TRANSITION_AGE_NS = 10_000_000_000

_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_CGROUP_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.:@-]+$")
_BOOT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_AUDIT_ARCH_X86_64 = 0xC000003E
_X32_SYSCALL_BIT = 0x40000000
_AF_UNIX = 1

_CLONE_NEWNS = 0x00020000
_CLONE_NEWCGROUP = 0x02000000
_CLONE_NEWUTS = 0x04000000
_CLONE_NEWIPC = 0x08000000
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWPID = 0x20000000
_CLONE_NEWNET = 0x40000000
_CLONE_NEWTIME = 0x00000080
_CLONE_NAMESPACE_MASK = (
    _CLONE_NEWNS
    | _CLONE_NEWCGROUP
    | _CLONE_NEWUTS
    | _CLONE_NEWIPC
    | _CLONE_NEWUSER
    | _CLONE_NEWPID
    | _CLONE_NEWNET
    | _CLONE_NEWTIME
)

_MS_RDONLY = 1
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MS_REMOUNT = 32
_MS_BIND = 4096
_MS_REC = 16384
_MS_PRIVATE = 1 << 18
_MNT_DETACH = 2

_PR_GET_SECCOMP = 21
_PR_SET_SECCOMP = 22
_PR_CAPBSET_DROP = 24
_PR_SET_SECUREBITS = 28
_PR_GET_SECUREBITS = 27
_PR_SET_NO_NEW_PRIVS = 38
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_SECCOMP_MODE_FILTER = 2

_SECBIT_NOROOT = 1 << 0
_SECBIT_NOROOT_LOCKED = 1 << 1
_SECBIT_NO_SETUID_FIXUP = 1 << 2
_SECBIT_NO_SETUID_FIXUP_LOCKED = 1 << 3
_SECBIT_KEEP_CAPS_LOCKED = 1 << 5
_SECBIT_NO_CAP_AMBIENT_RAISE = 1 << 6
_SECBIT_NO_CAP_AMBIENT_RAISE_LOCKED = 1 << 7
_LOCKED_SECUREBITS = (
    _SECBIT_NOROOT
    | _SECBIT_NOROOT_LOCKED
    | _SECBIT_NO_SETUID_FIXUP
    | _SECBIT_NO_SETUID_FIXUP_LOCKED
    | _SECBIT_KEEP_CAPS_LOCKED
    | _SECBIT_NO_CAP_AMBIENT_RAISE
    | _SECBIT_NO_CAP_AMBIENT_RAISE_LOCKED
)

_LINUX_CAPABILITY_VERSION_3 = 0x20080522

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
_LANDLOCK_ACCESS_FS_REFER = 1 << 13
_LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14
_LANDLOCK_WRITE_RIGHTS = (
    _LANDLOCK_ACCESS_FS_WRITE_FILE
    | _LANDLOCK_ACCESS_FS_REMOVE_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_FILE
    | _LANDLOCK_ACCESS_FS_MAKE_CHAR
    | _LANDLOCK_ACCESS_FS_MAKE_DIR
    | _LANDLOCK_ACCESS_FS_MAKE_REG
    | _LANDLOCK_ACCESS_FS_MAKE_SOCK
    | _LANDLOCK_ACCESS_FS_MAKE_FIFO
    | _LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | _LANDLOCK_ACCESS_FS_MAKE_SYM
    | _LANDLOCK_ACCESS_FS_REFER
    | _LANDLOCK_ACCESS_FS_TRUNCATE
)

_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_JMP_JSET_K = 0x45
_BPF_RET_K = 0x06
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000

# Linux x86-64 syscall numbers.  The module rejects every other audit arch and
# rejects the x32 marker before these values are considered.
_SYSCALLS: Mapping[str, int] = {
    "socket": 41,
    "recvmsg": 47,
    "socketpair": 53,
    "clone": 56,
    "ptrace": 101,
    "pivot_root": 155,
    "chroot": 161,
    "mount": 165,
    "umount2": 166,
    "unshare": 272,
    "recvmmsg": 299,
    "setns": 308,
    "process_vm_readv": 310,
    "process_vm_writev": 311,
    "kcmp": 312,
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    "open_tree": 428,
    "move_mount": 429,
    "fsopen": 430,
    "fsconfig": 431,
    "fsmount": 432,
    "fspick": 433,
    "clone3": 435,
    "pidfd_getfd": 438,
    "mount_setattr": 442,
}
_UNCONDITIONAL_DENY_NAMES = (
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "recvmsg",
    "recvmmsg",
    "pidfd_getfd",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "kcmp",
    "setns",
    "unshare",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "open_tree",
    "move_mount",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "mount_setattr",
)

_PROTECTED_WRITE_PATHS = (
    Path("/proc"),
    Path("/sys"),
    Path("/run"),
    Path("/mnt/wslg/run"),
)


class ComfyUISandboxError(RuntimeError):
    """The namespace sandbox or its authority could not be proven."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            uid=int(value.st_uid),
            gid=int(value.st_gid),
        )

    def as_dict(self, *, path: Path | None = None) -> dict[str, Any]:
        result = {
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
        }
        if path is not None:
            result["path"] = str(path)
        return result


@dataclass(frozen=True)
class ComfyUISandboxConfig:
    """Exact, immutable inputs for one sandbox transition."""

    host_transition: Mapping[str, Any]
    nonce: str
    expected_cgroup_path: str
    allowed_write_roots: tuple[Path, ...]
    command: tuple[str, ...]
    cwd: Path
    proof_fd: int
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        transition = json.loads(
            json.dumps(dict(self.host_transition), ensure_ascii=True, sort_keys=True)
        )
        command = tuple(str(value) for value in self.command)
        roots = tuple(Path(value) for value in self.allowed_write_roots)
        environment = {str(key): str(value) for key, value in self.environment.items()}
        object.__setattr__(self, "host_transition", transition)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "allowed_write_roots", roots)
        object.__setattr__(self, "environment", environment)
        _validate_static_config(self)


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ushort),
        ("filter", ctypes.POINTER(_SockFilter)),
    ]


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ComfyUISandboxError(message)


def _normalise_cgroup_path(value: str, *, allow_root: bool = False) -> str:
    raw = str(value or "").strip()
    _require(raw.startswith("/"), "expected cgroup path must be absolute")
    _require("\x00" not in raw, "expected cgroup path contains NUL")
    pieces = raw.split("/")
    _require(".." not in pieces and "." not in pieces, "expected cgroup path is not canonical")
    components = [piece for piece in pieces if piece]
    _require(
        all(_CGROUP_COMPONENT_RE.fullmatch(piece) for piece in components),
        "expected cgroup path contains an invalid component",
    )
    result = "/" + "/".join(components)
    _require(allow_root or result != "/", "ComfyUI sandbox requires a dedicated cgroup leaf")
    _require(result == raw, "expected cgroup path is not canonical")
    return result


def _canonical_directory(path: Path, *, label: str) -> tuple[Path, FileIdentity]:
    candidate = Path(path)
    _require(candidate.is_absolute(), f"{label} must be absolute")
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except Exception as exc:
        raise ComfyUISandboxError(f"cannot inspect {label}: {exc}") from exc
    _require(candidate == resolved, f"{label} must be an exact canonical path")
    _require(not stat.S_ISLNK(metadata.st_mode), f"{label} cannot be a symlink")
    _require(stat.S_ISDIR(metadata.st_mode), f"{label} must be a directory")
    return candidate, FileIdentity.from_stat(metadata)


def _canonical_executable(path: str) -> tuple[Path, FileIdentity]:
    candidate = Path(path)
    _require(candidate.is_absolute(), "sandbox command executable must be absolute")
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except Exception as exc:
        raise ComfyUISandboxError(f"cannot inspect command executable: {exc}") from exc
    _require(candidate == resolved, "sandbox command executable must be canonical")
    _require(not stat.S_ISLNK(metadata.st_mode), "sandbox command executable cannot be a symlink")
    _require(stat.S_ISREG(metadata.st_mode), "sandbox command executable must be a regular file")
    _require(metadata.st_mode & 0o111, "sandbox command executable is not executable")
    return candidate, FileIdentity.from_stat(metadata)


def _validate_static_config(config: ComfyUISandboxConfig) -> None:
    _require(_NONCE_RE.fullmatch(config.nonce), "sandbox nonce must be 128-bit lowercase hex")
    _normalise_cgroup_path(config.expected_cgroup_path)
    _require(
        isinstance(config.proof_fd, int)
        and not isinstance(config.proof_fd, bool)
        and config.proof_fd >= 3,
        "sandbox proof fd must be an inherited descriptor >= 3",
    )
    _require(bool(config.command), "sandbox fixed command cannot be empty")
    _require(
        all(value and "\x00" not in value for value in config.command),
        "sandbox fixed command contains an empty argument or NUL",
    )
    _canonical_executable(config.command[0])
    _canonical_directory(config.cwd, label="sandbox cwd")
    _require(config.allowed_write_roots, "sandbox requires at least one write root")
    seen: set[Path] = set()
    for value in config.allowed_write_roots:
        path, _identity = _canonical_directory(value, label="sandbox write root")
        _require(path not in seen, "sandbox write roots contain a duplicate")
        seen.add(path)
        for protected in _PROTECTED_WRITE_PATHS:
            _require(
                path != protected
                and path not in protected.parents
                and protected not in path.parents,
                f"sandbox write root exposes protected namespace path: {path}",
            )
    _require(
        all(
            key
            and "=" not in key
            and "\x00" not in key
            and "\x00" not in value
            for key, value in config.environment.items()
        ),
        "sandbox environment contains an invalid key or NUL",
    )


def _unique_json_object(raw: str) -> dict[str, Any]:
    encoded = raw.encode("utf-8")
    _require(len(encoded) <= MAX_TRANSITION_JSON_BYTES, "host transition JSON is too large")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ComfyUISandboxError(f"host transition JSON duplicates key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=unique)
    except ComfyUISandboxError:
        raise
    except Exception as exc:
        raise ComfyUISandboxError(f"host transition JSON is invalid: {exc}") from exc
    _require(isinstance(payload, dict), "host transition JSON must be an object")
    return payload


def _root_records(roots: Sequence[Path]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in roots:
        path, identity = _canonical_directory(value, label="sandbox write root")
        result.append(identity.as_dict(path=path))
    return result


def _native_path_record(path: Path, *, directory: bool, label: str) -> dict[str, Any]:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except Exception as exc:
        raise ComfyUISandboxError(f"cannot inspect {label}: {exc}") from exc
    _require(candidate.is_absolute() and candidate == resolved, f"{label} is not canonical")
    _require(not stat.S_ISLNK(metadata.st_mode), f"{label} cannot be a symlink")
    if directory:
        _require(stat.S_ISDIR(metadata.st_mode), f"{label} is not a directory")
    else:
        _require(stat.S_ISREG(metadata.st_mode), f"{label} is not a regular control file")
    return FileIdentity.from_stat(metadata).as_dict(path=candidate)


def _validate_cgroup_leaf_identity_record(
    value: Any,
    *,
    expected_leaf_path: Path,
) -> dict[str, Any]:
    _require(isinstance(value, Mapping), "host transition cgroup leaf identity is missing")
    result = dict(value)
    expected_paths = {
        "root": expected_leaf_path,
        "cgroup_procs": expected_leaf_path / "cgroup.procs",
        "cgroup_events": expected_leaf_path / "cgroup.events",
    }
    for name, expected_path in expected_paths.items():
        record = result.get(name)
        _require(isinstance(record, Mapping), f"host transition {name} identity is missing")
        _require(record.get("path") == str(expected_path), f"host transition {name} path differs")
        for field in ("device", "inode", "mode", "uid", "gid"):
            item = record.get(field)
            _require(isinstance(item, int) and not isinstance(item, bool), f"host transition {name} {field} is invalid")
        if name == "root":
            _require(stat.S_ISDIR(int(record["mode"])), "host transition cgroup root is not a directory")
        else:
            _require(stat.S_ISREG(int(record["mode"])), f"host transition {name} is not a regular control")
        _require(int(record["inode"]) > 0, f"host transition {name} inode is invalid")
    _require(result.get("cgroup_type") in {"domain", "domain threaded", "threaded"}, "host transition cgroup type is invalid")
    _require(result.get("subtree_control") == [], "ComfyUI cgroup leaf still has enabled subtree controllers")
    _require(
        result.get("subtree_controllers_enabled") is False,
        "host transition does not factually prove empty subtree controllers",
    )
    _require(
        result.get("descendant_cgroups") == 0,
        "host transition cgroup leaf has descendant cgroups",
    )
    _require(
        result.get("workload_delegation_capability") == "pending_sandbox",
        "host transition must leave workload delegation capability pending",
    )
    _require("delegated" not in result, "host transition cannot self-assert delegated state")
    _require(result.get("current_pid_present") is True, "host transition leaf does not contain the launcher")
    _require(result.get("ok") is True, "host transition leaf identity did not pass")
    return result


def _count_descendant_cgroups(root: Path) -> int:
    pending = [Path(root)]
    count = 0
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except Exception as exc:
            raise ComfyUISandboxError(
                f"cannot enumerate cgroup descendants below {current}: {exc}"
            ) from exc
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            metadata = path.lstat()
            _require(
                stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode),
                f"cgroup descendant is not a native directory: {path}",
            )
            count += 1
            _require(count <= 1024, "cgroup descendant enumeration exceeded its safety bound")
            pending.append(path)
    return count


def _live_cgroup_leaf_identity(
    leaf_path: Path,
    *,
    expected_pid: int,
) -> dict[str, Any]:
    root = _native_path_record(leaf_path, directory=True, label="live ComfyUI cgroup leaf")
    procs_path = leaf_path / "cgroup.procs"
    events_path = leaf_path / "cgroup.events"
    procs = _native_path_record(procs_path, directory=False, label="live ComfyUI cgroup.procs")
    events = _native_path_record(events_path, directory=False, label="live ComfyUI cgroup.events")
    pids = {
        int(row.strip())
        for row in procs_path.read_text(encoding="ascii").splitlines()
        if row.strip()
    }
    _require(expected_pid in pids, "live ComfyUI cgroup leaf does not contain the launcher")
    cgroup_type = (leaf_path / "cgroup.type").read_text(encoding="ascii").strip()
    subtree_control = (leaf_path / "cgroup.subtree_control").read_text(encoding="ascii").split()
    descendant_cgroups = _count_descendant_cgroups(leaf_path)
    result = {
        "root": root,
        "cgroup_procs": procs,
        "cgroup_events": events,
        "cgroup_type": cgroup_type,
        "subtree_control": subtree_control,
        "subtree_controllers_enabled": bool(subtree_control),
        "descendant_cgroups": descendant_cgroups,
        "workload_delegation_capability": "pending_sandbox",
        "current_pid_present": True,
        "ok": True,
    }
    return _validate_cgroup_leaf_identity_record(result, expected_leaf_path=leaf_path)


def _same_kernel_object(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        left.get("device") == right.get("device")
        and left.get("inode") == right.get("inode")
        and stat.S_IFMT(int(left.get("mode", 0))) == stat.S_IFMT(int(right.get("mode", 0)))
    )


def _require_same_cgroup_leaf_objects(
    pinned: Mapping[str, Any],
    bound: Mapping[str, Any],
) -> None:
    for name in ("root", "cgroup_procs", "cgroup_events"):
        left = pinned.get(name)
        right = bound.get(name)
        _require(
            isinstance(left, Mapping)
            and isinstance(right, Mapping)
            and _same_kernel_object(left, right),
            f"fresh cgroup2 {name} is not the pinned host leaf object",
        )
    _require(bound.get("cgroup_type") == pinned.get("cgroup_type"), "fresh cgroup2 type differs from the pinned leaf")
    _require(bound.get("subtree_control") == [], "fresh cgroup2 root unexpectedly delegates controllers")
    _require(bound.get("subtree_controllers_enabled") is False, "fresh cgroup2 root has subtree controllers enabled")
    _require(bound.get("descendant_cgroups") == 0, "fresh cgroup2 root has descendant cgroups")


def validate_host_transition_payload(
    payload: Mapping[str, Any],
    *,
    nonce: str,
    expected_cgroup_path: str,
    allowed_write_roots: Sequence[Path],
    current_pid: int,
    current_cgroup_path: str,
    current_monotonic_ns: int,
    current_start_ticks: int | None = None,
    current_boot_id: str | None = None,
    current_leaf_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate caller evidence independently of procfs/mount live checks."""

    expected = _normalise_cgroup_path(expected_cgroup_path)
    actual = _normalise_cgroup_path(current_cgroup_path)
    _require(expected == actual, "live process is not in the expected ComfyUI leaf")
    _require(_NONCE_RE.fullmatch(nonce), "host transition nonce is invalid")
    _require(payload.get("schema_version") == HOST_TRANSITION_SCHEMA_VERSION, "host transition schema is invalid")
    _require(payload.get("nonce") == nonce, "host transition nonce differs from the challenge")
    _require(payload.get("pid") == current_pid, "host transition PID differs from the launcher")
    _require(payload.get("role") == "comfyui", "host transition role is not comfyui")
    _require(payload.get("cgroup_path") == expected, "host transition cgroup differs from the launcher")
    expected_leaf_path = Path(f"/sys/fs/cgroup{expected}")
    leaf_identity = _validate_cgroup_leaf_identity_record(
        payload.get("leaf_identity"),
        expected_leaf_path=expected_leaf_path,
    )
    if current_leaf_identity is not None:
        _require(
            leaf_identity == dict(current_leaf_identity),
            "host transition cgroup leaf identity changed before sandbox entry",
        )
    process = payload.get("process")
    _require(isinstance(process, Mapping), "host transition process identity is missing")
    transition_start_ticks = process.get("start_ticks")
    transition_boot_id = str(process.get("boot_id") or "")
    _require(
        process.get("pid") == current_pid
        and isinstance(transition_start_ticks, int)
        and not isinstance(transition_start_ticks, bool)
        and transition_start_ticks > 0
        and _BOOT_ID_RE.fullmatch(transition_boot_id)
        and process.get("cgroup_path") == expected,
        "host transition process identity is incomplete",
    )
    if current_start_ticks is not None:
        _require(transition_start_ticks == current_start_ticks, "host transition process start time changed")
    if current_boot_id is not None:
        _require(transition_boot_id == current_boot_id, "host transition kernel boot identity changed")
    placement = payload.get("placement")
    _require(isinstance(placement, Mapping), "host transition placement is missing")
    _require(
        placement.get("pid") == current_pid
        and placement.get("campaign_cgroup") == expected
        and placement.get("exact_leaf") is True
        and placement.get("ok") is True,
        "host transition placement is incomplete",
    )
    write = payload.get("cgroup_write")
    _require(isinstance(write, Mapping), "host transition cgroup write evidence is missing")
    target = str(write.get("target") or "")
    _require(
        write.get("attempted") is True
        and write.get("completed") is True
        and write.get("verified_after_write") is True
        and write.get("written_pid") == current_pid
        and target == f"/sys/fs/cgroup{expected}/cgroup.procs",
        "host transition does not prove the pre-namespace cgroup write",
    )
    expected_roots = _root_records(tuple(Path(value) for value in allowed_write_roots))
    _require(payload.get("allowed_write_roots") == expected_roots, "host transition write-root identities differ")
    created_monotonic_ns = payload.get("created_monotonic_ns")
    _require(
        isinstance(created_monotonic_ns, int)
        and not isinstance(created_monotonic_ns, bool)
        and created_monotonic_ns > 0
        and isinstance(current_monotonic_ns, int)
        and not isinstance(current_monotonic_ns, bool)
        and current_monotonic_ns >= created_monotonic_ns,
        "host transition monotonic timestamp is invalid",
    )
    transition_age_ns = current_monotonic_ns - created_monotonic_ns
    _require(
        transition_age_ns <= MAX_HOST_TRANSITION_AGE_NS,
        "host transition receipt is stale",
    )
    _require(payload.get("actual_execution") is True, "host transition is not an actual execution")
    _require(payload.get("simulated") is False, "host transition is simulated")
    _require(payload.get("ok") is True, "host transition did not pass")
    digest = hashlib.sha256(
        json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": HOST_TRANSITION_SCHEMA_VERSION,
        "nonce": nonce,
        "pid": current_pid,
        "start_ticks": transition_start_ticks,
        "boot_id": transition_boot_id,
        "cgroup_path": expected,
        "leaf_identity": leaf_identity,
        "allowed_write_roots": expected_roots,
        "transition_age_ns": transition_age_ns,
        "receipt_sha256": digest,
        "ok": True,
    }


def _read_unified_cgroup(proc_root: Path = Path("/proc")) -> str:
    rows = (proc_root / "self" / "cgroup").read_text(encoding="utf-8").splitlines()
    matches = []
    for row in rows:
        parts = row.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            matches.append(_normalise_cgroup_path(parts[2], allow_root=True))
    _require(len(matches) == 1, "unified cgroup v2 membership is unavailable or ambiguous")
    return matches[0]


def _mountinfo_unescape(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _read_mount_record(target: Path, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    wanted = str(target)
    matches: list[dict[str, Any]] = []
    for row in (proc_root / "self" / "mountinfo").read_text(encoding="utf-8").splitlines():
        fields = row.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator < 6 or len(fields) < separator + 4:
            continue
        mount_point = _mountinfo_unescape(fields[4])
        if mount_point != wanted:
            continue
        matches.append({
            "mount_id": int(fields[0]),
            "parent_mount_id": int(fields[1]),
            "root": _mountinfo_unescape(fields[3]),
            "mount_point": mount_point,
            "mount_options": sorted(set(fields[5].split(","))),
            "optional_fields": fields[6:separator],
            "filesystem_type": fields[separator + 1],
            "mount_source": _mountinfo_unescape(fields[separator + 2]),
            "super_options": sorted(set(fields[separator + 3].split(","))),
        })
    _require(bool(matches), f"mountinfo has no record for {target}")
    return matches[-1]


def _namespace_links(proc_root: Path = Path("/proc"), pid: str = "self") -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("user", "mnt", "cgroup", "pid"):
        result[name] = os.readlink(proc_root / pid / "ns" / name)
    return result


def _status_fields(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = row.partition(":")
        if separator:
            result[key] = value.strip()
    return result


def _live_start_ticks(proc_root: Path = Path("/proc")) -> int:
    tail = (proc_root / "self" / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
    _require(len(tail) > 19, "live process stat is malformed")
    value = int(tail[19])
    _require(value > 0, "live process start time is invalid")
    return value


def _live_boot_id(proc_root: Path = Path("/proc")) -> str:
    value = (proc_root / "sys" / "kernel" / "random" / "boot_id").read_text(encoding="ascii").strip().lower()
    _require(_BOOT_ID_RE.fullmatch(value), "kernel boot identity is invalid")
    return value


def _validate_host_kernel(config: ComfyUISandboxConfig) -> dict[str, Any]:
    uname = os.uname()
    _require(uname.sysname == "Linux", "ComfyUI namespaces require Linux")
    _require(uname.machine.lower() in {"x86_64", "amd64"}, "seccomp policy is reviewed only for x86-64")
    for namespace in ("user", "mnt", "cgroup", "pid", "pid_for_children"):
        _require(Path(f"/proc/self/ns/{namespace}").exists(), f"kernel lacks {namespace} namespace support")
    mount = _read_mount_record(Path("/sys/fs/cgroup"))
    _require(mount["filesystem_type"] == "cgroup2", "host cgroup mount is not cgroup v2")
    _require("nsdelegate" in mount["super_options"], "host cgroup2 mount lacks nsdelegate")
    _require(Path("/sys/fs/cgroup/cgroup.controllers").is_file(), "unified cgroup controllers are unavailable")
    current_cgroup = _read_unified_cgroup()
    live_leaf = _live_cgroup_leaf_identity(
        Path(f"/sys/fs/cgroup{current_cgroup}"),
        expected_pid=os.getpid(),
    )
    transition = validate_host_transition_payload(
        config.host_transition,
        nonce=config.nonce,
        expected_cgroup_path=config.expected_cgroup_path,
        allowed_write_roots=config.allowed_write_roots,
        current_pid=os.getpid(),
        current_cgroup_path=current_cgroup,
        current_monotonic_ns=time.monotonic_ns(),
        current_start_ticks=_live_start_ticks(),
        current_boot_id=_live_boot_id(),
        current_leaf_identity=live_leaf,
    )
    real_uid, effective_uid, saved_uid = os.getresuid()
    real_gid, effective_gid, saved_gid = os.getresgid()
    _require(real_uid > 0 and (real_uid, effective_uid, saved_uid) == (real_uid,) * 3, "launcher must be one unprivileged host UID")
    _require(real_gid > 0 and (real_gid, effective_gid, saved_gid) == (real_gid,) * 3, "launcher must be one unprivileged host GID")
    status = _status_fields(Path("/proc/self/status"))
    return {
        "kernel_release": uname.release,
        "machine": uname.machine.lower(),
        "host_uid": real_uid,
        "host_gid": real_gid,
        "host_supplementary_groups": sorted(os.getgroups()),
        "cgroup_mount": mount,
        "cgroup_path": current_cgroup,
        "leaf_identity": live_leaf,
        "namespace_links": _namespace_links(),
        "seccomp_mode_before": int(status.get("Seccomp", "0")),
        "seccomp_filters_before": int(status.get("Seccomp_filters", "0")),
        "host_transition": transition,
        "ok": True,
    }


def _descriptor_record(descriptor: int) -> dict[str, Any]:
    metadata = os.fstat(descriptor)
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    try:
        target = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError:
        target = ""
    return {
        "fd": descriptor,
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "mode": int(metadata.st_mode),
        "access_mode": int(flags & os.O_ACCMODE),
        "target": target,
        "is_socket": stat.S_ISSOCK(metadata.st_mode),
        "is_fifo": stat.S_ISFIFO(metadata.st_mode),
    }


def _validate_descriptor_contract(proof_fd: int) -> dict[str, Any]:
    stdio = [_descriptor_record(value) for value in (0, 1, 2)]
    _require(all(row["is_socket"] is False for row in stdio), "sandbox stdio cannot be a socket")
    proof = _descriptor_record(proof_fd)
    _require(proof["is_fifo"] is True and proof["is_socket"] is False, "sandbox proof fd must be a pipe")
    _require(proof["access_mode"] == os.O_WRONLY, "sandbox proof pipe must be write-only")
    _require(proof["target"].startswith("pipe:["), "sandbox proof fd is not an anonymous pipe")
    _require(
        all((row["device"], row["inode"]) != (proof["device"], proof["inode"]) for row in stdio),
        "sandbox proof pipe must be distinct from stdio",
    )
    descriptor_flags = fcntl.fcntl(proof_fd, fcntl.F_GETFD)
    fcntl.fcntl(proof_fd, fcntl.F_SETFD, descriptor_flags | fcntl.FD_CLOEXEC)
    return {"stdio": stdio, "proof_pipe": proof, "ok": True}


def _open_fd_numbers() -> list[int]:
    try:
        values = [int(value) for value in os.listdir("/proc/self/fd") if value.isdigit()]
    except Exception as exc:
        raise ComfyUISandboxError(f"cannot audit open descriptors: {exc}") from exc
    return sorted(set(values))


def _close_and_audit_fds(allowed: set[int]) -> dict[str, Any]:
    _require({0, 1, 2} <= allowed, "stdio must remain in the descriptor allowlist")
    before = _open_fd_numbers()
    for descriptor in before:
        if descriptor not in allowed:
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
    after = _open_fd_numbers()
    _require(after == sorted(allowed), f"unexpected descriptors remain open: {after}")
    return {"before": before, "closed": sorted(set(before) - allowed), "after": after, "ok": True}


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        _require(written > 0, "pipe write made no progress")
        view = view[written:]


def _write_json_line(descriptor: int, payload: Mapping[str, Any], *, limit: int = MAX_PROOF_JSON_BYTES) -> None:
    encoded = (json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _require(len(encoded) <= limit, "sandbox proof exceeds its size bound")
    _write_all(descriptor, encoded)


def _read_bounded_json_line(descriptor: int, *, limit: int) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(4096, limit - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        _require(total <= limit, "sandbox reaper synchronization exceeded its bound")
    try:
        payload = json.loads(b"".join(chunks))
    except Exception as exc:
        raise ComfyUISandboxError(f"sandbox reaper synchronization is invalid: {exc}") from exc
    _require(isinstance(payload, dict), "sandbox reaper synchronization must be an object")
    return payload


def _libc() -> ctypes.CDLL:
    library = ctypes.CDLL(None, use_errno=True)
    library.mount.restype = ctypes.c_int
    library.umount2.restype = ctypes.c_int
    library.unshare.restype = ctypes.c_int
    library.prctl.restype = ctypes.c_int
    return library


def _checked_zero(result: int, *, action: str) -> None:
    if int(result) != 0:
        number = ctypes.get_errno()
        raise ComfyUISandboxError(f"{action} failed: [{number}] {os.strerror(number)}")


def _mount(source: str | None, target: Path, filesystem: str | None, flags: int, data: str | None = None) -> None:
    library = _libc()
    _checked_zero(
        library.mount(
            source.encode() if source is not None else None,
            str(target).encode(),
            filesystem.encode() if filesystem is not None else None,
            ctypes.c_ulong(flags),
            data.encode() if data is not None else None,
        ),
        action=f"mount {filesystem or 'bind'} on {target}",
    )


def _umount(target: Path, flags: int = 0) -> None:
    library = _libc()
    _checked_zero(library.umount2(str(target).encode(), flags), action=f"unmount {target}")


def _write_proc_control(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        _write_all(descriptor, value.encode("ascii"))
    finally:
        os.close(descriptor)


def _read_map(path: Path) -> list[list[int]]:
    result: list[list[int]] = []
    for row in path.read_text(encoding="ascii").splitlines():
        fields = row.split()
        _require(len(fields) == 3, f"namespace map is malformed: {path}")
        result.append([int(value) for value in fields])
    return result


def _enter_namespaces(host: Mapping[str, Any]) -> dict[str, Any]:
    library = _libc()
    before = _namespace_links()
    _checked_zero(library.unshare(_CLONE_NEWUSER), action="unshare user namespace")
    host_uid = int(host["host_uid"])
    host_gid = int(host["host_gid"])
    _write_proc_control(Path("/proc/self/setgroups"), "deny")
    _write_proc_control(Path("/proc/self/uid_map"), f"0 {host_uid} 1")
    _write_proc_control(Path("/proc/self/gid_map"), f"0 {host_gid} 1")
    os.setresgid(0, 0, 0)
    os.setresuid(0, 0, 0)
    _checked_zero(
        library.unshare(_CLONE_NEWNS | _CLONE_NEWCGROUP | _CLONE_NEWPID),
        action="unshare mount/cgroup/PID namespaces",
    )
    after = _namespace_links()
    pid_for_children = os.readlink("/proc/self/ns/pid_for_children")
    _require(before["user"] != after["user"], "user namespace did not change")
    _require(before["mnt"] != after["mnt"], "mount namespace did not change")
    _require(before["cgroup"] != after["cgroup"], "cgroup namespace did not change")
    _require(before["pid"] != pid_for_children, "PID namespace for children did not change")
    uid_map = _read_map(Path("/proc/self/uid_map"))
    gid_map = _read_map(Path("/proc/self/gid_map"))
    setgroups = Path("/proc/self/setgroups").read_text(encoding="ascii").strip()
    _require(uid_map == [[0, host_uid, 1]], "UID namespace map is not exact")
    _require(gid_map == [[0, host_gid, 1]], "GID namespace map is not exact")
    _require(setgroups == "deny", "setgroups was not irreversibly denied")
    return {
        "before": before,
        "after_unshare": after,
        "pid_for_children": pid_for_children,
        "uid_map": uid_map,
        "gid_map": gid_map,
        "setgroups": setgroups,
        "inside_uid": list(os.getresuid()),
        "inside_gid": list(os.getresgid()),
        "ok": True,
    }


def _make_scratch(root: Path, nonce: str) -> Path:
    path = root / f".hackme-comfyui-sandbox-{nonce}-{os.getpid()}"
    path.mkdir(mode=0o700)
    metadata = path.lstat()
    _require(stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode), "sandbox mount scratch is invalid")
    return path


def _setup_mounts(config: ComfyUISandboxConfig, host: Mapping[str, Any]) -> dict[str, Any]:
    _mount(None, Path("/"), None, _MS_REC | _MS_PRIVATE)
    host_proc_mount_id = _read_mount_record(Path("/proc"))["mount_id"]
    _mount("proc", Path("/proc"), "proc", _MS_RDONLY | _MS_NOSUID | _MS_NODEV | _MS_NOEXEC)
    proc_record = _read_mount_record(Path("/proc"))
    _require(proc_record["filesystem_type"] == "proc" and proc_record["root"] == "/", "fresh procfs is not rooted at / in the PID namespace")
    _require(proc_record["mount_id"] != host_proc_mount_id, "fresh procfs mount was not created")
    _require(os.getpid() == 1, "namespace reaper is not PID 1")

    scratch = _make_scratch(config.allowed_write_roots[0], config.nonce)
    cgroup_source = scratch / "cgroup2"
    cgroup_source.mkdir(mode=0o700)
    source_mounted = False
    cleanup_error: BaseException | None = None
    source_identity: dict[str, Any] | None = None
    try:
        _mount("cgroup2", cgroup_source, "cgroup2", _MS_NOSUID | _MS_NODEV | _MS_NOEXEC, "nsdelegate")
        source_mounted = True
        source_record = _read_mount_record(cgroup_source)
        _require(source_record["filesystem_type"] == "cgroup2" and source_record["root"] == "/", "fresh cgroup2 mount is not namespace-rooted")
        source_identity = _live_cgroup_leaf_identity(
            cgroup_source,
            expected_pid=1,
        )
        _require_same_cgroup_leaf_objects(host["leaf_identity"], source_identity)
        _mount(str(cgroup_source), Path("/sys/fs/cgroup"), None, _MS_BIND | _MS_REC)
        _mount(
            None,
            Path("/sys/fs/cgroup"),
            None,
            _MS_BIND | _MS_REMOUNT | _MS_RDONLY | _MS_NOSUID | _MS_NODEV | _MS_NOEXEC,
        )
        _umount(cgroup_source, _MNT_DETACH)
        source_mounted = False
    finally:
        if source_mounted:
            try:
                _umount(cgroup_source, _MNT_DETACH)
            except BaseException as exc:
                cleanup_error = exc
        try:
            cgroup_source.rmdir()
            scratch.rmdir()
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise ComfyUISandboxError(f"sandbox mount scratch cleanup failed: {cleanup_error}") from cleanup_error

    cgroup_record = _read_mount_record(Path("/sys/fs/cgroup"))
    _require(cgroup_record["filesystem_type"] == "cgroup2", "sandbox cgroup view is not cgroup2")
    _require(cgroup_record["root"] == "/", "sandbox cgroup2 view is not rooted at namespace /")
    _require("ro" in cgroup_record["mount_options"], "sandbox cgroup2 view is not read-only")
    _require("nsdelegate" in cgroup_record["super_options"], "sandbox cgroup2 view lacks nsdelegate")
    _require(cgroup_record["mount_id"] != host["cgroup_mount"]["mount_id"], "sandbox cgroup2 view reused the host mount")
    _require(_read_unified_cgroup() == "/", "cgroup namespace does not expose the leaf as root")
    bound_identity = _live_cgroup_leaf_identity(
        Path("/sys/fs/cgroup"),
        expected_pid=1,
    )
    _require_same_cgroup_leaf_objects(host["leaf_identity"], bound_identity)
    _require(
        source_identity is not None,
        "fresh cgroup2 source identity was not captured",
    )

    hidden: list[dict[str, Any]] = []
    for target in (Path("/mnt/wslg/run"), Path("/run")):
        path, _identity = _canonical_directory(target, label=f"hidden runtime {target}")
        _mount("tmpfs", path, "tmpfs", _MS_RDONLY | _MS_NOSUID | _MS_NODEV | _MS_NOEXEC, "mode=000,size=4096")
        record = _read_mount_record(path)
        _require(record["filesystem_type"] == "tmpfs" and "ro" in record["mount_options"], f"runtime path was not hidden: {path}")
        hidden.append({"path": str(path), "mount": record, "mode": stat.S_IMODE(path.stat().st_mode), "hidden": True})
    return {
        "propagation_private": True,
        "proc": proc_record,
        "cgroup2": cgroup_record,
        "cgroup_namespace_path": "/",
        "pinned_host_leaf_identity": host["leaf_identity"],
        "fresh_source_leaf_identity": source_identity,
        "bound_read_only_leaf_identity": bound_identity,
        "leaf_kernel_objects_match": True,
        "hidden_runtime_paths": hidden,
        "ok": True,
    }


def _landlock_syscall(number: int, *arguments: Any) -> int:
    library = _libc()
    library.syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = int(library.syscall(int(number), *arguments))
    if result < 0:
        error_number = ctypes.get_errno()
        raise ComfyUISandboxError(f"Landlock syscall {number} failed: [{error_number}] {os.strerror(error_number)}")
    return result


def _install_landlock(roots: Sequence[Path]) -> dict[str, Any]:
    abi = _landlock_syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        ctypes.c_size_t(0),
        ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    _require(abi >= 3, f"Landlock ABI {abi} cannot enforce REFER and TRUNCATE")
    attr = _LandlockRulesetAttr(handled_access_fs=_LANDLOCK_WRITE_RIGHTS)
    ruleset = _landlock_syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
        ctypes.c_uint(0),
    )
    records: list[dict[str, Any]] = []
    try:
        for value in roots:
            path, identity = _canonical_directory(value, label="Landlock write root")
            descriptor = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                rule = _LandlockPathBeneathAttr(allowed_access=_LANDLOCK_WRITE_RIGHTS, parent_fd=descriptor)
                _landlock_syscall(
                    _LANDLOCK_ADD_RULE,
                    ruleset,
                    _LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.byref(rule),
                    ctypes.c_uint(0),
                )
            finally:
                os.close(descriptor)
            records.append(identity.as_dict(path=path))
        library = _libc()
        _checked_zero(library.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), action="set no_new_privs before Landlock")
        _landlock_syscall(_LANDLOCK_RESTRICT_SELF, ruleset, ctypes.c_uint(0))
    finally:
        os.close(ruleset)
    return {
        "abi": abi,
        "handled_write_rights": _LANDLOCK_WRITE_RIGHTS,
        "allowed_write_roots": records,
        "irreversible": True,
        "ok": True,
    }


def _probe_cgroup_write_denial() -> dict[str, Any]:
    path = Path("/sys/fs/cgroup/cgroup.procs")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        _require(exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}, "sandbox cgroup write failed for an unexpected reason")
        return {"path": str(path), "write_open_succeeded": False, "errno": int(exc.errno), "ok": True}
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    raise ComfyUISandboxError("sandbox cgroup view remained writable")


def _bpf_statement(code: int, value: int) -> tuple[int, int, int, int]:
    return (code, 0, 0, value)


def _bpf_jump(code: int, value: int, true_skip: int, false_skip: int) -> tuple[int, int, int, int]:
    _require(0 <= true_skip <= 255 and 0 <= false_skip <= 255, "seccomp jump exceeds classic-BPF range")
    return (code, true_skip, false_skip, value)


def build_seccomp_filter() -> tuple[tuple[int, int, int, int], ...]:
    """Return the reviewed classic-BPF program used by the real stage."""

    deny = _SECCOMP_RET_ERRNO | errno.EPERM
    unavailable = _SECCOMP_RET_ERRNO | errno.ENOSYS
    program: list[tuple[int, int, int, int]] = [
        _bpf_statement(_BPF_LD_W_ABS, 4),
        _bpf_jump(_BPF_JMP_JEQ_K, _AUDIT_ARCH_X86_64, 1, 0),
        _bpf_statement(_BPF_RET_K, _SECCOMP_RET_KILL_PROCESS),
        _bpf_statement(_BPF_LD_W_ABS, 0),
        _bpf_jump(_BPF_JMP_JSET_K, _X32_SYSCALL_BIT, 0, 1),
        _bpf_statement(_BPF_RET_K, _SECCOMP_RET_KILL_PROCESS),
        # AF_UNIX socket(AF_UNIX, ...) is denied; INET sockets proceed.
        _bpf_jump(_BPF_JMP_JEQ_K, _SYSCALLS["socket"], 0, 4),
        _bpf_statement(_BPF_LD_W_ABS, 16),
        _bpf_jump(_BPF_JMP_JEQ_K, _AF_UNIX, 0, 1),
        _bpf_statement(_BPF_RET_K, deny),
        _bpf_statement(_BPF_RET_K, _SECCOMP_RET_ALLOW),
        # AF_UNIX socketpair(AF_UNIX, ...) is denied as well.
        _bpf_jump(_BPF_JMP_JEQ_K, _SYSCALLS["socketpair"], 0, 4),
        _bpf_statement(_BPF_LD_W_ABS, 16),
        _bpf_jump(_BPF_JMP_JEQ_K, _AF_UNIX, 0, 1),
        _bpf_statement(_BPF_RET_K, deny),
        _bpf_statement(_BPF_RET_K, _SECCOMP_RET_ALLOW),
        # Ordinary thread/process clone remains usable, namespace clone does not.
        _bpf_jump(_BPF_JMP_JEQ_K, _SYSCALLS["clone"], 0, 4),
        _bpf_statement(_BPF_LD_W_ABS, 16),
        _bpf_jump(_BPF_JMP_JSET_K, _CLONE_NAMESPACE_MASK, 0, 1),
        _bpf_statement(_BPF_RET_K, deny),
        _bpf_statement(_BPF_RET_K, _SECCOMP_RET_ALLOW),
        # libc/Torch may probe clone3 and must receive ENOSYS to use clone(2).
        _bpf_jump(_BPF_JMP_JEQ_K, _SYSCALLS["clone3"], 0, 1),
        _bpf_statement(_BPF_RET_K, unavailable),
    ]
    for name in _UNCONDITIONAL_DENY_NAMES:
        program.extend((
            _bpf_jump(_BPF_JMP_JEQ_K, _SYSCALLS[name], 0, 1),
            _bpf_statement(_BPF_RET_K, deny),
        ))
    program.append(_bpf_statement(_BPF_RET_K, _SECCOMP_RET_ALLOW))
    _require(len(program) <= 0xFFFF, "seccomp program is too large")
    return tuple(program)


def evaluate_seccomp_filter_for_test(*, architecture: int, syscall: int, arg0: int = 0) -> int:
    """Tiny cBPF interpreter for policy unit tests; never used for authority."""

    accumulator = 0
    pc = 0
    program = build_seccomp_filter()
    values = {0: syscall & 0xFFFFFFFF, 4: architecture & 0xFFFFFFFF, 16: arg0 & 0xFFFFFFFF}
    while pc < len(program):
        code, true_skip, false_skip, value = program[pc]
        if code == _BPF_LD_W_ABS:
            accumulator = values.get(value, 0)
            pc += 1
        elif code == _BPF_JMP_JEQ_K:
            pc += 1 + (true_skip if accumulator == value else false_skip)
        elif code == _BPF_JMP_JSET_K:
            pc += 1 + (true_skip if accumulator & value else false_skip)
        elif code == _BPF_RET_K:
            return value
        else:
            raise AssertionError(f"unsupported test BPF instruction: {code}")
    raise AssertionError("seccomp test evaluator fell off the program")


def _seccomp_policy_evidence(program: Sequence[tuple[int, int, int, int]]) -> dict[str, Any]:
    encoded = b"".join(struct.pack("=HBBI", *instruction) for instruction in program)
    return {
        "audit_arch": _AUDIT_ARCH_X86_64,
        "architecture": "x86_64",
        "bad_arch_action": "KILL_PROCESS",
        "x32_rejected": True,
        "compat_arch_rejected": True,
        "denied_errno": errno.EPERM,
        "clone3_denied_errno": errno.ENOSYS,
        "unconditional_denied_syscalls": {name: _SYSCALLS[name] for name in _UNCONDITIONAL_DENY_NAMES},
        "af_unix_socket_denied": True,
        "af_unix_socketpair_denied": True,
        "legacy_clone_namespace_mask": _CLONE_NAMESPACE_MASK,
        "instruction_count": len(program),
        "program_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _install_seccomp() -> dict[str, Any]:
    program = build_seccomp_filter()
    filters = (_SockFilter * len(program))(*(_SockFilter(*value) for value in program))
    descriptor = _SockFprog(length=len(program), filter=ctypes.cast(filters, ctypes.POINTER(_SockFilter)))
    library = _libc()
    _checked_zero(library.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(descriptor)), action="install seccomp filter")
    mode = int(library.prctl(_PR_GET_SECCOMP, 0, 0, 0, 0))
    _require(mode == _SECCOMP_MODE_FILTER, "seccomp filter mode is not active")
    return {**_seccomp_policy_evidence(program), "mode": mode, "ok": True}


def _drop_privileges(*, seccomp_filters_before: int) -> dict[str, Any]:
    library = _libc()
    _checked_zero(library.prctl(_PR_SET_SECUREBITS, _LOCKED_SECUREBITS, 0, 0, 0), action="lock securebits")
    _checked_zero(library.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0), action="clear ambient capabilities")
    last_cap = int(Path("/proc/sys/kernel/cap_last_cap").read_text(encoding="ascii").strip())
    _require(0 <= last_cap <= 63, "kernel capability range exceeds reviewed capset v3")
    for capability in range(last_cap + 1):
        _checked_zero(library.prctl(_PR_CAPBSET_DROP, capability, 0, 0, 0), action=f"drop bounding capability {capability}")
    header = _CapHeader(version=_LINUX_CAPABILITY_VERSION_3, pid=0)
    data = (_CapData * 2)()
    library.capset.restype = ctypes.c_int
    _checked_zero(library.capset(ctypes.byref(header), ctypes.byref(data)), action="clear effective/permitted/inheritable capabilities")
    _checked_zero(library.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), action="set no_new_privs")
    seccomp = _install_seccomp()
    status = _status_fields(Path("/proc/self/status"))
    caps = {name: status.get(name, "").lower() for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")}
    _require(all(value == "0000000000000000" for value in caps.values()), "one or more capability sets remain nonzero")
    _require(status.get("NoNewPrivs") == "1", "no_new_privs is not active")
    _require(status.get("Seccomp") == str(_SECCOMP_MODE_FILTER), "seccomp status is not filter mode")
    filter_count = int(status.get("Seccomp_filters", "0"))
    _require(filter_count == seccomp_filters_before + 1, "sandbox did not add exactly one seccomp filter")
    securebits = int(library.prctl(_PR_GET_SECUREBITS, 0, 0, 0, 0))
    _require(securebits == _LOCKED_SECUREBITS, "securebits lock differs from the reviewed value")
    return {
        "inside_uid": list(os.getresuid()),
        "inside_gid": list(os.getresgid()),
        "capability_sets": caps,
        "cap_last_cap": last_cap,
        "securebits": securebits,
        "securebits_locked": True,
        "no_new_privileges": True,
        "seccomp_filters_before": seccomp_filters_before,
        "seccomp_filters_after": filter_count,
        "seccomp": seccomp,
        "ok": True,
    }


def _process_record(pid: int | None = None) -> dict[str, Any]:
    target = "self" if pid is None else str(pid)
    status = _status_fields(Path("/proc") / target / "status")
    namespace_pid_values = [int(value) for value in status.get("NSpid", "").split()]
    _require(bool(namespace_pid_values), f"NSpid is unavailable for {target}")
    actual_pid = os.getpid() if pid is None else pid
    return {
        "pid": actual_pid,
        "namespace_pids": namespace_pid_values,
        "namespace_pid": namespace_pid_values[-1],
        "process_group_visible": os.getpgrp() if pid is None else None,
        "status": {
            name: status.get(name)
            for name in ("Uid", "Gid", "NoNewPrivs", "Seccomp", "Seccomp_filters", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        },
    }


def _verify_pinned_executable(path: Path, identity: FileIdentity) -> None:
    metadata = path.lstat()
    _require(FileIdentity.from_stat(metadata) == identity, "fixed command executable changed before exec")
    _require(path.resolve(strict=True) == path and stat.S_ISREG(metadata.st_mode), "fixed command executable lost canonical identity")


def _read_reaper_fds() -> list[int]:
    return sorted(int(value.name) for value in Path("/proc/1/fd").iterdir() if value.name.isdigit())


def _final_workload_delegation_evidence(
    shared: Mapping[str, Any],
    privileges: Mapping[str, Any],
) -> dict[str, Any]:
    mounts = shared.get("mounts")
    landlock = shared.get("landlock")
    denial = shared.get("cgroup_write_denial")
    _require(isinstance(mounts, Mapping), "final delegation proof lacks mount evidence")
    _require(isinstance(landlock, Mapping) and landlock.get("ok") is True, "final delegation proof lacks Landlock")
    _require(isinstance(denial, Mapping) and denial.get("ok") is True, "final delegation proof lacks cgroup write denial")
    cgroup_mount = mounts.get("cgroup2")
    bound_leaf = mounts.get("bound_read_only_leaf_identity")
    _require(
        isinstance(cgroup_mount, Mapping)
        and cgroup_mount.get("filesystem_type") == "cgroup2"
        and cgroup_mount.get("root") == "/"
        and "ro" in (cgroup_mount.get("mount_options") or []),
        "final delegation proof lacks a namespace-rooted read-only cgroup2 view",
    )
    _require(
        mounts.get("cgroup_namespace_path") == "/"
        and mounts.get("leaf_kernel_objects_match") is True,
        "final delegation proof did not bind the pinned host leaf",
    )
    _require(
        isinstance(bound_leaf, Mapping)
        and bound_leaf.get("subtree_controllers_enabled") is False
        and bound_leaf.get("descendant_cgroups") == 0,
        "final delegation proof found subtree controllers or descendant cgroups",
    )
    capability_sets = privileges.get("capability_sets")
    seccomp = privileges.get("seccomp")
    denied_syscalls = seccomp.get("unconditional_denied_syscalls") if isinstance(seccomp, Mapping) else None
    _require(
        isinstance(capability_sets, Mapping)
        and all(value == "0000000000000000" for value in capability_sets.values())
        and privileges.get("securebits_locked") is True
        and privileges.get("no_new_privileges") is True,
        "final delegation proof retains privilege",
    )
    _require(
        isinstance(denied_syscalls, Mapping)
        and {"setns", "unshare", "mount", "umount2", "move_mount", "mount_setattr"}
        <= set(denied_syscalls)
        and seccomp.get("legacy_clone_namespace_mask") == _CLONE_NAMESPACE_MASK
        and seccomp.get("ok") is True,
        "final delegation proof lacks namespace and mount syscall denial",
    )
    return {
        "workload_delegation_capability": False,
        "host_leaf_state_before_sandbox": "pending_sandbox",
        "subtree_controllers_enabled": False,
        "descendant_cgroups": 0,
        "namespace_rooted_cgroup2": True,
        "cgroup2_read_only": True,
        "landlock_active": True,
        "capability_sets_zero": True,
        "securebits_locked": True,
        "namespace_and_mount_syscalls_denied": True,
        "ok": True,
    }


def _payload_main(
    config: ComfyUISandboxConfig,
    shared: Mapping[str, Any],
    sync_read_fd: int,
    sync_write_fd: int,
    executable: Path,
    executable_identity: FileIdentity,
) -> NoReturn:
    os.close(sync_write_fd)
    try:
        reaper = _read_bounded_json_line(sync_read_fd, limit=MAX_REAPER_SYNC_BYTES)
    finally:
        os.close(sync_read_fd)
    _require(reaper.get("ok") is True, f"sandbox reaper hardening failed: {reaper.get('error')}")
    privileges = _drop_privileges(seccomp_filters_before=int(shared["host"]["seccomp_filters_before"]))
    reaper_fds = _read_reaper_fds()
    _require(reaper_fds == [0, 1, 2], f"sandbox reaper retained descriptors: {reaper_fds}")
    fd_final = _close_and_audit_fds({0, 1, 2, config.proof_fd})
    descriptor = _descriptor_record(config.proof_fd)
    _require(descriptor == shared["descriptor_contract"]["proof_pipe"], "sandbox proof pipe identity changed")
    _verify_pinned_executable(executable, executable_identity)
    cwd, cwd_identity = _canonical_directory(config.cwd, label="sandbox cwd before exec")
    _require(cwd_identity.as_dict(path=cwd) == shared["cwd_identity"], "sandbox cwd identity changed")
    os.chdir(cwd)
    payload_process = _process_record()
    _require(payload_process["namespace_pid"] > 1 and os.getppid() == 1, "payload is not a child of the namespace reaper")
    namespace_links = _namespace_links()
    for name in ("user", "mnt", "cgroup", "pid"):
        _require(namespace_links[name] != shared["host"]["namespace_links"][name], f"payload did not enter a fresh {name} namespace")
    command_hash = hashlib.sha256(
        json.dumps(list(config.command), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    delegation = _final_workload_delegation_evidence(shared, privileges)
    proof = {
        "schema_version": SANDBOX_PROOF_SCHEMA_VERSION,
        "nonce": config.nonce,
        "actual_execution": True,
        "simulated": False,
        "adopted_external_process": False,
        "shell": False,
        "fixed_command": list(config.command),
        "fixed_command_sha256": command_hash,
        "command_executable": executable_identity.as_dict(path=executable),
        "cwd": shared["cwd_identity"],
        "environment_keys": sorted(config.environment),
        "host_transition": shared["host"]["host_transition"],
        "expected_host_cgroup_path": config.expected_cgroup_path,
        "allowed_write_roots": _root_records(config.allowed_write_roots),
        "launcher": {
            "host_pid": shared["launcher_host_pid"],
            "host_process_group": shared["launcher_process_group"],
            "host_session": shared["launcher_session"],
            "process_group_leader": True,
        },
        "namespace": shared["namespace"],
        "namespace_links": namespace_links,
        "mounts": shared["mounts"],
        "landlock": shared["landlock"],
        "cgroup_write_denial": shared["cgroup_write_denial"],
        "workload_delegation_capability": False,
        "workload_delegation_confinement": delegation,
        "reaper": {
            **reaper,
            "open_fds_after_sync": reaper_fds,
            "process": _process_record(1),
        },
        "payload": payload_process,
        "privileges": privileges,
        "descriptor_contract": shared["descriptor_contract"],
        "fd_audit_before_fork": shared["fd_audit_before_fork"],
        "fd_audit_before_exec": fd_final,
        "proof_written_before_exec": True,
        "outer_launcher_preserves_process_group": True,
        "reaper_preserves_wait_status": True,
        "ok": True,
    }
    _write_json_line(config.proof_fd, proof)
    os.close(config.proof_fd)
    os.execve(str(executable), list(config.command), dict(config.environment))
    raise AssertionError("execve returned")


def _kill_namespace_descendants() -> bool:
    try:
        os.kill(-1, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            while True:
                child, _status = os.waitpid(-1, os.WNOHANG)
                if child == 0:
                    break
        except ChildProcessError:
            return True
        time.sleep(0.01)
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            child, _status = os.waitpid(-1, os.WNOHANG)
            if child == 0:
                time.sleep(0.01)
                continue
        except ChildProcessError:
            return True
    return False


def _mirror_wait_status(status_value: int) -> NoReturn:
    if os.WIFEXITED(status_value):
        os._exit(os.WEXITSTATUS(status_value))
    if os.WIFSIGNALED(status_value):
        number = os.WTERMSIG(status_value)
        if number not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)
        os._exit(128 + number)
    os._exit(SANDBOX_EXIT_FAILURE)


def _reaper_main(
    config: ComfyUISandboxConfig,
    shared: Mapping[str, Any],
    sync_read_fd: int,
    sync_write_fd: int,
    payload_pid: int,
    pending_signals: Sequence[int],
) -> NoReturn:
    os.close(sync_read_fd)
    os.close(config.proof_fd)
    forwarded: list[int] = []

    def forward(number: int, _frame: Any) -> None:
        forwarded.append(number)
        try:
            os.kill(payload_pid, number)
        except ProcessLookupError:
            pass

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(number, forward)
    for number in pending_signals:
        forward(int(number), None)
    try:
        fd_audit = _close_and_audit_fds({0, 1, 2, sync_write_fd})
        privileges = _drop_privileges(seccomp_filters_before=int(shared["host"]["seccomp_filters_before"]))
        record = {
            "namespace_pid": os.getpid(),
            "parent_namespace_pid": os.getppid(),
            "fd_audit_before_sync_close": fd_audit,
            "privileges": privileges,
            "signal_forwarding": ["SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"],
            "trusted_pid1_reaper": os.getpid() == 1,
            "ok": os.getpid() == 1,
        }
        _write_json_line(sync_write_fd, record, limit=MAX_REAPER_SYNC_BYTES)
    except BaseException as exc:
        try:
            _write_json_line(sync_write_fd, {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}, limit=MAX_REAPER_SYNC_BYTES)
        except BaseException:
            pass
        try:
            os.kill(payload_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    finally:
        try:
            os.close(sync_write_fd)
        except OSError:
            pass
    try:
        waited_pid, status_value = os.waitpid(payload_pid, 0)
        _require(waited_pid == payload_pid, "reaper waited for another payload")
    except BaseException:
        _kill_namespace_descendants()
        os._exit(SANDBOX_EXIT_FAILURE)
    drained = _kill_namespace_descendants()
    if not drained:
        os._exit(SANDBOX_EXIT_FAILURE)
    _mirror_wait_status(status_value)


def _namespace_child_main(
    config: ComfyUISandboxConfig,
    shared: dict[str, Any],
    executable: Path,
    executable_identity: FileIdentity,
) -> NoReturn:
    namespace = _enter_namespaces(shared["host"])
    pending_signals: list[int] = []

    def queue_signal(number: int, _frame: Any) -> None:
        pending_signals.append(number)

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(number, queue_signal)
    pid1 = os.fork()
    if pid1 != 0:
        os.close(config.proof_fd)
        _close_and_audit_fds({0, 1, 2})

        def forward_to_pid1(number: int, _frame: Any) -> None:
            try:
                os.kill(pid1, number)
            except ProcessLookupError:
                pass

        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(number, forward_to_pid1)
        while True:
            try:
                waited_pid, status_value = os.waitpid(pid1, 0)
                break
            except InterruptedError:
                continue
        _require(waited_pid == pid1, "namespace parent waited for another process")
        _mirror_wait_status(status_value)
    # This is the first child after CLONE_NEWPID and must therefore be PID 1.
    _require(os.getpid() == 1, "namespace launcher did not become PID 1")
    namespace["pid_namespace_link"] = os.readlink("/proc/self/ns/pid")
    mounts = _setup_mounts(config, shared["host"])
    namespace["after_pid1"] = _namespace_links()
    landlock = _install_landlock(config.allowed_write_roots)
    cgroup_denial = _probe_cgroup_write_denial()
    shared.update({
        "namespace": namespace,
        "mounts": mounts,
        "landlock": landlock,
        "cgroup_write_denial": cgroup_denial,
    })
    sync_read_fd, sync_write_fd = os.pipe2(getattr(os, "O_CLOEXEC", 0))
    payload_pid = os.fork()
    if payload_pid == 0:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(number, signal.SIG_DFL)
        try:
            _payload_main(config, shared, sync_read_fd, sync_write_fd, executable, executable_identity)
        except BaseException as exc:
            try:
                _write_json_line(config.proof_fd, {
                    "schema_version": SANDBOX_PROOF_SCHEMA_VERSION,
                    "nonce": config.nonce,
                    "actual_execution": True,
                    "simulated": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                    "ok": False,
                })
            except BaseException:
                pass
            os._exit(SANDBOX_EXIT_FAILURE)
    _reaper_main(
        config,
        shared,
        sync_read_fd,
        sync_write_fd,
        payload_pid,
        pending_signals,
    )


def _outer_wait(child_pid: int, process_group: int) -> NoReturn:
    _require(os.getpgid(child_pid) == process_group, "namespace launcher left the caller process group")

    def forward(number: int, _frame: Any) -> None:
        try:
            os.kill(child_pid, number)
        except ProcessLookupError:
            pass

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(number, forward)
    while True:
        try:
            waited_pid, status_value = os.waitpid(child_pid, 0)
            break
        except InterruptedError:
            continue
    _require(waited_pid == child_pid, "outer launcher waited for another process")
    _mirror_wait_status(status_value)


def launch_comfyui_sandbox(config: ComfyUISandboxConfig) -> NoReturn:
    """Perform the real fail-closed transition and exact command exec."""

    executable, executable_identity = _canonical_executable(config.command[0])
    cwd, cwd_identity = _canonical_directory(config.cwd, label="sandbox cwd")
    descriptor_contract = _validate_descriptor_contract(config.proof_fd)
    host = _validate_host_kernel(config)
    launcher_pid = os.getpid()
    launcher_group = os.getpgrp()
    launcher_session = os.getsid(0)
    _require(launcher_group == launcher_pid, "sandbox launcher must already be the process-group leader")
    _require(launcher_session == launcher_pid, "sandbox launcher must already be the session leader")
    fd_audit = _close_and_audit_fds({0, 1, 2, config.proof_fd})
    shared: dict[str, Any] = {
        "host": host,
        "launcher_host_pid": launcher_pid,
        "launcher_process_group": launcher_group,
        "launcher_session": launcher_session,
        "descriptor_contract": descriptor_contract,
        "fd_audit_before_fork": fd_audit,
        "cwd_identity": cwd_identity.as_dict(path=cwd),
    }
    child_pid = os.fork()
    if child_pid == 0:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(number, signal.SIG_DFL)
        try:
            _namespace_child_main(config, shared, executable, executable_identity)
        except BaseException as exc:
            try:
                _write_json_line(config.proof_fd, {
                    "schema_version": SANDBOX_PROOF_SCHEMA_VERSION,
                    "nonce": config.nonce,
                    "actual_execution": True,
                    "simulated": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                    "ok": False,
                })
            except BaseException:
                pass
            os._exit(SANDBOX_EXIT_FAILURE)
    os.close(config.proof_fd)
    _outer_wait(child_pid, launcher_group)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enter the formal ComfyUI namespace sandbox")
    parser.add_argument("--host-transition-json", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--expected-cgroup-path", required=True)
    parser.add_argument("--allow-write-root", action="append", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--proof-fd", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def config_from_args(args: argparse.Namespace) -> ComfyUISandboxConfig:
    command = tuple(str(value) for value in (args.command or []))
    if command and command[0] == "--":
        command = command[1:]
    return ComfyUISandboxConfig(
        host_transition=_unique_json_object(args.host_transition_json),
        nonce=str(args.nonce),
        expected_cgroup_path=str(args.expected_cgroup_path),
        allowed_write_roots=tuple(Path(value) for value in args.allow_write_root),
        command=command,
        cwd=Path(args.cwd),
        proof_fd=int(args.proof_fd),
        environment=os.environ.copy(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = config_from_args(build_parser().parse_args(argv))
        launch_comfyui_sandbox(config)
    except Exception as exc:
        print(f"ComfyUI sandbox refused launch: {exc}", file=sys.stderr, flush=True)
        return SANDBOX_EXIT_FAILURE
    return SANDBOX_EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
