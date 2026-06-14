import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from functools import wraps
from flask import request, send_file

from services.users.member_levels import apply_member_level_change, ensure_member_level_user_columns
from services.storage.cloud_drive import (
    delete_avatar_file_if_unreferenced,
    decrypt_server_encrypted_bytes,
    ensure_cloud_drive_attachment_schema,
    is_server_encrypted_file,
    mark_avatar_file_asset,
    resolve_file_storage_path,
    store_cloud_upload,
)
from services.governance.sanction_notices import record_admin_sanction_notice
from services.governance.violation_fines import (
    FEATURE_LABELS,
    create_user_feature_restriction,
    create_violation_fine,
    ensure_violation_fine_schema,
)
from services.users.recovery import (
    ensure_account_recovery_schema,
    get_password_reset_review_request,
    list_password_reset_review_requests,
    mark_password_reset_review_request,
)
from services.users.friends import (
    accept_friend_by_code,
    accepted_friend_ids,
    block_user,
    create_friend_request,
    follow_user,
    get_profile_payload,
    list_targetable_users,
    list_friend_state,
    remove_friend,
    review_friend_request,
    unblock_user,
    unfollow_user,
)
from services.users.profiles import (
    ensure_user_profile_schema,
    get_profile_appearance,
    get_profile_display_timezone,
    rotate_friend_code,
    update_profile,
)


