function adminPercentValue(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function adminInputPercent(value, fallback = 0) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.round(number * 10000) / 10000;
}

function adminFormatPercent(value, fallback = 0) {
  const percent = adminPercentValue(value, fallback);
  return percent.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

const CLOUD_DRIVE_TRANSFER_LEVELS = [
  { key: "newbie", label: "新手" },
  { key: "normal", label: "一般" },
  { key: "trusted", label: "可信任" },
  { key: "vip", label: "VIP" },
  { key: "restricted", label: "限制中" },
  { key: "suspended", label: "停權" }
];

const DEFAULT_CLOUD_DRIVE_TRANSFER_LIMITS = {
  newbie: { upload_kb_per_sec: 256, download_kb_per_sec: 512, priority: 20 },
  normal: { upload_kb_per_sec: 512, download_kb_per_sec: 1024, priority: 40 },
  trusted: { upload_kb_per_sec: 2048, download_kb_per_sec: 4096, priority: 70 },
  vip: { upload_kb_per_sec: 8192, download_kb_per_sec: 16384, priority: 90 },
  restricted: { upload_kb_per_sec: 128, download_kb_per_sec: 256, priority: 10 },
  suspended: { upload_kb_per_sec: 0, download_kb_per_sec: 0, priority: 0 }
};

const DEFAULT_COMFYUI_REMOTE_API_URL = "http://127.0.0.1:8188";
const ADMIN_HF_MODEL_FILE_EXT_RE = /\.(?:safetensors|ckpt|pt|pth|bin|gguf)$/i;

function normalizeAdminHuggingFaceRepoInput(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const withScheme = raw.startsWith("http://") || raw.startsWith("https://") ? raw : (/^huggingface\.co\//i.test(raw) ? `https://${raw}` : "");
  if (withScheme) {
    try {
      const parsed = new URL(withScheme);
      const host = String(parsed.hostname || "").toLowerCase();
      if (host === "huggingface.co" || host === "www.huggingface.co") {
        const parts = parsed.pathname.split("/").filter(Boolean);
        const tail = parts[parts.length - 1] || "";
        if (ADMIN_HF_MODEL_FILE_EXT_RE.test(tail)) return "";
        return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : "";
      }
    } catch (_) {}
  }
  const normalized = raw.replace(/^\/+|\/+$/g, "");
  const tail = normalized.replace(/\\/g, "/").split("/").pop() || "";
  if (ADMIN_HF_MODEL_FILE_EXT_RE.test(tail)) return "";
  const parts = normalized.split("/");
  if (parts.length !== 2 || !parts[0] || !parts[1]) return "";
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/.test(parts[0])
    && /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/.test(parts[1])
    ? normalized
    : "";
}

let suppressNextSettingsStatusClear = false;
let currentServerMode = "dev_ready";
let settingsStatusAutoClearTimer = null;
let backpressureTrafficPollTimer = null;
let systemResourcePollTimer = null;
let systemResourceRefreshSeconds = 5;
let systemResourceRefreshInFlight = false;
let lastServerEnvironment = {};
let lastServerDatabaseUsage = {};
let lastServerTransferUsage = {};
let lastServerResourceUsage = {};
let rootBugReportsCache = [];
let rootBugReportSelectedId = "";
let backpressureTrafficRefreshSeconds = 4;
let serverOutputRefreshSeconds = 3;
let securityTestJobPollSeconds = 3;

const ROOT_ADMIN_TIMING_META = {
  "first-summary": {
    label: "first-summary",
    detail: "健康中心首次摘要 render",
    warnMs: 500,
    criticalMs: 1500,
  },
  "secondary-chart": {
    label: "secondary-chart",
    detail: "容量頁次要圖表 render",
    warnMs: 250,
    criticalMs: 900,
  },
};

function rootAdminTimingStore() {
  if (typeof window === "undefined") return {};
  if (!window.__hackmeRootAdminTimings || typeof window.__hackmeRootAdminTimings !== "object") {
    window.__hackmeRootAdminTimings = {};
  }
  return window.__hackmeRootAdminTimings;
}

function rootAdminTimingStart(key) {
  const started = typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
  if (typeof performance !== "undefined" && performance.mark) {
    try {
      performance.mark(`hackme.root.${key}.start`);
    } catch (_) {}
  }
  return started;
}

function rootAdminTimingFinish(key, started, detail = "") {
  const now = typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
  const durationMs = Math.max(0, now - Number(started || now));
  const meta = ROOT_ADMIN_TIMING_META[key] || { label: key, detail: "", warnMs: 500, criticalMs: 1500 };
  if (typeof performance !== "undefined" && performance.mark) {
    try {
      performance.mark(`hackme.root.${key}.end`);
    } catch (_) {}
  }
  if (typeof performance !== "undefined" && performance.measure) {
    try {
      performance.measure(`hackme.root.${key}`, `hackme.root.${key}.start`, `hackme.root.${key}.end`);
    } catch (_) {}
  }
  const record = {
    key,
    label: meta.label || key,
    detail: detail || meta.detail || "",
    duration_ms: Math.round(durationMs * 10) / 10,
    sampled_at: new Date().toISOString(),
    warn_ms: Number(meta.warnMs || 0),
    critical_ms: Number(meta.criticalMs || 0),
  };
  rootAdminTimingStore()[key] = record;
  renderRootFrontendTimingObservability();
  return record;
}

function rootAdminTimingColor(record) {
  const value = Number(record?.duration_ms || 0);
  if (record?.critical_ms && value >= Number(record.critical_ms)) return "#ff4f6d";
  if (record?.warn_ms && value >= Number(record.warn_ms)) return "#ffb74d";
  return "#4caf50";
}

function renderRootFrontendTimingObservability() {
  const host = $("server-health-frontend-observability");
  if (!host) return;
  const store = rootAdminTimingStore();
  const rows = Object.keys(ROOT_ADMIN_TIMING_META).map((key) => {
    const record = store[key] || {};
    if (!record.duration_ms && record.duration_ms !== 0) {
      return {
        label: ROOT_ADMIN_TIMING_META[key].label,
        value: "尚未量測",
        detail: ROOT_ADMIN_TIMING_META[key].detail,
        color: "#9e9e9e",
      };
    }
    return {
      label: record.label || key,
      value: `${Number(record.duration_ms).toLocaleString()} ms`,
      detail: `${record.detail || ROOT_ADMIN_TIMING_META[key].detail} · ${record.sampled_at || ""}`,
      color: rootAdminTimingColor(record),
    };
  });
  host.innerHTML = renderHealthRows(rows);
}

function adminRefreshSeconds(value, fallback = 5, min = 1, max = 300) {
  const parsed = parseInt(value, 10);
  return Math.max(min, Math.min(max, Number.isFinite(parsed) ? parsed : fallback));
}

function parseCloudDriveTransferLimits(raw) {
  if (!raw) return { ...DEFAULT_CLOUD_DRIVE_TRANSFER_LIMITS };
  try {
    const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
    const out = {};
    CLOUD_DRIVE_TRANSFER_LEVELS.forEach(({ key }) => {
      const value = parsed?.[key] || {};
      out[key] = {
        upload_kb_per_sec: Number.isFinite(Number(value.upload_kb_per_sec)) ? Math.max(0, parseInt(value.upload_kb_per_sec, 10)) : DEFAULT_CLOUD_DRIVE_TRANSFER_LIMITS[key].upload_kb_per_sec,
        download_kb_per_sec: Number.isFinite(Number(value.download_kb_per_sec)) ? Math.max(0, parseInt(value.download_kb_per_sec, 10)) : DEFAULT_CLOUD_DRIVE_TRANSFER_LIMITS[key].download_kb_per_sec,
        priority: Number.isFinite(Number(value.priority)) ? Math.min(100, Math.max(0, parseInt(value.priority, 10))) : DEFAULT_CLOUD_DRIVE_TRANSFER_LIMITS[key].priority
      };
    });
    return out;
  } catch (_) {
    return { ...DEFAULT_CLOUD_DRIVE_TRANSFER_LIMITS };
  }
}

function renderCloudDriveTransferLimits(raw) {
  const host = $("cloud-drive-transfer-limits-list");
  if (!host) return;
  const limits = parseCloudDriveTransferLimits(raw);
  host.innerHTML = CLOUD_DRIVE_TRANSFER_LEVELS.map(({ key, label }) => {
    const value = limits[key] || DEFAULT_CLOUD_DRIVE_TRANSFER_LIMITS[key];
    return `
      <div class="drive-transfer-limit-row" data-drive-transfer-level="${key}">
        <div class="drive-transfer-limit-level">${label}</div>
        <label>上傳 KB/s
          <input type="number" min="0" step="1" data-drive-transfer-field="upload_kb_per_sec" value="${value.upload_kb_per_sec}" />
        </label>
        <label>下載 KB/s
          <input type="number" min="0" step="1" data-drive-transfer-field="download_kb_per_sec" value="${value.download_kb_per_sec}" />
        </label>
        <label>優先序
          <input type="number" min="0" max="100" step="1" data-drive-transfer-field="priority" value="${value.priority}" />
        </label>
      </div>
    `;
  }).join("");
}

function collectCloudDriveTransferLimits() {
  const out = {};
  CLOUD_DRIVE_TRANSFER_LEVELS.forEach(({ key }) => {
    const row = document.querySelector(`[data-drive-transfer-level="${key}"]`);
    const fallback = DEFAULT_CLOUD_DRIVE_TRANSFER_LIMITS[key];
    out[key] = {};
    ["upload_kb_per_sec", "download_kb_per_sec", "priority"].forEach((field) => {
      const input = row?.querySelector(`[data-drive-transfer-field="${field}"]`);
      const max = field === "priority" ? 100 : Number.MAX_SAFE_INTEGER;
      const raw = parseInt(input?.value || `${fallback[field]}`, 10);
      out[key][field] = Math.min(max, Math.max(0, Number.isFinite(raw) ? raw : fallback[field]));
    });
  });
  return out;
}

function relocateSystemAdminSections() {
  const moves = [
    ["sec-settings-security", "security-settings-slot"],
    ["sec-server-health", "system-health-slot"],
    ["sec-settings-features", "system-features-slot"],
    ["sec-settings-appearance", "system-appearance-slot"],
    ["sec-settings-system", "system-core-slot"],
    ["sec-settings-backpressure", "system-capacity-slot"],
    ["sec-server-env", "system-env-slot"],
    ["sec-server-launch-check", "server-mode-launch-check-slot"],
    ["sec-settings-drive", "drive-root-settings-slot"],
    ["sec-settings-drive-transfer-limits", "drive-root-settings-slot"],
    ["sec-settings-member-levels", "accounts-member-settings-slot"],
    ["sec-settings-module-access", "accounts-module-access-slot"],
    ["sec-settings-comfyui", "comfyui-settings-slot"],
    ["sec-settings-billing", "economy-pricing-settings-slot"],
    ["sec-settings-danger-ops", "system-danger-ops-slot"],
    ["sec-settings-trading", "trading-settings-slot"],
  ];
  moves.forEach(([sectionId, slotId]) => {
    const section = $(sectionId);
    const slot = $(slotId);
    if (section && slot && section.parentElement !== slot) {
      section.classList.add("active");
      slot.appendChild(section);
    }
    if (sectionId === "sec-settings-comfyui" && section && slot && slot.contains(section)) {
      section.open = true;
      section.dataset.frontendSettingsExpanded = "1";
    }
  });
}

function isSystemOverviewActive() {
  return currentModuleTab === "server" && currentServerTab === "overview";
}

function isSystemHealthActive() {
  return currentModuleTab === "system" && currentSystemTab === "health";
}

function isSystemSettingsActive() {
  return currentModuleTab === "system" && currentSystemTab === "capacity";
}

function isSystemEnvActive() {
  return currentModuleTab === "system" && currentSystemTab === "env";
}

function isSystemBugReportsActive() {
  return currentModuleTab === "system" && currentSystemTab === "bug-reports";
}

function canRunRootManagementPoll(isActive) {
  if (currentUser !== "root" || document.hidden) return false;
  return typeof isActive !== "function" || isActive();
}

function scheduleRootManagementIdleTask(callback, timeout = 900) {
  if (typeof callback !== "function") return;
  const run = () => {
    if (document.hidden) return;
    try {
      const result = callback();
      if (result && typeof result.catch === "function") result.catch(() => {});
    } catch (_) {}
  };
  if (typeof window !== "undefined" && typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(run, { timeout });
  } else {
    setTimeout(run, 0);
  }
}

function stopRootManagementPolls() {
  stopServerOutputPoll();
  stopSystemResourcePoll();
  stopBackpressureTrafficPoll();
}

function resumeRootManagementPolls() {
  if (!canRunRootManagementPoll()) return;
  if (isSystemOverviewActive()) {
    startServerOutputPoll();
    scheduleRootManagementIdleTask(loadServerOutput, 500);
  }
  if (isSystemSettingsActive()) {
    startBackpressureTrafficPoll();
    scheduleRootManagementIdleTask(refreshBackpressureTraffic, 650);
  }
  if (isSystemEnvActive()) {
    startSystemResourcePoll();
    scheduleRootManagementIdleTask(refreshSystemResourceBoard, 650);
  }
}

function installRootManagementVisibilityGuard() {
  if (typeof window === "undefined" || window.__rootManagementVisibilityGuardInstalled) return;
  window.__rootManagementVisibilityGuardInstalled = true;
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopRootManagementPolls();
      return;
    }
    resumeRootManagementPolls();
  });
  window.addEventListener("pagehide", stopRootManagementPolls);
}

installRootManagementVisibilityGuard();

function updateServerModeLaunchCheckVisibility() {
  const target = String($("server-mode-select")?.value || currentServerMode || "").trim().toLowerCase();
  const shouldShow = target === "production";
  ["tab-system-launch-check", "system-launch-check-slot"].forEach((id) => {
    const el = $(id);
    if (el) el.style.display = "none";
  });
  const slot = $("server-mode-launch-check-slot");
  const section = $("sec-server-launch-check");
  if (slot) slot.style.display = shouldShow ? "" : "none";
  if (section) section.classList.toggle("active", shouldShow);
  if (!shouldShow && currentSystemTab === "launch-check") {
    switchSystemTab("health");
  }
  if (shouldShow && currentModuleTab === "server" && currentServerTab === "server-mode" && typeof loadLaunchCheck === "function") {
    loadLaunchCheck();
  }
}

function switchServerTab(tab) {
  currentServerTab = ["overview", "server-mode", "audit", "integrity"].includes(tab) ? tab : "overview";
  stopServerOutputPoll();
  stopBackpressureTrafficPoll();
  stopSystemResourcePoll();
  const sectionByTab = {
    overview: "sec-server-security",
    "server-mode": "sec-server-settings",
    audit: "sec-server-audit",
    integrity: "sec-server-integrity",
  };
  Object.entries(sectionByTab).forEach(([name, id]) => {
    const sec = $(id);
    if (sec) sec.classList.toggle("active", name === currentServerTab);
  });
  ["tab-server-overview", "tab-server-server-mode", "tab-server-audit", "tab-server-integrity"].forEach((id) => {
    const btn = $(id);
    if (!btn) return;
    btn.classList.toggle("active", id === "tab-server-" + currentServerTab);
  });
  if (currentServerTab === "overview") {
    loadSecurityCenter();
    loadSettings();
    startServerOutputPoll();
    scheduleRootManagementIdleTask(loadServerOutput, 600);
  }
  if (currentServerTab === "server-mode") {
    loadSettings();
    loadServerMode();
  }
  if (currentServerTab === "audit") loadAudit(0);
  if (currentServerTab === "integrity") loadIntegrityGuard();
  updateServerModeLaunchCheckVisibility();
  if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
}

function switchSystemTab(tab) {
  relocateSystemAdminSections();
  const allowedTabs = ["health", "features", "appearance", "core", "capacity", "env", "launch-check", "bug-reports"];
  currentSystemTab = allowedTabs.includes(tab) ? tab : "health";
  stopServerOutputPoll();
  if (currentSystemTab !== "capacity") stopBackpressureTrafficPoll();
  if (currentSystemTab !== "env") stopSystemResourcePoll();
  const sectionByTab = {
    health: "sec-server-health",
    features: "sec-settings-features",
    appearance: "sec-settings-appearance",
    core: "sec-settings-system",
    capacity: "sec-settings-backpressure",
    env: "sec-server-env",
    "launch-check": "sec-server-launch-check",
    "bug-reports": "system-bug-reports-slot",
  };
  Object.entries(sectionByTab).forEach(([name, id]) => {
    const sec = $(id);
    const slot = $(`system-${name}-slot`);
    if (sec) sec.classList.toggle("active", name === currentSystemTab);
    if (slot) slot.style.display = name === currentSystemTab ? "" : "none";
  });
  [
    "tab-system-health",
    "tab-system-features",
    "tab-system-appearance",
    "tab-system-core",
    "tab-system-capacity",
    "tab-system-env",
    "tab-system-launch-check",
    "tab-system-bug-reports",
  ].forEach((id) => {
    const btn = $(id);
    if (!btn) return;
    btn.classList.toggle("active", id === "tab-system-" + currentSystemTab);
  });
  if (currentSystemTab === "health") {
    loadServerHealth();
    scheduleRootManagementIdleTask(loadPlatformStats, 750);
    scheduleRootManagementIdleTask(() => {
      if (isSystemHealthActive()) loadServerUpdateStatus(false);
    }, 900);
  }
  if (["features", "appearance", "core"].includes(currentSystemTab)) loadSettings();
  if (currentSystemTab === "capacity") {
    loadSettings();
    startBackpressureTrafficPoll();
  }
  if (currentSystemTab === "env") {
    loadServerEnv();
    startSystemResourcePoll();
  }
  if (currentSystemTab === "launch-check") loadLaunchCheck();
  if (currentSystemTab === "bug-reports") {
    loadRootBugReports();
  }
  updateServerModeLaunchCheckVisibility();
  if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
}

function switchSettingsSection(tab) {
  if (tab === "trading") {
    if (typeof switchModuleTab === "function") switchModuleTab("trading");
    if (typeof openTradingSettingsPage === "function") openTradingSettingsPage();
    return;
  }
  if (tab === "drive" || tab === "shares" || tab === "albums") {
    if (typeof switchModuleTab === "function") switchModuleTab("drive");
    if (typeof setDriveActivePage === "function") setDriveActivePage("root-admin");
    if (typeof loadSettings === "function") loadSettings();
    if (typeof loadCloudDriveAdminPolicy === "function") loadCloudDriveAdminPolicy();
    return;
  }
  if (tab === "member-levels" || tab === "module-access" || tab === "accounts") {
    if (typeof switchModuleTab === "function") switchModuleTab("accounts");
    switchAdminTab("member-settings");
    if (typeof loadSettings === "function") loadSettings();
    return;
  }
  if (tab === "comfyui") {
    if (typeof switchModuleTab === "function") switchModuleTab("comfyui");
    if (typeof setComfyuiView === "function") setComfyuiView("settings");
    if (typeof loadSettings === "function") loadSettings();
    return;
  }
  if (tab === "billing") {
    if (typeof switchModuleTab === "function") switchModuleTab("economy");
    if (typeof setEconomyActivePage === "function") setEconomyActivePage("balance");
    if (typeof loadRootEconomyCatalog === "function") loadRootEconomyCatalog();
    if (typeof loadSettings === "function") loadSettings();
    return;
  }
  if (tab === "security") {
    if (typeof switchModuleTab === "function") switchModuleTab("server");
    if (typeof switchServerTab === "function") switchServerTab("overview");
    return;
  }
  const systemTabBySettingsSection = {
    features: "features",
    appearance: "appearance",
    system: "core",
    core: "core",
    capacity: "capacity",
    backpressure: "capacity",
  };
  const targetSystemTab = systemTabBySettingsSection[tab] || "core";
  if (typeof switchModuleTab === "function" && currentModuleTab !== "system") switchModuleTab("system");
  if (typeof switchSystemTab === "function") switchSystemTab(targetSystemTab);
  currentSettingsSection = tab || "core";
  if (typeof clearSettingsStatus === "function") {
    if (suppressNextSettingsStatusClear) suppressNextSettingsStatusClear = false;
    else clearSettingsStatus();
  }
}

function rootBugReportMsg(text = "", ok = true) {
  const el = $("root-bug-report-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = text ? "msg show" + (ok ? " ok" : " err") : "msg";
}

function rootBugReportStatusLabel(status) {
  return {
    new: "待審",
    approved: "已核准",
    rejected: "已駁回",
  }[String(status || "")] || String(status || "-");
}

function rootBugReportSeverityColor(severity) {
  return {
    low: "#82b1ff",
    medium: "#ffd166",
    high: "#ff9f43",
    critical: "#ff4f6d",
  }[String(severity || "").toLowerCase()] || "var(--muted)";
}

function filteredRootBugReports() {
  const status = $("root-bug-report-status")?.value || "all";
  return rootBugReportsCache.filter((item) => status === "all" || String(item.status || "new") === status);
}

function renderRootBugReportSummary() {
  const host = $("root-bug-report-summary");
  if (!host) return;
  const counts = rootBugReportsCache.reduce((acc, item) => {
    const status = String(item.status || "new");
    const severity = String(item.severity || "medium");
    acc.total += 1;
    acc[status] = (acc[status] || 0) + 1;
    acc[`sev_${severity}`] = (acc[`sev_${severity}`] || 0) + 1;
    return acc;
  }, { total: 0, new: 0, approved: 0, rejected: 0 });
  host.innerHTML = [
    ["全部", counts.total, "var(--accent)"],
    ["待審", counts.new || 0, "#ffd166"],
    ["已核准", counts.approved || 0, "#4ade80"],
    ["已駁回", counts.rejected || 0, "#ff4f6d"],
    ["Critical", counts.sev_critical || 0, "#ff4f6d"],
  ].map(([label, value, color]) => `
    <div class="health-card">
      <strong style="color:${color};">${sanitize(String(value))}</strong>
      <span>${sanitize(label)}</span>
    </div>
  `).join("");
}

function renderRootBugReportList() {
  renderRootBugReportSummary();
  const list = $("root-bug-report-list");
  if (!list) return;
  const rows = filteredRootBugReports();
  if (!rows.length) {
    list.innerHTML = `<div class="drive-empty">目前沒有符合篩選條件的 bug 回報</div>`;
    renderRootBugReportDetail(null);
    return;
  }
  if (!rows.some((item) => String(item.id || "") === String(rootBugReportSelectedId || ""))) {
    rootBugReportSelectedId = rows[0]?.id || "";
  }
  list.innerHTML = rows.map((item) => {
    const active = String(item.id || "") === String(rootBugReportSelectedId || "");
    const status = rootBugReportStatusLabel(item.status || "new");
    const color = rootBugReportSeverityColor(item.severity);
    return `
      <button class="drive-file-row ${active ? "active" : ""}" type="button" data-root-bug-report-select="${sanitize(item.id || "")}" style="width:100%;text-align:left;">
        <div>
          <strong>${sanitize(item.title || item.id || "-")}</strong>
          <div class="drive-card-sub">
            <span style="color:${color};font-weight:700;">${sanitize(item.severity || "medium")}</span>
            · ${sanitize(status)}
            · ${sanitize(item.feature || "other")}
            · ${sanitize(item.device || "unknown")}
          </div>
          <div class="drive-card-sub">${sanitize(item.reporter || "-")} · ${sanitize(item.created_at || "")}</div>
        </div>
      </button>
    `;
  }).join("");
  list.querySelectorAll("[data-root-bug-report-select]").forEach((btn) => {
    btn.addEventListener("click", () => {
      rootBugReportSelectedId = btn.dataset.rootBugReportSelect || "";
      renderRootBugReportList();
    });
  });
  renderRootBugReportDetail(rows.find((item) => String(item.id || "") === String(rootBugReportSelectedId || "")) || rows[0] || null);
}

function rootBugReportDetailRow(label, value) {
  if (!value) return "";
  return `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(label)}</strong>
        <div class="drive-card-sub" style="white-space:pre-wrap;">${sanitize(String(value))}</div>
      </div>
    </div>
  `;
}

function rootBugReportSummaryText(item) {
  if (!item) return "";
  return [
    `Bug 回報 ${item.id || "-"}`,
    `標題：${item.title || "-"}`,
    `狀態：${rootBugReportStatusLabel(item.status || "new")} / ${item.severity || "medium"}`,
    `回報者：${item.reporter || "-"}${item.reporter_id ? ` (#${item.reporter_id})` : ""}`,
    `功能：${item.feature || "other"}`,
    `頁面：${item.page || "-"}`,
    `描述：${item.description || "-"}`,
    `步驟：${item.steps || "-"}`,
    `預期：${item.expected || "-"}`,
    `實際：${item.actual || "-"}`,
  ].join("\n");
}

function selectedRootBugReport(reportId) {
  const id = String(reportId || rootBugReportSelectedId || "");
  return (rootBugReportsCache || []).find((item) => String(item.id || "") === id) || null;
}

async function copyRootBugReportText(text, button, okMessage) {
  const value = String(text || "");
  if (!value) {
    rootBugReportMsg("沒有可複製的內容。", false);
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
    rootBugReportMsg(okMessage || "已複製 bug 回報內容。");
    if (typeof showActionFeedback === "function") {
      showActionFeedback(button || document.activeElement, okMessage || "已複製", true, { skipToast: true });
    }
  } catch (_) {
    rootBugReportMsg("複製失敗，請手動選取內容。", false);
  }
}

function renderRootBugReportDetail(item) {
  const detail = $("root-bug-report-detail");
  if (!detail) return;
  detail.classList.add("show");
  if (!item) {
    detail.innerHTML = "請從左側選擇一筆 bug 回報。";
    return;
  }
  const reviewed = item.reviewed_at
    ? `審核：${item.reviewed_by || "-"} · ${item.reviewed_at}${item.ledger_uuid ? ` · ledger ${item.ledger_uuid}` : ""}`
    : "尚未審核";
  const canReview = !["approved", "rejected"].includes(String(item.status || ""));
  const suggestedReward = Number(item.suggested_reward_points ?? item.reward_points ?? 0);
  detail.innerHTML = `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(item.title || item.id || "-")}</strong>
        <div class="drive-card-sub">
          ${sanitize(item.id || "-")} · ${sanitize(rootBugReportStatusLabel(item.status || "new"))}
          · <span style="color:${rootBugReportSeverityColor(item.severity)};font-weight:700;">${sanitize(item.severity || "medium")}</span>
          · ${canReview ? "建議" : "實發"}獎勵 ${Number(item.reward_points || 0).toLocaleString()} 點
        </div>
        <div class="drive-card-sub">${sanitize(reviewed)}</div>
      </div>
      <div class="admin-toolbar" style="gap:.35rem;justify-content:flex-end;">
        <button class="btn btn-sm" type="button" data-root-bug-report-copy-id="${sanitize(item.id || "")}">複製 ID</button>
        <button class="btn btn-sm" type="button" data-root-bug-report-copy-summary="${sanitize(item.id || "")}">複製摘要</button>
        <button class="btn btn-sm" type="button" data-root-bug-report-open-announcement="${sanitize(item.id || "")}">編輯公告草稿</button>
        ${canReview ? `
          <button class="btn btn-sm" type="button" data-root-bug-report-approve="${sanitize(item.id || "")}">核准並發獎勵</button>
          <button class="btn btn-sm btn-danger" type="button" data-root-bug-report-reject="${sanitize(item.id || "")}">駁回</button>
        ` : ""}
      </div>
    </div>
    ${canReview ? `
      <label class="field" style="margin:.55rem 0;">
        <span>實際獎勵點數</span>
        <input type="number" id="root-bug-report-review-reward" min="0" max="1000000" step="1" value="${Number.isFinite(suggestedReward) ? suggestedReward : 0}" />
        <div class="field-hint">由 root 依實際影響決定；用戶自評 ${sanitize(item.severity || "medium")} 只作為參考，0 代表核准但不發獎勵。</div>
      </label>
      <label class="field" style="margin:.55rem 0;">
        <span>審核備註</span>
        <textarea id="root-bug-report-review-note" rows="3" maxlength="1000" placeholder="補充核准、駁回或後續處理原因">${sanitize(item.review_note || "")}</textarea>
      </label>
    ` : ""}
    <section class="server-env-panel" id="root-bug-report-announcement-draft" style="display:none;margin:.7rem 0;" aria-label="Bug 回報公告草稿">
      <div class="drive-card-title">全站公告草稿</div>
      <div class="drive-card-sub">請先確認並修改公告標題與內容，再由 root 手動發布。</div>
      <label class="field" style="margin:.55rem 0;">
        <span>公告標題</span>
        <input type="text" id="root-bug-report-announcement-title" maxlength="80" />
      </label>
      <label class="field" style="margin:.55rem 0;">
        <span>公告內容</span>
        <textarea id="root-bug-report-announcement-content" rows="8" maxlength="3000"></textarea>
      </label>
      <label class="inline-check">
        <input type="checkbox" id="root-bug-report-announcement-pinned" checked />
        <span>置頂公告</span>
      </label>
      <div class="admin-toolbar" style="gap:.45rem;margin-top:.55rem;">
        <button class="btn btn-primary" type="button" data-root-bug-report-publish-announcement="${sanitize(item.id || "")}">發布公告</button>
        <button class="btn" type="button" data-root-bug-report-cancel-announcement>取消草稿</button>
      </div>
    </section>
    ${rootBugReportDetailRow("回報者", `${item.reporter || "-"}${item.reporter_id ? ` (#${item.reporter_id})` : ""} · ${item.reporter_role || "-"}`)}
    ${rootBugReportDetailRow("功能 / 裝置 / 頁面", `${item.feature || "other"} · ${item.device || "unknown"}\n${item.page || ""}`)}
    ${rootBugReportDetailRow("問題描述", item.description)}
    ${rootBugReportDetailRow("重現步驟", item.steps)}
    ${rootBugReportDetailRow("預期結果", item.expected)}
    ${rootBugReportDetailRow("實際結果", item.actual)}
    ${rootBugReportDetailRow("請求資訊", `${item.request_ip || "-"}\n${item.user_agent || ""}`)}
    ${rootBugReportDetailRow("審核備註", item.review_note)}
    ${rootBugReportDetailRow("檔案", item.file)}
  `;
  detail.querySelectorAll("[data-root-bug-report-copy-id]").forEach((btn) => {
    btn.addEventListener("click", () => copyRootBugReportText(item.id || "", btn, "Bug 回報 ID 已複製"));
  });
  detail.querySelectorAll("[data-root-bug-report-copy-summary]").forEach((btn) => {
    btn.addEventListener("click", () => copyRootBugReportText(rootBugReportSummaryText(item), btn, "Bug 回報摘要已複製"));
  });
  detail.querySelectorAll("[data-root-bug-report-open-announcement]").forEach((btn) => {
    btn.addEventListener("click", () => openRootBugReportAnnouncementDraft(btn.dataset.rootBugReportOpenAnnouncement || ""));
  });
  detail.querySelectorAll("[data-root-bug-report-publish-announcement]").forEach((btn) => {
    btn.addEventListener("click", () => publishRootBugReportAnnouncement(btn.dataset.rootBugReportPublishAnnouncement || ""));
  });
  detail.querySelectorAll("[data-root-bug-report-cancel-announcement]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const draft = $("root-bug-report-announcement-draft");
      if (draft) draft.style.display = "none";
      rootBugReportMsg("公告草稿已收起，尚未發布。");
    });
  });
  detail.querySelectorAll("[data-root-bug-report-approve]").forEach((btn) => {
    btn.addEventListener("click", () => reviewRootBugReport(btn.dataset.rootBugReportApprove || "", "approve"));
  });
  detail.querySelectorAll("[data-root-bug-report-reject]").forEach((btn) => {
    btn.addEventListener("click", () => reviewRootBugReport(btn.dataset.rootBugReportReject || "", "reject"));
  });
}

async function loadRootBugReports() {
  if (currentUser !== "root" || !isSystemBugReportsActive()) return;
  rootBugReportMsg("Bug 回報讀取中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + "/admin/bug-reports", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    rootBugReportsCache = Array.isArray(json.reports) ? json.reports : [];
    renderRootBugReportList();
    rootBugReportMsg(`已讀取 ${rootBugReportsCache.length} 筆 bug 回報。`);
  } catch (err) {
    rootBugReportMsg(err.message || "Bug 回報讀取失敗", false);
  }
}

