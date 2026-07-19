import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, make_response
from cryptography.fernet import Fernet

from routes.public import register_public_routes
from services.security.captcha import create_captcha_challenge
from services.server.database import get_auth_db
from services.points_chain import BIRTHDAY_GIFT_POINTS, PointsLedgerService
from services.storage.quota_purchases import (
    BIRTHDAY_STORAGE_GIFT_BYTES,
    BIRTHDAY_STORAGE_GIFT_ITEM_KEY,
    active_storage_quota_purchases,
)
from services.users import auth as auth_service


def test_get_auth_db_creates_auth_hot_tables(tmp_path):
    auth_db_path = tmp_path / "auth.db"
    conn = get_auth_db(str(auth_db_path))
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        csrf_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(csrf_tokens)").fetchall()}
        session_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(sessions)").fetchall()}
        login_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(login_attempts)").fetchall()}
    finally:
        conn.close()
    assert {"csrf_tokens", "captcha_challenges", "login_attempts", "sessions"} <= tables
    assert "idx_csrf_username_expires" in csrf_indexes
    assert "idx_sessions_user_active" in session_indexes
    assert "idx_login_attempts_user_success_time" in login_indexes


def test_auth_service_writes_hot_auth_state_to_split_db_only(tmp_path):
    main_db_path = tmp_path / "database.db"
    auth_db_path = tmp_path / "auth.db"

    main_conn = sqlite3.connect(main_db_path)
    try:
        main_conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """
        )
        main_conn.execute(
            "CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        main_conn.execute(
            "INSERT INTO users (id, username, status) VALUES (1, 'alice', 'active')"
        )
        main_conn.commit()
    finally:
        main_conn.close()

    def _get_main_db():
        conn = sqlite3.connect(main_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_auth_db():
        return get_auth_db(str(auth_db_path))

    auth_service.configure_auth_service(
        get_db=_get_main_db,
        get_auth_db=_get_auth_db,
        get_user_by_username=lambda username: None,
        fernet=Fernet(Fernet.generate_key()),
        get_runtime_server_mode=lambda: "dev_ready",
    )

    auth_service.store_csrf_token("csrf-token", "alice")
    auth_service.record_login_attempt(
        user_id=1,
        ip_address="127.0.0.1",
        user_agent="pytest",
        success=True,
        attempted_at="2026-05-09T12:00:00",
    )
    auth_service.db_save_session(1, "session-token", "127.0.0.1", "pytest")

    captcha_conn = _get_auth_db()
    try:
        challenge = create_captcha_challenge(captcha_conn, mode="math", ttl_seconds=300, ip="127.0.0.1")
        captcha_conn.commit()
    finally:
        captcha_conn.close()

    main_conn = sqlite3.connect(main_db_path)
    try:
        main_tables = {
            row[0]
            for row in main_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        main_conn.close()
    assert "csrf_tokens" not in main_tables
    assert "captcha_challenges" not in main_tables
    assert "login_attempts" not in main_tables
    assert "sessions" not in main_tables

    auth_conn = _get_auth_db()
    try:
        csrf_count = auth_conn.execute("SELECT COUNT(*) FROM csrf_tokens").fetchone()[0]
        captcha_count = auth_conn.execute("SELECT COUNT(*) FROM captcha_challenges").fetchone()[0]
        login_count = auth_conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]
        session_row = auth_conn.execute(
            "SELECT user_id, ip_address, user_agent, is_revoked FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        auth_conn.close()

    assert challenge["required"] is True
    assert csrf_count == 1
    assert captcha_count == 1
    assert login_count == 1
    assert session_row is not None
    assert session_row["user_id"] == 1
    assert session_row["ip_address"] == "127.0.0.1"
    assert session_row["user_agent"] == "pytest"
    assert int(session_row["is_revoked"]) == 0


def test_split_auth_session_uses_main_db_security_epoch_and_remains_valid(tmp_path):
    main_db_path = tmp_path / "database.db"
    auth_db_path = tmp_path / "auth.db"

    main_conn = sqlite3.connect(main_db_path)
    try:
        main_conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            INSERT INTO users (id, username, status)
            VALUES (1, 'alice', 'active');
            INSERT INTO system_settings (key, value)
            VALUES ('server_security_epoch', '37');
            """
        )
        main_conn.commit()
    finally:
        main_conn.close()

    def _get_main_db():
        conn = sqlite3.connect(main_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_auth_db():
        return get_auth_db(str(auth_db_path))

    auth_service.configure_auth_service(
        get_db=_get_main_db,
        get_auth_db=_get_auth_db,
        get_user_by_username=lambda username: None,
        fernet=Fernet(Fernet.generate_key()),
        get_runtime_server_mode=lambda: "dev_ready",
    )

    token = "session-at-current-security-epoch"
    auth_service.db_save_session(1, token, "-", "")

    auth_conn = _get_auth_db()
    try:
        auth_tables = {
            row["name"]
            for row in auth_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        session_row = auth_conn.execute(
            "SELECT session_epoch, is_revoked FROM sessions WHERE token_hash=?",
            (auth_service._hash_token(token),),
        ).fetchone()
    finally:
        auth_conn.close()

    assert "system_settings" not in auth_tables
    assert session_row is not None
    assert int(session_row["session_epoch"]) == 37
    assert int(session_row["is_revoked"]) == 0
    assert auth_service.db_get_user_from_token(token) == "alice"


def _build_public_auth_app(main_db_path, auth_db_path, *, ensure_membership=None, points_service=None):
    app = Flask(__name__)
    app.testing = True

    def _get_main_db():
        conn = sqlite3.connect(main_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_auth_db():
        return get_auth_db(str(auth_db_path))

    def _get_user_by_username(username):
        conn = _get_main_db()
        try:
            row = conn.execute(
                """
                SELECT
                    id,
                    username,
                    status,
                    role,
                    nickname,
                    birthdate,
                    avatar_file_id,
                    must_change_password,
                    is_default_password
                FROM users
                WHERE username=?
                """,
                (username,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    auth_service.configure_auth_service(
        get_db=_get_main_db,
        get_auth_db=_get_auth_db,
        get_user_by_username=_get_user_by_username,
        fernet=Fernet(Fernet.generate_key()),
        get_client_ip=lambda: "127.0.0.1",
        get_runtime_server_mode=lambda: "dev_ready",
    )

    def _json_resp(payload, status=200):
        return make_response(jsonify(payload), status)

    token_counter = {"n": 0}

    def _make_token(username):
        token_counter["n"] += 1
        return f"session-{username}-{token_counter['n']}"

    register_public_routes(app, {
        "CSRF_TOKEN_TTL": 3600,
        "PUBLIC_DIR": str(main_db_path.parent),
        "ROLE_LABEL": {"user": "一般用戶"},
        "SERVER_APP_NAME": "hackme_web",
        "SERVER_RELEASE_ID": "pytest",
        "SERVER_STARTED_AT": "2026-05-09T00:00:00",
        "SERVER_VERSION": "pytest",
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": False,
        "SESSION_TTL": 3600,
        "audit": lambda *args, **kwargs: None,
        "db_delete_session": auth_service.db_delete_session,
        "db_get_user_from_token": auth_service.db_get_user_from_token,
        "db_save_session": auth_service.db_save_session,
        "delete_csrf_token": auth_service.delete_csrf_token,
        "delete_csrf_tokens_for_username": auth_service.delete_csrf_tokens_for_username,
        "decrypt_field": lambda value: value or "",
        "encrypt_field": lambda value: value,
        "ensure_user_official_room_membership": ensure_membership or (lambda *args, **kwargs: None),
        "get_auth_db": _get_auth_db,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": auth_service.get_current_user_ctx,
        "get_db": _get_main_db,
        "get_feature_settings": lambda: {},
        "get_system_settings": lambda: {},
        "get_ua": lambda: "pytest-agent",
        "hash_password": auth_service.hash_password,
        "is_feature_enabled": lambda key: False,
        "is_ip_blocked": lambda ip: False,
        "is_rate_limited": lambda *args, **kwargs: (False, {"limit": 30}),
        "json_resp": _json_resp,
        "make_csrf_token": auth_service.make_csrf_token,
        "make_token": _make_token,
        "normalize_text": lambda value: value.strip() if isinstance(value, str) else "",
        "parse_birthdate": lambda value: value,
        "record_login_failure": lambda *args, **kwargs: None,
        "record_login_attempt": auth_service.record_login_attempt,
        "record_security_event": lambda *args, **kwargs: None,
        "require_csrf": auth_service.require_csrf,
        "require_csrf_safe": auth_service.require_csrf_safe,
        "score_password_strength": lambda value: {"score": 4, "missing": []},
        "store_csrf_token": auth_service.store_csrf_token,
        "timing_delay": lambda: None,
        "validate_id_number": lambda value: True,
        "validate_password": lambda value: (True, "OK"),
        "enforce_password_strength": lambda value, min_score=3: (True, "OK", {"score": 4}),
        "validate_phone": lambda value: True,
        "verify_csrf_double_submit": lambda value: True,
        "verify_csrf_token": auth_service.verify_csrf_token,
        "verify_password": auth_service.verify_password,
        "points_service": points_service,
    })
    return app


def test_public_login_and_me_work_with_split_auth_db(tmp_path):
    main_db_path = tmp_path / "database.db"
    auth_db_path = tmp_path / "auth.db"

    main_conn = sqlite3.connect(main_db_path)
    try:
        main_conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active',
                role TEXT NOT NULL DEFAULT 'user',
                nickname TEXT,
                birthdate TEXT,
                avatar_file_id INTEGER,
                blocked_until TEXT
            );
            CREATE TABLE user_passwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        password_hash = auth_service.hash_password("correct-horse-battery-staple")
        main_conn.execute(
            "INSERT INTO users (id, username, status, role, nickname, birthdate, avatar_file_id) VALUES (1, 'alice', 'active', 'user', 'Alice', NULL, NULL)"
        )
        main_conn.execute(
            "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, ?, '2026-05-09T00:00:00')",
            (password_hash,),
        )
        main_conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('server_security_epoch', '0')"
        )
        main_conn.commit()
    finally:
        main_conn.close()

    app = _build_public_auth_app(main_db_path, auth_db_path)
    client = app.test_client()

    csrf_res = client.get("/api/csrf-token")
    assert csrf_res.status_code == 200
    csrf_token = csrf_res.get_json()["csrf_token"]
    assert csrf_token

    unauth_me_res = client.get("/api/me")
    assert unauth_me_res.status_code == 401
    optional_me_res = client.get("/api/me?optional=1")
    assert optional_me_res.status_code == 200
    optional_me_json = optional_me_res.get_json()
    assert optional_me_json["ok"] is False

    login_res = client.post(
        "/api/login",
        json={"username": "alice", "password": "correct-horse-battery-staple"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert login_res.status_code == 200
    login_json = login_res.get_json()
    assert login_json["ok"] is True
    assert login_json["msg"] == "恭喜登入成功"

    me_res = client.get("/api/me")
    assert me_res.status_code == 200
    me_json = me_res.get_json()
    assert me_json["ok"] is True
    assert me_json["username"] == "alice"
    assert me_json["appearance_settings"] == {}

    auth_conn = get_auth_db(str(auth_db_path))
    try:
        csrf_count = auth_conn.execute("SELECT COUNT(*) FROM csrf_tokens").fetchone()[0]
        login_attempt_count = auth_conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]
        session_count = auth_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        auth_conn.close()

    assert csrf_count >= 1
    assert login_attempt_count == 1
    assert session_count == 1

    main_conn = sqlite3.connect(main_db_path)
    try:
        main_conn.row_factory = sqlite3.Row
        profile_tables = {
            row["name"]
            for row in main_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        profile_count = 0
        if "user_profiles" in profile_tables:
            profile_count = main_conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]
    finally:
        main_conn.close()

    assert profile_count == 0


def test_public_login_awards_birthday_gift_once_for_current_year(tmp_path):
    main_db_path = tmp_path / "database.db"
    auth_db_path = tmp_path / "auth.db"
    today = datetime.now(ZoneInfo("UTC")).date()
    birthdate = today.replace(year=2000).isoformat()

    main_conn = sqlite3.connect(main_db_path)
    try:
        main_conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active',
                role TEXT NOT NULL DEFAULT 'user',
                nickname TEXT,
                birthdate TEXT,
                avatar_file_id INTEGER,
                blocked_until TEXT
            );
            CREATE TABLE user_passwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        password_hash = auth_service.hash_password("correct-horse-battery-staple")
        main_conn.execute(
            "INSERT INTO users (id, username, status, role, nickname, birthdate, avatar_file_id) VALUES (1, 'alice', 'active', 'user', 'Alice', ?, NULL)",
            (birthdate,),
        )
        main_conn.execute(
            "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, ?, '2026-05-09T00:00:00')",
            (password_hash,),
        )
        main_conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('server_security_epoch', '0')"
        )
        main_conn.commit()
    finally:
        main_conn.close()

    def _get_main_db():
        conn = sqlite3.connect(main_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    points_service = PointsLedgerService(
        get_db=_get_main_db,
        chain_secret="test-secret",
        backup_dir=tmp_path / "points_chain_backups",
        mode_reader=lambda: "production",
    )
    app = _build_public_auth_app(main_db_path, auth_db_path, points_service=points_service)
    client = app.test_client()

    csrf_token = client.get("/api/csrf-token").get_json()["csrf_token"]
    login_res = client.post(
        "/api/login",
        json={"username": "alice", "password": "correct-horse-battery-staple"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert login_res.status_code == 200
    login_json = login_res.get_json()
    assert login_json["ok"] is True
    assert login_json["birthday_gift"]["created"] is True
    assert login_json["birthday_gift"]["amount"] == BIRTHDAY_GIFT_POINTS
    assert login_json["birthday_gift"]["storage_quota_gift"]["created"] is True
    assert login_json["birthday_gift"]["storage_quota_gift"]["purchase"]["purchased_bytes"] == BIRTHDAY_STORAGE_GIFT_BYTES
    assert points_service.get_wallet(1)["points_balance"] == BIRTHDAY_GIFT_POINTS

    second_csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    second_login = client.post(
        "/api/login",
        json={"username": "alice", "password": "correct-horse-battery-staple"},
        headers={"X-CSRF-Token": second_csrf},
    )
    assert second_login.status_code == 200
    second_json = second_login.get_json()
    assert second_json["birthday_gift"]["created"] is False
    assert second_json["birthday_gift"]["storage_quota_gift"]["created"] is False
    assert points_service.get_wallet(1)["points_balance"] == BIRTHDAY_GIFT_POINTS

    conn = _get_main_db()
    try:
        purchases = active_storage_quota_purchases(conn, 1)
    finally:
        conn.close()
    assert len(purchases) == 1
    assert purchases[0]["item_key"] == BIRTHDAY_STORAGE_GIFT_ITEM_KEY
    assert purchases[0]["purchased_bytes"] == BIRTHDAY_STORAGE_GIFT_BYTES


def test_public_login_survives_locked_official_room_membership_sync(tmp_path):
    main_db_path = tmp_path / "database.db"
    auth_db_path = tmp_path / "auth.db"

    main_conn = sqlite3.connect(main_db_path)
    try:
        main_conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active',
                role TEXT NOT NULL DEFAULT 'user',
                nickname TEXT,
                birthdate TEXT,
                avatar_file_id INTEGER,
                blocked_until TEXT
            );
            CREATE TABLE user_passwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        password_hash = auth_service.hash_password("correct-horse-battery-staple")
        main_conn.execute(
            "INSERT INTO users (id, username, status, role, nickname, birthdate, avatar_file_id) VALUES (1, 'alice', 'active', 'user', 'Alice', NULL, NULL)"
        )
        main_conn.execute(
            "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, ?, '2026-05-09T00:00:00')",
            (password_hash,),
        )
        main_conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('server_security_epoch', '0')"
        )
        main_conn.commit()
    finally:
        main_conn.close()

    app = _build_public_auth_app(
        main_db_path,
        auth_db_path,
        ensure_membership=lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    client = app.test_client()

    csrf_res = client.get("/api/csrf-token")
    csrf_token = csrf_res.get_json()["csrf_token"]
    login_res = client.post(
        "/api/login",
        json={"username": "alice", "password": "correct-horse-battery-staple"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert login_res.status_code == 200
    assert login_res.get_json()["ok"] is True
