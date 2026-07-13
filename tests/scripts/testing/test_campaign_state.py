from __future__ import annotations

from pathlib import Path

import pytest

from scripts.testing.campaign_state import (
    CampaignState,
    CampaignStateError,
    CampaignStateMachine,
)


def machine(tmp_path: Path, *, required: int = 10) -> CampaignStateMachine:
    result = CampaignStateMachine(tmp_path / "campaign.state.json")
    result.initialize(
        campaign_uuid="campaign-test-001",
        required_active_seconds=required,
        orchestrator_pid=123,
        orchestrator_start_ticks=456,
    )
    return result


def freeze(result: CampaignStateMachine) -> None:
    result.transition(CampaignState.PREFLIGHT, reason="begin_preflight")
    result.mark_frozen(
        source={"verified": True, "digest": "abc"},
        containment={"verified": True, "cgroup": "/campaign.scope"},
    )


def test_state_machine_requires_machine_verified_freeze(tmp_path: Path) -> None:
    result = machine(tmp_path)
    result.transition(CampaignState.PREFLIGHT, reason="begin_preflight")

    with pytest.raises(CampaignStateError, match="source and containment"):
        result.mark_frozen(source={"verified": False}, containment={"verified": True})

    assert result.snapshot()["state"] == "PREFLIGHT"


def test_active_timer_counts_only_a_single_valid_segment(tmp_path: Path) -> None:
    result = machine(tmp_path)
    freeze(result)
    conditions = {
        "source_frozen": True,
        "primary_ready": True,
        "recovery_ready": True,
        "watchdog_alive": True,
        "monitor_alive": True,
        "load_generator_alive": True,
        "no_hard_stop": True,
        "campaign_state_active": True,
    }
    result.start_active(conditions, now_ns=1_000_000_000)
    state = result.tick_active(conditions, now_ns=4_000_000_000)

    assert state["clock"]["continuous_active_seconds"] == 3
    assert state["clock"]["formal_segment_valid"] is True

    invalid = dict(conditions, monitor_alive=False)
    state = result.tick_active(invalid, now_ns=5_000_000_000)
    assert state["clock"]["continuous_active_seconds"] == 3
    assert state["clock"]["invalid_seconds"] == 1
    assert state["clock"]["formal_segment_valid"] is False
    assert state["control"]["admit_new_jobs"] is False

    # A recovered condition cannot resume or patch the broken formal segment.
    state = result.tick_active(conditions, now_ns=9_000_000_000)
    assert state["clock"]["continuous_active_seconds"] == 3
    assert state["clock"]["invalid_seconds"] == 5


def test_hard_stop_atomically_stops_admission_before_cleanup(tmp_path: Path) -> None:
    result = machine(tmp_path)
    freeze(result)
    conditions = {"source_frozen": True, "primary_ready": True, "watchdog_alive": True}
    result.start_active(conditions, now_ns=1_000_000_000)
    result.tick_active(conditions, now_ns=6_000_000_000)

    state = result.hard_stop(
        reason_code="CGROUP_OOM_COUNTER_INCREASED",
        classification="FAIL_INFRA",
        evidence={"oom_kill_before": 0, "oom_kill_after": 1},
        now_ns=8_000_000_000,
    )

    assert state["state"] == "STOPPING_LOAD"
    assert state["control"] == {
        "admit_new_jobs": False,
        "load_generator_should_run": False,
        "preserve_evidence_requested": True,
    }
    assert state["clock"]["continuous_active_seconds"] == 5
    assert state["clock"]["active_finished_at"]
    assert state["hard_stop"]["reason_code"] == "CGROUP_OOM_COUNTER_INCREASED"

    with pytest.raises(CampaignStateError, match="cannot advance"):
        result.tick_active(conditions, now_ns=9_000_000_000)


def test_invalid_transition_is_rejected_without_corrupting_state(tmp_path: Path) -> None:
    result = machine(tmp_path)

    with pytest.raises(CampaignStateError, match="invalid campaign transition"):
        result.transition(CampaignState.PASS, reason="skip_everything")

    snapshot = result.snapshot()
    assert snapshot["state"] == "PREPARING"
    assert snapshot["revision"] == 1


def test_completion_requires_full_valid_duration_then_audit(tmp_path: Path) -> None:
    result = machine(tmp_path, required=5)
    freeze(result)
    conditions = {"all_required_processes_alive": True}
    result.start_active(conditions, now_ns=1_000_000_000)

    with pytest.raises(CampaignStateError, match="required continuous"):
        result.finish_active(now_ns=5_000_000_000)

    # The failed mutation is never committed; the same last tick remains.
    state = result.finish_active(now_ns=6_000_000_000)
    assert state["state"] == "COMPLETED"
    assert state["clock"]["continuous_active_seconds"] == 5
    state = result.transition(CampaignState.AUDITING, reason="validate_artifacts")
    assert state["state"] == "AUDITING"
    state = result.transition(CampaignState.PASS, reason="all_machine_gates_passed")
    assert state["state"] == "PASS"


def test_heartbeat_rejects_pid_reuse(tmp_path: Path) -> None:
    result = machine(tmp_path)

    result.heartbeat(
        orchestrator_pid=123,
        orchestrator_start_ticks=456,
        checkpoint_revision=1,
        now_ns=100,
    )
    with pytest.raises(CampaignStateError, match="starttime identity changed"):
        result.heartbeat(
            orchestrator_pid=123,
            orchestrator_start_ticks=999,
            checkpoint_revision=2,
            now_ns=200,
        )
