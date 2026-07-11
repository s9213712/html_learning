"""Role and execution policy for AI Agent site actions.

The underlying site API remains the authorization source of truth. This module
controls which actions the AI planner may expose and under which operation mode
the AI action gateway may dispatch them.
"""

from __future__ import annotations


ROLE_RANK = {"user": 0, "manager": 1, "super_admin": 2}


def normalize_action_role(value):
    raw = str(value or "").strip().lower()
    if raw == "admin":
        return "manager"
    if raw in {"root", "super", "super_admin"}:
        return "super_admin"
    return raw if raw in ROLE_RANK else "user"


# These actions are constrained to the current session/user by their existing
# API endpoints and may run in assist mode after an explicit confirmation.
ASSIST_SAFE_USER_ACTIONS = frozenset({
    "write_community_create_thread",
    "write_community_reply_thread",
    "write_comfyui_generate",
    "write_comfyui_background_composite",
    "write_chess_create_practice",
    "write_chess_make_move",
    "write_chat_create_room",
    "write_chat_join_room",
    "write_chat_invite",
    "write_chat_send_message",
    "write_chat_edit_message",
    "write_chat_friend_request",
    "write_chat_friend_decide",
    "write_appeal_create",
    "write_cloud_drive_create_text",
    "write_cloud_drive_upload",
    "write_cloud_drive_text_update",
    "write_cloud_drive_remote_download",
    "write_remote_download_direct",
    "write_remote_download_bt",
    "write_remote_download_pause",
    "write_remote_download_resume",
    "write_remote_download_cancel",
    "write_remote_download_recover",
    "write_share_create",
    "write_share_update",
    "write_share_revoke",
    "write_task_cancel",
    "write_task_retry",
    "write_album_create",
    "write_album_update",
    "write_album_add_file",
    "write_album_remove_file",
    "write_album_smart_organize",
    "write_video_upload",
    "write_video_publish",
    "write_video_update",
    "write_transcode_hls",
    "write_hls_rebuild",
    "write_subtitle_upload",
    "write_comfyui_save_image",
    "write_comfyui_share_image",
    "write_comfyui_favorite_image",
    "write_comfyui_delete_favorite",
    "write_comfyui_workflow_run",
})


# These are valid user actions, but they can move funds or irreversibly remove
# data and therefore require the site-wide write mode in addition to confirm.
WRITE_MODE_USER_ACTIONS = frozenset({
    "write_chat_delete_message",
    "write_cloud_drive_delete",
    "write_album_delete",
    "write_video_delete",
    "write_trading_place_order",
    "write_trading_cancel_order",
    "write_trading_bot_create",
    "write_trading_bot_backtest",
    "write_trading_bot_scan",
    "write_trading_grid_preview",
    "write_trading_grid_bot_create",
    "write_trading_grid_bot_toggle",
    "write_trading_margin_open",
    "write_trading_margin_close",
    "write_trading_margin_add_collateral",
    "write_trading_margin_withdraw_collateral",
    "write_points_wallet_transfer",
})


MANAGER_ACTIONS = frozenset({
    "write_member_reward",
    "write_member_penalty",
    "write_community_reward",
    "write_community_penalty",
    "write_governance_proposal_create",
    "write_governance_vote",
    "write_governance_execute",
    "write_member_create_user",
    "write_member_update_user",
    "write_member_set_avatar_from_cloud",
    "write_bug_report_review",
    "write_appeal_review",
    "write_notification_send",
    "write_report_claim",
    "write_report_resolve",
    "write_user_review_registration",
    "write_user_block",
    "write_user_add_violation",
    "write_user_reset_violations",
    "write_moderation_note",
    "write_moderation_proposal_create",
    "write_moderation_proposal_vote",
    "write_moderation_proposal_execute",
    "write_community_thread_reward",
    "write_community_post_penalty",
    "write_points_governance_sponsor",
    "write_points_governance_cancel",
})


DESTRUCTIVE_ACTIONS = frozenset({
    "write_governance_execute",
    "write_emergency_governance_action",
    "write_chat_delete_message",
    "write_cloud_drive_delete",
    "write_album_delete",
    "write_video_delete",
    "write_user_block",
    "write_user_reset_violations",
    "write_moderation_proposal_execute",
    "write_moderation_proposal_override",
    "write_points_governance_execute",
    "write_server_restart",
    "write_server_mode_switch",
    "write_incident_enter",
    "write_incident_resolve",
    "write_storage_trash_purge",
    "write_comfyui_workflow_delete",
})


FINANCIAL_ACTIONS = frozenset({
    "write_trading_place_order",
    "write_trading_cancel_order",
    "write_trading_bot_create",
    "write_trading_grid_bot_create",
    "write_trading_grid_bot_toggle",
    "write_trading_margin_open",
    "write_trading_margin_close",
    "write_trading_margin_add_collateral",
    "write_trading_margin_withdraw_collateral",
    "write_points_wallet_transfer",
    "write_community_thread_reward",
    "write_community_post_penalty",
    "write_points_governance_execute",
})


def resolve_action_policy(tool_name, *, blueprint=None, write=False):
    name = str(tool_name or "").strip()
    details = blueprint if isinstance(blueprint, dict) else {}
    default_role = "super_admin" if write else "user"
    min_role = normalize_action_role(details.get("min_role") or default_role)
    assist_safe = False
    if name in ASSIST_SAFE_USER_ACTIONS:
        min_role = "user"
        assist_safe = True
    elif name in WRITE_MODE_USER_ACTIONS:
        min_role = "user"
    elif name in MANAGER_ACTIONS:
        min_role = "manager"

    risk_level = "low"
    if name in FINANCIAL_ACTIONS or name in DESTRUCTIVE_ACTIONS:
        risk_level = "high"
    elif write or name.startswith("write_"):
        risk_level = "medium"
    return {
        "name": name,
        "min_role": min_role,
        "assist_safe": bool(assist_safe),
        "root_only": min_role == "super_admin",
        "risk_level": risk_level,
        "write": bool(write),
        "requires_confirm": bool(write),
        "data_scope": str(details.get("data_scope") or ""),
    }


def role_allows_action(tool_name, actor_role, *, blueprint=None, write=False):
    policy = resolve_action_policy(tool_name, blueprint=blueprint, write=write)
    role = normalize_action_role(actor_role)
    return ROLE_RANK[role] >= ROLE_RANK[policy["min_role"]]


def evaluate_action_execution(tool_name, actor_role, operation_mode, *, blueprint=None, write=False):
    policy = resolve_action_policy(tool_name, blueprint=blueprint, write=write)
    role = normalize_action_role(actor_role)
    if ROLE_RANK[role] < ROLE_RANK[policy["min_role"]]:
        return {
            **policy,
            "allowed": False,
            "reason": "role_denied",
            "actor_role": role,
            "operation_mode": str(operation_mode or "readonly"),
        }
    if not write:
        return {
            **policy,
            "allowed": True,
            "reason": "read_action",
            "actor_role": role,
            "operation_mode": str(operation_mode or "readonly"),
        }
    mode = str(operation_mode or "readonly").strip().lower()
    allowed = mode == "write" or (mode == "assist" and policy["assist_safe"])
    return {
        **policy,
        "allowed": bool(allowed),
        "reason": "allowed" if allowed else "operation_mode_denied",
        "actor_role": role,
        "operation_mode": mode,
    }
