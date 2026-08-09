'use strict';

function tradingBotTriggerLabel(row) {
  if (row.bot_type === "dca") return `每 ${Number(row.interval_hours || 24)} 小時定投 ${formatTradingPointsValue(row.budget_points)} 點`;
  if (row.workflow) {
    const branches = Array.isArray(row.workflow.branches) ? row.workflow.branches : [];
    return branches.length ? `${branches.length} 個 workflow 分支` : "Workflow 尚未設定";
  }
  const price = formatTradingPointsValue(row.trigger_price_points || 0);
  if (row.trigger_type === "price_above") return `價格 >= ${price}`;
  if (row.trigger_type === "price_below") return `價格 <= ${price}`;
  return "每次掃描";
}

function tradingBotBudgetText(bot) {
  const budget = Number(bot?.budget_points || 0);
  const frozen = Number(bot?.open_order_frozen_points || 0);
  const remaining = bot?.budget_remaining_points == null ? null : Number(bot.budget_remaining_points || 0);
  if (bot?.bot_type === "dca") return `每次投入 ${formatTradingPointsValue(budget)} 點`;
  if (budget <= 0) return frozen > 0
    ? `可用上限不設限 · 已掛單凍結 ${formatTradingPointsValue(frozen)} 點`
    : "可用上限不設限";
  return `可用上限 ${formatTradingPointsValue(budget)} 點 · 剩餘可掛 ${formatTradingPointsValue(remaining ?? Math.max(0, budget - frozen))} 點`;
}

function switchTradingBotTab(tab) {
  tradingCurrentBotTab = tab || "mybots";
  document.querySelectorAll("[data-trading-bot-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tradingBotTab === tradingCurrentBotTab);
  });
  document.querySelectorAll(".trading-bot-tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `trading-bot-tab-${tradingCurrentBotTab}`);
  });
  if (tradingCurrentBotTab === "grid") {
    renderGridBotPreview({ quiet: true }).catch((err) => tradingSetMsg(tradingFriendlyErrorText(err?.message || "網格試算失敗"), false));
  } else if (tradingCurrentBotTab === "competition") {
    loadTradingBotCompetition().catch((err) => tradingSetMsg(tradingFriendlyErrorText(err?.message || "競賽排行讀取失敗"), false));
  }
}

function openTradingBotPanel(tab = "mybots") {
  setTradingActivePage("spot", { openBot: true });
  const card = $("trading-bot-card");
  if (card) card.open = true;
  switchTradingBotTab(tab || tradingCurrentBotTab || "mybots");
}

function tradingWorkflowTemplates() {
  if (!Array.isArray(tradingState.workflowTemplates) || !tradingState.workflowTemplates.length) return {};
  return tradingState.workflowTemplates.reduce((acc, item) => {
    if (item && item.id && item.workflow) {
      acc[item.id] = {
          label: item.label || item.id,
          description: item.description || "",
          explanation: item.explanation || {},
          scope: item.scope || "system",
          source_path: item.source_path || "",
          workflow: item.workflow,
      };
    }
    return acc;
  }, {});
}

function renderTradingWorkflowTemplateOptions() {
  const select = $("trading-workflow-template-select");
  if (!select) return;
  const previous = select.value;
  const templates = tradingWorkflowTemplates();
  const entries = Object.entries(templates);
  if (!entries.length) {
    select.innerHTML = `<option value="">沒有可用模板</option>`;
    return;
  }
  select.innerHTML = entries.map(([id, item]) => {
    const scopeLabel = item.scope === "custom" ? "自訂" : "系統";
    return `<option value="${sanitize(id)}">${sanitize(item.label || id)}（${scopeLabel}）</option>`;
  }).join("");
  if (previous && entries.some(([id]) => id === previous)) select.value = previous;
  renderTradingWorkflowTemplateExplanation();
}

function tradingWorkflowExplanationList(items) {
  if (!Array.isArray(items) || !items.length) return "";
  return `<ul>${items.map((item) => `<li>${sanitize(item)}</li>`).join("")}</ul>`;
}

function renderTradingWorkflowTemplateExplanation() {
  const box = $("trading-workflow-template-explanation");
  if (!box) return;
  const key = $("trading-workflow-template-select")?.value || "";
  const item = tradingWorkflowTemplates()[key];
  if (!item) {
    box.innerHTML = `<div class="muted">選擇模板後會顯示用途、條件、行為與風險提醒。</div>`;
    return;
  }
  const detail = item.explanation || {};
  const sections = [
    ["用途", detail.purpose || item.description],
    ["觸發條件", tradingWorkflowExplanationList(detail.entry_conditions)],
    ["執行行為", tradingWorkflowExplanationList(detail.actions)],
    ["風險提醒", tradingWorkflowExplanationList(detail.risk_notes)],
    ["適合情境", tradingWorkflowExplanationList(detail.best_for)],
    ["可調參數", tradingWorkflowExplanationList(detail.tuning)],
  ].filter(([, content]) => !!content);
  const benchmarkHtml = renderTradingWorkflowTemplateBenchmark(key);
  box.innerHTML = `
    <div class="drive-card-title">${sanitize(item.label || key)}</div>
    ${sections.map(([title, content]) => `
      <div class="workflow-template-section">
        <strong>${sanitize(title)}</strong>
        <div>${content.startsWith("<") ? content : sanitize(content)}</div>
      </div>
    `).join("")}
    ${benchmarkHtml}
    <div class="muted">來源：${sanitize(item.source_path || item.scope || "workflow")}</div>
  `;
  loadTradingWorkflowBenchmarksAsync();
}

let tradingWorkflowBenchmarkCache = null;
let tradingWorkflowBenchmarkLoading = false;

function renderTradingWorkflowTemplateBenchmark(templateId) {
  const data = tradingWorkflowBenchmarkCache;
  if (!data) {
    return `
      <div class="workflow-template-section">
        <strong>歷史回測表現（BTC/USDT 1h）</strong>
        <div class="muted">資料載入中...</div>
      </div>
    `;
  }
  if (!Array.isArray(data.windows) || !data.windows.length) {
    return `
      <div class="workflow-template-section">
        <strong>歷史回測表現（BTC/USDT 1h）</strong>
        <div class="muted">${sanitize(data.load_error || "目前沒有可用的 Workflow 歷史回測資料。")}</div>
      </div>
    `;
  }
  const rows = data.windows.map((w) => {
    const r = (w.rankings || []).find((row) => row.template === templateId);
    if (!r || r.error) return [w.label, null, null, null];
    const pnl = Number(r.pnl_percent || 0);
    return [w.label, pnl, Number(r.trade_count || 0), Number(r.max_drawdown_percent || 0)];
  });
  if (!rows.length) return "";
  const fmtPct = (v) => {
    if (v === null || v === undefined) return "-";
    const n = Number(v);
    const cls = n > 0 ? "color:#00d4aa" : n < 0 ? "color:#ff6b6b" : "";
    return `<span style="${cls}">${n >= 0 ? "+" : ""}${n.toFixed(2)}%</span>`;
  };
  const table = `
    <table style="width:100%;border-collapse:collapse;font-size:.82rem;margin-top:.3rem;">
      <thead><tr style="border-bottom:1px solid var(--muted, #444);">
        <th style="text-align:left;padding:.2rem .4rem;">時長</th>
        <th style="text-align:right;padding:.2rem .4rem;">PnL</th>
        <th style="text-align:right;padding:.2rem .4rem;">交易次數</th>
        <th style="text-align:right;padding:.2rem .4rem;">最大回撤</th>
      </tr></thead>
      <tbody>${rows.map(([label, pnl, trades, dd]) => `
        <tr>
          <td style="padding:.2rem .4rem;">${sanitize(label)}</td>
          <td style="text-align:right;padding:.2rem .4rem;">${pnl === null ? "-" : fmtPct(pnl)}</td>
          <td style="text-align:right;padding:.2rem .4rem;">${trades === null ? "-" : trades}</td>
          <td style="text-align:right;padding:.2rem .4rem;">${dd === null ? "-" : fmtPct(-Math.abs(dd))}</td>
        </tr>
      `).join("")}</tbody>
    </table>
  `;
  return `
    <div class="workflow-template-section">
      <strong>歷史回測表現（BTC/USDT ${sanitize(data.interval || "1h")}，初始資金 ${Number(data.initial_cash_points || 0).toLocaleString()} 點）</strong>
      ${table}
      <div class="muted" style="font-size:.72rem;margin-top:.3rem;">資料來源：${sanitize(data.data_source || "")}；資料區間 ${sanitize(String(data.first_candle_iso || "").slice(0, 10))} → ${sanitize(String(data.last_candle_iso || "").slice(0, 10))}；產生時間 ${sanitize(data.generated_at || "")}</div>
    </div>
  `;
}

async function loadTradingWorkflowBenchmarksAsync() {
  if (tradingWorkflowBenchmarkCache || tradingWorkflowBenchmarkLoading) return;
  tradingWorkflowBenchmarkLoading = true;
  try {
    tradingWorkflowBenchmarkCache = await fetchTradingJson("/trading/workflow-template-benchmarks", { forceCsrf: false });
  } catch (_err) {
    tradingWorkflowBenchmarkCache = { windows: [], load_error: "Workflow 歷史回測報告讀取失敗。" };
  } finally {
    tradingWorkflowBenchmarkLoading = false;
    renderTradingWorkflowTemplateExplanation();
  }
}

async function loadTradingWorkflowTemplates({ force = false } = {}) {
  if (!force && Array.isArray(tradingState.workflowTemplates) && tradingState.workflowTemplates.length) {
    renderTradingWorkflowTemplateOptions();
    return;
  }
  try {
    const json = await fetchTradingJson("/trading/workflow-templates");
    tradingState.workflowTemplates = Array.isArray(json.templates) ? json.templates : [];
    renderTradingWorkflowTemplateOptions();
    if (Array.isArray(json.errors) && json.errors.length) {
      tradingSetMsg(`部分 Workflow 模板載入失敗：${json.errors[0].error || "未知錯誤"}`, false);
    }
  } catch (err) {
    renderTradingWorkflowTemplateOptions();
    tradingSetMsg(err.message || "Workflow 模板讀取失敗，請確認 workflows/trading_bot 內有模板檔", false);
  }
}

async function saveTradingWorkflowCustomTemplate() {
  const textarea = $("trading-auto-workflow-json");
  if (!textarea) {
    tradingSetMsg("找不到 Workflow JSON 編輯欄位", false);
    return;
  }
  let workflow;
  try {
    workflow = JSON.parse(textarea.value || tradingWorkflowText());
  } catch (err) {
    tradingSetMsg("Workflow JSON 格式錯誤，無法儲存自訂模板", false);
    return;
  }
  const label = ($("trading-workflow-custom-name")?.value || workflow.name || "自訂 Workflow").trim();
  try {
    const json = await fetchTradingJson("/trading/workflow-templates/custom", {
      method: "POST",
      body: JSON.stringify({
        id: label,
        label,
        description: workflow.description || "",
        workflow,
      }),
    });
    if (json.template) {
      tradingState.workflowTemplates = [
        ...(tradingState.workflowTemplates || []).filter((item) => item.id !== json.template.id),
        json.template,
      ];
      renderTradingWorkflowTemplateOptions();
      const select = $("trading-workflow-template-select");
      if (select) select.value = json.template.id;
    } else {
      await loadTradingWorkflowTemplates({ force: true });
    }
    tradingSetMsg(json.msg || `Workflow 自訂模板已儲存到 ${json.custom_workflow_root || "runtime/workflows/custom"}`);
  } catch (err) {
    tradingSetMsg(err.message || "Workflow 自訂模板儲存失敗", false);
  }
}

function tradingWorkflowTemplate(name = "dipbuy_rsi35_70_size99_late_tp15_nopyr_codex") {
  const templates = tradingWorkflowTemplates();
  const item = templates[name] || templates.dipbuy_rsi35_70_size99_late_tp15_nopyr_codex || Object.values(templates)[0];
  if (!item || !item.workflow) {
    return {
      version: 2,
      strategy_kind: "workflow_graph",
      name: "空白 Workflow",
      start_node_id: "start",
      nodes: [{ id: "start", type: "start", label: "開始", x: 80, y: 100 }],
      edges: [],
    };
  }
  return JSON.parse(JSON.stringify(item.workflow));
}

function applyTradingWorkflowTemplate() {
  const key = $("trading-workflow-template-select")?.value || "dipbuy_rsi35_70_size99_late_tp15_nopyr_codex";
  const templates = tradingWorkflowTemplates();
  const item = templates[key] || templates.dipbuy_rsi35_70_size99_late_tp15_nopyr_codex || Object.values(templates)[0];
  if (!item || !item.workflow) {
    tradingSetMsg("沒有可用 Workflow 模板，請確認 workflows/trading_bot 內有模板檔", false);
    return;
  }
  const textarea = $("trading-auto-workflow-json");
  if (!textarea) {
    tradingSetMsg("找不到 Workflow JSON 編輯欄位", false);
    return;
  }
  textarea.value = JSON.stringify(item.workflow, null, 2);
  localStorage.setItem(tradingUserStorageKey(TRADING_WORKFLOW_STORAGE_KEY), textarea.value);
  renderTradingWorkflowTemplateExplanation();
  tradingSetMsg(`已套用基礎模板：${item.label}。請依市場價格調整門檻後再儲存機器人。`);
}

function tradingWorkflowText() {
  const raw = $("trading-auto-workflow-json")?.value || "";
  if (raw.trim()) return raw.trim();
  const saved = localStorage.getItem(tradingUserStorageKey(TRADING_WORKFLOW_STORAGE_KEY));
  return saved || JSON.stringify(tradingWorkflowTemplate(), null, 2);
}

function loadTradingWorkflowFromEditor() {
  const saved = localStorage.getItem(tradingUserStorageKey(TRADING_WORKFLOW_STORAGE_KEY));
  const textarea = $("trading-auto-workflow-json");
  if (!textarea) return;
  textarea.value = saved || JSON.stringify(tradingWorkflowTemplate(), null, 2);
  tradingSetMsg(saved ? "已載入 Workflow 編輯器結果" : "尚無編輯器結果，已載入預設範例");
}

function parseTradingWorkflowInput() {
  try {
    return JSON.parse(tradingWorkflowText());
  } catch (err) {
    throw new Error("Workflow JSON 格式錯誤，請回編輯器修正後再載入");
  }
}

