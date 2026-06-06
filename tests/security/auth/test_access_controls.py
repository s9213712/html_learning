import json

from flask import Flask, jsonify, make_response, request

from routes.system_admin import register_system_admin_routes
from services.security.access_controls import (
    access_control_settings_payload,
    client_ip_allowed,
    hash_maintenance_bypass_token,
    is_browser_user_agent,
    maintenance_bypass_expires_at,
    maintenance_bypass_required_payload,
    maintenance_bypass_token_is_expired,
    parse_ip_whitelist,
    verify_maintenance_bypass_token,
)
from services.server.bind import (
    effective_server_bind,
    effective_server_ssl,
    server_ssl_settings_payload,
    validate_listen_host,
    validate_listen_port,
)
from services.server.request_guards import (
    get_request_maintenance_bypass_token,
    protect_sensitive_static_page,
)


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _passthrough(fn):
    return fn


def _admin_app(settings_state=None, actor=None, cert_file=None, key_file=None, current_ssl_enabled=False, audit_log=None):
    app = Flask(__name__)
    app.testing = True
    state = settings_state or {
        "root_ip_whitelist_enabled": False,
        "root_ip_whitelist": "",
        "browser_only_mode_enabled": False,
        "maintenance_bypass_token_hash": "",
        "maintenance_bypass_token_expires_at": "",
        "server_listen_host": "",
        "server_listen_port": 0,
        "server_ssl_enabled": True,
        "server_timezone": "UTC",
        "server_backpressure_traffic_refresh_seconds": 4,
        "server_output_refresh_seconds": 3,
        "security_test_job_poll_seconds": 3,
        "system_resource_board_refresh_seconds": 5,
        "job_center_refresh_seconds": 3,
        "economy_dashboard_refresh_seconds": 30,
        "trading_dashboard_refresh_seconds": 5,
        "trading_live_price_refresh_seconds": 2,
        "trading_reference_price_refresh_seconds": 1,
        "trading_reference_chart_refresh_seconds": 5,
        "comfyui_job_poll_seconds": 1,
        "notification_poll_seconds": 60,
        "game_invite_poll_active_seconds": 5,
        "game_invite_poll_idle_seconds": 60,
        "game_invite_poll_hidden_seconds": 180,
        "server_connection_monitor_seconds": 15,
        "drive_dashboard_lazy_refresh_seconds": 10,
        "comfyui_connection_mode": "remote",
        "comfyui_remote_api_url": "",
        "comfyui_base_dir": "",
        "comfyui_local_start_script": "",
        "comfyui_api_host": "localhost",
        "comfyui_api_port": 8192,
        "comfyui_max_batch_size": 1,
        "comfyui_default_width": 1024,
        "comfyui_default_height": 1024,
    }

    def save_settings(data):
        state.update(data)
        return dict(data)

    register_system_admin_routes(app, {
        "ANCHOR_DIR": ".",
        "BASE_DIR": ".",
        "CERT_FILE": str(cert_file or "missing-cert.pem"),
        "CHAT_DIR": ".",
        "CURRENT_SERVER_BIND_STATE": {"host": "0.0.0.0", "port": 5000, "ssl_enabled": current_ssl_enabled},
        "DB_PATH": "missing.db",
        "KEY_FILE": str(key_file or "missing-key.pem"),
        "LOG_DIR": ".",
        "SERVER_LOG_PATH": "server.log",
        "activate_emergency_lockdown": lambda reason: None,
        "audit": (lambda *args, **kwargs: audit_log.append((args, kwargs))) if audit_log is not None else (lambda *args, **kwargs: None),
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor or {"id": 1, "username": "root", "role": "super_admin"},
        "get_db": lambda: None,
        "get_feature_settings": lambda: {},
        "get_system_settings": lambda: dict(state),
        "get_ua": lambda: "pytest",
        "is_audit_chain_enabled": lambda: False,
        "json_resp": _json_resp,
        "repair_audit_chain": lambda **kwargs: {"entries_resealed": 0},
        "repair_violation_chains": lambda: {"entries_resealed": 0},
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "save_feature_settings": lambda data: {},
        "save_settings": save_settings,
        "server_mode_service": None,
        "snapshot_service": None,
        "verify_audit_integrity": lambda: (True, None, "ok"),
    })
    return app, state


def test_ip_whitelist_supports_exact_ips_and_cidrs():
    assert parse_ip_whitelist("127.0.0.1, 10.0.0.0/24\n::1") == ["127.0.0.1", "10.0.0.0/24", "::1"]
    assert client_ip_allowed("127.0.0.1", "127.0.0.1") is True
    assert client_ip_allowed("10.0.0.8", "10.0.0.0/24") is True
    assert client_ip_allowed("10.0.1.8", "10.0.0.0/24") is False
    assert client_ip_allowed("bad-ip", "127.0.0.1") is False


