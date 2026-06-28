import re
import sqlite3

from flask import Flask

from services.users import auth
from services.server.database import ensure_security_support_schema


def test_require_csrf_safe_get_does_not_require_csrf(monkeypatch):
    seen = {}
    app = Flask(__name__)
    app.testing = True

    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: "alice" if token == "session-1" else None)

    def fake_verify(token, username):
        seen["token"] = token
        seen["username"] = username
        return False

    monkeypatch.setattr(auth, "verify_csrf_token", fake_verify)

    @app.route("/safe-preview")
    @auth.require_csrf_safe
    def safe_preview():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    client.set_cookie("session_token", "session-1")
    response = client.get("/safe-preview")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert seen == {}


def test_require_csrf_safe_rejects_query_token_for_unsafe_methods(monkeypatch):
    seen = {}
    app = Flask(__name__)
    app.testing = True

    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: "alice" if token == "session-1" else None)

    def fake_verify(token, username):
        seen["token"] = token
        seen["username"] = username
        return token == "query-token" and username == "alice"

    monkeypatch.setattr(auth, "verify_csrf_token", fake_verify)

    @app.route("/safe-preview", methods=["POST"])
    @auth.require_csrf_safe
    def safe_preview_post():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    client.set_cookie("session_token", "session-1")
    response = client.post("/safe-preview?csrf_token=query-token")

    assert response.status_code == 403
    assert response.get_json() == {
        "ok": False,
        "error": "csrf_invalid",
        "message": "CSRF token expired or invalid",
    }
    assert seen == {"token": "", "username": "alice"}


def test_require_csrf_rotates_authenticated_session_token_after_success(tmp_path, monkeypatch):
    db_path = tmp_path / "csrf.sqlite"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS csrf_tokens (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        return conn

    auth.configure_auth_service(get_db=get_db, get_user_by_username=lambda username: None, fernet=None)
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: "alice" if token == "session-1" else None)
    auth.store_csrf_token("stable-token", "alice")
    app = Flask(__name__)
    app.testing = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_SECURE"] = False

    @app.route("/mutate", methods=["POST"])
    @auth.require_csrf
    def mutate():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    client.set_cookie("session_token", "session-1")
    response = client.post("/mutate", headers={"X-CSRF-Token": "stable-token"})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert auth.verify_csrf_token("stable-token", "alice") is True
    set_cookie = "\n".join(response.headers.getlist("Set-Cookie"))
    match = re.search(r"csrf_token=([^;]+);", set_cookie)
    assert match
    rotated = match.group(1)
    assert rotated != "stable-token"
    assert auth.verify_csrf_token(rotated, "alice") is True


def test_require_csrf_safe_rotates_authenticated_session_token_after_success(tmp_path, monkeypatch):
    db_path = tmp_path / "csrf.sqlite"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS csrf_tokens (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        return conn

    auth.configure_auth_service(get_db=get_db, get_user_by_username=lambda username: None, fernet=None)
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: "alice" if token == "session-1" else None)
    auth.store_csrf_token("safe-token", "alice")
    app = Flask(__name__)
    app.testing = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_SECURE"] = False

    @app.route("/safe-mutate", methods=["POST"])
    @auth.require_csrf_safe
    def safe_mutate():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    client.set_cookie("session_token", "session-1")
    response = client.post("/safe-mutate", headers={"X-CSRF-Token": "safe-token"})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert auth.verify_csrf_token("safe-token", "alice") is True
    set_cookie = "\n".join(response.headers.getlist("Set-Cookie"))
    match = re.search(r"csrf_token=([^;]+);", set_cookie)
    assert match
    rotated = match.group(1)
    assert rotated != "safe-token"
    assert auth.verify_csrf_token(rotated, "alice") is True