async function reviewRootBugReport(reportId, decision) {
  if (currentUser !== "root" || !reportId) return;
  const label = decision === "approve" ? "核准並發獎勵" : "駁回";
  const reviewNote = $("root-bug-report-review-note")?.value || "";
  const rewardInput = $("root-bug-report-review-reward");
  const rewardPoints = decision === "approve" ? Number.parseInt(rewardInput?.value || "0", 10) : 0;
  if (decision === "approve" && (!Number.isFinite(rewardPoints) || rewardPoints < 0)) {
    rootBugReportMsg("實際獎勵點數必須是 0 或正整數。", false);
    return;
  }
  rootBugReportMsg(`${label}中...`);
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/admin/bug-reports/${encodeURIComponent(reportId)}/review`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({ decision, review_note: reviewNote, reward_points: rewardPoints }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    rootBugReportMsg(`Bug 回報 ${reportId} 已${decision === "approve" ? "核准" : "駁回"}。`);
    rootBugReportSelectedId = reportId;
    await loadRootBugReports();
  } catch (err) {
    rootBugReportMsg(err.message || "Bug 回報審核失敗", false);
  }
}

function rootBugReportAnnouncementDraft(item) {
  const title = (`Bug 回報處理：${item?.title || item?.id || ""}`).slice(0, 80);
  const content = [
    `Bug 回報：${item?.title || item?.id || "-"}`,
    `回報編號：${item?.id || "-"}`,
    `目前狀態：${rootBugReportStatusLabel(item?.status || "new")}`,
    `影響等級：${item?.severity || "medium"}（以 root 審核為準）`,
    `相關功能：${item?.feature || "other"}`,
    "",
    "摘要：",
    item?.description || "-",
    "",
    "處理說明：",
    "請 root 在發布前改寫此段，說明已確認的影響範圍、臨時處理方式、修復狀態與用戶需要採取的動作。",
  ].join("\n").slice(0, 3000);
  return { title, content };
}

function openRootBugReportAnnouncementDraft(reportId) {
  if (currentUser !== "root" || !reportId) return;
  const item = selectedRootBugReport(reportId);
  if (!item) {
    rootBugReportMsg("找不到要建立公告草稿的 bug 回報。", false);
    return;
  }
  const draft = rootBugReportAnnouncementDraft(item);
  if ($("root-bug-report-announcement-title")) $("root-bug-report-announcement-title").value = draft.title;
  if ($("root-bug-report-announcement-content")) $("root-bug-report-announcement-content").value = draft.content;
  if ($("root-bug-report-announcement-pinned")) $("root-bug-report-announcement-pinned").checked = true;
  const panel = $("root-bug-report-announcement-draft");
  if (panel) {
    panel.style.display = "block";
    panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  rootBugReportMsg("已建立公告草稿，請確認並修改內容後再發布。");
}

async function publishRootBugReportAnnouncement(reportId) {
  if (currentUser !== "root" || !reportId) return;
  const item = selectedRootBugReport(reportId);
  if (!item) {
    rootBugReportMsg("找不到要發布公告的 bug 回報。", false);
    return;
  }
  const draftPanel = $("root-bug-report-announcement-draft");
  if (draftPanel && draftPanel.style.display === "none") {
    openRootBugReportAnnouncementDraft(reportId);
    return;
  }
  const title = String($("root-bug-report-announcement-title")?.value || "").trim();
  const content = String($("root-bug-report-announcement-content")?.value || "").trim();
  const isPinned = !!$("root-bug-report-announcement-pinned")?.checked;
  if (!title || !content) {
    rootBugReportMsg("請先填寫公告標題與公告內容。", false);
    return;
  }
  rootBugReportMsg("全站公告發布中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + "/community/announcements", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({ title: title.slice(0, 80), content: content.slice(0, 3000), is_pinned: isPinned }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    rootBugReportMsg("已發布為全站公告。");
    if (draftPanel) draftPanel.style.display = "none";
  } catch (err) {
    rootBugReportMsg(err.message || "全站公告發布失敗", false);
  }
}

function canOpenAdminTab(tab) {
  if (!currentUser) return false;
  const managerOrAbove = currentRole === "manager" || currentRole === "super_admin";
  switch (tab) {
    case "users":
    case "password-resets":
      return canAccessModule("accounts");
    case "member-settings":
      return currentUser === "root";
    case "violations":
      return canAccessModule("accounts") && (!isFeatureEnabledForUi || isFeatureEnabledForUi("feature_violation_center_enabled", false));
    case "governance":
      return managerOrAbove && (!isFeatureEnabledForUi || isFeatureEnabledForUi("feature_member_governance_enabled", false));
    case "notices":
      return managerOrAbove && (!isFeatureEnabledForUi || isFeatureEnabledForUi("feature_reports_notifications_enabled", false));
    case "appeals":
      return currentRole === "super_admin" && canAccessModule("appeals");
    case "reports":
      return currentRole === "super_admin" && (!isFeatureEnabledForUi || isFeatureEnabledForUi("feature_reports_enabled", false));
    default:
      return false;
  }
}

function firstAvailableAdminTab() {
  return ["users", "password-resets", "violations", "governance", "member-settings", "notices", "appeals", "reports"].find((tab) => canOpenAdminTab(tab)) || "users";
}

function switchModuleTab(tab) {
  relocateSystemAdminSections();
  const canAccessAccounts = canAccessModule("accounts");
  const canAccessServer = currentUser === "root";
  const canAccessSystem = currentUser === "root";
  const canAccessAppeals = currentRole !== "super_admin" && canAccessModule("appeals");
  const canAccessCommunity = !!currentUser && canAccessModule("community");
  const canAccessAnnouncements = canAccessCommunity;
  const canAccessChat = !!currentUser && canAccessModule("chat");
  const canAccessProfile = !!currentUser && canAccessModule("profile");
  const canAccessDrive = !!currentUser && canAccessModule("privacy_uploads");
  const canAccessAlbums = canAccessDrive && (!isFeatureEnabledForUi || isFeatureEnabledForUi("feature_storage_albums_enabled", false));
  const canAccessVideos = !!currentUser && canAccessModule("videos");
  const canAccessGames = !!currentUser && canAccessModule("games");
  const canAccessExperiments = !!currentUser && canAccessModule("experiments");
  const canAccessJobs = !!currentUser && canAccessModule("jobs");
  const canAccessShareCenter = !!currentUser && canAccessModule("shares");
  const canUseComfyuiTab = typeof isComfyuiAvailableForNavigation !== "function" || isComfyuiAvailableForNavigation();
  const canAccessComfyui = !!currentUser && canAccessModule("comfyui") && canUseComfyuiTab;
  const canAccessAiAgent = !!currentUser && canAccessModule("ai-agent");
  const canAccessEconomy = !!currentUser && canAccessModule("economy");
  const canAccessTrading = canAccessEconomy && canAccessModule("trading");

  let normTab = tab;
  const fallbackModule = () => ([
    [canAccessChat, "chat"],
    [canAccessProfile, "profile"],
    [canAccessCommunity, "community"],
    [canAccessDrive, "drive"],
    [canAccessVideos, "videos"],
    [canAccessGames, "games"],
    [canAccessExperiments, "experiments"],
    [canAccessJobs, "jobs"],
    [canAccessComfyui, "comfyui"],
    [canAccessAiAgent, "ai-agent"],
    [canAccessEconomy, "economy"],
    [canAccessAppeals, "appeals"],
    [canAccessAccounts, "accounts"],
  ].find(([allowed]) => allowed)?.[1] || "chat");
  if (tab === "chat" && !canAccessChat) normTab = fallbackModule();
  if (tab === "profile" && !canAccessProfile) normTab = fallbackModule();
  if (tab === "dm") normTab = fallbackModule();
  if (tab === "announcements" && !canAccessAnnouncements) normTab = fallbackModule();
  if (tab === "community" && !canAccessCommunity) normTab = fallbackModule();
  if (tab === "drive" && !canAccessDrive) normTab = fallbackModule();
  if (tab === "albums" && !canAccessAlbums) normTab = fallbackModule();
  if (tab === "videos" && !canAccessVideos) normTab = fallbackModule();
  if (tab === "games" && !canAccessGames) normTab = fallbackModule();
  if (tab === "experiments" && !canAccessExperiments) normTab = fallbackModule();
  if (tab === "jobs" && !canAccessJobs) normTab = fallbackModule();
  if (tab === "shares" && !canAccessShareCenter) normTab = fallbackModule();
  if (tab === "comfyui" && !canAccessComfyui) normTab = fallbackModule();
  if (tab === "ai-agent" && !canAccessAiAgent) normTab = fallbackModule();
  if (tab === "economy" && !canAccessEconomy) normTab = fallbackModule();
  if (tab === "trading" && !canAccessTrading) normTab = fallbackModule();
  if (tab === "accounts" && !canAccessAccounts) normTab = fallbackModule();
  if (tab === "server" && !canAccessServer) normTab = canAccessAccounts ? "accounts" : fallbackModule();
  if (tab === "system" && !canAccessSystem) normTab = canAccessAccounts ? "accounts" : fallbackModule();
  if (tab === "appeals" && !canAccessAppeals) normTab = fallbackModule();

  const previousModuleTab = currentModuleTab;
  currentModuleTab = normTab;
  const modChat = $("module-chat");
  const modProfile = $("module-profile");
  const modAnnouncements = $("module-announcements");
  const modCommunity = $("module-community");
  const modDrive = $("module-drive");
  const modAlbums = $("module-albums");
  const modVideos = $("module-videos");
  const modGames = $("module-games");
  const modExperiments = $("module-experiments");
  const modJobs = $("module-jobs");
  const modShares = $("module-shares");
  const modComfyui = $("module-comfyui");
  const modAiAgent = $("module-ai-agent");
  const modEconomy = $("module-economy");
  const modTrading = $("module-trading");
  const modAccounts = $("module-accounts");
  const modSystem = $("module-system");
  const modServer = $("module-server");
  const modAppeals = $("module-appeals");
  const mChat = $("tab-module-chat");
  const mProfile = $("tab-module-profile");
  const mAnnouncements = $("tab-module-announcements");
  const mCommunity = $("tab-module-community");
  const mDrive = $("tab-module-drive");
  const mAlbums = $("tab-module-albums");
  const mVideos = $("tab-module-videos");
  const mGames = $("tab-module-games");
  const mExperiments = $("tab-module-experiments");
  const mJobs = $("tab-module-jobs");
  const mShares = $("tab-module-shares");
  const mComfyui = $("tab-module-comfyui");
  const mAiAgent = $("tab-module-ai-agent");
  const mEconomy = $("tab-module-economy");
  const mTrading = $("tab-module-trading");
  const mAccounts = $("tab-module-accounts");
  const mSystem = $("tab-module-system");
  const mServer = $("tab-module-server");
  const mAppeals = $("tab-module-appeals");

  if (modChat) modChat.classList.toggle("active", normTab === "chat");
  if (modProfile) modProfile.classList.toggle("active", normTab === "profile");
  if (modAnnouncements) modAnnouncements.classList.toggle("active", normTab === "announcements");
  if (modCommunity) modCommunity.classList.toggle("active", normTab === "community");
  if (modDrive) modDrive.classList.toggle("active", normTab === "drive");
  if (modAlbums) modAlbums.classList.toggle("active", normTab === "albums");
  if (modVideos) modVideos.classList.toggle("active", normTab === "videos");
  if (modGames) modGames.classList.toggle("active", normTab === "games");
  if (modExperiments) modExperiments.classList.toggle("active", normTab === "experiments");
  if (modJobs) modJobs.classList.toggle("active", normTab === "jobs");
  if (modShares) modShares.classList.toggle("active", normTab === "shares");
  if (modComfyui) modComfyui.classList.toggle("active", normTab === "comfyui");
  if (modAiAgent) modAiAgent.classList.toggle("active", normTab === "ai-agent");
  if (modEconomy) modEconomy.classList.toggle("active", normTab === "economy");
  if (modTrading) modTrading.classList.toggle("active", normTab === "trading");
  if (modAccounts) modAccounts.classList.toggle("active", normTab === "accounts");
  if (modSystem) modSystem.classList.toggle("active", normTab === "system");
  if (modServer) modServer.classList.toggle("active", normTab === "server");
  if (modAppeals) modAppeals.classList.toggle("active", normTab === "appeals");
  if (mChat) mChat.classList.toggle("active", normTab === "chat");
  if (mProfile) mProfile.classList.toggle("active", normTab === "profile");
  if (mAnnouncements) mAnnouncements.classList.toggle("active", normTab === "announcements");
  if (mCommunity) mCommunity.classList.toggle("active", normTab === "community");
  if (mDrive) mDrive.classList.toggle("active", normTab === "drive");
  if (mAlbums) mAlbums.classList.toggle("active", normTab === "albums");
  if (mVideos) mVideos.classList.toggle("active", normTab === "videos");
  if (mGames) mGames.classList.toggle("active", normTab === "games");
  if (mExperiments) mExperiments.classList.toggle("active", normTab === "experiments");
  if (mJobs) mJobs.classList.toggle("active", normTab === "jobs");
  if (mShares) mShares.classList.toggle("active", normTab === "shares");
  if (mComfyui) mComfyui.classList.toggle("active", normTab === "comfyui");
  if (mAiAgent) mAiAgent.classList.toggle("active", normTab === "ai-agent");
  if (mEconomy) mEconomy.classList.toggle("active", normTab === "economy");
  if (mTrading) mTrading.classList.toggle("active", normTab === "trading");
  if (mAccounts) mAccounts.classList.toggle("active", normTab === "accounts");
  if (mSystem) mSystem.classList.toggle("active", normTab === "system");
  if (mServer) mServer.classList.toggle("active", normTab === "server");
  if (mAppeals) mAppeals.classList.toggle("active", normTab === "appeals");
  if (typeof animateActiveModule === "function") animateActiveModule(normTab);
  if (typeof syncRootModuleSettingsButtons === "function") syncRootModuleSettingsButtons();
  if (previousModuleTab !== normTab) {
    document.dispatchEvent(new CustomEvent("hackme:module-changed", {
      detail: { previous: previousModuleTab, current: normTab },
    }));
    try {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    } catch (_) {
      window.scrollTo(0, 0);
    }
  }

  if (normTab === "chat" && canAccessChat && typeof loadChatRooms === "function") {
    loadChatRooms();
  }
  if (normTab === "profile" && canAccessProfile && typeof loadProfilePanel === "function") {
    loadProfilePanel();
  }
  if (normTab === "community" && canAccessCommunity) {
    loadCommunityHome();
  }
  if (normTab === "announcements" && canAccessAnnouncements) {
    loadAnnouncements();
  }
  if (normTab !== "system") {
    stopServerOutputPoll();
    stopSystemResourcePoll();
    stopBackpressureTrafficPoll();
  }
  if (normTab === "server" && canAccessServer) {
    switchServerTab(currentServerTab || "overview");
  }
  if (normTab === "system" && canAccessSystem) {
    switchSystemTab(currentSystemTab || "health");
  }
  if (normTab === "drive" && canAccessDrive) {
    loadDriveDashboard({ lazy: true });
  }
  if (normTab === "albums" && canAccessAlbums) {
    loadAlbumGallery();
  }
  if (normTab === "videos" && canAccessVideos && typeof loadVideoPlatform === "function") {
    loadVideoPlatform();
  }
  if (normTab === "games" && canAccessGames && typeof loadGameZone === "function") {
    loadGameZone();
  }
  if (normTab === "experiments" && canAccessExperiments && typeof initExperimentArea === "function") {
    initExperimentArea();
  }
  if (normTab === "jobs" && canAccessJobs) {
    if (typeof startJobCenterPolling === "function") startJobCenterPolling({ immediate: true });
    else if (typeof loadJobCenter === "function") loadJobCenter();
  }
  if (normTab === "shares" && canAccessShareCenter && typeof loadShareCenter === "function") {
    loadShareCenter();
  }
  if (normTab === "comfyui" && canAccessComfyui && typeof loadComfyuiModels === "function") {
    loadComfyuiModels();
    if (typeof refreshComfyuiStatus === "function") refreshComfyuiStatus({ switchAway: true });
  }
  if (normTab === "ai-agent" && canAccessAiAgent && typeof loadAiAgentStatus === "function") {
    loadAiAgentStatus();
  }
  if (normTab === "economy" && canAccessEconomy && typeof loadEconomyDashboard === "function") {
    loadEconomyDashboard();
  }
  if (normTab === "trading" && canAccessTrading && typeof loadTradingDashboard === "function") {
    loadTradingDashboard();
  }
  if (normTab === "appeals" && canAccessAppeals) {
    loadUserAppeals();
  }
  if (normTab === "accounts" && canAccessAccounts) {
    const nextAdminTab = currentAdminTab && canOpenAdminTab(currentAdminTab) ? currentAdminTab : firstAvailableAdminTab();
    if (!$("sec-" + nextAdminTab)) switchAdminTab("users");
    else switchAdminTab(nextAdminTab);
  }
  if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
  if (typeof collapseSidebarAfterMobileNavigation === "function") collapseSidebarAfterMobileNavigation();
}

function switchAdminTab(tab) {
  currentAdminTab = canOpenAdminTab(tab) ? tab : firstAvailableAdminTab();
  ["users","password-resets","violations","governance","member-settings","notices","appeals","reports"].forEach(t => {
    const sec = $("sec-" + t);
    if (sec) sec.classList.toggle("active", t === currentAdminTab);
  });
  ["tab-users","tab-password-resets","tab-violations","tab-governance","tab-member-settings","tab-notices","tab-appeals","tab-reports"].forEach(id => {
    const btn = $(id);
    const tabKey = id.replace(/^tab-/, "");
    if (!btn) return;
    btn.style.display = canOpenAdminTab(tabKey) ? "" : "none";
    btn.classList.toggle("active", id === "tab-" + currentAdminTab);
  });
  if (currentAdminTab === "password-resets") loadPasswordResetReviews();
  if (currentAdminTab === "users") loadUsers();
  if (currentAdminTab === "violations") loadViolations(0);
  if (currentAdminTab === "governance") loadGovernanceDashboard();
  if (currentAdminTab === "member-settings") {
    loadSettings();
    if (typeof loadEditableMemberLevelRules === "function") loadEditableMemberLevelRules();
  }
  if (currentAdminTab === "notices") {
    loadUsers();
    renderAdminNoticeTargetOptions();
  }
  if (currentAdminTab === "appeals") loadAdminAppeals(1, adminAppealStatus);
  if (currentAdminTab === "reports") loadAdminReports(0, adminReportStatus);
  if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
}

const ADMIN_NOTICE_TEMPLATES = {
  custom: { title: "", body: "" },
  sanction: { title: "會員權益變更通知", body: "你的帳號權益已被調整，若有疑問請到申覆分頁提出申覆。" },
  points: { title: "積分異動通知", body: "你的積分已發生異動，請到積分錢包查看明細。" },
  account: { title: "帳號狀態通知", body: "你的帳號狀態已有更新，請確認個人資訊與系統通知。" },
};

function renderAdminNoticeTargetOptions() {
  const select = $("admin-notice-user-id");
  if (!select) return;
  const selectable = (Array.isArray(users) ? users : []).filter((user) => {
    if (!user || !user.id) return false;
    if (currentUser !== "root" && clientRoleRank(user.role || "user") >= clientRoleRank(currentRole || "user")) return false;
    return true;
  });
  select.innerHTML = selectable.length
    ? selectable.map((user) => {
        const marks = [user.is_friend ? "好友" : "", user.is_official ? "官方/管理者" : ""].filter(Boolean);
        const suffix = marks.length ? ` · ${marks.join(" · ")}` : "";
        return `<option value="${user.id}">${sanitize(user.username || "-")}（#${user.id}）${sanitize(suffix)}</option>`;
      }).join("")
    : `<option value="">沒有可發送通知的成員</option>`;
}

function applyAdminNoticeTemplate() {
  const key = $("admin-notice-template")?.value || "custom";
  const template = ADMIN_NOTICE_TEMPLATES[key] || ADMIN_NOTICE_TEMPLATES.custom;
  const title = $("admin-notice-title");
  const body = $("admin-notice-body");
  if (title && template.title) title.value = template.title;
  if (body && template.body) body.value = template.body;
}

async function sendAdminNotice() {
  const userId = $("admin-notice-user-id")?.value || "";
  const title = ($("admin-notice-title")?.value || "").trim();
  const body = ($("admin-notice-body")?.value || "").trim();
  const msg = $("admin-notice-msg");
  if (!userId || !title || !body) {
    flash(msg, "請選擇成員並填寫標題、內容", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/notifications/send", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ user_id: userId, title, body })
  });
  const json = await res.json().catch(() => ({}));
  flash(msg, json.msg || (json.ok ? "通知已發送" : "通知發送失敗"), !!json.ok);
  if (json.ok && $("admin-notice-body")) $("admin-notice-body").value = "";
}

// ── Audit log ───────────────────────────────────────────────
let auditPage = 0;
const AUDIT_PAGE_SIZE = 20;
let serverOutputPollTimer = null;

async function loadAudit(page) {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/audit?page=" + page, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    const statusEl = $("audit-chain-action-status");
    if (statusEl) statusEl.textContent = json.msg || "審計記錄讀取失敗";
    const container = $("audit-entries");
    if (container) container.innerHTML = `<p style='color:var(--red);text-align:center;padding:1rem;'>${sanitize(json.msg || "審計記錄讀取失敗")}</p>`;
    return;
  }
  auditPage = page;
  $("audit-total").textContent = json.total || 0;
  const integrity = json.integrity;
  const integEl = $("audit-integrity");
  if (integEl) {
    if (integrity && integrity.enabled === false) {
      integEl.textContent = "審計鏈檢查已停用";
      integEl.style.color = "var(--muted)";
    } else {
      const chainOk = integrity && integrity.ok;
      integEl.textContent = chainOk ? "🔗 鏈完整" : "⚠️ 鏈已斷！";
      integEl.style.color = chainOk ? "#4caf50" : "#ff4f6d";
    }
  }
  if (currentRole === "super_admin" && integrity && integrity.enabled !== false && integrity.ok === false && integrity.broken_at) {
    alert(`審計紀錄異常：hash chain 在 #${integrity.broken_at} 斷裂，請立即檢查。`);
  }
  const container = $("audit-entries");
  if (!container) return;
  if (!json.entries || json.entries.length === 0) {
    container.innerHTML = "<p style='color:var(--muted);text-align:center;padding:1rem;'>暫無審計記錄</p>";
    return;
  }
  container.innerHTML = json.entries.map(e => {
    const isObj = typeof e === "object";
    const ts = isObj ? e.timestamp || "" : "";
    const action = isObj ? e.action || "" : e;
    const actor = isObj ? e.actor || "" : "";
    const detail = isObj && e.details ? (typeof e.details === "string" ? e.details : JSON.stringify(e.details)) : "";
    const chain = isObj && e._chain_hash ? `<span style="color:#4caf50;">█</span>` : "·";
    const isBroken = integrity && integrity.broken_at && Number(e.id) === Number(integrity.broken_at);
    const isFailure = isObj && e.success === false;
    const rowStyle = isBroken
      ? "background:rgba(255,79,109,.22);border:1px solid rgba(255,79,109,.45);"
      : isFailure
        ? "background:rgba(255,79,109,.08);"
        : "";
    const badge = isBroken ? `<span style="color:#ff4f6d;font-weight:bold;">審計異常</span> ` : "";
    return `<div style="border-bottom:1px solid #222;padding:.35rem .25rem;word-break:break-all;${rowStyle}">
      <span style="color:#888;">${ts}</span> ${chain}
      ${badge}
      <span style="color:#e0e0e0;">${sanitize(action)}</span>
      ${actor ? `<span style="color:#82b1ff;"> by ${sanitize(actor)}</span>` : ""}
      ${detail ? `<span style="color:#888;font-size:.68rem;"> ${sanitize(detail)}</span>` : ""}
    </div>`;
  }).join("");
  $("audit-prev").disabled = page === 0;
  $("audit-next").disabled = (page + 1) * AUDIT_PAGE_SIZE >= (json.total || 0);
}

// ── Violations ──────────────────────────────────────────────
let violationsPage = 0;
const VIOLATIONS_PAGE_SIZE = 20;
let violationTargetUser = null;

function renderViolationUserSelect(list = [], selectedUsername = "") {
  const select = $("violation-user-select");
  if (!select) return;
  const previous = String(selectedUsername || select.value || "");
  const rows = Array.isArray(list) ? list : [];
  const options = rows
    .filter((u) => Number(u.violation_count || 0) > 0 || String(u.username || "") === previous)
    .map((u) => {
      const username = String(u.username || "");
      const count = Number(u.violation_count || 0);
      return `<option value="${sanitize(username)}">${sanitize(username)} · ${count} 點</option>`;
    }).join("");
  select.innerHTML = `<option value="">選擇帳號查看違規原因</option>${options}`;
  if (previous && rows.some((u) => String(u.username || "") === previous)) select.value = previous;
}

async function loadViolations(page, username) {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const selectedUsername = String(username || "").trim();
  const url = selectedUsername
    ? API + "/admin/violations?page=" + page + "&username=" + encodeURIComponent(selectedUsername)
    : API + "/admin/violations?page=0&summary_only=1";
  const res = await fetch(url, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    const usersEl = $("violation-users");
    const entriesEl = $("violation-entries");
    const message = json.msg || "違規記錄讀取失敗";
    if (usersEl) usersEl.innerHTML = "";
    if (entriesEl) entriesEl.innerHTML = `<p style='color:var(--red);text-align:center;padding:1rem;'>${sanitize(message)}</p>`;
    return;
  }
  violationsPage = page;
  $("violations-total").textContent = json.total || 0;
  const integEl = $("violations-integrity");
  if (integEl) {
    const chainOk = json.integrity === true || (json.integrity && json.integrity.ok === true);
    const chainBad = json.integrity === false || (json.integrity && json.integrity.ok === false);
    integEl.textContent = chainOk ? "🔗 鏈完整" : chainBad ? "⚠️ 鏈已斷！" : "";
    integEl.style.color = chainOk ? "#4caf50" : chainBad ? "#ff4f6d" : "var(--muted)";
  }

  // User pills
  const usersEl = $("violation-users");
  renderViolationUserSelect(json.users || [], selectedUsername);
  if (usersEl) {
    if (selectedUsername) {
      const found = (json.users || []).find((u) => String(u.username || "") === selectedUsername);
      const count = Number(found?.violation_count || 0);
      usersEl.textContent = `目前查看：${selectedUsername} · 違規計點 ${count}`;
    } else {
      const candidateCount = (json.users || []).filter((u) => Number(u.violation_count || 0) > 0).length;
      usersEl.textContent = candidateCount
        ? `共有 ${candidateCount} 個帳號有違規點數，請從下拉選單選擇帳號查看原因。`
        : "目前沒有帳號累積違規點數。";
    }
    violationTargetUser = selectedUsername || null;
  }
  renderAdminViolationFines(json.fines || [], json.fine_total || 0, json.fine_appeals || []);

  const container = $("violation-entries");
  if (!container) return;
  if (!selectedUsername) {
    container.innerHTML = "<p style='color:var(--muted);text-align:center;padding:1rem;'>請先選擇帳號，才會載入個別違規原因。</p>";
    $("violations-prev").disabled = true;
    $("violations-next").disabled = true;
    return;
  }
  if (!json.entries || json.entries.length === 0) {
    container.innerHTML = "<p style='color:var(--muted);text-align:center;padding:1rem;'>暫無違規記錄</p>";
    $("violations-prev").disabled = page === 0;
    $("violations-next").disabled = true;
    return;
  }
  container.innerHTML = json.entries.map(e => {
    const isObj = typeof e === "object";
    const ts = isObj ? e.timestamp || "" : "";
    const reason = isObj ? e.reason || "" : String(e);
    const username = isObj ? e.username || "" : "";
    const actor = isObj ? e.actor || "" : "";
    const points = isObj ? e.points || 0 : 0;
    const chain = isObj && e._chain_hash ? `<span style="color:#4caf50;">█</span>` : "·";
    const fineBtn = isObj && e.user_id && e.id
      ? `<button class="btn" type="button" data-create-violation-fine="${e.id}" data-fine-user-id="${e.user_id}" style="margin-left:.45rem;padding:.25rem .5rem;">建立罰單</button>`
      : "";
    return `<div style="border-bottom:1px solid #222;padding:.35rem .25rem;word-break:break-all;">
      <span style="color:#888;">${ts}</span> ${chain}
      ${username ? `<span style="color:#e0e0e0;">${sanitize(username)}</span>` : ""}
      <span style="color:#ff8a80;">${sanitize(reason)}</span>
      ${points ? `<span style="color:#bbb;"> +${points}</span>` : ""}
      ${actor ? `<span style="color:#82b1ff;"> by ${sanitize(actor)}</span>` : ""}
      ${fineBtn}
    </div>`;
  }).join("");
  container.querySelectorAll("button[data-create-violation-fine]").forEach((btn) => {
    btn.addEventListener("click", () => createManualViolationFine(
      parseInt(btn.getAttribute("data-fine-user-id"), 10),
      parseInt(btn.getAttribute("data-create-violation-fine"), 10)
    ));
  });
  $("violations-prev").disabled = page === 0;
  $("violations-next").disabled = (page + 1) * VIOLATIONS_PAGE_SIZE >= (json.total || 0);
}

async function addViolation(userId) {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const reason = prompt("輸入違規原因：");
  if (!reason) return;
  const res = await apiFetch(API + "/admin/users/" + userId + "/violation", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ reason })
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    loadViolations(0, violationTargetUser);
    loadUsers();
  } else {
    alert(json.msg || "新增違規失敗");
  }
}

async function resetViolations(userId) {
  if (!confirm("確定要歸零該用戶違規次數？")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/users/" + userId + "/reset-violations", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    loadViolations(0, violationTargetUser);
    loadUsers();
  } else {
    alert(json.msg || "歸零失敗");
  }
}

function renderAdminViolationFines(fines, total, fineAppeals) {
  const totalEl = $("violation-fines-total");
  if (totalEl) totalEl.textContent = Number(total || 0).toLocaleString();
  const list = $("violation-fine-list");
  if (list) {
    const rows = Array.isArray(fines) ? fines : [];
    if (!rows.length) {
      list.innerHTML = "<p style='color:var(--muted);text-align:center;padding:.75rem;'>目前沒有符合條件的罰單</p>";
    } else {
      list.innerHTML = rows.map((fine) => {
        const status = String(fine.status || "");
        const color = typeof violationFineStatusColor === "function" ? violationFineStatusColor(status) : "var(--muted)";
        const label = typeof violationFineStatusLabel === "function" ? violationFineStatusLabel(status) : status;
        const featureLabels = Array.isArray(fine.restriction_feature_labels) ? fine.restriction_feature_labels.join("、") : "";
        const baseAmount = Number(fine.amount_points || 0);
        const interest = Number(fine.overdue_interest_points || 0);
        const dueAmount = Number(fine.amount_due_points || baseAmount + interest);
        const closable = status === "pending" || status === "overdue";
        return `
          <div style="border-bottom:1px solid #222;padding:.45rem .25rem;word-break:break-all;">
            <div><strong>${sanitize(fine.username || "")}</strong> · <span style="color:${color};">${sanitize(label)}</span> · ${dueAmount.toLocaleString()} 點</div>
            ${interest > 0 ? `<div style="color:#ff8a80;font-size:.7rem;">本金 ${baseAmount.toLocaleString()} · 逾期附加費 ${interest.toLocaleString()}</div>` : ""}
            <div style="color:#ffcc80;">${sanitize(fine.reason || "")}</div>
            <div style="color:var(--muted);font-size:.7rem;">${sanitize(fine.fine_uuid || "")} · due ${sanitize(fine.due_at || "-")} · 限制 ${sanitize(featureLabels || "-")}</div>
            ${fine.payment_ledger_uuid ? `<div style="color:#4caf50;font-size:.7rem;">ledger ${sanitize(fine.payment_ledger_uuid || "")}</div>` : ""}
            ${closable ? `<button class="btn" type="button" data-waive-fine="${sanitize(fine.fine_uuid)}" style="margin-top:.35rem;">豁免 / 解除限制</button>` : ""}
          </div>
        `;
      }).join("");
      list.querySelectorAll("button[data-waive-fine]").forEach((btn) => {
        btn.addEventListener("click", () => waiveViolationFine(btn.getAttribute("data-waive-fine")));
      });
    }
  }
  const appealsList = $("violation-fine-appeal-list");
  if (!appealsList) return;
  const appeals = Array.isArray(fineAppeals) ? fineAppeals : [];
  if (!appeals.length) {
    appealsList.innerHTML = "<p style='color:var(--muted);'>目前沒有待審罰單申覆</p>";
    return;
  }
  appealsList.innerHTML = `
    <div style="color:var(--muted);font-size:.74rem;margin-bottom:.3rem;">待審罰單申覆</div>
    ${appeals.map((appeal) => `
      <div style="border-bottom:1px solid #222;padding:.45rem .25rem;word-break:break-all;">
        <div><strong>${sanitize(appeal.username || "")}</strong> · ${Number(appeal.amount_points || 0).toLocaleString()} 點 · 罰單 ${sanitize(String(appeal.fine_uuid || "").slice(0, 24))}</div>
        <div>申覆理由：${sanitize(appeal.reason || "")}</div>
        <div style="color:var(--muted);font-size:.7rem;">罰單原因：${sanitize(appeal.fine_reason || "")}</div>
        <div style="display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.35rem;">
          <button class="btn" type="button" data-fine-appeal-review="approve" data-fine-appeal-id="${appeal.id}" style="background:#1f9d57;color:#fff;border-color:#1f9d57;">核准豁免</button>
          <button class="btn" type="button" data-fine-appeal-review="reject" data-fine-appeal-id="${appeal.id}" style="background:#ff5252;color:#fff;border-color:#ff5252;">駁回</button>
        </div>
      </div>
    `).join("")}
  `;
  appealsList.querySelectorAll("button[data-fine-appeal-review]").forEach((btn) => {
    btn.addEventListener("click", () => reviewViolationFineAppeal(
      parseInt(btn.getAttribute("data-fine-appeal-id"), 10),
      btn.getAttribute("data-fine-appeal-review")
    ));
  });
}

async function loadAdminViolationFines() {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const status = $("violation-fine-status")?.value || "";
  const username = violationTargetUser || "";
  const qs = new URLSearchParams();
  if (status) qs.set("status", status);
  if (username) qs.set("username", username);
  qs.set("limit", "50");
  const res = await apiFetch(API + "/admin/violation-fines?" + qs.toString(), {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" },
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    renderAdminViolationFines([], 0, []);
    alert(json.msg || "罰單清單讀取失敗");
    return;
  }
  renderAdminViolationFines(json.fines || [], json.total || 0, json.pending_appeals || []);
}

async function createManualViolationFine(userId, violationId = null) {
  const amountRaw = prompt("罰款點數", "300");
  if (amountRaw === null) return;
  const amount = Math.max(1, parseInt(amountRaw, 10) || 0);
  if (!amount) {
    alert("罰款點數格式錯誤");
    return;
  }
  const reason = prompt("罰單原因", violationId ? `針對違規 #${violationId} 建立罰單` : "違規累計罰單");
  if (reason === null) return;
  const cleanReason = String(reason || "").trim();
  if (cleanReason.length < 6) {
    alert("罰單原因至少 6 字");
    return;
  }
  const featuresRaw = prompt("逾期限制功能（逗號分隔，可留空使用預設）", "community_post,community_comment,chat_dm,cloud_upload,video_publish,trading_order,service_spend");
  if (featuresRaw === null) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/users/" + parseInt(userId, 10) + "/violation-fines", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({
      amount_points: amount,
      reason: cleanReason,
      violation_id: violationId || null,
      restriction_features: String(featuresRaw || "").split(",").map((item) => item.trim()).filter(Boolean),
    }),
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    alert("罰單已建立");
    await loadAdminViolationFines();
  } else {
    alert(json.msg || "建立罰單失敗");
  }
}

async function waiveViolationFine(fineUuid) {
  const reason = prompt("豁免原因 / 審核備註", "");
  if (reason === null) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/violation-fines/" + encodeURIComponent(fineUuid) + "/waive", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ reason: String(reason || "").trim() }),
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) await loadAdminViolationFines();
  else alert(json.msg || "豁免罰單失敗");
}

async function reviewViolationFineAppeal(appealId, action) {
  const note = prompt("審核備註（非必填）", "");
  if (note === null) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/violation-fine-appeals/" + parseInt(appealId, 10) + "/review", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ action, note: String(note || "").trim() }),
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) await loadAdminViolationFines();
  else alert(json.msg || "審核罰單申覆失敗");
}

// ── Governance UI ───────────────────────────────────────────
let governancePendingTargetUserId = "";
const GOVERNANCE_ACTION_VALUE_HELP = {
  warn: "可留空。系統會依提案原因替對象記一次違規警告。",
  mute: "禁言期限請在下方填寫；通過後會限制聊天、私訊、發文與留言。",
  restrict: "請在下方選擇要限制的功能；可另外設定處分期限。",
  suspend: "可在下方設定暫停期限；留空代表需另行治理解除。",
  downgrade_level: "必填：newbie、normal、restricted 或 suspended。用來調整會員等級。",
  force_password_reset: "可留空。通過後對象下次登入必須重新設定密碼。",
  delete: "可留空。通過後帳號會被標記為 deleted，屬高風險操作。",
};
const GOVERNANCE_HIGH_RISK_ACTIONS = new Set(["suspend", "delete", "downgrade_level"]);
const GOVERNANCE_EMERGENCY_ACTIONS = new Set(["mute", "restrict", "suspend", "force_password_reset"]);
const GOVERNANCE_FEATURE_LABELS = {
  community_post: "討論區發文",
  community_comment: "留言 / 回覆",
  chat_send: "聊天發言",
  chat_dm: "私訊",
  cloud_upload: "雲端上傳",
  video_publish: "影音發布",
  trading_order: "交易所下單",
  service_spend: "站內付費功能",
  wallet_transfer: "錢包轉出",
};

async function loadGovernanceDashboard() {
  await Promise.allSettled([loadUsers(), loadMemberLevelRulesSummary(), loadGovernanceProposals()]);
  renderGovernanceTargetOptions();
  updateGovernanceActionValueHelp();
}

function renderGovernanceTargetOptions(selectedValue = null) {
  const select = $("governance-target-user-id");
  if (!select) return;
  const previous = selectedValue === null
    ? String(select.value || governancePendingTargetUserId || "")
    : String(selectedValue || "");
  const rows = Array.isArray(users) ? users : [];
  if (!rows.length) {
    select.innerHTML = `<option value="">無法讀取會員清單</option>`;
    return;
  }
  const targetRows = rows.filter((user) => user.username !== "root" && String(user.id || "") !== String(currentUserId || ""));
  select.innerHTML = `<option value="">請選擇治理目標</option>` + targetRows.map((user) => {
    const id = String(user.id || "");
    const role = user.username === "root" ? "root" : (user.role || "user");
    const status = user.status || "-";
    const level = user.effective_level || user.member_level || "";
    const relation = [user.is_friend ? "好友" : "", user.is_official ? "官方/管理者" : ""].filter(Boolean).join(" · ");
    const label = `${user.username || "unknown"} (#${id}) · ${role} · ${status}${level ? " · " + level : ""}${relation ? " · " + relation : ""}`;
    return `<option value="${sanitize(id)}">${sanitize(label)}</option>`;
  }).join("");
  if (previous && targetRows.some((user) => String(user.id || "") === previous)) {
    select.value = previous;
    governancePendingTargetUserId = "";
  }
}

function selectedGovernanceTarget() {
  const targetId = String($("governance-target-user-id")?.value || "");
  return (Array.isArray(users) ? users : []).find((user) => String(user.id || "") === targetId) || null;
}

function governancePolicySummary(action, target) {
  const targetRole = target?.role || "user";
  const highRisk = GOVERNANCE_HIGH_RISK_ACTIONS.has(action) || targetRole === "manager" || targetRole === "super_admin";
  return highRisk
    ? "高風險：需要 root 同意，且另外需要 2 位 admin/manager 同意。通過後必須由 root 執行。"
    : "一般：需要 1 位 admin/manager 或 root 同意。";
}

function selectedGovernanceRestrictionFeatures() {
  return Array.from(document.querySelectorAll("#governance-restriction-features input[type='checkbox']:checked"))
    .map((input) => input.value)
    .filter(Boolean);
}

function updateGovernanceActionValueHelp() {
  const action = $("governance-action-type")?.value || "warn";
  const input = $("governance-action-value");
  const help = $("governance-action-value-help");
  const policy = $("governance-vote-policy");
  const durationField = $("governance-duration-field");
  const durationLabel = $("governance-duration-label");
  const restrictionField = $("governance-restriction-features-field");
  const emergency = $("governance-emergency-execute");
  const ttl = $("governance-ttl-hours");
  const text = GOVERNANCE_ACTION_VALUE_HELP[action] || "依處理方式填寫；不需要額外參數時可留空。";
  if (help) help.textContent = text;
  if (policy) policy.textContent = governancePolicySummary(action, selectedGovernanceTarget());
  const showDuration = ["mute", "restrict", "suspend"].includes(action);
  if (durationField) durationField.style.display = showDuration ? "" : "none";
  if (durationLabel) {
    durationLabel.textContent = action === "mute" ? "禁言多久（小時）" : action === "restrict" ? "功能限制期限（小時）" : "暫停帳號期限（小時）";
  }
  if (restrictionField) restrictionField.style.display = action === "restrict" ? "" : "none";
  if (emergency) {
    emergency.disabled = !GOVERNANCE_EMERGENCY_ACTIONS.has(action);
    if (emergency.disabled) emergency.checked = false;
  }
  if (ttl) {
    if (emergency?.checked) {
      ttl.value = "1";
      ttl.disabled = true;
    } else {
      ttl.disabled = false;
    }
  }
  if (input) {
    input.placeholder = action === "downgrade_level"
      ? "newbie / normal / restricted / suspended"
      : action === "suspend"
        ? "可留空；期限請用下方小時欄位"
        : "通常可留空";
    input.disabled = ["mute", "restrict"].includes(action);
    if (input.disabled) input.value = "";
  }
}

