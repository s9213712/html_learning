import json
import hashlib
import sqlite3
from datetime import datetime
from urllib import error as urllib_error
from urllib import request as urllib_request

from cryptography.fernet import Fernet
from flask import Flask, jsonify, make_response, request
from services.ai_agent.hermes import AiAgentError, AI_AGENT_TOOL_BLUEPRINT, clear_ai_agent_audit_scan_state

from routes.ai_agent import register_ai_agent_routes
from routes.ai_agent import AI_AGENT_WRITE_TOOL_SPECS


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

        CREATE TABLE IF NOT EXISTS secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            action TEXT,
            ip TEXT,
            user TEXT,
            success INTEGER,
            detail TEXT
        );

        CREATE TABLE IF NOT EXISTS uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            system_asset_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS storage_files (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            owner_user_id INTEGER NOT NULL,
            parent_id TEXT,
            display_name TEXT NOT NULL,
            virtual_path TEXT NOT NULL,
            is_trashed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
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
    conn.execute(
        "INSERT OR IGNORE INTO uploaded_files (id, owner_user_id, storage_path, privacy_mode, risk_level, scan_status, original_filename_plain_for_public, mime_type_plain_for_public, size_bytes, system_asset_type, created_at, updated_at, deleted_at)\n"
        "VALUES ('file-user-a', 2, '/tmp/user-a.txt', 'standard_plain', 'low', 'clean', 'user-a.txt', 'text/plain', 12, '', '2026-01-01T00:04:00', '2026-01-01T00:04:10', NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO uploaded_files (id, owner_user_id, storage_path, privacy_mode, risk_level, scan_status, original_filename_plain_for_public, mime_type_plain_for_public, size_bytes, system_asset_type, created_at, updated_at, deleted_at)\n"
        "VALUES ('file-manager-a', 3, '/tmp/manager-a.txt', 'standard_plain', 'low', 'clean', 'manager-a.txt', 'text/plain', 24, '', '2026-01-01T00:05:00', '2026-01-01T00:05:10', NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO storage_files (id, file_id, owner_user_id, parent_id, display_name, virtual_path, is_trashed, created_at, updated_at, deleted_at)\n"
        "VALUES ('storage-user-a', 'file-user-a', 2, NULL, 'user-a.txt', '/user-a.txt', 0, '2026-01-01T00:04:00', '2026-01-01T00:04:10', NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO storage_files (id, file_id, owner_user_id, parent_id, display_name, virtual_path, is_trashed, created_at, updated_at, deleted_at)\n"
        "VALUES ('storage-manager-a', 'file-manager-a', 3, NULL, 'manager-a.txt', '/manager-a.txt', 0, '2026-01-01T00:05:00', '2026-01-01T00:05:10', NULL)"
    )
    conn.commit()
    conn.close()


def _build_app(db_path, actor, *, settings=None, audit_events=None, fernet=None):
    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    app = Flask(__name__)
    app.testing = True

    def record_audit(*args, **kwargs):
        if audit_events is not None:
            audit_events.append({"args": args, "kwargs": kwargs})

    register_ai_agent_routes(
        app,
        {
            "get_current_user_ctx": lambda: actor,
            "get_system_settings": lambda: dict({"module_ai_agent_min_role": "user", "ai_agent_api_base_url": "http://127.0.0.1:8642/v1"}, **(settings or {})),
            "get_client_ip": lambda: "127.0.0.1",
            "get_ua": lambda: "pytest",
            "audit": record_audit,
            "json_resp": _json_resp,
            "require_csrf": lambda x: x,
            "require_csrf_safe": lambda x: x,
            "get_db": get_db,
            "fernet": fernet or Fernet(Fernet.generate_key()),
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
    clear_ai_agent_audit_scan_state()
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
    assert payload["settings"]["operation_mode_policy"]["mode"] == "readonly"
    assert payload["settings"]["safety_boundaries"]
    assert "scan" not in payload["audit"]


def test_ai_agent_write_tools_root_only_and_lists_allowed_tools(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    settings = {
        "ai_agent_operation_mode": "write",
        "ai_agent_allowed_tools": "write_community_create_thread,write_launch_requirements_check",
    }
    user_app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings=settings)
    root_app = _build_app(db_path, {"id": 1, "username": "root", "role": "user"}, settings=settings)

    user_response = user_app.test_client().get("/api/ai-agent/write-tools")
    root_response = root_app.test_client().get("/api/ai-agent/write-tools")
    payload = root_response.get_json()

    assert user_response.status_code == 403
    assert root_response.status_code == 200
    assert payload["ok"] is True
    assert payload["root_only"] is True
    assert payload["write_enabled"] is True
    assert len(payload["catalog_sha256"]) == 64
    assert [tool["name"] for tool in payload["tools"]] == [
        "write_community_create_thread",
        "write_launch_requirements_check",
    ]


def test_ai_agent_write_tools_can_return_full_catalog_for_root_selector(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_community_create_thread",
        },
    )

    response = app.test_client().get("/api/ai-agent/write-tools?include_all=1")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["allowed_tools"] == "write_community_create_thread"
    assert [tool["name"] for tool in payload["tools"]] == ["write_community_create_thread"]
    assert {tool["name"] for tool in payload["catalog_tools"]} == set(AI_AGENT_WRITE_TOOL_SPECS)
    thread_tool = next(tool for tool in payload["catalog_tools"] if tool["name"] == "write_community_create_thread")
    assert thread_tool["domain"] == "community"
    assert thread_tool["data_scope"] == "write_tool:community"
    assert "board_id" in thread_tool["required"]
    assert "title" in thread_tool["body_fields"]
    assert "canonical args" in thread_tool["arg_hint"]


def test_ai_agent_write_tools_none_sentinel_disables_all_tools(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "__none__",
        },
    )

    response = app.test_client().get("/api/ai-agent/write-tools?include_all=1")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["allowed_tools"] == "__none__"
    assert payload["tools"] == []
    assert len(payload["catalog_tools"]) == len(AI_AGENT_WRITE_TOOL_SPECS)


def test_ai_agent_write_tools_lockdown_blocks_list_and_execute(monkeypatch, tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    audit_events = []
    settings = {
        "ai_agent_operation_mode": "write",
        "ai_agent_allowed_tools": "write_community_create_thread",
    }
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings=settings,
        audit_events=audit_events,
    )
    monkeypatch.setattr("routes.ai_agent.ai_agent_write_guard_status", lambda **kwargs: {
        "blocked": True,
        "reason": "AI Agent 敏感設定近期被修改",
        "anomalies": [{"code": "ai_agent.sensitive_settings_changed", "severity": "alert"}],
        "scanned_at": "2026-06-22T15:00:00",
    })
    client = app.test_client()

    listed = client.get("/api/ai-agent/write-tools")
    executed = client.post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_community_create_thread",
        "confirm": "EXECUTE",
        "arguments": {"board_id": 1, "title": "hello", "content": "world"},
    })

    assert listed.status_code == 423
    assert executed.status_code == 423
    assert listed.get_json()["guard"]["blocked"] is True
    lockdown_events = [
        event for event in audit_events
        if event["args"][0] == "AI_AGENT_WRITE_TOOLS_LOCKDOWN"
    ]
    assert len(lockdown_events) == 2


