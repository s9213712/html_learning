from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.testing.campaign_watchdog_sigstop_e2e import (
    EXACT_STALE_SECONDS,
    EXPECTED_LIMITS,
    INCIDENT_EXIT_CODE,
    REQUIRED_ASSERTIONS,
    SCHEMA_VERSION,
    SigstopE2EError,
    _artifact_record,
    _wait_for_json,
    assess_e2e_evidence,
    build_parser,
    main,
    validate_campaign_root,
)


COMMIT = "a" * 40
SCOPE = "/user.slice/user-1000.slice/hackme-web-campaign-test.scope"


def _passing_evidence() -> dict:
    placement_inside = {
        "ok": True,
        "inside_campaign_scope": True,
        "campaign_cgroup": SCOPE,
        "procfs_cgroupfs_agree": True,
    }
    placement_outside = {
        "ok": True,
        "inside_campaign_scope": False,
        "campaign_cgroup": SCOPE,
        "procfs_cgroupfs_agree": True,
    }
    artifact = {
        "artifact_id": "watchdog_incident",
        "path": "/tmp/campaign/artifacts/watchdog_incident.json",
        "relative_path": "artifacts/watchdog_incident.json",
        "media_type": "application/json",
        "schema_version": "hackme.campaign-watchdog.v1",
        "sha256": "b" * 64,
        "size": 100,
        "validated": True,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_uuid": "watchdog-sigstop-test",
        "commit": COMMIT,
        "actual_external_execution": True,
        "stale_timeout_seconds": EXACT_STALE_SECONDS,
        "source": {
            "actual_commit": COMMIT,
            "expected_commit": COMMIT,
            "commit_matches": True,
            "worktree_clean": True,
        },
        "cgroup": {
            "path": SCOPE,
            "limits": {
                "ok": True,
                "hard_limit_state": "verified",
                "checks": {
                    "memory.high": {"actual": EXPECTED_LIMITS["memory.high"], "ok": True},
                    "memory.max": {"actual": EXPECTED_LIMITS["memory.max"], "ok": True},
                    "memory.swap.max": {"actual": EXPECTED_LIMITS["memory.swap.max"], "ok": True},
                    "pids.max": {"actual": EXPECTED_LIMITS["pids.max"], "ok": True},
                    "cpu.max": {"actual_percent": EXPECTED_LIMITS["cpu.quota_percent"], "ok": True},
                },
            },
            "after_pids": [],
            "cleanup": {"ok": True, "cgroup_empty": True},
        },
        "processes": {
            "orchestrator": {
                "pid": 42001,
                "start_ticks": 1001,
                "placement": placement_inside,
                "state_after_sigstop": "T",
                "terminated": True,
            },
            "load": {
                "pid": 42002,
                "start_ticks": 1002,
                "placement": placement_inside,
                "terminated": True,
            },
            "watchdog": {
                "pid": 42003,
                "start_ticks": 1003,
                "placement": placement_outside,
                "terminated_after_result": True,
                "returncode": INCIDENT_EXIT_CODE,
            },
        },
        "watchdog": {
            "initial": {
                "verified": True,
                "external_process": True,
                "watchdog_outside_campaign_cgroup": True,
            },
            "final": {
                "reason": "HEARTBEAT_STALE",
                "evidence_path": "/tmp/campaign/artifacts/watchdog/watchdog_incident.json",
                "finished_at": "2026-07-13T00:02:01Z",
                "cgroup_stop": {
                    "freeze_written": True,
                    "kill_written": True,
                    "population_cleared": True,
                },
            },
        },
        "timings": {
            "heartbeat_last_monotonic_ns": 1_000_000_000,
            "sigstop_monotonic_ns": 1_100_000_000,
            "admission_closed_monotonic_ns": 121_100_000_000,
            "timer_stopped_monotonic_ns": 121_100_000_000,
            "scope_empty_monotonic_ns": 121_200_000_000,
            "stale_observed_seconds": 120.1,
            "heartbeat_to_detection_seconds": 120.1,
            "sigstop_to_detection_seconds": 120.0,
        },
        "state": {
            "at_sigstop": {"clock": {"continuous_active_seconds": 2.5}},
            "final": {
                "state": "INTERRUPTED",
                "control": {"admit_new_jobs": False, "load_generator_should_run": False},
                "clock": {
                    "continuous_active_seconds": 2.5,
                    "formal_segment_valid": False,
                    "clock_pause_reason": "HEARTBEAT_STALE",
                    "active_finished_at": "2026-07-13T00:02:01Z",
                },
            },
            "final_control": {"admit_new_jobs": False, "load_generator_should_run": False},
        },
        "incident": {
            "schema_version": "hackme.campaign-watchdog.v1",
            "incident_id": "watchdog-test",
            "reason": "HEARTBEAT_STALE",
            "credential_material_collected": False,
        },
        "artifacts": [artifact],
    }


