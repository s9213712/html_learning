'use strict';

const AI_AGENT_STATE = {
  loaded: false,
  loading: false,
  sending: false,
  sendingTool: false,
  readonlyLoading: false,
  messages: [],
  imageDataUrl: "",
  settings: {},
  actor: {},
  audit: {},
  modelIds: [],
  sessionId: "",
  accountScope: "",
};

const AI_AGENT_OPERATION_MODE_LABELS = {
  readonly: "唯讀",
  assist: "協助",
  write: "執行寫入",
  audit: "僅審計",
};

function aiAgentCurrentAccountScope() {
  return typeof getCurrentAccountStorageScope === "function"
    ? getCurrentAccountStorageScope()
    : "anonymous";
}

function aiAgentConversationStorageKey(scope = AI_AGENT_STATE.accountScope || aiAgentCurrentAccountScope()) {
  return `hackme:ai-agent:conversation:${scope || "anonymous"}`;
}

function aiAgentPersistConversation(scope = AI_AGENT_STATE.accountScope) {
  if (!scope || typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(aiAgentConversationStorageKey(scope), JSON.stringify({
      sessionId: AI_AGENT_STATE.sessionId || "",
      messages: AI_AGENT_STATE.messages.slice(-80),
      updatedAt: Date.now(),
    }));
  } catch (err) {
    // localStorage can be disabled or full; chat still works without persistence.
  }
}

function aiAgentLoadConversation(scope) {
  AI_AGENT_STATE.messages = [];
  AI_AGENT_STATE.sessionId = "";
  AI_AGENT_STATE.imageDataUrl = "";
  if (!scope || typeof localStorage === "undefined") return;
  try {
    const raw = localStorage.getItem(aiAgentConversationStorageKey(scope));
    if (!raw) return;
    const parsed = JSON.parse(raw);
    const messages = Array.isArray(parsed?.messages) ? parsed.messages : [];
    AI_AGENT_STATE.messages = messages
      .filter((message) => message && ["user", "assistant"].includes(message.role))
      .slice(-80)
      .map((message) => ({
        role: message.role,
        content: String(message.content || "").slice(0, 20000),
      }));
    AI_AGENT_STATE.sessionId = String(parsed?.sessionId || "").slice(0, 120);
  } catch (err) {
    AI_AGENT_STATE.messages = [];
    AI_AGENT_STATE.sessionId = "";
  }
}

function aiAgentResetScopeState() {
  const nextScope = aiAgentCurrentAccountScope();
  const previousScope = AI_AGENT_STATE.accountScope;
  if (previousScope && previousScope !== nextScope) {
    aiAgentPersistConversation(previousScope);
  }
  AI_AGENT_STATE.accountScope = nextScope;
  if (!previousScope || previousScope !== nextScope) {
    aiAgentLoadConversation(nextScope);
    renderAiAgentThread();
  }
}

function aiAgentEnsureSessionId() {
  if (AI_AGENT_STATE.sessionId) return AI_AGENT_STATE.sessionId;
  AI_AGENT_STATE.sessionId = `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  return AI_AGENT_STATE.sessionId;
}

function setAiAgentMessage(text = "", kind = "info") {
  const el = $("ai-agent-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = "msg";
  if (text) el.classList.add("show", kind === "err" ? "err" : kind === "ok" ? "ok" : "info");
}

function isMockAiAgentReply(text) {
  const compact = String(text || "")
    .toLowerCase()
    .replace(/[^0-9a-zA-Z\u4e00-\u9fff]/g, "");
  if (!compact) return false;
  const hasTraditional = compact.includes("已收到你的請求");
  const hasSimplified = compact.includes("已收到你的请求");
  return compact.includes("mockhermesresponse") && (hasTraditional || hasSimplified);
}

function aiAgentNumberInput(id, fallback, { min = null, max = null, integer = false } = {}) {
  const raw = ($(id)?.value || "").trim();
  if (!raw) return fallback;
  let value = Number(raw);
  if (!Number.isFinite(value)) return fallback;
  if (integer) value = Math.round(value);
  if (min !== null) value = Math.max(min, value);
  if (max !== null) value = Math.min(max, value);
  return value;
}

function aiAgentClampNumber(value, fallback, { min = null, max = null, integer = false } = {}) {
  let parsed = Number(value);
  if (!Number.isFinite(parsed)) parsed = fallback;
  if (integer) parsed = Math.round(parsed);
  if (min !== null) parsed = Math.max(min, parsed);
  if (max !== null) parsed = Math.min(max, parsed);
  return parsed;
}

function aiAgentStripFieldValue(value) {
  return String(value || "")
    .trim()
    .replace(/^[："“”"'`]+/, "")
    .replace(/[："“”"'`]+$/, "")
    .trim();
}

function aiAgentLineValue(text, patterns) {
  const lines = String(text || "").split(/\r?\n/);
  for (const line of lines) {
    for (const pattern of patterns) {
      const match = line.match(pattern);
      if (match) return aiAgentStripFieldValue(match[1] || "");
    }
  }
  return "";
}

function aiAgentParseComfyuiGenerateRequest(text) {
  const raw = String(text || "").trim();
  if (!raw) return null;
  const lower = raw.toLowerCase();
  const wantsImage = /生圖|產圖|生成圖片|畫圖|畫一張|做一張|comfyui|txt2img|t2i|sdxl|text\s*to\s*image/.test(lower);
  if (!wantsImage) return null;
  const prompt = aiAgentLineValue(raw, [
    /^\s*(?:提示詞|prompt|positive prompt)\s*[:：]\s*(.+)$/i,
  ]);
  if (!prompt) return null;
  const args = { prompt, confirm_billing: true };
  const negative = aiAgentLineValue(raw, [
    /^\s*(?:負面提示詞|negative prompt|negative|neg)\s*[:：]\s*(.+)$/i,
  ]);
  if (negative) args.negative_prompt = negative;
  const size = raw.match(/(?:size|尺寸|解析度)?\s*[:：]?\s*(\d{3,4})\s*[xX*×＊]\s*(\d{3,4})/i);
  if (size) {
    args.width = aiAgentClampNumber(size[1], 1024, { min: 256, max: 2048, integer: true });
    args.height = aiAgentClampNumber(size[2], 1024, { min: 256, max: 2048, integer: true });
  }
  const model = aiAgentLineValue(raw, [
    /^\s*(?:models?|模型|checkpoint|ckpt)\s*[:：]\s*(.+)$/i,
  ]);
  if (model) args.checkpoint = model;
  const cfg = raw.match(/(?:^|\n)\s*(?:cfg(?:[_\s-]?scale)?)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)/i);
  if (cfg) args.cfg_scale = aiAgentClampNumber(cfg[1], 7, { min: 1, max: 20 });
  const steps = raw.match(/(?:^|\n)\s*(?:steps?|步數)\s*[:：]?\s*(\d+)/i);
  if (steps) args.steps = aiAgentClampNumber(steps[1], 20, { min: 1, max: 80, integer: true });
  const batch = raw.match(/(?:^|\n)\s*(?:batch(?:[_\s-]?size)?|張數|數量)\s*[:：]?\s*(\d+)/i);
  if (batch) args.batch_size = aiAgentClampNumber(batch[1], 1, { min: 1, max: 8, integer: true });
  const seed = raw.match(/(?:^|\n)\s*(?:seed|種子)\s*[:：]?\s*(-?\d+)/i);
  if (seed) args.seed = aiAgentClampNumber(seed[1], -1, { integer: true });
  const sampler = aiAgentLineValue(raw, [
    /^\s*(?:sampler|採樣器|取樣器)\s*[:：]\s*(.+)$/i,
  ]);
  if (sampler) args.sampler = sampler;
  const scheduler = aiAgentLineValue(raw, [
    /^\s*(?:scheduler|排程器)\s*[:：]\s*(.+)$/i,
  ]);
  if (scheduler) args.scheduler = scheduler;
  const vae = aiAgentLineValue(raw, [
    /^\s*(?:vae)\s*[:：]\s*(.+)$/i,
  ]);
  if (vae) args.vae = vae;
  if (/sdxl|sdxl\s*t2i|sdxl[-_\s]*txt2img|sdxl[-_\s]*text\s*to\s*image/i.test(raw)) {
    args.official_workflow_id = "origin_sdxl_txt2img";
  }
  return args;
}