def test_browser_user_agent_detection_is_conservative():
    assert is_browser_user_agent("Mozilla/5.0 Chrome/120 Safari/537.36") is True
    assert is_browser_user_agent("curl/8.0") is False
    assert is_browser_user_agent("") is False


def test_maintenance_bypass_token_hash_roundtrip():
    stored = hash_maintenance_bypass_token("secret-token")
    assert stored
    assert verify_maintenance_bypass_token("secret-token", stored) is True
    assert verify_maintenance_bypass_token("wrong", stored) is False
    assert verify_maintenance_bypass_token("secret-token", "") is False


def test_maintenance_bypass_token_expiry_is_enforced():
    stored = hash_maintenance_bypass_token("secret-token")
    future = maintenance_bypass_expires_at(30)
    assert verify_maintenance_bypass_token("secret-token", stored, future) is True
    assert verify_maintenance_bypass_token("secret-token", stored, "2000-01-01T00:00:00+00:00") is False
    assert maintenance_bypass_token_is_expired("2000-01-01T00:00:00+00:00") is True


def test_maintenance_bypass_required_payload_names_token_header_not_hash():
    payload = maintenance_bypass_required_payload("need bypass")
    assert payload["requires"] == "maintenance_bypass_token"
    assert payload["header"] == "X-Maintenance-Bypass-Token"
    assert "hash" not in str(payload).lower()


def test_maintenance_bypass_token_only_uses_header_not_query_string():
    app = Flask(__name__)
    with app.test_request_context("/api/admin/health?maintenance_bypass_token=query-token"):
        assert get_request_maintenance_bypass_token(request) == ""
    with app.test_request_context(
        "/api/admin/health?maintenance_bypass_token=query-token",
        headers={"X-Maintenance-Bypass-Token": "header-token"},
    ):
        assert get_request_maintenance_bypass_token(request) == "header-token"


def test_comfyui_workflow_editor_static_page_requires_login():
    app = Flask(__name__)
    audit_log = []
    with app.test_request_context("/comfyui-workflow-editor.html"):
        resp = protect_sensitive_static_page(
            request,
            get_current_user_ctx=lambda: None,
            audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
            get_client_ip=lambda: "127.0.0.1",
            get_ua=lambda: "pytest",
            is_feature_enabled=lambda key: True,
            record_security_event=lambda *args, **kwargs: None,
            make_response=make_response,
        )

    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
    assert audit_log
    assert audit_log[-1][0][0] == "STATIC_PAGE_UNAUTH_DENIED"
    assert "path=/comfyui-workflow-editor.html" in audit_log[-1][1]["detail"]


def test_comfyui_workflow_editor_static_page_respects_feature_flag():
    app = Flask(__name__)
    security_events = []
    with app.test_request_context("/comfyui-workflow-editor.html"):
        resp = protect_sensitive_static_page(
            request,
            get_current_user_ctx=lambda: {"id": 1, "username": "root"},
            audit=lambda *args, **kwargs: None,
            get_client_ip=lambda: "127.0.0.1",
            get_ua=lambda: "pytest",
            is_feature_enabled=lambda key: False,
            record_security_event=lambda *args, **kwargs: security_events.append((args, kwargs)),
            make_response=make_response,
        )

    assert resp.status_code == 503
    assert b"ComfyUI workflow editor is disabled" in resp.data
    assert security_events
    assert "feature_comfyui_enabled" in security_events[-1][1]["detail"]


def test_admin_access_controls_endpoint_updates_safe_payload():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/access-controls", json={
        "root_ip_whitelist_enabled": True,
        "root_ip_whitelist": "127.0.0.1,10.0.0.0/24",
        "browser_only_mode_enabled": True,
        "maintenance_bypass_token_hash": "should-not-be-accepted-directly",
    })
    data = res.get_json()
    assert res.status_code == 200
    assert data["access_controls"]["root_ip_whitelist_enabled"] is True
    assert data["access_controls"]["root_ip_whitelist"] == "127.0.0.1,10.0.0.0/24"
    assert data["access_controls"]["browser_only_mode_enabled"] is True
    assert state["maintenance_bypass_token_hash"] == ""


def test_admin_access_controls_reject_invalid_root_ip_whitelist_entries():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/access-controls", json={
        "root_ip_whitelist_enabled": True,
        "root_ip_whitelist": "127.0.0.1,javascript:alert(1),999.999.999.999",
    })

    assert res.status_code == 400
    assert "無效的 IP / CIDR" in res.get_json()["msg"]
    assert state["root_ip_whitelist"] == ""
    assert state["root_ip_whitelist_enabled"] is False


def test_admin_access_controls_reject_enabling_empty_root_ip_whitelist():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/access-controls", json={
        "root_ip_whitelist_enabled": True,
        "root_ip_whitelist": "",
    })

    assert res.status_code == 400
    assert "至少要填入一個有效的 IP 或 CIDR" in res.get_json()["msg"]
    assert state["root_ip_whitelist_enabled"] is False


