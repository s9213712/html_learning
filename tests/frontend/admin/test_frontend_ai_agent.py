from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_ai_agent_module_frontend_is_wired_as_independent_feature():
    html = _read("public/index.html")
    core_js = _read("public/js/00-core.js")
    admin_js = _read("public/js/50-admin.js")
    bootstrap_js = _read("public/js/90-bootstrap.js")
    ai_agent_js = _read("public/js/37-ai-agent.js")
    public_routes_py = _read("routes/public.py")
    css = _read("public/styles.css")

    assert 'id="tab-module-ai-agent" style="display:none;"' in html
    assert 'id="module-ai-agent"' in html
    assert 'id="ai-agent-thread"' in html
    assert 'id="ai-agent-image-file" accept="image/*"' in html
    assert 'id="sec-settings-ai-agent"' in html
    assert 'id="s-ai-agent-api-base-url"' in html
    assert 'id="s-ai-agent-api-key"' in html
    assert 'id="s-module-ai-agent-min-role"' in html
    assert "/js/37-ai-agent.js?v=20260609-ai-agent-debug-msg" in html

    assert '"ai-agent": "feature_ai_agent_enabled"' in core_js
    assert "normalizeModuleSettingKey(moduleKey)" in core_js
    assert '"module_ai_agent_min_role": settings.get("module_ai_agent_min_role")' in public_routes_py
    assert 'tab-module-ai-agent' in core_js
    assert 'canAccessModule("ai-agent")' in core_js
    assert 'switchModuleTab("ai-agent")' in bootstrap_js
    assert 'loadAiAgentStatus({ force: true })' in bootstrap_js

    assert 'const canAccessAiAgent = !!currentUser && canAccessModule("ai-agent");' in admin_js
    assert 'modAiAgent.classList.toggle("active", normTab === "ai-agent")' in admin_js
    assert 'ai_agent_api_key_clear' in admin_js
    assert 'feature_ai_agent_enabled' in admin_js

    assert 'API + "/ai-agent/status"' in ai_agent_js
    assert 'API + "/ai-agent/chat"' in ai_agent_js
    assert 'image_url' in ai_agent_js

    assert ".ai-agent-layout" in css
    assert ".ai-agent-thread" in css
    assert "@media (max-width: 640px)" in css


def test_ai_agent_root_quick_settings_use_reserved_pricing_key():
    quick_js = _read("public/js/01-root-quick-settings.js")

    assert 'item_key: "ai_agent_task_basic"' in quick_js
    assert '"ai-agent": {' in quick_js
    assert 'pricingKeys: ["ai_agent_task_basic"]' in quick_js
    assert 'id: "s-feature-ai-agent-enabled"' in quick_js
    assert 'id: "s-module-ai-agent-min-role"' in quick_js
