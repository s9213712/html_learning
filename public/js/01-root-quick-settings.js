'use strict';

const ROOT_SERVICE_FEE_QUICK_PRESETS = [
  { item_key: "post_cost_standard", item_name: "一般發文成本", category: "forum", base_price: 1, min_price: 1, max_price: 10, rationale: "低額防洗版，低於每日登入 5 點。" },
  { item_key: "post_pin_24h", item_name: "文章置頂 24 小時", category: "forum", base_price: 100, min_price: 50, max_price: 300, rationale: "曝光型功能，價格約等於 20 天每日登入。" },
  { item_key: "cloud_storage_1gb_7d", item_name: "雲端容量 1GB / 7 天", category: "cloud_drive", base_price: 100, min_price: 50, max_price: 500, metadata: { storage_bytes: 1024 * 1024 * 1024, duration_days: 7, label: "雲端容量 1GB / 7 天" }, rationale: "容量是持續成本，保留較高 sink。" },
  { item_key: "cloud_storage_1gb_30d", item_name: "雲端容量 1GB / 30 天", category: "cloud_drive", base_price: 400, min_price: 200, max_price: 2000, metadata: { storage_bytes: 1024 * 1024 * 1024, duration_days: 30, label: "雲端容量 1GB / 30 天" }, rationale: "30 天方案是 7 天方案 4 倍點數，換取較長有效期。" },
  { item_key: "comfyui_txt2img_basic", item_name: "ComfyUI 基礎生圖一次", category: "comfyui", base_price: 5, min_price: 1, max_price: 25, dynamic_pricing: true, rationale: "等同每日登入一次，適合低門檻試用。" },
  { item_key: "comfyui_txt2img_highres", item_name: "ComfyUI 高解析生圖一次", category: "comfyui", base_price: 12, min_price: 5, max_price: 60, dynamic_pricing: true, rationale: "高資源消耗，約基礎生圖 2-3 倍。" },
  { item_key: "comfyui_batch_10", item_name: "ComfyUI 批次生圖 10 張", category: "comfyui", base_price: 45, min_price: 20, max_price: 200, dynamic_pricing: true, rationale: "批次任務佔用較久，保留折扣但高於單次。" },
  { item_key: "video_publish_basic", item_name: "影音發布處理費", category: "video", base_price: 2, min_price: 1, max_price: 20, rationale: "發布低價，收入重心在投幣抽成與流量分潤。" },
  { item_key: "video_boost_24h", item_name: "影音曝光加成 24 小時", category: "video", base_price: 80, min_price: 30, max_price: 300, rationale: "曝光型功能需高於一般發布，避免洗推薦。" },
  { item_key: "game_entry_standard", item_name: "遊戲一般入場", category: "game", base_price: 1, min_price: 1, max_price: 10, rationale: "高頻低額，走 pc0 站內帳本即時扣款，不逐筆等待鏈上確認。" },
  { item_key: "game_virtual_item_common", item_name: "普通虛寶", category: "game", base_price: 20, min_price: 5, max_price: 100, rationale: "遊戲內消耗品，價格應低於長期曝光型功能。" },
  { item_key: "marketplace_listing_fee", item_name: "市集上架費", category: "marketplace", base_price: 3, min_price: 1, max_price: 30, rationale: "低額抑制垃圾上架，成交抽成另列平台收入。" },
  { item_key: "ai_agent_task_basic", item_name: "AI Agent 基礎任務", category: "ai_task", base_price: 10, min_price: 5, max_price: 100, dynamic_pricing: true, rationale: "預留外部 API / 任務排程成本。" },
  { item_key: "username_change", item_name: "改名", category: "account", base_price: 200, min_price: 100, max_price: 1000, rationale: "低頻身分操作，維持較高價格降低濫用。" },
  { item_key: "profile_decoration", item_name: "個人頁裝飾", category: "account", base_price: 50, min_price: 10, max_price: 250, rationale: "個人頁外觀型服務，作為中低額 sink。" },
  { item_key: "violation_fine", item_name: "違規罰款繳納", category: "governance", base_price: 300, min_price: 1, max_price: 100000, metadata: { destination: "burn", description: "違規罰款由用戶授權付款，預設銷毀。" }, rationale: "罰款需可調整，但付款仍由用戶授權並預設銷毀。" },
];

