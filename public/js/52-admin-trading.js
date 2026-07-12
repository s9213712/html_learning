/*
 * Root trading admin module.
 *
 * This file owns trading settings, market registry, provider registry,
 * background jobs, audit dashboard, BTC trade bootstrap, and price fusion UI.
 * It is loaded after 50-admin.js so shared admin helpers and global state are
 * available without duplicating generic admin code.
 */

let rootTradingSettingsCache = { settings: {}, markets: [] };
let rootTradingPriceFusionStatusCache = null;
let rootTradingBotAuditCache = null;
let rootTradingBackgroundStatusCache = null;
let rootTradingMarketRegistryCache = [];
let rootTradingMarketRegistryAuditCache = [];
let rootTradingMarketProviderCache = [];
let rootTradingMarketRegistrySelectedId = null;
let rootTradingMarketProviderSelectedId = null;
let rootTradingAdminAccountGeneration = 0;

function rootTradingAdminOperationContext() {
  return {
    generation: rootTradingAdminAccountGeneration,
    scope: typeof getCurrentAccountStorageScope === "function"
      ? getCurrentAccountStorageScope()
      : String(currentUserId || currentUser || "anonymous"),
  };
}

function rootTradingAdminOperationIsCurrent(operation) {
  if (!operation || operation.generation !== rootTradingAdminAccountGeneration) return false;
  const scope = typeof getCurrentAccountStorageScope === "function"
    ? getCurrentAccountStorageScope()
    : String(currentUserId || currentUser || "anonymous");
  return operation.scope === scope;
}

function rootTradingAdminAssertOperationCurrent(operation) {
  if (rootTradingAdminOperationIsCurrent(operation)) return;
  if (typeof accountRequestAbortError === "function") throw accountRequestAbortError();
  throw new DOMException("Account context changed", "AbortError");
}

function resetRootTradingAdminAccountState() {
  rootTradingAdminAccountGeneration += 1;
  rootTradingSettingsCache = { settings: {}, markets: [] };
  rootTradingPriceFusionStatusCache = null;
  rootTradingBotAuditCache = null;
  rootTradingBackgroundStatusCache = null;
  rootTradingMarketRegistryCache = [];
  rootTradingMarketRegistryAuditCache = [];
  rootTradingMarketProviderCache = [];
  rootTradingMarketRegistrySelectedId = null;
  rootTradingMarketProviderSelectedId = null;
  const btcStartButton = $("root-trading-btc-trade-start-btn");
  if (btcStartButton) {
    btcStartButton.disabled = currentUser !== "root";
    btcStartButton.textContent = "一鍵啟動預測";
  }

  document.querySelectorAll('input[id^="root-trading-"], textarea[id^="root-trading-"], select[id^="root-trading-"]').forEach((control) => {
    if (control.type === "checkbox" || control.type === "radio") control.checked = false;
    else if (control.tagName === "SELECT") control.selectedIndex = -1;
    else control.value = "";
  });
  [
    "root-trading-background-summary", "root-trading-background-jobs", "root-trading-background-runs",
    "root-trading-market-registry-audit", "root-trading-market-provider-list",
    "root-trading-market-provider-selected", "root-trading-market-registry-list",
    "root-trading-price-fusion-summary", "root-trading-price-fusion-provider-list",
    "root-trading-price-fusion-excluded-list", "root-trading-bot-audit-summary",
    "root-trading-bot-audit-bots", "root-trading-bot-audit-bug-reports",
  ].forEach((id) => {
    const el = $(id);
    if (el) el.replaceChildren();
  });
  [
    "root-trading-settings-msg", "root-trading-price-fusion-msg", "root-trading-bot-audit-msg",
    "root-trading-background-msg", "root-trading-market-registry-msg",
    "root-trading-market-registry-editor-status", "root-trading-market-provider-status",
    "root-trading-btc-trade-status",
  ].forEach((id) => {
    const el = $(id);
    if (el) el.textContent = "";
  });
}

document.addEventListener("hackme:account-context-changed", resetRootTradingAdminAccountState);
window.resetRootTradingAdminAccountState = resetRootTradingAdminAccountState;

function rootTradingSettingsMsg(text, ok = true) {
  const msg = $("root-trading-settings-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
  scheduleInlineMessageClear(msg, text, ok);
}

function rootTradingPriceFusionMsg(text, ok = true) {
  const msg = $("root-trading-price-fusion-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
  scheduleInlineMessageClear(msg, text, ok);
}

function rootTradingBotAuditMsg(text, ok = true) {
  const msg = $("root-trading-bot-audit-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
  scheduleInlineMessageClear(msg, text, ok);
}

function rootTradingBackgroundMsg(text, ok = true) {
  const msg = $("root-trading-background-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
  scheduleInlineMessageClear(msg, text, ok);
}

function rootTradingBackgroundJobLabel(jobKey) {
  const labels = {
    price_refresh: "價格刷新",
    order_matching: "掛單撮合",
    take_profit_stop_loss_scan: "TP/SL 掃描",
    bot_trigger_scan: "Bot 觸發",
    margin_liquidation_scan: "借貸清算",
    interest_accrual: "借貸計息",
  };
  return labels[jobKey] || jobKey || "-";
}

function rootTradingBackgroundTime(value) {
  const text = String(value || "").trim();
  if (!text) return "-";
  const d = new Date(text.endsWith("Z") ? text : `${text}Z`);
  if (Number.isNaN(d.getTime())) return text;
  return d.toLocaleString();
}

function rootTradingBackgroundStatusLabel(job) {
  if (!job?.enabled) return { label: "paused", color: "#8a8f98" };
  if (job.lease_active) return { label: "running", color: "#d2a72a" };
  if (job.last_status === "failed") return { label: "failed", color: "#ff4f6d" };
  if (job.last_status === "skipped") return { label: "skipped", color: "#8a8f98" };
  if (job.last_success_at) return { label: "healthy", color: "#4caf50" };
  return { label: "waiting", color: "#d2a72a" };
}

function renderRootTradingBackgroundStatus(payload) {
  rootTradingBackgroundStatusCache = payload || {};
  const summaryEl = $("root-trading-background-summary");
  const jobsEl = $("root-trading-background-jobs");
  const runsEl = $("root-trading-background-runs");
  if (!summaryEl || !jobsEl || !runsEl) return;
  const jobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
  const runs = Array.isArray(payload?.recent_runs) ? payload.recent_runs : [];
  const locks = Array.isArray(payload?.locks) ? payload.locks : [];
  const failures = jobs.filter((job) => job.last_status === "failed").length;
  const active = jobs.filter((job) => job.lease_active).length;
  const paused = jobs.filter((job) => !job.enabled).length;
  summaryEl.innerHTML = `
    <div class="drive-file-row">
      <div>
        <strong>server time ${sanitize(rootTradingBackgroundTime(payload?.server_time))}</strong>
        <div class="drive-card-sub">jobs ${jobs.length} · active leases ${active} · paused ${paused} · failures ${failures} · locks ${locks.length}</div>
      </div>
    </div>
  `;
  if (!jobs.length) {
    jobsEl.innerHTML = `<div class="drive-empty">尚無 background jobs</div>`;
  } else {
    jobsEl.innerHTML = jobs.map((job) => {
      const status = rootTradingBackgroundStatusLabel(job);
      const summary = job.last_summary && typeof job.last_summary === "object" ? job.last_summary : {};
      const reason = summary.reason || job.paused_reason || job.last_error || "";
      return `
        <div class="drive-file-row billing-catalog-row">
          <div>
            <strong>${sanitize(rootTradingBackgroundJobLabel(job.job_key))}</strong>
            <div class="drive-card-sub">${sanitize(job.job_key)} · interval ${Number(job.interval_seconds || 0)}s · lease ${Number(job.lease_seconds || 0)}s</div>
            <div class="drive-card-sub">last success ${sanitize(rootTradingBackgroundTime(job.last_success_at))} · next ${sanitize(rootTradingBackgroundTime(job.next_run_at))} · runs ${Number(job.run_count || 0)} · failures ${Number(job.failure_count || 0)}</div>
            ${reason ? `<div class="drive-card-sub">${sanitize(reason)}</div>` : ""}
          </div>
          <span class="badge" style="color:${status.color};border-color:${status.color};">${sanitize(status.label)}</span>
        </div>
      `;
    }).join("");
  }
  if (!runs.length) {
    runsEl.innerHTML = `<div class="drive-empty">尚無最近執行紀錄</div>`;
  } else {
    runsEl.innerHTML = runs.slice(0, 8).map((run) => `
      <div class="drive-file-row billing-catalog-row">
        <div>
          <strong>${sanitize(rootTradingBackgroundJobLabel(run.job_key))} · ${sanitize(run.status || "-")}</strong>
          <div class="drive-card-sub">${sanitize(rootTradingBackgroundTime(run.started_at))} → ${sanitize(rootTradingBackgroundTime(run.finished_at))} · ${Number(run.duration_ms || 0).toFixed(1)}ms · mode ${sanitize(run.server_mode || "-")}</div>
          ${run.error ? `<div class="drive-card-sub">${sanitize(run.error)}</div>` : ""}
        </div>
      </div>
    `).join("");
  }
}

async function loadRootTradingBackgroundStatus() {
  if (currentUser !== "root" || !$("root-trading-background-jobs")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + "/root/trading/background/status?limit=12", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingBackgroundMsg(rootTradingSettingsHttpMessage(res, json, "背景引擎狀態讀取失敗"), false);
      return;
    }
    renderRootTradingBackgroundStatus(json);
    rootTradingBackgroundMsg("");
  } catch (err) {
    rootTradingBackgroundMsg(err.message || "背景引擎狀態請求失敗", false);
  }
}

async function postRootTradingBackgroundAction(path, payload, successText) {
  if (currentUser !== "root") return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload || {})
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingBackgroundMsg(rootTradingSettingsHttpMessage(res, json, "背景引擎操作失敗"), false);
      return;
    }
    rootTradingBackgroundMsg(successText || "背景引擎操作完成");
    await loadRootTradingBackgroundStatus();
  } catch (err) {
    rootTradingBackgroundMsg(err.message || "背景引擎操作請求失敗", false);
  }
}

function selectedRootTradingBackgroundJob() {
  return String($("root-trading-background-job-select")?.value || "").trim();
}

function bindRootTradingBackgroundControls() {
  const refreshBtn = $("root-trading-background-refresh-btn");
  if (refreshBtn && !refreshBtn.dataset.bgBound) {
    refreshBtn.addEventListener("click", loadRootTradingBackgroundStatus);
    refreshBtn.dataset.bgBound = "1";
  }
  const runBtn = $("root-trading-background-run-once-btn");
  if (runBtn && !runBtn.dataset.bgBound) {
    runBtn.addEventListener("click", () => {
      const jobKey = selectedRootTradingBackgroundJob();
      if (!jobKey) {
        rootTradingBackgroundMsg("請先選擇單一 job", false);
        return;
      }
      postRootTradingBackgroundAction(
        "/root/trading/background/run-once",
        { job_key: jobKey, confirm: "RUN_TRADING_JOB_ONCE" },
        `${rootTradingBackgroundJobLabel(jobKey)} 已送出`
      );
    });
    runBtn.dataset.bgBound = "1";
  }
  const pauseBtn = $("root-trading-background-pause-btn");
  if (pauseBtn && !pauseBtn.dataset.bgBound) {
    pauseBtn.addEventListener("click", () => {
      const jobKey = selectedRootTradingBackgroundJob();
      postRootTradingBackgroundAction(
        "/root/trading/background/pause",
        jobKey ? { job_key: jobKey, reason: "paused_from_root_ui" } : { reason: "paused_from_root_ui" },
        jobKey ? `${rootTradingBackgroundJobLabel(jobKey)} 已暫停` : "全部 background jobs 已暫停"
      );
    });
    pauseBtn.dataset.bgBound = "1";
  }
  const resumeBtn = $("root-trading-background-resume-btn");
  if (resumeBtn && !resumeBtn.dataset.bgBound) {
    resumeBtn.addEventListener("click", () => {
      const jobKey = selectedRootTradingBackgroundJob();
      postRootTradingBackgroundAction(
        "/root/trading/background/resume",
        jobKey ? { job_key: jobKey } : {},
        jobKey ? `${rootTradingBackgroundJobLabel(jobKey)} 已恢復` : "全部 background jobs 已恢復"
      );
    });
    resumeBtn.dataset.bgBound = "1";
  }
}

function rootTradingMarketRegistryMsg(text, ok = true) {
  const msg = $("root-trading-market-registry-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
  scheduleInlineMessageClear(msg, text, ok);
}

function rootTradingMarketRegistryEditorStatus(text, ok = true) {
  const msg = $("root-trading-market-registry-editor-status");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
  scheduleInlineMessageClear(msg, text, ok);
}

function rootTradingMarketProviderStatus(text, ok = true) {
  const msg = $("root-trading-market-provider-status");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
  scheduleInlineMessageClear(msg, text, ok);
}

function tradingMarketRegistryCheckbox(id, value, fallback = false) {
  const node = $(id);
  if (node) node.checked = value == null ? fallback : !!value;
}

function tradingMarketRegistryValue(id, value, fallback = "") {
  const node = $(id);
  if (node) node.value = value == null ? fallback : value;
}

function rootTradingDisplayMarketSymbol(symbol, displaySymbol = "") {
  const display = String(displaySymbol || "").trim().toUpperCase();
  if (display) return display.replace("/POINTS", "/USDT");
  return String(symbol || "").trim().toUpperCase().replace("/POINTS", "/USDT");
}

function rootTradingDisplayQuoteAsset(asset) {
  const normalized = String(asset || "").trim().toUpperCase();
  if (!normalized || normalized === "POINTS") return "USDT";
  return normalized;
}

function rootTradingPointsUnitText(value, digits = 4) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "-";
  return `${number.toLocaleString(undefined, { maximumFractionDigits: digits })} 點`;
}

