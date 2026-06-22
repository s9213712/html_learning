import pytest
import json
import sqlite3
from datetime import datetime, timedelta
from urllib import error as urllib_error

from services.ai_agent.hermes import (
    AiAgentError,
    ai_agent_capabilities,
    ai_agent_effective_tools,
    ai_agent_operation_mode_policy,
    ai_agent_write_guard_status,
    get_ai_agent_audit_last_scan,
    public_ai_agent_audit_status,
    normalize_ai_agent_allowed_models,
    normalize_ai_agent_allowed_tools,
    clear_ai_agent_audit_scan_state,
    normalize_ai_agent_operation_mode,
    normalize_ai_agent_role,
    _normalize_chat_messages,
    normalize_ai_agent_api_base_url,
    normalize_ai_agent_model,
    normalize_ai_agent_persona,
    public_ai_agent_settings,
    validate_ai_agent_api_key,
)
from services.ai_agent import hermes as hermes_client
from services.ai_agent.hermes import run_ai_agent_audit_scan


def test_ai_agent_public_settings_redacts_secret_and_keeps_connection_fields():
    payload = public_ai_agent_settings({
        "ai_agent_provider": "hermes",
        "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
        "ai_agent_api_key": "secret",
        "ai_agent_model": "hermes-agent",
        "ai_agent_request_timeout_seconds": 120,
        "ai_agent_max_prompt_chars": 20000,
        "ai_agent_allow_image_input": True,
        "ai_agent_allow_tool_runs": False,
    })

    assert payload["provider"] == "hermes"
    assert payload["api_base_url"] == "http://127.0.0.1:8642/v1"
    assert payload["api_key_configured"] is True
    assert "api_key" not in payload
    assert payload["model"] == "hermes-agent"
    assert payload["persona"] == "concise_helper"
    assert payload["tasks"] == {
        "site_guide": True,
        "troubleshoot": True,
        "prompt": True,
    }


def test_ai_agent_public_settings_default_prompt_limit_is_relaxed():
    payload = public_ai_agent_settings({})

    assert payload["max_prompt_chars"] == 80000


def test_ai_agent_base_url_rejects_credentials_query_and_fragment():
    assert normalize_ai_agent_api_base_url("http://127.0.0.1:8642/v1") == "http://127.0.0.1:8642/v1"
    assert normalize_ai_agent_api_base_url("https://agent.example.test/v1/") == "https://agent.example.test/v1"
    assert normalize_ai_agent_api_base_url("ftp://127.0.0.1:8642/v1") is None
    assert normalize_ai_agent_api_base_url("http://user:pass@127.0.0.1:8642/v1") is None
    assert normalize_ai_agent_api_base_url("http://127.0.0.1:8642/v1?x=1") is None
    assert normalize_ai_agent_api_base_url("http://127.0.0.1:8642/v1#frag") is None


def test_ai_agent_key_and_model_validation():
    assert validate_ai_agent_api_key("sk-local") == "sk-local"
    assert validate_ai_agent_api_key("bad key") is None
    assert validate_ai_agent_api_key("bad\nkey") is None
    assert normalize_ai_agent_model("hermes-agent") == "hermes-agent"
    assert normalize_ai_agent_model("bad\nmodel") is None


def test_ai_agent_persona_validation():
    assert normalize_ai_agent_persona("concise_helper") == "concise_helper"
    assert normalize_ai_agent_persona("strict_helper") == "strict_helper"
    assert normalize_ai_agent_persona("creative_coordinator") == "creative_coordinator"
    assert normalize_ai_agent_persona("bad-persona") is None


def test_ai_agent_admin_role_maps_to_manager_scope_only():
    assert normalize_ai_agent_role("admin") == "manager"
    assert normalize_ai_agent_role("manager") == "manager"
    assert normalize_ai_agent_role("root") == "super_admin"
    assert normalize_ai_agent_role("super_admin") == "super_admin"


