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
    assert '<select id="ai-agent-model">' in html
    assert '<input type="text" id="ai-agent-model"' not in html
    assert 'id="sec-settings-ai-agent"' in html
    assert 'id="s-ai-agent-api-base-url"' in html
    assert 'id="s-ai-agent-api-key"' in html
    assert 'id="s-ai-agent-model"' in html
    assert 'id="s-ai-agent-operation-mode"' in html
    assert 'id="s-ai-agent-allowed-models"' in html
    assert 'id="s-ai-agent-allowed-tools"' in html
    assert 'id="s-ai-agent-audit-interval-minutes"' in html
    assert 'id="s-ai-agent-audit-auto-block-suspect-ip"' in html
    assert 'id="ai-agent-operation-mode-state"' in html
    assert 'id="ai-agent-allowed-models-state"' in html
    assert 'id="ai-agent-allowed-tools-state"' in html
    assert 'id="ai-agent-safety-boundaries"' in html
    assert 'id="ai-agent-effective-tools"' in html
    assert 'id="ai-agent-audit-overview"' in html
    assert 'id="ai-agent-audit-scan-btn"' in html
    assert 'id="ai-agent-write-tools-panel" hidden aria-hidden="true" data-agent-internal-tool-panel="true"' in html
    assert 'id="ai-agent-comfyui-prompt"' in html
    assert 'id="ai-agent-comfyui-vae"' in html
    assert 'id="ai-agent-comfyui-generate-btn"' in html
    assert 'id="s-module-ai-agent-min-role"' in html
    assert "/js/37-ai-agent.js?v=20260612-ai-agent-natural-comfyui" in html
    assert "/js/90-bootstrap.js?v=20260611-ai-agent-comfyui-write-tool" in html

    assert '"ai-agent": "feature_ai_agent_enabled"' in core_js
    assert "normalizeModuleSettingKey(moduleKey)" in core_js
    assert '"module_ai_agent_min_role": settings.get("module_ai_agent_min_role")' in public_routes_py
    assert 'tab-module-ai-agent' in core_js
    assert 'canAccessModule("ai-agent")' in core_js
    assert 'switchModuleTab("ai-agent")' in bootstrap_js
    assert 'loadAiAgentStatus({ force: true })' in bootstrap_js
    assert 'loadAiAgentAuditStatus' in bootstrap_js
    assert 'runAiAgentAuditScan' in bootstrap_js
    assert 'runAiAgentComfyuiGenerate' in bootstrap_js

    assert 'const canAccessAiAgent = !!currentUser && canAccessModule("ai-agent");' in admin_js
    assert 'modAiAgent.classList.toggle("active", normTab === "ai-agent")' in admin_js
    assert 'ai_agent_api_key_clear' in admin_js
    assert 'ai_agent_operation_mode' in admin_js
    assert 'ai_agent_allowed_models' in admin_js
    assert 'ai_agent_allowed_tools' in admin_js
    assert 'ai_agent_audit_auto_block_suspect_ip' in admin_js
    assert 'feature_ai_agent_enabled' in admin_js

    assert 'API + "/ai-agent/status"' in ai_agent_js
    assert 'API + "/ai-agent/chat"' in ai_agent_js
    assert 'API + "/ai-agent/audit-status"' in ai_agent_js
    assert 'API + "/ai-agent/audit-scan"' in ai_agent_js
    assert 'API + "/ai-agent/write-tools/execute"' in ai_agent_js
    assert 'tool: "write_comfyui_generate"' in ai_agent_js
    assert 'confirm: "EXECUTE"' in ai_agent_js
    assert 'aiAgentCanRunWriteTool("write_comfyui_generate")' in ai_agent_js
    assert "function aiAgentParseComfyuiGenerateRequest" in ai_agent_js
    assert "aiAgentParseComfyuiGenerateRequest(prompt)" in ai_agent_js
    assert "aiAgentFillComfyuiToolForm(directComfyuiArgs)" in ai_agent_js
    assert "function aiAgentAnalyzeImageForComfyui" in ai_agent_js
    assert "function aiAgentAnalyzeTextForComfyui" in ai_agent_js
    assert "function aiAgentVisionModel" in ai_agent_js
    assert "function aiAgentImageAnalysisError" in ai_agent_js
    assert "function aiAgentReadonlyIntent" in ai_agent_js
    assert "aiAgentReadonlyIntent(prompt)" in ai_agent_js
    assert 'scope: "comfyui"' in ai_agent_js
    assert 'scope: "remote_download"' in ai_agent_js
    assert 'scope: "resources"' in ai_agent_js
    assert 'scope: "attack_diag"' in ai_agent_js
    assert "const selectedModel = aiAgentVisionModel();" in ai_agent_js
    assert "qwen3-vl" in ai_agent_js
    assert "圖片分析後端目前不可用" in ai_agent_js
    assert "圖片分析與提示詞生成中" in ai_agent_js
    assert "生圖需求解析中" in ai_agent_js
    assert "await aiAgentAnalyzeImageForComfyui(userText)" in ai_agent_js
    assert "await aiAgentAnalyzeTextForComfyui(userText)" in ai_agent_js
    assert "await runAiAgentComfyuiGenerate(analyzed.args)" in ai_agent_js
    assert "aiAgentConversationStorageKey" in ai_agent_js
    assert "localStorage.setItem" in ai_agent_js
    assert 'official_workflow_id = "origin_sdxl_txt2img"' in ai_agent_js
    assert 'ai-agent-write-tools-panel' in ai_agent_js
    assert 'panel.hidden = true;' in ai_agent_js
    assert '對話解析後會直接送出' in ai_agent_js
    assert "OpenAI-compatible" in ai_agent_js
    assert "Hermes Agent" in ai_agent_js
    assert "Hermes API 已連線" not in ai_agent_js
    assert "AI Agent 後端已連線" not in ai_agent_js
    assert 'operation_mode_policy' in ai_agent_js
    assert 'safety_boundaries' in ai_agent_js
    assert 'ai-agent-effective-tools' in ai_agent_js
    assert 'image_url' in ai_agent_js
    assert 'hackme:account-context-changed' in ai_agent_js

    assert ".ai-agent-layout" in css
    assert ".ai-agent-thread" in css
    assert ".ai-agent-tool-panel" in css
    assert "@media (max-width: 640px)" in css


def test_ai_agent_root_quick_settings_use_reserved_pricing_key():
    quick_js = _read("public/js/01-root-quick-settings.js")

    assert 'item_key: "ai_agent_task_basic"' in quick_js
    assert '"ai-agent": {' in quick_js
    assert 'pricingKeys: ["ai_agent_task_basic"]' in quick_js
    assert 'id: "s-feature-ai-agent-enabled"' in quick_js
    assert 'id: "s-module-ai-agent-min-role"' in quick_js
    assert 'id: "s-ai-agent-operation-mode"' in quick_js
    assert 'id: "s-ai-agent-allowed-models"' in quick_js
    assert 'id: "s-ai-agent-allowed-tools"' in quick_js
