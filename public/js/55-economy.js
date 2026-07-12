let economyLedgerOffset = 0;
let economyLedgerCache = [];
let economyBlockCountdownTimer = null;
let economyBlockSchedule = null;
let economyInlineEventsBound = false;
let economyDocumentEventsBound = false;
let economyAutoRefreshTimer = null;
let economyAutoRefreshBusy = false;
let economyAccountGeneration = 0;
const economyActiveFileReaders = new Set();
const economyPendingFilePickerCancels = new Set();
let economyColdWalletDraft = null;
let economyColdWalletBindCandidate = null;
let economyColdWalletSigningSessions = new Map();
let economyWalletOnboardingState = {};
let economyCurrentChainBranch = "main";
let economyCatalogCache = [];
let economyFundAddressCache = {};
let economyGovernanceProposalCache = new Map();
let economyTreasurySignerCenterCache = null;
let economyGovernanceCategory = "all";
let economyGovernanceStatusFilter = "review";
let economySelectedDisputeUuid = "";
let economySelectedDisputeProposalUuids = new Set();
let economyTransactionDisputeCache = [];
let economyExpandedGovernanceProposalUuids = new Set();
let economyOfficialHotWalletLabels = {};
const ECONOMY_PAGE_STORAGE_KEY = "hackme_web:economy:active_page";
const ECONOMY_SPEND_WALLET_STORAGE_KEY = "hackme_web:economy:default_spend_wallet";
const ECONOMY_FAVORITE_ADDRESS_STORAGE_KEY = "hackme_web:economy:favorite_addresses";
const ECONOMY_COLD_WALLET_FILE_FORMAT = "hackme-pcw1-encrypted-wallet";
const ECONOMY_COLD_WALLET_FILE_VERSION = 1;
const ECONOMY_COLD_WALLET_KDF_ITERATIONS = 600000;
const ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX = "pcp1";
const ECONOMY_COLD_UNLOCK_MNEMONIC_WORD_COUNT = 12;
const ECONOMY_COLD_UNLOCK_QUIZ_COUNT = 4;
const ECONOMY_COLD_WALLET_SIGNING_SESSION_MS = 10 * 60 * 1000;
const ECONOMY_COLD_UNLOCK_WORDS = `
able acid actor adapt adult agent ahead alarm album alert alley allow angle apple arena armor aroma arrow asset audio audit avoid badge basic beach began berry birth black blend bonus brave bread brick bring brush build cable canal candy carry cedar chair charm chart chase cheap cheer chess civic clean clerk clock cloud coast comic coral craft crisp crown dance delta diary donor draft dream drink early earth elbow entry equal error event extra faith fancy field final flame flash floor focus force forum frame fresh front frost fruit garden ghost giant glass grace grain grape green grid group guard guest happy harbor heart heavy hinge honey honor hotel house human image index inner input ivory jewel joint judge juice jumbo karma kayak laser laugh layer lemon level light logic lunar magic major maple march match media melon mercy metal meter micro mimic model money month motor music never night noble north ocean olive orbit order outer panel paper party patch patio peach pearl phase photo piano piece pilot pixel plant plaza point polar promo proof pulse quiet quota radio rapid ratio raven ready relay renew reply river robot rough royal scale scene scope score scout share shell shift shine shirt skill slate smart smile solar solid sound space spark speed spice sport staff stage stone store storm story sugar sunny super sword table tango thank theme tiger token topic tower trade trail train trust union urban valid value video vivid voice wallet water wheel white wisdom world yacht young zebra zero zone
`.trim().split(/\s+/);
const ECONOMY_GOV_RATE_UNIT_SUFFIX = "b" + "ps";
const ECONOMY_ADDRESS_DISPUTE_MIN_STATEMENT_CHARS = 12;

function economyPageStorageKey() {
  return typeof accountScopedStorageKey === "function" ? accountScopedStorageKey(ECONOMY_PAGE_STORAGE_KEY) : ECONOMY_PAGE_STORAGE_KEY;
}

function economySpendWalletStorageKey() {
  return typeof accountScopedStorageKey === "function" ? accountScopedStorageKey(ECONOMY_SPEND_WALLET_STORAGE_KEY) : ECONOMY_SPEND_WALLET_STORAGE_KEY;
}

function economyFavoriteAddressStorageKey() {
  return typeof accountScopedStorageKey === "function" ? accountScopedStorageKey(ECONOMY_FAVORITE_ADDRESS_STORAGE_KEY) : ECONOMY_FAVORITE_ADDRESS_STORAGE_KEY;
}

function readEconomyActivePage() {
  try {
    return localStorage.getItem(economyPageStorageKey()) || "balance";
  } catch (_) {
    return "balance";
  }
}

function readEconomySpendWalletAddress() {
  try {
    return localStorage.getItem(economySpendWalletStorageKey()) || "";
  } catch (_) {
    return "";
  }
}

function readEconomyDefaultSpendWalletAddress() {
  return readEconomySpendWalletAddress();
}

function writeEconomySpendWalletAddress(address) {
  try {
    const key = economySpendWalletStorageKey();
    const normalized = String(address || "").trim().toLowerCase();
    const previous = localStorage.getItem(key) || "";
    localStorage.setItem(key, normalized);
    if (previous !== normalized && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("economy:default-spend-wallet-changed", {
        detail: { address: normalized },
      }));
    }
  } catch (_) {}
}

function writeEconomyDefaultSpendWalletAddress(address) {
  writeEconomySpendWalletAddress(address);
}

function economyNormalizeAddress(address) {
  return String(address || "").trim().toLowerCase();
}

function economyIsPc0Address(address) {
  return economyNormalizeAddress(address).startsWith("pc0");
}

function readEconomyFavoriteAddresses() {
  try {
    const raw = localStorage.getItem(economyFavoriteAddressStorageKey()) || "[]";
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const seen = new Set();
    return parsed.map((item) => {
      const address = economyNormalizeAddress(typeof item === "string" ? item : item?.address);
      const label = String(typeof item === "string" ? "" : item?.label || "").trim().slice(0, 80);
      if (!address || seen.has(address)) return null;
      seen.add(address);
      return { address, label };
    }).filter(Boolean).slice(0, 60);
  } catch (_) {
    return [];
  }
}

function writeEconomyFavoriteAddresses(items) {
  try {
    localStorage.setItem(economyFavoriteAddressStorageKey(), JSON.stringify((items || []).slice(0, 60)));
  } catch (_) {}
}

function economyFavoriteAddressLabel(item) {
  const label = String(item?.label || "").trim();
  const address = economyNormalizeAddress(item?.address);
  return label ? `${label} · ${shortEconomyWalletAddress(address)}` : shortEconomyWalletAddress(address);
}

function upsertEconomyFavoriteAddress(address, label = "") {
  const normalized = economyNormalizeAddress(address);
  if (!normalized) throw new Error("請先輸入常用地址");
  const nextLabel = String(label || "").trim().slice(0, 80);
  const items = readEconomyFavoriteAddresses().filter((item) => item.address !== normalized);
  items.unshift({ address: normalized, label: nextLabel });
  writeEconomyFavoriteAddresses(items);
  return items;
}

function removeEconomyFavoriteAddress(address) {
  const normalized = economyNormalizeAddress(address);
  const items = readEconomyFavoriteAddresses().filter((item) => item.address !== normalized);
  writeEconomyFavoriteAddresses(items);
  return items;
}

let economyActivePage = readEconomyActivePage();

function shouldRunEconomyAutoRefresh() {
  if (!currentUser || document.hidden || currentModuleTab !== "economy") return false;
  return true;
}

function economyDashboardRefreshMs() {
  const seconds = Number(siteConfig?.economy_dashboard_refresh_seconds || 30);
  return Math.max(5, Math.min(600, Number.isFinite(seconds) ? seconds : 30)) * 1000;
}

function stopEconomyAutoRefresh() {
  if (economyAutoRefreshTimer) {
    clearInterval(economyAutoRefreshTimer);
    economyAutoRefreshTimer = null;
  }
}

function economyOperationContext() {
  return {
    generation: economyAccountGeneration,
    scope: typeof getCurrentAccountStorageScope === "function"
      ? getCurrentAccountStorageScope()
      : String(currentUserId || currentUser || "anonymous"),
  };
}

function economyOperationIsCurrent(operation) {
  if (!operation || Number(operation.generation) !== economyAccountGeneration) return false;
  const scope = typeof getCurrentAccountStorageScope === "function"
    ? getCurrentAccountStorageScope()
    : String(currentUserId || currentUser || "anonymous");
  return operation.scope === scope;
}

function economyAccountAbortError() {
  const err = new Error("Account context changed");
  err.name = "AbortError";
  return err;
}

function economyAssertOperationCurrent(operation) {
  if (!economyOperationIsCurrent(operation)) throw economyAccountAbortError();
}

function economyScrubPrivateJwk(jwk) {
  if (jwk && typeof jwk === "object" && "d" in jwk) jwk.d = "";
}

function resetEconomyAccountState() {
  economyAccountGeneration += 1;
  stopEconomyAutoRefresh();
  economyActiveFileReaders.forEach((reader) => {
    try { reader.abort(); } catch (_) {}
  });
  economyActiveFileReaders.clear();
  economyPendingFilePickerCancels.forEach((cancel) => cancel());
  economyPendingFilePickerCancels.clear();
  destroyEconomyColdWalletSecrets();
  if (typeof window.resetEconomyExplorerAccountState === "function") {
    window.resetEconomyExplorerAccountState();
  }
  if (economyBlockCountdownTimer) clearInterval(economyBlockCountdownTimer);
  economyBlockCountdownTimer = null;
  economyBlockSchedule = null;
  economyAutoRefreshBusy = false;
  economyLedgerOffset = 0;
  economyLedgerCache = [];
  economyColdWalletSigningSessions.clear();
  economyWalletOnboardingState = {};
  economyCurrentChainBranch = "main";
  economyCatalogCache = [];
  economyFundAddressCache = {};
  economyGovernanceProposalCache.clear();
  economyTreasurySignerCenterCache = null;
  economySelectedDisputeUuid = "";
  economySelectedDisputeProposalUuids.clear();
  economyTransactionDisputeCache = [];
  economyExpandedGovernanceProposalUuids.clear();
  economyOfficialHotWalletLabels = {};
  [
    "economy-ledger-list",
    "economy-wallet-list",
    "economy-transaction-list",
    "economy-transaction-dispute-list",
    "economy-governance-list",
    "economy-catalog-list",
    "economy-root-report",
    "economy-treasury-signer-center",
  ].forEach((id) => {
    const element = $(id);
    if (element) element.replaceChildren();
  });
}

function startEconomyAutoRefresh() {
  if (!shouldRunEconomyAutoRefresh() || economyAutoRefreshTimer) return;
  economyAutoRefreshTimer = setInterval(async () => {
    if (!shouldRunEconomyAutoRefresh() || economyAutoRefreshBusy) return;
    economyAutoRefreshBusy = true;
    try {
      await loadEconomyDashboard();
    } finally {
      economyAutoRefreshBusy = false;
    }
  }, economyDashboardRefreshMs());
}

function syncEconomyAutoRefreshLifecycle() {
  if (shouldRunEconomyAutoRefresh()) startEconomyAutoRefresh();
  else stopEconomyAutoRefresh();
}

function toggleWalletCard(bodyId, btn) {
  const body = $(bodyId);
  if (!body) return;
  const hidden = body.style.display === "none";
  body.style.display = hidden ? "" : "none";
  if (btn) {
    btn.textContent = hidden ? "▾" : "▸";
    btn.setAttribute("aria-expanded", hidden ? "true" : "false");
  }
}

function economyInlineMsg(targetId, text, ok = true, fallbackLabel = "PointsChain") {
  const el = $(targetId);
  if (!el) {
    const message = String(text || "").trim();
    if (message && typeof showAppToast === "function") {
      showAppToast(`${fallbackLabel}：${message}`, ok, { duration: ok ? 3600 : 6200 });
    }
    if (message && window.console) {
      const logger = ok ? console.info : console.error;
      logger.call(console, `[economy] missing message target #${targetId}: ${message}`);
    }
    return;
  }
  el.textContent = text || "";
  el.classList.toggle("show", Boolean(text));
  el.classList.toggle("ok", Boolean(text) && Boolean(ok));
  el.classList.toggle("err", Boolean(text) && !ok);
  el.classList.remove("info");
  el.setAttribute("role", ok ? "status" : "alert");
  el.setAttribute("aria-live", ok ? "polite" : "assertive");
  el.setAttribute("aria-atomic", "true");
  el.style.color = ok ? "#4caf50" : "#ff4f6d";
  scheduleInlineMessageClear(el, text, ok);
}

function economySetMsg(text, ok = true) {
  economyInlineMsg("economy-msg", text, ok, "PointsChain");
}

function auditChainActionMsg(text, ok = true) {
  economyInlineMsg("audit-chain-action-status", text, ok, "審計鏈");
}

function economyRecoveryActionMsg(text, ok = true) {
  economyInlineMsg("economy-recovery-action-status", text, ok, "PointsChain 恢復");
}

function economyRequestId(prefix = "economy") {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return `${prefix}:${window.crypto.randomUUID()}`;
  }
  return `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
}

function economyWalletMsg(text, ok = true) {
  economyInlineMsg("economy-wallet-onboarding-msg", text, ok, "錢包管理");
}

function economyExplorerMsg(text, ok = true) {
  economyInlineMsg("economy-explorer-msg", text, ok, "鏈上瀏覽器");
}

function economyGovernanceMsg(text, ok = true) {
  economyInlineMsg("economy-governance-msg", text, ok, "PointsChain 治理");
}

function economyTransactionMsg(text, ok = true) {
  economyInlineMsg("economy-transactions-msg", text, ok, "鏈上交易");
}

function economyTransferMsg(text, ok = true) {
  economyInlineMsg("economy-transfer-msg", text, ok, "鏈上送單");
}

function economyFailureMessage(err, fallback) {
  const message = String(err?.message || "").trim();
  if (message) return message;
  return fallback || "操作失敗";
}

function economyNotifyFailure(err, { msgFn = economySetMsg, label = "PointsChain", fallback = "操作失敗" } = {}) {
  const message = economyFailureMessage(err, fallback);
  if (typeof msgFn === "function") msgFn(message, false);
  if (msgFn !== economySetMsg) economySetMsg(message, false);
  if (message && typeof showAppToast === "function") {
    showAppToast(`${label}：${message}`, false, { duration: 8200 });
  }
  return message;
}

function economyNotifySuccess(text, { msgFn = economySetMsg, label = "PointsChain", toast = true } = {}) {
  const message = String(text || "").trim();
  if (!message) return "";
  if (typeof msgFn === "function") msgFn(message, true);
  if (msgFn !== economySetMsg) economySetMsg(message, true);
  if (toast && typeof showAppToast === "function") {
    showAppToast(`${label}：${message}`, true, { duration: 4200 });
  }
  return message;
}

function economyAddressDisputeStatementLength(text) {
  return Array.from(String(text || "").trim()).length;
}

function economyPromptAddressDisputeStatement({ promptText, cancelText, shortText, msgFn }) {
  while (true) {
    const statement = prompt(promptText, "");
    if (statement === null) {
      msgFn(cancelText, false);
      return null;
    }
    const normalized = String(statement || "").trim();
    const length = economyAddressDisputeStatementLength(normalized);
    if (length >= ECONOMY_ADDRESS_DISPUTE_MIN_STATEMENT_CHARS) return normalized;
    msgFn(`${shortText}（目前 ${length}/${ECONOMY_ADDRESS_DISPUTE_MIN_STATEMENT_CHARS} 字），請補充原因與佐證後再送出。`, false);
  }
}

function economyWarningSuffix(json) {
  const warnings = Array.isArray(json?.warnings) ? json.warnings.filter(Boolean) : [];
  if (!warnings.length) return "";
  if (warnings.includes("notification_delivery_failed")) return "；部分通知送出失敗，請到鏈上交易確認交易狀態。";
  return `；警告：${warnings.map((item) => String(item)).join("、")}`;
}

async function copyEconomyText(text, successText = "地址已複製", options = {}) {
  const value = String(text || "").trim();
  const contentLabel = String(options.contentLabel || "地址").trim() || "內容";
  if (!value) {
    economyWalletMsg("沒有可複製的內容", false);
    return false;
  }
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      economyWalletMsg(successText, true);
      if (typeof showAppToast === "function") showAppToast(`錢包管理：${successText}`, true, { duration: 3600 });
      return true;
    }
  } catch (_) {}
  if (typeof showCopyFallbackDialog === "function") {
    showCopyFallbackDialog(value, `複製${contentLabel}`);
    const fallbackText = `請在彈出視窗手動複製${contentLabel}`;
    economyWalletMsg(fallbackText, true);
    if (typeof showAppToast === "function") showAppToast(`錢包管理：${fallbackText}`, true, { duration: 5200 });
    return false;
  }
  window.prompt(`請手動複製${contentLabel}`, value);
  const manualText = `請手動複製${contentLabel}`;
  economyWalletMsg(manualText, true);
  if (typeof showAppToast === "function") showAppToast(`錢包管理：${manualText}`, true, { duration: 5200 });
  return false;
}

function destroyEconomyColdWalletSecrets({ hideGenerated = true } = {}) {
  economyColdWalletDraft = null;
  economyColdWalletBindCandidate = null;
  [
    "economy-wallet-generated-address",
    "economy-wallet-generated-file-name",
    "economy-wallet-generated-trade-password",
    "economy-wallet-private-key",
    "economy-wallet-file-password",
  ].forEach((id) => {
    const el = $(id);
    if (el && "value" in el) el.value = "";
  });
  const fileInput = $("economy-wallet-file-input");
  if (fileInput && "value" in fileInput) fileInput.value = "";
  const signingFileInput = $("economy-wallet-signing-file-input");
  if (signingFileInput && "value" in signingFileInput) signingFileInput.value = "";
  const confirmed = $("economy-wallet-private-key-confirmed");
  if (confirmed) confirmed.checked = false;
  const selectionStatus = $("economy-wallet-generated-selection-status");
  if (selectionStatus) selectionStatus.textContent = "尚未選用";
  resetEconomyColdWalletMnemonicQuiz();
  const downloadBtn = $("economy-wallet-download-file-btn");
  if (downloadBtn) downloadBtn.disabled = false;
  const copyBtn = $("economy-wallet-copy-trade-password-btn");
  if (copyBtn) copyBtn.disabled = false;
  const quizBtn = $("economy-wallet-start-mnemonic-quiz-btn");
  if (quizBtn) {
    quizBtn.disabled = true;
    quizBtn.textContent = "確認已保存，隱藏助記詞並開始考試";
  }
  const useBtn = $("economy-wallet-use-generated-cold-btn");
  if (useBtn) {
    useBtn.disabled = true;
    useBtn.textContent = "先完成記憶詞考試";
  }
  const panel = $("economy-wallet-generated-panel");
  if (panel && hideGenerated) panel.style.display = "none";
}

function economyBase64UrlFromBytes(bytes) {
  let binary = "";
  new Uint8Array(bytes).forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function economyBytesFromBase64Url(value) {
  const text = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
  const padded = text + "=".repeat((4 - (text.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function economyNormalizeColdWalletUnlockSecret(secret) {
  const raw = String(secret || "").trim().toLowerCase();
  if (!raw) return "";
  if (raw.startsWith(`${ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX} `)) {
    const words = raw.slice(ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX.length).trim().split(/[\s-]+/).filter(Boolean);
    return `${ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX} ${words.join(" ")}`;
  }
  if (raw.startsWith(`${ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX}-`)) {
    const words = raw.slice(ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX.length + 1).trim().split(/[\s-]+/).filter(Boolean);
    return `${ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX} ${words.join(" ")}`;
  }
  return raw.split(/\s+/).join(" ");
}

function economyMnemonicWordsFromDigest(digest) {
  const bytes = new Uint8Array(digest);
  const words = [];
  for (let index = 0; index < ECONOMY_COLD_UNLOCK_MNEMONIC_WORD_COUNT; index += 1) {
    words.push(ECONOMY_COLD_UNLOCK_WORDS[bytes[index] % ECONOMY_COLD_UNLOCK_WORDS.length]);
  }
  return words;
}

function economyColdUnlockMnemonicFromWords(words) {
  return `${ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX} ${(words || []).join(" ")}`.trim();
}

function economyColdWalletMnemonicWords(secret) {
  const normalized = economyNormalizeColdWalletUnlockSecret(secret);
  if (!normalized.startsWith(`${ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX} `)) return [];
  return normalized.slice(ECONOMY_COLD_UNLOCK_MNEMONIC_PREFIX.length).trim().split(/\s+/).filter(Boolean);
}

function economyBuildColdWalletMnemonicQuiz(words) {
  const list = Array.isArray(words) ? words : [];
  if (list.length < ECONOMY_COLD_UNLOCK_QUIZ_COUNT) return [];
  const selected = [];
  const used = new Set();
  const random = new Uint8Array(list.length);
  crypto.getRandomValues(random);
  for (const value of random) {
    const index = value % list.length;
    if (used.has(index)) continue;
    used.add(index);
    selected.push({ index, word: list[index] });
    if (selected.length >= ECONOMY_COLD_UNLOCK_QUIZ_COUNT) break;
  }
  for (let index = 0; selected.length < ECONOMY_COLD_UNLOCK_QUIZ_COUNT && index < list.length; index += 1) {
    if (!used.has(index)) selected.push({ index, word: list[index] });
  }
  return selected.sort((a, b) => a.index - b.index);
}

function renderEconomyColdWalletMnemonicQuiz() {
  const box = $("economy-wallet-mnemonic-quiz");
  const prompts = $("economy-wallet-mnemonic-quiz-prompts");
  const status = $("economy-wallet-mnemonic-quiz-status");
  if (!box || !prompts) return;
  const quiz = economyColdWalletDraft?.mnemonicQuiz || [];
  if (!quiz.length) {
    box.style.display = "none";
    prompts.innerHTML = "";
    if (status) status.textContent = "";
    return;
  }
  box.style.display = "";
  prompts.innerHTML = quiz.map((item) => `
    <label class="field">
      第 ${Number(item.index) + 1} 詞
      <input type="text" data-cold-wallet-mnemonic-index="${Number(item.index)}" autocomplete="off" spellcheck="false" placeholder="輸入第 ${Number(item.index) + 1} 詞" />
    </label>
  `).join("");
  if (status) status.textContent = "通過記憶詞考試後，才可選用並綁定此冷錢包。";
}

function startEconomyColdWalletMnemonicQuiz() {
  if (!economyColdWalletDraft?.mnemonicQuiz?.length) {
    economyWalletMsg("目前沒有可考試的冷錢包解鎖助記詞；請先建立冷錢包。", false);
    return;
  }
  const secretInput = $("economy-wallet-generated-trade-password");
  if (secretInput) {
    secretInput.value = "";
    secretInput.type = "password";
    secretInput.placeholder = "已隱藏；考試失敗需重新建立冷錢包";
  }
  const copyBtn = $("economy-wallet-copy-trade-password-btn");
  if (copyBtn) copyBtn.disabled = true;
  const quizBtn = $("economy-wallet-start-mnemonic-quiz-btn");
  if (quizBtn) {
    quizBtn.disabled = true;
    quizBtn.textContent = "記憶詞已隱藏";
  }
  renderEconomyColdWalletMnemonicQuiz();
  economyWalletMsg("解鎖助記詞已隱藏；請依記憶填回答案。若答錯，本次冷錢包草稿會作廢並需重建。");
}

function resetEconomyColdWalletMnemonicQuiz() {
  const box = $("economy-wallet-mnemonic-quiz");
  const prompts = $("economy-wallet-mnemonic-quiz-prompts");
  const status = $("economy-wallet-mnemonic-quiz-status");
  if (box) box.style.display = "none";
  if (prompts) prompts.innerHTML = "";
  if (status) status.textContent = "";
}

function economyGeneratedColdWalletReadyForSelection() {
  return Boolean(economyColdWalletDraft?.quizPassed);
}

function syncEconomyGeneratedColdWalletSelectionButton() {
  const useBtn = $("economy-wallet-use-generated-cold-btn");
  if (!useBtn) return;
  const ready = economyGeneratedColdWalletReadyForSelection();
  useBtn.disabled = !ready;
  useBtn.textContent = ready ? "選用此冷錢包" : "先完成記憶詞考試";
}

function checkEconomyColdWalletMnemonicQuiz() {
  if (!economyColdWalletDraft?.mnemonicQuiz?.length) {
    economyWalletMsg("目前沒有可驗證的冷錢包解鎖助記詞", false);
    return;
  }
  const inputs = Array.from(document.querySelectorAll("[data-cold-wallet-mnemonic-index]"));
  const answers = new Map(inputs.map((input) => [
    Number(input.getAttribute("data-cold-wallet-mnemonic-index")),
    String(input.value || "").trim().toLowerCase(),
  ]));
  const wrong = economyColdWalletDraft.mnemonicQuiz.filter((item) => answers.get(Number(item.index)) !== String(item.word || "").toLowerCase());
  if (wrong.length) {
    const first = wrong[0];
    destroyEconomyColdWalletSecrets();
    economyWalletMsg(`記憶詞考試未通過：第 ${Number(first.index) + 1} 詞不一致。本次冷錢包草稿已作廢，請重新建立冷錢包並重新保存助記詞。`, false);
    return;
  }
  economyColdWalletDraft.quizPassed = true;
  syncEconomyGeneratedColdWalletSelectionButton();
  const status = $("economy-wallet-mnemonic-quiz-status");
  if (status) status.textContent = "記憶詞考試已通過，可以選用此冷錢包。";
  economyWalletMsg("記憶詞考試已通過；請確認錢包檔與解鎖助記詞已分開離線保存。");
}

function economyCanonicalPublicJwk(jwk) {
  return {
    crv: String(jwk?.crv || ""),
    kty: String(jwk?.kty || ""),
    x: String(jwk?.x || ""),
    y: String(jwk?.y || ""),
  };
}

async function economyDerivedColdWalletTradePassword(privateJwk, address, operation = economyOperationContext()) {
  economyAssertOperationCurrent(operation);
  const material = [
    "hackme-pcw1-trade-password-v1",
    String(address || "").trim().toLowerCase(),
    String(privateJwk?.crv || ""),
    String(privateJwk?.x || ""),
    String(privateJwk?.y || ""),
    String(privateJwk?.d || ""),
  ].join("|");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(material));
  economyAssertOperationCurrent(operation);
  return economyColdUnlockMnemonicFromWords(economyMnemonicWordsFromDigest(digest));
}

async function economyDeriveColdWalletFileKey(password, salt, iterations = ECONOMY_COLD_WALLET_KDF_ITERATIONS, operation = economyOperationContext()) {
  economyAssertOperationCurrent(operation);
  const passphrase = economyNormalizeColdWalletUnlockSecret(password);
  if (!passphrase) throw new Error("請輸入冷錢包解鎖助記詞");
  const baseKey = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(passphrase),
    "PBKDF2",
    false,
    ["deriveKey"]
  );
  economyAssertOperationCurrent(operation);
  const key = await crypto.subtle.deriveKey(
    {
      name: "PBKDF2",
      hash: "SHA-256",
      salt,
      iterations: Math.max(120000, Math.floor(Number(iterations || ECONOMY_COLD_WALLET_KDF_ITERATIONS))),
    },
    baseKey,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"]
  );
  economyAssertOperationCurrent(operation);
  return key;
}

function economyIsEncryptedColdWalletFilePayload(payload) {
  return payload
    && payload.format === ECONOMY_COLD_WALLET_FILE_FORMAT
    && Number(payload.version || 0) === ECONOMY_COLD_WALLET_FILE_VERSION
    && payload.kdf
    && payload.cipher
    && payload.address;
}

async function economyEncryptColdWalletFile({ privateJwk, publicJwk, address, password, operation = economyOperationContext() }) {
  economyAssertOperationCurrent(operation);
  const salt = crypto.getRandomValues(new Uint8Array(32));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await economyDeriveColdWalletFileKey(password, salt, ECONOMY_COLD_WALLET_KDF_ITERATIONS, operation);
  economyAssertOperationCurrent(operation);
  const plaintext = {
    address,
    public_key_jwk: economyCanonicalPublicJwk(publicJwk),
    private_key_jwk: privateJwk,
    created_at: new Date().toISOString(),
  };
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    new TextEncoder().encode(JSON.stringify(plaintext))
  );
  economyAssertOperationCurrent(operation);
  return {
    format: ECONOMY_COLD_WALLET_FILE_FORMAT,
    version: ECONOMY_COLD_WALLET_FILE_VERSION,
    address,
    public_key_jwk: economyCanonicalPublicJwk(publicJwk),
    algorithm: {
      curve: "P-256",
      signature: "ECDSA_SHA256",
    },
    kdf: {
      name: "PBKDF2",
      hash: "SHA-256",
      iterations: ECONOMY_COLD_WALLET_KDF_ITERATIONS,
      salt: economyBase64UrlFromBytes(salt),
    },
    cipher: {
      name: "AES-GCM",
      iv: economyBase64UrlFromBytes(iv),
      ciphertext: economyBase64UrlFromBytes(ciphertext),
    },
    created_at: plaintext.created_at,
  };
}

async function economyDecryptColdWalletFilePayload(payload, password, operation = economyOperationContext()) {
  economyAssertOperationCurrent(operation);
  if (!economyIsEncryptedColdWalletFilePayload(payload)) {
    throw new Error("冷錢包檔格式不正確");
  }
  const salt = economyBytesFromBase64Url(payload.kdf?.salt || "");
  const iv = economyBytesFromBase64Url(payload.cipher?.iv || "");
  const ciphertext = economyBytesFromBase64Url(payload.cipher?.ciphertext || "");
  const key = await economyDeriveColdWalletFileKey(password, salt, payload.kdf?.iterations, operation);
  economyAssertOperationCurrent(operation);
  let decoded = null;
  let plaintext = null;
  try {
    plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ciphertext);
    decoded = JSON.parse(new TextDecoder().decode(plaintext));
  } catch (_) {
    throw new Error("冷錢包解鎖助記詞錯誤或冷錢包檔已損毀");
  } finally {
    if (plaintext) new Uint8Array(plaintext).fill(0);
  }
  economyAssertOperationCurrent(operation);
  const privateJwk = decoded?.private_key_jwk;
  if (!privateJwk?.d || !privateJwk?.x || !privateJwk?.y) throw new Error("冷錢包檔缺少私鑰資料");
  try {
    const publicJwk = economyCanonicalPublicJwk(privateJwk);
    const { address } = await economyWalletAddressFromPublicJwk(publicJwk);
    economyAssertOperationCurrent(operation);
    if (String(address || "").trim().toLowerCase() !== String(payload.address || "").trim().toLowerCase()) {
      throw new Error("冷錢包檔地址與私鑰不一致");
    }
    return privateJwk;
  } catch (err) {
    economyScrubPrivateJwk(privateJwk);
    throw err;
  }
}

function economyColdWalletFileName(address) {
  const suffix = String(address || "").trim().toLowerCase().slice(-12) || Date.now();
  return `hackme-cold-wallet-${suffix}.pcw1.json`;
}

function economyDownloadTextFile(filename, text, mimeType = "application/json") {
  const blob = new Blob([text], { type: `${mimeType};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function economyDownloadDraftColdWalletFile() {
  if (!economyColdWalletDraft?.walletFile || !economyColdWalletDraft?.walletFileName) {
    economyWalletMsg("目前沒有可下載的冷錢包檔", false);
    if (typeof showAppToast === "function") showAppToast("錢包管理：目前沒有可下載的冷錢包檔", false, { duration: 4200 });
    return;
  }
  economyDownloadTextFile(economyColdWalletDraft.walletFileName, JSON.stringify(economyColdWalletDraft.walletFile, null, 2));
  economyWalletMsg("已準備下載冷錢包檔；請與冷錢包解鎖助記詞分開離線保存。");
  if (typeof showAppToast === "function") showAppToast("錢包管理：已準備下載冷錢包檔", true, { duration: 4200 });
}

async function economyCopyDraftTradePassword() {
  const password = economyColdWalletDraft?.tradePassword || $("economy-wallet-generated-trade-password")?.value || "";
  await copyEconomyText(password, "冷錢包解鎖助記詞已複製；請離線保存", { contentLabel: "冷錢包解鎖助記詞" });
}

