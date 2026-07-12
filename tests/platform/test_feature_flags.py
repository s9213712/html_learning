import json
import sqlite3
import pytest
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, make_response

from routes.files import register_file_routes
from routes.system_admin import register_system_admin_routes
from services.platform import settings
from services.server.request_guards import (
    enforce_feature_flags,
    enforce_required_password_change,
    feature_gate_for_path,
    should_require_password_change_flag,
)
from services.users.member_levels import ensure_member_level_rules_schema
from services.security.upload_security import ensure_upload_security_schema


ROOT = Path(__file__).resolve().parents[2]


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _passthrough(fn):
    return fn


def test_feature_settings_roundtrip_and_ignore_unknown_keys(tmp_path):
    db_path = tmp_path / "settings.db"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    original_state = dict(settings._STATE)
    original_cache = settings._SYSTEM_SETTINGS
    try:
        settings.configure_settings_service(get_db=get_db, load_json=lambda path: {}, base_dir=str(tmp_path))
        conn = get_db()
        settings.init_system_settings_table(conn)
        settings._seed_missing_settings_to_db(conn)
        conn.commit()
        conn.close()
        settings.refresh_system_settings()

        updates = settings.save_feature_settings({
            "feature_chat_enabled": False,
            "feature_reports_notifications_enabled": True,
            "maintenance_mode": True,
            "unknown": True,
        })
        features = settings.get_feature_settings()

        assert updates == {"feature_chat_enabled": False, "feature_reports_notifications_enabled": True}
        assert features["feature_chat_enabled"] is False
        assert features["feature_reports_notifications_enabled"] is True
        assert settings.get_system_settings()["maintenance_mode"] is False
    finally:
        settings._STATE.clear()
        settings._STATE.update(original_state)
        settings._SYSTEM_SETTINGS = original_cache


def test_system_settings_reload_cross_worker_db_updates(tmp_path):
    db_path = tmp_path / "settings.db"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    original_state = dict(settings._STATE)
    original_cache = settings._SYSTEM_SETTINGS
    try:
        settings.configure_settings_service(get_db=get_db, load_json=lambda path: {}, base_dir=str(tmp_path))
        conn = get_db()
        settings.init_system_settings_table(conn)
        settings._seed_missing_settings_to_db(conn)
        conn.commit()
        conn.close()
        settings.refresh_system_settings()
        assert settings.get_system_settings()["feature_points_chain_enabled"] is True

        other_worker_conn = get_db()
        other_worker_conn.execute(
            """
            UPDATE system_settings
               SET value = 'False', updated_at = '2026-05-23T00:00:00', updated_by = 'other_worker'
             WHERE key = 'feature_points_chain_enabled'
            """
        )
        other_worker_conn.commit()
        other_worker_conn.close()

        assert settings._SYSTEM_SETTINGS["feature_points_chain_enabled"] is True
        assert settings.get_system_settings()["feature_points_chain_enabled"] is False
        assert settings.get_cached_system_setting("feature_points_chain_enabled", True) is False
        assert settings.is_feature_enabled("points_chain") is False
    finally:
        settings._STATE.clear()
        settings._STATE.update(original_state)
        settings._SYSTEM_SETTINGS = original_cache


def test_feature_gate_maps_existing_modules():
    assert feature_gate_for_path("/api/chat/rooms") == "feature_chat_enabled"
    assert feature_gate_for_path("/api/community/boards") == "feature_community_enabled"
    assert feature_gate_for_path("/api/admin/users") == "feature_accounts_enabled"
    assert feature_gate_for_path("/api/admin/users/7/violation") == "feature_violation_center_enabled"
    assert feature_gate_for_path("/api/admin/audit") == "feature_audit_log_enabled"
    assert feature_gate_for_path("/api/admin/message-reports") == "feature_reports_enabled"
    assert feature_gate_for_path("/api/reports") == "feature_reports_enabled"
    assert feature_gate_for_path("/api/admin/reports") == "feature_reports_enabled"
    assert feature_gate_for_path("/api/notifications") == "feature_reports_notifications_enabled"
    assert feature_gate_for_path("/api/files/upload") == "feature_privacy_uploads_enabled"
    assert feature_gate_for_path("/api/cloud-drive/upload") == "feature_privacy_uploads_enabled"
    assert feature_gate_for_path("/api/files/quota") == "feature_privacy_uploads_enabled"
    assert feature_gate_for_path("/api/crypto/init") == "feature_privacy_uploads_enabled"
    assert feature_gate_for_path("/api/comfyui/generate") == "feature_comfyui_enabled"
    assert feature_gate_for_path("/api/ai-agent/chat") == "feature_ai_agent_enabled"


