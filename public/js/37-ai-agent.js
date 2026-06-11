'use strict';

const AI_AGENT_STATE = {
  loaded: false,
  loading: false,
  sending: false,
  readonlyLoading: false,
  messages: [],
  imageDataUrl: "",
  settings: {},
  actor: {},
  audit: {},
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

function aiAgentResetScopeState() {
  const nextScope = aiAgentCurrentAccountScope();
  const previousScope = AI_AGENT_STATE.accountScope;
  AI_AGENT_STATE.accountScope = nextScope;
  if (previousScope && previousScope !== nextScope) {
    clearAiAgentConversation();
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


function renderAiAgentThread() {
  const host = $("ai-agent-thread");
  if (!host) return;
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
    status.textContent = health.ok ? "Hermes API 已連線" : `Hermes API 未連線${health.msg ? `：${health.msg}` : ""}`;
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
  const allowedModels = String(settings.allowed_models || "").split(",").map((item) => item.trim()).filter(Boolean);
  const modelInput = $("ai-agent-model");
  if (modelInput && (!modelInput.value || (allowedModels.length && !allowedModels.includes(modelInput.value)))) {
    modelInput.value = allowedModels[0] || settings.model || "hermes-agent";
  }
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

function renderAiAgentModels(modelsPayload) {
  const host = $("ai-agent-model-list");
  if (!host) return;
  const rawModels = Array.isArray(modelsPayload?.data) ? modelsPayload.data : [];
  const allowedModels = String(AI_AGENT_STATE.settings?.allowed_models || "").split(",").map((item) => item.trim()).filter(Boolean);
  const modelIds = [];
  rawModels.forEach((model) => {
    const id = typeof model === "string" ? model : model?.id || model?.name || "";
    if (id && !modelIds.includes(id)) modelIds.push(id);
  });
  allowedModels.forEach((id) => {
    if (id && !modelIds.includes(id)) modelIds.unshift(id);
  });
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
      const input = $("ai-agent-model");
      if (input) input.value = button.dataset.aiAgentModel || "";
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
  AI_AGENT_STATE.messages = [];
  AI_AGENT_STATE.imageDataUrl = "";
  AI_AGENT_STATE.sessionId = "";
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
  AI_AGENT_STATE.sending = true;
  const sendBtn = $("ai-agent-send-btn");
  if (sendBtn) sendBtn.disabled = true;
  setAiAgentMessage("送出中...", "info");
  const userText = prompt || "[圖片]";
  AI_AGENT_STATE.messages.push({ role: "user", content: mode === "image" && AI_AGENT_STATE.imageDataUrl ? `${userText}\n[已附加圖片]` : userText });
  renderAiAgentThread();
  try {
    const payload = {
      session_id: aiAgentEnsureSessionId(),
      model: ($("ai-agent-model")?.value || "").trim(),
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
