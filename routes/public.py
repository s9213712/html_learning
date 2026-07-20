import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import argon2
from flask import make_response, request, send_from_directory

from services.points_chain import BIRTHDAY_GIFT_POINTS
from services.points_chain.wallet_identity import (
    create_official_hot_wallet,
    signup_bonus_granted,
    system_account_wallet_onboarding_status,
    wallet_onboarding_status,
)
from services.storage.quota_purchases import grant_birthday_storage_quota
from services.security.access_controls import verify_internal_test_token
from services.server.request_guards import should_require_password_change_flag
from services.users.recovery import (
    create_password_reset_review_request,
    create_recovery_token,
    ensure_account_recovery_schema,
    lookup_valid_token,
    mark_token_used,
    normalize_email,
    queue_mail,
)
from services.security.captcha import create_captcha_challenge, normalize_captcha_mode, verify_captcha_response
from services.server.backpressure import backpressure_status
from services.platform.time_settings import normalize_server_timezone, server_time_payload
from services.users.profiles import (
    clear_profile_appearance,
    get_profile_appearance,
    get_profile_display_timezone,
    update_profile_appearance,
)


# Keep brute-force protection principal-scoped while allowing many legitimate
# members behind one office, carrier-grade NAT, reverse proxy, or load probe.
# Backpressure still independently caps concurrent Argon2 work.
LOGIN_IP_RATE_LIMIT_PER_MINUTE = 300
LOGIN_PRINCIPAL_RATE_LIMIT_PER_MINUTE = 30


