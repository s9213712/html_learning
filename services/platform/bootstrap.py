import json
import os
from datetime import datetime

from services.core.sqlite_safe import table_columns as safe_table_columns

CURRENT_SCHEMA_VERSION = 31
SCHEMA_MIGRATIONS = (
    (1, "bootstrap schema_migrations metadata table"),
    (2, "ensure legacy-compatible users columns"),
    (3, "ensure violation_appeals columns"),
    (4, "ensure system_settings baseline rows"),
    (5, "session revocation and security support schema"),
    (6, "add is_private column to chat_rooms for 1on1 PM support"),
    (7, "phase 1 identity governance user columns"),
    (8, "phase 2 password strength user columns"),
    (9, "phase 2 account lockout user columns"),
    (10, "phase 2 session device management columns"),
    (11, "phase 2 login location tracking schema"),
    (12, "phase 3 member level rules schema"),
    (13, "phase 3 moderation proposal voting schema"),
    (14, "phase 3 governance records schema"),
    (15, "member level governance fields and audit"),
    (16, "snapshot restore and server modes schema"),
    (17, "privacy upload security schema"),
    (18, "cloud drive quota and safety policy schema"),
    (19, "integrity guard schema"),
    (20, "cloud drive attachment references and grants schema"),
    (21, "cloud drive optional deep antivirus policy columns"),
    (22, "reports and notifications schema"),
    (23, "legacy direct messages retired"),
    (24, "storage files and albums schema"),
    (25, "storage share links schema"),
    (26, "points economy private chain schema"),
    (27, "chat recall stickers and friends schema"),
    (28, "game zone chess schema"),
    (29, "trading engine schema"),
    (30, "trading market registry seed metadata"),
    (31, "violation fines and feature restrictions schema"),
)

_STATE = {
    "get_db": None,
    "db_path": None,
    "schema_path": None,
    "legacy_fail_log": None,
    "legacy_blocked_ips": None,
    "legacy_rate_limit": None,
    "legacy_audit_log": None,
    "chain_seed": None,
    "chain_hash": None,
    "load_json": None,
    "normalize_text": None,
    "hash_password": None,
    "audit": None,
    "refresh_system_settings": None,
    "init_system_settings_table": None,
    "seed_missing_settings": None,
    "import_legacy_settings_files": None,
    "default_settings": None,
}


def configure_bootstrap_service(**kwargs):
    _STATE.update(kwargs)


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _password_history_limit():
    raw = os.environ.get("HTML_LEARNING_PASSWORD_HISTORY_LIMIT", "5")
    try:
        return max(1, int(str(raw).strip()))
    except Exception:
        return 5


def _trim_password_history(conn, user_id):
    conn.execute(
        "DELETE FROM user_passwords WHERE user_id=? AND id NOT IN ("
        "SELECT id FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT ?"
        ")",
        (user_id, user_id, _password_history_limit())
    )


def _insert_password_record(conn, user_id, password, hash_password, now):
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
        (user_id, hash_password(password), now)
    )
    _trim_password_history(conn, user_id)


def _default_password_policy_disabled():
    values = (
        os.environ.get("HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_POLICY", ""),
        os.environ.get("HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_CHANGE", ""),
    )
    return any(str(value).strip().lower() in {"1", "true", "yes", "on"} for value in values)


def _relax_default_password_gate_for_dev_defaults():
    if _env_bool("HACKME_DEV_SECURITY_ENABLED", False) or _env_bool("HTML_LEARNING_SECURITY_ENABLED", False):
        return False
    mode = (
        os.environ.get("HACKME_DEV_SERVER_MODE")
        or os.environ.get("HTML_LEARNING_SERVER_MODE")
        or os.environ.get("SERVER_MODE")
        or "dev_ready"
    )
    dev_like_mode = str(mode).strip().lower() in {"dev_ready", "internal_test", "test", "superweak"}
    if not dev_like_mode:
        return False
    return _env_bool("HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS", False) or _env_bool(
        "HTML_LEARNING_ALLOW_DEFAULT_PASSWORDS", False
    )