def test_external_chain_features_remain_disabled_by_rc1_scope():
    scope = (ROOT / "docs" / "archive" / "pointschain_rc1" / "RC1_SCOPE.md").read_text(encoding="utf-8")
    backlog = (ROOT / "docs" / "BLOCKCHAIN" / "POST_RC1_BACKLOG.md").read_text(encoding="utf-8")
    matrix = (ROOT / "docs" / "BLOCKCHAIN" / "POINTSCHAIN_REAL_INCIDENT_GAP_MATRIX.md").read_text(encoding="utf-8")
    combined = f"{scope}\n{backlog}\n{matrix}".lower()

    assert "not an external-chain bridge" in scope.lower()
    assert "external chain bridge" in combined
    assert "depeg" in combined
    assert "no bridge in rc1" in combined or "keep disabled" in combined
    assert feature_gate_for_path("/api/points/wallet") == "feature_economy_enabled"
    assert feature_gate_for_path("/api/admin/points/ledger") == "feature_economy_enabled"
    assert feature_gate_for_path("/api/root/points/chain/verify") == "feature_economy_enabled"
    assert feature_gate_for_path("/api/games/chess/practice") == "feature_games_enabled"
    assert feature_gate_for_path("/api/root/games/chess/weekly-rewards/award") == "feature_games_enabled"
    assert feature_gate_for_path("/api/admin/settings") is None


def test_internal_test_token_scope_blocks_enabled_feature_outside_scope():
    events = []
    request_obj = SimpleNamespace(method="GET", path="/api/files/quota", cookies={"session_token": "scoped"})

    response = enforce_feature_flags(
        request_obj,
        feature_gate_for_path_func=lambda path: "feature_privacy_uploads_enabled",
        is_feature_enabled=lambda key: True,
        get_current_user_ctx=lambda: {
            "username": "test",
            "auth_scope": "internal_test_token",
            "allowed_features": ["feature_chat_enabled"],
        },
        record_security_event=lambda *args, **kwargs: events.append((args, kwargs)),
        get_client_ip=lambda: "127.0.0.1",
        build_feature_disabled_payload=lambda feature: {"feature": feature},
        json_resp=lambda payload, status=200: payload,
    )

    assert response is not None
    payload, status = response
    assert status == 403
    assert payload["feature"] == "feature_privacy_uploads_enabled"
    assert events[0][0][0] == "permission_denied"


def test_internal_test_token_scope_allows_enabled_feature_inside_scope():
    request_obj = SimpleNamespace(method="GET", path="/api/chat/rooms", cookies={"session_token": "scoped"})

    response = enforce_feature_flags(
        request_obj,
        feature_gate_for_path_func=lambda path: "feature_chat_enabled",
        is_feature_enabled=lambda key: True,
        get_current_user_ctx=lambda: {
            "username": "test",
            "auth_scope": "internal_test_token",
            "allowed_features": ["feature_chat_enabled"],
        },
        record_security_event=lambda *args, **kwargs: None,
        get_client_ip=lambda: "127.0.0.1",
        build_feature_disabled_payload=lambda feature: {"feature": feature},
        json_resp=lambda payload, status=200: payload,
    )

    assert response is None