def test_admin_rotates_maintenance_bypass_token_once():
    audit_log = []
    app, state = _admin_app(audit_log=audit_log)
    client = app.test_client()
    res = client.post("/api/admin/access-controls/maintenance-bypass-token", json={"confirm": "ROTATE", "ttl_minutes": 15})
    data = res.get_json()
    assert res.status_code == 200
    assert data["token"]
    assert data["ttl_minutes"] == 15
    assert data["expires_at"]
    assert data["access_controls"]["maintenance_bypass_token_configured"] is True
    assert data["access_controls"]["maintenance_bypass_token_expires_at"] == state["maintenance_bypass_token_expires_at"]
    assert verify_maintenance_bypass_token(data["token"], state["maintenance_bypass_token_hash"], state["maintenance_bypass_token_expires_at"]) is True
    assert "maintenance_bypass_token_hash" not in data["access_controls"]
    event = next(call for call in audit_log if call[0][0] == "MAINTENANCE_BYPASS_TOKEN_ROTATED")
    detail = json.loads(event[1]["detail"])
    changes = {row["key"]: row for row in detail["changes"]}
    assert changes["maintenance_bypass_token_hash"]["old"] == ""
    assert changes["maintenance_bypass_token_hash"]["new"] == "<redacted>"
    assert data["token"] not in event[1]["detail"]


def test_access_controls_are_root_only():
    app, _ = _admin_app(actor={"id": 2, "username": "admin", "role": "manager"})
    res = app.test_client().get("/api/admin/access-controls")
    assert res.status_code == 403


def test_access_control_settings_payload_never_exposes_token_hash():
    payload = access_control_settings_payload({"maintenance_bypass_token_hash": "hash"})
    assert payload["maintenance_bypass_token_configured"] is True
    assert "maintenance_bypass_token_hash" not in payload


def test_server_bind_validation_accepts_ips_and_rejects_unsafe_values():
    assert validate_listen_host("127.0.0.1") == "127.0.0.1"
    assert validate_listen_host("::1") == "::1"
    assert validate_listen_host("localhost") == "localhost"
    assert validate_listen_host("0.0.0.0/0") is None
    assert validate_listen_host("127.0.0.1,0.0.0.0") is None
    assert validate_listen_port("8080") == 8080
    assert validate_listen_port("0") == 0
    assert validate_listen_port("70000") is None


def test_root_can_configure_server_bind_settings_with_restart_hint():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={
        "server_listen_host": "127.0.0.1",
        "server_listen_port": 8081,
    })
    data = res.get_json()

    assert res.status_code == 200
    assert state["server_listen_host"] == "127.0.0.1"
    assert state["server_listen_port"] == 8081
    assert data["server_bind"]["host"] == "127.0.0.1"
    assert data["server_bind"]["port"] == 8081
    assert data["server_bind"]["restart_required"] is True


def test_server_ssl_settings_require_root_setting_and_cert_files():
    enabled = effective_server_ssl({"server_ssl_enabled": True}, cert_exists=True)
    disabled_by_setting = effective_server_ssl({"server_ssl_enabled": False}, cert_exists=True)
    missing_cert = effective_server_ssl({"server_ssl_enabled": True}, cert_exists=False)
    restart = server_ssl_settings_payload(
        {"server_ssl_enabled": True},
        current_ssl_enabled=False,
        cert_exists=True,
    )

    assert enabled["enabled"] is True
    assert enabled["scheme"] == "https"
    assert disabled_by_setting["enabled"] is False
    assert disabled_by_setting["scheme"] == "http"
    assert missing_cert["enabled"] is False
    assert missing_cert["cert_required"] is True
    assert restart["restart_required"] is True


def test_root_can_configure_server_ssl_setting_with_restart_hint(tmp_path):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert", encoding="utf-8")
    key_file.write_text("key", encoding="utf-8")
    app, state = _admin_app(cert_file=cert_file, key_file=key_file, current_ssl_enabled=False)
    client = app.test_client()

    initial = client.get("/api/admin/settings").get_json()
    assert initial["server_ssl"]["enabled"] is True
    assert initial["server_ssl"]["restart_required"] is True

    # server_ssl_enabled is a dangerous setting on the disable side; the
    # admin route should reject the PUT until the operator opts in via
    # ``dangerous_confirm`` (P1 settings hardening).
    blocked = client.put("/api/admin/settings", json={"server_ssl_enabled": False})
    assert blocked.status_code == 400
    blocked_payload = blocked.get_json()
    assert blocked_payload.get("error") == "dangerous_change_blocked"
    assert state["server_ssl_enabled"] is True

    res = client.put(
        "/api/admin/settings",
        json={"server_ssl_enabled": False, "dangerous_confirm": "server_ssl_enabled"},
    )
    data = res.get_json()

    assert res.status_code == 200
    assert state["server_ssl_enabled"] is False
    assert data["server_ssl"]["enabled"] is False
    assert data["server_ssl"]["enabled_by_setting"] is False
    assert data["server_ssl"]["current_enabled"] is False