def test_ai_agent_chat_injects_persona_and_task_scope(monkeypatch):
    payloads = []

    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        payloads.append({"method": method, "path": path, "payload": payload, "session_key": session_key, "timeout": timeout})
        return {"choices": [{"message": {"content": "回覆內容"}}], "model": "hermes-agent"}

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)
    result = hermes_client.ai_agent_chat(
        {
            "ai_agent_persona": "strict_helper",
            "ai_agent_task_prompt": False,
            "ai_agent_task_site_guide": True,
            "ai_agent_task_troubleshoot": False,
            "ai_agent_allow_tool_runs": False,
        },
        messages=[{"role": "user", "content": "我想知道為什麼下載沒反應"}],
    )

    assert result["content"] == "回覆內容"
    assert payloads, "預期會發送一次 backend 請求"
    request_payload = payloads[0]["payload"]
    messages = request_payload.get("messages") or []
    assert messages and messages[0]["role"] == "system"
    content = str(messages[0]["content"])
    assert "嚴謹流程助手" in content
    assert "網站導覽" in content
    assert "未啟用任務提示" in content
    assert "生圖 / 下載排錯" in content
    assert "生圖提示詞與參數" in content
    assert "工具僅提供可執行建議" in content


def test_ai_agent_multimodal_messages_are_openai_compatible():
    messages = _normalize_chat_messages(
        [{"role": "user", "content": "describe this"}],
        image_data_url="data:image/png;base64,abc",
        allow_image_input=True,
    )

    assert messages == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ],
    }]


def test_ai_agent_rejects_image_when_disabled():
    with pytest.raises(AiAgentError):
        _normalize_chat_messages(
            [{"role": "user", "content": "describe this"}],
            image_data_url="data:image/png;base64,abc",
            allow_image_input=False,
        )


def test_ai_agent_health_checks_base_path_when_present(monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self, _size):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    requests = []

    def fake_urlopen(req, timeout=5):
        url = getattr(req, "full_url", "")
        requests.append(url)
        if url == "http://127.0.0.1:8642/v1/health":
            raise urllib_error.URLError("not found")
        if url == "http://127.0.0.1:8642/health":
            return FakeResponse({"ok": True})
        raise urllib_error.URLError("not found")

    monkeypatch.setattr(hermes_client.urllib_request, "urlopen", fake_urlopen)

    result = hermes_client.ai_agent_health({"ai_agent_api_base_url": "http://127.0.0.1:8642/v1"})

    assert requests == ["http://127.0.0.1:8642/v1/health", "http://127.0.0.1:8642/health"]
    assert result["ok"] is True
    assert result["url"] == "http://127.0.0.1:8642/health"
    assert result["payload"] == {"ok": True}


def test_ai_agent_health_marks_mock_backend_as_unhealthy(monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self, _size):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    calls = []

    def fake_urlopen(req, timeout=5):
        url = getattr(req, "full_url", "")
        calls.append(url)
        if url == "http://127.0.0.1:8642/v1/health":
            return FakeResponse({"service": "hermes-mock", "version": "mock-1"})
        if url == "http://127.0.0.1:8642/health":
            return FakeResponse({"service": "hermes-mock", "version": "mock-1"})
        raise urllib_error.URLError("not found")

    monkeypatch.setattr(hermes_client.urllib_request, "urlopen", fake_urlopen)

    result = hermes_client.ai_agent_health({"ai_agent_api_base_url": "http://127.0.0.1:8642/v1"})

    assert calls == ["http://127.0.0.1:8642/v1/health"]
    assert result["ok"] is False
    assert "hermes-mock" in str(result["msg"])
    assert result["payload"]["service"] == "hermes-mock"


def test_ai_agent_health_openai_compatible_uses_models_endpoint(monkeypatch):
    class FakeResponse:
        def read(self, _size):
            return json.dumps({"object": "list", "data": [{"id": "gpt-oss:120b-cloud"}]}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    calls = []

    def fake_urlopen(req, timeout=5):
        calls.append(getattr(req, "full_url", ""))
        return FakeResponse()

    monkeypatch.setattr(hermes_client.urllib_request, "urlopen", fake_urlopen)

    result = hermes_client.ai_agent_health({
        "ai_agent_provider": "openai_compatible",
        "ai_agent_api_base_url": "http://127.0.0.1:11434/v1",
    })

    assert calls == ["http://127.0.0.1:11434/v1/models"]
    assert result["ok"] is True
    assert result["url"] == "http://127.0.0.1:11434/v1/models"
    assert result["payload"]["data"][0]["id"] == "gpt-oss:120b-cloud"


def test_ai_agent_capabilities_openai_compatible_is_synthetic(monkeypatch):
    def fail_json_request(*_args, **_kwargs):
        raise AssertionError("openai-compatible capabilities must not call /capabilities")

    monkeypatch.setattr(hermes_client, "_json_request", fail_json_request)

    result = ai_agent_capabilities({"ai_agent_provider": "openai_compatible"})

    assert result["ok"] is True
    assert result["provider"] == "openai_compatible"
    assert result["chat"] is True
    assert result["models"] is True
    assert result["capabilities_endpoint"] is False


def test_ai_agent_chat_detects_mock_reply(monkeypatch):
    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        assert path == "/chat/completions"
        return {
            "model": "hermes-agent",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Mock hermes response: 已收到你的請求。",
                    },
                },
            ],
        }

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
            },
            messages=[{"role": "user", "content": "幫我看一下下載進度"}],
        )
    assert "mock 回覆" in str(exc.value)


