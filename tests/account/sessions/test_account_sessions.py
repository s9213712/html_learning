import hashlib
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, make_response

from routes.users import register_user_routes


ROOT = Path(__file__).resolve().parents[3]


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _build_app(
    db_path,
    actor_box,
    revoke_user_sessions=None,
    delete_csrf_tokens_for_username=None,
    enable_foreign_keys=False,
    validate_password=None,
    enforce_password_strength=None,
    count_role=None,
    system_settings=None,
    points_service=None,
):
    app = Flask(__name__)
    app.testing = True

    def get_db():
        conn = sqlite3.connect(db_path)
        if enable_foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def json_resp(payload, status=200):
        return make_response(jsonify(payload), status)

    def default_add_violation(*args, **kwargs):
        if kwargs.get("return_violation_id"):
            return ("none", "ok", 0, 1)
        return ("none", "ok", 0)

    register_user_routes(app, {
        "ACCOUNT_STATUSES": {"active", "inactive", "pending", "rejected"},
        "MAX_MANAGERS": 5,
        "MAX_EXTRA_SUPER_ADMINS": 2,
        "MEMBER_LEVELS": {"newbie", "normal", "trusted", "vip", "restricted", "suspended"},
        "PASSWORD_HISTORY_LIMIT": 5,
        "ROLE_LABEL": {"user": "一般用戶", "manager": "管理者", "super_admin": "最高管理者"},
        "ROLE_RANK": {"user": 0, "manager": 1, "super_admin": 2},
        "add_violation": default_add_violation,
        "audit": lambda *args, **kwargs: None,
        "check_user_rate_limit": lambda *args, **kwargs: (False, {"limit": 10}),
        "count_role": count_role or (lambda role: 0),
        "db_get_user_from_token": lambda *args, **kwargs: None,
        "db_get_user_role": lambda *args, **kwargs: "user",
        "delete_csrf_tokens_for_username": delete_csrf_tokens_for_username or (lambda username: None),
        "decrypt_field": lambda value: value or "",
        "encrypt_field": lambda value: value,
        "ensure_user_official_room_membership": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": get_db,
        "get_system_settings": lambda: dict(system_settings or {}),
        "get_ua": lambda: "test-agent",
        "hash_password": lambda value: value,
        "hash_token": _hash_token,
        "is_feature_enabled": lambda key: key == "feature_account_security_enabled",
        "json_resp": json_resp,
        "normalize_text": lambda value: value.strip() if isinstance(value, str) else "",
        "parse_birthdate": lambda value: value,
        "parse_positive_int": lambda value, default=None, **kwargs: int(value) if value is not None else default,
        "points_service": points_service,
        "revoke_user_sessions": revoke_user_sessions or (lambda *args, **kwargs: None),
        "require_csrf": lambda fn: fn,
        "require_csrf_safe": lambda fn: fn,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": False,
        "enforce_password_strength": enforce_password_strength or (lambda value, min_score=3: (True, "OK", {"score": 4})),
        "score_password_strength": lambda value: {"score": 4},
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "user_public_payload": lambda row, include_sensitive=False: dict(row),
        "validate_id_number": lambda value: True,
        "validate_password": validate_password or (lambda value: (True, "OK")),
        "validate_phone": lambda value: True,
        "verify_password": lambda stored, provided: stored == provided,
    })
    return app


def _seed_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            role TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            ip_address TEXT,
            user_agent TEXT,
            device_info TEXT,
            ip_country TEXT,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (1, 'alice', 'active', 'user')")
    conn.executemany(
        "INSERT INTO sessions (user_id, token_hash, ip_address, user_agent, device_info, ip_country, expires_at, is_revoked, last_seen, created_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        [
            (_hash_token("current"), "127.0.0.1", "Chrome", '{"browser":"Chrome","os":"Linux","device":"Desktop"}', None, "2999-01-01T00:00:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            (_hash_token("remote"), "10.0.0.2", "Firefox", '{"browser":"Firefox","os":"Windows","device":"Desktop"}', "TW", "2999-01-01T00:00:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        ],
    )
    conn.commit()
    conn.close()


