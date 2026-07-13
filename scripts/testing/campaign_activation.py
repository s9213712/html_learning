#!/usr/bin/env python3
"""Fail-closed one-shot artifacts for campaign/core activation handshakes."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Mapping


CORE_READY_SCHEMA_VERSION = "hackme.core-soak-ready.v1"
CORE_ACTIVATION_SCHEMA_VERSION = "hackme.core-soak-activation.v1"
CORE_ACK_SCHEMA_VERSION = "hackme.core-soak-activation-ack.v1"
MAX_ARTIFACT_BYTES = 128 * 1024


class ActivationArtifactError(RuntimeError):
    """Raised when a one-shot activation artifact is unsafe or invalid."""


def _stable_file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_uid),
        int(info.st_gid),
        int(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def canonical_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def artifact_exists(path: Path) -> bool:
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False


def _require_safe_ancestor_chain(path: Path, *, root: Path) -> None:
    candidate = Path(path).absolute()
    authority = Path(root).absolute()
    try:
        authority_info = authority.lstat()
    except FileNotFoundError as exc:
        raise ActivationArtifactError(f"artifact authority root is missing: {authority}") from exc
    if stat.S_ISLNK(authority_info.st_mode) or not stat.S_ISDIR(authority_info.st_mode):
        raise ActivationArtifactError(f"artifact authority root is unsafe: {authority}")
    if authority_info.st_uid != os.getuid() or stat.S_IMODE(authority_info.st_mode) & 0o022:
        raise ActivationArtifactError(f"artifact authority root ownership/mode is unsafe: {authority}")
    try:
        relative = candidate.relative_to(authority)
    except ValueError as exc:
        raise ActivationArtifactError(f"artifact path is outside authority root: {candidate}") from exc
    current = authority
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ActivationArtifactError(f"symlink rejected in artifact path: {current}")
        if current != candidate and not stat.S_ISDIR(info.st_mode):
            raise ActivationArtifactError(f"non-directory artifact ancestor: {current}")
        if info.st_uid != os.getuid():
            raise ActivationArtifactError(f"artifact path owner mismatch: {current}")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise ActivationArtifactError(f"group/world-writable artifact path rejected: {current}")


def prepare_private_directory(path: Path, *, authority_root: Path) -> Path:
    directory = Path(path).absolute()
    authority = Path(authority_root).absolute()
    _require_safe_ancestor_chain(directory.parent, root=authority)
    if artifact_exists(directory) and directory.is_symlink():
        raise ActivationArtifactError(f"activation directory symlink rejected: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    _require_safe_ancestor_chain(directory, root=authority)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ActivationArtifactError(f"activation directory must be mode 0700: {directory}")
    return directory


def assert_fresh_artifact_paths(paths: tuple[Path, ...] | list[Path]) -> None:
    stale = [str(path) for path in paths if artifact_exists(Path(path))]
    if stale:
        raise ActivationArtifactError(
            "pre-existing activation artifact rejected: " + ", ".join(sorted(stale))
        )


def secure_read_json(path: Path, *, authority_root: Path) -> tuple[dict[str, Any], str]:
    artifact = Path(path).absolute()
    authority = Path(authority_root).absolute()
    _require_safe_ancestor_chain(artifact.parent, root=authority)
    parent_info = artifact.parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise ActivationArtifactError("activation artifact parent must be owned mode 0700")
    parent_identity = _stable_file_identity(parent_info)
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        parent_descriptor = os.open(artifact.parent, parent_flags)
    except OSError as exc:
        raise ActivationArtifactError(
            f"cannot securely open activation directory: {exc.__class__.__name__}: {exc}"
        ) from exc
    if _stable_file_identity(os.fstat(parent_descriptor)) != parent_identity:
        os.close(parent_descriptor)
        raise ActivationArtifactError("activation artifact parent changed before read")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(artifact.name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        os.close(parent_descriptor)
        raise ActivationArtifactError(
            f"cannot securely open activation artifact {artifact}: {exc.__class__.__name__}: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ActivationArtifactError("activation artifact is not a regular file")
        if opened.st_uid != os.getuid():
            raise ActivationArtifactError("activation artifact owner mismatch")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise ActivationArtifactError("activation artifact must be mode 0600")
        if opened.st_nlink != 1:
            raise ActivationArtifactError("activation artifact link count must equal one")
        if opened.st_size <= 0 or opened.st_size > MAX_ARTIFACT_BYTES:
            raise ActivationArtifactError("activation artifact size is invalid")
        initial_identity = _stable_file_identity(opened)
        chunks: list[bytes] = []
        remaining = int(opened.st_size)
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                raise ActivationArtifactError(
                    "activation artifact became shorter during secure read"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ActivationArtifactError(
                "activation artifact became longer during secure read"
            )
        raw = b"".join(chunks)
        if len(raw) != int(opened.st_size):
            raise ActivationArtifactError("activation artifact exact-size read failed")
        after = os.fstat(descriptor)
        current = artifact.lstat()
        current_parent = artifact.parent.lstat()
        if (
            _stable_file_identity(after) != initial_identity
            or _stable_file_identity(current) != initial_identity
            or _stable_file_identity(current_parent) != parent_identity
            or _stable_file_identity(os.fstat(parent_descriptor)) != parent_identity
        ):
            raise ActivationArtifactError("activation artifact changed during secure read")
    except OSError as exc:
        raise ActivationArtifactError(
            f"activation artifact changed during secure read: {exc.__class__.__name__}: {exc}"
        ) from exc
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ActivationArtifactError(
            f"activation artifact JSON is invalid: {exc.__class__.__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ActivationArtifactError("activation artifact must contain a JSON object")
    return payload, hashlib.sha256(raw).hexdigest()


def secure_write_once_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    authority_root: Path,
) -> str:
    artifact = Path(path).absolute()
    authority = Path(authority_root).absolute()
    _require_safe_ancestor_chain(artifact.parent, root=authority)
    parent_info = artifact.parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise ActivationArtifactError("activation artifact parent must be owned mode 0700")
    if artifact_exists(artifact):
        raise ActivationArtifactError(f"activation artifact already exists: {artifact}")
    raw = canonical_json_bytes(payload)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ActivationArtifactError("activation artifact payload exceeds size limit")
    temporary = artifact.parent / f".{artifact.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    linked = False
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ActivationArtifactError("short activation artifact write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, artifact, follow_symlinks=False)
            linked = True
        except FileExistsError as exc:
            raise ActivationArtifactError(
                f"activation artifact appeared before one-shot publish: {artifact}"
            ) from exc
        temporary.unlink()
        directory_fd = os.open(artifact.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if not linked and artifact_exists(artifact):
            # Never remove an unexpected destination: it may be adversarial.
            pass
    readback, digest = secure_read_json(artifact, authority_root=authority)
    if readback != dict(payload):
        raise ActivationArtifactError("activation artifact readback mismatch")
    return digest