function formatTradingCountdown(ms) {
  const seconds = Math.max(0, Math.ceil(Number(ms || 0) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours > 0) return `${hours} 小時 ${String(minutes).padStart(2, "0")} 分 ${String(rest).padStart(2, "0")} 秒`;
  return `${minutes} 分 ${String(rest).padStart(2, "0")} 秒`;
}

function parseTradingDateMs(value) {
  if (!value) return NaN;
  const raw = String(value);
  let parsed = Date.parse(raw);
  if (!Number.isNaN(parsed)) return parsed;
  parsed = Date.parse(raw.replace(" ", "T"));
  return parsed;
}

function tradingBotMaxRunsValue(row) {
  return Number(row?.max_runs ?? 1);
}

function tradingBotMaxRunsLabel(row) {
  return tradingBotMaxRunsValue(row) === -1 ? "不限制" : String(tradingBotMaxRunsValue(row));
}

function tradingBotRunLimitReached(row) {
  const maxRuns = tradingBotMaxRunsValue(row);
  if (maxRuns === -1) return false;
  return Number(row?.run_count || 0) >= maxRuns;
}

function tradingBotNextRunInfo(row) {
  if (!row || !row.enabled) return { text: "下次執行：已停用", ready: false };
  if (tradingBotRunLimitReached(row)) return { text: "下次執行：已達最大次數", ready: false };
  let nextMs = parseTradingDateMs(row.next_run_at);
  if (Number.isNaN(nextMs)) {
    const lastMs = parseTradingDateMs(row.last_run_at);
    nextMs = Number.isNaN(lastMs) ? Date.now() : lastMs + (Number(row.cooldown_seconds || 0) * 1000);
  }
  const remaining = nextMs - Date.now();
  if (remaining <= 0) {
    const neverRan = !row.last_run_at && Number(row.run_count || 0) === 0;
    return { text: neverRan ? "下次執行：等待首次執行…" : "下次執行：可立即執行", ready: true };
  }
  const when = new Date(nextMs).toLocaleString("zh-TW", { hour12: false });
  const intervalHours = Number(row.interval_hours || 0);
  const intervalText = intervalHours > 0 ? `　每 ${intervalHours} 小時定投` : "";
  return { text: `下次執行：${formatTradingCountdown(remaining)} 後（${when}）${intervalText}`, ready: false };
}

function tradingBotNextRunText(row) {
  return tradingBotNextRunInfo(row).text;
}

function updateTradingBotCountdowns() {
  document.querySelectorAll("[data-trading-bot-next-run]").forEach((node) => {
    const uuid = node.dataset.tradingBotNextRun || "";
    const row = (tradingState.bots || []).find((item) => item.bot_uuid === uuid);
    node.textContent = tradingBotNextRunText(row);
  });
}

function restartTradingBotCountdown() {
  if (tradingBotCountdownTimer) window.clearInterval(tradingBotCountdownTimer);
  tradingBotCountdownTimer = null;
  updateTradingBotCountdowns();
  if ((tradingState.bots || []).some((row) => row.enabled && !tradingBotRunLimitReached(row))) {
    tradingBotCountdownTimer = window.setInterval(updateTradingBotCountdowns, 1000);
  }
}

function renderTradingBots(rows = [], runs = []) {
  const dcaList = $("trading-dca-bot-list");
  const strategyList = $("trading-strategy-bot-list");
  const runList = $("trading-bot-run-list");
  const renderRows = (items, emptyText) => items.length ? items.map((row) => {
    const checks = Array.isArray(row.condition_checks) ? row.condition_checks : [];
    const condHtml = checks.length
      ? `<div class="drive-card-sub trading-bot-conditions">${checks.map((c) => `<span class="${c.met ? "trading-condition-met" : "trading-condition-unmet"}">${sanitize(c.label)}</span>`).join("")}</div>`
      : "";
    const botRuns = runs.filter((r) => Number(r.bot_id) === Number(row.id) && r.status === "triggered").slice(0, 5);
    const tradeHistoryHtml = botRuns.length
      ? `<div class="drive-card-sub" style="margin-top:.25rem;">
          <span style="font-weight:600;">近期交易：</span>${botRuns.map((r) => {
            const price = Number(r.observed_price_points || 0);
            const sideLabel = row.side === "sell" ? "賣" : "買";
            const sideColor = row.side === "sell" ? "#ef4444" : "#22c55e";
            const timeStr = r.created_at ? new Date(r.created_at).toLocaleString() : "-";
            return `<span style="margin-left:.4rem;color:${sideColor};">${sideLabel} ${formatTradingPointsValue(price)} 點（${timeStr}）</span>`;
          }).join("")}
        </div>`
      : "";
    return `
        <div class="drive-file-row">
          <div>
            <strong>${sanitize(row.name || "未命名機器人")} · ${sanitize(tradingDisplaySymbol(row.market_symbol || ""))}</strong>
            <div class="drive-card-sub">
	              ${sanitize(row.bot_type_label || (row.bot_type === "dca" ? "定投機器人" : "Workflow 機器人"))} · ${sanitize(tradingBotTriggerLabel(row))} 時 ${row.side === "sell" ? "賣出" : "買入"} ${row.bot_type === "dca" ? "系統換算數量" : sanitize(row.quantity_text || "workflow 決定")}，
	              ${row.order_type === "limit" ? `限價 ${formatTradingPointsValue(row.limit_price_points)}` : "市價單"}
	              ${tradingParameterShareBadge(row)}
	            </div>
            <div class="drive-card-sub">
              狀態 ${row.enabled ? "啟用" : "停用"} · 已觸發 ${Number(row.run_count || 0)} / ${tradingBotMaxRunsLabel(row)} · 冷卻 ${Number(row.cooldown_seconds || 0)} 秒
            </div>
            <div class="drive-card-sub">${sanitize(tradingBotBudgetText(row))}</div>
            <div class="drive-card-sub">${sanitize(tradingRiskTargetText(row.stop_loss_percent, row.take_profit_percent))}</div>
            ${condHtml}
            ${tradeHistoryHtml}
            <div class="drive-card-sub" data-trading-bot-next-run="${sanitize(row.bot_uuid || "")}">${sanitize(tradingBotNextRunText(row))}</div>
            ${row.last_error ? `<div class="drive-card-sub negative">上次錯誤：${sanitize(row.last_error)}</div>` : ""}
          </div>
          <div class="drive-file-actions">
            ${tradingBotRunLimitReached(row) ? `<button class="btn" type="button" data-trading-bot-increase-runs="${sanitize(row.bot_uuid || "")}">增加次數</button>` : ""}
	            ${row.bot_type !== "dca" ? `<button class="btn" type="button" data-trading-bot-budget="${sanitize(row.bot_uuid || "")}">調整可用</button>` : ""}
	            <button class="btn" type="button" data-trading-bot-toggle="${sanitize(row.bot_uuid || "")}" data-trading-bot-enabled="${row.enabled ? "0" : "1"}">${row.enabled ? "暫停" : "啟用"}</button>
	            <button class="btn" type="button" data-trading-bot-share="${sanitize(row.bot_uuid || "")}" data-trading-bot-share-enabled="${row.share_parameters ? "0" : "1"}">${row.share_parameters ? "停止分享參數" : "分享參數"}</button>
	            <button class="btn" type="button" data-trading-bot-backtest="${sanitize(row.bot_uuid || "")}">回測</button>
            <button class="btn btn-danger" type="button" data-trading-bot-delete="${sanitize(row.bot_uuid || "")}">刪除</button>
          </div>
        </div>
      `;
  }).join("") : `<div class="drive-empty">${sanitize(emptyText)}</div>`;
  if (dcaList) dcaList.innerHTML = renderRows(rows.filter((row) => row.bot_type === "dca"), "尚無定投機器人");
  if (strategyList) strategyList.innerHTML = renderRows(rows.filter((row) => row.bot_type !== "dca"), "尚無自動化 Workflow");
  [dcaList, strategyList].forEach((list) => {
    if (!list) return;
    list.querySelectorAll("[data-trading-bot-delete]").forEach((btn) => {
      bindTradingActionButton(btn, () => deleteTradingBot(btn.dataset.tradingBotDelete || ""), "準備刪除交易機器人...", "交易機器人刪除失敗");
    });
    list.querySelectorAll("[data-trading-bot-toggle]").forEach((btn) => {
      bindTradingActionButton(
        btn,
        () => toggleTradingBot(btn.dataset.tradingBotToggle || "", btn.dataset.tradingBotEnabled === "1"),
        btn.dataset.tradingBotEnabled === "1" ? "準備啟用交易機器人..." : "準備暫停交易機器人...",
        "交易機器人狀態更新失敗"
      );
    });
	    list.querySelectorAll("[data-trading-bot-backtest]").forEach((btn) => {
	      bindTradingActionButton(btn, () => prepareTradingBacktestFromBot(btn.dataset.tradingBotBacktest || ""), "正在帶入回測設定...", "回測設定帶入失敗");
	    });
	    list.querySelectorAll("[data-trading-bot-share]").forEach((btn) => {
	      bindTradingActionButton(
	        btn,
	        () => setTradingBotParameterShare(btn.dataset.tradingBotShare || "", btn.dataset.tradingBotShareEnabled === "1"),
	        btn.dataset.tradingBotShareEnabled === "1" ? "正在開啟參數分享..." : "正在停止參數分享...",
	        "參數分享狀態更新失敗"
	      );
	    });
    list.querySelectorAll("[data-trading-bot-increase-runs]").forEach((btn) => {
      bindTradingActionButton(btn, () => increaseTradingBotMaxRuns(btn.dataset.tradingBotIncreaseRuns || ""), "正在增加可執行次數...", "增加機器人次數失敗");
    });
    list.querySelectorAll("[data-trading-bot-budget]").forEach((btn) => {
      bindTradingActionButton(btn, () => adjustTradingBotBudget(btn.dataset.tradingBotBudget || ""), "正在調整機器人可用上限...", "調整機器人可用上限失敗");
    });
  });
  refreshBacktestBotSelect();
  if (runList) {
    if (!runs.length) {
      runList.innerHTML = `<div class="drive-empty">尚無執行紀錄</div>`;
    } else {
      runList.innerHTML = runs.slice(0, 20).map((row) => `
        <div class="drive-file-row">
          <div>
            <strong>${sanitize(row.status || "-")} · ${sanitize(tradingDisplaySymbol(row.market_symbol || ""))}</strong>
            <div class="drive-card-sub">觀測價 ${formatTradingPointsValue(row.observed_price_points)} · 條件 ${sanitize(row.trigger_type || "-")} ${row.trigger_price_points ? formatTradingPointsValue(row.trigger_price_points) : ""}</div>
            <div class="drive-card-sub">${sanitize(row.created_at || "")}${row.order_uuid ? ` · 訂單 ${sanitize(row.order_uuid)}` : ""}</div>
            ${row.error ? `<div class="drive-card-sub negative">${sanitize(row.error)}</div>` : ""}
          </div>
        </div>
      `).join("");
    }
  }
  restartTradingBotCountdown();
  renderMyBotsList();
}

function tradingParameterShareBadge(bot) {
  return bot?.share_parameters
    ? ` · <span class="trading-share-badge">參數已分享</span>`
    : ` · <span class="trading-share-badge muted">參數未分享</span>`;
}

function tradingSharedParametersHtml(params) {
  if (!params || typeof params !== "object") return `<div class="drive-card-sub">此用戶未分享參數</div>`;
  const rows = Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => {
      const text = typeof value === "object" ? JSON.stringify(value) : String(value);
      return `<span><b>${sanitize(key)}</b> ${sanitize(text)}</span>`;
    });
  return rows.length
    ? `<div class="trading-shared-params">${rows.join("")}</div>`
    : `<div class="drive-card-sub">此用戶未分享參數</div>`;
}

function renderTradingBotCompetition(competition = null) {
  const container = $("trading-bot-competition-list");
  const meta = $("trading-bot-competition-meta");
  const rewardEl = $("trading-bot-competition-rewards");
  const weekInput = $("trading-bot-competition-week");
  const awardBtn = $("trading-bot-competition-award-btn");
  if (!container) return;
  const data = competition || tradingState.botCompetition || {};
  const week = data.week || "";
  if (weekInput && !weekInput.value) weekInput.value = week;
  const settings = data.settings || {};
  const enabled = settings.enabled !== false;
  const rewardPoints = Number(settings.weekly_reward_points || 0);
  if (meta) {
    meta.textContent = `${week || "-"} · ${enabled ? "週賽啟用" : "週賽停用"} · 各類第一名 ${rewardPoints.toLocaleString()} 點`;
  }
  const rewards = Array.isArray(data.rewards) ? data.rewards : [];
  const autoAwarded = Array.isArray(data.auto_awarded) ? data.auto_awarded : [];
  if (rewardEl) {
    const rewardText = rewards.length
      ? rewards.map((row) => `${row.category}：${row.username} +${Number(row.reward_points || 0).toLocaleString()} 點`).join(" · ")
      : "本週尚未發放競賽獎勵";
    const autoText = autoAwarded.length ? ` · 已自動補發 ${autoAwarded.length} 筆上週獎勵` : "";
    rewardEl.textContent = `${rewardText}${autoText}`;
  }
  if (awardBtn) {
    awardBtn.style.display = currentUser === "root" ? "" : "none";
    awardBtn.dataset.awardWeek = data.previous_week || "";
  }
  const categories = Array.isArray(data.categories) ? data.categories : [];
  if (!categories.length) {
    container.innerHTML = `<div class="drive-empty">尚無競賽資料</div>`;
    return;
  }
  container.innerHTML = categories.map((category) => {
    const rows = Array.isArray(category.leaderboard) ? category.leaderboard : [];
    const body = rows.length ? rows.slice(0, 10).map((row) => {
      const rank = row.rank ? `#${row.rank}` : "-";
      const perf = Number(row.performance_percent || 0);
      const pnl = Number(row.pnl_points || 0);
      const perfClass = perf >= 0 ? "positive" : "negative";
      const pnlClass = pnl >= 0 ? "positive" : "negative";
      return `<div class="trading-bot-competition-row">
        <div class="trading-bot-competition-rank">${sanitize(rank)}</div>
        <div>
          <strong>${sanitize(row.bot_name || "未命名")} · ${sanitize(row.username || "-")}</strong>
          <div class="drive-card-sub">${sanitize(row.display_symbol || row.market_symbol || "-")} · 成交 ${Number(row.fill_count || 0)} 筆 · 本金基準 ${formatTradingPointsValue(row.principal_points || 0)} 點${tradingParameterShareBadge(row)}</div>
          <details class="economy-collapse" style="margin-top:.25rem;">
            <summary>分享參數</summary>
            ${tradingSharedParametersHtml(row.shared_parameters)}
          </details>
        </div>
        <div class="trading-bot-competition-score">
          <span class="${perfClass}">${perf >= 0 ? "+" : ""}${perf.toFixed(4)}%</span>
          <small class="${pnlClass}">${pnl >= 0 ? "+" : ""}${formatTradingPointsValue(pnl)} 點</small>
        </div>
      </div>`;
    }).join("") : `<div class="drive-empty">本週此類型尚無有效成交</div>`;
    return `<section class="trading-bot-competition-card">
      <div class="drive-card-heading compact-heading">
        <div>
          <div class="drive-card-title">${sanitize(category.label || category.category || "-")}</div>
          <div class="drive-card-sub">第一名獎勵 ${Number(category.reward_points || rewardPoints || 0).toLocaleString()} 點</div>
        </div>
      </div>
      ${body}
    </section>`;
  }).join("");
}

function tradingWorkflowBotDetail(bot) {
  const parts = [];
  const wf = bot.workflow;
  if (wf && typeof wf === "object") {
    const branches = Array.isArray(wf.branches) ? wf.branches : [];
    const nodes = Array.isArray(wf.nodes) ? wf.nodes : [];
    if (branches.length) parts.push(`Workflow ${branches.length} 個分支`);
    else if (nodes.length) parts.push(`Workflow 圖（${nodes.length} 個節點）`);
    else parts.push("Workflow（無條件）");
  } else {
    const price = formatTradingPointsValue(bot.trigger_price_points || 0);
    if (bot.trigger_type === "price_above") parts.push(`價格 ≥ ${price} 點時觸發`);
    else if (bot.trigger_type === "price_below") parts.push(`價格 ≤ ${price} 點時觸發`);
    else parts.push("每次掃描觸發");
  }
  const action = bot.side === "sell" ? "賣出" : "買入";
  const order = bot.order_type === "limit" ? `限價 ${formatTradingPointsValue(bot.limit_price_points)} 點` : "市價單";
  parts.push(`${action} · ${order}`);
  parts.push(tradingBotBudgetText(bot));
  if (bot.quantity_text) parts.push(`數量：${bot.quantity_text}`);
  if (Number(bot.cooldown_seconds || 0) > 0) parts.push(`冷卻 ${bot.cooldown_seconds}s`);
  return parts.join(" · ");
}

