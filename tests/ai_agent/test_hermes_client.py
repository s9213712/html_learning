import pytest
import json
from urllib import error as urllib_error

from services.ai_agent.hermes import (
    AiAgentError,
    normalize_ai_agent_role,
    _normalize_chat_messages,
    normalize_ai_agent_api_base_url,
    normalize_ai_agent_model,
    normalize_ai_agent_persona,
    public_ai_agent_settings,
    validate_ai_agent_api_key,
)
from services.ai_agent import hermes as hermes_client


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
