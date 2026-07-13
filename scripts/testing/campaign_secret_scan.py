#!/usr/bin/env python3
"""Bounded, fail-closed raw-byte credential scanning for campaign artifacts."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import stat
import shutil
import struct
import tarfile
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


SECRET_SCAN_SCHEMA_VERSION = "hackme.campaign-secret-scan.v1"
PROTECTED_SECRET_STORE_NAME = "restart_develop_server.env"
DEFAULT_MINIMUM_FREE_RESERVE_BYTES = 20 * 1024**3
DEFAULT_SCAN_DEADLINE_SECONDS = 3_600.0
GENERIC_PATTERN_OVERLAP_BYTES = 64 * 1024
ARCHIVE_SUFFIXES = (
    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
    ".tar.xz", ".txz",
)
SENSITIVE_ENV_NAME = re.compile(
    r"(?:PASSWORD|PASSWD|TOKEN|SECRET|API[_-]?KEY|PRIVATE[_-]?KEY|AUTH|COOKIE|SESSION|CSRF|CREDENTIAL)",
    re.I,
)
GENERIC_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I)),
    ("authorization_bearer", re.compile(rb"Authorization\s*[:=]\s*Bearer\s+[A-Za-z0-9._~+/-]{12,}", re.I)),
    ("authorization_basic", re.compile(rb"Authorization\s*[:=]\s*Basic\s+[A-Za-z0-9+/=_-]{12,}", re.I)),
    ("jwt", re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("openai_style_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{12,}")),
    ("github_token", re.compile(rb"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{12,}")),
    ("slack_token", re.compile(rb"\bxox[bp]-[A-Za-z0-9-]{12,}")),
    (
        "sensitive_assignment",
        re.compile(
            rb"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|session|csrf|cookie|password|secret)"
            rb"\s*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/-]{12,}",
            re.I,
        ),
    ),
)


@dataclass(frozen=True)
class SecretScanConfig:
    artifact_root: Path
    needles: Mapping[str, str | bytes]
    controlled_runtime_roots: tuple[Path, ...] = ()
    chunk_bytes: int = 1024 * 1024
    progress_bytes: int = 32 * 1024 * 1024
    progress_entries: int = 4096
    progress_seconds: float = 30.0
    max_file_bytes: int = 64 * 1024**3
    max_total_bytes: int = 512 * 1024**3
    max_total_io_bytes: int = 1024 * 1024**3
    max_entries: int = 250_000
    max_files: int = 200_000
    max_depth: int = 128
    max_error_records: int = 10_000
    max_hit_records: int = 10_000
    max_inventory_records: int = 10_000
    max_needle_bytes: int = 64 * 1024
    deadline_seconds: float = DEFAULT_SCAN_DEADLINE_SECONDS
    minimum_free_reserve_bytes: int = DEFAULT_MINIMUM_FREE_RESERVE_BYTES
    enable_generic_patterns: bool = True
    scan_compressed_artifacts: bool = True
    max_archive_entries: int = 100_000
    max_archive_member_bytes: int = 8 * 1024**3
    max_archive_decoded_bytes: int = 64 * 1024**3
    max_archive_ratio: float = 250.0


def _identity(info: os.stat_result) -> tuple[int, ...]:
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


def _path_digest(path: Path | str) -> str:
    return hashlib.sha256(os.fsencode(str(path))).hexdigest()


def _safe_error(exc: BaseException) -> str:
    """Return only an exception class; messages can contain credentials."""

    return exc.__class__.__name__


def build_sensitive_needle_inventory(
    explicit: Mapping[str, str | bytes],
    *,
    environment: Mapping[str, str] | None = None,
    minimum_value_bytes: int = 8,
    maximum_values: int = 256,
    maximum_total_bytes: int = 1024 * 1024,
) -> dict[str, bytes]:
    """Build an in-memory-only sensitive value inventory.

    Labels derived from environment names are hashes.  Neither this mapping nor
    its values may be serialized into campaign evidence.
    """

    inventory: dict[str, bytes] = {}
    total = 0

    def add(label: str, value: str | bytes) -> None:
        nonlocal total
        raw = value if isinstance(value, bytes) else str(value or "").encode("utf-8")
        if len(raw) < max(1, int(minimum_value_bytes)):
            return
        if len(inventory) >= max(1, int(maximum_values)):
            return
        if total + len(raw) > max(1, int(maximum_total_bytes)):
            return
        inventory[str(label)] = bytes(raw)
        total += len(raw)

    for label, value in explicit.items():
        add(str(label), value)
    for name, value in sorted((environment or {}).items()):
        if not SENSITIVE_ENV_NAME.search(str(name)):
            continue
        label = "env_" + hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:16]
        add(label, str(value or ""))
    return inventory


def _needle_variants(raw: bytes) -> tuple[tuple[str, bytes], ...]:
    variants: dict[bytes, str] = {bytes(raw): "raw"}
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = ""
    if text:
        encoded = {
            "url": urllib.parse.quote(text, safe="").encode("ascii"),
            "url_plus": urllib.parse.quote_plus(text, safe="").encode("ascii"),
            "json": json.dumps(text, ensure_ascii=True)[1:-1].encode("ascii"),
        }
        for name, value in encoded.items():
            variants.setdefault(value, name)
    variants.setdefault(base64.b64encode(raw), "base64")
    variants.setdefault(base64.urlsafe_b64encode(raw), "base64url")
    variants.setdefault(base64.b64encode(raw).rstrip(b"="), "base64_unpadded")
    variants.setdefault(base64.urlsafe_b64encode(raw).rstrip(b"="), "base64url_unpadded")
    return tuple((name, value) for value, name in variants.items() if value)


class _Scanner:
    def __init__(
        self,
        config: SecretScanConfig,
        progress_callback: Callable[[str], None] | None,
    ) -> None:
        self.config = config
        self.root = Path(config.artifact_root).absolute()
        self.progress_callback = progress_callback
        self.needles: dict[str, bytes] = {}
        self.patterns = GENERIC_SECRET_PATTERNS if config.enable_generic_patterns else ()
        self.errors: list[dict[str, Any]] = []
        self.error_count = 0
        self.hits: list[dict[str, Any]] = []
        self.hit_count = 0
        self.symlinks: list[dict[str, Any]] = []
        self.symlink_count = 0
        self.protected: list[dict[str, Any]] = []
        self.protected_candidate_count = 0
        self.file_inventory: list[dict[str, Any]] = []
        self.entries = 0
        self.regular_files = 0
        self.files_attempted = 0
        self.files_scanned = 0
        self.bytes_scanned = 0
        self.verification_bytes = 0
        self.io_bytes_read = 0
        self.protected_files = 0
        self.protected_bytes = 0
        self.enumeration_complete = True
        self.progress_events = 0
        self.archive_entries = 0
        self.archive_decoded_bytes = 0
        self.tree_leaves: list[bytes] = []
        self.metadata_leaves: list[bytes] = []
        self.initial_tree_digest = ""
        self.final_tree_digest = ""
        self.final_inventory_entries = 0
        self.started_monotonic = time.monotonic()
        self.deadline_monotonic = self.started_monotonic + max(
            0.1, float(config.deadline_seconds)
        )
        self.root_identity: tuple[int, ...] | None = None
        self._bytes_since_progress = 0
        self._entries_since_progress = 0
        self._next_progress_at = time.monotonic() + max(
            0.1, float(config.progress_seconds)
        )
        self._controlled_stores = {
            Path(root).absolute() / PROTECTED_SECRET_STORE_NAME
            for root in config.controlled_runtime_roots
        }

    def _error(self, code: str, path: Path | str, **detail: Any) -> None:
        self.error_count += 1
        if len(self.errors) < max(1, int(self.config.max_error_records)):
            safe_detail = {
                key: value
                for key, value in detail.items()
                if key not in {"error", "target", "source_root", "snapshot_root"}
            }
            if "error" in detail:
                safe_detail["error_code"] = str(detail["error"]).split(":", 1)[0][:128]
            if "target" in detail:
                safe_detail["target_sha256"] = _path_digest(str(detail["target"]))
            self.errors.append({
                "code": str(code),
                "path_sha256": _path_digest(path),
                **safe_detail,
            })

    def _hit(self, *, label: str, path: Path, offset: int) -> None:
        self.hit_count += 1
        if len(self.hits) < max(1, int(self.config.max_hit_records)):
            self.hits.append({
                "label": str(label),
                "path_sha256": _path_digest(path),
                "byte_offset": int(offset),
            })

    def _check_deadline(self, path: Path | str | None = None) -> bool:
        if time.monotonic() <= self.deadline_monotonic:
            return True
        self._error(
            "secret_scan_deadline_exceeded",
            path or self.root,
            maximum_seconds=float(self.config.deadline_seconds),
        )
        self.enumeration_complete = False
        return False

    def _check_disk_reserve(self) -> bool:
        try:
            free = int(shutil.disk_usage(self.root).free)
        except Exception as exc:
            self._error(
                "secret_scan_disk_reserve_unreadable",
                self.root,
                error=_safe_error(exc),
            )
            self.enumeration_complete = False
            return False
        required = max(0, int(self.config.minimum_free_reserve_bytes))
        if free < required:
            self._error(
                "secret_scan_disk_reserve_breached",
                self.root,
                free_bytes=free,
                required_bytes=required,
            )
            self.enumeration_complete = False
            return False
        return True

    def _progress(self) -> None:
        if not self._check_deadline():
            return
        now = time.monotonic()
        if not (
            self._bytes_since_progress >= max(1, int(self.config.progress_bytes))
            or self._entries_since_progress >= max(1, int(self.config.progress_entries))
            or now >= self._next_progress_at
        ):
            return
        self._check_disk_reserve()
        self.progress_events += 1
        if self.progress_callback is not None:
            try:
                self.progress_callback(
                    "secret_scan_progress:"
                    f"entries={self.entries}:files={self.files_scanned}:"
                    f"bytes={self.bytes_scanned}:io_bytes={self.io_bytes_read}"
                )
            except Exception as exc:
                self._error(
                    "progress_callback_failed",
                    self.root,
                    error=_safe_error(exc),
                )
                self.progress_callback = None
        self._bytes_since_progress = 0
        self._entries_since_progress = 0
        self._next_progress_at = now + max(0.1, float(self.config.progress_seconds))

    def _prepare_needles(self) -> None:
        for label, value in self.config.needles.items():
            raw = value if isinstance(value, bytes) else str(value or "").encode("utf-8")
            if not raw:
                continue
            if len(raw) > int(self.config.max_needle_bytes):
                self._error(
                    "credential_needle_hard_cap_exceeded",
                    self.root,
                    label=str(label),
                    needle_bytes=len(raw),
                    maximum_bytes=int(self.config.max_needle_bytes),
                )
                continue
            for variant, encoded in _needle_variants(bytes(raw)):
                if len(encoded) > int(self.config.max_needle_bytes):
                    self._error(
                        "credential_variant_hard_cap_exceeded",
                        self.root,
                        label=str(label),
                        variant=variant,
                        needle_bytes=len(encoded),
                    )
                    continue
                output_label = str(label) if variant == "raw" else f"{label}:{variant}"
                self.needles[output_label] = encoded

    def _protected_policy(self, path: Path, info: os.stat_result) -> tuple[bool, dict[str, Any] | None]:
        if path.name != PROTECTED_SECRET_STORE_NAME:
            return False, None
        controlled = path in self._controlled_stores
        owner_ok = int(info.st_uid) == os.getuid()
        mode = stat.S_IMODE(info.st_mode)
        mode_ok = mode == 0o600
        link_ok = int(info.st_nlink) == 1
        allowed = bool(controlled and owner_ok and mode_ok and link_ok)
        row = {
            "path_sha256": _path_digest(path),
            "controlled_runtime_path": controlled,
            "owner_uid": int(info.st_uid),
            "expected_owner_uid": os.getuid(),
            "mode": oct(mode),
            "size_bytes": int(info.st_size),
            "link_count": int(info.st_nlink),
            "credential_hit_exempted": allowed,
            "stable_snapshot_verified": False,
            "ok": allowed,
        }
        self.protected_candidate_count += 1
        if len(self.protected) < max(1, int(self.config.max_inventory_records)):
            self.protected.append(row)
        if not allowed:
            self._error(
                "protected_secret_store_policy_failed",
                path,
                controlled_runtime_path=controlled,
                owner_ok=owner_ok,
                mode_ok=mode_ok,
                link_ok=link_ok,
            )
        return allowed, row

    @staticmethod
    def _metadata_leaf(kind: str, relative: str, info: os.stat_result) -> bytes:
        return hashlib.sha256(
            str(kind).encode("ascii")
            + b"\0"
            + str(relative).encode("utf-8", errors="surrogateescape")
            + b"\0"
            + json.dumps(list(_identity(info)), separators=(",", ":")).encode("ascii")
        ).digest()

    @staticmethod
    def _aggregate_digest(leaves: list[bytes]) -> str:
        digest = hashlib.sha256()
        for leaf in sorted(leaves):
            digest.update(leaf)
        return digest.hexdigest()

    def _match_window(
        self,
        *,
        window: bytes,
        path: Path,
        window_offset: int,
        found_labels: set[str],
        label_prefix: str = "",
    ) -> None:
        for label, needle in self.needles.items():
            output_label = f"{label_prefix}{label}"
            if output_label in found_labels:
                continue
            index = window.find(needle)
            if index >= 0:
                found_labels.add(output_label)
                self._hit(label=output_label, path=path, offset=window_offset + index)
        for label, pattern in self.patterns:
            output_label = f"{label_prefix}generic:{label}"
            if output_label in found_labels:
                continue
            match = pattern.search(window)
            if match is not None:
                found_labels.add(output_label)
                self._hit(
                    label=output_label,
                    path=path,
                    offset=window_offset + int(match.start()),
                )

    def _scan_decoded_stream(
        self,
        handle: Any,
        *,
        path: Path,
        found_labels: set[str],
    ) -> None:
        maximum_overlap = max(
            GENERIC_PATTERN_OVERLAP_BYTES,
            max((len(value) for value in self.needles.values()), default=1) - 1,
        )
        carry = b""
        offset = 0
        while True:
            if not self._check_deadline(path):
                raise TimeoutError("secret_scan_deadline_exceeded")
            remaining_budget = int(self.config.max_archive_decoded_bytes) - int(
                self.archive_decoded_bytes
            )
            if remaining_budget <= 0:
                raise OSError("archive_decoded_byte_cap_exceeded")
            block = handle.read(
                min(max(1, int(self.config.chunk_bytes)), remaining_budget + 1)
            )
            if not block:
                break
            if len(block) > remaining_budget:
                raise OSError("archive_decoded_byte_cap_exceeded")
            if self.io_bytes_read + len(block) > int(self.config.max_total_io_bytes):
                raise OSError("total_io_byte_hard_cap_exceeded")
            window = carry + block
            self._match_window(
                window=window,
                path=path,
                window_offset=offset - len(carry),
                found_labels=found_labels,
                label_prefix="archive:",
            )
            amount = len(block)
            self.archive_decoded_bytes += amount
            self.io_bytes_read += amount
            self._bytes_since_progress += amount
            offset += amount
            carry = window[-maximum_overlap:] if maximum_overlap else b""
            self._progress()

    def _zip_preflight(self, descriptor: int, size: int, path: Path) -> bool:
        tail_bytes = min(size, 65_557)
        tail = os.pread(descriptor, tail_bytes, max(0, size - tail_bytes))
        marker = tail.rfind(b"PK\x05\x06")
        if marker < 0 or len(tail) - marker < 22:
            self._error("zip_eocd_missing", path)
            return False
        try:
            (
                _signature,
                disk_number,
                central_disk,
                entries_on_disk,
                total_entries,
                central_size,
                central_offset,
                comment_size,
            ) = struct.unpack_from("<4s4H2LH", tail, marker)
        except struct.error as exc:
            self._error("zip_eocd_invalid", path, error=_safe_error(exc))
            return False
        if disk_number or central_disk or entries_on_disk != total_entries:
            self._error("zip_multidisk_rejected", path)
            return False
        if total_entries == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
            self._error("zip64_rejected", path)
            return False
        if total_entries > int(self.config.max_archive_entries):
            self._error(
                "archive_entry_cap_exceeded",
                path,
                entries=int(total_entries),
                maximum=int(self.config.max_archive_entries),
            )
            return False
        if central_size > 64 * 1024**2 or central_offset + central_size > size:
            self._error("zip_central_directory_invalid_or_too_large", path)
            return False
        if marker + 22 + comment_size > len(tail):
            self._error("zip_comment_out_of_bounds", path)
            return False
        return True

    def _scan_archive(
        self,
        *,
        descriptor: int,
        path: Path,
        initial: os.stat_result,
        found_labels: set[str],
    ) -> None:
        if not self.config.scan_compressed_artifacts:
            return
        lower_name = path.name.lower()
        magic = os.pread(descriptor, 8, 0)
        is_zip = magic.startswith(b"PK\x03\x04") or lower_name.endswith(".zip")
        is_tar = lower_name.endswith(ARCHIVE_SUFFIXES[1:])
        if not (is_zip or is_tar):
            return
        size = int(initial.st_size)
        before_decoded = self.archive_decoded_bytes
        if is_zip:
            if not self._zip_preflight(descriptor, size, path):
                return
            raw = os.fdopen(os.dup(descriptor), "rb", closefd=True)
            try:
                with raw, zipfile.ZipFile(raw) as archive:
                    infos = archive.infolist()
                    if len(infos) > int(self.config.max_archive_entries):
                        self._error("archive_entry_cap_exceeded", path)
                        return
                    for info in infos:
                        if info.is_dir():
                            continue
                        self.archive_entries += 1
                        if self.archive_entries > int(self.config.max_archive_entries):
                            self._error("archive_entry_cap_exceeded", path)
                            return
                        if info.file_size > int(self.config.max_archive_member_bytes):
                            self._error("archive_member_size_cap_exceeded", path)
                            return
                        compressed = max(1, int(info.compress_size))
                        if float(info.file_size) / compressed > float(self.config.max_archive_ratio):
                            self._error("archive_compression_ratio_cap_exceeded", path)
                            return
                        self._match_window(
                            window=info.filename.encode("utf-8", errors="replace"),
                            path=path,
                            window_offset=0,
                            found_labels=found_labels,
                            label_prefix="archive_name:",
                        )
                        with archive.open(info, "r") as member:
                            self._scan_decoded_stream(
                                member,
                                path=path,
                                found_labels=found_labels,
                            )
            except Exception as exc:
                self._error("archive_scan_failed", path, error=_safe_error(exc))
                return
        else:
            raw = os.fdopen(os.dup(descriptor), "rb", closefd=True)
            try:
                with raw, tarfile.open(fileobj=raw, mode="r|*") as archive:
                    for member in archive:
                        if not member.isfile():
                            continue
                        self.archive_entries += 1
                        if self.archive_entries > int(self.config.max_archive_entries):
                            raise OSError("archive_entry_cap_exceeded")
                        if int(member.size) > int(self.config.max_archive_member_bytes):
                            raise OSError("archive_member_size_cap_exceeded")
                        self._match_window(
                            window=member.name.encode("utf-8", errors="replace"),
                            path=path,
                            window_offset=0,
                            found_labels=found_labels,
                            label_prefix="archive_name:",
                        )
                        member_handle = archive.extractfile(member)
                        if member_handle is None:
                            raise OSError("archive_member_unreadable")
                        with member_handle:
                            self._scan_decoded_stream(
                                member_handle,
                                path=path,
                                found_labels=found_labels,
                            )
            except Exception as exc:
                self._error("archive_scan_failed", path, error=_safe_error(exc))
                return
        decoded = int(self.archive_decoded_bytes) - int(before_decoded)
        if size > 0 and float(decoded) / float(size) > float(self.config.max_archive_ratio):
            self._error("archive_aggregate_ratio_cap_exceeded", path)

    def _scan_file(
        self,
        *,
        path: Path,
        name: str,
        parent_fd: int,
        discovered: os.stat_result,
    ) -> None:
        if not self._check_deadline(path):
            return
        self.regular_files += 1
        if self.regular_files > int(self.config.max_files):
            self._error(
                "file_count_hard_cap_exceeded",
                path,
                maximum_files=int(self.config.max_files),
            )
            self.enumeration_complete = False
            return
        size = int(discovered.st_size)
        if size > int(self.config.max_file_bytes):
            self._error(
                "file_size_hard_cap_exceeded",
                path,
                size_bytes=size,
                maximum_bytes=int(self.config.max_file_bytes),
            )
            return
        if self.bytes_scanned + size > int(self.config.max_total_bytes):
            self._error(
                "total_byte_hard_cap_exceeded",
                path,
                projected_bytes=self.bytes_scanned + size,
                maximum_bytes=int(self.config.max_total_bytes),
            )
            self.enumeration_complete = False
            return
        if self.io_bytes_read + (2 * size) > int(self.config.max_total_io_bytes):
            self._error(
                "total_io_byte_hard_cap_exceeded",
                path,
                projected_bytes=self.io_bytes_read + (2 * size),
                maximum_bytes=int(self.config.max_total_io_bytes),
            )
            self.enumeration_complete = False
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = -1
        self.files_attempted += 1
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            initial = os.fstat(descriptor)
            initial_identity = _identity(initial)
            if not stat.S_ISREG(initial.st_mode):
                raise OSError("artifact changed to a non-regular file before open")
            if initial_identity != _identity(discovered):
                raise OSError("artifact identity changed between enumeration and open")
            protected_exempt, protected_row = self._protected_policy(path, initial)
            maximum_overlap = max(
                GENERIC_PATTERN_OVERLAP_BYTES,
                max((len(value) for value in self.needles.values()), default=1) - 1,
            )
            carry = b""
            found_labels: set[str] = set()
            first_digest = hashlib.sha256()
            remaining = int(initial.st_size)
            file_offset = 0
            while remaining > 0:
                chunk = os.read(
                    descriptor,
                    min(max(1, int(self.config.chunk_bytes)), remaining),
                )
                if not chunk:
                    raise OSError("artifact truncated during credential scan")
                window = carry + chunk
                first_digest.update(chunk)
                window_offset = file_offset - len(carry)
                if not protected_exempt:
                    self._match_window(
                        window=window,
                        path=path,
                        window_offset=window_offset,
                        found_labels=found_labels,
                    )
                carry = window[-maximum_overlap:] if maximum_overlap > 0 else b""
                amount = len(chunk)
                remaining -= amount
                file_offset += amount
                self.bytes_scanned += amount
                self.io_bytes_read += amount
                self._bytes_since_progress += amount
                self._progress()
                if not self.enumeration_complete:
                    raise TimeoutError("secret scan stopped by deadline or disk reserve")
            if os.read(descriptor, 1):
                raise OSError("artifact appended during credential scan")
            os.lseek(descriptor, 0, os.SEEK_SET)
            second_digest = hashlib.sha256()
            remaining = int(initial.st_size)
            while remaining > 0:
                chunk = os.read(
                    descriptor,
                    min(max(1, int(self.config.chunk_bytes)), remaining),
                )
                if not chunk:
                    raise OSError("artifact truncated during snapshot verification")
                second_digest.update(chunk)
                amount = len(chunk)
                remaining -= amount
                self.verification_bytes += amount
                self.io_bytes_read += amount
                self._bytes_since_progress += amount
                self._progress()
                if not self.enumeration_complete:
                    raise TimeoutError("secret scan stopped by deadline or disk reserve")
            if os.read(descriptor, 1):
                raise OSError("artifact appended during snapshot verification")
            if first_digest.digest() != second_digest.digest():
                raise OSError("artifact content changed during credential scan")
            if not protected_exempt:
                self._scan_archive(
                    descriptor=descriptor,
                    path=path,
                    initial=initial,
                    found_labels=found_labels,
                )
            final_fd = os.fstat(descriptor)
            final_entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                _identity(final_fd) != initial_identity
                or _identity(final_entry) != initial_identity
            ):
                raise OSError("artifact metadata or path changed during credential scan")
            self.files_scanned += 1
            relative_name = path.relative_to(self.root).as_posix()
            leaf = hashlib.sha256(
                b"file\0"
                + relative_name.encode("utf-8", errors="surrogateescape")
                + b"\0"
                + json.dumps(list(initial_identity), separators=(",", ":")).encode("ascii")
                + b"\0"
                + first_digest.hexdigest().encode("ascii")
            ).digest()
            self.tree_leaves.append(leaf)
            self.metadata_leaves.append(self._metadata_leaf("file", relative_name, initial))
            if len(self.file_inventory) < max(
                1, int(self.config.max_inventory_records)
            ):
                self.file_inventory.append({
                    "path_sha256": _path_digest(relative_name),
                    "size_bytes": int(initial.st_size),
                    "sha256": first_digest.hexdigest(),
                    "device": int(initial.st_dev),
                    "inode": int(initial.st_ino),
                    "mode": oct(stat.S_IMODE(initial.st_mode)),
                    "owner_uid": int(initial.st_uid),
                    "link_count": int(initial.st_nlink),
                    "mtime_ns": int(initial.st_mtime_ns),
                    "ctime_ns": int(initial.st_ctime_ns),
                })
            if protected_exempt:
                self.protected_files += 1
                self.protected_bytes += int(initial.st_size)
            if protected_row is not None:
                protected_row["stable_snapshot_verified"] = True
                protected_row["scanned_bytes"] = int(initial.st_size)
        except Exception as exc:
            self._error(
                "artifact_snapshot_failed",
                path,
                error=_safe_error(exc),
            )
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except Exception as exc:
                    self._error(
                        "artifact_descriptor_close_failed",
                        path,
                        error=_safe_error(exc),
                    )

    def _enumerate(self) -> None:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        root_fd = -1
        try:
            root_info = self.root.lstat()
        except Exception as exc:
            self._error(
                "artifact_root_unreadable",
                self.root,
                error=_safe_error(exc),
            )
            self.enumeration_complete = False
            return
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            self._error("artifact_root_not_regular_directory", self.root)
            self.enumeration_complete = False
            return
        if not self._check_disk_reserve() or not self._check_deadline(self.root):
            return

        def walk(directory_fd: int, relative: str, discovered: os.stat_result, depth: int) -> None:
            if not self.enumeration_complete or not self._check_deadline(self.root / relative):
                return
            initial_directory = os.fstat(directory_fd)
            initial_directory_identity = _identity(initial_directory)
            if (
                not stat.S_ISDIR(initial_directory.st_mode)
                or initial_directory_identity != _identity(discovered)
            ):
                raise OSError("directory identity changed before enumeration")
            iterator: Any = None
            try:
                iterator = os.scandir(directory_fd)
                for entry in iterator:
                    self.entries += 1
                    self._entries_since_progress += 1
                    self._progress()
                    if not self.enumeration_complete:
                        break
                    if self.entries > int(self.config.max_entries):
                        self._error(
                            "entry_count_hard_cap_exceeded",
                            self.root / relative,
                            maximum_entries=int(self.config.max_entries),
                        )
                        self.enumeration_complete = False
                        break
                    child_relative = f"{relative}/{entry.name}" if relative else entry.name
                    path = self.root / child_relative
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except Exception as exc:
                        self._error(
                            "artifact_entry_stat_failed",
                            path,
                            error=_safe_error(exc),
                        )
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        try:
                            target = os.readlink(entry.name, dir_fd=directory_fd)
                        except Exception as exc:
                            target = f"[unreadable:{exc.__class__.__name__}]"
                        self.symlink_count += 1
                        if len(self.symlinks) < max(
                            1, int(self.config.max_inventory_records)
                        ):
                            self.symlinks.append({
                                "path_sha256": _path_digest(child_relative),
                                "target_sha256": _path_digest(target),
                            })
                        self._error("artifact_symlink_rejected", path, target=target)
                    elif stat.S_ISDIR(info.st_mode):
                        if depth + 1 > int(self.config.max_depth):
                            self._error(
                                "directory_depth_hard_cap_exceeded",
                                path,
                                maximum_depth=int(self.config.max_depth),
                            )
                            self.enumeration_complete = False
                        else:
                            child_fd = -1
                            try:
                                child_fd = os.open(
                                    entry.name,
                                    directory_flags,
                                    dir_fd=directory_fd,
                                )
                                walk(child_fd, child_relative, info, depth + 1)
                                final_entry = os.stat(
                                    entry.name,
                                    dir_fd=directory_fd,
                                    follow_symlinks=False,
                                )
                                if _identity(final_entry) != _identity(info):
                                    raise OSError("directory entry changed during traversal")
                            finally:
                                if child_fd >= 0:
                                    os.close(child_fd)
                    elif stat.S_ISREG(info.st_mode):
                        self._scan_file(
                            path=path,
                            name=entry.name,
                            parent_fd=directory_fd,
                            discovered=info,
                        )
                        if not self.enumeration_complete:
                            break
                    else:
                        self._error(
                            "artifact_special_file_rejected",
                            path,
                            mode=oct(stat.S_IMODE(info.st_mode)),
                        )
                final_directory = os.fstat(directory_fd)
                if _identity(final_directory) != initial_directory_identity:
                    raise OSError("directory changed during bounded enumeration")
                self.metadata_leaves.append(
                    self._metadata_leaf("directory", relative or ".", final_directory)
                )
                self.tree_leaves.append(
                    self._metadata_leaf("directory", relative or ".", final_directory)
                )
            except Exception as exc:
                self._error(
                    "artifact_directory_enumeration_failed",
                    self.root / relative,
                    error=_safe_error(exc),
                )
                self.enumeration_complete = False
            finally:
                if iterator is not None:
                    try:
                        iterator.close()
                    except Exception as exc:
                        self._error(
                            "artifact_directory_iterator_close_failed",
                            self.root / relative,
                            error=_safe_error(exc),
                        )

        def metadata_walk(directory_fd: int, relative: str, depth: int) -> list[bytes]:
            if not self._check_deadline(self.root / relative):
                raise TimeoutError("secret scan metadata deadline exceeded")
            initial = os.fstat(directory_fd)
            leaves: list[bytes] = []
            iterator = os.scandir(directory_fd)
            try:
                for entry in iterator:
                    self.final_inventory_entries += 1
                    if self.final_inventory_entries > int(self.config.max_entries):
                        raise OSError("final inventory entry cap exceeded")
                    child_relative = f"{relative}/{entry.name}" if relative else entry.name
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise OSError("symlink appeared during final inventory")
                    if stat.S_ISDIR(info.st_mode):
                        if depth + 1 > int(self.config.max_depth):
                            raise OSError("final inventory depth cap exceeded")
                        child_fd = os.open(
                            entry.name,
                            directory_flags,
                            dir_fd=directory_fd,
                        )
                        try:
                            leaves.extend(metadata_walk(child_fd, child_relative, depth + 1))
                            final_entry = os.stat(
                                entry.name,
                                dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                            if _identity(final_entry) != _identity(info):
                                raise OSError("directory changed during final inventory")
                        finally:
                            os.close(child_fd)
                    elif stat.S_ISREG(info.st_mode):
                        leaves.append(self._metadata_leaf("file", child_relative, info))
                    else:
                        raise OSError("special file appeared during final inventory")
                final = os.fstat(directory_fd)
                if _identity(final) != _identity(initial):
                    raise OSError("directory changed during final inventory")
                leaves.append(self._metadata_leaf("directory", relative or ".", final))
                return leaves
            finally:
                iterator.close()

        try:
            root_fd = os.open(self.root, directory_flags)
            opened_root = os.fstat(root_fd)
            if _identity(opened_root) != _identity(root_info):
                raise OSError("artifact root changed before pinned open")
            self.root_identity = _identity(opened_root)
            walk(root_fd, "", opened_root, 0)
            self.initial_tree_digest = self._aggregate_digest(self.metadata_leaves)
            self.final_tree_digest = self._aggregate_digest(metadata_walk(root_fd, "", 0))
            if self.initial_tree_digest != self.final_tree_digest:
                self._error("artifact_tree_changed_after_file_scan", self.root)
                self.enumeration_complete = False
            final_root_fd = os.fstat(root_fd)
            final_root_path = self.root.lstat()
            if (
                _identity(final_root_fd) != self.root_identity
                or _identity(final_root_path) != self.root_identity
            ):
                raise OSError("artifact root path changed during final inventory")
        except Exception as exc:
            self._error(
                "artifact_root_pinned_traversal_failed",
                self.root,
                error=_safe_error(exc),
            )
            self.enumeration_complete = False
        finally:
            if root_fd >= 0:
                try:
                    os.close(root_fd)
                except Exception as exc:
                    self._error(
                        "artifact_root_descriptor_close_failed",
                        self.root,
                        error=_safe_error(exc),
                    )

    def _result(self, *, scope: str = "recursive_tree") -> dict[str, Any]:
        return {
            "schema_version": SECRET_SCAN_SCHEMA_VERSION,
            "scope": scope,
            "artifact_root_sha256": _path_digest(self.root),
            "ok": bool(
                self.enumeration_complete
                and self.error_count == 0
                and self.hit_count == 0
                and self.files_scanned == self.regular_files
                and (
                    scope == "exact_files"
                    or (
                        bool(self.initial_tree_digest)
                        and self.initial_tree_digest == self.final_tree_digest
                    )
                )
            ),
            "files": self.regular_files,
            "files_attempted": self.files_attempted,
            "files_scanned": self.files_scanned,
            "file_inventory": self.file_inventory,
            "file_inventory_truncated": self.files_scanned > len(self.file_inventory),
            "bytes": self.bytes_scanned,
            "bytes_scanned": self.bytes_scanned,
            "verification_bytes": self.verification_bytes,
            "io_bytes_read": self.io_bytes_read,
            "entries": self.entries,
            "final_inventory_entries": self.final_inventory_entries,
            "enumeration_complete": self.enumeration_complete,
            "content_tree_digest": self._aggregate_digest(self.tree_leaves),
            "initial_metadata_tree_digest": self.initial_tree_digest,
            "final_metadata_tree_digest": self.final_tree_digest,
            "tree_stable": bool(
                scope == "exact_files"
                or (
                    self.initial_tree_digest
                    and self.initial_tree_digest == self.final_tree_digest
                )
            ),
            "hits": self.hits,
            "hit_count": self.hit_count,
            "hits_truncated": self.hit_count > len(self.hits),
            "errors": self.errors,
            "error_count": self.error_count,
            "errors_truncated": self.error_count > len(self.errors),
            "symlinks": self.symlinks,
            "symlink_count": self.symlink_count,
            "symlinks_truncated": self.symlink_count > len(self.symlinks),
            "protected_secret_stores": self.protected,
            "protected_candidate_count": self.protected_candidate_count,
            "protected_inventory_truncated": self.protected_candidate_count > len(self.protected),
            "protected_files": self.protected_files,
            "protected_bytes": self.protected_bytes,
            "archive_entries": self.archive_entries,
            "archive_decoded_bytes": self.archive_decoded_bytes,
            "progress_events": self.progress_events,
            "elapsed_seconds": round(time.monotonic() - self.started_monotonic, 6),
            "limits": {
                "chunk_bytes": int(self.config.chunk_bytes),
                "max_file_bytes": int(self.config.max_file_bytes),
                "max_total_bytes": int(self.config.max_total_bytes),
                "max_total_io_bytes": int(self.config.max_total_io_bytes),
                "max_entries": int(self.config.max_entries),
                "max_files": int(self.config.max_files),
                "max_depth": int(self.config.max_depth),
                "max_inventory_records": int(self.config.max_inventory_records),
                "deadline_seconds": float(self.config.deadline_seconds),
                "minimum_free_reserve_bytes": int(
                    self.config.minimum_free_reserve_bytes
                ),
                "max_archive_entries": int(self.config.max_archive_entries),
                "max_archive_member_bytes": int(
                    self.config.max_archive_member_bytes
                ),
                "max_archive_decoded_bytes": int(
                    self.config.max_archive_decoded_bytes
                ),
                "max_archive_ratio": float(self.config.max_archive_ratio),
            },
        }

    def run(self) -> dict[str, Any]:
        self._prepare_needles()
        self._enumerate()
        return self._result()


def scan_campaign_secrets(
    config: SecretScanConfig,
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Scan every regular artifact byte or return a fail-closed error record."""

    return _Scanner(config, progress_callback).run()


