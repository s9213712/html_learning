'use strict';

let tradingState = {
  markets: [],
  positions: [],
  orders: [],
  fills: [],
  bots: [],
  botRuns: [],
  rootReport: null,
  fundingPool: null,
  spotSummary: null,
  marginSummary: null,
  futuresPositions: [],
  state: null,
  referencePrices: null,
  btcSignal: null,
  workflowTemplates: [],
  botCompetition: null,
  wallets: [],
};
let tradingActivePage = "spot";
let tradingRootSitewideActiveTab = "positions";
let tradingEventsBound = false;
let tradingReferencePriceAbort = null;
let tradingReferenceChartAbort = null;
let tradingReferenceAutoTimer = null;
let tradingReferenceAutoBusy = false;
let tradingReferenceChartAutoTimer = null;
let tradingReferenceChartAutoBusy = false;
let tradingReferenceChartModel = null;
let tradingReferenceHoverIndex = null;
let tradingDashboardAutoTimer = null;
let tradingDashboardAutoBusy = false;
let tradingMutationRefreshTimer = null;
let tradingMutationRefreshBusy = false;
let tradingLivePriceTimer = null;
let tradingLivePriceBusy = false;
let tradingLivePriceInFlight = false;
let tradingLivePriceAbort = null;
let tradingTrialCountdownTimer = null;
let tradingBtcSignalCountdownTimer = null;
let tradingBotCountdownTimer = null;
let tradingCurrentBotTab = "mybots";
let tradingBotChartOverlay = null;
let tradingActiveActionButton = null;
const tradingGridExpandedBots = new Set();
const TRADING_WORKFLOW_STORAGE_KEY = "hackme_trading_workflow_json";
const TRADING_PERSONAL_FORM_STORAGE_KEY = "hackme_trading_personal_form_v1";
let tradingAccountScope = "";
let tradingAccountGeneration = 0;
let tradingDashboardLoadGeneration = 0;

function tradingOperationContext() {
  return {
    generation: tradingAccountGeneration,
    scope: typeof getCurrentAccountStorageScope === "function"
      ? getCurrentAccountStorageScope()
      : String(currentUserId || currentUser || "anonymous"),
  };
}

function tradingOperationIsCurrent(operation) {
  if (!operation || operation.generation !== tradingAccountGeneration) return false;
  const scope = typeof getCurrentAccountStorageScope === "function"
    ? getCurrentAccountStorageScope()
    : String(currentUserId || currentUser || "anonymous");
  return operation.scope === scope;
}

function tradingAccountAbortError() {
  if (typeof accountRequestAbortError === "function") return accountRequestAbortError();
  return new DOMException("Account context changed", "AbortError");
}

function tradingAssertOperationCurrent(operation) {
  if (tradingOperationIsCurrent(operation)) return;
  throw tradingAccountAbortError();
}

function tradingAssertDashboardLoadCurrent(operation, loadGeneration) {
  tradingAssertOperationCurrent(operation);
  if (loadGeneration !== tradingDashboardLoadGeneration) throw tradingAccountAbortError();
}

function tradingRefreshMs(key, fallbackSeconds, minSeconds = 1, maxSeconds = 300) {
  const seconds = Number(siteConfig?.[key] || fallbackSeconds);
  return Math.max(minSeconds, Math.min(maxSeconds, Number.isFinite(seconds) ? seconds : fallbackSeconds)) * 1000;
}

function tradingDashboardRefreshMs() {
  return tradingRefreshMs("trading_dashboard_refresh_seconds", 5, 2, 300);
}

function tradingLivePriceRefreshMs() {
  return tradingRefreshMs("trading_live_price_refresh_seconds", 2, 1, 60);
}

function tradingReferencePriceRefreshMs() {
  return tradingRefreshMs("trading_reference_price_refresh_seconds", 1, 1, 60);
}

function tradingReferenceChartRefreshMs() {
  return tradingRefreshMs("trading_reference_chart_refresh_seconds", 5, 2, 300);
}

function shouldRunTradingPolling(tab = currentModuleTab) {
  if (!currentUser || document.hidden) return false;
  if (tab === currentModuleTab && currentModuleTab !== "trading" && currentModuleTab !== "economy") return false;
  return tab === "trading" || tab === "economy";
}

function shouldRunTradingFullPolling(tab = currentModuleTab) {
  return Boolean(shouldRunTradingPolling(tab) && tab === "trading");
}

function stopTradingModuleTimers() {
  if (tradingReferenceAutoTimer) clearInterval(tradingReferenceAutoTimer);
  if (tradingReferenceChartAutoTimer) clearInterval(tradingReferenceChartAutoTimer);
  if (tradingDashboardAutoTimer) clearInterval(tradingDashboardAutoTimer);
  if (tradingMutationRefreshTimer) clearTimeout(tradingMutationRefreshTimer);
  if (tradingLivePriceTimer) clearInterval(tradingLivePriceTimer);
  if (tradingTrialCountdownTimer) clearInterval(tradingTrialCountdownTimer);
  if (tradingBtcSignalCountdownTimer) clearInterval(tradingBtcSignalCountdownTimer);
  if (tradingLivePriceAbort) tradingLivePriceAbort.abort();
  tradingReferenceAutoTimer = null;
  tradingReferenceChartAutoTimer = null;
  tradingDashboardAutoTimer = null;
  tradingMutationRefreshTimer = null;
  tradingLivePriceTimer = null;
  tradingTrialCountdownTimer = null;
  tradingBtcSignalCountdownTimer = null;
  tradingLivePriceAbort = null;
  tradingLivePriceInFlight = false;
}

function stopTradingFullModuleTimers() {
  if (tradingReferenceAutoTimer) clearInterval(tradingReferenceAutoTimer);
  if (tradingReferenceChartAutoTimer) clearInterval(tradingReferenceChartAutoTimer);
  if (tradingDashboardAutoTimer) clearInterval(tradingDashboardAutoTimer);
  if (tradingTrialCountdownTimer) clearInterval(tradingTrialCountdownTimer);
  if (tradingBtcSignalCountdownTimer) clearInterval(tradingBtcSignalCountdownTimer);
  tradingReferenceAutoTimer = null;
  tradingReferenceChartAutoTimer = null;
  tradingDashboardAutoTimer = null;
  tradingTrialCountdownTimer = null;
  tradingBtcSignalCountdownTimer = null;
}

function startTradingModuleTimers() {
  if (!shouldRunTradingPolling()) {
    stopTradingModuleTimers();
    return;
  }
  if (shouldRunTradingFullPolling()) {
    restartTradingReferenceAutoRefresh();
    if (!tradingDashboardAutoTimer) {
      tradingDashboardAutoTimer = setInterval(async () => {
        if (!shouldRunTradingFullPolling() || tradingDashboardAutoBusy) return;
        tradingDashboardAutoBusy = true;
        try {
          await loadTradingDashboard();
        } finally {
          tradingDashboardAutoBusy = false;
        }
      }, tradingDashboardRefreshMs());
    }
    if (!tradingTrialCountdownTimer) {
      tradingTrialCountdownTimer = setInterval(updateTradingTrialCountdown, 1000);
    }
    if (!tradingBtcSignalCountdownTimer) {
      tradingBtcSignalCountdownTimer = setInterval(updateTradingBtcSignalMeta, 1000);
    }
  } else {
    stopTradingFullModuleTimers();
  }
  if (!tradingLivePriceTimer) {
    tradingLivePriceTimer = setInterval(async () => {
      if (!shouldRunTradingPolling() || tradingLivePriceBusy) return;
      tradingLivePriceBusy = true;
      try {
        await loadTradingLivePrice();
      } finally {
        tradingLivePriceBusy = false;
      }
    }, tradingLivePriceRefreshMs());
  }
}

function syncTradingModuleTimerLifecycle() {
  if (shouldRunTradingPolling()) startTradingModuleTimers();
  else stopTradingModuleTimers();
}

const TRADING_PERSONAL_FORM_FIELDS = [
  { id: "trading-market-select", value: "" },
  { id: "trading-side", value: "buy" },
  { id: "trading-order-type", value: "market" },
  { id: "trading-input-mode", value: "quantity" },
  { id: "trading-quantity", value: "0.01" },
  { id: "trading-limit-price", value: "" },
  { id: "trading-stop-loss-percent", value: "" },
  { id: "trading-take-profit-percent", value: "" },
  { id: "trading-margin-type", value: "margin_long" },
  { id: "trading-margin-market-select", value: "" },
  { id: "trading-margin-quantity", value: "0.01" },
  { id: "trading-margin-collateral", value: "100" },
  { id: "trading-margin-stop-loss-percent", value: "" },
  { id: "trading-margin-take-profit-percent", value: "" },
  { id: "trading-dca-bot-name", value: "" },
  { id: "trading-dca-bot-market", value: "" },
  { id: "trading-dca-bot-budget-points", value: "100" },
  { id: "trading-dca-bot-interval-preset", value: "24" },
  { id: "trading-dca-bot-interval-hours", value: "24" },
  { id: "trading-dca-price-upper", value: "" },
  { id: "trading-dca-price-lower", value: "" },
  { id: "trading-dca-stop-loss-percent", value: "" },
  { id: "trading-dca-take-profit-percent", value: "" },
  { id: "trading-dca-share-parameters", checked: false },
  { id: "trading-dca-bot-max-runs", value: "7" },
  { id: "trading-dca-bot-enabled", checked: true },
  { id: "trading-grid-bot-name", value: "" },
  { id: "trading-grid-bot-market", value: "" },
  { id: "trading-grid-preset", value: "" },
  { id: "trading-grid-upper-price", value: "" },
  { id: "trading-grid-lower-price", value: "" },
  { id: "trading-grid-count", value: "10" },
  { id: "trading-grid-order-amount", value: "100" },
  { id: "trading-grid-stop-loss-percent", value: "" },
  { id: "trading-grid-take-profit-percent", value: "" },
  { id: "trading-grid-spacing-mode", value: "arithmetic" },
  { id: "trading-grid-share-parameters", checked: false },
  { id: "trading-auto-bot-name", value: "" },
  { id: "trading-auto-bot-market", value: "" },
  { id: "trading-auto-bot-budget-points", value: "100" },
  { id: "trading-auto-daily-runs", value: "5" },
  { id: "trading-auto-bot-max-runs", value: "5" },
  { id: "trading-auto-bot-cooldown", value: "300" },
  { id: "trading-auto-share-parameters", checked: false },
  { id: "trading-auto-bot-enabled", checked: true },
  { id: "trading-workflow-custom-name", value: "" },
  { id: "trading-dca-backtest-timeframe", value: "15m" },
  { id: "trading-dca-backtest-market", value: "" },
  { id: "trading-dca-backtest-start", value: "" },
  { id: "trading-dca-backtest-end", value: "" },
  { id: "trading-dca-backtest-initial-cash", value: "10000" },
  { id: "trading-dca-backtest-slippage-percent", value: "0" },
  { id: "trading-dca-backtest-order-points", value: "100" },
  { id: "trading-dca-backtest-interval-candles", value: "1" },
  { id: "trading-dca-backtest-stop-loss-percent", value: "" },
  { id: "trading-dca-backtest-take-profit-percent", value: "" },
  { id: "trading-grid-backtest-timeframe", value: "15m" },
  { id: "trading-grid-backtest-market", value: "" },
  { id: "trading-grid-backtest-start", value: "" },
  { id: "trading-grid-backtest-end", value: "" },
  { id: "trading-grid-backtest-initial-cash", value: "10000" },
  { id: "trading-grid-backtest-slippage-percent", value: "0" },
  { id: "trading-grid-backtest-grid-lower", value: "" },
  { id: "trading-grid-backtest-grid-upper", value: "" },
  { id: "trading-grid-backtest-grid-count", value: "10" },
  { id: "trading-grid-backtest-grid-amount", value: "100" },
  { id: "trading-grid-backtest-stop-loss-percent", value: "" },
  { id: "trading-grid-backtest-take-profit-percent", value: "" },
  { id: "trading-grid-backtest-grid-spacing", value: "arithmetic" },
  { id: "trading-workflow-backtest-timeframe", value: "15m" },
  { id: "trading-workflow-backtest-market", value: "" },
  { id: "trading-workflow-backtest-start", value: "" },
  { id: "trading-workflow-backtest-end", value: "" },
  { id: "trading-workflow-backtest-initial-cash", value: "10000" },
  { id: "trading-workflow-backtest-slippage-percent", value: "0" },
  { id: "trading-workflow-backtest-order-points", value: "100" },
];

function tradingUserStorageScope() {
  if (typeof getCurrentAccountStorageScope === "function") return getCurrentAccountStorageScope();
  const id = Number(currentUserId || 0);
  if (Number.isFinite(id) && id > 0) return `user:${id}`;
  const name = String(currentUser || "").trim().toLowerCase();
  return name ? `name:${name}` : "anonymous";
}

function tradingUserStorageKey(key) {
  if (typeof accountScopedStorageKey === "function") return accountScopedStorageKey(key, tradingUserStorageScope());
  return `hackme_web:${tradingUserStorageScope()}:${String(key || "state")}`;
}

function tradingDefaultSpendWalletAddress() {
  if (currentUser === "root") return "";
  const tradingSelect = $("trading-payment-wallet");
  if (tradingSelect && String(tradingSelect.value || "").trim()) {
    return String(tradingSelect.value || "").trim().toLowerCase();
  }
  if (typeof readEconomyDefaultSpendWalletAddress === "function") {
    return String(readEconomyDefaultSpendWalletAddress() || "").trim().toLowerCase();
  }
  return "";
}

function tradingSourceWalletQuery() {
  const address = tradingDefaultSpendWalletAddress();
  return address ? `?source_wallet_address=${encodeURIComponent(address)}` : "";
}

function tradingShortWalletAddress(address) {
  const value = String(address || "");
  if (typeof shortEconomyWalletAddress === "function") return shortEconomyWalletAddress(value);
  return value.length > 16 ? `${value.slice(0, 8)}...${value.slice(-6)}` : value;
}

function tradingWalletOptionLabel(wallet) {
  if (typeof economyWalletOptionLabel === "function") return economyWalletOptionLabel(wallet);
  const label = String(wallet?.label || wallet?.wallet_type || "wallet");
  return `${label} · ${tradingShortWalletAddress(wallet?.address || "")}`;
}

function tradingSpendableWallets(wallets = []) {
  return (Array.isArray(wallets) ? wallets : []).filter((wallet) => {
    const status = String(wallet?.status || "");
    const mode = String(wallet?.custody_mode || "");
    const type = String(wallet?.wallet_type || "");
    const address = String(wallet?.address || "").toLowerCase();
    return status === "active" && mode === "server_hot" && type === "official_hot" && address.startsWith("pc0");
  });
}

function renderTradingPaymentWalletOptions(wallets = [], selectedAddress = "") {
  const select = $("trading-payment-wallet");
  const note = $("trading-payment-wallet-note");
  if (!select) return;
  if (currentUser === "root" || tradingState.funding?.mode === "root_simulated") {
    select.innerHTML = `<option value="">root 模擬資金</option>`;
    select.disabled = true;
    if (note) note.textContent = "root 下單固定使用 root 模擬資金，不可選用站內託管錢包或官方財庫。";
    return;
  }
  const spendable = tradingSpendableWallets(wallets);
  if (!spendable.length) {
    select.innerHTML = `<option value="">尚無可用付款錢包</option>`;
    select.disabled = true;
    if (note) note.textContent = "交易所僅支援 pc0 站內託管錢包；請先確認站內託管錢包已建立。";
    return;
  }
  const previous = select.value;
  const saved = typeof readEconomyDefaultSpendWalletAddress === "function"
    ? String(readEconomyDefaultSpendWalletAddress() || "").trim().toLowerCase()
    : "";
  const normalizedSelected = String(selectedAddress || "").trim().toLowerCase();
  select.disabled = false;
  select.innerHTML = spendable.map((wallet) => {
    return `<option value="${sanitize(wallet.address)}">${sanitize(tradingWalletOptionLabel(wallet))}</option>`;
  }).join("");
  const candidates = [previous, saved, normalizedSelected].filter(Boolean);
  const matched = candidates.find((address) => spendable.some((wallet) => String(wallet.address || "").toLowerCase() === address));
  if (matched) select.value = matched;
  else if (spendable.some((wallet) => wallet.is_primary)) select.value = spendable.find((wallet) => wallet.is_primary).address;
  else select.value = spendable[0].address;
  if (typeof writeEconomyDefaultSpendWalletAddress === "function") {
    writeEconomyDefaultSpendWalletAddress(select.value || "");
  }
  const wallet = spendable.find((item) => String(item.address || "").toLowerCase() === String(select.value || "").toLowerCase());
  if (note) {
    const warning = String(tradingState.funding?.wallet_selection_warning || "").trim();
    if (warning) {
      note.textContent = `原付款錢包不可用：${warning}；已切回可用錢包 ${tradingShortWalletAddress(select.value)}。`;
      return;
    }
    const balance = Number(wallet?.points_balance || 0);
    const frozen = Number(wallet?.points_frozen || 0);
    note.textContent = `${tradingShortWalletAddress(select.value)} · 可用 ${formatTradingPointsValue(balance)} · 凍結 ${formatTradingPointsValue(frozen)}。交易所僅使用站內託管錢包，不接受冷錢包直接下單。`;
  }
}

function tradingSetPersonalField(field, value) {
  const el = $(field.id);
  if (!el) return;
  if ("checked" in field) {
    el.checked = value === undefined ? !!field.checked : !!value;
    return;
  }
  const nextValue = value === undefined || value === null ? field.value : String(value);
  if (el.tagName === "SELECT" && nextValue) {
    const exists = Array.from(el.options || []).some((option) => option.value === nextValue);
    if (!exists) return;
  }
  if (el.tagName === "SELECT" && !nextValue && el.options?.length) {
    const emptyOption = Array.from(el.options || []).some((option) => option.value === "");
    if (!emptyOption) {
      el.selectedIndex = 0;
      return;
    }
  }
  el.value = nextValue;
}

function captureTradingPersonalFormState() {
  const state = {};
  TRADING_PERSONAL_FORM_FIELDS.forEach((field) => {
    const el = $(field.id);
    if (!el) return;
    state[field.id] = "checked" in field ? !!el.checked : el.value;
  });
  return state;
}

function saveTradingPersonalFormState() {
  try {
    localStorage.setItem(tradingUserStorageKey(TRADING_PERSONAL_FORM_STORAGE_KEY), JSON.stringify(captureTradingPersonalFormState()));
  } catch (err) {
    console.warn("[trading] failed to save personal form state", err);
  }
}

function applyTradingPersonalFormState(saved = {}) {
  TRADING_PERSONAL_FORM_FIELDS.forEach((field) => {
    const hasSavedValue = Object.prototype.hasOwnProperty.call(saved, field.id);
    tradingSetPersonalField(field, hasSavedValue ? saved[field.id] : undefined);
  });
  syncTradingDcaIntervalMode();
  syncTradingOrderSideTheme();
  syncTradingOrderInputMode();
  updateTradingOrderEstimate();
  updateTradingMarginEstimate();
  if ($("trading-grid-preset")?.value) applyGridPreset({ quiet: true, save: false });
  scheduleGridBotPreview();
}

function loadTradingPersonalFormState() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(tradingUserStorageKey(TRADING_PERSONAL_FORM_STORAGE_KEY)) || "{}") || {};
  } catch (err) {
    saved = {};
  }
  applyTradingPersonalFormState(saved);
}

function ensureTradingAccountScope(options = {}) {
  const nextScope = tradingUserStorageScope();
  if (!options.force && tradingAccountScope === nextScope) return false;
  tradingAccountScope = nextScope;
  loadTradingPersonalFormState();
  return true;
}

function resetTradingAccountState() {
  tradingAccountGeneration += 1;
  tradingDashboardLoadGeneration += 1;
  stopTradingModuleTimers();
  if (tradingReferencePriceAbort) tradingReferencePriceAbort.abort();
  if (tradingReferenceChartAbort) tradingReferenceChartAbort.abort();
  tradingReferencePriceAbort = null;
  tradingReferenceChartAbort = null;
  if (tradingBotCountdownTimer) clearInterval(tradingBotCountdownTimer);
  tradingBotCountdownTimer = null;
  tradingReferenceAutoBusy = false;
  tradingReferenceChartAutoBusy = false;
  tradingDashboardAutoBusy = false;
  tradingMutationRefreshBusy = false;
  tradingLivePriceBusy = false;
  tradingReferenceChartModel = null;
  tradingReferenceHoverIndex = null;
  tradingGridExpandedBots.clear();
  tradingState = {
    markets: [],
    positions: [],
    orders: [],
    fills: [],
    bots: [],
    botRuns: [],
    rootReport: null,
    fundingPool: null,
    spotSummary: null,
    marginSummary: null,
    futuresPositions: [],
    state: null,
    referencePrices: null,
    btcSignal: null,
    workflowTemplates: [],
    botCompetition: null,
    wallets: [],
  };
  if (typeof window.resetTradingBotsAccountState === "function") window.resetTradingBotsAccountState();
  [
    "trading-order-list",
    "trading-fill-list",
    "trading-position-list",
    "trading-spot-position-detail-list",
    "trading-portfolio-asset-list",
    "trading-margin-position-list",
    "trading-contract-position-list",
    "trading-my-bots-list",
    "trading-bot-competition-list",
    "trading-root-spot-position-list",
    "trading-root-margin-position-list",
    "trading-root-bot-position-list",
    "trading-root-lending-pool-list",
    "trading-root-reserve-events-list",
  ].forEach((id) => {
    const element = $(id);
    if (element) element.replaceChildren();
  });
}

function bindTradingPersonalFormPersistence() {
  TRADING_PERSONAL_FORM_FIELDS.forEach((field) => {
    const el = $(field.id);
    if (!el || el.dataset.tradingPersonalBound === "1") return;
    el.dataset.tradingPersonalBound = "1";
    el.addEventListener("input", saveTradingPersonalFormState);
    el.addEventListener("change", saveTradingPersonalFormState);
  });
}

function syncTradingDcaIntervalMode() {
  const dcaPreset = $("trading-dca-bot-interval-preset");
  const target = $("trading-dca-bot-interval-hours");
  const field = $("trading-dca-custom-interval-field");
  if (!dcaPreset || !target) return false;
  const custom = dcaPreset.value === "custom";
  target.disabled = !custom;
  if (field) field.style.display = custom ? "" : "none";
  if (!custom) target.value = dcaPreset.value;
  return custom;
}

function tradingWarningLanguage() {
  const raw = String(tradingState.settings?.warning_language || "zh-TW").trim().toLowerCase();
  return raw.startsWith("en") ? "en" : "zh-TW";
}

function tradingWarningText(key, vars = {}) {
  if (tradingWarningLanguage() === "en") {
    const messages = {
      risk_grade_unavailable: "Risk-grade price is unavailable. Market orders and other high-risk paths remain paused; limit orders are still allowed.",
      degrade_light: "Price sources degraded. Whether trading auto-pauses depends on the root risk-control policy.",
      market_kind: "Market-order trading",
      bot_kind: "Bot trading",
      borrowing_kind: "Borrowing / margin trading",
      root_auto_pause: `Root auto-pause enabled: ${vars.kinds || "-"}`,
      root_warn_only: `Root warning only; trading remains enabled (healthy providers ${vars.providerCount || 0}/${vars.tradeMinProviders || 0})`,
      root_warn_only_short: "Root warning only; trading remains enabled",
      risk_grade_unavailable_short: "Risk-grade price is unavailable",
      reference_degraded: "Reference price degraded",
      reference_healthy_risk_usable: "Reference price healthy; risk-grade price still usable",
      reference_healthy_confidence: `Reference price healthy; risk-grade confidence ${vars.confidence || "-"}`,
      auto_selected_market: `No market selected; automatically using ${vars.market || "-"}`,
      current_price_purpose_short: "Display / valuation",
      current_price_degraded_short: "Price degraded",
      current_price_paused_short: "Price degraded · paused",
      current_price_warning_short: "Price OK · notice",
      current_price_healthy_short: "Price OK",
      current_price_unavailable_short: "Price unavailable",
      current_price_defaulted_short: "Market auto-selected",
      pause_message: `${vars.kindLabel || "Trading"} paused because price health degraded: ${vars.reason || "-"}; healthy providers ${vars.providerCount || 0}, need at least ${vars.tradeMinProviders || 0}`,
      risk_usable_yes: "risk usable yes",
      risk_usable_no: "risk usable no",
    };
    return messages[key] || key;
  }
  const messages = {
    risk_grade_unavailable: "目前風控級價格不可用，已暫停市價單與高風險交易；限價單仍可使用",
    degrade_light: "價格來源降級，交易是否自動暫停由 root 風控開關決定",
    market_kind: "市價交易",
    bot_kind: "機器人交易",
    borrowing_kind: "借貸交易",
    root_auto_pause: `root 已設定自動暫停：${vars.kinds || "-"}`,
    root_warn_only: `root 目前僅警示，不自動暫停交易（健康來源 ${vars.providerCount || 0}/${vars.tradeMinProviders || 0}）`,
    root_warn_only_short: "root 目前僅警示，不自動暫停交易",
    risk_grade_unavailable_short: "風控級價格目前不可用",
    reference_degraded: "reference 價格降級",
    reference_healthy_risk_usable: "reference 價格正常 · 風控級價格仍可用",
    reference_healthy_confidence: `reference 價格正常 · 風控級來源信心 ${vars.confidence || "-"}`,
    auto_selected_market: `未指定市場，已自動選用 ${vars.market || "-"}`,
    current_price_purpose_short: "展示 / 估值",
    current_price_degraded_short: "價格降級",
    current_price_paused_short: "價格降級 · 已暫停",
    current_price_warning_short: "價格正常 · 有提示",
    current_price_healthy_short: "價格正常",
    current_price_unavailable_short: "價格不可用",
    current_price_defaulted_short: "已自動選市場",
    pause_message: `${vars.kindLabel || "交易"}已因價格降級暫停：${vars.reason || "-"} · 目前健康來源 ${vars.providerCount || 0} 家，至少需要 ${vars.tradeMinProviders || 0} 家`,
    risk_usable_yes: "風控可用 yes",
    risk_usable_no: "風控可用 no",
  };
  return messages[key] || key;
}

function tradingPriceDegradePolicy(riskContext, kind = "market") {
  const settings = tradingState.settings || {};
  const safe = riskContext && typeof riskContext === "object" ? riskContext : {};
  const warningLanguage = tradingWarningLanguage();
  const priceConfidenceOnlyWarns = settings.disable_price_confidence_gates !== false;
  const tradeMinProviders = Math.max(1, Number(settings.price_fusion_trade_min_provider_count || 1));
  const providerCount = Math.max(
    0,
    Number.isFinite(Number(safe.provider_count))
      ? Number(safe.provider_count)
      : Number(safe.risk_grade_provider_count || 0)
  );
  const conservativeMode = !!safe.conservative_mode;
  const fallback = !!safe.fallback;
  const stale = !!safe.stale;
  const degraded = !!safe.degraded;
  const providerShort = conservativeMode && providerCount < tradeMinProviders;
  const severeDegrade = fallback || stale || (degraded && !conservativeMode) || providerShort;
  let policyEnabled = false;
  let kindLabel = warningLanguage === "en" ? "Trading" : "交易";
  if (kind === "bot") {
    policyEnabled = !priceConfidenceOnlyWarns && !!settings.price_degrade_pause_bots;
    kindLabel = tradingWarningText("bot_kind");
  } else if (kind === "borrowing") {
    policyEnabled = !priceConfidenceOnlyWarns && !!settings.price_degrade_pause_borrowing;
    kindLabel = tradingWarningText("borrowing_kind");
  } else {
    policyEnabled = !priceConfidenceOnlyWarns && !!settings.price_degrade_pause_market_orders;
    kindLabel = tradingWarningText("market_kind");
  }
  return {
    kind,
    kindLabel,
    policyEnabled,
    shouldPause: policyEnabled && severeDegrade,
    conservativeMode,
    severeDegrade,
    providerShort,
    providerCount,
    tradeMinProviders,
  };
}

function tradingPriceDegradePauseMessage(kindLabel, riskContext, policy) {
  const safe = riskContext && typeof riskContext === "object" ? riskContext : {};
  const applied = policy || tradingPriceDegradePolicy(safe);
  const fallbackReason = tradingWarningLanguage() === "en"
    ? "Current price source is not suitable for risk-grade execution"
    : "價格來源目前不適合風控級成交";
  const reason = String(safe.warning_message || safe.high_risk_block_reason || safe.fallback_reason || fallbackReason).trim();
  return tradingWarningText("pause_message", {
    kindLabel,
    reason,
    providerCount: applied.providerCount,
    tradeMinProviders: applied.tradeMinProviders,
  });
}

