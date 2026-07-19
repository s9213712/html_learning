from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from scripts.testing import campaign_qualification_capture as capture_module
from scripts.testing import campaign_gate_bundle as gate_module
from scripts.testing import campaign_scenario_binding as scenario_binding
from scripts.testing import audit_evidence_triad
from scripts.testing.audit_evidence_triad import (
    INVARIANT_NAMES as AUDIT_EVIDENCE_INVARIANTS,
    SCHEMA_VERSION as AUDIT_EVIDENCE_SCHEMA_VERSION,
)
from scripts.testing.campaign_gate_bundle import (
    GATE_RAW_SPECS,
    RAW_ARTIFACT_BINDING_SCHEMA_VERSION,
    RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION,
)
from scripts.testing.campaign_qualification_capture import (
    QualificationCaptureError,
    QualificationCaptureWriter,
    QualificationContext,
    capture_gate_evidence as _capture_gate_evidence,
)
from scripts.testing.campaign_source_freeze import SOURCE_FREEZE_SCHEMA_VERSION
from services.server.database import get_audit_db
from services.system import audit as audit_service


COMMIT = "a" * 40
SOURCE_DIGEST = "b" * 64
PROTECTED_MANIFEST_DIGEST = "c" * 64
PROTECTED_CONTENT_DIGEST = "d" * 64
CAMPAIGN_UUID = "qualification-capture-0001"


@pytest.fixture(autouse=True)
def _restore_tmp_permissions(tmp_path: Path):
    """The writer intentionally seals attempts; make pytest cleanup possible."""

    yield
    for root, directories, files in os.walk(tmp_path, topdown=False):
        for name in files:
            try:
                os.chmod(Path(root) / name, 0o600)
            except FileNotFoundError:
                pass
        for name in directories:
            try:
                os.chmod(Path(root) / name, 0o700)
            except FileNotFoundError:
                pass
    os.chmod(tmp_path, 0o700)


