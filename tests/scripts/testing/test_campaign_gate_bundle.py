from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile

import pytest

from scripts.testing import campaign_gate_bundle as gate_module
from scripts.testing import (
    audit_evidence_triad,
    campaign_artifacts,
    campaign_cgroup,
    campaign_dependency_preflight,
    campaign_observability,
    campaign_scenario_binding,
    campaign_security_sentinel,
    campaign_smoke_load,
    campaign_source_freeze,
    campaign_state,
    campaign_watchdog,
    operational_campaign_supervisor,
)
from scripts.testing.campaign_gate_bundle import (
    GATE_BUNDLE_SCHEMA_VERSION,
    GATE_EVIDENCE_SCHEMA_VERSION,
    GATE_POLICIES,
    RAW_ARTIFACT_BINDING_SCHEMA_VERSION,
    RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION,
    REQUIRED_FORMAL_GATES,
    GateBundleError,
    build_gate_bundle,
    bundle_sha256,
    canonical_json_bytes,
    format_utc,
    sha256_bytes,
    validate_gate_bundle,
)
from services.server.database import get_audit_db
from services.system import audit as audit_service


COMMIT = "a" * 40
TRACKED_FILE_SHA = "f" * 64


def _content_digest() -> str:
    digest = hashlib.sha256()
    digest.update(b"app.py\0")
    digest.update(b"100644\0")
    digest.update(TRACKED_FILE_SHA.encode("ascii"))
    digest.update(b"\n")
    return digest.hexdigest()


SOURCE_DIGEST = _content_digest()
PROTECTED_ROWS = [
    {
        "path": ".hackme_capacity_defaults.env", "kind": "file",
        "working_sha256": "1" * 64, "symlink_target": "",
        "filesystem_mode": 0o600, "size": 10, "mtime_ns": 100,
        "ctime_ns": 101, "inode": 1001, "device": 1,
    },
    {
        "path": ".hackme_capacity_report.json", "kind": "file",
        "working_sha256": "2" * 64, "symlink_target": "",
        "filesystem_mode": 0o600, "size": 20, "mtime_ns": 200,
        "ctime_ns": 201, "inode": 1002, "device": 1,
    },
]


def _protected_digests(rows: list[dict[str, object]]) -> tuple[str, str]:
    manifest = hashlib.sha256()
    content = hashlib.sha256()
    for row in sorted(rows, key=lambda item: str(item["path"])):
        manifest.update(canonical_json_bytes(row) + b"\n")
        content.update(str(row["path"]).encode() + b"\0")
        content.update(str(row["kind"]).encode() + b"\0")
        content.update(str(row["filesystem_mode"]).encode() + b"\0")
        content.update(str(row["working_sha256"]).encode() + b"\0")
        content.update(str(row["symlink_target"]).encode() + b"\n")
    return manifest.hexdigest(), content.hexdigest()


PROTECTED_MANIFEST_DIGEST, PROTECTED_CONTENT_DIGEST = _protected_digests(PROTECTED_ROWS)
PROTECTED_SOURCE_DIGEST = gate_module.protected_source_identity_digest(
    PROTECTED_MANIFEST_DIGEST,
    PROTECTED_CONTENT_DIGEST,
)
CAMPAIGN_UUID = "qualification-0001"
NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
REHEARSAL_CAMPAIGN_UUID = "rehearsal-campaign-0001"
REHEARSAL_ATTEMPT_UUID = "rehearsal-attempt-0001"
REHEARSAL_NATIVE_INVOCATION_ID = (
    "native:60_minute_rehearsal_passed:semantic-fixture"
)
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
TEST_CAPTURE_PRODUCER = {
    "kind": gate_module.CAPTURE_PRODUCER_KIND,
    "pid": 4242,
    "start_ticks": 100_4242,
    "boot_id": "pytest-boot-id",
    "cgroup_path": "/pytest/capture",
    "invocation_id": "capture:pytest:gate-bundle",
}


@pytest.fixture(autouse=True)
def _private_checkpoint_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "persistent-checkpoints"
    root.mkdir()
    monkeypatch.setattr(gate_module, "_PERSISTENT_CHECKPOINT_ROOT", root.resolve())


def _binding(gate: str, role: str) -> dict[str, object]:
    return {
        "schema_version": RAW_ARTIFACT_BINDING_SCHEMA_VERSION,
        "gate_name": gate,
        "artifact_role": role,
        "qualification_campaign_uuid": CAMPAIGN_UUID,
        "commit": COMMIT,
        "source_digest": SOURCE_DIGEST,
        "protected_source_digest": PROTECTED_SOURCE_DIGEST,
        "actual_execution": True,
        "simulated": False,
        "component_only": False,
        "captured_at": format_utc(NOW - timedelta(minutes=2)),
        "producer": {
            **TEST_CAPTURE_PRODUCER,
        },
    }


def _capture_authority(gate: str, evidence_path: Path) -> dict[str, object]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    checked_at = gate_module.parse_utc(evidence["checked_at"], label="test checked_at")
    duration = (
        181.0 if gate == "180_second_smoke_passed"
        else 3601.0 if gate == "60_minute_rehearsal_passed"
        else 1.0
    )
    finished = checked_at - timedelta(seconds=1)
    started = finished - timedelta(seconds=duration)
    finished_ns = 10_000_000_000_000
    started_ns = finished_ns - int(duration * 1_000_000_000)
    invocation_id = f"native:{gate}:semantic-fixture"
    native_producer = {
        **TEST_CAPTURE_PRODUCER,
        "kind": gate_module.NATIVE_PRODUCER_KIND,
        "invocation_id": invocation_id,
    }
    return {
        "producer": dict(TEST_CAPTURE_PRODUCER),
        "created_at": format_utc(NOW - timedelta(minutes=2)),
        "native_execution": {
            "schema_version": gate_module.NATIVE_EXECUTION_RECEIPT_SCHEMA_VERSION,
            "gate_name": gate,
            "qualification_campaign_uuid": CAMPAIGN_UUID,
            "commit": COMMIT,
            "source_digest": SOURCE_DIGEST,
            "protected_source_digest": PROTECTED_SOURCE_DIGEST,
            "invocation_id": invocation_id,
            "activation_nonce": f"activation:{gate}:semantic-fixture",
            "actual_execution": True,
            "simulated": False,
            "component_only": False,
            "started_at": format_utc(started),
            "finished_at": format_utc(finished),
            "started_monotonic_ns": started_ns,
            "finished_monotonic_ns": finished_ns,
            "producer": native_producer,
            "source_authority_sha256": "e" * 64,
            "artifacts": {role: {} for role in evidence["raw_artifacts"]},
        },
    }


def _validate_semantic_fixture(
    evidence_path: Path,
    gate: str,
    *,
    now: datetime = NOW,
    registry: gate_module.ValidationRegistry | None = None,
) -> dict[str, object]:
    return gate_module._validate_unsealed_gate_evidence(
        evidence_path,
        gate_name=gate,
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        protected_source_digest=PROTECTED_SOURCE_DIGEST,
        qualification_campaign_uuid=CAMPAIGN_UUID,
        now=now,
        registry=registry,
        capture_authority=_capture_authority(gate, evidence_path),
    )


def _reference(gate: str, role: str, path: Path, media_type: str, content_schema: str) -> dict[str, object]:
    return {
        "schema_version": RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION,
        "artifact_id": f"artifact:{gate}:{role}",
        "gate_name": gate,
        "artifact_role": role,
        "path": str(path.resolve()),
        "sha256": sha256_bytes(path.read_bytes()),
        "size_bytes": path.stat().st_size,
        "media_type": media_type,
        "content_schema_version": content_schema,
        "qualification_campaign_uuid": CAMPAIGN_UUID,
        "commit": COMMIT,
        "source_digest": SOURCE_DIGEST,
        "protected_source_digest": PROTECTED_SOURCE_DIGEST,
    }


def _write_json_raw(root: Path, gate: str, role: str, schema: str, payload: dict) -> dict[str, object]:
    path = root / gate / f"{role}.json"
    if gate == "checkpoint_recovery_verified" and role == "checkpoint_mirror":
        path = gate_module._PERSISTENT_CHECKPOINT_ROOT / CAMPAIGN_UUID / "campaign.checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"schema_version": schema, **payload, "formal_binding": _binding(gate, role)}
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return _reference(gate, role, path, "application/json", schema)


def _write_jsonl_raw(root: Path, gate: str, role: str, schema: str, rows: list[dict]) -> dict[str, object]:
    path = root / gate / f"{role}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = [
        {"schema_version": schema, **row, "formal_binding": _binding(gate, role)}
        for row in rows
    ]
    path.write_text("".join(json.dumps(value, sort_keys=True) + "\n" for value in values), encoding="utf-8")
    path.chmod(0o600)
    return _reference(gate, role, path, "application/x-ndjson", schema)


def _write_bytes_raw(
    root: Path, gate: str, role: str, media_type: str, schema: str, content: bytes,
) -> dict[str, object]:
    suffix = ".bin"
    if media_type.startswith("text/") or media_type == "application/vnd.apple.mpegurl":
        suffix = ".txt"
    elif media_type == "image/png":
        suffix = ".png"
    elif media_type == "application/x-tar":
        suffix = ".tar"
    path = root / gate / f"{role}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)
    return _reference(gate, role, path, media_type, schema)


def _source_payload(label: str, digest: str, *, verified: bool = True) -> dict[str, object]:
    return {
        "label": label,
        "verified": verified,
        "require_clean": label in {"H0", "RESTORED"},
        "commit": COMMIT,
        "tracked_content_digest": digest,
        "protected_ignored_manifest_digest": PROTECTED_MANIFEST_DIGEST,
        "protected_ignored_content_digest": PROTECTED_CONTENT_DIGEST,
        "protected_ignored_file_count": len(PROTECTED_ROWS),
        "protected_ignored_present_count": len(PROTECTED_ROWS),
        "unsafe_protected_ignored_paths": [],
        "protected_ignored_policy": {
            "policy": "explicit_reviewed_list",
            "broad_ignored_runtime_is_excluded": True,
            "paths": [
                {
                    "path": row["path"], "reviewed": True, "git_ignored": True,
                    "authority_class": "protected_ignored_launcher_input",
                }
                for row in PROTECTED_ROWS
            ],
        },
    }


def _online_audit_triad_receipt() -> dict[str, object]:
    entry_hash = "8" * 64
    chain_hash = "9" * 64
    head = {
        "audit_id": 3,
        "entry_hash": entry_hash,
        "chain_hash": chain_hash,
    }
    return {
        "schema_version": audit_evidence_triad.SCHEMA_VERSION,
        "target": "security_sentinel",
        "mode": "online",
        "captured_at": "2026-07-13T07:57:00.000+00:00",
        "completed_at": "2026-07-13T07:57:01.000+00:00",
        "ok": True,
        "verdict": "PASS",
        "capture": {
            "mutation_lock_wait_ms": 0.25,
            "head_anchor": {"attempted": False, "performed": False},
            "sqlite_backup_api": True,
            "immutable_validation": True,
        },
        "artifacts": {
            "database": {
                "state": "present", "path": "audit_snapshot.sqlite3",
                "size": 4096, "sha256": "1" * 64,
            },
            "audit_log": {
                "state": "present", "path": "audit.log",
                "size": 512, "sha256": "2" * 64,
            },
            "anchor_history": {
                "state": "present", "path": "audit_head.jsonl",
                "size": 256, "sha256": "3" * 64,
            },
            "anchor_latest": {
                "state": "present", "path": "audit_head_latest.json",
                "size": 256, "sha256": "4" * 64,
            },
        },
        "counts": {
            "db_rows": 3,
            "log_entries": 3,
            "anchor_history_entries": 1,
            "rows_after_latest": 2,
        },
        "heads": {
            "database": head,
            "audit_log": dict(head),
            "anchor_latest": {
                "ts": "2026-07-13T07:56:00.000+00:00",
                "audit_id": 1,
                "entry_hash": "6" * 64,
                "chain_hash": "7" * 64,
                "reason": "interval",
            },
        },
        "invariants": {
            name: True for name in audit_evidence_triad.INVARIANT_NAMES
        },
        "errors": [],
        "secret_handling": {
            "integrity_key": "memory_only",
            "chain_seed": "memory_only",
            "secret_files_copied": False,
            "secret_values_in_receipt": False,
        },
    }