function tradingBotRecentFills(bot) {
  if (!bot) return [];
  const orderUuids = new Set();
  const botRuns = Array.isArray(tradingState.botRuns) ? tradingState.botRuns : [];
  botRuns.forEach((run) => {
    const sameBot = (bot.id && Number(run.bot_id || 0) === Number(bot.id)) || (bot.bot_uuid && run.bot_uuid === bot.bot_uuid);
    if (sameBot && run.order_uuid) orderUuids.add(run.order_uuid);
  });
  const gridOrders = Array.isArray(bot.orders) ? bot.orders : [];
  gridOrders.forEach((order) => {
    if (order?.trading_order_uuid) orderUuids.add(order.trading_order_uuid);
    if (order?.order_uuid) orderUuids.add(order.order_uuid);
  });
  return (Array.isArray(tradingState.fills) ? tradingState.fills : [])
    .filter((fill) => {
      if (fill?.order_uuid && orderUuids.has(fill.order_uuid)) return true;
      return Boolean(fill?.bot_name && bot?.name && fill.bot_name === bot.name);
    })
    .slice(0, 30);
}

function renderTradingBotFillDetails(fills = []) {
  if (!fills.length) return `<div class="drive-card-sub">尚無交易明細</div>`;
  return fills.map((fill) => {
    const side = fill.side === "sell" ? "賣出" : "買入";
    const sideClass = fill.side === "sell" ? "negative" : "positive";
    const qty = sanitize(fill.quantity || "0");
    const market = sanitize(tradingDisplaySymbol(fill.market_symbol || ""));
    const time = fill.created_at ? sanitize(String(fill.created_at).slice(0, 16).replace("T", " ")) : "-";
    const feeText = Number(fill.fee_points || 0) > 0 ? ` · 手續費 ${formatTradingPointsValue(fill.fee_points)}` : "";
    const pnl = Number(fill.realized_pnl_points || 0);
    const pnlText = Number.isFinite(pnl) && pnl !== 0
      ? ` · 已實現 <span class="${pnl >= 0 ? "positive" : "negative"}">${pnl >= 0 ? "+" : ""}${formatTradingPointsValue(pnl)}</span>`
      : "";
    return `<div class="drive-card-sub" style="white-space:normal;">
      <span class="${sideClass}" style="font-weight:600;">${side}</span>
      ${market} ${qty} @ ${formatTradingPointsValue(fill.execution_price_points || fill.price_points || 0)}
      ${feeText}${pnlText}
      <span style="color:var(--muted);"> · ${time}</span>
    </div>`;
  }).join("");
}

function renderMyBotsList() {
  const container = $("trading-my-bots-list");
  if (!container) return;
  const dcaBots = (tradingState.bots || []).filter((b) => b.bot_type === "dca");
  const workflowBots = (tradingState.bots || []).filter((b) => b.bot_type !== "dca");
  const gridBots = tradingGridBots || [];
  if (!dcaBots.length && !workflowBots.length && !gridBots.length) {
    container.innerHTML = `<div class="drive-empty">尚無機器人，在各設定頁新增</div>`;
    return;
  }
  const priceMap = {};
  for (const m of (tradingState.markets || [])) {
    priceMap[m.symbol] = tradingMarketPricePoints(m, "reference");
  }

  const rows = [];

  for (const bot of dcaBots) {
    const symbol = sanitize(tradingDisplaySymbol(bot.market_symbol || ""));
    const condHtml = (Array.isArray(bot.condition_checks) ? bot.condition_checks : []).map(
      (c) => `<span class="${c.met ? "trading-condition-met" : "trading-condition-unmet"}">${sanitize(c.label)}</span>`
    ).join("") || "";
    const fills = tradingBotRecentFills(bot);
    rows.push(`<div class="drive-file-row" data-mybot-uuid="${sanitize(bot.bot_uuid || "")}" data-mybot-type="dca">
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;">
          <span class="grid-status-badge" style="background:rgba(79,195,247,.18);color:#4fc3f7;">定投</span>
          <strong>${sanitize(bot.name || "定投機器人")} · ${symbol}</strong>
          <span class="grid-status-badge ${bot.enabled ? "grid-status-running" : "grid-status-stopped"}">${bot.enabled ? "運行中" : "已暫停"}</span>
        </div>
	        <div class="drive-card-sub">已觸發 ${Number(bot.run_count || 0)} / ${tradingBotMaxRunsLabel(bot)} · 冷卻 ${Number(bot.cooldown_seconds || 0)} 秒${bot.last_error ? ` · <span class="negative">上次錯誤：${sanitize(bot.last_error)}</span>` : ""}</div>
	        <div class="drive-card-sub">${sanitize(tradingRiskTargetText(bot.stop_loss_percent, bot.take_profit_percent))}${tradingParameterShareBadge(bot)}</div>
        ${condHtml ? `<div class="drive-card-sub trading-bot-conditions">${condHtml}</div>` : ""}
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>設定摘要</summary>
          <div class="drive-card-sub">${sanitize(bot.bot_type_label || "定投機器人")} · ${sanitize(bot.order_type === "limit" ? `限價 ${bot.limit_price_points}` : "市價單")} · 每次 ${sanitize(bot.quantity_text || "?")}</div>
        </details>
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>交易明細（${fills.length} 筆）</summary>
          ${renderTradingBotFillDetails(fills)}
        </details>
      </div>
      <div class="drive-file-actions" style="flex-shrink:0;">
        <button class="btn btn-sm" type="button" data-chart-type="dca" data-chart-bot-uuid="${sanitize(bot.bot_uuid || "")}" data-chart-symbol="${sanitize(bot.market_symbol || "")}">圖表</button>
	        <button class="btn btn-sm" type="button" data-trading-bot-backtest="${sanitize(bot.bot_uuid || "")}">回測</button>
	        <button class="btn btn-sm" type="button" data-trading-bot-share="${sanitize(bot.bot_uuid || "")}" data-trading-bot-share-enabled="${bot.share_parameters ? "0" : "1"}">${bot.share_parameters ? "停止分享" : "分享參數"}</button>
	        ${tradingBotRunLimitReached(bot) ? `<button class="btn btn-sm" type="button" data-trading-bot-increase-runs="${sanitize(bot.bot_uuid || "")}">增加次數</button>` : ""}
        <button class="btn btn-sm" type="button" data-trading-bot-toggle="${sanitize(bot.bot_uuid || "")}" data-trading-bot-enabled="${bot.enabled ? "0" : "1"}">${bot.enabled ? "暫停" : "啟用"}</button>
        <button class="btn btn-sm btn-danger" type="button" data-trading-bot-delete="${sanitize(bot.bot_uuid || "")}">刪除</button>
      </div>
    </div>`);
  }

  for (const bot of workflowBots) {
    const symbol = sanitize(tradingDisplaySymbol(bot.market_symbol || ""));
    const condHtml = (Array.isArray(bot.condition_checks) ? bot.condition_checks : []).map(
      (c) => `<span class="${c.met ? "trading-condition-met" : "trading-condition-unmet"}">${sanitize(c.label)}</span>`
    ).join("") || "";
    const fills = tradingBotRecentFills(bot);
    rows.push(`<div class="drive-file-row" data-mybot-uuid="${sanitize(bot.bot_uuid || "")}" data-mybot-type="workflow">
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;">
          <span class="grid-status-badge" style="background:rgba(167,139,250,.18);color:#a78bfa;">Workflow</span>
          <strong>${sanitize(bot.name || "Workflow 機器人")} · ${symbol}</strong>
          <span class="grid-status-badge ${bot.enabled ? "grid-status-running" : "grid-status-stopped"}">${bot.enabled ? "運行中" : "已暫停"}</span>
        </div>
	        <div class="drive-card-sub">已觸發 ${Number(bot.run_count || 0)} / ${tradingBotMaxRunsLabel(bot)} · ${sanitize(bot.side === "sell" ? "賣出" : "買入")} · ${sanitize(bot.order_type === "limit" ? `限價 ${bot.limit_price_points}` : "市價單")}${tradingParameterShareBadge(bot)}${bot.last_error ? ` · <span class="negative">上次錯誤：${sanitize(bot.last_error)}</span>` : ""}</div>
        <div class="drive-card-sub">${sanitize(tradingBotBudgetText(bot))}</div>
        ${condHtml ? `<div class="drive-card-sub trading-bot-conditions">${condHtml}</div>` : ""}
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>設定摘要</summary>
          <div class="drive-card-sub">${sanitize(tradingWorkflowBotDetail(bot))}</div>
        </details>
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>交易明細（${fills.length} 筆）</summary>
          ${renderTradingBotFillDetails(fills)}
        </details>
      </div>
      <div class="drive-file-actions" style="flex-shrink:0;">
	        <button class="btn btn-sm" type="button" data-chart-type="workflow" data-chart-bot-uuid="${sanitize(bot.bot_uuid || "")}" data-chart-symbol="${sanitize(bot.market_symbol || "")}">圖表</button>
	        <button class="btn btn-sm" type="button" data-trading-bot-backtest="${sanitize(bot.bot_uuid || "")}">回測</button>
	        <button class="btn btn-sm" type="button" data-trading-bot-share="${sanitize(bot.bot_uuid || "")}" data-trading-bot-share-enabled="${bot.share_parameters ? "0" : "1"}">${bot.share_parameters ? "停止分享" : "分享參數"}</button>
	        <button class="btn btn-sm" type="button" data-trading-bot-budget="${sanitize(bot.bot_uuid || "")}">調整可用</button>
	        ${tradingBotRunLimitReached(bot) ? `<button class="btn btn-sm" type="button" data-trading-bot-increase-runs="${sanitize(bot.bot_uuid || "")}">增加次數</button>` : ""}
        <button class="btn btn-sm" type="button" data-trading-bot-toggle="${sanitize(bot.bot_uuid || "")}" data-trading-bot-enabled="${bot.enabled ? "0" : "1"}">${bot.enabled ? "暫停" : "啟用"}</button>
        <button class="btn btn-sm btn-danger" type="button" data-trading-bot-delete="${sanitize(bot.bot_uuid || "")}">刪除</button>
      </div>
    </div>`);
  }

  for (const bot of gridBots) {
    const cp = priceMap[bot.market_symbol] || bot.initial_price_points || 0;
    const symbol = sanitize(tradingDisplaySymbol(bot.market_symbol || ""));
    const profit = Number(bot.total_profit_points || 0);
    const profitClass = profit >= 0 ? "positive" : "negative";
    const fills = tradingBotRecentFills(bot);
    rows.push(`<div class="drive-file-row" data-mybot-uuid="${sanitize(bot.bot_uuid || "")}" data-mybot-type="grid">
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;">
          <span class="grid-status-badge" style="background:rgba(34,197,94,.18);color:#22c55e;">網格</span>
          <strong>${sanitize(bot.name || "網格機器人")} · ${symbol}</strong>
          <span class="grid-status-badge ${bot.enabled ? "grid-status-running" : "grid-status-stopped"}">${bot.enabled ? "運行中" : "已暫停"}</span>
        </div>
        <div class="drive-card-sub">
          區間 ${formatTradingPointsValue(bot.lower_price_points)} ～ ${formatTradingPointsValue(bot.upper_price_points)} · ${Number(bot.grid_count)} 格 · 現價 ${formatTradingPointsValue(cp)}
        </div>
        <div class="drive-card-sub">
	          成交 ${Number(bot.total_trades || 0)} 次 · 盈虧 <span class="${profitClass}">${profit >= 0 ? "+" : ""}${formatTradingPointsValue(profit)}</span> 點
	          ${tradingParameterShareBadge(bot)}
	        </div>
        <div class="drive-card-sub">${sanitize(tradingRiskTargetText(bot.stop_loss_percent, bot.take_profit_percent))}</div>
        ${bot.last_error ? `<div class="drive-card-sub negative">錯誤：${sanitize(bot.last_error)}</div>` : ""}
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>設定摘要</summary>
          <div class="drive-card-sub">${sanitize(symbol)} · 區間 ${formatTradingPointsValue(bot.lower_price_points)} ～ ${formatTradingPointsValue(bot.upper_price_points)} · ${Number(bot.grid_count)} 格 · 每格 ${formatTradingPointsValue(bot.order_amount_points)} 點</div>
        </details>
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>掛單明細（${(Array.isArray(bot.orders) ? bot.orders : []).filter((o) => o.status === "open").length} 筆掛單）</summary>
          ${renderGridBotVisual(bot, cp)}
        </details>
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>交易明細（${fills.length} 筆）</summary>
          ${renderTradingBotFillDetails(fills)}
        </details>
      </div>
      <div class="drive-file-actions" style="flex-shrink:0;">
	        <button class="btn btn-sm" type="button" data-chart-type="grid" data-chart-bot-uuid="${sanitize(bot.bot_uuid || "")}" data-chart-symbol="${sanitize(bot.market_symbol || "")}">圖表</button>
	        <button class="btn btn-sm" type="button" data-grid-backtest="${sanitize(bot.bot_uuid || "")}">回測</button>
	        <button class="btn btn-sm" type="button" data-grid-share="${sanitize(bot.bot_uuid || "")}" data-grid-share-enabled="${bot.share_parameters ? "0" : "1"}">${bot.share_parameters ? "停止分享" : "分享參數"}</button>
	        <button class="btn btn-sm" type="button" data-grid-toggle="${sanitize(bot.bot_uuid || "")}" data-grid-enabled="${bot.enabled ? "0" : "1"}">${bot.enabled ? "暫停" : "啟用"}</button>
        <button class="btn btn-sm btn-danger" type="button" data-grid-delete="${sanitize(bot.bot_uuid || "")}">立即平倉刪除</button>
      </div>
    </div>`);
  }

  container.innerHTML = rows.join("");

  container.querySelectorAll("[data-chart-type]").forEach((btn) => {
    btn.addEventListener("click", () => {
      setBotChartOverlay(btn.dataset.chartType, btn.dataset.chartBotUuid || "", btn.dataset.chartSymbol || "");
    });
  });
  container.querySelectorAll("[data-trading-bot-delete]").forEach((btn) => {
    bindTradingActionButton(btn, () => deleteTradingBot(btn.dataset.tradingBotDelete || ""), "準備刪除交易機器人...", "交易機器人刪除失敗");
  });
  container.querySelectorAll("[data-trading-bot-toggle]").forEach((btn) => {
    bindTradingActionButton(
      btn,
      () => toggleTradingBot(btn.dataset.tradingBotToggle || "", btn.dataset.tradingBotEnabled === "1"),
      btn.dataset.tradingBotEnabled === "1" ? "準備啟用..." : "準備暫停...",
      "機器人狀態更新失敗"
    );
  });
  container.querySelectorAll("[data-trading-bot-increase-runs]").forEach((btn) => {
    bindTradingActionButton(btn, () => increaseTradingBotMaxRuns(btn.dataset.tradingBotIncreaseRuns || ""), "正在增加可執行次數...", "增加機器人次數失敗");
  });
  container.querySelectorAll("[data-trading-bot-budget]").forEach((btn) => {
    bindTradingActionButton(btn, () => adjustTradingBotBudget(btn.dataset.tradingBotBudget || ""), "正在調整機器人可用上限...", "調整機器人可用上限失敗");
  });
	  container.querySelectorAll("[data-trading-bot-backtest]").forEach((btn) => {
	    bindTradingActionButton(btn, () => prepareTradingBacktestFromBot(btn.dataset.tradingBotBacktest || ""), "正在帶入回測設定...", "回測設定帶入失敗");
	  });
	  container.querySelectorAll("[data-trading-bot-share]").forEach((btn) => {
	    bindTradingActionButton(
	      btn,
	      () => setTradingBotParameterShare(btn.dataset.tradingBotShare || "", btn.dataset.tradingBotShareEnabled === "1"),
	      btn.dataset.tradingBotShareEnabled === "1" ? "正在開啟參數分享..." : "正在停止參數分享...",
	      "參數分享狀態更新失敗"
	    );
	  });
  container.querySelectorAll("[data-grid-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const uuid = btn.dataset.gridToggle;
      const enable = btn.dataset.gridEnabled === "1";
      btn.disabled = true;
      try {
        const csrf = await tradingFreshCsrfToken();
        const res = await apiFetch(`${API}/trading/grid-bots/${uuid}/toggle`, {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
          body: JSON.stringify({ enabled: enable }),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || !json.ok) { tradingSetMsg(tradingFriendlyErrorText(json.msg || "狀態更新失敗"), false); return; }
        await loadGridBots();
        renderMyBotsList();
      } catch (e) { tradingSetMsg(e.message || "網格機器人狀態更新失敗", false); }
      finally { btn.disabled = false; }
    });
  });
	  container.querySelectorAll("[data-grid-delete]").forEach((btn) => {
	    bindTradingActionButton(btn, () => deleteGridBot(btn.dataset.gridDelete || ""), "正在刪除網格機器人...", "網格機器人刪除失敗");
	  });
	  container.querySelectorAll("[data-grid-share]").forEach((btn) => {
	    bindTradingActionButton(
	      btn,
	      () => setGridBotParameterShare(btn.dataset.gridShare || "", btn.dataset.gridShareEnabled === "1"),
	      btn.dataset.gridShareEnabled === "1" ? "正在開啟網格參數分享..." : "正在停止網格參數分享...",
	      "網格參數分享狀態更新失敗"
	    );
	  });
  container.querySelectorAll("[data-grid-backtest]").forEach((btn) => {
    bindTradingActionButton(btn, () => prepareTradingBacktestFromBot(btn.dataset.gridBacktest || ""), "正在帶入回測設定...", "回測設定帶入失敗");
  });
}

