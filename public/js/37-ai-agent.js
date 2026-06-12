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
  comfyuiWatchJobs: {},
  lastComfyuiJob: null,
  comfyuiPreviewLoads: {},
  persistTimer: null,
  loadingConversation: false,
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
  if (!scope || AI_AGENT_STATE.loadingConversation) return;
  if (!AI_AGENT_STATE.sessionId && !AI_AGENT_STATE.messages.length) return;
  const conversationId = aiAgentEnsureSessionId();
  const payload = {
    sessionId: conversationId,
    messages: AI_AGENT_STATE.messages.slice(-80).map((message) => ({
      role: message.role,
      content: String(message.content || "").slice(0, 20000),
      images: Array.isArray(message.images)
        ? message.images.slice(0, 4).map((image) => ({
          image_ref: image?.image_ref || null,
          prompt_id: String(image?.prompt_id || "").slice(0, 160),
          filename: String(image?.filename || "").slice(0, 260),
          mime_type: String(image?.mime_type || "").slice(0, 80),
        })).filter((image) => image.image_ref && image.filename)
        : [],
    })),
    habits: {},
  };
  apiFetch(API + "/ai-agent/conversation", {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: conversationId, payload }),
  }).catch(() => undefined);
}

function aiAgentSchedulePersistConversation() {
  if (AI_AGENT_STATE.persistTimer) clearTimeout(AI_AGENT_STATE.persistTimer);
  AI_AGENT_STATE.persistTimer = setTimeout(() => {
    AI_AGENT_STATE.persistTimer = null;
    aiAgentPersistConversation();
  }, 350);
}

function aiAgentLoadConversation(scope) {
  AI_AGENT_STATE.messages = [];
  AI_AGENT_STATE.sessionId = "";
  AI_AGENT_STATE.imageDataUrl = "";
  if (!scope) return;
  if (scope === "anonymous") {
    renderAiAgentThread({ skipPersist: true });
    return;
  }
  let storedSessionId = "";
  try {
    const raw = localStorage.getItem(aiAgentConversationStorageKey(scope));
    if (raw) {
      const parsed = JSON.parse(raw);
      storedSessionId = String(parsed?.sessionId || "").slice(0, 120);
    }
  } catch (err) {
    storedSessionId = "";
  }
  if (storedSessionId) AI_AGENT_STATE.sessionId = storedSessionId;
  aiAgentLoadEncryptedConversation(storedSessionId || "default").catch(() => undefined);
}

