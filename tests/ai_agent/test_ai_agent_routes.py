import json
import sqlite3
from urllib import error as urllib_error
from urllib import request as urllib_request

from flask import Flask, jsonify, make_response
from services.ai_agent.hermes import AiAgentError

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
            "role_rank": lambda role: {"user": 0, "manager": 1, "admin": 1, "super_admin": 2}.get(role or "user", 0),
        }
    )
    return app


def _insert_user(db_path, *, user_id, username, role):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, role, status, created_at) VALUES (?, ?, ?, 'active', '2026-01-01T00:03:00')",
            (user_id, username, role),
        )
        conn.commit()
    finally:
        conn.close()


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


def test_ai_agent_readonly_admin_role_keeps_member_scope_only(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    _insert_user(db_path, user_id=4, username="adminA", role="admin")
    admin_app = _build_app(db_path, {"id": 4, "username": "adminA", "role": "admin"})

    admin_payload = admin_app.test_client().get("/api/ai-agent/readonly?scope=all&limit=5").get_json()

    assert admin_payload["ok"] is True
    assert admin_payload["actor"]["role"] == "admin"
    assert admin_payload["permissions"]["manage_members"] is True
    assert admin_payload["permissions"]["manage_servers"] is False
    assert "member_management" in admin_payload
    assert "attack_diagnosis" not in admin_payload


def test_ai_agent_status_admin_keeps_member_scope_only(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    _insert_user(db_path, user_id=4, username="adminA", role="admin")
    app = _build_app(db_path, {"id": 4, "username": "adminA", "role": "admin"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
        "ai_agent_api_key": "secret",
    })

    monkeypatch.setattr("routes.ai_agent.ai_agent_health", lambda settings: {"ok": True, "url": "http://127.0.0.1:8642/health", "payload": {}})
    monkeypatch.setattr("routes.ai_agent.ai_agent_capabilities", lambda settings: {"ok": True, "chat": True})

    res = app.test_client().get("/api/ai-agent/status")
    payload = res.get_json()

    assert res.status_code == 200
    assert payload["ok"] is True
    assert payload["actor"]["role"] == "admin"
    assert payload["actor"]["scope"]["can_manage_members"] is True
    assert payload["actor"]["scope"]["can_manage_servers"] is False
    assert payload["settings"]["role"] == "manager"
    assert payload["settings"]["scope"]["label"] == "管理者助手"


def test_ai_agent_readonly_handles_missing_job_tables(tmp_path):
    db_path = tmp_path / "ai_agent_routes_missing_tables.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, role, status, created_at) VALUES (2, 'userA', 'user', 'active', '2026-01-01T00:00:01')"
        )
        conn.commit()
    finally:
        conn.close()

    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"})
    payload = app.test_client().get("/api/ai-agent/readonly?scope=all&limit=5").get_json()

    assert payload["ok"] is True
    assert payload["comfyui_jobs"] == []
    assert payload["remote_download_jobs"] == []


class _FakeHermesResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self, _size=-1):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_hermes_urlopen_spy(recorded, *, model="hermes-agent"):
    def fake_urlopen(req, timeout=5):
        url = getattr(req, "full_url", "")
        method = getattr(req, "method", None) or getattr(req, "get_method", lambda: "")()
        body = None
        if getattr(req, "data", None):
            try:
                body = json.loads(req.data.decode("utf-8"))
            except Exception:
                body = req.data.decode("utf-8", "replace")
        recorded.append((method.upper(), url, body))

        if url.endswith("/v1/health"):
            raise urllib_error.URLError("fallback")
        if url.endswith("/health"):
            return _FakeHermesResponse({"ok": True, "service": "hermes", "model": model})
        if url.endswith("/capabilities"):
            return _FakeHermesResponse({"tools": ["check_download_state", "suggest_navigation_step", "suggest_prompt"]})
        if url.endswith("/models"):
            return _FakeHermesResponse({"data": [model, "stable-diffusion-xl-base-1.0"]})
        if url.endswith("/chat/completions"):
            return _FakeHermesResponse({
                "model": model,
                "choices": [
                    {"message": {"role": "assistant", "content": "已查到占用與任務進度，建議先確認下載器與模型服務是否重啟。"}},
                ],
            })
        raise urllib_error.URLError(f"unhandled endpoint: {url}")

    return fake_urlopen


def test_ai_agent_routes_smoke_with_fake_hermes_endpoints(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
        "ai_agent_api_key": "smoke-key",
    })

    recorded = []
    monkeypatch.setattr(urllib_request, "urlopen", _make_hermes_urlopen_spy(recorded))

    status = app.test_client().get("/api/ai-agent/status")
    models = app.test_client().get("/api/ai-agent/models")
    chat = app.test_client().post("/api/ai-agent/chat", json={
        "session_id": "smoke-1",
        "messages": [{"role": "user", "content": "幫我看一下下載有沒有在下載"}],
    })
    readonly = app.test_client().get("/api/ai-agent/readonly?scope=all&limit=5")

    assert status.status_code == 200
    status_json = status.get_json()
    assert status_json["ok"] is True
    assert status_json["health"]["ok"] is True
    assert status_json["health"]["url"].endswith("/health")
    assert status_json["capabilities"]["tools"]

    assert models.status_code == 200
    models_json = models.get_json()
    assert models_json["ok"] is True
    assert "hermes-agent" in (models_json["models"].get("data", []))

    assert chat.status_code == 200
    chat_json = chat.get_json()
    assert chat_json["ok"] is True
    assert "已查到占用與任務進度" in chat_json["message"]["content"]

    assert readonly.status_code == 200
    readonly_json = readonly.get_json()
    assert readonly_json["ok"] is True
    assert readonly_json["resources"]["cpu"]["cores"] >= 1
    assert readonly_json["comfyui_jobs"]
    assert readonly_json["remote_download_jobs"]

    assert any(path.endswith("/v1/health") for _, path, _ in recorded)
    assert any(path.endswith("/health") for _, path, _ in recorded)
    assert any(path.endswith("/capabilities") for _, path, _ in recorded)
    assert any(path.endswith("/models") for _, path, _ in recorded)
    assert any(path.endswith("/chat/completions") for _, path, _ in recorded)
    chat_calls = [item for item in recorded if item[1].endswith("/chat/completions")]
    assert chat_calls and chat_calls[0][2]["messages"][0]["role"] == "system"


