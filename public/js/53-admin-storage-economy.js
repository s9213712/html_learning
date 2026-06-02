/*
 * Root storage and economy catalog admin module.
 *
 * Owns cloud-drive policy controls, root storage capacity/user overrides,
 * service-fee quick pricing presets, and economy catalog editing.
 * Loaded after 50-admin.js so shared admin helpers and state are available.
 */

function cloudDrivePolicyInputId(key) {
  return "s-cd-" + key.replaceAll("_", "-");
}

async function loadCloudDriveAdminPolicy() {
  if (!currentUser || currentUser !== "root") return;
  const rootEl = $("s-cd-require-scan-before-download");
  if (!rootEl) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/cloud-drive/security-policy", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  const msg = $("cloud-drive-policy-msg");
  if (!json.ok) {
    if (msg) {
      msg.textContent = json.msg || "雲端硬碟安全政策讀取失敗";
      msg.style.color = "#ff4f6d";
    }
    return;
  }
  const p = json.policy || {};
  CLOUD_DRIVE_POLICY_BOOL_FIELDS.forEach((key) => {
    const el = $(cloudDrivePolicyInputId(key));
    if (el) el.checked = !!p[key];
  });
  CLOUD_DRIVE_POLICY_INT_FIELDS.forEach((key) => {
    const el = $(cloudDrivePolicyInputId(key));
    if (el) el.value = p[key] ?? 0;
  });
  CLOUD_DRIVE_POLICY_TEXT_FIELDS.forEach((key) => {
    const el = $(cloudDrivePolicyInputId(key));
    if (el) el.value = p[key] || "";
  });
  if ($("s-cd-notes")) $("s-cd-notes").value = p.notes || "";
  if (msg) msg.textContent = "";
}

async function saveCloudDriveAdminPolicy() {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const payload = {};
  CLOUD_DRIVE_POLICY_BOOL_FIELDS.forEach((key) => {
    const el = $(cloudDrivePolicyInputId(key));
    if (el) payload[key] = !!el.checked;
  });
  CLOUD_DRIVE_POLICY_INT_FIELDS.forEach((key) => {
    const el = $(cloudDrivePolicyInputId(key));
    if (el) payload[key] = parseInt(el.value || "0");
  });
  CLOUD_DRIVE_POLICY_TEXT_FIELDS.forEach((key) => {
    const el = $(cloudDrivePolicyInputId(key));
    if (el) payload[key] = el.value || "";
  });
  payload.notes = $("s-cd-notes")?.value || "";
  const res = await apiFetch(API + "/admin/cloud-drive/security-policy", {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(payload)
  });
  const json = await res.json().catch(() => ({}));
  const msg = $("cloud-drive-policy-msg");
  if (msg) {
    msg.textContent = json.ok ? "雲端硬碟安全政策已儲存" : (json.msg || "儲存失敗");
    msg.style.color = json.ok ? "#4caf50" : "#ff4f6d";
  }
}

function rootStorageFormatBytes(bytes) {
  if (typeof formatDriveBytes === "function") return formatDriveBytes(bytes);
  if (bytes === null || bytes === undefined) return "無上限";
  return `${Number(bytes || 0)} bytes`;
}

function rootStorageMbFromBytes(bytes) {
  if (bytes === null || bytes === undefined) return "";
  return Math.round((Number(bytes || 0) / 1024 / 1024) * 100) / 100;
}

