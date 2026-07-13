#!/usr/bin/env python3
"""Git-backed source freeze evidence with lightweight runtime drift checks."""

from __future__ import annotations

import copy
import ctypes
import fnmatch
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SOURCE_FREEZE_SCHEMA_VERSION = "hackme.source-freeze.v3"
SOURCE_DRIFT_SCHEMA_VERSION = "hackme.source-drift.v4"

# These gitignored files directly alter how the server is launched.  They are
# reviewed source authority, not disposable runtime output, so a broad
# ``git check-ignore`` result must never hide them from the campaign freeze.
REVIEWED_PROTECTED_IGNORED_PATHS = (
    ".hackme_capacity_defaults.env",
    ".hackme_capacity_report.json",
)

_IN_MODIFY = 0x00000002
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_UNMOUNT = 0x00002000
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_ISDIR = 0x40000000
_INOTIFY_MUTATION_MASK = (
    _IN_MODIFY
    | _IN_ATTRIB
    | _IN_CLOSE_WRITE
    | _IN_MOVED_FROM
    | _IN_MOVED_TO
    | _IN_CREATE
    | _IN_DELETE
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
    | _IN_UNMOUNT
)
_INOTIFY_EVENT = struct.Struct("iIII")
_MAX_RUNTIME_EVENTS_PER_CHECK = 20_000
_DEFAULT_METADATA_RECONCILE_SECONDS = 300.0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class SourceFreezeError(RuntimeError):
    """Source state could not be proven clean and stable."""


@dataclass(frozen=True)
class TrackedEntry:
    path: str
    index_mode: str
    index_oid: str
    stage: int
    kind: str
    working_sha256: str
    symlink_target: str
    submodule_head: str
    filesystem_mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "index_mode": self.index_mode,
            "index_oid": self.index_oid,
            "stage": self.stage,
            "kind": self.kind,
            "working_sha256": self.working_sha256,
            "symlink_target": self.symlink_target,
            "submodule_head": self.submodule_head,
            "filesystem_mode": self.filesystem_mode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "inode": self.inode,
            "device": self.device,
        }


@dataclass(frozen=True)
class UntrackedEntry:
    path: str
    kind: str
    working_sha256: str
    symlink_target: str
    filesystem_mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "working_sha256": self.working_sha256,
            "symlink_target": self.symlink_target,
            "filesystem_mode": self.filesystem_mode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "inode": self.inode,
            "device": self.device,
        }


@dataclass(frozen=True)
class RuntimeMutationEvent:
    source: str
    path: str
    mask: int
    cookie: int
    is_directory: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "mask": self.mask,
            "cookie": self.cookie,
            "is_directory": self.is_directory,
        }


