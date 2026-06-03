"""Request guard and feature-gate helpers for ``server.py``.

The Flask decorators remain in ``server.py`` so hook order stays explicit.
This module owns the underlying guard logic and routing decisions.
"""

import os
import re

_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_truthy(name):
    return str(os.environ.get(name, "")).strip().lower() in _TRUTHY_ENV_VALUES


def _runtime_mode_from_env():
    mode = (
        os.environ.get("HACKME_DEV_SERVER_MODE")
        or os.environ.get("HTML_LEARNING_SERVER_MODE")
        or os.environ.get("SERVER_MODE")
        or "dev_ready"
    )
    return str(mode).strip().lower()


def _using_builtin_default_account_passwords():
    return (
        os.environ.get("HTML_LEARNING_ROOT_PASSWORD", "").strip() == "root"
        and os.environ.get("HTML_LEARNING_MANAGER_PASSWORD", "").strip() == "admin"
        and os.environ.get("HTML_LEARNING_TEST_PASSWORD", "").strip() == "test"
    )


def _running_from_isolated_runtime_copy():
    candidates = (
        os.environ.get("HACKME_RUNTIME_DIR", ""),
        os.environ.get("HTML_LEARNING_DB_DIR", ""),
        os.environ.get("HTML_LEARNING_LOG_DIR", ""),
        os.getcwd(),
        __file__,
    )
    return any("/tmp/hackme_web_isolated_" in str(value or "") for value in candidates)


def default_password_change_policy_disabled():
    disabled_values = (
        os.environ.get("HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_POLICY", ""),
        os.environ.get("HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_CHANGE", ""),
    )
    if any(str(value).strip().lower() in _TRUTHY_ENV_VALUES for value in disabled_values):
        return True
    security_enabled = _env_truthy("HACKME_DEV_SECURITY_ENABLED") or _env_truthy("HTML_LEARNING_SECURITY_ENABLED")
    dev_like_mode = _runtime_mode_from_env() in {"dev_ready", "internal_test", "test", "superweak"}
    default_passwords_allowed = _env_truthy("HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS") or _env_truthy(
        "HTML_LEARNING_ALLOW_DEFAULT_PASSWORDS"
    )
    isolated_default_passwords = _running_from_isolated_runtime_copy() and _using_builtin_default_account_passwords()
    return (default_passwords_allowed or isolated_default_passwords) and dev_like_mode and not security_enabled


def should_require_password_change_flag(value):
    return bool(value or 0) and not default_password_change_policy_disabled()


FEATURE_ROUTE_GATES = (
    ("feature_chat_enabled", ("/api/chat/", "/api/chat/rooms", "/api/chat/messages")),
    ("feature_community_enabled", ("/api/community/", "/api/community")),
    ("feature_appeals_enabled", ("/api/appeals", "/api/admin/appeals")),
    ("feature_reports_enabled", ("/api/reports", "/api/admin/reports", "/api/admin/message-reports", "/api/admin/community-post-reports")),
    ("feature_reports_notifications_enabled", ("/api/notifications",)),
    ("feature_audit_log_enabled", ("/api/admin/audit", "/api/audit")),
    ("feature_violation_center_enabled", ("/api/admin/violations", "/api/admin/users/")),
    ("feature_accounts_enabled", ("/api/admin/users",)),
    ("feature_system_health_enabled", ("/api/admin/health",)),
    ("feature_privacy_uploads_enabled", ("/api/files/", "/api/files", "/api/cloud-drive/", "/api/cloud-drive", "/api/root/announcement-attachment-requests", "/api/crypto/")),
    ("feature_storage_albums_enabled", ("/api/storage/", "/api/storage", "/api/admin/storage/", "/api/admin/storage")),
    ("feature_comfyui_enabled", ("/api/comfyui/", "/api/comfyui")),
    ("feature_videos_enabled", ("/api/videos/", "/api/videos")),
    ("feature_economy_enabled", ("/api/points/", "/api/points", "/api/admin/points/", "/api/admin/points", "/api/root/points/", "/api/root/points")),
    ("feature_trading_enabled", ("/api/trading/", "/api/trading", "/api/admin/trading/", "/api/admin/trading", "/api/root/trading/", "/api/root/trading")),
    ("feature_games_enabled", ("/api/games/", "/api/games", "/api/root/games/", "/api/root/games")),
)


def get_request_maintenance_bypass_token(request_obj):
    return request_obj.headers.get("X-Maintenance-Bypass-Token", "") or ""


