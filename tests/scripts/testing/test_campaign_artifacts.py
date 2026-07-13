from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zipfile

from PIL import Image, PngImagePlugin
import pytest

from scripts.testing import campaign_artifacts as artifacts
from scripts.testing.campaign_contract import (
    FormalResultStatus,
    ScenarioContract,
    ScenarioResult,
)


COMMIT = "b" * 40
SOURCE_DIGEST = "a" * 64
SCENARIO = "cloud_drive_share_001"


def spec(
    path: Path,
    artifact_id: str = "artifact_001",
    artifact_type: artifacts.ArtifactType = artifacts.ArtifactType.AUTO,
) -> artifacts.ArtifactSpec:
    return artifacts.ArtifactSpec(
        artifact_id=artifact_id,
        scenario_id=SCENARIO,
        path=path,
        artifact_type=artifact_type,
    )


def validate(path: Path, artifact_type: artifacts.ArtifactType = artifacts.ArtifactType.AUTO, **kwargs: object) -> dict:
    return artifacts.validate_artifact(
        spec(path, artifact_type=artifact_type),
        known_scenario_ids={SCENARIO},
        artifact_root=path.parent,
        **kwargs,
    )


def scenario_contract(artifact_id: str = "artifact_001") -> ScenarioContract:
    return ScenarioContract(
        scenario_id=SCENARIO,
        domain="cloud_drive",
        mandatory=True,
        role="user",
        preconditions=("primary_ready",),
        steps=("upload", "share", "revoke"),
        expected_terminal_state="success",
        side_effect_assertions=("share_revoked",),
        cleanup_assertions=("fixture_deleted",),
        artifacts=(artifact_id,),
        deadline_seconds=180,
        earliest_start=0,
        preferred_window=(30, 120),
        hard_deadline=240,
        resource_class=("disk_light",),
        conflicts_with=(),
    )


def scenario_result(artifact_id: str = "artifact_001") -> ScenarioResult:
    return ScenarioResult(
        scenario_id=SCENARIO,
        status=FormalResultStatus.PASS,
        terminal_state="success",
        elapsed_seconds=12,
        side_effect_assertions={"share_revoked": True},
        cleanup_assertions={"fixture_deleted": True},
        artifact_ids=(artifact_id,),
    )


def formal_evidence(artifact_id: str = "artifact_001") -> dict[str, object]:
    return {
        "scenario_contracts": {SCENARIO: scenario_contract(artifact_id)},
        "scenario_results": {SCENARIO: scenario_result(artifact_id)},
    }


def test_json_and_jsonl_are_reparsed_and_empty_payloads_cannot_pass(tmp_path: Path) -> None:
    valid_json = tmp_path / "valid.json"
    valid_json.write_text('{"result":"success"}\n', encoding="utf-8")
    valid_record = validate(valid_json)
    assert valid_record["validated"] is True
    assert valid_record["format_validation"]["details"]["semantic_nonempty"] is True

    empty_json = tmp_path / "empty.json"
    empty_json.write_text("{}\n", encoding="utf-8")
    assert validate(empty_json)["validated"] is False

    valid_jsonl = tmp_path / "valid.jsonl"
    valid_jsonl.write_text('{"sample":1}\n{"sample":2}\n', encoding="utf-8")
    assert validate(valid_jsonl)["format_validation"]["details"]["record_count"] == 2
    assert validate(valid_jsonl)["validated"] is True

    empty_jsonl = tmp_path / "empty.jsonl"
    empty_jsonl.write_text("\n", encoding="utf-8")
    assert validate(empty_jsonl)["validated"] is False