async function aiAgentLoadEncryptedConversation(conversationId = "default") {
  if (AI_AGENT_STATE.loadingConversation) return;
  AI_AGENT_STATE.loadingConversation = true;
  try {
    const res = await apiFetch(`${API}/ai-agent/conversation?conversation_id=${encodeURIComponent(conversationId || "default")}`, {
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) return;
    const payload = json.payload || {};
    const messages = Array.isArray(payload.messages) ? payload.messages : [];
    AI_AGENT_STATE.messages = messages
      .filter((message) => message && ["user", "assistant"].includes(message.role))
      .slice(-80)
      .map((message) => ({
        role: message.role,
        content: String(message.content || "").slice(0, 20000),
        images: Array.isArray(message.images)
          ? message.images.slice(0, 4).map((image) => ({
            image_ref: image?.image_ref || null,
            prompt_id: String(image?.prompt_id || "").slice(0, 160),
            filename: String(image?.filename || "").slice(0, 260),
            mime_type: String(image?.mime_type || "").slice(0, 80),
          })).filter((image) => image.image_ref && image.filename)
          : [],
      }));
    AI_AGENT_STATE.sessionId = String(payload.sessionId || json.conversation_id || conversationId || "").slice(0, 120);
    renderAiAgentThread({ skipPersist: true });
    aiAgentHydratePersistedComfyuiImages();
  } finally {
    AI_AGENT_STATE.loadingConversation = false;
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
    aiAgentHydratePersistedComfyuiImages();
  }
}

function aiAgentEnsureSessionId() {
  if (AI_AGENT_STATE.sessionId) return AI_AGENT_STATE.sessionId;
  AI_AGENT_STATE.sessionId = "default";
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

function aiAgentNormalizeUserText(value) {
  return String(value || "")
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .replace(/\\r/g, "\n");
}

function aiAgentLineValue(text, patterns) {
  const lines = aiAgentNormalizeUserText(text).split(/\r?\n/);
  for (const line of lines) {
    for (const pattern of patterns) {
      const match = line.match(pattern);
      if (match) return aiAgentStripFieldValue(match[1] || "");
    }
  }
  return "";
}

function aiAgentLooksLikeComfyuiPromptLine(line) {
  const value = aiAgentStripFieldValue(line);
  if (!value) return false;
  if (/^\d{3,4}\s*[xX*×＊]\s*\d{3,4}$/.test(value)) return false;
  if (/^(?:size|尺寸|解析度|cfg|steps?|步數|batch|張數|數量|seed|sampler|scheduler|vae|models?|模型|checkpoint|ckpt)\s*[:：]/i.test(value)) return false;
  if (/(幫我|請|使用|用|生成|產生|生圖|產圖|畫|comfyui|txt2img|t2i|sdxl)/i.test(value) && !value.includes(",")) return false;
  if (/,/.test(value)) return true;
  return /\b(?:girl|boy|woman|man|landscape|portrait|anime|photo|bikini|dress|style|lighting|background)\b/i.test(value);
}

function aiAgentLooksLikeComfyuiModelLine(line) {
  const value = aiAgentStripFieldValue(line);
  if (!value || value.includes(",")) return false;
  if (/^\d{3,4}\s*[xX*×＊]\s*\d{3,4}$/.test(value)) return false;
  if (/(幫我|請|生成|產生|生圖|產圖|畫|txt2img|t2i)/i.test(value)) return false;
  return /(?:v\s*\d+|v\d+|ckpt|checkpoint|model|模型|safetensors|janku|pony|illustrious|xl|sdxl|[A-Z]{2,}.*\d)/i.test(value);
}

function aiAgentParseComfyuiGenerateRequest(text) {
  const raw = aiAgentNormalizeUserText(text).trim();
  if (!raw) return null;
  const lower = raw.toLowerCase();
  const wantsImage = /生圖|產圖|生成圖片|畫圖|畫一張|做一張|comfyui|txt2img|t2i|sdxl|text\s*to\s*image/.test(lower);
  if (!wantsImage) return null;
  let prompt = aiAgentLineValue(raw, [
    /^\s*(?:提示詞|prompt|positive prompt)\s*[:：]\s*(.+)$/i,
  ]);
  const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!prompt) {
    const promptLine = lines.find((line) => aiAgentLooksLikeComfyuiPromptLine(line));
    prompt = promptLine ? aiAgentStripFieldValue(promptLine) : "";
  }
  if (!prompt) return null;
  const args = { prompt, confirm_billing: true };
  const negative = aiAgentLineValue(raw, [
    /^\s*(?:負面提示詞|負面詞|反向提示詞|反向詞|negative prompt|negative|neg)\s*[:：]\s*(.+)$/i,
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
  if (model) {
    args.checkpoint = model;
  } else {
    const modelLine = lines.find((line) => aiAgentLooksLikeComfyuiModelLine(line) && aiAgentStripFieldValue(line) !== prompt);
    if (modelLine) args.checkpoint = aiAgentStripFieldValue(modelLine);
  }
  const cfg = raw.match(/(?:^|[\s,，;；])(?:cfg(?:[_\s-]?scale)?)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)/i);
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
  const raw = aiAgentNormalizeUserText(text);
  if (/(不要|不必|不用|無需|別|禁止|不要幫我)\s*(?:再)?\s*(?:生圖|產圖|生成圖片|生成一張|產生圖片|產生一張|畫圖|畫一張|做一張|comfyui|txt2img|t2i|sdxl)/i.test(raw)) {
    return false;
  }
  if (/(查|看|顯示|確認|status|progress|進度|狀態|queue|running|pending|任務)/i.test(raw)
    && /(產圖|生圖|comfyui|generation|下載|download)/i.test(raw)) {
    return false;
  }
  if (/(描述|分析|解釋|辨識|看看|看一下|這張圖.*(是|有|哪|什麼)|what.*image|describe.*image)/i.test(raw)
    && !/(生圖|產圖|生成|產生|畫|做一張|comfyui|txt2img|t2i|sdxl|text\s*to\s*image)/i.test(raw)) {
    return false;
  }
  return /生圖|產圖|生成圖片|生成一張|產生圖片|產生一張|畫圖|畫一張|做一張|comfyui|txt2img|t2i|sdxl|text\s*to\s*image/i.test(raw);
}

function aiAgentComfyuiTextHasSubject(text) {
  const raw = aiAgentNormalizeUserText(text).trim();
  if (!raw) return false;
  const direct = aiAgentParseComfyuiGenerateRequest(raw);
  if (direct?.prompt) return true;
  const cleaned = raw
    .replace(/(?:幫我|請|使用|用|生成|產生|生圖|產圖|生成圖片|生成一張|產生圖片|產生一張|畫圖|畫一張|做一張|comfyui|txt2img|t2i|sdxl|text\s*to\s*image)/gi, " ")
    .replace(/(?:size|尺寸|解析度|cfg(?:[_\s-]?scale)?|steps?|步數|batch(?:[_\s-]?size)?|張數|數量|seed|種子|sampler|scheduler|vae|models?|模型|checkpoint|ckpt|負面提示詞|負面詞|negative prompt|negative|neg)\s*[:：]?\s*[^\n,，;；]*/gi, " ")
    .replace(/\d{3,4}\s*[xX*×＊]\s*\d{3,4}/g, " ")
    .replace(/[,\s，。；;：:、"'`“”]+/g, "");
  return cleaned.length >= 2;
}

function aiAgentComfyuiClarificationMessage() {
  return [
    "我還不知道你要畫什麼，所以不會自行沿用前文、記憶或模型猜提示詞，也不會送出生圖。",
    "請補充提示詞或主題，例如：提示詞、尺寸、模型、負面詞；只給「生圖」不足以執行。",
  ].join("\n");
}

function aiAgentParseComfyuiOptionOverrides(text) {
  const raw = aiAgentNormalizeUserText(text);
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
  const cfg = raw.match(/(?:^|[\s,，;；])(?:cfg(?:[_\s-]?scale)?)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)/i);
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

function aiAgentWriteIntent(text) {
  const raw = aiAgentNormalizeUserText(text).trim();
  if (!raw) return null;
  if (/(公告|announcement|notice)/i.test(raw) && /(發|發布|發佈|貼|新增|建立|create|post)/i.test(raw)) {
    return {
      tool: "write_community_create_thread",
      label: "發布公告",
      required: ["公告標題", "公告內容", "發布分類或版面"],
    };
  }
  if (/(發文|貼文|文章|thread|post)/i.test(raw) && /(發|發布|發佈|貼|新增|建立|create|post)/i.test(raw)) {
    return {
      tool: "write_community_create_thread",
      label: "發布貼文",
      required: ["標題", "內容", "發布分類或版面"],
    };
  }
  return null;
}

function aiAgentWriteIntentFollowup(intent) {
  const canElevate = aiAgentCanRequestWriteElevation(intent.tool);
  const mode = AI_AGENT_OPERATION_MODE_LABELS[AI_AGENT_STATE.settings?.operation_mode] || AI_AGENT_STATE.settings?.operation_mode || "目前模式";
  const lines = [
    `${intent.label}需要補齊必要資料後才能執行。`,
    `請提供：${intent.required.join("、")}。`,
  ];
  if (canElevate) {
    lines.push(`目前是 ${mode}；資料補齊後，我會在真正送出前請 root 確認本次提權。`);
  } else {
    lines.push(`目前不可直接執行 ${intent.tool}，我可以先幫你整理草稿與檢查內容。`);
  }
  return lines.join("\n");
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
  const tools = Array.isArray(AI_AGENT_STATE.settings?.tools) ? AI_AGENT_STATE.settings.tools : [];
  if (tools.some((tool) => tool?.name === toolName)) return true;
  const configured = String(AI_AGENT_STATE.settings?.allowed_tools || "").trim();
  if (!configured) return AI_AGENT_STATE.actor?.role === "super_admin";
  return configured.split(",").map((item) => item.trim()).filter(Boolean).includes(toolName);
}

function aiAgentCanRunWriteTool(toolName) {
  return AI_AGENT_STATE.actor?.role === "super_admin"
    && AI_AGENT_STATE.settings?.operation_mode === "write"
    && !!AI_AGENT_STATE.settings?.operation_mode_policy?.write_enabled
    && aiAgentHasEffectiveTool(toolName);
}

function aiAgentCanRequestWriteElevation(toolName) {
  return AI_AGENT_STATE.actor?.role === "super_admin"
    && AI_AGENT_STATE.settings?.operation_mode !== "write"
    && aiAgentHasEffectiveTool(toolName);
}

function aiAgentAuditTimeLabel(value, fallback = "尚未掃描") {
  const raw = String(value || "").trim();
  if (!raw) return fallback;
  if (/^1970-01-01(?:T| )/.test(raw)) return fallback;
  return raw;
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
    } else if (aiAgentCanRequestWriteElevation("write_comfyui_generate")) {
      state.textContent = "目前為唯讀/協助模式；需要生圖等寫入時會先詢問 root 是否允許本次提權。";
    } else {
      state.textContent = "工具白名單未允許 write_comfyui_generate，無法執行生圖寫入。";
    }
  }
  const canAttemptComfyui = canRunComfyui || aiAgentCanRequestWriteElevation("write_comfyui_generate");
  if (form) form.classList.toggle("disabled", !canAttemptComfyui);
  if (button) button.disabled = !canAttemptComfyui || AI_AGENT_STATE.sendingTool;
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

function aiAgentFindComfyuiJobPayload(value, seen = new Set()) {
  if (!value || typeof value !== "object") return null;
  if (seen.has(value)) return null;
  seen.add(value);
  if (value.job_id) return value;
  const preferred = [
    value.job,
    value.result?.job,
    value.result?.payload?.job,
    value.payload?.job,
    value.data?.job,
    value.result,
    value.payload,
    value.data,
  ];
  for (const item of preferred) {
    const found = aiAgentFindComfyuiJobPayload(item, seen);
    if (found) return found;
  }
  if (Array.isArray(value.jobs)) {
    for (const item of value.jobs) {
      const found = aiAgentFindComfyuiJobPayload(item, seen);
      if (found) return found;
    }
  }
  return null;
}

function aiAgentWriteToolErrorMessage(json = {}, status = 0) {
  const candidates = [
    json.msg,
    json.message,
    json.error,
    json.result?.msg,
    json.result?.message,
    json.result?.error,
    json.payload?.msg,
    json.payload?.message,
    json.payload?.error,
    json.result?.result?.msg,
    json.result?.result?.message,
    json.result?.result?.error,
  ];
  const msg = candidates.map((item) => String(item || "").trim()).find(Boolean);
  return msg || `ComfyUI 產圖送出失敗（HTTP ${status || "-"})`;
}

async function runAiAgentComfyuiGenerate(overrides = null) {
  if (AI_AGENT_STATE.sendingTool) return;
  const canRunDirectly = aiAgentCanRunWriteTool("write_comfyui_generate");
  let elevateOnce = false;
  if (!canRunDirectly) {
    if (aiAgentCanRequestWriteElevation("write_comfyui_generate")) {
      const ok = window.confirm(
        "AI Agent 目前是唯讀/協助模式。\n\n這個請求需要本次提權執行 ComfyUI 生圖 write-tool。\n是否只允許這一次寫入？"
      );
      if (!ok) {
        const msg = "已取消本次提權，ComfyUI 產圖未送出。";
        AI_AGENT_STATE.messages.push({ role: "assistant", content: msg });
        renderAiAgentThread();
        setAiAgentMessage(msg, "info");
        return;
      }
      elevateOnce = true;
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: "已取得 root 本次提權確認，將只對這次 ComfyUI 生圖請求附加一次性寫入授權。",
      });
      renderAiAgentThread();
    } else {
      const msg = "目前不可執行 ComfyUI write-tool：需要 root 身分，且工具需在白名單或角色預設工具內。";
      AI_AGENT_STATE.messages.push({ role: "assistant", content: `ComfyUI 產圖未送出：${msg}` });
      renderAiAgentThread();
      setAiAgentMessage(msg, "err");
      return;
    }
  }
  let args = {};
  try {
    args = aiAgentComfyuiToolArguments(overrides);
    if (!args.prompt) throw new Error("請先輸入提示詞");
  } catch (err) {
    const msg = err?.message || "產圖參數不完整";
    AI_AGENT_STATE.messages.push({ role: "assistant", content: `ComfyUI 產圖未送出：${msg}` });
    renderAiAgentThread();
    setAiAgentMessage(msg, "err");
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
        elevate_once: elevateOnce ? "ALLOW_WRITE_ONCE" : "",
      }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      const msg = aiAgentWriteToolErrorMessage(json, res.status);
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: `ComfyUI 產圖送出失敗（HTTP ${res.status}）：${msg}`,
      });
      renderAiAgentThread();
      setAiAgentMessage(msg, "err");
      return;
    }
    const job = aiAgentFindComfyuiJobPayload(json) || {};
    const jobId = job.job_id || json.result?.job_id || json.payload?.job_id || json.job_id || "-";
    const initialStatus = job.status || "queued";
    AI_AGENT_STATE.messages.push({
      role: "assistant",
      content: `ComfyUI 產圖已送出，正在確認後端接收狀態。\n工具：write_comfyui_generate\nJob ID：${jobId}\n狀態：${initialStatus}`,
    });
    renderAiAgentThread();
    setAiAgentMessage("ComfyUI 產圖已送出，正在確認狀態", "info");
    if (jobId && jobId !== "-") {
      aiAgentWatchComfyuiJob(jobId);
    } else {
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: "ComfyUI 產圖已送出，但回傳內容缺少 Job ID，無法自動追蹤進度。我會嘗試從只讀任務摘要接回最近的 ComfyUI 任務。",
      });
      renderAiAgentThread();
      setAiAgentMessage("ComfyUI 回傳缺少 Job ID，嘗試從任務摘要接回", "err");
    }
    await loadAiAgentReadOnly({ scope: "all", limit: 20, silent: true, force: true }).catch(() => undefined);
  } catch (err) {
    const msg = `ComfyUI 產圖送出失敗：${err}`;
    AI_AGENT_STATE.messages.push({ role: "assistant", content: msg });
    renderAiAgentThread();
    setAiAgentMessage(msg, "err");
  } finally {
    AI_AGENT_STATE.sendingTool = false;
    renderAiAgentWriteTools();
  }
}