def test_invalid_server_bind_settings_are_rejected():
    app, state = _admin_app()
    client = app.test_client()

    bad_host = client.put("/api/admin/settings", json={"server_listen_host": "0.0.0.0/0"})
    bad_port = client.put("/api/admin/settings", json={"server_listen_port": 70000})

    assert bad_host.status_code == 400
    assert bad_port.status_code == 400
    assert state["server_listen_host"] == ""
    assert state["server_listen_port"] == 0


def test_root_can_configure_server_timezone_and_get_time_payload():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"server_timezone": "Asia/Taipei"})
    data = res.get_json()

    assert res.status_code == 200
    assert state["server_timezone"] == "Asia/Taipei"
    assert data["settings"]["server_timezone"] == "Asia/Taipei"
    assert data["server_time"]["timezone"] == "Asia/Taipei"
    assert data["server_time"]["utc_offset_label"] == "UTC+08:00"

    bad = client.put("/api/admin/settings", json={"server_timezone": "Mars/Base"})

    assert bad.status_code == 400
    assert state["server_timezone"] == "Asia/Taipei"


def test_root_can_configure_system_resource_board_refresh_seconds():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"system_resource_board_refresh_seconds": 7})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert state["system_resource_board_refresh_seconds"] == 7
    assert data["settings"]["system_resource_board_refresh_seconds"] == 7

    bad = client.put("/api/admin/settings", json={"system_resource_board_refresh_seconds": 0})
    assert bad.status_code == 400
    assert state["system_resource_board_refresh_seconds"] == 7


def test_root_can_configure_site_identity_text_settings():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={
        "site_name": "BLOCKCHAIN Lab",
        "site_document_title": "BLOCKCHAIN Lab Portal",
        "site_login_heading": "Welcome to the lab",
        "site_login_subtitle": "Private playground",
        "site_success_heading": "登入完成",
        "site_success_message": "回到控制台",
    })

    assert res.status_code == 200
    data = res.get_json()
    assert data["settings"]["site_name"] == "BLOCKCHAIN Lab"
    assert data["settings"]["site_document_title"] == "BLOCKCHAIN Lab Portal"
    assert state["site_success_message"] == "回到控制台"

    bad = client.put("/api/admin/settings", json={"site_name": "x" * 81})
    assert bad.status_code == 400
    assert state["site_name"] == "BLOCKCHAIN Lab"


def test_root_can_configure_dashboard_refresh_seconds():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={
        "server_backpressure_traffic_refresh_seconds": 8,
        "server_output_refresh_seconds": 6,
        "security_test_job_poll_seconds": 7,
        "job_center_refresh_seconds": 4,
        "economy_dashboard_refresh_seconds": 45,
        "trading_dashboard_refresh_seconds": 9,
        "trading_live_price_refresh_seconds": 3,
        "trading_reference_price_refresh_seconds": 2,
        "trading_reference_chart_refresh_seconds": 10,
        "comfyui_job_poll_seconds": 2,
        "notification_poll_seconds": 90,
        "game_invite_poll_active_seconds": 6,
        "game_invite_poll_idle_seconds": 80,
        "game_invite_poll_hidden_seconds": 240,
        "server_connection_monitor_seconds": 20,
        "drive_dashboard_lazy_refresh_seconds": 12,
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body["settings"]["job_center_refresh_seconds"] == 4
    assert state["trading_live_price_refresh_seconds"] == 3
    assert state["comfyui_job_poll_seconds"] == 2
    assert state["notification_poll_seconds"] == 90
    assert state["game_invite_poll_hidden_seconds"] == 240
    assert state["drive_dashboard_lazy_refresh_seconds"] == 12

    bad = client.put("/api/admin/settings", json={"trading_live_price_refresh_seconds": 0})
    assert bad.status_code == 400
    assert state["trading_live_price_refresh_seconds"] == 3


def test_admin_settings_reject_invalid_boolean_strings_for_security_flags():
    app, state = _admin_app()
    state["integrity_guard_enabled"] = True
    state["audit_chain_enabled"] = True
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"integrity_guard_enabled": "yes_please"})

    assert res.status_code == 400
    assert state["integrity_guard_enabled"] is True

    res = client.put("/api/admin/settings", json={"audit_chain_enabled": "enable_me"})

    assert res.status_code == 400
    assert state["audit_chain_enabled"] is True


