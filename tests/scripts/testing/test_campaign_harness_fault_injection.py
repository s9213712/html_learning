from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts.testing.campaign_harness_fault_injection import (
    FAULT_INJECTION_SCHEMA_VERSION,
    GATE_BUNDLE_SCHEMA_VERSION,
    FaultInjectionError,
    load_recovery_checkpoint,
    main,
    probe_checkpoint_recovery_and_tamper,
    probe_hard_stop_state_admission_clock,
    probe_sample_completeness_empty_collector,
    probe_source_drift_isolated_git,
    run_level0,
)


ROOT = Path(__file__).resolve().parents[3]


def isolated_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    commands = (
        ("init", "--quiet"),
        ("config", "user.email", "harness-test@example.invalid"),
        ("config", "user.name", "Harness Test"),
    )
    for arguments in commands:
        subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    (repo / "fixture.txt").write_text("clean fixture\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "fixture.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "fixture"],
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


def test_hard_stop_injection_closes_admission_and_freezes_credit(tmp_path: Path) -> None:
    evidence = probe_hard_stop_state_admission_clock(tmp_path)

    assert evidence["state"] == "STOPPING_LOAD"
    assert evidence["control"]["admit_new_jobs"] is False
    assert evidence["control"]["load_generator_should_run"] is False
    assert evidence["clock"]["continuous_active_seconds"] == 5
    assert evidence["clock"]["invalid_seconds"] == 3
    assert evidence["clock"]["formal_segment_valid"] is False
    assert evidence["post_stop_tick_rejected"] is True


def test_checkpoint_probe_recovers_torn_primary_and_rejects_tamper(tmp_path: Path) -> None:
    evidence = probe_checkpoint_recovery_and_tamper(tmp_path)

    assert evidence["corrupt_primary_rejected"] is True
    assert evidence["valid_checkpoint_recovered"] is True
    assert evidence["tampered_checkpoint_rejected"] is True
    assert evidence["tampered_destination_absent"] is True


def test_checkpoint_loader_rejects_wrong_campaign_identity(tmp_path: Path) -> None:
    evidence = probe_checkpoint_recovery_and_tamper(tmp_path)

    with pytest.raises(FaultInjectionError, match="campaign UUID mismatch"):
        load_recovery_checkpoint(
            Path(evidence["checkpoint_path"]),
            expected_campaign_uuid="different-campaign",
            minimum_revision=1,
        )


def test_empty_resource_collector_can_never_reach_completeness_gate(tmp_path: Path) -> None:
    evidence = probe_sample_completeness_empty_collector(tmp_path)

    assert evidence["zero_sample_summary"]["samples"] == 0
    assert evidence["zero_sample_summary"]["mandatory_field_completeness"] == 0
    assert evidence["zero_sample_summary"]["ok"] is False
    assert evidence["schema_only_summary"]["samples"] == 1
    assert evidence["schema_only_summary"]["mandatory_field_completeness"] == 0
    assert evidence["schema_only_summary"]["ok"] is False


def test_source_drift_probe_uses_an_isolated_repository(tmp_path: Path) -> None:
    evidence = probe_source_drift_isolated_git(tmp_path)

    assert Path(evidence["repo_root"]).is_relative_to(tmp_path)
    assert evidence["verified_after_injection"] is False
    assert "service.py" in evidence["tracked_changes"]
    assert evidence["status_unchanged"] is False
    assert evidence["untracked_paths_unchanged"] is False


def test_level0_writes_partial_bundle_and_never_fakes_actual_gates(tmp_path: Path) -> None:
    result = run_level0(
        artifact_root=tmp_path / "artifacts",
        repo_root=isolated_git_repo(tmp_path),
    )

    assert result["schema_version"] == FAULT_INJECTION_SCHEMA_VERSION
    assert result["core_ok"] is True
    assert result["formal_ready"] is False
    assert result["actual_cgroup_watchdog_injection_ran"] is False
    for name in (
        "hard_stop_injection_verified",
        "checkpoint_recovery_verified",
        "sample_schema_completeness_verified",
        "source_drift_detection_verified",
    ):
        assert result["component_gates"][name]["status"] == "PASS"
        assert result["component_gates"][name]["machine_verified"] is True
        assert result["gates"][name]["status"] == "PARTIAL_PASS"
        assert result["gates"][name]["machine_verified"] is False
        assert result["gates"][name]["evidence"]["verification_scope"] == "component_only"
    for name in ("cgroup_limits_verified", "external_watchdog_verified"):
        assert result["gates"][name]["status"] == "NOT_RUN"
        assert result["gates"][name]["machine_verified"] is False

    bundle = json.loads(Path(result["artifacts"]["partial_gate_bundle"]).read_text(encoding="utf-8"))
    assert bundle["schema_version"] == GATE_BUNDLE_SCHEMA_VERSION
    assert bundle["commit"] == result["commit"]
    assert bundle["ok"] is False


def test_level0_refuses_to_mix_with_existing_artifacts(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    (artifact_root / "old.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FaultInjectionError, match="absent or empty"):
        run_level0(artifact_root=artifact_root, repo_root=ROOT)


def test_cli_returns_success_for_core_but_bundle_remains_nonformal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    artifact_root = tmp_path / "cli"
    repo_root = isolated_git_repo(tmp_path)

    assert main(["--artifact-root", str(artifact_root), "--repo-root", str(repo_root)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["core_ok"] is True
    assert output["formal_ready"] is False
    assert output["gates"]["external_watchdog_verified"]["status"] == "NOT_RUN"
