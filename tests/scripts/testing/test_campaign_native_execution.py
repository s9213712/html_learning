from __future__ import annotations

import json
import hashlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone

import pytest

from scripts.testing import campaign_native_execution as native_module
from scripts.testing.campaign_native_execution import (
    NATIVE_EXECUTION_RESULT_SCHEMA_VERSION,
    NativeExecutionError,
    execute_and_capture_gate,
)
from scripts.testing.campaign_source_freeze import SOURCE_FREEZE_SCHEMA_VERSION
from scripts.testing.campaign_source_freeze import GitSourceFreezer
from scripts.testing.campaign_qualification_capture import QualificationContext
from scripts.testing.campaign_qualification_capture import (
    build_rehearsal_projection_context,
    read_sealed_rehearsal_projection_context,
)
from scripts.testing.audit_evidence_triad import (
    INVARIANT_NAMES as AUDIT_EVIDENCE_INVARIANTS,
    SCHEMA_VERSION as AUDIT_EVIDENCE_SCHEMA_VERSION,
)
from scripts.testing import audit_evidence_triad
from services.server.database import get_audit_db
from services.system import audit as audit_service


COMMIT = "a" * 40
SOURCE_DIGEST = "b" * 64
PROTECTED_MANIFEST_DIGEST = "c" * 64
PROTECTED_CONTENT_DIGEST = "d" * 64
CAMPAIGN_UUID = "native-execution-campaign-0001"
REAL_SOURCE_GUARD = native_module._LiveSourceGuard
REAL_COMMAND_VALIDATOR = native_module._validate_reviewed_command


class _NoopSourceGuard:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def verify(self) -> dict[str, object]:
        return {"verified": True, "incident": False}

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _restore_tmp_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Most tests intentionally use a minimal synthetic H0 authority and mock
    # the expensive live Git verifier.  Dedicated tests below exercise the
    # real inotify-backed source guard.
    monkeypatch.setattr(native_module, "_LiveSourceGuard", _NoopSourceGuard)
    monkeypatch.setattr(
        native_module,
        "_validate_reviewed_command",
        lambda **_kwargs: {"unit_test_only": True},
    )
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
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return path


def _source_authority(tmp_path: Path) -> Path:
    return _write_private_json(tmp_path / "source-h0.json", {
        "schema_version": SOURCE_FREEZE_SCHEMA_VERSION,
        "label": "H0",
        "verified": True,
        "require_clean": True,
        "commit": COMMIT,
        "tracked_content_digest": SOURCE_DIGEST,
        "protected_ignored_manifest_digest": PROTECTED_MANIFEST_DIGEST,
        "protected_ignored_content_digest": PROTECTED_CONTENT_DIGEST,
    })


def _source_proof() -> dict[str, object]:
    return {
        "repo_root": "/controlled/repo",
        "commit": COMMIT,
        "source_digest": SOURCE_DIGEST,
        "protected_source_digest": "controlled-protected-digest",
        "git_status_empty": True,
        "git_diff_binary_empty": True,
        "submodules_clean": True,
        "source_authority_sha256": "e" * 64,
    }


