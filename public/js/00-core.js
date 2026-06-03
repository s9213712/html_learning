'use strict';

const API = "/api";
let _csrfToken = null;
const CSRF_STORAGE_KEY = "hackme_web.csrf_token";
const CSRF_BROADCAST_CHANNEL = "hackme_web.csrf";
let csrfBroadcast = null;
let currentUser = null;
var currentUserId = null;
let currentUserAvatarFileId = "";
let currentRole = "user";
let currentRoleLabel = "user";
let currentAuthScope = "";
let currentAllowedFeatures = [];
const USER_DISPLAY_TIMEZONE_STORAGE_KEY = "hackme_display_timezone";
let currentMustChangePassword = false;
let forcedPasswordChangeMode = false;
function setForcedPasswordChangeState(enabled) {
  const active = !!enabled;
  forcedPasswordChangeMode = active;
  currentMustChangePassword = active;
  try {
    document.body?.toggleAttribute("data-forced-password-change", active);
  } catch (_) {}
  const cancelBtn = typeof $ === "function" ? $("user-edit-cancel") : null;
  if (cancelBtn) cancelBtn.style.display = active ? "none" : "";
}

let canManageUsers = false;
let currentModuleTab = "chat";
let currentServerTab = "overview";
let currentSystemTab = "health";
let users = [];
let adminUsersPage = 1;
let adminUsersPageSize = 25;
let adminUsersPagination = { page: 1, page_size: 25, total: 0, total_pages: 1, sort: "id", order: "asc", q: "" };
let adminUsersRoleCounts = {};
let editingUserId = null;
let editingUserIsSelf = false;
let userAppeals = [];
let userViolationFines = [];
let userFeatureRestrictions = [];
let adminAppeals = [];
let adminAppealPage = 1;
let adminAppealStatus = "pending";
let adminViolationFineStatus = "";
let adminReports = [];
let adminReportPage = 0;
let adminReportStatus = "pending";
const editingUserOriginal = {};
let selectedPendingUserIds = new Set();
let selectedAppealIds = new Set();
let selectedReportIds = new Set();
let chatRooms = [];
let selectedChatRoomId = null;
let chatPollTimer = null;
let chatMessageCache = [];
const CHAT_POLL_DEFAULT_MS = 2500;
const DEFAULT_INACTIVITY_LOGOUT_MS = 10 * 60 * 1000;
const IDLE_TIMEOUT_LOGOUT_STORAGE_KEY = "hackme_web.idle_timeout_logout_pending";
const AUTH_SESSION_HINT_STORAGE_KEY = "hackme_web.auth.session_hint";
const THREE_JS_SRC = "/js/three.min.js?v=0.160.0";
let inactivityLogoutMs = DEFAULT_INACTIVITY_LOGOUT_MS;
let inactivityTimer = null;
let inactivityCountdownTimer = null;
let inactivityDeadline = null;
let inactivityWarned = false;
const inactivitySuspendReasons = new Map();
let siteConfig = {};
let globalSiteConfig = {};
let userSiteAppearanceConfig = {};
let serverMeta = {};
let currentSettingsSection = "security";
let serverConnectionFailures = 0;
let serverConnectionSlowStreak = 0;
let serverConnectionTimer = null;
const SERVER_CONNECTION_MONITOR_DEFAULT_MS = 15000;
let notificationPollTimer = null;
let notificationsOpen = false;
const lazyScriptPromises = new Map();
const avatarCacheBustByUserId = new Map();
let tradingSnapshotRefreshPromise = null;
let tradingSnapshotRefreshLastAt = 0;
const SITE_APPEARANCE_KEYS = [
  "site_theme_mode",
  "site_bg",
  "site_surface",
  "site_accent",
  "site_accent2",
  "site_text",
  "site_muted",
  "site_layout_mode",
  "site_density",
  "site_radius_px",
  "site_font_scale",
  "site_content_width",
  "site_font_family",
  "site_background_style",
  "site_panel_style",
  "site_sidebar_width",
];
const SITE_FONT_FAMILY_MAP = {
  system: "'Segoe UI', system-ui, sans-serif",
  rounded: "'Trebuchet MS', 'Segoe UI', system-ui, sans-serif",
  serif: "'Iowan Old Style', 'Palatino Linotype', Georgia, ui-serif, serif",
  mono: "ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace",
};
const SITE_SIDEBAR_WIDTH_MAP = {
  compact: { expanded: 216, collapsed: 64 },
  standard: { expanded: 244, collapsed: 68 },
  wide: { expanded: 288, collapsed: 76 },
};
const SITE_THEME_MODE_PALETTES = {
  dark: {
    site_bg: "#11131d",
    site_surface: "#1b2030",
    site_text: "#eceef8",
    site_muted: "#aeb8cc",
  },
  light: {
    site_bg: "#eef2f6",
    site_surface: "#fbfcfe",
    site_text: "#243041",
    site_muted: "#5c6678",
  },
  custom: {},
};
const APP_TOAST_LIMIT = 4;
const ACTION_FEEDBACK_DURATION_MS = { ok: 1600, info: 900, err: 3000 };
const INLINE_MESSAGE_DURATION_MS = { ok: 2200, info: 1200, err: 4200 };
const TOAST_DURATION_MS = { ok: 2400, info: 1600, err: 4200 };
let lastAppToastSignature = "";
let lastAppToastAt = 0;
let lastServerBusyToastAt = 0;
let appDialogResolve = null;
let appDialogPreviousFocus = null;
const SERVER_BUSY_USER_MESSAGE = "目前是流量高峰，伺服器正在保護服務品質。請稍候再試。";

function clientRoleRank(role) {
  if (role === "super_admin") return 3;
  if (role === "manager") return 2;
  return 1;
}

function getModuleMinRole(moduleKey, fallbackRole) {
  const key = `module_${moduleKey}_min_role`;
  const value = siteConfig && typeof siteConfig[key] === "string" ? siteConfig[key] : fallbackRole;
  return ["user", "manager", "super_admin"].includes(value) ? value : fallbackRole;
}

function isFeatureEnabledForUi(featureKey, defaultValue = true) {
  const raw = String(featureKey || "");
  const key = raw.startsWith("feature_")
    ? raw
    : `feature_${raw.replace(/^feature_/, "").replace(/_enabled$/, "")}_enabled`;
  if (!siteConfig || typeof siteConfig !== "object" || !(key in siteConfig)) return defaultValue;
  return siteConfig[key] !== false;
}

function normalizeClientFeatureKey(featureKey) {
  const raw = String(featureKey || "").trim();
  if (!raw) return "";
  let key = raw.startsWith("feature_") ? raw : `feature_${raw}`;
  if (!key.endsWith("_enabled")) key = `${key}_enabled`;
  return key;
}

function setCurrentAllowedFeatures(features) {
  if (Array.isArray(features)) {
    currentAllowedFeatures = features.map((item) => normalizeClientFeatureKey(item)).filter(Boolean);
    return;
  }
  if (typeof features === "string") {
    currentAllowedFeatures = features.split(/[\s,]+/).map((item) => normalizeClientFeatureKey(item)).filter(Boolean);
    return;
  }
  currentAllowedFeatures = [];
}

function internalTestTokenAllowsFeature(featureKey) {
  if (currentAuthScope !== "internal_test_token") return false;
  const normalized = normalizeClientFeatureKey(featureKey);
  if (!normalized) return false;
  if (!currentAllowedFeatures.length) return true;
  return currentAllowedFeatures.includes(normalized);
}

const MODULE_FEATURE_KEYS = Object.freeze({
  chat: "feature_chat_enabled",
  announcements: "feature_community_enabled",
  community: "feature_community_enabled",
  appeals: "feature_appeals_enabled",
  accounts: "feature_accounts_enabled",
  privacy_uploads: "feature_privacy_uploads_enabled",
  albums: "feature_storage_albums_enabled",
  videos: "feature_videos_enabled",
  games: "feature_games_enabled",
  experiments: "feature_experiments_enabled",
  comfyui: "feature_comfyui_enabled",
  economy: "feature_economy_enabled",
  trading: "feature_trading_enabled",
});

function moduleFeatureEnabledForUi(moduleKey) {
  const featureKey = MODULE_FEATURE_KEYS[moduleKey] || "";
  if (!featureKey) return true;
  return isFeatureEnabledForUi(featureKey, false);
}

function canAccessModule(moduleKey, role = currentRole) {
  if (!moduleFeatureEnabledForUi(moduleKey)) return false;
  if (moduleKey === "experiments") {
    if (currentUser === "root") return true;
    return !!currentUser && internalTestTokenAllowsFeature("feature_experiments_enabled");
  }
  const fallback = moduleKey === "accounts" ? "manager" : "user";
  return clientRoleRank(role || "user") >= clientRoleRank(getModuleMinRole(moduleKey, fallback));
}

function $(id) { return document.getElementById(id); }
window.$ = $;

const EMPTY_HEADING_SELECTOR = [
  "h1",
  "h2",
  "h3",
  ".mini-title",
  ".drive-card-title",
  ".experiment-title",
  ".experiment-panel-title",
  ".health-section-title",
  ".server-env-panel-title",
  ".economy-supply-title",
  ".comfyui-root-summary-title",
].join(",");

function syncEmptyHeadingVisibility(root = document) {
  const scope = root && root.querySelectorAll ? root : document;
  const nodes = [];
  if (scope.matches?.(EMPTY_HEADING_SELECTOR)) nodes.push(scope);
  nodes.push(...scope.querySelectorAll(EMPTY_HEADING_SELECTOR));
  nodes.forEach((node) => {
    const empty = !(node.textContent || "").trim();
    node.classList.toggle("is-empty-heading", empty);
    node.toggleAttribute("aria-hidden", empty);
  });
}

function startEmptyHeadingVisibilityObserver() {
  if (window.__hackmeEmptyHeadingVisibilityObserverStarted) return;
  window.__hackmeEmptyHeadingVisibilityObserverStarted = true;
  const run = () => syncEmptyHeadingVisibility(document);
  const start = () => {
    run();
    const observer = new MutationObserver((mutations) => {
      const roots = new Set();
      mutations.forEach((mutation) => {
        const target = mutation.target?.nodeType === Node.ELEMENT_NODE
          ? mutation.target
          : mutation.target?.parentElement;
        if (!target) return;
        if (target.matches?.(EMPTY_HEADING_SELECTOR)) {
          roots.add(target);
        } else if (target.querySelector?.(EMPTY_HEADING_SELECTOR)) {
          roots.add(target);
        }
      });
      roots.forEach((target) => syncEmptyHeadingVisibility(target));
    });
    observer.observe(document.body, { childList: true, characterData: true, subtree: true });
  };
  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start, { once: true });
}

startEmptyHeadingVisibilityObserver();

const ACCOUNT_SCOPE_STORAGE_KEY = "hackme_web.account.active_scope";

function accountScopeFromIdentity(userId = currentUserId, username = currentUser) {
  const id = Number(userId || 0);
  if (Number.isFinite(id) && id > 0) return `user:${id}`;
  const name = String(username || "").trim().toLowerCase();
  return name ? `name:${name}` : "anonymous";
}

function getCurrentAccountStorageScope() {
  return accountScopeFromIdentity(currentUserId, currentUser);
}

function accountScopedStorageKey(key, scope = getCurrentAccountStorageScope()) {
  return `hackme_web:${scope}:${String(key || "state")}`;
}

function syncActiveAccountStorageScope(previousScope = null) {
  const nextScope = getCurrentAccountStorageScope();
  try {
    if (nextScope === "anonymous") localStorage.removeItem(ACCOUNT_SCOPE_STORAGE_KEY);
    else localStorage.setItem(ACCOUNT_SCOPE_STORAGE_KEY, nextScope);
  } catch (err) {}
  if (previousScope !== null && previousScope !== nextScope) {
    document.dispatchEvent(new CustomEvent("hackme:account-context-changed", {
      detail: {
        previousScope,
        nextScope,
        userId: currentUserId,
        username: currentUser,
      },
    }));
  }
}