async function loadMemberLevelRulesSummary() {
  const container = $("member-level-rules-list");
  if (!container) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/member-level-rules", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    container.innerHTML = `<div style="color:#ffb74d;">${sanitize(json.msg || "會員規則讀取失敗或功能尚未啟用")}</div>`;
    return;
  }
  const rules = Array.isArray(json.rules) ? json.rules : [];
  container.innerHTML = rules.map((r) => {
    const level = r.level || "";
    const flags = [
      r.can_post ? "發文" : "禁發文",
      r.can_comment ? "留言" : "禁留言",
      r.can_send_dm ? "私訊" : "禁私訊",
      r.can_upload_attachment ? "上傳" : "禁上傳",
      r.requires_moderation ? "需審核" : "免審"
    ].join(" · ");
    return `<div style="border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:.6rem;background:rgba(0,0,0,.2);">
      <div style="font-weight:700;color:#e0e0f0;">${sanitize(level)}</div>
      <div style="color:var(--muted);margin-top:.2rem;">${sanitize(flags)}</div>
      <div style="color:#82b1ff;margin-top:.25rem;">post ${r.daily_post_limit ?? "-"} · upload ${r.attachment_quota_mb ?? 0} MB · report weight ${r.report_weight ?? "-"}</div>
    </div>`;
  }).join("");
}

async function loadGovernanceProposals() {
  const list = $("governance-proposal-list");
  const msg = $("governance-msg");
  if (!list) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const status = $("governance-proposal-status")?.value || "";
  const url = API + "/admin/moderation/proposals" + (status ? "?status=" + encodeURIComponent(status) : "");
  const res = await fetch(url, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    list.innerHTML = `<div style="color:#ff4f6d;">${sanitize(json.msg || "治理提案讀取失敗")}</div>`;
    if (msg) msg.textContent = "";
    return;
  }
  const proposals = Array.isArray(json.proposals) ? json.proposals : [];
  if (msg) msg.textContent = `共 ${proposals.length} 筆`;
  if (!proposals.length) {
    list.innerHTML = "<p style='color:var(--muted);text-align:center;padding:1rem;'>目前沒有治理提案</p>";
    return;
  }
  list.innerHTML = proposals.map((p) => {
    const target = p.target?.username || `#${p.target_user_id}`;
    const proposer = p.proposed_by?.username || `#${p.proposed_by_user_id}`;
    const votes = (p.votes || []).map(v => `${sanitize(v.voter_username || "")}:${sanitize(v.vote || "")}`).join(" · ") || "尚無投票";
    const policyText = p.policy_summary || (
      p.required_root_approval
        ? "高風險：需要 root 同意，且另外需要 2 位 admin/manager 同意。"
        : "一般：需要 1 位 admin/manager 或 root 同意。"
    );
    const progressText = p.required_root_approval
      ? `root ${p.root_requirement_met ? "已同意" : "未同意"} · admin/manager ${p.manager_approve_count || 0}/${p.required_manager_approvals || 2} · reject ${p.reject_count || 0}`
      : `${p.approve_count || 0}/${p.required_votes || 1} approve · ${p.reject_count || 0} reject`;
    const payload = p.action_payload || {};
    const details = [];
    if (p.is_emergency) details.push(`緊急處分${p.emergency_applied_at ? "已先行套用" : ""}${p.emergency_reverted_at ? " · 已解除" : ""}`);
    if (payload.duration_hours) details.push(`期限 ${payload.duration_hours} 小時${payload.expires_at ? ` · 到期 ${payload.expires_at}` : ""}`);
    const featureKeys = payload.restriction_features || payload.mute_features || [];
    if (featureKeys.length) details.push(`限制功能：${featureKeys.map((key) => GOVERNANCE_FEATURE_LABELS[key] || key).join("、")}`);
    const canVote = p.status === "pending";
    const canExecute = p.status === "approved";
    return `<div style="border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:.65rem;margin-bottom:.55rem;background:rgba(0,0,0,.22);">
      <div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;">
        <strong>#${p.id}</strong>
        <span style="color:#82b1ff;">${sanitize(p.action_type || "")}</span>
        <span>target=${sanitize(target)}</span>
        <span style="color:${p.risk_level === "high" ? "#ffb74d" : "#82b1ff"};">${p.risk_level === "high" ? "高風險" : "一般"}</span>
        <span style="color:${p.status === "approved" ? "#4caf50" : p.status === "rejected" ? "#ff4f6d" : "#ffb74d"};">${sanitize(p.status || "")}</span>
        <span style="margin-left:auto;color:var(--muted);">${sanitize(progressText)}</span>
      </div>
      <div style="color:var(--muted);margin-top:.25rem;">proposer=${sanitize(proposer)} · expires=${sanitize(p.expires_at || "")}</div>
      <div style="color:#82b1ff;margin-top:.25rem;">${sanitize(policyText)}</div>
      ${details.length ? `<div style="color:#ffb74d;margin-top:.25rem;">${sanitize(details.join(" · "))}</div>` : ""}
      <div style="margin-top:.35rem;white-space:pre-wrap;">${sanitize(p.reason || "")}</div>
      <div style="color:var(--muted);margin-top:.35rem;">votes: ${votes}</div>
      <div class="admin-toolbar" style="display:flex;gap:.45rem;margin-top:.5rem;">
        ${canVote ? `<button class="btn btn-primary" data-governance-vote="approve" data-proposal-id="${p.id}">同意</button><button class="btn" data-governance-vote="reject" data-proposal-id="${p.id}">否決</button>` : ""}
        ${canExecute ? `<button class="btn btn-primary" data-governance-execute="${p.id}">執行</button>` : ""}
      </div>
    </div>`;
  }).join("");
  list.querySelectorAll("button[data-governance-vote]").forEach((btn) => {
    btn.addEventListener("click", () => voteGovernanceProposal(btn.getAttribute("data-proposal-id"), btn.getAttribute("data-governance-vote")));
  });
  list.querySelectorAll("button[data-governance-execute]").forEach((btn) => {
    btn.addEventListener("click", () => executeGovernanceProposal(btn.getAttribute("data-governance-execute")));
  });
}

async function createGovernanceProposal() {
  const targetId = parseInt($("governance-target-user-id")?.value || "0", 10);
  const reason = ($("governance-reason")?.value || "").trim();
  if (!targetId || !reason) {
    alert("請選擇治理目標並填寫提案原因");
    return;
  }
  if (String(targetId) === String(currentUserId || "")) {
    alert("不能對自己建立治理提案");
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const actionType = $("governance-action-type")?.value || "warn";
  const emergency = Boolean($("governance-emergency-execute")?.checked);
  const durationHoursRaw = ($("governance-duration-hours")?.value || "").trim();
  const restrictionFeatures = selectedGovernanceRestrictionFeatures();
  if (actionType === "restrict" && !restrictionFeatures.length) {
    alert("限制功能提案必須選擇至少一個功能");
    return;
  }
  if (actionType === "mute" && !durationHoursRaw) {
    alert("禁言提案必須設定禁言多久");
    return;
  }
  if (emergency && !GOVERNANCE_EMERGENCY_ACTIONS.has(actionType)) {
    alert("此治理動作不支援緊急執行");
    return;
  }
  const payload = {
    target_user_id: targetId,
    action_type: actionType,
    action_value: ($("governance-action-value")?.value || "").trim() || null,
    ttl_hours: parseInt($("governance-ttl-hours")?.value || "72", 10),
    reason,
    emergency_execute: emergency,
    duration_hours: durationHoursRaw ? parseInt(durationHoursRaw, 10) : null,
    restriction_features: restrictionFeatures
  };
  const res = await apiFetch(API + "/admin/moderation/proposals", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(payload)
  });
  const json = await res.json().catch(() => ({}));
  alert(json.msg || (json.ok ? "治理提案已建立" : "治理提案建立失敗"));
  if (json.ok) {
    if ($("governance-reason")) $("governance-reason").value = "";
    await loadGovernanceProposals();
  }
}

async function voteGovernanceProposal(proposalId, vote) {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const comment = prompt("投票備註（可空白）") || "";
  const res = await apiFetch(API + `/admin/moderation/proposals/${proposalId}/vote`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ vote, comment })
  });
  const json = await res.json().catch(() => ({}));
  alert(json.msg || (json.ok ? "已完成投票" : "投票失敗"));
  await loadGovernanceProposals();
}

async function executeGovernanceProposal(proposalId) {
  if (!confirm("確定執行已通過的治理提案？")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/admin/moderation/proposals/${proposalId}/execute`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  alert(json.msg || (json.ok ? "治理提案已執行" : "執行失敗"));
  await Promise.all([loadGovernanceProposals(), loadUsers()]);
}

function openGovernanceProposalForUser(userId, username) {
  if (String(userId || "") === String(currentUserId || "")) {
    alert("不能對自己建立治理提案");
    return;
  }
  switchAdminTab("governance");
  governancePendingTargetUserId = String(userId || "");
  renderGovernanceTargetOptions(userId);
  if ($("governance-target-user-id")) $("governance-target-user-id").value = userId;
  if ($("governance-reason")) $("governance-reason").value = `針對 ${username || "user #" + userId} 建立治理提案：`;
  updateGovernanceActionValueHelp();
}

function passwordResetReviewSetMsg(text, ok = true) {
  const msg = $("password-reset-review-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
}

async function loadPasswordResetReviews() {
  const list = $("password-reset-review-list");
  if (!list) return;
  const status = $("password-reset-review-status")?.value || "pending";
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/password-reset-requests?status=" + encodeURIComponent(status), {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    list.innerHTML = "";
    passwordResetReviewSetMsg(json.msg || "密碼重設申請讀取失敗", false);
    return;
  }
  const rows = Array.isArray(json.requests) ? json.requests : [];
  if (!rows.length) {
    list.innerHTML = `<p style="color:var(--muted);">目前沒有密碼重設申請</p>`;
    passwordResetReviewSetMsg("", true);
    return;
  }
  list.innerHTML = rows.map((item) => `
    <div class="admin-card" style="margin-bottom:.65rem;">
      <div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;">
        <strong>#${Number(item.id || 0)}</strong>
        <span>${sanitize(item.username || "-")}</span>
        <span style="color:var(--muted);">${sanitize(item.role || "-")} · ${sanitize(item.target_status || "-")}</span>
        <span style="color:${item.status === "pending" ? "#ffb74d" : item.status === "approved" ? "#4caf50" : "#ff4f6d"};">${sanitize(item.status || "")}</span>
        <span style="margin-left:auto;color:var(--muted);">${sanitize(item.created_at || "")}</span>
      </div>
      <div style="color:var(--muted);font-size:.78rem;margin-top:.25rem;">IP: ${sanitize(item.requested_ip || "-")} · reviewed_by: ${sanitize(item.reviewed_by || "-")}</div>
      ${item.review_note ? `<div style="margin-top:.35rem;color:var(--muted);">${sanitize(item.review_note)}</div>` : ""}
      ${item.can_review ? `
        <div class="settings-option-grid" style="margin-top:.6rem;">
          <div class="field">
            <label>臨時密碼</label>
            <input type="password" data-reset-review-pass="${Number(item.id || 0)}" autocomplete="new-password" />
          </div>
          <div class="field">
            <label>確認臨時密碼</label>
            <input type="password" data-reset-review-pass-confirm="${Number(item.id || 0)}" autocomplete="new-password" />
          </div>
          <div class="field">
            <label>審核備註</label>
            <input type="text" data-reset-review-note="${Number(item.id || 0)}" placeholder="可填處理原因或交付方式" />
          </div>
          <div class="field">
            <label>&nbsp;</label>
            <div style="display:flex;gap:.45rem;">
              <button class="btn btn-primary" type="button" data-reset-review-approve="${Number(item.id || 0)}">通過</button>
              <button class="btn" type="button" data-reset-review-reject="${Number(item.id || 0)}">駁回</button>
            </div>
          </div>
        </div>
      ` : ""}
    </div>
  `).join("");
  list.querySelectorAll("[data-reset-review-approve]").forEach((btn) => {
    btn.addEventListener("click", () => approvePasswordResetReview(btn.getAttribute("data-reset-review-approve")));
  });
  list.querySelectorAll("[data-reset-review-reject]").forEach((btn) => {
    btn.addEventListener("click", () => rejectPasswordResetReview(btn.getAttribute("data-reset-review-reject")));
  });
}

async function approvePasswordResetReview(requestId) {
  const proposedPassword = document.querySelector(`[data-reset-review-pass="${CSS.escape(String(requestId))}"]`)?.value || "";
  const passwordConfirm = document.querySelector(`[data-reset-review-pass-confirm="${CSS.escape(String(requestId))}"]`)?.value || "";
  const note = document.querySelector(`[data-reset-review-note="${CSS.escape(String(requestId))}"]`)?.value || "";
  if (!proposedPassword || proposedPassword !== passwordConfirm) {
    passwordResetReviewSetMsg("請輸入一致的臨時密碼", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/admin/password-reset-requests/${encodeURIComponent(requestId)}/approve`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ temporary_password: proposedPassword, temporary_password_confirm: passwordConfirm, note })
  });
  const json = await res.json().catch(() => ({}));
  passwordResetReviewSetMsg(json.msg || (json.ok ? "已通過" : "處理失敗"), !!json.ok);
  if (json.ok) await loadPasswordResetReviews();
}

async function rejectPasswordResetReview(requestId) {
  const note = document.querySelector(`[data-reset-review-note="${CSS.escape(String(requestId))}"]`)?.value || "";
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/admin/password-reset-requests/${encodeURIComponent(requestId)}/reject`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ note })
  });
  const json = await res.json().catch(() => ({}));
  passwordResetReviewSetMsg(json.msg || (json.ok ? "已駁回" : "處理失敗"), !!json.ok);
  if (json.ok) await loadPasswordResetReviews();
}

// ── Settings & restart ───────────────────────────────────────
const MEMBER_LEVEL_BOOL_FIELDS = [
  ["can_post", "可發文"],
  ["can_comment", "可留言"],
  ["can_send_dm", "可私訊"],
  ["can_upload_attachment", "可上傳附件"],
  ["can_report", "可檢舉"],
  ["requires_moderation", "發文需審核"],
  ["require_admin_approval", "升等需 admin 核准"],
  ["require_root_approval", "升等需 root 核准"]
];
const MEMBER_LEVEL_INT_FIELDS = [
  ["daily_post_limit", "每日發文上限"],
  ["daily_dm_limit", "每日私訊上限"],
  ["post_rate_limit_per_hour", "每小時發文限制"],
  ["comment_rate_limit_per_hour", "每小時留言限制"],
  ["dm_rate_limit_per_day", "每日私訊 rate limit"],
  ["upload_rate_limit_per_day", "每日上傳限制"],
  ["max_attachment_size_mb", "單檔上限 MB"],
  ["attachment_quota_mb", "總容量 MB"],
  ["report_weight", "檢舉權重"],
  ["min_account_age_days", "升等帳齡天數"],
  ["min_approved_content_count", "升等核准內容數"],
  ["min_points", "升等點數"],
  ["min_trust_score", "升等 trust_score"],
  ["min_reputation", "升等 reputation"],
  ["max_violation_score", "升等最大違規分"],
  ["downgrade_violation_threshold", "降級/處分建議門檻"],
  ["session_idle_timeout_minutes", "閒置登出分鐘"]
];
let editableMemberLevelRules = [];
let rootStorageUsersCache = [];
const CLOUD_DRIVE_POLICY_BOOL_FIELDS = [
  "require_scan_before_download",
  "block_unclean_downloads",
  "warn_high_risk_downloads",
  "allow_inline_preview_for_high_risk",
  "e2ee_server_scan_claim_allowed",
  "revoke_shares_on_suspension",
  "scanner_enabled",
  "fail_closed_on_scanner_error",
  "quarantine_on_infected",
  "validate_magic_mime",
  "deep_archive_scan_enabled",
  "office_macro_scan_enabled",
  "image_reencode_enabled",
  "yara_enabled"
];
const CLOUD_DRIVE_POLICY_INT_FIELDS = [
  "scanner_timeout_seconds",
  "max_archive_depth",
  "image_reencode_max_pixels",
  "max_archive_files",
  "max_archive_uncompressed_bytes",
  "max_daily_downloads"
];
const CLOUD_DRIVE_POLICY_TEXT_FIELDS = [
  "scanner_backend",
  "scanner_command",
  "yara_command",
  "yara_rules_path"
];

// Root storage, service-fee pricing, and economy catalog handlers live in 53-admin-storage-economy.js.

// Root trading admin handlers are implemented in 52-admin-trading.js.

async function loadEditableMemberLevelRules() {
  if (!currentUser || currentUser !== "root") return;
  const container = $("settings-member-level-rules");
  if (!container) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/member-level-rules", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    container.innerHTML = `<div style="color:#ff4f6d;">${sanitize(json.msg || "會員等級規則讀取失敗")}</div>`;
    return;
  }
  editableMemberLevelRules = Array.isArray(json.rules) ? json.rules : [];
  const selected = $("settings-member-level-select")?.value || editableMemberLevelRules[0]?.level || "normal";
  container.innerHTML = `
    <div class="member-level-toolbar">
      <div class="field">
        <label>會員等級</label>
        <select id="settings-member-level-select">
          ${editableMemberLevelRules.map((rule) => `<option value="${sanitize(rule.level || "")}" ${rule.level === selected ? "selected" : ""}>${sanitize(rule.level || "")}</option>`).join("")}
        </select>
      </div>
      <div class="field">
        <label>操作</label>
        <button class="btn btn-primary" type="button" id="member-level-rule-save-btn">儲存此等級規則</button>
      </div>
    </div>
    <div id="settings-member-level-editor"></div>
  `;
  const select = $("settings-member-level-select");
  if (select) select.addEventListener("change", () => renderSelectedMemberLevelRule(select.value));
  const saveBtn = $("member-level-rule-save-btn");
  if (saveBtn) saveBtn.addEventListener("click", () => saveMemberLevelRule($("settings-member-level-select")?.value || ""));
  renderSelectedMemberLevelRule(selected);
}

function renderSelectedMemberLevelRule(level) {
  const editor = $("settings-member-level-editor");
  if (!editor) return;
  const rule = editableMemberLevelRules.find((item) => item.level === level) || editableMemberLevelRules[0];
  if (!rule) {
    editor.innerHTML = `<div style="color:#ff4f6d;">尚無會員等級規則</div>`;
    return;
  }
  const bools = MEMBER_LEVEL_BOOL_FIELDS.map(([key, label]) => `
      <label class="member-level-toggle" title="${sanitize(label)}">
        <input type="checkbox" data-level="${sanitize(level)}" data-rule-bool="${key}" ${rule[key] ? "checked" : ""} />
        <span>${sanitize(label)}</span>
      </label>
    `).join("");
  const ints = MEMBER_LEVEL_INT_FIELDS.map(([key, label]) => `
      <label class="member-level-number-field" title="${sanitize(label)}">
        <span>${sanitize(label)}</span>
        <input type="number" min="0" data-level="${sanitize(level)}" data-rule-int="${key}" value="${Number(rule[key] || 0)}" />
      </label>
    `).join("");
  editor.innerHTML = `<div class="member-level-editor-card">
      <div class="member-level-editor-head">
        <strong>${sanitize(rule.level || level)}</strong>
        <span>權限開關與限額門檻</span>
      </div>
      <div class="member-level-subtitle">權限開關</div>
      <div class="member-level-toggle-grid">${bools}</div>
      <div class="member-level-subtitle">限額與升降級門檻</div>
      <div class="member-level-number-grid">${ints}</div>
    </div>`;
}

async function saveMemberLevelRule(level) {
  if (!level) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const container = $("settings-member-level-rules");
  const payload = {};
  MEMBER_LEVEL_BOOL_FIELDS.forEach(([key]) => {
    const el = container?.querySelector(`[data-level="${CSS.escape(level)}"][data-rule-bool="${key}"]`);
    if (el) payload[key] = !!el.checked;
  });
  MEMBER_LEVEL_INT_FIELDS.forEach(([key]) => {
    const el = container?.querySelector(`[data-level="${CSS.escape(level)}"][data-rule-int="${key}"]`);
    if (el) payload[key] = parseInt(el.value || "0");
  });
  const res = await apiFetch(API + "/admin/member-level-rules/" + encodeURIComponent(level), {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(payload)
  });
  const json = await res.json().catch(() => ({}));
  const msg = $("member-level-settings-msg");
  if (msg) {
    msg.textContent = json.ok ? `${level} 規則已儲存` : (json.msg || "會員等級規則儲存失敗");
    msg.style.color = json.ok ? "#4caf50" : "#ff4f6d";
  }
  if (json.ok) loadMemberLevelRulesSummary();
}

// Server mode / launch-check handlers are implemented in 51-admin-server-mode-launch-check.js.

let currentComfyuiSettingsFamily = "comfyui";

function getComfyuiSettingsFamily() {
  const active = document.querySelector("[data-comfyui-settings-family].active");
  const family = currentComfyuiSettingsFamily || active?.dataset?.comfyuiSettingsFamily || "comfyui";
  return family === "hf" ? "hf" : "comfyui";
}

function setComfyuiSettingsFamily(family) {
  currentComfyuiSettingsFamily = family === "hf" ? "hf" : "comfyui";
  updateComfyuiConnectionModeFields();
}

function setComfyuiBackendMode(mode) {
  const backendMode = mode === "local" ? "local" : "remote";
  const backendModeSelect = $("s-comfyui-comfyui-backend-mode");
  const modeSelect = $("s-comfyui-connection-mode");
  if (backendModeSelect) backendModeSelect.value = backendMode;
  if (modeSelect) modeSelect.value = backendMode;
  updateComfyuiConnectionModeFields();
}

function updateComfyuiConnectionModeFields() {
  const modeSelect = $("s-comfyui-connection-mode");
  const backendModeSelect = $("s-comfyui-comfyui-backend-mode");
  let mode = modeSelect?.value || backendModeSelect?.value || "remote";
  if (!["remote", "local"].includes(mode)) mode = backendModeSelect?.value === "local" ? "local" : "remote";
  if (modeSelect && modeSelect.value !== mode) modeSelect.value = mode;
  const settingsFamily = getComfyuiSettingsFamily();
  const backendMode = mode === "local" ? "local" : "remote";
  const localBox = $("comfyui-local-settings");
  const remoteBox = $("comfyui-remote-settings");
  const diffusersBox = $("comfyui-diffusers-settings");
  const civitaiBox = $("comfyui-civitai-settings");
  const civitaiInput = $("s-comfyui-civitai-api-key");
  const hfTokenInput = $("s-comfyui-huggingface-api-token");
  const hfTokenClear = $("s-comfyui-huggingface-api-token-clear");
  const allowInProcessDiffusers = $("s-comfyui-allow-in-process-diffusers");
  const lowCpuMemUsage = $("s-comfyui-diffusers-low-cpu-mem-usage");
  const cudaFallbackToCpu = $("s-comfyui-diffusers-cuda-fallback-to-cpu");
  const keepDownloadedModels = $("s-comfyui-diffusers-keep-downloaded-models");
  const disableXet = $("s-comfyui-diffusers-disable-xet");
  const deviceMap = $("s-comfyui-diffusers-device-map");
  const localPerformanceFields = [
    "s-comfyui-local-vram-mode",
    "s-comfyui-local-precision",
    "s-comfyui-local-unet-dtype",
    "s-comfyui-local-vae-dtype",
    "s-comfyui-local-text-encoder-dtype",
    "s-comfyui-local-cpu-vae",
    "s-comfyui-local-attention-mode",
    "s-comfyui-local-upcast-attention",
    "s-comfyui-local-cuda-malloc",
    "s-comfyui-local-disable-smart-memory",
    "s-comfyui-local-deterministic",
    "s-comfyui-local-async-offload",
    "s-comfyui-local-cache-mode",
    "s-comfyui-local-cache-lru",
    "s-comfyui-local-reserve-vram-gb",
  ];
  if (backendModeSelect && backendModeSelect.value !== backendMode) backendModeSelect.value = backendMode;
  document.querySelectorAll("[data-comfyui-settings-family]").forEach((button) => {
    const active = button.dataset.comfyuiSettingsFamily === settingsFamily;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-comfyui-settings-family-panel]").forEach((panel) => {
    const family = panel.dataset.comfyuiSettingsFamilyPanel;
    panel.style.display = family === settingsFamily ? "" : "none";
  });
  if (localBox) localBox.style.display = mode === "local" && settingsFamily === "comfyui" ? "" : "none";
  if (remoteBox) remoteBox.style.display = mode === "remote" && settingsFamily === "comfyui" ? "" : "none";
  if (diffusersBox) diffusersBox.style.display = settingsFamily === "hf" ? "" : "none";
  if (civitaiBox) civitaiBox.style.display = settingsFamily === "comfyui" ? "" : "none";
  if (civitaiInput) civitaiInput.disabled = settingsFamily !== "comfyui";
  if (hfTokenInput) hfTokenInput.disabled = settingsFamily !== "hf";
  if (hfTokenClear) hfTokenClear.disabled = settingsFamily !== "hf";
  if (allowInProcessDiffusers) allowInProcessDiffusers.disabled = settingsFamily !== "hf";
  if (lowCpuMemUsage) lowCpuMemUsage.disabled = settingsFamily !== "hf";
  if (cudaFallbackToCpu) cudaFallbackToCpu.disabled = settingsFamily !== "hf";
  if (keepDownloadedModels) keepDownloadedModels.disabled = settingsFamily !== "hf";
  if (disableXet) disableXet.disabled = settingsFamily !== "hf";
  if (deviceMap) deviceMap.disabled = settingsFamily !== "hf";
  localPerformanceFields.forEach((id) => {
    const input = $(id);
    if (input) input.disabled = mode !== "local";
  });
  const status = $("comfyui-test-connection-status");
  if (status && !status.dataset.userTouched) {
    status.textContent = settingsFamily === "hf"
      ? "HF 頁籤會檢查 Hugging Face repo 與 Python / Diffusers 套件；只有勾選主程序資源風險確認後才允許直接推論，切換到這裡不會停用 ComfyUI 設定。"
      : (mode === "local"
        ? "本地模式會測試本地 API；若產圖時 API 未啟動，後端會嘗試執行啟動腳本。"
        : "ComfyUI / GGUF 遠端模式只負責呼叫指定 API 生圖；Civitai API Key 與模型匯入區會常駐可見，下載會寫入本站設定的 ComfyUI models 目錄。");
    status.style.color = "var(--muted)";
  }
  if (typeof updateComfyuiRootPanelVisibility === "function") updateComfyuiRootPanelVisibility(mode);
}

function updateCaptchaModeFields() {
  const mode = $("s-captcha-mode")?.value || "none";
  const wrap = $("captcha-turnstile-site-key-field");
  const input = $("s-captcha-turnstile-site-key");
  const showTurnstile = mode === "turnstile";
  if (wrap) wrap.style.display = showTurnstile ? "" : "none";
  if (input) input.disabled = !showTurnstile;
}

function updateBackpressureModeFields() {
  const mode = $("s-server-backpressure-mode")?.value || "auto";
  const manual = mode === "manual";
  ["s-server-backpressure-normal-limit", "s-server-backpressure-heavy-limit", "s-server-backpressure-fast-lane-reserved"].forEach((id) => {
    const input = $(id);
    if (!input) return;
    input.disabled = !manual;
    input.closest(".field")?.classList.toggle("muted", !manual);
  });
}

function formatServerTimeSkew(ms) {
  const absMs = Math.abs(Number(ms) || 0);
  if (absMs < 1000) return `${Math.round(absMs)} ms`;
  if (absMs < 60000) return `${(absMs / 1000).toFixed(1)} 秒`;
  return `${(absMs / 60000).toFixed(1)} 分鐘`;
}

function renderServerTimeStatus(serverTime, { sampledAtMs = Date.now() } = {}) {
  const status = $("server-time-status");
  if (!status) return;
  if (!serverTime || typeof serverTime !== "object") {
    status.textContent = "尚未取得伺服器時間。";
    status.style.color = "var(--muted)";
    return;
  }
  const serverMs = Number(serverTime.server_time_unix_ms || Date.parse(serverTime.server_time_utc || ""));
  if (!Number.isFinite(serverMs)) {
    status.textContent = "伺服器時間格式無法解析，請檢查 /api/version 回傳。";
    status.style.color = "#ff8a80";
    return;
  }
  const skewMs = serverMs - Number(sampledAtMs || Date.now());
  const absMs = Math.abs(skewMs);
  const tone = absMs <= 3000 ? "ok" : (absMs <= 30000 ? "warn" : "bad");
  const browserText = new Date(Number(sampledAtMs || Date.now())).toLocaleString();
  const direction = skewMs >= 0 ? "快" : "慢";
  const appTz = serverTime.timezone || "UTC";
  const systemTz = serverTime.system_timezone || appTz;
  status.textContent = [
    `App 顯示：${serverTime.server_time_local || serverTime.server_time_utc || "-"}（${appTz}，${serverTime.utc_offset_label || ""}）`,
    `主機實際：${serverTime.system_time_local || "-"}（${systemTz}）`,
    `UTC：${serverTime.server_time_utc || "-"}`,
    `瀏覽器：${browserText}`,
    `差距：伺服器約${direction} ${formatServerTimeSkew(skewMs)}`,
    tone === "ok" ? "時間看起來正常" : (tone === "warn" ? "有輕微偏移，建議確認主機 NTP" : "偏移明顯，請檢查主機時間/NTP 與時區設定"),
  ].join("；");
  status.style.color = tone === "ok" ? "var(--muted)" : (tone === "warn" ? "#ffb74d" : "#ff8a80");
}

async function refreshServerTimeStatus() {
  const status = $("server-time-status");
  if (status) {
    status.textContent = "正在檢查伺服器時間...";
    status.style.color = "var(--muted)";
  }
  try {
    const requestStartedAtMs = Date.now();
    const res = await apiFetch(API + "/version", {
      credentials: "same-origin",
      cache: "no-store",
    });
    const responseReceivedAtMs = Date.now();
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || "版本 API 讀取失敗");
    renderServerTimeStatus(json.server_time, {
      sampledAtMs: Math.round((requestStartedAtMs + responseReceivedAtMs) / 2),
    });
    if (json.server_time?.timezone && $("s-server-timezone") && !$("s-server-timezone").value) {
      $("s-server-timezone").value = json.server_time.timezone;
    }
  } catch (err) {
    if (status) {
      status.textContent = `伺服器時間檢查失敗：${err.message || "請求失敗"}`;
      status.style.color = "#ff8a80";
    }
  }
}

function renderBackpressureStatus(backpressure) {
  const status = $("server-backpressure-status");
  if (!status) return;
  if (!backpressure || typeof backpressure !== "object") {
    status.textContent = "尚未取得目前 backpressure 狀態。";
    renderBackpressureTrafficChart(null);
    return;
  }
  const normal = backpressure.normal || {};
  const heavy = backpressure.heavy || {};
  const root = backpressure.root || {};
  const edgeGuard = backpressure.edge_guard || {};
  const edgeLabels = edgeGuard.labels || {};
  const edgeRejected = Object.values(edgeLabels).reduce((sum, item) => sum + Number(item?.rejected || 0), 0);
  const sources = backpressure.limit_sources || {};
  const anomalyAudit = backpressure.anomaly_audit || {};
  const anomalyRecent = Array.isArray(anomalyAudit.recent) ? anomalyAudit.recent : [];
  const anomalyLogPath = anomalyAudit.log_path || "";
  status.textContent = [
    `目前 PID ${backpressure.pid || "-"}，${backpressure.process_local ? "每個 worker 各自統計" : "全域統計"}`,
    `threads ${backpressure.thread_capacity ?? "-"}，fast lane 保留 ${backpressure.fast_lane_reserved ?? "-"}`,
    `normal ${normal.active || 0}/${normal.limit || 0} rejected ${normal.rejected || 0} (${sources.normal || "auto"})`,
    `heavy ${heavy.active || 0}/${heavy.limit || 0} rejected ${heavy.rejected || 0} (${sources.heavy || "auto"})`,
    `root ${root.active || 0}/${root.limit || 0} rejected ${root.rejected || 0} (${sources.root || "auto"})`,
    `edge guard ${edgeGuard.enabled === false ? "off" : "on"} window ${edgeGuard.window_seconds || "-"}s rejected ${edgeRejected}`,
    anomalyLogPath
      ? `異常審計 ${anomalyRecent.length} 筆近期事件，紀錄檔 ${anomalyLogPath}`
      : `異常審計 ${anomalyRecent.length} 筆近期事件`,
  ].join("；");
  renderBackpressureTrafficChart(backpressure);
}

function trafficPolyline(points, key, maxValue, width, height, padX, padY) {
  if (!Array.isArray(points) || points.length < 2) return "";
  const span = Math.max(1, points.length - 1);
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;
  return points.map((point, index) => {
    const x = padX + (innerW * index / span);
    const y = height - padY - (innerH * Math.max(0, Number(point?.[key] || 0)) / Math.max(1, maxValue));
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

function renderBackpressureTrafficChart(backpressure) {
  const chart = $("server-backpressure-chart");
  if (!chart) return;
  const chartStarted = rootAdminTimingStart("secondary-chart");
  const traffic = backpressure?.traffic || {};
  const points = Array.isArray(traffic.points) ? traffic.points : [];
  if (!points.length) {
    chart.innerHTML = '<div class="traffic-chart-empty">尚未取得近期流量資料</div>';
    rootAdminTimingFinish("secondary-chart", chartStarted, "root backpressure chart empty-state render");
    return;
  }
  const totals = traffic.totals || {};
  const maxValue = Math.max(1, ...points.map((point) => Math.max(
    Number(point.total || 0),
    Number(point.accepted || 0),
    Number(point.rejected || 0),
    Number(point.root || 0),
    Number(point.edge_guard || 0)
  )));
  const width = 640;
  const height = 150;
  const padX = 18;
  const padY = 14;
  const totalLine = trafficPolyline(points, "total", maxValue, width, height, padX, padY);
  const acceptedLine = trafficPolyline(points, "accepted", maxValue, width, height, padX, padY);
  const rejectedLine = trafficPolyline(points, "rejected", maxValue, width, height, padX, padY);
  const rootLine = trafficPolyline(points, "root", maxValue, width, height, padX, padY);
  const edgeLine = trafficPolyline(points, "edge_guard", maxValue, width, height, padX, padY);
  const nowLabel = points[points.length - 1]?.label || "";
  const firstLabel = points[0]?.label || "";
  chart.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      <line x1="${padX}" y1="${height - padY}" x2="${width - padX}" y2="${height - padY}" stroke="rgba(160,170,190,.28)" stroke-width="1" />
      <line x1="${padX}" y1="${padY}" x2="${padX}" y2="${height - padY}" stroke="rgba(160,170,190,.22)" stroke-width="1" />
      <line x1="${padX}" y1="${padY}" x2="${width - padX}" y2="${padY}" stroke="rgba(160,170,190,.12)" stroke-width="1" />
      <polyline points="${totalLine}" fill="none" stroke="var(--accent)" stroke-width="2" vector-effect="non-scaling-stroke" opacity=".72" />
      <polyline points="${acceptedLine}" fill="none" stroke="var(--accent2)" stroke-width="2.5" vector-effect="non-scaling-stroke" />
      <polyline points="${rejectedLine}" fill="none" stroke="var(--danger)" stroke-width="2.5" vector-effect="non-scaling-stroke" />
      <polyline points="${rootLine}" fill="none" stroke="var(--warning)" stroke-width="2.25" vector-effect="non-scaling-stroke" />
      <polyline points="${edgeLine}" fill="none" stroke="#ff7a90" stroke-width="2" vector-effect="non-scaling-stroke" stroke-dasharray="4 4" />
      <text x="${padX}" y="${height - 2}" fill="var(--muted)" font-size="10">${sanitize(firstLabel)}</text>
      <text x="${width - padX}" y="${height - 2}" fill="var(--muted)" font-size="10" text-anchor="end">${sanitize(nowLabel)}</text>
      <text x="${width - padX}" y="${padY + 9}" fill="var(--muted)" font-size="10" text-anchor="end">max ${maxValue}/s</text>
    </svg>
    <div class="traffic-chart-legend">
      <span class="traffic-chart-total">總請求 ${Number(totals.total || 0)}</span>
      <span class="traffic-chart-accepted">接受 ${Number(totals.accepted || 0)}</span>
      <span class="traffic-chart-rejected">高峰拒絕 ${Number(totals.rejected || 0)}</span>
      <span class="traffic-chart-root">Root 管理 ${Number(totals.root || 0)}</span>
      <span class="traffic-chart-edge">Edge guard ${Number(totals.edge_guard || 0)}</span>
      <span>PID ${sanitize(String(backpressure?.pid || "-"))} · ${Number(traffic.window_seconds || 0)} 秒視窗</span>
    </div>
  `;
  rootAdminTimingFinish("secondary-chart", chartStarted, "GET /api/root/backpressure → traffic chart render");
}

