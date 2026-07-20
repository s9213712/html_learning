import hashlib
import sqlite3

from flask import Flask, jsonify, make_response

from routes.public import register_public_routes
from services.security.access_controls import hash_internal_test_token, maintenance_bypass_expires_at


def _build_app(
    db_path,
    settings,
    ip_box=None,
    event_log=None,
    db_delete_session=None,
    db_get_user_from_token=None,
    rate_limiter=None,
):
    app = Flask(__name__)
    app.testing = True

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def json_resp(payload, status=200):
        return make_response(jsonify(payload), status)

    register_public_routes(app, {
        "CSRF_TOKEN_TTL": 3600,
        "PUBLIC_DIR": ".",
        "ROLE_LABEL": {},
        "SERVER_APP_NAME": "hackme_web",
        "SERVER_RELEASE_ID": "test-release",
        "SERVER_STARTED_AT": "2026-01-01T00:00:00",
        "SERVER_VERSION": "test",
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": False,
        "SESSION_TTL": 3600,
        "audit": lambda *args, **kwargs: None,
        "db_delete_session": db_delete_session or (lambda *args, **kwargs: None),
        "db_get_user_from_token": db_get_user_from_token or (lambda *args, **kwargs: None),
        "db_save_session": lambda *args, **kwargs: None,
        "decrypt_field": lambda value: value,
        "encrypt_field": lambda value: value,
        "ensure_user_official_room_membership": lambda *args, **kwargs: None,
        "get_client_ip": lambda: (ip_box or {"ip": "127.0.0.1"})["ip"],
        "get_cached_system_setting": lambda key, default=None: settings.get(key, default),
        "get_current_user_ctx": lambda: None,
        "get_db": get_db,
        "get_feature_settings": lambda: {},
        "get_system_settings": lambda: settings,
        "get_ua": lambda: "test-agent",
        "hash_password": lambda value: value,
        "is_feature_enabled": lambda key: key == "feature_account_security_enabled",
        "is_ip_blocked": lambda ip: False,
        "is_rate_limited": rate_limiter or (lambda *args, **kwargs: (False, {"limit": 30})),
        "json_resp": json_resp,
        "make_csrf_token": lambda: "csrf",
        "make_token": lambda username: f"token-{username}",
        "normalize_text": lambda value: value.strip() if isinstance(value, str) else "",
        "parse_birthdate": lambda value: value,
        "record_login_failure": lambda *args, **kwargs: None,
        "record_security_event": lambda *args, **kwargs: (event_log.append((args, kwargs)) if event_log is not None else None),
        "require_csrf": lambda fn: fn,
        "require_csrf_safe": lambda fn: fn,
        "score_password_strength": lambda value: {"score": 4, "missing": []},
        "role_rank": lambda role: 0,
        "store_csrf_token": lambda *args, **kwargs: None,
        "timing_delay": lambda: None,
        "validate_id_number": lambda value: True,
        "validate_password": lambda value: (True, "OK"),
        "enforce_password_strength": lambda value, min_score=3: (True, "OK", {"score": 4}),
        "validate_phone": lambda value: True,
        "verify_csrf_double_submit": lambda value: True,
        "verify_password": lambda stored, provided: stored == provided,
    })
    return app