function clearBotChartOverlay() {
  tradingBotChartOverlay = null;
  const btn = $("trading-chart-clear-overlay-btn");
  if (btn) btn.style.display = "none";
  if (tradingState.referencePrices) renderTradingReferenceChart(tradingState.referencePrices);
}

function setBotChartOverlay(type, botUuid, marketSymbol) {
  if (type === "grid") {
    const bot = (tradingGridBots || []).find((b) => b.bot_uuid === botUuid);
    if (!bot) return;
    const levels = Array.isArray(bot.grid_levels) ? bot.grid_levels : [];
    tradingBotChartOverlay = { type: "grid", levels, symbol: marketSymbol };
  } else {
    const bot = (tradingState.bots || []).find((b) => b.bot_uuid === botUuid);
    if (!bot) return;
    const botRuns = (tradingState.botRuns || []).filter((r) => (Number(r.bot_id) === Number(bot.id) || r.bot_uuid === bot.bot_uuid) && r.status === "triggered");
    const runs = botRuns.map((r) => ({
      time: new Date(r.created_at || 0).getTime(),
      price: Number(r.observed_price_points || 0),
      side: bot.side || "buy",
    })).filter((r) => r.time > 0 && r.price > 0);
    tradingBotChartOverlay = { type: "bot", runs, symbol: marketSymbol };
  }
  const clearBtn = $("trading-chart-clear-overlay-btn");
  if (clearBtn) clearBtn.style.display = "";

  // Switch chart market to bot's market and reload
  const marketSelect = $("trading-market-select");
  if (marketSelect && marketSymbol && marketSelect.value !== marketSymbol) {
    if (Array.from(marketSelect.options).some((o) => o.value === marketSymbol)) {
      marketSelect.value = marketSymbol;
      renderTradingSummary();
    }
  }
  loadTradingReferencePrices().catch((err) => {
    reportFrontendFailure("trading-reference-prices-overlay", err);
    if (tradingState.referencePrices) renderTradingReferenceChart(tradingState.referencePrices);
    tradingSetMsg(`圖表已切換到 ${tradingDisplaySymbol(marketSymbol || "")}，但參考價格更新失敗`, false);
  });
  tradingSetMsg(`圖表已切換到 ${tradingDisplaySymbol(marketSymbol || "")}，標記 ${type === "grid" ? "網格層級" : "交易點位"}`);
}

function drawBotChartOverlay(ctx, overlay, yForPrice, pad, width, chartH, candles) {
  if (!overlay) return;
  const chartMarket = tradingState.referencePrices?.market;
  if (chartMarket && overlay.symbol && chartMarket !== overlay.symbol) return;
  ctx.save();
  if (overlay.type === "grid") {
    ctx.setLineDash([5, 3]);
    const n = overlay.levels.length;
    overlay.levels.forEach((price, i) => {
      const y = yForPrice(price);
      if (y < pad.top - 2 || y > pad.top + chartH + 2) return;
      const isBoundary = i === 0 || i === n - 1;
      ctx.strokeStyle = isBoundary ? "rgba(239,68,68,.6)" : "rgba(250,204,21,.4)";
      ctx.lineWidth = isBoundary ? 1.5 : 0.8;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
    });
    ctx.setLineDash([]);
  } else if (overlay.type === "bot") {
    overlay.runs.forEach(({ time, price, side }) => {
      const y = yForPrice(price);
      if (y < pad.top || y > pad.top + chartH) return;
      let nearestX = null;
      let minDiff = Infinity;
      for (const c of candles) {
        const cTime = Number(c.time || 0);
        const diff = Math.abs(cTime - time);
        if (diff < minDiff) { minDiff = diff; nearestX = c.x; }
      }
      if (nearestX == null) return;
      const sz = 9;
      ctx.beginPath();
      if (side === "buy") {
        ctx.fillStyle = "rgba(34,197,94,.92)";
        ctx.moveTo(nearestX, y - sz);
        ctx.lineTo(nearestX - sz * 0.8, y + sz * 0.5);
        ctx.lineTo(nearestX + sz * 0.8, y + sz * 0.5);
      } else {
        ctx.fillStyle = "rgba(239,68,68,.92)";
        ctx.moveTo(nearestX, y + sz);
        ctx.lineTo(nearestX - sz * 0.8, y - sz * 0.5);
        ctx.lineTo(nearestX + sz * 0.8, y - sz * 0.5);
      }
      ctx.closePath();
      ctx.fill();
    });
  }
  ctx.restore();
}

// ── Grid Trading Bot ────────────────────────────────────────────────────────

let tradingGridBots = [];
let tradingGridPreviewState = null;
let tradingGridPreviewTimer = null;
let tradingGridPreviewRequestSeq = 0;

window.resetTradingBotsAccountState = function resetTradingBotsAccountState() {
  if (tradingGridPreviewTimer) clearTimeout(tradingGridPreviewTimer);
  tradingGridPreviewTimer = null;
  tradingGridPreviewRequestSeq += 1;
  tradingGridBots = [];
  tradingGridPreviewState = null;
  tradingWorkflowBenchmarkCache = null;
  tradingWorkflowBenchmarkLoading = false;
};

function gridComputeLevels(lower, upper, count, mode) {
  if (count < 2 || lower <= 0 || upper <= lower) return [];
  const levels = [];
  if (mode === "geometric") {
    const ratio = Math.pow(upper / lower, 1 / (count - 1));
    for (let i = 0; i < count; i++) levels.push(Math.round(lower * Math.pow(ratio, i)));
  } else {
    const step = (upper - lower) / (count - 1);
    for (let i = 0; i < count; i++) levels.push(Math.round(lower + step * i));
  }
  return levels;
}

// 6 grid presets keyed off current market reference price. Numbers
// validated in security/competition_grid_skyfloor_test.py + the spacing
// follow-up in security/competition_grid_spacing_test.py against 5
// assets × 5y × 1h. Each preset declares its OWN best spacing_mode
// based on that data — most sky-floor variants prefer geometric (a few
// pp better), but skyfloor_5x's 100× range actually favours arithmetic
// by ~19pp because the wider absolute steps capture larger per-grid
// profit when price moves are big.
const TRADING_GRID_PRESETS = {
  conservative:    { lower_factor: 0.80, upper_factor: 1.20, grid_count: 10,  order_amount: 5000, spacing_mode: "arithmetic" },
  balanced:        { lower_factor: 0.50, upper_factor: 1.50, grid_count: 20,  order_amount: 5000, spacing_mode: "arithmetic" },
  skyfloor_narrow: { lower_factor: 0.20, upper_factor: 1.80, grid_count: 50,  order_amount: 2000, spacing_mode: "geometric" },
  skyfloor_mid:    { lower_factor: 0.10, upper_factor: 3.00, grid_count: 50,  order_amount: 2000, spacing_mode: "geometric" },
  skyfloor_wide:   { lower_factor: 0.10, upper_factor: 3.00, grid_count: 100, order_amount: 1000, spacing_mode: "geometric" },
  // skyfloor_5x: 100× range; arithmetic empirically beats geometric by ~19pp
  // on the 5y benchmark — see GRID_SPACING_COMPARISON.md.
  skyfloor_5x:     { lower_factor: 0.05, upper_factor: 5.00, grid_count: 100, order_amount: 1000, spacing_mode: "arithmetic" },
};