def has_valid_maintenance_bypass(
    settings,
    *,
    request_obj,
    verify_maintenance_bypass_token,
):
    return verify_maintenance_bypass_token(
        get_request_maintenance_bypass_token(request_obj),
        settings.get("maintenance_bypass_token_hash", ""),
        settings.get("maintenance_bypass_token_expires_at", ""),
    )


def path_is_root_recovery_allowed_during_lockdown(path):
    allowed_exact = {
        "/api/csrf-token",
        "/api/logout",
        "/api/session/idle-timeout",
        "/api/me",
        "/api/version",
        "/api/site-config",
        "/api/captcha/challenge",
    }
    if path in allowed_exact:
        return True
    allowed_prefixes = (
        "/api/root/server-mode",
        "/api/root/incident/",
        "/api/admin/server-mode",
        "/api/admin/snapshots",
        "/api/admin/integrity",
        "/api/admin/health",
        "/api/admin/server-output",
        "/api/admin/server-log",
        "/api/admin/settings",
        "/api/admin/features",
        "/api/root/points-chain",
        "/api/root/storage/users",
    )
    return any(path == prefix or path.startswith(prefix) for prefix in allowed_prefixes)


def root_ip_is_allowed(settings, *, get_client_ip, client_ip_allowed):
    if not settings.get("root_ip_whitelist_enabled", False):
        return True
    return client_ip_allowed(get_client_ip(), settings.get("root_ip_whitelist", ""))


def feature_gate_for_path(path):
    if path.startswith("/api/admin/users/") and (
        path.endswith("/violation") or path.endswith("/reset-violations")
    ):
        return "feature_violation_center_enabled"
    if path.startswith("/api/admin/users"):
        return "feature_accounts_enabled"
    for key, prefixes in FEATURE_ROUTE_GATES:
        if any(path == prefix or path.startswith(prefix) for prefix in prefixes):
            return key
    return None


def protect_sensitive_static_page(
    request_obj,
    *,
    get_current_user_ctx,
    audit,
    get_client_ip,
    get_ua,
    is_feature_enabled,
    record_security_event,
    make_response,
):
    protected_pages = {
        "/trading-workflow-editor.html": {
            "feature": "feature_trading_enabled",
            "disabled_message": "Trading workflow editor is disabled",
        },
        "/comfyui-workflow-editor.html": {
            "feature": "feature_comfyui_enabled",
            "disabled_message": "ComfyUI workflow editor is disabled",
        },
    }
    page = protected_pages.get(request_obj.path)
    if request_obj.method == "OPTIONS" or not page:
        return None
    actor = get_current_user_ctx()
    if not actor:
        audit(
            "STATIC_PAGE_UNAUTH_DENIED",
            get_client_ip(),
            user="-",
            ua=get_ua(),
            success=False,
            detail=f"path={request_obj.path}",
        )
        resp = make_response("", 302)
        resp.headers["Location"] = "/"
        return resp
    feature = page["feature"]
    if not is_feature_enabled(feature):
        record_security_event(
            "feature_disabled",
            get_client_ip(),
            target_user=actor["username"],
            detail=f"path={request_obj.path},feature={feature}",
        )
        return make_response(page["disabled_message"], 503)
    return None


def enforce_root_ip_whitelist(
    request_obj,
    *,
    get_system_settings,
    normalize_text,
    get_current_user_ctx,
    root_ip_is_allowed_func,
    record_security_event,
    get_client_ip,
    json_resp,
):
    if request_obj.method == "OPTIONS" or not request_obj.path.startswith("/api"):
        return None
    settings = get_system_settings()
    if not settings.get("root_ip_whitelist_enabled", False):
        return None
    if request_obj.path == "/api/login":
        data = request_obj.get_json(silent=True) if request_obj.is_json else {}
        username = normalize_text(data.get("username")) if isinstance(data, dict) else ""
        if username == "root" and not root_ip_is_allowed_func(settings):
            record_security_event("permission_denied", get_client_ip(), target_user="root", detail="root_ip_whitelist_login")
            return json_resp({"ok": False, "msg": "root IP 不在允許清單內"}), 403
        return None
    actor = get_current_user_ctx()
    if actor and actor["username"] == "root" and not root_ip_is_allowed_func(settings):
        record_security_event("permission_denied", get_client_ip(), target_user="root", detail=f"root_ip_whitelist:path={request_obj.path}")
        return json_resp({"ok": False, "msg": "root IP 不在允許清單內"}), 403
    return None