def _mark_default_account_password(conn, user_id, now):
    if _default_password_policy_disabled():
        return
    relax_default_gate = _relax_default_password_gate_for_dev_defaults()
    must_change = 0 if relax_default_gate else 1
    is_default = 0 if relax_default_gate else 1
    conn.execute(
        "UPDATE users SET must_change_password=?, is_default_password=?, updated_at=? WHERE id=?",
        (must_change, is_default, now, user_id)
    )


def _mark_default_account_if_unconfirmed(conn, user_id, now):
    if _default_password_policy_disabled():
        return
    row = conn.execute(
        "SELECT must_change_password, is_default_password, password_changed_at FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return
    if not row["password_changed_at"] and not row["must_change_password"] and not row["is_default_password"]:
        _mark_default_account_password(conn, user_id, now)


def _mark_default_account_if_password_matches(conn, user_id, raw_password, now):
    if _default_password_policy_disabled():
        return
    if not raw_password:
        return
    row = conn.execute(
        "SELECT password_hash FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not row:
        return
    verify_password = _STATE.get("verify_password")
    matches = False
    if verify_password:
        try:
            matches = bool(verify_password(row["password_hash"], raw_password))
        except Exception:
            matches = False
    if not matches and _STATE.get("hash_password"):
        try:
            matches = row["password_hash"] == _STATE["hash_password"](raw_password)
        except Exception:
            matches = False
    if matches:
        _mark_default_account_password(conn, user_id, now)


def _apply_default_account_level(conn, user_id, base_level, now):
    conn.execute(
        """
        UPDATE users
        SET base_level=CASE WHEN base_level IS NULL OR base_level='' OR base_level='normal' THEN ? ELSE base_level END,
            effective_level=CASE
                WHEN sanction_status IN ('restricted', 'suspended') THEN sanction_status
                WHEN effective_level IS NULL OR effective_level='' OR effective_level='normal' THEN ?
                ELSE effective_level
            END,
            member_level=CASE
                WHEN sanction_status IN ('restricted', 'suspended') THEN sanction_status
                WHEN member_level IS NULL OR member_level='' OR member_level='normal' THEN ?
                ELSE member_level
            END,
            updated_at=?
        WHERE id=?
        """,
        (base_level, base_level, base_level, now, user_id),
    )


def _clear_special_account_level(conn, user_id, now):
    conn.execute(
        """
        UPDATE users
        SET base_level='normal',
            effective_level='normal',
            member_level='normal',
            level_updated_at=?,
            level_update_reason='special role bypasses member levels',
            updated_at=?
        WHERE id=?
        """,
        (now, now, user_id),
    )


def _bootstrap_password(env_name, username, *, required):
    password = os.environ.get(env_name, "").strip()
    if password:
        return password
    return username


def _safe_iso(value, fallback=None):
    if not value:
        return fallback
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).isoformat()
        except Exception:
            return fallback
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return fallback
        try:
            return datetime.fromisoformat(v).isoformat()
        except Exception:
            return v
    return fallback


def _coerce_count(v, default=0):
    try:
        n = int(v)
        return n if n > 0 else default
    except Exception:
        return default


def _migrate_legacy_fail_log_rows():
    payload = _STATE["load_json"](_STATE["legacy_fail_log"])
    now = datetime.now().isoformat()
    rows = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = []
        for ip, item in payload.items():
            if not isinstance(ip, str):
                continue
            if isinstance(item, dict):
                count = _coerce_count(item.get("count", 1), 1)
                created_at = _safe_iso(item.get("created_at") or item.get("ts") or item.get("time"), now)
                username = _STATE["normalize_text"](item.get("username") or item.get("user") or item.get("target_user"))
                detail = _STATE["normalize_text"](item.get("detail") or item.get("reason"))
            else:
                count = _coerce_count(item, 1)
                created_at = now
                username = ""
                detail = "legacy fail_log"
            for _ in range(min(max(1, count), 1000)):
                rows.append({"event_type": "login_fail", "ip_address": ip, "target_user": username or None, "detail": detail or "legacy fail_log", "created_at": created_at})
    else:
        candidates = []
    if isinstance(payload, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            ip = _STATE["normalize_text"](item.get("ip") or item.get("ip_address"))
            if not ip:
                continue
            count = _coerce_count(item.get("count", 1), 1)
            username = _STATE["normalize_text"](item.get("username") or item.get("target_user") or item.get("user"))
            detail = _STATE["normalize_text"](item.get("detail") or item.get("reason"))
            created_at = _safe_iso(item.get("created_at") or item.get("ts") or item.get("time"), now)
            for _ in range(min(max(1, count), 1000)):
                rows.append({"event_type": "login_fail", "ip_address": ip, "target_user": username or None, "detail": detail or "legacy fail_log", "created_at": created_at})
    return rows


def _migrate_legacy_blocked_ip_rows():
    payload = _STATE["load_json"](_STATE["legacy_blocked_ips"])
    now = datetime.now().isoformat()
    rows = []
    if not isinstance(payload, dict):
        return rows
    for ip, value in payload.items():
        if not isinstance(ip, str):
            continue
        detail = ""
        blocked_until = None
        if isinstance(value, dict):
            blocked_until = _safe_iso(value.get("blocked_until") or value.get("until") or value.get("expires_at"), None)
            detail = _STATE["normalize_text"](value.get("detail") or value.get("reason"))
        elif isinstance(value, str):
            blocked_until = _safe_iso(value, None)
            detail = "legacy blocked_ips.json"
        elif isinstance(value, (int, float)):
            blocked_until = _safe_iso(value, None)
            detail = "legacy blocked_ips.json"
        else:
            detail = "legacy blocked_ips.json"
        if not blocked_until:
            blocked_until = now
        rows.append({"event_type": "ip_block", "ip_address": ip, "target_user": None, "detail": f"blocked_until={blocked_until}" + (f" ({detail})" if detail else ""), "created_at": now})
    return rows


def _migrate_legacy_rate_limit_rows():
    payload = _STATE["load_json"](_STATE["legacy_rate_limit"])
    now = datetime.now().isoformat()
    rows = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            ip = _STATE["normalize_text"](item.get("ip") or item.get("ip_address"))
            if not ip:
                continue
            rows.append({"event_type": "rate_limit", "ip_address": ip, "target_user": _STATE["normalize_text"](item.get("target_user") or item.get("user")), "detail": _STATE["normalize_text"](item.get("detail") or item.get("reason")) or "legacy rate_limit", "created_at": _safe_iso(item.get("created_at") or item.get("ts") or item.get("time"), now)})
    elif isinstance(payload, dict):
        for ip, entry in payload.items():
            if not isinstance(ip, str):
                continue
            if isinstance(entry, dict):
                detail = _STATE["normalize_text"](entry.get("detail") or entry.get("reason") or "legacy rate_limit")
                created_at = _safe_iso(entry.get("created_at") or entry.get("ts") or entry.get("time"), now)
            else:
                detail = "legacy rate_limit"
                created_at = now
            rows.append({"event_type": "rate_limit", "ip_address": ip, "target_user": "", "detail": detail, "created_at": created_at})
    return rows


def _migrate_legacy_audit_rows():
    if not os.path.exists(_STATE["legacy_audit_log"]):
        return []
    rows = []
    with open(_STATE["legacy_audit_log"], "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            rows.append({
                "action": _STATE["normalize_text"](obj.get("action", "legacy_audit")),
                "ip": _STATE["normalize_text"](obj.get("ip")),
                "user": _STATE["normalize_text"](obj.get("user")),
                "success": 1 if bool(obj.get("success")) else 0,
                "ua": _STATE["normalize_text"](obj.get("ua")),
                "detail": _STATE["normalize_text"](obj.get("detail")),
                "ts": _safe_iso(obj.get("ts"), datetime.now().isoformat()),
            })
    return rows


def migrate_legacy_json_artifacts(conn):
    summary = {"security_events_imported": 0, "secure_audit_imported": 0, "settings_imported": 0}

    _STATE["init_system_settings_table"](conn)
    existing_settings = {r["key"] for r in conn.execute("SELECT key FROM system_settings").fetchall()}
    before_settings = len(existing_settings)
    if len(existing_settings) < len(_STATE["default_settings"]):
        _STATE["import_legacy_settings_files"](conn)
        _STATE["seed_missing_settings"](conn)
        conn.commit()
        after_settings = {r["key"] for r in conn.execute("SELECT key FROM system_settings").fetchall()}
        summary["settings_imported"] = max(0, len(after_settings) - before_settings)

    has_security_rows = conn.execute("SELECT 1 FROM security_events LIMIT 1").fetchone()
    if not has_security_rows:
        security_rows = []
        security_rows.extend(_migrate_legacy_fail_log_rows())
        security_rows.extend(_migrate_legacy_blocked_ip_rows())
        security_rows.extend(_migrate_legacy_rate_limit_rows())
        security_rows.sort(key=lambda item: item.get("created_at", ""))
        for row in security_rows:
            conn.execute(
                "INSERT INTO security_events (event_type, ip_address, target_user, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (row["event_type"], row["ip_address"], row["target_user"], row["detail"], row["created_at"])
            )
        summary["security_events_imported"] = len(security_rows)

    has_audit_rows = conn.execute("SELECT 1 FROM secure_audit LIMIT 1").fetchone()
    if not has_audit_rows:
        prev_hash = _STATE["chain_seed"]
        for row in _migrate_legacy_audit_rows():
            entry = {
                "ts": row["ts"], "action": row["action"], "ip": row["ip"],
                "user": row["user"], "success": bool(row["success"]), "ua": row["ua"], "detail": row["detail"],
            }
            entry_json = json.dumps(entry, ensure_ascii=False)
            chain_hash = _STATE["chain_hash"](prev_hash, entry_json)
            conn.execute(
                "INSERT INTO secure_audit (ts, action, ip, user, success, ua, detail, chain_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row["ts"], row["action"], row["ip"] or "-", row["user"] or "-", 1 if bool(row["success"]) else 0, row["ua"] or "-", row["detail"] or "-", chain_hash)
            )
            prev_hash = chain_hash
            summary["secure_audit_imported"] += 1

    _STATE["seed_missing_settings"](conn)
    conn.commit()
    return summary


def migrate_legacy_json_to_db():
    conn = _STATE["get_db"]()
    try:
        _STATE["init_system_settings_table"](conn)
        summary = migrate_legacy_json_artifacts(conn)
        _STATE["refresh_system_settings"]()
        return summary
    finally:
        conn.close()


def ensure_schema_migrations_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
    """)


def get_schema_version(conn):
    ensure_schema_migrations_table(conn)
    try:
        row = conn.execute("SELECT MAX(version) as v FROM schema_migrations").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0
    except Exception:
        return 0


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn, table_name):
    try:
        return safe_table_columns(conn, table_name)
    except Exception:
        return set()


def _repair_existing_legacy_tables(
    conn,
    ensure_secure_audit_columns,
    ensure_user_columns,
    ensure_appeal_columns,
    ensure_session_columns,
):
    for table_name, repair in (
        ("secure_audit", ensure_secure_audit_columns),
        ("users", ensure_user_columns),
        ("violation_appeals", ensure_appeal_columns),
        ("sessions", ensure_session_columns),
    ):
        if _table_exists(conn, table_name):
            repair(conn)


def _ensure_reports_notifications_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            target_type          TEXT NOT NULL,
            target_id            INTEGER,
            reporter_user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reported_user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reason               TEXT NOT NULL,
            status               TEXT NOT NULL DEFAULT 'pending',
            claimed_by_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            claimed_by_username  TEXT,
            claimed_at           TEXT,
            reviewed_by          TEXT,
            reviewed_at          TEXT,
            review_note          TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL,
            UNIQUE(target_type, target_id, reporter_user_id, reason)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type            TEXT NOT NULL,
            title           TEXT NOT NULL,
            body            TEXT NOT NULL,
            link            TEXT,
            is_read         INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            read_at         TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_claimed ON reports(claimed_by_user_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, is_read, created_at)")


def _ensure_chat_social_schema(conn):
    room_cols = _table_columns(conn, "chat_rooms")
    for name, ddl in (
        ("join_password_hash", "TEXT"),
        ("join_password_required", "INTEGER NOT NULL DEFAULT 0"),
        ("allow_anonymous", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in room_cols:
            conn.execute(f"ALTER TABLE chat_rooms ADD COLUMN {name} {ddl}")
    member_cols = _table_columns(conn, "chat_room_members")
    if "anonymous_enabled" not in member_cols:
        conn.execute("ALTER TABLE chat_room_members ADD COLUMN anonymous_enabled INTEGER NOT NULL DEFAULT 0")
    cols = _table_columns(conn, "chat_messages")
    for name, ddl in (
        ("message_type", "TEXT NOT NULL DEFAULT 'text'"),
        ("sticker_key", "TEXT"),
        ("is_revoked", "INTEGER NOT NULL DEFAULT 0"),
        ("revoked_at", "TEXT"),
        ("revoked_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE chat_messages ADD COLUMN {name} {ddl}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_friends (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            friend_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status         TEXT    NOT NULL DEFAULT 'pending',
            requested_by   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at     TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL,
            UNIQUE(user_id, friend_user_id),
            CHECK (user_id <> friend_user_id),
            CHECK (status IN ('pending', 'accepted', 'rejected', 'blocked'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_friends_user_status ON user_friends(user_id, status)")


def apply_schema_migrations(
    conn,
    ensure_secure_audit_columns,
    ensure_user_columns,
    ensure_appeal_columns,
    ensure_session_columns,
    ensure_security_support_schema,
):
    current = get_schema_version(conn)
    if current >= CURRENT_SCHEMA_VERSION:
        return {"previous": current, "applied": [], "current": current}

    applied = []
    for version, name in SCHEMA_MIGRATIONS:
        if version <= current:
            continue
        if version == 1:
            pass
        elif version == 2:
            ensure_secure_audit_columns(conn)
            ensure_user_columns(conn)
        elif version == 3:
            ensure_appeal_columns(conn)
        elif version == 4:
            _STATE["init_system_settings_table"](conn)
            _STATE["seed_missing_settings"](conn)
        elif version == 5:
            ensure_session_columns(conn)
            ensure_security_support_schema(conn)
        elif version == 6:
            try:
                conn.execute("ALTER TABLE chat_rooms ADD COLUMN is_private INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass  # column may already exist
        elif version == 7:
            ensure_user_columns(conn)
        elif version == 8:
            ensure_user_columns(conn)
        elif version == 9:
            ensure_user_columns(conn)
        elif version == 10:
            ensure_session_columns(conn)
        elif version == 11:
            ensure_security_support_schema(conn)
        elif version == 12:
            ensure_security_support_schema(conn)
        elif version == 13:
            ensure_security_support_schema(conn)
        elif version == 14:
            ensure_security_support_schema(conn)
        elif version == 15:
            ensure_user_columns(conn)
            ensure_security_support_schema(conn)
        elif version == 16:
            ensure_security_support_schema(conn)
        elif version == 17:
            ensure_security_support_schema(conn)
        elif version == 18:
            ensure_security_support_schema(conn)
        elif version == 19:
            ensure_security_support_schema(conn)
        elif version == 21:
            ensure_security_support_schema(conn)
        elif version == 22:
            _ensure_reports_notifications_schema(conn)
        elif version == 23:
            _ensure_reports_notifications_schema(conn)
        elif version == 24:
            from services.storage.storage_albums import ensure_storage_album_schema

            ensure_storage_album_schema(conn)
        elif version == 25:
            from services.storage.storage_albums import ensure_storage_album_schema

            ensure_storage_album_schema(conn)
        elif version == 26:
            from services.points_chain import ensure_points_economy_schema

            ensure_points_economy_schema(conn)
        elif version == 27:
            _ensure_chat_social_schema(conn)
        elif version == 28:
            from routes.games import ensure_game_schema

            ensure_game_schema(conn)
        elif version == 29:
            from services.trading.trading_engine import ensure_trading_schema

            ensure_trading_schema(conn)
        elif version == 30:
            from services.trading.trading_engine import ensure_trading_schema

            ensure_trading_schema(conn)
        elif version == 31:
            from services.governance.violation_fines import ensure_violation_fine_schema

            ensure_violation_fine_schema(conn)

        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, datetime.now().isoformat())
        )
        applied.append(version)

    return {"previous": current, "applied": applied, "current": max(current, applied[-1] if applied else current)}


def init_db(
    *,
    ensure_secure_audit_columns,
    ensure_user_columns,
    ensure_appeal_columns,
    ensure_session_columns,
    ensure_security_support_schema,
    ensure_official_chat_room,
    hash_password,
    ensure_points_economy_schema=None,
    preserve_existing_runtime=False,
):
    conn = _STATE["get_db"]()
    schema_path = _STATE.get("schema_path") or (_STATE["db_path"] + '.schema.sql')
    # Existing SQLite tables may predate newer columns referenced by schema indexes.
    # Repair known legacy tables before replaying the full schema script.
    _repair_existing_legacy_tables(
        conn,
        ensure_secure_audit_columns,
        ensure_user_columns,
        ensure_appeal_columns,
        ensure_session_columns,
    )
    conn.executescript(open(schema_path, 'r', encoding='utf-8').read())
    ensure_secure_audit_columns(conn)
    ensure_user_columns(conn)
    ensure_appeal_columns(conn)
    ensure_session_columns(conn)
    ensure_security_support_schema(conn)
    from services.governance.violation_fines import ensure_violation_fine_schema

    ensure_violation_fine_schema(conn)
    if ensure_points_economy_schema is None:
        from services.points_chain import ensure_points_economy_schema
    ensure_points_economy_schema(conn)
    _STATE["init_system_settings_table"](conn)
    _STATE["seed_missing_settings"](conn)
    migration_plan = apply_schema_migrations(
        conn,
        ensure_secure_audit_columns,
        ensure_user_columns,
        ensure_appeal_columns,
        ensure_session_columns,
        ensure_security_support_schema,
    )
    conn.commit()
    if migration_plan["applied"] and int(migration_plan.get("previous") or 0) > 0:
        _STATE["audit"]("DB_SCHEMA_MIGRATION", "127.0.0.1", user="system", detail=f"schema migrated from v{migration_plan['previous']} to v{migration_plan['current']}")

    if preserve_existing_runtime:
        # A persisted incident may span a controlled process restart.  Schema
        # repair/migrations above are required to make recovery possible, but
        # legacy imports, special-account normalization, password bookkeeping,
        # and official-room seeding below are unrelated state mutations.  The
        # launcher has already detected the authoritative control-DB incident,
        # so stop here and leave all existing account/evidence rows untouched.
        _STATE["refresh_system_settings"]()
        conn.commit()
        conn.close()
        return {
            "preserved_existing_runtime": True,
            "migration": migration_plan,
        }

    migration_summary = migrate_legacy_json_artifacts(conn)
    if migration_summary["settings_imported"] or migration_summary["security_events_imported"] or migration_summary["secure_audit_imported"]:
        _STATE["audit"]("DB_MIGRATION_APPLIED", "127.0.0.1", user="system", detail=str(migration_summary))

    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    except Exception:
        pass
    conn.execute("UPDATE users SET role='user' WHERE role IS NULL OR role=''")

    now = datetime.now().isoformat()

    root_row = conn.execute("SELECT id FROM users WHERE username='root'").fetchone()
    root_password = None
    if root_row:
        root_id = root_row["id"]
        conn.execute("UPDATE users SET role='super_admin', status='active', updated_at=? WHERE id=?", (now, root_id))
        has_root_pw = conn.execute("SELECT 1 FROM user_passwords WHERE user_id=? LIMIT 1", (root_id,)).fetchone()
        if not has_root_pw:
            root_password = _bootstrap_password("HTML_LEARNING_ROOT_PASSWORD", "root", required=True)
    else:
        root_password = _bootstrap_password("HTML_LEARNING_ROOT_PASSWORD", "root", required=True)
        root_cur = conn.execute(
            "INSERT INTO users (username, status, role, created_at, updated_at) VALUES (?, 'active', 'super_admin', ?, ?)",
            ("root", now, now)
        )
        root_id = root_cur.lastrowid
        has_root_pw = None
    if not has_root_pw:
        _insert_password_record(conn, root_id, root_password, hash_password, now)
        _mark_default_account_password(conn, root_id, now)
    elif os.environ.get("HTML_LEARNING_ROOT_PASSWORD", "").strip():
        root_password = os.environ.get("HTML_LEARNING_ROOT_PASSWORD", "").strip()
        _mark_default_account_if_password_matches(conn, root_id, root_password, now)
        _mark_default_account_if_unconfirmed(conn, root_id, now)
    _clear_special_account_level(conn, root_id, now)

    admin_password = _bootstrap_password("HTML_LEARNING_MANAGER_PASSWORD", "admin", required=False)
    if admin_password:
        row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if row:
            admin_id = row["id"]
            conn.execute("UPDATE users SET role='manager', status='active', updated_at=? WHERE username='admin'", (now,))
        else:
            mgr_cur = conn.execute(
                "INSERT INTO users (username, status, role, created_at, updated_at) VALUES (?, 'active', 'manager', ?, ?)",
                ("admin", now, now)
            )
            admin_id = mgr_cur.lastrowid
        has_admin_pw = conn.execute("SELECT 1 FROM user_passwords WHERE user_id=? LIMIT 1", (admin_id,)).fetchone()
        if not has_admin_pw:
            _insert_password_record(conn, admin_id, admin_password, hash_password, now)
            _mark_default_account_password(conn, admin_id, now)
        else:
            _mark_default_account_if_password_matches(conn, admin_id, admin_password, now)
            _mark_default_account_if_unconfirmed(conn, admin_id, now)
        _clear_special_account_level(conn, admin_id, now)

    conn.execute(
        """
        UPDATE users
        SET base_level='normal',
            effective_level='normal',
            member_level='normal',
            level_updated_at=?,
            level_update_reason='special role bypasses member levels',
            updated_at=?
        WHERE username='root' OR role IN ('super_admin', 'manager')
        """,
        (now, now),
    )

    test_password = _bootstrap_password("HTML_LEARNING_TEST_PASSWORD", "test", required=False)
    if test_password:
        row = conn.execute("SELECT id FROM users WHERE username='test'").fetchone()
        if row:
            test_id = row["id"]
            conn.execute("UPDATE users SET role='user', status='active', updated_at=? WHERE username='test'", (now,))
        else:
            test_cur = conn.execute(
                "INSERT INTO users (username, status, role, created_at, updated_at) VALUES (?, 'active', 'user', ?, ?)",
                ("test", now, now)
            )
            test_id = test_cur.lastrowid
        has_test_pw = conn.execute("SELECT 1 FROM user_passwords WHERE user_id=? LIMIT 1", (test_id,)).fetchone()
        if not has_test_pw:
            _insert_password_record(conn, test_id, test_password, hash_password, now)
            _mark_default_account_password(conn, test_id, now)
        else:
            _mark_default_account_if_password_matches(conn, test_id, test_password, now)
            _mark_default_account_if_unconfirmed(conn, test_id, now)
        _apply_default_account_level(conn, test_id, "trusted", now)

    ensure_official_chat_room(conn)
    _STATE["refresh_system_settings"]()
    conn.commit()
    conn.close()