async function economyReadTextFile(file, operation = economyOperationContext()) {
  economyAssertOperationCurrent(operation);
  if (!file) throw new Error("請選擇冷錢包檔");
  if (typeof file.text === "function") {
    const text = await file.text();
    economyAssertOperationCurrent(operation);
    return text;
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const cleanup = () => economyActiveFileReaders.delete(reader);
    reader.onload = () => {
      cleanup();
      if (!economyOperationIsCurrent(operation)) {
        reject(economyAccountAbortError());
        return;
      }
      resolve(String(reader.result || ""));
    };
    reader.onerror = () => {
      cleanup();
      reject(new Error("讀取冷錢包檔失敗"));
    };
    reader.onabort = () => {
      cleanup();
      reject(economyAccountAbortError());
    };
    economyActiveFileReaders.add(reader);
    reader.readAsText(file);
  });
}

async function economyLoadEncryptedColdWalletFile(raw, password, { imported = true, operation = null } = {}) {
  const operationContext = operation || economyOperationContext();
  economyAssertOperationCurrent(operationContext);
  let payload = null;
  try {
    payload = JSON.parse(String(raw || "").trim());
  } catch (_) {
    throw new Error("冷錢包檔不是有效 JSON");
  }
  let privateJwk = null;
  try {
    privateJwk = await economyDecryptColdWalletFilePayload(payload, password, operationContext);
    economyAssertOperationCurrent(operationContext);
    const publicJwk = economyCanonicalPublicJwk(privateJwk);
    const { address } = await economyWalletAddressFromPublicJwk(publicJwk);
    economyAssertOperationCurrent(operationContext);
    const privateKey = await crypto.subtle.importKey(
      "jwk",
      privateJwk,
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["sign"]
    );
    economyAssertOperationCurrent(operationContext);
    return {
      address,
      privateKey,
      publicJwk,
      imported,
    };
  } finally {
    economyScrubPrivateJwk(privateJwk);
  }
}

async function economySha256Hex(text) {
  const data = new TextEncoder().encode(String(text || ""));
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function economyWalletAddressFromPublicJwk(publicJwk) {
  const canonical = JSON.stringify(economyCanonicalPublicJwk(publicJwk));
  const publicKeyHash = await economySha256Hex(canonical);
  const addressHash = await economySha256Hex(publicKeyHash);
  return { address: `pc1${addressHash.slice(0, 48)}`, publicKeyHash };
}

function economyWalletBindingPayload({ address, publicKeyHash, walletType }) {
  return JSON.stringify({
    action: "points_wallet_bind",
    address,
    key_algorithm: "ECDSA_P256_SHA256",
    public_key_hash: publicKeyHash,
    user_id: Number(currentUserId || 0),
    wallet_type: walletType,
  });
}

function economyWalletSignerKeyId(address) {
  const wallet = economyWalletByAddress(address);
  return String(wallet?.public_key_hash || "").trim();
}

function economyWalletTransferPayload({ source, destination, amount, fee, memo, requestUuid, chainBranch = "", actionType = "points_wallet_transfer", proposalId = "", payloadHash = "", signerKeyId = "" }) {
  return JSON.stringify({
    action: String(actionType || "points_wallet_transfer").trim() || "points_wallet_transfer",
    amount_points: Number(amount),
    chain_branch: String(chainBranch || economyCurrentChainBranch || "main").trim() || "main",
    destination_wallet_address: String(destination || "").trim().toLowerCase(),
    fee_points: Number(fee),
    memo: String(memo || "").slice(0, 240),
    payload_hash: String(payloadHash || "").trim(),
    proposal_id: String(proposalId || "").slice(0, 120),
    request_uuid: String(requestUuid || "").slice(0, 120),
    signer_key_id: String(signerKeyId || economyWalletSignerKeyId(source) || "").slice(0, 120),
    source_wallet_address: String(source || "").trim().toLowerCase(),
    user_id: Number(currentUserId || 0),
  });
}

function economyWalletServiceFeePayload({ source, itemKey, quantity, amount, requestUuid, referenceType, referenceId, chainBranch = "", actionType = "points_service_fee_payment", proposalId = "", payloadHash = "", signerKeyId = "" }) {
  return JSON.stringify({
    action: String(actionType || "points_service_fee_payment").trim() || "points_service_fee_payment",
    amount_points: Number(amount),
    chain_branch: String(chainBranch || economyCurrentChainBranch || "main").trim() || "main",
    item_key: String(itemKey || "").trim(),
    payload_hash: String(payloadHash || "").trim(),
    proposal_id: String(proposalId || "").slice(0, 120),
    quantity: Number(quantity || 1),
    reference_id: String(referenceId || "").slice(0, 240),
    reference_type: String(referenceType || "").slice(0, 120),
    request_uuid: String(requestUuid || "").slice(0, 120),
    signer_key_id: String(signerKeyId || economyWalletSignerKeyId(source) || "").slice(0, 120),
    source_wallet_address: String(source || "").trim().toLowerCase(),
    user_id: Number(currentUserId || 0),
  });
}

function economyAddressDisputeRuntimeMode() {
  return String(siteConfig?.server_mode || "unknown").trim() || "unknown";
}

function economyAddressDisputePayload({ purpose, txHash, from, to, amount, statementHash, evidenceHash, nonce, chainBranch, runtimeMode }) {
  return JSON.stringify({
    amount: Number(amount || 0),
    amount_points: Number(amount || 0),
    chain_branch: String(chainBranch || economyCurrentChainBranch || "main").trim() || "main",
    evidence_hash: String(evidenceHash || "").trim(),
    from: String(from || "").trim().toLowerCase(),
    from_wallet_address: String(from || "").trim().toLowerCase(),
    nonce: String(nonce || "").trim(),
    purpose: String(purpose || "").trim(),
    runtime_mode: String(runtimeMode || economyAddressDisputeRuntimeMode()).trim() || "unknown",
    statement_hash: String(statementHash || "").trim(),
    to: String(to || "").trim().toLowerCase(),
    to_wallet_address: String(to || "").trim().toLowerCase(),
    tx_hash: String(txHash || "").trim(),
  });
}

function economyNormalizeEvidenceInput(raw) {
  if (Array.isArray(raw)) {
    return raw.map((item) => String(item || "").trim()).filter(Boolean).slice(0, 20).map((item) => item.slice(0, 240));
  }
  return String(raw || "")
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 20)
    .map((item) => item.slice(0, 240));
}

async function economyBuildAddressDisputeProof({ purpose, signerAddress, txHash, from, to, amount, statement, evidence, chainBranch, runtimeMode }) {
  const operation = economyOperationContext();
  economyAssertOperationCurrent(operation);
  if (!window.crypto?.subtle) throw new Error("此瀏覽器不支援 WebCrypto，無法本機簽署地址證明");
  let loaded = null;
  try {
    loaded = await economyPromptColdWalletForSigning({
      expectedAddress: signerAddress,
      purposeLabel: "地址證明",
      cancelMessage: "已取消地址證明簽署，疑義案件未送出",
      mismatchMessage: "冷錢包檔地址與本次需證明的地址不一致",
      operation,
    });
    const statementHash = await economySha256Hex(String(statement || ""));
    economyAssertOperationCurrent(operation);
    const evidenceHash = await economySha256Hex(JSON.stringify(economyNormalizeEvidenceInput(evidence)));
    economyAssertOperationCurrent(operation);
    const nonce = window.crypto?.randomUUID ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    const signatureRuntimeMode = String(runtimeMode || economyAddressDisputeRuntimeMode()).trim() || "unknown";
    const payload = economyAddressDisputePayload({
      purpose,
      txHash,
      from,
      to,
      amount,
      statementHash,
      evidenceHash,
      nonce,
      chainBranch,
      runtimeMode: signatureRuntimeMode,
    });
    return {
      public_key_jwk: economyCanonicalPublicJwk(loaded.publicJwk),
      signature: await economySignWalletBinding(loaded.privateKey, payload, operation),
      signature_nonce: nonce,
      signature_runtime_mode: signatureRuntimeMode,
      statement_hash: statementHash,
      evidence_hash: evidenceHash,
    };
  } finally {
    loaded = null;
  }
}

async function economySignWalletBinding(privateKey, payload, operation = economyOperationContext()) {
  economyAssertOperationCurrent(operation);
  const signature = await crypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" },
    privateKey,
    new TextEncoder().encode(payload)
  );
  economyAssertOperationCurrent(operation);
  return economyBase64UrlFromBytes(signature);
}

async function economyBuildWalletBindPayload({ privateKey, publicJwk, walletType, operation = null }) {
  const operationContext = operation || economyOperationContext();
  economyAssertOperationCurrent(operationContext);
  const { address, publicKeyHash } = await economyWalletAddressFromPublicJwk(publicJwk);
  economyAssertOperationCurrent(operationContext);
  const payload = economyWalletBindingPayload({ address, publicKeyHash, walletType });
  const signature = await economySignWalletBinding(privateKey, payload, operationContext);
  return {
    mode: walletType,
    address,
    public_key_jwk: economyCanonicalPublicJwk(publicJwk),
    signature,
    backup_confirmed: true,
  };
}

function economyWalletByAddress(address) {
  const needle = String(address || "").trim().toLowerCase();
  if (!needle) return null;
  return economyVisibleWallets(economyWalletOnboardingState).find((wallet) => String(wallet.address || "").trim().toLowerCase() === needle) || null;
}

function economyWalletRequiresSignature(wallet) {
  const type = String(wallet?.wallet_type || "");
  const mode = String(wallet?.custody_mode || "");
  return mode === "self_custody" || ["self_custody_cold", "imported_cold"].includes(type);
}

function economyColdWalletSigningSessionKey(address) {
  return String(address || "").trim().toLowerCase();
}

function economyForgetColdWalletSigningSession(address = "") {
  if (!address) {
    economyColdWalletSigningSessions = new Map();
    return;
  }
  economyColdWalletSigningSessions.delete(economyColdWalletSigningSessionKey(address));
}

function economyRememberColdWalletSigningSession(loaded, unlockSecret) {
  const address = economyColdWalletSigningSessionKey(loaded?.address || "");
  const words = economyColdWalletMnemonicWords(unlockSecret);
  if (!address || !loaded?.privateKey || !loaded?.publicJwk || words.length < ECONOMY_COLD_UNLOCK_QUIZ_COUNT) return;
  economyColdWalletSigningSessions.set(address, {
    address,
    privateKey: loaded.privateKey,
    publicJwk: economyCanonicalPublicJwk(loaded.publicJwk),
    mnemonicWords: words,
    expiresAt: Date.now() + ECONOMY_COLD_WALLET_SIGNING_SESSION_MS,
  });
}

function economyColdWalletSigningSession(address) {
  const key = economyColdWalletSigningSessionKey(address);
  const session = economyColdWalletSigningSessions.get(key);
  if (!session) return null;
  if (Number(session.expiresAt || 0) <= Date.now()) {
    economyColdWalletSigningSessions.delete(key);
    return null;
  }
  return session;
}

async function economyVerifyColdWalletSigningSession(session, { purposeLabel = "", cancelMessage = "", operation = null } = {}) {
  const operationContext = operation || economyOperationContext();
  economyAssertOperationCurrent(operationContext);
  const quiz = economyBuildColdWalletMnemonicQuiz(session?.mnemonicWords || []);
  if (!quiz.length) {
    economyForgetColdWalletSigningSession(session?.address || "");
    return false;
  }
  const promptText = [
    `本機已暫時解鎖此冷錢包；簽署${purposeLabel || "交易"}前只需回答隨機助記詞挑戰。`,
    `請依序輸入：${quiz.map((item) => `第 ${Number(item.index) + 1} 詞`).join("、")}。`,
    "不需要輸入完整解鎖助記詞；答案只在本機比對，不會送到伺服器。",
  ].join("\n");
  const raw = window.prompt(promptText, "");
  economyAssertOperationCurrent(operationContext);
  if (raw === null) throw economyColdWalletSigningCancelled(cancelMessage);
  const answers = String(raw || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
  const ok = quiz.every((item, index) => answers[index] === String(item.word || "").toLowerCase());
  if (!ok) {
    economyForgetColdWalletSigningSession(session?.address || "");
    throw new Error("冷錢包隨機助記詞挑戰未通過；本機暫存解鎖已清除，請重新選擇錢包檔後再試。");
  }
  session.expiresAt = Date.now() + ECONOMY_COLD_WALLET_SIGNING_SESSION_MS;
  return true;
}

function economyWalletSupportsAccountBoundDisputeProof(wallet) {
  return String(wallet?.wallet_type || "") === "official_hot"
    && String(wallet?.custody_mode || "") === "server_hot";
}

function economyColdWalletSigningCancelled(cancelMessage) {
  const err = new Error(cancelMessage || "已取消冷錢包簽署");
  err.cancelled = true;
  return err;
}

async function economyPickColdWalletFileForSigning({ cancelMessage = "", operation = null } = {}) {
  const operationContext = operation || economyOperationContext();
  economyAssertOperationCurrent(operationContext);
  if (window.showOpenFilePicker && window.isSecureContext) {
    try {
      const handles = await window.showOpenFilePicker({
        multiple: false,
        types: [{
          description: "Hackme 冷錢包檔",
          accept: { "application/json": [".pcw1.json", ".json"] },
        }],
      });
      economyAssertOperationCurrent(operationContext);
      const file = await handles?.[0]?.getFile();
      economyAssertOperationCurrent(operationContext);
      if (file) return file;
    } catch (err) {
      if (!economyOperationIsCurrent(operationContext) || err?.name === "AbortError") {
        throw economyAccountAbortError();
      }
      throw economyColdWalletSigningCancelled(cancelMessage);
    }
    throw economyColdWalletSigningCancelled(cancelMessage);
  }
  return new Promise((resolve, reject) => {
    const input = $("economy-wallet-signing-file-input") || document.createElement("input");
    const created = !input.id;
    let settled = false;
    if (created) {
      input.type = "file";
      input.accept = ".pcw1.json,application/json,.json";
      input.style.display = "none";
      document.body.appendChild(input);
    }
    const cleanup = () => {
      input.removeEventListener("change", onChange);
      input.removeEventListener("cancel", onCancel);
      economyPendingFilePickerCancels.delete(cancelForAccountChange);
      if (created) input.remove();
    };
    const finish = (file) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (!economyOperationIsCurrent(operationContext)) {
        reject(economyAccountAbortError());
        return;
      }
      if (file) resolve(file);
      else reject(economyColdWalletSigningCancelled(cancelMessage));
    };
    function onChange() {
      finish(input.files?.[0] || null);
    }
    function onCancel() {
      finish(null);
    }
    function cancelForAccountChange() {
      if (settled) return;
      settled = true;
      cleanup();
      reject(economyAccountAbortError());
    }
    input.addEventListener("change", onChange, { once: true });
    input.addEventListener("cancel", onCancel, { once: true });
    economyPendingFilePickerCancels.add(cancelForAccountChange);
    input.value = "";
    input.click();
  });
}

async function economyPromptColdWalletForSigning({ expectedAddress, purposeLabel, cancelMessage, mismatchMessage, operation = null }) {
  const operationContext = operation || economyOperationContext();
  economyAssertOperationCurrent(operationContext);
  const normalizedExpected = String(expectedAddress || "").trim().toLowerCase();
  const session = economyColdWalletSigningSession(normalizedExpected);
  if (session) {
    await economyVerifyColdWalletSigningSession(session, { purposeLabel, cancelMessage, operation: operationContext });
    economyAssertOperationCurrent(operationContext);
    return {
      address: session.address,
      privateKey: session.privateKey,
      publicJwk: economyCanonicalPublicJwk(session.publicJwk),
      imported: true,
      fromSession: true,
    };
  }
  let loaded = null;
  const file = await economyPickColdWalletFileForSigning({ cancelMessage, operation: operationContext });
  economyAssertOperationCurrent(operationContext);
  const unlockMnemonic = window.prompt(`首次解鎖或本機簽署會話逾期時，需輸入完整冷錢包解鎖助記詞以本機解密錢包檔；之後同一頁面短時間內簽署${purposeLabel || "交易"}只會隨機詢問幾個詞。助記詞不會送到伺服器。`, "");
  economyAssertOperationCurrent(operationContext);
  if (unlockMnemonic === null) {
    throw economyColdWalletSigningCancelled(cancelMessage);
  }
  const raw = await economyReadTextFile(file, operationContext);
  economyAssertOperationCurrent(operationContext);
  loaded = await economyLoadEncryptedColdWalletFile(raw, unlockMnemonic, {
    imported: true,
    operation: operationContext,
  });
  economyAssertOperationCurrent(operationContext);
  if (normalizedExpected && String(loaded?.address || "").trim().toLowerCase() !== normalizedExpected) {
    throw new Error(mismatchMessage || "冷錢包檔地址與本次付款錢包不一致");
  }
  economyAssertOperationCurrent(operationContext);
  economyRememberColdWalletSigningSession(loaded, unlockMnemonic);
  return loaded;
}

async function economyBuildTransferSignature({ source, destination, amount, fee, memo, requestUuid }) {
  const operation = economyOperationContext();
  economyAssertOperationCurrent(operation);
  const wallet = economyWalletByAddress(source);
  if (!economyWalletRequiresSignature(wallet)) return "";
  if (!window.crypto?.subtle) throw new Error("此瀏覽器不支援 WebCrypto，無法簽署冷錢包交易");
  let loaded = null;
  try {
    loaded = await economyPromptColdWalletForSigning({
      expectedAddress: source,
      purposeLabel: "交易",
      cancelMessage: "已取消冷錢包簽署，交易未送出",
      mismatchMessage: "冷錢包檔地址與付款錢包不一致，交易未送出",
      operation,
    });
    economyAssertOperationCurrent(operation);
    const payload = economyWalletTransferPayload({ source, destination, amount, fee, memo, requestUuid });
    return await economySignWalletBinding(loaded.privateKey, payload, operation);
  } finally {
    loaded = null;
  }
}

async function economyBuildGovernanceMultisigSignature({ signer, destination, amount, payloadHash, requestUuid, custodyMode = "", walletType = "" }) {
  const operation = economyOperationContext();
  economyAssertOperationCurrent(operation);
  const wallet = economyWalletByAddress(signer);
  const needsSignature = economyWalletRequiresSignature(wallet)
    || String(custodyMode || "").trim() === "self_custody"
    || ["self_custody_cold", "imported_cold"].includes(String(walletType || "").trim());
  if (!needsSignature) return "";
  if (!window.crypto?.subtle) throw new Error("此瀏覽器不支援 WebCrypto，無法簽署官方多簽");
  let loaded = null;
  try {
    loaded = await economyPromptColdWalletForSigning({
      expectedAddress: signer,
      purposeLabel: "官方多簽",
      cancelMessage: "已取消官方多簽簽署，提案尚未授權",
      mismatchMessage: "冷錢包檔地址與 signer 錢包不一致，多簽未送出",
      operation,
    });
    economyAssertOperationCurrent(operation);
    const payload = economyWalletTransferPayload({
      source: signer,
      destination: destination || signer,
      amount: Math.max(1, Math.floor(Number(amount || 1))),
      fee: 0,
      memo: payloadHash,
      requestUuid,
      actionType: "points_governance_multisig_sign",
      proposalId: requestUuid,
      payloadHash,
      signerKeyId: economyWalletSignerKeyId(signer),
    });
    return await economySignWalletBinding(loaded.privateKey, payload, operation);
  } finally {
    loaded = null;
  }
}

async function economyBuildServiceFeeSignature({ source, itemKey, quantity, amount, requestUuid, referenceType, referenceId, operation = null }) {
  const operationContext = operation || economyOperationContext();
  economyAssertOperationCurrent(operationContext);
  const wallet = economyWalletByAddress(source);
  if (!economyWalletRequiresSignature(wallet)) return "";
  if (!window.crypto?.subtle) throw new Error("此瀏覽器不支援 WebCrypto，無法簽署冷錢包服務費");
  let loaded = null;
  try {
    loaded = await economyPromptColdWalletForSigning({
      expectedAddress: source,
      purposeLabel: "服務費",
      cancelMessage: "已取消冷錢包簽署，服務費未送出",
      mismatchMessage: "冷錢包檔地址與付款錢包不一致，服務費未送出",
      operation: operationContext,
    });
    economyAssertOperationCurrent(operationContext);
    const payload = economyWalletServiceFeePayload({ source, itemKey, quantity, amount, requestUuid, referenceType, referenceId });
    return await economySignWalletBinding(loaded.privateKey, payload, operationContext);
  } finally {
    loaded = null;
  }
}

function formatPointsCurrency(currency) {
  return "點";
}