def test_ai_agent_write_tools_lockdown_uses_persistent_audit_guard(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO secure_audit (ts, action, ip, user, success, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now().replace(microsecond=0).isoformat(),
            "AI_AGENT_AUDIT_MAIN_AI_GUARD",
            "127.0.0.1",
            "root",
            0,
            "sensitive_settings_changed keys=ai_agent_allowed_tools user=root",
        ),
    )
    conn.commit()
    conn.close()
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_community_create_thread",
        },
    )

    response = app.test_client().get("/api/ai-agent/write-tools")
    payload = response.get_json()

    assert response.status_code == 423
    assert payload["guard"]["blocked"] is True
    assert payload["guard"]["source"] == "secure_audit"
    assert payload["guard"]["anomalies"][0]["code"] == "ai_agent.persistent_write_guard"


def test_ai_agent_write_tools_include_expanded_capability_domains(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    settings = {
        "ai_agent_operation_mode": "write",
        "ai_agent_allowed_tools": (
            "write_trading_place_order,write_remote_download_direct,write_cloud_drive_create_text,"
            "write_share_create,write_album_create,write_video_publish,write_transcode_hls,"
            "write_subtitle_upload,write_community_post_penalty,write_points_governance_execute,"
            "write_points_wallet_transfer,write_member_set_avatar_from_cloud,write_server_restart,write_incident_enter,"
            "write_chat_create_room,write_appeal_review,write_notification_send,write_report_resolve,"
            "write_moderation_proposal_create,write_storage_quota_override,write_cloud_drive_text_update,"
            "write_comfyui_start,write_comfyui_workflow_run,write_comfyui_civitai_search,write_security_test_stress"
        ),
    }
    app = _build_app(db_path, {"id": 1, "username": "root", "role": "user"}, settings=settings)

    response = app.test_client().get("/api/ai-agent/write-tools")
    payload = response.get_json()
    names = [tool["name"] for tool in payload["tools"]]

    assert response.status_code == 200
    assert set(names) == set(settings["ai_agent_allowed_tools"].split(","))
    assert all(tool["requires_confirm"] for tool in payload["tools"])
    domains = {tool["name"]: tool["domain"] for tool in payload["tools"]}
    assert domains["write_chat_create_room"] == "chat"
    assert domains["write_storage_quota_override"] == "storage"
    assert domains["write_security_test_stress"] == "security"


def test_ai_agent_write_tool_specs_stay_in_blueprint():
    assert set(AI_AGENT_WRITE_TOOL_SPECS).issubset(set(AI_AGENT_TOOL_BLUEPRINT))


def test_ai_agent_all_write_tool_names_are_recognized_when_allowed(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    tool_names = [name for name in AI_AGENT_WRITE_TOOL_SPECS if name != "audit_scan"]
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": ",".join(tool_names),
        },
    )
    client = app.test_client()

    for tool_name in tool_names:
        response = client.post("/api/ai-agent/write-tools/execute", json={
            "tool": tool_name,
            "confirm": "EXECUTE",
            "arguments": {},
        })
        payload = response.get_json()
        assert payload.get("msg") != "不支援的 write tool", tool_name
        assert payload.get("msg") != "此工具未在目前 AI Agent allowed_tools/角色範圍內啟用", tool_name


def test_ai_agent_write_tools_labels_disambiguate_similar_actions(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    tool_names = [
        "write_task_retry",
        "write_automation_job_run",
        "write_video_upload",
        "write_video_publish",
    ]
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": ",".join(tool_names),
        },
    )

    response = app.test_client().get("/api/ai-agent/write-tools")
    payload = response.get_json()

    assert response.status_code == 200
    labels = {tool["name"]: tool["label"] for tool in payload["tools"]}
    assert labels["write_task_retry"] == "重試任務"
    assert labels["write_automation_job_run"] == "重試自動化任務"
    assert labels["write_video_upload"] == "AI Agent JSON 版影音發布"
    assert labels["write_video_publish"] == "發布既有雲端影音"