function clearRootTradingMarketProviderForm() {
  rootTradingMarketProviderSelectedId = null;
  if ($("root-trading-market-provider-name")) $("root-trading-market-provider-name").value = "binance_public_api";
  tradingMarketRegistryValue("root-trading-market-provider-symbol", "");
  tradingMarketRegistryValue("root-trading-market-provider-priority", 100);
  tradingMarketRegistryCheckbox("root-trading-market-provider-enabled", true, true);
  tradingMarketRegistryCheckbox("root-trading-market-provider-ticker", true, true);
  tradingMarketRegistryCheckbox("root-trading-market-provider-depth", true, true);
  tradingMarketRegistryCheckbox("root-trading-market-provider-candles", true, true);
  rootTradingMarketProviderStatus("");
}

function renderRootTradingMarketRegistryAudit(symbol = "") {
  const list = $("root-trading-market-registry-audit");
  if (!list) return;
  const normalized = String(symbol || "").trim().toUpperCase();
  const rows = rootTradingMarketRegistryAuditCache.filter((row) => !normalized || String(row.market_symbol || "").trim().toUpperCase() === normalized);
  if (!rows.length) {
    list.innerHTML = `<div class="drive-empty">尚無${normalized ? ` ${sanitize(normalized)} ` : ""}market registry audit</div>`;
    return;
  }
  list.innerHTML = rows.slice(0, 20).map((row) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(row.action || "audit")}</strong>
        <div class="drive-card-sub">${sanitize(row.market_symbol || "-")} · actor ${sanitize(String(row.actor_id ?? "-"))} · ${sanitize(row.created_at || "-")}</div>
      </div>
    </div>
  `).join("");
}

function populateRootTradingMarketProviderForm(row = null) {
  clearRootTradingMarketProviderForm();
  if (!row || typeof row !== "object") return;
  rootTradingMarketProviderSelectedId = Number(row.id || 0) || null;
  tradingMarketRegistryValue("root-trading-market-provider-name", row.provider || "binance_public_api");
  tradingMarketRegistryValue("root-trading-market-provider-symbol", row.provider_symbol || "");
  tradingMarketRegistryValue("root-trading-market-provider-priority", Number(row.priority || 100));
  tradingMarketRegistryCheckbox("root-trading-market-provider-enabled", row.enabled, true);
  tradingMarketRegistryCheckbox("root-trading-market-provider-ticker", row.supports_ticker, true);
  tradingMarketRegistryCheckbox("root-trading-market-provider-depth", row.supports_depth, true);
  tradingMarketRegistryCheckbox("root-trading-market-provider-candles", row.supports_candles, true);
  rootTradingMarketProviderStatus(`正在編輯 ${row.provider_label || row.provider || "provider"} mapping`);
}

function renderRootTradingMarketProviders(payload = {}) {
  const list = $("root-trading-market-provider-list");
  const selected = $("root-trading-market-provider-selected");
  if (!list || !selected) return;
  const market = payload.market || null;
  rootTradingMarketProviderCache = Array.isArray(payload.providers) ? payload.providers : [];
  if (!market) {
    selected.textContent = "尚未選擇市場";
    list.innerHTML = `<div class="drive-empty">請先從上方市場列表選擇一個市場</div>`;
    renderRootTradingMarketRegistryAudit("");
    return;
  }
  selected.textContent = `${rootTradingDisplayMarketSymbol(market.symbol, market.display_name || market.display_symbol)} · provider ${Number(market.provider_count || rootTradingMarketProviderCache.length)} 家 · probe ${market.probe_status || "pending"} · seed ${market.seed_sync_status || "-"}`;
  if (!rootTradingMarketProviderCache.length) {
    list.innerHTML = `<div class="drive-empty">尚無 provider mapping</div>`;
  } else {
    list.innerHTML = rootTradingMarketProviderCache.map((row) => `
      <div class="drive-file-row billing-catalog-row">
        <div>
          <strong>${sanitize(row.provider_label || row.provider || "-")}</strong>
          <div class="drive-card-sub">${sanitize(row.provider_symbol || "尚未設定 provider symbol")} · priority ${Number(row.priority || 0)} · ${row.enabled ? "啟用" : "停用"}</div>
          <div class="drive-card-sub">ticker ${row.supports_ticker ? "✓" : "×"} · depth ${row.supports_depth ? "✓" : "×"} · candles ${row.supports_candles ? "✓" : "×"}</div>
        </div>
        <div class="admin-toolbar" style="gap:.35rem;flex-wrap:wrap;">
          <button class="btn" type="button" data-root-trading-provider-edit="${Number(row.id || 0)}">編輯</button>
          <button class="btn" type="button" data-root-trading-provider-disable="${Number(row.id || 0)}">停用</button>
        </div>
      </div>
    `).join("");
    list.querySelectorAll("[data-root-trading-provider-edit]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const row = rootTradingMarketProviderCache.find((item) => Number(item.id || 0) === Number(btn.dataset.rootTradingProviderEdit || 0));
        populateRootTradingMarketProviderForm(row || null);
      });
    });
    list.querySelectorAll("[data-root-trading-provider-disable]").forEach((btn) => {
      btn.addEventListener("click", () => disableRootTradingMarketProvider(btn.dataset.rootTradingProviderDisable || ""));
    });
  }
  renderRootTradingMarketRegistryAudit(market.symbol || "");
}

function clearRootTradingMarketRegistryForm() {
  rootTradingMarketRegistrySelectedId = null;
  tradingMarketRegistryValue("root-trading-registry-symbol", "");
  if ($("root-trading-registry-symbol")) {
    $("root-trading-registry-symbol").disabled = false;
    $("root-trading-registry-symbol").dataset.rawSymbol = "";
  }
  tradingMarketRegistryValue("root-trading-registry-base-asset", "");
  tradingMarketRegistryValue("root-trading-registry-quote-asset", "USDT");
  if ($("root-trading-registry-quote-asset")) $("root-trading-registry-quote-asset").dataset.rawQuoteAsset = "";
  tradingMarketRegistryValue("root-trading-registry-display-quote", "USDT");
  tradingMarketRegistryValue("root-trading-registry-display-name", "");
  tradingMarketRegistryValue("root-trading-registry-market-type", "spot");
  tradingMarketRegistryValue("root-trading-registry-sort-order", 9999);
  tradingMarketRegistryValue("root-trading-registry-default-manual-price", 1);
  tradingMarketRegistryValue("root-trading-registry-price-precision", 8);
  tradingMarketRegistryValue("root-trading-registry-quantity-precision", 8);
  tradingMarketRegistryValue("root-trading-registry-min-order-size", 0.00000001);
  tradingMarketRegistryValue("root-trading-registry-max-order-size", 1000000);
  tradingMarketRegistryValue("root-trading-registry-lot-size", 0.00000001);
  tradingMarketRegistryValue("root-trading-registry-tick-size", 0.00000001);
  tradingMarketRegistryCheckbox("root-trading-registry-enabled", true, true);
  tradingMarketRegistryCheckbox("root-trading-registry-allow-spot", true, true);
  tradingMarketRegistryCheckbox("root-trading-registry-allow-margin", true, true);
  tradingMarketRegistryCheckbox("root-trading-registry-allow-bots", true, true);
  tradingMarketRegistryCheckbox("root-trading-registry-allow-risk-grade", false, false);
  tradingMarketRegistryCheckbox("root-trading-registry-live-price-enabled", true, true);
  tradingMarketRegistryCheckbox("root-trading-registry-reference-enabled", true, true);
  tradingMarketRegistryCheckbox("root-trading-registry-btc-trade-enabled", false, false);
  rootTradingMarketRegistryEditorStatus("");
  clearRootTradingMarketProviderForm();
  renderRootTradingMarketProviders({});
}

function populateRootTradingMarketRegistryForm(market = null) {
  clearRootTradingMarketRegistryForm();
  if (!market || typeof market !== "object") return;
  rootTradingMarketRegistrySelectedId = Number(market.id || 0) || null;
  tradingMarketRegistryValue("root-trading-registry-symbol", rootTradingDisplayMarketSymbol(market.symbol, market.display_name || market.display_symbol || ""));
  if ($("root-trading-registry-symbol")) {
    $("root-trading-registry-symbol").disabled = true;
    $("root-trading-registry-symbol").dataset.rawSymbol = String(market.symbol || "").trim().toUpperCase();
  }
  tradingMarketRegistryValue("root-trading-registry-base-asset", market.base_asset || "");
  tradingMarketRegistryValue("root-trading-registry-quote-asset", rootTradingDisplayQuoteAsset(market.quote_asset || market.quote_currency || "USDT"));
  if ($("root-trading-registry-quote-asset")) $("root-trading-registry-quote-asset").dataset.rawQuoteAsset = String(market.quote_asset || market.quote_currency || "").trim().toUpperCase();
  tradingMarketRegistryValue("root-trading-registry-display-quote", market.display_quote_currency || "USDT");
  tradingMarketRegistryValue("root-trading-registry-display-name", market.display_name || "");
  tradingMarketRegistryValue("root-trading-registry-market-type", market.market_type || "spot");
  tradingMarketRegistryValue("root-trading-registry-sort-order", Number(market.sort_order || 9999));
  tradingMarketRegistryValue("root-trading-registry-default-manual-price", market.default_manual_price_points ?? 1);
  tradingMarketRegistryValue("root-trading-registry-price-precision", Number(market.price_precision || 8));
  tradingMarketRegistryValue("root-trading-registry-quantity-precision", Number(market.quantity_precision || 8));
  tradingMarketRegistryValue("root-trading-registry-min-order-size", market.min_order_size ?? 0.00000001);
  tradingMarketRegistryValue("root-trading-registry-max-order-size", market.max_order_size ?? 1000000);
  tradingMarketRegistryValue("root-trading-registry-lot-size", market.lot_size ?? 0.00000001);
  tradingMarketRegistryValue("root-trading-registry-tick-size", market.tick_size ?? 0.00000001);
  tradingMarketRegistryCheckbox("root-trading-registry-enabled", market.enabled, true);
  tradingMarketRegistryCheckbox("root-trading-registry-allow-spot", market.allow_spot, true);
  tradingMarketRegistryCheckbox("root-trading-registry-allow-margin", market.allow_margin, true);
  tradingMarketRegistryCheckbox("root-trading-registry-allow-bots", market.allow_bots, true);
  tradingMarketRegistryCheckbox("root-trading-registry-allow-risk-grade", market.allow_risk_grade_usage, false);
  tradingMarketRegistryCheckbox("root-trading-registry-live-price-enabled", market.live_price_enabled, true);
  tradingMarketRegistryCheckbox("root-trading-registry-reference-enabled", market.reference_price_enabled, true);
  tradingMarketRegistryCheckbox("root-trading-registry-btc-trade-enabled", market.btc_trade_enabled, false);
  const ref = market.reference_price_status || {};
  const risk = market.risk_grade_price_status || {};
  rootTradingMarketRegistryEditorStatus(
    `${rootTradingDisplayMarketSymbol(market.symbol, market.display_name || market.display_symbol)} · probe ${market.probe_status || "pending"} · seed ${market.registry_source || "-"} / v${Number(market.seed_version || 0)} / ${market.seed_sync_status || "-"} · reference ${ref.source || "-"} / ${ref.confidence || "-"} · risk-grade ${risk.source || "-"} / ${risk.confidence || "-"} / usable ${risk.risk_grade_usable ? "yes" : "no"}${risk.high_risk_blocked ? " · 已封鎖高風險用途" : ""}`
  );
  loadRootTradingMarketProviders(rootTradingMarketRegistrySelectedId);
}

function renderRootTradingMarketRegistry(payload = {}) {
  const list = $("root-trading-market-registry-list");
  if (!list) return;
  rootTradingMarketRegistryCache = Array.isArray(payload.markets) ? payload.markets : [];
  rootTradingMarketRegistryAuditCache = Array.isArray(payload.audit) ? payload.audit : [];
  if (!rootTradingMarketRegistryCache.length) {
    list.innerHTML = `<div class="drive-empty">尚無市場 registry；可用「新增市場」建立新交易對。</div>`;
    renderRootTradingMarketRegistryAudit("");
    return;
  }
  list.innerHTML = rootTradingMarketRegistryCache.map((market) => {
    const ref = market.reference_price_status || {};
    const risk = market.risk_grade_price_status || {};
    const summary = market.probe_summary || {};
    const seedVersion = Number(market.seed_version || 0);
    const catalogSeedVersion = Number(market.catalog_seed_version || 0);
    const seedStatus = market.seed_sync_status || "-";
    const seedSource = market.registry_source || "-";
    const seedReasons = Array.isArray(market.seed_sync_reasons) && market.seed_sync_reasons.length
      ? ` · drift ${sanitize(market.seed_sync_reasons.join(", "))}`
      : "";
    return `
      <div class="drive-file-row billing-catalog-row">
        <div>
          <strong>${sanitize(rootTradingDisplayMarketSymbol(market.symbol, market.display_name || market.display_symbol || ""))}</strong>
          <div class="drive-card-sub">${market.enabled ? "啟用" : "停用"} · probe ${sanitize(market.probe_status || "pending")} · provider ${Number(market.provider_count || 0)} 家</div>
          <div class="drive-card-sub">registry ${sanitize(seedSource)} · seed v${seedVersion} / catalog v${catalogSeedVersion} · status ${sanitize(seedStatus)}${seedReasons}</div>
          <div class="drive-card-sub">reference ${sanitize(ref.source || "-")} / ${sanitize(ref.confidence || "-")} · stale ${ref.stale ? "yes" : "no"} · degraded ${ref.degraded ? "yes" : "no"} · providers ${Number(ref.provider_count || 0)}</div>
          <div class="drive-card-sub">risk-grade ${sanitize(risk.source || "-")} / ${sanitize(risk.confidence || "-")} · stale ${risk.stale ? "yes" : "no"} · degraded ${risk.degraded ? "yes" : "no"} · providers ${Number(risk.provider_count || 0)} · usable ${risk.risk_grade_usable ? "yes" : "no"}${risk.high_risk_blocked ? " · blocked" : ""}</div>
          <div class="drive-card-sub">spot ${market.allow_spot ? "✓" : "×"} · margin ${market.allow_margin ? "✓" : "×"} · bot ${market.allow_bots ? "✓" : "×"} · risk-grade ${market.allow_risk_grade_usage ? "✓" : "×"} · live ${market.live_price_enabled ? "✓" : "×"} · candles ${market.reference_price_enabled ? "✓" : "×"}</div>
          ${summary.message ? `<div class="drive-card-sub" style="color:${market.probe_status === "ok" ? "#9ecbff" : "#ffcf85"};">${sanitize(summary.message)}</div>` : ""}
          ${market.seed_sync_message ? `<div class="drive-card-sub" style="color:${seedStatus === "current" ? "#9ecbff" : "#ffcf85"};">${sanitize(market.seed_sync_message)}</div>` : ""}
        </div>
        <div class="admin-toolbar" style="gap:.35rem;flex-wrap:wrap;">
          <button class="btn" type="button" data-root-trading-market-edit="${Number(market.id || 0)}">編輯</button>
          <button class="btn" type="button" data-root-trading-market-probe="${Number(market.id || 0)}">Probe</button>
          <button class="btn" type="button" data-root-trading-market-disable="${Number(market.id || 0)}">停用</button>
        </div>
      </div>
    `;
  }).join("");
  list.querySelectorAll("[data-root-trading-market-edit]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const market = rootTradingMarketRegistryCache.find((item) => Number(item.id || 0) === Number(btn.dataset.rootTradingMarketEdit || 0));
      populateRootTradingMarketRegistryForm(market || null);
    });
  });
  list.querySelectorAll("[data-root-trading-market-probe]").forEach((btn) => {
    btn.addEventListener("click", () => probeRootTradingMarketRegistry(btn.dataset.rootTradingMarketProbe || ""));
  });
  list.querySelectorAll("[data-root-trading-market-disable]").forEach((btn) => {
    btn.addEventListener("click", () => disableRootTradingMarketRegistry(btn.dataset.rootTradingMarketDisable || ""));
  });
  const selected = rootTradingMarketRegistryCache.find((item) => Number(item.id || 0) === Number(rootTradingMarketRegistrySelectedId || 0));
  if (selected) renderRootTradingMarketRegistryAudit(selected.symbol || "");
}

async function loadRootTradingMarketRegistry(options = {}) {
  if (currentUser !== "root" || !$("root-trading-market-registry-list")) return;
  if (!options.silent) rootTradingMarketRegistryMsg("交易市場 registry 讀取中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + "/admin/trading/markets?include_disabled=1", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingMarketRegistryMsg(rootTradingSettingsHttpMessage(res, json, "交易市場 registry 讀取失敗"), false);
      return;
    }
    renderRootTradingMarketRegistry(json);
    if (rootTradingMarketRegistrySelectedId) {
      const selected = rootTradingMarketRegistryCache.find((item) => Number(item.id || 0) === Number(rootTradingMarketRegistrySelectedId));
      if (selected) {
        populateRootTradingMarketRegistryForm(selected);
      } else {
        clearRootTradingMarketRegistryForm();
      }
    } else {
      renderRootTradingMarketRegistryAudit("");
    }
    rootTradingMarketRegistryMsg("");
  } catch (err) {
    rootTradingMarketRegistryMsg(err.message || "交易市場 registry 讀取請求失敗", false);
  }
}

async function loadRootTradingMarketProviders(marketId) {
  if (currentUser !== "root" || !marketId) return;
  rootTradingMarketProviderStatus("provider mapping 讀取中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/admin/trading/markets/${encodeURIComponent(marketId)}/providers`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingMarketProviderStatus(rootTradingSettingsHttpMessage(res, json, "provider mapping 讀取失敗"), false);
      return;
    }
    renderRootTradingMarketProviders(json);
    rootTradingMarketProviderStatus("");
  } catch (err) {
    rootTradingMarketProviderStatus(err.message || "provider mapping 讀取請求失敗", false);
  }
}

