"""Positive-path coverage contracts for synchronized system/soak probes."""

from __future__ import annotations

from dataclasses import dataclass


ACCOUNT_SUCCESS_REQUIRED_OPERATIONS = frozenset(
    {
        "version",
        "me",
        "profile",
        "friends",
        "notifications",
        "jobs",
        "drive_list",
        "video_list",
        "share_manage",
        "albums",
        "appeals",
        "hf_status",
        "trading_markets",
        "trading_dashboard",
        "trading_asset_overview",
        "trading_bots",
        "trading_grid_bots",
        "trading_workflows",
        "trading_grid_preview",
        "games_catalog",
        "chess_leaderboard",
        "community_boards",
        "community_announcements",
        "chat_rooms",
        "points_wallet",
        "points_ledger",
        "points_catalog",
        "points_governance",
        "ai_agent_status",
        "ai_agent_tools",
    }
)

GLOBAL_SUCCESS_REQUIRED_OPERATIONS = ACCOUNT_SUCCESS_REQUIRED_OPERATIONS | {
    "drive_upload",
    "drive_download",
    "resumable_start",
    "video_playback",
    "hls_master",
    "hf_generate",
}


@dataclass(frozen=True)
class CampaignScenarioContract:
    """Immutable pre-run design contract for one formal campaign scenario."""

    category: str
    scheduled_fraction: float
    required_evidence: frozenset[str]
    resource_class: str = "ordinary"


# This is the design authority for the formal campaign.  A scenario cannot be
# removed, skipped, or reduced to an HTTP-status-only check without changing a
# reviewed contract and its regression tests before the source freeze.
CAMPAIGN_SCENARIO_CONTRACTS: dict[str, CampaignScenarioContract] = {
    "media_long_hls_share": CampaignScenarioContract(
        category="long_video_upload_stream_hls_share",
        scheduled_fraction=0.01,
        resource_class="io_heavy",
        required_evidence=frozenset({
            "long_fixture_minimum_3600_seconds",
            "parallel_multi_account_upload",
            "hls_terminal_ready",
            "master_variant_segment_measurement",
            "dual_audio_and_subtitles",
            "desktop_mobile_random_seek",
            "password_wrong_password_and_revoke",
            "primary_planned_restart",
            "post_restart_hls_share_continuity",
        }),
    ),
    "cloud_drive_share_stream": CampaignScenarioContract(
        category="cloud_drive_share_stream_hls_realtime",
        scheduled_fraction=0.07,
        resource_class="io_heavy",
        required_evidence=frozenset({
            "cloud_video_upload",
            "cloud_stream_prepare_terminal_ready",
            "storage_share_password_unlock",
            "storage_share_master_variant_segment_subtitle",
            "storage_share_realtime_proxy",
            "storage_share_desktop_mobile_playback",
            "storage_share_revoke_denial",
        }),
    ),
    "bt_download_stream_restart": CampaignScenarioContract(
        category="bt_magnet_torrent_completion_stream_restart",
        scheduled_fraction=0.12,
        resource_class="io_heavy",
        required_evidence=frozenset({
            "controlled_local_seed",
            "magnet_terminal_success",
            "torrent_file_terminal_success",
            "download_content_hash",
            "pause_resume_progress",
            "service_restart_resume",
            "downloaded_video_preview_share_stream_hls",
        }),
    ),
    "ai_agent_positive_operations": CampaignScenarioContract(
        category="ai_agent_full_positive_operations_and_ops_assistance",
        scheduled_fraction=0.18,
        required_evidence=frozenset({
            "role_scoped_tool_catalog",
            "settings_snapshot_and_restore",
            "drive_share_create_update_revoke_delete",
            "video_hls_publish_and_terminal_job",
            "spot_margin_lending_bot_workflow_operations",
            "community_and_governance_operations",
            "launch_preflight_dry_run",
            "incident_enter_resolve_and_mode_restore",
            "scheduled_restart_outage_and_readiness",
            "write_audit_chain_verify",
        }),
    ),
    "comfyui_real_workflows": CampaignScenarioContract(
        category="comfyui_real_generation_official_and_custom_workflows",
        scheduled_fraction=0.27,
        resource_class="gpu_heavy",
        required_evidence=frozenset({
            "real_backend_required",
            "feature_probe",
            "official_templates_execute",
            "custom_workflow_create_import_run_output_delete",
            "ai_agent_generation_terminal_output",
            "desktop_mobile_workflow_ui",
            "offline_and_dependency_failure_visible",
        }),
    ),
    "trading_background_custom_workflow": CampaignScenarioContract(
        category="trading_lending_bots_background_and_custom_workflow",
        scheduled_fraction=0.35,
        required_evidence=frozenset({
            "spot_order_match_cancel_race",
            "lending_margin_collateral_interest_liquidation",
            "grid_dca_conditional_tp_sl",
            "background_worker_without_browser",
            "custom_workflow_create_edit_backtest_enable_trade",
            "custom_workflow_restart_persistence",
            "full_concurrent_stress_mode",
            "reserve_ledger_and_nonnegative_invariants",
        }),
    ),
    "pointschain_hft_invariants": CampaignScenarioContract(
        category="pointschain_high_frequency_branches_and_invariants",
        scheduled_fraction=0.43,
        required_evidence=frozenset({
            "high_frequency_transfer_and_trade",
            "idempotency_overspend_replay_rejection",
            "external_address_and_finality",
            "hash_chain_verify",
            "branch_and_dispute_api",
            "post_stress_desktop_mobile_ui",
        }),
    ),
    "wallet_incident_governance": CampaignScenarioContract(
        category="wallet_hack_social_and_chain_governance_recovery",
        scheduled_fraction=0.52,
        required_evidence=frozenset({
            "simulated_key_compromise_and_theft",
            "double_spend_and_replay_rejection",
            "wallet_freeze_and_risk_marker",
            "public_dispute_and_governance_votes",
            "append_only_compensation",
            "governed_recovery_branch",
        }),
    ),
    "backup_restore_restart": CampaignScenarioContract(
        category="snapshot_full_server_backup_restore_restart",
        scheduled_fraction=0.60,
        resource_class="io_heavy",
        required_evidence=frozenset({
            "server_snapshot_restore_boundary",
            "portable_full_runtime_archive",
            "storage_restore_and_live_finance_protection",
            "sqlite_quick_check_all_databases",
            "planned_restart_outage_and_readiness",
            "post_restart_state_and_chain_invariants",
        }),
    ),
    "server_emergency_incident": CampaignScenarioContract(
        category="server_emergency_incident_response_and_launch_readiness",
        scheduled_fraction=0.68,
        required_evidence=frozenset({
            "incident_enter",
            "incident_restrictions_effective",
            "diagnostics_integrity_and_repair",
            "incident_resolve",
            "server_mode_restore",
            "readiness_security_log_finance_chain_verify",
        }),
    ),
    "media_proxy_cross_browser": CampaignScenarioContract(
        category="realtime_proxy_cross_browser_audio_subtitle",
        scheduled_fraction=0.74,
        resource_class="browser_heavy",
        required_evidence=frozenset({
            "realtime_proxy_busy_disconnect_recovery",
            "http_concurrency_and_backpressure",
            "chromium_firefox_webkit_desktop_mobile",
            "audio_track_and_subtitle_switch",
            "chat_video_share_embed",
        }),
    ),
    "community_governance_operations": CampaignScenarioContract(
        category="community_chat_notifications_moderation_and_governance",
        scheduled_fraction=0.80,
        required_evidence=frozenset({
            "forum_thread_reply_report_moderate",
            "chat_private_message_and_notifications",
            "friends_profiles_and_blocking",
            "social_proposal_vote_execute",
            "role_permission_and_rate_limit_boundaries",
            "desktop_mobile_community_ui",
        }),
    ),
    "final_ui_mobile_prelaunch": CampaignScenarioContract(
        category="all_features_desktop_mobile_ux_and_prelaunch",
        scheduled_fraction=0.85,
        resource_class="browser_heavy",
        required_evidence=frozenset({
            "heuristic_member_behavior",
            "all_feature_navigation_under_load",
            "critical_touch_targets_minimum_44px",
            "no_clipping_overflow_or_hidden_cta",
            "no_console_network_or_silent_failure",
            "representative_desktop_mobile_screenshots",
            "whole_site_launch_gate",
            "final_db_log_chain_finance_and_pointschain_invariants",
        }),
    ),
}


