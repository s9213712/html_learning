#!/usr/bin/env python3
"""Race-safe audit DB/log/anchor evidence capture and validation.

The validator is deliberately independent from the 24-hour orchestrator.  It
captures the three audit evidence domains while holding the same dedicated
mutation lock used by :mod:`services.system.audit`, copies SQLite through its
backup API (so committed WAL pages are included), and validates only private
immutable copies.

Secret material is read solely to recompute the HMAC chain.  Neither the
integrity key nor the chain seed is written to the receipt or copied as an
artifact.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import tarfile
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import quote


SCHEMA_VERSION = "hackme.audit-evidence-triad/v1"
SCHEMA_PATH = Path(__file__).with_name("schemas") / "audit_evidence_triad.schema.json"
SEALED_REASON = "formal_evidence_seal"
MODES = frozenset({"online", "sealed"})
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

COPY_CHUNK_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 64 * 1024
DEFAULT_MAX_DB_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_MAX_LOG_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_ANCHOR_BYTES = 512 * 1024 * 1024

ARCHIVE_SCHEMA_VERSION = "hackme.audit-evidence-triad-archive/v1"
ARCHIVE_VALIDATION_SCHEMA_VERSION = (
    "hackme.audit-evidence-triad-archive-validation/v1"
)
ARCHIVE_SCHEMA_FILENAME = "audit_evidence_triad.schema.json"
ARCHIVE_RECEIPT_FILENAME = "receipt.json"
ARCHIVE_BLOCK_SIZE = 512
ARCHIVE_RECORD_SIZE = 10 * 1024
DEFAULT_MAX_RECEIPT_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_SCHEMA_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
DEFAULT_ARCHIVE_FREE_SPACE_RESERVE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_BYTES = (
    DEFAULT_MAX_DB_BYTES
    + DEFAULT_MAX_LOG_BYTES
    + DEFAULT_MAX_ANCHOR_BYTES
    + DEFAULT_MAX_RECEIPT_BYTES
    + DEFAULT_MAX_SCHEMA_BYTES
    + 16 * 1024 * 1024
)

DB_FIELDS = (
    "id",
    "ts",
    "action",
    "ip",
    "user",
    "success",
    "ua",
    "detail",
    "prev_hash",
    "entry_hash",
    "chain_hash",
)
BASE_FIELDS = ("ts", "action", "ip", "user", "success", "ua", "detail")
LOG_FIELDS = frozenset(
    (*BASE_FIELDS, "_audit_id", "_prev_hash", "_entry_hash", "_chain_hash")
)
ANCHOR_FIELDS = frozenset(("ts", "audit_id", "entry_hash", "chain_hash", "reason"))

INVARIANT_NAMES = (
    "safe_paths",
    "mutation_lock_acquired",
    "capture_stable",
    "sqlite_backup_complete",
    "sqlite_quick_check",
    "modern_audit_schema",
    "db_ids_strictly_increasing",
    "db_chain_valid",
    "audit_log_valid",
    "audit_log_db_bijection",
    "anchor_history_valid",
    "anchor_history_references_db",
    "latest_matches_history_tail",
    "mode_anchor_policy",
    "secret_material_excluded_from_receipt",
)

RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "target",
        "mode",
        "captured_at",
        "completed_at",
        "ok",
        "verdict",
        "capture",
        "artifacts",
        "counts",
        "heads",
        "invariants",
        "errors",
        "secret_handling",
    }
)
ARTIFACT_FILENAMES = {
    "database": "audit_snapshot.sqlite3",
    "audit_log": "audit.log",
    "anchor_history": "audit_head.jsonl",
    "anchor_latest": "audit_head_latest.json",
}


class AuditEvidenceError(RuntimeError):
    """A fail-closed capture or local validator failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class AuditEvidencePaths:
    runtime_root: Path
    database: Path
    audit_log: Path
    anchor_history: Path
    anchor_latest: Path
    chain_seed: Path
    integrity_key: Path
    mutation_lock: Path

    @classmethod
    def for_runtime(cls, runtime_root: str | os.PathLike[str]) -> "AuditEvidencePaths":
        root = Path(runtime_root)
        log_path = root / "logs" / "audit.log"
        return cls(
            runtime_root=root,
            database=root / "database" / "audit.db",
            audit_log=log_path,
            anchor_history=root / "anchors" / "audit_head.jsonl",
            anchor_latest=root / "anchors" / "audit_head_latest.json",
            chain_seed=root / ".chain_seed",
            integrity_key=root / ".integrity_key",
            mutation_lock=Path(str(log_path) + ".mutation.lock"),
        )


@dataclass(frozen=True)
class CaptureLimits:
    database_bytes: int = DEFAULT_MAX_DB_BYTES
    audit_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    anchor_history_bytes: int = DEFAULT_MAX_ANCHOR_BYTES
    anchor_latest_bytes: int = 1024 * 1024
    lock_timeout_seconds: float = 10.0
    backup_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class ArchiveLimits:
    """Hard bounds used before any archive member is materialized."""

    archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    database_bytes: int = DEFAULT_MAX_DB_BYTES
    audit_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    anchor_history_bytes: int = DEFAULT_MAX_ANCHOR_BYTES
    anchor_latest_bytes: int = 1024 * 1024
    receipt_bytes: int = DEFAULT_MAX_RECEIPT_BYTES
    schema_bytes: int = DEFAULT_MAX_SCHEMA_BYTES
    jsonl_line_bytes: int = DEFAULT_MAX_JSONL_LINE_BYTES
    free_space_reserve_bytes: int = DEFAULT_ARCHIVE_FREE_SPACE_RESERVE_BYTES
    sqlite_validation_seconds: float = 60.0


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    links: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "_Identity":
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            uid=int(value.st_uid),
            gid=int(value.st_gid),
            links=int(value.st_nlink),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=int(value.st_ctime_ns),
        )


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and HASH_RE.fullmatch(value) is not None


def _entry_hash(entry: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(entry)).encode("utf-8")).hexdigest()


def _chain_hash(integrity_key: bytes, prev_hash: str, entry_hash: str) -> str:
    material = f"{prev_hash}:{entry_hash}".encode("utf-8")
    return hmac.new(integrity_key, material, "sha256").hexdigest()


def _strict_json_loads(text: str, *, label: str) -> dict[str, Any]:
    def object_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key in {label}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant in {label}: {value}")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuditEvidenceError("invalid_json", f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AuditEvidenceError("invalid_json_shape", f"{label} JSON root is not an object")
    return payload


def _parse_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return True