def test_default_password_change_guard_can_be_disabled_for_isolated_runtime(monkeypatch):
    request_obj = SimpleNamespace(method="GET", path="/api/points/wallet")

    blocked = enforce_required_password_change(
        request_obj,
        get_current_user_ctx=lambda: {"id": 1, "must_change_password": 1},
        json_resp=lambda payload, status=200: (payload, status),
    )
    assert blocked is not None
    assert blocked[1] == 403

    monkeypatch.setenv("HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_POLICY", "1")
    allowed = enforce_required_password_change(
        request_obj,
        get_current_user_ctx=lambda: {"id": 1, "must_change_password": 1},
        json_resp=lambda payload, status=200: (payload, status),
    )
    assert allowed is None
    assert should_require_password_change_flag(1) is False

    monkeypatch.delenv("HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_POLICY", raising=False)
    monkeypatch.setenv("HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS", "1")
    monkeypatch.setenv("HACKME_DEV_SECURITY_ENABLED", "0")
    dev_allowed = enforce_required_password_change(
        request_obj,
        get_current_user_ctx=lambda: {"id": 1, "must_change_password": 1},
        json_resp=lambda payload, status=200: (payload, status),
    )
    assert dev_allowed is None
    assert should_require_password_change_flag(1) is False

    monkeypatch.setenv("HACKME_DEV_SECURITY_ENABLED", "1")
    dev_security_blocked = enforce_required_password_change(
        request_obj,
        get_current_user_ctx=lambda: {"id": 1, "must_change_password": 1},
        json_resp=lambda payload, status=200: (payload, status),
    )
    assert dev_security_blocked is not None
    assert dev_security_blocked[1] == 403
    assert should_require_password_change_flag(1) is True

    monkeypatch.delenv("HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS", raising=False)
    monkeypatch.delenv("HACKME_DEV_SECURITY_ENABLED", raising=False)
    monkeypatch.setenv("HTML_LEARNING_ALLOW_DEFAULT_PASSWORDS", "1")
    monkeypatch.setenv("HTML_LEARNING_SERVER_MODE", "dev_ready")
    html_alias_allowed = enforce_required_password_change(
        request_obj,
        get_current_user_ctx=lambda: {"id": 1, "must_change_password": 1},
        json_resp=lambda payload, status=200: (payload, status),
    )
    assert html_alias_allowed is None
    assert should_require_password_change_flag(1) is False

    monkeypatch.setenv("HTML_LEARNING_SERVER_MODE", "production")
    production_alias_blocked = enforce_required_password_change(
        request_obj,
        get_current_user_ctx=lambda: {"id": 1, "must_change_password": 1},
        json_resp=lambda payload, status=200: (payload, status),
    )
    assert production_alias_blocked is not None
    assert production_alias_blocked[1] == 403


def test_isolated_runtime_builtin_default_passwords_disable_forced_change(monkeypatch):
    request_obj = SimpleNamespace(method="GET", path="/api/points/wallet")
    monkeypatch.setenv("HTML_LEARNING_ROOT_PASSWORD", "root")
    monkeypatch.setenv("HTML_LEARNING_MANAGER_PASSWORD", "admin")
    monkeypatch.setenv("HTML_LEARNING_TEST_PASSWORD", "test")
    monkeypatch.setenv("HTML_LEARNING_SERVER_MODE", "dev_ready")
    monkeypatch.setenv("HACKME_RUNTIME_DIR", "/tmp/hackme_web_isolated_54344/hackme_web/runtime")
    monkeypatch.delenv("HTML_LEARNING_ALLOW_DEFAULT_PASSWORDS", raising=False)
    monkeypatch.delenv("HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS", raising=False)
    monkeypatch.delenv("HACKME_DEV_SECURITY_ENABLED", raising=False)
    monkeypatch.delenv("HTML_LEARNING_SECURITY_ENABLED", raising=False)

    allowed = enforce_required_password_change(
        request_obj,
        get_current_user_ctx=lambda: {"id": 1, "must_change_password": 1},
        json_resp=lambda payload, status=200: (payload, status),
    )

    assert allowed is None
    assert should_require_password_change_flag(1) is False


def test_non_isolated_builtin_default_passwords_still_require_explicit_dev_allow(monkeypatch):
    request_obj = SimpleNamespace(method="GET", path="/api/points/wallet")
    monkeypatch.setenv("HTML_LEARNING_ROOT_PASSWORD", "root")
    monkeypatch.setenv("HTML_LEARNING_MANAGER_PASSWORD", "admin")
    monkeypatch.setenv("HTML_LEARNING_TEST_PASSWORD", "test")
    monkeypatch.setenv("HTML_LEARNING_SERVER_MODE", "dev_ready")
    monkeypatch.setenv("HACKME_RUNTIME_DIR", "/tmp/not_isolated_hackme/runtime")
    monkeypatch.delenv("HTML_LEARNING_ALLOW_DEFAULT_PASSWORDS", raising=False)
    monkeypatch.delenv("HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS", raising=False)
    monkeypatch.delenv("HACKME_DEV_SECURITY_ENABLED", raising=False)
    monkeypatch.delenv("HTML_LEARNING_SECURITY_ENABLED", raising=False)

    blocked = enforce_required_password_change(
        request_obj,
        get_current_user_ctx=lambda: {"id": 1, "must_change_password": 1},
        json_resp=lambda payload, status=200: (payload, status),
    )

    assert blocked is not None
    assert blocked[1] == 403