def scan_campaign_secret_files(
    config: SecretScanConfig,
    paths: tuple[Path, ...],
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Scan an explicit post-cutoff file inventory without implying tree coverage."""

    scanner = _Scanner(config, progress_callback)
    scanner._prepare_needles()
    seen: set[Path] = set()
    expected: list[str] = []
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd = -1
    try:
        root_info = scanner.root.lstat()
        root_fd = os.open(scanner.root, directory_flags)
        opened_root = os.fstat(root_fd)
        if (
            stat.S_ISLNK(root_info.st_mode)
            or not stat.S_ISDIR(root_info.st_mode)
            or _identity(root_info) != _identity(opened_root)
        ):
            raise OSError("exact scan root is not a pinned real directory")
        scanner.root_identity = _identity(opened_root)
        if not scanner._check_disk_reserve():
            raise OSError("exact scan disk reserve breached")
    except Exception as exc:
        scanner._error("exact_scan_root_open_failed", scanner.root, error=_safe_error(exc))
        scanner.enumeration_complete = False

    for requested in paths if root_fd >= 0 else ():
        path = Path(os.path.abspath(os.fspath(requested)))
        expected.append(_path_digest(path))
        parent_fd = -1
        if path in seen:
            scanner._error("exact_file_duplicate", path)
            continue
        seen.add(path)
        try:
            relative = path.relative_to(scanner.root)
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise OSError("exact path is not a strict child of artifact root")
            scanner.entries += 1
            scanner._entries_since_progress += 1
            scanner._progress()
            parent_fd = os.dup(root_fd)
            for component in relative.parts[:-1]:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child_fd
            discovered = os.stat(
                relative.parts[-1],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(discovered.st_mode) or not stat.S_ISREG(discovered.st_mode):
                raise OSError("exact path is not a regular non-symlink file")
            scanner._scan_file(
                path=path,
                name=relative.parts[-1],
                parent_fd=parent_fd,
                discovered=discovered,
            )
        except Exception as exc:
            scanner._error(
                "exact_file_pinned_open_failed",
                path,
                error=_safe_error(exc),
            )
        finally:
            if parent_fd >= 0:
                try:
                    os.close(parent_fd)
                except Exception as exc:
                    scanner._error(
                        "exact_file_parent_close_failed",
                        path,
                        error=_safe_error(exc),
                    )
                parent_fd = -1
    if root_fd >= 0:
        try:
            final_fd = os.fstat(root_fd)
            final_path = scanner.root.lstat()
            if (
                _identity(final_fd) != scanner.root_identity
                or _identity(final_path) != scanner.root_identity
            ):
                raise OSError("exact scan root changed")
        except Exception as exc:
            scanner._error("exact_scan_root_changed", scanner.root, error=_safe_error(exc))
            scanner.enumeration_complete = False
        finally:
            os.close(root_fd)
    scanner.initial_tree_digest = scanner._aggregate_digest(scanner.metadata_leaves)
    scanner.final_tree_digest = scanner.initial_tree_digest
    scanner.enumeration_complete = bool(
        scanner.enumeration_complete and len(seen) == len(paths)
    )
    result = scanner._result(scope="exact_files")
    result["expected_path_sha256"] = expected
    result["expected_file_count"] = len(paths)
    return result


CONTROL_SNAPSHOT_SCHEMA_VERSION = "hackme.campaign-control-snapshot.v1"


@dataclass(frozen=True)
class ControlSnapshotConfig:
    source_root: Path
    snapshot_root: Path
    chunk_bytes: int = 1024 * 1024
    progress_bytes: int = 32 * 1024 * 1024
    progress_entries: int = 4096
    progress_seconds: float = 30.0
    max_file_bytes: int = 64 * 1024**3
    max_total_bytes: int = 128 * 1024**3
    max_total_io_bytes: int = 256 * 1024**3
    max_entries: int = 100_000
    max_files: int = 50_000
    max_depth: int = 128
    max_rounds: int = 12
    max_seconds: float = 300.0
    minimum_free_reserve_bytes: int = DEFAULT_MINIMUM_FREE_RESERVE_BYTES


class _ControlSnapshotDeadline(TimeoutError):
    pass


class _ControlSnapshotDiskReserve(OSError):
    pass


class _ControlSnapshotSourceChanged(OSError):
    pass


class _ControlSnapshot:
    def __init__(
        self,
        config: ControlSnapshotConfig,
        progress_callback: Callable[[str], None] | None,
    ) -> None:
        self.config = config
        self.source_root = Path(config.source_root).absolute()
        self.snapshot_root = Path(config.snapshot_root).absolute()
        self.progress_callback = progress_callback
        self.records: dict[str, dict[str, Any]] = {}
        self.bytes_copied = 0
        self.verification_bytes = 0
        self.io_bytes_read = 0
        self.entries_observed = 0
        self.progress_events = 0
        self.permanent_errors: list[dict[str, Any]] = []
        self._bytes_since_progress = 0
        self._entries_since_progress = 0
        self._next_progress_at = time.monotonic() + max(
            0.1, float(config.progress_seconds)
        )
        self.started_monotonic = time.monotonic()
        self.deadline_monotonic = self.started_monotonic + max(
            0.1, float(config.max_seconds)
        )
        self.source_root_fd = -1
        self.source_root_identity: tuple[int, ...] | None = None

    @staticmethod
    def _safe_row(code: str, path: Path | str, **detail: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "code": str(code),
            "path_sha256": _path_digest(path),
        }
        for key, value in detail.items():
            if key in {"path", "source_root", "snapshot_root", "error"}:
                continue
            row[str(key)] = value
        if "error" in detail:
            error = detail["error"]
            row["error_code"] = (
                _safe_error(error)
                if isinstance(error, BaseException)
                else str(error).split(":", 1)[0][:128]
            )
        return row

    def _check_deadline(self) -> None:
        if time.monotonic() > self.deadline_monotonic:
            raise _ControlSnapshotDeadline("control_snapshot_deadline_exceeded")

    def _check_disk_reserve(self, *, additional_bytes: int = 0) -> None:
        self._check_deadline()
        try:
            free = int(shutil.disk_usage(self.snapshot_root).free)
        except Exception as exc:
            raise _ControlSnapshotDiskReserve(
                "control_snapshot_disk_reserve_unreadable"
            ) from exc
        required = max(0, int(self.config.minimum_free_reserve_bytes))
        if free - max(0, int(additional_bytes)) < required:
            raise _ControlSnapshotDiskReserve(
                "control_snapshot_disk_reserve_breached"
            )

    def _guard(self, *, additional_bytes: int = 0) -> None:
        self._check_deadline()
        self._check_disk_reserve(additional_bytes=additional_bytes)

    @staticmethod
    def _guard_error(exc: BaseException, path: Path | str) -> dict[str, Any]:
        if isinstance(exc, _ControlSnapshotDeadline):
            return _ControlSnapshot._safe_row(
                "control_snapshot_deadline_exceeded", path
            )
        if isinstance(exc, _ControlSnapshotDiskReserve):
            code = str(exc)
            if code not in {
                "control_snapshot_disk_reserve_unreadable",
                "control_snapshot_disk_reserve_breached",
            }:
                code = "control_snapshot_disk_reserve_breached"
            return _ControlSnapshot._safe_row(code, path)
        if isinstance(exc, _ControlSnapshotSourceChanged):
            return _ControlSnapshot._safe_row(
                "control_snapshot_source_identity_changed", path
            )
        return _ControlSnapshot._safe_row(
            "control_snapshot_file_copy_failed", path, error=exc
        )

    def _progress(self) -> str | None:
        self._guard()
        now = time.monotonic()
        if not (
            self._bytes_since_progress >= max(1, int(self.config.progress_bytes))
            or self._entries_since_progress >= max(1, int(self.config.progress_entries))
            or now >= self._next_progress_at
        ):
            return None
        self.progress_events += 1
        if self.progress_callback is not None:
            try:
                self.progress_callback(
                    "control_snapshot_progress:"
                    f"entries={self.entries_observed}:files={len(self.records)}:"
                    f"bytes={self.bytes_copied}"
                )
            except Exception as exc:
                self.progress_callback = None
                self.permanent_errors.append(self._safe_row(
                    "control_snapshot_progress_failed",
                    self.source_root,
                    error=exc,
                ))
                return "control_snapshot_progress_failed"
        self._bytes_since_progress = 0
        self._entries_since_progress = 0
        self._next_progress_at = now + max(0.1, float(self.config.progress_seconds))
        return None

    def _inventory(
        self,
    ) -> tuple[dict[str, os.stat_result], list[dict[str, Any]], int]:
        inventory: dict[str, os.stat_result] = {}
        errors: list[dict[str, Any]] = []
        entries = 0
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

        def walk(
            directory_fd: int,
            relative_directory: str,
            discovered_directory: os.stat_result,
            depth: int,
        ) -> None:
            nonlocal entries
            self._guard()
            initial_directory = os.fstat(directory_fd)
            initial_identity = _identity(initial_directory)
            if (
                not stat.S_ISDIR(initial_directory.st_mode)
                or initial_identity != _identity(discovered_directory)
            ):
                errors.append(self._safe_row(
                    "control_snapshot_directory_identity_changed",
                    relative_directory or ".",
                ))
                return
            iterator: Any = None
            try:
                iterator = os.scandir(directory_fd)
            except Exception as exc:
                errors.append(self._safe_row(
                    "control_snapshot_enumeration_failed",
                    relative_directory or ".",
                    error=exc,
                ))
                return
            try:
                for entry in iterator:
                    self._guard()
                    entries += 1
                    self.entries_observed += 1
                    self._entries_since_progress += 1
                    progress_error = self._progress()
                    if progress_error:
                        errors.append(self._safe_row(
                            "control_snapshot_progress_failed",
                            relative_directory or ".",
                        ))
                    relative = (
                        f"{relative_directory}/{entry.name}"
                        if relative_directory
                        else entry.name
                    )
                    if entries > int(self.config.max_entries):
                        errors.append(self._safe_row(
                            "control_snapshot_entry_cap_exceeded",
                            relative,
                            maximum=int(self.config.max_entries),
                        ))
                        return
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except Exception as exc:
                        errors.append(self._safe_row(
                            "control_snapshot_entry_stat_failed",
                            relative,
                            error=exc,
                        ))
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        errors.append(self._safe_row(
                            "control_snapshot_symlink_rejected", relative
                        ))
                    elif stat.S_ISDIR(info.st_mode):
                        if depth + 1 > int(self.config.max_depth):
                            errors.append(self._safe_row(
                                "control_snapshot_depth_cap_exceeded",
                                relative,
                                maximum=int(self.config.max_depth),
                            ))
                        else:
                            child_fd = -1
                            try:
                                child_fd = os.open(
                                    entry.name,
                                    directory_flags,
                                    dir_fd=directory_fd,
                                )
                                opened_child = os.fstat(child_fd)
                                if _identity(opened_child) != _identity(info):
                                    raise OSError(
                                        "control snapshot directory changed before open"
                                    )
                                walk(child_fd, relative, opened_child, depth + 1)
                                final_entry = os.stat(
                                    entry.name,
                                    dir_fd=directory_fd,
                                    follow_symlinks=False,
                                )
                                if _identity(final_entry) != _identity(info):
                                    raise OSError(
                                        "control snapshot directory entry changed"
                                    )
                            except Exception as exc:
                                if isinstance(
                                    exc,
                                    (_ControlSnapshotDeadline, _ControlSnapshotDiskReserve),
                                ):
                                    raise
                                errors.append(self._safe_row(
                                    "control_snapshot_directory_open_failed",
                                    relative,
                                    error=exc,
                                ))
                            finally:
                                if child_fd >= 0:
                                    os.close(child_fd)
                    elif stat.S_ISREG(info.st_mode):
                        inventory[relative] = info
                        if len(inventory) > int(self.config.max_files):
                            errors.append(self._safe_row(
                                "control_snapshot_file_cap_exceeded",
                                relative,
                                maximum=int(self.config.max_files),
                            ))
                            return
                    else:
                        errors.append(self._safe_row(
                            "control_snapshot_special_file_rejected",
                            relative,
                            mode=oct(stat.S_IMODE(info.st_mode)),
                        ))
                final_directory = os.fstat(directory_fd)
                if _identity(final_directory) != initial_identity:
                    errors.append(self._safe_row(
                        "control_snapshot_directory_changed_during_inventory",
                        relative_directory or ".",
                    ))
            finally:
                if iterator is not None:
                    iterator.close()

        if self.source_root_fd < 0 or self.source_root_identity is None:
            raise OSError("control snapshot source root is not pinned")
        opened_root = os.fstat(self.source_root_fd)
        walk(self.source_root_fd, "", opened_root, 0)
        self._validate_source_root()
        return inventory, errors, entries

    def _validate_source_root(self) -> None:
        if self.source_root_fd < 0 or self.source_root_identity is None:
            raise OSError("control snapshot source root descriptor is unavailable")
        final_fd = os.fstat(self.source_root_fd)
        final_path = self.source_root.lstat()
        if (
            _identity(final_fd) != self.source_root_identity
            or _identity(final_path) != self.source_root_identity
        ):
            raise _ControlSnapshotSourceChanged(
                "control snapshot source root identity changed"
            )

    def _open_source_parent(self, relative: str) -> tuple[int, str]:
        parts = Path(relative).parts
        if (
            not parts
            or Path(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise OSError("control snapshot relative path is invalid")
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_fd = os.dup(self.source_root_fd)
        try:
            for component in parts[:-1]:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child_fd
            return parent_fd, parts[-1]
        except Exception:
            os.close(parent_fd)
            raise

    def _copy_one(self, relative: str, discovered: os.stat_result) -> dict[str, Any]:
        destination = self.snapshot_root / relative
        if int(discovered.st_uid) != os.getuid() or int(discovered.st_nlink) != 1:
            raise OSError("control evidence must be owned by the campaign uid with one link")
        size = int(discovered.st_size)
        if size > int(self.config.max_file_bytes):
            raise OSError("control evidence file size hard cap exceeded")
        if self.io_bytes_read + (2 * size) > int(self.config.max_total_io_bytes):
            raise OSError("control snapshot total I/O byte hard cap exceeded")
        self._guard(additional_bytes=size)
        source_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        parent_fd = -1
        source_fd = -1
        destination_fd = -1
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            parent_fd, source_name = self._open_source_parent(relative)
            source_fd = os.open(source_name, source_flags, dir_fd=parent_fd)
            initial = os.fstat(source_fd)
            initial_identity = _identity(initial)
            if not stat.S_ISREG(initial.st_mode) or initial_identity != _identity(discovered):
                raise OSError("control evidence changed between inventory and open")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination.parent, 0o700)
            self._guard(additional_bytes=size)
            destination_fd = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(destination_fd, 0o600)
            digest = hashlib.sha256()
            remaining = int(initial.st_size)
            copied = 0
            while remaining > 0:
                self._guard()
                block = os.read(
                    source_fd,
                    min(max(1, int(self.config.chunk_bytes)), remaining),
                )
                if not block:
                    raise OSError("control evidence truncated during snapshot")
                if self.io_bytes_read + len(block) > int(
                    self.config.max_total_io_bytes
                ):
                    raise OSError("control snapshot total I/O byte hard cap exceeded")
                digest.update(block)
                view = memoryview(block)
                while view:
                    self._guard(additional_bytes=len(view))
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise OSError("short write while snapshotting control evidence")
                    view = view[written:]
                amount = len(block)
                copied += amount
                remaining -= amount
                self.bytes_copied += amount
                self.io_bytes_read += amount
                self._bytes_since_progress += amount
                progress_error = self._progress()
                if progress_error:
                    raise OSError(progress_error)
            if os.read(source_fd, 1):
                raise OSError("control evidence appended during snapshot")
            os.lseek(source_fd, 0, os.SEEK_SET)
            verification_digest = hashlib.sha256()
            remaining = int(initial.st_size)
            while remaining > 0:
                self._guard()
                block = os.read(
                    source_fd,
                    min(max(1, int(self.config.chunk_bytes)), remaining),
                )
                if not block:
                    raise OSError("control evidence truncated during verification")
                if self.io_bytes_read + len(block) > int(
                    self.config.max_total_io_bytes
                ):
                    raise OSError("control snapshot total I/O byte hard cap exceeded")
                verification_digest.update(block)
                amount = len(block)
                remaining -= amount
                self.verification_bytes += amount
                self.io_bytes_read += amount
                self._bytes_since_progress += amount
                progress_error = self._progress()
                if progress_error:
                    raise OSError(progress_error)
            if os.read(source_fd, 1):
                raise OSError("control evidence appended during verification")
            if digest.digest() != verification_digest.digest():
                raise OSError("control evidence content changed during snapshot")
            final_fd = os.fstat(source_fd)
            final_entry = os.stat(
                source_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                _identity(final_fd) != initial_identity
                or _identity(final_entry) != initial_identity
            ):
                raise OSError("control evidence changed during snapshot")
            os.fsync(destination_fd)
            os.close(destination_fd)
            destination_fd = -1
            os.replace(temporary, destination)
            destination_parent_fd = os.open(
                destination.parent,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(destination_parent_fd)
            finally:
                os.close(destination_parent_fd)
            destination_info = destination.lstat()
            if (
                not stat.S_ISREG(destination_info.st_mode)
                or int(destination_info.st_size) != copied
                or int(destination_info.st_uid) != os.getuid()
                or int(destination_info.st_nlink) != 1
                or stat.S_IMODE(destination_info.st_mode) != 0o600
            ):
                raise OSError("control snapshot destination verification failed")
            return {
                "relative_path_sha256": _path_digest(relative),
                "source_identity": list(initial_identity),
                "size_bytes": copied,
                "sha256": digest.hexdigest(),
                "snapshot_mode": "0o600",
            }
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if parent_fd >= 0:
                os.close(parent_fd)
            if destination_fd >= 0:
                os.close(destination_fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def run(self) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        converged = False
        rounds = 0
        if (
            self.source_root == self.snapshot_root
            or self.source_root in self.snapshot_root.parents
            or self.snapshot_root in self.source_root.parents
        ):
            errors.append(self._safe_row(
                "control_snapshot_roots_overlap", self.source_root
            ))
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            source_info = self.source_root.lstat()
            if (
                not stat.S_ISDIR(source_info.st_mode)
                or stat.S_ISLNK(source_info.st_mode)
                or int(source_info.st_uid) != os.getuid()
                or stat.S_IMODE(source_info.st_mode) & 0o077
            ):
                raise OSError(
                    "control root must be a private real directory owned by the campaign uid"
                )
            self.source_root_fd = os.open(self.source_root, directory_flags)
            opened_source = os.fstat(self.source_root_fd)
            if _identity(opened_source) != _identity(source_info):
                raise OSError("control root changed while it was being pinned")
            self.source_root_identity = _identity(opened_source)
        except Exception as exc:
            errors.append(self._safe_row(
                "control_snapshot_source_invalid", self.source_root, error=exc
            ))
        if self.snapshot_root.exists() or self.snapshot_root.is_symlink():
            errors.append(self._safe_row(
                "control_snapshot_destination_exists", self.snapshot_root
            ))
        if errors:
            if self.source_root_fd >= 0:
                os.close(self.source_root_fd)
                self.source_root_fd = -1
            return self._result(errors=errors, rounds=rounds, converged=False)
        self.snapshot_root.mkdir(parents=True, mode=0o700)
        os.chmod(self.snapshot_root, 0o700)
        last_round_errors: list[dict[str, Any]] = []
        try:
            root_snapshot_info = self.snapshot_root.lstat()
            if (
                not stat.S_ISDIR(root_snapshot_info.st_mode)
                or stat.S_ISLNK(root_snapshot_info.st_mode)
                or int(root_snapshot_info.st_uid) != os.getuid()
                or stat.S_IMODE(root_snapshot_info.st_mode) != 0o700
            ):
                raise OSError("snapshot root is not an owned private directory")
            self._guard()
            while rounds < int(self.config.max_rounds):
                self._guard()
                rounds += 1
                try:
                    inventory, inventory_errors, _entries = self._inventory()
                except Exception as exc:
                    last_round_errors = [self._guard_error(exc, self.source_root)]
                    break
                last_round_errors = list(inventory_errors)
                projected = sum(int(info.st_size) for info in inventory.values())
                if projected > int(self.config.max_total_bytes):
                    last_round_errors.append(self._safe_row(
                        "control_snapshot_total_byte_cap_exceeded",
                        self.source_root,
                        projected_bytes=projected,
                        maximum=int(self.config.max_total_bytes),
                    ))
                changed_bytes = sum(
                    int(info.st_size)
                    for relative, info in inventory.items()
                    if self.records.get(relative, {}).get("source_identity")
                    != list(_identity(info))
                )
                try:
                    self._check_disk_reserve(additional_bytes=changed_bytes)
                except Exception as exc:
                    last_round_errors.append(
                        self._guard_error(exc, self.snapshot_root)
                    )
                if last_round_errors:
                    break
                for stale in sorted(set(self.records) - set(inventory)):
                    destination = self.snapshot_root / stale
                    try:
                        stale_info = destination.lstat()
                        if not stat.S_ISREG(stale_info.st_mode):
                            raise OSError("stale snapshot entry is not a regular file")
                        destination.unlink()
                    except FileNotFoundError:
                        pass
                    except Exception as exc:
                        last_round_errors.append(self._safe_row(
                            "control_snapshot_stale_remove_failed",
                            stale,
                            error=exc,
                        ))
                    self.records.pop(stale, None)
                for relative, discovered in sorted(inventory.items()):
                    if self.records.get(relative, {}).get("source_identity") == list(
                        _identity(discovered)
                    ):
                        continue
                    try:
                        self.records[relative] = self._copy_one(relative, discovered)
                    except Exception as exc:
                        last_round_errors.append(self._guard_error(exc, relative))
                        if isinstance(
                            exc,
                            (_ControlSnapshotDeadline, _ControlSnapshotDiskReserve),
                        ):
                            break
                try:
                    final_inventory, final_errors, _entries = self._inventory()
                    last_round_errors.extend(final_errors)
                except Exception as exc:
                    last_round_errors.append(
                        self._guard_error(exc, self.source_root)
                    )
                    break
                expected_identities = {
                    relative: list(_identity(info))
                    for relative, info in final_inventory.items()
                }
                captured_identities = {
                    relative: row.get("source_identity")
                    for relative, row in self.records.items()
                }
                if not last_round_errors and captured_identities == expected_identities:
                    self._validate_source_root()
                    converged = True
                    break
            if not converged:
                errors.extend(last_round_errors or [self._safe_row(
                    "control_snapshot_did_not_converge",
                    self.source_root,
                    rounds=rounds,
                )])
        except Exception as exc:
            errors.append(self._guard_error(exc, self.source_root))
        finally:
            if self.source_root_fd >= 0:
                try:
                    os.close(self.source_root_fd)
                finally:
                    self.source_root_fd = -1
        errors.extend(self.permanent_errors)
        result = self._result(errors=errors, rounds=rounds, converged=converged)
        try:
            manifest_sha256 = self._write_manifest(result)
        except Exception as exc:
            errors.append(self._safe_row(
                "control_snapshot_manifest_verification_failed",
                self.snapshot_root / "control_snapshot_manifest.json",
                error=exc,
            ))
            return self._result(errors=errors, rounds=rounds, converged=False)
        result["manifest_path_sha256"] = _path_digest(
            self.snapshot_root / "control_snapshot_manifest.json"
        )
        result["manifest_sha256"] = manifest_sha256
        result["manifest_readback_verified"] = True
        return result

    def _write_manifest(self, result: Mapping[str, Any]) -> str:
        manifest_path = self.snapshot_root / "control_snapshot_manifest.json"
        manifest = dict(result)
        manifest["manifest_path_sha256"] = _path_digest(manifest_path)
        manifest["manifest_readback_required"] = True
        content = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        self._guard(additional_bytes=len(content))
        temporary = manifest_path.with_name(
            f".{manifest_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            view = memoryview(content)
            while view:
                self._guard(additional_bytes=len(view))
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short control snapshot manifest write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, manifest_path)
            root_fd = os.open(
                self.snapshot_root,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            path_info = manifest_path.lstat()
            if (
                not stat.S_ISREG(path_info.st_mode)
                or stat.S_ISLNK(path_info.st_mode)
                or int(path_info.st_uid) != os.getuid()
                or int(path_info.st_nlink) != 1
                or stat.S_IMODE(path_info.st_mode) != 0o600
                or int(path_info.st_size) != len(content)
            ):
                raise OSError("control snapshot manifest metadata verification failed")
            descriptor = os.open(
                manifest_path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened_info = os.fstat(descriptor)
            if _identity(opened_info) != _identity(path_info):
                raise OSError("control snapshot manifest changed before readback")
            readback = bytearray()
            while len(readback) <= len(content):
                self._guard()
                block = os.read(
                    descriptor,
                    min(1024 * 1024, len(content) + 1 - len(readback)),
                )
                if not block:
                    break
                readback.extend(block)
            if bytes(readback) != content:
                raise OSError("control snapshot manifest readback mismatch")
            final_info = os.fstat(descriptor)
            final_path_info = manifest_path.lstat()
            if (
                _identity(final_info) != _identity(path_info)
                or _identity(final_path_info) != _identity(path_info)
            ):
                raise OSError("control snapshot manifest changed during readback")
            return hashlib.sha256(content).hexdigest()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _result(
        self,
        *,
        errors: list[dict[str, Any]],
        rounds: int,
        converged: bool,
    ) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_SNAPSHOT_SCHEMA_VERSION,
            "ok": bool(converged and not errors),
            "source_root_sha256": _path_digest(self.source_root),
            "snapshot_root_sha256": _path_digest(self.snapshot_root),
            "converged": bool(converged),
            "rounds": int(rounds),
            "files": len(self.records),
            "bytes": sum(int(row.get("size_bytes") or 0) for row in self.records.values()),
            "copy_io_bytes": self.bytes_copied,
            "verification_bytes": self.verification_bytes,
            "io_bytes_read": self.io_bytes_read,
            "entries_observed": self.entries_observed,
            "progress_events": self.progress_events,
            "files_inventory": [self.records[name] for name in sorted(self.records)],
            "errors": errors,
            "error_count": len(errors),
            "elapsed_seconds": round(
                time.monotonic() - self.started_monotonic, 6
            ),
            "limits": {
                "max_file_bytes": int(self.config.max_file_bytes),
                "max_total_bytes": int(self.config.max_total_bytes),
                "max_total_io_bytes": int(self.config.max_total_io_bytes),
                "max_entries": int(self.config.max_entries),
                "max_files": int(self.config.max_files),
                "max_depth": int(self.config.max_depth),
                "max_rounds": int(self.config.max_rounds),
                "max_seconds": float(self.config.max_seconds),
                "minimum_free_reserve_bytes": int(
                    self.config.minimum_free_reserve_bytes
                ),
            },
            "captured_at": time.time(),
        }


def snapshot_control_evidence(
    config: ControlSnapshotConfig,
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Create a private immutable copy of a live external control tree."""

    return _ControlSnapshot(config, progress_callback).run()