def _absolute_canonical(path: Path, *, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise AuditEvidenceError("unsafe_path", f"{label} must be an absolute path")
    normalized = Path(os.path.abspath(os.fspath(candidate)))
    if normalized != candidate:
        raise AuditEvidenceError("unsafe_path", f"{label} must be canonical")
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise AuditEvidenceError("unsafe_path", f"cannot resolve {label}: {type(exc).__name__}") from exc
    if resolved != candidate:
        raise AuditEvidenceError("unsafe_path", f"{label} contains a symlink")
    return candidate


def _under_root(path: Path, root: Path, *, label: str) -> Path:
    candidate = _absolute_canonical(path, label=label)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AuditEvidenceError("path_escape", f"{label} escapes runtime root") from exc
    return candidate


def _regular_identity(
    path: Path,
    *,
    label: str,
    required: bool,
    maximum_bytes: int | None = None,
) -> _Identity | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise AuditEvidenceError("missing_source", f"{label} does not exist")
        return None
    except OSError as exc:
        raise AuditEvidenceError("unreadable_source", f"cannot inspect {label}: {type(exc).__name__}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise AuditEvidenceError("unsafe_path", f"{label} is not a regular file")
    if int(info.st_nlink) != 1:
        raise AuditEvidenceError("unsafe_path", f"{label} must have exactly one hard link")
    if maximum_bytes is not None and int(info.st_size) > int(maximum_bytes):
        raise AuditEvidenceError("source_oversize", f"{label} exceeds its bounded size")
    return _Identity.from_stat(info)


def _validate_paths(paths: AuditEvidencePaths) -> AuditEvidencePaths:
    root = _absolute_canonical(Path(paths.runtime_root), label="runtime_root")
    try:
        root_info = os.lstat(root)
    except OSError as exc:
        raise AuditEvidenceError("unsafe_path", f"cannot inspect runtime_root: {type(exc).__name__}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise AuditEvidenceError("unsafe_path", "runtime_root is not a directory")
    values: dict[str, Path] = {"runtime_root": root}
    for name in (
        "database",
        "audit_log",
        "anchor_history",
        "anchor_latest",
        "chain_seed",
        "integrity_key",
        "mutation_lock",
    ):
        values[name] = _under_root(Path(getattr(paths, name)), root, label=name)
        parent = values[name].parent
        try:
            parent_info = os.lstat(parent)
        except OSError as exc:
            raise AuditEvidenceError(
                "unsafe_path", f"cannot inspect {name} parent: {type(exc).__name__}"
            ) from exc
        if not stat.S_ISDIR(parent_info.st_mode):
            raise AuditEvidenceError("unsafe_path", f"{name} parent is not a directory")
    return AuditEvidencePaths(**values)


def _open_readonly(path: Path, expected: _Identity, *, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuditEvidenceError("unreadable_source", f"cannot securely open {label}: {type(exc).__name__}") from exc
    opened = _Identity.from_stat(os.fstat(descriptor))
    if opened != expected:
        os.close(descriptor)
        raise AuditEvidenceError("unstable_source", f"{label} changed before secure open")
    return descriptor


def _read_secret(path: Path, *, label: str, text: bool) -> str | bytes:
    expected = _regular_identity(path, label=label, required=True, maximum_bytes=MAX_SECRET_BYTES)
    assert expected is not None
    descriptor = _open_readonly(path, expected, label=label)
    try:
        content = bytearray()
        while True:
            block = os.read(descriptor, min(COPY_CHUNK_BYTES, MAX_SECRET_BYTES + 1 - len(content)))
            if not block:
                break
            content.extend(block)
            if len(content) > MAX_SECRET_BYTES:
                raise AuditEvidenceError("source_oversize", f"{label} exceeds its bounded size")
        if _Identity.from_stat(os.fstat(descriptor)) != expected:
            raise AuditEvidenceError("unstable_source", f"{label} changed while being read")
    finally:
        os.close(descriptor)
    if _regular_identity(path, label=label, required=True, maximum_bytes=MAX_SECRET_BYTES) != expected:
        raise AuditEvidenceError("unstable_source", f"{label} path changed while being read")
    if not content:
        raise AuditEvidenceError("missing_secret", f"{label} is empty")
    if not text:
        return bytes(content)
    try:
        value = bytes(content).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise AuditEvidenceError("invalid_secret", f"{label} is not UTF-8") from exc
    if not value:
        raise AuditEvidenceError("missing_secret", f"{label} is empty")
    return value


@contextmanager
def _mutation_guard(path: Path, *, timeout_seconds: float) -> Iterator[float]:
    existing = _regular_identity(path, label="mutation_lock", required=False)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise AuditEvidenceError("mutation_lock_open_failed", f"cannot open mutation lock: {type(exc).__name__}") from exc
    try:
        opened = _Identity.from_stat(os.fstat(descriptor))
        if not stat.S_ISREG(opened.mode) or opened.links != 1:
            raise AuditEvidenceError("unsafe_path", "mutation lock is not a private regular inode")
        if existing is not None and opened != existing:
            raise AuditEvidenceError("unstable_source", "mutation lock inode changed during open")
        started = time.monotonic()
        deadline = started + max(0.0, float(timeout_seconds))
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AuditEvidenceError("mutation_lock_timeout", "timed out acquiring mutation lock")
                time.sleep(min(0.025, max(0.001, deadline - time.monotonic())))
        wait_ms = (time.monotonic() - started) * 1000.0
        path_identity = _regular_identity(path, label="mutation_lock", required=True)
        if path_identity != opened:
            raise AuditEvidenceError("unstable_source", "mutation lock path changed after acquisition")
        yield wait_ms
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _open_private_output(path: Path) -> int:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        raise AuditEvidenceError("artifact_write_failed", f"cannot create private artifact: {type(exc).__name__}") from exc


def _open_private_output_at(parent_descriptor: int, name: str) -> int:
    if not name or name in {".", ".."} or "/" in name:
        raise AuditEvidenceError("unsafe_path", "output basename is unsafe")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return os.open(name, flags, 0o600, dir_fd=parent_descriptor)
    except OSError as exc:
        raise AuditEvidenceError(
            "artifact_write_failed",
            f"cannot create private artifact: {type(exc).__name__}",
        ) from exc


def _artifact_metadata(path: Path, *, state: str = "present") -> dict[str, Any]:
    if state == "absent":
        return {"state": "absent", "path": None, "size": 0, "sha256": None}
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        while True:
            block = os.read(descriptor, COPY_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
            size += len(block)
    finally:
        os.close(descriptor)
    return {
        "state": "present",
        "path": path.name,
        "size": size,
        "sha256": digest.hexdigest(),
    }


def _read_pinned_regular(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[bytes, _Identity]:
    """Read one bounded regular file while pinning its inode and metadata."""

    expected = _regular_identity(
        path,
        label=label,
        required=True,
        maximum_bytes=maximum_bytes,
    )
    assert expected is not None
    descriptor = _open_readonly(path, expected, label=label)
    content = bytearray()
    try:
        while True:
            remaining = maximum_bytes + 1 - len(content)
            if remaining <= 0:
                raise AuditEvidenceError(
                    "source_oversize", f"{label} exceeds its bounded size"
                )
            block = os.read(descriptor, min(COPY_CHUNK_BYTES, remaining))
            if not block:
                break
            content.extend(block)
        if _Identity.from_stat(os.fstat(descriptor)) != expected:
            raise AuditEvidenceError(
                "unstable_source", f"{label} changed while being read"
            )
    finally:
        os.close(descriptor)
    if (
        _regular_identity(
            path,
            label=label,
            required=True,
            maximum_bytes=maximum_bytes,
        )
        != expected
    ):
        raise AuditEvidenceError(
            "unstable_source", f"{label} path changed while being read"
        )
    return bytes(content), expected


def _hash_pinned_regular(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[dict[str, Any], _Identity]:
    expected = _regular_identity(
        path,
        label=label,
        required=True,
        maximum_bytes=maximum_bytes,
    )
    assert expected is not None
    descriptor = _open_readonly(path, expected, label=label)
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            block = os.read(descriptor, COPY_CHUNK_BYTES)
            if not block:
                break
            size += len(block)
            if size > maximum_bytes:
                raise AuditEvidenceError(
                    "source_oversize", f"{label} exceeds its bounded size"
                )
            digest.update(block)
        if _Identity.from_stat(os.fstat(descriptor)) != expected:
            raise AuditEvidenceError(
                "unstable_source", f"{label} changed while being hashed"
            )
    finally:
        os.close(descriptor)
    if (
        _regular_identity(
            path,
            label=label,
            required=True,
            maximum_bytes=maximum_bytes,
        )
        != expected
    ):
        raise AuditEvidenceError(
            "unstable_source", f"{label} path changed while being hashed"
        )
    return {"size": size, "sha256": digest.hexdigest()}, expected


def _hash_pinned_descriptor(
    descriptor: int,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[dict[str, Any], _Identity]:
    """Hash a caller-pinned descriptor without trusting its pathname."""

    expected = _Identity.from_stat(os.fstat(descriptor))
    if not stat.S_ISREG(expected.mode) or expected.links != 1:
        raise AuditEvidenceError(
            "unsafe_path", f"{label} descriptor is not a single-link regular file"
        )
    if expected.size > maximum_bytes:
        raise AuditEvidenceError("source_oversize", f"{label} exceeds its bounded size")
    digest = hashlib.sha256()
    offset = 0
    while offset < expected.size:
        block = os.pread(
            descriptor,
            min(COPY_CHUNK_BYTES, expected.size - offset),
            offset,
        )
        if not block:
            raise AuditEvidenceError(
                "unstable_source", f"{label} ended while being hashed"
            )
        digest.update(block)
        offset += len(block)
    if _Identity.from_stat(os.fstat(descriptor)) != expected:
        raise AuditEvidenceError(
            "unstable_source", f"{label} descriptor changed while being hashed"
        )
    return {"size": offset, "sha256": digest.hexdigest()}, expected


def _copy_source(
    source: Path,
    destination: Path,
    *,
    label: str,
    maximum_bytes: int,
    required: bool,
) -> tuple[dict[str, Any], _Identity | None]:
    expected = _regular_identity(
        source,
        label=label,
        required=required,
        maximum_bytes=maximum_bytes,
    )
    if expected is None:
        return _artifact_metadata(destination, state="absent"), None
    source_fd = _open_readonly(source, expected, label=label)
    destination_fd = _open_private_output(destination)
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            block = os.read(source_fd, COPY_CHUNK_BYTES)
            if not block:
                break
            size += len(block)
            if size > maximum_bytes:
                raise AuditEvidenceError("source_oversize", f"{label} exceeds its bounded size")
            digest.update(block)
            view = memoryview(block)
            offset = 0
            while offset < len(view):
                written = os.write(destination_fd, view[offset:])
                if written <= 0:
                    raise AuditEvidenceError("artifact_write_failed", "artifact write made no progress")
                offset += written
        os.fsync(destination_fd)
        if _Identity.from_stat(os.fstat(source_fd)) != expected:
            raise AuditEvidenceError("unstable_source", f"{label} changed while copied")
    finally:
        os.close(source_fd)
        os.close(destination_fd)
    if size != expected.size:
        raise AuditEvidenceError("unstable_source", f"{label} changed size while copied")
    if _regular_identity(source, label=label, required=True, maximum_bytes=maximum_bytes) != expected:
        raise AuditEvidenceError("unstable_source", f"{label} path changed while copied")
    return {
        "state": "present",
        "path": destination.name,
        "size": size,
        "sha256": digest.hexdigest(),
    }, expected


def _sqlite_backup(
    database: Path,
    destination: Path,
    *,
    maximum_bytes: int,
    timeout_seconds: float,
) -> tuple[dict[str, Any], _Identity]:
    expected = _regular_identity(
        database,
        label="database",
        required=True,
        maximum_bytes=maximum_bytes,
    )
    assert expected is not None
    output_fd = _open_private_output(destination)
    os.close(output_fd)
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    started = time.monotonic()

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() - started > timeout_seconds:
            raise AuditEvidenceError("sqlite_backup_timeout", "SQLite backup exceeded its deadline")

    try:
        uri = f"file:{quote(os.fspath(database), safe='/')}?mode=ro&nofollow=1"
        source = sqlite3.connect(uri, uri=True, timeout=min(10.0, timeout_seconds))
        source.execute("PRAGMA query_only=ON")
        source.execute(f"PRAGMA busy_timeout={max(1, int(timeout_seconds * 1000))}")
        target = sqlite3.connect(destination)
        source.backup(target, pages=256, progress=progress, sleep=0.025)
        target.commit()
    except AuditEvidenceError:
        raise
    except sqlite3.Error as exc:
        raise AuditEvidenceError("sqlite_backup_failed", f"SQLite backup failed: {type(exc).__name__}") from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
    os.chmod(destination, 0o600)
    if _regular_identity(database, label="database", required=True, maximum_bytes=maximum_bytes) != expected:
        raise AuditEvidenceError("unstable_source", "database path changed during SQLite backup")
    return _artifact_metadata(destination), expected


def _read_live_head(database: Path) -> dict[str, Any] | None:
    try:
        uri = f"file:{quote(os.fspath(database), safe='/')}?mode=ro&nofollow=1"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(secure_audit)").fetchall()
            }
            if not {"id", "entry_hash", "chain_hash"}.issubset(columns):
                return None
            row = connection.execute(
                "SELECT id, entry_hash, chain_hash FROM secure_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if row is None:
        return {}
    return {
        "audit_id": int(row["id"]),
        "entry_hash": str(row["entry_hash"] or ""),
        "chain_hash": str(row["chain_hash"] or ""),
    }


def _force_head_anchor(paths: AuditEvidencePaths) -> dict[str, Any]:
    head = _read_live_head(paths.database)
    if head is None:
        return {"attempted": True, "performed": False, "reason": "modern_schema_unavailable"}
    if not head:
        return {"attempted": True, "performed": False, "reason": "empty_chain"}
    if not _is_hash(head.get("entry_hash")) or not _is_hash(head.get("chain_hash")):
        return {
            "attempted": True,
            "performed": False,
            "reason": "invalid_db_head",
            "audit_id": int(head["audit_id"]),
        }
    payload = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "audit_id": head["audit_id"],
        "entry_hash": head["entry_hash"],
        "chain_hash": head["chain_hash"],
        "reason": SEALED_REASON,
    }
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    history_existing = _regular_identity(
        paths.anchor_history,
        label="anchor_history",
        required=False,
        maximum_bytes=DEFAULT_MAX_ANCHOR_BYTES,
    )
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        history_fd = os.open(paths.anchor_history, flags, 0o600)
    except OSError as exc:
        raise AuditEvidenceError("anchor_write_failed", f"cannot open anchor history: {type(exc).__name__}") from exc
    try:
        opened = _Identity.from_stat(os.fstat(history_fd))
        if not stat.S_ISREG(opened.mode) or opened.links != 1:
            raise AuditEvidenceError("unsafe_path", "anchor history is not a private regular inode")
        if history_existing is not None and opened != history_existing:
            raise AuditEvidenceError("unstable_source", "anchor history changed before seal")
        fcntl.flock(history_fd, fcntl.LOCK_EX)
        view = memoryview(encoded)
        offset = 0
        while offset < len(view):
            written = os.write(history_fd, view[offset:])
            if written <= 0:
                raise AuditEvidenceError("anchor_write_failed", "anchor append made no progress")
            offset += written
        os.fsync(history_fd)

        _regular_identity(
            paths.anchor_latest,
            label="anchor_latest",
            required=False,
            maximum_bytes=1024 * 1024,
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix="audit_head_latest.",
            suffix=".tmp",
            dir=paths.anchor_latest.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, paths.anchor_latest)
            directory_fd = os.open(paths.anchor_latest.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    finally:
        try:
            fcntl.flock(history_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(history_fd)
    return {
        "attempted": True,
        "performed": True,
        "reason": SEALED_REASON,
        "audit_id": int(head["audit_id"]),
        "entry_hash": str(head["entry_hash"]),
        "chain_hash": str(head["chain_hash"]),
    }


def _read_artifact(path: Path, metadata: Mapping[str, Any], *, label: str) -> bytes | None:
    if metadata.get("state") == "absent":
        return None
    expected_size = int(metadata.get("size") or 0)
    expected_digest = str(metadata.get("sha256") or "")
    info = _regular_identity(path, label=label, required=True, maximum_bytes=expected_size)
    assert info is not None
    if info.size != expected_size:
        raise AuditEvidenceError("artifact_unstable", f"{label} artifact size mismatch")
    descriptor = _open_readonly(path, info, label=label)
    digest = hashlib.sha256()
    content = bytearray()
    try:
        while True:
            block = os.read(descriptor, COPY_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
            content.extend(block)
    finally:
        os.close(descriptor)
    if digest.hexdigest() != expected_digest:
        raise AuditEvidenceError("artifact_unstable", f"{label} artifact digest mismatch")
    return bytes(content)


def _append_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    domain: str,
    message: str,
    audit_id: int | None = None,
) -> None:
    error: dict[str, Any] = {"code": code, "domain": domain, "message": message}
    if audit_id is not None:
        error["audit_id"] = int(audit_id)
    errors.append(error)


def _read_db_snapshot(
    path: Path,
    *,
    chain_seed: str,
    integrity_key: bytes,
    invariants: dict[str, bool],
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    rows: list[dict[str, Any]] = []
    head: dict[str, Any] | None = None
    try:
        connection = sqlite3.connect(f"file:{quote(os.fspath(path), safe='/')}?mode=ro&nofollow=1", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        _append_error(errors, code="snapshot_open_failed", domain="database", message=type(exc).__name__)
        return rows, head
    try:
        quick = connection.execute("PRAGMA quick_check").fetchall()
        quick_ok = bool(quick) and all(str(row[0]).lower() == "ok" for row in quick)
        invariants["sqlite_quick_check"] = quick_ok
        if not quick_ok:
            _append_error(errors, code="sqlite_quick_check_failed", domain="database", message="SQLite quick_check did not return ok")
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(secure_audit)").fetchall()
        }
        modern = set(DB_FIELDS).issubset(columns)
        invariants["modern_audit_schema"] = modern
        if not modern:
            _append_error(errors, code="legacy_or_missing_schema", domain="database", message="secure_audit modern columns are missing")
            return rows, head
        fetched = connection.execute(
            f"SELECT {','.join(DB_FIELDS)} FROM secure_audit ORDER BY id ASC"
        ).fetchall()
        rows = [{field: row[field] for field in DB_FIELDS} for row in fetched]
    except sqlite3.Error as exc:
        _append_error(errors, code="snapshot_query_failed", domain="database", message=type(exc).__name__)
        return [], None
    finally:
        connection.close()

    ids_ok = True
    chain_ok = True
    previous_id: int | None = None
    previous_hash = chain_seed
    for row in rows:
        audit_id = row["id"]
        if not _is_int(audit_id) or int(audit_id) <= 0 or (
            previous_id is not None and int(audit_id) <= previous_id
        ):
            ids_ok = False
            _append_error(errors, code="db_id_order", domain="database", message="audit IDs are not unique and strictly increasing")
            break
        previous_id = int(audit_id)
        if not _is_int(row["success"]) or int(row["success"]) not in (0, 1):
            chain_ok = False
            _append_error(errors, code="db_success_type", domain="database", message="success is not 0 or 1", audit_id=previous_id)
            continue
        if not _is_hash(row["entry_hash"]) or not _is_hash(row["chain_hash"]):
            chain_ok = False
            _append_error(errors, code="db_hash_shape", domain="database", message="entry or chain hash is not lowercase SHA-256", audit_id=previous_id)
            continue
        if not isinstance(row["prev_hash"], str) or row["prev_hash"] != previous_hash:
            chain_ok = False
            _append_error(errors, code="db_prev_hash_mismatch", domain="database", message="prev_hash does not reference the expected predecessor", audit_id=previous_id)
        base = {
            "ts": row["ts"],
            "action": row["action"],
            "ip": row["ip"],
            "user": row["user"],
            "success": bool(row["success"]),
            "ua": row["ua"],
            "detail": row["detail"],
        }
        try:
            computed_entry = _entry_hash(base)
        except (TypeError, ValueError):
            chain_ok = False
            _append_error(
                errors,
                code="db_entry_shape",
                domain="database",
                message="DB row cannot be represented by the canonical audit JSON contract",
                audit_id=previous_id,
            )
            previous_hash = str(row["chain_hash"])
            continue
        if not hmac.compare_digest(computed_entry, str(row["entry_hash"])):
            chain_ok = False
            _append_error(errors, code="db_entry_hash_mismatch", domain="database", message="stored entry_hash does not match canonical row content", audit_id=previous_id)
        computed_chain = _chain_hash(integrity_key, previous_hash, computed_entry)
        if not hmac.compare_digest(computed_chain, str(row["chain_hash"])):
            chain_ok = False
            _append_error(errors, code="db_chain_hash_mismatch", domain="database", message="stored chain_hash does not match HMAC chain", audit_id=previous_id)
        previous_hash = str(row["chain_hash"])
    invariants["db_ids_strictly_increasing"] = ids_ok
    invariants["db_chain_valid"] = chain_ok and ids_ok
    if rows:
        last = rows[-1]
        head = {
            "audit_id": int(last["id"]),
            "entry_hash": str(last["entry_hash"]),
            "chain_hash": str(last["chain_hash"]),
        }
    return rows, head


def _decode_jsonl(content: bytes | None, *, label: str) -> list[dict[str, Any]]:
    if content is None:
        return []
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuditEvidenceError("invalid_utf8", f"{label} is not UTF-8") from exc
    values: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        values.append(_strict_json_loads(raw_line, label=f"{label} line {line_number}"))
    return values


def _decode_jsonl_file(
    path: Path,
    *,
    label: str,
    maximum_line_bytes: int,
) -> list[dict[str, Any]]:
    """Parse bounded JSONL lines without loading an entire archive member."""

    values: list[dict[str, Any]] = []
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise AuditEvidenceError(
            "unreadable_source", f"{label} cannot be opened: {type(exc).__name__}"
        ) from exc
    with handle:
        line_number = 0
        while True:
            raw_line = handle.readline(maximum_line_bytes + 1)
            if not raw_line:
                break
            line_number += 1
            if len(raw_line) > maximum_line_bytes:
                raise AuditEvidenceError(
                    "jsonl_line_oversize", f"{label} line exceeds its hard bound"
                )
            if not raw_line.strip():
                continue
            try:
                text = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AuditEvidenceError(
                    "invalid_utf8", f"{label} is not UTF-8"
                ) from exc
            values.append(
                _strict_json_loads(text, label=f"{label} line {line_number}")
            )
    return values


def _validate_log(
    content: bytes | None,
    db_rows: list[dict[str, Any]],
    *,
    chain_seed: str,
    integrity_key: bytes,
    invariants: dict[str, bool],
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    try:
        entries = _decode_jsonl(content, label="audit_log")
    except AuditEvidenceError as exc:
        _append_error(errors, code=exc.code, domain="audit_log", message=str(exc))
        return [], None
    valid = True
    ordered = True
    seen: set[int] = set()
    previous_id: int | None = None
    previous_hash = chain_seed
    by_db_id = {int(row["id"]): row for row in db_rows if _is_int(row.get("id"))}
    for entry in entries:
        if set(entry) != LOG_FIELDS:
            valid = False
            _append_error(errors, code="log_shape", domain="audit_log", message="audit log entry field set mismatch")
            continue
        audit_id = entry.get("_audit_id")
        if not _is_int(audit_id) or int(audit_id) <= 0:
            valid = False
            _append_error(errors, code="log_id_type", domain="audit_log", message="audit log ID is not a positive integer")
            continue
        audit_id = int(audit_id)
        if audit_id in seen or (previous_id is not None and audit_id <= previous_id):
            ordered = False
            valid = False
            _append_error(errors, code="log_id_order", domain="audit_log", message="audit log IDs are duplicated or reordered", audit_id=audit_id)
        seen.add(audit_id)
        previous_id = audit_id
        if not isinstance(entry.get("success"), bool):
            valid = False
            _append_error(errors, code="log_success_type", domain="audit_log", message="audit log success must be JSON boolean", audit_id=audit_id)
        base = {field: entry.get(field) for field in BASE_FIELDS}
        computed_entry = _entry_hash(base)
        expected_prev = previous_hash
        computed_chain = _chain_hash(integrity_key, expected_prev, computed_entry)
        if (
            entry.get("_prev_hash") != expected_prev
            or not _is_hash(entry.get("_entry_hash"))
            or not hmac.compare_digest(str(entry.get("_entry_hash")), computed_entry)
            or not _is_hash(entry.get("_chain_hash"))
            or not hmac.compare_digest(str(entry.get("_chain_hash")), computed_chain)
        ):
            valid = False
            _append_error(errors, code="log_chain_mismatch", domain="audit_log", message="audit log chain metadata does not match canonical content", audit_id=audit_id)
        previous_hash = str(entry.get("_chain_hash") or "")
        db_row = by_db_id.get(audit_id)
        if db_row is None:
            valid = False
            continue
        expected_base = {
            "ts": db_row["ts"],
            "action": db_row["action"],
            "ip": db_row["ip"],
            "user": db_row["user"],
            "success": bool(db_row["success"]),
            "ua": db_row["ua"],
            "detail": db_row["detail"],
        }
        if base != expected_base or (
            entry.get("_prev_hash") != db_row["prev_hash"]
            or entry.get("_entry_hash") != db_row["entry_hash"]
            or entry.get("_chain_hash") != db_row["chain_hash"]
        ):
            valid = False
            _append_error(errors, code="log_db_mismatch", domain="cross_source", message="audit log entry differs from its DB row", audit_id=audit_id)
    db_ids = [int(row["id"]) for row in db_rows if _is_int(row.get("id"))]
    log_ids = [int(entry["_audit_id"]) for entry in entries if _is_int(entry.get("_audit_id"))]
    bijection = valid and ordered and log_ids == db_ids and len(log_ids) == len(db_ids)
    if log_ids != db_ids or len(log_ids) != len(db_ids):
        _append_error(errors, code="log_db_id_set_mismatch", domain="cross_source", message="audit log and DB ID sequences are not one-to-one")
    invariants["audit_log_valid"] = valid and ordered
    invariants["audit_log_db_bijection"] = bijection
    head = None
    if entries and _is_int(entries[-1].get("_audit_id")):
        head = {
            "audit_id": int(entries[-1]["_audit_id"]),
            "entry_hash": str(entries[-1].get("_entry_hash") or ""),
            "chain_hash": str(entries[-1].get("_chain_hash") or ""),
        }
    return entries, head


def _validate_anchors(
    history_content: bytes | None,
    latest_content: bytes | None,
    db_rows: list[dict[str, Any]],
    db_head: dict[str, Any] | None,
    *,
    mode: str,
    invariants: dict[str, bool],
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int]:
    try:
        history = _decode_jsonl(history_content, label="anchor_history")
    except AuditEvidenceError as exc:
        _append_error(errors, code=exc.code, domain="anchors", message=str(exc))
        history = []
        history_parse_ok = False
    else:
        history_parse_ok = True
    if db_rows and not history:
        history_parse_ok = False
        _append_error(
            errors,
            code="anchor_history_missing",
            domain="anchors",
            message="non-empty audit chain has no anchor history",
        )
    latest: dict[str, Any] | None = None
    latest_parse_ok = True
    if latest_content is not None:
        try:
            latest = _strict_json_loads(latest_content.decode("utf-8"), label="anchor_latest")
        except (UnicodeDecodeError, AuditEvidenceError) as exc:
            latest_parse_ok = False
            code = exc.code if isinstance(exc, AuditEvidenceError) else "invalid_utf8"
            _append_error(errors, code=code, domain="anchors", message="anchor latest is unreadable")

    history_valid = history_parse_ok
    references_valid = history_parse_ok
    previous_id: int | None = None
    hashes_by_id: dict[int, tuple[str, str]] = {}
    db_by_id = {int(row["id"]): row for row in db_rows if _is_int(row.get("id"))}
    for anchor in history:
        if set(anchor) != ANCHOR_FIELDS:
            history_valid = False
            _append_error(errors, code="anchor_shape", domain="anchors", message="anchor history field set mismatch")
            continue
        audit_id = anchor.get("audit_id")
        if not _is_int(audit_id) or int(audit_id) <= 0:
            history_valid = False
            _append_error(errors, code="anchor_id_type", domain="anchors", message="anchor ID is not a positive integer")
            continue
        audit_id = int(audit_id)
        if previous_id is not None and audit_id < previous_id:
            history_valid = False
            _append_error(errors, code="anchor_id_order", domain="anchors", message="anchor IDs move backwards", audit_id=audit_id)
        previous_id = audit_id
        if (
            not _parse_timestamp(anchor.get("ts"))
            or not isinstance(anchor.get("reason"), str)
            or not str(anchor.get("reason")).strip()
            or not _is_hash(anchor.get("entry_hash"))
            or not _is_hash(anchor.get("chain_hash"))
        ):
            history_valid = False
            _append_error(errors, code="anchor_value_shape", domain="anchors", message="anchor timestamp, reason, or hashes are invalid", audit_id=audit_id)
        hash_pair = (str(anchor.get("entry_hash") or ""), str(anchor.get("chain_hash") or ""))
        if audit_id in hashes_by_id and hashes_by_id[audit_id] != hash_pair:
            history_valid = False
            _append_error(errors, code="anchor_conflicting_duplicate", domain="anchors", message="same audit ID has conflicting anchor hashes", audit_id=audit_id)
        hashes_by_id[audit_id] = hash_pair
        db_row = db_by_id.get(audit_id)
        if db_row is None or db_row["entry_hash"] != hash_pair[0] or db_row["chain_hash"] != hash_pair[1]:
            references_valid = False
            _append_error(errors, code="anchor_db_mismatch", domain="cross_source", message="anchor does not reference the exact DB row", audit_id=audit_id)

    latest_tail_ok = latest_parse_ok
    latest_reference_ok = latest_parse_ok
    if db_rows:
        if latest is None:
            latest_tail_ok = False
            latest_reference_ok = False
            _append_error(errors, code="latest_missing", domain="anchors", message="non-empty audit chain has no latest anchor")
        else:
            if set(latest) != ANCHOR_FIELDS:
                latest_tail_ok = False
                latest_reference_ok = False
                _append_error(errors, code="latest_shape", domain="anchors", message="latest anchor field set mismatch")
            if (
                not _parse_timestamp(latest.get("ts"))
                or not isinstance(latest.get("reason"), str)
                or not str(latest.get("reason")).strip()
                or not _is_hash(latest.get("entry_hash"))
                or not _is_hash(latest.get("chain_hash"))
            ):
                latest_tail_ok = False
                latest_reference_ok = False
                _append_error(
                    errors,
                    code="latest_value_shape",
                    domain="anchors",
                    message="latest anchor timestamp, reason, or hashes are invalid",
                )
            audit_id = latest.get("audit_id")
            if not _is_int(audit_id) or int(audit_id) <= 0:
                latest_reference_ok = False
                _append_error(errors, code="latest_id_type", domain="anchors", message="latest anchor ID is invalid")
            else:
                audit_id = int(audit_id)
                db_row = db_by_id.get(audit_id)
                if db_row is None or db_row["entry_hash"] != latest.get("entry_hash") or db_row["chain_hash"] != latest.get("chain_hash"):
                    latest_reference_ok = False
                    _append_error(errors, code="latest_db_mismatch", domain="cross_source", message="latest anchor does not reference the exact DB row", audit_id=audit_id)
            if not history or history[-1] != latest:
                latest_tail_ok = False
                _append_error(errors, code="latest_history_mismatch", domain="anchors", message="latest anchor is not the history tail")
    else:
        if history:
            history_valid = False
            references_valid = False
            _append_error(errors, code="anchors_for_empty_db", domain="anchors", message="empty DB has anchor history")
        if latest is not None or latest_content is not None:
            latest_tail_ok = False
            latest_reference_ok = False
            _append_error(errors, code="latest_for_empty_db", domain="anchors", message="empty DB has a latest anchor")

    rows_after_latest = 0
    if latest is not None and _is_int(latest.get("audit_id")):
        latest_id = int(latest["audit_id"])
        rows_after_latest = sum(1 for row in db_rows if int(row["id"]) > latest_id)
    mode_ok = latest_reference_ok
    if mode == "sealed" and db_head is not None:
        mode_ok = bool(
            latest is not None
            and latest.get("audit_id") == db_head["audit_id"]
            and latest.get("entry_hash") == db_head["entry_hash"]
            and latest.get("chain_hash") == db_head["chain_hash"]
            and latest.get("reason") == SEALED_REASON
            and rows_after_latest == 0
        )
        if not mode_ok:
            _append_error(errors, code="sealed_head_mismatch", domain="anchors", message="sealed latest anchor does not equal the DB head")
    elif mode == "sealed" and db_head is None:
        mode_ok = not db_rows and not history and latest is None and latest_content is None

    invariants["anchor_history_valid"] = history_valid
    invariants["anchor_history_references_db"] = references_valid
    invariants["latest_matches_history_tail"] = latest_tail_ok
    invariants["mode_anchor_policy"] = mode_ok
    return history, latest, rows_after_latest


def _receipt_anchor_head(value: object) -> dict[str, Any] | None:
    """Return only schema-safe fields from an untrusted latest anchor."""

    if not isinstance(value, dict) or set(value) != ANCHOR_FIELDS:
        return None
    if (
        not isinstance(value.get("ts"), str)
        or not _is_int(value.get("audit_id"))
        or int(value["audit_id"]) <= 0
        or not isinstance(value.get("entry_hash"), str)
        or not isinstance(value.get("chain_hash"), str)
        or not isinstance(value.get("reason"), str)
    ):
        return None
    return {field: value[field] for field in ("ts", "audit_id", "entry_hash", "chain_hash", "reason")}


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (canonical_json(dict(payload)) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = _open_private_output(temporary)
    try:
        view = memoryview(encoded)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise AuditEvidenceError("artifact_write_failed", "receipt write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_audit_evidence_receipt(
    receipt: Mapping[str, Any] | object,
    *,
    required_mode: str,
    required_target: str,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Independently derive whether a triad receipt satisfies the v1 contract.

    This function never accepts the producer's top-level ``ok`` alone.  It
    verifies the complete invariant inventory, counts and heads, mode-specific
    anchor policy, artifact declarations, secret-handling declaration, and,
    when ``artifact_root`` is supplied, every captured file's size and digest.
    """

    errors: list[str] = []

    def require(condition: object, code: str) -> None:
        if not condition:
            errors.append(code)

    selected_mode = str(required_mode).strip().lower()
    require(selected_mode in MODES, "required_mode_invalid")
    require(
        isinstance(receipt, Mapping),
        "receipt_not_object",
    )
    if not isinstance(receipt, Mapping):
        return {
            "schema_version": "hackme.audit-evidence-triad-validation/v1",
            "ok": False,
            "classification": "FAIL_HARNESS",
            "errors": errors,
        }
    value = dict(receipt)
    require(set(value) == RECEIPT_FIELDS, "receipt_field_set_mismatch")
    require(value.get("schema_version") == SCHEMA_VERSION, "schema_version_mismatch")
    require(value.get("target") == required_target, "target_mismatch")
    require(value.get("mode") == selected_mode, "mode_mismatch")
    require(_parse_timestamp(value.get("captured_at")), "captured_at_invalid")
    require(_parse_timestamp(value.get("completed_at")), "completed_at_invalid")
    require(value.get("ok") is True, "receipt_not_pass")
    require(value.get("verdict") == "PASS", "receipt_verdict_not_pass")
    require(value.get("errors") == [], "receipt_errors_not_empty")

    invariants = value.get("invariants")
    require(isinstance(invariants, Mapping), "invariants_not_object")
    if isinstance(invariants, Mapping):
        require(set(invariants) == set(INVARIANT_NAMES), "invariant_set_mismatch")
        require(
            all(invariants.get(name) is True for name in INVARIANT_NAMES),
            "invariant_not_true",
        )

    capture = value.get("capture")
    require(isinstance(capture, Mapping), "capture_not_object")
    head_anchor: Mapping[str, Any] = {}
    if isinstance(capture, Mapping):
        require(capture.get("sqlite_backup_api") is True, "sqlite_backup_api_not_proven")
        require(capture.get("immutable_validation") is True, "immutable_validation_not_proven")
        wait_ms = capture.get("mutation_lock_wait_ms")
        require(
            isinstance(wait_ms, (int, float))
            and not isinstance(wait_ms, bool)
            and float(wait_ms) >= 0,
            "mutation_lock_wait_invalid",
        )
        candidate = capture.get("head_anchor")
        require(isinstance(candidate, Mapping), "head_anchor_not_object")
        if isinstance(candidate, Mapping):
            head_anchor = candidate

    counts = value.get("counts")
    required_counts = {
        "db_rows",
        "log_entries",
        "anchor_history_entries",
        "rows_after_latest",
    }
    require(isinstance(counts, Mapping), "counts_not_object")
    normalized_counts: dict[str, int] = {}
    if isinstance(counts, Mapping):
        require(set(counts) == required_counts, "count_field_set_mismatch")
        for name in required_counts:
            raw = counts.get(name)
            require(_is_int(raw) and int(raw) >= 0, f"count_invalid:{name}")
            normalized_counts[name] = int(raw) if _is_int(raw) else -1
        require(
            normalized_counts.get("db_rows") == normalized_counts.get("log_entries"),
            "db_log_count_mismatch",
        )

    heads = value.get("heads")
    require(isinstance(heads, Mapping), "heads_not_object")
    database_head: Mapping[str, Any] | None = None
    latest_head: Mapping[str, Any] | None = None
    if isinstance(heads, Mapping):
        require(
            set(heads) == {"database", "audit_log", "anchor_latest"},
            "head_field_set_mismatch",
        )
        database_candidate = heads.get("database")
        log_candidate = heads.get("audit_log")
        latest_candidate = heads.get("anchor_latest")
        require(database_candidate == log_candidate, "db_log_head_mismatch")
        if isinstance(database_candidate, Mapping):
            database_head = database_candidate
            require(
                set(database_candidate) == {"audit_id", "entry_hash", "chain_hash"},
                "database_head_shape_mismatch",
            )
            require(
                _is_int(database_candidate.get("audit_id"))
                and int(database_candidate["audit_id"]) > 0
                and _is_hash(database_candidate.get("entry_hash"))
                and _is_hash(database_candidate.get("chain_hash")),
                "database_head_value_invalid",
            )
        else:
            require(database_candidate is None, "database_head_invalid")
        if isinstance(latest_candidate, Mapping):
            latest_head = latest_candidate
            require(set(latest_candidate) == ANCHOR_FIELDS, "latest_head_shape_mismatch")
            require(
                _parse_timestamp(latest_candidate.get("ts"))
                and _is_int(latest_candidate.get("audit_id"))
                and int(latest_candidate["audit_id"]) > 0
                and _is_hash(latest_candidate.get("entry_hash"))
                and _is_hash(latest_candidate.get("chain_hash"))
                and isinstance(latest_candidate.get("reason"), str)
                and bool(str(latest_candidate.get("reason")).strip()),
                "latest_head_value_invalid",
            )
        else:
            require(latest_candidate is None, "latest_head_invalid")

    db_rows = normalized_counts.get("db_rows", -1)
    history_rows = normalized_counts.get("anchor_history_entries", -1)
    rows_after_latest = normalized_counts.get("rows_after_latest", -1)
    if db_rows == 0:
        require(database_head is None, "empty_db_has_head")
        require(latest_head is None, "empty_db_has_latest")
        require(history_rows == 0 and rows_after_latest == 0, "empty_db_anchor_counts_invalid")
    elif db_rows > 0:
        require(database_head is not None, "nonempty_db_head_missing")
        require(latest_head is not None, "nonempty_db_latest_missing")
        require(history_rows >= 1, "nonempty_db_history_missing")
        require(0 <= rows_after_latest < db_rows, "rows_after_latest_invalid")
        if database_head is not None and latest_head is not None:
            require(
                int(latest_head.get("audit_id") or 0)
                <= int(database_head.get("audit_id") or 0),
                "latest_ahead_of_database",
            )

    if selected_mode == "online":
        require(head_anchor.get("attempted") is False, "online_anchor_attempted")
        require(head_anchor.get("performed") is False, "online_anchor_performed")
    elif selected_mode == "sealed":
        require(head_anchor.get("attempted") is True, "sealed_anchor_not_attempted")
        if db_rows == 0:
            require(head_anchor.get("performed") is False, "empty_sealed_anchor_performed")
            require(head_anchor.get("reason") == "empty_chain", "empty_sealed_reason_invalid")
        elif db_rows > 0:
            require(head_anchor.get("performed") is True, "sealed_anchor_not_performed")
            require(head_anchor.get("reason") == SEALED_REASON, "sealed_reason_invalid")
            require(rows_after_latest == 0, "sealed_rows_after_latest_nonzero")
            if database_head is not None and latest_head is not None:
                require(
                    latest_head.get("audit_id") == database_head.get("audit_id")
                    and latest_head.get("entry_hash") == database_head.get("entry_hash")
                    and latest_head.get("chain_hash") == database_head.get("chain_hash")
                    and latest_head.get("reason") == SEALED_REASON,
                    "sealed_latest_not_database_head",
                )
                require(
                    head_anchor.get("audit_id") == database_head.get("audit_id")
                    and head_anchor.get("entry_hash") == database_head.get("entry_hash")
                    and head_anchor.get("chain_hash") == database_head.get("chain_hash"),
                    "sealed_capture_head_mismatch",
                )

    artifacts = value.get("artifacts")
    require(isinstance(artifacts, Mapping), "artifacts_not_object")
    artifact_maximums = {
        "database": DEFAULT_MAX_DB_BYTES,
        "audit_log": DEFAULT_MAX_LOG_BYTES,
        "anchor_history": DEFAULT_MAX_ANCHOR_BYTES,
        "anchor_latest": 1024 * 1024,
    }
    verified_artifact_root: Path | None = None
    if artifact_root is not None:
        try:
            verified_artifact_root = _absolute_canonical(
                Path(artifact_root), label="artifact_root"
            )
            root_info = os.lstat(verified_artifact_root)
            require(stat.S_ISDIR(root_info.st_mode), "artifact_root_not_directory")
        except (OSError, AuditEvidenceError):
            verified_artifact_root = None
            require(False, "artifact_root_unsafe")
    if isinstance(artifacts, Mapping):
        require(set(artifacts) == set(ARTIFACT_FILENAMES), "artifact_set_mismatch")
        for role, filename in ARTIFACT_FILENAMES.items():
            metadata = artifacts.get(role)
            require(isinstance(metadata, Mapping), f"artifact_not_object:{role}")
            if not isinstance(metadata, Mapping):
                continue
            require(
                set(metadata) == {"state", "path", "size", "sha256"},
                f"artifact_shape_mismatch:{role}",
            )
            state = metadata.get("state")
            size = metadata.get("size")
            require(_is_int(size) and int(size) >= 0, f"artifact_size_invalid:{role}")
            if state == "present":
                require(metadata.get("path") == filename, f"artifact_path_invalid:{role}")
                require(_is_hash(metadata.get("sha256")), f"artifact_sha_invalid:{role}")
                if role == "database":
                    require(_is_int(size) and int(size) > 0, "database_artifact_empty")
                if db_rows > 0:
                    require(
                        role not in {"audit_log", "anchor_history", "anchor_latest"}
                        or (_is_int(size) and int(size) > 0),
                        f"nonempty_chain_artifact_empty:{role}",
                    )
                if verified_artifact_root is not None:
                    candidate = verified_artifact_root / filename
                    try:
                        digest, _identity = _hash_pinned_regular(
                            candidate,
                            label=f"receipt_artifact:{role}",
                            maximum_bytes=artifact_maximums[role],
                        )
                        regular = True
                    except (OSError, AuditEvidenceError):
                        regular = False
                        digest = {}
                    require(regular, f"artifact_file_unsafe:{role}")
                    require(
                        digest.get("size") == size
                        and digest.get("sha256") == metadata.get("sha256"),
                        f"artifact_file_digest_mismatch:{role}",
                    )
            elif state == "absent":
                require(
                    metadata.get("path") is None
                    and size == 0
                    and metadata.get("sha256") is None,
                    f"absent_artifact_metadata_invalid:{role}",
                )
                require(
                    db_rows == 0 and role != "database",
                    f"required_artifact_absent:{role}",
                )
            else:
                require(False, f"artifact_state_invalid:{role}")

    secret_handling = value.get("secret_handling")
    require(
        isinstance(secret_handling, Mapping)
        and dict(secret_handling)
        == {
            "integrity_key": "memory_only",
            "chain_seed": "memory_only",
            "secret_files_copied": False,
            "secret_values_in_receipt": False,
        },
        "secret_handling_invalid",
    )
    classification = "PASS"
    if errors:
        classification = (
            "FAIL_PRODUCT"
            if value.get("verdict") == "FAIL_PRODUCT"
            else "FAIL_HARNESS"
        )
    return {
        "schema_version": "hackme.audit-evidence-triad-validation/v1",
        "ok": not errors,
        "classification": classification,
        "errors": sorted(set(errors)),
        "validated_invariants": sorted(INVARIANT_NAMES) if not errors else [],
        "artifact_files_verified": bool(
            verified_artifact_root is not None and not errors
        ),
    }


def _archive_member_limits(limits: ArchiveLimits) -> dict[str, int]:
    return {
        ARCHIVE_RECEIPT_FILENAME: int(limits.receipt_bytes),
        ARCHIVE_SCHEMA_FILENAME: int(limits.schema_bytes),
        ARTIFACT_FILENAMES["database"]: int(limits.database_bytes),
        ARTIFACT_FILENAMES["audit_log"]: int(limits.audit_log_bytes),
        ARTIFACT_FILENAMES["anchor_history"]: int(limits.anchor_history_bytes),
        ARTIFACT_FILENAMES["anchor_latest"]: int(limits.anchor_latest_bytes),
    }


def _archive_tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = int(size)
    info.mode = 0o400
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def create_audit_evidence_archive(
    *,
    output_dir: str | os.PathLike[str],
    archive_path: str | os.PathLike[str],
    limits: ArchiveLimits | None = None,
) -> dict[str, Any]:
    """Create a deterministic, private tar from one completed triad output.

    The receipt is the sole authority for artifact presence.  The archive has
    no directory entries and can contain only the receipt, the pinned v1 JSON
    schema, and the exact triad artifacts declared ``present``.  Runtime
    secrets and unrelated files are never candidates for inclusion.
    """

    active_limits = limits or ArchiveLimits()
    root = _absolute_canonical(Path(output_dir), label="output_dir")
    try:
        root_info = os.lstat(root)
    except OSError as exc:
        raise AuditEvidenceError(
            "unsafe_output_path", f"cannot inspect output_dir: {type(exc).__name__}"
        ) from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise AuditEvidenceError("unsafe_output_path", "output_dir is not a directory")

    destination = _absolute_canonical(Path(archive_path), label="archive_path")
    try:
        destination.relative_to(root)
    except ValueError:
        pass
    else:
        raise AuditEvidenceError(
            "unsafe_archive_path", "archive_path must be outside output_dir"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = _absolute_canonical(destination, label="archive_path")
    destination_parent = _absolute_canonical(
        destination.parent, label="archive_parent"
    )
    if destination.exists() or destination.is_symlink():
        raise AuditEvidenceError("archive_exists", "archive_path already exists")
    if not stat.S_ISDIR(os.lstat(destination_parent).st_mode):
        raise AuditEvidenceError("unsafe_archive_path", "archive parent is not a directory")

    member_limits = _archive_member_limits(active_limits)
    receipt_bytes, _receipt_identity = _read_pinned_regular(
        root / ARCHIVE_RECEIPT_FILENAME,
        label="receipt",
        maximum_bytes=member_limits[ARCHIVE_RECEIPT_FILENAME],
    )
    try:
        receipt_text = receipt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuditEvidenceError("invalid_utf8", "receipt is not UTF-8") from exc
    receipt = _strict_json_loads(receipt_text, label="receipt")
    required_mode = receipt.get("mode")
    required_target = receipt.get("target")
    if not isinstance(required_mode, str) or not isinstance(required_target, str):
        raise AuditEvidenceError(
            "invalid_receipt", "receipt lacks a string target or mode"
        )
    receipt_validation = validate_audit_evidence_receipt(
        receipt,
        required_mode=required_mode,
        required_target=required_target,
        artifact_root=root,
    )
    if receipt_validation.get("ok") is not True:
        codes = ",".join(str(code) for code in receipt_validation.get("errors", []))
        raise AuditEvidenceError(
            "invalid_receipt", f"completed receipt failed independent validation: {codes}"
        )

    schema_source = SCHEMA_PATH.resolve(strict=True)
    sources: list[tuple[str, Path, _Identity]] = []
    ordered_candidates: list[tuple[str, Path]] = [
        (ARCHIVE_RECEIPT_FILENAME, root / ARCHIVE_RECEIPT_FILENAME),
        (ARCHIVE_SCHEMA_FILENAME, schema_source),
    ]
    artifacts = receipt["artifacts"]
    for role in ("database", "audit_log", "anchor_history", "anchor_latest"):
        metadata = artifacts[role]
        if metadata["state"] == "present":
            filename = ARTIFACT_FILENAMES[role]
            ordered_candidates.append((filename, root / filename))
    for name, source in ordered_candidates:
        identity = _regular_identity(
            source,
            label=f"archive_source:{name}",
            required=True,
            maximum_bytes=member_limits[name],
        )
        assert identity is not None
        sources.append((name, source, identity))

    logical_tar_size = sum(
        ARCHIVE_BLOCK_SIZE
        + (
            (identity.size + ARCHIVE_BLOCK_SIZE - 1) // ARCHIVE_BLOCK_SIZE
        )
        * ARCHIVE_BLOCK_SIZE
        for _name, _source, identity in sources
    ) + 2 * ARCHIVE_BLOCK_SIZE
    expected_archive_size = (
        (logical_tar_size + ARCHIVE_RECORD_SIZE - 1) // ARCHIVE_RECORD_SIZE
    ) * ARCHIVE_RECORD_SIZE
    if expected_archive_size > int(active_limits.archive_bytes):
        raise AuditEvidenceError(
            "archive_oversize", "deterministic archive exceeds its hard bound"
        )

    archive_fd: int | None = None
    parent_fd: int | None = None
    pinned_archive_fd: int | None = None
    created_inode: tuple[int, int] | None = None
    created = False
    try:
        parent_fd = os.open(
            destination_parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_stat = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise AuditEvidenceError(
                "unsafe_archive_path", "archive parent descriptor is not a directory"
            )
        parent_inode = (int(parent_stat.st_dev), int(parent_stat.st_ino))
        archive_fd = _open_private_output_at(parent_fd, destination.name)
        created_stat = os.fstat(archive_fd)
        created_inode = (int(created_stat.st_dev), int(created_stat.st_ino))
        created = True
        with os.fdopen(archive_fd, "w+b", closefd=True) as archive_handle:
            archive_fd = None
            with tarfile.open(
                fileobj=archive_handle,
                mode="w",
                format=tarfile.USTAR_FORMAT,
            ) as bundle:
                for name, source, expected in sources:
                    source_fd = _open_readonly(
                        source, expected, label=f"archive_source:{name}"
                    )
                    with os.fdopen(source_fd, "rb", closefd=True) as source_handle:
                        bundle.addfile(
                            _archive_tar_info(name, expected.size), source_handle
                        )
                        if _Identity.from_stat(os.fstat(source_handle.fileno())) != expected:
                            raise AuditEvidenceError(
                                "unstable_source",
                                f"archive_source:{name} changed during archive creation",
                            )
                    if (
                        _regular_identity(
                            source,
                            label=f"archive_source:{name}",
                            required=True,
                            maximum_bytes=member_limits[name],
                        )
                        != expected
                    ):
                        raise AuditEvidenceError(
                            "unstable_source",
                            f"archive_source:{name} path changed during archive creation",
                        )
            archive_handle.flush()
            os.fsync(archive_handle.fileno())
        os.fsync(parent_fd)
        final_path_stat = os.stat(
            destination.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if created_inode != (
            int(final_path_stat.st_dev),
            int(final_path_stat.st_ino),
        ):
            raise AuditEvidenceError(
                "unstable_source", "archive path changed after creation"
            )
        current_parent = os.lstat(destination_parent)
        if parent_inode != (int(current_parent.st_dev), int(current_parent.st_ino)):
            raise AuditEvidenceError(
                "unstable_source", "archive parent path changed during creation"
            )
        pinned_archive_fd = os.open(
            destination.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        metadata, archive_identity = _hash_pinned_descriptor(
            pinned_archive_fd,
            label="audit_evidence_archive",
            maximum_bytes=int(active_limits.archive_bytes),
        )
        if stat.S_IMODE(archive_identity.mode) & 0o077:
            raise AuditEvidenceError(
                "archive_permissions", "archive is readable or writable by group/other"
            )
        if metadata["size"] != expected_archive_size:
            raise AuditEvidenceError(
                "archive_nondeterministic_length",
                "created archive does not have its unique canonical length",
            )
    except Exception:
        if archive_fd is not None:
            os.close(archive_fd)
        if pinned_archive_fd is not None:
            os.close(pinned_archive_fd)
        if created:
            try:
                if parent_fd is None:
                    raise FileNotFoundError
                current = os.stat(
                    destination.name, dir_fd=parent_fd, follow_symlinks=False
                )
                if created_inode == (int(current.st_dev), int(current.st_ino)):
                    os.unlink(destination.name, dir_fd=parent_fd)
            except (FileNotFoundError, OSError):
                pass
        if parent_fd is not None:
            os.close(parent_fd)
        raise
    try:
        validation = validate_audit_evidence_archive(
            required_mode=required_mode,
            required_target=required_target,
            descriptor=pinned_archive_fd,
            expected_sha256=str(metadata["sha256"]),
            expected_size=int(metadata["size"]),
            limits=active_limits,
        )
    finally:
        assert pinned_archive_fd is not None
        os.close(pinned_archive_fd)
        pinned_archive_fd = None
        assert parent_fd is not None
        os.close(parent_fd)
        parent_fd = None
    archive_ok = validation.get("ok") is True
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "ok": archive_ok,
        "classification": (
            "PASS" if archive_ok else str(validation.get("classification") or "FAIL_HARNESS")
        ),
        "errors": [] if archive_ok else list(validation.get("errors") or []),
        "target": required_target,
        "mode": required_mode,
        "archive_path": os.fspath(destination),
        "size": metadata["size"],
        "sha256": metadata["sha256"],
        "members": [name for name, _source, _identity in sources],
        "secret_files_included": False,
        "validation": validation,
    }


def _archive_validation_failure(
    errors: list[dict[str, str]],
    *,
    archive_metadata: Mapping[str, Any] | None = None,
    members: Mapping[str, Any] | None = None,
    receipt_validation: Mapping[str, Any] | None = None,
    receipt: Mapping[str, Any] | None = None,
    rederived: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    harness_failure = any(error.get("classification") == "FAIL_HARNESS" for error in errors)
    product_failure = any(error.get("classification") == "FAIL_PRODUCT" for error in errors)
    classification = (
        "FAIL_HARNESS"
        if harness_failure
        else "FAIL_PRODUCT" if product_failure else "FAIL_HARNESS"
    )
    return {
        "schema_version": ARCHIVE_VALIDATION_SCHEMA_VERSION,
        "ok": False,
        "classification": classification,
        "errors": errors,
        "archive": dict(archive_metadata or {}),
        "members": dict(members or {}),
        "receipt": dict(receipt or {}),
        "receipt_validation": dict(receipt_validation or {}),
        "rederived": dict(rederived or {}),
    }


def _machine_error(
    code: str,
    *,
    domain: str,
    classification: str,
    message: str,
) -> dict[str, str]:
    return {
        "code": str(code),
        "domain": str(domain),
        "classification": str(classification),
        "message": str(message),
    }


def _rederive_archive_evidence(
    root: Path,
    *,
    mode: str,
    maximum_jsonl_line_bytes: int,
    sqlite_validation_seconds: float,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Re-derive all properties that do not require the excluded HMAC key."""

    errors: list[dict[str, str]] = []

    def product_error(code: str, domain: str, message: str) -> None:
        errors.append(
            _machine_error(
                code,
                domain=domain,
                classification="FAIL_PRODUCT",
                message=message,
            )
        )

    def harness_error(code: str, domain: str, message: str) -> None:
        errors.append(
            _machine_error(
                code,
                domain=domain,
                classification="FAIL_HARNESS",
                message=message,
            )
        )

    invariants = {
        "sqlite_quick_check": False,
        "modern_audit_schema": False,
        "db_ids_strictly_increasing": False,
        "db_unkeyed_entry_hashes_valid": False,
        "db_prev_linkage_valid": False,
        "audit_log_valid": False,
        "audit_log_db_bijection": False,
        "anchor_history_valid": False,
        "anchor_history_references_db": False,
        "latest_matches_history_tail": False,
        "mode_anchor_policy": False,
    }
    db_rows: list[dict[str, Any]] = []
    database_path = root / ARTIFACT_FILENAMES["database"]
    sqlite_timed_out = False
    try:
        connection = sqlite3.connect(
            f"file:{quote(os.fspath(database_path), safe='/')}?mode=ro&nofollow=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        deadline = time.monotonic() + max(0.001, float(sqlite_validation_seconds))

        def stop_expensive_query() -> int:
            nonlocal sqlite_timed_out
            if time.monotonic() >= deadline:
                sqlite_timed_out = True
                return 1
            return 0

        connection.set_progress_handler(stop_expensive_query, 1000)
        try:
            quick = connection.execute("PRAGMA quick_check").fetchall()
            invariants["sqlite_quick_check"] = bool(quick) and all(
                str(row[0]).lower() == "ok" for row in quick
            )
            if not invariants["sqlite_quick_check"]:
                product_error(
                    "sqlite_quick_check_failed", "database", "SQLite quick_check failed"
                )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(secure_audit)"
                ).fetchall()
            }
            invariants["modern_audit_schema"] = set(DB_FIELDS).issubset(columns)
            if not invariants["modern_audit_schema"]:
                product_error(
                    "legacy_or_missing_schema",
                    "database",
                    "secure_audit modern columns are missing",
                )
            else:
                fetched = connection.execute(
                    f"SELECT {','.join(DB_FIELDS)} FROM secure_audit ORDER BY id ASC"
                ).fetchall()
                db_rows = [
                    {field: row[field] for field in DB_FIELDS} for row in fetched
                ]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        if sqlite_timed_out:
            harness_error(
                "sqlite_validation_timeout",
                "archive_validator",
                "SQLite evidence validation exceeded its hard deadline",
            )
        else:
            product_error(
                "snapshot_read_failed", "database", f"SQLite {type(exc).__name__}"
            )

    ids_valid = True
    entries_valid = True
    linkage_valid = True
    previous_id: int | None = None
    previous_chain: str | None = None
    for index, row in enumerate(db_rows):
        audit_id = row.get("id")
        if (
            not _is_int(audit_id)
            or int(audit_id) <= 0
            or (previous_id is not None and int(audit_id) <= previous_id)
        ):
            ids_valid = False
            product_error(
                "db_id_order", "database", "DB IDs are not strictly increasing"
            )
            continue
        previous_id = int(audit_id)
        if not _is_int(row.get("success")) or int(row["success"]) not in (0, 1):
            entries_valid = False
            product_error(
                "db_success_type", "database", "DB success is not 0 or 1"
            )
        base = {
            "ts": row.get("ts"),
            "action": row.get("action"),
            "ip": row.get("ip"),
            "user": row.get("user"),
            "success": bool(row.get("success")),
            "ua": row.get("ua"),
            "detail": row.get("detail"),
        }
        try:
            computed_entry = _entry_hash(base)
        except (TypeError, ValueError):
            entries_valid = False
            computed_entry = ""
            product_error(
                "db_entry_shape", "database", "DB row is not canonical JSON"
            )
        if (
            not _is_hash(row.get("entry_hash"))
            or not hmac.compare_digest(str(row.get("entry_hash") or ""), computed_entry)
        ):
            entries_valid = False
            product_error(
                "db_entry_hash_mismatch",
                "database",
                "DB entry_hash does not match canonical row bytes",
            )
        if not _is_hash(row.get("chain_hash")):
            linkage_valid = False
            product_error(
                "db_chain_hash_shape", "database", "DB chain_hash is not SHA-256"
            )
        if index == 0:
            if not isinstance(row.get("prev_hash"), str) or not row["prev_hash"]:
                linkage_valid = False
                product_error(
                    "db_initial_prev_missing",
                    "database",
                    "first DB row has no chain-seed reference",
                )
        elif row.get("prev_hash") != previous_chain:
            linkage_valid = False
            product_error(
                "db_prev_hash_mismatch",
                "database",
                "DB prev_hash does not reference the preceding chain_hash",
            )
        previous_chain = str(row.get("chain_hash") or "")
    invariants["db_ids_strictly_increasing"] = ids_valid
    invariants["db_unkeyed_entry_hashes_valid"] = entries_valid
    invariants["db_prev_linkage_valid"] = linkage_valid

    log_entries: list[dict[str, Any]] = []
    log_valid = True
    try:
        log_path = root / ARTIFACT_FILENAMES["audit_log"]
        if log_path.exists():
            log_entries = _decode_jsonl_file(
                log_path,
                label="archived_audit_log",
                maximum_line_bytes=maximum_jsonl_line_bytes,
            )
        elif db_rows:
            raise AuditEvidenceError(
                "audit_log_missing", "non-empty DB has no archived audit.log"
            )
    except (OSError, AuditEvidenceError) as exc:
        log_valid = False
        product_error(
            getattr(exc, "code", "audit_log_read_failed"),
            "audit_log",
            "archived audit.log is unreadable",
        )
    previous_log_id: int | None = None
    previous_log_chain: str | None = None
    for index, entry in enumerate(log_entries):
        if set(entry) != LOG_FIELDS:
            log_valid = False
            product_error(
                "log_shape", "audit_log", "audit log field set mismatch"
            )
            continue
        audit_id = entry.get("_audit_id")
        if (
            not _is_int(audit_id)
            or int(audit_id) <= 0
            or (previous_log_id is not None and int(audit_id) <= previous_log_id)
        ):
            log_valid = False
            product_error(
                "log_id_order", "audit_log", "audit log IDs are not ordered"
            )
            continue
        previous_log_id = int(audit_id)
        if not isinstance(entry.get("success"), bool):
            log_valid = False
            product_error(
                "log_success_type", "audit_log", "audit log success is not boolean"
            )
        base = {field: entry.get(field) for field in BASE_FIELDS}
        try:
            computed_entry = _entry_hash(base)
        except (TypeError, ValueError):
            computed_entry = ""
        if (
            not _is_hash(entry.get("_entry_hash"))
            or not hmac.compare_digest(
                str(entry.get("_entry_hash") or ""), computed_entry
            )
        ):
            log_valid = False
            product_error(
                "log_entry_hash_mismatch",
                "audit_log",
                "audit log entry_hash does not match canonical bytes",
            )
        if not _is_hash(entry.get("_chain_hash")):
            log_valid = False
            product_error(
                "log_chain_hash_shape", "audit_log", "audit log chain hash is invalid"
            )
        expected_prev = (
            db_rows[0].get("prev_hash")
            if index == 0 and db_rows
            else previous_log_chain
        )
        if entry.get("_prev_hash") != expected_prev:
            log_valid = False
            product_error(
                "log_prev_hash_mismatch",
                "audit_log",
                "audit log prev hash does not reference its predecessor",
            )
        previous_log_chain = str(entry.get("_chain_hash") or "")

    db_ids = [row.get("id") for row in db_rows]
    log_ids = [entry.get("_audit_id") for entry in log_entries]
    bijection = log_valid and db_ids == log_ids and len(db_rows) == len(log_entries)
    if db_ids != log_ids or len(db_rows) != len(log_entries):
        product_error(
            "log_db_id_set_mismatch",
            "cross_source",
            "DB and audit log ID sequences are not one-to-one",
        )
    for row, entry in zip(db_rows, log_entries):
        expected = {
            "ts": row.get("ts"),
            "action": row.get("action"),
            "ip": row.get("ip"),
            "user": row.get("user"),
            "success": bool(row.get("success")),
            "ua": row.get("ua"),
            "detail": row.get("detail"),
            "_audit_id": row.get("id"),
            "_prev_hash": row.get("prev_hash"),
            "_entry_hash": row.get("entry_hash"),
            "_chain_hash": row.get("chain_hash"),
        }
        if entry != expected:
            bijection = False
            product_error(
                "log_db_mismatch",
                "cross_source",
                "audit log entry differs from its DB row",
            )
    invariants["audit_log_valid"] = log_valid
    invariants["audit_log_db_bijection"] = bijection

    history: list[dict[str, Any]] = []
    latest: dict[str, Any] | None = None
    history_valid = True
    references_valid = True
    latest_tail_valid = True
    for name, destination in (
        ("history", root / ARTIFACT_FILENAMES["anchor_history"]),
        ("latest", root / ARTIFACT_FILENAMES["anchor_latest"]),
    ):
        if not destination.exists():
            continue
        try:
            if name == "history":
                history = _decode_jsonl_file(
                    destination,
                    label="archive_anchor_history",
                    maximum_line_bytes=maximum_jsonl_line_bytes,
                )
            else:
                latest = _strict_json_loads(
                    destination.read_text(encoding="utf-8"),
                    label="archive_anchor_latest",
                )
        except (OSError, UnicodeDecodeError, AuditEvidenceError):
            if name == "history":
                history_valid = False
            else:
                latest_tail_valid = False
            product_error(
                f"anchor_{name}_unreadable", "anchors", f"anchor {name} is unreadable"
            )
    db_by_id = {
        int(row["id"]): row for row in db_rows if _is_int(row.get("id"))
    }
    previous_anchor_id: int | None = None
    for anchor in history:
        if set(anchor) != ANCHOR_FIELDS:
            history_valid = False
            product_error(
                "anchor_shape", "anchors", "anchor history field set mismatch"
            )
            continue
        audit_id = anchor.get("audit_id")
        if (
            not _is_int(audit_id)
            or int(audit_id) <= 0
            or (previous_anchor_id is not None and int(audit_id) < previous_anchor_id)
        ):
            history_valid = False
            product_error(
                "anchor_id_order", "anchors", "anchor IDs are invalid or move backwards"
            )
            continue
        previous_anchor_id = int(audit_id)
        if (
            not _parse_timestamp(anchor.get("ts"))
            or not _is_hash(anchor.get("entry_hash"))
            or not _is_hash(anchor.get("chain_hash"))
            or not isinstance(anchor.get("reason"), str)
            or not str(anchor["reason"]).strip()
        ):
            history_valid = False
            product_error(
                "anchor_value_shape", "anchors", "anchor values are malformed"
            )
        row = db_by_id.get(int(audit_id))
        if (
            row is None
            or row.get("entry_hash") != anchor.get("entry_hash")
            or row.get("chain_hash") != anchor.get("chain_hash")
        ):
            references_valid = False
            product_error(
                "anchor_db_mismatch",
                "cross_source",
                "anchor does not reference the exact DB row",
            )

    if db_rows:
        if not history:
            history_valid = False
            product_error(
                "anchor_history_missing", "anchors", "non-empty DB has no history"
            )
        if latest is None:
            latest_tail_valid = False
            references_valid = False
            product_error(
                "latest_missing", "anchors", "non-empty DB has no latest anchor"
            )
        elif set(latest) != ANCHOR_FIELDS:
            latest_tail_valid = False
            references_valid = False
            product_error(
                "latest_shape", "anchors", "latest anchor field set mismatch"
            )
        else:
            if not history or history[-1] != latest:
                latest_tail_valid = False
                product_error(
                    "latest_history_mismatch",
                    "anchors",
                    "latest anchor is not the history tail",
                )
            latest_id = latest.get("audit_id")
            row = db_by_id.get(int(latest_id)) if _is_int(latest_id) else None
            if (
                row is None
                or row.get("entry_hash") != latest.get("entry_hash")
                or row.get("chain_hash") != latest.get("chain_hash")
            ):
                references_valid = False
                product_error(
                    "latest_db_mismatch",
                    "cross_source",
                    "latest anchor does not reference the exact DB row",
                )
    else:
        if history:
            history_valid = False
            references_valid = False
            product_error(
                "anchors_for_empty_db", "anchors", "empty DB has anchor history"
            )
        if latest is not None:
            latest_tail_valid = False
            references_valid = False
            product_error(
                "latest_for_empty_db", "anchors", "empty DB has latest anchor"
            )

    rows_after_latest = 0
    if latest is not None and _is_int(latest.get("audit_id")):
        rows_after_latest = sum(
            1 for row in db_rows if _is_int(row.get("id")) and row["id"] > latest["audit_id"]
        )
    db_head = None
    log_head = None
    if db_rows and _is_int(db_rows[-1].get("id")):
        db_head = {
            "audit_id": int(db_rows[-1]["id"]),
            "entry_hash": str(db_rows[-1].get("entry_hash") or ""),
            "chain_hash": str(db_rows[-1].get("chain_hash") or ""),
        }
    if log_entries and _is_int(log_entries[-1].get("_audit_id")):
        log_head = {
            "audit_id": int(log_entries[-1]["_audit_id"]),
            "entry_hash": str(log_entries[-1].get("_entry_hash") or ""),
            "chain_hash": str(log_entries[-1].get("_chain_hash") or ""),
        }
    mode_policy = references_valid
    if mode == "sealed" and db_head is not None:
        mode_policy = bool(
            latest is not None
            and latest.get("audit_id") == db_head["audit_id"]
            and latest.get("entry_hash") == db_head["entry_hash"]
            and latest.get("chain_hash") == db_head["chain_hash"]
            and latest.get("reason") == SEALED_REASON
            and rows_after_latest == 0
        )
        if not mode_policy:
            product_error(
                "sealed_head_mismatch",
                "anchors",
                "sealed latest anchor does not equal DB head",
            )
    elif mode == "sealed":
        mode_policy = not history and latest is None
    invariants["anchor_history_valid"] = history_valid
    invariants["anchor_history_references_db"] = references_valid
    invariants["latest_matches_history_tail"] = latest_tail_valid
    invariants["mode_anchor_policy"] = mode_policy
    return {
        "counts": {
            "db_rows": len(db_rows),
            "log_entries": len(log_entries),
            "anchor_history_entries": len(history),
            "rows_after_latest": rows_after_latest,
        },
        "heads": {
            "database": db_head,
            "audit_log": log_head,
            "anchor_latest": _receipt_anchor_head(latest),
        },
        "invariants": invariants,
    }, errors


def _read_exact_at(descriptor: int, offset: int, size: int) -> bytes:
    content = bytearray()
    while len(content) < size:
        block = os.pread(descriptor, size - len(content), offset + len(content))
        if not block:
            break
        content.extend(block)
    return bytes(content)


def _canonical_tar_size(raw: bytes) -> int:
    if len(raw) != 12 or raw[0] & 0x80:
        raise AuditEvidenceError(
            "archive_nondeterministic_header", "tar size is not canonical octal"
        )
    stripped = raw.rstrip(b"\0 ")
    if not stripped or any(value not in b"01234567" for value in stripped):
        raise AuditEvidenceError(
            "archive_nondeterministic_header", "tar size is not canonical octal"
        )
    try:
        return int(stripped, 8)
    except ValueError as exc:
        raise AuditEvidenceError(
            "archive_nondeterministic_header", "tar size is invalid"
        ) from exc


def _materialize_canonical_archive(
    descriptor: int,
    identity: _Identity,
    destination_root: Path,
    *,
    member_limits: Mapping[str, int],
    maximum_total_bytes: int,
) -> dict[str, dict[str, Any]]:
    """Parse only the narrow USTAR subset emitted by the archive creator."""

    manifest: dict[str, dict[str, Any]] = {}
    offset = 0
    total_declared = 0
    zero_block = b"\0" * ARCHIVE_BLOCK_SIZE
    while True:
        header = _read_exact_at(descriptor, offset, ARCHIVE_BLOCK_SIZE)
        if len(header) != ARCHIVE_BLOCK_SIZE:
            raise AuditEvidenceError(
                "archive_member_truncated", "archive ended before a complete tar header"
            )
        if header == zero_block:
            second = _read_exact_at(
                descriptor, offset + ARCHIVE_BLOCK_SIZE, ARCHIVE_BLOCK_SIZE
            )
            if second != zero_block:
                raise AuditEvidenceError(
                    "archive_trailing_or_truncated_data",
                    "archive lacks the canonical two-block terminator",
                )
            logical_size = offset + 2 * ARCHIVE_BLOCK_SIZE
            expected_size = (
                (logical_size + ARCHIVE_RECORD_SIZE - 1) // ARCHIVE_RECORD_SIZE
            ) * ARCHIVE_RECORD_SIZE
            if identity.size != expected_size:
                raise AuditEvidenceError(
                    "archive_trailing_or_truncated_data",
                    "archive byte length is not its unique canonical tar length",
                )
            padding = _read_exact_at(
                descriptor,
                logical_size,
                identity.size - logical_size,
            )
            if len(padding) != identity.size - logical_size or any(padding):
                raise AuditEvidenceError(
                    "archive_trailing_or_truncated_data",
                    "archive record padding contains hidden bytes",
                )
            break

        if len(manifest) >= len(member_limits):
            raise AuditEvidenceError(
                "archive_member_bomb", "archive has too many members"
            )
        name_field = header[:100]
        name_bytes, separator, suffix = name_field.partition(b"\0")
        if not separator or any(suffix) or any(header[345:500]):
            raise AuditEvidenceError(
                "archive_nondeterministic_header",
                "tar member name uses an extended or non-canonical form",
            )
        try:
            name = name_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AuditEvidenceError(
                "archive_unexpected_member", "tar member name is not ASCII"
            ) from exc
        if name not in member_limits:
            raise AuditEvidenceError(
                "archive_unexpected_member",
                "archive contains traversal, secret, or unexpected member",
            )
        if name in manifest:
            raise AuditEvidenceError(
                "archive_duplicate_member", "archive member is duplicated"
            )
        if header[156:157] != tarfile.REGTYPE:
            raise AuditEvidenceError(
                "archive_unsafe_member_type",
                "archive members must be plain regular files",
            )
        size = _canonical_tar_size(header[124:136])
        if size > int(member_limits[name]):
            raise AuditEvidenceError(
                "archive_member_oversize", "archive member exceeds its hard bound"
            )
        total_declared += size
        if total_declared > maximum_total_bytes:
            raise AuditEvidenceError(
                "archive_expansion_bomb",
                "archive declared content exceeds total hard bound",
            )
        expected_header = _archive_tar_info(name, size).tobuf(
            format=tarfile.USTAR_FORMAT,
            encoding="utf-8",
            errors="strict",
        )
        if header != expected_header:
            raise AuditEvidenceError(
                "archive_nondeterministic_metadata",
                "archive member header or checksum is not canonical",
            )

        data_offset = offset + ARCHIVE_BLOCK_SIZE
        padded_size = (
            (size + ARCHIVE_BLOCK_SIZE - 1) // ARCHIVE_BLOCK_SIZE
        ) * ARCHIVE_BLOCK_SIZE
        if data_offset + padded_size > identity.size:
            raise AuditEvidenceError(
                "archive_member_truncated", "archive member exceeds physical archive"
            )
        destination = destination_root / name
        output_fd = _open_private_output(destination)
        digest = hashlib.sha256()
        copied = 0
        try:
            while copied < size:
                block = os.pread(
                    descriptor,
                    min(COPY_CHUNK_BYTES, size - copied),
                    data_offset + copied,
                )
                if not block:
                    raise AuditEvidenceError(
                        "archive_member_truncated",
                        "archive member ended before declared size",
                    )
                digest.update(block)
                view = memoryview(block)
                written_total = 0
                while written_total < len(view):
                    written = os.write(output_fd, view[written_total:])
                    if written <= 0:
                        raise AuditEvidenceError(
                            "artifact_write_failed",
                            "archive validator write made no progress",
                        )
                    written_total += written
                copied += len(block)
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        padding_size = padded_size - size
        if padding_size:
            padding = _read_exact_at(descriptor, data_offset + size, padding_size)
            if len(padding) != padding_size or any(padding):
                raise AuditEvidenceError(
                    "archive_nondeterministic_header",
                    "tar member padding contains non-zero bytes",
                )
        manifest[name] = {"size": copied, "sha256": digest.hexdigest()}
        offset = data_offset + padded_size
    return manifest


def validate_audit_evidence_archive(
    archive_path: str | os.PathLike[str] | None = None,
    *,
    required_mode: str,
    required_target: str,
    descriptor: int | None = None,
    expected_sha256: str,
    expected_size: int,
    limits: ArchiveLimits | None = None,
) -> dict[str, Any]:
    """Independently validate a pinned triad tar without extracting paths.

    The archive inode, size, and mtime are pinned across hashing and parsing.
    Only six fixed basenames are recognized; every member is streamed into a
    private validator directory after its header and bounds are checked.
    """

    active_limits = limits or ArchiveLimits()
    errors: list[dict[str, str]] = []
    archive_metadata: dict[str, Any] = {}
    member_manifest: dict[str, dict[str, Any]] = {}
    receipt_evidence: dict[str, Any] = {}
    receipt_validation: dict[str, Any] = {}
    rederived: dict[str, Any] = {}
    owned_descriptor = -1

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal owned_descriptor
        if owned_descriptor >= 0:
            os.close(owned_descriptor)
            owned_descriptor = -1
        return payload

    try:
        pinned_path: Path | None = None
        if descriptor is not None:
            if not _is_int(descriptor) or int(descriptor) < 0:
                raise AuditEvidenceError(
                    "archive_descriptor_invalid", "descriptor is not a valid integer FD"
                )
            owned_descriptor = os.dup(int(descriptor))
            metadata, identity = _hash_pinned_descriptor(
                owned_descriptor,
                label="audit_evidence_archive",
                maximum_bytes=int(active_limits.archive_bytes),
            )
            if archive_path is not None:
                pinned_path = _absolute_canonical(
                    Path(archive_path), label="archive_path"
                )
                path_identity = _regular_identity(
                    pinned_path,
                    label="audit_evidence_archive",
                    required=True,
                    maximum_bytes=int(active_limits.archive_bytes),
                )
                if path_identity != identity:
                    raise AuditEvidenceError(
                        "archive_descriptor_path_mismatch",
                        "descriptor does not identify archive_path",
                    )
        else:
            if archive_path is None:
                raise AuditEvidenceError(
                    "archive_path_missing", "archive_path or descriptor is required"
                )
            pinned_path = _absolute_canonical(Path(archive_path), label="archive_path")
            path_identity = _regular_identity(
                pinned_path,
                label="audit_evidence_archive",
                required=True,
                maximum_bytes=int(active_limits.archive_bytes),
            )
            assert path_identity is not None
            owned_descriptor = _open_readonly(
                pinned_path, path_identity, label="audit_evidence_archive"
            )
            metadata, identity = _hash_pinned_descriptor(
                owned_descriptor,
                label="audit_evidence_archive",
                maximum_bytes=int(active_limits.archive_bytes),
            )
        archive_metadata = {
            "path": os.fspath(pinned_path) if pinned_path is not None else None,
            "descriptor_pinned": descriptor is not None,
            "authenticity_pinned": True,
            "size_pinned": True,
            "size": metadata["size"],
            "sha256": metadata["sha256"],
        }
        if stat.S_IMODE(identity.mode) & 0o077:
            raise AuditEvidenceError(
                "archive_permissions", "archive is accessible to group/other"
            )
        if not _is_int(expected_size) or int(expected_size) != metadata["size"]:
            raise AuditEvidenceError("archive_size_pin_mismatch", "archive size pin differs")
        if not _is_hash(expected_sha256) or not hmac.compare_digest(
            expected_sha256, metadata["sha256"]
        ):
            raise AuditEvidenceError(
                "archive_sha256_pin_mismatch", "archive SHA-256 pin differs"
            )

        member_limits = _archive_member_limits(active_limits)
        with tempfile.TemporaryDirectory(prefix="audit-triad-archive-") as temporary_name:
            extraction_root = Path(temporary_name)
            os.chmod(extraction_root, 0o700)
            filesystem = os.statvfs(extraction_root)
            available_bytes = int(filesystem.f_bavail) * int(filesystem.f_frsize)
            required_free_bytes = identity.size + max(
                0, int(active_limits.free_space_reserve_bytes)
            )
            if available_bytes < required_free_bytes:
                raise AuditEvidenceError(
                    "archive_insufficient_disk",
                    "validator filesystem lacks bounded extraction headroom",
                )
            member_manifest = _materialize_canonical_archive(
                owned_descriptor,
                identity,
                extraction_root,
                member_limits=member_limits,
                maximum_total_bytes=int(active_limits.archive_bytes),
            )
            if _Identity.from_stat(os.fstat(owned_descriptor)) != identity:
                raise AuditEvidenceError(
                    "unstable_source", "archive descriptor changed while being parsed"
                )
            if pinned_path is not None:
                if (
                    _regular_identity(
                        pinned_path,
                        label="audit_evidence_archive",
                        required=True,
                        maximum_bytes=int(active_limits.archive_bytes),
                    )
                    != identity
                ):
                    raise AuditEvidenceError(
                        "unstable_source", "archive path changed while being parsed"
                    )

            required_base = {ARCHIVE_RECEIPT_FILENAME, ARCHIVE_SCHEMA_FILENAME}
            if not required_base.issubset(member_manifest):
                raise AuditEvidenceError(
                    "archive_required_member_missing", "receipt or JSON schema is missing"
                )
            receipt_bytes, _ = _read_pinned_regular(
                extraction_root / ARCHIVE_RECEIPT_FILENAME,
                label="archived_receipt",
                maximum_bytes=int(active_limits.receipt_bytes),
            )
            try:
                receipt = _strict_json_loads(
                    receipt_bytes.decode("utf-8"), label="archived_receipt"
                )
            except UnicodeDecodeError as exc:
                raise AuditEvidenceError(
                    "invalid_utf8", "archived receipt is not UTF-8"
                ) from exc
            receipt_evidence = {
                "payload": receipt,
                "size": member_manifest[ARCHIVE_RECEIPT_FILENAME]["size"],
                "sha256": member_manifest[ARCHIVE_RECEIPT_FILENAME]["sha256"],
            }
            archived_schema, _ = _read_pinned_regular(
                extraction_root / ARCHIVE_SCHEMA_FILENAME,
                label="archived_schema",
                maximum_bytes=int(active_limits.schema_bytes),
            )
            current_schema, _ = _read_pinned_regular(
                SCHEMA_PATH.resolve(strict=True),
                label="current_schema",
                maximum_bytes=int(active_limits.schema_bytes),
            )
            if archived_schema != current_schema:
                raise AuditEvidenceError(
                    "archive_schema_mismatch", "archived JSON schema is not the pinned v1 schema"
                )
            _strict_json_loads(archived_schema.decode("utf-8"), label="archived_schema")

            artifacts = receipt.get("artifacts")
            if not isinstance(artifacts, Mapping):
                raise AuditEvidenceError(
                    "invalid_receipt", "archived receipt has no artifact inventory"
                )
            expected_member_order = [
                ARCHIVE_RECEIPT_FILENAME,
                ARCHIVE_SCHEMA_FILENAME,
            ]
            for role, filename in ARTIFACT_FILENAMES.items():
                metadata_value = artifacts.get(role)
                if isinstance(metadata_value, Mapping) and metadata_value.get("state") == "present":
                    expected_member_order.append(filename)
            if list(member_manifest) != expected_member_order:
                raise AuditEvidenceError(
                    "archive_member_inventory_mismatch",
                    "archive members do not exactly match canonical receipt inventory order",
                )
            receipt_validation = validate_audit_evidence_receipt(
                receipt,
                required_mode=required_mode,
                required_target=required_target,
                artifact_root=extraction_root,
            )
            if receipt_validation.get("ok") is not True:
                for code in receipt_validation.get("errors", []):
                    errors.append(
                        _machine_error(
                            f"receipt_contract:{code}",
                            domain="receipt",
                            classification="FAIL_HARNESS",
                            message="archived receipt failed independent contract validation",
                        )
                    )
                return finish(
                    _archive_validation_failure(
                        errors,
                        archive_metadata=archive_metadata,
                        members=member_manifest,
                        receipt=receipt_evidence,
                        receipt_validation=receipt_validation,
                    )
                )
            rederived, evidence_errors = _rederive_archive_evidence(
                extraction_root,
                mode=str(required_mode).strip().lower(),
                maximum_jsonl_line_bytes=int(active_limits.jsonl_line_bytes),
                sqlite_validation_seconds=float(
                    active_limits.sqlite_validation_seconds
                ),
            )
            errors.extend(evidence_errors)
            if rederived.get("counts") != receipt.get("counts"):
                errors.append(
                    _machine_error(
                        "receipt_counts_mismatch",
                        domain="receipt",
                        classification="FAIL_HARNESS",
                        message="receipt counts differ from archived bytes",
                    )
                )
            if rederived.get("heads") != receipt.get("heads"):
                errors.append(
                    _machine_error(
                        "receipt_heads_mismatch",
                        domain="receipt",
                        classification="FAIL_HARNESS",
                        message="receipt heads differ from archived bytes",
                    )
                )
            if not all(rederived.get("invariants", {}).values()):
                if not evidence_errors:
                    errors.append(
                        _machine_error(
                            "rederived_invariant_false",
                            domain="cross_source",
                            classification="FAIL_PRODUCT",
                            message="one or more independently derived invariants are false",
                        )
                    )
            if errors:
                return finish(
                    _archive_validation_failure(
                        errors,
                        archive_metadata=archive_metadata,
                        members=member_manifest,
                        receipt=receipt_evidence,
                        receipt_validation=receipt_validation,
                        rederived=rederived,
                    )
                )
    except AuditEvidenceError as exc:
        errors.append(
            _machine_error(
                exc.code,
                domain="archive",
                classification="FAIL_HARNESS",
                message=str(exc),
            )
        )
        return finish(
            _archive_validation_failure(
                errors,
                archive_metadata=archive_metadata,
                members=member_manifest,
                receipt=receipt_evidence,
                receipt_validation=receipt_validation,
                rederived=rederived,
            )
        )
    except (OSError, tarfile.TarError, UnicodeDecodeError, ValueError) as exc:
        errors.append(
            _machine_error(
                "archive_validation_error",
                domain="archive",
                classification="FAIL_HARNESS",
                message=type(exc).__name__,
            )
        )
        return finish(
            _archive_validation_failure(
                errors,
                archive_metadata=archive_metadata,
                members=member_manifest,
                receipt=receipt_evidence,
                receipt_validation=receipt_validation,
                rederived=rederived,
            )
        )
    except Exception as exc:
        errors.append(
            _machine_error(
                "archive_validator_internal_error",
                domain="archive",
                classification="FAIL_HARNESS",
                message=type(exc).__name__,
            )
        )
        return finish(
            _archive_validation_failure(
                errors,
                archive_metadata=archive_metadata,
                members=member_manifest,
                receipt=receipt_evidence,
                receipt_validation=receipt_validation,
                rederived=rederived,
            )
        )
    return finish(
        {
            "schema_version": ARCHIVE_VALIDATION_SCHEMA_VERSION,
            "ok": True,
            "classification": "PASS",
            "errors": [],
            "archive": archive_metadata,
            "members": member_manifest,
            "receipt": receipt_evidence,
            "receipt_validation": receipt_validation,
            "rederived": rederived,
        }
    )


def capture_audit_evidence(
    *,
    paths: AuditEvidencePaths,
    output_dir: str | os.PathLike[str],
    target: str,
    mode: str = "online",
    limits: CaptureLimits | None = None,
) -> dict[str, Any]:
    """Capture and validate one runtime's audit evidence.

    ``online`` accepts a valid latest anchor that is a prefix of the DB/log
    chain.  ``sealed`` appends an explicit ``formal_evidence_seal`` anchor
    under the mutation lock and requires it to equal the captured DB/log head.
    The caller remains responsible for proving that formal writers have
    stopped before using sealed mode.
    """

    selected_mode = str(mode).strip().lower()
    if selected_mode not in MODES:
        raise ValueError(f"unsupported audit evidence mode: {mode}")
    target_name = str(target).strip()
    if not target_name or len(target_name) > 128:
        raise ValueError("target must be a non-empty string of at most 128 characters")
    active_limits = limits or CaptureLimits()
    destination = _absolute_canonical(Path(output_dir), label="output_dir")
    runtime_for_output_check = _absolute_canonical(
        Path(paths.runtime_root), label="runtime_root"
    )
    try:
        destination.relative_to(runtime_for_output_check)
    except ValueError:
        pass
    else:
        raise AuditEvidenceError(
            "unsafe_output_path", "output_dir must be outside the audited runtime"
        )
    if destination.exists():
        raise AuditEvidenceError("artifact_exists", "output_dir already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    os.chmod(destination, 0o700)

    invariants = {name: False for name in INVARIANT_NAMES}
    errors: list[dict[str, Any]] = []
    artifacts: dict[str, dict[str, Any]] = {}
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target": target_name,
        "mode": selected_mode,
        "captured_at": _utc_now(),
        "ok": False,
        "verdict": "FAIL_HARNESS",
        "capture": {
            "mutation_lock_wait_ms": None,
            "head_anchor": {"attempted": selected_mode == "sealed", "performed": False},
            "sqlite_backup_api": True,
            "immutable_validation": True,
        },
        "artifacts": artifacts,
        "counts": {"db_rows": 0, "log_entries": 0, "anchor_history_entries": 0, "rows_after_latest": 0},
        "heads": {"database": None, "audit_log": None, "anchor_latest": None},
        "invariants": invariants,
        "errors": errors,
        "secret_handling": {
            "integrity_key": "memory_only",
            "chain_seed": "memory_only",
            "secret_files_copied": False,
            "secret_values_in_receipt": False,
        },
    }
    chain_seed = ""
    integrity_key = b""
    try:
        reviewed_paths = _validate_paths(paths)
        invariants["safe_paths"] = True
        with _mutation_guard(
            reviewed_paths.mutation_lock,
            timeout_seconds=active_limits.lock_timeout_seconds,
        ) as wait_ms:
            invariants["mutation_lock_acquired"] = True
            receipt["capture"]["mutation_lock_wait_ms"] = round(wait_ms, 3)
            chain_seed = str(_read_secret(reviewed_paths.chain_seed, label="chain_seed", text=True))
            integrity_key = bytes(
                _read_secret(reviewed_paths.integrity_key, label="integrity_key", text=False)
            )
            if selected_mode == "sealed":
                receipt["capture"]["head_anchor"] = _force_head_anchor(reviewed_paths)

            db_artifact = destination / "audit_snapshot.sqlite3"
            artifacts["database"] = _sqlite_backup(
                reviewed_paths.database,
                db_artifact,
                maximum_bytes=active_limits.database_bytes,
                timeout_seconds=active_limits.backup_timeout_seconds,
            )[0]
            invariants["sqlite_backup_complete"] = True
            artifacts["audit_log"] = _copy_source(
                reviewed_paths.audit_log,
                destination / "audit.log",
                label="audit_log",
                maximum_bytes=active_limits.audit_log_bytes,
                required=False,
            )[0]
            artifacts["anchor_history"] = _copy_source(
                reviewed_paths.anchor_history,
                destination / "audit_head.jsonl",
                label="anchor_history",
                maximum_bytes=active_limits.anchor_history_bytes,
                required=False,
            )[0]
            artifacts["anchor_latest"] = _copy_source(
                reviewed_paths.anchor_latest,
                destination / "audit_head_latest.json",
                label="anchor_latest",
                maximum_bytes=active_limits.anchor_latest_bytes,
                required=False,
            )[0]
            invariants["capture_stable"] = True

        db_rows, db_head = _read_db_snapshot(
            destination / "audit_snapshot.sqlite3",
            chain_seed=chain_seed,
            integrity_key=integrity_key,
            invariants=invariants,
            errors=errors,
        )
        log_content = _read_artifact(
            destination / "audit.log", artifacts["audit_log"], label="audit_log"
        )
        log_entries, log_head = _validate_log(
            log_content,
            db_rows,
            chain_seed=chain_seed,
            integrity_key=integrity_key,
            invariants=invariants,
            errors=errors,
        )
        history_content = _read_artifact(
            destination / "audit_head.jsonl",
            artifacts["anchor_history"],
            label="anchor_history",
        )
        latest_content = _read_artifact(
            destination / "audit_head_latest.json",
            artifacts["anchor_latest"],
            label="anchor_latest",
        )
        history, latest, rows_after_latest = _validate_anchors(
            history_content,
            latest_content,
            db_rows,
            db_head,
            mode=selected_mode,
            invariants=invariants,
            errors=errors,
        )
        receipt["counts"] = {
            "db_rows": len(db_rows),
            "log_entries": len(log_entries),
            "anchor_history_entries": len(history),
            "rows_after_latest": rows_after_latest,
        }
        receipt["heads"] = {
            "database": db_head,
            "audit_log": log_head,
            "anchor_latest": _receipt_anchor_head(latest),
        }
        # The receipt contains only public chain heads and operational
        # metadata.  The two secrets are never interpolated into errors or
        # copied into the artifact set.
        encoded_without_flag = canonical_json(receipt)
        key_leaked = bool(integrity_key) and integrity_key.hex() in encoded_without_flag
        seed_leaked = bool(chain_seed) and chain_seed in encoded_without_flag
        invariants["secret_material_excluded_from_receipt"] = not key_leaked and not seed_leaked
        if key_leaked or seed_leaked:
            _append_error(errors, code="secret_in_receipt", domain="validator", message="secret material reached the receipt")
        product_ok = all(invariants.values()) and not errors
        receipt["ok"] = product_ok
        receipt["verdict"] = "PASS" if product_ok else "FAIL_PRODUCT"
    except AuditEvidenceError as exc:
        _append_error(errors, code=exc.code, domain="capture", message=str(exc))
        receipt["ok"] = False
        receipt["verdict"] = "FAIL_HARNESS"
    except Exception as exc:  # fail closed without serializing source values
        _append_error(
            errors,
            code="validator_internal_error",
            domain="validator",
            message=type(exc).__name__,
        )
        receipt["ok"] = False
        receipt["verdict"] = "FAIL_HARNESS"
    finally:
        chain_seed = ""
        integrity_key = b""
        receipt["completed_at"] = _utc_now()
        _atomic_write_json(destination / "receipt.json", receipt)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--mode", choices=sorted(MODES), default="online")
    parser.add_argument("--database")
    parser.add_argument("--audit-log")
    parser.add_argument("--anchor-history")
    parser.add_argument("--anchor-latest")
    parser.add_argument("--chain-seed")
    parser.add_argument("--integrity-key")
    parser.add_argument("--mutation-lock")
    parser.add_argument("--lock-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--backup-timeout-seconds", type=float, default=60.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    defaults = AuditEvidencePaths.for_runtime(Path(args.runtime_root))
    paths = AuditEvidencePaths(
        runtime_root=Path(args.runtime_root),
        database=Path(args.database) if args.database else defaults.database,
        audit_log=Path(args.audit_log) if args.audit_log else defaults.audit_log,
        anchor_history=Path(args.anchor_history) if args.anchor_history else defaults.anchor_history,
        anchor_latest=Path(args.anchor_latest) if args.anchor_latest else defaults.anchor_latest,
        chain_seed=Path(args.chain_seed) if args.chain_seed else defaults.chain_seed,
        integrity_key=Path(args.integrity_key) if args.integrity_key else defaults.integrity_key,
        mutation_lock=Path(args.mutation_lock) if args.mutation_lock else defaults.mutation_lock,
    )
    receipt = capture_audit_evidence(
        paths=paths,
        output_dir=Path(args.output_dir),
        target=args.target,
        mode=args.mode,
        limits=CaptureLimits(
            lock_timeout_seconds=max(0.0, args.lock_timeout_seconds),
            backup_timeout_seconds=max(0.1, args.backup_timeout_seconds),
        ),
    )
    print(canonical_json({"schema_version": SCHEMA_VERSION, "ok": receipt["ok"], "verdict": receipt["verdict"]}))
    return 0 if receipt["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