def test_account_sessions_list_marks_current_and_revokes_remote(tmp_path):
    db_path = tmp_path / "sessions.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "status": "active"}}
    client = _build_app(str(db_path), actor_box).test_client()
    client.set_cookie("session_token", "current")

    listed = client.get("/api/account/sessions")
    assert listed.status_code == 200
    sessions = listed.get_json()["sessions"]
    assert len(sessions) == 2
    assert [item for item in sessions if item["is_current"]][0]["device_info"]["browser"] == "Chrome"

    deleted = client.delete("/api/account/sessions/2")
    assert deleted.status_code == 200
    assert deleted.get_json()["current_revoked"] is False

    conn = sqlite3.connect(db_path)
    revoked = conn.execute("SELECT is_revoked FROM sessions WHERE id=2").fetchone()[0]
    current = conn.execute("SELECT is_revoked FROM sessions WHERE id=1").fetchone()[0]
    conn.close()
    assert revoked == 1
    assert current == 0


def test_admin_users_include_online_status_from_active_sessions(tmp_path):
    db_path = tmp_path / "admin-users-online.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (1, 'admin', 'active', 'manager')")
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (2, 'test', 'active', 'user')")
    conn.execute(
        "INSERT INTO sessions (user_id, token_hash, expires_at, is_revoked, last_seen, created_at) VALUES (2, 'tok', '2999-01-01T00:00:00', 0, ?, ?)",
        ("2999-01-01T00:00:00", "2999-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    actor_box = {"actor": {"id": 1, "username": "admin", "role": "manager", "status": "active"}}
    client = _build_app(str(db_path), actor_box).test_client()

    res = client.get("/api/admin/users")

    assert res.status_code == 200
    users = {row["username"]: row for row in res.get_json()["users"]}
    assert users["test"]["is_online"] is True
    assert users["test"]["online_status"] == "online"
    assert users["test"]["active_session_count"] == 1
    assert users["admin"]["is_online"] is False


def test_admin_users_session_lookup_is_scoped_to_visible_page():
    users_py = (ROOT / "routes" / "users.py").read_text(encoding="utf-8")

    assert "def active_session_map(auth_conn, user_ids=None)" in users_py
    assert "AND user_id IN ({placeholders})" in users_py
    assert 'active_session_map(auth_conn, [int(row["id"]) for row in rows])' in users_py


def test_admin_user_block_rejects_self_block_for_root_and_manager(tmp_path):
    db_path = tmp_path / "admin-self-block.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            blocked_until TEXT
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (1, 'root', 'active', 'super_admin')")
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (2, 'admin', 'active', 'manager')")
    conn.commit()
    conn.close()

    root_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    root_client = _build_app(str(db_path), root_box).test_client()
    root_res = root_client.post("/api/admin/users/1/block", json={"minutes": 60})
    assert root_res.status_code == 400
    assert root_res.get_json()["msg"] == "不能封鎖目前登入中的自己"

    admin_box = {"actor": {"id": 2, "username": "admin", "role": "manager", "status": "active"}}
    admin_client = _build_app(str(db_path), admin_box).test_client()
    admin_res = admin_client.post("/api/admin/users/2/block", json={"minutes": 60})
    assert admin_res.status_code == 400
    assert admin_res.get_json()["msg"] == "不能封鎖目前登入中的自己"

    conn = sqlite3.connect(db_path)
    rows = {
        row[0]: row[1:]
        for row in conn.execute("SELECT username, status, blocked_until FROM users ORDER BY id").fetchall()
    }
    conn.close()
    assert rows["root"] == ("active", None)
    assert rows["admin"] == ("active", None)


def test_reject_registration_deletes_pending_account(tmp_path):
    db_path = tmp_path / "registration-review.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            role TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (1, 'admin', 'active', 'manager')")
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (2, 'pending_user', 'pending', 'user')")
    conn.execute(
        "INSERT INTO sessions (user_id, token_hash, expires_at, is_revoked, last_seen, created_at) VALUES (2, 'pendingtok', '2999-01-01T00:00:00', 0, ?, ?)",
        ("2999-01-01T00:00:00", "2999-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    revoked = []
    deleted_csrf = []
    actor_box = {"actor": {"id": 1, "username": "admin", "role": "manager", "status": "active"}}
    client = _build_app(
        str(db_path),
        actor_box,
        revoke_user_sessions=lambda user_id: revoked.append(user_id),
        delete_csrf_tokens_for_username=lambda username: deleted_csrf.append(username),
    ).test_client()

    res = client.post("/api/admin/users/2/review-registration", json={"action": "reject"})

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["status"] == "deleted"
    assert "刪除" in body["msg"]
    assert revoked == [2]
    assert deleted_csrf == ["pending_user"]
    conn = sqlite3.connect(db_path)
    remaining = conn.execute("SELECT COUNT(*) FROM users WHERE id=2").fetchone()[0]
    conn.close()
    assert remaining == 0


def test_delete_user_repairs_missing_optional_announcement_table(tmp_path):
    db_path = tmp_path / "delete-user-legacy-fk.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY
        );
        CREATE TABLE announcement_attachment_requests (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            requested_by INTEGER NOT NULL REFERENCES users(id),
            announcement_id INTEGER REFERENCES announcements(id),
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_by INTEGER REFERENCES users(id),
            reviewed_at TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (1, 'root', 'active', 'super_admin')")
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (4, 'pending_user', 'pending', 'user')")
    conn.commit()
    conn.close()
    revoked = []
    deleted_csrf = []
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(
        str(db_path),
        actor_box,
        revoke_user_sessions=lambda user_id: revoked.append(user_id),
        delete_csrf_tokens_for_username=lambda username: deleted_csrf.append(username),
        enable_foreign_keys=True,
    ).test_client()

    res = client.delete("/api/admin/users/4")

    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert revoked == [4]
    assert deleted_csrf == ["pending_user"]
    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT status FROM users WHERE id=4").fetchone()[0]
    assert status == "deleted"
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='announcements'").fetchone()
    conn.close()


def test_delete_user_cleans_legacy_user_foreign_keys_without_cascade(tmp_path):
    db_path = tmp_path / "delete-user-legacy-user-refs.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY
        );
        CREATE TABLE announcement_attachment_requests (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            requested_by INTEGER NOT NULL REFERENCES users(id),
            reviewed_by INTEGER REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE legacy_nullable_refs (
            id INTEGER PRIMARY KEY,
            reviewed_by INTEGER REFERENCES users(id)
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (1, 'root', 'active', 'super_admin')")
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (5, 'pending_user', 'pending', 'user')")
    conn.execute("INSERT INTO uploaded_files (id) VALUES ('f1')")
    conn.execute(
        "INSERT INTO announcement_attachment_requests (id, file_id, requested_by, reviewed_by, status, created_at) "
        "VALUES ('r1', 'f1', 5, 5, 'pending', '2026-01-01T00:00:00')"
    )
    conn.execute("INSERT INTO legacy_nullable_refs (id, reviewed_by) VALUES (1, 5)")
    conn.commit()
    conn.close()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(str(db_path), actor_box, enable_foreign_keys=True).test_client()

    res = client.delete("/api/admin/users/5")

    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    conn = sqlite3.connect(db_path)
    user_row = conn.execute("SELECT status FROM users WHERE id=5").fetchone()
    assert user_row[0] == "deleted"
    assert conn.execute("SELECT COUNT(*) FROM announcement_attachment_requests WHERE requested_by=5 OR reviewed_by=5").fetchone()[0] == 1
    assert conn.execute("SELECT reviewed_by FROM legacy_nullable_refs WHERE id=1").fetchone()[0] == 5
    conn.close()


def test_delete_user_closes_wallet_and_soft_deletes_cloud_drive_without_breaking_history(tmp_path):
    db_path = tmp_path / "delete-user-soft-delete.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE points_wallets (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            soft_balance INTEGER NOT NULL DEFAULT 0,
            hard_balance INTEGER NOT NULL DEFAULT 0,
            soft_frozen INTEGER NOT NULL DEFAULT 0,
            hard_frozen INTEGER NOT NULL DEFAULT 0,
            total_soft_earned INTEGER NOT NULL DEFAULT 0,
            total_hard_earned INTEGER NOT NULL DEFAULT 0,
            total_soft_spent INTEGER NOT NULL DEFAULT 0,
            total_hard_spent INTEGER NOT NULL DEFAULT 0,
            wallet_status TEXT NOT NULL DEFAULT 'active',
            risk_level TEXT NOT NULL DEFAULT 'normal',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            deleted_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE storage_files (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            is_trashed INTEGER NOT NULL DEFAULT 0,
            trashed_at TEXT,
            deleted_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE storage_folders (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            deleted_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE storage_quota_overrides (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE video_comments (
            id INTEGER PRIMARY KEY,
            video_id INTEGER,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (1, 'root', 'active', 'super_admin')")
    conn.execute("INSERT INTO users (id, username, status, role) VALUES (7, 'wallet_user', 'active', 'user')")
    conn.execute(
        "INSERT INTO points_wallets (user_id, created_at, updated_at) VALUES (7, '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.execute("INSERT INTO uploaded_files (id, owner_user_id) VALUES ('f1', 7)")
    conn.execute("INSERT INTO storage_files (id, file_id, owner_user_id) VALUES ('sf1', 'f1', 7)")
    conn.execute("INSERT INTO storage_folders (id, owner_user_id) VALUES ('dir1', 7)")
    conn.execute("INSERT INTO storage_quota_overrides (user_id, enabled) VALUES (7, 1)")
    conn.execute("INSERT INTO video_comments (id, video_id, user_id, content) VALUES (1, 100, 7, 'hello')")
    conn.commit()
    conn.close()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(str(db_path), actor_box, enable_foreign_keys=True).test_client()

    res = client.delete("/api/admin/users/7")

    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert res.get_json()["cleanup"]["storage_quota_overrides_deleted"] == 1
    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT status FROM users WHERE id=7").fetchone()[0]
    wallet = conn.execute("SELECT wallet_status, risk_level FROM points_wallets WHERE user_id=7").fetchone()
    file_deleted_at = conn.execute("SELECT deleted_at FROM uploaded_files WHERE id='f1'").fetchone()[0]
    storage_row = conn.execute("SELECT is_trashed, deleted_at FROM storage_files WHERE id='sf1'").fetchone()
    folder_deleted_at = conn.execute("SELECT deleted_at FROM storage_folders WHERE id='dir1'").fetchone()[0]
    quota_override_count = conn.execute(
        "SELECT COUNT(*) FROM storage_quota_overrides WHERE user_id=7"
    ).fetchone()[0]
    comment_count = conn.execute("SELECT COUNT(*) FROM video_comments WHERE user_id=7").fetchone()[0]
    assert status == "deleted"
    assert wallet == ("closed", "blocked")
    assert file_deleted_at is not None
    assert storage_row[0] == 1
    assert storage_row[1] is not None
    assert folder_deleted_at is not None
    assert quota_override_count == 0
    assert comment_count == 1
    conn.close()


def test_admin_users_list_hides_deleted_accounts_by_default(tmp_path):
    db_path = tmp_path / "admin-users-hide-deleted.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level) VALUES (1, 'root', 'active', 'super_admin', 'normal', 'normal', 'normal')")
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level) VALUES (2, 'active_user', 'active', 'user', 'trusted', 'trusted', 'trusted')")
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level) VALUES (3, 'deleted_user', 'deleted', 'user', 'vip', 'vip', 'vip')")
    conn.commit()
    conn.close()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(str(db_path), actor_box, enable_foreign_keys=True).test_client()

    res = client.get("/api/admin/users")
    body = res.get_json()

    assert res.status_code == 200
    assert body["ok"] is True
    usernames = [item["username"] for item in body["users"]]
    assert "active_user" in usernames
    assert "deleted_user" not in usernames


def test_admin_users_list_includes_official_hot_wallet_live_balance(tmp_path):
    db_path = tmp_path / "admin-users-hot-wallet-balance.db"
    hot_address = "pc0" + ("a" * 48)

    class FakePointsService:
        def ensure_schema(self, _conn):
            return None

        def _wallet_identity_balances_for_user(self, _conn, user_id):
            if int(user_id) != 2:
                return {"balances": {}}
            return {
                "balances": {
                    hot_address: {
                        "balance": 1234,
                        "frozen": 56,
                        "pending_outgoing": 7,
                    }
                }
            }

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE points_wallet_identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            address TEXT NOT NULL UNIQUE,
            wallet_type TEXT NOT NULL,
            custody_mode TEXT NOT NULL,
            key_algorithm TEXT NOT NULL,
            public_key_jwk_json TEXT NOT NULL DEFAULT '{}',
            public_key_hash TEXT NOT NULL,
            server_private_key_stored INTEGER NOT NULL DEFAULT 0,
            is_primary INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            label TEXT NOT NULL DEFAULT '',
            backup_confirmed_at TEXT,
            imported_at TEXT,
            revoked_at TEXT,
            wallet_scope TEXT NOT NULL DEFAULT 'user',
            spend_capability TEXT NOT NULL DEFAULT 'enabled',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level) VALUES (1, 'root', 'active', 'super_admin', 'normal', 'normal', 'normal')")
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level) VALUES (2, 'alice', 'active', 'user', 'trusted', 'trusted', 'trusted')")
    conn.execute(
        """
        INSERT INTO points_wallet_identities (
            user_id, address, wallet_type, custody_mode, key_algorithm, public_key_hash,
            is_primary, status, label, backup_confirmed_at, created_at, updated_at
        ) VALUES (2, ?, 'official_hot', 'server_hot', 'SYSTEM_SIMULATED_V1', 'hot-hash', 1, 'active', '官方熱錢包', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (hot_address,),
    )
    conn.commit()
    conn.close()

    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(str(db_path), actor_box, enable_foreign_keys=True, points_service=FakePointsService()).test_client()

    res = client.get("/api/admin/users")
    body = res.get_json()
    alice = next(row for row in body["users"] if row["username"] == "alice")

    assert res.status_code == 200
    assert alice["official_hot_wallet_address"] == hot_address
    assert alice["official_hot_wallet_balance"] == 1234
    assert alice["official_hot_wallet_frozen"] == 56
    assert alice["official_hot_wallet_pending_outgoing"] == 7
    assert alice["official_hot_wallets"][0]["points_balance"] == 1234


def test_admin_users_list_paginates_and_sorts_by_id(tmp_path):
    db_path = tmp_path / "admin-users-pagination.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    rows = [
        (1, "root", "active", "super_admin"),
        (2, "zed_user", "active", "user"),
        (3, "alpha_user", "active", "user"),
        (4, "manager_user", "active", "manager"),
        (5, "beta_user", "active", "user"),
    ]
    conn.executemany(
        "INSERT INTO users (id, username, status, role, member_level, base_level, effective_level) VALUES (?, ?, ?, ?, 'normal', 'normal', 'normal')",
        rows,
    )
    conn.commit()
    conn.close()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(str(db_path), actor_box, enable_foreign_keys=True).test_client()

    res = client.get("/api/admin/users?page=2&page_size=2")
    body = res.get_json()

    assert res.status_code == 200
    assert body["ok"] is True
    assert [row["id"] for row in body["users"]] == [3, 4]
    assert body["pagination"] == {
        "page": 2,
        "page_size": 2,
        "total": 5,
        "total_pages": 3,
        "sort": "id",
        "order": "asc",
        "q": "",
    }
    assert body["role_counts"]["manager"] == 1
    assert body["role_counts"]["super_admin"] == 1


def test_soft_delete_releases_original_username_for_reuse(tmp_path):
    db_path = tmp_path / "soft-delete-release-username.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0,
            deleted_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            must_change_password INTEGER DEFAULT 0,
            is_default_password INTEGER DEFAULT 0
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role, created_at, updated_at) VALUES (1, 'root', 'active', 'super_admin', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO users (id, username, status, role, created_at, updated_at) VALUES (2, 'reuse_me', 'active', 'user', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()


def test_admin_create_user_accepts_minimal_required_fields(tmp_path):
    db_path = tmp_path / "admin-create-user-minimal.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level, created_at, updated_at) VALUES (1, 'root', 'active', 'super_admin', 'normal', 'normal', 'normal', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(str(db_path), actor_box, enable_foreign_keys=True).test_client()

    res = client.post(
        "/api/admin/users",
        json={
            "username": "minimal_user",
            "password": "AdminCreate#123",
            "password_confirm": "AdminCreate#123",
            "nickname": "Minimal",
            "role": "user",
            "status": "active",
        },
    )
    body = res.get_json()

    assert res.status_code == 200
    assert body["ok"] is True
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT username, status, role, member_level, base_level, effective_level FROM users WHERE username='minimal_user'").fetchone()
    assert row == ("minimal_user", "active", "user", "trusted", "trusted", "trusted")
    conn.close()


def test_admin_create_manager_obeys_root_configured_seat_limit(tmp_path):
    db_path = tmp_path / "admin-create-manager-seat-limit.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level, created_at, updated_at) VALUES (1, 'root', 'active', 'super_admin', 'normal', 'normal', 'normal', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level, created_at, updated_at) VALUES (2, 'admin', 'active', 'manager', 'normal', 'normal', 'normal', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(
        str(db_path),
        actor_box,
        enable_foreign_keys=True,
        count_role=lambda role: 1 if role == "manager" else 0,
        system_settings={"max_manager_seats": 1},
    ).test_client()

    res = client.post(
        "/api/admin/users",
        json={
            "username": "blocked_manager",
            "password": "AdminCreate#123",
            "password_confirm": "AdminCreate#123",
            "nickname": "Blocked Manager",
            "role": "manager",
            "status": "active",
        },
    )

    assert res.status_code == 409
    assert res.get_json()["msg"] == "管理者已達上限（1 人）"
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT 1 FROM users WHERE username='blocked_manager'").fetchone()
    conn.close()
    assert row is None


def test_admin_promote_user_succeeds_when_csrf_cleanup_writes_same_db(tmp_path):
    db_path = tmp_path / "admin-promote-lock-regression.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT,
            base_level TEXT,
            effective_level TEXT,
            trust_score INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            violation_score INTEGER DEFAULT 0,
            sanction_status TEXT,
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER DEFAULT 0,
            avatar_file_id INTEGER,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE csrf_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            revoked_at TEXT
        );
        """
    )
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level, created_at, updated_at) VALUES (1, 'root', 'active', 'super_admin', 'normal', 'normal', 'normal', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO users (id, username, status, role, member_level, base_level, effective_level, created_at, updated_at) VALUES (2, 'promote_me', 'active', 'user', 'normal', 'normal', 'normal', '2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO csrf_tokens (username, revoked_at) VALUES ('promote_me', NULL)")
    conn.commit()
    conn.close()

    def cleanup_csrf(username):
        db = sqlite3.connect(db_path)
        db.execute("UPDATE csrf_tokens SET revoked_at='2026-01-02T00:00:00' WHERE username=?", (username,))
        db.commit()
        db.close()

    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(
        str(db_path),
        actor_box,
        delete_csrf_tokens_for_username=cleanup_csrf,
        enable_foreign_keys=True,
    ).test_client()

    res = client.post("/api/admin/users/2/promote", json={})
    body = res.get_json()

    assert res.status_code == 200
    assert body["ok"] is True
    conn = sqlite3.connect(db_path)
    user_row = conn.execute("SELECT role FROM users WHERE id=2").fetchone()
    csrf_row = conn.execute("SELECT revoked_at FROM csrf_tokens WHERE username='promote_me'").fetchone()
    assert user_row == ("manager",)
    assert csrf_row == ("2026-01-02T00:00:00",)
    conn.close()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(str(db_path), actor_box, enable_foreign_keys=True).test_client()

    res = client.delete("/api/admin/users/2")
    body = res.get_json()

    assert res.status_code == 200
    assert body["ok"] is True
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT username, status FROM users WHERE id=2").fetchone()
    assert row[1] == "deleted"
    assert row[0] != "reuse_me"
    conn.execute(
        "INSERT INTO users (username, status, role, created_at, updated_at) VALUES (?, 'pending', 'user', '2026-01-02T00:00:00', '2026-01-02T00:00:00')",
        ("reuse_me",),
    )
    conn.commit()
    conn.close()


def test_account_sessions_logout_all_can_keep_current(tmp_path):
    db_path = tmp_path / "sessions.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "status": "active"}}
    client = _build_app(str(db_path), actor_box).test_client()
    client.set_cookie("session_token", "current")

    res = client.post("/api/account/sessions/logout-all", json={"keep_current": True})
    assert res.status_code == 200
    assert res.get_json()["current_revoked"] is False

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id, is_revoked FROM sessions ORDER BY id").fetchall()
    conn.close()
    assert rows == [(1, 0), (2, 1)]


def test_password_change_revokes_existing_sessions(tmp_path):
    db_path = tmp_path / "password-change.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            member_level TEXT NOT NULL DEFAULT 'normal',
            trust_score INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            reputation INTEGER NOT NULL DEFAULT 0,
            password_strength_score INTEGER NOT NULL DEFAULT 0,
            blocked_until TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0,
            password_changed_at TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            is_default_password INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT
        );
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            ip_address TEXT,
            user_agent TEXT,
            device_info TEXT,
            ip_country TEXT,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO users (id, username, role, status, must_change_password, is_default_password, updated_at) "
        "VALUES (1, 'alice', 'user', 'active', 1, 1, '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, 'oldpass', '2026-01-01T00:00:00')"
    )
    conn.executemany(
        "INSERT INTO sessions (user_id, token_hash, expires_at, is_revoked, created_at) VALUES (1, ?, '2999-01-01T00:00:00', 0, '2026-01-01T00:00:00')",
        [(_hash_token("current"),), (_hash_token("remote"),)],
    )
    conn.commit()
    conn.close()

    def revoke_sessions(user_id, **kwargs):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET is_revoked=1, revoked_at='now' WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "status": "active"}}
    client = _build_app(str(db_path), actor_box, revoke_user_sessions=revoke_sessions).test_client()
    res = client.put(
        "/api/admin/users/1",
        json={"current_password": "oldpass", "password": "Newpass@123", "password_confirm": "Newpass@123"},
    )

    assert res.status_code == 200

    conn = sqlite3.connect(db_path)
    user_flags = conn.execute("SELECT must_change_password, is_default_password FROM users WHERE id=1").fetchone()
    revoked = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_revoked=1").fetchone()[0]
    passwords = conn.execute("SELECT COUNT(*) FROM user_passwords WHERE user_id=1").fetchone()[0]
    conn.close()
    assert revoked == 2
    assert passwords == 2
    assert user_flags == (0, 0)


def test_self_password_change_revokes_sessions_without_root_security_alert_noise(tmp_path):
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT NOT NULL DEFAULT 'normal',
            trust_score INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            reputation INTEGER NOT NULL DEFAULT 0,
            password_strength_score INTEGER NOT NULL DEFAULT 0,
            blocked_until TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0,
            password_changed_at TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            is_default_password INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT
        );
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            ip_address TEXT,
            user_agent TEXT,
            device_info TEXT,
            ip_country TEXT,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO users (id, username, role, status, must_change_password, is_default_password, updated_at) "
        "VALUES (1, 'alice', 'user', 'active', 1, 1, '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, 'oldpass', '2026-01-01T00:00:00')"
    )
    conn.executemany(
        "INSERT INTO sessions (user_id, token_hash, expires_at, is_revoked, created_at) VALUES (1, ?, '2999-01-01T00:00:00', 0, '2026-01-01T00:00:00')",
        [(_hash_token("current"),), (_hash_token("remote"),)],
    )
    conn.commit()
    conn.close()

    revoke_calls = []

    def revoke_sessions(user_id, **kwargs):
        revoke_calls.append((user_id, kwargs))
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET is_revoked=1, revoked_at='now' WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "status": "active"}}
    client = _build_app(str(db_path), actor_box, revoke_user_sessions=revoke_sessions).test_client()
    res = client.put(
        "/api/admin/users/1",
        json={"current_password": "oldpass", "password": "Newpass@123", "password_confirm": "Newpass@123"},
    )

    assert res.status_code == 200
    assert revoke_calls == [(1, {"notify_security_event": False, "detail": "self_password_change"})]