def enforce_browser_only_mode(
    request_obj,
    *,
    get_system_settings,
    has_valid_maintenance_bypass_func,
    is_browser_user_agent,
    get_current_user_ctx,
    record_security_event,
    get_client_ip,
    maintenance_bypass_required_payload,
    json_resp,
):
    if request_obj.method == "OPTIONS" or not request_obj.path.startswith("/api"):
        return None
    settings = get_system_settings()
    if not settings.get("browser_only_mode_enabled", False):
        return None
    if has_valid_maintenance_bypass_func(settings):
        return None
    if request_obj.path in ("/api/version", "/api/site-config", "/api/captcha/challenge"):
        return None
    if is_browser_user_agent(request_obj.headers.get("User-Agent", "")):
        return None
    actor = get_current_user_ctx()
    record_security_event(
        "permission_denied",
        get_client_ip(),
        target_user=(actor["username"] if actor else "-"),
        detail=f"browser_only_mode:path={request_obj.path}",
    )
    return json_resp(
        maintenance_bypass_required_payload(
            "browser-only mode 已啟用，請使用瀏覽器存取；維護腳本可由 root 提供維護旁路 token。"
        )
    ), 403


def enforce_mode_restrictions(
    request_obj,
    *,
    get_system_settings,
    smv2_current_ctx,
    has_valid_maintenance_bypass_func,
    get_current_user_ctx,
    path_is_root_recovery_allowed_during_lockdown_func,
    revoke_user_sessions,
    audit,
    get_client_ip,
    maintenance_bypass_required_payload,
    json_resp,
    session_cookie_samesite,
    session_cookie_secure,
):
    if request_obj.method == "OPTIONS" or not request_obj.path.startswith("/api"):
        return None
    settings = get_system_settings()
    runtime_mode = smv2_current_ctx().mode
    mode_blocks_writes = runtime_mode in {"maintenance", "incident_lockdown"}
    if not mode_blocks_writes and not settings.get("maintenance_mode", False):
        return None
    if has_valid_maintenance_bypass_func(settings):
        return None
    if request_obj.path in ("/api/csrf-token", "/api/logout", "/api/session/idle-timeout", "/api/me", "/api/captcha/challenge"):
        return None
    if request_obj.path == "/api/login":
        data = request_obj.get_json(silent=True) if request_obj.is_json else {}
        username = str(data.get("username") or "").strip() if isinstance(data, dict) else ""
        if username == "root":
            return None
        if runtime_mode == "maintenance":
            return None
        return json_resp(
            maintenance_bypass_required_payload(
                "系統進入事故封鎖模式，僅允許最高管理者登入修復。"
                if runtime_mode == "incident_lockdown"
                else "系統進入緊急維護模式，僅允許最高管理者登入；維護腳本可由 root 提供維護旁路 token。"
            )
        ), 503

    actor = get_current_user_ctx()
    if actor and actor["username"] == "root":
        if runtime_mode == "incident_lockdown" and not path_is_root_recovery_allowed_during_lockdown_func(request_obj.path):
            return json_resp(
                {
                    "ok": False,
                    "msg": "事故封鎖模式中，root 只能操作修復、檢查、snapshot、server mode 與 incident API",
                    "server_mode": runtime_mode,
                }
            ), 503
        return None
    if actor and runtime_mode == "maintenance":
        if request_obj.method == "GET" and request_obj.path in {"/api/me", "/api/version", "/api/site-config", "/api/admin/health"}:
            return None
        return json_resp(
            {
                "ok": False,
                "msg": "系統維護中，非 root 帳號只能查看狀態，不能執行操作。",
                "server_mode": runtime_mode,
            }
        ), 503
    if actor:
        try:
            revoke_user_sessions(actor["id"])
            audit(
                "INCIDENT_FORCED_LOGOUT" if runtime_mode == "incident_lockdown" else "MAINTENANCE_FORCED_LOGOUT",
                get_client_ip(),
                user=actor["username"],
                success=True,
                detail=f"path={request_obj.path},mode={runtime_mode}",
            )
        except Exception:
            pass
        resp = json_resp(
            maintenance_bypass_required_payload(
                "系統進入事故封鎖模式，非 root 帳號已強制登出。"
                if runtime_mode == "incident_lockdown"
                else "系統進入緊急維護模式，非 root 帳號已強制登出。"
            ),
            503,
        )
        resp.delete_cookie("session_token", path="/", samesite=session_cookie_samesite, secure=session_cookie_secure)
        resp.delete_cookie("csrf_token", path="/", samesite=session_cookie_samesite, secure=session_cookie_secure)
        return resp
    return json_resp(
        maintenance_bypass_required_payload(
            "系統進入事故封鎖模式，請等待 root 修復。"
            if runtime_mode == "incident_lockdown"
            else "系統進入緊急維護模式，請等待最高管理者處理，或由 root 提供維護旁路 token。"
        ),
        503,
    )