function setRootStorageMsg(text, ok = true) {
  const msg = $("root-storage-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
}

function setDriveRootStorageSettingsMsg(text, ok = true) {
  const msg = $("cloud-drive-storage-status");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
}

function renderRootStorageCapacity(capacity) {
  const target = $("root-storage-capacity-summary");
  if (!target) return;
  const audit = capacity && typeof capacity === "object" ? capacity : {};
  const disk = audit.disk && typeof audit.disk === "object" ? audit.disk : {};
  const global = audit.global_capacity && typeof audit.global_capacity === "object" ? audit.global_capacity : {};
  const rows = [
    ["目前 storage root", disk.path || "-"],
    ["磁碟總容量", rootStorageFormatBytes(disk.total_bytes || 0)],
    ["實體剩餘容量", rootStorageFormatBytes(disk.free_bytes || 0)],
    ["安全可承諾容量", rootStorageFormatBytes(disk.safe_free_bytes || 0)],
    ["雲端檔案已用", rootStorageFormatBytes(audit.cloud_used_bytes || 0)],
    ["全站容量上限", global.limit_bytes === null || global.limit_bytes === undefined ? "依磁碟 95%" : rootStorageFormatBytes(global.limit_bytes)],
    ["已承諾用戶容量", rootStorageFormatBytes(audit.committed_total_bytes || 0)],
  ];
  target.innerHTML = rows.map(([label, value]) => `
    <div class="drive-summary-row">
      <span>${sanitize(label)}</span>
      <strong>${sanitize(String(value))}</strong>
    </div>
  `).join("") + `
    <div class="drive-card-sub" style="margin-top:.45rem;color:${audit.ok === false ? "#ffb74d" : "var(--muted)"};">
      狀態：${sanitize(audit.status || "ok")} · 承諾率 ${Number(audit.percent_committed || 0)}%${Array.isArray(audit.reasons) && audit.reasons.length ? ` · ${sanitize(audit.reasons.join(", "))}` : ""}
    </div>
  `;
}

function renderRootStorageUsers(users) {
  const list = $("root-storage-users");
  const select = $("root-storage-user-select");
  if (select) {
    const current = select.value;
    select.innerHTML = (users || []).length
      ? `<option value="">選擇要管理的帳號</option>` + users.map((user) => {
          const label = `${user.username || "user"} · ${rootStorageFormatBytes(user.used_bytes || 0)} / ${rootStorageFormatBytes(user.total_bytes)}`;
          return `<option value="${sanitize(String(user.user_id || ""))}" ${String(user.user_id || "") === current ? "selected" : ""}>${sanitize(label)}</option>`;
        }).join("")
      : `<option value="">沒有帳號資料</option>`;
  }
  if (!list) return;
  if (!users || !users.length) {
    list.innerHTML = `<div class="drive-card-sub">目前沒有可管理的帳號用量資料</div>`;
    return;
  }
  list.innerHTML = users.map((user) => {
    const override = user.override || user.root_override || {};
    const overrideText = override.enabled
      ? `root 直接設定中 · ${sanitize(override.reason || "未填原因")}`
      : "沿用角色/會員等級";
    return `<div class="drive-file-row" data-root-storage-user="${sanitize(String(user.user_id || ""))}">
      <div>
        <strong>${sanitize(user.username || `user #${user.user_id}`)}</strong>
        <div class="drive-card-sub">
          ${sanitize(user.role || "user")} · ${sanitize(user.effective_level || user.member_level || "-")} ·
          ${rootStorageFormatBytes(user.used_bytes || 0)} / ${rootStorageFormatBytes(user.total_bytes)} ·
          ${Number(user.percent_used || 0)}% · ${Number(user.file_count || 0)} 個檔案
        </div>
        <div class="drive-card-sub">${sanitize(overrideText)} · quota source=${sanitize(user.quota_source || "-")}</div>
      </div>
      <button class="btn" type="button" data-root-storage-select="${sanitize(String(user.user_id || ""))}">管理</button>
    </div>`;
  }).join("");
}

function fillRootStorageOverrideForm(userId) {
  const user = rootStorageUsersCache.find((item) => String(item.user_id) === String(userId));
  if (!user) return;
  const override = user.override || {};
  if ($("root-storage-user-select")) $("root-storage-user-select").value = String(user.user_id || "");
  if ($("root-storage-quota-mb")) $("root-storage-quota-mb").value = override.enabled ? rootStorageMbFromBytes(override.quota_bytes) : "";
  if ($("root-storage-max-file-mb")) $("root-storage-max-file-mb").value = override.enabled ? rootStorageMbFromBytes(override.max_file_size_bytes) : "";
  if ($("root-storage-daily-limit")) $("root-storage-daily-limit").value = override.enabled && override.upload_rate_limit_per_day !== null && override.upload_rate_limit_per_day !== undefined ? override.upload_rate_limit_per_day : "";
  if ($("root-storage-can-upload")) {
    const value = override.enabled ? override.can_upload_override : null;
    $("root-storage-can-upload").value = value === null || value === undefined ? "inherit" : String(!!value);
  }
  if ($("root-storage-override-reason")) $("root-storage-override-reason").value = override.enabled ? (override.reason || "") : "";
}

async function loadRootStorageUsers() {
  if (!currentUser || currentUser !== "root") return;
  if (!$("root-storage-users")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/storage/users", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    setRootStorageMsg(json.msg || "root 雲端硬碟管理資料讀取失敗", false);
    return;
  }
  renderRootStorageCapacity(json.storage_capacity || {});
  rootStorageUsersCache = Array.isArray(json.users) ? json.users : [];
  renderRootStorageUsers(rootStorageUsersCache);
  const selected = $("root-storage-user-select")?.value || rootStorageUsersCache[0]?.user_id || "";
  if (selected) fillRootStorageOverrideForm(selected);
}

async function saveDriveRootStorageSettings() {
  if (currentUser !== "root") return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const payload = {
    cloud_drive_storage_root: ($("s-cloud-drive-storage-root")?.value || "").trim(),
    cloud_drive_global_capacity_limit_mb: parseInt($("s-cloud-drive-global-capacity-limit-mb")?.value || "-1", 10),
    server_max_content_mb: parseInt($("s-server-max-content-mb")?.value || "8192", 10),
    remote_download_max_concurrent_global: parseInt($("s-remote-download-max-concurrent-global")?.value || "1", 10) || 1,
    remote_download_max_concurrent_per_user: parseInt($("s-remote-download-max-concurrent-per-user")?.value || "1", 10) || 1,
    bt_download_backend: ($("s-bt-download-backend")?.value || "auto"),
    transmission_rpc_url: ($("s-transmission-rpc-url")?.value || "").trim(),
    transmission_rpc_username: ($("s-transmission-rpc-username")?.value || "").trim(),
  };
  const transmissionRpcPassword = ($("s-transmission-rpc-password")?.value || "").trim();
  if (transmissionRpcPassword) payload.transmission_rpc_password = transmissionRpcPassword;
  const res = await apiFetch(API + "/admin/settings", {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(payload),
  });
  const json = await res.json().catch(() => ({}));
  setDriveRootStorageSettingsMsg(json.ok ? "雲端硬碟儲存設定已儲存" : (json.msg || "儲存失敗"), !!json.ok);
  if (json.ok) {
    await loadSettings();
    await loadRootStorageUsers();
  }
}


async function testTransmissionRpcConnection() {
  if (currentUser !== "root") return;
  const btn = $("transmission-rpc-test-btn");
  const originalText = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "測試中...";
  }
  setDriveRootStorageSettingsMsg("正在測試 Transmission RPC 連線...", true);
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const payload = {
      transmission_rpc_url: ($("s-transmission-rpc-url")?.value || "").trim(),
      transmission_rpc_username: ($("s-transmission-rpc-username")?.value || "").trim(),
    };
    const password = ($("s-transmission-rpc-password")?.value || "").trim();
    if (password) payload.transmission_rpc_password = password;
    const res = await apiFetch(API + "/admin/settings/transmission/test", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload),
    });
    const json = await res.json().catch(() => ({}));
    const suffix = json.version ? `（${json.version}）` : "";
    setDriveRootStorageSettingsMsg(json.ok ? `${json.msg || "Transmission RPC 連線成功"}${suffix}` : (json.msg || "Transmission RPC 連線失敗"), !!json.ok);
  } catch (err) {
    setDriveRootStorageSettingsMsg(err?.message || "Transmission RPC 連線測試失敗", false);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = originalText || "測試 Transmission 連線";
    }
  }
}

async function saveRootStorageOverride() {
  const userId = $("root-storage-user-select")?.value || "";
  if (!userId) {
    setRootStorageMsg("請先選擇帳號", false);
    return;
  }
  const reason = ($("root-storage-override-reason")?.value || "").trim();
  if (!reason) {
    setRootStorageMsg("請填寫覆寫原因", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const payload = {
    enabled: true,
    quota_mb: $("root-storage-quota-mb")?.value || "",
    max_file_size_mb: $("root-storage-max-file-mb")?.value || "",
    upload_rate_limit_per_day: $("root-storage-daily-limit")?.value || "",
    can_upload: $("root-storage-can-upload")?.value || "inherit",
    reason
  };
  const res = await apiFetch(API + `/root/storage/users/${encodeURIComponent(userId)}/quota-override`, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(payload)
  });
  const json = await res.json().catch(() => ({}));
  setRootStorageMsg(json.ok ? "root 直接設定已套用" : (json.msg || "設定失敗"), !!json.ok);
  if (json.ok) await loadRootStorageUsers();
}

async function clearRootStorageOverride() {
  const userId = $("root-storage-user-select")?.value || "";
  if (!userId) {
    setRootStorageMsg("請先選擇帳號", false);
    return;
  }
  if (!confirm("清除此帳號的 root 直接雲端硬碟設定？")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/root/storage/users/${encodeURIComponent(userId)}/quota-override`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  setRootStorageMsg(json.ok ? "root 直接設定已清除" : (json.msg || "清除失敗"), !!json.ok);
  if (json.ok) await loadRootStorageUsers();
}

let rootEconomyCatalogCache = [];
const ROOT_SERVICE_FEE_PRICING_PRESETS = Array.isArray(window.HACKME_SERVICE_FEE_PRICING_PRESETS)
  ? window.HACKME_SERVICE_FEE_PRICING_PRESETS
  : [];

function rootCatalogMsg(text, ok = true) {
  const msg = $("root-catalog-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
}

function rootCatalogStorageGbFromBytes(bytes) {
  if (!bytes) return "";
  return Math.round((Number(bytes || 0) / 1024 / 1024 / 1024) * 100) / 100;
}

function clearRootCatalogForm() {
  if ($("root-catalog-item-key")) $("root-catalog-item-key").value = "";
  if ($("root-catalog-item-name")) $("root-catalog-item-name").value = "";
  if ($("root-catalog-category")) $("root-catalog-category").value = "comfyui";
  if ($("root-catalog-base-price")) $("root-catalog-base-price").value = "1";
  if ($("root-catalog-min-price")) $("root-catalog-min-price").value = "";
  if ($("root-catalog-max-price")) $("root-catalog-max-price").value = "";
  if ($("root-catalog-dynamic-pricing")) $("root-catalog-dynamic-pricing").checked = false;
  if ($("root-catalog-enabled")) $("root-catalog-enabled").checked = true;
  if ($("root-catalog-storage-gb")) $("root-catalog-storage-gb").value = "";
  if ($("root-catalog-duration-days")) $("root-catalog-duration-days").value = "";
  rootCatalogMsg("");
}

function fillRootCatalogForm(itemKey) {
  const item = rootEconomyCatalogCache.find((row) => row.item_key === itemKey);
  if (!item) return;
  const metadata = item.metadata || {};
  if ($("root-catalog-item-key")) $("root-catalog-item-key").value = item.item_key || "";
  if ($("root-catalog-item-name")) $("root-catalog-item-name").value = item.item_name || "";
  if ($("root-catalog-category")) $("root-catalog-category").value = item.category || "custom";
  if ($("root-catalog-base-price")) $("root-catalog-base-price").value = item.base_price ?? 1;
  if ($("root-catalog-min-price")) $("root-catalog-min-price").value = item.min_price ?? "";
  if ($("root-catalog-max-price")) $("root-catalog-max-price").value = item.max_price ?? "";
  if ($("root-catalog-dynamic-pricing")) $("root-catalog-dynamic-pricing").checked = !!item.dynamic_pricing;
  if ($("root-catalog-enabled")) $("root-catalog-enabled").checked = item.enabled !== 0 && item.enabled !== false;
  if ($("root-catalog-storage-gb")) $("root-catalog-storage-gb").value = rootCatalogStorageGbFromBytes(metadata.storage_bytes);
  if ($("root-catalog-duration-days")) $("root-catalog-duration-days").value = metadata.duration_days || "";
  rootCatalogMsg(`正在編輯 ${item.item_key}`);
}

function fillRootCatalogFormFromPreset(item) {
  if (!item) return;
  const metadata = item.metadata || {};
  if ($("root-catalog-item-key")) $("root-catalog-item-key").value = item.item_key || "";
  if ($("root-catalog-item-name")) $("root-catalog-item-name").value = item.item_name || "";
  if ($("root-catalog-category")) $("root-catalog-category").value = item.category || "custom";
  if ($("root-catalog-base-price")) $("root-catalog-base-price").value = item.base_price ?? 1;
  if ($("root-catalog-min-price")) $("root-catalog-min-price").value = item.min_price ?? "";
  if ($("root-catalog-max-price")) $("root-catalog-max-price").value = item.max_price ?? "";
  if ($("root-catalog-dynamic-pricing")) $("root-catalog-dynamic-pricing").checked = !!item.dynamic_pricing;
  if ($("root-catalog-enabled")) $("root-catalog-enabled").checked = item.enabled !== false;
  if ($("root-catalog-storage-gb")) $("root-catalog-storage-gb").value = metadata.storage_bytes ? rootCatalogStorageGbFromBytes(metadata.storage_bytes) : "";
  if ($("root-catalog-duration-days")) $("root-catalog-duration-days").value = metadata.duration_days || "";
  rootCatalogMsg(`已套入建議：${item.item_key}`);
}

function rootPricingPresetPayload(item) {
  return {
    item_key: item.item_key,
    item_name: item.item_name,
    category: item.category,
    base_price: item.base_price,
    min_price: item.min_price || "",
    max_price: item.max_price || "",
    dynamic_pricing: !!item.dynamic_pricing,
    enabled: item.enabled !== false,
    metadata: item.metadata || {},
  };
}

async function saveRootServiceFeePricingPreset(itemKey) {
  if (currentUser !== "root") return;
  const preset = ROOT_SERVICE_FEE_PRICING_PRESETS.find((item) => item.item_key === itemKey);
  if (!preset) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/economy/catalog", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(rootPricingPresetPayload(preset))
  });
  const json = await res.json().catch(() => ({}));
  rootCatalogMsg(json.ok ? `已套用 ${preset.item_key}` : (json.msg || "套用建議定價失敗"), !!json.ok);
  if (json.ok) {
    rootEconomyCatalogCache = Array.isArray(json.catalog) ? json.catalog : [];
    renderRootEconomyCatalog(rootEconomyCatalogCache);
    renderRootServiceFeeQuickPricing();
    if (typeof loadEconomy === "function") loadEconomy();
  }
}

function renderRootServiceFeeQuickPricing() {
  const list = $("root-service-fee-quick-pricing-list");
  if (!list) return;
  const catalog = new Map(rootEconomyCatalogCache.map((item) => [item.item_key, item]));
  list.innerHTML = `
    <div class="drive-file-row billing-catalog-row">
      <div>
        <strong>服務費快速定價</strong>
        <div class="drive-card-sub">建議以低額高頻、曝光較高、資源消耗更高的模型定價；收入列入官方 Treasury，鏈上交易 fee 仍進 BURN。</div>
      </div>
    </div>
    ${ROOT_SERVICE_FEE_PRICING_PRESETS.map((preset) => {
      const current = catalog.get(preset.item_key);
      const currentText = current ? `${Number(current.base_price || 0)} 點${current.enabled ? "" : " · 停用"}` : "尚未建立";
      return `<div class="drive-file-row billing-catalog-row">
        <div>
          <strong>${sanitize(preset.item_name)} · 建議 ${Number(preset.base_price || 0)} 點</strong>
          <div class="drive-card-sub">${sanitize(preset.item_key)} · ${sanitize(preset.category)} · 目前 ${sanitize(currentText)}</div>
          <div class="drive-card-sub">${sanitize(preset.rationale || "")}</div>
        </div>
        <div class="drive-file-actions">
          <button class="btn btn-sm" type="button" data-root-pricing-fill="${sanitize(preset.item_key)}">套入編輯</button>
          <button class="btn btn-sm btn-primary" type="button" data-root-pricing-save="${sanitize(preset.item_key)}">套用建議</button>
        </div>
      </div>`;
    }).join("")}
  `;
  list.querySelectorAll("[data-root-pricing-fill]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const preset = ROOT_SERVICE_FEE_PRICING_PRESETS.find((item) => item.item_key === btn.dataset.rootPricingFill);
      fillRootCatalogFormFromPreset(preset);
    });
  });
  list.querySelectorAll("[data-root-pricing-save]").forEach((btn) => {
    btn.addEventListener("click", () => saveRootServiceFeePricingPreset(btn.dataset.rootPricingSave || ""));
  });
}