function aiAgentComfyuiResultSummary(job = {}) {
  const result = job.result || {};
  const images = Array.isArray(result.images) ? result.images : (result.image ? [result.image] : []);
  const names = images
    .map((item) => item?.image_ref?.filename || item?.file_ref?.filename || item?.filename || "")
    .filter(Boolean)
    .slice(0, 4);
  const count = images.length || (result.image ? 1 : 0);
  const lines = [
    "ComfyUI 產圖完成。",
    `Job ID：${job.job_id || "-"}`,
    `輸出：${count || "已產生"} 張`,
  ];
  if (names.length) lines.push(`檔案：${names.join("、")}`);
  lines.push("");
  lines.push("接下來要我怎麼處理？可以直接回覆：");
  lines.push("1. 修改參數重跑（例如：CFG 改 8、步數 30、換模型）");
  lines.push("2. 儲存或加入收藏");
  lines.push("3. 發文分享並幫你寫標題與內容");
  return lines.join("\n");
}

function aiAgentComfyuiImagesFromJob(job = {}) {
  const result = job.result || {};
  const rawImages = Array.isArray(result.images) ? result.images : (result.image ? [result.image] : []);
  return rawImages
    .map((item) => {
      const imageRef = item?.image_ref || item?.file_ref || null;
      const filename = imageRef?.filename || item?.filename || "";
      if (!imageRef || !filename) return null;
      return {
        image_ref: imageRef,
        prompt_id: item?.prompt_id || result?.image?.prompt_id || job?.progress?.prompt_id || "",
        filename,
        mime_type: item?.mime_type || "image/png",
      };
    })
    .filter(Boolean)
    .slice(0, 4);
}