def test_authenticated_csrf_rotation_keeps_recent_tokens_but_prunes_oldest(tmp_path):
    db_path = tmp_path / "csrf.sqlite"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS csrf_tokens (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        return conn

    auth.configure_auth_service(get_db=get_db, get_user_by_username=lambda username: None, fernet=None)
    for index in range(10):
        auth.store_csrf_token(f"token-{index}", "alice")

    auth.prune_authenticated_csrf_tokens("alice", keep=3)

    assert auth.verify_csrf_token("token-9", "alice") is True
    assert auth.verify_csrf_token("token-8", "alice") is True
    assert auth.verify_csrf_token("token-7", "alice") is True
    assert auth.verify_csrf_token("token-0", "alice") is False


def test_authenticated_recent_csrf_token_can_be_reused_without_false_security_event(tmp_path, monkeypatch):
    db_path = tmp_path / "csrf.sqlite"
    events = []

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS csrf_tokens (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        return conn

    auth.configure_auth_service(get_db=get_db, get_user_by_username=lambda username: None, fernet=None)
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: "alice" if token == "session-1" else None)
    monkeypatch.setattr(auth, "record_security_event", lambda *args, **kwargs: events.append((args, kwargs)))
    auth.store_csrf_token("tab-token", "alice")
    app = Flask(__name__)
    app.testing = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_SECURE"] = False

    @app.route("/mutate", methods=["POST"])
    @auth.require_csrf
    def mutate():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    client.set_cookie("session_token", "session-1")

    first = client.post("/mutate", headers={"X-CSRF-Token": "tab-token"})
    second = client.post("/mutate", headers={"X-CSRF-Token": "tab-token"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert not [event for event in events if event[0] and event[0][0] == "csrf_fail"]


def test_require_csrf_accepts_form_field_for_public_post(monkeypatch):
    app = Flask(__name__)
    app.testing = True

    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: None)
    monkeypatch.setattr(auth, "verify_csrf_token", lambda token, username: token == "form-token" and username == "__public__")
    monkeypatch.setattr(auth, "consume_csrf_token", lambda token, username: True)

    @app.route("/public-form", methods=["POST"])
    @auth.require_csrf
    def public_form():
        return auth.json_resp({"ok": True})

    response = app.test_client().post("/public-form", data={"csrf_token": "form-token"})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_require_csrf_consumes_public_token_but_keeps_session_tokens(tmp_path, monkeypatch):
    db_path = tmp_path / "csrf.sqlite"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS csrf_tokens (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        return conn

    auth.configure_auth_service(get_db=get_db, get_user_by_username=lambda username: None, fernet=None)
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: None)
    auth.store_csrf_token("public-token", "__public__")
    app = Flask(__name__)
    app.testing = True

    @app.route("/public-form", methods=["POST"])
    @auth.require_csrf
    def public_form():
        return auth.json_resp({"ok": True})

    response = app.test_client().post("/public-form", headers={"X-CSRF-Token": "public-token"})

    assert response.status_code == 200
    assert auth.verify_csrf_token("public-token", "__public__") is False


def test_public_signed_csrf_token_avoids_auth_db_write(tmp_path, monkeypatch):
    db_path = tmp_path / "csrf.sqlite"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS csrf_tokens (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        return conn

    auth.configure_auth_service(
        get_db=get_db,
        get_user_by_username=lambda username: None,
        fernet=None,
        csrf_secret="signed-public-csrf-test-secret",
    )
    token = auth.make_csrf_token()
    auth.store_csrf_token(token, "__public__")

    conn = get_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM csrf_tokens").fetchone()[0] == 0
    finally:
        conn.close()

    assert auth.verify_csrf_token(token, "__public__") is True
    assert auth.consume_csrf_token(token, "__public__") is True
    assert auth.verify_csrf_token(token, "__public__") is True


def test_login_accepts_public_csrf_even_when_old_session_cookie_exists(monkeypatch):
    events = []
    consumed = []
    app = Flask(__name__)
    app.testing = True

    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: "root" if token == "root-session" else None)
    monkeypatch.setattr(auth, "verify_csrf_token", lambda token, owner: token == "public-login-token" and owner == "__public__")
    monkeypatch.setattr(auth, "consume_csrf_token", lambda token, owner: consumed.append((token, owner)) or True)
    monkeypatch.setattr(auth, "record_security_event", lambda event_type, ip, **kwargs: events.append((event_type, ip, kwargs)))

    @app.route("/api/login", methods=["POST"])
    @auth.require_csrf
    def login_probe():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    client.set_cookie("session_token", "root-session")
    response = client.post(
        "/api/login",
        json={"username": "root", "password": "pw"},
        headers={"X-CSRF-Token": "public-login-token"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert consumed == [("public-login-token", "__public__")]
    assert events == []


def test_login_does_not_rotate_old_session_csrf_over_login_response(monkeypatch):
    app = Flask(__name__)
    app.testing = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_SECURE"] = False

    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: "root" if token == "root-session" else None)
    monkeypatch.setattr(auth, "verify_csrf_token", lambda token, owner: token == "root-login-token" and owner == "root")
    monkeypatch.setattr(auth, "consume_csrf_token", lambda token, owner: True)

    @app.route("/api/login", methods=["POST"])
    @auth.require_csrf
    def login_probe():
        resp = auth.json_resp({"ok": True})
        resp.set_cookie("csrf_token", "login-issued-token", path="/")
        return resp

    client = app.test_client()
    client.set_cookie("session_token", "root-session")
    response = client.post(
        "/api/login",
        json={"username": "root", "password": "pw"},
        headers={"X-CSRF-Token": "root-login-token"},
    )

    set_cookie = "\n".join(response.headers.getlist("Set-Cookie"))
    assert response.status_code == 200
    assert "csrf_token=login-issued-token;" in set_cookie
    assert set_cookie.count("csrf_token=") == 1


def test_csrf_failure_ip_uses_configured_client_ip_not_spoofed_xff(monkeypatch):
    seen = {}

    monkeypatch.setattr(auth, "record_security_event", lambda event_type, ip, **kwargs: seen.update({"event_type": event_type, "ip": ip, **kwargs}))
    auth.configure_auth_service(get_db=lambda: None, get_user_by_username=lambda username: None, fernet=None, get_client_ip=lambda: "127.0.0.1")
    app = Flask(__name__)
    app.testing = True

    with app.test_request_context("/mutate", headers={"X-Forwarded-For": "8.8.8.8"}):
        response, status = auth.csrf_invalid_response("test", "alice")

    assert status == 403
    assert response.get_json()["error"] == "csrf_invalid"
    assert seen["event_type"] == "csrf_fail"
    assert seen["ip"] == "127.0.0.1"


def test_delete_csrf_tokens_for_username_invalidates_old_session_token(tmp_path):
    db_path = tmp_path / "csrf.sqlite"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS csrf_tokens (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        return conn

    auth.configure_auth_service(get_db=get_db, get_user_by_username=lambda username: None, fernet=None)
    auth.store_csrf_token("old-token", "alice")

    assert auth.verify_csrf_token("old-token", "alice") is True
    auth.delete_csrf_tokens_for_username("alice")
    assert auth.verify_csrf_token("old-token", "alice") is False


def test_security_support_schema_creates_csrf_tokens_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE secure_audit (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE violation_appeals (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, created_at TEXT, expires_at TEXT)")
    conn.execute("CREATE TABLE security_events (id INTEGER PRIMARY KEY, event_type TEXT, ip_address TEXT, detail TEXT, created_at TEXT)")

    ensure_security_support_schema(
        conn,
        ensure_member_level_rules_schema=lambda db: None,
        ensure_moderation_proposals_schema=lambda db: None,
        ensure_governance_records_schema=lambda db: None,
        ensure_snapshot_schema=lambda db: None,
        ensure_upload_security_schema=lambda db: None,
        ensure_integrity_schema=lambda db: None,
        ensure_account_recovery_schema=lambda db: None,
    )

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(csrf_tokens)").fetchall()}
    assert cols >= {"id", "token_hash", "username", "expires_at"}


# ── Server Mode v2 §Mode Behavior Matrix footnote 1 ────────────────────────
# CSRF is on in every mode EXCEPT `superweak` (the deliberate weakest web
# mode for red-team / fuzz / pentest). Tests below lock both halves of that
# invariant: enforced-by-default + bypassed-only-in-superweak.
# See SERVER_MODE_V2_PROFILE_MATRIX.md footnote 1.


def test_require_csrf_is_enforced_when_no_token_present(monkeypatch):
    """csrf default-on: missing CSRF token must be rejected."""
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: None)
    monkeypatch.setattr(auth, "verify_csrf_token", lambda token, owner: False)

    app = Flask(__name__)
    app.testing = True

    @app.route("/probe", methods=["POST"])
    @auth.require_csrf
    def probe():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    response = client.post("/probe", json={"username": ""})
    assert response.status_code == 403
    assert response.get_json().get("error") == "csrf_invalid"


def test_require_csrf_safe_rejects_authenticated_request_without_csrf(monkeypatch):
    """csrf default-on: authenticated POST without verified CSRF must 403."""
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: "alice" if token == "session-1" else None)
    monkeypatch.setattr(auth, "verify_csrf_token", lambda token, owner: False)

    app = Flask(__name__)
    app.testing = True

    @app.route("/safe-probe", methods=["POST"])
    @auth.require_csrf_safe
    def safe_probe():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    client.set_cookie("session_token", "session-1")
    response = client.post("/safe-probe", json={})
    assert response.status_code == 403
    assert response.get_json().get("error") == "csrf_invalid"


def test_require_csrf_safe_still_requires_login_first(monkeypatch):
    """No session → 401 before CSRF check is even attempted."""
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: None)

    app = Flask(__name__)
    app.testing = True

    @app.route("/safe-probe", methods=["POST"])
    @auth.require_csrf_safe
    def safe_probe():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    response = client.post("/safe-probe", json={})
    assert response.status_code == 401


def test_require_csrf_bypassed_in_superweak(monkeypatch):
    """superweak mode: CSRF check is skipped for require_csrf-decorated POSTs.

    This is the only mode that bypasses CSRF; all 6 other modes
    (test / internal_test / dev_ready / production / maintenance /
    incident_lockdown) keep CSRF on.
    """
    events = []
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: None)
    # If the bypass leaks, this would force a 403 — so we keep verify
    # returning False and assert we never reach it.
    monkeypatch.setattr(auth, "verify_csrf_token", lambda token, owner: False)
    monkeypatch.setattr(auth, "record_security_event", lambda event_type, ip, **kwargs: events.append({"event_type": event_type, "ip": ip, **kwargs}))
    auth.configure_auth_service(
        get_db=lambda: None,
        get_user_by_username=lambda username: None,
        fernet=None,
        get_runtime_server_mode=lambda: "superweak",
    )

    app = Flask(__name__)
    app.testing = True

    @app.route("/probe", methods=["POST"])
    @auth.require_csrf
    def probe():
        return auth.json_resp({"ok": True})

    response = app.test_client().post("/probe", json={})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert any(ev["event_type"] == "csrf_skipped_superweak" and "require_csrf" in ev.get("detail", "") for ev in events), events


def test_require_csrf_safe_bypassed_in_superweak(monkeypatch):
    """superweak mode: CSRF check is skipped for require_csrf_safe-decorated POSTs.

    Login is still required (the bypass is only on the CSRF check, not on
    authentication).
    """
    events = []
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: "alice" if token == "session-1" else None)
    monkeypatch.setattr(auth, "verify_csrf_token", lambda token, owner: False)
    monkeypatch.setattr(auth, "record_security_event", lambda event_type, ip, **kwargs: events.append({"event_type": event_type, "ip": ip, **kwargs}))
    auth.configure_auth_service(
        get_db=lambda: None,
        get_user_by_username=lambda username: None,
        fernet=None,
        get_runtime_server_mode=lambda: "superweak",
    )

    app = Flask(__name__)
    app.testing = True

    @app.route("/safe-probe", methods=["POST"])
    @auth.require_csrf_safe
    def safe_probe():
        return auth.json_resp({"ok": True})

    client = app.test_client()

    # Without session → 401 (auth still required).
    no_session = client.post("/safe-probe", json={})
    assert no_session.status_code == 401

    # With session but no CSRF → 200 in superweak.
    client.set_cookie("session_token", "session-1")
    response = client.post("/safe-probe", json={})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert any(ev["event_type"] == "csrf_skipped_superweak" and "require_csrf_safe" in ev.get("detail", "") for ev in events), events


def test_csrf_bypass_strict_equality_only(monkeypatch):
    """superweak bypass is strict equality only — no other mode bypasses.

    Modes that contain 'super' or 'weak' as substrings, or modes whose
    name is similar but not exactly 'superweak', must not trigger bypass.
    """
    monkeypatch.setattr(auth, "db_get_user_from_token", lambda token: None)
    monkeypatch.setattr(auth, "verify_csrf_token", lambda token, owner: False)
    monkeypatch.setattr(auth, "record_security_event", lambda event_type, ip, **kwargs: None)

    app = Flask(__name__)
    app.testing = True

    @app.route("/probe", methods=["POST"])
    @auth.require_csrf
    def probe():
        return auth.json_resp({"ok": True})

    client = app.test_client()
    for mode in ("production", "internal_test", "test", "dev_ready", "maintenance", "incident_lockdown", "", None, "Superweak", "superweak ", "super_weak"):
        auth.configure_auth_service(
            get_db=lambda: None,
            get_user_by_username=lambda username: None,
            fernet=None,
            get_runtime_server_mode=lambda mode=mode: mode,
        )
        response = client.post("/probe", json={})
        assert response.status_code == 403, f"mode={mode!r} unexpectedly bypassed CSRF"