def test_ai_agent_chat_detects_hermes_failed_envelope(monkeypatch):
    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        assert path == "/chat/completions"
        return {
            "model": "qwen3-vl:235b-instruct-cloud",
            "choices": [
                {
                    "finish_reason": "error",
                    "message": {
                        "role": "assistant",
                        "content": "API call failed after 3 retries: HTTP 500: Internal Server Error",
                    },
                },
            ],
            "hermes": {
                "completed": False,
                "failed": True,
                "error": "HTTP 500: Internal Server Error",
            },
        }

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
                "ai_agent_allowed_models": "qwen3-vl:235b-instruct-cloud",
            },
            messages=[{"role": "user", "content": "分析圖片"}],
            model="qwen3-vl:235b-instruct-cloud",
        )
    assert "後端執行失敗" in str(exc.value)


def test_ai_agent_chat_empty_model_uses_configured_allowed_model(monkeypatch):
    captured = {}

    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        assert path == "/chat/completions"
        captured["payload"] = payload
        return {
            "model": "gpt-oss:120b",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "{\"ok\":true}",
                    },
                },
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    result = hermes_client.ai_agent_chat(
        {
            "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
            "ai_agent_api_key": "dummy-key",
            "ai_agent_model": "gpt-oss:120b-cloud",
            "ai_agent_allowed_models": "gpt-oss:120b-cloud,qwen3-vl:235b-instruct-cloud",
        },
        messages=[{"role": "user", "content": "只回覆 JSON"}],
        model="",
    )

    assert captured["payload"]["model"] == "gpt-oss:120b-cloud"
    assert result["content"] == "{\"ok\":true}"
    assert result["usage"]["total_tokens"] == 12


def test_ai_agent_chat_trims_old_history_before_prompt_limit(monkeypatch):
    captured = {}

    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        assert path == "/chat/completions"
        captured["payload"] = payload
        return {
            "model": "gpt-oss:120b",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    result = hermes_client.ai_agent_chat(
        {
            "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
            "ai_agent_api_key": "dummy-key",
            "ai_agent_model": "gpt-oss:120b-cloud",
            "ai_agent_max_prompt_chars": 1000,
        },
        messages=[
            {"role": "user", "content": "OLD-" * 400},
            {"role": "assistant", "content": "PREVIOUS-" * 100},
            {"role": "user", "content": "最新指令：請查伺服器狀態"},
        ],
        model="",
    )

    sent_messages = captured["payload"]["messages"]
    sent_text = "\n".join(str(message.get("content") or "") for message in sent_messages)
    assert result["content"] == "ok"
    assert sent_messages[0]["role"] == "system"
    assert "最新指令" in sent_text
    assert "OLD-" not in sent_text


def test_ai_agent_chat_detects_error_finish_reason(monkeypatch):
    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        assert path == "/chat/completions"
        return {
            "model": "qwen3-vl:235b-instruct-cloud",
            "choices": [
                {
                    "finish_reason": "error",
                    "message": {
                        "role": "assistant",
                        "content": "API call failed after 3 retries: HTTP 500: Internal Server Error",
                    },
                },
            ],
        }

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
                "ai_agent_allowed_models": "qwen3-vl:235b-instruct-cloud",
            },
            messages=[{"role": "user", "content": "分析圖片"}],
            model="qwen3-vl:235b-instruct-cloud",
        )
    assert "後端執行失敗" in str(exc.value)


