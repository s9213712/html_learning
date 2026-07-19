"""Database and schema helpers extracted from ``server.py``."""

from __future__ import annotations

import re
import threading
from datetime import datetime

from services.core.sqlite_hardening import connect_sqlite, connect_sqlite_readonly


def _request_connection_bucket():
    try:
        from flask import g, has_request_context

        if not has_request_context():
            return None
        bucket = getattr(g, "_hackme_sqlite_connections", None)
        if bucket is None:
            bucket = []
            setattr(g, "_hackme_sqlite_connections", bucket)
        return bucket
    except Exception:
        return None


def track_request_connection(conn):
    bucket = _request_connection_bucket()
    if bucket is not None:
        bucket.append(conn)
    return conn


def close_request_db_connections(_exc=None):
    try:
        from flask import g, has_request_context

        if not has_request_context():
            return None
        bucket = getattr(g, "_hackme_sqlite_connections", None) or []
        setattr(g, "_hackme_sqlite_connections", [])
    except Exception:
        return None
    while bucket:
        conn = bucket.pop()
        try:
            if getattr(conn, "in_transaction", False):
                conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    return None


def _open_sqlite(db_path, *, register_app_mode=None):
    conn = connect_sqlite(db_path, timeout=15, row_factory=True, foreign_keys=True, wal=True)
    try:
        if register_app_mode is not None:
            register_app_mode(conn)
    except Exception:
        pass
    return conn


def _open_sqlite_readonly(db_path, *, register_app_mode=None):
    conn = connect_sqlite_readonly(db_path, timeout=15, row_factory=True, foreign_keys=True)
    try:
        if register_app_mode is not None:
            register_app_mode(conn)
    except Exception:
        pass
    return conn