function renderRootEconomyCatalog(items) {
  const list = $("root-catalog-list");
  if (!list) return;
  if (!items || !items.length) {
    list.innerHTML = `<div class="drive-empty">尚無計費項目</div>`;
    return;
  }
  list.innerHTML = items.map((item) => {
    const metadata = item.metadata || {};
    const enabled = item.enabled !== 0 && item.enabled !== false;
    const storageText = item.category === "cloud_drive"
      ? ` · ${rootCatalogStorageGbFromBytes(metadata.storage_bytes)} GB / ${metadata.duration_days || "-"} 天`
      : "";
    return `<div class="drive-file-row billing-catalog-row">
      <div>
        <strong>${sanitize(item.item_name || item.item_key)}</strong>
        <div class="drive-card-sub">${sanitize(item.item_key || "")}</div>
        <div class="drive-card-sub">${sanitize(item.category || "-")} · ${Number(item.base_price || 0)} 點${storageText} · ${enabled ? "啟用" : "停用"}</div>
      </div>
      <button class="btn" type="button" data-root-catalog-edit="${sanitize(item.item_key || "")}">編輯</button>
    </div>`;
  }).join("");
  list.querySelectorAll("[data-root-catalog-edit]").forEach((btn) => {
    btn.addEventListener("click", () => fillRootCatalogForm(btn.dataset.rootCatalogEdit || ""));
  });
}

