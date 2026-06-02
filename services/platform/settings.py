import os
import sqlite3
import threading
from datetime import datetime

from services.comfyui.settings import COMFYUI_DEFAULT_SETTINGS

SYSTEM_SETTINGS_TABLE = "system_settings"


def _env_int(name, default, *, minimum=None, maximum=None):
    try:
        value = int(os.environ.get(name, default))
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def _env_choice(name, default, choices):
    value = str(os.environ.get(name, default) or default).strip().lower()
    return value if value in set(choices) else default


DEFAULT_SETTINGS = {
    "audit_chain_enabled": True,
    "audit_chain_reseal_required": False,
    "ip_blocking_enabled": True,
    "max_login_fails_for_violation": 5,
    "login_violation_enabled": True,
    "rate_limit_violation_enabled": True,
    "maintenance_mode": False,
    "root_ip_whitelist_enabled": False,
    "root_ip_whitelist": "",
    "browser_only_mode_enabled": False,
    "production_single_ip_account_lock_enabled": False,
    "production_single_account_ip_lock_enabled": False,
    "maintenance_bypass_token_hash": "",
    "maintenance_bypass_token_expires_at": "",
    "internal_test_login_token_hash": "",
    "internal_test_login_token_expires_at": "",
    "internal_test_login_token_user_id": 0,
    "internal_test_login_token_username": "",
    "internal_test_login_token_allowed_features_json": "[]",
    "server_listen_host": "",
    "server_listen_port": 0,
    "server_ssl_enabled": True,
    "server_timezone": "UTC",
    "server_backpressure_enabled": True,
    "server_backpressure_mode": "auto",
    "server_backpressure_thread_capacity": 0,
    "server_backpressure_normal_limit": 0,
    "server_backpressure_heavy_limit": 0,
    "server_backpressure_root_priority_enabled": True,
    "server_backpressure_root_limit": 0,
    "server_backpressure_fast_lane_reserved": 0,
    "server_backpressure_retry_after_seconds": 2,
    "server_backpressure_refresh_seconds": 2,
    "server_backpressure_traffic_refresh_seconds": 4,
    "server_output_refresh_seconds": 3,
    "security_test_job_poll_seconds": 3,
    "system_resource_board_refresh_seconds": 5,
    "job_center_refresh_seconds": 3,
    "economy_dashboard_refresh_seconds": 30,
    "trading_dashboard_refresh_seconds": 5,
    "trading_live_price_refresh_seconds": 2,
    "trading_reference_price_refresh_seconds": 1,
    "trading_reference_chart_refresh_seconds": 5,
    "comfyui_job_poll_seconds": 1,
    "notification_poll_seconds": 60,
    "game_invite_poll_active_seconds": 5,
    "game_invite_poll_idle_seconds": 60,
    "game_invite_poll_hidden_seconds": 180,
    "server_connection_monitor_seconds": 15,
    "drive_dashboard_lazy_refresh_seconds": 10,
    "server_max_content_mb": 8192,
    "cloud_drive_storage_root": "",
    "cloud_drive_global_capacity_limit_mb": -1,
    "cloud_drive_transfer_limits_enabled": False,
    "cloud_drive_transfer_limits_json": '{"newbie":{"upload_kb_per_sec":256,"download_kb_per_sec":512,"priority":20},"normal":{"upload_kb_per_sec":512,"download_kb_per_sec":1024,"priority":40},"trusted":{"upload_kb_per_sec":2048,"download_kb_per_sec":4096,"priority":70},"vip":{"upload_kb_per_sec":8192,"download_kb_per_sec":16384,"priority":90},"restricted":{"upload_kb_per_sec":128,"download_kb_per_sec":256,"priority":10},"suspended":{"upload_kb_per_sec":0,"download_kb_per_sec":0,"priority":0}}',
    "remote_download_max_concurrent_global": _env_int("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL", 1, minimum=1, maximum=64),
    "remote_download_max_concurrent_per_user": _env_int("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER", 1, minimum=1, maximum=16),
    "bt_download_backend": _env_choice("HACKME_BT_BACKEND", "auto", {"auto", "transmission", "aria2"}),
    "transmission_rpc_url": os.environ.get("HACKME_TRANSMISSION_RPC_URL", "http://127.0.0.1:9091/transmission/rpc"),
    "transmission_rpc_username": os.environ.get("HACKME_TRANSMISSION_RPC_USERNAME", ""),
    "transmission_rpc_password": os.environ.get("HACKME_TRANSMISSION_RPC_PASSWORD", ""),
    "allow_register": True,
    "require_email_verification": False,
    "password_strength_policy_enabled": True,
    "password_reset_mode": "admin_review",
    "login_autofill_block_enabled": False,
    "max_manager_seats": 5,
    "points_admin_weekly_salary_enabled": True,
    "points_admin_weekly_salary_weekday": 1,
    "points_admin_weekly_salary_time": "09:00",
    "points_admin_weekly_salary_award_on_login": False,
    "notification_muted_types": "",
    "captcha_mode": "none",
    "captcha_ttl_seconds": 300,
    "captcha_turnstile_site_key": "",
    "max_login_failures": 3,
    "block_duration_minutes": 10,
    "session_ttl_hours": 4,
    "session_idle_timeout_minutes": 10,
    "site_theme_mode": "dark",
    "site_bg": "#11131d",
    "site_surface": "#1b2030",
    "site_accent": "#7a7bdc",
    "site_accent2": "#43b6a0",
    "site_text": "#eceef8",
    "site_muted": "#aeb8cc",
    "site_layout_mode": "centered",
    "site_density": "comfortable",
    "site_radius_px": 12,
    "site_font_scale": 1.0,
    "site_content_width": 1380,
    "site_font_family": "system",
    "site_background_style": "flat",
    "site_panel_style": "glass",
    "site_sidebar_width": "standard",
    "module_chat_min_role": "user",
    "module_profile_min_role": "user",
    "module_community_min_role": "user",
    "module_appeals_min_role": "user",
    "module_accounts_min_role": "manager",
    "module_comfyui_min_role": "user",
    "module_games_min_role": "user",
    "module_videos_min_role": "user",
    **COMFYUI_DEFAULT_SETTINGS,
    "chat_filter_rules_json": "",
    "feature_chat_enabled": False,
    "feature_community_enabled": False,
    "feature_accounts_enabled": True,
    "feature_appeals_enabled": False,
    "feature_audit_log_enabled": True,
    "feature_violation_center_enabled": True,
    "feature_reports_enabled": False,
    "feature_system_health_enabled": True,
    "feature_identity_governance_enabled": True,
    "feature_account_security_enabled": False,
    "feature_member_governance_enabled": True,
    "feature_server_modes_enabled": True,
    "feature_snapshot_restore_enabled": True,
    "snapshot_daily_auto_enabled": False,
    "snapshot_daily_time": "03:00",
    "snapshot_daily_last_date": "",
    "feature_health_center_enabled": True,
    "feature_forum_core_enabled": False,
    "feature_ui_rebuild_enabled": False,
    "feature_reports_notifications_enabled": True,
    "feature_attachments_enabled": False,
    "feature_storage_albums_enabled": False,
    "storage_maintenance_auto_enabled": False,
    "storage_maintenance_daily_time": "04:00",
    "storage_maintenance_last_date": "",
    "storage_trash_retention_days": 30,
    "feature_personalization_enabled": True,
    "feature_social_search_enabled": False,
    "feature_advanced_security_enabled": False,
    "feature_privacy_uploads_enabled": False,
    "feature_comfyui_enabled": False,
    # ComfyUI Template Importer rollout flags (§15)
    # - legacy_import_enabled: keep the old sanitize-only POST /api/comfyui/workflows/import
    #   path alive during migration. When false, that endpoint must require a preview_token
    #   minted by /api/comfyui/templates/preview.
    # - template_importer_strict: when true, /api/comfyui/workflows/<id>/run runs the §10
    #   5-gate. When false, the run handler stays on its legacy code path.
    "feature_comfyui_legacy_import_enabled": True,
    "feature_comfyui_template_importer_strict": False,
    "feature_economy_enabled": False,
    "feature_points_chain_enabled": True,
    "feature_trading_enabled": False,
    "feature_games_enabled": False,
    "feature_experiments_enabled": False,
    "feature_videos_enabled": False,
    "video_tip_fee_percent": 5,
    "video_tip_min_points": 1,
    "video_e2ee_derivatives_enabled": True,
    "video_e2ee_derivative_heights": "720,480",
    "video_e2ee_derivative_reject_larger_than_original": True,
    "video_e2ee_derivative_quota_exempt": True,
    "video_hls_cold_cleanup_enabled": True,
    "video_hls_protect_days_after_upload": 7,
    "video_hls_warm_plays_30d_below": 20,
    "video_hls_cold_plays_30d_below": 5,
    "video_hls_cold_no_play_days": 14,
    "video_hls_very_cold_no_play_days": 90,
    "video_hls_rebuild_plays_24h": 3,
    "video_hls_rebuild_plays_7d": 5,
    "video_hls_warm_keep_variants": "original,480p",
    "video_hls_mobile_floor_keep_variants": "480p",
    "video_hls_cold_cleanup_max_assets_per_run": 25,
    "integrity_guard_enabled": True,
    "integrity_guard_strict_mode": False,
    "security_pending_chat_reports_threshold": 10,
    "security_pending_appeals_threshold": 10,
    "security_pending_moderation_proposals_threshold": 10,
    "security_quarantined_files_threshold": 0,
    "security_unknown_encrypted_files_threshold": 50,
    "security_log_tail_lines": 200,
}