function aiAgentComfyuiCompletionMessage(job = {}) {
  return {
    role: "assistant",
    content: aiAgentComfyuiResultSummary(job),
    images: aiAgentComfyuiImagesFromJob(job),
  };
}

function aiAgentComfyuiImageKey(image = {}) {
  const ref = image?.image_ref || {};
  return [ref.type || "", ref.subfolder || "", ref.filename || "", image.prompt_id || ""].join("|");
}

async function aiAgentFetchComfyuiPreview(image = {}) {
  const res = await apiFetch(`${API}/comfyui/image-preview`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image_ref: image.image_ref }),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    throw new Error(json.msg || `HTTP ${res.status}`);
  }
  return json.image || {};
}

async function aiAgentHydrateComfyuiMessageImages(message) {
  const images = Array.isArray(message?.images) ? message.images : [];
  const pending = images.filter((image) => image?.image_ref && !image.data_url && !image.error);
  if (!pending.length) return;
  await Promise.all(pending.map(async (image) => {
    const key = aiAgentComfyuiImageKey(image);
    if (AI_AGENT_STATE.comfyuiPreviewLoads[key]) return;
    AI_AGENT_STATE.comfyuiPreviewLoads[key] = true;
    try {
      const preview = await aiAgentFetchComfyuiPreview(image);
      image.data_url = preview.data_url || "";
      image.mime_type = preview.mime_type || image.mime_type || "image/png";
      image.size_bytes = preview.size_bytes || 0;
    } catch (err) {
      image.error = err?.message || String(err || "圖片預覽讀取失敗");
    } finally {
      delete AI_AGENT_STATE.comfyuiPreviewLoads[key];
    }
  }));
  renderAiAgentThread();
}