async function loadRootEconomyCatalog() {
  if (currentUser !== "root" || !$("root-catalog-list")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/economy/catalog", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    rootCatalogMsg(json.msg || "計費項目讀取失敗", false);
    return;
  }
  rootEconomyCatalogCache = Array.isArray(json.catalog) ? json.catalog : [];
  renderRootEconomyCatalog(rootEconomyCatalogCache);
  renderRootServiceFeeQuickPricing();
}

async function saveRootEconomyCatalogItem() {
  if (currentUser !== "root") return;
  const category = $("root-catalog-category")?.value || "custom";
  const metadata = {};
  if (category === "cloud_drive") {
    const gb = Number($("root-catalog-storage-gb")?.value || 0);
    const days = Number($("root-catalog-duration-days")?.value || 0);
    metadata.storage_bytes = Math.round(gb * 1024 * 1024 * 1024);
    metadata.duration_days = Math.round(days);
    metadata.label = $("root-catalog-item-name")?.value || "";
  }
  const payload = {
    item_key: ($("root-catalog-item-key")?.value || "").trim(),
    item_name: ($("root-catalog-item-name")?.value || "").trim(),
    category,
    base_price: Number($("root-catalog-base-price")?.value || 0),
    min_price: $("root-catalog-min-price")?.value || "",
    max_price: $("root-catalog-max-price")?.value || "",
    dynamic_pricing: !!$("root-catalog-dynamic-pricing")?.checked,
    enabled: !!$("root-catalog-enabled")?.checked,
    metadata,
  };
  if (!payload.item_key || !payload.item_name) {
    rootCatalogMsg("請填項目 key 與顯示名稱", false);
    return;
  }
  if (payload.category === "cloud_drive" && (!metadata.storage_bytes || !metadata.duration_days)) {
    rootCatalogMsg("雲端容量商品必須填容量 GB 與有效天數", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/economy/catalog", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(payload)
  });
  const json = await res.json().catch(() => ({}));
  rootCatalogMsg(json.ok ? "計費項目已儲存" : (json.msg || "儲存失敗"), !!json.ok);
  if (json.ok) {
    rootEconomyCatalogCache = Array.isArray(json.catalog) ? json.catalog : [];
    renderRootEconomyCatalog(rootEconomyCatalogCache);
    renderRootServiceFeeQuickPricing();
    if (typeof loadEconomy === "function") loadEconomy();
  }
}