def test_notifications_remain_enabled_after_management_only_reset():
    assert settings.DEFAULT_SETTINGS["feature_reports_notifications_enabled"] is True
    assert settings.MANAGEMENT_ONLY_RESET_SETTINGS["feature_reports_notifications_enabled"] is True


def test_feature_disabled_payload_names_missing_parent_feature(tmp_path):
    db_path = tmp_path / "settings.db"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    original_state = dict(settings._STATE)
    original_cache = settings._SYSTEM_SETTINGS
    try:
        settings.configure_settings_service(get_db=get_db, load_json=lambda path: {}, base_dir=".")
        conn = get_db()
        settings.init_system_settings_table(conn)
        settings._seed_missing_settings_to_db(conn)
        conn.execute("UPDATE system_settings SET value = 'False' WHERE key = 'feature_trading_enabled'")
        conn.execute("UPDATE system_settings SET value = 'False' WHERE key = 'feature_economy_enabled'")
        conn.commit()
        conn.close()
        payload = settings.build_feature_disabled_payload("feature_trading_enabled")
        assert payload["feature"] == "feature_trading_enabled"
        assert payload["feature_label"] == "積分交易所"
        assert payload["missing_required"] == ["feature_economy_enabled"]
        assert "基本積分系統" in payload["msg"]
    finally:
        settings._STATE.clear()
        settings._STATE.update(original_state)
        settings._SYSTEM_SETTINGS = original_cache


def test_feature_disabled_payload_lists_enabled_dependents_of_blocked_parent(tmp_path):
    db_path = tmp_path / "settings.db"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    original_state = dict(settings._STATE)
    original_cache = settings._SYSTEM_SETTINGS
    try:
        settings.configure_settings_service(get_db=get_db, load_json=lambda path: {}, base_dir=".")
        conn = get_db()
        settings.init_system_settings_table(conn)
        settings._seed_missing_settings_to_db(conn)
        conn.execute("UPDATE system_settings SET value = 'False' WHERE key = 'feature_privacy_uploads_enabled'")
        conn.execute("UPDATE system_settings SET value = 'True' WHERE key = 'feature_storage_albums_enabled'")
        conn.execute("UPDATE system_settings SET value = 'True' WHERE key = 'feature_videos_enabled'")
        conn.execute("UPDATE system_settings SET value = 'True' WHERE key = 'feature_comfyui_enabled'")
        conn.commit()
        conn.close()
        payload = settings.build_feature_disabled_payload("feature_privacy_uploads_enabled")
        assert payload["feature_label"] == "隱私分級上傳 / E2EE"
        assert payload["enabled_dependents_required"] == ["feature_storage_albums_enabled"]
        assert "Storage / 相簿" in payload["msg"]
        assert "影音分享" in payload["msg"] or "ComfyUI AI 產圖" in payload["msg"]
    finally:
        settings._STATE.clear()
        settings._STATE.update(original_state)
        settings._SYSTEM_SETTINGS = original_cache


def test_save_feature_settings_rejects_required_child_without_parent(tmp_path):
    db_path = tmp_path / "settings.db"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    original_state = dict(settings._STATE)
    original_cache = settings._SYSTEM_SETTINGS
    try:
        settings.configure_settings_service(get_db=get_db, load_json=lambda path: {}, base_dir=str(tmp_path))
        conn = get_db()
        settings.init_system_settings_table(conn)
        settings._seed_missing_settings_to_db(conn)
        conn.commit()
        conn.close()
        settings.refresh_system_settings()

        with pytest.raises(ValueError, match="feature_trading_enabled requires feature_economy_enabled"):
            settings.save_feature_settings({
                "feature_economy_enabled": False,
                "feature_trading_enabled": True,
            })
    finally:
        settings._STATE.clear()
        settings._STATE.update(original_state)
        settings._SYSTEM_SETTINGS = original_cache