const SIDEBAR_COLLAPSED_STORAGE_KEY = "hackme_web.sidebar.collapsed";
const SIDEBAR_ICON_PATHS = {
  chat: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7A2.5 2.5 0 0 1 17.5 15H9l-5 4v-4.5A2.5 2.5 0 0 1 4 12.5z"/>',
  mail: '<path d="M4 6h16v12H4z"/><path d="m4 7 8 6 8-6"/>',
  bell: '<path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6"/><path d="M10 19a2 2 0 0 0 4 0"/>',
  forum: '<path d="M5 5h14v9H8l-3 3z"/><path d="M8 8h8M8 11h5"/>',
  drive: '<path d="M4 8h16l-2 10H6z"/><path d="m7 8 2-3h6l2 3"/>',
  image: '<path d="M5 5h14v14H5z"/><path d="m7 16 4-4 3 3 2-2 2 3"/><path d="M8.5 8.5h.01"/>',
  video: '<path d="M5 5h11v14H5z"/><path d="m16 9 5-3v12l-5-3z"/><path d="M8 9h5M8 13h3"/>',
  share: '<path d="M18 8a3 3 0 1 0-2.83-4H15a3 3 0 0 0 .17 1L8.9 8.13a3 3 0 1 0 0 3.74L15.17 15A3 3 0 1 0 14 17.4L7.73 14.27a3 3 0 0 0 0-4.54L14 6.6A3 3 0 0 0 18 8z"/>',
  game: '<path d="M8 4h8v4h-3v3h3v3h-3v6h-2v-6H8v-3h3V8H8z"/><path d="M5 20h14"/>',
  spark: '<path d="M12 3 9.5 9.5 3 12l6.5 2.5L12 21l2.5-6.5L21 12l-6.5-2.5z"/>',
  wallet: '<path d="M4 7.5A2.5 2.5 0 0 1 6.5 5H19v14H6.5A2.5 2.5 0 0 1 4 16.5z"/><path d="M16 11h4v4h-4z"/><path d="M7 5V3.8L17 5"/>',
  appeal: '<path d="M6 4h12v16H6z"/><path d="M9 8h6M9 12h6M9 16h3"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><path d="M9.5 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  shield: '<path d="M12 3 20 6v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="m9 12 2 2 4-5"/>',
  system: '<path d="M4 5h16v10H4z"/><path d="M8 19h8"/><path d="M12 15v4"/><path d="M8 9h3M8 12h6"/>',
};
const SIDEBAR_MENU_CONFIG = [
  { tabId: "tab-module-announcements", module: "community", tab: "announcements", icon: "bell", label: "公告", group: "公告" },
  { tabId: "tab-module-chat", module: "chat", tab: "chat", icon: "chat", label: "聊天", group: "社交" },
  {
    tabId: "tab-module-profile",
    module: "profile",
    tab: "profile",
    icon: "users",
    label: "個人面板",
    group: "社交",
    submenu: [
      { label: "我的主頁", action: "profile:home" },
      { label: "主頁資料", action: "profile:edit" },
      { label: "主頁外觀", action: "profile:appearance" },
      { label: "帳號安全", action: "profile:account" },
      { label: "好友", action: "profile:friends" },
    ],
  },
  {
    tabId: "tab-module-community",
    module: "community",
    tab: "community",
    icon: "forum",
    label: "社群",
    group: "社交",
    submenu: [
      { label: "看板清單", action: "module:community" },
      { label: "主題審核", action: "community:review", requiresCommunityReview: true },
    ],
  },
  {
    tabId: "tab-module-drive",
    module: "privacy_uploads",
    tab: "drive",
    icon: "drive",
    label: "雲端硬碟",
    group: "功能",
    submenu: [
      { label: "檔案清單", action: "module:drive" },
      { label: "相簿", action: "module:albums", moduleKey: "privacy_uploads", featureKey: "feature_storage_albums_enabled" },
    ],
  },
  { tabId: "tab-module-videos", module: "videos", tab: "videos", icon: "video", label: "影音", group: "功能" },
  { tabId: "tab-module-games", module: "games", tab: "games", icon: "game", label: "遊戲區", group: "功能" },
  { tabId: "tab-module-comfyui", module: "comfyui", tab: "comfyui", icon: "spark", label: "AI 產圖", group: "功能" },
  {
    tabId: "tab-module-economy",
    module: "economy",
    tab: "economy",
    icon: "wallet",
    label: "積分錢包",
    group: "帳務",
    submenu: [
      { label: "錢包管理", action: "economy:balance" },
      { label: "鏈上交易", action: "economy:transactions" },
      { label: "鏈上瀏覽器", action: "economy:explorer" },
      { label: "治理看板", action: "economy:governance" },
      { label: "積分私有鏈", action: "economy:chain", role: "root" },
    ],
  },
  {
    tabId: "tab-module-trading",
    module: "trading",
    tab: "trading",
    icon: "wallet",
    label: "積分交易所",
    group: "帳務",
    submenu: [
      { label: "交易所", action: "module:trading" },
      { label: "我的倉位", action: "trading:my-positions", hideForRoot: true },
      { label: "全站倉位看板", action: "trading:sitewide-positions", role: "root" },
      { label: "基金營運管理", action: "trading:fund-ops", role: "root" },
      { label: "root 模擬倉位", action: "trading:root-sim", role: "root" },
      { label: "交易所設定", action: "trading:settings", role: "root" },
    ],
  },
  { tabId: "tab-module-jobs", module: "jobs", tab: "jobs", icon: "bell", label: "任務中心", group: "管理" },
  { tabId: "tab-module-shares", module: "shares", tab: "shares", icon: "share", label: "分享管理", group: "管理" },
  { tabId: "tab-module-appeals", module: "appeals", tab: "appeals", icon: "appeal", label: "申覆", group: "管理", hideForSuperAdmin: true },
  {
    tabId: "tab-module-accounts",
    module: "accounts",
    tab: "accounts",
    icon: "users",
    label: "帳號管理",
    group: "管理",
    submenu: [
      { label: "帳號", action: "admin:users" },
      { label: "違規計次", action: "admin:violations", featureKey: "feature_violation_center_enabled" },
      { label: "會員治理", action: "admin:governance", featureKey: "feature_member_governance_enabled" },
      { label: "發放通知", action: "admin:notices", featureKey: "feature_reports_notifications_enabled" },
      { label: "申覆審核", action: "admin:appeals", featureKey: "feature_appeals_enabled" },
      { label: "訊息檢舉", action: "admin:reports", featureKey: "feature_reports_enabled" },
    ],
  },
  {
    tabId: "tab-module-server",
    role: "root",
    tab: "server",
    icon: "shield",
    label: "安全中心",
    group: "管理",
    submenu: [
      { label: "總覽", action: "server:overview" },
      { label: "伺服器模式", action: "server:server-mode" },
      { label: "審計日誌", action: "server:audit", featureKey: "feature_audit_log_enabled" },
      { label: "Integrity Guard", action: "server:integrity" },
    ],
  },
  {
    tabId: "tab-module-system",
    role: "root",
    tab: "system",
    icon: "system",
    label: "系統管理",
    group: "管理",
    submenu: [
      { label: "健康度", action: "system:health", featureKey: "feature_system_health_enabled" },
      { label: "功能", action: "system:features" },
      { label: "外觀", action: "system:appearance" },
      { label: "系統", action: "system:core" },
      { label: "請求容量", action: "system:capacity" },
      { label: "系統環境", action: "system:env" },
      { label: "Bug 回報", action: "system:bug-reports" },
    ],
  },
  { tabId: "tab-module-experiments", module: "experiments", tab: "experiments", icon: "spark", label: "實驗區", group: "實驗區" },
];

function sidebarIconSvg(icon) {
  const paths = SIDEBAR_ICON_PATHS[icon] || SIDEBAR_ICON_PATHS.chat;
  return `<svg class="sidebar-icon-svg" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${paths}</svg>`;
}

function sidebarItemForTab(tabId) {
  return SIDEBAR_MENU_CONFIG.find((item) => item.tabId === tabId);
}

function canShowSidebarItem(item) {
  if (!item || !currentUser) return false;
  if (item.hideForSuperAdmin && currentRole === "super_admin") return false;
  if (item.role === "root") return currentUser === "root";
  if (item.role === "super_admin") return currentRole === "super_admin";
  if (Array.isArray(item.requiresFeatures) && item.requiresFeatures.some((key) => !isFeatureEnabledForUi(key))) return false;
  if (Array.isArray(item.accessAny) && item.accessAny.length) return item.accessAny.some((key) => canAccessModule(key));
  if (item.module === "trading") return canAccessModule("economy") && canAccessModule("trading");
  return canAccessModule(item.module);
}

function canShowSidebarSubitem(sub) {
  if (!sub) return false;
  if (sub.hideForRoot && currentUser === "root") return false;
  if (sub.role === "root" && currentUser !== "root") return false;
  if (sub.role === "super_admin" && currentRole !== "super_admin") return false;
  if (Array.isArray(sub.roles) && !sub.roles.includes(currentRole)) return false;
  if (sub.moduleKey && !canAccessModule(sub.moduleKey)) return false;
  if (sub.featureKey && !isFeatureEnabledForUi(sub.featureKey)) return false;
  if (sub.requiresCommunityReview) {
    if (typeof canOpenCommunityReviewMode === "function") return canOpenCommunityReviewMode();
    return currentRole === "manager" || currentRole === "super_admin";
  }
  return true;
}

function isSidebarEntryVisible(node) {
  return !!node && !node.hidden && node.style.display !== "none";
}

function sidebarGroupHasVisibleEntries(group) {
  if (!group) return false;
  let node = group.nextElementSibling;
  while (node && !node.classList.contains("sidebar-group")) {
    if (node.classList.contains("tab") && isSidebarEntryVisible(node)) return true;
    node = node.nextElementSibling;
  }
  return false;
}

function decorateSidebarMenu() {
  SIDEBAR_MENU_CONFIG.forEach((item) => {
    const button = $(item.tabId);
    if (!button || button.dataset.sidebarDecorated === "1") return;
    if (item.group && !$("sidebar-group-" + item.group)) {
      const group = document.createElement("div");
      group.className = "sidebar-group";
      group.id = "sidebar-group-" + item.group;
      group.dataset.sidebarGroup = item.group;
      group.textContent = item.group;
      button.insertAdjacentElement("beforebegin", group);
    }
    button.dataset.sidebarDecorated = "1";
    button.dataset.sidebarTab = item.tab;
    button.dataset.sidebarGroup = item.group || "";
    button.title = item.label;
    button.innerHTML = `<span class="sidebar-icon">${sidebarIconSvg(item.icon)}</span><span class="sidebar-label">${sanitize(item.label)}</span>${item.submenu ? '<span class="sidebar-caret">›</span>' : ""}`;
    if (item.submenu && !$(item.tabId + "-submenu")) {
      const submenu = document.createElement("div");
      submenu.className = "sidebar-submenu";
      submenu.id = item.tabId + "-submenu";
      submenu.dataset.parentTab = item.tab;
      submenu.dataset.sidebarGroup = item.group || "";
      submenu.innerHTML = item.submenu.map((sub) => `<button class="sidebar-subitem" type="button" data-sidebar-action="${sanitize(sub.action)}">${sanitize(sub.label)}</button>`).join("");
      button.insertAdjacentElement("afterend", submenu);
    }
  });
}

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", !!collapsed);
  const toggle = $("sidebar-toggle");
  if (toggle) {
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    toggle.setAttribute("aria-label", collapsed ? "展開側邊欄" : "收合側邊欄");
    const isMobile = typeof window !== "undefined" && window.matchMedia && window.matchMedia("(max-width: 860px)").matches;
    toggle.textContent = isMobile ? (collapsed ? "☰" : "×") : "‹";
  }
  try {
    localStorage.setItem(sidebarCollapsedStorageKey(), collapsed ? "1" : "0");
  } catch (err) {}
  updateSidebarActiveState();
}

function sidebarCollapsedStorageKey() {
  const roleKey = currentRole || "guest";
  return accountScopedStorageKey(`${SIDEBAR_COLLAPSED_STORAGE_KEY}.${roleKey}`);
}

function restoreSidebarState() {
  let collapsed = typeof window !== "undefined" && window.matchMedia && window.matchMedia("(max-width: 860px)").matches;
  try {
    const stored = localStorage.getItem(sidebarCollapsedStorageKey());
    if (stored !== null) collapsed = stored === "1";
  } catch (err) {}
  setSidebarCollapsed(collapsed);
}

function collapseSidebarAfterMobileNavigation() {
  if (typeof window === "undefined" || !window.matchMedia) return;
  if (!window.matchMedia("(max-width: 860px)").matches) return;
  setSidebarCollapsed(true);
}

function syncSidebarMenuVisibility() {
  decorateSidebarMenu();
  const configuredTabIds = new Set(SIDEBAR_MENU_CONFIG.map((item) => item.tabId));
  document.querySelectorAll("#module-main-tabs > .tab[id^='tab-module-']").forEach((button) => {
    if (!configuredTabIds.has(button.id)) button.style.display = "none";
  });
  SIDEBAR_MENU_CONFIG.forEach((item) => {
    const button = $(item.tabId);
    const submenu = $(item.tabId + "-submenu");
    const visible = canShowSidebarItem(item);
    if (button) button.style.display = visible ? "" : "none";
    if (submenu) submenu.style.display = visible ? "" : "none";
    if (submenu && item.submenu) {
      submenu.querySelectorAll("[data-sidebar-action]").forEach((subButton) => {
        const sub = item.submenu.find((candidate) => candidate.action === subButton.dataset.sidebarAction);
        subButton.style.display = visible && canShowSidebarSubitem(sub) ? "" : "none";
      });
    }
  });
  document.querySelectorAll("[data-sidebar-group]").forEach((group) => {
    if (!group.classList.contains("sidebar-group")) return;
    group.style.display = sidebarGroupHasVisibleEntries(group) ? "" : "none";
  });
  updateSidebarIdentity();
  updateSidebarActiveState();
}

function updateSidebarIdentity() {
  const user = $("sidebar-current-user");
  const role = $("sidebar-current-role");
  const level = $("sidebar-current-level");
  const avatar = $("sidebar-user-avatar");
  const points = $("sidebar-points");
  const violations = $("sidebar-violations");
  const effective = $("sidebar-effective-level");
  if (user) user.textContent = currentUser || "未登入";
  if (role) role.textContent = currentRoleLabel || currentRole || "-";
  if (level) level.textContent = currentUser ? (level.dataset.memberLevel || "-") : "-";
  if (avatar) {
    avatar.innerHTML = currentUser ? userAvatarInnerMarkup(currentUserId, currentUser, currentUserAvatarFileId) : '<span class="user-avatar-fallback">-</span>';
    avatar.setAttribute("title", currentUser || "未登入");
    bindAvatarFallbacks(avatar);
  }
  if (points) points.textContent = currentUser ? (points.dataset.points || "0") : "0";
  if (violations) violations.textContent = currentUser ? (violations.dataset.violations || "0") : "0";
  if (effective) effective.textContent = currentUser ? (effective.dataset.effectiveLevel || "-") : "-";
}

function isSidebarActionActive(action) {
  if (!action) return false;
  if (action.startsWith("server:")) return currentModuleTab === "server" && currentServerTab === action.split(":")[1];
  if (action.startsWith("system:")) return currentModuleTab === "system" && currentSystemTab === action.split(":")[1];
  if (action.startsWith("admin:")) return currentModuleTab === "accounts" && typeof currentAdminTab !== "undefined" && currentAdminTab === action.split(":")[1];
  if (action === "profile:appearance") return currentModuleTab === "profile" && typeof profileQuickCustomizeOpen !== "undefined" && !!profileQuickCustomizeOpen;
  if (action.startsWith("profile:")) return currentModuleTab === "profile" && typeof currentProfileTab !== "undefined" && currentProfileTab === action.split(":")[1];
  if (action.startsWith("economy:")) return currentModuleTab === "economy" && typeof economyActivePage !== "undefined" && economyActivePage === action.split(":")[1];
  if (action === "module:trading" || action === "trading:spot" || action === "trading:bots" || action === "trading:lending") return currentModuleTab === "trading" && (typeof tradingActivePage === "undefined" || tradingActivePage === "spot" || tradingActivePage === "exchange");
  if (action === "trading:my-positions") return currentModuleTab === "trading" && typeof tradingActivePage !== "undefined" && tradingActivePage === "my-positions";
  if (action === "trading:sitewide-positions") return currentModuleTab === "trading" && typeof tradingActivePage !== "undefined" && tradingActivePage === "sitewide-positions";
  if (action === "trading:sitewide-pools" || action === "trading:fund-ops") return currentModuleTab === "trading" && typeof tradingActivePage !== "undefined" && (tradingActivePage === "fund-ops" || tradingActivePage === "sitewide-pools");
  if (action === "trading:root-sim") return currentModuleTab === "trading" && typeof tradingActivePage !== "undefined" && tradingActivePage === "root-sim";
  if (action === "trading:settings") return currentModuleTab === "trading" && typeof tradingActivePage !== "undefined" && tradingActivePage === "settings";
  if (action === "module:" + currentModuleTab) return true;
  if (action === "community:review") return currentModuleTab === "community" && typeof communityMode !== "undefined" && communityMode === "review";
  return false;
}