LEGACY_THEME_DEFAULT_REPLACEMENTS = {
    "site_bg": ("#0f0f1a", DEFAULT_SETTINGS["site_bg"]),
    "site_surface": ("#1a1a2e", DEFAULT_SETTINGS["site_surface"]),
    "site_accent": ("#6c63ff", DEFAULT_SETTINGS["site_accent"]),
    "site_accent2": ("#00d4aa", DEFAULT_SETTINGS["site_accent2"]),
    "site_text": ("#e0e0f0", DEFAULT_SETTINGS["site_text"]),
    "site_muted": ("#8888aa", DEFAULT_SETTINGS["site_muted"]),
}

FEATURE_FLAG_KEYS = tuple(key for key in DEFAULT_SETTINGS if key.startswith("feature_"))
FEATURE_SETTING_LABELS = {
    "feature_chat_enabled": "聊天室",
    "feature_community_enabled": "討論區 / 公告 / 留言",
    "feature_accounts_enabled": "帳號管理",
    "feature_appeals_enabled": "用戶申覆",
    "feature_audit_log_enabled": "Audit log 查詢",
    "feature_violation_center_enabled": "違規中心",
    "feature_reports_enabled": "檢舉審核",
    "feature_system_health_enabled": "系統健康燈",
    "feature_identity_governance_enabled": "身份治理欄位 / 會員等級",
    "feature_account_security_enabled": "帳號安全強化",
    "feature_member_governance_enabled": "會員治理與投票",
    "feature_server_modes_enabled": "伺服器模式",
    "feature_snapshot_restore_enabled": "Snapshot / Restore / Reset",
    "feature_health_center_enabled": "健康監控中心新版",
    "feature_forum_core_enabled": "論壇核心新版",
    "feature_ui_rebuild_enabled": "UI 架構重構",
    "feature_reports_notifications_enabled": "檢舉 / 申訴 / 通知新版",
    "feature_attachments_enabled": "附件 / 頭像 / CAPTCHA",
    "feature_storage_albums_enabled": "Storage / 相簿",
    "feature_videos_enabled": "影音分享",
    "feature_games_enabled": "遊戲區 / 西洋棋",
    "feature_experiments_enabled": "實驗區",
    "feature_comfyui_enabled": "ComfyUI AI 產圖",
    "feature_economy_enabled": "基本積分系統",
    "feature_points_chain_enabled": "PointsChain 私有鏈",
    "feature_trading_enabled": "積分交易所",
    "feature_personalization_enabled": "個人外觀覆寫",
    "feature_social_search_enabled": "社交 / 搜尋",
    "feature_advanced_security_enabled": "進階安全",
    "feature_privacy_uploads_enabled": "隱私分級上傳 / E2EE",
}
FEATURE_DEPENDENCY_RULES = {
    "feature_storage_albums_enabled": {
        "required": ("feature_privacy_uploads_enabled",),
        "description": "Storage / 相簿需要先有雲端硬碟父功能。",
    },
    "feature_trading_enabled": {
        "required": ("feature_economy_enabled", "feature_points_chain_enabled"),
        "description": "積分交易所必須依附在基本積分與 PointsChain 私有鏈上。",
    },
    "feature_points_chain_enabled": {
        "recommended": ("feature_economy_enabled",),
        "description": "PointsChain 私有鏈通常以基本積分系統作為站內財務入口。",
    },
    "feature_videos_enabled": {
        "recommended": ("feature_privacy_uploads_enabled", "feature_economy_enabled", "feature_points_chain_enabled"),
        "description": "影音若搭配雲端硬碟、基本積分與 PointsChain，才有上傳、保存與打賞等完整服務。",
    },
    "feature_comfyui_enabled": {
        "recommended": ("feature_privacy_uploads_enabled",),
        "description": "ComfyUI 若搭配雲端硬碟，可直接保存與分享產圖結果。",
    },
    "feature_chat_enabled": {
        "recommended": ("feature_attachments_enabled", "feature_reports_enabled", "feature_reports_notifications_enabled"),
        "description": "聊天室完整體驗通常會搭配附件、檢舉與通知。",
    },
    "feature_community_enabled": {
        "recommended": ("feature_attachments_enabled", "feature_reports_enabled", "feature_reports_notifications_enabled"),
        "description": "討論區完整體驗通常會搭配附件、檢舉與通知。",
    },
    "feature_appeals_enabled": {
        "recommended": ("feature_accounts_enabled", "feature_violation_center_enabled", "feature_reports_notifications_enabled"),
        "description": "申覆流程通常會搭配帳號管理、違規中心與通知。",
    },
    "feature_violation_center_enabled": {
        "recommended": ("feature_accounts_enabled",),
        "description": "違規中心通常和帳號管理一起使用。",
    },
    "feature_reports_enabled": {
        "recommended": ("feature_accounts_enabled", "feature_reports_notifications_enabled"),
        "description": "檢舉審核通常會搭配帳號管理與通知。",
    },
    "feature_reports_notifications_enabled": {
        "recommended": ("feature_accounts_enabled",),
        "description": "通知中心通常由帳號管理模組承載。",
    },
    "feature_identity_governance_enabled": {
        "recommended": ("feature_accounts_enabled",),
        "description": "身份治理欄位通常和帳號管理一起開。",
    },
    "feature_account_security_enabled": {
        "recommended": ("feature_accounts_enabled",),
        "description": "帳號安全強化通常和帳號管理一起開。",
    },
    "feature_member_governance_enabled": {
        "recommended": ("feature_accounts_enabled",),
        "description": "會員治理頁面通常掛在帳號管理底下。",
    },
}
MANAGEMENT_ONLY_FEATURE_FLAGS = frozenset({
    "feature_accounts_enabled",
    "feature_audit_log_enabled",
    "feature_violation_center_enabled",
    "feature_system_health_enabled",
    "feature_identity_governance_enabled",
    "feature_account_security_enabled",
    "feature_member_governance_enabled",
    "feature_server_modes_enabled",
    "feature_snapshot_restore_enabled",
    "feature_health_center_enabled",
    "feature_reports_notifications_enabled",
})
MANAGEMENT_ONLY_RESET_SETTINGS = {
    **{key: key in MANAGEMENT_ONLY_FEATURE_FLAGS for key in FEATURE_FLAG_KEYS},
    "audit_chain_enabled": True,
    "integrity_guard_enabled": True,
    "allow_register": False,
    "snapshot_daily_auto_enabled": False,
    "storage_maintenance_auto_enabled": False,
}

