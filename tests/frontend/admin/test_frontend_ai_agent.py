import re
import warnings
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def _warn_if_asset_cache_version_changed(html, asset_path, expected_version):
    match = re.search(rf'{re.escape(asset_path)}\?v=([^"]+)', html)
    assert match, f"{asset_path}?v= is not referenced"
    actual_version = match.group(1)
    if actual_version != expected_version:
        warnings.warn(
            f"{asset_path} cache-bust version changed: expected {expected_version!r}, got {actual_version!r}",
            stacklevel=2,
        )


def test_ai_agent_module_frontend_is_wired_as_independent_feature():
    html = _read("public/index.html")
    core_js = _read("public/js/00-core.js")
    admin_js = _read("public/js/50-admin.js")
    bootstrap_js = _read("public/js/90-bootstrap.js")
    ai_agent_js = _read("public/js/37-ai-agent.js")
    public_routes_py = _read("routes/public.py")
    ai_agent_routes_py = _read("routes/ai_agent.py")
    backpressure_py = _read("services/server/backpressure.py")
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
    _warn_if_asset_cache_version_changed(html, "/js/37-ai-agent.js", "20260623-ai-agent-live-models-v1")
    _warn_if_asset_cache_version_changed(html, "/js/90-bootstrap.js", "20260611-ai-agent-comfyui-write-tool")

    assert '"ai-agent": "feature_ai_agent_enabled"' in core_js
    assert '/js/00-core.js?v=20260623-site-config-retry' in html
    assert "site config load skipped after retry" in core_js
    assert "site config load failed" not in core_js
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
    assert "負面詞|反向提示詞|反向詞" in ai_agent_js
    assert "function aiAgentLooksLikeComfyuiPromptLine" in ai_agent_js
    assert "function aiAgentLooksLikeComfyuiModelLine" in ai_agent_js
    assert "aiAgentPlanToolAction(plannerText" in ai_agent_js
    assert "aiAgentExecuteToolPlan(plan, plannerText" in ai_agent_js
    assert "write_tool=執行 context.effective_tools" in ai_agent_js
    assert "function aiAgentPlannerToolSchemas" in ai_agent_js
    assert "function aiAgentWriteToolSpecMap" in ai_agent_js
    assert "domain: aiAgentToolDomain(tool)" in ai_agent_js
    assert "required: Array.isArray(tool.required)" in ai_agent_js
    assert "body_fields: Array.isArray(tool.body_fields)" in ai_agent_js
    assert "async function aiAgentRunGenericWriteTool" in ai_agent_js
    assert "async function aiAgentPostWriteToolExecute" in ai_agent_js
    assert "function aiAgentServerBusyDelayMs" in ai_agent_js
    assert "retry_after_seconds" in ai_agent_js
    assert "等待 backpressure 重試" in ai_agent_js
    assert "function aiAgentScrollThreadToBottom" in ai_agent_js
    assert "host.scrollTop = host.scrollHeight" in ai_agent_js
    assert 'requestAnimationFrame(scroll)' in ai_agent_js
    assert "function aiAgentAnalyzeImageForComfyui" in ai_agent_js
    assert "function aiAgentAnalyzeTextForComfyui" in ai_agent_js
    assert "function aiAgentComfyuiTextHasSubject" in ai_agent_js
    assert "不會自行沿用前文、記憶或模型猜提示詞" in ai_agent_js
    assert "function aiAgentVisionModel" in ai_agent_js
    assert "function updateAiAgentModelStateLabel" in ai_agent_js
    assert "async function aiAgentRefreshModelState" in ai_agent_js
    assert 'const statusRes = await apiFetch(API + "/ai-agent/status"' in ai_agent_js
    assert 'const modelsRes = await apiFetch(API + "/ai-agent/models"' in ai_agent_js
    assert "設定模型不在目前 /models 清單" in ai_agent_js
    assert "模型：沒有符合允許清單的可用模型" in ai_agent_js
    assert "模型：尚未取得 /models 清單" in ai_agent_js
    assert "目前沒有可用的文字模型。請確認 AI Agent 後端 /models 有回傳可用模型後再試。" in ai_agent_js
    assert "模型：${settings.model" not in ai_agent_js
    assert "if (id && !modelIds.includes(id)) modelIds.unshift(id)" not in ai_agent_js
    assert 'select.innerHTML = `<option value="${sanitize(fallback)}"' not in ai_agent_js
    assert "await aiAgentRefreshModelState();" in ai_agent_js
    assert "unavailableModelIds: new Set()" in ai_agent_js
    assert "AI_AGENT_STATE.unavailableModelIds?.has(id)" in ai_agent_js
    assert "function aiAgentImageModelUnavailable" in ai_agent_js
    assert "function aiAgentMarkModelUnavailable" in ai_agent_js
    assert "json?.status || status" in ai_agent_js
    assert "圖片理解模型不可用或已下架" in ai_agent_js
    assert "aiAgentMarkModelUnavailable(selectedModel" in ai_agent_js
    assert "function aiAgentImageAnalysisError" in ai_agent_js
    assert "function aiAgentImageTransportError" in ai_agent_js
    assert "圖片分析請求傳輸失敗" in ai_agent_js
    assert "function aiAgentNormalizeReadonlyScope" in ai_agent_js
    assert "async function aiAgentRunReadonlyQuery" in ai_agent_js
    assert "describe.*image" in ai_agent_js
    assert "參考.*圖|照.*圖" not in ai_agent_js
    assert 'scope: "comfyui"' in ai_agent_js
    assert 'scope: "remote_download"' in ai_agent_js
    assert 'scope: "resources"' in ai_agent_js
    assert 'scope: "attack_diag"' in ai_agent_js
    assert "const selectedModel = aiAgentVisionModel();" in ai_agent_js
    assert "qwen3-vl" not in ai_agent_js
    assert "/models 回傳且支援圖片的模型" in ai_agent_js
    assert "圖片分析後端目前不可用" in ai_agent_js
    assert "圖片分析與生圖參數生成中" in ai_agent_js
    assert "ComfyUI 產圖送出失敗（HTTP ${res.status}）" in ai_agent_js
    assert "function aiAgentWatchComfyuiJob" in ai_agent_js
    assert 'toolName === "write_comfyui_generate"' in ai_agent_js
    assert "function aiAgentPollComfyuiJob" in ai_agent_js
    assert "function aiAgentShouldNotifyComfyuiProgress" in ai_agent_js
    assert "function aiAgentMarkComfyuiProgressNotified" in ai_agent_js
    assert "function aiAgentFindComfyuiJobPayload" in ai_agent_js
    assert "function aiAgentResumeComfyuiWatchJobs" in ai_agent_js
    assert "function aiAgentParseComfyuiRerunRequest" in ai_agent_js
    assert "function aiAgentRememberComfyuiSubmit" in ai_agent_js
    assert "function aiAgentPlanToolAction" in ai_agent_js
    assert "function aiAgentExecuteToolPlan" in ai_agent_js
    assert "function aiAgentSelectedTextModel" in ai_agent_js
    assert "function aiAgentNormalizeReadonlyScope" in ai_agent_js
    assert "function aiAgentCleanComfyuiArgs" in ai_agent_js
    assert "function aiAgentRecentImageRefs" in ai_agent_js
    assert "function aiAgentLooksLikeStaleImageEditPrompt" in ai_agent_js
    assert "AI_AGENT_STATE.lastComfyuiArgs?.prompt" in ai_agent_js
    assert "recent_image_refs: aiAgentRecentImageRefs(8)" in ai_agent_js
    assert "source_image_ref, mask_image_ref, denoise_strength" in ai_agent_js
    assert "generation_mode=img2img" in ai_agent_js
    assert "generation_mode=inpaint" in ai_agent_js
    assert "generation_mode=outpaint" in ai_agent_js
    assert "comfyui_generate 的 prompt 不可空白" in ai_agent_js
    assert "不可只複製 context.last_comfyui_args.prompt" in ai_agent_js
    assert '["img2img", "inpaint", "outpaint", "upscale"].includes(generationMode)' in ai_agent_js
    assert "若 inpaint 缺少可用 mask_image_ref，action=clarify" in ai_agent_js
    assert "outpaint_left, outpaint_top, outpaint_right, outpaint_bottom, outpaint_feathering" in ai_agent_js
    assert "function aiAgentRememberComfyuiAttempt" in ai_agent_js
    assert "function aiAgentUpdateComfyuiAttemptFromJob" in ai_agent_js
    assert "function aiAgentLooksLikeComfyuiRecall" in ai_agent_js
    assert "function aiAgentComfyuiRecallSummary" in ai_agent_js
    assert "前幾個版本" in ai_agent_js
    assert "vae\" && autoLike.test(value)" in ai_agent_js
    assert "comfyuiAttemptHistory" in ai_agent_js
    assert 'throw new Error("目前沒有可用的圖片理解模型。請在 AI Agent 模型允許清單加入 /models 回傳且支援圖片的模型後再試。")' in ai_agent_js
    assert 'const selectedModel = mode === "image" ? aiAgentVisionModel() : aiAgentSelectedTextModel();' in ai_agent_js
    assert 'return "";' in ai_agent_js
    assert 'action === "readonly" || action === "comfyui_status"' in ai_agent_js
    assert '/ai-agent/readonly?scope=${encodeURIComponent(requestScope)}&limit=20' in ai_agent_js
    assert 'image_data_url: mode === "image" ? AI_AGENT_STATE.imageDataUrl : ""' in ai_agent_js
    assert 'if ($("ai-agent-mode")) $("ai-agent-mode").value = "image";' in ai_agent_js
    assert 'const hasAttachedImage = !!AI_AGENT_STATE.imageDataUrl;' in ai_agent_js
    assert 'const mode = hasAttachedImage ? "image" : selectedMode;' in ai_agent_js
    assert "若 input_mode=image，請用語意判斷使用者是要圖片問答、圖片分析產 prompt，還是要求用附圖執行生圖" in ai_agent_js
    assert "若 input_mode=image 且使用者意圖依上下文仍不明，請輸出 chat 或 clarify；不得設定 execute_write=true" in ai_agent_js
    assert "若 action=write_tool 且使用者明確要求建立、更新、刪除、執行、下載、轉帳、交易或治理處置，execute_write 必須是 true" in ai_agent_js
    assert "context.effective_tools[] 會提供每個站內工具的 domain, label, description, method, required, path_params, body_fields, query_fields, arg_hint" in ai_agent_js
    assert "args 對 write_tool 必須只使用 context.effective_tools 中該工具 schema 的 required/path_params/body_fields/query_fields canonical 欄位" in ai_agent_js
    assert "站內所有功能需優先從 context.effective_tools 的 domain/label/description/schema 語意選 tool" in ai_agent_js
    assert "若 schema.required 缺少且無法從上下文推得，action=clarify" in ai_agent_js
    assert "未確認寫入，已停止執行" in ai_agent_js
    assert "規劃結果未確認這是可執行寫入，所以沒有執行" in ai_agent_js
    assert "action=readonly 並 readonly_scope=all" in ai_agent_js
    assert "effective_tools" in ai_agent_js
    assert "writable_tools" in ai_agent_js
    assert "不要用關鍵字索引決策" in ai_agent_js
    assert "input_mode=image" in ai_agent_js
    assert "圖片分析與生圖參數生成中" in ai_agent_js
    assert "comfyui_rerun=沿用上一筆生圖參數" in ai_agent_js
    assert "理解需求與規劃工具中" in ai_agent_js
    assert "AI_AGENT_STATE.comfyuiSubmittedJobs" in ai_agent_js
    assert "directComfyuiArgs" not in ai_agent_js
    assert "rerunComfyuiArgs" not in ai_agent_js
    assert "aiAgentHasPendingComfyuiClarification" not in ai_agent_js
    assert "aiAgentLooksLikeImageDescription" not in ai_agent_js
    assert "跑出結果或目前進度" in ai_agent_js
    assert "再來" in ai_agent_js
    assert "接回 ComfyUI 任務進度追蹤" in ai_agent_js
    assert "ComfyUI 產圖進度更新" in ai_agent_js
    assert "ComfyUI 任務仍在佇列中" in ai_agent_js
    assert "function aiAgentComfyuiResultSummary" in ai_agent_js
    assert "function aiAgentComfyuiCompletionMessage" in ai_agent_js
    assert "function aiAgentHydrateComfyuiMessageImages" in ai_agent_js
    assert "function aiAgentRenderMessageImages" in ai_agent_js
    assert "comfyui/image-preview" in ai_agent_js
    assert "接下來要我怎麼處理？" in ai_agent_js
    assert "修改參數重跑" in ai_agent_js
    assert "儲存或加入收藏" in ai_agent_js
    assert "發文分享" in ai_agent_js
    assert "await aiAgentAnalyzeImageForComfyui(userText)" in ai_agent_js
    assert "await runAiAgentComfyuiGenerate(args)" in ai_agent_js
    assert "if (!plan) {" in ai_agent_js
    assert "await aiAgentExecuteToolPlan(plan, plannerText, input" in ai_agent_js
    assert "aiAgentConversationStorageKey" in ai_agent_js
    assert 'API + "/ai-agent/conversation"' in ai_agent_js
    assert "localStorage.setItem" not in ai_agent_js
    assert "ALLOW_WRITE_ONCE" in ai_agent_js
    assert "elevate_once" in ai_agent_js
    assert 'official_workflow_id = "origin_sdxl_txt2img"' in ai_agent_js
    assert 'ai-agent-write-tools-panel' in ai_agent_js
    assert 'ai-agent-tool-selector' in html
    assert 'ai-agent-tool-selector-list' in html
    assert 'include_all=1' in ai_agent_js
    assert 'await loadAiAgentWriteToolCatalog({ force: false }).catch(() => undefined);' in ai_agent_js
    assert 'ai_agent_allowed_tools: allowedTools' in ai_agent_js
    assert '"__none__"' in ai_agent_js
    assert 'setAiAgentToolSelection("comfyui")' in ai_agent_js
    assert ".ai-agent-tool-selector" in css
    assert 'panel.hidden = true;' in ai_agent_js
    assert '對話解析後會直接送出' in ai_agent_js
    assert "OpenAI-compatible" in ai_agent_js
    assert "Local AI Backend" in ai_agent_js
    assert "Hermes Agent" not in ai_agent_js
    assert "Hermes API 已連線" not in ai_agent_js
    assert "AI Agent 後端已連線" not in ai_agent_js
    assert 'operation_mode_policy' in ai_agent_js
    assert 'safety_boundaries' in ai_agent_js
    assert 'ai-agent-effective-tools' in ai_agent_js
    assert 'image_url' in ai_agent_js
    assert 'hackme:account-context-changed' in ai_agent_js
    assert 'environ_base={"hackme.internal_dispatch": "ai_agent_write_tool"}' in ai_agent_routes_py
    assert 'request.environ.get("hackme.internal_dispatch") == "ai_agent_write_tool"' in backpressure_py

    assert ".ai-agent-layout" in css
    assert ".ai-agent-thread" in css
    assert ".ai-agent-image-results" in css
    assert ".ai-agent-image-result img" in css
    assert "grid-template-columns: repeat(auto-fit, minmax(116px, 156px));" in css
    assert "max-height: 156px;" in css
    assert "object-fit: contain;" in css
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