function formatEconomyPointsValue(value) {
  if (typeof formatTradingPointsValue === "function") return formatTradingPointsValue(value);
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function formatEconomyPercentValue(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "-";
  return `${number.toLocaleString(undefined, { maximumFractionDigits: 4 })}%`;
}

function economyDisplayMarketSymbol(symbol) {
  if (typeof tradingDisplaySymbol === "function") return tradingDisplaySymbol(symbol);
  return String(symbol || "").toUpperCase().replace("/POINTS", "/USDT");
}

function formatEconomyQuantityValue(value) {
  if (typeof formatTradingQuantityValue === "function") return formatTradingQuantityValue(value);
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString(undefined, { maximumFractionDigits: 8 });
}

function formatEconomyLedgerAction(actionType) {
  const action = String(actionType || "");
  const labels = {
    admin_adjust_credit: "管理員加點",
    admin_adjust_debit: "管理員扣點",
    admin_initial_grant: "管理員初始配點",
    admin_weekly_salary: "管理員週薪",
    game_daily_challenge_reward: "遊戲每日任務獎勵",
    game_weekly_leaderboard_reward: "遊戲週排行榜獎勵",
    trading_bot_weekly_competition_reward: "交易機器人週賽獎勵",
    new_user_signup_bonus: "註冊獎勵",
    birthday_gift: "生日禮金",
    official_wallet_grant: "官方 Treasury 撥款",
    user_initial_grant: "會員初始配點",
    chain_acceleration_fee: "鏈上加速費用",
    wallet_transfer: "鏈上轉帳",
    wallet_transfer_out: "鏈上轉帳",
    wallet_transfer_in: "鏈上轉帳入帳",
    wallet_transfer_fee: "鏈上手續費",
    service_fee_batch_unfreeze: "舊版站內服務費批次解凍",
    service_fee_batch_debit: "舊版站內服務費批次扣款",
  };
  if (labels[action]) return labels[action];
  if (action.startsWith("service_fee_reserve:")) return "舊版站內服務費凍結";
  if (action.startsWith("service_fee_internal_debit:")) return "站內服務費即時扣款";
  if (action.startsWith("compensation:")) return `補償交易：${formatEconomyLedgerAction(action.slice("compensation:".length))}`;
  if (action.startsWith("rollback:")) return `Rollback：${formatEconomyLedgerAction(action.slice("rollback:".length))}`;
  return action || "-";
}

function formatEconomyLedgerAmount(row) {
  const direction = String(row?.direction || "");
  const amount = Number(row?.amount || 0);
  const currency = formatPointsCurrency(row?.currency_type);
  if (direction === "credit" || direction === "transfer_in") return `收入 +${amount} ${currency}`;
  if (direction === "debit" || direction === "transfer_out" || direction === "reverse") return `支出 -${amount} ${currency}`;
  if (direction === "freeze") return `凍結 ${amount} ${currency}`;
  if (direction === "unfreeze") return `解凍 ${amount} ${currency}`;
  return `${direction || "異動"} ${amount} ${currency}`;
}

function formatEconomyLedgerSource(row) {
  const meta = row?.public_metadata && typeof row.public_metadata === "object" ? row.public_metadata : {};
  const parts = [];
  if (row?.reason) parts.push(row.reason);
  if (meta.game_key) parts.push(`遊戲：${meta.game_key}`);
  if (meta.difficulty) parts.push(`難度：${meta.difficulty}`);
  if (meta.score !== undefined && meta.score !== null && meta.score !== "") parts.push(`分數：${meta.score}`);
  if (row?.reference_id) parts.push(`參照：${row.reference_id}`);
  return parts.join(" · ");
}

function shortEconomyWalletAddress(value) {
  const text = String(value || "").trim();
  if (!text || text === "-") return "-";
  if (text.length <= 24) return text;
  return `${text.slice(0, 12)}…${text.slice(-8)}`;
}

function economyCanSeeOfficialHotWalletLabels() {
  return currentUser === "root" || currentRole === "manager" || currentRole === "super_admin";
}

function updateEconomyOfficialHotWalletLabels(labels = {}) {
  if (!economyCanSeeOfficialHotWalletLabels()) {
    economyOfficialHotWalletLabels = {};
    return;
  }
  if (!labels || typeof labels !== "object") return;
  Object.entries(labels).forEach(([address, label]) => {
    const key = String(address || "").trim().toLowerCase();
    const value = String(label || "").trim();
    if (key && value) economyOfficialHotWalletLabels[key] = value;
  });
}

function formatEconomyWalletAddressWithManagerLabel(address, { short = true } = {}) {
  const raw = String(address || "").trim();
  if (!raw || raw === "-") return "-";
  const base = short ? shortEconomyWalletAddress(raw) : raw;
  if (!economyCanSeeOfficialHotWalletLabels()) return base;
  const label = economyOfficialHotWalletLabels[String(raw).trim().toLowerCase()] || "";
  return label ? `${base}（${label}）` : base;
}

function formatEconomyLedgerWalletFlow(row) {
  const flow = row?.wallet_flow && typeof row.wallet_flow === "object" ? row.wallet_flow : null;
  if (!flow?.source_wallet_address && !flow?.destination_wallet_address) return "";
  const sourceLabel = flow.source_label || "來源地址";
  const destLabel = flow.destination_label || "目的地址";
  const source = formatEconomyWalletAddressWithManagerLabel(flow.source_wallet_address);
  const dest = formatEconomyWalletAddressWithManagerLabel(flow.destination_wallet_address);
  if (flow.internal_movement) return `地址流：${sourceLabel} ${source} 內部異動`;
  return `地址流：${sourceLabel} ${source} → ${destLabel} ${dest}`;
}

function formatEconomyCountdown(seconds) {
  const safe = Math.max(0, Number(seconds || 0));
  const minutes = Math.floor(safe / 60);
  const rest = safe % 60;
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function stopEconomyBlockCountdown() {
  if (economyBlockCountdownTimer) {
    clearInterval(economyBlockCountdownTimer);
    economyBlockCountdownTimer = null;
  }
}

function economyChainEnabled() {
  return !siteConfig || siteConfig.feature_points_chain_enabled !== false;
}

function canManageEconomyPoints() {
  return economyChainEnabled() && economyGovernanceCanManage();
}

function setEconomyActivePage(page, options = {}) {
  const rootMode = currentUser === "root";
  const chainFeatureOn = economyChainEnabled();
  const chainAllowed = canManageEconomyPoints();
  const requestedPage = ["chain", "transactions", "explorer", "governance"].includes(page) ? page : "balance";
  const nextPage =
    requestedPage === "chain" && chainAllowed
      ? "chain"
      : requestedPage === "transactions" && chainFeatureOn
        ? "transactions"
      : requestedPage === "explorer" && chainFeatureOn
        ? "explorer"
      : requestedPage === "governance" && chainFeatureOn
        ? "governance"
      : "balance";
  economyActivePage = nextPage;
  if (options.persist !== false) {
    try {
      localStorage.setItem(economyPageStorageKey(), nextPage);
    } catch (_) {}
  }
  const balancePage = $("economy-balance-page");
  const transactionsPage = $("economy-transactions-page");
  const explorerPage = $("economy-explorer-page");
  const governancePage = $("economy-governance-page");
  const chainPage = $("economy-chain-page");
  if (balancePage) balancePage.classList.toggle("active", nextPage === "balance");
  if (transactionsPage) transactionsPage.classList.toggle("active", nextPage === "transactions");
  if (explorerPage) explorerPage.classList.toggle("active", nextPage === "explorer");
  if (governancePage) governancePage.classList.toggle("active", nextPage === "governance");
  if (chainPage) chainPage.classList.toggle("active", chainAllowed && nextPage === "chain");
  const balanceTab = $("tab-economy-balance");
  const transactionsTab = $("tab-economy-transactions");
  const explorerTab = $("tab-economy-explorer");
  const governanceTab = $("tab-economy-governance");
  const chainTab = $("tab-economy-chain");
  if (balanceTab) {
    balanceTab.textContent = rootMode ? "官方錢包管理" : "積分餘額";
    balanceTab.classList.toggle("active", nextPage === "balance");
    balanceTab.setAttribute("aria-selected", nextPage === "balance" ? "true" : "false");
  }
  if (transactionsTab) {
    transactionsTab.style.display = chainFeatureOn ? "" : "none";
    transactionsTab.classList.toggle("active", nextPage === "transactions");
    transactionsTab.setAttribute("aria-selected", nextPage === "transactions" ? "true" : "false");
  }
  if (explorerTab) {
    explorerTab.style.display = chainFeatureOn ? "" : "none";
    explorerTab.classList.toggle("active", nextPage === "explorer");
    explorerTab.setAttribute("aria-selected", nextPage === "explorer" ? "true" : "false");
  }
  if (governanceTab) {
    governanceTab.style.display = chainFeatureOn ? "" : "none";
    governanceTab.classList.toggle("active", nextPage === "governance");
    governanceTab.setAttribute("aria-selected", nextPage === "governance" ? "true" : "false");
  }
  if (chainTab) {
    chainTab.style.display = chainAllowed ? "" : "none";
    chainTab.textContent = rootMode ? "積分私有鏈" : "官方錢包管理";
    chainTab.classList.toggle("active", chainAllowed && nextPage === "chain");
    chainTab.setAttribute("aria-selected", chainAllowed && nextPage === "chain" ? "true" : "false");
  }
  const title = $("economy-page-title");
  if (title) {
    if (nextPage === "transactions") title.textContent = "鏈上交易";
    else if (nextPage === "explorer") title.textContent = "鏈上瀏覽器";
    else if (nextPage === "governance") title.textContent = "公共投票與疑義事件";
    else if (!rootMode) title.textContent = nextPage === "chain" ? "官方錢包管理" : "積分錢包";
    else title.textContent = nextPage === "chain" ? "積分私有鏈" : "官方錢包管理";
  }
  if (nextPage === "governance") {
    loadEconomyGovernance({ silent: true });
    loadEconomyTransactionDisputes({ silent: true });
  }
}

function syncEconomySubpages(rootMode) {
  if (!canManageEconomyPoints() && economyActivePage === "chain") economyActivePage = "balance";
  if (!economyChainEnabled() && ["transactions", "explorer", "governance", "chain"].includes(economyActivePage)) economyActivePage = "balance";
  if (["positions", "funding-pools", "all-positions"].includes(economyActivePage)) economyActivePage = "balance";
  setEconomyActivePage(economyActivePage, { persist: false });
}

function relocateEconomyOfficialWalletCard(rootMode) {
  const card = $("economy-root-wallet-management-card");
  if (!card) return;
  const balancePage = $("economy-balance-page");
  const chainPage = $("economy-chain-page");
  if (rootMode) {
    const pricingCard = $("economy-root-pricing-settings-card");
    const ledgerCard = $("economy-user-ledger-card");
    if (balancePage && card.parentElement !== balancePage) {
      balancePage.insertBefore(card, pricingCard || ledgerCard || null);
    }
    return;
  }
  if (economyGovernanceCanManage() && chainPage && card.parentElement !== chainPage) {
    const managerCard = $("economy-manager-points-management-card");
    chainPage.insertBefore(card, managerCard || chainPage.firstChild);
  }
}

function setEconomyRootLayout(rootMode) {
  const chainFeatureOn = economyChainEnabled();
  const rootWalletManagementCard = $("economy-root-wallet-management-card");
  relocateEconomyOfficialWalletCard(rootMode);
  if (rootWalletManagementCard) rootWalletManagementCard.style.display = economyGovernanceCanManage() && chainFeatureOn ? "" : "none";
  const pricingSettingsCard = $("economy-root-pricing-settings-card");
  if (pricingSettingsCard) pricingSettingsCard.style.display = rootMode ? "" : "none";
}

function updateEconomyBlockCountdown() {
  const el = $("economy-chain-countdown");
  if (!el || !economyBlockSchedule) return;
  const unsealed = Number(economyBlockSchedule.unsealed_entries || 0);
  const threshold = Number(economyBlockSchedule.ledger_threshold || 10);
  if (economyBlockSchedule.mode === "hybrid" || economyBlockSchedule.mode === "ledger_count") {
    const remainingEntries = Math.max(0, threshold - unsealed);
    const target = economyBlockSchedule.nextSealAtMs || 0;
    const remainingSeconds = target ? Math.max(0, Math.ceil((target - Date.now()) / 1000)) : null;
    if (!unsealed) {
      el.textContent = `封塊進度：目前沒有未封 ledger；累積 ${threshold} 筆或最長等待 ${economyBlockSchedule.max_interval_minutes || "-"} 分鐘自動封塊`;
    } else if (remainingEntries) {
      const timeText = remainingSeconds === null ? "" : `，時間還剩 ${formatEconomyCountdown(remainingSeconds)}`;
      el.textContent = `封塊進度：${unsealed}/${threshold} 筆，還差 ${remainingEntries} 筆${timeText}`;
    } else {
      el.textContent = `封塊進度：${unsealed}/${threshold} 筆，可自動封塊`;
    }
    return;
  }
  const interval = Number(economyBlockSchedule.interval_minutes || 0);
  if (!unsealed) {
    el.textContent = `封塊倒數：目前沒有未封 ledger；設定為每 ${interval || "-"} 分鐘封塊一次`;
    return;
  }
  const target = economyBlockSchedule.nextSealAtMs || 0;
  const remaining = Math.max(0, Math.ceil((target - Date.now()) / 1000));
  el.textContent = remaining
    ? `封塊倒數：${formatEconomyCountdown(remaining)}（每 ${interval || "-"} 分鐘封塊一次）`
    : `封塊倒數：可封塊（每 ${interval || "-"} 分鐘封塊一次）`;
}

function startEconomyBlockCountdown(schedule) {
  stopEconomyBlockCountdown();
  economyBlockSchedule = null;
  if (!schedule) {
    const el = $("economy-chain-countdown");
    if (el) el.textContent = "封塊進度：-";
    return;
  }
  const nextMs = schedule.next_seal_at ? Date.parse(schedule.next_seal_at) : 0;
  economyBlockSchedule = { ...schedule, nextSealAtMs: Number.isFinite(nextMs) ? nextMs : 0 };
  updateEconomyBlockCountdown();
  economyBlockCountdownTimer = setInterval(updateEconomyBlockCountdown, 1000);
}

function renderEconomyWallet(wallet) {
  if (!wallet) return;
  economyCurrentChainBranch = String(wallet.chain_branch || wallet.branch?.branch_uuid || economyCurrentChainBranch || "main").trim() || "main";
  const pointsBalance = wallet.account_points_balance !== undefined
    ? Number(wallet.account_points_balance || 0)
    : wallet.points_balance !== undefined
    ? Number(wallet.points_balance || 0)
    : Number(wallet.soft_balance || 0) + Number(wallet.hard_balance || 0);
  const pointsFrozen = wallet.account_points_frozen !== undefined
    ? Number(wallet.account_points_frozen || 0)
    : wallet.points_frozen !== undefined
    ? Number(wallet.points_frozen || 0)
    : Number(wallet.soft_frozen || 0) + Number(wallet.hard_frozen || 0);
  const pointsEarned = wallet.total_points_earned !== undefined
    ? Number(wallet.total_points_earned || 0)
    : Number(wallet.total_soft_earned || 0) + Number(wallet.total_hard_earned || 0);
  const pointsSpent = wallet.total_points_spent !== undefined
    ? Number(wallet.total_points_spent || 0)
    : Number(wallet.total_soft_spent || 0) + Number(wallet.total_hard_spent || 0);
  if ($("economy-points-balance")) $("economy-points-balance").textContent = String(pointsBalance);
  if ($("economy-points-frozen")) $("economy-points-frozen").textContent = `金額凍結 ${pointsFrozen}`;
  if ($("economy-points-earned")) $("economy-points-earned").textContent = `收入 ${pointsEarned}`;
  if ($("economy-points-spent")) $("economy-points-spent").textContent = `支出 ${pointsSpent}`;
  if ($("economy-soft-balance")) $("economy-soft-balance").textContent = String(pointsBalance);
  if ($("economy-hard-balance")) $("economy-hard-balance").textContent = "0";
  if ($("economy-soft-frozen")) $("economy-soft-frozen").textContent = `金額凍結 ${pointsFrozen}`;
  if ($("economy-hard-frozen")) $("economy-hard-frozen").textContent = "金額凍結 0";
  if ($("economy-wallet-status")) $("economy-wallet-status").textContent = wallet.wallet_status || "-";
  if ($("economy-public-account")) {
    const accountId = wallet.public_account_id || "";
    $("economy-public-account").textContent = accountId ? `Legacy 帳本 ID：${shortEconomyWalletAddress(accountId)}` : "Legacy 帳本 ID：-";
    $("economy-public-account").title = accountId;
  }
  const sidebarPoints = $("sidebar-points");
  if (sidebarPoints) {
    sidebarPoints.dataset.points = String(pointsBalance);
    updateSidebarIdentity();
  }
}

function formatEconomyWalletIdentityType(type) {
  const labels = {
    official_hot: "站內託管錢包",
    self_custody_cold: "自管冷錢包",
    imported_cold: "匯入冷錢包",
    multisig: "多簽錢包",
    user_multisig_preview: "多簽錢包（觀察/收款）",
    official_treasury_multisig: "官方財庫多簽",
    mint: "Mint 錢包",
    burn: "Burn 錢包",
  };
  return labels[String(type || "")] || String(type || "-");
}

function renderEconomyWalletOnboarding(onboarding) {
  const card = $("economy-wallet-onboarding-card");
  if (!card) return;
  economyWalletOnboardingState = onboarding || {};
  const rootMode = currentUser === "root";
  const chainFeatureOn = economyChainEnabled();
  const createCard = $("economy-wallet-create-card");
  const actionPanel = $("economy-wallet-action-panel");
  if (!chainFeatureOn) {
    card.style.display = "none";
    if (createCard) createCard.style.display = "none";
    if (actionPanel) actionPanel.style.display = "none";
    return;
  }
  card.style.display = rootMode ? "none" : "";
  if (createCard) createCard.style.display = rootMode ? "none" : "";
  if (rootMode) {
    if (createCard) createCard.style.display = "none";
    if (actionPanel) actionPanel.style.display = "none";
    return;
  }
  const wallet = onboarding?.wallet || null;
  const onboardingWarnings = Array.isArray(onboarding?.warnings) ? onboarding.warnings : [];
  renderEconomyWalletCreationFeeOptions(onboarding);
  renderEconomyWalletIdentityList(onboarding);
  const actions = $("economy-wallet-onboarding-actions");
  const boundActions = $("economy-wallet-bound-actions");
  const visibleWallets = economyVisibleWallets(onboarding);
  if (actions) actions.style.display = "";
  if (boundActions) boundActions.style.display = "none";
  if ($("economy-wallet-onboarding-status")) {
    $("economy-wallet-onboarding-status").textContent = wallet
      ? "已綁定模擬鏈錢包；伺服器未保存用戶冷錢包檔或冷錢包解鎖助記詞。"
      : "尚未建立官方熱錢包；按更新即可建立站內託管錢包。";
  }
  if ($("economy-wallet-count")) $("economy-wallet-count").textContent = String(visibleWallets.length || 0);
  if ($("economy-wallet-primary-note")) {
    $("economy-wallet-primary-note").textContent = wallet
      ? `${formatEconomyWalletIdentityType(wallet.wallet_type)} · ${wallet.custody_mode || "-"}`
      : "尚未綁定";
  }
  if ($("economy-wallet-identity-address")) {
    $("economy-wallet-identity-address").textContent = wallet?.address || "-";
    $("economy-wallet-identity-address").title = wallet?.address || "";
  }
  if ($("economy-wallet-identity-status")) $("economy-wallet-identity-status").textContent = wallet?.status || "-";
  if (onboardingWarnings.length) {
    const labels = onboardingWarnings.map((item) => item?.code || item?.message || String(item)).filter(Boolean);
    economyWalletMsg(`錢包資料部分讀取失敗：${labels.join("、")}`, false);
  }
}

function economyWalletOptionLabel(wallet) {
  const primary = wallet.is_primary ? " · primary" : "";
  const balance = wallet.points_balance !== undefined ? ` · ${formatEconomyPointsValue(wallet.points_balance)} 點` : "";
  return `${formatEconomyWalletIdentityType(wallet.wallet_type)}${primary} · ${shortEconomyWalletAddress(wallet.address)}${balance}`;
}

function economyVisibleWallets(onboarding = {}) {
  const walletMap = new Map();
  const addWallet = (wallet) => {
    if (!wallet || typeof wallet !== "object" || !wallet.address) return;
    if (!walletMap.has(wallet.address)) walletMap.set(wallet.address, wallet);
  };
  if (Array.isArray(onboarding.wallets)) onboarding.wallets.forEach(addWallet);
  addWallet(onboarding.wallet);
  return Array.from(walletMap.values())
    .filter((wallet) => {
      const status = String(wallet.status || "");
      return ["pending_backup", "active"].includes(status) && wallet.address;
    });
}

function economyColdWallets(onboarding = {}) {
  return economyVisibleWallets(onboarding).filter((wallet) => {
    const type = String(wallet.wallet_type || "");
    const mode = String(wallet.custody_mode || "");
    return ["self_custody_cold", "imported_cold"].includes(type) && mode !== "system";
  });
}

function economySpendableWallets(onboarding = {}) {
  return economyVisibleWallets(onboarding)
    .filter((wallet) => {
      const status = String(wallet.status || "");
      const mode = String(wallet.custody_mode || "");
      const type = String(wallet.wallet_type || "");
      const spend = String(wallet.spend_capability || "enabled");
      return status === "active" && spend === "enabled" && mode !== "system" && mode !== "multisig" && !["mint", "burn", "user_multisig_preview"].includes(type) && wallet.address;
    });
}

function economyWalletCanSpend(wallet) {
  const status = String(wallet?.status || "");
  const mode = String(wallet?.custody_mode || "");
  const type = String(wallet?.wallet_type || "");
  const spend = String(wallet?.spend_capability || "enabled");
  return status === "active" && spend === "enabled" && mode !== "system" && mode !== "multisig" && !["mint", "burn", "user_multisig_preview"].includes(type);
}

function economyTransferFavoriteAllowed(address, { source = "", rail = "" } = {}) {
  const normalized = economyNormalizeAddress(address);
  const normalizedSource = economyNormalizeAddress(source);
  if (!normalized || normalized === normalizedSource) return false;
  const sourcePc0 = economyIsPc0Address(normalizedSource);
  const destinationPc0 = economyIsPc0Address(normalized);
  if (!sourcePc0 && destinationPc0) return false;
  if (sourcePc0 && rail === "internal_pc0") return destinationPc0;
  if (sourcePc0 && rail === "external_cold") return !destinationPc0;
  return !destinationPc0;
}

function economyFavoriteAddressOptionsHtml({ source = "", rail = "" } = {}) {
  const favorites = readEconomyFavoriteAddresses().filter((item) => economyTransferFavoriteAllowed(item.address, { source, rail }));
  if (!favorites.length) return `<option value="">沒有符合此模式的常用地址</option>`;
  return `<option value="">選擇常用地址</option>` + favorites.map((item) => {
    return `<option value="${sanitize(item.address)}">${sanitize(economyFavoriteAddressLabel(item))}</option>`;
  }).join("");
}

function economyActiveDepositAddress() {
  return String(economyWalletOnboardingState?.deposit_address || "").trim().toLowerCase();
}

function economyWalletActionPanel() {
  return $("economy-wallet-action-panel");
}

function economyWalletActionContent() {
  return $("economy-wallet-action-content");
}

function economyTransferRailFromInputs() {
  return String($("economy-transfer-rail")?.value || "").trim();
}

function economySyncTransferRailFields() {
  const source = economyNormalizeAddress($("economy-transfer-source-wallet")?.value);
  const destination = economyNormalizeAddress($("economy-transfer-destination-wallet")?.value);
  const sourcePc0 = economyIsPc0Address(source);
  const railSelect = $("economy-transfer-rail");
  const rail = sourcePc0 ? economyTransferRailFromInputs() || "internal_pc0" : "cold_chain";
  const feeInput = $("economy-transfer-fee");
  const feeEstimate = $("economy-transfer-fee-estimate");
  const destinationInput = $("economy-transfer-destination-wallet");
  const favoriteSelect = $("economy-transfer-favorite-address");
  if (railSelect) railSelect.value = rail;
  if (destinationInput) {
    destinationInput.placeholder = sourcePc0 && rail === "internal_pc0" ? "pc0..." : "pc1... / 外部地址";
  }
  if (feeInput) {
    const noNetworkFee = sourcePc0 && rail === "internal_pc0";
    feeInput.disabled = noNetworkFee;
    feeInput.value = noNetworkFee ? "0" : String(Math.max(1, Number(feeInput.value || 1)));
    if (feeEstimate) {
      feeEstimate.textContent = noNetworkFee
        ? "pc0 站內互轉不收鏈上 fee、不等待 Proved。"
        : "預估 Proved 時間讀取中...";
    }
  }
  if (favoriteSelect) favoriteSelect.innerHTML = economyFavoriteAddressOptionsHtml({ source, rail });
  if (source && destination && !sourcePc0 && economyIsPc0Address(destination)) {
    economyTransferMsg("pc1 冷錢包不能直接轉到 pc0 站內地址；請改用熱錢包轉入顯示的 pc1 入金地址。", false);
  }
  if (sourcePc0 && rail === "external_cold" && destination && economyIsPc0Address(destination)) {
    economyTransferMsg("外部 / 冷錢包橋接模式不能填 pc0；要站內互轉請切回 pc0 站內地址。", false);
  }
  if (!feeInput?.disabled) scheduleEconomyTransferFeeEstimate();
}

function economyWalletCreationFeeQuote(onboarding = economyWalletOnboardingState) {
  const quote = onboarding?.wallet_creation_fee;
  return quote && typeof quote === "object" ? quote : {};
}

function renderEconomyWalletCreationFeeOptions(onboarding = {}) {
  const panel = $("economy-wallet-creation-fee-panel");
  const select = $("economy-wallet-creation-fee-source");
  const note = $("economy-wallet-creation-fee-note");
  if (!panel || !select) return;
  const quote = economyWalletCreationFeeQuote(onboarding);
  const amount = Number(quote.amount_points || quote.amount || 0);
  const wallets = economySpendableWallets(onboarding);
  if (amount <= 0) {
    panel.style.display = "none";
    select.innerHTML = `<option value="">第一個冷錢包免費</option>`;
    select.disabled = true;
    if (note) note.textContent = "第一個冷錢包免費；第二個以上依數量指數加價，收入進官方 Treasury。";
    return;
  }
  panel.style.display = "";
  select.disabled = !wallets.length;
  const previous = select.value;
  select.innerHTML = wallets.length
    ? wallets.map((wallet) => `<option value="${sanitize(wallet.address)}">${sanitize(economyWalletOptionLabel(wallet))}</option>`).join("")
    : `<option value="">沒有可支付建立費的錢包</option>`;
  if (previous && wallets.some((wallet) => wallet.address === previous)) select.value = previous;
  else if (wallets.some((wallet) => wallet.is_primary)) select.value = wallets.find((wallet) => wallet.is_primary).address;
  if (note) {
    const next = Number(quote.next_wallet_number || 0);
    note.textContent = `第 ${next || wallets.length + 1} 個錢包建立費 ${formatEconomyPointsValue(amount)} 點，收入進官方 Treasury；冷錢包付款會要求本機簽署。`;
  }
}

async function economyWalletCreationFeePayload(mode, operation = null) {
  const operationContext = operation || economyOperationContext();
  economyAssertOperationCurrent(operationContext);
  const quote = economyWalletCreationFeeQuote();
  const amount = Math.floor(Number(quote.amount_points || quote.amount || 0));
  if (!Number.isFinite(amount) || amount <= 0) return {};
  const source = String($("economy-wallet-creation-fee-source")?.value || "").trim().toLowerCase();
  if (!source) throw new Error("建立第二個以上錢包需選擇既有錢包支付建立費");
  const requestUuid = economyRequestId("wallet_creation_fee");
  const itemKey = String(quote.item_key || "wallet_creation_fee");
  const referenceType = String(quote.reference_type || "wallet_identity");
  const referenceId = String(quote.reference_id || `wallet_identity:create:${String(mode || "wallet")}:${quote.next_wallet_number || ""}`);
  economyWalletMsg("等待付款錢包本機簽署建立費；錢包檔與冷錢包解鎖助記詞不會送到伺服器。");
  const signature = await economyBuildServiceFeeSignature({
    source,
    itemKey,
    quantity: 1,
    amount,
    requestUuid,
    referenceType,
    referenceId,
    operation: operationContext,
  });
  economyAssertOperationCurrent(operationContext);
  return {
    fee_source_wallet_address: source,
    fee_request_uuid: requestUuid,
    fee_signature: signature,
    fee_quote_amount: amount,
  };
}

function renderEconomyWalletIdentityList(onboarding = {}) {
  const list = $("economy-wallet-identity-list");
  if (!list) return;
  const wallets = economyVisibleWallets(onboarding);
  if (!wallets.length) {
    list.innerHTML = `<div class="drive-empty">尚無錢包</div>`;
    return;
  }
  const walletRows = wallets.map((wallet) => {
    const address = String(wallet.address || "").trim().toLowerCase();
    const risk = wallet.risk_label && typeof wallet.risk_label === "object" ? wallet.risk_label : null;
    const freeze = wallet.governance_freeze && typeof wallet.governance_freeze === "object" ? wallet.governance_freeze : null;
    const walletType = String(wallet.wallet_type || "");
    const coldWallet = ["self_custody_cold", "imported_cold"].includes(walletType);
    const canSpend = economyWalletCanSpend(wallet);
    const defaultWallet = readEconomyDefaultSpendWalletAddress();
    const isDefaultSpend = address && address === String(defaultWallet || "").trim().toLowerCase();
    const riskLine = risk
      ? `<div class="drive-card-sub" style="color:#ff4f6d;">治理標記：${sanitize(risk.risk_level || risk.label || "risk")} · ${sanitize(risk.reason || "")}</div>`
      : "";
    const freezeLine = freeze
      ? `<div class="drive-card-sub" style="color:#ff4f6d;">${freeze.freeze_type === "provisional" ? "短期審核凍結" : "治理凍結"}：禁止轉出${freeze.expires_at ? ` · 到期 ${sanitize(freeze.expires_at)}` : ""}</div>`
      : "";
    const capability = String(wallet.spend_capability || "enabled");
    const capabilityLine = capability !== "enabled"
      ? `<div class="drive-card-sub" style="color:#f6c56b;">${capability === "receive_only" ? "觀察/收款模式，轉出暫不支援" : "此錢包已停用轉出"}</div>`
      : "";
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(formatEconomyWalletIdentityType(wallet.wallet_type))}${wallet.is_primary ? " · primary" : ""}</strong>
          <div class="drive-card-sub">${sanitize(wallet.status || "-")} · ${sanitize(wallet.wallet_scope || "user")} · ${sanitize(wallet.custody_mode || "-")} · ${sanitize(capability)}${isDefaultSpend ? " · 交易所預設付款" : ""} · ${formatEconomyPointsValue(wallet.points_balance || 0)} 點 · 金額凍結 ${formatEconomyPointsValue(wallet.points_frozen || 0)} 點</div>
          ${capabilityLine}
          ${riskLine}
          ${freezeLine}
          <button class="economy-ledger-hash economy-explorer-address" type="button" data-explorer-query="${sanitize(wallet.address || "")}">${sanitize(wallet.address || "-")}</button>
        </div>
        <div class="drive-file-actions">
          <button class="btn btn-sm" type="button" data-wallet-receive="${sanitize(address)}">轉入</button>
          <button class="btn btn-sm" type="button" data-wallet-send="${sanitize(address)}"${canSpend ? "" : " disabled"}>轉出</button>
          <button class="btn btn-sm" type="button" data-wallet-default="${sanitize(address)}"${capability === "enabled" ? "" : " disabled"}>${isDefaultSpend ? "已預設" : "設為預設"}</button>
          ${coldWallet ? `<button class="btn btn-sm" type="button" data-wallet-secret-check="${sanitize(address)}">密鑰驗證</button>` : ""}
          ${coldWallet ? `<button class="btn btn-danger btn-sm" type="button" data-wallet-delete-cold="${sanitize(address)}">刪除</button>` : ""}
        </div>
      </div>
    `;
  }).join("");
  list.innerHTML = walletRows;
  list.querySelectorAll("[data-explorer-query]").forEach((btn) => {
    if (btn.dataset.explorerBound === "1") return;
    btn.dataset.explorerBound = "1";
    btn.addEventListener("click", () => {
      const query = btn.dataset.explorerQuery || "";
      if ($("economy-explorer-query")) $("economy-explorer-query").value = query;
      setEconomyActivePage("explorer");
      searchEconomyExplorer(query);
    });
  });
  list.querySelectorAll("[data-dispute-tx]").forEach((btn) => {
    if (btn.dataset.disputeBound === "1") return;
    btn.dataset.disputeBound = "1";
    btn.addEventListener("click", () => createEconomyTransactionDispute({
      txHash: btn.dataset.disputeTx || "",
      from: btn.dataset.disputeFrom || "",
      to: btn.dataset.disputeTo || "",
      amount: btn.dataset.disputeAmount || "0",
      chainBranch: btn.dataset.disputeBranch || "",
    }));
  });
  list.querySelectorAll("[data-wallet-receive]").forEach((btn) => {
    if (btn.dataset.walletActionBound === "1") return;
    btn.dataset.walletActionBound = "1";
    btn.addEventListener("click", () => openEconomyWalletReceive(btn.dataset.walletReceive || ""));
  });
  list.querySelectorAll("[data-wallet-send]").forEach((btn) => {
    if (btn.dataset.walletActionBound === "1") return;
    btn.dataset.walletActionBound = "1";
    btn.addEventListener("click", () => openEconomyWalletSend(btn.dataset.walletSend || ""));
  });
  list.querySelectorAll("[data-wallet-default]").forEach((btn) => {
    if (btn.dataset.walletActionBound === "1") return;
    btn.dataset.walletActionBound = "1";
    btn.addEventListener("click", () => setEconomyDefaultWalletFromCard(btn.dataset.walletDefault || ""));
  });
  list.querySelectorAll("[data-wallet-secret-check]").forEach((btn) => {
    if (btn.dataset.walletActionBound === "1") return;
    btn.dataset.walletActionBound = "1";
    btn.addEventListener("click", () => verifyEconomyColdWalletBackupForAddress(btn.dataset.walletSecretCheck || ""));
  });
  list.querySelectorAll("[data-wallet-delete-cold]").forEach((btn) => {
    if (btn.dataset.walletActionBound === "1") return;
    btn.dataset.walletActionBound = "1";
    btn.addEventListener("click", () => deleteEconomyColdWallet(btn.dataset.walletDeleteCold || ""));
  });
}

function economyTransactionDirectionLabel(direction) {
  const value = String(direction || "");
  if (value === "incoming") return "轉入";
  if (value === "outgoing") return "轉出";
  if (value === "self") return "錢包間轉帳";
  if (value === "official_outgoing") return "官方 Treasury 支出";
  if (value === "official_fund_transfer") return "官方基金調撥";
  if (value === "observed") return "鏈上交易";
  return "交易";
}

function economyTransactionStatusLabel(status) {
  const value = String(status || "");
  if (value === "pending") return "Pending";
  if (value === "confirmed") return "Confirmed";
  if (value.startsWith("failed")) return "Failed";
  return value || "-";
}

function economyTransactionFinalityIsInternal(finality = {}) {
  return String(finality.finality_status || "") === "internal_settled"
    || String(finality.finality_simulation || "") === "internal_hot_wallet_ledger_v1"
    || Number(finality.target_proved_count ?? 20) === 0;
}

function economyTransactionProvedText(finality = {}) {
  if (economyTransactionFinalityIsInternal(finality)) return "免 Proved";
  const target = Number(finality.target_proved_count ?? 20);
  const proved = Math.max(0, Math.min(target, Number(finality.proved_count ?? 0)));
  return `${proved}/${target} Proved`;
}

function economyTransactionAmountText(tx = {}) {
  const amount = Number(tx.amount_points || tx.amount || 0);
  const fee = Number(tx.fee_points || 0);
  if (tx.direction === "incoming") return `+${formatEconomyPointsValue(amount)}`;
  if (tx.direction === "official_outgoing") return `Treasury 支出 ${formatEconomyPointsValue(amount + fee)}`;
  if (tx.direction === "official_fund_transfer") return `基金調撥 ${formatEconomyPointsValue(amount + fee)}`;
  if (tx.direction === "outgoing") return `-${formatEconomyPointsValue(amount + fee)}`;
  return formatEconomyPointsValue(amount);
}

function renderEconomyTransferLastResult(txHash, transaction = {}) {
  const wrap = $("economy-transfer-last-result");
  if (!wrap) return;
  if (!txHash) {
    wrap.innerHTML = "";
    return;
  }
  const finality = transaction.finality && typeof transaction.finality === "object" ? transaction.finality : {};
  const eta = economyExplorerSecondsText(finality.eta_seconds || finality.settlement_seconds || 0);
  const provedText = economyTransactionProvedText(finality);
  const etaText = economyTransactionFinalityIsInternal(finality) ? "即時" : eta;
  wrap.innerHTML = `
    <span>Transaction Hash</span>
    <button class="economy-ledger-hash economy-explorer-address" type="button" data-explorer-query="${sanitize(txHash)}">${sanitize(txHash)}</button>
    <span>${sanitize(provedText)} · ETA ${sanitize(etaText)} · ${sanitize(economyTransactionStatusLabel(transaction.status || "pending"))}</span>
  `;
  wrap.querySelectorAll("[data-explorer-query]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const query = btn.dataset.explorerQuery || "";
      if ($("economy-explorer-query")) $("economy-explorer-query").value = query;
      setEconomyActivePage("explorer");
      searchEconomyExplorer(query);
    });
  });
}

function bindEconomyTransactionListEvents() {
  const list = $("economy-transactions-list");
  if (!list) return;
  list.querySelectorAll("[data-explorer-query]").forEach((btn) => {
    if (btn.dataset.explorerBound === "1") return;
    btn.dataset.explorerBound = "1";
    btn.addEventListener("click", () => {
      const query = btn.dataset.explorerQuery || "";
      if ($("economy-explorer-query")) $("economy-explorer-query").value = query;
      setEconomyActivePage("explorer");
      searchEconomyExplorer(query);
    });
  });
  list.querySelectorAll("[data-dispute-tx]").forEach((btn) => {
    if (btn.dataset.disputeBound === "1") return;
    btn.dataset.disputeBound = "1";
    btn.addEventListener("click", () => createEconomyTransactionDispute({
      txHash: btn.dataset.disputeTx || "",
      from: btn.dataset.disputeFrom || "",
      to: btn.dataset.disputeTo || "",
      amount: btn.dataset.disputeAmount || "0",
      chainBranch: btn.dataset.disputeBranch || "",
    }));
  });
}

function renderEconomyTransactions(payload = {}) {
  updateEconomyOfficialHotWalletLabels(payload.official_hot_wallet_labels);
  const summary = payload.summary && typeof payload.summary === "object" ? payload.summary : {};
  const rows = Array.isArray(payload.transactions) ? payload.transactions : [];
  setEconomyText("economy-transactions-pending-count", String(Number(summary.pending_count || 0)));
  setEconomyText("economy-transactions-confirmed-count", String(Number(summary.confirmed_count || 0)));
  setEconomyText("economy-transactions-failed-count", String(Number(summary.failed_count || 0)));
  const pendingPoints = Number(summary.pending_incoming_points || 0) + Number(summary.pending_outgoing_points || 0);
  setEconomyText("economy-transactions-pending-points", `待確認 ${formatEconomyPointsValue(pendingPoints)} 點`);
  setEconomyText("economy-transactions-finalized-count", `本次成交 ${Number(summary.finalized_count || 0)}`);
  renderEconomyRootList(rows, "economy-transactions-list", "尚無轉帳交易", (tx) => {
    const finality = tx.finality && typeof tx.finality === "object" ? tx.finality : {};
    const proved = economyTransactionProvedText(finality);
    const status = economyTransactionStatusLabel(tx.status);
    const pendingNote = String(tx.status || "") === "pending"
      ? " · Pending 不會讓收款錢包入帳"
      : "";
    const unownedNote = tx.wallet_flow?.destination_unowned || tx.destination_unowned
      ? " · 未綁定地址"
      : "";
    const direction = economyTransactionDirectionLabel(tx.direction);
    const amountText = economyTransactionAmountText(tx);
    const flowSource = tx.source_wallet_address || tx.wallet_flow?.source_wallet_address || "";
    const flowDestination = tx.destination_wallet_address || tx.wallet_flow?.destination_wallet_address || "";
    const displayedFee = tx.fee_points ?? tx.network_fee_points ?? tx.wallet_flow?.network_fee_points ?? 0;
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(direction)} · ${sanitize(status)} · ${sanitize(proved)} · ${sanitize(amountText)} 點</strong>
          <div class="drive-card-sub">${sanitize(tx.created_at || "")}${pendingNote}${unownedNote}</div>
          <div class="drive-card-sub">From ${sanitize(formatEconomyWalletAddressWithManagerLabel(flowSource))} → To ${sanitize(formatEconomyWalletAddressWithManagerLabel(flowDestination))} · Fee ${formatEconomyPointsValue(displayedFee)} 點</div>
          <button class="economy-ledger-hash economy-explorer-address" type="button" data-explorer-query="${sanitize(tx.transaction_hash || tx.tx_group_hash || "")}">${sanitize(tx.transaction_hash || tx.tx_group_hash || "-")}</button>
        </div>
        <div class="drive-file-actions">
          <button class="btn btn-sm" type="button" data-explorer-query="${sanitize(tx.transaction_hash || tx.tx_group_hash || "")}">查看</button>
          <button class="btn btn-sm" type="button"
            data-dispute-tx="${sanitize(tx.transaction_hash || tx.tx_group_hash || "")}"
            data-dispute-from="${sanitize(flowSource)}"
            data-dispute-to="${sanitize(flowDestination)}"
            data-dispute-amount="${sanitize(String(tx.amount_points || tx.amount || 0))}"
            data-dispute-branch="${sanitize(tx.chain_branch || tx.wallet_flow?.chain_branch || economyCurrentChainBranch || "main")}">疑義交易</button>
        </div>
      </div>
    `;
  });
  bindEconomyTransactionListEvents();
}

async function loadEconomyTransactions() {
  if (!currentUser || !economyChainEnabled()) {
    renderEconomyTransactions({ transactions: [], summary: {} });
    return null;
  }
  try {
    const json = await fetchEconomyJson("/points/transactions?limit=50&compact=1");
    renderEconomyTransactions(json);
    return json;
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyTransactionMsg, label: "鏈上交易", fallback: "鏈上交易紀錄讀取失敗" });
    renderEconomyTransactions({ transactions: [], summary: {} });
    return null;
  }
}