function tradingSetGridPresetFieldValue(id, value, { notify = true } = {}) {
  const el = $(id);
  if (!el) return;
  el.value = String(value);
  if (!notify) return;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function tradingGridPresetMarket() {
  const markets = Array.isArray(tradingState.markets) ? tradingState.markets : [];
  const select = $("trading-grid-bot-market");
  const selectedSymbol = select?.value || "";
  const botMarkets = markets.filter((market) => market.allow_bots !== false);
  let market = botMarkets.find((row) => row.symbol === selectedSymbol) || null;
  if (!market) {
    const activeMarket = selectedTradingMarket();
    if (activeMarket && activeMarket.allow_bots !== false) market = activeMarket;
  }
  if (!market) market = botMarkets[0] || markets[0] || null;
  if (select && market?.symbol && Array.from(select.options || []).some((option) => option.value === market.symbol)) {
    select.value = market.symbol;
  }
  return market;
}

function tradingGridPresetReferencePrice(market) {
  return tradingMarketPricePoints(market, "reference") || tradingMarketPricePoints(market, "risk_grade");
}

function applyGridPreset(options = {}) {
  const opts = options && options.type ? {} : (options || {});
  const quiet = !!opts.quiet;
  const shouldSave = opts.save !== false;
  const select = $("trading-grid-preset");
  if (!select) return;
  const key = select.value || "";
  if (!key) return;
  const cfg = TRADING_GRID_PRESETS[key];
  if (!cfg) return;
  const market = tradingGridPresetMarket();
  if (!market?.symbol) {
    if (!quiet) tradingSetMsg("請先選擇市場再套用預設");
    return;
  }
  const refPrice = tradingGridPresetReferencePrice(market);
  if (!refPrice || refPrice <= 0) {
    if (!quiet) tradingSetMsg("該市場目前沒有可參考的市價，無法計算上下限");
    return;
  }
  const lower = Math.max(1, Math.round(refPrice * cfg.lower_factor));
  const upper = Math.max(lower + 1, Math.round(refPrice * cfg.upper_factor));
  tradingSetGridPresetFieldValue("trading-grid-lower-price", lower);
  tradingSetGridPresetFieldValue("trading-grid-upper-price", upper);
  tradingSetGridPresetFieldValue("trading-grid-count", cfg.grid_count);
  tradingSetGridPresetFieldValue("trading-grid-order-amount", cfg.order_amount);
  // Each preset carries its own empirically-tuned spacing_mode; see
  // docs/archive/competition_2026-05-06/GRID_SPACING_COMPARISON.md for the
  // per-config table.
  if (cfg.spacing_mode) tradingSetGridPresetFieldValue("trading-grid-spacing-mode", cfg.spacing_mode);
  if (shouldSave) saveTradingPersonalFormState();
  if (typeof scheduleGridBotPreview === "function") scheduleGridBotPreview();
  if (!quiet) tradingSetMsg(`已套用預設「${key}」（市價 ${refPrice}） — 區間 ${lower}–${upper}，間距 ${cfg.spacing_mode}`);
}

function clearGridBotPreview() {
  tradingGridPreviewState = null;
  const preview = $("trading-grid-preview");
  if (preview) preview.innerHTML = "";
}

function collectGridBotPreviewPayload() {
  const marketSymbol = $("trading-grid-bot-market")?.value || "";
  const upper = Number($("trading-grid-upper-price")?.value || 0);
  const lower = Number($("trading-grid-lower-price")?.value || 0);
  const count = Number($("trading-grid-count")?.value || 10);
  const amount = Number($("trading-grid-order-amount")?.value || 0);
  const mode = $("trading-grid-spacing-mode")?.value || "arithmetic";
  if (!upper || !lower || upper <= lower || count < 2 || amount < 1) {
    return null;
  }
  if (!marketSymbol) return null;
  return {
    market_symbol: marketSymbol,
    upper_price_points: upper,
    lower_price_points: lower,
    grid_count: count,
    order_amount_points: amount,
    spacing_mode: mode,
    order_mode: "maker",
  };
}

function scheduleGridBotPreview() {
  if (tradingGridPreviewTimer) clearTimeout(tradingGridPreviewTimer);
  const operation = tradingOperationContext();
  tradingGridPreviewTimer = setTimeout(() => {
    tradingGridPreviewTimer = null;
    if (!tradingOperationIsCurrent(operation)) return;
    renderGridBotPreview({ quiet: true, operation }).catch((err) => {
      if (tradingOperationIsCurrent(operation) && !tradingIsAbortError(err)) tradingSetMsg(tradingFriendlyErrorText(err?.message || "網格試算失敗"), false);
    });
  }, 150);
}

async function renderGridBotPreview({ quiet = true, operation = null } = {}) {
  const operationContext = operation || tradingOperationContext();
  tradingAssertOperationCurrent(operationContext);
  const preview = $("trading-grid-preview");
  if (!preview) return null;
  const payload = collectGridBotPreviewPayload();
  if (!payload) {
    clearGridBotPreview();
    return null;
  }
  const upper = Number(payload.upper_price_points || 0);
  const lower = Number(payload.lower_price_points || 0);
  const count = Number(payload.grid_count || 10);
  const amount = Number(payload.order_amount_points || 0);
  const mode = payload.spacing_mode || "arithmetic";
  const levels = gridComputeLevels(lower, upper, count, mode);
  if (levels.length < 2) {
    clearGridBotPreview();
    return null;
  }
  const requestSeq = ++tradingGridPreviewRequestSeq;
  let feePreview = null;
  try {
    const csrf = await tradingFreshCsrfToken();
    const res = await apiFetch(`${API}/trading/grid/preview`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload),
    });
    const json = await res.json().catch(() => ({}));
    tradingAssertOperationCurrent(operationContext);
    if (requestSeq !== tradingGridPreviewRequestSeq) return tradingGridPreviewState;
    if (!res.ok || !json.ok) {
      tradingGridPreviewState = null;
      preview.innerHTML = `<div class="negative">網格試算失敗：${sanitize(tradingFriendlyErrorText(json.msg || "無法計算目前設定"))}</div>`;
      if (!quiet) tradingSetMsg(tradingFriendlyErrorText(json.msg || "網格試算失敗"), false);
      return null;
    }
    feePreview = json;
    tradingGridPreviewState = json;
  } catch (e) {
    if (!tradingOperationIsCurrent(operationContext) || tradingIsAbortError(e)) return null;
    if (requestSeq !== tradingGridPreviewRequestSeq) return tradingGridPreviewState;
    tradingGridPreviewState = null;
    preview.innerHTML = `<div class="negative">網格試算失敗：${sanitize(e.message || "請稍後再試")}</div>`;
    if (!quiet) tradingSetMsg(e.message || "網格試算失敗", false);
    return null;
  }

  const market = (tradingState.markets || []).find((m) => m.symbol === payload.market_symbol);
  const feeRatePct = market ? Number(market.fee_rate_percent || 0) : 0;
  const gridDiscountPct = Math.max(0, Math.min(100, tradingNumber(tradingState.settings?.grid_fee_discount_percent, 25)));
  const stepPcts = levels.slice(1).map((p, i) => ((p - levels[i]) / levels[i]) * 100);
  const minStepPct = Math.min(...stepPcts);
  const maxStepPct = Math.max(...stepPcts);
  const arithmeticStep = Math.round((upper - lower) / (count - 1));
  const stepInfo = mode === "geometric"
    ? `固定間距 ${minStepPct.toFixed(2)}%（等比）`
    : `固定間距 ${formatTradingPointsValue(arithmeticStep)} 點（等差）`;
  const midStepPct = ((upper - lower) / (count - 1) / ((upper + lower) / 2) * 100).toFixed(2);

  // Estimate current price from market or use midpoint
  const marketPrice = market ? tradingMarketPricePoints(market, "reference") : 0;
  const refPrice = marketPrice || Math.round((upper + lower) / 2);
  const buyLevels = levels.filter((p) => p < refPrice);
  const sellLevels = levels.filter((p) => p > refPrice);
  // Buy orders: freeze amount + fee per level
  const gridBuyFeePercent = tradingNumber(feePreview?.fee_model?.buy_fee_percent, feeRatePct * ((100 - gridDiscountPct) / 100));
  const feePerBuyOrder = Math.max(0, amount * gridBuyFeePercent / 100);
  const buyOrderCost = amount + feePerBuyOrder;
  const buyCostTotal = buyLevels.length * buyOrderCost;
  // Sell orders: need spot inventory (asset units); estimate cost at current price
  const spotUnitsNeeded = sellLevels.reduce((sum, p) => sum + (p > 0 ? amount / p : 0), 0);
  const assetLabel = market ? tradingDisplaySymbol(market.symbol).split("/")[0] : "資產";
  const spotDisplay = spotUnitsNeeded > 0 ? `約 ${spotUnitsNeeded.toFixed(6)} 個 ${assetLabel}` : "0";
  // Spot acquisition cost at current price + buy fee (full rate, not grid discount)
  const spotValueAtRefPrice = spotUnitsNeeded * refPrice;
  const spotBuyFee = spotValueAtRefPrice * feeRatePct / 100;
  const spotTotalCost = Math.ceil(spotValueAtRefPrice + spotBuyFee);
  // Total capital if opening ALL orders from scratch (buy frozen + spot acquisition)
  const totalCapital = Math.ceil(buyCostTotal) + spotTotalCost;
  const feeReserve = Number(feePreview?.grid_profit?.estimated_total_fee || 0);

  let feeHtml = "";
  if (feePreview) {
    const risk = feePreview.risk || {};
    const riskStatus = risk.status || "green";
    const riskColor = riskStatus === "green" ? "#16a34a" : (riskStatus === "yellow" ? "#f59e0b" : "#ef4444");
    const riskLabel = riskStatus === "green" ? "綠燈" : (riskStatus === "yellow" ? "黃燈" : "紅燈");
    feeHtml = `
      <div style="margin-top:.55rem;padding:.75rem;border:1px solid ${riskColor};border-radius:.6rem;background:rgba(15,23,42,.03);">
        <div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.35rem;">
          <strong style="color:${riskColor};">${riskLabel}</strong>
          <span>網格費率試算（限價掛單 / maker）</span>
        </div>
        <div>每格間距：${formatTradingPointsValue(feePreview.grid_profit?.grid_spacing_percent || 0)}%</div>
        <div>買入手續費：${formatTradingPointsValue(feePreview.fee_model?.buy_fee_percent || 0)}% · 賣出手續費：${formatTradingPointsValue(feePreview.fee_model?.sell_fee_percent || 0)}% · 來回：${formatTradingPointsValue(feePreview.fee_model?.round_trip_fee_percent || 0)}%</div>
        <div>損益兩平間距：約 ${formatTradingPointsValue(feePreview.break_even?.min_spread_percent || 0)}%</div>
        <div>最不利一格毛利：約 ${formatTradingPointsValue(feePreview.grid_profit?.estimated_gross_profit_per_grid || 0)} 點</div>
        <div>最不利一格手續費：約 ${formatTradingPointsValue(feePreview.grid_profit?.estimated_fee_per_grid || 0)} 點</div>
        <div>最不利一格扣費後淨利：約 ${formatTradingPointsValue(feePreview.grid_profit?.estimated_net_profit_per_grid || 0)} 點（${formatTradingPointsValue(feePreview.grid_profit?.estimated_net_spread_percent || 0)}%）</div>
        <div>預估一輪全格總手續費：約 ${formatTradingPointsValue(feePreview.grid_profit?.estimated_total_fee || 0)} 點 · 預估一輪全格總淨利：約 ${formatTradingPointsValue(feePreview.grid_profit?.estimated_total_net_profit || 0)} 點</div>
        <div style="margin-top:.35rem;color:${riskColor};font-weight:600;">${sanitize(risk.message || "")}</div>
        <div style="margin-top:.25rem;color:var(--muted);font-size:.82em;">現貨基礎費率 ${formatTradingPointsValue(feePreview.fee_model?.spot_fee_percent || feeRatePct)}%，Grid 折扣 ${formatTradingPointsValue(feePreview.fee_model?.grid_discount_percent || gridDiscountPct)}%。實際建立網格前仍會由後端再次驗證，不接受前端自行改值。</div>
      </div>
    `.trim();
  }

  const capitalBreakdown = refPrice > 0
    ? `買單凍結 ${formatTradingPointsValue(Math.ceil(buyCostTotal))} ＋ 賣單底倉 ${formatTradingPointsValue(spotTotalCost)}（以現價購入含手續費）${feeReserve > 0 ? ` ＋ 手續費預留 ${formatTradingPointsValue(feeReserve)}` : ""}`
    : `買單凍結 ${formatTradingPointsValue(Math.ceil(buyCostTotal))}（賣單需現有底倉 ${spotDisplay}，無現價無法估算購入成本）`;
  const totalLine = refPrice > 0
    ? `<div style="margin-top:.35rem;font-weight:600;">預估開單所需總資金：${formatTradingPointsValue(totalCapital + feeReserve)} 點</div><div style="color:var(--muted);font-size:.82em;">${capitalBreakdown}</div>`
    : `<div style="margin-top:.35rem;">買單 ${buyLevels.length} 格需 ${formatTradingPointsValue(Math.ceil(buyCostTotal))} 點，賣單 ${sellLevels.length} 格需底倉 ${spotDisplay}</div>`;

  preview.innerHTML = `
    <div>${count} 格，${stepInfo}（中間位約 ${midStepPct}%），每格 ${formatTradingPointsValue(amount)} 點</div>
    <div>買單 ${buyLevels.length} 格（凍結積分），賣單 ${sellLevels.length} 格（需底倉 ${spotDisplay}）</div>
    ${totalLine}
    ${feeHtml}
  `.trim();
  return feePreview;
}

function renderGridBotVisual(bot, currentPrice) {
  const levels = Array.isArray(bot.grid_levels) ? bot.grid_levels : [];
  if (!levels.length) return `<div class="drive-card-sub">（無網格層）</div>`;
  const orders = Array.isArray(bot.orders) ? bot.orders : [];
  const orderByLevel = {};
  // Sort by id ascending so the highest id (most recent) wins per level
  const sortedOrders = [...orders].sort((a, b) => Number(a.id || 0) - Number(b.id || 0));
  for (const o of sortedOrders) {
    orderByLevel[o.level_index] = o;
  }
  const cp = Number(currentPrice || bot.initial_price_points || 0);
  const rows = [...levels].reverse().map((price, revIdx) => {
    const idx = levels.length - 1 - revIdx;
    const order = orderByLevel[idx];
    const isCurrent = cp > 0 && Math.abs(price - cp) / cp < 0.005;
    let statusClass = "grid-level-empty";
    let statusText = "—";
    let sideLabel = "";
    if (order) {
      if (order.status === "open") {
        if (order.side === "buy") { statusClass = "grid-level-buy"; statusText = "掛買"; sideLabel = "BUY"; }
        else { statusClass = "grid-level-sell"; statusText = "掛賣"; sideLabel = "SELL"; }
      } else if (order.status === "filled") {
        statusClass = "grid-level-filled";
        statusText = order.side === "buy" ? "買入成交" : "賣出成交";
        sideLabel = order.side === "buy" ? "BUY✓" : "SELL✓";
      } else {
        statusClass = "grid-level-cancelled";
        statusText = "已取消";
      }
    }
    const currentMark = isCurrent ? `<span class="grid-current-price-mark">◀ 現價</span>` : "";
    return `<div class="grid-level-row ${statusClass}${isCurrent ? " grid-current-price-row" : ""}">
      <span class="grid-level-price">${formatTradingPointsValue(price)}</span>
      <span class="grid-level-side">${sideLabel}</span>
      <span class="grid-level-status">${statusText}</span>
      ${currentMark}
    </div>`;
  });
  return `<div class="grid-visual">${rows.join("")}</div>`;
}

function renderGridBotFills(fills) {
  if (!fills.length) return `<div class="drive-card-sub">尚無成交紀錄</div>`;
  return fills.slice().reverse().slice(0, 30).map((o) => {
    const side = o.side === "buy" ? "買入" : "賣出";
    const cls = o.side === "buy" ? "grid-buy-count" : "grid-sell-count";
    return `<div class="drive-card-sub" style="white-space:nowrap;"><span class="${cls}">${side}</span> ${formatTradingPointsValue(o.price_points)} 點 · 第 ${Number(o.level_index) + 1} 層${o.updated_at ? " · " + sanitize(String(o.updated_at).slice(0, 16).replace("T", " ")) : ""}</div>`;
  }).join("");
}

function renderGridBotList(bots, currentPriceMap) {
  const container = $("trading-grid-bot-list");
  if (!container) return;
  if (!bots.length) {
    container.innerHTML = `<div class="drive-empty">尚無網格機器人，在上方建立第一個網格</div>`;
    return;
  }
  container.innerHTML = bots.map((bot) => {
    const cp = (currentPriceMap || {})[bot.market_symbol] || bot.initial_price_points || 0;
    const symbol = sanitize(tradingDisplaySymbol(bot.market_symbol || ""));
    const assetSymbol = sanitize((tradingDisplaySymbol(bot.market_symbol || "") || "").split("/")[0] || "資產");
    const profit = Number(bot.total_profit_points || 0);
    const profitClass = profit >= 0 ? "positive" : "negative";
    const orders = Array.isArray(bot.orders) ? bot.orders : [];
    const openOrders = orders.filter((o) => o.status === "open");
    const filledOrders = orders.filter((o) => o.status === "filled");
    const fills = tradingBotRecentFills(bot);
    const buyOrders = openOrders.filter((o) => o.side === "buy");
    const sellOrders = openOrders.filter((o) => o.side === "sell");
    const levels = Array.isArray(bot.grid_levels) ? bot.grid_levels : [];
    const SCALE = 100_000_000;
    // Net inventory: buy fills - sell fills (in asset units from filled_quantity_units)
    const buyFillUnits = filledOrders.filter((o) => o.side === "buy").reduce((s, o) => s + Number(o.filled_quantity_units || 0), 0);
    const sellFillUnits = filledOrders.filter((o) => o.side === "sell").reduce((s, o) => s + Number(o.filled_quantity_units || 0), 0);
    const netInventoryUnits = buyFillUnits - sellFillUnits;
    const netInventoryDisplay = (netInventoryUnits / SCALE).toFixed(6);
    // Open orders locked value
    const buyLockedPoints = buyOrders.length * Number(bot.order_amount_points || 0);
    const sellLockedUnits = sellOrders.reduce((s, o) => s + (o.price_points > 0 ? Number(bot.order_amount_points || 0) / o.price_points : 0), 0);
    // Fee estimate: total trades × amount × grid fee rate after root-configured discount.
    const marketObj = (tradingState.markets || []).find((m) => m.symbol === bot.market_symbol);
    const feeRatePct = marketObj ? Number(marketObj.fee_rate_percent || 0) : 0;
    const gridDiscountPct = Math.max(0, Math.min(100, tradingNumber(tradingState.settings?.grid_fee_discount_percent, 25)));
    const totalTrades = Number(bot.total_trades || 0);
    const estimatedFee = totalTrades * Number(bot.order_amount_points || 0) * feeRatePct * ((100 - gridDiscountPct) / 100) / 100;
    return `<div class="drive-file-row grid-bot-card" data-grid-bot-uuid="${sanitize(bot.bot_uuid || "")}">
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;">
          <strong>${sanitize(bot.name || "網格機器人")} · ${symbol}</strong>
          <span class="grid-status-badge ${bot.enabled ? "grid-status-running" : "grid-status-stopped"}">${bot.enabled ? "運行中" : "已暫停"}</span>
        </div>
        <div class="drive-card-sub">
          區間 ${formatTradingPointsValue(bot.lower_price_points)} ～ ${formatTradingPointsValue(bot.upper_price_points)} · ${Number(bot.grid_count)} 格 · 每格 ${formatTradingPointsValue(bot.order_amount_points)} 點
        </div>
        <div class="drive-card-sub">
          現價 ${formatTradingPointsValue(cp)} · 底倉 ${netInventoryDisplay} ${assetSymbol} · 掛單：<span class="grid-buy-count">買 ${buyOrders.length} 格（${formatTradingPointsValue(buyLockedPoints)} 點）</span> / <span class="grid-sell-count">賣 ${sellOrders.length} 格（約 ${sellLockedUnits.toFixed(6)} ${assetSymbol}）</span>
        </div>
	        <div class="drive-card-sub">
	          已成交 ${totalTrades} 次 · 估計手續費支出 ${formatTradingPointsValue(Math.round(estimatedFee))} 點 · 累計盈虧 <span class="${profitClass}">${profit >= 0 ? "+" : ""}${formatTradingPointsValue(profit)}</span> 點
	          ${tradingParameterShareBadge(bot)}
	        </div>
        <div class="drive-card-sub">${sanitize(tradingRiskTargetText(bot.stop_loss_percent, bot.take_profit_percent))}</div>
        ${bot.last_error ? `<div class="drive-card-sub negative">錯誤：${sanitize(bot.last_error)}</div>` : ""}
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>設定摘要</summary>
          <div class="drive-card-sub">${sanitize(symbol)} · 區間 ${formatTradingPointsValue(bot.lower_price_points)} ～ ${formatTradingPointsValue(bot.upper_price_points)} · ${Number(bot.grid_count)} 格 · 每格 ${formatTradingPointsValue(bot.order_amount_points)} 點 · ${sanitize(tradingRiskTargetText(bot.stop_loss_percent, bot.take_profit_percent))}</div>
        </details>
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>掛單明細（${openOrders.length} 筆掛單）</summary>
          <div style="display:flex;gap:1rem;align-items:flex-start;flex-wrap:wrap;">
            <div style="flex:0 0 auto;">
              ${renderGridBotVisual(bot, cp)}
            </div>
            <div style="flex:1;min-width:160px;">
              <div class="drive-card-sub" style="margin-bottom:.25rem;font-weight:600;">掛單成交層級（${orders.filter((o) => o.status === "filled").length} 筆）</div>
              ${renderGridBotFills(orders.filter((o) => o.status === "filled"))}
            </div>
          </div>
        </details>
        <details class="economy-collapse" style="margin-top:.35rem;">
          <summary>交易明細（${fills.length} 筆）</summary>
          ${renderTradingBotFillDetails(fills)}
        </details>
      </div>
	      <div class="drive-file-actions" style="flex-shrink:0;">
	        <button class="btn btn-sm" type="button" data-grid-backtest="${sanitize(bot.bot_uuid || "")}">回測</button>
	        <button class="btn btn-sm" type="button" data-grid-share="${sanitize(bot.bot_uuid || "")}" data-grid-share-enabled="${bot.share_parameters ? "0" : "1"}">${bot.share_parameters ? "停止分享" : "分享參數"}</button>
	        <button class="btn btn-sm" type="button" data-grid-toggle="${sanitize(bot.bot_uuid || "")}" data-grid-enabled="${bot.enabled ? "0" : "1"}">${bot.enabled ? "暫停" : "啟用"}</button>
        <button class="btn btn-sm btn-danger" type="button" data-grid-delete="${sanitize(bot.bot_uuid || "")}">刪除</button>
      </div>
    </div>`;
  }).join("");
  container.querySelectorAll("[data-grid-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const uuid = btn.dataset.gridToggle;
      const enable = btn.dataset.gridEnabled === "1";
      btn.disabled = true;
      try {
        const csrf = await tradingFreshCsrfToken();
        const res = await apiFetch(`${API}/trading/grid-bots/${uuid}/toggle`, {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
          body: JSON.stringify({ enabled: enable }),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || !json.ok) { tradingSetMsg(tradingFriendlyErrorText(json.msg || "狀態更新失敗"), false); return; }
        await loadGridBots();
      } catch (e) { tradingSetMsg(e.message || "網格機器人狀態更新失敗", false); }
      finally { btn.disabled = false; }
    });
  });
	  container.querySelectorAll("[data-grid-delete]").forEach((btn) => {
	    bindTradingActionButton(btn, () => deleteGridBot(btn.dataset.gridDelete || ""), "正在刪除網格機器人...", "網格機器人刪除失敗");
	  });
	  container.querySelectorAll("[data-grid-share]").forEach((btn) => {
	    bindTradingActionButton(
	      btn,
	      () => setGridBotParameterShare(btn.dataset.gridShare || "", btn.dataset.gridShareEnabled === "1"),
	      btn.dataset.gridShareEnabled === "1" ? "正在開啟網格參數分享..." : "正在停止網格參數分享...",
	      "網格參數分享狀態更新失敗"
	    );
	  });
  container.querySelectorAll("[data-grid-backtest]").forEach((btn) => {
    bindTradingActionButton(btn, () => prepareTradingBacktestFromBot(btn.dataset.gridBacktest || ""), "正在帶入回測設定...", "回測設定帶入失敗");
  });
}