def test_ai_agent_write_tool_execute_requires_write_mode_for_mutation(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "assist",
            "ai_agent_allowed_tools": "write_community_create_thread",
        },
    )

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_community_create_thread",
        "confirm": "EXECUTE",
        "arguments": {"board_id": 1, "title": "hello", "content": "world"},
    })
    payload = response.get_json()

    assert response.status_code == 409
    assert payload["ok"] is False
    assert "operation mode" in payload["msg"]


def test_ai_agent_write_tool_execute_allows_root_elevate_once(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "readonly",
            "ai_agent_allowed_tools": "write_community_create_thread",
        },
    )
    captured = {}

    @app.route("/api/community/boards/<int:board_id>/threads", methods=["POST"])
    def fake_thread_create(board_id):
        captured["board_id"] = board_id
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "thread_id": 456})

    blocked = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_community_create_thread",
        "confirm": "EXECUTE",
        "arguments": {"board_id": 1, "title": "hello", "content": "world"},
    })
    allowed = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_community_create_thread",
        "confirm": "EXECUTE",
        "elevate_once": "ALLOW_WRITE_ONCE",
        "arguments": {"board_id": 1, "title": "hello", "content": "world"},
    })
    payload = allowed.get_json()

    assert blocked.status_code == 409
    assert allowed.status_code == 200
    assert payload["ok"] is True
    assert payload["elevated_once"] is True
    assert captured["board_id"] == 1


def test_ai_agent_conversation_memory_is_encrypted_and_user_isolated(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    root_app = _build_app(db_path, {"id": 1, "username": "root", "role": "user"})
    user_app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"})

    payload = {
        "sessionId": "default",
        "messages": [{"role": "user", "content": "secret habit: use ogipote style"}],
        "habits": {"style": "ogipote"},
    }
    saved = root_app.test_client().put("/api/ai-agent/conversation", json={
        "conversation_id": "default",
        "payload": payload,
    })
    root_loaded = root_app.test_client().get("/api/ai-agent/conversation?conversation_id=default")
    user_loaded = user_app.test_client().get("/api/ai-agent/conversation?conversation_id=default")

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT payload_encrypted FROM ai_agent_conversations WHERE owner_user_id=1").fetchone()
    finally:
        conn.close()

    assert saved.status_code == 200
    assert saved.get_json()["encrypted"] is True
    assert row is not None
    raw_encrypted = row[0]
    assert "secret habit" not in raw_encrypted
    assert "ogipote" not in raw_encrypted
    assert root_loaded.status_code == 200
    assert root_loaded.get_json()["payload"]["messages"][0]["content"] == "secret habit: use ogipote style"
    assert user_loaded.status_code == 200
    assert user_loaded.get_json()["payload"]["messages"] == []


def test_ai_agent_conversation_history_is_root_only_and_cross_session(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    shared_fernet = Fernet(Fernet.generate_key())
    root_app = _build_app(db_path, {"id": 1, "username": "root", "role": "user"}, fernet=shared_fernet)
    user_app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, fernet=shared_fernet)

    saved = root_app.test_client().put("/api/ai-agent/conversation", json={
        "conversation_id": "default",
        "payload": {
            "sessionId": "default",
            "messages": [{"role": "user", "content": "live browser request"}],
        },
    })
    encrypted = shared_fernet.encrypt(json.dumps({
        "sessionId": "avatar-audit",
        "messages": [
            {"role": "user", "content": "請產圖並替換頭像"},
            {"role": "assistant", "content": "已完成視覺預檢並拒絕低品質裁切"},
        ],
        "habits": {},
    }, ensure_ascii=False).encode("utf-8")).decode("utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ai_agent_conversations
                (owner_user_id, session_binding, conversation_id, payload_encrypted, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (1, "playwright-session", "avatar-audit", encrypted, "2026-06-22T20:00:00", "2026-06-22T20:10:00"),
        )
        conn.commit()
    finally:
        conn.close()

    denied = user_app.test_client().get("/api/ai-agent/conversation-history")
    listed = root_app.test_client().get("/api/ai-agent/conversation-history?limit=10")
    detail = root_app.test_client().get(
        "/api/ai-agent/conversation-history"
        "?limit=1&include_payload=1&owner_user_id=1&session_binding=playwright-session&conversation_id=avatar-audit"
    )
    listed_payload = listed.get_json()
    detail_payload = detail.get_json()

    assert saved.status_code == 200
    assert denied.status_code == 403
    assert listed.status_code == 200
    assert listed_payload["root_only"] is True
    assert {item["conversation_id"] for item in listed_payload["conversations"]} >= {"default", "avatar-audit"}
    audit_row = next(item for item in listed_payload["conversations"] if item["conversation_id"] == "avatar-audit")
    assert audit_row["session_binding"] == "playwright-session"
    assert audit_row["message_count"] == 2
    assert "請產圖" in audit_row["last_user"]
    assert detail.status_code == 200
    assert detail_payload["conversations"][0]["payload"]["messages"][1]["content"] == "已完成視覺預檢並拒絕低品質裁切"


def test_ai_agent_write_tool_execute_dispatches_allowlisted_read_tool(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={"ai_agent_allowed_tools": "write_launch_requirements_check"},
    )

    @app.route("/api/root/server-mode/requirements", methods=["GET"])
    def fake_requirements():
        return _json_resp({
            "ok": True,
            "checked": True,
            "session_cookie_seen": bool(request.cookies.get("session_token")),
        })

    client = app.test_client()
    client.set_cookie("session_token", "root-session")
    response = client.post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_launch_requirements_check",
        "arguments": {},
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["tool"] == "write_launch_requirements_check"
    assert payload["result"]["checked"] is True
    assert payload["result"]["session_cookie_seen"] is True


def test_ai_agent_expanded_write_tool_dispatches_json_route(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_trading_place_order",
        },
    )
    captured = {}

    @app.route("/api/trading/orders", methods=["POST"])
    def fake_trading_order():
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "order": {"order_uuid": "ord-1", "market_symbol": captured.get("market_symbol")}})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_trading_place_order",
        "confirm": "EXECUTE",
        "arguments": {
            "market_symbol": "BTC/POINTS",
            "side": "buy",
            "order_type": "limit",
            "quantity": "0.01",
            "limit_price_points": 100,
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["market_symbol"] == "BTC/POINTS"
    assert captured["side"] == "buy"


def test_ai_agent_remote_download_bt_aliases_magnet_uri_to_url(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_remote_download_bt",
        },
    )
    captured = {}

    @app.route("/api/cloud-drive/remote-download/torrent-tasks", methods=["POST"])
    def fake_remote_download_bt():
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "task_id": "bt-1"})

    magnet_uri = "magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=audit-test.iso"
    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_remote_download_bt",
        "confirm": "EXECUTE",
        "arguments": {"magnet_uri": magnet_uri, "filename": "audit-test.iso"},
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["url"] == magnet_uri
    assert "magnet_uri" not in captured
    assert captured["filename"] == "audit-test.iso"