def test_ai_agent_chat_detects_mock_reply_with_whitespace(monkeypatch):
    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        assert path == "/chat/completions"
        return {
            "model": "hermes-agent",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": " Mock  \nHermes  Response:   已收到你的請求。 ",
                    },
                },
            ],
        }

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
            },
            messages=[{"role": "user", "content": "幫我看一下下載進度"}],
        )
    assert "mock 回覆" in str(exc.value)


def test_ai_agent_chat_detects_mock_reply_simplified_chinese(monkeypatch):
    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        assert path == "/chat/completions"
        return {
            "model": "hermes-agent",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Mock hermes response: 已收到你的请求。",
                    },
                },
            ],
        }

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
            },
            messages=[{"role": "user", "content": "幫我看一下下載進度"}],
        )
    assert "mock 回覆" in str(exc.value)


def test_ai_agent_chat_detects_mock_reply_in_nested_field(monkeypatch):
    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        assert path == "/chat/completions"
        return {
            "model": "hermes-agent",
            "response": " Mock  \nHermes  Response:   已收到你的請求。 ",
        }

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
            },
            messages=[{"role": "user", "content": "幫我看一下下載進度"}],
        )
    assert "mock 回覆" in str(exc.value)


def test_ai_agent_chat_detects_mock_reply_with_interleaving_whitespace(monkeypatch):
    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        assert path == "/chat/completions"
        return {
            "model": "hermes-agent",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": " mock\tHermes\nResponse :\u3000已\u6536\u5230\u4f60\u7684\u8bf7\u6c42。 ",
                    },
                },
            ],
        }

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
            },
            messages=[{"role": "user", "content": "幫我看一下下載進度"}],
        )
    assert "mock 回覆" in str(exc.value)


def test_ai_agent_operation_mode_normalizes_and_keeps_allowed_models():
    assert normalize_ai_agent_operation_mode("readonly") == "readonly"
    assert normalize_ai_agent_operation_mode("Read_Only") == "readonly"
    assert normalize_ai_agent_operation_mode("audit") == "audit"
    assert normalize_ai_agent_operation_mode("bad") is None

    assert normalize_ai_agent_allowed_models("model-a,model-b") == "model-a,model-b"
    assert normalize_ai_agent_allowed_models(["model-a", "model-b", "model-a"]) == "model-a,model-b"
    assert normalize_ai_agent_allowed_models("  ") == ""
    assert normalize_ai_agent_allowed_models("model\nx") is None
    assert normalize_ai_agent_allowed_tools("check_resource_state,audit_scan") == "check_resource_state,audit_scan"
    assert normalize_ai_agent_allowed_tools("bad_tool") is None

    write_policy = ai_agent_operation_mode_policy("write")
    assert write_policy["mode"] == "write"
    assert write_policy["write_enabled"] is True
    assert write_policy["min_role"] == "super_admin"


def test_ai_agent_effective_tools_are_role_and_allowlist_scoped():
    user_tools = {tool["name"] for tool in ai_agent_effective_tools({}, actor_role="user")}
    root_tools = {tool["name"] for tool in ai_agent_effective_tools({}, actor_role="super_admin")}
    restricted_root_tools = {
        tool["name"]
        for tool in ai_agent_effective_tools({"ai_agent_allowed_tools": "audit_scan,inspect_user_files"}, actor_role="super_admin")
    }

    assert "inspect_user_files" in user_tools
    assert "audit_scan" not in user_tools
    assert "audit_scan" in root_tools
    assert restricted_root_tools == {"audit_scan", "inspect_user_files"}


def test_ai_agent_chat_blocks_mutating_request_in_readonly_mode(monkeypatch):
    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        return {"choices": [{"message": {"content": "should not reach"}}], "model": "hermes-agent"}

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
                "ai_agent_operation_mode": "readonly",
            },
            messages=[{"role": "user", "content": "幫我刪除資料"}],
            actor={"role": "user"},
        )
    assert "唯讀模式" in str(exc.value)