def register_user_routes(app, deps):
    ACCOUNT_STATUSES = deps["ACCOUNT_STATUSES"]
    MAX_MANAGERS = deps["MAX_MANAGERS"]
    MAX_EXTRA_SUPER_ADMINS = deps["MAX_EXTRA_SUPER_ADMINS"]
    MEMBER_LEVELS = deps["MEMBER_LEVELS"]
    PASSWORD_HISTORY_LIMIT = deps["PASSWORD_HISTORY_LIMIT"]
    ROLE_LABEL = deps["ROLE_LABEL"]
    ROLE_RANK = deps["ROLE_RANK"]
    add_violation = deps["add_violation"]
    audit = deps["audit"]
    check_user_rate_limit = deps["check_user_rate_limit"]
    count_role = deps["count_role"]
    delete_csrf_tokens_for_username = deps.get("delete_csrf_tokens_for_username", lambda username: None)
    decrypt_field = deps["decrypt_field"]
    encrypt_field = deps["encrypt_field"]
    ensure_user_official_room_membership = deps["ensure_user_official_room_membership"]
    get_client_ip = deps["get_client_ip"]
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_auth_db = deps.get("get_auth_db", deps["get_db"])
    get_readonly_auth_db = deps.get("get_readonly_auth_db", get_auth_db)
    get_db = deps["get_db"]
    get_system_settings = deps.get("get_system_settings", lambda: {})
    get_ua = deps["get_ua"]
    hash_password = deps["hash_password"]
    hash_token = deps["hash_token"]
    is_feature_enabled = deps["is_feature_enabled"]
    json_resp = deps["json_resp"]
    normalize_text = deps["normalize_text"]
    parse_birthdate = deps["parse_birthdate"]
    parse_positive_int = deps["parse_positive_int"]
    points_service = deps.get("points_service")
    revoke_user_sessions = deps["revoke_user_sessions"]
    require_csrf = deps["require_csrf"]
    require_csrf_safe = deps["require_csrf_safe"]
    SESSION_COOKIE_SAMESITE = deps["SESSION_COOKIE_SAMESITE"]
    SESSION_COOKIE_SECURE = deps["SESSION_COOKIE_SECURE"]
    enforce_password_strength = deps["enforce_password_strength"]
    role_rank = deps["role_rank"]
    score_password_strength = deps["score_password_strength"]
    user_public_payload = deps["user_public_payload"]
    validate_id_number = deps["validate_id_number"]
    validate_password = deps["validate_password"]
    validate_phone = deps["validate_phone"]
    verify_password = deps["verify_password"]

    def quote_identifier(name):
        if not isinstance(name, str) or "\x00" in name:
            raise ValueError("invalid SQL identifier")
        return '"' + name.replace('"', '""') + '"'

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

    def manager_seat_limit():
        try:
            settings = get_system_settings() or {}
            value = int(settings.get("max_manager_seats", MAX_MANAGERS))
        except Exception:
            value = int(MAX_MANAGERS)
        return max(0, min(value, 1000))

    def ensure_user_delete_dependency_tables(conn):
        """Repair legacy DBs with optional FK parents missing before user delete."""
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "announcement_attachment_requests" in tables and "announcements" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS announcements (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    title            TEXT NOT NULL DEFAULT '',
                    content          TEXT NOT NULL DEFAULT '',
                    author_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    author_username  TEXT NOT NULL DEFAULT '',
                    is_pinned        INTEGER NOT NULL DEFAULT 0,
                    is_active        INTEGER NOT NULL DEFAULT 1,
                    created_at       TEXT NOT NULL DEFAULT '',
                    updated_at       TEXT NOT NULL DEFAULT ''
                )
            """)

    def _table_column_meta(conn, table):
        return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()}

    def _table_exists(conn, table):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(table or ""),),
        ).fetchone()
        return bool(row)

    def cleanup_user_foreign_key_refs(conn, *, user_id):
        """Clear existing user references before deleting, including legacy FKs.

        SQLite only applies ON DELETE rules while foreign_keys is enabled, and
        several older optional tables were created without explicit ON DELETE
        behavior.  A root delete should not fail just because a user has
        messages, uploads, invites, or pending attachment review rows.
        """
        tables = [
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if row["name"] != "users" and not str(row["name"]).startswith("sqlite_")
        ]
        operations = []
        for table in tables:
            try:
                fks = conn.execute(f"PRAGMA foreign_key_list({quote_identifier(table)})").fetchall()
            except sqlite3.Error:
                continue
            user_fks = [fk for fk in fks if fk["table"] == "users"]
            if not user_fks:
                continue
            cols = _table_column_meta(conn, table)
            for fk in user_fks:
                column = fk["from"]
                if column not in cols:
                    continue
                quoted_table = quote_identifier(table)
                quoted_column = quote_identifier(column)
                on_delete = str(fk["on_delete"] or "").upper()
                nullable = not bool(cols[column]["notnull"]) and not bool(cols[column]["pk"])
                if on_delete == "SET NULL" or nullable:
                    cur = conn.execute(
                        f"UPDATE {quoted_table} SET {quoted_column}=NULL WHERE {quoted_column}=?",
                        (user_id,),
                    )
                    action = "set_null"
                else:
                    cur = conn.execute(
                        f"DELETE FROM {quoted_table} WHERE {quoted_column}=?",
                        (user_id,),
                    )
                    action = "delete_rows"
                if cur.rowcount:
                    operations.append({"table": table, "column": column, "action": action, "count": cur.rowcount})
        return operations

    def hard_delete_user_row(conn, *, user_id, username):
        ensure_user_delete_dependency_tables(conn)
        cleanup_user_foreign_key_refs(conn, user_id=user_id)
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        return {"user_id": int(user_id), "username": username}

    def soft_delete_user_row(conn, *, user_id, username):
        ensure_user_delete_dependency_tables(conn)
        now = datetime.now().isoformat()
        tombstone_username = f"deleted_u{int(user_id)}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        summary = {
            "cloud_files_deleted": 0,
            "storage_entries_deleted": 0,
            "folders_deleted": 0,
            "albums_deleted": 0,
            "videos_blocked": 0,
            "wallet_closed": False,
            "warnings": [],
            "original_username": username,
            "tombstone_username": tombstone_username,
        }

        def _warn(scope, exc):
            summary["warnings"].append({"scope": scope, "error": str(exc)})

        if _table_exists(conn, "uploaded_files"):
            try:
                upload_cols = _table_column_meta(conn, "uploaded_files")
                upload_updates = []
                upload_params = []
                if "deleted_at" in upload_cols:
                    upload_updates.append("deleted_at=?")
                    upload_params.append(now)
                if "updated_at" in upload_cols:
                    upload_updates.append("updated_at=?")
                    upload_params.append(now)
                if upload_updates and "owner_user_id" in upload_cols:
                    upload_where = "owner_user_id=?"
                    if "deleted_at" in upload_cols:
                        upload_where += " AND deleted_at IS NULL"
                    file_cur = conn.execute(
                        f"UPDATE uploaded_files SET {', '.join(upload_updates)} WHERE {upload_where}",
                        (*upload_params, int(user_id)),
                    )
                    summary["cloud_files_deleted"] = max(int(file_cur.rowcount or 0), 0)
            except sqlite3.Error as exc:
                _warn("uploaded_files", exc)

        if _table_exists(conn, "storage_files"):
            try:
                storage_cols = _table_column_meta(conn, "storage_files")
                storage_updates = []
                storage_params = []
                if "deleted_at" in storage_cols:
                    storage_updates.append("deleted_at=?")
                    storage_params.append(now)
                if "updated_at" in storage_cols:
                    storage_updates.append("updated_at=?")
                    storage_params.append(now)
                if "is_trashed" in storage_cols:
                    storage_updates.append("is_trashed=1")
                if "trashed_at" in storage_cols:
                    storage_updates.append("trashed_at=COALESCE(trashed_at, ?)")
                    storage_params.append(now)
                if storage_updates:
                    storage_where = "owner_user_id=?"
                    if "deleted_at" in storage_cols:
                        storage_where += " AND deleted_at IS NULL"
                    storage_cur = conn.execute(
                        f"UPDATE storage_files SET {', '.join(storage_updates)} WHERE {storage_where}",
                        (*storage_params, int(user_id)),
                    )
                    summary["storage_entries_deleted"] = max(int(storage_cur.rowcount or 0), 0)
            except sqlite3.Error as exc:
                _warn("storage_files", exc)

        if _table_exists(conn, "storage_folders"):
            try:
                folder_cols = _table_column_meta(conn, "storage_folders")
                folder_updates = []
                folder_params = []
                if "deleted_at" in folder_cols:
                    folder_updates.append("deleted_at=?")
                    folder_params.append(now)
                if "updated_at" in folder_cols:
                    folder_updates.append("updated_at=?")
                    folder_params.append(now)
                if folder_updates:
                    folder_where = "owner_user_id=?"
                    if "deleted_at" in folder_cols:
                        folder_where += " AND deleted_at IS NULL"
                    folder_cur = conn.execute(
                        f"UPDATE storage_folders SET {', '.join(folder_updates)} WHERE {folder_where}",
                        (*folder_params, int(user_id)),
                    )
                    summary["folders_deleted"] = max(int(folder_cur.rowcount or 0), 0)
            except sqlite3.Error as exc:
                _warn("storage_folders", exc)

        if _table_exists(conn, "albums"):
            try:
                album_cols = _table_column_meta(conn, "albums")
                album_updates = []
                album_params = []
                if "deleted_at" in album_cols:
                    album_updates.append("deleted_at=?")
                    album_params.append(now)
                if "updated_at" in album_cols:
                    album_updates.append("updated_at=?")
                    album_params.append(now)
                if "visibility" in album_cols:
                    album_updates.append("visibility='private'")
                if album_updates:
                    album_where = "owner_user_id=?"
                    if "deleted_at" in album_cols:
                        album_where += " AND deleted_at IS NULL"
                    album_cur = conn.execute(
                        f"UPDATE albums SET {', '.join(album_updates)} WHERE {album_where}",
                        (*album_params, int(user_id)),
                    )
                    summary["albums_deleted"] = max(int(album_cur.rowcount or 0), 0)
            except sqlite3.Error as exc:
                _warn("albums", exc)

        if _table_exists(conn, "storage_share_links"):
            try:
                share_cols = _table_column_meta(conn, "storage_share_links")
                if "revoked_at" in share_cols:
                    conn.execute(
                        "UPDATE storage_share_links SET revoked_at=? WHERE owner_user_id=? AND revoked_at IS NULL",
                        (now, int(user_id)),
                    )
            except sqlite3.Error as exc:
                _warn("storage_share_links", exc)

        if _table_exists(conn, "album_share_links") and _table_exists(conn, "albums"):
            try:
                album_share_cols = _table_column_meta(conn, "album_share_links")
                if "revoked_at" in album_share_cols:
                    conn.execute(
                        """
                        UPDATE album_share_links
                        SET revoked_at=?
                        WHERE revoked_at IS NULL
                          AND album_id IN (
                              SELECT id FROM albums WHERE owner_user_id=?
                          )
                        """,
                        (now, int(user_id)),
                    )
            except sqlite3.Error as exc:
                _warn("album_share_links", exc)

        if _table_exists(conn, "videos"):
            try:
                video_cols = _table_column_meta(conn, "videos")
                video_updates = []
                video_params = []
                if "visibility" in video_cols:
                    video_updates.append("visibility='private'")
                if "status" in video_cols:
                    video_updates.append("status='blocked'")
                if "updated_at" in video_cols:
                    video_updates.append("updated_at=?")
                    video_params.append(now)
                if video_updates:
                    video_cur = conn.execute(
                        f"UPDATE videos SET {', '.join(video_updates)} WHERE owner_user_id=?",
                        (*video_params, int(user_id)),
                    )
                    summary["videos_blocked"] = max(int(video_cur.rowcount or 0), 0)
            except sqlite3.Error as exc:
                _warn("videos", exc)

        if _table_exists(conn, "user_storage"):
            try:
                user_storage_cols = _table_column_meta(conn, "user_storage")
                updates = []
                params = []
                for column in ("used_bytes", "reserved_bytes", "file_count"):
                    if column in user_storage_cols:
                        updates.append(f"{column}=0")
                if "updated_at" in user_storage_cols:
                    updates.append("updated_at=?")
                    params.append(now)
                if updates:
                    conn.execute(
                        f"UPDATE user_storage SET {', '.join(updates)} WHERE user_id=?",
                        (*params, int(user_id)),
                    )
            except sqlite3.Error as exc:
                _warn("user_storage", exc)

        if _table_exists(conn, "points_wallets"):
            wallet_cols = _table_column_meta(conn, "points_wallets")
            wallet_updates = []
            wallet_params = []
            if "wallet_status" in wallet_cols:
                wallet_updates.append("wallet_status='closed'")
            if "risk_level" in wallet_cols:
                wallet_updates.append("risk_level='blocked'")
            if "updated_at" in wallet_cols:
                wallet_updates.append("updated_at=?")
                wallet_params.append(now)
            if wallet_updates:
                wallet_cur = conn.execute(
                    f"UPDATE points_wallets SET {', '.join(wallet_updates)} WHERE user_id=?",
                    (*wallet_params, int(user_id)),
                )
                summary["wallet_closed"] = bool(wallet_cur.rowcount)

        user_cols = _table_column_meta(conn, "users")
        user_updates = []
        user_params = []
        if "status" in user_cols:
            user_updates.append("status='deleted'")
        if "username" in user_cols:
            user_updates.append("username=?")
            user_params.append(tombstone_username)
        if "deleted_at" in user_cols:
            user_updates.append("deleted_at=?")
            user_params.append(now)
        if "updated_at" in user_cols:
            user_updates.append("updated_at=?")
            user_params.append(now)
        for column in ("email", "nickname", "real_name", "birthdate", "id_number", "phone", "avatar_file_id", "avatar_crop_json", "blocked_until"):
            if column in user_cols:
                user_updates.append(f"{column}=NULL")
        if "must_change_password" in user_cols:
            user_updates.append("must_change_password=0")
        if "is_default_password" in user_cols:
            user_updates.append("is_default_password=0")
        conn.execute(
            f"UPDATE users SET {', '.join(user_updates)} WHERE id=?",
            (*user_params, int(user_id)),
        )
        summary["user_id"] = int(user_id)
        summary["username"] = tombstone_username
        return summary
    get_member_level_rule = deps.get("get_member_level_rule")
    storage_root = deps.get("STORAGE_DIR", ".")
    server_file_fernet = deps.get("server_file_fernet")

    def require_csrf_by_method(fn):
        safe = require_csrf_safe(fn)
        strict = require_csrf(fn)

        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                return safe(*args, **kwargs)
            return strict(*args, **kwargs)

        return wrapper

    def trim_password_history(conn, user_id):
        conn.execute(
            "DELETE FROM user_passwords WHERE user_id=? AND id NOT IN ("
            "SELECT id FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT ?"
            ")",
            (user_id, user_id, PASSWORD_HISTORY_LIMIT)
        )

    def parse_device_info(raw):
        if not raw:
            return {}
        try:
            import json
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def active_session_map(auth_conn, user_ids=None):
        try:
            session_cols = {r["name"] for r in auth_conn.execute("PRAGMA table_info(sessions)").fetchall()}
        except Exception:
            return {}
        if not {"user_id", "expires_at"}.issubset(session_cols):
            return {}
        scoped_user_ids = None
        if user_ids is not None:
            scoped_user_ids = sorted({
                int(user_id)
                for user_id in user_ids
                if str(user_id or "").strip().isdigit() and int(user_id) > 0
            })
            if not scoped_user_ids:
                return {}
        last_seen_expr = "COALESCE(last_seen, created_at)" if "last_seen" in session_cols else "created_at"
        revoked_filter = "AND COALESCE(is_revoked, 0)=0" if "is_revoked" in session_cols else ""
        user_filter = ""
        params = [datetime.now().isoformat()]
        if scoped_user_ids is not None:
            placeholders = ",".join("?" for _ in scoped_user_ids)
            user_filter = f"AND user_id IN ({placeholders})"
            params.extend(scoped_user_ids)
        online_cutoff = (datetime.now() - timedelta(minutes=5)).isoformat()
        try:
            rows = auth_conn.execute(
                f"""
                SELECT user_id, MAX({last_seen_expr}) AS last_seen, COUNT(*) AS session_count
                FROM sessions
                WHERE expires_at>? {revoked_filter} {user_filter}
                GROUP BY user_id
                """,
                tuple(params),
            ).fetchall()
        except Exception:
            return {}
        result = {}
        for row in rows:
            last_seen = row["last_seen"]
            result[int(row["user_id"])] = {
                "last_seen": last_seen,
                "session_count": int(row["session_count"] or 0),
                "is_online": bool(last_seen and str(last_seen) >= online_cutoff),
            }
        return result

    def current_session_hash():
        tok = request.cookies.get("session_token")
        return hash_token(tok) if tok else ""

    def _row_snapshot(row):
        return {key: row[key] for key in row.keys()} if row else {}

    def _governance_feature_list_from_payload(data, *field_names):
        raw = None
        found = False
        for name in field_names:
            if name in data:
                raw = data.get(name)
                found = True
                break
        if not found or raw in (None, ""):
            return [], []
        if isinstance(raw, str):
            items = [item.strip() for item in raw.split(",")]
        elif isinstance(raw, (list, tuple, set)):
            items = list(raw)
        else:
            return [], ["restriction_features"]
        features = []
        invalid = []
        for item in items:
            key = str(item or "").strip().lower().replace("-", "_")
            if not key:
                continue
            if key not in FEATURE_LABELS:
                invalid.append(str(item)[:60])
                continue
            if key not in features:
                features.append(key)
        return features, invalid

    def _optional_positive_points(data, *field_names, max_value=100000):
        for name in field_names:
            if name not in data:
                continue
            raw = data.get(name)
            if raw in (None, ""):
                return None, None
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None, f"{name} 格式錯誤"
            if value <= 0 or value > max_value:
                return None, f"{name} 需介於 1 到 {max_value}"
            return value, None
        return None, None

    def _optional_positive_hours(data, field_name, default=72, max_value=24 * 30):
        raw = data.get(field_name, default)
        if raw in (None, ""):
            return default, None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default, f"{field_name} 格式錯誤"
        if value <= 0 or value > max_value:
            return default, f"{field_name} 需介於 1 到 {max_value}"
        return value, None

    def _sanction_label(data, target):
        parts = []
        if "role" in data:
            parts.append(f"角色 {target['role'] or '-'} -> {normalize_text(data.get('role')) or '-'}")
        if "sanction_status" in data:
            parts.append(f"處分狀態 {target['sanction_status'] or 'none'} -> {normalize_text(data.get('sanction_status')) or 'none'}")
        if data.get("sanction_until"):
            parts.append(f"處分期限 {normalize_text(data.get('sanction_until'))}")
        if "status" in data:
            parts.append(f"帳號狀態 {target['status'] or '-'} -> {normalize_text(data.get('status')) or '-'}")
        if "base_level" in data or "member_level" in data:
            next_level = normalize_text(data.get("base_level") or data.get("member_level"))
            if next_level:
                parts.append(f"會員等級 {target['base_level'] or target['member_level'] or '-'} -> {next_level}")
        features, _invalid = _governance_feature_list_from_payload(data, "restriction_features", "immediate_restriction_features")
        if features:
            labels = "、".join(FEATURE_LABELS.get(key, key) for key in features)
            parts.append(f"限制功能：{labels}")
        fine_amount, _fine_err = _optional_positive_points(data, "fine_amount_points", "fine_points", "violation_fine_points")
        if fine_amount:
            parts.append(f"罰款 {fine_amount} 點")
        return "；".join(parts) or "會員管理處分"

    def _is_punitive_member_update(data, target):
        if "sanction_status" in data:
            next_sanction = normalize_text(data.get("sanction_status")) or "none"
            if next_sanction in {"restricted", "suspended"} and next_sanction != (target["sanction_status"] or "none"):
                return True
        if "status" in data:
            next_status = normalize_text(data.get("status"))
            if next_status and next_status not in {"active", "pending"} and next_status != target["status"]:
                return True
        next_level = normalize_text(data.get("base_level") or data.get("member_level"))
        if next_level in {"restricted", "suspended"} and next_level != (target["base_level"] or target["member_level"]):
            return True
        return False

    def _send_member_governance_notice(conn, *, actor, actor_role, target, previous, action_label, reason, points=0, existing_violation_id=None, appealable=True):
        if not target or target["username"] == "root":
            return
        record_admin_sanction_notice(
            conn,
            actor=actor,
            target=target,
            previous=previous,
            violation_id=existing_violation_id,
            action_label=action_label,
            reason=reason,
            appealable=appealable,
        )

    def _send_admin_sanction_notice(conn, *, actor, actor_role, target, previous, data):
        reason = normalize_text(data.get("level_update_reason") or data.get("reason") or "會員管理處分")
        _send_member_governance_notice(
            conn,
            actor=actor,
            actor_role=actor_role,
            target=target,
            previous=previous,
            action_label=_sanction_label(data, target),
            reason=reason,
            points=0,
            appealable=_is_punitive_member_update(data, target),
        )

    def _can_review_password_reset(actor_role, target):
        if not target or target["target_username"] == "root":
            return False, "root 密碼不可透過管理審核流程重設"
        if role_rank(actor_role) >= role_rank("super_admin"):
            return True, ""
        if role_rank(actor_role) >= role_rank("manager") and target["target_role"] == "user":
            return True, ""
        return False, "管理員只能審核一般用戶的密碼重設"

    def _apply_reviewed_password_reset(conn, *, request_row, actor, actor_role, new_credential, note):
        allowed, msg = _can_review_password_reset(actor_role, request_row)
        if not allowed:
            return msg, 403
        if request_row["status"] != "pending":
            return "此密碼重設申請已處理", 409
        ok, msg, strength = validate_configured_password(new_credential)
        if not ok:
            return msg, 400
        current_row = conn.execute(
            "SELECT password_hash FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (request_row["user_id"],),
        ).fetchone()
        if current_row and verify_password(current_row["password_hash"], new_credential):
            return "新密碼不可與目前密碼相同", 400
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
            (request_row["user_id"], hash_password(new_credential), now),
        )
        conn.execute(
            """
            UPDATE users
            SET password_strength_score=?, password_changed_at=?, must_change_password=1,
                is_default_password=0, failed_login_count=0, locked_until=NULL, updated_at=?
            WHERE id=?
            """,
            (strength["score"], now, now, request_row["user_id"]),
        )
        trim_password_history(conn, request_row["user_id"])
        delete_csrf_tokens_for_username(request_row["username"])
        mark_password_reset_review_request(
            conn,
            request_id=request_row["id"],
            status="approved",
            reviewer_user_id=actor["id"],
            note=note,
        )
        revoke_user_sessions(request_row["user_id"])
        return "", 200

    def ensure_avatar_user_columns(conn):
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "avatar_file_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_file_id TEXT")
        if "avatar_crop_json" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_crop_json TEXT")
        if "preferred_landing_module" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN preferred_landing_module TEXT")
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")

    PUBLIC_PROFILE_ACCOUNT_FIELDS = {"nickname", "real_name", "birthdate", "phone"}

    def _profile_public_account_field_keys(conn, user_id):
        ensure_user_profile_schema(conn)
        row = conn.execute(
            "SELECT public_account_fields_json FROM user_profiles WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        raw = row["public_account_fields_json"] if row else "[]"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            parsed = []
        if not isinstance(parsed, list):
            parsed = []
        out = []
        for item in parsed:
            key = str(item or "").strip()
            if key in PUBLIC_PROFILE_ACCOUNT_FIELDS and key not in out:
                out.append(key)
        return out

    def _profile_viewer_value(viewer, key, default=None):
        if viewer is None:
            return default
        getter = getattr(viewer, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                pass
        try:
            return viewer[key]
        except Exception:
            return getattr(viewer, key, default)

    def _profile_payload_allows_public_account_fields(payload, viewer):
        if not payload:
            return False
        owner = viewer and int(_profile_viewer_value(viewer, "id") or -1) == int(payload.get("id") or -2)
        if owner:
            return True
        privileged = viewer and (
            _profile_viewer_value(viewer, "username") == "root"
            or _profile_viewer_value(viewer, "role") in {"manager", "super_admin"}
        )
        if privileged:
            return True
        visibility = payload.get("profile_visibility") or "public"
        if visibility == "public":
            return True
        if visibility == "members" and viewer:
            return True
        if visibility == "friends" and payload.get("friend_status") == "accepted":
            return True
        return False

    def _enrich_profile_public_account_fields(conn, payload, viewer):
        if not payload:
            return payload
        keys = _profile_public_account_field_keys(conn, payload.get("id"))
        owner = viewer and int(_profile_viewer_value(viewer, "id") or -1) == int(payload.get("id") or -2)
        if owner:
            payload["profile_public_account_fields"] = keys
        payload["public_account_fields"] = {}
        if not keys or not _profile_payload_allows_public_account_fields(payload, viewer):
            return payload
        row = conn.execute(
            "SELECT nickname, real_name, birthdate, phone FROM users WHERE id=?",
            (int(payload.get("id")),),
        ).fetchone()
        if not row:
            return payload
        values = {
            "nickname": decrypt_field(row["nickname"]),
            "real_name": decrypt_field(row["real_name"]),
            "birthdate": decrypt_field(row["birthdate"]),
            "phone": decrypt_field(row["phone"]),
        }
        payload["public_account_fields"] = {
            key: values.get(key) or ""
            for key in keys
            if values.get(key)
        }
        return payload

    @app.route("/api/users/me/profile", methods=["GET", "PUT"], strict_slashes=False)
    @require_csrf_by_method
    def api_me_profile():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            if request.method == "PUT":
                data = request.get_json(silent=True) or {}
                if not isinstance(data, dict):
                    return json_resp({"ok":False,"msg":"請求內容格式錯誤"}), 400
                profile, error = update_profile(conn, actor=actor, data=data)
                if error:
                    return json_resp({"ok":False,"msg":error}), 400
                conn.commit()
                audit("USER_PROFILE_UPDATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"user_id={actor['id']}")
                payload = get_profile_payload(conn, target_user_id=actor["id"], viewer=actor) or profile
                payload = _enrich_profile_public_account_fields(conn, payload, actor)
                return json_resp({"ok":True,"profile":payload,"msg":"個人資料已更新"})
            payload = get_profile_payload(conn, target_user_id=actor["id"], viewer=actor)
            payload = _enrich_profile_public_account_fields(conn, payload, actor)
            if not payload:
                return json_resp({"ok":False,"msg":"找不到個人資料"}), 404
            conn.commit()
            return json_resp({"ok":True,"profile":payload})
        finally:
            conn.close()

    @app.route("/api/users/<int:user_id>/profile", methods=["GET"], strict_slashes=False)
    @require_csrf_safe
    def api_user_profile(user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            payload = get_profile_payload(conn, target_user_id=user_id, viewer=actor)
            payload = _enrich_profile_public_account_fields(conn, payload, actor)
            if not payload:
                return json_resp({"ok":False,"msg":"找不到使用者"}), 404
            conn.commit()
            return json_resp({"ok":True,"profile":payload})
        finally:
            conn.close()

    @app.route("/api/users/me/friend-code/rotate", methods=["POST"], strict_slashes=False)
    @require_csrf
    def api_rotate_friend_code():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            code = rotate_friend_code(conn, actor["id"])
            conn.commit()
            audit("USER_FRIEND_CODE_ROTATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"user_id={actor['id']}")
            return json_resp({"ok":True,"friend_code":code,"msg":"好友代碼已重新產生"})
        finally:
            conn.close()

    @app.route("/api/friends", methods=["GET"], strict_slashes=False)
    @require_csrf_safe
    def api_friends():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            state = list_friend_state(conn, actor)
            return json_resp({"ok":True, **state})
        finally:
            conn.close()

    @app.route("/api/users/target-options", methods=["GET"], strict_slashes=False)
    @require_csrf_safe
    def api_user_target_options():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            users = list_targetable_users(
                conn,
                actor,
                context=request.args.get("context") or "personal",
                query=request.args.get("q") or "",
                limit=request.args.get("limit") or 80,
            )
            conn.commit()
            return json_resp({"ok":True,"users":users})
        finally:
            conn.close()

    @app.route("/api/friends/requests", methods=["GET"], strict_slashes=False)
    @require_csrf_safe
    def api_friend_requests():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            state = list_friend_state(conn, actor)
            return json_resp({"ok":True,"incoming":state["incoming"],"outgoing":state["outgoing"]})
        finally:
            conn.close()

    @app.route("/api/friends/request", methods=["POST"], strict_slashes=False)
    @require_csrf
    def api_friend_request():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg":"請求內容格式錯誤"}), 400
        username = normalize_text(data.get("username"))
        target_user_id = data.get("user_id")
        if not username and not target_user_id:
            return json_resp({"ok":False,"msg":"請指定使用者"}), 400
        if target_user_id:
            try:
                target_user_id = int(target_user_id)
            except (TypeError, ValueError):
                return json_resp({"ok":False,"msg":"使用者 ID 格式錯誤"}), 400
        conn = get_db()
        try:
            result, msg, status = create_friend_request(
                conn,
                actor,
                target_user_id=target_user_id if target_user_id else None,
                username=username if username else None,
            )
            if status < 400:
                conn.commit()
                target = (result or {}).get("target") or {}
                audit("FRIEND_REQUESTED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"target_user_id={target.get('id')},status={(result or {}).get('status')}")
                return json_resp({"ok":True,"msg":msg,"request":result})
            return json_resp({"ok":False,"msg":msg}), status
        finally:
            conn.close()

    @app.route("/api/friends/requests/<int:request_id>/accept", methods=["POST"], strict_slashes=False)
    @require_csrf
    def api_friend_request_accept(request_id):
        return _review_friend_request_api(request_id, "accept")

    @app.route("/api/friends/requests/<int:request_id>/reject", methods=["POST"], strict_slashes=False)
    @require_csrf
    def api_friend_request_reject(request_id):
        return _review_friend_request_api(request_id, "reject")

    def _review_friend_request_api(request_id, decision):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            result, msg, status = review_friend_request(conn, actor, request_id=request_id, decision=decision)
            if status < 400:
                conn.commit()
                audit("FRIEND_REQUEST_REVIEWED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"request_id={request_id},decision={decision}")
                return json_resp({"ok":True,"msg":msg,"request":result})
            return json_resp({"ok":False,"msg":msg}), status
        finally:
            conn.close()

    @app.route("/api/friends/add-by-code", methods=["POST"], strict_slashes=False)
    @require_csrf
    def api_friend_add_by_code():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg":"請求內容格式錯誤"}), 400
        conn = get_db()
        try:
            result, msg, status = accept_friend_by_code(conn, actor, friend_code=data.get("friend_code"))
            if status < 400:
                conn.commit()
                target = (result or {}).get("target") or {}
                audit("FRIEND_ADDED_BY_CODE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"target_user_id={target.get('id')}")
                return json_resp({"ok":True,"msg":msg,"request":result})
            return json_resp({"ok":False,"msg":msg}), status
        finally:
            conn.close()

    @app.route("/api/friends/<int:friend_user_id>", methods=["DELETE"], strict_slashes=False)
    @require_csrf
    def api_friend_remove(friend_user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            ok, msg, status = remove_friend(conn, actor, friend_user_id=friend_user_id)
            if ok:
                conn.commit()
                audit("FRIEND_REMOVED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"friend_user_id={friend_user_id}")
                return json_resp({"ok":True,"msg":msg})
            return json_resp({"ok":False,"msg":msg}), status
        finally:
            conn.close()

    @app.route("/api/friends/<int:target_user_id>/block", methods=["POST"], strict_slashes=False)
    @require_csrf
    def api_friend_block(target_user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            result, msg, status = block_user(conn, actor, target_user_id=target_user_id)
            if status < 400:
                conn.commit()
                target = (result or {}).get("target") or {}
                audit("FRIEND_BLOCKED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"target_user_id={target.get('id')}")
                return json_resp({"ok":True,"msg":msg,"block":result})
            return json_resp({"ok":False,"msg":msg}), status
        finally:
            conn.close()

    @app.route("/api/friends/<int:target_user_id>/block", methods=["DELETE"], strict_slashes=False)
    @require_csrf
    def api_friend_unblock(target_user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            ok, msg, status = unblock_user(conn, actor, target_user_id=target_user_id)
            if ok:
                conn.commit()
                audit("FRIEND_UNBLOCKED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"target_user_id={target_user_id}")
                return json_resp({"ok":True,"msg":msg})
            return json_resp({"ok":False,"msg":msg}), status
        finally:
            conn.close()

    @app.route("/api/users/<int:target_user_id>/follow", methods=["POST"], strict_slashes=False)
    @require_csrf
    def api_user_follow(target_user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            result, msg, status = follow_user(conn, actor, target_user_id=target_user_id)
            if status < 400:
                conn.commit()
                target = (result or {}).get("target") or {}
                audit("USER_FOLLOWED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"target_user_id={target.get('id')}")
                profile = get_profile_payload(conn, target_user_id=target_user_id, viewer=actor)
                return json_resp({"ok":True,"msg":msg,"follow":result,"profile":profile})
            return json_resp({"ok":False,"msg":msg}), status
        finally:
            conn.close()

    @app.route("/api/users/<int:target_user_id>/follow", methods=["DELETE"], strict_slashes=False)
    @require_csrf
    def api_user_unfollow(target_user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        conn = get_db()
        try:
            ok, msg, status = unfollow_user(conn, actor, target_user_id=target_user_id)
            if ok:
                conn.commit()
                audit("USER_UNFOLLOWED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"target_user_id={target_user_id}")
                profile = get_profile_payload(conn, target_user_id=target_user_id, viewer=actor)
                return json_resp({"ok":True,"msg":msg,"profile":profile})
            return json_resp({"ok":False,"msg":msg}), status
        finally:
            conn.close()

    @app.route("/api/account/sessions", methods=["GET"])
    @require_csrf_safe
    def account_sessions():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if not is_feature_enabled("feature_account_security_enabled"):
            return json_resp({"ok":False,"msg":"帳號安全功能目前已關閉"}), 503

        token_hash = current_session_hash()
        now = datetime.now().isoformat()
        conn = get_readonly_auth_db()
        try:
            rows = conn.execute(
                "SELECT id, token_hash, ip_address, user_agent, device_info, ip_country, expires_at, is_revoked, revoked_at, last_seen, created_at "
                "FROM sessions WHERE user_id=? ORDER BY COALESCE(last_seen, created_at) DESC",
                (actor["id"],)
            ).fetchall()
            sessions = []
            for row in rows:
                sessions.append({
                    "id": row["id"],
                    "ip_address": row["ip_address"] or "",
                    "user_agent": row["user_agent"] or "",
                    "device_info": parse_device_info(row["device_info"] if "device_info" in row.keys() else ""),
                    "ip_country": row["ip_country"] if "ip_country" in row.keys() else None,
                    "expires_at": row["expires_at"],
                    "is_revoked": bool(row["is_revoked"]),
                    "revoked_at": row["revoked_at"],
                    "last_seen": row["last_seen"],
                    "created_at": row["created_at"],
                    "is_current": bool(token_hash and row["token_hash"] == token_hash),
                    "is_active": bool(not row["is_revoked"] and row["expires_at"] > now),
                })
            return json_resp({"ok":True,"sessions":sessions})
        finally:
            if conn is not None:
                conn.close()

    @app.route("/api/account/sessions/<int:session_id>", methods=["DELETE"])
    @require_csrf
    def account_session_revoke(session_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if not is_feature_enabled("feature_account_security_enabled"):
            return json_resp({"ok":False,"msg":"帳號安全功能目前已關閉"}), 503

        token_hash = current_session_hash()
        conn = get_auth_db()
        try:
            row = conn.execute(
                "SELECT id, token_hash, is_revoked FROM sessions WHERE id=? AND user_id=?",
                (session_id, actor["id"])
            ).fetchone()
            if not row:
                return json_resp({"ok":False,"msg":"找不到 session"}), 404
            conn.execute(
                "UPDATE sessions SET is_revoked=1, revoked_at=? WHERE id=? AND user_id=?",
                (datetime.now().isoformat(), session_id, actor["id"])
            )
            conn.commit()
            audit("ACCOUNT_SESSION_REVOKED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"session_id={session_id},self={row['token_hash'] == token_hash}")
            resp = json_resp({"ok":True,"msg":"裝置 session 已登出","current_revoked":row["token_hash"] == token_hash})
            if row["token_hash"] == token_hash:
                resp.delete_cookie("session_token", path="/", samesite=SESSION_COOKIE_SAMESITE, secure=SESSION_COOKIE_SECURE)
                resp.delete_cookie("csrf_token", path="/", samesite=SESSION_COOKIE_SAMESITE, secure=SESSION_COOKIE_SECURE)
            return resp
        finally:
            if conn is not None:
                conn.close()

    @app.route("/api/account/sessions/logout-all", methods=["POST"])
    @require_csrf
    def account_sessions_logout_all():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if not is_feature_enabled("feature_account_security_enabled"):
            return json_resp({"ok":False,"msg":"帳號安全功能目前已關閉"}), 503

        token_hash = current_session_hash()
        keep_current = bool(request.get_json(silent=True) or {}) and bool((request.get_json(silent=True) or {}).get("keep_current"))
        sql = "UPDATE sessions SET is_revoked=1, revoked_at=? WHERE user_id=? AND is_revoked=0"
        params = [datetime.now().isoformat(), actor["id"]]
        if keep_current and token_hash:
            sql += " AND token_hash<>?"
            params.append(token_hash)
        conn = get_auth_db()
        try:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            audit("ACCOUNT_SESSIONS_REVOKED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"count={cur.rowcount},keep_current={keep_current}")
            resp = json_resp({"ok":True,"msg":"已登出指定裝置","revoked_count":cur.rowcount,"current_revoked":not keep_current})
            if not keep_current:
                resp.delete_cookie("session_token", path="/", samesite=SESSION_COOKIE_SAMESITE, secure=SESSION_COOKIE_SECURE)
                resp.delete_cookie("csrf_token", path="/", samesite=SESSION_COOKIE_SAMESITE, secure=SESSION_COOKIE_SECURE)
            return resp
        finally:
            conn.close()

    @app.route("/api/admin/users", methods=["GET","POST"])
    @require_csrf_by_method
    def admin_users():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401

        if actor["username"] == "root":
            actor_role = "super_admin"
        else:
            actor_role = actor["role"]

        # Manager can only view; super_admin can add / modify / delete
        if request.method == "GET":
            if role_rank(actor_role) < role_rank("manager"):
                return json_resp({"ok":False,"msg":"權限不足"}), 403
            include_deleted = str(request.args.get("include_deleted") or "").strip().lower() in {"1", "true", "yes"}
            page = parse_positive_int(request.args.get("page", 1), default=1, min_value=1)
            if page is None:
                return json_resp({"ok":False,"msg":"page 參數格式錯誤"}), 400
            page_size = parse_positive_int(request.args.get("page_size", 25), default=25, min_value=1, max_value=100)
            if page_size is None:
                return json_resp({"ok":False,"msg":"page_size 參數格式錯誤"}), 400
            search_query = normalize_text(request.args.get("q"))[:80]
            conn = get_db()
            try:
                ensure_member_level_user_columns(conn)
                ensure_avatar_user_columns(conn)
                conn.commit()
                where_parts = []
                params = []
                if not include_deleted:
                    where_parts.append("COALESCE(status, 'active') <> 'deleted'")
                if search_query:
                    where_parts.append(
                        "(LOWER(username) LIKE ? OR LOWER(COALESCE(email, '')) LIKE ? "
                        "OR LOWER(COALESCE(nickname, '')) LIKE ? OR LOWER(COALESCE(real_name, '')) LIKE ?)"
                    )
                    pattern = f"%{search_query.lower()}%"
                    params.extend([pattern, pattern, pattern, pattern])
                where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
                total_row = conn.execute(
                    f"SELECT COUNT(*) AS c FROM users{where}",
                    tuple(params),
                ).fetchone()
                total = int(total_row["c"] if total_row else 0)
                total_pages = max(1, (total + int(page_size) - 1) // int(page_size))
                if int(page) > total_pages:
                    page = total_pages
                offset = (int(page) - 1) * int(page_size)
                rows = conn.execute(
                    "SELECT id, username, email, nickname, real_name, birthdate, id_number, phone, status, role, "
                    "member_level, base_level, effective_level, trust_score, points, reputation, violation_score, "
                    "sanction_status, sanction_until, level_updated_at, level_updated_by, level_update_reason, "
                    "password_strength_score, avatar_file_id, avatar_crop_json, blocked_until, violation_count "
                    f"FROM users{where} ORDER BY id ASC LIMIT ? OFFSET ?",
                    tuple(params + [int(page_size), offset]),
                ).fetchall()
                role_rows = conn.execute(
                    "SELECT role, COUNT(*) AS c FROM users "
                    "WHERE COALESCE(status, 'active') <> 'deleted' GROUP BY role"
                ).fetchall()
                role_counts = {str(row["role"] or "user"): int(row["c"] or 0) for row in role_rows}
                try:
                    auth_conn = get_readonly_auth_db()
                    try:
                        sessions_by_user = active_session_map(auth_conn, [int(row["id"]) for row in rows])
                    finally:
                        auth_conn.close()
                except Exception:
                    sessions_by_user = {}
                try:
                    friend_ids = accepted_friend_ids(conn, actor["id"])
                except Exception:
                    friend_ids = set()
                official_hot_by_user = {}
                deposit_address_by_user = {}
                if points_service is not None:
                    points_conn = None
                    try:
                        points_conn = points_service.get_db() if hasattr(points_service, "get_db") else get_db()
                        member_wallet_user_ids = []
                        for row in rows:
                            if str(row["username"] or "") == "root":
                                continue
                            uid = int(row["id"] or 0)
                            if uid > 0:
                                member_wallet_user_ids.append(uid)
                        wallet_balance_by_address = {}
                        if member_wallet_user_ids and _table_exists(points_conn, "points_wallet_identities"):
                            placeholders = ",".join("?" for _ in member_wallet_user_ids)
                            wallet_rows = points_conn.execute(
                                f"""
                                SELECT user_id, address, status
                                FROM points_wallet_identities
                                WHERE wallet_type='official_hot'
                                  AND custody_mode='server_hot'
                                  AND status IN ('pending_backup', 'active')
                                  AND user_id IN ({placeholders})
                                ORDER BY user_id ASC, is_primary DESC, id ASC
                                """,
                                tuple(member_wallet_user_ids),
                            ).fetchall()
                            wallet_addresses = [
                                str(wallet_row["address"] or "").strip().lower()
                                for wallet_row in wallet_rows
                                if str(wallet_row["address"] or "").strip()
                            ]
                            if _table_exists(points_conn, "points_chain_deposit_addresses"):
                                deposit_rows = points_conn.execute(
                                    f"""
                                    SELECT user_id, address
                                    FROM points_chain_deposit_addresses
                                    WHERE status='active'
                                      AND user_id IN ({placeholders})
                                    ORDER BY user_id ASC, id ASC
                                    """,
                                    tuple(member_wallet_user_ids),
                                ).fetchall()
                            else:
                                deposit_rows = []
                            if wallet_addresses and _table_exists(points_conn, "points_wallet_identity_balances"):
                                balance_placeholders = ",".join("?" for _ in wallet_addresses)
                                branch_row = None
                                if _table_exists(points_conn, "points_wallet_identity_balance_state"):
                                    branch_row = points_conn.execute(
                                        """
                                        SELECT chain_branch
                                        FROM points_wallet_identity_balance_state
                                        ORDER BY updated_at DESC
                                        LIMIT 1
                                        """
                                    ).fetchone()
                                balance_params = list(wallet_addresses)
                                branch_filter = ""
                                if branch_row and branch_row["chain_branch"]:
                                    branch_filter = "AND chain_branch=?"
                                    balance_params.append(str(branch_row["chain_branch"]))
                                balance_rows = points_conn.execute(
                                    f"""
                                    SELECT wallet_address, available_points, frozen_points, pending_outgoing_points
                                    FROM points_wallet_identity_balances
                                    WHERE wallet_address IN ({balance_placeholders}) {branch_filter}
                                    """,
                                    tuple(balance_params),
                                ).fetchall()
                                for balance_row in balance_rows:
                                    wallet_balance_by_address[str(balance_row["wallet_address"] or "").strip().lower()] = {
                                        "balance": int(balance_row["available_points"] or 0),
                                        "frozen": int(balance_row["frozen_points"] or 0),
                                        "pending_outgoing": int(balance_row["pending_outgoing_points"] or 0),
                                    }
                        else:
                            wallet_rows = []
                            deposit_rows = []
                        for deposit_row in deposit_rows:
                            uid = int(deposit_row["user_id"] or 0)
                            if uid > 0 and uid not in deposit_address_by_user:
                                deposit_address_by_user[uid] = str(deposit_row["address"] or "")
                        for wallet_row in wallet_rows:
                            uid = int(wallet_row["user_id"] or 0)
                            if uid <= 0:
                                continue
                            address_key = str(wallet_row["address"] or "").strip().lower()
                            balance = wallet_balance_by_address.get(address_key, {})
                            if (
                                not balance
                                and hasattr(points_service, "_wallet_identity_balances_for_user")
                                and not _table_exists(points_conn, "points_wallet_identity_balances")
                            ):
                                try:
                                    balance_state = points_service._wallet_identity_balances_for_user(points_conn, uid)
                                    balance = (balance_state.get("balances") or {}).get(address_key, {})
                                except Exception:
                                    balance = {}
                            official_hot_by_user.setdefault(uid, []).append({
                                "address": wallet_row["address"],
                                "status": wallet_row["status"],
                                "points_balance": int(balance.get("balance") or 0),
                                "points_frozen": int(balance.get("frozen") or 0),
                                "pending_outgoing_points": int(balance.get("pending_outgoing") or 0),
                            })
                    except Exception:
                        if points_conn is not None:
                            try:
                                points_conn.rollback()
                            except Exception:
                                pass
                        official_hot_by_user = {}
                        deposit_address_by_user = {}
                    finally:
                        if points_conn is not None:
                            points_conn.close()
                data = []
                for r in rows:
                    item = user_public_payload(r, include_sensitive=False)
                    item["is_friend"] = bool(int(r["id"]) in friend_ids)
                    item["is_official"] = bool(item.get("username") == "root" or item.get("role") in {"manager", "super_admin"})
                    official_hot_wallets = official_hot_by_user.get(int(r["id"]), [])
                    item["official_hot_wallets"] = official_hot_wallets
                    item["official_hot_wallet_address"] = official_hot_wallets[0]["address"] if official_hot_wallets else ""
                    item["official_hot_wallet_deposit_address"] = deposit_address_by_user.get(int(r["id"]), "")
                    item["official_hot_wallet_balance"] = official_hot_wallets[0]["points_balance"] if official_hot_wallets else 0
                    item["official_hot_wallet_frozen"] = official_hot_wallets[0]["points_frozen"] if official_hot_wallets else 0
                    item["official_hot_wallet_pending_outgoing"] = official_hot_wallets[0]["pending_outgoing_points"] if official_hot_wallets else 0
                    session_info = sessions_by_user.get(int(r["id"]), {})
                    item["is_online"] = bool(session_info.get("is_online"))
                    item["online_status"] = "online" if item["is_online"] else "offline"
                    item["online_last_seen"] = session_info.get("last_seen") or ""
                    item["active_session_count"] = int(session_info.get("session_count") or 0)
                    data.append(item)
                conn.commit()
            finally:
                conn.close()
            return json_resp({
                "ok": True,
                "users": data,
                "pagination": {
                    "page": int(page),
                    "page_size": int(page_size),
                    "total": total,
                    "total_pages": total_pages,
                    "sort": "id",
                    "order": "asc",
                    "q": search_query,
                },
                "role_counts": role_counts,
                "can_manage": role_rank(actor_role) >= role_rank("super_admin"),
                "can_review": role_rank(actor_role) >= role_rank("manager")
            })

        # POST — super_admin only
        if role_rank(actor_role) < role_rank("super_admin"):
            return json_resp({"ok":False,"msg":"只有最高權限可新增帳號"}), 403

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400

        username = normalize_text(data.get("username"))
        credential_text = data.get("password", "") if isinstance(data.get("password"), str) else ""
        credential_confirm = data.get("password_confirm","") if isinstance(data.get("password_confirm"), str) else ""
        nickname = normalize_text(data.get("nickname"))
        real_name = normalize_text(data.get("real_name"))
        id_number = normalize_text(data.get("id_number"))
        birthdate = parse_birthdate(data.get("birthdate"))
        phone = normalize_text(data.get("phone"))
        role = normalize_text(data.get("role")) or "user"
        status = normalize_text(data.get("status")) or "active"
        identity_governance_enabled = is_feature_enabled("feature_identity_governance_enabled")
        member_level = normalize_text(data.get("member_level")) or ("trusted" if role == "user" else "normal")

        if role not in ROLE_RANK:
            return json_resp({"ok":False,"msg":"不支援的角色"}), 400
        if role == "manager":
            limit = manager_seat_limit()
            if count_role("manager") >= limit:
                return json_resp({"ok":False,"msg":f"管理者已達上限（{limit} 人）"}), 409
        if role == "super_admin" and count_role("super_admin") >= MAX_EXTRA_SUPER_ADMINS:
            return json_resp({"ok":False,"msg":f"非 root 最高管理者已達上限（{MAX_EXTRA_SUPER_ADMINS} 人）"}), 409
        if status not in ACCOUNT_STATUSES:
            return json_resp({"ok":False,"msg":"帳號狀態錯誤"}), 400
        if not identity_governance_enabled and "member_level" in data:
            return json_resp({"ok":False,"msg":"身份治理功能目前已關閉"}), 503
        if member_level not in MEMBER_LEVELS:
            return json_resp({"ok":False,"msg":"會員等級錯誤"}), 400
        if not username or len(username) < 3:
            return json_resp({"ok":False,"msg":"帳號至少 3 字元"}), 400
        if len(username) > 32:
            return json_resp({"ok":False,"msg":"帳號最長 32 字元"}), 400
        if not re.fullmatch(r"[a-zA-Z0-9_\-]+", username):
            return json_resp({"ok":False,"msg":"帳號只能包含英文、數字、底線、減號"}), 400
        if not nickname:
            return json_resp({"ok":False,"msg":"暱稱不可為空"}), 400
        if id_number and not validate_id_number(id_number):
            return json_resp({"ok":False,"msg":"身分證格式錯誤"}), 400
        if data.get("birthdate") and not birthdate:
            return json_resp({"ok":False,"msg":"生日需為 YYYY-MM-DD"}), 400
        if phone and not validate_phone(phone):
            return json_resp({"ok":False,"msg":"電話格式錯誤"}), 400
        if credential_text != credential_confirm:
            return json_resp({"ok":False,"msg":"兩次輸入的密碼不一致"}), 400

        # 超級管理者可指定任意密碼（繞過複雜度規則，但仍截斷長度）
        is_super = actor_role == "super_admin"
        if credential_text:
            credential_text = credential_text[:128]  # 截斷防止超長密碼
            if not is_super:
                ok, msg, strength = validate_configured_password(credential_text)
                if not ok:
                    return json_resp({"ok":False,"msg":msg,"password_strength":strength}), 400
        else:
            return json_resp({"ok":False,"msg":"新建帳號必須指定密碼"}), 400
        strength = score_password_strength(credential_text)

        conn = get_db()
        try:
            existing = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if existing:
                return json_resp({"ok":False,"msg":"帳號已存在"}), 409
            now = datetime.now().isoformat()
            cur = conn.execute(
                "INSERT INTO users (username, nickname, real_name, birthdate, id_number, phone, role, status, member_level, base_level, effective_level, password_strength_score, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username, encrypt_field(nickname), encrypt_field(real_name), encrypt_field(birthdate), encrypt_field(id_number), encrypt_field(phone), role, status, member_level, member_level, member_level, strength["score"], now, now)
            )
            conn.execute(
                "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
                (cur.lastrowid, hash_password(credential_text), now)
            )
            new_user_id = cur.lastrowid
            trim_password_history(conn, cur.lastrowid)
            conn.commit()
            if points_service and role in {"manager", "super_admin"} and username != "root":
                try:
                    points_service.award_admin_initial_grant(
                        user_id=new_user_id,
                        actor={"id": actor["id"], "username": actor["username"], "role": actor_role},
                    )
                except Exception as exc:
                    audit("POINTS_ADMIN_INITIAL_GRANT_FAILED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(),
                          detail=f"target={username}, error={exc}")
            audit("ADMIN_CREATE_USER", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"target={username}, role={role}")
            return json_resp({"ok":True,"msg":"帳號已建立"})
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if "users.username" in str(exc):
                return json_resp({"ok":False,"msg":"帳號已存在"}), 409
            raise
        finally:
            conn.close()

    @app.route("/api/admin/users/<int:user_id>", methods=["GET"])
    @require_csrf_safe
    def admin_user_item(user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        is_self = actor["id"] == user_id
        if role_rank(actor_role) < role_rank("manager") and not is_self:
            return json_resp({"ok":False,"msg":"權限不足"}), 403
        include_sensitive = is_self or actor["username"] == "root"

        conn = get_db()
        try:
            ensure_member_level_user_columns(conn)
            ensure_avatar_user_columns(conn)
            ensure_user_profile_schema(conn)
            target = conn.execute(
                "SELECT id, username, nickname, real_name, birthdate, id_number, phone, role, status, "
                "member_level, base_level, effective_level, trust_score, points, reputation, violation_score, "
                "sanction_status, sanction_until, level_updated_at, level_updated_by, level_update_reason, "
                "password_strength_score, avatar_file_id, avatar_crop_json, blocked_until, violation_count FROM users WHERE id=?",
                (user_id,)
            ).fetchone()
            if not target:
                return json_resp({"ok":False,"msg":"找不到帳號"}), 404
            payload = {
                "ok": True,
                "user": user_public_payload(target, include_sensitive=include_sensitive)
            }
            if is_self:
                payload["user"]["display_timezone"] = get_profile_display_timezone(conn, user_id)
                payload["appearance_settings"] = get_profile_appearance(conn, user_id)
            return json_resp(payload)
        finally:
            conn.close()

    def _parse_avatar_crop(raw):
        if not raw:
            return {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return {}
        if not isinstance(raw, dict):
            return {}
        crop = {}
        for key in ("x", "y", "width", "height"):
            try:
                value = int(raw.get(key))
            except Exception:
                continue
            if value < 0:
                continue
            crop[key] = min(value, 10000)
        if not {"x", "y", "width", "height"} <= set(crop):
            return {}
        if crop.get("width", 0) <= 0 or crop.get("height", 0) <= 0:
            return {}
        try:
            rotation = int(raw.get("rotation", 0) or 0)
        except Exception:
            rotation = 0
        rotation = ((round(rotation / 90) * 90) % 360 + 360) % 360
        if rotation:
            crop["rotation"] = rotation
        return crop

    def _avatar_crop_response(path, crop, mimetype):
        try:
            from io import BytesIO
            from PIL import Image, ImageOps
        except Exception:
            return None
        try:
            Image.MAX_IMAGE_PIXELS = 25_000_000
            with Image.open(path) as img:
                if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                    return None
                clean = ImageOps.exif_transpose(img)
                rotation = int(crop.get("rotation", 0) or 0) if crop else 0
                if rotation:
                    clean = clean.rotate(-rotation, expand=True)
                image_width, image_height = clean.size
                if image_width <= 0 or image_height <= 0:
                    return None
                if not crop:
                    side = min(image_width, image_height)
                    crop = {
                        "x": max(0, (image_width - side) // 2),
                        "y": max(0, (image_height - side) // 2),
                        "width": side,
                        "height": side,
                    }
                x = max(0, min(int(crop.get("x", 0) or 0), image_width - 1))
                y = max(0, min(int(crop.get("y", 0) or 0), image_height - 1))
                crop_width = max(0, min(int(crop.get("width", 0) or 0), image_width - x))
                crop_height = max(0, min(int(crop.get("height", 0) or 0), image_height - y))
                side = min(crop_width, crop_height)
                if side <= 0:
                    return None
                left = x + max(0, (crop_width - side) // 2)
                top = y + max(0, (crop_height - side) // 2)
                cropped = clean.crop((left, top, left + side, top + side))
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
                cropped = cropped.resize((512, 512), resample)
                output = BytesIO()
                use_png = "png" in str(mimetype or "").lower() or cropped.mode in {"RGBA", "LA", "P"}
                if use_png:
                    if cropped.mode not in {"RGBA", "LA"}:
                        cropped = cropped.convert("RGBA")
                    cropped.save(output, format="PNG", optimize=True)
                    out_mimetype = "image/png"
                    download_name = "avatar.png"
                else:
                    if cropped.mode not in {"RGB", "L"}:
                        cropped = cropped.convert("RGB")
                    cropped.save(output, format="JPEG", quality=90, optimize=True)
                    out_mimetype = "image/jpeg"
                    download_name = "avatar.jpg"
                output.seek(0)
                return send_file(output, mimetype=out_mimetype, download_name=download_name, max_age=3600)
        except Exception:
            return None

    @app.route("/api/admin/users/<int:user_id>/avatar", methods=["POST"])
    @require_csrf
    def user_avatar_upload(user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        is_self = int(actor["id"]) == int(user_id)
        if not is_self and role_rank(actor_role) < role_rank("super_admin"):
            return json_resp({"ok": False, "msg": "只有 root 可修改他人頭像"}), 403
        file_storage = request.files.get("file")
        cloud_file_id = str(request.form.get("cloud_file_id") or request.form.get("existing_file_id") or "").strip()
        if not file_storage and not cloud_file_id:
            return json_resp({"ok": False, "msg": "缺少 file 或 cloud_file_id"}), 400
        conn = get_db()
        try:
            ensure_member_level_user_columns(conn)
            ensure_avatar_user_columns(conn)
            ensure_cloud_drive_attachment_schema(conn)
            target = conn.execute("SELECT id, username, role, member_level, effective_level, sanction_status, avatar_file_id FROM users WHERE id=?", (user_id,)).fetchone()
            if not target:
                return json_resp({"ok": False, "msg": "找不到帳號"}), 404
            previous_avatar_file_id = str(target["avatar_file_id"] or "").strip()
            crop = _parse_avatar_crop(request.form.get("crop_json"))
            if cloud_file_id:
                file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=? AND deleted_at IS NULL", (cloud_file_id,)).fetchone()
                if not file_row:
                    return json_resp({"ok": False, "msg": "找不到雲端圖片"}), 404
                if int(file_row["owner_user_id"]) != int(user_id):
                    return json_resp({"ok": False, "msg": "只能選擇該帳號自己的雲端硬碟檔案"}), 403
                mimetype = (file_row["mime_type_plain_for_public"] or "").lower()
                filename = (file_row["original_filename_plain_for_public"] or "").lower()
                allowed_mimetypes = {"image/jpeg", "image/png", "image/gif"}
                allowed_exts = {".jpg", ".jpeg", ".png", ".gif"}
                if mimetype not in allowed_mimetypes and not any(filename.endswith(ext) for ext in allowed_exts):
                    return json_resp({"ok": False, "msg": "頭像僅支援 JPEG / PNG / GIF"}), 400
                if file_row["scan_status"] not in {"clean", "not_required"}:
                    return json_resp({"ok": False, "msg": "雲端圖片尚未通過安全掃描"}), 400
                if str(file_row["privacy_mode"] or "") == "e2ee":
                    return json_resp({"ok": False, "msg": "E2EE 頭像需先在瀏覽器解密後上傳頭像衍生圖，伺服器不能直接讀取原始 E2EE 檔案"}), 400
                if is_server_encrypted_file(file_row) and not server_file_fernet:
                    return json_resp({"ok": False, "msg": "伺服器端加密金鑰尚未載入，無法使用此圖片作為頭像"}), 503
                mark_avatar_file_asset(conn, file_id=cloud_file_id, owner_user_id=user_id)
                conn.execute(
                    "UPDATE users SET avatar_file_id=?, avatar_crop_json=?, updated_at=? WHERE id=?",
                    (cloud_file_id, json.dumps(crop, ensure_ascii=False), datetime.now().isoformat(), user_id),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO cloud_file_refs (
                        id, file_id, owner_user_id, context_type, context_id, attached_by, created_at, permission_snapshot_json
                    ) VALUES (?, ?, ?, 'avatar', ?, ?, ?, ?)
                    """,
                    (
                        f"avatar_{user_id}_{cloud_file_id}",
                        cloud_file_id,
                        user_id,
                        str(user_id),
                        actor["id"],
                        datetime.now().isoformat(),
                        json.dumps({"public_avatar": True, "crop": crop, "source": "cloud_drive"}, ensure_ascii=False),
                    ),
                )
                old_avatar_cleanup = (
                    delete_avatar_file_if_unreferenced(conn, actor=dict(target), file_id=previous_avatar_file_id, storage_root=storage_root)
                    if previous_avatar_file_id and previous_avatar_file_id != cloud_file_id else {"deleted": False, "reason": "same_or_empty"}
                )
                conn.commit()
                audit("USER_AVATAR_CLOUD_SELECT", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"target_id={user_id},file_id={cloud_file_id}")
                return json_resp({"ok": True, "avatar_file_id": cloud_file_id, "avatar_crop": crop, "file": dict(file_row), "old_avatar_cleanup": old_avatar_cleanup})
            mimetype = (getattr(file_storage, "mimetype", "") or "").lower()
            if mimetype not in {"image/jpeg", "image/png", "image/gif"}:
                return json_resp({"ok": False, "msg": "頭像僅支援 JPEG / PNG / GIF"}), 400
            # Enforce extension allowlist (L-1: path traversal + extension validation)
            filename = (getattr(file_storage, "filename", "") or "").lower()
            allowed_exts = {".jpg", ".jpeg", ".png", ".gif"}
            if not any(filename.endswith(ext) for ext in allowed_exts):
                return json_resp({"ok": False, "msg": "頭像僅支援 JPEG / PNG / GIF 副檔名"}), 400
            rule = get_member_level_rule(conn, target["effective_level"] or target["member_level"]) if get_member_level_rule else None
            result, msg = store_cloud_upload(
                conn,
                actor=dict(target),
                member_rule=rule,
                storage_root=storage_root,
                file_storage=file_storage,
                privacy_mode="standard_plain",
                scan_now=True,
                system_asset_type="avatar",
                quota_exempt=True,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            if result.get("scan_status") not in {"clean", "not_required"}:
                conn.rollback()
                return json_resp({"ok": False, "msg": "頭像未通過安全掃描"}), 400
            conn.execute(
                "UPDATE users SET avatar_file_id=?, avatar_crop_json=?, updated_at=? WHERE id=?",
                (result["file_id"], json.dumps(crop, ensure_ascii=False), datetime.now().isoformat(), user_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO cloud_file_refs (
                    id, file_id, owner_user_id, context_type, context_id, attached_by, created_at, permission_snapshot_json
                ) VALUES (?, ?, ?, 'avatar', ?, ?, ?, ?)
                """,
                (
                    f"avatar_{user_id}_{result['file_id']}",
                    result["file_id"],
                    user_id,
                    str(user_id),
                    actor["id"],
                    datetime.now().isoformat(),
                    json.dumps({"public_avatar": True, "crop": crop}, ensure_ascii=False),
                ),
            )
            old_avatar_cleanup = (
                delete_avatar_file_if_unreferenced(conn, actor=dict(target), file_id=previous_avatar_file_id, storage_root=storage_root)
                if previous_avatar_file_id and previous_avatar_file_id != result["file_id"] else {"deleted": False, "reason": "same_or_empty"}
            )
            conn.commit()
            audit("USER_AVATAR_UPLOAD", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"target_id={user_id},file_id={result['file_id']}")
            return json_resp({"ok": True, "avatar_file_id": result["file_id"], "avatar_crop": crop, "file": result, "old_avatar_cleanup": old_avatar_cleanup})
        finally:
            conn.close()

    @app.route("/api/admin/users/<int:user_id>/avatar", methods=["GET"])
    @require_csrf_safe
    def user_avatar_get(user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        # Avatars are public identity assets inside authenticated areas such as
        # chat, DM, and forums. Anonymous users are still blocked above.
        conn = get_db()
        try:
            ensure_member_level_user_columns(conn)
            ensure_avatar_user_columns(conn)
            ensure_cloud_drive_attachment_schema(conn)
            row = conn.execute(
                """
                SELECT f.storage_path, f.mime_type_plain_for_public, f.scan_status, f.privacy_mode, f.deleted_at,
                       u.avatar_crop_json
                FROM users u
                JOIN uploaded_files f ON f.id=u.avatar_file_id
                WHERE u.id=?
                """,
                (user_id,),
            ).fetchone()
            if not row or row["deleted_at"]:
                return json_resp({"ok": False, "msg": "尚未設定頭像"}), 404
            if row["privacy_mode"] != "e2ee" and row["scan_status"] not in {"clean", "not_required"}:
                return json_resp({"ok": False, "msg": "頭像尚未通過安全掃描"}), 403
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists() or not path.is_file():
                return json_resp({"ok": False, "msg": "頭像檔案不存在"}), 404
            if str(row["privacy_mode"] or "") == "e2ee":
                return json_resp({"ok": False, "msg": "此頭像來源是 E2EE 原始檔，請先以瀏覽器解密後重新上傳頭像衍生圖"}), 409
            crop = _parse_avatar_crop(row["avatar_crop_json"])
            avatar_source = path
            if is_server_encrypted_file(row):
                if not server_file_fernet:
                    return json_resp({"ok": False, "msg": "伺服器端加密金鑰尚未載入"}), 503
                from io import BytesIO
                avatar_source = BytesIO(decrypt_server_encrypted_bytes(path, server_file_fernet))
            cropped = _avatar_crop_response(avatar_source, crop, row["mime_type_plain_for_public"])
            if cropped is not None:
                return cropped
            if is_server_encrypted_file(row):
                avatar_source.seek(0)
                return send_file(avatar_source, mimetype=row["mime_type_plain_for_public"] or "application/octet-stream", download_name="avatar")
            return send_file(path, mimetype=row["mime_type_plain_for_public"] or "application/octet-stream")
        finally:
            conn.close()

    @app.route("/api/admin/users/<int:user_id>", methods=["PUT", "DELETE"])
    @require_csrf
    def admin_user_item_mutate(user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        is_self = actor["id"] == user_id
        if request.method == "DELETE" and role_rank(actor_role) < role_rank("super_admin"):
            return json_resp({"ok":False,"msg":"只有最高權限可刪除帳號"}), 403
        if request.method == "PUT" and not is_self and role_rank(actor_role) < role_rank("manager"):
            return json_resp({"ok":False,"msg":"只有管理者以上可修改他人帳號"}), 403

        conn = get_db()
        try:
            ensure_member_level_user_columns(conn)
            ensure_avatar_user_columns(conn)
            target = conn.execute(
                "SELECT id, username, nickname, real_name, birthdate, id_number, phone, role, status, "
                "member_level, base_level, effective_level, trust_score, points, reputation, violation_score, "
                "sanction_status, sanction_until, level_updated_at, level_updated_by, level_update_reason, "
                "password_strength_score, avatar_file_id, avatar_crop_json, blocked_until, violation_count FROM users WHERE id=?",
                (user_id,)
            ).fetchone()
            if not target:
                return json_resp({"ok":False,"msg":"找不到帳號"}), 404
            if request.method == "PUT" and target["username"] == "root" and actor["username"] != "root":
                return json_resp({"ok":False,"msg":"只有 root 可修改 root 帳號"}), 403

            if request.method == "DELETE":
                if target["username"] == "root":
                    return json_resp({"ok":False,"msg":"不可刪除最高管理者帳號"}), 403
                if target["username"] == actor["username"]:
                    return json_resp({"ok":False,"msg":"不可刪除目前登入中的帳號"}), 403
                target_username = target["username"]
                try:
                    cleanup = soft_delete_user_row(conn, user_id=user_id, username=target_username)
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    audit("ADMIN_DELETE_USER_FAILED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(),
                          detail=f"target_id={user_id}, integrity_error={exc}")
                    return json_resp({"ok":False,"msg":"刪除帳號失敗：仍有未清理的帳號關聯資料，請重新整理後再試"}), 409
                except sqlite3.Error as exc:
                    conn.rollback()
                    audit("ADMIN_DELETE_USER_FAILED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(),
                          detail=f"target_id={user_id}, error={exc}")
                    return json_resp({"ok":False,"msg":f"刪除帳號失敗：{exc}"}), 500
                msg = "帳號已停用並清理雲端硬碟"
                if cleanup.get("warnings"):
                    msg = "帳號已停用，但部分附屬資料清理失敗，請查看 cleanup.warnings"
                delete_result = {"cleanup": cleanup, "msg": msg, "target_username": target_username}
                conn.close()
                conn = None
                try:
                    revoke_user_sessions(user_id)
                except Exception as exc:
                    cleanup.setdefault("warnings", []).append({"scope": "revoke_user_sessions", "error": str(exc)})
                try:
                    delete_csrf_tokens_for_username(target_username)
                except Exception as exc:
                    cleanup.setdefault("warnings", []).append({"scope": "delete_csrf_tokens", "error": str(exc)})
                audit("ADMIN_DELETE_USER", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"target_id={user_id},cleanup={json.dumps(cleanup, ensure_ascii=False, sort_keys=True)}")
                if cleanup.get("warnings"):
                    delete_result["msg"] = "帳號已停用，但部分附屬資料清理失敗，請查看 cleanup.warnings"
                return json_resp({"ok":True,"msg":delete_result["msg"],"cleanup":cleanup})

            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
            if not isinstance(data, dict):
                return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400
            governance_restriction_features, invalid_governance_features = _governance_feature_list_from_payload(
                data,
                "restriction_features",
                "immediate_restriction_features",
            )
            if invalid_governance_features:
                return json_resp({"ok":False,"msg":"不支援的功能限制：" + "、".join(invalid_governance_features[:5])}), 400
            governance_fine_amount, governance_fine_error = _optional_positive_points(
                data,
                "fine_amount_points",
                "fine_points",
                "violation_fine_points",
            )
            if governance_fine_error:
                return json_resp({"ok":False,"msg":governance_fine_error}), 400
            governance_fine_due_hours, governance_due_error = _optional_positive_hours(
                data,
                "fine_due_hours",
                default=72,
                max_value=24 * 30,
            )
            if governance_due_error:
                return json_resp({"ok":False,"msg":governance_due_error}), 400
            governance_disposition_requested = bool(governance_restriction_features or governance_fine_amount)
            if governance_disposition_requested:
                if not is_feature_enabled("feature_member_governance_enabled"):
                    return json_resp({"ok":False,"msg":"會員治理功能目前已關閉"}), 503
                if is_self:
                    return json_resp({"ok":False,"msg":"不可對自己建立會員治理處分"}), 403
                if target["username"] == "root":
                    return json_resp({"ok":False,"msg":"不可對 root 建立會員治理處分"}), 403
                if role_rank(actor_role) < role_rank("manager"):
                    return json_resp({"ok":False,"msg":"只有管理者以上可建立會員治理處分"}), 403
                if actor_role == "manager" and target["role"] != "user":
                    return json_resp({"ok":False,"msg":"管理員只能對一般用戶建立會員治理處分"}), 403
            if request.method == "PUT" and not is_self and role_rank(actor_role) < role_rank("super_admin"):
                allowed_manager_keys = {
                    "member_level",
                    "base_level",
                    "level_update_reason",
                    "reason",
                    "restriction_features",
                    "immediate_restriction_features",
                    "fine_amount_points",
                    "fine_points",
                    "violation_fine_points",
                    "fine_due_hours",
                    "governance_disposition_reason",
                }
                if set(data.keys()) - allowed_manager_keys:
                    return json_resp({"ok":False,"msg":"管理員只能調整一般用戶會員等級；角色、狀態與個資需 root"}), 403

            revoke_sessions_needed = False
            level_changed = False
            previous_target = _row_snapshot(target)
            governance_notice_needed = False
            governance_action_results = []
            updates = []
            params = []
            if "nickname" in data:
                updates.append("nickname=?")
                params.append(encrypt_field(normalize_text(data["nickname"])))
            if "real_name" in data:
                updates.append("real_name=?")
                params.append(encrypt_field(normalize_text(data["real_name"])))
            if "id_number" in data:
                val = normalize_text(data["id_number"])
                if not validate_id_number(val):
                    return json_resp({"ok":False,"msg":"身分證格式錯誤"}), 400
                updates.append("id_number=?")
                params.append(encrypt_field(val))
            if "birthdate" in data:
                val = parse_birthdate(data["birthdate"])
                if not val:
                    return json_resp({"ok":False,"msg":"生日需為 YYYY-MM-DD"}), 400
                updates.append("birthdate=?")
                params.append(encrypt_field(val))
            if "phone" in data:
                val = normalize_text(data["phone"])
                if not validate_phone(val):
                    return json_resp({"ok":False,"msg":"電話格式錯誤"}), 400
                updates.append("phone=?")
                params.append(encrypt_field(val))
            if "status" in data:
                if is_self:
                    return json_resp({"ok":False,"msg":"不可自行變更帳號狀態"}), 403
                val = normalize_text(data["status"])
                if val not in ACCOUNT_STATUSES:
                    return json_resp({"ok":False,"msg":"帳號狀態錯誤"}), 400
                governance_notice_needed = True
                updates.append("status=?")
                params.append(val)
                if val != "active":
                    revoke_sessions_needed = True
            if "member_level" in data or "base_level" in data or "sanction_status" in data or "sanction_until" in data:
                if not is_feature_enabled("feature_identity_governance_enabled"):
                    return json_resp({"ok":False,"msg":"身份治理功能目前已關閉"}), 503
                if is_self:
                    return json_resp({"ok":False,"msg":"不可自行變更會員等級"}), 403
                requested_sanction_change = "sanction_status" in data or "sanction_until" in data
                val = normalize_text(data.get("base_level") or data.get("member_level") or target["base_level"] or target["member_level"])
                manager_level_change = (
                    role_rank(actor_role) >= role_rank("manager")
                    and target["role"] == "user"
                    and not requested_sanction_change
                    and val in {"newbie", "normal", "trusted", "vip"}
                )
                if role_rank(actor_role) < role_rank("super_admin") and not manager_level_change:
                    return json_resp({"ok":False,"msg":"管理員只能調整一般用戶的 newbie/normal/trusted/vip 會員等級；處分與角色需 root"}), 403
                level_user, err = apply_member_level_change(
                    conn,
                    user_id,
                    actor=actor["username"],
                    source="root" if actor["username"] == "root" else "admin",
                    base_level=val,
                    sanction_status=normalize_text(data.get("sanction_status")) if "sanction_status" in data else None,
                    sanction_until=normalize_text(data.get("sanction_until")) if data.get("sanction_until") else None,
                    reason=normalize_text(data.get("level_update_reason") or data.get("reason") or "admin user update"),
                )
                if err:
                    return json_resp({"ok":False,"msg":err}), 400
                level_changed = True
                governance_notice_needed = True
            if "role" in data:
                if is_self:
                    return json_resp({"ok":False,"msg":"不可自行變更角色"}), 403
                if actor["username"] != "root":
                    return json_resp({"ok":False,"msg":"只有 root 可變更角色"}), 403
                val = normalize_text(data["role"])
                if val not in ROLE_RANK:
                    return json_resp({"ok":False,"msg":"不支援的角色"}), 400
                if target["username"] == "root" and val != "super_admin":
                    return json_resp({"ok":False,"msg":"最高管理者角色不可變更"}), 403
                if val == "manager" and target["role"] != "manager":
                    limit = manager_seat_limit()
                    if count_role("manager") >= limit:
                        return json_resp({"ok":False,"msg":f"管理者已達上限（{limit} 人）"}), 409
                if val == "super_admin" and target["role"] != "super_admin" and count_role("super_admin") >= MAX_EXTRA_SUPER_ADMINS:
                    return json_resp({"ok":False,"msg":f"非 root 最高管理者已達上限（{MAX_EXTRA_SUPER_ADMINS} 人）"}), 409
                updates.append("role=?")
                params.append(val)
                governance_notice_needed = True
                if role_rank(val) > role_rank(target["role"]):
                    revoke_sessions_needed = True
            if "password" in data and isinstance(data["password"], str) and data["password"]:
                action_name = "password_change" if is_self else "admin_password_reset"
                limit = 5 if is_self else 20
                blocked, info = check_user_rate_limit(actor["id"], action_name, max_req=limit, window_sec=3600)
                if blocked:
                    return json_resp({"ok":False,"msg":f"密碼操作過於頻繁（每小時最多 {info['limit']} 次）"}), 429
                pw = data["password"][:128]
                pw_confirm = data.get("password_confirm","") if isinstance(data.get("password_confirm"), str) else ""
                if not pw_confirm:
                    return json_resp({"ok":False,"msg":"請再次輸入新密碼"}), 400
                if pw_confirm != pw:
                    return json_resp({"ok":False,"msg":"兩次密碼輸入不一致"}), 400
                current_row = conn.execute(
                    "SELECT password_hash FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT 1",
                    (user_id,)
                ).fetchone()
                if is_self:
                    current_pw = data.get("current_password","") if isinstance(data.get("current_password"), str) else ""
                    if not current_pw:
                        return json_resp({"ok":False,"msg":"請輸入目前密碼"}), 400
                    if not current_row or not verify_password(current_row["password_hash"], current_pw):
                        return json_resp({"ok":False,"msg":"目前密碼錯誤"}), 403
                if current_row and verify_password(current_row["password_hash"], pw):
                    return json_resp({"ok":False,"msg":"新密碼不可與目前密碼相同"}), 400
                target_is_root = normalize_text(target["username"]).lower() == "root"
                must_follow_password_policy = not target_is_root
                if must_follow_password_policy:
                    ok, msg, strength = validate_configured_password(pw)
                    if not ok:
                        return json_resp({"ok":False,"msg":msg,"password_strength":strength}), 400
                strength = score_password_strength(pw)
                conn.execute(
                    "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
                    (user_id, hash_password(pw), datetime.now().isoformat())
                )
                updates.append("password_strength_score=?")
                params.append(strength["score"])
                updates.append("password_changed_at=?")
                params.append(datetime.now().isoformat())
                updates.append("must_change_password=0")
                updates.append("is_default_password=0")
                trim_password_history(conn, user_id)
                revoke_sessions_needed = True
            if "username" in data:
                return json_resp({"ok":False,"msg":"不允許變更帳號名稱"}), 400

            pw_payload = "password" in data and isinstance(data["password"], str) and data["password"]
            if governance_disposition_requested:
                ensure_violation_fine_schema(conn)
                source_ref = "member_governance:" + uuid.uuid4().hex
                governance_reason = normalize_text(
                    data.get("governance_disposition_reason")
                    or data.get("level_update_reason")
                    or data.get("reason")
                    or "會員治理處分"
                )[:500] or "會員治理處分"
                for feature_key in governance_restriction_features:
                    restriction = create_user_feature_restriction(
                        conn,
                        user_id=target["id"],
                        feature_key=feature_key,
                        source_type="member_governance",
                        source_ref=source_ref,
                        reason=governance_reason,
                        created_by=actor["username"],
                        metadata={
                            "actor_role": actor_role,
                            "target_role": target["role"],
                            "source": "admin_user_update",
                        },
                    )
                    governance_action_results.append({"type": "feature_restriction", "restriction": restriction})
                if governance_fine_amount:
                    fine, created = create_violation_fine(
                        conn,
                        user_id=target["id"],
                        username=target["username"],
                        amount_points=governance_fine_amount,
                        reason=governance_reason,
                        created_by=actor["username"],
                        due_hours=governance_fine_due_hours,
                        restriction_features=governance_restriction_features or None,
                        policy_key="member_governance",
                        metadata={
                            "source": "admin_user_update",
                            "source_ref": source_ref,
                            "actor_role": actor_role,
                        },
                    )
                    governance_action_results.append({"type": "violation_fine", "fine": fine, "created": created})
                governance_notice_needed = True

            if not updates and not pw_payload and not level_changed and not governance_action_results:
                return json_resp({"ok":False,"msg":"未提供可更新欄位"}), 400

            if pw_payload and not updates:
                conn.commit()
            elif updates:
                updates.append("updated_at=?")
                params.append(datetime.now().isoformat())
                params.append(user_id)
                sql = "UPDATE users SET " + ", ".join(updates) + " WHERE id=?"
                conn.execute(sql, params)
                conn.commit()
            elif level_changed:
                conn.commit()
            if governance_notice_needed and target["username"] != "root" and not is_self:
                appealable_notice = _is_punitive_member_update(data, target)
                _send_member_governance_notice(
                    conn,
                    actor=actor,
                    actor_role=actor_role,
                    target=target,
                    previous=previous_target,
                    action_label=_sanction_label(data, target),
                    reason=normalize_text(data.get("level_update_reason") or data.get("reason") or "會員權益變更"),
                    points=0,
                    appealable=appealable_notice,
                )
                conn.commit()
            if revoke_sessions_needed:
                revoke_user_sessions(
                    user_id,
                    notify_security_event=not is_self,
                    detail="self_password_change" if is_self else "user_sessions_revoked",
                )
                delete_csrf_tokens_for_username(target["username"])
            audit("ADMIN_UPDATE_USER", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"target_id={user_id},self={is_self}")
            payload = {"ok":True,"msg":"帳號已更新"}
            if governance_action_results:
                payload["governance_actions"] = governance_action_results
            return json_resp(payload)
        finally:
            if conn is not None:
                conn.close()

    @app.route("/api/admin/users/<int:user_id>/review-registration", methods=["POST"])
    @require_csrf
    def review_registration(user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if role_rank(actor_role) < role_rank("manager"):
            return json_resp({"ok":False,"msg":"只有管理者以上可審核註冊"}), 403

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400

        action = normalize_text(data.get("action"))
        if action not in ("approve", "reject"):
            return json_resp({"ok":False,"msg":"不支援的審核動作"}), 400

        conn = get_db()
        try:
            target = conn.execute(
                "SELECT id, username, status FROM users WHERE id=?",
                (user_id,)
            ).fetchone()
            if not target:
                return json_resp({"ok":False,"msg":"找不到帳號"}), 404
            if target["status"] != "pending":
                return json_resp({"ok":False,"msg":"此帳號目前不是待審核狀態"}), 409

            if action == "approve":
                new_status = "active"
                conn.execute(
                    "UPDATE users SET status=?, updated_at=? WHERE id=?",
                    (new_status, datetime.now().isoformat(), user_id)
                )
                ensure_user_official_room_membership(conn, user_id)
                msg = "註冊申請已核准"
            else:
                new_status = "deleted"
                target_username = target["username"]
                try:
                    hard_delete_user_row(conn, user_id=user_id, username=target_username)
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    audit("REGISTRATION_REVIEW_FAILED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(),
                          detail=f"target={target['username']},action={action},integrity_error={exc}")
                    return json_resp({"ok":False,"msg":"駁回註冊申請失敗：仍有未清理的帳號關聯資料，請重新整理後再試"}), 409
                except sqlite3.Error as exc:
                    conn.rollback()
                    audit("REGISTRATION_REVIEW_FAILED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(),
                          detail=f"target={target['username']},action={action},error={exc}")
                    return json_resp({"ok":False,"msg":f"駁回註冊申請失敗：{exc}"}), 500
                msg = "註冊申請已駁回並刪除帳號"
            if action == "approve":
                conn.commit()
            audit(
                "REGISTRATION_REVIEWED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"target={target['username']},action={action}"
            )
            if action == "reject":
                conn.close()
                conn = None
                revoke_user_sessions(user_id)
                delete_csrf_tokens_for_username(target_username)
            return json_resp({"ok":True,"msg":msg,"status":new_status})
        finally:
            if conn is not None:
                conn.close()

    @app.route("/api/admin/password-reset-requests", methods=["GET"])
    @require_csrf_safe
    def admin_password_reset_requests():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if role_rank(actor_role) < role_rank("manager"):
            return json_resp({"ok": False, "msg": "只有管理者以上可審核密碼重設"}), 403
        status = normalize_text(request.args.get("status") or "pending") or "pending"
        if status not in {"pending", "approved", "rejected", "all"}:
            return json_resp({"ok": False, "msg": "status 不支援"}), 400
        conn = get_db()
        try:
            ensure_account_recovery_schema(conn)
            rows = list_password_reset_review_requests(conn, status=status, limit=100)
            requests_payload = []
            for row in rows:
                allowed, _ = _can_review_password_reset(actor_role, row)
                requests_payload.append({
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "username": row["target_username"],
                    "role": row["target_role"],
                    "target_status": row["target_status"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "requested_ip": row["requested_ip"],
                    "reviewed_at": row["reviewed_at"],
                    "reviewed_by": row["reviewed_by_username"],
                    "review_note": row["review_note"],
                    "can_review": bool(allowed and row["status"] == "pending"),
                })
            return json_resp({"ok": True, "requests": requests_payload})
        finally:
            conn.close()

    @app.route("/api/admin/password-reset-requests/<int:request_id>/approve", methods=["POST"])
    @require_csrf
    def admin_password_reset_approve(request_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if role_rank(actor_role) < role_rank("manager"):
            return json_resp({"ok": False, "msg": "只有管理者以上可審核密碼重設"}), 403
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        temporary_credential = data.get("temporary_password", "") if isinstance(data.get("temporary_password"), str) else ""
        temporary_credential_confirm = data.get("temporary_password_confirm", "") if isinstance(data.get("temporary_password_confirm"), str) else ""
        note = normalize_text(data.get("note") or "password reset review approved")
        if not temporary_credential:
            return json_resp({"ok": False, "msg": "請輸入臨時密碼"}), 400
        if temporary_credential != temporary_credential_confirm:
            return json_resp({"ok": False, "msg": "兩次密碼輸入不一致"}), 400
        conn = get_db()
        try:
            ensure_account_recovery_schema(conn)
            row = get_password_reset_review_request(conn, request_id)
            if not row:
                return json_resp({"ok": False, "msg": "找不到密碼重設申請"}), 404
            err, status_code = _apply_reviewed_password_reset(
                conn,
                request_row=row,
                actor=actor,
                actor_role=actor_role,
                new_credential=temporary_credential[:128],
                note=note,
            )
            if err:
                return json_resp({"ok": False, "msg": err}), status_code
            conn.commit()
            audit(
                "PASSWORD_RESET_REVIEW_APPROVED",
                get_client_ip(),
                user=actor["username"],
                ua=get_ua(),
                success=True,
                detail=f"request_id={request_id},target={row['target_username']}",
            )
            return json_resp({"ok": True, "msg": "密碼已重設，該帳號下次登入必須修改密碼"})
        finally:
            conn.close()

    @app.route("/api/admin/password-reset-requests/<int:request_id>/reject", methods=["POST"])
    @require_csrf
    def admin_password_reset_reject(request_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if role_rank(actor_role) < role_rank("manager"):
            return json_resp({"ok": False, "msg": "只有管理者以上可審核密碼重設"}), 403
        try:
            data = request.get_json(force=True)
        except Exception:
            data = {}
        note = normalize_text((data or {}).get("note") or "password reset review rejected")
        conn = get_db()
        try:
            ensure_account_recovery_schema(conn)
            row = get_password_reset_review_request(conn, request_id)
            if not row:
                return json_resp({"ok": False, "msg": "找不到密碼重設申請"}), 404
            allowed, msg = _can_review_password_reset(actor_role, row)
            if not allowed:
                return json_resp({"ok": False, "msg": msg}), 403
            if row["status"] != "pending":
                return json_resp({"ok": False, "msg": "此密碼重設申請已處理"}), 409
            mark_password_reset_review_request(
                conn,
                request_id=request_id,
                status="rejected",
                reviewer_user_id=actor["id"],
                note=note,
            )
            conn.commit()
            audit(
                "PASSWORD_RESET_REVIEW_REJECTED",
                get_client_ip(),
                user=actor["username"],
                ua=get_ua(),
                success=True,
                detail=f"request_id={request_id},target={row['target_username']}",
            )
            return json_resp({"ok": True, "msg": "密碼重設申請已駁回"})
        finally:
            conn.close()

    @app.route("/api/admin/users/<int:user_id>/block", methods=["POST"])
    @require_csrf
    def admin_user_block(user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if role_rank(actor_role) < role_rank("manager"):
            return json_resp({"ok":False,"msg":"權限不足"}), 403

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400

        action = (normalize_text(data.get("action")) or "").lower() or "block"
        minutes = data.get("minutes", 30)
        if not isinstance(minutes, int):
            try:
                minutes = int(minutes)
            except Exception:
                minutes = 30
        if minutes < 1: minutes = 1
        if minutes > 1440: minutes = 1440

        conn = get_db()
        try:
            target = conn.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,)).fetchone()
            if not target:
                return json_resp({"ok":False,"msg":"找不到帳號"}), 404
            if int(target["id"]) == int(actor["id"]):
                return json_resp({"ok": False, "msg": "不能封鎖目前登入中的自己"}), 400
            if target["username"] == "root" and actor_role != "super_admin":
                return json_resp({"ok":False,"msg":"無權限封鎖最高管理者"}), 403
            if actor["username"] != "root" and role_rank(actor_role) <= role_rank(target["role"]):
                return json_resp({"ok":False,"msg":"無法封鎖同級或更高階帳號"}), 403

            if action == "unblock":
                conn.execute("UPDATE users SET status='active', blocked_until=NULL WHERE id=?", (user_id,))
                conn.commit()
                audit("ADMIN_UNBLOCK_USER", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                      detail=f"target_id={user_id}")
                return json_resp({"ok":True,"msg":"帳號已解除封鎖"})

            blocked_until = (datetime.now() + timedelta(minutes=minutes)).isoformat()
            conn.execute("UPDATE users SET status='inactive', blocked_until=? WHERE id=?", (blocked_until, user_id))
            conn.commit()
            revoke_user_sessions(user_id)
            audit("ADMIN_BLOCK_USER", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"target_id={user_id}, minutes={minutes}")
            return json_resp({"ok":True,"msg":f"帳號已封鎖 {minutes} 分鐘"})
        finally:
            conn.close()

    # ── 推廣 / 降級（promote / demote）───────────────────────────────────────────────
    @app.route("/api/admin/users/<int:user_id>/promote", methods=["POST"])
    @require_csrf
    def admin_user_promote(user_id):
        """僅超級管理者可推廣帳號"""
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if actor["username"] != "root":
            return json_resp({"ok":False,"msg":"只有 root 可晉升帳號"}), 403

        conn = get_db()
        try:
            target = conn.execute(
                "SELECT id, username, role, status, member_level, base_level, effective_level, sanction_status, sanction_until FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not target:
                return json_resp({"ok":False,"msg":"找不到帳號"}), 404

            from_role = target["role"]
            if from_role == "super_admin":
                return json_resp({"ok":False,"msg":"最高管理者無需推廣"}), 400
            if from_role == "manager" and role_rank(actor_role) < role_rank("super_admin"):
                return json_resp({"ok":False,"msg":"只有最高管理者可推廣管理者"}), 403

            to_role = "manager" if from_role == "user" else "super_admin"
            if to_role == "manager":
                limit = manager_seat_limit()
                if count_role("manager") >= limit:
                    return json_resp({"ok":False,"msg":f"管理者已達上限（{limit} 人）"}), 400
            if to_role == "super_admin" and count_role("super_admin") >= MAX_EXTRA_SUPER_ADMINS:
                return json_resp({"ok":False,"msg":f"非 root 最高管理者已達上限（{MAX_EXTRA_SUPER_ADMINS} 人）"}), 409

            previous_target = _row_snapshot(target)
            conn.execute("UPDATE users SET role=?, violation_count=0, updated_at=? WHERE id=?",
                         (to_role, datetime.now().isoformat(), user_id))
            conn.commit()
            conn.close()
            conn = None
            warnings = []
            try:
                delete_csrf_tokens_for_username(target["username"])
            except Exception as exc:
                warnings.append({"scope": "delete_csrf_tokens", "error": str(exc)})
            if points_service and to_role in {"manager", "super_admin"} and target["username"] != "root":
                try:
                    points_service.award_admin_initial_grant(
                        user_id=user_id,
                        actor={"id": actor["id"], "username": actor["username"], "role": actor_role},
                    )
                except Exception as exc:
                    warnings.append({"scope": "award_admin_initial_grant", "error": str(exc)})
                    audit("POINTS_ADMIN_INITIAL_GRANT_FAILED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(),
                          detail=f"target_id={user_id}, error={exc}")
            try:
                notice_conn = get_db()
                try:
                    _send_member_governance_notice(
                        notice_conn,
                        actor=actor,
                        actor_role=actor_role,
                        target=target,
                        previous=previous_target,
                        action_label=f"角色 {from_role} -> {to_role}",
                        reason="root 晉升帳號",
                        points=0,
                        appealable=False,
                    )
                    notice_conn.commit()
                finally:
                    notice_conn.close()
            except Exception as exc:
                warnings.append({"scope": "member_governance_notice", "error": str(exc)})
                audit("USER_PROMOTE_NOTICE_FAILED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(),
                      detail=f"user_id={user_id}, error={exc}")
            audit("USER_PROMOTED", get_client_ip(), user=actor["username"],
                  success=True, detail=f"user_id={user_id} {from_role}→{to_role}")
            payload = {"ok":True,"msg":f"已升為 {ROLE_LABEL[to_role]}"}
            if warnings:
                payload["warnings"] = warnings
            return json_resp(payload)
        finally:
            if conn is not None:
                conn.close()

    @app.route("/api/admin/users/<int:user_id>/demote", methods=["POST"])
    @require_csrf
    def admin_user_demote(user_id):
        """超級管理者可將管理者降為一般用戶；可選目標狀態：restricted / suspended / inactive"""
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if actor["username"] != "root":
            return json_resp({"ok":False,"msg":"只有 root 可降級帳號"}), 403

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400
        target_status = str(data.get("target_status", "inactive")).strip()
        valid_statuses = {"restricted", "suspended", "inactive"}
        if target_status not in valid_statuses:
            return json_resp({"ok":False,"msg":f"無效的目標狀態，支援：{', '.join(valid_statuses)}"}), 400

        conn = get_db()
        try:
            target = conn.execute(
                "SELECT id, username, role, status, member_level, base_level, effective_level, sanction_status, sanction_until FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not target:
                return json_resp({"ok":False,"msg":"找不到帳號"}), 404
            if target["username"] == "root":
                return json_resp({"ok":False,"msg":"最高管理者帳號不可降級"}), 403
            from_role = target["role"]
            if from_role == "user":
                # Demote user to selected restricted/suspended/inactive state (Bug: demote)
                previous_target = _row_snapshot(target)
                conn.execute(
                    f"UPDATE users SET status=?, blocked_until=NULL, updated_at=? WHERE id=?",
                    (target_status, datetime.now().isoformat(), user_id)
                )
                conn.commit()
                conn.close()
                conn = None
                warnings = []
                try:
                    revoke_user_sessions(user_id)
                except Exception as exc:
                    warnings.append({"scope": "revoke_user_sessions", "error": str(exc)})
                audit("USER_DEACTIVATED_BY_ADMIN", get_client_ip(), user=actor["username"],
                      detail=f"user_id={user_id} demoted to {target_status}")
                try:
                    notice_conn = get_db()
                    try:
                        _send_member_governance_notice(
                            notice_conn,
                            actor=actor,
                            actor_role=actor_role,
                            target=target,
                            previous=previous_target,
                            action_label=f"帳號狀態 {target['status'] or '-'} -> {target_status}",
                            reason="root 降級帳號",
                            points=0,
                            appealable=True,
                        )
                        notice_conn.commit()
                    finally:
                        notice_conn.close()
                except Exception as exc:
                    warnings.append({"scope": "member_governance_notice", "error": str(exc)})
                payload = {"ok":True,"msg":f"帳號已降級為 {target_status}"}
                if warnings:
                    payload["warnings"] = warnings
                return json_resp(payload)
            # 管理者 → 一般用戶
            previous_target = _row_snapshot(target)
            conn.execute("UPDATE users SET role='user', violation_count=0, updated_at=? WHERE id=?",
                         (datetime.now().isoformat(), user_id))
            conn.commit()
            conn.close()
            conn = None
            audit("MANAGER_DEMOTED_BY_ADMIN", get_client_ip(), user=actor["username"],
                  detail=f"user_id={user_id} manager→user")
            warnings = []
            try:
                notice_conn = get_db()
                try:
                    _send_member_governance_notice(
                        notice_conn,
                        actor=actor,
                        actor_role=actor_role,
                        target=target,
                        previous=previous_target,
                        action_label="角色 manager -> user",
                        reason="root 降級管理員",
                        points=0,
                        appealable=True,
                    )
                    notice_conn.commit()
                finally:
                    notice_conn.close()
            except Exception as exc:
                warnings.append({"scope": "member_governance_notice", "error": str(exc)})
            payload = {"ok":True,"msg":"已降級為一般用戶"}
            if warnings:
                payload["warnings"] = warnings
            return json_resp(payload)
        finally:
            if conn is not None:
                conn.close()

    # ── 違規計點（系統自動 or 超級管理者手動）─────────────────────────────────────
    @app.route("/api/admin/users/<int:user_id>/violation", methods=["POST"])
    @require_csrf
    def admin_user_violation(user_id):
        """管理者可對一般用戶計點；超級管理者可對任何帳號計點（root 除外）"""
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if role_rank(actor_role) < role_rank("manager"):
            return json_resp({"ok":False,"msg":"權限不足"}), 403

        try:
            data = request.get_json(force=True) or {}
        except:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400

        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400
        points = parse_positive_int(data.get("points", 1))
        if points is None:
            return json_resp({"ok":False,"msg":"違規點數格式錯誤"}), 400
        reason = str(data.get("reason", "手動計點"))[:200]
        triggered_by = "super_admin" if actor_role == "super_admin" else "manager"

        conn = get_db()
        try:
            target = conn.execute(
                "SELECT id, username, role, status, member_level, base_level, effective_level, sanction_status, sanction_until FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not target:
                return json_resp({"ok":False,"msg":"找不到帳號"}), 404
            if target["username"] == "root":
                return json_resp({"ok":False,"msg":"無法對最高管理者計點"}), 403
            if actor_role == "manager" and target["role"] != "user":
                return json_resp({"ok":False,"msg":"無權對此角色計點"}), 403

            action, msg, new_count, violation_id = add_violation(
                user_id, target["username"], target["role"],
                points=points, reason=reason,
                triggered_by=triggered_by, actor_username=actor["username"],
                return_violation_id=True,
            )
            audit("VIOLATION_ADDED", get_client_ip(), user=actor["username"],
                  detail=f"target_id={user_id} action={action} points={points} reason={reason}")
            _send_member_governance_notice(
                conn,
                actor=actor,
                actor_role=actor_role,
                target=target,
                previous=_row_snapshot(target),
                action_label=f"違規點數 +{points}",
                reason=reason,
                points=points,
                existing_violation_id=violation_id,
                appealable=True,
            )
            conn.commit()
            return json_resp({"ok":True,"msg":msg,"new_count":new_count})
        finally:
            conn.close()
