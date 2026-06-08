import json
import os
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urljoin, urlparse


DEFAULT_AI_AGENT_API_BASE_URL = os.environ.get("HACKME_AI_AGENT_API_BASE_URL", "http://127.0.0.1:8642/v1")
DEFAULT_AI_AGENT_MODEL = os.environ.get("HACKME_AI_AGENT_MODEL", "hermes-agent")
DEFAULT_AI_AGENT_PROVIDER = "hermes"
DEFAULT_AI_AGENT_PERSONA = "concise_helper"
MAX_AI_AGENT_IMAGE_DATA_URL_CHARS = 3 * 1024 * 1024


AI_AGENT_PERSONA_PRESETS = {
    "concise_helper": {
        "label": "簡潔客服導向",
        "guidance": "請保持回應簡潔、步驟明確，先判斷使用者在網站流程中的位置。",
        "tone": "平實、可執行導向。",
    },
    "strict_helper": {
        "label": "嚴謹流程助手",
        "guidance": "回應請先列出確認條件，逐步排查，附上檢查順序與結果判讀。",
        "tone": "保守、結構化。",
    },
    "creative_coordinator": {
        "label": "創意流程統籌",
        "guidance": "回應時提供清楚的提示詞與參數建議，但要先確認任務範圍是否符合站內功能。",
        "tone": "有組織、鼓勵式。",
    },
}

AI_AGENT_TASKS = {
    "site_guide": {
        "label": "網站導覽",
        "description": "回答站內功能位置、按鈕位置、流程步驟。",
        "safe_reply": "若未啟用，請回覆請先開啟「網站導覽」。",
    },
    "troubleshoot": {
        "label": "生圖 / 下載排錯",
        "description": "協助檢查生圖、下載、模型載入、輸出與報錯流程，不做實際操作。",
        "safe_reply": "若未啟用，請回覆請先開啟「生圖 / 下載排錯」。",
    },
    "prompt": {
        "label": "生圖提示詞與參數",
        "description": "提供提示詞、尺寸、步數與常見參數建議，但不直接執行。",
        "safe_reply": "若未啟用，請回覆請先開啟「生圖提示詞與參數」。",
    },
}

AI_AGENT_TOOL_BLUEPRINT = {
    "check_download_state": {
        "label": "下載排查",
        "description": "依下載、輸出與錯誤訊息提供下一步檢查順序。",
    },
    "suggest_navigation_step": {
        "label": "導覽建議",
        "description": "指出網站畫面、頁面與操作路徑。",
    },
    "suggest_prompt": {
        "label": "提示詞建議",
        "description": "提供可直接複製調整的提示詞與參數草稿。",
    },
}

AI_AGENT_SAFETY_BOUNDARIES = (
    "不得要求或收集帳號憑證、API key、session token、私密金鑰。",
    "不得輸出可執行指令、程式碼、SQL、腳本或可直接修改伺服器狀態的操作。",
    "不得建議或引導惡意存取、越權、刪除資料與提權流程。",
    "對超出站內導覽、生圖、提示詞、下載排錯範圍的請求，需明確拒絕並給予建議改走站內正規流程。",
)

AI_AGENT_ROLE_SCOPES = {
    "user": {
        "label": "個別用戶助手",
        "description": "專門處理已登入用戶的站內導覽、排錯與提示詞建議，僅提供讀取與建議，不代為操作。",
        "capabilities": [
            "個人任務查詢（生圖 / 下載）",
            "站內流程導覽",
            "提示詞與參數建議",
            "失敗排查步驟建議（只提供指引）",
        ],
    },
    "manager": {
        "label": "管理者助手",
        "description": "除了個別用戶能力外，提供會員管理輔助方向與帳號異常判讀（讀取導向）。",
        "capabilities": [
            "個人任務查詢（生圖 / 下載）",
            "站內流程導覽",
            "提示詞與參數建議",
            "失敗排查步驟建議（只提供指引）",
            "會員管理與帳號狀態（只提供唯讀建議）",
        ],
        "additional_tasks": ["member_management"],
    },
    "super_admin": {
        "label": "最高管理者助手",
        "description": "管理者能力加上伺服器資源與攻擊告警的唯讀診斷建議。",
        "capabilities": [
            "個人任務查詢（生圖 / 下載）",
            "站內流程導覽",
            "提示詞與參數建議",
            "失敗排查步驟建議（只提供指引）",
            "會員管理與帳號狀態（只提供唯讀建議）",
            "伺服器資源與攻擊訊號（只提供診斷建議）",
        ],
        "additional_tasks": ["member_management", "attack_diagnosis"],
    },
}


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


def normalize_ai_agent_persona(value):
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_AI_AGENT_PERSONA
    return raw if raw in AI_AGENT_PERSONA_PRESETS else None


