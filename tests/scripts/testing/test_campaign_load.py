from __future__ import annotations

import pytest

from scripts.testing.campaign_load import EffectiveLoadWindow, summarize_target_load


def window(**changes):
    values = {
        "window_started_at": "2026-07-12T00:00:00Z",
        "window_seconds": 60,
        "scheduled_load_level": 32,
        "active_workers": 30,
        "inflight_requests": 28,
        "operations_completed": 900,
        "expected_operations": 1000,
        "blocked_workers": 1,
        "idle_workers": 1,
        "queue_depth": 8,
        "retries": 5,
        "attempts": 1000,
        "baseline_32_operations_per_minute": 1000,
    }
    values.update(changes)
    return EffectiveLoadWindow(**values).evidence()


def test_concurrency_32_alone_does_not_count_as_target_load() -> None:
    result = window(active_workers=4, operations_completed=100, expected_operations=1000)

    assert result["scheduled_load_level"] == 32
    assert result["at_target_load"] is False
    assert "ACTIVE_WORKERS_BELOW_28" in result["target_failure_reasons"]
    assert "EFFECTIVE_LOAD_RATIO_BELOW_0_85" in result["target_failure_reasons"]


def test_effective_target_load_requires_workers_ratio_and_throughput() -> None:
    result = window()
    assert result["at_target_load"] is True
    assert result["effective_load_ratio"] == 0.9
    assert result["operations_per_minute"] == 900
    assert all(result["target_conditions"].values())


def test_policy_can_require_a_128_way_target_without_32_way_aliasing() -> None:
    result = window(
        scheduled_load_level=128,
        active_workers=109,
        inflight_requests=109,
        operations_completed=900,
        target_load_level=128,
        minimum_active_workers_at_target=109,
    )

    assert result["target_load_level"] == 128
    assert result["minimum_active_workers_at_target"] == 109
    assert result["target_conditions"]["scheduled_load_level_128"] is True
    assert result["target_conditions"]["active_workers_at_least_109"] is True
    assert result["at_target_load"] is True
    summary = summarize_target_load([result], target_load_level=128)
    assert summary["target_load_level"] == 128
    assert summary["ok"] is True


def test_maintenance_exclusion_requires_explicit_allowlisted_reason() -> None:
    result = window(
        active_workers=0,
        operations_completed=0,
        maintenance_window=True,
        maintenance_reason="PLANNED_RESTART",
    )
    assert result["at_target_load"] is False
    assert result["target_failure_reasons"] == []

    with pytest.raises(ValueError, match="allowed reason"):
        window(maintenance_window=True, maintenance_reason="because_idle")


def test_target_coverage_excludes_ramp_and_reviewed_maintenance_only() -> None:
    ramp = window(scheduled_load_level=16, active_workers=15, operations_completed=450, expected_operations=500, baseline_32_operations_per_minute=1000)
    target = [window() for _ in range(9)]
    failed = window(active_workers=10, operations_completed=200, degradation_reason="IO_PRESSURE")
    maintenance = window(active_workers=0, operations_completed=0, maintenance_window=True, maintenance_reason="BACKUP_RESTORE")
    summary = summarize_target_load([ramp, *target, failed, maintenance])

    assert summary["eligible_post_ramp_seconds"] == 600
    assert summary["target_load_seconds"] == 540
    assert summary["maintenance_seconds_excluded"] == 60
    assert summary["target_load_coverage"] == 0.9
    assert summary["ok"] is True


def test_unknown_or_empty_samples_fail_closed() -> None:
    assert summarize_target_load([])["ok"] is False
    result = summarize_target_load([{"sample_schema_version": "wrong", "window_seconds": 60}])
    assert result["ok"] is False
    assert result["invalid_samples"] == [0]
