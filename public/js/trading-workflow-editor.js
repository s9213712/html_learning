'use strict';

(function () {
  const STORAGE_KEY = "hackme_trading_workflow_json";
  const PREVIEW_STORAGE_KEY = "hackme_trading_workflow_preview";
  const ACCOUNT_SCOPE_STORAGE_KEY = "hackme_web.account.active_scope";
  const $ = (id) => document.getElementById(id);
  const NODE_TYPES = ["start", "condition", "logic", "control", "action"];
  const CONDITION_TYPES = ["always", "price_below", "price_above", "rsi_above", "rsi_below", "kd_above", "kd_below", "ma_position", "bb_position", "has_position"];
  const ACTION_TYPES = ["buy_percent", "buy_amount", "sell_percent", "close_all", "hold"];
  const CONDITION_LABELS = {
    always: "永遠成立",
    price_below: "價格低於",
    price_above: "價格高於",
    rsi_above: "RSI 高於",
    rsi_below: "RSI 低於",
    kd_above: "KD 高於",
    kd_below: "KD 低於",
    ma_position: "均線位置",
    bb_position: "布林位置",
    has_position: "持倉狀態",
  };
  const ACTION_LABELS = {
    buy_percent: "買入百分比",
    buy_amount: "固定金額買入",
    sell_percent: "賣出百分比",
    close_all: "全部平倉",
    hold: "不動作",
  };

  function editorAccountStorageScope() {
    try {
      const openerScope = window.opener?.getCurrentAccountStorageScope?.();
      if (openerScope) return openerScope;
    } catch (_) {}
    try {
      return localStorage.getItem(ACCOUNT_SCOPE_STORAGE_KEY) || "anonymous";
    } catch (_) {
      return "anonymous";
    }
  }

  const EDITOR_INITIAL_ACCOUNT_SCOPE = editorAccountStorageScope();
  const editorAccountAbortController = typeof AbortController === "function" ? new AbortController() : null;
  let editorAccountInvalidated = false;

  function invalidateEditorAccountScope() {
    if (editorAccountInvalidated) return;
    editorAccountInvalidated = true;
    editorAccountAbortController?.abort();
    if (document.body) {
      document.body.replaceChildren();
      const notice = document.createElement("main");
      notice.className = "workflow-editor-session-ended";
      notice.setAttribute("role", "alert");
      notice.textContent = "帳戶已切換，這個 Workflow 編輯器已關閉。";
      document.body.appendChild(notice);
    }
    try { window.close(); } catch (_) {}
  }

  function assertEditorAccountScope() {
    if (!editorAccountInvalidated && editorAccountStorageScope() === EDITOR_INITIAL_ACCOUNT_SCOPE) return;
    invalidateEditorAccountScope();
    const err = new Error("Account context changed");
    err.name = "AbortError";
    throw err;
  }

  async function editorFetch(input, options = {}) {
    assertEditorAccountScope();
    const requestOptions = { ...options };
    if (editorAccountAbortController) {
      const signals = [requestOptions.signal, editorAccountAbortController.signal].filter(Boolean);
      requestOptions.signal = signals.length > 1 && typeof AbortSignal !== "undefined" && typeof AbortSignal.any === "function"
        ? AbortSignal.any(signals)
        : editorAccountAbortController.signal;
    }
    const response = await fetch(input, requestOptions);
    assertEditorAccountScope();
    return response;
  }

  function editorScopedStorageKey(key) {
    assertEditorAccountScope();
    return `hackme_web:${EDITOR_INITIAL_ACCOUNT_SCOPE}:${String(key || "state")}`;
  }
  window.addEventListener("storage", (event) => {
    if (event.key === ACCOUNT_SCOPE_STORAGE_KEY) {
      try { assertEditorAccountScope(); } catch (_) {}
    }
  });
  window.addEventListener("focus", () => {
    try { assertEditorAccountScope(); } catch (_) {}
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      try { assertEditorAccountScope(); } catch (_) {}
    }
  });
  const PORTS = {
    start: { inputs: [], outputs: ["out"] },
    condition: { inputs: ["in"], outputs: ["true", "false"] },
    logic: { inputs: ["in"], outputs: ["true", "false"] },
    control: { inputs: ["in"], outputs: ["then", "wait"] },
    action: { inputs: ["in"], outputs: ["out"] },
  };

  let selectedNodeId = "start";
  let pendingConnection = null;
  let dragNode = null;
  let workflow = normalizeWorkflow(loadInitialWorkflow());
  let workflowPreviewCsrfToken = "";
  let workflowPreviewBound = false;
  let workflowPreviewState = { markets: [], result: null };
  const GRAPH_NODE_WIDTH = 210;
  const GRAPH_NODE_HEIGHT = 118;

  function uid(prefix) {
    return `${prefix}_${Math.random().toString(36).slice(2, 9)}`;
  }

  function html(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch]));
  }

  function numberValue(value, fallback = 0) {
    const next = Number(value);
    return Number.isFinite(next) ? next : fallback;
  }

  function apiPath(path) {
    return `/api${path}`;
  }

  function readCookie(name) {
    const text = document.cookie || "";
    const prefix = `${name}=`;
    const chunk = text.split(/;\s*/).find((item) => item.startsWith(prefix));
    return chunk ? decodeURIComponent(chunk.slice(prefix.length)) : "";
  }

  async function fetchWorkflowPreviewCsrfToken({ force = false } = {}) {
    assertEditorAccountScope();
    const cookieToken = readCookie("csrf_token");
    if (!force && (workflowPreviewCsrfToken || cookieToken)) {
      workflowPreviewCsrfToken = workflowPreviewCsrfToken || cookieToken || "";
      return workflowPreviewCsrfToken;
    }
    try {
      const res = await editorFetch(apiPath("/csrf-token"), { credentials: "same-origin" });
      const json = await res.json().catch(() => ({}));
      assertEditorAccountScope();
      if (res.ok && json && typeof json.csrf_token === "string" && json.csrf_token) {
        workflowPreviewCsrfToken = json.csrf_token;
        return workflowPreviewCsrfToken;
      }
    } catch (err) {
      if (err?.name === "AbortError") throw err;
      // fall through to cookie fallback
    }
    assertEditorAccountScope();
    workflowPreviewCsrfToken = readCookie("csrf_token") || "";
    return workflowPreviewCsrfToken;
  }

  async function fetchWorkflowPreviewJson(path, options = {}, retryOnCsrf = true) {
    const requestOptions = { ...(options || {}) };
    const method = String(requestOptions.method || "GET").toUpperCase();
    const headers = { ...(requestOptions.headers || {}) };
    const needsCsrf = !["GET", "HEAD", "OPTIONS"].includes(method);
    if (needsCsrf) {
      headers["X-CSRF-Token"] = await fetchWorkflowPreviewCsrfToken({ force: true });
      if (requestOptions.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    }
    const res = await editorFetch(apiPath(path), {
      credentials: "same-origin",
      ...requestOptions,
      method,
      headers,
    });
    const raw = await res.text().catch(() => "");
    assertEditorAccountScope();
    let json = {};
    try {
      json = raw ? JSON.parse(raw) : {};
    } catch (_) {
      json = {};
    }
    if (!res.ok && retryOnCsrf && needsCsrf && json && json.error === "csrf_invalid") {
      workflowPreviewCsrfToken = "";
      return fetchWorkflowPreviewJson(path, options, false);
    }
    if (!res.ok || !json.ok) {
      throw new Error(String(json.msg || json.message || raw || `HTTP ${res.status}` || "操作失敗").slice(0, 240));
    }
    return json;
  }

  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  function toLocalDatetimeValue(date) {
    const safe = date instanceof Date ? date : new Date();
    return `${safe.getFullYear()}-${pad2(safe.getMonth() + 1)}-${pad2(safe.getDate())}T${pad2(safe.getHours())}:${pad2(safe.getMinutes())}`;
  }

  function defaultPreviewConfig() {
    const end = new Date();
    const start = new Date(end.getTime() - 180 * 24 * 60 * 60 * 1000);
    return {
      market_symbol: "BTC/POINTS",
      timeframe: "1h",
      initial_cash_points: 10000,
      slippage_percent: 0,
      start_time: toLocalDatetimeValue(start),
      end_time: toLocalDatetimeValue(end),
    };
  }

  function loadPreviewConfig() {
    const fallback = defaultPreviewConfig();
    try {
      const parsed = JSON.parse(localStorage.getItem(editorScopedStorageKey(PREVIEW_STORAGE_KEY)) || "{}");
      if (!parsed || typeof parsed !== "object") return fallback;
      return {
        market_symbol: String(parsed.market_symbol || fallback.market_symbol),
        timeframe: String(parsed.timeframe || fallback.timeframe),
        initial_cash_points: Math.max(1, numberValue(parsed.initial_cash_points, fallback.initial_cash_points)),
        slippage_percent: Math.max(0, numberValue(parsed.slippage_percent, fallback.slippage_percent)),
        start_time: String(parsed.start_time || fallback.start_time),
        end_time: String(parsed.end_time || fallback.end_time),
      };
    } catch (_) {
      return fallback;
    }
  }

  function applyPreviewConfig(config) {
    const safe = config || defaultPreviewConfig();
    if ($("workflow-preview-market")) $("workflow-preview-market").value = String(safe.market_symbol || "");
    if ($("workflow-preview-timeframe")) $("workflow-preview-timeframe").value = String(safe.timeframe || "1h");
    if ($("workflow-preview-initial-cash")) $("workflow-preview-initial-cash").value = String(Math.max(1, numberValue(safe.initial_cash_points, 10000)));
    if ($("workflow-preview-slippage")) $("workflow-preview-slippage").value = String(Math.max(0, numberValue(safe.slippage_percent, 0)));
    if ($("workflow-preview-start")) $("workflow-preview-start").value = String(safe.start_time || "");
    if ($("workflow-preview-end")) $("workflow-preview-end").value = String(safe.end_time || "");
  }

  function currentPreviewConfig() {
    return {
      market_symbol: String($("workflow-preview-market")?.value || ""),
      timeframe: String($("workflow-preview-timeframe")?.value || "1h"),
      initial_cash_points: Math.max(1, numberValue($("workflow-preview-initial-cash")?.value, 10000)),
      slippage_percent: Math.max(0, numberValue($("workflow-preview-slippage")?.value, 0)),
      start_time: String($("workflow-preview-start")?.value || ""),
      end_time: String($("workflow-preview-end")?.value || ""),
    };
  }

  function savePreviewConfig() {
    try {
      localStorage.setItem(editorScopedStorageKey(PREVIEW_STORAGE_KEY), JSON.stringify(currentPreviewConfig()));
    } catch (_) {
      // ignore local storage failures
    }
  }

  function shiftPreviewRange(days) {
    const safeDays = Math.max(1, Number(days || 180));
    const end = new Date();
    const start = new Date(end.getTime() - safeDays * 24 * 60 * 60 * 1000);
    if ($("workflow-preview-start")) $("workflow-preview-start").value = toLocalDatetimeValue(start);
    if ($("workflow-preview-end")) $("workflow-preview-end").value = toLocalDatetimeValue(end);
    savePreviewConfig();
  }

  function previewStatus(message, good = true) {
    const el = $("workflow-preview-status");
    if (!el) return;
    el.textContent = message || "";
    el.style.color = good ? "var(--muted)" : "var(--red)";
  }

  function formatMetricValue(value, digits = 2) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "-";
    return num.toLocaleString("zh-Hant-TW", {
      minimumFractionDigits: digits > 0 ? digits : 0,
      maximumFractionDigits: digits,
    });
  }

  function formatSignedMetric(value, digits = 2, suffix = "") {
    const num = Number(value);
    if (!Number.isFinite(num)) return "-";
    return `${num >= 0 ? "+" : ""}${formatMetricValue(num, digits)}${suffix}`;
  }

  function previewMetricCard(label, value, subtext) {
    return `
      <div class="preview-metric">
        <span class="label">${html(label)}</span>
        <strong>${html(value)}</strong>
        <small>${html(subtext || "")}</small>
      </div>
    `;
  }

  function renderWorkflowPreviewResult(result) {
    const badge = $("workflowPreviewBadge");
    const metrics = $("workflow-preview-metrics");
    const detail = $("workflow-preview-detail");
    const warnings = $("workflow-preview-warnings");
    workflowPreviewState.result = result || null;
    if (badge) {
      badge.className = `badge ${result ? (Number(result.return_percent || 0) >= 0 ? "ok" : "warn") : ""}`.trim();
      badge.textContent = result ? "已完成" : "未執行";
    }
    if (!metrics || !detail || !warnings) return;
    if (!result) {
      metrics.innerHTML = "";
      detail.innerHTML = "";
      warnings.innerHTML = "";
      return;
    }
    metrics.innerHTML = [
      previewMetricCard("最終資產", `${formatMetricValue(result.final_value_points, 2)} POINTS`, `初始 ${formatMetricValue(result.initial_cash_points, 2)} POINTS`),
      previewMetricCard("最終報酬", `${formatSignedMetric(result.return_percent, 2, "%")}`, `${formatSignedMetric(result.pnl_points, 2)} POINTS`),
      previewMetricCard("成交數", String(Number(result.trade_count || 0)), "backtest only"),
      previewMetricCard("最大回撤", `${formatMetricValue(result.max_drawdown_percent || 0, 2)}%`, `資料 ${Number(result.candle_count || 0)} 根`),
    ].join("");
    detail.innerHTML = [
      `<div class="preview-detail-item">資料來源：${html(result.data_source || "-")} · 市場：${html(result.market_symbol || "-")} · 週期：${html(result.interval || "-")}</div>`,
      `<div class="preview-detail-item">區間：${html(String(result.first_candle_time || "-"))} ～ ${html(String(result.last_candle_time || "-"))}</div>`,
      `<div class="preview-detail-item">回測視窗：約 ${html(formatMetricValue(result.backtest_window_days || 0, 1))} 天 · 上限 ${html(Number(result.max_backtest_candles || 0).toLocaleString("zh-Hant-TW"))} 根</div>`,
    ].join("");
    const rangeWarnings = Array.isArray(result.range_warnings) ? result.range_warnings : [];
    warnings.innerHTML = rangeWarnings.length
      ? rangeWarnings.map((item) => `<div class="preview-warning-item">⚠ ${html(item)}</div>`).join("")
      : "";
  }

  function renderWorkflowPreviewMarkets() {
    const select = $("workflow-preview-market");
    if (!select) return;
    const markets = Array.isArray(workflowPreviewState.markets) ? workflowPreviewState.markets : [];
    const current = currentPreviewConfig().market_symbol || "BTC/POINTS";
    const options = markets.length
      ? markets.map((market) => {
          const value = String(market.symbol || "");
          const label = String(market.display_market_symbol || market.symbol || value);
          return `<option value="${html(value)}">${html(label)}</option>`;
        }).join("")
      : `<option value="BTC/POINTS">BTC/USDT</option>`;
    select.innerHTML = options;
    if (Array.from(select.options).some((item) => item.value === current)) select.value = current;
    else if (Array.from(select.options).some((item) => item.value === "BTC/POINTS")) select.value = "BTC/POINTS";
  }

  async function loadWorkflowPreviewMarkets() {
    try {
      const json = await fetchWorkflowPreviewJson("/trading/markets");
      workflowPreviewState.markets = Array.isArray(json.markets) ? json.markets : [];
      renderWorkflowPreviewMarkets();
      savePreviewConfig();
    } catch (err) {
      renderWorkflowPreviewMarkets();
      previewStatus(err.message || "市場列表載入失敗", false);
    }
  }

  async function runWorkflowPreviewBacktest() {
    const errors = validateWorkflow().filter((item) => item.level === "err");
    if (errors.length) {
      previewStatus(workflowValidationErrorSummary(errors, "Workflow validation 未通過"), false);
      return;
    }
    const config = currentPreviewConfig();
    if (!config.market_symbol) {
      previewStatus("請先選擇回測市場。", false);
      return;
    }
    if (config.start_time && config.end_time && config.start_time >= config.end_time) {
      previewStatus("回測開始時間必須早於結束時間。", false);
      return;
    }
    savePreviewConfig();
    previewStatus("回測中…");
    renderWorkflowPreviewResult(null);
    try {
      const json = await fetchWorkflowPreviewJson("/trading/workflow-editor/backtest", {
        method: "POST",
        body: JSON.stringify({
          market_symbol: config.market_symbol,
          timeframe: config.timeframe,
          initial_cash_points: config.initial_cash_points,
          slippage_percent: config.slippage_percent,
          start_time: config.start_time,
          end_time: config.end_time,
          workflow_json: normalizeWorkflow(workflow),
        }),
      });
      renderWorkflowPreviewResult(json);
      previewStatus(
        `回測完成：報酬 ${formatSignedMetric(json.return_percent, 2, "%")} · 成交 ${Number(json.trade_count || 0)} 次 · 最大回撤 ${formatMetricValue(json.max_drawdown_percent || 0, 2)}%`,
        Number(json.return_percent || 0) >= 0,
      );
    } catch (err) {
      renderWorkflowPreviewResult(null);
      previewStatus(err.message || "回測失敗", false);
    }
  }

  function bindWorkflowPreview() {
    if (workflowPreviewBound) return;
    workflowPreviewBound = true;
    $("workflow-preview-run-btn")?.addEventListener("click", () => { runWorkflowPreviewBacktest(); });
    $("workflow-preview-range-180d")?.addEventListener("click", () => { shiftPreviewRange(180); });
    $("workflow-preview-range-365d")?.addEventListener("click", () => { shiftPreviewRange(365); });
    ["workflow-preview-market", "workflow-preview-timeframe", "workflow-preview-initial-cash", "workflow-preview-slippage", "workflow-preview-start", "workflow-preview-end"].forEach((id) => {
      $(id)?.addEventListener("change", savePreviewConfig);
      $(id)?.addEventListener("input", savePreviewConfig);
    });
  }

  function initializeWorkflowPreview() {
    applyPreviewConfig(loadPreviewConfig());
    bindWorkflowPreview();
    renderWorkflowPreviewMarkets();
    renderWorkflowPreviewResult(null);
    loadWorkflowPreviewMarkets();
  }

  function loadInitialWorkflow() {
    try {
      const saved = localStorage.getItem(editorScopedStorageKey(STORAGE_KEY));
      if (saved) return JSON.parse(saved);
    } catch (err) {
      // keep default
    }
    return templateWorkflow();
  }

  function templateWorkflow() {
    return {
      version: 2,
      strategy_kind: "workflow_graph",
      source: "workflow_editor",
      name: "BTC 多層決策策略",
      description: "強制止損優先，其次風險減倉，再做多條件分批進場。",
      start_node_id: "start",
      nodes: [
        node("start", "start", 40, 210),
        node("condition", "stop_price", 250, 60, { label: "價格 < 50000", condition: { type: "price_below", value: 50000 }, priority: 100 }),
        node("action", "close_all", 500, 60, { label: "立即全平", action: { type: "close_all", step: 1, order_type: "market" }, priority: 100 }),
        node("condition", "has_position", 250, 210, { label: "已有持倉", condition: { type: "has_position", value: true }, priority: 50 }),
        node("condition", "risk_price", 250, 360, { label: "價格 < 80000", condition: { type: "price_below", value: 80000 }, priority: 50 }),
        node("logic", "risk_and", 500, 285, { label: "減倉條件 AND", operator: "AND", priority: 50 }),
        node("action", "reduce_50", 750, 285, { label: "賣出 50%", action: { type: "sell_percent", percent: 50, step: 1, order_type: "market" }, priority: 50 }),
        node("condition", "entry_price", 250, 540, { label: "價格 < 100000", condition: { type: "price_below", value: 100000 }, priority: 10 }),
        node("condition", "entry_kd", 250, 690, { label: "KD > 50", condition: { type: "kd_above", value: 50 }, priority: 10 }),
        node("logic", "entry_or", 500, 615, { label: "KD 或 RSI", operator: "OR", priority: 10 }),
        node("condition", "entry_ma", 250, 840, { label: "MA50 上方", condition: { type: "ma_position", period: 50, position: "above" }, priority: 10 }),
        node("logic", "entry_and", 750, 615, { label: "進場條件 AND", operator: "AND", priority: 10 }),
        node("control", "entry_cooldown", 1000, 615, { label: "每小時檢查", cooldown_seconds: 3600, max_runs: 100, priority: 10 }),
        node("action", "buy_10", 1250, 555, { label: "買入 10%", action: { type: "buy_percent", percent: 10, step: 1, order_type: "market" }, priority: 10 }),
        node("action", "buy_20", 1250, 705, { label: "買入 20%", action: { type: "buy_percent", percent: 20, step: 2, order_type: "market" }, priority: 10 }),
      ],
      edges: [
        edge("start", "out", "stop_price", "in"),
        edge("stop_price", "true", "close_all", "in"),
        edge("start", "out", "has_position", "in"),
        edge("start", "out", "risk_price", "in"),
        edge("has_position", "true", "risk_and", "in"),
        edge("risk_price", "true", "risk_and", "in"),
        edge("risk_and", "true", "reduce_50", "in"),
        edge("start", "out", "entry_price", "in"),
        edge("start", "out", "entry_kd", "in"),
        edge("entry_kd", "true", "entry_or", "in"),
        edge("entry_price", "true", "entry_and", "in"),
        edge("entry_or", "true", "entry_and", "in"),
        edge("entry_ma", "true", "entry_and", "in"),
        edge("start", "out", "entry_ma", "in"),
        edge("entry_and", "true", "entry_cooldown", "in"),
        edge("entry_cooldown", "then", "buy_10", "in"),
        edge("entry_cooldown", "then", "buy_20", "in"),
      ],
    };
  }

  function node(type, id, x, y, extra = {}) {
    const spec = PORTS[type] || PORTS.condition;
    return {
      id,
      type,
      label: extra.label || `${type} node`,
      x,
      y,
      inputs: spec.inputs.slice(),
      outputs: spec.outputs.slice(),
      priority: numberValue(extra.priority, 0),
      ...(type === "condition" ? { condition: cleanCondition(extra.condition || { type: "price_below", value: 100000 }) } : {}),
      ...(type === "logic" ? { operator: String(extra.operator || "AND").toUpperCase() } : {}),
      ...(type === "control" ? { cooldown_seconds: numberValue(extra.cooldown_seconds, 300), max_runs: Math.max(1, numberValue(extra.max_runs, 100)) } : {}),
      ...(type === "action" ? { action: cleanAction(extra.action || { type: "buy_percent", percent: 10, step: 1, order_type: "market" }) } : {}),
    };
  }

  function edge(from, fromPort, to, toPort) {
    return { id: uid("edge"), from, from_port: fromPort, to, to_port: toPort };
  }

  function legacyBranchesToGraph(input) {
    const base = templateWorkflow();
    const branches = Array.isArray(input?.branches) ? input.branches : [];
    if (!branches.length) return base;
    const graph = {
      version: 2,
      strategy_kind: "workflow_graph",
      source: "workflow_editor_legacy_import",
      name: input.name || "Legacy Workflow Import",
      description: input.description || "",
      start_node_id: "start",
      nodes: [node("start", "start", 40, 180)],
      edges: [],
    };
    branches.slice(0, 20).forEach((branch, branchIndex) => {
      const y = 80 + branchIndex * 260;
      const logicId = uid("logic");
      const controlId = uid("control");
      graph.nodes.push(node("logic", logicId, 520, y + 50, { label: branch.name || `分支 ${branchIndex + 1}`, operator: branch.logic || "AND", priority: branch.priority || 0 }));
      graph.nodes.push(node("control", controlId, 760, y + 50, { label: "分支控制", cooldown_seconds: branch.cooldown_seconds || 0, max_runs: branch.max_runs || 100, priority: branch.priority || 0 }));
      (branch.conditions || [{ type: "always" }]).slice(0, 20).forEach((condition, index) => {
        const conditionId = uid("condition");
        graph.nodes.push(node("condition", conditionId, 260, y + index * 80, { label: conditionLabel(cleanCondition(condition)), condition, priority: branch.priority || 0 }));
        graph.edges.push(edge("start", "out", conditionId, "in"), edge(conditionId, "true", logicId, "in"));
      });
      graph.edges.push(edge(logicId, "true", controlId, "in"));
      (branch.actions || [{ type: "hold" }]).slice(0, 20).forEach((action, index) => {
        const actionId = uid("action");
        graph.nodes.push(node("action", actionId, 1000, y + index * 90, { label: actionLabel(cleanAction(action)), action, priority: branch.priority || 0 }));
        graph.edges.push(edge(controlId, "then", actionId, "in"));
      });
    });
    return graph;
  }

  function normalizeWorkflow(input) {
    let base = input && typeof input === "object" ? input : templateWorkflow();
    if (!Array.isArray(base.nodes) && Array.isArray(base.branches)) base = legacyBranchesToGraph(base);
    const fallback = templateWorkflow();
    const cleanNodes = (Array.isArray(base.nodes) && base.nodes.length ? base.nodes : fallback.nodes).slice(0, 100).map((raw, index) => {
      const type = NODE_TYPES.includes(raw.type) ? raw.type : "condition";
      const clean = node(type, String(raw.id || uid(type)).slice(0, 80), numberValue(raw.x, index * 180), numberValue(raw.y, 120), raw);
      clean.label = String(raw.label || raw.name || clean.label || clean.id).slice(0, 80);
      return clean;
    });
    if (!cleanNodes.some((item) => item.type === "start")) cleanNodes.unshift(node("start", "start", 40, 120));
    const ids = new Set(cleanNodes.map((item) => item.id));
    const cleanEdges = (Array.isArray(base.edges) ? base.edges : []).slice(0, 200).filter((raw) => (
      ids.has(raw.from || raw.source) && ids.has(raw.to || raw.target)
    )).map((raw) => ({
      id: String(raw.id || uid("edge")).slice(0, 80),
      from: String(raw.from || raw.source).slice(0, 80),
      from_port: String(raw.from_port || raw.source_port || "out").toLowerCase(),
      to: String(raw.to || raw.target).slice(0, 80),
      to_port: String(raw.to_port || raw.target_port || "in").toLowerCase(),
    })).filter((item) => {
      const source = cleanNodes.find((nodeItem) => nodeItem.id === item.from);
      const target = cleanNodes.find((nodeItem) => nodeItem.id === item.to);
      return source && target && source.outputs.includes(item.from_port) && target.inputs.includes(item.to_port);
    });
    return {
      version: 2,
      strategy_kind: "workflow_graph",
      source: String(base.source || "workflow_editor").slice(0, 80),
      name: String(base.name || fallback.name).slice(0, 80),
      description: String(base.description || "").slice(0, 160),
      start_node_id: String(base.start_node_id || cleanNodes.find((item) => item.type === "start")?.id || cleanNodes[0].id).slice(0, 80),
      nodes: cleanNodes,
      edges: cleanEdges,
    };
  }

  function cleanCondition(raw) {
    if (raw?.AND || raw?.OR || raw?.NOT) return raw;
    const type = CONDITION_TYPES.includes(raw?.type) ? raw.type : "price_below";
    const clean = { type };
    if (type === "ma_position") {
      clean.period = Math.max(1, numberValue(raw.period, 50));
      clean.position = raw.position === "below" ? "below" : "above";
    } else if (type === "bb_position") {
      clean.position = ["above_mid", "below_mid", "above_upper", "below_lower"].includes(raw.position) ? raw.position : "above_mid";
    } else if (type === "has_position") {
      clean.value = raw.value === false ? false : true;
    } else if (type !== "always") {
      clean.value = numberValue(raw.value, type.includes("rsi") || type.includes("kd") ? 50 : 100000);
    }
    return clean;
  }

  function cleanAction(raw) {
    const type = ACTION_TYPES.includes(raw?.type) ? raw.type : "buy_percent";
    const clean = {
      type,
      step: Math.max(1, numberValue(raw.step, 1)),
      order_type: raw.order_type === "limit" ? "limit" : "market",
    };
    if (type === "buy_amount") clean.amount_points = Math.max(1, numberValue(raw.amount_points ?? raw.value, 100));
    if (type === "buy_percent" || type === "sell_percent") clean.percent = Math.max(0, Math.min(100, numberValue(raw.percent ?? raw.value, 10)));
    if (numberValue(raw.limit_price_points, 0) > 0) clean.limit_price_points = numberValue(raw.limit_price_points, 0);
    return clean;
  }

  function conditionLabel(condition) {
    if (condition.AND) return "Nested AND";
    if (condition.OR) return "Nested OR";
    if (condition.NOT) return "Nested NOT";
    if (condition.type === "price_below") return `價格 < ${condition.value}`;
    if (condition.type === "price_above") return `價格 > ${condition.value}`;
    if (condition.type === "rsi_above" || condition.type === "rsi_below" || condition.type === "kd_above" || condition.type === "kd_below") return `${CONDITION_LABELS[condition.type]} ${condition.value}`;
    if (condition.type === "ma_position") return `價格在 MA${condition.period || 50} ${condition.position === "below" ? "下方" : "上方"}`;
    if (condition.type === "bb_position") return `布林 ${condition.position || "above_mid"}`;
    if (condition.type === "has_position") return condition.value === false ? "沒有持倉" : "已有持倉";
    return CONDITION_LABELS[condition.type] || condition.type || "條件";
  }

  function actionLabel(action) {
    if (action.type === "buy_percent") return `買入 ${action.percent}%`;
    if (action.type === "buy_amount") return `買入 ${action.amount_points} 點`;
    if (action.type === "sell_percent") return `賣出 ${action.percent}%`;
    if (action.type === "close_all") return "全部平倉";
    return ACTION_LABELS[action.type] || action.type || "行為";
  }

  function selectedNode() {
    return workflow.nodes.find((item) => item.id === selectedNodeId) || workflow.nodes[0];
  }

  function nodeTitle(item) {
    if (item.type === "condition") return conditionLabel(item.condition || {});
    if (item.type === "action") return actionLabel(item.action || {});
    if (item.type === "logic") return `${item.operator || "AND"} 邏輯`;
    if (item.type === "control") return `冷卻 ${Number(item.cooldown_seconds || 0)} 秒`;
    return "Start";
  }

  function renderSummary() {
    const badges = $("summaryBadges");
    if (!badges) return;
    badges.innerHTML = `
      <span class="badge ok">${workflow.nodes.length} nodes</span>
      <span class="badge">${workflow.edges.length} edges</span>
      <span class="badge">input/output ports</span>
      <span class="badge">TRUE/FALSE branch</span>
    `;
  }

  function renderTabs() {
    const target = $("branchTabs");
    if (!target) return;
    target.innerHTML = `
      <button class="branch-tab active" type="button">Node Graph</button>
      <details class="branch-action-menu">
        <summary class="branch-tab">圖表操作</summary>
        <div class="branch-action-menu-body">
          <button class="branch-tab" type="button" data-add-node="logic:AND">新增 AND</button>
          <button class="branch-tab" type="button" data-add-node="logic:OR">新增 OR</button>
          <button class="branch-tab" type="button" data-add-node="control:cooldown">新增控制</button>
          <button class="branch-tab" type="button" data-auto-layout>自動整理</button>
        </div>
      </details>
    `;
  }

  function portButton(item, port, direction) {
    return `<button class="port ${direction} ${port}" type="button" data-port-node="${html(item.id)}" data-port-name="${html(port)}" data-port-direction="${direction}" title="${direction === "out" ? "輸出" : "輸入"} ${html(port)}">${html(port.toUpperCase())}</button>`;
  }

  function renderNode(item) {
    const selected = item.id === selectedNodeId ? "selected" : "";
    return `
      <article class="node graph-node ${html(item.type)} ${selected}" data-node-id="${html(item.id)}" data-x="${Number(item.x || 0)}" data-y="${Number(item.y || 0)}" draggable="true">
        <div class="node-top">
          <span class="node-kind">${html(item.type)}</span>
          <span class="node-tools">
            <button type="button" data-duplicate-node="${html(item.id)}" title="複製">⧉</button>
            ${item.type !== "start" ? `<button type="button" data-delete-node="${html(item.id)}" title="刪除">×</button>` : ""}
          </span>
        </div>
        <div class="node-label">${html(item.label || nodeTitle(item))}</div>
        <div class="node-sub">${html(nodeTitle(item))}</div>
        ${renderNodeInlineControls(item)}
        <div class="ports">
          <div>${(item.inputs || []).map((port) => portButton(item, port, "in")).join("")}</div>
          <div>${(item.outputs || []).map((port) => portButton(item, port, "out")).join("")}</div>
        </div>
      </article>
    `;
  }

  function renderNodeInlineControls(item) {
    const nodeAttr = `data-inline-node="${html(item.id)}" data-node-inline`;
    if (item.type === "condition") {
      const condition = item.condition || {};
      const valueInput = condition.type && !["always", "ma_position", "bb_position", "has_position"].includes(condition.type)
        ? `<input ${nodeAttr} data-condition-field="value" type="number" step="0.01" value="${html(condition.value ?? "")}" aria-label="條件數值">`
        : "";
      const periodInput = condition.type === "ma_position"
        ? `<input ${nodeAttr} data-condition-field="period" type="number" min="1" value="${Number(condition.period || 50)}" aria-label="MA 週期">`
        : "";
      const positionSelect = condition.type === "ma_position"
        ? `<select ${nodeAttr} data-condition-field="position" aria-label="均線位置"><option value="above" ${condition.position !== "below" ? "selected" : ""}>上方</option><option value="below" ${condition.position === "below" ? "selected" : ""}>下方</option></select>`
        : "";
      const bbSelect = condition.type === "bb_position"
        ? `<select ${nodeAttr} data-condition-field="position" aria-label="布林位置"><option value="above_mid" ${condition.position === "above_mid" ? "selected" : ""}>中線上方</option><option value="below_mid" ${condition.position === "below_mid" ? "selected" : ""}>中線下方</option><option value="above_upper" ${condition.position === "above_upper" ? "selected" : ""}>上軌上方</option><option value="below_lower" ${condition.position === "below_lower" ? "selected" : ""}>下軌下方</option></select>`
        : "";
      const boolSelect = condition.type === "has_position"
        ? `<select ${nodeAttr} data-condition-field="value_bool" aria-label="持倉狀態"><option value="true" ${condition.value !== false ? "selected" : ""}>有持倉</option><option value="false" ${condition.value === false ? "selected" : ""}>沒有持倉</option></select>`
        : "";
      return `
        <div class="node-inline-controls">
          <select ${nodeAttr} data-condition-field="type" aria-label="條件類型">${CONDITION_TYPES.map((type) => `<option value="${type}" ${type === condition.type ? "selected" : ""}>${html(CONDITION_LABELS[type] || type)}</option>`).join("")}</select>
          ${valueInput}${periodInput}${positionSelect}${bbSelect}${boolSelect}
        </div>
      `;
    }
    if (item.type === "logic") {
      return `
        <div class="node-inline-controls">
          <select ${nodeAttr} data-node-field="operator" aria-label="邏輯"><option value="AND" ${item.operator === "AND" ? "selected" : ""}>AND</option><option value="OR" ${item.operator === "OR" ? "selected" : ""}>OR</option><option value="NOT" ${item.operator === "NOT" ? "selected" : ""}>NOT</option></select>
        </div>
      `;
    }
    if (item.type === "control") {
      return `
        <div class="node-inline-controls two">
          <input ${nodeAttr} data-node-field="cooldown_seconds" type="number" min="0" value="${Number(item.cooldown_seconds || 0)}" aria-label="冷卻秒數">
          <input ${nodeAttr} data-node-field="max_runs" type="number" min="1" value="${Number(item.max_runs || 100)}" aria-label="最大執行次數">
        </div>
      `;
    }
    if (item.type === "action") {
      const action = item.action || {};
      const percentInput = ["buy_percent", "sell_percent"].includes(action.type)
        ? `<input ${nodeAttr} data-action-field="percent" type="number" min="0" max="100" step="0.01" value="${html(action.percent ?? 10)}" aria-label="百分比">`
        : "";
      const amountInput = action.type === "buy_amount"
        ? `<input ${nodeAttr} data-action-field="amount_points" type="number" min="1" step="1" value="${html(action.amount_points ?? 100)}" aria-label="買入點數">`
        : "";
      return `
        <div class="node-inline-controls">
          <select ${nodeAttr} data-action-field="type" aria-label="行為類型">${ACTION_TYPES.map((type) => `<option value="${type}" ${type === action.type ? "selected" : ""}>${html(ACTION_LABELS[type] || type)}</option>`).join("")}</select>
          <input ${nodeAttr} data-action-field="step" type="number" min="1" value="${Number(action.step || 1)}" aria-label="Step">
          ${percentInput}${amountInput}
        </div>
      `;
    }
    return "";
  }

  function portPoint(nodeItem, portName, direction) {
    const ports = direction === "out" ? (nodeItem.outputs || []) : (nodeItem.inputs || []);
    const index = Math.max(0, ports.indexOf(portName));
    const count = Math.max(1, ports.length);
    const x = Number(nodeItem.x || 0) + (direction === "out" ? GRAPH_NODE_WIDTH : 0);
    const y = Number(nodeItem.y || 0) + GRAPH_NODE_HEIGHT - 18 - ((count - 1 - index) * 24);
    return { x, y };
  }

  function edgePath(edgeItem) {
    const source = workflow.nodes.find((nodeItem) => nodeItem.id === edgeItem.from);
    const target = workflow.nodes.find((nodeItem) => nodeItem.id === edgeItem.to);
    if (!source || !target) return "";
    const start = portPoint(source, edgeItem.from_port, "out");
    const end = portPoint(target, edgeItem.to_port, "in");
    const dx = Math.max(80, Math.abs(end.x - start.x) * 0.45);
    return `M ${start.x} ${start.y} C ${start.x + dx} ${start.y}, ${end.x - dx} ${end.y}, ${end.x} ${end.y}`;
  }

  function renderGraphEdgeLayer() {
    const maxX = Math.max(GRAPH_NODE_WIDTH + 260, ...workflow.nodes.map((item) => Number(item.x || 0) + GRAPH_NODE_WIDTH + 180));
    const maxY = Math.max(GRAPH_NODE_HEIGHT + 260, ...workflow.nodes.map((item) => Number(item.y || 0) + GRAPH_NODE_HEIGHT + 160));
    return `
      <svg class="graph-edge-layer" width="${Math.ceil(maxX)}" height="${Math.ceil(maxY)}" viewBox="0 0 ${Math.ceil(maxX)} ${Math.ceil(maxY)}" aria-hidden="true">
        ${workflow.edges.map((item) => {
          const kind = ["true", "then"].includes(item.from_port) ? "positive" : (["false", "wait"].includes(item.from_port) ? "negative" : "neutral");
          return `<path class="graph-edge ${kind}" d="${html(edgePath(item))}" data-edge-path="${html(item.id)}"></path>`;
        }).join("")}
      </svg>
    `;
  }

  function renderEdges() {
    return `
      <div class="graph-edges">
        <h3>Edges</h3>
        ${workflow.edges.length ? workflow.edges.map((item) => `
          <div class="edge-row">
            <span>${html(item.from)}.${html(item.from_port)} → ${html(item.to)}.${html(item.to_port)}</span>
            <button type="button" data-delete-edge="${html(item.id)}">刪除</button>
          </div>
        `).join("") : '<div class="empty">尚未建立連線。點輸出 port，再點輸入 port。</div>'}
      </div>`;
  }

  function renderCanvas() {
    const canvas = $("canvas");
    if (!canvas) return;
    canvas.innerHTML = `
      <section class="panel graph-panel">
        <div class="branch-head">
          <div>
            <div class="branch-name">Node Graph Canvas</div>
            <div class="drive-card-sub">點擊輸出 port 後再點擊輸入 port 建立連線；condition / logic 的 TRUE/FALSE port 會決定決策分支。</div>
          </div>
          <div class="row-actions">
            <button type="button" data-clear-connection>取消連線</button>
          </div>
        </div>
        <div class="graph-canvas" data-graph-canvas>
          ${renderGraphEdgeLayer()}
          ${workflow.nodes.map(renderNode).join("")}
        </div>
        ${renderEdges()}
      </section>
    `;
  }

  function applyGraphNodePositions() {
    document.querySelectorAll("[data-node-id][data-x][data-y]").forEach((el) => {
      el.style.left = `${Number(el.dataset.x || 0)}px`;
      el.style.top = `${Number(el.dataset.y || 0)}px`;
    });
  }

  function renderInspector() {
    const item = selectedNode();
    const inspector = $("inspector");
    const badge = $("selectedBadge");
    if (!item || !inspector) return;
    if (badge) badge.textContent = `${item.type} · ${item.id}`;
    const common = `
      <label class="field">節點名稱<input data-node-field="label" value="${html(item.label || "")}"></label>
      <div class="two-col">
        <label class="field">X<input data-node-field="x" type="number" value="${Number(item.x || 0)}"></label>
        <label class="field">Y<input data-node-field="y" type="number" value="${Number(item.y || 0)}"></label>
      </div>
      <label class="field">優先權<input data-node-field="priority" type="number" value="${Number(item.priority || 0)}"></label>
    `;
    if (item.type === "condition") {
      const condition = item.condition || {};
      inspector.innerHTML = `
        <div class="inspector-card">
          ${common}
          <label class="field">條件類型<select data-condition-field="type">${CONDITION_TYPES.map((type) => `<option value="${type}">${html(CONDITION_LABELS[type] || type)}</option>`).join("")}</select></label>
          ${condition.type && !["always", "ma_position", "bb_position", "has_position"].includes(condition.type) ? `<label class="field">數值<input data-condition-field="value" type="number" step="0.01" value="${html(condition.value ?? "")}"></label>` : ""}
          ${condition.type === "ma_position" ? `<div class="two-col"><label class="field">MA 週期<input data-condition-field="period" type="number" min="1" value="${Number(condition.period || 50)}"></label><label class="field">位置<select data-condition-field="position"><option value="above">上方</option><option value="below">下方</option></select></label></div>` : ""}
          ${condition.type === "bb_position" ? `<label class="field">布林位置<select data-condition-field="position"><option value="above_mid">中線上方</option><option value="below_mid">中線下方</option><option value="above_upper">上軌上方</option><option value="below_lower">下軌下方</option></select></label>` : ""}
          ${condition.type === "has_position" ? `<label class="field">持倉條件<select data-condition-field="value_bool"><option value="true">有持倉</option><option value="false">沒有持倉</option></select></label>` : ""}
        </div>`;
      const typeField = document.querySelector('[data-condition-field="type"]');
      if (typeField) typeField.value = condition.type || "always";
      const position = document.querySelector('[data-condition-field="position"]');
      if (position) position.value = condition.position || "above";
      const boolField = document.querySelector('[data-condition-field="value_bool"]');
      if (boolField) boolField.value = condition.value === false ? "false" : "true";
      return;
    }
    if (item.type === "logic") {
      inspector.innerHTML = `
        <div class="inspector-card">
          ${common}
          <label class="field">邏輯<select data-node-field="operator"><option value="AND">AND：全部成立</option><option value="OR">OR：任一成立</option><option value="NOT">NOT：反向</option></select></label>
          <div class="hint">Logic node 可串接多個 condition 或 logic，因此能形成 nested AND/OR 決策樹。</div>
        </div>`;
      document.querySelector('[data-node-field="operator"]').value = item.operator || "AND";
      return;
    }
    if (item.type === "control") {
      inspector.innerHTML = `
        <div class="inspector-card">
          ${common}
          <div class="two-col">
            <label class="field">冷卻秒數<input data-node-field="cooldown_seconds" type="number" min="0" value="${Number(item.cooldown_seconds || 0)}"></label>
            <label class="field">最大執行次數<input data-node-field="max_runs" type="number" min="1" value="${Number(item.max_runs || 100)}"></label>
          </div>
        </div>`;
      return;
    }
    if (item.type === "action") {
      const action = item.action || {};
      inspector.innerHTML = `
        <div class="inspector-card">
          ${common}
          <label class="field">行為類型<select data-action-field="type">${ACTION_TYPES.map((type) => `<option value="${type}">${html(ACTION_LABELS[type] || type)}</option>`).join("")}</select></label>
          <div class="two-col">
            <label class="field">Step<input data-action-field="step" type="number" min="1" value="${Number(action.step || 1)}"></label>
            <label class="field">委託型態<select data-action-field="order_type"><option value="market">市價</option><option value="limit">限價</option></select></label>
          </div>
          ${["buy_percent", "sell_percent"].includes(action.type) ? `<label class="field">百分比<input data-action-field="percent" type="number" min="0" max="100" step="0.01" value="${html(action.percent ?? 10)}"></label>` : ""}
          ${action.type === "buy_amount" ? `<label class="field">買入點數<input data-action-field="amount_points" type="number" min="1" step="1" value="${html(action.amount_points ?? 100)}"></label>` : ""}
          ${!["close_all", "hold"].includes(action.type) ? `<label class="field">限價價格<input data-action-field="limit_price_points" type="number" min="0" step="0.01" value="${html(action.limit_price_points ?? "")}" placeholder="市價可留空"></label>` : ""}
          <div class="hint">相同路徑的分批買賣依 step 由小到大執行；已執行的 step 不會重複觸發。</div>
        </div>`;
      document.querySelector('[data-action-field="type"]').value = action.type || "hold";
      document.querySelector('[data-action-field="order_type"]').value = action.order_type || "market";
      return;
    }
    inspector.innerHTML = `<div class="inspector-card">${common}<div class="hint">Start node 是 graph execution order 的入口。</div></div>`;
  }

  function refreshSelectedNodeDom(item) {
    const nodeEl = document.querySelector(`[data-node-id="${CSS.escape(item.id)}"]`);
    if (!nodeEl) return;
    const label = nodeEl.querySelector(".node-label");
    const sub = nodeEl.querySelector(".node-sub");
    if (label) label.textContent = item.label || nodeTitle(item);
    if (sub) sub.textContent = nodeTitle(item);
    nodeEl.dataset.x = String(Number(item.x || 0));
    nodeEl.dataset.y = String(Number(item.y || 0));
    nodeEl.style.left = `${Number(item.x || 0)}px`;
    nodeEl.style.top = `${Number(item.y || 0)}px`;
  }

  function refreshGraphEdgeLayer() {
    const canvas = document.querySelector("[data-graph-canvas]");
    const layer = canvas?.querySelector(".graph-edge-layer");
    if (!canvas || !layer) return;
    const maxX = Math.max(GRAPH_NODE_WIDTH + 260, ...workflow.nodes.map((item) => Number(item.x || 0) + GRAPH_NODE_WIDTH + 180));
    const maxY = Math.max(GRAPH_NODE_HEIGHT + 260, ...workflow.nodes.map((item) => Number(item.y || 0) + GRAPH_NODE_HEIGHT + 160));
    layer.setAttribute("width", String(Math.ceil(maxX)));
    layer.setAttribute("height", String(Math.ceil(maxY)));
    layer.setAttribute("viewBox", `0 0 ${Math.ceil(maxX)} ${Math.ceil(maxY)}`);
    workflow.edges.forEach((item) => {
      const path = layer.querySelector(`[data-edge-path="${CSS.escape(item.id)}"]`);
      if (path) path.setAttribute("d", edgePath(item));
    });
  }

  function refreshAfterFieldEdit(item, { rebuild = false } = {}) {
    if (rebuild) {
      render();
      return;
    }
    refreshSelectedNodeDom(item);
    refreshGraphEdgeLayer();
    renderSummary();
    renderValidation();
    syncJson();
  }

  function validateWorkflow() {
    const issues = [];
    const ids = new Set(workflow.nodes.map((item) => item.id));
    const start = workflow.nodes.filter((item) => item.type === "start");
    if (start.length !== 1) issues.push({ level: "err", text: "必須剛好有一個 Start node。" });
    if (!workflow.nodes.some((item) => item.type === "action")) issues.push({ level: "err", text: "至少需要一個 Action node。" });
    workflow.edges.forEach((item) => {
      const source = workflow.nodes.find((nodeItem) => nodeItem.id === item.from);
      const target = workflow.nodes.find((nodeItem) => nodeItem.id === item.to);
      if (!source || !target) issues.push({ level: "err", text: `Edge ${item.id} 指向不存在的 node。` });
      else if (!source.outputs.includes(item.from_port) || !target.inputs.includes(item.to_port)) issues.push({ level: "err", text: `Edge ${item.id} 使用了不合法的 port。` });
    });
    workflow.nodes.filter((item) => item.type !== "start").forEach((item) => {
      if (!workflow.edges.some((edgeItem) => edgeItem.to === item.id)) issues.push({ level: item.type === "action" ? "err" : "warn", text: `${item.label || item.id} 沒有 input edge。` });
      if (item.type !== "action" && item.type !== "control" && !workflow.edges.some((edgeItem) => edgeItem.from === item.id)) issues.push({ level: "warn", text: `${item.label || item.id} 沒有 output edge。` });
    });
    const actionSteps = {};
    workflow.nodes.filter((item) => item.type === "action").forEach((item) => {
      const action = item.action || {};
      const key = `${action.type}:${action.step}`;
      actionSteps[key] = (actionSteps[key] || 0) + 1;
      if (["buy_percent", "sell_percent"].includes(action.type) && (numberValue(action.percent, 0) <= 0 || numberValue(action.percent, 0) > 100)) issues.push({ level: "err", text: `${item.label} 百分比必須在 0 到 100。` });
    });
    Object.entries(actionSteps).forEach(([key, count]) => {
      if (count > 1) issues.push({ level: "warn", text: `偵測到重複 action step：${key}，確認是否刻意分支共用。` });
    });
    if (!ids.has(workflow.start_node_id)) issues.push({ level: "err", text: "start_node_id 指向不存在的 node。" });
    return issues;
  }

  function renderValidation() {
    const issues = validateWorkflow();
    const badge = $("validationBadge");
    const list = $("validationList");
    if (!badge || !list) return;
    const hasError = issues.some((item) => item.level === "err");
    badge.className = `badge ${issues.length ? (hasError ? "err" : "warn") : "ok"}`;
    badge.textContent = issues.length ? (hasError ? "需修正" : "有提醒") : "可儲存";
    list.innerHTML = issues.length ? issues.map((item) => `<li class="${item.level}">${html(item.text)}</li>`).join("") : "<li>Graph validation passed。節點、edge、port 目前可執行。</li>";
  }

  function workflowValidationErrorSummary(issues, prefix = "Graph validation 未通過") {
    const errors = Array.isArray(issues) ? issues.filter((item) => item && item.level === "err") : [];
    if (!errors.length) return prefix;
    const details = errors
      .slice(0, 3)
      .map((item) => String(item.text || "").trim())
      .filter(Boolean);
    const suffix = errors.length > details.length ? `；另有 ${errors.length - details.length} 項錯誤，詳見「策略檢查」` : "；詳見「策略檢查」";
    return `${prefix}：${details.join("；")}${suffix}`;
  }

  function syncJson() {
    const out = $("jsonOut");
    if (out) out.value = JSON.stringify(workflow, null, 2);
  }

  function setStatus(message, good = true) {
    const el = $("status");
    if (!el) return;
    el.textContent = message || "";
    el.style.color = good ? "var(--muted)" : "var(--red)";
  }

  function render() {
    workflow = normalizeWorkflow(workflow);
    if (!workflow.nodes.some((item) => item.id === selectedNodeId)) selectedNodeId = workflow.start_node_id;
    if ($("strategyName")) $("strategyName").value = workflow.name || "";
    if ($("strategyDescription")) $("strategyDescription").value = workflow.description || "";
    renderSummary();
    renderTabs();
    renderCanvas();
    applyGraphNodePositions();
    renderInspector();
    renderValidation();
    syncJson();
  }

  function addNode(type, subtype) {
    const x = 180 + workflow.nodes.length * 30;
    const y = 140 + workflow.nodes.length * 20;
    let item;
    if (type === "condition") item = node("condition", uid("condition"), x, y, { label: CONDITION_LABELS[subtype] || "條件", condition: { type: subtype || "price_below", value: subtype?.includes("rsi") || subtype?.includes("kd") ? 50 : 100000 } });
    else if (type === "action") item = node("action", uid("action"), x, y, { label: ACTION_LABELS[subtype] || "行為", action: { type: subtype || "buy_percent", percent: subtype === "sell_percent" ? 50 : 10, step: 1, order_type: "market" } });
    else if (type === "logic") item = node("logic", uid("logic"), x, y, { label: `${subtype || "AND"} 邏輯`, operator: subtype || "AND" });
    else if (type === "control") item = node("control", uid("control"), x, y, { label: "冷卻控制", cooldown_seconds: 300, max_runs: 100 });
    else item = node("condition", uid("condition"), x, y);
    workflow.nodes.push(item);
    selectedNodeId = item.id;
    render();
  }

  function addEdgeFromPorts(sourceId, sourcePort, targetId, targetPort) {
    if (sourceId === targetId) {
      setStatus("不能把 node 連到自己。", false);
      return;
    }
    const source = workflow.nodes.find((item) => item.id === sourceId);
    const target = workflow.nodes.find((item) => item.id === targetId);
    if (!source || !target || !source.outputs.includes(sourcePort) || !target.inputs.includes(targetPort)) {
      setStatus("Port 不相容，請從 output 連到 input。", false);
      return;
    }
    const exists = workflow.edges.some((item) => item.from === sourceId && item.from_port === sourcePort && item.to === targetId && item.to_port === targetPort);
    if (!exists) workflow.edges.push(edge(sourceId, sourcePort, targetId, targetPort));
    pendingConnection = null;
    setStatus("Edge 已建立。");
    render();
  }

  function handleClick(event) {
    const templateBtn = event.target.closest("#templateBtn");
    if (templateBtn) {
      workflow = templateWorkflow();
      selectedNodeId = "start";
      pendingConnection = null;
      render();
      setStatus("範例 graph 已載入。");
      return;
    }
    const importBtn = event.target.closest("#importBtn");
    if (importBtn) {
      try {
        workflow = normalizeWorkflow(JSON.parse($("jsonOut").value || "{}"));
        selectedNodeId = workflow.start_node_id;
        render();
        setStatus("JSON 已套用。");
      } catch (err) {
        setStatus(err.message || "JSON 格式錯誤", false);
      }
      return;
    }
    const saveBtn = event.target.closest("#saveBtn");
    if (saveBtn) {
      const errors = validateWorkflow().filter((item) => item.level === "err");
      if (errors.length) {
        setStatus(workflowValidationErrorSummary(errors), false);
        return;
      }
      localStorage.setItem(editorScopedStorageKey(STORAGE_KEY), JSON.stringify(workflow));
      setStatus("Workflow 已儲存，交易頁可載入這份設定。");
      return;
    }
    const copyBtn = event.target.closest("#copyBtn");
    if (copyBtn) {
      navigator.clipboard?.writeText(JSON.stringify(workflow, null, 2));
      setStatus("JSON 已複製。");
      return;
    }
    const add = event.target.closest("[data-add-node]");
    if (add) {
      const [type, subtype] = String(add.dataset.addNode || "").split(":");
      addNode(type, subtype);
      return;
    }
    const autoLayout = event.target.closest("[data-auto-layout]");
    if (autoLayout) {
      autoLayoutGraph();
      render();
      return;
    }
    const nodeEl = event.target.closest("[data-node-id]");
    if (nodeEl && !event.target.closest(".node-tools") && !event.target.closest("[data-port-node]") && !event.target.closest("[data-node-inline]")) {
      selectedNodeId = nodeEl.dataset.nodeId;
      render();
      return;
    }
    const port = event.target.closest("[data-port-node]");
    if (port) {
      const nodeId = port.dataset.portNode;
      const portName = port.dataset.portName;
      const direction = port.dataset.portDirection;
      if (direction === "out") {
        pendingConnection = { nodeId, portName };
        setStatus(`已選擇輸出 ${nodeId}.${portName}，請點目標 input port。`);
      } else if (pendingConnection) {
        addEdgeFromPorts(pendingConnection.nodeId, pendingConnection.portName, nodeId, portName);
      } else {
        setStatus("請先點 output port，再點 input port。", false);
      }
      return;
    }
    const clearConnection = event.target.closest("[data-clear-connection]");
    if (clearConnection) {
      pendingConnection = null;
      setStatus("已取消連線選取。");
      return;
    }
    const deleteEdge = event.target.closest("[data-delete-edge]");
    if (deleteEdge) {
      workflow.edges = workflow.edges.filter((item) => item.id !== deleteEdge.dataset.deleteEdge);
      render();
      return;
    }
    const deleteNode = event.target.closest("[data-delete-node]");
    if (deleteNode) {
      const id = deleteNode.dataset.deleteNode;
      workflow.nodes = workflow.nodes.filter((item) => item.id !== id);
      workflow.edges = workflow.edges.filter((item) => item.from !== id && item.to !== id);
      selectedNodeId = workflow.start_node_id;
      render();
      return;
    }
    const duplicateNode = event.target.closest("[data-duplicate-node]");
    if (duplicateNode) {
      const source = workflow.nodes.find((item) => item.id === duplicateNode.dataset.duplicateNode);
      if (!source) return;
      const copy = JSON.parse(JSON.stringify(source));
      copy.id = uid(copy.type);
      copy.label = `${copy.label || source.type} 副本`;
      copy.x = Number(copy.x || 0) + 40;
      copy.y = Number(copy.y || 0) + 40;
      workflow.nodes.push(copy);
      selectedNodeId = copy.id;
      render();
    }
  }

  function handleInput(event) {
    if (event.target.id === "strategyName") workflow.name = event.target.value.slice(0, 80);
    if (event.target.id === "strategyDescription") workflow.description = event.target.value.slice(0, 160);
    const inlineNodeId = event.target.dataset.inlineNode;
    const item = inlineNodeId ? workflow.nodes.find((nodeItem) => nodeItem.id === inlineNodeId) : selectedNode();
    if (!item) return render();
    const nodeField = event.target.dataset.nodeField;
    const conditionField = event.target.dataset.conditionField;
    const actionField = event.target.dataset.actionField;
    const typeChangingField = conditionField === "type" || actionField === "type";
    if (nodeField) {
      if (["x", "y", "priority", "cooldown_seconds", "max_runs"].includes(nodeField)) item[nodeField] = numberValue(event.target.value, item[nodeField] || 0);
      else item[nodeField] = event.target.value;
    }
    if (conditionField) {
      item.condition = item.condition || { type: "always" };
      if (conditionField === "value_bool") item.condition.value = event.target.value === "true";
      else if (["value", "period"].includes(conditionField)) item.condition[conditionField] = numberValue(event.target.value, item.condition[conditionField] || 0);
      else item.condition[conditionField] = event.target.value;
      item.condition = cleanCondition(item.condition);
      item.label = conditionLabel(item.condition);
    }
    if (actionField) {
      item.action = item.action || { type: "hold", step: 1, order_type: "market" };
      if (["step", "percent", "amount_points", "limit_price_points"].includes(actionField)) item.action[actionField] = numberValue(event.target.value, item.action[actionField] || 0);
      else item.action[actionField] = event.target.value;
      item.action = cleanAction(item.action);
      item.label = actionLabel(item.action);
    }
    refreshAfterFieldEdit(item, { rebuild: event.type === "change" || typeChangingField });
  }

  function handleDrop(event) {
    event.preventDefault();
    if (!dragNode) return;
    const canvas = document.querySelector("[data-graph-canvas]");
    const rect = canvas?.getBoundingClientRect();
    const item = workflow.nodes.find((nodeItem) => nodeItem.id === dragNode.id);
    if (item && rect) {
      item.x = Math.max(0, Math.round(event.clientX - rect.left - dragNode.dx));
      item.y = Math.max(0, Math.round(event.clientY - rect.top - dragNode.dy));
      render();
    }
    dragNode = null;
  }

  function autoLayoutGraph() {
    const columns = { start: 0, condition: 1, logic: 2, control: 3, action: 4 };
    const counters = {};
    workflow.nodes.forEach((item) => {
      const column = columns[item.type] ?? 1;
      const row = counters[column] || 0;
      counters[column] = row + 1;
      item.x = 40 + column * 250;
      item.y = 60 + row * 130;
    });
  }

  document.addEventListener("click", handleClick);
  document.addEventListener("input", handleInput);
  document.addEventListener("change", handleInput);
  document.addEventListener("dragstart", (event) => {
    const target = event.target.closest("[data-node-id]");
    if (!target) return;
    const rect = target.getBoundingClientRect();
    dragNode = { id: target.dataset.nodeId, dx: event.clientX - rect.left, dy: event.clientY - rect.top };
  });
  document.addEventListener("dragover", (event) => {
    if (event.target.closest("[data-graph-canvas]")) event.preventDefault();
  });
  document.addEventListener("drop", handleDrop);

  window.HackmeTradingWorkflowEditor = {
    getWorkflow: () => normalizeWorkflow(workflow),
    setWorkflow: (next) => {
      workflow = normalizeWorkflow(next);
      selectedNodeId = workflow.start_node_id;
      render();
    },
    runBacktestPreview: () => runWorkflowPreviewBacktest(),
    templateWorkflow,
    validateWorkflow,
  };

  render();
  initializeWorkflowPreview();
}());
