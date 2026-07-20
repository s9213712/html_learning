from __future__ import annotations

from scripts.testing.ai_agent_comfyui_status_probe import (
    chat_event_succeeded,
    chat_response_message,
    probe_succeeded,
)


def test_chat_response_message_accepts_success_object() -> None:
    assert chat_response_message({"message": {"content": "running"}}) == "running"


def test_chat_response_message_accepts_error_string() -> None:
    assert chat_response_message({"message": "server is busy", "msg": "fallback"}) == "server is busy"


def test_controlled_server_busy_event_is_not_success() -> None:
    event = {
        "status": 503,
        "response": {"ok": False, "error": "server_busy", "message": "retry later"},
    }
    assert chat_event_succeeded(event) is False
    assert probe_succeeded({
        "send_result": {"ok": True},
        "send_disabled_after": False,
        "chat_events": [event],
    }) is False


def test_probe_requires_terminal_successful_chat_event() -> None:
    success = {"status": 200, "response": {"ok": True, "message": {"content": "idle"}}}
    assert probe_succeeded({
        "send_result": {"ok": True},
        "send_disabled_after": False,
        "chat_events": [success],
    }) is True
    assert probe_succeeded({
        "send_result": {"ok": True},
        "send_disabled_after": False,
        "chat_events": [],
    }) is False