def test_admin_feature_routes_reject_required_child_without_parent():
    app = Flask(__name__)
    app.testing = True
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    feature_state = {
        **settings.DEFAULT_SETTINGS,
        "feature_economy_enabled": False,
        "feature_trading_enabled": False,
        "feature_privacy_uploads_enabled": False,
        "feature_storage_albums_enabled": False,
    }

    def save_feature_settings(data):
        feature_state.update(data)
        return dict(data)

    register_system_admin_routes(app, {
        "ANCHOR_DIR": ".",
        "BASE_DIR": ".",
        "CERT_FILE": "missing-cert.pem",
        "CHAT_DIR": ".",
        "CURRENT_SERVER_BIND_STATE": {"host": "0.0.0.0", "port": 5000, "ssl_enabled": False},
        "DB_PATH": "missing.db",
        "KEY_FILE": "missing-key.pem",
        "LOG_DIR": ".",
        "SERVER_LOG_PATH": "server.log",
        "activate_emergency_lockdown": lambda reason: None,
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": lambda: None,
        "get_feature_settings": lambda: {key: feature_state[key] for key in settings.FEATURE_FLAG_KEYS},
        "get_system_settings": lambda: dict(feature_state),
        "get_ua": lambda: "pytest",
        "is_audit_chain_enabled": lambda: False,
        "json_resp": _json_resp,
        "repair_audit_chain": lambda **kwargs: {"entries_resealed": 0},
        "repair_violation_chains": lambda: {"entries_resealed": 0},
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "save_feature_settings": save_feature_settings,
        "save_settings": lambda data: dict(data),
        "server_mode_service": None,
        "snapshot_service": None,
        "verify_audit_integrity": lambda: (True, None, "ok"),
    })
    client = app.test_client()

    trading_res = client.put("/api/admin/features", json={"feature_economy_enabled": False, "feature_trading_enabled": True})
    storage_res = client.put("/api/admin/features", json={"feature_privacy_uploads_enabled": False, "feature_storage_albums_enabled": True})

    assert trading_res.status_code == 400
    assert "積分交易所 需要先啟用" in trading_res.get_json()["msg"]
    assert storage_res.status_code == 400
    assert "Storage / 相簿 需要先啟用" in storage_res.get_json()["msg"]


def test_file_quota_endpoint_returns_user_usage(tmp_path):
    db_path = tmp_path / "files.db"
    app = Flask(__name__)
    app.testing = True
    actor = {
        "id": 1,
        "username": "alice",
        "role": "user",
        "member_level": "trusted",
        "effective_level": "trusted",
        "sanction_status": "none",
    }

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    conn = get_db()
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'alice')")
    ensure_member_level_rules_schema(conn)
    ensure_upload_security_schema(conn)
    conn.execute(
        "INSERT INTO uploaded_files (id, owner_user_id, storage_path, privacy_mode, risk_level, scan_status, size_bytes, created_at) "
        "VALUES ('f1', 1, 'storage/f1', 'standard_plain', 'low', 'clean', 1024, '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    register_file_routes(app, {
        "get_current_user_ctx": lambda: actor,
        "get_db": get_db,
        "get_member_level_rule": lambda conn, level: {
            "can_upload_attachment": True,
            "attachment_quota_mb": 2,
            "max_attachment_size_mb": 1,
            "upload_rate_limit_per_day": 10,
        },
        "json_resp": _json_resp,
        "require_csrf_safe": _passthrough,
    })
    client = app.test_client()
    res = client.get("/api/files/quota")
    data = res.get_json()
    assert res.status_code == 200
    assert data["quota"]["used_bytes"] == 1024
    assert data["quota"]["remaining_bytes"] == 2 * 1024 * 1024 - 1024