def test_idle_timeout_logout_revokes_session_without_csrf(tmp_path):
    deleted_tokens = []
    client = _build_app(
        tmp_path / "timeout.db",
        {},
        db_delete_session=lambda token, **kwargs: deleted_tokens.append(token),
        db_get_user_from_token=lambda token: "alice" if token == "session-1" else None,
    ).test_client()
    client.set_cookie("session_token", "session-1")
    client.set_cookie("csrf_token", "stale-csrf")

    response = client.post(
        "/api/session/idle-timeout",
        headers={"X-Idle-Timeout-Logout": "1"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert deleted_tokens == ["session-1"]
    set_cookie = "\n".join(response.headers.getlist("Set-Cookie"))
    assert "session_token=;" in set_cookie
    assert "csrf_token=;" in set_cookie


def test_idle_timeout_logout_requires_idle_confirmation_header(tmp_path):
    deleted_tokens = []
    client = _build_app(
        tmp_path / "timeout.db",
        {},
        db_delete_session=lambda token, **kwargs: deleted_tokens.append(token),
        db_get_user_from_token=lambda token: "alice",
    ).test_client()
    client.set_cookie("session_token", "session-1")

    response = client.post("/api/session/idle-timeout")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert deleted_tokens == []


def test_public_version_endpoints_expose_release_id(tmp_path):
    client = _build_app(tmp_path / "release.db", {"server_timezone": "Asia/Taipei"}).test_client()

    site_config = client.get("/api/site-config")
    version = client.get("/api/version")

    assert site_config.status_code == 200
    assert site_config.get_json()["server_meta"]["release_id"] == "test-release"
    assert site_config.get_json()["server_meta"]["version"] == "test"
    assert site_config.get_json()["server_meta"]["server_time"]["timezone"] == "Asia/Taipei"
    assert site_config.get_json()["site_config"]["server_mode"] == "test"
    assert version.status_code == 200
    assert version.get_json()["release_id"] == "test-release"
    assert version.get_json()["server_time"]["timezone"] == "Asia/Taipei"
    assert "server_time_unix_ms" in version.get_json()["server_time"]


def _seed_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            role TEXT NOT NULL,
            blocked_until TEXT,
            locked_until TEXT,
            failed_login_count INTEGER NOT NULL DEFAULT 0,
            last_login_at TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            is_default_password INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ip_address TEXT,
            user_agent TEXT,
            success INTEGER NOT NULL,
            attempted_at TEXT NOT NULL
        );
        CREATE TABLE login_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ip_hash TEXT NOT NULL,
            country TEXT,
            city TEXT,
            login_at TEXT NOT NULL,
            is_suspicious INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            ip_address TEXT,
            user_agent TEXT,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00'
        );
        """
    )
    conn.execute(
        "INSERT INTO users (id, username, status, role) VALUES (1, 'alice', 'active', 'user')"
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, 'correct', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()


def test_account_security_locks_user_after_repeated_bad_passwords(tmp_path):
    db_path = tmp_path / "lockout.db"
    _seed_db(db_path)
    client = _build_app(
        str(db_path),
        {"max_login_failures": 2, "block_duration_minutes": 10},
    ).test_client()

    first = client.post("/api/login", json={"username": "alice", "password": "bad"})
    second = client.post("/api/login", json={"username": "alice", "password": "bad"})
    locked = client.post("/api/login", json={"username": "alice", "password": "correct"})

    assert first.status_code == 401
    assert second.status_code == 401
    assert locked.status_code == 401

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT failed_login_count, locked_until FROM users WHERE username='alice'"
    ).fetchone()
    conn.close()

    assert row[0] == 2
    assert row[1]


def test_login_rate_limits_shared_source_and_principal_independently(tmp_path):
    db_path = tmp_path / "login-rate-scope.db"
    _seed_db(db_path)
    calls = []

    def limiter(key, max_req, window_sec):
        calls.append((key, max_req, window_sec))
        if key[1] == "login_principal":
            return True, {"limit": max_req, "retry_after_seconds": 7}
        return False, {"limit": max_req, "retry_after_seconds": 0}

    client = _build_app(
        str(db_path),
        {"max_login_failures": 3, "block_duration_minutes": 10},
        ip_box={"ip": "10.0.0.9"},
        rate_limiter=limiter,
    ).test_client()

    response = client.post("/api/login", json={"username": "alice", "password": "correct"})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert response.get_json() == {
        "ok": False,
        "msg": "登入請求太頻繁，請稍後再試",
        "error": "login_rate_limited",
        "scope": "account",
        "retry_after_seconds": 7,
    }
    assert calls == [
        (("10.0.0.9", "login_ip"), 300, 60),
        (("10.0.0.9", "login_principal", 1), 30, 60),
    ]


def test_login_source_rate_limit_is_structured_and_stops_before_lookup(tmp_path):
    db_path = tmp_path / "login-source-rate.db"
    _seed_db(db_path)

    def limiter(key, max_req, window_sec):
        assert key == ("10.0.0.8", "login_ip")
        assert (max_req, window_sec) == (300, 60)
        return True, {"limit": max_req, "retry_after_seconds": 11}

    client = _build_app(
        str(db_path),
        {"max_login_failures": 3, "block_duration_minutes": 10},
        ip_box={"ip": "10.0.0.8"},
        rate_limiter=limiter,
    ).test_client()

    response = client.post("/api/login", json={"username": "alice", "password": "correct"})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "11"
    assert response.get_json()["error"] == "login_rate_limited"
    assert response.get_json()["scope"] == "source"


def test_login_cookie_lifetime_uses_session_ttl_setting(tmp_path):
    db_path = tmp_path / "login-cookie-ttl.db"
    _seed_db(db_path)
    client = _build_app(
        str(db_path),
        {
            "max_login_failures": 3,
            "block_duration_minutes": 10,
            "session_ttl_hours": 168,
        },
    ).test_client()

    response = client.post("/api/login", json={"username": "alice", "password": "correct"})

    assert response.status_code == 200
    set_cookie = "\n".join(response.headers.getlist("Set-Cookie"))
    assert "session_token=token-alice" in set_cookie
    assert "csrf_token=csrf" in set_cookie
    assert "Max-Age=604800" in set_cookie


def test_internal_test_mode_requires_root_issued_token_for_the_bound_non_root_login(tmp_path):
    db_path = tmp_path / "internal-test.db"
    _seed_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE server_modes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_mode TEXT NOT NULL,
            previous_mode TEXT,
            active_snapshot_id TEXT,
            mode_changed_by INTEGER,
            mode_changed_at TEXT,
            notes TEXT
        );
        INSERT INTO server_modes (id, current_mode) VALUES (1, 'internal_test');
        INSERT INTO users (id, username, status, role) VALUES (2, 'root', 'active', 'super_admin');
        INSERT INTO users (id, username, status, role) VALUES (3, 'bob', 'active', 'user');
        INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (2, 'rootpass', '2026-01-01T00:00:00');
        INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (3, 'correct', '2026-01-01T00:00:00');
        """
    )
    conn.commit()
    conn.close()
    token = "issued-by-root"
    client = _build_app(
        str(db_path),
        {
            "max_login_failures": 3,
            "block_duration_minutes": 10,
            "internal_test_login_token_hash": hash_internal_test_token(token),
            "internal_test_login_token_expires_at": maintenance_bypass_expires_at(30),
            "internal_test_login_token_user_id": 1,
        },
    ).test_client()

    denied = client.post("/api/login", json={"username": "alice", "password": "correct"})
    bad_token = client.post("/api/login", json={"username": "alice", "password": "correct", "internal_test_token": "bad"})
    allowed = client.post("/api/login", json={"username": "alice", "password": "correct", "internal_test_token": token})
    wrong_user = client.post("/api/login", json={"username": "bob", "password": "correct", "internal_test_token": token})
    root_allowed = client.post("/api/login", json={"username": "root", "password": "rootpass"})

    assert denied.status_code == 403
    assert "內測模式" in denied.get_json()["msg"]
    assert bad_token.status_code == 403
    assert allowed.status_code == 200
    assert wrong_user.status_code == 403
    assert root_allowed.status_code == 200