def _normalize_ai_agent_task_flag(value, *, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in {"1", "true", "on", "yes", "y"}:
        return True
    if raw in {"0", "false", "off", "no", "n", "disable", "disabled"}:
        return False
    return default


def _normalize_ai_agent_behavior(settings):
    persona = normalize_ai_agent_persona(settings.get("ai_agent_persona")) or DEFAULT_AI_AGENT_PERSONA
    tasks = {
        "site_guide": _normalize_ai_agent_task_flag(settings.get("ai_agent_task_site_guide"), default=True),
        "troubleshoot": _normalize_ai_agent_task_flag(settings.get("ai_agent_task_troubleshoot"), default=True),
        "prompt": _normalize_ai_agent_task_flag(settings.get("ai_agent_task_prompt"), default=True),
    }
    tools = []
    for tool_key, details in AI_AGENT_TOOL_BLUEPRINT.items():
        tools.append({
            "name": tool_key,
            "label": details["label"],
            "description": details["description"],
        })
    return {
        "persona": persona,
        "tasks": tasks,
        "tools": tools,
    }


def normalize_ai_agent_role(value):
    raw = str(value or "").strip().lower()
    if raw in AI_AGENT_ROLE_SCOPES:
        return raw
    if raw in {"root", "admin", "super", "super_admin"}:
        return "super_admin"
    return "user"


def _agent_role_scope(role):
    return AI_AGENT_ROLE_SCOPES.get(role, AI_AGENT_ROLE_SCOPES["user"])


def _ai_agent_system_prompt(behavior, *, role="user", allow_tool_runs=False):
    scope = _agent_role_scope(normalize_ai_agent_role(role))
    persona_meta = AI_AGENT_PERSONA_PRESETS.get(behavior.get("persona"), AI_AGENT_PERSONA_PRESETS[DEFAULT_AI_AGENT_PERSONA])
    enabled_tasks = [
        f"- {AI_AGENT_TASKS[task_id]['label']}: {AI_AGENT_TASKS[task_id]['description']}"
        for task_id in AI_AGENT_TASKS
        if behavior.get("tasks", {}).get(task_id)
    ]
    disabled_tasks = [
        AI_AGENT_TASKS[task_id]["label"]
        for task_id in AI_AGENT_TASKS
        if not behavior.get("tasks", {}).get(task_id)
    ]
    tool_lines = []
    for detail in behavior.get("tools") or []:
        tool_lines.append(f"- {detail.get('name')}（{detail.get('label')}）：{detail.get('description')}")
    tool_scope = (
        "工具僅提供可執行建議，不會直接呼叫系統 API 或修改站內狀態。"
        if not allow_tool_runs
        else "可提供建議型工具摘要，仍不直接下發站內變更操作。"
    )

    return (
        "你是 hackme_web 網站內的 AI 助理，嚴格負責在本站功能邊界內回答。\n"
        f"角色：{persona_meta['label']}。\n"
        f"語氣：{persona_meta['tone']}。\n"
        f"基本原則：{persona_meta['guidance']}\n"
        f"服務範圍：{scope['label']}。\n"
        f"用途：{scope['description']}\n"
        "可執行任務：\n"
        + "\n".join(enabled_tasks or ["- 目前未啟用任務，請管理端先啟用任務後再處理。"]) + "\n"
        "可提供服務：\n"
        + "\n".join(f"- {item}" for item in scope["capabilities"]) + "\n"
        + "安全邊際：\n"
        + "\n".join(f"- {item}" for item in AI_AGENT_SAFETY_BOUNDARIES) + "\n"
        + "工具公告：\n"
        + "\n".join(tool_lines) + "\n"
        f"{tool_scope}\n"
        + (f"未啟用任務提示：{', '.join(disabled_tasks)}\n" if disabled_tasks else "")
        + "回應時若使用者需求不在可執行任務範圍，請明確回應無法執行並引導到可用功能。\n"
    )


def public_ai_agent_settings(settings, *, actor=None):
    settings = settings or {}
    key = str(settings.get("ai_agent_api_key") or "").strip()
    behavior = _normalize_ai_agent_behavior(settings)
    actor_role = normalize_ai_agent_role((actor or {}).get("role") if isinstance(actor, dict) else "user")
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
        "role": actor_role,
        "scope": _agent_role_scope(actor_role),
        "persona": behavior["persona"],
        "tasks": behavior["tasks"],
        "tools": behavior["tools"],
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
    path = (parsed.path or "").rstrip("/")
    urls = []
    if path:
        urls.append(f"{parsed.scheme}://{parsed.netloc}{path}/health")
    urls.append(f"{parsed.scheme}://{parsed.netloc}/health")

    last_error = ""
    for health_url in urls:
        req = urllib_request.Request(health_url, headers=_backend_headers(settings), method="GET")
        try:
            with urllib_request.urlopen(req, timeout=min(_backend_timeout(settings), 8)) as resp:
                raw = resp.read(1024 * 1024)
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return {"ok": True, "url": health_url, "payload": payload}
        except Exception as exc:  # pragma: no cover - fallback path probing
            last_error = str(exc)
            continue

    return {"ok": False, "url": urls[-1] if urls else base_url, "msg": last_error}


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


def ai_agent_chat(settings, *, messages=None, prompt="", image_data_url="", model="", session_key="", actor=None):
    public = public_ai_agent_settings(settings)
    normalized_messages = _normalize_chat_messages(
        messages,
        prompt=prompt,
        image_data_url=image_data_url,
        allow_image_input=public["allow_image_input"],
    )
    if not normalized_messages:
        raise AiAgentError("請輸入訊息")

    sanitized_messages = [
        message
        for message in normalized_messages
        if str(message.get("role") or "").strip() in {"user", "assistant"}
    ]
    if not sanitized_messages:
        raise AiAgentError("請輸入訊息")
    behavior = _normalize_ai_agent_behavior(settings)
    actor_role = normalize_ai_agent_role((actor or {}).get("role") if isinstance(actor, dict) else "user")
    system_prompt = _ai_agent_system_prompt(
        behavior,
        role=actor_role,
        allow_tool_runs=bool(public["allow_tool_runs"]),
    )
    sanitized_messages = [{"role": "system", "content": system_prompt}, *sanitized_messages]

    max_prompt_chars = public["max_prompt_chars"]
    if _message_text_length(sanitized_messages[1:]) > max_prompt_chars:
        raise AiAgentError(f"訊息內容超過上限 {max_prompt_chars} 字")
    model_name = normalize_ai_agent_model(model) or public["model"] or DEFAULT_AI_AGENT_MODEL
    payload = {
        "model": model_name,
        "messages": sanitized_messages,
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