function tradingRequestId(prefix = "trading") {
  if (typeof economyRequestId === "function") return economyRequestId(prefix);
  if (window.crypto && typeof window.crypto.randomUUID === "function") return `${prefix}:${window.crypto.randomUUID()}`;
  return `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
}

function tradingSetMsg(text, ok = true) {
  const kind = ok === false ? "err" : ok === null ? "info" : "ok";
  const apply = (msg) => {
    if (!msg) return;
    msg.textContent = text || "";
    msg.className = text ? `msg show ${kind}` : "msg";
    scheduleInlineMessageClear(msg, text, ok);
  };
  const targets = [
    $("trading-inline-msg"),
    $("trading-root-inline-msg"),
    $("trading-msg"),
  ].filter(Boolean);
  targets.forEach(apply);
  if (!targets.length && typeof economySetMsg === "function") {
    economySetMsg(text, ok);
    return;
  }
}

function tradingSetBackgroundStatus(text, ok = true) {
  const status = $("trading-background-status");
  if (!status) return;
  if (!text || normalizeTradingPage(tradingActivePage) !== "spot") {
    status.textContent = "";
    status.style.display = "none";
    return;
  }
  status.textContent = text;
  status.style.display = "";
  status.style.color = ok ? "var(--muted)" : "#ff4f6d";
}

function tradingErrorText(json, fallback = "操作失敗") {
  if (!json || typeof json !== "object") return fallback;
  if (json.msg) return String(json.msg);
  if (json.message) return String(json.message);
  if (json.error) return String(json.error);
  if (Array.isArray(json.errors) && json.errors.length) {
    return json.errors.map((item) => {
      if (!item) return "";
      if (typeof item === "string") return item;
      return item.msg || item.message || item.error || JSON.stringify(item);
    }).filter(Boolean).slice(0, 3).join("；");
  }
  return fallback;
}

function tradingFriendlyErrorText(text, fallback = "操作失敗") {
  const raw = String(text || "").trim();
  if (!raw) return fallback;
  if (raw.includes("candles are required for selected backtest range")) {
    return "目前圖表 K 線不涵蓋你選的回測區間。系統會改由後端重抓歷史 K 線；若仍失敗，請縮小區間或改大時間週期。";
  }
  if (raw.includes("grid bot create is disabled for this market") || raw.includes("bots are disabled for this market")) {
    return "這個市場目前未開放交易機器人。請改選其他市場，或由 root 到交易市場 registry 開啟 allow_bots。";
  }
  if (raw.includes("unsupported workflow node type")) {
    const nodeType = raw.split(":").slice(1).join(":").trim();
    return nodeType
      ? `Workflow 節點類型目前不支援：${nodeType}。請重新套用目前版本模板或回編輯器重存。`
      : "Workflow 節點類型目前不支援。請重新套用目前版本模板或回編輯器重存。";
  }
  if (raw.includes("trading place_order forbidden in mode='dev_ready'")) {
    return "目前伺服器是 dev_ready 模式，交易下單被停用。請切到 test / internal_test / production 後再下單。";
  }
  if (raw.includes("market order is blocked while fused price is in conservative mode")) {
    const reason = raw.split(":").slice(1).join(":").trim();
    return reason
      ? `目前風控級價格不可用，市價單已暫停：${reason}。可等價格來源恢復，或改用有價格上限的限價單。`
      : "目前風控級價格不可用，市價單已暫停。可等價格來源恢復，或改用有價格上限的限價單。";
  }
  if (raw.includes("尚未收到任何即時價格更新") || raw.includes("啟動時的預設參考價")) {
    return `${raw}。開發測試站請用 test_for_develop.sh 啟動；正式站請等待價格來源完成暖機。`;
  }
  return raw;
}

function tradingIsAbortError(err) {
  const name = String(err?.name || "").toLowerCase();
  const message = String(err?.message || err || "").toLowerCase();
  return name === "aborterror" || message.includes("abort") || message.includes("aborted") || message.includes("signal is aborted");
}

async function fetchTradingJson(url, options = {}) {
  const rawOptions = options || {};
  const method = String(rawOptions.method || "GET").toUpperCase();
  const { forceCsrf = method !== "GET", allowMissingSnapshot = false, ...requestOptions } = rawOptions;
  await fetchCsrfToken({ force: !!forceCsrf });
  const headers = { ...(requestOptions.headers || {}), "X-CSRF-Token": getCsrfToken() || "" };
  if (requestOptions.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const res = await apiFetch(API + url, { credentials: "same-origin", ...requestOptions, headers });
  const raw = await res.text().catch(() => "");
  let json = {};
  try {
    json = raw ? JSON.parse(raw) : {};
  } catch (_) {
    json = {};
  }
  if (allowMissingSnapshot && json?.snapshot?.missing) return json;
  if (!res.ok || !json.ok) {
    let fallback = `HTTP ${res.status}`;
    if (res.status === 404) fallback = `交易所 API 不存在：${url}。請確認伺服器已重啟且目前是包含交易所功能的分支。`;
    else if (raw && raw.trim() && !/^not found$/i.test(raw.trim())) fallback = raw.slice(0, 220);
    const text = tradingFriendlyErrorText(tradingErrorText(json, fallback), fallback);
    throw new Error(text || `HTTP ${res.status}`);
  }
  return json;
}

function scheduleTradingMutationRefresh(delayMs = 120, operation = null) {
  const operationContext = operation || tradingOperationContext();
  if (!tradingOperationIsCurrent(operationContext)) return;
  if (tradingMutationRefreshTimer) clearTimeout(tradingMutationRefreshTimer);
  tradingMutationRefreshTimer = setTimeout(async () => {
    tradingMutationRefreshTimer = null;
    if (!tradingOperationIsCurrent(operationContext)) return;
    if (tradingMutationRefreshBusy) {
      scheduleTradingMutationRefresh(300, operationContext);
      return;
    }
    tradingMutationRefreshBusy = true;
    try {
      await loadTradingDashboard();
    } catch (_) {
      // The order already succeeded; the next scheduled refresh will retry state sync.
    } finally {
      if (tradingOperationIsCurrent(operationContext)) tradingMutationRefreshBusy = false;
    }
  }, Math.max(0, Number(delayMs) || 0));
}

async function tradingFreshCsrfToken() {
  return await fetchCsrfToken({ force: true });
}

function bindTradingActionButton(el, handler, pendingText, fallbackText) {
  if (!el || typeof handler !== "function") return;
  el.addEventListener("click", async (event) => {
    event.preventDefault();
    if (el.disabled) return;
    const previousText = el.textContent;
    el.disabled = true;
    el.setAttribute("aria-busy", "true");
    tradingActiveActionButton = el;
    if (pendingText) tradingSetMsg(pendingText, null);
    try {
      await handler(event);
    } catch (err) {
      tradingSetMsg(`${fallbackText || "操作失敗"}：${err?.message || "未提供錯誤原因"}`, false);
    } finally {
      tradingActiveActionButton = null;
      el.disabled = false;
      el.removeAttribute("aria-busy");
      if (previousText) el.textContent = previousText;
    }
  });
}

function selectedTradingMarket() {
  const symbol = $("trading-market-select")?.value || "";
  return tradingState.markets.find((market) => market.symbol === symbol) || tradingState.markets[0] || null;
}

function tradingUsablePricePoints(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0;
}

function tradingPriceContextHasUsablePrice(context) {
  return !!(context && typeof context === "object" && tradingUsablePricePoints(context.price_points));
}

function tradingMergeLivePriceContext(previousContext, nextContext) {
  const previous = previousContext && typeof previousContext === "object" ? previousContext : null;
  const next = nextContext && typeof nextContext === "object" ? nextContext : null;
  if (!next) return previous;
  if (tradingPriceContextHasUsablePrice(next)) return next;
  if (!tradingPriceContextHasUsablePrice(previous)) return next;
  const warning = String(next.warning_message || "").trim();
  return {
    ...next,
    price_points: previous.price_points,
    source: next.source || previous.source,
    source_label: next.source_label || previous.source_label,
    stale_price_for_display: true,
    warning_message: warning
      ? `${warning} · 沿用上一筆可用風控價顯示盈虧`
      : "沿用上一筆可用風控價顯示盈虧",
  };
}

function tradingMergeLiveMarket(previousMarket, nextMarket) {
  const previous = previousMarket && typeof previousMarket === "object" ? previousMarket : {};
  const next = nextMarket && typeof nextMarket === "object" ? nextMarket : {};
  const merged = { ...previous, ...next };
  [
    ["reference_price_context", "reference_price_points"],
    ["risk_grade_price_context", "risk_grade_price_points"],
  ].forEach(([contextKey, priceKey]) => {
    const context = tradingMergeLivePriceContext(previous[contextKey], next[contextKey]);
    if (context) merged[contextKey] = context;
    if (tradingPriceContextHasUsablePrice(context)) {
      merged[priceKey] = context.price_points;
    } else if (!tradingUsablePricePoints(merged[priceKey]) && tradingUsablePricePoints(previous[priceKey])) {
      merged[priceKey] = previous[priceKey];
    }
  });
  return merged;
}

function tradingMarketPriceContext(market, priceType = "reference") {
  const type = priceType === "risk_grade" ? "risk_grade" : "reference";
  const contextKey = type === "risk_grade" ? "risk_grade_price_context" : "reference_price_context";
  const symbol = String(market?.symbol || "");
  const liveMeta = tradingState.livePriceMeta || {};
  const liveContext = tradingMergeLivePriceContext(market?.[contextKey], liveMeta[symbol]?.[contextKey]);
  if (liveContext && typeof liveContext === "object") {
    return {
      ...liveContext,
      conservative_mode: !!liveMeta[symbol]?.conservative_mode || liveMeta[symbol]?.price_health === "conservative",
      minimum_provider_count: Number(liveMeta[symbol]?.minimum_provider_count || liveContext.minimum_provider_count || 0),
    };
  }
  if (market?.[contextKey] && typeof market[contextKey] === "object") {
    return {
      ...market[contextKey],
      conservative_mode: !!market?.conservative_mode || market?.price_health === "conservative",
      minimum_provider_count: Number(market?.minimum_provider_count || market[contextKey].minimum_provider_count || 0),
    };
  }
  const source = String(market?.price_source || "manual_root");
  const pricePoints = type === "risk_grade"
    ? tradingNumber(market?.risk_grade_price_points ?? market?.manual_price_points, 0)
    : tradingNumber(market?.reference_price_points ?? market?.manual_price_points, 0);
  return {
    price_type: type,
    price_points: pricePoints,
    source,
    source_label: source,
    confidence: source === "manual_root" ? "manual" : "unknown",
    stale: source.endsWith("_cached"),
    degraded: source === "manual_root" || source.endsWith("_cached"),
    provider_count: source === "manual_root" ? 0 : 1,
    purpose: type === "risk_grade" ? "融資 / 強平 / 保證金 / PnL / bot 風控 / 交易限制" : "展示 / 一般估值 / K 線 / 非風控參考",
    warning_message: source === "manual_root" ? "目前使用手動價格" : (source.endsWith("_cached") ? "目前使用最後健康快取" : ""),
    high_risk_blocked: false,
    risk_grade_usable: type === "risk_grade" && source !== "manual_root" && !source.endsWith("_cached"),
    conservative_mode: false,
    minimum_provider_count: 0,
    warnings: [],
    excluded_sources: [],
  };
}

function tradingMarketPricePoints(market, priceType = "reference") {
  const context = tradingMarketPriceContext(market, priceType);
  const fallback = priceType === "risk_grade"
    ? (market?.risk_grade_price_points ?? market?.reference_price_points ?? market?.manual_price_points)
    : (market?.reference_price_points ?? market?.manual_price_points);
  return tradingNumber(context?.price_points ?? fallback, 0);
}

function tradingPriceConfidenceLabel(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === "high") return "高";
  if (normalized === "medium") return "中";
  if (normalized === "low") return "低";
  if (normalized === "manual") return "手動";
  return normalized || "-";
}

function tradingPriceContextSummary(context, { compact = false } = {}) {
  const safe = context && typeof context === "object" ? context : {};
  const sourceLabel = safe.source_label || safe.source || "未知來源";
  const providerText = Number.isFinite(Number(safe.provider_count))
    ? `來源 ${Number(safe.provider_count)} 家`
    : "來源數未知";
  const confidenceText = `信心 ${tradingPriceConfidenceLabel(safe.confidence)}`;
  const stateText = safe.stale ? "stale" : (safe.degraded ? "degraded" : "正常");
  const riskGradeUsableText = safe.price_type === "risk_grade"
    ? (safe.risk_grade_usable ? tradingWarningText("risk_usable_yes") : tradingWarningText("risk_usable_no"))
    : "";
  const warning = String(safe.warning_message || "").trim();
  if (compact) {
    return `${sourceLabel} · ${confidenceText} · ${stateText}${riskGradeUsableText ? ` · ${riskGradeUsableText}` : ""}${warning ? ` · ${warning}` : ""}`;
  }
  return `${safe.purpose || ""} · ${sourceLabel} · ${providerText} · ${confidenceText} · ${stateText}${riskGradeUsableText ? ` · ${riskGradeUsableText}` : ""}${warning ? ` · ${warning}` : ""}`;
}

function tradingTransportStateSummary(state, { compact = false } = {}) {
  const safe = state && typeof state === "object" ? state : {};
  const mode = String(safe.mode || safe.transport || "http_polling_only");
  const connection = safe.connected ? "connected" : "disconnected";
  const fallback = safe.fallback ? "HTTP fallback" : "直連";
  const stale = safe.stale ? "stale" : "fresh";
  const confidence = `信心 ${tradingPriceConfidenceLabel(safe.confidence)}`;
  const providerText = Number.isFinite(Number(safe.provider_count)) ? `provider ${Number(safe.provider_count)}` : "provider ?";
  const reason = String(safe.exclusion_reason || safe.message || "").trim();
  const base = `${mode} · ${connection} · ${fallback} · ${stale} · ${confidence} · ${providerText}`;
  if (compact) return reason ? `${base} · ${reason}` : base;
  return reason ? `provider input：${base} · ${reason}` : `provider input：${base}`;
}

function setTradingPriceTooltip(el, text, detail = "") {
  if (!el) return;
  const cleanText = String(text || "").trim();
  const cleanDetail = String(detail || "").trim();
  el.textContent = cleanText;
  if (cleanDetail && cleanDetail !== cleanText) {
    el.dataset.tooltip = cleanDetail;
    el.title = cleanDetail;
    el.tabIndex = 0;
    el.classList.add("trading-price-tooltip");
  } else {
    delete el.dataset.tooltip;
    el.removeAttribute("title");
    el.removeAttribute("tabindex");
    el.classList.remove("trading-price-tooltip");
  }
}

function renderTradingCurrentPrice(market, options = {}) {
  const priceEl = $("trading-current-price");
  const labelEl = $("trading-current-label");
  const marketEl = $("trading-current-market");
  const deltaEl = $("trading-current-delta");
  const purposeEl = $("trading-current-purpose");
  const healthEl = $("trading-current-health");
  const animate = options.animate !== false;
  const symbol = String(market?.symbol || "");
  const referenceContext = options.referencePriceContext || tradingMarketPriceContext(market, "reference");
  const riskContext = options.riskGradePriceContext || tradingMarketPriceContext(market, "risk_grade");
  const nextPrice = tradingMarketPricePoints(market, "reference");
  const priceHistory = tradingState.livePriceHistory || (tradingState.livePriceHistory = {});
  const liveMeta = tradingState.livePriceMeta || {};
  const health = options.priceHealth || referenceContext?.health || liveMeta[symbol]?.price_health || "healthy";
  const fallbackReason = options.fallbackReason || referenceContext?.warning_message || liveMeta[symbol]?.fallback_reason || "";
  const excludedSources = Array.isArray(options.excludedSources) ? options.excludedSources : (referenceContext?.excluded_sources || liveMeta[symbol]?.excluded_sources || []);
  const warnings = Array.isArray(options.warnings) ? options.warnings : (referenceContext?.warnings || liveMeta[symbol]?.warnings || []);
  const highRiskBlockReason = options.highRiskBlockReason || riskContext?.warning_message || liveMeta[symbol]?.high_risk_block_reason || "";
  const defaultedMarket = options.defaultedMarket === true || liveMeta[symbol]?.defaulted_market === true;
  const transportState = options.transportState || liveMeta[symbol]?.transport_state || {};
  const marketPausePolicy = tradingPriceDegradePolicy(riskContext, "market");
  const botPausePolicy = tradingPriceDegradePolicy(riskContext, "bot");
  const borrowingPausePolicy = tradingPriceDegradePolicy(riskContext, "borrowing");
  if (labelEl) labelEl.textContent = "目前價格（reference）";
  setTradingPriceTooltip(
    purposeEl,
    `用途：${tradingWarningText("current_price_purpose_short")}`,
    `用途：展示 / 一般估值 · ${tradingPriceContextSummary(referenceContext, { compact: true })} · ${tradingTransportStateSummary(transportState, { compact: true })}`,
  );
  const previousPrice = symbol && Number.isFinite(priceHistory[symbol]) ? Number(priceHistory[symbol]) : null;
  if (priceEl) {
    priceEl.textContent = market ? formatTradingPointsValue(nextPrice) : "-";
    priceEl.classList.remove("trading-price-up", "trading-price-down", "trading-price-flat", "trading-price-flash");
  }
  if (marketEl) {
    marketEl.textContent = market
      ? `${tradingDisplaySymbol(market.symbol)} · ${referenceContext?.source_label || market.price_source || "last_good_cache"} · ${Math.round(tradingLivePriceRefreshMs() / 1000)} 秒更新`
      : "-";
  }
  if (!market || !Number.isFinite(nextPrice)) {
    if (deltaEl) {
      deltaEl.textContent = "即時價格暫不可用";
      deltaEl.classList.remove("positive", "negative");
    }
    if (healthEl) {
      setTradingPriceTooltip(healthEl, `🟡 ${tradingWarningText("current_price_unavailable_short")}`, "即時 reference 價格暫不可用；請稍後重試或改用限價。");
      healthEl.classList.add("warning");
    }
    return;
  }
  let direction = "flat";
  let delta = 0;
  if (previousPrice != null && Number.isFinite(previousPrice)) {
    delta = nextPrice - previousPrice;
    direction = delta > 0 ? "up" : (delta < 0 ? "down" : "flat");
  }
  priceHistory[symbol] = nextPrice;
  if (priceEl) {
    priceEl.classList.add(direction === "up" ? "trading-price-up" : (direction === "down" ? "trading-price-down" : "trading-price-flat"));
    if (animate && direction !== "flat") {
      void priceEl.offsetWidth;
      priceEl.classList.add("trading-price-flash");
    }
  }
  if (deltaEl) {
    if (direction === "up") {
      deltaEl.textContent = `▲ +${formatTradingPointsValue(delta)}`;
      deltaEl.classList.add("positive");
      deltaEl.classList.remove("negative");
    } else if (direction === "down") {
      deltaEl.textContent = `▼ ${formatTradingPointsValue(delta)}`;
      deltaEl.classList.add("negative");
      deltaEl.classList.remove("positive");
    } else {
      deltaEl.textContent = `即時輪詢 ${Math.round(tradingLivePriceRefreshMs() / 1000)} 秒`;
      deltaEl.classList.remove("positive", "negative");
    }
  }
  if (healthEl) {
    if (health === "conservative") {
      const notes = [];
      if (defaultedMarket) notes.push(`未指定市場，已改用 ${tradingDisplaySymbol(symbol)}`);
      if (highRiskBlockReason) notes.push(highRiskBlockReason);
      if (excludedSources.length) notes.push(`排除 ${excludedSources.join(", ")}`);
      if (transportState.fallback) notes.push("WebSocket provider input 已退回 HTTP polling");
      if (transportState.stale) notes.push("provider input stale");
      const pauseKinds = [marketPausePolicy, botPausePolicy, borrowingPausePolicy]
        .filter((item) => item.shouldPause)
        .map((item) => item.kindLabel);
      if (pauseKinds.length) {
        notes.unshift(tradingWarningText("root_auto_pause", { kinds: pauseKinds.join(" / ") }));
      } else {
        notes.unshift(tradingWarningText("root_warn_only", {
          providerCount: marketPausePolicy.providerCount,
          tradeMinProviders: marketPausePolicy.tradeMinProviders,
        }));
      }
      setTradingPriceTooltip(
        healthEl,
        `🟡 ${tradingWarningText(pauseKinds.length ? "current_price_paused_short" : "current_price_degraded_short")}`,
        `${tradingWarningText("degrade_light")}${notes.length ? ` · ${notes.join(" · ")}` : ""}`,
      );
      healthEl.classList.add("warning");
    } else if (
      health === "fallback"
      || health === "degraded"
      || transportState.fallback
      || transportState.stale
      || transportState.degraded
      || riskContext?.high_risk_blocked
      || riskContext?.risk_grade_usable === false
    ) {
      const notes = [];
      if (excludedSources.length) notes.push(`排除 ${excludedSources.join(", ")}`);
      if (fallbackReason) notes.push(fallbackReason);
      if (!fallbackReason && warnings.length) notes.push(String(warnings[0]?.message || warnings[0]?.code || ""));
      if (!fallbackReason && !warnings.length && riskContext?.warning_message) notes.push(riskContext.warning_message);
      if (transportState.fallback) notes.push("WebSocket provider input 已退回 HTTP polling");
      if (transportState.stale) notes.push("provider input stale");
      if (riskContext?.risk_grade_usable === false && !riskContext?.high_risk_blocked) notes.push(tradingWarningText("risk_grade_unavailable_short"));
      const pauseKinds = [marketPausePolicy, botPausePolicy, borrowingPausePolicy]
        .filter((item) => item.shouldPause)
        .map((item) => item.kindLabel);
      if (pauseKinds.length) notes.unshift(tradingWarningText("root_auto_pause", { kinds: pauseKinds.join(" / ") }));
      else notes.unshift(tradingWarningText("root_warn_only_short"));
      if (defaultedMarket) notes.push(tradingWarningText("auto_selected_market", { market: tradingDisplaySymbol(symbol) }));
      setTradingPriceTooltip(
        healthEl,
        `🟡 ${tradingWarningText(pauseKinds.length ? "current_price_paused_short" : "current_price_degraded_short")}`,
        `${tradingWarningText("reference_degraded")}${notes.length ? ` · ${notes.join(" · ")}` : ""}`,
      );
      healthEl.classList.add("warning");
    } else if (excludedSources.length || warnings.length || referenceContext?.warning_only || riskContext?.warning_only) {
      const notes = [];
      if (excludedSources.length) notes.push(`已自動排除 ${excludedSources.join(", ")}`);
      if (warnings.length) notes.push(String(warnings[0]?.message || warnings[0]?.code || ""));
      setTradingPriceTooltip(
        healthEl,
        `🟢 ${tradingWarningText("current_price_warning_short")}`,
        `${tradingWarningText("reference_healthy_risk_usable")}${notes.length ? ` · ${notes.join(" · ")}` : ""}`,
      );
      healthEl.classList.remove("warning");
    } else if (defaultedMarket) {
      setTradingPriceTooltip(
        healthEl,
        `🟢 ${tradingWarningText("current_price_defaulted_short")}`,
        tradingWarningText("auto_selected_market", { market: tradingDisplaySymbol(symbol) }),
      );
      healthEl.classList.remove("warning");
    } else {
      setTradingPriceTooltip(
        healthEl,
        `🟢 ${tradingWarningText("current_price_healthy_short")}`,
        tradingWarningText("reference_healthy_confidence", { confidence: tradingPriceConfidenceLabel(riskContext?.confidence) }),
      );
      healthEl.classList.remove("warning");
    }
  }
}

function tradingDisplaySymbol(symbol) {
  const normalized = String(symbol || "").trim().toUpperCase();
  const market = (tradingState.markets || []).find((row) => String(row?.symbol || "").trim().toUpperCase() === normalized);
  if (market?.display_symbol) return String(market.display_symbol);
  return normalized.replace("/POINTS", "/USDT");
}

function tradingMarketRequestSymbol(symbol) {
  return tradingDisplaySymbol(symbol || "");
}

function tradingAssetDisplayLabel(asset) {
  const normalized = String(asset || "").trim().toUpperCase();
  if (!normalized || normalized === "POINTS") return "積分";
  return normalized;
}

function tradingBaseAssetLabel(marketOrSymbol) {
  const market = typeof marketOrSymbol === "object"
    ? marketOrSymbol
    : tradingMarketBySymbol(String(marketOrSymbol || ""));
  return String(market?.base_asset || tradingDisplaySymbol(market?.symbol || marketOrSymbol).split("/")[0] || "資產").toUpperCase();
}

function tradingBorrowAprGroupForMarket(market, positionType = "margin_long") {
  const normalizedType = String(positionType || "margin_long").toLowerCase();
  const asset = normalizedType === "short"
    ? String(market?.base_asset || "").toUpperCase()
    : String(market?.quote_currency || "USDT").toUpperCase();
  return asset === "BTC" || asset === "ETH" ? "btc_eth" : "usdt_points";
}

function tradingBorrowBaseAprPercent(group) {
  const settings = tradingState.settings || {};
  if (group === "btc_eth") return tradingNumber(settings.borrow_apr_btc_eth_percent, 8);
  return tradingNumber(settings.borrow_apr_usdt_points_percent, 10);
}

function tradingBorrowEffectiveAprPercent(group, utilizationPercent = null) {
  const util = tradingNumber(
    utilizationPercent ?? tradingState.fundingPool?.utilization_percent,
    0,
  ) / 100;
  const pressure = tradingNumber(tradingState.settings?.borrow_interest_pool_pressure_multiplier, 4);
  const baseApr = tradingBorrowBaseAprPercent(group);
  return baseApr * (1 + Math.max(0, util) * Math.max(0, pressure));
}

function tradingBorrowTimingSummary() {
  const intervalHours = Math.max(1, tradingNumber(tradingState.settings?.borrow_interest_interval_hours, 1));
  const minimumHours = Math.max(1, tradingNumber(tradingState.settings?.borrow_interest_minimum_hours, 1));
  return {
    intervalHours,
    minimumHours,
    text: `每 ${formatTradingPointsValue(intervalHours)} 小時計息，不足 ${formatTradingPointsValue(minimumHours)} 小時以 ${formatTradingPointsValue(minimumHours)} 小時計`,
  };
}

function formatTradingPointsValue(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function tradingPercentValue(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function tradingInputPercent(value, fallback = 0) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.round(number * 10000) / 10000;
}

function formatTradingPercent(value, fallback = 0) {
  return formatTradingPointsValue(tradingPercentValue(value, fallback));
}

function formatTradingDuration(ms) {
  const totalSeconds = Math.max(0, Math.floor(Number(ms || 0) / 1000));
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (days > 0) return `${days}天 ${hours}小時 ${minutes}分`;
  if (hours > 0) return `${hours}小時 ${minutes}分 ${seconds}秒`;
  if (minutes > 0) return `${minutes}分 ${seconds}秒`;
  return `${seconds}秒`;
}

const BACKTEST_TOTAL_CANDLE_LIMIT = 20000;
const TRADING_BACKTEST_CONTEXTS = {
  dca: { tab: "dca", botType: "dca", prefix: "trading-dca-backtest" },
  grid: { tab: "grid", botType: "grid", prefix: "trading-grid-backtest" },
  workflow: { tab: "strategy", botType: "workflow", prefix: "trading-workflow-backtest" },
};

function tradingBacktestConfig(contextKey = "dca") {
  return TRADING_BACKTEST_CONTEXTS[contextKey] || TRADING_BACKTEST_CONTEXTS.dca;
}

function tradingBacktestEl(contextKey, suffix) {
  const cfg = tradingBacktestConfig(contextKey);
  return $(`${cfg.prefix}-${suffix}`);
}

function tradingTimeframeMinutes(timeframe) {
  const mapping = { "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440 };
  return mapping[String(timeframe || "15m").trim()] || 15;
}

function estimateBacktestRequestedCandles(startTime, endTime, timeframe) {
  const startMs = startTime ? Date.parse(startTime) : NaN;
  const endMs = endTime ? Date.parse(endTime) : NaN;
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return 0;
  const intervalMs = tradingTimeframeMinutes(timeframe) * 60 * 1000;
  return Math.max(2, Math.floor((endMs - startMs) / intervalMs) + 1);
}

function formatBacktestDatetimeLocal(ms) {
  if (!Number.isFinite(ms)) return "";
  const date = new Date(ms);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function backtestTimeframeLabel(contextKey = "dca") {
  const select = tradingBacktestEl(contextKey, "timeframe");
  return select?.selectedOptions?.[0]?.textContent?.trim() || `${tradingTimeframeMinutes(select?.value)} 分`;
}

function updateBacktestDateRangeGuidance(contextKey = "dca") {
  const hint = tradingBacktestEl(contextKey, "date-hint");
  const startEl = tradingBacktestEl(contextKey, "start");
  const endEl = tradingBacktestEl(contextKey, "end");
  if (!hint || !startEl || !endEl) return;
  const timeframe = tradingBacktestEl(contextKey, "timeframe")?.value || "15m";
  const timeframeText = backtestTimeframeLabel(contextKey);
  const intervalMs = tradingTimeframeMinutes(timeframe) * 60 * 1000;
  const maxSpanMs = Math.max(0, (BACKTEST_TOTAL_CANDLE_LIMIT - 1) * intervalMs);
  const maxSpanText = formatTradingDuration(maxSpanMs);
  const startMs = startEl.value ? Date.parse(startEl.value) : NaN;
  const endMs = endEl.value ? Date.parse(endEl.value) : NaN;

  endEl.min = Number.isFinite(startMs) ? formatBacktestDatetimeLocal(startMs) : "";
  endEl.max = Number.isFinite(startMs) ? formatBacktestDatetimeLocal(startMs + maxSpanMs) : "";
  startEl.max = Number.isFinite(endMs) ? formatBacktestDatetimeLocal(endMs) : "";
  startEl.min = Number.isFinite(endMs) ? formatBacktestDatetimeLocal(endMs - maxSpanMs) : "";

  let text = `以目前 ${timeframeText} 週期，單次回測最多約 ${maxSpanText}。`;
  let color = "var(--muted)";

  if (!Number.isFinite(startMs) && !Number.isFinite(endMs)) {
    text += " 先選開始或結束時間，系統會提示另一側最遠可選到哪裡。";
  } else if (Number.isFinite(startMs) && !Number.isFinite(endMs)) {
    text += ` 若保留開始時間，結束最晚可選 ${formatBacktestDatetimeLocal(startMs + maxSpanMs)}。`;
  } else if (!Number.isFinite(startMs) && Number.isFinite(endMs)) {
    text += ` 若保留結束時間，開始最早可選 ${formatBacktestDatetimeLocal(endMs - maxSpanMs)}。`;
  } else if (endMs < startMs) {
    color = "#ff4f6d";
    text = `結束時間不能早於開始時間。若保留開始時間，結束至少要從 ${formatBacktestDatetimeLocal(startMs)} 開始。`;
  } else {
    const estimatedCandles = estimateBacktestRequestedCandles(startEl.value, endEl.value, timeframe);
    const selectedWindowMs = Math.max(0, endMs - startMs);
    if (estimatedCandles > BACKTEST_TOTAL_CANDLE_LIMIT) {
      color = "#ffb74d";
      text = `這段時間對 ${timeframeText} 週期來說太長了。若保留開始時間，結束最晚可選 ${formatBacktestDatetimeLocal(startMs + maxSpanMs)}；若保留結束時間，開始最早可選 ${formatBacktestDatetimeLocal(endMs - maxSpanMs)}。`;
    } else {
      text += ` 目前區間約 ${formatTradingDuration(selectedWindowMs)}，仍在單次回測範圍內。若保留開始時間，結束最晚可選 ${formatBacktestDatetimeLocal(startMs + maxSpanMs)}。`;
    }
  }

  hint.textContent = text;
  hint.style.color = color;
}

function tradingTrialCountdownText(trial) {
  if (!trial || trial.status !== "active") return "";
  const expiresAt = trial.expires_at ? new Date(trial.expires_at).getTime() : 0;
  if (!expiresAt || Number.isNaN(expiresAt)) return "到期時間未設定";
  const remaining = expiresAt - Date.now();
  if (remaining <= 0) return "體驗金已到期，等待下次交易狀態刷新";
  return `倒數 ${formatTradingDuration(remaining)}`;
}

function updateTradingTrialCountdown() {
  const funding = tradingState.funding || {};
  const trial = funding.trial_credit || null;
  const note = $("trading-trial-credit-note");
  if (!note) return;
  if (!trial) {
    note.textContent = "root 不適用";
    return;
  }
  if (trial.status !== "active") {
    note.textContent = `狀態 ${trial.status}`;
    return;
  }
  const countdown = tradingTrialCountdownText(trial);
  note.textContent = `${countdown} · 鎖定 ${formatTradingPointsValue(trial.locked_points)} · 部位 ${formatTradingPointsValue(trial.deployed_points)} · 初始 ${formatTradingPointsValue(trial.initial_points)}`;
}

function tradingNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function tradingSettlementFeePoints(notional, feeRatePercent) {
  return tradingMicropointsToSettlementPoints(tradingFeeMicropoints(notional, feeRatePercent));
}

function tradingFeeMicropoints(notional, feeRatePercent) {
  const exactFee = Number(notional || 0) * Number(feeRatePercent || 0) * TRADING_POINT_MICRO_SCALE / 100;
  if (!Number.isFinite(exactFee) || exactFee <= 0) return 0;
  return Math.round(exactFee);
}

function tradingMicropointsToSettlementPoints(totalMicropoints) {
  const micro = Math.max(0, Math.round(tradingNumber(totalMicropoints, 0)));
  if (micro <= 0) return 0;
  return Math.ceil(micro / TRADING_POINT_MICRO_SCALE - Number.EPSILON);
}

function currentTradingPosition(marketSymbol) {
  return tradingState.positions.find((row) => row.market_symbol === marketSymbol) || null;
}

function tradingSellableState(marketSymbol = selectedTradingMarket()?.symbol || "") {
  const market = tradingMarketBySymbol(marketSymbol) || selectedTradingMarket();
  const position = currentTradingPosition(marketSymbol);
  const available = Math.max(0, spotPositionNumber(position, "quantity"));
  const locked = Math.max(0, spotPositionNumber(position, "locked_quantity"));
  const total = available + locked;
  const riskValue = position ? tradingSpotRiskGradeValue(position, market) : 0;
  const asset = tradingDisplaySymbol(marketSymbol || market?.symbol || "").split("/")[0] || "資產";
  return { market, position, available, locked, total, riskValue, asset };
}

function tradingMarketBySymbol(symbol) {
  return tradingState.markets.find((row) => row.symbol === symbol) || null;
}

function tradingLivePriceTargetSymbols() {
  const symbols = new Set();
  if (currentModuleTab === "trading") {
    const selected = selectedTradingMarket();
    if (selected?.symbol) symbols.add(selected.symbol);
  }
  (tradingState.positions || []).forEach((row) => {
    const quantity = tradingNumber(row?.quantity, 0) + tradingNumber(row?.locked_quantity, 0);
    if (quantity > 0 && row?.market_symbol) symbols.add(row.market_symbol);
  });
  (tradingState.marginPositions || []).forEach((row) => {
    if (row?.status === "open" && row?.market_symbol) symbols.add(row.market_symbol);
  });
  return Array.from(symbols);
}

function tradingOrderInputMode() {
  return $("trading-input-mode")?.value === "points" ? "points" : "quantity";
}

function formatTradingQuantityValue(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString(undefined, { maximumFractionDigits: 8 });
}

function tradingQuantityForSubmit(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "";
  return number.toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
}

function syncTradingOrderInputMode() {
  const inputMode = tradingOrderInputMode();
  const side = $("trading-side")?.value === "sell" ? "sell" : "buy";
  const input = $("trading-quantity");
  const label = $("trading-quantity-label");
  const note = $("trading-input-mode-note");
  if (label) label.textContent = inputMode === "points" ? "點數金額" : "數量";
  if (input) {
    input.min = inputMode === "points" ? "1" : "0.00000001";
    input.step = inputMode === "points" ? "1" : "0.00000001";
    if (side === "sell" && inputMode === "quantity") {
      const state = tradingSellableState();
      input.max = tradingQuantityForSubmit(state.available) || "0";
      input.placeholder = state.available > 0
        ? `最多 ${formatTradingQuantityValue(state.available)} ${state.asset}`
        : "目前沒有可賣現貨";
    } else {
      input.removeAttribute("max");
      input.placeholder = inputMode === "points" ? "例如 1000" : "例如 0.01";
    }
  }
  if (note) {
    const assetLabel = tradingBaseAssetLabel(selectedTradingMarket());
    if (side === "sell") {
      const state = tradingSellableState();
      note.textContent = inputMode === "points"
        ? `賣出時點數視為成交名目金額，系統會換算枚數；目前可賣 ${formatTradingQuantityValue(state.available)} ${state.asset}。`
        : `直接輸入 ${assetLabel} 枚數；目前可賣 ${formatTradingQuantityValue(state.available)} ${state.asset}。`;
    } else {
      note.textContent = inputMode === "points"
        ? "買入時點數視為含手續費的總支出；賣出時點數視為成交名目金額，系統自動換算枚數。"
        : `直接輸入 ${assetLabel} 枚數。`;
    }
  }
}

function fillTradingSellableQuantity() {
  const state = tradingSellableState();
  if (!state.available) {
    tradingSetMsg("此交易對目前沒有可賣現貨", false);
    return;
  }
  const mode = $("trading-input-mode");
  const quantity = $("trading-quantity");
  if (mode) mode.value = "quantity";
  if (quantity) {
    quantity.value = tradingQuantityForSubmit(state.available);
    quantity.focus();
  }
  syncTradingOrderInputMode();
  updateTradingOrderEstimate();
}

function updateTradingSellableHint() {
  const hint = $("trading-sellable-hint");
  if (!hint) return;
  const side = $("trading-side")?.value === "sell" ? "sell" : "buy";
  const market = selectedTradingMarket();
  if (side !== "sell" || !market) {
    hint.hidden = true;
    hint.innerHTML = "";
    hint.removeAttribute("data-market");
    hint.removeAttribute("data-sellable-quantity");
    hint.removeAttribute("data-asset");
    hint.classList.remove("has-sellable", "no-sellable");
    return;
  }
  const state = tradingSellableState(market.symbol);
  const wallet = state.position?.source_wallet_address || state.position?.wallet_address || state.position?.hot_wallet_address || "";
  const hasSellable = state.available > 0;
  hint.hidden = false;
  hint.setAttribute("data-market", market.symbol || "");
  hint.setAttribute("data-sellable-quantity", tradingQuantityForSubmit(state.available) || "0");
  hint.setAttribute("data-asset", state.asset || "");
  hint.classList.toggle("has-sellable", hasSellable);
  hint.classList.toggle("no-sellable", !hasSellable);
  hint.setAttribute("aria-label", hasSellable
    ? `${state.asset} 可賣 ${formatTradingQuantityValue(state.available)}`
    : `${state.asset} 目前沒有可賣現貨`);
  hint.innerHTML = `
    <div>
      <strong>${hasSellable ? "目前可賣" : "目前沒有可賣"} ${sanitize(formatTradingQuantityValue(state.available))} ${sanitize(state.asset)}</strong>
      <span>選擇賣出 ${sanitize(tradingDisplaySymbol(market.symbol))} · 總持有 ${sanitize(formatTradingQuantityValue(state.total))} · 鎖定 ${sanitize(formatTradingQuantityValue(state.locked))} · 估值 ${formatTradingPointsValue(state.riskValue)} 點${wallet ? ` · ${sanitize(tradingShortWalletAddress(wallet))}` : ""}</span>
    </div>
    <button class="btn btn-sm" type="button" data-trading-fill-sellable="1"${hasSellable ? "" : " disabled"}>填入全部</button>
  `;
}

function tradingOrderDraftEstimate() {
  const market = selectedTradingMarket();
  if (!market) return { ok: false, blocking: true, message: "沒有可用交易市場" };
  const side = $("trading-side")?.value || "buy";
  const orderType = $("trading-order-type")?.value || "market";
  const inputMode = tradingOrderInputMode();
  const rawInputValue = String($("trading-quantity")?.value || "").trim();
  const inputValue = tradingNumber(rawInputValue, 0);
  const limitPrice = tradingNumber($("trading-limit-price")?.value, 0);
  const referenceContext = tradingMarketPriceContext(market, "reference");
  const riskContext = tradingMarketPriceContext(market, "risk_grade");
  const marketPausePolicy = tradingPriceDegradePolicy(riskContext, "market");
  const priceConfidenceOnlyWarns = tradingState.settings?.disable_price_confidence_gates !== false || !!tradingState.settings?.dev_allow_conservative_market_orders;
  const riskGradePrice = tradingMarketPricePoints(market, "risk_grade");
  const referencePrice = tradingMarketPricePoints(market, "reference");
  const price = orderType === "limit"
    ? limitPrice
    : (riskGradePrice > 0 ? riskGradePrice : (marketPausePolicy.shouldPause ? riskGradePrice : referencePrice));
  const feeRate = tradingNumber(market.fee_rate_percent, 0) / 100;
  if (rawInputValue && inputValue <= 0) {
    return {
      ok: false,
      blocking: true,
      message: inputMode === "points" ? "點數金額必須大於 0" : "交易數量必須大於 0",
    };
  }
  if (!inputValue) {
    return {
      ok: false,
      blocking: false,
      message: inputMode === "points" ? "輸入點數後自動換算枚數" : "輸入數量後顯示預估金額",
    };
  }
  if (!price || price <= 0) {
    return {
      ok: false,
      blocking: true,
      message: orderType === "limit"
        ? "請輸入有效限價"
        : (marketPausePolicy.shouldPause
          ? tradingPriceDegradePauseMessage("市價交易", riskContext, marketPausePolicy)
          : "目前無法取得可用成交估值，請稍後再試"),
    };
  }
  if (orderType !== "limit" && !priceConfidenceOnlyWarns && (riskContext?.high_risk_blocked || riskContext?.risk_grade_usable === false)) {
    return {
      ok: false,
      blocking: true,
      message: `${tradingPriceDegradePauseMessage("市價交易", riskContext, marketPausePolicy)}。可等價格來源恢復，或改用有價格上限的限價單。`,
    };
  }
  if (orderType !== "limit" && marketPausePolicy.shouldPause) {
    return {
      ok: false,
      blocking: true,
      message: `${tradingPriceDegradePauseMessage("市價交易", riskContext, marketPausePolicy)} · ${tradingPriceContextSummary(riskContext, { compact: true })}`,
    };
  }
  let quantity = inputValue;
  if (inputMode === "points") {
    const denominator = side === "buy" ? price * (1 + feeRate) : price;
    quantity = denominator > 0 ? inputValue / denominator : 0;
  }
  if (!quantity || quantity <= 0) {
    return { ok: false, blocking: true, message: "點數金額太小，無法換算有效枚數" };
  }
  const notional = quantity * price;
  const fee = Math.max(0, notional * feeRate);
  const quantityNote = inputMode === "points"
    ? `，約 ${formatTradingQuantityValue(quantity)} ${tradingDisplaySymbol(market.symbol).split("/")[0]}`
    : "";
  const funding = tradingState.funding || {};
  const availablePoints = tradingNumber(funding.available_points, 0);
  const position = currentTradingPosition(market.symbol);
  const positionQuantity = tradingNumber(position?.quantity, 0);
  const sellableQuantity = Math.max(0, positionQuantity);
  const orderPriceNote = orderType === "limit"
    ? `限價單以你輸入的價格為準；目前 reference 價 ${formatTradingPointsValue(tradingMarketPricePoints(market, "reference"))} · ${tradingPriceContextSummary(referenceContext, { compact: true })}`
    : (riskGradePrice > 0
      ? `市價單估值採用風控級價格；${tradingPriceContextSummary(riskContext, { compact: true })}`
      : `市價單暫以 reference 價估值；${tradingPriceContextSummary(referenceContext, { compact: true })}`);
  if (side === "buy") {
    const total = notional + fee;
    return {
      ok: total <= availablePoints,
      blocking: total > availablePoints,
      side,
      quantity,
      price,
      notional,
      fee,
      total,
      availablePoints,
      message: total > availablePoints
        ? `買入預估 ${formatTradingPointsValue(total)} 點${quantityNote}（含手續費 ${formatTradingPointsValue(fee)}），超過可用 ${formatTradingPointsValue(availablePoints)} 點 · ${orderPriceNote}`
        : `買入預估 ${formatTradingPointsValue(total)} 點${quantityNote}（成交 ${formatTradingPointsValue(notional)} + 手續費 ${formatTradingPointsValue(fee)}） · ${orderPriceNote}`,
    };
  }
  const net = Math.max(0, notional - fee);
  return {
    ok: quantity <= sellableQuantity,
    blocking: quantity > sellableQuantity,
    side,
    quantity,
    price,
    notional,
    fee,
    total: net,
    sellableQuantity,
    message: quantity > sellableQuantity
      ? `賣出 ${formatTradingQuantityValue(quantity)} 超過可賣現貨 ${formatTradingQuantityValue(sellableQuantity)} · ${orderPriceNote}`
      : `賣出預估收入 ${formatTradingPointsValue(net)} 點${quantityNote}（成交 ${formatTradingPointsValue(notional)} - 手續費 ${formatTradingPointsValue(fee)}） · ${orderPriceNote}`,
  };
}

function updateTradingOrderEstimate() {
  const estimate = tradingOrderDraftEstimate();
  const target = $("trading-order-estimate");
  const submitBtn = $("trading-submit-order-btn");
  updateTradingSellableHint();
  if (target) {
    target.textContent = estimate.message || "";
    target.style.color = estimate.blocking ? "#ff6b7a" : "var(--muted)";
  }
  if (submitBtn) {
    submitBtn.setAttribute("aria-disabled", estimate.blocking ? "true" : "false");
    submitBtn.classList.toggle("is-soft-disabled", !!estimate.blocking);
  }
  return estimate;
}

function syncTradingOrderSideTheme() {
  const side = $("trading-side")?.value === "sell" ? "sell" : "buy";
  const form = $("trading-order-form");
  const submitBtn = $("trading-submit-order-btn");
  if (form) {
    form.classList.toggle("trading-order-buy", side === "buy");
    form.classList.toggle("trading-order-sell", side === "sell");
  }
  if (submitBtn) {
    submitBtn.classList.toggle("trading-submit-buy", side === "buy");
    submitBtn.classList.toggle("trading-submit-sell", side === "sell");
    submitBtn.textContent = side === "buy" ? "買入下單" : "賣出下單";
  }
}

function tradingPositionLabel(row) {
  return `${tradingDisplaySymbol(row.market_symbol)} ${formatTradingPointsValue(row.quantity || 0)}`;
}

function economySpotMarkets(markets = []) {
  return (markets || []).filter((market) => {
    const quote = String(market?.quote_currency || "USDT").toUpperCase();
    return quote === "USDT" || quote === "POINTS";
  });
}

function spotPositionNumber(position, key) {
  return tradingNumber(position?.[key], 0);
}

function spotPositionTotalQuantity(position) {
  return spotPositionNumber(position, "quantity") + spotPositionNumber(position, "locked_quantity");
}

function tradingSpotBackendPnl(position) {
  return tradingNumber(position?.risk_grade_unrealized_pnl_points ?? position?.unrealized_pnl_points, 0);
}

function tradingSpotHasBackendPnl(position) {
  return !!position && (position.risk_grade_unrealized_pnl_points !== undefined || position.unrealized_pnl_points !== undefined);
}

function tradingSpotPnl(position, market) {
  if (tradingSpotHasBackendPnl(position) && !market) return tradingSpotBackendPnl(position);
  const quantity = spotPositionTotalQuantity(position);
  const costBasis = tradingSpotCostBasis(position, market, "risk_grade");
  const currentValue = tradingSpotRiskGradeValue(position, market);
  if (!quantity || !costBasis || !currentValue) {
    return tradingSpotHasBackendPnl(position) ? tradingSpotBackendPnl(position) : 0;
  }
  return currentValue - costBasis;
}

function tradingSpotUnrealizedPnl(position, market) {
  return tradingSpotPnl(position, market);
}

function tradingSpotFee(value, market, multiplier = 1) {
  const feeRate = tradingNumber(market?.fee_rate_percent, 0) * Number(multiplier || 1) / 100;
  return Math.max(0, Number(value || 0) * feeRate);
}

function tradingSpotHoldingCost(position, market) {
  const quantity = spotPositionTotalQuantity(position);
  const avgCost = spotPositionNumber(position, "avg_cost_points");
  if (!quantity || !avgCost) return 0;
  const buyNotional = quantity * avgCost;
  const buyFeeEstimate = tradingSpotFee(buyNotional, market);
  return buyNotional + buyFeeEstimate;
}

function tradingSpotHoldingCostPerUnit(position, market) {
  const quantity = spotPositionTotalQuantity(position);
  const holdingCost = tradingSpotHoldingCost(position, market);
  if (!quantity || !holdingCost) return 0;
  return holdingCost / quantity;
}

function tradingSpotBreakEvenExitPrice(position, market) {
  const quantity = spotPositionTotalQuantity(position);
  const holdingCost = tradingSpotHoldingCost(position, market);
  const feeRate = tradingNumber(market?.fee_rate_percent, 0) / 100;
  if (!quantity || !holdingCost || feeRate >= 1) return 0;
  return holdingCost / (quantity * (1 - feeRate));
}

function tradingSpotCurrentValue(position, market) {
  if (position && position.reference_current_value_points !== undefined && !market) {
    return tradingNumber(position.reference_current_value_points, tradingNumber(position.current_value_points, 0));
  }
  const quantity = spotPositionTotalQuantity(position);
  const currentPrice = tradingMarketPricePoints(market, "reference");
  if (quantity > 0 && currentPrice > 0) return quantity * currentPrice;
  return tradingNumber(position?.reference_current_value_points ?? position?.current_value_points, 0);
}

function tradingSpotCostBasis(position, market, priceType = "reference") {
  if (position && position.cost_basis_points !== undefined && !market) {
    return tradingNumber(position.cost_basis_points, 0);
  }
  const holdingCost = tradingSpotHoldingCost(position, market);
  const currentValue = priceType === "risk_grade"
    ? tradingSpotRiskGradeValue(position, market)
    : tradingSpotCurrentValue(position, market);
  const fallbackCostBasis = priceType === "risk_grade"
    ? position?.cost_basis_points
    : (position?.reference_cost_basis_points ?? position?.cost_basis_points);
  if (!holdingCost || !currentValue) return tradingNumber(fallbackCostBasis, 0);
  const sellFeeEstimate = tradingSpotFee(currentValue, market);
  return holdingCost + sellFeeEstimate;
}

function tradingSpotRiskGradeValue(position, market) {
  if (position && position.risk_grade_current_value_points !== undefined && !market) {
    return tradingNumber(position.risk_grade_current_value_points, 0);
  }
  const quantity = spotPositionTotalQuantity(position);
  const currentPrice = tradingMarketPricePoints(market, "risk_grade");
  if (quantity > 0 && currentPrice > 0) return quantity * currentPrice;
  return tradingNumber(position?.risk_grade_current_value_points ?? position?.current_value_points, 0);
}

const TRADING_POINT_MICRO_SCALE = 1000000;

function tradingMarginBillableInterestPoints(totalMicropoints) {
  const micro = Math.max(0, Math.round(tradingNumber(totalMicropoints, 0)));
  if (micro <= 0) return 0;
  return tradingMicropointsToSettlementPoints(micro);
}

function tradingMarginPositionIsShort(row) {
  const type = String(row?.position_type || "").toLowerCase();
  return type === "short" || type === "margin_short";
}

function tradingMarginInterestTiming(row) {
  return {
    intervalHours: Math.max(1, tradingNumber(row?.interest_interval_hours, 1)),
    minimumHours: Math.max(1, tradingNumber(row?.interest_minimum_hours, 1)),
  };
}

function tradingMarginOpenedAtMs(row) {
  const opened = row?.opened_at ? new Date(row.opened_at).getTime() : 0;
  return Number.isFinite(opened) ? opened : 0;
}

function tradingMarginBillableInterestHours(row, nowMs = Date.now()) {
  const principal = tradingNumber(row?.principal_points, 0);
  const ratePercentDaily = tradingNumber(row?.interest_percent_daily, 0);
  const openedAtMs = tradingMarginOpenedAtMs(row);
  if (!principal || ratePercentDaily <= 0 || !openedAtMs || nowMs <= openedAtMs) return 0;
  const { intervalHours, minimumHours } = tradingMarginInterestTiming(row);
  const elapsedSeconds = Math.max(0, (nowMs - openedAtMs) / 1000);
  if (!elapsedSeconds) return 0;
  const billedHours = Math.ceil(elapsedSeconds / (intervalHours * 3600)) * intervalHours;
  return Math.max(minimumHours, billedHours);
}

function tradingMarginLiveInterest(row, nowMs = Date.now()) {
  const principal = tradingNumber(row?.principal_points, 0);
  const ratePercentDaily = tradingNumber(row?.interest_percent_daily, 0);
  const capitalized = tradingNumber(row?.interest_capitalized_points ?? row?.interest_points, 0);
  const carryMicropoints = tradingNumber(row?.interest_carry_micropoints, 0);
  const accruedHours = tradingNumber(row?.interest_accrued_hours, 0);
  const totalHours = tradingMarginBillableInterestHours(row, nowMs);
  const dueHours = Math.max(0, totalHours - accruedHours);
  if (!principal || ratePercentDaily <= 0 || dueHours <= 0) {
    const exact = capitalized + (carryMicropoints / TRADING_POINT_MICRO_SCALE);
    return { points: capitalized + tradingMarginBillableInterestPoints(carryMicropoints), exactPoints: exact, totalHours, dueHours, totalMicropoints: carryMicropoints };
  }
  const hourlyRate = (ratePercentDaily / 100) / 24;
  const dueMicropoints = Math.round(principal * hourlyRate * dueHours * TRADING_POINT_MICRO_SCALE);
  const totalMicropoints = carryMicropoints + dueMicropoints;
  const duePoints = tradingMarginBillableInterestPoints(totalMicropoints);
  const points = capitalized + duePoints;
  const exactPoints = capitalized + (totalMicropoints / TRADING_POINT_MICRO_SCALE);
  return { points, exactPoints, totalHours, dueHours, totalMicropoints };
}

function tradingMarginNextInterestAtMs(row, nowMs = Date.now()) {
  const openedAtMs = tradingMarginOpenedAtMs(row);
  if (!openedAtMs) return 0;
  const { intervalHours } = tradingMarginInterestTiming(row);
  const accruedHours = tradingNumber(row?.interest_accrued_hours, 0);
  let nextBillingHours = tradingMarginBillableInterestHours(row, nowMs);
  if (nextBillingHours && nextBillingHours <= accruedHours) {
    nextBillingHours = accruedHours + intervalHours;
  }
  return nextBillingHours > 0 ? openedAtMs + (nextBillingHours * 3600 * 1000) : 0;
}

function tradingMarginBreakEvenPrice(row, interestExactPoints, market = null) {
  const resolvedMarket = market || tradingMarketBySymbol(row?.market_symbol || "");
  const quantity = tradingNumber(row?.quantity, 0);
  const principal = tradingNumber(row?.principal_points, 0);
  const collateral = tradingNumber(row?.collateral_points, 0);
  const openFee = tradingNumber(row?.open_fee_points, 0);
  const feeRate = tradingNumber(resolvedMarket?.fee_rate_percent, 0) / 100;
  if (!resolvedMarket || !quantity || feeRate < 0 || feeRate >= 1) return 0;
  if (tradingMarginPositionIsShort(row)) {
    const recoverableValue = principal - openFee - interestExactPoints;
    if (recoverableValue <= 0) return 0;
    return recoverableValue / (quantity * (1 + feeRate));
  }
  const requiredExitValue = collateral + principal + openFee + interestExactPoints;
  return requiredExitValue > 0 ? requiredExitValue / (quantity * (1 - feeRate)) : 0;
}

function tradingLiveMarginRisk(row, market = null) {
  const fallback = row?.risk && typeof row.risk === "object" ? row.risk : {};
  const resolvedMarket = market || tradingMarketBySymbol(row?.market_symbol || "");
  if (!row || !resolvedMarket) return fallback;
  const quantity = tradingNumber(row.quantity, 0);
  const riskContext = tradingMarketPriceContext(resolvedMarket, "risk_grade");
  const currentPrice = tradingMarketPricePoints(resolvedMarket, "risk_grade") || tradingNumber(fallback.price_points, 0);
  const principal = tradingNumber(row.principal_points, 0);
  const collateral = tradingNumber(row.collateral_points, 0);
  const dynamicInterest = tradingMarginLiveInterest(row);
  const interest = tradingNumber(dynamicInterest.points, tradingNumber(fallback.interest_points, 0));
  const interestExact = tradingNumber(dynamicInterest.exactPoints, tradingNumber(fallback.interest_exact_points ?? fallback.interest_points, 0));
  const feeRatePercent = tradingNumber(resolvedMarket.fee_rate_percent, 0);
  const maintenancePercent = tradingNumber(tradingState.settings?.margin_maintenance_percent, tradingNumber(fallback.maintenance_percent, 0));
  const exitNotional = quantity > 0 && currentPrice > 0 ? Math.ceil(quantity * currentPrice) : tradingNumber(fallback.exit_notional_points, 0);
  const openFeeMicropoints = tradingNumber(row.open_fee_micropoints, tradingNumber(row.open_fee_points, 0) * TRADING_POINT_MICRO_SCALE);
  const closeFeeMicropoints = tradingFeeMicropoints(exitNotional, feeRatePercent);
  const closeFee = tradingMicropointsToSettlementPoints(openFeeMicropoints + closeFeeMicropoints);
  const isShort = tradingMarginPositionIsShort(row);
  const equityAfter = isShort
    ? (collateral + principal - exitNotional - interest - closeFee)
    : (exitNotional - principal - interest - closeFee);
  const delta = isShort
    ? (principal - exitNotional - interest - closeFee)
    : (equityAfter - collateral);
  const maintenancePoints = Math.max(0, Math.ceil(exitNotional * maintenancePercent / 100));
  const breakEvenPrice = tradingMarginBreakEvenPrice(row, interestExact, resolvedMarket);
  let liquidationPrice = 0;
  const quantityUnits = quantity * 100000000;
  if (quantityUnits > 0) {
    if (isShort) {
      const denominatorPercent = 100 + feeRatePercent + maintenancePercent;
      const liquidationBase = collateral + principal - interest;
      if (denominatorPercent > 0 && liquidationBase > 0) {
        liquidationPrice = (Math.ceil((liquidationBase * 100) / denominatorPercent) * 100000000) / quantityUnits;
      }
    } else {
      const denominatorPercent = 100 - feeRatePercent - maintenancePercent;
      if (denominatorPercent > 0) {
        liquidationPrice = (Math.ceil(((principal + interest) * 100) / denominatorPercent) * 100000000) / quantityUnits;
      }
    }
  }
  const maintenanceRatioPercent = maintenancePoints > 0
    ? Math.round((equityAfter * 10000) / maintenancePoints) / 100
    : tradingNumber(fallback.maintenance_ratio_percent, 0);
  let riskStatus = "normal";
  let riskReason = isShort
    ? "借券放空在價格上漲時會虧損，價格越高維持率越低"
    : "融資做多在價格下跌時會虧損，價格越低維持率越低";
  if (equityAfter <= maintenancePoints) {
    riskStatus = "liquidation";
    riskReason = "權益已低於維持保證金，會被列入強制平倉";
  } else if (maintenanceRatioPercent < 150) {
    riskStatus = "warning";
    riskReason = "整體維持率偏低，建議補保證金或降低倉位";
  } else if (isShort) {
    riskStatus = "short_price_risk";
  }
  const withdrawEstimate = tradingMarginWithdrawEstimate(row, {
    collateral_points: collateral,
    initial_margin_points: collateral,
    unrealized_pnl_points: delta,
    delta_points: delta,
    equity_after_points: equityAfter,
    maintenance_points: maintenancePoints,
    maintenance_margin_points: maintenancePoints,
  });
  return {
    ...fallback,
    price_points: currentPrice,
    price_context: riskContext,
    close_fee_points: closeFee,
    exit_notional_points: exitNotional,
    interest_points: interest,
    interest_exact_points: interestExact,
    interest_total_hours: dynamicInterest.totalHours,
    equity_after_points: equityAfter,
    unrealized_pnl_points: delta,
    delta_points: delta,
    breakeven_price_points: breakEvenPrice,
    maintenance_percent: maintenancePercent,
    maintenance_points: maintenancePoints,
    maintenance_margin_percent: maintenancePercent,
    maintenance_margin_points: maintenancePoints,
    liquidation_price_points: liquidationPrice || tradingNumber(fallback.liquidation_price_points, 0),
    maintenance_ratio_percent: maintenanceRatioPercent,
    risk_status: riskStatus,
    risk_reason: riskReason,
    liquidation_required: equityAfter <= maintenancePoints,
    max_withdrawable_collateral_points: withdrawEstimate.maxWithdrawable,
    withdrawable_collateral_points: withdrawEstimate.maxWithdrawable,
    withdrawable_collateral_reason: withdrawEstimate.reason,
    withdrawable_collateral_after_ratio_percent: withdrawEstimate.afterRatio,
  };
}

function tradingMarginWithdrawEstimate(row, liveRisk = null) {
  const risk = liveRisk || tradingLiveMarginRisk(row);
  const backendMax = Number(risk?.max_withdrawable_collateral_points);
  const collateral = Math.floor(tradingNumber(
    risk?.collateral_points ?? risk?.initial_margin_points ?? row?.collateral_points ?? row?.initial_margin_points,
    0
  ));
  const profitableSurplus = Math.max(0, Math.floor(tradingNumber(
    risk?.unrealized_pnl_points ?? risk?.delta_points ?? row?.unrealized_pnl_points,
    0
  )));
  const equity = Math.floor(tradingNumber(risk?.equity_after_points ?? row?.equity_after_points, 0));
  const maintenance = Math.max(0, Math.ceil(tradingNumber(
    risk?.maintenance_margin_points ?? risk?.maintenance_points ?? row?.maintenance_margin_points ?? row?.maintenance_points,
    0
  )));
  const collateralCap = Math.max(0, collateral - 1);
  const maintenanceCap = Math.max(0, equity - maintenance - 1);
  const fallbackMax = Math.max(0, Math.min(collateralCap, profitableSurplus, maintenanceCap));
  const maxWithdrawable = Number.isFinite(backendMax)
    ? Math.max(0, Math.floor(backendMax))
    : fallbackMax;
  const afterEquity = equity - maxWithdrawable;
  const afterRatio = maintenance > 0 && maxWithdrawable > 0
    ? Math.round((afterEquity * 10000) / maintenance) / 100
    : null;
  let reason = "後端仍會用最新風控價、利息與維持率重算";
  if (collateralCap <= 0) reason = "保證金已接近最低保留額，暫不可抽出";
  else if (profitableSurplus <= 0) reason = "目前沒有可抽出的未實現盈利";
  else if (maintenanceCap <= 0) reason = "抽出後會接近或低於維持保證金，暫不可抽出";
  return {
    maxWithdrawable,
    afterRatio,
    reason,
    collateralCap,
    profitableSurplus,
    maintenanceCap,
  };
}

function tradingMarginPositionByUuid(positionUuid) {
  const target = String(positionUuid || "");
  return (tradingState.marginPositions || []).find((row) => String(row.position_uuid || "") === target) || null;
}

function tradingLiveMarginFreeMarginPoints() {
  const fundingAvailable = Number(tradingState.funding?.available_points);
  if (Number.isFinite(fundingAvailable)) return Math.max(0, fundingAvailable);
  const summaryFreeMargin = Number(tradingState.marginSummary?.free_margin_points);
  if (Number.isFinite(summaryFreeMargin)) return Math.max(0, summaryFreeMargin);
  const walletAvailable = Number(tradingState.funding?.wallet_available_points);
  const trialAvailable = Number(tradingState.funding?.trial_credit?.available_points);
  const combined = (Number.isFinite(walletAvailable) ? walletAvailable : 0)
    + (Number.isFinite(trialAvailable) ? trialAvailable : 0);
  return Math.max(0, combined);
}

function tradingLiveMarginSummary(rows = []) {
  const openRows = rows.filter((row) => row.status === "open");
  if (!openRows.length) return { open_count: 0 };
  let totalPositionEquity = 0;
  let totalBorrowed = 0;
  let totalMaintenance = 0;
  openRows.forEach((row) => {
    const risk = tradingLiveMarginRisk(row);
    totalPositionEquity += tradingNumber(risk.equity_after_points, 0);
    totalBorrowed += tradingNumber(row.principal_points, 0);
    totalMaintenance += tradingNumber(risk.maintenance_margin_points ?? risk.maintenance_points, 0);
  });
  const freeMargin = tradingLiveMarginFreeMarginPoints();
  const accountEquity = totalPositionEquity + freeMargin;
  const availableMargin = accountEquity - totalMaintenance;
  const ratio = totalMaintenance > 0 ? Math.round((accountEquity * 10000) / totalMaintenance) / 100 : null;
  let reason = "整戶維持率正常";
  if (ratio !== null && ratio <= 100) reason = "整戶維持率已低於強平門檻";
  else if (ratio !== null && ratio < 150) reason = "整戶維持率偏低";
  return {
    open_count: openRows.length,
    cross_margin_ratio_percent: ratio,
    maintenance_ratio_percent: ratio,
    account_equity_points: accountEquity,
    total_position_equity_points: totalPositionEquity,
    free_margin_points: freeMargin,
    available_margin_points: availableMargin,
    total_borrowed_points: totalBorrowed,
    total_maintenance_requirement_points: totalMaintenance,
    total_maintenance_points: totalMaintenance,
    reason,
  };
}

function tradingDisplayedMarginSummary(summary = null, rows = null) {
  const marginRows = Array.isArray(rows) ? rows : (tradingState.marginPositions || []);
  const hasOpenMargin = marginRows.some((row) => row.status === "open");
  if (!hasOpenMargin) {
    return summary && typeof summary === "object" && Object.keys(summary).length ? summary : { open_count: 0 };
  }
  return tradingLiveMarginSummary(marginRows);
}

function refreshTradingWalletLiveMetrics() {
  const liveMarginSummary = tradingLiveMarginSummary(tradingState.marginPositions || []);
  renderTradingMarginPositions(tradingState.marginPositions || []);
  renderTradingMarginAccountSummary(liveMarginSummary);
}

function tradingMarginRiskText(row) {
  const risk = row?.risk || {};
  const ratio = risk.maintenance_ratio_percent ?? row.maintenance_ratio_percent;
  const status = risk.risk_status || row.risk_status || "normal";
  const reason = risk.risk_reason || row.risk_reason || "";
  const ratioText = ratio === null || ratio === undefined ? "無法計算" : `${formatTradingPointsValue(ratio)}%`;
  const statusLabel = status === "liquidation" ? "清算風險"
    : (status === "warning" ? "維持率偏低"
      : (status === "short_price_risk" ? "放空價格風險" : "正常"));
  return { ratioText, statusLabel, reason };
}

function tradingMarginPositionRow(row, scope = "trading") {
  const liveRisk = tradingLiveMarginRisk(row);
  const isShort = tradingMarginPositionIsShort(row);
  const typeLabel = row.position_label || (isShort ? "借券放空" : "融資買入");
  const principal = tradingNumber(row.principal_points, 0);
  const collateral = tradingNumber(liveRisk.initial_margin_points ?? row.initial_margin_points ?? row.collateral_points, 0);
  const fee = tradingNumber(row.open_fee_points, 0);
  const interest = tradingNumber(liveRisk.interest_exact_points ?? row.interest_exact_points ?? row.interest_points, 0);
  const paidInterest = tradingNumber(row.interest_paid_points, 0);
  const interestHours = tradingNumber(liveRisk.interest_total_hours ?? row.interest_accrued_hours, 0);
  const totalElapsedHours = tradingMarginOpenedAtMs(row) ? Math.max(0, Math.floor((Date.now() - tradingMarginOpenedAtMs(row)) / 3600000)) : tradingNumber(row.total_elapsed_hours, 0);
  const interestAprPercent = tradingNumber(row.interest_apr_percent ?? ((tradingNumber(row.interest_percent_daily, 0) || 0) * 365), 0);
  const interestIntervalHours = tradingNumber(row.interest_interval_hours, 1);
  const minimumHours = tradingNumber(row.interest_minimum_hours, 1);
  const nextInterestAt = tradingMarginNextInterestAtMs(row);
  const nextInterestCountdown = (nextInterestAt && nextInterestAt > Date.now())
    ? `下次計息 ${formatTradingDuration(nextInterestAt - Date.now())} 後`
    : (totalElapsedHours > 0 ? "下次計息即將觸發" : "");
  const nextInterestLabel = nextInterestAt
    ? new Date(nextInterestAt).toLocaleString()
    : "尚未開始計息";
  const entry = tradingNumber(row.entry_price_points, 0);
  const currentPrice = tradingNumber(liveRisk.price_points ?? row.current_price_points, 0);
  const riskContext = liveRisk.price_context || tradingMarketPriceContext(tradingMarketBySymbol(row.market_symbol || ""), "risk_grade");
  const equity = tradingNumber(liveRisk.equity_after_points ?? row.equity_after_points, 0);
  const maintenance = tradingNumber(liveRisk.maintenance_margin_points ?? liveRisk.maintenance_points ?? row.maintenance_margin_points ?? row.maintenance_points, 0);
  const initialMarginRatePercent = tradingNumber(liveRisk.initial_margin_percent ?? row.initial_margin_percent, 0);
  const maintenanceRatePercent = tradingNumber(liveRisk.maintenance_margin_percent ?? liveRisk.maintenance_percent ?? row.maintenance_margin_percent, 0);
  const unrealizedPnl = tradingNumber(liveRisk.unrealized_pnl_points ?? row.unrealized_pnl_points, 0);
  const breakEvenPrice = tradingNumber(liveRisk.breakeven_price_points ?? row.breakeven_price_points, 0);
  const liquidationPrice = tradingNumber(liveRisk.liquidation_price_points ?? row.liquidation_price_points, 0);
  const pnlClass = unrealizedPnl > 0 ? "positive" : (unrealizedPnl < 0 ? "negative" : "");
  const leverageHint = collateral > 0 ? `${(principal / collateral).toFixed(2)}x 風險倍數` : "未提供風險倍數";
  const riskText = tradingMarginRiskText({ ...row, risk: liveRisk });
  const riskTargetText = tradingRiskTargetText(row.stop_loss_percent, row.take_profit_percent);
  const withdrawEstimate = tradingMarginWithdrawEstimate(row, liveRisk);
  const withdrawHint = withdrawEstimate.maxWithdrawable > 0
    ? `預估可抽出 ${formatTradingPointsValue(withdrawEstimate.maxWithdrawable)} 點；若全數抽出，維持率約 ${withdrawEstimate.afterRatio == null ? "無法估算" : `${formatTradingPointsValue(withdrawEstimate.afterRatio)}%`}`
    : `預估可抽出 0 點；${withdrawEstimate.reason}`;
  const withdrawDisabledAttr = withdrawEstimate.maxWithdrawable > 0 ? "" : " disabled";
  const prefix = scope === "economy" ? "economy-" : "";
  return `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(typeLabel)} · ${sanitize(tradingDisplaySymbol(row.market_symbol || "-"))} · ${sanitize(row.quantity || "0")}</strong>
        <div class="drive-card-sub">
          入場 ${formatTradingPointsValue(entry)} · 現價 ${currentPrice ? formatTradingPointsValue(currentPrice) : "-"} · 本金 ${formatTradingPointsValue(principal)} · 原始保證金 ${formatTradingPointsValue(collateral)}
        </div>
        <div class="drive-card-sub">風控級價格用途：融資 / 強平 / 保證金 / PnL · ${sanitize(tradingPriceContextSummary(riskContext, { compact: true }))}</div>
        <div class="drive-card-sub">
          原始保證金率 ${formatTradingPointsValue(initialMarginRatePercent)}% · 維持率 ${sanitize(riskText.ratioText)} · 權益 ${formatTradingPointsValue(equity)} · 維持保證金 ${formatTradingPointsValue(maintenance)}（${formatTradingPointsValue(maintenanceRatePercent)}%） · ${sanitize(riskText.statusLabel)}
        </div>
        <div class="drive-card-sub">
          未實現盈虧 <b class="trading-spot-pnl ${pnlClass}">${unrealizedPnl >= 0 ? "+" : ""}${formatTradingPointsValue(unrealizedPnl)} 點</b> · 損益平衡價 ${breakEvenPrice ? formatTradingPointsValue(breakEvenPrice) : "無法估算"} · 逐倉估算強平價 ${liquidationPrice ? formatTradingPointsValue(liquidationPrice) : "無法估算"}
        </div>
        <div class="drive-card-sub">損益平衡價已含開倉費、累積利息與預估平倉手續費；實際清算仍依全倉維持率</div>
        <div class="drive-card-sub">${sanitize(riskTargetText)}</div>
        <div class="drive-card-sub">${sanitize(riskText.reason || "")}</div>
        <div class="drive-card-sub">開倉費 ${formatTradingPointsValue(fee)} · 年利率 ${formatTradingPercent(interestAprPercent)}% APR · 累積利息 ${formatTradingPointsValue(interest)} 點 · 已實扣 ${formatTradingPointsValue(paidInterest)} 點 · 已持倉 ${totalElapsedHours} 小時 · 已計息 ${interestHours} 小時</div>
        <div class="drive-card-sub">下一次計息 ${sanitize(nextInterestLabel)}${nextInterestCountdown ? ` · ${sanitize(nextInterestCountdown)}` : ""} · 規則：每 ${formatTradingPointsValue(interestIntervalHours)} 小時、至少 ${formatTradingPointsValue(minimumHours)} 小時 · ${sanitize(leverageHint)}</div>
        <div class="economy-ledger-hash">${sanitize(row.position_uuid || "")}</div>
      </div>
      <div class="trading-spot-actions">
        <div class="field">
          <label>補保證金</label>
          <input type="number" min="1" step="1" placeholder="點數" data-${prefix}margin-collateral-amount="${sanitize(row.position_uuid || "")}" />
        </div>
        <button class="btn" type="button" data-${prefix}margin-add-collateral="${sanitize(row.position_uuid || "")}">補保證金</button>
        <div class="field">
          <label>抽出保證金</label>
          <input type="number" min="1" step="1" max="${sanitize(withdrawEstimate.maxWithdrawable)}" placeholder="${sanitize(withdrawEstimate.maxWithdrawable > 0 ? `${formatTradingPointsValue(withdrawEstimate.maxWithdrawable)} 點內` : "暫不可抽出")}" data-${prefix}margin-withdraw-collateral-amount="${sanitize(row.position_uuid || "")}" />
          <div class="field-hint">${sanitize(withdrawHint)}。此為提示，實際送出仍以後端最新風控價格為準。</div>
        </div>
        <button class="btn" type="button" data-${prefix}margin-withdraw-collateral="${sanitize(row.position_uuid || "")}"${withdrawDisabledAttr}>抽出保證金</button>
        <button class="btn btn-danger" type="button" data-${prefix}margin-close="${sanitize(row.position_uuid || "")}">平倉</button>
      </div>
    </div>
  `;
}

function renderTradingMarketOptions() {
  const select = $("trading-market-select");
  const rootSelect = $("trading-root-market-select");
  const contractSelect = $("trading-contract-market-select");
  const marginSelect = $("trading-margin-market-select");
  const botSelects = [$("trading-auto-bot-market"), $("trading-dca-bot-market"), $("trading-grid-bot-market")];
  const botBacktestSelects = [$("trading-dca-backtest-market"), $("trading-grid-backtest-market"), $("trading-workflow-backtest-market")];
  const options = tradingState.markets.length
    ? tradingState.markets.map((market) => `<option value="${sanitize(market.symbol)}">${sanitize(tradingDisplaySymbol(market.symbol))}</option>`).join("")
    : `<option value="">沒有可用市場</option>`;
  const botMarkets = (tradingState.markets || []).filter((market) => market.allow_bots !== false);
  const botOptions = botMarkets.length
    ? botMarkets.map((market) => `<option value="${sanitize(market.symbol)}">${sanitize(tradingDisplaySymbol(market.symbol))}</option>`).join("")
    : `<option value="">目前沒有開放機器人的市場</option>`;
  [select, rootSelect, contractSelect, marginSelect, ...botBacktestSelects].forEach((target) => {
    if (!target) return;
    const previous = target.value;
    target.innerHTML = options;
    if (previous && Array.from(target.options).some((option) => option.value === previous)) target.value = previous;
  });
  botSelects.forEach((target) => {
    if (!target) return;
    const previous = target.value;
    target.innerHTML = botOptions;
    if (previous && Array.from(target.options).some((option) => option.value === previous)) target.value = previous;
  });
}

function tradingReadinessClass(state) {
  if (state === "bad") return "trading-readiness-bad";
  if (state === "warn") return "trading-readiness-warn";
  return "trading-readiness-ok";
}

function tradingWorstReadinessState(states = []) {
  if (states.includes("bad")) return "bad";
  if (states.includes("warn")) return "warn";
  if (states.includes("ok")) return "ok";
  return "unknown";
}

function tradingSignalStateLabel(state) {
  if (state === "bad") return "阻擋";
  if (state === "warn") return "觀察";
  if (state === "ok") return "正常";
  return "未讀取";
}

function updateTradingSignalLight(id, state, label, detail) {
  const el = $(id);
  if (!el) return;
  const normalized = state === "bad" || state === "warn" || state === "ok" ? state : "unknown";
  el.dataset.state = normalized;
  el.title = `${label}：${tradingSignalStateLabel(normalized)}${detail ? ` · ${detail}` : ""}`;
  el.setAttribute("aria-label", el.title);
}

function tradingMarketBootSummary() {
  const liveMarkets = (tradingState.markets || []).filter((market) => market.live_price_enabled !== false);
  const liveMeta = tradingState.livePriceMeta || {};
  let ready = 0;
  let warming = 0;
  let pending = 0;
  let degraded = 0;
  liveMarkets.forEach((market) => {
    const meta = liveMeta[market.symbol] || {};
    const health = String(meta.price_health || market.price_health || "").trim();
    if (health === "boot_pending") {
      pending += 1;
      return;
    }
    if (health === "fallback" || health === "degraded" || health === "conservative" || meta.high_risk_blocked || meta.risk_grade_price_context?.risk_grade_usable === false) {
      degraded += 1;
    }
    if (market.live_price_confirmed_at) ready += 1;
    else if (market.live_price_warmup_started_at) warming += 1;
    else pending += 1;
  });
  return {
    total: liveMarkets.length,
    ready,
    warming,
    pending,
    degraded,
    state: pending > 0 || degraded > 0 ? "warn" : "ok",
  };
}

function tradingSelectedPriceReadiness(market) {
  if (!market) return { state: "warn", value: "no market", detail: "尚未選擇市場" };
  const symbol = market.symbol;
  const meta = (tradingState.livePriceMeta || {})[symbol] || {};
  const referenceContext = tradingMarketPriceContext(market, "reference");
  const riskContext = tradingMarketPriceContext(market, "risk_grade");
  const health = String(meta.price_health || referenceContext?.health || "healthy").trim();
  const providerCount = Number(meta.provider_count ?? riskContext?.provider_count ?? 0);
  const minimum = Number(meta.minimum_provider_count ?? riskContext?.minimum_provider_count ?? 0);
  const reason = meta.fallback_reason || meta.high_risk_block_reason || riskContext?.warning_message || referenceContext?.warning_message || "";
  let state = "ok";
  if (health === "boot_pending" || health === "fallback" || health === "degraded" || health === "conservative" || riskContext?.risk_grade_usable === false) {
    state = health === "boot_pending" ? "warn" : "bad";
  }
  return {
    state,
    value: health === "boot_pending" ? "boot pending" : (riskContext?.risk_grade_usable === false ? "risk paused" : health),
    detail: `${tradingDisplaySymbol(symbol)} · provider ${providerCount || 0}/${minimum || 0}${reason ? ` · ${reason}` : ""}`,
  };
}

function renderTradingRiskDashboard() {
  const grid = $("trading-risk-dashboard-grid");
  if (!grid) return;
  const market = selectedTradingMarket();
  const boot = tradingMarketBootSummary();
  const selectedPrice = tradingSelectedPriceReadiness(market);
  const funding = tradingState.funding || {};
  const trial = funding.trial_credit || null;
  const fundingPool = tradingState.fundingPool || {};
  const settings = tradingState.settings || {};
  const bots = tradingState.bots || [];
  const enabledBots = bots.filter((bot) => bot.enabled);
  const runnableBots = enabledBots.filter((bot) => bot.can_run !== false);
  const botMarkets = (tradingState.markets || []).filter((row) => row.allow_bots !== false);
  const marginSummary = tradingDisplayedMarginSummary(tradingState.marginSummary);
  const openMargins = (tradingState.marginPositions || []).filter((row) => row.status === "open");
  const reserve = tradingState.rootReport?.reserve_pool || null;
  const reserveBalance = reserve ? Number(reserve.balance_points || 0) : Number(fundingPool.available_points || 0);
  const botState = boot.pending > 0 || boot.degraded > 0 ? "warn" : (enabledBots.length && runnableBots.length === 0 ? "warn" : "ok");
  const trialState = trial?.pending_reclaim ? "warn" : (trial && trial.status !== "active" ? "warn" : "ok");
  const marginRatio = marginSummary.cross_margin_ratio_percent ?? marginSummary.maintenance_ratio_percent;
  const marginState = !settings.borrowing_enabled ? "warn" : (marginRatio != null && Number(marginRatio) < Number(settings.margin_maintenance_percent || 0) ? "bad" : "ok");
  const priceItem = {
    label: "價格健康",
    value: selectedPrice.value,
    detail: selectedPrice.detail,
    state: selectedPrice.state,
  };
  const liveGateItem = {
    label: "Live price gate",
    value: `${boot.ready}/${boot.total || 0} ready`,
    detail: `boot pending ${boot.pending} · warming ${boot.warming} · degraded ${boot.degraded}`,
    state: boot.state,
  };
  const botItem = {
    label: "Bot / backtest",
    value: `${runnableBots.length}/${enabledBots.length} runnable`,
    detail: `可用 bot 市場 ${botMarkets.length} · 回測上限 ${Number(settings.backtest_max_candles || 0).toLocaleString()} candles`,
    state: botState,
  };
  const reserveItem = {
    label: currentUser === "root" ? "交易所基金" : "借貸基金",
    value: `${formatTradingPointsValue(reserveBalance)} 點`,
    detail: reserve
      ? `CFD 保留 ${formatTradingPointsValue(fundingPool.cfd_profit_reserve_required_points || 0)} · 借貸可用 ${formatTradingPointsValue(fundingPool.available_points || 0)} · events ${(tradingState.rootReport?.reserve_events || []).length}`
      : `借出 ${formatTradingPointsValue(fundingPool.outstanding_principal_points || 0)} / 上限 ${formatTradingPointsValue(fundingPool.max_outstanding_principal_points || 0)} · 剩餘可借 ${formatTradingPointsValue(fundingPool.remaining_borrow_capacity_points || 0)} · CFD 保留 ${formatTradingPointsValue(fundingPool.cfd_profit_reserve_required_points || 0)} · 使用率 ${formatTradingPercent(fundingPool.utilization_percent || 0)}%`,
    state: reserveBalance > 0 ? "ok" : "warn",
  };
  const trialItem = {
    label: "Trial credit",
    value: trial ? String(trial.status || "unknown") : "root / none",
    detail: trial
      ? `可用 ${formatTradingPointsValue(trial.available_points || 0)} · 鎖定 ${formatTradingPointsValue(trial.locked_points || 0)}${trial.reclaim_blocked_reason ? ` · ${trial.reclaim_blocked_reason}` : ""}`
      : "root 不使用體驗金",
    state: trialState,
  };
  const marginItem = {
    label: "Margin / lending",
    value: settings.borrowing_enabled ? `${openMargins.length} open` : "disabled",
    detail: settings.borrowing_enabled
      ? `強平 ${settings.margin_liquidation_enabled ? "on" : "off"} · 維持率 ${marginRatio == null ? "-" : `${formatTradingPointsValue(marginRatio)}%`} · 借貸上限 ${formatTradingPercent(fundingPool.max_pool_utilization_percent || settings.margin_max_pool_utilization_percent || 0)}% · 基金負債上限 ${formatTradingPointsValue(settings.exchange_liability_limit_points || 0)}`
      : "root 尚未開啟借貸交易",
    state: marginState,
  };
  const items = [priceItem, liveGateItem, botItem, reserveItem, trialItem, marginItem];
  const priceState = tradingWorstReadinessState([priceItem.state, liveGateItem.state]);
  const riskState = tradingWorstReadinessState([reserveItem.state, trialItem.state, marginItem.state]);
  updateTradingSignalLight("trading-signal-light-price", priceState, "價格訊號", `${priceItem.value} · ${liveGateItem.value}`);
  updateTradingSignalLight("trading-signal-light-bot", botItem.state, "Bot 訊號", `${botItem.value} · ${botItem.detail}`);
  updateTradingSignalLight("trading-signal-light-risk", riskState, "風控訊號", `${reserveItem.value} · ${marginItem.value}`);
  grid.innerHTML = items.map((item) => `
    <div class="trading-readiness-item ${tradingReadinessClass(item.state)}">
      <span>${sanitize(item.label)}</span>
      <strong>${sanitize(item.value)}</strong>
      <small>${sanitize(item.detail)}</small>
    </div>
  `).join("");
  const badge = $("trading-risk-dashboard-badge");
  const sub = $("trading-risk-dashboard-sub");
  const badCount = items.filter((item) => item.state === "bad").length;
  const warnCount = items.filter((item) => item.state === "warn").length;
  const dashboard = $("trading-risk-dashboard");
  if (dashboard) dashboard.dataset.state = tradingWorstReadinessState(items.map((item) => item.state));
  if (badge) badge.textContent = badCount ? `${badCount} 阻擋` : (warnCount ? `${warnCount} 觀察` : "正常");
  if (sub) sub.textContent = `價格 ${tradingSignalStateLabel(priceState)} · Bot ${tradingSignalStateLabel(botItem.state)} · 風控 ${tradingSignalStateLabel(riskState)}；滑鼠移過查看細節`;
}

function tradingSetText(id, text) {
  const el = $(id);
  if (el) el.textContent = text == null || text === "" ? "-" : String(text);
}

function tradingRootSitewideAllowed() {
  return currentUser === "root" && (!siteConfig || siteConfig.feature_trading_enabled !== false);
}

function syncTradingRootSitewideVisibility() {
  const card = $("trading-root-sitewide-card");
  const active = normalizeTradingPage(tradingActivePage);
  if (card) card.style.display = tradingRootSitewideAllowed() && (active === "sitewide-positions" || active === "fund-ops") ? "" : "none";
}

function syncTradingRootSimulationVisibility() {
  const card = $("trading-root-sim-card");
  if (card) card.style.display = tradingRootSitewideAllowed() && normalizeTradingPage(tradingActivePage) === "root-sim" ? "" : "none";
}

function tradingPageElementGroups() {
  return {
    spot: [
      "trading-asset-overview-card",
      "trading-exchange-reference-card",
      "trading-risk-dashboard",
      "trading-market-summary-grid",
      "trading-order-form",
      "trading-btc-signal-card",
      "trading-bot-card",
      "trading-margin-card",
      "trading-orders-fills-grid",
    ],
    positions: [
      "trading-portfolio-card",
      "trading-spot-position-card",
      "trading-margin-card",
      "trading-bot-position-card",
      "trading-orders-fills-grid",
    ],
    sitewide: ["trading-root-sitewide-card"],
    rootSim: ["trading-root-sim-card"],
    settings: ["trading-settings-page"],
  };
}

function normalizeTradingPage(page) {
  if (page === "spot" || page === "exchange") return "spot";
  if (page === "bots" || page === "lending" || page === "margin") return "spot";
  if (page === "my-positions" || page === "portfolio" || page === "account-positions") return "my-positions";
  if (page === "sitewide-positions" || page === "positions") return "sitewide-positions";
  if (page === "fund-ops" || page === "sitewide-pools" || page === "pools") return "fund-ops";
  if (page === "root-sim" || page === "root-simulation") return "root-sim";
  if (page === "settings" || page === "root-settings" || page === "admin-settings") return "settings";
  return "spot";
}

function tradingRootOnlyPage(page) {
  return ["sitewide-positions", "fund-ops", "root-sim", "settings"].includes(normalizeTradingPage(page));
}

function syncTradingPageChrome(active) {
  const page = normalizeTradingPage(active);
  const title = $("trading-page-title");
  const note = $("trading-availability-note");
  const copy = {
    spot: {
      title: "交易所",
      note: "現貨下單、交易機器人與借貸功能都留在同一個交易所頁；root 的全站倉位、基金營運與模擬倉位另列管理頁。",
    },
    "my-positions": {
      title: "我的倉位",
      note: "交易所內帳、整戶維持率、現貨明細、借貸倉位、訂單與成交集中在這裡，不再塞回積分錢包。",
    },
    "sitewide-positions": {
      title: "全站倉位看板",
      note: "root 唯讀檢視全站現貨、借貸、掛單與機器人風險；不操作基金，不顯示 root 模擬倉位。",
    },
    "fund-ops": {
      title: "交易所基金營運管理",
      note: "root 查看交易所基金、借貸基金、準備金與最近基金事件；全站倉位與 root 模擬倉位另列頁面。",
    },
    "root-sim": {
      title: "root 模擬倉位",
      note: "root 模擬金與合約沙盒，不寫入 PointsChain，也不與官方基金或用戶倉位混算。",
    },
    settings: {
      title: "交易所管理",
      note: "交易所詳細設定、價格來源、背景引擎、機器人掃描與市場 registry 都集中在交易所底下。",
    },
  };
  if (title) title.textContent = copy[page]?.title || copy.spot.title;
  if (note) note.textContent = copy[page]?.note || copy.spot.note;
}

function tradingMarginCardShouldShow() {
  const borrowingEnabled = !!tradingState.settings?.borrowing_enabled;
  const openMarginCount = (tradingState.marginPositions || []).filter((row) => row.status === "open").length;
  return borrowingEnabled || openMarginCount > 0;
}

function syncTradingExchangeSurfaceVisibility(activePage = normalizeTradingPage(tradingActivePage)) {
  const active = normalizeTradingPage(activePage);
  const orderForm = $("trading-order-form");
  const contractCard = $("trading-root-contract-card");
  const marginCard = $("trading-margin-card");
  const marginOpenForm = $("trading-margin-open-form");
  const marginEstimate = $("trading-margin-estimate");
  const fundingPoolCard = $("trading-funding-pool-public");
  const marginSummary = $("trading-margin-account-summary");
  const marginPositionList = $("trading-margin-position-list");
  const engineStatus = $("trading-safe-mode");
  const backgroundStatus = $("trading-background-status");
  if (orderForm) orderForm.style.display = active === "spot" ? "" : "none";
  if (contractCard) contractCard.style.display = currentUser === "root" && active === "root-sim" ? "" : "none";
  if (marginCard) {
    marginCard.style.display = active === "my-positions" || (active === "spot" && tradingMarginCardShouldShow()) ? "" : "none";
    if (active === "my-positions") marginCard.open = true;
  }
  if (marginOpenForm) marginOpenForm.style.display = active === "spot" && tradingState.settings?.borrowing_enabled ? "" : "none";
  if (marginEstimate) marginEstimate.style.display = active === "spot" && tradingState.settings?.borrowing_enabled ? "" : "none";
  if (fundingPoolCard) fundingPoolCard.style.display = active === "spot" && tradingState.settings?.borrowing_enabled ? "" : "none";
  if (marginSummary && active !== "my-positions") marginSummary.style.display = "none";
  if (marginPositionList) marginPositionList.style.display = active === "my-positions" ? "" : "none";
  if (engineStatus) engineStatus.style.display = active === "spot" ? "" : "none";
  if (backgroundStatus) backgroundStatus.style.display = active === "spot" && backgroundStatus.textContent ? "" : "none";
}

function syncTradingSubpages() {
  const groups = tradingPageElementGroups();
  Object.values(groups).flat().forEach((id) => {
    const el = $(id);
    if (el) {
      el.hidden = true;
      el.style.display = "none";
    }
  });
  const active = normalizeTradingPage(tradingActivePage);
  const tradingCard = $("trading-card");
  if (tradingCard) tradingCard.style.display = active === "settings" ? "none" : "";
  const tradingSettingsSection = $("sec-settings-trading");
  if (tradingSettingsSection) tradingSettingsSection.classList.toggle("active", active === "settings");
  const settingsBtn = $("trading-root-settings-page-btn");
  if (settingsBtn) {
    settingsBtn.style.display = tradingRootSitewideAllowed() ? "" : "none";
    settingsBtn.classList.toggle("active", active === "settings");
  }
  let showIds = groups.spot;
  if (active === "my-positions") showIds = groups.positions;
  if (active === "sitewide-positions" || active === "fund-ops") showIds = groups.sitewide;
  else if (active === "root-sim") showIds = groups.rootSim;
  else if (active === "settings") showIds = groups.settings;
  showIds.forEach((id) => {
    const el = $(id);
    if (el) {
      el.hidden = false;
      el.style.display = "";
    }
  });
  syncTradingRootSitewideVisibility();
  syncTradingRootSimulationVisibility();
  syncTradingExchangeSurfaceVisibility(active);
  syncTradingPageChrome(active);
  if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
}

function setTradingActivePage(page, options = {}) {
  const next = normalizeTradingPage(page);
  if (tradingRootOnlyPage(next) && !tradingRootSitewideAllowed()) {
    tradingSetMsg("只有 root 可以查看全站倉位、基金營運與模擬倉位", false);
    tradingActivePage = "spot";
  } else {
    tradingActivePage = next;
  }
  syncTradingSubpages();
  if (options.openBot === true) {
    const card = $("trading-bot-card");
    if (card) card.open = true;
  }
  if (options.openLending === true) {
    const card = $("trading-margin-card");
    if (card) card.open = true;
  }
  if (tradingActivePage === "root-sim") {
    const card = $("trading-root-contract-card");
    if (card) card.open = true;
  }
  if (tradingActivePage === "settings") {
    if (typeof relocateSystemAdminSections === "function") relocateSystemAdminSections();
    if (typeof loadRootTradingSettings === "function") loadRootTradingSettings();
  }
}

function tradingRootList(rows, targetId, emptyText, renderRow) {
  const target = $(targetId);
  if (!target) return;
  const list = Array.isArray(rows) ? rows : [];
  target.innerHTML = list.length
    ? list.map(renderRow).join("")
    : `<div class="drive-empty">${sanitize(emptyText)}</div>`;
}

function tradingSignedPointsValue(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "-";
  const prefix = number > 0 ? "+" : number < 0 ? "-" : "";
  return `${prefix}${formatTradingPointsValue(Math.abs(number))}`;
}

function tradingFundFlowStatus(flow) {
  const safe = flow && typeof flow === "object" ? flow : {};
  const eventCount = Number(safe.event_count || 0);
  if (!eventCount) return "-";
  if (safe.balance_matches_event_replay === false) return "回放異常";
  const operatingNet = Number(safe.operating_net_points || 0);
  if (operatingNet > 0) return "營運為正";
  if (operatingNet < 0) return "營運淨流出";
  return "收支打平";
}

function tradingFundFlowDirectionLabel(value) {
  const number = Number(value || 0);
  if (number > 0) return "流入";
  if (number < 0) return "流出";
  return "打平";
}

function tradingFundFlowRoleLabel(row) {
  const role = String(row?.statement_role || "");
  if (role === "capital") return "資本";
  if (role === "principal_transfer") return "本金移轉";
  return "營運";
}

function tradingFundFlowMeterHtml({ inflow = 0, outflow = 0, net = 0, leftLabel = "流出", rightLabel = "流入" } = {}) {
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
      <span>${sanitize(leftLabel)} ${formatTradingPointsValue(outflow)}</span>
      <strong class="${netClass}">淨額 ${tradingSignedPointsValue(net)}</strong>
      <span>${sanitize(rightLabel)} ${formatTradingPointsValue(inflow)}</span>
    </div>
  `;
}

