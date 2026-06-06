from flask import request

from services.ai_agent.hermes import (
    AiAgentError,
    ai_agent_capabilities,
    ai_agent_chat,
    ai_agent_health,
    ai_agent_models,
    public_ai_agent_settings,
)


def _actor_value(actor, key, default=None):
    if not actor:
        return default
    try:
        return actor[key]
    except Exception:
        return actor.get(key, default) if hasattr(actor, "get") else default


def register_ai_agent_routes(app, deps):
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_system_settings = deps.get("get_system_settings", lambda: {})
    get_client_ip = deps.get("get_client_ip", lambda: "")
    get_ua = deps.get("get_ua", lambda: "")
    audit = deps.get("audit", lambda *args, **kwargs: None)
    json_resp = deps["json_resp"]
    require_csrf_safe = deps["require_csrf_safe"]
    require_csrf = deps.get("require_csrf", require_csrf_safe)
    role_rank = deps.get("role_rank", lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0))

    def _actor_or_401():
        actor = get_current_user_ctx()
        if not actor:
            return None, json_resp({"ok": False, "msg": "請先登入"}, 401)
        settings = get_system_settings() or {}
        min_role = str(settings.get("module_ai_agent_min_role") or "manager")
        actor_role = _actor_value(actor, "role", "user")
        if _actor_value(actor, "username") != "root" and role_rank(actor_role) < role_rank(min_role):
            return None, json_resp({"ok": False, "msg": "沒有 AI Agent 使用權限"}, 403)
        return actor, None

    @app.route("/api/ai-agent/status", methods=["GET"])
    @require_csrf_safe
    def ai_agent_status():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        settings = get_system_settings() or {}
        public = public_ai_agent_settings(settings)
        health = ai_agent_health(settings)
        capabilities = ai_agent_capabilities(settings) if health.get("ok") else {}
        return json_resp({
            "ok": True,
            "settings": public,
            "health": health,
            "capabilities": capabilities,
            "actor": {
                "username": _actor_value(actor, "username", ""),
                "role": _actor_value(actor, "role", "user"),
            },
        })

    @app.route("/api/ai-agent/models", methods=["GET"])
    @require_csrf_safe
    def ai_agent_models_route():
        _actor, denied = _actor_or_401()
        if denied:
            return denied
        settings = get_system_settings() or {}
        try:
            models = ai_agent_models(settings)
        except AiAgentError as exc:
            return json_resp({"ok": False, "msg": str(exc), "status": exc.status, "payload": exc.payload}), 502
        return json_resp({"ok": True, "models": models})

    @app.route("/api/ai-agent/chat", methods=["POST"])
    @require_csrf
    def ai_agent_chat_route():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        settings = get_system_settings() or {}
        user_id = _actor_value(actor, "id", 0)
        session_id = str(data.get("session_id") or "").strip()[:120]
        session_key = f"hackme:{user_id}:{session_id or 'default'}"
        try:
            result = ai_agent_chat(
                settings,
                messages=data.get("messages"),
                prompt=data.get("prompt") or "",
                image_data_url=data.get("image_data_url") or "",
                model=data.get("model") or "",
                session_key=session_key,
            )
        except AiAgentError as exc:
            audit(
                "AI_AGENT_CHAT",
                get_client_ip(),
                user=_actor_value(actor, "username", "-"),
                ua=get_ua(),
                success=False,
                detail=f"status={exc.status or '-'},error={str(exc)[:180]}",
            )
            return json_resp({"ok": False, "msg": str(exc), "status": exc.status, "payload": exc.payload}), 502
        audit(
            "AI_AGENT_CHAT",
            get_client_ip(),
            user=_actor_value(actor, "username", "-"),
            ua=get_ua(),
            success=True,
            detail=f"model={result.get('model') or ''},image={bool(data.get('image_data_url'))}",
        )
        return json_resp({
            "ok": True,
            "message": {"role": "assistant", "content": result.get("content") or ""},
            "model": result.get("model") or "",
            "usage": result.get("usage") or {},
        })