function aiAgentWantsComfyuiGeneration(text) {
  const raw = String(text || "");
  if (/(查|看|顯示|確認|status|progress|進度|狀態|queue|running|pending|任務)/i.test(raw)
    && /(產圖|生圖|comfyui|generation|下載|download)/i.test(raw)) {
    return false;
  }
  return /生圖|產圖|生成圖片|畫圖|畫一張|做一張|參考.*圖|照.*圖|comfyui|txt2img|t2i|sdxl|text\s*to\s*image/i.test(raw);
}

function aiAgentParseComfyuiOptionOverrides(text) {
  const raw = String(text || "");
  const args = {};
  const size = raw.match(/(?:size|尺寸|解析度)?\s*[:：]?\s*(\d{3,4})\s*[xX*×＊]\s*(\d{3,4})/i);
  if (size) {
    args.width = aiAgentClampNumber(size[1], 1024, { min: 256, max: 2048, integer: true });
    args.height = aiAgentClampNumber(size[2], 1024, { min: 256, max: 2048, integer: true });
  }
  const model = aiAgentLineValue(raw, [
    /^\s*(?:models?|模型|checkpoint|ckpt)\s*[:：]\s*(.+)$/i,
  ]);
  if (model) args.checkpoint = model;
  const cfg = raw.match(/(?:^|\n)\s*(?:cfg(?:[_\s-]?scale)?)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)/i);
  if (cfg) args.cfg_scale = aiAgentClampNumber(cfg[1], 7, { min: 1, max: 20 });
  const steps = raw.match(/(?:^|\n)\s*(?:steps?|步數)\s*[:：]?\s*(\d+)/i);
  if (steps) args.steps = aiAgentClampNumber(steps[1], 20, { min: 1, max: 80, integer: true });
  const vae = aiAgentLineValue(raw, [
    /^\s*(?:vae)\s*[:：]\s*(.+)$/i,
  ]);
  if (vae) args.vae = vae;
  if (/sdxl|sdxl\s*t2i|sdxl[-_\s]*txt2img|sdxl[-_\s]*text\s*to\s*image/i.test(raw)) {
    args.official_workflow_id = "origin_sdxl_txt2img";
  }
  return args;
}

function aiAgentExtractJsonObject(text) {
  const raw = String(text || "").trim();
  if (!raw) return null;
  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidates = [];
  if (fenced) candidates.push(fenced[1]);
  candidates.push(raw);
  const first = raw.indexOf("{");
  const last = raw.lastIndexOf("}");
  if (first >= 0 && last > first) candidates.push(raw.slice(first, last + 1));
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === "object") return parsed;
    } catch (err) {}
  }
  return null;
}

function aiAgentNormalizeAnalysisArgs(parsed, userText) {
  const source = parsed?.arguments && typeof parsed.arguments === "object" ? parsed.arguments : parsed;
  const prompt = aiAgentStripFieldValue(source?.prompt || source?.positive_prompt || source?.comfyui_prompt || "");
  if (!prompt) throw new Error("圖片分析沒有產生可用提示詞");
  const args = {
    prompt,
    negative_prompt: aiAgentStripFieldValue(source?.negative_prompt || source?.negative || ""),
    width: source?.width,
    height: source?.height,
    steps: source?.steps,
    cfg_scale: source?.cfg_scale ?? source?.cfg,
    batch_size: source?.batch_size,
    checkpoint: aiAgentStripFieldValue(source?.checkpoint || source?.model || ""),
    vae: aiAgentStripFieldValue(source?.vae || ""),
    sampler: aiAgentStripFieldValue(source?.sampler || ""),
    scheduler: aiAgentStripFieldValue(source?.scheduler || ""),
    official_workflow_id: source?.official_workflow_id || "",
    confirm_billing: true,
    ...aiAgentParseComfyuiOptionOverrides(userText),
  };
  Object.keys(args).forEach((key) => {
    if (args[key] === "" || args[key] === undefined || args[key] === null) delete args[key];
  });
  return args;
}

async function aiAgentAnalyzeImageForComfyui(userText) {
  const selectedModel = aiAgentVisionModel();
  const selectableModels = aiAgentSelectableModels();
  if (selectableModels.length && !selectableModels.includes(selectedModel)) {
    throw new Error("請從模型選單選擇可用模型後再做圖片分析。");
  }
  const analysisPrompt = [
    "請先分析使用者附上的圖片，產生可用於 ComfyUI text-to-image 的提示詞。",
    "請只輸出 JSON，不要 Markdown，不要表格，不要操作教學。",
    "JSON 欄位：prompt, negative_prompt, width, height, steps, cfg_scale, checkpoint, vae, sampler, scheduler, official_workflow_id。",
    "如果使用者文字指定尺寸、模型、CFG、VAE 或 SDXL T2I，請保留那些指定。",
    `使用者需求：${userText || "參考圖片產生相似風格圖片"}`,
  ].join("\n");
  const started = performance.now();
  const res = await apiFetch(API + "/ai-agent/chat", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: aiAgentEnsureSessionId(),
      model: selectedModel,
      mode: "image",
      messages: [{ role: "user", content: analysisPrompt }],
      image_data_url: AI_AGENT_STATE.imageDataUrl,
    }),
  });
  const json = await res.json().catch(() => ({}));
  const content = json?.message?.content || json?.msg || "";
  if (!res.ok || !json.ok || isMockAiAgentReply(content)) {
    throw new Error(aiAgentImageAnalysisError(json, res.status));
  }
  const parsed = aiAgentExtractJsonObject(content);
  const args = aiAgentNormalizeAnalysisArgs(parsed || { prompt: content }, userText);
  return {
    args,
    analysis: content,
    elapsedMs: Math.round(performance.now() - started),
  };
}