def test_ai_agent_chat_blocks_non_root_in_audit_mode(monkeypatch):
    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        return {"choices": [{"message": {"content": "root audit ok"}}], "model": "hermes-agent"}

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
                "ai_agent_operation_mode": "audit",
            },
            messages=[{"role": "user", "content": "查一下目前任務進度"}],
            actor={"role": "user"},
        )
    assert "審計模式" in str(exc.value)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
                "ai_agent_operation_mode": "audit",
            },
            messages=[{"role": "user", "content": "查一下目前任務進度"}],
            actor={"role": "manager"},
        )
    assert "root" in str(exc.value)

    result = hermes_client.ai_agent_chat(
        {
            "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
            "ai_agent_api_key": "dummy-key",
            "ai_agent_operation_mode": "audit",
        },
        messages=[{"role": "user", "content": "查一下目前任務進度"}],
        actor={"username": "root", "role": "user"},
    )
    assert result["content"] == "root audit ok"


def test_ai_agent_chat_write_mode_is_root_only(monkeypatch):
    payloads = []

    def fake_json_request(_settings, method, path, payload=None, session_key="", timeout=None):
        payloads.append(payload or {})
        return {"choices": [{"message": {"content": "root write ok"}}], "model": "hermes-agent"}

    monkeypatch.setattr(hermes_client, "_json_request", fake_json_request)

    with pytest.raises(AiAgentError) as exc:
        hermes_client.ai_agent_chat(
            {
                "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
                "ai_agent_api_key": "dummy-key",
                "ai_agent_operation_mode": "write",
            },
            messages=[{"role": "user", "content": "幫我調整設定"}],
            actor={"role": "manager"},
        )
    assert "執行寫入模式" in str(exc.value)

    result = hermes_client.ai_agent_chat(
        {
            "ai_agent_api_base_url": "http://127.0.0.1:8642/v1",
            "ai_agent_api_key": "dummy-key",
            "ai_agent_operation_mode": "write",
        },
        messages=[{"role": "user", "content": "幫我調整設定"}],
        actor={"username": "root", "role": "user"},
    )
    assert result["content"] == "root write ok"
    system_prompt = payloads[-1]["messages"][0]["content"]
    assert "目前登入者：root" in system_prompt
    assert "目前權限：super_admin" in system_prompt
    assert "你不是一般使用者助手，也不是唯讀模式" in system_prompt
    assert "前台直接執行" in system_prompt
    assert "不要要求複製 JSON 或手動 POST" in system_prompt
    assert "/api/ai-agent/write-tools/execute" in system_prompt
    assert "confirm=EXECUTE" in system_prompt


def test_ai_agent_audit_scan_reports_anomalies_and_uses_cache(tmp_path):
    db_path = tmp_path / "ai_agent_audit_scan.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS security_events")
    conn.execute("DROP TABLE IF EXISTS secure_audit")
    conn.execute(
        """
        CREATE TABLE security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            ip_address TEXT,
            target_user TEXT,
            detail TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            action TEXT,
            ip TEXT,
            user TEXT,
            success INTEGER,
            detail TEXT
        )
        """
    )
    now = datetime.now().replace(microsecond=0)
    for idx in range(3):
        created_at = (now - timedelta(seconds=idx * 5)).isoformat()
        conn.execute(
            "INSERT INTO security_events (event_type, ip_address, target_user, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            ("ip_block", "198.51.100.10", "userA", f"event-{idx}", created_at),
        )
    for idx in range(12):
        ts = (now - timedelta(seconds=idx)).isoformat()
        conn.execute(
            "INSERT INTO secure_audit (ts, action, ip, user, success, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, "ai_agent", "198.51.100.10", "userA", 1, f"probe-{idx}"),
        )
    conn.commit()
    conn.close()

    actor = {"id": 1, "username": "root", "role": "super_admin"}
    settings = {
        "ai_agent_audit_interval_minutes": 1,
        "ai_agent_audit_cpu_percent_threshold": 1,
        "ai_agent_audit_ram_percent_threshold": 1,
        "ai_agent_audit_disk_percent_threshold": 1,
        "ai_agent_audit_ip_event_rate_threshold": 1,
        "ai_agent_audit_ip_event_rate_window_minutes": 1,
        "ai_agent_audit_security_event_rate_threshold": 1,
        "ai_agent_audit_security_event_rate_window_minutes": 1,
        "ai_agent_audit_auto_block_suspect_ip": False,
        "ai_agent_audit_notify_root": False,
    }

    calls = {"db": 0}

    def get_db():
        calls["db"] += 1
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    first = run_ai_agent_audit_scan(
        settings,
        get_db=get_db,
        actor=actor,
        force=True,
        get_client_ip=lambda: "127.0.0.1",
        get_ua=lambda: "pytest",
        audit=lambda *args, **kwargs: None,
    )
    assert isinstance(first, dict)
    assert first["status"] in {"warn", "alert"}
    assert first["anomalies"], first
    assert first["aggregates"]["security_events_total"] == 3
    assert first["aggregates"]["secure_audit_total"] == 12
    assert calls["db"] == 1

    second = run_ai_agent_audit_scan(
        settings,
        get_db=get_db,
        actor=actor,
        force=False,
        get_client_ip=lambda: "127.0.0.1",
        get_ua=lambda: "pytest",
        audit=lambda *args, **kwargs: None,
    )
    assert second["cached"] is True
    assert calls["db"] == 1

    third = run_ai_agent_audit_scan(
        settings,
        get_db=get_db,
        actor=actor,
        force=True,
        get_client_ip=lambda: "127.0.0.1",
        get_ua=lambda: "pytest",
        audit=lambda *args, **kwargs: None,
    )
    assert third["cached"] is False
    assert calls["db"] >= 2