_SETTINGS_LOCK = threading.Lock()
_SYSTEM_SETTINGS = None
_STATE = {
    "get_db": None,
    "load_json": None,
    "settings_files": (),
}


class DangerousChangeBlocked(Exception):
    """Raised by save_settings when a risky change has no matching confirm.

    The route layer should catch this and return a structured 400 with the
    affected settings, their transition direction, and the warning text.
    """

    def __init__(self, risky):
        self.risky = list(risky or [])
        super().__init__(
            "dangerous settings require explicit confirmation: "
            + ", ".join(key for key, _danger, _transition in self.risky)
        )


def normalize_feature_key(feature_key):
    key = str(feature_key or "").strip()
    if not key:
        return ""
    if not key.startswith("feature_"):
        key = f"feature_{key.replace('_enabled', '')}_enabled"
    elif not key.endswith("_enabled"):
        key = f"{key}_enabled"
    return key


def get_feature_setting_label(feature_key):
    key = normalize_feature_key(feature_key)
    return FEATURE_SETTING_LABELS.get(key, key)


def get_feature_dependency_rule(feature_key):
    return FEATURE_DEPENDENCY_RULES.get(normalize_feature_key(feature_key), {})


def get_feature_dependency_details(feature_key):
    key = normalize_feature_key(feature_key)
    rule = get_feature_dependency_rule(key)
    required = tuple(rule.get("required", ()) or ())
    recommended = tuple(rule.get("recommended", ()) or ())
    missing_required = tuple(dep for dep in required if not is_feature_enabled(dep))
    missing_recommended = tuple(dep for dep in recommended if not is_feature_enabled(dep))
    enabled_dependents_required = []
    enabled_dependents_recommended = []
    for candidate, candidate_rule in FEATURE_DEPENDENCY_RULES.items():
        candidate_required = tuple(candidate_rule.get("required", ()) or ())
        candidate_recommended = tuple(candidate_rule.get("recommended", ()) or ())
        if key in candidate_required and is_feature_enabled(candidate):
            enabled_dependents_required.append(candidate)
        if key in candidate_recommended and is_feature_enabled(candidate):
            enabled_dependents_recommended.append(candidate)
    return {
        "feature": key,
        "feature_label": get_feature_setting_label(key),
        "description": rule.get("description", ""),
        "required": required,
        "recommended": recommended,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
        "enabled_dependents_required": tuple(enabled_dependents_required),
        "enabled_dependents_recommended": tuple(enabled_dependents_recommended),
    }