async function aiAgentAnalyzeTextForComfyui(userText) {
  const selectedModel = ($("ai-agent-model")?.value || "").trim() || AI_AGENT_STATE.settings?.model || "";
  const selectableModels = aiAgentSelectableModels();
  if (selectableModels.length && selectedModel && !selectableModels.includes(selectedModel)) {
    throw new Error("請從模型選單選擇可用模型後再做生圖解析。");
  }
  const analysisPrompt = [
    "請把使用者的自然語言生圖需求轉成 ComfyUI text-to-image write-tool 參數。",
    "請只輸出 JSON，不要 Markdown，不要表格，不要操作教學。",
    "JSON 欄位：prompt, negative_prompt, width, height, steps, cfg_scale, batch_size, seed, checkpoint, vae, sampler, scheduler, official_workflow_id。",
    "如果使用者提到 SDXL T2I、SDXL txt2img 或文字生圖，official_workflow_id 設為 origin_sdxl_txt2img。",
    "如果使用者指定模型、Checkpoint、VAE、尺寸、CFG、步數或張數，必須保留。",
    "prompt 欄位要是可直接送 ComfyUI 的正向提示詞，不要包含解釋文字。",
    `使用者需求：${userText}`,
  ].join("\n");
  const started = performance.now();
  const res = await apiFetch(API + "/ai-agent/chat", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: aiAgentEnsureSessionId(),
      model: selectedModel,
      mode: "text",
      messages: [{ role: "user", content: analysisPrompt }],
      image_data_url: "",
    }),
  });
  const json = await res.json().catch(() => ({}));
  const content = json?.message?.content || json?.msg || "";
  if (!res.ok || !json.ok || isMockAiAgentReply(content)) {
    throw new Error(json.msg || `生圖需求解析失敗（HTTP ${res.status}）`);
  }
  const parsed = aiAgentExtractJsonObject(content);
  const args = aiAgentNormalizeAnalysisArgs(parsed || { prompt: content }, userText);
  return {
    args,
    analysis: content,
    elapsedMs: Math.round(performance.now() - started),
  };
}

function aiAgentFillComfyuiToolForm(args = {}) {
  const map = {
    "ai-agent-comfyui-prompt": args.prompt,
    "ai-agent-comfyui-negative": args.negative_prompt,
    "ai-agent-comfyui-width": args.width,
    "ai-agent-comfyui-height": args.height,
    "ai-agent-comfyui-steps": args.steps,
    "ai-agent-comfyui-cfg": args.cfg_scale,
    "ai-agent-comfyui-batch-size": args.batch_size,
    "ai-agent-comfyui-seed": args.seed,
    "ai-agent-comfyui-checkpoint": args.checkpoint,
    "ai-agent-comfyui-vae": args.vae,
  };
  Object.entries(map).forEach(([id, value]) => {
    const el = $(id);
    if (!el || value === undefined || value === null || value === "") return;
    el.value = String(value);
  });
}

function aiAgentHasEffectiveTool(toolName) {
  return (AI_AGENT_STATE.settings?.tools || []).some((tool) => tool?.name === toolName);
}

function aiAgentCanRunWriteTool(toolName) {
  return AI_AGENT_STATE.actor?.role === "super_admin"
    && AI_AGENT_STATE.settings?.operation_mode === "write"
    && !!AI_AGENT_STATE.settings?.operation_mode_policy?.write_enabled
    && aiAgentHasEffectiveTool(toolName);
}

function renderAiAgentWriteTools() {
  const panel = $("ai-agent-write-tools-panel");
  const state = $("ai-agent-write-tools-state");
  const form = $("ai-agent-comfyui-tool-form");
  const button = $("ai-agent-comfyui-generate-btn");
  if (!panel) return;
  const isRoot = AI_AGENT_STATE.actor?.role === "super_admin";
  const canRunComfyui = aiAgentCanRunWriteTool("write_comfyui_generate");
  panel.hidden = true;
  panel.setAttribute("aria-hidden", "true");
  if (state) {
    if (!isRoot) {
      state.textContent = "僅 root 可使用 write-tool。";
    } else if (canRunComfyui) {
      state.textContent = "已啟用 write_comfyui_generate；對話解析後會直接送出，並自動附帶 confirm=EXECUTE。";
    } else {
      state.textContent = "需切換為 write 模式，且工具白名單需允許 write_comfyui_generate。";
    }
  }
  if (form) form.classList.toggle("disabled", !canRunComfyui);
  if (button) button.disabled = !canRunComfyui || AI_AGENT_STATE.sendingTool;
}

function aiAgentComfyuiToolArguments(overrides = null) {
  if (overrides && typeof overrides === "object") {
    return {
      ...overrides,
      prompt: String(overrides.prompt || "").trim(),
      negative_prompt: String(overrides.negative_prompt || "").trim(),
      width: aiAgentClampNumber(overrides.width, 1024, { min: 256, max: 2048, integer: true }),
      height: aiAgentClampNumber(overrides.height, 1024, { min: 256, max: 2048, integer: true }),
      steps: aiAgentClampNumber(overrides.steps, 20, { min: 1, max: 80, integer: true }),
      cfg_scale: aiAgentClampNumber(overrides.cfg_scale, 7, { min: 1, max: 20 }),
      batch_size: aiAgentClampNumber(overrides.batch_size, 1, { min: 1, max: 8, integer: true }),
      confirm_billing: true,
    };
  }
  const prompt = ($("ai-agent-comfyui-prompt")?.value || "").trim();
  if (!prompt) throw new Error("請先輸入提示詞");
  const args = {
    prompt,
    negative_prompt: ($("ai-agent-comfyui-negative")?.value || "").trim(),
    width: aiAgentNumberInput("ai-agent-comfyui-width", 1024, { min: 256, max: 2048, integer: true }),
    height: aiAgentNumberInput("ai-agent-comfyui-height", 1024, { min: 256, max: 2048, integer: true }),
    steps: aiAgentNumberInput("ai-agent-comfyui-steps", 20, { min: 1, max: 80, integer: true }),
    cfg_scale: aiAgentNumberInput("ai-agent-comfyui-cfg", 7, { min: 1, max: 20 }),
    batch_size: aiAgentNumberInput("ai-agent-comfyui-batch-size", 1, { min: 1, max: 8, integer: true }),
    confirm_billing: true,
  };
  const seedRaw = ($("ai-agent-comfyui-seed")?.value || "").trim();
  if (seedRaw) args.seed = aiAgentNumberInput("ai-agent-comfyui-seed", -1, { integer: true });
  const checkpoint = ($("ai-agent-comfyui-checkpoint")?.value || "").trim();
  if (checkpoint) args.checkpoint = checkpoint;
  const vae = ($("ai-agent-comfyui-vae")?.value || "").trim();
  if (vae) args.vae = vae;
  return args;
}

