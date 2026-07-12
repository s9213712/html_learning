from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_ai_agent_launch_preflight_is_dry_run_unless_root_explicitly_confirms_go_live():
    ai_agent_js = _read("public/js/37-ai-agent.js")

    assert "function aiAgentLaunchAutoSwitchRequested" in ai_agent_js
    assert "args.auto_switch = aiAgentLaunchAutoSwitchRequested(raw);" in ai_agent_js
    assert "if (args.auto_switch) args.confirm = \"GO_LIVE\";" in ai_agent_js
    assert "return /\\bGO_LIVE\\b/i.test(raw);" in ai_agent_js
    assert "中文的立即、確認、正式上線或一般『上線前檢查』都不能替代 GO_LIVE" in ai_agent_js
    assert "args={target_mode:'production', auto_switch:false, force_audit:true}" in ai_agent_js
    assert "function aiAgentEnforceLaunchPlanConfirmation" in ai_agent_js
    assert "auto_switch: explicitGoLive" in ai_agent_js
    assert "else delete repaired.args.confirm;" in ai_agent_js


def test_ai_agent_write_plans_require_affirmative_user_intent_and_treat_quoted_commands_as_data():
    ai_agent_js = _read("public/js/37-ai-agent.js")

    assert "function aiAgentUserTextIsNonExecutingContext" in ai_agent_js
    assert "function aiAgentUserTextExplicitlyRequestsWrite" in ai_agent_js
    assert "model output is never user consent" in ai_agent_js
    assert "Model confidence is untrusted metadata" in ai_agent_js
    assert "localPlan.tool !== plannedTool && localSchema" in ai_agent_js
    assert "localPlan.confidence >= Number(repaired.confidence" not in ai_agent_js
    assert "return aiAgentUserTextExplicitlyRequestsWrite(raw);" in ai_agent_js
    assert "文件|範例|例子|示例|客服|訊息|內容|文字|引用" in ai_agent_js
    assert "translate this|explain how|show me how" in ai_agent_js
    confirmed_write = ai_agent_js.split("function aiAgentPlanConfirmedWrite", 1)[1].split("function ", 1)[0]
    assert "plan?.execute_write === true" not in confirmed_write
    assert "args.confirm_billing === true" not in confirmed_write


def test_ai_agent_local_financial_parser_does_not_confuse_field_names_with_market_orders():
    ai_agent_js = _read("public/js/37-ai-agent.js")

    assert 'args.order_type = /市價|\\bmarket(?:\\s+order)?\\b/i.test(raw) ? "market" : "limit";' in ai_agent_js
    assert 'args.order_type = /市價|market/i.test(raw)' not in ai_agent_js
    assert 'explicitStrategy || (/均線|moving\\s*average|\\bma\\b/i.test(raw) ? "moving_average" : "default")' in ai_agent_js
    fast_path = ai_agent_js.split("function aiAgentLocalFastPathAllowed", 1)[1].split("function aiAgentEnforceLaunchPlanConfirmation", 1)[0]
    assert '"write_trading_place_order"' not in fast_path
    assert '"write_points_wallet_transfer"' not in fast_path
    assert '"write_points_governance_execute"' not in fast_path