async function deleteGridBot(uuid) {
  if (!uuid) return;
  if (!confirm("確定結束網格機器人？這會取消所有網格掛單並移除機器人。")) return;
  const sellBase = confirm("網格結束後要賣出這個網格鎖定的現貨底倉嗎？\n\n確定：用市價賣出底倉\n取消：保留為現貨底倉");
  const baseAction = sellBase ? "sell" : "keep";
  const csrf = await tradingFreshCsrfToken();
  const res = await apiFetch(`${API}/trading/grid-bots/${uuid}`, {
    method: "DELETE", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ base_action: baseAction }),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(tradingFriendlyErrorText(json.msg || "刪除失敗"));
  const baseQty = formatTradingQuantityValue(Number(json.base_quantity_units || 0) / 100000000);
  if (json.sell_error) {
    tradingSetMsg(`網格機器人已結束；底倉賣出失敗，已改為保留現貨（底倉約 ${baseQty}）。${tradingFriendlyErrorText(json.sell_error)}`, false);
  } else if (baseAction === "sell") {
    tradingSetMsg(`網格機器人已結束；已送出底倉賣出（約 ${baseQty}）。`);
  } else {
    tradingSetMsg(`網格機器人已結束；底倉已保留為現貨（約 ${baseQty}）。`);
  }
  tradingGridExpandedBots.delete(uuid);
  await loadGridBots();
  await loadTradingDashboard();
}

async function loadGridBots() {
  try {
    const res = await apiFetch(`${API}/trading/grid-bots`, { credentials: "same-origin" });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      throw new Error(tradingFriendlyErrorText(json.msg || `網格機器人讀取失敗（HTTP ${res.status}）`));
    }
    tradingGridBots = Array.isArray(json.bots) ? json.bots : [];
    const priceMap = {};
    for (const m of (tradingState.markets || [])) {
      priceMap[m.symbol] = tradingMarketPricePoints(m, "reference");
    }
    renderGridBotList(tradingGridBots, priceMap);
    renderMyBotsList();
    renderTradingBotPositionCard();
    refreshBacktestBotSelect();
  } catch (e) {
    tradingSetMsg(e?.message || "網格機器人讀取失敗", false);
  }
}

async function loadTradingBotCompetition() {
  if (!currentUser || !canAccessModule("economy")) return;
  const week = $("trading-bot-competition-week")?.value?.trim() || "";
  const path = `/trading/bot-competition${week ? `?week=${encodeURIComponent(week)}` : ""}`;
  const json = await fetchTradingJson(path, { forceCsrf: false });
  tradingState.botCompetition = json.competition || json;
  renderTradingBotCompetition(tradingState.botCompetition);
}

async function awardTradingBotCompetition() {
  if (currentUser !== "root") {
    tradingSetMsg("只有 root 可以發放競賽獎勵", false);
    return;
  }
  const week = $("trading-bot-competition-award-btn")?.dataset?.awardWeek || tradingState.botCompetition?.previous_week || "";
  const json = await fetchTradingJson("/root/trading/bot-competition/award", {
    method: "POST",
    body: JSON.stringify({ week }),
  });
  const awarded = Array.isArray(json.awarded) ? json.awarded : [];
  tradingSetMsg(awarded.length ? `已發放 ${awarded.length} 筆機器人週賽獎勵` : "沒有新的週賽獎勵需要發放");
  await loadTradingBotCompetition();
  if (typeof loadEconomyDashboard === "function") {
    loadEconomyDashboard().catch((err) => reportFrontendFailure("trading-award-economy-refresh", err));
  }
}

function computeGridSpotSituation(marketSymbol, lower, upper, count, amount, mode) {
  const market = (tradingState.markets || []).find((m) => m.symbol === marketSymbol);
  const currentPrice = market ? tradingMarketPricePoints(market, "reference") : 0;
  if (!currentPrice || lower >= upper || count < 2) return null;
  const levels = gridComputeLevels(lower, upper, count, mode);
  const sellLevels = levels.filter((p) => p > currentPrice);
  if (!sellLevels.length) return null;
  const spotNeeded = sellLevels.reduce((sum, p) => sum + (p > 0 ? amount / p : 0), 0);
  const position = currentTradingPosition(marketSymbol);
  const currentSpot = tradingNumber(position?.quantity, 0) + tradingNumber(position?.locked_quantity, 0);
  const deficit = Math.max(0, spotNeeded - currentSpot);
  const feeRatePct = market ? tradingNumber(market.fee_rate_percent, 0) : 0;
  const assetSymbol = (tradingDisplaySymbol(marketSymbol) || "").split("/")[0] || "資產";
  return { spotNeeded, currentSpot, deficit, currentPrice, feeRatePct, market, assetSymbol, sellLevels };
}

function showGridSpotConfirm(situation, formData) {
  const confirmDiv = $("trading-grid-spot-confirm");
  if (!confirmDiv) { doCreateGridBot(formData); return; }
  const { spotNeeded, currentSpot, deficit, currentPrice, feeRatePct, assetSymbol } = situation;
  const fmtQty = (n) => n.toFixed(6);

  let html = "";
  if (deficit <= 0) {
    // Sufficient spot
    html = `<div class="drive-card-sub" style="margin-top:.5rem;padding:.75rem;border:1px solid var(--accent,#4fc3f7);border-radius:.5rem;">
      <div style="font-weight:600;margin-bottom:.5rem;">確認底倉</div>
      <div style="margin-bottom:.5rem;">你目前持有 <strong>${fmtQty(currentSpot)} ${sanitize(assetSymbol)}</strong>，足夠作為賣單底倉（需 ${fmtQty(spotNeeded)} ${sanitize(assetSymbol)}）。</div>
      <div style="margin-bottom:.75rem;color:var(--muted);">直接建立網格，使用現有現貨作為底倉。</div>
      <div style="display:flex;gap:.5rem;flex-wrap:wrap;">
        <button class="btn btn-primary btn-sm" data-grid-spot-action="proceed">確認建立網格</button>
        <button class="btn btn-sm" data-grid-spot-action="cancel">取消</button>
      </div>
    </div>`;
  } else {
    // Deficit — need to buy
    const buyCostPoints = deficit * currentPrice;
    const buyFeePoints = buyCostPoints * (feeRatePct / 100);
    const totalCostPoints = Math.ceil(buyCostPoints + buyFeePoints);
    const hasPartial = currentSpot > 0;
    const desc = hasPartial
      ? `目前持有 <strong>${fmtQty(currentSpot)} ${sanitize(assetSymbol)}</strong>，尚缺 <strong>${fmtQty(deficit)} ${sanitize(assetSymbol)}</strong>`
      : `賣單需 <strong>${fmtQty(spotNeeded)} ${sanitize(assetSymbol)}</strong> 底倉，目前持有 0`;
    html = `<div class="drive-card-sub" style="margin-top:.5rem;padding:.75rem;border:1px solid var(--accent,#4fc3f7);border-radius:.5rem;">
      <div style="font-weight:600;margin-bottom:.5rem;">底倉不足</div>
      <div style="margin-bottom:.5rem;">${desc}。</div>
      <div style="margin-bottom:.5rem;">以現價 <strong>${formatTradingPointsValue(currentPrice)} 點</strong> 買入 <strong>${fmtQty(deficit)} ${sanitize(assetSymbol)}</strong>，預估花費約 <strong>${formatTradingPointsValue(totalCostPoints)} 點</strong>（含手續費）。</div>
      <div style="margin-bottom:.75rem;color:var(--muted);">若積分不足，請先儲值後再操作。</div>
      <div style="display:flex;gap:.5rem;flex-wrap:wrap;">
        <button class="btn btn-primary btn-sm" data-grid-spot-action="buy">買入底倉並建立</button>
        <button class="btn btn-sm" data-grid-spot-action="proceed">不買底倉直接建立</button>
        <button class="btn btn-sm" data-grid-spot-action="cancel">取消</button>
      </div>
    </div>`;
  }
  confirmDiv.innerHTML = html;
  confirmDiv.style.display = "";
  window._gridSpotPending = { situation, formData };
  confirmDiv.querySelectorAll("[data-grid-spot-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const action = btn.dataset.gridSpotAction;
      if (action === "proceed") gridSpotConfirmProceed();
      else if (action === "cancel") gridSpotConfirmCancel();
      else if (action === "buy") gridSpotBuyAndCreate();
    });
  });
}

function gridSpotConfirmCancel() {
  const confirmDiv = $("trading-grid-spot-confirm");
  if (confirmDiv) { confirmDiv.innerHTML = ""; confirmDiv.style.display = "none"; }
  window._gridSpotPending = null;
  const btn = $("trading-grid-bot-create-btn");
  if (btn) btn.disabled = false;
  tradingSetMsg("已取消建立網格");
}

function gridSpotConfirmProceed() {
  const confirmDiv = $("trading-grid-spot-confirm");
  if (confirmDiv) { confirmDiv.innerHTML = ""; confirmDiv.style.display = "none"; }
  const pending = window._gridSpotPending;
  window._gridSpotPending = null;
  if (pending) doCreateGridBot(pending.formData);
}