function updateSidebarActiveState() {
  const collapsed = document.body.classList.contains("sidebar-collapsed");
  SIDEBAR_MENU_CONFIG.forEach((item) => {
    const submenu = $(item.tabId + "-submenu");
    const button = $(item.tabId);
    if (!button) return;
    const submenuActive = Array.isArray(item.submenu) && item.submenu.some((sub) => isSidebarActionActive(sub.action));
    const isActive = button.classList.contains("active") || submenuActive;
    button.classList.toggle("active", isActive);
    if (!submenu) return;
    submenu.classList.toggle("show", isActive && !collapsed);
    submenu.querySelectorAll("[data-sidebar-action]").forEach((sub) => {
      const action = sub.dataset.sidebarAction || "";
      sub.classList.toggle("active", isSidebarActionActive(action));
    });
  });
}

function showLoginScreen() {
  document.body.classList.remove("app-authenticated");
  const successScreen = $("success-screen");
  if (successScreen) successScreen.classList.remove("show");
  const adminWrap = $("admin-wrap");
  if (adminWrap) adminWrap.className = "admin-wrap";
  const authCard = $("auth-card");
  if (authCard) authCard.style.display = "";
  const loginSection = $("sec-login");
  const registerSection = $("sec-register");
  const loginTab = $("tab-login");
  const registerTab = $("tab-register");
  if (loginSection) loginSection.classList.add("active");
  if (registerSection) registerSection.classList.remove("active");
  if (loginTab) loginTab.classList.add("active");
  if (registerTab) registerTab.classList.remove("active");
}

function runSidebarAction(action) {
  if (!action) return;
  const [scope, value] = action.split(":");
  if (scope === "module" && value && typeof switchModuleTab === "function") {
    switchModuleTab(value);
    if (value === "trading" && typeof openTradingExchangePage === "function") openTradingExchangePage();
    return;
  }
  if (scope === "server" && value && typeof switchModuleTab === "function" && typeof switchServerTab === "function") {
    switchModuleTab("server");
    switchServerTab(value);
    return;
  }
  if (scope === "system" && value && typeof switchModuleTab === "function" && typeof switchSystemTab === "function") {
    switchModuleTab("system");
    switchSystemTab(value);
    return;
  }
  if (scope === "admin" && value && typeof switchModuleTab === "function" && typeof switchAdminTab === "function") {
    switchModuleTab("accounts");
    switchAdminTab(value);
    return;
  }
  if (scope === "economy" && value && typeof switchModuleTab === "function") {
    switchModuleTab("economy");
    if (typeof setEconomyActivePage === "function") setEconomyActivePage(value);
    return;
  }
  if (scope === "trading" && value && typeof switchModuleTab === "function") {
    switchModuleTab("trading");
    if (value === "spot" && typeof openTradingSpotPage === "function") {
      openTradingSpotPage();
      return;
    }
    if (value === "bots" && typeof openTradingBotPanel === "function") {
      openTradingBotPanel("mybots");
      return;
    }
    if (value === "lending" && typeof openTradingLendingPanel === "function") {
      openTradingLendingPanel();
      return;
    }
    if (value === "my-positions" && typeof openTradingMyPositionsPanel === "function") {
      openTradingMyPositionsPanel();
      return;
    }
    if (value === "sitewide-positions" && typeof openTradingRootSitewidePanel === "function") {
      openTradingRootSitewidePanel("positions", { refreshSnapshot: false });
      return;
    }
    if ((value === "sitewide-pools" || value === "fund-ops") && typeof openTradingRootFundOpsPanel === "function") {
      openTradingRootFundOpsPanel({ refreshSnapshot: false });
      return;
    }
    if (value === "root-sim" && typeof openTradingRootSimulationPanel === "function") {
      openTradingRootSimulationPanel();
      return;
    }
    if (value === "settings" && typeof openTradingSettingsPage === "function") {
      openTradingSettingsPage();
      return;
    }
    return;
  }
  if (scope === "profile" && value && typeof switchModuleTab === "function") {
    switchModuleTab("profile");
    if (value === "account") {
      if (currentUserId && typeof editUser === "function") editUser(currentUserId);
      return;
    }
    if (typeof switchProfileTab === "function") switchProfileTab(value);
    return;
  }
  if (action === "community:review" && typeof switchModuleTab === "function") {
    if (typeof canOpenCommunityReviewMode === "function" && !canOpenCommunityReviewMode()) {
      switchModuleTab("community");
      return;
    }
    switchModuleTab("community");
    if (typeof switchCommunityMode === "function") switchCommunityMode("review");
  }
}

function stopInactivityTimer() {
  if (inactivityTimer) {
    clearTimeout(inactivityTimer);
    inactivityTimer = null;
  }
  if (inactivityCountdownTimer) {
    clearInterval(inactivityCountdownTimer);
    inactivityCountdownTimer = null;
  }
  inactivityDeadline = null;
  inactivityWarned = false;
  if (renderInactivitySuspendedState()) return;
  const label = $("session-countdown-label");
  if (label) {
    label.textContent = currentUser && inactivityLogoutMs <= 0 ? "閒置登出：停用" : (currentUser ? "閒置登出：--:--" : "未登入");
    label.style.color = "var(--muted)";
  }
}

function isInactivityCountdownSuspended() {
  return inactivitySuspendReasons.size > 0;
}

function currentInactivitySuspendLabel() {
  const labels = Array.from(inactivitySuspendReasons.values()).filter(Boolean);
  return labels.length ? labels[labels.length - 1] : "系統工作進行中";
}

function renderInactivitySuspendedState() {
  const label = $("session-countdown-label");
  if (!label || !currentUser || !isInactivityCountdownSuspended()) return false;
  label.textContent = `閒置登出：${currentInactivitySuspendLabel()}，暫停`;
  label.style.color = "#4caf50";
  return true;
}

function setInactivitySuspendState(reason, active, labelText = "系統工作進行中") {
  const key = String(reason || "generic").trim() || "generic";
  if (active) inactivitySuspendReasons.set(key, String(labelText || "系統工作進行中"));
  else inactivitySuspendReasons.delete(key);
  if (!currentUser) return;
  if (isInactivityCountdownSuspended()) {
    stopInactivityTimer();
    return;
  }
  if (inactivityLogoutMs > 0) resetInactivityTimer();
  else stopInactivityTimer();
}

function resetInactivityTimer() {
  if (!currentUser) return;
  if (!inactivityLogoutMs || inactivityLogoutMs <= 0) {
    stopInactivityTimer();
    return;
  }
  if (isInactivityCountdownSuspended()) {
    stopInactivityTimer();
    return;
  }
  stopInactivityTimer();
  inactivityDeadline = Date.now() + inactivityLogoutMs;
  updateInactivityCountdown();
  inactivityCountdownTimer = setInterval(updateInactivityCountdown, 1000);
  inactivityTimer = setTimeout(async () => {
    alert(`已閒置 ${formatInactivityTimeoutLabel()}，系統將自動登出。`);
    if (typeof forceIdleTimeoutLogout === "function") await forceIdleTimeoutLogout();
    else await doLogout({ immediate: true });
  }, inactivityLogoutMs);
}

function updateInactivityCountdown() {
  const label = $("session-countdown-label");
  if (!label) return;
  if (renderInactivitySuspendedState()) return;
  if (!currentUser || !inactivityDeadline) {
    label.textContent = currentUser && inactivityLogoutMs <= 0 ? "閒置登出：停用" : (currentUser ? "閒置登出：--:--" : "未登入");
    label.style.color = "var(--muted)";
    return;
  }
  const remaining = Math.max(0, inactivityDeadline - Date.now());
  const seconds = Math.ceil(remaining / 1000);
  const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
  const ss = String(seconds % 60).padStart(2, "0");
  label.textContent = `閒置登出：${mm}:${ss}`;
  if (seconds <= 30) {
    label.style.color = "#ff4f6d";
    if (!inactivityWarned) {
      inactivityWarned = true;
      const msg = $("li-msg");
      if (msg) {
        msg.textContent = "即將因閒置自動登出，請移動滑鼠或按鍵延長登入狀態。";
        msg.style.color = "#ffb74d";
      }
    }
  } else if (seconds <= 60) {
    label.style.color = "#ffb74d";
  } else {
    label.style.color = "var(--muted)";
  }
}

function setupInactivityTracking() {
  ["click", "keydown", "mousemove", "touchstart", "scroll"].forEach((eventName) => {
    window.addEventListener(eventName, resetInactivityTimer, { passive: true });
  });
}

// ── Sanitization (XSS defense — defense in depth) ────────────
function sanitize(str) {
  if (typeof str !== 'string') return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
    .replace(/\//g, '&#x2F;');
}

function chatMessageUrlParts(rawUrl) {
  let url = String(rawUrl || "");
  let suffix = "";
  while (url && /[.,!?;:，。！？；：）\])}】》]/.test(url[url.length - 1])) {
    suffix = url[url.length - 1] + suffix;
    url = url.slice(0, -1);
  }
  return { url, suffix };
}

function chatSafeMessageHref(rawUrl) {
  const candidate = String(rawUrl || "").trim();
  if (!candidate) return "";
  try {
    if (candidate.startsWith("/")) {
      if (!candidate.startsWith("/shared/videos/")) return "";
      return new URL(candidate, window.location.origin).href;
    }
    const parsed = new URL(candidate);
    if (!["http:", "https:"].includes(parsed.protocol)) return "";
    return parsed.href;
  } catch (_err) {
    return "";
  }
}