def test_ai_agent_mutations_are_not_blindly_retried_and_preflight_errors_are_visible_in_thread():
    ai_agent_js = _read("public/js/37-ai-agent.js")

    assert "function aiAgentWriteToolAutoRetryAllowed" in ai_agent_js
    assert "const attemptLimit = aiAgentWriteToolAutoRetryAllowed(resolvedToolName)" in ai_agent_js
    assert '"write_trading_place_order"' not in ai_agent_js.split("function aiAgentWriteToolAutoRetryAllowed", 1)[1].split("async function", 1)[0]
    assert "function aiAgentRecordChatPreflightFailure" in ai_agent_js
    assert "AI_AGENT_STATE.messages.push({ role: \"assistant\", content: message });" in ai_agent_js
    assert 'policy.risk_level !== "high"' in ai_agent_js


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
    assert '/js/37-ai-agent.js?v=__ASSET_VERSION__' in html
    assert '/js/90-bootstrap.js?v=__ASSET_VERSION__' in html

    assert '"ai-agent": "feature_ai_agent_enabled"' in core_js
    assert '/js/00-core.js?v=__ASSET_VERSION__' in html
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
    assert "圖片理解模型目前無權限或額度不足" in ai_agent_js
    assert "requires a subscription" in ai_agent_js
    assert "upgrade for access" in ai_agent_js
    assert "aiAgentMarkModelUnavailable(selectedModel" in ai_agent_js
    assert "function aiAgentImageAnalysisError" in ai_agent_js
    assert "function aiAgentImageTransportError" in ai_agent_js
    assert "圖片分析請求傳輸失敗" in ai_agent_js
    assert "function aiAgentRenderUsageMeta" in ai_agent_js
    assert "aiAgentMessageWithTokenStats" in ai_agent_js
    assert "total tokens" in ai_agent_js
    assert "tokens/s" in ai_agent_js
    assert "function aiAgentIsTransientChatFailure" in ai_agent_js
    assert "async function aiAgentVisionGateChatFetch" in ai_agent_js
    assert "attempts: 3" in ai_agent_js
    assert "vision gate 第 ${reviewFetch.attempt} 次嘗試成功" in ai_agent_js
    assert "暫時性 vision/cloud/route 錯誤" in ai_agent_js
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
    assert "function aiAgentSetComfyuiIdleSuspend" in ai_agent_js
    assert "function aiAgentStopWatchingComfyuiJob" in ai_agent_js
    assert 'setInactivitySuspendState(' in ai_agent_js
    assert '"AI Agent 產圖追蹤中"' in ai_agent_js
    assert 'toolName === "write_comfyui_generate"' in ai_agent_js
    assert "function aiAgentPollComfyuiJob" in ai_agent_js
    assert "function aiAgentComfyuiRetryDelayMsFromError" in ai_agent_js
    assert "ComfyUI 任務狀態暫時受到伺服器保護限制，會自動重試" in ai_agent_js
    assert "watch.busyRetryCount" in ai_agent_js
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
    assert "function aiAgentInferRecentImageRef" in ai_agent_js
    assert "function aiAgentEnsureComfyuiImageRefs" in ai_agent_js
    assert "function aiAgentPromoteExistingPoseMapControlArgs" in ai_agent_js
    assert "origin_qwen_image_controlnet_2512" in ai_agent_js
    assert "pose/control apply: use existing SDPose pose map" in ai_agent_js
    assert "(?:3|three)\\s*refs?" in ai_agent_js
    assert "(?:^|\\b)3ref\\b" in ai_agent_js
    assert "combine\\s+(?:the\\s+)?refs?" in ai_agent_js
    assert "img2img|i2i|image\\s*to\\s*image" in ai_agent_js
    assert 'sourceImageRef = aiAgentInferRecentImageRef("source")' in ai_agent_js
    assert 'maskImageRef = aiAgentInferRecentImageRef("mask")' in ai_agent_js
    assert 'next.source_image_ref = inferred' in ai_agent_js
    assert "工具規劃器沒有輸出可執行 JSON 決策" in ai_agent_js
    assert "AI Agent 工具規劃失敗" in ai_agent_js
    assert "function aiAgentLooksLikeStaleImageEditPrompt" in ai_agent_js
    assert "AI_AGENT_STATE.lastComfyuiArgs?.prompt" in ai_agent_js
    assert "recent_image_refs: aiAgentRecentImageRefs(8)" in ai_agent_js
    assert "prompt, edit_instruction, edit_prompt, negative_prompt" in ai_agent_js
    assert "source_image_ref" in ai_agent_js
    assert "mask_image_ref" in ai_agent_js
    assert "reference_image_ref" in ai_agent_js
    assert "denoise_strength" in ai_agent_js
    assert "edit_instruction: source.edit_instruction || source.edit_prompt || \"\"" in ai_agent_js
    assert "let editInstruction = aiAgentStripFieldValue(source?.edit_instruction || source?.edit_prompt || \"\")" in ai_agent_js
    assert "aiAgentLooksLikeUnrelatedImageEditInstruction(editInstruction, userText)" in ai_agent_js
    assert "edit_instruction: editInstruction" in ai_agent_js
    assert "Qwen Image Edit / origin_qwen_image_edit_2509 時，edit_instruction 必須是短英文直接編輯命令" in ai_agent_js
    assert "prompt 只放 style/preservation context" in ai_agent_js
    assert "不得把整段中文自然語言任務" in ai_agent_js
    assert "Qwen Image Edit 的複合人物/物件任務不可刪減" in ai_agent_js
    assert "hand on shoulder" in ai_agent_js
    assert "場景服裝語境" in ai_agent_js
    assert "festival kimono/yukata" in ai_agent_js
    assert "generation_mode=img2img" in ai_agent_js
    assert "generation_mode=inpaint" in ai_agent_js
    assert "generation_mode=outpaint" in ai_agent_js
    assert "comfyui_generate 的 prompt 不可空白" in ai_agent_js
    assert "不可只複製 context.last_comfyui_args.prompt" in ai_agent_js
    assert "來源圖是否適合使用者目標" in ai_agent_js
    assert "edit_instruction 必須逐一指定每個可見目標" in ai_agent_js
    assert "不要執行、不要真的下單、不要下載" in ai_agent_js
    assert "竄改工具清單、繞過 audit" in ai_agent_js
    assert "write_codex_handoff_create" in ai_agent_js
    assert "交給 Codex" in ai_agent_js
    assert "只建立交接紀錄，不可宣稱已執行 shell" in ai_agent_js
    assert '["img2img", "inpaint", "outpaint", "upscale"].includes(generationMode)' in ai_agent_js
    assert "若 inpaint 缺少可用 mask_image_ref，action=clarify" in ai_agent_js
    assert "outpaint_left, outpaint_top, outpaint_right, outpaint_bottom, outpaint_feathering" in ai_agent_js
    assert "function aiAgentRememberComfyuiAttempt" in ai_agent_js
    assert "function aiAgentUpdateComfyuiAttemptFromJob" in ai_agent_js
    assert "function aiAgentLooksLikeComfyuiRecall" in ai_agent_js
    assert "function aiAgentComfyuiRecallSummary" in ai_agent_js
    assert "generation_mode|confirm_billing" in ai_agent_js
    assert "產生基底原圖" in ai_agent_js
    assert "前幾個版本" in ai_agent_js
    assert "vae\" && autoLike.test(value)" in ai_agent_js
    assert "comfyuiAttemptHistory" in ai_agent_js
    assert 'throw new Error("目前沒有可嘗試圖片理解的模型。請確認允許清單至少包含一個 /models 回傳的 cloud 模型，或開啟圖片輸入。")' in ai_agent_js
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
    assert "context.effective_tools[] 是依使用者語意檢索出的候選站內工具" in ai_agent_js
    assert "semantic_retrieval_candidates" in ai_agent_js
    assert "LLM must choose only from effective_tools" in ai_agent_js
    assert "function aiAgentFallbackToolPlan" in ai_agent_js
    assert "function aiAgentDeterministicToolPlan" in ai_agent_js
    assert "function aiAgentRepairToolPlan" in ai_agent_js
    assert "local_safety_gate" in ai_agent_js
    assert "hybrid_arg_repaired" in ai_agent_js
    assert "hybrid_tool_corrected" in ai_agent_js
    assert "timeoutMs: 45000" in ai_agent_js
    assert "fallback_error" in ai_agent_js
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
    assert "function aiAgentComfyuiSubmitArgs" in ai_agent_js
    assert 'key.startsWith("agent_review_")' in ai_agent_js
    assert "function aiAgentNormalizeVisionReviewText" in ai_agent_js
    assert "vision review did not return valid JSON" in ai_agent_js
    assert "Return one plain JSON object only" in ai_agent_js
    assert "This is not private raw data" in ai_agent_js
    assert "function aiAgentApplyPairwiseCrossReferenceStage" in ai_agent_js
    assert "function aiAgentSingleReferenceStageFromText" in ai_agent_js
    assert "function aiAgentLooksLikeWrongSingleReferenceInstruction" in ai_agent_js
    assert "clothes reference" in ai_agent_js
    assert "background reference" in ai_agent_js
    assert "if (semanticKey && semanticKey !== key)" in ai_agent_js
    assert "return { item, score: -10000 }" in ai_agent_js
    assert "face|identity|eye shape|mature face|hair color|hairstyle|pose|body pose" in ai_agent_js
    assert "referenceDescriptionCache" in ai_agent_js
    assert "function aiAgentDescribeReferenceForEdit" in ai_agent_js
    assert "function aiAgentPrepareComfyuiArgsForStrategy" in ai_agent_js
    assert "semanticStageKey" in ai_agent_js
    assert ai_agent_js.count("args = await aiAgentPrepareComfyuiArgsForStrategy(args);") >= 2
    assert "function aiAgentBuildReferenceAwareStageInstruction" in ai_agent_js
    assert "function aiAgentCleanStageTargetText" in ai_agent_js
    assert "AI_AGENT_QWEN_EDIT_STYLE_FALLBACK" in ai_agent_js
    assert "function aiAgentStartComfyuiIdleKeepalive" in ai_agent_js
    assert "idleKeepaliveTimer" in ai_agent_js
    assert "stageContract" in ai_agent_js
    assert "Vision suggested refinement, but do not drop the active" in ai_agent_js
    assert "Stay on the ${stageKey} stage until this gate passes" in ai_agent_js
    assert 'next.qwen_edit_profile = "base"' in ai_agent_js
    assert "Do not submit a near-identical preservation pass" in ai_agent_js
    assert "停止同一路徑重送以避免浪費算力" in ai_agent_js
    assert "function aiAgentImagePixelDelta" in ai_agent_js
    assert "function aiAgentDownscaleDataUrlForVision" in ai_agent_js
    assert "aiAgentReferenceDescriptionLooksUnusable" in ai_agent_js
    assert "aiAgentAssertUsableReferenceDescription" in ai_agent_js
    assert "rejectUnusableVision" in ai_agent_js
    assert "pixel_near_identical" in ai_agent_js
    assert "此結果不會被視為通過，也不會消耗 vision token" in ai_agent_js
    assert "function aiAgentScheduleStagedReviewRetry" in ai_agent_js
    assert "candidate-only review" in ai_agent_js
    assert "args.confirm_billing" in ai_agent_js
    assert "請真的使用" in ai_agent_js
    assert "cat\\s+ears?" in ai_agent_js
    assert "visible garment type, fabric, color, drape, and outfit silhouette only" in ai_agent_js
    assert "vision extract current reference traits" in ai_agent_js
    assert "delete next.reference_image_ref;" in ai_agent_js
    assert "vision_text_traits_only" in ai_agent_js
    assert "qwen_reference_force_image2" in ai_agent_js
    assert "stage_guarded_image2" in ai_agent_js
    assert "qwen_reference_image2" in ai_agent_js
    assert "function aiAgentTextRequestsExactReferenceClothes" in ai_agent_js
    assert "function aiAgentApplyExactReferenceClothesIntent" in ai_agent_js
    assert "Put that reference outfit on the source character" in ai_agent_js
    assert "穿到|套到" in ai_agent_js
    assert "qwen_edit_profile = next.qwen_edit_profile || next.qwen_profile || next.profile || \"fast\"" in ai_agent_js
    assert "next.steps = 4;" in ai_agent_js
    assert "next.cfg_scale = 1;" in ai_agent_js
    assert "agent_review_pass_threshold = Math.max(Number(next.agent_review_pass_threshold || 0) || 0, 0.93)" in ai_agent_js
    assert "This exact-outfit request fails if the result only copies rough color/style" in ai_agent_js
    assert "Score <= 0.70 for style-only transfer" in ai_agent_js
    assert "Do not accept a rough style transfer" in ai_agent_js
    assert "use the extracted reference traits only as guarded visual evidence" in ai_agent_js
    assert "agent_review_reference_image_ref" in ai_agent_js
    assert "開始用 vision 模型讀取" in ai_agent_js
    assert "不要只寫 use this reference" in ai_agent_js
    assert "不要只提高 denoise 重送，要改走 pose/control workflow" in ai_agent_js
    assert "function aiAgentBuildPoseControlFallbackArgs" in ai_agent_js
    assert "function aiAgentReferenceLooksLikePoseMap" in ai_agent_js
    assert "function aiAgentPoseControlSourceImageRef" in ai_agent_js
    assert "aiAgentSameImageRef(candidateRef, poseRef)" in ai_agent_js
    assert "aiAgentInferSemanticImageRef(\"source\")?.image_ref" in ai_agent_js
    assert "function aiAgentPoseControlSecondaryReferenceRef" in ai_agent_js
    assert "function aiAgentPoseControlClothesReferenceRef" in ai_agent_js
    assert "function aiAgentApplyPoseControlReferenceRouting" in ai_agent_js
    assert "function aiAgentRequestedPoseControlRef" in ai_agent_js
    assert "function aiAgentShouldPreserveRequestedPoseControl" in ai_agent_js
    assert "function aiAgentApplyRequestedPoseControlArgs" in ai_agent_js
    assert "const requestedPoseRef = aiAgentRequestedPoseControlRef(next);" in ai_agent_js
    assert "const requestedPoseSeedArgs = { ...next };" in ai_agent_js
    assert "next = aiAgentApplyRequestedPoseControlArgs(next, requestedPoseRef, requestedPoseSeedArgs);" in ai_agent_js
    assert "const clothesRef = aiAgentPoseControlClothesReferenceRef(next, poseRef);" in ai_agent_js
    assert "next.reference_image_ref = { ...clothesRef, semantic_key: \"clothes\" };" in ai_agent_js
    assert "item.cloud_file_id" in ai_agent_js
    assert "source?.control_image_ref || source?.control_image_ref_json || source?.control_ref || source?.controlnet?.image_ref" in ai_agent_js
    assert "const sourceRef = aiAgentPoseControlSourceImageRef(args.source_image_ref, poseRef);" in ai_agent_js
    assert "reference_image_ref: args.reference_image_ref" in ai_agent_js
    assert "function aiAgentSanitizePoseControlBasePrompt" in ai_agent_js
    assert "function aiAgentBuildPoseControlApplyArgs" in ai_agent_js
    assert "const useFastProfile = [\"fast\", \"lightning\", \"lite\", \"quick\"].includes(profile);" in ai_agent_js
    assert "const steps = useFastProfile ? 4 : Math.max(20, requestedSteps > 4 ? requestedSteps : 28);" in ai_agent_js
    assert "const cfg = useFastProfile ? 1 : (requestedCfg > 1.2 ? requestedCfg : 4);" in ai_agent_js
    assert "qwen_controlnet_profile: useFastProfile ? \"fast\" : \"base\"" in ai_agent_js
    assert "use existing SDPose pose map as control_image_ref" in ai_agent_js
    assert "use the supplied SDPose/control pose map directly; do not re-describe it with vision" in ai_agent_js
    assert "the pose control map overrides any earlier instruction to preserve the old pose" in ai_agent_js
    assert "const poseSummary = stageKey === \"pose\" ? summary : \"\";" in ai_agent_js
    assert "const wantsPoseControl = workflowId === \"origin_qwen_image_controlnet_2512\" || controlType === \"pose\";" in ai_agent_js
    assert "next.control_image_ref = poseRef;" in ai_agent_js
    assert "aiAgentInferSemanticImageRef(\"pose\")?.image_ref" in ai_agent_js
    assert "aiAgentCleanComfyuiArgs(aiAgentEnsureComfyuiImageRefs(aiAgentPromoteExistingPoseMapControlArgs(aiAgentApplyQwenEditInstructionPrompt(args))))" in ai_agent_js
    assert "args = aiAgentEnsureComfyuiImageRefs(args);" in ai_agent_js
    assert "agent_followup_after_completion" in ai_agent_js
    assert "origin_sdpose_multi_person" in ai_agent_js
    assert "origin_qwen_image_controlnet_2512" in ai_agent_js
    assert "pose map 已完成，開始第二步 Qwen Image ControlNet pose 生成" in ai_agent_js
    assert "if (key.startsWith(\"agent_followup_\")) delete cleaned[key];" in ai_agent_js
    assert "pairwise_reference_merge" in ai_agent_js
    assert "stage 1 chara merge" in ai_agent_js
    assert "For the active stage, also fail if the candidate is nearly unchanged from SOURCE" in ai_agent_js
    assert "comfyui/image-preview" in ai_agent_js
    assert "接下來要我怎麼處理？" in ai_agent_js
    assert "修改參數重跑" in ai_agent_js
    assert "儲存或加入收藏" in ai_agent_js
    assert "發文分享" in ai_agent_js
    assert "await aiAgentAnalyzeImageForComfyui(userText)" in ai_agent_js
    assert "await runAiAgentComfyuiGenerate(args, { operation })" in ai_agent_js
    assert "if (!plan && aiAgentOperationIsCurrent(operation)) {" in ai_agent_js
    assert "if (plan) {" in ai_agent_js
    assert "await aiAgentExecuteToolPlan(plan, plannerText, input" in ai_agent_js
    assert "aiAgentConversationStorageKey" in ai_agent_js
    assert 'API + "/ai-agent/conversation"' in ai_agent_js
    assert "conversationPersistError" in ai_agent_js
    assert "persistRetryCount" in ai_agent_js
    assert "AI Agent conversation persist failed after retries" in ai_agent_js
    assert "void aiAgentPersistConversation(scope, { retryCount: nextRetryCount, snapshot });" in ai_agent_js
    assert "conversationLoadToken" in ai_agent_js
    assert "scope !== AI_AGENT_STATE.accountScope" in ai_agent_js
    assert "persistRetryTimers" in ai_agent_js
    assert "persistInFlight[scope] instanceof Set" in ai_agent_js
    assert "persistControllers" in ai_agent_js
    assert "await Promise.allSettled(pendingPersists);" in ai_agent_js
    assert "aiAgentCancelConversationPersistence(scope, { invalidate: true });" in ai_agent_js
    assert "flushAiAgentConversationBeforeLogout" in ai_agent_js
    assert "localStorage.setItem" not in ai_agent_js
    assert "ALLOW_WRITE_ONCE" in ai_agent_js
    assert "elevate_once" in ai_agent_js
    assert 'official_workflow_id = "origin_sdxl_txt2img"' in ai_agent_js
    assert 'mode !== "txt2img" && cleaned.official_workflow_id === "origin_sdxl_txt2img"' in ai_agent_js
    assert "SDXL 等級" not in ai_agent_js
    assert 'ai-agent-write-tools-panel' in ai_agent_js
    assert 'ai-agent-tool-selector' in html
    assert 'ai-agent-tool-selector-list' in html
    assert 'include_all=1' in ai_agent_js
    assert 'await loadAiAgentWriteToolCatalog({ force: false });' in ai_agent_js
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
    assert ".ai-agent-message-meta" in css
    assert "@media (max-width: 640px)" in css
    assert "37-ai-agent.js?v=__ASSET_VERSION__" in html


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
