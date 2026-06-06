import json
import os
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urljoin, urlparse


DEFAULT_AI_AGENT_API_BASE_URL = os.environ.get("HACKME_AI_AGENT_API_BASE_URL", "http://127.0.0.1:8642/v1")
DEFAULT_AI_AGENT_MODEL = os.environ.get("HACKME_AI_AGENT_MODEL", "hermes-agent")
DEFAULT_AI_AGENT_PROVIDER = "hermes"
MAX_AI_AGENT_IMAGE_DATA_URL_CHARS = 3 * 1024 * 1024


class AiAgentError(Exception):
    """Raised when the configured AI Agent backend cannot satisfy a request."""

    def __init__(self, message, *, status=None, payload=None):
        self.status = status
        self.payload = payload
        super().__init__(message)


def parse_int_setting(settings, key, default, minimum, maximum):
    try:
        value = int((settings or {}).get(key, default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def normalize_ai_agent_api_base_url(value, *, allow_blank=True):
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return "" if allow_blank else None
    if len(raw) > 2048:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.fragment:
        return None
    if parsed.query:
        return None
    return raw


def validate_ai_agent_api_key(value, *, allow_blank=True):
    raw = str(value or "").strip()
    if not raw:
        return "" if allow_blank else None
    if len(raw) > 2048 or any(ch.isspace() for ch in raw):
        return None
    return raw


def normalize_ai_agent_model(value):
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_AI_AGENT_MODEL
    if len(raw) > 200 or any(ch in raw for ch in "\r\n\t"):
        return None
    return raw


def normalize_ai_agent_provider(value):
    raw = str(value or DEFAULT_AI_AGENT_PROVIDER).strip().lower()
    return raw if raw in {"hermes", "openai_compatible"} else None


def public_ai_agent_settings(settings):
    settings = settings or {}
    key = str(settings.get("ai_agent_api_key") or "").strip()
    return {
        "provider": normalize_ai_agent_provider(settings.get("ai_agent_provider")) or DEFAULT_AI_AGENT_PROVIDER,
        "api_base_url": normalize_ai_agent_api_base_url(
            settings.get("ai_agent_api_base_url") or DEFAULT_AI_AGENT_API_BASE_URL,
            allow_blank=True,
        ) or "",
        "api_key_configured": bool(key),
        "model": normalize_ai_agent_model(settings.get("ai_agent_model")) or DEFAULT_AI_AGENT_MODEL,
        "request_timeout_seconds": parse_int_setting(settings, "ai_agent_request_timeout_seconds", 120, 5, 600),
        "max_prompt_chars": parse_int_setting(settings, "ai_agent_max_prompt_chars", 20000, 1000, 200000),
        "allow_image_input": bool(settings.get("ai_agent_allow_image_input", True)),
        "allow_tool_runs": bool(settings.get("ai_agent_allow_tool_runs", False)),
    }


def _backend_base_url(settings):
    base_url = normalize_ai_agent_api_base_url(
        (settings or {}).get("ai_agent_api_base_url") or DEFAULT_AI_AGENT_API_BASE_URL,
        allow_blank=False,
    )
    if not base_url:
        raise AiAgentError("AI Agent API 位址尚未設定或格式錯誤")
    return base_url


def _backend_timeout(settings):
    return parse_int_setting(settings, "ai_agent_request_timeout_seconds", 120, 5, 600)


def _backend_headers(settings, *, session_key=""):
    headers = {"Content-Type": "application/json"}
    api_key = validate_ai_agent_api_key((settings or {}).get("ai_agent_api_key"), allow_blank=True) or ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        if session_key:
            headers["X-Hermes-Session-Key"] = str(session_key)[:240]
    return headers


def _json_request(settings, method, path, payload=None, *, session_key="", timeout=None):
    base_url = _backend_base_url(settings)
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        headers=_backend_headers(settings, session_key=session_key),
        method=method.upper(),
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout or _backend_timeout(settings)) as resp:
            raw = resp.read(10 * 1024 * 1024)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib_error.HTTPError as exc:
        raw = exc.read(512 * 1024)
        payload = {}
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {"raw": raw.decode("utf-8", "replace")}
        message = (
            payload.get("error", {}).get("message")
            if isinstance(payload.get("error"), dict)
            else payload.get("msg") or payload.get("message")
        )
        raise AiAgentError(message or f"AI Agent backend HTTP {exc.code}", status=exc.code, payload=payload)
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        raise AiAgentError(f"AI Agent backend 無法連線：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise AiAgentError(f"AI Agent backend 回傳不是有效 JSON：{exc}") from exc


def ai_agent_health(settings):
    base_url = _backend_base_url(settings)
    parsed = urlparse(base_url)
    health_url = f"{parsed.scheme}://{parsed.netloc}/health"
    req = urllib_request.Request(health_url, headers=_backend_headers(settings), method="GET")
    try:
        with urllib_request.urlopen(req, timeout=min(_backend_timeout(settings), 8)) as resp:
            raw = resp.read(1024 * 1024)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return {"ok": True, "url": health_url, "payload": payload}
    except Exception as exc:
        return {"ok": False, "url": health_url, "msg": str(exc)}


def ai_agent_capabilities(settings):
    try:
        return _json_request(settings, "GET", "/capabilities", timeout=min(_backend_timeout(settings), 8))
    except AiAgentError as exc:
        return {"ok": False, "msg": str(exc), "status": exc.status}


def ai_agent_models(settings):
    return _json_request(settings, "GET", "/models", timeout=min(_backend_timeout(settings), 15))


def _message_text_length(messages):
    total = 0
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(str(part.get("text") or ""))
    return total


def _normalize_chat_messages(messages, *, prompt="", image_data_url="", allow_image_input=True):
    normalized = []
    source = messages if isinstance(messages, list) else []
    for item in source:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        content = item.get("content")
        if isinstance(content, str):
            normalized.append({"role": role, "content": content[:200000]})
        elif isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "").strip()
                if part_type == "text":
                    parts.append({"type": "text", "text": str(part.get("text") or "")[:200000]})
                elif part_type == "image_url" and allow_image_input:
                    image_url = part.get("image_url") or {}
                    if isinstance(image_url, dict):
                        url = str(image_url.get("url") or "")
                    else:
                        url = str(image_url or "")
                    if url.startswith("data:image/") and len(url) <= MAX_AI_AGENT_IMAGE_DATA_URL_CHARS:
                        parts.append({"type": "image_url", "image_url": {"url": url}})
            if parts:
                normalized.append({"role": role, "content": parts})
    if not normalized and prompt:
        normalized.append({"role": "user", "content": str(prompt)})
    if image_data_url:
        if not allow_image_input:
            raise AiAgentError("目前設定不允許圖片輸入")
        image_data_url = str(image_data_url or "")
        if not image_data_url.startswith("data:image/") or len(image_data_url) > MAX_AI_AGENT_IMAGE_DATA_URL_CHARS:
            raise AiAgentError("圖片資料格式錯誤或超過大小限制")
        if not normalized:
            normalized.append({"role": "user", "content": []})
        last = normalized[-1]
        content = last.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        elif not isinstance(content, list):
            content = []
        content.append({"type": "image_url", "image_url": {"url": image_data_url}})
        last["content"] = content
    return normalized


def ai_agent_chat(settings, *, messages=None, prompt="", image_data_url="", model="", session_key=""):
    public = public_ai_agent_settings(settings)
    normalized_messages = _normalize_chat_messages(
        messages,
        prompt=prompt,
        image_data_url=image_data_url,
        allow_image_input=public["allow_image_input"],
    )
    if not normalized_messages:
        raise AiAgentError("請輸入訊息")
    max_prompt_chars = public["max_prompt_chars"]
    if _message_text_length(normalized_messages) > max_prompt_chars:
        raise AiAgentError(f"訊息內容超過上限 {max_prompt_chars} 字")
    model_name = normalize_ai_agent_model(model) or public["model"] or DEFAULT_AI_AGENT_MODEL
    payload = {
        "model": model_name,
        "messages": normalized_messages,
        "stream": False,
    }
    response = _json_request(settings, "POST", "/chat/completions", payload, session_key=session_key)
    choices = response.get("choices") if isinstance(response, dict) else None
    message = {}
    if choices and isinstance(choices, list) and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        content = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return {
        "content": str(content or ""),
        "model": response.get("model") if isinstance(response, dict) else model_name,
        "usage": response.get("usage") if isinstance(response, dict) else None,
        "raw": response,
    }