function collectRootTradingMarketRegistryForm() {
  const symbolInput = $("root-trading-registry-symbol");
  const quoteInput = $("root-trading-registry-quote-asset");
  const editing = Number(rootTradingMarketRegistrySelectedId || 0) > 0;
  return {
    symbol: ((editing && symbolInput?.dataset.rawSymbol) || symbolInput?.value || "").trim().toUpperCase(),
    base_asset: ($("root-trading-registry-base-asset")?.value || "").trim().toUpperCase(),
    quote_asset: ((editing && quoteInput?.dataset.rawQuoteAsset) || quoteInput?.value || "").trim().toUpperCase(),
    display_quote_currency: ($("root-trading-registry-display-quote")?.value || "").trim().toUpperCase(),
    display_name: ($("root-trading-registry-display-name")?.value || "").trim(),
    market_type: ($("root-trading-registry-market-type")?.value || "spot").trim(),
    sort_order: Number($("root-trading-registry-sort-order")?.value || 9999),
    default_manual_price_points: Number($("root-trading-registry-default-manual-price")?.value || 1),
    price_precision: Number($("root-trading-registry-price-precision")?.value || 8),
    quantity_precision: Number($("root-trading-registry-quantity-precision")?.value || 8),
    min_order_size: Number($("root-trading-registry-min-order-size")?.value || 0.00000001),
    max_order_size: Number($("root-trading-registry-max-order-size")?.value || 1000000),
    lot_size: Number($("root-trading-registry-lot-size")?.value || 0.00000001),
    tick_size: Number($("root-trading-registry-tick-size")?.value || 0.00000001),
    enabled: !!$("root-trading-registry-enabled")?.checked,
    allow_spot: !!$("root-trading-registry-allow-spot")?.checked,
    allow_margin: !!$("root-trading-registry-allow-margin")?.checked,
    allow_bots: !!$("root-trading-registry-allow-bots")?.checked,
    allow_risk_grade_usage: !!$("root-trading-registry-allow-risk-grade")?.checked,
    live_price_enabled: !!$("root-trading-registry-live-price-enabled")?.checked,
    reference_price_enabled: !!$("root-trading-registry-reference-enabled")?.checked,
    btc_trade_enabled: !!$("root-trading-registry-btc-trade-enabled")?.checked,
  };
}

function collectRootTradingMarketProviderForm() {
  return {
    provider: ($("root-trading-market-provider-name")?.value || "").trim(),
    provider_symbol: ($("root-trading-market-provider-symbol")?.value || "").trim(),
    priority: Number($("root-trading-market-provider-priority")?.value || 100),
    enabled: !!$("root-trading-market-provider-enabled")?.checked,
    supports_ticker: !!$("root-trading-market-provider-ticker")?.checked,
    supports_depth: !!$("root-trading-market-provider-depth")?.checked,
    supports_candles: !!$("root-trading-market-provider-candles")?.checked,
  };
}

async function saveRootTradingMarketRegistry() {
  if (currentUser !== "root") return;
  const payload = collectRootTradingMarketRegistryForm();
  if (!payload.symbol && payload.base_asset && payload.quote_asset) {
    payload.symbol = `${payload.base_asset}/${payload.quote_asset}`;
  }
  if (!payload.symbol) {
    rootTradingMarketRegistryEditorStatus("請先填寫市場 symbol", false);
    return;
  }
  rootTradingMarketRegistryEditorStatus("市場儲存中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const editingId = Number(rootTradingMarketRegistrySelectedId || 0);
  const url = editingId ? `${API}/admin/trading/markets/${editingId}` : `${API}/admin/trading/markets`;
  const method = editingId ? "PUT" : "POST";
  try {
    const res = await apiFetch(url, {
      method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload),
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingMarketRegistryEditorStatus(rootTradingSettingsHttpMessage(res, json, "市場儲存失敗"), false);
      return;
    }
    const market = json.market || null;
    rootTradingMarketRegistrySelectedId = Number(market?.id || rootTradingMarketRegistrySelectedId || 0) || null;
    await loadRootTradingMarketRegistry({ silent: true });
    if (rootTradingMarketRegistrySelectedId) await loadRootTradingMarketProviders(rootTradingMarketRegistrySelectedId);
    rootTradingMarketRegistryMsg(editingId ? "市場已更新" : "市場已建立");
    rootTradingMarketRegistryEditorStatus(market?.probe_summary?.message || "市場已儲存");
  } catch (err) {
    rootTradingMarketRegistryEditorStatus(err.message || "市場儲存請求失敗", false);
  }
}

async function probeRootTradingMarketRegistry(explicitId = "") {
  const marketId = Number(explicitId || rootTradingMarketRegistrySelectedId || 0);
  if (!marketId) {
    rootTradingMarketRegistryEditorStatus("請先選擇市場再執行 probe", false);
    return;
  }
  rootTradingMarketRegistryEditorStatus("provider probe 執行中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/admin/trading/markets/${encodeURIComponent(marketId)}/probe`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingMarketRegistryEditorStatus(rootTradingSettingsHttpMessage(res, json, "provider probe 失敗"), false);
      return;
    }
    rootTradingMarketRegistrySelectedId = marketId;
    await loadRootTradingMarketRegistry({ silent: true });
    if (rootTradingMarketRegistrySelectedId) await loadRootTradingMarketProviders(rootTradingMarketRegistrySelectedId);
    rootTradingMarketRegistryEditorStatus(json.probe?.message || "provider probe 完成");
  } catch (err) {
    rootTradingMarketRegistryEditorStatus(err.message || "provider probe 請求失敗", false);
  }
}

async function disableRootTradingMarketRegistry(explicitId = "") {
  const marketId = Number(explicitId || rootTradingMarketRegistrySelectedId || 0);
  if (!marketId) {
    rootTradingMarketRegistryEditorStatus("請先選擇市場再停用", false);
    return;
  }
  const market = rootTradingMarketRegistryCache.find((item) => Number(item.id || 0) === marketId);
  if (!confirm(`停用 ${market?.display_name || market?.symbol || "此市場"}？既有歷史仍保留，但之後不能再下單。`)) return;
  rootTradingMarketRegistryEditorStatus("市場停用中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/admin/trading/markets/${encodeURIComponent(marketId)}/disable`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingMarketRegistryEditorStatus(rootTradingSettingsHttpMessage(res, json, "市場停用失敗"), false);
      return;
    }
    await loadRootTradingMarketRegistry({ silent: true });
    if (rootTradingMarketRegistrySelectedId === marketId) {
      populateRootTradingMarketRegistryForm(json.market || null);
    }
    rootTradingMarketRegistryMsg("市場已停用");
    rootTradingMarketRegistryEditorStatus("disabled market 不會破壞既有歷史，但之後不可下單。");
  } catch (err) {
    rootTradingMarketRegistryEditorStatus(err.message || "市場停用請求失敗", false);
  }
}