async function gridSpotBuyAndCreate() {
  const confirmDiv = $("trading-grid-spot-confirm");
  if (confirmDiv) { confirmDiv.innerHTML = ""; confirmDiv.style.display = "none"; }
  const pending = window._gridSpotPending;
  window._gridSpotPending = null;
  if (!pending) return;
  const { situation, formData } = pending;
  const btn = $("trading-grid-bot-create-btn");
  tradingSetMsg("正在買入底倉...");
  try {
    const csrf = await tradingFreshCsrfToken();
    const res = await apiFetch(`${API}/trading/orders`, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({ market_symbol: formData.market_symbol, side: "buy", order_type: "market", quantity: situation.deficit.toFixed(8) }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) { tradingSetMsg(tradingFriendlyErrorText(json.msg || "買入底倉失敗"), false); if (btn) btn.disabled = false; return; }
    tradingSetMsg("底倉買入成功，正在建立網格機器人...");
    await loadTradingDashboard();
  } catch (e) { tradingSetMsg(e.message || "買入底倉失敗", false); if (btn) btn.disabled = false; return; }
  await doCreateGridBot(formData);
}

async function doCreateGridBot(formData) {
  const btn = $("trading-grid-bot-create-btn");
  tradingSetMsg("正在建立網格機器人並掛單...");
  try {
    const csrf = await tradingFreshCsrfToken();
    const res = await apiFetch(`${API}/trading/grid-bots`, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(formData),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) { tradingSetMsg(tradingFriendlyErrorText(json.msg || "網格建立失敗"), false); return; }
    const placed = (json.placed || []).length;
    const errors = json.errors || [];
    let msg = `網格機器人已建立，成功掛單 ${placed} 筆`;
    if (errors.length) msg += `，${errors.length} 個層級失敗`;
    tradingSetMsg(msg, !errors.length);
    if ($("trading-grid-bot-name")) $("trading-grid-bot-name").value = "";
    await loadGridBots();
    await loadTradingDashboard();
  } catch (e) { tradingSetMsg(e.message || "網格機器人建立失敗", false); }
  finally { if (btn) btn.disabled = false; }
}

async function createGridBot() {
  const name = $("trading-grid-bot-name")?.value?.trim() || "";
  const marketSymbol = $("trading-grid-bot-market")?.value || "";
  const market = (tradingState.markets || []).find((row) => row.symbol === marketSymbol);
  const upper = Number($("trading-grid-upper-price")?.value || 0);
  const lower = Number($("trading-grid-lower-price")?.value || 0);
  const count = Number($("trading-grid-count")?.value || 10);
  const amount = Number($("trading-grid-order-amount")?.value || 0);
  const spacingMode = $("trading-grid-spacing-mode")?.value || "arithmetic";
  const stopLossPercent = tradingOptionalPercentValue("trading-grid-stop-loss-percent");
  const takeProfitPercent = tradingOptionalPercentValue("trading-grid-take-profit-percent");
  if (!name) { tradingSetMsg("請填寫機器人名稱", false); return; }
  if (!marketSymbol) { tradingSetMsg("請選擇交易市場", false); return; }
  if (market && market.allow_bots === false) {
    tradingSetMsg("這個市場目前未開放網格機器人，請改選其他市場或請 root 開啟 allow_bots。", false);
    return;
  }
  if (!upper || !lower || upper <= lower) { tradingSetMsg("上限價格必須大於下限價格", false); return; }
  if (count < 2) { tradingSetMsg("網格數量至少為 2", false); return; }
  if (amount < 1) { tradingSetMsg("每格金額必須大於 0", false); return; }
  const preview = await renderGridBotPreview({ quiet: false });
  if (!preview || !preview.ok) return;
  const risk = preview.risk || {};
  if (risk.blocked || risk.status === "red") {
    tradingSetMsg(risk.message || "目前網格設定扣費後預期虧損，請加大間距或減少網格數", false);
    return;
  }
  let confirmThinProfit = false;
  if (risk.requires_confirmation) {
    const ok = confirm(`${risk.message || "此網格利潤過薄。"}\n\n若仍要建立，請確認你接受可能被滑價吃掉的風險。`);
    if (!ok) {
      tradingSetMsg("已取消建立網格", false);
      return;
    }
    confirmThinProfit = true;
  }
  const btn = $("trading-grid-bot-create-btn");
  if (btn) btn.disabled = true;
  const formData = {
    name,
    market_symbol: marketSymbol,
    upper_price_points: upper,
    lower_price_points: lower,
	    grid_count: count,
	    order_amount_points: amount,
	    stop_loss_percent: stopLossPercent,
	    take_profit_percent: takeProfitPercent,
	    spacing_mode: spacingMode,
	    share_parameters: !!$("trading-grid-share-parameters")?.checked,
	    confirm_thin_profit: confirmThinProfit,
	  };
  const situation = computeGridSpotSituation(marketSymbol, lower, upper, count, amount, spacingMode);
  if (situation) {
    showGridSpotConfirm(situation, formData);
  } else {
    await doCreateGridBot(formData);
  }
}

async function scanGridBots() {
  tradingSetMsg("掃描網格機器人中...");
  const btn = $("trading-grid-scan-btn");
  if (btn) btn.disabled = true;
  try {
    const csrf = await tradingFreshCsrfToken();
    const res = await apiFetch(`${API}/trading/grid-bots/scan`, {
      method: "POST", credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) { tradingSetMsg(tradingFriendlyErrorText(json.msg || "掃描失敗"), false); return; }
    const results = json.results || [];
    const fills = results.reduce((s, r) => s + (r.fills_processed || []).length, 0);
    const placed = results.reduce((s, r) => s + (r.counter_orders_placed || []).length, 0);
    tradingSetMsg(`網格掃描完成：${json.scanned} 個機器人，處理 ${fills} 筆成交，新掛 ${placed} 筆反向單`);
    await loadGridBots();
    await loadTradingDashboard();
  } catch (e) { tradingSetMsg(e.message || "掃描失敗", false); }
  finally { if (btn) btn.disabled = false; }
}

// ── End Grid Trading Bot ────────────────────────────────────────────────────


async function saveTradingBot() {
  const marketSymbol = $("trading-auto-bot-market")?.value || selectedTradingMarket()?.symbol || "";
  if (!marketSymbol) {
    tradingSetMsg("請先選擇自動化機器人市場", false);
    return;
  }
  const market = (tradingState.markets || []).find((row) => row.symbol === marketSymbol);
  if (market && market.allow_bots === false) {
    tradingSetMsg("這個市場目前未開放 Workflow / 自動化機器人，請改選其他市場或請 root 開啟 allow_bots。", false);
    return;
  }
  let workflow;
  try {
    workflow = parseTradingWorkflowInput();
  } catch (err) {
    tradingSetMsg(err.message || "Workflow JSON 格式錯誤", false);
    return;
  }
  const payload = {
    bot_type: "conditional",
    name: $("trading-auto-bot-name")?.value || "",
    market_symbol: marketSymbol,
    trigger_type: "always",
    trigger_price_points: null,
    side: "buy",
    order_type: "market",
    quantity: "0.00000001",
    limit_price_points: null,
    budget_points: Number($("trading-auto-bot-budget-points")?.value || 0),
    workflow_json: workflow,
    max_daily_runs: Number($("trading-auto-daily-runs")?.value || 5),
    max_runs: Number($("trading-auto-bot-max-runs")?.value || 1),
    cooldown_seconds: Number($("trading-auto-bot-cooldown")?.value || 300),
	    share_parameters: !!$("trading-auto-share-parameters")?.checked,
	    enabled: !!$("trading-auto-bot-enabled")?.checked,
	  };
  try {
    tradingSetMsg("正在新增自動化機器人...");
    await fetchTradingJson("/trading/bots", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    tradingSetMsg("自動化條件機器人已新增");
    if ($("trading-auto-bot-name")) $("trading-auto-bot-name").value = "";
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(`自動化機器人新增失敗：${tradingFriendlyErrorText(err.message || "後端未提供錯誤原因")}`, false);
  }
}

async function saveTradingDcaBot() {
  const marketSymbol = $("trading-dca-bot-market")?.value || selectedTradingMarket()?.symbol || "";
  if (!marketSymbol) {
    tradingSetMsg("請先選擇定投市場", false);
    return;
  }
  const market = (tradingState.markets || []).find((row) => row.symbol === marketSymbol);
  if (market && market.allow_bots === false) {
    tradingSetMsg("這個市場目前未開放定投 / 機器人，請改選其他市場或請 root 開啟 allow_bots。", false);
    return;
  }
  const preset = $("trading-dca-bot-interval-preset")?.value || "24";
  const intervalHours = preset === "custom" ? Number($("trading-dca-bot-interval-hours")?.value || 24) : Number(preset || 24);
  const payload = {
    bot_type: "dca",
    name: $("trading-dca-bot-name")?.value || "",
    market_symbol: marketSymbol,
    budget_points: Number($("trading-dca-bot-budget-points")?.value || 0),
    interval_hours: intervalHours,
    price_upper_limit: Number($("trading-dca-price-upper")?.value || 0) || null,
    price_lower_limit: Number($("trading-dca-price-lower")?.value || 0) || null,
    max_runs: Number($("trading-dca-bot-max-runs")?.value || 1),
	    stop_loss_percent: tradingOptionalPercentValue("trading-dca-stop-loss-percent"),
	    take_profit_percent: tradingOptionalPercentValue("trading-dca-take-profit-percent"),
	    share_parameters: !!$("trading-dca-share-parameters")?.checked,
	    enabled: !!$("trading-dca-bot-enabled")?.checked,
	  };
  try {
    tradingSetMsg("正在新增定投機器人...");
    const json = await fetchTradingJson("/trading/bots", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const initial = json.initial_run || null;
    const failed = Array.isArray(initial?.failed) ? initial.failed : [];
    const triggered = Array.isArray(initial?.triggered) ? initial.triggered : [];
    const skipped = Array.isArray(initial?.skipped) ? initial.skipped : [];
    if (!payload.enabled) {
      tradingSetMsg("定投機器人已新增（目前停用，未立即執行）");
    } else if (failed.length) {
      tradingSetMsg(`定投機器人已新增，但首次執行失敗：${tradingErrorText(failed[0], "後端未提供錯誤原因")}`, false);
    } else if (triggered.length) {
      tradingSetMsg("定投機器人已新增，已立即執行第一筆");
    } else if (skipped.length) {
      tradingSetMsg(`定投機器人已新增，但首次執行被略過：${sanitize(skipped[0].reason || "未符合條件")}`, false);
    } else {
      tradingSetMsg("定投機器人已新增，等待下一次掃描");
    }
    if ($("trading-dca-bot-name")) $("trading-dca-bot-name").value = "";
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(`定投機器人新增失敗：${tradingFriendlyErrorText(err.message || "後端未提供錯誤原因")}`, false);
  }
}

function downloadBacktestTrades(trades, marketSymbol) {
  if (!trades || !trades.length) return;
  const header = "時間,方向,數量,價格（點）,金額（點）,手續費（點）";
  const rows = trades.map((r) => [
    `"${String(r.time || "").replace(/"/g, '""')}"`,
    r.side === "sell" ? "賣出" : "買入",
    r.quantity || "0",
    r.price_points || 0,
    r.spend_points || 0,
    r.fee_points || 0,
  ].join(","));
  const csv = [header, ...rows].join("\n");
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `backtest_${(marketSymbol || "trades").replace(/\//g, "-")}_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
}

function tradingCandlesCoverRange(candles, startTime = "", endTime = "") {
  const rows = Array.isArray(candles) ? candles : [];
  if (rows.length < 2) return false;
  if (!startTime && !endTime) return true;
  const first = String(rows[0]?.time_iso || rows[0]?.time || "");
  const last = String(rows[rows.length - 1]?.time_iso || rows[rows.length - 1]?.time || "");
  if (startTime && first && startTime < first) return false;
  if (endTime && last && endTime > last) return false;
  return true;
}


function tradingRiskTargetText(stopLossPercent, takeProfitPercent) {
  const parts = [];
  if (Number(stopLossPercent || 0) > 0) parts.push(`停損 ${formatTradingPointsValue(stopLossPercent)}%`);
  if (Number(takeProfitPercent || 0) > 0) parts.push(`停利 ${formatTradingPointsValue(takeProfitPercent)}%`);
  return parts.length ? parts.join(" · ") : "未設定停損 / 停利";
}

async function backtestTradingBot(contextKey = "dca") {
  const cfg = tradingBacktestConfig(contextKey);
  const result = tradingBacktestEl(contextKey, "result");
  const marketSymbol = tradingBacktestEl(contextKey, "market")?.value || selectedTradingMarket()?.symbol || "";
  const botUuid = tradingBacktestEl(contextKey, "bot-select")?.value || "";
  const selectedGridBot = contextKey === "grid" && botUuid ? (tradingGridBots || []).find((g) => g.bot_uuid === botUuid) : null;
  const selectedBot = contextKey !== "grid" && botUuid ? (tradingState.bots || []).find((row) => row.bot_uuid === botUuid) : null;
  const botType = cfg.botType;
  let allCandles = tradingState.referencePrices?.candles || tradingState.referencePrices?.points || [];
  if (!marketSymbol) {
    tradingSetMsg("請先選擇回測市場", false);
    return;
  }

  let gridParams = {};
  if (botType === "grid") {
    const src = selectedGridBot || {};
    gridParams = {
      lower_price_points: Number(tradingBacktestEl(contextKey, "grid-lower")?.value || src.lower_price_points || 0),
      upper_price_points: Number(tradingBacktestEl(contextKey, "grid-upper")?.value || src.upper_price_points || 0),
      grid_count: Number(tradingBacktestEl(contextKey, "grid-count")?.value || src.grid_count || 10),
      order_amount_points: Number(tradingBacktestEl(contextKey, "grid-amount")?.value || src.order_amount_points || 100),
      spacing_mode: tradingBacktestEl(contextKey, "grid-spacing")?.value || src.spacing_mode || "arithmetic",
    };
    if (!gridParams.lower_price_points || !gridParams.upper_price_points || gridParams.upper_price_points <= gridParams.lower_price_points) {
      tradingSetMsg("請填寫正確的網格上下限價格（下限 < 上限）", false);
      return;
    }
  }

  let workflow = null;
  if (botType === "workflow") {
    try {
      workflow = selectedBot?.workflow || parseTradingWorkflowInput();
    } catch (err) {
      tradingSetMsg(err.message || "Workflow JSON 格式錯誤", false);
      return;
    }
  }

  const orderPoints = botType === "grid"
    ? 0
    : Number(tradingBacktestEl(contextKey, "order-points")?.value || 100);
  const intervalCandles = botType === "dca"
    ? Math.max(1, Number(tradingBacktestEl(contextKey, "interval-candles")?.value || 1))
    : 1;
  const riskTargetPayload = botType === "workflow" ? {} : {
    stop_loss_percent: tradingOptionalPercentValue(tradingBacktestEl(contextKey, "stop-loss-percent")),
    take_profit_percent: tradingOptionalPercentValue(tradingBacktestEl(contextKey, "take-profit-percent")),
  };
  const basePayload = {
    market_symbol: marketSymbol,
    strategy: botType,
    workflow_json: workflow,
    initial_cash_points: Number(tradingBacktestEl(contextKey, "initial-cash")?.value || 10000),
    order_points: orderPoints,
    interval_candles: intervalCandles,
    timeframe: tradingBacktestEl(contextKey, "timeframe")?.value || "15m",
    start_time: tradingBacktestEl(contextKey, "start")?.value || "",
    end_time: tradingBacktestEl(contextKey, "end")?.value || "",
    slippage_percent: Number(tradingBacktestEl(contextKey, "slippage-percent")?.value || 0),
    ...riskTargetPayload,
    ...gridParams,
  };
  const hasCandleData = Array.isArray(allCandles) && allCandles.length >= 2;
  const localRangeCovered = hasCandleData && tradingCandlesCoverRange(allCandles, basePayload.start_time, basePayload.end_time);
  if (!hasCandleData || !localRangeCovered) {
    const estimatedCandles = estimateBacktestRequestedCandles(basePayload.start_time, basePayload.end_time, basePayload.timeframe);
    if (estimatedCandles > BACKTEST_TOTAL_CANDLE_LIMIT) {
      tradingSetMsg(`回測區間約需 ${estimatedCandles.toLocaleString()} 根 K 線，超過單次上限 ${BACKTEST_TOTAL_CANDLE_LIMIT.toLocaleString()} 根。請縮小區間或改大時間週期。`, false);
      return;
    }
    basePayload.auto_fetch_reference_candles = true;
    basePayload.candle_limit = estimatedCandles || 500;
    tradingSetMsg(
      estimatedCandles
        ? `${hasCandleData ? "目前圖表不涵蓋你選的回測區間，" : "未載入圖表，"}正在由後端分批下載約 ${estimatedCandles.toLocaleString()} 根歷史 K 線後回測...`
        : `${hasCandleData ? "目前圖表不涵蓋你選的回測區間，" : "未載入圖表，"}正在由後端下載歷史 K 線後回測...`
    );
  } else {
    if (allCandles.length > BACKTEST_TOTAL_CANDLE_LIMIT) {
      tradingSetMsg(`目前單次回測最多 ${BACKTEST_TOTAL_CANDLE_LIMIT.toLocaleString()} 根 K 線；你目前載入了 ${allCandles.length.toLocaleString()} 根。請縮小圖表區間或改大時間週期。`, false);
      return;
    }
    basePayload.data_source = tradingState.referencePrices?.source || "browser_loaded_chart";
    basePayload.provider_symbol = tradingState.referencePrices?.symbol || "";
  }
  try {
    if (hasCandleData) basePayload.candles = allCandles;
    if (result) result.textContent = "回測中…";
    const combinedJson = await fetchTradingJson("/trading/bots/backtest", { method: "POST", body: JSON.stringify(basePayload) });
    const sourceText = combinedJson.data_source ? `，資料 ${sanitize(combinedJson.data_source)} ${Number(combinedJson.candle_count || 0)} 根` : "";
    const batchNote = combinedJson.segmented_backtest
      ? `（後端自動分 ${Number(combinedJson.segmented_backtest_batches || 0)} 批）`
      : "";
    const text = `回測完成${batchNote}：交易 ${Number(combinedJson.trade_count || 0)} 次，期末 ${formatTradingPointsValue(combinedJson.final_value_points)} 點，損益 ${Number(combinedJson.pnl_points || 0) >= 0 ? "+" : ""}${formatTradingPointsValue(combinedJson.pnl_points)} 點，報酬 ${formatTradingPointsValue(combinedJson.return_percent)}%${sourceText}`;
    if (result) result.textContent = text;
    renderTradingBacktestResult(combinedJson, contextKey);
    tradingSetMsg(text, Number(combinedJson.pnl_points || 0) >= 0);
  } catch (err) {
    const text = tradingFriendlyErrorText(err.message || "回測失敗");
    if (result) result.textContent = text;
    tradingSetMsg(text, false);
  }
}

function renderTradingBacktestResult(json, contextKey = "dca") {
  const metrics = tradingBacktestEl(contextKey, "metrics");
  const trades = tradingBacktestEl(contextKey, "trades");
  const warnings = tradingBacktestEl(contextKey, "warnings");
  if (warnings) {
    const rangeWarns = Array.isArray(json.range_warnings) ? json.range_warnings : [];
    warnings.innerHTML = rangeWarns.length
      ? rangeWarns.map((w) => `<div class="trading-backtest-warning">⚠ ${sanitize(w)}</div>`).join("")
      : "";
    warnings.style.display = rangeWarns.length ? "" : "none";
  }
  if (metrics) {
    metrics.innerHTML = `
      <div><span class="drive-card-sub">初始資金</span><strong>${formatTradingPointsValue(json.initial_cash_points)}</strong><small>點</small></div>
      <div><span class="drive-card-sub">最終資金</span><strong>${formatTradingPointsValue(json.final_value_points)}</strong><small>點</small></div>
      <div><span class="drive-card-sub">總損益</span><strong>${Number(json.pnl_points || 0) >= 0 ? "+" : ""}${formatTradingPointsValue(json.pnl_points)}</strong><small>${formatTradingPointsValue(json.return_percent)}%</small></div>
      <div><span class="drive-card-sub">交易次數</span><strong>${Number(json.trade_count || 0)}</strong><small>回測未修改帳本</small></div>
      <div><span class="drive-card-sub">資料來源</span><strong>${sanitize(json.data_source || "-")}</strong><small>${Number(json.candle_count || 0)} 根 K 線</small></div>
      <div><span class="drive-card-sub">資料範圍</span><strong>${sanitize(String(json.first_candle_time || "-"))}</strong><small>～ ${sanitize(String(json.last_candle_time || "-"))}</small></div>
      <div><span class="drive-card-sub">回測上限</span><strong>${Number(json.max_backtest_candles || 0).toLocaleString()} 根</strong><small>單批最多 ${Number(json.max_backtest_candles_per_batch || json.max_backtest_candles || 0).toLocaleString()} 根 · 已使用 ${Number(json.candle_count || 0).toLocaleString()} 根</small></div>
    `;
  }
  if (trades) {
    const rows = Array.isArray(json.trades) ? json.trades : [];
    const dlBtn = rows.length ? `<button class="btn btn-sm" type="button" data-backtest-download="${sanitize(contextKey)}" style="margin-bottom:.5rem;">下載成交記錄 CSV（${rows.length} 筆）</button>` : "";
    trades.innerHTML = dlBtn + (rows.length ? rows.map((row) => `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(row.time || "-")}</strong>
          <div class="drive-card-sub">${sanitize(row.side === "sell" ? "賣出" : "買入")} ${sanitize(row.quantity || "0")} · 價格 ${formatTradingPointsValue(row.price_points)} · 金額 ${formatTradingPointsValue(row.spend_points)} · 手續費 ${formatTradingPointsValue(row.fee_points)}</div>
        </div>
      </div>
    `).join("") : `<div class="drive-empty">回測期間沒有交易</div>`);
    if (rows.length) {
      const dlBtnEl = trades.querySelector(`[data-backtest-download="${CSS.escape(contextKey)}"]`);
      if (dlBtnEl) dlBtnEl.addEventListener("click", () => downloadBacktestTrades(rows, json.market_symbol));
    }
  }
}

function refreshBacktestBotSelect() {
  refreshBacktestBotSelects();
}

function refreshBacktestBotSelects() {
  const optionsByContext = {
    dca: (tradingState.bots || []).filter((row) => row.bot_type === "dca").map((row) =>
      `<option value="${sanitize(row.bot_uuid || "")}">${sanitize(`定投 · ${row.name || row.market_symbol || ""}`)}</option>`
    ).join(""),
    workflow: (tradingState.bots || []).filter((row) => row.bot_type !== "dca").map((row) =>
      `<option value="${sanitize(row.bot_uuid || "")}">${sanitize(`Workflow · ${row.name || row.market_symbol || ""}`)}</option>`
    ).join(""),
    grid: (tradingGridBots || []).map((bot) =>
      `<option value="${sanitize(bot.bot_uuid || "")}">${sanitize(`網格 · ${bot.name || bot.market_symbol || ""}`)}</option>`
    ).join(""),
  };
  Object.keys(TRADING_BACKTEST_CONTEXTS).forEach((contextKey) => {
    const sel = tradingBacktestEl(contextKey, "bot-select");
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = `<option value="">使用目前表單設定</option>${optionsByContext[contextKey] || ""}`;
    if (prev && Array.from(sel.options).some((o) => o.value === prev)) sel.value = prev;
  });
}

function prepareTradingBacktestFromBot(botUuid) {
  const gridBot = (tradingGridBots || []).find((g) => g.bot_uuid === botUuid);
  if (gridBot) {
    switchTradingBotTab("grid");
    if (tradingBacktestEl("grid", "bot-select")) tradingBacktestEl("grid", "bot-select").value = botUuid;
    if (tradingBacktestEl("grid", "market")) tradingBacktestEl("grid", "market").value = gridBot.market_symbol || "";
    if (tradingBacktestEl("grid", "grid-lower")) tradingBacktestEl("grid", "grid-lower").value = gridBot.lower_price_points || "";
    if (tradingBacktestEl("grid", "grid-upper")) tradingBacktestEl("grid", "grid-upper").value = gridBot.upper_price_points || "";
    if (tradingBacktestEl("grid", "grid-count")) tradingBacktestEl("grid", "grid-count").value = gridBot.grid_count || 10;
    if (tradingBacktestEl("grid", "grid-amount")) tradingBacktestEl("grid", "grid-amount").value = gridBot.order_amount_points || 100;
    if (tradingBacktestEl("grid", "grid-spacing")) tradingBacktestEl("grid", "grid-spacing").value = gridBot.spacing_mode || "arithmetic";
    if (tradingBacktestEl("grid", "stop-loss-percent")) tradingBacktestEl("grid", "stop-loss-percent").value = gridBot.stop_loss_percent || "";
    if (tradingBacktestEl("grid", "take-profit-percent")) tradingBacktestEl("grid", "take-profit-percent").value = gridBot.take_profit_percent || "";
    updateBacktestDateRangeGuidance("grid");
    tradingSetMsg("已帶入網格機器人回測設定，請確認時間範圍後執行回測");
    return;
  }
  const bot = (tradingState.bots || []).find((row) => row.bot_uuid === botUuid);
  if (!bot) {
    tradingSetMsg("找不到要回測的交易機器人", false);
    return;
  }
  const contextKey = bot.bot_type === "dca" ? "dca" : "workflow";
  switchTradingBotTab(tradingBacktestConfig(contextKey).tab);
  if (tradingBacktestEl(contextKey, "bot-select")) tradingBacktestEl(contextKey, "bot-select").value = botUuid;
  if (tradingBacktestEl(contextKey, "market")) tradingBacktestEl(contextKey, "market").value = bot.market_symbol || "";
  if (contextKey === "dca") {
    if (tradingBacktestEl("dca", "order-points")) tradingBacktestEl("dca", "order-points").value = bot.budget_points || 100;
    if (tradingBacktestEl("dca", "stop-loss-percent")) tradingBacktestEl("dca", "stop-loss-percent").value = bot.stop_loss_percent || "";
    if (tradingBacktestEl("dca", "take-profit-percent")) tradingBacktestEl("dca", "take-profit-percent").value = bot.take_profit_percent || "";
    const timeframe = tradingBacktestEl("dca", "timeframe")?.value || "15m";
    const hoursPerCandle = tradingTimeframeMinutes(timeframe) / 60;
    if (tradingBacktestEl("dca", "interval-candles")) {
      tradingBacktestEl("dca", "interval-candles").value = Math.max(1, Math.ceil(Number(bot.interval_hours || 1) / Math.max(hoursPerCandle, 1 / 12)));
    }
    updateBacktestDateRangeGuidance("dca");
  } else {
    if (Number(bot.budget_points || 0) > 0 && tradingBacktestEl("workflow", "order-points")) {
      tradingBacktestEl("workflow", "order-points").value = bot.budget_points || 100;
    }
    updateBacktestDateRangeGuidance("workflow");
  }
  tradingSetMsg("已帶入機器人回測設定，請確認時間範圍後執行回測");
}

async function deleteTradingBot(botUuid) {
  if (!botUuid) {
    tradingSetMsg("找不到要刪除的交易機器人", false);
    return;
  }
  if (!confirm("確定刪除這個交易機器人？")) {
    tradingSetMsg("已取消刪除交易機器人");
    return;
  }
  try {
    tradingSetMsg("正在刪除交易機器人...");
    await fetchTradingJson(`/trading/bots/${encodeURIComponent(botUuid)}`, {
      method: "DELETE",
      body: JSON.stringify({}),
    });
    tradingSetMsg("交易機器人已刪除");
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "交易機器人刪除失敗", false);
  }
}

async function setTradingBotParameterShare(botUuid, shareParameters) {
  if (!botUuid) {
    tradingSetMsg("找不到要更新的交易機器人", false);
    return;
  }
  await fetchTradingJson(`/trading/bots/${encodeURIComponent(botUuid)}/share`, {
    method: "POST",
    body: JSON.stringify({ share_parameters: !!shareParameters }),
  });
  tradingSetMsg(shareParameters ? "已在競賽中分享參數" : "已停止分享參數");
  await loadTradingDashboard();
  await loadTradingBotCompetition();
}

async function setGridBotParameterShare(botUuid, shareParameters) {
  if (!botUuid) {
    tradingSetMsg("找不到要更新的網格機器人", false);
    return;
  }
  await fetchTradingJson(`/trading/grid-bots/${encodeURIComponent(botUuid)}/share`, {
    method: "POST",
    body: JSON.stringify({ share_parameters: !!shareParameters }),
  });
  tradingSetMsg(shareParameters ? "已在競賽中分享網格參數" : "已停止分享網格參數");
  await loadGridBots();
  await loadTradingBotCompetition();
}

async function toggleTradingBot(botUuid, enabled) {
  const bot = tradingState.bots.find((row) => row.bot_uuid === botUuid);
  if (!bot) {
    tradingSetMsg("找不到要更新的交易機器人", false);
    return;
  }
  const payload = {
    bot_type: bot.bot_type || "conditional",
    name: bot.name || "",
    market_symbol: bot.market_symbol,
    side: bot.side || "buy",
    order_type: bot.order_type || "market",
    quantity: bot.quantity_text || "0.00000001",
    limit_price_points: bot.limit_price_points || null,
    trigger_type: bot.trigger_type || "always",
    trigger_price_points: bot.trigger_price_points || null,
    budget_points: Number(bot.budget_points || 0),
	    interval_hours: Number(bot.interval_hours || 24),
	    max_runs: Number(bot.max_runs ?? 1),
	    cooldown_seconds: Number(bot.cooldown_seconds || 0),
	    stop_loss_percent: bot.stop_loss_percent ?? null,
	    take_profit_percent: bot.take_profit_percent ?? null,
	    share_parameters: !!bot.share_parameters,
	    workflow_json: bot.workflow || null,
	    enabled,
	  };
  try {
    tradingSetMsg(enabled ? "正在啟用機器人..." : "正在暫停機器人...");
    const json = await fetchTradingJson(`/trading/bots/${encodeURIComponent(botUuid)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    if (!enabled) {
      tradingSetMsg("機器人已暫停");
    } else if (bot.bot_type === "dca") {
      const initial = json.initial_run || null;
      const failed   = Array.isArray(initial?.failed)   ? initial.failed   : [];
      const triggered = Array.isArray(initial?.triggered) ? initial.triggered : [];
      const skipped  = Array.isArray(initial?.skipped)  ? initial.skipped  : [];
      if (failed.length) {
        tradingSetMsg(`機器人已啟用，但首次執行失敗：${tradingErrorText(failed[0], "後端未提供錯誤原因")}`, false);
      } else if (triggered.length) {
        tradingSetMsg("機器人已啟用，已立即執行第一筆定投");
      } else if (skipped.length && skipped[0]?.reason === "cooldown") {
        tradingSetMsg("機器人已啟用，定投冷卻中，將依排程執行下一筆");
      } else {
        tradingSetMsg("機器人已啟用");
      }
    } else {
      tradingSetMsg("機器人已啟用");
    }
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "機器人狀態更新失敗", false);
  }
}

async function increaseTradingBotMaxRuns(botUuid) {
  const bot = (tradingState.bots || []).find((row) => row.bot_uuid === botUuid);
  if (!bot) {
    tradingSetMsg("找不到要增加次數的交易機器人", false);
    return;
  }
  if (tradingBotMaxRunsValue(bot) === -1) {
    tradingSetMsg("這個定投機器人目前是不限制執行次數，不需要再增加上限");
    return;
  }
  const raw = window.prompt(`目前 ${Number(bot.run_count || 0)} / ${tradingBotMaxRunsLabel(bot)} 次。\n要再增加幾次？`, "1");
  if (raw == null) {
    tradingSetMsg("已取消增加機器人次數");
    return;
  }
  const delta = Number(raw);
  if (!Number.isInteger(delta) || delta <= 0) {
    tradingSetMsg("請輸入大於 0 的整數次數", false);
    return;
  }
  const json = await fetchTradingJson(`/trading/bots/${encodeURIComponent(botUuid)}/increase-runs`, {
    method: "POST",
    body: JSON.stringify({ delta }),
  });
  const nextLimitLabel = tradingBotMaxRunsLabel(json?.bot || {});
  tradingSetMsg(`已增加 ${delta} 次，新的最大交易次數為 ${nextLimitLabel === "不限制" ? "不限制" : `${nextLimitLabel} 次`}`);
  await loadTradingDashboard();
}

async function adjustTradingBotBudget(botUuid) {
  const bot = (tradingState.bots || []).find((row) => row.bot_uuid === botUuid);
  if (!bot) {
    tradingSetMsg("找不到要調整可用上限的交易機器人", false);
    return;
  }
  const current = Number(bot.budget_points || 0);
  const floor = Number(bot.minimum_budget_points || bot.open_order_frozen_points || 0);
  const raw = window.prompt(
    `目前 ${tradingBotBudgetText(bot)}。\n請輸入新的可用上限點數；0 代表不設上限。若設定上限，最低不能低於已掛單凍結 ${formatTradingPointsValue(floor)} 點。`,
    String(current)
  );
  if (raw == null) {
    tradingSetMsg("已取消調整機器人可用上限");
    return;
  }
  const budgetPoints = Number(raw);
  if (!Number.isInteger(budgetPoints) || budgetPoints < 0) {
    tradingSetMsg("請輸入 0 或大於 0 的整數點數", false);
    return;
  }
  if (budgetPoints !== 0 && budgetPoints < floor) {
    tradingSetMsg(`可用上限不能低於已掛單凍結 ${formatTradingPointsValue(floor)} 點；若要取消上限請填 0`, false);
    return;
  }
  const json = await fetchTradingJson(`/trading/bots/${encodeURIComponent(botUuid)}/budget`, {
    method: "POST",
    body: JSON.stringify({ budget_points: budgetPoints }),
  });
  tradingSetMsg(`已更新機器人可用上限：${tradingBotBudgetText(json?.bot || { budget_points: budgetPoints })}`);
  await loadTradingDashboard();
}

async function scanTradingBots() {
  try {
    tradingSetMsg("正在掃描已啟用交易機器人...");
    const json = await fetchTradingJson("/trading/bots/scan", {
      method: "POST",
      body: JSON.stringify({ limit: 50 }),
    });
    const triggered = Array.isArray(json.triggered) ? json.triggered.length : 0;
    const failed = Array.isArray(json.failed) ? json.failed.length : 0;
    tradingSetMsg(`機器人掃描完成：掃描 ${Number(json.scanned || 0)} 個，觸發 ${triggered} 個，失敗 ${failed} 個`, failed === 0);
    await loadTradingDashboard();
  } catch (err) {
    tradingSetMsg(err.message || "交易機器人掃描失敗", false);
  }
}

async function toggleGridBot(uuid, enable) {
  if (!uuid) {
    tradingSetMsg("找不到要更新的網格機器人", false);
    return;
  }
  const csrf = await tradingFreshCsrfToken();
  const res = await apiFetch(`${API}/trading/grid-bots/${encodeURIComponent(uuid)}/toggle`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ enabled: !!enable }),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(tradingFriendlyErrorText(json.msg || "網格機器人狀態更新失敗"));
  tradingSetMsg(enable ? "網格機器人已啟用" : "網格機器人已暫停");
  await loadGridBots();
  await loadTradingDashboard();
}
