import json
import sqlite3

from flask import Flask, jsonify, make_response

from routes.ai_agent import register_ai_agent_routes


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _build_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS comfyui_generation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            owner_user_id INTEGER NOT NULL,
            owner_username TEXT,
            status TEXT,
            error TEXT,
            progress_json TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS job_center_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_uuid TEXT NOT NULL,
            owner_user_id INTEGER NOT NULL,
            owner_username TEXT,
            status TEXT,
            error_code TEXT,
            error_message TEXT,
            stage TEXT,
            stage_detail TEXT,
            progress_percent INTEGER,
            source_module TEXT,
            updated_at TEXT,
            metadata_json TEXT,
            result_json TEXT
        );

        CREATE TABLE IF NOT EXISTS security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            ip_address TEXT,
            target_user TEXT,
            detail TEXT,
            created_at TEXT
        );
        """
    )
    conn.execute("INSERT OR IGNORE INTO users (id, username, role, status, created_at) VALUES (1, 'root', 'super_admin', 'active', '2026-01-01T00:00:00')")
    conn.execute("INSERT OR IGNORE INTO users (id, username, role, status, created_at) VALUES (2, 'userA', 'user', 'active', '2026-01-01T00:00:01')")
    conn.execute("INSERT OR IGNORE INTO users (id, username, role, status, created_at) VALUES (3, 'managerA', 'manager', 'active', '2026-01-01T00:00:02')")
    conn.execute(
        "INSERT OR IGNORE INTO comfyui_generation_jobs (job_id, owner_user_id, owner_username, status, error, progress_json, created_at, updated_at)\n"
        "VALUES ('job-comfy-1', 2, 'userA', 'failed', 'timeout', ?, '2026-01-01T00:01:00', '2026-01-01T00:01:10')",
        (json.dumps({"percent": 12}),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO job_center_jobs (job_uuid, owner_user_id, owner_username, status, error_code, error_message, stage, stage_detail, progress_percent, source_module, updated_at, metadata_json, result_json)\n"
        "VALUES ('dl-1', 2, 'userA', 'failed', 'timeout', '下載逾時', 'retry', '重試中', 11, 'cloud_drive_remote_download', '2026-01-01T00:02:00', ?, '{}')",
        (json.dumps({"filename": "foo.bin", "loaded_bytes": 64, "total_bytes": 128, "speed_bytes_per_sec": 1}),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO security_events (event_type, ip_address, target_user, detail, created_at)\n"
        "VALUES ('attack', '203.0.113.10', 'userA', '多次嘗試', '2026-01-01T00:03:00')"
    )
    conn.commit()
    conn.close()


def _build_app(db_path, actor, *, settings=None):
    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    app = Flask(__name__)
    app.testing = True
    register_ai_agent_routes(
        app,
        {
            "get_current_user_ctx": lambda: actor,
            "get_system_settings": lambda: dict({"module_ai_agent_min_role": "user", "ai_agent_api_base_url": "http://127.0.0.1:8642/v1"}, **(settings or {})),
            "get_client_ip": lambda: "127.0.0.1",
            "get_ua": lambda: "pytest",
            "audit": lambda *args, **kwargs: None,
            "json_resp": _json_resp,
            "require_csrf": lambda x: x,
            "require_csrf_safe": lambda x: x,
            "get_db": get_db,
            "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        }
    )
    return app


def test_ai_agent_status_includes_role_scope_and_settings(monkeypatch, tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"})

    monkeypatch.setattr("routes.ai_agent.ai_agent_health", lambda settings: {"ok": True, "url": "http://127.0.0.1:8642/health", "payload": {}})
    monkeypatch.setattr("routes.ai_agent.ai_agent_capabilities", lambda settings: {"ok": True, "chat": True})

    res = app.test_client().get("/api/ai-agent/status")
    payload = res.get_json()

    assert res.status_code == 200
    assert payload["ok"] is True
    assert payload["actor"]["role"] == "user"
    assert payload["settings"]["role"] == "user"
    assert payload["settings"]["scope"]["label"] == "個別用戶助手"


def test_ai_agent_readonly_user_scope_filters_by_permissions(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"})

    res = app.test_client().get("/api/ai-agent/readonly?scope=all&limit=5")
    payload = res.get_json()

    assert res.status_code == 200
    assert payload["ok"] is True
    assert payload["actor"]["role"] == "user"
    assert payload["permissions"]["manage_members"] is False
    assert payload["permissions"]["manage_servers"] is False
    assert "member_management" not in payload
    assert "attack_diagnosis" not in payload
    assert payload["resources"]["cpu"]
    assert payload["comfyui_jobs"]
    assert payload["remote_download_jobs"]


def test_ai_agent_readonly_manager_and_super_admin_incremental_permissions(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    manager_app = _build_app(db_path, {"id": 3, "username": "managerA", "role": "manager"})
    super_app = _build_app(db_path, {"id": 1, "username": "root", "role": "user"})

    manager_payload = manager_app.test_client().get("/api/ai-agent/readonly?scope=all&limit=5").get_json()
    super_payload = super_app.test_client().get("/api/ai-agent/readonly?scope=all&limit=5").get_json()

    assert manager_payload["ok"] is True
    assert manager_payload["actor"]["role"] == "manager"
    assert manager_payload["permissions"]["manage_members"] is True
    assert manager_payload["permissions"]["manage_servers"] is False
    assert "member_management" in manager_payload
    assert "attack_diagnosis" not in manager_payload

    assert super_payload["ok"] is True
    assert super_payload["actor"]["role"] == "super_admin"
    assert super_payload["permissions"]["manage_members"] is True
    assert super_payload["permissions"]["manage_servers"] is True
    assert "member_management" in super_payload
    assert "attack_diagnosis" in super_payload