function aiAgentHydratePersistedComfyuiImages() {
  AI_AGENT_STATE.messages
    .filter((message) => Array.isArray(message.images) && message.images.some((image) => image?.image_ref && !image.data_url && !image.error))
    .forEach((message) => {
      aiAgentHydrateComfyuiMessageImages(message).catch(() => undefined);
    });
}

function aiAgentComfyuiRunningSummary(job = {}, options = {}) {
  const progress = job.progress || {};
  const detail = progress.detail || progress.phase || "running";
  const queuedLike = /佇列|queue/i.test(detail);
  const title = options.update
    ? "ComfyUI 產圖進度更新。"
    : queuedLike
      ? "ComfyUI 任務已進入後端佇列。"
      : "ComfyUI 後端已開始處理。";
  return `${title}\nJob ID：${job.job_id || "-"}\n進度：${Math.round(Number(progress.percent || 0))}%\n狀態：${detail}`;
}

function aiAgentShouldNotifyComfyuiProgress(watch = {}, job = {}) {
  const progress = job.progress || {};
  const percent = Math.max(0, Math.min(100, Math.round(Number(progress.percent || 0))));
  const detail = String(progress.detail || progress.phase || job.status || "running");
  const now = Date.now();
  if (!watch.runningNotified) {
    return { notify: true, percent, detail, initial: true };
  }
  const lastPercent = Number.isFinite(watch.lastNotifiedPercent) ? watch.lastNotifiedPercent : 0;
  const lastAt = Number.isFinite(watch.lastNotifiedAt) ? watch.lastNotifiedAt : 0;
  const detailChanged = detail && detail !== watch.lastNotifiedDetail;
  const enoughProgress = percent >= Math.min(95, lastPercent + 10);
  const enoughTimeForDetail = detailChanged && now - lastAt >= 15000;
  const heartbeat = now - lastAt >= 30000 && percent > lastPercent;
  return {
    notify: enoughProgress || enoughTimeForDetail || heartbeat,
    percent,
    detail,
    initial: false,
  };
}