function stopBackpressureTrafficPoll() {
  if (backpressureTrafficPollTimer) {
    clearInterval(backpressureTrafficPollTimer);
    backpressureTrafficPollTimer = null;
  }
}

function startBackpressureTrafficPoll() {
  stopBackpressureTrafficPoll();
  if (!canRunRootManagementPoll(isSystemSettingsActive)) return;
  backpressureTrafficPollTimer = setInterval(refreshBackpressureTraffic, backpressureTrafficRefreshSeconds * 1000);
}

async function refreshBackpressureTraffic() {
  if (!canRunRootManagementPoll(isSystemSettingsActive)) return;
  try {
    const csrf = await fetchCsrfToken();
    const res = await apiFetch(API + "/root/backpressure", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (json.ok) renderBackpressureStatus(json.backpressure);
  } catch (_) {}
}

async function loadSettings() {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/settings", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    setSettingsStatus(json.msg || "系統設定讀取失敗", false);
    return;
  }
  const s = json.settings || {};
  const bind = json.server_bind || {};
  const ssl = json.server_ssl || {};
  renderBackpressureStatus(json.backpressure);
  if ($("s-maintenance-mode")) $("s-maintenance-mode").checked = !!s.maintenance_mode;
  if ($("s-audit-chain-enabled")) $("s-audit-chain-enabled").checked = !!s.audit_chain_enabled;
  if ($("s-ip-blocking-enabled")) $("s-ip-blocking-enabled").checked = !!s.ip_blocking_enabled;
  if ($("s-login-violation-enabled")) $("s-login-violation-enabled").checked = !!s.login_violation_enabled;
  if ($("s-rate-limit-violation-enabled")) $("s-rate-limit-violation-enabled").checked = !!s.rate_limit_violation_enabled;
  if ($("s-root-ip-whitelist-enabled")) $("s-root-ip-whitelist-enabled").checked = !!s.root_ip_whitelist_enabled;
  if ($("s-password-strength-policy-enabled")) $("s-password-strength-policy-enabled").checked = s.password_strength_policy_enabled !== false;
  if ($("s-root-ip-whitelist")) $("s-root-ip-whitelist").value = s.root_ip_whitelist || "";
  if ($("s-browser-only-mode-enabled")) $("s-browser-only-mode-enabled").checked = !!s.browser_only_mode_enabled;
  if ($("s-integrity-guard-enabled")) $("s-integrity-guard-enabled").checked = !!s.integrity_guard_enabled;
  if ($("s-integrity-guard-strict-mode")) $("s-integrity-guard-strict-mode").checked = !!s.integrity_guard_strict_mode;
  if ($("s-allow-register")) $("s-allow-register").checked = !!s.allow_register;
  if ($("s-require-email")) $("s-require-email").checked = !!s.require_email_verification;
  if ($("s-password-reset-mode")) $("s-password-reset-mode").value = s.password_reset_mode || "admin_review";
  if ($("s-login-autofill-block-enabled")) $("s-login-autofill-block-enabled").checked = !!s.login_autofill_block_enabled;
  if ($("s-captcha-mode")) $("s-captcha-mode").value = s.captcha_mode || "none";
  if ($("s-captcha-ttl-seconds")) $("s-captcha-ttl-seconds").value = s.captcha_ttl_seconds || 300;
  if ($("s-captcha-turnstile-site-key")) $("s-captcha-turnstile-site-key").value = s.captcha_turnstile_site_key || "";
  updateCaptchaModeFields();
  if ($("s-max-fail")) $("s-max-fail").value = s.max_login_failures || 5;
  if ($("s-block-dur")) $("s-block-dur").value = s.block_duration_minutes || 30;
  if ($("s-session-ttl")) $("s-session-ttl").value = s.session_ttl_hours || 24;
  if ($("s-session-idle-timeout")) $("s-session-idle-timeout").value = s.session_idle_timeout_minutes ?? "";
  if ($("s-notification-muted-types")) $("s-notification-muted-types").value = s.notification_muted_types || "";
  if ($("s-server-ssl-enabled")) $("s-server-ssl-enabled").checked = !!s.server_ssl_enabled;
  if ($("s-server-listen-host")) $("s-server-listen-host").value = s.server_listen_host || "";
  if ($("s-server-listen-port")) $("s-server-listen-port").value = s.server_listen_port || "";
  if ($("s-server-timezone")) $("s-server-timezone").value = s.server_timezone || "UTC";
  renderServerTimeStatus(json.server_time);
  if ($("s-server-backpressure-enabled")) $("s-server-backpressure-enabled").checked = s.server_backpressure_enabled !== false;
  if ($("s-server-backpressure-mode")) $("s-server-backpressure-mode").value = s.server_backpressure_mode || "auto";
  if ($("s-server-backpressure-thread-capacity")) $("s-server-backpressure-thread-capacity").value = Number(s.server_backpressure_thread_capacity || 0);
  if ($("s-server-backpressure-normal-limit")) $("s-server-backpressure-normal-limit").value = Number(s.server_backpressure_normal_limit || 0);
  if ($("s-server-backpressure-heavy-limit")) $("s-server-backpressure-heavy-limit").value = Number(s.server_backpressure_heavy_limit || 0);
  if ($("s-server-backpressure-root-priority-enabled")) $("s-server-backpressure-root-priority-enabled").checked = s.server_backpressure_root_priority_enabled !== false;
  if ($("s-server-backpressure-root-limit")) $("s-server-backpressure-root-limit").value = Number(s.server_backpressure_root_limit || 0);
  if ($("s-server-backpressure-fast-lane-reserved")) $("s-server-backpressure-fast-lane-reserved").value = Number(s.server_backpressure_fast_lane_reserved || 0);
  if ($("s-server-backpressure-retry-after-seconds")) $("s-server-backpressure-retry-after-seconds").value = Number(s.server_backpressure_retry_after_seconds || 2);
  if ($("s-server-backpressure-refresh-seconds")) $("s-server-backpressure-refresh-seconds").value = Number(s.server_backpressure_refresh_seconds || 2);
  backpressureTrafficRefreshSeconds = adminRefreshSeconds(s.server_backpressure_traffic_refresh_seconds, 4, 1, 300);
  serverOutputRefreshSeconds = adminRefreshSeconds(s.server_output_refresh_seconds, 3, 1, 300);
  securityTestJobPollSeconds = adminRefreshSeconds(s.security_test_job_poll_seconds, 3, 1, 300);
  if ($("s-server-backpressure-traffic-refresh-seconds")) $("s-server-backpressure-traffic-refresh-seconds").value = backpressureTrafficRefreshSeconds;
  if ($("s-server-output-refresh-seconds")) $("s-server-output-refresh-seconds").value = serverOutputRefreshSeconds;
  if ($("s-security-test-job-poll-seconds")) $("s-security-test-job-poll-seconds").value = securityTestJobPollSeconds;
  applySystemResourceRefreshSeconds(s.system_resource_board_refresh_seconds || 5, { restart: false });
  if ($("s-job-center-refresh-seconds")) $("s-job-center-refresh-seconds").value = adminRefreshSeconds(s.job_center_refresh_seconds, 3, 1, 300);
  if ($("s-economy-dashboard-refresh-seconds")) $("s-economy-dashboard-refresh-seconds").value = adminRefreshSeconds(s.economy_dashboard_refresh_seconds, 30, 5, 600);
  if ($("s-trading-dashboard-refresh-seconds")) $("s-trading-dashboard-refresh-seconds").value = adminRefreshSeconds(s.trading_dashboard_refresh_seconds, 5, 2, 300);
  if ($("s-trading-live-price-refresh-seconds")) $("s-trading-live-price-refresh-seconds").value = adminRefreshSeconds(s.trading_live_price_refresh_seconds, 2, 1, 60);
  if ($("s-trading-reference-price-refresh-seconds")) $("s-trading-reference-price-refresh-seconds").value = adminRefreshSeconds(s.trading_reference_price_refresh_seconds, 1, 1, 60);
  if ($("s-trading-reference-chart-refresh-seconds")) $("s-trading-reference-chart-refresh-seconds").value = adminRefreshSeconds(s.trading_reference_chart_refresh_seconds, 5, 2, 300);
  if ($("s-comfyui-job-poll-seconds")) $("s-comfyui-job-poll-seconds").value = adminRefreshSeconds(s.comfyui_job_poll_seconds, 1, 1, 60);
  if ($("s-notification-poll-seconds")) $("s-notification-poll-seconds").value = adminRefreshSeconds(s.notification_poll_seconds, 60, 5, 600);
  if ($("s-game-invite-poll-active-seconds")) $("s-game-invite-poll-active-seconds").value = adminRefreshSeconds(s.game_invite_poll_active_seconds, 5, 2, 300);
  if ($("s-game-invite-poll-idle-seconds")) $("s-game-invite-poll-idle-seconds").value = adminRefreshSeconds(s.game_invite_poll_idle_seconds, 60, 10, 600);
  if ($("s-game-invite-poll-hidden-seconds")) $("s-game-invite-poll-hidden-seconds").value = adminRefreshSeconds(s.game_invite_poll_hidden_seconds, 180, 30, 1800);
  if ($("s-server-connection-monitor-seconds")) $("s-server-connection-monitor-seconds").value = adminRefreshSeconds(s.server_connection_monitor_seconds, 15, 5, 300);
  if ($("s-drive-dashboard-lazy-refresh-seconds")) $("s-drive-dashboard-lazy-refresh-seconds").value = adminRefreshSeconds(s.drive_dashboard_lazy_refresh_seconds, 10, 1, 300);
  updateBackpressureModeFields();
  if ($("s-comfyui-connection-mode")) $("s-comfyui-connection-mode").value = s.comfyui_connection_mode || "remote";
  if ($("s-comfyui-remote-api-url")) $("s-comfyui-remote-api-url").value = s.comfyui_remote_api_url || DEFAULT_COMFYUI_REMOTE_API_URL;
  if ($("s-comfyui-base-dir")) $("s-comfyui-base-dir").value = s.comfyui_base_dir || "";
  if ($("s-comfyui-local-start-script")) $("s-comfyui-local-start-script").value = s.comfyui_local_start_script || "";
  if ($("s-comfyui-api-host")) $("s-comfyui-api-host").value = s.comfyui_api_host || "localhost";
  if ($("s-comfyui-api-port")) $("s-comfyui-api-port").value = s.comfyui_api_port || 8192;
  if ($("s-comfyui-local-vram-mode")) $("s-comfyui-local-vram-mode").value = s.comfyui_local_vram_mode || "auto";
  if ($("s-comfyui-local-precision")) $("s-comfyui-local-precision").value = s.comfyui_local_precision || "auto";
  if ($("s-comfyui-local-unet-dtype")) $("s-comfyui-local-unet-dtype").value = s.comfyui_local_unet_dtype || "auto";
  if ($("s-comfyui-local-vae-dtype")) $("s-comfyui-local-vae-dtype").value = s.comfyui_local_vae_dtype || "auto";
  if ($("s-comfyui-local-text-encoder-dtype")) $("s-comfyui-local-text-encoder-dtype").value = s.comfyui_local_text_encoder_dtype || "auto";
  if ($("s-comfyui-local-cpu-vae")) $("s-comfyui-local-cpu-vae").checked = !!s.comfyui_local_cpu_vae;
  if ($("s-comfyui-local-attention-mode")) $("s-comfyui-local-attention-mode").value = s.comfyui_local_attention_mode || "auto";
  if ($("s-comfyui-local-upcast-attention")) $("s-comfyui-local-upcast-attention").value = s.comfyui_local_upcast_attention || "auto";
  if ($("s-comfyui-local-cuda-malloc")) $("s-comfyui-local-cuda-malloc").value = s.comfyui_local_cuda_malloc || "auto";
  if ($("s-comfyui-local-disable-smart-memory")) $("s-comfyui-local-disable-smart-memory").checked = !!s.comfyui_local_disable_smart_memory;
  if ($("s-comfyui-local-deterministic")) $("s-comfyui-local-deterministic").checked = !!s.comfyui_local_deterministic;
  if ($("s-comfyui-local-async-offload")) $("s-comfyui-local-async-offload").value = s.comfyui_local_async_offload || "auto";
  if ($("s-comfyui-local-cache-mode")) $("s-comfyui-local-cache-mode").value = s.comfyui_local_cache_mode || "auto";
  if ($("s-comfyui-local-cache-lru")) $("s-comfyui-local-cache-lru").value = Number(s.comfyui_local_cache_lru || 0);
  if ($("s-comfyui-local-reserve-vram-gb")) $("s-comfyui-local-reserve-vram-gb").value = s.comfyui_local_reserve_vram_gb || "";
  if ($("s-comfyui-civitai-api-key")) $("s-comfyui-civitai-api-key").value = s.comfyui_civitai_api_key || "";
  if ($("s-comfyui-diffusers-model-repo")) $("s-comfyui-diffusers-model-repo").value = normalizeAdminHuggingFaceRepoInput(s.comfyui_diffusers_model_repo || "");
  if ($("s-comfyui-huggingface-api-token")) $("s-comfyui-huggingface-api-token").value = "";
  if ($("s-comfyui-huggingface-api-token-clear")) $("s-comfyui-huggingface-api-token-clear").checked = false;
  if ($("s-comfyui-huggingface-cache-root")) $("s-comfyui-huggingface-cache-root").value = s.comfyui_huggingface_cache_root || "";
  if ($("s-comfyui-diffusers-device")) $("s-comfyui-diffusers-device").value = s.comfyui_diffusers_device || "auto";
  if ($("s-comfyui-diffusers-dtype")) $("s-comfyui-diffusers-dtype").value = s.comfyui_diffusers_dtype || "auto";
  if ($("s-comfyui-diffusers-device-map")) $("s-comfyui-diffusers-device-map").value = s.comfyui_diffusers_device_map || "auto";
  if ($("s-comfyui-allow-in-process-diffusers")) $("s-comfyui-allow-in-process-diffusers").checked = !!s.comfyui_allow_in_process_diffusers;
  if ($("s-comfyui-diffusers-low-cpu-mem-usage")) $("s-comfyui-diffusers-low-cpu-mem-usage").checked = s.comfyui_diffusers_low_cpu_mem_usage !== false;
  if ($("s-comfyui-diffusers-cuda-fallback-to-cpu")) $("s-comfyui-diffusers-cuda-fallback-to-cpu").checked = s.comfyui_diffusers_cuda_fallback_to_cpu !== false;
  if ($("s-comfyui-diffusers-keep-downloaded-models")) $("s-comfyui-diffusers-keep-downloaded-models").checked = s.comfyui_diffusers_keep_downloaded_models !== false;
  if ($("s-comfyui-diffusers-disable-xet")) $("s-comfyui-diffusers-disable-xet").checked = s.comfyui_diffusers_disable_xet !== false;
  if ($("comfyui-huggingface-api-token-state")) {
    $("comfyui-huggingface-api-token-state").textContent = s.comfyui_huggingface_api_token_configured
      ? "目前已儲存 Hugging Face API Token；留空儲存不會變更。"
      : "目前未儲存 Hugging Face API Token；公開模型可不填。";
  }
  if ($("s-comfyui-paid-api-nodes-enabled")) $("s-comfyui-paid-api-nodes-enabled").checked = !!s.comfyui_paid_api_nodes_enabled;
  if ($("s-comfyui-account-api-key")) $("s-comfyui-account-api-key").value = "";
  if ($("s-comfyui-account-api-key-clear")) $("s-comfyui-account-api-key-clear").checked = false;
  if ($("comfyui-account-api-key-state")) {
    $("comfyui-account-api-key-state").textContent = s.comfyui_account_api_key_configured
      ? "目前已儲存 ComfyUI Account API Key；留空儲存不會變更。"
      : "目前未儲存 ComfyUI Account API Key。";
  }
  if ($("s-ai-agent-provider")) $("s-ai-agent-provider").value = s.ai_agent_provider || "hermes";
  if ($("s-ai-agent-api-base-url")) $("s-ai-agent-api-base-url").value = s.ai_agent_api_base_url || "http://127.0.0.1:8642/v1";
  if ($("s-ai-agent-api-key")) $("s-ai-agent-api-key").value = "";
  if ($("s-ai-agent-api-key-clear")) $("s-ai-agent-api-key-clear").checked = false;
  if ($("ai-agent-api-key-state")) {
    $("ai-agent-api-key-state").textContent = s.ai_agent_api_key_configured
      ? "目前已儲存 AI Agent API Key；留空儲存不會變更。"
      : "目前未儲存 AI Agent API Key。本機 Hermes local-only 模式可不填。";
  }
  if ($("s-ai-agent-model")) $("s-ai-agent-model").value = s.ai_agent_model || "hermes-agent";
  if ($("s-ai-agent-operation-mode")) $("s-ai-agent-operation-mode").value = s.ai_agent_operation_mode || "assist";
  if ($("s-ai-agent-allowed-models")) $("s-ai-agent-allowed-models").value = s.ai_agent_allowed_models || "";
  if ($("s-ai-agent-request-timeout-seconds")) $("s-ai-agent-request-timeout-seconds").value = s.ai_agent_request_timeout_seconds || 120;
  if ($("s-ai-agent-max-prompt-chars")) $("s-ai-agent-max-prompt-chars").value = s.ai_agent_max_prompt_chars || 20000;
  if ($("s-ai-agent-allow-image-input")) $("s-ai-agent-allow-image-input").checked = s.ai_agent_allow_image_input !== false;
  if ($("s-ai-agent-allow-tool-runs")) $("s-ai-agent-allow-tool-runs").checked = !!s.ai_agent_allow_tool_runs;
  if ($("s-ai-agent-persona")) $("s-ai-agent-persona").value = s.ai_agent_persona || "concise_helper";
  if ($("s-ai-agent-task-site-guide")) $("s-ai-agent-task-site-guide").checked = s.ai_agent_task_site_guide !== false;
  if ($("s-ai-agent-task-troubleshoot")) $("s-ai-agent-task-troubleshoot").checked = s.ai_agent_task_troubleshoot !== false;
  if ($("s-ai-agent-task-prompt")) $("s-ai-agent-task-prompt").checked = s.ai_agent_task_prompt !== false;
  if ($("s-ai-agent-audit-interval-minutes")) $("s-ai-agent-audit-interval-minutes").value = s.ai_agent_audit_interval_minutes || 5;
  if ($("s-ai-agent-audit-cpu-percent-threshold")) $("s-ai-agent-audit-cpu-percent-threshold").value = s.ai_agent_audit_cpu_percent_threshold || 92;
  if ($("s-ai-agent-audit-ram-percent-threshold")) $("s-ai-agent-audit-ram-percent-threshold").value = s.ai_agent_audit_ram_percent_threshold || 92;
  if ($("s-ai-agent-audit-disk-percent-threshold")) $("s-ai-agent-audit-disk-percent-threshold").value = s.ai_agent_audit_disk_percent_threshold || 95;
  if ($("s-ai-agent-audit-ip-event-rate-threshold")) $("s-ai-agent-audit-ip-event-rate-threshold").value = s.ai_agent_audit_ip_event_rate_threshold || 240;
  if ($("s-ai-agent-audit-ip-event-rate-window-minutes")) $("s-ai-agent-audit-ip-event-rate-window-minutes").value = s.ai_agent_audit_ip_event_rate_window_minutes || 5;
  if ($("s-ai-agent-audit-security-event-rate-threshold")) $("s-ai-agent-audit-security-event-rate-threshold").value = s.ai_agent_audit_security_event_rate_threshold || 120;
  if ($("s-ai-agent-audit-security-event-rate-window-minutes")) $("s-ai-agent-audit-security-event-rate-window-minutes").value = s.ai_agent_audit_security_event_rate_window_minutes || 5;
  if ($("s-ai-agent-audit-auto-block-suspect-ip")) $("s-ai-agent-audit-auto-block-suspect-ip").checked = !!s.ai_agent_audit_auto_block_suspect_ip;
  if ($("s-ai-agent-audit-block-minutes")) $("s-ai-agent-audit-block-minutes").value = s.ai_agent_audit_block_minutes || 30;
  if ($("s-ai-agent-audit-notify-root")) $("s-ai-agent-audit-notify-root").checked = !!s.ai_agent_audit_notify_root;
  updateComfyuiConnectionModeFields();
  if ($("s-comfyui-max-batch-size")) $("s-comfyui-max-batch-size").value = s.comfyui_max_batch_size || 1;
  if ($("s-comfyui-default-width")) $("s-comfyui-default-width").value = s.comfyui_default_width || 1024;
  if ($("s-comfyui-default-height")) $("s-comfyui-default-height").value = s.comfyui_default_height || 1024;
  if ($("s-cloud-drive-storage-root")) $("s-cloud-drive-storage-root").value = s.cloud_drive_storage_root || "";
  if ($("s-cloud-drive-global-capacity-limit-mb")) $("s-cloud-drive-global-capacity-limit-mb").value = s.cloud_drive_global_capacity_limit_mb ?? -1;
  if ($("s-server-max-content-mb")) $("s-server-max-content-mb").value = s.server_max_content_mb ?? 8192;
  if ($("server-max-content-status")) {
    const currentMax = Number(s.server_max_content_current_mb || s.server_max_content_mb || 0);
    const envText = s.server_max_content_env_override ? "；目前啟動環境有 HTML_LEARNING_MAX_CONTENT_MB 覆寫，下次重啟會以啟動值為準" : "";
    $("server-max-content-status").textContent = `目前執行上限：${currentMax || "-"} MB${envText}`;
  }
  if ($("s-cloud-drive-transfer-limits-enabled")) $("s-cloud-drive-transfer-limits-enabled").checked = !!s.cloud_drive_transfer_limits_enabled;
  renderCloudDriveTransferLimits(s.cloud_drive_transfer_limits_json);
  if ($("s-remote-download-max-concurrent-global")) $("s-remote-download-max-concurrent-global").value = s.remote_download_max_concurrent_global ?? 1;
  if ($("s-remote-download-max-concurrent-per-user")) $("s-remote-download-max-concurrent-per-user").value = s.remote_download_max_concurrent_per_user ?? 1;
  if ($("s-bt-download-backend")) $("s-bt-download-backend").value = s.bt_download_backend || "auto";
  if ($("s-transmission-rpc-url")) $("s-transmission-rpc-url").value = s.transmission_rpc_url || "http://127.0.0.1:9091/transmission/rpc";
  if ($("s-transmission-rpc-username")) $("s-transmission-rpc-username").value = s.transmission_rpc_username || "";
  if ($("s-transmission-rpc-password")) $("s-transmission-rpc-password").value = "";
  if ($("transmission-rpc-password-state")) {
    $("transmission-rpc-password-state").textContent = s.transmission_rpc_password_configured
      ? "目前已儲存 Transmission RPC 密碼；留空儲存不會變更。"
      : "目前未儲存 Transmission RPC 密碼。";
  }
  if ($("s-storage-maintenance-auto-enabled")) $("s-storage-maintenance-auto-enabled").checked = !!s.storage_maintenance_auto_enabled;
  if ($("s-storage-maintenance-daily-time")) $("s-storage-maintenance-daily-time").value = s.storage_maintenance_daily_time || "04:00";
  if ($("s-storage-trash-retention-days")) $("s-storage-trash-retention-days").value = s.storage_trash_retention_days || 30;
  if ($("s-snapshot-daily-auto-enabled")) $("s-snapshot-daily-auto-enabled").checked = !!s.snapshot_daily_auto_enabled;
  if ($("s-snapshot-daily-time")) $("s-snapshot-daily-time").value = s.snapshot_daily_time || "03:00";
  const bindStatus = $("server-bind-status");
  if (bindStatus) {
    const restartText = bind.restart_required ? "需重啟才會套用新 listen 設定" : "目前執行中的 listen 設定已一致";
    bindStatus.textContent = `目前 ${bind.current_host || bind.host || "0.0.0.0"}:${bind.current_port || bind.port || 5000}，下次啟動 ${bind.host || "0.0.0.0"}:${bind.port || 5000}。${restartText}`;
    bindStatus.style.color = bind.restart_required ? "#ffb74d" : "var(--muted)";
  }
  const sslStatus = $("server-ssl-status");
  if (sslStatus) {
    let detail = `目前 ${ssl.current_scheme || "http"}，下次啟動 ${ssl.scheme || "http"}。`;
    if (ssl.cert_required) detail += " 已要求 HTTPS，但缺少 runtime/cert.pem 或 runtime/key.pem。";
    else if (!ssl.enabled_by_setting) detail += " root 設定為停用 HTTPS。";
    else detail += " HTTPS 憑證檢查通過。";
    if (ssl.restart_required) detail += " 需重啟才會套用。";
    sslStatus.textContent = detail;
    sslStatus.style.color = ssl.cert_required || ssl.restart_required ? "#ffb74d" : "var(--muted)";
  }
  const driveStorage = json.cloud_drive_storage || {};
  const driveStorageStatus = $("cloud-drive-storage-status");
  if (driveStorageStatus) {
    const restartText = driveStorage.restart_required ? "需重啟服務器才會切到新儲存根目錄" : "目前執行中的儲存根目錄已一致";
    const globalCapacity = driveStorage.global_capacity || {};
    const disk = driveStorage.disk || {};
    const capacityText = globalCapacity.configured_limit_mb === -1
      ? `全用戶容量上限：磁碟總容量 95%（${formatBytes(globalCapacity.limit_bytes)}）`
      : `全用戶容量上限：${formatBytes(globalCapacity.limit_bytes)}`;
    const diskText = disk.total_bytes ? `實體磁碟：剩餘 ${formatBytes(disk.free_bytes)} / 總量 ${formatBytes(disk.total_bytes)}` : "";
    driveStorageStatus.textContent = `目前 ${driveStorage.current_root || "-"}，下次啟動 ${driveStorage.effective_next_root || "-"}。${restartText}。${capacityText}${diskText ? `。${diskText}` : ""}`;
    driveStorageStatus.style.color = driveStorage.restart_required ? "#ffb74d" : "var(--muted)";
  }
  if ($("s-module-chat-min-role")) $("s-module-chat-min-role").value = s.module_chat_min_role || "user";
  if ($("s-module-profile-min-role")) $("s-module-profile-min-role").value = s.module_profile_min_role || "user";
  if ($("s-module-community-min-role")) $("s-module-community-min-role").value = s.module_community_min_role || "user";
  if ($("s-module-appeals-min-role")) $("s-module-appeals-min-role").value = s.module_appeals_min_role || "user";
  if ($("s-module-accounts-min-role")) $("s-module-accounts-min-role").value = s.module_accounts_min_role || "manager";
  if ($("s-module-comfyui-min-role")) $("s-module-comfyui-min-role").value = s.module_comfyui_min_role || "user";
  if ($("s-module-ai-agent-min-role")) $("s-module-ai-agent-min-role").value = s.module_ai_agent_min_role || "user";
  if ($("s-module-games-min-role")) $("s-module-games-min-role").value = s.module_games_min_role || "user";
  if ($("s-module-videos-min-role")) $("s-module-videos-min-role").value = s.module_videos_min_role || "user";
  if ($("s-video-tip-fee-percent")) $("s-video-tip-fee-percent").value = s.video_tip_fee_percent ?? 5;
  if ($("s-video-tip-min-points")) $("s-video-tip-min-points").value = s.video_tip_min_points ?? 1;
  if ($("s-video-e2ee-derivatives-enabled")) $("s-video-e2ee-derivatives-enabled").checked = s.video_e2ee_derivatives_enabled !== false;
  if ($("s-video-e2ee-derivative-heights")) $("s-video-e2ee-derivative-heights").value = s.video_e2ee_derivative_heights || "720,480";
  if ($("s-video-e2ee-derivative-reject-larger-than-original")) $("s-video-e2ee-derivative-reject-larger-than-original").checked = s.video_e2ee_derivative_reject_larger_than_original !== false;
  if ($("s-video-e2ee-derivative-quota-exempt")) $("s-video-e2ee-derivative-quota-exempt").checked = s.video_e2ee_derivative_quota_exempt !== false;
  if ($("s-video-hls-cold-cleanup-enabled")) $("s-video-hls-cold-cleanup-enabled").checked = s.video_hls_cold_cleanup_enabled !== false;
  if ($("s-video-hls-protect-days-after-upload")) $("s-video-hls-protect-days-after-upload").value = s.video_hls_protect_days_after_upload ?? 7;
  if ($("s-video-hls-warm-plays-30d-below")) $("s-video-hls-warm-plays-30d-below").value = s.video_hls_warm_plays_30d_below ?? 20;
  if ($("s-video-hls-cold-plays-30d-below")) $("s-video-hls-cold-plays-30d-below").value = s.video_hls_cold_plays_30d_below ?? 5;
  if ($("s-video-hls-cold-no-play-days")) $("s-video-hls-cold-no-play-days").value = s.video_hls_cold_no_play_days ?? 14;
  if ($("s-video-hls-very-cold-no-play-days")) $("s-video-hls-very-cold-no-play-days").value = s.video_hls_very_cold_no_play_days ?? 90;
  if ($("s-video-hls-rebuild-plays-24h")) $("s-video-hls-rebuild-plays-24h").value = s.video_hls_rebuild_plays_24h ?? 3;
  if ($("s-video-hls-rebuild-plays-7d")) $("s-video-hls-rebuild-plays-7d").value = s.video_hls_rebuild_plays_7d ?? 5;
  if ($("s-video-hls-warm-keep-variants")) $("s-video-hls-warm-keep-variants").value = s.video_hls_warm_keep_variants || "original,480p";
  if ($("s-video-hls-mobile-floor-keep-variants")) $("s-video-hls-mobile-floor-keep-variants").value = s.video_hls_mobile_floor_keep_variants || "480p";
  if ($("s-video-hls-cold-cleanup-max-assets-per-run")) $("s-video-hls-cold-cleanup-max-assets-per-run").value = s.video_hls_cold_cleanup_max_assets_per_run ?? 25;
  if ($("s-site-name")) $("s-site-name").value = s.site_name || "hackme_web";
  if ($("s-site-document-title")) $("s-site-document-title").value = s.site_document_title || "hackme_web — 登入系統";
  if ($("s-site-login-heading")) $("s-site-login-heading").value = s.site_login_heading || "Help me improve, hack me please.";
  if ($("s-site-login-subtitle")) $("s-site-login-subtitle").value = s.site_login_subtitle || "簡單的帳號註冊與登入系統";
  if ($("s-site-success-heading")) $("s-site-success-heading").value = s.site_success_heading || "恭喜登入成功";
  if ($("s-site-success-message")) $("s-site-success-message").value = s.site_success_message || "歡迎回來！";
  if ($("s-site-bg")) $("s-site-bg").value = s.site_bg || "#11131d";
  if ($("s-site-theme-mode")) $("s-site-theme-mode").value = s.site_theme_mode || "dark";
  if ($("s-site-surface")) $("s-site-surface").value = s.site_surface || "#1b2030";
  if ($("s-site-accent")) $("s-site-accent").value = s.site_accent || "#7a7bdc";
  if ($("s-site-accent2")) $("s-site-accent2").value = s.site_accent2 || "#43b6a0";
  if ($("s-site-text")) $("s-site-text").value = s.site_text || "#eceef8";
  if ($("s-site-muted")) $("s-site-muted").value = s.site_muted || "#aeb8cc";
  if ($("s-site-layout-mode")) $("s-site-layout-mode").value = s.site_layout_mode || "centered";
  if ($("s-site-density")) $("s-site-density").value = s.site_density || "comfortable";
  if ($("s-site-radius-px")) $("s-site-radius-px").value = String(s.site_radius_px || 12);
  if ($("s-site-font-scale")) $("s-site-font-scale").value = String(s.site_font_scale || 1);
  if ($("s-site-content-width")) $("s-site-content-width").value = String(s.site_content_width || 1380);
  if ($("s-site-font-family")) $("s-site-font-family").value = s.site_font_family || "system";
  if ($("s-site-background-style")) $("s-site-background-style").value = s.site_background_style || "flat";
  if ($("s-site-panel-style")) $("s-site-panel-style").value = s.site_panel_style || "glass";
  if ($("s-site-sidebar-width")) $("s-site-sidebar-width").value = s.site_sidebar_width || "standard";
  applySiteConfig(s);
  renderFeatureSwitchGroups();
  FEATURE_SETTING_KEYS.forEach((key) => {
    const el = $(featureSettingInputId(key));
    if (el) el.checked = !!s[key];
  });
  renderFeatureBundleToolbar();
  renderFeatureAdvisories();
  updateServerModeLaunchCheckVisibility();
}

const FEATURE_SETTING_KEYS = [
  "feature_chat_enabled",
  "feature_community_enabled",
  "feature_accounts_enabled",
  "feature_appeals_enabled",
  "feature_audit_log_enabled",
  "feature_violation_center_enabled",
  "feature_reports_enabled",
  "feature_system_health_enabled",
  "feature_identity_governance_enabled",
  "feature_account_security_enabled",
  "feature_member_governance_enabled",
  "feature_server_modes_enabled",
  "feature_snapshot_restore_enabled",
  "feature_health_center_enabled",
  "feature_forum_core_enabled",
  "feature_ui_rebuild_enabled",
  "feature_reports_notifications_enabled",
  "feature_attachments_enabled",
  "feature_storage_albums_enabled",
  "feature_personalization_enabled",
  "feature_social_search_enabled",
  "feature_advanced_security_enabled",
  "feature_privacy_uploads_enabled",
  "feature_experiments_enabled",
  "feature_comfyui_enabled",
  "feature_ai_agent_enabled",
  "feature_economy_enabled",
  "feature_points_chain_enabled",
  "feature_trading_enabled",
  "feature_games_enabled",
  "feature_videos_enabled"
];

const FEATURE_SETTING_LABELS = {
  feature_chat_enabled: "聊天室",
  feature_community_enabled: "討論區 / 公告 / 留言",
  feature_accounts_enabled: "帳號管理",
  feature_appeals_enabled: "用戶申覆",
  feature_audit_log_enabled: "Audit log 查詢",
  feature_violation_center_enabled: "違規中心",
  feature_reports_enabled: "檢舉審核",
  feature_system_health_enabled: "系統健康燈",
  feature_identity_governance_enabled: "身份治理欄位 / 會員等級",
  feature_account_security_enabled: "帳號安全強化",
  feature_member_governance_enabled: "會員治理與投票",
  feature_server_modes_enabled: "伺服器模式",
  feature_snapshot_restore_enabled: "Snapshot / Restore / Reset",
  feature_health_center_enabled: "健康監控中心新版",
  feature_forum_core_enabled: "論壇核心新版",
  feature_ui_rebuild_enabled: "UI 架構重構",
  feature_reports_notifications_enabled: "檢舉 / 申訴 / 通知新版",
  feature_attachments_enabled: "附件 / 頭像 / CAPTCHA",
  feature_storage_albums_enabled: "Storage / 相簿",
  feature_videos_enabled: "影音分享",
  feature_games_enabled: "遊戲區 / 西洋棋",
  feature_comfyui_enabled: "ComfyUI AI 產圖",
  feature_ai_agent_enabled: "AI Agent / LLM",
  feature_economy_enabled: "基本積分系統",
  feature_points_chain_enabled: "PointsChain 私有鏈",
  feature_trading_enabled: "積分交易所",
  feature_personalization_enabled: "個人外觀覆寫",
  feature_social_search_enabled: "社交 / 搜尋",
  feature_advanced_security_enabled: "進階安全",
  feature_privacy_uploads_enabled: "隱私分級上傳 / E2EE",
  feature_experiments_enabled: "實驗區",
};

