from __future__ import annotations

from scripts.testing.operation_coverage import (
    ASYNC_TERMINAL_SUCCESS_REQUIRED,
    CAMPAIGN_SCENARIO_CONTRACTS,
    CONTINUOUS_FULL_FEATURE_DOMAINS,
    FORMAL_PASS_THRESHOLDS,
)


def test_formal_campaign_design_has_unique_ordered_schedule_and_evidence() -> None:
    fractions = [contract.scheduled_fraction for contract in CAMPAIGN_SCENARIO_CONTRACTS.values()]

    assert fractions == sorted(fractions)
    assert len(fractions) == len(set(fractions))
    assert fractions[0] > 0
    assert fractions[-1] < 1
    assert all(contract.required_evidence for contract in CAMPAIGN_SCENARIO_CONTRACTS.values())


def test_formal_campaign_design_covers_every_requested_complex_domain() -> None:
    evidence = set().union(*(contract.required_evidence for contract in CAMPAIGN_SCENARIO_CONTRACTS.values()))

    for required in (
        "post_restart_hls_share_continuity",
        "storage_share_realtime_proxy",
        "magnet_terminal_success",
        "torrent_file_terminal_success",
        "custom_workflow_create_import_run_output_delete",
        "custom_workflow_create_edit_backtest_enable_trade",
        "lending_margin_collateral_interest_liquidation",
        "governed_recovery_branch",
        "portable_full_runtime_archive",
        "incident_restrictions_effective",
        "chromium_firefox_webkit_desktop_mobile",
        "critical_touch_targets_minimum_44px",
    ):
        assert required in evidence


def test_continuous_soak_and_async_contracts_cannot_status_only_fake_success() -> None:
    assert "ai_agent_tools_planning_execution_and_ops_assistance" in CONTINUOUS_FULL_FEATURE_DOMAINS
    assert "trading_exchange_lending_margin_bots_workflows_reserve" in CONTINUOUS_FULL_FEATURE_DOMAINS
    assert {"bt_magnet", "bt_torrent", "hls_transcode", "comfyui_workflow", "trading_workflow"} <= ASYNC_TERMINAL_SUCCESS_REQUIRED


def test_formal_thresholds_require_a_real_continuous_day_and_zero_silent_failures() -> None:
    assert FORMAL_PASS_THRESHOLDS["continuous_active_seconds"] == 86_400
    assert FORMAL_PASS_THRESHOLDS["critical_touch_target_px"] == 44
    assert FORMAL_PASS_THRESHOLDS["minimum_resource_sample_ratio"] == 0.95
    assert FORMAL_PASS_THRESHOLDS["effective_target_load_coverage_ratio"] == 0.90
    assert FORMAL_PASS_THRESHOLDS["artifact_validation_ratio"] == 1.0
    assert FORMAL_PASS_THRESHOLDS["maximum_database_lock_count"] == 0
    assert FORMAL_PASS_THRESHOLDS["maximum_silent_failure_count"] == 0
