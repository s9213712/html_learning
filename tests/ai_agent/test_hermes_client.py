import pytest

from services.ai_agent.hermes import (
    AiAgentError,
    _normalize_chat_messages,
    normalize_ai_agent_api_base_url,
    normalize_ai_agent_model,
    public_ai_agent_settings,
    validate_ai_agent_api_key,
)


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