function aiAgentMarkComfyuiProgressNotified(watch = {}, snapshot = {}) {
  watch.runningNotified = true;
  watch.lastNotifiedPercent = snapshot.percent;
  watch.lastNotifiedDetail = snapshot.detail;
  watch.lastNotifiedAt = Date.now();
}

function aiAgentComfyuiFailureSummary(job = {}) {
  const progress = job.progress || {};
  const detail = progress.error_message || progress.detail || job.error || "後端回報失敗，但沒有提供詳細訊息。";
  return `ComfyUI 產圖失敗。\nJob ID：${job.job_id || "-"}\n狀態：${job.status || "error"}\n錯誤：${detail}\n\n請修正模型、VAE、尺寸或提示詞後再叫我重送。`;
}

async function aiAgentFetchComfyuiJob(jobId) {
  const res = await apiFetch(`${API}/comfyui/jobs/${encodeURIComponent(jobId)}`, {
    method: "GET",
    credentials: "same-origin",
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    throw new Error(json.msg || `HTTP ${res.status}`);
  }
  return json.job || {};
}

function aiAgentWatchComfyuiJob(jobId) {
  const id = String(jobId || "").trim();
  if (!id || AI_AGENT_STATE.comfyuiWatchJobs[id]) return;
  AI_AGENT_STATE.comfyuiWatchJobs[id] = {
    startedAt: Date.now(),
    lastPhase: "",
    runningNotified: false,
    lastNotifiedPercent: -1,
    lastNotifiedDetail: "",
    lastNotifiedAt: 0,
    lastQueuedNotifiedAt: Date.now(),
  };
  aiAgentPollComfyuiJob(id);
}

async function aiAgentPollComfyuiJob(jobId) {
  const watch = AI_AGENT_STATE.comfyuiWatchJobs[jobId];
  if (!watch) return;
  try {
    const job = await aiAgentFetchComfyuiJob(jobId);
    AI_AGENT_STATE.lastComfyuiJob = job;
    const status = String(job.status || "").toLowerCase();
    const progress = job.progress || {};
    const phase = String(progress.phase || "").toLowerCase();
    if (["error", "failed", "cancelled"].includes(status) || phase === "error") {
      AI_AGENT_STATE.messages.push({ role: "assistant", content: aiAgentComfyuiFailureSummary(job) });
      renderAiAgentThread();
      setAiAgentMessage(`ComfyUI 產圖失敗：${progress.error_message || progress.detail || job.error || "未知錯誤"}`, "err");
      delete AI_AGENT_STATE.comfyuiWatchJobs[jobId];
      return;
    }
    if (status === "completed") {
      const message = aiAgentComfyuiCompletionMessage(job);
      AI_AGENT_STATE.messages.push(message);
      renderAiAgentThread();
      setAiAgentMessage("ComfyUI 產圖完成", "ok");
      delete AI_AGENT_STATE.comfyuiWatchJobs[jobId];
      aiAgentHydrateComfyuiMessageImages(message).catch(() => undefined);
      await loadAiAgentReadOnly({ scope: "all", limit: 20, silent: true, force: true }).catch(() => undefined);
      return;
    }
    if (status === "running") {
      const progressNotice = aiAgentShouldNotifyComfyuiProgress(watch, job);
      if (progressNotice.notify) {
        aiAgentMarkComfyuiProgressNotified(watch, progressNotice);
        AI_AGENT_STATE.messages.push({
          role: "assistant",
          content: aiAgentComfyuiRunningSummary(job, { update: !progressNotice.initial }),
        });
        renderAiAgentThread();
        setAiAgentMessage(progressNotice.initial && /佇列|queue/i.test(progressNotice.detail)
          ? "ComfyUI 任務已進入後端佇列"
          : progressNotice.initial
            ? "ComfyUI 後端已開始處理"
            : `ComfyUI 產圖進度 ${progressNotice.percent}%`, "ok");
      }
    }
    if (status === "queued") {
      const now = Date.now();
      if (!watch.lastQueuedNotifiedAt || now - watch.lastQueuedNotifiedAt >= 30000) {
        watch.lastQueuedNotifiedAt = now;
        AI_AGENT_STATE.messages.push({
          role: "assistant",
          content: `ComfyUI 任務仍在佇列中。\nJob ID：${jobId}\n狀態：${job.status || "queued"}`,
        });
        renderAiAgentThread();
        setAiAgentMessage("ComfyUI 任務仍在佇列中", "info");
      }
    }
    const elapsed = Date.now() - watch.startedAt;
    if (elapsed >= 30 * 60 * 1000) {
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: `ComfyUI 任務仍未完成。\nJob ID：${jobId}\n狀態：${job.status || "queued"}\n我先停止自動追蹤，之後你可以叫我查產圖進度。`,
      });
      renderAiAgentThread();
      setAiAgentMessage("ComfyUI 任務追蹤已超過 30 分鐘", "info");
      delete AI_AGENT_STATE.comfyuiWatchJobs[jobId];
      return;
    }
    const delay = elapsed < 15000 ? 2000 : 5000;
    setTimeout(() => aiAgentPollComfyuiJob(jobId), delay);
  } catch (err) {
    AI_AGENT_STATE.messages.push({
      role: "assistant",
      content: `ComfyUI 任務狀態確認失敗。\nJob ID：${jobId}\n錯誤：${err?.message || err}`,
    });
    renderAiAgentThread();
    setAiAgentMessage(`ComfyUI 任務狀態確認失敗：${err?.message || err}`, "err");
    delete AI_AGENT_STATE.comfyuiWatchJobs[jobId];
  }
}