function renderChatMessageContent(content) {
  const text = String(content || "");
  const urlRe = /(https?:\/\/[^\s<>"']+|\/shared\/videos\/[A-Za-z0-9_-]+(?:[?#][^\s<>"']*)?)/g;
  let cursor = 0;
  const parts = [];
  for (const match of text.matchAll(urlRe)) {
    const index = Number(match.index || 0);
    const raw = String(match[0] || "");
    if (index > cursor) parts.push(sanitize(text.slice(cursor, index)));
    const split = chatMessageUrlParts(raw);
    const href = chatSafeMessageHref(split.url);
    if (href) {
      parts.push(`<a class="chat-inline-link" href="${sanitize(href)}" target="_blank" rel="noopener noreferrer">${sanitize(split.url)}</a>${sanitize(split.suffix)}`);
    } else {
      parts.push(sanitize(raw));
    }
    cursor = index + raw.length;
  }
  if (cursor < text.length) parts.push(sanitize(text.slice(cursor)));
  return `<div class="chat-message-content">${parts.join("").replace(/\n/g, "<br>")}</div>`;
}

function shareExpiryTodayDate() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shareExpiryPartsFromValue(value) {
  const raw = String(value || "").trim();
  const match = raw.match(/^(\d{4}-\d{2}-\d{2})(?:[T\s](\d{2}:\d{2}))?/);
  return {
    date: match ? match[1] : "",
    time: match && match[2] ? match[2] : "23:59",
  };
}

function composeShareExpiryValue(dateValue, timeValue) {
  const date = String(dateValue || "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return "";
  const time = /^\d{2}:\d{2}$/.test(String(timeValue || "").trim())
    ? String(timeValue || "").trim()
    : "23:59";
  return `${date}T${time}`;
}

function shareExpiryPickerMarkup(options = {}) {
  const parts = shareExpiryPartsFromValue(options.value || "");
  const hiddenId = String(options.hiddenId || "").trim();
  const hiddenAttrs = String(options.hiddenAttrs || "").trim();
  const targetAttr = hiddenId ? ` data-expiry-target="${sanitize(hiddenId)}"` : "";
  const idAttr = hiddenId ? ` id="${sanitize(hiddenId)}"` : "";
  const help = options.help
    ? `<div class="expiry-picker-help">${sanitize(String(options.help))}</div>`
    : "";
  return `
    <div class="expiry-picker" data-expiry-picker${targetAttr}>
      <input type="hidden"${idAttr} ${hiddenAttrs} data-expiry-value value="${sanitize(composeShareExpiryValue(parts.date, parts.time))}" />
      <div class="expiry-picker-grid">
        <label class="expiry-picker-field">
          <span>日期</span>
          <input type="date" data-expiry-date min="${sanitize(shareExpiryTodayDate())}" value="${sanitize(parts.date)}" />
        </label>
        <label class="expiry-picker-field">
          <span>時間</span>
          <input type="time" data-expiry-time step="60" value="${sanitize(parts.time)}" />
        </label>
        <button class="btn btn-sm expiry-picker-clear" type="button" data-expiry-clear>清除</button>
      </div>
      ${help}
    </div>
  `;
}

function shareExpiryPickerForValueInput(valueInput) {
  if (!valueInput) return null;
  const direct = valueInput.closest?.("[data-expiry-picker]");
  if (direct) return direct;
  if (valueInput.id) {
    return Array.from(document.querySelectorAll("[data-expiry-picker]"))
      .find((picker) => picker.dataset.expiryTarget === valueInput.id) || null;
  }
  return null;
}

function syncShareExpiryPicker(picker) {
  if (!picker) return "";
  const hidden = picker.querySelector("[data-expiry-value]");
  const dateInput = picker.querySelector("[data-expiry-date]");
  const timeInput = picker.querySelector("[data-expiry-time]");
  if (dateInput && !dateInput.min) dateInput.min = shareExpiryTodayDate();
  const value = composeShareExpiryValue(dateInput?.value || "", timeInput?.value || "");
  if (hidden) hidden.value = value;
  return value;
}

function syncShareExpiryPickers(root = document) {
  if (root.matches?.("[data-expiry-picker]")) syncShareExpiryPicker(root);
  root.querySelectorAll?.("[data-expiry-picker]").forEach((picker) => syncShareExpiryPicker(picker));
}

function setShareExpiryPickerValue(targetOrInput, value = "") {
  const input = typeof targetOrInput === "string" ? $(targetOrInput) : targetOrInput;
  if (input) input.value = String(value || "").trim();
  const picker = shareExpiryPickerForValueInput(input);
  if (!picker) return input?.value || "";
  const parts = shareExpiryPartsFromValue(value || "");
  const dateInput = picker.querySelector("[data-expiry-date]");
  const timeInput = picker.querySelector("[data-expiry-time]");
  if (dateInput) {
    dateInput.min = dateInput.min || shareExpiryTodayDate();
    dateInput.value = parts.date;
  }
  if (timeInput) timeInput.value = parts.time;
  return syncShareExpiryPicker(picker);
}

function getShareExpiryPickerValue(targetOrInput) {
  const input = typeof targetOrInput === "string" ? $(targetOrInput) : targetOrInput;
  const picker = shareExpiryPickerForValueInput(input);
  if (picker) return syncShareExpiryPicker(picker);
  return String(input?.value || "").trim();
}

function installShareExpiryPickerHandlers() {
  if (document.body?.dataset.expiryPickerBound === "1") return;
  if (document.body) document.body.dataset.expiryPickerBound = "1";
  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!target?.matches?.("[data-expiry-date], [data-expiry-time]")) return;
    syncShareExpiryPicker(target.closest("[data-expiry-picker]"));
  });
  document.addEventListener("input", (event) => {
    const target = event.target;
    if (!target?.matches?.("[data-expiry-date], [data-expiry-time]")) return;
    syncShareExpiryPicker(target.closest("[data-expiry-picker]"));
  });
  document.addEventListener("click", (event) => {
    const clear = event.target?.closest?.("[data-expiry-clear]");
    if (!clear) return;
    const picker = clear.closest("[data-expiry-picker]");
    const hidden = picker?.querySelector("[data-expiry-value]");
    if (hidden) setShareExpiryPickerValue(hidden, "");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  installShareExpiryPickerHandlers();
  syncShareExpiryPickers(document);
});

window.shareExpiryPickerMarkup = shareExpiryPickerMarkup;
window.syncShareExpiryPickers = syncShareExpiryPickers;
window.setShareExpiryPickerValue = setShareExpiryPickerValue;
window.getShareExpiryPickerValue = getShareExpiryPickerValue;

async function enqueueTradingSnapshotRefreshOnce(reason = "missing_snapshot") {
  if (currentUser !== "root") return { ok: false, skipped: true, reason: "not_root" };
  const now = Date.now();
  if (tradingSnapshotRefreshPromise) return tradingSnapshotRefreshPromise;
  if (now - tradingSnapshotRefreshLastAt < 15000) {
    return { ok: true, throttled: true, reason: "recently_queued" };
  }
  tradingSnapshotRefreshLastAt = now;
  tradingSnapshotRefreshPromise = (async () => {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(`${API}/root/trading/background/run-once`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || "",
      },
      body: JSON.stringify({
        job_key: "sitewide_metrics_refresh",
        confirm: "RUN_TRADING_JOB_ONCE",
        reason,
      }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      return {
        ok: false,
        status: res.status,
        msg: json.msg || `HTTP ${res.status}`,
      };
    }
    return json;
  })();
  try {
    return await tradingSnapshotRefreshPromise;
  } finally {
    window.setTimeout(() => {
      tradingSnapshotRefreshPromise = null;
    }, 500);
  }
}

window.enqueueTradingSnapshotRefreshOnce = enqueueTradingSnapshotRefreshOnce;

function avatarInitial(username) {
  const value = String(username || "").trim();
  return value ? value.slice(0, 1).toUpperCase() : "?";
}

function avatarUrlForUser(userId, avatarFileId = "") {
  if (!userId || !avatarFileId) return "";
  const bust = avatarCacheBustByUserId.get(String(userId));
  const suffix = bust ? `?v=${encodeURIComponent(bust)}` : "";
  return `${API}/admin/users/${encodeURIComponent(userId)}/avatar${suffix}`;
}

function userAvatarInnerMarkup(userId, username, avatarFileId = "") {
  const url = avatarUrlForUser(userId, avatarFileId);
  const label = `${username || "使用者"} 大頭貼`;
  return `
    <span class="user-avatar-fallback">${sanitize(avatarInitial(username))}</span>
    ${url ? `<img class="user-avatar-img" src="${sanitize(url)}" alt="${sanitize(label)}" loading="lazy">` : ""}
  `;
}

function userAvatarMarkup(userId, username, extraClass = "", avatarFileId = "") {
  return `<span class="user-avatar ${sanitize(extraClass)}" title="${sanitize(username || "使用者")}">${userAvatarInnerMarkup(userId, username, avatarFileId)}</span>`;
}

function userIdentityMarkup(userId, username, meta = "", extraClass = "", avatarFileId = "") {
  const canOpenProfile = userId && username && !["系統", "匿名", "anonymous"].includes(String(username || "").toLowerCase());
  const profileAttrs = canOpenProfile
    ? ` role="button" tabindex="0" data-open-user-profile="${sanitize(String(userId))}" title="查看 ${sanitize(username)} 的個人主頁"`
    : "";
  return `
    <span class="identity-with-avatar ${sanitize(extraClass)}${canOpenProfile ? " identity-profile-link" : ""}"${profileAttrs}>
      ${userAvatarMarkup(userId, username, "", avatarFileId)}
      <span class="identity-text">
        <strong>${sanitize(username || "系統")}</strong>
        ${meta ? `<small>${sanitize(meta)}</small>` : ""}
      </span>
    </span>
  `;
}

function bindAvatarFallbacks(root = document) {
  root.querySelectorAll("img.user-avatar-img:not([data-avatar-bound])").forEach((img) => {
    img.dataset.avatarBound = "1";
    img.addEventListener("error", () => {
      img.style.display = "none";
    });
  });
}

function markUserAvatarUpdated(userId, avatarFileId = null) {
  if (!userId) return;
  avatarCacheBustByUserId.set(String(userId), Date.now());
  if (String(userId) === String(currentUserId || "") && avatarFileId !== null && avatarFileId !== undefined) {
    currentUserAvatarFileId = avatarFileId || "";
  }
  updateSidebarIdentity();
}

function readCookie(name) {
  const cookie = document.cookie || "";
  const prefix = `${name}=`;
  const item = cookie.split('; ').find((v) => v.startsWith(prefix));
  return item ? decodeURIComponent(item.substring(prefix.length)) : "";
}

function formatInactivityTimeoutLabel() {
  const totalSeconds = Math.max(1, Math.round((inactivityLogoutMs || DEFAULT_INACTIVITY_LOGOUT_MS) / 1000));
  if (totalSeconds % 60 === 0) return `${totalSeconds / 60} 分鐘`;
  if (totalSeconds > 60) return `${Math.floor(totalSeconds / 60)} 分 ${totalSeconds % 60} 秒`;
  return `${totalSeconds} 秒`;
}

function isInternalTestLoginMode() {
  return siteConfig && ["test", "internal_test"].includes(siteConfig.server_mode);
}

function updateLoginModeFields() {
  const field = $("li-internal-test-token-field");
  const input = $("li-internal-test-token");
  const showInternalTestToken = isInternalTestLoginMode();
  if (field) field.style.display = showInternalTestToken ? "" : "none";
  if (input) {
    input.disabled = !showInternalTestToken;
    if (!showInternalTestToken) input.value = "";
  }
}

function loginAutofillBlockedForUi() {
  if (siteConfig && typeof siteConfig === "object" && siteConfig.login_autofill_block_enabled === true) return true;
  const authCard = $("auth-card");
  return authCard?.dataset?.loginAutofillBlock === "1";
}

function bindLoginAutofillGuards() {
  ["li-user", "li-pw"].forEach((id) => {
    const input = $(id);
    if (!input || input.dataset.autofillGuardBound === "1") return;
    const unlock = () => {
      if (input.dataset.autofillGuardEnabled === "1") input.readOnly = false;
    };
    input.addEventListener("focus", unlock);
    input.addEventListener("pointerdown", unlock);
    input.addEventListener("keydown", unlock);
    input.dataset.autofillGuardBound = "1";
  });
}

function updateLoginAutofillPolicy() {
  bindLoginAutofillGuards();
  const enabled = loginAutofillBlockedForUi();
  const authCard = $("auth-card");
  if (authCard) authCard.dataset.loginAutofillBlock = enabled ? "1" : "0";
  const loginSection = $("sec-login");
  const dummyId = "li-autofill-decoys";
  const existingDummy = $(dummyId);
  if (loginSection && enabled && !existingDummy) {
    const decoys = document.createElement("div");
    decoys.id = dummyId;
    decoys.style.display = "none";
    decoys.setAttribute("aria-hidden", "true");
    decoys.innerHTML = '<input type="text" autocomplete="username"><input type="password" autocomplete="current-password">';
    loginSection.insertBefore(decoys, loginSection.firstChild);
  } else if (!enabled && existingDummy) {
    existingDummy.remove();
  }
  [
    { id: "li-user", normalAutocomplete: "username" },
    { id: "li-pw", normalAutocomplete: "current-password" },
  ].forEach(({ id, normalAutocomplete }) => {
    const input = $(id);
    if (!input) return;
    input.autocomplete = enabled ? "off" : normalAutocomplete;
    input.setAttribute("data-lpignore", enabled ? "true" : "false");
    input.dataset.formType = enabled ? "other" : normalAutocomplete;
    input.dataset.autofillGuardEnabled = enabled ? "1" : "0";
    input.readOnly = enabled;
  });
}

function extractSiteAppearanceConfig(config) {
  const out = {};
  if (!config || typeof config !== "object") return out;
  SITE_APPEARANCE_KEYS.forEach((key) => {
    const value = config[key];
    if (value !== undefined && value !== null && value !== "") out[key] = value;
  });
  return out;
}

function normalizeSiteThemeMode(value, fallback = "dark") {
  const raw = String(value || "").trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(SITE_THEME_MODE_PALETTES, raw) ? raw : fallback;
}

function getUserAppearanceConfig() {
  return { ...userSiteAppearanceConfig };
}

function getEffectiveSiteThemeMode() {
  const globalMode = normalizeSiteThemeMode(globalSiteConfig.site_theme_mode, "dark");
  return normalizeSiteThemeMode(userSiteAppearanceConfig.site_theme_mode, globalMode);
}

function renderEffectiveSiteConfig() {
  const themeMode = getEffectiveSiteThemeMode();
  siteConfig = {
    ...globalSiteConfig,
    ...(SITE_THEME_MODE_PALETTES[themeMode] || {}),
    ...userSiteAppearanceConfig,
    site_theme_mode: themeMode,
  };
  updateLoginModeFields();
  updateLoginAutofillPolicy();
  updatePasswordPolicyHints();
  const root = document.documentElement;
  const mappings = {
    site_bg: "--bg",
    site_surface: "--surface",
    site_accent: "--accent",
    site_accent2: "--accent2",
    site_text: "--text",
    site_muted: "--muted",
  };
  Object.entries(mappings).forEach(([key, cssVar]) => {
    const value = siteConfig[key];
    if (typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value)) {
      root.style.setProperty(cssVar, value);
    }
  });
  const radius = Number(siteConfig.site_radius_px || 12);
  root.style.setProperty("--radius", `${Math.max(4, Math.min(32, Number.isFinite(radius) ? radius : 12))}px`);
  const fontScale = Number(siteConfig.site_font_scale || 1);
  root.style.setProperty("--font-scale", String(Math.max(0.85, Math.min(1.3, Number.isFinite(fontScale) ? fontScale : 1))));
  const contentWidth = Number(siteConfig.site_content_width || 1380);
  root.style.setProperty("--content-max-width", `${Math.max(980, Math.min(1800, Number.isFinite(contentWidth) ? contentWidth : 1380))}px`);
  const fontFamilyKey = typeof siteConfig.site_font_family === "string" ? siteConfig.site_font_family : "system";
  root.style.setProperty("--ui-font-family", SITE_FONT_FAMILY_MAP[fontFamilyKey] || SITE_FONT_FAMILY_MAP.system);
  const sidebarWidthKey = typeof siteConfig.site_sidebar_width === "string" ? siteConfig.site_sidebar_width : "standard";
  const sidebarWidths = SITE_SIDEBAR_WIDTH_MAP[sidebarWidthKey] || SITE_SIDEBAR_WIDTH_MAP.standard;
  root.style.setProperty("--sidebar-width", `${sidebarWidths.expanded}px`);
  root.style.setProperty("--sidebar-width-collapsed", `${sidebarWidths.collapsed}px`);
  const layoutMode = typeof siteConfig.site_layout_mode === "string" ? siteConfig.site_layout_mode : "centered";
  const density = typeof siteConfig.site_density === "string" ? siteConfig.site_density : "comfortable";
  const backgroundStyle = typeof siteConfig.site_background_style === "string" ? siteConfig.site_background_style : "flat";
  const panelStyle = typeof siteConfig.site_panel_style === "string" ? siteConfig.site_panel_style : "glass";
  document.body.dataset.themeMode = themeMode;
  document.body.dataset.layoutMode = layoutMode;
  document.body.dataset.density = density;
  document.body.dataset.backgroundStyle = backgroundStyle;
  document.body.dataset.panelStyle = panelStyle;
  document.body.dataset.sidebarWidth = sidebarWidthKey;
  if (typeof updateThemeQuickToggleUi === "function") updateThemeQuickToggleUi();
  if (typeof updateRecoveryModeUi === "function") updateRecoveryModeUi();
}

function updatePasswordPolicyHints() {
  const enabled = !siteConfig || siteConfig.password_strength_policy_enabled !== false;
  const rules = $("reg-pw-rules");
  if (rules) {
    rules.style.display = enabled ? "" : "none";
  }
  const hint = $("reg-pw-hint");
  if (hint && !enabled) {
    hint.textContent = "";
  }
}

function applySiteConfig(config, options = {}) {
  if (!config || typeof config !== "object") return;
  const scope = options && typeof options.scope === "string" ? options.scope : "global";
  if (scope === "user") {
    userSiteAppearanceConfig = extractSiteAppearanceConfig(config);
  } else {
    globalSiteConfig = { ...globalSiteConfig, ...config };
  }
  renderEffectiveSiteConfig();
}

function configRefreshSeconds(key, fallback, min = 1, max = 300) {
  const seconds = Number(siteConfig?.[key] ?? fallback);
  const safe = Number.isFinite(seconds) ? seconds : fallback;
  return Math.max(min, Math.min(max, safe));
}

function chatPollMs() {
  return Math.round(configRefreshSeconds("chat_poll_seconds", CHAT_POLL_DEFAULT_MS / 1000, 1, 60) * 1000);
}

function serverConnectionMonitorMs() {
  return Math.round(configRefreshSeconds("server_connection_monitor_seconds", SERVER_CONNECTION_MONITOR_DEFAULT_MS / 1000, 5, 300) * 1000);
}

function clearUserAppearanceConfig() {
  userSiteAppearanceConfig = {};
  renderEffectiveSiteConfig();
}

function renderServerVersion(meta) {
  if (!meta || typeof meta !== "object") return;
  serverMeta = { ...serverMeta, ...meta };
  const releaseId = typeof serverMeta.release_id === "string" && serverMeta.release_id
    ? serverMeta.release_id
    : (typeof serverMeta.version === "string" && serverMeta.version ? serverMeta.version : "unknown");
  const startedAt = typeof serverMeta.started_at === "string" && serverMeta.started_at ? formatChatTime(serverMeta.started_at) : "";
  const text = startedAt ? `發佈號: ${releaseId} · 啟動 ${startedAt}` : `發佈號: ${releaseId}`;
  document.querySelectorAll("[data-server-version-badge]").forEach((el) => {
    el.textContent = text;
  });
  const sidebarVersion = $("sidebar-server-version");
  if (sidebarVersion) sidebarVersion.textContent = `發佈號: ${releaseId}`;
}