def test_complete_real_evidence_can_become_candidate() -> None:
    result = assess_e2e_evidence(_passing_evidence(), real_external_execution=True)

    assert result["status"] == "PASS_CANDIDATE"
    assert result["verification_scope"] == "end_to_end"
    assert result["machine_verified"] is True
    assert result["formal_gate_candidate"] is True
    assert result["failed_assertions"] == []
    assert set(result["assertions"]) == set(REQUIRED_ASSERTIONS)
    assert all(row["status"] == "PASS" and row["evidence"] for row in result["assertions"].values())


def test_component_assessment_never_claims_a_formal_gate() -> None:
    result = assess_e2e_evidence(_passing_evidence())

    assert result["status"] == "PARTIAL_PASS"
    assert result["verification_scope"] == "component_only"
    assert result["machine_verified"] is False
    assert result["formal_gate_candidate"] is False


@pytest.mark.parametrize(
    ("mutate", "failed_assertion"),
    [
        (lambda value: value["source"].update(worktree_clean=False), "source_at_commit_clean"),
        (lambda value: value["cgroup"]["limits"]["checks"]["memory.max"].update(actual=9), "exact_cgroup_limits"),
        (lambda value: value["processes"]["watchdog"]["placement"].update(inside_campaign_scope=True), "watchdog_outside_scope"),
        (lambda value: value["timings"].update(stale_observed_seconds=119.999), "stale_timeout_120_observed"),
        (lambda value: value["state"]["final"]["control"].update(admit_new_jobs=True), "admission_closed"),
        (lambda value: value["state"]["final"]["clock"].update(continuous_active_seconds=3.0), "continuous_time_stopped"),
        (lambda value: value["cgroup"].update(after_pids=[42002]), "cgroup_empty_after"),
        (lambda value: value["artifacts"][0].update(sha256="not-a-hash"), "artifact_hashes_valid"),
    ],
)
def test_any_missing_proof_fails_closed(mutate, failed_assertion: str) -> None:
    evidence = _passing_evidence()
    mutate(evidence)

    result = assess_e2e_evidence(evidence, real_external_execution=True)

    assert result["status"] == "FAIL_HARNESS"
    assert result["formal_gate_candidate"] is False
    assert failed_assertion in result["failed_assertions"]


def test_campaign_root_must_be_strictly_below_tmp(tmp_path: Path) -> None:
    assert validate_campaign_root(tmp_path / "campaign").is_relative_to(Path("/tmp"))
    with pytest.raises(SigstopE2EError, match="strictly below /tmp"):
        validate_campaign_root(Path("/tmp"))
    with pytest.raises(SigstopE2EError, match="strictly below /tmp"):
        validate_campaign_root(Path.home() / "not-a-campaign")


@pytest.mark.parametrize("forbidden", ["--stale-after-seconds", "--development-mode"])
def test_cli_exposes_no_shortened_or_development_mode(tmp_path: Path, forbidden: str) -> None:
    extra = [forbidden, "1"] if forbidden == "--stale-after-seconds" else [forbidden]
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "--campaign-root", str(tmp_path / "campaign"),
            "--expected-commit", COMMIT,
            *extra,
        ])


def test_wait_accepts_durable_terminal_artifact_after_process_exit(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"incident_id": "done", "reason": "HEARTBEAT_STALE"}), encoding="utf-8")

    class ExitedProcess:
        returncode = INCIDENT_EXIT_CODE

        @staticmethod
        def poll() -> int:
            return INCIDENT_EXIT_CODE

    payload = _wait_for_json(
        path,
        lambda value: value.get("incident_id") == "done",
        timeout=0.1,
        label="terminal watchdog evidence",
        process=ExitedProcess(),  # type: ignore[arg-type]
    )

    assert payload["reason"] == "HEARTBEAT_STALE"


def test_cli_rejects_non_tmp_root_before_running(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([
        "--campaign-root", str(Path.home() / "watchdog-test"),
        "--expected-commit", COMMIT,
    ]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "FAIL_HARNESS"
    assert payload["formal_gate_candidate"] is False


def test_artifact_record_reparses_json_and_records_hash(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    path = root / "artifacts" / "evidence.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": "example.v1", "ok": True}), encoding="utf-8")

    record = _artifact_record(path, campaign_root=root, artifact_id="evidence")

    assert record["validated"] is True
    assert record["schema_version"] == "example.v1"
    assert record["size"] > 0
    assert len(record["sha256"]) == 64


def test_artifact_record_rejects_empty_and_non_object_json(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    path = root / "artifacts" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    with pytest.raises(SigstopE2EError, match="empty"):
        _artifact_record(path, campaign_root=root, artifact_id="bad")

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SigstopE2EError, match="object"):
        _artifact_record(path, campaign_root=root, artifact_id="bad")