def _write_private_json(path: Path, payload: object) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _write_private_bytes(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _source_payload() -> dict[str, object]:
    return {
        "schema_version": SOURCE_FREEZE_SCHEMA_VERSION,
        "label": "H0",
        "verified": True,
        "require_clean": True,
        "commit": COMMIT,
        "tracked_content_digest": SOURCE_DIGEST,
        "protected_ignored_manifest_digest": PROTECTED_MANIFEST_DIGEST,
        "protected_ignored_content_digest": PROTECTED_CONTENT_DIGEST,
    }


def _context(tmp_path: Path) -> tuple[QualificationContext, Path]:
    authority = _write_private_json(tmp_path / "source-h0.json", _source_payload())
    context = QualificationContext.create(
        qualification_campaign_uuid=CAMPAIGN_UUID,
        source_authority_path=authority.resolve(),
        invocation_id="capture-invocation-0001",
    )
    return context, authority


def _security_report(*, ok: bool = True) -> dict[str, object]:
    names = {
        "production_launcher_contract",
        "transport",
        "anonymous_root_denied",
        "login_missing_csrf_denied",
        "root_login",
        "manager_login",
        "user_login",
        "production_mode_active",
        "manager_root_boundary_denied",
        "user_root_boundary_denied",
        "authenticated_missing_csrf_denied",
        "dangerous_confirmation_required",
        "production_security_controls",
        "audit_log_chain",
        "cross_worker_session_consistency",
        "audit_evidence_triad_online",
    }
    denied_statuses = {
        "anonymous_root_denied": 403,
        "login_missing_csrf_denied": 403,
        "manager_root_boundary_denied": 403,
        "user_root_boundary_denied": 403,
        "authenticated_missing_csrf_denied": 403,
        "dangerous_confirmation_required": 400,
    }
    checks: list[dict[str, object]] = []
    for name in sorted(names):
        detail: dict[str, object] = {}
        if name == "production_launcher_contract":
            detail = {
                "security": "on",
                "server_mode": "production",
                "gunicorn_workers": 2,
                "isolated_runtime": True,
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
                "receipt_schema_version": AUDIT_EVIDENCE_SCHEMA_VERSION,
                "mode": "online",
                "target": "security_sentinel",
                "artifact_files_verified": True,
                "validation_classification": "PASS",
                "validation_errors": [],
            }
        checks.append(
            {
                "name": name,
                "ok": True,
                "status": denied_statuses.get(name, 200),
                "detail": detail,
            }
        )
    receipt = {
        "schema_version": AUDIT_EVIDENCE_SCHEMA_VERSION,
        "target": "security_sentinel",
        "mode": "online",
        "captured_at": "2026-07-13T07:57:00.000+00:00",
        "completed_at": "2026-07-13T07:57:01.000+00:00",
        "ok": True,
        "verdict": "PASS",
        "capture": {
            "mutation_lock_wait_ms": 0.0,
            "head_anchor": {"attempted": False, "performed": False},
            "sqlite_backup_api": True,
            "immutable_validation": True,
        },
        "artifacts": {
            "database": {
                "state": "present", "path": "audit_snapshot.sqlite3",
                "size": 4096, "sha256": "1" * 64,
            },
            "audit_log": {"state": "absent", "path": None, "size": 0, "sha256": None},
            "anchor_history": {"state": "absent", "path": None, "size": 0, "sha256": None},
            "anchor_latest": {"state": "absent", "path": None, "size": 0, "sha256": None},
        },
        "counts": {
            "db_rows": 0, "log_entries": 0,
            "anchor_history_entries": 0, "rows_after_latest": 0,
        },
        "heads": {"database": None, "audit_log": None, "anchor_latest": None},
        "invariants": {name: True for name in AUDIT_EVIDENCE_INVARIANTS},
        "errors": [],
        "secret_handling": {
            "integrity_key": "memory_only",
            "chain_seed": "memory_only",
            "secret_files_copied": False,
            "secret_values_in_receipt": False,
        },
    }
    receipt_bytes = (
        json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    for check in checks:
        if check["name"] == "audit_evidence_triad_online":
            check["detail"].update({
                "receipt_sha256": receipt_sha256,
                "receipt_size_bytes": len(receipt_bytes),
            })
    return {
        "schema_version": "hackme.production-security-sentinel.v1",
        "ok": ok,
        "classification": "PASS" if ok else "FAIL_PRODUCT",
        "failed_checks": [] if ok else ["transport"],
        "checks": checks,
        "audit_evidence": {
            "schema_version": "hackme.audit-evidence-triad-reference/v1",
            "receipt_schema_version": AUDIT_EVIDENCE_SCHEMA_VERSION,
            "mode": "online",
            "target": "security_sentinel",
            "receipt_path": "/tmp/security-sentinel/audit-evidence/receipt.json",
            "receipt_sha256": receipt_sha256,
            "receipt_size_bytes": len(receipt_bytes),
            "receipt": receipt,
            "validation": {
                "schema_version": "hackme.audit-evidence-triad-validation/v1",
                "ok": True,
                "classification": "PASS",
                "errors": [],
                "validated_invariants": sorted(AUDIT_EVIDENCE_INVARIANTS),
                "artifact_files_verified": True,
            },
        },
    }


def _security_native(tmp_path: Path, *, ok: bool = True) -> Path:
    runtime = tmp_path / "security-audit-runtime"
    for directory in (
        runtime / "database",
        runtime / "logs",
        runtime / "anchors",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    seed = "ac" * 24
    key = b"q" * 32
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
        "qualification_security_fixture",
        "127.0.0.1",
        user="root",
        success=True,
        ua="pytest",
        detail="online-triad",
    )
    triad_root = tmp_path / "security-audit-triad"
    receipt = audit_evidence_triad.capture_audit_evidence(
        paths=audit_evidence_triad.AuditEvidencePaths.for_runtime(runtime),
        output_dir=triad_root,
        target="security_sentinel",
        mode="online",
    )
    archive_path = tmp_path / "security-audit-triad.tar"
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
    receipt_bytes = (triad_root / "receipt.json").read_bytes()
    report = _security_report(ok=ok)
    reference = report["audit_evidence"]
    reference.update({
        "receipt_path": str((triad_root / "receipt.json").resolve()),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "receipt_size_bytes": len(receipt_bytes),
        "receipt": receipt,
        "archive_schema_version": audit_evidence_triad.ARCHIVE_SCHEMA_VERSION,
        "archive_path": str(archive_path.resolve()),
        "archive_sha256": archive["sha256"],
        "archive_size_bytes": archive["size"],
        "archive_validation": archive_validation,
    })
    for check in report["checks"]:
        if check["name"] == "audit_evidence_triad_online":
            check["detail"].update({
                "receipt_sha256": reference["receipt_sha256"],
                "receipt_size_bytes": reference["receipt_size_bytes"],
                "archive_schema_version": audit_evidence_triad.ARCHIVE_SCHEMA_VERSION,
                "archive_sha256": archive["sha256"],
                "archive_size_bytes": archive["size"],
                "archive_validation_classification": "PASS",
                "archive_validation_errors": [],
            })
    return _write_private_json(tmp_path / "security-native.json", report)


def _security_native_paths(tmp_path: Path, report: Path) -> dict[str, Path]:
    return {
        "security_sentinel": report.resolve(),
        "audit_evidence_archive": (tmp_path / "security-audit-triad.tar").resolve(),
    }


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_native_execution_receipt(
    *,
    attempt_root: Path,
    context: QualificationContext,
    gate_name: str,
    native_artifact_paths: dict[str, Path],
    duration_seconds: float = 1.0,
) -> Path:
    """Test-only controlled runner receipt; never accepted as loose evidence."""

    finished_ns = time.monotonic_ns()
    started_ns = finished_ns - int(duration_seconds * 1_000_000_000)
    finished = gate_module.utc_now()
    started = finished - gate_module.timedelta(seconds=duration_seconds)
    invocation_id = f"native:{gate_name}:pytest"
    producer = context.to_dict()["producer"]
    producer.update(kind=gate_module.NATIVE_PRODUCER_KIND, invocation_id=invocation_id)
    artifacts: dict[str, object] = {}
    for role, raw_path in native_artifact_paths.items():
        path = Path(raw_path)
        info = path.lstat()
        artifacts[role] = {
            "path": str(path),
            "file_identity": capture_module.FileIdentity.from_stat(info).to_dict(),
            "sha256": _sha256(path),
        }
    payload = {
        "schema_version": gate_module.NATIVE_EXECUTION_RECEIPT_SCHEMA_VERSION,
        "gate_name": gate_name,
        "qualification_campaign_uuid": context.qualification_campaign_uuid,
        "commit": context.commit,
        "source_digest": context.source_digest,
        "protected_source_digest": context.protected_source_digest,
        "invocation_id": invocation_id,
        "activation_nonce": f"activation:{gate_name}:pytest",
        "actual_execution": True,
        "simulated": False,
        "component_only": False,
        "started_at": gate_module.format_utc(started),
        "finished_at": gate_module.format_utc(finished),
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": finished_ns,
        "producer": producer,
        "source_authority_sha256": context.source_authority.sha256,
        "artifacts": artifacts,
    }
    receipt_path = attempt_root.parent / f".{attempt_root.name}.{gate_name}.native.json"
    return _write_private_json(receipt_path, payload)


def capture_gate_evidence(
    *,
    attempt_root: Path,
    context: QualificationContext,
    gate_name: str,
    native_artifact_paths: dict[str, Path],
    native_execution_receipt_path: Path | None = None,
):
    receipt = native_execution_receipt_path or _write_native_execution_receipt(
        attempt_root=attempt_root,
        context=context,
        gate_name=gate_name,
        native_artifact_paths=native_artifact_paths,
    )
    return _capture_gate_evidence(
        attempt_root=attempt_root,
        context=context,
        gate_name=gate_name,
        native_artifact_paths=native_artifact_paths,
        native_execution_receipt_path=receipt,
    )


def _assert_preserved_failure(attempt_root: Path) -> dict[str, object]:
    failure = _read_json(attempt_root / "attempt.failure.json")
    assert failure["status"] == "FAIL_HARNESS"
    assert failure["machine_verified"] is False
    assert stat.S_IMODE(attempt_root.stat().st_mode) == 0o500
    return failure


def test_public_capture_adds_exact_binding_and_returns_only_validated_pass(
    tmp_path: Path,
) -> None:
    context, _authority = _context(tmp_path)
    native = _security_native(tmp_path)
    attempt = (tmp_path / "attempt-success").resolve()
    projection = capture_module.project_bound_json_identity(
        context=context,
        gate_name="production_security_sentinel_verified",
        role="security_sentinel",
        native_path=native.resolve(),
    )

    result = capture_gate_evidence(
        attempt_root=attempt,
        context=context,
        gate_name="production_security_sentinel_verified",
        native_artifact_paths=_security_native_paths(tmp_path, native),
    )

    assert result["status"] == "PASS"
    assert result["machine_verified"] is True
    assert result["_derived"]["cross_worker_requests"] == 4
    assert Path(result["_evidence_path"]) == attempt / "evidence" / "production_security_sentinel_verified.json"
    assert (attempt / "attempt.failure.json").exists() is False

    evidence = _read_json(Path(result["_evidence_path"]))
    reference = evidence["raw_artifacts"]["security_sentinel"]
    assert set(reference) == {
        "schema_version",
        "artifact_id",
        "gate_name",
        "artifact_role",
        "path",
        "sha256",
        "size_bytes",
        "media_type",
        "content_schema_version",
        "qualification_campaign_uuid",
        "commit",
        "source_digest",
        "protected_source_digest",
    }
    assert reference["schema_version"] == RAW_ARTIFACT_REFERENCE_SCHEMA_VERSION
    captured = Path(reference["path"])
    captured_payload = _read_json(captured)
    binding = captured_payload.pop("formal_binding")
    assert captured_payload == _read_json(native)
    assert binding == context.formal_binding(
        gate_name="production_security_sentinel_verified",
        artifact_role="security_sentinel",
        captured_at=binding["captured_at"],
    )
    assert binding["schema_version"] == RAW_ARTIFACT_BINDING_SCHEMA_VERSION
    assert binding["actual_execution"] is True
    assert binding["simulated"] is False
    assert binding["component_only"] is False
    assert binding["producer"]["pid"] == os.getpid()
    assert binding["producer"]["start_ticks"] == context.producer.start_ticks
    assert captured.stat().st_ino != native.stat().st_ino
    assert captured.stat().st_nlink == 1
    assert reference["sha256"] == _sha256(captured)
    assert reference["size_bytes"] == captured.stat().st_size
    assert reference["sha256"] == projection["sha256"]
    assert reference["size_bytes"] == projection["size_bytes"]
    assert projection["native_sha256"] == _sha256(native)
    assert stat.S_IMODE(captured.stat().st_mode) == 0o400
    assert stat.S_IMODE(attempt.stat().st_mode) == 0o500


def test_public_api_has_no_caller_controlled_promotion_flags() -> None:
    public_callables = (
        QualificationContext.create,
        QualificationCaptureWriter.capture_gate,
        capture_gate_evidence,
        capture_module.project_bound_json_identity,
    )
    forbidden = {"actual_execution", "simulated", "component_only", "machine_verified"}
    for function in public_callables:
        assert forbidden.isdisjoint(inspect.signature(function).parameters)

    parser_dests = {action.dest for action in capture_module.build_parser()._actions}
    assert forbidden.isdisjoint(parser_dests)
    assert capture_module.MAX_JSON_BYTES == gate_module._MAX_RAW_JSON_BYTES
    assert capture_module.MAX_NDJSON_BYTES == gate_module._MAX_RAW_NDJSON_BYTES
    assert capture_module.MAX_NDJSON_LINE_BYTES == gate_module._MAX_RAW_NDJSON_LINE_BYTES
    assert capture_module.MAX_NDJSON_RECORDS == gate_module._MAX_RAW_NDJSON_ROWS
    assert capture_module.MAX_STRUCTURED_GATE_BYTES == gate_module._MAX_GATE_STRUCTURED_BYTES
    assert capture_module.MAX_RAW_DECODED_NODES == gate_module._MAX_RAW_DECODED_NODES
    assert (
        capture_module.MAX_RAW_DECODED_STRING_BYTES
        == gate_module._MAX_RAW_DECODED_STRING_BYTES
    )
    assert capture_module.MAX_GATE_DECODED_NODES == gate_module._MAX_GATE_DECODED_NODES
    assert (
        capture_module.MAX_GATE_DECODED_STRING_BYTES
        == gate_module._MAX_GATE_DECODED_STRING_BYTES
    )
    assert capture_module.MAX_STREAM_ARTIFACT_BYTES == gate_module._MAX_NATIVE_ARTIFACT_BYTES
    assert capture_module.STREAM_ROLE_MAX_BYTES == gate_module._NATIVE_ROLE_MAX_BYTES


def test_direct_cli_help_bootstraps_repo_imports(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(capture_module.__file__).resolve()), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--artifact ROLE=ABSOLUTE_PATH" in completed.stdout


def test_smoke_claim_cannot_replace_measured_native_duration(tmp_path: Path) -> None:
    context, _authority = _context(tmp_path)
    supervisor = _write_private_json(
        tmp_path / "supervisor.json",
        {
            "schema_version": "hackme.campaign-supervisor.v1",
            "level": "smoke",
            "ok": True,
            "classification": "PASS",
            "runner_returncode": 0,
            "runner_verdict": "PASS",
            "source_final": {"verified": True},
            "cleanup": {
                "source_monitor": {"ok": True},
                "watchdog": {"ok": True},
                "scope": {"ok": True},
            },
            "gates": {
                "cgroup_limits_verified": {"status": "PASS"},
                "external_watchdog_verified": {"status": "PASS"},
                "runner_and_watchdog_placement_verified": {"status": "PASS"},
            },
        },
    )
    runner = _write_private_json(
        tmp_path / "smoke.json",
        {
            "schema_version": "hackme.campaign-smoke-load.v2",
            "probe": "campaign_level0_lifecycle_load",
            "runtime_seconds": 180.5,
            "contract": {
                "configured_duration_seconds": 180,
                "configured_concurrency": 32,
            },
            "metrics": {
                "max_active_workers": 32,
                "operations_completed": 9000,
                "transport_errors": {},
            },
            "unexpected_errors": [],
            "silent_failures": [],
            "gates": {"duration": True, "load": True, "terminal": True},
            "classification": "PASS",
            "ok": True,
        },
    )

    attempt = (tmp_path / "attempt-smoke").resolve()
    with pytest.raises(QualificationCaptureError, match="measured duration boundary"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="180_second_smoke_passed",
            native_artifact_paths={
                "supervisor_result": supervisor.resolve(),
                "smoke_runner": runner.resolve(),
            },
        )
    failure = _assert_preserved_failure(attempt)
    assert "measured duration boundary" in failure["error"]["message"]


def test_binary_capture_streams_original_bytes_to_a_unique_inode(tmp_path: Path) -> None:
    context, _authority = _context(tmp_path)
    writer = QualificationCaptureWriter(
        context=context,
        attempt_root=(tmp_path / "attempt-binary").resolve(),
    )
    writer._prepare_attempt()
    native_content = (b"qualification-binary\x00\xff" * 140_000) + b"tail"
    native = _write_private_bytes(tmp_path / "native.bin", native_content)
    identity = capture_module._inspect_native(native.resolve(), label="binary native")
    destination = writer.raw_root / "captured.bin"

    captured_sha, captured_size, native_sha = writer._capture_stream(
        source=native.resolve(),
        identity=identity,
        destination=destination,
        gate_name="test_gate",
        role="binary",
    )

    assert captured_size == len(native_content)
    assert captured_sha == native_sha == hashlib.sha256(native_content).hexdigest()
    assert destination.stat().st_ino != native.stat().st_ino
    assert destination.stat().st_nlink == 1
    assert _sha256(destination) == _sha256(native)


def test_jsonl_capture_binds_every_record_without_changing_native_rows(tmp_path: Path) -> None:
    context, _authority = _context(tmp_path)
    writer = QualificationCaptureWriter(
        context=context,
        attempt_root=(tmp_path / "attempt-jsonl").resolve(),
    )
    writer._prepare_attempt()
    rows = [
        {"sample_schema_version": "hackme.resource-sample.v1", "sequence": 1},
        {"sample_schema_version": "hackme.resource-sample.v1", "sequence": 2},
    ]
    native = _write_private_bytes(
        tmp_path / "native.jsonl",
        b"".join(
            json.dumps(row, sort_keys=True).encode("utf-8") + b"\n"
            for row in rows
        ),
    )
    identity = capture_module._inspect_native(native.resolve(), label="JSONL native")
    destination = writer.raw_root / "captured.jsonl"
    binding = context.formal_binding(
        gate_name="sample_schema_completeness_verified",
        artifact_role="resource_samples",
        captured_at="2026-07-13T10:00:00Z",
    )

    writer._capture_jsonl(
        source=native.resolve(),
        identity=identity,
        destination=destination,
        gate_name="sample_schema_completeness_verified",
        role="resource_samples",
        spec=GATE_RAW_SPECS["sample_schema_completeness_verified"]["resource_samples"],
        binding=binding,
    )

    captured_rows = [json.loads(line) for line in destination.read_text().splitlines()]
    for expected, captured in zip(rows, captured_rows, strict=True):
        assert captured.pop("formal_binding") == binding
        assert captured == expected
    native_rows = [json.loads(line) for line in native.read_text().splitlines()]
    assert native_rows == rows


def test_jsonl_capture_enforces_decoded_budget_across_all_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    writer = QualificationCaptureWriter(
        context=context,
        attempt_root=(tmp_path / "attempt-jsonl-decoded").resolve(),
    )
    writer._prepare_attempt()
    rows = [
        {"sample_schema_version": "hackme.resource-sample.v1", "value": index}
        for index in range(2)
    ]
    native = _write_private_bytes(
        tmp_path / "decoded.jsonl",
        b"".join(json.dumps(row).encode("utf-8") + b"\n" for row in rows),
    )
    one_row_nodes, _strings = capture_module._validate_json_shape(
        rows[0],
        label="one native row",
    )
    monkeypatch.setattr(capture_module, "MAX_RAW_DECODED_NODES", one_row_nodes)
    identity = capture_module._inspect_native(native.resolve(), label="decoded JSONL")
    binding = context.formal_binding(
        gate_name="sample_schema_completeness_verified",
        artifact_role="resource_samples",
        captured_at="2026-07-13T10:00:00Z",
    )

    with pytest.raises(QualificationCaptureError, match="raw artifact limit"):
        writer._capture_jsonl(
            source=native.resolve(),
            identity=identity,
            destination=writer.raw_root / "decoded.jsonl",
            gate_name="sample_schema_completeness_verified",
            role="resource_samples",
            spec=GATE_RAW_SPECS["sample_schema_completeness_verified"]["resource_samples"],
            binding=binding,
        )


def test_capture_accounts_native_and_bound_decoded_budget_across_whole_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    writer = QualificationCaptureWriter(
        context=context,
        attempt_root=(tmp_path / "attempt-gate-decoded").resolve(),
    )
    writer._prepare_attempt()
    native = _security_native(tmp_path)
    payload = _read_json(native)
    binding = context.formal_binding(
        gate_name="production_security_sentinel_verified",
        artifact_role="security_sentinel",
        captured_at="2026-07-13T10:00:00Z",
    )
    native_nodes, _native_strings = capture_module._validate_json_shape(
        payload,
        label="native projection",
    )
    bound_nodes, _bound_strings = capture_module._validate_json_shape(
        {**payload, "formal_binding": binding},
        label="bound projection",
    )
    monkeypatch.setattr(
        capture_module,
        "MAX_GATE_DECODED_NODES",
        max(native_nodes, bound_nodes),
    )
    identity = capture_module._inspect_native(native.resolve(), label="decoded JSON")
    spec = GATE_RAW_SPECS["production_security_sentinel_verified"]["security_sentinel"]
    writer._capture_json(
        source=native.resolve(),
        identity=identity,
        destination=writer.raw_root / "first.json",
        gate_name="production_security_sentinel_verified",
        role="security_sentinel",
        spec=spec,
        binding=binding,
    )

    with pytest.raises(QualificationCaptureError, match="whole-gate decoded node"):
        writer._capture_json(
            source=native.resolve(),
            identity=identity,
            destination=writer.raw_root / "second.json",
            gate_name="production_security_sentinel_verified",
            role="security_sentinel_second",
            spec=spec,
            binding=context.formal_binding(
                gate_name="production_security_sentinel_verified",
                artifact_role="security_sentinel_second",
                captured_at="2026-07-13T10:00:01Z",
            ),
        )


def test_capture_enforces_decoded_string_budget_not_only_single_string_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    writer = QualificationCaptureWriter(
        context=context,
        attempt_root=(tmp_path / "attempt-string-decoded").resolve(),
    )
    writer._prepare_attempt()
    native = _security_native(tmp_path)
    payload = _read_json(native)
    _nodes, native_string_bytes = capture_module._validate_json_shape(
        payload,
        label="native string projection",
    )
    assert native_string_bytes > 1
    monkeypatch.setattr(
        capture_module,
        "MAX_RAW_DECODED_STRING_BYTES",
        native_string_bytes - 1,
    )

    with pytest.raises(QualificationCaptureError, match="decoded string budget"):
        writer._capture_json(
            source=native.resolve(),
            identity=capture_module._inspect_native(native.resolve(), label="string JSON"),
            destination=writer.raw_root / "strings.json",
            gate_name="production_security_sentinel_verified",
            role="security_sentinel",
            spec=GATE_RAW_SPECS["production_security_sentinel_verified"]["security_sentinel"],
            binding=context.formal_binding(
                gate_name="production_security_sentinel_verified",
                artifact_role="security_sentinel",
                captured_at="2026-07-13T10:00:00Z",
            ),
        )


def _native_stub_set(
    tmp_path: Path,
    gate_name: str,
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for index, (role, spec) in enumerate(GATE_RAW_SPECS[gate_name].items(), start=1):
        if spec.media_type == "application/json":
            path = _write_private_json(
                tmp_path / f"{role}.json",
                {"schema_version": spec.content_schema_version},
            )
        elif spec.media_type == "application/x-ndjson":
            path = _write_private_bytes(
                tmp_path / f"{role}.jsonl",
                json.dumps(
                    {"schema_version": spec.content_schema_version, "stub": index}
                ).encode("utf-8")
                + b"\n",
            )
        else:
            suffix = ".m3u8" if spec.media_type == "application/vnd.apple.mpegurl" else ".bin"
            path = _write_private_bytes(
                tmp_path / f"{role}{suffix}",
                f"native-{role}-{index}".encode("utf-8"),
            )
        result[role] = path.resolve()
    return result


def _rehearsal_path_contract_fixture(
    tmp_path: Path,
    *,
    context: QualificationContext,
    attempt: Path,
) -> tuple[dict[str, Path], dict[str, capture_module.FileIdentity], dict[str, Path]]:
    gate = "60_minute_rehearsal_passed"
    native_root = tmp_path / "rehearsal-native"
    native_root.mkdir()
    native = _native_stub_set(native_root, gate)
    plan = capture_module.planned_capture_paths(
        attempt,
        gate_name=gate,
        native_artifact_paths=native,
        qualification_campaign_uuid=context.qualification_campaign_uuid,
    )
    common = {
        "qualification_campaign_uuid": context.qualification_campaign_uuid,
        "campaign_uuid": "rehearsal-campaign-0001",
        "campaign_attempt_uuid": "rehearsal-attempt-0001",
        "native_invocation_id": f"native:{gate}:pytest",
        "commit": context.commit,
        "source_digest": context.source_digest,
        "protected_source_digest": context.protected_source_digest,
        "started_at": "2026-07-14T00:00:00Z",
        "finished_at": "2026-07-14T01:00:00Z",
        "started_monotonic_ns": 10_000_000_000,
        "finished_monotonic_ns": 3_610_000_000_000,
    }
    scenarios = tuple(
        role.removeprefix("scenario_")
        for role in GATE_RAW_SPECS[gate]
        if role.startswith("scenario_")
        and not role.startswith("scenario_bundle_")
        and not role.startswith("scenario_archive_")
    )
    scenario_index: dict[str, object] = {}
    for index, scenario_id in enumerate(scenarios, start=1):
        receipt_role = f"scenario_{scenario_id}"
        bundle_role = f"scenario_bundle_{scenario_id}"
        archive_role = f"scenario_archive_{scenario_id}"
        archive_payload = f"sealed-archive-{scenario_id}-{index}".encode("utf-8")
        _write_private_bytes(native[archive_role], archive_payload)
        archive_sha = _sha256(native[archive_role])
        authority = {
            **common,
            "scenario_attempt_uuid": f"scenario-attempt-{index:04d}-{scenario_id}",
            "native_invocation_id": f"scenario-invocation-{index:04d}-{scenario_id}",
            "started_at": "2026-07-14T00:00:01Z",
            "finished_at": "2026-07-14T00:00:02Z",
            "started_monotonic_ns": 11_000_000_000 + index,
            "finished_monotonic_ns": 12_000_000_000 + index,
        }
        bundle = {
            "schema_version": scenario_binding.NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION,
            "authority": authority,
            "artifact_archive": {
                "artifact_id": f"native.artifact.archive.{scenario_id}",
                "content_schema_version": scenario_binding.NATIVE_ARTIFACT_ARCHIVE_SCHEMA_VERSION,
                "path": str(plan[archive_role]),
                "sha256": archive_sha,
                "size_bytes": len(archive_payload),
                "media_type": "application/x-tar",
            },
        }
        _write_private_json(native[bundle_role], bundle)
        bundle_projection = capture_module.project_bound_json_identity(
            context=context,
            gate_name=gate,
            role=bundle_role,
            native_path=native[bundle_role],
        )
        receipt = {
            "schema_version": scenario_binding.RUNTIME_RECEIPT_SCHEMA_VERSION,
            "authority": authority,
            "artifact_bundle": {
                "path": str(plan[bundle_role]),
                "sha256": bundle_projection["sha256"],
                "size_bytes": bundle_projection["size_bytes"],
                "artifact_archive_sha256": archive_sha,
                "artifact_archive_size_bytes": len(archive_payload),
            },
        }
        _write_private_json(native[receipt_role], receipt)
        receipt_projection = capture_module.project_bound_json_identity(
            context=context,
            gate_name=gate,
            role=receipt_role,
            native_path=native[receipt_role],
        )
        scenario_index[scenario_id] = {
            "scenario_attempt_uuid": authority["scenario_attempt_uuid"],
            "native_invocation_id": authority["native_invocation_id"],
            "receipt": {
                "path": str(plan[receipt_role]),
                "sha256": receipt_projection["sha256"],
                "size_bytes": receipt_projection["size_bytes"],
            },
            "artifact_bundle": {
                "path": str(plan[bundle_role]),
                "sha256": bundle_projection["sha256"],
                "size_bytes": bundle_projection["size_bytes"],
            },
            "artifact_archive": {
                "path": str(plan[archive_role]),
                "sha256": archive_sha,
                "size_bytes": len(archive_payload),
            },
        }
    runner = {
        "schema_version": GATE_RAW_SPECS[gate]["runner_result"].content_schema_version,
        **common,
        "scenario_receipts": scenario_index,
    }
    _write_private_json(native["runner_result"], runner)
    runner_projection = capture_module.project_bound_json_identity(
        context=context,
        gate_name=gate,
        role="runner_result",
        native_path=native["runner_result"],
    )
    supervisor = {
        "schema_version": GATE_RAW_SPECS[gate]["supervisor_result"].content_schema_version,
        **common,
        "runner_report": {
            "path": str(plan["runner_result"]),
            "sha256": runner_projection["sha256"],
            "size_bytes": runner_projection["size_bytes"],
        },
    }
    _write_private_json(native["supervisor_result"], supervisor)
    identities = {
        role: capture_module._inspect_native(
            path,
            label=f"rehearsal fixture {role}",
        )
        for role, path in native.items()
    }
    return native, identities, plan


def test_rehearsal_path_contract_accepts_only_the_exact_bottom_up_chain(
    tmp_path: Path,
) -> None:
    context, _authority = _context(tmp_path)
    attempt = (tmp_path / "attempt-rehearsal-contract").resolve()
    native, identities, plan = _rehearsal_path_contract_fixture(
        tmp_path,
        context=context,
        attempt=attempt,
    )

    capture_module._validate_rehearsal_path_contract(
        context=context,
        sources=native,
        identities=identities,
        destinations=plan,
    )


def test_rehearsal_path_contract_rejects_cross_attempt_bundle_substitution(
    tmp_path: Path,
) -> None:
    context, _authority = _context(tmp_path)
    attempt = (tmp_path / "attempt-rehearsal-cross-attempt").resolve()
    native, identities, plan = _rehearsal_path_contract_fixture(
        tmp_path,
        context=context,
        attempt=attempt,
    )
    first, second = tuple(
        role.removeprefix("scenario_bundle_")
        for role in native
        if role.startswith("scenario_bundle_")
    )[:2]
    first_role = f"scenario_bundle_{first}"
    second_role = f"scenario_bundle_{second}"
    substituted_sources = dict(native)
    substituted_identities = dict(identities)
    substituted_sources[first_role] = native[second_role]
    substituted_identities[first_role] = identities[second_role]

    with pytest.raises(
        QualificationCaptureError,
        match="projected bound raw authority|authority mismatch",
    ):
        capture_module._validate_rehearsal_path_contract(
            context=context,
            sources=substituted_sources,
            identities=substituted_identities,
            destinations=plan,
        )


def test_rehearsal_path_contract_rejects_bundle_destination_alias(
    tmp_path: Path,
) -> None:
    context, _authority = _context(tmp_path)
    attempt = (tmp_path / "attempt-rehearsal-path-alias").resolve()
    native, identities, plan = _rehearsal_path_contract_fixture(
        tmp_path,
        context=context,
        attempt=attempt,
    )
    bundle_roles = [role for role in native if role.startswith("scenario_bundle_")]
    aliased_plan = dict(plan)
    aliased_plan[bundle_roles[0]] = plan[bundle_roles[1]]

    with pytest.raises(QualificationCaptureError, match="producer-bound to planned raw path"):
        capture_module._validate_rehearsal_path_contract(
            context=context,
            sources=native,
            identities=identities,
            destinations=aliased_plan,
        )


def test_rehearsal_archive_mutation_fails_before_any_raw_copy(
    tmp_path: Path,
) -> None:
    context, _authority = _context(tmp_path)
    attempt = (tmp_path / "attempt-rehearsal-archive-mutation").resolve()
    native, _identities, _plan = _rehearsal_path_contract_fixture(
        tmp_path,
        context=context,
        attempt=attempt,
    )
    archive_role = next(
        role for role in native if role.startswith("scenario_archive_")
    )
    native[archive_role].write_bytes(b"mutated-stale-archive-copy")
    native[archive_role].chmod(0o600)

    with pytest.raises(QualificationCaptureError, match="archive hash/size mismatch"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="60_minute_rehearsal_passed",
            native_artifact_paths=native,
        )

    _assert_preserved_failure(attempt)
    assert list((attempt / "raw").iterdir()) == []


def test_rehearsal_archive_symlink_is_rejected_before_capture(
    tmp_path: Path,
) -> None:
    context, _authority = _context(tmp_path)
    attempt = (tmp_path / "attempt-rehearsal-archive-symlink").resolve()
    native, _identities, _plan = _rehearsal_path_contract_fixture(
        tmp_path,
        context=context,
        attempt=attempt,
    )
    archive_roles = [role for role in native if role.startswith("scenario_archive_")]
    first_path = native[archive_roles[0]]
    target_path = native[archive_roles[1]]
    first_path.unlink()
    first_path.symlink_to(target_path)

    with pytest.raises(QualificationCaptureError, match="symlink"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="60_minute_rehearsal_passed",
            native_artifact_paths=native,
        )

    _assert_preserved_failure(attempt)
    assert list((attempt / "raw").iterdir()) == []


def test_worktree_native_paths_must_be_producer_bound_before_copy(tmp_path: Path) -> None:
    context, _authority = _context(tmp_path)
    gate = "worktree_clean_and_frozen"
    native = _native_stub_set(tmp_path, gate)
    source_payload = {
        "schema_version": "hackme.source-freeze.v3",
        "artifacts": {
            role: str(native[role])
            for role in (
                "git_status",
                "git_diff_binary",
                "git_ls_files",
                "git_submodule_status",
                "tracked_manifest",
                "protected_ignored_manifest",
            )
        },
    }
    _write_private_json(native["source_h0"], source_payload)
    attempt = (tmp_path / "attempt-worktree-paths").resolve()
    plan = capture_module.planned_capture_paths(
        attempt,
        gate_name=gate,
        native_artifact_paths=native,
        qualification_campaign_uuid=CAMPAIGN_UUID,
    )
    assert plan["git_status"] != native["git_status"]

    with pytest.raises(QualificationCaptureError, match="producer-bound to planned raw path"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name=gate,
            native_artifact_paths=native,
        )

    _assert_preserved_failure(attempt)
    assert list((attempt / "raw").iterdir()) == []
    assert _read_json(native["source_h0"]) == source_payload


def test_dependency_native_paths_must_be_producer_bound_before_copy(tmp_path: Path) -> None:
    context, _authority = _context(tmp_path)
    gate = "all_mandatory_dependencies_verified"
    native = _native_stub_set(tmp_path, gate)
    hls_ffprobe = {
        "schema_version": GATE_RAW_SPECS[gate]["hls_ffprobe"].content_schema_version,
        "input_path": str(native["hls_playlist"]),
    }
    _write_private_json(native["hls_ffprobe"], hls_ffprobe)
    attempt = (tmp_path / "attempt-dependency-paths").resolve()

    with pytest.raises(QualificationCaptureError, match="hls_ffprobe.input_path.*producer-bound"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name=gate,
            native_artifact_paths=native,
        )

    failure = _assert_preserved_failure(attempt)
    assert "planned raw path" in failure["error"]["message"]
    assert list((attempt / "raw").iterdir()) == []
    assert _read_json(native["hls_ffprobe"]) == hls_ffprobe


def _set_nested(payload: dict[str, object], keys: tuple[str, ...], value: object) -> None:
    current = payload
    for key in keys[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[keys[-1]] = value


def test_dependency_planned_paths_and_projected_browser_hashes_reach_semantics(
    tmp_path: Path,
) -> None:
    context, _authority = _context(tmp_path)
    gate = "all_mandatory_dependencies_verified"
    native = _native_stub_set(tmp_path, gate)
    attempt = (tmp_path / "attempt-dependency-planned").resolve()
    plan = capture_module.planned_capture_paths(
        attempt,
        gate_name=gate,
        native_artifact_paths=native,
        qualification_campaign_uuid=CAMPAIGN_UUID,
    )

    direct = (
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
        ("backup_restore_manifest", ("restored_database_path",), "backup_restored_database"),
        ("backup_sqlite_check", ("database_path",), "backup_restored_database"),
        ("security_receipt", ("evidence", "request_trace_path"), "security_requests"),
        ("security_receipt", ("evidence", "audit_chain_path"), "security_audit_chain"),
    )
    payloads: dict[str, dict[str, object]] = {}
    for role, keys, target_role in direct:
        payload = payloads.setdefault(role, _read_json(native[role]))
        _set_nested(payload, keys, str(plan[target_role]))
    for role, payload in payloads.items():
        _write_private_json(native[role], payload)

    _write_private_bytes(
        native["hls_playlist"],
        (
            "#EXTM3U\n#EXTINF:1.0,\n"
            f"{plan['hls_segment'].name}\n"
            "#EXT-X-ENDLIST\n"
        ).encode("utf-8"),
    )
    checks: list[dict[str, object]] = []
    for engine in ("chromium", "firefox", "webkit"):
        role = f"browser_{engine}_launch"
        projection = capture_module.project_bound_json_identity(
            context=context,
            gate_name=gate,
            role=role,
            native_path=native[role],
        )
        checks.append(
            {
                "name": f"browser_{engine}",
                "details": {
                    "evidence": {
                        "raw_authority_path": str(plan[role]),
                        "raw_authority_sha256": projection["sha256"],
                    }
                },
            }
        )
    checks.append(
        {
            "name": "ffmpeg_hls",
            "details": {
                "evidence": {
                    "playlist": str(plan["hls_playlist"]),
                    "segment_path": str(plan["hls_segment"]),
                    "ffprobe_path": str(plan["hls_ffprobe"]),
                }
            },
        }
    )
    external_paths = {
        "bt_seed_download": {
            "download_path": "bt_payload",
            "trace_path": "bt_protocol_trace",
        },
        "comfyui_terminal": {
            "output_path": "comfyui_output",
            "history_path": "comfyui_history",
        },
        "ai_provider_terminal": {"exchange_path": "ai_provider_exchange"},
        "backup_restore": {
            "archive_path": "backup_archive",
            "manifest_path": "backup_restore_manifest",
            "quick_check_path": "backup_sqlite_check",
        },
        "production_security_sentinel": {
            "request_trace_path": "security_requests",
            "audit_chain_path": "security_audit_chain",
        },
    }
    for check_name, fields in external_paths.items():
        checks.append(
            {
                "name": check_name,
                "details": {
                    "evidence": {
                        "evidence": {
                            key: str(plan[role])
                            for key, role in fields.items()
                        }
                    }
                },
            }
        )
    _write_private_json(
        native["dependency_preflight"],
        {
            "schema_version": "hackme.campaign.dependency-preflight/v1",
            "status": "FAIL",
            "ok": False,
            "failed_checks": ["intentional_semantic_failure"],
            "checks": checks,
        },
    )

    with pytest.raises(QualificationCaptureError, match="semantic authority did not derive PASS"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name=gate,
            native_artifact_paths=native,
        )

    failure = _assert_preserved_failure(attempt)
    assert "dependency preflight did not PASS" in failure["error"]["message"]
    assert len(list((attempt / "raw").iterdir())) == len(GATE_RAW_SPECS[gate])


def test_checkpoint_mirror_is_copied_to_unique_persistent_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    persistent = tmp_path / "persistent-checkpoints"
    persistent.mkdir(mode=0o700)
    persistent.chmod(0o700)
    monkeypatch.setattr(capture_module, "_PERSISTENT_CHECKPOINT_ROOT", persistent.resolve())
    monkeypatch.setattr(gate_module, "_PERSISTENT_CHECKPOINT_ROOT", persistent.resolve())
    campaign = "native-checkpoint-campaign"
    before_payload = {
        "schema_version": "hackme.campaign-checkpoint.v1",
        "campaign_uuid": campaign,
        "revision": 1,
        "phase": "before",
    }
    recovered_payload = {
        "schema_version": "hackme.campaign-checkpoint.v1",
        "campaign_uuid": campaign,
        "revision": 2,
        "phase": "recovered",
    }
    tamper_payload = {
        "schema_version": "hackme.campaign-checkpoint-tamper-trial/v1",
        "campaign_uuid": campaign,
        "candidate_accepted": False,
        "classification": "FAIL_HARNESS",
        "rejection_reason": "hash_mismatch",
        "formal_time_resumed": False,
        "revalidated": {
            "pid_identity": True,
            "cgroup_identity": True,
            "source_identity": True,
        },
    }
    before = _write_private_json(tmp_path / "checkpoint-before.json", before_payload)
    primary = _write_private_json(tmp_path / "checkpoint-primary.json", recovered_payload)
    mirror = _write_private_json(tmp_path / "checkpoint-mirror-native.json", recovered_payload)
    tamper = _write_private_json(tmp_path / "checkpoint-tamper.json", tamper_payload)

    result = capture_gate_evidence(
        attempt_root=(tmp_path / "attempt-checkpoint").resolve(),
        context=context,
        gate_name="checkpoint_recovery_verified",
        native_artifact_paths={
            "checkpoint_before": before.resolve(),
            "checkpoint_primary": primary.resolve(),
            "checkpoint_mirror": mirror.resolve(),
            "tamper_rejection": tamper.resolve(),
        },
    )

    raw_paths = [Path(path) for path in result["_raw_paths"]]
    captured_mirror = next(path for path in raw_paths if path.name == "checkpoint_mirror.json")
    assert captured_mirror.is_relative_to(persistent)
    assert captured_mirror.stat().st_ino != mirror.stat().st_ino
    assert captured_mirror.stat().st_nlink == 1
    assert stat.S_IMODE(captured_mirror.stat().st_mode) == 0o600
    assert result["_derived"]["revision"] == 2


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "world_write"])
def test_public_capture_rejects_unsafe_native_files_and_preserves_attempt(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    context, _authority = _context(tmp_path)
    safe_native = _security_native(tmp_path)
    supplied = safe_native
    if unsafe_kind == "symlink":
        supplied = tmp_path / "security-link.json"
        supplied.symlink_to(safe_native)
    elif unsafe_kind == "hardlink":
        supplied = tmp_path / "security-hardlink.json"
        os.link(safe_native, supplied)
    else:
        safe_native.chmod(0o666)
    attempt = (tmp_path / f"attempt-{unsafe_kind}").resolve()

    with pytest.raises(QualificationCaptureError, match="symlink|hard-linked|writable") as caught:
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="production_security_sentinel_verified",
            native_artifact_paths={
                **_security_native_paths(tmp_path, safe_native),
                "security_sentinel": supplied.absolute(),
            },
        )

    assert caught.value.attempt_root == attempt
    _assert_preserved_failure(attempt)
    assert (attempt / "evidence" / "production_security_sentinel_verified.json").exists() is False


def test_preexisting_attempt_root_is_rejected_without_touching_it(tmp_path: Path) -> None:
    context, _authority = _context(tmp_path)
    native = _security_native(tmp_path)
    attempt = (tmp_path / "attempt-preexisting").resolve()
    attempt.mkdir()
    marker = _write_private_bytes(attempt / "owner.marker", b"do-not-touch")
    before = (marker.stat().st_ino, marker.stat().st_mtime_ns, marker.read_bytes())

    with pytest.raises(QualificationCaptureError, match="already exists"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="production_security_sentinel_verified",
            native_artifact_paths=_security_native_paths(tmp_path, native),
        )

    assert (marker.stat().st_ino, marker.stat().st_mtime_ns, marker.read_bytes()) == before
    assert sorted(path.name for path in attempt.iterdir()) == ["owner.marker"]


def test_native_toctou_change_invalidates_and_preserves_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    native = _security_native(tmp_path)
    attempt = (tmp_path / "attempt-toctou").resolve()
    writer = QualificationCaptureWriter(context=context, attempt_root=attempt)
    original = writer._capture_json

    def capture_then_mutate(**kwargs):
        result = original(**kwargs)
        changed = _read_json(native)
        changed["post_capture_mutation"] = True
        _write_private_json(native, changed)
        return result

    monkeypatch.setattr(writer, "_capture_json", capture_then_mutate)
    native_paths = _security_native_paths(tmp_path, native)
    receipt = _write_native_execution_receipt(
        attempt_root=attempt,
        context=context,
        gate_name="production_security_sentinel_verified",
        native_artifact_paths=native_paths,
    )

    with pytest.raises(QualificationCaptureError, match="changed during gate capture"):
        writer.capture_gate(
            gate_name="production_security_sentinel_verified",
            native_artifact_paths=native_paths,
            native_execution_receipt_path=receipt,
        )

    failure = _assert_preserved_failure(attempt)
    assert failure["captured_roles"] == [
        "security_sentinel",
        "audit_evidence_archive",
    ]
    assert (attempt / "evidence" / "production_security_sentinel_verified.json").exists() is False


def test_oversized_json_is_rejected_before_binding_and_attempt_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    source_native = _security_native(tmp_path)
    native_payload = _read_json(source_native)
    native_payload["padding"] = "x" * 4096
    native = _write_private_json(tmp_path / "security-native.json", native_payload)
    attempt = (tmp_path / "attempt-oversized-json").resolve()
    monkeypatch.setattr(capture_module, "MAX_JSON_BYTES", 1024)

    with pytest.raises(QualificationCaptureError, match="bounded JSON limit"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="production_security_sentinel_verified",
            native_artifact_paths=_security_native_paths(tmp_path, native),
        )

    _assert_preserved_failure(attempt)


def test_overdeep_native_json_is_rejected_before_snapshot(tmp_path: Path) -> None:
    context, _authority = _context(tmp_path)
    source_native = _security_native(tmp_path)
    payload = _read_json(source_native)
    nested: dict[str, object] = {}
    payload["nested"] = nested
    for _index in range(capture_module.MAX_JSON_DEPTH + 2):
        child: dict[str, object] = {}
        nested["child"] = child
        nested = child
    native = _write_private_json(tmp_path / "security-overdeep.json", payload)
    attempt = (tmp_path / "attempt-overdeep-json").resolve()

    with pytest.raises(QualificationCaptureError, match="JSON nesting exceeds"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="production_security_sentinel_verified",
            native_artifact_paths=_security_native_paths(tmp_path, native),
        )

    _assert_preserved_failure(attempt)


def test_unbounded_jsonl_line_is_rejected_and_attempt_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    resource = _write_private_bytes(
        tmp_path / "resource.jsonl",
        json.dumps(
            {
                "sample_schema_version": "hackme.resource-sample.v1",
                "padding": "x" * 512,
            }
        ).encode("utf-8") + b"\n",
    )
    negative = _write_private_json(
        tmp_path / "negative.json",
        {
            "schema_version": "hackme.resource-negative-trials/v1",
            "trials": [],
        },
    )
    attempt = (tmp_path / "attempt-jsonl-line").resolve()
    monkeypatch.setattr(capture_module, "MAX_NDJSON_LINE_BYTES", 128)

    with pytest.raises(QualificationCaptureError, match="JSONL line exceeds"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="sample_schema_completeness_verified",
            native_artifact_paths={
                "resource_samples": resource.resolve(),
                "negative_collector_trials": negative.resolve(),
            },
        )

    _assert_preserved_failure(attempt)


def test_formal_binding_cannot_expand_jsonl_past_the_line_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    native_line = json.dumps(
        {"sample_schema_version": "hackme.resource-sample.v1"}
    ).encode("utf-8") + b"\n"
    assert len(native_line) < 128
    resource = _write_private_bytes(tmp_path / "resource-small.jsonl", native_line)
    negative = _write_private_json(
        tmp_path / "negative-small.json",
        {
            "schema_version": "hackme.resource-negative-trials/v1",
            "trials": [],
        },
    )
    attempt = (tmp_path / "attempt-jsonl-bound-line").resolve()
    monkeypatch.setattr(capture_module, "MAX_NDJSON_LINE_BYTES", 128)

    with pytest.raises(QualificationCaptureError, match="bound JSONL line exceeds"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="sample_schema_completeness_verified",
            native_artifact_paths={
                "resource_samples": resource.resolve(),
                "negative_collector_trials": negative.resolve(),
            },
        )

    _assert_preserved_failure(attempt)


def test_bound_json_cannot_exceed_structured_gate_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    native = _security_native(tmp_path)
    attempt = (tmp_path / "attempt-structured-aggregate").resolve()
    native_size = native.stat().st_size
    monkeypatch.setattr(
        capture_module,
        "MAX_STRUCTURED_GATE_BYTES",
        native_size + 16,
    )

    with pytest.raises(QualificationCaptureError, match="structured captured output"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="production_security_sentinel_verified",
            native_artifact_paths=_security_native_paths(tmp_path, native),
        )

    _assert_preserved_failure(attempt)


def test_prebound_json_is_rejected_instead_of_repromoted(tmp_path: Path) -> None:
    context, _authority = _context(tmp_path)
    source_native = _security_native(tmp_path)
    payload = _read_json(source_native)
    payload["formal_binding"] = {
        "actual_execution": True,
        "simulated": False,
        "component_only": False,
    }
    native = _write_private_json(tmp_path / "prebound.json", payload)
    attempt = (tmp_path / "attempt-prebound").resolve()

    with pytest.raises(QualificationCaptureError, match="already formally bound"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="production_security_sentinel_verified",
            native_artifact_paths=_security_native_paths(tmp_path, native),
        )

    _assert_preserved_failure(attempt)


def test_semantic_validator_failure_never_returns_or_names_final_pass(
    tmp_path: Path,
) -> None:
    context, _authority = _context(tmp_path)
    native = _security_native(tmp_path, ok=False)
    attempt = (tmp_path / "attempt-semantic-fail").resolve()

    with pytest.raises(
        QualificationCaptureError,
        match="semantic authority did not derive PASS",
    ):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="production_security_sentinel_verified",
            native_artifact_paths=_security_native_paths(tmp_path, native),
        )

    failure = _assert_preserved_failure(attempt)
    assert "did not PASS" in failure["error"]["message"]
    assert (attempt / "evidence" / "production_security_sentinel_verified.json").exists() is False
    assert (attempt / "evidence" / ".production_security_sentinel_verified.candidate.json").exists()


def test_second_public_validation_failure_quarantines_final_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    native = _security_native(tmp_path)
    attempt = (tmp_path / "attempt-second-validation-fail").resolve()
    original = capture_module._validate_unsealed_gate_evidence
    calls = 0

    def fail_second_validation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise gate_module.GateBundleError("injected second validation failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(capture_module, "_validate_unsealed_gate_evidence", fail_second_validation)

    with pytest.raises(QualificationCaptureError, match="semantic authority did not derive PASS"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="production_security_sentinel_verified",
            native_artifact_paths=_security_native_paths(tmp_path, native),
        )

    assert calls == 2
    _assert_preserved_failure(attempt)
    assert (attempt / "evidence" / "production_security_sentinel_verified.json").exists() is False
    assert (attempt / "evidence" / ".production_security_sentinel_verified.invalid.json").exists()


def test_changed_live_producer_identity_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _authority = _context(tmp_path)
    native = _security_native(tmp_path)
    attempt = (tmp_path / "attempt-producer-change").resolve()
    changed = replace(context.producer, start_ticks=context.producer.start_ticks + 1)
    monkeypatch.setattr(capture_module, "capture_process_identity", lambda _pid: changed)

    with pytest.raises(QualificationCaptureError, match="producer identity changed"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name="production_security_sentinel_verified",
            native_artifact_paths=_security_native_paths(tmp_path, native),
        )

    _assert_preserved_failure(attempt)


def test_source_authority_leaf_symlink_is_rejected(tmp_path: Path) -> None:
    authority = _write_private_json(tmp_path / "source-real.json", _source_payload())
    authority_link = tmp_path / "source-link.json"
    authority_link.symlink_to(authority)

    with pytest.raises(QualificationCaptureError, match="symlink"):
        QualificationContext.create(
            qualification_campaign_uuid=CAMPAIGN_UUID,
            source_authority_path=authority_link.absolute(),
            invocation_id="capture-source-link-0001",
        )


def test_planning_rejects_native_dotdot_alias_and_keeps_role_caps_aligned(
    tmp_path: Path,
) -> None:
    native = _security_native(tmp_path)
    alias_directory = tmp_path / "native-alias-directory"
    alias_directory.mkdir()
    native_alias = str(alias_directory / ".." / native.name)

    with pytest.raises(QualificationCaptureError, match="exact canonical"):
        capture_module.planned_capture_paths(
            (tmp_path / "attempt-alias-plan").resolve(),
            gate_name="production_security_sentinel_verified",
            native_artifact_paths={
                **_security_native_paths(tmp_path, native),
                "security_sentinel": Path(native_alias),
            },
            qualification_campaign_uuid=CAMPAIGN_UUID,
        )

    key = ("all_mandatory_dependencies_verified", "hls_segment")
    assert capture_module.STREAM_ROLE_MAX_BYTES[key] == gate_module._NATIVE_ROLE_MAX_BYTES[key]
    assert capture_module.STREAM_ROLE_MAX_BYTES[key] == 512 * 1024 * 1024


def test_writer_rejects_dotdot_alias_in_declared_planned_path(tmp_path: Path) -> None:
    context, _authority = _context(tmp_path)
    gate = "all_mandatory_dependencies_verified"
    native = _native_stub_set(tmp_path, gate)
    attempt = (tmp_path / "attempt-declared-alias").resolve()
    plan = capture_module.planned_capture_paths(
        attempt,
        gate_name=gate,
        native_artifact_paths=native,
        qualification_campaign_uuid=CAMPAIGN_UUID,
    )
    playlist_path = plan["hls_playlist"]
    alias = str(
        playlist_path.parent
        / "declared-alias-directory"
        / ".."
        / playlist_path.name
    )
    _write_private_json(
        native["hls_ffprobe"],
        {
            "schema_version": GATE_RAW_SPECS[gate]["hls_ffprobe"].content_schema_version,
            "input_path": alias,
        },
    )

    with pytest.raises(QualificationCaptureError, match="exact canonical"):
        capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name=gate,
            native_artifact_paths=native,
        )

    _assert_preserved_failure(attempt)
    assert list((attempt / "raw").iterdir()) == []