function tradingFundFlowTileHtml({ title = "-", amount = 0, direction = "inflow", meta = "", detail = "" } = {}) {
  return `
    <div class="finance-flow-tile" data-flow-direction="${sanitize(direction)}">
      <strong>${sanitize(title)}</strong>
      <b>${sanitize(amount)}</b>
      ${meta ? `<small>${sanitize(meta)}</small>` : ""}
      ${detail ? `<div class="drive-card-sub">${sanitize(detail)}</div>` : ""}
    </div>
  `;
}

function renderTradingRootSitewidePools(payload) {
  const safe = payload && typeof payload === "object" ? payload : {};
  const reserve = safe.reserve_pool && typeof safe.reserve_pool === "object" ? safe.reserve_pool : {};
  const funding = safe.funding_pool && typeof safe.funding_pool === "object" ? safe.funding_pool : {};
  const lending = safe.lending_summary && typeof safe.lending_summary === "object" ? safe.lending_summary : {};
  const margin = safe.open_margin_summary && typeof safe.open_margin_summary === "object" ? safe.open_margin_summary : {};
  const fees = safe.fee_summary && typeof safe.fee_summary === "object" ? safe.fee_summary : {};
  const flow = safe.fund_flow_summary && typeof safe.fund_flow_summary === "object" ? safe.fund_flow_summary : {};
  const flowCategories = Array.isArray(flow.categories) ? flow.categories : [];
  const realizedFlowCategories = Array.isArray(flow.realized_categories)
    ? flow.realized_categories
    : flowCategories.filter((row) => row && row.counts_as_operating !== false && row.statement_role !== "capital" && row.statement_role !== "principal_transfer");
  const flowLabelByEvent = new Map(flowCategories.map((row) => [String(row.event_type || ""), row.label || row.event_type || "-"]));
  const retainedIncome = Number(lending.fee_retained_points || 0) + Number(lending.interest_retained_points || 0);
  tradingSetText("trading-root-reserve-balance", formatTradingPointsValue(reserve.balance_points || 0));
  tradingSetText("trading-root-reserve-updated", `更新 ${reserve.updated_at || "-"}`);
  tradingSetText("trading-root-funding-available", formatTradingPointsValue(funding.available_points || 0));
  tradingSetText("trading-root-funding-outstanding", `貸出 ${formatTradingPointsValue(funding.outstanding_principal_points || 0)}`);
  tradingSetText("trading-root-funding-utilization", formatTradingPercent(funding.utilization_percent || 0));
  tradingSetText("trading-root-funding-apr", `APR ${formatTradingPercent(funding.effective_interest_apr_percent || 0)}% · ${tradingAssetDisplayLabel(funding.borrowed_asset_symbol || "USDT")}`);
  tradingSetText("trading-root-pool-income", formatTradingPointsValue(retainedIncome));
  tradingSetText(
    "trading-root-pool-income-detail",
    `fee ${formatTradingPointsValue(lending.fee_retained_points || fees.total_fee_points || 0)} / interest ${formatTradingPointsValue(lending.interest_retained_points || 0)}`,
  );
  tradingSetText("trading-root-fund-flow-inflow", formatTradingPointsValue(flow.operating_inflow_points || 0));
  tradingSetText("trading-root-fund-flow-inflow-detail", `總流入 ${formatTradingPointsValue(flow.total_inflow_points || 0)} · 資本 ${formatTradingPointsValue(flow.capital_inflow_points || 0)} · 本金 ${formatTradingPointsValue(flow.principal_inflow_points || 0)}`);
  tradingSetText("trading-root-fund-flow-outflow", formatTradingPointsValue(flow.operating_outflow_points || 0));
  tradingSetText("trading-root-fund-flow-outflow-detail", `總流出 ${formatTradingPointsValue(flow.total_outflow_points || 0)} · 資本 ${formatTradingPointsValue(flow.capital_outflow_points || 0)} · 本金 ${formatTradingPointsValue(flow.principal_outflow_points || 0)}`);
  tradingSetText("trading-root-fund-flow-net", tradingSignedPointsValue(flow.operating_net_points || 0));
  tradingSetText("trading-root-fund-flow-net-detail", `總淨流 ${tradingSignedPointsValue(flow.net_flow_points || 0)} · 目前餘額 ${formatTradingPointsValue(flow.current_balance_points || 0)}`);
  tradingSetText("trading-root-fund-flow-balance", tradingFundFlowStatus(flow));
  tradingSetText(
    "trading-root-fund-flow-balance-detail",
    `${flow.balance_matches_event_replay === false ? "事件回放不一致" : "事件回放一致"} · 事件 ${formatTradingPointsValue(flow.event_count || 0)} · 已實現分類 ${formatTradingPointsValue(flow.realized_category_count ?? realizedFlowCategories.length)}`,
  );
  const flowMeter = $("trading-root-fund-flow-meter");
  if (flowMeter) {
    flowMeter.innerHTML = tradingFundFlowMeterHtml({
      inflow: Number(flow.operating_inflow_points || 0),
      outflow: Number(flow.operating_outflow_points || 0),
      net: Number(flow.operating_net_points || 0),
      leftLabel: "營運流出",
      rightLabel: "營運流入",
    });
  }
  tradingRootList([
    {
      title: "借貸基金",
      value: `可用 ${formatTradingPointsValue(funding.available_points || 0)} · 貸出 ${formatTradingPointsValue(funding.outstanding_principal_points || 0)}`,
      detail: `容量 ${formatTradingPointsValue(funding.capacity_points || 0)} · CFD 盈餘保留 ${formatTradingPointsValue(funding.cfd_profit_reserve_required_points || 0)} · 使用率 ${formatTradingPercent(funding.utilization_percent || 0)}%`,
    },
    {
      title: "本金與回收",
      value: `貸出 ${formatTradingPointsValue(lending.lent_out_points || 0)} · 回收 ${formatTradingPointsValue(lending.repaid_points || 0)}`,
      detail: `開放倉位 ${formatTradingPointsValue(margin.open_margin_positions || 0)} · 本金 ${formatTradingPointsValue(margin.open_principal_points || 0)}`,
    },
    {
      title: "利息與 carry",
      value: `應收 ${formatTradingPointsValue(margin.open_interest_due_points || 0)} · 已保留 ${formatTradingPointsValue(lending.interest_retained_points || 0)}`,
      detail: `micropoints carry ${formatTradingPointsValue(margin.interest_carry_micropoints || 0)} · 最近事件 ${lending.latest_reserve_event_at || "-"}`,
    },
  ], "trading-root-lending-pool-list", "尚無借貸基金資料", (row) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(row.title)}</strong>
        <div class="drive-card-sub">${sanitize(row.value)}</div>
        <div class="drive-card-sub">${sanitize(row.detail)}</div>
      </div>
    </div>
  `);
  tradingRootList(realizedFlowCategories, "trading-root-fund-flow-category-list", "尚無已實現淨收支分類", (row) => {
    const net = Number(row.net_points || 0);
    const eventType = row.event_type || "-";
    return tradingFundFlowTileHtml({
      title: row.label || eventType,
      amount: `${tradingSignedPointsValue(net)} 點`,
      direction: net < 0 ? "outflow" : "inflow",
      meta: `已實現 · ${tradingFundFlowDirectionLabel(net)} · 事件 ${formatTradingPointsValue(row.event_count || 0)}`,
      detail: `流入 ${formatTradingPointsValue(row.inflow_points || 0)} · 流出 ${formatTradingPointsValue(row.outflow_points || 0)} · 最近 ${row.latest_event_at || "-"} · 類型 ${eventType}`,
    });
  });
  tradingRootList(safe.reserve_events || [], "trading-root-reserve-events-list", "尚無基金事件", (row) => {
    const delta = Number(row.delta_points || 0);
    const signed = delta >= 0 ? `+${formatTradingPointsValue(delta)}` : `-${formatTradingPointsValue(Math.abs(delta))}`;
    const label = flowLabelByEvent.get(String(row.event_type || "")) || row.event_type || "-";
    return tradingFundFlowTileHtml({
      title: label,
      amount: `${signed} 點`,
      direction: delta < 0 ? "outflow" : "inflow",
      meta: `${row.created_at || ""} · balance ${formatTradingPointsValue(row.balance_after || 0)}`,
      detail: row.reason || "-",
    });
  });
}

function renderTradingRootSitewidePositions(payload) {
  const safe = payload && typeof payload === "object" ? payload : {};
  const summary = safe.summary && typeof safe.summary === "object" ? safe.summary : {};
  tradingSetText("trading-root-position-spot-count", formatTradingPointsValue(summary.spot_position_count || 0));
  tradingSetText("trading-root-position-margin-count", formatTradingPointsValue(summary.margin_position_count || 0));
  tradingSetText("trading-root-position-margin-detail", `開倉 ${formatTradingPointsValue(summary.margin_position_count || 0)}`);
  tradingSetText("trading-root-position-orders", formatTradingPointsValue(summary.open_order_count || 0));
  tradingSetText("trading-root-position-orders-detail", `凍結 ${formatTradingPointsValue(summary.frozen_order_points || 0)}`);
  tradingSetText("trading-root-position-bots", formatTradingPointsValue(summary.total_bot_count || 0));
  tradingSetText("trading-root-position-bots-detail", `啟用 ${formatTradingPointsValue(summary.total_enabled_bot_count || 0)} · 網格 ${formatTradingPointsValue(summary.grid_bot_count || 0)}`);
  tradingSetText("trading-root-position-pnl", formatTradingPointsValue(summary.total_unrealized_pnl_points || 0));
  tradingSetText("trading-root-position-pnl-detail", `已實現 ${formatTradingPointsValue(summary.total_realized_pnl_points || 0)}`);
  tradingSetText("trading-root-position-fees", formatTradingPointsValue(summary.total_fee_points || 0));
  tradingSetText("trading-root-position-fees-detail", `現貨 ${formatTradingPointsValue(summary.spot_fee_points || 0)} · 借貸 ${formatTradingPointsValue(summary.margin_fee_points || 0)}`);
  tradingRootList(safe.spot_positions || [], "trading-root-spot-position-list", "尚無現貨倉位", (row) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(row.username || `user:${row.user_id || "-"}`)} · ${sanitize(tradingDisplaySymbol(row.market_symbol))}</strong>
        <div class="drive-card-sub">數量 ${sanitize(formatTradingQuantityValue(row.quantity))} · 鎖定 ${sanitize(formatTradingQuantityValue(row.locked_quantity))}</div>
        <div class="drive-card-sub">均價 ${sanitize(formatTradingPointsValue(row.avg_cost_points || 0))} · 現值 ${sanitize(formatTradingPointsValue(row.risk_grade_current_value_points ?? row.current_value_points ?? 0))} · 成本 ${sanitize(formatTradingPointsValue(row.cost_basis_points || 0))}</div>
        <div class="drive-card-sub">未實現 ${sanitize(formatTradingPointsValue(row.unrealized_pnl_points || 0))} · 已實現 ${sanitize(formatTradingPointsValue(row.realized_pnl_points || 0))} · 手續費 ${sanitize(formatTradingPointsValue(row.total_fee_points || 0))}</div>
        <div class="drive-card-sub">TP ${sanitize(row.take_profit_percent ?? "-")}% · SL ${sanitize(row.stop_loss_percent ?? "-")}%</div>
      </div>
    </div>
  `);
  tradingRootList(safe.margin_positions || [], "trading-root-margin-position-list", "尚無借貸倉位", (row) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(row.username || `user:${row.user_id || "-"}`)} · ${sanitize(tradingDisplaySymbol(row.market_symbol))} · ${sanitize(row.position_type || "-")}</strong>
        <div class="drive-card-sub">數量 ${sanitize(formatTradingQuantityValue(row.quantity))} · 入場 ${sanitize(formatTradingPointsValue(row.entry_price_points || 0))}</div>
        <div class="drive-card-sub">本金 ${sanitize(formatTradingPointsValue(row.principal_points || 0))} · 擔保 ${sanitize(formatTradingPointsValue(row.collateral_points || 0))} · 利息 ${sanitize(formatTradingPointsValue(row.interest_due_points || 0))}</div>
        <div class="drive-card-sub">未實現 ${sanitize(formatTradingPointsValue(row.unrealized_pnl_points || 0))} · 已實現 ${sanitize(formatTradingPointsValue(row.realized_pnl_points || 0))} · 手續費 ${sanitize(formatTradingPointsValue(row.total_fee_points || 0))}</div>
        <div class="economy-ledger-hash">${sanitize(row.position_uuid || "")}</div>
      </div>
    </div>
  `);
  const botRows = [
    ...(Array.isArray(safe.bots) ? safe.bots.map((row) => ({ ...row, family: "bot" })) : []),
    ...(Array.isArray(safe.grid_bots) ? safe.grid_bots.map((row) => ({ ...row, family: "grid" })) : []),
  ];
  tradingRootList(botRows, "trading-root-bot-position-list", "尚無交易機器人", (row) => {
    const isGrid = row.family === "grid";
    const status = row.enabled ? "啟用" : "暫停";
    const subtitle = isGrid
      ? `格數 ${formatTradingPointsValue(row.grid_count || 0)} · 每格 ${formatTradingPointsValue(row.order_amount_points || 0)} 點 · 掛單 ${formatTradingPointsValue(row.open_grid_orders || 0)}`
      : `${sanitize(row.side || "-")} ${sanitize(row.order_type || "-")} · 執行 ${formatTradingPointsValue(row.run_count || 0)} / ${formatTradingPointsValue(row.max_runs || 0)}`;
    const timing = isGrid
      ? `掃描 ${sanitize(row.last_scan_at || "-")} · 成交 ${formatTradingPointsValue(row.total_trades || 0)} · 利潤 ${formatTradingPointsValue(row.total_profit_points || 0)}`
      : `觸發 ${sanitize(row.trigger_type || "-")} ${sanitize(row.trigger_price_points ?? "-")} · 最近 ${sanitize(row.last_run_at || "-")}`;
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(row.username || `user:${row.user_id || "-"}`)} · ${sanitize(row.name || "-")} · ${sanitize(status)}</strong>
          <div class="drive-card-sub">${subtitle}</div>
          <div class="drive-card-sub">${timing}</div>
        </div>
      </div>
    `;
  });
}