async function loadSiteConfig() {
  try {
    const res = await fetch(API + "/site-config", { credentials: "same-origin" });
    const json = await res.json().catch(() => ({}));
    if (json && json.ok && json.site_config) {
      applySiteConfig(json.site_config);
    }
    if (json && json.ok && json.server_meta) {
      renderServerVersion(json.server_meta);
    }
  } catch (err) {
    console.error("site config load failed", err);
  }
}

function setServerConnectionState(state, label) {
  const dots = [$("sidebar-server-dot"), $("auth-server-dot")].filter(Boolean);
  const labels = [$("sidebar-server-label"), $("auth-server-label")].filter(Boolean);
  const containers = [$("sidebar-server-dot")?.closest(".sidebar-server-state"), $("auth-server-dot")?.closest(".auth-server-status")].filter(Boolean);
  if (!dots.length || !labels.length) return;
  const colors = {
    online: ["#4caf50", "rgba(76,175,80,.75)"],
    unstable: ["#ffb74d", "rgba(255,183,77,.75)"],
    offline: ["#ff4f6d", "rgba(255,79,109,.75)"],
  };
  const isHealthy = state === "online";
  const [color, glow] = colors[state] || colors.unstable;
  dots.forEach((dot) => {
    dot.style.background = color;
    dot.style.boxShadow = `0 0 10px ${glow}`;
    dot.title = label;
    dot.setAttribute("aria-label", label);
  });
  containers.forEach((node) => {
    node.title = label;
    node.setAttribute("aria-label", label);
  });
  labels.forEach((text) => {
    text.hidden = isHealthy;
    text.textContent = isHealthy ? "" : label;
    text.title = label;
    text.setAttribute("aria-label", label);
  });
}

const SERVER_CONNECTION_UNSTABLE_FAILURE_COUNT = 2;
const SERVER_CONNECTION_OFFLINE_FAILURE_COUNT = 3;
const SERVER_CONNECTION_UNSTABLE_LATENCY_MS = 2500;
const SERVER_CONNECTION_UNSTABLE_SLOW_STREAK = 2;

async function checkServerConnection() {
  const started = Date.now();
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), 3500);
  try {
    const res = await fetch(API + "/version", {
      credentials: "same-origin",
      cache: "no-store",
      signal: ctrl.signal
    });
    clearTimeout(timeout);
    const latency = Date.now() - started;
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error("bad status");
    serverConnectionFailures = 0;
    if (json.maintenance_mode) {
      serverConnectionSlowStreak = 0;
      setServerConnectionState("unstable", "維護模式");
      return;
    }
    if (latency > SERVER_CONNECTION_UNSTABLE_LATENCY_MS) {
      serverConnectionSlowStreak += 1;
      if (serverConnectionSlowStreak >= SERVER_CONNECTION_UNSTABLE_SLOW_STREAK) {
        setServerConnectionState("unstable", `連線偏慢 ${latency}ms`);
      } else {
        setServerConnectionState("online", "伺服器正常");
      }
      return;
    }
    serverConnectionSlowStreak = 0;
    setServerConnectionState("online", "伺服器正常");
  } catch (_) {
    clearTimeout(timeout);
    serverConnectionSlowStreak = 0;
    serverConnectionFailures += 1;
    if (serverConnectionFailures >= SERVER_CONNECTION_OFFLINE_FAILURE_COUNT) {
      setServerConnectionState("offline", "伺服器離線");
    } else if (serverConnectionFailures >= SERVER_CONNECTION_UNSTABLE_FAILURE_COUNT) {
      setServerConnectionState("unstable", "連線不穩");
    }
  }
}

function startServerConnectionMonitor() {
  if (serverConnectionTimer) clearInterval(serverConnectionTimer);
  checkServerConnection();
  serverConnectionTimer = setInterval(checkServerConnection, serverConnectionMonitorMs());
}

function getCsrfToken() { return _csrfToken; }

function setCsrfToken(token) {
  _csrfToken = token || null;
  try {
    if (_csrfToken) localStorage.setItem(CSRF_STORAGE_KEY, _csrfToken);
    else localStorage.removeItem(CSRF_STORAGE_KEY);
  } catch (_) {}
  try {
    if (!csrfBroadcast && "BroadcastChannel" in window) csrfBroadcast = new BroadcastChannel(CSRF_BROADCAST_CHANNEL);
    if (csrfBroadcast) csrfBroadcast.postMessage({ type: "csrf-token", token: _csrfToken });
  } catch (_) {}
  return _csrfToken;
}

try {
  const storedCsrfToken = localStorage.getItem(CSRF_STORAGE_KEY);
  if (storedCsrfToken) _csrfToken = storedCsrfToken;
} catch (_) {}

try {
  if ("BroadcastChannel" in window) {
    csrfBroadcast = new BroadcastChannel(CSRF_BROADCAST_CHANNEL);
    csrfBroadcast.onmessage = (event) => {
      const data = event?.data || {};
      if (data.type === "csrf-token") _csrfToken = data.token || null;
    };
  }
} catch (_) {}

window.addEventListener("storage", (event) => {
  if (event.key === CSRF_STORAGE_KEY) _csrfToken = event.newValue || null;
});

let _csrfTokenRequest = null;

function loadHackmeScriptOnce(src) {
  const target = String(src || "").trim();
  if (!target) return Promise.reject(new Error("missing script src"));
  if (lazyScriptPromises.has(target)) return lazyScriptPromises.get(target);
  const existing = Array.from(document.scripts || []).find((script) => script.getAttribute("src") === target);
  if (existing?.dataset.loaded === "1") return Promise.resolve(existing);
  const promise = new Promise((resolve, reject) => {
    const script = existing || document.createElement("script");
    script.src = target;
    script.defer = true;
    script.addEventListener("load", () => {
      script.dataset.loaded = "1";
      resolve(script);
    }, { once: true });
    script.addEventListener("error", () => reject(new Error(`failed to load ${target}`)), { once: true });
    if (!existing) document.head.appendChild(script);
  }).catch((err) => {
    lazyScriptPromises.delete(target);
    throw err;
  });
  lazyScriptPromises.set(target, promise);
  return promise;
}

async function ensureThreeJsLoaded() {
  if (window.THREE) return window.THREE;
  await loadHackmeScriptOnce(THREE_JS_SRC);
  return window.THREE || null;
}

function markIdleTimeoutLogoutPending() {
  try { localStorage.setItem(IDLE_TIMEOUT_LOGOUT_STORAGE_KEY, String(Date.now())); } catch (_) {}
}

function clearIdleTimeoutLogoutPending() {
  try { localStorage.removeItem(IDLE_TIMEOUT_LOGOUT_STORAGE_KEY); } catch (_) {}
}

function hasIdleTimeoutLogoutPending() {
  try { return Boolean(localStorage.getItem(IDLE_TIMEOUT_LOGOUT_STORAGE_KEY)); } catch (_) { return false; }
}

async function fetchCsrfToken({ force = false } = {}) {
  const cookieToken = readCookie("csrf_token");
  if (!force && (_csrfToken || cookieToken)) {
    setCsrfToken(cookieToken || _csrfToken || null);
    return _csrfToken;
  }
  if (_csrfTokenRequest) {
    await _csrfTokenRequest;
    return _csrfToken;
  }
  _csrfTokenRequest = (async () => {
    try {
      const res = await fetch(API + '/csrf-token', { credentials: 'same-origin' });
      const json = await res.json().catch(() => ({}));
      if (json && json.ok && typeof json.csrf_token === "string" && json.csrf_token) {
        setCsrfToken(json.csrf_token);
        return;
      }
    } catch (_) {}
    const latestCookieToken = readCookie("csrf_token");
    setCsrfToken(latestCookieToken || null);
  })();
  try {
    await _csrfTokenRequest;
  } finally {
    _csrfTokenRequest = null;
  }
  return _csrfToken;
}

function isStateChangingMethod(method) {
  return ["POST", "PUT", "PATCH", "DELETE"].includes(String(method || "GET").toUpperCase());
}

async function apiFetch(url, options = {}, retryOnCsrf = true) {
  const opts = { ...options };
  opts.credentials = opts.credentials || "same-origin";
  const method = String(opts.method || "GET").toUpperCase();
  const headers = new Headers(opts.headers || {});
  if (isStateChangingMethod(method) && !headers.has("X-CSRF-Token")) {
    headers.set("X-CSRF-Token", await fetchCsrfToken());
  }
  opts.headers = headers;
  const response = await fetch(url, opts);
  const latestCookieToken = readCookie("csrf_token");
  if (latestCookieToken) setCsrfToken(latestCookieToken);
  if (response.status !== 403 || !retryOnCsrf) {
    await notifyServerBusyResponse(response);
    return response;
  }
  const payload = await response.clone().json().catch(() => ({}));
  if (!payload || payload.error !== "csrf_invalid") {
    await notifyServerBusyResponse(response, payload);
    return response;
  }
  const refreshed = await fetchCsrfToken({ force: true });
  if (!refreshed) return response;
  const retryHeaders = new Headers(options.headers || {});
  if (isStateChangingMethod(method)) retryHeaders.set("X-CSRF-Token", refreshed);
  const retried = await apiFetch(url, { ...options, credentials: opts.credentials, headers: retryHeaders }, false);
  const retryCookieToken = readCookie("csrf_token");
  if (retryCookieToken) setCsrfToken(retryCookieToken);
  return retried;
}

async function notifyServerBusyResponse(response, payload = null) {
  if (!response || response.status !== 503) return;
  let body = payload;
  if (!body) body = await response.clone().json().catch(() => ({}));
  const isServerBusy = response.headers?.get?.("X-Hackme-Backpressure") || body?.error === "server_busy";
  if (!isServerBusy) return;
  const retryAfter = Number(body.retry_after_seconds || response.headers?.get?.("Retry-After") || 0);
  const suffix = retryAfter > 0 ? `約 ${retryAfter} 秒後再試。` : "請稍候再試。";
  const message = body.user_message || body.message || body.msg || `${SERVER_BUSY_USER_MESSAGE}${suffix}`;
  const now = Date.now();
  if (now - lastServerBusyToastAt < 6000) return;
  lastServerBusyToastAt = now;
  showAppToast(String(message || SERVER_BUSY_USER_MESSAGE), false, { duration: 5200 });
}

function flash(el, text, ok, options = {}) {
  if (!el) return;
  el.textContent = text || "";
  el.className = text ? "msg show " + (ok ? "ok" : "err") : "msg";
  el.setAttribute("role", ok ? "status" : "alert");
  el.setAttribute("aria-live", ok ? "polite" : "assertive");
  el.setAttribute("aria-atomic", "true");
  scheduleInlineMessageClear(el, text, ok, options);
}

function uiPrefersReducedMotion() {
  return Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
}

function showAppToast(text, ok = true, options = {}) {
  const host = $("toast-host");
  const message = String(text || "").replace(/\s+/g, " ").trim().slice(0, 180);
  if (!host || !message) return;
  const kind = ok === true ? "ok" : ok === false ? "err" : "info";
  const signature = `${kind}:${message}`;
  const now = Date.now();
  if (signature === lastAppToastSignature && now - lastAppToastAt < 1200) return;
  lastAppToastSignature = signature;
  lastAppToastAt = now;
  while (host.children.length >= APP_TOAST_LIMIT) host.firstElementChild?.remove();
  const toast = document.createElement("div");
  toast.className = `toast toast-${kind}`;
  toast.setAttribute("role", kind === "err" ? "alert" : "status");
  toast.textContent = message;
  host.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("show"));
  const duration = Number(options.duration || TOAST_DURATION_MS[kind] || TOAST_DURATION_MS.ok);
  window.setTimeout(() => {
    toast.classList.remove("show");
    toast.addEventListener("transitionend", () => toast.remove(), { once: true });
    window.setTimeout(() => toast.remove(), 420);
  }, duration);
}

function closeAppDialog(result = null) {
  const overlay = $("app-dialog-overlay");
  if (overlay) {
    overlay.classList.remove("show");
    overlay.setAttribute("aria-hidden", "true");
  }
  document.body?.classList?.remove("modal-open");
  const resolve = appDialogResolve;
  appDialogResolve = null;
  if (typeof resolve === "function") resolve(result);
  if (appDialogPreviousFocus && typeof appDialogPreviousFocus.focus === "function") {
    appDialogPreviousFocus.focus();
  }
  appDialogPreviousFocus = null;
}

function ensureAppDialogOverlay() {
  let overlay = $("app-dialog-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "app-dialog-overlay";
  overlay.className = "app-dialog-overlay";
  overlay.setAttribute("aria-hidden", "true");
  overlay.innerHTML = `
    <form class="app-dialog-card" id="app-dialog-card" role="dialog" aria-modal="true" aria-labelledby="app-dialog-title">
      <div class="mini-title" id="app-dialog-title"></div>
      <div class="drive-card-sub app-dialog-message" id="app-dialog-message"></div>
      <div class="field app-dialog-input-wrap" id="app-dialog-input-wrap" hidden></div>
      <div class="msg app-dialog-msg" id="app-dialog-msg"></div>
      <div class="app-dialog-actions">
        <button class="btn" type="button" id="app-dialog-cancel">取消</button>
        <button class="btn btn-primary" type="submit" id="app-dialog-confirm">確認</button>
      </div>
    </form>
  `;
  document.body.appendChild(overlay);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeAppDialog(null);
  });
  overlay.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeAppDialog(null);
    }
  });
  $("app-dialog-cancel")?.addEventListener("click", () => closeAppDialog(null));
  $("app-dialog-card")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("app-dialog-input");
    const msg = $("app-dialog-msg");
    if (input) {
      const value = String(input.value || "");
      const required = input.dataset.required === "1";
      const maxLength = Number(input.getAttribute("maxlength") || 0);
      if (required && !value.trim()) {
        if (msg) {
          msg.textContent = "請填寫內容";
          msg.className = "msg app-dialog-msg show err";
        }
        input.focus();
        return;
      }
      if (maxLength > 0 && value.length > maxLength) {
        if (msg) {
          msg.textContent = `請控制在 ${maxLength} 字以內`;
          msg.className = "msg app-dialog-msg show err";
        }
        input.focus();
        return;
      }
      closeAppDialog(value);
      return;
    }
    closeAppDialog(true);
  });
  return overlay;
}