def test_admin_features_endpoint_is_root_only():
    app = Flask(__name__)
    app.testing = True
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    feature_state = {"feature_chat_enabled": True, "feature_reports_notifications_enabled": False}
    audit_log = []

    def save_feature_settings(data):
        updates = {k: bool(v) for k, v in data.items() if k in feature_state}
        feature_state.update(updates)
        return updates

    register_system_admin_routes(app, {
        "ANCHOR_DIR": ".",
        "BASE_DIR": ".",
        "CHAT_DIR": ".",
        "DB_PATH": "missing.db",
        "LOG_DIR": ".",
        "SERVER_LOG_PATH": "server.log",
        "activate_emergency_lockdown": lambda reason: None,
        "audit": lambda *args, **kwargs: audit_log.append((args, kwargs)),
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": lambda: None,
        "get_feature_settings": lambda: dict(feature_state),
        "get_system_settings": lambda: dict(feature_state),
        "is_audit_chain_enabled": lambda: False,
        "json_resp": _json_resp,
        "repair_audit_chain": lambda **kwargs: {"entries_resealed": 0},
        "repair_violation_chains": lambda: {"entries_resealed": 0},
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "save_feature_settings": save_feature_settings,
        "save_settings": lambda data: data,
        "verify_audit_integrity": lambda: (True, None, "ok"),
    })
    client = app.test_client()

    res = client.get("/api/admin/features")
    assert res.status_code == 200
    assert res.get_json()["features"] == feature_state

    res = client.put("/api/admin/features", json={"feature_chat_enabled": False, "maintenance_mode": True})
    assert res.status_code == 200
    assert res.get_json()["features"] == {"feature_chat_enabled": False}
    feature_audit = next(call for call in audit_log if call[0][0] == "FEATURE_FLAGS_CHANGED")
    detail = json.loads(feature_audit[1]["detail"])
    assert detail["scope"] == "feature_flags"
    assert detail["changes"] == [{
        "key": "feature_chat_enabled",
        "old": True,
        "new": False,
        "changed": True,
    }]

    actor_box["actor"] = {"id": 2, "username": "admin", "role": "manager"}
    res = client.get("/api/admin/features")
    assert res.status_code == 403


def test_admin_settings_validates_cloud_drive_storage_root(tmp_path):
    app = Flask(__name__)
    app.testing = True
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    state = {"server_listen_host": "", "server_listen_port": 0, "cloud_drive_storage_root": "", "cloud_drive_global_capacity_limit_mb": -1}
    audit_log = []
    storage_dir = tmp_path / "current-storage"
    storage_dir.mkdir()

    def save_settings(data):
        state.update(data)
        return dict(data)

    register_system_admin_routes(app, {
        "ANCHOR_DIR": str(tmp_path),
        "BASE_DIR": str(tmp_path),
        "CHAT_DIR": str(tmp_path),
        "DB_PATH": str(tmp_path / "missing.db"),
        "LOG_DIR": str(tmp_path),
        "SERVER_LOG_PATH": str(tmp_path / "server.log"),
        "STORAGE_DIR": str(storage_dir),
        "CURRENT_SERVER_BIND_STATE": {"host": "127.0.0.1", "port": 5000},
        "activate_emergency_lockdown": lambda reason: None,
        "audit": lambda *args, **kwargs: audit_log.append((args, kwargs)),
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": lambda: None,
        "get_feature_settings": lambda: {},
        "get_system_settings": lambda: dict(state),
        "is_audit_chain_enabled": lambda: False,
        "json_resp": _json_resp,
        "repair_audit_chain": lambda **kwargs: {"entries_resealed": 0},
        "repair_violation_chains": lambda: {"entries_resealed": 0},
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "save_feature_settings": lambda data: {},
        "save_settings": save_settings,
        "verify_audit_integrity": lambda: (True, None, "ok"),
    })
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"cloud_drive_storage_root": "/"})
    assert res.status_code == 400
    assert "cloud_drive_storage_root" in res.get_json()["msg"]

    res = client.put("/api/admin/settings", json={"cloud_drive_global_capacity_limit_mb": -2})
    assert res.status_code == 400
    assert "cloud_drive_global_capacity_limit_mb" in res.get_json()["msg"]

    next_root = tmp_path / "next-storage"
    res = client.put("/api/admin/settings", json={"cloud_drive_storage_root": str(next_root), "cloud_drive_global_capacity_limit_mb": 2048})
    data = res.get_json()
    assert res.status_code == 200
    assert data["settings"]["cloud_drive_storage_root"] == str(next_root)
    assert data["settings"]["cloud_drive_global_capacity_limit_mb"] == 2048
    assert data["cloud_drive_storage"]["global_capacity"]["configured_limit_mb"] == 2048
    assert data["cloud_drive_storage"]["restart_required"] is True
    settings_audit = next(call for call in audit_log if call[0][0] == "SETTINGS_CHANGED")
    detail = json.loads(settings_audit[1]["detail"])
    assert detail["scope"] == "system_settings"
    changes = {item["key"]: item for item in detail["changes"]}
    assert changes["cloud_drive_storage_root"]["old"] == ""
    assert changes["cloud_drive_storage_root"]["new"] == str(next_root)
    assert changes["cloud_drive_global_capacity_limit_mb"]["old"] == -1
    assert changes["cloud_drive_global_capacity_limit_mb"]["new"] == 2048


