'use strict';

const AI_AGENT_STATE = {
  loaded: false,
  loading: false,
  sending: false,
  sendingTool: false,
  readonlyLoading: false,
  messages: [],
  imageDataUrl: "",
  imageLoading: false,
  settings: {},
  actor: {},
  audit: {},
  modelIds: [],
  unavailableModelIds: new Set(),
  unavailableModelReasons: {},
  sessionId: "",
  accountScope: "",
  comfyuiWatchJobs: {},
  comfyuiSubmittedJobs: {},
  comfyuiAttemptHistory: [],
  comfyuiAnnouncedJobs: {},
  lastComfyuiJob: null,
  lastComfyuiArgs: null,
  comfyuiPreviewLoads: {},
  persistTimer: null,
  loadingConversation: false,
  historyLoading: false,
  historyOpen: false,
  conversationHistory: [],
  historySelected: null,
  writeToolCatalog: [],
  writeToolEnabled: new Set(),
  writeToolLoading: false,
  writeToolSaving: false,
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
  AI_AGENT_STATE.imageLoading = false;
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

function aiAgentRenderHistoryDetail(item = null) {
  const host = $("ai-agent-history-detail");
  if (!host) return;
  if (!item || !item.payload) {
    host.innerHTML = '<div class="drive-empty">選擇一筆歷史對話後查看內容</div>';
    return;
  }
  const messages = Array.isArray(item.payload.messages) ? item.payload.messages : [];
  if (!messages.length) {
    host.innerHTML = '<div class="drive-empty">這筆歷史對話沒有訊息</div>';
    return;
  }
  host.innerHTML = messages.map((message) => {
    const role = message.role === "assistant" ? "assistant" : "user";
    const label = role === "assistant" ? "AI" : "使用者";
    return `
      <div class="ai-agent-message ${role}">
        <div class="ai-agent-message-role">${sanitize(label)}</div>
        <div class="ai-agent-message-body">${sanitize(message.content || "")}</div>
        ${aiAgentRenderMessageImages(message)}
      </div>
    `;
  }).join("");
  aiAgentScrollElementToBottom(host);
}

function renderAiAgentConversationHistory() {
  const panel = $("ai-agent-history-panel");
  const list = $("ai-agent-history-list");
  const state = $("ai-agent-history-state");
  const button = $("ai-agent-history-btn");
  const canView = aiAgentCanViewConversationHistory();
  if (button) button.hidden = !canView;
  if (panel) panel.hidden = !canView || !AI_AGENT_STATE.historyOpen;
  if (!canView || !list || !state) return;
  if (AI_AGENT_STATE.historyLoading) {
    state.textContent = "歷史對話載入中...";
    return;
  }
  const rows = AI_AGENT_STATE.conversationHistory || [];
  state.textContent = rows.length
    ? `共 ${rows.length} 筆最近 AI Agent 歷史對話`
    : "目前沒有可回顧的歷史對話";
  list.innerHTML = rows.map((item, index) => {
    const title = `${item.owner_username || item.owner_user_id || "-"} / ${item.conversation_id || "default"}`;
    const time = item.updated_at || item.created_at || "";
    const preview = item.last_user || item.last_assistant || "沒有摘要";
    return `
      <button class="drive-file-row" type="button"
        data-ai-agent-history-index="${index}"
        title="${sanitize(preview)}">
        <strong>${sanitize(title)}</strong>
        <span>${sanitize(time)}，${Number(item.message_count || 0)} 則，session ${sanitize(item.session_binding || "-")}</span>
        <span>${sanitize(preview)}</span>
      </button>
    `;
  }).join("");
  list.querySelectorAll("[data-ai-agent-history-index]").forEach((row) => {
    row.addEventListener("click", () => {
      const index = parseInt(row.getAttribute("data-ai-agent-history-index") || "-1", 10);
      const item = AI_AGENT_STATE.conversationHistory[index];
      if (item) loadAiAgentConversationHistoryPayload(item);
    });
  });
  aiAgentRenderHistoryDetail(AI_AGENT_STATE.historySelected);
}

async function loadAiAgentConversationHistory(options = {}) {
  if (!aiAgentCanViewConversationHistory()) return;
  if (AI_AGENT_STATE.historyLoading && !options.force) return;
  AI_AGENT_STATE.historyLoading = true;
  renderAiAgentConversationHistory();
  try {
    const res = await apiFetch(`${API}/ai-agent/conversation-history?limit=50`, {
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      setAiAgentMessage(json.msg || "AI Agent 歷史對話讀取失敗", "err");
      return;
    }
    AI_AGENT_STATE.conversationHistory = Array.isArray(json.conversations) ? json.conversations : [];
    if (!AI_AGENT_STATE.historySelected && AI_AGENT_STATE.conversationHistory.length) {
      AI_AGENT_STATE.historySelected = null;
    }
  } catch (err) {
    setAiAgentMessage(`AI Agent 歷史對話讀取失敗：${err}`, "err");
  } finally {
    AI_AGENT_STATE.historyLoading = false;
    renderAiAgentConversationHistory();
  }
}

async function loadAiAgentConversationHistoryPayload(item = {}) {
  if (!aiAgentCanViewConversationHistory()) return;
  const params = new URLSearchParams({
    limit: "1",
    include_payload: "1",
    owner_user_id: String(item.owner_user_id || ""),
    session_binding: String(item.session_binding || ""),
    conversation_id: String(item.conversation_id || "default"),
  });
  try {
    const res = await apiFetch(`${API}/ai-agent/conversation-history?${params.toString()}`, {
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok || !Array.isArray(json.conversations) || !json.conversations.length) {
      setAiAgentMessage(json.msg || "歷史對話內容讀取失敗", "err");
      return;
    }
    AI_AGENT_STATE.historySelected = json.conversations[0];
    renderAiAgentConversationHistory();
    setAiAgentMessage("已載入歷史對話", "ok");
  } catch (err) {
    setAiAgentMessage(`歷史對話內容讀取失敗：${err}`, "err");
  }
}

function toggleAiAgentConversationHistory() {
  if (!aiAgentCanViewConversationHistory()) return;
  AI_AGENT_STATE.historyOpen = !AI_AGENT_STATE.historyOpen;
  renderAiAgentConversationHistory();
  if (AI_AGENT_STATE.historyOpen) loadAiAgentConversationHistory({ force: true });
}

function aiAgentResetScopeState() {
  const nextScope = aiAgentCurrentAccountScope();
  const previousScope = AI_AGENT_STATE.accountScope;
  if (previousScope && previousScope !== nextScope) {
    aiAgentPersistConversation(previousScope);
  }
  AI_AGENT_STATE.accountScope = nextScope;
  if (!previousScope || previousScope !== nextScope) {
    AI_AGENT_STATE.historyOpen = false;
    AI_AGENT_STATE.conversationHistory = [];
    AI_AGENT_STATE.historySelected = null;
    AI_AGENT_STATE.writeToolCatalog = [];
    AI_AGENT_STATE.writeToolEnabled = new Set();
    AI_AGENT_STATE.writeToolGuard = {};
    aiAgentLoadConversation(nextScope);
    renderAiAgentThread();
    renderAiAgentConversationHistory();
    renderAiAgentToolSelector();
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

function aiAgentRequestTimeoutMs(mode = "text") {
  const configured = parseInt(AI_AGENT_STATE.settings?.request_timeout_seconds || "", 10);
  const seconds = Number.isFinite(configured) && configured > 0 ? configured : (mode === "image" ? 180 : 120);
  return Math.max(10000, Math.min(610000, (seconds + 10) * 1000));
}

async function aiAgentChatFetch(payload, options = {}) {
  const mode = options.mode || payload?.mode || "text";
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timeoutMs = aiAgentRequestTimeoutMs(mode);
  const timer = controller
    ? setTimeout(() => controller.abort(), timeoutMs)
    : null;
  try {
    return await apiFetch(API + "/ai-agent/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      ...(controller ? { signal: controller.signal } : {}),
    });
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error(`AI Agent 請求逾時（${Math.round(timeoutMs / 1000)} 秒），已中止前端等待。`);
    }
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function aiAgentCanViewConversationHistory() {
  const actor = AI_AGENT_STATE.actor || {};
  return actor.username === "root" || actor.role === "super_admin";
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

function aiAgentPromptFingerprint(value) {
  return aiAgentStripFieldValue(value).replace(/\s+/g, " ").toLowerCase();
}

function aiAgentLooksLikeStaleImageEditPrompt(prompt, generationMode, sourceImageRef) {
  if (!sourceImageRef || !["img2img", "inpaint", "outpaint", "upscale"].includes(generationMode)) return false;
  const promptKey = aiAgentPromptFingerprint(prompt);
  if (!promptKey) return false;
  const lastKey = aiAgentPromptFingerprint(AI_AGENT_STATE.lastComfyuiArgs?.prompt || "");
  return !!lastKey && promptKey === lastKey;
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
    /^\s*(?:負面提示詞|負面詞|反向提示詞|反向詞|negative prompt|negative|neg)\s*(?:加上|加入|新增|改成|設為|變成)?\s*[:：]?\s*(.+)$/i,
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
  const negative = aiAgentLineValue(raw, [
    /^\s*(?:負面提示詞|負面詞|反向提示詞|反向詞|negative prompt|negative|neg)\s*(?:加上|加入|新增|改成|設為|變成)?\s*[:：]?\s*(.+)$/i,
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

function aiAgentHasComfyuiOverrideIntent(text) {
  const raw = aiAgentNormalizeUserText(text).trim();
  if (!raw) return false;
  if (/(負面提示詞|負面詞|反向提示詞|反向詞|negative prompt|negative|neg|cfg|steps?|步數|尺寸|解析度|模型|checkpoint|ckpt|vae|seed|種子|張數|數量)/i.test(raw)
    && /(加上|加入|新增|改成|設為|變成|調整|修改|重跑|再跑|再生|重新|rerun|again|換成|改)/i.test(raw)) {
    return true;
  }
  return /(再來|再一張|再跑|重跑|再生|重新產圖|重新生圖|rerun|run again)/i.test(raw);
}

function aiAgentMergeCommaList(base, extra) {
  const values = [];
  String(base || "").split(/[，,]/).concat(String(extra || "").split(/[，,]/)).forEach((item) => {
    const value = item.trim();
    if (value && !values.some((existing) => existing.toLowerCase() === value.toLowerCase())) values.push(value);
  });
  return values.join(", ");
}

function aiAgentCurrentComfyuiArgs() {
  if (AI_AGENT_STATE.lastComfyuiArgs?.prompt) return { ...AI_AGENT_STATE.lastComfyuiArgs };
  try {
    const args = aiAgentComfyuiToolArguments();
    return args?.prompt ? args : null;
  } catch (err) {
    return null;
  }
}

function aiAgentParseComfyuiRerunRequest(text) {
  if (!aiAgentHasComfyuiOverrideIntent(text)) return null;
  const base = aiAgentCurrentComfyuiArgs();
  if (!base?.prompt) return null;
  const raw = aiAgentNormalizeUserText(text);
  const overrides = aiAgentParseComfyuiOptionOverrides(raw);
  const merged = { ...base, ...overrides, confirm_billing: true };
  if (overrides.negative_prompt && /(加上|加入|新增|append|add)/i.test(raw)) {
    merged.negative_prompt = aiAgentMergeCommaList(base.negative_prompt, overrides.negative_prompt);
  }
  return merged;
}

function aiAgentRememberComfyuiSubmit(args = {}, job = {}) {
  if (args?.prompt) {
    AI_AGENT_STATE.lastComfyuiArgs = {
      ...args,
      prompt: String(args.prompt || "").trim(),
      negative_prompt: String(args.negative_prompt || "").trim(),
    };
  }
  const jobId = String(job?.job_id || "").trim();
  if (jobId) {
    AI_AGENT_STATE.comfyuiSubmittedJobs[jobId] = {
      job_id: jobId,
      status: job.status || "queued",
      args: AI_AGENT_STATE.lastComfyuiArgs ? { ...AI_AGENT_STATE.lastComfyuiArgs } : {},
      submittedAt: Date.now(),
    };
  }
}

function aiAgentCleanComfyuiArgs(args = {}) {
  const cleaned = { ...(args || {}) };
  const autoLike = /^(auto|automatic|default|none|null|undefined|自動|預設)$/i;
  ["vae", "checkpoint", "sampler", "sampler_name", "scheduler", "official_workflow_id", "generation_mode"].forEach((key) => {
    const value = String(cleaned[key] || "").trim();
    if (!value || (key === "vae" && autoLike.test(value))) delete cleaned[key];
    else cleaned[key] = value;
  });
  Object.keys(cleaned).forEach((key) => {
    if (cleaned[key] === "" || cleaned[key] === undefined || cleaned[key] === null) delete cleaned[key];
  });
  return cleaned;
}

function aiAgentNormalizeComfyuiGenerationMode(value = "") {
  const raw = String(value || "").trim().toLowerCase();
  if (!raw) return "";
  const key = raw.replace(/[\s_-]+/g, "");
  const aliases = {
    texttoimage: "txt2img",
    txt2img: "txt2img",
    t2i: "txt2img",
    imagetoimage: "img2img",
    img2img: "img2img",
    i2i: "img2img",
    style: "img2img",
    styletransfer: "img2img",
    restyle: "img2img",
    "風格化": "img2img",
    "改風格": "img2img",
    inpaint: "inpaint",
    inpainting: "inpaint",
    "局部重繪": "inpaint",
    outpaint: "outpaint",
    outpainting: "outpaint",
    "外延": "outpaint",
    "向外延展": "outpaint",
    upscale: "upscale",
  };
  return aliases[key] || raw;
}

function aiAgentRecentImageRefs(limit = 8) {
  const refs = [];
  const seen = new Set();
  AI_AGENT_STATE.messages.slice().reverse().forEach((message) => {
    (Array.isArray(message.images) ? message.images : []).forEach((image) => {
      const ref = image?.image_ref || null;
      const filename = ref?.filename || image?.filename || "";
      if (!ref || !filename) return;
      const key = [ref.type || "", ref.subfolder || "", filename].join("|");
      if (seen.has(key)) return;
      seen.add(key);
      refs.push({
        filename,
        prompt_id: String(image?.prompt_id || "").slice(0, 160),
        mime_type: String(image?.mime_type || "").slice(0, 80),
        image_ref: ref,
      });
    });
  });
  return refs.slice(0, limit);
}

function aiAgentRememberComfyuiAttempt(args = {}, patch = {}) {
  const existingId = patch.attempt_id || "";
  const cleanedArgs = aiAgentCleanComfyuiArgs(args);
  let item = existingId ? AI_AGENT_STATE.comfyuiAttemptHistory.find((entry) => entry.attempt_id === existingId) : null;
  if (!item) {
    item = {
      attempt_id: `attempt-${Date.now()}-${AI_AGENT_STATE.comfyuiAttemptHistory.length + 1}`,
      version: AI_AGENT_STATE.comfyuiAttemptHistory.length + 1,
      createdAt: Date.now(),
      args: cleanedArgs,
      status: "planned",
      job_id: "",
      error: "",
    };
    AI_AGENT_STATE.comfyuiAttemptHistory.push(item);
  }
  item.args = Object.keys(cleanedArgs).length ? cleanedArgs : item.args;
  if (patch.status) item.status = patch.status;
  if (patch.job_id) item.job_id = patch.job_id;
  if (patch.error !== undefined) item.error = String(patch.error || "");
  item.updatedAt = Date.now();
  AI_AGENT_STATE.comfyuiAttemptHistory = AI_AGENT_STATE.comfyuiAttemptHistory.slice(-12);
  return item;
}

function aiAgentNormalizeComfyuiAttemptStatus(status = "", error = "") {
  const raw = String(status || "").trim().toLowerCase();
  if (error) return "error";
  if (["error", "failed", "cancelled", "timeout"].includes(raw)) return "error";
  if (raw === "completed") return "completed";
  if (raw === "running") return "running";
  if (raw === "queued") return "queued";
  if (raw === "sending") return "sending";
  return raw || "planned";
}

function aiAgentUpdateComfyuiAttemptFromJob(job = {}) {
  const jobId = String(job?.job_id || "").trim();
  if (!jobId) return;
  const item = (AI_AGENT_STATE.comfyuiAttemptHistory || []).find((entry) => entry.job_id === jobId);
  if (!item) return;
  const progress = job.progress || {};
  const status = String(job.status || item.status || "").trim();
  const error = progress.error_message || progress.detail || job.error || "";
  item.status = aiAgentNormalizeComfyuiAttemptStatus(status || item.status, item.error);
  if (["error", "failed", "cancelled"].includes(status.toLowerCase()) || String(progress.phase || "").toLowerCase() === "error") {
    item.status = "error";
    item.error = error || "未知錯誤";
  } else if (status === "completed") {
    item.status = "completed";
    item.error = "";
  }
  item.updatedAt = Date.now();
}

function aiAgentMarkComfyuiAttemptError(jobId = "", error = "") {
  const id = String(jobId || "").trim();
  if (!id) return;
  const item = (AI_AGENT_STATE.comfyuiAttemptHistory || []).find((entry) => entry.job_id === id);
  if (!item) return;
  item.status = "error";
  item.error = String(error || "未知錯誤");
  item.updatedAt = Date.now();
}

function aiAgentPlannerContext(options = {}) {
  const submittedJobs = Object.values(AI_AGENT_STATE.comfyuiSubmittedJobs || {})
    .sort((a, b) => Number(b.submittedAt || 0) - Number(a.submittedAt || 0))
    .slice(0, 6)
    .map((job) => ({
      job_id: job.job_id || "",
      status: job.status || "",
      generation_mode: job.args?.generation_mode || "txt2img",
      prompt: job.args?.prompt || "",
      negative_prompt: job.args?.negative_prompt || "",
      source_image_ref: job.args?.source_image_ref || null,
      mask_image_ref: job.args?.mask_image_ref || null,
      submitted_at: job.submittedAt || 0,
    }));
  const messages = AI_AGENT_STATE.messages.slice(-10).map((message) => ({
    role: message.role === "assistant" ? "assistant" : "user",
    content: String(message.content || "").slice(0, 1200),
    images: Array.isArray(message.images)
      ? message.images.slice(0, 4).map((image) => ({
        filename: image?.image_ref?.filename || image?.filename || "",
        prompt_id: String(image?.prompt_id || "").slice(0, 160),
        image_ref: image?.image_ref || null,
      })).filter((image) => image.image_ref && image.filename)
      : [],
  }));
  const effectiveTools = Array.isArray(AI_AGENT_STATE.settings?.tools)
    ? AI_AGENT_STATE.settings.tools.map((tool) => ({
      name: tool.name || "",
      label: tool.label || "",
      description: tool.description || "",
      data_scope: tool.data_scope || "",
      write: !!tool.write,
      available: aiAgentHasEffectiveTool(tool.name || ""),
      can_execute_now: tool.name ? aiAgentCanRunWriteTool(tool.name) : false,
      can_request_elevation: tool.name ? aiAgentCanRequestWriteElevation(tool.name) : false,
    }))
    : [];
  return {
    input_mode: options.mode || "text",
    has_image: !!options.hasImage,
    actor: {
      role: AI_AGENT_STATE.actor?.role || "anonymous",
      username: AI_AGENT_STATE.actor?.username || "",
    },
    operation_mode: AI_AGENT_STATE.settings?.operation_mode || "assist",
    operation_mode_policy: AI_AGENT_STATE.settings?.operation_mode_policy || {},
    readonly_tools: [
      {
        scope: "resources",
        purpose: "回答目前系統環境、CPU、RAM、disk、GPU/資源負載類問題。",
      },
      {
        scope: "server_mode",
        purpose: "回答目前伺服器模式、安全 profile、上線需求 gate 與 incident 狀態。",
      },
      {
        scope: "comfyui",
        purpose: "回答目前能否生圖、ComfyUI 連線、模型/模式清單、生圖任務進度與結果。",
      },
      {
        scope: "remote_download",
        purpose: "回答下載任務、遠端下載、佇列與完成狀態。",
      },
      {
        scope: "attack_diag",
        purpose: "回答安全審計、攻擊診斷、異常流量、IP、log、近期失敗任務與事件。",
      },
      {
        scope: "all",
        purpose: "只在使用者明確要求全站總覽、整體狀態或跨多模組摘要時使用。",
      },
    ],
    effective_tools: effectiveTools,
    writable_tools: effectiveTools.filter((tool) => tool.write && (tool.can_execute_now || tool.can_request_elevation)).map((tool) => tool.name),
    recent_image_refs: aiAgentRecentImageRefs(8),
    last_comfyui_args: aiAgentCurrentComfyuiArgs() || null,
    last_comfyui_job: AI_AGENT_STATE.lastComfyuiJob ? {
      job_id: AI_AGENT_STATE.lastComfyuiJob.job_id || "",
      status: AI_AGENT_STATE.lastComfyuiJob.status || "",
      progress: AI_AGENT_STATE.lastComfyuiJob.progress || {},
      images: aiAgentComfyuiImagesFromJob(AI_AGENT_STATE.lastComfyuiJob),
    } : null,
    submitted_comfyui_jobs: submittedJobs,
    comfyui_attempt_history: (AI_AGENT_STATE.comfyuiAttemptHistory || []).slice(-6).map((item) => ({
      version: item.version,
      status: item.status,
      job_id: item.job_id || "",
      error: item.error || "",
      generation_mode: item.args?.generation_mode || "txt2img",
      prompt: item.args?.prompt || "",
      negative_prompt: item.args?.negative_prompt || "",
      source_image_ref: item.args?.source_image_ref || null,
      mask_image_ref: item.args?.mask_image_ref || null,
      width: item.args?.width,
      height: item.args?.height,
      steps: item.args?.steps,
    })),
    recent_messages: messages,
  };
}

function aiAgentShouldUseToolPlanner(text) {
  const raw = aiAgentNormalizeUserText(text).trim();
  if (!raw) return false;
  return true;
}

async function aiAgentPlanToolAction(userText, options = {}) {
  if (!aiAgentShouldUseToolPlanner(userText)) return null;
  const selectedModel = aiAgentSelectedTextModel();
  const context = aiAgentPlannerContext(options);
  const planningPrompt = [
    "你是網站 AI Agent 的工具路由器。你的任務是理解使用者意圖、檢查可用工具與權限，然後只輸出 JSON 決策。",
    "不要用關鍵字索引決策；請根據完整上下文、使用者目的、input_mode、has_image、readonly_tools、effective_tools、operation_mode_policy 判斷。",
    "可用 action：chat、clarify、readonly、comfyui_status、comfyui_generate、comfyui_rerun、write_tool、community_post_draft。",
    "JSON 欄位：action, confidence, reason, question, readonly_scope, merge_strategy, execute_write, tool, args。",
    "readonly_scope 必須從 context.readonly_tools 的 scope 中選最貼近使用者目的的一項；除非使用者明確要求全站總覽，否則不可使用 all。",
    "args 對 comfyui_generate 可含：prompt, negative_prompt, width, height, steps, cfg_scale, cfg, batch_size, seed, checkpoint, vae, sampler, sampler_name, scheduler, official_workflow_id, generation_mode, source_image_ref, mask_image_ref, denoise_strength, outpaint_left, outpaint_top, outpaint_right, outpaint_bottom, outpaint_feathering。",
    "args 對 write_tool 應依 context.effective_tools 的工具語意填入站內欄位；例如頭像工具可填 user_id, cloud_file_id, crop{x,y,width,height,rotation}, zoom, decision_reason。",
    "工具語意：readonly=讀取指定 readonly_scope 的站內唯讀資料；comfyui_status=讀取 ComfyUI 目前可用性與生圖進度；comfyui_generate=建立新的 ComfyUI 生圖任務；comfyui_rerun=沿用上一筆生圖參數並套用使用者修改；write_tool=執行 context.effective_tools 中的白名單站內工具；community_post_draft=只產生發文草稿，不直接發布。",
    "若 action=write_tool，tool 必須完全等於 context.effective_tools[].name，args 只能包含使用者明確提供或可從 recent_messages/站內上下文推得的站內欄位；不得產生 shell、SQL、外部檔案路徑或站外操作。",
    "若 action=write_tool 且使用者明確要求建立、更新、刪除、執行、下載、轉帳、交易或治理處置，execute_write 必須是 true；只有使用者要草稿、詢問、資料不足或權限不足時才可為 false。",
    "若使用者目的需要工具，但 effective_tools 或權限不足，仍可輸出該 action；前端會處理提權、拒絕或反問。",
    "若使用者目的不明或缺少必要資料，action=clarify 並用 question 提出一個具體反問。",
    "若使用者以短句詢問某件事是否開始、完成、跑出結果或目前進度，請先依 recent_messages 與 submitted_comfyui_jobs 判斷目標；若仍不確定，action=readonly 並 readonly_scope=all，讓前端回報真實可見任務狀態。",
    "若使用者要求修改、重繪、風格化、套風格、以圖生圖或把上一張/剛剛那張圖再加工，action=comfyui_generate、execute_write=true，並用 context.recent_image_refs 或 last_comfyui_job.images 的 image_ref 填 source_image_ref；風格化/以圖生圖 generation_mode=img2img，局部重繪 generation_mode=inpaint 且需要 mask_image_ref，向外延展 generation_mode=outpaint 且填 outpaint_* 邊界。",
    "comfyui_generate 的 prompt 不可空白；圖生圖/風格化時若使用者只描述修改方向，請把修改方向整理成可執行 prompt。",
    "圖生圖/風格化/外延/局部重繪時，prompt 必須描述本輪要修改的方向；不可只複製 context.last_comfyui_args.prompt，除非使用者明確要求完全沿用原 prompt。",
    "若 inpaint 缺少可用 mask_image_ref，action=clarify，question 只問使用者要提供 mask 或改用 img2img/outpaint；不要假裝能局部重繪。",
    "若 outpaint 未指定方向或像素，可用 128px 與 feathering 48 作安全預設；若 style change 未指定 denoise_strength，可用 0.55-0.75。",
    "若 input_mode=image，請用語意判斷使用者是要圖片問答、圖片分析產 prompt，還是要求用附圖執行生圖；只有明確要求執行寫入的情況才可輸出 comfyui_generate 並設 execute_write=true。",
    "若 input_mode=image 且使用者明確要求用附圖執行生圖，即使未提供 prompt、尺寸或步數，也應輸出 comfyui_generate 並設 execute_write=true；前端會先用 vision 模型分析圖片並補齊安全預設參數。",
    "若 input_mode=image 且使用者意圖依上下文仍不明，請輸出 chat 或 clarify；不得設定 execute_write=true，也不得暗示已送出任何寫入工具。",
    "checkpoint 只能填使用者明確提供的實際 checkpoint 名稱；泛稱模型請省略 checkpoint，必要時用 official_workflow_id。",
    "不要產生教學文字，不要宣稱已送出、正在查詢或正在執行；若 action 需要工具，由前端執行後回報實際結果。",
    `context=${JSON.stringify(context)}`,
    `user=${userText}`,
  ].join("\n");
  const started = performance.now();
  const res = await aiAgentChatFetch({
    session_id: aiAgentEnsureSessionId(),
    model: selectedModel,
    mode: "text",
    messages: [{ role: "user", content: planningPrompt }],
    image_data_url: "",
  }, {
    mode: "text",
  });
  const json = await res.json().catch(() => ({}));
  const content = json?.message?.content || json?.msg || "";
  if (!res.ok || !json.ok || isMockAiAgentReply(content)) return null;
  const plan = aiAgentExtractJsonObject(content);
  if (!plan || typeof plan !== "object") return null;
  plan.elapsedMs = Math.round(performance.now() - started);
  return plan;
}

function aiAgentPlannerArgs(plan = {}, userText = "") {
  const source = plan.args && typeof plan.args === "object" ? plan.args : {};
  return aiAgentNormalizeAnalysisArgs({
    prompt: source.prompt || "",
    negative_prompt: source.negative_prompt || source.negative || "",
    width: source.width,
    height: source.height,
    steps: source.steps,
    cfg_scale: source.cfg_scale ?? source.cfg,
    cfg: source.cfg,
    batch_size: source.batch_size,
    seed: source.seed,
    checkpoint: source.checkpoint || source.model || "",
    vae: source.vae || "",
    sampler: source.sampler || "",
    sampler_name: source.sampler_name || "",
    scheduler: source.scheduler || "",
    official_workflow_id: source.official_workflow_id || "",
    generation_mode: source.generation_mode || source.mode || source.edit_mode || "",
    source_image_ref: source.source_image_ref || source.source_image_ref_json || source.image_ref || source.source_ref || null,
    mask_image_ref: source.mask_image_ref || source.mask_image_ref_json || source.mask_ref || null,
    denoise_strength: source.denoise_strength ?? source.denoise ?? source.strength,
    outpaint_left: source.outpaint_left ?? source.outpaint?.left,
    outpaint_top: source.outpaint_top ?? source.outpaint?.top,
    outpaint_right: source.outpaint_right ?? source.outpaint?.right,
    outpaint_bottom: source.outpaint_bottom ?? source.outpaint?.bottom,
    outpaint_feathering: source.outpaint_feathering ?? source.outpaint?.feathering,
  }, userText);
}

function aiAgentPlannerRerunArgs(plan = {}, userText = "") {
  const base = aiAgentCurrentComfyuiArgs();
  if (!base?.prompt) return null;
  const source = plan.args && typeof plan.args === "object" ? plan.args : {};
  const overrides = {
    ...aiAgentParseComfyuiOptionOverrides(userText),
    prompt: aiAgentStripFieldValue(source.prompt || ""),
    negative_prompt: aiAgentStripFieldValue(source.negative_prompt || source.negative || ""),
    width: source.width,
    height: source.height,
    steps: source.steps,
    cfg_scale: source.cfg_scale ?? source.cfg,
    cfg: source.cfg,
    batch_size: source.batch_size,
    seed: source.seed,
    checkpoint: aiAgentStripFieldValue(source.checkpoint || source.model || ""),
    vae: aiAgentStripFieldValue(source.vae || ""),
    sampler: aiAgentStripFieldValue(source.sampler || ""),
    sampler_name: aiAgentStripFieldValue(source.sampler_name || ""),
    scheduler: aiAgentStripFieldValue(source.scheduler || ""),
    official_workflow_id: source.official_workflow_id || "",
    generation_mode: aiAgentNormalizeComfyuiGenerationMode(source.generation_mode || source.mode || source.edit_mode || ""),
    source_image_ref: source.source_image_ref || source.source_image_ref_json || source.image_ref || source.source_ref || null,
    mask_image_ref: source.mask_image_ref || source.mask_image_ref_json || source.mask_ref || null,
    denoise_strength: source.denoise_strength ?? source.denoise ?? source.strength,
    outpaint_left: source.outpaint_left ?? source.outpaint?.left,
    outpaint_top: source.outpaint_top ?? source.outpaint?.top,
    outpaint_right: source.outpaint_right ?? source.outpaint?.right,
    outpaint_bottom: source.outpaint_bottom ?? source.outpaint?.bottom,
    outpaint_feathering: source.outpaint_feathering ?? source.outpaint?.feathering,
  };
  Object.keys(overrides).forEach((key) => {
    if (overrides[key] === "" || overrides[key] === undefined || overrides[key] === null) delete overrides[key];
  });
  const merged = { ...base, ...overrides, confirm_billing: true };
  if (overrides.negative_prompt && String(plan.merge_strategy || "").toLowerCase() === "append_negative") {
    merged.negative_prompt = aiAgentMergeCommaList(base.negative_prompt, overrides.negative_prompt);
  }
  return merged;
}

function aiAgentPlanConfirmedWrite(plan = {}) {
  return plan?.execute_write === true || String(plan?.execute_write || "").toLowerCase() === "true";
}

function aiAgentWriteToolResultSummary(toolName, json = {}, elapsedMs = 0) {
  const result = json.result || json.payload || {};
  const status = json.status || result.status || "";
  const ok = json.ok !== false;
  const lines = [
    `${ok ? "已執行" : "執行失敗"}：${toolName}`,
    `HTTP 狀態：${status || "-"}`,
    `耗時：${elapsedMs} ms`,
  ];
  const msg = aiAgentWriteToolErrorMessage(json, status);
  if (!ok && msg) lines.push(`錯誤：${msg}`);
  const nested = result.result || result.payload || result;
  const previewSource = nested && typeof nested === "object" ? nested : result;
  try {
    const preview = JSON.stringify(previewSource, null, 2);
    if (preview && preview !== "{}") lines.push(`結果摘要：\n${preview.slice(0, 1800)}`);
  } catch (err) {}
  return lines.join("\n");
}

async function aiAgentRunGenericWriteTool(plan, userText, input) {
  const toolName = String(plan?.tool || plan?.args?.tool || "").trim();
  if (!toolName) {
    AI_AGENT_STATE.messages.push({ role: "user", content: userText });
    AI_AGENT_STATE.messages.push({ role: "assistant", content: "需要指定要執行的站內白名單工具。" });
    renderAiAgentThread();
    if (input) input.value = "";
    setAiAgentMessage("缺少工具名稱", "err");
    return true;
  }
  const canRunDirectly = aiAgentCanRunWriteTool(toolName);
  let elevateOnce = false;
  if (!canRunDirectly) {
    if (aiAgentCanRequestWriteElevation(toolName)) {
      const ok = window.confirm(
        `AI Agent 目前不是 write 模式。\n\n這個請求需要本次提權執行 ${toolName}。\n是否只允許這一次寫入？`
      );
      if (!ok) {
        AI_AGENT_STATE.messages.push({ role: "user", content: userText });
        AI_AGENT_STATE.messages.push({ role: "assistant", content: `已取消本次提權，未執行 ${toolName}。` });
        renderAiAgentThread();
        if (input) input.value = "";
        setAiAgentMessage("已取消本次提權", "info");
        return true;
      }
      elevateOnce = true;
    } else {
      AI_AGENT_STATE.messages.push({ role: "user", content: userText });
      AI_AGENT_STATE.messages.push({ role: "assistant", content: `目前不可執行 ${toolName}。請確認 root 身分、operation mode 與 allowed_tools。` });
      renderAiAgentThread();
      if (input) input.value = "";
      setAiAgentMessage("工具未允許", "err");
      return true;
    }
  }
  const args = plan?.args && typeof plan.args === "object" ? { ...plan.args } : {};
  delete args.tool;
  AI_AGENT_STATE.messages.push({ role: "user", content: userText });
  AI_AGENT_STATE.messages.push({
    role: "assistant",
    content: `我理解為執行站內工具：${toolName}\n規劃耗時：${plan.elapsedMs || 0} ms`,
  });
  renderAiAgentThread();
  if (input) input.value = "";
  AI_AGENT_STATE.sendingTool = true;
  setAiAgentMessage(`執行 ${toolName} 中...`, "info");
  const started = performance.now();
  try {
    const result = await aiAgentPostWriteToolExecute({
        tool: toolName,
        arguments: args,
        confirm: "EXECUTE",
        elevate_once: elevateOnce ? "ALLOW_WRITE_ONCE" : undefined,
      },
      { toolName },
    );
    const res = result?.res || { ok: false, status: 0 };
    const json = result?.json || {};
    const elapsed = result?.elapsed || Math.round(performance.now() - started);
    const retryNote = result?.attempt > 1 ? `\n重試次數：${result.attempt - 1}` : "";
    AI_AGENT_STATE.messages.push({ role: "assistant", content: `${aiAgentWriteToolResultSummary(toolName, json, elapsed)}${retryNote}` });
    renderAiAgentThread();
    setAiAgentMessage(json.ok && res.ok ? `${toolName} 已完成` : `${toolName} 失敗`, json.ok && res.ok ? "ok" : "err");
    if (toolName === "write_comfyui_generate" && json.ok && res.ok) {
      const job = aiAgentFindComfyuiJobPayload(json) || {};
      const jobId = job.job_id || json.result?.job_id || json.payload?.job_id || json.job_id || "";
      const initialStatus = job.status || "queued";
      if (jobId) {
        job.job_id = jobId;
        job.status = initialStatus;
        aiAgentRememberComfyuiSubmit(args, job);
        AI_AGENT_STATE.messages.push({
          role: "assistant",
          content: `ComfyUI 產圖已送出，正在確認後端接收狀態。\n工具：write_comfyui_generate\nJob ID：${jobId}\n狀態：${initialStatus}${retryNote}`,
        });
        renderAiAgentThread();
        setAiAgentMessage("ComfyUI 產圖已送出，正在確認狀態", "info");
        aiAgentWatchComfyuiJob(jobId);
      }
    }
  } catch (err) {
    AI_AGENT_STATE.messages.push({ role: "assistant", content: `${toolName} 執行失敗：${err?.message || err}` });
    renderAiAgentThread();
    setAiAgentMessage(`${toolName} 執行失敗：${err?.message || err}`, "err");
  } finally {
    AI_AGENT_STATE.sendingTool = false;
  }
  return true;
}

async function aiAgentRunReadonlyQuery(intent, userText, input) {
  AI_AGENT_STATE.sending = true;
  const sendBtn = $("ai-agent-send-btn");
  if (sendBtn) sendBtn.disabled = true;
  AI_AGENT_STATE.messages.push({ role: "user", content: userText });
  renderAiAgentThread();
  if (input) input.value = "";
  const normalizedScope = aiAgentNormalizeReadonlyScope(intent.scope || "all");
  const requestScope = normalizedScope;
  setAiAgentMessage(`${intent.label || "唯讀查詢"}讀取中...`, "info");
  try {
    const res = await apiFetch(`${API}/ai-agent/readonly?scope=${encodeURIComponent(requestScope)}&limit=20`, {
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `唯讀查詢失敗（HTTP ${res.status}）`);
    json.scope = normalizedScope;
    await aiAgentAttachComfyuiHealth(json, normalizedScope);
    renderAiAgentReadOnly(json);
    aiAgentResumeComfyuiWatchJobs(json);
    AI_AGENT_STATE.messages.push({ role: "assistant", content: aiAgentReadonlySummary(json, intent) });
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
}

async function aiAgentExecuteToolPlan(plan, userText, input, options = {}) {
  const action = String(plan?.action || "").trim().toLowerCase();
  const confidence = Number(plan?.confidence ?? 0.75);
  if (!action || action === "chat" || confidence < 0.45) return false;
  if (action === "clarify") {
    AI_AGENT_STATE.messages.push({ role: "user", content: userText });
    AI_AGENT_STATE.messages.push({ role: "assistant", content: String(plan.question || "請補充你希望我怎麼處理。") });
    renderAiAgentThread();
    if (input) input.value = "";
    setAiAgentMessage("需要補充資訊", "info");
    return true;
  }
  if (action === "readonly" || action === "comfyui_status") {
    await aiAgentRunReadonlyQuery({
      scope: plan.readonly_scope || (action === "comfyui_status" ? "comfyui" : "all"),
      label: action === "comfyui_status" ? "ComfyUI 產圖進度" : "唯讀查詢",
    }, userText, input);
    return true;
  }
  if (action === "write_tool") {
    if (!aiAgentPlanConfirmedWrite(plan)) {
      const toolName = String(plan?.tool || "").trim();
      AI_AGENT_STATE.messages.push({ role: "user", content: userText });
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: `我識別到可能需要站內工具${toolName ? `：${toolName}` : ""}，但規劃結果未確認這是可執行寫入，所以沒有執行。請明確指定要執行後再送出。`,
      });
      renderAiAgentThread();
      if (input) input.value = "";
      setAiAgentMessage("未確認寫入，已停止執行", "info");
      return true;
    }
    return aiAgentRunGenericWriteTool(plan, userText, input);
  }
  if (action === "comfyui_generate" || action === "comfyui_rerun") {
    if (options.hasImage && action === "comfyui_generate" && !aiAgentPlanConfirmedWrite(plan)) {
      AI_AGENT_STATE.messages.push({ role: "user", content: `${userText}\n[已附加圖片]` });
      renderAiAgentThread();
      if (input) input.value = "";
      setAiAgentMessage("圖片分析中...", "info");
      try {
        const analyzed = await aiAgentAnalyzeImageForComfyui(userText);
        aiAgentFillComfyuiToolForm(analyzed.args);
        AI_AGENT_STATE.messages.push({
          role: "assistant",
          content: `我先只分析這張圖，沒有送出生圖任務。\n提示詞：${analyzed.args.prompt}\n負面詞：${analyzed.args.negative_prompt || "-"}\n圖片分析耗時：${analyzed.elapsedMs} ms\n\n如果要我直接用這組參數生圖，請明確說「用這張生圖」或「用剛剛的 prompt 生圖」。`,
        });
        renderAiAgentThread();
        setAiAgentMessage("圖片分析完成，未執行寫入", "info");
      } catch (err) {
        AI_AGENT_STATE.messages.push({ role: "assistant", content: `圖片分析失敗，未送出生圖：${err?.message || err}` });
        renderAiAgentThread();
        setAiAgentMessage(`圖片分析失敗：${err?.message || err}`, "err");
      }
      return true;
    }
    let args = null;
    try {
      if (action === "comfyui_generate" && options.hasImage) {
        args = null;
      } else {
        args = action === "comfyui_generate"
          ? aiAgentPlannerArgs(plan, userText)
          : aiAgentPlannerRerunArgs(plan, userText);
      }
    } catch (err) {
      args = null;
    }
    if (!options.hasImage && !args?.prompt) {
      AI_AGENT_STATE.messages.push({ role: "user", content: userText });
      AI_AGENT_STATE.messages.push({ role: "assistant", content: plan.question || "我需要知道要畫什麼，或要基於哪一筆生圖重跑。" });
      renderAiAgentThread();
      if (input) input.value = "";
      setAiAgentMessage("需要補充生圖資訊", "info");
      return true;
    }
    AI_AGENT_STATE.messages.push({ role: "user", content: options.hasImage ? `${userText}\n[已附加圖片]` : userText });
    renderAiAgentThread();
    if (input) input.value = "";
    if (options.hasImage && action === "comfyui_generate") {
      setAiAgentMessage("圖片分析與生圖參數生成中...", "info");
      try {
        const analyzed = await aiAgentAnalyzeImageForComfyui(userText);
        args = analyzed.args;
        aiAgentFillComfyuiToolForm(args);
        AI_AGENT_STATE.messages.push({
          role: "assistant",
          content: `圖片分析完成，將依分析結果執行生圖。\n提示詞：${args.prompt}\n負面詞：${args.negative_prompt || "-"}\n規劃耗時：${plan.elapsedMs || 0} ms；圖片分析耗時：${analyzed.elapsedMs} ms`,
        });
        renderAiAgentThread();
      } catch (err) {
        AI_AGENT_STATE.messages.push({ role: "assistant", content: `圖片分析失敗，未送出生圖：${err?.message || err}` });
        renderAiAgentThread();
        setAiAgentMessage(`圖片分析失敗：${err?.message || err}`, "err");
        return true;
      }
    } else {
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: `${action === "comfyui_rerun" ? "我理解為修改上一筆參數後重跑。" : "我理解為直接執行生圖。"}\n提示詞：${args.prompt}\n負面詞：${args.negative_prompt || "-"}\n規劃耗時：${plan.elapsedMs || 0} ms`,
      });
      renderAiAgentThread();
      aiAgentFillComfyuiToolForm(args);
    }
    await runAiAgentComfyuiGenerate(args);
    return true;
  }
  if (action === "community_post_draft") return false;
  return false;
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
  const generationMode = aiAgentNormalizeComfyuiGenerationMode(
    source?.generation_mode || source?.mode || source?.edit_mode || source?.image_edit_mode || ""
  );
  const sourceImageRef = source?.source_image_ref || source?.source_image_ref_json || source?.image_ref || source?.source_ref || null;
  let prompt = aiAgentStripFieldValue(source?.prompt || source?.positive_prompt || source?.comfyui_prompt || "");
  if (!prompt && sourceImageRef && ["img2img", "inpaint", "outpaint", "upscale"].includes(generationMode)) {
    prompt = aiAgentStripFieldValue(userText || "");
  }
  if (prompt && aiAgentLooksLikeStaleImageEditPrompt(prompt, generationMode, sourceImageRef)) {
    prompt = aiAgentStripFieldValue(userText || prompt);
  }
  if (!prompt) throw new Error("圖片分析沒有產生可用提示詞");
  const args = {
    prompt,
    negative_prompt: aiAgentStripFieldValue(source?.negative_prompt || source?.negative || ""),
    width: source?.width,
    height: source?.height,
    steps: source?.steps,
    cfg_scale: source?.cfg_scale ?? source?.cfg,
    cfg: source?.cfg,
    batch_size: source?.batch_size,
    seed: source?.seed,
    checkpoint: aiAgentStripFieldValue(source?.checkpoint || source?.model || ""),
    vae: aiAgentStripFieldValue(source?.vae || ""),
    sampler: aiAgentStripFieldValue(source?.sampler || ""),
    sampler_name: aiAgentStripFieldValue(source?.sampler_name || ""),
    scheduler: aiAgentStripFieldValue(source?.scheduler || ""),
    official_workflow_id: source?.official_workflow_id || "",
    generation_mode: generationMode,
    source_image_ref: sourceImageRef,
    mask_image_ref: source?.mask_image_ref || source?.mask_image_ref_json || source?.mask_ref || null,
    denoise_strength: source?.denoise_strength ?? source?.denoise ?? source?.strength,
    outpaint_left: source?.outpaint_left ?? source?.outpaint?.left,
    outpaint_top: source?.outpaint_top ?? source?.outpaint?.top,
    outpaint_right: source?.outpaint_right ?? source?.outpaint?.right,
    outpaint_bottom: source?.outpaint_bottom ?? source?.outpaint?.bottom,
    outpaint_feathering: source?.outpaint_feathering ?? source?.outpaint?.feathering,
    confirm_billing: true,
    ...aiAgentParseComfyuiOptionOverrides(userText),
  };
  Object.keys(args).forEach((key) => {
    if (args[key] === "" || args[key] === undefined || args[key] === null) delete args[key];
  });
  return aiAgentCleanComfyuiArgs(args);
}

async function aiAgentAnalyzeImageForComfyui(userText) {
  await aiAgentRefreshModelState();
  const selectedModel = aiAgentVisionModel();
  const selectableModels = aiAgentSelectableModels();
  if (!selectedModel) {
    throw new Error("目前沒有可用的圖片理解模型。請在 AI Agent 模型允許清單加入 /models 回傳且支援圖片的模型後再試。");
  }
  if (selectableModels.length && !selectableModels.includes(selectedModel)) {
    throw new Error("請從模型選單選擇可用模型後再做圖片分析。");
  }
  const analysisPrompt = [
    "請先分析使用者附上的圖片，依使用者語意產生可用於 ComfyUI 的生圖或圖生圖參數。",
    "請只輸出 JSON，不要 Markdown，不要表格，不要操作教學。",
    "JSON 欄位：prompt, negative_prompt, width, height, steps, cfg_scale, checkpoint, vae, sampler, sampler_name, scheduler, official_workflow_id, generation_mode, denoise_strength。",
    "若使用者要求改變附圖風格或以圖生圖，generation_mode=img2img；局部重繪需 mask 才能 inpaint，沒有 mask 時不要假裝已具備 mask；向外延展 generation_mode=outpaint。",
    "如果使用者文字指定尺寸、模型、CFG、VAE 或 SDXL T2I，請保留那些指定。",
    "checkpoint 只能填使用者明確提供的實際 checkpoint 名稱；如果只提到 SDXL、SDXL T2I 或泛稱，不要填 sdxl_base_1.0.ckpt，請省略 checkpoint。",
    `使用者需求：${userText || "參考圖片產生相似風格圖片"}`,
  ].join("\n");
  const started = performance.now();
  const res = await aiAgentChatFetch({
    session_id: aiAgentEnsureSessionId(),
    model: selectedModel,
    mode: "image",
    messages: [{ role: "user", content: analysisPrompt }],
    image_data_url: AI_AGENT_STATE.imageDataUrl,
  }, {
    mode: "image",
  });
  const json = await res.json().catch(() => ({}));
  const content = json?.message?.content || json?.msg || "";
  if (!res.ok || !json.ok || isMockAiAgentReply(content)) {
    if (aiAgentImageModelUnavailable(json, res.status)) {
      aiAgentMarkModelUnavailable(selectedModel, aiAgentImageAnalysisError(json, res.status));
    }
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
  const selectedModel = aiAgentSelectedTextModel();
  const selectableModels = aiAgentSelectableModels();
  if (selectableModels.length && (!selectedModel || !selectableModels.includes(selectedModel))) {
    throw new Error("請從模型選單選擇可用模型後再做生圖解析。");
  }
  const analysisPrompt = [
    "請把使用者的自然語言需求轉成 ComfyUI write-tool 參數，可支援 text-to-image、img2img、inpaint、outpaint。",
    "請只輸出 JSON，不要 Markdown，不要表格，不要操作教學。",
    "JSON 欄位：prompt, negative_prompt, width, height, steps, cfg_scale, cfg, batch_size, seed, checkpoint, vae, sampler, sampler_name, scheduler, official_workflow_id, generation_mode, source_image_ref, mask_image_ref, denoise_strength, outpaint_left, outpaint_top, outpaint_right, outpaint_bottom, outpaint_feathering。",
    "若使用者要求修改、重繪、風格化、以圖生圖或外延站內圖片，請保留 source_image_ref/mask_image_ref/outpaint/denoise 欄位；風格化 generation_mode=img2img。",
    "如果使用者提到 SDXL T2I、SDXL txt2img 或文字生圖，official_workflow_id 設為 origin_sdxl_txt2img。",
    "如果使用者指定模型、Checkpoint、VAE、尺寸、CFG、步數或張數，必須保留。",
    "checkpoint 只能填使用者明確提供的實際 checkpoint 名稱；如果只提到 SDXL、SDXL T2I 或泛稱，不要填 sdxl_base_1.0.ckpt，請省略 checkpoint。",
    "prompt 欄位要是可直接送 ComfyUI 的正向提示詞，不要包含解釋文字。",
    `使用者需求：${userText}`,
  ].join("\n");
  const started = performance.now();
  const res = await aiAgentChatFetch({
    session_id: aiAgentEnsureSessionId(),
    model: selectedModel,
    mode: "text",
    messages: [{ role: "user", content: analysisPrompt }],
    image_data_url: "",
  }, {
    mode: "text",
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

function aiAgentConfiguredWriteTools(configured = AI_AGENT_STATE.settings?.allowed_tools || "") {
  const raw = String(configured || "").trim();
  if (raw === "__none__") return new Set();
  const catalogNames = (AI_AGENT_STATE.writeToolCatalog || []).map((tool) => tool.name).filter(Boolean);
  if (!raw) return new Set(catalogNames);
  return new Set(raw.split(",").map((item) => item.trim()).filter(Boolean));
}

function renderAiAgentToolSelector() {
  const panel = $("ai-agent-tool-selector");
  const state = $("ai-agent-tool-selector-state");
  const list = $("ai-agent-tool-selector-list");
  const saveBtn = $("ai-agent-tool-selector-save-btn");
  const isRoot = AI_AGENT_STATE.actor?.role === "super_admin";
  if (panel) {
    panel.hidden = !isRoot;
    panel.setAttribute("aria-hidden", isRoot ? "false" : "true");
  }
  if (!isRoot || !list) return;
  const catalog = Array.isArray(AI_AGENT_STATE.writeToolCatalog) ? AI_AGENT_STATE.writeToolCatalog : [];
  const enabled = AI_AGENT_STATE.writeToolEnabled instanceof Set ? AI_AGENT_STATE.writeToolEnabled : new Set();
  if (saveBtn) saveBtn.disabled = AI_AGENT_STATE.writeToolSaving || AI_AGENT_STATE.writeToolLoading || !catalog.length;
  const blocked = !!AI_AGENT_STATE.writeToolGuard?.blocked;
  if (state) {
    if (AI_AGENT_STATE.writeToolLoading) {
      state.textContent = "工具 catalog 載入中...";
    } else if (!catalog.length) {
      state.textContent = "尚未載入工具 catalog";
    } else {
      const suffix = blocked ? "，audit lockdown 中，儲存設定可用但執行仍會被擋" : "";
      state.textContent = `已啟用 ${enabled.size}/${catalog.length} 個 write tools${suffix}`;
    }
  }
  if (!catalog.length) {
    list.innerHTML = '<div class="drive-empty">尚未載入工具 catalog</div>';
    return;
  }
  list.innerHTML = catalog.map((tool) => {
    const name = String(tool.name || "");
    const checked = enabled.has(name) ? "checked" : "";
    return `
      <label class="drive-file-row ai-agent-tool-selector-row">
        <input type="checkbox" data-ai-agent-tool-toggle="${sanitize(name)}" ${checked} />
        <strong>${sanitize(tool.label || name)}</strong>
        <span>${sanitize(name)}</span>
        <span>${sanitize(tool.description || "")}</span>
      </label>
    `;
  }).join("");
  list.querySelectorAll("[data-ai-agent-tool-toggle]").forEach((input) => {
    input.addEventListener("change", () => {
      const name = input.getAttribute("data-ai-agent-tool-toggle") || "";
      if (!name) return;
      if (input.checked) AI_AGENT_STATE.writeToolEnabled.add(name);
      else AI_AGENT_STATE.writeToolEnabled.delete(name);
      renderAiAgentToolSelector();
    });
  });
}

async function loadAiAgentWriteToolCatalog(options = {}) {
  if (AI_AGENT_STATE.writeToolLoading && !options.force) return;
  if (AI_AGENT_STATE.actor?.role !== "super_admin") {
    renderAiAgentToolSelector();
    return;
  }
  AI_AGENT_STATE.writeToolLoading = true;
  renderAiAgentToolSelector();
  try {
    const res = await apiFetch(`${API}/ai-agent/write-tools?include_all=1`, {
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      setAiAgentMessage(json.msg || "write tools catalog 載入失敗", "err");
      return;
    }
    AI_AGENT_STATE.writeToolCatalog = Array.isArray(json.catalog_tools) ? json.catalog_tools : [];
    AI_AGENT_STATE.writeToolGuard = json.guard || {};
    AI_AGENT_STATE.writeToolEnabled = aiAgentConfiguredWriteTools(json.allowed_tools ?? AI_AGENT_STATE.settings?.allowed_tools ?? "");
  } catch (err) {
    setAiAgentMessage(`write tools catalog 載入失敗：${err}`, "err");
  } finally {
    AI_AGENT_STATE.writeToolLoading = false;
    renderAiAgentToolSelector();
  }
}

function setAiAgentToolSelection(mode) {
  const names = (AI_AGENT_STATE.writeToolCatalog || []).map((tool) => tool.name).filter(Boolean);
  if (mode === "all") {
    AI_AGENT_STATE.writeToolEnabled = new Set(names);
  } else if (mode === "none") {
    AI_AGENT_STATE.writeToolEnabled = new Set();
  } else if (mode === "comfyui") {
    AI_AGENT_STATE.writeToolEnabled = new Set(names.includes("write_comfyui_generate") ? ["write_comfyui_generate"] : []);
  }
  renderAiAgentToolSelector();
}

async function saveAiAgentToolSelection() {
  if (AI_AGENT_STATE.writeToolSaving || AI_AGENT_STATE.actor?.role !== "super_admin") return;
  const names = (AI_AGENT_STATE.writeToolCatalog || []).map((tool) => tool.name).filter(Boolean);
  const enabled = names.filter((name) => AI_AGENT_STATE.writeToolEnabled.has(name));
  const allowedTools = enabled.length ? enabled.join(",") : "__none__";
  AI_AGENT_STATE.writeToolSaving = true;
  renderAiAgentToolSelector();
  try {
    const res = await apiFetch(`${API}/admin/settings`, {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ai_agent_allowed_tools: allowedTools }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      setAiAgentMessage(json.msg || "write tools 清單儲存失敗", "err");
      return;
    }
    setAiAgentMessage("write tools 清單已儲存", "ok");
    AI_AGENT_STATE.loaded = false;
    await loadAiAgentStatus({ force: true });
    await loadAiAgentWriteToolCatalog({ force: true });
  } catch (err) {
    setAiAgentMessage(`write tools 清單儲存失敗：${err}`, "err");
  } finally {
    AI_AGENT_STATE.writeToolSaving = false;
    renderAiAgentToolSelector();
  }
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
    return aiAgentCleanComfyuiArgs({
      ...overrides,
      prompt: String(overrides.prompt || "").trim(),
      negative_prompt: String(overrides.negative_prompt || "").trim(),
      width: aiAgentClampNumber(overrides.width, 1024, { min: 256, max: 2048, integer: true }),
      height: aiAgentClampNumber(overrides.height, 1024, { min: 256, max: 2048, integer: true }),
      steps: aiAgentClampNumber(overrides.steps, 20, { min: 1, max: 80, integer: true }),
      cfg_scale: aiAgentClampNumber(overrides.cfg_scale, 7, { min: 1, max: 20 }),
      batch_size: aiAgentClampNumber(overrides.batch_size, 1, { min: 1, max: 8, integer: true }),
      confirm_billing: true,
    });
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
  return aiAgentCleanComfyuiArgs(args);
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

function aiAgentSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms) || 0)));
}

function aiAgentServerBusyDelayMs(json = {}, status = 0, attempt = 1) {
  const error = String(json.error || json.msg || json.message || "").toLowerCase();
  if (Number(status) !== 503 && !error.includes("server_busy")) return 0;
  const retryAfter = Number(json.retry_after_seconds || json.retry_after || 0);
  if (Number.isFinite(retryAfter) && retryAfter > 0) return Math.min(10000, Math.max(500, retryAfter * 1000));
  return Math.min(10000, 1000 * Math.max(1, attempt));
}

async function aiAgentPostWriteToolExecute(payload, { toolName = "", maxAttempts = 4 } = {}) {
  const started = performance.now();
  let last = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const res = await apiFetch(API + "/ai-agent/write-tools/execute", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const json = await res.json().catch(() => ({}));
    last = { res, json, attempt, elapsed: Math.round(performance.now() - started) };
    const delayMs = aiAgentServerBusyDelayMs(json, res.status, attempt);
    if (delayMs > 0 && attempt < maxAttempts) {
      const label = toolName || payload?.tool || "write-tool";
      AI_AGENT_STATE.messages.push({
        role: "assistant",
        content: `${label} 暫時受到伺服器保護限制，${Math.round(delayMs / 1000)} 秒後自動重試（${attempt}/${maxAttempts - 1}）。`,
      });
      renderAiAgentThread();
      setAiAgentMessage(`${label} 等待 backpressure 重試...`, "info");
      await aiAgentSleep(delayMs);
      continue;
    }
    return last;
  }
  return last;
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
  let attempt = null;
  try {
    args = aiAgentComfyuiToolArguments(overrides);
    if (!args.prompt) throw new Error("請先輸入提示詞");
    attempt = aiAgentRememberComfyuiAttempt(args, { status: "sending" });
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
    const result = await aiAgentPostWriteToolExecute({
        tool: "write_comfyui_generate",
        arguments: args,
        confirm: "EXECUTE",
        elevate_once: elevateOnce ? "ALLOW_WRITE_ONCE" : "",
      },
      { toolName: "write_comfyui_generate" },
    );
    const res = result?.res || { ok: false, status: 0 };
    const json = result?.json || {};
    if (!res.ok || !json.ok) {
      const msg = aiAgentWriteToolErrorMessage(json, res.status);
      if (attempt) aiAgentRememberComfyuiAttempt(args, { attempt_id: attempt.attempt_id, status: "error", error: msg });
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
    if (jobId && jobId !== "-") {
      job.job_id = jobId;
      job.status = initialStatus;
      if (attempt) aiAgentRememberComfyuiAttempt(args, { attempt_id: attempt.attempt_id, status: initialStatus, job_id: jobId, error: "" });
      aiAgentRememberComfyuiSubmit(args, job);
    }
    const retryNote = result?.attempt > 1 ? `\n重試次數：${result.attempt - 1}` : "";
    AI_AGENT_STATE.messages.push({
      role: "assistant",
      content: `ComfyUI 產圖已送出，正在確認後端接收狀態。\n工具：write_comfyui_generate\nJob ID：${jobId}\n狀態：${initialStatus}${retryNote}`,
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
    if (attempt) aiAgentRememberComfyuiAttempt(args, { attempt_id: attempt.attempt_id, status: "error", error: String(err?.message || err) });
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
  const jobId = String(job.job_id || "").trim();
  if (jobId) AI_AGENT_STATE.comfyuiAnnouncedJobs[jobId] = "completed";
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
  const jobId = String(job.job_id || "").trim();
  if (jobId) AI_AGENT_STATE.comfyuiAnnouncedJobs[jobId] = "error";
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
    if (AI_AGENT_STATE.comfyuiSubmittedJobs[jobId]) {
      AI_AGENT_STATE.comfyuiSubmittedJobs[jobId].status = job.status || AI_AGENT_STATE.comfyuiSubmittedJobs[jobId].status;
      AI_AGENT_STATE.comfyuiSubmittedJobs[jobId].updatedAt = Date.now();
    }
    aiAgentUpdateComfyuiAttemptFromJob(job);
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
    const detail = err?.message || String(err || "未知錯誤");
    aiAgentMarkComfyuiAttemptError(jobId, detail);
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
    aiAgentUpdateComfyuiAttemptFromJob(lastJob);
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
  aiAgentScrollThreadToBottom();
}

function aiAgentScrollThreadToBottom() {
  const host = $("ai-agent-thread");
  aiAgentScrollElementToBottom(host);
}

function aiAgentScrollElementToBottom(host) {
  if (!host) return;
  const scroll = () => {
    host.scrollTop = host.scrollHeight;
  };
  scroll();
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(scroll);
  } else {
    setTimeout(scroll, 0);
  }
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
  renderAiAgentConversationHistory();
  const health = json?.health || {};
  const status = $("ai-agent-status");
  if (status) {
    const providerLabel = settings.provider === "openai_compatible" ? "OpenAI-compatible" : "Local AI Backend";
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
  AI_AGENT_STATE.writeToolEnabled = aiAgentConfiguredWriteTools(settings.allowed_tools || "");
  renderAiAgentToolSelector();
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
    if (!jobId) return;
    if (["completed", "error", "failed", "cancelled"].includes(status)) {
      const isKnown = !!AI_AGENT_STATE.comfyuiSubmittedJobs[jobId] || AI_AGENT_STATE.lastComfyuiJob?.job_id === jobId;
      if (!isKnown || AI_AGENT_STATE.comfyuiAnnouncedJobs[jobId]) return;
      aiAgentFetchComfyuiJob(jobId).then((fullJob) => {
        const fullStatus = String(fullJob?.status || status).toLowerCase();
        if (["error", "failed", "cancelled"].includes(fullStatus)) {
          AI_AGENT_STATE.messages.push({ role: "assistant", content: aiAgentComfyuiFailureSummary(fullJob || job) });
        } else if (fullStatus === "completed") {
          const message = aiAgentComfyuiCompletionMessage(fullJob || job);
          AI_AGENT_STATE.messages.push(message);
          aiAgentHydrateComfyuiMessageImages(message).catch(() => undefined);
        }
        renderAiAgentThread();
      }).catch(() => undefined);
      return;
    }
    if (!["queued", "running", "pending"].includes(status)) return;
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
  return modelIds.filter((id) => {
    if (AI_AGENT_STATE.unavailableModelIds?.has(id)) return false;
    return !allowedModels.length || allowedModels.includes(id);
  });
}

function aiAgentSelectedTextModel() {
  const select = $("ai-agent-model");
  const options = aiAgentSelectableModels();
  const selected = (select?.value || "").trim();
  const configured = AI_AGENT_STATE.settings?.model || "";
  let chosen = selected;
  if (options.length) {
    if (!chosen || !options.includes(chosen)) {
      chosen = options.includes(configured) ? configured : options[0];
    }
    if (select && chosen && select.value !== chosen) select.value = chosen;
    return chosen;
  }
  return chosen || configured || "";
}

function aiAgentVisionModel() {
  const options = aiAgentSelectableModels();
  const selected = ($("ai-agent-model")?.value || "").trim();
  const vision = options.find((id) => /(?:^|[-_:])vl(?:[-_:]|$)|vision|multimodal/i.test(id));
  if (vision) {
    const select = $("ai-agent-model");
    if (select && select.value !== vision) select.value = vision;
    return vision;
  }
  return "";
}

function aiAgentImageAnalysisError(json = {}, status = 0) {
  const raw = String(json?.msg || json?.error || json?.message?.content || "").trim();
  const lowered = raw.toLowerCase();
  const effectiveStatus = Number(json?.status || status || 0);
  if (effectiveStatus === 410 || lowered.includes("retired") || lowered.includes("not found") || lowered.includes("unavailable") || lowered.includes("已下架")) {
    return raw
      ? `圖片理解模型不可用或已下架：${raw}`
      : "圖片理解模型不可用或已下架。請改用目前 /models 可呼叫的 cloud vision 模型。";
  }
  if (lowered.includes("does not support image input") || lowered.includes("不支援圖片")) {
    return "目前選用模型不支援圖片分析，請改用 /models 回傳且支援圖片的模型後再試。";
  }
  if (status >= 500 || lowered.includes("internal server error")) {
    return `圖片分析後端目前不可用（HTTP ${status || 500}）。${raw ? `後端訊息：${raw}` : "請稍後重試或檢查 vision 模型服務。"}`;
  }
  return raw || `圖片分析失敗（HTTP ${status || "-"}）`;
}

function aiAgentImageModelUnavailable(json = {}, status = 0) {
  const raw = String(json?.msg || json?.error || json?.payload?.error || "").toLowerCase();
  const effectiveStatus = Number(json?.status || status || 0);
  return effectiveStatus === 410 || raw.includes("http 410") || raw.includes("retired") || raw.includes("not found") || raw.includes("unavailable") || raw.includes("已下架");
}

function aiAgentMarkModelUnavailable(modelId, reason = "") {
  const id = String(modelId || "").trim();
  if (!id) return;
  AI_AGENT_STATE.unavailableModelIds.add(id);
  AI_AGENT_STATE.unavailableModelReasons[id] = String(reason || "backend rejected model").slice(0, 180);
  syncAiAgentModelSelect();
  renderAiAgentModels({ data: (AI_AGENT_STATE.modelIds || []).map((model) => ({ id: model })) });
}

function aiAgentImageTransportError(err) {
  const raw = String(err?.message || err || "Load failed").trim();
  return `圖片分析請求傳輸失敗：${raw}。請重試或改用較小圖片；若仍失敗，代表目前 vision 後端不可用。`;
}

function aiAgentLooksLikeComfyuiRecall(prompt) {
  const text = String(prompt || "");
  if (!/(回顧|回看|整理|列出|總結|比較|差在哪|前幾個版本|前幾版|哪些版本|job id|失敗原因|結果如何|結果怎樣)/i.test(text)) return false;
  return /(產圖|生圖|comfyui|prompt|提示詞|負面詞|job id|版本|第一版|第二版|v\d)/i.test(text);
}

function aiAgentComfyuiRecallSummary() {
  const attempts = (AI_AGENT_STATE.comfyuiAttemptHistory || []).slice(-8);
  if (!attempts.length) {
    return "目前這個對話頁面沒有可回顧的 ComfyUI 生圖嘗試紀錄。若你剛重新整理頁面，我可以依目前對話文字協助整理，但無法保證有完整 Job ID。";
  }
  const lines = ["剛剛幾版 ComfyUI 生圖紀錄："];
  attempts.forEach((item) => {
    const args = item.args || {};
    const size = args.width && args.height ? `${args.width}x${args.height}` : "-";
    const steps = args.steps !== undefined ? args.steps : "-";
    const status = aiAgentNormalizeComfyuiAttemptStatus(item.status, item.error);
    lines.push(
      `V${item.version}：${status || "-"}`
      + `\nPrompt：${args.prompt || "-"}`
      + `\nNegative：${args.negative_prompt || "-"}`
      + `\n尺寸/步數：${size} / ${steps}`
      + `\nJob ID：${item.job_id || "-"}`
      + (status === "error" ? `\n失敗原因：${item.error || "未知錯誤"}` : "")
    );
  });
  lines.push("要我沿用其中一版修改重跑時，可以直接說「把 V2 改成...再生圖」。");
  return lines.join("\n");
}

function aiAgentNormalizeReadonlyScope(scope) {
  const raw = String(scope || "").trim().toLowerCase();
  if (["resources", "comfyui", "remote_download", "attack_diag", "server_mode", "all"].includes(raw)) return raw;
  return "all";
}

async function aiAgentFetchOptionalJson(path) {
  const res = await apiFetch(path, { credentials: "same-origin" });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
  return json;
}

function aiAgentComfyuiModelsSummary(models = {}) {
  const diffusionModels = Array.isArray(models.diffusion_models) ? models.diffusion_models : [];
  const checkpoints = Array.isArray(models.checkpoints) ? models.checkpoints : [];
  const generationModes = Array.isArray(models.generation_modes) ? models.generation_modes : [];
  return {
    ok: !!models.ok || diffusionModels.length > 0 || checkpoints.length > 0 || generationModes.length > 0,
    connection_mode: models.connection_mode || "",
    comfyui_url: models.comfyui_url || "",
    diffusion_model_count: diffusionModels.length,
    checkpoint_count: checkpoints.length,
    generation_mode_count: generationModes.filter((mode) => mode?.available !== false).length,
    available_generation_modes: generationModes
      .filter((mode) => mode?.available !== false)
      .slice(0, 6)
      .map((mode) => mode.label || mode.key || "")
      .filter(Boolean),
    default_width: models.default_width,
    default_height: models.default_height,
  };
}

async function aiAgentAttachComfyuiHealth(payload = {}, scope = "") {
  if (!["all", "comfyui"].includes(scope)) return payload;
  const [statusResult, resourcesResult, modelsResult] = await Promise.allSettled([
    aiAgentFetchOptionalJson(`${API}/comfyui/status`),
    aiAgentFetchOptionalJson(`${API}/comfyui/resources`),
    aiAgentFetchOptionalJson(`${API}/comfyui/models`),
  ]);
  if (statusResult.status === "fulfilled") {
    payload.comfyui_status = statusResult.value;
  } else {
    payload.comfyui_status = { ok: false, available: false, msg: statusResult.reason?.message || "ComfyUI 狀態讀取失敗" };
  }
  if (resourcesResult.status === "fulfilled") {
    payload.comfyui_resources = resourcesResult.value.resource_usage || {};
  }
  if (modelsResult.status === "fulfilled") {
    payload.comfyui_models_summary = aiAgentComfyuiModelsSummary(modelsResult.value);
  } else {
    payload.comfyui_models_summary = { ok: false, error: modelsResult.reason?.message || "ComfyUI 模型清單讀取失敗" };
  }
  return payload;
}

function aiAgentReadonlySummary(payload = {}, intent = {}) {
  const lines = [`${intent.label || "唯讀查詢"}：已直接讀取站內唯讀資料。`];
  const serverMode = payload.server_mode || payload.server_mode_status || null;
  if (serverMode) {
    const mode = serverMode.mode || {};
    const requirements = serverMode.production_requirements || {};
    const missingCount = Array.isArray(requirements.missing) ? requirements.missing.length : 0;
    const failedCount = Array.isArray(requirements.failed) ? requirements.failed.length : 0;
    const incident = serverMode.incident || null;
    lines.push(
      serverMode.ok
        ? `伺服器模式：${mode.current_mode || "-"}；前次：${mode.previous_mode || "-"}；原因：${mode.reason || "-"}；備註：${mode.notes || "-"}`
        : `伺服器模式讀取失敗：${serverMode.msg || "未知錯誤"}`
    );
    if (serverMode.ok && requirements.ok !== undefined) {
      lines.push(`上線需求：${requirements.ok ? "通過" : "未通過"}；缺少 ${missingCount}；失敗 ${failedCount}`);
    }
    if (incident) lines.push(`事件狀態：${incident.status || incident.state || "active"} ${incident.reason || ""}`.trim());
  }
  const comfyuiStatus = payload.comfyui_status || null;
  if (comfyuiStatus) {
    const available = comfyuiStatus.ok !== false && comfyuiStatus.available === true;
    const mode = comfyuiStatus.connection_mode || payload.comfyui_models_summary?.connection_mode || "-";
    const url = comfyuiStatus.comfyui_url || payload.comfyui_models_summary?.comfyui_url || "-";
    const modelSummary = payload.comfyui_models_summary || {};
    const resource = payload.comfyui_resources || {};
    const gpu = resource.gpu || {};
    const vram = resource.vram || {};
    const modelBits = [];
    if (modelSummary.diffusion_model_count !== undefined) modelBits.push(`模型 ${modelSummary.diffusion_model_count}`);
    if (modelSummary.generation_mode_count !== undefined) modelBits.push(`模式 ${modelSummary.generation_mode_count}`);
    const resourceBits = [];
    if (gpu.available) resourceBits.push(`GPU ${gpu.percent !== undefined && gpu.percent !== null ? `${Number(gpu.percent).toFixed(1)}%` : "可用"}`);
    if (vram.available) resourceBits.push(`VRAM ${vram.percent !== undefined && vram.percent !== null ? `${Number(vram.percent).toFixed(1)}%` : "可用"}`);
    lines.push(
      `目前${available ? "可以" : "暫時不能"}生圖：ComfyUI ${available ? "已連線" : "未就緒"}，模式 ${mode}，端點 ${url}`
      + (modelBits.length ? `，${modelBits.join(" / ")}` : "")
      + (resourceBits.length ? `，${resourceBits.join(" / ")}` : "")
      + (!available && comfyuiStatus.msg ? `。原因：${comfyuiStatus.msg}` : "")
    );
    const warnings = Array.isArray(comfyuiStatus.storage_warnings) ? comfyuiStatus.storage_warnings : [];
    if (warnings.length) lines.push(`提醒：${warnings[0].message || warnings[0].code || "ComfyUI 儲存路徑有警告"}`);
  }
  const resources = payload.resources || {};
  if (resources.cpu || resources.ram || resources.disk) {
    const cpu = resources.cpu?.percent;
    const ram = resources.ram?.percent;
    const disk = resources.disk?.percent;
    lines.push(`資源：CPU ${cpu !== undefined && cpu !== null ? `${Number(cpu).toFixed(1)}%` : "-"} / RAM ${ram !== undefined && ram !== null ? `${Number(ram).toFixed(1)}%` : "-"} / Disk ${disk !== undefined && disk !== null ? `${Number(disk).toFixed(1)}%` : "-"}`);
  }
  if (Array.isArray(payload.comfyui_jobs)) {
    const jobs = payload.comfyui_jobs.slice(0, 5).map((job) => {
      const jobId = String(job.job_id || job.id || "").trim();
      const shortId = jobId ? jobId.slice(0, 8) : "-";
      const percent = Number(job.progress_percent || 0);
      const detail = job.error || job.progress?.detail || job.progress?.phase || "";
      return `#${shortId} ${job.status || "-"} ${Number.isFinite(percent) ? `${Math.round(percent)}%` : ""}${detail ? ` ${detail}` : ""}`.trim();
    });
    const hasActive = payload.comfyui_jobs.some((job) => ["queued", "running", "processing", "submitted"].includes(String(job.status || "").toLowerCase()));
    const hasOnlyFailures = payload.comfyui_jobs.length > 0 && payload.comfyui_jobs.every((job) => ["error", "failed", "cancelled"].includes(String(job.status || "").toLowerCase()));
    if (hasActive || !comfyuiStatus) {
      lines.push(`ComfyUI 任務：${payload.comfyui_jobs.length ? jobs.join("；") : "目前沒有可見任務"}`);
    } else if (hasOnlyFailures && comfyuiStatus?.available === true) {
      lines.push(`近期任務：有 ${payload.comfyui_jobs.length} 筆失敗紀錄，但目前服務已連線，這些舊失敗不代表現在不能生圖。`);
    } else {
      lines.push(`ComfyUI 任務：${payload.comfyui_jobs.length ? jobs.join("；") : "目前沒有可見任務"}`);
    }
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
    const unavailable = AI_AGENT_STATE.unavailableModelIds?.has(id);
    const reason = AI_AGENT_STATE.unavailableModelReasons?.[id] || "";
    const suffix = unavailable ? `（不可用${reason ? `：${reason}` : ""}）` : (allowed ? "" : "（未在允許清單）");
    return `<button class="drive-file-row ai-agent-model-option${allowed && !unavailable ? "" : " disabled"}" type="button" ${allowed && !unavailable ? "" : "disabled"} data-ai-agent-model="${sanitize(id)}">${sanitize(id || "-")}${sanitize(suffix)}</button>`;
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

async function aiAgentRefreshModelState() {
  const statusRes = await apiFetch(API + "/ai-agent/status", { credentials: "same-origin" });
  const statusJson = await statusRes.json().catch(() => ({}));
  if (statusRes.ok && statusJson.ok) {
    renderAiAgentStatus(statusJson);
  }
  const modelsRes = await apiFetch(API + "/ai-agent/models", { credentials: "same-origin" });
  const modelsJson = await modelsRes.json().catch(() => ({}));
  if (modelsRes.ok && modelsJson.ok) {
    renderAiAgentModels(modelsJson.models || {});
  }
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
      if (json?.actor?.role === "super_admin") {
        await loadAiAgentWriteToolCatalog({ force: true }).catch(() => undefined);
      }
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
  AI_AGENT_STATE.imageLoading = false;
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
  AI_AGENT_STATE.imageLoading = false;
  if (!file) {
    if ($("ai-agent-image-state")) $("ai-agent-image-state").textContent = "未附加圖片";
    return;
  }
  if (!file.type || !file.type.startsWith("image/")) {
    setAiAgentMessage("只能附加圖片檔", "err");
    event.target.value = "";
    return;
  }
  if (file.size > 3 * 1024 * 1024) {
    setAiAgentMessage("圖片大小不可超過 3 MB；可上傳 1024 或一般 1920x1080 JPEG，過大的 PNG 請先轉成 JPEG。", "err");
    event.target.value = "";
    return;
  }
  AI_AGENT_STATE.imageLoading = true;
  if ($("ai-agent-image-state")) $("ai-agent-image-state").textContent = "圖片讀取中...";
  const reader = new FileReader();
  reader.onload = () => {
    AI_AGENT_STATE.imageDataUrl = String(reader.result || "");
    AI_AGENT_STATE.imageLoading = false;
    if ($("ai-agent-mode")) $("ai-agent-mode").value = "image";
    if ($("ai-agent-image-state")) $("ai-agent-image-state").textContent = file.name || "已附加圖片";
  };
  reader.onerror = () => {
    AI_AGENT_STATE.imageLoading = false;
    AI_AGENT_STATE.imageDataUrl = "";
    if ($("ai-agent-image-state")) $("ai-agent-image-state").textContent = "圖片讀取失敗";
    setAiAgentMessage("圖片讀取失敗", "err");
  };
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
  const selectedMode = $("ai-agent-mode")?.value || "text";
  const hasAttachedImage = !!AI_AGENT_STATE.imageDataUrl;
  const mode = hasAttachedImage ? "image" : selectedMode;
  if (!prompt && !(mode === "image" && AI_AGENT_STATE.imageDataUrl)) {
    setAiAgentMessage("請輸入訊息", "err");
    return;
  }
  if (mode === "image" && AI_AGENT_STATE.imageLoading) {
    setAiAgentMessage("圖片仍在讀取中，請稍等讀取完成後再送出", "err");
    return;
  }
  if (mode === "image" && !AI_AGENT_STATE.imageDataUrl) {
    setAiAgentMessage("請選擇圖片", "err");
    return;
  }
  if (mode === "text" && aiAgentLooksLikeComfyuiRecall(prompt)) {
    AI_AGENT_STATE.messages.push({ role: "user", content: prompt });
    AI_AGENT_STATE.messages.push({ role: "assistant", content: aiAgentComfyuiRecallSummary() });
    renderAiAgentThread();
    if (input) input.value = "";
    setAiAgentMessage("已回顧本頁生圖版本紀錄", "info");
    return;
  }
  const plannerText = prompt || (mode === "image" ? "請分析這張圖片" : "");
  if (aiAgentShouldUseToolPlanner(plannerText)) {
    if (!AI_AGENT_STATE.loaded && typeof loadAiAgentStatus === "function") {
      await loadAiAgentStatus({ force: true }).catch(() => undefined);
    }
    const sendBtn = $("ai-agent-send-btn");
    AI_AGENT_STATE.sending = true;
    if (sendBtn) sendBtn.disabled = true;
    setAiAgentMessage("理解需求與規劃工具中...", "info");
    let plan = null;
    try {
      plan = await aiAgentPlanToolAction(plannerText, {
        mode,
        hasImage: mode === "image" && !!AI_AGENT_STATE.imageDataUrl,
      });
    } catch (err) {
      plan = null;
    } finally {
      if (!plan) {
        AI_AGENT_STATE.sending = false;
        if (sendBtn) sendBtn.disabled = false;
      }
    }
    if (plan) {
      try {
        if (await aiAgentExecuteToolPlan(plan, plannerText, input, {
          mode,
          hasImage: mode === "image" && !!AI_AGENT_STATE.imageDataUrl,
        })) {
          return;
        }
      } finally {
        AI_AGENT_STATE.sending = false;
        if (sendBtn) sendBtn.disabled = false;
      }
    }
  }
  AI_AGENT_STATE.sending = true;
  const sendBtn = $("ai-agent-send-btn");
  if (sendBtn) sendBtn.disabled = true;
  setAiAgentMessage("送出中...", "info");
  const selectedModel = mode === "image" ? aiAgentVisionModel() : aiAgentSelectedTextModel();
  const selectableModels = aiAgentSelectableModels();
  if (mode === "image" && !selectedModel) {
    AI_AGENT_STATE.sending = false;
    if (sendBtn) sendBtn.disabled = false;
    setAiAgentMessage("目前沒有可用的圖片理解模型。請在 AI Agent 模型允許清單加入 /models 回傳且支援圖片的模型後再試。", "err");
    return;
  }
  if (selectableModels.length && (!selectedModel || !selectableModels.includes(selectedModel))) {
    AI_AGENT_STATE.sending = false;
    if (sendBtn) sendBtn.disabled = false;
    setAiAgentMessage("請從模型選單選擇可用模型。", "err");
    return;
  }
  const userText = prompt || (mode === "image" ? "請分析這張圖片" : "[圖片]");
  AI_AGENT_STATE.messages.push({ role: "user", content: mode === "image" && AI_AGENT_STATE.imageDataUrl ? `${userText}\n[已附加圖片]` : userText });
  renderAiAgentThread();
  try {
    const payload = {
      session_id: aiAgentEnsureSessionId(),
      model: selectedModel,
      mode,
      messages: aiAgentBuildMessages(userText, mode),
      image_data_url: mode === "image" ? AI_AGENT_STATE.imageDataUrl : "",
    };
    const raw = await aiAgentChatFetch(payload, { mode }).then(async (res) => {
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
  if (event?.detail?.current === "ai-agent") {
    loadAiAgentStatus();
    aiAgentScrollThreadToBottom();
  }
});

document.addEventListener("hackme:account-context-changed", handleAiAgentAccountContextChanged);

$("ai-agent-history-btn")?.addEventListener("click", toggleAiAgentConversationHistory);
$("ai-agent-tool-selector-refresh-btn")?.addEventListener("click", () => loadAiAgentWriteToolCatalog({ force: true }));
$("ai-agent-tool-selector-save-btn")?.addEventListener("click", saveAiAgentToolSelection);
$("ai-agent-tool-select-all-btn")?.addEventListener("click", () => setAiAgentToolSelection("all"));
$("ai-agent-tool-select-none-btn")?.addEventListener("click", () => setAiAgentToolSelection("none"));
$("ai-agent-tool-select-comfyui-btn")?.addEventListener("click", () => setAiAgentToolSelection("comfyui"));

aiAgentResetScopeState();

renderAiAgentThread();
aiAgentHydratePersistedComfyuiImages();