async function createEconomyTransactionDispute(input = {}) {
  const data = typeof input === "string" ? { txHash: input } : (input || {});
  const hash = String(data.txHash || "").trim();
  economyTransactionMsg(hash ? `開始申報疑義交易 ${shortEconomyWalletAddress(hash)}；下一步會檢查 From 地址持有證明。` : "開始申報疑義交易。");
  if (!hash) {
    economyTransactionMsg("找不到交易 hash，無法申報疑義。", false);
    return;
  }
  if (currentUser === "root") {
    economyNotifyFailure(new Error("root 帳號不使用匿名地址疑義流程；官方錢包或官方地址事故請改走官方治理、內部帳務治理或緊急安全治理。"), {
      msgFn: economyTransactionMsg,
      label: "鏈上交易",
      fallback: "疑義交易申報失敗",
    });
    return;
  }
  const fromAddress = String(data.from || prompt("From 地址（冷錢包需用錢包檔與冷錢包解鎖助記詞本機簽署；站內託管錢包使用登入帳號綁定證明）", "") || "").trim().toLowerCase();
  const toAddress = String(data.to || prompt("To 地址（系統會先短期限制此地址轉出）", "") || "").trim().toLowerCase();
  const amount = Math.max(0, Math.floor(Number(data.amount || prompt("交易點數", "0") || 0)));
  const chainBranch = String(data.chainBranch || economyCurrentChainBranch || "main").trim() || "main";
  if (!fromAddress || !toAddress || !amount) {
    economyTransactionMsg("疑義交易需要 From、To 與交易點數才能建立地址簽章。", false);
    return;
  }
  const statement = economyPromptAddressDisputeStatement({
    promptText: `請描述疑義交易原因、你主張的事實與佐證，至少 ${ECONOMY_ADDRESS_DISPUTE_MIN_STATEMENT_CHARS} 字。此內容會提供 root 程序審查；請勿填入帳號、暱稱、email。`,
    cancelText: "已取消疑義交易申報。",
    shortText: "疑義交易說明太短",
    msgFn: economyTransactionMsg,
  });
  if (statement === null) return;
  const lossCause = prompt("損失原因：private_key_leak / user_phishing / protocol_fault / exchange_bug / unknown", "private_key_leak") || "unknown";
  const evidence = prompt("證據 refs，每行一個 tx hash、截圖編號或案件號（可留空）", "") || "";
  try {
    const fromWallet = economyWalletByAddress(fromAddress);
    const accountBoundProof = economyWalletSupportsAccountBoundDisputeProof(fromWallet);
    const hasLocalSignaturePath = economyWalletRequiresSignature(fromWallet);
    if (!accountBoundProof && fromWallet && !hasLocalSignaturePath) {
      economyNotifyFailure(new Error("此 From 錢包沒有可用的本機簽章能力，不能用地址證明疑義流程申報。"), {
        msgFn: economyTransactionMsg,
        label: "鏈上交易",
        fallback: "疑義交易申報失敗",
      });
      return;
    }
    let proof = {
      account_bound_proof: accountBoundProof,
      signature_nonce: economyRequestId("address_dispute_account_bound"),
      signature_runtime_mode: economyAddressDisputeRuntimeMode(),
    };
    if (accountBoundProof) {
      economyTransactionMsg("From 是目前登入帳號綁定的站內託管錢包，使用帳號持有狀態建立疑義，不要求私鑰。");
    } else {
      economyTransactionMsg("等待 From 地址本機簽署疑義交易；私鑰不會送到伺服器。若此地址是他人的站內託管錢包，只有該帳號可直接申報。");
      proof = await economyBuildAddressDisputeProof({
        purpose: "address_dispute_open",
        signerAddress: fromAddress,
        txHash: hash,
        from: fromAddress,
        to: toAddress,
        amount,
        statement,
        evidence,
        chainBranch,
        runtimeMode: economyAddressDisputeRuntimeMode(),
      });
    }
    const json = await fetchEconomyJson("/points/transactions/disputes", {
      method: "POST",
      body: JSON.stringify({
        tx_hash: hash,
        from_wallet_address: fromAddress,
        to_wallet_address: toAddress,
        chain_branch: chainBranch,
        statement,
        victim_wallet_address: fromAddress,
        claimed_amount_points: amount,
        loss_cause: lossCause,
        evidence,
        public_key_jwk: proof.public_key_jwk,
        signature: proof.signature,
        signature_nonce: proof.signature_nonce,
        signature_runtime_mode: proof.signature_runtime_mode,
        account_bound_proof: !!proof.account_bound_proof,
      }),
    });
    economyNotifySuccess(`疑義交易已送出：${json.dispute?.dispute_uuid || ""}；To 地址已短期限制轉出，此申報只建立 case，不代表一定補償或 rollback。`, {
      msgFn: economyTransactionMsg,
      label: "鏈上交易",
    });
    await loadEconomyTransactionDisputes({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, {
      msgFn: economyTransactionMsg,
      label: "鏈上交易",
      fallback: "疑義交易申報失敗",
    });
  }
}

function updateEconomyGovernanceOverviewCounts(proposals = null) {
  const rows = Array.isArray(proposals) ? proposals : Array.from(economyGovernanceProposalCache.values());
  const publicRows = rows.filter((item) => economyGovernanceCategoryForProposal(item) === "public");
  const publicVoting = publicRows.filter((item) => economyGovernanceStatusBucket(item) === "voting").length;
  const disputeRows = Array.isArray(economyTransactionDisputeCache) ? economyTransactionDisputeCache : [];
  const activeDisputes = disputeRows.filter((item) => {
    const status = String(item.status || "").trim().toLowerCase();
    return !["rejected", "cancelled", "expired", "closed", "resolved"].includes(status);
  }).length;
  const disputeWithProposal = disputeRows.filter((item) => economyProposalUuidsForDispute(item).length > 0).length;
  setEconomyText("economy-governance-public-count", String(publicRows.length));
  setEconomyText("economy-governance-public-status", `目前可投 ${publicVoting}`);
  setEconomyText("economy-governance-dispute-count", String(disputeRows.length));
  setEconomyText("economy-governance-dispute-status", `待處理 ${activeDisputes} · 已提案 ${disputeWithProposal}`);
}

function renderEconomyTransactionDisputes(payload = {}) {
  updateEconomyOfficialHotWalletLabels(payload.official_hot_wallet_labels);
  const rows = Array.isArray(payload.disputes) ? payload.disputes : [];
  economyTransactionDisputeCache = rows;
  updateEconomyGovernanceOverviewCounts();
  const list = $("economy-disputes-list");
  if (!list) return;
  if (!rows.length) {
    list.innerHTML = `<div class="drive-empty">尚無疑義交易案件</div>`;
    return;
  }
  const availableSelected = rows.some((row) => String(row.dispute_uuid || "") === economySelectedDisputeUuid);
  if (economySelectedDisputeUuid && !availableSelected) {
    economySelectedDisputeUuid = "";
    economySelectedDisputeProposalUuids = new Set();
  }
  list.innerHTML = rows.map((row) => {
    const proposalUuids = economyProposalUuidsForDispute(row);
    const selected = String(row.dispute_uuid || "") === economySelectedDisputeUuid;
    const proposalRows = proposalUuids.map((uuid) => economyGovernanceProposalCache.get(String(uuid || ""))).filter(Boolean);
    const inlineGovernance = selected
      ? `
        <div class="economy-dispute-governance-panel" data-dispute-governance-panel="${sanitize(row.dispute_uuid || "")}" style="margin:.35rem 0 .9rem 1rem;padding:.75rem;border-left:3px solid rgba(80,180,255,.55);background:rgba(80,180,255,.06);">
          <div class="drive-card-sub" style="margin-bottom:.45rem;">此案件的治理提案 / 投票</div>
          <div class="drive-file-list">
            ${proposalRows.length
              ? proposalRows.map((proposal) => economyRenderGovernanceProposalCard(proposal, { nested: true })).join("")
              : proposalUuids.length
                ? `<div class="drive-empty">治理提案資料載入中，請按更新治理。</div>`
                : `<div class="drive-empty">此案件尚未建立治理提案。manager+ 核准並提案後，投票會顯示在這裡。</div>`}
          </div>
        </div>
      `
      : "";
    const managerActions = economyGovernanceCanManage() && ["pending_review", "approved"].includes(String(row.status || ""))
      ? `
        <button class="btn btn-sm" type="button" data-dispute-review="approved" data-dispute-uuid="${sanitize(row.dispute_uuid || "")}">核准</button>
        <button class="btn btn-sm" type="button" data-dispute-review="rejected" data-dispute-uuid="${sanitize(row.dispute_uuid || "")}">駁回</button>
        <button class="btn btn-danger btn-sm" type="button" data-dispute-review-proposal="${sanitize(row.dispute_uuid || "")}">核准並提案</button>
      `
      : "";
    const replyAction = ["pending_review", "approved", "proposal_created"].includes(String(row.status || ""))
      ? `<button class="btn btn-sm" type="button"
          data-dispute-reply="${sanitize(row.dispute_uuid || "")}"
          data-dispute-tx="${sanitize(row.tx_hash || "")}"
          data-dispute-from="${sanitize(row.from_wallet_address || row.victim_wallet_address || "")}"
          data-dispute-to="${sanitize(row.to_wallet_address || row.suspect_wallet_address || "")}"
          data-dispute-amount="${sanitize(String(row.claimed_amount_points || 0))}"
          data-dispute-branch="${sanitize(row.chain_branch || economyCurrentChainBranch || "main")}"
          data-dispute-runtime-mode="${sanitize(row.signature_runtime_mode || economyAddressDisputeRuntimeMode())}">To 地址回覆</button>`
      : "";
    return `
      <div class="drive-file-row${selected ? " active" : ""}" style="${selected ? "border-color:rgba(80,180,255,.55);" : ""}">
        <div>
          <strong>${sanitize(row.status || "-")} · ${sanitize(row.loss_cause || "-")} · ${formatEconomyPointsValue(row.claimed_amount_points || 0)} 點</strong>
          <div class="drive-card-sub">address-proven anonymous · ${sanitize(row.created_at || "")}</div>
          <div class="drive-card-sub">${sanitize(row.statement || "")}</div>
          <button class="economy-ledger-hash economy-explorer-address" type="button" data-explorer-query="${sanitize(row.tx_hash || "")}">${sanitize(row.tx_hash || "")}</button>
          <div class="drive-card-sub">From ${sanitize(formatEconomyWalletAddressWithManagerLabel(row.from_wallet_address || row.victim_wallet_address || ""))} → To ${sanitize(formatEconomyWalletAddressWithManagerLabel(row.to_wallet_address || row.suspect_wallet_address || ""))} · from proof ${row.from_signature_verified ? "ok" : "missing"} · to reply ${row.reply_signature_verified ? "ok" : "none"}</div>
          ${row.initial_freeze_expires_at ? `<div class="drive-card-sub">初始短期凍結至 ${sanitize(row.initial_freeze_expires_at)}${row.escalated_freeze_expires_at ? ` · 治理延長至 ${sanitize(row.escalated_freeze_expires_at)}` : ""}</div>` : ""}
          ${row.reply_statement ? `<div class="drive-card-sub">To 回覆：${sanitize(row.reply_statement || "")}</div>` : ""}
          <div class="drive-card-sub">proposal ${sanitize(row.governance_proposal_uuid || "-")}</div>
        </div>
        <div class="drive-file-actions">
          <button class="btn btn-sm" type="button"
            data-dispute-select="${sanitize(row.dispute_uuid || "")}"
            data-dispute-proposals="${sanitize(proposalUuids.join(","))}">${selected ? "收合提案" : (proposalUuids.length ? "展開提案/投票" : "選取案件")}</button>
          ${replyAction}${managerActions}
        </div>
      </div>
      ${inlineGovernance}
    `;
  }).join("");
  bindEconomyDisputeEvents();
}

async function replyEconomyTransactionDispute(input = {}) {
  const disputeUuid = String(input.disputeUuid || "").trim();
  const txHash = String(input.txHash || "").trim();
  const fromAddress = String(input.from || "").trim().toLowerCase();
  const toAddress = String(input.to || "").trim().toLowerCase();
  const amount = Math.max(0, Math.floor(Number(input.amount || 0)));
  const chainBranch = String(input.chainBranch || economyCurrentChainBranch || "main").trim() || "main";
  const runtimeMode = String(input.runtimeMode || economyAddressDisputeRuntimeMode()).trim() || "unknown";
  if (!disputeUuid || !txHash || !fromAddress || !toAddress || !amount) {
    economyGovernanceMsg("缺少疑義交易回覆所需的地址或交易資料。", false);
    return;
  }
  const statement = economyPromptAddressDisputeStatement({
    promptText: `To 地址持有人回覆，至少 ${ECONOMY_ADDRESS_DISPUTE_MIN_STATEMENT_CHARS} 字。只能寫地址層證據與事實，請勿填入 user id、用戶名、暱稱、email 或帳號資訊。`,
    cancelText: "已取消 To 地址回覆。",
    shortText: "To 地址回覆太短",
    msgFn: economyGovernanceMsg,
  });
  if (statement === null) return;
  const evidence = prompt("To 地址證據 refs，每行一個 tx hash、截圖編號或案件號（可留空）", "") || "";
  try {
    const toWallet = economyWalletByAddress(toAddress);
    const accountBoundProof = economyWalletSupportsAccountBoundDisputeProof(toWallet);
    let proof = {
      account_bound_proof: accountBoundProof,
      signature_nonce: economyRequestId("address_dispute_reply_account_bound"),
      signature_runtime_mode: runtimeMode,
    };
    if (accountBoundProof) {
      economyGovernanceMsg("To 是目前登入帳號綁定的站內託管錢包，使用帳號持有狀態回覆，不要求私鑰。");
    } else {
      economyGovernanceMsg("等待 To 地址本機簽署回覆；私鑰不會送到伺服器。若此地址是他人的站內託管錢包，只有該帳號可直接回覆。");
      proof = await economyBuildAddressDisputeProof({
        purpose: "address_dispute_reply",
        signerAddress: toAddress,
        txHash,
        from: fromAddress,
        to: toAddress,
        amount,
        statement,
        evidence,
        chainBranch,
        runtimeMode,
      });
    }
    await fetchEconomyJson(`/points/transactions/disputes/${encodeURIComponent(disputeUuid)}/reply`, {
      method: "POST",
      body: JSON.stringify({
        statement,
        evidence,
        public_key_jwk: proof.public_key_jwk,
        signature: proof.signature,
        signature_nonce: proof.signature_nonce,
        signature_runtime_mode: proof.signature_runtime_mode,
        account_bound_proof: !!proof.account_bound_proof,
      }),
    });
    economyNotifySuccess("To 地址回覆已送出。", { msgFn: economyGovernanceMsg, label: "疑義交易" });
    await loadEconomyTransactionDisputes({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "疑義交易", fallback: "疑義交易回覆失敗" });
  }
}

async function loadEconomyTransactionDisputes({ silent = false } = {}) {
  if (!currentUser || !economyChainEnabled()) {
    renderEconomyTransactionDisputes({ disputes: [] });
    return false;
  }
  try {
    const json = await fetchEconomyJson("/points/transactions/disputes?limit=50");
    renderEconomyTransactionDisputes(json);
    if (!silent) economyGovernanceMsg("疑義交易案件已更新。");
    return true;
  } catch (err) {
    if (!silent) economyGovernanceMsg(err.message || "疑義交易案件讀取失敗", false);
    return false;
  }
}

async function reviewEconomyTransactionDispute(disputeUuid, status, createProposal = false) {
  const strategy = createProposal
    ? (prompt("Recovery 方案：tainted_remainder_return / treasury_compensation / exclude_tainted_descendants", "tainted_remainder_return") || "tainted_remainder_return")
    : "tainted_remainder_return";
  const note = prompt(createProposal ? "審核意見，會附在治理提案中" : "審核意見", "") || "";
  try {
    const json = await fetchEconomyJson(`/admin/points/transactions/disputes/${encodeURIComponent(disputeUuid)}/review`, {
      method: "POST",
      body: JSON.stringify({
        status,
        review_note: note,
        recommended_strategy: strategy,
        create_proposal: createProposal,
      }),
    });
    const linked = [
      json.proposal?.proposal_uuid ? `recovery ${json.proposal.proposal_uuid}` : "",
      json.address_risk_proposal?.proposal_uuid ? `標記 ${json.address_risk_proposal.proposal_uuid}` : "",
      json.address_freeze_proposal?.proposal_uuid ? `凍結 ${json.address_freeze_proposal.proposal_uuid}` : "",
    ].filter(Boolean).join("，");
    if (createProposal) {
      const proposalUuids = [
        json.proposal?.proposal_uuid,
        json.address_risk_proposal?.proposal_uuid,
        json.address_freeze_proposal?.proposal_uuid,
      ].map((item) => String(item || "").trim()).filter(Boolean);
      economySelectedDisputeUuid = String(disputeUuid || "").trim();
      economySelectedDisputeProposalUuids = new Set(proposalUuids);
    }
    economyNotifySuccess(`疑義交易已更新${linked ? `，已建立治理案：${linked}` : ""}`, {
      msgFn: economyGovernanceMsg,
      label: "治理提案",
    });
    await Promise.all([loadEconomyTransactionDisputes({ silent: true }), loadEconomyGovernance({ silent: true })]);
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "疑義交易審核失敗" });
  }
}

function bindEconomyDisputeEvents() {
  const list = $("economy-disputes-list");
  if (!list) return;
  list.querySelectorAll("[data-dispute-select]").forEach((btn) => {
    if (btn.dataset.disputeBound === "1") return;
    btn.dataset.disputeBound = "1";
    btn.addEventListener("click", async () => {
      const nextUuid = String(btn.dataset.disputeSelect || "").trim();
      const sameSelection = nextUuid && nextUuid === economySelectedDisputeUuid;
      economySelectedDisputeUuid = sameSelection ? "" : nextUuid;
      economySelectedDisputeProposalUuids = sameSelection
        ? new Set()
        : new Set(String(btn.dataset.disputeProposals || "").split(",").map((item) => item.trim()).filter(Boolean));
      economyGovernanceMsg(!economySelectedDisputeUuid
        ? "已收合疑義案件提案。"
        : economySelectedDisputeProposalUuids.size
          ? `已展開疑義案件 ${shortEconomyWalletAddress(economySelectedDisputeUuid)} 的治理提案 / 投票。`
          : `已選取疑義案件 ${shortEconomyWalletAddress(economySelectedDisputeUuid)}；此案件尚未建立治理提案。`);
      await loadEconomyGovernance({ silent: true });
      renderEconomyTransactionDisputes({ disputes: economyTransactionDisputeCache });
    });
  });
  list.querySelectorAll("[data-explorer-query]").forEach((btn) => {
    if (btn.dataset.explorerBound === "1") return;
    btn.dataset.explorerBound = "1";
    btn.addEventListener("click", () => {
      const query = btn.dataset.explorerQuery || "";
      if ($("economy-explorer-query")) $("economy-explorer-query").value = query;
      setEconomyActivePage("explorer");
      searchEconomyExplorer(query);
    });
  });
  list.querySelectorAll("[data-dispute-review]").forEach((btn) => {
    if (btn.dataset.disputeBound === "1") return;
    btn.dataset.disputeBound = "1";
    btn.addEventListener("click", () => reviewEconomyTransactionDispute(btn.dataset.disputeUuid || "", btn.dataset.disputeReview || "", false));
  });
  list.querySelectorAll("[data-dispute-review-proposal]").forEach((btn) => {
    if (btn.dataset.disputeBound === "1") return;
    btn.dataset.disputeBound = "1";
    btn.addEventListener("click", () => reviewEconomyTransactionDispute(btn.dataset.disputeReviewProposal || "", "approved", true));
  });
  list.querySelectorAll("[data-dispute-reply]").forEach((btn) => {
    if (btn.dataset.disputeBound === "1") return;
    btn.dataset.disputeBound = "1";
    btn.addEventListener("click", () => replyEconomyTransactionDispute({
      disputeUuid: btn.dataset.disputeReply || "",
      txHash: btn.dataset.disputeTx || "",
      from: btn.dataset.disputeFrom || "",
      to: btn.dataset.disputeTo || "",
      amount: btn.dataset.disputeAmount || "0",
      chainBranch: btn.dataset.disputeBranch || "",
      runtimeMode: btn.dataset.disputeRuntimeMode || "",
    }));
  });
  bindEconomyGovernanceEvents(list);
}

async function submitEconomyWalletTransfer() {
  if (!economyChainEnabled()) {
    economyTransferMsg("PointsChain 私有鏈已停用，鏈上轉帳不可用；基本積分帳本仍可使用。", false);
    return;
  }
  const btn = $("economy-transfer-submit-btn");
  const source = String($("economy-transfer-source-wallet")?.value || "").trim();
  const destination = String($("economy-transfer-destination-wallet")?.value || "").trim();
  const amount = Math.floor(Number($("economy-transfer-amount")?.value || 0));
  let fee = Math.floor(Number($("economy-transfer-fee")?.value || 0));
  const memo = String($("economy-transfer-memo")?.value || "").trim();
  const sourcePc0 = economyIsPc0Address(source);
  const destinationPc0 = economyIsPc0Address(destination);
  const rail = economyTransferRailFromInputs() || (sourcePc0 ? "internal_pc0" : "cold_chain");
  if (!sourcePc0 && destinationPc0) {
    economyTransferMsg("pc1 冷錢包不能直接轉到 pc0 站內地址；請使用對方熱錢包轉入畫面列出的 pc1 橋接入金地址。", false);
    return;
  }
  if (sourcePc0 && rail === "internal_pc0" && !destinationPc0) {
    economyTransferMsg("站內 pc0 互轉模式只能填 pc0 地址；若要提領到外部 / 冷錢包，請切換轉出模式。", false);
    return;
  }
  if (sourcePc0 && rail === "external_cold" && destinationPc0) {
    economyTransferMsg("外部 / 冷錢包轉出模式不能填 pc0 地址；站內互轉請切回 pc0 站內地址。", false);
    return;
  }
  if (sourcePc0 && rail === "internal_pc0") fee = 0;
  if (!source || !destination || !Number.isFinite(amount) || amount <= 0 || !Number.isFinite(fee) || fee < 0) {
    economyTransferMsg("請確認 From、To、Value 與 Transaction Fee", false);
    return;
  }
  const requestUuid = economyRequestId("points_chain_transfer");
  const oldText = btn ? btn.textContent : "";
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "簽署中...";
    }
    const sourceWallet = economyWalletByAddress(source);
    economyTransferMsg(economyWalletRequiresSignature(sourceWallet)
      ? "等待冷錢包本機簽署，請確認錢包檔與冷錢包解鎖助記詞只在可信裝置使用。"
      : sourcePc0 && rail === "internal_pc0"
        ? "正在送出 pc0 站內互轉；不收鏈上 fee、不等待 Proved。"
        : "正在送出轉出請求。");
    const signature = await economyBuildTransferSignature({ source, destination, amount, fee, memo, requestUuid });
    if (btn) btn.textContent = "送單中...";
    const json = await fetchEconomyJson("/points/transactions/submit", {
      method: "POST",
      body: JSON.stringify({
        source_wallet_address: source,
        destination_wallet_address: destination,
        amount_points: amount,
        fee_points: fee,
        memo,
        request_uuid: requestUuid,
        signature,
      }),
    });
    const txHash = json.transaction_hash || json.tx_group_hash || json.transaction?.transaction_hash || "";
    const finality = json.transaction?.finality || {};
    const tx = json.transaction || {};
    const eta = economyExplorerSecondsText(finality.eta_seconds || finality.settlement_seconds || 0);
    const warningSuffix = economyWarningSuffix(json);
    const immediate = tx.chain_required === false
      || tx.chain_required === 0
      || ["internal_hot_wallet", "internal_system_burn", "deposit_bridge_credit", "withdrawal_bridge_refund"].includes(String(tx.settlement_rail || ""));
    const successMessage = immediate
      ? (txHash ? `pc0 站內互轉已完成：${txHash}。` : "pc0 站內互轉已完成。")
      : txHash
        ? `交易已送出：${txHash}，等待 20/20 Proved，ETA ${eta}；成交前收款方不會入帳。`
        : `交易已送出，等待 20/20 Proved，ETA ${eta}；成交前收款方不會入帳。`;
    const visibleMessage = `${successMessage}${warningSuffix}`;
    economyTransferMsg(visibleMessage, !warningSuffix);
    renderEconomyTransferLastResult(txHash, json.transaction || {});
    if (txHash) {
      if ($("economy-explorer-query")) $("economy-explorer-query").value = txHash;
    }
    setEconomyActivePage("transactions");
    await loadEconomyDashboard();
    await loadEconomyTransactions();
    economySetMsg(visibleMessage, !warningSuffix);
  } catch (err) {
    economyNotifyFailure(err, {
      msgFn: economyTransferMsg,
      label: "鏈上送單",
      fallback: "冷錢包本機簽署或鏈上送單失敗，交易未送出",
    });
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText || "送出交易";
    }
  }
}

