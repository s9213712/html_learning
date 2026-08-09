from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest
from flask import Flask, jsonify, make_response

from routes.public import register_public_routes
from services.platform.db_mode_triggers import register_app_mode_function
from services.points_chain import PointsLedgerService, SIGNUP_BONUS_POINTS
from services.server.finance_database import get_finance_db


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _passthrough(fn):
    return fn


def _build_app(db_path, *, points_service=None, settings=None):
    app = Flask(__name__)
    app.testing = True

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        register_app_mode_function(conn, mode_reader=lambda: "production")
        return conn

    register_public_routes(app, {
        "CSRF_TOKEN_TTL": 3600,
        "PUBLIC_DIR": ".",
        "ROLE_LABEL": {},
        "SERVER_APP_NAME": "hackme_web",
        "SERVER_RELEASE_ID": "test",
        "SERVER_STARTED_AT": "2026-01-01T00:00:00",
        "SERVER_VERSION": "test",
        "SESSION_COOKIE_SAMESITE": "Strict",
        "SESSION_COOKIE_SECURE": False,
        "SESSION_TTL": 3600,
        "audit": lambda *args, **kwargs: None,
        "db_delete_session": lambda *args, **kwargs: None,
        "db_get_user_from_token": lambda *args, **kwargs: None,
        "db_save_session": lambda *args, **kwargs: None,
        "decrypt_field": lambda value: value or "",
        "encrypt_field": lambda value: value,
        "ensure_user_official_room_membership": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: None,
        "get_db": get_db,
        "get_feature_settings": lambda: {},
        "get_member_level_rule": lambda conn, level: {},
        "get_system_settings": lambda: {
            "allow_register": True,
            "captcha_mode": "none",
            "max_login_failures": 5,
            "block_duration_minutes": 10,
            **(settings or {}),
        },
        "get_ua": lambda: "test-agent",
        "hash_password": lambda value: value,
        "is_feature_enabled": lambda name: False,
        "is_ip_blocked": lambda ip: False,
        "is_rate_limited": lambda *args, **kwargs: (False, {"limit": 10}),
        "json_resp": _json_resp,
        "make_csrf_token": lambda: "csrf",
        "make_token": lambda username: "session-token",
        "normalize_text": lambda value: str(value or "").strip(),
        "parse_birthdate": lambda value: value if value else "",
        "points_service": points_service,
        "record_login_failure": lambda *args, **kwargs: None,
        "record_security_event": lambda *args, **kwargs: None,
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "score_password_strength": lambda value: {"score": 4},
        "store_csrf_token": lambda *args, **kwargs: None,
        "timing_delay": lambda: None,
        "validate_id_number": lambda value: True,
        "validate_password": lambda value: (len(value) >= 8, "密碼太短"),
        "enforce_password_strength": lambda value, min_score=3: (True, "", {"score": 4}),
        "validate_phone": lambda value: True,
        "verify_csrf_double_submit": lambda token: True,
        "verify_password": lambda stored, provided: stored == provided,
    })
    return app