def test_admin_settings_reject_absurd_ranges_and_invalid_snapshot_time():
    app, state = _admin_app()
    state["video_tip_fee_percent"] = 5
    state["video_tip_min_points"] = 1
    state["security_log_tail_lines"] = 200
    state["snapshot_daily_time"] = "03:00"
    client = app.test_client()

    assert client.put("/api/admin/settings", json={"video_tip_fee_percent": -5}).status_code == 400
    assert client.put("/api/admin/settings", json={"video_tip_fee_percent": 99999}).status_code == 400
    assert client.put("/api/admin/settings", data='{"video_tip_fee_percent": NaN}', content_type="application/json").status_code == 400
    assert client.put("/api/admin/settings", data='{"video_tip_fee_percent": Infinity}', content_type="application/json").status_code == 400
    assert client.put("/api/admin/settings", json={"video_tip_min_points": -1}).status_code == 400
    assert client.put("/api/admin/settings", json={"video_tip_min_points": 10**18}).status_code == 400
    assert client.put("/api/admin/settings", json={"security_log_tail_lines": -1}).status_code == 400
    assert client.put("/api/admin/settings", json={"security_log_tail_lines": 10**9}).status_code == 400
    assert client.put("/api/admin/settings", json={"snapshot_daily_time": "25:99"}).status_code == 400
    assert client.put("/api/admin/settings", json={"snapshot_daily_time": "abcd"}).status_code == 400
    assert client.put("/api/admin/settings", json={"snapshot_daily_time": "12:30:45"}).status_code == 400

    assert state["video_tip_fee_percent"] == 5
    assert state["video_tip_min_points"] == 1
    assert state["security_log_tail_lines"] == 200
    assert state["snapshot_daily_time"] == "03:00"


def test_root_can_configure_comfyui_api_endpoint_without_restart_hint():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"comfyui_api_host": "192.168.1.20", "comfyui_api_port": 8193})

    assert res.status_code == 200
    assert state["comfyui_api_host"] == "192.168.1.20"
    assert state["comfyui_api_port"] == 8193
    assert res.get_json()["settings"]["comfyui_api_host"] == "192.168.1.20"
    assert res.get_json()["settings"]["comfyui_api_port"] == 8193


def test_root_can_configure_local_comfyui_script_with_absolute_path(tmp_path):
    app, state = _admin_app()
    client = app.test_client()
    comfy_base = tmp_path / "ComfyUI_windows_portable"
    comfy_base.mkdir()
    script = comfy_base / "run_in_linux.sh"
    script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

    res = client.put(
        "/api/admin/settings",
        json={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
            "comfyui_local_start_script": str(script),
            "comfyui_api_host": "localhost",
            "comfyui_api_port": 8188,
        },
    )

    assert res.status_code == 200
    assert state["comfyui_connection_mode"] == "local"
    assert state["comfyui_base_dir"] == str(comfy_base)
    assert state["comfyui_local_start_script"] == "run_in_linux.sh"
    assert state["comfyui_api_port"] == 8188


def test_root_can_configure_local_comfyui_main_py_performance_flags():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put(
        "/api/admin/settings",
        json={
            "comfyui_connection_mode": "local",
            "comfyui_local_vram_mode": "lowvram",
            "comfyui_local_precision": "force_fp16",
            "comfyui_local_unet_dtype": "fp8_e4m3fn",
            "comfyui_local_vae_dtype": "fp32",
            "comfyui_local_text_encoder_dtype": "fp16",
            "comfyui_local_cpu_vae": True,
            "comfyui_local_attention_mode": "pytorch",
            "comfyui_local_upcast_attention": "force",
            "comfyui_local_cuda_malloc": "disable",
            "comfyui_local_disable_smart_memory": True,
            "comfyui_local_deterministic": True,
            "comfyui_local_async_offload": "disable",
            "comfyui_local_cache_mode": "lru",
            "comfyui_local_cache_lru": 64,
            "comfyui_local_reserve_vram_gb": "1.5",
        },
    )

    assert res.status_code == 200
    assert state["comfyui_local_vram_mode"] == "lowvram"
    assert state["comfyui_local_precision"] == "force_fp16"
    assert state["comfyui_local_unet_dtype"] == "fp8_e4m3fn"
    assert state["comfyui_local_vae_dtype"] == "fp32"
    assert state["comfyui_local_text_encoder_dtype"] == "fp16"
    assert state["comfyui_local_cpu_vae"] is True
    assert state["comfyui_local_attention_mode"] == "pytorch"
    assert state["comfyui_local_upcast_attention"] == "force"
    assert state["comfyui_local_cuda_malloc"] == "disable"
    assert state["comfyui_local_disable_smart_memory"] is True
    assert state["comfyui_local_deterministic"] is True
    assert state["comfyui_local_async_offload"] == "disable"
    assert state["comfyui_local_cache_mode"] == "lru"
    assert state["comfyui_local_cache_lru"] == 64
    assert state["comfyui_local_reserve_vram_gb"] == "1.5"
    assert res.get_json()["settings"]["comfyui_local_cpu_vae"] is True