async function postEconomyWalletOnboarding(payload, operation = null) {
  const operationContext = operation || economyOperationContext();
  economyAssertOperationCurrent(operationContext);
  await fetchCsrfToken({ force: true });
  economyAssertOperationCurrent(operationContext);
  const json = await fetchEconomyJson("/points/wallet/onboarding", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  economyAssertOperationCurrent(operationContext);
  renderEconomyWalletOnboarding(json.onboarding || {});
  const createdMessages = [];
  if (json.creation_fee?.charged) createdMessages.push(`建立費 ${formatEconomyPointsValue(json.creation_fee.amount_points || 0)} 點已入官方 Treasury`);
  economyWalletMsg(createdMessages.length ? `錢包已綁定，${createdMessages.join("，")}。` : "錢包已綁定。");
  await loadEconomyDashboard();
  economyAssertOperationCurrent(operationContext);
}

async function useOfficialHotWallet() {
  const operation = economyOperationContext();
  try {
    const feePayload = await economyWalletCreationFeePayload("official_hot", operation);
    economyAssertOperationCurrent(operation);
    await postEconomyWalletOnboarding({ mode: "official_hot", ...feePayload }, operation);
    destroyEconomyColdWalletSecrets();
  } catch (err) {
    if (!economyOperationIsCurrent(operation) || err?.name === "AbortError") return;
    economyWalletMsg(err.message || "站內託管錢包建立失敗", false);
  }
}

function openEconomyWalletReceive(address) {
  const wallet = economyWalletByAddress(address);
  const target = economyNormalizeAddress(wallet?.address || address);
  const panel = economyWalletActionPanel();
  const content = economyWalletActionContent();
  if (!target || !panel || !content) {
    economyWalletMsg("找不到錢包地址", false);
    return;
  }
  const pc0 = economyIsPc0Address(target);
  const depositAddress = economyActiveDepositAddress();
  panel.style.display = "";
  if ($("economy-transfer-last-result")) $("economy-transfer-last-result").innerHTML = "";
  const receiveBody = pc0
    ? `
      <div class="settings-option-grid">
        <div class="field">
          <label>站內 pc0 收款地址</label>
          <input type="text" value="${sanitize(target)}" readonly autocomplete="off" />
          <small class="drive-card-sub">站內會員互轉可以使用此 pc0 地址，立即入帳且不收鏈上 fee。</small>
          <button class="btn btn-sm" type="button" data-wallet-copy-address="${sanitize(target)}">複製 pc0 地址</button>
        </div>
        <div class="field">
          <label>此 pc0 綁定的橋接 pc1 入金地址</label>
          <input type="text" value="${sanitize(depositAddress || "尚未建立")}" readonly autocomplete="off" />
          <small class="drive-card-sub">冷錢包或外部地址不能直接轉到 pc0；此 pc0 只能透過這個 pc1 橋接地址入金，確認後系統 credit 到 pc0 站內託管錢包。</small>
          ${depositAddress ? `<button class="btn btn-sm" type="button" data-wallet-copy-address="${sanitize(depositAddress)}">複製 pc1 入金地址</button>` : ""}
        </div>
      </div>
    `
    : `
      <div class="settings-option-grid">
        <div class="field">
          <label>冷錢包收款地址</label>
          <input type="text" value="${sanitize(target)}" readonly autocomplete="off" />
          <small class="drive-card-sub">此為 pc1 冷錢包地址，可供一般鏈上轉入；站內 pc0 互轉請使用對方 pc0 站內地址。</small>
          <button class="btn btn-sm" type="button" data-wallet-copy-address="${sanitize(target)}">複製地址</button>
        </div>
      </div>
    `;
  content.innerHTML = `
    <div class="drive-card">
      <div class="drive-card-heading">
        <div>
          <div class="drive-card-title">轉入 ${sanitize(shortEconomyWalletAddress(target))}</div>
          <div class="drive-card-sub">${pc0 ? "站內託管錢包：pc0 只供站內辨識；冷錢包入金請使用 pc1 入金地址。" : "冷錢包：顯示原始 pc1 地址供鏈上收款。"}</div>
        </div>
      </div>
      ${receiveBody}
    </div>
  `;
  bindEconomyWalletActionPanelEvents();
  setEconomyActivePage("balance");
  panel.scrollIntoView({ block: "start", behavior: "smooth" });
  economyTransferMsg(pc0 ? "已顯示 pc0 站內地址與 pc1 入金地址。" : "已顯示冷錢包收款地址。");
}

function openEconomyWalletSend(address) {
  const wallet = economyWalletByAddress(address);
  const source = economyNormalizeAddress(wallet?.address || address);
  const panel = economyWalletActionPanel();
  const content = economyWalletActionContent();
  if (!source || !panel || !content) {
    economyWalletMsg("找不到轉出錢包", false);
    return;
  }
  if (!economyWalletCanSpend(wallet)) {
    economyWalletMsg("此錢包目前不能轉出", false);
    return;
  }
  const sourcePc0 = economyIsPc0Address(source);
  const railOptions = sourcePc0
    ? `<select id="economy-transfer-rail">
        <option value="internal_pc0">站內 pc0 地址</option>
        <option value="external_cold">外部 / 冷錢包地址</option>
      </select>`
    : `<input type="text" id="economy-transfer-rail" value="cold_chain" readonly />`;
  const railNote = sourcePc0
    ? "站內 pc0 轉出可選內部互轉或提領到外部 / 冷錢包；內部互轉不收鏈上 fee。"
    : "冷錢包轉出是一般鏈上轉帳；目的地不能是 pc0 站內地址，入金 pc0 請用對方熱錢包顯示的 pc1 入金地址。";
  panel.style.display = "";
  if ($("economy-transfer-last-result")) $("economy-transfer-last-result").innerHTML = "";
  content.innerHTML = `
    <div class="drive-card">
      <div class="drive-card-heading">
        <div>
          <div class="drive-card-title">轉出 ${sanitize(shortEconomyWalletAddress(source))}</div>
          <div class="drive-card-sub">${sanitize(railNote)}</div>
        </div>
      </div>
      <div class="settings-option-grid">
        <div class="field">
          <label>From</label>
          <input type="text" id="economy-transfer-source-wallet" value="${sanitize(source)}" readonly autocomplete="off" />
        </div>
        <div class="field">
          <label>轉出模式</label>
          ${railOptions}
        </div>
        <div class="field">
          <label>常用地址</label>
          <select id="economy-transfer-favorite-address">${economyFavoriteAddressOptionsHtml({ source, rail: sourcePc0 ? "internal_pc0" : "cold_chain" })}</select>
          <small class="drive-card-sub">常用地址會依目前模式過濾；pc1 → pc0 不會列出也不能送出。</small>
        </div>
        <div class="field">
          <label>To</label>
          <input type="text" id="economy-transfer-destination-wallet" placeholder="${sourcePc0 ? "pc0..." : "pc1... / 外部地址"}" autocomplete="off" />
        </div>
        <div class="field">
          <label>常用地址名稱</label>
          <input type="text" id="economy-transfer-favorite-label" maxlength="80" placeholder="可留空" autocomplete="off" />
          <button class="btn btn-sm" id="economy-transfer-save-favorite-btn" type="button">加入常用</button>
          <button class="btn btn-sm" id="economy-transfer-remove-favorite-btn" type="button">移除選取常用</button>
        </div>
        <div class="field"><label>Value</label><input type="number" id="economy-transfer-amount" min="1" value="1" /></div>
        <div class="field"><label>Transaction Fee</label><input type="number" id="economy-transfer-fee" min="0" value="${sourcePc0 ? "0" : "1"}" /><small id="economy-transfer-fee-estimate" class="drive-card-sub">預估 Proved 時間讀取中...</small></div>
        <div class="field"><label>Input Data</label><input type="text" id="economy-transfer-memo" maxlength="180" placeholder="可留空" /></div>
        <div class="field"><label>&nbsp;</label><button class="btn btn-primary" id="economy-transfer-submit-btn" type="button">送出交易</button></div>
      </div>
    </div>
  `;
  bindEconomyWalletActionPanelEvents();
  economySyncTransferRailFields();
  setEconomyActivePage("balance");
  panel.scrollIntoView({ block: "start", behavior: "smooth" });
  economyTransferMsg("請輸入目的地與金額後送出。");
}

function bindEconomyWalletActionPanelEvents() {
  const panel = economyWalletActionPanel();
  if (!panel) return;
  panel.querySelectorAll("[data-wallet-copy-address]").forEach((btn) => {
    if (btn.dataset.walletActionBound === "1") return;
    btn.dataset.walletActionBound = "1";
    btn.addEventListener("click", () => copyEconomyText(btn.dataset.walletCopyAddress || "", "地址已複製"));
  });
  const rail = $("economy-transfer-rail");
  if (rail && rail.dataset.walletActionBound !== "1") {
    rail.dataset.walletActionBound = "1";
    rail.addEventListener("change", economySyncTransferRailFields);
  }
  const destination = $("economy-transfer-destination-wallet");
  if (destination && destination.dataset.walletActionBound !== "1") {
    destination.dataset.walletActionBound = "1";
    destination.addEventListener("input", economySyncTransferRailFields);
    destination.addEventListener("change", economySyncTransferRailFields);
  }
  const favoriteSelect = $("economy-transfer-favorite-address");
  if (favoriteSelect && favoriteSelect.dataset.walletActionBound !== "1") {
    favoriteSelect.dataset.walletActionBound = "1";
    favoriteSelect.addEventListener("change", () => {
      const value = economyNormalizeAddress(favoriteSelect.value);
      if (value && $("economy-transfer-destination-wallet")) {
        $("economy-transfer-destination-wallet").value = value;
        economySyncTransferRailFields();
      }
    });
  }
  const saveFavorite = $("economy-transfer-save-favorite-btn");
  if (saveFavorite && saveFavorite.dataset.walletActionBound !== "1") {
    saveFavorite.dataset.walletActionBound = "1";
    saveFavorite.addEventListener("click", () => {
      try {
        const source = economyNormalizeAddress($("economy-transfer-source-wallet")?.value);
        const railValue = economyTransferRailFromInputs();
        const address = economyNormalizeAddress($("economy-transfer-destination-wallet")?.value);
        if (!economyTransferFavoriteAllowed(address, { source, rail: railValue })) {
          economyTransferMsg("此地址不符合目前轉出模式，不能加入常用。", false);
          return;
        }
        upsertEconomyFavoriteAddress(address, $("economy-transfer-favorite-label")?.value || "");
        if (favoriteSelect) {
          favoriteSelect.innerHTML = economyFavoriteAddressOptionsHtml({ source, rail: railValue });
          favoriteSelect.value = address;
        }
        economyTransferMsg(`已加入常用地址：${shortEconomyWalletAddress(address)}`);
      } catch (err) {
        economyTransferMsg(err.message || "加入常用地址失敗", false);
      }
    });
  }
  const removeFavorite = $("economy-transfer-remove-favorite-btn");
  if (removeFavorite && removeFavorite.dataset.walletActionBound !== "1") {
    removeFavorite.dataset.walletActionBound = "1";
    removeFavorite.addEventListener("click", () => {
      const source = economyNormalizeAddress($("economy-transfer-source-wallet")?.value);
      const railValue = economyTransferRailFromInputs();
      const selected = economyNormalizeAddress(favoriteSelect?.value);
      if (!selected) {
        economyTransferMsg("請先選擇要移除的常用地址", false);
        return;
      }
      removeEconomyFavoriteAddress(selected);
      if (favoriteSelect) favoriteSelect.innerHTML = economyFavoriteAddressOptionsHtml({ source, rail: railValue });
      economyTransferMsg(`已移除常用地址：${shortEconomyWalletAddress(selected)}`);
    });
  }
  const submitBtn = $("economy-transfer-submit-btn");
  if (submitBtn && submitBtn.dataset.walletActionBound !== "1") {
    submitBtn.dataset.walletActionBound = "1";
    submitBtn.addEventListener("click", submitEconomyWalletTransfer);
  }
  const feeInput = $("economy-transfer-fee");
  if (feeInput && feeInput.dataset.economyEstimateBound !== "1") {
    feeInput.dataset.economyEstimateBound = "1";
    feeInput.addEventListener("input", scheduleEconomyTransferFeeEstimate);
    feeInput.addEventListener("change", scheduleEconomyTransferFeeEstimate);
  }
}

function setEconomyDefaultWalletFromCard(address) {
  const target = String(address || "").trim().toLowerCase();
  if (!target) {
    economyWalletMsg("找不到要設為預設的錢包地址", false);
    return;
  }
  writeEconomyDefaultSpendWalletAddress(target);
  renderEconomyWalletIdentityList(economyWalletOnboardingState);
  economyWalletMsg(`已設為交易所預設付款錢包：${shortEconomyWalletAddress(target)}。`);
}

async function verifyEconomyColdWalletBackupForAddress(address) {
  const operation = economyOperationContext();
  const target = String(address || "").trim().toLowerCase();
  if (!target) {
    economyWalletMsg("找不到要驗證的冷錢包地址", false);
    return;
  }
  let loaded = null;
  try {
    loaded = await economyPromptColdWalletForSigning({
      expectedAddress: target,
      purposeLabel: "密鑰驗證",
      cancelMessage: "已取消冷錢包密鑰驗證。",
      mismatchMessage: "冷錢包檔地址與此冷錢包不一致",
      operation,
    });
    economyAssertOperationCurrent(operation);
    economyWalletMsg(`錢包檔可控制此地址：${shortEconomyWalletAddress(target)}。私鑰只在瀏覽器本機解密，不會送到伺服器。`);
  } catch (err) {
    if (!economyOperationIsCurrent(operation) || err?.name === "AbortError") return;
    economyWalletMsg(err.message || "冷錢包密鑰驗證失敗", false);
  } finally {
    loaded = null;
  }
}

async function deleteEconomyColdWallet(addressOverride = "") {
  try {
    const address = String(addressOverride || "").trim();
    if (!address) {
      economyWalletMsg("請先選擇要刪除的冷錢包", false);
      return;
    }
    if (!confirm("刪除冷錢包不會刪除帳本，但之後必須提供該錢包檔與冷錢包解鎖助記詞才能恢復同一地址。確定刪除？")) return;
    const json = await fetchEconomyJson("/points/wallet/onboarding", {
      method: "DELETE",
      body: JSON.stringify({ address, reason: "user_deleted_cold_wallet" }),
    });
    renderEconomyWalletOnboarding(json.onboarding || {});
    economyForgetColdWalletSigningSession(address);
    destroyEconomyColdWalletSecrets();
    economyWalletMsg("冷錢包已移除，且不再列入帳戶總額；若要恢復同一地址，請匯入錢包檔並輸入冷錢包解鎖助記詞。");
    await loadEconomyDashboard();
  } catch (err) {
    economyWalletMsg(err.message || "冷錢包刪除失敗", false);
  }
}

async function createColdWalletDraft() {
  const operation = economyOperationContext();
  economyAssertOperationCurrent(operation);
  if (!window.crypto?.subtle) {
    economyWalletMsg("此瀏覽器不支援 WebCrypto，無法建立冷錢包", false);
    return;
  }
  let privateJwk = null;
  try {
    const keyPair = await crypto.subtle.generateKey(
      { name: "ECDSA", namedCurve: "P-256" },
      true,
      ["sign", "verify"]
    );
    economyAssertOperationCurrent(operation);
    privateJwk = await crypto.subtle.exportKey("jwk", keyPair.privateKey);
    economyAssertOperationCurrent(operation);
    const publicJwk = await crypto.subtle.exportKey("jwk", keyPair.publicKey);
    economyAssertOperationCurrent(operation);
    const { address } = await economyWalletAddressFromPublicJwk(publicJwk);
    economyAssertOperationCurrent(operation);
    const tradePassword = await economyDerivedColdWalletTradePassword(privateJwk, address, operation);
    economyAssertOperationCurrent(operation);
    const mnemonicWords = economyColdWalletMnemonicWords(tradePassword);
    const mnemonicQuiz = economyBuildColdWalletMnemonicQuiz(mnemonicWords);
    const walletFile = await economyEncryptColdWalletFile({
      privateJwk,
      publicJwk,
      address,
      password: tradePassword,
      operation,
    });
    economyAssertOperationCurrent(operation);
    const walletFileName = economyColdWalletFileName(address);
    economyScrubPrivateJwk(privateJwk);
    destroyEconomyColdWalletSecrets({ hideGenerated: false });
    economyColdWalletDraft = { address, walletFile, walletFileName, tradePassword, mnemonicWords, mnemonicQuiz, quizPassed: false };
    if ($("economy-wallet-generated-panel")) $("economy-wallet-generated-panel").style.display = "";
    if ($("economy-wallet-generated-address")) $("economy-wallet-generated-address").value = address;
    if ($("economy-wallet-generated-file-name")) $("economy-wallet-generated-file-name").value = walletFileName;
    const tradePasswordInput = $("economy-wallet-generated-trade-password");
    if (tradePasswordInput) {
      tradePasswordInput.type = "text";
      tradePasswordInput.value = tradePassword;
      tradePasswordInput.placeholder = "只顯示一次，請離線保存";
    }
    if ($("economy-wallet-generated-selection-status")) $("economy-wallet-generated-selection-status").textContent = "尚未選用";
    resetEconomyColdWalletMnemonicQuiz();
    const quizBtn = $("economy-wallet-start-mnemonic-quiz-btn");
    if (quizBtn) {
      quizBtn.disabled = false;
      quizBtn.textContent = "確認已保存，隱藏助記詞並開始考試";
    }
    syncEconomyGeneratedColdWalletSelectionButton();
    economyWalletMsg("冷錢包只建立草稿，尚未匯入或綁定；請先下載錢包檔、離線保存冷錢包解鎖助記詞，再按確認隱藏助記詞進行考試。");
  } catch (err) {
    if (!economyOperationIsCurrent(operation) || err?.name === "AbortError") return;
    economyWalletMsg(err.message || "冷錢包建立失敗", false);
  } finally {
    economyScrubPrivateJwk(privateJwk);
  }
}

async function selectGeneratedColdWalletForImport() {
  if (!economyColdWalletDraft) {
    economyWalletMsg("目前沒有新建冷錢包草稿", false);
    return;
  }
  if (!economyGeneratedColdWalletReadyForSelection()) {
    economyWalletMsg("請先通過記憶詞考試，再選用此冷錢包。", false);
    return;
  }
  const operation = economyOperationContext();
  const draft = economyColdWalletDraft;
  try {
    const raw = JSON.stringify(draft.walletFile || {});
    const loaded = await economyLoadEncryptedColdWalletFile(raw, draft.tradePassword, {
      imported: false,
      operation,
    });
    economyAssertOperationCurrent(operation);
    if (String(loaded.address || "").toLowerCase() !== String(draft.address || "").toLowerCase()) {
      throw new Error("冷錢包檔與草稿地址不一致");
    }
    economyColdWalletBindCandidate = loaded;
    economyRememberColdWalletSigningSession(loaded, draft.tradePassword);
    if ($("economy-wallet-private-key")) $("economy-wallet-private-key").value = "";
    if ($("economy-wallet-file-input")) $("economy-wallet-file-input").value = "";
    if ($("economy-wallet-file-password")) $("economy-wallet-file-password").value = "";
    if ($("economy-wallet-private-key-confirmed")) $("economy-wallet-private-key-confirmed").checked = false;
    if ($("economy-wallet-generated-selection-status")) {
      $("economy-wallet-generated-selection-status").textContent = `已選用 ${shortEconomyWalletAddress(draft.address)}`;
    }
    if ($("economy-wallet-use-generated-cold-btn")) {
      $("economy-wallet-use-generated-cold-btn").textContent = "已選用";
      $("economy-wallet-use-generated-cold-btn").disabled = true;
    }
    $("economy-wallet-private-key-confirmed")?.focus();
    economyWalletMsg(`已選用此冷錢包 ${shortEconomyWalletAddress(draft.address)}；確認已保存錢包檔與冷錢包解鎖助記詞後才會綁定。`);
  } catch (err) {
    if (!economyOperationIsCurrent(operation) || err?.name === "AbortError") return;
    economyColdWalletBindCandidate = null;
    if ($("economy-wallet-generated-selection-status")) $("economy-wallet-generated-selection-status").textContent = "選用失敗";
    economyWalletMsg(err.message || "選用冷錢包失敗", false);
  }
}

async function importColdWalletFromInputs() {
  if (!window.crypto?.subtle) {
    economyWalletMsg("此瀏覽器不支援 WebCrypto，無法匯入冷錢包", false);
    return;
  }
  const operation = economyOperationContext();
  try {
    economyColdWalletBindCandidate = null;
    const file = $("economy-wallet-file-input")?.files?.[0] || null;
    const password = $("economy-wallet-file-password")?.value || "";
    if (file) {
      const raw = await economyReadTextFile(file, operation);
      economyAssertOperationCurrent(operation);
      const loaded = await economyLoadEncryptedColdWalletFile(raw, password, {
        imported: true,
        operation,
      });
      economyAssertOperationCurrent(operation);
      economyColdWalletBindCandidate = loaded;
    } else {
      economyWalletMsg("請先選擇冷錢包檔並輸入冷錢包解鎖助記詞。", false);
      $("economy-wallet-file-input")?.focus();
      return;
    }
    economyRememberColdWalletSigningSession(economyColdWalletBindCandidate, password);
    if ($("economy-wallet-private-key-confirmed")) $("economy-wallet-private-key-confirmed").checked = false;
    if ($("economy-wallet-generated-selection-status")) $("economy-wallet-generated-selection-status").textContent = "已改用匯入錢包檔";
    economyWalletMsg("冷錢包已在瀏覽器本機解密。確認已保存錢包檔與冷錢包解鎖助記詞後即可綁定。");
  } catch (err) {
    if (!economyOperationIsCurrent(operation) || err?.name === "AbortError") return;
    economyWalletMsg(err.message || "冷錢包匯入失敗", false);
  }
}

async function startColdWalletImport() {
  const createCard = $("economy-wallet-create-card");
  if (createCard && "open" in createCard) createCard.open = true;
  economyColdWalletBindCandidate = null;
  if ($("economy-wallet-private-key-confirmed")) $("economy-wallet-private-key-confirmed").checked = false;
  const file = $("economy-wallet-file-input")?.files?.[0] || null;
  if (!file) {
    $("economy-wallet-file-input")?.focus();
    economyWalletMsg("請選擇要匯入或恢復的冷錢包檔並輸入冷錢包解鎖助記詞。");
    return;
  }
  await importColdWalletFromInputs();
}

async function confirmColdWalletBinding() {
  const operation = economyOperationContext();
  let scrubSecrets = false;
  try {
    if (!$("economy-wallet-private-key-confirmed")?.checked) {
      economyWalletMsg("請先確認已保存錢包檔與冷錢包解鎖助記詞", false);
      return;
    }
    const file = $("economy-wallet-file-input")?.files?.[0] || null;
    if (file) {
      await importColdWalletFromInputs();
      economyAssertOperationCurrent(operation);
    }
    if (!economyColdWalletBindCandidate) {
      economyWalletMsg("請先匯入錢包檔或選用新冷錢包", false);
      return;
    }
    const walletType = economyColdWalletBindCandidate.imported ? "imported_cold" : "self_custody_cold";
    const payload = await economyBuildWalletBindPayload({
      privateKey: economyColdWalletBindCandidate.privateKey,
      publicJwk: economyColdWalletBindCandidate.publicJwk,
      walletType,
      operation,
    });
    economyAssertOperationCurrent(operation);
    const feePayload = await economyWalletCreationFeePayload(walletType, operation);
    economyAssertOperationCurrent(operation);
    scrubSecrets = true;
    await postEconomyWalletOnboarding({ ...payload, ...feePayload }, operation);
  } catch (err) {
    if (!economyOperationIsCurrent(operation) || err?.name === "AbortError") return;
    economyWalletMsg(err.message || "冷錢包綁定失敗", false);
  } finally {
    if (scrubSecrets && economyOperationIsCurrent(operation)) destroyEconomyColdWalletSecrets();
  }
}

function renderEconomyCatalog(items) {
  economyCatalogCache = Array.isArray(items) ? items : [];
  const list = $("economy-catalog-list");
  if (!list) return;
  if (!items || !items.length) {
    list.innerHTML = `<div class="drive-empty">尚無服務價格</div>`;
    return;
  }
  list.innerHTML = items.map((item) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(item.item_name || item.item_key)}</strong>
        <div class="drive-card-sub">${sanitize(item.category || "-")} · ${Number(item.base_price || 0)} ${formatPointsCurrency(item.currency_type)}</div>
      </div>
      <button class="btn" type="button" data-economy-spend="${sanitize(item.item_key)}">試扣</button>
    </div>
  `).join("");
  list.querySelectorAll("[data-economy-spend]").forEach((btn) => {
    btn.addEventListener("click", () => spendEconomyItem(btn.dataset.economySpend || ""));
  });
}

function exportEconomyLedgerCsv() {
  const rows = economyLedgerCache;
  if (!rows || !rows.length) { economySetMsg("尚無帳本資料可匯出", false); return; }
  const header = ["時間", "方向", "金額", "幣種", "類型", "Ledger UUID", "Ledger Hash"];
  const escape = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [header.map(escape).join(",")];
  for (const row of rows) {
    lines.push([
      row.created_at || "",
      row.direction || "",
      row.amount ?? "",
      row.currency_type || "",
      row.action_type || "",
      row.ledger_uuid || "",
      row.ledger_hash || "",
    ].map(escape).join(","));
  }
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `ledger_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

async function downloadCsvEndpoint(path, label) {
  try {
    const res = await apiFetch(API + path, { credentials: "same-origin" });
    if (!res.ok) {
      const json = await res.clone().json().catch(() => ({}));
      throw new Error(json.msg || json.message || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const disposition = res.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match ? match[1] : `${label.replace(/\s+/g, "_")}_${new Date().toISOString().slice(0, 10)}.csv`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    economySetMsg(`${label} 下載已開始`, true);
  } catch (err) {
    economySetMsg(err.message || `${label} 下載失敗`, false);
  }
}

function downloadEconomyWalletCsv() {
  downloadCsvEndpoint("/points/wallet/export.csv", "積分錢包 CSV");
}

function downloadEconomyTradingCsv() {
  downloadCsvEndpoint("/trading/history/export.csv", "交易紀錄 CSV");
}

function renderEconomyLedger(rows, targetId = "economy-ledger-list") {
  if (targetId === "economy-ledger-list") economyLedgerCache = Array.isArray(rows) ? rows : [];
  const list = $(targetId);
  if (!list) return;
  if (!rows || !rows.length) {
    list.innerHTML = `<div class="drive-empty">尚無帳本紀錄</div>`;
    return;
  }
  list.innerHTML = rows.map((row) => {
    const source = formatEconomyLedgerSource(row);
    const walletFlow = formatEconomyLedgerWalletFlow(row);
    const proofButton = economyChainEnabled()
      ? `<button class="btn" type="button" data-economy-proof="${sanitize(row.ledger_uuid || "")}">Proof</button>`
      : "";
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(formatEconomyLedgerAmount(row))}</strong>
          <div class="drive-card-sub">${sanitize(formatEconomyLedgerAction(row.action_type))} · ${sanitize(row.created_at || "")}</div>
          ${walletFlow ? `<div class="drive-card-sub economy-ledger-wallet-flow">${sanitize(walletFlow)}</div>` : ""}
          ${source ? `<div class="drive-card-sub">${sanitize(source)}</div>` : ""}
          <div class="economy-ledger-hash">Ledger UUID：${sanitize(row.ledger_uuid || row.ledger_hash || "")}</div>
        </div>
        ${proofButton}
      </div>
    `;
  }).join("");
  list.querySelectorAll("[data-economy-proof]").forEach((btn) => {
    btn.addEventListener("click", () => loadEconomyProof(btn.dataset.economyProof || ""));
  });
}

function renderEconomyRootList(rows, targetId, emptyText, renderRow) {
  const list = $(targetId);
  if (!list) return;
  const safeRows = Array.isArray(rows) ? rows.filter((row) => row !== null && row !== undefined) : [];
  if (!safeRows.length) {
    list.innerHTML = `<div class="drive-empty">${sanitize(emptyText)}</div>`;
    return;
  }
  list.innerHTML = safeRows.map(renderRow).join("");
}

function setEconomyChainStatus(text, ok = true) {
  const status = $("economy-chain-status");
  if (!status) return;
  status.textContent = text || "";
  status.className = ok ? "drive-card-sub economy-chain-status ok" : "drive-card-sub economy-chain-status err";
}

function formatEconomyVerificationSummary(verification) {
  const safe = verification && typeof verification === "object" ? verification : {};
  const counts = safe.counts && typeof safe.counts === "object" ? safe.counts : {};
  const state = safe.ok === true ? "全鏈驗證正常" : (safe.ok === false ? "全鏈驗證異常" : "全鏈狀態未知");
  return `${state}：${Number(counts.ledger_entries || 0)} 筆 ledger，${Number(counts.sealed_blocks || 0)} 個封塊，${Number(counts.unsealed_entries || 0)} 筆未封，${Number(counts.audit_events || 0)} 筆審計事件`;
}

function formatEconomyRecoveryResult(result) {
  const safe = result && typeof result === "object" ? result : {};
  const verification = safe.verification && typeof safe.verification === "object" ? safe.verification : {};
  const counts = verification.counts && typeof verification.counts === "object" ? verification.counts : {};
  if (safe.ok !== true) return safe.msg || "PointsChain 恢復失敗";
  return [
    "PointsChain 已完成異常處理檢查",
    "備份還原：停用",
    `ledger：${Number(counts.ledger_entries || 0)} 筆`,
    `封塊：${Number(counts.sealed_blocks || 0)} 個`,
    `safe mode：${(safe.recovery || {}).safe_mode ? "仍啟用" : "已解除"}`,
  ].join("；");
}

function setEconomyText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function economyFormulaCard(label, value, tone = "") {
  const toneClass = tone ? ` ${tone}` : "";
  return `
    <div class="economy-formula-card${toneClass}">
      <span>${sanitize(label)}</span>
      <strong>${sanitize(value)}</strong>
    </div>
  `;
}

function economyFormulaOperator(symbol) {
  return `<div class="economy-formula-operator" aria-hidden="true">${sanitize(symbol)}</div>`;
}

function economyFormulaSection(title, subtitle, cardsHtml, tone = "") {
  const toneClass = tone ? ` ${tone}` : "";
  return `
    <section class="economy-formula-section${toneClass}">
      <div class="economy-formula-section-head">
        <strong>${sanitize(title)}</strong>
        <span>${sanitize(subtitle)}</span>
      </div>
      <div class="economy-formula-section-grid">
        ${cardsHtml}
      </div>
    </section>
  `;
}

function renderEconomyLayerSummary(report) {
  const stats = report?.stats && typeof report.stats === "object" ? report.stats : {};
  const layer = stats.economy_layer && typeof stats.economy_layer === "object" ? stats.economy_layer : {};
  const supply = layer.supply && typeof layer.supply === "object" ? layer.supply : {};
  const funds = layer.funds && typeof layer.funds === "object" ? layer.funds : {};
  const health = layer.health && typeof layer.health === "object" ? layer.health : {};
  const replay = layer.replay && typeof layer.replay === "object" ? layer.replay : {};
  const bridge = layer.supply_equation && typeof layer.supply_equation === "object"
    ? layer.supply_equation
    : (layer.legacy_bridge && typeof layer.legacy_bridge === "object" ? layer.legacy_bridge : {});
  const snapshot = replay.snapshot && typeof replay.snapshot === "object" ? replay.snapshot : {};
  const derivedVerify = replay.derived_verify && typeof replay.derived_verify === "object" ? replay.derived_verify : {};
  const fund = (key) => funds[key] && typeof funds[key] === "object" ? funds[key] : {};
  const fundStatus = (key) => {
    const item = fund(key);
    const custody = item.custody_mode || "system";
    const status = item.wallet_status || item.status || "active";
    const cache = item.derived_cache ? "derived cache" : "ledger replay";
    return `${status} · ${custody} · ${cache}`;
  };
  const fundAddress = (key) => {
    const item = fund(key);
    return item.address || item.wallet_address || item.public_address || item.account_address || "-";
  };
  economyFundAddressCache = {
    mint: fundAddress("mint"),
    official_treasury: fundAddress("official_treasury"),
    promo_fund: fundAddress("promo_fund"),
    exchange_fund: fundAddress("exchange_fund"),
    burn: fundAddress("burn"),
  };
  setEconomyText("economy-layer-health", String(health.status || "-").toUpperCase());
  setEconomyText("economy-layer-health-detail", `原因 ${Array.isArray(health.reasons) ? health.reasons.join(", ") : "ok"}`);
  setEconomyText("economy-layer-max-supply", formatEconomyPointsValue(supply.max_supply || 0));
  setEconomyText("economy-layer-minted-total", `已 Mint ${formatEconomyPointsValue(supply.minted_total || 0)}`);
  setEconomyText("economy-layer-mint-remaining", formatEconomyPointsValue(supply.mint_remaining || 0));
  setEconomyText("economy-layer-releasable-supply", `可釋出上限 ${formatEconomyPointsValue(supply.releasable_supply || 0)}`);
  setEconomyText("economy-layer-releasable-remaining", formatEconomyPointsValue(supply.releasable_remaining || 0));
  setEconomyText("economy-layer-reserved-locked", `保留鎖定 ${formatEconomyPointsValue(supply.reserved_locked || 0)}`);
  setEconomyText("economy-layer-active-supply", formatEconomyPointsValue(supply.active_supply || 0));
  setEconomyText("economy-layer-circulating-supply", `流通 ${formatEconomyPointsValue(supply.circulating_supply || 0)} · fund ${formatEconomyPointsValue(supply.fund_supply || 0)}`);
  setEconomyText("economy-root-wallet-mint-balance", `未發放 ${formatEconomyPointsValue(supply.mint_remaining || 0)}`);
  setEconomyText("economy-root-wallet-mint-status", fundStatus("mint"));
  setEconomyText("economy-root-wallet-mint-address", fundAddress("mint"));
  setEconomyText("economy-root-wallet-official-balance", formatEconomyPointsValue(fund("official_treasury").balance || 0));
  setEconomyText("economy-root-wallet-official-status", fundStatus("official_treasury"));
  setEconomyText("economy-root-wallet-official-address", fundAddress("official_treasury"));
  setEconomyText("economy-root-wallet-promo-balance", formatEconomyPointsValue(fund("promo_fund").balance || 0));
  setEconomyText("economy-root-wallet-promo-status", fundStatus("promo_fund"));
  setEconomyText("economy-root-wallet-promo-address", fundAddress("promo_fund"));
  setEconomyText("economy-root-wallet-exchange-balance", formatEconomyPointsValue(fund("exchange_fund").balance || 0));
  setEconomyText(
    "economy-root-wallet-exchange-status",
    `${fundStatus("exchange_fund")} · 資產 ${formatEconomyPointsValue(supply.exchange_total_assets || fund("exchange_fund").balance || 0)} · 應收 ${formatEconomyPointsValue(supply.exchange_receivable_principal || 0)}`,
  );
  setEconomyText("economy-root-wallet-exchange-address", fundAddress("exchange_fund"));
  setEconomyText("economy-root-wallet-burn-balance", formatEconomyPointsValue(supply.burned_total || fund("burn").balance || 0));
  setEconomyText("economy-root-wallet-burn-status", fundStatus("burn"));
  setEconomyText("economy-root-wallet-burn-address", fundAddress("burn"));
  const formulaEl = $("economy-layer-supply-formula");
  if (formulaEl) {
    const burned = Number(bridge.burned_total ?? supply.burned_total ?? 0);
    const official = Number(bridge.official_treasury_balance ?? fund("official_treasury").balance ?? 0);
    const memberInternal = Number(
      bridge.member_internal_circulating_points
        ?? bridge.legacy_outstanding_points
        ?? 0
    );
    const memberAvailable = Number(bridge.member_internal_available_points ?? 0);
    const memberFrozen = Number(bridge.member_internal_frozen_points ?? 0);
    const rootInternal = Number(bridge.root_internal_circulating_points ?? bridge.root_outstanding_points ?? 0);
    const offWallet = Number(
      bridge.off_wallet_economy_external_points
        ?? supply.external_supply
        ?? supply.circulating_supply
        ?? 0
    );
    const ledgerEconomyGap = Number(bridge.ledger_vs_economy_external_gap_points ?? 0);
    const externalBreakdown = bridge.economy_external_balance_breakdown && typeof bridge.economy_external_balance_breakdown === "object"
      ? bridge.economy_external_balance_breakdown
      : {};
    const exchangeReceivable = Number(externalBreakdown.exchange_principal_receivable_points ?? 0);
    const withheldContra = Number(bridge.exchange_margin_settlement_withheld_contra_points ?? 0);
    const bridgeFlow = bridge.bridge_flow_totals && typeof bridge.bridge_flow_totals === "object"
      ? bridge.bridge_flow_totals
      : {};
    const pc0BridgeOut = Number(bridgeFlow.hot_to_cold_confirmed_points ?? bridgeFlow.economy_hot_to_external_points ?? 0);
    const bridgeDepositIn = Number(bridgeFlow.deposit_credited_points ?? bridgeFlow.economy_external_to_hot_points ?? 0);
    const hotToColdNetworkFee = Number(bridgeFlow.hot_to_cold_network_fee_points ?? 0);
    const depositNetworkFee = Number(bridgeFlow.deposit_network_fee_points ?? 0);
    const fundExternalOut = Number(bridgeFlow.economy_fund_to_external_points ?? 0);
    const flowGap = Number(bridgeFlow.economy_flow_reconciliation_gap_points ?? 0);
    const unexplainedFlowGap = Math.abs(flowGap) > exchangeReceivable
      ? flowGap - Math.sign(flowGap || 1) * exchangeReceivable
      : 0;
    const mintRemaining = Number(bridge.mint_remaining ?? supply.mint_remaining ?? 0);
    const exchange = Number(bridge.exchange_fund_balance ?? fund("exchange_fund").balance ?? 0);
    const promo = Number(bridge.promo_fund_balance ?? fund("promo_fund").balance ?? 0);
    const total = Number(bridge.bridged_supply_equation_total ?? (burned + official + memberInternal + rootInternal + offWallet + mintRemaining + exchange + promo));
    const maxSupply = Number(bridge.max_supply ?? supply.max_supply ?? 0);
    const gap = Number(bridge.bridged_supply_equation_gap_points ?? (total - maxSupply));
    const formulaBalanced = gap === 0;
    const gapTone = formulaBalanced ? "total" : "warning";
    const offWalletDetail = ledgerEconomyGap
      ? `${formatEconomyPointsValue(offWallet)} · PC1 Reserve 對帳差 ${formatEconomyPointsValue(ledgerEconomyGap)}`
      : formatEconomyPointsValue(offWallet);
    const bridgeFlowParts = [
      `PC0 → PC1 已結算出金 ${formatEconomyPointsValue(pc0BridgeOut)}`,
      `PC1 → PC0 已入帳橋接 ${formatEconomyPointsValue(bridgeDepositIn)}`,
    ];
    if (hotToColdNetworkFee) bridgeFlowParts.push(`PC0 → PC1 鏈費 ${formatEconomyPointsValue(hotToColdNetworkFee)}`);
    if (depositNetworkFee) bridgeFlowParts.push(`PC1 → PC0 鏈費 ${formatEconomyPointsValue(depositNetworkFee)}`);
    if (fundExternalOut) bridgeFlowParts.push(`官方基金外部撥出 ${formatEconomyPointsValue(fundExternalOut)}`);
    if (exchangeReceivable) bridgeFlowParts.push(`交易所應收本金 ${formatEconomyPointsValue(exchangeReceivable)}`);
    if (withheldContra) bridgeFlowParts.push(`扣留式費用對帳 ${formatEconomyPointsValue(withheldContra)}`);
    if (unexplainedFlowGap) bridgeFlowParts.push(`未分類橋接流量差 ${formatEconomyPointsValue(unexplainedFlowGap)}`);
    const bridgeFlowDetail = bridgeFlowParts.join(" / ");
    const wrappedOperationalTotal = official + memberInternal + rootInternal + exchange + promo;
    const canonicalActive = Number(bridge.active_supply ?? supply.active_supply ?? 0);
    const canonicalMinted = Number(bridge.minted_total ?? supply.minted_total ?? 0);
    const bridgeTone = ledgerEconomyGap || unexplainedFlowGap ? "warning" : "";
    formulaEl.innerHTML = `
      <div class="economy-supply-title">多帳本結算控制平面</div>
      <div class="drive-card-sub economy-supply-note">
        PC1 canonical reserve、PC0 wrapped operational liabilities、Bridge settlement 與 pending isolation 分層對帳；pending settlement 不與 finalized supply 混算。
      </div>
      <div class="economy-supply-equation-ui economy-supply-layer-grid">
        ${economyFormulaSection(
          "PC1 Canonical Reserve",
          "Settlement ledger / reserve truth",
          `
            ${economyFormulaCard("總上限", formatEconomyPointsValue(maxSupply), "total")}
            ${economyFormulaCard("Active Supply", formatEconomyPointsValue(canonicalActive))}
            ${economyFormulaCard("已 Mint", formatEconomyPointsValue(canonicalMinted))}
            ${economyFormulaCard("系統 burn sink", formatEconomyPointsValue(burned))}
            ${economyFormulaCard("Mint Authority 未發放", formatEconomyPointsValue(mintRemaining))}
          `,
        )}
        ${economyFormulaSection(
          "PC0 Wrapped Operational Supply",
          "站內可用餘額、官方營運錢包與基金 liabilities",
          `
            ${economyFormulaCard("官方 Treasury", formatEconomyPointsValue(official))}
            ${economyFormulaCard("用戶 PC0 站內流通", `${formatEconomyPointsValue(memberInternal)} · 可用 ${formatEconomyPointsValue(memberAvailable)} / 凍結 ${formatEconomyPointsValue(memberFrozen)}`)}
            ${economyFormulaCard("root/其他 PC0 站內餘額", formatEconomyPointsValue(rootInternal))}
            ${economyFormulaCard("交易所基金", formatEconomyPointsValue(exchange))}
            ${economyFormulaCard("PROMO 基金", formatEconomyPointsValue(promo))}
            ${economyFormulaCard("Wrapped liabilities 小計", formatEconomyPointsValue(wrappedOperationalTotal), "total")}
          `,
        )}
        ${economyFormulaSection(
          "Bridge Settlement / Pending Isolation",
          "PC1 ↔ PC0 cross-ledger settlement，不與站內 finalized liability 混為同一層",
          `
            ${economyFormulaCard("External Circulation", offWalletDetail, bridgeTone)}
            ${economyFormulaCard("Bridge finalized flow", bridgeFlowDetail, bridgeTone)}
          `,
          bridgeTone,
        )}
        ${economyFormulaSection(
          "Financial Reconciliation",
          "多帳本 finalized supply equation",
          `
            ${economyFormulaCard("公式總和", formatEconomyPointsValue(total), gapTone)}
            ${economyFormulaCard("差額", `${formatEconomyPointsValue(gap)} · ${formulaBalanced ? "Settlement invariant 正常" : "需查帳"}`, gapTone)}
          `,
          gapTone,
        )}
      </div>
    `;
  }
  setEconomyText("economy-layer-replay-height", formatEconomyPointsValue(replay.height || 0));
  setEconomyText("economy-layer-replay-hash", `derived cache · ${shortEconomyWalletAddress(replay.wallet_root_hash || "")}`);
  setEconomyText("economy-layer-snapshot-height", formatEconomyPointsValue(snapshot.snapshot_height ?? replay.height ?? 0));
  setEconomyText("economy-layer-derived-verify", `${derivedVerify.ok === true ? "verify ok" : "verify failed"} · ${shortEconomyWalletAddress(snapshot.wallet_root_hash || replay.wallet_root_hash || "")}`);
}

function economyGovernanceIncidentRows(governance = {}) {
  const safe = governance && typeof governance === "object" ? governance : {};
  const rows = [];
  const push = (kind, severity, title, detail, meta = {}) => {
    rows.push({ kind, severity, title, detail, meta });
  };
  const riskLabels = Array.isArray(safe.active_risk_labels) ? safe.active_risk_labels.filter((item) => item && typeof item === "object") : [];
  riskLabels.forEach((item) => {
    push(
      "risk_label",
      "high",
      `詐騙 / 風險標記：${shortEconomyWalletAddress(item.wallet_address || "")}`,
      `${item.risk_level || item.label || "risk"} · ${item.reason || ""}`,
      { address: item.wallet_address || "", proposal_uuid: item.proposal_uuid || "", created_at: item.updated_at || item.created_at || "" },
    );
  });
  const freezes = Array.isArray(safe.active_freezes) ? safe.active_freezes.filter((item) => item && typeof item === "object") : [];
  freezes.forEach((item) => {
    push(
      "governance_freeze",
      "critical",
      `治理凍結：${shortEconomyWalletAddress(item.wallet_address || "")}`,
      `禁止轉出 · ${item.reason || ""}`,
      { address: item.wallet_address || "", proposal_uuid: item.freeze_proposal_uuid || "", created_at: item.updated_at || item.created_at || "" },
    );
  });
  const provisional = Array.isArray(safe.active_provisional_freezes) ? safe.active_provisional_freezes.filter((item) => item && typeof item === "object") : [];
  provisional.forEach((item) => {
    push(
      "provisional_freeze",
      "warning",
      `短期審核凍結：${shortEconomyWalletAddress(item.wallet_address || "")}`,
      `禁止轉出 · 到期 ${item.expires_at || "-"} · ${item.reason || ""}`,
      { address: item.wallet_address || "", proposal_uuid: item.linked_proposal_uuid || "", created_at: item.updated_at || item.created_at || "" },
    );
  });
  const branches = Array.isArray(safe.branches) ? safe.branches.filter((item) => item && typeof item === "object") : [];
  branches.forEach((item) => {
    const canonical = Boolean(item.is_canonical);
    const writeEnabled = Boolean(item.write_enabled);
    const status = String(item.status || "");
    if (!canonical || status.includes("recovery") || !writeEnabled) {
      push(
        "branch",
        canonical ? "warning" : "info",
        `${canonical ? "目前 canonical recovery branch" : "非 canonical 舊分支"}：${shortEconomyWalletAddress(item.branch_uuid || "")}`,
        `${status || "-"} · parent ${shortEconomyWalletAddress(item.parent_branch_uuid || "")} · write ${writeEnabled ? "enabled" : "disabled"}`,
        { branch_uuid: item.branch_uuid || "", proposal_uuid: item.proposal_uuid || "", created_at: item.activated_at || item.created_at || "" },
      );
    }
  });
  const auditVerify = safe.audit_verify && typeof safe.audit_verify === "object" ? safe.audit_verify : {};
  if (auditVerify.ok === false) {
    push(
      "governance_audit",
      "critical",
      "治理 audit hash chain 異常",
      `${Number((auditVerify.errors || []).length || 0)} 個 audit verify error`,
      { created_at: "" },
    );
  }
  rows.sort((a, b) => String(b.meta?.created_at || "").localeCompare(String(a.meta?.created_at || "")));
  return rows;
}

function renderEconomyGovernanceIncidents(governance = {}) {
  const rows = economyGovernanceIncidentRows(governance);
  const summary = $("economy-governance-incident-summary");
  if (summary) {
    const riskCount = Array.isArray(governance.active_risk_labels) ? governance.active_risk_labels.length : 0;
    const freezeCount = Array.isArray(governance.active_freezes) ? governance.active_freezes.length : 0;
    const provisionalCount = Array.isArray(governance.active_provisional_freezes) ? governance.active_provisional_freezes.length : 0;
    const branchCount = Array.isArray(governance.branches)
      ? governance.branches.filter((item) => item && (item.is_canonical || item.status !== "main")).length
      : 0;
    summary.textContent = rows.length
      ? `風險標記 ${riskCount} · 治理凍結 ${freezeCount} · 短期凍結 ${provisionalCount} · 分支事件 ${branchCount}`
      : "目前沒有 active 治理風險事件";
    summary.style.color = rows.length ? "#ffb74d" : "var(--muted)";
  }
  renderEconomyRootList(rows, "economy-governance-incident-list", "目前沒有 active 治理風險事件", (row) => {
    const tone = row.severity === "critical" ? "negative" : (row.severity === "warning" || row.severity === "high" ? "warning" : "");
    const address = row.meta?.address || "";
    const branch = row.meta?.branch_uuid || "";
    const query = address || branch || row.meta?.proposal_uuid || "";
    return `
      <div class="drive-file-row">
        <div>
          <strong class="${tone}">${sanitize(row.title || "-")}</strong>
          <div class="drive-card-sub">${sanitize(row.detail || "")}</div>
          <div class="drive-card-sub">${sanitize(row.kind || "-")} · ${sanitize(row.meta?.created_at || "-")} · proposal ${sanitize(shortEconomyWalletAddress(row.meta?.proposal_uuid || ""))}</div>
          ${query ? `<button class="economy-ledger-hash economy-explorer-address" type="button" data-explorer-query="${sanitize(query)}">${sanitize(query)}</button>` : ""}
        </div>
      </div>
    `;
  });
  const list = $("economy-governance-incident-list");
  if (list) {
    list.querySelectorAll("[data-explorer-query]").forEach((btn) => {
      if (btn.dataset.explorerBound === "1") return;
      btn.dataset.explorerBound = "1";
      btn.addEventListener("click", () => {
        const query = btn.dataset.explorerQuery || "";
        if ($("economy-explorer-query")) $("economy-explorer-query").value = query;
        setEconomyActivePage("explorer");
        searchEconomyExplorer(query);
      });
    });
  }
}

function renderEconomyBranchTree(governance = {}) {
  const tree = governance.branch_tree && typeof governance.branch_tree === "object" ? governance.branch_tree : {};
  const nodes = Array.isArray(tree.nodes) ? tree.nodes.filter((item) => item && typeof item === "object") : [];
  const summary = $("economy-branch-tree-summary");
  const list = $("economy-branch-tree-list");
  const canonical = tree.canonical_branch_uuid || "main";
  if (summary) {
    summary.textContent = nodes.length
      ? `canonical ${shortEconomyWalletAddress(canonical)} · 分支 ${Number(tree.node_count || nodes.length)} · archived ${Number(tree.archived_count || 0)} · write-enabled ${Number(tree.write_enabled_count || 0)}`
      : "尚無分支資料，main 為目前 canonical";
  }
  if (!list) return;
  if (!nodes.length) {
    list.innerHTML = `<div class="drive-empty">尚無 recovery branch；main 分支正常運作</div>`;
    return;
  }
  list.innerHTML = nodes.map((node) => {
    const branchUuid = String(node.branch_uuid || "main");
    const parent = String(node.parent_branch_uuid || "");
    const isCanonical = Boolean(node.is_canonical);
    const writeEnabled = Boolean(node.write_enabled);
    const archived = !isCanonical;
    const depth = Math.min(8, Math.max(0, Number(node.depth || 0)));
    const status = String(node.status || node.node_state || "-");
    const openAttr = isCanonical || (!node.auto_collapsed && !archived) ? " open" : "";
    const badgeClass = isCanonical ? "canonical" : (archived ? "archived" : "");
    const label = isCanonical ? "CANONICAL" : (writeEnabled ? "WRITE" : "READ ONLY");
    const incident = node.incident_tx_hash || "";
    const proposal = node.proposal_uuid || "";
    return `
      <details class="economy-branch-node" style="margin-left:${depth * 18}px"${openAttr}>
        <summary>
          <span class="economy-branch-title">
            <span class="economy-branch-badge ${badgeClass}">${sanitize(label)}</span>
            <strong>${sanitize(shortEconomyWalletAddress(branchUuid))}</strong>
            <span class="drive-card-sub">${sanitize(status)}</span>
          </span>
        </summary>
        <div class="economy-branch-meta">
          <div>branch ${sanitize(branchUuid)}</div>
          <div>parent ${sanitize(parent || "-")} · children ${Number(node.children_count || 0)}</div>
          <div>ledger ${Number(node.ledger_count || 0)} · blocks ${Number(node.sealed_block_count || 0)} · unsealed ${Number(node.unsealed_ledger_count || 0)} · economy events ${Number(node.economy_event_count || 0)}</div>
          <div>recovery ${sanitize(node.recovery_type || "-")} · activated ${sanitize(node.activated_at || node.created_at || "-")}</div>
          ${incident ? `<button class="economy-ledger-hash economy-explorer-address" type="button" data-explorer-query="${sanitize(incident)}">incident ${sanitize(shortEconomyWalletAddress(incident))}</button>` : ""}
          ${proposal ? `<div>proposal ${sanitize(shortEconomyWalletAddress(proposal))}</div>` : ""}
        </div>
      </details>
    `;
  }).join("");
  list.querySelectorAll("[data-explorer-query]").forEach((btn) => {
    if (btn.dataset.explorerBound === "1") return;
    btn.dataset.explorerBound = "1";
    btn.addEventListener("click", () => {
      const query = btn.dataset.explorerQuery || "";
      if ($("economy-explorer-query")) $("economy-explorer-query").value = query;
      setEconomyActivePage("explorer");
      searchEconomyExplorer(query);
    });
  });
}

function renderEconomyRootReport(report) {
  const safeReport = report && typeof report === "object" ? report : {};
  renderEconomyLayerSummary(safeReport);
  const verification = safeReport.verification && typeof safeReport.verification === "object" ? safeReport.verification : {};
  const counts = verification.counts && typeof verification.counts === "object" ? verification.counts : {};
  if ($("economy-chain-ok")) {
    $("economy-chain-ok").textContent = verification.ok === true ? "完整" : (verification.ok === false ? "異常" : "未知");
  }
  if ($("economy-chain-counts")) $("economy-chain-counts").textContent = `${Number(counts.ledger_entries || 0)} 筆 ledger`;
  if ($("economy-chain-blocks")) $("economy-chain-blocks").textContent = String(counts.sealed_blocks || 0);
  if ($("economy-chain-unsealed")) $("economy-chain-unsealed").textContent = `未封 ${Number(counts.unsealed_entries || 0)}`;
  if ($("economy-chain-audit-count")) $("economy-chain-audit-count").textContent = String(counts.audit_events || 0);
  startEconomyBlockCountdown(safeReport.block_schedule || null);
  renderEconomyRecovery(safeReport.recovery || {}, safeReport.ledger_backups || []);
  renderEconomyGovernanceIncidents(safeReport.governance || {});
  renderEconomyBranchTree(safeReport.governance || {});
  renderEconomyRootList(safeReport.blocks, "economy-block-list", "尚無封塊", (block) => `
    <div class="drive-file-row">
      <div>
        <strong>#${Number(block.block_number || 0)} · ${Number(block.ledger_count || 0)} 筆</strong>
        <div class="drive-card-sub">${sanitize(block.sealed_at || "")} · ${sanitize(block.anchor_status || "local_only")} · ${sanitize(block.signature_algorithm || "unsigned")}</div>
        <div class="economy-ledger-hash">${sanitize(block.block_hash || "")}</div>
      </div>
    </div>
  `);
  renderEconomyRootList(safeReport.audit_logs, "economy-audit-list", "尚無審計事件", (row) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(row.event_type || "-")} · ${sanitize(row.severity || "-")}</strong>
        <div class="drive-card-sub">${sanitize(row.created_at || "")} · actor=${sanitize(row.actor_user_id || "-")} · target=${sanitize(row.target_user_id || "-")}</div>
        <div class="drive-card-sub">${sanitize(row.message || "")}</div>
      </div>
    </div>
  `);
  renderEconomyRootList(safeReport.high_risk_ledger, "economy-risk-ledger-list", "尚無異常帳本", (row) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(row.verification_status || row.status || "-")} · ${sanitize(formatEconomyLedgerAmount(row))}</strong>
        <div class="drive-card-sub">ledger #${Number(row.id || 0)} · ${sanitize(row.public_account_id || "-")} · ${sanitize(formatEconomyLedgerAction(row.action_type))} · risk=${sanitize(row.risk_flag || "none")}</div>
        ${(Array.isArray(row.verification_errors) ? row.verification_errors : []).map((issue) => `
          <div class="drive-card-sub">
            驗證異常：${sanitize(issue.type || "-")} · ${sanitize(issue.message || "")}
          </div>
          ${issue.expected_ledger_hash || issue.actual_ledger_hash ? `<div class="economy-ledger-hash">expected=${sanitize(issue.expected_ledger_hash || "-")} · actual=${sanitize(issue.actual_ledger_hash || "-")}</div>` : ""}
          ${issue.expected_previous_ledger_hash || issue.actual_previous_ledger_hash ? `<div class="economy-ledger-hash">prev expected=${sanitize(issue.expected_previous_ledger_hash || "-")} · prev actual=${sanitize(issue.actual_previous_ledger_hash || "-")}</div>` : ""}
        `).join("")}
        <div class="economy-ledger-hash">${sanitize(row.ledger_uuid || "")}</div>
      </div>
      <button class="btn" type="button" data-economy-proof="${sanitize(row.ledger_uuid || "")}">Proof</button>
    </div>
  `);
  const riskList = $("economy-risk-ledger-list");
  if (riskList) {
    riskList.querySelectorAll("[data-economy-proof]").forEach((btn) => {
      btn.addEventListener("click", () => loadEconomyProof(btn.dataset.economyProof || ""));
    });
  }
  renderEconomyRootList(safeReport.unsealed_transactions, "economy-unsealed-transaction-list", "目前沒有未封交易", (row) => {
    const hash = row.transaction_hash || row.ledger_hash || row.ledger_uuid || "";
    const finality = row.finality && typeof row.finality === "object" ? row.finality : {};
    const proved = economyTransactionProvedText(finality);
    const status = finality.finality_status || row.status || "unsealed";
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(formatEconomyLedgerAction(row.action_type || "wallet_transfer"))} · ${sanitize(status)} · ${sanitize(proved)}</strong>
          <div class="drive-card-sub">${sanitize(row.source || "unsealed")} · ${sanitize(row.created_at || "")} · Value ${formatEconomyPointsValue(row.amount || 0)} 點</div>
          <button class="economy-ledger-hash economy-explorer-address" type="button" data-explorer-query="${sanitize(hash)}">${sanitize(hash || "-")}</button>
        </div>
      </div>
    `;
  });
  const unsealedList = $("economy-unsealed-transaction-list");
  if (unsealedList) {
    unsealedList.querySelectorAll("[data-explorer-query]").forEach((btn) => {
      if (btn.dataset.explorerBound === "1") return;
      btn.dataset.explorerBound = "1";
      btn.addEventListener("click", () => {
        const query = btn.dataset.explorerQuery || "";
        if ($("economy-explorer-query")) $("economy-explorer-query").value = query;
        setEconomyActivePage("explorer");
        searchEconomyExplorer(query);
      });
    });
  }
  const loadedAt = new Date().toLocaleTimeString("zh-TW", { hour12: false });
  if ($("economy-chain-loaded-at")) $("economy-chain-loaded-at").textContent = `最後更新 ${loadedAt}`;
  setEconomyChainStatus(formatEconomyVerificationSummary(verification), verification.ok !== false);
}

function renderEconomyRecovery(recovery, backups) {
  const safe = recovery && typeof recovery === "object" ? recovery : {};
  const plan = safe.restore_plan && typeof safe.restore_plan === "object" ? safe.restore_plan : {};
  const status = $("economy-recovery-status");
  if (status) {
    status.textContent = safe.safe_mode
      ? `safe mode：啟用 · ${safe.reason || "-"} · forensic=${safe.forensic_bundle_id || "-"}`
      : "safe mode：未啟用";
    status.style.color = safe.safe_mode ? "#ffb74d" : "var(--muted)";
  }
  renderEconomyRootList([plan], "economy-restore-plan-list", "目前沒有恢復方案", (item) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(item.mode === "branch_governance_recovery" ? "分支 / 緊急治理恢復" : "鏈異常處理")}</strong>
        <div class="drive-card-sub">目前 height ${Number(item.current_chain_height || 0)}；備份還原：已停用；wallet 來源：${sanitize(item.wallet_rebuild_source || "append-only ledger replay")}</div>
        <div class="drive-card-sub">下一步：${(Array.isArray(item.next_steps) ? item.next_steps : ["verify_chain", "review_forensic_bundle", "branch_or_governance_correction"]).map((step) => sanitize(step)).join(" / ")}</div>
      </div>
    </div>
  `);
}

async function fetchEconomyJson(url, options = {}) {
  const { allowMissingSnapshot = false, ...requestOptions } = options || {};
  await fetchCsrfToken({ force: true });
  const headers = { ...(requestOptions.headers || {}), "X-CSRF-Token": getCsrfToken() || "" };
  if (requestOptions.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const path = String(url || "");
  const requestUrl = path.startsWith("/api/") ? path : API + path;
  const res = await apiFetch(requestUrl, { credentials: "same-origin", ...requestOptions, headers });
  const json = await res.json().catch(() => ({}));
  if (allowMissingSnapshot && json?.snapshot?.missing) return json;
  if (!res.ok || !json.ok) throw new Error(json.msg || json.message || json.error || `HTTP ${res.status}`);
  return json;
}

async function loadEconomyDashboard() {
  if (!currentUser) return;
  try {
    const rootMode = currentUser === "root";
    const chainFeatureOn = economyChainEnabled();
    syncEconomySubpages(rootMode);
    if ($("economy-user-summary-grid")) $("economy-user-summary-grid").style.display = rootMode ? "none" : "";
    if ($("economy-user-ledger-card")) $("economy-user-ledger-card").style.display = rootMode ? "none" : "";
    setEconomyRootLayout(rootMode);
    if (rootMode) {
      if ($("economy-chain-ok")) $("economy-chain-ok").textContent = chainFeatureOn ? "讀取中" : "已停用";
      if ($("economy-chain-countdown")) $("economy-chain-countdown").textContent = chainFeatureOn ? "封塊進度：讀取中..." : "封塊進度：PointsChain 私有鏈已停用";
      const sidebarPoints = $("sidebar-points");
      if (sidebarPoints) {
        sidebarPoints.dataset.points = "root: server resources";
        updateSidebarIdentity();
      }
      if (chainFeatureOn) {
        const [transactions, onboarding] = await Promise.all([
          fetchEconomyJson("/points/transactions?limit=50&compact=1"),
          fetchEconomyJson("/points/wallet/onboarding"),
        ]);
        renderEconomyWalletOnboarding(onboarding.onboarding || {});
        renderEconomyTransactions(transactions || {});
      } else {
        stopEconomyBlockCountdown();
        renderEconomyTransactions({ transactions: [], summary: {} });
        setEconomyChainStatus("基本積分模式：PointsChain 私有鏈已停用，錢包地址、Explorer、鏈上交易與封塊不會載入。");
      }
    } else {
      stopEconomyBlockCountdown();
      const baseRequests = [
        fetchEconomyJson("/points/wallet"),
        fetchEconomyJson(`/points/ledger?limit=50&offset=${economyLedgerOffset}`),
        fetchEconomyJson("/points/catalog"),
      ];
      const [wallet, ledger, catalog] = await Promise.all(baseRequests);
      const [onboarding, transactions] = chainFeatureOn
        ? await Promise.all([
            fetchEconomyJson("/points/wallet/onboarding"),
            fetchEconomyJson("/points/transactions?limit=50&compact=1"),
          ])
        : [{ onboarding: {} }, { transactions: [], summary: {} }];
      renderEconomyWallet(wallet.wallet);
      renderEconomyWalletOnboarding(onboarding.onboarding || {});
      renderEconomyLedger(ledger.ledger || []);
      renderEconomyCatalog(catalog.catalog || []);
      renderEconomyTransactions(transactions || {});
    }
    const rootCard = $("economy-root-card");
    if (rootCard) rootCard.style.display = currentUser === "root" ? "" : "none";
    const rootReportOk = rootMode && chainFeatureOn ? await loadEconomyRootReport() : true;
    if (chainFeatureOn && economyGovernanceCanManage()) {
      await loadEconomyTreasurySignerCenter({ silent: true });
    } else {
      renderEconomyTreasurySignerCenter(null);
    }
    if (typeof loadTradingDashboard === "function") {
      await loadTradingDashboard();
    }
    if (rootReportOk !== false) {
      economySetMsg(chainFeatureOn ? "" : "基本積分模式：PointsChain 私有鏈已停用；帳本與服務扣點仍可使用。");
    }
    if (chainFeatureOn) {
      await loadEconomyGovernance({ silent: true });
      if (economyActivePage === "governance") await loadEconomyTransactionDisputes({ silent: true });
    }
  } catch (err) {
    economySetMsg(err.message || "PointsChain 讀取失敗", false);
  }
}

async function loadEconomyRootReport() {
  if (currentUser !== "root") return true;
  if (!economyChainEnabled()) {
    stopEconomyBlockCountdown();
    if ($("economy-chain-ok")) $("economy-chain-ok").textContent = "已停用";
    if ($("economy-chain-countdown")) $("economy-chain-countdown").textContent = "封塊進度：PointsChain 私有鏈已停用";
    setEconomyChainStatus("基本積分模式：PointsChain 私有鏈已停用。");
    return true;
  }
  setEconomyChainStatus("讀取 PointsChain 狀態中...");
  try {
    const json = await fetchEconomyJson("/root/points/report");
    if (json.async) {
      setEconomyChainStatus(`PointsChain 報表正在背景更新${json.job_id ? `：${json.job_id}` : ""}`);
      if (json.latest_snapshot_url) {
        try {
          const latest = await fetchEconomyJson(json.latest_snapshot_url);
          renderEconomyRootReport(latest.report || {});
        } catch (_snapshotErr) {
          renderEconomyRootReport({});
        }
      }
      economySetMsg("PointsChain 報表已排入背景任務；完成後會讀取最新快照。");
      return true;
    }
    renderEconomyRootReport(json.report || {});
    economySetMsg("");
    return true;
  } catch (err) {
    stopEconomyBlockCountdown();
    if ($("economy-chain-ok")) $("economy-chain-ok").textContent = "讀取失敗";
    if ($("economy-chain-countdown")) $("economy-chain-countdown").textContent = "封塊進度：讀取失敗";
    setEconomyChainStatus(err.message || "PointsChain 狀態讀取失敗", false);
    economySetMsg(err.message || "PointsChain 狀態讀取失敗", false);
    return false;
  }
}

async function spendEconomyItem(itemKey) {
  if (!itemKey) return;
  const item = economyCatalogCache.find((entry) => String(entry.item_key || "") === String(itemKey));
  const quantity = 1;
  const amount = Math.floor(Number(item?.base_price || 0)) * quantity;
  const chainFeatureOn = economyChainEnabled();
  let sourceWallet = "";
  if (chainFeatureOn) {
    const defaultWallet = readEconomyDefaultSpendWalletAddress();
    sourceWallet = window.prompt("請確認本次付款錢包地址；可改用其他錢包。", defaultWallet || "");
    if (sourceWallet === null) {
      economySetMsg("已取消付款");
      return;
    }
  }
  if (!Number.isFinite(amount) || amount <= 0) {
    economySetMsg("找不到服務價格，付款未送出", false);
    return;
  }
  const requestUuid = economyRequestId("points_service_fee");
  const referenceType = "price_catalog";
  const referenceId = `catalog:${itemKey}`;
  try {
    if (chainFeatureOn && sourceWallet && !String(sourceWallet).startsWith("pc0")) {
      economySetMsg("冷錢包直接服務付款已停用；請先入金到 pc0 站內託管錢包後再支付。", false);
      return;
    }
    const signature = chainFeatureOn
      ? await economyBuildServiceFeeSignature({
          source: sourceWallet,
          itemKey,
          quantity,
          amount,
          requestUuid,
          referenceType,
          referenceId,
        })
      : "";
    const json = await fetchEconomyJson("/points/spend", {
      method: "POST",
      body: JSON.stringify({
        item_key: itemKey,
        quantity,
        request_uuid: requestUuid,
        signature,
        source_wallet_address: sourceWallet,
      }),
    });
    renderEconomyWallet(json.wallet);
    await loadEconomyDashboard();
    const charge = json.charge || {};
    const settlement = json.settlement || {};
    const msg = settlement.created
      ? `服務費已由站內託管錢包即時結算：${formatEconomyPointsValue(settlement.settled_amount_points || amount)} 點`
      : chainFeatureOn
        ? `服務費已由站內託管錢包即時扣款：${formatEconomyPointsValue(charge.amount_points || amount)} 點`
        : `服務費已由基本積分帳本即時扣款：${formatEconomyPointsValue(charge.amount_points || amount)} 點`;
    economySetMsg(msg);
  } catch (err) {
    economyNotifyFailure(err, {
      msgFn: economySetMsg,
      label: "服務費付款",
      fallback: "冷錢包本機簽署或服務費付款失敗，服務費未送出",
    });
  }
}

function economyRootOfficialGrantMsg(text, ok = true) {
  economyInlineMsg("economy-root-official-grant-msg", text, ok, "官方 Treasury 撥款提案");
}

function economyGovernanceStatusLabel(status) {
  const labels = {
    voting: "投票中",
    passed: "已通過",
    rejected: "已否決",
    expired: "已過期",
    executed: "已執行",
    cancelled: "已取消",
  };
  return labels[String(status || "")] || String(status || "-");
}

function economyGovernanceTypeLabel(type) {
  if (type === "scam_address_label") return "詐騙地址標記";
  if (type === "freeze_wallet_address") return "治理凍結地址";
  if (type === "unfreeze_wallet_address") return "治理解除凍結";
  if (type === "emergency_recovery_branch") return "緊急 recovery branch";
  return String(type || "-");
}

function economyGovernanceActionLabel(action) {
  const labels = {
    MARK_SCAM: "標記詐騙",
    FREEZE_ADDRESS: "凍結地址",
    UNFREEZE_ADDRESS: "解除凍結",
    ROLLBACK_BRANCH: "Rollback 分支",
    EMERGENCY_LOCKDOWN: "緊急鎖定",
    AUTO_BURN_POLICY: "自動銷毀政策",
    MINT_REQUEST: "Mint 申請",
    TREASURY_TRANSFER: "官方撥款",
    EXCHANGE_FUND_REPLENISH: "撥補交易所基金",
    CONTEST_REWARD_PAYOUT: "競賽獎金",
    TREASURY_SIGNER_CHANGE: "官方簽署者變更",
    PARAMETER_CHANGE: "參數變更",
    SUPPLY_EXPANSION_REQUEST: "憲法級增發條款",
    FEATURE_ACTIVATION: "功能啟用",
    HARD_FORK_ACCEPTANCE: "Hard fork 接受",
  };
  return labels[String(action || "").toUpperCase()] || economyGovernanceTypeLabel(action);
}

function economyGovernanceCanManage() {
  return currentUser === "root" || (typeof clientRoleRank === "function" && clientRoleRank(currentRole || "user") >= clientRoleRank("manager"));
}

function economyCurrentMemberLevel() {
  const effective = $("sidebar-effective-level")?.dataset?.effectiveLevel || "";
  const level = $("sidebar-current-level")?.dataset?.memberLevel || "";
  const raw = String(effective || level || "").split("/")[0].trim().toLowerCase();
  if (["newbie", "normal", "trusted", "vip", "restricted", "suspended"].includes(raw)) return raw;
  return "";
}

function economyGovernanceCanProposePublic() {
  if (economyGovernanceCanManage()) return true;
  const rank = { suspended: -2, restricted: -1, newbie: 0, normal: 1, trusted: 2, vip: 3 };
  const level = economyCurrentMemberLevel() || "normal";
  return (rank[level] || 0) >= rank.trusted;
}

function economyGovernanceCategoryForProposal(proposal = {}) {
  if (String(proposal.reference || "").startsWith("transaction_dispute:")) return "dispute";
  const domain = String(proposal.governance_domain || "").toUpperCase();
  const action = String(proposal.action_type || proposal.proposal_type || "").toUpperCase();
  if (action === "MINT_REQUEST") return "mint";
  if (domain === "OFFICIAL_TREASURY" || ["TREASURY_TRANSFER", "EXCHANGE_FUND_REPLENISH", "CONTEST_REWARD_PAYOUT"].includes(action)) return "treasury";
  if (domain === "EMERGENCY_SECURITY" || ["ROLLBACK_BRANCH", "EMERGENCY_LOCKDOWN"].includes(action)) return "emergency";
  if (String(proposal?.payload?.proposal_type || "").toUpperCase() === "SUPPLY_EXPANSION_REQUEST") return "policy";
  if (domain === "PROTOCOL_PARAMETER" || domain === "ADMIN_POLICY" || ["PARAMETER_CHANGE", "SUPPLY_EXPANSION_REQUEST", "FEATURE_ACTIVATION", "AUTO_BURN_POLICY", "TREASURY_SIGNER_CHANGE", "HARD_FORK_ACCEPTANCE"].includes(action)) return "policy";
  return "public";
}

function economyProposalUuidsForDispute(row = {}) {
  return [
    row.governance_proposal_uuid,
    row.address_risk_proposal_uuid,
    row.address_freeze_proposal_uuid,
  ].map((item) => String(item || "").trim()).filter(Boolean);
}

function setEconomyGovernanceCategory(category) {
  const next = ["all", "dispute", "emergency", "treasury", "mint", "policy"].includes(String(category || ""))
    ? String(category || "")
    : "all";
  economyGovernanceCategory = next;
  const select = $("economy-governance-category-select");
  if (select && select.value !== next) select.value = next;
}

function setEconomyGovernanceStatusFilter(status) {
  const next = ["review", "voting", "closed"].includes(String(status || ""))
    ? String(status || "")
    : "review";
  economyGovernanceStatusFilter = next;
  document.querySelectorAll("[data-governance-status-filter]").forEach((btn) => {
    const active = String(btn.dataset.governanceStatusFilter || "") === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function economyGovernanceStatusBucket(proposal = {}) {
  const rawStatus = String(proposal.status || "").trim().toLowerCase();
  const lifecycle = String(proposal.lifecycle_status || "").trim().toUpperCase();
  if (rawStatus === "voting" || lifecycle === "VOTING") return "voting";
  if (
    ["executed", "failed", "rejected", "vetoed", "expired", "cancelled", "canceled"].includes(rawStatus)
    || ["EXECUTED", "FAILED", "REJECTED", "VETOED", "EXPIRED", "CANCELLED", "CANCELED"].includes(lifecycle)
  ) {
    return "closed";
  }
  return "review";
}

function economyGovernanceStatusFilterLabel(status) {
  if (status === "voting") return "投票中";
  if (status === "closed") return "已結案";
  return "審核中";
}

function economyGovernanceSignerAddress(proposal = {}) {
  const policy = proposal.multisig?.policy || {};
  if (policy.current_signer_wallet_address) return String(policy.current_signer_wallet_address || "").trim().toLowerCase();
  const signers = Array.isArray(policy.signers) ? policy.signers : [];
  const signer = signers.find((item) => Number(item.user_id || 0) === Number(currentUserId || 0));
  return String(signer?.wallet_address || "").trim().toLowerCase();
}

function economyGovernanceProgress(proposal = {}) {
  const yes = Number(proposal.yes_count || 0);
  const no = Number(proposal.no_count || 0);
  const abstain = Number(proposal.abstain_count || 0);
  const quorum = Number(proposal.quorum_count || 0);
  const eligible = Number(proposal.eligible_voter_count || 0);
  const approval = (Number(proposal[`approval_${ECONOMY_GOV_RATE_UNIT_SUFFIX}`] || 0) / 100).toFixed(2);
  return `YES ${yes} / NO ${no} / ABSTAIN ${abstain} · quorum ${yes + no + abstain}/${quorum} of ${eligible} · approval ${approval}%`;
}

function economyGovernanceTimeline(proposal = {}) {
  const domain = String(proposal.governance_domain || "");
  const status = String(proposal.lifecycle_status || proposal.status || "").toUpperCase();
  const steps = ["REVIEW", "VOTING", "PASSED", "TIMELOCK", domain === "OFFICIAL_TREASURY" ? "MULTISIG" : "EXECUTE", "EXECUTED"];
  return steps.map((step) => step === status || (status === "QUEUED" && step === "TIMELOCK") ? `[${step}]` : step).join(" -> ");
}

function economyGovernanceReadiness(proposal = {}) {
  const readiness = proposal.execution_readiness && typeof proposal.execution_readiness === "object" ? proposal.execution_readiness : {};
  const marks = [
    ["quorum", readiness.quorum_reached],
    ["vote", readiness.vote_succeeded],
    ["timelock", readiness.timelock_finished],
    ["payload", readiness.payload_verified],
    ["multisig", readiness.multisig_ready],
  ];
  return marks.map(([label, ok]) => `${ok ? "OK" : "WAIT"} ${label}`).join(" · ");
}

function economyRenderEvidenceRefs(refs = []) {
  const list = Array.isArray(refs) ? refs : [];
  if (!list.length) return "<span class=\"drive-card-sub\">未提供佐證 refs。</span>";
  return `<ul class="drive-card-sub" style="margin:.25rem 0 .35rem 1.1rem;">${list.map((item) => `<li>${sanitize(item)}</li>`).join("")}</ul>`;
}

function economyRenderDisputeGovernanceMaterials(payload = {}) {
  const victimStatement = String(payload.victim_statement || "").trim();
  const victimEvidence = Array.isArray(payload.victim_evidence_refs) ? payload.victim_evidence_refs : [];
  const claims = Array.isArray(payload.victim_claims) ? payload.victim_claims : [];
  const reply = payload.counterparty_reply && typeof payload.counterparty_reply === "object" ? payload.counterparty_reply : {};
  const hasReply = Boolean(String(reply.statement || "").trim());
  if (!victimStatement && !victimEvidence.length && !claims.length && !hasReply) return "";
  const claimRows = claims.map((claim) => `
    <div class="drive-card-sub">
      From claim ${sanitize(formatEconomyWalletAddressWithManagerLabel(claim.wallet_address || ""))} · ${formatEconomyPointsValue(claim.claim_amount_points || 0)} 點 · proof ${claim.address_signature_verified ? "ok" : "missing"}
    </div>
  `).join("");
  return `
    <div class="economy-dispute-governance-materials" style="margin:.45rem 0;padding:.55rem;border:1px solid rgba(255,255,255,.12);border-radius:8px;background:rgba(0,0,0,.12);">
      <div class="drive-card-sub" style="font-weight:700;color:var(--text);">疑義交易雙方材料</div>
      ${victimStatement ? `<div class="drive-card-sub">From 主張：${sanitize(victimStatement)}</div>` : ""}
      ${claimRows}
      <div class="drive-card-sub">From 佐證：</div>
      ${economyRenderEvidenceRefs(victimEvidence)}
      ${hasReply ? `
        <div class="drive-card-sub">To 回覆：${sanitize(reply.statement || "")}</div>
        <div class="drive-card-sub">To 地址 ${sanitize(formatEconomyWalletAddressWithManagerLabel(reply.wallet_address || ""))} · proof ${reply.address_signature_verified ? "ok" : "missing"} · ${sanitize(reply.reply_created_at || "")}</div>
        <div class="drive-card-sub">To 佐證：</div>
        ${economyRenderEvidenceRefs(reply.evidence_refs || [])}
      ` : `<div class="drive-card-sub">To 尚未回覆；投票者應將未回覆狀態納入判斷。</div>`}
    </div>
  `;
}

function renderEconomyGovernanceFromCache() {
  renderEconomyGovernance({ proposals: Array.from(economyGovernanceProposalCache.values()) });
}

function toggleEconomyGovernanceProposal(proposalUuid) {
  const uuid = String(proposalUuid || "").trim();
  if (!uuid) return;
  if (economyExpandedGovernanceProposalUuids.has(uuid)) {
    economyExpandedGovernanceProposalUuids.delete(uuid);
  } else {
    economyExpandedGovernanceProposalUuids.add(uuid);
  }
  renderEconomyGovernanceFromCache();
}

function economyRenderGovernanceProposalCard(proposal = {}, { nested = false } = {}) {
  const proposalUuid = String(proposal.proposal_uuid || "").trim();
  const status = String(proposal.status || "");
  const target = proposal.target_wallet_address || proposal.incident_tx_hash || "";
  const targetDisplay = String(target || "").startsWith("pc1")
    ? formatEconomyWalletAddressWithManagerLabel(target, { short: false })
    : target;
  const vote = proposal.user_vote?.vote ? `你已投 ${proposal.user_vote.vote}` : "尚未投票";
  const expanded = proposalUuid && economyExpandedGovernanceProposalUuids.has(proposalUuid);
  const multisig = proposal.multisig && typeof proposal.multisig === "object" ? proposal.multisig : {};
  const multisigText = multisig.required
    ? `多簽 ${Number(multisig.signature_count || 0)}/${Number(multisig.threshold || 0)} · 權重 ${Number(multisig.signature_weight || 0)}/${Number(multisig.threshold_weight || 0)}${multisig.ready ? " · 可執行" : ""}`
    : "不需要多簽";
  const signerAddress = economyGovernanceSignerAddress(proposal);
  const signerMeta = Array.isArray(multisig.policy?.signers)
    ? multisig.policy.signers.find((item) => String(item.wallet_address || "").trim().toLowerCase() === signerAddress)
    : null;
  const rootVeto = currentUser === "root" && proposal.root_veto_allowed && !proposal.root_veto_used && !["executed", "cancelled", "expired"].includes(status)
    ? `<button class="btn btn-danger btn-sm" type="button" data-governance-veto="${sanitize(proposal.proposal_uuid || "")}">VETO</button>`
    : "";
  const sponsor = economyGovernanceCanManage() && proposal.lifecycle_status === "REVIEW"
    ? `<button class="btn btn-sm" type="button" data-governance-sponsor="${sanitize(proposal.proposal_uuid || "")}">Sponsor</button>`
    : "";
  const cancel = economyGovernanceCanManage() && !["executed", "cancelled", "expired"].includes(status)
    ? `<button class="btn btn-sm" type="button" data-governance-cancel="${sanitize(proposal.proposal_uuid || "")}">取消</button>`
    : "";
  const multisigSign = economyGovernanceCanManage() && multisig.required && !multisig.ready && signerAddress && status === "passed"
    ? `<button class="btn btn-sm" type="button" data-governance-multisig-sign="${sanitize(proposal.proposal_uuid || "")}" data-signer-wallet="${sanitize(signerAddress)}" data-target-wallet="${sanitize(proposal.target_wallet_address || signerAddress)}" data-requested-amount="${Number(proposal.requested_amount || 1)}" data-payload-hash="${sanitize(proposal.execution_payload_hash || "")}" data-custody-mode="${sanitize(signerMeta?.custody_mode || "")}" data-wallet-type="${sanitize(signerMeta?.wallet_type || "")}">簽署多簽</button>`
    : "";
  const rootExecute = economyGovernanceCanManage() && proposal.executable
    ? `<button class="btn btn-danger btn-sm" type="button" data-governance-execute="${sanitize(proposal.proposal_uuid || "")}">執行</button>`
    : "";
  const votingButtons = status === "voting" && proposal.lifecycle_status === "VOTING"
    ? `
      <button class="btn btn-sm" type="button" data-governance-vote="yes" data-proposal-uuid="${sanitize(proposal.proposal_uuid || "")}">YES</button>
      <button class="btn btn-sm" type="button" data-governance-vote="no" data-proposal-uuid="${sanitize(proposal.proposal_uuid || "")}">NO</button>
      <button class="btn btn-sm" type="button" data-governance-vote="abstain" data-proposal-uuid="${sanitize(proposal.proposal_uuid || "")}">ABSTAIN</button>
    `
    : "";
  const proposalPayload = proposal.payload && typeof proposal.payload === "object" ? proposal.payload : {};
  const recoveryOptions = proposalPayload.recovery_options && typeof proposalPayload.recovery_options === "object"
    ? Object.keys(proposalPayload.recovery_options).join(" / ")
    : "";
  const claims = Array.isArray(proposalPayload.victim_claims) ? proposalPayload.victim_claims : [];
  const disputeMaterials = economyRenderDisputeGovernanceMaterials(proposalPayload);
  const recoveryDetail = String(proposal.action_type || "").toUpperCase() === "ROLLBACK_BRANCH"
    ? `<div class="drive-card-sub">recovery ${sanitize(proposalPayload.recovery_strategy || proposalPayload.selected_recovery_strategy || "-")} · options ${sanitize(recoveryOptions || "-")} · claims ${claims.length}</div>`
    : "";
  const actionLabel = String(proposalPayload.proposal_type || "").toUpperCase() === "SUPPLY_EXPANSION_REQUEST"
    ? economyGovernanceActionLabel("SUPPLY_EXPANSION_REQUEST")
    : economyGovernanceActionLabel(proposal.action_type || proposal.proposal_type);
  const actionPanel = [votingButtons, sponsor, multisigSign, rootVeto, cancel, rootExecute].filter(Boolean).join("");
  return `
    <div class="economy-governance-proposal-card${nested ? " economy-governance-inline-proposal" : ""}">
      <div class="drive-file-row economy-governance-proposal-toggle" role="button" tabindex="0" aria-expanded="${expanded ? "true" : "false"}" data-governance-toggle-proposal="${sanitize(proposalUuid)}">
        <div>
          <strong>${sanitize(actionLabel)} · ${sanitize(proposal.governance_domain || "-")} · ${sanitize(proposal.lifecycle_status || economyGovernanceStatusLabel(status))}</strong>
          <div class="drive-card-sub">${sanitize(proposal.reason || "")}</div>
          <div class="drive-card-sub">${sanitize(economyGovernanceProgress(proposal))}</div>
          <div class="drive-card-sub">timeline ${sanitize(economyGovernanceTimeline(proposal))}</div>
          <div class="drive-card-sub">readiness ${sanitize(economyGovernanceReadiness(proposal))}</div>
          ${recoveryDetail}
          <div class="drive-card-sub">severity ${sanitize(proposal.proposal_severity || "NORMAL")} · ${sanitize(multisigText)} · veto ${proposal.root_veto_allowed ? "root allowed" : "not allowed"}</div>
          ${proposal.execution_payload_hash ? `<div class="drive-card-sub">payload hash <span class="economy-ledger-hash">${sanitize(proposal.execution_payload_hash)}</span></div>` : ""}
          <div class="drive-card-sub">${sanitize(proposal.impact_scope || "")}${proposal.risk_summary ? " · " + sanitize(proposal.risk_summary) : ""}</div>
          <div class="economy-ledger-hash">${sanitize(targetDisplay || proposal.proposal_uuid || "")}</div>
          <div class="drive-card-sub">timelock ${sanitize(proposal.timelock_until || "-")} · expires ${sanitize(proposal.expires_at || "-")} · ${sanitize(vote)}</div>
        </div>
        <div class="drive-file-actions">
          <span class="btn btn-sm" aria-hidden="true">${expanded ? "收合操作" : "展開投票/操作"}</span>
        </div>
      </div>
      ${expanded ? `
        <div class="economy-governance-proposal-action-panel" style="margin:.35rem 0 .85rem 1rem;padding:.65rem;border-left:3px solid rgba(255,255,255,.18);background:rgba(255,255,255,.04);">
          <div class="drive-card-sub" style="margin-bottom:.45rem;">提案投票 / 執行操作</div>
          ${disputeMaterials}
          <div class="drive-file-actions" style="justify-content:flex-start;gap:.45rem;flex-wrap:wrap;">
            ${actionPanel || `<span class="drive-card-sub">目前沒有可執行操作。</span>`}
          </div>
        </div>
      ` : ""}
    </div>
  `;
}

function parseEconomyGovernanceLines(value) {
  return String(value || "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function parseEconomyGovernanceClaims(value) {
  const raw = String(value || "").trim();
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error("claims JSON must be an array");
    return parsed;
  } catch (err) {
    throw new Error(`Claims JSON 格式錯誤：${err.message || err}`);
  }
}

function economySignedPointsValue(value) {
  const amount = Number(value || 0);
  return `${amount > 0 ? "+" : ""}${formatEconomyPointsValue(amount)}`;
}

function economyTreasuryFlowStatusLabel(flow, analysis) {
  const status = String(flow?.status || analysis?.status || "unknown").toLowerCase();
  if (status === "green") return "收支平衡";
  if (status === "yellow") return "需留意支出";
  if (status === "red") return "資金不足";
  return "待確認";
}

function economyFlowMeterHtml({ inflow = 0, outflow = 0, net = 0, leftLabel = "流出", rightLabel = "流入" } = {}) {
  const maxValue = Math.max(1, Number(inflow || 0), Number(outflow || 0), Math.abs(Number(net || 0)));
  const inWidth = Math.min(100, Math.round((Number(inflow || 0) / maxValue) * 100));
  const outWidth = Math.min(100, Math.round((Number(outflow || 0) / maxValue) * 100));
  const netClass = Number(net || 0) >= 0 ? "finance-flow-net-positive" : "finance-flow-net-negative";
  return `
    <div class="finance-flow-meter-bar" aria-label="${sanitize(leftLabel)}與${sanitize(rightLabel)}比較">
      <div class="finance-flow-meter-side out"><div class="finance-flow-meter-fill" style="width:${outWidth}%"></div></div>
      <div class="finance-flow-meter-side in"><div class="finance-flow-meter-fill" style="width:${inWidth}%"></div></div>
    </div>
    <div class="finance-flow-meter-labels">
      <span>${sanitize(leftLabel)} ${formatEconomyPointsValue(outflow)}</span>
      <strong class="${netClass}">淨額 ${economySignedPointsValue(net)}</strong>
      <span>${sanitize(rightLabel)} ${formatEconomyPointsValue(inflow)}</span>
    </div>
  `;
}

function economyTreasuryFlowRow(row, maxAbs = 1) {
  const inflow = Number(row?.inflow_points || row?.amount_points || 0);
  const outflow = Number(row?.outflow_points || 0);
  const net = Number(row?.net_points ?? (inflow - outflow));
  const direction = net < 0 || String(row?.direction || "") === "outflow" ? "outflow" : (String(row?.direction || "") === "pending" ? "pending" : "inflow");
  const amountText = direction === "outflow"
    ? `-${formatEconomyPointsValue(outflow || Math.abs(net))}`
    : economySignedPointsValue(inflow || net);
  const meta = [
    row?.category_key || row?.transaction_type || row?.item_key || "",
    `${Number(row?.event_count ?? row?.count ?? row?.charge_count ?? 0)} 筆`,
    row?.latest_at || row?.last_activity_at || "",
    row?.ledger_only ? "legacy ledger 補列" : "",
    row?.reserved_points ? `待處理 ${formatEconomyPointsValue(row.reserved_points)} 點` : "",
    row?.cancelled_points ? `取消 ${formatEconomyPointsValue(row.cancelled_points)} 點` : "",
  ].filter(Boolean).join(" · ");
  return `
    <div class="finance-flow-tile economy-flow-row-${sanitize(direction)}" data-flow-direction="${sanitize(direction)}">
      <strong>${sanitize(row?.label || row?.item_name || row?.transaction_type || row?.item_key || "-")}</strong>
      <b>${sanitize(amountText)} 點</b>
      <small>${sanitize(meta || "-")}</small>
    </div>
  `;
}

function renderEconomyTreasuryFlowList(id, title, subtitle, rows, emptyText) {
  const list = $(id);
  if (!list) return;
  const items = Array.isArray(rows) ? rows : [];
  const maxAbs = items.reduce((max, row) => Math.max(max, Math.abs(Number(row?.net_points || row?.amount_points || 0)), Number(row?.inflow_points || 0), Number(row?.outflow_points || 0)), 1);
  list.innerHTML = `
    <div class="finance-flow-list-heading">
      <div>
        <strong>${sanitize(title)}</strong>
        <small>${sanitize(subtitle || "")}</small>
      </div>
    </div>
    ${items.length ? items.slice(0, 20).map((row) => economyTreasuryFlowRow(row, maxAbs)).join("") : `<div class="drive-empty">${sanitize(emptyText)}</div>`}
  `;
}

function renderEconomyTreasuryAnalysis(payload) {
  const analysis = payload && typeof payload === "object" ? payload : {};
  const summary = analysis.summary && typeof analysis.summary === "object" ? analysis.summary : {};
  const flow = analysis.flow_summary && typeof analysis.flow_summary === "object" ? analysis.flow_summary : {};
  const settlement = analysis.settlement_policy && typeof analysis.settlement_policy === "object" ? analysis.settlement_policy : {};
  const period = analysis.period && typeof analysis.period === "object" ? analysis.period : {};
  const periodText = period.label ? `本月 ${period.label}` : "本月";
  const inflow = Number(flow.total_inflow_points ?? summary.income_total_points ?? 0);
  const outflow = Number(flow.total_outflow_points ?? summary.expense_total_points ?? 0);
  const net = Number(flow.net_flow_points ?? summary.net_points ?? (inflow - outflow));
  const balance = Number(flow.current_balance_points ?? summary.official_wallet_balance_points ?? 0);
  const mintAllocation = Number(summary.non_operating_mint_allocation_points || 0);
  setEconomyText("economy-treasury-analysis-updated-at", analysis.generated_at ? `${periodText} · 最後更新 ${analysis.generated_at}` : "等待即時資料");
  const categories = Array.isArray(flow.categories) ? flow.categories : [];
  const summaryEl = $("economy-treasury-analysis-summary");
  if (summaryEl) {
    summaryEl.innerHTML = `
      <div class="finance-flow-panel-head">
        <div>
          <strong>官方財庫本月收支</strong>
          <small>類似健康度的站內錢包流量檢視：左側流出、右側流入，中間看淨額。</small>
        </div>
        <span>${sanitize(economyTreasuryFlowStatusLabel(flow, analysis))}</span>
      </div>
      <div class="finance-flow-meter">
        ${economyFlowMeterHtml({ inflow, outflow, net, leftLabel: "官方錢包流出", rightLabel: "官方錢包流入" })}
      </div>
      <div class="economy-flow-summary">
        <div><span>流入總額</span><strong>${formatEconomyPointsValue(inflow)} 點</strong><small>服務費、投幣抽成與治理收入</small></div>
        <div><span>流出總額</span><strong>${formatEconomyPointsValue(outflow)} 點</strong><small>撥款、補助與事故賠付</small></div>
        <div><span>收支淨額</span><strong>${economySignedPointsValue(net)} 點</strong><small>${sanitize(periodText)}淨流量</small></div>
        <div><span>官方錢包餘額</span><strong>${formatEconomyPointsValue(balance)} 點</strong><small>${sanitize(economyTreasuryFlowStatusLabel(flow, analysis))}</small></div>
      </div>
      <div class="economy-flow-note">官方錢包收支由 fund ledger / pc0 站內帳本 replay；pc0 服務費收入、影音投幣抽成會列入官方 Treasury，鏈上交易 fee 與加速費仍進 BURN，不算官方收益。Mint 發行撥補${mintAllocation ? ` ${formatEconomyPointsValue(mintAllocation)} 點` : ""}只列為供給/撥補，不列入營運收入。</div>
    `;
  }
  const recent = Array.isArray(analysis.recent_service_fee_revenue_ledgers)
    ? analysis.recent_service_fee_revenue_ledgers
    : (Array.isArray(analysis.recent_service_fee_settlements) ? analysis.recent_service_fee_settlements : []);
  const serviceList = $("economy-treasury-service-fee-list");
  if (serviceList) {
    serviceList.innerHTML = `
      <div class="economy-flow-list-heading">
        <div>
          <strong>${sanitize(periodText)}站內服務費收入規則</strong>
          <small>策略 ${sanitize(settlement.service_fee_layer || "-")} · ${sanitize(settlement.service_fee_ledger_action || "-")} → ${sanitize(settlement.service_fee_destination_fund_key || "-")}</small>
        </div>
      </div>
      <div class="economy-flow-note">${sanitize(settlement.note || "")} 計費單價請到系統管理「伺服器設定 > 計費」或各功能右上快速設定調整。</div>
      ${recent.length ? recent.slice(0, 4).map((row) => economyTreasuryFlowRow({
        label: "最近服務費收入帳本",
        category_key: shortEconomyWalletAddress(row.ledger_uuid || ""),
        direction: "inflow",
        inflow_points: Number(row.amount_points || 0),
        net_points: Number(row.amount_points || 0),
        event_count: Number(row.charge_count || 0),
        latest_at: row.created_at || "",
      }, Math.max(1, ...recent.map((item) => Number(item.amount_points || 0))))).join("") : `<div class="drive-empty">尚無站內服務費資料。</div>`}
    `;
  }
  const inflowRows = categories.filter((row) => Number(row?.inflow_points || 0) > 0 || String(row?.direction || "") === "inflow");
  const outflowRows = categories.filter((row) => Number(row?.outflow_points || 0) > 0 || String(row?.direction || "") === "outflow");
  const serviceRows = Array.isArray(analysis.service_fee_flow_categories) ? analysis.service_fee_flow_categories : [];
  renderEconomyTreasuryFlowList("economy-treasury-income-list", "官方錢包流入", "依官方 Treasury fund ledger 分類。", inflowRows, "目前沒有可見官方 Treasury 流入。");
  renderEconomyTreasuryFlowList("economy-treasury-expense-list", "官方錢包流出", "依官方 Treasury fund ledger 分類。", outflowRows, "目前沒有可見官方 Treasury 流出。");
  renderEconomyTreasuryFlowList("economy-treasury-monthly-feature-list", "積分類型 / 各功能服務費收入", "只統計已入帳至官方 Treasury 的 pc0 站內服務費；這裡是看板，不提供定價調整。", serviceRows, "本月尚無站內服務費收入。");
}

function economyTreasurySignerRoleLabel(role) {
  const value = String(role || "").trim();
  if (value === "super_admin") return "最高管理者";
  if (value === "manager") return "管理者";
  return value || "-";
}

function economyTreasurySignerWeightLabel(weight) {
  return `簽署權重 ${Number(weight || 0)}`;
}

function renderEconomyTreasurySignerCenter(payload = null) {
  const card = $("economy-treasury-signer-center-card");
  const managerCard = $("economy-manager-points-management-card");
  const canManage = economyGovernanceCanManage();
  const managerMode = canManage && currentUser !== "root";
  relocateEconomyOfficialWalletCard(currentUser === "root");
  if (managerCard) managerCard.style.display = managerMode ? "" : "none";
  if (!card && !managerCard) return;
  if (!canManage) {
    if (card) card.style.display = "none";
    if (managerCard) managerCard.style.display = "none";
    return;
  }
  const data = payload && typeof payload === "object" ? payload : {};
  const wallet = data.official_wallet && typeof data.official_wallet === "object" ? data.official_wallet : {};
  const fundAddresses = data.fund_addresses && typeof data.fund_addresses === "object" ? data.fund_addresses : {};
  if (Object.keys(fundAddresses).length) {
    economyFundAddressCache = {
      ...economyFundAddressCache,
      ...Object.fromEntries(Object.entries(fundAddresses).map(([key, value]) => [key, String(value || "").trim()])),
    };
  }
  const policy = data.policy && typeof data.policy === "object" ? data.policy : {};
  if (data.economy_layer && typeof data.economy_layer === "object") {
    renderEconomyLayerSummary({ stats: { economy_layer: data.economy_layer } });
  }
  const signers = Array.isArray(policy.signers) ? policy.signers : [];
  const proposals = Array.isArray(data.pending_proposals) ? data.pending_proposals : [];
  const signable = Array.isArray(data.signable) ? data.signable : [];
  if (card) card.style.display = "";
  setEconomyText("economy-treasury-signer-center-status", data.policy_error ? `多簽政策異常：${data.policy_error}` : "官方財庫收支、餘額 replay 與待簽治理集中檢視；manager+ signer threshold 共同控制。");
  setEconomyText("economy-treasury-signer-official-balance", `${formatEconomyPointsValue(wallet.balance || 0)} 點`);
  setEconomyText("economy-treasury-signer-official-address", wallet.address || "-");
  setEconomyText("economy-treasury-signer-threshold", `門檻 ${Number(policy.threshold || 0)}/${Number(policy.signer_count || signers.length || 0)} 位 · 權重 ${Number(policy.threshold_weight || 0)}/${Number(policy.total_weight || 0)}`);
  setEconomyText("economy-treasury-signer-policy", `官方財庫收支分析 · 多簽規則 ${sanitize(policy.policy_version || "-")}`);
  setEconomyText("economy-treasury-signer-pending-count", `${signable.length} / ${proposals.length}`);
  setEconomyText("economy-treasury-signer-branch", `branch ${data.canonical_branch || "-"}`);
  setEconomyText("economy-manager-points-status", data.policy_error ? `多簽政策異常：${data.policy_error}` : "官方財庫、治理簽署與疑義事件集中管理；私有鏈底層操作保留 root。");
  setEconomyText("economy-manager-points-official-balance", `${formatEconomyPointsValue(wallet.balance || 0)} 點`);
  setEconomyText("economy-manager-points-official-address", wallet.address || "-");
  setEconomyText("economy-manager-points-threshold", `門檻 ${Number(policy.threshold || 0)}/${Number(policy.signer_count || signers.length || 0)} 位 · 權重 ${Number(policy.threshold_weight || 0)}/${Number(policy.total_weight || 0)}`);
  setEconomyText("economy-manager-points-policy", `官方財庫收支 / 多簽規則 ${sanitize(policy.policy_version || "-")}`);
  setEconomyText("economy-manager-points-pending-count", `${signable.length} / ${proposals.length}`);
  setEconomyText("economy-manager-points-branch", `branch ${data.canonical_branch || "-"}`);
  renderEconomyTreasuryAnalysis(data.treasury_analysis || null);
  const signerList = $("economy-treasury-signer-list");
  if (signerList) {
    signerList.innerHTML = signers.length
      ? signers.map((signer) => `
          <div class="drive-file-row">
            <div>
              <strong>${sanitize(economyTreasurySignerRoleLabel(signer.role))} · ${sanitize(economyTreasurySignerWeightLabel(signer.weight))}</strong>
              <div class="drive-card-sub">官方財庫簽署人 · ${sanitize(signer.custody_mode || "")} · ${sanitize(signer.device_id || "")}</div>
              <button class="economy-ledger-hash economy-explorer-address" type="button" data-explorer-query="${sanitize(signer.wallet_address || "")}">${sanitize(signer.wallet_address || "-")}</button>
            </div>
          </div>
        `).join("")
      : `<div class="drive-empty">尚無可用官方 signer；至少需要兩個 manager+ signer wallet。</div>`;
  }
  const pendingList = $("economy-treasury-signer-pending-list");
  if (pendingList) {
    pendingList.innerHTML = signable.length
      ? signable.map((item) => `
          <div class="drive-file-row">
            <div>
              <strong>${sanitize(economyGovernanceActionLabel(item.action_type))} · 簽署 ${Number(item.signature_count || 0)}/${Number(item.threshold || 0)} · 權重 ${Number(item.signature_weight || 0)}/${Number(item.threshold_weight || 0)}</strong>
              <div class="drive-card-sub">timelock ${sanitize(item.timelock_until || "-")} · ${sanitize(shortEconomyWalletAddress(item.target_wallet_address || ""))} · ${formatEconomyPointsValue(item.requested_amount || 0)} 點</div>
              <div class="drive-card-sub">signing hash ${sanitize(item.signing_payload_hash || "-")} · payload ${sanitize(item.execution_payload_hash || "-")}</div>
              <div class="economy-ledger-hash">${sanitize(item.proposal_uuid || "")}</div>
            </div>
          </div>
        `).join("")
      : `<div class="drive-empty">目前沒有需要你簽署的官方財庫提案。</div>`;
  }
  renderEconomyRootList(signable, "economy-manager-points-pending-list", "目前沒有需要你簽署的官方財庫提案。", (item) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(economyGovernanceActionLabel(item.action_type))} · 簽署 ${Number(item.signature_count || 0)}/${Number(item.threshold || 0)} · 權重 ${Number(item.signature_weight || 0)}/${Number(item.threshold_weight || 0)}</strong>
        <div class="drive-card-sub">timelock ${sanitize(item.timelock_until || "-")} · ${sanitize(shortEconomyWalletAddress(item.target_wallet_address || ""))} · ${formatEconomyPointsValue(item.requested_amount || 0)} 點</div>
        <div class="economy-ledger-hash">${sanitize(item.proposal_uuid || "")}</div>
      </div>
    </div>
  `);
  if (!card) return;
  card.querySelectorAll("[data-explorer-query]").forEach((btn) => {
    if (btn.dataset.explorerBound === "1") return;
    btn.dataset.explorerBound = "1";
    btn.addEventListener("click", () => {
      const query = btn.dataset.explorerQuery || "";
      if ($("economy-explorer-query")) $("economy-explorer-query").value = query;
      setEconomyActivePage("explorer");
      searchEconomyExplorer(query);
    });
  });
}

async function loadEconomyTreasurySignerCenter({ silent = false } = {}) {
  if (!currentUser || !economyChainEnabled() || !economyGovernanceCanManage()) {
    renderEconomyTreasurySignerCenter(null);
    return false;
  }
  try {
    const json = await fetchEconomyJson("/admin/points/governance/treasury-signer-center?limit=50");
    economyTreasurySignerCenterCache = json;
    renderEconomyTreasurySignerCenter(json);
    return true;
  } catch (err) {
    economyTreasurySignerCenterCache = null;
    renderEconomyTreasurySignerCenter({ policy_error: err.message || "官方財庫收支分析讀取失敗" });
    if (!silent) economyGovernanceMsg(err.message || "官方財庫收支分析讀取失敗", false);
    return false;
  }
}

async function refreshEconomyOfficialWalletManagement() {
  if (!economyChainEnabled() || !economyGovernanceCanManage()) return;
  if (currentUser === "root") await loadEconomyRootReport();
  await loadEconomyTreasurySignerCenter({ silent: false });
}

function renderEconomyGovernance(payload = {}) {
  updateEconomyOfficialHotWalletLabels(payload.official_hot_wallet_labels);
  const proposals = Array.isArray(payload.proposals) ? payload.proposals : [];
  economyGovernanceProposalCache = new Map(proposals.map((proposal) => [String(proposal.proposal_uuid || ""), proposal]));
  setEconomyGovernanceCategory(economyGovernanceCategory);
  setEconomyGovernanceStatusFilter(economyGovernanceStatusFilter);
  const selectedCase = $("economy-governance-selected-case");
  if (selectedCase) {
    selectedCase.textContent = economySelectedDisputeUuid
      ? `已選取疑義案件 ${shortEconomyWalletAddress(economySelectedDisputeUuid)}；提案/投票會直接折疊顯示在該案件下方。`
      : "未選取疑義交易案件；點選上方案件後，該案件的治理提案會直接展開在案件下方。";
  }
  const showCreateGroup = (category) => economyGovernanceCategory === "all" || economyGovernanceCategory === category;
  const mintTools = $("economy-governance-mint-create-details");
  if (mintTools) mintTools.style.display = economyGovernanceCanManage() && showCreateGroup("mint") ? "" : "none";
  const policyTools = $("economy-governance-policy-create-details");
  if (policyTools) policyTools.style.display = (economyGovernanceCanManage() || economyGovernanceCanProposePublic()) && showCreateGroup("policy") ? "" : "none";
  const emergencyTools = $("economy-governance-emergency-create-details");
  if (emergencyTools) emergencyTools.style.display = economyGovernanceCanManage() && showCreateGroup("emergency") ? "" : "none";
  setEconomyText("economy-governance-proposal-count", String(proposals.length));
  const reviewCount = proposals.filter((item) => economyGovernanceStatusBucket(item) === "review").length;
  const votingCount = proposals.filter((item) => economyGovernanceStatusBucket(item) === "voting").length;
  const closedCount = proposals.filter((item) => economyGovernanceStatusBucket(item) === "closed").length;
  setEconomyText("economy-governance-open-count", String({ review: reviewCount, voting: votingCount, closed: closedCount }[economyGovernanceStatusFilter] || 0));
  const statusSmall = $("economy-governance-open-count")?.nextElementSibling;
  if (statusSmall) statusSmall.textContent = `${economyGovernanceStatusFilterLabel(economyGovernanceStatusFilter)} · 審核 ${reviewCount} / 投票 ${votingCount} / 結案 ${closedCount}`;
  updateEconomyGovernanceOverviewCounts(proposals);
  const list = $("economy-governance-list");
  if (!list) return;
  const filteredProposals = proposals.filter((proposal) => {
    if (economyGovernanceStatusBucket(proposal) !== economyGovernanceStatusFilter) return false;
    if (String(proposal.reference || "").startsWith("transaction_dispute:") && economyGovernanceCategory !== "dispute") return false;
    if (economyGovernanceCategory === "all") return true;
    if (economyGovernanceCategory === "dispute") {
      return economySelectedDisputeProposalUuids.has(String(proposal.proposal_uuid || ""));
    }
    return economyGovernanceCategoryForProposal(proposal) === economyGovernanceCategory;
  });
  if (!filteredProposals.length) {
    if (economyGovernanceCategory === "dispute") {
      list.innerHTML = economySelectedDisputeUuid
        ? `<div class="drive-empty">此疑義案件在「${economyGovernanceStatusFilterLabel(economyGovernanceStatusFilter)}」沒有治理提案；案件卡片下方會顯示該案件全部提案。</div>`
        : `<div class="drive-empty">請先在疑義交易案件列表點選案件，再查看該案件的治理提案 / 投票。</div>`;
    } else {
      list.innerHTML = `<div class="drive-empty">目前「${economyGovernanceStatusFilterLabel(economyGovernanceStatusFilter)}」沒有此分類的 PointsChain governance proposal</div>`;
    }
    if (economyTransactionDisputeCache.length) renderEconomyTransactionDisputes({ disputes: economyTransactionDisputeCache });
    return;
  }
  list.innerHTML = filteredProposals.map((proposal) => economyRenderGovernanceProposalCard(proposal)).join("");
  bindEconomyGovernanceEvents(list);
  if (economyTransactionDisputeCache.length) renderEconomyTransactionDisputes({ disputes: economyTransactionDisputeCache });
}

async function loadEconomyGovernance({ silent = false } = {}) {
  if (!currentUser || !economyChainEnabled()) {
    renderEconomyGovernance({ proposals: [] });
    return false;
  }
  try {
    const json = await fetchEconomyJson("/points/governance/proposals?limit=50");
    renderEconomyGovernance(json);
    if (!silent) economyGovernanceMsg("治理提案已更新。");
    return true;
  } catch (err) {
    if (!silent) economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "治理提案讀取失敗" });
    return false;
  }
}