async function saveRootTradingMarketProvider() {
  if (currentUser !== "root") return;
  const marketId = Number(rootTradingMarketRegistrySelectedId || 0);
  if (!marketId) {
    rootTradingMarketProviderStatus("請先選擇市場再儲存 provider mapping", false);
    return;
  }
  const payload = collectRootTradingMarketProviderForm();
  rootTradingMarketProviderStatus("provider mapping 儲存中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const mappingId = Number(rootTradingMarketProviderSelectedId || 0);
  const url = mappingId
    ? `${API}/admin/trading/markets/${marketId}/providers/${mappingId}`
    : `${API}/admin/trading/markets/${marketId}/providers`;
  const method = mappingId ? "PUT" : "POST";
  try {
    const res = await apiFetch(url, {
      method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload),
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingMarketProviderStatus(rootTradingSettingsHttpMessage(res, json, "provider mapping 儲存失敗"), false);
      return;
    }
    clearRootTradingMarketProviderForm();
    renderRootTradingMarketProviders(json);
    await loadRootTradingMarketRegistry({ silent: true });
    rootTradingMarketProviderStatus(mappingId ? "provider mapping 已更新" : "provider mapping 已建立");
  } catch (err) {
    rootTradingMarketProviderStatus(err.message || "provider mapping 儲存請求失敗", false);
  }
}

async function disableRootTradingMarketProvider(mappingIdValue = "") {
  if (currentUser !== "root") return;
  const marketId = Number(rootTradingMarketRegistrySelectedId || 0);
  const mappingId = Number(mappingIdValue || 0);
  if (!marketId || !mappingId) {
    rootTradingMarketProviderStatus("請先選擇要停用的 provider mapping", false);
    return;
  }
  const row = rootTradingMarketProviderCache.find((item) => Number(item.id || 0) === mappingId);
  if (!confirm(`停用 ${row?.provider_label || row?.provider || "此"} provider mapping？`)) return;
  rootTradingMarketProviderStatus("provider mapping 停用中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/admin/trading/markets/${marketId}/providers/${mappingId}`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingMarketProviderStatus(rootTradingSettingsHttpMessage(res, json, "provider mapping 停用失敗"), false);
      return;
    }
    clearRootTradingMarketProviderForm();
    renderRootTradingMarketProviders(json);
    await loadRootTradingMarketRegistry({ silent: true });
    rootTradingMarketProviderStatus("provider mapping 已停用");
  } catch (err) {
    rootTradingMarketProviderStatus(err.message || "provider mapping 停用請求失敗", false);
  }
}

function bindRootTradingMarketRegistryControls() {
  const refreshBtn = $("root-trading-market-registry-refresh-btn");
  if (refreshBtn && !refreshBtn.dataset.registryBound) {
    refreshBtn.addEventListener("click", () => loadRootTradingMarketRegistry());
    refreshBtn.dataset.registryBound = "1";
  }
  const newBtn = $("root-trading-market-registry-new-btn");
  if (newBtn && !newBtn.dataset.registryBound) {
    newBtn.addEventListener("click", () => {
      clearRootTradingMarketRegistryForm();
      rootTradingMarketRegistryEditorStatus("正在建立新市場；symbol 會成為不可變的內部市場代號。");
    });
    newBtn.dataset.registryBound = "1";
  }
  const saveBtn = $("root-trading-market-registry-save-btn");
  if (saveBtn && !saveBtn.dataset.registryBound) {
    saveBtn.addEventListener("click", saveRootTradingMarketRegistry);
    saveBtn.dataset.registryBound = "1";
  }
  const probeBtn = $("root-trading-market-registry-probe-btn");
  if (probeBtn && !probeBtn.dataset.registryBound) {
    probeBtn.addEventListener("click", () => probeRootTradingMarketRegistry());
    probeBtn.dataset.registryBound = "1";
  }
  const disableBtn = $("root-trading-market-registry-disable-btn");
  if (disableBtn && !disableBtn.dataset.registryBound) {
    disableBtn.addEventListener("click", () => disableRootTradingMarketRegistry());
    disableBtn.dataset.registryBound = "1";
  }
  const cancelBtn = $("root-trading-market-registry-cancel-btn");
  if (cancelBtn && !cancelBtn.dataset.registryBound) {
    cancelBtn.addEventListener("click", clearRootTradingMarketRegistryForm);
    cancelBtn.dataset.registryBound = "1";
  }
  const providerSaveBtn = $("root-trading-market-provider-save-btn");
  if (providerSaveBtn && !providerSaveBtn.dataset.registryBound) {
    providerSaveBtn.addEventListener("click", saveRootTradingMarketProvider);
    providerSaveBtn.dataset.registryBound = "1";
  }
  const providerCancelBtn = $("root-trading-market-provider-cancel-btn");
  if (providerCancelBtn && !providerCancelBtn.dataset.registryBound) {
    providerCancelBtn.addEventListener("click", clearRootTradingMarketProviderForm);
    providerCancelBtn.dataset.registryBound = "1";
  }
}

function tradingBotAuditColor(status) {
  if (status === "red") return "#ff6b81";
  if (status === "yellow") return "#ffb347";
  if (status === "green") return "#4caf50";
  return "#9ecbff";
}

function renderRootTradingPriceFusionMarketOptions(payload) {
  const select = $("root-trading-price-fusion-market");
  if (!select) return;
  const settings = payload?.settings || {};
  const liveMarkets = Array.isArray(settings.price_fusion_live_markets) ? settings.price_fusion_live_markets : [];
  const labels = new Map(
    (Array.isArray(payload?.markets) ? payload.markets : [])
      .filter((market) => liveMarkets.includes(market.symbol))
      .map((market) => [market.symbol, market.display_symbol || market.symbol])
  );
  const options = liveMarkets.map((symbol) => ({ symbol, label: rootTradingDisplayMarketSymbol(symbol, labels.get(symbol) || "") }));
  const previous = select.value;
  if (!options.length) {
    select.innerHTML = `<option value="">沒有支援融合價格的市場</option>`;
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = options.map((item) => `
    <option value="${sanitize(item.symbol)}">${sanitize(item.label)}</option>
  `).join("");
  select.value = options.some((item) => item.symbol === previous) ? previous : options[0].symbol;
}

function renderRootTradingPriceFusionStatus(status = {}) {
  rootTradingPriceFusionStatusCache = status || {};
  const summary = $("root-trading-price-fusion-summary");
  const providers = $("root-trading-price-fusion-provider-list");
  const excluded = $("root-trading-price-fusion-excluded-list");
  if (!summary || !providers || !excluded) return;
  const formatNumber = (value, digits = 4) => {
    if (value == null || value === "" || Number.isNaN(Number(value))) return "-";
    return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
  };
  const state = String(status.state || "inactive");
  const resolvedSource = String(status.resolved_source || "-");
  const resolvedMode = String(status.resolved_mode || status.requested_mode || "-");
  const requestedMarketSymbol = String(status.requested_market_symbol || "");
  const resolvedMarketSymbol = String(status.resolved_market_symbol || status.market_symbol || "");
  const displayMarketSymbol = rootTradingDisplayMarketSymbol(status.market_symbol || "", status.display_market_symbol || status.market_symbol || "-");
  const pricePoints = status.price_points == null ? "-" : rootTradingPointsUnitText(status.price_points, 8);
  const weightsSum = Number(status.weights_sum_percent || 0).toFixed(2);
  const depthLevels = Number(status.depth_levels || 0);
  const bandPercent = Number(status.depth_band_percent || 0);
  const minCoveragePercent = Number(status.min_orderbook_coverage_percent || 0);
  const minProviderCount = Number(status.min_provider_count || 0);
  const providerCap = Number(status.max_single_provider_weight_percent || 0).toFixed(2);
  const medianMidpoint = status.median_midpoint_points == null ? "-" : `${formatNumber(status.median_midpoint_points, 8)} 點`;
  const usedRows = Array.isArray(status.providers_used) ? status.providers_used : [];
  const excludedRows = Array.isArray(status.excluded_providers) ? status.excluded_providers : [];
  const warnings = Array.isArray(status.warnings) ? status.warnings : [];
  const transportState = status.transport_state && typeof status.transport_state === "object" ? status.transport_state : {};
  const message = String(status.message || "").trim();
  const riskEligibleRows = usedRows.filter((row) => row && row.risk_grade_eligible);
  const referenceSourceText = usedRows.length === 1
    ? `reference 來源：${usedRows[0]?.label || usedRows[0]?.source || "-"}`
    : `reference 來源：${usedRows.map((row) => row.label || row.source || "-").join("、") || "0 家"}`;
  const qualifiedSourceText = riskEligibleRows.length === 1
    ? `唯一合格來源：${riskEligibleRows[0]?.label || riskEligibleRows[0]?.source || "-"}`
    : `風控級合格來源：${riskEligibleRows.map((row) => row.label || row.source || "-").join("、") || "0 家"}`;
  const providerCountSummary = `reference 可用來源 ${Number(status.reference_provider_count || usedRows.length)}/${usedRows.length || 0} · 風控級 ${Number(status.risk_grade_provider_count || riskEligibleRows.length)}/${minProviderCount || 0}`;
  const stateLabel = state === "healthy"
    ? "正常"
    : state === "conservative"
      ? "價格來源降級"
      : state === "degraded"
        ? "部分來源排除"
        : state === "unsupported"
          ? "市場不支援"
          : "未啟用融合";
  summary.innerHTML = `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(displayMarketSymbol)}</strong>
        <div class="drive-card-sub">狀態 ${sanitize(stateLabel)} · 目前價格 ${sanitize(pricePoints)}</div>
        ${requestedMarketSymbol || resolvedMarketSymbol ? `<div class="drive-card-sub">請求市場 ${sanitize(rootTradingDisplayMarketSymbol(requestedMarketSymbol || "（未指定）"))} · 市場 ${sanitize(rootTradingDisplayMarketSymbol(resolvedMarketSymbol || "-"))}</div>` : ""}
        <div class="drive-card-sub">設定來源 ${sanitize(status.configured_source || "-")} · 實際來源 ${sanitize(resolvedSource)} · reference 模式 ${sanitize(status.reference_mode || resolvedMode)} · 風控級模式 ${sanitize(status.risk_grade_mode || "-")}</div>
        <div class="drive-card-sub">reference 價格 ${sanitize(status.reference_price_points == null ? "-" : `${formatNumber(status.reference_price_points, 8)} 點`)} · 風控級價格 ${sanitize(status.risk_grade_price_points == null ? "-" : `${formatNumber(status.risk_grade_price_points, 8)} 點`)} · reference 權重合計 ${sanitize(Number(status.reference_weights_sum_percent || weightsSum).toFixed(2))}% · 風控級權重合計 ${sanitize(Number(status.risk_grade_weights_sum_percent || 0).toFixed(2))}%</div>
        <div class="drive-card-sub">每家最多採樣 ${sanitize(String(depthLevels || "-"))} 檔 · 目標深度區間 ±${sanitize(String(bandPercent || "-"))}% · 最低覆蓋門檻 ${sanitize(String(minCoveragePercent || "-"))}% · 最少來源 ${sanitize(String(minProviderCount || "-"))} 家 · 單一來源上限 ${sanitize(providerCap)}% · 中位 midpoint ${sanitize(medianMidpoint)}</div>
        <div class="drive-card-sub">${sanitize(referenceSourceText)} · ${sanitize(qualifiedSourceText)} · ${sanitize(providerCountSummary)}</div>
        <div class="drive-card-sub">provider input ${sanitize(String(transportState.mode || "http_polling_only"))} · 連線 ${transportState.connected ? "connected" : "disconnected"} · fallback ${transportState.fallback ? "HTTP polling" : "no"} · stale ${transportState.stale ? "yes" : "no"} · 信心 ${sanitize(String(transportState.confidence || "-"))} · provider_count ${sanitize(String(transportState.provider_count ?? 0))}${transportState.last_update_at ? ` · last update ${sanitize(String(transportState.last_update_at))}` : ""}</div>
        ${transportState.exclusion_reason ? `<div class="drive-card-sub" style="color:#ffcf85;">provider input exclusion：${sanitize(String(transportState.exclusion_reason || ""))}</div>` : ""}
        ${message ? `<div class="drive-card-sub" style="color:${state === "healthy" ? "#9ecbff" : "#ffb347"};">${sanitize(message)}</div>` : ""}
        ${transportState.message ? `<div class="drive-card-sub" style="color:${transportState.degraded ? "#ffb347" : "#9ecbff"};">${sanitize(String(transportState.message || ""))}</div>` : ""}
        ${warnings.length ? `<div class="drive-card-sub" style="color:#ffcf85;">${warnings.map((warning) => sanitize(String(warning?.message || warning?.code || ""))).filter(Boolean).join("；")}</div>` : ""}
        ${status.conservative_mode ? `<div class="drive-card-sub" style="color:#ff9aa8;">已進入保守模式：目前不是正常 fused price，僅能作為 degraded reference price，不建議高風險交易。</div>` : ""}
        <div class="drive-card-sub" style="color:#ffcf85;">目前這套 auto_depth 融合較適合作為 v1 reference price 與流動性 sanity check，不建議單獨作為強平、機器人或實際成交的唯一依據。</div>
      </div>
    </div>
  `;
  providers.innerHTML = usedRows.length
    ? usedRows.map((row) => `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(row.label || row.source || "-")}</strong>
          <div class="drive-card-sub">reference 占比 ${Number((row.reference_weight_percent ?? row.normalized_weight_percent) || 0).toFixed(2)}% · 風控級占比 ${Number(row.risk_grade_weight_percent || 0).toFixed(2)}% · 價格 ${formatNumber(row.price_points, 8)} 點 · midpoint ${formatNumber(row.midpoint_points, 8)}</div>
          <div class="drive-card-sub">best bid ${formatNumber(row.best_bid_points, 8)} · best ask ${formatNumber(row.best_ask_points, 8)} · spread ${formatNumber(row.spread_percent, 6)}%</div>
          <div class="drive-card-sub">bid notional ${formatNumber(row.bid_notional_points, 4)} · ask notional ${formatNumber(row.ask_notional_points, 4)} · depth score ${formatNumber(row.depth_score, 4)} · density ${formatNumber(row.depth_density_score, 4)}</div>
          <div class="drive-card-sub">bid coverage ${formatNumber(row.bid_coverage_percent, 6)}%${row.bid_reached_lower_bound ? " ✓" : ""} · ask coverage ${formatNumber(row.ask_coverage_percent, 6)}%${row.ask_reached_upper_bound ? " ✓" : ""} · ${row.orderbook_truncated ? "coverage truncated" : "coverage complete"}</div>
          <div class="drive-card-sub">midpoint deviation ${formatNumber(row.midpoint_deviation_percent, 6)}% · age ${formatNumber(row.age_seconds, 3)}s · latency ${formatNumber(row.latency_ms, 2)}ms</div>
          <div class="drive-card-sub">raw levels bid/ask ${Number(row.raw_bid_levels_count || 0)}/${Number(row.raw_ask_levels_count || 0)} · used ${Number(row.used_bid_levels_count || 0)}/${Number(row.used_ask_levels_count || 0)} · balance ${formatNumber(row.side_balance_ratio_percent, 4)}%</div>
          <div class="drive-card-sub">數量單位 ${sanitize(row.quantity_unit_label || row.quantity_unit || "-")} · raw ${Number(row.raw_normalized_weight_percent || 0).toFixed(2)}% · effective score ${formatNumber(row.effective_depth_score, 4)}${row.weight_cap_applied ? ` · capped to ${Number((row.reference_weight_percent ?? row.normalized_weight_percent) || 0).toFixed(2)}%` : ""}</div>
          <div class="drive-card-sub">provider depth limit ${Number(row.provider_depth_request_limit || 0)}${row.provider_depth_limit_reached ? " · provider depth limit reached" : ""}</div>
          ${row.coverage_warning_message ? `<div class="drive-card-sub" style="color:#ffcf85;">${sanitize(row.coverage_warning_message)}</div>` : ""}
          ${row.risk_grade_eligible ? "" : `<div class="drive-card-sub" style="color:#ff9aa8;">coverage 未達門檻，已排除風控級權重；資料截斷，不代表該交易所真實深度不足。</div>`}
        </div>
      </div>
    `).join("")
    : `<div class="drive-empty">目前沒有可用的融合來源</div>`;
  excluded.innerHTML = excludedRows.length
    ? excludedRows.map((row) => {
      const reason = row.reason === "fetch_failed"
        ? `API 失效 / timeout / 格式錯誤：${row.error || "未提供細節"}`
        : row.reason === "manual_weight_zero"
          ? "手動權重為 0，因此不參與融合"
          : row.reason === "midpoint_deviation_exceeded"
            ? `midpoint deviation 過大：${row.error || "已排除"}`
            : row.reason === "one_sided_depth"
              ? `單邊深度過高：${row.error || "已排除"}`
              : row.reason === "insufficient_coverage"
                ? `深度覆蓋不足：${row.error || "已排除"}`
              : row.reason === "latency_too_high"
                ? `order book latency 過高：${row.error || "已排除"}`
                : row.reason === "stale_orderbook"
                  ? `order book stale：${row.error || "已排除"}`
          : row.error || row.reason || "已排除";
      return `
        <div class="drive-file-row">
          <div>
            <strong>${sanitize(row.label || row.source || "-")}</strong>
            <div class="drive-card-sub">${sanitize(reason)}</div>
            ${row.best_bid_points != null ? `<div class="drive-card-sub">best bid ${sanitize(formatNumber(row.best_bid_points, 8))} · best ask ${sanitize(formatNumber(row.best_ask_points, 8))} · spread ${sanitize(formatNumber(row.spread_percent, 6))}%</div>` : ""}
            ${row.bid_notional_points != null ? `<div class="drive-card-sub">bid notional ${sanitize(formatNumber(row.bid_notional_points, 4))} · ask notional ${sanitize(formatNumber(row.ask_notional_points, 4))} · bid coverage ${sanitize(formatNumber(row.bid_coverage_percent, 6))}% · ask coverage ${sanitize(formatNumber(row.ask_coverage_percent, 6))}%</div>` : ""}
            ${row.bid_notional_points != null ? `<div class="drive-card-sub">deviation ${sanitize(formatNumber(row.midpoint_deviation_percent, 6))}% · latency ${sanitize(formatNumber(row.latency_ms, 2))}ms · ${row.orderbook_truncated ? "coverage truncated" : "coverage complete"}${row.provider_depth_limit_reached ? " · provider depth limit reached" : ""}</div>` : ""}
          </div>
        </div>
      `;
    }).join("")
    : `<div class="drive-empty">目前沒有被排除的來源</div>`;
}

async function loadRootTradingPriceFusionStatus() {
  if (currentUser !== "root" || !$("root-trading-price-fusion-summary")) return;
  const selectedMarket = $("root-trading-price-fusion-market")?.value || "";
  if (!selectedMarket) {
    renderRootTradingPriceFusionStatus({
      state: "unsupported",
      market_symbol: "",
      message: "尚無支援融合價格的市場可供檢查。",
      providers_used: [],
      excluded_providers: [],
    });
    return;
  }
  rootTradingPriceFusionMsg("正在抓取目前生效中的融合價格占比...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/root/trading/price-fusion-status?market_symbol=${encodeURIComponent(selectedMarket)}`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingPriceFusionMsg(rootTradingSettingsHttpMessage(res, json, "融合價格診斷讀取失敗"), false);
      return;
    }
    renderRootTradingPriceFusionStatus(json.status || {});
    rootTradingPriceFusionMsg(
      json.status?.conservative_mode
        ? "已抓取融合價格占比，但目前處於價格來源降級/保守模式。"
        : "已更新目前生效中的融合價格占比。"
    );
  } catch (err) {
    rootTradingPriceFusionMsg(err.message || "融合價格診斷請求失敗", false);
  }
}