def test_ai_agent_album_add_file_aliases_cloud_file_id(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_album_add_file",
        },
    )
    captured = {}

    @app.route("/api/storage/albums/<album_id>/files", methods=["POST"])
    def fake_album_add_file(album_id):
        captured["album_id"] = album_id
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "album_id": album_id})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_album_add_file",
        "confirm": "EXECUTE",
        "arguments": {"album_id": "album-audit-1", "cloud_file_id": "file-audit-1", "caption": "AI audit sample"},
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["album_id"] == "album-audit-1"
    assert captured["file_id"] == "file-audit-1"
    assert "cloud_file_id" not in captured
    assert captured["caption"] == "AI audit sample"

    missing = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_album_add_file",
        "confirm": "EXECUTE",
        "arguments": {"album_id": "album-audit-1"},
    })
    assert missing.status_code == 400
    assert "file_id 或 storage_file_id" in missing.get_json()["msg"]


def test_ai_agent_points_wallet_transfer_dispatches_submit_transaction(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_points_wallet_transfer",
        },
    )
    captured = {}

    @app.route("/api/points/transactions/submit", methods=["POST"])
    def fake_points_transfer():
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "transaction_hash": "tx-1", "compact": bool(captured.get("compact"))})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_points_wallet_transfer",
        "confirm": "EXECUTE",
        "arguments": {
            "source_wallet_address": "HP_SRC",
            "destination_wallet_address": "HP_DST",
            "amount_points": 10,
            "fee_points": 1,
            "request_uuid": "ai-agent-transfer-test-1",
            "memo": "ai agent qa",
            "signature": "test-signature",
            "compact": True,
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["source_wallet_address"] == "HP_SRC"
    assert captured["destination_wallet_address"] == "HP_DST"
    assert captured["amount_points"] == 10
    assert captured["request_uuid"] == "ai-agent-transfer-test-1"


def test_ai_agent_member_avatar_from_cloud_wraps_multipart_crop_decision(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_member_set_avatar_from_cloud",
        },
    )
    captured = {}

    @app.route("/api/admin/users/<int:user_id>/avatar", methods=["POST"])
    def fake_avatar_upload(user_id):
        captured["user_id"] = user_id
        captured["cloud_file_id"] = request.form.get("cloud_file_id")
        captured["crop_json"] = request.form.get("crop_json")
        return _json_resp({"ok": True, "avatar_file_id": captured["cloud_file_id"], "avatar_crop": json.loads(captured["crop_json"])})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_member_set_avatar_from_cloud",
        "confirm": "EXECUTE",
        "arguments": {
            "user_id": 1,
            "cloud_file_id": "generated-avatar-file",
            "crop": {"x": 8, "y": 4, "width": 512, "height": 512, "rotation": 90},
            "zoom": 1.2,
            "decision_reason": "portrait is sideways; rotate right and center crop",
            "confidence": 0.88,
            "subject_detected": True,
            "crop_quality": "good",
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["user_id"] == 1
    assert captured["cloud_file_id"] == "generated-avatar-file"
    assert json.loads(captured["crop_json"]) == {"x": 8, "y": 4, "width": 512, "height": 512, "rotation": 90}
    assert payload["result"]["avatar_ai_decision"]["zoom"] == 1.2
    assert payload["result"]["avatar_ai_decision"]["confidence"] == 0.88


def test_ai_agent_member_avatar_rejects_low_confidence_visual_decision(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_member_set_avatar_from_cloud",
        },
    )

    @app.route("/api/admin/users/<int:user_id>/avatar", methods=["POST"])
    def fake_avatar_upload(user_id):
        raise AssertionError("low-confidence AI avatar decision must not write")

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_member_set_avatar_from_cloud",
        "confirm": "EXECUTE",
        "arguments": {
            "user_id": 1,
            "cloud_file_id": "generated-avatar-file",
            "crop": {"x": 0, "y": 0, "width": 512, "height": 512},
            "zoom": 1.5,
            "decision_reason": "Image contains abstract glitch art with no discernible human face or shoulders.",
            "confidence": 0.4,
            "subject_detected": False,
            "crop_quality": "poor",
            "issues": ["no visible subject"],
        },
    })
    payload = response.get_json()

    assert response.status_code == 400
    assert payload["ok"] is False
    assert "不適合作為頭像" in payload["result"]["msg"]
    assert payload["result"]["avatar_ai_decision"]["confidence"] == 0.4