def test_local_comfyui_main_py_performance_flags_reject_invalid_values():
    app, state = _admin_app()
    client = app.test_client()

    assert client.put("/api/admin/settings", json={"comfyui_local_vram_mode": "fastest"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_local_unet_dtype": "int4"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_local_cpu_vae": "maybe"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_local_cache_lru": 10001}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_local_reserve_vram_gb": "999"}).status_code == 400
    assert "comfyui_local_vram_mode" not in state


def test_root_can_leave_remote_comfyui_url_blank_when_saving_settings():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"comfyui_connection_mode": "remote", "comfyui_remote_api_url": ""})

    assert res.status_code == 200
    assert state["comfyui_connection_mode"] == "remote"
    assert state["comfyui_remote_api_url"] == ""


def test_remote_comfyui_url_requires_explicit_port():
    app, state = _admin_app()
    client = app.test_client()

    good = client.put("/api/admin/settings", json={"comfyui_remote_api_url": "https://comfy.example.com:8443"})
    bad = client.put("/api/admin/settings", json={"comfyui_remote_api_url": "https://comfy.example.com"})

    assert good.status_code == 200
    assert state["comfyui_remote_api_url"] == "https://comfy.example.com:8443"
    assert bad.status_code == 400


def test_invalid_comfyui_api_endpoint_is_rejected():
    app, state = _admin_app()
    client = app.test_client()

    bad_host = client.put("/api/admin/settings", json={"comfyui_api_host": "http://127.0.0.1:8192/prompt"})
    bad_port = client.put("/api/admin/settings", json={"comfyui_api_port": 70000})

    assert bad_host.status_code == 400
    assert bad_port.status_code == 400
    assert state["comfyui_api_host"] == "localhost"
    assert state["comfyui_api_port"] == 8192


def test_root_can_configure_comfyui_batch_limit_without_restart_hint():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"comfyui_max_batch_size": 4})

    assert res.status_code == 200
    assert state["comfyui_max_batch_size"] == 4
    assert res.get_json()["settings"]["comfyui_max_batch_size"] == 4


def test_comfyui_account_api_key_is_write_only_and_clearable():
    audit_log = []
    app, state = _admin_app(audit_log=audit_log)
    client = app.test_client()

    saved = client.put(
        "/api/admin/settings",
        json={
            "comfyui_paid_api_nodes_enabled": True,
            "comfyui_account_api_key": "comfyui-secret-key",
        },
    )

    assert saved.status_code == 200, saved.get_json()
    assert state["comfyui_paid_api_nodes_enabled"] is True
    assert state["comfyui_account_api_key"] == "comfyui-secret-key"
    payload = saved.get_json()["settings"]
    assert payload["comfyui_account_api_key"] == ""
    assert payload["comfyui_account_api_key_configured"] is True
    assert "comfyui-secret-key" not in json.dumps(saved.get_json(), ensure_ascii=False)
    assert "comfyui-secret-key" not in json.dumps(audit_log, ensure_ascii=False)

    readback = client.get("/api/admin/settings").get_json()["settings"]
    assert readback["comfyui_account_api_key"] == ""
    assert readback["comfyui_account_api_key_configured"] is True

    unchanged = client.put("/api/admin/settings", json={"comfyui_account_api_key": "", "comfyui_max_batch_size": 2})
    assert unchanged.status_code == 200, unchanged.get_json()
    assert state["comfyui_account_api_key"] == "comfyui-secret-key"
    assert unchanged.get_json()["settings"]["comfyui_account_api_key_configured"] is True

    cleared = client.put("/api/admin/settings", json={"comfyui_account_api_key_clear": True})
    assert cleared.status_code == 200, cleared.get_json()
    assert state["comfyui_account_api_key"] == ""
    assert cleared.get_json()["settings"]["comfyui_account_api_key_configured"] is False


def test_comfyui_account_api_key_rejects_whitespace():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"comfyui_account_api_key": "bad key with spaces"})

    assert res.status_code == 400
    assert "comfyui_account_api_key" not in state


def test_ai_agent_api_key_is_write_only_and_clearable():
    app, state = _admin_app()
    client = app.test_client()

    saved = client.put(
        "/api/admin/settings",
        json={
            "ai_agent_provider": "hermes",
            "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
            "ai_agent_api_key": "hermes-secret-key",
            "ai_agent_model": "hermes-agent",
            "ai_agent_allow_image_input": True,
        },
    )

    assert saved.status_code == 200, saved.get_json()
    assert state["ai_agent_api_key"] == "hermes-secret-key"
    payload = saved.get_json()["settings"]
    assert payload["ai_agent_api_key"] == ""
    assert payload["ai_agent_api_key_configured"] is True
    assert "hermes-secret-key" not in json.dumps(saved.get_json(), ensure_ascii=False)

    unchanged = client.put("/api/admin/settings", json={"ai_agent_api_key": "", "ai_agent_model": "hermes-agent-v2"})
    assert unchanged.status_code == 200, unchanged.get_json()
    assert state["ai_agent_api_key"] == "hermes-secret-key"
    assert state["ai_agent_model"] == "hermes-agent-v2"
    assert unchanged.get_json()["settings"]["ai_agent_api_key_configured"] is True

    cleared = client.put("/api/admin/settings", json={"ai_agent_api_key_clear": True})
    assert cleared.status_code == 200, cleared.get_json()
    assert state["ai_agent_api_key"] == ""
    assert cleared.get_json()["settings"]["ai_agent_api_key_configured"] is False