async function refreshTradingRootSitewideSnapshots(reason = "trading_root_sitewide_refresh") {
  if (!tradingRootSitewideAllowed()) return { ok: false, skipped: true };
  return fetchTradingJson("/root/trading/sitewide/refresh", {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

async function loadTradingRootSitewide({ refreshSnapshot = false, silent = false } = {}) {
  if (!tradingRootSitewideAllowed()) return true;
  const status = $("trading-root-sitewide-status");
  try {
    if (status && !silent) status.textContent = "正在讀取全站倉位與借貸基金...";
    if (refreshSnapshot) {
      const refresh = await refreshTradingRootSitewideSnapshots(`trading_root_sitewide_${tradingRootSitewideActiveTab}_refresh`);
      if (refresh?.async && status && !silent) {
        status.textContent = `全站快照刷新已排入背景任務${refresh.job_id ? `：${refresh.job_id}` : ""}`;
        status.style.color = "var(--muted)";
      }
    }
    const [pools, positions] = await Promise.all([
      fetchTradingJson("/root/trading/sitewide/pools", { allowMissingSnapshot: true }),
      fetchTradingJson("/root/trading/sitewide/user-positions", { allowMissingSnapshot: true }),
    ]);
    if (pools?.snapshot?.missing || positions?.snapshot?.missing) {
      renderTradingRootSitewidePools({});
      renderTradingRootSitewidePositions({});
      const queued = typeof enqueueTradingSnapshotRefreshOnce === "function"
        ? await enqueueTradingSnapshotRefreshOnce("trading_root_sitewide_missing_snapshot")
        : { ok: false, msg: "背景刷新 helper 尚未載入" };
      if (status) {
        status.textContent = queued?.ok
          ? "全站倉位快照正在建立；已排入背景刷新，完成後重新整理即可查看。"
          : `全站倉位快照尚未建立；排程失敗：${queued?.msg || "請確認背景 worker"}`;
        status.style.color = queued?.ok === false ? "#ff4f6d" : "var(--muted)";
      }
      return true;
    }
    renderTradingRootSitewidePools(pools.pools || {});
    renderTradingRootSitewidePositions(positions.positions || {});
    if (status) {
      status.textContent = `全站倉位與借貸基金已更新：${new Date().toLocaleTimeString()}`;
      status.style.color = "var(--muted)";
    }
    return true;
  } catch (err) {
    if (status) {
      status.textContent = tradingFriendlyErrorText(err?.message || "全站倉位與借貸基金讀取失敗");
      status.style.color = "#ff4f6d";
    }
    if (!silent) tradingSetMsg(tradingFriendlyErrorText(err?.message || "全站倉位與借貸基金讀取失敗"), false);
    return false;
  }
}

function switchTradingRootSitewideTab(tab = "positions", options = {}) {
  syncTradingRootSitewideVisibility();
  if (!tradingRootSitewideAllowed()) return;
  tradingRootSitewideActiveTab = tab === "pools" ? "pools" : "positions";
  if (options.updatePage !== false) {
    tradingActivePage = tradingRootSitewideActiveTab === "pools" ? "fund-ops" : "sitewide-positions";
    syncTradingSubpages();
  }
  document.querySelectorAll("[data-trading-root-sitewide-tab]").forEach((btn) => {
    const lockedTab = tradingActivePage === "sitewide-positions" ? "positions" : (tradingActivePage === "fund-ops" ? "pools" : "");
    btn.hidden = !!lockedTab && btn.dataset.tradingRootSitewideTab !== lockedTab;
    btn.classList.toggle("active", btn.dataset.tradingRootSitewideTab === tradingRootSitewideActiveTab);
  });
  const title = $("trading-root-sitewide-title");
  const status = $("trading-root-sitewide-status");
  if (title) title.textContent = tradingRootSitewideActiveTab === "pools" ? "交易所基金營運管理" : "root 全站倉位看板";
  if (status) {
    status.textContent = tradingRootSitewideActiveTab === "pools"
      ? "交易所基金、借貸基金、準備金、保留收入與最近基金事件；不混入全站倉位或 root 模擬倉位。"
      : "交易所全站風控入口；現貨、借貸倉位、掛單與機器人狀態集中唯讀檢視。";
  }
  const positions = $("trading-root-sitewide-positions-panel");
  const pools = $("trading-root-sitewide-pools-panel");
  if (positions) positions.style.display = tradingRootSitewideActiveTab === "positions" ? "" : "none";
  if (pools) pools.style.display = tradingRootSitewideActiveTab === "pools" ? "" : "none";
  if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
  loadTradingRootSitewide(options);
}

function openTradingRootSitewidePanel(tab = "positions", options = {}) {
  setTradingActivePage(tab === "pools" ? "fund-ops" : "sitewide-positions");
  switchTradingRootSitewideTab(tab, options);
}

function openTradingRootFundOpsPanel(options = {}) {
  openTradingRootSitewidePanel("pools", options);
}

function openTradingRootSimulationPanel() {
  setTradingActivePage("root-sim");
}

function openTradingLendingPanel() {
  setTradingActivePage("spot", { openLending: true });
}

function openTradingMyPositionsPanel() {
  setTradingActivePage("my-positions");
}

function openTradingSpotPage() {
  setTradingActivePage("spot");
}

function openTradingExchangePage() {
  openTradingSpotPage();
}

function openTradingSettingsPage() {
  setTradingActivePage("settings");
}

function setTradingMarketSelection(marketSymbol) {
  const select = $("trading-market-select");
  const market = tradingMarketBySymbol(marketSymbol || "");
  if (select && market?.symbol) select.value = market.symbol;
  return market;
}

function prepareTradingSpotSellOrder(marketSymbol, orderType = "market") {
  const market = setTradingMarketSelection(marketSymbol);
  if (!market) {
    tradingSetMsg("找不到可操作的交易市場", false);
    return null;
  }
  const state = tradingSellableState(market.symbol);
  if (!state.available) {
    tradingSetMsg(`${tradingDisplaySymbol(market.symbol)} 目前沒有可賣現貨`, false);
    return null;
  }
  setTradingActivePage("spot");
  const side = $("trading-side");
  const type = $("trading-order-type");
  const mode = $("trading-input-mode");
  const quantity = $("trading-quantity");
  if (side) side.value = "sell";
  if (type) type.value = orderType === "limit" ? "limit" : "market";
  if (mode) mode.value = "quantity";
  if (quantity) quantity.value = tradingQuantityForSubmit(state.available);
  syncTradingOrderSideTheme();
  syncTradingOrderInputMode();
  updateTradingOrderEstimate();
  if (orderType === "limit") {
    const limit = $("trading-limit-price");
    if (limit) {
      limit.focus();
      limit.select?.();
    }
    tradingSetMsg(`已帶入 ${tradingDisplaySymbol(market.symbol)} 全部可賣數量，請輸入限價後送出。`);
  } else {
    tradingSetMsg(`已帶入 ${tradingDisplaySymbol(market.symbol)} 全部可賣數量，可確認後送出市價賣單。`);
  }
  return { market, state };
}

async function closeTradingSpotPositionMarket(marketSymbol) {
  const prepared = prepareTradingSpotSellOrder(marketSymbol, "market");
  if (!prepared) return;
  const { market, state } = prepared;
  if (!confirm(`以市價賣出 ${formatTradingQuantityValue(state.available)} ${state.asset}？`)) return;
  await submitTradingOrder();
}

function bindTradingSpotAssetActions(container = document) {
  container.querySelectorAll("[data-trading-spot-sell-market]").forEach((btn) => {
    if (btn.dataset.tradingSpotActionBound === "1") return;
    btn.dataset.tradingSpotActionBound = "1";
    bindTradingActionButton(
      btn,
      () => closeTradingSpotPositionMarket(btn.dataset.tradingSpotSellMarket || ""),
      "正在送出市價平倉...",
      "市價平倉失敗",
    );
  });
  container.querySelectorAll("[data-trading-spot-sell-limit]").forEach((btn) => {
    if (btn.dataset.tradingSpotActionBound === "1") return;
    btn.dataset.tradingSpotActionBound = "1";
    btn.addEventListener("click", () => prepareTradingSpotSellOrder(btn.dataset.tradingSpotSellLimit || "", "limit"));
  });
}

function tradingActiveSpotPositionRows(rows = []) {
  return Array.isArray(rows)
    ? rows.filter((row) => spotPositionTotalQuantity(row) > 0 || spotPositionNumber(row, "locked_quantity") > 0)
    : [];
}

function tradingSummaryNumber(summary, keys = [], fallback = 0) {
  const source = summary && typeof summary === "object" ? summary : {};
  for (const key of keys) {
    if (source[key] !== undefined && source[key] !== null) return tradingNumber(source[key], fallback);
  }
  return fallback;
}

function tradingSignedPointsText(value) {
  const number = tradingNumber(value, 0);
  return `${number >= 0 ? "+" : "-"}${formatTradingPointsValue(Math.abs(number))} 點`;
}

function tradingSpotPortfolioSummary(rows = tradingState.positions || []) {
  const activeRows = tradingActiveSpotPositionRows(rows);
  let referenceValue = 0;
  let riskValue = 0;
  let unrealizedPnl = 0;
  let totalFees = 0;
  let lockedMarkets = 0;
  activeRows.forEach((row) => {
    const market = tradingMarketBySymbol(row.market_symbol || "");
    referenceValue += tradingSpotCurrentValue(row, market);
    riskValue += tradingSpotRiskGradeValue(row, market);
    unrealizedPnl += tradingSpotPnl(row, market);
    totalFees += tradingNumber(row.total_fee_points ?? row.fee_points, 0);
    if (spotPositionNumber(row, "locked_quantity") > 0) lockedMarkets += 1;
  });
  const backend = tradingState.spotSummary || {};
  return {
    rows: activeRows,
    count: activeRows.length,
    lockedMarkets,
    referenceValue: tradingSummaryNumber(backend, ["reference_current_value_points", "current_value_points"], referenceValue),
    riskValue: tradingSummaryNumber(backend, ["risk_grade_current_value_points", "current_value_points"], riskValue || referenceValue),
    unrealizedPnl: tradingSummaryNumber(backend, ["risk_grade_unrealized_pnl_points", "unrealized_pnl_points"], unrealizedPnl),
    totalFees: tradingSummaryNumber(backend, ["total_fee_points", "fee_points"], totalFees),
  };
}

function tradingPortfolioFuturesRows(rows = tradingState.futuresPositions || []) {
  return Array.isArray(rows) ? rows.filter((row) => row.status === "open") : [];
}

function tradingPortfolioMarginRows(rows = tradingState.marginPositions || []) {
  return Array.isArray(rows) ? rows.filter((row) => row.status === "open") : [];
}

function tradingPortfolioSummary() {
  const funding = tradingState.funding || {};
  const spot = tradingSpotPortfolioSummary(tradingState.positions || []);
  const marginRows = tradingPortfolioMarginRows(tradingState.marginPositions || []);
  const futuresRows = tradingPortfolioFuturesRows(tradingState.futuresPositions || []);
  const marginSummary = tradingDisplayedMarginSummary(tradingState.marginSummary, marginRows);
  const available = tradingNumber(funding.available_points ?? funding.wallet_available_points, 0);
  const locked = tradingNumber(funding.locked_points, 0);
  const marginEquity = tradingNumber(marginSummary.total_position_equity_points, 0);
  const marginBorrowed = tradingNumber(marginSummary.total_borrowed_points, 0);
  const marginPnl = marginRows.reduce((total, row) => total + tradingNumber(row.unrealized_pnl_points ?? row.pnl_points, 0), 0);
  const marginFees = marginRows.reduce((total, row) => {
    return total + tradingNumber(row.open_fee_points ?? row.fee_points, 0) + tradingNumber(row.interest_paid_points, 0);
  }, 0);
  const futuresEquity = futuresRows.reduce((total, row) => {
    return total + tradingNumber(row.margin_points, 0) + tradingNumber(row.unrealized_pnl_points ?? row.pnl_points, 0);
  }, 0);
  const futuresPnl = futuresRows.reduce((total, row) => total + tradingNumber(row.unrealized_pnl_points ?? row.pnl_points, 0), 0);
  return {
    available,
    locked,
    totalEquity: available + locked + spot.riskValue + marginEquity + futuresEquity,
    spot,
    marginRows,
    futuresRows,
    marginEquity,
    marginBorrowed,
    marginPnl,
    marginFees,
    futuresEquity,
    futuresPnl,
  };
}

function tradingPortfolioWalletRow(summary) {
  const funding = tradingState.funding || {};
  const selectedWallet = funding.selected_wallet_address || funding.active_wallet_address || "";
  return `
    <div class="drive-file-row">
      <div>
        <strong>站內可用點數 · ${formatTradingPointsValue(summary.available)} 點</strong>
        <div class="drive-card-sub">鎖定 ${formatTradingPointsValue(summary.locked)} 點${selectedWallet ? ` · 付款錢包 ${sanitize(tradingShortWalletAddress(selectedWallet))}` : ""}</div>
      </div>
    </div>
  `;
}

function tradingPortfolioSpotRow(position) {
  const market = tradingMarketBySymbol(position.market_symbol || "");
  const quantity = spotPositionTotalQuantity(position);
  const available = Math.max(0, spotPositionNumber(position, "quantity"));
  const locked = Math.max(0, spotPositionNumber(position, "locked_quantity"));
  const riskValue = tradingSpotRiskGradeValue(position, market);
  const pnl = tradingSpotPnl(position, market);
  const pnlClass = pnl >= 0 ? "positive" : "negative";
  const wallet = position.source_wallet_address || position.wallet_address || position.hot_wallet_address || "";
  return `
    <div class="drive-file-row">
      <div>
        <strong>現貨 · ${sanitize(tradingDisplaySymbol(position.market_symbol || "-"))} · ${sanitize(formatTradingQuantityValue(quantity))}</strong>
        <div class="drive-card-sub">估值 ${formatTradingPointsValue(riskValue)} 點 · 未實現 <b class="${pnlClass}">${sanitize(tradingSignedPointsText(pnl))}</b></div>
        <div class="drive-card-sub">可賣 ${sanitize(formatTradingQuantityValue(available))} · 鎖定 ${sanitize(formatTradingQuantityValue(locked))}${wallet ? ` · 錢包 ${sanitize(tradingShortWalletAddress(wallet))}` : ""}</div>
      </div>
      <div class="drive-file-actions">
        <button class="btn btn-sm" type="button" data-trading-spot-sell-market="${sanitize(position.market_symbol || "")}"${available > 0 ? "" : " disabled"}>市價平倉</button>
        <button class="btn btn-sm" type="button" data-trading-spot-sell-limit="${sanitize(position.market_symbol || "")}"${available > 0 ? "" : " disabled"}>限價平倉</button>
      </div>
    </div>
  `;
}

function tradingPortfolioMarginRow(row) {
  const risk = tradingLiveMarginRisk(row);
  const isShort = tradingMarginPositionIsShort(row);
  const typeLabel = row.position_label || (isShort ? "借券放空" : "融資買入");
  const equity = tradingNumber(risk.equity_after_points ?? row.equity_points, 0);
  const pnl = tradingNumber(risk.unrealized_pnl_points ?? row.unrealized_pnl_points ?? row.pnl_points, 0);
  const pnlClass = pnl >= 0 ? "positive" : "negative";
  const withdrawEstimate = tradingMarginWithdrawEstimate(row, risk);
  const withdrawDisabledAttr = withdrawEstimate.maxWithdrawable > 0 ? "" : " disabled";
  const uuid = row.position_uuid || "";
  return `
    <div class="drive-file-row">
      <div>
        <strong>借貸 · ${sanitize(typeLabel)} · ${sanitize(tradingDisplaySymbol(row.market_symbol || "-"))}</strong>
        <div class="drive-card-sub">數量 ${sanitize(formatTradingQuantityValue(row.quantity))} · 權益 ${formatTradingPointsValue(equity)} 點 · 本金 ${formatTradingPointsValue(row.principal_points || 0)} 點</div>
        <div class="drive-card-sub">未實現 <b class="${pnlClass}">${sanitize(tradingSignedPointsText(pnl))}</b> · 開倉費 ${formatTradingPointsValue(row.open_fee_points || 0)} 點 · 已付利息 ${formatTradingPointsValue(row.interest_paid_points || 0)} 點</div>
      </div>
      <div class="drive-file-actions trading-portfolio-actions">
        <div class="field trading-inline-field">
          <label>補保證金</label>
          <input type="number" min="1" step="1" placeholder="點數" data-margin-collateral-amount="${sanitize(uuid)}" />
        </div>
        <button class="btn btn-sm" type="button" data-margin-add-collateral="${sanitize(uuid)}">補保證金</button>
        <div class="field trading-inline-field">
          <label>抽出保證金</label>
          <input type="number" min="1" step="1" max="${sanitize(withdrawEstimate.maxWithdrawable)}" placeholder="${sanitize(withdrawEstimate.maxWithdrawable > 0 ? `${formatTradingPointsValue(withdrawEstimate.maxWithdrawable)} 點內` : "暫不可抽出")}" data-margin-withdraw-collateral-amount="${sanitize(uuid)}" />
        </div>
        <button class="btn btn-sm" type="button" data-margin-withdraw-collateral="${sanitize(uuid)}"${withdrawDisabledAttr}>抽出</button>
        <button class="btn btn-sm btn-danger" type="button" data-margin-close="${sanitize(uuid)}">平倉</button>
      </div>
    </div>
  `;
}

function tradingPortfolioFuturesRow(row) {
  const pnl = tradingNumber(row.unrealized_pnl_points ?? row.pnl_points, 0);
  const pnlClass = pnl >= 0 ? "positive" : "negative";
  return `
    <div class="drive-file-row">
      <div>
        <strong>合約 · ${sanitize(row.side || "-")} · ${sanitize(tradingDisplaySymbol(row.market_symbol || "-"))}</strong>
        <div class="drive-card-sub">數量 ${sanitize(formatTradingQuantityValue(row.quantity))} · 保證金 ${formatTradingPointsValue(row.margin_points || 0)} 點 · 槓桿 ${formatTradingPointsValue(row.leverage || 1)}x</div>
        <div class="drive-card-sub">未實現 <b class="${pnlClass}">${sanitize(tradingSignedPointsText(pnl))}</b></div>
      </div>
      <div class="drive-file-actions">
        <button class="btn btn-sm btn-danger" type="button" data-contract-close="${sanitize(row.position_uuid || "")}">平倉</button>
      </div>
    </div>
  `;
}

function bindTradingMarginAssetActions(container = document) {
  container.querySelectorAll("[data-margin-close]").forEach((btn) => {
    bindTradingActionButton(btn, () => closeTradingMarginPosition(btn.dataset.marginClose || ""), "正在平倉進階交易...", "進階交易平倉失敗");
  });
  container.querySelectorAll("[data-margin-add-collateral]").forEach((btn) => {
    bindTradingActionButton(
      btn,
      (event) => addTradingMarginCollateral(btn.dataset.marginAddCollateral || "", event?.currentTarget || btn),
      "正在補入保證金...",
      "補保證金失敗"
    );
  });
  container.querySelectorAll("[data-margin-withdraw-collateral]").forEach((btn) => {
    bindTradingActionButton(
      btn,
      (event) => withdrawTradingMarginCollateral(btn.dataset.marginWithdrawCollateral || "", event?.currentTarget || btn),
      "正在抽出保證金...",
      "抽出保證金失敗"
    );
  });
}

function bindTradingContractAssetActions(container = document) {
  container.querySelectorAll("[data-contract-close]").forEach((btn) => {
    bindTradingActionButton(btn, () => closeRootTradingContract(btn.dataset.contractClose || ""), "正在平倉合約...", "合約平倉失敗");
  });
}

function renderTradingPortfolioSummary() {
  const summary = tradingPortfolioSummary();
  if ($("trading-portfolio-total-equity")) $("trading-portfolio-total-equity").textContent = formatTradingPointsValue(summary.totalEquity);
  if ($("trading-portfolio-detail")) {
    $("trading-portfolio-detail").textContent = `可用 ${formatTradingPointsValue(summary.available)} 點 · 鎖定 ${formatTradingPointsValue(summary.locked)} 點 · 現貨估值 ${formatTradingPointsValue(summary.spot.riskValue)} 點`;
  }
  if ($("trading-position-quantity")) {
    $("trading-position-quantity").textContent = `${formatTradingPointsValue(summary.spot.count)} 個市場`;
  }
  if ($("trading-position-locked")) {
    $("trading-position-locked").textContent = `估值 ${formatTradingPointsValue(summary.spot.riskValue)} 點 · 未實現 ${tradingSignedPointsText(summary.spot.unrealizedPnl)} · 手續費 ${formatTradingPointsValue(summary.spot.totalFees)} 點`;
  }
  if ($("trading-portfolio-leverage-count")) {
    $("trading-portfolio-leverage-count").textContent = `${formatTradingPointsValue(summary.marginRows.length + summary.futuresRows.length)} 筆`;
  }
  if ($("trading-portfolio-leverage-detail")) {
    $("trading-portfolio-leverage-detail").textContent = `借貸 ${formatTradingPointsValue(summary.marginRows.length)} · 合約 ${formatTradingPointsValue(summary.futuresRows.length)} · 借款 ${formatTradingPointsValue(summary.marginBorrowed)} 點 · 未實現 ${tradingSignedPointsText(summary.marginPnl + summary.futuresPnl)}`;
  }
  const target = $("trading-portfolio-asset-list");
  if (!target) return;
  const rows = [
    tradingPortfolioWalletRow(summary),
    ...summary.spot.rows.map((row) => tradingPortfolioSpotRow(row)),
    ...summary.marginRows.map((row) => tradingPortfolioMarginRow(row)),
    ...summary.futuresRows.map((row) => tradingPortfolioFuturesRow(row)),
  ];
  if (rows.length <= 1) {
    rows.push('<div class="drive-empty">尚無現貨、借貸或合約倉位</div>');
  }
  target.innerHTML = rows.join("");
  bindTradingSpotAssetActions(target);
  bindTradingMarginAssetActions(target);
  bindTradingContractAssetActions(target);
}

function renderTradingSummary() {
  const market = selectedTradingMarket();
  const funding = tradingState.funding || {};
  const orderForm = $("trading-order-form");
  const submitBtn = $("trading-submit-order-btn");
  const availabilityNote = $("trading-availability-note");
  const contractCard = $("trading-root-contract-card");
  const marginCard = $("trading-margin-card");
  const marginOpenForm = $("trading-margin-open-form");
  const marginEstimate = $("trading-margin-estimate");
  const fundingPoolCard = $("trading-funding-pool-public");
  const activePage = normalizeTradingPage(tradingActivePage);
  syncTradingExchangeSurfaceVisibility(activePage);
  if (submitBtn) submitBtn.disabled = false;
  const borrowingEnabled = !!tradingState.settings?.borrowing_enabled;
  const openMarginCount = (tradingState.marginPositions || []).filter((row) => row.status === "open").length;
  const showMarginCard = borrowingEnabled || openMarginCount > 0;
  if (marginCard) {
    marginCard.style.display = activePage === "my-positions" || (activePage === "spot" && showMarginCard) ? "" : "none";
    if (activePage === "my-positions") marginCard.open = true;
  }
  if (marginOpenForm) marginOpenForm.style.display = activePage === "spot" && borrowingEnabled ? "" : "none";
  if (marginEstimate) marginEstimate.style.display = activePage === "spot" && borrowingEnabled ? "" : "none";
  if (fundingPoolCard) fundingPoolCard.style.display = activePage === "spot" && borrowingEnabled ? "" : "none";
  const marginControlsDisabled = !borrowingEnabled;
  ["trading-margin-market-select", "trading-margin-type", "trading-margin-quantity", "trading-margin-collateral", "trading-margin-stop-loss-percent", "trading-margin-take-profit-percent", "trading-margin-open-btn"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = marginControlsDisabled;
  });
  if ($("trading-margin-note")) {
    const timing = tradingBorrowTimingSummary();
    const cryptoApr = tradingBorrowEffectiveAprPercent("btc_eth");
    const stableApr = tradingBorrowEffectiveAprPercent("usdt_points");
    $("trading-margin-note").textContent = borrowingEnabled
      ? (currentUser === "root"
        ? `root 可用模擬資金進行融資 / 借券；BTC / ETH 約 ${formatTradingPercent(cryptoApr)}% APR，USDT / 積分 約 ${formatTradingPercent(stableApr)}% APR；${timing.text}；不寫入 PointsChain。`
        : `已開啟；BTC / ETH 約 ${formatTradingPercent(cryptoApr)}% APR，USDT / 積分 約 ${formatTradingPercent(stableApr)}% APR；${timing.text}；本金由借貸基金借出，手續費與利息回到借貸基金。`)
      : "root 尚未開啟借貸交易，目前僅可查看此區。";
  }
  const fundingPool = tradingState.fundingPool || {};
  if ($("trading-funding-pool-available")) $("trading-funding-pool-available").textContent = formatTradingPointsValue(fundingPool.available_points);
  if ($("trading-funding-pool-outstanding")) $("trading-funding-pool-outstanding").textContent = formatTradingPointsValue(fundingPool.outstanding_principal_points);
  if ($("trading-funding-pool-utilization")) $("trading-funding-pool-utilization").textContent = formatTradingPercent(fundingPool.utilization_percent);
  if ($("trading-funding-pool-rate-btc-eth")) $("trading-funding-pool-rate-btc-eth").textContent = formatTradingPercent(tradingBorrowEffectiveAprPercent("btc_eth"));
  if ($("trading-funding-pool-rate-usdt-points")) $("trading-funding-pool-rate-usdt-points").textContent = formatTradingPercent(tradingBorrowEffectiveAprPercent("usdt_points"));
  syncTradingRootSitewideVisibility();
  if (availabilityNote) {
    const publicSpotSymbols = economySpotMarkets(tradingState.markets || [])
      .map((row) => tradingDisplaySymbol(row.symbol))
      .filter(Boolean);
    const pauseKinds = [];
    if (tradingState.settings?.price_degrade_pause_market_orders) pauseKinds.push("市價交易");
    if (tradingState.settings?.price_degrade_pause_bots) pauseKinds.push("機器人");
    if (tradingState.settings?.price_degrade_pause_borrowing) pauseKinds.push("借貸交易");
    const degradeNote = pauseKinds.length
      ? `價格降級時會自動暫停：${pauseKinds.join(" / ")}。`
      : "價格降級時目前只警示，不自動暫停交易。";
    availabilityNote.textContent = currentUser === "root"
      ? `root 可使用現貨、進階交易與合約模擬；root 以外用戶目前僅開放現貨與已啟用的進階交易。${degradeNote}`
      : `目前對 root 以外用戶開放 ${publicSpotSymbols.join("、") || "已啟用的積分現貨市場"} 現貨。${degradeNote}`;
  }
  const trial = funding.trial_credit || null;
  const trialAvailable = trial ? Number(trial.available_points || 0) : 0;
  const trialInitial = trial ? Number(trial.initial_points || 0) : 0;
  const walletAvailable = Number(funding.wallet_available_points || 0);
  const totalAvailable = Number(funding.available_points ?? (walletAvailable + trialAvailable));
  if ($("trading-funding-available")) $("trading-funding-available").textContent = funding.available_points != null ? formatTradingPointsValue(totalAvailable) : "-";
  if ($("trading-funding-mode")) {
    const selectedWallet = funding.selected_wallet_address || funding.active_wallet_address || "";
    const walletText = selectedWallet ? `付款錢包 ${tradingShortWalletAddress(selectedWallet)} · ` : "";
    $("trading-funding-mode").textContent = funding.mode === "root_simulated"
      ? `root 模擬資金 · 鎖定 ${formatTradingPointsValue(funding.locked_points)}`
      : `${walletText}體驗金優先 · 總可用 ${formatTradingPointsValue(totalAvailable)} = 體驗金 ${formatTradingPointsValue(trialAvailable)} + 真實積分 ${formatTradingPointsValue(walletAvailable)} · 鎖定 ${formatTradingPointsValue(funding.locked_points)}`;
  }
  if ($("trading-trial-credit-available")) {
    $("trading-trial-credit-available").textContent = trial ? `${formatTradingPointsValue(trialAvailable)} / ${formatTradingPointsValue(trialInitial)}` : "-";
  }
  updateTradingTrialCountdown();
  renderTradingCurrentPrice(market, { animate: false, ...(tradingState.livePriceMeta?.[market?.symbol] || {}) });
  renderTradingRiskDashboard();
  if ($("trading-fee-rate-percent")) $("trading-fee-rate-percent").textContent = market ? formatTradingPercent(market.fee_rate_percent || 0) : "-";
  renderTradingPortfolioSummary();
  renderTradingRootSimulationPositions();
  renderTradingSpotPositions(tradingState.positions || []);
  const limit = $("trading-limit-price");
  if (limit && market && !$("trading-root-price")?.matches(":focus")) {
    limit.placeholder = `目前 ${formatTradingPointsValue(tradingMarketPricePoints(market, "reference"))}`;
  }
  syncTradingOrderSideTheme();
  syncTradingOrderInputMode();
  updateTradingOrderEstimate();
  updateTradingMarginEstimate();
  loadTradingBtcSignal();
  loadTradingReferencePrices();
}

function tradingSpotPositionDetailRow(position) {
  const market = tradingState.markets.find((row) => row.symbol === position.market_symbol) || null;
  const quantity = spotPositionTotalQuantity(position);
  const available = Math.max(0, spotPositionNumber(position, "quantity"));
  const locked = spotPositionNumber(position, "locked_quantity");
  const holdingCost = tradingSpotHoldingCost(position, market);
  const holdingCostPerUnit = tradingSpotHoldingCostPerUnit(position, market);
  const breakEvenPrice = tradingSpotBreakEvenExitPrice(position, market);
  const currentValue = tradingSpotCurrentValue(position, market);
  const riskValue = tradingSpotRiskGradeValue(position, market);
  const unrealizedPnl = tradingSpotPnl(position, market);
  const realizedPnl = tradingNumber(position.realized_pnl_points ?? position.realized_profit_points, 0);
  const pnlClass = unrealizedPnl >= 0 ? "positive" : "negative";
  const wallet = position.source_wallet_address || position.wallet_address || position.hot_wallet_address || "";
  return `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(tradingDisplaySymbol(position.market_symbol || "-"))} · ${sanitize(formatTradingQuantityValue(quantity))}</strong>
        <div class="drive-card-sub">可賣 ${sanitize(formatTradingQuantityValue(available))} · 鎖定 ${sanitize(formatTradingQuantityValue(locked))} · 目前部位價值 ${formatTradingPointsValue(currentValue)} 點 · risk-grade 估值 ${formatTradingPointsValue(riskValue)} 點</div>
        <div class="drive-card-sub">持有成本 ${formatTradingPointsValue(holdingCost)} 點 · 單顆 ${formatTradingPointsValue(holdingCostPerUnit)} · 損益平均價格 ${breakEvenPrice ? formatTradingPointsValue(breakEvenPrice) : "無法估算"}</div>
        <div class="drive-card-sub">平均成本 ${formatTradingPointsValue(position.avg_cost_points || 0)} · 買入待攤手續費 ${formatTradingPointsValue(position.estimated_buy_fee_points || 0)} 點${wallet ? ` · 錢包 ${sanitize(tradingShortWalletAddress(wallet))}` : ""}</div>
        <div class="drive-card-sub">目前部位價值採 reference price；未實現盈虧採 risk-grade price。損益平均價格已含預估賣出手續費；risk-grade 價計算未實現盈虧。</div>
        <div class="drive-card-sub">未實現盈虧 <b class="trading-spot-pnl ${pnlClass}">${unrealizedPnl >= 0 ? "+" : ""}${formatTradingPointsValue(unrealizedPnl)} 點</b> · 已實現盈虧 ${realizedPnl >= 0 ? "+" : ""}${formatTradingPointsValue(realizedPnl)} 點</div>
      </div>
      <div class="drive-file-actions">
        <button class="btn btn-sm" type="button" data-trading-spot-sell-market="${sanitize(position.market_symbol || "")}"${available > 0 ? "" : " disabled"}>市價平倉</button>
        <button class="btn btn-sm" type="button" data-trading-spot-sell-limit="${sanitize(position.market_symbol || "")}"${available > 0 ? "" : " disabled"}>限價平倉</button>
      </div>
    </div>
  `;
}

function renderTradingSpotPositions(rows = []) {
  const target = $("trading-spot-position-detail-list");
  if (!target) return;
  const list = tradingActiveSpotPositionRows(rows);
  target.innerHTML = list.length
    ? list.map((row) => tradingSpotPositionDetailRow(row)).join("")
    : '<div class="drive-empty">尚無現貨部位</div>';
  bindTradingSpotAssetActions(target);
}

function tradingSignalBoolLabel(value) {
  if (value === true) return "通過";
  if (value === false) return "未通過";
  return "-";
}

function tradingBtcSignalCountdownText(signal) {
  const nextAt = signal?.next_prediction_at ? new Date(signal.next_prediction_at).getTime() : 0;
  if (!Number.isFinite(nextAt) || nextAt <= 0) return "";
  const remainingMs = Math.max(0, nextAt - Date.now());
  return remainingMs > 0
    ? `下次預測倒數 ${formatTradingDuration(remainingMs)}`
    : "下次預測即將更新";
}

function updateTradingBtcSignalMeta() {
  const meta = $("trading-btc-signal-meta");
  const card = $("trading-btc-signal-card");
  const payload = tradingState.btcSignal || null;
  const signal = payload?.signal || null;
  if (!meta || !card || card.style.display === "none" || !payload?.available || !signal) return;
  const updatedAt = signal.updated_at ? new Date(signal.updated_at) : null;
  const updated = updatedAt && Number.isFinite(updatedAt.getTime()) ? updatedAt.toLocaleString() : "-";
  const ageMs = updatedAt && Number.isFinite(updatedAt.getTime()) ? Math.max(0, Date.now() - updatedAt.getTime()) : Number(signal.age_seconds || 0) * 1000;
  const countdown = tradingBtcSignalCountdownText(signal);
  meta.textContent = `來源 BTC_trade · 週期 ${signal.timeframe || "4h"} · 更新 ${updated}${ageMs ? ` · 約 ${formatTradingDuration(ageMs)} 前` : ""}${countdown ? ` · ${countdown}` : ""}`;
}

function renderTradingBtcSignal(payload = null) {
  const card = $("trading-btc-signal-card");
  if (!card) return;
  const market = selectedTradingMarket();
  if (!market || !market?.btc_trade_supported || !payload?.available || !payload.signal) {
    card.style.display = "none";
    return;
  }
  const signal = payload.signal || {};
  const entryChecks = signal.entry_checks && typeof signal.entry_checks === "object" ? signal.entry_checks : {};
  const ml = signal.ml_status && typeof signal.ml_status === "object" ? signal.ml_status : {};
  const signalOk = signal.signal_ok === true;
  const mlOk = signal.ml_ok === true;
  const badge = $("trading-btc-signal-badge");
  const meta = $("trading-btc-signal-meta");
  const body = $("trading-btc-signal-body");
  const checks = $("trading-btc-signal-checks");
  card.style.display = "";
  if (badge) {
    badge.textContent = signalOk && mlOk ? "偏多觀察" : "等待條件";
    badge.style.background = signalOk && mlOk ? "rgba(76,175,80,.18)" : "rgba(255,183,77,.16)";
    badge.style.color = signalOk && mlOk ? "#4caf50" : "#ffb74d";
  }
  if (meta) {
    updateTradingBtcSignalMeta();
  }
  if (body) {
    body.innerHTML = `
      <div><span class="drive-card-sub">目前價格</span><strong>${sanitize(tradingReferenceLabel(signal.current_price))}</strong><small>${sanitize(tradingDisplaySymbol(market.symbol))}</small></div>
      <div><span class="drive-card-sub">七條件信號</span><strong>${sanitize(tradingSignalBoolLabel(signal.signal_ok))}</strong><small>${signalOk ? "可進場觀察" : "未全滿足"}</small></div>
      <div><span class="drive-card-sub">ML 過濾</span><strong>${sanitize(tradingSignalBoolLabel(signal.ml_ok))}</strong><small>${sanitize(ml.situation || (ml.blocked ? "已阻擋" : "未提供"))}</small></div>
      <div><span class="drive-card-sub">BTC_trade 持倉</span><strong>${sanitize(signal.position || signal.portfolio?.position || "空手")}</strong><small>${sanitize(signal.last_trade?.action || "無最新交易")}</small></div>
      <div><span class="drive-card-sub">策略版本</span><strong>${sanitize(signal.strategy_version || "-")}</strong><small>Fear & Greed ${sanitize(signal.fear_greed ?? "-")}</small></div>
      ${signal.next_prediction_at ? `<div><span class="drive-card-sub">下次預測</span><strong>${sanitize(tradingBtcSignalCountdownText(signal).replace("下次預測", "").trim())}</strong><small>${sanitize(new Date(signal.next_prediction_at).toLocaleString())}</small></div>` : ""}
    `;
  }
  if (checks) {
    const rows = Object.entries(entryChecks).map(([name, ok]) => `${ok ? "✓" : "×"} ${name}`).slice(0, 12);
    checks.textContent = rows.length ? rows.join(" · ") : "尚無條件細節";
  }
}

async function loadTradingBtcSignal() {
  const market = selectedTradingMarket();
  if (!market || !market?.btc_trade_supported) {
    renderTradingBtcSignal(null);
    return;
  }
  try {
    const json = await fetchTradingJson(`/trading/btc-signal?market=${encodeURIComponent(tradingMarketRequestSymbol(market.symbol))}`);
    tradingState.btcSignal = json;
    renderTradingBtcSignal(json);
  } catch (_) {
    tradingState.btcSignal = null;
    renderTradingBtcSignal(null);
  }
}

function tradingReferenceLabel(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "-";
  return number >= 1000
    ? `$${number.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
    : `$${number.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function tradingReferenceTimeLabel(point, interval = "") {
  const rawTime = Number(point?.time || 0);
  const date = Number.isFinite(rawTime) && rawTime > 0
    ? new Date(rawTime)
    : new Date(point?.time_iso || Date.now());
  if (Number.isNaN(date.getTime())) return "-";
  return interval === "1d"
    ? date.toLocaleDateString()
    : date.toLocaleString();
}

function hideTradingReferenceTooltip() {
  const tooltip = $("trading-reference-tooltip");
  if (tooltip) {
    tooltip.hidden = true;
    tooltip.textContent = "";
  }
  if (tradingReferenceHoverIndex !== null) {
    tradingReferenceHoverIndex = null;
    if (tradingState.referencePrices) renderTradingReferenceChart(tradingState.referencePrices);
  }
}

function tradingIndicatorEnabled(id) {
  const el = $(id);
  return !!el?.checked;
}

function tradingIndicatorClose(point) {
  return Number(point?.close_usdt || point?.price_usdt || point?.close || 0);
}

function tradingIndicatorHigh(point) {
  return Number(point?.high_usdt || point?.high_points || point?.price_usdt || point?.close_usdt || point?.close || 0);
}

function tradingIndicatorLow(point) {
  return Number(point?.low_usdt || point?.low_points || point?.price_usdt || point?.close_usdt || point?.close || 0);
}

function tradingIndicatorSeries(candles, period, mode = "sma") {
  const closes = candles.map(tradingIndicatorClose);
  const values = Array(closes.length).fill(null);
  if (!period || period < 1 || closes.length < period) return values;
  if (mode === "ema") {
    const alpha = 2 / (period + 1);
    let ema = null;
    closes.forEach((close, index) => {
      if (!Number.isFinite(close) || close <= 0) return;
      ema = ema == null ? close : close * alpha + ema * (1 - alpha);
      values[index] = ema;
    });
    return values;
  }
  for (let index = period - 1; index < closes.length; index += 1) {
    const windowValues = closes.slice(index - period + 1, index + 1).filter((value) => Number.isFinite(value) && value > 0);
    if (windowValues.length !== period) continue;
    values[index] = windowValues.reduce((sum, value) => sum + value, 0) / period;
  }
  return values;
}

function tradingIndicatorWindowAverage(values, period) {
  const output = Array(values.length).fill(null);
  if (!period || period < 1) return output;
  for (let index = period - 1; index < values.length; index += 1) {
    const windowValues = values.slice(index - period + 1, index + 1);
    if (windowValues.some((value) => !Number.isFinite(value))) continue;
    output[index] = windowValues.reduce((sum, value) => sum + value, 0) / period;
  }
  return output;
}

function tradingBollingerSeries(candles, period = 20, multiplier = 2) {
  const closes = candles.map(tradingIndicatorClose);
  const upper = Array(closes.length).fill(null);
  const middle = Array(closes.length).fill(null);
  const lower = Array(closes.length).fill(null);
  for (let index = period - 1; index < closes.length; index += 1) {
    const windowValues = closes.slice(index - period + 1, index + 1).filter((value) => Number.isFinite(value) && value > 0);
    if (windowValues.length !== period) continue;
    const mean = windowValues.reduce((sum, value) => sum + value, 0) / period;
    const variance = windowValues.reduce((sum, value) => sum + (value - mean) ** 2, 0) / period;
    const stddev = Math.sqrt(variance);
    upper[index] = mean + multiplier * stddev;
    middle[index] = mean;
    lower[index] = mean - multiplier * stddev;
  }
  return { upper, middle, lower };
}

function tradingRsiSeries(candles, period = 14) {
  const closes = candles.map(tradingIndicatorClose);
  const values = Array(closes.length).fill(null);
  if (!period || period < 1 || closes.length <= period) return values;
  let gains = 0;
  let losses = 0;
  for (let index = 1; index <= period; index += 1) {
    const prev = closes[index - 1];
    const current = closes[index];
    if (!Number.isFinite(prev) || !Number.isFinite(current) || prev <= 0 || current <= 0) return values;
    const delta = current - prev;
    gains += Math.max(delta, 0);
    losses += Math.max(-delta, 0);
  }
  let avgGain = gains / period;
  let avgLoss = losses / period;
  values[period] = avgLoss === 0 ? (avgGain === 0 ? 50 : 100) : 100 - (100 / (1 + (avgGain / avgLoss)));
  for (let index = period + 1; index < closes.length; index += 1) {
    const prev = closes[index - 1];
    const current = closes[index];
    if (!Number.isFinite(prev) || !Number.isFinite(current) || prev <= 0 || current <= 0) continue;
    const delta = current - prev;
    const gain = Math.max(delta, 0);
    const loss = Math.max(-delta, 0);
    avgGain = ((avgGain * (period - 1)) + gain) / period;
    avgLoss = ((avgLoss * (period - 1)) + loss) / period;
    values[index] = avgLoss === 0 ? (avgGain === 0 ? 50 : 100) : 100 - (100 / (1 + (avgGain / avgLoss)));
  }
  return values;
}

function tradingKdSeries(candles, lookback = 9, smoothK = 3, smoothD = 3) {
  const rawK = Array(candles.length).fill(null);
  for (let index = lookback - 1; index < candles.length; index += 1) {
    const windowPoints = candles.slice(index - lookback + 1, index + 1);
    const highs = windowPoints.map(tradingIndicatorHigh).filter((value) => Number.isFinite(value) && value > 0);
    const lows = windowPoints.map(tradingIndicatorLow).filter((value) => Number.isFinite(value) && value > 0);
    const close = tradingIndicatorClose(candles[index]);
    if (highs.length !== lookback || lows.length !== lookback || !Number.isFinite(close) || close <= 0) continue;
    const highest = Math.max(...highs);
    const lowest = Math.min(...lows);
    rawK[index] = highest === lowest ? 50 : ((close - lowest) * 100) / (highest - lowest);
  }
  const k = tradingIndicatorWindowAverage(rawK, smoothK);
  const d = tradingIndicatorWindowAverage(k, smoothD);
  return { k, d };
}

function buildTradingReferenceIndicators(candles) {
  const overlays = [];
  const oscillators = [];
  if (tradingIndicatorEnabled("trading-indicator-ma5")) {
    overlays.push({ key: "ma5", label: "MA5", color: "#f59e0b", values: tradingIndicatorSeries(candles, 5), axis: "price" });
  }
  if (tradingIndicatorEnabled("trading-indicator-ma10")) {
    overlays.push({ key: "ma10", label: "MA10", color: "#fde047", values: tradingIndicatorSeries(candles, 10), axis: "price" });
  }
  if (tradingIndicatorEnabled("trading-indicator-ma20")) {
    overlays.push({ key: "ma20", label: "MA20", color: "#38bdf8", values: tradingIndicatorSeries(candles, 20), axis: "price" });
  }
  if (tradingIndicatorEnabled("trading-indicator-ma30")) {
    overlays.push({ key: "ma30", label: "MA30", color: "#34d399", values: tradingIndicatorSeries(candles, 30), axis: "price" });
  }
  if (tradingIndicatorEnabled("trading-indicator-ma60")) {
    overlays.push({ key: "ma60", label: "MA60", color: "#a78bfa", values: tradingIndicatorSeries(candles, 60), axis: "price" });
  }
  if (tradingIndicatorEnabled("trading-indicator-ema12")) {
    overlays.push({ key: "ema12", label: "EMA12", color: "#22d3ee", values: tradingIndicatorSeries(candles, 12, "ema"), axis: "price" });
  }
  if (tradingIndicatorEnabled("trading-indicator-ema26")) {
    overlays.push({ key: "ema26", label: "EMA26", color: "#fb7185", values: tradingIndicatorSeries(candles, 26, "ema"), axis: "price" });
  }
  if (tradingIndicatorEnabled("trading-indicator-ema50")) {
    overlays.push({ key: "ema50", label: "EMA50", color: "#f97316", values: tradingIndicatorSeries(candles, 50, "ema"), axis: "price" });
  }
  if (tradingIndicatorEnabled("trading-indicator-bollinger")) {
    const bands = tradingBollingerSeries(candles, 20, 2);
    overlays.push({ key: "bb_upper", label: "BB上", color: "rgba(16, 185, 129, .82)", values: bands.upper, dash: [4, 4], axis: "price" });
    overlays.push({ key: "bb_mid", label: "BB中", color: "rgba(16, 185, 129, .5)", values: bands.middle, axis: "price" });
    overlays.push({ key: "bb_lower", label: "BB下", color: "rgba(16, 185, 129, .82)", values: bands.lower, dash: [4, 4], axis: "price" });
  }
  if (tradingIndicatorEnabled("trading-indicator-rsi14")) {
    oscillators.push({ key: "rsi14", label: "RSI14", color: "#fbbf24", values: tradingRsiSeries(candles, 14), axis: "oscillator" });
  }
  if (tradingIndicatorEnabled("trading-indicator-kd")) {
    const kd = tradingKdSeries(candles, 9, 3, 3);
    oscillators.push({ key: "kd_k", label: "KD-K", color: "#f472b6", values: kd.k, axis: "oscillator" });
    oscillators.push({ key: "kd_d", label: "KD-D", color: "#60a5fa", values: kd.d, axis: "oscillator" });
  }
  return { overlays, oscillators };
}

function drawTradingIndicatorLine(ctx, indicator, candleModels, yForPrice) {
  ctx.save();
  ctx.strokeStyle = indicator.color;
  ctx.lineWidth = 1.45;
  if (indicator.dash) ctx.setLineDash(indicator.dash);
  let drawing = false;
  ctx.beginPath();
  indicator.values.forEach((value, index) => {
    const invalid = indicator?.axis === "oscillator"
      ? (!Number.isFinite(value) || value < 0)
      : (!Number.isFinite(value) || value <= 0);
    if (invalid || !candleModels[index]) {
      drawing = false;
      return;
    }
    const x = candleModels[index].x;
    const y = yForPrice(value);
    if (!drawing) {
      ctx.moveTo(x, y);
      drawing = true;
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
  ctx.restore();
}

function tradingIndicatorValueLabel(indicator, value) {
  if (!Number.isFinite(value)) return "-";
  return indicator?.axis === "oscillator"
    ? `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}`
    : tradingReferenceLabel(value);
}

function tradingIndicatorHasValue(indicator, value) {
  if (!Number.isFinite(value)) return false;
  return indicator?.axis === "oscillator" ? value >= 0 : value > 0;
}

function tradingIndicatorLegend(indicators) {
  const active = indicators
    .filter((item) => item.values.some((value) => tradingIndicatorHasValue(item, value)))
    .map((item) => item.label);
  return active.length ? ` · 指標 ${active.join(" / ")}` : "";
}

function drawTradingOscillatorPanel(ctx, indicators, candleModels, panel, width, pad) {
  if (!indicators.length) return;
  const yForValue = (value) => panel.top + panel.height - ((value - panel.min) / panel.spread) * panel.height;
  ctx.save();
  ctx.strokeStyle = "rgba(148, 163, 184, .18)";
  ctx.lineWidth = 1;
  [0, 20, 50, 80, 100].forEach((level) => {
    const y = yForValue(level);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillStyle = level === 50 ? "#e2e8f0" : "#94a3b8";
    ctx.font = "10px system-ui, sans-serif";
    ctx.fillText(`${level}`, 12, y + 4);
  });
  [
    { value: 70, color: "rgba(248, 113, 113, .55)" },
    { value: 30, color: "rgba(74, 222, 128, .55)" },
  ].forEach((line) => {
    const y = yForValue(line.value);
    ctx.save();
    ctx.strokeStyle = line.color;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.restore();
  });
  indicators.forEach((indicator) => drawTradingIndicatorLine(ctx, indicator, candleModels, yForValue));
  ctx.fillStyle = "#cbd5e1";
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillText("RSI / KD", pad.left, panel.top - 4);
  ctx.restore();
}

function renderTradingReferenceChart(payload, errorText = "") {
  const canvas = $("trading-reference-chart");
  const meta = $("trading-reference-price-meta");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, Math.floor(rect.width || canvas.width || 920));
  const height = Math.max(180, Math.floor(rect.height || canvas.height || 240));
  const ratio = window.devicePixelRatio || 1;
  if (canvas.width !== Math.floor(width * ratio) || canvas.height !== Math.floor(height * ratio)) {
    canvas.width = Math.floor(width * ratio);
    canvas.height = Math.floor(height * ratio);
  }
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const bg = ctx.createLinearGradient(0, 0, 0, height);
  bg.addColorStop(0, "#111827");
  bg.addColorStop(1, "#0b1120");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, width, height);
  const candles = Array.isArray(payload?.candles) ? payload.candles : (Array.isArray(payload?.points) ? payload.points : []);
  if (!candles.length) {
    tradingReferenceChartModel = null;
    hideTradingReferenceTooltip();
    ctx.fillStyle = "#94a3b8";
    ctx.font = "14px system-ui, sans-serif";
    ctx.fillText(errorText || "參考價格讀取中", 18, 34);
    if (meta) meta.textContent = errorText || "公開 API 蠟燭圖載入中；成交引擎會由後端重新取得即時價。";
    return;
  }
  const indicatorGroups = buildTradingReferenceIndicators(candles);
  const overlayIndicators = indicatorGroups.overlays;
  const oscillatorIndicators = indicatorGroups.oscillators;
  const indicators = overlayIndicators.concat(oscillatorIndicators);
  const prices = candles.flatMap((point) => [
    Number(point.high_usdt || point.high_points || point.price_usdt || point.price_points || 0),
    Number(point.low_usdt || point.low_points || point.price_usdt || point.price_points || 0),
  ]).concat(overlayIndicators.flatMap((indicator) => indicator.values)).filter((value) => Number.isFinite(value) && value > 0);
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);
  const spread = Math.max(1, maxPrice - minPrice);
  const pad = { left: 58, right: 18, top: 22, bottom: 34 };
  const chartW = width - pad.left - pad.right;
  const panelGap = oscillatorIndicators.some((indicator) => indicator.values.some((value) => Number.isFinite(value) && value >= 0)) ? 12 : 0;
  const totalChartH = height - pad.top - pad.bottom;
  const oscillatorH = panelGap ? Math.max(72, Math.round(totalChartH * 0.24)) : 0;
  const mainChartH = totalChartH - oscillatorH - panelGap;
  const oscillatorPanel = panelGap ? { top: pad.top + mainChartH + panelGap, height: oscillatorH, min: 0, max: 100, spread: 100 } : null;
  const yForPrice = (price) => pad.top + mainChartH - ((price - minPrice) / spread) * mainChartH;
  ctx.strokeStyle = "rgba(148, 163, 184, .22)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.top + (mainChartH * i / 4);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    const label = tradingReferenceLabel(maxPrice - (spread * i / 4));
    ctx.fillStyle = "#94a3b8";
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillText(label, 8, y + 4);
  }
  const slot = chartW / Math.max(candles.length, 1);
  const bodyW = Math.max(5, Math.min(18, slot * 0.64));
  const candleModels = candles.map((point, index) => {
    const open = Number(point.open_usdt || point.price_usdt || 0);
    const high = Number(point.high_usdt || open);
    const low = Number(point.low_usdt || open);
    const close = Number(point.close_usdt || point.price_usdt || open);
    const x = pad.left + slot * index + slot / 2;
    const yHigh = yForPrice(high);
    const yLow = yForPrice(low);
    const yOpen = yForPrice(open);
    const yClose = yForPrice(close);
    const up = close >= open;
    const color = up ? "#22c55e" : "#ef4444";
    const fillColor = up ? "rgba(34, 197, 94, .82)" : "rgba(239, 68, 68, .86)";
    ctx.strokeStyle = color;
    ctx.fillStyle = fillColor;
    ctx.lineWidth = 1.25;
    ctx.beginPath();
    ctx.moveTo(x, yHigh);
    ctx.lineTo(x, yLow);
    ctx.stroke();
    const bodyTop = Math.min(yOpen, yClose);
    const bodyH = Math.max(2, Math.abs(yOpen - yClose));
    ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, bodyH);
    ctx.strokeRect(x - bodyW / 2, bodyTop, bodyW, bodyH);
    return { ...point, index, open, high, low, close, x, yHigh, yLow, yOpen, yClose, bodyTop, bodyH };
  });
  overlayIndicators.forEach((indicator) => drawTradingIndicatorLine(ctx, indicator, candleModels, yForPrice));
  if (oscillatorPanel) {
    drawTradingOscillatorPanel(ctx, oscillatorIndicators, candleModels, oscillatorPanel, width, pad);
  }
  tradingReferenceChartModel = { payload, candles: candleModels, indicators, pad, width, height, chartW, chartH: mainChartH, slot, oscillatorPanel };
  if (tradingReferenceHoverIndex !== null && candleModels[tradingReferenceHoverIndex]) {
    const hover = candleModels[tradingReferenceHoverIndex];
    ctx.save();
    ctx.strokeStyle = "rgba(248, 250, 252, .72)";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(hover.x, pad.top);
    ctx.lineTo(hover.x, oscillatorPanel ? oscillatorPanel.top + oscillatorPanel.height : height - pad.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.strokeStyle = "rgba(248, 250, 252, .9)";
    ctx.strokeRect(hover.x - bodyW / 2 - 2, hover.bodyTop - 2, bodyW + 4, hover.bodyH + 4);
    ctx.restore();
  }
  const first = candles[0];
  const last = candles[candles.length - 1];
  const lastPrice = Number(last.close_usdt || last.price_usdt || 0);
  ctx.fillStyle = "#e2e8f0";
  ctx.font = "12px system-ui, sans-serif";
  ctx.fillText(`${payload.display_market || payload.symbol || ""} ${tradingReferenceLabel(lastPrice)}`, pad.left, 16);
  ctx.fillStyle = "#94a3b8";
  ctx.fillText(new Date(first.time || Date.now()).toLocaleDateString(), pad.left, height - 10);
  ctx.textAlign = "right";
  ctx.fillText(new Date(last.time || Date.now()).toLocaleDateString(), width - pad.right, height - 10);
  ctx.textAlign = "left";
  if (meta) {
    const open = tradingReferenceLabel(last.open_usdt || last.price_usdt || lastPrice);
    const high = tradingReferenceLabel(last.high_usdt || lastPrice);
    const low = tradingReferenceLabel(last.low_usdt || lastPrice);
    const close = tradingReferenceLabel(last.close_usdt || lastPrice);
    meta.textContent = `${payload.display_market || payload.symbol || "-"} · ${payload.interval || "-"} · Binance 公開 API 蠟燭圖 · 最新 ${close} · O ${open} / H ${high} / L ${low} / C ${close}${tradingIndicatorLegend(indicators)}`;
  }
  if (tradingBotChartOverlay) {
    drawBotChartOverlay(ctx, tradingBotChartOverlay, yForPrice, pad, width, mainChartH, candleModels);
  }
}

function updateTradingReferenceTooltip(event) {
  const model = tradingReferenceChartModel;
  const canvas = $("trading-reference-chart");
  const tooltip = $("trading-reference-tooltip");
  if (!model || !canvas || !tooltip || !model.candles.length) return;
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  if (x < model.pad.left || x > model.width - model.pad.right || y < model.pad.top || y > model.height - model.pad.bottom) {
    hideTradingReferenceTooltip();
    return;
  }
  const index = Math.max(0, Math.min(model.candles.length - 1, Math.floor((x - model.pad.left) / model.slot)));
  const point = model.candles[index];
  if (!point) {
    hideTradingReferenceTooltip();
    return;
  }
  if (tradingReferenceHoverIndex !== index) {
    tradingReferenceHoverIndex = index;
    renderTradingReferenceChart(model.payload);
  }
  tooltip.innerHTML = `
    <strong>${sanitize(tradingReferenceTimeLabel(point, model.payload?.interval || ""))}</strong>
    <span>開 ${sanitize(tradingReferenceLabel(point.open))} · 高 ${sanitize(tradingReferenceLabel(point.high))}</span>
    <span>低 ${sanitize(tradingReferenceLabel(point.low))} · 收 ${sanitize(tradingReferenceLabel(point.close))}</span>
    ${model.indicators?.map((indicator) => {
      const value = indicator.values?.[index];
      return tradingIndicatorHasValue(indicator, value)
        ? `<span>${sanitize(indicator.label)} ${sanitize(tradingIndicatorValueLabel(indicator, value))}</span>`
        : "";
    }).join("") || ""}
  `;
  tooltip.hidden = false;
  const tooltipWidth = tooltip.offsetWidth || 180;
  const tooltipHeight = tooltip.offsetHeight || 70;
  const left = Math.min(Math.max(point.x + 12, 8), Math.max(8, rect.width - tooltipWidth - 8));
  const top = Math.min(Math.max(y - tooltipHeight - 10, 8), Math.max(8, rect.height - tooltipHeight - 8));
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${top}px`;
}

function updateTradingReferenceTooltipFromTouch(event) {
  const touch = event?.touches?.[0] || event?.changedTouches?.[0];
  if (touch) updateTradingReferenceTooltip(touch);
}

function tradingReferenceAutoRefreshMs() {
  return tradingReferencePriceRefreshMs();
}

function tradingReferenceChartLimit(interval) {
  return interval === "1d" ? 90 : 96;
}

function tradingReferenceCandles(payload) {
  return Array.isArray(payload?.candles)
    ? payload.candles
    : (Array.isArray(payload?.points) ? payload.points : []);
}

function tradingReferencePayloadHasCandles(payload) {
  return tradingReferenceCandles(payload).length > 0;
}

function mergeTradingReferenceLatestPayload(currentPayload, latestPayload, maxCandles) {
  const latestCandles = tradingReferenceCandles(latestPayload);
  if (!latestCandles.length) return currentPayload || null;
  const existingCandles = tradingReferenceCandles(currentPayload);
  const mergedCandles = existingCandles.slice();
  latestCandles.forEach((candle) => {
    const candleTime = Number(candle?.time || 0);
    const lastIndex = mergedCandles.length - 1;
    const lastTime = Number(mergedCandles[lastIndex]?.time || 0);
    if (lastIndex >= 0 && candleTime > 0 && candleTime === lastTime) {
      mergedCandles[lastIndex] = candle;
      return;
    }
    if (lastIndex < 0 || !lastTime || candleTime > lastTime) {
      mergedCandles.push(candle);
      return;
    }
    const existingIndex = mergedCandles.findIndex((item) => Number(item?.time || 0) === candleTime);
    if (existingIndex >= 0) mergedCandles[existingIndex] = candle;
  });
  const trimmedCandles = mergedCandles.slice(Math.max(0, mergedCandles.length - maxCandles));
  return {
    ...(currentPayload || {}),
    ...(latestPayload || {}),
    candles: trimmedCandles,
    points: trimmedCandles,
    latest_only: false,
  };
}

function restartTradingReferenceAutoRefresh() {
  if (tradingReferenceAutoTimer) clearInterval(tradingReferenceAutoTimer);
  if (tradingReferenceChartAutoTimer) clearInterval(tradingReferenceChartAutoTimer);
  tradingReferenceAutoTimer = null;
  tradingReferenceChartAutoTimer = null;
  if (!shouldRunTradingFullPolling()) return;
  tradingReferenceAutoTimer = setInterval(async () => {
    if (!shouldRunTradingFullPolling() || tradingReferenceAutoBusy) return;
    tradingReferenceAutoBusy = true;
    try {
      await loadTradingReferencePrices({ silent: true, priceOnly: true });
    } finally {
      tradingReferenceAutoBusy = false;
    }
  }, tradingReferenceAutoRefreshMs());
  tradingReferenceChartAutoTimer = setInterval(async () => {
    if (!shouldRunTradingFullPolling() || tradingReferenceChartAutoBusy) return;
    tradingReferenceChartAutoBusy = true;
    try {
      await loadTradingReferencePrices({ silent: true, latestOnly: true });
    } finally {
      tradingReferenceChartAutoBusy = false;
    }
  }, tradingReferenceChartRefreshMs());
}

async function loadTradingReferencePrices(options = {}) {
  const market = selectedTradingMarket();
  const canvas = $("trading-reference-chart");
  if (!market || !canvas) return;
  const interval = $("trading-reference-interval")?.value || "15m";
  const isPriceOnly = !!options.priceOnly;
  const abortKey = isPriceOnly ? "price" : "chart";
  if (abortKey === "price") {
    if (tradingReferencePriceAbort) tradingReferencePriceAbort.abort();
    tradingReferencePriceAbort = new AbortController();
  } else {
    if (tradingReferenceChartAbort) tradingReferenceChartAbort.abort();
    tradingReferenceChartAbort = new AbortController();
  }
  const signal = abortKey === "price" ? tradingReferencePriceAbort.signal : tradingReferenceChartAbort.signal;
  const hasReusableChart = !!(
    tradingReferencePayloadHasCandles(tradingState.referencePrices)
    && tradingState.referencePrices.market === market.symbol
    && tradingState.referencePrices.interval === interval
  );
  if (!options.silent && !hasReusableChart) {
    renderTradingReferenceChart(null, "參考價格讀取中");
  } else if (!options.silent && hasReusableChart && $("trading-reference-price-meta")) {
    $("trading-reference-price-meta").textContent = "正在更新參考價格，保留上一張蠟燭圖。";
  }
  try {
    const maxCandles = tradingReferenceChartLimit(interval);
    const canPatchLatest = !!(
      options.latestOnly
      && tradingReferencePayloadHasCandles(tradingState.referencePrices)
      && tradingState.referencePrices.market === market.symbol
      && tradingState.referencePrices.interval === interval
    );
    const latestOnly = !!(isPriceOnly || canPatchLatest);
    const limit = latestOnly ? 1 : maxCandles;
    const latestParam = latestOnly ? "&latest=1" : "";
    const json = await fetchTradingJson(`/trading/reference-prices?market=${encodeURIComponent(tradingMarketRequestSymbol(market.symbol))}&interval=${encodeURIComponent(interval)}&limit=${limit}${latestParam}`, {
      signal,
    });
    const responseCandles = tradingReferenceCandles(json);
    const referenceContext = json.price_context && typeof json.price_context === "object" ? json.price_context : null;
    let nextPayload = null;
    if (!isPriceOnly) {
      nextPayload = latestOnly
        ? mergeTradingReferenceLatestPayload(tradingState.referencePrices, json, maxCandles)
        : json;
      if (tradingReferencePayloadHasCandles(nextPayload)) {
        tradingState.referencePrices = nextPayload;
      } else if (!hasReusableChart) {
        renderTradingReferenceChart(null, "Binance 參考價格暫無有效資料");
      }
    }
    const last = responseCandles[responseCandles.length - 1] || null;
    if (last && last.close_points) {
      // Reference-price polling is for the chart only. The trading card's
      // "current price" must keep using the live/fused market price returned
      // by the trading dashboard, otherwise single-source reference candles can
      // visually overwrite the real execution reference price.
      if ($("trading-reference-price-meta")) {
        const providerLabel = Number.isFinite(Number(json.provider_count)) ? ` · 來源 ${Number(json.provider_count)} 家` : "";
        const staleLabel = json.stale ? " · stale" : "";
        const degradedLabel = json.degraded ? " · degraded" : "";
        const contextLabel = referenceContext ? ` · ${tradingPriceContextSummary(referenceContext, { compact: true })}` : "";
        $("trading-reference-price-meta").textContent = `reference price：${json.display_market || json.market || market.symbol} · ${json.interval || interval} · ${json.source || "reference_price"} · 信心 ${tradingPriceConfidenceLabel(json.confidence)}${providerLabel}${staleLabel}${degradedLabel} · 最新收盤 ${Number(last.close_points || 0)}${contextLabel}`;
      }
    }
    if (!isPriceOnly && tradingReferencePayloadHasCandles(tradingState.referencePrices)) {
      renderTradingReferenceChart(tradingState.referencePrices);
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    if (!isPriceOnly && !options.silent) {
      if (tradingReferencePayloadHasCandles(tradingState.referencePrices)) {
        renderTradingReferenceChart(tradingState.referencePrices);
        if ($("trading-reference-price-meta")) {
          $("trading-reference-price-meta").textContent = `參考價格更新失敗，已保留上一張蠟燭圖：${err.message || "Binance 參考價格讀取失敗"}`;
        }
      } else {
        renderTradingReferenceChart(null, err.message || "Binance 參考價格讀取失敗");
      }
    }
  }
}

function renderTradingOrders(rows, targetId = "trading-order-list", allowCancel = true) {
  const list = $(targetId);
  if (!list) return;
  if (!rows || !rows.length) {
    list.innerHTML = `<div class="drive-empty">尚無訂單</div>`;
    return;
  }
  list.innerHTML = rows.map((row) => {
    const canCancel = row.status === "open" || row.status === "partially_filled";
    const price = row.order_type === "limit" ? row.limit_price_points : row.execution_price_points;
    const botTag = row.bot_name ? `<span class="trading-bot-tag">🤖 ${sanitize(row.bot_name)}</span>` : "";
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(row.side)} · ${sanitize(row.order_type)} · ${sanitize(row.status)}${botTag ? ` ${botTag}` : ""}</strong>
          <div class="drive-card-sub">${sanitize(tradingDisplaySymbol(row.market_symbol))} · 數量 ${sanitize(row.quantity)} · 價格 ${sanitize(price || "-")} · 凍結 ${Number(row.frozen_points || 0)}</div>
          <div class="economy-ledger-hash">${sanitize(row.order_uuid || "")}</div>
        </div>
        ${allowCancel && canCancel ? `<button class="btn" type="button" data-trading-cancel="${sanitize(row.order_uuid || "")}">取消</button>` : ""}
      </div>
    `;
  }).join("");
  list.querySelectorAll("[data-trading-cancel]").forEach((btn) => {
    bindTradingActionButton(btn, () => cancelTradingOrder(btn.dataset.tradingCancel || ""), "正在取消訂單...", "取消訂單失敗");
  });
}

function applyTradingOrderResult(order) {
  if (!order?.order_uuid) return;
  const rows = Array.isArray(tradingState.orders) ? tradingState.orders.slice() : [];
  const index = rows.findIndex((row) => row.order_uuid === order.order_uuid);
  if (index >= 0) rows[index] = { ...rows[index], ...order };
  else rows.unshift(order);
  tradingState.orders = rows.slice(0, 50);
  renderTradingOrders(tradingState.orders);
}

function renderTradingFills(rows, targetId = "trading-fill-list") {
  const list = $(targetId);
  if (!list) return;
  if (!rows || !rows.length) {
    list.innerHTML = `<div class="drive-empty">尚無成交</div>`;
    return;
  }
  list.innerHTML = rows.map((row) => {
    const isMargin = String(row.record_type || "").startsWith("margin_");
    const pnl = row.realized_pnl_points == null ? null : Number(row.realized_pnl_points || 0);
    const interest = Number(row.interest_points || 0);
    const paidProfit = Number(row.paid_profit_points || 0);
    const pendingProfit = Number(row.pending_profit_points || 0);
    const extra = isMargin
      ? `${pnl == null ? "" : ` · 損益 ${pnl >= 0 ? "+" : ""}${pnl} 點`}${interest ? ` · 利息 ${interest} 點` : ""}${paidProfit ? ` · 已支付盈利 ${paidProfit} 點` : ""}${pendingProfit ? ` · 未結盈利 ${pendingProfit} 點` : ""}`
      : "";
    const governanceLine = pendingProfit && row.governance_proposal_uuid
      ? `<div class="drive-card-sub">短缺治理提案 ${sanitize(row.governance_proposal_uuid || "")}</div>`
      : "";
    const botTag = row.bot_name ? `<span class="trading-bot-tag">🤖 ${sanitize(row.bot_name)}</span>` : "";
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(row.side)} · ${sanitize(tradingDisplaySymbol(row.market_symbol))} · ${sanitize(row.quantity)}${botTag ? ` ${botTag}` : ""}</strong>
          <div class="drive-card-sub">${isMargin ? "進階交易" : "現貨成交"} · 價格 ${Number(row.price_points || 0) || "-"} · 成交 ${Number(row.notional_points || 0)} 點 · 手續費 ${Number(row.fee_points || 0)}${extra}</div>
          ${governanceLine}
          <div class="drive-card-sub">${sanitize(row.created_at || "")}</div>
          ${row.position_uuid ? `<div class="economy-ledger-hash">${sanitize(row.position_uuid || "")}</div>` : ""}
        </div>
      </div>
    `;
  }).join("");
}

function renderTradingBotPositionCard() {
  const list = $("trading-bot-position-list");
  if (!list) return;
  const bots = Array.isArray(tradingState.bots) ? tradingState.bots : [];
  const gridBots = typeof tradingGridBots !== "undefined" && Array.isArray(tradingGridBots) ? tradingGridBots : [];
  const runs = Array.isArray(tradingState.botRuns) ? tradingState.botRuns : [];
  const botOrders = (Array.isArray(tradingState.orders) ? tradingState.orders : []).filter((row) => row.bot_name || row.bot_uuid || row.grid_bot_uuid);
  const gridOpenOrders = gridBots.flatMap((bot) => (Array.isArray(bot.orders) ? bot.orders : [])
    .filter((order) => order.status === "open")
    .map((order) => ({ ...order, bot_name: bot.name || "網格機器人", market_symbol: bot.market_symbol, bot_uuid: bot.bot_uuid, grid_order: true })));
  const botFills = (Array.isArray(tradingState.fills) ? tradingState.fills : []).filter((row) => row.bot_name || row.bot_uuid || row.grid_bot_uuid);
  const recentRuns = runs.filter((row) => row.status === "triggered" || row.order_uuid).slice(0, 20);
  const enabledCount = bots.filter((bot) => bot.enabled).length + gridBots.filter((bot) => bot.enabled).length;
  const openOrderCount = botOrders.filter((row) => row.status === "open" || row.status === "partially_filled").length + gridOpenOrders.length;
  const lockedPoints = botOrders.reduce((sum, row) => sum + Number(row.frozen_points || 0), 0)
    + gridOpenOrders.reduce((sum, row) => sum + Number(row.frozen_points || row.order_amount_points || 0), 0);
  tradingSetText("trading-bot-position-count", formatTradingPointsValue(bots.length + gridBots.length));
  tradingSetText("trading-bot-position-enabled", `啟用 ${formatTradingPointsValue(enabledCount)}`);
  tradingSetText("trading-bot-open-order-count", formatTradingPointsValue(openOrderCount));
  tradingSetText("trading-bot-open-order-locked", `凍結 ${formatTradingPointsValue(lockedPoints)}`);
  tradingSetText("trading-bot-recent-run-count", formatTradingPointsValue(recentRuns.length));

  const rows = [];
  const botSummary = [...bots, ...gridBots].slice(0, 12);
  if (botSummary.length) {
    rows.push(`<div class="drive-card-sub" style="font-weight:600;">已建立機器人</div>`);
    botSummary.forEach((bot) => {
      const isGrid = bot.grid_count !== undefined || Array.isArray(bot.orders);
      const kind = isGrid ? "網格" : (bot.bot_type_label || (bot.bot_type === "dca" ? "定投" : "Workflow"));
      const symbol = tradingDisplaySymbol(bot.market_symbol || "");
      const budget = isGrid
        ? `每格 ${formatTradingPointsValue(bot.order_amount_points || 0)} 點`
        : tradingBotBudgetText(bot);
      const actionHtml = isGrid
        ? `
          <button class="btn btn-sm" type="button" data-grid-toggle="${sanitize(bot.bot_uuid || "")}" data-grid-enabled="${bot.enabled ? "0" : "1"}">${bot.enabled ? "暫停" : "啟用"}</button>
          <button class="btn btn-sm btn-danger" type="button" data-grid-delete="${sanitize(bot.bot_uuid || "")}">停止</button>
        `
        : `
          <button class="btn btn-sm" type="button" data-trading-bot-toggle="${sanitize(bot.bot_uuid || "")}" data-trading-bot-enabled="${bot.enabled ? "0" : "1"}">${bot.enabled ? "暫停" : "啟用"}</button>
          <button class="btn btn-sm btn-danger" type="button" data-trading-bot-delete="${sanitize(bot.bot_uuid || "")}">停止</button>
        `;
      rows.push(`<div class="drive-file-row">
        <div>
          <strong>${sanitize(kind)} · ${sanitize(bot.name || "未命名機器人")} · ${sanitize(symbol)}</strong>
          <div class="drive-card-sub">狀態 ${bot.enabled ? "啟用" : "停用"} · ${sanitize(budget)}${bot.last_error ? ` · <span class="negative">上次錯誤：${sanitize(bot.last_error)}</span>` : ""}</div>
        </div>
        <div class="drive-file-actions">${actionHtml}</div>
      </div>`);
    });
  }

  const openRows = [
    ...botOrders.filter((row) => row.status === "open" || row.status === "partially_filled"),
    ...gridOpenOrders,
  ].slice(0, 20);
  if (openRows.length) {
    rows.push(`<div class="drive-card-sub" style="font-weight:600;margin-top:.45rem;">機器人掛單</div>`);
    openRows.forEach((row) => {
      const side = row.side === "sell" ? "賣出" : "買入";
      const price = row.limit_price_points || row.price_points || row.execution_price_points || "-";
      const priceLabel = Number.isFinite(Number(price)) ? formatTradingPointsValue(price) : "-";
      const uuid = row.order_uuid || row.trading_order_uuid || row.grid_order_uuid || row.bot_uuid || "";
      rows.push(`<div class="drive-file-row">
        <div>
          <strong>${sanitize(row.bot_name || "機器人")} · ${sanitize(side)} · ${sanitize(row.status || "open")}</strong>
          <div class="drive-card-sub">${sanitize(tradingDisplaySymbol(row.market_symbol || ""))} · 價格 ${sanitize(priceLabel)} · 凍結 ${formatTradingPointsValue(row.frozen_points || row.order_amount_points || 0)}</div>
          <div class="economy-ledger-hash">${sanitize(String(uuid).slice(0, 32))}</div>
        </div>
      </div>`);
    });
  }

  if (botFills.length || recentRuns.length) {
    rows.push(`<div class="drive-card-sub" style="font-weight:600;margin-top:.45rem;">近期 Bot 成交 / 觸發</div>`);
    botFills.slice(0, 10).forEach((row) => {
      const side = row.side === "sell" ? "賣出" : "買入";
      rows.push(`<div class="drive-file-row">
        <div>
          <strong>${sanitize(row.bot_name || "機器人")} · ${sanitize(side)} · ${sanitize(tradingDisplaySymbol(row.market_symbol || ""))}</strong>
          <div class="drive-card-sub">數量 ${sanitize(row.quantity || "0")} · 成交 ${formatTradingPointsValue(row.notional_points || 0)} 點 · 手續費 ${formatTradingPointsValue(row.fee_points || 0)} · ${sanitize(row.created_at || "")}</div>
        </div>
      </div>`);
    });
    recentRuns.slice(0, 10).forEach((row) => {
      if (botFills.some((fill) => fill.order_uuid && fill.order_uuid === row.order_uuid)) return;
      rows.push(`<div class="drive-file-row">
        <div>
          <strong>${sanitize(row.status || "triggered")} · ${sanitize(tradingDisplaySymbol(row.market_symbol || ""))}</strong>
          <div class="drive-card-sub">觀測價 ${formatTradingPointsValue(row.observed_price_points || 0)} · ${sanitize(row.created_at || "")}${row.order_uuid ? ` · 訂單 ${sanitize(String(row.order_uuid).slice(0, 32))}` : ""}</div>
        </div>
      </div>`);
    });
  }

  list.innerHTML = rows.length ? rows.join("") : `<div class="drive-empty">尚無機器人訂單或掛單</div>`;
  list.querySelectorAll("[data-trading-bot-toggle]").forEach((btn) => {
    bindTradingActionButton(
      btn,
      () => toggleTradingBot(btn.dataset.tradingBotToggle || "", btn.dataset.tradingBotEnabled === "1"),
      btn.dataset.tradingBotEnabled === "1" ? "準備啟用交易機器人..." : "準備暫停交易機器人...",
      "交易機器人狀態更新失敗"
    );
  });
  list.querySelectorAll("[data-trading-bot-delete]").forEach((btn) => {
    bindTradingActionButton(btn, () => deleteTradingBot(btn.dataset.tradingBotDelete || ""), "準備停止交易機器人...", "交易機器人停止失敗");
  });
  list.querySelectorAll("[data-grid-toggle]").forEach((btn) => {
    bindTradingActionButton(
      btn,
      () => toggleGridBot(btn.dataset.gridToggle || "", btn.dataset.gridEnabled === "1"),
      btn.dataset.gridEnabled === "1" ? "準備啟用網格機器人..." : "準備暫停網格機器人...",
      "網格機器人狀態更新失敗"
    );
  });
  list.querySelectorAll("[data-grid-delete]").forEach((btn) => {
    bindTradingActionButton(btn, () => deleteGridBot(btn.dataset.gridDelete || ""), "正在停止網格機器人...", "網格機器人停止失敗");
  });
  const managerBtn = $("trading-open-bot-manager-btn");
  if (managerBtn && managerBtn.dataset.bound !== "1") {
    managerBtn.dataset.bound = "1";
    managerBtn.addEventListener("click", () => openTradingBotPanel("mybots"));
  }
}

function renderTradingContracts(rows = []) {
  const list = $("trading-contract-position-list");
  if (!list) return;
  const contracts = rows.filter((row) => row.status === "open");
  if (!contracts.length) {
    list.innerHTML = `<div class="drive-empty">尚無 root 合約持倉</div>`;
    return;
  }
  list.innerHTML = contracts.map((row) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(row.side || "-")} · ${sanitize(tradingDisplaySymbol(row.market_symbol || "-"))} · ${sanitize(row.quantity || "0")}</strong>
        <div class="drive-card-sub">入場 ${Number(row.entry_price_points || 0)} 點 · 槓桿 ${Number(row.leverage || 1)}x · 保證金 ${Number(row.margin_points || 0)} 點</div>
        <div class="economy-ledger-hash">${sanitize(row.position_uuid || "")}</div>
      </div>
      <button class="btn" type="button" data-contract-close="${sanitize(row.position_uuid || "")}">平倉</button>
    </div>
  `).join("");
  bindTradingContractAssetActions(list);
}

function renderTradingRootSimulationPositions() {
  if (currentUser !== "root") return;
  const summary = tradingPortfolioSummary();
  const spotRows = summary.spot.rows || [];
  const marginRows = summary.marginRows || [];
  const futuresRows = summary.futuresRows || [];
  tradingSetText("trading-root-sim-spot-count", `${formatTradingPointsValue(spotRows.length)} 個市場`);
  tradingSetText(
    "trading-root-sim-spot-detail",
    `估值 ${formatTradingPointsValue(summary.spot.riskValue)} 點 · 未實現 ${tradingSignedPointsText(summary.spot.unrealizedPnl)} · 手續費 ${formatTradingPointsValue(summary.spot.totalFees)} 點`,
  );
  tradingSetText("trading-root-sim-margin-count", `${formatTradingPointsValue(marginRows.length)} 筆`);
  tradingSetText(
    "trading-root-sim-margin-detail",
    `權益 ${formatTradingPointsValue(summary.marginEquity)} 點 · 借款 ${formatTradingPointsValue(summary.marginBorrowed)} 點 · 未實現 ${tradingSignedPointsText(summary.marginPnl)}`,
  );
  tradingSetText("trading-root-sim-contract-count", `${formatTradingPointsValue(futuresRows.length)} 筆`);
  tradingSetText(
    "trading-root-sim-contract-detail",
    `權益 ${formatTradingPointsValue(summary.futuresEquity)} 點 · 未實現 ${tradingSignedPointsText(summary.futuresPnl)}`,
  );

  const spotList = $("trading-root-sim-spot-list");
  if (spotList) {
    spotList.innerHTML = spotRows.length
      ? spotRows.map((row) => tradingPortfolioSpotRow(row)).join("")
      : `<div class="drive-empty">尚無 root 現貨模擬倉位</div>`;
    bindTradingSpotAssetActions(spotList);
  }
  const marginList = $("trading-root-sim-margin-list");
  if (marginList) {
    marginList.innerHTML = marginRows.length
      ? marginRows.map((row) => tradingPortfolioMarginRow(row)).join("")
      : `<div class="drive-empty">尚無 root 借貸模擬倉位</div>`;
    bindTradingMarginAssetActions(marginList);
  }
}

function updateTradingMarginEstimate() {
  const market = tradingState.markets.find((row) => row.symbol === ($("trading-margin-market-select")?.value || "")) || selectedTradingMarket();
  const estimate = $("trading-margin-estimate");
  const openBtn = $("trading-margin-open-btn");
  if (!estimate || !market) return { ok: false, blocking: true, message: "沒有可用進階交易市場" };
  const quantity = tradingNumber($("trading-margin-quantity")?.value, 0);
  const collateral = tradingNumber($("trading-margin-collateral")?.value, 0);
  const riskContext = tradingMarketPriceContext(market, "risk_grade");
  const borrowingPausePolicy = tradingPriceDegradePolicy(riskContext, "borrowing");
  const priceConfidenceOnlyWarns = tradingState.settings?.disable_price_confidence_gates !== false || !!tradingState.settings?.dev_disable_price_confidence_gates;
  const riskGradePrice = tradingMarketPricePoints(market, "risk_grade");
  const referencePrice = tradingMarketPricePoints(market, "reference");
  const price = riskGradePrice > 0 ? riskGradePrice : (borrowingPausePolicy.shouldPause ? riskGradePrice : referencePrice);
  const notional = quantity > 0 && price > 0 ? Math.ceil(quantity * price) : 0;
  const positionType = $("trading-margin-type")?.value || "margin_long";
  const marginLongFinancingRatePercent = tradingNumber(tradingState.settings?.margin_long_financing_percent, 90);
  const shortCollateralRatePercent = tradingNumber(tradingState.settings?.short_collateral_percent, 60);
  const feeRatePercent = tradingNumber(market.fee_rate_percent, 0);
  const maintenancePercent = tradingNumber(tradingState.settings?.margin_maintenance_percent, 15);
  const baseMinCollateral = positionType === "short"
    ? Math.ceil(notional * shortCollateralRatePercent / 100)
    : Math.ceil(notional * Math.max(0, 100 - marginLongFinancingRatePercent) / 100);
  const safetyMinCollateral = Math.ceil(notional * Math.max(0, maintenancePercent + feeRatePercent) / 100) + 2;
  const minCollateral = Math.max(baseMinCollateral, safetyMinCollateral);
  const minimumBorrowUnitPoints = 1;
  const maxLongCollateral = Math.max(0, notional - minimumBorrowUnitPoints);
  const feeMicropoints = tradingFeeMicropoints(notional, feeRatePercent);
  const fee = tradingMicropointsToSettlementPoints(feeMicropoints);
  const feeExact = feeMicropoints / TRADING_POINT_MICRO_SCALE;
  const available = tradingNumber(tradingState.funding?.available_points, 0);
  const upfrontRequired = collateral;
  const maxCollateralFromAvailable = Math.max(0, available);
  const principal = positionType === "short" ? notional : Math.max(0, notional - collateral);
  const fundingPool = tradingState.fundingPool || {};
  const poolAvailable = tradingNumber(fundingPool.available_points, 0);
  const borrowGroup = tradingBorrowAprGroupForMarket(market, positionType);
  const poolApr = tradingBorrowEffectiveAprPercent(
    borrowGroup,
    tradingNumber(fundingPool.projected_utilization_percent, fundingPool.utilization_percent)
  );
  const timing = tradingBorrowTimingSummary();
  const typeLabel = positionType === "short" ? "借券放空" : "融資買入";
  if (!quantity || !collateral || !notional) {
    estimate.textContent = "輸入數量與保證金後顯示預估風險。";
    estimate.style.color = "var(--muted)";
    if (openBtn) openBtn.disabled = true;
    return { ok: false, blocking: true, message: estimate.textContent };
  }
  let blocking = false;
  if (!priceConfidenceOnlyWarns && borrowingPausePolicy.shouldPause) {
    const message = `${tradingPriceDegradePauseMessage(typeLabel, riskContext, borrowingPausePolicy)} · ${tradingPriceContextSummary(riskContext, { compact: true })}`;
    blocking = true;
    estimate.textContent = message;
    estimate.style.color = "#ff6b7a";
    if (openBtn) openBtn.setAttribute("aria-disabled", "true");
    return { ok: false, blocking, message };
  }
  const priceLabel = riskGradePrice > 0 ? "風控級價格" : "reference 價格";
  const contextForMessage = riskGradePrice > 0 ? riskContext : tradingMarketPriceContext(market, "reference");
  const baseRuleText = positionType === "short"
    ? `借券保證金 ${formatTradingPercent(shortCollateralRatePercent)}% 底線 ${baseMinCollateral} 點`
    : `融資自備 ${formatTradingPercent(Math.max(0, 100 - marginLongFinancingRatePercent))}% 底線 ${baseMinCollateral} 點`;
  const safetyRuleText = `維持率 + 費率安全底線 ${safetyMinCollateral} 點`;
  let message = `${typeLabel} · ${priceLabel} ${formatTradingPointsValue(price)} 點 · ${tradingPriceContextSummary(contextForMessage, { compact: true })} · 名目金額約 ${notional} 點 · 預估開倉手續費 ${formatTradingPointsValue(feeExact)} 點（結算時合併進位，約 ${fee} 點）· 原始保證金最低需求 ${minCollateral} 點（${baseRuleText}；${safetyRuleText}）· 目前填寫保證金 ${collateral} 點 · 實際預扣 ${upfrontRequired} 點`;
  if (positionType === "short") {
    message = `${message}；借券放空風險：價格上漲會虧損並降低維持率；借券保證金比例 ${formatTradingPercent(shortCollateralRatePercent)}%`;
  } else {
    message = `${message}；融資可貸比例 ${formatTradingPercent(marginLongFinancingRatePercent)}%`;
  }
  if (positionType === "margin_long" && maxLongCollateral < minCollateral) {
    message = `${message}；這筆名目金額太小，扣除至少借 1 點後，已無法同時滿足最低原始保證金需求。請提高買入數量，或改用現貨買入。`;
    blocking = true;
  } else if (positionType === "margin_long" && collateral >= notional) {
    message = `${message}；你填寫的保證金已超過本次買入名目金額，這不屬於融資交易。請改用現貨買入；若要融資，保證金需介於 ${minCollateral}～${maxLongCollateral} 點之間，且至少要借 1 點。`;
    blocking = true;
  } else if (collateral < minCollateral) {
    message = `${message}；原始保證金不足，至少需要 ${minCollateral} 點。若要融資，保證金需介於 ${minCollateral}～${maxLongCollateral} 點之間。`;
    blocking = true;
  } else if (upfrontRequired > available) {
    if (maxCollateralFromAvailable >= minCollateral) {
      message = `${message}；可用資金不足：本欄位最多可填 ${maxCollateralFromAvailable} 點保證金（手續費會在平倉 / 清算時合併結算）。`;
    } else {
      message = `${message}；可用資金不足：最多只能填 ${maxCollateralFromAvailable} 點保證金，低於最低需求 ${minCollateral} 點；目前可用 ${available} 點。`;
    }
    blocking = true;
  } else if (principal > poolAvailable && currentUser !== "root") {
    message = `${message}；借貸基金可借餘額不足，需要借出 ${principal} 點，目前可借 ${poolAvailable} 點`;
    blocking = true;
  } else if (tradingState.settings?.borrowing_enabled) {
    const borrowGroupLabel = borrowGroup === "btc_eth" ? "BTC / ETH" : "USDT / 積分";
    message = `${message}；預估借出本金 ${principal} 點，目前浮動年利率約 ${formatTradingPercent(poolApr)}% APR（${borrowGroupLabel}）`;
    if (timing.text) message = `${message}；${timing.text}`;
  }
  estimate.textContent = message;
  estimate.style.color = blocking ? "#ff6b7a" : "var(--muted)";
  if (openBtn) {
    openBtn.disabled = !tradingState.settings?.borrowing_enabled;
    openBtn.setAttribute("aria-disabled", blocking ? "true" : "false");
  }
  return { ok: !blocking, blocking, message };
}

function renderTradingMarginPositions(rows = []) {
  const list = $("trading-margin-position-list");
  if (!list) return;
  const openRows = rows.filter((row) => row.status === "open");
  if (!openRows.length) {
    list.innerHTML = `<div class="drive-empty">尚無進階交易倉位</div>`;
    return;
  }
  list.innerHTML = openRows.map((row) => tradingMarginPositionRow(row)).join("");
  bindTradingMarginAssetActions(list);
}

function renderTradingMarginAccountSummary(summary = null) {
  const wrap = $("trading-margin-account-summary");
  if (!wrap) return;
  const data = (summary && typeof summary === "object" && Object.keys(summary).length)
    ? summary
    : tradingLiveMarginSummary(tradingState.marginPositions || []);
  if (normalizeTradingPage(tradingActivePage) !== "my-positions") {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "";
  const ratio = data.cross_margin_ratio_percent ?? data.maintenance_ratio_percent;
  if ($("trading-margin-cross-ratio")) {
    $("trading-margin-cross-ratio").textContent = !data.open_count ? "無開倉" : (ratio == null ? "無法計算" : `${formatTradingPointsValue(ratio)}%`);
  }
  if ($("trading-margin-cross-status")) $("trading-margin-cross-status").textContent = !data.open_count ? "尚無借貸倉位" : (data.reason || "整戶維持率正常");
  if ($("trading-margin-account-equity")) $("trading-margin-account-equity").textContent = `${formatTradingPointsValue(data.account_equity_points || 0)} 點`;
  if ($("trading-margin-free-margin")) $("trading-margin-free-margin").textContent = `${formatTradingPointsValue(data.free_margin_points || 0)} 點`;
  if ($("trading-margin-available-margin")) $("trading-margin-available-margin").textContent = `維持後可用 ${formatTradingPointsValue(data.available_margin_points || 0)} 點`;
  if ($("trading-margin-total-borrowed")) $("trading-margin-total-borrowed").textContent = `${formatTradingPointsValue(data.total_borrowed_points || 0)} 點`;
  if ($("trading-margin-maintenance-total")) $("trading-margin-maintenance-total").textContent = `總維持需求 ${formatTradingPointsValue(data.total_maintenance_requirement_points || data.total_maintenance_points || 0)} 點`;
}

// Trading bot, workflow, backtest, competition, and grid-bot UI live in 56-trading-bots.js.

function renderTradingRootReport(report) {
  const safe = report && typeof report === "object" ? report : {};
  const reserve = safe.reserve_pool || {};
  const verification = safe.verification || {};
  tradingState.fundingPool = safe.funding_pool || tradingState.fundingPool || null;
  if ($("trading-reserve-balance")) $("trading-reserve-balance").textContent = String(Number(reserve.balance_points || 0));
  if ($("trading-verification-status")) $("trading-verification-status").textContent = verification.ok === false ? "異常" : "正常";
  if ($("trading-verification-detail")) $("trading-verification-detail").textContent = `${Array.isArray(verification.errors) ? verification.errors.length : 0} 個問題`;
  const settings = safe.settings || {};
  if ($("trading-risk-flags")) {
    const degradePauses = [];
    if (settings.price_degrade_pause_market_orders) degradePauses.push("市價");
    if (settings.price_degrade_pause_bots) degradePauses.push("bot");
    if (settings.price_degrade_pause_borrowing) degradePauses.push("借貸");
    $("trading-risk-flags").textContent = `borrow=${settings.borrowing_enabled ? "true" : "false"} / liquidation=${settings.margin_liquidation_enabled ? "true" : "false"} / futures=${settings.futures_enabled ? "true" : "false"} / pvp=${settings.pvp_matching_enabled ? "true" : "false"} / degrade_pause=${degradePauses.join("+") || "off"}`;
  }
  if ($("trading-liquidation-status")) {
    $("trading-liquidation-status").textContent = settings.margin_liquidation_enabled
      ? `自動清算排程：啟用，維持保證金 ${formatTradingPercent(settings.margin_maintenance_percent || 0)}%`
      : "自動清算排程：停用";
  }
  if ($("trading-root-sim-balance")) {
    const funding = tradingState.funding || {};
    $("trading-root-sim-balance").textContent = funding.mode === "root_simulated" ? String(Number(funding.available_points || 0)) : "10000";
  }
  const markets = Array.isArray(safe.markets) ? safe.markets : tradingState.markets;
  tradingState.markets = markets;
  renderTradingMarketOptions();
  populateTradingRootMarketForm();
  const auditList = $("trading-audit-list");
  if (auditList) {
    const rows = Array.isArray(safe.audit_events) ? safe.audit_events : [];
    auditList.innerHTML = rows.length ? rows.map((row) => `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(row.event_type || "-")} · ${sanitize(row.severity || "-")}</strong>
          <div class="drive-card-sub">${sanitize(row.market_symbol || "-")} · ${sanitize(row.created_at || "")}</div>
          <div class="drive-card-sub">${sanitize(row.message || "")}</div>
        </div>
      </div>
    `).join("") : `<div class="drive-empty">尚無交易審計</div>`;
  }
  const eventList = $("trading-reserve-event-list");
  if (eventList) {
    const rows = Array.isArray(safe.reserve_events) ? safe.reserve_events : [];
    eventList.innerHTML = rows.length ? rows.map((row) => `
      <div class="drive-file-row">
        <div>
          <strong>${Number(row.delta_points || 0)} 點 · ${sanitize(row.event_type || "-")}</strong>
          <div class="drive-card-sub">餘額 ${Number(row.balance_after || 0)} · ${sanitize(row.reason || "-")} · ${sanitize(row.created_at || "")}</div>
        </div>
      </div>
    `).join("") : `<div class="drive-empty">尚無基金事件</div>`;
  }
}

function populateTradingRootMarketForm() {
  const symbol = $("trading-root-market-select")?.value || "";
  const market = tradingState.markets.find((row) => row.symbol === symbol) || tradingState.markets[0];
  if (!market) return;
  if ($("trading-root-market-select")) $("trading-root-market-select").value = market.symbol;
  if ($("trading-root-price")) $("trading-root-price").value = Number(market.manual_price_points || 0);
  if ($("trading-root-jump-percent")) $("trading-root-jump-percent").value = formatTradingPercent(market.max_price_jump_percent || 0);
  if ($("trading-root-fee-percent")) $("trading-root-fee-percent").value = formatTradingPercent(market.fee_rate_percent || 0);
  if ($("trading-root-min-order")) $("trading-root-min-order").value = Number(market.min_order_points || 1);
  if ($("trading-root-max-order")) $("trading-root-max-order").value = Number(market.max_order_points || 1);
  if ($("trading-root-enabled")) $("trading-root-enabled").checked = !!market.enabled;
}

async function loadTradingDashboard() {
  const operation = tradingOperationContext();
  const loadGeneration = ++tradingDashboardLoadGeneration;
  if (!currentUser || !canAccessModule("economy")) return;
  ensureTradingAccountScope();
  const tradingEnabled = !siteConfig || siteConfig.feature_trading_enabled !== false;
  const card = $("trading-card");
  const rootCard = $("trading-root-card");
  if (card && !tradingEnabled) {
    card.style.display = "none";
  }
  if (rootCard) rootCard.style.display = tradingEnabled && currentUser === "root" ? "" : "none";
  if (!tradingEnabled) return;
  if (card) card.style.display = "";
  try {
    await loadTradingWorkflowTemplates();
    tradingAssertDashboardLoadCurrent(operation, loadGeneration);
    const json = await fetchTradingJson(`/trading/dashboard${tradingSourceWalletQuery()}`);
    tradingAssertDashboardLoadCurrent(operation, loadGeneration);
    const payload = json.trading || {};
    tradingState.funding = payload.funding || null;
    tradingState.fundingPool = payload.funding_pool || null;
    tradingState.wallets = payload.wallets || [];
    tradingState.spotSummary = payload.spot_summary || null;
    tradingState.marginSummary = payload.margin_summary || null;
    tradingState.markets = payload.markets || [];
    tradingState.settings = payload.settings || {};
    tradingState.positions = payload.positions || [];
    tradingState.marginPositions = payload.margin_positions || [];
    tradingState.futuresPositions = payload.futures_positions || [];
    tradingState.orders = payload.orders || [];
    tradingState.fills = payload.fills || [];
    tradingState.bots = payload.bots || [];
    tradingState.botRuns = payload.bot_runs || [];
    const state = payload.state || {};
    tradingState.state = state;
    const status = $("trading-safe-mode");
    if (status) {
      status.textContent = state.safe_mode ? `交易 safe mode：${state.reason || "已啟用"}` : "交易引擎正常";
      status.style.color = state.safe_mode ? "#ffb74d" : "var(--muted)";
    }
    renderTradingPaymentWalletOptions(tradingState.wallets, tradingState.funding?.selected_wallet_address || tradingState.funding?.active_wallet_address || "");
    renderTradingMarketOptions();
    loadTradingPersonalFormState();
    renderTradingSummary();
    syncTradingSubpages();
    loadTradingLivePrice().catch((err) => {
      if (tradingOperationIsCurrent(operation) && loadGeneration === tradingDashboardLoadGeneration && !tradingIsAbortError(err)) {
        tradingSetBackgroundStatus(tradingFriendlyErrorText(err?.message || "即時價格讀取失敗"), false);
      }
    });
    renderTradingOrders(tradingState.orders);
	    renderTradingFills(tradingState.fills);
	    renderTradingBots(tradingState.bots, tradingState.botRuns);
	    renderTradingBotPositionCard();
	    loadGridBots().catch((err) => {
	      if (tradingOperationIsCurrent(operation) && loadGeneration === tradingDashboardLoadGeneration && !tradingIsAbortError(err)) tradingSetMsg(tradingFriendlyErrorText(err?.message || "網格機器人讀取失敗"), false);
	    });
	    loadTradingBotCompetition().catch((err) => {
	      if (tradingOperationIsCurrent(operation) && loadGeneration === tradingDashboardLoadGeneration && !tradingIsAbortError(err)) tradingSetMsg(tradingFriendlyErrorText(err?.message || "競賽排行讀取失敗"), false);
	    });
	    renderTradingContracts(tradingState.futuresPositions);
    renderTradingMarginPositions(tradingState.marginPositions);
    const displayedMarginSummary = tradingDisplayedMarginSummary(tradingState.marginSummary);
    renderTradingMarginAccountSummary(displayedMarginSummary);
    if (typeof loadTradingAssetOverview === "function") {
      loadTradingAssetOverview({ quiet: true }).catch((err) => {
        if (tradingOperationIsCurrent(operation) && loadGeneration === tradingDashboardLoadGeneration && !tradingIsAbortError(err)) {
          tradingSetBackgroundStatus(tradingFriendlyErrorText(err?.message || "交易資產總覽讀取失敗"), false);
        }
      });
    }
    if (currentUser === "root") {
      await loadTradingRootReport();
      tradingAssertDashboardLoadCurrent(operation, loadGeneration);
      await loadTradingRootSitewide({ silent: true });
      tradingAssertDashboardLoadCurrent(operation, loadGeneration);
    }
  } catch (err) {
    if (!tradingOperationIsCurrent(operation) || loadGeneration !== tradingDashboardLoadGeneration || tradingIsAbortError(err)) return;
    const status = $("trading-safe-mode");
    if (status) {
      status.textContent = err.message || "交易狀態讀取失敗";
      status.style.color = "#ff4f6d";
    }
  }
}

async function loadTradingLivePrice() {
  const operation = tradingOperationContext();
  if (!shouldRunTradingPolling()) return;
  const targets = tradingLivePriceTargetSymbols();
  if (!targets.length) return;
  if (tradingLivePriceInFlight) return;
  if (tradingLivePriceAbort) tradingLivePriceAbort.abort();
  const controller = new AbortController();
  tradingLivePriceAbort = controller;
  tradingLivePriceInFlight = controller;
  try {
    const liveMeta = tradingState.livePriceMeta || (tradingState.livePriceMeta = {});
    let selectedMeta = null;
    let updated = false;
    const failures = [];
    const selectedSymbol = selectedTradingMarket()?.symbol || "";
    for (const symbol of targets) {
      if (controller.signal.aborted || !shouldRunTradingPolling()) return;
      try {
        const requestSymbol = tradingMarketRequestSymbol(symbol);
        const json = await fetchTradingJson(`/trading/live-price?market=${encodeURIComponent(requestSymbol)}`, {
          forceCsrf: false,
          signal: controller.signal,
        });
        tradingAssertOperationCurrent(operation);
        if (controller.signal.aborted || !shouldRunTradingPolling()) return;
        const nextMarket = json.market || null;
        if (!nextMarket?.symbol) continue;
        const index = tradingState.markets.findIndex((row) => row.symbol === nextMarket.symbol);
        const previousMarket = index >= 0 ? tradingState.markets[index] : null;
        const mergedMarket = tradingMergeLiveMarket(previousMarket, nextMarket);
        if (index >= 0) {
          tradingState.markets[index] = mergedMarket;
        } else {
          tradingState.markets.unshift(mergedMarket);
        }
        const previousMeta = liveMeta[nextMarket.symbol] || {};
        const referencePriceContext = tradingMergeLivePriceContext(
          previousMeta.reference_price_context || previousMarket?.reference_price_context,
          json.reference_price_context,
        );
        const riskGradePriceContext = tradingMergeLivePriceContext(
          previousMeta.risk_grade_price_context || previousMarket?.risk_grade_price_context,
          json.risk_grade_price_context,
        );
        liveMeta[nextMarket.symbol] = {
          price_type: json.price_type || "reference",
          source: json.source || nextMarket.price_source || "",
          confidence: json.confidence || "",
          stale: !!json.stale,
          degraded: !!json.degraded,
          conservative_mode: json.price_health === "conservative" || !!json.conservative_mode,
          provider_count: Number.isFinite(Number(json.provider_count)) ? Number(json.provider_count) : null,
          minimum_provider_count: Number.isFinite(Number(json.minimum_provider_count)) ? Number(json.minimum_provider_count) : null,
          connected: !!json.connected,
          fallback: !!json.fallback,
          last_update_at: json.last_update_at || "",
          exclusion_reason: json.exclusion_reason || "",
          price_health: json.price_health || "healthy",
          fallback_reason: json.fallback_reason || "",
          excluded_sources: Array.isArray(json.excluded_sources) ? json.excluded_sources : [],
          warnings: Array.isArray(json.warnings) ? json.warnings : [],
          high_risk_blocked: !!json.high_risk_blocked,
          high_risk_block_reason: json.high_risk_block_reason || "",
          defaulted_market: !!json.defaulted_market,
          reference_price_context: referencePriceContext,
          risk_grade_price_context: riskGradePriceContext,
          transport_state: json.transport_state && typeof json.transport_state === "object" ? json.transport_state : null,
        };
        if (nextMarket.symbol === selectedSymbol) selectedMeta = liveMeta[nextMarket.symbol];
        updated = true;
      } catch (err) {
        if (tradingIsAbortError(err) || controller.signal.aborted || !shouldRunTradingPolling()) return;
        failures.push(`${tradingDisplaySymbol(symbol)}: ${tradingFriendlyErrorText(err?.message || "即時價格讀取失敗")}`);
        // Keep the last visible price for this market; partial failure should not stop other wallet markets.
      }
    }
    if (!updated && failures.length) {
      tradingSetBackgroundStatus(`即時價格讀取失敗：${failures.slice(0, 2).join("；")}`, false);
      return;
    }
    if (!updated || controller.signal.aborted || !shouldRunTradingPolling()) return;
    tradingSetBackgroundStatus("", true);
    const selected = selectedTradingMarket();
    if (currentModuleTab === "trading" && selected) {
      renderTradingCurrentPrice(selected, {
        animate: true,
        priceHealth: selectedMeta?.price_health,
        fallbackReason: selectedMeta?.fallback_reason,
        excludedSources: selectedMeta?.excluded_sources,
        defaultedMarket: !!selectedMeta?.defaulted_market,
        transportState: selectedMeta?.transport_state,
      });
      updateTradingOrderEstimate();
      updateTradingMarginEstimate();
      renderTradingRiskDashboard();
      const limit = $("trading-limit-price");
      if (limit) {
        limit.placeholder = `目前 ${formatTradingPointsValue(tradingMarketPricePoints(selected, "reference"))}`;
      }
    }
    refreshTradingWalletLiveMetrics();
  } catch (err) {
    if (tradingOperationIsCurrent(operation) && !tradingIsAbortError(err) && !controller.signal.aborted && shouldRunTradingPolling()) {
      tradingSetBackgroundStatus(tradingFriendlyErrorText(err?.message || "即時價格讀取失敗"), false);
    }
  } finally {
    if (tradingLivePriceAbort === controller) tradingLivePriceAbort = null;
    if (tradingLivePriceInFlight === controller) tradingLivePriceInFlight = false;
  }
}

async function loadTradingRootReport() {
  if (currentUser !== "root") {
    tradingSetMsg("只有 root 可以讀取交易所管理報告", false);
    return;
  }
  try {
    const json = await fetchTradingJson("/admin/trading/report", { allowMissingSnapshot: true });
    if (json?.snapshot?.missing) {
      tradingState.rootReport = {};
      renderTradingRootReport(tradingState.rootReport);
      renderTradingRiskDashboard();
      const queued = typeof enqueueTradingSnapshotRefreshOnce === "function"
        ? await enqueueTradingSnapshotRefreshOnce("root_trading_report_missing_snapshot")
        : { ok: false, msg: "背景刷新 helper 尚未載入" };
      tradingSetMsg(
        queued?.ok
          ? "交易報表第一次開啟正在建立快照；已排入 sitewide_metrics_refresh，背景完成後會自動帶出報表。"
          : `交易報表快照正在等待背景刷新；排程失敗：${queued?.msg || "請到 root 背景引擎檢查 worker"}`,
        queued?.ok !== false,
      );
      return;
    }
    tradingState.rootReport = json.report || {};
    renderTradingRootReport(tradingState.rootReport);
    renderTradingRiskDashboard();
  } catch (err) {
    tradingSetMsg(tradingFriendlyErrorText(err.message || "交易報告讀取失敗"), false);
  }
}

async function submitTradingOrder() {
  ensureTradingAccountScope();
  saveTradingPersonalFormState();
  const market = selectedTradingMarket();
  if (!market) {
    tradingSetMsg("沒有可用交易市場", false);
    return;
  }
  const estimate = updateTradingOrderEstimate();
  if (estimate.blocking) {
    tradingSetMsg(estimate.message || "下單資料超出可用資產", false);
    return;
  }
  const orderType = $("trading-order-type")?.value || "market";
  const payload = {
    market_symbol: market.symbol,
    side: $("trading-side")?.value || "buy",
    order_type: orderType,
    quantity: tradingQuantityForSubmit(estimate.quantity),
    source_wallet_address: tradingDefaultSpendWalletAddress(),
    stop_loss_percent: tradingOptionalPercentValue("trading-stop-loss-percent"),
    take_profit_percent: tradingOptionalPercentValue("trading-take-profit-percent"),
  };
  if (orderType === "limit") payload.limit_price_points = Number($("trading-limit-price")?.value || 0);
  try {
    const json = await fetchTradingJson("/trading/orders", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (!json.order?.order_uuid) {
      throw new Error(json.msg || "交易引擎未回傳訂單，請重新整理後確認掛單 / 成交明細。");
    }
    applyTradingOrderResult(json.order || null);
    const rootPrefix = currentUser === "root" ? "root 模擬" : "";
    tradingSetMsg(json.executed
      ? `${rootPrefix}訂單已成交，正在背景更新錢包與成交明細`
      : `${rootPrefix}限價單已掛出，正在背景更新錢包與訂單列表`);
    scheduleTradingMutationRefresh();
  } catch (err) {
    tradingSetMsg(tradingFriendlyErrorText(err.message || "下單失敗"), false);
  }
}

// Trading bot mutation and backtest handlers live in 56-trading-bots.js.

function tradingOptionalPercentValue(target) {
  const el = typeof target === "string" ? $(target) : target;
  if (!el) return null;
  const raw = String(el.value || "").trim();
  if (!raw) return null;
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? value : null;
}

async function openRootTradingContract() {
  if (currentUser !== "root") {
    tradingSetMsg("只有 root 可以使用合約模擬交易", false);
    return;
  }
  const symbol = $("trading-contract-market-select")?.value || selectedTradingMarket()?.symbol || "";
  if (!symbol) {
    tradingSetMsg("請先選擇合約市場", false);
    return;
  }
  try {
    const json = await fetchTradingJson("/root/trading/contracts", {
      method: "POST",
      body: JSON.stringify({
        market_symbol: symbol,
        side: $("trading-contract-side")?.value || "long",
        quantity: $("trading-contract-quantity")?.value || "",
        leverage: Number($("trading-contract-leverage")?.value || 1),
        margin_points: Number($("trading-contract-margin")?.value || 0),
      }),
    });
    if (json.funding) tradingState.funding = json.funding;
    tradingSetMsg("root 合約模擬倉位已建立");
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "合約開倉失敗", false);
  }
}

async function closeRootTradingContract(positionUuid) {
  if (!positionUuid) {
    tradingSetMsg("找不到要平倉的合約倉位", false);
    return;
  }
  try {
    const json = await fetchTradingJson(`/root/trading/contracts/${encodeURIComponent(positionUuid)}/close`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (json.funding) tradingState.funding = json.funding;
    tradingSetMsg(`合約已平倉，損益 ${Number(json.pnl_points || 0)} 點`);
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "合約平倉失敗", false);
  }
}

async function openTradingMarginPosition() {
  ensureTradingAccountScope();
  saveTradingPersonalFormState();
  const symbol = $("trading-margin-market-select")?.value || selectedTradingMarket()?.symbol || "";
  if (!symbol) {
    tradingSetMsg("請先選擇進階交易市場", false);
    return;
  }
  const estimate = updateTradingMarginEstimate();
  if (estimate?.blocking) {
    tradingSetMsg(estimate.message || "進階交易參數不符合開倉條件", false);
    return;
  }
  try {
    const json = await fetchTradingJson("/trading/margin/open", {
      method: "POST",
      body: JSON.stringify({
        market_symbol: symbol,
        position_type: $("trading-margin-type")?.value || "margin_long",
        quantity: $("trading-margin-quantity")?.value || "",
        collateral_points: Number($("trading-margin-collateral")?.value || 0),
        stop_loss_percent: tradingOptionalPercentValue("trading-margin-stop-loss-percent"),
        take_profit_percent: tradingOptionalPercentValue("trading-margin-take-profit-percent"),
        idempotency_key: tradingRequestId("margin-open"),
      }),
    });
    if (json.funding) tradingState.funding = json.funding;
    tradingSetMsg("進階交易倉位已建立");
    await loadTradingDashboard();
  } catch (err) {
    const detail = tradingFriendlyErrorText(err.message || "後端未提供錯誤原因");
    tradingSetMsg(`進階交易開倉失敗：${detail}`, false);
  }
}

async function closeTradingMarginPosition(positionUuid) {
  if (!positionUuid) {
    tradingSetMsg("找不到要平倉的進階交易倉位", false);
    return;
  }
  if (!confirm("確定平掉這筆進階交易倉位？")) {
    tradingSetMsg("已取消進階交易平倉");
    return;
  }
  try {
    const json = await fetchTradingJson(`/trading/margin/${encodeURIComponent(positionUuid)}/close`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (json.funding) tradingState.funding = json.funding;
    tradingSetMsg(`進階交易已平倉，損益 ${Number(json.delta_points || 0)} 點，利息 ${Number(json.interest_points || 0)} 點`);
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "進階交易平倉失敗", false);
  }
}

function tradingScopedActionInput(trigger, selector) {
  const row = trigger?.closest?.(".drive-file-row");
  return row?.querySelector?.(selector) || document.querySelector(selector);
}

async function addTradingMarginCollateral(positionUuid, trigger = null) {
  if (!positionUuid) {
    tradingSetMsg("找不到要補保證金的進階交易倉位", false);
    return;
  }
  const input = tradingScopedActionInput(trigger, `[data-margin-collateral-amount="${CSS.escape(positionUuid)}"]`);
  const amount = Number(input?.value || 0);
  if (!amount || amount <= 0) {
    tradingSetMsg("請輸入要補入的保證金點數", false);
    return;
  }
  try {
    const idempotencyKey = `margin-collateral:${positionUuid}:${amount}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
    const json = await fetchTradingJson(`/trading/margin/${encodeURIComponent(positionUuid)}/collateral`, {
      method: "POST",
      body: JSON.stringify({ amount_points: amount, idempotency_key: idempotencyKey }),
    });
    if (json.funding) tradingState.funding = json.funding;
    tradingSetMsg(`已補入 ${formatTradingPointsValue(amount)} 點保證金`);
    await loadTradingDashboard();
    if (typeof loadEconomyDashboard === "function") await loadEconomyDashboard();
  } catch (err) {
    tradingSetMsg(`補保證金失敗：${err.message || "後端未提供錯誤原因"}`, false);
  }
}

async function withdrawTradingMarginCollateral(positionUuid, trigger = null) {
  if (!positionUuid) {
    tradingSetMsg("找不到要抽出保證金的進階交易倉位", false);
    return;
  }
  const input = tradingScopedActionInput(trigger, `[data-margin-withdraw-collateral-amount="${CSS.escape(positionUuid)}"]`);
  const amount = Number(input?.value || 0);
  if (!amount || amount <= 0) {
    tradingSetMsg("請輸入要抽出的保證金點數", false);
    return;
  }
  const position = tradingMarginPositionByUuid(positionUuid);
  const withdrawEstimate = position ? tradingMarginWithdrawEstimate(position) : null;
  if (withdrawEstimate && amount > withdrawEstimate.maxWithdrawable) {
    tradingSetMsg(`預估可抽出保證金上限為 ${formatTradingPointsValue(withdrawEstimate.maxWithdrawable)} 點；${withdrawEstimate.reason}`, false);
    return;
  }
  if (!confirm("抽出保證金會降低維持率，價格反向波動時更容易接近強平線。確定要抽出？")) {
    tradingSetMsg("已取消抽出保證金");
    return;
  }
  try {
    const idempotencyKey = `margin-collateral-withdraw:${positionUuid}:${amount}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
    const json = await fetchTradingJson(`/trading/margin/${encodeURIComponent(positionUuid)}/collateral/withdraw`, {
      method: "POST",
      body: JSON.stringify({ amount_points: amount, idempotency_key: idempotencyKey }),
    });
    if (json.funding) tradingState.funding = json.funding;
    const beforeRatio = json.risk_before?.maintenance_ratio_percent;
    const afterRatio = json.risk_after?.maintenance_ratio_percent;
    const ratioText = beforeRatio != null && afterRatio != null
      ? `，維持率 ${formatTradingPointsValue(beforeRatio)}% → ${formatTradingPointsValue(afterRatio)}%`
      : "";
    tradingSetMsg(`已抽出 ${formatTradingPointsValue(amount)} 點保證金${ratioText}。請留意強平風險。`);
    await loadTradingDashboard();
    if (typeof loadEconomyDashboard === "function") await loadEconomyDashboard();
  } catch (err) {
    tradingSetMsg(`抽出保證金失敗：${err.message || "後端未提供錯誤原因"}`, false);
  }
}

async function cancelTradingOrder(orderUuid) {
  if (!orderUuid) {
    tradingSetMsg("找不到要取消的訂單", false);
    return;
  }
  try {
    await fetchTradingJson(`/trading/orders/${encodeURIComponent(orderUuid)}/cancel`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    tradingSetMsg("訂單已取消");
    await loadEconomyDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "取消訂單失敗", false);
  }
}

async function saveTradingRootMarket() {
  const symbol = $("trading-root-market-select")?.value || "";
  if (!symbol) {
    tradingSetMsg("請先選擇市場", false);
    return;
  }
  try {
    await fetchTradingJson(`/root/trading/markets/${encodeURIComponent(symbol)}`, {
      method: "POST",
      body: JSON.stringify({
        max_price_jump_percent: tradingInputPercent($("trading-root-jump-percent")?.value || 0),
        fee_rate_percent: tradingInputPercent($("trading-root-fee-percent")?.value || 0),
        min_order_points: Number($("trading-root-min-order")?.value || 0),
        max_order_points: Number($("trading-root-max-order")?.value || 0),
        enabled: !!$("trading-root-enabled")?.checked,
      }),
    });
    tradingSetMsg("交易市場設定已儲存");
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "市場設定儲存失敗", false);
  }
}

async function resetRootTradingSimulatedBalance() {
  if (!confirm("確認重置 root 模擬交易？這會刪除 root 的模擬訂單、成交紀錄、現貨與合約持倉，並把虛擬積分回到 10000。")) return;
  try {
    const json = await fetchTradingJson("/root/trading/simulated-balance/reset", {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (json.funding) tradingState.funding = json.funding;
    const deleted = json.deleted || {};
    tradingSetMsg(
      `root 模擬交易已重置為 ${Number(json.funding?.available_points || 10000)} 點；` +
      `已清除訂單 ${Number(deleted.orders || 0)}、成交 ${Number(deleted.fills || 0)}、現貨 ${Number(deleted.spot_positions || 0)}。`
    );
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "root 模擬資金重設失敗", false);
  }
}

async function scanTradingLiquidations() {
  if (currentUser !== "root") {
    tradingSetMsg("只有 root 可以手動掃描強平條件", false);
    return;
  }
  const status = $("trading-liquidation-status");
  if (status) status.textContent = "正在掃描強平條件...";
  try {
    const json = await fetchTradingJson("/root/trading/liquidations/scan", {
      method: "POST",
      body: JSON.stringify({ limit: 100 }),
    });
    const liquidated = Array.isArray(json.liquidated) ? json.liquidated.length : 0;
    const errors = Array.isArray(json.errors) ? json.errors.length : 0;
    tradingSetMsg(`強平掃描完成：掃描 ${Number(json.scanned || 0)} 筆，清算 ${liquidated} 筆，錯誤 ${errors} 筆`, errors === 0);
    if (status) status.textContent = `最近手動掃描：清算 ${liquidated} 筆，錯誤 ${errors} 筆`;
    await loadTradingDashboard();
  } catch (err) {
    if (status) status.textContent = err.message || "強平掃描失敗";
    tradingSetMsg(err.message || "強平掃描失敗", false);
  }
}

async function matchTradingLimitOrders() {
  if (currentUser !== "root") {
    tradingSetMsg("只有 root 可以手動掃描限價單撮合", false);
    return;
  }
  try {
    const json = await fetchTradingJson("/root/trading/orders/match", {
      method: "POST",
      body: JSON.stringify({ limit: 200 }),
    });
    const matched = Array.isArray(json.matched) ? json.matched.length : 0;
    const errors = Array.isArray(json.errors) ? json.errors.length : 0;
    tradingSetMsg(`限價單撮合完成：掃描 ${Number(json.scanned || 0)} 筆，成交 ${matched} 筆，錯誤 ${errors} 筆`, errors === 0);
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "限價單撮合失敗", false);
  }
}

function bindTradingEvents() {
  if (tradingEventsBound) return;
  tradingEventsBound = true;
  ensureTradingAccountScope({ force: true });
  bindTradingPersonalFormPersistence();
  document.addEventListener("hackme:account-context-changed", () => {
    resetTradingAccountState();
    ensureTradingAccountScope({ force: true });
  });
  window.addEventListener("economy:default-spend-wallet-changed", () => {
    if (!shouldRunTradingPolling()) return;
    loadTradingDashboard().catch((err) => tradingSetMsg(tradingFriendlyErrorText(err.message || "交易錢包餘額更新失敗"), false));
  });
  const paymentWallet = $("trading-payment-wallet");
  if (paymentWallet && paymentWallet.dataset.tradingPaymentWalletBound !== "1") {
    paymentWallet.dataset.tradingPaymentWalletBound = "1";
    paymentWallet.addEventListener("change", () => {
      if (typeof writeEconomyDefaultSpendWalletAddress === "function") {
        writeEconomyDefaultSpendWalletAddress(paymentWallet.value || "");
      }
      loadTradingDashboard().catch((err) => tradingSetMsg(tradingFriendlyErrorText(err.message || "交易錢包餘額更新失敗"), false));
    });
  }
  const rootSettingsPageBtn = $("trading-root-settings-page-btn");
  if (rootSettingsPageBtn) rootSettingsPageBtn.addEventListener("click", openTradingSettingsPage);
  const rootSettingsBackBtn = $("trading-root-settings-back-btn");
  if (rootSettingsBackBtn) rootSettingsBackBtn.addEventListener("click", openTradingExchangePage);
  const bindings = [
    ["trading-refresh-btn", loadTradingDashboard, "正在重新整理交易資料...", "交易資料重新整理失敗"],
    ["trading-submit-order-btn", submitTradingOrder, "正在送出訂單...", "下單失敗"],
    ["trading-auto-bot-save-btn", saveTradingBot, "正在新增自動化機器人...", "自動化機器人新增失敗"],
    ["trading-dca-bot-save-btn", saveTradingDcaBot, "正在新增定投機器人...", "定投機器人新增失敗"],
    ["trading-bot-scan-btn", scanTradingBots, "正在掃描已啟用交易機器人...", "交易機器人掃描失敗"],
    ["trading-dca-backtest-run-btn", () => backtestTradingBot("dca"), "正在執行定投回測...", "定投回測失敗"],
    ["trading-grid-backtest-run-btn", () => backtestTradingBot("grid"), "正在執行網格回測...", "網格回測失敗"],
    ["trading-workflow-backtest-run-btn", () => backtestTradingBot("workflow"), "正在執行 Workflow 回測...", "Workflow 回測失敗"],
    ["trading-workflow-load-btn", loadTradingWorkflowFromEditor, "正在載入 Workflow 編輯器結果...", "Workflow 載入失敗"],
	    ["trading-workflow-template-apply-btn", applyTradingWorkflowTemplate, "正在套用 Workflow 基礎模板...", "Workflow 模板套用失敗"],
	    ["trading-workflow-custom-save-btn", saveTradingWorkflowCustomTemplate, "正在儲存 Workflow 自訂模板...", "Workflow 自訂模板儲存失敗"],
	    ["trading-bot-competition-refresh-btn", loadTradingBotCompetition, "正在讀取機器人競賽排行...", "競賽排行讀取失敗"],
    ["trading-bot-competition-award-btn", awardTradingBotCompetition, "正在發放機器人週賽獎勵...", "週賽獎勵發放失敗"],
    ["trading-root-refresh-btn", loadTradingRootReport, "正在讀取 root 交易報告...", "交易報告讀取失敗"],
    ["trading-root-save-market-btn", saveTradingRootMarket, "正在儲存交易市場設定...", "市場設定儲存失敗"],
    ["trading-root-reset-sim-btn", resetRootTradingSimulatedBalance, "準備重置 root 模擬交易...", "root 模擬資金重設失敗"],
    ["trading-contract-open-btn", openRootTradingContract, "正在建立 root 合約模擬倉位...", "合約開倉失敗"],
    ["trading-margin-open-btn", openTradingMarginPosition, "正在建立進階交易倉位...", "進階交易開倉失敗"],
    ["trading-limit-match-btn", matchTradingLimitOrders, "正在掃描限價單撮合...", "限價單撮合失敗"],
    ["trading-liquidation-scan-btn", scanTradingLiquidations, "正在掃描強平條件...", "強平掃描失敗"],
  ];
  bindings.forEach(([id, handler, pendingText, fallbackText]) => {
    const el = $(id);
    if (!el) return;
    bindTradingActionButton(el, handler, pendingText, fallbackText);
  });
  const workflowTemplateSelect = $("trading-workflow-template-select");
  if (workflowTemplateSelect) workflowTemplateSelect.addEventListener("change", renderTradingWorkflowTemplateExplanation);
  Object.keys(TRADING_BACKTEST_CONTEXTS).forEach((contextKey) => {
    ["timeframe", "start", "end"].forEach((suffix) => {
      const el = tradingBacktestEl(contextKey, suffix);
      if (!el) return;
      el.addEventListener("change", () => updateBacktestDateRangeGuidance(contextKey));
      el.addEventListener("input", () => updateBacktestDateRangeGuidance(contextKey));
    });
    const botSelect = tradingBacktestEl(contextKey, "bot-select");
    if (botSelect) {
      botSelect.addEventListener("change", () => {
        if (botSelect.value) prepareTradingBacktestFromBot(botSelect.value);
        else tradingSetMsg("已切換為使用目前表單設定回測");
      });
    }
  });
  Object.keys(TRADING_BACKTEST_CONTEXTS).forEach((contextKey) => updateBacktestDateRangeGuidance(contextKey));
  // Grid bot wiring
  const gridCreateBtn = $("trading-grid-bot-create-btn");
  if (gridCreateBtn) bindTradingActionButton(gridCreateBtn, createGridBot, "正在建立網格機器人...", "網格機器人建立失敗");
  const gridScanBtn = $("trading-grid-scan-btn");
  if (gridScanBtn) bindTradingActionButton(gridScanBtn, scanGridBots, "掃描網格機器人中...", "網格掃描失敗");
  const clearOverlayBtn = $("trading-chart-clear-overlay-btn");
  if (clearOverlayBtn) clearOverlayBtn.addEventListener("click", clearBotChartOverlay);
  ["trading-grid-upper-price", "trading-grid-lower-price", "trading-grid-count", "trading-grid-order-amount"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", scheduleGridBotPreview);
  });
  const gridSpacingMode = $("trading-grid-spacing-mode");
  if (gridSpacingMode) gridSpacingMode.addEventListener("change", scheduleGridBotPreview);
  const gridMarketSelect = $("trading-grid-bot-market");
  if (gridMarketSelect) {
    gridMarketSelect.addEventListener("change", () => {
      if ($("trading-grid-preset")?.value) applyGridPreset({ quiet: true });
      else scheduleGridBotPreview();
    });
  }
  const gridPresetSelect = $("trading-grid-preset");
  if (gridPresetSelect) gridPresetSelect.addEventListener("change", applyGridPreset);
  document.querySelectorAll("[data-trading-bot-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      switchTradingBotTab(btn.dataset.tradingBotTab || "dca");
      tradingSetMsg(`已切換到${btn.textContent?.trim() || "交易機器人"}分頁`);
    });
  });
  document.querySelectorAll("[data-trading-root-sitewide-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      switchTradingRootSitewideTab(btn.dataset.tradingRootSitewideTab || "positions", { refreshSnapshot: false });
    });
  });
  const rootSitewideRefreshBtn = $("trading-root-sitewide-refresh-btn");
  if (rootSitewideRefreshBtn) {
    bindTradingActionButton(
      rootSitewideRefreshBtn,
      () => loadTradingRootSitewide({ refreshSnapshot: true }),
      "正在更新全站倉位與借貸基金...",
      "全站倉位與借貸基金更新失敗",
    );
  }
  const dcaPreset = $("trading-dca-bot-interval-preset");
  if (dcaPreset) {
    syncTradingDcaIntervalMode();
    dcaPreset.addEventListener("change", () => {
      const custom = syncTradingDcaIntervalMode();
      saveTradingPersonalFormState();
      tradingSetMsg(custom ? "已切換為自訂定投間隔" : `已選擇每 ${dcaPreset.value} 小時定投`);
    });
  }
  const marketSelect = $("trading-market-select");
  if (marketSelect) {
    marketSelect.addEventListener("change", () => {
      renderTradingSummary();
      loadTradingLivePrice().catch((err) => {
        if (!tradingIsAbortError(err)) {
          tradingSetBackgroundStatus(tradingFriendlyErrorText(err?.message || "即時價格讀取失敗"), false);
        }
      });
    });
  }
  const marginMarketSelect = $("trading-margin-market-select");
  if (marginMarketSelect) marginMarketSelect.addEventListener("change", updateTradingMarginEstimate);
  ["trading-margin-type", "trading-margin-quantity", "trading-margin-collateral"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", updateTradingMarginEstimate);
    el.addEventListener("change", updateTradingMarginEstimate);
  });
  ["trading-side", "trading-order-type", "trading-input-mode", "trading-quantity", "trading-limit-price"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", () => {
      syncTradingOrderSideTheme();
      syncTradingOrderInputMode();
      updateTradingOrderEstimate();
    });
    el.addEventListener("change", () => {
      syncTradingOrderSideTheme();
      syncTradingOrderInputMode();
      updateTradingOrderEstimate();
    });
  });
  const sellableHint = $("trading-sellable-hint");
  if (sellableHint) {
    sellableHint.addEventListener("click", (event) => {
      const btn = event.target?.closest?.("[data-trading-fill-sellable]");
      if (!btn) return;
      fillTradingSellableQuantity();
    });
  }
  const referenceInterval = $("trading-reference-interval");
  if (referenceInterval) {
    referenceInterval.addEventListener("change", () => {
      hideTradingReferenceTooltip();
      restartTradingReferenceAutoRefresh();
      loadTradingReferencePrices();
    });
  }
  [
    "trading-indicator-ma5",
    "trading-indicator-ma10",
    "trading-indicator-ma20",
    "trading-indicator-ma30",
    "trading-indicator-ma60",
    "trading-indicator-ema12",
    "trading-indicator-ema26",
    "trading-indicator-ema50",
    "trading-indicator-bollinger",
    "trading-indicator-rsi14",
    "trading-indicator-kd",
  ].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("change", () => renderTradingReferenceChart(tradingState.referencePrices));
  });
  const referenceChart = $("trading-reference-chart");
  if (referenceChart) {
    referenceChart.addEventListener("mousemove", updateTradingReferenceTooltip);
    referenceChart.addEventListener("mouseleave", hideTradingReferenceTooltip);
    referenceChart.addEventListener("pointermove", updateTradingReferenceTooltip);
    referenceChart.addEventListener("pointerleave", hideTradingReferenceTooltip);
    referenceChart.addEventListener("pointercancel", hideTradingReferenceTooltip);
    referenceChart.addEventListener("touchstart", updateTradingReferenceTooltipFromTouch, { passive: true });
    referenceChart.addEventListener("touchmove", updateTradingReferenceTooltipFromTouch, { passive: true });
    referenceChart.addEventListener("touchend", hideTradingReferenceTooltip);
    referenceChart.addEventListener("touchcancel", hideTradingReferenceTooltip);
  }
  const rootMarketSelect = $("trading-root-market-select");
  if (rootMarketSelect) rootMarketSelect.addEventListener("change", populateTradingRootMarketForm);
  document.addEventListener("hackme:module-changed", syncTradingModuleTimerLifecycle);
  document.addEventListener("visibilitychange", syncTradingModuleTimerLifecycle);
  syncTradingModuleTimerLifecycle();
}

if (document.readyState === "complete") {
  bindTradingEvents();
} else {
  document.addEventListener("DOMContentLoaded", bindTradingEvents);
}