async function createGovernanceRecoveryBranchProposal() {
  const incident = String($("economy-governance-branch-incident")?.value || "").trim();
  const incidentRefs = String($("economy-governance-branch-incident-refs")?.value || "").trim();
  const baseBlock = String($("economy-governance-branch-base-block")?.value || "").trim();
  const recoveryStrategy = String($("economy-governance-branch-strategy")?.value || "tainted_remainder_return").trim();
  const lossCause = String($("economy-governance-branch-loss-cause")?.value || "unknown").trim();
  const reason = String($("economy-governance-branch-reason")?.value || "").trim();
  const excluded = String($("economy-governance-branch-excluded")?.value || "").trim();
  const victimStatement = String($("economy-governance-branch-victim-statement")?.value || "").trim();
  const victimEvidence = String($("economy-governance-branch-victim-evidence")?.value || "").trim();
  let victimClaims = [];
  try {
    victimClaims = parseEconomyGovernanceClaims($("economy-governance-branch-claims")?.value || "");
  } catch (err) {
    economyGovernanceMsg(err.message || "Claims JSON 格式錯誤", false);
    return;
  }
  try {
    const json = await fetchEconomyJson("/admin/points/governance/recovery-branch", {
      method: "POST",
      body: JSON.stringify({
        incident_tx_hash: incident,
        incident_tx_hashes: parseEconomyGovernanceLines(incidentRefs),
        base_block_number: baseBlock || null,
        reason,
        excluded_tx_hashes: excluded,
        recovery_strategy: recoveryStrategy,
        loss_cause: lossCause,
        victim_statement: victimStatement,
        victim_evidence_refs: parseEconomyGovernanceLines(victimEvidence),
        victim_claims: victimClaims,
      }),
    });
    economyNotifySuccess(`已建立緊急 recovery branch 提案：${json.proposal?.proposal_uuid || ""}`, { msgFn: economyGovernanceMsg, label: "治理提案" });
    await loadEconomyGovernance({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "緊急 recovery branch 提案失敗" });
  }
}

