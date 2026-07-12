'use strict';

let economyExplorerLastQuery = "";

let economyExplorerActiveLayer = "pc1";

let economyExplorerCountdownTimer = null;

const ECONOMY_EXPLORER_LAYERS = {
  pc1: {
    title: "PC1 Canonical Settlement Layer",
    shortTitle: "Settlement Explorer",
    assetType: "Canonical Reserve Asset",
    description: "只看 canonical reserve、冷鏈交易、封塊、鏈上 fee、mint/burn 與治理結算。",
    placeholder: "pc1... / cold tx hash / block hash / block height",
  },
  pc0: {
    title: "PC0 Operational Wrapped Layer",
    shortTitle: "Operational Explorer",
    assetType: "Wrapped Operational Representation",
    description: "站內託管錢包、交易所、機器人、遊戲與服務付款等高速 operational 記帳；不是 PC1 原生資產。",
    placeholder: "pc0... / internal ledger uuid / app tx hash",
  },
  bridge: {
    title: "Bridge Cross-Ledger Settlement Layer",
    shortTitle: "Bridge Explorer",
    assetType: "Cross-Ledger Settlement Event",
    description: "查 lock/mint、burn/unlock、deposit credit 等跨帳本事件與 reserve/wrapped linkage。",
    placeholder: "bridge uuid / chain tx hash / internal ledger uuid",
  },
  audit: {
    title: "Audit & Reserve Invariants",
    shortTitle: "Audit Explorer",
    assetType: "Reserve / Liability Audit",
    description: "查 reserve、wrapped liabilities、pending settlement、bridge invariant 與 PC0/PC1 boundary。",
    placeholder: "Audit 分頁可直接按查詢載入 invariant",
  },
};

function economyExplorerSecondsText(seconds) {
  const value = Math.max(0, Number(seconds || 0));
  if (value <= 0) return "已達成";
  if (value < 60) return `${value} 秒`;
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
}

function economyExplorerSecondsRangeText(minSeconds, maxSeconds) {
  const min = Math.max(0, Math.round(Number(minSeconds || 0)));
  const max = Math.max(min, Math.round(Number(maxSeconds || min)));
  if (min === max) return economyExplorerSecondsText(min);
  return `${economyExplorerSecondsText(min)}-${economyExplorerSecondsText(max)}`;
}

function economyExplorerFeeEstimateFromDataset(root, extraFeePoints = 0) {
  if (!root) return null;
  const baseMin = Number(root.dataset.baseSecondsMin || 120);
  const baseMax = Number(root.dataset.baseSecondsMax || 180);
  const floorMin = Number(root.dataset.minimumSecondsMin || 30);
  const floorMax = Number(root.dataset.minimumSecondsMax || 45);
  const reference = Math.max(1, Number(root.dataset.feeReferencePoints || 20));
  const currentFee = Math.max(0, Number(root.dataset.currentFeePoints || 0));
  const fee = Math.max(0, currentFee + Math.floor(Number(extraFeePoints || 0)));
  const ratio = fee > 0 ? fee / (fee + reference) : 0;
  const min = Math.round(baseMin - (baseMin - floorMin) * ratio);
  const max = Math.round(baseMax - (baseMax - floorMax) * ratio);
  return {
    fee,
    min,
    max: Math.max(max, min + 15),
    ratio,
  };
}

function updateEconomyExplorerAccelerateEstimate() {
  const root = document.querySelector("#economy-explorer-result .economy-explorer-accelerate");
  const input = $("economy-explorer-accelerate-fee");
  const target = $("economy-explorer-accelerate-estimate");
  if (!root || !input || !target) return;
  const extraFee = Number(input.value || 0);
  if (!Number.isFinite(extraFee) || extraFee <= 0) {
    target.textContent = "輸入鏈上費用後顯示預估 Proved 時間";
    return;
  }
  const current = economyExplorerFeeEstimateFromDataset(root, 0);
  const next = economyExplorerFeeEstimateFromDataset(root, extraFee);
  if (!current || !next) return;
  target.textContent = `預估 Proved ${economyExplorerSecondsRangeText(next.min, next.max)}；目前 ${economyExplorerSecondsRangeText(current.min, current.max)}；總費用 ${formatEconomyPointsValue(next.fee)} 點`;
}

let economyTransferFeeEstimateTimer = null;