CONTINUOUS_FULL_FEATURE_DOMAINS = frozenset({
    "auth_register_login_session_csrf",
    "server_admin_security_health_launch_checks",
    "member_levels_quotas_rate_limits_permissions",
    "cloud_drive_preview_share_e2ee_remote_download",
    "video_upload_publish_share_hls_realtime",
    "albums_and_password_shares",
    "forum_chat_private_messages_notifications",
    "profiles_friends_moderation_social_governance",
    "games_chess_and_solo_scores",
    "comfyui_generation_and_workflow_editor",
    "points_wallet_ledger_catalog_admin_adjustment",
    "trading_exchange_lending_margin_bots_workflows_reserve",
    "pointschain_branches_backup_governance_incidents",
    "ai_agent_tools_planning_execution_and_ops_assistance",
    "snapshot_full_backup_restore_restart_emergency",
    "desktop_mobile_cross_browser_usability",
})


ASYNC_TERMINAL_SUCCESS_REQUIRED = frozenset({
    "remote_download",
    "bt_magnet",
    "bt_torrent",
    "hls_transcode",
    "comfyui_generation",
    "comfyui_workflow",
    "ai_agent_job",
    "trading_bot",
    "trading_workflow",
    "backup_restore",
    "server_restart",
})


FORMAL_PASS_THRESHOLDS = {
    "continuous_active_seconds": 86_400,
    "mandatory_scenario_coverage_ratio": 1.0,
    "scenario_contract_pass_ratio": 1.0,
    "effective_target_load_coverage_ratio": 0.90,
    "ordinary_p95_ms": 3_000,
    "ordinary_p99_ms": 8_000,
    "sentinel_p95_ms": 3_000,
    "hls_playlist_p95_ms": 2_000,
    "hls_segment_p95_ms": 3_000,
    "ui_navigation_p95_ms": 5_000,
    "video_first_frame_p95_ms": 8_000,
    "random_seek_p95_ms": 5_000,
    "async_job_acceptance_p95_ms": 3_000,
    "critical_touch_target_px": 44,
    "minimum_resource_sample_ratio": 0.95,
    "maximum_database_lock_count": 0,
    "maximum_oom_count": 0,
    "maximum_unclassified_traceback_count": 0,
    "maximum_server_uncaught_traceback_count": 0,
    "maximum_silent_failure_count": 0,
    "maximum_unknown_or_stuck_async_jobs": 0,
    "maximum_financial_or_chain_invariant_violations": 0,
    "maximum_blocking_ui_defects": 0,
    "maximum_secret_findings": 0,
    "maximum_orphan_processes": 0,
    "artifact_validation_ratio": 1.0,
}
