from datetime import datetime

from flask import request

from services.storage.capacity_audit import audit_storage_capacity, can_allocate_storage_bytes
from services.storage.maintenance import run_storage_maintenance, storage_maintenance_status
from services.storage.quota_overrides import (
    clear_storage_quota_override,
    set_storage_quota_override,
)
from services.storage.storage_albums import (
    ensure_storage_album_schema,
    get_user_storage_summary,
    sync_user_storage_summary,
)
from services.security.upload_security import get_user_cloud_drive_usage


def register_file_admin_storage_routes(app, ctx):
    get_db = ctx["get_db"]
    get_client_ip = ctx["get_client_ip"]
    get_ua = ctx["get_ua"]
    audit = ctx["audit"]
    json_resp = ctx["json_resp"]
    require_csrf = ctx["require_csrf"]
    require_csrf_safe = ctx["require_csrf_safe"]
    get_member_level_rule = ctx["get_member_level_rule"]
    get_system_settings = ctx["get_system_settings"]
    storage_root = ctx["storage_root"]

    actor_or_401 = ctx["actor_or_401"]
    actor_value = ctx["actor_value"]
    is_root = ctx["is_root"]
    manager_or_403 = ctx["manager_or_403"]
    root_or_403 = ctx["root_or_403"]
    optional_mb_to_bytes = ctx["optional_mb_to_bytes"]
    optional_nonnegative_int = ctx["optional_nonnegative_int"]
    optional_bool = ctx["optional_bool"]
    storage_usage_for_user_row = ctx["storage_usage_for_user_row"]
    storage_summary_with_live_quota = ctx["storage_summary_with_live_quota"]

    @app.route("/api/admin/storage/summary", methods=["GET"])
    @require_csrf_safe
    def admin_storage_summary():
        actor, err = manager_or_403()
        if err:
            return err
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            file_stats = conn.execute(
                """
                SELECT COUNT(sf.id) AS storage_files,
                       COALESCE(SUM(f.size_bytes), 0) AS storage_bytes,
                       SUM(CASE WHEN sf.is_trashed=1 THEN 1 ELSE 0 END) AS trashed_files
                FROM storage_files sf
                JOIN uploaded_files f ON f.id=sf.file_id
                WHERE sf.deleted_at IS NULL AND f.deleted_at IS NULL
                """
            ).fetchone()
            album_count = conn.execute("SELECT COUNT(*) AS c FROM albums WHERE deleted_at IS NULL").fetchone()
            share_count = conn.execute("SELECT COUNT(*) AS c FROM storage_share_links WHERE revoked_at IS NULL").fetchone()
            users = conn.execute(
                """
                SELECT COUNT(*) AS users_with_storage,
                       COALESCE(SUM(used_bytes), 0) AS used_bytes,
                       COALESCE(SUM(file_count), 0) AS file_count
                FROM user_storage
                """
            ).fetchone()
            return json_resp({
                "ok": True,
                "summary": {
                    "storage_files": int(file_stats["storage_files"] or 0),
                    "storage_bytes": int(file_stats["storage_bytes"] or 0),
                    "trashed_files": int(file_stats["trashed_files"] or 0),
                    "albums": int(album_count["c"] or 0),
                    "active_share_links": int(share_count["c"] or 0),
                    "users_with_storage": int(users["users_with_storage"] or 0),
                    "tracked_used_bytes": int(users["used_bytes"] or 0),
                    "tracked_file_count": int(users["file_count"] or 0),
                },
            })
        finally:
            conn.close()

    @app.route("/api/admin/storage/users", methods=["GET"])
    @require_csrf_safe
    def admin_storage_users():
        actor, err = manager_or_403()
        if err:
            return err
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            rows = conn.execute("SELECT * FROM users ORDER BY username ASC, id ASC LIMIT 200").fetchall()
            trashed = {
                int(row["owner_user_id"]): int(row["trashed_files"] or 0)
                for row in conn.execute(
                    """
                    SELECT owner_user_id, COUNT(*) AS trashed_files
                    FROM storage_files
                    WHERE is_trashed=1 AND deleted_at IS NULL
                    GROUP BY owner_user_id
                    """
                ).fetchall()
            }
            users = []
            for row in rows:
                usage = storage_usage_for_user_row(conn, row)
                summary = get_user_storage_summary(conn, row["id"])
                usage["quota_bytes"] = int(usage["total_bytes"]) if usage.get("total_bytes") is not None else int(summary.get("quota_bytes") or 0)
                usage["reserved_bytes"] = int(summary.get("reserved_bytes") or 0)
                usage["trashed_files"] = int(trashed.get(int(row["id"]), 0))
                users.append(usage)
            users.sort(key=lambda item: (-int(item.get("used_bytes") or 0), -int(item.get("file_count") or 0), int(item.get("user_id") or 0)))
            settings = get_system_settings() if get_system_settings else {}
            capacity = audit_storage_capacity(conn, storage_root, settings=settings, include_users=False)
            return json_resp({"ok": True, "users": users, "storage_capacity": capacity})
        finally:
            conn.close()

    @app.route("/api/root/storage/users", methods=["GET"])
    @require_csrf_safe
    def root_storage_users():
        actor, err = root_or_403()
        if err:
            return err
        query = (request.args.get("q") or "").strip().lower()
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            rows = conn.execute("SELECT * FROM users ORDER BY username ASC, id ASC LIMIT 300").fetchall()
            users = []
            for row in rows:
                usage = storage_usage_for_user_row(conn, row)
                if query and query not in str(usage.get("username") or "").lower():
                    continue
                users.append(usage)
            settings = get_system_settings() if get_system_settings else {}
            capacity = audit_storage_capacity(conn, storage_root, settings=settings, include_users=False)
            return json_resp({"ok": True, "users": users, "storage_capacity": capacity})
        finally:
            conn.close()

    @app.route("/api/root/storage/users/<int:user_id>", methods=["GET"])
    @require_csrf_safe
    def root_storage_user_detail(user_id):
        actor, err = root_or_403()
        if err:
            return err
        include_trashed = request.args.get("include_trashed") in {"1", "true", "yes"}
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            row = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到帳號"}, 404)
            where = "sf.owner_user_id=? AND sf.deleted_at IS NULL AND f.deleted_at IS NULL"
            params = [int(user_id)]
            if not include_trashed:
                where += " AND sf.is_trashed=0"
            files = conn.execute(
                f"""
                SELECT sf.*, f.size_bytes, f.scan_status, f.risk_level, f.privacy_mode
                FROM storage_files sf
                JOIN uploaded_files f ON f.id=sf.file_id
                WHERE {where}
                ORDER BY sf.updated_at DESC
                LIMIT 300
                """,
                tuple(params),
            ).fetchall()
            return json_resp({
                "ok": True,
                "user": storage_usage_for_user_row(conn, row),
                "files": [dict(item) for item in files],
            })
        finally:
            conn.close()

    @app.route("/api/root/storage/users/<int:user_id>/quota-override", methods=["PUT"])
    @require_csrf
    def root_storage_set_quota_override(user_id):
        actor, err = root_or_403()
        if err:
            return err
        try:
            data = request.get_json(force=True) or {}
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}, 400)
        try:
            quota_bytes = optional_mb_to_bytes(data.get("quota_mb"), "quota_mb")
            max_file_size_bytes = optional_mb_to_bytes(data.get("max_file_size_mb"), "max_file_size_mb")
            upload_rate_limit = optional_nonnegative_int(data.get("upload_rate_limit_per_day"), "upload_rate_limit_per_day")
        except ValueError as exc:
            return json_resp({"ok": False, "msg": str(exc)}, 400)
        try:
            can_upload_override = optional_bool(data.get("can_upload"))
        except ValueError as exc:
            return json_resp({"ok": False, "msg": str(exc)}, 400)
        reason = (data.get("reason") or "").strip()
        if not reason:
            return json_resp({"ok": False, "msg": "請填寫 root 覆寫原因"}, 400)
        conn = get_db()
        try:
            target = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
            if not target:
                return json_resp({"ok": False, "msg": "找不到帳號"}, 404)
            if quota_bytes is not None and target["username"] != "root":
                current_usage = storage_usage_for_user_row(conn, target)
                current_total = int(current_usage.get("total_bytes") or 0)
                additional_bytes = max(0, int(quota_bytes) - current_total)
                if additional_bytes:
                    capacity_ok, capacity_msg, capacity_audit = can_allocate_storage_bytes(conn, storage_root, additional_bytes)
                    if not capacity_ok:
                        return json_resp({
                            "ok": False,
                            "msg": capacity_msg,
                            "storage_capacity": capacity_audit,
                        }), 409
            override = set_storage_quota_override(
                conn,
                user_id,
                enabled=bool(data.get("enabled", True)),
                quota_bytes=quota_bytes,
                max_file_size_bytes=max_file_size_bytes,
                upload_rate_limit_per_day=upload_rate_limit,
                can_upload_override=can_upload_override,
                reason=reason,
                actor_user_id=actor_value(actor, "id"),
            )
            conn.commit()
            audit(
                "ROOT_STORAGE_QUOTA_OVERRIDE",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"user_id={user_id}, quota_bytes={quota_bytes}, max_file_size_bytes={max_file_size_bytes}, rate={upload_rate_limit}",
            )
            return json_resp({"ok": True, "override": override, "user": storage_usage_for_user_row(conn, target)})
        finally:
            conn.close()

    @app.route("/api/root/storage/users/<int:user_id>/quota-override", methods=["DELETE"])
    @require_csrf
    def root_storage_clear_quota_override(user_id):
        actor, err = root_or_403()
        if err:
            return err
        conn = get_db()
        try:
            target = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
            if not target:
                return json_resp({"ok": False, "msg": "找不到帳號"}, 404)
            clear_storage_quota_override(conn, user_id)
            conn.commit()
            audit("ROOT_STORAGE_QUOTA_OVERRIDE_CLEAR", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"user_id={user_id}")
            return json_resp({"ok": True, "user": storage_usage_for_user_row(conn, target)})
        finally:
            conn.close()

    @app.route("/api/admin/storage/files", methods=["GET"])
    @require_csrf_safe
    def admin_storage_files():
        actor, err = manager_or_403()
        if err:
            return err
        user_id = request.args.get("user_id")
        include_trashed = request.args.get("include_trashed") in {"1", "true", "yes"}
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            where = "sf.deleted_at IS NULL AND f.deleted_at IS NULL"
            params = []
            if user_id:
                where += " AND sf.owner_user_id=?"
                params.append(int(user_id))
            if not include_trashed:
                where += " AND sf.is_trashed=0"
            rows = conn.execute(
                f"""
                SELECT sf.*, f.size_bytes, f.scan_status, f.risk_level, f.privacy_mode,
                       u.username AS owner_username
                FROM storage_files sf
                JOIN uploaded_files f ON f.id=sf.file_id
                JOIN users u ON u.id=sf.owner_user_id
                WHERE {where}
                ORDER BY sf.updated_at DESC
                LIMIT 200
                """,
                tuple(params),
            ).fetchall()
            return json_resp({"ok": True, "files": [dict(row) for row in rows]})
        finally:
            conn.close()

    @app.route("/api/admin/storage/sync-quota", methods=["POST"])
    @require_csrf
    def admin_storage_sync_quota():
        actor, err = manager_or_403()
        if err:
            return err
        data = request.get_json(silent=True) if request.is_json else {}
        if not isinstance(data, dict):
            data = {}
        try:
            limit = max(1, min(500, int(data.get("limit") or request.args.get("limit") or 200)))
        except Exception:
            limit = 200
        try:
            offset = max(0, int(data.get("offset") or request.args.get("offset") or 0))
        except Exception:
            offset = 0
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM users ORDER BY id ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            synced = []
            for row in rows:
                usage = storage_usage_for_user_row(conn, row)
                summary = sync_user_storage_summary(conn, row["id"], actor_user_id=actor["id"], source="admin", reason="admin_sync_quota")
                synced.append(storage_summary_with_live_quota(summary, usage))
            conn.commit()
            next_offset = offset + len(rows)
            has_more = next_offset < int(total or 0)
            audit("STORAGE_ADMIN_SYNC_QUOTA", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"users={len(synced)},offset={offset},limit={limit},has_more={has_more}")
            return json_resp({
                "ok": True,
                "synced": synced,
                "limit": limit,
                "offset": offset,
                "next_offset": next_offset if has_more else None,
                "has_more": has_more,
                "total_users": int(total or 0),
            })
        finally:
            conn.close()

    @app.route("/api/admin/storage/trash/purge", methods=["POST"])
    @require_csrf
    def admin_storage_purge_trash():
        actor, err = manager_or_403()
        if err:
            return err
        if not is_root(actor):
            return json_resp({"ok": False, "msg": "只有 root 可清理 storage 回收筒"}), 403
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}, 400)
        if data.get("confirm") != "PURGE STORAGE TRASH":
            return json_resp({"ok": False, "msg": "confirm 必須等於 PURGE STORAGE TRASH"}), 400
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            user_id = data.get("user_id")
            where = "is_trashed=1 AND deleted_at IS NULL"
            params = []
            if user_id:
                where += " AND owner_user_id=?"
                params.append(int(user_id))
            now = datetime.now().isoformat()
            affected_users = [
                int(row["owner_user_id"])
                for row in conn.execute(
                    f"SELECT DISTINCT owner_user_id FROM storage_files WHERE {where}",
                    tuple(params),
                ).fetchall()
                if row["owner_user_id"] is not None
            ]
            cur = conn.execute(f"UPDATE storage_files SET deleted_at=?, updated_at=? WHERE {where}", (now, now, *params))
            for owner_user_id in affected_users:
                sync_user_storage_summary(conn, owner_user_id, actor_user_id=actor["id"], source="admin", reason="admin_purge_trash")
            conn.commit()
            audit("STORAGE_ADMIN_PURGE_TRASH", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"purged={cur.rowcount}, user_id={user_id or '*'},synced_users={len(affected_users)}")
            return json_resp({"ok": True, "purged": cur.rowcount, "synced_users": len(affected_users)})
        finally:
            conn.close()

    @app.route("/api/admin/storage/maintenance", methods=["GET", "POST"])
    @require_csrf_safe
    def admin_storage_maintenance():
        actor, err = manager_or_403()
        if err:
            return err
        settings = get_system_settings() if get_system_settings else {}
        if request.method == "GET":
            return json_resp({"ok": True, "maintenance": storage_maintenance_status(settings)})
        conn = get_db()
        try:
            result = run_storage_maintenance(
                conn,
                actor_user_id=actor["id"],
                retention_days=settings.get("storage_trash_retention_days", 30),
                storage_root=storage_root,
                settings=settings,
            )
            conn.commit()
            audit("STORAGE_MAINTENANCE_RUN", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=str(result))
            return json_resp({"ok": True, "maintenance": result})
        finally:
            conn.close()

    @app.route("/api/files/quota", methods=["GET"])
    @require_csrf_safe
    def file_quota():
        actor, err = actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            rule = get_member_level_rule(conn, actor["effective_level"] or actor["member_level"])
            usage = get_user_cloud_drive_usage(conn, actor, member_rule=rule, storage_root=storage_root)
            return json_resp({"ok": True, "quota": usage})
        finally:
            conn.close()