def test_malformed_json_fails_format_validation(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")
    record = validate(path)
    assert record["validated"] is False
    assert "format_validation_failed" in record["errors"]


def test_image_is_decoded_and_metadata_is_secret_scanned(tmp_path: Path) -> None:
    clean = tmp_path / "clean.png"
    Image.new("RGB", (2, 3), color="blue").save(clean)
    record = validate(clean)
    assert record["validated"] is True
    assert record["format_validation"]["details"] == {"format": "PNG", "width": 2, "height": 3}

    secret_value = "campaign-credential-123"
    tainted = tmp_path / "tainted.png"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", secret_value)
    Image.new("RGB", (2, 2), color="red").save(tainted, pnginfo=metadata)
    tainted_record = validate(tainted, known_secret_values={"member": secret_value})
    assert tainted_record["validated"] is False
    assert tainted_record["secret_scan"]["finding_count"] >= 1
    assert secret_value not in json.dumps(tainted_record)


def test_video_uses_ffprobe_and_scans_metadata_without_leaking_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sample.mp4"
    path.write_bytes(b"synthetic-video-container")
    secret_value = "provider-credential-456"

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == "ffprobe" else None

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "format": {"duration": "2.5", "tags": {"comment": secret_value}},
                "streams": [{"codec_type": "video", "codec_name": "h264"}],
            }),
            stderr="",
        )

    monkeypatch.setattr(artifacts.shutil, "which", fake_which)
    monkeypatch.setattr(artifacts.subprocess, "run", fake_run)

    record = validate(
        path,
        artifacts.ArtifactType.VIDEO,
        known_secret_values={"provider": secret_value},
    )
    assert record["format_validation"]["details"]["duration_seconds"] == 2.5
    assert record["validated"] is False
    assert record["secret_scan"]["finding_count"] == 1
    assert secret_value not in json.dumps(record)


def test_missing_video_validator_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sample.mp4"
    path.write_bytes(b"synthetic-video-container")
    monkeypatch.setattr(artifacts.shutil, "which", lambda _name: None)
    record = validate(path, artifacts.ArtifactType.VIDEO)
    assert record["validated"] is False
    assert record["format_validation"]["errors"] == ["video_validator_unavailable"]