def test_ai_agent_member_avatar_rejects_failed_visual_preflight(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_member_set_avatar_from_cloud",
        },
    )

    @app.route("/api/admin/users/<int:user_id>/avatar", methods=["POST"])
    def fake_avatar_upload(user_id):
        raise AssertionError("failed AI avatar preflight must not write")

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_member_set_avatar_from_cloud",
        "confirm": "EXECUTE",
        "arguments": {
            "user_id": 1,
            "cloud_file_id": "generated-avatar-file",
            "crop": {"x": 112, "y": 112, "width": 400, "height": 400},
            "zoom": 1.5,
            "decision_reason": "Focus on face and shoulders",
            "confidence": 0.9,
            "subject_detected": True,
            "crop_quality": "good",
            "preflight_ok": False,
            "preflight_crop_quality": "needs_adjustment",
            "preflight_issues": ["face_off_center", "excessive_whitespace", "text_interference"],
        },
    })
    payload = response.get_json()

    assert response.status_code == 400
    assert payload["ok"] is False
    assert "不適合作為頭像" in payload["result"]["msg"]
    assert payload["result"]["avatar_ai_decision"]["preflight_ok"] is False
    assert payload["result"]["avatar_ai_decision"]["preflight_issues"] == [
        "face_off_center",
        "excessive_whitespace",
        "text_interference",
    ]


def test_ai_agent_safe_path_param_supports_market_symbol(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_trading_market_update",
        },
    )
    captured = {}

    @app.route("/api/root/trading/markets/<path:symbol>", methods=["POST"])
    def fake_market_update(symbol):
        captured["symbol"] = symbol
        captured["body"] = request.get_json(silent=True) or {}
        return _json_resp({"ok": True, "symbol": symbol})

    allowed = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_trading_market_update",
        "confirm": "EXECUTE",
        "arguments": {"symbol": "BTC/POINTS", "enabled": True},
    })
    blocked = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_trading_market_update",
        "confirm": "EXECUTE",
        "arguments": {"symbol": "../server.py", "enabled": True},
    })

    assert allowed.status_code == 200
    assert allowed.get_json()["ok"] is True
    assert captured["symbol"] == "BTC/POINTS"
    assert blocked.status_code == 400
    assert "相對跳脫" in blocked.get_json()["msg"]