const FEATURE_DEPENDENCY_RULES = {
  feature_storage_albums_enabled: {
    required: ["feature_privacy_uploads_enabled"],
    description: "Storage / 相簿需要先有雲端硬碟父功能。",
  },
  feature_trading_enabled: {
    required: ["feature_economy_enabled", "feature_points_chain_enabled"],
    description: "積分交易所必須依附在基本積分與 PointsChain 私有鏈上。",
  },
  feature_points_chain_enabled: {
    recommended: ["feature_economy_enabled"],
    description: "PointsChain 私有鏈通常以基本積分系統作為站內財務入口。",
  },
  feature_videos_enabled: {
    recommended: ["feature_privacy_uploads_enabled", "feature_economy_enabled", "feature_points_chain_enabled"],
    description: "影音若搭配雲端硬碟、基本積分與 PointsChain，才有上傳、保存與打賞等完整服務。",
  },
  feature_comfyui_enabled: {
    recommended: ["feature_privacy_uploads_enabled"],
    description: "ComfyUI 若搭配雲端硬碟，可直接保存與分享產圖結果。",
  },
  feature_ai_agent_enabled: {
    recommended: ["feature_chat_enabled"],
    description: "AI Agent 可獨立使用；若搭配聊天室，未來可延伸成對話摘要與輔助回覆。",
  },
  feature_chat_enabled: {
    recommended: ["feature_attachments_enabled", "feature_reports_enabled", "feature_reports_notifications_enabled"],
    description: "聊天室完整體驗通常會搭配附件、檢舉與通知。",
  },
  feature_community_enabled: {
    recommended: ["feature_attachments_enabled", "feature_reports_enabled", "feature_reports_notifications_enabled"],
    description: "討論區完整體驗通常會搭配附件、檢舉與通知。",
  },
  feature_appeals_enabled: {
    recommended: ["feature_accounts_enabled", "feature_violation_center_enabled", "feature_reports_notifications_enabled"],
    description: "申覆流程通常會搭配帳號管理、違規中心與通知。",
  },
  feature_violation_center_enabled: {
    recommended: ["feature_accounts_enabled"],
    description: "違規中心通常和帳號管理一起使用。",
  },
  feature_reports_enabled: {
    recommended: ["feature_accounts_enabled", "feature_reports_notifications_enabled"],
    description: "檢舉審核通常會搭配帳號管理與通知。",
  },
  feature_reports_notifications_enabled: {
    recommended: ["feature_accounts_enabled"],
    description: "通知中心通常由帳號管理模組承載。",
  },
  feature_identity_governance_enabled: {
    recommended: ["feature_accounts_enabled"],
    description: "身份治理欄位通常和帳號管理一起開。",
  },
  feature_account_security_enabled: {
    recommended: ["feature_accounts_enabled"],
    description: "帳號安全強化通常和帳號管理一起開。",
  },
  feature_member_governance_enabled: {
    recommended: ["feature_accounts_enabled"],
    description: "會員治理頁面通常掛在帳號管理底下。",
  },
};

const FEATURE_MINIMUM_BUNDLE_FEATURES = [
  "feature_accounts_enabled",
  "feature_audit_log_enabled",
  "feature_system_health_enabled",
  "feature_health_center_enabled",
  "feature_server_modes_enabled",
  "feature_snapshot_restore_enabled",
  "feature_reports_notifications_enabled",
];

const FEATURE_SETTING_GROUPS = [
  {
    key: "ops",
    title: "維運與安全",
    subtitle: "帳號、稽核、健康、伺服器模式、snapshot 與安全護欄。",
    defaultOpen: true,
    features: [
      "feature_accounts_enabled",
      "feature_audit_log_enabled",
      "feature_system_health_enabled",
      "feature_health_center_enabled",
      "feature_server_modes_enabled",
      "feature_snapshot_restore_enabled",
      "feature_account_security_enabled",
      "feature_advanced_security_enabled",
    ],
  },
  {
    key: "governance",
    title: "治理、申覆與通知",
    subtitle: "違規、罰單、申覆、檢舉、會員治理與站內通知。",
    defaultOpen: true,
    features: [
      "feature_violation_center_enabled",
      "feature_appeals_enabled",
      "feature_reports_enabled",
      "feature_reports_notifications_enabled",
      "feature_identity_governance_enabled",
      "feature_member_governance_enabled",
    ],
  },
  {
    key: "social",
    title: "社交與內容互動",
    subtitle: "聊天、討論區、附件、搜尋與使用者互動。",
    features: [
      "feature_chat_enabled",
      "feature_community_enabled",
      "feature_social_search_enabled",
      "feature_attachments_enabled",
    ],
  },
  {
    key: "storage",
    title: "儲存與媒體",
    subtitle: "隱私分級上傳、Storage / 相簿與影音分享。",
    features: [
      "feature_privacy_uploads_enabled",
      "feature_storage_albums_enabled",
      "feature_videos_enabled",
    ],
  },
  {
    key: "economy",
    title: "積分、PointsChain 與交易",
    subtitle: "基本積分、私有鏈與積分交易所。",
    features: [
      "feature_economy_enabled",
      "feature_points_chain_enabled",
      "feature_trading_enabled",
    ],
  },
  {
    key: "heavy",
    title: "可選重型與實驗模組",
    subtitle: "遊戲、ComfyUI、實驗區與大型 UI / 個人化模組；不屬於最低維運。",
    features: [
      "feature_games_enabled",
      "feature_experiments_enabled",
      "feature_comfyui_enabled",
      "feature_ai_agent_enabled",
      "feature_forum_core_enabled",
      "feature_ui_rebuild_enabled",
      "feature_personalization_enabled",
    ],
  },
];

const FEATURE_SERVICE_BUNDLES = [
  {
    key: "all-enabled",
    label: "全開",
    category: "完整前台",
    description: "所有功能開關全部打開。",
    features: FEATURE_SETTING_KEYS,
    replace: true,
  },
  {
    key: "ops-minimum",
    label: "維運骨架",
    category: "維運",
    description: "新版維運骨架：帳號、Audit、健康監控、Server Mode、Snapshot 與通知。",
    features: FEATURE_MINIMUM_BUNDLE_FEATURES,
    replace: true,
  },
  {
    key: "minimum-ops",
    label: "最低維運",
    category: "維運",
    description: "只保留帳號、Audit、健康燈、Server Mode、Snapshot 與通知等最小維運骨架。",
    features: FEATURE_MINIMUM_BUNDLE_FEATURES,
    replace: true,
  },
  {
    key: "safe-community",
    label: "安全社群",
    category: "社群",
    description: "社群互動加上檢舉、申覆、違規與帳號安全，適合正式對外開放留言與貼文。",
    features: [
      ...FEATURE_MINIMUM_BUNDLE_FEATURES,
      "feature_chat_enabled",
      "feature_community_enabled",
      "feature_attachments_enabled",
      "feature_reports_enabled",
      "feature_appeals_enabled",
      "feature_violation_center_enabled",
      "feature_account_security_enabled",
      "feature_social_search_enabled",
    ],
    replace: true,
  },
  {
    key: "raspberry-lite",
    label: "Raspberry 套餐",
    category: "低資源",
    description: "輕量主機預設：保留帳號、社群、附件、雲端硬碟、遊戲與基本積分；關閉 ComfyUI、影音、PointsChain 私有鏈與交易等較吃 CPU / I/O / 長連線的模組。",
    features: [
      ...FEATURE_MINIMUM_BUNDLE_FEATURES,
      "feature_accounts_enabled",
      "feature_chat_enabled",
      "feature_community_enabled",
      "feature_appeals_enabled",
      "feature_violation_center_enabled",
      "feature_reports_enabled",
      "feature_reports_notifications_enabled",
      "feature_attachments_enabled",
      "feature_privacy_uploads_enabled",
      "feature_storage_albums_enabled",
      "feature_economy_enabled",
      "feature_games_enabled",
      "feature_social_search_enabled",
      "feature_account_security_enabled",
      "feature_advanced_security_enabled",
    ],
    replace: true,
  },
  {
    key: "low-resource",
    label: "低資源完整前台",
    category: "低資源",
    description: "低端設備可用的前台組合：關閉 ComfyUI、影音、交易所與 PointsChain，保留社群、儲存、遊戲、基本積分與治理安全。",
    features: [
      ...FEATURE_MINIMUM_BUNDLE_FEATURES,
      "feature_chat_enabled",
      "feature_community_enabled",
      "feature_appeals_enabled",
      "feature_violation_center_enabled",
      "feature_reports_enabled",
      "feature_attachments_enabled",
      "feature_privacy_uploads_enabled",
      "feature_storage_albums_enabled",
      "feature_economy_enabled",
      "feature_games_enabled",
      "feature_social_search_enabled",
      "feature_account_security_enabled",
      "feature_advanced_security_enabled",
    ],
    replace: true,
  },
  {
    key: "accounts-suite",
    label: "帳號治理整套",
    category: "治理",
    description: "帳號、違規、治理、通知、申覆一起開。",
    features: [
      "feature_accounts_enabled",
      "feature_identity_governance_enabled",
      "feature_account_security_enabled",
      "feature_member_governance_enabled",
      "feature_violation_center_enabled",
      "feature_reports_notifications_enabled",
      "feature_appeals_enabled",
      "feature_reports_enabled",
    ],
  },
  {
    key: "creator-media",
    label: "創作者影音",
    category: "內容",
    description: "創作者前台：儲存、影音、附件、檢舉通知、基本積分與 PointsChain 打賞結算。",
    features: [
      "feature_accounts_enabled",
      "feature_reports_notifications_enabled",
      "feature_reports_enabled",
      "feature_attachments_enabled",
      "feature_privacy_uploads_enabled",
      "feature_storage_albums_enabled",
      "feature_videos_enabled",
      "feature_economy_enabled",
      "feature_points_chain_enabled",
    ],
  },
  {
    key: "community-suite",
    label: "社群互動整套",
    category: "社群",
    description: "聊天、討論區、附件、檢舉與通知一起開。",
    features: [
      "feature_chat_enabled",
      "feature_community_enabled",
      "feature_attachments_enabled",
      "feature_reports_enabled",
      "feature_reports_notifications_enabled",
    ],
  },
  {
    key: "drive-suite",
    label: "雲端硬碟整套",
    category: "內容",
    description: "隱私分級上傳加 Storage / 相簿一起開。",
    features: [
      "feature_privacy_uploads_enabled",
      "feature_storage_albums_enabled",
    ],
  },
  {
    key: "video-suite",
    label: "影音分享整套",
    category: "內容",
    description: "影音、雲端硬碟、基本積分與 PointsChain 一起開。",
    features: [
      "feature_videos_enabled",
      "feature_privacy_uploads_enabled",
      "feature_economy_enabled",
      "feature_points_chain_enabled",
    ],
  },
  {
    key: "points-chain-rc1",
    label: "PointsChain RC1",
    category: "積分鏈",
    description: "RC1 私有鏈營運組合：帳號、稽核、健康、治理、申覆、通知、基本積分與 PointsChain，不開交易所。",
    features: [
      ...FEATURE_MINIMUM_BUNDLE_FEATURES,
      "feature_economy_enabled",
      "feature_points_chain_enabled",
      "feature_violation_center_enabled",
      "feature_appeals_enabled",
      "feature_reports_enabled",
      "feature_identity_governance_enabled",
      "feature_member_governance_enabled",
      "feature_account_security_enabled",
      "feature_advanced_security_enabled",
    ],
    replace: true,
  },
  {
    key: "exchange-ops",
    label: "交易所營運",
    category: "積分鏈",
    description: "在 PointsChain RC1 上加開積分交易所，適合交易所相關 QA 或營運測試。",
    features: [
      ...FEATURE_MINIMUM_BUNDLE_FEATURES,
      "feature_economy_enabled",
      "feature_points_chain_enabled",
      "feature_trading_enabled",
      "feature_violation_center_enabled",
      "feature_appeals_enabled",
      "feature_reports_enabled",
      "feature_identity_governance_enabled",
      "feature_member_governance_enabled",
      "feature_account_security_enabled",
      "feature_advanced_security_enabled",
    ],
    replace: true,
  },
  {
    key: "ai-suite",
    label: "AI 產圖整套",
    category: "重型運算",
    description: "ComfyUI 加雲端硬碟保存流程一起開。",
    features: [
      "feature_comfyui_enabled",
      "feature_privacy_uploads_enabled",
    ],
  },
  {
    key: "economy-suite",
    label: "積分交易整套",
    category: "積分鏈",
    description: "基本積分、PointsChain 私有鏈與積分交易所一起開。",
    features: [
      "feature_economy_enabled",
      "feature_points_chain_enabled",
      "feature_trading_enabled",
    ],
  },
  {
    key: "full-user",
    label: "一般前台完整體驗",
    category: "完整前台",
    description: "一般使用者完整體驗：社群、儲存、影音、遊戲、AI、個人化、積分鏈與交易所。",
    features: [
      "feature_chat_enabled",
      "feature_community_enabled",
      "feature_attachments_enabled",
      "feature_reports_enabled",
      "feature_reports_notifications_enabled",
      "feature_appeals_enabled",
      "feature_violation_center_enabled",
      "feature_privacy_uploads_enabled",
      "feature_storage_albums_enabled",
      "feature_videos_enabled",
      "feature_games_enabled",
      "feature_comfyui_enabled",
      "feature_economy_enabled",
      "feature_points_chain_enabled",
      "feature_trading_enabled",
      "feature_personalization_enabled",
      "feature_social_search_enabled",
      "feature_account_security_enabled",
    ],
    replace: true,
  },
];

function featureSettingInputId(key) {
  return "s-" + key.replaceAll("_", "-");
}

function featureSettingLabel(key) {
  return FEATURE_SETTING_LABELS[key] || key;
}

function featureDependencyHint(key) {
  const rule = FEATURE_DEPENDENCY_RULES[key] || {};
  const parts = [];
  if (Array.isArray(rule.required) && rule.required.length) {
    parts.push(`必須先開：${rule.required.map(featureSettingLabel).join("、")}`);
  }
  if (Array.isArray(rule.recommended) && rule.recommended.length) {
    parts.push(`建議搭配：${rule.recommended.map(featureSettingLabel).join("、")}`);
  }
  return parts.join("；");
}

function renderFeatureSwitchGroups() {
  const host = $("feature-switch-groups");
  if (!host) return;
  host.innerHTML = FEATURE_SETTING_GROUPS.map((group) => {
    const rows = (group.features || []).filter((key) => FEATURE_SETTING_KEYS.includes(key)).map((key) => {
      const hint = featureDependencyHint(key);
      return `
        <label class="settings-feature-row" for="${sanitize(featureSettingInputId(key))}">
          <input type="checkbox" id="${sanitize(featureSettingInputId(key))}" data-feature-key="${sanitize(key)}" />
          <span class="settings-feature-row-main">
            <span class="settings-feature-row-title">${sanitize(featureSettingLabel(key))}</span>
            ${hint ? `<span class="settings-feature-row-hint">${sanitize(hint)}</span>` : ""}
          </span>
        </label>`;
    }).join("");
    return `
      <details class="drive-collapsible-panel settings-feature-group" ${group.defaultOpen ? "open" : ""}>
        <summary>
          <div>
            <div class="drive-card-title">${sanitize(group.title)}</div>
            <div class="drive-card-sub">${sanitize(group.subtitle || "")}</div>
          </div>
        </summary>
        <div class="drive-collapsible-body settings-feature-group-body">
          <div class="settings-feature-group-grid">${rows}</div>
          <div class="settings-feature-group-actions">
            <button class="btn btn-sm" type="button" data-feature-group-action="on" data-feature-group-key="${sanitize(group.key)}">全開此群組</button>
            <button class="btn btn-sm" type="button" data-feature-group-action="off" data-feature-group-key="${sanitize(group.key)}">全關此群組</button>
          </div>
        </div>
      </details>`;
  }).join("");
  host.querySelectorAll("input[data-feature-key]").forEach((input) => {
    input.addEventListener("change", () => {
      clearSettingsStatus();
      renderFeatureAdvisories();
    });
  });
  host.querySelectorAll("[data-feature-group-action]").forEach((button) => {
    button.addEventListener("click", () => {
      setFeatureGroupState(button.dataset.featureGroupKey || "", button.dataset.featureGroupAction === "on");
    });
  });
}

function setFeatureGroupState(groupKey, enabled) {
  const group = FEATURE_SETTING_GROUPS.find((item) => item.key === groupKey);
  if (!group) return;
  (group.features || []).forEach((key) => {
    const el = $(featureSettingInputId(key));
    if (el) el.checked = !!enabled;
  });
  renderFeatureAdvisories();
  setSettingsStatus(`${enabled ? "已全開" : "已全關"}「${group.title}」群組，記得再按「儲存設定」才會真正寫入。`, null);
}

function setSettingsStatus(text = "", ok = null, options = {}) {
  const el = $("settings-msg");
  if (!el) return;
  if (settingsStatusAutoClearTimer) {
    clearTimeout(settingsStatusAutoClearTimer);
    settingsStatusAutoClearTimer = null;
  }
  if (!text) {
    el.textContent = "";
    el.style.display = "none";
    return;
  }
  el.style.display = "block";
  el.textContent = text;
  el.style.color = ok === true ? "#4caf50" : ok === false ? "#ff4f6d" : "#ffb74d";
  const autoClearMs = Number(options.autoClearMs || 0);
  scheduleInlineMessageClear(el, text, ok, autoClearMs > 0 ? { duration: autoClearMs } : { persistent: ok === null });
  if (autoClearMs > 0) {
    const expectedText = text;
    settingsStatusAutoClearTimer = setTimeout(() => {
      if ($("settings-msg")?.textContent === expectedText) clearSettingsStatus();
    }, autoClearMs);
  }
}

function clearSettingsStatus() {
  if (settingsStatusAutoClearTimer) {
    clearTimeout(settingsStatusAutoClearTimer);
    settingsStatusAutoClearTimer = null;
  }
  setSettingsStatus("");
}

function formatFeatureAdvisoryLine(item) {
  if (!item) return "";
  const parts = [];
  if (Array.isArray(item.missingRequired) && item.missingRequired.length) {
    parts.push(`缺少父功能：${item.missingRequired.map(featureSettingLabel).join("、")}`);
  }
  if (Array.isArray(item.missingRecommended) && item.missingRecommended.length) {
    parts.push(`建議一起開：${item.missingRecommended.map(featureSettingLabel).join("、")}`);
  }
  return parts.length ? `${item.feature}（${parts.join("；")}）` : item.feature;
}

function featureToggleValue(key) {
  return !!$(featureSettingInputId(key))?.checked;
}

function buildFeatureAdvisories() {
  return FEATURE_SETTING_KEYS.flatMap((key) => {
    if (!featureToggleValue(key)) return [];
    const rule = FEATURE_DEPENDENCY_RULES[key];
    if (!rule) return [];
    const missingRequired = (rule.required || []).filter((dep) => !featureToggleValue(dep));
    const missingRecommended = (rule.recommended || []).filter((dep) => !featureToggleValue(dep));
    if (!missingRequired.length && !missingRecommended.length) return [];
    return [{
      feature: featureSettingLabel(key),
      description: rule.description || "",
      missingRequired,
      missingRecommended,
    }];
  });
}

function renderFeatureAdvisories() {
  const wrap = $("feature-advisory-list");
  if (!wrap) return;
  const advisories = buildFeatureAdvisories();
  if (!advisories.length) {
    wrap.innerHTML = `<div class="settings-feature-advisory ok">目前已勾選的功能沒有缺少父功能；若要一次打開整套服務，可用上方功能套餐。</div>`;
    return;
  }
  wrap.innerHTML = advisories.map((item) => {
    const required = item.missingRequired.length
      ? `<div><strong>還缺父功能：</strong>${item.missingRequired.map(featureSettingLabel).join("、")}</div>`
      : "";
    const recommended = item.missingRecommended.length
      ? `<div><strong>完整服務建議一併開啟：</strong>${item.missingRecommended.map(featureSettingLabel).join("、")}</div>`
      : "";
    const tone = item.missingRequired.length ? "warn" : "info";
    return `
      <div class="settings-feature-advisory ${tone}">
        <div><strong>${sanitize(item.feature)}</strong></div>
        ${item.description ? `<div>${sanitize(item.description)}</div>` : ""}
        ${required}
        ${recommended}
      </div>
    `;
  }).join("");
}

function renderFeatureBundleToolbar() {
  const toolbar = $("feature-bundle-toolbar");
  if (!toolbar) return;
  const select = $("feature-bundle-select");
  const apply = $("feature-bundle-apply");
  if (!select || !apply) return;
  const previous = select.value || "";
  const grouped = new Map();
  FEATURE_SERVICE_BUNDLES.forEach((bundle) => {
    const category = bundle.category || "其他";
    if (!grouped.has(category)) grouped.set(category, []);
    grouped.get(category).push(bundle);
  });
  select.innerHTML = `<option value="">選擇功能套餐</option>` + Array.from(grouped.entries()).map(([category, bundles]) => `
    <optgroup label="${sanitize(category)}">
      ${bundles.map((bundle) => `<option value="${sanitize(bundle.key)}">${sanitize(bundle.label)}</option>`).join("")}
    </optgroup>
  `).join("");
  if (previous && FEATURE_SERVICE_BUNDLES.some((bundle) => bundle.key === previous)) select.value = previous;

  const renderPreview = () => {
    const preview = $("feature-bundle-preview");
    if (!preview) return;
    const bundle = FEATURE_SERVICE_BUNDLES.find((item) => item.key === select.value);
    if (!bundle) {
      preview.innerHTML = `<div class="settings-feature-advisory info">請先選擇功能套餐。替換型套餐會重設整組功能；加開型套餐只會打開相關功能，不會關閉其他模組。</div>`;
      apply.disabled = true;
      return;
    }
    const mode = bundle.replace === true ? "替換目前整組功能開關" : "在目前設定上加開這些功能";
    const features = Array.from(new Set(bundle.features || [])).map(featureSettingLabel).join("、");
    preview.innerHTML = `
      <div class="settings-feature-advisory info">
        <div><strong>${sanitize(bundle.label)}</strong> · ${sanitize(bundle.category || "其他")} · ${sanitize(mode)}</div>
        <div>${sanitize(bundle.description || "")}</div>
        <div style="margin-top:.35rem;color:var(--muted);">${sanitize(features || "無功能項目")}</div>
      </div>`;
    apply.disabled = false;
  };

  select.onchange = renderPreview;
  apply.onclick = () => {
    const bundle = FEATURE_SERVICE_BUNDLES.find((item) => item.key === select.value);
    if (!bundle) return;
    const enabledFeatures = new Set(bundle.features || []);
    if (bundle.replace === true) {
      FEATURE_SETTING_KEYS.forEach((key) => {
        const el = $(featureSettingInputId(key));
        if (el) el.checked = enabledFeatures.has(key);
      });
    } else {
      enabledFeatures.forEach((key) => {
        const el = $(featureSettingInputId(key));
        if (el) el.checked = true;
      });
    }
    renderFeatureAdvisories();
    renderPreview();
    const actionVerb = bundle.replace === true ? "已切換為" : "已套用";
    setSettingsStatus(`${actionVerb}「${bundle.label}」套餐，記得再按「儲存設定」才會真正寫入。`, null);
  };
  renderPreview();
}

function bindSettingsAssistants() {
  const settingsForm = $("settings-form");
  if (!settingsForm || settingsForm.dataset.settingsAssistantsBound === "1") return;
  settingsForm.dataset.settingsAssistantsBound = "1";
  renderFeatureSwitchGroups();
  renderFeatureBundleToolbar();
  renderFeatureAdvisories();
  document.querySelectorAll("[id^='s-']").forEach((el) => {
    el.addEventListener("change", () => {
      clearSettingsStatus();
      if (FEATURE_SETTING_KEYS.includes(el.id.replace(/^s-/, "").replaceAll("-", "_"))) renderFeatureAdvisories();
    });
    if (el.matches("input[type='text'], input[type='number'], input[type='password'], input[type='url'], textarea")) {
      el.addEventListener("input", clearSettingsStatus);
    }
  });
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  const decimals = index === 0 ? 0 : (size >= 100 ? 0 : 1);
  return `${size.toFixed(decimals)} ${units[index]}`;
}

function formatDurationSeconds(seconds) {
  const value = Number(seconds || 0);
  if (!Number.isFinite(value) || value <= 0) return "0s";
  if (value < 60) return `${Math.round(value)}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  if (value < 86400) return `${Math.round(value / 3600)}h`;
  return `${Math.round(value / 86400)}d`;
}

const SECURITY_CONTROL_KEYS = [
  "maintenance_mode",
  "server_ssl_enabled",
  "audit_chain_enabled",
  "feature_audit_log_enabled",
  "ip_blocking_enabled",
  "login_violation_enabled",
  "rate_limit_violation_enabled",
  "root_ip_whitelist_enabled",
  "root_ip_whitelist",
  "browser_only_mode_enabled",
  "integrity_guard_enabled",
  "integrity_guard_strict_mode",
  "feature_economy_enabled",
  "feature_points_chain_enabled"
];
const SECURITY_THRESHOLD_KEYS = [
  "max_login_failures",
  "block_duration_minutes",
  "security_pending_chat_reports_threshold",
  "security_pending_appeals_threshold",
  "security_pending_moderation_proposals_threshold",
  "security_quarantined_files_threshold",
  "security_unknown_encrypted_files_threshold",
  "security_log_tail_lines"
];
const SECURITY_FIELD_LABELS = {
  maintenance_mode: "維護模式",
  server_ssl_enabled: "HTTPS / SSL",
  audit_chain_enabled: "審計 hash chain",
  feature_audit_log_enabled: "Audit log 查詢頁",
  ip_blocking_enabled: "錯誤登入鎖 IP",
  login_violation_enabled: "登入失敗寫入違規",
  rate_limit_violation_enabled: "速率限制寫入違規",
  root_ip_whitelist_enabled: "root IP 白名單",
  root_ip_whitelist: "root IP 白名單內容",
  browser_only_mode_enabled: "Browser-only 模式",
  integrity_guard_enabled: "Integrity Guard",
  integrity_guard_strict_mode: "Integrity strict mode",
  feature_economy_enabled: "基本積分系統",
  feature_points_chain_enabled: "PointsChain 私有鏈",
  feature_videos_enabled: "影音分享模組",
  max_login_failures: "登入失敗鎖定次數",
  block_duration_minutes: "封鎖時長（分鐘）",
  security_pending_chat_reports_threshold: "待審聊天室檢舉警戒",
  security_pending_appeals_threshold: "待審申覆警戒",
  security_pending_moderation_proposals_threshold: "待審治理提案警戒",
  security_quarantined_files_threshold: "隔離檔案警戒",
  security_unknown_encrypted_files_threshold: "未知加密檔案警戒",
  security_log_tail_lines: "log 顯示行數"
};
let securityProfiles = [];

function securityInputId(prefix, key) {
  return prefix + "-" + key.replaceAll("_", "-");
}

function findSecurityProfile(name) {
  const profileName = String(name || "");
  return securityProfiles.find((profile) => profile && profile.name === profileName) || null;
}

function profileKeysSummary(profile, key) {
  const data = profile && profile[key] && typeof profile[key] === "object" ? profile[key] : {};
  const keys = Object.keys(data);
  return keys.length
    ? keys.map((item) => `${SECURITY_FIELD_LABELS[item] || item}=${data[item] === true ? "開" : data[item] === false ? "關" : JSON.stringify(data[item])}`).join("，")
    : "未設定";
}

function collectSecurityProfileDraft(prefix = "security-profile") {
  const settings = {};
  SECURITY_CONTROL_KEYS.forEach((key) => {
    const el = $(securityInputId(prefix, key));
    if (!el) return;
    settings[key] = el.type === "checkbox" ? !!el.checked : el.value || "";
  });
  const thresholds = {};
  SECURITY_THRESHOLD_KEYS.forEach((key) => {
    const el = $(securityInputId(prefix, key));
    if (!el) return;
    const number = parseInt(el.value || "0", 10);
    thresholds[key] = Number.isFinite(number) ? number : 0;
  });
  return { settings, thresholds };
}

function fillSecurityProfileDraft(settings = {}, thresholds = {}, prefix = "security-profile") {
  SECURITY_CONTROL_KEYS.forEach((key) => {
    const el = $(securityInputId(prefix, key));
    if (!el) return;
    const value = Object.prototype.hasOwnProperty.call(settings, key) ? settings[key] : "";
    if (el.type === "checkbox") el.checked = !!value;
    else el.value = value ?? "";
  });
  SECURITY_THRESHOLD_KEYS.forEach((key) => {
    const el = $(securityInputId(prefix, key));
    if (!el) return;
    el.value = Object.prototype.hasOwnProperty.call(thresholds, key) ? thresholds[key] : 0;
  });
}

function renderSecurityProfilePreview(selectId, previewId) {
  const preview = $(previewId);
  const select = $(selectId);
  if (!preview || !select) return;
  const profile = findSecurityProfile(select.value);
  if (!profile) {
    preview.classList.remove("show");
    preview.innerHTML = "";
    return;
  }
  preview.classList.add("show");
  preview.innerHTML = `
    <div><strong>${sanitize(profile.label || profile.name || "")}</strong> ${profile.is_builtin ? "內建" : "自定義"}</div>
    <div>${sanitize(profile.description || "無描述")}</div>
    <div>安全開關：${sanitize(profileKeysSummary(profile, "settings"))}</div>
    <div>閾值：${sanitize(profileKeysSummary(profile, "thresholds"))}</div>
  `;
}

function applySecurityProfileDataToInputs(profile, prefix = "sc") {
  if (!profile) return;
  const settings = profile.settings && typeof profile.settings === "object" ? profile.settings : {};
  SECURITY_CONTROL_KEYS.forEach((key) => {
    if (!Object.prototype.hasOwnProperty.call(settings, key)) return;
    const el = $(securityInputId(prefix, key));
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!settings[key];
    else el.value = settings[key] ?? "";
  });
  SECURITY_THRESHOLD_KEYS.forEach((key) => {
    const source = Object.prototype.hasOwnProperty.call(settings, key)
      ? settings
      : (profile.thresholds && typeof profile.thresholds === "object" ? profile.thresholds : {});
    if (!Object.prototype.hasOwnProperty.call(source, key)) return;
    const el = $(securityInputId(prefix, key));
    if (el) el.value = source[key] ?? 0;
  });
}

function applySecurityProfileToInputs(profileName, prefix = "sc") {
  const profile = findSecurityProfile(profileName);
  applySecurityProfileDataToInputs(profile, prefix);
}

function previewSecurityProfileSelection(selectId, previewId, inputPrefix = "") {
  renderSecurityProfilePreview(selectId, previewId);
  if (inputPrefix) applySecurityProfileToInputs($(selectId)?.value, inputPrefix);
  const profile = findSecurityProfile($(selectId)?.value);
  const msg = selectId === "security-mode-select" ? $("security-controls-msg") : $("server-mode-status");
  if (msg && profile) {
    msg.textContent = `已預覽「${profile.label || profile.name}」的安全開關；按套用才會寫入伺服器。`;
    msg.style.color = "var(--muted)";
  }
}

function bindSecurityProfileSelect(selectId, previewId, inputPrefix = "") {
  const select = $(selectId);
  if (!select) return;
  select.onchange = () => previewSecurityProfileSelection(selectId, previewId, inputPrefix);
}

function populateProfileSelect(selectId, profiles, selectedMode) {
  const select = $(selectId);
  if (!select) return;
  const rows = Array.isArray(profiles) ? profiles : [];
  select.innerHTML = rows.map((profile) => `
    <option value="${sanitize(profile.name || "")}" ${profile.name === selectedMode ? "selected" : ""}>
      ${sanitize(profile.label || profile.name || "")}${profile.is_builtin ? " · builtin" : " · custom"}
    </option>
  `).join("");
  if (selectedMode && rows.some((profile) => profile.name === selectedMode)) {
    select.value = selectedMode;
  }
}

// Server Mode v2 mode -> banner color (matches PROFILE_MATRIX UI banner row).
const SERVER_MODE_COLORS = {
  production: "#4caf50",
  dev_ready: "#82b1ff",
  internal_test: "#ffb74d",
  test: "#26c6da",
  maintenance: "#ba68c8",
  incident_lockdown: "#ff4f6d",
  superweak: "#ff4f6d",
};

// Some toggles are good when ON, others are good when ON-with-WARN
// (e.g. maintenance_mode is operational — green when off, red when on).
// `expect` controls which side counts as healthy:
//   "on"  => ON is green, OFF is red
//   "off" => OFF is green, ON is red
function lightForToggle(label, value, expect) {
  const on = !!value;
  const ok = expect === "on" ? on : !on;
  return [label, on ? "啟用" : "關閉", ok ? "#4caf50" : "#ff4f6d"];
}

function renderSecuritySummary(sc) {
  const summary = $("security-center-summary");
  if (!summary) return;
  const anomaly = sc.anomaly || {};
  const readiness = sc.readiness || {};
  const audit = sc.audit_integrity || {};
  const mode = sc.mode || {};
  const settings = sc.settings || {};
  const signalCount = Array.isArray(anomaly.signals) ? anomaly.signals.length : 0;
  const auditEnabled = audit.enabled !== false;
  const modeName = mode.current_mode || "dev_ready";
  const modeColor = SERVER_MODE_COLORS[modeName] || "#82b1ff";

  const cards = [
    // Top-line health + readiness mirror the 健康度 dashboard so the
    // operator sees the same colours in both places.
    ["Readiness", readiness.status || "-", healthStatusColor(readiness.status)],
    ["Anomaly", anomaly.status || "ok", healthStatusColor(anomaly.status)],
    ["Signals", String(signalCount), signalCount ? "#ffb74d" : "#4caf50"],
    ["Audit Chain", auditEnabled ? (audit.ok ? "完整" : "異常") : "停用",
     auditEnabled ? (audit.ok ? "#4caf50" : "#ff4f6d") : "#9e9e9e"],
    ["Server Mode", modeName, modeColor],
    // Maintenance / browser-only: ON = ops in restricted state -> red.
    lightForToggle("維護模式", settings.maintenance_mode, "off"),
    lightForToggle("Browser-only", settings.browser_only_mode_enabled, "off"),
    // Defense toggles: ON = healthy, OFF = exposed -> red.
    lightForToggle("HTTPS / SSL", settings.server_ssl_enabled, "on"),
    lightForToggle("審計 chain", settings.audit_chain_enabled, "on"),
    lightForToggle("IP 封鎖", settings.ip_blocking_enabled, "on"),
    lightForToggle("登入暴力鎖", settings.login_violation_enabled, "on"),
    lightForToggle("速率限制", settings.rate_limit_violation_enabled, "on"),
    lightForToggle("Integrity Guard", settings.integrity_guard_enabled, "on"),
  ];
  summary.innerHTML = cards.map(([label, value, color]) => renderHealthMetric(label, value, color)).join("");
}

function renderServerOutput(output) {
  const box = $("security-server-output");
  if (!box) return;
  const rows = Array.isArray(output?.lines) ? output.lines : [];
  if (!rows.length) {
    box.textContent = "尚無伺服器輸出";
    return;
  }
  box.textContent = rows.map((row) => {
    const stream = row.stream || "stdout";
    const ts = row.timestamp || "";
    const line = row.line || "";
    return `[${ts}] ${stream}> ${line}`;
  }).join("\n");
  box.scrollTop = box.scrollHeight;
}

function securityTestMsg(text, ok = true) {
  const msg = $("security-test-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.className = text ? `msg show ${ok ? "ok" : "err"}` : "msg";
}

function securityTestKindLabel(kind) {
  return kind === "pentest"
    ? "滲透測試"
    : kind === "privilege"
      ? "越權測試"
      : kind === "functional"
        ? "全功能測試"
        : kind === "stress"
          ? "壓力測試"
          : (kind || "-");
}

function securityTestKindSummary(kind) {
  return kind === "pentest"
    ? "檢查滲透測試工具、HTTP 漏洞掃描與授權目標設定。"
    : kind === "privilege"
      ? "檢查角色邊界、提權與權限濫用流程。"
      : kind === "functional"
        ? "檢查隔離 runtime 內的全站功能 smoke。"
        : kind === "stress"
          ? "檢查受控壓測、fallback 與流量峰值。"
          : "尚無任務紀錄";
}

function securityTestPanelRefs(kind) {
  return {
    status: $(`security-${kind}-status`),
    fill: $(`security-${kind}-progress-fill`),
    label: $(`security-${kind}-progress-label`),
    detail: $(`security-${kind}-detail`),
    log: $(`security-${kind}-log`),
  };
}

function renderSecurityTestPanel(kind, job) {
  const refs = securityTestPanelRefs(kind);
  const { status, fill, label, detail, log } = refs;
  if (!status || !fill || !label || !detail || !log) return;
  const title = securityTestKindLabel(kind);
  if (!job) {
    status.textContent = `${title}：尚未執行`;
    status.style.color = "var(--muted)";
    fill.classList.remove("indeterminate");
    fill.style.width = "0%";
    label.textContent = "等待啟動";
    detail.textContent = securityTestKindSummary(kind);
    log.textContent = "等待測試輸出...";
    return;
  }
  const colorFor = (value) => value === "passed" ? "#4caf50" : value === "failed" ? "#ff4f6d" : "#ffb74d";
  const progress = Math.max(0, Math.min(100, Number(job.progress_percent ?? 0)));
  const running = job.status === "running";
  status.textContent = `${title}：${running ? "執行中" : job.status === "passed" ? "已通過" : job.status === "failed" ? "失敗" : (job.status || "-")}`;
  status.style.color = colorFor(job.status);
  fill.classList.toggle("indeterminate", running);
  fill.style.width = running ? `${Math.max(progress, 12)}%` : `${progress}%`;
  label.textContent = `${running ? "執行中" : "完成"} · ${Math.round(progress)}%`;
  const detailParts = [
    job.job_id ? `job=${job.job_id}` : "",
    job.started_at ? `started=${job.started_at}` : "",
    job.finished_at ? `finished=${job.finished_at}` : "",
    job.returncode == null ? "" : `rc=${job.returncode}`,
    job.report_dir ? `report=${job.report_dir}` : "",
    !job.report_dir && Array.isArray(job.report_artifacts) && job.report_artifacts.length ? `artifacts=${job.report_artifacts.join(", ")}` : "",
    job.log_path ? `log=${job.log_path}` : "",
    job.error ? `error=${job.error}` : "",
  ].filter(Boolean);
  detail.textContent = detailParts.join(" · ") || securityTestKindSummary(kind);
  log.textContent = (Array.isArray(job.log_tail) && job.log_tail.length)
    ? job.log_tail.join("\n")
    : (running ? "測試啟動中，等待輸出..." : "此任務目前沒有輸出。");
  log.scrollTop = log.scrollHeight;
}

function renderSecurityTestPanels(jobs) {
  const rows = Array.isArray(jobs) ? jobs : [];
  const latestByKind = new Map();
  rows.forEach((job) => {
    if (!job || !job.kind || latestByKind.has(job.kind)) return;
    latestByKind.set(job.kind, job);
  });
  ["pentest", "privilege", "functional", "stress"].forEach((kind) => {
    renderSecurityTestPanel(kind, latestByKind.get(kind) || null);
  });
}

function renderSecurityTestJobs(jobs) {
  const list = $("security-test-jobs");
  if (!list) return;
  const rows = Array.isArray(jobs) ? jobs : [];
  renderSecurityTestPanels(rows);
  if (!rows.length) {
    list.innerHTML = `<div class="drive-empty">尚無 root 啟動的測試任務</div>`;
    return;
  }
  const colorFor = (status) => status === "passed" ? "#4caf50" : status === "failed" ? "#ff4f6d" : "#ffb74d";
  const labelFor = (kind) => securityTestKindLabel(kind);
  list.innerHTML = rows.map((job) => `
    <div class="drive-file-row">
      <div style="min-width:0;flex:1;">
        <strong>${sanitize(labelFor(job.kind))} · <span style="color:${colorFor(job.status)};">${sanitize(job.status || "-")}</span></strong>
        <div class="drive-card-sub">${sanitize(job.started_at || "")}${job.finished_at ? " -> " + sanitize(job.finished_at) : ""}</div>
        <div class="economy-ledger-hash">${sanitize(job.job_id || "")}</div>
        <div class="drive-progress" aria-label="${sanitize(labelFor(job.kind))} progress">
          <div class="drive-progress-fill ${job.status === "running" ? "indeterminate" : ""}" style="width:${Math.max(0, Math.min(100, Number(job.progress_percent ?? 0)))}%;"></div>
        </div>
        <div class="drive-progress-label">${job.status === "running" ? "執行中" : "完成"} · ${Math.round(Number(job.progress_percent ?? 0))}%</div>
        <div class="drive-card-sub">report: ${sanitize(job.report_dir || (Array.isArray(job.report_artifacts) ? job.report_artifacts.join(", ") : "") || job.report_root || "-")}</div>
        <div class="drive-card-sub">log: ${sanitize(job.log_path || "-")}</div>
        <pre class="security-log-box security-log-pre" style="max-height:180px;margin-top:.45rem;">${sanitize((job.log_tail || []).join("\n") || "等待測試輸出...")}</pre>
      </div>
      <button class="btn" type="button" data-security-test-job="${sanitize(job.job_id || "")}">查詢</button>
    </div>
  `).join("");
  list.querySelectorAll("[data-security-test-job]").forEach((btn) => {
    btn.addEventListener("click", () => loadSecurityTestJob(btn.dataset.securityTestJob || ""));
  });
}

async function loadSecurityTestJobs() {
  if (currentUser !== "root") return;
  try {
    const csrf = await fetchCsrfToken();
    const target = $("security-pentest-target");
    if (target && !target.value) target.value = window.location.origin;
    const stressTarget = $("security-stress-target");
    if (stressTarget && !stressTarget.value) stressTarget.value = window.location.origin;
    const privilegeTarget = $("security-privilege-target");
    if (privilegeTarget && !privilegeTarget.value) privilegeTarget.value = window.location.origin;
    const res = await apiFetch(API + "/root/security-tests", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      securityTestMsg(json.msg || `測試任務讀取失敗（HTTP ${res.status}）`, false);
      return;
    }
    renderSecurityTestJobs(json.jobs || []);
    if ((json.jobs || []).some((job) => job.status === "running")) {
      clearTimeout(window.securityTestPollTimer);
      window.securityTestPollTimer = setTimeout(loadSecurityTestJobs, securityTestJobPollSeconds * 1000);
    }
  } catch (err) {
    securityTestMsg(`測試任務讀取失敗：${err.message || "請檢查伺服器連線"}`, false);
  }
}

async function loadSecurityTestJob(jobId) {
  if (!jobId) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/root/security-tests/${encodeURIComponent(jobId)}`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    securityTestMsg(json.msg || "任務查詢失敗", false);
    return;
  }
  renderSecurityTestPanel(json.job?.kind || "", json.job || null);
  securityTestMsg(`已刷新 ${securityTestKindLabel(json.job?.kind || "")} 任務 ${json.job?.job_id || ""}`, true);
}

