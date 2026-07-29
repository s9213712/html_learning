from datetime import datetime


def ensure_storage_quota_override_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS storage_quota_overrides (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL DEFAULT 0,
            quota_bytes INTEGER,
            max_file_size_bytes INTEGER,
            upload_rate_limit_per_day INTEGER,
            can_upload_override INTEGER,
            reason TEXT,
            updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_storage_quota_overrides_enabled ON storage_quota_overrides(enabled)"
    )


def _row_to_dict(row):
    if not row:
        return None
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    value = data.get("can_upload_override")
    data["can_upload_override"] = None if value is None else bool(value)
    return data


def get_storage_quota_override(conn, user_id, *, ensure_schema=True):
    if ensure_schema:
        ensure_storage_quota_override_schema(conn)
    try:
        row = conn.execute(
            "SELECT * FROM storage_quota_overrides WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    except Exception:
        if ensure_schema:
            raise
        return None
    return _row_to_dict(row)


def set_storage_quota_override(conn, user_id, *, enabled=True, quota_bytes=None,
                               max_file_size_bytes=None, upload_rate_limit_per_day=None,
                               can_upload_override=None, reason="", actor_user_id=None):
    ensure_storage_quota_override_schema(conn)
    now = datetime.now().isoformat()
    enabled_int = 1 if enabled else 0
    can_upload_int = None if can_upload_override is None else (1 if can_upload_override else 0)
    conn.execute(
        """
        INSERT INTO storage_quota_overrides (
            user_id, enabled, quota_bytes, max_file_size_bytes,
            upload_rate_limit_per_day, can_upload_override, reason, updated_by, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            enabled=excluded.enabled,
            quota_bytes=excluded.quota_bytes,
            max_file_size_bytes=excluded.max_file_size_bytes,
            upload_rate_limit_per_day=excluded.upload_rate_limit_per_day,
            can_upload_override=excluded.can_upload_override,
            reason=excluded.reason,
            updated_by=excluded.updated_by,
            updated_at=excluded.updated_at
        """,
        (
            int(user_id),
            enabled_int,
            quota_bytes,
            max_file_size_bytes,
            upload_rate_limit_per_day,
            can_upload_int,
            reason,
            actor_user_id,
            now,
        ),
    )
    return get_storage_quota_override(conn, user_id)


def clear_storage_quota_override(conn, user_id):
    ensure_storage_quota_override_schema(conn)
    conn.execute("DELETE FROM storage_quota_overrides WHERE user_id=?", (int(user_id),))


def apply_storage_quota_override(usage, override):
    if not override or not override.get("enabled"):
        return usage
    adjusted = dict(usage)
    if override.get("quota_bytes") is not None:
        adjusted["total_bytes"] = int(override["quota_bytes"])
        adjusted["quota_source"] = "root_user_override"
    if override.get("max_file_size_bytes") is not None:
        adjusted["max_file_size_bytes"] = int(override["max_file_size_bytes"])
    if override.get("upload_rate_limit_per_day") is not None:
        adjusted["upload_rate_limit_per_day"] = int(override["upload_rate_limit_per_day"])
    if override.get("can_upload_override") is not None:
        adjusted["can_upload"] = bool(override["can_upload_override"])
    total_bytes = adjusted.get("total_bytes")
    used_bytes = int(adjusted.get("used_bytes") or 0)
    adjusted["remaining_bytes"] = None if total_bytes is None else max(0, int(total_bytes) - used_bytes)
    if total_bytes and int(total_bytes) > 0:
        adjusted["percent_used"] = min(100.0, round((used_bytes / int(total_bytes)) * 100, 2))
    elif total_bytes == 0 and used_bytes > 0:
        adjusted["percent_used"] = 100.0
    else:
        adjusted["percent_used"] = 0.0
    if adjusted.get("quota_source") == "root_user_override":
        adjusted["warning_threshold_percent"] = 80
        adjusted["warning_threshold_bytes"] = int(int(total_bytes or 0) * 0.8) if total_bytes is not None else None
        adjusted["warning_active"] = bool(total_bytes is not None and used_bytes >= int(int(total_bytes or 0) * 0.8))
    adjusted["root_override"] = override
    return adjusted