def find_feature_dependency_violations(current_settings, updates):
    if not isinstance(updates, dict):
        return []
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(current_settings, dict):
        merged.update(current_settings)
    merged.update({key: value for key, value in updates.items() if key in DEFAULT_SETTINGS})
    violations = []
    for child, rule in FEATURE_DEPENDENCY_RULES.items():
        if not bool(merged.get(child)):
            continue
        for parent in tuple(rule.get("required", ()) or ()):
            if not bool(merged.get(parent)):
                violations.append({
                    "feature": child,
                    "feature_label": get_feature_setting_label(child),
                    "required": parent,
                    "required_label": get_feature_setting_label(parent),
                })
    return violations


def build_feature_disabled_payload(feature_key):
    details = get_feature_dependency_details(feature_key)
    label = details["feature_label"]
    message = f"此功能目前已由 root 關閉：{label}"
    if details["missing_required"]:
        required_labels = "、".join(get_feature_setting_label(item) for item in details["missing_required"])
        message += f"。若要完整使用這個功能，還要先開啟：{required_labels}"
    else:
        if details["enabled_dependents_required"]:
            affected = "、".join(get_feature_setting_label(item) for item in details["enabled_dependents_required"])
            message += f"。目前已開啟且會被一起擋住的相依功能：{affected}"
        if details["enabled_dependents_recommended"]:
            affected = "、".join(get_feature_setting_label(item) for item in details["enabled_dependents_recommended"])
            message += f"。若要提供完整服務，通常也會一起開啟：{affected}"
    if details["missing_recommended"] and not details["missing_required"] and not details["enabled_dependents_recommended"]:
        recommended_labels = "、".join(get_feature_setting_label(item) for item in details["missing_recommended"])
        message += f"。若要提供完整服務，建議一併開啟：{recommended_labels}"
    payload = {"ok": False, "msg": message, "feature": details["feature"], "feature_label": label}
    if details["description"]:
        payload["feature_description"] = details["description"]
    if details["missing_required"]:
        payload["missing_required"] = list(details["missing_required"])
    if details["missing_recommended"]:
        payload["missing_recommended"] = list(details["missing_recommended"])
    if details["enabled_dependents_required"]:
        payload["enabled_dependents_required"] = list(details["enabled_dependents_required"])
    if details["enabled_dependents_recommended"]:
        payload["enabled_dependents_recommended"] = list(details["enabled_dependents_recommended"])
    return payload