def register_public_routes(app, deps):
    RESERVED_REGISTRATION_USERNAMES = {"root", "admin", "test", "system", "anonymous"}
    RESERVED_USERNAME_CONFUSABLE_TRANS = str.maketrans({
        "0": "o",
        "1": "l",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
    })
    CSRF_TOKEN_TTL = deps["CSRF_TOKEN_TTL"]
    PUBLIC_DIR = deps["PUBLIC_DIR"]
    ROLE_LABEL = deps["ROLE_LABEL"]
    SERVER_APP_NAME = deps.get("SERVER_APP_NAME", "hackme_web")
    SERVER_RELEASE_ID = deps.get("SERVER_RELEASE_ID", deps.get("SERVER_VERSION", "unknown"))
    SERVER_STARTED_AT = deps["SERVER_STARTED_AT"]
    SERVER_VERSION = deps["SERVER_VERSION"]
    SESSION_COOKIE_SAMESITE = deps["SESSION_COOKIE_SAMESITE"]
    SESSION_COOKIE_SECURE = deps["SESSION_COOKIE_SECURE"]
    SESSION_TTL = deps["SESSION_TTL"]
    audit = deps["audit"]
    db_delete_session = deps["db_delete_session"]
    db_get_user_from_token = deps["db_get_user_from_token"]
    db_save_session = deps["db_save_session"]
    delete_csrf_token = deps.get("delete_csrf_token", lambda token: None)
    delete_csrf_tokens_for_username = deps.get("delete_csrf_tokens_for_username", lambda username: None)
    decrypt_field = deps["decrypt_field"]
    encrypt_field = deps["encrypt_field"]
    ensure_user_official_room_membership = deps["ensure_user_official_room_membership"]
    get_auth_db = deps.get("get_auth_db", deps["get_db"])
    get_control_db = deps.get("get_control_db", deps["get_db"])
    get_client_ip = deps["get_client_ip"]
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_db = deps["get_db"]
    get_feature_settings = deps["get_feature_settings"]
    get_member_level_rule = deps.get("get_member_level_rule")
    get_cached_system_setting = deps.get("get_cached_system_setting")
    get_system_settings = deps["get_system_settings"]
    get_ua = deps["get_ua"]
    hash_password = deps["hash_password"]
    is_feature_enabled = deps["is_feature_enabled"]
    is_ip_blocked = deps["is_ip_blocked"]
    is_rate_limited = deps["is_rate_limited"]
    json_resp = deps["json_resp"]
    make_csrf_token = deps["make_csrf_token"]
    make_token = deps["make_token"]
    normalize_text = deps["normalize_text"]
    parse_birthdate = deps["parse_birthdate"]
    points_service = deps.get("points_service")
    record_login_failure = deps["record_login_failure"]
    record_login_attempt = deps.get("record_login_attempt")
    record_security_event = deps["record_security_event"]
    require_csrf = deps["require_csrf"]
    require_csrf_safe = deps["require_csrf_safe"]
    revoke_user_sessions = deps.get("revoke_user_sessions", lambda user_id: 0)
    score_password_strength = deps["score_password_strength"]
    store_csrf_token = deps["store_csrf_token"]
    timing_delay = deps["timing_delay"]
    validate_id_number = deps["validate_id_number"]
    validate_password = deps["validate_password"]
    enforce_password_strength = deps["enforce_password_strength"]
    validate_phone = deps["validate_phone"]
    verify_csrf_double_submit = deps["verify_csrf_double_submit"]
    verify_csrf_token = deps.get("verify_csrf_token", lambda token, username: False)
    verify_password = deps["verify_password"]

    def current_session_ttl_seconds():
        try:
            settings = get_system_settings() or {}
            hours = int(settings.get("session_ttl_hours") or 0)
            if hours > 0:
                return max(60, hours * 3600)
        except Exception:
            pass
        try:
            return max(60, int(SESSION_TTL or 0))
        except Exception:
            return 3600 * 4

    def current_csrf_token_ttl_seconds():
        try:
            return max(int(CSRF_TOKEN_TTL or 0), current_session_ttl_seconds())
        except Exception:
            return current_session_ttl_seconds()

    def password_strength_policy_enabled():
        try:
            value = (get_system_settings() or {}).get("password_strength_policy_enabled", True)
            if isinstance(value, str):
                return value.strip().lower() not in {"0", "false", "no", "off", ""}
            return bool(value)
        except Exception:
            return True

    def validate_configured_password(password):
        if not isinstance(password, str) or not password:
            return False, "密碼不可為空", score_password_strength(password or "")
        if len(password) > 128:
            return False, "密碼太長（最多 128 字元）", score_password_strength(password)
        strength = score_password_strength(password)
        if not password_strength_policy_enabled():
            return True, "OK", strength
        ok, msg = validate_password(password)
        if not ok:
            return False, msg, strength
        if is_feature_enabled("feature_account_security_enabled"):
            return enforce_password_strength(password, min_score=3)
        return True, "OK", strength

    def record_login_location(conn, user_id, username, ip, ua):
        ip_hash = hashlib.sha256((ip or "-").encode("utf-8")).hexdigest()
        previous = conn.execute(
            "SELECT 1 FROM login_locations WHERE user_id=? AND ip_hash<>? LIMIT 1",
            (user_id, ip_hash)
        ).fetchone()
        is_suspicious = 1 if previous else 0
        conn.execute(
            "INSERT INTO login_locations (user_id, ip_hash, country, city, login_at, is_suspicious) "
            "VALUES (?, ?, NULL, NULL, ?, ?)",
            (user_id, ip_hash, datetime.now().isoformat(), is_suspicious)
        )
        if is_suspicious:
            record_security_event(
                "login_location_suspicious",
                ip,
                target_user=username,
                detail=f"new_ip_hash={ip_hash[:12]},ua={ua[:80]}",
            )

    def server_local_date(settings):
        timezone_name = normalize_server_timezone((settings or {}).get("server_timezone"))
        try:
            zone = ZoneInfo(timezone_name or "UTC")
        except Exception:
            zone = ZoneInfo("UTC")
        return datetime.now(zone).date()

    def maybe_award_birthday_gift(user_row, settings, ip, ua):
        if not points_service or not user_row or user_row["username"] == "root":
            return None
        birthdate = parse_birthdate(decrypt_field(user_row["birthdate"] if "birthdate" in user_row.keys() else ""))
        if not birthdate:
            return None
        try:
            birthday = datetime.strptime(birthdate, "%Y-%m-%d").date()
            today = server_local_date(settings)
            if (birthday.month, birthday.day) != (today.month, today.day):
                return None
            result = points_service.award_birthday_gift(
                user_id=user_row["id"],
                birthday_year=today.year,
                birthday_date=today.isoformat(),
                actor={"id": user_row["id"], "username": user_row["username"], "role": user_row["role"]},
            )
            ledger = result.get("ledger") or {}
            storage_quota_gift = None
            try:
                quota_conn = get_db()
                try:
                    storage_quota_gift = grant_birthday_storage_quota(
                        quota_conn,
                        user_id=user_row["id"],
                        birthday_year=today.year,
                        ledger_uuid=ledger.get("ledger_uuid") or "",
                    )
                    quota_conn.commit()
                except Exception:
                    quota_conn.rollback()
                    raise
                finally:
                    quota_conn.close()
            except Exception as quota_exc:
                audit("STORAGE_BIRTHDAY_QUOTA_GIFT_FAILED", ip, user_row["username"], ua=ua, success=False, detail=str(quota_exc))
            return {
                "eligible": True,
                "created": bool(result.get("created")),
                "amount": int(ledger.get("amount") or BIRTHDAY_GIFT_POINTS),
                "year": int(today.year),
                "ledger_uuid": ledger.get("ledger_uuid") or "",
                "storage_quota_gift": storage_quota_gift,
            }
        except Exception as exc:
            audit("POINTS_BIRTHDAY_GIFT_FAILED", ip, user_row["username"], ua=ua, success=False, detail=str(exc))
            return {"eligible": True, "created": False, "error": "award_failed"}

    def generic_recovery_response():
        return json_resp({"ok": True, "msg": "如果資料符合，系統會寄出後續操作通知"})

    def reserved_registration_username(username):
        folded = unicodedata.normalize("NFKC", str(username or "")).strip().casefold()
        skeleton = folded.translate(RESERVED_USERNAME_CONFUSABLE_TRANS)
        return folded in RESERVED_REGISTRATION_USERNAMES or skeleton in RESERVED_REGISTRATION_USERNAMES

    def password_reset_mode():
        mode = str(get_system_settings().get("password_reset_mode") or "admin_review").strip().lower()
        return mode if mode in {"admin_review", "email_token"} else "admin_review"

    def password_reset_success_response(mode):
        if mode == "admin_review":
            return json_resp({"ok": True, "msg": "如果資料符合，系統會建立密碼重設審核申請"})
        return generic_recovery_response()

    def is_root_account_row(row):
        return bool(row and str(row.get("username") if isinstance(row, dict) else row["username"] or "").strip().lower() == "root")

    def current_server_mode(conn):
        try:
            control_conn = get_control_db()
            try:
                row = control_conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()
            finally:
                control_conn.close()
            mode = str(row["current_mode"] or "test").strip().lower() if row else "test"
            return "dev_ready" if mode == "preprod" else mode
        except Exception:
            try:
                row = conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()
                mode = str(row["current_mode"] or "test").strip().lower() if row else "test"
                return "dev_ready" if mode == "preprod" else mode
            except Exception:
                return "test"

    def login_autofill_block_enabled():
        return bool(get_system_settings().get("login_autofill_block_enabled", False))

    def wallet_onboarding_payload(user_id):
        if not points_service:
            return None
        app_conn = get_db()
        try:
            user_row = app_conn.execute("SELECT username FROM users WHERE id=?", (int(user_id),)).fetchone()
            is_root_account = bool(user_row and user_row["username"] == "root")
        except Exception:
            is_root_account = False
        finally:
            app_conn.close()
        conn = points_service.get_db() if hasattr(points_service, "get_db") else get_db()
        try:
            points_service.ensure_schema(conn)
            payload = wallet_onboarding_status(conn, points_service=points_service, user_id=user_id)
            if is_root_account:
                payload = system_account_wallet_onboarding_status(payload)
            conn.commit()
            return payload
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            audit("POINTS_WALLET_ONBOARDING_STATUS_FAILED", get_client_ip(), ua=get_ua(), success=False, detail=str(exc))
            return {"required": False, "error": "wallet_onboarding_status_failed"}
        finally:
            conn.close()

    def maybe_award_signup_bonus_for_login(user_row):
        if not points_service or not user_row or user_row["username"] == "root":
            return None
        conn = points_service.get_db() if hasattr(points_service, "get_db") else get_db()
        try:
            points_service.ensure_schema(conn)
            if signup_bonus_granted(conn, int(user_row["id"])):
                conn.commit()
                return {"ok": True, "created": False, "already_granted": True}
            create_official_hot_wallet(
                conn,
                user_id=int(user_row["id"]),
                chain_secret=getattr(points_service, "chain_secret", ""),
            )
            try:
                points_service.ensure_user_deposit_address(conn, int(user_row["id"]))
            except Exception:
                pass
            if hasattr(points_service, "award_signup_bonus_locked"):
                result = points_service.award_signup_bonus_locked(
                    conn,
                    user_id=int(user_row["id"]),
                    actor={"id": int(user_row["id"]), "username": user_row["username"], "role": user_row["role"]},
                )
                conn.commit()
                return result
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            audit("POINTS_SIGNUP_BONUS_LOGIN_AUTO_FAILED", get_client_ip(), user=user_row["username"], ua=get_ua(), success=False, detail=str(exc))
            return {"ok": False, "created": False, "error": str(exc)}
        finally:
            conn.close()
        try:
            return points_service.award_signup_bonus(
                user_id=int(user_row["id"]),
                actor={"id": int(user_row["id"]), "username": user_row["username"], "role": user_row["role"]},
            )
        except Exception as exc:
            audit("POINTS_SIGNUP_BONUS_LOGIN_AUTO_FAILED", get_client_ip(), user=user_row["username"], ua=get_ua(), success=False, detail=str(exc))
            return {"ok": False, "created": False, "error": str(exc)}

    def tester_token_login_allowed(conn, token, user_id):
        token = str(token or "").strip()
        if not token:
            return False
        try:
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            row = conn.execute(
                """
                SELECT 1
                FROM tester_tokens
                WHERE token_hash=?
                  AND tester_user_id=?
                  AND revoked_at IS NULL
                  AND expires_at>?
                LIMIT 1
                """,
                (token_hash, int(user_id), datetime.now().isoformat()),
            ).fetchone()
            return bool(row)
        except Exception:
            return False

    def internal_test_login_authorization(conn, settings, data, user_id):
        token = (
            str(data.get("internal_test_token") or "").strip()
            or str(data.get("login_token") or "").strip()
            or str(data.get("tester_token") or "").strip()
            or request.headers.get("X-Internal-Test-Token", "").strip()
            or request.headers.get("X-Tester-Token", "").strip()
        )
        if tester_token_login_allowed(conn, token, user_id):
            return {"ok": True, "scope": "tester_token", "allowed_features": []}
        if not verify_internal_test_token(
            token,
            settings.get("internal_test_login_token_hash", ""),
            settings.get("internal_test_login_token_expires_at", ""),
        ):
            return {"ok": False, "scope": "", "allowed_features": []}
        try:
            bound_user_id = int(settings.get("internal_test_login_token_user_id") or 0)
        except Exception:
            bound_user_id = 0
        if bound_user_id and int(user_id) != bound_user_id:
            return {"ok": False, "scope": "", "allowed_features": []}
        allowed_features = []
        try:
            parsed_features = json.loads(str(settings.get("internal_test_login_token_allowed_features_json") or "[]"))
            if isinstance(parsed_features, list):
                allowed_features = [str(item).strip() for item in parsed_features if str(item).strip()]
        except Exception:
            allowed_features = []
        return {"ok": True, "scope": "internal_test_token", "allowed_features": allowed_features}

    def production_login_conflict(main_conn, auth_conn, user_id, ip, now_iso, settings):
        if current_server_mode(main_conn) != "production":
            return None
        try:
            if bool(settings.get("production_single_ip_account_lock_enabled", False)):
                ip_conflict = auth_conn.execute(
                    """
                    SELECT s.user_id
                    FROM sessions s
                    WHERE COALESCE(s.is_revoked, 0)=0
                      AND s.expires_at>?
                      AND s.ip_address=?
                      AND s.user_id<>?
                    LIMIT 1
                    """,
                    (now_iso, ip, user_id),
                ).fetchone()
                if ip_conflict:
                    conflict_user_id = int(ip_conflict["user_id"])
                    conflict_user = main_conn.execute(
                        "SELECT username FROM users WHERE id=?",
                        (conflict_user_id,),
                    ).fetchone()
                    return {
                        "kind": "ip_reused_by_other_account",
                        "msg": "正式上線模式禁止同一 IP 同時登入多個帳號，請先登出原帳號",
                        "detail": f"active_user_id={conflict_user_id},active_username={(conflict_user['username'] if conflict_user else '-')}",
                    }
            if bool(settings.get("production_single_account_ip_lock_enabled", False)):
                account_conflict = auth_conn.execute(
                    """
                    SELECT ip_address
                    FROM sessions
                    WHERE user_id=?
                      AND COALESCE(is_revoked, 0)=0
                      AND expires_at>?
                      AND ip_address IS NOT NULL
                      AND ip_address<>?
                    LIMIT 1
                    """,
                    (user_id, now_iso, ip),
                ).fetchone()
                if account_conflict:
                    return {
                        "kind": "account_active_from_other_ip",
                        "msg": "正式上線模式禁止同一帳號同時從不同 IP 登入，請先登出其他裝置",
                        "detail": f"active_ip={account_conflict['ip_address']}",
                    }
        except Exception as exc:
            return {
                "kind": "session_policy_check_failed",
                "msg": "正式上線模式登入安全檢查失敗，請稍後再試",
                "detail": f"error={exc}",
            }
        return None

    def sync_official_room_membership(user_id):
        last_exc = None
        for _ in range(3):
            room_conn = get_db()
            try:
                ensure_user_official_room_membership(room_conn, user_id)
                room_conn.commit()
                return True
            except sqlite3.OperationalError as exc:
                last_exc = exc
                try:
                    room_conn.rollback()
                except Exception:
                    pass
                if "locked" not in str(exc).lower():
                    raise
                time.sleep(0.1)
            finally:
                room_conn.close()
        if last_exc is not None:
            audit("LOGIN_ROOM_MEMBERSHIP_FAILED", "-", detail=f"user_id={user_id};{last_exc}")
        return False

    def find_recovery_user(conn, identifier):
        ident = str(identifier or "").strip()
        if not ident:
            return None
        email = normalize_email(ident)
        if email:
            return conn.execute(
                "SELECT id, username, email, status, email_verified FROM users WHERE lower(email)=lower(?) LIMIT 1",
                (email,),
            ).fetchone()
        username = normalize_text(ident)
        if not username:
            return None
        return conn.execute(
            "SELECT id, username, email, status, email_verified FROM users WHERE username=? LIMIT 1",
            (username,),
        ).fetchone()

    def trim_password_history(conn, user_id, limit=5):
        conn.execute(
            "DELETE FROM user_passwords WHERE user_id=? AND id NOT IN ("
            "SELECT id FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT ?"
            ")",
            (user_id, user_id, int(limit)),
        )

    def ensure_public_account_columns(conn):
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        additions = (
            ("email", "TEXT"),
            ("email_verified", "INTEGER NOT NULL DEFAULT 0"),
            ("birthdate", "TEXT"),
            ("blocked_until", "TEXT"),
            ("failed_login_count", "INTEGER NOT NULL DEFAULT 0"),
            ("locked_until", "TEXT"),
            ("last_login_at", "TEXT"),
            ("password_strength_score", "INTEGER NOT NULL DEFAULT 0"),
            ("password_changed_at", "TEXT"),
            ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),
            ("is_default_password", "INTEGER NOT NULL DEFAULT 0"),
            ("signup_bonus_deferred", "INTEGER NOT NULL DEFAULT 0"),
            ("updated_at", "TEXT"),
        )
        for name, ddl in additions:
            if name not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
                cols.add(name)
        if "username" in cols:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_lower_username ON users(lower(username))")
        if "email" in cols:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_lower_email ON users(lower(email))")

    @app.route("/")
    @app.route("/videos")
    def index():
        with open(os.path.join(str(PUBLIC_DIR), "index.html"), "r", encoding="utf-8") as handle:
            html = handle.read()
        autofill_block = login_autofill_block_enabled()
        html = (
            html.replace("__ASSET_VERSION__", str(SERVER_RELEASE_ID))
            .replace("__LOGIN_AUTOFILL_BLOCK__", "1" if autofill_block else "0")
            .replace("__LOGIN_USER_AUTOCOMPLETE__", "off" if autofill_block else "username")
            .replace("__LOGIN_PASSWORD_AUTOCOMPLETE__", "off" if autofill_block else "current-password")
        )
        resp = make_response(html)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        tok = request.cookies.get("session_token")
        user = db_get_user_from_token(tok) if tok else None
        csrf_owner = user or "__public__"
        csrf_cookie = request.cookies.get("csrf_token", "")
        if not csrf_cookie or not verify_csrf_token(csrf_cookie, csrf_owner):
            csrf_cookie = make_csrf_token()
            store_csrf_token(csrf_cookie, csrf_owner)
            resp.set_cookie("csrf_token", csrf_cookie, max_age=current_csrf_token_ttl_seconds(),
                            httponly=False, samesite=SESSION_COOKIE_SAMESITE,
                            secure=SESSION_COOKIE_SECURE)
        return resp

    # ── GET CSRF token ─────────────────────────────────────────────────────────────
    @app.route("/api/csrf-token", methods=["GET"])
    def get_csrf_token():
        tok = request.cookies.get("session_token")
        username = db_get_user_from_token(tok) if tok else None
        owner = username or "__public__"
        token = request.cookies.get("csrf_token", "")
        if not token or not verify_csrf_token(token, owner):
            token = make_csrf_token()
            store_csrf_token(token, owner)
        resp = json_resp({"ok":True,"csrf_token":token})
        resp.set_cookie("csrf_token", token, max_age=current_csrf_token_ttl_seconds(),
                        httponly=False, samesite=SESSION_COOKIE_SAMESITE,
                        secure=SESSION_COOKIE_SECURE)
        return resp

    @app.route("/api/site-config", methods=["GET"])
    def get_site_config():
        settings = get_system_settings()
        features = get_feature_settings()
        conn = get_db()
        try:
            server_mode = current_server_mode(conn)
        finally:
            conn.close()
        return json_resp({
            "ok": True,
            "site_config": {
                "server_mode": server_mode,
                "site_bg": settings.get("site_bg"),
                "site_surface": settings.get("site_surface"),
                "site_accent": settings.get("site_accent"),
                "site_accent2": settings.get("site_accent2"),
                "site_text": settings.get("site_text"),
                "site_muted": settings.get("site_muted"),
                "site_layout_mode": settings.get("site_layout_mode"),
                "site_density": settings.get("site_density"),
                "site_radius_px": settings.get("site_radius_px"),
                "site_font_scale": settings.get("site_font_scale"),
                "site_content_width": settings.get("site_content_width"),
                "site_font_family": settings.get("site_font_family"),
                "site_background_style": settings.get("site_background_style"),
                "site_panel_style": settings.get("site_panel_style"),
                "site_sidebar_width": settings.get("site_sidebar_width"),
                "site_name": settings.get("site_name"),
                "site_document_title": settings.get("site_document_title"),
                "site_login_heading": settings.get("site_login_heading"),
                "site_login_subtitle": settings.get("site_login_subtitle"),
                "site_success_heading": settings.get("site_success_heading"),
                "site_success_message": settings.get("site_success_message"),
                "module_chat_min_role": settings.get("module_chat_min_role"),
                "module_profile_min_role": settings.get("module_profile_min_role"),
                "module_community_min_role": settings.get("module_community_min_role"),
                "module_appeals_min_role": settings.get("module_appeals_min_role"),
                "module_accounts_min_role": settings.get("module_accounts_min_role"),
                "module_comfyui_min_role": settings.get("module_comfyui_min_role"),
                "module_ai_agent_min_role": settings.get("module_ai_agent_min_role"),
                "module_games_min_role": settings.get("module_games_min_role"),
                "module_videos_min_role": settings.get("module_videos_min_role"),
                "password_strength_policy_enabled": bool(settings.get("password_strength_policy_enabled", True)),
                "password_reset_mode": settings.get("password_reset_mode", "admin_review"),
                "login_autofill_block_enabled": bool(settings.get("login_autofill_block_enabled", False)),
                "maintenance_mode": bool(settings.get("maintenance_mode", False)),
                "job_center_refresh_seconds": settings.get("job_center_refresh_seconds", 3),
                "economy_dashboard_refresh_seconds": settings.get("economy_dashboard_refresh_seconds", 30),
                "trading_dashboard_refresh_seconds": settings.get("trading_dashboard_refresh_seconds", 5),
                "trading_live_price_refresh_seconds": settings.get("trading_live_price_refresh_seconds", 2),
                "trading_reference_price_refresh_seconds": settings.get("trading_reference_price_refresh_seconds", 1),
                "trading_reference_chart_refresh_seconds": settings.get("trading_reference_chart_refresh_seconds", 5),
                "comfyui_job_poll_seconds": settings.get("comfyui_job_poll_seconds", 1),
                "notification_poll_seconds": settings.get("notification_poll_seconds", 60),
                "game_invite_poll_active_seconds": settings.get("game_invite_poll_active_seconds", 5),
                "game_invite_poll_idle_seconds": settings.get("game_invite_poll_idle_seconds", 60),
                "game_invite_poll_hidden_seconds": settings.get("game_invite_poll_hidden_seconds", 180),
                "server_connection_monitor_seconds": settings.get("server_connection_monitor_seconds", 15),
                "drive_dashboard_lazy_refresh_seconds": settings.get("drive_dashboard_lazy_refresh_seconds", 10),
                **features,
            },
            "server_meta": {
                "app": SERVER_APP_NAME,
                "release_id": SERVER_RELEASE_ID,
                "version": SERVER_VERSION,
                "started_at": SERVER_STARTED_AT,
                "server_time": server_time_payload(settings),
            },
        })

    @app.route("/api/version", methods=["GET"])
    def get_version():
        maintenance_mode = (
            get_cached_system_setting("maintenance_mode", False)
            if callable(get_cached_system_setting)
            else False
        )
        settings = {
            "server_timezone": get_cached_system_setting("server_timezone", "UTC")
            if callable(get_cached_system_setting)
            else "UTC"
        }
        return json_resp({
            "ok": True,
            "app": SERVER_APP_NAME,
            "release_id": SERVER_RELEASE_ID,
            "version": SERVER_VERSION,
            "started_at": SERVER_STARTED_AT,
            "server_time": server_time_payload(settings),
            "maintenance_mode": bool(maintenance_mode),
        })

    @app.route("/livez", methods=["GET"])
    @app.route("/api/livez", methods=["GET"])
    @app.route("/healthz", methods=["GET"])
    @app.route("/api/healthz", methods=["GET"])
    def livez():
        return json_resp({
            "ok": True,
            "status": "live",
            "app": SERVER_APP_NAME,
            "release_id": SERVER_RELEASE_ID,
            "started_at": SERVER_STARTED_AT,
        })

    @app.route("/readyz", methods=["GET"])
    @app.route("/api/readyz", methods=["GET"])
    def readyz():
        started = time.perf_counter()
        db_ok = False
        db_error = ""
        try:
            with get_db() as conn:
                conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception as exc:
            db_error = str(exc)[:160]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        payload = {
            "ok": db_ok,
            "status": "ready" if db_ok else "not_ready",
            "app": SERVER_APP_NAME,
            "release_id": SERVER_RELEASE_ID,
            "started_at": SERVER_STARTED_AT,
            "server_time": server_time_payload({
                "server_timezone": get_cached_system_setting("server_timezone", "UTC")
                if callable(get_cached_system_setting)
                else "UTC"
            }),
            "checks": {
                "db": {
                    "ok": db_ok,
                    "elapsed_ms": elapsed_ms,
                    "error": db_error,
                },
            },
            "backpressure": backpressure_status(app),
        }
        return json_resp(payload), (200 if db_ok else 503)

    @app.route("/api/password-strength", methods=["POST"])
    def password_strength():
        ip = get_client_ip()
        blocked, info = is_rate_limited(ip, max_req=30, window_sec=60)
        if blocked:
            return json_resp({"ok": False, "msg": f"請求太頻繁（{info['limit']}次/分鐘）"}), 429
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        password = data.get("password", "") if isinstance(data, dict) and isinstance(data.get("password"), str) else ""
        result = score_password_strength(password)
        return json_resp({"ok": True, **result})

    @app.route("/api/captcha/challenge", methods=["GET"])
    def captcha_challenge():
        ip = get_client_ip()
        settings = get_system_settings()
        mode = normalize_captcha_mode(settings.get("captcha_mode"))
        if mode == "turnstile":
            return json_resp({
                "ok": True,
                "captcha": {
                    "required": True,
                    "mode": "turnstile",
                    "site_key": settings.get("captcha_turnstile_site_key", ""),
                },
            })
        conn = get_auth_db()
        try:
            challenge = create_captcha_challenge(
                conn,
                mode=mode,
                ttl_seconds=settings.get("captcha_ttl_seconds", 300),
                ip=ip,
            )
            conn.commit()
            return json_resp({"ok": True, "captcha": challenge})
        finally:
            conn.close()

    @app.route("/api/register", methods=["POST"])
    @require_csrf
    def register():
        ip, ua = get_client_ip(), get_ua()
        audit("REGISTER_ATTEMPT", ip, ua=ua)
        settings = get_system_settings()
        if not settings.get("allow_register", True):
            audit("REGISTER_DISABLED", ip, ua=ua, detail="allow_register=false")
            return json_resp({"ok":False,"msg":"目前暫停開放註冊"}), 403
        if is_ip_blocked(ip):
            audit("REGISTER_BLOCKED", ip, ua=ua, detail="IP locked")
            return json_resp({"ok":False,"msg":"IP 已被鎖定，請稍後再試"}), 429

        blocked, info = is_rate_limited(ip, max_req=10, window_sec=60)
        if blocked:
            audit("REGISTER_RATELIMIT", ip, ua=ua)
            return json_resp({"ok":False,"msg":f"請求太頻繁（{info['limit']}次/分鐘）"}), 429

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤", "field": "request_body"}), 400
        if not isinstance(data, dict):
            audit("REGISTER_EMPTY", ip, ua=ua)
            return json_resp({"ok": False, "msg": "註冊資料格式錯誤", "field": "request_body"}), 400

        conn = get_auth_db()
        try:
            captcha_ok, captcha_msg = verify_captcha_response(conn, settings, data, ip=ip)
            if captcha_ok:
                conn.commit()
            else:
                audit("REGISTER_CAPTCHA_FAIL", ip, ua=ua, success=False, detail=captcha_msg)
                return json_resp({"ok":False,"msg":captcha_msg}), 400
        finally:
            conn.close()

        username = normalize_text(data.get("username"))
        password = data.get("password","") if isinstance(data.get("password"), str) else ""
        password_confirm = data.get("password_confirm","") if isinstance(data.get("password_confirm"), str) else ""
        nickname = normalize_text(data.get("nickname"))
        real_name = normalize_text(data.get("real_name"))
        id_number = normalize_text(data.get("id_number"))
        birthdate = parse_birthdate(data.get("birthdate"))
        phone = normalize_text(data.get("phone"))
        email = normalize_email(data.get("email"))

        # Username validation
        if not username:        return json_resp({"ok":False,"msg":"帳號不可為空", "field": "username"}), 400
        if len(username) < 3:  return json_resp({"ok":False,"msg":"帳號至少需要 3 個字元", "field": "username"}), 400
        if len(username) > 32: return json_resp({"ok":False,"msg":"帳號最長 32 字元", "field": "username"}), 400
        if not re.fullmatch(r"[a-zA-Z0-9_\-]+", username):
            return json_resp({"ok":False,"msg":"帳號只能包含英文、數字、底線、減號", "field": "username"}), 400
        if reserved_registration_username(username):
            audit("REGISTER_RESERVED", ip, username, ua=ua, success=False)
            return json_resp({"ok": False, "msg": "此帳號為系統保留字", "field": "username"}), 400
        if not nickname:
            return json_resp({"ok":False,"msg":"暱稱不可為空", "field": "nickname"}), 400
        if data.get("email") and not email:
            return json_resp({"ok":False,"msg":"Email 格式錯誤", "field": "email"}), 400
        if id_number and not validate_id_number(id_number):
            return json_resp({"ok":False,"msg":"身分證格式錯誤", "field": "id_number"}), 400
        if data.get("birthdate") and not birthdate:
            return json_resp({"ok":False,"msg":"生日需為 YYYY-MM-DD", "field": "birthdate"}), 400
        if phone and not validate_phone(phone):
            return json_resp({"ok":False,"msg":"電話格式錯誤", "field": "phone"}), 400
        if password != password_confirm:
            return json_resp({"ok":False,"msg":"兩次輸入的密碼不一致", "field": "password_confirm"}), 400

        ok, msg, strength = validate_configured_password(password)
        if not ok:
            audit("REGISTER_BAD_PW", ip, username, ua=ua, detail=msg)
            return json_resp({"ok":False,"msg":msg, "field": "password", "password_strength": strength}), 400

        conn = get_db()
        try:
            ensure_public_account_columns(conn)
            existing = conn.execute("SELECT 1 FROM users WHERE lower(username)=lower(?)",(username,)).fetchone()
            if existing:
                timing_delay()
                audit("REGISTER_DUP", ip, username, ua=ua, success=False)
                # Return generic — don't reveal account exists
                return json_resp({"ok":False,"msg":"註冊失敗，請稍後再試"}), 409

            # Always do timing-delay to normalize response time (Timing Oracle mitigation)
            timing_delay()

            now = datetime.now().isoformat()
            cur = conn.execute(
                "INSERT INTO users (username, email, nickname, real_name, birthdate, id_number, phone, status, role, member_level, base_level, effective_level, password_strength_score, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'user', 'newbie', 'newbie', 'newbie', ?, ?, ?)",
                (username, email or None, encrypt_field(nickname), encrypt_field(real_name), encrypt_field(birthdate), encrypt_field(id_number), encrypt_field(phone), strength["score"], now, now)
            )
            conn.execute(
                "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
                (cur.lastrowid, hash_password(password), now)
            )
            new_user_id = int(cur.lastrowid)
            conn.commit()
            hot_wallet_address = ""
            deposit_address = ""
            signup_bonus = None
            if points_service is not None:
                points_conn = points_service.get_db() if hasattr(points_service, "get_db") else get_db()
                try:
                    points_service.ensure_schema(points_conn)
                    hot_wallet = create_official_hot_wallet(
                        points_conn,
                        user_id=new_user_id,
                        chain_secret=getattr(points_service, "chain_secret", ""),
                    )
                    hot_wallet_address = str((hot_wallet or {}).get("address") or "")
                    try:
                        deposit = points_service.ensure_user_deposit_address(points_conn, new_user_id)
                        deposit_address = str((deposit or {}).get("address") or "")
                    except Exception:
                        deposit_address = ""
                    if hasattr(points_service, "award_signup_bonus_locked"):
                        signup_bonus = points_service.award_signup_bonus_locked(
                            points_conn,
                            user_id=new_user_id,
                            actor={"id": new_user_id, "username": username, "role": "user"},
                        )
                    points_conn.commit()
                except Exception as exc:
                    try:
                        points_conn.rollback()
                    except Exception:
                        pass
                    signup_bonus = {"created": False, "error": str(exc)}
                    audit("POINTS_REGISTER_WALLET_INIT_FAILED", ip, username, ua=ua, success=False, detail=str(exc))
                finally:
                    points_conn.close()
            signup_bonus_deferred = bool((signup_bonus or {}).get("error"))
            if signup_bonus_deferred:
                conn.execute(
                    "UPDATE users SET signup_bonus_deferred=1, updated_at=? WHERE id=?",
                    (datetime.now().isoformat(), new_user_id),
                )
                conn.commit()
            audit("REGISTER_PENDING", ip, username, ua=ua, success=True, detail="awaiting manager approval")
            signup_bonus_msg = (
                "註冊申請已送出，需經管理員或最高管理者審核後才能登入；註冊禮暫時未完成，審核通過後首次登入會自動補發。"
                if signup_bonus_deferred
                else "註冊申請已送出，需經管理員或最高管理者審核後才能登入；站內託管錢包與註冊禮已建立，審核通過後可直接使用站內付款與交易所。"
            )
            return json_resp({
                "ok": True,
                "msg": signup_bonus_msg,
                "wallet_onboarding_required": False,
                "signup_bonus_deferred": signup_bonus_deferred,
                "signup_bonus": {
                    "created": bool((signup_bonus or {}).get("created")),
                    "ledger_hash": ((signup_bonus or {}).get("ledger") or {}).get("ledger_hash", ""),
                    "wallet_address": hot_wallet_address,
                    "deferred_reason": "points_initialization_failed" if signup_bonus_deferred else "",
                },
                "official_hot_wallet_address": hot_wallet_address,
                "deposit_address": deposit_address,
            })
        finally:
            conn.close()

    @app.route("/api/login", methods=["POST"])
    @require_csrf
    def login():
        ip, ua = get_client_ip(), get_ua()
        settings = get_system_settings()

        # Check blocked BEFORE anything else
        if is_ip_blocked(ip):
            audit("LOGIN_BLOCKED", ip, ua=ua, detail="IP locked")
            timing_delay()  # still do delay so blocked vs not-blocked timing looks same
            return json_resp({"ok":False,"msg":"登入失敗（帳號或密碼錯誤）"}), 429

        blocked, info = is_rate_limited(
            (ip, "login_ip"),
            max_req=LOGIN_IP_RATE_LIMIT_PER_MINUTE,
            window_sec=60,
        )
        if blocked:
            audit("LOGIN_RATELIMIT", ip, ua=ua)
            timing_delay()
            retry_after = max(1, int(info.get("retry_after_seconds") or 1))
            response = json_resp({
                "ok": False,
                "msg": "登入請求太頻繁，請稍後再試",
                "error": "login_rate_limited",
                "scope": "source",
                "retry_after_seconds": retry_after,
            }, 429)
            response.headers["Retry-After"] = str(retry_after)
            return response

        try:
            data = request.get_json(force=True)
        except Exception:
            record_login_failure(ip, ua=ua, detail="invalid_json")
            timing_delay()
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            record_login_failure(ip, ua=ua, detail="json_not_object")
            timing_delay()
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400

        username = (data.get("username","") if isinstance(data.get("username"), str) else "").strip()
        password = data.get("password","") if isinstance(data.get("password"), str) else ""
        login_token_value = (
            str(data.get("internal_test_token") or "").strip()
            or str(data.get("login_token") or "").strip()
            or str(data.get("tester_token") or "").strip()
            or request.headers.get("X-Internal-Test-Token", "").strip()
            or request.headers.get("X-Tester-Token", "").strip()
        )
        has_login_token = bool(login_token_value)

        # Generic blank check — same message regardless of which field
        if not username or (not password and not has_login_token):
            record_login_failure(ip, username, ua=ua, detail="blank_field")
            timing_delay()
            return json_resp({"ok":False,"msg":"請填寫帳號與密碼"}), 400

        conn = get_db()
        try:
            ensure_public_account_columns(conn)
            user_row = conn.execute(
                "SELECT id, username, status, blocked_until, locked_until, failed_login_count, role, "
                "email, email_verified, birthdate, must_change_password, is_default_password, signup_bonus_deferred FROM users WHERE username=?",
                (username,)
            ).fetchone()

            # A shared source address must not make unrelated members consume
            # one another's 30/minute allowance.  Unknown usernames share one
            # source-scoped bucket so random names cannot grow memory without
            # bound or evade the source-wide ceiling.
            principal = int(user_row["id"]) if user_row else "unknown"
            blocked, info = is_rate_limited(
                (ip, "login_principal", principal),
                max_req=LOGIN_PRINCIPAL_RATE_LIMIT_PER_MINUTE,
                window_sec=60,
            )
            if blocked:
                audit("LOGIN_RATELIMIT", ip, username, ua=ua, detail="scope=principal")
                timing_delay()
                retry_after = max(1, int(info.get("retry_after_seconds") or 1))
                response = json_resp({
                    "ok": False,
                    "msg": "登入請求太頻繁，請稍後再試",
                    "error": "login_rate_limited",
                    "scope": "account",
                    "retry_after_seconds": retry_after,
                }, 429)
                response.headers["Retry-After"] = str(retry_after)
                return response

            # Always do timing-consuming verify to prevent timing oracles
            pw_hash = None
            if user_row:
                pw_row = conn.execute(
                    "SELECT password_hash FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT 1",
                    (user_row["id"],)
                ).fetchone()
                if pw_row:
                    pw_hash = pw_row["password_hash"]

            # Perform verify with constant-ish delay regardless of user existence
            if pw_hash and password:
                verified = verify_password(pw_hash, password)
            else:
                # Fake hash work — user doesn't exist, still burn same CPU time
                # Properly formatted: 16-byte salt → 22 b64 chars, 32-byte hash → 43 b64 chars
                fake_salt = base64.urlsafe_b64encode(b"fakesaltPASSSalt0").decode()[:22]
                fake_hsh  = base64.urlsafe_b64encode(b"f" * 32).decode()[:43]
                try:
                    verify_password(f"$argon2id$v=19$m=65536,t=3,p=4${fake_salt}${fake_hsh}", password or "token-login-padding")
                except argon2.exceptions.VerifyMismatchError:
                    pass  # expected — always fails
                verified = False

            # ALWAYS add jitter delay to obscure timing differences
            timing_delay()

            now = datetime.now().isoformat()
            account_security_enabled = is_feature_enabled("feature_account_security_enabled")
            mode = current_server_mode(conn)
            token_authz = {"ok": False, "scope": "", "allowed_features": []}
            if user_row and has_login_token and mode in {"test", "internal_test"}:
                token_authz = internal_test_login_authorization(conn, settings, data, user_row["id"])
                if token_authz.get("ok"):
                    verified = True
            if verified:
                if account_security_enabled and user_row["locked_until"]:
                    try:
                        locked_until = datetime.fromisoformat(user_row["locked_until"])
                        if datetime.now() < locked_until:
                            audit("LOGIN_ACCOUNT_LOCKED", ip, username, ua=ua, detail=f"locked_until={user_row['locked_until']}")
                            return json_resp({"ok":False,"msg":"登入失敗（帳號或密碼錯誤）"}), 401
                    except Exception:
                        pass
                if user_row["blocked_until"]:
                    try:
                        blocked_until = datetime.fromisoformat(user_row["blocked_until"])
                        if datetime.now() < blocked_until:
                            audit("LOGIN_BLOCKED_TEMP", ip, username, ua=ua, detail=f"blocked_until={user_row['blocked_until']}")
                            return json_resp({"ok":False,"msg":"登入失敗（帳號或密碼錯誤）"}), 401
                    except Exception:
                        pass
                if user_row["status"] != "active":
                    audit("LOGIN_INACTIVE", ip, username, ua=ua, success=False)
                    return json_resp({"ok":False,"msg":"登入失敗（帳號或密碼錯誤）"}), 401
                if settings.get("require_email_verification") and not bool(user_row["email_verified"] or 0):
                    audit("LOGIN_EMAIL_UNVERIFIED", ip, username, ua=ua, success=False)
                    return json_resp({"ok":False,"msg":"登入失敗（帳號或密碼錯誤）"}), 401
                signup_bonus = maybe_award_signup_bonus_for_login(user_row) if bool(user_row["signup_bonus_deferred"] or 0) else None
                if signup_bonus and signup_bonus.get("ok") and (signup_bonus.get("created") or signup_bonus.get("already_granted")):
                    conn.execute(
                        "UPDATE users SET signup_bonus_deferred=0, updated_at=? WHERE id=?",
                        (now, user_row["id"]),
                    )
                    conn.commit()
                internal_test_auth_scope = ""
                internal_test_allowed_features = []
                if token_authz.get("ok"):
                    internal_test_auth_scope = str(token_authz.get("scope") or "")
                    internal_test_allowed_features = list(token_authz.get("allowed_features") or [])
                if mode == "internal_test" and user_row["username"] != "root" and not token_authz.get("ok"):
                    internal_test_authz = internal_test_login_authorization(conn, settings, data, user_row["id"])
                    if not internal_test_authz.get("ok"):
                        if callable(record_login_attempt):
                            record_login_attempt(
                                user_id=user_row["id"],
                                ip_address=ip,
                                user_agent=ua,
                                success=False,
                                attempted_at=now,
                            )
                        audit("LOGIN_INTERNAL_TEST_TOKEN_REQUIRED", ip, username, ua=ua, success=False)
                        return json_resp({"ok":False,"msg":"目前是內測模式，請輸入 root 提供的內測 token"}), 403
                    internal_test_auth_scope = str(internal_test_authz.get("scope") or "")
                    internal_test_allowed_features = list(internal_test_authz.get("allowed_features") or [])

                auth_conn = get_auth_db()
                try:
                    conflict = production_login_conflict(conn, auth_conn, user_row["id"], ip, now, settings)
                finally:
                    auth_conn.close()
                if conflict:
                    if callable(record_login_attempt):
                        record_login_attempt(
                            user_id=user_row["id"],
                            ip_address=ip,
                            user_agent=ua,
                            success=False,
                            attempted_at=now,
                        )
                    audit(
                        "LOGIN_PRODUCTION_SESSION_CONFLICT",
                        ip,
                        username,
                        ua=ua,
                        success=False,
                        detail=f"{conflict['kind']};{conflict['detail']}",
                    )
                    return json_resp({"ok":False,"msg":conflict["msg"]}), 403

                # Log successful attempt
                if callable(record_login_attempt):
                    record_login_attempt(
                        user_id=user_row["id"],
                        ip_address=ip,
                        user_agent=ua,
                        success=True,
                        attempted_at=now,
                    )
                if account_security_enabled:
                    conn.execute(
                        "UPDATE users SET failed_login_count=0, locked_until=NULL, last_login_at=?, updated_at=? WHERE id=?",
                        (now, now, user_row["id"])
                    )
                    record_login_location(conn, user_row["id"], username, ip, ua)
                conn.commit()

                birthday_gift = None
                if (
                    points_service
                    and bool(settings.get("points_admin_weekly_salary_award_on_login", False))
                    and user_row["username"] != "root"
                    and user_row["role"] in {"manager", "super_admin"}
                ):
                    try:
                        points_service.award_admin_weekly_salary(
                            user_id=user_row["id"],
                            actor={"id": user_row["id"], "username": user_row["username"], "role": user_row["role"]},
                        )
                    except Exception as exc:
                        audit("POINTS_ADMIN_SALARY_FAILED", ip, username, ua=ua, success=False, detail=str(exc))
                birthday_gift = maybe_award_birthday_gift(user_row, settings, ip, ua)

                # Create token + save session to DB
                token = make_token(username)
                db_save_session(
                    user_row["id"],
                    token,
                    ip,
                    ua,
                    auth_scope=internal_test_auth_scope,
                    allowed_features=internal_test_allowed_features,
                )
                sync_official_room_membership(user_row["id"])

                audit("LOGIN_OK", ip, username, ua=ua, success=True)
                login_msg = str(settings.get("site_success_heading") or "恭喜登入成功").strip()[:120] or "恭喜登入成功"
                birthday_storage_created = bool(((birthday_gift or {}).get("storage_quota_gift") or {}).get("created"))
                if birthday_gift and (birthday_gift.get("created") or birthday_storage_created):
                    login_msg = f"恭喜登入成功，生日禮金 {birthday_gift.get('amount', BIRTHDAY_GIFT_POINTS)} 點與 1GB 雲端硬碟 7 日已入帳"
                elif signup_bonus and signup_bonus.get("created"):
                    login_msg = "恭喜登入成功，註冊禮已入帳官方熱錢包"
                resp = json_resp({
                    "ok": True,
                    "msg": login_msg,
                    "birthday_gift": birthday_gift,
                    "signup_bonus": signup_bonus,
                    "wallet_onboarding": wallet_onboarding_payload(user_row["id"]),
                    "must_change_password": should_require_password_change_flag(user_row["must_change_password"]),
                    "is_default_password": bool(user_row["is_default_password"] or 0),
                })
                resp.set_cookie("session_token", token, max_age=current_session_ttl_seconds(),
                                httponly=True, samesite=SESSION_COOKIE_SAMESITE,
                                secure=SESSION_COOKIE_SECURE)
                # Invalidate the public CSRF token and issue a fresh per-user token
                new_csrf = make_csrf_token()
                store_csrf_token(new_csrf, username)
                resp.set_cookie("csrf_token", new_csrf, max_age=current_csrf_token_ttl_seconds(),
                                httponly=False, samesite=SESSION_COOKIE_SAMESITE,
                                secure=SESSION_COOKIE_SECURE)
                return resp
            else:
                # Log failed attempt
                user_id_for_log = user_row["id"] if user_row else None
                if callable(record_login_attempt):
                    record_login_attempt(
                        user_id=user_id_for_log,
                        ip_address=ip,
                        user_agent=ua,
                        success=False,
                        attempted_at=now,
                    )
                if account_security_enabled and user_row:
                    fail_count = int(user_row["failed_login_count"] or 0) + 1
                    lock_limit = max(1, int(settings.get("max_login_failures", 5)))
                    updates = ["failed_login_count=?", "updated_at=?"]
                    params = [fail_count, now]
                    if fail_count >= lock_limit:
                        locked_until = (datetime.now() + timedelta(minutes=max(1, int(settings.get("block_duration_minutes", 10))))).isoformat()
                        updates.append("locked_until=?")
                        params.append(locked_until)
                        audit("LOGIN_ACCOUNT_LOCKED", ip, username, ua=ua, detail=f"failed_login_count={fail_count},locked_until={locked_until}")
                    params.append(user_row["id"])
                    conn.execute("UPDATE users SET " + ", ".join(updates) + " WHERE id=?", params)
                    conn.commit()
                record_login_failure(ip, username=username, ua=ua, detail="bad_credentials")

                # Generic message — never distinguish "no user" from "bad pw"
                return json_resp({"ok":False,"msg":"登入失敗（帳號或密碼錯誤）"}), 401
        finally:
            conn.close()

    @app.route("/api/password-reset/request", methods=["POST"])
    @require_csrf
    def password_reset_request():
        ip, ua = get_client_ip(), get_ua()
        blocked, info = is_rate_limited(f"password-reset:{ip}", max_req=5, window_sec=3600)
        if blocked:
            return json_resp({"ok": False, "msg": f"請求太頻繁（每小時最多 {info['limit']} 次）"}), 429
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        identifier = data.get("username_or_email") or data.get("username") or data.get("email") if isinstance(data, dict) else ""
        conn = get_db()
        try:
            ensure_public_account_columns(conn)
            ensure_account_recovery_schema(conn)
            row = find_recovery_user(conn, identifier)
            mode = password_reset_mode()
            if row and row["status"] == "active" and not is_root_account_row(row):
                user_blocked, user_info = is_rate_limited(f"password-reset-user:{row['id']}", max_req=2, window_sec=3600)
                if user_blocked:
                    audit(
                        "PASSWORD_RESET_USER_RATELIMIT",
                        ip,
                        user=row["username"],
                        ua=ua,
                        success=False,
                        detail=f"limit={user_info['limit']}",
                    )
                    conn.commit()
                    timing_delay()
                    return password_reset_success_response(mode)
            if is_root_account_row(row):
                audit(
                    "PASSWORD_RESET_ROOT_BLOCKED",
                    ip,
                    user="root",
                    ua=ua,
                    success=False,
                    detail="offline_recovery_required",
                )
            if mode == "email_token" and row and row["status"] == "active" and row["email"] and not is_root_account_row(row):
                conn.execute(
                    "UPDATE account_recovery_tokens SET used_at=? "
                    "WHERE user_id=? AND purpose='password_reset' AND used_at IS NULL",
                    (datetime.now().isoformat(), row["id"]),
                )
                token = create_recovery_token(conn, user_id=row["id"], purpose="password_reset", ip=ip, user_agent=ua, ttl_minutes=60)
                queue_mail(
                    conn,
                    recipient=row["email"],
                    subject=f"{SERVER_APP_NAME} password reset",
                    body=f"Password reset token for {row['username']}:\n{token}\nThis token expires in 60 minutes.",
                    kind="password_reset",
                )
                audit("PASSWORD_RESET_REQUESTED", ip, user=row["username"], ua=ua, success=True)
            elif mode == "admin_review" and row and row["status"] == "active" and not is_root_account_row(row):
                request_id, created = create_password_reset_review_request(
                    conn,
                    user_id=row["id"],
                    identifier=identifier,
                    ip=ip,
                    user_agent=ua,
                )
                audit(
                    "PASSWORD_RESET_REVIEW_REQUESTED",
                    ip,
                    user=row["username"],
                    ua=ua,
                    success=True,
                    detail=f"review_request_id={request_id},created={created}",
                )
            else:
                audit("PASSWORD_RESET_REQUESTED", ip, user="-", ua=ua, success=True, detail="generic_no_delivery")
            conn.commit()
            timing_delay()
            return password_reset_success_response(mode)
        finally:
            conn.close()

    @app.route("/api/password-reset/confirm", methods=["POST"])
    @require_csrf
    def password_reset_confirm():
        ip, ua = get_client_ip(), get_ua()
        blocked, info = is_rate_limited(f"password-reset-confirm:{ip}", max_req=10, window_sec=3600)
        if blocked:
            audit("PASSWORD_RESET_CONFIRM_RATE_LIMITED", ip, ua=ua, success=False)
            return json_resp({"ok": False, "msg": f"重設密碼確認嘗試過於頻繁（每小時最多 {info['limit']} 次）"}), 429
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        token = str(data.get("token") or "").strip()
        password = data.get("password", "") if isinstance(data.get("password"), str) else ""
        password_confirm = data.get("password_confirm", "") if isinstance(data.get("password_confirm"), str) else ""
        if not token:
            return json_resp({"ok": False, "msg": "驗證碼不可為空"}), 400
        if password != password_confirm:
            return json_resp({"ok": False, "msg": "兩次密碼輸入不一致"}), 400
        conn = get_db()
        try:
            ensure_public_account_columns(conn)
            token_row = lookup_valid_token(conn, token=token, purpose="password_reset")
            if not token_row:
                audit("PASSWORD_RESET_TOKEN_INVALID", ip, ua=ua, success=False)
                return json_resp({"ok": False, "msg": "驗證碼無效或已過期"}), 400
            target_is_root = str(token_row["username"] or "").strip().lower() == "root"
            if target_is_root:
                audit("PASSWORD_RESET_ROOT_BLOCKED", ip, user="root", ua=ua, success=False, detail="offline_recovery_required_confirm")
                return json_resp({"ok": False, "msg": "root 帳號不可透過 Web 忘記密碼流程重設，請改用離線 root recovery CLI"}), 403
            if not target_is_root:
                ok, msg, strength = validate_configured_password(password)
                if not ok:
                    return json_resp({"ok": False, "msg": msg, "password_strength": strength}), 400
            current_row = conn.execute(
                "SELECT password_hash FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (token_row["user_id"],),
            ).fetchone()
            if current_row and verify_password(current_row["password_hash"], password):
                return json_resp({"ok": False, "msg": "新密碼不可與目前密碼相同"}), 400
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
                (token_row["user_id"], hash_password(password), now),
            )
            conn.execute(
                "UPDATE users SET password_strength_score=?, password_changed_at=?, must_change_password=0, is_default_password=0, failed_login_count=0, locked_until=NULL, updated_at=? WHERE id=?",
                (strength["score"], now, now, token_row["user_id"]),
            )
            mark_token_used(conn, token_row["id"])
            trim_password_history(conn, token_row["user_id"])
            conn.commit()
            revoke_user_sessions(token_row["user_id"])
            audit("PASSWORD_RESET_CONFIRMED", ip, user=token_row["username"], ua=ua, success=True)
            return json_resp({"ok": True, "msg": "密碼已重設，請重新登入"})
        finally:
            conn.close()

    @app.route("/api/email-verification/request", methods=["POST"])
    @require_csrf
    def email_verification_request():
        ip, ua = get_client_ip(), get_ua()
        blocked, info = is_rate_limited(f"email-verify:{ip}", max_req=5, window_sec=3600)
        if blocked:
            return json_resp({"ok": False, "msg": f"請求太頻繁（每小時最多 {info['limit']} 次）"}), 429
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        identifier = data.get("username_or_email") or data.get("username") or data.get("email") if isinstance(data, dict) else ""
        conn = get_db()
        try:
            ensure_public_account_columns(conn)
            ensure_account_recovery_schema(conn)
            row = find_recovery_user(conn, identifier)
            if row and row["email"] and not bool(row["email_verified"] or 0):
                token = create_recovery_token(conn, user_id=row["id"], purpose="email_verify", ip=ip, user_agent=ua, ttl_minutes=1440)
                queue_mail(
                    conn,
                    recipient=row["email"],
                    subject=f"{SERVER_APP_NAME} email verification",
                    body=f"Email verification token for {row['username']}:\n{token}\nThis token expires in 24 hours.",
                    kind="email_verify",
                )
                audit("EMAIL_VERIFICATION_REQUESTED", ip, user=row["username"], ua=ua, success=True)
            else:
                audit("EMAIL_VERIFICATION_REQUESTED", ip, user="-", ua=ua, success=True, detail="generic_no_delivery")
            conn.commit()
            timing_delay()
            return generic_recovery_response()
        finally:
            conn.close()

    @app.route("/api/email-verification/confirm", methods=["POST"])
    @require_csrf
    def email_verification_confirm():
        ip, ua = get_client_ip(), get_ua()
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        token = str(data.get("token") or "").strip() if isinstance(data, dict) else ""
        if not token:
            return json_resp({"ok": False, "msg": "驗證碼不可為空"}), 400
        conn = get_db()
        try:
            ensure_public_account_columns(conn)
            token_row = lookup_valid_token(conn, token=token, purpose="email_verify")
            if not token_row:
                audit("EMAIL_VERIFICATION_TOKEN_INVALID", ip, ua=ua, success=False)
                return json_resp({"ok": False, "msg": "驗證碼無效或已過期"}), 400
            now = datetime.now().isoformat()
            conn.execute("UPDATE users SET email_verified=1, updated_at=? WHERE id=?", (now, token_row["user_id"]))
            mark_token_used(conn, token_row["id"])
            conn.commit()
            audit("EMAIL_VERIFIED", ip, user=token_row["username"], ua=ua, success=True)
            return json_resp({"ok": True, "msg": "Email 已完成驗證"})
        finally:
            conn.close()

    @app.route("/api/logout", methods=["POST"])
    @require_csrf
    def logout():
        ip, ua, tok = get_client_ip(), get_ua(), request.cookies.get("session_token")
        user = db_get_user_from_token(tok) if tok else None
        if tok:
            db_delete_session(tok, notify_security_event=False)
        if user:
            delete_csrf_tokens_for_username(user)
        else:
            delete_csrf_token(request.cookies.get("csrf_token", ""))
        audit("LOGOUT", ip, user=user or "-", ua=ua, success=bool(user))
        resp = json_resp({"ok":True,"msg":"已登出"})
        resp.delete_cookie("session_token", path="/", samesite=SESSION_COOKIE_SAMESITE, secure=SESSION_COOKIE_SECURE)
        resp.delete_cookie("csrf_token", path="/", samesite=SESSION_COOKIE_SAMESITE, secure=SESSION_COOKIE_SECURE)
        return resp

    @app.route("/api/session/idle-timeout", methods=["POST"])
    @require_csrf
    def idle_timeout_logout():
        if request.headers.get("X-Idle-Timeout-Logout") != "1":
            return json_resp({"ok": False, "msg": "缺少閒置登出確認"}), 400
        ip, ua, tok = get_client_ip(), get_ua(), request.cookies.get("session_token")
        user = db_get_user_from_token(tok) if tok else None
        if tok:
            db_delete_session(tok, notify_security_event=False)
        if user:
            delete_csrf_tokens_for_username(user)
        else:
            delete_csrf_token(request.cookies.get("csrf_token", ""))
        audit("IDLE_TIMEOUT_LOGOUT", ip, user=user or "-", ua=ua, success=bool(tok))
        resp = json_resp({"ok": True, "msg": "閒置逾時，已登出"})
        resp.delete_cookie("session_token", path="/", samesite=SESSION_COOKIE_SAMESITE, secure=SESSION_COOKIE_SECURE)
        resp.delete_cookie("csrf_token", path="/", samesite=SESSION_COOKIE_SAMESITE, secure=SESSION_COOKIE_SECURE)
        return resp

    @app.route("/api/me", methods=["GET"])
    def me():
        ctx = get_current_user_ctx()
        if not ctx:
            if str(request.args.get("optional") or "").lower() in {"1", "true", "yes"}:
                return json_resp({"ok": False, "msg": "未登入"})
            return json_resp({"ok":False,"msg":"未登入"}), 401
        role = "super_admin" if ctx["username"] == "root" else ctx["role"]
        is_special_account = ctx["username"] == "root" or role in {"super_admin", "manager"}
        effective_level = None if is_special_account else (dict(ctx).get("effective_level") or dict(ctx).get("member_level") or "normal")
        # 全局覆寫（system_settings）> member_level 規則 > 預設 10
        settings = get_system_settings()
        conn = get_db()
        try:
            avatar_row = conn.execute("SELECT avatar_file_id FROM users WHERE id=?", (ctx["id"],)).fetchone()
            appearance_settings = get_profile_appearance(conn, ctx["id"])
            display_timezone = get_profile_display_timezone(conn, ctx["id"])
        finally:
            conn.close()
        override = settings.get("session_idle_timeout_minutes")
        if override is not None:
            session_idle_timeout_minutes = max(0, int(override))
        elif get_member_level_rule and effective_level:
            conn = get_db()
            try:
                rule = get_member_level_rule(conn, effective_level) or {}
                session_idle_timeout_minutes = max(1, int(rule.get("session_idle_timeout_minutes") or 10))
            finally:
                conn.close()
        else:
            session_idle_timeout_minutes = 10
        return json_resp({
            "ok": True,
            "id": ctx["id"],
            "username": ctx["username"],
            "role": role,
            "role_label": ROLE_LABEL.get(role, role),
            "status": ctx["status"],
            "member_level": None if is_special_account else (dict(ctx).get("member_level") or "normal"),
            "base_level": None if is_special_account else (dict(ctx).get("base_level") or dict(ctx).get("member_level") or "normal"),
            "effective_level": effective_level,
            "member_level_label": "特殊階級" if is_special_account else effective_level,
            "special_account": is_special_account,
            "session_idle_timeout_minutes": session_idle_timeout_minutes,
            "trust_score": dict(ctx).get("trust_score") or 0,
            "reputation": dict(ctx).get("reputation") or 0,
            "violation_score": dict(ctx).get("violation_score") or dict(ctx).get("violation_count") or 0,
            "sanction_status": dict(ctx).get("sanction_status") or "none",
            "sanction_until": dict(ctx).get("sanction_until"),
            "must_change_password": should_require_password_change_flag(dict(ctx).get("must_change_password")),
            "is_default_password": bool(dict(ctx).get("is_default_password") or 0),
            "nickname": decrypt_field(ctx["nickname"]),
            "birthdate": decrypt_field(ctx["birthdate"]),
            "avatar_file_id": ((avatar_row["avatar_file_id"] if avatar_row and "avatar_file_id" in avatar_row.keys() else dict(ctx).get("avatar_file_id")) or ""),
            "chat_violation_warned": dict(ctx).get("chat_violation_warned") or 0,
            "appearance_settings": appearance_settings,
            "display_timezone": display_timezone,
            "auth_scope": dict(ctx).get("auth_scope") or dict(ctx).get("_auth_scope") or "",
            "allowed_features": list(dict(ctx).get("allowed_features") or dict(ctx).get("_allowed_features") or []),
            "wallet_onboarding": wallet_onboarding_payload(ctx["id"]),
        })

    @app.route("/api/me/appearance", methods=["GET", "PUT", "DELETE"])
    @require_csrf_safe
    def me_appearance():
        ctx = get_current_user_ctx()
        if not ctx:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if request.method == "GET":
            conn = get_db()
            try:
                return json_resp({"ok": True, "appearance_settings": get_profile_appearance(conn, ctx["id"])})
            finally:
                conn.close()
        if request.method == "DELETE":
            conn = get_db()
            try:
                appearance = clear_profile_appearance(conn, actor=ctx)
                conn.commit()
            finally:
                conn.close()
            audit("USER_APPEARANCE_RESET", get_client_ip(), user=ctx["username"], success=True, ua=get_ua())
            return json_resp({"ok": True, "appearance_settings": appearance, "msg": "已恢復全站預設外觀"})
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            appearance = update_profile_appearance(conn, actor=ctx, data=data)
            conn.commit()
        finally:
            conn.close()
        audit("USER_APPEARANCE_SAVED", get_client_ip(), user=ctx["username"], success=True, ua=get_ua(), detail=str(sorted(appearance.keys())))
        return json_resp({"ok": True, "appearance_settings": appearance, "msg": "個人外觀已儲存"})