def test_ai_agent_subtitle_tool_wraps_text_as_multipart(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_subtitle_upload",
        },
    )
    captured = {}

    @app.route("/api/videos/<int:video_id>/subtitles", methods=["POST"])
    def fake_subtitle_upload(video_id):
        upload = request.files.get("subtitle")
        captured["video_id"] = video_id
        captured["filename"] = upload.filename
        captured["text"] = upload.read().decode("utf-8")
        captured["label"] = request.form.get("label")
        return _json_resp({"ok": True, "video_id": video_id, "filename": upload.filename})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_subtitle_upload",
        "confirm": "EXECUTE",
        "arguments": {
            "video_id": 7,
            "subtitle_text": "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello",
            "filename": "agent.vtt",
            "label": "English",
            "language": "en",
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured == {
        "video_id": 7,
        "filename": "agent.vtt",
        "text": "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello",
        "label": "English",
    }


def test_ai_agent_community_write_tool_maps_discussion_post_type(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_community_create_thread",
        },
    )
    captured = {}

    @app.route("/api/community/boards/<int:board_id>/threads", methods=["POST"])
    def fake_thread_create(board_id):
        captured["board_id"] = board_id
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "thread_id": 123})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_community_create_thread",
        "confirm": "EXECUTE",
        "arguments": {
            "board_id": 1,
            "title": "AI Agent QA",
            "content": "natural-language discussion post",
            "post_type": "discussion",
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["board_id"] == 1
    assert captured["post_type"] == "normal"


def test_ai_agent_comfyui_write_tool_maps_checkpoint_to_model(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_comfyui_generate",
        },
    )
    captured = {}

    @app.route("/api/comfyui/models", methods=["GET"])
    def fake_comfyui_models():
        return _json_resp({"ok": True, "models": ["JANKU-V777.safetensors"]})

    @app.route("/api/comfyui/generate", methods=["POST"])
    def fake_comfyui_generate():
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "job": {"job_id": "job-1", "status": "queued"}})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_comfyui_generate",
        "confirm": "EXECUTE",
        "arguments": {
            "prompt": "by ogipote, 2girls, bikini",
            "checkpoint": "JANKU-V777.safetensor",
            "width": 1024,
            "height": 1024,
            "confirm_billing": True,
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["model"] == "JANKU-V777.safetensors"
    assert captured["checkpoint"] == "JANKU-V777.safetensors"


def test_ai_agent_comfyui_write_tool_resolves_natural_checkpoint_name(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_comfyui_generate",
        },
    )
    captured = {}

    @app.route("/api/comfyui/models", methods=["GET"])
    def fake_comfyui_models():
        return _json_resp({
            "ok": True,
            "models": [
                "JANKUTrainedChenkinNoobai_v69.safetensors",
                "JANKUTrainedChenkinNoobai_v777.safetensors",
                "netayumeLuminaNetaLumina_v40.safetensors",
                "perfectionRealisticILXL_60.safetensors",
            ],
        })

    @app.route("/api/comfyui/generate", methods=["POST"])
    def fake_comfyui_generate():
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "job": {"job_id": "job-resolved", "status": "queued"}})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_comfyui_generate",
        "confirm": "EXECUTE",
        "arguments": {
            "prompt": "by ogipote, 2girls, bikini",
            "checkpoint": "JANKU…..V777",
            "width": 1024,
            "height": 1024,
            "confirm_billing": True,
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["model"] == "JANKUTrainedChenkinNoobai_v777.safetensors"
    assert captured["checkpoint"] == "JANKUTrainedChenkinNoobai_v777.safetensors"


def test_ai_agent_comfyui_write_tool_resolves_generic_sdxl_checkpoint(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_comfyui_generate",
        },
    )
    captured = {}

    @app.route("/api/comfyui/models", methods=["GET"])
    def fake_comfyui_models():
        return _json_resp({
            "ok": True,
            "models": [
                "JANKUTrainedChenkinNoobai_v69.safetensors",
                "JANKUTrainedChenkinNoobai_v777.safetensors",
                "netayumeLuminaNetaLumina_v40.safetensors",
                "perfectionRealisticILXL_60.safetensors",
            ],
        })

    @app.route("/api/comfyui/generate", methods=["POST"])
    def fake_comfyui_generate():
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "job": {"job_id": "job-generic-sdxl", "status": "queued"}})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_comfyui_generate",
        "confirm": "EXECUTE",
        "arguments": {
            "prompt": "by ogipote, 2girls, bikini",
            "checkpoint": "sdxl_base_1.0.ckpt",
            "official_workflow_id": "origin_sdxl_txt2img",
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["model"] == "JANKUTrainedChenkinNoobai_v777.safetensors"
    assert captured["checkpoint"] == "JANKUTrainedChenkinNoobai_v777.safetensors"


def test_ai_agent_comfyui_write_tool_defaults_missing_checkpoint(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_comfyui_generate",
        },
    )
    captured = {}

    @app.route("/api/comfyui/models", methods=["GET"])
    def fake_comfyui_models():
        return _json_resp({
            "ok": True,
            "models": [
                "JANKUTrainedChenkinNoobai_v69.safetensors",
                "JANKUTrainedChenkinNoobai_v777.safetensors",
            ],
        })

    @app.route("/api/comfyui/generate", methods=["POST"])
    def fake_comfyui_generate():
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "job": {"job_id": "job-default-model", "status": "queued"}})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_comfyui_generate",
        "confirm": "EXECUTE",
        "arguments": {
            "prompt": "by ogipote, 2girls, bikini",
            "official_workflow_id": "origin_sdxl_txt2img",
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["model"] == "JANKUTrainedChenkinNoobai_v777.safetensors"
    assert captured["checkpoint"] == "JANKUTrainedChenkinNoobai_v777.safetensors"


def test_ai_agent_comfyui_write_tool_preserves_image_edit_args(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_comfyui_generate",
        },
    )
    captured = {}

    @app.route("/api/comfyui/models", methods=["GET"])
    def fake_comfyui_models():
        return _json_resp({"ok": True, "models": ["JANKUTrainedChenkinNoobai_v777.safetensors"]})

    @app.route("/api/comfyui/generate", methods=["POST"])
    def fake_comfyui_generate():
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "job": {"job_id": "job-img2img-edit", "status": "queued"}})

    source_ref = {"filename": "source.png", "subfolder": "2026-06-23", "type": "output"}
    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_comfyui_generate",
        "confirm": "EXECUTE",
        "arguments": {
            "prompt": "turn the existing portrait into watercolor style",
            "generation_mode": "style_transfer",
            "image_ref": source_ref,
            "denoise": 0.62,
            "cfg_scale": 6.5,
            "sampler": "euler",
            "mask_image_ref": None,
            "vae": None,
            "confirm_billing": True,
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["generation_mode"] == "img2img"
    assert captured["source_image_ref"] == source_ref
    assert captured["denoise_strength"] == 0.62
    assert captured["cfg"] == 6.5
    assert captured["sampler_name"] == "euler"
    assert "mask_image_ref" not in captured
    assert "vae" not in captured


def test_ai_agent_comfyui_write_tool_flattens_outpaint_args(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_comfyui_generate",
        },
    )
    captured = {}

    @app.route("/api/comfyui/models", methods=["GET"])
    def fake_comfyui_models():
        return _json_resp({"ok": True, "models": ["JANKUTrainedChenkinNoobai_v777.safetensors"]})

    @app.route("/api/comfyui/generate", methods=["POST"])
    def fake_comfyui_generate():
        captured.update(request.get_json(silent=True) or {})
        return _json_resp({"ok": True, "job": {"job_id": "job-outpaint-edit", "status": "queued"}})

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_comfyui_generate",
        "confirm": "EXECUTE",
        "arguments": {
            "prompt": "extend the same scene outward",
            "mode": "outpainting",
            "source_image_ref": {"filename": "scene.png", "subfolder": "", "type": "output"},
            "outpaint": {"left": 128, "top": 64, "right": 128, "bottom": 64, "feathering": 48},
            "confirm_billing": True,
        },
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert captured["generation_mode"] == "outpaint"
    assert captured["outpaint_left"] == 128
    assert captured["outpaint_top"] == 64
    assert captured["outpaint_right"] == 128
    assert captured["outpaint_bottom"] == 64
    assert captured["outpaint_feathering"] == 48