def configure_settings_service(*, get_db, load_json, base_dir):
    _STATE.update({
        "get_db": get_db,
        "load_json": load_json,
        "settings_files": (
            os.path.join(base_dir, "system_settings.json"),
            os.path.join(base_dir, "settings.json"),
        ),
    })


def _coerce_setting_value(key, value):
    default = DEFAULT_SETTINGS.get(key)
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "y", "t"}
        if isinstance(value, int):
            return value != 0
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except Exception:
            return default
    if default is None:
        return value
    return str(value)


def init_system_settings_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            updated_by  TEXT
        )
        """
    )


def _load_settings_from_db(conn=None):
    close_conn = False
    if conn is None:
        conn = _STATE["get_db"]()
        close_conn = True
    try:
        try:
            rows = conn.execute(
                f"SELECT key, value FROM {SYSTEM_SETTINGS_TABLE}"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        values = {r["key"]: _coerce_setting_value(r["key"], r["value"]) for r in rows}
        return {**DEFAULT_SETTINGS, **values}
    finally:
        if close_conn:
            conn.close()


def _seed_missing_settings_to_db(conn):
    now = datetime.now().isoformat()
    existing = {r["key"] for r in conn.execute("SELECT key FROM system_settings").fetchall()}
    for key, default in DEFAULT_SETTINGS.items():
        if key not in existing:
            conn.execute(
                "INSERT INTO system_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                (key, str(default), now, "system")
            )
    for key, (legacy_value, replacement_value) in LEGACY_THEME_DEFAULT_REPLACEMENTS.items():
        conn.execute(
            """
            UPDATE system_settings
               SET value = ?, updated_at = ?, updated_by = ?
             WHERE key = ?
               AND value = ?
               AND COALESCE(updated_by, 'system') IN ('system', 'migration')
            """,
            (str(replacement_value), now, "system_theme_refresh", key, str(legacy_value))
        )


def _import_legacy_settings_files(conn):
    for path in _STATE["settings_files"]:
        data = _STATE["load_json"](path)
        if not isinstance(data, dict):
            continue
        for key in DEFAULT_SETTINGS:
            if key in data:
                conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                    (key, str(_coerce_setting_value(key, data[key])), datetime.now().isoformat(), "migration")
                )


def get_system_settings():
    global _SYSTEM_SETTINGS
    with _SETTINGS_LOCK:
        # Settings are security-relevant runtime controls. In a multi-worker
        # deployment, save_settings() only refreshes the worker that handled the
        # write, so returning a long-lived in-process cache can leave other
        # workers enforcing stale feature gates or maintenance flags.
        _SYSTEM_SETTINGS = _load_settings_from_db()
        return _SYSTEM_SETTINGS.copy()


def load_system_settings_from_db(conn=None):
    return _load_settings_from_db(conn)


def get_cached_system_setting(key, default=None):
    return get_system_settings().get(key, DEFAULT_SETTINGS.get(key, default))


def load_settings():
    return get_system_settings()


def refresh_system_settings():
    global _SYSTEM_SETTINGS
    with _SETTINGS_LOCK:
        _SYSTEM_SETTINGS = _load_settings_from_db()
        return _SYSTEM_SETTINGS


def _confirm_keys_from_payload(payload):
    if isinstance(payload, str):
        return {payload}
    if isinstance(payload, (list, tuple, set)):
        return {str(item) for item in payload if isinstance(item, str)}
    if isinstance(payload, dict):
        return {str(key) for key, value in payload.items() if value}
    return set()


def enforce_dangerous_confirm(current_settings, data):
    """Raise ``DangerousChangeBlocked`` if ``data`` flips a risky toggle
    without listing it in ``dangerous_confirm``.

    Internal callers (boot scripts, server-mode automation) skip this gate
    by simply not calling this function. The HTTP admin routes always call
    it before delegating to ``save_settings``.
    """
    if not isinstance(data, dict):
        return
    confirm_keys = _confirm_keys_from_payload(data.get("dangerous_confirm"))
    # Import here to avoid a circular import at module load.
    from services.platform.settings_metadata import find_dangerous_changes  # noqa: WPS433
    coerced = {}
    for key, value in data.items():
        if key not in DEFAULT_SETTINGS:
            continue
        coerced[key] = _coerce_setting_value(key, value)
    risky = [
        (key, danger, transition)
        for key, danger, transition in find_dangerous_changes(current_settings, coerced)
        if key not in confirm_keys
    ]
    if risky:
        raise DangerousChangeBlocked(risky)


def save_settings(data):
    updates = {}
    if not isinstance(data, dict):
        return {}
    current_settings = get_system_settings()
    for key, value in data.items():
        if key not in DEFAULT_SETTINGS:
            continue
        updates[key] = _coerce_setting_value(key, value)
    violations = find_feature_dependency_violations(current_settings, updates)
    if violations:
        first = violations[0]
        raise ValueError(f"{first['feature']} requires {first['required']}")
    if (
        "audit_chain_enabled" in updates
        and bool(updates["audit_chain_enabled"])
        and not bool(current_settings.get("audit_chain_enabled", False))
        and "audit_chain_reseal_required" not in updates
    ):
        updates["audit_chain_reseal_required"] = True
    if not updates:
        return {}

    conn = _STATE["get_db"]()
    try:
        init_system_settings_table(conn)
        now = datetime.now().isoformat()
        for key, value in updates.items():
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                (key, str(value), now, "admin")
            )
        conn.commit()
        _seed_missing_settings_to_db(conn)
    finally:
        conn.close()
    refresh_system_settings()
    return updates


def is_feature_enabled(feature_key):
    key = str(feature_key or "")
    if not key.startswith("feature_"):
        key = f"feature_{key}_enabled"
    settings = get_system_settings()
    default = DEFAULT_SETTINGS.get(key, False)
    return bool(settings.get(key, default))


def get_feature_settings():
    settings = get_system_settings()
    return {key: bool(settings.get(key, DEFAULT_SETTINGS[key])) for key in FEATURE_FLAG_KEYS}


def save_feature_settings(data):
    if not isinstance(data, dict):
        return {}
    updates = {key: value for key, value in data.items() if key in FEATURE_FLAG_KEYS}
    return save_settings(updates)