async function runAiAgentComfyuiGenerate(overrides = null) {
  if (AI_AGENT_STATE.sendingTool) return;
  if (!aiAgentCanRunWriteTool("write_comfyui_generate")) {
    setAiAgentMessage("目前不可執行 ComfyUI write-tool，請確認 root / write 模式 / 工具白名單。", "err");
    return;
  }
  let args = {};
  try {
    args = aiAgentComfyuiToolArguments(overrides);
    if (!args.prompt) throw new Error("請先輸入提示詞");
  } catch (err) {
    setAiAgentMessage(err?.message || "產圖參數不完整", "err");
    return;
  }
  AI_AGENT_STATE.sendingTool = true;
  renderAiAgentWriteTools();
  setAiAgentMessage("ComfyUI 產圖任務送出中...", "info");
  try {
    const res = await apiFetch(API + "/ai-agent/write-tools/execute", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tool: "write_comfyui_generate",
        arguments: args,
        confirm: "EXECUTE",
      }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      setAiAgentMessage(json.msg || `ComfyUI 產圖送出失敗（HTTP ${res.status}）`, "err");
      return;
    }
    const job = json.result?.job || json.payload?.job || json.job || {};
    const jobId = job.job_id || json.result?.job_id || "-";
    AI_AGENT_STATE.messages.push({
      role: "assistant",
      content: `ComfyUI 產圖已送出。\n工具：write_comfyui_generate\nJob ID：${jobId}\n狀態：${job.status || "queued"}`,
    });
    renderAiAgentThread();
    setAiAgentMessage("ComfyUI 產圖已送出", "ok");
    await loadAiAgentReadOnly({ scope: "all", limit: 20, silent: true, force: true }).catch(() => undefined);
  } catch (err) {
    setAiAgentMessage(`ComfyUI 產圖送出失敗：${err}`, "err");
  } finally {
    AI_AGENT_STATE.sendingTool = false;
    renderAiAgentWriteTools();
  }
}


function renderAiAgentThread() {
  const host = $("ai-agent-thread");
  if (!host) return;
  aiAgentPersistConversation();
  if (!AI_AGENT_STATE.messages.length) {
    host.innerHTML = '<div class="drive-empty">目前沒有訊息</div>';
    return;
  }
  host.innerHTML = AI_AGENT_STATE.messages.map((message) => {
    const role = message.role === "assistant" ? "assistant" : "user";
    const label = role === "assistant" ? "AI" : "你";
    return `
      <div class="ai-agent-message ${role}">
        <div class="ai-agent-message-role">${sanitize(label)}</div>
        <div class="ai-agent-message-body">${sanitize(message.content || "")}</div>
      </div>
    `;
  }).join("");
  host.scrollTop = host.scrollHeight;
}

function renderAiAgentStatus(json) {
  const settings = json?.settings || {};
  AI_AGENT_STATE.settings = settings;
  const actor = json?.actor || {};
  AI_AGENT_STATE.actor = actor;
  AI_AGENT_STATE.audit = json?.audit || {};
  const health = json?.health || {};
  const status = $("ai-agent-status");
  if (status) {
    const providerLabel = settings.provider === "openai_compatible" ? "OpenAI-compatible" : "Hermes Agent";
    status.textContent = health.ok ? `${providerLabel} 已連線` : `${providerLabel} 未連線${health.msg ? `：${health.msg}` : ""}`;
    status.style.color = health.ok ? "var(--accent2)" : "var(--muted)";
  }
  if ($("ai-agent-provider")) $("ai-agent-provider").textContent = settings.provider || "-";
  if ($("ai-agent-base-url")) $("ai-agent-base-url").textContent = settings.api_base_url || "-";
  if ($("ai-agent-key-state")) $("ai-agent-key-state").textContent = settings.api_key_configured ? "已設定" : "未設定";
  if ($("ai-agent-image-allowed")) $("ai-agent-image-allowed").textContent = settings.allow_image_input === false ? "關閉" : "開啟";
  const mode = settings.operation_mode || "assist";
  const modePolicy = settings.operation_mode_policy || {};
  if ($("ai-agent-operation-mode-state")) $("ai-agent-operation-mode-state").textContent = `${modePolicy.label || AI_AGENT_OPERATION_MODE_LABELS[mode] || mode}：${modePolicy.description || ""}`;
  if ($("ai-agent-allowed-models-state")) $("ai-agent-allowed-models-state").textContent = settings.allowed_models ? settings.allowed_models : "不限";
  if ($("ai-agent-allowed-tools-state")) $("ai-agent-allowed-tools-state").textContent = settings.allowed_tools ? settings.allowed_tools : "依角色預設";
  const personaLabelMap = {
    concise_helper: "簡潔客服導向",
    strict_helper: "嚴謹流程助手",
    creative_coordinator: "創意流程統籌",
  };
  if ($("ai-agent-persona-state")) $("ai-agent-persona-state").textContent = personaLabelMap[settings.persona] || settings.persona || "未設定";
  const tasks = settings.tasks || {};
  const taskLines = [];
  if (tasks.site_guide) taskLines.push("網站導覽");
  if (tasks.troubleshoot) taskLines.push("生圖/下載排錯");
  if (tasks.prompt) taskLines.push("提示詞與參數");
  if ($("ai-agent-tasks-state")) $("ai-agent-tasks-state").textContent = taskLines.length ? taskLines.join("、") : "未啟用";
  if ($("ai-agent-model-state")) $("ai-agent-model-state").textContent = `模型：${settings.model || "-"}`;
  syncAiAgentModelSelect();
  if ($("ai-agent-safety-boundaries")) {
    const rules = Array.isArray(settings.safety_boundaries) ? settings.safety_boundaries : [];
    $("ai-agent-safety-boundaries").innerHTML = rules.length
      ? rules.map((line) => `<div>${sanitize(line)}</div>`).join("")
      : "尚未載入安全規則。";
  }
  if ($("ai-agent-effective-tools")) {
    const tools = Array.isArray(settings.tools) ? settings.tools : [];
    $("ai-agent-effective-tools").innerHTML = tools.length
      ? tools.map((tool) => `<div>${sanitize(tool.label || tool.name || "-")}：${sanitize(tool.description || "")}<br><span class="drive-card-sub">${sanitize(tool.name || "")} / ${sanitize(tool.data_scope || "")}</span></div>`).join("")
      : "目前沒有可調用工具。";
  }
  renderAiAgentWriteTools();
  renderAiAgentAuditStatus(AI_AGENT_STATE.audit, actor);
}

