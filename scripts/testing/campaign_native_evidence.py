"""Canonical evidence summary and manifest writer for native campaign scenarios.

Scenario runners remain responsible for computing domain assertions from real
probe outputs.  This module binds those reviewed booleans to the exact source
artifact hashes and emits the only manifest shape accepted by the strict
runtime pipeline.  It never accepts or writes a formal runtime receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from scripts.testing.campaign_scenario_binding import (
    FORMAL_SCENARIO_BINDINGS,
    NATIVE_EVIDENCE_MANIFEST_SCHEMA_VERSION,
    NATIVE_EVIDENCE_SUMMARY_SCHEMA_VERSION,
    NATIVE_RUNNER_RESULT_SCHEMA_VERSION,
)


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,159}$")


class NativeEvidenceBuildError(ValueError):
    """Raised when a scenario tries to emit incomplete or ambiguous evidence."""


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise NativeEvidenceBuildError(f"native evidence output already exists: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        data = memoryview(_canonical_json_bytes(payload))
        while data:
            written = os.write(descriptor, data)
            data = data[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _boolean_map(
    value: Mapping[str, object],
    *,
    expected_ids: set[str] | None,
    label: str,
) -> dict[str, bool]:
    if not isinstance(value, Mapping) or not value:
        raise NativeEvidenceBuildError(f"{label} must be a non-empty mapping")
    if expected_ids is not None and set(value) != expected_ids:
        missing = sorted(expected_ids - set(value))
        extra = sorted(set(value) - expected_ids)
        raise NativeEvidenceBuildError(
            f"{label} IDs mismatch; missing={missing}, extra={extra}"
        )
    normalized: dict[str, bool] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _IDENTIFIER.fullmatch(key):
            raise NativeEvidenceBuildError(f"{label} contains invalid ID: {key!r}")
        if type(item) is not bool:
            raise NativeEvidenceBuildError(f"{label}.{key} must be boolean")
        normalized[key] = item
    return normalized


def attach_native_evidence(
    runner_result: Mapping[str, Any],
    *,
    scenario_id: str,
    output_dir: Path,
    scenario_assertions: Mapping[str, object],
    terminal_assertions: Mapping[str, object],
    cleanup_assertions: Mapping[str, object],
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Hash source artifacts and attach a canonical summary plus pointer manifest."""

    binding = FORMAL_SCENARIO_BINDINGS.get(scenario_id)
    if binding is None:
        raise NativeEvidenceBuildError(f"unknown formal scenario: {scenario_id}")
    if not isinstance(runner_result, Mapping):
        raise NativeEvidenceBuildError("runner_result must be a mapping")
    if runner_result.get("schema_version") != NATIVE_RUNNER_RESULT_SCHEMA_VERSION:
        raise NativeEvidenceBuildError("runner_result schema mismatch")
    if runner_result.get("scenario_id") != scenario_id:
        raise NativeEvidenceBuildError("runner_result scenario mismatch")
    artifacts = runner_result.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise NativeEvidenceBuildError("runner_result has no source artifacts")
    source_hashes: dict[str, str] = {}
    copied_artifacts: list[dict[str, str]] = []
    for declaration in artifacts:
        if not isinstance(declaration, Mapping):
            raise NativeEvidenceBuildError("source artifact declaration is invalid")
        artifact_id = str(declaration.get("artifact_id") or "")
        path = Path(str(declaration.get("path") or "")).expanduser()
        artifact_type = str(declaration.get("artifact_type") or "")
        if not _IDENTIFIER.fullmatch(artifact_id):
            raise NativeEvidenceBuildError(f"invalid source artifact ID: {artifact_id!r}")
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise NativeEvidenceBuildError(f"source artifact unavailable: {artifact_id}")
        if artifact_id in source_hashes:
            raise NativeEvidenceBuildError(f"duplicate source artifact ID: {artifact_id}")
        source_hashes[artifact_id] = _sha256(path)
        copied_artifacts.append({
            "artifact_id": artifact_id,
            "path": str(path.resolve(strict=True)),
            "artifact_type": artifact_type,
        })

    evidence = _boolean_map(
        scenario_assertions,
        expected_ids=set(binding.evidence_adapter_ids),
        label="scenario_assertions",
    )
    terminal = _boolean_map(
        terminal_assertions,
        expected_ids=None,
        label="terminal_assertions",
    )
    cleanup = _boolean_map(
        cleanup_assertions,
        expected_ids=None,
        label="cleanup_assertions",
    )
    output_dir = Path(output_dir).expanduser().resolve(strict=False)
    summary_path = output_dir / "native_evidence_summary.json"
    manifest_path = output_dir / "native_evidence_manifest.json"
    summary_id = f"native.summary.{scenario_id}"
    summary = {
        "schema_version": NATIVE_EVIDENCE_SUMMARY_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "source_artifact_sha256": dict(sorted(source_hashes.items())),
        "scenario_assertions": dict(sorted(evidence.items())),
        "terminal_assertions": dict(sorted(terminal.items())),
        "cleanup_assertions": dict(sorted(cleanup.items())),
        "details": dict(details or {}),
    }
    _write_private_json(summary_path, summary)
    copied_artifacts.append({
        "artifact_id": summary_id,
        "path": str(summary_path.resolve(strict=True)),
        "artifact_type": "json",
    })
    manifest = {
        "schema_version": NATIVE_EVIDENCE_MANIFEST_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "artifact_ids": [item["artifact_id"] for item in copied_artifacts],
        "evidence": {
            evidence_id: [{
                "source_artifact_id": summary_id,
                "json_pointer": f"/scenario_assertions/{_pointer_token(evidence_id)}",
                "predicate": "is_true",
                "expected": None,
            }]
            for evidence_id in binding.evidence_adapter_ids
        },
        "terminal": {
            "state": "success",
            "observations": [{
                "source_artifact_id": summary_id,
                "json_pointer": f"/terminal_assertions/{_pointer_token(assertion_id)}",
                "predicate": "is_true",
                "expected": None,
            } for assertion_id in sorted(terminal)],
        },
        "cleanup": {
            "state": "clean",
            "observations": [{
                "source_artifact_id": summary_id,
                "json_pointer": f"/cleanup_assertions/{_pointer_token(assertion_id)}",
                "predicate": "is_true",
                "expected": None,
            } for assertion_id in sorted(cleanup)],
        },
    }
    _write_private_json(manifest_path, manifest)
    result = dict(runner_result)
    result["artifacts"] = copied_artifacts
    result["formal_evidence_manifest"] = str(manifest_path.resolve(strict=True))
    return result


__all__ = [
    "NATIVE_EVIDENCE_SUMMARY_SCHEMA_VERSION",
    "NativeEvidenceBuildError",
    "attach_native_evidence",
]