async function createGovernanceEmergencyLockdownProposal() {
  const scope = String($("economy-governance-lockdown-scope")?.value || "").trim();
  const reason = String($("economy-governance-lockdown-reason")?.value || "").trim();
  try {
    const json = await fetchEconomyJson("/admin/points/governance/policy", {
      method: "POST",
      body: JSON.stringify({
        action_type: "EMERGENCY_LOCKDOWN",
        title: "緊急鎖定提案",
        reason,
        proposal_severity: "CRITICAL",
        impact_scope: scope,
        risk_summary: "Temporarily pauses high-risk operations while incident review proceeds.",
        payload: { lockdown_scope: scope, description: reason },
        reference: economyRequestId("emergency_lockdown"),
      }),
    });
    economyNotifySuccess(`已建立緊急鎖定提案：${json.proposal?.proposal_uuid || ""}`, { msgFn: economyGovernanceMsg, label: "治理提案" });
    await loadEconomyGovernance({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "緊急鎖定提案失敗" });
  }
}

async function createGovernanceMintRequestProposal() {
  const destination = String($("economy-governance-mint-destination")?.value || "official_treasury").trim();
  const amount = Math.floor(Number($("economy-governance-mint-amount")?.value || 0));
  const reason = String($("economy-governance-mint-reason")?.value || "").trim();
  if (!Number.isFinite(amount) || amount <= 0 || !reason) {
    economyGovernanceMsg("Mint 申請需要正數 Value 與 Reason。", false);
    return;
  }
  try {
    const json = await fetchEconomyJson("/admin/points/governance/mint-request", {
      method: "POST",
      body: JSON.stringify({
        destination_fund_key: destination,
        amount,
        reason,
        reference: economyRequestId("mint_request"),
      }),
    });
    economyNotifySuccess(`Mint 申請已送出：${json.proposal?.proposal_uuid || ""}`, { msgFn: economyGovernanceMsg, label: "治理提案" });
    await loadEconomyGovernance({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "Mint 申請失敗" });
  }
}