window.HACKME_SERVICE_FEE_PRICING_PRESETS = ROOT_SERVICE_FEE_QUICK_PRESETS;

let rootModuleEconomyCatalogCache = [];

const ROOT_MODULE_QUICK_SETTINGS = {
  chat: {
    label: "聊天室",
    section: "features",
    fields: [
      { id: "s-feature-chat-enabled", label: "開放聊天室" },
      { id: "s-module-chat-min-role", label: "最低可用角色" },
      { id: "s-feature-attachments-enabled", label: "允許聊天附件" },
      { id: "s-feature-reports-enabled", label: "啟用檢舉審核" },
      { id: "s-feature-reports-notifications-enabled", label: "啟用檢舉 / 通知" },
    ],
  },
  announcements: {
    label: "公告",
    section: "features",
    pricingKeys: ["post_cost_standard", "post_pin_24h"],
    fields: [
      { id: "s-feature-community-enabled", label: "開放公告 / 討論功能" },
      { id: "s-module-community-min-role", label: "公告與討論最低角色" },
      { id: "s-feature-attachments-enabled", label: "允許公告附件" },
      { id: "s-feature-reports-notifications-enabled", label: "啟用管理通知" },
    ],
  },
  community: {
    label: "討論區",
    section: "features",
    pricingKeys: ["post_cost_standard", "post_pin_24h"],
    fields: [
      { id: "s-feature-community-enabled", label: "開放討論區" },
      { id: "s-module-community-min-role", label: "最低可用角色" },
      { id: "s-feature-forum-core-enabled", label: "新版論壇核心" },
      { id: "s-feature-attachments-enabled", label: "允許附件" },
      { id: "s-feature-reports-enabled", label: "啟用檢舉審核" },
    ],
  },
  drive: {
    label: "雲端硬碟",
    section: "drive",
    pricingKeys: ["cloud_storage_1gb_7d", "cloud_storage_1gb_30d"],
    fields: [
      { id: "s-feature-privacy-uploads-enabled", label: "開放雲端硬碟 / E2EE" },
      { id: "s-feature-storage-albums-enabled", label: "開放相簿" },
      { id: "s-cloud-drive-global-capacity-limit-mb", label: "全站容量上限 MB" },
      { id: "s-storage-maintenance-auto-enabled", label: "每日自動維護" },
      { id: "s-storage-trash-retention-days", label: "已刪除檔案保留天數" },
      { id: "s-cd-require-scan-before-download", label: "下載前要求掃描", save: "drivePolicy" },
      { id: "s-cd-block-unclean-downloads", label: "阻擋未掃描乾淨下載", save: "drivePolicy" },
    ],
  },
  albums: {
    label: "相簿",
    section: "drive",
    pricingKeys: ["cloud_storage_1gb_7d", "cloud_storage_1gb_30d"],
    fields: [
      { id: "s-feature-storage-albums-enabled", label: "開放相簿" },
      { id: "s-feature-privacy-uploads-enabled", label: "開放雲端硬碟 / E2EE" },
      { id: "s-cloud-drive-global-capacity-limit-mb", label: "全站容量上限 MB" },
      { id: "s-storage-trash-retention-days", label: "已刪除檔案保留天數" },
    ],
  },
  videos: {
    label: "影音",
    section: "billing",
    pricingKeys: ["video_publish_basic", "video_boost_24h"],
    fields: [
      { id: "s-feature-videos-enabled", label: "開放影音分享" },
      { id: "s-module-videos-min-role", label: "最低可用角色" },
      { id: "s-video-tip-fee-percent", label: "投幣平台抽成 %" },
      { id: "s-video-tip-min-points", label: "投幣最低點數" },
      { id: "s-feature-privacy-uploads-enabled", label: "允許引用雲端檔案" },
    ],
  },
  games: {
    label: "遊戲區",
    section: "features",
    pricingKeys: ["game_entry_standard", "game_virtual_item_common"],
    fields: [
      { id: "s-feature-games-enabled", label: "開放遊戲區" },
      { id: "s-module-games-min-role", label: "最低可用角色" },
    ],
  },
  experiments: {
    label: "實驗區",
    section: "features",
    note: "實驗區目前是純前端 Canvas 教育模擬，沒有後端重型 job、DB 或 worker。",
    fields: [
      { id: "s-feature-experiments-enabled", label: "開放實驗區" },
    ],
  },
  jobs: {
    label: "任務中心",
    section: "system",
    note: "任務中心目前主要顯示後台任務狀態，這裡只保留當頁常用 root 快速設定。",
    fields: [
      { id: "s-notification-muted-types", label: "靜音通知類型" },
    ],
  },
  shares: {
    label: "分享管理",
    section: "drive",
    pricingKeys: ["cloud_storage_1gb_7d", "cloud_storage_1gb_30d", "video_publish_basic"],
    fields: [
      { id: "s-feature-privacy-uploads-enabled", label: "開放檔案分享來源" },
      { id: "s-feature-storage-albums-enabled", label: "開放相簿分享來源" },
      { id: "s-feature-videos-enabled", label: "開放影音分享來源" },
      { id: "s-cd-revoke-shares-on-suspension", label: "停權時撤銷分享", save: "drivePolicy" },
      { id: "s-cd-warn-high-risk-downloads", label: "高風險下載警告", save: "drivePolicy" },
    ],
  },
  comfyui: {
    label: "AI 產圖",
    section: "comfyui",
    pricingKeys: ["comfyui_txt2img_basic", "comfyui_txt2img_highres", "comfyui_batch_10"],
    fields: [
      { id: "s-feature-comfyui-enabled", label: "開放 AI 產圖" },
      { id: "s-module-comfyui-min-role", label: "最低可用角色" },
      { id: "s-comfyui-comfyui-backend-mode", label: "ComfyUI / GGUF 執行方式" },
      { id: "s-comfyui-remote-api-url", label: "遠端 API 位址", visibleWhen: { id: "s-comfyui-connection-mode", value: "remote" } },
      { id: "s-comfyui-base-dir", label: "本地 ComfyUI 資料夾", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-local-start-script", label: "本地啟動腳本", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-api-host", label: "本地 API Host", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-api-port", label: "本地 API Port", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-local-vram-mode", label: "本地 VRAM 模式", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-local-precision", label: "本地整體精度", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-local-unet-dtype", label: "本地 UNet dtype", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-local-vae-dtype", label: "本地 VAE dtype", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-local-cpu-vae", label: "本地 CPU VAE", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-local-attention-mode", label: "本地 attention backend", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-local-reserve-vram-gb", label: "本地保留 VRAM GB", visibleWhen: { id: "s-comfyui-connection-mode", value: "local" } },
      { id: "s-comfyui-diffusers-model-repo", label: "Hugging Face Repo", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-huggingface-api-token", label: "Hugging Face API Token", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-huggingface-api-token-clear", label: "清除已儲存 HF Token", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-huggingface-cache-root", label: "Hugging Face 快取根目錄", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-diffusers-device", label: "Diffusers Device", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-diffusers-cuda-fallback-to-cpu", label: "GPU 失敗改用 CPU", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-diffusers-dtype", label: "Diffusers dtype", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-diffusers-device-map", label: "Diffusers device_map", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-allow-in-process-diffusers", label: "接受主程序 Diffusers 資源風險", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-diffusers-low-cpu-mem-usage", label: "低 RAM 載入", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-diffusers-keep-downloaded-models", label: "保留已下載模型快取", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-diffusers-disable-xet", label: "停用 HF Xet 下載", visibleWhen: { id: "s-comfyui-connection-mode", value: "diffusers" } },
      { id: "s-comfyui-max-batch-size", label: "單次張數上限" },
      { id: "s-comfyui-default-width", label: "預設寬度" },
      { id: "s-comfyui-default-height", label: "預設高度" },
    ],
  },
  "ai-agent": {
    label: "AI 助理",
    section: "features",
    pricingKeys: ["ai_agent_task_basic"],
    fields: [
      { id: "s-feature-ai-agent-enabled", label: "開放 AI Agent / LLM" },
      { id: "s-module-ai-agent-min-role", label: "最低可用角色" },
      { id: "s-ai-agent-provider", label: "Provider" },
      { id: "s-ai-agent-api-base-url", label: "API Base URL" },
      { id: "s-ai-agent-model", label: "模型" },
      { id: "s-ai-agent-operation-mode", label: "運作模式" },
      { id: "s-ai-agent-allowed-models", label: "允許模型清單" },
      { id: "s-ai-agent-allowed-tools", label: "可調用工具清單" },
      { id: "s-ai-agent-allow-image-input", label: "允許圖片理解" },
    ],
  },
  economy: {
    label: "積分系統",
    section: "features",
    fields: [
      { id: "s-feature-economy-enabled", label: "開放積分系統" },
      { id: "s-feature-trading-enabled", label: "開放積分交易所" },
    ],
  },
  trading: {
    label: "積分交易所",
    section: "trading",
    fields: [
      { id: "s-feature-economy-enabled", label: "開放積分系統" },
      { id: "s-feature-trading-enabled", label: "開放交易所" },
      { id: "root-trading-enabled", label: "啟用交易引擎", save: "trading" },
      { id: "root-trading-borrowing-enabled", label: "允許借貸交易", save: "trading" },
      { id: "root-trading-maintenance-percent", label: "維持保證金 %", save: "trading" },
      { id: "root-trading-price-source", label: "交易價格來源", save: "trading" },
      { id: "root-trading-price-fusion-min-provider-count", label: "融合最低來源數", save: "trading" },
      { id: "root-trading-price-fusion-trade-min-provider-count", label: "交易最低健康來源數", save: "trading" },
      { id: "root-trading-simulated-slippage-enabled", label: "啟用模擬滑價", save: "trading" },
      { id: "root-trading-bot-auto-enabled", label: "交易機器人自動掃描", save: "trading" },
      { id: "root-trading-bot-audit-enabled", label: "交易機器人定期稽核", save: "trading" },
    ],
  },
  appeals: {
    label: "申覆",
    section: "features",
    pricingKeys: ["violation_fine"],
    fields: [
      { id: "s-feature-appeals-enabled", label: "開放用戶申覆" },
      { id: "s-module-appeals-min-role", label: "最低可用角色" },
      { id: "s-feature-reports-notifications-enabled", label: "啟用審核通知" },
    ],
  },
  accounts: {
    label: "帳號管理",
    section: "accounts",
    pricingKeys: ["username_change", "profile_decoration", "violation_fine"],
    fields: [
      { id: "s-feature-accounts-enabled", label: "開放帳號管理" },
      { id: "s-module-accounts-min-role", label: "最低可用角色" },
      { id: "s-feature-account-security-enabled", label: "帳號安全強化" },
      { id: "s-feature-identity-governance-enabled", label: "身份治理 / 會員等級" },
      { id: "s-feature-member-governance-enabled", label: "會員治理與投票" },
      { id: "s-feature-violation-center-enabled", label: "違規中心" },
      { id: "s-feature-reports-notifications-enabled", label: "管理通知" },
    ],
  },
  profile: {
    label: "個人面板",
    section: "system",
    pricingKeys: ["username_change", "profile_decoration"],
    fields: [
      { id: "s-module-profile-min-role", label: "最低可用角色" },
    ],
  },
  server: {
    label: "安全中心",
    section: "security",
    fields: [
      { id: "s-maintenance-mode", label: "維護模式" },
      { id: "s-audit-chain-enabled", label: "審計 hash chain" },
      { id: "s-ip-blocking-enabled", label: "錯誤登入鎖 IP" },
      { id: "s-login-violation-enabled", label: "錯誤登入寫入違規" },
      { id: "s-rate-limit-violation-enabled", label: "速率限制寫入違規" },
      { id: "s-root-ip-whitelist-enabled", label: "root IP 白名單" },
      { id: "s-root-ip-whitelist", label: "root IP 白名單內容" },
      { id: "s-server-ssl-enabled", label: "啟用 HTTPS" },
    ],
  },
};

function rootModuleQuickConfig(tab = currentModuleTab) {
  return ROOT_MODULE_QUICK_SETTINGS[tab] || null;
}

function rootModuleQuickButtonLabel(tab) {
  const label = rootModuleQuickConfig(tab)?.label || tab || "目前頁面";
  return `${label}快速設定`;
}

function ensureRootModuleSettingsButtons() {
  document.querySelectorAll(".module-section[id^='module-']").forEach((section) => {
    if (section.querySelector(":scope > .root-module-settings-btn")) return;
    const tab = section.id.replace(/^module-/, "");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "root-module-settings-btn";
    button.dataset.rootModuleSettings = tab;
    button.setAttribute("aria-label", rootModuleQuickButtonLabel(tab));
    button.title = rootModuleQuickButtonLabel(tab);
    button.textContent = "⚙";
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openRootModuleSettings(tab);
    });
    section.prepend(button);
  });
}