def test_archive_is_fully_read_and_compressed_members_are_secret_scanned(tmp_path: Path) -> None:
    clean = tmp_path / "clean.zip"
    with zipfile.ZipFile(clean, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("reports/result.json", '{"ok":true}')
    assert validate(clean)["validated"] is True

    secret_value = "archive-credential-789"
    tainted = tmp_path / "tainted.zip"
    with zipfile.ZipFile(tainted, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("logs/runtime.log", f"credential={secret_value}")
    record = validate(tainted, known_secret_values={"root": secret_value})
    assert record["validated"] is False
    assert any(finding["source"].startswith("archive_member:") for finding in record["secret_scan"]["findings"])
    assert "logs/runtime.log" not in json.dumps(record["secret_scan"])
    assert secret_value not in json.dumps(record)


def test_empty_or_unsafe_archive_cannot_pass(tmp_path: Path) -> None:
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w"):
        pass
    assert validate(empty)["validated"] is False

    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.txt", "content")
    record = validate(unsafe)
    assert record["validated"] is False
    assert "unsafe_archive_member_path" in record["format_validation"]["errors"]


def test_sqlite_runs_quick_check_and_rejects_schema_empty_database(tmp_path: Path) -> None:
    valid = tmp_path / "valid.db"
    connection = sqlite3.connect(valid)
    connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, result TEXT NOT NULL)")
    connection.execute("INSERT INTO evidence(result) VALUES ('pass')")
    connection.commit()
    connection.close()
    record = validate(valid)
    assert record["validated"] is True
    assert record["format_validation"]["details"]["quick_check"] == ["ok"]

    empty = tmp_path / "empty.db"
    connection = sqlite3.connect(empty)
    connection.execute("PRAGMA user_version=1")
    connection.close()
    empty_record = validate(empty)
    assert empty_record["validated"] is False
    assert "sqlite_schema_is_empty" in empty_record["format_validation"]["errors"]


def test_known_secret_scan_records_only_label_and_offset(tmp_path: Path) -> None:
    secret_value = "campaign-secret-value-012345"
    path = tmp_path / "evidence.log"
    path.write_text(f"operation complete\ncredential={secret_value}\n", encoding="utf-8")
    record = validate(path, known_secret_values={"test_credential": secret_value})
    serialized = json.dumps(record)
    assert record["secret_scan"]["performed"] is True
    assert record["secret_scan"]["coverage_complete"] is True
    assert record["secret_scan"]["ok"] is False
    assert record["secret_scan"]["findings"][0]["pattern"] == "known_secret:test_credential"
    assert secret_value not in serialized


def test_missing_unknown_outside_root_and_hash_mismatch_all_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    missing_record = validate(missing)
    assert missing_record["validated"] is False
    assert missing_record["secret_scan"]["performed"] is False

    present = tmp_path / "present.json"
    present.write_text('{"ok":true}\n', encoding="utf-8")
    unknown = artifacts.validate_artifact(spec(present), known_scenario_ids={"different_scenario"})
    assert unknown["validated"] is False
    assert "unknown_scenario_id" in unknown["errors"]

    outside = artifacts.validate_artifact(
        spec(present),
        known_scenario_ids={SCENARIO},
        artifact_root=tmp_path / "other_root",
    )
    assert outside["validated"] is False
    assert "artifact_outside_artifact_root" in outside["errors"]

    mismatch_spec = artifacts.ArtifactSpec(
        artifact_id="hash_mismatch_001",
        scenario_id=SCENARIO,
        path=present,
        expected_sha256="0" * 64,
    )
    mismatch = artifacts.validate_artifact(mismatch_spec, known_scenario_ids={SCENARIO})
    assert mismatch["validated"] is False
    assert "sha256_mismatch" in mismatch["errors"]


def test_artifact_mutation_during_validation_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "changing.json"
    path.write_text('{"state":"before"}\n', encoding="utf-8")
    original_scan = artifacts.scan_artifact_secrets

    def mutate_after_scan(*args: object, **kwargs: object) -> dict:
        result = original_scan(*args, **kwargs)
        path.write_text('{"state":"after-and-different-size"}\n', encoding="utf-8")
        return result

    monkeypatch.setattr(artifacts, "scan_artifact_secrets", mutate_after_scan)
    record = validate(path)
    assert record["validated"] is False
    assert record["validation_snapshot_stable"] is False
    assert "artifact_changed_during_validation" in record["errors"]


def test_index_cannot_pass_empty_missing_scenario_or_missing_required_artifact(tmp_path: Path) -> None:
    empty = artifacts.build_artifact_index(
        campaign_uuid="campaign-001",
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        artifacts=[],
        known_scenario_ids={SCENARIO},
        artifact_root=tmp_path,
    )
    assert empty["ok"] is False
    assert "artifact_declarations_empty" in empty["errors"]
    assert empty["summary"]["covered_scenario_count"] == 0

    path = tmp_path / "one.json"
    path.write_text('{"ok":true}\n', encoding="utf-8")
    incomplete = artifacts.build_artifact_index(
        campaign_uuid="campaign-002",
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        artifacts=[spec(path)],
        known_scenario_ids={SCENARIO, "second_scenario_001"},
        required_artifacts_by_scenario={SCENARIO: {"artifact_001", "missing_001"}},
        artifact_root=tmp_path,
    )
    assert incomplete["ok"] is False
    assert f"scenario_without_artifact:second_scenario_001" in incomplete["errors"]
    assert f"scenario_missing_required_artifacts:{SCENARIO}" in incomplete["errors"]


def test_valid_index_has_schema_summary_hash_manifest_and_survives_readback(tmp_path: Path) -> None:
    path = tmp_path / "one.json"
    path.write_text('{"result":"pass"}\n', encoding="utf-8")
    index = artifacts.build_artifact_index(
        campaign_uuid="campaign-003",
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        artifacts=[spec(path)],
        known_scenario_ids={SCENARIO},
        required_artifacts_by_scenario={SCENARIO: {"artifact_001"}},
        artifact_root=tmp_path,
        **formal_evidence(),
    )

    assert index["schema_version"] == artifacts.ARTIFACT_INDEX_SCHEMA_VERSION
    assert index["ok"] is True
    assert index["summary"] == {
        "artifact_count": 1,
        "validated_count": 1,
        "invalid_count": 0,
        "mandatory_count": 1,
        "scenario_count": 1,
        "covered_scenario_count": 1,
        "secret_finding_count": 0,
    }
    assert len(index["hash_manifest_sha256"]) == 64
    assert artifacts.validate_artifact_index(index)["ok"] is True

    persisted = artifacts.write_artifact_index(tmp_path / "index" / "artifacts.json", index)
    assert persisted["ok"] is True
    assert persisted["size"] > 0
    assert len(persisted["sha256"]) == 64


def test_index_self_validation_detects_tampered_hash_or_summary(tmp_path: Path) -> None:
    path = tmp_path / "one.json"
    path.write_text('{"result":"pass"}\n', encoding="utf-8")
    index = artifacts.build_artifact_index(
        campaign_uuid="campaign-004",
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        artifacts=[spec(path)],
        known_scenario_ids={SCENARIO},
        artifact_root=tmp_path,
        **formal_evidence(),
    )
    index["hash_manifest_sha256"] = "0" * 64
    index["summary"]["artifact_count"] = 0
    validation = artifacts.validate_artifact_index(index)
    assert validation["ok"] is False
    assert {"hash_manifest_mismatch", "summary_mismatch"} <= set(validation["errors"])


def test_index_self_validation_rechecks_record_components_not_only_validated_flag(tmp_path: Path) -> None:
    path = tmp_path / "one.json"
    path.write_text('{"result":"pass"}\n', encoding="utf-8")
    index = artifacts.build_artifact_index(
        campaign_uuid="campaign-005",
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        artifacts=[spec(path)],
        known_scenario_ids={SCENARIO},
        artifact_root=tmp_path,
        **formal_evidence(),
    )
    index["artifacts"][0]["secret_scan"]["ok"] = False
    validation = artifacts.validate_artifact_index(index)
    assert validation["ok"] is False
    assert "artifact_record_gate_failed:artifact_001" in validation["errors"]


def test_formal_index_binds_result_to_validated_same_scenario_artifact(tmp_path: Path) -> None:
    path = tmp_path / "one.json"
    path.write_text('{"result":"pass"}\n', encoding="utf-8")
    index = artifacts.build_artifact_index(
        campaign_uuid="campaign-006",
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        artifacts=[spec(path)],
        known_scenario_ids={SCENARIO},
        scenario_contracts={SCENARIO: scenario_contract()},
        scenario_results={SCENARIO: scenario_result("missing_result_artifact_001")},
        artifact_root=tmp_path,
    )

    assert index["artifact_gate_ok"] is True
    assert index["scenario_result_gate"]["ok"] is False
    assert (
        f"result_artifact_missing:{SCENARIO}:missing_result_artifact_001"
        in index["scenario_result_gate"]["result_artifact_link_errors"]
    )
    assert index["ok"] is False


def test_explicit_smoke_disable_never_yields_overall_green(tmp_path: Path) -> None:
    path = tmp_path / "one.json"
    path.write_text('{"result":"pass"}\n', encoding="utf-8")
    index = artifacts.build_artifact_index(
        campaign_uuid="campaign-smoke-001",
        commit=COMMIT,
        source_digest=SOURCE_DIGEST,
        artifacts=[spec(path)],
        known_scenario_ids={SCENARIO},
        require_scenario_results=False,
        scenario_gate_disabled_reason="level0_harness_smoke_does_not_execute_feature_contracts",
        artifact_root=tmp_path,
    )

    assert index["artifact_gate_ok"] is True
    assert index["scenario_result_gate"]["required"] is False
    assert index["scenario_result_gate"]["ok"] is False
    assert index["ok"] is False
    validation = artifacts.validate_artifact_index(index)
    assert validation["schema_ok"] is True
    assert validation["artifact_gate_ok"] is True
    assert validation["scenario_result_gate_ok"] is False
    assert validation["ok"] is False