def test_ai_agent_chat_session_key_is_user_isolated(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    user_app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
    })
    other_app = _build_app(db_path, {"id": 3, "username": "managerA", "role": "manager"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
    })

    calls = []

    def fake_chat(settings, *, messages=None, prompt="", image_data_url="", model="", session_key="", actor=None):
        calls.append({
            "session_key": session_key,
            "actor": actor.get("username") if isinstance(actor, dict) else None,
        })
        return {"content": "ok", "model": "hermes-agent", "usage": {}}

    monkeypatch.setattr("routes.ai_agent.ai_agent_chat", fake_chat)

    user_client = user_app.test_client()
    other_client = other_app.test_client()
    payload = {"session_id": "shared-session", "messages": [{"role": "user", "content": "查一下任務"}]}
    response_a = user_client.post("/api/ai-agent/chat", json=payload)
    response_b = other_client.post("/api/ai-agent/chat", json=payload)

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert len(calls) == 2
    assert calls[0]["actor"] != calls[1]["actor"]
    assert calls[0]["session_key"] == "hackme:2:shared-session"
    assert calls[1]["session_key"] == "hackme:3:shared-session"


def test_ai_agent_chat_rejects_mock_reply(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
    })

    def fake_chat(settings, *, messages=None, prompt="", image_data_url="", model="", session_key="", actor=None):
        raise AiAgentError("AI Agent 後端仍回傳 mock 回覆，請確認 ai_agent_api_base_url 是否指向真實 Hermes endpoint")

    monkeypatch.setattr("routes.ai_agent.ai_agent_chat", fake_chat)

    response = app.test_client().post("/api/ai-agent/chat", json={
        "session_id": "mock-test",
        "messages": [{"role": "user", "content": "幫我看一下下載進度"}],
    })
    payload = response.get_json()

    assert response.status_code == 502
    assert payload["ok"] is False
    assert "mock" in payload["msg"]


def test_ai_agent_chat_rejects_mock_reply_even_if_service_bypasses_guard(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
    })

    def fake_chat(settings, *, messages=None, prompt="", image_data_url="", model="", session_key="", actor=None):
        return {"content": "Mock hermes response: 已收到你的請求。", "model": "hermes-agent", "usage": {}}

    monkeypatch.setattr("routes.ai_agent.ai_agent_chat", fake_chat)

    response = app.test_client().post("/api/ai-agent/chat", json={
        "session_id": "mock-test",
        "messages": [{"role": "user", "content": "幫我看一下下載進度"}],
    })
    payload = response.get_json()

    assert response.status_code == 502
    assert payload["ok"] is False
    assert "mock 回覆" in payload["msg"]


def test_ai_agent_chat_rejects_mock_reply_variants_with_whitespace(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
    })

    def fake_chat(settings, *, messages=None, prompt="", image_data_url="", model="", session_key="", actor=None):
        return {
            "content": " mock\tHermes\nResponse :\u3000已\u6536\u5230\u4f60\u7684\u8bf7\u6c42。 ",
            "model": "hermes-agent",
            "usage": {},
        }

    monkeypatch.setattr("routes.ai_agent.ai_agent_chat", fake_chat)

    response = app.test_client().post("/api/ai-agent/chat", json={
        "session_id": "mock-test",
        "messages": [{"role": "user", "content": "幫我看一下下載進度"}],
    })
    payload = response.get_json()

    assert response.status_code == 502
    assert payload["ok"] is False
    assert "mock 回覆" in payload["msg"]


def test_ai_agent_chat_rejects_simplified_mock_reply_even_if_service_bypasses_guard(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
    })

    def fake_chat(settings, *, messages=None, prompt="", image_data_url="", model="", session_key="", actor=None):
        return {"content": "Mock hermes response: 已收到你的请求。", "model": "hermes-agent", "usage": {}}

    monkeypatch.setattr("routes.ai_agent.ai_agent_chat", fake_chat)

    response = app.test_client().post("/api/ai-agent/chat", json={
        "session_id": "mock-test",
        "messages": [{"role": "user", "content": "幫我看一下下載進度"}],
    })
    payload = response.get_json()

    assert response.status_code == 502
    assert payload["ok"] is False
    assert "mock 回覆" in payload["msg"]


def test_ai_agent_status_marks_mock_health_as_warning(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
    })

    monkeypatch.setattr("routes.ai_agent.ai_agent_health", lambda settings: {
        "ok": False,
        "url": "http://127.0.0.1:8642/health",
        "msg": "偵測到 hermes-mock 後端，請改連到真實 AI Agent 服務",
        "payload": {"service": "hermes-mock"},
    })
    monkeypatch.setattr("routes.ai_agent.ai_agent_capabilities", lambda settings: {"ok": False, "msg": "unavailable"})

    response = app.test_client().get("/api/ai-agent/status")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["health"]["ok"] is False
    assert "hermes-mock" in str(payload["health"]["msg"])