def test_ai_agent_settings_validate_url_and_key_shape():
    app, state = _admin_app()
    client = app.test_client()

    bad_url = client.put("/api/admin/settings", json={"ai_agent_api_base_url": "http://user:pass@127.0.0.1:8642/v1"})
    assert bad_url.status_code == 400
    assert "ai_agent_api_base_url" not in state

    bad_key = client.put("/api/admin/settings", json={"ai_agent_api_key": "bad key with spaces"})
    assert bad_key.status_code == 400
    assert "ai_agent_api_key" not in state


def test_root_can_configure_diffusers_backend_and_hf_token_write_only():
    app, state = _admin_app()
    client = app.test_client()

    saved = client.put(
        "/api/admin/settings",
        json={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": "dhead/waiIllustriousSDXL_v150",
            "comfyui_huggingface_api_token": "hf_read_token",
            "comfyui_diffusers_device": "cuda",
            "comfyui_diffusers_dtype": "float16",
            "comfyui_diffusers_device_map": "cuda",
            "comfyui_allow_in_process_diffusers": True,
            "comfyui_diffusers_low_cpu_mem_usage": True,
            "comfyui_diffusers_cuda_fallback_to_cpu": False,
            "comfyui_diffusers_keep_downloaded_models": False,
            "comfyui_diffusers_disable_xet": False,
        },
    )

    assert saved.status_code == 200
    assert state["comfyui_connection_mode"] == "diffusers"
    assert state["comfyui_diffusers_model_repo"] == "dhead/waiIllustriousSDXL_v150"
    assert state["comfyui_huggingface_api_token"] == "hf_read_token"
    assert state["comfyui_diffusers_device"] == "cuda"
    assert state["comfyui_diffusers_dtype"] == "float16"
    assert state["comfyui_diffusers_device_map"] == "cuda"
    assert state["comfyui_allow_in_process_diffusers"] is True
    assert state["comfyui_diffusers_low_cpu_mem_usage"] is True
    assert state["comfyui_diffusers_cuda_fallback_to_cpu"] is False
    assert state["comfyui_diffusers_keep_downloaded_models"] is False
    assert state["comfyui_diffusers_disable_xet"] is False
    payload = saved.get_json()["settings"]
    assert payload["comfyui_huggingface_api_token"] == ""
    assert payload["comfyui_huggingface_api_token_configured"] is True
    assert payload["comfyui_allow_in_process_diffusers"] is True
    assert payload["comfyui_diffusers_device_map"] == "cuda"
    assert payload["comfyui_diffusers_low_cpu_mem_usage"] is True
    assert payload["comfyui_diffusers_cuda_fallback_to_cpu"] is False
    assert payload["comfyui_diffusers_keep_downloaded_models"] is False
    assert payload["comfyui_diffusers_disable_xet"] is False

    unchanged = client.put("/api/admin/settings", json={"comfyui_huggingface_api_token": "", "comfyui_diffusers_device": "auto", "comfyui_allow_in_process_diffusers": False, "comfyui_diffusers_device_map": "disabled", "comfyui_diffusers_low_cpu_mem_usage": False, "comfyui_diffusers_cuda_fallback_to_cpu": True, "comfyui_diffusers_keep_downloaded_models": True, "comfyui_diffusers_disable_xet": True})
    assert unchanged.status_code == 200
    assert state["comfyui_huggingface_api_token"] == "hf_read_token"
    assert state["comfyui_allow_in_process_diffusers"] is False
    assert state["comfyui_diffusers_device_map"] == "disabled"
    assert state["comfyui_diffusers_low_cpu_mem_usage"] is False
    assert state["comfyui_diffusers_cuda_fallback_to_cpu"] is True
    assert state["comfyui_diffusers_keep_downloaded_models"] is True
    assert state["comfyui_diffusers_disable_xet"] is True
    assert unchanged.get_json()["settings"]["comfyui_huggingface_api_token_configured"] is True

    cleared = client.put("/api/admin/settings", json={"comfyui_huggingface_api_token_clear": True})
    assert cleared.status_code == 200
    assert state["comfyui_huggingface_api_token"] == ""
    assert cleared.get_json()["settings"]["comfyui_huggingface_api_token_configured"] is False