async function startSecurityPentest() {
  if (currentUser !== "root") return;
  const target = $("security-pentest-target")?.value || window.location.origin;
  const payload = {
    target,
    only: $("security-pentest-only")?.value || "",
    skip: $("security-pentest-skip")?.value || "",
    tool_timeout_seconds: Number($("security-pentest-timeout")?.value || 180),
    i_own_this_target: !!$("security-pentest-own-target")?.checked,
  };
  securityTestMsg("滲透測試啟動中...", true);
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/root/security-tests/pentest", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    const ok = res.ok && !!json.ok;
    securityTestMsg(ok ? `滲透測試已啟動：${json.job?.job_id || ""}` : (json.msg || `滲透測試啟動失敗（HTTP ${res.status}）`), ok);
    if (ok) await loadSecurityTestJobs();
  } catch (err) {
    securityTestMsg(`滲透測試啟動失敗：${err.message || "請檢查伺服器連線"}`, false);
  }
}

async function startSecurityPrivilegeTest() {
  if (currentUser !== "root") return;
  const payload = {
    target: $("security-privilege-target")?.value || window.location.origin,
    destructive: !!$("security-privilege-destructive")?.checked,
  };
  securityTestMsg("越權測試啟動中...", true);
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/root/security-tests/privilege", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    const ok = res.ok && !!json.ok;
    securityTestMsg(ok ? `越權測試已啟動：${json.job?.job_id || ""}` : (json.msg || `越權測試啟動失敗（HTTP ${res.status}）`), ok);
    if (ok) await loadSecurityTestJobs();
  } catch (err) {
    securityTestMsg(`越權測試啟動失敗：${err.message || "請檢查伺服器連線"}`, false);
  }
}

async function startSecurityFunctionalSmoke() {
  if (currentUser !== "root") return;
  const payload = {
    port: Number($("security-functional-port")?.value || 50741),
    keep_runtime: !!$("security-functional-keep-runtime")?.checked,
  };
  securityTestMsg("全功能測試啟動中...", true);
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/root/security-tests/functional", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    const ok = res.ok && !!json.ok;
    securityTestMsg(ok ? `全功能測試已啟動：${json.job?.job_id || ""}` : (json.msg || `全功能測試啟動失敗（HTTP ${res.status}）`), ok);
    if (ok) await loadSecurityTestJobs();
  } catch (err) {
    securityTestMsg(`全功能測試啟動失敗：${err.message || "請檢查伺服器連線"}`, false);
  }
}

async function startSecurityStressTest() {
  if (currentUser !== "root") return;
  const payload = {
    target: $("security-stress-target")?.value || window.location.origin,
    requests: Number($("security-stress-requests")?.value || 200),
    concurrency: Number($("security-stress-concurrency")?.value || 20),
    paths: $("security-stress-paths")?.value || "",
  };
  securityTestMsg("壓力測試啟動中...", true);
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/root/security-tests/stress", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    const ok = res.ok && !!json.ok;
    securityTestMsg(ok ? `壓力測試已啟動：${json.job?.job_id || ""}` : (json.msg || `壓力測試啟動失敗（HTTP ${res.status}）`), ok);
    if (ok) await loadSecurityTestJobs();
  } catch (err) {
    securityTestMsg(`壓力測試啟動失敗：${err.message || "請檢查伺服器連線"}`, false);
  }
}

async function loadServerOutput() {
  if (!canRunRootManagementPoll(isSystemOverviewActive)) return;
  const csrf = await fetchCsrfToken();
  const res = await apiFetch(API + "/admin/server-output?limit=300", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    securityTestMsg(json.msg || "伺服器即時輸出讀取失敗", false);
    return;
  }
  renderServerOutput(json.server_output || {});
}

function startServerOutputPoll() {
  if (!canRunRootManagementPoll(isSystemOverviewActive)) return;
  if (serverOutputPollTimer) return;
  serverOutputPollTimer = setInterval(loadServerOutput, serverOutputRefreshSeconds * 1000);
}

function stopServerOutputPoll() {
  if (!serverOutputPollTimer) return;
  clearInterval(serverOutputPollTimer);
  serverOutputPollTimer = null;
}

function populateSecurityProfiles(profiles, selectedMode) {
  securityProfiles = Array.isArray(profiles) ? profiles : [];
  populateProfileSelect("security-mode-select", securityProfiles, selectedMode);
  populateProfileSelect("server-mode-select", securityProfiles, selectedMode);
  bindSecurityProfileSelect("security-mode-select", "security-mode-profile-preview", "sc");
  bindSecurityProfileSelect("server-mode-select", "server-mode-profile-preview", "s");
  renderSecurityProfilePreview("security-mode-select", "security-mode-profile-preview");
  renderSecurityProfilePreview("server-mode-select", "server-mode-profile-preview");
}

async function loadSecurityCenter() {
  if (!canRunRootManagementPoll(isSystemOverviewActive)) return;
  const csrf = await fetchCsrfToken();
  const res = await apiFetch(API + "/admin/security-center", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  const sc = json.security_center || {};
  if (!json.ok) {
    const summary = $("security-center-summary");
    if (summary) summary.innerHTML = renderHealthMetric("安全中心", json.msg || "讀取失敗", "#ff4f6d");
    return;
  }
  renderSecuritySummary(sc);
  const settings = sc.settings || {};
  SECURITY_CONTROL_KEYS.forEach((key) => {
    const el = $(securityInputId("sc", key));
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!settings[key];
    else el.value = settings[key] || "";
  });
  const thresholds = sc.thresholds || {};
  SECURITY_THRESHOLD_KEYS.forEach((key) => {
    const el = $(securityInputId("sc", key));
    if (el) el.value = thresholds[key] ?? 0;
  });
  const mode = sc.mode || {};
  populateSecurityProfiles(sc.profiles || [], mode.current_mode || "dev_ready");
  const modeStatus = $("security-mode-status");
  if (modeStatus) {
    const previous = mode.previous_mode ? `，上一個模式：${mode.previous_mode}` : "";
    const snapshot = mode.active_snapshot_id ? `，active snapshot：${mode.active_snapshot_id}` : "";
    modeStatus.textContent = `目前模式：${mode.current_mode || "dev_ready"}${previous}${snapshot}`;
    modeStatus.style.color = mode.current_mode === "superweak" ? "#ff4f6d" : "var(--muted)";
  }
  const auditBox = $("security-audit-entries");
  if (auditBox) {
    const rows = sc.audit_entries || [];
    auditBox.innerHTML = rows.length ? rows.map((e) => `
      <div class="security-log-row">
        <span style="color:#888;">${sanitize(e.timestamp || "")}</span>
        <span style="color:${e.success ? "#4caf50" : "#ff4f6d"};">${e.success ? "OK" : "FAIL"}</span>
        <span style="color:#e0e0e0;">${sanitize(e.action || "")}</span>
        <span style="color:#82b1ff;">${sanitize(e.actor || "")}</span>
        <span style="color:#888;">${sanitize(e.details || "")}</span>
      </div>
    `).join("") : "<p style='color:var(--muted);'>暫無審計資料</p>";
  }
  const logBox = $("security-server-log");
  if (logBox) {
    const log = sc.server_log || {};
    logBox.textContent = log.exists ? (log.lines || []).join("\n") : `server log 不存在：${log.path || "-"}`;
  }
  renderServerOutput(sc.server_output || {});
  await loadSecurityTestJobs();
  startServerOutputPoll();
}

function setSecuritySaveStatus(message, ok = true, targetId = "") {
  const color = ok ? "#4caf50" : "#ff4f6d";
  ["security-save-status", targetId].filter(Boolean).forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.textContent = message || "";
    el.style.color = color;
  });
}

async function saveSecurityCenterControls() {
  const btn = $("security-controls-save-btn");
  if (btn) btn.disabled = true;
  setSecuritySaveStatus("正在儲存安全開關...", true, "security-controls-msg");
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const payload = {};
    SECURITY_CONTROL_KEYS.forEach((key) => {
      const el = $(securityInputId("sc", key));
      if (!el) return;
      payload[key] = el.type === "checkbox" ? !!el.checked : el.value || "";
    });
    const res = await apiFetch(API + "/admin/security-center/controls", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    const message = json.ok ? (json.msg || "安全機制開關已儲存") : (json.msg || `安全開關儲存失敗（HTTP ${res.status}）`);
    setSecuritySaveStatus(message, !!json.ok, "security-controls-msg");
    if (json.ok) await loadSecurityCenter();
  } catch (err) {
    setSecuritySaveStatus(`安全開關儲存失敗：${err.message || "請求失敗"}`, false, "security-controls-msg");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function saveSecurityThresholds() {
  const btn = $("security-thresholds-save-btn");
  if (btn) btn.disabled = true;
  setSecuritySaveStatus("正在儲存安全閾值...", true, "security-thresholds-msg");
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const payload = {};
    SECURITY_THRESHOLD_KEYS.forEach((key) => {
      const el = $(securityInputId("sc", key));
      if (el) payload[key] = parseInt(el.value || "0", 10);
    });
    const res = await apiFetch(API + "/admin/security-center/thresholds", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    const message = json.ok ? (json.msg || "安全閾值已儲存") : (json.msg || `安全閾值儲存失敗（HTTP ${res.status}）`);
    setSecuritySaveStatus(message, !!json.ok, "security-thresholds-msg");
    if (json.ok) await loadSecurityCenter();
  } catch (err) {
    setSecuritySaveStatus(`安全閾值儲存失敗：${err.message || "請求失敗"}`, false, "security-thresholds-msg");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function applySecurityMode() {
  const target = $("security-mode-select")?.value || "dev_ready";
  const confirmText = $("security-mode-confirm")?.value || "";
  const notes = $("security-mode-notes")?.value || "";
  if (target === "production" && confirmText !== "GO_LIVE") {
    alert("進入 production 上線模式必須輸入 GO_LIVE");
    return;
  }
  if (target === "superweak" && confirmText !== "ENABLE_SUPERWEAK") {
    alert("進入 superweak 必須輸入 ENABLE_SUPERWEAK");
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/server-mode", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ mode: target, confirm: confirmText, notes })
  });
  const json = await res.json().catch(() => ({}));
  const status = $("security-mode-status");
  if (status) {
    status.textContent = json.ok ? "伺服器模式 / 安全設定檔已套用" : (json.msg || "套用失敗");
    status.style.color = json.ok ? "#4caf50" : "#ff4f6d";
  }
  if (json.ok) {
    if (json.profile) {
      applySecurityProfileDataToInputs(json.profile, "sc");
      applySecurityProfileDataToInputs(json.profile, "s");
    }
    if ($("security-mode-confirm")) $("security-mode-confirm").value = "";
    await loadSecurityCenter();
    await loadServerMode();
    await loadSettings();
  }
}

function loadCurrentSecurityProfileDraft() {
  const current = collectSecurityProfileDraft("sc");
  fillSecurityProfileDraft(current.settings, current.thresholds, "security-profile");
  const msg = $("security-profile-msg");
  if (msg) {
    msg.textContent = "已帶入目前安全開關與閾值；請填名稱後用表單調整並儲存。";
    msg.style.color = "var(--muted)";
  }
}

async function saveSecurityProfile() {
  const { settings, thresholds } = collectSecurityProfileDraft("security-profile");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const payload = {
    name: $("security-profile-name")?.value || "",
    label: $("security-profile-label")?.value || "",
    description: $("security-profile-description")?.value || "",
    settings,
    thresholds
  };
  const res = await apiFetch(API + "/admin/security-center/profiles", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(payload)
  });
  const json = await res.json().catch(() => ({}));
  const msg = $("security-profile-msg");
  if (msg) {
    msg.textContent = json.ok ? "自定義安全設定檔已儲存" : (json.msg || "儲存失敗");
    msg.style.color = json.ok ? "#4caf50" : "#ff4f6d";
  }
  if (json.ok) {
    const savedName = json.profile?.name || payload.name;
    await loadSecurityCenter();
    await loadServerMode();
    ["security-mode-select", "server-mode-select"].forEach((id) => {
      const select = $(id);
      if (select && savedName) select.value = savedName;
    });
    renderSecurityProfilePreview("security-mode-select", "security-mode-profile-preview");
    renderSecurityProfilePreview("server-mode-select", "server-mode-profile-preview");
  }
}

function healthStatusColor(status) {
  if (status === "critical") return "#ff4f6d";
  if (status === "degraded" || status === "warning") return "#ffb74d";
  return "#4caf50";
}

function renderHealthMetric(label, value, color = "#82b1ff") {
  return `
    <div class="health-metric-card">
      <div class="health-metric-label">${sanitize(label)}</div>
      <div class="health-metric-value" style="color:${color};">${sanitize(value)}</div>
    </div>
  `;
}

function renderHealthRows(rows) {
  if (!rows.length) return `<p class="health-empty">目前沒有需要顯示的項目</p>`;
  return rows.map((row) => `
    <div class="health-row">
      <div class="health-row-main">
        <strong>${sanitize(row.label)}</strong>
        ${row.detail ? `<small>${sanitize(row.detail)}</small>` : ""}
      </div>
      <span class="health-row-value" style="color:${row.color || "#82b1ff"};">${sanitize(row.value)}</span>
    </div>
  `).join("");
}

async function loadPlaywrightCiHealth() {
  const host = $("server-health-playwright-ci");
  if (!host || !currentUser || currentRole !== "super_admin") return;
  host.innerHTML = `<p class="health-empty">讀取 Playwright CI 中...</p>`;
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/admin/health/playwright-ci", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    const ci = json.playwright_ci || {};
    const latest = ci.latest || {};
    const color = ci.status === "success" ? "#4caf50" : ci.status === "unreachable" ? "#ffb74d" : "#ff4f6d";
    const rows = [
      {
        label: "Workflow",
        value: ci.status || "unknown",
        detail: `${ci.repo || "-"} · ${ci.branch || "-"} · ${ci.workflow_file || "-"}`,
        color,
      },
      {
        label: "Latest run",
        value: latest.conclusion || latest.status || "-",
        detail: `${latest.display_title || latest.name || "-"} · ${latest.event || "-"} · ${latest.updated_at || latest.created_at || "-"}`,
        color: latest.conclusion === "success" ? "#4caf50" : latest.status && latest.status !== "completed" ? "#ffb74d" : color,
      },
      {
        label: "Auth",
        value: ci.auth_configured ? "token" : "public API",
        detail: ci.workflow_present ? "workflow file exists locally" : "workflow file missing locally",
        color: ci.workflow_present ? "#82b1ff" : "#ff4f6d",
      },
    ];
    host.innerHTML = renderHealthRows(rows);
    if (latest.html_url) {
      host.insertAdjacentHTML("beforeend", `<div class="health-row"><div class="health-row-main"><strong>GitHub Actions</strong><small>${sanitize(latest.html_url)}</small></div><a class="btn btn-sm" href="${sanitize(latest.html_url)}" target="_blank" rel="noopener">開啟</a></div>`);
    }
    if (ci.msg && ci.status !== "success") {
      host.insertAdjacentHTML("beforeend", `<div class="drive-card-sub" style="color:${color};margin-top:.35rem;">${sanitize(ci.msg)}</div>`);
    }
  } catch (err) {
    host.innerHTML = renderHealthRows([{
      label: "Playwright CI",
      value: "unavailable",
      detail: err && err.message ? err.message : "CI 狀態讀取失敗",
      color: "#ffb74d",
    }]);
  }
}

async function loadServerHealth() {
  if (!currentUser || currentRole !== "super_admin" || !isSystemHealthActive() || document.hidden) return;
  const firstSummaryStarted = rootAdminTimingStart("first-summary");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/health", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  const summary = $("server-health-summary");
  const details = $("server-health-details");
  const workqueue = $("server-health-workqueue");
  const countsBox = $("server-health-counts");
  const storageBox = $("server-health-storage");
  const pointsFinalityBox = $("server-health-points-finality");
  const dbMaintenanceBox = $("server-health-db-maintenance");
  const auditBox = $("server-health-audit");
  if (!summary || !details || !workqueue || !countsBox || !storageBox || !pointsFinalityBox || !dbMaintenanceBox || !auditBox) return;
  if (!json.ok) {
    summary.innerHTML = `<div style="color:#ff4f6d;">${sanitize(json.msg || "健康度讀取失敗")}</div>`;
    rootAdminTimingFinish("first-summary", firstSummaryStarted, "GET /api/admin/health failed before summary payload");
    renderRootFrontendTimingObservability();
    details.textContent = "";
    workqueue.innerHTML = "";
    countsBox.innerHTML = "";
    storageBox.innerHTML = "";
    pointsFinalityBox.innerHTML = "";
    dbMaintenanceBox.innerHTML = "";
    auditBox.innerHTML = "";
    return;
  }
  const c = json.counts || {};
  const s = json.storage || {};
  const capacity = s.capacity_audit || {};
  const auditOk = json.audit_integrity && json.audit_integrity.ok;
  const auditEnabled = !(json.audit_integrity && json.audit_integrity.enabled === false);
  const readiness = json.readiness || {};
  const anomaly = json.anomaly || {};
  const pointsFinality = json.points_finality || {};
  const pendingQueue = pointsFinality.pending_queue || {};
  const compactSweep = pointsFinality.compact_sweep || {};
  const lastSweep = compactSweep.last_process_local_sweep || {};
  const latestSweepSnapshot = pointsFinality.latest_sweep_snapshot || {};
  const latestSweepSummary = latestSweepSnapshot.summary || {};
  const databaseUsage = json.database_usage || {};
  const readinessChecks = Array.isArray(readiness.checks) ? readiness.checks : [];
  const failedChecks = readinessChecks.filter((item) => !item.ok);
  const anomalySignals = Array.isArray(anomaly.signals) ? anomaly.signals : [];
  const finalitySignals = Array.isArray(pointsFinality.signals) ? pointsFinality.signals : [];
  const statusLabel = json.status === "critical" ? "Critical" : json.status === "degraded" ? "Degraded" : "OK";
  const cards = [
    ["整體狀態", statusLabel, healthStatusColor(json.status)],
    ["維護模式", json.maintenance_mode ? "啟用" : "關閉", json.maintenance_mode ? "#ff4f6d" : "#4caf50"],
    ["審計鏈", auditEnabled ? (auditOk ? "完整" : "異常") : "停用", auditEnabled ? (auditOk ? "#4caf50" : "#ff4f6d") : "#9e9e9e"],
    ["Readiness", readiness.status || "unknown", healthStatusColor(readiness.status)],
    ["Anomaly", anomaly.status || "ok", healthStatusColor(anomaly.status)],
    ["活躍 Session", String(c.active_sessions || 0), "#82b1ff"],
  ];
  summary.innerHTML = cards.map(([label, value, color]) => renderHealthMetric(label, value, color)).join("");
  rootAdminTimingFinish("first-summary", firstSummaryStarted, "GET /api/admin/health → server-health-summary render");
  details.textContent = `最後讀取：${new Date().toLocaleString()} · DB schema ${readiness.database?.schema_version ?? "-"} / ${readiness.database?.expected_schema_version ?? "-"}`;
  const queueRows = [
    ["待審檢舉", c.pending_reports ?? c.pending_chat_reports ?? 0],
    ["待審申覆", c.pending_appeals || 0],
    ["治理提案", c.pending_moderation_proposals || 0],
    ["看板審核", c.pending_board_reviews || 0],
    ["主題審核", c.pending_thread_reviews || 0],
    ["隔離檔案", c.quarantined_files || 0],
    ["未知加密檔", c.unknown_encrypted_files || 0],
  ].map(([label, value]) => ({
    label,
    value: String(value),
    color: Number(value) > 0 ? "#ffb74d" : "#4caf50",
  }));
  workqueue.innerHTML = renderHealthRows(queueRows);
  countsBox.innerHTML = renderHealthRows([
    { label: "使用者", value: `${c.active_users || 0}/${c.users_total || 0}`, detail: "active / total", color: "#82b1ff" },
    { label: "聊天訊息", value: String(c.chat_messages || 0), color: "#82b1ff" },
    { label: "上傳檔案", value: String(c.uploaded_files || 0), color: "#82b1ff" },
    { label: "違規紀錄", value: String(c.violations_total || 0), color: "#82b1ff" },
    { label: "審計紀錄", value: String(c.audit_entries || 0), color: "#82b1ff" },
  ]);
  storageBox.innerHTML = renderHealthRows([
    { label: "SQLite DB", value: formatBytes(s.database_bytes), color: "#82b1ff" },
    { label: "聊天檔案", value: `${s.chat_files || 0} / ${formatBytes(s.chat_bytes)}`, detail: s.chat_dir || "runtime/chats/", color: "#82b1ff" },
    { label: "Server logs", value: `${s.log_files || 0} / ${formatBytes(s.log_bytes)}`, color: "#82b1ff" },
    { label: "Anchor files", value: `${s.anchor_files || 0} / ${formatBytes(s.anchor_bytes)}`, color: "#82b1ff" },
    { label: "雲端硬碟實體根目錄", value: `${s.storage_files || 0} / ${formatBytes(s.storage_bytes)}`, detail: s.storage_dir || capacity.disk?.path || "", color: "#82b1ff" },
    {
      label: "會員雲端容量審計",
      value: capacity.status === "critical" ? "超額" : capacity.status === "warning" ? "接近上限" : "正常",
      detail: `會員總配額 ${formatBytes(capacity.committed_total_bytes)} / 全用戶上限 ${formatBytes(capacity.available_cloud_capacity_bytes ?? capacity.disk?.safe_free_bytes)}，剩餘承諾 ${formatBytes(capacity.committed_remaining_bytes)} / 實際安全剩餘 ${formatBytes(capacity.disk?.safe_free_bytes)}`,
      color: capacity.status === "critical" ? "#ff4f6d" : capacity.status === "warning" ? "#ffb74d" : "#4caf50",
    },
    {
      label: "Host 實際可用",
      value: formatBytes(capacity.disk?.free_bytes),
      detail: `storage root: ${capacity.disk?.path || "-"}`,
      color: "#82b1ff",
    },
  ]);
  pointsFinalityBox.innerHTML = renderHealthRows([
    {
      label: "Pending transfers",
      value: String(pendingQueue.pending_count || 0),
      detail: `oldest ${formatDurationSeconds(pendingQueue.oldest_age_seconds || 0)} · ${Number(pendingQueue.amount_points || 0).toLocaleString()} 點`,
      color: Number(pendingQueue.pending_count || 0) > 1000 ? "#ffb74d" : "#4caf50",
    },
    {
      label: "Compact finality sweep",
      value: lastSweep.finished_at ? `${lastSweep.finalized_count || 0} finalized` : "no local sweep",
      detail: lastSweep.finished_at
        ? `${lastSweep.source || "-"} · checked ${lastSweep.checked_count || 0} · limit ${compactSweep.root_transaction_list_sweep_limit || "-"} · ${lastSweep.finished_at}`
        : `root list sweep limit ${compactSweep.root_transaction_list_sweep_limit || "-"}`,
      color: lastSweep.finalization_paused ? "#ffb74d" : "#82b1ff",
    },
    {
      label: "Unsealed ledger sample",
      value: String(pointsFinality.private_chain?.unsealed_recent_sample_count || 0),
      detail: `bounded latest ${pointsFinality.private_chain?.unsealed_recent_sample_limit || pointsFinality.recent_limit || "-"} · block ${pointsFinality.private_chain?.latest_block?.block_number ?? "-"}`,
      color: pointsFinality.private_chain?.unsealed_sample_limit_reached ? "#ffb74d" : "#4caf50",
    },
    {
      label: "Latest sweep snapshot",
      value: latestSweepSnapshot.ok ? `${latestSweepSummary.finalized_count || 0} finalized` : "missing",
      detail: latestSweepSnapshot.generated_at
        ? `${latestSweepSnapshot.generated_at} · job ${(latestSweepSnapshot.source_job_uuid || "").slice(0, 8) || "-"}`
        : (latestSweepSnapshot.error || "尚未建立 latest snapshot"),
      color: latestSweepSnapshot.ok ? "#4caf50" : "#ffb74d",
    },
    {
      label: "Finality snapshot",
      value: pointsFinality.status || "unknown",
      detail: `bounded=${pointsFinality.bounded ? "true" : "false"} · ${pointsFinality.management_timing?.total_ms ?? 0} ms`,
      color: healthStatusColor(pointsFinality.status),
    },
  ]);
  dbMaintenanceBox.innerHTML = renderHealthRows([
    {
      label: "Split DB total",
      value: formatBytes(databaseUsage.total_bytes || s.database_bytes || 0),
      detail: `${Number(databaseUsage.file_count || 0)} DB files · sidecar ${formatBytes(databaseUsage.sidecar_bytes || 0)}`,
      color: "#82b1ff",
    },
    {
      label: "Main DB",
      value: formatBytes(databaseUsage.main_database_total_bytes || s.database_bytes || 0),
      detail: databaseUsage.db_dir || "runtime/database",
      color: "#82b1ff",
    },
    {
      label: "Largest DB",
      value: formatBytes((Array.isArray(databaseUsage.files) ? databaseUsage.files : []).reduce((max, item) => Math.max(max, Number(item.total_bytes || 0)), 0)),
      detail: ((Array.isArray(databaseUsage.files) ? databaseUsage.files : []).slice().sort((a, b) => Number(b.total_bytes || 0) - Number(a.total_bytes || 0))[0] || {}).label || "-",
      color: "#82b1ff",
    },
  ]);
  const auditRows = [
    {
      label: "Audit chain",
      value: auditEnabled ? (auditOk ? "完整" : "異常") : "停用",
      detail: json.audit_integrity?.details || "",
      color: auditEnabled ? (auditOk ? "#4caf50" : "#ff4f6d") : "#9e9e9e",
    },
    ...failedChecks.map((item) => ({
      label: `Readiness: ${item.name || "-"}`,
      value: item.severity || "failed",
      detail: item.detail || "",
      color: item.severity === "critical" ? "#ff4f6d" : "#ffb74d",
    })),
    ...(capacity.status && capacity.status !== "ok" ? [{
      label: "Storage capacity audit",
      value: capacity.status,
      detail: (capacity.reasons || []).join(", ") || "會員容量承諾已超過 Host 安全容量",
      color: capacity.status === "critical" ? "#ff4f6d" : "#ffb74d",
    }] : []),
    ...anomalySignals.map((item) => ({
      label: `Anomaly: ${item.name || "-"}`,
      value: item.level || "-",
      detail: item.detail || `value=${item.value}, threshold=${item.threshold}`,
      color: item.level === "critical" ? "#ff4f6d" : item.level === "warning" ? "#ffb74d" : "#82b1ff",
    })),
    ...finalitySignals.map((item) => ({
      label: `Points finality: ${item.code || "-"}`,
      value: item.severity || "-",
      detail: item.detail || "",
      color: item.severity === "critical" ? "#ff4f6d" : "#ffb74d",
    })),
  ];
  auditBox.innerHTML = renderHealthRows(auditRows);
  renderRootFrontendTimingObservability();
  const repairBtn = $("integrity-repair-btn");
  if (repairBtn) {
    repairBtn.disabled = currentUser !== "root" || !auditEnabled || auditOk !== false;
  }
  const sweepBtn = $("points-finality-sweep-btn");
  if (sweepBtn) {
    sweepBtn.disabled = currentUser !== "root";
    sweepBtn.title = currentUser === "root" ? "排入 bounded finality sweep job" : "只有 root 可執行";
  }
  loadPlaywrightCiHealth().catch((err) => {
    const host = $("server-health-playwright-ci");
    if (host) {
      host.innerHTML = renderHealthRows([{
        label: "Playwright CI",
        value: "unavailable",
        detail: err?.message || "CI 狀態讀取失敗",
        color: "#ffb74d",
      }]);
    }
  });
}

async function startPointsFinalitySweep() {
  if (currentUser !== "root") {
    alert("只有 root 可執行 finality sweep");
    return;
  }
  const btn = $("points-finality-sweep-btn");
  const originalText = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "排入中...";
  }
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/root/points/finality-sweep", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({ limit: 50 }),
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      alert(json.msg || "Finality sweep 排入失敗");
      return;
    }
    if (typeof startJobCenterPolling === "function") startJobCenterPolling({ immediate: true, force: true });
    setTimeout(() => loadServerHealth(), 1200);
  } catch (err) {
    alert(err && err.message ? err.message : "Finality sweep 排入失敗");
  } finally {
    if (btn) {
      btn.disabled = currentUser !== "root";
      btn.textContent = originalText || "Finality sweep";
    }
  }
}