def test_admin_cloud_drive_security_policy_endpoint_is_root_only(tmp_path):
    db_path = tmp_path / "cloud-policy.db"
    app = Flask(__name__)
    app.testing = True
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    register_system_admin_routes(app, {
        "ANCHOR_DIR": str(tmp_path),
        "BASE_DIR": str(tmp_path),
        "CHAT_DIR": str(tmp_path),
        "DB_PATH": str(db_path),
        "LOG_DIR": str(tmp_path),
        "SERVER_LOG_PATH": str(tmp_path / "server.log"),
        "STORAGE_DIR": str(tmp_path / "storage"),
        "activate_emergency_lockdown": lambda reason: None,
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": get_db,
        "get_feature_settings": lambda: {},
        "get_system_settings": lambda: {},
        "is_audit_chain_enabled": lambda: False,
        "json_resp": _json_resp,
        "repair_audit_chain": lambda **kwargs: {"entries_resealed": 0},
        "repair_violation_chains": lambda: {"entries_resealed": 0},
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "save_feature_settings": lambda data: {},
        "save_settings": lambda data: data,
        "verify_audit_integrity": lambda: (True, None, "ok"),
    })
    client = app.test_client()

    res = client.get("/api/admin/cloud-drive/security-policy")
    assert res.status_code == 200
    assert res.get_json()["policy"]["block_unclean_downloads"] is True

    res = client.put("/api/admin/cloud-drive/security-policy", json={"block_unclean_downloads": False, "max_daily_downloads": 8})
    assert res.status_code == 200
    assert res.get_json()["policy"]["block_unclean_downloads"] is False
    assert res.get_json()["policy"]["max_daily_downloads"] == 8

    actor_box["actor"] = {"id": 2, "username": "admin", "role": "manager"}
    res = client.get("/api/admin/cloud-drive/security-policy")
    assert res.status_code == 403


def test_member_level_rules_summary_is_manager_readable_but_updates_are_root_only(tmp_path):
    db_path = tmp_path / "rules.db"
    app = Flask(__name__)
    app.testing = True
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    register_system_admin_routes(app, {
        "ANCHOR_DIR": ".",
        "BASE_DIR": ".",
        "CHAT_DIR": ".",
        "DB_PATH": str(db_path),
        "LOG_DIR": ".",
        "SERVER_LOG_PATH": "server.log",
        "activate_emergency_lockdown": lambda reason: None,
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": get_db,
        "get_feature_settings": lambda: {},
        "get_system_settings": lambda: {},
        "is_audit_chain_enabled": lambda: False,
        "json_resp": _json_resp,
        "repair_audit_chain": lambda **kwargs: {"entries_resealed": 0},
        "repair_violation_chains": lambda: {"entries_resealed": 0},
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "save_feature_settings": lambda data: {},
        "save_settings": lambda data: data,
        "verify_audit_integrity": lambda: (True, None, "ok"),
    })
    client = app.test_client()

    res = client.get("/api/admin/member-level-rules")
    assert res.status_code == 200
    assert any(rule["level"] == "normal" for rule in res.get_json()["rules"])

    res = client.put("/api/admin/member-level-rules/normal", json={"can_post": False, "daily_post_limit": 4})
    assert res.status_code == 200
    assert res.get_json()["rule"]["can_post"] is False
    assert res.get_json()["rule"]["daily_post_limit"] == 4

    actor_box["actor"] = {"id": 2, "username": "admin", "role": "manager"}
    res = client.get("/api/admin/member-level-rules")
    assert res.status_code == 200
    assert any(rule["level"] == "normal" for rule in res.get_json()["rules"])

    res = client.put("/api/admin/member-level-rules/normal", json={"can_post": True})
    assert res.status_code == 403