function renderAiAgentReadOnly(payload = {}) {
  const actor = payload?.actor || {};
  const permissions = payload.permissions || {};
  const scope = (actor.role || "user").toString();
  const canManageMembers = !!permissions.manage_members;
  const canManageServers = !!permissions.manage_servers;

  if ($("ai-agent-readonly-role")) $("ai-agent-readonly-role").textContent = scope;
  if ($("ai-agent-readonly-resource")) $("ai-agent-readonly-resource").textContent = "可";
  if ($("ai-agent-readonly-member-mgmt")) $("ai-agent-readonly-member-mgmt").textContent = canManageMembers ? "可" : "否";
  if ($("ai-agent-readonly-attack-diagnostic")) $("ai-agent-readonly-attack-diagnostic").textContent = canManageServers ? "可" : "否";

  const capabilities = ["個別記憶隔離", "個人資料與任務只看自己"];
  if (canManageMembers) capabilities.push("會員管理報表（唯讀）");
  if (canManageServers) capabilities.push("伺服器資源與攻擊診斷（唯讀）");
  if ($("ai-agent-readonly-capabilities")) {
    $("ai-agent-readonly-capabilities").innerHTML = capabilities.map((line) => `<div>${sanitize(line)}</div>`).join("");
  }

  const resources = payload.resources || {};
  const cpu = resources?.cpu || {};
  const ram = resources?.ram || {};
  const disk = resources?.disk || {};
  const jobs = [];
  if (Array.isArray(payload.comfyui_jobs) && payload.comfyui_jobs.length) {
    jobs.push(`ComfyUI 任務：${payload.comfyui_jobs.length} 筆`);
  }
  if (Array.isArray(payload.remote_download_jobs) && payload.remote_download_jobs.length) {
    jobs.push(`下載任務：${payload.remote_download_jobs.length} 筆`);
  }
  if (Array.isArray(payload.storage_files)) {
    jobs.push(`檔案快照：${payload.storage_files.length} 筆${canManageServers ? "（root 全站摘要）" : "（個人隔離）"}`);
  }

  const summary = [
    `取樣時間：${resources?.sampled_at || "-"}`,
    `CPU：${cpu?.percent !== null && cpu?.percent !== undefined ? `${cpu.percent.toFixed(1)}%` : "-"}`,
    `RAM：${ram?.percent !== null && ram?.percent !== undefined ? `${ram.percent.toFixed(1)}%` : "-"}`,
    `硬碟：${disk?.percent !== null && disk?.percent !== undefined ? `${disk.percent.toFixed(1)}%` : "-"}`,
    `任務摘要：${jobs.length ? jobs.join("；") : "無進行中唯讀快照"}`,
  ];

  if (payload.member_management) {
    const member = payload.member_management;
    summary.push(`近期使用者：${member.recent_users?.length || 0} / 全站 ${member.total_users || 0}`);
  }
  if (payload.attack_diagnosis) {
    summary.push(`安全事件：${payload.attack_diagnosis.security_events?.length || 0} / 失敗任務：${payload.attack_diagnosis.recent_failed_jobs?.length || 0}`);
  }

  if ($("ai-agent-readonly-overview")) {
    $("ai-agent-readonly-overview").innerHTML = summary.map((line) => `<div>${sanitize(line)}</div>`).join("");
  }
}