def _init_register_tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT,
                nickname TEXT,
                real_name TEXT,
                birthdate TEXT,
                id_number TEXT,
                phone TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                role TEXT NOT NULL DEFAULT 'user',
                member_level TEXT,
                base_level TEXT,
                effective_level TEXT,
                password_strength_score INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE user_passwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_register_validation_returns_field_for_username(tmp_path):
    client = _build_app(tmp_path / "register.db").test_client()

    response = client.post(
        "/api/register",
        json={"username": "ab", "password": "GoodPass1!", "password_confirm": "GoodPass1!", "nickname": "Nick"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["field"] == "username"


def test_register_can_disable_password_strength_policy(tmp_path):
    db_path = tmp_path / "register.db"
    _init_register_tables(db_path)
    client = _build_app(db_path, settings={"password_strength_policy_enabled": False}).test_client()

    response = client.post(
        "/api/register",
        json={"username": "shortpw", "password": "x", "password_confirm": "x", "nickname": "Short"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_concurrent_registration_migrates_public_account_columns_once(tmp_path):
    """Legacy-schema upgrades must not race under a simultaneous login burst."""

    db_path = tmp_path / "concurrent_register.db"
    _init_register_tables(db_path)
    app = _build_app(db_path, settings={"password_strength_policy_enabled": False})
    start = threading.Barrier(8)

    def register(index):
        with app.test_client() as client:
            start.wait(timeout=10)
            return client.post(
                "/api/register",
                json={
                    "username": f"burst{index:02d}",
                    "password": "x",
                    "password_confirm": "x",
                    "nickname": f"Burst {index}",
                },
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(register, range(8)))

    assert [response.status_code for response in responses] == [200] * 8
    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        assert "signup_bonus_deferred" in columns
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 8
    finally:
        conn.close()


def test_register_awards_signup_bonus_to_official_hot_wallet(tmp_path):
    db_path = tmp_path / "register.db"
    _init_register_tables(db_path)

    def points_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        register_app_mode_function(conn, mode_reader=lambda: "production")
        return conn

    points_service = PointsLedgerService(
        get_db=points_db,
        chain_secret="test-secret",
        backup_dir=tmp_path / "points_chain_backups",
        mode_reader=lambda: "production",
    )
    client = _build_app(db_path, points_service=points_service).test_client()

    response = client.post(
        "/api/register",
        json={
            "username": "alice123",
            "password": "GoodPass1!",
            "password_confirm": "GoodPass1!",
            "nickname": "Alice",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["wallet_onboarding_required"] is False
    assert payload["signup_bonus_deferred"] is False
    assert payload["official_hot_wallet_address"].startswith("pc0")
    assert payload["signup_bonus"]["created"] is True
    assert payload["signup_bonus"]["wallet_address"] == payload["official_hot_wallet_address"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        user = conn.execute("SELECT username, status FROM users WHERE username='alice123'").fetchone()
        ledger = conn.execute(
            """
            SELECT * FROM points_ledger
            WHERE action_type='new_user_signup_bonus'
              AND user_id=(SELECT id FROM users WHERE username='alice123')
            """
        ).fetchone()
    finally:
        conn.close()
    assert tuple(user) == ("alice123", "pending")
    assert ledger is not None
    assert int(ledger["amount"]) == SIGNUP_BONUS_POINTS
    assert points_service.get_wallet(ledger["user_id"])["points_balance"] == SIGNUP_BONUS_POINTS


def test_register_writes_signup_bonus_to_split_finance_db(tmp_path):
    db_path = tmp_path / "register.db"
    finance_path = tmp_path / "finance.db"
    _init_register_tables(db_path)

    def points_db():
        return get_finance_db(finance_path, core_db_path=db_path, register_app_mode=lambda conn: register_app_mode_function(conn, mode_reader=lambda: "production"))

    points_service = PointsLedgerService(
        get_db=points_db,
        chain_secret="test-secret",
        backup_dir=tmp_path / "points_chain_backups",
        mode_reader=lambda: "production",
    )
    client = _build_app(db_path, points_service=points_service).test_client()

    response = client.post(
        "/api/register",
        json={
            "username": "alice123",
            "password": "GoodPass1!",
            "password_confirm": "GoodPass1!",
            "nickname": "Alice",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["signup_bonus"]["created"] is True
    core = sqlite3.connect(db_path)
    finance = sqlite3.connect(finance_path)
    try:
        assert core.execute("SELECT username FROM users WHERE username='alice123'").fetchone()[0] == "alice123"
        assert core.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='points_ledger'").fetchone() is None
        assert finance.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] == 1
    finally:
        core.close()
        finance.close()


def test_register_marks_signup_bonus_deferred_when_points_init_fails(tmp_path):
    db_path = tmp_path / "register.db"
    _init_register_tables(db_path)

    class FailingPointsService:
        chain_secret = "test-secret"

        def get_db(self):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn

        def ensure_schema(self, conn):
            raise RuntimeError("points unavailable")

    client = _build_app(db_path, points_service=FailingPointsService()).test_client()

    response = client.post(
        "/api/register",
        json={
            "username": "deferred_user",
            "password": "GoodPass1!",
            "password_confirm": "GoodPass1!",
            "nickname": "Deferred",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["signup_bonus_deferred"] is True
    assert payload["signup_bonus"]["created"] is False
    assert payload["signup_bonus"]["deferred_reason"] == "points_initialization_failed"

    conn = sqlite3.connect(db_path)
    try:
        user = conn.execute(
            "SELECT username, status, signup_bonus_deferred FROM users WHERE username='deferred_user'"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(user) == ("deferred_user", "pending", 1)


def test_login_reissues_deferred_signup_bonus_once_after_approval(tmp_path):
    db_path = tmp_path / "register.db"
    _init_register_tables(db_path)

    class FailingPointsService:
        chain_secret = "test-secret"

        def get_db(self):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn

        def ensure_schema(self, conn):
            raise RuntimeError("points unavailable")

    register_client = _build_app(db_path, points_service=FailingPointsService()).test_client()
    register_response = register_client.post(
        "/api/register",
        json={
            "username": "deferred_user",
            "password": "GoodPass1!",
            "password_confirm": "GoodPass1!",
            "nickname": "Deferred",
        },
    )
    assert register_response.status_code == 200
    assert register_response.get_json()["signup_bonus_deferred"] is True

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE users SET status='active' WHERE username='deferred_user'")
        conn.commit()
    finally:
        conn.close()

    def points_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        register_app_mode_function(conn, mode_reader=lambda: "production")
        return conn

    points_service = PointsLedgerService(
        get_db=points_db,
        chain_secret="test-secret",
        backup_dir=tmp_path / "points_chain_backups",
        mode_reader=lambda: "production",
    )
    login_client = _build_app(db_path, points_service=points_service).test_client()

    first_login = login_client.post(
        "/api/login",
        json={"username": "deferred_user", "password": "GoodPass1!"},
    )
    assert first_login.status_code == 200
    first_payload = first_login.get_json()
    assert first_payload["ok"] is True
    assert first_payload["signup_bonus"]["created"] is True
    assert points_service.get_wallet(1)["points_balance"] == SIGNUP_BONUS_POINTS

    conn = sqlite3.connect(db_path)
    try:
        deferred = conn.execute(
            "SELECT signup_bonus_deferred FROM users WHERE username='deferred_user'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert deferred == 0

    second_login = login_client.post(
        "/api/login",
        json={"username": "deferred_user", "password": "GoodPass1!"},
    )
    assert second_login.status_code == 200
    assert second_login.get_json()["signup_bonus"] is None
    assert points_service.get_wallet(1)["points_balance"] == SIGNUP_BONUS_POINTS


def test_register_validation_returns_field_for_password_confirmation(tmp_path):
    client = _build_app(tmp_path / "register.db").test_client()

    response = client.post(
        "/api/register",
        json={"username": "alice123", "password": "GoodPass1!", "password_confirm": "Mismatch1!", "nickname": "Nick"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["field"] == "password_confirm"


@pytest.mark.parametrize("username", ["ROOT", "Root", "ADMIN", "Admin", "TEST", "Test", "r0ot", "te5t"])
def test_register_blocks_reserved_username_case_and_simple_confusables(tmp_path, username):
    client = _build_app(tmp_path / "register.db").test_client()

    response = client.post(
        "/api/register",
        json={"username": username, "password": "GoodPass1!", "password_confirm": "GoodPass1!", "nickname": "Nick"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["field"] == "username"
    assert "保留" in payload["msg"]