def test_ai_agent_comfyui_write_tool_rejects_unknown_checkpoint_before_queue(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_comfyui_generate",
        },
    )

    @app.route("/api/comfyui/models", methods=["GET"])
    def fake_comfyui_models():
        return _json_resp({
            "ok": True,
            "models": [
                "JANKUTrainedChenkinNoobai_v69.safetensors",
                "JANKUTrainedChenkinNoobai_v777.safetensors",
            ],
        })

    @app.route("/api/comfyui/generate", methods=["POST"])
    def fake_comfyui_generate():
        raise AssertionError("unknown checkpoint must be rejected before queuing")

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_comfyui_generate",
        "confirm": "EXECUTE",
        "arguments": {
            "prompt": "by ogipote, 2girls, bikini",
            "checkpoint": "missing checkpoint",
        },
    })
    payload = response.get_json()

    assert response.status_code == 400
    assert payload["ok"] is False
    assert "不在 ComfyUI checkpoint 清單" in payload["msg"]


def test_ai_agent_write_tool_execute_blocks_unallowed_tool(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "write",
            "ai_agent_allowed_tools": "write_launch_requirements_check",
        },
    )

    response = app.test_client().post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_community_create_thread",
        "confirm": "EXECUTE",
        "arguments": {"board_id": 1, "title": "hello", "content": "world"},
    })
    payload = response.get_json()

    assert response.status_code == 403
    assert payload["ok"] is False
    assert "未在目前 AI Agent allowed_tools" in payload["msg"]


def test_ai_agent_frontend_routes_emit_audit_events(monkeypatch, tmp_path):
    clear_ai_agent_audit_scan_state()
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    audit_events = []
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "ai_agent_operation_mode": "readonly",
            "ai_agent_allowed_tools": "write_community_create_thread,write_launch_requirements_check",
        },
        audit_events=audit_events,
    )
    monkeypatch.setattr("routes.ai_agent.ai_agent_health", lambda settings: {"ok": True, "url": "http://127.0.0.1:11434/v1/models", "payload": {}})
    monkeypatch.setattr("routes.ai_agent.ai_agent_capabilities", lambda settings: {"ok": True, "chat": True})
    monkeypatch.setattr("routes.ai_agent.ai_agent_models", lambda settings: {"object": "list", "data": [{"id": "gpt-oss:120b-cloud"}]})

    client = app.test_client()
    assert client.get("/api/ai-agent/status").status_code == 200
    assert client.get("/api/ai-agent/models").status_code == 200
    assert client.get("/api/ai-agent/readonly?scope=resources").status_code == 200
    assert client.get("/api/ai-agent/write-tools").status_code == 200
    blocked = client.post("/api/ai-agent/write-tools/execute", json={
        "tool": "write_community_create_thread",
        "confirm": "EXECUTE",
        "arguments": {"board_id": 1, "title": "hello", "content": "world"},
    })

    actions = [event["args"][0] for event in audit_events]
    assert blocked.status_code == 409
    assert "AI_AGENT_STATUS" in actions
    assert "AI_AGENT_MODELS" in actions
    assert "AI_AGENT_READONLY" in actions
    assert "AI_AGENT_WRITE_TOOLS_LIST" in actions
    assert any(
        event["args"][0] == "AI_AGENT_WRITE_TOOL"
        and event["kwargs"].get("success") is False
        and "operation_mode_not_write" in event["kwargs"].get("detail", "")
        for event in audit_events
    )


def test_ai_agent_models_backend_unavailable_degrades_without_5xx(monkeypatch, tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    audit_events = []
    app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        audit_events=audit_events,
    )

    def unavailable_models(settings):
        raise AiAgentError("AI Agent backend 無法連線：connection refused")

    monkeypatch.setattr("routes.ai_agent.ai_agent_models", unavailable_models)
    response = app.test_client().get("/api/ai-agent/models")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is False
    assert payload["models"] == {}
    assert payload["backend_unavailable"] is True
    assert any(
        event["args"][0] == "AI_AGENT_MODELS"
        and event["kwargs"].get("success") is False
        for event in audit_events
    )


def test_ai_agent_status_only_super_admin_gets_audit_scan(monkeypatch, tmp_path):
    clear_ai_agent_audit_scan_state()
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    user_app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={"ai_agent_operation_mode": "audit"})
    super_app = _build_app(db_path, {"id": 1, "username": "root", "role": "user"}, settings={"ai_agent_operation_mode": "audit"})

    monkeypatch.setattr("routes.ai_agent.ai_agent_health", lambda settings: {"ok": True, "url": "http://127.0.0.1:8642/health", "payload": {}})
    monkeypatch.setattr("routes.ai_agent.ai_agent_capabilities", lambda settings: {"ok": True, "chat": True})

    user_payload = user_app.test_client().get("/api/ai-agent/status").get_json()
    root_payload = super_app.test_client().get("/api/ai-agent/status").get_json()

    assert user_payload["ok"] is True
    assert user_payload["audit"]["scheduler"]["enabled"] is True
    assert "scan" not in user_payload["audit"]
    assert root_payload["ok"] is True
    assert "scan" in root_payload["audit"]


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
    assert [item["owner_user_id"] for item in payload["storage_files"]] == [2]


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
    assert sorted(item["owner_user_id"] for item in super_payload["storage_files"]) == [2, 3]


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
        self._body = json.dumps(payload).encode("utf-8")
        self._offset = 0

    def read(self, size=-1):
        if self._offset >= len(self._body):
            return b""
        if size is None or int(size) < 0:
            chunk = self._body[self._offset:]
            self._offset = len(self._body)
            return chunk
        end = min(len(self._body), self._offset + int(size))
        chunk = self._body[self._offset:end]
        self._offset = end
        return chunk

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
        "model": "hermes-agent",
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
    system_prompt = chat_calls[0][2]["messages"][0]["content"]
    assert "不能在一般聊天中聲稱已呼叫" in system_prompt
    assert "工具：check_generation_progress" in system_prompt