function resetEconomyExplorerAccountState() {
  if (economyExplorerCountdownTimer) clearInterval(economyExplorerCountdownTimer);
  if (economyTransferFeeEstimateTimer) clearTimeout(economyTransferFeeEstimateTimer);
  economyExplorerCountdownTimer = null;
  economyTransferFeeEstimateTimer = null;
  economyExplorerLastQuery = "";
  economyExplorerActiveLayer = "pc1";
  ["economy-explorer-query", "economy-explorer-accelerate-fee"].forEach((id) => {
    const input = $(id);
    if (input) input.value = "";
  });
  ["economy-explorer-result", "economy-explorer-accelerate-estimate", "economy-transfer-fee-estimate"].forEach((id) => {
    const el = $(id);
    if (el) el.replaceChildren();
  });
  const msg = $("economy-explorer-msg");
  if (msg) {
    msg.textContent = "";
    msg.className = "msg";
  }
  document.querySelectorAll("[data-economy-explorer-layer]").forEach((btn) => {
    const active = btn.dataset.economyExplorerLayer === "pc1";
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
}

window.resetEconomyExplorerAccountState = resetEconomyExplorerAccountState;

function scheduleEconomyTransferFeeEstimate() {
  if (economyTransferFeeEstimateTimer) clearTimeout(economyTransferFeeEstimateTimer);
  const operation = economyOperationContext();
  economyTransferFeeEstimateTimer = setTimeout(() => {
    economyTransferFeeEstimateTimer = null;
    if (economyOperationIsCurrent(operation)) loadEconomyTransferFeeEstimate(operation);
  }, 250);
}

async function loadEconomyTransferFeeEstimate(operation = null) {
  const operationContext = operation || economyOperationContext();
  economyAssertOperationCurrent(operationContext);
  const target = $("economy-transfer-fee-estimate");
  if (!target) return;
  const feeInput = $("economy-transfer-fee");
  if (feeInput?.disabled) {
    target.textContent = "pc0 站內互轉不收鏈上 fee、不等待 Proved。";
    return;
  }
  if (!economyChainEnabled()) {
    target.textContent = "PointsChain 私有鏈已停用";
    return;
  }
  const fee = Math.max(0, Math.floor(Number(feeInput?.value || 0)));
  if (!Number.isFinite(fee)) {
    target.textContent = "請輸入鏈上費用以估算 Proved 時間";
    return;
  }
  try {
    const json = await fetchEconomyJson(`/points/explorer/fee-estimate?fee_points=${encodeURIComponent(String(fee))}`);
    economyAssertOperationCurrent(operationContext);
    const estimate = json.estimate || {};
    const network = estimate.network_fee_state || {};
    const label = network.congestion_label || "idle";
    const suggested = Number(network.suggested_total_fee_points || 0);
    target.textContent = `預估 Proved ${economyExplorerSecondsRangeText(estimate.estimated_seconds_min, estimate.estimated_seconds_max)} · 鏈上 ${label} · 建議費用 ${formatEconomyPointsValue(suggested)} 點`;
  } catch (err) {
    if (!economyOperationIsCurrent(operationContext) || err?.name === "AbortError") return;
    target.textContent = err.message || "預估 Proved 時間讀取失敗";
  }
}

function economyExplorerLayerMeta(layer = economyExplorerActiveLayer) {
  return ECONOMY_EXPLORER_LAYERS[layer] || ECONOMY_EXPLORER_LAYERS.pc1;
}

function economyExplorerInferLayerFromTransaction(tx = {}) {
  if (tx.layer) return String(tx.layer);
  const flow = tx.wallet_flow && typeof tx.wallet_flow === "object" ? tx.wallet_flow : {};
  const rail = String(tx.settlement_rail || flow.settlement_rail || tx.input_data?.settlement_rail || "");
  if (["internal_hot_wallet", "internal_system_burn", "deposit_bridge_credit", "withdrawal_bridge_lock", "withdrawal_bridge_refund"].includes(rail)) return "pc0";
  if (rail.startsWith("deposit_bridge") || rail.startsWith("withdrawal_bridge")) return "bridge";
  if (economyIsPc0Address(flow.source_wallet_address) || economyIsPc0Address(flow.destination_wallet_address)) return "pc0";
  return "pc1";
}

function economyExplorerInferLayer(result = {}) {
  if (result.layer) return String(result.layer);
  if (result.kind === "bridge") return "bridge";
  if (result.kind === "block") return "pc1";
  if (result.kind === "transaction") return economyExplorerInferLayerFromTransaction(result.transaction || {});
  if (result.kind === "wallet") {
    const wallet = result.wallet || {};
    if (wallet.layer) return String(wallet.layer);
    if (economyIsPc0Address(wallet.address)) return "pc0";
    return "pc1";
  }
  return economyExplorerActiveLayer || "pc1";
}

function economyExplorerLayerBanner(result = null) {
  const layer = result ? economyExplorerInferLayer(result) : economyExplorerActiveLayer;
  const meta = economyExplorerLayerMeta(layer);
  const requestedMeta = economyExplorerLayerMeta(economyExplorerActiveLayer);
  const mismatch = result && layer !== economyExplorerActiveLayer && economyExplorerActiveLayer !== "audit"
    ? `<div class="drive-card-sub" style="margin-top:.35rem;color:#fbbf24;">目前結果屬於 ${sanitize(meta.shortTitle)}，不是目前選取的 ${sanitize(requestedMeta.shortTitle)}。Explorer 仍顯示結果以便追蹤 cross-reference。</div>`
    : "";
  const assetType = result?.asset_type || result?.transaction?.asset_type || result?.wallet?.asset_type || result?.bridge_event?.asset_type || meta.assetType;
  return `
    <div class="economy-explorer-layer-banner" data-layer="${sanitize(layer)}">
      <div class="drive-card-title">${sanitize(meta.title)}</div>
      <div class="drive-card-sub">Asset Type: ${sanitize(assetType)} · ${sanitize(meta.description)}</div>
      ${mismatch}
    </div>
  `;
}

function setEconomyExplorerLayer(layer, { reset = true } = {}) {
  const next = ECONOMY_EXPLORER_LAYERS[layer] ? layer : "pc1";
  economyExplorerActiveLayer = next;
  document.querySelectorAll("[data-economy-explorer-layer]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.economyExplorerLayer === next);
    btn.setAttribute("aria-selected", btn.dataset.economyExplorerLayer === next ? "true" : "false");
  });
  const desc = $("economy-explorer-layer-description");
  if (desc) desc.textContent = `${economyExplorerLayerMeta(next).title}：${economyExplorerLayerMeta(next).description}`;
  const input = $("economy-explorer-query");
  if (input) input.placeholder = economyExplorerLayerMeta(next).placeholder;
  if (reset) {
    stopEconomyExplorerCountdown();
    const wrap = $("economy-explorer-result");
    if (wrap) {
      if (next === "audit") {
        wrap.innerHTML = `${economyExplorerLayerBanner()}<div class="drive-empty">按查詢或重新查詢載入 reserve / liability invariant。</div>`;
      } else {
        wrap.innerHTML = `${economyExplorerLayerBanner()}<div class="drive-empty">輸入 ${sanitize(economyExplorerLayerMeta(next).shortTitle)} 查詢目標。</div>`;
      }
    }
  }
}

function stopEconomyExplorerCountdown() {
  if (economyExplorerCountdownTimer) {
    clearInterval(economyExplorerCountdownTimer);
    economyExplorerCountdownTimer = null;
  }
}

function startEconomyExplorerCountdown() {
  stopEconomyExplorerCountdown();
  const card = document.querySelector("#economy-explorer-result .economy-explorer-finality");
  if (!card) return;
  const status = String(card.dataset.finalityStatus || "");
  if (status !== "pending") return;
  let eta = Math.max(0, Number(card.dataset.etaSeconds || 0));
  let next = Math.max(0, Number(card.dataset.nextProofEtaSeconds || 0));
  const etaEl = card.querySelector("[data-finality-eta-text]");
  const nextEl = card.querySelector("[data-finality-next-text]");
  const query = economyExplorerLastQuery || $("economy-explorer-query")?.value || "";
  economyExplorerCountdownTimer = setInterval(() => {
    eta = Math.max(0, eta - 1);
    next = Math.max(0, next - 1);
    if (etaEl) etaEl.textContent = economyExplorerSecondsText(eta);
    if (nextEl) nextEl.textContent = economyExplorerSecondsText(next);
    if (next <= 0 || eta <= 0) {
      stopEconomyExplorerCountdown();
      if (query) searchEconomyExplorer(query);
    }
  }, 1000);
}

function economyExplorerIsInternalFinality(finality = {}) {
  return String(finality.finality_status || "") === "internal_settled"
    || String(finality.finality_simulation || "") === "internal_hot_wallet_ledger_v1"
    || Number(finality.target_proved_count ?? 20) === 0;
}

function economyExplorerFinalityCard(finality = {}) {
  const internalFinality = economyExplorerIsInternalFinality(finality);
  const target = internalFinality ? 0 : Number(finality.target_proved_count || 20);
  const proved = internalFinality ? 0 : Math.max(0, Math.min(target, Number(finality.proved_count || 0)));
  const percent = internalFinality ? 100 : (target > 0 ? Math.round((proved / target) * 100) : 0);
  const feePolicy = finality.chain_fee_policy && typeof finality.chain_fee_policy === "object" ? finality.chain_fee_policy : {};
  const network = finality.network_fee_state && typeof finality.network_fee_state === "object" ? finality.network_fee_state : {};
  const status = internalFinality
    ? "已站內入帳"
    : finality.finality_status === "failed"
    ? "未成交"
    : finality.finality_status === "sealed"
    ? "已封塊"
    : finality.finality_status === "proved"
      ? "已成交"
      : "等待 Proved";
  const eta = internalFinality ? "即時" : economyExplorerSecondsText(finality.eta_seconds || 0);
  const pending = !internalFinality && String(finality.finality_status || "") === "pending";
  const nextProofEta = pending
    ? ` · 下一個 Proved 約 <span data-finality-next-text>${sanitize(economyExplorerSecondsText(finality.next_proof_eta_seconds || 0))}</span>`
    : "";
  const accelerated = finality.acceleration_fee_paid_points
    ? ` · 加速費 ${formatEconomyPointsValue(finality.acceleration_fee_paid_points)} 點 → ${finality.acceleration_fee_destination_label || "BURN 銷毀錢包"}`
    : "";
  const baseFee = finality.base_transaction_fee_points !== undefined
    ? ` · 原始費用 ${formatEconomyPointsValue(finality.base_transaction_fee_points)} 點`
    : "";
  const feeNote = feePolicy.base_fee_exempt
    ? ` · ${feePolicy.exemption_reason || "設定自動發放交易免鏈上費用"}`
    : "";
  const networkNote = !internalFinality && network.congestion_label
    ? ` · 鏈上 ${network.congestion_label} · 建議費用 ${formatEconomyPointsValue(network.suggested_total_fee_points || 0)} 點`
    : "";
  const title = internalFinality ? `${status} · 免 Proved` : `${status} · ${proved}/${target} Proved`;
  const humanRule = finality.human_rule || (
    internalFinality
      ? "pc0 Inner Address 使用站內帳本即時成交；免 20 Proved、免 priority fee"
      : "20 Proved；ETA 依鏈上忙碌度與 priority fee 動態估算"
  );
  return `
    <div class="economy-explorer-finality" data-finality-status="${sanitize(finality.finality_status || "")}" data-eta-seconds="${Number(finality.eta_seconds || 0)}" data-next-proof-eta-seconds="${Number(finality.next_proof_eta_seconds || 0)}">
      <div class="drive-card-heading">
        <div>
          <div class="drive-card-title">${sanitize(title)}</div>
          <div class="drive-card-sub">${sanitize(humanRule)} · ETA <span data-finality-eta-text>${sanitize(eta)}</span>${nextProofEta}${sanitize(feeNote)}${sanitize(networkNote)}${sanitize(baseFee)}${sanitize(accelerated)}</div>
        </div>
        <span class="economy-explorer-badge">${sanitize(finality.block_status || "unsealed")}</span>
      </div>
      <div class="economy-explorer-progress"><span style="width:${Math.max(0, Math.min(100, percent))}%;"></span></div>
    </div>
  `;
}

function economyExplorerFlowHtml(tx = {}) {
  const flow = tx.wallet_flow && typeof tx.wallet_flow === "object" ? tx.wallet_flow : {};
  const source = flow.source_wallet_address || "";
  const dest = flow.destination_wallet_address || "";
  const addressNode = (label, address) => address
    ? `<button class="economy-explorer-address" type="button" data-explorer-query="${sanitize(address)}">${sanitize(label)}<strong>${sanitize(shortEconomyWalletAddress(address))}</strong></button>`
    : `<span class="economy-explorer-address">${sanitize(label)}<strong>-</strong></span>`;
  return `
    <div class="economy-explorer-flow">
      ${addressNode(flow.source_label || "來源", source)}
      <span>→</span>
      ${addressNode(flow.destination_label || "目的", dest)}
    </div>
  `;
}

function economyExplorerTxCard(tx = {}) {
  const block = tx.block || null;
  const finality = tx.finality || {};
  const internalFinality = economyExplorerIsInternalFinality(finality);
  const layer = economyExplorerInferLayerFromTransaction(tx);
  const layerMeta = economyExplorerLayerMeta(layer);
  const settlementRail = tx.settlement_rail || tx.wallet_flow?.settlement_rail || tx.input_data?.settlement_rail || "";
  const assetType = tx.asset_type || layerMeta.assetType;
  const refs = tx.cross_references && typeof tx.cross_references === "object" ? tx.cross_references : {};
  const feePolicy = finality.chain_fee_policy && typeof finality.chain_fee_policy === "object" ? finality.chain_fee_policy : {};
  const pending = !internalFinality && !["sealed", "proved"].includes(String(finality.finality_status || ""));
  const canAccelerate = pending && feePolicy.acceleration_allowed !== false;
  const flow = tx.wallet_flow && typeof tx.wallet_flow === "object" ? tx.wallet_flow : {};
  const feeText = feePolicy.base_fee_exempt
    ? "免除"
    : `${formatEconomyPointsValue(finality.transaction_fee_points || finality.acceleration_fee_paid_points || 0)} 點`;
  const gasPriceText = feePolicy.base_fee_exempt
    ? "0 points/proved"
    : `${finality.gas_price_points_per_proved || 0} points/proved`;
  const network = finality.network_fee_state && typeof finality.network_fee_state === "object" ? finality.network_fee_state : {};
  const currentFeePoints = Number(finality.fee_points ?? finality.transaction_fee_points ?? finality.acceleration_fee_paid_points ?? 0);
  return `
    <div class="drive-card economy-explorer-card">
      <div class="drive-card-heading">
        <div>
          <div class="drive-card-title">交易 ${sanitize(shortEconomyWalletAddress(tx.ledger_hash || tx.ledger_uuid || ""))}</div>
          <div class="drive-card-sub">${sanitize(layerMeta.shortTitle)} · ${sanitize(assetType)} · ${sanitize(formatEconomyLedgerAction(tx.action_type))} · ${sanitize(tx.created_at || "")}</div>
        </div>
        <strong>${sanitize(formatEconomyLedgerAmount(tx))}</strong>
      </div>
      <div class="economy-explorer-kv">
        <span>Layer</span><code>${sanitize(layerMeta.title)}</code>
        <span>Asset Type</span><code>${sanitize(assetType)}</code>
        <span>Settlement Rail</span><code>${sanitize(settlementRail || "-")}</code>
        <span>Transaction Hash</span><code>${sanitize(tx.ledger_hash || "-")}</code>
        <span>Status</span><code>${internalFinality ? "internal_settled · 免 Proved" : `${sanitize(finality.finality_status || tx.status || "-")} · ${Number(finality.proved_count || 0)}/${Number(finality.target_proved_count || 20)} Proved`}</code>
        <span>Block</span>${internalFinality ? `<code>Internal Ledger</code>` : (block ? `<button type="button" data-explorer-query="${sanitize(String(block.block_number || ""))}">#${Number(block.block_number || 0)} · ${sanitize(shortEconomyWalletAddress(block.block_hash || ""))}</button>` : `<code>Pending / Unsealed</code>`)}
        <span>Timestamp</span><code>${sanitize(tx.created_at || "-")}</code>
        <span>From</span>${flow.source_wallet_address ? `<button type="button" data-explorer-query="${sanitize(flow.source_wallet_address)}">${sanitize(flow.source_wallet_address)}</button>` : `<code>-</code>`}
        <span>To</span>${flow.destination_wallet_address ? `<button type="button" data-explorer-query="${sanitize(flow.destination_wallet_address)}">${sanitize(flow.destination_wallet_address)}</button>` : `<code>-</code>`}
        <span>Value</span><code>${sanitize(formatEconomyLedgerAmount(tx))}</code>
        <span>Transaction Fee</span><code>${sanitize(feeText)}</code>
        ${finality.acceleration_fee_paid_points ? `<span>Acceleration</span><code>${formatEconomyPointsValue(finality.acceleration_fee_paid_points)} 點 → ${sanitize(finality.acceleration_fee_destination_label || "BURN 銷毀錢包")}</code>` : ""}
        <span>Gas Price</span><code>${sanitize(internalFinality ? "不適用" : gasPriceText)}</code>
        <span>Input Data</span><code>${sanitize(JSON.stringify(tx.input_data || {}))}</code>
        <span>Ledger UUID</span><button type="button" data-explorer-query="${sanitize(tx.ledger_uuid || "")}">${sanitize(tx.ledger_uuid || "-")}</button>
        <span>Previous</span><code>${sanitize(tx.previous_ledger_hash || "-")}</code>
        ${refs.bridge_event_uuid ? `<span>Bridge Event</span><button type="button" data-explorer-query="${sanitize(refs.bridge_event_query || refs.bridge_event_uuid)}" data-explorer-layer-jump="bridge">${sanitize(refs.bridge_event_uuid)}</button>` : ""}
        ${refs.pc1_settlement_tx ? `<span>PC1 Settlement TX</span><button type="button" data-explorer-query="${sanitize(refs.pc1_settlement_tx)}" data-explorer-layer-jump="pc1">${sanitize(refs.pc1_settlement_tx)}</button>` : ""}
        ${refs.pc0_wrapped_credit ? `<span>PC0 Wrapped Event</span><button type="button" data-explorer-query="${sanitize(refs.pc0_wrapped_credit)}" data-explorer-layer-jump="pc0">${sanitize(refs.pc0_wrapped_credit)}</button>` : ""}
      </div>
      ${economyExplorerFlowHtml(tx)}
      ${economyExplorerFinalityCard(finality)}
      ${canAccelerate ? `
        <div class="economy-explorer-accelerate" data-base-seconds-min="${Number(finality.network_base_seconds_min || finality.base_seconds_min || 120)}" data-base-seconds-max="${Number(finality.network_base_seconds_max || finality.base_seconds_max || 180)}" data-minimum-seconds-min="${Number(finality.minimum_seconds_min || 30)}" data-minimum-seconds-max="${Number(finality.minimum_seconds_max || 45)}" data-fee-reference-points="${Number(finality.fee_reference_points || network.suggested_priority_fee_points || 20)}" data-current-fee-points="${Number(currentFeePoints || 0)}">
          <div class="field"><label>鏈上費用</label><input type="number" id="economy-explorer-accelerate-fee" min="1" max="10000" value="${Number(network.suggested_priority_fee_points || 20)}" /><small id="economy-explorer-accelerate-estimate" class="drive-card-sub">輸入鏈上費用後顯示預估 Proved 時間</small></div>
          <button class="btn" id="economy-explorer-accelerate-btn" type="button" data-ledger-ref="${sanitize(tx.ledger_uuid || tx.ledger_hash || "")}">提高費用並加速</button>
        </div>
      ` : pending && feePolicy.base_fee_exempt ? `<div class="drive-card-sub economy-explorer-fee-note">${sanitize(feePolicy.exemption_reason || "設定自動發放交易免鏈上費用")}</div>` : ""}
    </div>
  `;
}

function economyExplorerWalletCard(wallet = {}) {
  const rows = Array.isArray(wallet.recent_transactions) ? wallet.recent_transactions : [];
  const addressType = String(wallet.address_type || "");
  const legacyAccount = Boolean(wallet.legacy_account) || addressType === "legacy_account";
  const systemFund = addressType === "system_fund" || Boolean(wallet.fund_key);
  const innerAddress = addressType === "inner_address" || economyIsPc0Address(wallet.address);
  const layer = wallet.layer || (innerAddress ? "pc0" : "pc1");
  const layerMeta = economyExplorerLayerMeta(layer);
  const assetType = wallet.asset_type || (innerAddress ? "Wrapped Operational Representation" : layerMeta.assetType);
  const risk = wallet.risk_label && typeof wallet.risk_label === "object" ? wallet.risk_label : null;
  const freeze = wallet.governance_freeze && typeof wallet.governance_freeze === "object" ? wallet.governance_freeze : null;
  const riskHtml = risk
    ? `<div class="drive-card-sub" style="color:#ff4f6d;margin-top:.45rem;">治理標記：${sanitize(risk.risk_level || risk.label || "risk")} · ${sanitize(risk.reason || "")}</div>`
    : "";
  const freezeHtml = freeze
    ? `<div class="drive-card-sub" style="color:#ff4f6d;margin-top:.35rem;">${freeze.freeze_type === "provisional" ? "短期審核凍結" : "治理凍結"}：禁止轉出${freeze.expires_at ? ` · 到期 ${sanitize(freeze.expires_at)}` : ""} · ${sanitize(freeze.reason || "")}</div>`
    : "";
  const titlePrefix = legacyAccount ? "Legacy 帳本身份" : (systemFund ? "系統基金錢包" : (innerAddress ? "Inner Address" : "錢包"));
  const identityLabel = legacyAccount ? "Legacy 帳本 ID" : (innerAddress ? "Inner Address" : "地址");
  const typeText = legacyAccount
    ? "legacy_account · 舊帳本公開識別碼"
    : innerAddress
      ? `inner address · pc0 站內託管地址 · ${sanitize(wallet.wallet_type || "-")} · ${sanitize(wallet.status || "-")}`
    : `${sanitize(wallet.wallet_type || "-")} · ${sanitize(wallet.status || "-")}${wallet.fund_key ? ` · ${sanitize(wallet.fund_key)}` : ""}`;
  const humanRule = wallet.human_rule || (
    innerAddress
      ? "pc0 Inner Address 使用站內帳本即時成交；免 20 Proved、免 priority fee"
      : "20 Proved；ETA 依鏈上忙碌度與 priority fee 動態估算"
  );
  return `
    <div class="drive-card economy-explorer-card">
      <div class="drive-card-heading">
        <div>
          <div class="drive-card-title">${sanitize(titlePrefix)} ${sanitize(shortEconomyWalletAddress(wallet.address || ""))}</div>
          <div class="drive-card-sub">${sanitize(layerMeta.shortTitle)} · ${sanitize(assetType)} · ${typeText}</div>
          ${riskHtml}
          ${freezeHtml}
        </div>
        <strong>${formatEconomyPointsValue(wallet.points_balance || 0)} 點</strong>
      </div>
      <div class="economy-explorer-kv">
        <span>Layer</span><code>${sanitize(layerMeta.title)}</code>
        <span>Asset Type</span><code>${sanitize(assetType)}</code>
        <span>${sanitize(identityLabel)}</span><code>${sanitize(wallet.address || "-")}</code>
        <span>金額凍結</span><code>${formatEconomyPointsValue(wallet.points_frozen || 0)} 點</code>
        <span>治理凍結</span><code>${freeze ? (freeze.freeze_type === "provisional" ? "短期禁止轉出" : "禁止轉出") : "無"}</code>
        <span>風險標記</span><code>${risk ? sanitize(risk.risk_level || risk.label || "risk") : "無"}</code>
        <span>交易數</span><code>${Number(wallet.transaction_count || 0)}</code>
        <span>成交條件</span><code>${sanitize(humanRule)}</code>
      </div>
      <div class="drive-file-list economy-explorer-tx-list">
        ${rows.length ? rows.map((tx) => `
          <div class="drive-file-row">
            <div>
              <strong>${sanitize(formatEconomyLedgerAmount(tx))} · ${sanitize(formatEconomyLedgerAction(tx.action_type))}</strong>
              <div class="drive-card-sub">${sanitize(tx.created_at || "")} · ${sanitize(shortEconomyWalletAddress(tx.ledger_hash || ""))}</div>
            </div>
            <button class="btn btn-sm" type="button" data-explorer-query="${sanitize(tx.ledger_uuid || tx.ledger_hash || "")}">查看</button>
          </div>
        `).join("") : `<div class="drive-empty">沒有可顯示的鏈上交易</div>`}
      </div>
    </div>
  `;
}

function economyExplorerBlockCard(block = {}) {
  const txs = Array.isArray(block.transactions) ? block.transactions : [];
  return `
    <div class="drive-card economy-explorer-card">
      <div class="drive-card-heading">
        <div>
          <div class="drive-card-title">區塊 #${Number(block.block_number || 0)}</div>
          <div class="drive-card-sub">PC1 Settlement · Canonical block · ${sanitize(block.seal_status || "-")} · ${sanitize(block.sealed_at || "")}</div>
        </div>
        <strong>${Number(block.ledger_count || 0)} tx</strong>
      </div>
      <div class="economy-explorer-kv">
        <span>Layer</span><code>PC1 Canonical Settlement Layer</code>
        <span>Asset Type</span><code>Canonical Settlement Block</code>
        <span>Block Hash</span><code>${sanitize(block.block_hash || "-")}</code>
        <span>Previous</span><code>${sanitize(block.previous_block_hash || "-")}</code>
        <span>Merkle Root</span><code>${sanitize(block.merkle_root || "-")}</code>
        <span>Anchor</span><code>${sanitize(block.anchor_status || "-")}</code>
      </div>
      <div class="drive-file-list economy-explorer-tx-list">
        ${txs.length ? txs.map((tx) => `
          <div class="drive-file-row">
            <div>
              <strong>${sanitize(formatEconomyLedgerAmount(tx))} · ${sanitize(formatEconomyLedgerAction(tx.action_type))}</strong>
              <div class="drive-card-sub">${sanitize(tx.created_at || "")} · ${sanitize(shortEconomyWalletAddress(tx.ledger_hash || ""))}</div>
            </div>
            <button class="btn btn-sm" type="button" data-explorer-query="${sanitize(tx.ledger_uuid || tx.ledger_hash || "")}">查看</button>
          </div>
        `).join("") : `<div class="drive-empty">此區塊沒有交易</div>`}
      </div>
    </div>
  `;
}

function economyExplorerBridgeCard(bridge = {}) {
  const internalTx = bridge.internal_transaction || null;
  const invariantOk = String(bridge.invariant_status || "") === "valid";
  return `
    <div class="drive-card economy-explorer-card">
      <div class="drive-card-heading">
        <div>
          <div class="drive-card-title">Bridge Event ${sanitize(shortEconomyWalletAddress(bridge.bridge_uuid || ""))}</div>
          <div class="drive-card-sub">Cross-Ledger Settlement Event · ${sanitize(bridge.bridge_type || "-")} · ${sanitize(bridge.status || "-")}</div>
        </div>
        <strong>${formatEconomyPointsValue(bridge.amount_points || 0)} 點</strong>
      </div>
      <div class="economy-explorer-kv">
        <span>Layer</span><code>Bridge Cross-Ledger Settlement Layer</code>
        <span>Asset Type</span><code>Cross-Ledger Settlement Event</code>
        <span>Invariant Status</span><code>${invariantOk ? "valid" : "invalid"}</code>
        <span>Bridge UUID</span><code>${sanitize(bridge.bridge_uuid || "-")}</code>
        <span>PC1 Settlement TX</span><button type="button" data-explorer-query="${sanitize(bridge.pc1_settlement_tx || bridge.chain_tx_hash || "")}" data-explorer-layer-jump="pc1">${sanitize(bridge.pc1_settlement_tx || bridge.chain_tx_hash || "-")}</button>
        <span>PC1 Deposit Address</span><button type="button" data-explorer-query="${sanitize(bridge.destination_address || "")}" data-explorer-layer-jump="pc1">${sanitize(bridge.destination_address || "-")}</button>
        <span>PC0 Wrapped Credit</span>${bridge.pc0_wrapped_credit ? `<button type="button" data-explorer-query="${sanitize(bridge.pc0_wrapped_credit)}" data-explorer-layer-jump="pc0">${sanitize(bridge.pc0_wrapped_credit)}</button>` : `<code>-</code>`}
        <span>PC0 Hot Wallet</span>${bridge.pc0_hot_wallet ? `<button type="button" data-explorer-query="${sanitize(bridge.pc0_hot_wallet)}" data-explorer-layer-jump="pc0">${sanitize(bridge.pc0_hot_wallet)}</button>` : `<code>-</code>`}
        <span>Risk</span><code>${sanitize(bridge.risk_status || "-")}</code>
        <span>Confirmations</span><code>${Number(bridge.confirmations || 0)}/${Number(bridge.required_confirmations || 0)}</code>
        <span>Network Fee</span><code>${formatEconomyPointsValue(bridge.network_fee_points || 0)} 點</code>
        <span>Created</span><code>${sanitize(bridge.created_at || "-")}</code>
      </div>
      ${internalTx ? `<div style="margin-top:.75rem;">${economyExplorerTxCard(internalTx)}</div>` : `<div class="drive-empty" style="margin-top:.75rem;">尚未產生 PC0 wrapped credit ledger</div>`}
    </div>
  `;
}

function economyExplorerAuditCard(report = {}) {
  const invariants = Array.isArray(report.invariants) ? report.invariants : [];
  const reserve = report.canonical_reserve || {};
  const liabilities = report.wrapped_operational_liabilities || {};
  const bridge = report.bridge_reconstruction || {};
  const boundary = report.ledger_boundary || {};
  return `
    <div class="drive-card economy-explorer-card">
      <div class="drive-card-heading">
        <div>
          <div class="drive-card-title">Reserve / Liability Audit</div>
          <div class="drive-card-sub">${sanitize(report.model || "pc1_canonical_reserve_pc0_wrapped_operational_v1")} · ${sanitize(report.status || "-")}</div>
        </div>
        <strong>${report.ok ? "PASS" : "FAIL"}</strong>
      </div>
      <div class="economy-explorer-kv">
        <span>PC1 Canonical Reserve</span><code>${formatEconomyPointsValue(reserve.canonical_locked_reserve_points || reserve.active_supply_points || 0)} 點</code>
        <span>PC0 Wrapped Outstanding</span><code>${formatEconomyPointsValue(liabilities.wrapped_supply_points || liabilities.finalized_total_points || 0)} 點</code>
        <span>Bridge External</span><code>${formatEconomyPointsValue(bridge.current_cold_chain_or_bridge_external_points || 0)} 點</code>
        <span>Flow Gap</span><code>${formatEconomyPointsValue(bridge.flow_gap_points || 0)} 點</code>
        <span>Boundary</span><code>${boundary.ok ? "clean" : "polluted"} · sealed pc0 ${Number(boundary.counts?.sealed_pc0_operational_ledgers || 0)}</code>
        <span>Merkle Root</span><code>${sanitize(liabilities.liability_merkle?.merkle_root || "-")}</code>
      </div>
      <div class="drive-file-list economy-explorer-tx-list">
        ${invariants.length ? invariants.map((item) => `
          <div class="drive-file-row">
            <div>
              <strong>${item.pass ? "PASS" : "FAIL"} · ${sanitize(item.name || "-")}</strong>
              <div class="drive-card-sub">${sanitize(JSON.stringify(item))}</div>
            </div>
          </div>
        `).join("") : `<div class="drive-empty">沒有 invariant 資料</div>`}
      </div>
    </div>
  `;
}

function bindEconomyExplorerResultEvents() {
  const root = $("economy-explorer-result");
  if (!root) return;
  root.querySelectorAll("[data-explorer-query]").forEach((btn) => {
    if (btn.dataset.explorerBound === "1") return;
    btn.dataset.explorerBound = "1";
    btn.addEventListener("click", () => {
      const query = btn.dataset.explorerQuery || "";
      const layerJump = btn.dataset.explorerLayerJump || "";
      if (layerJump) setEconomyExplorerLayer(layerJump, { reset: false });
      if ($("economy-explorer-query")) $("economy-explorer-query").value = query;
      searchEconomyExplorer(query);
    });
  });
  const accelerateBtn = $("economy-explorer-accelerate-btn");
  if (accelerateBtn && accelerateBtn.dataset.explorerBound !== "1") {
    accelerateBtn.dataset.explorerBound = "1";
    accelerateBtn.addEventListener("click", accelerateEconomyExplorerTx);
  }
  const accelerateFeeInput = $("economy-explorer-accelerate-fee");
  if (accelerateFeeInput && accelerateFeeInput.dataset.explorerEstimateBound !== "1") {
    accelerateFeeInput.dataset.explorerEstimateBound = "1";
    accelerateFeeInput.addEventListener("input", updateEconomyExplorerAccelerateEstimate);
    accelerateFeeInput.addEventListener("change", updateEconomyExplorerAccelerateEstimate);
    updateEconomyExplorerAccelerateEstimate();
  }
}

function renderEconomyExplorerResult(result) {
  const wrap = $("economy-explorer-result");
  if (!wrap) return;
  stopEconomyExplorerCountdown();
  if (!result) {
    wrap.innerHTML = `${economyExplorerLayerBanner()}<div class="drive-empty">查無分層帳本資料</div>`;
    return;
  }
  if (result.kind === "transaction") wrap.innerHTML = economyExplorerLayerBanner(result) + economyExplorerTxCard(result.transaction || {});
  else if (result.kind === "wallet") wrap.innerHTML = economyExplorerLayerBanner(result) + economyExplorerWalletCard(result.wallet || {});
  else if (result.kind === "block") wrap.innerHTML = economyExplorerLayerBanner(result) + economyExplorerBlockCard(result.block || {});
  else if (result.kind === "bridge") wrap.innerHTML = economyExplorerLayerBanner(result) + economyExplorerBridgeCard(result.bridge_event || {});
  else if (result.kind === "audit") wrap.innerHTML = economyExplorerLayerBanner(result) + economyExplorerAuditCard(result.financial_invariants || {});
  else wrap.innerHTML = `<div class="drive-empty">不支援的查詢結果</div>`;
  bindEconomyExplorerResultEvents();
  startEconomyExplorerCountdown();
}

async function searchEconomyExplorer(query = null) {
  if (!economyChainEnabled()) {
    economyExplorerMsg("PointsChain 私有鏈已停用，Explorer 不可用。", false);
    return;
  }
  const input = $("economy-explorer-query");
  const value = String(query ?? input?.value ?? "").trim();
  if (!value && economyExplorerActiveLayer === "audit") {
    try {
      const json = await fetchEconomyJson("/root/points/financial-invariants");
      if (json.async) {
        let latestPayload = null;
        if (json.latest_snapshot_url) {
          try {
            const latest = await fetchEconomyJson(json.latest_snapshot_url, { allowMissingSnapshot: true });
            latestPayload = latest?.financial_invariants ? latest : null;
          } catch (_) {}
        }
        if (latestPayload) {
          renderEconomyExplorerResult({ kind: "audit", layer: "audit", asset_type: "Reserve / Liability Audit", financial_invariants: latestPayload.financial_invariants || {} });
        } else {
          renderEconomyExplorerResult(null);
        }
        economyExplorerMsg(`Audit invariant 已排入背景任務${json.job_id ? `：${json.job_id}` : ""}`);
        return;
      }
      renderEconomyExplorerResult({ kind: "audit", layer: "audit", asset_type: "Reserve / Liability Audit", financial_invariants: json.financial_invariants || {} });
      economyExplorerMsg("已更新 Audit invariant");
    } catch (err) {
      renderEconomyExplorerResult(null);
      economyExplorerMsg(err.message || "Audit 查詢失敗", false);
    }
    return;
  }
  if (!value) {
    economyExplorerMsg("請輸入交易 hash、Ledger UUID、錢包地址、區塊或 bridge event", false);
    return;
  }
  economyExplorerLastQuery = value;
  if (input) input.value = value;
  const btn = $("economy-explorer-search-btn");
  const oldText = btn ? btn.textContent : "";
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "查詢中...";
    }
    const json = economyExplorerActiveLayer === "bridge"
      ? await fetchEconomyJson(`/points/explorer/bridge/${encodeURIComponent(value)}`)
      : await fetchEconomyJson(`/points/explorer/search?q=${encodeURIComponent(value)}&limit=25`);
    renderEconomyExplorerResult(json.result);
    economyExplorerMsg("已更新分層帳本資料");
  } catch (err) {
    renderEconomyExplorerResult(null);
    economyExplorerMsg(err.message || "分層帳本查詢失敗", false);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText || "查詢";
    }
  }
}

async function accelerateEconomyExplorerTx() {
  if (!economyChainEnabled()) {
    economyExplorerMsg("PointsChain 私有鏈已停用，無法加速交易。", false);
    return;
  }
  const btn = $("economy-explorer-accelerate-btn");
  const ledgerRef = btn?.dataset?.ledgerRef || economyExplorerLastQuery;
  const fee = Number($("economy-explorer-accelerate-fee")?.value || 0);
  if (!ledgerRef || !Number.isFinite(fee) || fee <= 0) {
    economyExplorerMsg("請輸入有效鏈上費用", false);
    return;
  }
  const oldText = btn ? btn.textContent : "";
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "送出中...";
    }
    const json = await fetchEconomyJson("/points/explorer/accelerate", {
      method: "POST",
      body: JSON.stringify({
        ledger_ref: ledgerRef,
        fee_points: Math.floor(fee),
        request_uuid: economyRequestId("points_chain_acceleration"),
      }),
    });
    renderEconomyExplorerResult(json.result);
    const tx = json.result?.transaction || {};
    const finality = tx.finality || {};
    const eta = Number(finality.eta_seconds || 0);
    const proved = `${Number(finality.proved_count || 0)}/${Number(finality.target_proved_count || 20)}`;
    economyExplorerMsg(`已送出鏈上加速費用，Proved ${proved}，ETA ${economyExplorerSecondsText(eta)}`);
    await loadEconomyDashboard();
  } catch (err) {
    economyExplorerMsg(err.message || "加速失敗", false);
  } finally {
    if (btn) {
      btn.disabled = false;
        btn.textContent = oldText || "提高費用並加速";
    }
  }
}

async function loadEconomyProof(ledgerUuid) {
  if (!ledgerUuid) return;
  try {
    const json = await fetchEconomyJson(`/points/ledger/${encodeURIComponent(ledgerUuid)}/proof`);
    const proof = json.proof || {};
    const text = proof.sealed
      ? `Proof 已封塊：ledger ${sanitize(ledgerUuid)} 位於區塊 #${Number(proof.block_number || 0)}，Merkle path ${Array.isArray(proof.merkle_path) ? proof.merkle_path.length : 0} 層`
      : `Proof 尚未封塊：ledger ${sanitize(ledgerUuid)} 仍在未封 ledger 中`;
    setEconomyChainStatus(text);
  } catch (err) {
    economySetMsg(err.message || "Proof 讀取失敗", false);
    setEconomyChainStatus(err.message || "Proof 讀取失敗", false);
  }
}