function showAppDialog(options = {}) {
  const overlay = ensureAppDialogOverlay();
  if (!overlay) return Promise.resolve(null);
  if (appDialogResolve) closeAppDialog(null);
  appDialogPreviousFocus = document.activeElement;
  const title = $("app-dialog-title");
  const message = $("app-dialog-message");
  const inputWrap = $("app-dialog-input-wrap");
  const msg = $("app-dialog-msg");
  const confirm = $("app-dialog-confirm");
  const cancel = $("app-dialog-cancel");
  if (title) title.textContent = options.title || "確認操作";
  if (message) message.textContent = options.message || "";
  if (msg) {
    msg.textContent = "";
    msg.className = "msg app-dialog-msg";
  }
  if (confirm) {
    confirm.textContent = options.confirmLabel || "確認";
    confirm.classList.toggle("btn-danger", Boolean(options.danger));
    confirm.classList.toggle("btn-primary", !options.danger);
  }
  if (cancel) cancel.textContent = options.cancelLabel || "取消";
  if (inputWrap) {
    inputWrap.hidden = !options.prompt;
    inputWrap.innerHTML = "";
    if (options.prompt) {
      const label = document.createElement("label");
      label.setAttribute("for", "app-dialog-input");
      label.textContent = options.inputLabel || "內容";
      const input = document.createElement(options.multiline ? "textarea" : "input");
      input.id = "app-dialog-input";
      input.name = "app-dialog-input";
      if (!options.multiline) input.type = "text";
      input.value = String(options.defaultValue || "");
      input.placeholder = options.placeholder || "";
      input.dataset.required = options.required ? "1" : "0";
      if (Number(options.maxLength || 0) > 0) input.setAttribute("maxlength", String(Number(options.maxLength)));
      if (options.readOnly) input.readOnly = true;
      if (options.multiline) input.rows = Math.max(3, Number(options.rows || 4));
      inputWrap.append(label, input);
    }
  }
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
  document.body?.classList?.add("modal-open");
  const focusTarget = $("app-dialog-input") || confirm || cancel;
  window.setTimeout(() => focusTarget?.focus?.(), 0);
  return new Promise((resolve) => {
    appDialogResolve = resolve;
  });
}

function showAppConfirm(message, options = {}) {
  return showAppDialog({
    title: options.title || "確認操作",
    message,
    confirmLabel: options.confirmLabel || "確認",
    cancelLabel: options.cancelLabel || "取消",
    danger: Boolean(options.danger),
  }).then((result) => result === true);
}

function showAppPrompt(message, options = {}) {
  return showAppDialog({
    ...options,
    title: options.title || "需要輸入",
    message,
    prompt: true,
  });
}

function showCopyFallbackDialog(text, title = "複製內容") {
  return showAppPrompt("瀏覽器阻擋自動複製，請手動複製下方內容。", {
    title,
    inputLabel: "完整內容",
    defaultValue: text || "",
    multiline: true,
    rows: 4,
    readOnly: true,
    confirmLabel: "完成",
    cancelLabel: "關閉",
  });
}

function messageKind(ok) {
  return ok === false ? "err" : ok === null ? "info" : "ok";
}

function feedbackDuration(ok, options = {}, durations = ACTION_FEEDBACK_DURATION_MS) {
  const explicit = Number(options.duration || 0);
  if (explicit > 0) return explicit;
  const kind = messageKind(ok);
  return Number(durations[kind] || durations.ok || 1800);
}

function scheduleInlineMessageClear(el, text, ok = true, options = {}) {
  const message = String(text || "");
  if (!el || !message || options.persistent) return;
  if (el._inlineMessageClearTimer) clearTimeout(el._inlineMessageClearTimer);
  el._inlineMessageClearTimer = window.setTimeout(() => {
    if (String(el.textContent || "") !== message) return;
    el.textContent = "";
    el.classList?.remove("show", "ok", "err", "info");
    if (el.style) el.style.color = "";
    el._inlineMessageClearTimer = null;
  }, feedbackDuration(ok, options, INLINE_MESSAGE_DURATION_MS));
}

function announceInlineMessage(text, ok) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized || /載入中|讀取中|同步中|處理中|準備中/.test(normalized)) return;
  showAppToast(normalized, ok);
}

function actionFeedbackAnchor(trigger) {
  if (!trigger || typeof trigger.closest !== "function") return null;
  return trigger.closest(".field, .admin-toolbar, .drive-card-heading, .economy-wallet-grid > div, .drive-file-row, .edit-user-actions, .auth-recovery-actions, .game-action-toolbar")
    || trigger.parentElement;
}

function showActionFeedback(trigger, text, ok = true, options = {}) {
  const button = trigger?.closest?.(".btn, input[type='button'], input[type='submit']");
  const message = String(text || "").replace(/\s+/g, " ").trim();
  if (!button || !message) return;
  const anchor = actionFeedbackAnchor(button);
  if (!anchor) return;
  let box = anchor.querySelector(":scope > .action-feedback");
  if (!box) {
    box = document.createElement("div");
    anchor.appendChild(box);
  }
  const kind = messageKind(ok);
  box.textContent = message;
  box.className = `msg action-feedback show ${kind}`;
  if (!options.skipToast) announceInlineMessage(message, ok);
  if (box._actionFeedbackTimer) clearTimeout(box._actionFeedbackTimer);
  if (!options.persistent) {
    box._actionFeedbackTimer = window.setTimeout(() => {
      box.classList.remove("show");
      box._actionFeedbackTimer = null;
    }, feedbackDuration(ok, options, ACTION_FEEDBACK_DURATION_MS));
  }
}

function showCopyLinkFeedback(trigger, text = "已完成複製", ok = true) {
  const button = trigger?.closest?.(".btn, button, input[type='button'], input[type='submit']");
  const message = String(text || "").replace(/\s+/g, " ").trim();
  if (!button || !message) return;
  let box = button.nextElementSibling;
  if (!box || !box.classList?.contains("copy-link-feedback")) {
    box = document.createElement("div");
    button.insertAdjacentElement("afterend", box);
  }
  box.textContent = message;
  box.className = `copy-link-feedback ${ok ? "ok" : "err"} show`;
  if (box._copyLinkFeedbackTimer) clearTimeout(box._copyLinkFeedbackTimer);
  box._copyLinkFeedbackTimer = window.setTimeout(() => {
    box.classList.remove("show");
    box._copyLinkFeedbackTimer = null;
  }, feedbackDuration(ok, {}, INLINE_MESSAGE_DURATION_MS));
}

function animateActiveModule(tab) {
  const section = $("module-" + tab);
  if (!section || uiPrefersReducedMotion()) return;
  section.classList.remove("ui-module-enter");
  void section.offsetWidth;
  section.classList.add("ui-module-enter");
  window.setTimeout(() => section.classList.remove("ui-module-enter"), 460);
}

function installUiInteractionFeedback() {
  if (document.documentElement.dataset.uiFeedbackBound === "1") return;
  document.documentElement.dataset.uiFeedbackBound = "1";
  document.addEventListener("pointerdown", (event) => {
    const target = event.target?.closest?.(".btn, .tab, .icon-action-btn, .game-catalog-card, .drive-file-row, .community-thread-item, .video-card");
    if (!target || target.disabled || target.getAttribute("aria-disabled") === "true") return;
    const rect = target.getBoundingClientRect();
    target.style.setProperty("--ui-press-x", `${event.clientX - rect.left}px`);
    target.style.setProperty("--ui-press-y", `${event.clientY - rect.top}px`);
    target.classList.remove("ui-pressed");
    void target.offsetWidth;
    target.classList.add("ui-pressed");
    window.setTimeout(() => target.classList.remove("ui-pressed"), 520);
  }, { passive: true });
}

function clearMsg() {
  $("li-msg").className = "msg";
  $("reg-msg").className = "msg";
}

function setUserEditMsg(text, ok) {
  const el = $("user-edit-msg");
  if (!el) return;
  el.textContent = text;
  el.className = ok === true ? "msg show ok" : ok === false ? "msg show err" : "msg";
}

function setChatMsg(elId, text, ok) {
  const el = $(elId);
  if (!el) return;
  el.textContent = text || "";
  el.className = text ? "msg show " + (ok ? "ok" : "err") : "msg";
  el.setAttribute("role", ok ? "status" : "alert");
  el.setAttribute("aria-live", ok ? "polite" : "assertive");
  el.setAttribute("aria-atomic", "true");
  scheduleInlineMessageClear(el, text, ok);
}

function stopChatPoll() {
  if (chatPollTimer) {
    clearInterval(chatPollTimer);
    chatPollTimer = null;
  }
}

function shouldRunChatPoll() {
  return Boolean(currentUser && selectedChatRoomId && currentModuleTab === "chat" && !document.hidden);
}

function startChatPoll() {
  stopChatPoll();
  if (!shouldRunChatPoll()) return;
  chatPollTimer = setInterval(() => {
    if (!shouldRunChatPoll()) {
      stopChatPoll();
      return;
    }
    loadChatMessages(selectedChatRoomId, true);
  }, chatPollMs());
}

function syncChatPollLifecycle() {
  if (shouldRunChatPoll()) startChatPoll();
  else stopChatPoll();
}

document.addEventListener("hackme:module-changed", syncChatPollLifecycle);
document.addEventListener("visibilitychange", syncChatPollLifecycle);

function formatChatTime(ts) {
  if (!ts) return "";
  let input = String(ts || "").trim();
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(input) && !/[zZ]|[+-]\d{2}:\d{2}$/.test(input)) {
    input += "Z";
  }
  const d = new Date(input);
  if (Number.isNaN(d.getTime())) return ts;
  const displayTimezone = getUserDisplayTimezone();
  if (displayTimezone && displayTimezone !== "auto") {
    try {
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: displayTimezone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).formatToParts(d).reduce((acc, part) => {
        acc[part.type] = part.value;
        return acc;
      }, {});
      return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
    } catch (err) {
      setUserDisplayTimezone("auto");
    }
  }
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd} ${hh}:${mi}`;
}

function normalizeUserDisplayTimezone(value) {
  const text = String(value || "auto").trim();
  return text || "auto";
}

function setUserDisplayTimezone(value) {
  const timezone = normalizeUserDisplayTimezone(value);
  window.hackmeDisplayTimezone = timezone;
  try {
    localStorage.setItem(USER_DISPLAY_TIMEZONE_STORAGE_KEY, timezone);
  } catch (err) {}
  return timezone;
}

function getUserDisplayTimezone() {
  if (window.hackmeDisplayTimezone) return normalizeUserDisplayTimezone(window.hackmeDisplayTimezone);
  try {
    const stored = localStorage.getItem(USER_DISPLAY_TIMEZONE_STORAGE_KEY);
    if (stored) return normalizeUserDisplayTimezone(stored);
  } catch (err) {}
  return "auto";
}

function canRemoveContextAttachment(ref) {
  return !!(ref && (ref.can_remove === true || ref.can_remove === 1 || ref.can_remove === "1" || ref.can_remove === "true"));
}

function renderChatRooms() {
  const wrap = $("chat-room-list");
  if (!wrap) return;
  if (!chatRooms.length) {
    wrap.innerHTML = "<p style=\"color:var(--muted);\">尚未加入任何聊天室</p>";
    return;
  }
  const prevId = selectedChatRoomId;
  wrap.innerHTML = "";
  chatRooms.forEach((r) => {
    const row = document.createElement("div");
    row.className = "chat-room-row";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chat-room-item" + (Number(prevId) === Number(r.id) ? " active" : "");
    const lock = r.is_private ? "🔒 " : "";
    const passwordLabel = r.join_password_required ? " · 密碼" : "";
    const memberCount = r.hide_member_count ? "" : (r.member_count ? ` · ${r.member_count}人` : "");
    const anonymousLabel = r.allow_anonymous && !r.is_private ? " · 可匿名" : "";
    btn.textContent = `${lock}#${r.id} ${r.name}${memberCount}${anonymousLabel}${passwordLabel}`;
    btn.setAttribute("title", `聊天室持有者：${r.owner_username || "未知"}${r.is_private ? " · 私人訊息" : ""}${passwordLabel}`);
    btn.addEventListener("click", () => openChatRoom(r.id, true));
    row.appendChild(btn);
    if (canDeleteChatRoom(r)) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "chat-room-delete-btn";
      del.textContent = "刪除";
      del.setAttribute("title", "刪除此聊天室");
      del.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteChatRoom(r.id);
      });
      row.appendChild(del);
    }
    wrap.appendChild(row);
  });
}