async function aiAgentConfirmComfyuiJob(jobId) {
  const delays = [800, 1200, 1800, 2500, 3500];
  let lastJob = null;
  for (const delay of delays) {
    await new Promise((resolve) => setTimeout(resolve, delay));
    const res = await apiFetch(`${API}/comfyui/jobs/${encodeURIComponent(jobId)}`, {
      method: "GET",
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      const msg = json.msg || `HTTP ${res.status}`;
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: `ComfyUI 任務狀態確認失敗。\nJob ID：${jobId}\n錯誤：${msg}`,
      });
      renderAiAgentThread();
      setAiAgentMessage(`ComfyUI 任務狀態確認失敗：${msg}`, "err");
      return;
    }
    lastJob = json.job || {};
    lastJob.job_id = lastJob.job_id || jobId;
    const status = String(lastJob.status || "").toLowerCase();
    const progress = lastJob.progress || {};
    const detail = progress.error_message || progress.detail || lastJob.error || "";
    if (["error", "failed", "cancelled"].includes(status) || String(progress.phase || "").toLowerCase() === "error") {
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: aiAgentComfyuiFailureSummary(lastJob),
      });
      renderAiAgentThread();
      setAiAgentMessage(`ComfyUI 產圖失敗：${detail || lastJob.error || "未知錯誤"}`, "err");
      return;
    }
    if (status === "completed") {
      const message = aiAgentComfyuiCompletionMessage(lastJob);
      AI_AGENT_STATE.messages.push(message);
      renderAiAgentThread();
      setAiAgentMessage("ComfyUI 產圖完成", "ok");
      aiAgentHydrateComfyuiMessageImages(message).catch(() => undefined);
      return;
    }
    if (status === "running") {
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: aiAgentComfyuiRunningSummary(lastJob),
      });
      renderAiAgentThread();
      setAiAgentMessage("ComfyUI 後端已開始處理", "ok");
      return;
    }
  }
  const progress = lastJob?.progress || {};
  AI_AGENT_STATE.messages.push({
    role: "assistant",
    content: `ComfyUI 任務仍在等待後端確認。\nJob ID：${jobId}\n狀態：${lastJob?.status || "queued"}\n提示：若 ComfyUI 後台沒有看到任務，請稍後查詢進度；若後端驗證失敗，系統會在任務狀態中顯示錯誤。`,
  });
  renderAiAgentThread();
  setAiAgentMessage("ComfyUI 任務仍在等待後端確認", "info");
}

function renderAiAgentThread(options = {}) {
  const host = $("ai-agent-thread");
  if (!host) return;
  if (!options.skipPersist) aiAgentSchedulePersistConversation();
  if (!AI_AGENT_STATE.messages.length) {
    host.innerHTML = '<div class="drive-empty">目前沒有訊息</div>';
    return;
  }
  host.innerHTML = AI_AGENT_STATE.messages.map((message) => {
    const role = message.role === "assistant" ? "assistant" : "user";
    const label = role === "assistant" ? "AI" : "你";
    const imageHtml = aiAgentRenderMessageImages(message);
    return `
      <div class="ai-agent-message ${role}">
        <div class="ai-agent-message-role">${sanitize(label)}</div>
        <div class="ai-agent-message-body">${sanitize(message.content || "")}</div>
        ${imageHtml}
      </div>
    `;
  }).join("");
  host.scrollTop = host.scrollHeight;
}