function aiAgentAllowedModels() {
  return String(AI_AGENT_STATE.settings?.allowed_models || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function aiAgentSelectableModels() {
  const allowedModels = aiAgentAllowedModels();
  const modelIds = [];
  (AI_AGENT_STATE.modelIds || []).forEach((id) => {
    if (id && !modelIds.includes(id)) modelIds.push(id);
  });
  allowedModels.forEach((id) => {
    if (id && !modelIds.includes(id)) modelIds.unshift(id);
  });
  const configured = AI_AGENT_STATE.settings?.model || "";
  if (configured && !modelIds.includes(configured)) modelIds.unshift(configured);
  return modelIds.filter((id) => !allowedModels.length || allowedModels.includes(id));
}

function aiAgentVisionModel() {
  const options = aiAgentSelectableModels();
  const selected = ($("ai-agent-model")?.value || "").trim();
  const vision = options.find((id) => /(?:^|[-_:])vl(?:[-_:]|$)|vision|qwen3-vl/i.test(id));
  return vision || selected || options[0] || AI_AGENT_STATE.settings?.model || "";
}

function aiAgentImageAnalysisError(json = {}, status = 0) {
  const raw = String(json?.msg || json?.error || json?.message?.content || "").trim();
  const lowered = raw.toLowerCase();
  if (lowered.includes("does not support image input") || lowered.includes("不支援圖片")) {
    return "目前選用模型不支援圖片分析，請改用 vision 模型（例如 qwen3-vl）後再試。";
  }
  if (status >= 500 || lowered.includes("internal server error")) {
    return `圖片分析後端目前不可用（HTTP ${status || 500}）。${raw ? `後端訊息：${raw}` : "請稍後重試或檢查 Ollama/Hermes vision 模型服務。"}`;
  }
  return raw || `圖片分析失敗（HTTP ${status || "-"}）`;
}

function aiAgentReadonlyIntent(prompt) {
  const text = String(prompt || "").toLowerCase();
  if (!text) return null;
  const asksStatus = /(查|看|顯示|確認|狀態|status|progress|進度|摘要|目前)/i.test(text);
  if (!asksStatus) return null;
  if (/(產圖|生圖|comfyui|generation|queue|任務)/i.test(text) && /(進度|狀態|queue|排隊|running|pending|完成|失敗)/i.test(text)) {
    return { scope: "comfyui", label: "ComfyUI 產圖進度" };
  }
  if (/(下載|download|remote download)/i.test(text)) {
    return { scope: "remote_download", label: "下載進度" };
  }
  if (/(資源|cpu|gpu|ram|memory|記憶體|硬碟|disk|load|負載)/i.test(text)) {
    return { scope: "resources", label: "伺服器資源" };
  }
  if (/(審計|audit|logs?|log|ip|流量|攻擊|異常|安全事件|attack)/i.test(text)) {
    return { scope: "attack_diag", label: "安全審計" };
  }
  return null;
}

function aiAgentReadonlySummary(payload = {}, intent = {}) {
  const lines = [`${intent.label || "唯讀查詢"}：已直接讀取站內唯讀資料。`];
  const resources = payload.resources || {};
  if (resources.cpu || resources.ram || resources.disk) {
    const cpu = resources.cpu?.percent;
    const ram = resources.ram?.percent;
    const disk = resources.disk?.percent;
    lines.push(`資源：CPU ${cpu !== undefined && cpu !== null ? `${Number(cpu).toFixed(1)}%` : "-"} / RAM ${ram !== undefined && ram !== null ? `${Number(ram).toFixed(1)}%` : "-"} / Disk ${disk !== undefined && disk !== null ? `${Number(disk).toFixed(1)}%` : "-"}`);
  }
  if (Array.isArray(payload.comfyui_jobs)) {
    const jobs = payload.comfyui_jobs.slice(0, 5).map((job) => `${job.status || "-"} ${job.title || job.prompt || job.id || ""}`.trim());
    lines.push(`ComfyUI 任務：${payload.comfyui_jobs.length ? jobs.join("；") : "目前沒有可見任務"}`);
  }
  if (Array.isArray(payload.remote_download_jobs)) {
    const jobs = payload.remote_download_jobs.slice(0, 5).map((job) => `${job.status || "-"} ${job.filename || job.title || job.id || ""}`.trim());
    lines.push(`下載任務：${payload.remote_download_jobs.length ? jobs.join("；") : "目前沒有可見下載任務"}`);
  }
  if (payload.attack_diagnosis) {
    const diag = payload.attack_diagnosis;
    lines.push(`安全事件：${diag.security_events?.length || 0}；近期失敗任務：${diag.recent_failed_jobs?.length || 0}`);
  } else if (intent.scope === "attack_diag") {
    lines.push("安全審計完整資料限 root 檢視；目前帳號只會看到允許範圍。");
  }
  return lines.join("\n");
}

function syncAiAgentModelSelect() {
  const select = $("ai-agent-model");
  if (!select) return;
  const previous = select.value;
  const options = aiAgentSelectableModels();
  if (!options.length) {
    const fallback = AI_AGENT_STATE.settings?.model || "";
    select.innerHTML = `<option value="${sanitize(fallback)}">${sanitize(fallback || "尚未取得模型")}</option>`;
    select.disabled = !fallback;
    return;
  }
  select.disabled = false;
  select.innerHTML = options.map((id) => `<option value="${sanitize(id)}">${sanitize(id)}</option>`).join("");
  const configured = AI_AGENT_STATE.settings?.model || "";
  select.value = previous && options.includes(previous)
    ? previous
    : (configured && options.includes(configured) ? configured : options[0]);
}

function renderAiAgentModels(modelsPayload) {
  const host = $("ai-agent-model-list");
  if (!host) return;
  const rawModels = Array.isArray(modelsPayload?.data) ? modelsPayload.data : [];
  const allowedModels = aiAgentAllowedModels();
  const modelIds = [];
  rawModels.forEach((model) => {
    const id = typeof model === "string" ? model : model?.id || model?.name || "";
    if (id && !modelIds.includes(id)) modelIds.push(id);
  });
  AI_AGENT_STATE.modelIds = modelIds.slice();
  allowedModels.forEach((id) => {
    if (id && !modelIds.includes(id)) modelIds.unshift(id);
  });
  syncAiAgentModelSelect();
  if (!modelIds.length) {
    host.innerHTML = '<div class="drive-empty">尚未取得模型清單</div>';
    return;
  }
  host.innerHTML = modelIds.slice(0, 16).map((id) => {
    const allowed = !allowedModels.length || allowedModels.includes(id);
    const suffix = allowed ? "" : "（未在允許清單）";
    return `<button class="drive-file-row ai-agent-model-option${allowed ? "" : " disabled"}" type="button" ${allowed ? "" : "disabled"} data-ai-agent-model="${sanitize(id)}">${sanitize(id || "-")}${sanitize(suffix)}</button>`;
  }).join("");
  host.querySelectorAll("[data-ai-agent-model]").forEach((button) => {
    button.addEventListener("click", () => {
      const select = $("ai-agent-model");
      const model = button.dataset.aiAgentModel || "";
      if (select && Array.from(select.options).some((option) => option.value === model)) {
        select.value = model;
      }
    });
  });
}

function renderAiAgentAuditStatus(audit = {}, actor = {}) {
  const host = $("ai-agent-audit-overview");
  const actions = $("ai-agent-audit-actions");
  const scope = actor?.scope || {};
  const canManageServers = !!scope.can_manage_servers;
  if (actions) actions.hidden = !canManageServers;
  if (!host) return;
  const scheduler = audit.scheduler || {};
  const summary = audit.summary || {};
  const mode = audit.mode || AI_AGENT_STATE.settings?.operation_mode || "-";
  const lines = [
    `模式：${AI_AGENT_OPERATION_MODE_LABELS[mode] || mode}`,
    `排程：${scheduler.enabled ? "啟用" : "未啟用"} / ${scheduler.interval_minutes || "-"} 分鐘`,
    `上次掃描：${scheduler.last_scanned_at || "尚未掃描"}`,
    `下次預計：${scheduler.next_due_at || "-"}`,
  ];
  if (summary.status) {
    lines.push(`結果：${summary.status}，異常 ${summary.anomaly_count || 0}，處置 ${summary.intervention_count || 0}，通知 ${summary.notification_count || 0}`);
  }
  if (!canManageServers) {
    lines.push("完整審計資料限 root 檢視。");
  }
  host.innerHTML = lines.map((line) => `<div>${sanitize(line)}</div>`).join("");
}

async function loadAiAgentAuditStatus(options = {}) {
  try {
    const res = await apiFetch(API + "/ai-agent/audit-status", { credentials: "same-origin" });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      if (!options.silent) setAiAgentMessage(json.msg || "審計狀態讀取失敗", "err");
      return;
    }
    AI_AGENT_STATE.audit = json.audit_status || {};
    renderAiAgentAuditStatus(AI_AGENT_STATE.audit, AI_AGENT_STATE.actor);
    if (!options.silent) setAiAgentMessage("審計狀態已更新", "ok");
  } catch (err) {
    if (!options.silent) setAiAgentMessage(`審計狀態讀取失敗：${err}`, "err");
  }
}

async function runAiAgentAuditScan() {
  setAiAgentMessage("審計掃描中...", "info");
  try {
    const res = await apiFetch(API + "/ai-agent/audit-scan", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: true }),
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      setAiAgentMessage(json.msg || "審計掃描失敗", "err");
      return;
    }
    AI_AGENT_STATE.audit = {
      ...(AI_AGENT_STATE.audit || {}),
      summary: {
        status: json.scan?.status || "unknown",
        scanned_at: json.scan?.scanned_at || "",
        anomaly_count: (json.scan?.anomalies || []).length,
        intervention_count: (json.scan?.interventions || []).length,
        notification_count: (json.scan?.notifications || []).length,
      },
      scan: json.scan || {},
    };
    renderAiAgentAuditStatus(AI_AGENT_STATE.audit, AI_AGENT_STATE.actor);
    setAiAgentMessage("審計掃描完成", "ok");
  } catch (err) {
    setAiAgentMessage(`審計掃描失敗：${err}`, "err");
  }
}