def ensure_audit_db_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT NOT NULL DEFAULT '',
            entry_hash TEXT NOT NULL DEFAULT '',
            chain_hash TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(secure_audit)").fetchall()}
    for col, ddl in (
        ("prev_hash", "ALTER TABLE secure_audit ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''"),
        ("entry_hash", "ALTER TABLE secure_audit ADD COLUMN entry_hash TEXT NOT NULL DEFAULT ''"),
        ("chain_hash", "ALTER TABLE secure_audit ADD COLUMN chain_hash TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in cols:
            conn.execute(ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_secure_audit_ts ON secure_audit(ts)")


def get_db(db_path, *, register_app_mode=None):
    conn = _open_sqlite(db_path, register_app_mode=register_app_mode)
    return track_request_connection(conn)


def get_readonly_db(db_path, *, register_app_mode=None):
    return track_request_connection(_open_sqlite_readonly(db_path, register_app_mode=register_app_mode))


def get_readonly_auth_db(db_path):
    return track_request_connection(_open_sqlite_readonly(db_path))


def get_readonly_audit_db(db_path):
    return track_request_connection(_open_sqlite_readonly(db_path))


def get_readonly_control_db(db_path):
    return track_request_connection(_open_sqlite_readonly(db_path))


def get_audit_db(db_path):
    conn = _open_sqlite(db_path)
    path = str(db_path)
    if path not in _ENSURED_AUDIT_DB_PATHS:
        with _ENSURE_LOCK:
            if path not in _ENSURED_AUDIT_DB_PATHS:
                ensure_audit_db_schema(conn)
                conn.commit()
                _ENSURED_AUDIT_DB_PATHS.add(path)
    return track_request_connection(conn)


def count_role(role, *, get_db):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE role=? AND username<>'root'",
            (role,),
        ).fetchone()
        return row["c"] if row else 0
    finally:
        conn.close()


def get_user_by_username(username, *, get_db):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, email, nickname, real_name, birthdate, id_number, phone, status, role, "
            "member_level, base_level, effective_level, trust_score, points, reputation, violation_score, "
            "sanction_status, sanction_until, level_updated_at, level_updated_by, level_update_reason, "
            "password_strength_score, must_change_password, is_default_password, avatar_file_id, avatar_crop_json, blocked_until, violation_count, chat_violation_warned "
            "FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if not row:
            return None
        return row
    finally:
        conn.close()


def ensure_user_columns(conn, *, ensure_user_identity_columns):
    ensure_user_identity_columns(conn)


def ensure_secure_audit_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(secure_audit)").fetchall()}
    for name in ("prev_hash", "entry_hash"):
        if name not in cols:
            conn.execute(f"ALTER TABLE secure_audit ADD COLUMN {name} TEXT")


def ensure_appeal_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(violation_appeals)").fetchall()}
    additions = [
        ("latest_violation_id", "INTEGER"),
        ("violation_count_snapshot", "INTEGER NOT NULL DEFAULT 0"),
        ("penalty_points", "INTEGER NOT NULL DEFAULT 0"),
        ("pre_status", "TEXT NOT NULL DEFAULT 'active'"),
        ("pre_role", "TEXT NOT NULL DEFAULT 'user'"),
        ("review_note", "TEXT"),
    ]
    for name, ddl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE violation_appeals ADD COLUMN {name} {ddl}")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(violation_appeals)").fetchall()}
    if {"user_id", "latest_violation_id"}.issubset(cols):
        duplicate = conn.execute(
            """
            SELECT user_id, latest_violation_id, COUNT(*) AS row_count
            FROM violation_appeals
            WHERE latest_violation_id IS NOT NULL
            GROUP BY user_id, latest_violation_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate:
            raise RuntimeError(
                "duplicate violation appeals require explicit recovery before startup: "
                f"user_id={duplicate['user_id']} violation_id={duplicate['latest_violation_id']} "
                f"rows={duplicate['row_count']}"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_appeal_user_violation "
            "ON violation_appeals(user_id, latest_violation_id)"
        )


def ensure_session_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    additions = [
        ("is_revoked", "INTEGER NOT NULL DEFAULT 0"),
        ("revoked_at", "TEXT"),
        ("last_seen", "TEXT"),
        ("device_info", "TEXT"),
        ("ip_country", "TEXT"),
        ("session_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("auth_scope", "TEXT NOT NULL DEFAULT ''"),
        ("allowed_features_json", "TEXT NOT NULL DEFAULT '[]'"),
    ]
    for name, ddl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")
    conn.execute("UPDATE sessions SET is_revoked=0 WHERE is_revoked IS NULL")
    conn.execute("UPDATE sessions SET last_seen=created_at WHERE last_seen IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_revoked ON sessions(is_revoked)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_active ON sessions(user_id, is_revoked, expires_at, last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_active_expires ON sessions(is_revoked, expires_at)")


def ensure_auth_db_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS csrf_tokens (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash   TEXT NOT NULL UNIQUE,
            username     TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS captcha_challenges (
            id           TEXT PRIMARY KEY,
            mode         TEXT NOT NULL,
            answer_hash  TEXT NOT NULL,
            ip_hash      TEXT,
            expires_at   TEXT NOT NULL,
            used_at      TEXT,
            created_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            ip_address   TEXT,
            user_agent   TEXT,
            success      INTEGER NOT NULL DEFAULT 0,
            attempted_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            token_hash    TEXT NOT NULL UNIQUE,
            ip_address    TEXT,
            user_agent    TEXT,
            device_info   TEXT,
            ip_country    TEXT,
            expires_at    TEXT NOT NULL,
            is_revoked    INTEGER NOT NULL DEFAULT 0,
            revoked_at    TEXT,
            last_seen     TEXT,
            session_epoch INTEGER NOT NULL DEFAULT 0,
            auth_scope    TEXT NOT NULL DEFAULT '',
            allowed_features_json TEXT NOT NULL DEFAULT '[]',
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    ensure_session_columns(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_csrf_expires_at ON csrf_tokens(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_csrf_username_expires ON csrf_tokens(username, expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_captcha_expires_at ON captcha_challenges(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_user_time ON login_attempts(user_id, attempted_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts(ip_address, attempted_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_user_success_time ON login_attempts(user_id, success, attempted_at)")


def get_auth_db(db_path):
    conn = _open_sqlite(db_path)
    path = str(db_path)
    if path not in _ENSURED_AUTH_DB_PATHS:
        with _ENSURE_LOCK:
            if path not in _ENSURED_AUTH_DB_PATHS:
                ensure_auth_db_schema(conn)
                conn.commit()
                _ENSURED_AUTH_DB_PATHS.add(path)
    return track_request_connection(conn)


def get_control_db(db_path):
    conn = _open_sqlite(db_path)
    path = str(db_path)
    if path not in _ENSURED_CONTROL_DB_PATHS:
        with _ENSURE_LOCK:
            if path not in _ENSURED_CONTROL_DB_PATHS:
                from services.snapshots.schema import ensure_control_db_schema

                ensure_control_db_schema(conn)
                conn.commit()
                _ENSURED_CONTROL_DB_PATHS.add(path)
    return track_request_connection(conn)


def ensure_security_support_schema(
    conn,
    *,
    ensure_member_level_rules_schema,
    ensure_moderation_proposals_schema,
    ensure_governance_records_schema,
    ensure_snapshot_schema,
    ensure_upload_security_schema,
    ensure_integrity_schema,
    ensure_account_recovery_schema,
):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS csrf_tokens (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash   TEXT NOT NULL UNIQUE,
            username     TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ip_blocks (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address     TEXT NOT NULL UNIQUE,
            blocked_until  TEXT NOT NULL,
            reason         TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_locations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ip_hash       TEXT NOT NULL,
            country       TEXT,
            city          TEXT,
            login_at      TEXT NOT NULL,
            is_suspicious INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ip_blocks_ip ON ip_blocks(ip_address)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ip_blocks_until ON ip_blocks(blocked_until)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_locations_user ON login_locations(user_id, login_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_locations_ip ON login_locations(ip_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_csrf_expires_at ON csrf_tokens(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_event_type_ip_time ON security_events(event_type, ip_address, created_at)")
    ensure_member_level_rules_schema(conn)
    ensure_moderation_proposals_schema(conn)
    ensure_governance_records_schema(conn)
    ensure_snapshot_schema(conn)
    ensure_upload_security_schema(conn)
    ensure_integrity_schema(conn)
    ensure_account_recovery_schema(conn)

    legacy_rows = conn.execute(
        "SELECT ip_address, detail, created_at FROM security_events "
        "WHERE event_type='ip_block' ORDER BY id DESC"
    ).fetchall()
    seen = set()
    for row in legacy_rows:
        ip = row["ip_address"]
        if not ip or ip in seen:
            continue
        seen.add(ip)
        detail = row["detail"] or ""
        match = re.search(r"blocked_until=([0-9T:\-\.]+)", detail)
        if not match:
            continue
        blocked_until = match.group(1)
        conn.execute(
            "INSERT OR IGNORE INTO ip_blocks (ip_address, blocked_until, reason, created_at) VALUES (?, ?, ?, ?)",
            (ip, blocked_until, detail, row["created_at"] or datetime.now().isoformat()),
        )


def db_get_user_role(username, *, get_db):
    conn = get_db()
    try:
        row = conn.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
        return row["role"] if row else None
    finally:
        conn.close()


def activate_emergency_lockdown(
    reason,
    *,
    get_db,
    init_system_settings_table,
    refresh_system_settings,
    audit,
    get_client_ip_func,
):
    conn = get_db()
    try:
        init_system_settings_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("maintenance_mode", "True", datetime.now().isoformat(), "audit_guard"),
        )
        conn.commit()
        refresh_system_settings()
    finally:
        conn.close()
    try:
        audit("EMERGENCY_LOCKDOWN_ENABLED", get_client_ip_func(), user="audit_guard", success=True, detail=reason)
    except Exception:
        pass
_ENSURED_AUTH_DB_PATHS: set[str] = set()
_ENSURED_AUDIT_DB_PATHS: set[str] = set()
_ENSURED_CONTROL_DB_PATHS: set[str] = set()
_ENSURE_LOCK = threading.Lock()