function renderRootTradingBotAuditDashboard(dashboard = {}, reports = []) {
  rootTradingBotAuditCache = dashboard || {};
  const summary = $("root-trading-bot-audit-summary");
  const bots = $("root-trading-bot-audit-bots");
  const bugReports = $("root-trading-bot-audit-bug-reports");
  if (!summary || !bots || !bugReports) return;
  const counts = dashboard.summary || {};
  summary.innerHTML = `
    <div class="drive-file-row">
      <div>
        <strong>稽核總覽</strong>
        <div class="drive-card-sub">未稽核 ${Number(counts.unaudited || 0)} · 綠燈 ${Number(counts.green || 0)} · 黃燈 ${Number(counts.yellow || 0)} · 紅燈 ${Number(counts.red || 0)}</div>
        <div class="drive-card-sub">自動稽核 ${dashboard.settings?.bot_audit_enabled === false ? "停用" : "啟用"} · 間隔 ${Number(dashboard.settings?.bot_audit_interval_seconds || 0)} 秒 · 納入條件：首筆成交或啟用滿 ${Math.round(Number(dashboard.settings?.bot_audit_min_enabled_seconds || 0) / 3600)} 小時</div>
      </div>
    </div>
  `;
  const items = Array.isArray(dashboard.items) ? dashboard.items.slice() : [];
  items.sort((a, b) => {
    const rank = { red: 0, yellow: 1, unaudited: 2, green: 3 };
    return (rank[a.audit_status] ?? 9) - (rank[b.audit_status] ?? 9);
  });
  bots.innerHTML = items.length ? items.map((item) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(item.name || item.display_symbol || item.market_symbol || "-")}</strong>
        <span style="display:inline-block;margin-left:.4rem;padding:.1rem .45rem;border-radius:999px;background:${tradingBotAuditColor(item.audit_status)}22;color:${tradingBotAuditColor(item.audit_status)};font-size:.78rem;">${sanitize(item.audit_label || "未稽核")}</span>
        <div class="drive-card-sub">${sanitize(item.username || "-")} · ${sanitize(item.display_symbol || item.market_symbol || "-")} · ${item.bot_kind === "grid_bot" ? "網格機器人" : "交易機器人"}</div>
        <div class="drive-card-sub">${sanitize(item.eligible_reason_label || "")}</div>
        <div class="drive-card-sub">
          ${item.bot_kind === "grid_bot"
            ? `總成交 ${Number(item.total_trades || 0)} · 開單 ${Number(item.open_order_count || 0)}`
            : `已成交觸發 ${Number(item.triggered_run_count || 0)} · 執行次數 ${Number(item.run_count || 0)}`
          }
          · 最近稽核 ${sanitize(item.last_audited_at || "尚未稽核")}
        </div>
        ${item.last_error ? `<div class="drive-card-sub" style="color:#ffb347;">最近錯誤：${sanitize(item.last_error)}</div>` : ""}
      </div>
    </div>
  `).join("") : `<div class="drive-empty">目前沒有交易機器人</div>`;
  const tradingReports = (Array.isArray(reports) ? reports : []).filter((item) => {
    const feature = String(item.feature || "").toLowerCase();
    const title = String(item.title || "").toLowerCase();
    return feature === "trading" || title.includes("交易");
  });
  bugReports.innerHTML = tradingReports.length ? tradingReports.slice(0, 20).map((item) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(item.title || item.id || "-")}</strong>
        <div class="drive-card-sub">${sanitize(item.status || "-")} · ${sanitize(item.severity || "-")} · ${sanitize(item.reporter || "-")} · ${sanitize(item.created_at || "")}</div>
        <div class="drive-card-sub">${sanitize(item.feature || "trading")}</div>
      </div>
      <div class="admin-toolbar" style="gap:.35rem;flex-wrap:wrap;">
        <button class="btn btn-sm" type="button" data-trading-audit-bug-approve="${sanitize(item.id || "")}">核准</button>
        <button class="btn btn-sm btn-danger" type="button" data-trading-audit-bug-reject="${sanitize(item.id || "")}">駁回</button>
      </div>
    </div>
  `).join("") : `<div class="drive-empty">目前沒有交易相關 bug 回報</div>`;
  bugReports.querySelectorAll("[data-trading-audit-bug-approve]").forEach((btn) => {
    btn.addEventListener("click", () => reviewTradingAuditBugReport(btn.dataset.tradingAuditBugApprove, "approve"));
  });
  bugReports.querySelectorAll("[data-trading-audit-bug-reject]").forEach((btn) => {
    btn.addEventListener("click", () => reviewTradingAuditBugReport(btn.dataset.tradingAuditBugReject, "reject"));
  });
}

async function loadRootTradingBotAuditDashboard() {
  if (currentUser !== "root" || !$("root-trading-bot-audit-summary")) return;
  rootTradingBotAuditMsg("交易機器人稽核 dashboard 載入中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const [dashRes, bugRes] = await Promise.all([
      apiFetch(API + "/root/trading/bot-audit/dashboard", {
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf || "" },
      }),
      apiFetch(API + "/admin/bug-reports", {
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf || "" },
      }),
    ]);
    const dashJson = await parseRootTradingSettingsResponse(dashRes);
    const bugJson = await parseRootTradingSettingsResponse(bugRes);
    if (!dashRes.ok || !dashJson.ok) {
      rootTradingBotAuditMsg(rootTradingSettingsHttpMessage(dashRes, dashJson, "交易機器人稽核 dashboard 讀取失敗"), false);
      return;
    }
    renderRootTradingBotAuditDashboard(dashJson.dashboard || {}, Array.isArray(bugJson?.reports) ? bugJson.reports : []);
    rootTradingBotAuditMsg("已更新交易機器人稽核 dashboard。");
  } catch (err) {
    rootTradingBotAuditMsg(err.message || "交易機器人稽核 dashboard 請求失敗", false);
  }
}

async function runRootTradingBotAudit(force = true) {
  if (currentUser !== "root") return;
  rootTradingBotAuditMsg("交易機器人稽核執行中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + "/root/trading/bot-audit/run", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({ force }),
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingBotAuditMsg(rootTradingSettingsHttpMessage(res, json, "交易機器人稽核執行失敗"), false);
      return;
    }
    rootTradingBotAuditMsg(`交易機器人稽核完成：已稽核 ${Number((json.audited || []).length)} 個，略過 ${Number((json.skipped || []).length)} 個。`);
    await loadRootTradingBotAuditDashboard();
  } catch (err) {
    rootTradingBotAuditMsg(err.message || "交易機器人稽核執行請求失敗", false);
  }
}

