"""Artifact indexing and fail-closed validation for formal campaigns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
from typing import Any, BinaryIO, Collection, Iterable, Mapping
import zipfile

from scripts.testing.campaign_contract import (
    SCENARIO_ROLLUP_SCHEMA_VERSION,
    ContractValidationError,
    ScenarioContract,
    ScenarioResult,
    build_formal_rollup,
)


ARTIFACT_RECORD_SCHEMA_VERSION = "hackme.campaign.artifact-record/v1"
ARTIFACT_INDEX_SCHEMA_VERSION = "hackme.campaign.artifact-index/v2"
SECRET_SCAN_SCHEMA_VERSION = "hackme.campaign.secret-scan/v1"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I)),
    ("bearer_token", re.compile(rb"Authorization\s*:\s*Bearer\s+[A-Za-z0-9_.-]{12,}", re.I)),
    ("openai_style_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{12,}")),
    ("github_token", re.compile(rb"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{12,}")),
    ("slack_token", re.compile(rb"\bxox[bp]-[A-Za-z0-9-]{12,}")),
)
_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".tar.zst",
    ".tzst",
    ".7z",
)
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts"}


class ArtifactValidationError(ValueError):
    """Raised for malformed artifact declarations."""


class ArtifactType(str, Enum):
    AUTO = "auto"
    JSON = "json"
    JSONL = "jsonl"
    IMAGE = "image"
    VIDEO = "video"
    ARCHIVE = "archive"
    SQLITE = "sqlite"
    TEXT = "text"
    BINARY = "binary"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value.strip()):
        raise ArtifactValidationError(f"{label} must match {_IDENTIFIER.pattern!r}")
    return value.strip()


@dataclass(frozen=True)
class ArtifactSpec:
    """Declaration linking one expected artifact to one reviewed scenario."""

    artifact_id: str
    scenario_id: str
    path: Path
    artifact_type: ArtifactType = ArtifactType.AUTO
    mandatory: bool = True
    expected_sha256: str = ""
    minimum_size_bytes: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "scenario_id", _identifier(self.scenario_id, "scenario_id"))
        object.__setattr__(self, "path", Path(self.path).expanduser())
        try:
            object.__setattr__(self, "artifact_type", ArtifactType(self.artifact_type))
        except (TypeError, ValueError) as exc:
            raise ArtifactValidationError(f"unsupported artifact_type: {self.artifact_type!r}") from exc
        if type(self.mandatory) is not bool:
            raise ArtifactValidationError("mandatory must be boolean")
        digest = str(self.expected_sha256 or "").lower()
        if digest and not _SHA256.fullmatch(digest):
            raise ArtifactValidationError("expected_sha256 must be empty or 64 lowercase hex characters")
        object.__setattr__(self, "expected_sha256", digest)
        if (
            isinstance(self.minimum_size_bytes, bool)
            or not isinstance(self.minimum_size_bytes, int)
            or self.minimum_size_bytes < 1
        ):
            raise ArtifactValidationError("minimum_size_bytes must be a positive integer")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_artifact_type(path: Path) -> ArtifactType:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix == ".json":
        return ArtifactType.JSON
    if suffix in {".jsonl", ".ndjson"}:
        return ArtifactType.JSONL
    if suffix in _IMAGE_SUFFIXES:
        return ArtifactType.IMAGE
    if suffix in _VIDEO_SUFFIXES:
        return ArtifactType.VIDEO
    if any(name.endswith(archive_suffix) for archive_suffix in _ARCHIVE_SUFFIXES):
        return ArtifactType.ARCHIVE
    if suffix in {".db", ".db3", ".sqlite", ".sqlite3"}:
        return ArtifactType.SQLITE
    if suffix in {".txt", ".log", ".csv", ".html", ".xml", ".har", ".vtt", ".srt", ".m3u8"}:
        return ArtifactType.TEXT
    return ArtifactType.BINARY


def _nonempty_json(value: object) -> bool:
    return value not in (None, "", [], {})


def _format_result(
    *,
    ok: bool,
    method: str,
    details: Mapping[str, Any] | None = None,
    errors: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "method": method,
        "details": dict(details or {}),
        "errors": list(errors),
    }


def _validate_json(path: Path) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _format_result(ok=False, method="json.loads", errors=[f"{type(exc).__name__}: {exc}"]), []
    nonempty = _nonempty_json(payload)
    details = {"top_level_type": type(payload).__name__, "semantic_nonempty": nonempty}
    return _format_result(
        ok=nonempty,
        method="json.loads",
        details=details,
        errors=[] if nonempty else ["empty_json_payload"],
    ), []


def _validate_jsonl(path: Path) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    errors: list[str] = []
    record_count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record_count += 1
            try:
                payload = json.loads(line)
                if not _nonempty_json(payload):
                    errors.append(f"line_{line_number}:empty_json_payload")
            except Exception as exc:
                errors.append(f"line_{line_number}:{type(exc).__name__}")
    if not record_count:
        errors.append("empty_jsonl_payload")
    return _format_result(
        ok=not errors,
        method="linewise_json.loads",
        details={"record_count": record_count},
        errors=errors,
    ), []


def _validate_image(path: Path) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    try:
        from PIL import Image
    except Exception:
        return _format_result(ok=False, method="Pillow.Image.verify", errors=["image_validator_unavailable"]), []
    try:
        with Image.open(path) as image:
            image_format = image.format or ""
            width, height = image.size
            metadata = {
                "info": {str(key): str(value) for key, value in image.info.items()},
                "exif": {str(key): str(value) for key, value in image.getexif().items()},
            }
        # Metadata access may move Pillow's decoder cursor, so verification
        # intentionally uses a fresh decoder instance.
        with Image.open(path) as image:
            image.verify()
        metadata_bytes = json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ok = bool(image_format and width > 0 and height > 0)
        return _format_result(
            ok=ok,
            method="Pillow.Image.verify",
            details={"format": image_format, "width": width, "height": height},
            errors=[] if ok else ["invalid_image_dimensions_or_format"],
        ), [("image_metadata", metadata_bytes)]
    except Exception as exc:
        return _format_result(ok=False, method="Pillow.Image.verify", errors=[f"{type(exc).__name__}: {exc}"]), []


def _positive_duration(metadata: Mapping[str, Any]) -> float:
    candidates: list[object] = [(metadata.get("format") or {}).get("duration")]
    candidates.extend(stream.get("duration") for stream in metadata.get("streams") or [])
    for value in candidates:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    return 0.0


def _extract_subtitle_sources(path: Path, metadata: Mapping[str, Any]) -> tuple[list[tuple[str, bytes]], list[str]]:
    subtitle_count = sum(1 for stream in metadata.get("streams") or [] if stream.get("codec_type") == "subtitle")
    if not subtitle_count:
        return [], []
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return [], ["subtitle_secret_scanner_unavailable"]
    sources: list[tuple[str, bytes]] = []
    errors: list[str] = []
    for ordinal in range(subtitle_count):
        try:
            completed = subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-map",
                    f"0:s:{ordinal}",
                    "-f",
                    "srt",
                    "-",
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:
            errors.append(f"subtitle_{ordinal}:{type(exc).__name__}")
            continue
        if completed.returncode != 0:
            errors.append(f"subtitle_{ordinal}:ffmpeg_exit_{completed.returncode}")
            continue
        sources.append((f"video_subtitle_{ordinal}", completed.stdout))
    return sources, errors


def _validate_video(path: Path) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return _format_result(ok=False, method="ffprobe", errors=["video_validator_unavailable"]), []
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        return _format_result(ok=False, method="ffprobe", errors=[f"{type(exc).__name__}: {exc}"]), []
    if completed.returncode != 0:
        return _format_result(ok=False, method="ffprobe", errors=[f"ffprobe_exit_{completed.returncode}"]), []
    try:
        metadata = json.loads(completed.stdout)
    except Exception as exc:
        return _format_result(ok=False, method="ffprobe", errors=[f"invalid_ffprobe_json:{type(exc).__name__}"]), []
    streams = metadata.get("streams") if isinstance(metadata, dict) else []
    streams = streams if isinstance(streams, list) else []
    video_streams = sum(1 for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video")
    duration = _positive_duration(metadata)
    subtitle_sources, subtitle_errors = _extract_subtitle_sources(path, metadata)
    errors = list(subtitle_errors)
    if not video_streams:
        errors.append("video_stream_missing")
    if duration <= 0:
        errors.append("positive_duration_missing")
    metadata_bytes = json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _format_result(
        ok=not errors,
        method="ffprobe",
        details={
            "stream_count": len(streams),
            "video_stream_count": video_streams,
            "subtitle_stream_count": sum(1 for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "subtitle"),
            "duration_seconds": duration,
        },
        errors=errors,
    ), [("ffprobe_metadata", metadata_bytes), *subtitle_sources]


def _unsafe_archive_name(name: str) -> bool:
    path = Path(name.replace("\\", "/"))
    return path.is_absolute() or ".." in path.parts


def _archive_member_reference(name: str) -> str:
    """Identify a member without copying a possibly sensitive filename."""

    return hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:16]


def _validate_archive(path: Path) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    errors: list[str] = []
    member_count = 0
    regular_count = 0
    unpacked_bytes = 0
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                member_count = len(infos)
                regular = [info for info in infos if not info.is_dir()]
                regular_count = len(regular)
                unpacked_bytes = sum(info.file_size for info in regular)
                if any(_unsafe_archive_name(info.filename) for info in infos):
                    errors.append("unsafe_archive_member_path")
                bad_member = archive.testzip()
                if bad_member:
                    errors.append("archive_crc_failure")
            method = "zipfile.testzip"
        elif tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as archive:
                members = archive.getmembers()
                member_count = len(members)
                regular = [member for member in members if member.isfile()]
                regular_count = len(regular)
                unpacked_bytes = sum(member.size for member in regular)
                if any(_unsafe_archive_name(member.name) for member in members):
                    errors.append("unsafe_archive_member_path")
                for member in regular:
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        errors.append(f"unreadable_archive_member:{_archive_member_reference(member.name)}")
                        continue
                    for _chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                        pass
            method = "tarfile.full_read"
        else:
            return _format_result(ok=False, method="archive_probe", errors=["unsupported_or_corrupt_archive"]), []
    except Exception as exc:
        return _format_result(ok=False, method="archive_probe", errors=[f"{type(exc).__name__}: {exc}"]), []
    if not regular_count:
        errors.append("archive_has_no_regular_files")
    if unpacked_bytes <= 0:
        errors.append("archive_payload_is_empty")
    return _format_result(
        ok=not errors,
        method=method,
        details={
            "member_count": member_count,
            "regular_file_count": regular_count,
            "unpacked_bytes": unpacked_bytes,
        },
        errors=errors,
    ), []


def _validate_sqlite(path: Path) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    rows: list[str] = []
    object_count = 0
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
            object_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            )
        finally:
            connection.close()
    except Exception as exc:
        return _format_result(ok=False, method="sqlite PRAGMA quick_check", errors=[f"{type(exc).__name__}: {exc}"]), []
    errors: list[str] = []
    if rows != ["ok"]:
        errors.append("sqlite_quick_check_failed")
    if object_count <= 0:
        errors.append("sqlite_schema_is_empty")
    return _format_result(
        ok=not errors,
        method="sqlite PRAGMA quick_check",
        details={"quick_check": rows, "schema_object_count": object_count},
        errors=errors,
    ), []


def _validate_text(path: Path) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return _format_result(ok=False, method="utf-8 decode", errors=[f"{type(exc).__name__}: {exc}"]), []
    ok = bool(text.strip())
    return _format_result(
        ok=ok,
        method="utf-8 decode",
        details={"character_count": len(text)},
        errors=[] if ok else ["text_payload_is_empty"],
    ), []


def validate_artifact_format(path: Path, artifact_type: ArtifactType) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    validators = {
        ArtifactType.JSON: _validate_json,
        ArtifactType.JSONL: _validate_jsonl,
        ArtifactType.IMAGE: _validate_image,
        ArtifactType.VIDEO: _validate_video,
        ArtifactType.ARCHIVE: _validate_archive,
        ArtifactType.SQLITE: _validate_sqlite,
        ArtifactType.TEXT: _validate_text,
    }
    if artifact_type is ArtifactType.BINARY:
        return _format_result(ok=True, method="non-empty binary", details={"size": path.stat().st_size}), []
    return validators[artifact_type](path)


def _secret_patterns(known_secret_values: Mapping[str, str] | None) -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    patterns = list(_DEFAULT_SECRET_PATTERNS)
    for label, value in sorted((known_secret_values or {}).items()):
        if not isinstance(value, str) or not value:
            continue
        label_text = str(label)
        if not _IDENTIFIER.fullmatch(label_text):
            label_text = f"label_{hashlib.sha256(label_text.encode('utf-8')).hexdigest()[:12]}"
        patterns.append((f"known_secret:{label_text}", re.compile(re.escape(value.encode("utf-8")))))
    return tuple(patterns)


def _scan_stream(
    handle: BinaryIO,
    source: str,
    patterns: tuple[tuple[str, re.Pattern[bytes]], ...],
) -> tuple[list[dict[str, Any]], int]:
    findings: list[dict[str, Any]] = []
    found_patterns: set[str] = set()
    tail = b""
    offset = 0
    overlap = 4096
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        window = tail + chunk
        base_offset = max(0, offset - len(tail))
        for name, pattern in patterns:
            if name in found_patterns:
                continue
            match = pattern.search(window)
            if match:
                findings.append({"source": source, "pattern": name, "byte_offset": base_offset + match.start()})
                found_patterns.add(name)
        offset += len(chunk)
        tail = window[-overlap:]
    return findings, offset


def _scan_archive_members(
    path: Path,
    patterns: tuple[tuple[str, re.Pattern[bytes]], ...],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    findings: list[dict[str, Any]] = []
    scanned_bytes = 0
    errors: list[str] = []
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    try:
                        with archive.open(info) as handle:
                            reference = _archive_member_reference(info.filename)
                            hits, count = _scan_stream(handle, f"archive_member:{reference}", patterns)
                        findings.extend(hits)
                        scanned_bytes += count
                    except Exception as exc:
                        reference = _archive_member_reference(info.filename)
                        errors.append(f"archive_member_scan:{reference}:{type(exc).__name__}")
        elif tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    handle = archive.extractfile(member)
                    if handle is None:
                        reference = _archive_member_reference(member.name)
                        errors.append(f"archive_member_scan:{reference}:unreadable")
                        continue
                    with handle:
                        reference = _archive_member_reference(member.name)
                        hits, count = _scan_stream(handle, f"archive_member:{reference}", patterns)
                    findings.extend(hits)
                    scanned_bytes += count
        else:
            errors.append("archive_member_scan:unsupported_or_corrupt")
    except Exception as exc:
        errors.append(f"archive_member_scan:{type(exc).__name__}")
    return findings, scanned_bytes, errors


def scan_artifact_secrets(
    path: Path,
    artifact_type: ArtifactType,
    *,
    known_secret_values: Mapping[str, str] | None = None,
    extra_sources: Iterable[tuple[str, bytes]] = (),
) -> dict[str, Any]:
    """Scan raw bytes, extracted archive members, and parser metadata.

    Findings contain only pattern labels and byte offsets; matched credentials
    are never copied into the machine-readable report.
    """

    patterns = _secret_patterns(known_secret_values)
    findings: list[dict[str, Any]] = []
    collector_errors: list[str] = []
    scanned_bytes = 0
    sources = 0
    try:
        with path.open("rb") as handle:
            hits, count = _scan_stream(handle, "artifact", patterns)
        findings.extend(hits)
        scanned_bytes += count
        sources += 1
    except Exception as exc:
        collector_errors.append(f"artifact_scan:{type(exc).__name__}")
    if artifact_type is ArtifactType.ARCHIVE:
        hits, count, errors = _scan_archive_members(path, patterns)
        findings.extend(hits)
        scanned_bytes += count
        collector_errors.extend(errors)
        sources += 1
    for source, payload in extra_sources:
        try:
            hits, count = _scan_stream(io.BytesIO(payload), source, patterns)
            findings.extend(hits)
            scanned_bytes += count
            sources += 1
        except Exception as exc:
            collector_errors.append(f"{source}:{type(exc).__name__}")
    return {
        "schema_version": SECRET_SCAN_SCHEMA_VERSION,
        "performed": True,
        "coverage_complete": not collector_errors,
        "ok": not findings and not collector_errors,
        "scanned_bytes": scanned_bytes,
        "source_count": sources,
        "pattern_count": len(patterns),
        "finding_count": len(findings),
        "findings": findings[:100],
        "collector_errors": collector_errors,
    }


def _failed_secret_scan(error: str) -> dict[str, Any]:
    return {
        "schema_version": SECRET_SCAN_SCHEMA_VERSION,
        "performed": False,
        "coverage_complete": False,
        "ok": False,
        "scanned_bytes": 0,
        "source_count": 0,
        "pattern_count": len(_DEFAULT_SECRET_PATTERNS),
        "finding_count": 0,
        "findings": [],
        "collector_errors": [error],
    }


def validate_artifact(
    spec: ArtifactSpec,
    *,
    known_scenario_ids: Collection[str],
    artifact_root: Path | None = None,
    known_secret_values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate one artifact and return a complete, non-secret-bearing record."""

    declared_path = spec.path
    path = declared_path.resolve(strict=False)
    artifact_type = infer_artifact_type(path) if spec.artifact_type is ArtifactType.AUTO else spec.artifact_type
    errors: list[str] = []
    scenario_link_valid = spec.scenario_id in set(known_scenario_ids)
    if not scenario_link_valid:
        errors.append("unknown_scenario_id")
    within_artifact_root = True
    if artifact_root is not None:
        root = Path(artifact_root).expanduser().resolve(strict=False)
        within_artifact_root = path != root and root in path.parents
        if not within_artifact_root:
            errors.append("artifact_outside_artifact_root")
    if declared_path.is_symlink():
        errors.append("symlink_artifact_rejected")
    exists = path.exists()
    is_file = path.is_file() if exists else False
    if not exists:
        errors.append("artifact_missing")
    elif not is_file:
        errors.append("artifact_not_regular_file")
    stat_before = None
    if is_file:
        try:
            stat_before = path.stat()
        except Exception as exc:
            errors.append(f"artifact_stat_failed:{type(exc).__name__}")
    size = stat_before.st_size if stat_before is not None else 0
    nonzero = size >= spec.minimum_size_bytes
    if is_file and not nonzero:
        errors.append("artifact_below_minimum_size")
    digest = ""
    if is_file:
        try:
            digest = sha256_file(path)
        except Exception as exc:
            errors.append(f"sha256_collection_failed:{type(exc).__name__}")
    hash_verified = bool(digest) and (not spec.expected_sha256 or digest == spec.expected_sha256)
    if spec.expected_sha256 and digest != spec.expected_sha256:
        errors.append("sha256_mismatch")
    if is_file and nonzero:
        try:
            format_validation, extra_sources = validate_artifact_format(path, artifact_type)
        except Exception as exc:  # pragma: no cover - final fail-closed guard
            format_validation = _format_result(
                ok=False,
                method="validator_dispatch",
                errors=[f"{type(exc).__name__}: {exc}"],
            )
            extra_sources = []
        secret_scan = scan_artifact_secrets(
            path,
            artifact_type,
            known_secret_values=known_secret_values,
            extra_sources=extra_sources,
        )
    else:
        format_validation = _format_result(ok=False, method="not_run", errors=["artifact_unreadable_or_empty"])
        secret_scan = _failed_secret_scan("artifact_unreadable_or_empty")
    stable_snapshot = False
    if stat_before is not None:
        try:
            stat_after = path.stat()
            stable_snapshot = (
                stat_before.st_dev,
                stat_before.st_ino,
                stat_before.st_size,
                stat_before.st_mtime_ns,
            ) == (
                stat_after.st_dev,
                stat_after.st_ino,
                stat_after.st_size,
                stat_after.st_mtime_ns,
            )
        except Exception as exc:
            errors.append(f"artifact_restat_failed:{type(exc).__name__}")
    if is_file and not stable_snapshot:
        errors.append("artifact_changed_during_validation")
    if not format_validation.get("ok"):
        errors.append("format_validation_failed")
    if not secret_scan.get("ok"):
        errors.append("secret_scan_failed")
    validated = bool(
        scenario_link_valid
        and within_artifact_root
        and exists
        and is_file
        and nonzero
        and stable_snapshot
        and hash_verified
        and format_validation.get("ok") is True
        and secret_scan.get("performed") is True
        and secret_scan.get("coverage_complete") is True
        and secret_scan.get("ok") is True
        and not errors
    )
    return {
        "schema_version": ARTIFACT_RECORD_SCHEMA_VERSION,
        "artifact_id": spec.artifact_id,
        "scenario_id": spec.scenario_id,
        "path": str(path),
        "created_at": (
            datetime.fromtimestamp(stat_before.st_mtime, timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
            if stat_before is not None
            else ""
        ),
        "type": artifact_type.value,
        "mandatory": spec.mandatory,
        "scenario_link_valid": scenario_link_valid,
        "within_artifact_root": within_artifact_root,
        "exists": exists,
        "regular_file": is_file,
        "size": size,
        "minimum_size_bytes": spec.minimum_size_bytes,
        "nonzero": nonzero,
        "validation_snapshot_stable": stable_snapshot,
        "sha256": digest,
        "expected_sha256": spec.expected_sha256,
        "sha256_verified": hash_verified,
        "format_validation": format_validation,
        "secret_scan": secret_scan,
        "validated": validated,
        "errors": errors,
    }


def _hash_manifest(
    records: Iterable[Mapping[str, Any]],
    scenario_contracts: Mapping[str, Any],
    scenario_result_gate: Mapping[str, Any],
) -> str:
    rows = [
        {
            "artifact_id": record.get("artifact_id"),
            "scenario_id": record.get("scenario_id"),
            "sha256": record.get("sha256"),
            "size": record.get("size"),
            "validated": record.get("validated"),
        }
        for record in records
    ]
    manifest = {
        "artifacts": rows,
        "scenario_contracts": scenario_contracts,
        "scenario_result_gate": scenario_result_gate,
    }
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_artifact_link_errors(
    results: Mapping[str, ScenarioResult],
    records: Iterable[Mapping[str, Any]],
) -> list[str]:
    records_by_id = {
        str(record.get("artifact_id")): record
        for record in records
        if isinstance(record, Mapping)
    }
    errors: list[str] = []
    for scenario_id, result in sorted(results.items()):
        for artifact_id in result.artifact_ids:
            record = records_by_id.get(artifact_id)
            if record is None:
                errors.append(f"result_artifact_missing:{scenario_id}:{artifact_id}")
                continue
            if record.get("scenario_id") != scenario_id:
                errors.append(f"result_artifact_scenario_mismatch:{scenario_id}:{artifact_id}")
            if record.get("validated") is not True:
                errors.append(f"result_artifact_not_validated:{scenario_id}:{artifact_id}")
    return errors


def _build_scenario_result_gate(
    *,
    required: bool,
    disabled_reason: str,
    contracts: Mapping[str, ScenarioContract],
    results: Mapping[str, ScenarioResult],
    known_scenario_ids: Collection[str],
    records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    if not required:
        reason = disabled_reason.strip() if isinstance(disabled_reason, str) else ""
        errors = [] if reason else ["scenario_gate_disabled_reason_missing"]
        return {
            "required": False,
            "disabled_reason": reason,
            "rollup": None,
            "result_artifact_link_errors": [],
            "ok": False,
            "errors": errors or ["scenario_result_gate_explicitly_disabled"],
        }

    errors: list[str] = []
    if not contracts:
        errors.append("scenario_contracts_missing")
    if not results:
        errors.append("scenario_results_missing")
    if set(contracts) != set(known_scenario_ids):
        errors.append("scenario_contract_ids_do_not_match_known_scenarios")
    rollup = build_formal_rollup(results, contracts)
    link_errors = _result_artifact_link_errors(results, records)
    errors.extend(link_errors)
    if rollup.get("formal_pass") is not True:
        errors.append("mandatory_scenario_rollup_not_pass")
    return {
        "required": True,
        "disabled_reason": "",
        "rollup": rollup,
        "result_artifact_link_errors": link_errors,
        "ok": not errors,
        "errors": errors,
    }


def build_artifact_index(
    *,
    campaign_uuid: str,
    commit: str,
    source_digest: str,
    artifacts: Iterable[ArtifactSpec],
    known_scenario_ids: Collection[str],
    scenario_contracts: Mapping[str, ScenarioContract] | None = None,
    scenario_results: Mapping[str, ScenarioResult] | None = None,
    require_scenario_results: bool = True,
    scenario_gate_disabled_reason: str = "",
    required_artifacts_by_scenario: Mapping[str, Collection[str]] | None = None,
    artifact_root: Path | None = None,
    known_secret_values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build an index that cannot PASS with empty or disconnected evidence."""

    specs = list(artifacts)
    known = set(known_scenario_ids)
    errors: list[str] = []
    if not isinstance(campaign_uuid, str) or not campaign_uuid.strip():
        errors.append("campaign_uuid_missing")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        errors.append("commit_invalid")
    if not isinstance(source_digest, str) or not _SHA256.fullmatch(source_digest):
        errors.append("source_digest_invalid")
    if not known:
        errors.append("known_scenarios_empty")
    if any(not isinstance(scenario_id, str) or not _IDENTIFIER.fullmatch(scenario_id) for scenario_id in known):
        errors.append("known_scenario_id_invalid")
    if not specs:
        errors.append("artifact_declarations_empty")
    artifact_ids = [spec.artifact_id for spec in specs]
    if len(set(artifact_ids)) != len(artifact_ids):
        errors.append("duplicate_artifact_ids")
    records = [
        validate_artifact(
            spec,
            known_scenario_ids=known,
            artifact_root=artifact_root,
            known_secret_values=known_secret_values,
        )
        for spec in specs
    ]
    declared_by_scenario = {
        scenario_id: {spec.artifact_id for spec in specs if spec.scenario_id == scenario_id}
        for scenario_id in sorted(known)
    }
    scenario_coverage: dict[str, Any] = {}
    contract_map = dict(scenario_contracts or {}) if isinstance(scenario_contracts, Mapping) else {}
    result_map = dict(scenario_results or {}) if isinstance(scenario_results, Mapping) else {}
    if scenario_contracts is not None and not isinstance(scenario_contracts, Mapping):
        errors.append("scenario_contracts_not_mapping")
    if scenario_results is not None and not isinstance(scenario_results, Mapping):
        errors.append("scenario_results_not_mapping")
    if type(require_scenario_results) is not bool:
        errors.append("require_scenario_results_not_boolean")
        require_scenario_results = True
    requirements = {
        scenario_id: set(artifact_ids)
        for scenario_id, artifact_ids in (required_artifacts_by_scenario or {}).items()
    }
    for scenario_id, contract in contract_map.items():
        if isinstance(contract, ScenarioContract):
            requirements.setdefault(scenario_id, set()).update(contract.artifacts)
    for scenario_id in sorted(known):
        required = set(requirements.get(scenario_id) or ())
        declared = declared_by_scenario[scenario_id]
        missing = sorted(required - declared)
        valid_ids = sorted(
            record["artifact_id"]
            for record in records
            if record["scenario_id"] == scenario_id and record["validated"] is True
        )
        covered = bool(declared) and not missing and declared <= set(valid_ids)
        scenario_coverage[scenario_id] = {
            "declared_artifact_ids": sorted(declared),
            "required_artifact_ids": sorted(required),
            "missing_required_artifact_ids": missing,
            "validated_artifact_ids": valid_ids,
            "covered": covered,
        }
        if not declared:
            errors.append(f"scenario_without_artifact:{scenario_id}")
        if missing:
            errors.append(f"scenario_missing_required_artifacts:{scenario_id}")
        if declared and declared - set(valid_ids):
            errors.append(f"scenario_has_invalid_artifacts:{scenario_id}")
    unknown_requirement_scenarios = sorted(set(requirements) - known)
    if unknown_requirement_scenarios:
        errors.append("requirements_reference_unknown_scenarios")
    validated_count = sum(record["validated"] is True for record in records)
    secret_findings = sum(int(record["secret_scan"].get("finding_count") or 0) for record in records)
    summary = {
        "artifact_count": len(records),
        "validated_count": validated_count,
        "invalid_count": len(records) - validated_count,
        "mandatory_count": sum(record["mandatory"] is True for record in records),
        "scenario_count": len(known),
        "covered_scenario_count": sum(item["covered"] is True for item in scenario_coverage.values()),
        "secret_finding_count": secret_findings,
    }
    artifact_gate_ok = bool(
        records
        and known
        and validated_count == len(records)
        and summary["covered_scenario_count"] == len(known)
        and not errors
    )
    serialized_contracts = {
        scenario_id: contract.to_dict()
        for scenario_id, contract in sorted(contract_map.items())
        if isinstance(contract, ScenarioContract)
    }
    scenario_result_gate = _build_scenario_result_gate(
        required=require_scenario_results,
        disabled_reason=scenario_gate_disabled_reason,
        contracts=contract_map,
        results=result_map,
        known_scenario_ids=known,
        records=records,
    )
    errors.extend(scenario_result_gate["errors"])
    ok = bool(artifact_gate_ok and scenario_result_gate["ok"] is True and not errors)
    return {
        "schema_version": ARTIFACT_INDEX_SCHEMA_VERSION,
        "campaign_uuid": campaign_uuid,
        "commit": commit,
        "source_digest": source_digest,
        "generated_at": utc_now(),
        "artifact_root": str(Path(artifact_root).expanduser().resolve(strict=False)) if artifact_root else "",
        "known_scenario_ids": sorted(known),
        "scenario_contracts": serialized_contracts,
        "scenario_result_gate": scenario_result_gate,
        "artifacts": records,
        "scenario_coverage": scenario_coverage,
        "summary": summary,
        "hash_manifest_sha256": _hash_manifest(
            records,
            serialized_contracts,
            scenario_result_gate,
        ),
        "artifact_gate_ok": artifact_gate_ok,
        "errors": errors,
        "ok": ok,
    }


def validate_artifact_index(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Re-parse and self-check an artifact index after it has been persisted."""

    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return {"ok": False, "schema_ok": False, "artifact_gate_ok": False, "errors": ["index_not_object"]}
    if payload.get("schema_version") != ARTIFACT_INDEX_SCHEMA_VERSION:
        errors.append("schema_version_invalid")
    records = payload.get("artifacts")
    if not isinstance(records, list) or not records:
        errors.append("artifact_records_empty_or_invalid")
        records = []
    if any(not isinstance(record, Mapping) for record in records):
        errors.append("artifact_record_not_object")
        records = [record for record in records if isinstance(record, Mapping)]
    if any(record.get("schema_version") != ARTIFACT_RECORD_SCHEMA_VERSION for record in records):
        errors.append("artifact_record_schema_invalid")
    ids = [record.get("artifact_id") for record in records]
    if any(not isinstance(artifact_id, str) or not _IDENTIFIER.fullmatch(artifact_id) for artifact_id in ids):
        errors.append("artifact_id_invalid")
    elif len(ids) != len(set(ids)):
        errors.append("duplicate_artifact_ids")
    for record in records:
        secret_scan = record.get("secret_scan")
        format_validation = record.get("format_validation")
        record_errors = record.get("errors")
        component_gate = bool(
            record.get("scenario_link_valid") is True
            and record.get("within_artifact_root") is True
            and record.get("exists") is True
            and record.get("regular_file") is True
            and record.get("nonzero") is True
            and record.get("validation_snapshot_stable") is True
            and isinstance(record.get("sha256"), str)
            and _SHA256.fullmatch(record.get("sha256"))
            and record.get("sha256_verified") is True
            and isinstance(format_validation, Mapping)
            and format_validation.get("ok") is True
            and isinstance(secret_scan, Mapping)
            and secret_scan.get("schema_version") == SECRET_SCAN_SCHEMA_VERSION
            and secret_scan.get("performed") is True
            and secret_scan.get("coverage_complete") is True
            and secret_scan.get("ok") is True
            and secret_scan.get("finding_count") == 0
            and isinstance(record_errors, list)
            and not record_errors
        )
        if record.get("validated") is not True or not component_gate:
            errors.append(f"artifact_record_gate_failed:{record.get('artifact_id', 'unknown')}")
    if any(record.get("validated") is not True for record in records):
        errors.append("artifact_validation_not_all_passed")
    scenarios = payload.get("known_scenario_ids")
    coverage = payload.get("scenario_coverage")
    if not isinstance(scenarios, list) or not scenarios:
        errors.append("known_scenarios_empty_or_invalid")
        scenarios = []
    if not isinstance(coverage, Mapping) or set(coverage) != set(scenarios):
        errors.append("scenario_coverage_shape_invalid")
        coverage = {}
    if any(not isinstance(item, Mapping) or item.get("covered") is not True for item in coverage.values()):
        errors.append("scenario_coverage_incomplete")
    summary = payload.get("summary")
    def finding_count(record: Mapping[str, Any]) -> int:
        secret_scan = record.get("secret_scan")
        if not isinstance(secret_scan, Mapping):
            return 0
        try:
            return int(secret_scan.get("finding_count") or 0)
        except (TypeError, ValueError):
            return 0

    expected_summary = {
        "artifact_count": len(records),
        "validated_count": sum(record.get("validated") is True for record in records),
        "invalid_count": sum(record.get("validated") is not True for record in records),
        "mandatory_count": sum(record.get("mandatory") is True for record in records),
        "scenario_count": len(scenarios),
        "covered_scenario_count": sum(
            isinstance(item, Mapping) and item.get("covered") is True for item in coverage.values()
        ),
        "secret_finding_count": sum(finding_count(record) for record in records),
    }
    if summary != expected_summary:
        errors.append("summary_mismatch")
    artifact_component_error_count = len(errors)

    serialized_contracts = payload.get("scenario_contracts")
    if not isinstance(serialized_contracts, Mapping):
        errors.append("scenario_contracts_shape_invalid")
        serialized_contracts = {}
    contracts: dict[str, ScenarioContract] = {}
    for scenario_id, contract_payload in serialized_contracts.items():
        try:
            contract = ScenarioContract.from_dict(contract_payload)
        except (ContractValidationError, TypeError) as exc:
            errors.append(f"scenario_contract_invalid:{scenario_id}:{type(exc).__name__}")
            continue
        if scenario_id != contract.scenario_id:
            errors.append(f"scenario_contract_mapping_key_mismatch:{scenario_id}")
            continue
        contracts[scenario_id] = contract

    scenario_result_gate = payload.get("scenario_result_gate")
    scenario_gate_ok = False
    if not isinstance(scenario_result_gate, Mapping):
        errors.append("scenario_result_gate_shape_invalid")
        scenario_result_gate = {}
    required = scenario_result_gate.get("required")
    if type(required) is not bool:
        errors.append("scenario_result_gate_required_invalid")
        required = True
    restored_results: dict[str, ScenarioResult] = {}
    if required:
        rollup = scenario_result_gate.get("rollup")
        if not isinstance(rollup, Mapping) or rollup.get("schema_version") != SCENARIO_ROLLUP_SCHEMA_VERSION:
            errors.append("scenario_rollup_shape_invalid")
            rollup = {}
        result_payloads = rollup.get("results")
        if not isinstance(result_payloads, Mapping):
            errors.append("scenario_results_shape_invalid")
            result_payloads = {}
        for scenario_id, result_payload in result_payloads.items():
            contract = contracts.get(scenario_id)
            if contract is None:
                errors.append(f"scenario_result_without_contract:{scenario_id}")
                continue
            try:
                restored_results[scenario_id] = ScenarioResult.from_dict(
                    result_payload,
                    contract=contract,
                )
            except (ContractValidationError, TypeError) as exc:
                errors.append(f"scenario_result_invalid:{scenario_id}:{type(exc).__name__}")
        expected_gate = _build_scenario_result_gate(
            required=True,
            disabled_reason="",
            contracts=contracts,
            results=restored_results,
            known_scenario_ids=scenarios,
            records=records,
        )
        if scenario_result_gate != expected_gate:
            errors.append("scenario_result_gate_recomputation_mismatch")
        scenario_gate_ok = expected_gate["ok"] is True and scenario_result_gate.get("ok") is True
    else:
        expected_gate = _build_scenario_result_gate(
            required=False,
            disabled_reason=scenario_result_gate.get("disabled_reason", ""),
            contracts={},
            results={},
            known_scenario_ids=scenarios,
            records=records,
        )
        if scenario_result_gate != expected_gate:
            errors.append("scenario_result_gate_recomputation_mismatch")
        if scenario_result_gate.get("ok") is not False:
            errors.append("disabled_scenario_result_gate_cannot_pass")

    if payload.get("hash_manifest_sha256") != _hash_manifest(
        records,
        serialized_contracts,
        scenario_result_gate,
    ):
        errors.append("hash_manifest_mismatch")
    build_errors = payload.get("errors")
    if not isinstance(build_errors, list):
        errors.append("build_errors_shape_invalid")
        build_errors = ["invalid"]
    if not isinstance(payload.get("campaign_uuid"), str) or not payload.get("campaign_uuid", "").strip():
        errors.append("campaign_uuid_invalid")
    if not isinstance(payload.get("commit"), str) or not re.fullmatch(r"[0-9a-f]{7,64}", payload.get("commit", "")):
        errors.append("commit_invalid")
    if not isinstance(payload.get("source_digest"), str) or not _SHA256.fullmatch(payload.get("source_digest", "")):
        errors.append("source_digest_invalid")
    gate_errors = scenario_result_gate.get("errors") if isinstance(scenario_result_gate, Mapping) else []
    gate_errors = gate_errors if isinstance(gate_errors, list) else []
    non_scenario_build_errors = [error for error in build_errors if error not in gate_errors]
    artifact_gate_ok = bool(
        payload.get("artifact_gate_ok") is True
        and records
        and artifact_component_error_count == 0
        and not non_scenario_build_errors
    )
    expected_overall_ok = artifact_gate_ok and scenario_gate_ok and not build_errors
    if payload.get("ok") is not expected_overall_ok:
        errors.append("overall_ok_mismatch")
    schema_ok = not errors
    return {
        "ok": schema_ok and expected_overall_ok,
        "schema_ok": schema_ok,
        "artifact_gate_ok": artifact_gate_ok,
        "scenario_result_gate_ok": scenario_gate_ok,
        "errors": errors,
    }


def write_artifact_index(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically persist, re-open, parse, and validate the artifact index."""

    destination = Path(path).expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    try:
        restored = json.loads(destination.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "schema_ok": False,
            "artifact_gate_ok": False,
            "errors": [f"persisted_index_parse_failed:{type(exc).__name__}"],
            "path": str(destination),
        }
    result = validate_artifact_index(restored)
    result["path"] = str(destination)
    result["size"] = destination.stat().st_size
    result["sha256"] = sha256_file(destination)
    return result