def test_diffusers_settings_reject_invalid_repo_token_and_runtime_options():
    app, state = _admin_app()
    client = app.test_client()

    assert client.put("/api/admin/settings", json={"comfyui_diffusers_model_repo": "../bad/model"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_huggingface_api_token": "bad token"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_diffusers_device": "tpu"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_diffusers_dtype": "int8"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_diffusers_device_map": "everything"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_allow_in_process_diffusers": "maybe"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_diffusers_low_cpu_mem_usage": "maybe"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_diffusers_cuda_fallback_to_cpu": "maybe"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_diffusers_keep_downloaded_models": "maybe"}).status_code == 400
    assert client.put("/api/admin/settings", json={"comfyui_diffusers_disable_xet": "maybe"}).status_code == 400
    assert "comfyui_diffusers_model_repo" not in state


def test_root_can_configure_diffusers_huggingface_cache_root(tmp_path):
    app, state = _admin_app()
    client = app.test_client()
    cache_root = tmp_path / "hf-cache"
    cache_root.mkdir()

    saved = client.put("/api/admin/settings", json={"comfyui_huggingface_cache_root": str(cache_root)})

    assert saved.status_code == 200
    assert state["comfyui_huggingface_cache_root"] == str(cache_root.resolve())
    assert saved.get_json()["settings"]["comfyui_huggingface_cache_root"] == str(cache_root.resolve())
    assert client.put("/api/admin/settings", json={"comfyui_huggingface_cache_root": "/"}).status_code == 400


def test_root_can_configure_comfyui_default_dimensions_without_restart_hint():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"comfyui_default_width": 768, "comfyui_default_height": 1024})

    assert res.status_code == 200
    assert state["comfyui_default_width"] == 768
    assert state["comfyui_default_height"] == 1024
    assert res.get_json()["settings"]["comfyui_default_width"] == 768
    assert res.get_json()["settings"]["comfyui_default_height"] == 1024


def test_invalid_comfyui_default_dimensions_are_rejected():
    app, state = _admin_app()
    client = app.test_client()

    bad_small = client.put("/api/admin/settings", json={"comfyui_default_width": 32})
    bad_step = client.put("/api/admin/settings", json={"comfyui_default_height": 1025})

    assert bad_small.status_code == 400
    assert bad_step.status_code == 400
    assert state["comfyui_default_width"] == 1024
    assert state["comfyui_default_height"] == 1024


def test_invalid_comfyui_batch_limit_is_rejected():
    app, state = _admin_app()
    client = app.test_client()

    res = client.put("/api/admin/settings", json={"comfyui_max_batch_size": 9})

    assert res.status_code == 400
    assert state["comfyui_max_batch_size"] == 1


def test_admin_environment_exposes_relative_paths_and_pid():
    app, _ = _admin_app()
    client = app.test_client()

    res = client.get("/api/admin/environment")
    assert res.status_code == 200
    env = res.get_json()["environment"]
    assert env["pid"] > 0
    assert env["base_dir"] == "."
    assert env["database_path"] == "missing.db"
    assert env["log_dir"] == "."
    assert env["chat_dir"] == "."
    resources = res.get_json()["resource_usage"]
    database_usage = res.get_json()["database_usage"]
    transfer_usage = res.get_json()["transfer_usage"]
    assert res.get_json()["resource_refresh_seconds"] == 5
    assert {"cpu", "gpu", "vram", "ram", "sampled_at"} <= set(resources)
    assert resources["cpu"]["label"] == "CPU"
    assert resources["ram"]["label"] == "RAM"
    assert {"total_bytes", "file_count", "files"} <= set(database_usage)
    assert {"upload_bytes_per_second", "download_bytes_per_second", "cumulative_upload_bytes", "cumulative_download_bytes"} <= set(transfer_usage)
    assert env["anchor_dir"] == "."
    for key in ("base_dir", "database_path", "log_dir", "chat_dir", "anchor_dir"):
        assert not str(env[key]).startswith("/")


def test_admin_environment_resources_is_lightweight_resource_endpoint():
    app, state = _admin_app({"system_resource_board_refresh_seconds": 9})
    client = app.test_client()

    res = client.get("/api/admin/environment/resources")

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["resource_refresh_seconds"] == 9
    assert {"cpu", "gpu", "vram", "ram", "sampled_at"} <= set(body["resource_usage"])
    assert {"total_bytes", "file_count", "files"} <= set(body["database_usage"])
    assert {"upload_bytes_per_second", "download_bytes_per_second", "cumulative_upload_bytes", "cumulative_download_bytes"} <= set(body["transfer_usage"])


def test_effective_server_bind_falls_back_to_environment():
    bind = effective_server_bind(
        {"server_listen_host": "", "server_listen_port": 0},
        env={"HTML_LEARNING_HOST": "127.0.0.1", "HTML_LEARNING_PORT": "9000"},
    )
    assert bind["host"] == "127.0.0.1"
    assert bind["port"] == 9000
    assert bind["host_source"] == "env"