def test_internal_test_mode_accepts_scoped_tester_token_table(tmp_path):
    db_path = tmp_path / "internal-test-v2.db"
    _seed_db(db_path)
    token = "tester-token-v2"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE server_modes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_mode TEXT NOT NULL
        );
        CREATE TABLE tester_tokens (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            tester_user_id INTEGER,
            allowed_features_json TEXT NOT NULL DEFAULT '[]',
            allowed_routes_json TEXT NOT NULL DEFAULT '[]',
            expires_at TEXT NOT NULL,
            max_requests_per_minute INTEGER NOT NULL DEFAULT 60,
            can_modify_own_role INTEGER NOT NULL DEFAULT 0,
            can_modify_own_points INTEGER NOT NULL DEFAULT 0,
            can_run_security_tests INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        );
        INSERT INTO server_modes (id, current_mode) VALUES (1, 'internal_test');
        """
    )
    conn.execute(
        "INSERT INTO tester_tokens (id, token_hash, tester_user_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
        ("tester_token_1", hashlib.sha256(token.encode("utf-8")).hexdigest(), 1, "2999-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    client = _build_app(
        str(db_path),
        {"max_login_failures": 3, "block_duration_minutes": 10},
    ).test_client()

    denied = client.post("/api/login", json={"username": "alice", "password": "correct", "internal_test_token": "bad"})
    allowed = client.post("/api/login", json={"username": "alice", "password": "correct", "internal_test_token": token})

    assert denied.status_code == 403
    assert allowed.status_code == 200


def test_production_mode_rejects_same_ip_using_different_account(tmp_path):
    db_path = tmp_path / "production-ip-conflict.db"
    _seed_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE server_modes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_mode TEXT NOT NULL,
            previous_mode TEXT,
            active_snapshot_id TEXT,
            mode_changed_by INTEGER,
            mode_changed_at TEXT,
            notes TEXT
        );
        INSERT INTO server_modes (id, current_mode) VALUES (1, 'production');
        INSERT INTO users (id, username, status, role) VALUES (2, 'bob', 'active', 'user');
        INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (2, 'correct', '2026-01-01T00:00:00');
        INSERT INTO sessions (user_id, token_hash, ip_address, user_agent, expires_at, is_revoked)
        VALUES (2, 'bob-session', '10.0.0.1', 'test-agent', '2999-01-01T00:00:00', 0);
        """
    )
    conn.commit()
    conn.close()
    client = _build_app(
        str(db_path),
        {"max_login_failures": 3, "block_duration_minutes": 10, "production_single_ip_account_lock_enabled": True},
        ip_box={"ip": "10.0.0.1"},
    ).test_client()

    denied = client.post("/api/login", json={"username": "alice", "password": "correct"})

    assert denied.status_code == 403
    assert "同一 IP" in denied.get_json()["msg"]