def _security_report(root: Path) -> tuple[dict[str, object], Path]:
    runtime = root / "security-audit-runtime"
    for directory in (
        runtime / "database",
        runtime / "logs",
        runtime / "anchors",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    seed = "ab" * 24
    key = b"k" * 32
    (runtime / ".chain_seed").write_text(seed, encoding="utf-8")
    (runtime / ".integrity_key").write_bytes(key)
    database = runtime / "database" / "audit.db"
    audit_service.configure_audit_service(
        get_db=lambda: get_audit_db(str(database)),
        chain_seed=seed,
        integrity_key=key,
        audit_log_path=str(runtime / "logs" / "audit.log"),
        audit_anchor_path=str(runtime / "anchors" / "audit_head.jsonl"),
        audit_anchor_latest_path=str(runtime / "anchors" / "audit_head_latest.json"),
        audit_anchor_interval_seconds=60,
    )
    audit_service._last_audit_anchor_at = 0.0
    audit_service.audit(
        "production_security_gate_fixture",
        "127.0.0.1",
        user="root",
        success=True,
        ua="pytest",
        detail="online-triad",
    )
    triad_root = root / "security-audit-triad"
    receipt = audit_evidence_triad.capture_audit_evidence(
        paths=audit_evidence_triad.AuditEvidencePaths.for_runtime(runtime),
        output_dir=triad_root,
        target="security_sentinel",
        mode="online",
    )
    archive_path = root / "security-audit-triad.tar"
    archive = audit_evidence_triad.create_audit_evidence_archive(
        output_dir=triad_root,
        archive_path=archive_path,
    )
    archive_validation = audit_evidence_triad.validate_audit_evidence_archive(
        archive_path,
        required_mode="online",
        required_target="security_sentinel",
        expected_sha256=str(archive["sha256"]),
        expected_size=int(archive["size"]),
    )
    assert receipt["ok"] is True and archive_validation["ok"] is True
    encoded = (triad_root / "receipt.json").read_bytes()
    names = {
        "production_launcher_contract", "transport", "anonymous_root_denied",
        "login_missing_csrf_denied", "root_login", "manager_login", "user_login",
        "production_mode_active", "manager_root_boundary_denied",
        "user_root_boundary_denied", "authenticated_missing_csrf_denied",
        "dangerous_confirmation_required", "production_security_controls",
        "audit_log_chain", "cross_worker_session_consistency",
        "audit_evidence_triad_online",
    }
    checks: list[dict[str, object]] = []
    denied_statuses = {
        "anonymous_root_denied": 403,
        "login_missing_csrf_denied": 403,
        "manager_root_boundary_denied": 403,
        "user_root_boundary_denied": 403,
        "authenticated_missing_csrf_denied": 403,
        "dangerous_confirmation_required": 400,
    }
    for name in sorted(names):
        detail: dict[str, object] = {}
        if name == "production_launcher_contract":
            detail = {
                "security": "on", "server_mode": "production",
                "gunicorn_workers": 2, "isolated_runtime": True,
            }
        elif name == "production_security_controls":
            detail = {
                "required_settings": {
                    "audit_chain_enabled": True,
                    "feature_audit_log_enabled": True,
                    "login_violation_enabled": True,
                    "rate_limit_violation_enabled": True,
                }
            }
        elif name == "cross_worker_session_consistency":
            detail = {"requests": 4, "statuses": [200, 200, 200, 200]}
        elif name == "audit_evidence_triad_online":
            detail = {
                "receipt_schema_version": audit_evidence_triad.SCHEMA_VERSION,
                "mode": "online",
                "target": "security_sentinel",
                "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
                "receipt_size_bytes": len(encoded),
                "artifact_files_verified": True,
                "validation_classification": "PASS",
                "validation_errors": [],
                "archive_schema_version": audit_evidence_triad.ARCHIVE_SCHEMA_VERSION,
                "archive_sha256": archive["sha256"],
                "archive_size_bytes": archive["size"],
                "archive_validation_classification": "PASS",
                "archive_validation_errors": [],
            }
        checks.append({"name": name, "ok": True, "status": denied_statuses.get(name, 200), "detail": detail})
    return {
        "ok": True,
        "classification": "PASS",
        "failed_checks": [],
        "checks": checks,
        "audit_evidence": {
            "schema_version": "hackme.audit-evidence-triad-reference/v1",
            "receipt_schema_version": audit_evidence_triad.SCHEMA_VERSION,
            "mode": "online",
            "target": "security_sentinel",
            "receipt_path": "/tmp/security-sentinel/audit-evidence/receipt.json",
            "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
            "receipt_size_bytes": len(encoded),
            "receipt": receipt,
            "validation": {
                "schema_version": "hackme.audit-evidence-triad-validation/v1",
                "ok": True,
                "classification": "PASS",
                "errors": [],
                "validated_invariants": sorted(audit_evidence_triad.INVARIANT_NAMES),
                "artifact_files_verified": True,
            },
            "archive_schema_version": audit_evidence_triad.ARCHIVE_SCHEMA_VERSION,
            "archive_path": str(archive_path.resolve()),
            "archive_sha256": archive["sha256"],
            "archive_size_bytes": archive["size"],
            "archive_validation": archive_validation,
        },
    }, archive_path


def _external_receipt(dependency: str, evidence: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "hackme.campaign.external-dependency-probe/v1",
        "dependency": dependency,
        "available": True,
        "synthetic": False,
        "terminal_state": "completed",
        "evidence": evidence,
    }


def _supervisor(level: str) -> dict[str, object]:
    return {
        "campaign_uuid": f"native-{level}-campaign",
        "level": level,
        "commit": COMMIT,
        "source_digest": SOURCE_DIGEST,
        "runner_returncode": 0,
        "runner_verdict": "PASS",
        "source_final": {"verified": True},
        "gates": {
            "cgroup_limits_verified": {"status": "PASS"},
            "external_watchdog_verified": {"status": "PASS"},
            "runner_and_watchdog_placement_verified": {"status": "PASS"},
        },
        "cleanup": {
            "source_monitor": {"ok": True},
            "watchdog": {"ok": True},
            "scope": {"ok": True},
        },
        "classification": "PASS",
        "ok": True,
    }


def _artifact_link(reference: dict[str, object]) -> dict[str, object]:
    return {
        "path": reference["path"],
        "sha256": reference["sha256"],
        "size_bytes": reference["size_bytes"],
    }


def _rehearsal_authority(
    *,
    started_offset_seconds: float,
    finished_offset_seconds: float,
    scenario_attempt_uuid: str | None = None,
) -> dict[str, object]:
    native_started = NOW - timedelta(minutes=1, seconds=3602)
    native_started_ns = 10_000_000_000_000 - 3_601_000_000_000
    started = native_started + timedelta(seconds=started_offset_seconds)
    finished = native_started + timedelta(seconds=finished_offset_seconds)
    authority: dict[str, object] = {
        "qualification_campaign_uuid": CAMPAIGN_UUID,
        "campaign_uuid": REHEARSAL_CAMPAIGN_UUID,
        "campaign_attempt_uuid": REHEARSAL_ATTEMPT_UUID,
        "native_invocation_id": REHEARSAL_NATIVE_INVOCATION_ID,
        "commit": COMMIT,
        "source_digest": SOURCE_DIGEST,
        "protected_source_digest": PROTECTED_SOURCE_DIGEST,
        "started_at": format_utc(started),
        "finished_at": format_utc(finished),
        "started_monotonic_ns": native_started_ns
        + int(started_offset_seconds * 1_000_000_000),
        "finished_monotonic_ns": native_started_ns
        + int(finished_offset_seconds * 1_000_000_000),
    }
    if scenario_attempt_uuid is not None:
        authority["scenario_attempt_uuid"] = scenario_attempt_uuid
        authority["native_invocation_id"] = (
            f"scenario-invocation:{scenario_attempt_uuid}"
        )
    return authority


def _scenario_receipt(
    scenario_id: str,
    *,
    authority: dict[str, object],
    bundle_reference: dict[str, object],
    manifest_sha256: str,
    member_inventory: list[dict[str, object]],
    archive_reference: dict[str, object],
    evidence_adapter_results: dict[str, dict[str, object]],
    validator_results: dict[str, dict[str, object]],
) -> dict[str, object]:
    binding = campaign_scenario_binding.FORMAL_SCENARIO_BINDINGS[scenario_id]
    return {
        "schema_version": campaign_scenario_binding.RUNTIME_RECEIPT_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "runner_id": binding.runner_id,
        "status": "PASS",
        "terminal_state": "success",
        "authority": authority,
        "evidence_receipts": {
            evidence_id: {
                "evidence_id": evidence_id,
                "adapter_id": adapter_id,
                "validated": evidence_adapter_results[evidence_id]["validated"],
                "native_observation_ids": evidence_adapter_results[evidence_id][
                    "native_observation_ids"
                ],
            }
            for evidence_id, adapter_id in binding.evidence_adapter_ids.items()
        },
        "terminal_validator_results": {
            name: validator_results[name]["passed"]
            for name in binding.terminal_validator_ids
        },
        "cleanup_validator_results": {
            name: validator_results[name]["passed"]
            for name in binding.cleanup_validator_ids
        },
        "artifact_validator_results": {
            name: validator_results[name]["passed"]
            for name in binding.artifact_validator_ids
        },
        "artifact_ids": [f"native.artifact.bundle.{scenario_id}"],
        "artifact_bundle": {
            "artifact_id": f"native.artifact.bundle.{scenario_id}",
            "content_schema_version": (
                campaign_scenario_binding.NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION
            ),
            **_artifact_link(bundle_reference),
            "manifest_sha256": manifest_sha256,
            "member_inventory_sha256": (
                campaign_scenario_binding.scenario_member_inventory_sha256(
                    member_inventory
                )
            ),
            "member_count": len(member_inventory),
            "artifact_archive_id": f"native.artifact.archive.{scenario_id}",
            "artifact_archive_sha256": archive_reference["sha256"],
            "artifact_archive_size_bytes": archive_reference["size_bytes"],
        },
        "diagnostics": [],
    }


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output,
        mode="w",
        format=tarfile.USTAR_FORMAT,
    ) as archive:
        for member_path, content in sorted(members.items()):
            info = tarfile.TarInfo(member_path)
            info.size = len(content)
            info.mode = 0o600
            info.mtime = 1
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _validated_artifact_record(
    *,
    artifact_id: str,
    scenario_id: str,
    path: Path,
    content: bytes,
    artifact_type: str = "json",
) -> dict[str, object]:
    return {
        "schema_version": campaign_artifacts.ARTIFACT_RECORD_SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "scenario_id": scenario_id,
        "path": str(path),
        "created_at": "2026-07-13T07:00:00Z",
        "type": artifact_type,
        "mandatory": True,
        "scenario_link_valid": True,
        "within_artifact_root": True,
        "exists": True,
        "regular_file": True,
        "size": len(content),
        "minimum_size_bytes": 1,
        "nonzero": True,
        "validation_snapshot_stable": True,
        "sha256": sha256_bytes(content),
        "expected_sha256": "",
        "sha256_verified": True,
        "format_validation": {
            "ok": True,
            "method": "fixture-reparse",
            "details": {"size": len(content)},
            "errors": [],
        },
        "secret_scan": {
            "schema_version": campaign_artifacts.SECRET_SCAN_SCHEMA_VERSION,
            "performed": True,
            "coverage_complete": True,
            "ok": True,
            "scanned_bytes": len(content),
            "source_count": 1,
            "pattern_count": 1,
            "finding_count": 0,
            "findings": [],
            "collector_errors": [],
        },
        "validated": True,
        "errors": [],
    }