def test_ai_agent_audit_scan_requires_super_admin(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    user_app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={"module_ai_agent_min_role": "user"})
    super_app = _build_app(db_path, {"id": 1, "username": "root", "role": "user"}, settings={"module_ai_agent_min_role": "user"})

    called = []

    def fake_scan(settings, *, get_db, get_audit_db=None, actor=None, force=False, get_client_ip=None, get_ua=None, audit=None):
        called.append({"actor": actor, "force": force})
        return {"status": "ok", "cached": False}

    monkeypatch.setattr("routes.ai_agent.run_ai_agent_audit_scan", fake_scan)

    user_resp = user_app.test_client().get("/api/ai-agent/audit-scan")
    assert user_resp.status_code == 403
    assert user_resp.get_json()["ok"] is False

    super_resp = super_app.test_client().post("/api/ai-agent/audit-scan", json={"force": True})
    assert super_resp.status_code == 200
    payload = super_resp.get_json()
    assert payload["ok"] is True
    assert payload["scan"]["status"] == "ok"
    assert called and called[-1]["force"] is True
    assert called[-1]["actor"]["username"] == "root"


def test_ai_agent_audit_scan_accepts_force_query_string(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    super_app = _build_app(db_path, {"id": 1, "username": "root", "role": "user"}, settings={"module_ai_agent_min_role": "user"})

    called = []

    def fake_scan(settings, *, get_db, get_audit_db=None, actor=None, force=False, get_client_ip=None, get_ua=None, audit=None):
        called.append(force)
        return {"status": "ok", "cached": False}

    monkeypatch.setattr("routes.ai_agent.run_ai_agent_audit_scan", fake_scan)

    with_force = super_app.test_client().get("/api/ai-agent/audit-scan?force=true")
    plain = super_app.test_client().get("/api/ai-agent/audit-scan")

    assert with_force.status_code == 200
    assert plain.status_code == 200
    assert called[0] is True
    assert called[1] is False


def test_ai_agent_audit_status_shows_scheduler_summary_for_super_admin(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    super_app = _build_app(
        db_path,
        {"id": 1, "username": "root", "role": "user"},
        settings={
            "module_ai_agent_min_role": "user",
            "ai_agent_operation_mode": "audit",
            "ai_agent_audit_interval_minutes": 7,
        },
    )
    response = super_app.test_client().get("/api/ai-agent/audit-status")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["audit_status"]["scheduler"]["enabled"] is True
    assert payload["audit_status"]["scheduler"]["interval_minutes"] == 7
    assert "summary" in payload["audit_status"]


def test_ai_agent_audit_status_forbidden_for_non_super_admin(tmp_path):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    user_app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={"module_ai_agent_min_role": "user"})
    response = user_app.test_client().get("/api/ai-agent/audit-status")

    payload = response.get_json()
    assert response.status_code == 403
    assert payload["ok"] is False
    assert "最高管理者" in payload["msg"]


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


def test_ai_agent_chat_session_key_is_bound_to_login_session_token(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={
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

    client = app.test_client()
    client.set_cookie("session_token", "cookie-session")
    payload = {"session_id": "shared-session", "messages": [{"role": "user", "content": "查一下任務"}]}
    response = client.post("/api/ai-agent/chat", json=payload)

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["actor"] == "userA"
    assert calls[0]["session_key"] == "hackme:2:%s:shared-session" % hashlib.sha256("cookie-session".encode()).hexdigest()[:16]


def test_ai_agent_chat_rejects_actor_without_id(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"username": "userA", "role": "user"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
    })

    def fake_chat(*_args, **_kwargs):
        raise RuntimeError("should not be called")

    monkeypatch.setattr("routes.ai_agent.ai_agent_chat", fake_chat)

    response = app.test_client().post("/api/ai-agent/chat", json={
        "session_id": "missing-id",
        "messages": [{"role": "user", "content": "查一下任務"}],
    })
    payload = response.get_json()

    assert response.status_code == 401
    assert payload["ok"] is False
    assert "無法辨識使用者身份" in payload["msg"]


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


def test_ai_agent_chat_uses_validation_http_status(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_agent_routes.db"
    _build_db(db_path)
    app = _build_app(db_path, {"id": 2, "username": "userA", "role": "user"}, settings={
        "module_ai_agent_min_role": "user",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
    })

    def fake_chat(settings, *, messages=None, prompt="", image_data_url="", model="", session_key="", actor=None):
        raise AiAgentError("訊息內容超過上限 20000 字", http_status=413)

    monkeypatch.setattr("routes.ai_agent.ai_agent_chat", fake_chat)

    response = app.test_client().post("/api/ai-agent/chat", json={
        "session_id": "long-prompt-test",
        "messages": [{"role": "user", "content": "x" * 20001}],
    })
    payload = response.get_json()

    assert response.status_code == 413
    assert payload["ok"] is False
    assert "訊息內容超過上限" in payload["msg"]


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