class _RuntimeDriftMonitor:
    """A bounded inotify collector; it never writes per-poll evidence directories."""

    def __init__(
        self,
        owner: "GitSourceFreezer",
        *,
        metadata_reconcile_seconds: float = _DEFAULT_METADATA_RECONCILE_SECONDS,
    ):
        self.owner = weakref.proxy(owner)
        self.metadata_reconcile_seconds = max(30.0, float(metadata_reconcile_seconds))
        self.started_at = utc_now()
        self.mode = "metadata_fallback"
        self.fd = -1
        self.libc: Any | None = None
        self.watches: dict[int, tuple[str, Path]] = {}
        self.paths_to_watch: dict[tuple[str, str], int] = {}
        self.errors: list[str] = []
        self.watch_add_attempts = 0
        self.watch_add_failures = 0
        self.events_seen = 0
        self.queue_overflows = 0
        self.self_test_passed = False
        self.protected_ignored_watch_coverage: dict[str, dict[str, Any]] = {}
        self.first_reconciliation_pending = True
        self.last_reconciled_at: str | None = None
        self.next_reconcile_monotonic = 0.0
        self._bootstrap_events: list[RuntimeMutationEvent] = []
        self._closed = False
        self._initialize()

    def _error(self, message: str) -> None:
        if len(self.errors) < 100:
            self.errors.append(str(message)[:1000])

    def _initialize(self) -> None:
        if not sys.platform.startswith("linux"):
            self._error(f"inotify is unavailable on platform {sys.platform}")
            return
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            init = libc.inotify_init1
            init.argtypes = [ctypes.c_int]
            init.restype = ctypes.c_int
            add = libc.inotify_add_watch
            add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            add.restype = ctypes.c_int
            fd = int(init(os.O_NONBLOCK | os.O_CLOEXEC))
            if fd < 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
            self.libc = libc
            self.fd = fd
            self.mode = "inotify"
            self._add_tree(self.owner.repo_root, "source")
            self._ensure_protected_ignored_watches()
            for root in self._git_authority_roots():
                self._add_tree(root, "git")
            self._self_test()
        except Exception as exc:
            self._error(f"inotify initialization failed: {exc.__class__.__name__}: {exc}")
            self.close()
            self.mode = "metadata_fallback"

    def _ensure_protected_ignored_watches(self) -> None:
        coverage: dict[str, dict[str, Any]] = {}
        for relative in self.owner.protected_ignored_paths:
            parent = self.owner._repo_path(relative).parent.resolve()
            self._add_watch(parent, "source")
            watched = ("source", str(parent)) in self.paths_to_watch
            coverage[relative] = {
                "parent": str(parent),
                "watched": watched,
            }
            if not watched:
                self._error(f"protected ignored launcher input is unobservable: {relative}")
        self.protected_ignored_watch_coverage = coverage

    def _protected_ignored_observable(self) -> bool:
        coverage: dict[str, dict[str, Any]] = {}
        observable = self.mode == "inotify" and self.fd >= 0
        for relative in self.owner.protected_ignored_paths:
            parent = self.owner._repo_path(relative).parent.resolve()
            watched = ("source", str(parent)) in self.paths_to_watch
            coverage[relative] = {
                "parent": str(parent),
                "watched": watched,
            }
            observable = bool(observable and watched)
        self.protected_ignored_watch_coverage = coverage
        return observable

    def _git_authority_roots(self) -> list[Path]:
        roots: list[Path] = []
        for args in (("rev-parse", "--absolute-git-dir"), ("rev-parse", "--git-common-dir")):
            raw = self.owner._git_text(*args).strip()
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = self.owner.repo_root / path
            path = path.resolve()
            if path.is_dir() and path not in roots:
                roots.append(path)
        return roots

    def _add_watch(self, path: Path, source: str) -> None:
        if self.fd < 0 or self.libc is None:
            return
        resolved = path.resolve()
        key = (source, str(resolved))
        if key in self.paths_to_watch:
            return
        self.watch_add_attempts += 1
        encoded = os.fsencode(resolved)
        wd = int(self.libc.inotify_add_watch(self.fd, encoded, _INOTIFY_MUTATION_MASK))
        if wd < 0:
            self.watch_add_failures += 1
            error_number = ctypes.get_errno()
            self._error(f"cannot watch {source}:{resolved}: [{error_number}] {os.strerror(error_number)}")
            return
        previous = self.watches.get(wd)
        if previous is not None and previous != (source, resolved):
            self.watch_add_failures += 1
            self._error(f"inotify watch descriptor collision for {resolved}")
            return
        self.watches[wd] = (source, resolved)
        self.paths_to_watch[key] = wd

    def _add_tree(self, root: Path, source: str) -> None:
        if not root.is_dir():
            self._error(f"watch root is not a directory: {source}:{root}")
            return
        def on_walk_error(exc: OSError) -> None:
            self._error(f"cannot enumerate watch tree {source}:{root}: [{exc.errno}] {exc.strerror}")

        for directory, names, _files in os.walk(
            root,
            topdown=True,
            onerror=on_walk_error,
            followlinks=False,
        ):
            if source == "source":
                names[:] = [name for name in names if name != ".git"]
            self._add_watch(Path(directory), source)

    def add_source_tree(self, path: Path) -> None:
        if self.mode == "inotify" and path.is_dir():
            self._add_tree(path, "source")

    def _self_test(self) -> None:
        probe = self.owner.artifact_root / "runtime_drift" / ".monitor_probe"
        probe.mkdir(parents=True, exist_ok=True)
        self._add_watch(probe, "probe")
        marker = probe / f"probe-{os.getpid()}-{time.time_ns()}"
        marker.write_bytes(b"inotify-self-test\n")
        marker.unlink()
        observed_probe = False
        for event in self._read_events():
            if event.source == "probe" and event.path == marker.name:
                observed_probe = True
            elif event.source != "probe":
                self._bootstrap_events.append(event)
        if not observed_probe:
            self._error("inotify self-test mutation was not observed")
        self.self_test_passed = observed_probe

    def _read_events(self) -> list[RuntimeMutationEvent]:
        if self.fd < 0:
            return []
        parsed: list[RuntimeMutationEvent] = []
        while len(parsed) < _MAX_RUNTIME_EVENTS_PER_CHECK:
            try:
                payload = os.read(self.fd, 1024 * 1024)
            except BlockingIOError:
                break
            except OSError as exc:
                self._error(f"inotify read failed: [{exc.errno}] {exc.strerror}")
                break
            if not payload:
                self._error("inotify returned EOF")
                break
            offset = 0
            while offset + _INOTIFY_EVENT.size <= len(payload):
                wd, mask, cookie, name_length = _INOTIFY_EVENT.unpack_from(payload, offset)
                offset += _INOTIFY_EVENT.size
                if offset + name_length > len(payload):
                    self._error("truncated inotify event payload")
                    offset = len(payload)
                    break
                raw_name = payload[offset:offset + name_length].split(b"\0", 1)[0]
                offset += name_length
                if mask & _IN_Q_OVERFLOW:
                    self.queue_overflows += 1
                    parsed.append(RuntimeMutationEvent("monitor", "@queue_overflow", mask, cookie, False))
                    continue
                watch = self.watches.get(wd)
                if watch is None:
                    if not (mask & _IN_IGNORED):
                        self._error(f"event arrived for unknown watch descriptor {wd}")
                    continue
                source, directory = watch
                name = os.fsdecode(raw_name)
                event_path = directory / name if name else directory
                if source == "source":
                    try:
                        reported_path = event_path.relative_to(self.owner.repo_root).as_posix()
                    except ValueError:
                        self._error(f"source event escaped repository: {event_path}")
                        reported_path = "@escaped"
                elif source == "git":
                    reported_path = f"@git/{event_path.name}" if not name else f"@git/{name}"
                else:
                    reported_path = name
                parsed.append(RuntimeMutationEvent(
                    source=source,
                    path=reported_path,
                    mask=int(mask),
                    cookie=int(cookie),
                    is_directory=bool(mask & _IN_ISDIR),
                ))
                if mask & _IN_IGNORED:
                    self.watches.pop(wd, None)
                    self.paths_to_watch.pop((source, str(directory)), None)
            if len(payload) < 1024 * 1024:
                continue
        if len(parsed) >= _MAX_RUNTIME_EVENTS_PER_CHECK:
            self._error("runtime event safety bound reached before queue was drained")
        self.events_seen += len(parsed)
        return parsed

    def drain(self) -> list[RuntimeMutationEvent]:
        events = self._bootstrap_events
        self._bootstrap_events = []
        events.extend(self._read_events())
        return events

    def reconciliation_due(self) -> bool:
        return self.first_reconciliation_pending or time.monotonic() >= self.next_reconcile_monotonic

    def mark_reconciled(self) -> None:
        self.first_reconciliation_pending = False
        self.last_reconciled_at = utc_now()
        self.next_reconcile_monotonic = time.monotonic() + self.metadata_reconcile_seconds

    def health(self) -> dict[str, Any]:
        protected_ignored_observable = self._protected_ignored_observable()
        inotify_effective = bool(
            self.mode == "inotify"
            and self.fd >= 0
            and self.self_test_passed
            and self.watches
            and not self.errors
            and self.queue_overflows == 0
            and protected_ignored_observable
        )
        return {
            "mode": self.mode,
            "active": bool(self.fd >= 0) if self.mode == "inotify" else True,
            "machine_verified": inotify_effective,
            "formal_eligible": inotify_effective,
            "self_test_passed": self.self_test_passed,
            "watch_count": len(self.watches),
            "watch_add_attempts": self.watch_add_attempts,
            "watch_add_failures": self.watch_add_failures,
            "events_seen": self.events_seen,
            "queue_overflows": self.queue_overflows,
            "collector_errors": list(self.errors),
            "protected_ignored_paths": list(self.owner.protected_ignored_paths),
            "protected_ignored_observable": protected_ignored_observable,
            "protected_ignored_watch_coverage": copy.deepcopy(self.protected_ignored_watch_coverage),
            "first_reconciliation_completed": not self.first_reconciliation_pending,
            "last_reconciled_at": self.last_reconciled_at,
            "metadata_reconcile_seconds": self.metadata_reconcile_seconds,
            "started_at": self.started_at,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = -1
        self.watches.clear()
        self.paths_to_watch.clear()

    def __del__(self) -> None:
        self.close()


class GitSourceFreezer:
    """Captures both Git authority and a full tracked working-tree manifest."""

    def __init__(self, repo_root: Path, artifact_root: Path, *, untracked_allowlist: Iterable[str] = ()):
        self.repo_root = Path(repo_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        try:
            self.artifact_root.relative_to(self.repo_root)
        except ValueError:
            pass
        else:
            raise SourceFreezeError("source-freeze artifacts must be outside the repository root")
        self.untracked_allowlist = tuple(str(pattern) for pattern in untracked_allowlist)
        self.protected_ignored_paths = tuple(REVIEWED_PROTECTED_IGNORED_PATHS)
        for relative in self.protected_ignored_paths:
            self._repo_path(relative)
        self.baseline: dict[str, Any] | None = None
        self._baseline_entries: dict[str, TrackedEntry] = {}
        self._baseline_untracked_entries: dict[str, UntrackedEntry] = {}
        self._baseline_protected_ignored_entries: dict[str, UntrackedEntry] = {}
        self._runtime_monitor: _RuntimeDriftMonitor | None = None
        self._drift_lock = threading.RLock()
        self._runtime_incident: dict[str, Any] | None = None
        self._ignored_runtime_prefixes: set[str] = set()
        self._baseline_parent_directories: set[str] = set()

    def _refresh_baseline_parent_directories(self) -> None:
        parents: set[str] = set()
        for relative in (
            *self._baseline_entries,
            *self._baseline_untracked_entries,
            *self._baseline_protected_ignored_entries,
        ):
            parent = Path(relative).parent
            while parent != Path("."):
                parents.add(parent.as_posix())
                parent = parent.parent
        self._baseline_parent_directories = parents

    def _start_runtime_monitor(self) -> None:
        if self._runtime_monitor is not None:
            self._runtime_monitor.close()
        self._runtime_incident = None
        self._ignored_runtime_prefixes.clear()
        self._runtime_monitor = _RuntimeDriftMonitor(self)

    def _git_bytes(self, *args: str, timeout: int = 60) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repo_root), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                timeout=timeout,
                check=False,
            )
        except Exception as exc:
            raise SourceFreezeError(f"git {' '.join(args)} failed: {exc.__class__.__name__}: {exc}") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")[:1000]
            raise SourceFreezeError(f"git {' '.join(args)} returned {completed.returncode}: {stderr}")
        return completed.stdout

    def _git_text(self, *args: str, timeout: int = 60) -> str:
        return self._git_bytes(*args, timeout=timeout).decode("utf-8", errors="replace")

    @staticmethod
    def _parse_status_rows(raw: bytes) -> list[dict[str, str]]:
        items = raw.split(b"\0")
        rows: list[dict[str, str]] = []
        index = 0
        while index < len(items):
            item = items[index]
            index += 1
            if not item:
                continue
            text = item.decode("utf-8", errors="surrogateescape")
            if len(text) < 4:
                rows.append({"status": "??", "path": text})
                continue
            status_code = text[:2]
            path = text[3:]
            row = {"status": status_code, "path": path}
            if "R" in status_code or "C" in status_code:
                if index < len(items) and items[index]:
                    row["source_path"] = items[index].decode("utf-8", errors="surrogateescape")
                    index += 1
            rows.append(row)
        return rows

    def _status_bytes(self) -> bytes:
        return self._git_bytes(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )

    def _status_rows(self) -> list[dict[str, str]]:
        return self._parse_status_rows(self._status_bytes())

    def _status_policy(self, rows: Iterable[Mapping[str, str]]) -> dict[str, Any]:
        allowed: list[dict[str, str]] = []
        blocked: list[dict[str, str]] = []
        for source in rows:
            row = dict(source)
            path = str(row.get("path") or "")
            is_allowed = row.get("status") == "??" and any(fnmatch.fnmatch(path, pattern) for pattern in self.untracked_allowlist)
            (allowed if is_allowed else blocked).append(row)
        return {
            "untracked_allowlist": list(self.untracked_allowlist),
            "allowed_untracked": allowed,
            "blocked_changes": blocked,
            "clean": not blocked,
        }

    def _index_rows(self) -> list[tuple[str, str, int, str]]:
        raw = self._git_bytes("ls-files", "-s", "-z")
        rows: list[tuple[str, str, int, str]] = []
        for item in raw.split(b"\0"):
            if not item:
                continue
            metadata, path_bytes = item.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            rows.append((mode, oid, int(stage), path_bytes.decode("utf-8", errors="surrogateescape")))
        return rows

    def _untracked_paths(self) -> list[str]:
        raw = self._git_bytes("ls-files", "--others", "--exclude-standard", "-z")
        return [
            item.decode("utf-8", errors="surrogateescape")
            for item in raw.split(b"\0")
            if item
        ]

    def _repo_path(self, relative: str) -> Path:
        if not relative or relative.startswith("/"):
            raise SourceFreezeError(f"unsafe repository-relative path: {relative!r}")
        parts = Path(relative).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise SourceFreezeError(f"unsafe repository-relative path: {relative!r}")
        return self.repo_root.joinpath(*parts)

    @staticmethod
    def _entry_stat(info: os.stat_result) -> dict[str, int]:
        return {
            "filesystem_mode": int(stat.S_IMODE(info.st_mode)),
            "size": int(info.st_size),
            "mtime_ns": int(info.st_mtime_ns),
            "ctime_ns": int(info.st_ctime_ns),
            "inode": int(info.st_ino),
            "device": int(info.st_dev),
        }

    def _submodule_head(self, path: Path) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                timeout=30,
                check=False,
            )
        except Exception:
            return ""
        if completed.returncode != 0:
            return ""
        value = completed.stdout.decode("ascii", errors="replace").strip()
        return value if re_full_sha(value) else ""

    def tracked_entries(self) -> list[TrackedEntry]:
        entries: list[TrackedEntry] = []
        for mode, oid, stage, relative in self._index_rows():
            path = self._repo_path(relative)
            try:
                info = path.lstat()
            except FileNotFoundError:
                entries.append(TrackedEntry(
                    relative, mode, oid, stage,
                    "submodule_missing" if mode == "160000" else "missing",
                    "", "", "", -1, -1, -1, -1, -1, -1,
                ))
                continue
            if mode == "160000":
                target = ""
                kind = "submodule"
                submodule_head = self._submodule_head(path)
                digest = sha256_bytes(f"{oid}\0{submodule_head}".encode("ascii"))
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                kind = "symlink"
                submodule_head = ""
                digest = sha256_bytes(target.encode("utf-8", errors="surrogateescape"))
            elif stat.S_ISREG(info.st_mode):
                target = ""
                kind = "file"
                submodule_head = ""
                digest = sha256_file(path)
            else:
                target = ""
                kind = "other"
                submodule_head = ""
                digest = ""
            stat_values = self._entry_stat(info)
            entries.append(TrackedEntry(
                path=relative,
                index_mode=mode,
                index_oid=oid,
                stage=stage,
                kind=kind,
                working_sha256=digest,
                symlink_target=target,
                submodule_head=submodule_head,
                **stat_values,
            ))
        return entries

    def untracked_entries(self) -> list[UntrackedEntry]:
        entries: list[UntrackedEntry] = []
        for relative in self._untracked_paths():
            path = self._repo_path(relative)
            try:
                info = path.lstat()
            except FileNotFoundError:
                entries.append(UntrackedEntry(relative, "missing", "", "", -1, -1, -1, -1, -1, -1))
                continue
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                kind = "symlink"
                digest = sha256_bytes(target.encode("utf-8", errors="surrogateescape"))
            elif stat.S_ISREG(info.st_mode):
                target = ""
                kind = "file"
                digest = sha256_file(path)
            else:
                target = ""
                kind = "other"
                digest = ""
            entries.append(UntrackedEntry(
                path=relative,
                kind=kind,
                working_sha256=digest,
                symlink_target=target,
                **self._entry_stat(info),
            ))
        return entries

    def protected_ignored_entries(self) -> list[UntrackedEntry]:
        """Snapshot reviewed ignored launcher inputs, including their absence."""

        entries: list[UntrackedEntry] = []
        for relative in self.protected_ignored_paths:
            path = self._repo_path(relative)
            try:
                info = path.lstat()
            except FileNotFoundError:
                entries.append(UntrackedEntry(relative, "missing", "", "", -1, -1, -1, -1, -1, -1))
                continue
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                kind = "symlink"
                digest = sha256_bytes(target.encode("utf-8", errors="surrogateescape"))
            elif stat.S_ISREG(info.st_mode):
                target = ""
                kind = "file"
                digest = sha256_file(path)
            else:
                target = ""
                kind = "other"
                digest = ""
            entries.append(UntrackedEntry(
                path=relative,
                kind=kind,
                working_sha256=digest,
                symlink_target=target,
                **self._entry_stat(info),
            ))
        return entries

    def _protected_ignored_policy(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for relative in self.protected_ignored_paths:
            try:
                completed = subprocess.run(
                    ["git", "-C", str(self.repo_root), "check-ignore", "-q", "--no-index", "--", relative],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                    timeout=30,
                    check=False,
                )
            except Exception as exc:
                raise SourceFreezeError(
                    f"cannot classify protected ignored launcher input {relative}: "
                    f"{exc.__class__.__name__}: {exc}"
                ) from exc
            if completed.returncode not in {0, 1}:
                detail = completed.stderr.decode("utf-8", errors="replace")[:500]
                raise SourceFreezeError(
                    f"git check-ignore returned {completed.returncode} for protected input {relative}: {detail}"
                )
            rows.append({
                "path": relative,
                "reviewed": True,
                "git_ignored": completed.returncode == 0,
                "authority_class": "protected_ignored_launcher_input",
            })
        return {
            "policy": "explicit_reviewed_list",
            "broad_ignored_runtime_is_excluded": True,
            "paths": rows,
        }

    @staticmethod
    def manifest_digest(entries: Iterable[TrackedEntry]) -> str:
        digest = hashlib.sha256()
        for entry in sorted(entries, key=lambda item: item.path):
            canonical = json.dumps(entry.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            digest.update(canonical.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def content_digest(entries: Iterable[TrackedEntry]) -> str:
        digest = hashlib.sha256()
        for entry in sorted(entries, key=lambda item: item.path):
            digest.update(entry.path.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(entry.index_mode.encode("ascii"))
            digest.update(b"\0")
            digest.update(entry.working_sha256.encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def untracked_manifest_digest(entries: Iterable[UntrackedEntry]) -> str:
        digest = hashlib.sha256()
        for entry in sorted(entries, key=lambda item: item.path):
            canonical = json.dumps(entry.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            digest.update(canonical.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def untracked_content_digest(entries: Iterable[UntrackedEntry]) -> str:
        digest = hashlib.sha256()
        for entry in sorted(entries, key=lambda item: item.path):
            digest.update(entry.path.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(entry.kind.encode("ascii"))
            digest.update(b"\0")
            digest.update(str(entry.filesystem_mode).encode("ascii"))
            digest.update(b"\0")
            digest.update(entry.working_sha256.encode("ascii"))
            digest.update(b"\0")
            digest.update(entry.symlink_target.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _authority_snapshot(self) -> dict[str, Any]:
        status_raw = self._status_bytes()
        return {
            "commit": self._git_text("rev-parse", "HEAD").strip(),
            "branch": self._git_text("rev-parse", "--abbrev-ref", "HEAD").strip(),
            "status_raw": status_raw,
            "status_rows": self._parse_status_rows(status_raw),
            "diff_binary": self._git_bytes(
                "diff", "--no-ext-diff", "--binary", "--ignore-submodules=none", "HEAD", "--", timeout=120,
            ),
            "ls_files": self._git_bytes("ls-files", "-s", "-z"),
            "submodule": self._git_bytes("submodule", "status", "--recursive", timeout=120),
        }

    @staticmethod
    def _authority_digests(snapshot: Mapping[str, Any]) -> dict[str, str]:
        return {
            "commit": str(snapshot["commit"]),
            "branch": str(snapshot["branch"]),
            "git_status_sha256": sha256_bytes(snapshot["status_raw"]),
            "git_diff_binary_sha256": sha256_bytes(snapshot["diff_binary"]),
            "git_ls_files_sha256": sha256_bytes(snapshot["ls_files"]),
            "git_submodule_status_sha256": sha256_bytes(snapshot["submodule"]),
        }

    @staticmethod
    def _write_manifest(path: Path, entries: Iterable[TrackedEntry | UntrackedEntry]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for entry in sorted(entries, key=lambda item: item.path):
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=True, sort_keys=True) + "\n")

    @staticmethod
    def _write_authority_artifacts(destination: Path, snapshot: Mapping[str, Any]) -> dict[str, str]:
        paths = {
            "git_status": destination / "git_status_porcelain_v1.zlist",
            "git_diff_binary": destination / "git_diff_binary.patch",
            "git_ls_files": destination / "git_ls_files_s.zlist",
            "git_submodule_status": destination / "git_submodule_status.txt",
        }
        paths["git_status"].write_bytes(snapshot["status_raw"])
        paths["git_diff_binary"].write_bytes(snapshot["diff_binary"])
        paths["git_ls_files"].write_bytes(snapshot["ls_files"])
        paths["git_submodule_status"].write_bytes(snapshot["submodule"])
        return {name: str(path) for name, path in paths.items()}

    def capture(self, *, label: str = "H0", require_clean: bool = True) -> dict[str, Any]:
        destination = self.artifact_root / str(label)
        destination.mkdir(parents=True, exist_ok=True)
        before = self._authority_snapshot()
        entries_before = self.tracked_entries()
        untracked_before = self.untracked_entries()
        protected_ignored_before = self.protected_ignored_entries()
        entries = self.tracked_entries()
        untracked = self.untracked_entries()
        protected_ignored = self.protected_ignored_entries()
        authority = self._authority_snapshot()
        protected_ignored_after_authority = self.protected_ignored_entries()
        authority_stable = self._authority_digests(before) == self._authority_digests(authority)
        manifests_stable = bool(
            self.manifest_digest(entries_before) == self.manifest_digest(entries)
            and self.untracked_manifest_digest(untracked_before) == self.untracked_manifest_digest(untracked)
            and self.untracked_manifest_digest(protected_ignored_before)
            == self.untracked_manifest_digest(protected_ignored)
            == self.untracked_manifest_digest(protected_ignored_after_authority)
        )
        protected_ignored = protected_ignored_after_authority
        capture_stable = authority_stable and manifests_stable
        commit = str(authority["commit"])
        branch = str(authority["branch"])
        status_raw = authority["status_raw"]
        diff_binary = authority["diff_binary"]
        ls_files = authority["ls_files"]
        submodule = authority["submodule"]
        status_rows = authority["status_rows"]
        status_policy = self._status_policy(status_rows)
        protected_ignored_policy = self._protected_ignored_policy()
        symlinks = [entry.to_dict() for entry in entries if entry.kind == "symlink"]
        untracked_symlinks = [entry.to_dict() for entry in untracked if entry.kind == "symlink"]
        missing = [entry.path for entry in entries if entry.kind in {"missing", "submodule_missing"}]
        missing_untracked = [entry.path for entry in untracked if entry.kind == "missing"]
        unsupported = [entry.path for entry in entries if entry.kind == "other"]
        unsupported_untracked = [entry.path for entry in untracked if entry.kind == "other"]
        unsafe_protected_ignored = [
            entry.path for entry in protected_ignored if entry.kind in {"other", "symlink"}
        ]
        nonzero_stages = [entry.path for entry in entries if entry.stage != 0]
        submodule_lines = submodule.decode("utf-8", errors="replace").splitlines()
        submodule_dirty = [line for line in submodule_lines if line[:1] in {"-", "+", "U"}]
        submodule_paths = {entry.path for entry in entries if entry.kind == "submodule"}
        submodule_worktree_changes = [
            dict(row)
            for row in status_rows
            if str(row.get("path") or "") in submodule_paths and row.get("status") != "??"
        ]
        status_untracked_paths = {
            str(row.get("path") or "") for row in status_rows if row.get("status") == "??"
        }
        manifest_untracked_paths = {entry.path for entry in untracked}
        untracked_path_consistent = status_untracked_paths == manifest_untracked_paths

        artifacts = self._write_authority_artifacts(destination, authority)
        tracked_manifest_path = destination / "tracked_manifest.jsonl"
        untracked_manifest_path = destination / "untracked_manifest.jsonl"
        protected_ignored_manifest_path = destination / "protected_ignored_manifest.jsonl"
        self._write_manifest(tracked_manifest_path, entries)
        self._write_manifest(untracked_manifest_path, untracked)
        self._write_manifest(protected_ignored_manifest_path, protected_ignored)
        symlinks_path = destination / "symlinks.json"
        atomic_write_json(symlinks_path, {"tracked": symlinks, "untracked": untracked_symlinks})
        artifacts.update({
            "tracked_manifest": str(tracked_manifest_path),
            "untracked_manifest": str(untracked_manifest_path),
            "protected_ignored_manifest": str(protected_ignored_manifest_path),
            "symlinks": str(symlinks_path),
        })

        verified = bool(
            re_full_sha(commit)
            and capture_stable
            and (not status_rows or not require_clean)
            and (not diff_binary or not require_clean)
            and not missing
            and not missing_untracked
            and not unsupported
            and not unsupported_untracked
            and not unsafe_protected_ignored
            and not nonzero_stages
            and not submodule_dirty
            and not submodule_worktree_changes
            and untracked_path_consistent
        )
        result = {
            "schema_version": SOURCE_FREEZE_SCHEMA_VERSION,
            "captured_at": utc_now(),
            "label": str(label),
            "repo_root": str(self.repo_root),
            "commit": commit,
            "branch": branch,
            "verified": verified,
            "require_clean": bool(require_clean),
            "capture_stable": capture_stable,
            "authority_stable": authority_stable,
            "manifests_stable": manifests_stable,
            "status": status_policy,
            "git_status_empty": not status_rows,
            "git_status_sha256": sha256_bytes(status_raw),
            "git_diff_binary_empty": not diff_binary,
            "git_diff_binary_sha256": sha256_bytes(diff_binary),
            "git_ls_files_sha256": sha256_bytes(ls_files),
            "git_submodule_status_sha256": sha256_bytes(submodule),
            "submodule_dirty": submodule_dirty,
            "submodule_worktree_changes": submodule_worktree_changes,
            "tracked_file_count": len(entries),
            "tracked_manifest_digest": self.manifest_digest(entries),
            "tracked_content_digest": self.content_digest(entries),
            "untracked_file_count": len(untracked),
            "untracked_manifest_digest": self.untracked_manifest_digest(untracked),
            "untracked_content_digest": self.untracked_content_digest(untracked),
            "untracked_path_consistent": untracked_path_consistent,
            "protected_ignored_policy": protected_ignored_policy,
            "protected_ignored_file_count": len(protected_ignored),
            "protected_ignored_present_count": sum(entry.kind != "missing" for entry in protected_ignored),
            "protected_ignored_manifest_digest": self.untracked_manifest_digest(protected_ignored),
            "protected_ignored_content_digest": self.untracked_content_digest(protected_ignored),
            "unsafe_protected_ignored_paths": unsafe_protected_ignored,
            "missing_tracked_paths": missing,
            "missing_untracked_paths": missing_untracked,
            "unsupported_tracked_paths": unsupported,
            "unsupported_untracked_paths": unsupported_untracked,
            "nonzero_index_stage_paths": nonzero_stages,
            "symlink_count": len(symlinks),
            "untracked_symlink_count": len(untracked_symlinks),
            "artifact_root": str(destination),
            "artifacts": artifacts,
        }
        if label == "H0" and verified:
            self.baseline = result
            self._baseline_entries = {entry.path: entry for entry in entries}
            self._baseline_untracked_entries = {entry.path: entry for entry in untracked}
            self._baseline_protected_ignored_entries = {
                entry.path: entry for entry in protected_ignored
            }
            self._refresh_baseline_parent_directories()
            self._start_runtime_monitor()
            assert self._runtime_monitor is not None
            monitor_health = self._runtime_monitor.health()
            result["runtime_monitor"] = monitor_health
            result["protected_ignored_observable"] = bool(
                monitor_health.get("protected_ignored_observable")
            )
            if not monitor_health.get("formal_eligible"):
                verified = False
                result["verified"] = False
                self.baseline = None
                self._baseline_entries.clear()
                self._baseline_untracked_entries.clear()
                self._baseline_protected_ignored_entries.clear()
                self._baseline_parent_directories.clear()
                self.close()
        elif self._runtime_monitor is not None:
            monitor_health = self._runtime_monitor.health()
            result["runtime_monitor"] = monitor_health
            result["protected_ignored_observable"] = bool(
                monitor_health.get("protected_ignored_observable")
            )
        else:
            result["protected_ignored_observable"] = None
        atomic_write_json(destination / "source_freeze.json", result)
        if require_clean and not verified:
            raise SourceFreezeError(
                "source freeze verification failed: "
                f"status_rows={len(status_rows)}, blocked={len(status_policy['blocked_changes'])}, "
                f"diff={bool(diff_binary)}, capture_stable={capture_stable}, "
                f"missing={len(missing) + len(missing_untracked)}, "
                f"unsupported={len(unsupported) + len(unsupported_untracked)}, "
                f"unsafe_protected_ignored={len(unsafe_protected_ignored)}, "
                f"protected_ignored_observable={result.get('protected_ignored_observable')}, "
                f"index_stages={len(nonzero_stages)}, "
                f"submodules={len(submodule_dirty) + len(submodule_worktree_changes)}"
            )
        return result

    def load_baseline(self, source_freeze_path: Path) -> dict[str, Any]:
        """Restore an H0 snapshot in a separately launched campaign process."""

        try:
            payload = json.loads(Path(source_freeze_path).read_text(encoding="utf-8"))
        except Exception as exc:
            raise SourceFreezeError(f"cannot load H0 source freeze: {exc.__class__.__name__}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != SOURCE_FREEZE_SCHEMA_VERSION:
            raise SourceFreezeError("H0 source freeze schema is missing or unsupported")
        if payload.get("label") != "H0" or payload.get("verified") is not True:
            raise SourceFreezeError("H0 source freeze was not machine-verified")
        if Path(str(payload.get("repo_root") or "")).resolve() != self.repo_root:
            raise SourceFreezeError("H0 source freeze belongs to a different repository root")
        baseline_allowlist = tuple(str(item) for item in ((payload.get("status") or {}).get("untracked_allowlist") or ()))
        if baseline_allowlist != self.untracked_allowlist:
            raise SourceFreezeError("H0 untracked allowlist does not match campaign policy")
        protected_policy_paths = tuple(
            str(item.get("path") or "")
            for item in ((payload.get("protected_ignored_policy") or {}).get("paths") or ())
            if isinstance(item, Mapping)
        )
        if protected_policy_paths != self.protected_ignored_paths:
            raise SourceFreezeError("H0 protected ignored launcher-input policy does not match reviewed policy")
        expected_dir = Path(source_freeze_path).resolve().parent
        artifact_values = payload.get("artifacts") or {}

        def artifact_path(name: str) -> Path:
            raw = str(artifact_values.get(name) or "")
            try:
                path = Path(raw).resolve(strict=True)
            except Exception as exc:
                raise SourceFreezeError(f"H0 artifact {name} is unavailable: {exc}") from exc
            if path.parent != expected_dir or not path.is_file():
                raise SourceFreezeError(f"H0 artifact {name} escaped its evidence directory")
            return path

        authority_hashes = {
            "git_status": "git_status_sha256",
            "git_diff_binary": "git_diff_binary_sha256",
            "git_ls_files": "git_ls_files_sha256",
            "git_submodule_status": "git_submodule_status_sha256",
        }
        for artifact_name, digest_name in authority_hashes.items():
            path = artifact_path(artifact_name)
            if sha256_file(path) != payload.get(digest_name):
                raise SourceFreezeError(f"H0 artifact {artifact_name} digest mismatch")

        manifest_path = artifact_path("tracked_manifest")
        untracked_manifest_path = artifact_path("untracked_manifest")
        protected_ignored_manifest_path = artifact_path("protected_ignored_manifest")
        try:
            rows: dict[str, TrackedEntry] = {}
            with manifest_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    entry = TrackedEntry(
                        path=str(row["path"]),
                        index_mode=str(row["index_mode"]),
                        index_oid=str(row["index_oid"]),
                        stage=int(row["stage"]),
                        kind=str(row["kind"]),
                        working_sha256=str(row["working_sha256"]),
                        symlink_target=str(row["symlink_target"]),
                        submodule_head=str(row["submodule_head"]),
                        filesystem_mode=int(row["filesystem_mode"]),
                        size=int(row["size"]),
                        mtime_ns=int(row["mtime_ns"]),
                        ctime_ns=int(row["ctime_ns"]),
                        inode=int(row["inode"]),
                        device=int(row["device"]),
                    )
                    if entry.path in rows:
                        raise SourceFreezeError(f"duplicate H0 manifest path: {entry.path}")
                    rows[entry.path] = entry
            untracked_rows: dict[str, UntrackedEntry] = {}
            with untracked_manifest_path.open("r", encoding="utf-8") as handle:
                for untracked_line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    entry = UntrackedEntry(
                        path=str(row["path"]),
                        kind=str(row["kind"]),
                        working_sha256=str(row["working_sha256"]),
                        symlink_target=str(row["symlink_target"]),
                        filesystem_mode=int(row["filesystem_mode"]),
                        size=int(row["size"]),
                        mtime_ns=int(row["mtime_ns"]),
                        ctime_ns=int(row["ctime_ns"]),
                        inode=int(row["inode"]),
                        device=int(row["device"]),
                    )
                    if entry.path in untracked_rows:
                        raise SourceFreezeError(f"duplicate H0 untracked manifest path: {entry.path}")
                    untracked_rows[entry.path] = entry
            protected_ignored_rows: dict[str, UntrackedEntry] = {}
            with protected_ignored_manifest_path.open("r", encoding="utf-8") as handle:
                for protected_line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    entry = UntrackedEntry(
                        path=str(row["path"]),
                        kind=str(row["kind"]),
                        working_sha256=str(row["working_sha256"]),
                        symlink_target=str(row["symlink_target"]),
                        filesystem_mode=int(row["filesystem_mode"]),
                        size=int(row["size"]),
                        mtime_ns=int(row["mtime_ns"]),
                        ctime_ns=int(row["ctime_ns"]),
                        inode=int(row["inode"]),
                        device=int(row["device"]),
                    )
                    if entry.path in protected_ignored_rows:
                        raise SourceFreezeError(f"duplicate H0 protected ignored manifest path: {entry.path}")
                    protected_ignored_rows[entry.path] = entry
        except SourceFreezeError:
            raise
        except Exception as exc:
            raise SourceFreezeError(
                "cannot restore H0 source manifests at tracked line "
                f"{locals().get('line_number', 0)} / untracked line "
                f"{locals().get('untracked_line_number', 0)} / protected line "
                f"{locals().get('protected_line_number', 0)}: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        if not rows or len(rows) != int(payload.get("tracked_file_count") or 0):
            raise SourceFreezeError("H0 tracked manifest count is empty or mismatched")
        if len(untracked_rows) != int(payload.get("untracked_file_count") or 0):
            raise SourceFreezeError("H0 untracked manifest count is mismatched")
        if (
            set(protected_ignored_rows) != set(self.protected_ignored_paths)
            or len(protected_ignored_rows) != int(payload.get("protected_ignored_file_count") or 0)
        ):
            raise SourceFreezeError("H0 protected ignored manifest paths or count are mismatched")
        if self.manifest_digest(rows.values()) != payload.get("tracked_manifest_digest"):
            raise SourceFreezeError("H0 tracked manifest digest mismatch")
        if self.content_digest(rows.values()) != payload.get("tracked_content_digest"):
            raise SourceFreezeError("H0 tracked content digest mismatch")
        if self.untracked_manifest_digest(untracked_rows.values()) != payload.get("untracked_manifest_digest"):
            raise SourceFreezeError("H0 untracked manifest digest mismatch")
        if self.untracked_content_digest(untracked_rows.values()) != payload.get("untracked_content_digest"):
            raise SourceFreezeError("H0 untracked content digest mismatch")
        if (
            self.untracked_manifest_digest(protected_ignored_rows.values())
            != payload.get("protected_ignored_manifest_digest")
        ):
            raise SourceFreezeError("H0 protected ignored manifest digest mismatch")
        if (
            self.untracked_content_digest(protected_ignored_rows.values())
            != payload.get("protected_ignored_content_digest")
        ):
            raise SourceFreezeError("H0 protected ignored content digest mismatch")
        self.baseline = payload
        self._baseline_entries = rows
        self._baseline_untracked_entries = untracked_rows
        self._baseline_protected_ignored_entries = protected_ignored_rows
        self._refresh_baseline_parent_directories()
        self._start_runtime_monitor()
        assert self._runtime_monitor is not None
        if not self._runtime_monitor.health().get("formal_eligible"):
            self.baseline = None
            self._baseline_entries.clear()
            self._baseline_untracked_entries.clear()
            self._baseline_protected_ignored_entries.clear()
            self._baseline_parent_directories.clear()
            self.close()
            raise SourceFreezeError("protected ignored launcher inputs cannot be continuously observed")
        return payload

    @staticmethod
    def _runtime_stat_signature(entry: TrackedEntry | UntrackedEntry) -> tuple[Any, ...]:
        return (
            entry.kind,
            entry.filesystem_mode,
            entry.size,
            entry.mtime_ns,
            entry.ctime_ns,
            entry.inode,
            entry.device,
        )

    def _inspect_frozen_path(
        self,
        relative: str,
        baseline: TrackedEntry | UntrackedEntry,
        *,
        mutation_event_seen: bool = False,
    ) -> dict[str, Any] | None:
        path = self._repo_path(relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return {
                "reason": "missing",
                "expected_sha256": baseline.working_sha256,
                "actual_sha256": "",
                "expected_kind": baseline.kind,
                "actual_kind": "missing",
            }
        if isinstance(baseline, TrackedEntry) and baseline.index_mode == "160000":
            kind = "submodule"
        elif stat.S_ISLNK(info.st_mode):
            kind = "symlink"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            kind = "other"
        current_stat = self._entry_stat(info)
        current_signature = (
            kind,
            current_stat["filesystem_mode"],
            current_stat["size"],
            current_stat["mtime_ns"],
            current_stat["ctime_ns"],
            current_stat["inode"],
            current_stat["device"],
        )
        baseline_signature = self._runtime_stat_signature(baseline)
        if current_signature == baseline_signature and not mutation_event_seen:
            return None
        if kind == "symlink":
            target = os.readlink(path)
            digest = sha256_bytes(target.encode("utf-8", errors="surrogateescape"))
        elif kind == "file":
            target = ""
            digest = sha256_file(path)
        elif kind == "submodule" and isinstance(baseline, TrackedEntry):
            target = ""
            digest = sha256_bytes(
                f"{baseline.index_oid}\0{self._submodule_head(path)}".encode("ascii")
            )
        else:
            target = ""
            digest = ""
        content_changed = bool(
            digest != baseline.working_sha256
            or target != baseline.symlink_target
            or kind != baseline.kind
        )
        return {
            "reason": (
                "content_or_type_changed" if content_changed
                else "metadata_changed" if current_signature != baseline_signature
                else "mutation_event_without_persistent_delta"
            ),
            "mutation_event_seen": bool(mutation_event_seen),
            "expected_sha256": baseline.working_sha256,
            "actual_sha256": digest,
            "expected_kind": baseline.kind,
            "actual_kind": kind,
            "expected_stat": list(baseline_signature),
            "actual_stat": list(current_signature),
        }

    def _inspect_protected_ignored_path(
        self,
        relative: str,
        baseline: UntrackedEntry,
        *,
        mutation_event_seen: bool = False,
    ) -> dict[str, Any] | None:
        path = self._repo_path(relative)
        exists = path.exists() or path.is_symlink()
        if baseline.kind == "missing" and not exists:
            if mutation_event_seen:
                return {
                    "reason": "protected_ignored_mutation_without_persistent_delta",
                    "mutation_event_seen": True,
                    "authority_class": "protected_ignored_launcher_input",
                    "expected_sha256": "",
                    "actual_sha256": "",
                    "expected_kind": "missing",
                    "actual_kind": "missing",
                }
            return None
        evidence = self._inspect_frozen_path(
            relative,
            baseline,
            mutation_event_seen=mutation_event_seen,
        )
        if evidence is not None:
            evidence["authority_class"] = "protected_ignored_launcher_input"
        return evidence

    def _protected_ignored_digests(self) -> tuple[list[UntrackedEntry], str, str]:
        entries = self.protected_ignored_entries()
        return (
            entries,
            self.untracked_manifest_digest(entries),
            self.untracked_content_digest(entries),
        )

    def _runtime_path_is_ignored(self, relative: str) -> bool:
        if relative in self.protected_ignored_paths:
            return False
        for prefix in self._ignored_runtime_prefixes:
            if relative == prefix or relative.startswith(f"{prefix}/"):
                return True
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repo_root), "check-ignore", "-q", "--no-index", "--", relative],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                timeout=30,
                check=False,
            )
        except Exception as exc:
            if self._runtime_monitor is not None:
                self._runtime_monitor._error(
                    f"git check-ignore failed for {relative}: {exc.__class__.__name__}: {exc}"
                )
            return False
        if completed.returncode == 0:
            self._ignored_runtime_prefixes.add(relative)
            return True
        if completed.returncode not in {0, 1} and self._runtime_monitor is not None:
            detail = completed.stderr.decode("utf-8", errors="replace")[:500]
            self._runtime_monitor._error(
                f"git check-ignore returned {completed.returncode} for {relative}: {detail}"
            )
        return False

    def _runtime_event_is_relevant(self, relative: str) -> bool:
        if relative in {"", ".", "@escaped"}:
            return True
        if relative in self._baseline_entries or relative in self._baseline_untracked_entries:
            return True
        if relative in self._baseline_protected_ignored_entries:
            return True
        if relative in self._baseline_parent_directories:
            return True
        return not self._runtime_path_is_ignored(relative)

    def _inspect_new_runtime_path(self, relative: str) -> dict[str, dict[str, Any]]:
        path = self._repo_path(relative)
        evidence: dict[str, dict[str, Any]] = {}
        if not path.exists() and not path.is_symlink():
            evidence[relative] = {
                "reason": "transient_untracked_mutation",
                "actual_kind": "missing_at_collection",
                "actual_sha256": "",
            }
            return evidence
        candidates: list[tuple[str, Path]] = []
        if path.is_dir() and not path.is_symlink():
            for directory, _names, files in os.walk(path, topdown=True, followlinks=False):
                directory_path = Path(directory)
                for name in files:
                    candidate = directory_path / name
                    candidates.append((candidate.relative_to(self.repo_root).as_posix(), candidate))
                for name in list(_names):
                    candidate = directory_path / name
                    if candidate.is_symlink():
                        candidates.append((candidate.relative_to(self.repo_root).as_posix(), candidate))
        else:
            candidates.append((relative, path))
        for candidate_relative, candidate in candidates:
            if candidate_relative in self._baseline_entries or candidate_relative in self._baseline_untracked_entries:
                continue
            if (
                candidate_relative not in self.protected_ignored_paths
                and self._runtime_path_is_ignored(candidate_relative)
            ):
                continue
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                evidence[candidate_relative] = {
                    "reason": "transient_untracked_mutation",
                    "actual_kind": "missing_at_collection",
                    "actual_sha256": "",
                }
                continue
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
                target = os.readlink(candidate)
                digest = sha256_bytes(target.encode("utf-8", errors="surrogateescape"))
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
                target = ""
                digest = sha256_file(candidate)
            else:
                continue
            evidence[candidate_relative] = {
                "reason": "new_untracked_path",
                "actual_kind": kind,
                "actual_sha256": digest,
                "symlink_target": target,
                "actual_stat": list((
                    kind,
                    self._entry_stat(info)["filesystem_mode"],
                    int(info.st_size),
                    int(info.st_mtime_ns),
                    int(info.st_ctime_ns),
                    int(info.st_ino),
                    int(info.st_dev),
                )),
            }
        return evidence

    def _metadata_reconciliation(
        self,
        *,
        full_authority: bool,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, bool],
        Mapping[str, Any] | None,
    ]:
        tracked_changes: dict[str, dict[str, Any]] = {}
        untracked_changes: dict[str, dict[str, Any]] = {}
        protected_ignored_changes: dict[str, dict[str, Any]] = {}
        for relative, baseline in self._baseline_entries.items():
            evidence = self._inspect_frozen_path(relative, baseline)
            if evidence is not None:
                tracked_changes[relative] = evidence
        for relative, baseline in self._baseline_untracked_entries.items():
            evidence = self._inspect_frozen_path(relative, baseline)
            if evidence is not None:
                untracked_changes[relative] = evidence
        for relative, baseline in self._baseline_protected_ignored_entries.items():
            evidence = self._inspect_protected_ignored_path(relative, baseline)
            if evidence is not None:
                protected_ignored_changes[relative] = evidence

        current_untracked_paths = set(self._untracked_paths())
        baseline_untracked_paths = set(self._baseline_untracked_entries)
        for relative in sorted(current_untracked_paths - baseline_untracked_paths):
            if not self._runtime_path_is_ignored(relative):
                untracked_changes.update(self._inspect_new_runtime_path(relative))
        for relative in sorted(baseline_untracked_paths - current_untracked_paths):
            untracked_changes.setdefault(relative, {
                "reason": "missing",
                "expected_sha256": self._baseline_untracked_entries[relative].working_sha256,
                "actual_sha256": "",
            })

        authority: Mapping[str, Any] | None = None
        _protected_entries, protected_manifest_digest, _protected_content_digest = self._protected_ignored_digests()
        comparisons = {
            name: True
            for name in ("commit", "branch", "status", "diff", "ls_files", "submodules", "protected_ignored")
        }
        comparisons["protected_ignored"] = bool(
            protected_manifest_digest == self.baseline.get("protected_ignored_manifest_digest")
        )
        if full_authority:
            authority = self._authority_snapshot()
            digests = self._authority_digests(authority)
            comparisons = {
                "commit": digests["commit"] == self.baseline.get("commit"),
                "branch": digests["branch"] == self.baseline.get("branch"),
                "status": digests["git_status_sha256"] == self.baseline.get("git_status_sha256"),
                "diff": digests["git_diff_binary_sha256"] == self.baseline.get("git_diff_binary_sha256"),
                "ls_files": digests["git_ls_files_sha256"] == self.baseline.get("git_ls_files_sha256"),
                "submodules": (
                    digests["git_submodule_status_sha256"]
                    == self.baseline.get("git_submodule_status_sha256")
                ),
                "protected_ignored": comparisons["protected_ignored"],
            }
        else:
            commit = self._git_text("rev-parse", "HEAD").strip()
            branch = self._git_text("rev-parse", "--abbrev-ref", "HEAD").strip()
            ls_files = self._git_bytes("ls-files", "-s", "-z")
            submodules = self._git_bytes("submodule", "status", "--recursive", timeout=120)
            comparisons.update({
                "commit": commit == self.baseline.get("commit"),
                "branch": branch == self.baseline.get("branch"),
                "ls_files": sha256_bytes(ls_files) == self.baseline.get("git_ls_files_sha256"),
                "submodules": sha256_bytes(submodules) == self.baseline.get("git_submodule_status_sha256"),
            })
        return tracked_changes, untracked_changes, protected_ignored_changes, comparisons, authority

    def _incident_result(
        self,
        *,
        tracked_changes: dict[str, dict[str, Any]],
        untracked_changes: dict[str, dict[str, Any]],
        protected_ignored_changes: dict[str, dict[str, Any]],
        events: list[RuntimeMutationEvent],
        authority_event_seen: bool,
        monitor_failure: bool,
        authority: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if authority is None:
            authority = self._authority_snapshot()
        digests = self._authority_digests(authority)
        protected_entries, protected_manifest_digest, protected_content_digest = self._protected_ignored_digests()
        comparisons = {
            "commit": digests["commit"] == self.baseline.get("commit"),
            "branch": digests["branch"] == self.baseline.get("branch"),
            "status": digests["git_status_sha256"] == self.baseline.get("git_status_sha256"),
            "diff": digests["git_diff_binary_sha256"] == self.baseline.get("git_diff_binary_sha256"),
            "ls_files": digests["git_ls_files_sha256"] == self.baseline.get("git_ls_files_sha256"),
            "submodules": digests["git_submodule_status_sha256"] == self.baseline.get("git_submodule_status_sha256"),
            "protected_ignored": (
                protected_manifest_digest == self.baseline.get("protected_ignored_manifest_digest")
            ),
        }
        status = self._status_policy(authority["status_rows"])
        status_untracked_paths = {
            str(row.get("path") or "")
            for row in authority["status_rows"]
            if row.get("status") == "??"
        }
        current_untracked_paths = set(self._untracked_paths())
        baseline_untracked_paths = set(self._baseline_untracked_entries)
        untracked_path_consistent = current_untracked_paths == status_untracked_paths
        untracked_paths_unchanged = current_untracked_paths == baseline_untracked_paths
        if untracked_changes:
            tracked_changes.setdefault("@untracked", {
                "reason": "untracked_source_drift",
                "paths": sorted(untracked_changes),
            })
        if protected_ignored_changes:
            tracked_changes.setdefault("@protected_ignored_launcher_inputs", {
                "reason": "protected_ignored_launcher_input_drift",
                "paths": sorted(protected_ignored_changes),
            })
        if authority_event_seen or not all(comparisons.values()):
            tracked_changes.setdefault("@git_authority", {
                "reason": "git_authority_mutation",
                "event_seen": authority_event_seen,
                "failed_comparisons": sorted(name for name, passed in comparisons.items() if not passed),
            })
        if monitor_failure:
            tracked_changes.setdefault("@monitor", {
                "reason": "runtime_monitor_not_machine_verified",
            })
        if not tracked_changes and not untracked_changes:
            tracked_changes["@source_freeze"] = {
                "reason": "mutation_event_invalidated_frozen_source",
            }

        incident_id = f"{time.time_ns()}-{os.getpid()}"
        destination = self.artifact_root / "runtime_drift" / "incidents" / incident_id
        destination.mkdir(parents=True, exist_ok=False)
        artifacts = self._write_authority_artifacts(destination, authority)
        changed_evidence_path = destination / "changed_path_evidence.json"
        event_rows = [event.to_dict() for event in events[:1000]]
        atomic_write_json(changed_evidence_path, {
            "schema_version": SOURCE_DRIFT_SCHEMA_VERSION,
            "incident_id": incident_id,
            "tracked_changes": tracked_changes,
            "untracked_changes": untracked_changes,
            "protected_ignored_changes": protected_ignored_changes,
            "events": event_rows,
            "events_truncated": len(events) > len(event_rows),
        })
        artifacts["changed_path_evidence"] = str(changed_evidence_path)
        monitor = self._runtime_monitor.health() if self._runtime_monitor is not None else {
            "mode": "not_started",
            "machine_verified": False,
            "formal_eligible": False,
        }
        result = {
            "schema_version": SOURCE_DRIFT_SCHEMA_VERSION,
            "source_freeze_schema_version": SOURCE_FREEZE_SCHEMA_VERSION,
            "checked_at": utc_now(),
            "verified": False,
            "incident": True,
            "incident_id": incident_id,
            "incident_evidence_preserved": True,
            "authority_stable": not authority_event_seen,
            "authority_event_seen": authority_event_seen,
            "monitor_failure": monitor_failure,
            "monitor": monitor,
            "comparisons": comparisons,
            "status": status,
            **digests,
            "status_unchanged": comparisons["status"],
            "diff_unchanged": comparisons["diff"],
            "ls_files_unchanged": comparisons["ls_files"],
            "submodules_unchanged": comparisons["submodules"],
            "untracked_path_consistent": untracked_path_consistent,
            "untracked_paths_unchanged": untracked_paths_unchanged,
            "protected_ignored_paths": list(self.protected_ignored_paths),
            "protected_ignored_manifest_digest": protected_manifest_digest,
            "protected_ignored_content_digest": protected_content_digest,
            "protected_ignored_present_count": sum(entry.kind != "missing" for entry in protected_entries),
            "protected_ignored_unchanged": comparisons["protected_ignored"],
            "tracked_changes": tracked_changes,
            "untracked_changes": untracked_changes,
            "protected_ignored_changes": protected_ignored_changes,
            "events": event_rows,
            "events_truncated": len(events) > len(event_rows),
            "full_git_authority_captured": True,
            "poll_evidence_mode": "unique_incident",
            "artifacts": artifacts,
            "artifact_root": str(destination),
        }
        atomic_write_json(destination / "drift_check.json", result)
        runtime_root = self.artifact_root / "runtime_drift"
        atomic_write_json(runtime_root / "latest.json", result)
        self._runtime_incident = copy.deepcopy(result)
        return result

    def lightweight_drift_check(self) -> dict[str, Any]:
        if self.baseline is None or not self._baseline_entries:
            raise SourceFreezeError("H0 baseline has not been captured")
        with self._drift_lock:
            if self._runtime_incident is not None:
                return copy.deepcopy(self._runtime_incident)
            if self._runtime_monitor is None:
                self._start_runtime_monitor()
            assert self._runtime_monitor is not None
            monitor = self._runtime_monitor
            events = monitor.drain()
            tracked_changes: dict[str, dict[str, Any]] = {}
            untracked_changes: dict[str, dict[str, Any]] = {}
            protected_ignored_changes: dict[str, dict[str, Any]] = {}
            authority_event_seen = any(event.source == "git" for event in events)
            monitor_failure = any(event.source == "monitor" for event in events)

            relevant_events: list[RuntimeMutationEvent] = []
            for event in events:
                if event.source in {"probe", "git", "monitor"}:
                    if event.source != "probe":
                        relevant_events.append(event)
                    continue
                if not self._runtime_event_is_relevant(event.path):
                    continue
                relevant_events.append(event)
                if event.is_directory and event.mask & (_IN_CREATE | _IN_MOVED_TO):
                    monitor.add_source_tree(self._repo_path(event.path))
                matched = False
                prefix = f"{event.path.rstrip('/')}/"
                direct_tracked = self._baseline_entries.get(event.path)
                direct_untracked = self._baseline_untracked_entries.get(event.path)
                direct_protected_ignored = self._baseline_protected_ignored_entries.get(event.path)
                if direct_tracked is not None:
                    matched = True
                    evidence = self._inspect_frozen_path(event.path, direct_tracked, mutation_event_seen=True)
                    if evidence is not None:
                        tracked_changes[event.path] = evidence
                if direct_untracked is not None:
                    matched = True
                    evidence = self._inspect_frozen_path(event.path, direct_untracked, mutation_event_seen=True)
                    if evidence is not None:
                        untracked_changes[event.path] = evidence
                if direct_protected_ignored is not None:
                    matched = True
                    evidence = self._inspect_protected_ignored_path(
                        event.path,
                        direct_protected_ignored,
                        mutation_event_seen=True,
                    )
                    if evidence is not None:
                        protected_ignored_changes[event.path] = evidence
                if event.is_directory and event.path in self._baseline_parent_directories:
                    for relative, baseline in self._baseline_entries.items():
                        if not relative.startswith(prefix):
                            continue
                        matched = True
                        evidence = self._inspect_frozen_path(relative, baseline, mutation_event_seen=True)
                        if evidence is not None:
                            tracked_changes[relative] = evidence
                    for relative, baseline in self._baseline_untracked_entries.items():
                        if not relative.startswith(prefix):
                            continue
                        matched = True
                        evidence = self._inspect_frozen_path(relative, baseline, mutation_event_seen=True)
                        if evidence is not None:
                            untracked_changes[relative] = evidence
                    for relative, baseline in self._baseline_protected_ignored_entries.items():
                        if not relative.startswith(prefix):
                            continue
                        matched = True
                        evidence = self._inspect_protected_ignored_path(
                            relative,
                            baseline,
                            mutation_event_seen=True,
                        )
                        if evidence is not None:
                            protected_ignored_changes[relative] = evidence
                if not matched and event.path not in {"", ".", "@escaped"}:
                    untracked_changes.update(self._inspect_new_runtime_path(event.path))
                elif event.path in {"", ".", "@escaped"}:
                    tracked_changes["@repository_root"] = {
                        "reason": "repository_root_mutation_event",
                        "path": event.path,
                    }

            authority: Mapping[str, Any] | None = None
            comparisons = {
                name: True
                for name in (
                    "commit",
                    "branch",
                    "status",
                    "diff",
                    "ls_files",
                    "submodules",
                    "protected_ignored",
                )
            }
            if monitor.reconciliation_due():
                full_authority = bool(monitor.first_reconciliation_pending or monitor.mode != "inotify")
                (
                    reconciled_tracked,
                    reconciled_untracked,
                    reconciled_protected_ignored,
                    comparisons,
                    authority,
                ) = self._metadata_reconciliation(full_authority=full_authority)
                tracked_changes.update(reconciled_tracked)
                untracked_changes.update(reconciled_untracked)
                protected_ignored_changes.update(reconciled_protected_ignored)
                monitor.mark_reconciled()
            monitor_health = monitor.health()
            monitor_failure = bool(monitor_failure or not monitor_health.get("machine_verified"))
            authority_changed = bool(authority_event_seen or not all(comparisons.values()))
            if (
                relevant_events
                or tracked_changes
                or untracked_changes
                or protected_ignored_changes
                or authority_changed
                or monitor_failure
            ):
                return self._incident_result(
                    tracked_changes=tracked_changes,
                    untracked_changes=untracked_changes,
                    protected_ignored_changes=protected_ignored_changes,
                    events=relevant_events,
                    authority_event_seen=authority_event_seen,
                    monitor_failure=monitor_failure,
                    authority=authority,
                )

            runtime_root = self.artifact_root / "runtime_drift"
            latest_path = runtime_root / "latest.json"
            result = {
                "schema_version": SOURCE_DRIFT_SCHEMA_VERSION,
                "source_freeze_schema_version": SOURCE_FREEZE_SCHEMA_VERSION,
                "checked_at": utc_now(),
                "verified": True,
                "incident": False,
                "authority_stable": True,
                "authority_event_seen": False,
                "monitor_failure": False,
                "monitor": monitor_health,
                "comparisons": comparisons,
                "status": copy.deepcopy(self.baseline.get("status") or {}),
                "commit": self.baseline.get("commit"),
                "branch": self.baseline.get("branch"),
                "git_status_sha256": self.baseline.get("git_status_sha256"),
                "git_diff_binary_sha256": self.baseline.get("git_diff_binary_sha256"),
                "git_ls_files_sha256": self.baseline.get("git_ls_files_sha256"),
                "git_submodule_status_sha256": self.baseline.get("git_submodule_status_sha256"),
                "status_unchanged": True,
                "diff_unchanged": True,
                "ls_files_unchanged": True,
                "submodules_unchanged": True,
                "untracked_path_consistent": True,
                "untracked_paths_unchanged": True,
                "protected_ignored_paths": list(self.protected_ignored_paths),
                "protected_ignored_manifest_digest": self.baseline.get("protected_ignored_manifest_digest"),
                "protected_ignored_content_digest": self.baseline.get("protected_ignored_content_digest"),
                "protected_ignored_present_count": self.baseline.get("protected_ignored_present_count"),
                "protected_ignored_unchanged": True,
                "tracked_changes": {},
                "untracked_changes": {},
                "protected_ignored_changes": {},
                "events": [],
                "events_truncated": False,
                "full_git_authority_captured": False,
                "poll_evidence_mode": "bounded_latest",
                "artifacts": {"latest": str(latest_path)},
                "artifact_root": str(runtime_root),
            }
            atomic_write_json(latest_path, result)
            return result

    def close(self) -> None:
        """Idempotently release the inotify descriptor owned by this freezer."""

        with self._drift_lock:
            if self._runtime_monitor is not None:
                self._runtime_monitor.close()
                self._runtime_monitor = None

    def __enter__(self) -> "GitSourceFreezer":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def verify_final(self, *, require_clean: bool = True) -> dict[str, Any]:
        if self.baseline is None:
            raise SourceFreezeError("H0 baseline has not been captured")
        runtime_drift_before_h24 = self.lightweight_drift_check()
        final = self.capture(label="H24", require_clean=False)
        runtime_drift_after_h24 = self.lightweight_drift_check()
        comparisons = {
            "runtime_drift_before_h24": bool(runtime_drift_before_h24["verified"]),
            "runtime_drift_after_h24": bool(runtime_drift_after_h24["verified"]),
            "h24_internal_verified": bool(final["verified"]),
            "commit": final["commit"] == self.baseline["commit"],
            "branch": final["branch"] == self.baseline["branch"],
            "capture_stable": bool(final["capture_stable"]),
            "tracked_manifest_digest": final["tracked_manifest_digest"] == self.baseline["tracked_manifest_digest"],
            "tracked_content_digest": final["tracked_content_digest"] == self.baseline["tracked_content_digest"],
            "untracked_manifest_digest": final["untracked_manifest_digest"] == self.baseline["untracked_manifest_digest"],
            "untracked_content_digest": final["untracked_content_digest"] == self.baseline["untracked_content_digest"],
            "untracked_file_count": final["untracked_file_count"] == self.baseline["untracked_file_count"],
            "untracked_path_consistent": bool(final["untracked_path_consistent"]),
            "protected_ignored_manifest_digest": (
                final["protected_ignored_manifest_digest"]
                == self.baseline["protected_ignored_manifest_digest"]
            ),
            "protected_ignored_content_digest": (
                final["protected_ignored_content_digest"]
                == self.baseline["protected_ignored_content_digest"]
            ),
            "protected_ignored_file_count": (
                final["protected_ignored_file_count"]
                == self.baseline["protected_ignored_file_count"]
            ),
            "git_ls_files": final["git_ls_files_sha256"] == self.baseline["git_ls_files_sha256"],
            "submodules": final["git_submodule_status_sha256"] == self.baseline["git_submodule_status_sha256"],
            "status_unchanged": final["git_status_sha256"] == self.baseline["git_status_sha256"],
            "diff_unchanged": final["git_diff_binary_sha256"] == self.baseline["git_diff_binary_sha256"],
        }
        if require_clean:
            comparisons["status_clean"] = bool(final["git_status_empty"])
            comparisons["diff_empty"] = bool(final["git_diff_binary_empty"])
        result = {
            "schema_version": SOURCE_FREEZE_SCHEMA_VERSION,
            "verified": all(comparisons.values()),
            "require_clean": bool(require_clean),
            "comparisons": comparisons,
            "runtime_drift_before_h24": runtime_drift_before_h24,
            "runtime_drift_after_h24": runtime_drift_after_h24,
            "h0": self.baseline,
            "h24": final,
        }
        atomic_write_json(self.artifact_root / "source_freeze_final_verification.json", result)
        self.close()
        return result


def re_full_sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdefABCDEF" for character in value)