function renderChatMessages(messages) {
  const list = $("chat-room-messages");
  if (!list) return;
  chatMessageCache = Array.isArray(messages) ? messages : [];
  if (!messages.length) {
    list.innerHTML = "<p style=\"color:var(--muted);\">目前還沒有訊息</p>";
    return;
  }
  list.innerHTML = messages.map((m) => {
    const isSelf = !!m.is_self || String(m.sender || "") === String(currentUser || "");
    const cls = ["chat-msg"];
    const actions = [];
    if (isSelf) cls.push("self");
    if (!isSelf && m.id) actions.push(`<button class="chat-report-btn" type="button" data-report-message="${m.id}">檢舉</button>`);
    if (m.can_edit) actions.push(`<button class="chat-edit-btn" type="button" data-edit-message="${m.id}">編輯</button>`);
    if (m.can_recall) actions.push(`<button class="chat-delete-btn" type="button" data-delete-message="${m.id}" data-recall-message="1">收回</button>`);
    else if (canDeleteChatMessage(m)) actions.push(`<button class="chat-delete-btn" type="button" data-delete-message="${m.id}">刪除</button>`);
    const editedLabel = !m.is_revoked && m.edited_at ? `<small class="chat-edited-label">已編輯</small>` : "";
    const body = m.is_revoked
      ? `<div class="chat-revoked">訊息已收回</div>`
      : (m.message_type === "sticker"
        ? `<div class="chat-sticker">${sanitize(chatStickerLabel(m.sticker_key, m.sticker))}</div>`
        : renderChatMessageContent(m.content || ""));
    const attachments = Array.isArray(m.attachments) && m.attachments.length
      ? `<div class="chat-message-attachments">${m.attachments.map((file) => {
          const name = file.original_filename_plain_for_public || file.file_id || "附件";
          const size = typeof formatDriveBytes === "function" ? formatDriveBytes(file.size_bytes || 0) : `${Number(file.size_bytes || 0)} bytes`;
          const warn = typeof driveFileNeedsWarning === "function" ? driveFileNeedsWarning(file) : false;
          const removeButton = canRemoveContextAttachment(file)
            ? `<button class="btn btn-danger chat-sticker-btn" type="button" data-drive-action="delete-context-attachment" data-ref-id="${sanitize(file.ref_id || file.id || "")}" data-context-type="chat_message" data-context-id="${sanitize(m.id || file.context_id || "")}" data-target-id="chat-messages">移除附件</button>`
            : "";
          const imagePreview = typeof driveFileIsImage === "function" && typeof drivePreviewContentUrl === "function" && driveFileIsImage(file)
            ? `<button class="chat-message-image-preview" type="button" data-drive-action="album-full-preview" data-file-id="${sanitize(file.file_id || "")}" data-name="${sanitize(name)}"><img src="${sanitize(drivePreviewContentUrl(file.file_id || ""))}" alt="${sanitize(name)}" loading="lazy" /></button>`
            : "";
          return `
            <div class="chat-message-attachment">
              <span>
                <strong>${sanitize(name)}</strong>
                <small>${sanitize(size)} · scan=${sanitize(file.scan_status || "-")} · risk=${sanitize(file.risk_level || "-")}</small>
                ${imagePreview}
              </span>
              <span class="chat-message-attachment-actions">
                <button class="btn chat-sticker-btn" type="button" data-drive-action="preview" data-file-id="${sanitize(file.file_id || "")}">預覽</button>
                <button class="btn chat-sticker-btn ${warn ? "btn-danger" : "btn-primary"}" type="button" data-drive-action="download" data-file-id="${sanitize(file.file_id || "")}" data-warn="${warn ? "1" : "0"}">下載</button>
                ${removeButton}
              </span>
            </div>
          `;
        }).join("")}</div>`
      : "";
    const senderOfficial = !!m.sender_is_official;
    const senderClass = senderOfficial ? "chat-sender-official" : "";
    const officialBadge = senderOfficial ? `<span class="chat-official-badge">（官方）</span>` : "";
    const originalSender = m.sender_original && String(m.sender_original) !== String(m.sender || "")
      ? `<small class="chat-original-sender">原發言者：${sanitize(m.sender_original)}</small>`
      : "";
    return `
      <div class="${cls.join(" ")}">
        <div class="chat-msg-head">
          ${userAvatarMarkup(m.sender_id, m.sender || "系統", "user-avatar-sm", m.sender_avatar_file_id || "")}
          <span class="meta"><strong class="${senderClass}">${sanitize(m.sender || "系統")}${officialBadge}</strong><small>${sanitize(formatChatTime(m.created_at))}</small>${editedLabel}${originalSender}</span>
        </div>
        ${body}
        ${attachments}
        ${actions.join("")}
      </div>
    `;
  }).join("");
  list.querySelectorAll("button[data-report-message]").forEach((btn) => {
    btn.addEventListener("click", () => reportChatMessage(parseInt(btn.getAttribute("data-report-message"), 10)));
  });
  list.querySelectorAll("button[data-edit-message]").forEach((btn) => {
    btn.addEventListener("click", () => editChatMessage(parseInt(btn.getAttribute("data-edit-message"), 10)));
  });
  list.querySelectorAll("button[data-delete-message]").forEach((btn) => {
    btn.addEventListener("click", () => deleteChatMessage(parseInt(btn.getAttribute("data-delete-message"), 10), btn.getAttribute("data-recall-message") === "1"));
  });
  bindAvatarFallbacks(list);
  list.scrollTop = list.scrollHeight;
}

function hideUserEditDialog() {
  if (forcedPasswordChangeMode) return;
  if (typeof restoreUserAppearancePreviewIfNeeded === "function") {
    restoreUserAppearancePreviewIfNeeded();
  }
  const overlay = $("user-edit-overlay");
  if (overlay) {
    overlay.classList.remove("show");
  }
  if (typeof resetAvatarCropper === "function") resetAvatarCropper();
  editingUserId = null;
  editingUserIsSelf = false;
}

function isBirthdayToday(birthdate) {
  if (typeof birthdate !== "string") return false;
  const normalized = birthdate.trim();
  if (!normalized) return false;

  const m = normalized.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return false;

  const month = Number(m[2]);
  const day = Number(m[3]);
  const today = new Date();
  return today.getMonth() + 1 === month && today.getDate() === day;
}

function setUserEditField(id, value) {
  const el = $(id);
  if (!el) return;
  el.value = value || "";
}

function setLoading(btnId, spinnerId, on) {
  const btn = $(btnId);
  const sp  = $(spinnerId);
  if (!btn || !sp) return;
  btn.classList.toggle("loading", on);
  sp.style.display = on ? "block" : "none";
}

function showTab(tab) {
  $("sec-login").classList.toggle("active",    tab === "login");
  $("sec-register").classList.toggle("active", tab === "register");
  $("tab-login").classList.toggle("active",    tab === "login");
  $("tab-register").classList.toggle("active",tab === "register");
  clearMsg();
}

function setupPwToggle(inputId, btnId) {
  const input = $(inputId);
  const btn   = $(btnId);
  if (!input || !btn) return;
  btn.addEventListener("mousedown", () => {
    input.type = "text";
    btn.textContent = "🙈";
  });
  btn.addEventListener("mouseup", () => {
    input.type = "password";
    btn.textContent = "👁";
  });
  btn.addEventListener("mouseleave", () => {
    input.type = "password";
    btn.textContent = "👁";
  });
  btn.addEventListener("touchstart", (e) => {
    e.preventDefault();
    input.type = "text";
    btn.textContent = "🙈";
  }, { passive: false });
  btn.addEventListener("touchend", (e) => {
    e.preventDefault();
    input.type = "password";
    btn.textContent = "👁";
  }, { passive: false });
}

setupPwToggle("li-pw", "li-pw-toggle");
setupPwToggle("reg-pw", "reg-pw-toggle");
setupPwToggle("reg-pw-confirm", "reg-pw-confirm-toggle");
setupPwToggle("admin-add-pw", "admin-add-pw-toggle");
setupPwToggle("admin-add-pw-confirm", "admin-add-pw-confirm-toggle");
setupPwToggle("edit-user-current-pw", "edit-user-current-pw-toggle");
setupPwToggle("edit-user-pw", "edit-user-pw-toggle");
setupPwToggle("edit-user-pw-confirm", "edit-user-pw-confirm-toggle");
installUiInteractionFeedback();

$("reg-pw").addEventListener("input", function () {
  if (siteConfig && siteConfig.password_strength_policy_enabled === false) {
    $("reg-pw-hint").textContent = "";
    return;
  }
  const v = this.value;
  const hints = [];
  if (v.length < 8)                      hints.push("至少 8 字");
  if (!/[A-Z]/.test(v))                 hints.push("需大寫");
  if (!/[a-z]/.test(v))                 hints.push("需小寫");
  if (!/[!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?]/.test(v)) hints.push("需符號");
  $("reg-pw-hint").textContent = hints.length ? "❌ " + hints.join(" · ") : "✓ 符合強度要求";
});
function updateRegPwMatchHint() {
  const pw = $("reg-pw").value;
  const confirmPw = $("reg-pw-confirm").value;
  const hintEl = $("reg-pw-confirm-hint");
  if (!hintEl) return;
  if (!confirmPw) {
    hintEl.textContent = "";
    return;
  }
  if (pw !== confirmPw) {
    hintEl.textContent = "❌ 兩次輸入的密碼不一致";
    return;
  }
  hintEl.textContent = "✓ 密碼一致";
}
$("reg-pw-confirm").addEventListener("input", updateRegPwMatchHint);

function updateAdminPwMatchHint() {
  const pw = $("admin-add-pw").value;
  const confirmPw = $("admin-add-pw-confirm").value;
  const hintEl = $("admin-add-pw-confirm-hint");
  if (!hintEl) return;
  if (!confirmPw) {
    hintEl.textContent = "";
    return;
  }
  if (pw !== confirmPw) {
    hintEl.textContent = "❌ 兩次輸入的密碼不一致";
    return;
  }
  hintEl.textContent = "✓ 密碼一致";
}
$("admin-add-pw-confirm").addEventListener("input", updateAdminPwMatchHint);

function setAuthState(json, showLoginHero = false) {
  const previousAccountScope = getCurrentAccountStorageScope();
  currentUser = json.username || null;
  currentUserId = json.id || null;
  currentUserAvatarFileId = json.avatar_file_id || "";
  currentRole = json.role || "user";
  currentRoleLabel = json.role_label || currentRole || "user";
  currentAuthScope = json.auth_scope || json._auth_scope || "";
  setUserDisplayTimezone(json.display_timezone || "auto");
  setCurrentAllowedFeatures(json.allowed_features || json._allowed_features || []);
  currentMustChangePassword = !!json.must_change_password;
  try {
    localStorage.setItem(AUTH_SESSION_HINT_STORAGE_KEY, "1");
  } catch (err) {}
  const idleMinutes = Number(json.session_idle_timeout_minutes ?? 10);
  inactivityLogoutMs = idleMinutes > 0 ? Math.max(1, idleMinutes) * 60 * 1000 : 0;
  if (inactivityLogoutMs > 0) resetInactivityTimer();
  if (json && json.appearance_settings && typeof json.appearance_settings === "object") {
    applySiteConfig(json.appearance_settings, { scope: "user" });
  } else {
    clearUserAppearanceConfig();
  }
  canManageUsers = currentRole === "super_admin";
  $("auth-card").style.display = "none";
  document.body.classList.add("app-authenticated");
  $("success-screen").classList.add("show");
  const loginHero = $("login-success-hero");
  if (loginHero) {
    loginHero.classList.toggle("show", !!showLoginHero);
    if (showLoginHero) {
      setTimeout(() => loginHero.classList.remove("show"), 2800);
    }
  }
  if ($("me-user")) $("me-user").textContent = sanitize(currentUser || "-");
  if ($("me-role")) $("me-role").textContent = sanitize(json.role_label || currentRole || "-");
  const levelText = json.member_level_label || json.effective_level || json.member_level || "-";
  const levelEl = $("me-level");
  if (levelEl) levelEl.textContent = sanitize(levelText);
  if ($("me-nickname")) $("me-nickname").textContent = sanitize(json.nickname || "-");
  const sidebarLevel = $("sidebar-current-level");
  if (sidebarLevel) {
    sidebarLevel.dataset.memberLevel = levelText;
    sidebarLevel.textContent = sidebarLevel.dataset.memberLevel;
  }
  const sidebarEffective = $("sidebar-effective-level");
  if (sidebarEffective) {
    if (json.special_account) {
      sidebarEffective.dataset.effectiveLevel = "特殊階級";
    } else {
      const base = json.base_level || json.member_level || "-";
      const effective = json.effective_level || base;
      sidebarEffective.dataset.effectiveLevel = base && base !== effective ? `${effective} / ${base}` : effective;
    }
  }
  const sidebarPoints = $("sidebar-points");
  if (sidebarPoints) {
    const score = Number(json.reputation ?? json.trust_score ?? 0);
    sidebarPoints.dataset.points = Number.isFinite(score) ? String(score) : "0";
  }
  const sidebarViolations = $("sidebar-violations");
  if (sidebarViolations) {
    const score = Number(json.violation_score ?? 0);
    sidebarViolations.dataset.violations = Number.isFinite(score) ? String(score) : "0";
  }
  updateSidebarIdentity();
  const selfEditBtn = $("self-edit-btn");
  if (selfEditBtn) selfEditBtn.style.display = currentUser ? "" : "none";
  const welcomeMsg = $("welcome-msg");
  if (welcomeMsg) {
    welcomeMsg.classList.remove("birthday-greeting");
    if (isBirthdayToday(json.birthdate)) {
      const name = json.nickname || currentUser || "";
      const label = name ? `，${name}` : "";
      welcomeMsg.textContent = `🎉 生日快樂${label}！今天也是你的生日！`;
      void welcomeMsg.offsetWidth;
      welcomeMsg.classList.add("birthday-greeting");
    } else {
      welcomeMsg.textContent = "歡迎回來！";
    }
  }
  const adminWrap = $("admin-wrap");
  if (adminWrap) {
    if (currentRole === "manager" || currentRole === "super_admin") {
      adminWrap.classList.add("show");
    } else {
      adminWrap.classList.remove("show");
    }
  }
  const addPanel = $("admin-manager-view");
  if (addPanel) addPanel.style.display = canManageUsers ? "block" : "none";

  // Module access controls
  const tabModuleAccounts = $("tab-module-accounts");
  const tabModuleSystem = $("tab-module-system");
  const tabModuleServer = $("tab-module-server");
  const tabModuleChat = $("tab-module-chat");
  const tabModuleProfile = $("tab-module-profile");
  const tabModuleAnnouncements = $("tab-module-announcements");
  const tabModuleCommunity = $("tab-module-community");
  const tabModuleDrive = $("tab-module-drive");
  const tabModuleAlbums = $("tab-module-albums");
  const tabModuleVideos = $("tab-module-videos");
  const tabModuleGames = $("tab-module-games");
  const tabModuleExperiments = $("tab-module-experiments");
  const tabModuleJobs = $("tab-module-jobs");
  const tabModuleShares = $("tab-module-shares");
  const tabModuleComfyui = $("tab-module-comfyui");
  const tabModuleEconomy = $("tab-module-economy");
  const tabModuleTrading = $("tab-module-trading");
  const tabModuleAppeals = $("tab-module-appeals");
  const appealsTab = $("tab-appeals");
  const reportsTab = $("tab-reports");
  const governanceTab = $("tab-governance");
  const noticesTab = $("tab-notices");
  if (tabModuleAccounts) tabModuleAccounts.style.display = canAccessModule("accounts") ? "" : "none";
  if (tabModuleSystem) tabModuleSystem.style.display = currentUser === "root" ? "" : "none";
  if (tabModuleServer) tabModuleServer.style.display = currentUser === "root" ? "" : "none";
  if (tabModuleChat) tabModuleChat.style.display = canAccessModule("chat") ? "" : "none";
  if (tabModuleProfile) tabModuleProfile.style.display = canAccessModule("profile") ? "" : "none";
  if (tabModuleAnnouncements) tabModuleAnnouncements.style.display = canAccessModule("community") ? "" : "none";
  if (tabModuleCommunity) tabModuleCommunity.style.display = canAccessModule("community") ? "" : "none";
  if (tabModuleDrive) tabModuleDrive.style.display = canAccessModule("privacy_uploads") ? "" : "none";
  if (tabModuleAlbums) tabModuleAlbums.style.display = (canAccessModule("privacy_uploads") && isFeatureEnabledForUi("feature_storage_albums_enabled", false)) ? "" : "none";
  if (tabModuleVideos) tabModuleVideos.style.display = canAccessModule("videos") ? "" : "none";
  if (tabModuleGames) tabModuleGames.style.display = canAccessModule("games") ? "" : "none";
  if (tabModuleExperiments) tabModuleExperiments.style.display = canAccessModule("experiments") ? "" : "none";
  if (tabModuleJobs) tabModuleJobs.style.display = canAccessModule("jobs") ? "" : "none";
  if (tabModuleShares) tabModuleShares.style.display = canAccessModule("shares") ? "" : "none";
  if (tabModuleComfyui) tabModuleComfyui.style.display = canAccessModule("comfyui") ? "" : "none";
  if (tabModuleEconomy) tabModuleEconomy.style.display = canAccessModule("economy") ? "" : "none";
  if (tabModuleTrading) tabModuleTrading.style.display = (canAccessModule("economy") && canAccessModule("trading")) ? "" : "none";
  if (tabModuleAppeals) {
    if (currentRole !== "super_admin" && canAccessModule("appeals")) {
      tabModuleAppeals.style.display = "";
    } else {
      tabModuleAppeals.style.display = "none";
    }
  }
  if (typeof syncSidebarMenuVisibility === "function") {
    syncSidebarMenuVisibility();
    restoreSidebarState();
  }
  if (appealsTab) {
    if (currentRole === "super_admin" && canAccessModule("appeals")) {
      appealsTab.style.display = "";
    } else {
      appealsTab.style.display = "none";
    }
  }
  if (reportsTab) reportsTab.style.display = (currentRole === "super_admin" && isFeatureEnabledForUi("feature_reports_enabled", false)) ? "" : "none";
  if (governanceTab) governanceTab.style.display = ((currentRole === "manager" || currentRole === "super_admin") && isFeatureEnabledForUi("feature_member_governance_enabled", false)) ? "" : "none";
  if (noticesTab) noticesTab.style.display = ((currentRole === "manager" || currentRole === "super_admin") && isFeatureEnabledForUi("feature_reports_notifications_enabled", false)) ? "" : "none";
  const restartBtn = $("restart-server-btn");
	  if (restartBtn) restartBtn.style.display = currentUser === "root" ? "" : "none";
	  if (typeof setDriveActivePage === "function") {
	    setDriveActivePage(document.querySelector("[data-drive-page-tab].active")?.dataset.drivePageTab || "files");
	  }
	  syncActiveAccountStorageScope(previousAccountScope);

  if (currentMustChangePassword) {
    resetInactivityTimer();
    setTimeout(() => forceDefaultPasswordChange(), 0);
    return;
  }

  if (typeof startNotificationPoll === "function") startNotificationPoll();
  if (typeof ensureGameMultiplayerInvitePolling === "function") ensureGameMultiplayerInvitePolling();
  let requestedModuleParam = "";
  try {
    requestedModuleParam = new URLSearchParams(location.search || "").get("module") || "";
  } catch (err) {}
  let requestedInitialModule = "";
  if ((location.pathname === "/videos" || (location.hash || "").startsWith("#videos/")) && canAccessModule("videos")) {
    requestedInitialModule = "videos";
  } else if (requestedModuleParam === "games" && canAccessModule("games")) {
    requestedInitialModule = "games";
  } else if (requestedModuleParam === "experiments" && canAccessModule("experiments")) {
    requestedInitialModule = "experiments";
  }
  const initialModule = requestedInitialModule || (canAccessModule("accounts")
    ? "accounts"
    : canAccessModule("chat")
      ? "chat"
      : canAccessModule("profile")
        ? "profile"
      : canAccessModule("community")
          ? "community"
            : canAccessModule("privacy_uploads")
              ? "drive"
              : canAccessModule("videos")
                ? "videos"
                : canAccessModule("comfyui")
                  ? "comfyui"
                  : canAccessModule("games")
                    ? "games"
                    : canAccessModule("experiments")
                      ? "experiments"
                      : canAccessModule("economy")
                        ? "economy"
                        : (currentRole !== "super_admin" && canAccessModule("appeals")) ? "appeals" : "chat");
  switchModuleTab(initialModule);
  if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
  resetInactivityTimer();
}