def _write_rehearsal_scenario(
    root: Path,
    gate: str,
    scenario_id: str,
    index: int,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    binding = campaign_scenario_binding.FORMAL_SCENARIO_BINDINGS[scenario_id]
    proof_id = f"native.artifact.{scenario_id}.proof"
    summary_id = f"native.summary.{scenario_id}"
    manifest_id = f"native.manifest.{scenario_id}"
    proof_path = "artifacts/proof.json"
    summary_path = "artifacts/native_evidence_summary.json"
    manifest_path = "manifest/evidence.json"
    proof_payload = {"scenario_id": scenario_id, "terminal_state": "success"}
    proof_content = json.dumps(proof_payload, sort_keys=True).encode("utf-8")
    summary_payload = {
        "schema_version": (
            campaign_scenario_binding.NATIVE_EVIDENCE_SUMMARY_SCHEMA_VERSION
        ),
        "scenario_id": scenario_id,
        "source_artifact_sha256": {proof_id: sha256_bytes(proof_content)},
        "scenario_assertions": {
            evidence_id: True for evidence_id in binding.evidence_adapter_ids
        },
        "terminal_assertions": {"domain_terminal_success": True},
        "cleanup_assertions": {"fixture_cleanup_complete": True},
        "details": {"source_kind": "runtime_probe"},
    }
    summary_content = json.dumps(summary_payload, sort_keys=True).encode("utf-8")

    def pointer_token(value: str) -> str:
        return value.replace("~", "~0").replace("/", "~1")

    def summary_observation(pointer: str) -> dict[str, object]:
        return {
            "source_artifact_id": summary_id,
            "json_pointer": pointer,
            "predicate": "is_true",
            "expected": None,
        }

    manifest_payload = {
        "schema_version": (
            campaign_scenario_binding.NATIVE_EVIDENCE_MANIFEST_SCHEMA_VERSION
        ),
        "scenario_id": scenario_id,
        "artifact_ids": [proof_id, summary_id],
        "evidence": {
            evidence_id: [summary_observation(
                f"/scenario_assertions/{pointer_token(evidence_id)}"
            )]
            for evidence_id in binding.evidence_adapter_ids
        },
        "terminal": {
            "state": "success",
            "observations": [summary_observation(
                "/terminal_assertions/domain_terminal_success"
            )],
        },
        "cleanup": {
            "state": "clean",
            "observations": [summary_observation(
                "/cleanup_assertions/fixture_cleanup_complete"
            )],
        },
    }
    manifest_content = json.dumps(manifest_payload, sort_keys=True).encode("utf-8")
    members = {
        proof_path: proof_content,
        summary_path: summary_content,
        manifest_path: manifest_content,
    }
    member_inventory = [
        {
            "artifact_id": proof_id,
            "member_path": proof_path,
            "sha256": sha256_bytes(proof_content),
            "size_bytes": len(proof_content),
            "artifact_type": "json",
        },
        {
            "artifact_id": summary_id,
            "member_path": summary_path,
            "sha256": sha256_bytes(summary_content),
            "size_bytes": len(summary_content),
            "artifact_type": "json",
        },
        {
            "artifact_id": manifest_id,
            "member_path": manifest_path,
            "sha256": sha256_bytes(manifest_content),
            "size_bytes": len(manifest_content),
            "artifact_type": "json",
        },
    ]
    archive_role = f"scenario_archive_{scenario_id}"
    archive_reference = _write_bytes_raw(
        root,
        gate,
        archive_role,
        "application/x-tar",
        campaign_scenario_binding.NATIVE_ARTIFACT_ARCHIVE_SCHEMA_VERSION,
        _tar_bytes(members),
    )
    authority = _rehearsal_authority(
        started_offset_seconds=1.0 + (index * 2),
        finished_offset_seconds=2.0 + (index * 2),
        scenario_attempt_uuid=f"scenario-attempt-{index:04d}",
    )
    native_member_root = (root / gate / "native-members" / scenario_id).resolve()
    artifact_records = {
        proof_id: _validated_artifact_record(
            artifact_id=proof_id,
            scenario_id=scenario_id,
            path=native_member_root / "proof.json",
            content=proof_content,
        ),
        summary_id: _validated_artifact_record(
            artifact_id=summary_id,
            scenario_id=scenario_id,
            path=native_member_root / "native_evidence_summary.json",
            content=summary_content,
        ),
    }
    manifest_record = _validated_artifact_record(
        artifact_id=manifest_id,
        scenario_id=scenario_id,
        path=native_member_root / "evidence.json",
        content=manifest_content,
    )
    artifact_payloads = {
        proof_id: proof_payload,
        summary_id: summary_payload,
    }
    artifact_sha256 = {
        proof_id: sha256_bytes(proof_content),
        summary_id: sha256_bytes(summary_content),
    }
    adapters = campaign_scenario_binding.build_strict_native_adapter_registry(
        bindings={scenario_id: binding}
    )
    validators = campaign_scenario_binding.build_strict_native_validator_registry(
        bindings={scenario_id: binding}
    )
    evidence_adapter_results = {}
    for evidence_id, adapter_id in binding.evidence_adapter_ids.items():
        registration = adapters[adapter_id]
        evidence_adapter_results[evidence_id] = dict(registration.handler(
            registration=registration,
            manifest=manifest_payload,
            artifact_payloads=artifact_payloads,
            artifact_sha256=artifact_sha256,
        ))
    validator_results = {}
    for validator_id in (
        binding.terminal_validator_ids
        + binding.cleanup_validator_ids
        + binding.artifact_validator_ids
    ):
        registration = validators[validator_id]
        validator_results[validator_id] = dict(registration.handler(
            registration=registration,
            manifest=manifest_payload,
            artifact_payloads=artifact_payloads,
            artifact_sha256=artifact_sha256,
            artifact_records=artifact_records,
            manifest_record=manifest_record,
        ))
    bundle_role = f"scenario_bundle_{scenario_id}"
    bundle_reference = _write_json_raw(
        root,
        gate,
        bundle_role,
        campaign_scenario_binding.NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION,
        {
            "pipeline_schema_version": (
                campaign_scenario_binding.NATIVE_RUNTIME_PIPELINE_SCHEMA_VERSION
            ),
            "scenario_id": scenario_id,
            "runner_id": binding.runner_id,
            "candidate_status": "PASS",
            "authority": authority,
            "artifact_records": artifact_records,
            "manifest_record": manifest_record,
            "artifact_archive": {
                "artifact_id": f"native.artifact.archive.{scenario_id}",
                "content_schema_version": (
                    campaign_scenario_binding.NATIVE_ARTIFACT_ARCHIVE_SCHEMA_VERSION
                ),
                **_artifact_link(archive_reference),
                "media_type": "application/x-tar",
            },
            "member_inventory": member_inventory,
            "member_inventory_sha256": (
                campaign_scenario_binding.scenario_member_inventory_sha256(
                    member_inventory
                )
            ),
            "evidence_adapter_results": evidence_adapter_results,
            "validator_results": validator_results,
            "diagnostics": [],
        },
    )
    receipt_role = f"scenario_{scenario_id}"
    receipt_reference = _write_json_raw(
        root,
        gate,
        receipt_role,
        campaign_scenario_binding.RUNTIME_RECEIPT_SCHEMA_VERSION,
        _scenario_receipt(
            scenario_id,
            authority=authority,
            bundle_reference=bundle_reference,
            manifest_sha256=str(manifest_record["sha256"]),
            member_inventory=member_inventory,
            archive_reference=archive_reference,
            evidence_adapter_results=evidence_adapter_results,
            validator_results=validator_results,
        ),
    )
    raw = {
        receipt_role: receipt_reference,
        bundle_role: bundle_reference,
        archive_role: archive_reference,
    }
    runner_index = {
        "scenario_attempt_uuid": authority["scenario_attempt_uuid"],
        "native_invocation_id": authority["native_invocation_id"],
        "receipt": _artifact_link(receipt_reference),
        "artifact_bundle": _artifact_link(bundle_reference),
        "artifact_archive": _artifact_link(archive_reference),
    }
    return raw, runner_index


def _set_nested(target: dict[str, object], dotted: str, value: object) -> None:
    cursor = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = cursor.setdefault(part, {})
        assert isinstance(child, dict)
        cursor = child
    cursor[parts[-1]] = value


def _formal_resource_row(index: int) -> dict[str, object]:
    expected = set(campaign_observability.ResourceCollector.BASE_EXPECTED_FIELDS)
    expected.update(gate_module._FORMAL_RESOURCE_ALWAYS_REQUIRED)
    for role in gate_module._MANDATORY_ROLES:
        expected.update(
            f"process_roles.{role}.{metric}"
            for metric in gate_module._RESOURCE_PROCESS_METRICS
        )
    row: dict[str, object] = {
        "at": f"2026-07-13T07:5{index}:00Z",
        "collector_errors": {},
        "hard_limit_state": {"ok": True, "tripped": []},
    }
    for field_name in sorted(expected):
        value: object = 1
        if field_name.endswith(".semantic_ready") or field_name == "hard_limit_state.ok":
            value = True
        elif field_name.endswith(".status_code") or field_name == "comfyui_queue.status":
            value = 200
        _set_nested(row, field_name, value)
    row["expected_fields"] = sorted(expected)
    row["valid_fields"] = sorted(expected)
    row["missing_fields"] = []
    row["field_completeness_ratio"] = 1.0
    return row


def _gate_raw(root: Path, gate: str) -> dict[str, dict[str, object]]:
    if gate == "cgroup_limits_verified":
        limits = {
            "memory.high": 5 * 1024**3,
            "memory.max": 6 * 1024**3,
            "memory.swap.max": 512 * 1024**2,
            "cpu.quota_percent": 300,
            "pids.max": 384,
            "io.weight": campaign_cgroup.DEFAULT_IO_WEIGHT,
        }
        readback = _write_json_raw(root, gate, "cgroup_readback", campaign_cgroup.CGROUP_SCHEMA_VERSION, {
            "campaign_id": "native-cgroup-campaign", "created": True,
            "cgroup_path": "/user.slice/formal.scope", "expected_limits": limits,
            "actual_limits": limits, "controllers_verified": ["cpu", "io", "memory", "pids"],
        })
        placements = [
            {"role": role, "pid": 5000 + index, "inside_scope": True, "cgroup_path": "/user.slice/formal.scope"}
            for index, role in enumerate((
                "primary", "recovery", "security_sentinel", "load_generator", "browser",
                "ffmpeg", "bt", "comfyui", "scenario",
            ))
        ]
        placement = _write_json_raw(root, gate, "pid_placement", "hackme.campaign-cgroup-placement-set/v1", {
            "campaign_uuid": "native-cgroup-campaign", "placements": placements,
            "watchdog": {"pid": 4999, "inside_scope": False, "cgroup_path": "/user.slice/watchdog.scope"},
        })
        return {"cgroup_readback": readback, "pid_placement": placement}

    if gate == "external_watchdog_verified":
        common = {"campaign_uuid": "native-watchdog-campaign"}
        return {
            "watchdog_startup": _write_json_raw(root, gate, "watchdog_startup", "hackme.campaign-watchdog.v1", {
                **common, "verified": True, "external_process": True,
                "watchdog_outside_campaign_cgroup": True, "stale_after_seconds": 120,
                "watchdog_pid": 6102, "orchestrator_pid": 6101,
            }),
            "watchdog_incident": _write_json_raw(root, gate, "watchdog_incident", "hackme.campaign-watchdog.v1", {
                **common, "incident_id": "incident-watchdog-0001", "reason": "HEARTBEAT_STALE",
                "details": {"heartbeat_age_seconds": 120.5},
                "watchdog": {"pid": 6102},
                "orchestrator_process": {"pid": 6101, "identity_verified": True},
                "watchdog_survived_orchestrator_stop": True,
            }),
            "watchdog_terminal": _write_json_raw(root, gate, "watchdog_terminal", "hackme.campaign-watchdog.v1", {
                **common, "ok": True, "incident_id": "incident-watchdog-0001",
                "admit_new_jobs": False, "collector_errors": [],
                "cgroup_stop": {"freeze_written": True, "kill_written": True, "population_cleared": True},
            }),
        }

    if gate == "hard_stop_injection_verified":
        campaign = "native-hard-stop-campaign"
        before = {
            "campaign_uuid": campaign, "state": "ACTIVE",
            "clock": {"continuous_active_seconds": 42.0, "formal_segment_valid": True},
            "control": {"admit_new_jobs": True, "load_generator_should_run": True},
        }
        after = {
            "campaign_uuid": campaign, "state": "STOPPING_LOAD",
            "clock": {"continuous_active_seconds": 42.0, "formal_segment_valid": False, "active_finished_at": "2026-07-13T07:58:00Z"},
            "control": {"admit_new_jobs": False, "load_generator_should_run": False, "preserve_evidence_requested": True},
            "hard_stop": {"injected": True, "fault_kind": "SQLITE_LOCK"},
        }
        control = {
            "campaign_uuid": campaign, "state": "STOPPING_LOAD", "admit_new_jobs": False,
            "load_generator_should_run": False, "preserve_evidence_requested": True,
        }
        stop = {"campaign_uuid": campaign, "freeze_written": True, "kill_written": True, "population_cleared": True}
        return {
            "state_before": _write_json_raw(root, gate, "state_before", "hackme.campaign-state.v1", before),
            "state_after": _write_json_raw(root, gate, "state_after", "hackme.campaign-state.v1", after),
            "control_after": _write_json_raw(root, gate, "control_after", "hackme.campaign-control.v1", control),
            "cgroup_stop": _write_json_raw(root, gate, "cgroup_stop", "hackme.campaign-cgroup-stop/v1", stop),
        }

    if gate == "checkpoint_recovery_verified":
        campaign = "native-checkpoint-campaign"
        checkpoint = {"campaign_uuid": campaign, "revision": 18, "status": "active", "payload": {"phase": "qualification"}}
        return {
            "checkpoint_before": _write_json_raw(root, gate, "checkpoint_before", "hackme.campaign-checkpoint.v1", {**checkpoint, "revision": 17}),
            "checkpoint_primary": _write_json_raw(root, gate, "checkpoint_primary", "hackme.campaign-checkpoint.v1", checkpoint),
            "checkpoint_mirror": _write_json_raw(root, gate, "checkpoint_mirror", "hackme.campaign-checkpoint.v1", checkpoint),
            "tamper_rejection": _write_json_raw(root, gate, "tamper_rejection", "hackme.campaign-checkpoint-tamper-trial/v1", {
                "campaign_uuid": campaign, "candidate_accepted": False, "classification": "FAIL_HARNESS",
                "rejection_reason": "checkpoint_sha256_mismatch", "formal_time_resumed": False,
                "revalidated": {"pid_identity": True, "cgroup_identity": True, "source_identity": True},
            }),
        }

    if gate == "source_drift_detection_verified":
        return {
            "source_h0": _write_json_raw(root, gate, "source_h0", "hackme.source-freeze.v3", _source_payload("H0", SOURCE_DIGEST)),
            "drift_incident": _write_json_raw(root, gate, "drift_incident", "hackme.source-drift.v4", {
                "incident": True, "verified": False, "incident_evidence_preserved": True,
                "tracked_changes": {"public/app.js": {"reason": "sha256_changed"}}, "untracked_changes": {},
                "monitor": {"machine_verified": True, "formal_eligible": True},
            }),
            "source_h24": _write_json_raw(root, gate, "source_h24", "hackme.source-freeze.v3", _source_payload("H24", "c" * 64)),
            "source_restored": _write_json_raw(root, gate, "source_restored", "hackme.source-freeze.v3", _source_payload("RESTORED", SOURCE_DIGEST)),
            "terminal_state": _write_json_raw(root, gate, "terminal_state", "hackme.campaign-state.v1", {
                "campaign_uuid": "native-source-drift-campaign", "state": "STOPPING_LOAD",
                "control": {"admit_new_jobs": False, "load_generator_should_run": False},
            }),
        }

    if gate == "sample_schema_completeness_verified":
        rows = [_formal_resource_row(index) for index in range(2)]
        return {
            "resource_samples": _write_jsonl_raw(root, gate, "resource_samples", "hackme.resource-sample.v1", rows),
            "negative_collector_trials": _write_json_raw(root, gate, "negative_collector_trials", "hackme.resource-negative-trials/v1", {
                "trials": [
                    {
                        "case": "empty_collector", "candidate_samples": [],
                        "accepted": False, "classification": "FAIL_HARNESS",
                    },
                    {
                        "case": "schema_only_sample",
                        "candidate_samples": [{
                            "sample_schema_version": "hackme.resource-sample.v1",
                            "expected_fields": ["host.memory.available_bytes"],
                            "valid_fields": ["host.memory.available_bytes"],
                        }],
                        "accepted": False, "classification": "FAIL_HARNESS",
                    },
                ]
            }),
        }

    if gate == "production_security_sentinel_verified":
        report, archive_path = _security_report(root / gate)
        return {
            "security_sentinel": _write_json_raw(
                root,
                gate,
                "security_sentinel",
                "hackme.production-security-sentinel.v1",
                report,
            ),
            "audit_evidence_archive": _write_bytes_raw(
                root,
                gate,
                "audit_evidence_archive",
                "application/x-tar",
                audit_evidence_triad.ARCHIVE_SCHEMA_VERSION,
                archive_path.read_bytes(),
            ),
        }

    if gate == "all_mandatory_dependencies_verified":
        browser_refs: dict[str, dict[str, object]] = {}
        browser_evidence: dict[str, dict[str, object]] = {}
        for offset, engine in enumerate(("chromium", "firefox", "webkit"), start=1):
            role = f"browser_{engine}_launch"
            marker = f"level1-{engine}"
            ref = _write_json_raw(
                root, gate, role, gate_module._DEPENDENCY_RAW_SCHEMAS["browser_launch"],
                {
                    "engine": engine, "browser_version": f"{engine}-1.0",
                    "executable_path": f"/opt/{engine}", "browser_pid": 7000 + offset,
                    "process_start_ticks": 9000 + offset,
                    "dom_marker_expected": marker, "dom_marker_observed": marker,
                    "page_url": "about:blank", "console_errors": [], "page_errors": [],
                    "closed_cleanly": True, "started_at": "2026-07-13T07:55:00Z",
                    "finished_at": "2026-07-13T07:55:01Z",
                },
            )
            browser_refs[role] = ref
            browser_evidence[engine] = {
                "engine": engine, "version": f"{engine}-1.0", "dom_marker": marker,
                "raw_authority_path": ref["path"], "raw_authority_sha256": ref["sha256"],
            }

        segment_bytes = bytes([0x47]) + b"\0" * 187 + bytes([0x47]) + b"\0" * 187
        segment = _write_bytes_raw(
            root, gate, "hls_segment", "video/mp2t", "native.hls-segment/v1",
            segment_bytes,
        )
        playlist = _write_bytes_raw(
            root, gate, "hls_playlist", "application/vnd.apple.mpegurl",
            "native.hls-playlist/v1",
            f"#EXTM3U\n#EXTINF:1.0,\n{Path(str(segment['path'])).name}\n#EXT-X-ENDLIST\n".encode(),
        )
        ffprobe = _write_json_raw(
            root, gate, "hls_ffprobe", gate_module._DEPENDENCY_RAW_SCHEMAS["hls_ffprobe"],
            {
                "input_path": playlist["path"], "returncode": 0, "segment_count": 1,
                "segment_sha256": segment["sha256"],
                "streams": [{"codec_type": "video", "codec_name": "mpeg2video"}],
                "format": {"duration": 1.0},
            },
        )

        bt_payload = _write_bytes_raw(
            root, gate, "bt_payload", "application/octet-stream",
            "native.bt-payload/v1", b"torrent-payload",
        )
        info_hash = "a" * 40
        bt_trace = _write_json_raw(
            root, gate, "bt_protocol_trace", gate_module._DEPENDENCY_RAW_SCHEMAS["bt_trace"],
            {
                "protocol": "bittorrent", "terminal_state": "completed",
                "info_hash": info_hash, "seed_pid": 8101, "client_pid": 8102,
                "peer_handshake_observed": True, "piece_hashes_verified": True,
                "payload_path": bt_payload["path"], "payload_sha256": bt_payload["sha256"],
                "payload_size_bytes": bt_payload["size_bytes"],
                "events": [
                    {"event": "torrent_created"}, {"event": "peer_handshake"},
                    {"event": "piece_verified"}, {"event": "download_completed"},
                ],
            },
        )

        comfy_output = _write_bytes_raw(
            root, gate, "comfyui_output", "image/png", "native.png/v1", PNG_BYTES,
        )
        comfy_history = _write_json_raw(
            root, gate, "comfyui_history", gate_module._DEPENDENCY_RAW_SCHEMAS["comfyui_history"],
            {
                "prompt_id": "prompt-0001", "job_id": "job-0001",
                "terminal_state": "success", "executed_node_count": 3,
                "output_path": comfy_output["path"], "output_sha256": comfy_output["sha256"],
                "errors": [],
            },
        )
        ai_exchange = _write_json_raw(
            root, gate, "ai_provider_exchange", gate_module._DEPENDENCY_RAW_SCHEMAS["ai_exchange"],
            {
                "configured_provider": "real-provider", "provider": "real-provider",
                "configured_model": "real-model", "model": "real-model",
                "request_id": "request-0001", "synthetic": False,
                "terminal_state": "completed", "response_text": "provider response",
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            },
        )

        snapshot_id = "snapshot-0001"
        snapshot_method = "sqlite_backup_api"
        marker = {
            "table": campaign_dependency_preflight.BACKUP_SNAPSHOT_MARKER_TABLE,
            "snapshot_id": snapshot_id,
            "marker_value": "snapshot-marker-0001",
            "committed_at": "2026-07-13T07:55:00Z",
        }
        source_db = root / gate / "source.db"
        source_db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(source_db)
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO proof(value) VALUES ('restored')")
        connection.execute(
            "CREATE TABLE campaign_snapshot_markers ("
            "snapshot_id TEXT PRIMARY KEY NOT NULL, "
            "marker_value TEXT NOT NULL, committed_at TEXT NOT NULL"
            ") WITHOUT ROWID"
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        connection.execute(
            "INSERT INTO campaign_snapshot_markers "
            "(snapshot_id, marker_value, committed_at) VALUES (?, ?, ?)",
            (snapshot_id, marker["marker_value"], marker["committed_at"]),
        )
        connection.commit()
        snapshot_db = root / gate / "reviewed-snapshot.db"
        snapshot_connection = sqlite3.connect(snapshot_db)
        connection.backup(snapshot_connection)
        snapshot_connection.commit()
        result_page_count = int(snapshot_connection.execute("PRAGMA page_count").fetchone()[0])
        snapshot_connection.close()
        wal_busy, wal_frames, checkpointed_frames = connection.execute(
            "PRAGMA wal_checkpoint(FULL)"
        ).fetchone()
        source_page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        connection.close()
        assert wal_frames > 0
        database_bytes = snapshot_db.read_bytes()
        restored_db = _write_bytes_raw(
            root, gate, "backup_restored_database", "application/vnd.sqlite3",
            "native.sqlite3/v1", database_bytes,
        )
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
            directory = tarfile.TarInfo("database")
            directory.type = tarfile.DIRTYPE
            directory.size = 0
            archive.addfile(directory)
            info = tarfile.TarInfo("database/app.db")
            info.size = len(database_bytes)
            archive.addfile(info, io.BytesIO(database_bytes))
        backup_archive = _write_bytes_raw(
            root, gate, "backup_archive", "application/x-tar", "native.tar/v1",
            tar_buffer.getvalue(),
        )
        backup_manifest = _write_json_raw(
            root, gate, "backup_restore_manifest", gate_module._DEPENDENCY_RAW_SCHEMAS["backup_manifest"],
            {
                "snapshot_id": snapshot_id, "archive_path": backup_archive["path"],
                "archive_sha256": backup_archive["sha256"], "restore_completed": True,
                "archive_entries": [
                    {
                        "path": "database", "kind": "directory",
                        "sha256": sha256_bytes(b""), "size_bytes": 0,
                    },
                    {
                        "path": "database/app.db", "kind": "file",
                        "sha256": sha256_bytes(database_bytes),
                        "size_bytes": len(database_bytes),
                    },
                ],
                "restored_database_path": restored_db["path"],
                "source_database_sha256": sha256_bytes(database_bytes),
                "restored_database_sha256": restored_db["sha256"],
                "sqlite_snapshot": {
                    "snapshot_method": snapshot_method,
                    "source_journal_mode": "wal",
                    "wal_checkpoint": {
                        "mode": "FULL", "busy": wal_busy,
                        "log_frames": wal_frames,
                        "checkpointed_frames": checkpointed_frames,
                        "completed": wal_busy == 0 and wal_frames == checkpointed_frames,
                    },
                    "backup_completion": {
                        "method": snapshot_method, "completed": True,
                        "source_page_count": source_page_count,
                        "result_page_count": result_page_count,
                        "result_database_bytes": len(database_bytes),
                    },
                    "snapshot_marker": marker,
                },
            },
        )
        quick_check = _write_json_raw(
            root, gate, "backup_sqlite_check", gate_module._DEPENDENCY_RAW_SCHEMAS["backup_quick_check"],
            {
                "snapshot_id": snapshot_id, "database_path": restored_db["path"],
                "quick_check_rows": ["ok"], "source_sha256": restored_db["sha256"],
                "restored_sha256": restored_db["sha256"],
                "snapshot_method": snapshot_method, "snapshot_marker": marker,
            },
        )

        request_rows: list[dict[str, object]] = []
        for case, statuses in gate_module._SECURITY_RAW_REQUEST_CASES.items():
            count = 2 if case == "cross_worker_session_success" else 1
            for repetition in range(count):
                request_rows.append({
                    "case": case, "request_id": f"request-{case}-{repetition}",
                    "role": "root" if "root_login" in case or "cross_worker" in case else "user",
                    "method": "POST", "path": "/api/security/probe",
                    "csrf_mode": "valid" if case.endswith("success") else "missing_or_boundary",
                    "status": min(statuses), "response_semantic": "observed_expected_boundary",
                    "started_at": "2026-07-13T07:56:00Z",
                    "finished_at": "2026-07-13T07:56:01Z",
                })
        security_requests = _write_jsonl_raw(
            root, gate, "security_requests", gate_module._DEPENDENCY_RAW_SCHEMAS["security_request"],
            request_rows,
        )
        audit_rows: list[dict[str, object]] = []
        previous = "0" * 64
        for sequence, event_type in enumerate(sorted(gate_module._SECURITY_AUDIT_EVENTS), start=1):
            unsigned: dict[str, object] = {
                "sequence": sequence, "event_type": event_type, "actor": "qualification-root",
                "previous_hash": previous, "payload": {"request_id": f"audit-{sequence}"},
            }
            event_hash = sha256_bytes(canonical_json_bytes(unsigned))
            audit_rows.append({**unsigned, "event_hash": event_hash})
            previous = event_hash
        security_audit = _write_jsonl_raw(
            root, gate, "security_audit_chain", gate_module._DEPENDENCY_RAW_SCHEMAS["security_audit"],
            audit_rows,
        )

        bt_native = _external_receipt("bt_seed_download", {
            "seed_started": True, "torrent_created": True, "peer_observed": True,
            "download_terminal": True, "payload_sha256_match": True, "downloaded_via_bt": True,
            "info_hash": info_hash, "download_path": bt_payload["path"],
            "payload_sha256": bt_payload["sha256"], "trace_path": bt_trace["path"],
        })
        comfy_native = _external_receipt("comfyui_terminal", {
            "job_submitted": True, "terminal_polled": True, "history_terminal": True,
            "output_exists": True, "output_decodable": True, "job_id": "job-0001",
            "prompt_id": "prompt-0001", "output_path": comfy_output["path"],
            "output_sha256": comfy_output["sha256"], "history_path": comfy_history["path"],
        })
        ai_native = _external_receipt("ai_provider_terminal", {
            "provider_called": True, "terminal_polled": True, "response_nonempty": True,
            "usage_reported": True, "provider": "real-provider", "model": "real-model",
            "request_id": "request-0001", "exchange_path": ai_exchange["path"],
        })
        backup_native = _external_receipt("backup_restore", {
            "archive_created": True, "archive_readable": True, "restore_completed": True,
            "source_restore_digest_match": True, "sqlite_quick_check": True,
            "manifest_validated": True, "consistent_snapshot_created": True,
            "wal_checkpoint_completed": True, "snapshot_marker_verified": True,
            "backup_api_completed": True, "snapshot_method": snapshot_method,
            "snapshot_marker_id": marker["marker_value"], "snapshot_id": snapshot_id,
            "archive_path": backup_archive["path"], "archive_sha256": backup_archive["sha256"],
            "manifest_path": backup_manifest["path"], "quick_check_path": quick_check["path"],
        })
        security_native = _external_receipt("production_security_sentinel", {
            "production_mode": True, "csrf_enforced": True, "rbac_enforced": True,
            "confirmation_enforced": True, "audit_chain_verified": True,
            "cross_worker_session_verified": True, "request_trace_path": security_requests["path"],
            "audit_chain_path": security_audit["path"],
        })
        external = {
            "bt_seed_download": bt_native, "comfyui_terminal": comfy_native,
            "ai_provider_terminal": ai_native, "backup_restore": backup_native,
            "production_security_sentinel": security_native,
        }
        checks: list[dict[str, object]] = []
        for name in sorted(gate_module._MANDATORY_DEPENDENCIES):
            if name in external:
                evidence: dict[str, object] = external[name]
            elif name == "ffmpeg_hls":
                evidence = {
                    "playlist": playlist["path"], "segment_path": segment["path"],
                    "ffprobe_path": ffprobe["path"],
                }
            else:
                engine = name.removeprefix("browser_")
                evidence = browser_evidence[engine]
            checks.append({"name": name, "status": "PASS", "ok": True, "details": {"evidence": evidence}})

        return {
            **browser_refs,
            "dependency_preflight": _write_json_raw(
                root, gate, "dependency_preflight", "hackme.campaign.dependency-preflight/v1",
                {"status": "PASS", "ok": True, "failed_checks": [], "checks": checks},
            ),
            "bt_receipt": _write_json_raw(root, gate, "bt_receipt", "hackme.campaign.external-dependency-probe/v1", bt_native),
            "bt_protocol_trace": bt_trace,
            "comfyui_receipt": _write_json_raw(root, gate, "comfyui_receipt", "hackme.campaign.external-dependency-probe/v1", comfy_native),
            "comfyui_history": comfy_history,
            "ai_receipt": _write_json_raw(root, gate, "ai_receipt", "hackme.campaign.external-dependency-probe/v1", ai_native),
            "ai_provider_exchange": ai_exchange,
            "backup_receipt": _write_json_raw(root, gate, "backup_receipt", "hackme.campaign.external-dependency-probe/v1", backup_native),
            "backup_restore_manifest": backup_manifest, "backup_sqlite_check": quick_check,
            "security_receipt": _write_json_raw(root, gate, "security_receipt", "hackme.campaign.external-dependency-probe/v1", security_native),
            "security_requests": security_requests, "security_audit_chain": security_audit,
            "hls_playlist": playlist, "hls_segment": segment, "hls_ffprobe": ffprobe,
            "bt_payload": bt_payload, "comfyui_output": comfy_output,
            "backup_archive": backup_archive, "backup_restored_database": restored_db,
        }

    if gate == "180_second_smoke_passed":
        runner = {
            "probe": "campaign_level0_lifecycle_load", "runtime_seconds": 180.5,
            "contract": {"configured_duration_seconds": 180, "configured_concurrency": 32},
            "metrics": {
                "max_active_workers": 32, "operations_completed": 9000,
                "transport_errors": {},
            },
            "unexpected_errors": [], "silent_failures": [],
            "gates": {"duration": True, "load": True, "terminal": True},
            "classification": "PASS", "ok": True,
        }
        return {
            "supervisor_result": _write_json_raw(root, gate, "supervisor_result", "hackme.campaign-supervisor.v1", _supervisor("smoke")),
            "smoke_runner": _write_json_raw(root, gate, "smoke_runner", "hackme.campaign-smoke-load.v2", runner),
        }

    if gate == "60_minute_rehearsal_passed":
        scenarios = (
            "media_long_hls_share", "cloud_drive_share_stream", "bt_download_stream_restart",
            "ai_agent_positive_operations", "comfyui_real_workflows",
            "trading_background_custom_workflow", "pointschain_hft_invariants",
            "wallet_incident_governance", "backup_restore_restart",
            "server_emergency_incident", "media_proxy_cross_browser",
            "community_governance_operations", "final_ui_mobile_prelaunch",
        )
        result: dict[str, dict[str, object]] = {}
        scenario_index: dict[str, object] = {}
        for index, scenario in enumerate(scenarios):
            scenario_raw, index_entry = _write_rehearsal_scenario(
                root,
                gate,
                scenario,
                index,
            )
            result.update(scenario_raw)
            scenario_index[scenario] = index_entry
        runner_authority = _rehearsal_authority(
            started_offset_seconds=0.2,
            finished_offset_seconds=3600.8,
        )
        runner_reference = _write_json_raw(
            root,
            gate,
            "runner_result",
            "hackme.campaign-operational-result/v1",
            {
                **runner_authority,
                "ok": True,
                "verdict": "PASS",
                "classification": "PASS",
                "required_active_test_seconds": 3600,
                "active_test_seconds": 3600.5,
                "invalid_seconds": 0,
                "scenario_scope": "mandatory_full_feature_matrix",
                "mandatory_features_executed": [
                    "planned_restart",
                    "runtime_backup_restore",
                    "comfyui_real_workflow",
                    "bt_terminal_download",
                    "cross_browser_mobile_ui",
                ],
                "skips": [],
                "fallbacks": [],
                "expected_gaps": [],
                "scenario_receipts": scenario_index,
            },
        )
        supervisor = {
            **_supervisor("rehearsal"),
            **_rehearsal_authority(
                started_offset_seconds=0.1,
                finished_offset_seconds=3600.9,
            ),
            "runner_report": _artifact_link(runner_reference),
        }
        result["runner_result"] = runner_reference
        result["supervisor_result"] = _write_json_raw(
            root,
            gate,
            "supervisor_result",
            "hackme.campaign-supervisor.v1",
            supervisor,
        )
        return result

    if gate == "worktree_clean_and_frozen":
        status = _write_bytes_raw(root, gate, "git_status", "text/plain", "native.git-status-porcelain-v1", b"")
        diff = _write_bytes_raw(root, gate, "git_diff_binary", "application/octet-stream", "native.git-diff-binary-v1", b"")
        ls_files = _write_bytes_raw(root, gate, "git_ls_files", "text/plain", "native.git-ls-files-stage-v1", f"100644 {'d' * 40} 0\tapp.py\0".encode())
        submodules = _write_bytes_raw(root, gate, "git_submodule_status", "text/plain", "native.git-submodule-status-v1", b"")
        tracked_native = {"path": "app.py", "index_mode": "100644", "working_sha256": TRACKED_FILE_SHA}
        manifest = _write_jsonl_raw(root, gate, "tracked_manifest", "hackme.source-tracked-entry/v1", [tracked_native])
        protected_manifest = _write_jsonl_raw(
            root, gate, "protected_ignored_manifest",
            gate_module._PROTECTED_ENTRY_SCHEMA_VERSION,
            PROTECTED_ROWS,
        )
        manifest_digest = hashlib.sha256(canonical_json_bytes(tracked_native) + b"\n").hexdigest()
        source = {
            **_source_payload("H0", SOURCE_DIGEST),
            "git_status_empty": True, "git_diff_binary_empty": True,
            "git_status_sha256": status["sha256"], "git_diff_binary_sha256": diff["sha256"],
            "git_ls_files_sha256": ls_files["sha256"], "git_submodule_status_sha256": submodules["sha256"],
            "tracked_manifest_digest": manifest_digest, "tracked_file_count": 1,
            "nonzero_index_stage_paths": [], "submodule_dirty": [], "submodule_worktree_changes": [],
            "missing_tracked_paths": [], "unsupported_tracked_paths": [],
            "artifacts": {
                "git_status": status["path"], "git_diff_binary": diff["path"],
                "git_ls_files": ls_files["path"], "git_submodule_status": submodules["path"],
                "tracked_manifest": manifest["path"],
                "protected_ignored_manifest": protected_manifest["path"],
            },
        }
        return {
            "source_h0": _write_json_raw(root, gate, "source_h0", "hackme.source-freeze.v3", source),
            "git_status": status, "git_diff_binary": diff, "git_ls_files": ls_files,
            "git_submodule_status": submodules, "tracked_manifest": manifest,
            "protected_ignored_manifest": protected_manifest,
        }
    raise AssertionError(gate)


def write_evidence_set(tmp_path: Path, *, now: datetime = NOW) -> dict[str, Path]:
    raw_root = tmp_path / "raw"
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for gate in REQUIRED_FORMAL_GATES:
        payload = {
            "schema_version": GATE_EVIDENCE_SCHEMA_VERSION,
            "gate_name": gate,
            "status": "PASS",
            "machine_verified": True,
            "verification_scope": GATE_POLICIES[gate].verification_scope,
            "actual_execution": True,
            "simulated": False,
            "component_only": False,
            "qualification_campaign_uuid": CAMPAIGN_UUID,
            "commit": COMMIT,
            "source_digest": SOURCE_DIGEST,
            "protected_source_digest": PROTECTED_SOURCE_DIGEST,
            "checked_at": format_utc(now - timedelta(minutes=1)),
            "valid_until": format_utc(now + timedelta(minutes=4)),
            "raw_artifacts": _gate_raw(raw_root, gate),
        }
        path = evidence_root / f"{gate}.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        result[gate] = path
    return result


def _source_authority(evidence: dict[str, Path]) -> dict[str, object]:
    gate = json.loads(evidence["worktree_clean_and_frozen"].read_text(encoding="utf-8"))
    raw_path = Path(gate["raw_artifacts"]["source_h0"]["path"])
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    payload.pop("formal_binding", None)
    return payload


def build_valid(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    evidence = write_evidence_set(tmp_path)
    bundle = tmp_path / "gate_bundle.json"
    result = build_gate_bundle(
        bundle, commit=COMMIT, source_authority=_source_authority(evidence),
        qualification_campaign_uuid=CAMPAIGN_UUID, evidence_paths=evidence, now=NOW,
    )
    assert result["schema_version"] == GATE_BUNDLE_SCHEMA_VERSION
    return bundle, evidence


def _rewrite_json(path: Path, mutate) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _rewrite_bundle(path: Path, mutate) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    payload["bundle_sha256"] = bundle_sha256(payload)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _refresh_raw_reference(evidence_path: Path, role: str, raw_path: Path) -> None:
    raw_path.chmod(0o600)
    _rewrite_json(
        evidence_path,
        lambda payload: payload["raw_artifacts"][role].update({
            "sha256": sha256_bytes(raw_path.read_bytes()),
            "size_bytes": raw_path.stat().st_size,
        }),
    )


def _raw_reference(evidence_path: Path, role: str) -> dict[str, object]:
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    return dict(payload["raw_artifacts"][role])


def _refresh_rehearsal_runner_and_supervisor_links(
    evidence_path: Path,
    *,
    scenario_id: str | None = None,
) -> None:
    runner_reference = _raw_reference(evidence_path, "runner_result")
    runner_path = Path(str(runner_reference["path"]))
    if scenario_id is not None:
        receipt_role = f"scenario_{scenario_id}"
        bundle_role = f"scenario_bundle_{scenario_id}"
        archive_role = f"scenario_archive_{scenario_id}"

        def relink_scenario(payload: dict[str, object]) -> None:
            entry = payload["scenario_receipts"][scenario_id]
            entry["receipt"] = _artifact_link(
                _raw_reference(evidence_path, receipt_role)
            )
            entry["artifact_bundle"] = _artifact_link(
                _raw_reference(evidence_path, bundle_role)
            )
            entry["artifact_archive"] = _artifact_link(
                _raw_reference(evidence_path, archive_role)
            )

        _rewrite_json(runner_path, relink_scenario)
        _refresh_raw_reference(evidence_path, "runner_result", runner_path)
        runner_reference = _raw_reference(evidence_path, "runner_result")
    supervisor_reference = _raw_reference(evidence_path, "supervisor_result")
    supervisor_path = Path(str(supervisor_reference["path"]))
    _rewrite_json(
        supervisor_path,
        lambda payload: payload.update(
            {"runner_report": _artifact_link(runner_reference)}
        ),
    )
    _refresh_raw_reference(
        evidence_path,
        "supervisor_result",
        supervisor_path,
    )


def test_private_semantic_fixtures_rederive_and_public_api_rejects_loose_evidence(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    for gate in REQUIRED_FORMAL_GATES:
        if gate == "all_mandatory_dependencies_verified":
            with pytest.raises(GateBundleError, match="live-source database authority"):
                _validate_semantic_fixture(evidence[gate], gate)
        else:
            result = _validate_semantic_fixture(evidence[gate], gate)
            assert result["status"] == "PASS"
            assert len(result["_derived_sha256"]) == 64

    with pytest.raises(GateBundleError, match="attempt manifest shape mismatch"):
        build_gate_bundle(
            tmp_path / "bundle.json",
            commit=COMMIT,
            source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
            qualification_campaign_uuid=CAMPAIGN_UUID,
            evidence_paths=evidence,
            now=NOW,
        )


def test_native_raw_specs_track_current_producer_schema_constants() -> None:
    specs = gate_module.GATE_RAW_SPECS
    assert specs["cgroup_limits_verified"]["cgroup_readback"].content_schema_version == campaign_cgroup.CGROUP_SCHEMA_VERSION
    for role in ("watchdog_startup", "watchdog_incident", "watchdog_terminal"):
        assert specs["external_watchdog_verified"][role].content_schema_version == campaign_watchdog.WATCHDOG_SCHEMA_VERSION
    for gate, role in (
        ("hard_stop_injection_verified", "state_before"),
        ("hard_stop_injection_verified", "state_after"),
        ("source_drift_detection_verified", "terminal_state"),
    ):
        assert specs[gate][role].content_schema_version == campaign_state.STATE_SCHEMA_VERSION
    assert specs["hard_stop_injection_verified"]["control_after"].content_schema_version == campaign_watchdog.CONTROL_SCHEMA_VERSION
    for role in ("source_h0", "source_h24", "source_restored"):
        assert specs["source_drift_detection_verified"][role].content_schema_version == campaign_source_freeze.SOURCE_FREEZE_SCHEMA_VERSION
    assert specs["source_drift_detection_verified"]["drift_incident"].content_schema_version == campaign_source_freeze.SOURCE_DRIFT_SCHEMA_VERSION
    assert specs["worktree_clean_and_frozen"]["source_h0"].content_schema_version == campaign_source_freeze.SOURCE_FREEZE_SCHEMA_VERSION
    assert specs["worktree_clean_and_frozen"]["protected_ignored_manifest"].content_schema_version == gate_module._PROTECTED_ENTRY_SCHEMA_VERSION
    assert specs["sample_schema_completeness_verified"]["resource_samples"].content_schema_version == campaign_observability.RESOURCE_SAMPLE_SCHEMA_VERSION
    assert specs["production_security_sentinel_verified"]["security_sentinel"].content_schema_version == campaign_security_sentinel.SECURITY_SENTINEL_SCHEMA_VERSION
    dependency_specs = specs["all_mandatory_dependencies_verified"]
    assert dependency_specs["dependency_preflight"].content_schema_version == campaign_dependency_preflight.PREFLIGHT_SCHEMA_VERSION
    for role in ("bt_receipt", "comfyui_receipt", "ai_receipt", "backup_receipt", "security_receipt"):
        assert dependency_specs[role].content_schema_version == campaign_dependency_preflight.EXTERNAL_PROBE_SCHEMA_VERSION
    for engine in ("chromium", "firefox", "webkit"):
        assert dependency_specs[f"browser_{engine}_launch"].content_schema_version == gate_module._DEPENDENCY_RAW_SCHEMAS["browser_launch"]
    assert specs["180_second_smoke_passed"]["supervisor_result"].content_schema_version == operational_campaign_supervisor.SUPERVISOR_SCHEMA_VERSION
    assert specs["180_second_smoke_passed"]["smoke_runner"].content_schema_version == campaign_smoke_load.SMOKE_LOAD_SCHEMA_VERSION
    rehearsal_specs = specs["60_minute_rehearsal_passed"]
    assert rehearsal_specs["supervisor_result"].content_schema_version == operational_campaign_supervisor.SUPERVISOR_SCHEMA_VERSION
    for role, spec in rehearsal_specs.items():
        if role.startswith("scenario_bundle_"):
            assert spec.content_schema_version == campaign_scenario_binding.NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION
        elif role.startswith("scenario_archive_"):
            assert spec.content_schema_version == campaign_scenario_binding.NATIVE_ARTIFACT_ARCHIVE_SCHEMA_VERSION
        elif role.startswith("scenario_"):
            assert spec.content_schema_version == campaign_scenario_binding.RUNTIME_RECEIPT_SCHEMA_VERSION


def test_native_binary_raw_is_stream_hashed_without_retaining_payload(
    tmp_path: Path,
) -> None:
    gate = "all_mandatory_dependencies_verified"
    role = "bt_payload"
    reference = _write_bytes_raw(
        tmp_path,
        gate,
        role,
        "application/octet-stream",
        "native.bt-payload/v1",
        b"streamed-bt-payload" * 4096,
    )

    artifact = gate_module._load_raw_artifact(
        reference,
        gate=gate,
        role=role,
        spec=gate_module.GATE_RAW_SPECS[gate][role],
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        protected_source_digest=PROTECTED_SOURCE_DIGEST,
        campaign_uuid=CAMPAIGN_UUID,
        checked_at=NOW,
        registry=gate_module.ValidationRegistry(),
    )

    try:
        assert artifact.bytes is None
        assert artifact.size_bytes == Path(reference["path"]).stat().st_size
        assert artifact.content_sha256 == reference["sha256"]
    finally:
        artifact.close()


def test_structured_raw_over_size_limit_fails_before_json_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = "production_security_sentinel_verified"
    role = "security_sentinel"
    reference = _write_json_raw(
        tmp_path,
        gate,
        role,
        "hackme.production-security-sentinel.v1",
        {"padding": "x" * 2048},
    )
    monkeypatch.setattr(gate_module, "_MAX_RAW_JSON_BYTES", 128)

    with pytest.raises(GateBundleError, match="bounded size limit"):
        gate_module._load_raw_artifact(
            reference,
            gate=gate,
            role=role,
            spec=gate_module.GATE_RAW_SPECS[gate][role],
            commit=COMMIT,
            source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
            campaign_uuid=CAMPAIGN_UUID,
            checked_at=NOW,
            registry=gate_module.ValidationRegistry(),
        )


def test_gate_aggregate_structured_size_is_bounded_before_raw_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = write_evidence_set(tmp_path)
    path = evidence["180_second_smoke_passed"]
    monkeypatch.setattr(gate_module, "_MAX_GATE_STRUCTURED_BYTES", 1)

    with pytest.raises(GateBundleError, match="aggregate structured"):
        _validate_semantic_fixture(path, "180_second_smoke_passed")


def test_dependency_validation_closes_every_pinned_native_descriptor(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    before = len(list(Path("/proc/self/fd").iterdir()))

    with pytest.raises(GateBundleError, match="live-source database authority"):
        _validate_semantic_fixture(
            evidence["all_mandatory_dependencies_verified"],
            "all_mandatory_dependencies_verified",
        )

    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before


def test_png_validation_reopens_and_forces_pixel_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeImage:
        format = "PNG"
        size = (32, 16)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def verify(self) -> None:
            calls.append("verify")

        def load(self) -> None:
            calls.append("load")

    class FakeArtifact:
        @staticmethod
        def pinned_path() -> Path:
            return Path("/proc/self/fd/999")

    from PIL import Image

    monkeypatch.setattr(Image, "open", lambda _path: FakeImage())
    result = gate_module._validate_png_artifact(FakeArtifact(), label="test PNG")
    assert result == {"width": 32, "height": 16, "pixels": 512}
    assert calls == ["verify", "load"]


def test_png_validation_rejects_dimensions_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    class OversizedImage:
        format = "PNG"
        size = (gate_module._MAX_PNG_DIMENSION + 1, 1)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def verify(self) -> None:
            raise AssertionError("oversized PNG must be rejected before verify")

    from PIL import Image

    monkeypatch.setattr(Image, "open", lambda _path: OversizedImage())
    with pytest.raises(GateBundleError, match="dimensions/pixel count"):
        gate_module._validate_png_artifact(
            type("Artifact", (), {"pinned_path": lambda self: Path("/unused")})(),
            label="oversized PNG",
        )


def test_tar_member_validation_returns_hash_metadata_not_member_bytes(
    tmp_path: Path,
) -> None:
    content = b"sqlite-snapshot" * 1024
    archive_path = tmp_path / "backup-source.tar"
    with tarfile.open(archive_path, mode="w") as archive:
        info = tarfile.TarInfo("database/app.db")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    archive_reference = _write_bytes_raw(
        tmp_path,
        "all_mandatory_dependencies_verified",
        "backup_archive",
        "application/x-tar",
        "native.tar/v1",
        archive_path.read_bytes(),
    )
    artifact = gate_module._load_raw_artifact(
        archive_reference,
        gate="all_mandatory_dependencies_verified",
        role="backup_archive",
        spec=gate_module.GATE_RAW_SPECS["all_mandatory_dependencies_verified"]["backup_archive"],
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        protected_source_digest=PROTECTED_SOURCE_DIGEST,
        campaign_uuid=CAMPAIGN_UUID,
        checked_at=NOW,
        registry=gate_module.ValidationRegistry(),
    )

    try:
        members = gate_module._safe_tar_members(artifact)
    finally:
        artifact.close()

    assert members == {
        "database/app.db": {
            "kind": "file",
            "sha256": sha256_bytes(content),
            "size_bytes": len(content),
        }
    }


def _load_backup_archive_artifact(tmp_path: Path, content: bytes):
    reference = _write_bytes_raw(
        tmp_path,
        "all_mandatory_dependencies_verified",
        "backup_archive",
        "application/x-tar",
        "native.tar/v1",
        content,
    )
    return gate_module._load_raw_artifact(
        reference,
        gate="all_mandatory_dependencies_verified",
        role="backup_archive",
        spec=gate_module.GATE_RAW_SPECS["all_mandatory_dependencies_verified"]["backup_archive"],
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        protected_source_digest=PROTECTED_SOURCE_DIGEST,
        campaign_uuid=CAMPAIGN_UUID,
        checked_at=NOW,
        registry=gate_module.ValidationRegistry(),
    )


@pytest.mark.parametrize("member_name", (
    "../escape.db",
    "/absolute.db",
    "C:/drive.db",
    "\\\\server\\share.db",
    "database//alias.db",
    "database/./alias.db",
    "database/../alias.db",
    "database/cafe\u0301.db",
))
def test_tar_member_paths_fail_closed_on_noncanonical_aliases(
    tmp_path: Path,
    member_name: str,
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(member_name)
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    artifact = _load_backup_archive_artifact(tmp_path, buffer.getvalue())
    try:
        with pytest.raises(GateBundleError, match="backup archive member"):
            gate_module._safe_tar_members(artifact)
    finally:
        artifact.close()


def test_tar_directory_aliases_are_normalized_once_and_duplicates_rejected(
    tmp_path: Path,
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in ("database", "database/"):
            directory = tarfile.TarInfo(name)
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
        info = tarfile.TarInfo("database/app.db")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    artifact = _load_backup_archive_artifact(tmp_path, buffer.getvalue())
    try:
        with pytest.raises(GateBundleError, match="canonical member path is duplicated"):
            gate_module._safe_tar_members(artifact)
    finally:
        artifact.close()


def test_tar_hidden_pax_payload_is_bounded_before_tarfile_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    long_name = "database/" + ("x" * 180) + ".db"
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(long_name)
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    artifact = _load_backup_archive_artifact(tmp_path, buffer.getvalue())
    monkeypatch.setattr(gate_module, "_MAX_ARCHIVE_EXTENSION_PAYLOAD_BYTES", 32)
    try:
        with pytest.raises(GateBundleError, match="extension payload exceeds limit"):
            gate_module._safe_tar_members(artifact)
    finally:
        artifact.close()


def test_tar_compression_and_aggregate_resource_limits_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compressed = io.BytesIO()
    with tarfile.open(fileobj=compressed, mode="w:gz") as archive:
        info = tarfile.TarInfo("database/app.db")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    artifact = _load_backup_archive_artifact(tmp_path / "compressed", compressed.getvalue())
    try:
        with pytest.raises(GateBundleError, match="uncompressed tar"):
            gate_module._safe_tar_members(artifact)
    finally:
        artifact.close()

    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w") as archive:
        info = tarfile.TarInfo("database/app.db")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"xx"))
    artifact = _load_backup_archive_artifact(tmp_path / "bounded", plain.getvalue())
    monkeypatch.setattr(gate_module, "_MAX_ARCHIVE_UNCOMPRESSED_BYTES", 1)
    try:
        with pytest.raises(GateBundleError, match="uncompressed size exceeds limit"):
            gate_module._safe_tar_members(artifact)
    finally:
        artifact.close()


def test_tar_validation_has_metadata_member_and_deadline_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("database/app.db")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))

    artifact = _load_backup_archive_artifact(tmp_path / "metadata", buffer.getvalue())
    monkeypatch.setattr(gate_module, "_MAX_ARCHIVE_METADATA_BYTES", 511)
    try:
        with pytest.raises(GateBundleError, match="metadata exceeds aggregate limit"):
            gate_module._safe_tar_members(artifact)
    finally:
        artifact.close()

    monkeypatch.setattr(gate_module, "_MAX_ARCHIVE_METADATA_BYTES", 64 * 1024**2)
    artifact = _load_backup_archive_artifact(tmp_path / "members", buffer.getvalue())
    monkeypatch.setattr(gate_module, "_MAX_ARCHIVE_MEMBERS", 0)
    try:
        with pytest.raises(GateBundleError, match="member count exceeds limit"):
            gate_module._safe_tar_members(artifact)
    finally:
        artifact.close()

    monkeypatch.setattr(gate_module, "_MAX_ARCHIVE_MEMBERS", 100_000)
    artifact = _load_backup_archive_artifact(tmp_path / "deadline", buffer.getvalue())
    moments = iter((0.0, gate_module._ARCHIVE_VALIDATION_TIMEOUT_SECONDS + 1.0))
    monkeypatch.setattr(gate_module.time, "monotonic", lambda: next(moments, 999.0))
    try:
        with pytest.raises(GateBundleError, match="monotonic deadline"):
            gate_module._safe_tar_members(artifact)
    finally:
        artifact.close()


def test_tar_sqlite_extraction_preserves_twenty_gibibyte_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("database/app.db")
        info.size = 4
        archive.addfile(info, io.BytesIO(b"data"))
    artifact = _load_backup_archive_artifact(tmp_path / "reserve", buffer.getvalue())
    extract_root = tmp_path / "extract"
    extract_root.mkdir(mode=0o700)

    class LowSpace:
        f_bavail = 1
        f_frsize = 4096

    monkeypatch.setattr(gate_module.os, "statvfs", lambda _path: LowSpace())
    try:
        with pytest.raises(GateBundleError, match="20 GiB temporary disk reserve"):
            gate_module._safe_tar_members(
                artifact,
                sqlite_extract_root=extract_root,
                sqlite_extract_paths={},
            )
    finally:
        artifact.close()


def test_backup_gate_rejects_main_database_copy_that_omits_committed_wal_marker(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    gate_name = "all_mandatory_dependencies_verified"
    evidence_path = evidence[gate_name]
    gate_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    references = gate_payload["raw_artifacts"]
    manifest_path = Path(references["backup_restore_manifest"]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    marker = manifest["sqlite_snapshot"]["snapshot_marker"]

    # Establish the schema in the main database, then commit only the proof
    # row to WAL.  A naive byte-copy of the main file remains structurally
    # valid but does not contain the committed snapshot marker.
    source = tmp_path / "wal-omission-source.db"
    connection = sqlite3.connect(source)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO proof(value) VALUES ('base-main-file')")
    connection.execute(
        "CREATE TABLE campaign_snapshot_markers ("
        "snapshot_id TEXT PRIMARY KEY NOT NULL, "
        "marker_value TEXT NOT NULL, committed_at TEXT NOT NULL"
        ") WITHOUT ROWID"
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    connection.execute(
        "INSERT INTO campaign_snapshot_markers "
        "(snapshot_id, marker_value, committed_at) VALUES (?, ?, ?)",
        (marker["snapshot_id"], marker["marker_value"], marker["committed_at"]),
    )
    connection.commit()
    assert Path(f"{source}-wal").stat().st_size > 0
    omitted_bytes = source.read_bytes()

    omitted_copy = tmp_path / "omitted-main-only.db"
    omitted_copy.write_bytes(omitted_bytes)
    omitted_connection = sqlite3.connect(
        f"{omitted_copy.as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        assert omitted_connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert omitted_connection.execute(
            "SELECT snapshot_id FROM campaign_snapshot_markers"
        ).fetchall() == []
        page_count = int(omitted_connection.execute("PRAGMA page_count").fetchone()[0])
    finally:
        omitted_connection.close()
        connection.close()

    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        directory = tarfile.TarInfo("database")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        database = tarfile.TarInfo("database/app.db")
        database.size = len(omitted_bytes)
        archive.addfile(database, io.BytesIO(omitted_bytes))
    archive_path = Path(references["backup_archive"]["path"])
    archive_path.write_bytes(archive_buffer.getvalue())
    restored_path = Path(references["backup_restored_database"]["path"])
    restored_path.write_bytes(omitted_bytes)
    archive_sha = sha256_bytes(archive_path.read_bytes())
    database_sha = sha256_bytes(omitted_bytes)

    def mutate_manifest(payload: dict[str, object]) -> None:
        payload["archive_sha256"] = archive_sha
        payload["source_database_sha256"] = database_sha
        payload["restored_database_sha256"] = database_sha
        for entry in payload["archive_entries"]:
            if entry["kind"] == "file":
                entry.update({"sha256": database_sha, "size_bytes": len(omitted_bytes)})
        completion = payload["sqlite_snapshot"]["backup_completion"]
        completion.update({
            "source_page_count": page_count,
            "result_page_count": page_count,
            "result_database_bytes": len(omitted_bytes),
        })

    _rewrite_json(manifest_path, mutate_manifest)
    quick_path = Path(references["backup_sqlite_check"]["path"])
    _rewrite_json(
        quick_path,
        lambda payload: payload.update({
            "source_sha256": database_sha,
            "restored_sha256": database_sha,
        }),
    )
    receipt_path = Path(references["backup_receipt"]["path"])
    _rewrite_json(
        receipt_path,
        lambda payload: payload["evidence"].update({"archive_sha256": archive_sha}),
    )
    preflight_path = Path(references["dependency_preflight"]["path"])

    def mutate_preflight(payload: dict[str, object]) -> None:
        backup_check = next(
            row for row in payload["checks"] if row["name"] == "backup_restore"
        )
        backup_check["details"]["evidence"]["evidence"]["archive_sha256"] = archive_sha

    _rewrite_json(preflight_path, mutate_preflight)
    for role, raw_path in (
        ("backup_archive", archive_path),
        ("backup_restored_database", restored_path),
        ("backup_restore_manifest", manifest_path),
        ("backup_sqlite_check", quick_path),
        ("backup_receipt", receipt_path),
        ("dependency_preflight", preflight_path),
    ):
        _refresh_raw_reference(evidence_path, role, raw_path)

    with pytest.raises(GateBundleError, match="live-source database authority"):
        _validate_semantic_fixture(evidence_path, gate_name)


def test_handwritten_boolean_and_number_claims_cannot_manufacture_green(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path)
    path = evidence["180_second_smoke_passed"]
    _rewrite_json(path, lambda payload: (
        payload.pop("raw_artifacts"),
        payload.update({
            "assertions": {"everything_passed": True},
            "measurements": {"continuous_active_seconds": 999999},
        }),
    ))
    # The public builder accepts sealed attempt manifests only.  A caller
    # cannot promote this hand-authored summary far enough to inspect claims.
    with pytest.raises(GateBundleError, match="attempt manifest shape mismatch"):
        build_gate_bundle(
            tmp_path / "bundle.json", commit=COMMIT, source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
            qualification_campaign_uuid=CAMPAIGN_UUID, evidence_paths=evidence, now=NOW,
        )


def test_cgroup_io_weight_authority_is_exact_and_fail_closed(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["cgroup_limits_verified"]
    raw_path = Path(str(_raw_reference(evidence_path, "cgroup_readback")["path"]))
    original = json.loads(raw_path.read_text(encoding="utf-8"))
    cases = (
        (
            lambda payload: payload["expected_limits"].pop("io.weight"),
            "cgroup expected limits were weakened",
        ),
        (
            lambda payload: payload["actual_limits"].update(
                {"io.weight": 100}
            ),
            "cgroup kernel limit readback mismatch",
        ),
        (
            lambda payload: payload["controllers_verified"].remove("io"),
            "cgroup controller proof is incomplete",
        ),
    )
    for mutate, error in cases:
        payload = json.loads(json.dumps(original))
        mutate(payload)
        raw_path.write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
        _refresh_raw_reference(evidence_path, "cgroup_readback", raw_path)

        with pytest.raises(GateBundleError, match=error):
            _validate_semantic_fixture(
                evidence_path,
                "cgroup_limits_verified",
            )


@pytest.mark.parametrize("field,value,match", (
    ("simulated", True, "simulated evidence"),
    ("component_only", True, "component-only evidence"),
))
def test_summary_simulation_and_component_evidence_are_rejected(
    tmp_path: Path, field: str, value: bool, match: str,
) -> None:
    evidence = write_evidence_set(tmp_path)
    _rewrite_json(evidence["hard_stop_injection_verified"], lambda payload: payload.update({field: value}))
    with pytest.raises(GateBundleError, match=match):
        _validate_semantic_fixture(
            evidence["hard_stop_injection_verified"],
            "hard_stop_injection_verified",
        )


def test_raw_component_or_wrong_campaign_binding_is_rejected(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path)
    raw_path = Path(json.loads(evidence["production_security_sentinel_verified"].read_text())["raw_artifacts"]["security_sentinel"]["path"])
    _rewrite_json(raw_path, lambda payload: payload["formal_binding"].update({"component_only": True}))
    raw_path.chmod(0o600)
    _rewrite_json(evidence["production_security_sentinel_verified"], lambda payload: payload["raw_artifacts"]["security_sentinel"].update({
        "sha256": sha256_bytes(raw_path.read_bytes()), "size_bytes": raw_path.stat().st_size,
    }))
    with pytest.raises(GateBundleError, match="component raw evidence"):
        _validate_semantic_fixture(
            evidence["production_security_sentinel_verified"],
            "production_security_sentinel_verified",
        )

    evidence = write_evidence_set(tmp_path / "campaign")
    raw_path = Path(json.loads(evidence["cgroup_limits_verified"].read_text())["raw_artifacts"]["cgroup_readback"]["path"])
    _rewrite_json(raw_path, lambda payload: payload["formal_binding"].update({"qualification_campaign_uuid": "qualification-wrong"}))
    raw_path.chmod(0o600)
    _rewrite_json(evidence["cgroup_limits_verified"], lambda payload: payload["raw_artifacts"]["cgroup_readback"].update({
        "sha256": sha256_bytes(raw_path.read_bytes()), "size_bytes": raw_path.stat().st_size,
    }))
    with pytest.raises(GateBundleError, match="raw binding campaign mismatch"):
        _validate_semantic_fixture(
            evidence["cgroup_limits_verified"],
            "cgroup_limits_verified",
        )


def test_security_gate_rederives_online_triad_invariants_instead_of_trusting_ok(
    tmp_path: Path,
) -> None:
    gate = "production_security_sentinel_verified"
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence[gate]
    raw_path = Path(
        json.loads(evidence_path.read_text(encoding="utf-8"))["raw_artifacts"]
        ["security_sentinel"]["path"]
    )

    def tamper(payload: dict[str, object]) -> None:
        reference = payload["audit_evidence"]
        assert isinstance(reference, dict)
        receipt = reference["receipt"]
        assert isinstance(receipt, dict)
        invariants = receipt["invariants"]
        assert isinstance(invariants, dict)
        invariants["audit_log_db_bijection"] = False
        encoded = (
            json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        reference["receipt_sha256"] = hashlib.sha256(encoded).hexdigest()
        reference["receipt_size_bytes"] = len(encoded)
        checks = payload["checks"]
        assert isinstance(checks, list)
        detail = next(
            row["detail"]
            for row in checks
            if isinstance(row, dict)
            and row.get("name") == "audit_evidence_triad_online"
        )
        assert isinstance(detail, dict)
        detail["receipt_sha256"] = reference["receipt_sha256"]
        detail["receipt_size_bytes"] = reference["receipt_size_bytes"]

    _rewrite_json(raw_path, tamper)
    raw_path.chmod(0o600)
    _refresh_raw_reference(evidence_path, "security_sentinel", raw_path)

    with pytest.raises(GateBundleError, match="independent validation"):
        _validate_semantic_fixture(evidence_path, gate)


def test_security_gate_rejects_contract_valid_report_receipt_not_in_pinned_archive(
    tmp_path: Path,
) -> None:
    gate = "production_security_sentinel_verified"
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence[gate]
    raw_path = Path(
        json.loads(evidence_path.read_text(encoding="utf-8"))["raw_artifacts"]
        ["security_sentinel"]["path"]
    )

    def replace_with_different_contract_valid_receipt(
        payload: dict[str, object],
    ) -> None:
        reference = payload["audit_evidence"]
        assert isinstance(reference, dict)
        receipt = reference["receipt"]
        assert isinstance(receipt, dict)
        receipt["captured_at"] = "2026-01-02T03:04:05.678+00:00"
        encoded = (
            json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        reference["receipt_sha256"] = hashlib.sha256(encoded).hexdigest()
        reference["receipt_size_bytes"] = len(encoded)
        checks = payload["checks"]
        assert isinstance(checks, list)
        detail = next(
            row["detail"]
            for row in checks
            if isinstance(row, dict)
            and row.get("name") == "audit_evidence_triad_online"
        )
        assert isinstance(detail, dict)
        detail["receipt_sha256"] = reference["receipt_sha256"]
        detail["receipt_size_bytes"] = reference["receipt_size_bytes"]

    _rewrite_json(raw_path, replace_with_different_contract_valid_receipt)
    raw_path.chmod(0o600)
    _refresh_raw_reference(evidence_path, "security_sentinel", raw_path)

    with pytest.raises(GateBundleError, match="pinned audit archive receipt"):
        _validate_semantic_fixture(evidence_path, gate)


def test_tampered_missing_and_wrong_type_raw_artifacts_fail_closed(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path / "tamper")
    _validate_semantic_fixture(
        evidence["external_watchdog_verified"],
        "external_watchdog_verified",
    )
    raw_ref = json.loads(evidence["external_watchdog_verified"].read_text())["raw_artifacts"]["watchdog_incident"]
    Path(raw_ref["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(GateBundleError, match="size mismatch|SHA-256 mismatch"):
        _validate_semantic_fixture(
            evidence["external_watchdog_verified"],
            "external_watchdog_verified",
        )

    evidence = write_evidence_set(tmp_path / "missing")
    _rewrite_json(evidence["source_drift_detection_verified"], lambda payload: payload["raw_artifacts"].pop("drift_incident"))
    with pytest.raises(GateBundleError, match="raw artifact role set mismatch"):
        _validate_semantic_fixture(
            evidence["source_drift_detection_verified"],
            "source_drift_detection_verified",
        )

    evidence = write_evidence_set(tmp_path / "wrong-type")
    _rewrite_json(evidence["sample_schema_completeness_verified"], lambda payload: payload["raw_artifacts"]["resource_samples"].update({"media_type": "application/json"}))
    with pytest.raises(GateBundleError, match="raw media type mismatch"):
        _validate_semantic_fixture(
            evidence["sample_schema_completeness_verified"],
            "sample_schema_completeness_verified",
        )


def test_cross_gate_raw_path_reuse_is_rejected_before_semantic_reparse(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path)
    cgroup = json.loads(evidence["cgroup_limits_verified"].read_text())["raw_artifacts"]["cgroup_readback"]
    def reuse(payload: dict) -> None:
        row = payload["raw_artifacts"]["watchdog_startup"]
        row.update({
            "path": cgroup["path"], "sha256": cgroup["sha256"], "size_bytes": cgroup["size_bytes"],
            "artifact_id": "artifact:watchdog:reused-cgroup", "gate_name": "external_watchdog_verified",
            "artifact_role": "watchdog_startup", "media_type": "application/json",
            "content_schema_version": "hackme.campaign-watchdog.v1",
        })
    _rewrite_json(evidence["external_watchdog_verified"], reuse)
    registry = gate_module.ValidationRegistry()
    _validate_semantic_fixture(
        evidence["cgroup_limits_verified"],
        "cgroup_limits_verified",
        registry=registry,
    )
    with pytest.raises(GateBundleError, match="raw artifact path reused"):
        _validate_semantic_fixture(
            evidence["external_watchdog_verified"],
            "external_watchdog_verified",
            registry=registry,
        )


def test_handwritten_resource_valid_fields_are_recomputed_from_values(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path)
    ref = json.loads(evidence["sample_schema_completeness_verified"].read_text())["raw_artifacts"]["resource_samples"]
    raw_path = Path(ref["path"])
    rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
    rows[0]["host"]["memory"]["available_bytes"] = None
    raw_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    raw_path.chmod(0o600)
    _rewrite_json(evidence["sample_schema_completeness_verified"], lambda payload: payload["raw_artifacts"]["resource_samples"].update({
        "sha256": sha256_bytes(raw_path.read_bytes()), "size_bytes": raw_path.stat().st_size,
    }))
    with pytest.raises(GateBundleError, match="marks missing value valid"):
        _validate_semantic_fixture(
            evidence["sample_schema_completeness_verified"],
            "sample_schema_completeness_verified",
        )


def test_shrunken_one_field_resource_contract_cannot_report_100_percent(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["sample_schema_completeness_verified"]
    ref = json.loads(evidence_path.read_text())["raw_artifacts"]["resource_samples"]
    raw_path = Path(ref["path"])
    rewritten = []
    for row in (json.loads(line) for line in raw_path.read_text().splitlines()):
        rewritten.append({
            "schema_version": row["schema_version"],
            "formal_binding": row["formal_binding"],
            "host": {"memory": {"available_bytes": 4_000_000_000}},
            "process_roles": {},
            "expected_fields": ["host.memory.available_bytes"],
            "valid_fields": ["host.memory.available_bytes"],
            "missing_fields": [],
            "collector_errors": {},
            "hard_limit_state": {"ok": True, "tripped": []},
            "field_completeness_ratio": 1.0,
        })
    raw_path.write_text("".join(json.dumps(row) + "\n" for row in rewritten), encoding="utf-8")
    _refresh_raw_reference(evidence_path, "resource_samples", raw_path)
    with pytest.raises(GateBundleError, match="formal collector contract"):
        _validate_semantic_fixture(evidence_path, "sample_schema_completeness_verified")


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    (
        ("runner_id", "native.runner.unregistered", "runner_id_mismatch"),
        ("terminal_state", "completed", "terminal_state_not_success"),
    ),
)
def test_rehearsal_receipt_requires_reviewed_ids_and_success_terminal(
    tmp_path: Path, field: str, value: str, expected: str,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    role = "scenario_media_long_hls_share"
    ref = json.loads(evidence_path.read_text())["raw_artifacts"][role]
    raw_path = Path(ref["path"])
    _rewrite_json(raw_path, lambda payload: payload.update({field: value}))
    _refresh_raw_reference(evidence_path, role, raw_path)
    with pytest.raises(GateBundleError, match=expected):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rejects_stale_receipt_from_another_scenario_attempt(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    role = f"scenario_{scenario_id}"
    receipt_path = Path(str(_raw_reference(evidence_path, role)["path"]))
    _rewrite_json(
        receipt_path,
        lambda payload: payload["authority"].update(
            {"scenario_attempt_uuid": "stale-scenario-attempt-9999"}
        ),
    )
    _refresh_raw_reference(evidence_path, role, receipt_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=scenario_id,
    )

    with pytest.raises(GateBundleError, match="runner scenario attempt mismatch"):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rejects_inner_receipt_from_another_campaign(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    role = f"scenario_{scenario_id}"
    receipt_path = Path(str(_raw_reference(evidence_path, role)["path"]))
    _rewrite_json(
        receipt_path,
        lambda payload: payload["authority"].update(
            {"campaign_uuid": "other-rehearsal-campaign-0002"}
        ),
    )
    _refresh_raw_reference(evidence_path, role, receipt_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=scenario_id,
    )

    with pytest.raises(GateBundleError, match="scenario authority mismatch"):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rejects_reused_scenario_native_invocation(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    first = "media_long_hls_share"
    second = "cloud_drive_share_stream"
    first_receipt = Path(
        str(_raw_reference(evidence_path, f"scenario_{first}")["path"])
    )
    reused = json.loads(first_receipt.read_text(encoding="utf-8"))[
        "authority"
    ]["native_invocation_id"]
    second_bundle_role = f"scenario_bundle_{second}"
    second_bundle = Path(
        str(_raw_reference(evidence_path, second_bundle_role)["path"])
    )
    _rewrite_json(
        second_bundle,
        lambda payload: payload["authority"].update(
            {"native_invocation_id": reused}
        ),
    )
    _refresh_raw_reference(evidence_path, second_bundle_role, second_bundle)
    second_receipt_role = f"scenario_{second}"
    second_receipt = Path(
        str(_raw_reference(evidence_path, second_receipt_role)["path"])
    )

    def reuse_receipt_invocation(payload: dict[str, object]) -> None:
        payload["authority"]["native_invocation_id"] = reused
        payload["artifact_bundle"].update(
            _artifact_link(_raw_reference(evidence_path, second_bundle_role))
        )

    _rewrite_json(second_receipt, reuse_receipt_invocation)
    _refresh_raw_reference(evidence_path, second_receipt_role, second_receipt)
    runner_path = Path(
        str(_raw_reference(evidence_path, "runner_result")["path"])
    )
    _rewrite_json(
        runner_path,
        lambda payload: payload["scenario_receipts"][second].update(
            {"native_invocation_id": reused}
        ),
    )
    _refresh_raw_reference(evidence_path, "runner_result", runner_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=second,
    )

    with pytest.raises(GateBundleError, match="native invocation IDs are reused"):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rejects_scenario_interval_outside_runner_authority(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    receipt_role = f"scenario_{scenario_id}"
    bundle_role = f"scenario_bundle_{scenario_id}"
    receipt_path = Path(str(_raw_reference(evidence_path, receipt_role)["path"]))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    authority = dict(receipt["authority"])
    authority["started_at"] = format_utc(
        gate_module.parse_utc(
            authority["started_at"],
            label="test scenario start",
        ) - timedelta(seconds=1)
    )
    authority["finished_at"] = format_utc(
        gate_module.parse_utc(
            authority["finished_at"],
            label="test scenario finish",
        ) - timedelta(seconds=1)
    )
    authority["started_monotonic_ns"] -= 1_000_000_000
    authority["finished_monotonic_ns"] -= 1_000_000_000

    bundle_path = Path(str(_raw_reference(evidence_path, bundle_role)["path"]))
    _rewrite_json(
        bundle_path,
        lambda payload: payload.update({"authority": authority}),
    )
    _refresh_raw_reference(evidence_path, bundle_role, bundle_path)
    bundle_reference = _raw_reference(evidence_path, bundle_role)

    def move_receipt(payload: dict[str, object]) -> None:
        payload["authority"] = authority
        payload["artifact_bundle"].update(_artifact_link(bundle_reference))

    _rewrite_json(receipt_path, move_receipt)
    _refresh_raw_reference(evidence_path, receipt_role, receipt_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=scenario_id,
    )

    with pytest.raises(
        GateBundleError,
        match="(wall|monotonic) interval escapes its parent",
    ):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_authority_must_match_outer_native_invocation(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    capture_authority = _capture_authority(
        "60_minute_rehearsal_passed",
        evidence_path,
    )
    other_invocation = "native:60_minute_rehearsal_passed:other-invocation"
    capture_authority["native_execution"]["invocation_id"] = other_invocation
    capture_authority["native_execution"]["producer"]["invocation_id"] = (
        other_invocation
    )

    with pytest.raises(GateBundleError, match="native invocation mismatch"):
        gate_module._validate_unsealed_gate_evidence(
            evidence_path,
            gate_name="60_minute_rehearsal_passed",
            commit=COMMIT,
            source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
            qualification_campaign_uuid=CAMPAIGN_UUID,
            now=NOW,
            capture_authority=capture_authority,
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("path", "/tmp/substituted-rehearsal-bundle.json"),
        ("sha256", "0" * 64),
        ("size_bytes", 1),
    ),
)
def test_rehearsal_rejects_runner_bundle_link_substitution(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    runner_reference = _raw_reference(evidence_path, "runner_result")
    runner_path = Path(str(runner_reference["path"]))

    def substitute(payload: dict[str, object]) -> None:
        payload["scenario_receipts"][scenario_id]["artifact_bundle"][field] = (
            invalid_value
        )

    _rewrite_json(runner_path, substitute)
    _refresh_raw_reference(evidence_path, "runner_result", runner_path)
    _refresh_rehearsal_runner_and_supervisor_links(evidence_path)

    with pytest.raises(
        GateBundleError,
        match="runner scenario bundle .* path/hash/size mismatch",
    ):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_reopens_archive_and_rejects_resealed_member_mutation(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    archive_role = f"scenario_archive_{scenario_id}"
    bundle_role = f"scenario_bundle_{scenario_id}"
    receipt_role = f"scenario_{scenario_id}"
    archive_path = Path(str(_raw_reference(evidence_path, archive_role)["path"]))
    with tarfile.open(archive_path, mode="r:") as archive:
        members = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }
    members["artifacts/proof.json"] = members["artifacts/proof.json"].replace(
        b'"success"',
        b'"failure"',
    )
    archive_path.write_bytes(_tar_bytes(members))
    _refresh_raw_reference(evidence_path, archive_role, archive_path)
    archive_reference = _raw_reference(evidence_path, archive_role)

    bundle_path = Path(str(_raw_reference(evidence_path, bundle_role)["path"]))
    _rewrite_json(
        bundle_path,
        lambda payload: payload["artifact_archive"].update(
            _artifact_link(archive_reference)
        ),
    )
    _refresh_raw_reference(evidence_path, bundle_role, bundle_path)
    bundle_reference = _raw_reference(evidence_path, bundle_role)

    receipt_path = Path(str(_raw_reference(evidence_path, receipt_role)["path"]))

    def relink_receipt(payload: dict[str, object]) -> None:
        payload["artifact_bundle"].update(_artifact_link(bundle_reference))
        payload["artifact_bundle"].update(
            {
                "artifact_archive_sha256": archive_reference["sha256"],
                "artifact_archive_size_bytes": archive_reference["size_bytes"],
            }
        )

    _rewrite_json(receipt_path, relink_receipt)
    _refresh_raw_reference(evidence_path, receipt_role, receipt_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=scenario_id,
    )

    with pytest.raises(
        GateBundleError,
        match="archive member inventory/content mismatch",
    ):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rejects_receipt_bundle_evidence_result_mismatch(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    bundle_role = f"scenario_bundle_{scenario_id}"
    receipt_role = f"scenario_{scenario_id}"
    binding = campaign_scenario_binding.FORMAL_SCENARIO_BINDINGS[scenario_id]
    evidence_id = next(iter(binding.evidence_adapter_ids))
    bundle_path = Path(str(_raw_reference(evidence_path, bundle_role)["path"]))
    _rewrite_json(
        bundle_path,
        lambda payload: payload["evidence_adapter_results"][evidence_id].update(
            {"native_observation_ids": ["observation.from.other.attempt"]}
        ),
    )
    _refresh_raw_reference(evidence_path, bundle_role, bundle_path)
    bundle_reference = _raw_reference(evidence_path, bundle_role)
    receipt_path = Path(str(_raw_reference(evidence_path, receipt_role)["path"]))
    _rewrite_json(
        receipt_path,
        lambda payload: payload["artifact_bundle"].update(
            _artifact_link(bundle_reference)
        ),
    )
    _refresh_raw_reference(evidence_path, receipt_role, receipt_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=scenario_id,
    )

    with pytest.raises(GateBundleError, match="receipt/bundle evidence result mismatch"):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rejects_pass_receipt_bound_to_failed_bundle(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    bundle_role = f"scenario_bundle_{scenario_id}"
    receipt_role = f"scenario_{scenario_id}"
    bundle_path = Path(str(_raw_reference(evidence_path, bundle_role)["path"]))
    _rewrite_json(
        bundle_path,
        lambda payload: payload.update({
            "candidate_status": "FAIL_HARNESS",
            "diagnostics": ["injected_bundle_failure"],
        }),
    )
    _refresh_raw_reference(evidence_path, bundle_role, bundle_path)
    bundle_reference = _raw_reference(evidence_path, bundle_role)
    receipt_path = Path(str(_raw_reference(evidence_path, receipt_role)["path"]))
    _rewrite_json(
        receipt_path,
        lambda payload: payload["artifact_bundle"].update(
            _artifact_link(bundle_reference)
        ),
    )
    _refresh_raw_reference(evidence_path, receipt_role, receipt_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=scenario_id,
    )

    with pytest.raises(
        GateBundleError,
        match="receipt/bundle status or diagnostics mismatch",
    ):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rederives_synchronized_observation_ids_from_archive(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    binding = campaign_scenario_binding.FORMAL_SCENARIO_BINDINGS[scenario_id]
    evidence_id = next(iter(binding.evidence_adapter_ids))
    bundle_role = f"scenario_bundle_{scenario_id}"
    receipt_role = f"scenario_{scenario_id}"
    forged_ids = ["native.observation." + ("0" * 64)]
    bundle_path = Path(str(_raw_reference(evidence_path, bundle_role)["path"]))
    _rewrite_json(
        bundle_path,
        lambda payload: payload["evidence_adapter_results"][evidence_id].update(
            {"native_observation_ids": forged_ids}
        ),
    )
    _refresh_raw_reference(evidence_path, bundle_role, bundle_path)
    bundle_reference = _raw_reference(evidence_path, bundle_role)
    receipt_path = Path(str(_raw_reference(evidence_path, receipt_role)["path"]))

    def synchronize_receipt(payload: dict[str, object]) -> None:
        payload["artifact_bundle"].update(_artifact_link(bundle_reference))
        payload["evidence_receipts"][evidence_id]["native_observation_ids"] = (
            forged_ids
        )

    _rewrite_json(receipt_path, synchronize_receipt)
    _refresh_raw_reference(evidence_path, receipt_role, receipt_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=scenario_id,
    )

    with pytest.raises(
        GateBundleError,
        match="sealed evidence re-derivation mismatch",
    ):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rederives_semantics_after_coherently_resealed_archive(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    binding = campaign_scenario_binding.FORMAL_SCENARIO_BINDINGS[scenario_id]
    evidence_id = next(iter(binding.evidence_adapter_ids))
    archive_role = f"scenario_archive_{scenario_id}"
    bundle_role = f"scenario_bundle_{scenario_id}"
    receipt_role = f"scenario_{scenario_id}"
    archive_path = Path(str(_raw_reference(evidence_path, archive_role)["path"]))
    with tarfile.open(archive_path, mode="r:") as archive:
        members = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }
    summary_member_path = "artifacts/native_evidence_summary.json"
    summary_payload = json.loads(members[summary_member_path])
    summary_payload["scenario_assertions"][evidence_id] = False
    members[summary_member_path] = json.dumps(
        summary_payload,
        sort_keys=True,
    ).encode("utf-8")
    archive_path.write_bytes(_tar_bytes(members))
    _refresh_raw_reference(evidence_path, archive_role, archive_path)
    archive_reference = _raw_reference(evidence_path, archive_role)

    bundle_path = Path(str(_raw_reference(evidence_path, bundle_role)["path"]))
    bundle_payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    summary_id = f"native.summary.{scenario_id}"
    manifest_id = f"native.manifest.{scenario_id}"
    summary_content = members[summary_member_path]
    summary_sha256 = sha256_bytes(summary_content)
    for item in bundle_payload["member_inventory"]:
        if item["artifact_id"] == summary_id:
            item["sha256"] = summary_sha256
            item["size_bytes"] = len(summary_content)
    summary_record = bundle_payload["artifact_records"][summary_id]
    summary_record["sha256"] = summary_sha256
    summary_record["size"] = len(summary_content)
    summary_record["format_validation"]["details"]["size"] = len(summary_content)
    summary_record["secret_scan"]["scanned_bytes"] = len(summary_content)
    bundle_payload["artifact_archive"].update(_artifact_link(archive_reference))
    bundle_payload["member_inventory_sha256"] = (
        campaign_scenario_binding.scenario_member_inventory_sha256(
            bundle_payload["member_inventory"]
        )
    )
    manifest_payload = json.loads(members["manifest/evidence.json"])
    artifact_payloads = {}
    artifact_sha256 = {}
    for item in bundle_payload["member_inventory"]:
        artifact_id = item["artifact_id"]
        if artifact_id == manifest_id:
            continue
        artifact_payloads[artifact_id] = json.loads(members[item["member_path"]])
        artifact_sha256[artifact_id] = item["sha256"]
    validators = campaign_scenario_binding.build_strict_native_validator_registry(
        bindings={scenario_id: binding}
    )
    for validator_id in (
        binding.terminal_validator_ids
        + binding.cleanup_validator_ids
        + binding.artifact_validator_ids
    ):
        registration = validators[validator_id]
        bundle_payload["validator_results"][validator_id] = dict(
            registration.handler(
                registration=registration,
                manifest=manifest_payload,
                artifact_payloads=artifact_payloads,
                artifact_sha256=artifact_sha256,
                artifact_records=bundle_payload["artifact_records"],
                manifest_record=bundle_payload["manifest_record"],
            )
        )
    bundle_path.write_text(
        json.dumps(bundle_payload, sort_keys=True),
        encoding="utf-8",
    )
    _refresh_raw_reference(evidence_path, bundle_role, bundle_path)
    bundle_reference = _raw_reference(evidence_path, bundle_role)

    receipt_path = Path(str(_raw_reference(evidence_path, receipt_role)["path"]))

    def relink_receipt(payload: dict[str, object]) -> None:
        payload["artifact_bundle"].update(_artifact_link(bundle_reference))
        payload["artifact_bundle"].update({
            "member_inventory_sha256": bundle_payload[
                "member_inventory_sha256"
            ],
            "artifact_archive_sha256": archive_reference["sha256"],
            "artifact_archive_size_bytes": archive_reference["size_bytes"],
        })

    _rewrite_json(receipt_path, relink_receipt)
    _refresh_raw_reference(evidence_path, receipt_role, receipt_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=scenario_id,
    )

    with pytest.raises(
        GateBundleError,
        match="sealed evidence re-derivation mismatch",
    ):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rejects_incomplete_artifact_record_schema(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    scenario_id = "media_long_hls_share"
    bundle_role = f"scenario_bundle_{scenario_id}"
    receipt_role = f"scenario_{scenario_id}"
    bundle_path = Path(str(_raw_reference(evidence_path, bundle_role)["path"]))

    def remove_secret_scan(payload: dict[str, object]) -> None:
        artifact_id = next(iter(payload["artifact_records"]))
        payload["artifact_records"][artifact_id].pop("secret_scan")

    _rewrite_json(bundle_path, remove_secret_scan)
    _refresh_raw_reference(evidence_path, bundle_role, bundle_path)
    bundle_reference = _raw_reference(evidence_path, bundle_role)
    receipt_path = Path(str(_raw_reference(evidence_path, receipt_role)["path"]))
    _rewrite_json(
        receipt_path,
        lambda payload: payload["artifact_bundle"].update(
            _artifact_link(bundle_reference)
        ),
    )
    _refresh_raw_reference(evidence_path, receipt_role, receipt_path)
    _refresh_rehearsal_runner_and_supervisor_links(
        evidence_path,
        scenario_id=scenario_id,
    )

    with pytest.raises(GateBundleError, match="record_shape_mismatch"):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


def test_rehearsal_rejects_symlinked_archive_even_when_target_is_unchanged(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["60_minute_rehearsal_passed"]
    role = "scenario_archive_media_long_hls_share"
    archive_path = Path(str(_raw_reference(evidence_path, role)["path"]))
    target = archive_path.with_name("archive-target.tar")
    target.write_bytes(archive_path.read_bytes())
    target.chmod(0o600)
    archive_path.unlink()
    archive_path.symlink_to(target)

    with pytest.raises(GateBundleError, match="symlink|regular file"):
        _validate_semantic_fixture(evidence_path, "60_minute_rehearsal_passed")


@pytest.mark.parametrize("changed_path", (
    ".hackme_capacity_defaults.env",
    ".hackme_capacity_report.json",
))
def test_old_bundle_rejected_when_protected_launcher_input_changes_before_formal_h0(
    tmp_path: Path, changed_path: str,
) -> None:
    evidence = write_evidence_set(tmp_path / "qualification")
    current = _source_authority(evidence)
    rows = [dict(row) for row in PROTECTED_ROWS]
    for row in rows:
        if row["path"] == changed_path:
            row["working_sha256"] = "e" * 64
            row["mtime_ns"] = int(row["mtime_ns"]) + 1
            row["ctime_ns"] = int(row["ctime_ns"]) + 1
    manifest_digest, content_digest = _protected_digests(rows)
    current_manifest = tmp_path / "current-h0" / "protected_ignored_manifest.jsonl"
    current_manifest.parent.mkdir(parents=True)
    current_manifest.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    current["artifacts"]["protected_ignored_manifest"] = str(current_manifest)
    current["protected_ignored_manifest_digest"] = manifest_digest
    current["protected_ignored_content_digest"] = content_digest
    with pytest.raises(GateBundleError, match="protected source digest differs"):
        gate_module._resolve_source_identity(
            source_authority=current,
            source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
        )


def test_protected_manifest_rejects_unsafe_type_even_with_updated_reference(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["worktree_clean_and_frozen"]
    ref = json.loads(evidence_path.read_text())["raw_artifacts"]["protected_ignored_manifest"]
    raw_path = Path(ref["path"])
    rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
    rows[0].update({"kind": "symlink", "symlink_target": "/tmp/redirect"})
    raw_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    _refresh_raw_reference(evidence_path, "protected_ignored_manifest", raw_path)
    with pytest.raises(GateBundleError, match="unsafe type"):
        _validate_semantic_fixture(evidence_path, "worktree_clean_and_frozen")


def test_hand_authored_bt_receipt_and_arbitrary_payload_do_not_replace_protocol_trace(
    tmp_path: Path,
) -> None:
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence["all_mandatory_dependencies_verified"]
    refs = json.loads(evidence_path.read_text())["raw_artifacts"]
    payload_path = Path(refs["bt_payload"]["path"])
    payload_path.write_bytes(b"arbitrary-not-the-traced-download")
    _refresh_raw_reference(evidence_path, "bt_payload", payload_path)
    new_sha = sha256_bytes(payload_path.read_bytes())

    receipt_path = Path(refs["bt_receipt"]["path"])
    _rewrite_json(
        receipt_path,
        lambda payload: payload["evidence"].update({"payload_sha256": new_sha}),
    )
    _refresh_raw_reference(evidence_path, "bt_receipt", receipt_path)

    preflight_path = Path(refs["dependency_preflight"]["path"])
    def rewrite_preflight(payload: dict) -> None:
        row = next(item for item in payload["checks"] if item["name"] == "bt_seed_download")
        row["details"]["evidence"]["evidence"]["payload_sha256"] = new_sha
    _rewrite_json(preflight_path, rewrite_preflight)
    _refresh_raw_reference(evidence_path, "dependency_preflight", preflight_path)

    with pytest.raises(GateBundleError, match="BT protocol trace"):
        _validate_semantic_fixture(evidence_path, "all_mandatory_dependencies_verified")


def test_rehearsal_transport_shortcut_cannot_replace_native_terminal_receipt(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path)
    role = "scenario_media_long_hls_share"
    ref = json.loads(evidence["60_minute_rehearsal_passed"].read_text())["raw_artifacts"][role]
    raw_path = Path(ref["path"])
    _rewrite_json(raw_path, lambda payload: payload.update({"http_status": 200}))
    raw_path.chmod(0o600)
    _rewrite_json(evidence["60_minute_rehearsal_passed"], lambda payload: payload["raw_artifacts"][role].update({
        "sha256": sha256_bytes(raw_path.read_bytes()), "size_bytes": raw_path.stat().st_size,
    }))
    with pytest.raises(GateBundleError, match="canonical receipt validation failed"):
        _validate_semantic_fixture(
            evidence["60_minute_rehearsal_passed"],
            "60_minute_rehearsal_passed",
        )


def test_bundle_semantic_tamper_changes_digest_and_is_rejected(tmp_path: Path) -> None:
    payload = {
        "schema_version": GATE_BUNDLE_SCHEMA_VERSION,
        "qualification_campaign_uuid": CAMPAIGN_UUID,
        "generated_at": format_utc(NOW),
        "valid_until": format_utc(NOW + timedelta(hours=1)),
        "commit": COMMIT,
        "source_digest": SOURCE_DIGEST,
        "protected_source_digest": PROTECTED_SOURCE_DIGEST,
        "required_gates": list(REQUIRED_FORMAL_GATES),
        "gates": {},
        "ok": True,
    }
    original_digest = bundle_sha256(payload)
    payload["bundle_sha256"] = original_digest
    payload["gates"] = {"tampered": {"derived_sha256": "0" * 64}}
    assert bundle_sha256(payload) != original_digest
    bundle = tmp_path / "tampered-bundle.json"
    bundle.write_text(json.dumps(payload), encoding="utf-8")
    bundle.chmod(0o600)
    with pytest.raises(GateBundleError, match="bundle digest mismatch"):
        validate_gate_bundle(
            bundle,
            commit=COMMIT,
            source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
            now=NOW,
        )


def test_expired_evidence_and_group_writable_raw_file_are_rejected(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path / "expired", now=NOW - timedelta(hours=2))
    with pytest.raises(GateBundleError, match="expired"):
        _validate_semantic_fixture(
            evidence["cgroup_limits_verified"],
            "cgroup_limits_verified",
            now=NOW,
        )

    evidence = write_evidence_set(tmp_path / "writable")
    ref = json.loads(evidence["cgroup_limits_verified"].read_text())["raw_artifacts"]["pid_placement"]
    Path(ref["path"]).chmod(0o620)
    with pytest.raises(GateBundleError, match="group/world writable"):
        _validate_semantic_fixture(
            evidence["cgroup_limits_verified"],
            "cgroup_limits_verified",
        )


def test_raw_reference_requires_exact_canonical_path_string(tmp_path: Path) -> None:
    gate = "all_mandatory_dependencies_verified"
    role = "bt_payload"
    reference = _write_bytes_raw(
        tmp_path,
        gate,
        role,
        "application/octet-stream",
        "native.bt-payload/v1",
        b"canonical-path-authority",
    )
    raw_path = Path(str(reference["path"]))
    alias_directory = raw_path.parent / "alias-directory"
    alias_directory.mkdir()
    reference["path"] = str(alias_directory / ".." / raw_path.name)

    with pytest.raises(GateBundleError, match="exact canonical"):
        gate_module._load_raw_artifact(
            reference,
            gate=gate,
            role=role,
            spec=gate_module.GATE_RAW_SPECS[gate][role],
            commit=COMMIT,
            source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
            campaign_uuid=CAMPAIGN_UUID,
            checked_at=NOW,
            registry=gate_module.ValidationRegistry(),
        )


def test_evidence_keeps_caller_lexical_inode_through_final_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = "production_security_sentinel_verified"
    evidence = write_evidence_set(tmp_path)
    evidence_path = evidence[gate]
    alias_directory = evidence_path.parent / "evidence-alias-directory"
    alias_directory.mkdir()
    alias_path = str(alias_directory / ".." / evidence_path.name)
    with pytest.raises(GateBundleError, match="exact canonical"):
        _validate_semantic_fixture(Path(alias_path), gate)

    moved_path = evidence_path.with_name(f"{evidence_path.name}.moved")
    original_read = gate_module._stable_read
    swapped = False

    def read_then_replace(path: Path, *args, **kwargs):
        nonlocal swapped
        result = original_read(path, *args, **kwargs)
        if (
            not swapped
            and Path(path) == evidence_path
            and kwargs.get("label") == f"{gate} evidence"
        ):
            evidence_path.rename(moved_path)
            evidence_path.symlink_to(moved_path)
            swapped = True
        return result

    monkeypatch.setattr(gate_module, "_stable_read", read_then_replace)
    with pytest.raises(GateBundleError, match="regular file|changed"):
        _validate_semantic_fixture(evidence_path, gate)
    assert swapped is True
    assert evidence_path.is_symlink()


def test_pinned_raw_authority_never_resolves_to_rename_symlink_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = "all_mandatory_dependencies_verified"
    role = "bt_payload"
    reference = _write_bytes_raw(
        tmp_path,
        gate,
        role,
        "application/octet-stream",
        "native.bt-payload/v1",
        b"pinned-before-rename",
    )
    lexical_path = Path(str(reference["path"]))
    moved_path = lexical_path.with_name(f"{lexical_path.name}.moved")
    original_pin = gate_module._pin_stable_artifact

    def pin_then_replace(path: Path, *, expected: os.stat_result, label: str) -> int:
        descriptor = original_pin(path, expected=expected, label=label)
        path.rename(moved_path)
        path.symlink_to(moved_path)
        return descriptor

    monkeypatch.setattr(gate_module, "_pin_stable_artifact", pin_then_replace)
    artifact = gate_module._load_raw_artifact(
        reference,
        gate=gate,
        role=role,
        spec=gate_module.GATE_RAW_SPECS[gate][role],
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        protected_source_digest=PROTECTED_SOURCE_DIGEST,
        campaign_uuid=CAMPAIGN_UUID,
        checked_at=NOW,
        registry=gate_module.ValidationRegistry(),
    )
    try:
        assert artifact.path == lexical_path
        assert artifact.path != moved_path
        with pytest.raises(GateBundleError, match="regular file|changed"):
            gate_module._verify_raw_artifact_unchanged(artifact)
    finally:
        artifact.close()
    assert lexical_path.is_symlink()


def test_hls_sparse_segment_uses_only_pinned_376_byte_prefix(tmp_path: Path) -> None:
    gate = "all_mandatory_dependencies_verified"
    role = "hls_segment"
    path = (tmp_path / "large-segment.ts").resolve()
    prefix = bytes([0x47]) + b"\0" * 187 + bytes([0x47]) + b"\0" * 187
    with path.open("wb") as handle:
        handle.write(prefix)
        handle.truncate(65 * gate_module._MIB)
    path.chmod(0o600)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(gate_module._MIB), b""):
            digest.update(block)
    reference = {
        "schema_version": RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION,
        "artifact_id": "artifact:large-hls-segment",
        "gate_name": gate,
        "artifact_role": role,
        "path": str(path),
        "sha256": digest.hexdigest(),
        "size_bytes": path.stat().st_size,
        "media_type": "video/mp2t",
        "content_schema_version": "native.hls-segment/v1",
        "qualification_campaign_uuid": CAMPAIGN_UUID,
        "commit": COMMIT,
        "source_digest": SOURCE_DIGEST,
        "protected_source_digest": PROTECTED_SOURCE_DIGEST,
    }
    artifact = gate_module._load_raw_artifact(
        reference,
        gate=gate,
        role=role,
        spec=gate_module.GATE_RAW_SPECS[gate][role],
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        protected_source_digest=PROTECTED_SOURCE_DIGEST,
        campaign_uuid=CAMPAIGN_UUID,
        checked_at=NOW,
        registry=gate_module.ValidationRegistry(),
    )
    try:
        assert artifact.size_bytes > gate_module._MAX_SMALL_NATIVE_BYTES
        assert artifact.bytes is None
        assert artifact.descriptor is not None
        before_offset = os.lseek(artifact.descriptor, 0, os.SEEK_CUR)
        observed = gate_module._validate_hls_segment_prefix(artifact)
        after_offset = os.lseek(artifact.descriptor, 0, os.SEEK_CUR)
        assert observed == prefix
        assert len(observed) == 376
        assert artifact.bytes is None
        assert before_offset == after_offset == 0
        gate_module._verify_raw_artifact_unchanged(artifact)
    finally:
        artifact.close()


def test_bundle_and_evidence_apis_reject_symlink_leaf_paths(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle-target.json"
    bundle.write_text("{}", encoding="utf-8")
    bundle_link = tmp_path / "bundle-link.json"
    bundle_link.symlink_to(bundle)
    with pytest.raises(GateBundleError, match="symlink"):
        validate_gate_bundle(
            bundle_link,
            commit=COMMIT,
            source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
            now=NOW,
        )

    evidence_target = tmp_path / "evidence-target.json"
    evidence_target.write_text("{}", encoding="utf-8")
    evidence_link = tmp_path / "evidence-link.json"
    evidence_link.symlink_to(evidence_target)
    linked_evidence = {gate: evidence_link for gate in REQUIRED_FORMAL_GATES}
    with pytest.raises(GateBundleError, match="symlink"):
        build_gate_bundle(
            tmp_path / "linked-evidence-bundle.json",
            commit=COMMIT,
            source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
            qualification_campaign_uuid=CAMPAIGN_UUID,
            evidence_paths=linked_evidence,
            now=NOW,
        )

    output_target = tmp_path / "output-target.json"
    output_target.write_text("{}", encoding="utf-8")
    output_link = tmp_path / "output-link.json"
    output_link.symlink_to(output_target)
    with pytest.raises(GateBundleError, match="symlink"):
        build_gate_bundle(
            output_link,
            commit=COMMIT,
            source_digest=SOURCE_DIGEST,
            protected_source_digest=PROTECTED_SOURCE_DIGEST,
            qualification_campaign_uuid=CAMPAIGN_UUID,
            evidence_paths=linked_evidence,
            now=NOW,
        )


def test_source_authority_manifest_rejects_symlink_leaf(tmp_path: Path) -> None:
    evidence = write_evidence_set(tmp_path / "authority")
    authority = _source_authority(evidence)
    artifacts = dict(authority["artifacts"])
    tracked = Path(str(artifacts["tracked_manifest"]))
    tracked_link = tmp_path / "tracked-manifest-link.jsonl"
    tracked_link.symlink_to(tracked)
    artifacts["tracked_manifest"] = str(tracked_link.absolute())
    authority["artifacts"] = artifacts

    with pytest.raises(GateBundleError, match="symlink"):
        gate_module._source_identity_from_authority(authority)