def test_ai_agent_audit_scan_auto_block_suspect_ip(monkeypatch, tmp_path):
    db_path = tmp_path / "ai_agent_audit_block.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            action TEXT,
            ip TEXT,
            user TEXT,
            success INTEGER,
            detail TEXT
        )
        """
    )
    now = datetime.now().replace(microsecond=0)
    for idx in range(12):
        ts = (now - timedelta(seconds=idx)).isoformat()
        conn.execute(
            "INSERT INTO secure_audit (ts, action, ip, user, success, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, "ai_agent", "198.51.100.20", "userA", 1, f"probe-{idx}"),
        )
    conn.commit()
    conn.close()

    blocked_ips = []

    def fake_block_ip(ip, *, minutes, reason):
        blocked_ips.append({"ip": ip, "minutes": minutes, "reason": reason})

    monkeypatch.setattr("services.security.events.block_ip", fake_block_ip)

    def get_db():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    scan = run_ai_agent_audit_scan(
        {
            "ai_agent_audit_interval_minutes": 1,
            "ai_agent_audit_ip_event_rate_threshold": 1,
            "ai_agent_audit_ip_event_rate_window_minutes": 1,
            "ai_agent_audit_security_event_rate_threshold": 100,
            "ai_agent_audit_security_event_rate_window_minutes": 1,
            "ai_agent_audit_cpu_percent_threshold": 100,
            "ai_agent_audit_ram_percent_threshold": 100,
            "ai_agent_audit_disk_percent_threshold": 100,
            "ai_agent_audit_auto_block_suspect_ip": True,
            "ai_agent_audit_block_minutes": 8,
            "ai_agent_audit_notify_root": False,
        },
        get_db=get_db,
        actor={"id": 1, "username": "root", "role": "super_admin"},
        force=True,
        get_client_ip=lambda: "127.0.0.1",
        get_ua=lambda: "pytest",
        audit=lambda *args, **kwargs: None,
    )

    assert scan["interventions"], scan
    assert blocked_ips and blocked_ips[0]["ip"] == "198.51.100.20"
    assert blocked_ips[0]["minutes"] == 8


def test_ai_agent_audit_scan_locks_down_write_tools_on_sensitive_setting_change(tmp_path):
    clear_ai_agent_audit_scan_state()
    db_path = tmp_path / "ai_agent_audit_guard.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            action TEXT,
            ip TEXT,
            user TEXT,
            success INTEGER,
            detail TEXT
        )
        """
    )
    now = datetime.now().replace(microsecond=0)
    conn.execute(
        "INSERT INTO secure_audit (ts, action, ip, user, success, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (
            now.isoformat(),
            "SETTINGS_CHANGED",
            "127.0.0.1",
            "root",
            1,
            json.dumps({"changed_keys": ["ai_agent_allowed_tools"], "scope": "system_settings"}),
        ),
    )
    conn.commit()
    conn.close()
    audit_events = []

    def get_db():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    scan = run_ai_agent_audit_scan(
        {
            "ai_agent_audit_interval_minutes": 1,
            "ai_agent_audit_ip_event_rate_threshold": 100,
            "ai_agent_audit_ip_event_rate_window_minutes": 1,
            "ai_agent_audit_security_event_rate_threshold": 100,
            "ai_agent_audit_security_event_rate_window_minutes": 1,
            "ai_agent_audit_cpu_percent_threshold": 100,
            "ai_agent_audit_ram_percent_threshold": 100,
            "ai_agent_audit_disk_percent_threshold": 100,
            "ai_agent_audit_auto_block_suspect_ip": False,
            "ai_agent_audit_notify_root": False,
        },
        get_db=get_db,
        actor={"id": 1, "username": "root", "role": "super_admin"},
        force=True,
        get_client_ip=lambda: "127.0.0.1",
        get_ua=lambda: "pytest",
        audit=lambda *args, **kwargs: audit_events.append({"args": args, "kwargs": kwargs}),
    )

    assert scan["status"] == "alert"
    assert any(item["code"] == "ai_agent.sensitive_settings_changed" for item in scan["anomalies"])
    assert any(item["type"] == "ai_agent_write_tools_lockdown" for item in scan["interventions"])
    assert ai_agent_write_guard_status()["blocked"] is True
    assert any(event["args"][0] == "AI_AGENT_AUDIT_MAIN_AI_GUARD" for event in audit_events)


