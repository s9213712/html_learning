"""Positive-path coverage contracts for synchronized system/soak probes."""

from __future__ import annotations


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