async function reviewTradingAuditBugReport(reportId, decision) {
  if (currentUser !== "root" || !reportId) return;
  const reviewNote = window.prompt(decision === "approve" ? "核准原因（可留空）" : "駁回原因（可留空）", "") || "";
  let rewardPoints = 0;
  if (decision === "approve") {
    const rewardRaw = window.prompt("root 核定獎勵點數（0 代表核准但不發獎勵）", "0");
    if (rewardRaw === null) return;
    rewardPoints = Number.parseInt(rewardRaw, 10);
    if (!Number.isFinite(rewardPoints) || rewardPoints < 0) {
      rootTradingBotAuditMsg("獎勵點數必須是 0 或正整數。", false);
      return;
    }
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/admin/bug-reports/${encodeURIComponent(reportId)}/review`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({ decision, review_note: reviewNote, reward_points: rewardPoints }),
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingBotAuditMsg(rootTradingSettingsHttpMessage(res, json, "交易 bug 回報審核失敗"), false);
      return;
    }
    rootTradingBotAuditMsg(`交易 bug 回報 ${sanitize(reportId)} 已${decision === "approve" ? "核准" : "駁回"}。`);
    await loadRootTradingBotAuditDashboard();
  } catch (err) {
    rootTradingBotAuditMsg(err.message || "交易 bug 回報審核請求失敗", false);
  }
}

async function parseRootTradingSettingsResponse(res) {
  const json = await res.clone().json().catch(() => null);
  if (json && typeof json === "object") return json;
  const text = await res.text().catch(() => "");
  const fallback = text && text.length < 160 ? text.trim() : "";
  return {
    ok: false,
    msg: fallback || `HTTP ${res.status}`,
  };
}

function rootTradingSettingsHttpMessage(res, json, fallback) {
  if (res.status === 404) {
    return "交易所參數 API 找不到。請重新整理頁面，並確認目前啟動的是 03.Economy 版本伺服器。";
  }
  return json?.msg || fallback || `HTTP ${res.status}`;
}

function renderRootTradingFusionWeightInputs(settings = {}) {
  const container = $("root-trading-price-fusion-weights");
  if (!container) return;
  const providers = Array.isArray(settings.price_fusion_providers) && settings.price_fusion_providers.length
    ? settings.price_fusion_providers
    : ["binance_public_api", "okx_public_api", "coinbase_exchange", "kraken_public_api", "gemini_public_api", "bitstamp_public_api"];
  const labels = settings.price_fusion_provider_labels && typeof settings.price_fusion_provider_labels === "object"
    ? settings.price_fusion_provider_labels
    : {};
  const weights = settings.price_fusion_manual_weights && typeof settings.price_fusion_manual_weights === "object"
    ? settings.price_fusion_manual_weights
    : {};
  container.innerHTML = providers.map((provider) => `
    <label class="trading-fusion-weight-chip">
      <span class="trading-fusion-weight-label">${sanitize(labels[provider] || provider)}</span>
      <span class="trading-fusion-weight-input-wrap">
        <input type="number" min="0" max="1000" step="0.1" data-trading-price-weight="${sanitize(provider)}" value="${Number(weights[provider] ?? 1)}" />
        <span class="trading-fusion-weight-unit">%</span>
      </span>
    </label>
  `).join("");
}

function toggleRootTradingPriceFusionControls() {
  const source = $("root-trading-price-source")?.value || "binance_public_api";
  const mode = $("root-trading-price-fusion-mode")?.value || "auto_depth";
  const modeField = $("root-trading-price-fusion-mode-field");
  const weightsField = $("root-trading-price-fusion-weights-field");
  const note = $("root-trading-price-source-note");
  const fusionEnabled = source === "fused_weighted";
  if (modeField) modeField.hidden = !fusionEnabled;
  if (weightsField) weightsField.hidden = !(fusionEnabled && mode === "manual_weights");
  if (note) {
    if (note.dataset.rawInvalidPriceSource) {
      note.textContent = note.dataset.rawInvalidPriceSource;
      note.style.color = "#ffcf85";
    } else if (source === "manual_root") {
      note.textContent = rootTradingManualPriceAllowedInCurrentMode()
        ? "手動價格只供 Dev / QA 測試使用；正式環境請切回 Binance 或融合價格。"
        : "目前伺服器模式不允許手動價格，儲存會被拒絕；請使用 Binance 或融合價格。";
      note.style.color = rootTradingManualPriceAllowedInCurrentMode() ? "#ffcf85" : "#ff8a8a";
    } else {
      note.textContent = "預設會用多交易所融合價格；若部分 API 失效，會自動用仍健康的交易所重新分配權重，必要時再退回最後健康快取。";
      note.style.color = "";
    }
  }
}

const ROOT_TRADING_EDITABLE_PRICE_SOURCES = new Set(["fused_weighted", "binance_public_api", "manual_root"]);

function rootTradingNormalizeEditablePriceSource(source) {
  const value = String(source || "").trim();
  return ROOT_TRADING_EDITABLE_PRICE_SOURCES.has(value) ? value : "binance_public_api";
}

function rootTradingManualPriceAllowedInCurrentMode() {
  const mode = String(currentServerMode || siteConfig?.server_mode || "production").trim().toLowerCase();
  return ["dev_ready", "test", "internal_test", "superweak"].includes(mode);
}

function collectRootTradingFusionWeights() {
  const out = {};
  document.querySelectorAll("[data-trading-price-weight]").forEach((input) => {
    out[input.dataset.tradingPriceWeight || ""] = Number(input.value || 0);
  });
  return out;
}

function renderRootTradingSettings(payload) {
  const settings = payload?.settings || {};
  const markets = Array.isArray(payload?.markets) ? payload.markets : [];
  const reserve = payload?.reserve_pool || {};
  bindRootTradingBackgroundControls();
  bindRootTradingMarketRegistryControls();
  if ($("root-trading-enabled")) $("root-trading-enabled").checked = settings.enabled !== false;
  if ($("root-trading-borrowing-enabled")) $("root-trading-borrowing-enabled").checked = !!settings.borrowing_enabled;
  if ($("root-trading-borrow-apr-btc-eth")) $("root-trading-borrow-apr-btc-eth").value = adminPercentValue(settings.borrow_apr_btc_eth_percent ?? 8, 8);
  if ($("root-trading-borrow-apr-usdt-points")) $("root-trading-borrow-apr-usdt-points").value = adminPercentValue(settings.borrow_apr_usdt_points_percent ?? 10, 10);
  if ($("root-trading-borrow-pressure-multiplier")) $("root-trading-borrow-pressure-multiplier").value = Number(settings.borrow_interest_pool_pressure_multiplier ?? 4);
  if ($("root-trading-borrow-interest-interval-hours")) $("root-trading-borrow-interest-interval-hours").value = Number(settings.borrow_interest_interval_hours ?? 1);
  if ($("root-trading-borrow-interest-minimum-hours")) $("root-trading-borrow-interest-minimum-hours").value = Number(settings.borrow_interest_minimum_hours ?? 1);
  if ($("root-trading-grid-fee-discount-percent")) $("root-trading-grid-fee-discount-percent").value = adminPercentValue(settings.grid_fee_discount_percent ?? 25, 25);
  if ($("root-trading-margin-long-financing-percent")) $("root-trading-margin-long-financing-percent").value = adminPercentValue(settings.margin_long_financing_percent ?? 90, 90);
  if ($("root-trading-margin-max-pool-utilization-percent")) $("root-trading-margin-max-pool-utilization-percent").value = adminPercentValue(settings.margin_max_pool_utilization_percent ?? 80, 80);
  if ($("root-trading-short-collateral-percent")) $("root-trading-short-collateral-percent").value = adminPercentValue(settings.short_collateral_percent ?? 60, 60);
  if ($("root-trading-exchange-liability-limit-points")) $("root-trading-exchange-liability-limit-points").value = Number(settings.exchange_liability_limit_points ?? 0);
  if ($("root-trading-exchange-liability-grace-minutes")) $("root-trading-exchange-liability-grace-minutes").value = Number(settings.exchange_liability_grace_minutes ?? 60);
  if ($("root-trading-profit-settlement-interval-minutes")) $("root-trading-profit-settlement-interval-minutes").value = Number(settings.profit_settlement_interval_minutes ?? 0);
  const rawPriceSource = settings.price_source || "binance_public_api";
  const normalizedPriceSource = rootTradingNormalizeEditablePriceSource(rawPriceSource);
  const rawPriceSourceUnsupported = String(rawPriceSource) !== normalizedPriceSource;
  if ($("root-trading-price-source")) $("root-trading-price-source").value = normalizedPriceSource;
  const priceSourceNote = $("root-trading-price-source-note");
  if (priceSourceNote && rawPriceSourceUnsupported) {
    priceSourceNote.dataset.rawInvalidPriceSource = `目前資料庫價格來源是 ${rawPriceSource}，不屬於正式可選項；儲存後會改回 ${normalizedPriceSource === "binance_public_api" ? "Binance 公開 API" : normalizedPriceSource}。`;
    priceSourceNote.textContent = priceSourceNote.dataset.rawInvalidPriceSource;
    priceSourceNote.style.color = "#ffcf85";
  } else if (priceSourceNote) {
    delete priceSourceNote.dataset.rawInvalidPriceSource;
  }
  if ($("root-trading-price-fusion-mode")) $("root-trading-price-fusion-mode").value = settings.price_fusion_mode || "auto_depth";
  if ($("root-trading-price-fusion-depth-band-percent")) $("root-trading-price-fusion-depth-band-percent").value = Number(settings.price_fusion_depth_band_percent ?? 1);
  if ($("root-trading-price-fusion-depth-levels")) $("root-trading-price-fusion-depth-levels").value = Number(settings.price_fusion_depth_levels ?? 100);
  if ($("root-trading-price-fusion-min-coverage-percent")) $("root-trading-price-fusion-min-coverage-percent").value = Number(settings.price_fusion_min_orderbook_coverage_percent ?? 0.5);
  if ($("root-trading-price-fusion-max-provider-weight")) $("root-trading-price-fusion-max-provider-weight").value = adminPercentValue(settings.price_fusion_max_single_provider_weight_percent ?? 40, 40);
  if ($("root-trading-price-fusion-min-provider-count")) $("root-trading-price-fusion-min-provider-count").value = Number(settings.price_fusion_min_provider_count ?? 1);
  if ($("root-trading-price-fusion-trade-min-provider-count")) $("root-trading-price-fusion-trade-min-provider-count").value = Number(settings.price_fusion_trade_min_provider_count ?? 1);
  if ($("root-trading-warning-language")) $("root-trading-warning-language").value = settings.warning_language || "zh-TW";
  if ($("root-trading-price-degrade-pause-market-orders")) $("root-trading-price-degrade-pause-market-orders").checked = !!settings.price_degrade_pause_market_orders;
  if ($("root-trading-price-degrade-pause-bots")) $("root-trading-price-degrade-pause-bots").checked = !!settings.price_degrade_pause_bots;
  if ($("root-trading-price-degrade-pause-borrowing")) $("root-trading-price-degrade-pause-borrowing").checked = !!settings.price_degrade_pause_borrowing;
  if ($("root-trading-allow-unready-markets")) $("root-trading-allow-unready-markets").checked = settings.allow_unready_markets !== false;
  if ($("root-trading-disable-price-confidence-gates")) $("root-trading-disable-price-confidence-gates").checked = settings.disable_price_confidence_gates !== false;
  if ($("root-trading-simulated-slippage-enabled")) $("root-trading-simulated-slippage-enabled").checked = !!settings.simulated_slippage_enabled;
  if ($("root-trading-simulated-slippage-base-basis-points")) $("root-trading-simulated-slippage-base-basis-points").value = Number(settings.simulated_slippage_base_basis_points ?? 0);
  if ($("root-trading-simulated-slippage-size-basis-points-per-10k-notional")) $("root-trading-simulated-slippage-size-basis-points-per-10k-notional").value = Number(settings.simulated_slippage_size_basis_points_per_10k_notional ?? 0);
  if ($("root-trading-simulated-slippage-max-basis-points")) $("root-trading-simulated-slippage-max-basis-points").value = Number(settings.simulated_slippage_max_basis_points ?? 0);
  if ($("root-trading-price-stream-ws-enabled")) $("root-trading-price-stream-ws-enabled").checked = settings.price_stream_ws_enabled !== false;
  if ($("root-trading-price-stream-ws-stale-seconds")) $("root-trading-price-stream-ws-stale-seconds").value = Number(settings.price_stream_ws_stale_seconds ?? 10);
  if ($("root-trading-qa-live-price-provider-enabled")) $("root-trading-qa-live-price-provider-enabled").checked = rawPriceSourceUnsupported ? false : !!settings.qa_live_price_provider_enabled;
  renderRootTradingFusionWeightInputs(settings);
  renderRootTradingPriceFusionMarketOptions(payload);
  const priceSourceSelect = $("root-trading-price-source");
  if (priceSourceSelect && !priceSourceSelect.dataset.fusionBound) {
    priceSourceSelect.addEventListener("change", () => {
      const note = $("root-trading-price-source-note");
      if (note) delete note.dataset.rawInvalidPriceSource;
      toggleRootTradingPriceFusionControls();
    });
    priceSourceSelect.dataset.fusionBound = "1";
  }
  const fusionModeSelect = $("root-trading-price-fusion-mode");
  if (fusionModeSelect && !fusionModeSelect.dataset.fusionBound) {
    fusionModeSelect.addEventListener("change", toggleRootTradingPriceFusionControls);
    fusionModeSelect.dataset.fusionBound = "1";
  }
  const fusionRefreshBtn = $("root-trading-price-fusion-refresh-btn");
  if (fusionRefreshBtn && !fusionRefreshBtn.dataset.fusionBound) {
    fusionRefreshBtn.addEventListener("click", loadRootTradingPriceFusionStatus);
    fusionRefreshBtn.dataset.fusionBound = "1";
  }
  const fusionMarketSelect = $("root-trading-price-fusion-market");
  if (fusionMarketSelect && !fusionMarketSelect.dataset.fusionBound) {
    fusionMarketSelect.addEventListener("change", loadRootTradingPriceFusionStatus);
    fusionMarketSelect.dataset.fusionBound = "1";
  }
  const botAuditRefreshBtn = $("root-trading-bot-audit-refresh-btn");
  if (botAuditRefreshBtn && !botAuditRefreshBtn.dataset.auditBound) {
    botAuditRefreshBtn.addEventListener("click", loadRootTradingBotAuditDashboard);
    botAuditRefreshBtn.dataset.auditBound = "1";
  }
  const botAuditRunBtn = $("root-trading-bot-audit-run-btn");
  if (botAuditRunBtn && !botAuditRunBtn.dataset.auditBound) {
    botAuditRunBtn.addEventListener("click", () => runRootTradingBotAudit(true));
    botAuditRunBtn.dataset.auditBound = "1";
  }
  const btcTradeCheckBtn = $("root-trading-btc-trade-check-btn");
  if (btcTradeCheckBtn && !btcTradeCheckBtn.dataset.btcTradeBound) {
    btcTradeCheckBtn.addEventListener("click", checkRootBtcTradeStatus);
    btcTradeCheckBtn.dataset.btcTradeBound = "1";
  }
  const btcTradeSetupBtn = $("root-trading-btc-trade-setup-btn");
  if (btcTradeSetupBtn && !btcTradeSetupBtn.dataset.btcTradeBound) {
    btcTradeSetupBtn.addEventListener("click", () => setupRootBtcTrade({ automatic: false }));
    btcTradeSetupBtn.dataset.btcTradeBound = "1";
  }
  const btcTradeStartBtn = $("root-trading-btc-trade-start-btn");
  if (btcTradeStartBtn && !btcTradeStartBtn.dataset.btcTradeBound) {
    btcTradeStartBtn.addEventListener("click", startRootBtcTradePrediction);
    btcTradeStartBtn.dataset.btcTradeBound = "1";
  }
  toggleRootTradingPriceFusionControls();
  if ($("root-trading-max-price-staleness")) $("root-trading-max-price-staleness").value = settings.max_price_staleness_seconds ?? 900;
  if ($("root-trading-liquidation-enabled")) $("root-trading-liquidation-enabled").checked = settings.margin_liquidation_enabled !== false;
  if ($("root-trading-bot-auto-enabled")) $("root-trading-bot-auto-enabled").checked = settings.bot_auto_scan_enabled !== false;
  if ($("root-trading-bot-auto-interval")) $("root-trading-bot-auto-interval").value = settings.bot_auto_scan_interval_seconds ?? 30;
  if ($("root-trading-bot-auto-limit")) $("root-trading-bot-auto-limit").value = settings.bot_auto_scan_limit ?? 50;
  if ($("root-trading-bot-competition-enabled")) $("root-trading-bot-competition-enabled").checked = settings.bot_competition_enabled !== false;
  if ($("root-trading-bot-competition-reward")) $("root-trading-bot-competition-reward").value = settings.bot_competition_weekly_reward_points ?? 100;
  if ($("root-trading-backtest-max-candles")) $("root-trading-backtest-max-candles").value = settings.backtest_max_candles ?? 20000;
  if ($("root-trading-backtest-capacity-budget")) $("root-trading-backtest-capacity-budget").value = settings.backtest_capacity_time_budget_seconds ?? 60;
  const measuredHintEl = $("root-trading-backtest-measured-hint");
  if (measuredHintEl) {
    const minCap   = Number(settings.backtest_measured_capacity || 0);
    const maxCap   = Number(settings.backtest_measured_capacity_max || 0);
    const measuredAt = String(settings.backtest_capacity_measured_at || "").trim();
    const bottleneck = String(settings.backtest_capacity_bottleneck || "").trim();
    const fastest    = String(settings.backtest_capacity_fastest || "").trim();
    const budget   = Number(settings.backtest_capacity_time_budget_seconds || 60);
    if (minCap > 0) {
      const ts = measuredAt ? `（${measuredAt} 量測）` : "";
      const minPart = `所有機器人最低 ${minCap.toLocaleString()} 根（瓶頸：${bottleneck || "-"}，做為預設值）`;
      const maxPart = maxCap > 0 ? `；最快 ${maxCap.toLocaleString()} 根（${fastest || "-"}）` : "";
      measuredHintEl.textContent = `${budget} 秒內 — ${minPart}${maxPart}${ts}`;
      measuredHintEl.style.color = "var(--accent-2, #00d4aa)";
    } else {
      measuredHintEl.textContent = "本機性能尚未量測（首次啟動時自動執行）";
      measuredHintEl.style.color = "";
    }
  }
  if ($("root-trading-bot-audit-enabled")) $("root-trading-bot-audit-enabled").checked = settings.bot_audit_enabled !== false;
  if ($("root-trading-bot-audit-interval")) $("root-trading-bot-audit-interval").value = settings.bot_audit_interval_seconds ?? 300;
  if ($("root-trading-bot-audit-limit")) $("root-trading-bot-audit-limit").value = settings.bot_audit_limit ?? 50;
  if ($("root-trading-maintenance-percent")) $("root-trading-maintenance-percent").value = adminPercentValue(settings.margin_maintenance_percent ?? 15, 15);
  if ($("root-trading-futures-enabled")) $("root-trading-futures-enabled").checked = !!settings.futures_enabled;
  if ($("root-trading-pvp-enabled")) $("root-trading-pvp-enabled").checked = !!settings.pvp_matching_enabled;
  if ($("root-trading-reserve-pool")) {
    const fundingPool = json.funding_pool || {};
    $("root-trading-reserve-pool").textContent = `可借 ${Number(fundingPool.available_points || 0)} / 交易所基金 ${Number(reserve.balance_points || 0)} / CFD 保留 ${Number(fundingPool.cfd_profit_reserve_required_points || 0)} 點`;
  }
  if ($("root-trading-btc-trade-enabled")) $("root-trading-btc-trade-enabled").checked = !!settings.btc_trade_enabled;
  if ($("root-trading-btc-trade-repo")) $("root-trading-btc-trade-repo").value = settings.btc_trade_repo_url || "https://github.com/s9213712/BTC_trade.git";
  if ($("root-trading-btc-trade-branch")) $("root-trading-btc-trade-branch").value = settings.btc_trade_branch || "strategy/v15b-plus";
  if ($("root-trading-btc-trade-path")) $("root-trading-btc-trade-path").value = settings.btc_trade_project_dir || "";
  const list = $("root-trading-market-settings");
  if (!list) return;
  if (!markets.length) {
    list.innerHTML = `<div class="drive-empty">尚無交易市場</div>`;
    loadRootTradingMarketRegistry({ silent: true });
    return;
  }
  list.innerHTML = markets.map((market) => `
    <div class="drive-file-row billing-catalog-row root-trading-market-row" data-root-trading-market="${sanitize(market.symbol || "")}">
      <div>
        <strong>${sanitize(rootTradingDisplayMarketSymbol(market.symbol, market.display_symbol || ""))}</strong>
        <div class="drive-card-sub">現貨手續費 ${adminFormatPercent(market.fee_rate_percent || 0)}% · Grid 折扣後約 ${adminFormatPercent((Number(market.fee_rate_percent || 0) * (100 - Number(settings.grid_fee_discount_percent || 25)) / 100) || 0)}% · 最低 ${Number(market.min_order_points || 0)} 點 · 最高 ${Number(market.max_order_points || 0)} 點</div>
      </div>
      <div class="settings-option-grid billing-market-grid">
        <label><input type="checkbox" data-trading-market-field="enabled" ${market.enabled ? "checked" : ""} /> 啟用</label>
        <label>手續費百分比<input type="number" min="0" max="50" step="0.01" data-trading-market-field="fee_rate_percent" value="${adminFormatPercent(market.fee_rate_percent || 0)}" /></label>
        <label>最低交易額<input type="number" min="0" max="1000000000" step="1" data-trading-market-field="min_order_points" value="${Number(market.min_order_points || 0)}" /></label>
        <label>最高交易額<input type="number" min="1" max="1000000000000" step="1" data-trading-market-field="max_order_points" value="${Number(market.max_order_points || 0)}" /></label>
      </div>
    </div>
  `).join("");
  loadRootTradingPriceFusionStatus();
  loadRootTradingBotAuditDashboard();
  loadRootTradingBackgroundStatus();
  loadRootTradingMarketRegistry({ silent: true });
}

async function loadRootTradingSettings() {
  if (currentUser !== "root" || !$("root-trading-market-settings")) return;
  rootTradingSettingsMsg("交易所參數讀取中...");
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + "/root/trading/settings", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await parseRootTradingSettingsResponse(res);
    if (!res.ok || !json.ok) {
      rootTradingSettingsMsg(rootTradingSettingsHttpMessage(res, json, "交易所參數讀取失敗"), false);
      return;
    }
    rootTradingSettingsCache = json;
    renderRootTradingSettings(json);
    rootTradingSettingsMsg("");
  } catch (err) {
    rootTradingSettingsMsg(err.message || "交易所參數讀取請求失敗", false);
  }
}

function collectRootTradingMarketSettings() {
  return Array.from(document.querySelectorAll("[data-root-trading-market]")).map((row) => {
    const symbol = row.dataset.rootTradingMarket || "";
    const payload = { symbol };
    row.querySelectorAll("[data-trading-market-field]").forEach((input) => {
      const key = input.dataset.tradingMarketField;
      payload[key] = input.type === "checkbox" ? input.checked : (key === "fee_rate_percent" ? adminInputPercent(input.value) : Number(input.value || 0));
    });
    return payload;
  });
}

async function saveRootTradingSettings() {
  if (currentUser !== "root") return;
  const saveBtn = $("root-trading-settings-save-btn");
  const wasBtcTradeEnabled = rootTradingSettingsCache?.settings?.btc_trade_enabled === true;
  const willBtcTradeEnable = !!$("root-trading-btc-trade-enabled")?.checked;
  const selectedPriceSource = $("root-trading-price-source")?.value || "binance_public_api";
  if (selectedPriceSource === "manual_root" && !rootTradingManualPriceAllowedInCurrentMode()) {
    rootTradingSettingsMsg("手動價格只允許在 dev_ready / test / internal_test / superweak 模式使用", false);
    return;
  }
  if (saveBtn) saveBtn.disabled = true;
  rootTradingSettingsMsg("交易所參數儲存中...");
  const payload = {
    settings: {
      enabled: !!$("root-trading-enabled")?.checked,
      borrowing_enabled: !!$("root-trading-borrowing-enabled")?.checked,
      borrow_apr_btc_eth_percent: adminInputPercent($("root-trading-borrow-apr-btc-eth")?.value || 0),
      borrow_apr_usdt_points_percent: adminInputPercent($("root-trading-borrow-apr-usdt-points")?.value || 0),
      borrow_interest_pool_pressure_multiplier: Number($("root-trading-borrow-pressure-multiplier")?.value || 0),
      borrow_interest_interval_hours: Number($("root-trading-borrow-interest-interval-hours")?.value || 1),
      borrow_interest_minimum_hours: Number($("root-trading-borrow-interest-minimum-hours")?.value || 1),
      grid_fee_discount_percent: adminInputPercent($("root-trading-grid-fee-discount-percent")?.value || 25),
      margin_long_financing_percent: adminInputPercent($("root-trading-margin-long-financing-percent")?.value || 90, 90),
      margin_max_pool_utilization_percent: adminInputPercent($("root-trading-margin-max-pool-utilization-percent")?.value || 80, 80),
      short_collateral_percent: adminInputPercent($("root-trading-short-collateral-percent")?.value || 60, 60),
      exchange_liability_limit_points: Number($("root-trading-exchange-liability-limit-points")?.value || 0),
      exchange_liability_grace_minutes: Number($("root-trading-exchange-liability-grace-minutes")?.value || 60),
      profit_settlement_interval_minutes: Number($("root-trading-profit-settlement-interval-minutes")?.value || 0),
      price_source: selectedPriceSource,
      price_fusion_mode: ($("root-trading-price-fusion-mode")?.value || "auto_depth"),
      price_fusion_manual_weights: collectRootTradingFusionWeights(),
      price_fusion_depth_band_percent: Number($("root-trading-price-fusion-depth-band-percent")?.value || 1),
      price_fusion_depth_levels: Number($("root-trading-price-fusion-depth-levels")?.value || 100),
      price_fusion_min_orderbook_coverage_percent: Number($("root-trading-price-fusion-min-coverage-percent")?.value || 0.5),
      price_fusion_max_single_provider_weight_percent: adminInputPercent($("root-trading-price-fusion-max-provider-weight")?.value || 40),
      price_fusion_min_provider_count: Number($("root-trading-price-fusion-min-provider-count")?.value || 1),
      price_fusion_trade_min_provider_count: Number($("root-trading-price-fusion-trade-min-provider-count")?.value || 1),
      warning_language: ($("root-trading-warning-language")?.value || "zh-TW"),
      price_degrade_pause_market_orders: !!$("root-trading-price-degrade-pause-market-orders")?.checked,
      price_degrade_pause_bots: !!$("root-trading-price-degrade-pause-bots")?.checked,
      price_degrade_pause_borrowing: !!$("root-trading-price-degrade-pause-borrowing")?.checked,
      allow_unready_markets: $("root-trading-allow-unready-markets")?.checked !== false,
      disable_price_confidence_gates: $("root-trading-disable-price-confidence-gates")?.checked !== false,
      simulated_slippage_enabled: !!$("root-trading-simulated-slippage-enabled")?.checked,
      simulated_slippage_base_basis_points: Number($("root-trading-simulated-slippage-base-basis-points")?.value || 0),
      simulated_slippage_size_basis_points_per_10k_notional: Number($("root-trading-simulated-slippage-size-basis-points-per-10k-notional")?.value || 0),
      simulated_slippage_max_basis_points: Number($("root-trading-simulated-slippage-max-basis-points")?.value || 0),
      price_stream_ws_enabled: !!$("root-trading-price-stream-ws-enabled")?.checked,
      price_stream_ws_stale_seconds: Number($("root-trading-price-stream-ws-stale-seconds")?.value || 10),
      qa_live_price_provider_enabled: !!$("root-trading-qa-live-price-provider-enabled")?.checked,
      max_price_staleness_seconds: Number($("root-trading-max-price-staleness")?.value || 0),
      margin_liquidation_enabled: !!$("root-trading-liquidation-enabled")?.checked,
      bot_auto_scan_enabled: !!$("root-trading-bot-auto-enabled")?.checked,
      bot_auto_scan_interval_seconds: Number($("root-trading-bot-auto-interval")?.value || 30),
      bot_auto_scan_limit: Number($("root-trading-bot-auto-limit")?.value || 50),
      bot_competition_enabled: $("root-trading-bot-competition-enabled")?.checked !== false,
      bot_competition_weekly_reward_points: Number($("root-trading-bot-competition-reward")?.value || 100),
      backtest_max_candles: Number($("root-trading-backtest-max-candles")?.value || 20000),
      backtest_capacity_time_budget_seconds: Number($("root-trading-backtest-capacity-budget")?.value || 60),
      bot_audit_enabled: !!$("root-trading-bot-audit-enabled")?.checked,
      bot_audit_interval_seconds: Number($("root-trading-bot-audit-interval")?.value || 300),
      bot_audit_limit: Number($("root-trading-bot-audit-limit")?.value || 50),
      margin_maintenance_percent: adminInputPercent($("root-trading-maintenance-percent")?.value || 0),
      futures_enabled: !!$("root-trading-futures-enabled")?.checked,
      pvp_matching_enabled: !!$("root-trading-pvp-enabled")?.checked,
      btc_trade_enabled: willBtcTradeEnable,
      btc_trade_repo_url: ($("root-trading-btc-trade-repo")?.value || "").trim(),
      btc_trade_branch: ($("root-trading-btc-trade-branch")?.value || "").trim(),
      btc_trade_project_dir: ($("root-trading-btc-trade-path")?.value || "").trim(),
    },
    markets: collectRootTradingMarketSettings(),
  };
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/root/trading/settings", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload)
    });
    const json = await parseRootTradingSettingsResponse(res);
    rootTradingSettingsMsg(
      res.ok && json.ok ? "交易所參數已儲存" : rootTradingSettingsHttpMessage(res, json, "交易所參數儲存失敗"),
      !!(res.ok && json.ok)
    );
    if (res.ok && json.ok) {
      rootTradingSettingsCache = json;
      renderRootTradingSettings(json);
      loadRootTradingPriceFusionStatus();
      loadRootTradingBotAuditDashboard();
      loadRootTradingBackgroundStatus();
      if (typeof loadTradingDashboard === "function") loadTradingDashboard();
      if (willBtcTradeEnable && !wasBtcTradeEnabled) {
        await setupRootBtcTrade({ automatic: true });
      }
    }
  } catch (err) {
    rootTradingSettingsMsg(err.message || "交易所參數儲存請求失敗", false);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function checkRootBtcTradeStatus() {
  if (currentUser !== "root") return;
  const status = $("root-trading-btc-trade-status");
  const button = $("root-trading-btc-trade-check-btn");
  const projectDir = ($("root-trading-btc-trade-path")?.value || "").trim();
  if (button) button.disabled = true;
  if (status) {
    status.textContent = "BTC_trade 狀態檢查中...";
    status.style.color = "var(--muted)";
  }
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/root/trading/btc-trade/check", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
      body: JSON.stringify({ project_dir: projectDir }),
    });
    const json = await parseRootTradingSettingsResponse(res);
    const info = json.status || {};
    if (status) {
      const commands = Array.isArray(info.commands) && info.commands.length ? `；初始化：cd ${projectDir || "BTC_trade"} && ${info.commands.join(" && ")}` : "";
      status.textContent = res.ok && json.ok
        ? `${info.available ? "可用" : "不可用"}：${formatBtcTradeStatusSummary(info)}${info.needs_initialization ? commands : ""}`
        : (json.msg || `HTTP ${res.status}`);
      status.style.color = res.ok && json.ok && info.available ? "#4caf50" : "#ffb74d";
    }
  } catch (err) {
    if (status) {
      status.textContent = err.message || "BTC_trade 狀態檢查失敗";
      status.style.color = "#ff4f6d";
    }
  } finally {
    if (button) button.disabled = false;
  }
}

function formatBtcTradeStepResult(step) {
  if (!step || typeof step !== "object") return "";
  const label = step.label || step.command || "step";
  const state = step.ok ? "成功" : "失敗";
  const detail = step.message || step.error || (step.stderr_tail ? step.stderr_tail.split("\n").filter(Boolean).slice(-1)[0] : "");
  return `${label}${state}${detail ? `：${detail}` : ""}`;
}

function formatBtcTradeArtifactState(label, info = {}, options = {}) {
  if (!info || typeof info !== "object") return `${label}：未知`;
  const positiveLabel = options.positiveLabel || "已是最新";
  const negativeLabel = options.negativeLabel || "需要處理";
  const positive = info.needs_update === false && info.needs_retrain === false && info.needs_refresh === false;
  const state = positive ? positiveLabel : negativeLabel;
  const updatedAt = info.last_bar_at || info.generated_at || info.updated_at;
  return `${label}：${state}${updatedAt ? `（${updatedAt}）` : ""}`;
}

function formatBtcTradeStatusSummary(info = {}) {
  const parts = [];
  if (info.message) parts.push(info.message);
  if (info.artifacts && typeof info.artifacts === "object") {
    parts.push(formatBtcTradeArtifactState("資料", info.artifacts.data || {}, { positiveLabel: "已是最新", negativeLabel: "需要更新" }));
    parts.push(formatBtcTradeArtifactState("模型", info.artifacts.models || {}, { positiveLabel: "已完成重訓", negativeLabel: "需要重訓" }));
    parts.push(formatBtcTradeArtifactState("預測", info.artifacts.prediction || {}, { positiveLabel: "可直接使用", negativeLabel: "需要刷新" }));
  }
  if (Array.isArray(info.missing) && info.missing.length) parts.push(`缺少 ${info.missing.join(", ")}`);
  return parts.filter(Boolean).join("；");
}

async function setupRootBtcTrade(options = {}) {
  if (currentUser !== "root") return;
  const status = $("root-trading-btc-trade-status");
  const button = $("root-trading-btc-trade-setup-btn");
  const projectDir = ($("root-trading-btc-trade-path")?.value || "").trim();
  const repoUrl = ($("root-trading-btc-trade-repo")?.value || "").trim();
  const branch = ($("root-trading-btc-trade-branch")?.value || "").trim();
  if (button) button.disabled = true;
  if (status) {
    status.textContent = options.automatic ? "已啟用 BTC_trade，開始自動下載/更新並建置..." : "BTC_trade 下載/更新並建置中，可能需要數分鐘...";
    status.style.color = "var(--muted)";
  }
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/root/trading/btc-trade/setup", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
      body: JSON.stringify({ project_dir: projectDir, repo_url: repoUrl, branch }),
    });
    const json = await parseRootTradingSettingsResponse(res);
    const result = json.result || {};
    if (json.project_dir && $("root-trading-btc-trade-path") && !$("root-trading-btc-trade-path").value.trim()) {
      $("root-trading-btc-trade-path").value = json.project_dir;
    }
    if (status) {
      const steps = Array.isArray(result.steps) && result.steps.length
        ? `；步驟：${result.steps.map(formatBtcTradeStepResult).join(" / ")}`
        : "";
      status.textContent = res.ok && json.ok
        ? `${json.setup_ok ? "建置完成" : "建置未完成"}：${json.message || result.message || "-"}${steps}`
        : (json.msg || `HTTP ${res.status}`);
      status.style.color = res.ok && json.ok && json.setup_ok ? "#4caf50" : "#ffb74d";
    }
    if (res.ok && json.ok && json.setup_ok && typeof loadTradingDashboard === "function") {
      loadTradingDashboard();
    }
  } catch (err) {
    if (status) {
      status.textContent = err.message || "BTC_trade 建置請求失敗，請自行建置後再檢查";
      status.style.color = "#ff4f6d";
    }
  } finally {
    if (button) button.disabled = false;
  }
}

async function startRootBtcTradePrediction() {
  if (currentUser !== "root") return;
  const operation = rootTradingAdminOperationContext();
  const status = $("root-trading-btc-trade-status");
  const button = $("root-trading-btc-trade-start-btn");
  const projectDir = ($("root-trading-btc-trade-path")?.value || "").trim();
  const repoUrl = ($("root-trading-btc-trade-repo")?.value || "").trim();
  const branch = ($("root-trading-btc-trade-branch")?.value || "").trim();
  if (button) button.disabled = true;
  if (status) {
    status.textContent = "BTC_trade 一鍵啟動中：必要時自動下載/更新、安裝依賴，再更新資料、重訓並執行預測腳本...";
    status.style.color = "var(--muted)";
  }
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/root/trading/btc-trade/start", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
      body: JSON.stringify({ project_dir: projectDir, repo_url: repoUrl, branch, timeframe: "4h" }),
    });
    const json = await parseRootTradingSettingsResponse(res);
    rootTradingAdminAssertOperationCurrent(operation);
    const job = json.job || {};
    if (json.project_dir && $("root-trading-btc-trade-path") && !$("root-trading-btc-trade-path").value.trim()) {
      $("root-trading-btc-trade-path").value = json.project_dir;
    }
    if (status) {
      const steps = Array.isArray(job.steps) && job.steps.length
        ? `；步驟：${job.steps.map(formatBtcTradeStepResult).join(" / ")}`
        : "";
      status.textContent = res.ok
        ? `${json.message || json.msg || "BTC_trade 一鍵啟動已開始"}${job.job_id ? `；工作編號 ${job.job_id}` : ""}${steps}`
        : (json.msg || `HTTP ${res.status}`);
      status.style.color = res.ok && json.ok && json.start_ok ? "#4caf50" : "#ffb74d";
    }
    if (res.ok && json.start_ok && job.job_id && job.status !== "completed" && job.status !== "error") {
      await pollRootBtcTradeStartJob(job.job_id, operation);
    }
    if (res.ok && json.ok && json.start_ok && typeof loadTradingDashboard === "function") {
      loadTradingDashboard();
    }
  } catch (err) {
    if (!rootTradingAdminOperationIsCurrent(operation) || err?.name === "AbortError") return;
    if (status) {
      status.textContent = err.message || "BTC_trade 一鍵啟動失敗";
      status.style.color = "#ff4f6d";
    }
  } finally {
    if (rootTradingAdminOperationIsCurrent(operation) && button) button.disabled = false;
  }
}

async function pollRootBtcTradeStartJob(jobId, operation = null) {
  const operationContext = operation || rootTradingAdminOperationContext();
  rootTradingAdminAssertOperationCurrent(operationContext);
  if (!jobId || currentUser !== "root") return;
  const status = $("root-trading-btc-trade-status");
  while (true) {
    rootTradingAdminAssertOperationCurrent(operationContext);
    await fetchCsrfToken({ force: true });
    rootTradingAdminAssertOperationCurrent(operationContext);
    const res = await apiFetch(API + `/root/trading/btc-trade/start-status?job_id=${encodeURIComponent(jobId)}`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" }
    });
    const json = await parseRootTradingSettingsResponse(res);
    rootTradingAdminAssertOperationCurrent(operationContext);
    const job = json.job || {};
    const result = job.result || {};
    if (status) {
      const steps = Array.isArray(job.steps) && job.steps.length
        ? `；步驟：${job.steps.map(formatBtcTradeStepResult).join(" / ")}`
        : "";
      const actionSummary = result.actions
        ? `；資料${result.actions.data_updated ? "已更新" : "沿用"} / 模型${result.actions.model_retrained ? "已重訓" : "沿用"} / 預測${result.actions.prediction_refreshed ? "已刷新" : "沿用有效結果"}`
        : "";
      status.textContent = res.ok
        ? `${job.status === "completed" ? "一鍵啟動完成" : (job.status === "error" ? "一鍵啟動失敗" : "一鍵啟動執行中")}：${job.message || json.msg || "-"}${actionSummary}${steps}`
        : (json.msg || `HTTP ${res.status}`);
      status.style.color = job.status === "completed" ? "#4caf50" : (job.status === "error" ? "#ff4f6d" : "#ffb74d");
    }
    if (!res.ok || job.status === "completed" || job.status === "error") return;
    await new Promise((resolve) => setTimeout(resolve, 1500));
    rootTradingAdminAssertOperationCurrent(operationContext);
  }
}