def _security_report(*, include_fallback: bool = False) -> dict[str, object]:
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
            detail = {"statuses": [200, 200, 200, 200]}
        elif name == "audit_evidence_triad_online":
            detail = {
                "receipt_schema_version": AUDIT_EVIDENCE_SCHEMA_VERSION,
                "mode": "online",
                "target": "security_sentinel",
                "artifact_files_verified": True,
                "validation_classification": "PASS",
                "validation_errors": [],
            }
        checks.append({
            "name": name,
            "ok": True,
            "status": denied_statuses.get(name, 200),
            "detail": detail,
        })
    receipt: dict[str, object] = {
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
    report: dict[str, object] = {
        "schema_version": "hackme.production-security-sentinel.v1",
        "ok": True,
        "classification": "PASS",
        "failed_checks": [],
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
    if include_fallback:
        report["fallback"] = "security sentinel unavailable"
    return report


def _security_bundle_payload(
    tmp_path: Path,
    payload: object,
) -> tuple[object, bytes]:
    runtime = tmp_path / "security-fixture-runtime"
    for directory in (
        runtime / "database",
        runtime / "logs",
        runtime / "anchors",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    seed = "ad" * 24
    key = b"n" * 32
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
        "native_security_fixture",
        "127.0.0.1",
        user="root",
        success=True,
        ua="pytest",
        detail="online-triad",
    )
    triad_root = tmp_path / "security-fixture-triad"
    receipt = audit_evidence_triad.capture_audit_evidence(
        paths=audit_evidence_triad.AuditEvidencePaths.for_runtime(runtime),
        output_dir=triad_root,
        target="security_sentinel",
        mode="online",
    )
    staging_archive = tmp_path / "security-fixture-triad.tar"
    archive = audit_evidence_triad.create_audit_evidence_archive(
        output_dir=triad_root,
        archive_path=staging_archive,
    )
    archive_validation = audit_evidence_triad.validate_audit_evidence_archive(
        staging_archive,
        required_mode="online",
        required_target="security_sentinel",
        expected_sha256=str(archive["sha256"]),
        expected_size=int(archive["size"]),
    )
    assert receipt["ok"] is True and archive_validation["ok"] is True
    enriched = json.loads(json.dumps(payload))
    if isinstance(enriched, dict) and isinstance(enriched.get("audit_evidence"), dict):
        receipt_bytes = (triad_root / "receipt.json").read_bytes()
        reference = enriched["audit_evidence"]
        reference.update({
            "receipt_path": str((triad_root / "receipt.json").resolve()),
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "receipt_size_bytes": len(receipt_bytes),
            "receipt": receipt,
            "archive_schema_version": audit_evidence_triad.ARCHIVE_SCHEMA_VERSION,
            "archive_path": str(staging_archive.resolve()),
            "archive_sha256": archive["sha256"],
            "archive_size_bytes": archive["size"],
            "archive_validation": archive_validation,
        })
        for check in enriched.get("checks") or []:
            if isinstance(check, dict) and check.get("name") == "audit_evidence_triad_online":
                check.setdefault("detail", {}).update({
                    "receipt_sha256": reference["receipt_sha256"],
                    "receipt_size_bytes": reference["receipt_size_bytes"],
                    "archive_schema_version": audit_evidence_triad.ARCHIVE_SCHEMA_VERSION,
                    "archive_sha256": archive["sha256"],
                    "archive_size_bytes": archive["size"],
                    "archive_validation_classification": "PASS",
                    "archive_validation_errors": [],
                })
    return enriched, staging_archive.read_bytes()


def _artifact_writer_command(
    path: Path,
    payload: object,
    *,
    returncode: int = 0,
    archive_path: Path | None = None,
    archive_bytes: bytes = b"",
) -> list[str]:
    code = (
        "import json,pathlib,sys; "
        f"p=pathlib.Path({str(path)!r}); "
        f"p.write_text({json.dumps(json.dumps(payload, sort_keys=True))}, encoding='utf-8'); "
        "p.chmod(0o600); "
        + (
            f"a=pathlib.Path({str(archive_path)!r}); "
            f"a.write_bytes(bytes.fromhex({archive_bytes.hex()!r})); a.chmod(0o600); "
            if archive_path is not None
            else ""
        )
        + f"sys.exit({int(returncode)})"
    )
    return [sys.executable, "-c", code]


def _run_security_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> dict[str, object]:
    monkeypatch.setattr(
        native_module,
        "_verify_live_source",
        lambda _context, *, scratch_root: _source_proof(),
    )
    artifact = (tmp_path / "security-native.json").resolve()
    archive_artifact = (tmp_path / "security-native-audit.tar").resolve()
    bound_payload, archive_bytes = _security_bundle_payload(tmp_path, payload)
    return execute_and_capture_gate(
        attempt_root=(tmp_path / "attempt-security").resolve(),
        gate_name="production_security_sentinel_verified",
        source_authority_path=_source_authority(tmp_path).resolve(),
        qualification_campaign_uuid=CAMPAIGN_UUID,
        native_artifact_paths={
            "security_sentinel": artifact,
            "audit_evidence_archive": archive_artifact,
        },
        command=_artifact_writer_command(
            artifact,
            bound_payload,
            archive_path=archive_artifact,
            archive_bytes=archive_bytes,
        ),
        cwd=tmp_path.resolve(),
        timeout_seconds=30,
    )


def test_native_producer_runs_command_and_captures_independent_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_security_attempt(tmp_path, monkeypatch, _security_report())

    assert result["schema_version"] == NATIVE_EXECUTION_RESULT_SCHEMA_VERSION
    assert result["status"] == "PASS"
    assert result["machine_verified"] is True
    receipt_path = Path(str(result["native_execution_receipt"]))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["actual_execution"] is True
    assert receipt["simulated"] is False
    assert receipt["component_only"] is False
    assert receipt["producer"]["pid"] == os.getpid()
    assert set(receipt["artifacts"]) == {
        "security_sentinel",
        "audit_evidence_archive",
    }
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400
    assert Path(str(result["attempt_manifest"])).is_file()


@pytest.mark.parametrize(
    "gate_name",
    sorted(
        set(native_module.GATE_RAW_SPECS)
        - {"60_minute_rehearsal_passed"}
    ),
)
def test_public_native_producer_rejects_every_unreviewed_gate_command(
    gate_name: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(
        NativeExecutionError,
        match="has no activated reviewed native command contract",
    ):
        REAL_COMMAND_VALIDATOR(
            gate_name=gate_name,
            command=(sys.executable, "-c", "raise SystemExit(0)"),
            cwd=tmp_path.resolve(),
        )


def _reviewed_rehearsal_command(tmp_path: Path) -> list[str]:
    comfy_main = tmp_path / "main.py"
    comfy_main.write_text("raise SystemExit(0)\n", encoding="utf-8")
    comfy_main.chmod(0o600)
    working = tmp_path / "comfy-working"
    models = tmp_path / "comfy-models"
    working.mkdir()
    models.mkdir()
    return [
        str(Path(sys.executable).resolve(strict=True)),
        str(
            (
                native_module.ROOT
                / "scripts"
                / "testing"
                / "operational_campaign_supervisor.py"
            ).resolve(strict=True)
        ),
        "--campaign-root",
        str((tmp_path / "reviewed-campaign").resolve()),
        "--level",
        "rehearsal",
        "--duration-seconds",
        "3600",
        "--comfyui-python-executable",
        str(Path(sys.executable).resolve(strict=True)),
        "--comfyui-main",
        str(comfy_main.resolve(strict=True)),
        "--comfyui-working-root",
        str(working.resolve(strict=True)),
        "--comfyui-models-root",
        str(models.resolve(strict=True)),
        "--comfyui-api-url",
        "http://127.0.0.1:38188",
        "--comfyui-port",
        "38188",
    ]


def test_rehearsal_reviewed_command_contract_is_exact_and_not_arbitrary(
    tmp_path: Path,
) -> None:
    command = _reviewed_rehearsal_command(tmp_path)
    contract = REAL_COMMAND_VALIDATOR(
        gate_name="60_minute_rehearsal_passed",
        command=command,
        cwd=native_module.ROOT.resolve(strict=True),
    )
    assert contract["contract"] == "supervised_rehearsal_v1"
    assert contract["duration_seconds"] == 3600
    assert len(
        native_module.reviewed_rehearsal_native_artifact_paths(
            Path(contract["campaign_root"])
        )
    ) == 41

    with pytest.raises(NativeExecutionError):
        REAL_COMMAND_VALIDATOR(
            gate_name="60_minute_rehearsal_passed",
            command=(sys.executable, "-c", "raise SystemExit(0)"),
            cwd=native_module.ROOT.resolve(strict=True),
        )
    with pytest.raises(NativeExecutionError, match="runner overrides"):
        REAL_COMMAND_VALIDATOR(
            gate_name="60_minute_rehearsal_passed",
            command=(*command, "--", "--allow-short-duration"),
            cwd=native_module.ROOT.resolve(strict=True),
        )


def test_rehearsal_projection_context_is_kernel_sealed_and_has_unique_authority(
    tmp_path: Path,
) -> None:
    context = QualificationContext.create(
        qualification_campaign_uuid=CAMPAIGN_UUID,
        source_authority_path=_source_authority(tmp_path).resolve(),
        invocation_id="capture:rehearsal:pytest",
    )
    campaign_root = (tmp_path / "campaign").resolve()
    native_paths = native_module.reviewed_rehearsal_native_artifact_paths(
        campaign_root
    )
    scenario_authorities = {
        scenario_id: {
            "scenario_attempt_uuid": f"attempt:{index:02d}:{scenario_id}",
            "native_invocation_id": f"invocation:{index:02d}:{scenario_id}",
        }
        for index, scenario_id in enumerate(
            native_module.FORMAL_SCENARIO_BINDINGS,
            start=1,
        )
    }
    payload = build_rehearsal_projection_context(
        context=context,
        attempt_root=(tmp_path / "attempt").resolve(),
        native_artifact_paths=native_paths,
        outer_native_invocation_id="native:60_minute_rehearsal_passed:pytest",
        activation_nonce="activation:60_minute_rehearsal_passed:pytest",
        campaign_attempt_uuid="campaign-attempt:pytest-0001",
        scenario_authorities=scenario_authorities,
    )
    descriptor, locator, digest = native_module._create_sealed_projection_memfd(
        payload
    )
    try:
        reopened = read_sealed_rehearsal_projection_context(locator, digest)
        assert reopened == payload
        assert len(reopened["native_artifact_paths"]) == 41
        assert len(set(
            item["scenario_attempt_uuid"]
            for item in reopened["scenario_authorities"].values()
        )) == 13
        assert len(set(
            item["native_invocation_id"]
            for item in reopened["scenario_authorities"].values()
        )) == 13
        with pytest.raises(OSError):
            os.write(descriptor, b"forged")
    finally:
        os.close(descriptor)


def test_rehearsal_archive_reopen_rejects_inventory_substitution(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "scenario.tar"
    content = b'{"terminal_state":"success"}'
    with tarfile.open(archive_path, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo("artifacts/proof.json")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    archive_path.chmod(0o600)
    inventory = [{
        "member_path": "artifacts/proof.json",
        "sha256": native_module.hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }]
    native_module._verify_rehearsal_archive(
        archive_path,
        {"member_inventory": inventory},
        scenario_id="archive-reopen-test",
    )
    forged = [{**inventory[0], "sha256": "0" * 64}]
    with pytest.raises(NativeExecutionError, match="content/inventory mismatch"):
        native_module._verify_rehearsal_archive(
            archive_path,
            {"member_inventory": forged},
            scenario_id="archive-reopen-test",
        )


def test_rehearsal_projection_cannot_promote_non_pass_and_leaves_no_roles(
    tmp_path: Path,
) -> None:
    context = QualificationContext.create(
        qualification_campaign_uuid=CAMPAIGN_UUID,
        source_authority_path=_source_authority(tmp_path).resolve(),
        invocation_id="capture:no-posthoc-pass",
    )
    campaign_root = (tmp_path / "failed-campaign").resolve()
    (campaign_root / "reports").mkdir(parents=True)
    (campaign_root / "artifacts").mkdir()
    runner_path = campaign_root / "reports" / "operational_campaign_24h.json"
    supervisor_path = campaign_root / "artifacts" / "campaign_supervisor.json"
    _write_private_json(runner_path, {
        "schema_version": "hackme.campaign-operational-result/v1",
        "ok": False,
        "verdict": "FAIL_PRODUCT",
        "classification": "FAIL_PRODUCT",
    })
    _write_private_json(supervisor_path, {
        "schema_version": "hackme.campaign-supervisor.v1",
        "level": "rehearsal",
        "ok": True,
        "classification": "PASS",
        "runner_returncode": 0,
        "runner_verdict": "PASS",
        "runner_report": str(runner_path),
        "source_final": {"verified": True},
        "cleanup": {
            "source_monitor": {"ok": True},
            "watchdog": {"ok": True},
            "scope": {"ok": True},
        },
        "campaign_uuid": "no-posthoc-pass-campaign",
    })
    native_paths = native_module.reviewed_rehearsal_native_artifact_paths(
        campaign_root
    )
    scenario_authorities = {
        scenario_id: {
            "scenario_attempt_uuid": f"attempt:{index:02d}:{scenario_id}",
            "native_invocation_id": f"invocation:{index:02d}:{scenario_id}",
        }
        for index, scenario_id in enumerate(
            native_module.FORMAL_SCENARIO_BINDINGS,
            start=1,
        )
    }
    projection = build_rehearsal_projection_context(
        context=context,
        attempt_root=(tmp_path / "failed-attempt").resolve(),
        native_artifact_paths=native_paths,
        outer_native_invocation_id="native:60_minute_rehearsal_passed:no-posthoc",
        activation_nonce="activation:60-minute-rehearsal:no-posthoc",
        campaign_attempt_uuid="campaign-attempt:no-posthoc",
        scenario_authorities=scenario_authorities,
    )
    started = datetime.now(timezone.utc) - timedelta(hours=1, seconds=1)
    with pytest.raises(NativeExecutionError, match="runner did not independently PASS"):
        native_module._materialize_rehearsal_projection(
            context=context,
            projection_context=projection,
            reviewed_command={"campaign_root": campaign_root},
            native_artifact_paths=native_paths,
            started_at=started,
            started_monotonic_ns=10_000_000_000,
            child_finished_at=started + timedelta(hours=1),
            child_finished_monotonic_ns=3_610_000_000_000,
        )
    assert not (
        campaign_root / "artifacts" / "formal_native_rehearsal"
    ).exists()
    assert not any(path.exists() for path in native_paths.values())


def test_failed_projection_cleanup_removes_only_exact_role_files(
    tmp_path: Path,
) -> None:
    campaign_root = (tmp_path / "cleanup-campaign").resolve()
    paths = native_module.reviewed_rehearsal_native_artifact_paths(campaign_root)
    root = next(iter(paths.values())).parent
    root.mkdir(parents=True)
    partial = paths["runner_result"]
    partial.write_text("{}", encoding="utf-8")
    partial.chmod(0o600)

    native_module._cleanup_failed_rehearsal_projection(campaign_root)

    assert not root.exists()


def test_execute_rejects_unreviewed_command_before_child_or_source_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_module,
        "_validate_reviewed_command",
        REAL_COMMAND_VALIDATOR,
    )
    child_marker = tmp_path / "unreviewed-child-ran"
    artifact = (tmp_path / "unreviewed-security.json").resolve()
    archive_artifact = (tmp_path / "unreviewed-security-audit.tar").resolve()

    with pytest.raises(
        NativeExecutionError,
        match="has no activated reviewed native command contract",
    ):
        execute_and_capture_gate(
            attempt_root=(tmp_path / "unreviewed-attempt").resolve(),
            gate_name="production_security_sentinel_verified",
            source_authority_path=(tmp_path / "missing-source.json").resolve(),
            qualification_campaign_uuid=CAMPAIGN_UUID,
            native_artifact_paths={
                "security_sentinel": artifact,
                "audit_evidence_archive": archive_artifact,
            },
            command=[
                sys.executable,
                "-c",
                f"open({str(child_marker)!r}, 'w').close()",
            ],
            cwd=tmp_path.resolve(),
            timeout_seconds=30,
        )

    assert not child_marker.exists()
    assert not artifact.exists()
    assert not (tmp_path / "unreviewed-attempt").exists()


def test_native_producer_rejects_self_reported_pass_without_semantic_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pass = {
        "schema_version": "hackme.production-security-sentinel.v1",
        "ok": True,
        "failed_checks": [],
        "checks": [],
    }

    with pytest.raises(Exception, match="semantic authority did not derive PASS"):
        _run_security_attempt(tmp_path, monkeypatch, fake_pass)

    attempt = tmp_path / "attempt-security"
    failure = json.loads((attempt / "attempt.failure.json").read_text(encoding="utf-8"))
    assert failure["status"] == "FAIL_HARNESS"
    assert not (attempt / "evidence" / "production_security_sentinel_verified.json").exists()


@pytest.mark.parametrize(
    "marker",
    [
        {"skip": "not installed"},
        {"skip_reason": "not installed"},
        {"fallback_error": "provider unavailable"},
        {"fallback_reason": "provider unavailable"},
        {"expected_gaps": ["mobile"]},
        {"execution_mode": "simulation"},
        {"not_run": "browser unavailable"},
        {"simulated": True},
        {"synthetic": True},
        {"actual_execution": False},
        {"actual_execution": "false"},
    ],
)
def test_native_producer_rejects_skip_fallback_and_fake_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: dict[str, object],
) -> None:
    report = _security_report()
    report.update(marker)

    with pytest.raises(NativeExecutionError, match="forbidden skip/fallback/fake markers"):
        _run_security_attempt(tmp_path, monkeypatch, report)

    assert not (tmp_path / "attempt-security").exists()


def test_native_producer_refuses_preexisting_artifact_before_child_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_module,
        "_verify_live_source",
        lambda _context, *, scratch_root: _source_proof(),
    )
    artifact = _write_private_json(tmp_path / "security-native.json", _security_report())
    archive_artifact = (tmp_path / "security-native-audit.tar").resolve()
    child_marker = tmp_path / "child-ran"

    with pytest.raises(NativeExecutionError, match="already exists"):
        execute_and_capture_gate(
            attempt_root=(tmp_path / "attempt-security").resolve(),
            gate_name="production_security_sentinel_verified",
            source_authority_path=_source_authority(tmp_path).resolve(),
            qualification_campaign_uuid=CAMPAIGN_UUID,
            native_artifact_paths={
                "security_sentinel": artifact.resolve(),
                "audit_evidence_archive": archive_artifact,
            },
            command=[sys.executable, "-c", f"open({str(child_marker)!r}, 'w').close()"],
            cwd=tmp_path.resolve(),
            timeout_seconds=30,
        )

    assert not child_marker.exists()


def test_native_producer_rejects_nonzero_child_without_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_module,
        "_verify_live_source",
        lambda _context, *, scratch_root: _source_proof(),
    )
    artifact = (tmp_path / "security-native.json").resolve()
    archive_artifact = (tmp_path / "security-native-audit.tar").resolve()
    payload, archive_bytes = _security_bundle_payload(tmp_path, _security_report())

    with pytest.raises(NativeExecutionError, match="exited with 7"):
        execute_and_capture_gate(
            attempt_root=(tmp_path / "attempt-security").resolve(),
            gate_name="production_security_sentinel_verified",
            source_authority_path=_source_authority(tmp_path).resolve(),
            qualification_campaign_uuid=CAMPAIGN_UUID,
            native_artifact_paths={
                "security_sentinel": artifact,
                "audit_evidence_archive": archive_artifact,
            },
            command=_artifact_writer_command(
                artifact,
                payload,
                returncode=7,
                archive_path=archive_artifact,
                archive_bytes=archive_bytes,
            ),
            cwd=tmp_path.resolve(),
            timeout_seconds=30,
        )

    assert not list(tmp_path.glob("*.native.json"))
    assert not (tmp_path / "attempt-security").exists()


def test_native_producer_rejects_sensitive_environment_value_in_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "native-gate-secret-value-4eb97b59"
    monkeypatch.setenv("FORMAL_NATIVE_TEST_API_TOKEN", secret)
    report = _security_report()
    report["diagnostic"] = secret

    with pytest.raises(NativeExecutionError, match="credential scan failed closed"):
        _run_security_attempt(tmp_path, monkeypatch, report)

    assert not list(tmp_path.glob("*.native.json"))
    assert not (tmp_path / "attempt-security").exists()


def test_native_producer_kills_and_rejects_setsid_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_module,
        "_verify_live_source",
        lambda _context, *, scratch_root: _source_proof(),
    )
    artifact = (tmp_path / "security-native.json").resolve()
    archive_artifact = (tmp_path / "security-native-audit.tar").resolve()
    daemon_pid = (tmp_path / "escaped.pid").resolve()
    report, archive_bytes = _security_bundle_payload(tmp_path, _security_report())
    report_json = json.dumps(report, sort_keys=True)
    code = (
        "import json,pathlib,subprocess,sys; "
        f"p=pathlib.Path({str(artifact)!r}); "
        f"p.write_text({report_json!r},encoding='utf-8'); p.chmod(0o600); "
        f"a=pathlib.Path({str(archive_artifact)!r}); "
        f"a.write_bytes(bytes.fromhex({archive_bytes.hex()!r})); a.chmod(0o600); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,close_fds=True); "
        f"pathlib.Path({str(daemon_pid)!r}).write_text(str(child.pid),encoding='ascii')"
    )

    with pytest.raises(
        NativeExecutionError,
        match="left descendants outside its process group",
    ):
        execute_and_capture_gate(
            attempt_root=(tmp_path / "attempt-security").resolve(),
            gate_name="production_security_sentinel_verified",
            source_authority_path=_source_authority(tmp_path).resolve(),
            qualification_campaign_uuid=CAMPAIGN_UUID,
            native_artifact_paths={
                "security_sentinel": artifact,
                "audit_evidence_archive": archive_artifact,
            },
            command=[sys.executable, "-c", code],
            cwd=tmp_path.resolve(),
            timeout_seconds=30,
        )

    escaped_pid = int(daemon_pid.read_text(encoding="ascii"))
    deadline = time.monotonic() + 3.0
    while Path(f"/proc/{escaped_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not Path(f"/proc/{escaped_pid}").exists()
    assert not list(tmp_path.glob("*.native.json"))


def test_live_source_guard_detects_transient_modify_and_restore(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "qa@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "QA"], check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    with GitSourceFreezer(repo, tmp_path / "freeze") as freezer:
        authority = freezer.capture(label="H0", require_clean=True)
    authority_path = Path(str(authority["artifact_root"])) / "source_freeze.json"
    authority_path.chmod(0o600)
    context = QualificationContext.create(
        qualification_campaign_uuid=CAMPAIGN_UUID,
        source_authority_path=authority_path.resolve(),
    )
    guard = REAL_SOURCE_GUARD(
        context,
        {"repo_root": str(repo.resolve())},
        scratch_root=tmp_path / "guard",
    )
    try:
        tracked.write_text("temporary mutation\n", encoding="utf-8")
        tracked.write_text("baseline\n", encoding="utf-8")
        with pytest.raises(NativeExecutionError, match="source changed"):
            guard.verify()
    finally:
        guard.close()


def test_native_producer_rejects_source_change_between_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proofs = [_source_proof(), {**_source_proof(), "source_digest": "f" * 64}]
    monkeypatch.setattr(
        native_module,
        "_verify_live_source",
        lambda _context, *, scratch_root: proofs.pop(0),
    )
    artifact = (tmp_path / "security-native.json").resolve()
    archive_artifact = (tmp_path / "security-native-audit.tar").resolve()
    payload, archive_bytes = _security_bundle_payload(tmp_path, _security_report())

    with pytest.raises(NativeExecutionError, match="source authority changed"):
        execute_and_capture_gate(
            attempt_root=(tmp_path / "attempt-security").resolve(),
            gate_name="production_security_sentinel_verified",
            source_authority_path=_source_authority(tmp_path).resolve(),
            qualification_campaign_uuid=CAMPAIGN_UUID,
            native_artifact_paths={
                "security_sentinel": artifact,
                "audit_evidence_archive": archive_artifact,
            },
            command=_artifact_writer_command(
                artifact,
                payload,
                archive_path=archive_artifact,
                archive_bytes=archive_bytes,
            ),
            cwd=tmp_path.resolve(),
            timeout_seconds=30,
        )

    assert not list(tmp_path.glob("*.native.json"))
    assert not (tmp_path / "attempt-security").exists()
