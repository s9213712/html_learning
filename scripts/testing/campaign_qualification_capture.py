#!/usr/bin/env python3
"""Capture immutable raw authority for one formal qualification gate.

This module is deliberately a transport and binding layer, not a native
evidence producer.  Callers must supply every native artifact required by the
reviewed gate.  JSON objects and JSONL records retain their native fields and
receive a writer-controlled ``formal_binding``; opaque artifacts are copied
byte-for-byte.  A candidate evidence envelope is returned only after
``validate_gate_evidence`` independently re-derives the gate from the captured
raw authority.

The writer never accepts ``actual_execution``, ``simulated`` or
``component_only`` inputs.  Its binding is tied to the live capture process,
the exact H0 source authority, and a new, non-overwritable attempt directory.
Path/hash-linked producers use ``planned_capture_paths`` and
``project_bound_json_identity``; the writer never silently rewrites their
native authority.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid
from typing import Any, BinaryIO, Mapping, Sequence


if __package__ in {None, ""}:
    _REPO_ROOT = Path(__file__).resolve().parents[2]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))


try:
    from scripts.testing.campaign_gate_bundle import (
        GATE_EVIDENCE_SCHEMA_VERSION,
        GATE_POLICIES,
        GATE_RAW_SPECS,
        CAPTURE_PRODUCER_KIND,
        NATIVE_PRODUCER_KIND,
        NATIVE_EXECUTION_RECEIPT_SCHEMA_VERSION,
        QUALIFICATION_ATTEMPT_SCHEMA_VERSION,
        RAW_ARTIFACT_BINDING_SCHEMA_VERSION,
        RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION,
        GateBundleError,
        RawSpec,
        _MAX_GATE_DECODED_NODES,
        _MAX_GATE_DECODED_STRING_BYTES,
        _MAX_GATE_STRUCTURED_BYTES,
        _MAX_JSON_DEPTH,
        _MAX_JSON_NODES,
        _MAX_JSON_STRING_BYTES,
        _MAX_NATIVE_ARTIFACT_BYTES,
        _MAX_RAW_JSON_BYTES,
        _MAX_RAW_DECODED_NODES,
        _MAX_RAW_DECODED_STRING_BYTES,
        _MAX_RAW_NDJSON_BYTES,
        _MAX_RAW_NDJSON_LINE_BYTES,
        _MAX_RAW_NDJSON_ROWS,
        _NATIVE_ROLE_MAX_BYTES,
        _PERSISTENT_CHECKPOINT_ROOT,
        format_utc,
        protected_source_identity_digest,
        _validate_unsealed_gate_evidence,
        _validate_native_execution_receipt,
        validate_gate_attempt,
    )
    from scripts.testing.campaign_source_freeze import SOURCE_FREEZE_SCHEMA_VERSION
    from scripts.testing.campaign_watchdog import (
        ProcessIdentity,
        capture_process_identity,
    )
except ModuleNotFoundError:  # Direct ``python scripts/testing/...`` execution.
    from campaign_gate_bundle import (
        GATE_EVIDENCE_SCHEMA_VERSION,
        GATE_POLICIES,
        GATE_RAW_SPECS,
        CAPTURE_PRODUCER_KIND,
        NATIVE_PRODUCER_KIND,
        NATIVE_EXECUTION_RECEIPT_SCHEMA_VERSION,
        QUALIFICATION_ATTEMPT_SCHEMA_VERSION,
        RAW_ARTIFACT_BINDING_SCHEMA_VERSION,
        RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION,
        GateBundleError,
        RawSpec,
        _MAX_GATE_DECODED_NODES,
        _MAX_GATE_DECODED_STRING_BYTES,
        _MAX_GATE_STRUCTURED_BYTES,
        _MAX_JSON_DEPTH,
        _MAX_JSON_NODES,
        _MAX_JSON_STRING_BYTES,
        _MAX_NATIVE_ARTIFACT_BYTES,
        _MAX_RAW_JSON_BYTES,
        _MAX_RAW_DECODED_NODES,
        _MAX_RAW_DECODED_STRING_BYTES,
        _MAX_RAW_NDJSON_BYTES,
        _MAX_RAW_NDJSON_LINE_BYTES,
        _MAX_RAW_NDJSON_ROWS,
        _NATIVE_ROLE_MAX_BYTES,
        _PERSISTENT_CHECKPOINT_ROOT,
        format_utc,
        protected_source_identity_digest,
        _validate_unsealed_gate_evidence,
        _validate_native_execution_receipt,
        validate_gate_attempt,
    )
    from campaign_source_freeze import SOURCE_FREEZE_SCHEMA_VERSION
    from campaign_watchdog import ProcessIdentity, capture_process_identity


QUALIFICATION_CONTEXT_SCHEMA_VERSION = "hackme.formal-qualification-context.v1"
REHEARSAL_PROJECTION_CONTEXT_SCHEMA_VERSION = (
    "hackme.formal-rehearsal-projection-context.v1"
)
REHEARSAL_PROJECTION_CONTEXT_ENV = "HACKME_FORMAL_REHEARSAL_PROJECTION_CONTEXT"
REHEARSAL_PROJECTION_CONTEXT_SHA256_ENV = (
    "HACKME_FORMAL_REHEARSAL_PROJECTION_CONTEXT_SHA256"
)
_SEALED_MEMFD_PATH = re.compile(r"^/proc/([1-9][0-9]*)/fd/([0-9]+)$")

# JSON must be parsed to add a binding, so it is intentionally small and
# bounded.  JSONL and opaque artifacts are streamed.  The large-stream ceiling
# accommodates multi-gigabyte media/backup authority while still rejecting an
# accidentally unbounded device or sparse-file declaration.
MAX_JSON_BYTES = _MAX_RAW_JSON_BYTES
MAX_NDJSON_BYTES = _MAX_RAW_NDJSON_BYTES
MAX_NDJSON_LINE_BYTES = _MAX_RAW_NDJSON_LINE_BYTES
MAX_NDJSON_RECORDS = _MAX_RAW_NDJSON_ROWS
MAX_STRUCTURED_GATE_BYTES = _MAX_GATE_STRUCTURED_BYTES
MAX_JSON_DEPTH = _MAX_JSON_DEPTH
MAX_JSON_NODES = _MAX_JSON_NODES
MAX_JSON_STRING_BYTES = _MAX_JSON_STRING_BYTES
MAX_RAW_DECODED_NODES = _MAX_RAW_DECODED_NODES
MAX_RAW_DECODED_STRING_BYTES = _MAX_RAW_DECODED_STRING_BYTES
MAX_GATE_DECODED_NODES = _MAX_GATE_DECODED_NODES
MAX_GATE_DECODED_STRING_BYTES = _MAX_GATE_DECODED_STRING_BYTES
MAX_STREAM_ARTIFACT_BYTES = _MAX_NATIVE_ARTIFACT_BYTES
STREAM_ROLE_MAX_BYTES = dict(_NATIVE_ROLE_MAX_BYTES)
COPY_CHUNK_BYTES = 1024 * 1024
MINIMUM_FREE_RESERVE_BYTES = 20 * 1024**3
STRUCTURED_OUTPUT_HEADROOM_BYTES = MAX_STRUCTURED_GATE_BYTES

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUIDISH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")


class QualificationCaptureError(RuntimeError):
    """A capture attempt was rejected or could not be machine-verified."""

    def __init__(self, message: str, *, attempt_root: Path | None = None):
        super().__init__(message)
        self.attempt_root = Path(attempt_root) if attempt_root is not None else None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _precise_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise QualificationCaptureError(message)


def _require_free_space(
    directory: Path,
    *,
    remaining_output_bytes: int,
    label: str,
) -> None:
    usage = os.statvfs(directory)
    available = int(usage.f_bavail * usage.f_frsize)
    required = int(remaining_output_bytes) + MINIMUM_FREE_RESERVE_BYTES
    _require(
        available >= required,
        f"{label} would violate the 20 GiB free-space reserve",
    )


def _validate_json_shape(value: Any, *, label: str) -> tuple[int, int]:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    string_bytes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        _require(nodes <= MAX_JSON_NODES, f"{label} JSON node count exceeds limit")
        _require(depth <= MAX_JSON_DEPTH, f"{label} JSON nesting exceeds limit")
        if isinstance(current, str):
            encoded_length = len(current.encode("utf-8", errors="surrogatepass"))
            _require(
                encoded_length <= MAX_JSON_STRING_BYTES,
                f"{label} JSON string exceeds limit",
            )
            string_bytes += encoded_length
        elif isinstance(current, float):
            _require(math.isfinite(current), f"{label} contains a non-finite number")
        elif isinstance(current, Mapping):
            for key, child in current.items():
                _require(isinstance(key, str), f"{label} JSON object key is not text")
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        else:
            _require(
                current is None or isinstance(current, (bool, int)),
                f"{label} contains a non-JSON value",
            )
    return nodes, string_bytes


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int
    link_count: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            uid=int(value.st_uid),
            gid=int(value.st_gid),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=int(value.st_ctime_ns),
            link_count=int(value.st_nlink),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "mode": stat.S_IMODE(self.mode),
            "uid": self.uid,
            "gid": self.gid,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "link_count": self.link_count,
        }


@dataclass(frozen=True)
class SourceAuthorityIdentity:
    path: Path
    sha256: str
    file: FileIdentity

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.file.size,
            "file_identity": self.file.to_dict(),
        }


def _process_dict(identity: ProcessIdentity) -> dict[str, Any]:
    return {
        "kind": CAPTURE_PRODUCER_KIND,
        "pid": int(identity.pid),
        "start_ticks": int(identity.start_ticks),
        "boot_id": str(identity.boot_id),
        "cgroup_path": str(identity.cgroup_path),
    }


def _same_process(expected: ProcessIdentity, actual: ProcessIdentity) -> bool:
    return bool(
        expected.pid == actual.pid
        and expected.start_ticks == actual.start_ticks
        and expected.boot_id == actual.boot_id
        and expected.cgroup_path == actual.cgroup_path
    )


def _validate_native_identity(identity: FileIdentity, *, label: str) -> None:
    _require(stat.S_ISREG(identity.mode), f"{label} is not a regular file")
    _require(identity.link_count == 1, f"{label} is hard-linked")
    _require(stat.S_IMODE(identity.mode) & 0o022 == 0, f"{label} is group/world writable")
    _require(identity.size >= 0, f"{label} has an invalid size")
    _require(identity.size <= MAX_STREAM_ARTIFACT_BYTES, f"{label} exceeds the reviewed stream ceiling")


def _absolute_path(path: Path | str, *, label: str) -> Path:
    try:
        raw_path = os.fspath(path)
    except TypeError as exc:
        raise QualificationCaptureError(f"{label} must be a filesystem path") from exc
    _require(
        isinstance(raw_path, str) and bool(raw_path),
        f"{label} must be a non-empty path string",
    )
    candidate = Path(raw_path)
    _require(candidate.is_absolute(), f"{label} must be an absolute path")
    _require(
        raw_path == str(candidate),
        f"{label} must use its exact canonical absolute path string",
    )
    try:
        leaf = candidate.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise QualificationCaptureError(
            f"cannot inspect {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    else:
        _require(not stat.S_ISLNK(leaf.st_mode), f"{label} is a symlink")
    try:
        resolved = candidate.resolve(strict=False)
    except Exception as exc:
        raise QualificationCaptureError(
            f"cannot canonicalize {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(
        resolved == candidate,
        f"{label} must use its exact canonical absolute path string",
    )
    return candidate


def _open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _inspect_native(path: Path, *, label: str) -> FileIdentity:
    candidate = _absolute_path(path, label=label)
    try:
        before = os.lstat(candidate)
    except FileNotFoundError as exc:
        raise QualificationCaptureError(f"{label} does not exist") from exc
    if stat.S_ISLNK(before.st_mode):
        raise QualificationCaptureError(f"{label} is a symlink")
    before_identity = FileIdentity.from_stat(before)
    _validate_native_identity(before_identity, label=label)
    descriptor = -1
    try:
        descriptor = os.open(candidate, _open_flags())
        opened_identity = FileIdentity.from_stat(os.fstat(descriptor))
    except OSError as exc:
        raise QualificationCaptureError(
            f"cannot securely open {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _require(opened_identity == before_identity, f"{label} changed between lstat and open")
    return before_identity


def _open_expected(path: Path, expected: FileIdentity, *, label: str) -> BinaryIO:
    current = _inspect_native(path, label=label)
    _require(current == expected, f"{label} changed before snapshot")
    try:
        descriptor = os.open(path, _open_flags())
    except OSError as exc:
        raise QualificationCaptureError(
            f"cannot open {label} for snapshot: {exc.__class__.__name__}: {exc}"
        ) from exc
    opened = FileIdentity.from_stat(os.fstat(descriptor))
    if opened != expected:
        os.close(descriptor)
        raise QualificationCaptureError(f"{label} changed while opening snapshot")
    return os.fdopen(descriptor, "rb", closefd=True)


def _verify_open_and_path(
    path: Path,
    handle: BinaryIO,
    expected: FileIdentity,
    *,
    label: str,
) -> None:
    descriptor_identity = FileIdentity.from_stat(os.fstat(handle.fileno()))
    _require(descriptor_identity == expected, f"{label} changed while being read")
    final = _inspect_native(path, label=label)
    _require(final == expected, f"{label} path identity changed during snapshot")


def _read_bounded_json(
    path: Path,
    expected: FileIdentity,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[dict[str, Any], str, int]:
    _require(expected.size <= maximum_bytes, f"{label} exceeds the bounded JSON limit")
    digest = hashlib.sha256()
    buffer = bytearray()
    with _open_expected(path, expected, label=label) as handle:
        while True:
            chunk = handle.read(min(COPY_CHUNK_BYTES, maximum_bytes + 1 - len(buffer)))
            if not chunk:
                break
            buffer.extend(chunk)
            digest.update(chunk)
            if len(buffer) > maximum_bytes:
                raise QualificationCaptureError(f"{label} exceeds the bounded JSON limit")
        _verify_open_and_path(path, handle, expected, label=label)
    _require(len(buffer) == expected.size, f"{label} size changed during bounded read")
    try:
        payload = json.loads(buffer.decode("utf-8"))
    except Exception as exc:
        raise QualificationCaptureError(
            f"cannot parse {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(isinstance(payload, dict), f"{label} JSON root must be an object")
    _validate_json_shape(payload, label=label)
    return payload, digest.hexdigest(), len(buffer)


def _write_all(handle: BinaryIO, content: bytes) -> None:
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        written = handle.write(view[offset:])
        if written is None:
            written = 0
        if written <= 0:
            raise QualificationCaptureError("private output write made no progress")
        offset += written


def _open_new_output(path: Path) -> BinaryIO:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise QualificationCaptureError(f"capture output already exists: {path}") from exc
    except OSError as exc:
        raise QualificationCaptureError(
            f"cannot create private capture output {path}: {exc.__class__.__name__}: {exc}"
        ) from exc
    return os.fdopen(descriptor, "wb", closefd=True)


def _json_encoder() -> json.JSONEncoder:
    return json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _iter_json_bytes(payload: Mapping[str, Any]):
    for token in _json_encoder().iterencode(dict(payload)):
        yield token.encode("utf-8")
    yield b"\n"


def _encoded_json_identity(payload: Mapping[str, Any]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in _iter_json_bytes(payload):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _write_json_value(path: Path, payload: Mapping[str, Any]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open_new_output(path) as handle:
        for chunk in _iter_json_bytes(payload):
            _write_all(handle, chunk)
            digest.update(chunk)
            size += len(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    _verify_private_output(path, expected_size=size)
    return digest.hexdigest(), size


def _verify_private_output(path: Path, *, expected_size: int) -> FileIdentity:
    try:
        value = os.lstat(path)
    except FileNotFoundError as exc:
        raise QualificationCaptureError(f"captured output disappeared: {path}") from exc
    identity = FileIdentity.from_stat(value)
    _require(stat.S_ISREG(identity.mode), f"captured output is not regular: {path}")
    _require(identity.link_count == 1, f"captured output is hard-linked: {path}")
    _require(identity.uid == os.geteuid(), f"captured output owner changed: {path}")
    _require(stat.S_IMODE(identity.mode) & 0o077 == 0, f"captured output is not private: {path}")
    _require(identity.size == expected_size, f"captured output size mismatch: {path}")
    return identity


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class QualificationContext:
    qualification_campaign_uuid: str
    commit: str
    source_digest: str
    protected_source_digest: str
    source_authority: SourceAuthorityIdentity
    producer: ProcessIdentity
    invocation_id: str
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        qualification_campaign_uuid: str,
        source_authority_path: Path,
        invocation_id: str | None = None,
    ) -> "QualificationContext":
        campaign_uuid = str(qualification_campaign_uuid or "")
        _require(_UUIDISH.fullmatch(campaign_uuid) is not None, "qualification campaign UUID is invalid")
        invocation = str(invocation_id or f"capture:{uuid.uuid4().hex}")
        _require(_UUIDISH.fullmatch(invocation) is not None, "qualification invocation ID is invalid")

        authority_path = _absolute_path(source_authority_path, label="source authority")
        identity = _inspect_native(authority_path, label="source authority")
        payload, authority_sha, _size = _read_bounded_json(
            authority_path,
            identity,
            label="source authority",
            maximum_bytes=MAX_JSON_BYTES,
        )
        _require(
            payload.get("schema_version") == SOURCE_FREEZE_SCHEMA_VERSION,
            "source authority schema is unsupported",
        )
        _require(payload.get("label") == "H0", "source authority must be the H0 capture")
        _require(payload.get("verified") is True, "source authority is not machine verified")
        _require(
            payload.get("require_clean") is True,
            "source authority must be a strict clean H0 capture",
        )
        commit = str(payload.get("commit") or "").lower()
        source_digest = str(payload.get("tracked_content_digest") or "").lower()
        protected_manifest = str(payload.get("protected_ignored_manifest_digest") or "").lower()
        protected_content = str(payload.get("protected_ignored_content_digest") or "").lower()
        _require(_SHA40.fullmatch(commit) is not None, "source authority commit is invalid")
        _require(_SHA256.fullmatch(source_digest) is not None, "source authority digest is invalid")
        _require(_SHA256.fullmatch(protected_manifest) is not None, "protected manifest digest is invalid")
        _require(_SHA256.fullmatch(protected_content) is not None, "protected content digest is invalid")

        producer = capture_process_identity(os.getpid())
        return cls(
            qualification_campaign_uuid=campaign_uuid,
            commit=commit,
            source_digest=source_digest,
            protected_source_digest=protected_source_identity_digest(
                protected_manifest,
                protected_content,
            ),
            source_authority=SourceAuthorityIdentity(
                path=authority_path,
                sha256=authority_sha,
                file=identity,
            ),
            producer=producer,
            invocation_id=invocation,
            created_at=format_utc(utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        producer = _process_dict(self.producer)
        producer["invocation_id"] = self.invocation_id
        return {
            "schema_version": QUALIFICATION_CONTEXT_SCHEMA_VERSION,
            "qualification_campaign_uuid": self.qualification_campaign_uuid,
            "commit": self.commit,
            "source_digest": self.source_digest,
            "protected_source_digest": self.protected_source_digest,
            "source_authority": self.source_authority.to_dict(),
            "producer": producer,
            "created_at": self.created_at,
        }

    def formal_binding(self, *, gate_name: str, artifact_role: str, captured_at: str) -> dict[str, Any]:
        producer = _process_dict(self.producer)
        producer["invocation_id"] = self.invocation_id
        return {
            "schema_version": RAW_ARTIFACT_BINDING_SCHEMA_VERSION,
            "gate_name": gate_name,
            "artifact_role": artifact_role,
            "qualification_campaign_uuid": self.qualification_campaign_uuid,
            "commit": self.commit,
            "source_digest": self.source_digest,
            "protected_source_digest": self.protected_source_digest,
            "actual_execution": True,
            "simulated": False,
            "component_only": False,
            "captured_at": captured_at,
            "producer": producer,
        }


def _assert_qualification_context_live(context: QualificationContext) -> None:
    actual = capture_process_identity(context.producer.pid)
    _require(_same_process(context.producer, actual), "qualification producer identity changed")
    identity = _inspect_native(context.source_authority.path, label="source authority")
    _require(identity == context.source_authority.file, "source authority identity changed")
    payload, digest, _size = _read_bounded_json(
        context.source_authority.path,
        identity,
        label="source authority",
        maximum_bytes=MAX_JSON_BYTES,
    )
    _require(digest == context.source_authority.sha256, "source authority content changed")
    _require(payload.get("commit") == context.commit, "source authority commit changed")
    _require(
        payload.get("tracked_content_digest") == context.source_digest,
        "source authority digest changed",
    )


def _load_native_execution_receipt(
    *,
    context: QualificationContext,
    gate_name: str,
    receipt_path: Path,
    sources: Mapping[str, Path],
    identities: Mapping[str, FileIdentity],
) -> tuple[dict[str, Any], FileIdentity, str]:
    path = _absolute_path(receipt_path, label=f"{gate_name} native execution receipt")
    identity = _inspect_native(path, label=f"{gate_name} native execution receipt")
    payload, digest, _size = _read_bounded_json(
        path,
        identity,
        label=f"{gate_name} native execution receipt",
        maximum_bytes=MAX_JSON_BYTES,
    )
    validated = _validate_native_execution_receipt(
        payload,
        gate=gate_name,
        commit=context.commit,
        source_digest=context.source_digest,
        protected_source_digest=context.protected_source_digest,
        campaign_uuid=context.qualification_campaign_uuid,
        checked_at=utc_now(),
        expected_roles=set(GATE_RAW_SPECS[gate_name]),
    )
    _require(
        validated["source_authority_sha256"] == context.source_authority.sha256,
        f"{gate_name} native execution source authority mismatch",
    )
    producer = validated["producer"]
    _require(producer.get("kind") == NATIVE_PRODUCER_KIND, f"{gate_name} native producer kind mismatch")
    try:
        live = capture_process_identity(int(producer["pid"]))
    except Exception as exc:
        raise QualificationCaptureError(
            f"{gate_name} native producer is not live: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(
        live.pid == producer["pid"]
        and live.start_ticks == producer["start_ticks"]
        and live.boot_id == producer["boot_id"]
        and live.cgroup_path == producer["cgroup_path"],
        f"{gate_name} native producer process identity changed",
    )
    for role in GATE_RAW_SPECS[gate_name]:
        artifact = validated["artifacts"].get(role)
        _require(isinstance(artifact, Mapping), f"{gate_name}.{role} native execution artifact is missing")
        _require(
            artifact.get("path") == str(sources[role])
            and artifact.get("file_identity") == identities[role].to_dict(),
            f"{gate_name}.{role} native execution artifact identity mismatch",
        )
        declared_sha = str(artifact.get("sha256") or "")
        _require(_SHA256.fullmatch(declared_sha) is not None, f"{gate_name}.{role} native SHA is invalid")
    return payload, identity, digest


def project_bound_json_identity(
    *,
    context: QualificationContext,
    gate_name: str,
    role: str,
    native_path: Path,
) -> dict[str, Any]:
    """Project the exact bound JSON hash for a path/hash-linked producer.

    The projection does not create evidence or mutate the native artifact.  A
    producer can use it when a later native receipt must identify the exact
    canonical JSON snapshot that this writer will create.
    """

    _require(gate_name in GATE_RAW_SPECS, f"unknown formal gate: {gate_name}")
    specs = GATE_RAW_SPECS[gate_name]
    _require(role in specs, f"unknown raw role for {gate_name}: {role}")
    spec = specs[role]
    _require(
        spec.media_type == "application/json",
        f"{gate_name}.{role} is not a projectable JSON artifact",
    )
    _assert_qualification_context_live(context)
    source = _absolute_path(native_path, label=f"{gate_name}.{role} native artifact")
    identity = _inspect_native(source, label=f"{gate_name}.{role} native artifact")
    payload, native_sha, native_size = _read_bounded_json(
        source,
        identity,
        label=f"{gate_name}.{role} native JSON projection",
        maximum_bytes=MAX_JSON_BYTES,
    )
    _require("formal_binding" not in payload, f"{gate_name}.{role} is already formally bound")
    _require(
        payload.get("schema_version") == spec.content_schema_version,
        f"{gate_name}.{role} native JSON schema mismatch",
    )
    projected = dict(payload)
    projected["formal_binding"] = context.formal_binding(
        gate_name=gate_name,
        artifact_role=role,
        captured_at=context.created_at,
    )
    _validate_json_shape(projected, label=f"{gate_name}.{role} projected bound JSON")
    sha256, size = _encoded_json_identity(projected)
    _require(size <= MAX_JSON_BYTES, f"{gate_name}.{role} projected JSON exceeds the limit")
    return {
        "gate_name": gate_name,
        "artifact_role": role,
        "captured_at": context.created_at,
        "sha256": sha256,
        "size_bytes": size,
        "native_sha256": native_sha,
        "native_size_bytes": native_size,
    }


@dataclass(frozen=True)
class CapturedArtifact:
    role: str
    native_path: Path
    native_identity: FileIdentity
    native_sha256: str
    captured_path: Path
    captured_sha256: str
    captured_size: int
    reference: Mapping[str, Any]

    def to_attempt_record(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "native_path": str(self.native_path),
            "native_identity": self.native_identity.to_dict(),
            "native_sha256": self.native_sha256,
            "captured_path": str(self.captured_path),
            "captured_sha256": self.captured_sha256,
            "captured_size": self.captured_size,
            "reference": dict(self.reference),
        }


def _suffix_for(role: str, native_path: Path, spec: RawSpec) -> str:
    by_media_type = {
        "application/json": ".json",
        "application/x-ndjson": ".jsonl",
        "application/vnd.apple.mpegurl": ".m3u8",
        "video/mp2t": ".ts",
        "image/png": ".png",
        "application/x-tar": ".tar",
        "application/vnd.sqlite3": ".sqlite3",
        "text/plain": ".txt",
    }
    if spec.media_type in by_media_type:
        return by_media_type[spec.media_type]
    suffix = native_path.suffix
    if suffix and re.fullmatch(r"\.[A-Za-z0-9_.-]{1,32}", suffix):
        return suffix
    return ".bin"


def planned_capture_path(
    attempt_root: Path,
    *,
    gate_name: str,
    role: str,
    native_path: Path | str,
    qualification_campaign_uuid: str | None = None,
) -> Path:
    """Return the deterministic raw destination for a role without creating it."""

    _require(gate_name in GATE_RAW_SPECS, f"unknown formal gate: {gate_name}")
    specs = GATE_RAW_SPECS[gate_name]
    _require(role in specs, f"unknown raw role for {gate_name}: {role}")
    root = _absolute_path(attempt_root, label="attempt root")
    source = _absolute_path(
        native_path,
        label=f"{gate_name}.{role} native artifact",
    )
    if gate_name == "checkpoint_recovery_verified" and role == "checkpoint_mirror":
        campaign_uuid = str(qualification_campaign_uuid or "")
        _require(
            _UUIDISH.fullmatch(campaign_uuid) is not None,
            "qualification campaign UUID is required for the persistent checkpoint snapshot",
        )
        attempt_key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
        return (
            Path(_PERSISTENT_CHECKPOINT_ROOT)
            / campaign_uuid
            / "qualification_snapshots"
            / attempt_key
            / "checkpoint_mirror.json"
        )
    return root / "raw" / f"{role}{_suffix_for(role, source, specs[role])}"


def planned_capture_paths(
    attempt_root: Path,
    *,
    gate_name: str,
    native_artifact_paths: Mapping[str, Path | str],
    qualification_campaign_uuid: str,
) -> dict[str, Path]:
    """Return the exact paths native producers must reference in receipts.

    This is a pure planning API: it neither creates the attempt nor rewrites a
    receipt.  Path-linked producers must emit their native authority with these
    final paths before calling :func:`capture_gate_evidence`; JSON hash links
    use :func:`project_bound_json_identity`.
    """

    _require(gate_name in GATE_RAW_SPECS, f"unknown formal gate: {gate_name}")
    specs = GATE_RAW_SPECS[gate_name]
    _require(
        set(native_artifact_paths) == set(specs),
        f"{gate_name} native artifact role set mismatch",
    )
    return {
        role: planned_capture_path(
            attempt_root,
            gate_name=gate_name,
            role=role,
            native_path=native_artifact_paths[role],
            qualification_campaign_uuid=qualification_campaign_uuid,
        )
        for role in specs
    }


def _rehearsal_scenario_ids() -> tuple[str, ...]:
    gate = "60_minute_rehearsal_passed"
    return tuple(
        role.removeprefix("scenario_")
        for role in GATE_RAW_SPECS[gate]
        if role.startswith("scenario_")
        and not role.startswith("scenario_bundle_")
        and not role.startswith("scenario_archive_")
    )


def validate_rehearsal_projection_context(
    value: object,
    *,
    require_live_producer: bool = True,
) -> dict[str, Any]:
    """Validate the non-secret, parent-sealed rehearsal projection contract.

    The context is transport authority only.  It cannot declare PASS and it
    contains no credentials.  Its capture bindings and all 41 native/planned
    paths are exact so a child cannot redirect a later qualification capture.
    """

    _require(isinstance(value, Mapping), "rehearsal projection context is not an object")
    payload = dict(value)
    expected_fields = {
        "schema_version",
        "gate_name",
        "outer_native_invocation_id",
        "activation_nonce",
        "campaign_attempt_uuid",
        "capture_context",
        "native_artifact_paths",
        "planned_capture_paths",
        "formal_bindings",
        "scenario_authorities",
    }
    _require(
        set(payload) == expected_fields,
        "rehearsal projection context shape mismatch",
    )
    gate = "60_minute_rehearsal_passed"
    _require(
        payload.get("schema_version") == REHEARSAL_PROJECTION_CONTEXT_SCHEMA_VERSION
        and payload.get("gate_name") == gate,
        "rehearsal projection context schema/gate mismatch",
    )
    outer_invocation = str(payload.get("outer_native_invocation_id") or "")
    activation_nonce = str(payload.get("activation_nonce") or "")
    campaign_attempt = str(payload.get("campaign_attempt_uuid") or "")
    _require(
        _UUIDISH.fullmatch(outer_invocation) is not None
        and _UUIDISH.fullmatch(activation_nonce) is not None
        and _UUIDISH.fullmatch(campaign_attempt) is not None,
        "rehearsal projection invocation/attempt authority is invalid",
    )

    capture_context = payload.get("capture_context")
    _require(isinstance(capture_context, Mapping), "projection capture context is missing")
    _require(
        set(capture_context)
        == {
            "schema_version",
            "qualification_campaign_uuid",
            "commit",
            "source_digest",
            "protected_source_digest",
            "source_authority",
            "producer",
            "created_at",
        }
        and capture_context.get("schema_version") == QUALIFICATION_CONTEXT_SCHEMA_VERSION,
        "projection capture context shape/schema mismatch",
    )
    _require(
        _UUIDISH.fullmatch(str(capture_context.get("qualification_campaign_uuid") or ""))
        is not None
        and _SHA40.fullmatch(str(capture_context.get("commit") or "")) is not None
        and _SHA256.fullmatch(str(capture_context.get("source_digest") or "")) is not None
        and _SHA256.fullmatch(str(capture_context.get("protected_source_digest") or ""))
        is not None,
        "projection capture source/campaign identity is invalid",
    )
    producer = capture_context.get("producer")
    _require(
        isinstance(producer, Mapping)
        and set(producer)
        == {"kind", "pid", "start_ticks", "boot_id", "cgroup_path", "invocation_id"}
        and producer.get("kind") == CAPTURE_PRODUCER_KIND
        and type(producer.get("pid")) is int
        and int(producer.get("pid") or 0) > 0
        and type(producer.get("start_ticks")) is int
        and int(producer.get("start_ticks") or 0) > 0
        and _UUIDISH.fullmatch(str(producer.get("invocation_id") or "")) is not None,
        "projection capture producer identity is invalid",
    )
    if require_live_producer:
        try:
            live = capture_process_identity(int(producer["pid"]))
        except Exception as exc:
            raise QualificationCaptureError(
                f"projection capture producer is not live: {exc.__class__.__name__}: {exc}"
            ) from exc
        _require(
            live.pid == producer.get("pid")
            and live.start_ticks == producer.get("start_ticks")
            and live.boot_id == producer.get("boot_id")
            and live.cgroup_path == producer.get("cgroup_path"),
            "projection capture producer identity changed",
        )

    role_set = set(GATE_RAW_SPECS[gate])
    native_paths = payload.get("native_artifact_paths")
    planned_paths = payload.get("planned_capture_paths")
    _require(
        isinstance(native_paths, Mapping)
        and isinstance(planned_paths, Mapping)
        and set(native_paths) == role_set
        and set(planned_paths) == role_set
        and len(role_set) == 41,
        "rehearsal projection role inventory is not exactly 41",
    )
    for role in sorted(role_set):
        native = _absolute_path(native_paths[role], label=f"projection native path {role}")
        planned = _absolute_path(planned_paths[role], label=f"projection planned path {role}")
        _require(native != planned, f"projection native/planned paths alias: {role}")

    json_roles = {
        role
        for role, spec in GATE_RAW_SPECS[gate].items()
        if spec.media_type == "application/json"
    }
    bindings = payload.get("formal_bindings")
    _require(
        isinstance(bindings, Mapping) and set(bindings) == json_roles,
        "rehearsal projection formal binding role set mismatch",
    )
    for role in sorted(json_roles):
        expected_binding = {
            "schema_version": RAW_ARTIFACT_BINDING_SCHEMA_VERSION,
            "gate_name": gate,
            "artifact_role": role,
            "qualification_campaign_uuid": capture_context["qualification_campaign_uuid"],
            "commit": capture_context["commit"],
            "source_digest": capture_context["source_digest"],
            "protected_source_digest": capture_context["protected_source_digest"],
            "actual_execution": True,
            "simulated": False,
            "component_only": False,
            "captured_at": capture_context["created_at"],
            "producer": dict(producer),
        }
        _require(
            isinstance(bindings[role], Mapping)
            and dict(bindings[role]) == expected_binding,
            f"rehearsal projection formal binding mismatch: {role}",
        )

    scenario_ids = _rehearsal_scenario_ids()
    scenario_authorities = payload.get("scenario_authorities")
    _require(
        isinstance(scenario_authorities, Mapping)
        and set(scenario_authorities) == set(scenario_ids),
        "rehearsal projection scenario authority inventory mismatch",
    )
    attempts: set[str] = set()
    invocations: set[str] = set()
    for scenario_id in scenario_ids:
        authority = scenario_authorities[scenario_id]
        _require(
            isinstance(authority, Mapping)
            and set(authority) == {"scenario_attempt_uuid", "native_invocation_id"},
            f"rehearsal projection scenario authority shape mismatch: {scenario_id}",
        )
        attempt = str(authority.get("scenario_attempt_uuid") or "")
        invocation = str(authority.get("native_invocation_id") or "")
        _require(
            _UUIDISH.fullmatch(attempt) is not None
            and _UUIDISH.fullmatch(invocation) is not None
            and attempt not in attempts
            and invocation not in invocations
            and invocation != outer_invocation,
            f"rehearsal projection scenario attempt/invocation is invalid or reused: {scenario_id}",
        )
        attempts.add(attempt)
        invocations.add(invocation)
    return payload


def build_rehearsal_projection_context(
    *,
    context: QualificationContext,
    attempt_root: Path,
    native_artifact_paths: Mapping[str, Path],
    outer_native_invocation_id: str,
    activation_nonce: str,
    campaign_attempt_uuid: str,
    scenario_authorities: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """Build the exact non-secret projection plan before the child starts."""

    gate = "60_minute_rehearsal_passed"
    _assert_qualification_context_live(context)
    planned = planned_capture_paths(
        attempt_root,
        gate_name=gate,
        native_artifact_paths=native_artifact_paths,
        qualification_campaign_uuid=context.qualification_campaign_uuid,
    )
    payload = {
        "schema_version": REHEARSAL_PROJECTION_CONTEXT_SCHEMA_VERSION,
        "gate_name": gate,
        "outer_native_invocation_id": str(outer_native_invocation_id),
        "activation_nonce": str(activation_nonce),
        "campaign_attempt_uuid": str(campaign_attempt_uuid),
        "capture_context": context.to_dict(),
        "native_artifact_paths": {
            role: str(Path(path)) for role, path in native_artifact_paths.items()
        },
        "planned_capture_paths": {
            role: str(path) for role, path in planned.items()
        },
        "formal_bindings": {
            role: context.formal_binding(
                gate_name=gate,
                artifact_role=role,
                captured_at=context.created_at,
            )
            for role, spec in GATE_RAW_SPECS[gate].items()
            if spec.media_type == "application/json"
        },
        "scenario_authorities": {
            scenario_id: dict(authority)
            for scenario_id, authority in scenario_authorities.items()
        },
    }
    return validate_rehearsal_projection_context(payload)


def encoded_rehearsal_projection_context(payload: Mapping[str, Any]) -> bytes:
    """Return the canonical bytes written into the sealed parent memfd."""

    validated = validate_rehearsal_projection_context(payload)
    return b"".join(_iter_json_bytes(validated))


def read_sealed_rehearsal_projection_context(
    locator: str,
    expected_sha256: str,
) -> dict[str, Any]:
    """Read a live parent's immutable memfd without inheriting its descriptor."""

    match = _SEALED_MEMFD_PATH.fullmatch(str(locator or ""))
    _require(match is not None, "rehearsal projection locator is not a parent memfd path")
    _require(
        _SHA256.fullmatch(str(expected_sha256 or "")) is not None,
        "rehearsal projection digest is invalid",
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(locator), flags)
    except OSError as exc:
        raise QualificationCaptureError(
            f"cannot open sealed rehearsal projection: {exc.__class__.__name__}: {exc}"
        ) from exc
    try:
        identity = FileIdentity.from_stat(os.fstat(descriptor))
        _require(stat.S_ISREG(identity.mode), "rehearsal projection memfd is not regular")
        _require(identity.uid == os.geteuid(), "rehearsal projection memfd owner mismatch")
        _require(0 < identity.size <= MAX_JSON_BYTES, "rehearsal projection size is invalid")
        required_seals = (
            getattr(fcntl, "F_SEAL_SEAL", 0x0001)
            | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
            | getattr(fcntl, "F_SEAL_GROW", 0x0004)
            | getattr(fcntl, "F_SEAL_WRITE", 0x0008)
        )
        actual_seals = int(fcntl.fcntl(descriptor, fcntl.F_GET_SEALS))
        _require(
            actual_seals & required_seals == required_seals,
            "rehearsal projection memfd is not fully sealed",
        )
        content = bytearray()
        while len(content) <= MAX_JSON_BYTES:
            chunk = os.read(descriptor, min(COPY_CHUNK_BYTES, MAX_JSON_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        _require(len(content) == identity.size, "rehearsal projection changed while reading")
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(content).hexdigest()
    _require(digest == expected_sha256, "rehearsal projection digest mismatch")
    try:
        decoded = json.loads(bytes(content))
    except Exception as exc:
        raise QualificationCaptureError(
            f"rehearsal projection JSON is invalid: {exc.__class__.__name__}: {exc}"
        ) from exc
    payload = validate_rehearsal_projection_context(decoded)
    producer_pid = int(payload["capture_context"]["producer"]["pid"])
    _require(
        int(match.group(1)) == producer_pid,
        "rehearsal projection locator is not owned by the capture producer",
    )
    return payload


def _require_declared_path(
    payload: Mapping[str, Any],
    keys: Sequence[str],
    expected: Path,
    *,
    label: str,
) -> None:
    value: Any = payload
    for key in keys:
        _require(isinstance(value, Mapping) and key in value, f"{label} is missing")
        value = value[key]
    _require(isinstance(value, str) and value, f"{label} is not a path string")
    declared = _absolute_path(value, label=label)
    planned = _absolute_path(expected, label=f"{label} planned path")
    _require(
        declared == planned and value == str(planned),
        f"{label} must be producer-bound to planned raw path {planned}, got {value}",
    )


def _bounded_native_bytes(
    path: Path,
    identity: FileIdentity,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    _require(identity.size <= maximum_bytes, f"{label} exceeds its bounded size limit")
    content = bytearray()
    with _open_expected(path, identity, label=label) as handle:
        while True:
            chunk = handle.read(min(COPY_CHUNK_BYTES, maximum_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            _require(len(content) <= maximum_bytes, f"{label} exceeds its bounded size limit")
        _verify_open_and_path(path, handle, identity, label=label)
    _require(len(content) == identity.size, f"{label} changed during bounded read")
    return bytes(content)


def _native_stream_sha256(
    path: Path,
    identity: FileIdentity,
    *,
    label: str,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open_expected(path, identity, label=label) as handle:
        while True:
            chunk = handle.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        _verify_open_and_path(path, handle, identity, label=label)
    _require(size == identity.size, f"{label} size changed during hash")
    return digest.hexdigest(), size


def _native_json_for_path_contract(
    *,
    gate_name: str,
    role: str,
    source: Path,
    identity: FileIdentity,
) -> dict[str, Any]:
    payload, _digest, _size = _read_bounded_json(
        source,
        identity,
        label=f"{gate_name}.{role} native path authority",
        maximum_bytes=MAX_JSON_BYTES,
    )
    return payload


def _require_projected_json_link(
    *,
    context: QualificationContext,
    gate_name: str,
    source_role: str,
    source: Path,
    destination: Path,
    value: object,
    label: str,
) -> None:
    _require(isinstance(value, Mapping), f"{label} is not an artifact link")
    _require(
        set(value) == {"path", "sha256", "size_bytes"},
        f"{label} shape mismatch",
    )
    _require_declared_path(
        {"link": value},
        ("link", "path"),
        destination,
        label=f"{label}.path",
    )
    projection = project_bound_json_identity(
        context=context,
        gate_name=gate_name,
        role=source_role,
        native_path=source,
    )
    _require(
        value.get("sha256") == projection["sha256"]
        and value.get("size_bytes") == projection["size_bytes"],
        f"{label} hash/size must match projected bound raw authority",
    )


def _validate_worktree_path_contract(
    *,
    sources: Mapping[str, Path],
    identities: Mapping[str, FileIdentity],
    destinations: Mapping[str, Path],
) -> None:
    source = _native_json_for_path_contract(
        gate_name="worktree_clean_and_frozen",
        role="source_h0",
        source=sources["source_h0"],
        identity=identities["source_h0"],
    )
    for source_name, role in (
        ("git_status", "git_status"),
        ("git_diff_binary", "git_diff_binary"),
        ("git_ls_files", "git_ls_files"),
        ("git_submodule_status", "git_submodule_status"),
        ("tracked_manifest", "tracked_manifest"),
        ("protected_ignored_manifest", "protected_ignored_manifest"),
    ):
        _require_declared_path(
            source,
            ("artifacts", source_name),
            destinations[role],
            label=f"worktree source_h0.artifacts.{source_name}",
        )


def _validate_dependency_path_contract(
    *,
    context: QualificationContext,
    sources: Mapping[str, Path],
    identities: Mapping[str, FileIdentity],
    destinations: Mapping[str, Path],
) -> None:
    gate = "all_mandatory_dependencies_verified"
    direct_contracts: tuple[tuple[str, tuple[str, ...], str], ...] = (
        ("hls_ffprobe", ("input_path",), "hls_playlist"),
        ("bt_receipt", ("evidence", "download_path"), "bt_payload"),
        ("bt_receipt", ("evidence", "trace_path"), "bt_protocol_trace"),
        ("bt_protocol_trace", ("payload_path",), "bt_payload"),
        ("comfyui_receipt", ("evidence", "output_path"), "comfyui_output"),
        ("comfyui_receipt", ("evidence", "history_path"), "comfyui_history"),
        ("comfyui_history", ("output_path",), "comfyui_output"),
        ("ai_receipt", ("evidence", "exchange_path"), "ai_provider_exchange"),
        ("backup_receipt", ("evidence", "archive_path"), "backup_archive"),
        ("backup_receipt", ("evidence", "manifest_path"), "backup_restore_manifest"),
        ("backup_receipt", ("evidence", "quick_check_path"), "backup_sqlite_check"),
        ("backup_restore_manifest", ("archive_path",), "backup_archive"),
        (
            "backup_restore_manifest",
            ("restored_database_path",),
            "backup_restored_database",
        ),
        ("backup_sqlite_check", ("database_path",), "backup_restored_database"),
        ("security_receipt", ("evidence", "request_trace_path"), "security_requests"),
        ("security_receipt", ("evidence", "audit_chain_path"), "security_audit_chain"),
    )
    by_role: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for role, keys, destination_role in direct_contracts:
        by_role.setdefault(role, []).append((keys, destination_role))
    for role, contracts in by_role.items():
        payload = _native_json_for_path_contract(
            gate_name=gate,
            role=role,
            source=sources[role],
            identity=identities[role],
        )
        for keys, destination_role in contracts:
            _require_declared_path(
                payload,
                keys,
                destinations[destination_role],
                label=f"{gate}.{role}.{'/'.join(keys)}",
            )

    preflight = _native_json_for_path_contract(
        gate_name=gate,
        role="dependency_preflight",
        source=sources["dependency_preflight"],
        identity=identities["dependency_preflight"],
    )
    checks = preflight.get("checks")
    _require(isinstance(checks, list), "dependency preflight path authority checks are missing")
    by_name = {
        str(item.get("name") or ""): item
        for item in checks
        if isinstance(item, Mapping)
    }
    for engine in ("chromium", "firefox", "webkit"):
        role = f"browser_{engine}_launch"
        check = by_name.get(f"browser_{engine}", {})
        _require_declared_path(
            check,
            ("details", "evidence", "raw_authority_path"),
            destinations[role],
            label=f"dependency preflight browser_{engine} raw authority path",
        )
        evidence = (
            check.get("details", {}).get("evidence", {})
            if isinstance(check, Mapping)
            and isinstance(check.get("details"), Mapping)
            else {}
        )
        projection = project_bound_json_identity(
            context=context,
            gate_name=gate,
            role=role,
            native_path=sources[role],
        )
        _require(
            isinstance(evidence, Mapping)
            and evidence.get("raw_authority_sha256") == projection["sha256"],
            f"dependency preflight browser_{engine} SHA must match the projected bound raw authority",
        )
    for keys, destination_role in (
        (("playlist",), "hls_playlist"),
        (("segment_path",), "hls_segment"),
        (("ffprobe_path",), "hls_ffprobe"),
    ):
        _require_declared_path(
            by_name.get("ffmpeg_hls", {}),
            ("details", "evidence", *keys),
            destinations[destination_role],
            label=f"dependency preflight ffmpeg_hls {keys[0]}",
        )
    external_contracts: tuple[tuple[str, tuple[str, ...], str], ...] = (
        ("bt_seed_download", ("download_path",), "bt_payload"),
        ("bt_seed_download", ("trace_path",), "bt_protocol_trace"),
        ("comfyui_terminal", ("output_path",), "comfyui_output"),
        ("comfyui_terminal", ("history_path",), "comfyui_history"),
        ("ai_provider_terminal", ("exchange_path",), "ai_provider_exchange"),
        ("backup_restore", ("archive_path",), "backup_archive"),
        ("backup_restore", ("manifest_path",), "backup_restore_manifest"),
        ("backup_restore", ("quick_check_path",), "backup_sqlite_check"),
        ("production_security_sentinel", ("request_trace_path",), "security_requests"),
        ("production_security_sentinel", ("audit_chain_path",), "security_audit_chain"),
    )
    for check_name, keys, destination_role in external_contracts:
        _require_declared_path(
            by_name.get(check_name, {}),
            ("details", "evidence", "evidence", *keys),
            destinations[destination_role],
            label=f"dependency preflight {check_name} {'/'.join(keys)}",
        )

    playlist_bytes = _bounded_native_bytes(
        sources["hls_playlist"],
        identities["hls_playlist"],
        label="dependency native HLS playlist",
        maximum_bytes=4 * 1024 * 1024,
    )
    try:
        playlist_text = playlist_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise QualificationCaptureError("dependency native HLS playlist is not UTF-8") from exc
    segment_uris = [
        line.strip()
        for line in playlist_text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    _require(
        len(segment_uris) == 1,
        "dependency native HLS playlist must contain one planned segment URI",
    )
    declared_segment = (
        destinations["hls_playlist"].parent / segment_uris[0]
    ).resolve(strict=False)
    _require(
        declared_segment == destinations["hls_segment"].resolve(strict=False),
        "dependency native HLS playlist is not producer-bound to the planned raw segment",
    )


def _validate_rehearsal_path_contract(
    *,
    context: QualificationContext,
    sources: Mapping[str, Path],
    identities: Mapping[str, FileIdentity],
    destinations: Mapping[str, Path],
) -> None:
    """Bind supervisor -> runner -> receipt -> bundle -> sealed archive pre-copy."""

    gate = "60_minute_rehearsal_passed"
    scenario_ids = tuple(
        role.removeprefix("scenario_")
        for role in GATE_RAW_SPECS[gate]
        if role.startswith("scenario_")
        and not role.startswith("scenario_bundle_")
        and not role.startswith("scenario_archive_")
    )
    supervisor = _native_json_for_path_contract(
        gate_name=gate,
        role="supervisor_result",
        source=sources["supervisor_result"],
        identity=identities["supervisor_result"],
    )
    runner = _native_json_for_path_contract(
        gate_name=gate,
        role="runner_result",
        source=sources["runner_result"],
        identity=identities["runner_result"],
    )
    _require_projected_json_link(
        context=context,
        gate_name=gate,
        source_role="runner_result",
        source=sources["runner_result"],
        destination=destinations["runner_result"],
        value=supervisor.get("runner_report"),
        label="rehearsal supervisor runner report",
    )
    authority_fields = (
        "qualification_campaign_uuid",
        "campaign_uuid",
        "campaign_attempt_uuid",
        "native_invocation_id",
        "commit",
        "source_digest",
        "protected_source_digest",
    )
    scenario_common_fields = tuple(
        field for field in authority_fields if field != "native_invocation_id"
    )
    supervisor_identity = {field: supervisor.get(field) for field in authority_fields}
    runner_identity = {field: runner.get(field) for field in authority_fields}
    _require(
        supervisor_identity == runner_identity,
        "rehearsal supervisor/runner authority mismatch before capture",
    )
    _require(
        runner_identity["qualification_campaign_uuid"]
        == context.qualification_campaign_uuid
        and runner_identity["commit"] == context.commit
        and runner_identity["source_digest"] == context.source_digest
        and runner_identity["protected_source_digest"]
        == context.protected_source_digest,
        "rehearsal producer authority differs from qualification context",
    )
    runner_index = runner.get("scenario_receipts")
    _require(
        isinstance(runner_index, Mapping)
        and set(runner_index) == set(scenario_ids),
        "rehearsal runner scenario receipt inventory role set mismatch",
    )
    scenario_attempts: set[str] = set()
    scenario_invocations: set[str] = set()
    for scenario_id in scenario_ids:
        receipt_role = f"scenario_{scenario_id}"
        bundle_role = f"scenario_bundle_{scenario_id}"
        archive_role = f"scenario_archive_{scenario_id}"
        receipt = _native_json_for_path_contract(
            gate_name=gate,
            role=receipt_role,
            source=sources[receipt_role],
            identity=identities[receipt_role],
        )
        bundle = _native_json_for_path_contract(
            gate_name=gate,
            role=bundle_role,
            source=sources[bundle_role],
            identity=identities[bundle_role],
        )
        authority = receipt.get("authority")
        _require(
            isinstance(authority, Mapping),
            f"rehearsal scenario authority is missing: {scenario_id}",
        )
        _require(
            {field: authority.get(field) for field in scenario_common_fields}
            == {field: runner_identity[field] for field in scenario_common_fields},
            f"rehearsal scenario authority mismatch before capture: {scenario_id}",
        )
        attempt_uuid = str(authority.get("scenario_attempt_uuid") or "")
        _require(
            _UUIDISH.fullmatch(attempt_uuid) is not None
            and attempt_uuid not in scenario_attempts,
            f"rehearsal scenario attempt is invalid or reused: {scenario_id}",
        )
        scenario_attempts.add(attempt_uuid)
        scenario_invocation = str(authority.get("native_invocation_id") or "")
        _require(
            _UUIDISH.fullmatch(scenario_invocation) is not None
            and scenario_invocation != runner_identity["native_invocation_id"]
            and scenario_invocation not in scenario_invocations,
            f"rehearsal scenario invocation is invalid or reused: {scenario_id}",
        )
        scenario_invocations.add(scenario_invocation)
        _require(
            isinstance(bundle.get("authority"), Mapping)
            and dict(bundle["authority"]) == dict(authority),
            f"rehearsal receipt/bundle authority mismatch before capture: {scenario_id}",
        )
        index = runner_index.get(scenario_id)
        _require(
            isinstance(index, Mapping)
            and set(index)
            == {
                "scenario_attempt_uuid",
                "native_invocation_id",
                "receipt",
                "artifact_bundle",
                "artifact_archive",
            },
            f"rehearsal runner scenario index shape mismatch: {scenario_id}",
        )
        _require(
            index.get("scenario_attempt_uuid") == attempt_uuid,
            f"rehearsal runner scenario attempt mismatch: {scenario_id}",
        )
        _require(
            index.get("native_invocation_id") == scenario_invocation,
            f"rehearsal runner scenario invocation mismatch: {scenario_id}",
        )
        _require_projected_json_link(
            context=context,
            gate_name=gate,
            source_role=receipt_role,
            source=sources[receipt_role],
            destination=destinations[receipt_role],
            value=index.get("receipt"),
            label=f"rehearsal runner scenario receipt {scenario_id}",
        )
        _require_projected_json_link(
            context=context,
            gate_name=gate,
            source_role=bundle_role,
            source=sources[bundle_role],
            destination=destinations[bundle_role],
            value=index.get("artifact_bundle"),
            label=f"rehearsal runner scenario bundle {scenario_id}",
        )
        receipt_bundle = receipt.get("artifact_bundle")
        _require(
            isinstance(receipt_bundle, Mapping),
            f"rehearsal scenario concrete bundle reference missing: {scenario_id}",
        )
        _require_projected_json_link(
            context=context,
            gate_name=gate,
            source_role=bundle_role,
            source=sources[bundle_role],
            destination=destinations[bundle_role],
            value={
                "path": receipt_bundle.get("path"),
                "sha256": receipt_bundle.get("sha256"),
                "size_bytes": receipt_bundle.get("size_bytes"),
            },
            label=f"rehearsal receipt concrete bundle {scenario_id}",
        )
        archive_sha, archive_size = _native_stream_sha256(
            sources[archive_role],
            identities[archive_role],
            label=f"rehearsal scenario archive {scenario_id}",
        )
        archive_link = index.get("artifact_archive")
        _require(
            isinstance(archive_link, Mapping)
            and set(archive_link) == {"path", "sha256", "size_bytes"},
            f"rehearsal runner scenario archive link shape mismatch: {scenario_id}",
        )
        _require_declared_path(
            {"link": archive_link},
            ("link", "path"),
            destinations[archive_role],
            label=f"rehearsal runner scenario archive {scenario_id}.path",
        )
        _require(
            archive_link.get("sha256") == archive_sha
            and archive_link.get("size_bytes") == archive_size,
            f"rehearsal runner scenario archive hash/size mismatch: {scenario_id}",
        )
        bundle_archive = bundle.get("artifact_archive")
        _require(
            isinstance(bundle_archive, Mapping),
            f"rehearsal bundle archive reference missing: {scenario_id}",
        )
        _require_declared_path(
            {"archive": bundle_archive},
            ("archive", "path"),
            destinations[archive_role],
            label=f"rehearsal bundle archive {scenario_id}.path",
        )
        _require(
            bundle_archive.get("sha256") == archive_sha
            and bundle_archive.get("size_bytes") == archive_size
            and receipt_bundle.get("artifact_archive_sha256") == archive_sha
            and receipt_bundle.get("artifact_archive_size_bytes") == archive_size,
            f"rehearsal bundle/archive identity mismatch before capture: {scenario_id}",
        )


def _validate_gate_path_contract(
    *,
    context: QualificationContext,
    gate_name: str,
    sources: Mapping[str, Path],
    identities: Mapping[str, FileIdentity],
    destinations: Mapping[str, Path],
) -> None:
    if gate_name == "worktree_clean_and_frozen":
        _validate_worktree_path_contract(
            sources=sources,
            identities=identities,
            destinations=destinations,
        )
    elif gate_name == "all_mandatory_dependencies_verified":
        _validate_dependency_path_contract(
            context=context,
            sources=sources,
            identities=identities,
            destinations=destinations,
        )
    elif gate_name == "60_minute_rehearsal_passed":
        _validate_rehearsal_path_contract(
            context=context,
            sources=sources,
            identities=identities,
            destinations=destinations,
        )


def _require_private_directory(path: Path, *, label: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise QualificationCaptureError(f"{label} does not exist: {path}") from exc
    _require(stat.S_ISDIR(info.st_mode), f"{label} is not a directory: {path}")
    _require(not stat.S_ISLNK(info.st_mode), f"{label} is a symlink: {path}")
    _require(info.st_uid == os.geteuid(), f"{label} owner mismatch: {path}")
    _require(info.st_mode & 0o022 == 0, f"{label} is group/world writable: {path}")


def _prepare_checkpoint_destination(destination: Path) -> None:
    root = Path(_PERSISTENT_CHECKPOINT_ROOT)
    _require(root.is_absolute(), "persistent checkpoint root is not absolute")
    _require_private_directory(root, label="persistent checkpoint root")
    _require(
        destination != root and destination.is_relative_to(root),
        "checkpoint snapshot destination escapes the persistent root",
    )
    current = root
    relative_parent = destination.parent.relative_to(root)
    for part in relative_parent.parts:
        current = current / part
        try:
            os.mkdir(current, 0o700)
            _fsync_directory(current.parent)
        except FileExistsError:
            pass
        _require_private_directory(current, label="persistent checkpoint snapshot directory")
    try:
        os.lstat(destination)
    except FileNotFoundError:
        return
    raise QualificationCaptureError(f"persistent checkpoint snapshot already exists: {destination}")


class QualificationCaptureWriter:
    """One-shot fail-closed writer for one gate qualification attempt."""

    def __init__(self, *, context: QualificationContext, attempt_root: Path):
        self.context = context
        self.attempt_root = _absolute_path(attempt_root, label="attempt root")
        self.raw_root = self.attempt_root / "raw"
        self.evidence_root = self.attempt_root / "evidence"
        self._prepared = False
        self._captured: list[CapturedArtifact] = []
        self._structured_output_bytes = 0
        self._native_decoded_nodes = 0
        self._native_decoded_string_bytes = 0
        self._bound_decoded_nodes = 0
        self._bound_decoded_string_bytes = 0

    def _check_decoded_budget(
        self,
        *,
        gate_name: str,
        role: str,
        native_nodes: int,
        native_string_bytes: int,
        bound_nodes: int,
        bound_string_bytes: int,
    ) -> None:
        _require(
            native_nodes <= MAX_RAW_DECODED_NODES
            and bound_nodes <= MAX_RAW_DECODED_NODES,
            f"{gate_name}.{role} decoded node budget exceeds the raw artifact limit",
        )
        _require(
            native_string_bytes <= MAX_RAW_DECODED_STRING_BYTES
            and bound_string_bytes <= MAX_RAW_DECODED_STRING_BYTES,
            f"{gate_name}.{role} decoded string budget exceeds the raw artifact limit",
        )
        _require(
            self._native_decoded_nodes + native_nodes <= MAX_GATE_DECODED_NODES
            and self._bound_decoded_nodes + bound_nodes <= MAX_GATE_DECODED_NODES,
            f"{gate_name} native/bound whole-gate decoded node budget exceeded",
        )
        _require(
            self._native_decoded_string_bytes + native_string_bytes
            <= MAX_GATE_DECODED_STRING_BYTES
            and self._bound_decoded_string_bytes + bound_string_bytes
            <= MAX_GATE_DECODED_STRING_BYTES,
            f"{gate_name} native/bound whole-gate decoded string budget exceeded",
        )

    def _commit_decoded_budget(
        self,
        *,
        native_nodes: int,
        native_string_bytes: int,
        bound_nodes: int,
        bound_string_bytes: int,
    ) -> None:
        self._native_decoded_nodes += native_nodes
        self._native_decoded_string_bytes += native_string_bytes
        self._bound_decoded_nodes += bound_nodes
        self._bound_decoded_string_bytes += bound_string_bytes

    def _prepare_attempt(self) -> None:
        parent = self.attempt_root.parent
        try:
            parent_stat = os.lstat(parent)
        except FileNotFoundError as exc:
            raise QualificationCaptureError(f"attempt parent does not exist: {parent}") from exc
        _require(stat.S_ISDIR(parent_stat.st_mode), "attempt parent is not a directory")
        _require(not stat.S_ISLNK(parent_stat.st_mode), "attempt parent is a symlink")
        try:
            os.lstat(self.attempt_root)
        except FileNotFoundError:
            pass
        else:
            raise QualificationCaptureError(f"attempt root already exists: {self.attempt_root}")
        try:
            os.mkdir(self.attempt_root, 0o700)
            self._prepared = True
            os.mkdir(self.raw_root, 0o700)
            os.mkdir(self.evidence_root, 0o700)
        except Exception as exc:
            raise QualificationCaptureError(
                f"cannot create private attempt root: {exc.__class__.__name__}: {exc}",
                attempt_root=self.attempt_root if self.attempt_root.exists() else None,
            ) from exc
        os.chmod(self.attempt_root, 0o700)
        os.chmod(self.raw_root, 0o700)
        os.chmod(self.evidence_root, 0o700)
        _fsync_directory(parent)

    def _assert_live_context(self) -> None:
        _assert_qualification_context_live(self.context)

    def _output_reference(
        self,
        *,
        gate_name: str,
        role: str,
        spec: RawSpec,
        path: Path,
        sha256: str,
        size: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION,
            "artifact_id": f"raw:{uuid.uuid4().hex}",
            "gate_name": gate_name,
            "artifact_role": role,
            "path": str(path),
            "sha256": sha256,
            "size_bytes": int(size),
            "media_type": spec.media_type,
            "content_schema_version": spec.content_schema_version,
            "qualification_campaign_uuid": self.context.qualification_campaign_uuid,
            "commit": self.context.commit,
            "source_digest": self.context.source_digest,
            "protected_source_digest": self.context.protected_source_digest,
        }

    def _capture_json(
        self,
        *,
        source: Path,
        identity: FileIdentity,
        destination: Path,
        gate_name: str,
        role: str,
        spec: RawSpec,
        binding: Mapping[str, Any],
    ) -> tuple[str, int, str]:
        payload, native_sha, _native_size = _read_bounded_json(
            source,
            identity,
            label=f"{gate_name}.{role} native JSON",
            maximum_bytes=MAX_JSON_BYTES,
        )
        _require("formal_binding" not in payload, f"{gate_name}.{role} is already formally bound")
        _require(
            payload.get("schema_version") == spec.content_schema_version,
            f"{gate_name}.{role} native JSON schema mismatch",
        )
        native_nodes, native_string_bytes = _validate_json_shape(
            payload,
            label=f"{gate_name}.{role} native JSON",
        )
        value = dict(payload)
        value["formal_binding"] = dict(binding)
        bound_nodes, bound_string_bytes = _validate_json_shape(
            value,
            label=f"{gate_name}.{role} bound JSON",
        )
        self._check_decoded_budget(
            gate_name=gate_name,
            role=role,
            native_nodes=native_nodes,
            native_string_bytes=native_string_bytes,
            bound_nodes=bound_nodes,
            bound_string_bytes=bound_string_bytes,
        )
        projected_sha, projected_size = _encoded_json_identity(value)
        _require(
            projected_size <= MAX_JSON_BYTES,
            f"{gate_name}.{role} bound JSON exceeds the reviewed limit",
        )
        _require(
            self._structured_output_bytes + projected_size
            <= MAX_STRUCTURED_GATE_BYTES,
            f"{gate_name} structured captured output exceeds the aggregate limit",
        )
        captured_sha, captured_size = _write_json_value(destination, value)
        _require(
            captured_sha == projected_sha and captured_size == projected_size,
            f"{gate_name}.{role} bound JSON differs from its projection",
        )
        self._structured_output_bytes += captured_size
        self._commit_decoded_budget(
            native_nodes=native_nodes,
            native_string_bytes=native_string_bytes,
            bound_nodes=bound_nodes,
            bound_string_bytes=bound_string_bytes,
        )
        return captured_sha, captured_size, native_sha

    def _capture_jsonl(
        self,
        *,
        source: Path,
        identity: FileIdentity,
        destination: Path,
        gate_name: str,
        role: str,
        spec: RawSpec,
        binding: Mapping[str, Any],
    ) -> tuple[str, int, str]:
        _require(
            identity.size <= MAX_NDJSON_BYTES,
            f"{gate_name}.{role} exceeds the bounded JSONL artifact limit",
        )
        native_digest = hashlib.sha256()
        output_digest = hashlib.sha256()
        native_size = 0
        output_size = 0
        records = 0
        native_nodes = 0
        native_string_bytes = 0
        bound_nodes = 0
        bound_string_bytes = 0
        encoder = _json_encoder()
        with _open_expected(source, identity, label=f"{gate_name}.{role} native JSONL") as input_handle:
            with _open_new_output(destination) as output_handle:
                while True:
                    line = input_handle.readline(MAX_NDJSON_LINE_BYTES + 1)
                    if not line:
                        break
                    native_digest.update(line)
                    native_size += len(line)
                    if native_size > MAX_NDJSON_BYTES:
                        raise QualificationCaptureError(
                            f"{gate_name}.{role} exceeds the bounded JSONL artifact limit"
                        )
                    if len(line) > MAX_NDJSON_LINE_BYTES:
                        raise QualificationCaptureError(
                            f"{gate_name}.{role} JSONL line exceeds the reviewed bound"
                        )
                    if not line.strip():
                        continue
                    records += 1
                    if records > MAX_NDJSON_RECORDS:
                        raise QualificationCaptureError(
                            f"{gate_name}.{role} JSONL record count exceeds the reviewed bound"
                        )
                    try:
                        row = json.loads(line.decode("utf-8"))
                    except Exception as exc:
                        raise QualificationCaptureError(
                            f"cannot parse {gate_name}.{role} JSONL record {records}: "
                            f"{exc.__class__.__name__}: {exc}"
                        ) from exc
                    _require(isinstance(row, dict), f"{gate_name}.{role} JSONL record is not an object")
                    row_nodes, row_string_bytes = _validate_json_shape(
                        row,
                        label=f"{gate_name}.{role} native JSONL record {records}",
                    )
                    _require("formal_binding" not in row, f"{gate_name}.{role} JSONL is already formally bound")
                    native_schema = row.get("sample_schema_version") or row.get("schema_version")
                    _require(
                        native_schema == spec.content_schema_version,
                        f"{gate_name}.{role} JSONL schema mismatch",
                    )
                    value = dict(row)
                    value["formal_binding"] = dict(binding)
                    value_nodes, value_string_bytes = _validate_json_shape(
                        value,
                        label=f"{gate_name}.{role} bound JSONL record {records}",
                    )
                    prospective_native_nodes = native_nodes + row_nodes
                    prospective_native_string_bytes = (
                        native_string_bytes + row_string_bytes
                    )
                    prospective_bound_nodes = bound_nodes + value_nodes
                    prospective_bound_string_bytes = (
                        bound_string_bytes + value_string_bytes
                    )
                    self._check_decoded_budget(
                        gate_name=gate_name,
                        role=role,
                        native_nodes=prospective_native_nodes,
                        native_string_bytes=prospective_native_string_bytes,
                        bound_nodes=prospective_bound_nodes,
                        bound_string_bytes=prospective_bound_string_bytes,
                    )
                    native_nodes = prospective_native_nodes
                    native_string_bytes = prospective_native_string_bytes
                    bound_nodes = prospective_bound_nodes
                    bound_string_bytes = prospective_bound_string_bytes
                    output_line_size = 0
                    for token in encoder.iterencode(value):
                        chunk = token.encode("utf-8")
                        output_line_size += len(chunk)
                        if output_line_size + 1 > MAX_NDJSON_LINE_BYTES:
                            raise QualificationCaptureError(
                                f"{gate_name}.{role} bound JSONL line exceeds the reviewed bound"
                            )
                        if (
                            self._structured_output_bytes + output_size + len(chunk)
                            > MAX_STRUCTURED_GATE_BYTES
                        ):
                            raise QualificationCaptureError(
                                f"{gate_name} structured captured output exceeds the aggregate limit"
                            )
                        _write_all(output_handle, chunk)
                        output_digest.update(chunk)
                        output_size += len(chunk)
                        if output_size > MAX_NDJSON_BYTES:
                            raise QualificationCaptureError(
                                f"{gate_name}.{role} bound JSONL exceeds the reviewed limit"
                            )
                    if (
                        self._structured_output_bytes + output_size + 1
                        > MAX_STRUCTURED_GATE_BYTES
                    ):
                        raise QualificationCaptureError(
                            f"{gate_name} structured captured output exceeds the aggregate limit"
                        )
                    _write_all(output_handle, b"\n")
                    output_digest.update(b"\n")
                    output_size += 1
                    if output_size > MAX_NDJSON_BYTES:
                        raise QualificationCaptureError(
                            f"{gate_name}.{role} bound JSONL exceeds the reviewed limit"
                        )
                    if (
                        self._structured_output_bytes + output_size
                        > MAX_STRUCTURED_GATE_BYTES
                    ):
                        raise QualificationCaptureError(
                            f"{gate_name} structured captured output exceeds the aggregate limit"
                        )
                _verify_open_and_path(
                    source,
                    input_handle,
                    identity,
                    label=f"{gate_name}.{role} native JSONL",
                )
                output_handle.flush()
                os.fsync(output_handle.fileno())
        _require(native_size == identity.size, f"{gate_name}.{role} native JSONL size changed")
        _require(records > 0, f"{gate_name}.{role} native JSONL contains no records")
        _verify_private_output(destination, expected_size=output_size)
        self._structured_output_bytes += output_size
        self._commit_decoded_budget(
            native_nodes=native_nodes,
            native_string_bytes=native_string_bytes,
            bound_nodes=bound_nodes,
            bound_string_bytes=bound_string_bytes,
        )
        return output_digest.hexdigest(), output_size, native_digest.hexdigest()

    def _capture_stream(
        self,
        *,
        source: Path,
        identity: FileIdentity,
        destination: Path,
        gate_name: str,
        role: str,
    ) -> tuple[str, int, str]:
        role_limit = int(
            STREAM_ROLE_MAX_BYTES.get(
                (gate_name, role),
                MAX_STREAM_ARTIFACT_BYTES,
            )
        )
        _require(
            identity.size <= role_limit,
            f"{gate_name}.{role} exceeds the reviewed role size limit",
        )
        native_digest = hashlib.sha256()
        output_digest = hashlib.sha256()
        copied = 0
        next_space_check = 256 * 1024 * 1024
        with _open_expected(source, identity, label=f"{gate_name}.{role} native artifact") as input_handle:
            with _open_new_output(destination) as output_handle:
                while True:
                    chunk = input_handle.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > role_limit:
                        raise QualificationCaptureError(
                            f"{gate_name}.{role} exceeds the reviewed role size limit"
                        )
                    native_digest.update(chunk)
                    output_digest.update(chunk)
                    _write_all(output_handle, chunk)
                    if copied >= next_space_check:
                        _require_free_space(
                            destination.parent,
                            remaining_output_bytes=max(0, identity.size - copied),
                            label=f"{gate_name}.{role} streaming capture",
                        )
                        next_space_check = copied + 256 * 1024 * 1024
                _verify_open_and_path(
                    source,
                    input_handle,
                    identity,
                    label=f"{gate_name}.{role} native artifact",
                )
                output_handle.flush()
                os.fsync(output_handle.fileno())
        _require(copied == identity.size, f"{gate_name}.{role} native artifact size changed")
        _verify_private_output(destination, expected_size=copied)
        _require(native_digest.digest() == output_digest.digest(), f"{gate_name}.{role} byte copy differs")
        return output_digest.hexdigest(), copied, native_digest.hexdigest()

    def _capture_one(
        self,
        *,
        gate_name: str,
        role: str,
        spec: RawSpec,
        source: Path,
        identity: FileIdentity,
        captured_at: str,
    ) -> CapturedArtifact:
        destination = planned_capture_path(
            self.attempt_root,
            gate_name=gate_name,
            role=role,
            native_path=source,
            qualification_campaign_uuid=self.context.qualification_campaign_uuid,
        )
        projected_output_bytes = (
            MAX_JSON_BYTES
            if spec.media_type == "application/json"
            else MAX_NDJSON_BYTES
            if spec.media_type == "application/x-ndjson"
            else identity.size
        )
        _require_free_space(
            destination.parent,
            remaining_output_bytes=projected_output_bytes,
            label=f"{gate_name}.{role} capture",
        )
        binding = self.context.formal_binding(
            gate_name=gate_name,
            artifact_role=role,
            captured_at=captured_at,
        )
        if spec.media_type == "application/json":
            captured_sha, captured_size, native_sha = self._capture_json(
                source=source,
                identity=identity,
                destination=destination,
                gate_name=gate_name,
                role=role,
                spec=spec,
                binding=binding,
            )
        elif spec.media_type == "application/x-ndjson":
            captured_sha, captured_size, native_sha = self._capture_jsonl(
                source=source,
                identity=identity,
                destination=destination,
                gate_name=gate_name,
                role=role,
                spec=spec,
                binding=binding,
            )
        else:
            captured_sha, captured_size, native_sha = self._capture_stream(
                source=source,
                identity=identity,
                destination=destination,
                gate_name=gate_name,
                role=role,
            )
        _require(spec.allow_empty or captured_size > 0, f"{gate_name}.{role} captured artifact is empty")
        reference = self._output_reference(
            gate_name=gate_name,
            role=role,
            spec=spec,
            path=destination,
            sha256=captured_sha,
            size=captured_size,
        )
        return CapturedArtifact(
            role=role,
            native_path=source,
            native_identity=identity,
            native_sha256=native_sha,
            captured_path=destination,
            captured_sha256=captured_sha,
            captured_size=captured_size,
            reference=reference,
        )

    def _seal_raw_files(self, *, gate_name: str) -> None:
        for item in self._captured:
            mode = 0o600 if (
                gate_name == "checkpoint_recovery_verified"
                and item.role == "checkpoint_mirror"
            ) else 0o400
            os.chmod(item.captured_path, mode)

    def _write_attempt_failure(self, *, gate_name: str, exc: BaseException) -> None:
        if not self._prepared:
            return
        failure_path = self.attempt_root / "attempt.failure.json"
        payload = {
            "schema_version": QUALIFICATION_ATTEMPT_SCHEMA_VERSION,
            "status": "FAIL_HARNESS",
            "machine_verified": False,
            "gate_name": gate_name,
            "attempt_root": str(self.attempt_root),
            "context": self.context.to_dict(),
            "captured_roles": [item.role for item in self._captured],
            "error": {
                "type": exc.__class__.__name__,
                "message": str(exc)[:4000],
            },
            "finished_at": format_utc(utc_now()),
        }
        try:
            if not failure_path.exists():
                _write_json_value(failure_path, payload)
            os.chmod(failure_path, 0o400)
        except Exception:
            # The original failure remains authoritative.  Never replace it
            # with a secondary evidence-write error.
            pass

    def _quarantine_final_evidence(self, *, gate_name: str) -> None:
        """Remove a PASS-shaped final name from every failed attempt."""

        if not self._prepared:
            return
        final_path = self.evidence_root / f"{gate_name}.json"
        try:
            os.lstat(final_path)
        except FileNotFoundError:
            return
        invalid_path = self.evidence_root / f".{gate_name}.invalid.json"
        if invalid_path.exists() or invalid_path.is_symlink():
            invalid_path = self.evidence_root / f".{gate_name}.invalid.{uuid.uuid4().hex}.json"
        try:
            os.rename(final_path, invalid_path)
            if not invalid_path.is_symlink():
                os.chmod(invalid_path, 0o400)
            _fsync_directory(self.evidence_root)
        except OSError:
            # The failure record still makes the attempt unusable.  Best effort
            # quarantine must never hide the original validator exception.
            try:
                if not final_path.is_symlink():
                    os.chmod(final_path, 0o000)
            except OSError:
                pass

    def _seal_attempt_directories(self) -> None:
        if not self._prepared:
            return
        for directory in (self.evidence_root, self.raw_root, self.attempt_root):
            try:
                os.chmod(directory, 0o500)
            except FileNotFoundError:
                pass
        try:
            _fsync_directory(self.attempt_root.parent)
        except Exception:
            pass

    def capture_gate(
        self,
        *,
        gate_name: str,
        native_artifact_paths: Mapping[str, Path],
        native_execution_receipt_path: Path | None = None,
    ) -> dict[str, Any]:
        """Capture one exact gate and return only independently validated PASS."""

        capture_started_at = _precise_utc_now()
        capture_started_monotonic_ns = time.monotonic_ns()
        try:
            self._prepare_attempt()
            _require(gate_name in GATE_RAW_SPECS, f"unknown formal gate: {gate_name}")
            specs = GATE_RAW_SPECS[gate_name]
            _require(
                set(native_artifact_paths) == set(specs),
                f"{gate_name} native artifact role set mismatch",
            )
            self._assert_live_context()

            sources = {
                role: _absolute_path(native_artifact_paths[role], label=f"{gate_name}.{role} native artifact")
                for role in specs
            }
            identities = {
                role: _inspect_native(path, label=f"{gate_name}.{role} native artifact")
                for role, path in sources.items()
            }
            structured_native_bytes = sum(
                identities[role].size
                for role, spec in specs.items()
                if spec.media_type in {"application/json", "application/x-ndjson"}
            )
            _require(
                structured_native_bytes <= MAX_STRUCTURED_GATE_BYTES,
                f"{gate_name} native structured size exceeds the aggregate projection limit",
            )
            for role, spec in specs.items():
                if spec.media_type in {"application/json", "application/x-ndjson"}:
                    continue
                role_limit = int(
                    STREAM_ROLE_MAX_BYTES.get(
                        (gate_name, role),
                        MAX_STREAM_ARTIFACT_BYTES,
                    )
                )
                _require(
                    identities[role].size <= role_limit,
                    f"{gate_name}.{role} exceeds the reviewed role size limit",
                )
            destinations = planned_capture_paths(
                self.attempt_root,
                gate_name=gate_name,
                native_artifact_paths=sources,
                qualification_campaign_uuid=self.context.qualification_campaign_uuid,
            )
            inode_keys = {(identity.device, identity.inode) for identity in identities.values()}
            _require(len(inode_keys) == len(identities), f"{gate_name} reuses one native inode for multiple roles")
            _validate_gate_path_contract(
                context=self.context,
                gate_name=gate_name,
                sources=sources,
                identities=identities,
                destinations=destinations,
            )
            _require(
                native_execution_receipt_path is not None,
                f"{gate_name} requires a supervised native execution receipt",
            )
            native_execution, native_execution_identity, native_execution_sha = (
                _load_native_execution_receipt(
                    context=self.context,
                    gate_name=gate_name,
                    receipt_path=native_execution_receipt_path,
                    sources=sources,
                    identities=identities,
                )
            )
            if gate_name == "checkpoint_recovery_verified":
                mirror_destination = destinations["checkpoint_mirror"]
                _prepare_checkpoint_destination(mirror_destination)
                mirror_free = os.statvfs(mirror_destination.parent)
                mirror_free_bytes = int(mirror_free.f_bavail * mirror_free.f_frsize)
                _require(
                    mirror_free_bytes
                    >= (
                        identities["checkpoint_mirror"].size
                        + MAX_JSON_BYTES
                        + MINIMUM_FREE_RESERVE_BYTES
                    ),
                    "insufficient persistent disk space for the checkpoint snapshot",
                )
            free = os.statvfs(self.attempt_root)
            free_bytes = int(free.f_bavail * free.f_frsize)
            declared_bytes = sum(identity.size for identity in identities.values())
            _require(
                free_bytes
                >= (
                    declared_bytes
                    + STRUCTURED_OUTPUT_HEADROOM_BYTES
                    + MINIMUM_FREE_RESERVE_BYTES
                ),
                "insufficient disk space for a private stable snapshot",
            )

            # A fixed context timestamp makes path/hash-linked native receipts
            # projectable without allowing the caller to supply promotion
            # flags or asking the writer to rewrite their authority.
            captured_at = self.context.created_at
            for role, spec in specs.items():
                item = self._capture_one(
                    gate_name=gate_name,
                    role=role,
                    spec=spec,
                    source=sources[role],
                    identity=identities[role],
                    captured_at=captured_at,
                )
                self._captured.append(item)

            for item in self._captured:
                declared_native = native_execution["artifacts"][item.role]
                _require(
                    declared_native.get("sha256") == item.native_sha256,
                    f"{gate_name}.{item.role} native execution SHA mismatch",
                )

            # The entire raw set must describe one stable interval, not merely
            # files that happened to be stable one-by-one during their copy.
            for role, source in sources.items():
                final_identity = _inspect_native(source, label=f"{gate_name}.{role} native artifact")
                _require(final_identity == identities[role], f"{gate_name}.{role} changed during gate capture")
            self._assert_live_context()
            final_execution_identity = _inspect_native(
                _absolute_path(native_execution_receipt_path, label=f"{gate_name} native execution receipt"),
                label=f"{gate_name} native execution receipt",
            )
            _require(
                final_execution_identity == native_execution_identity,
                f"{gate_name} native execution receipt changed during capture",
            )
            _receipt_payload, final_execution_sha, _receipt_size = _read_bounded_json(
                _absolute_path(native_execution_receipt_path, label=f"{gate_name} native execution receipt"),
                final_execution_identity,
                label=f"{gate_name} native execution receipt final readback",
                maximum_bytes=MAX_JSON_BYTES,
            )
            _require(
                final_execution_sha == native_execution_sha,
                f"{gate_name} native execution receipt content changed",
            )
            self._seal_raw_files(gate_name=gate_name)

            checked_at = utc_now().replace(microsecond=0)
            policy = GATE_POLICIES[gate_name]
            evidence_payload = {
                "schema_version": GATE_EVIDENCE_SCHEMA_VERSION,
                "gate_name": gate_name,
                "status": "PASS",
                "machine_verified": True,
                "verification_scope": policy.verification_scope,
                "actual_execution": True,
                "simulated": False,
                "component_only": False,
                "qualification_campaign_uuid": self.context.qualification_campaign_uuid,
                "commit": self.context.commit,
                "source_digest": self.context.source_digest,
                "protected_source_digest": self.context.protected_source_digest,
                "checked_at": format_utc(checked_at),
                "valid_until": format_utc(checked_at + policy.maximum_validity),
                "raw_artifacts": {
                    item.role: dict(item.reference)
                    for item in self._captured
                },
            }
            candidate_path = self.evidence_root / f".{gate_name}.candidate.json"
            final_path = self.evidence_root / f"{gate_name}.json"
            _write_json_value(candidate_path, evidence_payload)
            os.chmod(candidate_path, 0o400)

            # First derivation prevents an invalid candidate from acquiring a
            # final evidence name.  The second derivation validates the exact
            # path later consumed by the bundle builder.
            capture_authority = {
                "producer": self.context.to_dict()["producer"],
                "created_at": self.context.created_at,
                "native_execution": native_execution,
            }
            _validate_unsealed_gate_evidence(
                candidate_path,
                gate_name=gate_name,
                commit=self.context.commit,
                source_digest=self.context.source_digest,
                protected_source_digest=self.context.protected_source_digest,
                qualification_campaign_uuid=self.context.qualification_campaign_uuid,
                now=checked_at,
                capture_authority=capture_authority,
            )
            _require(not final_path.exists() and not final_path.is_symlink(), "final evidence path already exists")
            os.rename(candidate_path, final_path)
            _fsync_directory(self.evidence_root)
            validated = _validate_unsealed_gate_evidence(
                final_path,
                gate_name=gate_name,
                commit=self.context.commit,
                source_digest=self.context.source_digest,
                protected_source_digest=self.context.protected_source_digest,
                qualification_campaign_uuid=self.context.qualification_campaign_uuid,
                now=checked_at,
                capture_authority=capture_authority,
            )
            _require(
                validated.get("status") == "PASS"
                and validated.get("machine_verified") is True,
                f"{gate_name} did not independently derive machine PASS",
            )

            capture_finished_at = _precise_utc_now()
            capture_finished_monotonic_ns = time.monotonic_ns()
            if capture_finished_monotonic_ns <= capture_started_monotonic_ns:
                capture_finished_monotonic_ns = capture_started_monotonic_ns + 1
            attempt_manifest_path = self.attempt_root / "attempt.json"
            manifest = {
                "schema_version": QUALIFICATION_ATTEMPT_SCHEMA_VERSION,
                "status": "PASS",
                "machine_verified": True,
                "gate_name": gate_name,
                "attempt_root": str(self.attempt_root),
                "context": self.context.to_dict(),
                "native_execution": native_execution,
                "capture_execution": {
                    "started_at": capture_started_at,
                    "finished_at": capture_finished_at,
                    "started_monotonic_ns": capture_started_monotonic_ns,
                    "finished_monotonic_ns": capture_finished_monotonic_ns,
                    "producer": self.context.to_dict()["producer"],
                },
                "evidence_path": str(final_path),
                "evidence_sha256": validated["_evidence_sha256"],
                "evidence_size_bytes": validated["_evidence_size"],
                "derived_sha256": validated["_derived_sha256"],
                "raw_artifacts": [item.to_attempt_record() for item in self._captured],
                "finished_at": capture_finished_at,
            }
            _write_json_value(attempt_manifest_path, manifest)
            os.chmod(attempt_manifest_path, 0o400)
            sealed = validate_gate_attempt(
                attempt_manifest_path,
                gate_name=gate_name,
                commit=self.context.commit,
                source_digest=self.context.source_digest,
                protected_source_digest=self.context.protected_source_digest,
                qualification_campaign_uuid=self.context.qualification_campaign_uuid,
                now=checked_at,
            )
            self._seal_attempt_directories()
            result = dict(sealed)
            result["_attempt_root"] = str(self.attempt_root)
            result["_evidence_path"] = str(final_path)
            result["_attempt_manifest"] = str(attempt_manifest_path)
            return result
        except Exception as exc:
            if self._prepared:
                if gate_name in GATE_RAW_SPECS:
                    self._quarantine_final_evidence(gate_name=gate_name)
                self._write_attempt_failure(gate_name=gate_name, exc=exc)
                # Make every partial output read-only before preserving the
                # failed attempt.  No partial candidate can be silently reused.
                for path in self.attempt_root.rglob("*"):
                    try:
                        if path.is_symlink():
                            continue
                        if path.is_file():
                            os.chmod(path, 0o400)
                    except OSError:
                        pass
                self._seal_attempt_directories()
            if isinstance(exc, QualificationCaptureError):
                if not self._prepared:
                    raise
                raise QualificationCaptureError(str(exc), attempt_root=self.attempt_root) from exc
            if isinstance(exc, GateBundleError):
                raise QualificationCaptureError(
                    f"{gate_name} semantic authority did not derive PASS: {exc}",
                    attempt_root=self.attempt_root,
                ) from exc
            raise QualificationCaptureError(
                f"{gate_name} qualification capture failed: {exc.__class__.__name__}: {exc}",
                attempt_root=self.attempt_root,
            ) from exc


def capture_gate_evidence(
    *,
    attempt_root: Path,
    context: QualificationContext,
    gate_name: str,
    native_artifact_paths: Mapping[str, Path],
    native_execution_receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Convenience entrypoint for one one-shot qualification attempt."""

    return QualificationCaptureWriter(
        context=context,
        attempt_root=attempt_root,
    ).capture_gate(
        gate_name=gate_name,
        native_artifact_paths=native_artifact_paths,
        native_execution_receipt_path=native_execution_receipt_path,
    )


def _parse_artifact_paths(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        role, separator, path = str(value).partition("=")
        if not separator or not role or not path or role in result:
            raise QualificationCaptureError(f"invalid --artifact value: {value!r}")
        result[role] = Path(path)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--gate-name", choices=tuple(GATE_RAW_SPECS), required=True)
    parser.add_argument("--source-authority", type=Path, required=True)
    parser.add_argument("--qualification-campaign-uuid", required=True)
    parser.add_argument("--invocation-id")
    parser.add_argument("--native-execution-receipt", type=Path, required=True)
    parser.add_argument("--artifact", action="append", default=[], metavar="ROLE=ABSOLUTE_PATH")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = QualificationContext.create(
            qualification_campaign_uuid=args.qualification_campaign_uuid,
            source_authority_path=args.source_authority,
            invocation_id=args.invocation_id,
        )
        result = capture_gate_evidence(
            attempt_root=args.attempt_root,
            context=context,
            gate_name=args.gate_name,
            native_artifact_paths=_parse_artifact_paths(args.artifact),
            native_execution_receipt_path=args.native_execution_receipt,
        )
    except Exception as exc:
        print(json.dumps({
            "schema_version": QUALIFICATION_ATTEMPT_SCHEMA_VERSION,
            "status": "FAIL_HARNESS",
            "machine_verified": False,
            "error": f"{exc.__class__.__name__}: {exc}",
            "attempt_root": str(getattr(exc, "attempt_root", "") or ""),
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps({
        "schema_version": QUALIFICATION_ATTEMPT_SCHEMA_VERSION,
        "status": "PASS",
        "machine_verified": True,
        "gate_name": args.gate_name,
        "attempt_root": result["_attempt_root"],
        "evidence_path": result["_evidence_path"],
        "derived_sha256": result["_derived_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