def test_production_mode_session_conflict_policy_is_disabled_by_default(tmp_path):
    db_path = tmp_path / "production-conflict-disabled.db"
    _seed_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE server_modes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_mode TEXT NOT NULL
        );
        INSERT INTO server_modes (id, current_mode) VALUES (1, 'production');
        INSERT INTO users (id, username, status, role) VALUES (2, 'bob', 'active', 'user');
        INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (2, 'correct', '2026-01-01T00:00:00');
        INSERT INTO sessions (user_id, token_hash, ip_address, user_agent, expires_at, is_revoked)
        VALUES (2, 'bob-session', '10.0.0.1', 'test-agent', '2999-01-01T00:00:00', 0);
        """
    )
    conn.commit()
    conn.close()
    client = _build_app(
        str(db_path),
        {"max_login_failures": 3, "block_duration_minutes": 10},
        ip_box={"ip": "10.0.0.1"},
    ).test_client()

    allowed = client.post("/api/login", json={"username": "alice", "password": "correct"})

    assert allowed.status_code == 200


def test_production_mode_rejects_same_account_from_different_ip(tmp_path):
    db_path = tmp_path / "production-account-conflict.db"
    _seed_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE server_modes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_mode TEXT NOT NULL,
            previous_mode TEXT,
            active_snapshot_id TEXT,
            mode_changed_by INTEGER,
            mode_changed_at TEXT,
            notes TEXT
        );
        INSERT INTO server_modes (id, current_mode) VALUES (1, 'production');
        INSERT INTO sessions (user_id, token_hash, ip_address, user_agent, expires_at, is_revoked)
        VALUES (1, 'alice-session', '10.0.0.9', 'test-agent', '2999-01-01T00:00:00', 0);
        """
    )
    conn.commit()
    conn.close()
    client = _build_app(
        str(db_path),
        {"max_login_failures": 3, "block_duration_minutes": 10, "production_single_account_ip_lock_enabled": True},
        ip_box={"ip": "10.0.0.1"},
    ).test_client()

    denied = client.post("/api/login", json={"username": "alice", "password": "correct"})

    assert denied.status_code == 403
    assert "不同 IP" in denied.get_json()["msg"]


def test_production_mode_allows_same_account_same_ip(tmp_path):
    db_path = tmp_path / "production-same-account-ip.db"
    _seed_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE server_modes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_mode TEXT NOT NULL,
            previous_mode TEXT,
            active_snapshot_id TEXT,
            mode_changed_by INTEGER,
            mode_changed_at TEXT,
            notes TEXT
        );
        INSERT INTO server_modes (id, current_mode) VALUES (1, 'production');
        INSERT INTO sessions (user_id, token_hash, ip_address, user_agent, expires_at, is_revoked)
        VALUES (1, 'alice-session', '10.0.0.1', 'test-agent', '2999-01-01T00:00:00', 0);
        """
    )
    conn.commit()
    conn.close()
    client = _build_app(
        str(db_path),
        {"max_login_failures": 3, "block_duration_minutes": 10},
        ip_box={"ip": "10.0.0.1"},
    ).test_client()

    allowed = client.post("/api/login", json={"username": "alice", "password": "correct"})

    assert allowed.status_code == 200


def test_successful_login_records_suspicious_new_location(tmp_path):
    db_path = tmp_path / "locations.db"
    _seed_db(db_path)
    ip_box = {"ip": "10.0.0.1"}
    events = []
    client = _build_app(
        str(db_path),
        {"max_login_failures": 2, "block_duration_minutes": 10},
        ip_box=ip_box,
        event_log=events,
    ).test_client()

    first = client.post("/api/login", json={"username": "alice", "password": "correct"})
    ip_box["ip"] = "10.0.0.2"
    second = client.post("/api/login", json={"username": "alice", "password": "correct"})

    assert first.status_code == 200
    assert second.status_code == 200

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT is_suspicious FROM login_locations ORDER BY id"
    ).fetchall()
    conn.close()

    assert rows == [(0,), (1,)]
    assert events
    assert events[-1][0][0] == "login_location_suspicious"