async function createGovernancePolicyProposal() {
  const actionType = String($("economy-governance-policy-action")?.value || "PARAMETER_CHANGE").trim().toUpperCase();
  const key = String($("economy-governance-policy-key")?.value || "").trim();
  const value = String($("economy-governance-policy-value")?.value || "").trim();
  const reason = String($("economy-governance-policy-reason")?.value || "").trim();
  if (!key || !reason) {
    economyGovernanceMsg("政策提案需要 Key / Feature 與 Reason。", false);
    return;
  }
  if (actionType === "SUPPLY_EXPANSION_REQUEST") {
    const requestedDelta = Math.floor(Number(value || 0));
    if (!Number.isFinite(requestedDelta) || requestedDelta <= 0) {
      economyGovernanceMsg("憲法級增發條款需要在 Value 填入正數增發額。", false);
      return;
    }
    try {
      const json = await fetchEconomyJson("/admin/points/governance/supply-expansion", {
        method: "POST",
        body: JSON.stringify({
          destination_fund_key: key,
          requested_delta: requestedDelta,
          reason,
          financial_report: reason,
          risk_disclosure: "Monetary policy amendment dilutes all holders and only changes max_supply.",
          reference: economyRequestId("supply_expansion"),
        }),
      });
      economyNotifySuccess(`憲法級增發條款已送出：${json.proposal?.proposal_uuid || ""}`, { msgFn: economyGovernanceMsg, label: "治理提案" });
      await loadEconomyGovernance({ silent: true });
    } catch (err) {
      economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "憲法級增發條款送出失敗" });
    }
    return;
  }
  try {
    const json = await fetchEconomyJson("/admin/points/governance/policy", {
      method: "POST",
      body: JSON.stringify({
        action_type: actionType,
        title: economyGovernanceActionLabel(actionType),
        reason,
        payload: {
          parameter_key: actionType === "PARAMETER_CHANGE" ? key : "",
          parameter_value: actionType === "PARAMETER_CHANGE" ? value : "",
          feature_key: actionType === "FEATURE_ACTIVATION" ? key : "",
          burn_policy: actionType === "AUTO_BURN_POLICY" ? value : "",
          signer_change: actionType === "TREASURY_SIGNER_CHANGE" ? key : "",
          signer_change_value: actionType === "TREASURY_SIGNER_CHANGE" ? value : "",
          description: `${key}: ${value}`,
        },
        parameter_key: actionType === "PARAMETER_CHANGE" ? key : "",
        parameter_value: actionType === "PARAMETER_CHANGE" ? value : "",
        feature_key: actionType === "FEATURE_ACTIVATION" ? key : "",
        burn_policy: actionType === "AUTO_BURN_POLICY" ? value : "",
        description: `${key}: ${value}`,
        proposal_severity: actionType === "AUTO_BURN_POLICY" || actionType === "TREASURY_SIGNER_CHANGE" ? "HIGH" : "NORMAL",
        impact_scope: key,
        risk_summary: value,
        reference: economyRequestId("policy_proposal"),
      }),
    });
    economyNotifySuccess(`政策提案已送出：${json.proposal?.proposal_uuid || ""}`, { msgFn: economyGovernanceMsg, label: "治理提案" });
    await loadEconomyGovernance({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "政策提案送出失敗" });
  }
}

async function voteEconomyGovernanceProposal(proposalUuid, vote) {
  const proposal = economyGovernanceProposalCache.get(String(proposalUuid || "")) || {};
  let recoveryChoice = "";
  if (String(proposal.action_type || "").toUpperCase() === "ROLLBACK_BRANCH" && vote === "yes") {
    const payload = proposal.payload && typeof proposal.payload === "object" ? proposal.payload : {};
    const options = payload.recovery_options && typeof payload.recovery_options === "object"
      ? Object.keys(payload.recovery_options)
      : ["tainted_remainder_return", "treasury_compensation", "exclude_tainted_descendants"];
    recoveryChoice = prompt(`請選擇本次 rollback 分支方案：${options.join(" / ")}`, payload.recovery_strategy || "tainted_remainder_return") || "";
    if (!recoveryChoice) return;
  }
  try {
    await fetchEconomyJson(`/points/governance/proposals/${encodeURIComponent(proposalUuid)}/vote`, {
      method: "POST",
      body: JSON.stringify({ vote, recovery_choice: recoveryChoice }),
    });
    economyNotifySuccess(`已投票 ${vote.toUpperCase()}。`, { msgFn: economyGovernanceMsg, label: "治理提案" });
    await loadEconomyGovernance({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "治理投票失敗" });
  }
}

async function executeEconomyGovernanceProposal(proposalUuid) {
  if (!confirm("確認執行已通過的 PointsChain governance proposal？此操作只會套用治理結果，不會刪改舊 ledger。")) return;
  try {
    const json = await fetchEconomyJson(`/admin/points/governance/proposals/${encodeURIComponent(proposalUuid)}/execute`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    economyNotifySuccess(`治理提案已執行：${json.result?.action || ""}`, { msgFn: economyGovernanceMsg, label: "治理提案" });
    await Promise.all([loadEconomyGovernance({ silent: true }), loadEconomyRootReport()]);
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "治理提案執行失敗" });
  }
}

async function sponsorEconomyGovernanceProposal(proposalUuid) {
  try {
    await fetchEconomyJson(`/admin/points/governance/proposals/${encodeURIComponent(proposalUuid)}/sponsor`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    economyNotifySuccess("治理提案已 sponsor。", { msgFn: economyGovernanceMsg, label: "治理提案" });
    await loadEconomyGovernance({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "治理 sponsor 失敗" });
  }
}

async function cancelEconomyGovernanceProposal(proposalUuid) {
  const reason = prompt("取消 / 作廢提案理由");
  if (reason === null) return;
  try {
    await fetchEconomyJson(`/admin/points/governance/proposals/${encodeURIComponent(proposalUuid)}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
    economyNotifySuccess("治理提案已取消。", { msgFn: economyGovernanceMsg, label: "治理提案" });
    await loadEconomyGovernance({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "治理提案取消失敗" });
  }
}

async function vetoEconomyGovernanceProposal(proposalUuid) {
  const reason = prompt("Root veto 理由");
  if (reason === null) return;
  try {
    await fetchEconomyJson(`/root/points/governance/proposals/${encodeURIComponent(proposalUuid)}/veto`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
    economyNotifySuccess("治理提案已 veto。", { msgFn: economyGovernanceMsg, label: "治理提案" });
    await loadEconomyGovernance({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "治理提案", fallback: "root veto 失敗" });
  }
}

async function signEconomyGovernanceMultisig(proposalUuid, signerWalletAddress, targetWalletAddress = "", amountPoints = 1, payloadHash = "", custodyMode = "", walletType = "") {
  try {
    const signature = await economyBuildGovernanceMultisigSignature({
      signer: signerWalletAddress,
      destination: targetWalletAddress || signerWalletAddress,
      amount: amountPoints,
      payloadHash: payloadHash || proposalUuid,
      requestUuid: proposalUuid,
      custodyMode,
      walletType,
    });
    const json = await fetchEconomyJson(`/admin/points/governance/proposals/${encodeURIComponent(proposalUuid)}/multisig-sign`, {
      method: "POST",
      body: JSON.stringify({ signer_wallet_address: signerWalletAddress, signature }),
    });
    economyNotifySuccess(`多簽已簽署：${json.multisig?.signature_count || 0}/${json.multisig?.threshold || 0}`, { msgFn: economyGovernanceMsg, label: "官方多簽" });
    await loadEconomyGovernance({ silent: true });
  } catch (err) {
    economyNotifyFailure(err, { msgFn: economyGovernanceMsg, label: "官方多簽", fallback: "多簽簽署失敗" });
  }
}

function bindEconomyGovernanceEvents(root = null) {
  const list = root || $("economy-governance-list");
  if (!list) return;
  list.querySelectorAll("[data-governance-toggle-proposal]").forEach((row) => {
    if (row.dataset.governanceToggleBound === "1") return;
    row.dataset.governanceToggleBound = "1";
    const toggle = () => toggleEconomyGovernanceProposal(row.dataset.governanceToggleProposal || "");
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      toggle();
    });
  });
  list.querySelectorAll("[data-governance-vote]").forEach((btn) => {
    if (btn.dataset.governanceBound === "1") return;
    btn.dataset.governanceBound = "1";
    btn.addEventListener("click", () => voteEconomyGovernanceProposal(btn.dataset.proposalUuid || "", btn.dataset.governanceVote || ""));
  });
  list.querySelectorAll("[data-governance-execute]").forEach((btn) => {
    if (btn.dataset.governanceBound === "1") return;
    btn.dataset.governanceBound = "1";
    btn.addEventListener("click", () => executeEconomyGovernanceProposal(btn.dataset.governanceExecute || ""));
  });
  list.querySelectorAll("[data-governance-sponsor]").forEach((btn) => {
    if (btn.dataset.governanceBound === "1") return;
    btn.dataset.governanceBound = "1";
    btn.addEventListener("click", () => sponsorEconomyGovernanceProposal(btn.dataset.governanceSponsor || ""));
  });
  list.querySelectorAll("[data-governance-cancel]").forEach((btn) => {
    if (btn.dataset.governanceBound === "1") return;
    btn.dataset.governanceBound = "1";
    btn.addEventListener("click", () => cancelEconomyGovernanceProposal(btn.dataset.governanceCancel || ""));
  });
  list.querySelectorAll("[data-governance-veto]").forEach((btn) => {
    if (btn.dataset.governanceBound === "1") return;
    btn.dataset.governanceBound = "1";
    btn.addEventListener("click", () => vetoEconomyGovernanceProposal(btn.dataset.governanceVeto || ""));
  });
  list.querySelectorAll("[data-governance-multisig-sign]").forEach((btn) => {
    if (btn.dataset.governanceBound === "1") return;
    btn.dataset.governanceBound = "1";
    btn.addEventListener("click", () => signEconomyGovernanceMultisig(
      btn.dataset.governanceMultisigSign || "",
      btn.dataset.signerWallet || "",
      btn.dataset.targetWallet || "",
      btn.dataset.requestedAmount || "1",
      btn.dataset.payloadHash || "",
      btn.dataset.custodyMode || "",
      btn.dataset.walletType || "",
    ));
  });
}

async function sendEconomyRootOfficialGrant() {
  if (!economyChainEnabled()) {
    economyRootOfficialGrantMsg("PointsChain 私有鏈已停用，官方 Treasury 撥款提案不可用。", false);
    return;
  }
  const btn = $("economy-root-official-grant-btn");
  const destination = String($("economy-root-official-grant-destination")?.value || "").trim();
  const amount = Math.floor(Number($("economy-root-official-grant-amount")?.value || 0));
  const reason = String($("economy-root-official-grant-reason")?.value || "").trim();
  if (!destination || !Number.isFinite(amount) || amount <= 0) {
    economyRootOfficialGrantMsg("請確認 To 錢包地址與 Value", false);
    return;
  }
  const oldText = btn ? btn.textContent : "";
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "送出中...";
    }
    const json = await fetchEconomyJson("/admin/points/governance/treasury-transfer", {
      method: "POST",
      body: JSON.stringify({
        destination_wallet_address: destination,
        amount,
        reason,
        reference: economyRequestId("official_wallet_grant"),
      }),
    });
    const proposalUuid = json.proposal?.proposal_uuid || json.proposal_uuid || "";
    const warningSuffix = economyWarningSuffix(json);
    const successMessage = proposalUuid
      ? `官方 Treasury 撥款提案已送出：${proposalUuid}；需 manager+ 投票、root veto 檢查、timelock 與官方多簽簽合後才會正式執行。`
      : "官方 Treasury 撥款提案已送出；需治理通過與官方多簽簽合後才會正式執行。";
    const visibleMessage = `${successMessage}${warningSuffix}`;
    economyRootOfficialGrantMsg(visibleMessage, !warningSuffix);
    setEconomyActivePage("explorer");
    await loadEconomyDashboard();
    await loadEconomyGovernance({ silent: true });
    economySetMsg(visibleMessage, !warningSuffix);
  } catch (err) {
    const message = err.message || "官方 Treasury 撥款提案送出失敗";
    economyRootOfficialGrantMsg(message, false);
    economySetMsg(message, false);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText || "提出撥款提案";
    }
  }
}

async function sealPointsChainBlock() {
  if (!economyChainEnabled()) {
    economySetMsg("PointsChain 私有鏈已停用，無法封塊。", false);
    return;
  }
  try {
    const json = await fetchEconomyJson("/root/points/chain/seal", {
      method: "POST",
      body: JSON.stringify({ limit: 100 }),
    });
    if (json.async) {
      setEconomyChainStatus(`封塊任務已排入背景執行${json.job_id ? `：${json.job_id}` : ""}`);
      economySetMsg("封塊已排入背景任務；完成後可讀取 latest snapshot。");
      return;
    }
    const block = json.block || {};
    setEconomyChainStatus(json.sealed
      ? `已封存區塊 #${Number(block.block_number || 0)}，包含 ${Number(block.ledger_count || 0)} 筆 ledger`
      : (json.msg || "目前沒有未封 ledger 可封存"));
    await loadEconomyDashboard();
  } catch (err) {
    economySetMsg(err.message || "封塊失敗", false);
    setEconomyChainStatus(err.message || "封塊失敗", false);
  }
}

async function verifyPointsChain() {
  if (!economyChainEnabled()) {
    setEconomyChainStatus("PointsChain 私有鏈已停用。");
    return;
  }
  try {
    const json = await fetchEconomyJson("/root/points/chain/verify");
    if (json.async) {
      setEconomyChainStatus(`驗證任務已排入背景執行${json.job_id ? `：${json.job_id}` : ""}`);
      economySetMsg("PointsChain 驗證已排入背景任務；完成後可讀取 latest snapshot。");
      return;
    }
    setEconomyChainStatus(formatEconomyVerificationSummary(json.verification || json), (json.verification || json).ok !== false);
    if (currentUser === "root") await loadEconomyRootReport();
  } catch (err) {
    economySetMsg(err.message || "驗證失敗", false);
    setEconomyChainStatus(err.message || "驗證失敗", false);
  }
}

async function createPointsChainBackup() {
  if (currentUser !== "root") return;
  economySetMsg("PointsChain ledger backup/restore 已停用；鏈異常請使用 safe mode、forensic bundle、分支與緊急治理。", false);
}

async function approvePointsChainRecovery() {
  if (currentUser !== "root") return;
  economySetMsg("備份還原會覆寫 append-only ledger，已停用。請改用 recovery branch、疑義交易、緊急治理與補正交易。", false);
}

async function autoHandlePointsChainRecovery() {
  if (currentUser !== "root") return;
  if (!economyChainEnabled()) {
    economySetMsg("PointsChain 私有鏈已停用，無法處理鏈異常。", false);
    return;
  }
  if (!confirm("系統會驗證 PointsChain 並產生 safe mode / forensic / 分支治理處理方案；不會套用備份還原或覆寫 ledger。是否繼續？")) return;
  const btn = $("economy-recovery-auto-handle-btn");
  const oldText = btn ? btn.textContent : "";
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "處理中...";
    }
    economySetMsg("正在驗證 PointsChain 並準備分支 / 緊急治理處理方案...");
    economyRecoveryActionMsg("正在檢查 PointsChain 異常處理方案...");
    const json = await fetchEconomyJson("/root/points/chain/recovery/auto-handle", {
      method: "POST",
      body: JSON.stringify({ confirm: "AUTO HANDLE POINTSCHAIN" }),
    });
    if (json.async) {
      const msg = `異常鏈處理方案已排入背景任務${json.job_id ? `：${json.job_id}` : ""}`;
      economySetMsg(msg);
      economyRecoveryActionMsg(msg);
      setEconomyChainStatus(msg);
      await loadEconomyRootReport();
      return;
    }
    await loadEconomyDashboard();
    if (json.action === "verified_clean") {
      economySetMsg(json.msg || "PointsChain 驗證正常");
      economyRecoveryActionMsg(json.msg || "PointsChain 驗證正常");
      setEconomyChainStatus(formatEconomyVerificationSummary(json.verification || {}), true);
      if (typeof loadAudit === "function") await loadAudit(auditPage || 0);
      return;
    }
    const resultMessage = formatEconomyRecoveryResult(json);
    economySetMsg(json.msg || resultMessage || "異常鏈處理完成", !!json.ok);
    economyRecoveryActionMsg(json.msg || resultMessage || "異常鏈處理完成", !!json.ok);
    setEconomyChainStatus(formatEconomyVerificationSummary(json.verification || json.initial_verification || {}), !!json.ok);
    if (typeof loadAudit === "function") await loadAudit(auditPage || 0);
  } catch (err) {
    economySetMsg(err.message || "一鍵處理異常鏈失敗", false);
    economyRecoveryActionMsg(err.message || "一鍵處理異常鏈失敗", false);
    setEconomyChainStatus(err.message || "一鍵處理異常鏈失敗", false);
    await loadEconomyRootReport();
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText || "檢查異常處理方案";
    }
  }
}

// Economy explorer, finality, proof, and fee-estimate handlers live in 55-economy-explorer.js.

function bindEconomyInlineEvents() {
  const bindings = [
    ["economy-refresh-btn", loadEconomyDashboard],
    ["economy-wallet-onboarding-refresh-btn", loadEconomyDashboard],
    ["economy-wallet-download-btn", downloadEconomyWalletCsv],
    ["economy-wallet-official-hot-btn", useOfficialHotWallet],
    ["economy-wallet-create-cold-btn", createColdWalletDraft],
    ["economy-wallet-download-file-btn", economyDownloadDraftColdWalletFile],
    ["economy-wallet-copy-trade-password-btn", economyCopyDraftTradePassword],
    ["economy-wallet-start-mnemonic-quiz-btn", startEconomyColdWalletMnemonicQuiz],
    ["economy-wallet-mnemonic-check-btn", checkEconomyColdWalletMnemonicQuiz],
    ["economy-wallet-use-generated-cold-btn", selectGeneratedColdWalletForImport],
    ["economy-wallet-import-cold-btn", startColdWalletImport],
    ["economy-wallet-confirm-cold-btn", confirmColdWalletBinding],
    ["economy-transfer-submit-btn", submitEconomyWalletTransfer],
    ["economy-transactions-refresh-btn", loadEconomyTransactions],
    ["economy-disputes-refresh-btn", () => loadEconomyTransactionDisputes()],
    ["economy-explorer-search-btn", () => searchEconomyExplorer()],
    ["economy-explorer-refresh-btn", () => searchEconomyExplorer(economyExplorerLastQuery || $("economy-explorer-query")?.value || "")],
    ["economy-governance-refresh-btn", () => loadEconomyGovernance()],
    ["economy-governance-branch-create-btn", createGovernanceRecoveryBranchProposal],
    ["economy-governance-lockdown-create-btn", createGovernanceEmergencyLockdownProposal],
    ["economy-governance-mint-create-btn", createGovernanceMintRequestProposal],
    ["economy-governance-policy-create-btn", createGovernancePolicyProposal],
    ["economy-treasury-analysis-refresh-btn", () => loadEconomyTreasurySignerCenter()],
    ["economy-manager-points-open-wallets-btn", () => {
      setEconomyActivePage("chain");
      $("economy-root-wallet-management-card")?.scrollIntoView?.({ block: "start", behavior: "smooth" });
    }],
    ["economy-manager-points-open-governance-btn", () => setEconomyActivePage("governance")],
    ["economy-manager-points-open-explorer-btn", () => setEconomyActivePage("explorer")],
    ["economy-ledger-export-btn", exportEconomyLedgerCsv],
    ["economy-root-wallet-refresh-btn", refreshEconomyOfficialWalletManagement],
    ["economy-root-official-grant-btn", sendEconomyRootOfficialGrant],
    ["economy-root-report-btn", loadEconomyRootReport],
    ["economy-recovery-auto-handle-btn", autoHandlePointsChainRecovery],
    ["economy-seal-btn", sealPointsChainBlock],
    ["economy-verify-btn", verifyPointsChain],
  ];
  bindings.forEach(([id, handler]) => {
    const el = $(id);
    if (!el || el.dataset.economyInlineBound === "1") return;
    el.dataset.economyInlineBound = "1";
    el.addEventListener("click", handler);
  });
  economyInlineEventsBound = true;
  document.querySelectorAll("[data-economy-page]").forEach((tab) => {
    if (tab.dataset.economyPageBound === "1") return;
    tab.dataset.economyPageBound = "1";
    tab.addEventListener("click", () => setEconomyActivePage(tab.dataset.economyPage || "balance"));
  });
  document.querySelectorAll("[data-economy-explorer-layer]").forEach((btn) => {
    if (btn.dataset.economyExplorerLayerBound === "1") return;
    btn.dataset.economyExplorerLayerBound = "1";
    btn.addEventListener("click", () => {
      setEconomyExplorerLayer(btn.dataset.economyExplorerLayer || "pc1");
    });
  });
  setEconomyExplorerLayer(economyExplorerActiveLayer, { reset: false });
  const explorerInput = $("economy-explorer-query");
  if (explorerInput && explorerInput.dataset.economyInlineBound !== "1") {
    explorerInput.dataset.economyInlineBound = "1";
    explorerInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") searchEconomyExplorer();
    });
  }
  const transferFeeInput = $("economy-transfer-fee");
  if (transferFeeInput && transferFeeInput.dataset.economyEstimateBound !== "1") {
    transferFeeInput.dataset.economyEstimateBound = "1";
    transferFeeInput.addEventListener("input", scheduleEconomyTransferFeeEstimate);
    transferFeeInput.addEventListener("change", scheduleEconomyTransferFeeEstimate);
    scheduleEconomyTransferFeeEstimate();
  }
  const governanceCategory = $("economy-governance-category-select");
  if (governanceCategory && governanceCategory.dataset.economyGovernanceCategoryBound !== "1") {
    governanceCategory.dataset.economyGovernanceCategoryBound = "1";
    governanceCategory.addEventListener("change", () => {
      setEconomyGovernanceCategory(governanceCategory.value || "all");
      renderEconomyGovernance({ proposals: Array.from(economyGovernanceProposalCache.values()) });
    });
  }
  document.querySelectorAll("[data-governance-status-filter]").forEach((btn) => {
    if (btn.dataset.economyGovernanceStatusBound === "1") return;
    btn.dataset.economyGovernanceStatusBound = "1";
    btn.addEventListener("click", () => {
      setEconomyGovernanceStatusFilter(btn.dataset.governanceStatusFilter || "review");
      renderEconomyGovernance({ proposals: Array.from(economyGovernanceProposalCache.values()) });
    });
  });
  setEconomyGovernanceStatusFilter(economyGovernanceStatusFilter);
  if (economyDocumentEventsBound) {
    syncEconomySubpages(currentUser === "root");
    syncEconomyAutoRefreshLifecycle();
    return;
  }
  economyDocumentEventsBound = true;
  document.addEventListener("hackme:account-context-changed", () => {
    resetEconomyAccountState();
    economyActivePage = readEconomyActivePage();
    syncEconomySubpages(currentUser === "root");
  });
  syncEconomySubpages(currentUser === "root");
  document.addEventListener("hackme:module-changed", syncEconomyAutoRefreshLifecycle);
  document.addEventListener("visibilitychange", syncEconomyAutoRefreshLifecycle);
  syncEconomyAutoRefreshLifecycle();
}

if (document.readyState === "complete") {
  bindEconomyInlineEvents();
} else {
  document.addEventListener("DOMContentLoaded", bindEconomyInlineEvents);
}