def enforce_feature_flags(
    request_obj,
    *,
    feature_gate_for_path_func,
    is_feature_enabled,
    get_current_user_ctx,
    record_security_event,
    get_client_ip,
    build_feature_disabled_payload,
    json_resp,
):
    def actor_value(actor, key, default=None):
        if not actor:
            return default
        try:
            if isinstance(actor, dict):
                return actor.get(key, default)
            if hasattr(actor, "keys") and key in actor.keys():
                return actor[key]
        except Exception:
            pass
        return default

    def token_scope_denies(actor, feature_key):
        auth_scope = str(actor_value(actor, "_auth_scope", actor_value(actor, "auth_scope", "")) or "")
        if auth_scope != "internal_test_token":
            return False
        allowed_features = actor_value(actor, "_allowed_features", actor_value(actor, "allowed_features", []))
        if isinstance(allowed_features, str):
            allowed = {item.strip() for item in allowed_features.replace("\n", ",").split(",") if item.strip()}
        else:
            try:
                allowed = {str(item).strip() for item in allowed_features if str(item).strip()}
            except Exception:
                allowed = set()
        return bool(allowed) and feature_key not in allowed

    if request_obj.method == "OPTIONS" or not request_obj.path.startswith("/api"):
        return None
    if request_obj.path in (
        "/api/admin/settings",
        "/api/admin/features",
        "/api/site-config",
        "/api/csrf-token",
        "/api/captcha/challenge",
        "/api/me",
        "/api/login",
        "/api/logout",
        "/api/session/idle-timeout",
    ):
        return None
    feature_key = feature_gate_for_path_func(request_obj.path)
    if not feature_key:
        return None
    actor = None
    if is_feature_enabled(feature_key):
        request_cookies = getattr(request_obj, "cookies", {}) or {}
        if request_cookies.get("session_token"):
            actor = get_current_user_ctx()
            if actor and token_scope_denies(actor, feature_key):
                username = actor_value(actor, "username", "-")
                record_security_event(
                    "permission_denied",
                    get_client_ip(),
                    target_user=username,
                    detail=f"internal_test_token_feature_scope:path={request_obj.path},feature={feature_key}",
                )
                return json_resp({
                    "ok": False,
                    "msg": "內測登入 token 未開放此功能範圍",
                    "feature": feature_key,
                    "auth_scope": "internal_test_token",
                }), 403
        return None
    actor = get_current_user_ctx()
    if not actor:
        return json_resp({"ok": False, "msg": "未登入"}), 401
    if token_scope_denies(actor, feature_key):
        username = actor_value(actor, "username", "-")
        record_security_event(
            "permission_denied",
            get_client_ip(),
            target_user=username,
            detail=f"internal_test_token_feature_scope:path={request_obj.path},feature={feature_key}",
        )
        return json_resp({
            "ok": False,
            "msg": "內測登入 token 未開放此功能範圍",
            "feature": feature_key,
            "auth_scope": "internal_test_token",
        }), 403
    record_security_event(
        "feature_disabled",
        get_client_ip(),
        target_user=actor_value(actor, "username", "-"),
        detail=f"path={request_obj.path},feature={feature_key}",
    )
    return json_resp(build_feature_disabled_payload(feature_key), 503)


def enforce_required_password_change(request_obj, *, get_current_user_ctx, json_resp):
    if default_password_change_policy_disabled():
        return None
    if request_obj.method == "OPTIONS" or not request_obj.path.startswith("/api"):
        return None
    allowed = {
        "/api/csrf-token",
        "/api/logout",
        "/api/session/idle-timeout",
        "/api/me",
        "/api/version",
        "/api/site-config",
        "/api/password-strength",
        "/api/captcha/challenge",
    }
    if request_obj.path in allowed:
        return None
    actor = get_current_user_ctx()
    if not actor or not dict(actor).get("must_change_password"):
        return None
    match = re.fullmatch(r"/api/admin/users/(\d+)", request_obj.path or "")
    if request_obj.method in {"GET", "PUT"} and match and int(match.group(1)) == int(actor["id"]):
        return None
    return json_resp(
        {
            "ok": False,
            "msg": "此預設帳號初次登入必須先變更密碼",
            "must_change_password": True,
        }
    ), 403