function resetAuthState() {
  const previousAccountScope = getCurrentAccountStorageScope();
  showLoginScreen();
  try {
    localStorage.removeItem(AUTH_SESSION_HINT_STORAGE_KEY);
  } catch (err) {}
  if (typeof clearDriveE2eeSessionPassphrases === "function") clearDriveE2eeSessionPassphrases();
  clearUserAppearanceConfig();
  currentUser = null;
  currentUserId = null;
  currentUserAvatarFileId = "";
  currentRole = "user";
  currentRoleLabel = "user";
  currentAuthScope = "";
  currentAllowedFeatures = [];
  setForcedPasswordChangeState(false);
  inactivityLogoutMs = DEFAULT_INACTIVITY_LOGOUT_MS;
  inactivitySuspendReasons.clear();
  canManageUsers = false;
  syncActiveAccountStorageScope(previousAccountScope);
  users = [];
  currentServerTab = "overview";
  currentSystemTab = "health";
  editingUserIsSelf = false;
  updateSidebarIdentity();
  stopInactivityTimer();
  stopChatPoll();
  if (typeof stopNotificationPoll === "function") stopNotificationPoll();
  if (typeof stopGameMultiplayerInvitePolling === "function") stopGameMultiplayerInvitePolling();
  if (typeof stopTradingModuleTimers === "function") stopTradingModuleTimers();
  if (typeof stopEconomyAutoRefresh === "function") stopEconomyAutoRefresh();
  if (typeof clearDriveE2eeSessionPassphrases === "function") clearDriveE2eeSessionPassphrases();
  hideUserEditDialog();
  const welcomeMsg = $("welcome-msg");
  if (welcomeMsg) {
    welcomeMsg.classList.remove("birthday-greeting");
    welcomeMsg.textContent = "歡迎回來！";
  }
  const moduleChat = $("module-chat");
  const moduleProfile = $("module-profile");
  const moduleAnnouncements = $("module-announcements");
  const moduleCommunity = $("module-community");
  const moduleDrive = $("module-drive");
  const moduleAlbums = $("module-albums");
  const moduleVideos = $("module-videos");
  const moduleGames = $("module-games");
  const moduleExperiments = $("module-experiments");
  const moduleJobs = $("module-jobs");
  const moduleShares = $("module-shares");
  const moduleComfyui = $("module-comfyui");
  const moduleEconomy = $("module-economy");
  const moduleTrading = $("module-trading");
  const moduleAccounts = $("module-accounts");
  const moduleSystem = $("module-system");
  const moduleServer = $("module-server");
  const moduleAppeals = $("module-appeals");
  if (moduleChat) moduleChat.classList.remove("active");
  if (moduleProfile) moduleProfile.classList.remove("active");
  if (moduleAnnouncements) moduleAnnouncements.classList.remove("active");
  if (moduleCommunity) moduleCommunity.classList.remove("active");
  if (moduleDrive) moduleDrive.classList.remove("active");
  if (moduleAlbums) moduleAlbums.classList.remove("active");
  if (moduleVideos) moduleVideos.classList.remove("active");
  if (moduleGames) moduleGames.classList.remove("active");
  if (moduleExperiments) moduleExperiments.classList.remove("active");
  if (moduleJobs) moduleJobs.classList.remove("active");
  if (moduleShares) moduleShares.classList.remove("active");
  if (moduleComfyui) moduleComfyui.classList.remove("active");
  if (moduleEconomy) moduleEconomy.classList.remove("active");
  if (moduleTrading) moduleTrading.classList.remove("active");
  if (moduleAccounts) moduleAccounts.classList.remove("active");
  if (moduleSystem) moduleSystem.classList.remove("active");
  if (moduleServer) moduleServer.classList.remove("active");
  if (moduleAppeals) moduleAppeals.classList.remove("active");
  if (typeof setComfyuiTabAvailability === "function") setComfyuiTabAvailability(null);
  if (typeof syncSidebarMenuVisibility === "function") syncSidebarMenuVisibility();
  if (typeof syncRootModuleSettingsButtons === "function") syncRootModuleSettingsButtons();
  $("me-user").textContent = "-";
  $("me-role").textContent = "-";
  $("me-nickname").textContent = "-";
  selectedChatRoomId = null;
  chatRooms = [];
  const chatWarn = $("chat-room-warn");
  if (chatWarn) chatWarn.className = "msg";
  const chatRoomList = $("chat-room-list");
  if (chatRoomList) chatRoomList.innerHTML = "<p style=\"color:var(--muted);\">尚未登入</p>";
  const chatRoomTitle = $("chat-room-title");
  if (chatRoomTitle) chatRoomTitle.textContent = "請先建立或加入聊天室";
  const chatRoomMessages = $("chat-room-messages");
  if (chatRoomMessages) chatRoomMessages.innerHTML = "<p style=\"color:var(--muted);\">尚未登入</p>";
  userAppeals = [];
  adminAppeals = [];
  adminAppealPage = 1;
  adminAppealStatus = "pending";
  const tb = $("user-table")?.querySelector("tbody");
  if (tb) tb.innerHTML = "";
}

// Global modal close affordance: every popup should have close controls near top and bottom.
(function setupGlobalModalCloseAffordance() {
  const MODAL_TARGET_SELECTOR = [
    "#chat-room-create-panel",
    "#chat-room-join-panel",
    "#drive-upload-mode-overlay",
    "#game-multiplayer-invite-modal",
    ".user-edit-modal",
    ".app-dialog-card",
    ".comfyui-mask-editor-modal",
    ".comfyui-image-picker-modal",
    ".share-center-events-modal",
    ".profile-avatar-preview-modal",
    ".root-module-settings-modal",
    ".chess-action-dialog-modal",
    ".game-multiplayer-invite-modal",
    ".drive-preview-mobile-dialog",
    "[role='dialog']",
  ].join(",");
  const CLOSE_CONTROL_SELECTOR = [
    "[data-drive-action^='close']",
    "[data-drive-action*='close-']",
    "[data-share-events-close]",
    "[data-modal-close]",
    "[data-dialog-close]",
    "#app-dialog-cancel",
    "button[id$='-close-btn']",
    "button[id$='-cancel-btn']",
    "button[id$='-close']",
    "button[id$='-cancel']",
    "button.modal-close",
    "button.dialog-close",
  ].join(",");

  function modalCloseContent(candidate) {
    if (!candidate || candidate.nodeType !== 1) return null;
    if (candidate.matches?.(".user-edit-overlay, .app-dialog-overlay, .chat-room-dialog, .game-multiplayer-invite-overlay")) {
      return candidate.querySelector(".user-edit-modal, .app-dialog-card, [role='dialog']:not(.user-edit-overlay):not(.app-dialog-overlay)");
    }
    if (candidate.querySelector?.(":scope > .user-edit-modal")) return candidate.querySelector(":scope > .user-edit-modal");
    return candidate;
  }

  function elementVisible(el) {
    if (!el || el.hidden) return false;
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    return !style || (style.display !== "none" && style.visibility !== "hidden");
  }

  function isCloseControl(el) {
    return !!el && el.matches?.(CLOSE_CONTROL_SELECTOR) && !el.dataset.globalModalClose;
  }

  function existingCloseControls(modal) {
    return Array.from(modal.querySelectorAll(CLOSE_CONTROL_SELECTOR)).filter((el) => isCloseControl(el) && elementVisible(el));
  }

  function modalHasGlobalCloseRow(modal, position) {
    return !!modal.querySelector(`:scope > .global-modal-close-row-${position}`);
  }

  function modalTopHasClose(modal) {
    return modalHasGlobalCloseRow(modal, "top");
  }

  function modalBottomHasClose(modal) {
    return modalHasGlobalCloseRow(modal, "bottom");
  }

  function modalCloseBlocked(modal) {
    const userEdit = modal.closest?.("#user-edit-overlay");
    if (!userEdit) return false;
    const cancel = document.getElementById("user-edit-cancel");
    return !!cancel && !elementVisible(cancel);
  }

  function fallbackCloseModal(modal) {
    if (modalCloseBlocked(modal)) return;
    const overlay = modal.closest?.(".user-edit-overlay, .app-dialog-overlay, .album-full-preview-overlay, .root-module-settings-overlay, [aria-modal='true']");
    if (overlay && overlay !== modal) {
      overlay.classList.remove("show", "active", "open");
      overlay.setAttribute("aria-hidden", "true");
      if (overlay.hasAttribute("hidden")) overlay.hidden = true;
    } else {
      modal.classList.remove("show", "active", "open");
      modal.setAttribute("aria-hidden", "true");
      if (modal.hasAttribute("hidden")) modal.hidden = true;
    }
    if (!document.querySelector(".user-edit-overlay.show, .album-full-preview-overlay.show, .app-dialog-overlay.show")) {
      document.body?.classList?.remove("modal-open");
    }
  }

  function closeModalFromButton(button) {
    const modal = button.closest("#chat-room-create-panel, #chat-room-join-panel, #drive-upload-mode-overlay, #game-multiplayer-invite-modal, .user-edit-modal, .app-dialog-card, .comfyui-mask-editor-modal, .comfyui-image-picker-modal, .share-center-events-modal, .profile-avatar-preview-modal, .root-module-settings-modal, .chess-action-dialog-modal, .game-multiplayer-invite-modal, .drive-preview-mobile-dialog, [role='dialog']");
    if (!modal) return;
    if (modalCloseBlocked(modal)) return;
    const existing = existingCloseControls(modal)[0];
    if (existing) {
      existing.click();
      return;
    }
    const escapeEvent = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    modal.dispatchEvent(escapeEvent);
    document.dispatchEvent(escapeEvent);
    setTimeout(() => {
      const stillOpen = modal.closest(".show") || (!modal.hidden && elementVisible(modal));
      if (stillOpen) fallbackCloseModal(modal);
    }, 0);
  }

  function createCloseRow(position) {
    const row = document.createElement("div");
    row.className = `global-modal-close-row global-modal-close-row-${position}`;
    row.dataset.globalModalClose = position;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-sm global-modal-close-btn";
    button.dataset.globalModalClose = position;
    button.textContent = "關閉";
    button.addEventListener("click", () => closeModalFromButton(button));
    row.appendChild(button);
    return row;
  }

  function enhanceModal(candidate) {
    const modal = modalCloseContent(candidate);
    if (!modal || modal.dataset.globalModalCloseReady === "1") return;
    if (modal.matches?.(".user-edit-overlay, .app-dialog-overlay")) return;
    modal.dataset.globalModalCloseReady = "1";
    if (!modalTopHasClose(modal)) {
      modal.insertBefore(createCloseRow("top"), modal.firstChild);
    }
    if (!modalBottomHasClose(modal)) {
      modal.appendChild(createCloseRow("bottom"));
    }
  }

  function enhanceAllModals(root = document) {
    if (!root?.querySelectorAll) return;
    root.querySelectorAll(MODAL_TARGET_SELECTOR).forEach(enhanceModal);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => enhanceAllModals(document), { once: true });
  } else {
    enhanceAllModals(document);
  }
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      mutation.addedNodes.forEach((node) => {
        if (node.nodeType !== 1) return;
        if (node.matches?.(MODAL_TARGET_SELECTOR)) enhanceModal(node);
        enhanceAllModals(node);
      });
    }
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