function aiAgentRenderMessageImages(message = {}) {
  const images = Array.isArray(message.images) ? message.images : [];
  if (!images.length) return "";
  return `
    <div class="ai-agent-image-results">
      ${images.map((image, index) => {
        const filename = image.filename || image?.image_ref?.filename || `output-${index + 1}.png`;
        if (image.data_url) {
          return `
            <a class="ai-agent-image-result" href="${sanitize(image.data_url)}" download="${sanitize(filename)}" title="開啟或下載 ${sanitize(filename)}">
              <img src="${sanitize(image.data_url)}" alt="${sanitize(filename)}" loading="lazy" />
              <span>${sanitize(filename)}</span>
            </a>
          `;
        }
        return `
          <div class="ai-agent-image-result loading">
            <span>${sanitize(image.error ? `圖片預覽讀取失敗：${image.error}` : `圖片預覽載入中：${filename}`)}</span>
          </div>
        `;
      }).join("")}
    </div>
  `;
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

function aiAgentResumeComfyuiWatchJobs(payload = {}) {
  const jobs = Array.isArray(payload.comfyui_jobs) ? payload.comfyui_jobs : [];
  jobs.forEach((job) => {
    const jobId = String(job?.job_id || "").trim();
    const status = String(job?.status || "").toLowerCase();
    if (!jobId || !["queued", "running", "pending"].includes(status)) return;
    if (AI_AGENT_STATE.comfyuiWatchJobs[jobId]) return;
    AI_AGENT_STATE.messages.push({
      role: "assistant",
      content: `接回 ComfyUI 任務進度追蹤。\nJob ID：${jobId}\n狀態：${job.status || "running"}\n進度：${Math.round(Number(job.progress_percent || 0))}%`,
    });
    renderAiAgentThread();
    aiAgentWatchComfyuiJob(jobId);
  });
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

function aiAgentImageTransportError(err) {
  const raw = String(err?.message || err || "Load failed").trim();
  return `圖片分析請求傳輸失敗：${raw}。請重試或改用較小圖片；若仍失敗，代表目前 Hermes/Ollama vision 後端不可用。`;
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
    `上次掃描：${aiAgentAuditTimeLabel(scheduler.last_scanned_at)}`,
    `下次預計：${scheduler.enabled ? aiAgentAuditTimeLabel(scheduler.next_due_at, "-") : "-"}`,
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
    aiAgentResumeComfyuiWatchJobs(json);
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
  const conversationId = AI_AGENT_STATE.sessionId || "default";
  apiFetch(API + "/ai-agent/conversation", {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: conversationId }),
  }).catch(() => undefined);
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
  if (mode === "text" && aiAgentWantsComfyuiGeneration(prompt) && !directComfyuiArgs && !aiAgentComfyuiTextHasSubject(prompt)) {
    const userText = prompt;
    AI_AGENT_STATE.messages.push({ role: "user", content: userText });
    AI_AGENT_STATE.messages.push({ role: "assistant", content: aiAgentComfyuiClarificationMessage() });
    renderAiAgentThread();
    if (input) input.value = "";
    setAiAgentMessage("需要補充生圖提示詞後才能執行", "info");
    return;
  }
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
  const writeIntent = mode === "text" ? aiAgentWriteIntent(prompt) : null;
  if (writeIntent) {
    if (!AI_AGENT_STATE.loaded && typeof loadAiAgentStatus === "function") {
      await loadAiAgentStatus({ force: true }).catch(() => undefined);
    }
    const userText = prompt;
    AI_AGENT_STATE.messages.push({ role: "user", content: userText });
    AI_AGENT_STATE.messages.push({ role: "assistant", content: aiAgentWriteIntentFollowup(writeIntent) });
    renderAiAgentThread();
    if (input) input.value = "";
    setAiAgentMessage("需要補充資料後才能執行寫入", "info");
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
      image_data_url: "",
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
      const shownMsg = mode === "image"
        ? aiAgentImageAnalysisError(json, res.status)
        : (msg || "AI Agent 回應失敗");
      AI_AGENT_STATE.messages.push({ role: "assistant", content: shownMsg });
      if (!json.ok) {
        setAiAgentMessage(mode === "image" ? shownMsg : (json.msg ? `${json.msg} ${statusHint}` : `AI Agent 回應失敗 ${statusHint}`), "err");
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
    const msg = mode === "image" ? aiAgentImageTransportError(err) : `AI Agent 回應失敗：${err}`;
    AI_AGENT_STATE.messages.push({ role: "assistant", content: msg });
    renderAiAgentThread();
    setAiAgentMessage(msg, "err");
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
aiAgentHydratePersistedComfyuiImages();