async function repairIntegrityChains() {
  if (currentUser !== "root") {
    alert("只有 root 可處理鏈異常");
    return;
  }
  if (!confirm("確定要重新封鏈並解除維護模式？")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/integrity/repair", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  alert(json.msg || (json.ok ? "鏈異常已處理" : "處理失敗"));
  await loadServerHealth();
  if (currentServerTab === "audit") await loadAudit(auditPage);
  if (currentAdminTab === "violations") await loadViolations(violationsPage, violationTargetUser);
}

async function loadIntegrityGuard() {
  if (!currentUser || currentUser !== "root") return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const [statusRes, findingsRes] = await Promise.all([
    apiFetch(API + "/root/integrity/status", { credentials: "same-origin", headers: { "X-CSRF-Token": csrf || "" } }),
    apiFetch(API + "/root/integrity/findings?status=pending", { credentials: "same-origin", headers: { "X-CSRF-Token": csrf || "" } })
  ]);
  const statusJson = await statusRes.json().catch(() => ({}));
  const findingsJson = await findingsRes.json().catch(() => ({}));
  const summary = $("integrity-summary");
  const warning = $("integrity-warning");
  const list = $("integrity-findings");
  if (!summary || !warning || !list) return;
  if (!statusJson.ok) {
    summary.innerHTML = `<div style="color:#ff4f6d;">${sanitize(statusJson.msg || "Integrity Guard 狀態讀取失敗")}</div>`;
    warning.textContent = "";
    list.innerHTML = "";
    return;
  }
  const ig = statusJson.integrity || {};
  const s = ig.summary || {};
  const last = ig.last_scan || {};
  const cards = [
    ["受保護檔案", String(ig.protected_files || 0), "#82b1ff"],
    ["Pending", String(s.pending || 0), (s.pending || 0) ? "#ffb74d" : "#4caf50"],
    ["High Risk", String(s.high_risk_pending || 0), (s.high_risk_pending || 0) ? "#ff4f6d" : "#4caf50"],
    ["Modified", String(s.modified || 0), (s.modified || 0) ? "#ffb74d" : "#82b1ff"],
    ["Added", String(s.added || 0), (s.added || 0) ? "#ffb74d" : "#82b1ff"],
    ["Deleted", String(s.deleted || 0), (s.deleted || 0) ? "#ff4f6d" : "#82b1ff"],
    ["上次掃描", sanitize(last.finished_at || last.started_at || "-"), "#82b1ff"],
    ["Manifest 簽章", last.manifest_signature_valid === 1 ? "有效" : "未驗證/異常", last.manifest_signature_valid === 1 ? "#4caf50" : "#ff4f6d"],
  ];
  summary.innerHTML = cards.map(([label, value, color]) => `
    <div style="border:1px solid rgba(255,255,255,.08);background:rgba(0,0,0,.22);border-radius:8px;padding:.6rem;">
      <div style="font-size:.68rem;color:var(--muted);">${label}</div>
      <div style="font-size:1rem;color:${color};font-weight:700;margin-top:.2rem;word-break:break-all;">${value}</div>
    </div>
  `).join("");
  if ((s.high_risk_pending || 0) > 0) {
    warning.innerHTML = `<span style="color:#ff4f6d;font-weight:700;">高風險警告：</span>此變更涉及安全核心、root、admin、auth、snapshot、storage 或 Integrity Guard 本身。pending finding 會顯示 24 小時，逾期自動 approve；rejected high risk finding 仍會阻止進入準上線模式。`;
  } else if ((s.pending || 0) > 0) {
    warning.innerHTML = `<span style="color:#ffb74d;font-weight:700;">待處理：</span>存在尚未審核的檔案完整性變更，請確認是否為合法部署；pending 超過 24 小時會自動 approve。`;
  } else {
    warning.innerHTML = `<span style="color:#4caf50;font-weight:700;">正常：</span>目前沒有 pending finding。`;
  }
  const findings = Array.isArray(findingsJson.findings) ? findingsJson.findings : [];
  if (!findings.length) {
    list.innerHTML = "<p style='color:var(--muted);text-align:center;padding:1rem;'>目前沒有 pending integrity finding</p>";
    return;
  }
  list.innerHTML = findings.map((f) => `
    <div style="border:1px solid ${f.risk_level === "high" ? "rgba(255,79,109,.45)" : "rgba(255,255,255,.1)"};border-radius:9px;padding:.65rem;margin-bottom:.55rem;background:rgba(0,0,0,.22);">
      <div style="display:flex;gap:.45rem;align-items:center;flex-wrap:wrap;">
        <label style="display:inline-flex;align-items:center;gap:.25rem;color:var(--muted);"><input type="checkbox" class="integrity-finding-check" value="${f.id}" /> 選取</label>
        <strong>#${f.id}</strong>
        <span style="color:${f.risk_level === "high" ? "#ff4f6d" : f.risk_level === "medium" ? "#ffb74d" : "#82b1ff"};">${sanitize(f.risk_level || "")}</span>
        <span style="color:#82b1ff;">${sanitize(f.change_type || "")}</span>
        <span style="word-break:break-all;">${sanitize(f.file_path || "")}</span>
        <span style="margin-left:auto;color:var(--muted);">${sanitize(f.detected_at || "")}</span>
      </div>
      <div style="color:var(--muted);margin-top:.35rem;word-break:break-all;">old=${sanitize(f.old_hash || "-")} · new=${sanitize(f.new_hash || "-")}</div>
      <div style="color:var(--muted);margin-top:.2rem;">size ${f.old_size ?? "-"} -> ${f.new_size ?? "-"} · category=${sanitize(f.category || "")}</div>
      <div class="admin-toolbar" style="display:flex;gap:.45rem;margin-top:.5rem;">
        <button class="btn btn-primary" data-integrity-action="approve" data-finding-id="${f.id}">approve</button>
        <button class="btn" data-integrity-action="reject" data-finding-id="${f.id}">reject</button>
        <button class="btn" data-integrity-action="ignore" data-finding-id="${f.id}">ignore</button>
      </div>
    </div>
  `).join("");
  list.querySelectorAll("[data-integrity-action]").forEach((btn) => {
    btn.addEventListener("click", () => reviewIntegrityFinding(btn.getAttribute("data-finding-id"), btn.getAttribute("data-integrity-action")));
  });
  updateIntegritySelectedCount();
}

function selectedIntegrityFindingIds() {
  return Array.from(document.querySelectorAll(".integrity-finding-check:checked"))
    .map((item) => Number(item.value))
    .filter((value) => Number.isInteger(value) && value > 0);
}

function updateIntegritySelectedCount() {
  const count = selectedIntegrityFindingIds().length;
  const total = document.querySelectorAll(".integrity-finding-check").length;
  const el = $("integrity-selected-count");
  if (el) el.textContent = total > 0 ? `已選取 ${count}/${total} 筆` : "";
  const selectAll = $("integrity-select-all");
  if (selectAll) selectAll.checked = count > 0 && count === total;
  if (selectAll) selectAll.indeterminate = count > 0 && count < total;
}

function setupIntegritySelectAll() {
  const selectAll = $("integrity-select-all");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      const checked = selectAll.checked;
      document.querySelectorAll(".integrity-finding-check").forEach((cb) => { cb.checked = checked; });
      updateIntegritySelectedCount();
    });
  }
  // Delegate for dynamically rendered findings
  document.addEventListener("change", (e) => {
    if (e.target && e.target.classList.contains("integrity-finding-check")) {
      updateIntegritySelectedCount();
    }
  });
}

async function rescanIntegrityGuard() {
  if (!confirm("重新掃描會比對目前檔案與已核准 manifest，異常不會自動核准。")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/integrity/rescan", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  alert(json.ok ? "Integrity Guard 掃描完成" : (json.msg || "掃描失敗"));
  await loadIntegrityGuard();
}

async function reviewIntegrityFinding(id, action) {
  if (!id || !action) return;
  let confirmText = "";
  let note = "";
  if (action === "approve") {
    alert("approve 代表你確認這些檔案變更是合法部署或可信修改，系統將更新 hash manifest。");
    confirmText = prompt("請輸入 APPROVE INTEGRITY UPDATE 以確認：") || "";
    if (confirmText !== "APPROVE INTEGRITY UPDATE") {
      alert("確認字串不正確，已取消 approve。");
      return;
    }
  } else {
    if (!confirm(`確定要 ${action} 這筆 integrity finding？`)) return;
  }
  note = prompt("審核備註（可留空）：") || "";
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/root/integrity/findings/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ confirm: confirmText, note })
  });
  const json = await res.json().catch(() => ({}));
  const reason = json.reason || json.error_message || "";
  const message = json.ok
    ? (json.msg || "操作完成")
    : (reason ? `${json.msg || "操作失敗"}\n原因：${reason}` : (json.msg || "操作失敗"));
  alert(message);
  await loadIntegrityGuard();
}

async function reviewSelectedIntegrityFindings(action) {
  const ids = selectedIntegrityFindingIds();
  if (!ids.length) {
    alert("請先勾選要處理的 integrity finding。");
    return;
  }
  let confirmText = "";
  if (action === "approve") {
    alert("approve 代表你確認這些檔案變更是合法部署或可信修改，系統將更新 hash manifest。");
    confirmText = prompt(`將批次 approve ${ids.length} 筆 finding，請輸入 APPROVE INTEGRITY UPDATE 以確認：`) || "";
    if (confirmText !== "APPROVE INTEGRITY UPDATE") {
      alert("確認字串不正確，已取消批次 approve。");
      return;
    }
  } else if (!confirm(`確定要 ${action} 選取的 ${ids.length} 筆 integrity finding？`)) {
    return;
  }
  const note = prompt("批次審核備註（可留空）：") || "";
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/integrity/findings/bulk-review", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ action, finding_ids: ids, confirm: confirmText, note })
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    alert(`批次操作完成：${json.reviewed}/${json.total}`);
  } else {
    const failed = Array.isArray(json.results)
      ? json.results.filter((item) => !item.ok).map((item) => `#${item.finding_id}: ${item.reason || item.msg || item.error || "unknown"}`)
      : [];
    const summary = json.msg || `批次操作失敗：${json.reviewed || 0}/${json.total || ids.length}`;
    alert(failed.length ? `${summary}\n${failed.join("\n")}` : summary);
  }
  await loadIntegrityGuard();
}