def test_ai_agent_write_guard_persistent_clear_event_unblocks(tmp_path):
    clear_ai_agent_audit_scan_state()
    db_path = tmp_path / "ai_agent_audit_guard_clear.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            action TEXT,
            ip TEXT,
            user TEXT,
            success INTEGER,
            detail TEXT
        )
        """
    )
    now = datetime.now().replace(microsecond=0)
    conn.execute(
        "INSERT INTO secure_audit (ts, action, ip, user, success, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (now.isoformat(), "AI_AGENT_AUDIT_MAIN_AI_GUARD", "127.0.0.1", "root", 0, "blocked"),
    )
    conn.execute(
        "INSERT INTO secure_audit (ts, action, ip, user, success, detail) VALUES (?, ?, ?, ?, ?, ?)",
        ((now + timedelta(seconds=1)).isoformat(), "AI_AGENT_AUDIT_MAIN_AI_GUARD_CLEAR", "127.0.0.1", "root", 1, "clear"),
    )
    conn.commit()
    conn.close()

    def get_db():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    assert ai_agent_write_guard_status(get_db=get_db)["blocked"] is False


def test_public_ai_agent_audit_status_uses_last_scan_cache(tmp_path):
    clear_ai_agent_audit_scan_state()
    db_path = tmp_path / "ai_agent_audit_status.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS security_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, ip_address TEXT, target_user TEXT, detail TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS secure_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT, ip TEXT, user TEXT, success INTEGER, detail TEXT)")
    conn.commit()
    conn.close()

    status = public_ai_agent_audit_status({
        "ai_agent_operation_mode": "audit",
        "ai_agent_audit_interval_minutes": 1,
        "ai_agent_audit_auto_block_suspect_ip": False,
        "ai_agent_audit_notify_root": False,
    })
    assert status["mode"] == "audit"
    assert status["scheduler"]["enabled"] is True
    assert status["scheduler"]["has_scan"] is False

    state = get_ai_agent_audit_last_scan()
    assert state["has_result"] is False

    settings = {
        "ai_agent_audit_interval_minutes": 1,
        "ai_agent_audit_auto_block_suspect_ip": False,
        "ai_agent_audit_notify_root": False,
    }
    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    run_ai_agent_audit_scan(
        settings,
        get_db=get_db,
        actor={"id": 1, "username": "root", "role": "super_admin"},
        force=True,
        get_client_ip=lambda: "127.0.0.1",
        get_ua=lambda: "pytest",
        audit=lambda *args, **kwargs: None,
    )
    status_after = public_ai_agent_audit_status({
        "ai_agent_operation_mode": "audit",
        "ai_agent_audit_interval_minutes": 1,
        "ai_agent_audit_auto_block_suspect_ip": False,
        "ai_agent_audit_notify_root": False,
    })
    assert status_after["scheduler"]["has_scan"] is True
    assert "status" in status_after["summary"]