def test_password_change_rejects_same_as_current_password(tmp_path):
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT NOT NULL DEFAULT 'normal',
            trust_score INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            reputation INTEGER NOT NULL DEFAULT 0,
            password_strength_score INTEGER NOT NULL DEFAULT 0,
            blocked_until TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0,
            password_changed_at TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            is_default_password INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT
        );
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            ip_address TEXT,
            user_agent TEXT,
            device_info TEXT,
            ip_country TEXT,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO users (id, username, role, status, must_change_password, is_default_password, updated_at) "
        "VALUES (1, 'alice', 'user', 'active', 1, 1, '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, 'oldpass', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "status": "active"}}
    client = _build_app(str(db_path), actor_box).test_client()
    res = client.put(
        "/api/admin/users/1",
        json={"current_password": "oldpass", "password": "oldpass", "password_confirm": "oldpass"},
    )

    assert res.status_code == 400
    assert res.get_json()["msg"] == "新密碼不可與目前密碼相同"

    conn = sqlite3.connect(db_path)
    passwords = conn.execute("SELECT COUNT(*) FROM user_passwords WHERE user_id=1").fetchone()[0]
    conn.close()
    assert passwords == 1


def test_root_password_change_bypasses_password_policy(tmp_path):
    db_path = tmp_path / "root-password-change.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT NOT NULL DEFAULT 'normal',
            trust_score INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            reputation INTEGER NOT NULL DEFAULT 0,
            password_strength_score INTEGER NOT NULL DEFAULT 0,
            blocked_until TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0,
            password_changed_at TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            is_default_password INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT
        );
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            ip_address TEXT,
            user_agent TEXT,
            device_info TEXT,
            ip_country TEXT,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO users (id, username, role, status, must_change_password, is_default_password, updated_at) "
        "VALUES (1, 'root', 'super_admin', 'active', 1, 1, '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, 'oldpass', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin", "status": "active"}}
    client = _build_app(
        str(db_path),
        actor_box,
        validate_password=lambda value: (False, "密碼規則應被略過"),
        enforce_password_strength=lambda value, min_score=3: (False, "強度規則應被略過", {"score": 0}),
    ).test_client()
    res = client.put(
        "/api/admin/users/1",
        json={"current_password": "oldpass", "password": "x", "password_confirm": "x"},
    )

    assert res.status_code == 200
    conn = sqlite3.connect(db_path)
    try:
        latest_pw = conn.execute("SELECT password_hash FROM user_passwords ORDER BY id DESC LIMIT 1").fetchone()[0]
        passwords = conn.execute("SELECT COUNT(*) FROM user_passwords WHERE user_id=1").fetchone()[0]
    finally:
        conn.close()
    assert latest_pw == "x"
    assert passwords == 2


def test_non_root_password_change_still_follows_password_policy(tmp_path):
    db_path = tmp_path / "user-password-change.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            member_level TEXT NOT NULL DEFAULT 'normal',
            trust_score INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            reputation INTEGER NOT NULL DEFAULT 0,
            password_strength_score INTEGER NOT NULL DEFAULT 0,
            blocked_until TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0,
            password_changed_at TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            is_default_password INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT
        );
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            ip_address TEXT,
            user_agent TEXT,
            device_info TEXT,
            ip_country TEXT,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO users (id, username, role, status, must_change_password, is_default_password, updated_at) "
        "VALUES (1, 'alice', 'user', 'active', 1, 1, '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, 'oldpass', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "status": "active"}}
    client = _build_app(
        str(db_path),
        actor_box,
        validate_password=lambda value: (False, "密碼太弱"),
    ).test_client()
    res = client.put(
        "/api/admin/users/1",
        json={"current_password": "oldpass", "password": "x", "password_confirm": "x"},
    )

    assert res.status_code == 400
    assert res.get_json()["msg"] == "密碼太弱"