async function loadAiAgentStatus(options = {}) {
  if (AI_AGENT_STATE.loading) return;
  if (AI_AGENT_STATE.loaded && !options.force) return;
  AI_AGENT_STATE.loading = true;
    try {
      const res = await apiFetch(API + "/ai-agent/status", { credentials: "same-origin" });
      const json = await res.json().catch(() => ({}));
      if (!json.ok) {
        setAiAgentMessage(json.msg || "AI Agent 狀態讀取失敗", "err");
        return;
      }
      renderAiAgentStatus(json);
      await loadAiAgentReadOnly({ scope: "all", limit: 20, silent: true }).catch(() => undefined);
      if (json?.actor?.scope?.can_manage_servers) {
        await loadAiAgentAuditStatus({ silent: true }).catch(() => undefined);
      }
      AI_AGENT_STATE.loaded = true;
      setAiAgentMessage("", "info");
      try {
        const modelsRes = await apiFetch(API + "/ai-agent/models", { credentials: "same-origin" });
      const modelsJson = await modelsRes.json().catch(() => ({}));
      renderAiAgentModels(modelsJson.models || {});
    } catch (err) {
      renderAiAgentModels({});
    }
  } catch (err) {
    setAiAgentMessage(`AI Agent 狀態讀取失敗：${err}`, "err");
  } finally {
    AI_AGENT_STATE.loading = false;
  }
}