async function exportIntegrityReport() {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/integrity/report", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    alert(json.msg || "匯出失敗");
    return;
  }
  const blob = new Blob([JSON.stringify(json.report || {}, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "integrity_report.json";
  a.click();
  URL.revokeObjectURL(url);
}

async function saveSettings() {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const captchaMode = $("s-captcha-mode")?.value || "none";
  const rawComfyuiMode = $("s-comfyui-comfyui-backend-mode")?.value || $("s-comfyui-connection-mode")?.value || "remote";
  const comfyuiMode = rawComfyuiMode === "local" ? "local" : "remote";
  const payload = {
    maintenance_mode: !!$("s-maintenance-mode")?.checked,
    audit_chain_enabled: !!$("s-audit-chain-enabled")?.checked,
    ip_blocking_enabled: !!$("s-ip-blocking-enabled")?.checked,
    login_violation_enabled: !!$("s-login-violation-enabled")?.checked,
    rate_limit_violation_enabled: !!$("s-rate-limit-violation-enabled")?.checked,
    root_ip_whitelist_enabled: !!$("s-root-ip-whitelist-enabled")?.checked,
    password_strength_policy_enabled: $("s-password-strength-policy-enabled") ? !!$("s-password-strength-policy-enabled").checked : true,
    root_ip_whitelist: $("s-root-ip-whitelist")?.value || "",
    browser_only_mode_enabled: !!$("s-browser-only-mode-enabled")?.checked,
    integrity_guard_enabled: !!$("s-integrity-guard-enabled")?.checked,
    integrity_guard_strict_mode: !!$("s-integrity-guard-strict-mode")?.checked,
    allow_register: !!$("s-allow-register")?.checked,
    require_email_verification: !!$("s-require-email")?.checked,
    password_reset_mode: $("s-password-reset-mode")?.value || "admin_review",
    login_autofill_block_enabled: !!$("s-login-autofill-block-enabled")?.checked,
    captcha_mode: captchaMode,
    captcha_ttl_seconds: parseInt($("s-captcha-ttl-seconds")?.value || "300"),
    captcha_turnstile_site_key: ($("s-captcha-turnstile-site-key")?.value || "").trim(),
    max_login_failures: parseInt($("s-max-fail")?.value || "5"),
    block_duration_minutes: parseInt($("s-block-dur")?.value || "30"),
    session_ttl_hours: parseInt($("s-session-ttl")?.value || "24"),
    session_idle_timeout_minutes: parseInt($("s-session-idle-timeout")?.value || "0", 10) || 0,
    notification_muted_types: ($("s-notification-muted-types")?.value || "").trim(),
    server_ssl_enabled: $("s-server-ssl-enabled") ? !!$("s-server-ssl-enabled").checked : true,
    server_listen_host: ($("s-server-listen-host")?.value || "").trim(),
    server_listen_port: parseInt($("s-server-listen-port")?.value || "0"),
    server_timezone: ($("s-server-timezone")?.value || "UTC").trim() || "UTC",
    server_backpressure_enabled: $("s-server-backpressure-enabled") ? !!$("s-server-backpressure-enabled").checked : true,
    server_backpressure_mode: $("s-server-backpressure-mode")?.value || "auto",
    server_backpressure_thread_capacity: parseInt($("s-server-backpressure-thread-capacity")?.value || "0", 10) || 0,
    server_backpressure_normal_limit: parseInt($("s-server-backpressure-normal-limit")?.value || "0", 10) || 0,
    server_backpressure_heavy_limit: parseInt($("s-server-backpressure-heavy-limit")?.value || "0", 10) || 0,
    server_backpressure_root_priority_enabled: $("s-server-backpressure-root-priority-enabled") ? !!$("s-server-backpressure-root-priority-enabled").checked : true,
    server_backpressure_root_limit: parseInt($("s-server-backpressure-root-limit")?.value || "0", 10) || 0,
    server_backpressure_fast_lane_reserved: parseInt($("s-server-backpressure-fast-lane-reserved")?.value || "0", 10) || 0,
    server_backpressure_retry_after_seconds: parseInt($("s-server-backpressure-retry-after-seconds")?.value || "2", 10) || 2,
    server_backpressure_refresh_seconds: parseInt($("s-server-backpressure-refresh-seconds")?.value || "2", 10) || 2,
    server_backpressure_traffic_refresh_seconds: parseInt($("s-server-backpressure-traffic-refresh-seconds")?.value || "4", 10) || 4,
    server_output_refresh_seconds: parseInt($("s-server-output-refresh-seconds")?.value || "3", 10) || 3,
    security_test_job_poll_seconds: parseInt($("s-security-test-job-poll-seconds")?.value || "3", 10) || 3,
    system_resource_board_refresh_seconds: parseInt($("s-system-resource-board-refresh-seconds")?.value || "5", 10) || 5,
    job_center_refresh_seconds: parseInt($("s-job-center-refresh-seconds")?.value || "3", 10) || 3,
    economy_dashboard_refresh_seconds: parseInt($("s-economy-dashboard-refresh-seconds")?.value || "30", 10) || 30,
    trading_dashboard_refresh_seconds: parseInt($("s-trading-dashboard-refresh-seconds")?.value || "5", 10) || 5,
    trading_live_price_refresh_seconds: parseInt($("s-trading-live-price-refresh-seconds")?.value || "2", 10) || 2,
    trading_reference_price_refresh_seconds: parseInt($("s-trading-reference-price-refresh-seconds")?.value || "1", 10) || 1,
    trading_reference_chart_refresh_seconds: parseInt($("s-trading-reference-chart-refresh-seconds")?.value || "5", 10) || 5,
    comfyui_job_poll_seconds: parseInt($("s-comfyui-job-poll-seconds")?.value || "1", 10) || 1,
    notification_poll_seconds: parseInt($("s-notification-poll-seconds")?.value || "60", 10) || 60,
    game_invite_poll_active_seconds: parseInt($("s-game-invite-poll-active-seconds")?.value || "5", 10) || 5,
    game_invite_poll_idle_seconds: parseInt($("s-game-invite-poll-idle-seconds")?.value || "60", 10) || 60,
    game_invite_poll_hidden_seconds: parseInt($("s-game-invite-poll-hidden-seconds")?.value || "180", 10) || 180,
    server_connection_monitor_seconds: parseInt($("s-server-connection-monitor-seconds")?.value || "15", 10) || 15,
    drive_dashboard_lazy_refresh_seconds: parseInt($("s-drive-dashboard-lazy-refresh-seconds")?.value || "10", 10) || 10,
    comfyui_connection_mode: comfyuiMode,
    comfyui_remote_api_url: ($("s-comfyui-remote-api-url")?.value || DEFAULT_COMFYUI_REMOTE_API_URL).trim(),
    comfyui_base_dir: ($("s-comfyui-base-dir")?.value || "").trim(),
    comfyui_local_start_script: ($("s-comfyui-local-start-script")?.value || "").trim(),
    comfyui_api_host: ($("s-comfyui-api-host")?.value || "localhost").trim(),
    comfyui_api_port: parseInt($("s-comfyui-api-port")?.value || "8192"),
    comfyui_local_vram_mode: $("s-comfyui-local-vram-mode")?.value || "auto",
    comfyui_local_precision: $("s-comfyui-local-precision")?.value || "auto",
    comfyui_local_unet_dtype: $("s-comfyui-local-unet-dtype")?.value || "auto",
    comfyui_local_vae_dtype: $("s-comfyui-local-vae-dtype")?.value || "auto",
    comfyui_local_text_encoder_dtype: $("s-comfyui-local-text-encoder-dtype")?.value || "auto",
    comfyui_local_cpu_vae: !!$("s-comfyui-local-cpu-vae")?.checked,
    comfyui_local_attention_mode: $("s-comfyui-local-attention-mode")?.value || "auto",
    comfyui_local_upcast_attention: $("s-comfyui-local-upcast-attention")?.value || "auto",
    comfyui_local_cuda_malloc: $("s-comfyui-local-cuda-malloc")?.value || "auto",
    comfyui_local_disable_smart_memory: !!$("s-comfyui-local-disable-smart-memory")?.checked,
    comfyui_local_deterministic: !!$("s-comfyui-local-deterministic")?.checked,
    comfyui_local_async_offload: $("s-comfyui-local-async-offload")?.value || "auto",
    comfyui_local_cache_mode: $("s-comfyui-local-cache-mode")?.value || "auto",
    comfyui_local_cache_lru: parseInt($("s-comfyui-local-cache-lru")?.value || "0", 10) || 0,
    comfyui_local_reserve_vram_gb: ($("s-comfyui-local-reserve-vram-gb")?.value || "").trim(),
    comfyui_civitai_api_key: ($("s-comfyui-civitai-api-key")?.value || "").trim(),
    comfyui_diffusers_model_repo: normalizeAdminHuggingFaceRepoInput($("s-comfyui-diffusers-model-repo")?.value || ""),
    comfyui_huggingface_api_token: ($("s-comfyui-huggingface-api-token")?.value || "").trim(),
    comfyui_huggingface_api_token_clear: !!$("s-comfyui-huggingface-api-token-clear")?.checked,
    comfyui_huggingface_cache_root: ($("s-comfyui-huggingface-cache-root")?.value || "").trim(),
    comfyui_diffusers_device: $("s-comfyui-diffusers-device")?.value || "auto",
    comfyui_diffusers_dtype: $("s-comfyui-diffusers-dtype")?.value || "auto",
    comfyui_diffusers_device_map: $("s-comfyui-diffusers-device-map")?.value || "auto",
    comfyui_allow_in_process_diffusers: !!$("s-comfyui-allow-in-process-diffusers")?.checked,
    comfyui_diffusers_low_cpu_mem_usage: $("s-comfyui-diffusers-low-cpu-mem-usage") ? !!$("s-comfyui-diffusers-low-cpu-mem-usage").checked : true,
    comfyui_diffusers_cuda_fallback_to_cpu: $("s-comfyui-diffusers-cuda-fallback-to-cpu") ? !!$("s-comfyui-diffusers-cuda-fallback-to-cpu").checked : true,
    comfyui_diffusers_keep_downloaded_models: $("s-comfyui-diffusers-keep-downloaded-models") ? !!$("s-comfyui-diffusers-keep-downloaded-models").checked : true,
    comfyui_diffusers_disable_xet: $("s-comfyui-diffusers-disable-xet") ? !!$("s-comfyui-diffusers-disable-xet").checked : true,
    comfyui_paid_api_nodes_enabled: !!$("s-comfyui-paid-api-nodes-enabled")?.checked,
    comfyui_account_api_key: ($("s-comfyui-account-api-key")?.value || "").trim(),
    comfyui_account_api_key_clear: !!$("s-comfyui-account-api-key-clear")?.checked,
    ai_agent_provider: $("s-ai-agent-provider")?.value || "hermes",
    ai_agent_api_base_url: ($("s-ai-agent-api-base-url")?.value || "http://127.0.0.1:8642/v1").trim(),
    ai_agent_api_key: ($("s-ai-agent-api-key")?.value || "").trim(),
    ai_agent_api_key_clear: !!$("s-ai-agent-api-key-clear")?.checked,
    ai_agent_model: ($("s-ai-agent-model")?.value || "hermes-agent").trim(),
    ai_agent_operation_mode: $("s-ai-agent-operation-mode")?.value || "assist",
    ai_agent_allowed_models: ($("s-ai-agent-allowed-models")?.value || "").trim(),
    ai_agent_request_timeout_seconds: parseInt($("s-ai-agent-request-timeout-seconds")?.value || "120", 10) || 120,
    ai_agent_max_prompt_chars: parseInt($("s-ai-agent-max-prompt-chars")?.value || "20000", 10) || 20000,
    ai_agent_allow_image_input: $("s-ai-agent-allow-image-input") ? !!$("s-ai-agent-allow-image-input").checked : true,
    ai_agent_allow_tool_runs: !!$("s-ai-agent-allow-tool-runs")?.checked,
    ai_agent_persona: $("s-ai-agent-persona")?.value || "concise_helper",
    ai_agent_task_site_guide: !!$("s-ai-agent-task-site-guide")?.checked,
    ai_agent_task_troubleshoot: !!$("s-ai-agent-task-troubleshoot")?.checked,
    ai_agent_task_prompt: !!$("s-ai-agent-task-prompt")?.checked,
    ai_agent_audit_interval_minutes: parseInt($("s-ai-agent-audit-interval-minutes")?.value || "5", 10) || 5,
    ai_agent_audit_cpu_percent_threshold: parseInt($("s-ai-agent-audit-cpu-percent-threshold")?.value || "92", 10) || 92,
    ai_agent_audit_ram_percent_threshold: parseInt($("s-ai-agent-audit-ram-percent-threshold")?.value || "92", 10) || 92,
    ai_agent_audit_disk_percent_threshold: parseInt($("s-ai-agent-audit-disk-percent-threshold")?.value || "95", 10) || 95,
    ai_agent_audit_ip_event_rate_threshold: parseInt($("s-ai-agent-audit-ip-event-rate-threshold")?.value || "240", 10) || 240,
    ai_agent_audit_ip_event_rate_window_minutes: parseInt($("s-ai-agent-audit-ip-event-rate-window-minutes")?.value || "5", 10) || 5,
    ai_agent_audit_security_event_rate_threshold: parseInt($("s-ai-agent-audit-security-event-rate-threshold")?.value || "120", 10) || 120,
    ai_agent_audit_security_event_rate_window_minutes: parseInt($("s-ai-agent-audit-security-event-rate-window-minutes")?.value || "5", 10) || 5,
    ai_agent_audit_auto_block_suspect_ip: !!$("s-ai-agent-audit-auto-block-suspect-ip")?.checked,
    ai_agent_audit_block_minutes: parseInt($("s-ai-agent-audit-block-minutes")?.value || "30", 10) || 30,
    ai_agent_audit_notify_root: !!$("s-ai-agent-audit-notify-root")?.checked,
    comfyui_max_batch_size: parseInt($("s-comfyui-max-batch-size")?.value || "1"),
    comfyui_default_width: parseInt($("s-comfyui-default-width")?.value || "1024"),
    comfyui_default_height: parseInt($("s-comfyui-default-height")?.value || "1024"),
    server_max_content_mb: parseInt($("s-server-max-content-mb")?.value || "8192"),
    cloud_drive_storage_root: ($("s-cloud-drive-storage-root")?.value || "").trim(),
    cloud_drive_global_capacity_limit_mb: parseInt($("s-cloud-drive-global-capacity-limit-mb")?.value || "-1"),
    cloud_drive_transfer_limits_enabled: !!$("s-cloud-drive-transfer-limits-enabled")?.checked,
    cloud_drive_transfer_limits_json: JSON.stringify(collectCloudDriveTransferLimits()),
    remote_download_max_concurrent_global: parseInt($("s-remote-download-max-concurrent-global")?.value || "1", 10) || 1,
    remote_download_max_concurrent_per_user: parseInt($("s-remote-download-max-concurrent-per-user")?.value || "1", 10) || 1,
    bt_download_backend: ($("s-bt-download-backend")?.value || "auto"),
    transmission_rpc_url: ($("s-transmission-rpc-url")?.value || "").trim(),
    transmission_rpc_username: ($("s-transmission-rpc-username")?.value || "").trim(),
    storage_maintenance_auto_enabled: !!$("s-storage-maintenance-auto-enabled")?.checked,
    storage_maintenance_daily_time: $("s-storage-maintenance-daily-time")?.value || "04:00",
    storage_trash_retention_days: parseInt($("s-storage-trash-retention-days")?.value || "30"),
    snapshot_daily_auto_enabled: !!$("s-snapshot-daily-auto-enabled")?.checked,
    snapshot_daily_time: $("s-snapshot-daily-time")?.value || "03:00",
    module_chat_min_role: $("s-module-chat-min-role")?.value || "user",
    module_profile_min_role: $("s-module-profile-min-role")?.value || "user",
    module_community_min_role: $("s-module-community-min-role")?.value || "user",
    module_appeals_min_role: $("s-module-appeals-min-role")?.value || "user",
    module_accounts_min_role: $("s-module-accounts-min-role")?.value || "manager",
    module_comfyui_min_role: $("s-module-comfyui-min-role")?.value || "user",
    module_ai_agent_min_role: $("s-module-ai-agent-min-role")?.value || "user",
    module_games_min_role: $("s-module-games-min-role")?.value || "user",
    module_videos_min_role: $("s-module-videos-min-role")?.value || "user",
    video_tip_fee_percent: Number($("s-video-tip-fee-percent")?.value || 5),
    video_tip_min_points: parseInt($("s-video-tip-min-points")?.value || "1"),
    video_e2ee_derivatives_enabled: $("s-video-e2ee-derivatives-enabled") ? !!$("s-video-e2ee-derivatives-enabled").checked : true,
    video_e2ee_derivative_heights: ($("s-video-e2ee-derivative-heights")?.value || "720,480").trim(),
    video_e2ee_derivative_reject_larger_than_original: $("s-video-e2ee-derivative-reject-larger-than-original") ? !!$("s-video-e2ee-derivative-reject-larger-than-original").checked : true,
    video_e2ee_derivative_quota_exempt: $("s-video-e2ee-derivative-quota-exempt") ? !!$("s-video-e2ee-derivative-quota-exempt").checked : true,
    video_hls_cold_cleanup_enabled: $("s-video-hls-cold-cleanup-enabled") ? !!$("s-video-hls-cold-cleanup-enabled").checked : true,
    video_hls_protect_days_after_upload: parseInt($("s-video-hls-protect-days-after-upload")?.value || "7", 10) || 0,
    video_hls_warm_plays_30d_below: parseInt($("s-video-hls-warm-plays-30d-below")?.value || "20", 10) || 20,
    video_hls_cold_plays_30d_below: parseInt($("s-video-hls-cold-plays-30d-below")?.value || "5", 10) || 0,
    video_hls_cold_no_play_days: parseInt($("s-video-hls-cold-no-play-days")?.value || "14", 10) || 14,
    video_hls_very_cold_no_play_days: parseInt($("s-video-hls-very-cold-no-play-days")?.value || "90", 10) || 90,
    video_hls_rebuild_plays_24h: parseInt($("s-video-hls-rebuild-plays-24h")?.value || "3", 10) || 3,
    video_hls_rebuild_plays_7d: parseInt($("s-video-hls-rebuild-plays-7d")?.value || "5", 10) || 5,
    video_hls_warm_keep_variants: ($("s-video-hls-warm-keep-variants")?.value || "original,480p").trim(),
    video_hls_mobile_floor_keep_variants: ($("s-video-hls-mobile-floor-keep-variants")?.value || "480p").trim(),
    video_hls_cold_cleanup_max_assets_per_run: parseInt($("s-video-hls-cold-cleanup-max-assets-per-run")?.value || "25", 10) || 25,
    site_name: ($("s-site-name")?.value || "").trim() || "hackme_web",
    site_document_title: ($("s-site-document-title")?.value || "").trim() || "hackme_web — 登入系統",
    site_login_heading: ($("s-site-login-heading")?.value || "").trim() || "Help me improve, hack me please.",
    site_login_subtitle: ($("s-site-login-subtitle")?.value || "").trim() || "簡單的帳號註冊與登入系統",
    site_success_heading: ($("s-site-success-heading")?.value || "").trim() || "恭喜登入成功",
    site_success_message: ($("s-site-success-message")?.value || "").trim() || "歡迎回來！",
    site_theme_mode: $("s-site-theme-mode")?.value || "dark",
    site_bg: $("s-site-bg")?.value || "#11131d",
    site_surface: $("s-site-surface")?.value || "#1b2030",
    site_accent: $("s-site-accent")?.value || "#7a7bdc",
    site_accent2: $("s-site-accent2")?.value || "#43b6a0",
    site_text: $("s-site-text")?.value || "#eceef8",
    site_muted: $("s-site-muted")?.value || "#aeb8cc",
    site_layout_mode: $("s-site-layout-mode")?.value || "centered",
    site_density: $("s-site-density")?.value || "comfortable",
    site_radius_px: parseInt($("s-site-radius-px")?.value || "12", 10) || 12,
    site_font_scale: Number($("s-site-font-scale")?.value || 1) || 1,
    site_content_width: parseInt($("s-site-content-width")?.value || "1380", 10) || 1380,
    site_font_family: $("s-site-font-family")?.value || "system",
    site_background_style: $("s-site-background-style")?.value || "flat",
    site_panel_style: $("s-site-panel-style")?.value || "glass",
    site_sidebar_width: $("s-site-sidebar-width")?.value || "standard"
  };
  const transmissionRpcPassword = ($("s-transmission-rpc-password")?.value || "").trim();
  if (transmissionRpcPassword) payload.transmission_rpc_password = transmissionRpcPassword;
  FEATURE_SETTING_KEYS.forEach((key) => {
    const el = $(featureSettingInputId(key));
    if (el) payload[key] = !!el.checked;
  });
  const res = await apiFetch(API + "/admin/settings", {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(payload)
  });
  const json = await res.json().catch(() => ({}));
  const bind = json.server_bind || {};
  const ssl = json.server_ssl || {};
  const driveStorage = json.cloud_drive_storage || {};
  renderBackpressureStatus(json.backpressure);
  renderServerTimeStatus(json.server_time);
  const restartParts = [];
  if (bind.restart_required) restartParts.push("listen IP/port");
  if (ssl.restart_required) restartParts.push("HTTPS 開關");
  if (driveStorage.restart_required) restartParts.push("雲端硬碟儲存位置");
  const restartHint = restartParts.length ? `，${restartParts.join("、")} 需重啟服務器後生效` : "";
  if (!json.ok) {
    setSettingsStatus(json.msg || "儲存失敗", false);
  }
  if (json.ok) {
    const activeModule = currentModuleTab;
    const activeServerTab = currentServerTab;
    const activeSystemTab = currentSystemTab;
    const activeSettingsSection = currentSettingsSection;
    applySiteConfig(payload);
    if (typeof updateAuthUI === "function") {
      try {
        await updateAuthUI();
      } catch (_) {}
    }
    if (typeof stopTradingModuleTimers === "function" && typeof startTradingModuleTimers === "function") {
      stopTradingModuleTimers();
      startTradingModuleTimers();
    } else if (typeof syncTradingModuleTimerLifecycle === "function") {
      syncTradingModuleTimerLifecycle();
    }
    if (typeof stopEconomyAutoRefresh === "function" && typeof startEconomyAutoRefresh === "function") {
      stopEconomyAutoRefresh();
      startEconomyAutoRefresh();
    }
    if (typeof startJobCenterPolling === "function" && currentModuleTab === "jobs") {
      startJobCenterPolling({ immediate: false, force: true });
    }
    if (typeof restartNotificationPoll === "function" && currentUser) {
      restartNotificationPoll();
    } else if (typeof stopNotificationPoll === "function" && typeof startNotificationPoll === "function" && currentUser) {
      stopNotificationPoll();
      startNotificationPoll();
    }
    if (typeof stopGameMultiplayerInvitePolling === "function" && typeof ensureGameMultiplayerInvitePolling === "function") {
      stopGameMultiplayerInvitePolling();
      ensureGameMultiplayerInvitePolling({ kickoff: false });
    }
    if (typeof startServerConnectionMonitor === "function") {
      startServerConnectionMonitor();
    }
    const warnings = buildFeatureAdvisories().filter((item) => item.missingRequired.length);
    const warningHint = warnings.length ? `；仍有父功能未齊：${warnings.map(formatFeatureAdvisoryLine).join("、")}` : "";
    const trafficChanged = payload.server_backpressure_traffic_refresh_seconds !== backpressureTrafficRefreshSeconds;
    const outputChanged = payload.server_output_refresh_seconds !== serverOutputRefreshSeconds;
    backpressureTrafficRefreshSeconds = adminRefreshSeconds(payload.server_backpressure_traffic_refresh_seconds, 4, 1, 300);
    serverOutputRefreshSeconds = adminRefreshSeconds(payload.server_output_refresh_seconds, 3, 1, 300);
    securityTestJobPollSeconds = adminRefreshSeconds(payload.security_test_job_poll_seconds, 3, 1, 300);
    if (trafficChanged && isSystemSettingsActive()) startBackpressureTrafficPoll();
    if (outputChanged && isSystemOverviewActive()) {
      stopServerOutputPoll();
      startServerOutputPoll();
    }
    applySystemResourceRefreshSeconds(payload.system_resource_board_refresh_seconds, { restart: true });
    setSettingsStatus(
      `${warnings.length ? "設定已儲存，但功能組合仍未完整" : "✅ 設定已儲存"}${restartHint}${warningHint}`,
      warnings.length ? null : true,
      { autoClearMs: warnings.length ? 0 : 4000 }
    );
    renderFeatureAdvisories();
    const idleMinutes = Number(payload.session_idle_timeout_minutes ?? 10);
    inactivityLogoutMs = idleMinutes > 0 ? Math.max(1, idleMinutes) * 60 * 1000 : 0;
    if (inactivityLogoutMs > 0) resetInactivityTimer();
    if (typeof syncSidebarMenuVisibility === "function") syncSidebarMenuVisibility();
    if (activeModule && typeof switchModuleTab === "function") {
      suppressNextSettingsStatusClear = true;
      currentServerTab = activeServerTab;
      currentSystemTab = activeSystemTab;
      currentSettingsSection = activeSettingsSection;
      switchModuleTab(activeModule);
    }
  }
}

async function testComfyuiConnection() {
  const status = $("comfyui-test-connection-status");
  const button = $("comfyui-test-connection-btn");
  const settingsFamily = typeof getComfyuiSettingsFamily === "function" ? getComfyuiSettingsFamily() : "comfyui";
  const backendMode = $("s-comfyui-connection-mode")?.value === "local" ? "local" : "remote";
  const mode = settingsFamily === "hf" ? "diffusers" : backendMode;
  const host = ($("s-comfyui-api-host")?.value || "localhost").trim();
  const port = parseInt($("s-comfyui-api-port")?.value || "8192", 10);
  const apiUrl = ($("s-comfyui-remote-api-url")?.value || DEFAULT_COMFYUI_REMOTE_API_URL).trim();
  const baseDir = ($("s-comfyui-base-dir")?.value || "").trim();
  const startScript = ($("s-comfyui-local-start-script")?.value || "").trim();
  const diffusersRepo = ($("s-comfyui-diffusers-model-repo")?.value || "").trim();
  const targetLabel = mode === "local"
    ? `本地 http://${host || "localhost"}:${Number.isFinite(port) ? port : "-"}`
    : (mode === "diffusers" ? (diffusersRepo || "Hugging Face Diffusers") : (apiUrl || "遠端 API"));
  if (status) {
    status.dataset.userTouched = "1";
    status.textContent = `正在測試 ${targetLabel} ...`;
    status.style.color = "var(--muted)";
  }
  if (button) button.disabled = true;
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/root/comfyui/test-connection", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || ""
      },
      body: JSON.stringify({
        mode,
        api_url: apiUrl,
        host,
        port,
        base_dir: baseDir,
        local_start_script: startScript,
        diffusers_model_repo: diffusersRepo,
        comfyui_diffusers_device: $("s-comfyui-diffusers-device")?.value || "auto",
        comfyui_diffusers_dtype: $("s-comfyui-diffusers-dtype")?.value || "auto",
        comfyui_diffusers_cuda_fallback_to_cpu: $("s-comfyui-diffusers-cuda-fallback-to-cpu") ? !!$("s-comfyui-diffusers-cuda-fallback-to-cpu").checked : true
      })
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 連線測試失敗（HTTP ${res.status}）`);
    if (status) {
      const script = json.local_script || {};
      const scriptText = mode === "local" && script.configured
        ? `；啟動腳本${script.exists ? "存在" : "缺失"}${script.syntax_ok === false ? "，語法檢查失敗" : (script.syntax_ok === true ? "，語法正常" : "")}`
        : "";
      const autostart = json.autostart || {};
      const dependencyHint = mode === "diffusers" && json.dependency_install_hint ? `；${json.dependency_install_hint}` : "";
      const startupLogTail = Array.isArray((json.local_runtime || {}).startup_log_tail)
        ? json.local_runtime.startup_log_tail.filter(Boolean)
        : (Array.isArray(autostart.start?.startup_log_tail) ? autostart.start.startup_log_tail.filter(Boolean) : []);
      const startupLogText = startupLogTail.length ? `；最近輸出：${startupLogTail.join(" / ")}` : "";
      const autostartText = mode === "local" && autostart.attempted
        ? `；${autostart.available ? "已成功自動啟動 ComfyUI" : (autostart.message || "已嘗試自動啟動 ComfyUI，仍未就緒")}${startupLogText}`
        : "";
      if (json.available) {
        const backendText = mode === "diffusers" ? `Diffusers 可用：${json.endpoint?.model_repo || targetLabel}` : `連線成功：${json.comfyui_url || targetLabel}`;
        status.textContent = `${backendText}${scriptText}${autostartText}`;
        status.style.color = "#4caf50";
      } else if (json.starting) {
        status.textContent = `啟動中：${json.msg || "ComfyUI 正在初始化"}（${json.comfyui_url || targetLabel}）${scriptText}${autostartText}`;
        status.style.color = "#f5b544";
      } else {
        status.textContent = `連線失敗：${json.msg || (mode === "diffusers" ? "HF / Diffusers 環境不可用" : "ComfyUI 沒有回應")}（${json.comfyui_url || targetLabel}）${scriptText}${autostartText}${dependencyHint}`;
        status.style.color = "#ff4f6d";
      }
    }
  } catch (err) {
    if (status) {
      status.textContent = err.message || "ComfyUI 連線測試失敗";
      status.style.color = "#ff4f6d";
    }
  } finally {
    if (button) button.disabled = false;
  }
}

function setServerUpdateStatus(message, ok = true) {
  const el = $("server-update-status");
  if (!el) return;
  el.textContent = message || "";
  el.className = `msg show ${ok ? "ok" : "err"}`;
  el.style.color = ok ? "#4caf50" : "#ff4f6d";
}

function renderServerUpdateSummary(summary) {
  const box = $("server-update-summary");
  if (!box) return;
  box.textContent = summary || "尚未提供更新摘要。每次 push 前請更新 docs/UPDATE_SUMMARY.md。";
}

function renderServerUpdatePreview(preview) {
  const box = $("server-update-diff");
  if (!box) return;
  if (!preview || !preview.ok) {
    box.textContent = preview?.msg || "";
    renderServerUpdateSummary(preview?.release_summary || "");
    return;
  }
  renderServerUpdateSummary(preview.release_summary || preview.state?.release_summary || "");
  const state = preview.state || {};
  const summary = preview.summary || {};
  const files = (preview.changed_files || []).map((row) => `${row.status}\t${row.path}`).join("\n");
  box.textContent = [
    `目前分支：${state.current_branch || "-"} @ ${state.current_commit || "-"}`,
    `目標分支：${preview.remote_ref || "-"}`,
    `本地 ahead：${summary.ahead ?? "-"}，遠端 ahead：${summary.behind ?? "-"}`,
    `工作目錄：${state.dirty ? "有未提交變更（只檢查，不會線上套用）" : "乾淨"}`,
    "",
    "警告：",
    preview.warning || "此頁只提供版本檢查與 diff；正式更新請走部署流程。",
    "",
    "Diff stat：",
    preview.diff_stat || "(無差異)",
    "",
    "Changed files：",
    files || "(無檔案變更)"
  ].join("\n");
}

async function loadServerUpdateStatus(fetchRemote = false) {
  if (currentUser !== "root") return;
  const branchSelect = $("server-update-branch-select");
  const refreshBtn = $("server-update-refresh-btn");
  if (refreshBtn) refreshBtn.disabled = true;
  setServerUpdateStatus(fetchRemote ? "正在從 GitHub 讀取分支..." : "正在讀取更新狀態...");
  try {
    const csrf = await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + `/root/server-update/status${fetchRemote ? "?fetch=1" : ""}`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(json.msg || json.update?.msg || `更新狀態讀取失敗（HTTP ${res.status}）`);
    const update = json.update || {};
    const branches = update.branches || [];
    if (branchSelect) {
      const previous = branchSelect.value || update.current_branch || "";
      branchSelect.innerHTML = branches.length
        ? branches.map((branch) => `<option value="${sanitize(branch)}">${sanitize(branch)}</option>`).join("")
        : `<option value="${sanitize(update.current_branch || "main")}">${sanitize(update.current_branch || "main")}</option>`;
      branchSelect.value = branches.includes(previous) ? previous : (branches.includes(update.current_branch) ? update.current_branch : (branches[0] || update.current_branch || "main"));
    }
    setServerUpdateStatus(`目前 ${update.current_branch || "-"} @ ${update.current_commit || "-"}；${update.dirty ? "工作目錄有未提交變更" : "工作目錄乾淨"}。此頁只做檢查，不提供線上套用。`, true);
    renderServerUpdateSummary(update.release_summary || "");
    renderServerUpdatePreview({ ok: true, state: update, warning: json.warning, summary: {}, changed_files: [], diff_stat: "" });
  } catch (err) {
    setServerUpdateStatus(err.message || "更新狀態讀取失敗", false);
  } finally {
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

async function previewServerUpdate() {
  const branch = $("server-update-branch-select")?.value || "";
  const btn = $("server-update-preview-btn");
  if (!branch) {
    setServerUpdateStatus("請先選擇更新分支", false);
    return;
  }
  if (btn) btn.disabled = true;
  setServerUpdateStatus("正在 fetch GitHub 並產生 diff 預覽...");
  try {
    const csrf = await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/root/server-update/preview", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({ branch })
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(json.msg || json.preview?.msg || `diff 預覽失敗（HTTP ${res.status}）`);
    renderServerUpdatePreview(json.preview || {});
    setServerUpdateStatus("Diff 預覽完成。此頁不提供線上套用；請走部署流程更新。");
  } catch (err) {
    setServerUpdateStatus(err.message || "diff 預覽失敗", false);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function systemResourcePercent(value) {
  const percent = Number(value);
  return Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : null;
}

function systemResourceStatusClass(percent, available = true) {
  if (!available || percent === null) return "muted";
  if (percent >= 90) return "danger";
  if (percent >= 75) return "warn";
  return "ok";
}

function systemResourceGaugeMarkup(item) {
  const percent = systemResourcePercent(item.percent);
  const available = item.available !== false && percent !== null;
  const displayPercent = available ? Math.round(percent) : 0;
  const statusClass = systemResourceStatusClass(percent, item.available !== false);
  const detail = item.detail || (available ? "使用中" : "未偵測到資料");
  return `
    <div class="system-resource-gauge-card ${statusClass}" style="--resource-percent:${displayPercent};--resource-color:${sanitize(item.color || "#82b1ff")};">
      <div class="system-resource-gauge-label">${sanitize(item.label || "-")}</div>
      <div class="system-resource-arc" aria-label="${sanitize(item.label || "resource")} ${displayPercent}%">
        <div class="system-resource-value">${available ? `${displayPercent}%` : "--"}</div>
      </div>
      <div class="system-resource-detail">${sanitize(detail)}</div>
    </div>
  `;
}

function renderSystemResourceBoard(resource = {}) {
  const host = $("system-resource-gauges");
  const sampled = $("system-resource-sampled-at");
  if (!host) return;
  const cpu = resource.cpu || {};
  const ram = resource.ram || {};
  const gpu = resource.gpu || {};
  const vram = resource.vram || {};
  const gpuNames = Array.isArray(gpu.gpus) && gpu.gpus.length
    ? gpu.gpus.map((item) => item.name || `GPU ${item.index || ""}`).join(" / ")
    : "";
  const loadAvg = Array.isArray(cpu.load_avg) && cpu.load_avg.length ? ` · load ${Number(cpu.load_avg[0] || 0).toFixed(2)}` : "";
  const cards = [
    {
      label: "CPU",
      percent: cpu.percent,
      color: "#82b1ff",
      detail: `${Number(cpu.cores || 0) || "-"} cores${loadAvg}`,
    },
    {
      label: "RAM",
      percent: ram.percent,
      color: "#66e3c4",
      detail: ram.total_bytes ? `${formatBytes(ram.used_bytes || 0)} / ${formatBytes(ram.total_bytes)}` : "未偵測到記憶體資料",
    },
    {
      label: "GPU",
      percent: gpu.percent,
      color: "#ffca6b",
      available: gpu.available,
      detail: gpu.available ? (gpuNames || "GPU 使用率") : "未偵測到 NVIDIA GPU",
    },
    {
      label: "VRAM",
      percent: vram.percent,
      color: "#c792ea",
      available: vram.available,
      detail: vram.available && vram.total_bytes ? `${formatBytes(vram.used_bytes || 0)} / ${formatBytes(vram.total_bytes)}` : "未偵測到 VRAM",
    },
  ];
  host.innerHTML = cards.map(systemResourceGaugeMarkup).join("");
  if (sampled) sampled.textContent = resource.sampled_at ? `最後採樣：${formatChatTime(resource.sampled_at)}` : "等待資料";
}

function formatBytesPerSecond(bytes) {
  return `${formatBytes(bytes)}/s`;
}

function renderServerEnvSummary(env = lastServerEnvironment, transfer = lastServerTransferUsage, database = lastServerDatabaseUsage, resource = lastServerResourceUsage) {
  const summary = $("server-env-summary");
  if (!summary) return;
  const cards = [
    { label: "作業平台", value: env.platform || "-", color: "#82b1ff" },
    { label: "Python", value: env.python_version || "-", color: "#82b1ff" },
  ];
  summary.innerHTML = cards.map(({ label, value, color, detail }) => `
    <div class="server-env-stat-card">
      <div class="server-env-stat-label">${sanitize(label)}</div>
      <div class="server-env-stat-value" style="color:${color};">${sanitize(value)}</div>
      ${detail ? `<div class="server-env-stat-detail">${sanitize(detail)}</div>` : ""}
    </div>
  `).join("");
}

function renderServerEnvTransferDetails(transfer = {}) {
  const host = $("server-env-transfer-details");
  if (!host) return;
  const localText = transfer.process_local ? "目前 worker 統計" : "全域統計";
  const windowSeconds = Number(transfer.recent_window_seconds || transfer.window_seconds || 0);
  host.innerHTML = `
    <div class="server-env-panel-title">傳輸量</div>
    <div class="server-env-kv-grid">
      <div><span>上傳速度</span><strong>${sanitize(formatBytesPerSecond(transfer.upload_bytes_per_second || 0))}</strong></div>
      <div><span>下載速度</span><strong>${sanitize(formatBytesPerSecond(transfer.download_bytes_per_second || 0))}</strong></div>
      <div><span>累計上傳</span><strong>${sanitize(formatBytes(transfer.cumulative_upload_bytes || 0))}</strong></div>
      <div><span>累計下載</span><strong>${sanitize(formatBytes(transfer.cumulative_download_bytes || 0))}</strong></div>
      <div><span>累計請求</span><strong>${Number(transfer.cumulative_requests || 0).toLocaleString()}</strong></div>
      <div><span>採樣範圍</span><strong>${windowSeconds ? `${windowSeconds}s` : "-"} · PID ${sanitize(String(transfer.pid || "-"))}</strong></div>
    </div>
    <div class="server-env-panel-note">${sanitize(localText)}；串流或瀏覽器中斷時只能統計 response header 可得的大小。</div>
  `;
}

function renderServerEnvDatabaseDetails(database = {}) {
  const host = $("server-env-db-details");
  if (!host) return;
  const files = Array.isArray(database.files) ? database.files.slice() : [];
  files.sort((a, b) => Number(b.total_bytes || 0) - Number(a.total_bytes || 0));
  const integrity = database.integrity_check || {};
  const audit = database.audit_hash_check || {};
  const integrityTone = integrity.ok === true ? "通過" : (integrity.ok === false ? "異常" : "未檢查");
  const auditTone = audit.enabled === false
    ? "未啟用"
    : (audit.ok === true ? "通過" : (audit.ok === false ? "異常" : "未檢查"));
  const integrityDetail = [
    `DB integrity ${integrityTone}`,
    integrity.schema_version || integrity.expected_schema_version
      ? `schema ${integrity.schema_version ?? "-"}/${integrity.expected_schema_version ?? "-"}`
      : "",
    Array.isArray(integrity.quick_check) && integrity.quick_check.length
      ? `quick_check ${integrity.quick_check.join(", ")}`
      : "",
    Array.isArray(integrity.foreign_key_violations) && integrity.foreign_key_violations.length
      ? `FK 異常 ${integrity.foreign_key_violations.length} 筆`
      : "",
    `Audit hash ${auditTone}`,
    audit.details || "",
    audit.broken_at ? `broken_at ${audit.broken_at}` : "",
  ].filter(Boolean).join(" · ");
  const rows = files.length ? files.map((item) => `
    <tr>
      <td data-label="DB">${sanitize(item.label || "-")}</td>
      <td data-label="合計">${sanitize(formatBytes(item.total_bytes || 0))}</td>
      <td data-label="主檔">${sanitize(formatBytes(item.database_bytes || 0))}</td>
      <td data-label="WAL/SHM">${sanitize(formatBytes((item.wal_bytes || 0) + (item.shm_bytes || 0)))}</td>
    </tr>
  `).join("") : '<tr><td colspan="4">尚未偵測到 DB 檔案</td></tr>';
  host.innerHTML = `
    <div class="server-env-panel-title">資料庫大小合計</div>
    <div class="server-env-db-total">${sanitize(formatBytes(database.total_bytes || 0))}</div>
    <div class="server-env-panel-note">${sanitize(database.db_dir || ".")} · ${Number(database.file_count || 0)} 個 DB 檔案 · WAL/SHM ${sanitize(formatBytes(database.sidecar_bytes || 0))}</div>
    <div class="server-env-panel-note">${sanitize(integrityDetail || "尚未取得資料庫完整性與 audit hash 檢查結果")}</div>
    <div class="server-env-table-wrap">
      <table class="server-env-table">
        <thead><tr><th>DB</th><th>合計</th><th>主檔</th><th>WAL/SHM</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function renderServerEnvProcessList(resource = {}) {
  const host = $("server-env-process-list");
  if (!host) return;
  const rows = Array.isArray(resource.processes) ? resource.processes : [];
  const body = rows.length ? rows.map((item) => {
    const cpu = item.cpu_percent === null || item.cpu_percent === undefined
      ? "-"
      : `${Number(item.cpu_percent).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
    const ram = item.rss_bytes ? formatBytes(item.rss_bytes) : "-";
    const sockets = item.socket_count === null || item.socket_count === undefined ? "-" : Number(item.socket_count).toLocaleString();
    const command = String(item.command || item.args || item.process_name || "").trim();
    const commandSuffix = item.command_truncated ? " …" : "";
    return `
      <tr>
        <td data-label="PID">${sanitize(String(item.pid || "-"))}</td>
        <td data-label="主程式 / 詳細命令">
          <strong>${sanitize(item.name || "-")}</strong>
          <div class="server-env-process-command">${sanitize(command || item.process_name || "-")}${sanitize(commandSuffix)}</div>
          <div class="drive-card-sub">comm=${sanitize(item.process_name || "-")} · ppid=${sanitize(String(item.ppid || "-"))}</div>
        </td>
        <td data-label="CPU">${sanitize(cpu)}</td>
        <td data-label="RAM">${sanitize(ram)}</td>
        <td data-label="Socket">${sanitize(sockets)}</td>
      </tr>
    `;
  }).join("") : '<tr><td colspan="5">尚未偵測到相關背景程式</td></tr>';
  host.innerHTML = `
    <div class="server-env-panel-title">相關背景程式資源</div>
    <div class="server-env-panel-note">列出本專案、ffmpeg/HLS、aria2 下載與交易背景引擎相關程序；交易引擎若在 web worker 內執行，會顯示在同一個 worker PID。</div>
    <div class="server-env-table-wrap">
      <table class="server-env-table">
        <thead><tr><th>PID</th><th>主程式 / 詳細命令</th><th>CPU</th><th>RAM</th><th>Socket</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
  `;
}

function renderServerEnvPathDetails(env = {}) {
  const details = $("server-env-details");
  if (!details) return;
  details.textContent = "";
}

function renderServerEnvOperationalStats({ env, transfer, database, resource } = {}) {
  if (env) lastServerEnvironment = env;
  if (transfer) lastServerTransferUsage = transfer;
  if (database) lastServerDatabaseUsage = database;
  if (resource) lastServerResourceUsage = resource;
  renderServerEnvSummary(lastServerEnvironment, lastServerTransferUsage, lastServerDatabaseUsage, lastServerResourceUsage);
  renderServerEnvTransferDetails(lastServerTransferUsage);
  renderServerEnvDatabaseDetails(lastServerDatabaseUsage);
  renderServerEnvProcessList(lastServerResourceUsage);
  renderServerEnvPathDetails(lastServerEnvironment);
}

function normalizeSystemResourceRefreshSeconds(value) {
  const parsed = parseInt(value, 10);
  return Math.max(1, Math.min(300, Number.isFinite(parsed) ? parsed : 5));
}

function applySystemResourceRefreshSeconds(value, { restart = true } = {}) {
  const next = normalizeSystemResourceRefreshSeconds(value);
  const changed = next !== systemResourceRefreshSeconds;
  systemResourceRefreshSeconds = next;
  const input = $("s-system-resource-board-refresh-seconds");
  if (input && String(input.value || "") !== String(next)) input.value = String(next);
  if (changed && restart && currentUser === "root" && isSystemEnvActive()) {
    startSystemResourcePoll();
  }
  return next;
}

function stopSystemResourcePoll() {
  if (systemResourcePollTimer) {
    clearInterval(systemResourcePollTimer);
    systemResourcePollTimer = null;
  }
}

function startSystemResourcePoll() {
  stopSystemResourcePoll();
  if (!canRunRootManagementPoll(isSystemEnvActive)) return;
  systemResourcePollTimer = setInterval(refreshSystemResourceBoard, systemResourceRefreshSeconds * 1000);
}

async function refreshSystemResourceBoard() {
  if (systemResourceRefreshInFlight || !canRunRootManagementPoll(isSystemEnvActive)) return;
  systemResourceRefreshInFlight = true;
  try {
    const csrf = await fetchCsrfToken();
    const res = await apiFetch(API + "/admin/environment/resources", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || "系統資源讀取失敗");
    applySystemResourceRefreshSeconds(json.resource_refresh_seconds, { restart: true });
    renderSystemResourceBoard(json.resource_usage || {});
    renderServerEnvOperationalStats({
      env: json.environment || lastServerEnvironment || {},
      resource: json.resource_usage || {},
      transfer: json.transfer_usage || {},
      database: json.database_usage || {},
    });
  } catch (err) {
    const sampled = $("system-resource-sampled-at");
    if (sampled) sampled.textContent = `資源看板更新失敗：${err.message || "請求失敗"}`;
  } finally {
    systemResourceRefreshInFlight = false;
  }
}

async function loadServerEnv() {
  if (!currentUser || currentRole !== "super_admin" || !isSystemEnvActive() || document.hidden) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/environment", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  const summary = $("server-env-summary");
  const details = $("server-env-details");
  if (!summary) return;
  if (!json.ok) {
    summary.innerHTML = `<div style="color:#ff4f6d;">${sanitize(json.msg || "系統環境讀取失敗")}</div>`;
    if (details) details.textContent = "";
    renderSystemResourceBoard({});
    return;
  }
  const env = json.environment || {};
  applySystemResourceRefreshSeconds(json.resource_refresh_seconds, { restart: true });
  renderSystemResourceBoard(json.resource_usage || {});
  renderServerEnvOperationalStats({
    env,
    resource: json.resource_usage || {},
    transfer: json.transfer_usage || {},
    database: json.database_usage || {},
  });
}

async function restartServer(event) {
  if (event && typeof event.preventDefault === "function") event.preventDefault();
  if (!confirm("⚠️ 確定要重啟伺服器？所有連線將中斷。")) return;
  const status = $("restart-server-status");
  const button = $("restart-server-btn");
  const previousStartedAt = serverMeta?.started_at || "";
  if (button) button.disabled = true;
  if (status) {
    status.textContent = "已送出重啟指令，等待伺服器離線...";
    status.className = "msg show";
  }
  try {
    if (status) status.textContent = "正在驗證操作權限...";
    const csrf = await fetchCsrfToken({ force: true });
    if (!csrf) throw new Error("安全驗證狀態失效，請重新整理頁面後再試。");
    if (status) status.textContent = "已送出重啟指令，等待伺服器離線...";
    const res = await apiFetch(API + "/admin/restart", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(json.msg || "重啟失敗");
    const wentOffline = await waitForRestartOffline(25000);
    if (!wentOffline) throw new Error("25 秒內沒有偵測到伺服器離線，重啟流程可能沒有真正執行。");
    if (status) status.textContent = "已偵測到離線，等待伺服器恢復...";
    const onlineMeta = await waitForRestartOnline(previousStartedAt, 180000);
    if (!onlineMeta) throw new Error("3 分鐘內未重新連線，請檢查 server log。");
    renderServerVersion(onlineMeta);
    if (status) {
      status.textContent = "伺服器已重啟完成，正在重新載入頁面...";
      status.className = "msg show ok";
    }
    setTimeout(() => location.reload(), 900);
  } catch (err) {
    if (status) {
      status.textContent = err.message || "重啟失敗";
      status.className = "msg show err";
    } else {
      alert(err.message || "重啟失敗");
    }
    if (button) button.disabled = false;
  }
}

async function probeRestartVersion(timeoutMs = 1500) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await apiFetch(API + "/version?restart_probe=" + Date.now(), {
      credentials: "same-origin",
      cache: "no-store",
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    if (!res.ok) return null;
    const json = await res.json().catch(() => ({}));
    return json && json.ok ? json : null;
  } catch (_) {
    clearTimeout(timer);
    return null;
  }
}

async function waitForRestartOffline(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const meta = await probeRestartVersion(1200);
    if (!meta) return true;
    await new Promise((resolve) => setTimeout(resolve, 650));
  }
  return false;
}

async function waitForRestartOnline(previousStartedAt, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const meta = await probeRestartVersion(1800);
    if (meta && (!previousStartedAt || meta.started_at !== previousStartedAt)) return meta;
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
  return null;
}

// ── Snapshot / Reset Server ───────────────────────────────────

async function loadSnapshots() {
  const list = $("snapshot-list");
  const actions = $("snapshot-actions");
  if (!list || !actions) return;
  list.innerHTML = "<em>載入中…</em>";
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/snapshots", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    list.innerHTML = `<span style="color:#ff4f6d;">${sanitize(json.msg || "載入失敗")}</span>`;
    actions.innerHTML = "";
    return;
  }
  const snapshots = json.snapshots || [];
  if (!snapshots.length) {
    list.innerHTML = "<em>目前沒有 snapshot</em>";
    actions.innerHTML = "";
    return;
  }
  list.innerHTML = snapshots.map((s) => `
    <div style="border:1px solid rgba(255,255,255,.1);border-radius:7px;padding:.55rem;margin-bottom:.5rem;background:rgba(0,0,0,.2);">
      <div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;">
        <strong style="color:#82b1ff;">${sanitize(s.snapshot_id || s.id || "")}</strong>
        <span style="color:${s.snapshot_type === "manual" ? "#4caf50" : s.snapshot_type === "daily" ? "#ffb74d" : "#9e9e9e"};">${sanitize(s.snapshot_type || "")}</span>
        <span style="color:var(--muted);font-size:.72rem;">${sanitize(s.created_at || s.ts || "")}</span>
        <span style="color:var(--muted);font-size:.72rem;">${sanitize(s.actor || "")}</span>
      </div>
      <div style="color:var(--muted);font-size:.7rem;margin-top:.2rem;">${sanitize(s.notes || "")}</div>
      <div style="display:flex;gap:.4rem;margin-top:.45rem;">
        <button class="btn btn-primary" type="button" data-snapshot-restore="${sanitize(s.snapshot_id || s.id || "")}" style="padding:.2rem .6rem;font-size:.72rem;">Restore</button>
        <button class="btn" type="button" data-snapshot-download="${sanitize(s.snapshot_id || s.id || "")}" style="padding:.2rem .6rem;font-size:.72rem;">下載</button>
        <button class="btn" type="button" data-snapshot-delete="${sanitize(s.snapshot_id || s.id || "")}" style="padding:.2rem .6rem;font-size:.72rem;">刪除</button>
      </div>
    </div>
  `).join("");
  actions.innerHTML = `
    <button class="btn btn-primary" type="button" id="btn-confirm-restore" disabled style="padding:.3rem .75rem;font-size:.78rem;">Restore 選取的 Snapshot</button>
    <span id="restore-hint" style="font-size:.72rem;color:var(--muted);margin-left:.4rem;">請先點選要 restore 的 snapshot</span>
  `;
  list.querySelectorAll("[data-snapshot-restore]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      document.querySelectorAll("[data-snapshot-restore]").forEach((b) => { b.classList.remove("btn-primary"); b.classList.add("btn"); });
      btn.classList.remove("btn"); btn.classList.add("btn-primary");
      window._selectedSnapshotId = btn.getAttribute("data-snapshot-restore");
      const confirmBtn = $("btn-confirm-restore");
      if (confirmBtn) { confirmBtn.disabled = false; }
      const hint = $("restore-hint");
      if (hint) hint.textContent = `已選取：${window._selectedSnapshotId}，確認後將執行 restore`;
    });
  });
  list.querySelectorAll("[data-snapshot-download]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const id = btn.getAttribute("data-snapshot-download");
      downloadSnapshot(id);
    });
  });
  list.querySelectorAll("[data-snapshot-delete]").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const id = btn.getAttribute("data-snapshot-delete");
      if (!confirm(`確定刪除 snapshot ${id}？`)) return;
      await fetchCsrfToken({ force: true });
      const csrf = getCsrfToken();
      const r = await apiFetch(API + `/admin/snapshots/${encodeURIComponent(id)}?reason=admin_delete`, {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf || "" }
      });
      const j = await r.json().catch(() => ({}));
      alert(j.msg || (j.ok ? "已刪除" : "刪除失敗"));
      if (j.ok) await loadSnapshots();
    });
  });
  const confirmRestoreBtn = $("btn-confirm-restore");
  if (confirmRestoreBtn) {
    confirmRestoreBtn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const sid = window._selectedSnapshotId;
      if (!sid) { alert("請先選取要 restore 的 snapshot"); return; }
      const reason = prompt("請輸入 restore 原因：") || "";
      const confirmText = prompt(`確定要 restore 到 snapshot ${sid}？\n此操作會重啟服務，請輸入 RESTORE 確認：`) || "";
      if (confirmText !== "RESTORE") { alert("確認字串不正確，已取消"); return; }
      performRestore(sid, reason);
    });
  }
}

async function downloadSnapshot(snapshotId) {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/admin/snapshots/${encodeURIComponent(snapshotId)}/download`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  if (!res.ok) {
    const json = await res.json().catch(() => ({}));
    alert(json.msg || "下載失敗");
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="?([^"]+)"?/i);
  link.href = url;
  link.download = match ? match[1] : `${snapshotId}.snapshot.tar.gz`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function performRestore(snapshotId, reason) {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/admin/snapshots/${encodeURIComponent(snapshotId)}/restore`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ confirm: "RESTORE", reason })
  });
  const json = await res.json().catch(() => ({}));
  alert(json.msg || (json.ok ? "Restore 請求已提交，系統將重啟" : "Restore 請求失敗"));
  if (json.ok) setTimeout(() => location.reload(), 3000);
}

async function uploadSnapshotRestore() {
  const input = $("snapshot-upload-file");
  const file = input?.files?.[0];
  if (!file) { alert("請先選擇 snapshot 封包"); return; }
  const reason = prompt("請輸入 restore 原因：") || "";
  const confirmText = prompt("確定要使用上傳的 snapshot 封包 restore？\n此操作會覆蓋目前 runtime 狀態，請輸入 RESTORE 確認：") || "";
  if (confirmText !== "RESTORE") { alert("確認字串不正確，已取消"); return; }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const form = new FormData();
  form.append("file", file);
  form.append("confirm", "RESTORE");
  form.append("reason", reason);
  const res = await apiFetch(API + "/admin/snapshots/upload-restore", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" },
    body: form
  });
  const json = await res.json().catch(() => ({}));
  alert(json.msg || (json.ok ? "Upload restore 已完成" : "Upload restore 失敗"));
  if (json.ok) setTimeout(() => location.reload(), 3000);
}

async function createSnapshot() {
  const notes = prompt("Snapshot 備註（可留空）：") || "";
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/snapshots", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ confirm: "CREATE_SNAPSHOT", notes })
  });
  const json = await res.json().catch(() => ({}));
  alert(json.msg || (json.ok ? "Snapshot 建立成功" : "建立失敗"));
  if (json.ok) await loadSnapshots();
}

async function resetServer() {
  const reason = $("s-reset-reason")?.value || "";
  const confirmText = $("s-reset-confirm")?.value || "";
  const status = $("reset-status");
  if (confirmText !== "RESET_RUNTIME_STATE") {
    if (status) { status.textContent = "確認字串錯誤，請輸入 RESET_RUNTIME_STATE"; status.style.color = "#ff4f6d"; }
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/system-reset", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ confirm: "RESET_RUNTIME_STATE", reason })
  });
  const json = await res.json().catch(() => ({}));
  if (status) {
    status.textContent = json.msg || (json.ok ? "Reset 請求已提交，系統將重啟" : "Reset 失敗");
    status.style.color = json.ok ? "#4caf50" : "#ff4f6d";
  }
  if (json.ok) setTimeout(() => location.reload(), 4500);
}

// ── Integrity Guard quick-button handlers ──────────────────────

async function refreshIntegrityGuard() {
  await loadIntegrityGuard();
}

async function exportIntegrityGuard() {
  await exportIntegrityReport();
}

// ── Platform Stats (traffic, active users, point balance) ─────

function platformStatNumber(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number : 0;
}

function platformStatPoints(value) {
  return platformStatNumber(value).toLocaleString("zh-TW");
}

function renderPlatformBarChart(title, rows, options = {}) {
  const maxValue = Math.max(1, ...rows.map((row) => Math.abs(platformStatNumber(row.value))));
  return `
    <div class="platform-stats-chart" style="border:1px solid rgba(255,255,255,.08);background:rgba(0,0,0,.22);border-radius:8px;padding:.75rem;min-width:0;">
      <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem;">
        <strong style="color:#e0e0f0;">${sanitize(title)}</strong>
        ${options.caption ? `<small style="color:var(--muted);margin-left:auto;">${sanitize(options.caption)}</small>` : ""}
      </div>
      <div style="display:grid;gap:.48rem;">
        ${rows.map((row) => {
          const value = platformStatNumber(row.value);
          const percent = Math.max(3, Math.min(100, Math.round((Math.abs(value) / maxValue) * 100)));
          const color = row.color || "#82b1ff";
          return `
            <div class="platform-chart-row" style="display:grid;grid-template-columns:minmax(5.5rem,.72fr) minmax(8rem,1.6fr) minmax(3.2rem,.35fr);gap:.55rem;align-items:center;">
              <span style="color:var(--muted);font-size:.72rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${sanitize(row.label)}</span>
              <div style="height:.72rem;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden;">
                <div style="height:100%;width:${percent}%;background:${color};border-radius:999px;"></div>
              </div>
              <strong style="color:${color};font-size:.78rem;text-align:right;white-space:nowrap;">${sanitize(row.format === "points" ? platformStatPoints(value) : String(value))}</strong>
            </div>
          `;
        }).join("")}
      </div>
    </div>
  `;
}

function renderPlatformNetChart(stats) {
  const inflow = platformStatNumber(stats.points_member_internal_inflow_month ?? stats.points_earned_month);
  const outflow = platformStatNumber(stats.points_member_internal_outflow_month ?? stats.points_spent_month);
  const net = platformStatNumber(stats.points_member_internal_net_month ?? stats.points_net_month);
  const maxValue = Math.max(1, inflow, outflow, Math.abs(net));
  const positiveWidth = net >= 0 ? Math.min(50, Math.round((net / maxValue) * 50)) : 0;
  const negativeWidth = net < 0 ? Math.min(50, Math.round((Math.abs(net) / maxValue) * 50)) : 0;
  const netColor = net >= 0 ? "#4caf50" : "#ff4f6d";
  return `
    <div class="platform-stats-chart" style="border:1px solid rgba(255,255,255,.08);background:rgba(0,0,0,.22);border-radius:8px;padding:.75rem;min-width:0;">
      <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem;">
        <strong style="color:#e0e0f0;">本月積分收支</strong>
        <small style="color:var(--muted);margin-left:auto;">pc0 member ledger</small>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0;align-items:center;margin:.9rem 0 .6rem;">
        <div style="height:1.05rem;background:rgba(255,255,255,.08);border-radius:999px 0 0 999px;display:flex;justify-content:flex-end;overflow:hidden;">
          <div style="height:100%;width:${negativeWidth}%;background:#ff4f6d;"></div>
        </div>
        <div style="height:1.05rem;background:rgba(255,255,255,.08);border-radius:0 999px 999px 0;overflow:hidden;">
          <div style="height:100%;width:${positiveWidth}%;background:#4caf50;"></div>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;gap:.75rem;font-size:.75rem;color:var(--muted);">
        <span>流出 ${platformStatPoints(outflow)}</span>
        <strong style="color:${netColor};">本月積分淨值 ${platformStatPoints(net)}</strong>
        <span>流入 ${platformStatPoints(inflow)}</span>
      </div>
      <div style="margin-top:.75rem;border-top:1px solid rgba(255,255,255,.08);padding-top:.65rem;color:#ce93d8;font-weight:700;">
        用戶站內流通餘額 ${sanitize(platformStatPoints(stats.points_user_hot_circulating ?? stats.total_points))}
      </div>
    </div>
  `;
}

function renderPlatformSupplyChart(stats) {
  const gap = platformStatNumber(stats.points_closed_loop_gap);
  const balanced = gap === 0;
  const warning = stats.points_economy_error ? ` · ${stats.points_economy_error}` : "";
  const auditWarning = stats.points_closed_loop_balanced === false && gap === 0 ? " · 對帳提示待查" : "";
  return `
    <div class="platform-stats-chart" style="border:1px solid rgba(255,255,255,.08);background:rgba(0,0,0,.22);border-radius:8px;padding:.75rem;min-width:0;">
      <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem;">
        <strong style="color:#e0e0f0;">多帳本供應對帳</strong>
        <small style="color:${balanced ? "#4caf50" : "#ff4f6d"};margin-left:auto;">${balanced ? "Settlement invariant 正常" : "需查帳"}${sanitize(auditWarning)}${sanitize(warning)}</small>
      </div>
      <div style="display:grid;gap:.45rem;font-size:.8rem;">
        <div class="platform-chart-row" style="display:grid;grid-template-columns:minmax(7rem,1fr) minmax(8rem,1fr);gap:.55rem;"><span style="color:var(--muted);">Active Supply</span><strong>${sanitize(platformStatPoints(stats.points_active_supply))}</strong></div>
        <div class="platform-chart-row" style="display:grid;grid-template-columns:minmax(7rem,1fr) minmax(8rem,1fr);gap:.55rem;"><span style="color:var(--muted);">用戶 pc0 流通</span><strong>${sanitize(platformStatPoints(stats.points_user_hot_circulating))}</strong></div>
        <div class="platform-chart-row" style="display:grid;grid-template-columns:minmax(7rem,1fr) minmax(8rem,1fr);gap:.55rem;"><span style="color:var(--muted);">官方 pc0 基金</span><strong>${sanitize(platformStatPoints(stats.points_pc0_platform_funds))}</strong></div>
        <div class="platform-chart-row" style="display:grid;grid-template-columns:minmax(7rem,1fr) minmax(8rem,1fr);gap:.55rem;"><span style="color:var(--muted);">Burn / Mint 未發放</span><strong>${sanitize(platformStatPoints(stats.points_burned_total))} / ${sanitize(platformStatPoints(stats.points_mint_remaining))}</strong></div>
        <div class="platform-chart-row" style="display:grid;grid-template-columns:minmax(7rem,1fr) minmax(8rem,1fr);gap:.55rem;"><span style="color:var(--muted);">Invariant 差額</span><strong style="color:${balanced ? "#4caf50" : "#ff4f6d"};">${sanitize(platformStatPoints(gap))}</strong></div>
      </div>
    </div>
  `;
}

async function loadPlatformStats() {
  const container = $("platform-stats");
  if (!container) return;
  if (!canRunRootManagementPoll(isSystemHealthActive)) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/platform-stats", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    container.innerHTML = `<span style="color:#ff4f6d;">${sanitize(json.msg || "讀取失敗")}</span>`;
    return;
  }
  const stats = json.stats || {};
  container.innerHTML = `
    ${renderPlatformBarChart("流量與使用者", [
      { label: "今日瀏覽量", value: stats.page_views_today, color: "#82b1ff" },
      { label: "同時在線", value: stats.active_sessions, color: "#4caf50" },
      { label: "本月新用戶", value: stats.new_users_month, color: "#ffb74d" },
      { label: "總用戶數", value: stats.total_users, color: "#82b1ff" },
    ], { caption: "人次 / 帳號" })}
    ${renderPlatformBarChart("官方基金與營運流量", [
      { label: "Treasury", value: stats.points_official_treasury, color: "#82b1ff", format: "points" },
      { label: "交易所基金", value: stats.points_exchange_fund, color: "#4caf50", format: "points" },
      { label: "Promo 基金", value: stats.points_promo_fund, color: "#ffb74d", format: "points" },
      { label: "本月營運收入", value: stats.points_fund_income_month, color: "#4caf50", format: "points" },
      { label: "本月基金支出", value: stats.points_fund_expense_month, color: "#ff4f6d", format: "points" },
      { label: "本月 Burn", value: stats.points_burned_month, color: "#ce93d8", format: "points" },
    ], { caption: "pc0 funds / events" })}
    ${renderPlatformNetChart(stats)}
    ${renderPlatformSupplyChart(stats)}
  `;
}

 // ── Bind all UI events ───────────────────────────────────────
(function setupUIBindings() {
  // Snapshot / Reset
  const loadSnapBtn = document.getElementById("btn-load-snapshots");
  if (loadSnapBtn) loadSnapBtn.addEventListener("click", loadSnapshots);
  const createSnapBtn = document.getElementById("btn-create-snapshot");
  if (createSnapBtn) createSnapBtn.addEventListener("click", createSnapshot);
  const uploadRestoreBtn = document.getElementById("btn-upload-snapshot-restore");
  if (uploadRestoreBtn) uploadRestoreBtn.addEventListener("click", uploadSnapshotRestore);
  const resetBtn = document.getElementById("btn-reset-server");
  if (resetBtn) resetBtn.addEventListener("click", resetServer);

  // Platform Stats
  const psRefreshBtn = document.getElementById("platform-stats-refresh-btn");
  if (psRefreshBtn) psRefreshBtn.addEventListener("click", loadPlatformStats);

  // Init select-all after DOM ready
  setupIntegritySelectAll();
})();