function syncRootModuleSettingsButtons() {
  ensureRootModuleSettingsButtons();
  const rootMode = currentUser === "root";
  document.querySelectorAll(".root-module-settings-btn").forEach((button) => {
    const section = button.closest(".module-section");
    const tab = button.dataset.rootModuleSettings || "";
    const visible = rootMode && !!section?.classList.contains("active") && !!rootModuleQuickConfig(tab);
    button.classList.toggle("show", visible);
    button.hidden = !visible;
  });
}

function ensureRootModuleSettingsModal() {
  let overlay = $("root-module-settings-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "root-module-settings-overlay";
  overlay.className = "user-edit-overlay root-module-settings-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "root-module-settings-title");
  overlay.innerHTML = `
    <div class="user-edit-modal root-module-settings-modal">
      <div class="drive-card-heading compact-heading">
        <div>
          <div class="mini-title" id="root-module-settings-title">頁面設定</div>
          <div class="drive-card-sub" id="root-module-settings-subtitle">root 快速設定</div>
        </div>
        <button type="button" class="btn btn-sm" id="root-module-settings-close">關閉</button>
      </div>
      <div class="root-module-settings-note" id="root-module-settings-note"></div>
      <div class="settings-option-grid root-module-settings-grid" id="root-module-settings-fields"></div>
      <div class="root-module-pricing-panel" id="root-module-pricing-panel" hidden>
        <div class="drive-card-sub root-module-pricing-note">此功能的服務扣點項目。root 儲存後會寫入 economy price catalog，收入以 pc0 站內帳本即時入官方 Treasury。</div>
        <div class="drive-file-list root-module-pricing-list" id="root-module-pricing-list"></div>
      </div>
      <div id="root-module-settings-msg" class="msg"></div>
      <div class="edit-user-actions">
        <button type="button" id="root-module-settings-save" class="btn btn-primary">儲存</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  $("root-module-settings-close")?.addEventListener("click", closeRootModuleSettings);
  $("root-module-settings-save")?.addEventListener("click", saveRootModuleSettings);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeRootModuleSettings();
  });
  return overlay;
}

function rootModuleProxyId(sourceId) {
  return `root-module-setting-${sourceId}`;
}

function rootModuleFieldValue(source) {
  if (!source) return "";
  if (source.type === "checkbox") return !!source.checked;
  return source.value ?? "";
}

function rootModuleFieldVisible(field) {
  if (!field?.visibleWhen) return true;
  const source = $(rootModuleProxyId(field.visibleWhen.id)) || $(field.visibleWhen.id);
  if (!source) return true;
  const values = Array.isArray(field.visibleWhen.value) ? field.visibleWhen.value : [field.visibleWhen.value];
  return values.includes(rootModuleFieldValue(source));
}

function rootModulePricingPresets(config) {
  const keys = Array.isArray(config?.pricingKeys) ? new Set(config.pricingKeys) : null;
  const categories = Array.isArray(config?.pricingCategories) ? new Set(config.pricingCategories) : null;
  if (!keys && !categories) return [];
  return ROOT_SERVICE_FEE_QUICK_PRESETS.filter((item) => (
    (keys && keys.has(item.item_key)) || (categories && categories.has(item.category))
  ));
}

function rootModuleCatalogByKey() {
  return new Map((rootModuleEconomyCatalogCache || []).map((item) => [String(item.item_key || ""), item]));
}

async function loadRootModuleEconomyCatalogIfNeeded(config) {
  if (!rootModulePricingPresets(config).length) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/economy/catalog", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" },
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `服務扣點讀取失敗（HTTP ${res.status}）`);
  rootModuleEconomyCatalogCache = Array.isArray(json.catalog) ? json.catalog : [];
}

function rootModulePricingPayloadForKey(itemKey) {
  const catalog = rootModuleCatalogByKey();
  const preset = ROOT_SERVICE_FEE_QUICK_PRESETS.find((item) => item.item_key === itemKey);
  const current = catalog.get(itemKey) || {};
  if (!preset && !current.item_key) return null;
  const escapedKey = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(itemKey) : String(itemKey).replace(/"/g, '\\"');
  const input = document.querySelector(`[data-root-module-pricing-price="${escapedKey}"]`);
  const enabled = document.querySelector(`[data-root-module-pricing-enabled="${escapedKey}"]`);
  const basePrice = Math.max(0, Math.round(Number(input?.value || current.base_price || preset?.base_price || 0)));
  return {
    item_key: itemKey,
    item_name: current.item_name || preset?.item_name || itemKey,
    category: current.category || preset?.category || "custom",
    base_price: basePrice,
    min_price: current.min_price ?? preset?.min_price ?? "",
    max_price: current.max_price ?? preset?.max_price ?? "",
    dynamic_pricing: current.dynamic_pricing !== undefined ? !!current.dynamic_pricing : !!preset?.dynamic_pricing,
    enabled: enabled ? !!enabled.checked : (current.enabled !== 0 && current.enabled !== false && preset?.enabled !== false),
    metadata: current.metadata || preset?.metadata || {},
  };
}

function renderRootModulePricingFields(tab) {
  const config = rootModuleQuickConfig(tab);
  const panel = $("root-module-pricing-panel");
  const list = $("root-module-pricing-list");
  if (!panel || !list || !config) return;
  const presets = rootModulePricingPresets(config);
  panel.hidden = !presets.length;
  if (!presets.length) {
    list.innerHTML = "";
    return;
  }
  const catalog = rootModuleCatalogByKey();
  list.innerHTML = presets.map((preset) => {
    const current = catalog.get(preset.item_key);
    const active = current ? (current.enabled !== 0 && current.enabled !== false) : preset.enabled !== false;
    const price = Number(current?.base_price ?? preset.base_price ?? 0);
    const bounds = [
      preset.min_price !== undefined && preset.min_price !== "" ? `下限 ${Number(preset.min_price)} 點` : "",
      preset.max_price !== undefined && preset.max_price !== "" ? `上限 ${Number(preset.max_price)} 點` : "",
      preset.dynamic_pricing ? "動態定價" : "",
    ].filter(Boolean).join(" · ");
    const currentText = current ? `目前 ${Number(current.base_price || 0)} 點${active ? "" : " · 停用"}` : "尚未建立，儲存後建立";
    return `
      <div class="drive-file-row root-module-pricing-row">
        <div>
          <strong>${sanitize(preset.item_name)}</strong>
          <div class="drive-card-sub">${sanitize(preset.item_key)} · ${sanitize(currentText)}${bounds ? ` · ${sanitize(bounds)}` : ""}</div>
          <div class="drive-card-sub">${sanitize(preset.rationale || "")}</div>
        </div>
        <div class="root-module-pricing-controls">
          <label>
            每次消耗點數
            <input type="number" min="0" step="1" value="${sanitize(String(price))}" data-root-module-pricing-price="${sanitize(preset.item_key)}" />
          </label>
          <label class="root-module-pricing-enabled">
            <input type="checkbox" ${active ? "checked" : ""} data-root-module-pricing-enabled="${sanitize(preset.item_key)}" />
            啟用
          </label>
        </div>
      </div>
    `;
  }).join("");
}

function updateRootModuleConditionalFields() {
  const config = rootModuleQuickConfig($("root-module-settings-overlay")?.dataset.moduleTab || currentModuleTab);
  if (!config) return;
  config.fields.forEach((field) => {
    const row = document.querySelector(`[data-root-module-field-row="${field.id}"]`);
    if (row) row.style.display = rootModuleFieldVisible(field) ? "" : "none";
  });
}

function rootModuleRenderSettingField(field) {
  const source = $(field.id);
  if (!source) return "";
  const proxyId = rootModuleProxyId(field.id);
  const label = sanitize(field.label || source.closest(".field")?.querySelector("label")?.textContent || field.id);
  const hint = field.hint ? `<div class="field-hint">${sanitize(field.hint)}</div>` : "";
  const sourceTag = source.tagName.toLowerCase();
  if (source.type === "checkbox") {
    return `
      <div class="field root-module-setting-field" data-root-module-field-row="${sanitize(field.id)}">
        <label><input type="checkbox" id="${sanitize(proxyId)}" data-root-setting-source="${sanitize(field.id)}" ${source.checked ? "checked" : ""} /> ${label}</label>
        ${hint}
      </div>
    `;
  }
  if (sourceTag === "select") {
    const options = Array.from(source.options || []).map((option) => `
      <option value="${sanitize(option.value)}" ${option.selected ? "selected" : ""}>${sanitize(option.textContent || option.value)}</option>
    `).join("");
    return `
      <div class="field root-module-setting-field" data-root-module-field-row="${sanitize(field.id)}">
        <label for="${sanitize(proxyId)}">${label}</label>
        <select id="${sanitize(proxyId)}" data-root-setting-source="${sanitize(field.id)}">${options}</select>
        ${hint}
      </div>
    `;
  }
  if (sourceTag === "textarea") {
    return `
      <div class="field root-module-setting-field" data-root-module-field-row="${sanitize(field.id)}">
        <label for="${sanitize(proxyId)}">${label}</label>
        <textarea id="${sanitize(proxyId)}" data-root-setting-source="${sanitize(field.id)}" rows="${sanitize(source.getAttribute("rows") || "3")}">${sanitize(source.value || "")}</textarea>
        ${hint}
      </div>
    `;
  }
  const attrs = ["min", "max", "step", "placeholder", "autocomplete"].map((name) => {
    const value = source.getAttribute(name);
    return value == null ? "" : ` ${name}="${sanitize(value)}"`;
  }).join("");
  const type = source.type || "text";
  return `
    <div class="field root-module-setting-field" data-root-module-field-row="${sanitize(field.id)}">
      <label for="${sanitize(proxyId)}">${label}</label>
      <input type="${sanitize(type)}" id="${sanitize(proxyId)}" data-root-setting-source="${sanitize(field.id)}" value="${sanitize(source.value || "")}"${attrs} />
      ${hint}
    </div>
  `;
}

function renderRootModuleSettingsFields(tab) {
  const config = rootModuleQuickConfig(tab);
  const fieldsWrap = $("root-module-settings-fields");
  const note = $("root-module-settings-note");
  if (!fieldsWrap || !config) return;
  const renderedFields = (config.fields || []).map(rootModuleRenderSettingField).filter(Boolean);
  note.textContent = config.note || "只顯示此頁最常用的 root 設定；完整低頻設定仍保留在安全中心。";
  fieldsWrap.innerHTML = renderedFields.length
    ? renderedFields.join("")
    : `<div class="drive-empty">此頁目前沒有專用快速設定。</div>`;
  renderRootModulePricingFields(tab);
  fieldsWrap.querySelectorAll("input, select, textarea").forEach((el) => {
    el.addEventListener("input", updateRootModuleConditionalFields);
    el.addEventListener("change", updateRootModuleConditionalFields);
  });
  updateRootModuleConditionalFields();
}

async function preloadRootModuleSettings(config) {
  const saves = new Set((config.fields || []).map((field) => field.save || "settings"));
  if (saves.has("settings") && typeof loadSettings === "function") await loadSettings();
  if (saves.has("drivePolicy") && typeof loadCloudDriveAdminPolicy === "function") await loadCloudDriveAdminPolicy();
  if (saves.has("trading") && typeof loadRootTradingSettings === "function") await loadRootTradingSettings();
  await loadRootModuleEconomyCatalogIfNeeded(config);
}

async function openRootModuleSettings(tab = currentModuleTab) {
  if (currentUser !== "root") return;
  const config = rootModuleQuickConfig(tab);
  if (!config) return;
  const overlay = ensureRootModuleSettingsModal();
  overlay.dataset.moduleTab = tab;
  $("root-module-settings-title").textContent = `${config.label}設定`;
  $("root-module-settings-subtitle").textContent = "root 快速設定";
  $("root-module-settings-fields").innerHTML = `<div class="drive-empty">設定讀取中...</div>`;
  if ($("root-module-pricing-panel")) $("root-module-pricing-panel").hidden = true;
  const msg = $("root-module-settings-msg");
  if (msg) msg.className = "msg";
  overlay.classList.add("show");
  document.body.classList.add("modal-open");
  try {
    await preloadRootModuleSettings(config);
    renderRootModuleSettingsFields(tab);
    const firstInput = overlay.querySelector("input, select, textarea, button");
    firstInput?.focus?.();
  } catch (err) {
    if (msg) {
      msg.textContent = err?.message || "設定讀取失敗";
      msg.className = "msg show err";
    }
  }
}

function closeRootModuleSettings() {
  const overlay = $("root-module-settings-overlay");
  if (!overlay) return;
  overlay.classList.remove("show");
  document.body.classList.remove("modal-open");
}

function syncRootModuleProxyValues() {
  const overlay = $("root-module-settings-overlay");
  if (!overlay) return;
  overlay.querySelectorAll("[data-root-setting-source]").forEach((proxy) => {
    const source = $(proxy.dataset.rootSettingSource || "");
    if (!source) return;
    if (source.type === "checkbox") source.checked = !!proxy.checked;
    else source.value = proxy.value;
  });
  if (typeof updateComfyuiConnectionModeFields === "function") updateComfyuiConnectionModeFields();
}

async function saveRootModulePricing(config) {
  const presets = rootModulePricingPresets(config);
  if (!presets.length) return 0;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  let saved = 0;
  for (const preset of presets) {
    const payload = rootModulePricingPayloadForKey(preset.item_key);
    if (!payload) continue;
    const res = await apiFetch(API + "/root/economy/catalog", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `${preset.item_name} 扣點設定儲存失敗（HTTP ${res.status}）`);
    rootModuleEconomyCatalogCache = Array.isArray(json.catalog) ? json.catalog : rootModuleEconomyCatalogCache;
    saved += 1;
  }
  renderRootModulePricingFields($("root-module-settings-overlay")?.dataset.moduleTab || currentModuleTab);
  if (typeof loadRootEconomyCatalog === "function") loadRootEconomyCatalog();
  if (typeof loadEconomy === "function") loadEconomy();
  if (typeof refreshComfyuiStatus === "function" && rootModulePricingPresets(config).some((item) => item.category === "comfyui")) {
    refreshComfyuiStatus({ switchAway: false }).catch(() => {});
  }
  return saved;
}

async function saveRootModuleSettings() {
  const overlay = $("root-module-settings-overlay");
  const config = rootModuleQuickConfig(overlay?.dataset.moduleTab || currentModuleTab);
  const msg = $("root-module-settings-msg");
  const saveBtn = $("root-module-settings-save");
  if (!config || !overlay) return;
  const saves = new Set((config.fields || []).map((field) => field.save || "settings"));
  syncRootModuleProxyValues();
  if (saveBtn) saveBtn.disabled = true;
  if (msg) {
    msg.textContent = "儲存中...";
    msg.className = "msg show info";
  }
  try {
    if (saves.has("drivePolicy") && typeof saveCloudDriveAdminPolicy === "function") await saveCloudDriveAdminPolicy();
    if (saves.has("trading") && typeof saveRootTradingSettings === "function") await saveRootTradingSettings();
    if (saves.has("settings") && typeof saveSettings === "function") await saveSettings();
    const pricingSaved = await saveRootModulePricing(config);
    if (msg) {
      msg.textContent = `${config.label}設定已送出${pricingSaved ? `，服務扣點 ${pricingSaved} 項已更新` : ""}`;
      msg.className = "msg show ok";
      scheduleInlineMessageClear(msg, msg.textContent, true);
    }
    syncRootModuleSettingsButtons();
  } catch (err) {
    if (msg) {
      msg.textContent = err?.message || "儲存失敗";
      msg.className = "msg show err";
    }
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", syncRootModuleSettingsButtons);
} else {
  syncRootModuleSettingsButtons();
}
document.addEventListener("hackme:module-changed", syncRootModuleSettingsButtons);
window.syncRootModuleSettingsButtons = syncRootModuleSettingsButtons;
window.openRootModuleSettings = openRootModuleSettings;