async function loadAiAgentReadOnly(options = {}) {
  if (AI_AGENT_STATE.readonlyLoading && !options.force) return;
  AI_AGENT_STATE.readonlyLoading = true;
  const scope = options.scope || "all";
  const limit = Math.max(1, Math.min(100, parseInt(options.limit || 20, 10) || 20));
  try {
    const res = await apiFetch(`${API}/ai-agent/readonly?scope=${encodeURIComponent(scope)}&limit=${limit}`, {
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      if (!options.silent) setAiAgentMessage(json.msg || "AI Agent 只讀摘要讀取失敗", "err");
      return;
    }
    renderAiAgentReadOnly(json);
    if (options.silent) {
      return;
    }
    setAiAgentMessage("", "info");
  } finally {
    AI_AGENT_STATE.readonlyLoading = false;
  }
}

function clearAiAgentConversation() {
  const scope = AI_AGENT_STATE.accountScope || aiAgentCurrentAccountScope();
  AI_AGENT_STATE.messages = [];
  AI_AGENT_STATE.imageDataUrl = "";
  AI_AGENT_STATE.sessionId = "";
  try {
    if (scope && typeof localStorage !== "undefined") localStorage.removeItem(aiAgentConversationStorageKey(scope));
  } catch (err) {}
  if ($("ai-agent-input")) $("ai-agent-input").value = "";
  if ($("ai-agent-image-file")) $("ai-agent-image-file").value = "";
  if ($("ai-agent-image-state")) $("ai-agent-image-state").textContent = "未附加圖片";
  renderAiAgentThread();
  setAiAgentMessage("", "info");
}

function handleAiAgentAccountContextChanged() {
  aiAgentResetScopeState();
}

function handleAiAgentImagePick(event) {
  const file = event?.target?.files?.[0];
  AI_AGENT_STATE.imageDataUrl = "";
  if (!file) {
    if ($("ai-agent-image-state")) $("ai-agent-image-state").textContent = "未附加圖片";
    return;
  }
  if (!file.type || !file.type.startsWith("image/")) {
    setAiAgentMessage("只能附加圖片檔", "err");
    event.target.value = "";
    return;
  }
  if (file.size > 2 * 1024 * 1024) {
    setAiAgentMessage("圖片大小不可超過 2 MB", "err");
    event.target.value = "";
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    AI_AGENT_STATE.imageDataUrl = String(reader.result || "");
    if ($("ai-agent-image-state")) $("ai-agent-image-state").textContent = file.name || "已附加圖片";
  };
  reader.onerror = () => setAiAgentMessage("圖片讀取失敗", "err");
  reader.readAsDataURL(file);
}

function aiAgentBuildMessages(prompt, mode) {
  const history = AI_AGENT_STATE.messages.slice(-12).map((message) => ({
    role: message.role === "assistant" ? "assistant" : "user",
    content: message.content || "",
  }));
  const userContent = [];
  userContent.push({ type: "text", text: prompt });
  if (mode === "image" && AI_AGENT_STATE.imageDataUrl) {
    userContent.push({ type: "image_url", image_url: { url: AI_AGENT_STATE.imageDataUrl } });
  }
  history.push({ role: "user", content: userContent });
  return history;
}

async function sendAiAgentMessage() {
  if (AI_AGENT_STATE.sending) return;
  const input = $("ai-agent-input");
  const prompt = (input?.value || "").trim();
  const mode = $("ai-agent-mode")?.value || "text";
  if (!prompt && !(mode === "image" && AI_AGENT_STATE.imageDataUrl)) {
    setAiAgentMessage("請輸入訊息", "err");
    return;
  }
  if (mode === "image" && !AI_AGENT_STATE.imageDataUrl) {
    setAiAgentMessage("請選擇圖片", "err");
    return;
  }
  if (mode === "image" && AI_AGENT_STATE.imageDataUrl && aiAgentWantsComfyuiGeneration(prompt)) {
    if (!AI_AGENT_STATE.loaded && typeof loadAiAgentStatus === "function") {
      await loadAiAgentStatus({ force: true }).catch(() => undefined);
    }
    const userText = prompt || "參考圖片生圖";
    AI_AGENT_STATE.messages.push({ role: "user", content: `${userText}\n[已附加圖片]` });
    renderAiAgentThread();
    if (input) input.value = "";
    AI_AGENT_STATE.sending = true;
    const sendBtn = $("ai-agent-send-btn");
    if (sendBtn) sendBtn.disabled = true;
    setAiAgentMessage("圖片分析與提示詞生成中...", "info");
    try {
      const analyzed = await aiAgentAnalyzeImageForComfyui(userText);
      aiAgentFillComfyuiToolForm(analyzed.args);
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: `圖片分析完成（${analyzed.elapsedMs} ms）。\n產生提示詞：${analyzed.args.prompt}`,
      });
      renderAiAgentThread();
      await runAiAgentComfyuiGenerate(analyzed.args);
    } catch (err) {
      AI_AGENT_STATE.messages.push({ role: "assistant", content: `圖片分析失敗，未送出生圖：${err?.message || err}` });
      renderAiAgentThread();
      setAiAgentMessage(`圖片分析失敗：${err?.message || err}`, "err");
    } finally {
      AI_AGENT_STATE.sending = false;
      if (sendBtn) sendBtn.disabled = false;
    }
    return;
  }
  const directComfyuiArgs = mode === "text" ? aiAgentParseComfyuiGenerateRequest(prompt) : null;
  if (directComfyuiArgs) {
    if (!AI_AGENT_STATE.loaded && typeof loadAiAgentStatus === "function") {
      await loadAiAgentStatus({ force: true }).catch(() => undefined);
    }
    const userText = prompt;
    AI_AGENT_STATE.messages.push({ role: "user", content: userText });
    renderAiAgentThread();
    aiAgentFillComfyuiToolForm(directComfyuiArgs);
    if (input) input.value = "";
    await runAiAgentComfyuiGenerate(directComfyuiArgs);
    return;
  }
  if (mode === "text" && aiAgentWantsComfyuiGeneration(prompt)) {
    if (!AI_AGENT_STATE.loaded && typeof loadAiAgentStatus === "function") {
      await loadAiAgentStatus({ force: true }).catch(() => undefined);
    }
    const userText = prompt;
    AI_AGENT_STATE.messages.push({ role: "user", content: userText });
    renderAiAgentThread();
    if (input) input.value = "";
    AI_AGENT_STATE.sending = true;
    const sendBtn = $("ai-agent-send-btn");
    if (sendBtn) sendBtn.disabled = true;
    setAiAgentMessage("生圖需求解析中...", "info");
    try {
      const analyzed = await aiAgentAnalyzeTextForComfyui(userText);
      aiAgentFillComfyuiToolForm(analyzed.args);
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: `生圖需求解析完成（${analyzed.elapsedMs} ms）。\n產生提示詞：${analyzed.args.prompt}`,
      });
      renderAiAgentThread();
      await runAiAgentComfyuiGenerate(analyzed.args);
    } catch (err) {
      AI_AGENT_STATE.messages.push({ role: "assistant", content: `生圖需求解析失敗，未送出生圖：${err?.message || err}` });
      renderAiAgentThread();
      setAiAgentMessage(`生圖需求解析失敗：${err?.message || err}`, "err");
    } finally {
      AI_AGENT_STATE.sending = false;
      if (sendBtn) sendBtn.disabled = false;
    }
    return;
  }
  const readonlyIntent = mode === "text" ? aiAgentReadonlyIntent(prompt) : null;
  if (readonlyIntent) {
    AI_AGENT_STATE.sending = true;
    const sendBtn = $("ai-agent-send-btn");
    if (sendBtn) sendBtn.disabled = true;
    const userText = prompt;
    AI_AGENT_STATE.messages.push({ role: "user", content: userText });
    renderAiAgentThread();
    if (input) input.value = "";
    setAiAgentMessage(`${readonlyIntent.label}讀取中...`, "info");
    try {
      const res = await apiFetch(`${API}/ai-agent/readonly?scope=${encodeURIComponent(readonlyIntent.scope)}&limit=20`, {
        credentials: "same-origin",
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok || !json.ok) {
        throw new Error(json.msg || `唯讀查詢失敗（HTTP ${res.status}）`);
      }
      renderAiAgentReadOnly(json);
      AI_AGENT_STATE.messages.push({ role: "assistant", content: aiAgentReadonlySummary(json, readonlyIntent) });
      renderAiAgentThread();
      setAiAgentMessage("已完成唯讀查詢", "ok");
    } catch (err) {
      AI_AGENT_STATE.messages.push({ role: "assistant", content: `唯讀查詢失敗：${err?.message || err}` });
      renderAiAgentThread();
      setAiAgentMessage(`唯讀查詢失敗：${err?.message || err}`, "err");
    } finally {
      AI_AGENT_STATE.sending = false;
      if (sendBtn) sendBtn.disabled = false;
    }
    return;
  }
  AI_AGENT_STATE.sending = true;
  const sendBtn = $("ai-agent-send-btn");
  if (sendBtn) sendBtn.disabled = true;
  setAiAgentMessage("送出中...", "info");
  const selectedModel = ($("ai-agent-model")?.value || "").trim();
  const selectableModels = aiAgentSelectableModels();
  if (selectableModels.length && !selectableModels.includes(selectedModel)) {
    AI_AGENT_STATE.sending = false;
    if (sendBtn) sendBtn.disabled = false;
    setAiAgentMessage("請從模型選單選擇可用模型。", "err");
    return;
  }
  const userText = prompt || "[圖片]";
  AI_AGENT_STATE.messages.push({ role: "user", content: mode === "image" && AI_AGENT_STATE.imageDataUrl ? `${userText}\n[已附加圖片]` : userText });
  renderAiAgentThread();
  try {
    const payload = {
      session_id: aiAgentEnsureSessionId(),
      model: selectedModel,
      mode,
      messages: aiAgentBuildMessages(prompt, mode),
      image_data_url: mode === "image" ? AI_AGENT_STATE.imageDataUrl : "",
    };
    const raw = await apiFetch(API + "/ai-agent/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(async (res) => {
      const text = await res.text();
      let parsed = {};
      try {
        parsed = text ? JSON.parse(text) : {};
      } catch (error) {
        parsed = {};
      }
      return { res, text, parsed };
    });

    const { res, text, parsed: json } = raw;
    const replied = json?.message?.content || "";
    const statusHint = `（HTTP ${res.status} ${res.statusText || ""}）`.trim();
    if (!json.ok || isMockAiAgentReply(replied)) {
      const msg = json.msg || (text ? text.slice(0, 160) : `AI Agent 回應失敗 ${statusHint}`);
      AI_AGENT_STATE.messages.push({ role: "assistant", content: msg || "AI Agent 回應失敗" });
      if (!json.ok) {
        setAiAgentMessage(json.msg ? `${json.msg} ${statusHint}` : `AI Agent 回應失敗 ${statusHint}`, "err");
      } else {
        setAiAgentMessage("AI Agent 後端仍回傳 mock 回覆，請確認 AI Agent Base URL 設定", "err");
      }
    } else {
      AI_AGENT_STATE.messages.push(json.message || { role: "assistant", content: "" });
      setAiAgentMessage("已完成", "ok");
      if (input) input.value = "";
    }
    renderAiAgentThread();
  } catch (err) {
    AI_AGENT_STATE.messages.push({ role: "assistant", content: `AI Agent 回應失敗：${err}` });
    renderAiAgentThread();
    setAiAgentMessage(`AI Agent 回應失敗：${err}`, "err");
  } finally {
    AI_AGENT_STATE.sending = false;
    if (sendBtn) sendBtn.disabled = false;
  }
}

document.addEventListener("hackme:module-changed", (event) => {
  if (event?.detail?.current === "ai-agent") loadAiAgentStatus();
});

document.addEventListener("hackme:account-context-changed", handleAiAgentAccountContextChanged);

aiAgentResetScopeState();

renderAiAgentThread();
