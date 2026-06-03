function formatDriveBytes(bytes) {
  if (bytes === null || bytes === undefined) return "無上限";
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function formatDriveSpeed(bytesPerSecond) {
  const value = Number(bytesPerSecond || 0);
  if (!Number.isFinite(value) || value <= 0) return "";
  return `${formatDriveBytes(value)}/s`;
}

const DRIVE_PRIVACY_MODE_LABELS = {
  standard_plain: "一般檔案",
  server_encrypted: "伺服器端加密",
  e2ee: "端到端加密",
};

const DRIVE_PRIVACY_MODE_DESCRIPTIONS = {
  standard_plain: "伺服器可讀明文並掃毒，預覽與分享支援最完整",
  server_encrypted: "磁碟上是密文，伺服器可暫時解密掃毒、預覽與下載明文",
  e2ee: "瀏覽器端加密，站方無法讀取，預覽與掃毒受限",
};

const DRIVE_PRIVACY_MODE_COMPARISON = [
  {
    mode: "standard_plain",
    bestFor: "一般檔案、附件、相簿、分享",
    serverAccess: "可讀明文",
    scan: "伺服器掃毒",
    preview: "最完整",
    keyRisk: "無本機金鑰風險",
  },
  {
    mode: "server_encrypted",
    bestFor: "降低磁碟或備份外洩風險",
    serverAccess: "可暫時解密",
    scan: "解密後伺服器掃毒",
    preview: "通過政策後可預覽",
    keyRisk: "伺服器金鑰遺失會影響復原",
  },
  {
    mode: "e2ee",
    bestFor: "高度私密保存",
    serverAccess: "不可讀明文",
    scan: "只能檢查密文/metadata；可附本機回報",
    preview: "不提供伺服器預覽",
    keyRisk: "清除瀏覽器金鑰後可能無法解密",
  },
];

const DRIVE_E2EE_PASSPHRASE_WRAPPER = "browser_passphrase_pbkdf2_v2";
const DRIVE_E2EE_PBKDF2_ITERATIONS = 310000;
const DRIVE_E2EE_PREVIEW_NO_RECENT_PASSWORD = "無法預覽：本次登入尚無最近輸入過的 E2EE 密碼可試。";
const DRIVE_E2EE_PREVIEW_DECRYPT_FAILED = "無法預覽：最近輸入過的 E2EE 密碼無法解開此檔案。";
const DRIVE_SHARE_FRAGMENT_STORAGE_KEY = "hackme_web.drive_share_fragments";
const DRIVE_E2EE_BROWSER_SESSION_KEY_PREFIX = "hackme_web.drive_e2ee_session_passphrase.";
const DRIVE_SHARE_COPY_RESET_MS = 1800;
const driveE2eeSessionPassphrases = new Map();
const driveE2eeRecentSessionPassphrases = [];
const ATTACHMENT_FILE_SELECT_IDS = [
  "chat-attachment-existing-file-id",
  "dm-attachment-existing-file-id",
  "announcement-attachment-existing-file-id",
];

let driveTransferRows = [];
let driveTaskCenterLocalJobs = [];
let driveAttachmentFileOptions = [];
let driveAttachmentFileOptionsLoadedAt = 0;
let driveStorageUpgradeCatalog = [];
let driveStorageUpgradeCanPurchase = false;
let driveStorageUpgradeMessage = "";
let driveRemoteDownloadCapabilities = { direct: true, bt_magnet: false, bt_file: false };
let driveRemoteDownloadCapabilitiesRetryTimer = null;
let driveRemoteDownloadCapabilitiesRetryCount = 0;
let driveLatestQuota = null;
let driveDashboardInFlight = null;
let driveDashboardLoadedAt = 0;
let chatAttachmentUploadInFlight = false;
let chatAttachmentUploadFingerprint = "";
const driveRemotePollingTaskIds = new Set();
const DRIVE_TRANSFER_COMPLETED_VISIBLE_MS = 6000;
const DRIVE_TRANSFER_FAILED_VISIBLE_MS = 15000;
const DRIVE_TASK_CENTER_LOCAL_MAX = 80;
const DRIVE_REMOTE_STATUS_RETRY_LIMIT = 12;
const DRIVE_REMOTE_CAPABILITY_RETRY_LIMIT = 4;
const DRIVE_DASHBOARD_LAZY_REFRESH_MS = 10000;
const DRIVE_RESUMABLE_UPLOAD_THRESHOLD_BYTES = 8 * 1024 * 1024;
const DRIVE_RESUMABLE_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024;
const DRIVE_RESUMABLE_UPLOAD_STORAGE_PREFIX = "hackme_web.resumable_upload.";

function driveDashboardLazyRefreshMs() {
  if (typeof configRefreshSeconds === "function") {
    return Math.round(configRefreshSeconds("drive_dashboard_lazy_refresh_seconds", DRIVE_DASHBOARD_LAZY_REFRESH_MS / 1000, 1, 300) * 1000);
  }
  return DRIVE_DASHBOARD_LAZY_REFRESH_MS;
}

function isDriveTransferActive(item = {}) {
  return !["completed", "failed", "paused", "cancelled", "waiting_resume"].includes(String(item.status || ""));
}

function hasActiveDriveBrowserUpload() {
  return driveTransferRows.some((item) => ["upload", "folder_upload", "resumable_upload"].includes(item.kind) && isDriveTransferActive(item));
}

function syncDriveTransferIdleSuspend() {
  if (typeof setInactivitySuspendState !== "function") return;
  const active = driveTransferRows.some(isDriveTransferActive);
  setInactivitySuspendState("drive_transfer", active, "雲端硬碟傳輸中");
}

if (typeof window !== "undefined" && !window.__driveTransferBeforeUnloadBound) {
  window.__driveTransferBeforeUnloadBound = true;
  window.addEventListener("beforeunload", (event) => {
    if (!hasActiveDriveBrowserUpload()) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

function drivePrivacyModeLabel(mode) {
  return DRIVE_PRIVACY_MODE_LABELS[mode] || mode || "-";
}

function drivePrivacyModeDescription(mode) {
  return DRIVE_PRIVACY_MODE_DESCRIPTIONS[mode] || "";
}

function renderDrivePrivacyModeComparison() {
  const target = $("drive-privacy-mode-comparison");
  if (!target) return;
  target.innerHTML = `
    <div class="drive-mode-table-wrap">
      <table class="drive-mode-table">
        <thead>
          <tr>
            <th>模式</th>
            <th>適合用途</th>
            <th>站方能否讀取</th>
            <th>安全檢查</th>
            <th>預覽/下載</th>
            <th>注意事項</th>
          </tr>
        </thead>
        <tbody>
          ${DRIVE_PRIVACY_MODE_COMPARISON.map((item) => `
            <tr>
              <td><strong>${sanitize(drivePrivacyModeLabel(item.mode))}</strong><span>${sanitize(item.mode)}</span></td>
              <td>${sanitize(item.bestFor)}</td>
              <td>${sanitize(item.serverAccess)}</td>
              <td>${sanitize(item.scan)}</td>
              <td>${sanitize(item.preview)}</td>
              <td>${sanitize(item.keyRisk)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
    <div class="drive-card-sub">結論：要預覽、掃毒、分享就用「一般檔案」；要防磁碟/備份外洩但保留伺服器功能用「伺服器端加密」；真正不想讓站方讀內容才用 E2EE。</div>
  `;
}

function attachmentFileDisplayName(file) {
  return file?.original_filename_plain_for_public || file?.display_name || file?.filename || file?.id || file?.file_id || "雲端檔案";
}

function attachmentFileOptionLabel(file) {
  const id = file?.id || file?.file_id || "";
  const name = attachmentFileDisplayName(file);
  const size = typeof formatDriveBytes === "function" ? formatDriveBytes(file?.size_bytes || 0) : `${Number(file?.size_bytes || 0)} bytes`;
  const scan = file?.scan_status || "-";
  const risk = file?.risk_level || "-";
  return `${name} · ${size} · scan=${scan} · risk=${risk} · #${id}`;
}

function renderAttachmentFileSelects(files = driveAttachmentFileOptions) {
  const rows = Array.isArray(files) ? files : [];
  ATTACHMENT_FILE_SELECT_IDS.forEach((id) => {
    const select = $(id);
    if (!select) return;
    const previous = select.value || "";
    if (!rows.length) {
      select.innerHTML = `<option value="">目前沒有可選擇的雲端檔案</option>`;
      return;
    }
    select.innerHTML = `<option value="">請選擇雲端檔案</option>` + rows.map((file) => {
      const fileId = file.id || file.file_id || "";
      return `<option value="${sanitize(fileId)}">${sanitize(attachmentFileOptionLabel(file))}</option>`;
    }).join("");
    if (previous && rows.some((file) => String(file.id || file.file_id || "") === previous)) {
      select.value = previous;
    }
  });
}

async function ensureAttachmentFileOptionsLoaded({ force = false } = {}) {
  if (!currentUser || !canAccessModule("privacy_uploads")) return [];
  const fresh = driveAttachmentFileOptionsLoadedAt && Date.now() - driveAttachmentFileOptionsLoadedAt < 30000;
  if (!force && fresh) {
    renderAttachmentFileSelects();
    return driveAttachmentFileOptions;
  }
  ATTACHMENT_FILE_SELECT_IDS.forEach((id) => {
    const select = $(id);
    if (select && !select.value) select.innerHTML = `<option value="">讀取雲端檔案中...</option>`;
  });
  const csrf = await fetchCsrfToken();
  const res = await apiFetch(API + "/cloud-drive/files", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    ATTACHMENT_FILE_SELECT_IDS.forEach((id) => {
      const select = $(id);
      if (select) select.innerHTML = `<option value="">雲端檔案讀取失敗</option>`;
    });
    return [];
  }
  driveAttachmentFileOptions = Array.isArray(json.files) ? json.files : [];
  driveAttachmentFileOptionsLoadedAt = Date.now();
  renderAttachmentFileSelects();
  return driveAttachmentFileOptions;
}

function isDriveE2eeMode(mode) {
  return String(mode || "") === "e2ee";
}

function isDriveServerEncryptedMode(mode) {
  return String(mode || "") === "server_encrypted";
}

function drivePostUploadProcessingMessage(mode) {
  if (isDriveServerEncryptedMode(mode)) return "上傳完成，伺服器端加密、儲存與掃描中";
  if (isDriveE2eeMode(mode)) return "上傳完成，伺服器儲存密文與掃描中";
  return "上傳完成，伺服器儲存與掃描中";
}

function shouldUseDriveResumableUpload(blob) {
  return Number(blob?.size || 0) >= DRIVE_RESUMABLE_UPLOAD_THRESHOLD_BYTES;
}

function driveResumableUploadKey({ file, blob, target = "cloud_drive", virtualPath = "", privacyMode = "standard_plain" } = {}) {
  if (isDriveE2eeMode(privacyMode)) return "";
  const name = file?.name || blob?.name || "";
  const size = Number(blob?.size ?? file?.size ?? 0);
  const modified = Number(file?.lastModified || 0);
  const raw = [currentUser || "", target, privacyMode, name, size, modified, virtualPath || ""].join("|");
  return `${DRIVE_RESUMABLE_UPLOAD_STORAGE_PREFIX}${btoa(unescape(encodeURIComponent(raw))).replace(/=+$/g, "")}`;
}

function rememberDriveResumableUpload(key, sessionId) {
  if (!key || !sessionId) return;
  try {
    localStorage.setItem(key, sessionId);
  } catch (_) {}
}

function forgetDriveResumableUpload(key) {
  if (!key) return;
  try {
    localStorage.removeItem(key);
  } catch (_) {}
}

function rememberedDriveResumableUpload(key) {
  if (!key) return "";
  try {
    return localStorage.getItem(key) || "";
  } catch (_) {
    return "";
  }
}

function driveResumableUploadCanResumeSession(session = {}, { filename = "", totalBytes = 0, target = "cloud_drive", privacyMode = "standard_plain" } = {}) {
  if (!session || !session.session_id) return false;
  const status = String(session.status || "");
  if (["completed", "aborted", "cancelled"].includes(status)) return false;
  return String(session.filename || "") === String(filename || "")
    && Number(session.total_bytes || 0) === Number(totalBytes || 0)
    && String(session.target || "cloud_drive") === String(target || "cloud_drive")
    && String(session.privacy_mode || "standard_plain") === String(privacyMode || "standard_plain");
}

async function findDriveResumableUploadSessionForBlob({ filename = "", totalBytes = 0, target = "cloud_drive", privacyMode = "standard_plain", csrf = "" } = {}) {
  try {
    const sessions = await loadDriveResumableUploadSessions({ csrf });
    return sessions
      .filter((session) => driveResumableUploadCanResumeSession(session, { filename, totalBytes, target, privacyMode }))
      .sort((a, b) => Number(b.received_bytes || 0) - Number(a.received_bytes || 0))[0] || null;
  } catch (_) {
    return null;
  }
}

function driveEncryptedUploadFields(encrypted = {}) {
  return {
    encrypted_metadata: encrypted.encrypted_metadata || "",
    encrypted_file_key: encrypted.encrypted_file_key || "",
    wrapped_by: encrypted.wrapped_by || "",
    ciphertext_sha256: encrypted.ciphertext_sha256 || "",
    encryption_algorithm: encrypted.encryption_algorithm || "",
    encryption_version: encrypted.encryption_version || "",
    nonce: encrypted.nonce || "",
  };
}

function appendDriveUploadFields(form, fields = {}) {
  Object.entries(fields || {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") form.append(key, value);
  });
}

function driveBytesToBase64(bytes) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
  let binary = "";
  for (let i = 0; i < view.length; i += 1) binary += String.fromCharCode(view[i]);
  return btoa(binary);
}

function driveBytesToBase64Url(bytes) {
  return driveBytesToBase64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function driveBase64ToBytes(value) {
  const binary = atob(String(value || ""));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function driveBufferToHex(buffer) {
  return Array.from(new Uint8Array(buffer || [])).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function driveRandomNonce(length = 12) {
  const nonce = new Uint8Array(length);
  window.crypto.getRandomValues(nonce);
  return nonce;
}

function driveE2eeModeSelected() {
  return isDriveE2eeMode($("drive-upload-privacy-mode")?.value || "");
}

function drivePasswordStrengthPolicyEnabled() {
  const liveSetting = $("s-password-strength-policy-enabled");
  if (liveSetting) return !!liveSetting.checked;
  const configs = [];
  if (typeof siteConfig !== "undefined" && siteConfig && typeof siteConfig === "object") configs.push(siteConfig);
  if (typeof window !== "undefined") {
    configs.push(window.siteConfig || {}, window.publicConfig || {}, window.APP_CONFIG || {});
  }
  for (const cfg of configs) {
    if (cfg && Object.prototype.hasOwnProperty.call(cfg, "password_strength_policy_enabled")) {
      return cfg.password_strength_policy_enabled !== false;
    }
  }
  return true;
}

function syncDriveE2eePassphrasePolicyHints() {
  const strict = drivePasswordStrengthPolicyEnabled();
  const sessionHint = strict ? "依目前密碼安全政策" : "安全政策已關閉，只需非空白密碼";
  const uploadHint = strict ? "至少 8 個字元並提高複雜度" : "安全政策已關閉，只需非空白密碼";
  const sessionInput = $("drive-e2ee-session-passphrase");
  const uploadInput = $("drive-upload-mode-passphrase");
  const legacyInput = $("drive-e2ee-passphrase");
  if (sessionInput) sessionInput.placeholder = sessionHint;
  if (uploadInput) uploadInput.placeholder = uploadHint;
  if (legacyInput) legacyInput.placeholder = uploadHint;
}

function driveE2eeBrowserSessionKey() {
  return `${DRIVE_E2EE_BROWSER_SESSION_KEY_PREFIX}${currentUserId || "anon"}`;
}

function getDriveE2eeBrowserSessionPassphrase() {
  try {
    return sessionStorage.getItem(driveE2eeBrowserSessionKey()) || "";
  } catch (_) {
    return "";
  }
}

function rememberDriveE2eeBrowserSessionPassphrase(passphrase) {
  const normalized = String(passphrase || "");
  if (!normalized) return;
  try {
    sessionStorage.setItem(driveE2eeBrowserSessionKey(), normalized);
  } catch (_) {}
}

function clearDriveE2eeBrowserSessionPassphrase() {
  try {
    sessionStorage.removeItem(driveE2eeBrowserSessionKey());
  } catch (_) {}
}

function setDriveE2eeSessionPassphraseFromUi() {
  syncDriveE2eePassphrasePolicyHints();
  const input = $("drive-e2ee-session-passphrase");
  const msg = $("drive-e2ee-session-msg");
  const passphrase = input?.value || "";
  const policy = validateDriveE2eePassphraseStrength(passphrase);
  if (!policy.ok) {
    if (msg) flash(msg, policy.msg, false);
    return;
  }
  rememberDriveE2eeRecentSessionPassphrase(passphrase);
  rememberDriveE2eeBrowserSessionPassphrase(passphrase);
  if (input) input.value = "";
  if (msg) flash(msg, "已套用到本次瀏覽器工作階段；登出或清除後會移除。", true);
}

function clearDriveE2eeSessionPassphraseFromUi() {
  clearDriveE2eeSessionPassphrases();
  const input = $("drive-e2ee-session-passphrase");
  const msg = $("drive-e2ee-session-msg");
  if (input) input.value = "";
  if (msg) flash(msg, "已清除本次瀏覽器 E2EE 密碼。", true);
}

function askDriveUploadPrivacyOptions({ allowE2ee = true, title = "選擇隱私模式" } = {}) {
  return new Promise((resolve) => {
    syncDriveE2eePassphrasePolicyHints();
    const overlay = $("drive-upload-mode-overlay");
    const titleEl = $("drive-upload-mode-title");
    const e2eeChoice = $("drive-upload-mode-e2ee-choice");
    const e2eeFields = $("drive-upload-mode-e2ee-fields");
    const passphraseInput = $("drive-upload-mode-passphrase");
    const passphraseConfirm = $("drive-upload-mode-passphrase-confirm");
    const msg = $("drive-upload-mode-msg");
    const confirmBtn = $("drive-upload-mode-confirm-btn");
    const cancelBtn = $("drive-upload-mode-cancel-btn");
    if (!overlay || !confirmBtn || !cancelBtn) {
      resolve({ privacyMode: "standard_plain", passphrase: "" });
      return;
    }
    const radios = Array.from(document.querySelectorAll("input[name='drive-upload-mode-choice']"));
    const setMsg = (text = "", ok = false) => {
      if (!msg) return;
      msg.textContent = text;
      msg.className = text ? `msg ${ok ? "ok" : "err"}` : "msg";
    };
    const selectedMode = () => radios.find((radio) => radio.checked)?.value || "standard_plain";
    const sync = () => {
      const isE2ee = selectedMode() === "e2ee";
      if (e2eeFields) e2eeFields.style.display = isE2ee ? "" : "none";
      if (isE2ee && passphraseInput && !passphraseInput.value) {
        const remembered = getDriveE2eeBrowserSessionPassphrase();
        if (remembered) {
          passphraseInput.value = remembered;
          if (passphraseConfirm) passphraseConfirm.value = remembered;
          setMsg("已套用本次瀏覽器工作階段暫存的 E2EE 密碼。", true);
        }
      }
    };
    const cleanup = (value) => {
      confirmBtn.removeEventListener("click", onConfirm);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onOverlayClick);
      document.removeEventListener("keydown", onKeyDown);
      radios.forEach((radio) => radio.removeEventListener("change", sync));
      overlay.classList.remove("show");
      overlay.setAttribute("aria-hidden", "true");
      document.body.classList.remove("modal-open");
      resolve(value);
    };
    const onCancel = () => cleanup(null);
    const onOverlayClick = (event) => {
      if (event.target === overlay) cleanup(null);
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") cleanup(null);
    };
    const onConfirm = () => {
      const privacyMode = selectedMode();
      const options = { privacyMode, passphrase: "" };
      if (privacyMode === "e2ee") {
        const passphrase = passphraseInput?.value || "";
        const confirm = passphraseConfirm?.value || "";
        const policy = validateDriveE2eePassphraseStrength(passphrase);
        if (!policy.ok) {
          setMsg(policy.msg);
          return;
        }
        if (passphrase !== confirm) {
          setMsg("兩次輸入的 E2EE 檔案加密密碼不一致");
          return;
        }
        options.passphrase = passphrase;
        rememberDriveE2eeRecentSessionPassphrase(passphrase);
        rememberDriveE2eeBrowserSessionPassphrase(passphrase);
      }
      cleanup(options);
    };
    if (titleEl) titleEl.textContent = title;
    if (e2eeChoice) e2eeChoice.style.display = allowE2ee ? "" : "none";
    radios.forEach((radio) => {
      radio.checked = radio.value === "standard_plain";
      radio.disabled = radio.value === "e2ee" && !allowE2ee;
      radio.addEventListener("change", sync);
    });
    if (passphraseInput) passphraseInput.value = "";
    if (passphraseConfirm) passphraseConfirm.value = "";
    setMsg("");
    sync();
    confirmBtn.addEventListener("click", onConfirm);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onOverlayClick);
    document.addEventListener("keydown", onKeyDown);
    overlay.classList.add("show");
    overlay.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    setTimeout(() => {
      const checked = radios.find((radio) => radio.checked && !radio.disabled);
      (checked || confirmBtn).focus?.();
    }, 0);
  });
}

function updateDriveE2eePassphraseVisibility() {
  const field = $("drive-e2ee-passphrase-field");
  const isE2ee = driveE2eeModeSelected();
  if (field) field.style.display = isE2ee ? "" : "none";
}

function getDriveE2eeUploadPassphrase() {
  const passphrase = $("drive-e2ee-passphrase")?.value || "";
  const confirm = $("drive-e2ee-passphrase-confirm")?.value || "";
  if (!passphrase) throw new Error("請輸入 E2EE 檔案加密密碼");
  const policy = validateDriveE2eePassphraseStrength(passphrase);
  if (!policy.ok) throw new Error(policy.msg);
  if (passphrase !== confirm) throw new Error("兩次輸入的 E2EE 檔案加密密碼不一致");
  return passphrase;
}

function clearDriveE2eeUploadPassphrase() {
  if ($("drive-e2ee-passphrase")) $("drive-e2ee-passphrase").value = "";
  if ($("drive-e2ee-passphrase-confirm")) $("drive-e2ee-passphrase-confirm").value = "";
}

function driveE2eeSessionKey(fileId) {
  return `${currentUserId || "anon"}:${String(fileId || "")}`;
}

function driveE2eeKnownFileIds(fileId) {
  const ids = new Set();
  const addId = (value) => {
    const normalized = String(value || "").trim();
    if (normalized) ids.add(normalized);
  };
  addId(fileId);
  const known = findKnownDriveFile(fileId);
  if (known) {
    addId(known.id);
    addId(known.file_id);
    addId(known.storage_file_id);
  }
  return Array.from(ids);
}

function clearDriveE2eeSessionPassphrases() {
  driveE2eeSessionPassphrases.clear();
  driveE2eeRecentSessionPassphrases.length = 0;
  clearDriveE2eeBrowserSessionPassphrase();
}

function hasActiveDriveUploads() {
  const active = new Set(["queued", "running", "uploading", "completing", "waiting_resume"]);
  return driveTransferRows.some((item) => {
    const kind = String(item?.kind || "");
    if (!["upload", "folder_upload", "resumable_upload"].includes(kind)) return false;
    return active.has(String(item?.status || ""));
  });
}

window.clearDriveE2eeSessionPassphrases = clearDriveE2eeSessionPassphrases;
window.hasActiveDriveUploads = hasActiveDriveUploads;

function validateDriveE2eePassphraseStrength(passphrase) {
  const value = String(passphrase || "");
  if (!drivePasswordStrengthPolicyEnabled()) {
    return {
      ok: value.length > 0 && value.length <= 128,
      score: value ? 4 : 0,
      msg: value ? "OK" : "E2EE 密碼不可為空",
    };
  }
  const checks = {
    length8: value.length >= 8,
    lower: /[a-z]/.test(value),
    upper: /[A-Z]/.test(value),
    digit: /\d/.test(value),
    symbol: /[!@#$%^&*()_+\-=[\]{};':"\\|,.<>/?]/.test(value),
    length12: value.length >= 12,
  };
  let score = 0;
  if (checks.length8) score += 1;
  if (checks.lower && checks.upper) score += 1;
  if (checks.digit) score += 1;
  if (checks.symbol) score += 1;
  if (checks.length12 && score < 4) score += 1;
  const missing = [];
  if (!checks.length8) missing.push("至少 8 個字元");
  if (!(checks.lower && checks.upper)) missing.push("同時包含大小寫英文字母");
  if (!checks.digit) missing.push("包含數字");
  if (!checks.symbol) missing.push("包含符號");
  return {
    ok: score >= 3,
    score,
    msg: score >= 3 ? "OK" : `E2EE 密碼強度不足（${score}/4）：${missing.join("、") || "提高密碼複雜度"}`,
  };
}

function forgetDriveE2eeSessionPassphrase(fileId) {
  driveE2eeKnownFileIds(fileId).forEach((id) => {
    driveE2eeSessionPassphrases.delete(driveE2eeSessionKey(id));
  });
}

function getRememberedDriveE2eeSessionPassphrase(fileId) {
  for (const id of driveE2eeKnownFileIds(fileId)) {
    const key = driveE2eeSessionKey(id);
    if (driveE2eeSessionPassphrases.has(key)) {
      return driveE2eeSessionPassphrases.get(key);
    }
  }
  return "";
}

function rememberDriveE2eeRecentSessionPassphrase(passphrase) {
  const normalized = String(passphrase || "");
  if (!normalized) return;
  const existing = driveE2eeRecentSessionPassphrases.indexOf(normalized);
  if (existing >= 0) driveE2eeRecentSessionPassphrases.splice(existing, 1);
  driveE2eeRecentSessionPassphrases.unshift(normalized);
  if (driveE2eeRecentSessionPassphrases.length > 4) {
    driveE2eeRecentSessionPassphrases.length = 4;
  }
}

function getDriveE2eeSessionPassphraseCandidates(fileId) {
  const remembered = getRememberedDriveE2eeSessionPassphrase(fileId);
  const candidates = [];
  const addCandidate = (value) => {
    const normalized = String(value || "");
    if (!normalized || candidates.includes(normalized)) return;
    candidates.push(normalized);
  };
  addCandidate(remembered);
  addCandidate(getDriveE2eeBrowserSessionPassphrase());
  driveE2eeRecentSessionPassphrases.forEach(addCandidate);
  return candidates;
}

function rememberDriveE2eeSessionPassphrase(fileId, passphrase) {
  if (!passphrase) return;
  driveE2eeKnownFileIds(fileId).forEach((id) => {
    driveE2eeSessionPassphrases.set(driveE2eeSessionKey(id), passphrase);
  });
  rememberDriveE2eeBrowserSessionPassphrase(passphrase);
  rememberDriveE2eeRecentSessionPassphrase(passphrase);
}

async function getDriveE2eeSessionPassphrase(fileId, promptText, { force = false, allowPrompt = false } = {}) {
  if (!force) {
    const remembered = getRememberedDriveE2eeSessionPassphrase(fileId);
    if (remembered) return remembered;
  }
  if (!allowPrompt) return "";
  const passphrase = await askDriveE2eePassphrase(promptText);
  return passphrase || "";
}

async function deriveDriveE2eePassphraseKey(passphrase, salt, iterations = DRIVE_E2EE_PBKDF2_ITERATIONS) {
  if (!window.crypto?.subtle) {
    throw new Error("此瀏覽器不支援端到端加密上傳，請改用私密檔案或換用支援 WebCrypto 的瀏覽器。");
  }
  const material = await window.crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(passphrase || ""),
    "PBKDF2",
    false,
    ["deriveKey"]
  );
  return window.crypto.subtle.deriveKey(
    {
      name: "PBKDF2",
      hash: "SHA-256",
      salt: salt instanceof Uint8Array ? salt : driveBase64ToBytes(salt),
      iterations: Math.max(100000, Number(iterations || DRIVE_E2EE_PBKDF2_ITERATIONS)),
    },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"]
  );
}

async function encryptDriveJsonMetadata(fileKey, payload) {
  const nonce = driveRandomNonce();
  const encoded = new TextEncoder().encode(JSON.stringify(payload || {}));
  const ciphertext = await window.crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, fileKey, encoded);
  return JSON.stringify({
    alg: "AES-GCM",
    v: 1,
    nonce: driveBytesToBase64(nonce),
    ciphertext: driveBytesToBase64(ciphertext),
  });
}

async function wrapDriveFileKey(fileKey, passphrase) {
  const rawKey = await window.crypto.subtle.exportKey("raw", fileKey);
  const salt = driveRandomNonce(16);
  const nonce = driveRandomNonce();
  const wrappingKey = await deriveDriveE2eePassphraseKey(passphrase, salt);
  const wrapped = await window.crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, wrappingKey, rawKey);
  return JSON.stringify({
    alg: "AES-GCM",
    v: 2,
    wrapped_by: DRIVE_E2EE_PASSPHRASE_WRAPPER,
    kdf: "PBKDF2-SHA256",
    iterations: DRIVE_E2EE_PBKDF2_ITERATIONS,
    salt: driveBytesToBase64(salt),
    nonce: driveBytesToBase64(nonce),
    ciphertext: driveBytesToBase64(wrapped),
  });
}

async function unwrapDriveFileKey(encryptedFileKey, passphrase) {
  const envelope = JSON.parse(encryptedFileKey || "{}");
  if (envelope.wrapped_by !== DRIVE_E2EE_PASSPHRASE_WRAPPER || !envelope.salt) {
    throw new Error("此檔案使用舊版本機 vault key 包裝，無法用密碼解密；請重新上傳為新版 E2EE。");
  }
  const wrappingKey = await deriveDriveE2eePassphraseKey(passphrase, envelope.salt, envelope.iterations);
  const rawKey = await window.crypto.subtle.decrypt(
    { name: "AES-GCM", iv: driveBase64ToBytes(envelope.nonce) },
    wrappingKey,
    driveBase64ToBytes(envelope.ciphertext)
  );
  return window.crypto.subtle.importKey("raw", rawKey, { name: "AES-GCM" }, true, ["encrypt", "decrypt"]);
}

async function decryptDriveJsonMetadata(fileKey, encryptedMetadata) {
  if (!encryptedMetadata) return {};
  const envelope = JSON.parse(encryptedMetadata || "{}");
  const plaintext = await window.crypto.subtle.decrypt(
    { name: "AES-GCM", iv: driveBase64ToBytes(envelope.nonce) },
    fileKey,
    driveBase64ToBytes(envelope.ciphertext)
  );
  return JSON.parse(new TextDecoder().decode(plaintext));
}

async function decryptDriveE2eeBlob(blob, e2ee, passphrase) {
  const fileKey = await unwrapDriveFileKey(e2ee.encrypted_file_key, passphrase);
  const plaintext = await window.crypto.subtle.decrypt(
    { name: "AES-GCM", iv: driveBase64ToBytes(e2ee.nonce) },
    fileKey,
    await blob.arrayBuffer()
  );
  const metadata = await decryptDriveJsonMetadata(fileKey, e2ee.encrypted_metadata);
  return {
    blob: new Blob([plaintext], { type: metadata.mime_type || "application/octet-stream" }),
    filename: metadata.filename || "download",
  };
}

async function prepareDriveE2eeUpload(file, passphrase) {
  if (!window.crypto?.subtle) {
    throw new Error("此瀏覽器不支援端到端加密上傳，請改用私密檔案或換用支援 WebCrypto 的瀏覽器。");
  }
  const originalName = file.name || "未命名檔案";
  const plaintext = await file.arrayBuffer();
  const fileKey = await window.crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, true, ["encrypt", "decrypt"]);
  const nonce = driveRandomNonce();
  const ciphertext = await window.crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, fileKey, plaintext);
  const encryptedBlob = new Blob([ciphertext], { type: "application/octet-stream" });
  const encryptedMetadata = await encryptDriveJsonMetadata(fileKey, {
    filename: originalName,
    mime_type: file.type || "application/octet-stream",
    size_bytes: file.size,
    encrypted_at: new Date().toISOString(),
  });
  const encryptedFileKey = await wrapDriveFileKey(fileKey, passphrase);
  const ciphertextHash = await window.crypto.subtle.digest("SHA-256", ciphertext);
  return {
    blob: encryptedBlob,
    filename: originalName,
    encrypted_metadata: encryptedMetadata,
    encrypted_file_key: encryptedFileKey,
    wrapped_by: DRIVE_E2EE_PASSPHRASE_WRAPPER,
    ciphertext_sha256: driveBufferToHex(ciphertextHash),
    encryption_algorithm: "AES-GCM",
    encryption_version: "browser-passphrase-v2",
    nonce: driveBytesToBase64(nonce),
  };
}

const ALBUM_VISIBILITY_LABELS = {
  private: "私人",
  unlisted: "不列出，持連結可看",
  public: "公開",
};

function albumVisibilityLabel(value) {
  return ALBUM_VISIBILITY_LABELS[value] || value || "私人";
}

function renderDriveGroupedStats(targetId, grouped, emptyText, labelFn) {
  const el = $(targetId);
  if (!el) return;
  const entries = Object.entries(grouped || {});
  if (!entries.length) {
    el.innerHTML = `<div class="drive-empty">${sanitize(emptyText || "尚無資料")}</div>`;
    return;
  }
  el.innerHTML = entries.map(([name, item]) => `
    <div class="drive-pill">
      <strong>${sanitize(labelFn ? labelFn(name) : name)}</strong>
      <span>${Number(item.count || 0)} 個 · ${formatDriveBytes(item.bytes || 0)}</span>
    </div>
  `).join("");
}

function storageUpgradeLabel(item) {
  const bytes = formatDriveBytes(item.storage_bytes || 0);
  const days = Number(item.duration_days || 0);
  return `${item.label || item.item_name || item.item_key} · ${bytes} / ${days} 天`;
}

function storageUpgradePricePreviewHtml(item) {
  if (!item) return "";
  const effective = Number(item.effective_price ?? item.base_price ?? 0);
  const base = Number(item.base_price || 0);
  const tierMultiplier = Number(item.tier_multiplier || 1);
  if (tierMultiplier > 1) {
    return `<span style="color:var(--warning,#e6a817)">本次定價：<strong>${effective} 積分</strong>（基礎 ${base} × ${tierMultiplier.toFixed(1)} 階梯加價）</span>`;
  }
  return `本次定價：<strong>${effective} 積分</strong>`;
}

function renderStorageUpgrade(payload) {
  const card = $("drive-storage-upgrade-card");
  const select = $("drive-storage-upgrade-select");
  const summary = $("drive-storage-upgrade-summary");
  const list = $("drive-storage-upgrade-active-list");
  const button = document.querySelector("[data-drive-action='purchase-storage-upgrade']");
  if (!card || !select || !summary || !list) return;

  const canPurchase = Boolean(payload?.can_purchase);
  const isRoot = currentUser === "root";
  driveStorageUpgradeCanPurchase = canPurchase && !isRoot;
  driveStorageUpgradeMessage = payload?.message || (isRoot ? "root 依實際磁碟容量控管，不需要購買容量方案" : "");
  driveStorageUpgradeCatalog = driveStorageUpgradeCanPurchase && Array.isArray(payload?.catalog) ? payload.catalog : [];
  select.innerHTML = driveStorageUpgradeCatalog.length
    ? driveStorageUpgradeCatalog.map((item) => `<option value="${sanitize(item.item_key)}">${sanitize(storageUpgradeLabel(item))}</option>`).join("")
    : `<option value="">${isRoot ? "root 不需要購買容量方案" : "目前沒有可用的容量方案"}</option>`;
  select.disabled = !canPurchase || !driveStorageUpgradeCatalog.length;
  if (button) {
    button.disabled = !driveStorageUpgradeCanPurchase || !driveStorageUpgradeCatalog.length;
    button.textContent = driveStorageUpgradeCanPurchase ? "用積分購買容量" : (isRoot ? "root 不需要購買容量" : "容量方案不可購買");
    button.title = driveStorageUpgradeCanPurchase ? "用積分購買所選雲端硬碟容量" : (driveStorageUpgradeMessage || "目前沒有可購買的容量方案");
  }
  const pricePreview = $("drive-storage-upgrade-price-preview");
  const updatePricePreview = () => {
    if (!pricePreview) return;
    const key = select.value;
    const item = driveStorageUpgradeCatalog.find((i) => i.item_key === key);
    pricePreview.innerHTML = item ? storageUpgradePricePreviewHtml(item) : "";
  };
  select.removeEventListener("change", select._upgradePriceHandler);
  select._upgradePriceHandler = updatePricePreview;
  select.addEventListener("change", updatePricePreview);
  updatePricePreview();

  const usage = payload?.usage || {};
  const purchased = Number(usage.purchased_extra_bytes || 0);
  summary.textContent = canPurchase
    ? `已加購容量：${formatDriveBytes(purchased)}`
    : (payload?.message || "此帳號不需要加購容量");

  const active = Array.isArray(payload?.active_purchases) ? payload.active_purchases : [];
  list.innerHTML = active.length
    ? active.map((row) => `
      <div class="drive-pill">
        <strong>${sanitize(row.item_key)}</strong>
        <span>${formatDriveBytes(row.purchased_bytes || 0)} · 到期 ${sanitize(row.expires_at || "-")}</span>
      </div>
    `).join("")
    : `<div class="drive-empty">目前沒有有效的加購容量</div>`;
}

function renderDriveCapacityGauge(percent, level, options = {}) {
  const visual = $("drive-capacity-visual");
  const label = $("drive-capacity-percent-label");
  const note = $("drive-capacity-note");
  const unlimited = !!options.unlimited;
  const zeroQuota = !!options.zeroQuota;
  const usedBytes = Number(options.usedBytes || 0);
  const safePercent = Math.max(0, Math.min(100, Number(percent || 0)));
  let labelText = `${Math.round(safePercent)}%`;
  let noteText = "容量使用率";
  if (unlimited) {
    labelText = "無上限";
    noteText = `已使用 ${formatDriveBytes(usedBytes)}`;
  } else if (zeroQuota) {
    labelText = usedBytes > 0 ? "超出容量" : "未配置";
    noteText = usedBytes > 0 ? `已使用 ${formatDriveBytes(usedBytes)}` : "尚未設定可用容量";
  }
  if (label) {
    label.textContent = labelText;
  }
  if (note) note.textContent = noteText;
  if (!visual) return;
  visual.style.setProperty("--drive-capacity-level", unlimited ? "18%" : `${safePercent}%`);
  visual.dataset.warning = unlimited ? "low" : (level || "low");
  visual.dataset.unlimited = unlimited ? "true" : "false";
  visual.dataset.zeroQuota = zeroQuota ? "true" : "false";
  visual.setAttribute(
    "aria-label",
    unlimited
      ? `雲端硬碟容量無上限，已使用 ${formatDriveBytes(usedBytes)}`
      : zeroQuota
        ? (usedBytes > 0 ? `雲端硬碟尚未配置容量，已使用 ${formatDriveBytes(usedBytes)}` : "雲端硬碟尚未配置容量")
        : `雲端硬碟容量使用率 ${Math.round(safePercent)}%`
  );
}

function renderDriveDashboard(payload) {
  const rootTab = $("drive-root-admin-tab");
  if (rootTab) rootTab.hidden = currentUser !== "root";
  const security = payload && payload.security ? payload.security : {};
  const quota = security.usage || (payload && payload.quota) || {};
  driveLatestQuota = quota;
  const used = Number(quota.used_bytes || 0);
  const total = quota.total_bytes;
  const remaining = quota.remaining_bytes;
  const unlimited = total === null || total === undefined;
  const totalNumber = Number(total || 0);
  const zeroQuota = !unlimited && totalNumber <= 0;
  const percent = unlimited ? 0 : (zeroQuota && used > 0 ? 100 : Math.max(0, Math.min(100, Number(quota.percent_used || 0))));
  const level = zeroQuota && used > 0 ? "high" : (quota.warning_active || percent >= 80 ? "high" : percent >= 65 ? "medium" : "low");

  const usedLabel = $("drive-used-label");
  const totalLabel = $("drive-total-label");
  const remainingLabel = $("drive-remaining-label");
  const limitLabel = $("drive-limit-label");
  const barFill = $("drive-quota-bar-fill");

  if (usedLabel) usedLabel.textContent = formatDriveBytes(used);
  if (totalLabel) totalLabel.textContent = total === null || total === undefined ? " / 無上限" : ` / ${formatDriveBytes(total)}`;
  if (remainingLabel) remainingLabel.textContent = `剩餘容量：${formatDriveBytes(remaining)}`;
  if (limitLabel) {
    const maxFile = formatDriveBytes(quota.max_file_size_bytes);
    const daily = quota.upload_rate_limit_per_day === null || quota.upload_rate_limit_per_day === undefined ? "無上限" : `${quota.upload_rate_limit_per_day} 次`;
    const diskNote = quota.quota_source === "root_disk_total_95_percent"
      ? ` · root 上限：全用戶容量設定（磁碟總容量 95%），${quota.warning_threshold_percent || 80}% 起警示`
      : quota.quota_source === "root_global_capacity_limit_mb"
        ? ` · root 上限：全用戶容量設定，${quota.warning_threshold_percent || 80}% 起警示`
      : String(quota.quota_source || "").startsWith("manager_role_fixed_1gb")
        ? " · manager 上限：1 GB"
      : "";
    const purchaseNote = Number(quota.purchased_extra_bytes || 0) > 0 ? ` · 加購：${formatDriveBytes(quota.purchased_extra_bytes)}` : "";
    limitLabel.textContent = `單檔限制：${maxFile} · 每日上傳：${daily} · 檔案數：${Number(quota.file_count || 0)}${diskNote}${purchaseNote}`;
    limitLabel.style.color = quota.warning_active ? "#ffb74d" : "var(--muted)";
  }
  renderDriveCapacityGauge(percent, level, { unlimited, zeroQuota, usedBytes: used });
  if (barFill) {
    barFill.style.width = `${percent}%`;
    barFill.dataset.warning = level;
    // P5: also toggle the fx-capacity-bar warning/critical classes on the
    // parent so the wave + pulse animations fire. Existing colour rules
    // (#drive-quota-bar-fill[data-warning]) keep working in parallel.
    const wrap = barFill.parentElement;
    if (wrap && wrap.classList) {
      wrap.classList.toggle("warning", level === "medium" || (level === "high" && percent < 90));
      wrap.classList.toggle("critical", level === "high" && percent >= 90);
      wrap.style.setProperty("--fx-capacity-percent", `${percent}%`);
    }
  }

  const list = $("drive-security-list");
  if (list) {
    const restrictions = Array.isArray(security.restrictions) ? security.restrictions : [];
    list.innerHTML = restrictions.length
      ? restrictions.map((item) => `<li>${sanitize(item)}</li>`).join("")
      : "<li>目前沒有額外限制</li>";
  }

  renderDriveGroupedStats("drive-risk-summary", quota.by_risk_level, "尚無風險統計");
  renderDriveGroupedStats("drive-scan-summary", quota.by_scan_status, "尚無掃描狀態");
  renderDriveGroupedStats("drive-mode-summary", quota.by_privacy_mode, "尚無隱私模式統計", drivePrivacyModeLabel);
  renderDrivePrivacyModeComparison();
}

async function ensureDriveUploadQuota() {
  if (driveLatestQuota) return driveLatestQuota;
  try {
    const csrf = await fetchCsrfToken();
    const res = await apiFetch(API + "/files/security-policy", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (res.ok && json.ok) {
      renderDriveDashboard(json);
      return driveLatestQuota;
    }
  } catch (err) {
    // Server remains the final authority; a quota refresh failure should not
    // block small uploads that the backend may still accept.
  }
  return null;
}

function driveUploadQuotaError(sizeBytes, label = "檔案", options = {}) {
  const quota = options.quota || driveLatestQuota || {};
  const size = Number(sizeBytes || 0);
  if (!quota || !size) return "";
  if (quota.can_upload === false) return "目前會員等級或處分狀態不可上傳";
  const remaining = quota.remaining_bytes;
  if (remaining !== null && remaining !== undefined && size > Number(remaining)) {
    return `超過雲端硬碟容量上限：${label} ${formatDriveBytes(size)}，剩餘容量 ${formatDriveBytes(remaining)}。`;
  }
  if (options.checkMaxFile !== false) {
    const maxFile = quota.max_file_size_bytes;
    if (maxFile !== null && maxFile !== undefined && size > Number(maxFile)) {
      return `檔案超過單檔大小限制：${label} ${formatDriveBytes(size)}，單檔上限 ${formatDriveBytes(maxFile)}。`;
    }
  }
  return "";
}

async function preflightDriveUploadSize(sizeBytes, label = "檔案", options = {}) {
  const quota = await ensureDriveUploadQuota();
  const detail = driveUploadQuotaError(sizeBytes, label, { ...options, quota });
  if (!detail) return true;
  const msg = $("drive-msg");
  if (msg) flash(msg, detail, false);
  alert(detail);
  return false;
}

function driveFileNeedsWarning(file) {
  const risk = file && file.risk_level;
  const status = file && file.scan_status;
  return ["high", "blocked", "unknown_encrypted"].includes(risk) || ["infected", "quarantined", "failed", "unknown_encrypted"].includes(status);
}

function driveFileExtension(name) {
  const lower = String(name || "").toLowerCase();
  for (const ext of [".tar.gz", ".tar.bz2", ".tar.xz"]) {
    if (lower.endsWith(ext)) return ext;
  }
  const dot = lower.lastIndexOf(".");
  return dot >= 0 ? lower.slice(dot) : "";
}

function driveFileIsE2ee(file) {
  return String(file?.privacy_mode || "") === "e2ee";
}

function driveFileCategory(file) {
  const name = file?.display_name || file?.virtual_path || file?.original_filename_plain_for_public || file?.storage_path || "";
  const mime = String(file?.mime_type_plain_for_public || file?.mime_type || "").toLowerCase();
  const ext = driveFileExtension(name);
  if (mime.startsWith("audio/") || [".aac", ".aif", ".aiff", ".amr", ".flac", ".m4a", ".mid", ".midi", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".weba"].includes(ext)) return "audio";
  if (mime.startsWith("video/") || [".avi", ".m4v", ".mkv", ".mov", ".mp4", ".ogv", ".webm"].includes(ext)) return "video";
  if (mime.startsWith("image/") || [".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"].includes(ext)) return "image";
  if (mime === "application/pdf" || ext === ".pdf") return "pdf";
  if (mime.startsWith("text/") || [".c", ".cc", ".cpp", ".cs", ".css", ".csv", ".go", ".htm", ".html", ".ini", ".java", ".js", ".json", ".jsx", ".log", ".md", ".php", ".py", ".rs", ".sh", ".sql", ".text", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml"].includes(ext) || !ext) return "text";
  if ([".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"].includes(ext)) return "archive";
  return "metadata";
}

function driveFileIsImage(file) {
  return driveFileCategory(file) === "image";
}

function driveFileIsMedia(file) {
  const category = driveFileCategory(file);
  if (category === "audio" || category === "video") return true;
  const name = String(file?.display_name || file?.virtual_path || file?.original_filename_plain_for_public || file?.storage_path || file?.id || "").toLowerCase();
  const mime = String(file?.mime_type_plain_for_public || file?.mime_type || "").toLowerCase();
  return mime.startsWith("audio/")
    || mime.startsWith("video/")
    || [".mp4", ".m4v", ".mov", ".webm", ".ogv", ".avi", ".mkv", ".mp3", ".m4a", ".aac", ".flac", ".wav", ".weba", ".opus", ".oga", ".ogg"].some((ext) => name.endsWith(ext));
}

function driveTextLanguage(filename = "") {
  const ext = driveFileExtension(filename);
  const map = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".html": "html",
    ".htm": "html",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".php": "php",
    ".py": "python",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
  };
  return map[ext] || "text";
}

function driveHighlightCode(text = "", filename = "") {
  const language = driveTextLanguage(filename);
  let html = sanitize(text || "");
  if (language === "markdown") return html;
  if (["javascript", "typescript", "python", "cpp", "c", "csharp", "java", "go", "rust", "php", "shell"].includes(language)) {
    html = html.replace(/(&quot;.*?&quot;|&#39;.*?&#39;|".*?"|'.*?')/g, '<span class="drive-code-string">$1</span>');
    html = html.replace(/\b(function|return|const|let|var|if|else|for|while|class|def|import|from|try|except|catch|public|private|protected|static|void|int|float|double|char|bool|string|auto|struct|namespace|using|new|delete|async|await|yield|match|enum|impl|fn|mut|package|func|go|defer|echo|then|fi)\b/g, '<span class="drive-code-keyword">$1</span>');
    html = html.replace(/\b(true|false|null|None|nil|nullptr|self|this)\b/g, '<span class="drive-code-literal">$1</span>');
    html = html.replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="drive-code-number">$1</span>');
  } else if (["json", "yaml", "toml"].includes(language)) {
    html = html.replace(/(&quot;[^&]*?&quot;)(\s*:)/g, '<span class="drive-code-key">$1</span>$2');
    html = html.replace(/\b(true|false|null)\b/g, '<span class="drive-code-literal">$1</span>');
    html = html.replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="drive-code-number">$1</span>');
  } else if (["html", "xml"].includes(language)) {
    html = html.replace(/(&lt;\/?[\w:-]+|\/?&gt;)/g, '<span class="drive-code-keyword">$1</span>');
    html = html.replace(/\b([\w:-]+)=/g, '<span class="drive-code-key">$1</span>=');
  } else if (language === "css") {
    html = html.replace(/([.#]?[a-zA-Z_][\w-]*)(\s*\{)/g, '<span class="drive-code-key">$1</span>$2');
    html = html.replace(/\b([a-z-]+)(\s*:)/g, '<span class="drive-code-keyword">$1</span>$2');
  }
  return html;
}

function driveRenderTextPreview(preview) {
  const filename = preview?.filename || "";
  const text = preview?.text || "";
  const language = driveTextLanguage(filename);
  if (language === "markdown" && typeof markdownToSafeHtml === "function") {
    return `
      <div class="drive-markdown-preview">${markdownToSafeHtml(text)}</div>
      <details class="drive-source-details">
        <summary>查看 Markdown 原始碼</summary>
        <pre class="drive-preview-text"><code>${sanitize(text)}</code></pre>
      </details>
    `;
  }
  const languageLabel = language === "text" ? "純文字" : language;
  return `
    <div class="drive-card-sub">語言：${sanitize(languageLabel)}</div>
    <pre class="drive-preview-text drive-code-preview language-${sanitize(language)}"><code>${driveHighlightCode(text, filename)}</code></pre>
  `;
}

function drivePrimaryAction(file) {
  if (driveFileIsE2ee(file)) return { action: "preview", label: "解密預覽" };
  const category = driveFileCategory(file);
  if (category === "text") return { action: "edit-text", label: "編輯" };
  if (category === "audio" || category === "video") return { action: "preview", label: "串流" };
  if (category === "metadata") return { action: "preview", label: "資訊" };
  return { action: "preview", label: "預覽" };
}

function driveTransferPercent(item) {
  const raw = Number(item?.progress_percent);
  if (Number.isFinite(raw)) return Math.max(0, Math.min(100, raw));
  const loaded = Number(item?.loaded_bytes || 0);
  const total = Number(item?.total_bytes || 0);
  if (total > 0) return Math.max(0, Math.min(100, (loaded / total) * 100));
  return null;
}

function driveTransferJobStatus(status) {
  if (status === "completed") return "succeeded";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "cancelled";
  if (status === "paused") return "paused";
  if (status === "waiting_resume") return "paused";
  if (status === "queued") return "queued";
  return "running";
}

function driveRemoteTaskJobStatus(status) {
  if (status === "completed") return "succeeded";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "cancelled";
  if (status === "paused") return "paused";
  if (status === "queued") return "queued";
  return "running";
}

function driveRemoteTaskSourceLabel(task = {}) {
  return ["torrent_file", "torrent_url", "magnet"].includes(task.source_type) ? "BT 下載" : "Direct link";
}

function driveResumableUploadSourceRef(sessionId) {
  return `upload_session:${sessionId || ""}`;
}

function driveResumableSessionTransferStatus(session = {}) {
  const status = String(session.status || "");
  if (status === "completed") return "completed";
  if (status === "failed") return "waiting_resume";
  if (status === "aborted") return "cancelled";
  if (status === "completing") return "running";
  return "waiting_resume";
}

function driveResumableSessionStatusMessage(session = {}, transferStatus = "") {
  const received = formatDriveBytes(session.received_bytes || 0);
  const total = formatDriveBytes(session.total_bytes || 0);
  if (session.status === "completing") return "伺服器正在合併、掃描與保存";
  if (session.status === "failed" && transferStatus !== "waiting_resume") return session.error_message || "分段上傳失敗";
  if (session.status === "aborted") return "分段上傳已中止";
  if (session.status === "completed") return "分段上傳已完成";
  if (transferStatus === "waiting_resume") return `等待瀏覽器續傳，重新選擇同一檔案可接續上傳（${received} / ${total}）`;
  return `分段上傳中（${received} / ${total}）`;
}

function driveTransferSourceLabel(item = {}) {
  if (item.kind === "remote_download") {
    return item.source_label || "遠端下載";
  }
  if (item.kind === "resumable_upload") return "分段上傳";
  return item.kind === "folder_upload" ? "資料夾上傳" : "檔案上傳";
}

function driveTransferToJobCenterJob(item = {}) {
  const percent = driveTransferPercent(item);
  const status = driveTransferJobStatus(item.status);
  const isRemote = item.kind === "remote_download";
  const isResumable = item.kind === "resumable_upload";
  const sourceModule = isRemote
    ? "cloud_drive_remote_download"
    : (isResumable ? "cloud_drive_resumable_upload" : "cloud_drive_upload");
  const sourceRef = item.source_ref
    || (item.task_id ? `remote_download:${item.task_id}` : "")
    || (item.session_id ? driveResumableUploadSourceRef(item.session_id) : "")
    || `local_transfer:${item.id || ""}`;
  const title = `${driveTransferSourceLabel(item)}：${item.name || item.filename || "處理中的檔案"}`;
  const speed = formatDriveSpeed(item.speed_bytes_per_sec);
  const bytes = item.total_bytes
    ? `${formatDriveBytes(item.loaded_bytes || 0)} / ${formatDriveBytes(item.total_bytes)}`
    : (item.loaded_bytes ? formatDriveBytes(item.loaded_bytes) : "");
  return {
    job_uuid: item.job_uuid || `${sourceModule}:${sourceRef}`,
    owner_user_id: null,
    job_type: isRemote ? "cloud_drive.remote_download" : (isResumable ? "cloud_drive.resumable_upload" : "cloud_drive.upload"),
    title,
    description: "雲端硬碟傳輸任務",
    source_module: sourceModule,
    source_ref: sourceRef,
    status,
    live_progress: true,
    live_status_source: "Drive",
    progress_percent: percent === null ? (status === "succeeded" || status === "failed" || status === "cancelled" ? 100 : 0) : Math.round(percent),
    stage: item.phase || item.status || status,
    stage_detail: [item.msg || "", bytes, speed].filter(Boolean).join(" · "),
    error_message: status === "failed" ? (item.msg || "傳輸失敗") : "",
    error_stage: status === "failed" ? (item.phase || "failed") : "",
    cancellable: (isRemote || isResumable) && ["queued", "running", "paused"].includes(status),
    metadata: {
      local_transfer: true,
      live_progress: true,
      transfer_id: item.id,
      task_id: item.task_id || "",
      session_id: item.session_id || "",
      loaded_bytes: item.loaded_bytes,
      total_bytes: item.total_bytes,
      speed_bytes_per_sec: item.speed_bytes_per_sec,
    },
    created_at: item.created_at || item.updated_at || new Date().toISOString(),
    updated_at: item.updated_at || item.created_at || new Date().toISOString(),
  };
}

function driveResumableSessionToJobCenterJob(session = {}) {
  const transferStatus = driveResumableSessionTransferStatus(session);
  const status = driveTransferJobStatus(transferStatus);
  const percent = Number(session.progress_percent);
  const sourceRef = driveResumableUploadSourceRef(session.session_id);
  const bytes = session.total_bytes
    ? `${formatDriveBytes(session.received_bytes || 0)} / ${formatDriveBytes(session.total_bytes)}`
    : (session.received_bytes ? formatDriveBytes(session.received_bytes) : "");
  return {
    job_uuid: `cloud_drive_resumable_upload:${sourceRef}`,
    job_type: "cloud_drive.resumable_upload",
    title: `分段上傳：${session.filename || "處理中的檔案"}`,
    description: "雲端硬碟 resumable/chunk upload、掃描與保存",
    source_module: "cloud_drive_resumable_upload",
    source_ref: sourceRef,
    status,
    live_progress: true,
    live_status_source: "分段上傳",
    progress_percent: Number.isFinite(percent) ? Math.max(0, Math.min(100, Math.round(percent))) : (status === "succeeded" || status === "failed" || status === "cancelled" ? 100 : 0),
    stage: session.status || transferStatus,
    stage_detail: [driveResumableSessionStatusMessage(session, transferStatus), bytes].filter(Boolean).join(" · "),
    error_message: status === "failed" ? (session.error_message || "分段上傳失敗") : "",
    error_stage: status === "failed" ? (session.status || "failed") : "",
    cancellable: ["queued", "running", "paused"].includes(status),
    metadata: {
      session_id: session.session_id || "",
      live_progress: true,
      filename: session.filename || "",
      loaded_bytes: session.received_bytes,
      total_bytes: session.total_bytes,
      privacy_mode: session.privacy_mode || "",
    },
    created_at: session.created_at || session.updated_at || new Date().toISOString(),
    updated_at: session.updated_at || session.created_at || new Date().toISOString(),
  };
}

function driveRemoteTaskToJobCenterJob(task = {}) {
  const label = driveRemoteTaskSourceLabel(task);
  const status = driveRemoteTaskJobStatus(task.status);
  const percent = Number(task.progress_percent);
  const name = task.filename || task.torrent_filename || task.url || "遠端下載";
  const speed = formatDriveSpeed(task.speed_bytes_per_sec);
  const bytes = task.total_bytes
    ? `${formatDriveBytes(task.loaded_bytes || 0)} / ${formatDriveBytes(task.total_bytes)}`
    : (task.loaded_bytes ? formatDriveBytes(task.loaded_bytes) : "");
  return {
    job_uuid: `cloud_drive_remote_download:remote_download:${task.id || ""}`,
    job_type: ["torrent_file", "torrent_url", "magnet"].includes(task.source_type)
      ? `cloud_drive.remote_download.bt.${task.source_type || "bt"}`
      : "cloud_drive.remote_download.direct",
    title: `${label}：${name}`,
    description: "遠端下載、掃描與保存",
    source_module: "cloud_drive_remote_download",
    source_ref: `remote_download:${task.id || ""}`,
    status,
    live_progress: true,
    live_status_source: "遠端下載",
    progress_percent: Number.isFinite(percent) ? Math.max(0, Math.min(100, Math.round(percent))) : (status === "succeeded" || status === "failed" || status === "cancelled" ? 100 : 0),
    stage: task.phase || task.status || status,
    stage_detail: [task.msg || task.error || "", task.availability_hint ? `可用度 ${task.availability_score || 0} · ${task.availability_hint}` : "", bytes, speed].filter(Boolean).join(" · "),
    error_message: status === "failed" ? (task.error || task.msg || "遠端下載失敗") : "",
    error_stage: status === "failed" ? (task.phase || "failed") : "",
    cancellable: ["queued", "running", "paused"].includes(status),
    metadata: {
      task_id: task.id,
      live_progress: true,
      source_type: task.source_type,
      loaded_bytes: task.loaded_bytes,
      total_bytes: task.total_bytes,
      speed_bytes_per_sec: task.speed_bytes_per_sec,
      recoverable: !!task.recoverable,
      availability_score: task.availability_score,
      availability_hint: task.availability_hint,
    },
    created_at: task.created_at || task.updated_at || new Date().toISOString(),
    updated_at: task.updated_at || task.created_at || new Date().toISOString(),
  };
}

function upsertDriveTaskCenterLocalJob(item = {}) {
  if (!item.id && !item.task_id) return;
  const job = driveTransferToJobCenterJob(item);
  const key = `${job.source_module}:${job.source_ref || job.job_uuid}`;
  driveTaskCenterLocalJobs = [
    job,
    ...driveTaskCenterLocalJobs.filter((existing) => `${existing.source_module}:${existing.source_ref || existing.job_uuid}` !== key),
  ].slice(0, DRIVE_TASK_CENTER_LOCAL_MAX);
}

function mergeDriveTaskCenterJobs(jobs = []) {
  const byKey = new Map();
  jobs.filter(Boolean).forEach((job) => {
    const key = `${job.source_module || ""}:${job.source_ref || job.job_uuid || ""}`;
    if (!key.trim()) return;
    const existing = byKey.get(key);
    if (!existing || Date.parse(job.updated_at || job.created_at || "") >= Date.parse(existing.updated_at || existing.created_at || "")) {
      byKey.set(key, job);
    }
  });
  return Array.from(byKey.values()).sort((a, b) => {
    const at = Date.parse(a.updated_at || a.created_at || "") || 0;
    const bt = Date.parse(b.updated_at || b.created_at || "") || 0;
    return bt - at;
  });
}

function getDriveTaskCenterLocalJobs() {
  const transferJobs = driveTransferRows.map(driveTransferToJobCenterJob);
  return mergeDriveTaskCenterJobs([...transferJobs, ...driveTaskCenterLocalJobs]);
}

async function loadDriveTaskCenterJobs({ csrf = "" } = {}) {
  const jobs = getDriveTaskCenterLocalJobs();
  const token = csrf || getCsrfToken() || await fetchCsrfToken();
  try {
    const res = await apiFetch(API + "/cloud-drive/remote-download/tasks", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": token || "" },
    });
    const json = await res.json().catch(() => ({}));
    if (res.ok && json.ok && Array.isArray(json.tasks)) {
      jobs.push(...json.tasks.map(driveRemoteTaskToJobCenterJob));
    }
  } catch (_) {
    // Job Center must still render platform jobs if drive remote tasks cannot be read.
  }
  try {
    const resumableSessions = await loadDriveResumableUploadSessions({ csrf: token });
    jobs.push(...resumableSessions.map(driveResumableSessionToJobCenterJob));
  } catch (_) {
    // Job Center must still render platform jobs if resumable sessions cannot be read.
  }
  return mergeDriveTaskCenterJobs(jobs);
}

function addDriveTransferRow(item) {
  const id = item.id || `transfer-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const now = new Date().toISOString();
  const row = {
    id,
    kind: item.kind || "upload",
    status: item.status || "running",
    phase: item.phase || "starting",
    name: item.name || item.filename || "處理中的檔案",
    loaded_bytes: item.loaded_bytes || 0,
    total_bytes: item.total_bytes ?? null,
    progress_percent: item.progress_percent ?? 0,
    speed_bytes_per_sec: item.speed_bytes_per_sec || 0,
    msg: item.msg || "準備中",
    task_id: item.task_id || "",
    session_id: item.session_id || "",
    source_label: item.source_label || "",
    source_ref: item.source_ref || "",
    created_at: item.created_at || now,
    updated_at: item.updated_at || now,
  };
  driveTransferRows = [row, ...driveTransferRows.filter((existing) => existing.id !== id)];
  upsertDriveTaskCenterLocalJob(row);
  syncDriveTransferIdleSuspend();
  renderDriveFiles(lastDriveFiles || []);
  renderStorageBrowser();
  return id;
}

function findDriveTransferRowIdForTask(taskId) {
  if (!taskId) return "";
  const row = driveTransferRows.find((item) => item.task_id === taskId);
  return row?.id || "";
}

function updateDriveTransferRow(id, updates) {
  let found = false;
  driveTransferRows = driveTransferRows.map((item) => {
    if (item.id !== id) return item;
    found = true;
    const row = { ...item, ...updates, updated_at: new Date().toISOString() };
    upsertDriveTaskCenterLocalJob(row);
    return row;
  });
  if (!found) {
    addDriveTransferRow({ id, ...updates });
    return;
  }
  syncDriveTransferIdleSuspend();
  renderDriveFiles(lastDriveFiles || []);
  renderStorageBrowser();
}

function removeDriveTransferRow(id) {
  driveTransferRows = driveTransferRows.filter((item) => item.id !== id);
  syncDriveTransferIdleSuspend();
  renderDriveFiles(lastDriveFiles || []);
  renderStorageBrowser();
}

async function dismissRemoteDownloadTask(taskId, transferId) {
  if (taskId) {
    await fetchCsrfToken({ force: true });
    await apiFetch(API + `/cloud-drive/remote-download/tasks/${encodeURIComponent(taskId)}`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" },
    }).catch(() => {});
  }
  removeDriveTransferRow(transferId);
}

async function controlRemoteDownloadTask(taskId, transferId, action) {
  if (!taskId || !["pause", "resume", "cancel"].includes(action)) return null;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + `/cloud-drive/remote-download/tasks/${encodeURIComponent(taskId)}/${action}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" },
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || "下載任務更新失敗");
  const task = json.task || {};
  if (task.id) applyRemoteDownloadTaskToTransfer(task);
  if (transferId && task.id) {
    updateDriveTransferRow(transferId, {
      task_id: task.id,
      name: task.filename || task.torrent_filename || task.url || "遠端下載",
      status: task.status || "running",
      phase: task.phase || "",
      loaded_bytes: task.loaded_bytes,
      total_bytes: task.total_bytes,
      progress_percent: task.progress_percent,
      speed_bytes_per_sec: task.speed_bytes_per_sec,
      recoverable: !!task.recoverable,
      msg: task.msg || json.msg || "",
    });
  }
  return task;
}

async function pauseRemoteDownloadTask(taskId, transferId) {
  const task = await controlRemoteDownloadTask(taskId, transferId, "pause");
  flash($("drive-msg"), "已暫停下載任務", true);
  return task;
}

async function resumeRemoteDownloadTask(taskId, transferId) {
  const task = await controlRemoteDownloadTask(taskId, transferId, "resume");
  flash($("drive-msg"), "已繼續下載任務", true);
  resumeRemoteDownloadTaskPolling(task);
  return task;
}

async function cancelRemoteDownloadTask(taskId, transferId) {
  if (!window.confirm("確定要取消這個下載任務？")) return null;
  const task = await controlRemoteDownloadTask(taskId, transferId, "cancel");
  flash($("drive-msg"), "已取消下載任務", true);
  return task;
}

async function recoverRemoteDownloadTask(taskId, transferId) {
  if (!taskId) return null;
  const ok = window.confirm("這個遠端下載已完成但保存被中斷。要從 staging 檔恢復並保存到雲端硬碟嗎？");
  if (!ok) return null;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + `/cloud-drive/remote-download/tasks/${encodeURIComponent(taskId)}/recover`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `恢復保存失敗（HTTP ${res.status}）`);
  const task = json.task || {};
  if (transferId) updateDriveTransferRow(transferId, {
    status: task.status || "completed",
    phase: task.phase || "completed",
    msg: task.msg || json.msg || "遠端下載已恢復並保存",
    progress_percent: 100,
    loaded_bytes: task.loaded_bytes,
    total_bytes: task.total_bytes,
    recoverable: false,
  });
  flash($("drive-msg"), json.msg || "遠端下載已恢復並保存", true);
  await loadDriveDashboard();
  return task;
}

function renderDriveTransferRow(item) {
  const percent = driveTransferPercent(item);
  const width = percent === null ? 100 : percent;
  const label = percent === null ? "計算中" : `${Math.round(percent)}%`;
  const bytes = item.total_bytes
    ? `${formatDriveBytes(item.loaded_bytes || 0)} / ${formatDriveBytes(item.total_bytes)}`
    : (
      item.loaded_bytes
        ? formatDriveBytes(item.loaded_bytes)
        : (item.status === "completed" ? "已保存" : "等待資料")
    );
  const speed = formatDriveSpeed(item.speed_bytes_per_sec);
  const isRemote = item.kind === "remote_download" || !!item.task_id;
  const isResumable = item.kind === "resumable_upload";
  const hasRemoteTask = isRemote && !!item.task_id;
  const hasResumableSession = isResumable && !!item.session_id;
  const terminalOrStopped = ["completed", "failed", "paused", "cancelled", "waiting_resume"].includes(item.status);
  const transferMeta = speed && !terminalOrStopped
    ? `${bytes} · ${speed}`
    : bytes;
  const statusClass = item.status === "failed"
    ? "failed"
    : item.status === "completed"
      ? "completed"
      : item.status === "paused"
        ? "paused"
        : item.status === "cancelled"
          ? "cancelled"
          : item.status === "waiting_resume"
            ? "paused"
            : "running";
  const actionName = isRemote ? "下載" : (isResumable ? "分段上傳" : "上傳");
  const statusText = item.status === "failed"
    ? `${actionName}失敗`
    : item.status === "completed"
      ? `${actionName}完成`
      : item.status === "paused"
        ? "已暫停"
        : item.status === "cancelled"
          ? "已取消"
          : item.status === "waiting_resume"
            ? "等待續傳"
            : item.phase === "pause_requested"
              ? "暫停中"
              : item.phase === "cancel_requested"
                ? "取消中"
                : (isRemote ? "下載中" : (isResumable ? "分段上傳中" : "上傳中"));
  const remoteControlLocked = ["saving", "pause_requested", "cancel_requested"].includes(item.phase);
  const canPause = hasRemoteTask && ["queued", "running"].includes(item.status) && !remoteControlLocked;
  const canResume = hasRemoteTask && item.status === "paused";
  const canCancel = hasRemoteTask && ["queued", "running", "paused"].includes(item.status) && !["saving", "cancel_requested"].includes(item.phase);
  const canRecoverRemote = hasRemoteTask && !!item.recoverable;
  const canResumeResumable = hasResumableSession && item.status === "waiting_resume";
  const canCancelResumable = hasResumableSession && ["waiting_resume", "running"].includes(item.status) && !["completing", "finalizing", "server_processing"].includes(item.phase);
  const canDismiss = item.status === "failed" || item.status === "completed" || item.status === "paused" || item.status === "cancelled";
  const controls = [
    canPause ? `<button class="btn btn-small" type="button" data-drive-action="pause-remote-download" data-transfer-id="${sanitize(item.id)}" data-task-id="${sanitize(item.task_id || "")}">暫停</button>` : "",
    canResume ? `<button class="btn btn-small btn-primary" type="button" data-drive-action="resume-remote-download" data-transfer-id="${sanitize(item.id)}" data-task-id="${sanitize(item.task_id || "")}">繼續</button>` : "",
    canCancel ? `<button class="btn btn-small btn-danger" type="button" data-drive-action="cancel-remote-download" data-transfer-id="${sanitize(item.id)}" data-task-id="${sanitize(item.task_id || "")}">取消</button>` : "",
    canRecoverRemote ? `<button class="btn btn-small btn-primary" type="button" data-drive-action="recover-remote-download" data-transfer-id="${sanitize(item.id)}" data-task-id="${sanitize(item.task_id || "")}">恢復保存</button>` : "",
    canResumeResumable ? `<button class="btn btn-small btn-primary" type="button" data-drive-action="resume-resumable-upload" data-transfer-id="${sanitize(item.id)}" data-session-id="${sanitize(item.session_id || "")}">選同檔續傳</button>` : "",
    canCancelResumable ? `<button class="btn btn-small btn-danger" type="button" data-drive-action="cancel-resumable-upload" data-transfer-id="${sanitize(item.id)}" data-session-id="${sanitize(item.session_id || "")}">中止</button>` : "",
    canDismiss ? `<button class="btn btn-small" type="button" data-drive-action="dismiss-transfer" data-transfer-id="${sanitize(item.id)}" data-task-id="${sanitize(item.task_id || "")}">移除</button>` : "",
  ].join("");
  return `
    <div class="drive-file-row drive-transfer-row ${sanitize(statusClass)}">
      <div>
        <strong>${sanitize(item.name || item.filename || "處理中的檔案")}</strong>
        <div class="drive-card-sub">${sanitize(statusText)} · ${sanitize(item.msg || item.phase || "處理中")} · ${sanitize(transferMeta)}</div>
        <div class="drive-progress" aria-label="${sanitize(label)}">
          <div class="drive-progress-fill ${percent === null ? "indeterminate" : ""}" style="width:${width}%;"></div>
        </div>
      </div>
      <div class="drive-file-actions">
        <span class="drive-progress-label">${sanitize(label)}</span>
        ${controls}
      </div>
    </div>
  `;
}

let lastDriveFiles = [];

async function openDriveFileInVideoPublish(fileId, name = "") {
  const msg = $("drive-msg");
  if (!fileId) {
    if (msg) flash(msg, "找不到要分享到影音的檔案", false);
    return;
  }
  if (typeof openVideoPublishFromDrive !== "function") {
    if (typeof switchModuleTab === "function") switchModuleTab("videos");
    if (msg) flash(msg, "已切換到影音頁，請在發布影音中選擇該檔案。", true);
    return;
  }
  const ok = await openVideoPublishFromDrive(fileId, { title: name });
  if (msg) flash(msg, ok ? "已前往影音發布設定" : "無法將此檔案帶入影音發布", !!ok);
}

function driveShareFileMeta(fileId) {
  const target = String(fileId || "");
  if (!target) return null;
  return findKnownDriveFile(target) || null;
}

function updateDriveShareScopeFields() {
  const scope = $("drive-share-scope")?.value || "link";
  const field = $("drive-share-account-field");
  if (field) field.style.display = scope === "account" ? "" : "none";
}

function openDriveShareDialog(fileId, name = "", storageFileId = "") {
  const overlay = $("drive-share-overlay");
  if (!overlay || !fileId) return;
  const file = driveShareFileMeta(fileId) || {};
  if ($("drive-share-file-id")) $("drive-share-file-id").value = fileId;
  if ($("drive-share-storage-file-id")) $("drive-share-storage-file-id").value = storageFileId || file.storage_file_id || "";
  if ($("drive-share-file-name")) $("drive-share-file-name").value = name || file.original_filename_plain_for_public || "";
  if ($("drive-share-file-label")) $("drive-share-file-label").textContent = name || file.original_filename_plain_for_public || fileId;
  if ($("drive-share-scope")) $("drive-share-scope").value = "link";
  if ($("drive-share-account")) $("drive-share-account").value = "";
  if (typeof setShareExpiryPickerValue === "function") setShareExpiryPickerValue("drive-share-expires-at", "");
  else if ($("drive-share-expires-at")) $("drive-share-expires-at").value = "";
  if ($("drive-share-max-views")) $("drive-share-max-views").value = "0";
  if ($("drive-share-password")) $("drive-share-password").value = "";
  if ($("drive-share-result")) {
    $("drive-share-result").style.display = "none";
    $("drive-share-result").textContent = "";
  }
  const e2eeNote = $("drive-share-e2ee-note");
  if (e2eeNote) e2eeNote.style.display = driveFileIsE2ee(file) ? "" : "none";
  const msg = $("drive-share-msg");
  if (msg) msg.className = "msg";
  updateDriveShareScopeFields();
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
}

function closeDriveShareDialog() {
  const overlay = $("drive-share-overlay");
  if (!overlay) return;
  overlay.classList.remove("show");
  overlay.setAttribute("aria-hidden", "true");
  const anyOverlayOpen = document.querySelector(".user-edit-overlay.show, .album-full-preview-overlay.show");
  if (!anyOverlayOpen) document.body.classList.remove("modal-open");
}

async function ensureDriveE2eeShareUnlocked(fileId) {
  const file = driveShareFileMeta(fileId) || {};
  if (!driveFileIsE2ee(file)) return true;
  await fetchCsrfToken();
  const csrf = getCsrfToken() || "";
  const e2ee = await fetchDriveE2eeKey(fileId, csrf);
  const passphrase = await getDriveE2eeSessionPassphrase(
    fileId,
    "分享 E2EE 檔案前需先在瀏覽器解密確認。密碼不會送到伺服器。",
    { allowPrompt: true }
  );
  if (!passphrase) return false;
  await unwrapDriveFileKey(e2ee.encrypted_file_key, passphrase);
  rememberDriveE2eeSessionPassphrase(fileId, passphrase);
  return true;
}

async function openDriveShareDialogAfterDecrypt(fileId, name = "", storageFileId = "") {
  if (!(await ensureDriveE2eeShareUnlocked(fileId))) return;
  openDriveShareDialog(fileId, name, storageFileId);
}

async function buildDriveE2eeShareEnvelope(fileId) {
  if (!window.crypto?.subtle) {
    throw new Error("此瀏覽器不支援建立 E2EE 分享授權。");
  }
  await fetchCsrfToken();
  const csrf = getCsrfToken() || "";
  const e2ee = await fetchDriveE2eeKey(fileId, csrf);
  const passphrase = await getDriveE2eeSessionPassphrase(
    fileId,
    "請輸入此 E2EE 檔案的原始加密密碼。密碼只在瀏覽器端使用，用來建立分享下載授權。",
    { allowPrompt: true }
  );
  if (!passphrase) throw new Error("E2EE 檔案需要輸入原始加密密碼才能分享。");
  const fileKey = await unwrapDriveFileKey(e2ee.encrypted_file_key, passphrase);
  rememberDriveE2eeSessionPassphrase(fileId, passphrase);
  const rawFileKey = await window.crypto.subtle.exportKey("raw", fileKey);
  const shareKeyBytes = driveRandomNonce(32);
  const shareKey = await window.crypto.subtle.importKey("raw", shareKeyBytes, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
  const nonce = driveRandomNonce();
  const ciphertext = await window.crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, shareKey, rawFileKey);
  return {
    wrapped_file_key_envelope: JSON.stringify({
      alg: "AES-GCM",
      v: 1,
      nonce: driveBytesToBase64(nonce),
      ciphertext: driveBytesToBase64(new Uint8Array(ciphertext)),
    }),
    fragment_key: driveBytesToBase64Url(shareKeyBytes),
  };
}

function driveShareAbsoluteUrl(url, fragmentKey = "") {
  const absolute = new URL(url || "/", window.location.origin);
  if (fragmentKey) absolute.hash = `key=${fragmentKey}`;
  return absolute.toString();
}

function normalizeDriveShareFragmentStorageUrl(url) {
  try {
    const parsed = new URL(String(url || ""), window.location.origin);
    parsed.hash = "";
    return `${parsed.pathname}${parsed.search}`;
  } catch (_) {
    return String(url || "").split("#")[0].trim();
  }
}

function loadDriveShareFragments() {
  try {
    return JSON.parse(sessionStorage.getItem(DRIVE_SHARE_FRAGMENT_STORAGE_KEY) || "{}") || {};
  } catch (_) {
    return {};
  }
}

function saveDriveShareFragments(data) {
  try {
    sessionStorage.setItem(DRIVE_SHARE_FRAGMENT_STORAGE_KEY, JSON.stringify(data || {}));
  } catch (_) {
    // ignore session storage failure
  }
}

function rememberDriveShareFragment(shareUrl, fragmentKey) {
  const key = normalizeDriveShareFragmentStorageUrl(shareUrl);
  const fragment = String(fragmentKey || "").trim();
  if (!key || !fragment) return;
  const state = loadDriveShareFragments();
  state[key] = fragment;
  saveDriveShareFragments(state);
}

function getRememberedDriveShareFragment(shareUrl) {
  const state = loadDriveShareFragments();
  const key = normalizeDriveShareFragmentStorageUrl(shareUrl);
  return String(state[key] || state[String(shareUrl || "").trim()] || "").trim();
}

function driveShareUrlHasFragmentKey(url) {
  try {
    const parsed = new URL(String(url || ""), window.location.origin);
    const hash = String(parsed.hash || "").replace(/^#/, "");
    if (!hash) return false;
    const params = new URLSearchParams(hash);
    return Boolean(params.get("key") || params.get("k") || (!hash.includes("=") && hash));
  } catch (_) {
    return /#(?:key=|k=|[^=]+$)/.test(String(url || ""));
  }
}

function driveShareUrlWithRememberedFragment(url, fragmentKey = "") {
  const fragment = String(fragmentKey || getRememberedDriveShareFragment(url) || "").trim();
  return driveShareAbsoluteUrl(url, fragment);
}

function setDriveShareCopyStatus(text, ok = true, button = null) {
  const status = document.querySelector("[data-drive-share-copy-status]");
  if (status) {
    status.textContent = text || "";
    status.className = `drive-card-sub drive-share-copy-status${ok ? "" : " err"}`;
  }
  const msg = $("drive-share-msg") || $("drive-msg");
  if (msg && text) flash(msg, text, ok);
  if (button && ok && text === "連結已複製") {
    const original = button.dataset.originalLabel || button.textContent || "複製連結";
    button.dataset.originalLabel = original;
    button.textContent = "已複製";
    window.clearTimeout(Number(button.dataset.copyResetTimer || 0));
    const timer = window.setTimeout(() => {
      button.textContent = button.dataset.originalLabel || "複製連結";
      delete button.dataset.copyResetTimer;
    }, DRIVE_SHARE_COPY_RESET_MS);
    button.dataset.copyResetTimer = String(timer);
    if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(button, "已完成複製", true);
  } else if (button && text) {
    if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(button, text, ok);
  }
}

async function createDriveShareLink() {
  const fileId = $("drive-share-file-id")?.value || "";
  const storageFileId = $("drive-share-storage-file-id")?.value || "";
  const file = driveShareFileMeta(fileId) || {};
  const requiresFragment = driveFileIsE2ee(file);
  const msg = $("drive-share-msg");
  if (!fileId) {
    if (msg) flash(msg, "找不到要分享的檔案", false);
    return;
  }
  const scope = $("drive-share-scope")?.value || "link";
  const payload = {
    access_scope: scope,
    expires_at: typeof getShareExpiryPickerValue === "function"
      ? getShareExpiryPickerValue("drive-share-expires-at")
      : $("drive-share-expires-at")?.value || "",
    max_views: Number($("drive-share-max-views")?.value || 0),
  };
  const sharePassword = ($("drive-share-password")?.value || "").trim();
  if (sharePassword) payload.share_password = sharePassword;
  if (storageFileId) payload.storage_file_id = storageFileId;
  else payload.file_id = fileId;
  if (scope === "account") {
    const account = ($("drive-share-account")?.value || "").trim();
    if (!account) {
      if (msg) flash(msg, "請輸入指定帳戶", false);
      return;
    }
    if (/^\d+$/.test(account)) payload.required_user_id = Number(account);
    else payload.required_username = account;
  }
  let fragmentKey = "";
  try {
    if (requiresFragment) {
      if (msg) flash(msg, "正在建立 E2EE 分享授權...", true);
      const envelope = await buildDriveE2eeShareEnvelope(fileId);
      payload.wrapped_file_key_envelope = envelope.wrapped_file_key_envelope;
      fragmentKey = envelope.fragment_key;
    }
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/storage/share-links", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || "",
      },
      body: JSON.stringify(payload),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || "分享連結建立失敗");
    const link = json.share_link || {};
    const bareShareUrl = driveShareAbsoluteUrl(link.share_url || (link.token ? `/shared/files/${link.token}` : ""), "");
    if (fragmentKey) rememberDriveShareFragment(bareShareUrl, fragmentKey);
    const shareUrl = driveShareUrlWithRememberedFragment(bareShareUrl, fragmentKey);
    if (requiresFragment && !driveShareUrlHasFragmentKey(shareUrl)) {
      throw new Error("E2EE 分享連結缺少片段金鑰，請重新產生分享連結後再複製。");
    }
    const result = $("drive-share-result");
    if (result) {
      result.style.display = "block";
      result.innerHTML = `
        <div class="drive-card-sub">下載分享連結</div>
        <div class="drive-share-link"><a href="${sanitize(shareUrl)}" target="_blank" rel="noreferrer">${sanitize(shareUrl)}</a></div>
        <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.5rem;">
          <button class="btn" type="button" data-drive-action="copy-drive-share-link" data-share-url="">複製連結</button>
          <button class="btn" type="button" data-drive-action="open-share-center">分享管理</button>
        </div>
        <div class="drive-card-sub drive-share-copy-status" data-drive-share-copy-status aria-live="polite"></div>
      `;
      const copyBtn = result.querySelector('[data-drive-action="copy-drive-share-link"]');
      if (copyBtn) {
        copyBtn.dataset.shareUrl = shareUrl;
        copyBtn.dataset.shareRequiresFragment = requiresFragment ? "1" : "0";
      }
    }
    if (msg) flash(msg, "分享連結已建立", true);
  } catch (err) {
    if (msg) flash(msg, err?.message || "分享連結建立失敗", false);
  }
}

async function copyDriveShareUrl(url, options = {}) {
  const button = options.button || null;
  const requiresFragment = Boolean(options.requiresFragment);
  let shareUrl = String(url || "");
  if (!shareUrl) return;
  if (requiresFragment && !driveShareUrlHasFragmentKey(shareUrl)) {
    shareUrl = driveShareUrlWithRememberedFragment(shareUrl);
  }
  if (requiresFragment && !driveShareUrlHasFragmentKey(shareUrl)) {
    setDriveShareCopyStatus("分享連結缺少 E2EE 片段金鑰，請重新產生分享連結後再複製。", false, button);
    return;
  }
  try {
    await navigator.clipboard.writeText(shareUrl);
    setDriveShareCopyStatus("連結已複製", true, button);
  } catch (_) {
    setDriveShareCopyStatus("請在彈出視窗複製完整連結。", true, button);
    window.prompt("分享連結", shareUrl);
  }
}

function renderDriveFiles(files) {
  lastDriveFiles = Array.isArray(files) ? files : [];
  const list = $("drive-file-list");
  if (!list) return;
  const transferHtml = driveTransferRows.map(renderDriveTransferRow).join("");
  if ((!Array.isArray(files) || !files.length) && !driveTransferRows.length) {
    list.innerHTML = `<div class="drive-empty">尚無雲端檔案</div>`;
    return;
  }
  const fileHtml = (Array.isArray(files) ? files : []).map((file) => {
    const name = file.original_filename_plain_for_public || file.id || "download.bin";
    const warn = driveFileNeedsWarning(file);
    const primary = drivePrimaryAction(file);
    const e2eeNote = driveFileIsE2ee(file) ? " · 需密碼預覽" : "";
    const rowAction = ` data-drive-action="preview"`;
    const albumButton = driveFileIsImage(file)
      ? `<button class="btn" type="button" data-drive-action="add-cloud-to-album" data-file-id="${sanitize(file.id)}" data-name="${sanitize(name)}">加入相簿</button>`
      : "";
    const videoButton = driveFileIsMedia(file)
      ? `<button class="btn" type="button" data-drive-action="publish-to-video" data-file-id="${sanitize(file.id)}" data-name="${sanitize(name)}">分享到影音</button>`
      : "";
    return `
      <div class="drive-file-row"${rowAction} data-file-id="${sanitize(file.id)}" data-name="${sanitize(name)}">
        <div>
          <strong>${sanitize(name)}</strong>
          <div class="drive-card-sub">${formatDriveBytes(file.size_bytes || 0)} · ${sanitize(drivePrivacyModeLabel(file.privacy_mode))}${drivePrivacyModeDescription(file.privacy_mode) ? `（${sanitize(drivePrivacyModeDescription(file.privacy_mode))}）` : ""} · ${sanitize(driveFileCategory(file))}${sanitize(e2eeNote)} · risk=${sanitize(file.risk_level || "-")} · scan=${sanitize(file.scan_status || "-")}</div>
        </div>
        <div class="drive-file-actions">
          <button class="btn" type="button" data-drive-action="${sanitize(primary.action)}" data-file-id="${sanitize(file.id)}">${sanitize(primary.label)}</button>
          ${videoButton}
          <button class="btn" type="button" data-drive-action="share-cloud-file" data-file-id="${sanitize(file.id)}" data-name="${sanitize(name)}">${driveFileIsE2ee(file) ? "解密後分享" : "分享"}</button>
          <button class="btn" type="button" data-drive-action="move-cloud-to-storage" data-file-id="${sanitize(file.id)}" data-name="${sanitize(name)}">移動</button>
          ${albumButton}
          <button class="btn ${warn ? "btn-danger" : "btn-primary"}" type="button" data-drive-action="download" data-file-id="${sanitize(file.id)}" data-warn="${warn ? "1" : "0"}">下載</button>
          <button class="btn btn-danger" type="button" data-drive-action="delete-cloud" data-file-id="${sanitize(file.id)}">永久刪除</button>
        </div>
      </div>
    `;
  }).join("");
  list.innerHTML = transferHtml + fileHtml;
}

function askDriveE2eePassphrase(promptText = "請輸入此檔案的 E2EE 加密密碼。") {
  return new Promise((resolve) => {
    const overlay = $("drive-e2ee-passphrase-overlay");
    const input = $("drive-e2ee-passphrase-input");
    const msg = $("drive-e2ee-passphrase-msg");
    const label = $("drive-e2ee-passphrase-prompt");
    const confirmBtn = $("drive-e2ee-passphrase-confirm-btn");
    const cancelBtn = $("drive-e2ee-passphrase-cancel-btn");
    if (!overlay || !input || !confirmBtn || !cancelBtn) {
      resolve(window.prompt(promptText) || "");
      return;
    }
    let done = false;
    const cleanup = (value) => {
      if (done) return;
      done = true;
      overlay.classList.remove("show");
      overlay.setAttribute("aria-hidden", "true");
      const anyOverlayOpen = document.querySelector(".user-edit-overlay.show, .album-full-preview-overlay.show");
      if (!anyOverlayOpen) document.body.classList.remove("modal-open");
      confirmBtn.removeEventListener("click", onConfirm);
      cancelBtn.removeEventListener("click", onCancel);
      input.removeEventListener("keydown", onKeydown);
      input.value = "";
      if (msg) msg.textContent = "";
      resolve(value || "");
    };
    const onConfirm = () => {
      if (!input.value) {
        if (msg) flash(msg, "請輸入 E2EE 加密密碼", false);
        return;
      }
      cleanup(input.value);
    };
    const onCancel = () => cleanup("");
    const onKeydown = (event) => {
      if (event.key === "Enter") onConfirm();
      if (event.key === "Escape") onCancel();
    };
    if (label) label.textContent = promptText;
    overlay.classList.add("show");
    overlay.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    confirmBtn.addEventListener("click", onConfirm);
    cancelBtn.addEventListener("click", onCancel);
    input.addEventListener("keydown", onKeydown);
    setTimeout(() => input.focus(), 0);
  });
}

async function loadDriveFiles(csrf) {
  const list = $("drive-file-list");
  if (!list) return;
  const res = await apiFetch(API + "/cloud-drive/files", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    list.innerHTML = `<div class="drive-empty">${sanitize(json.msg || "檔案列表讀取失敗")}</div>`;
    return;
  }
  const files = json.files || [];
  renderDriveFiles(files);
  driveAttachmentFileOptions = Array.isArray(files) ? files : [];
  driveAttachmentFileOptionsLoadedAt = Date.now();
  renderAttachmentFileSelects(files);
}

async function uploadDriveFile() {
  const input = $("drive-upload-file");
  if (!input || !input.files || !input.files[0]) {
    alert("請先選擇檔案");
    return;
  }
  const file = input.files[0];
  if (!(await preflightDriveUploadSize(file.size, `檔案「${file.name || "upload.bin"}」`))) {
    input.value = "";
    return;
  }
  const transferId = addDriveTransferRow({
    kind: "upload",
    name: file.name,
    loaded_bytes: 0,
    total_bytes: file.size,
    progress_percent: 0,
    msg: "等待上傳",
  });
  await fetchCsrfToken();
  const csrf = getCsrfToken();
  const privacyMode = $("drive-upload-privacy-mode")?.value || "standard_plain";
  const form = new FormData();
  try {
    let uploadBlob = file;
    let uploadFilename = file.name || "upload.bin";
    let uploadMimeType = file.type || "application/octet-stream";
    let uploadFields = {};
    if (isDriveE2eeMode(privacyMode)) {
      updateDriveTransferRow(transferId, { phase: "encrypting", msg: "瀏覽器端加密中", progress_percent: null });
      const encrypted = await prepareDriveE2eeUpload(file, getDriveE2eeUploadPassphrase());
      const encryptedQuotaError = driveUploadQuotaError(encrypted.blob.size, `加密後檔案「${file.name || encrypted.filename}」`);
      if (encryptedQuotaError) throw new Error(encryptedQuotaError);
      uploadBlob = encrypted.blob;
      uploadFilename = encrypted.filename;
      uploadMimeType = encrypted.blob.type || "application/octet-stream";
      uploadFields = driveEncryptedUploadFields(encrypted);
      updateDriveTransferRow(transferId, { phase: "uploading", msg: "加密完成，開始上傳密文", progress_percent: 0 });
    }
    let json = null;
    if (shouldUseDriveResumableUpload(uploadBlob)) {
      json = await uploadDriveBlobResumable({
        blob: uploadBlob,
        sourceFile: file,
        filename: uploadFilename,
        mimeType: uploadMimeType,
        privacyMode,
        fields: uploadFields,
        target: "cloud_drive",
        transferId,
        csrf,
        label: uploadFilename,
      });
    } else {
      form.append("file", uploadBlob, uploadFilename);
      appendDriveUploadFields(form, uploadFields);
      form.append("privacy_mode", privacyMode);
      const upload = await xhrUploadWithProgress(API + "/cloud-drive/upload", form, csrf, (event) => {
        if (event.lengthComputable) {
          updateDriveTransferRow(transferId, {
            loaded_bytes: event.loaded,
            total_bytes: event.total,
            progress_percent: (event.loaded / event.total) * 100,
            phase: event.loaded >= event.total ? "server_processing" : "uploading",
            msg: event.loaded >= event.total ? drivePostUploadProcessingMessage(privacyMode) : "上傳中",
          });
        } else {
          updateDriveTransferRow(transferId, {
            loaded_bytes: event.loaded || 0,
            total_bytes: null,
            progress_percent: null,
            msg: "上傳中",
          });
        }
      });
      json = upload.json || {};
      if (upload.status < 200 || upload.status >= 300 || !json.ok) {
        const detail = json.error_code ? `${json.msg || "雲端硬碟上傳失敗"}（${json.error_code}）` : (json.msg || `雲端硬碟上傳失敗（HTTP ${upload.status}）`);
        updateDriveTransferRow(transferId, { status: "failed", phase: "failed", msg: detail, progress_percent: 100 });
        alert(detail);
        return;
      }
    }
    updateDriveTransferRow(transferId, {
      status: "completed",
      phase: "completed",
      msg: "上傳完成",
      progress_percent: 100,
      loaded_bytes: uploadBlob.size,
      total_bytes: uploadBlob.size,
      source_ref: json.file?.file_id ? `cloud_file:${json.file.file_id}` : "",
    });
    input.value = "";
    if (isDriveE2eeMode(privacyMode)) clearDriveE2eeUploadPassphrase();
    await loadDriveDashboard();
    setTimeout(() => removeDriveTransferRow(transferId), DRIVE_TRANSFER_COMPLETED_VISIBLE_MS);
  } catch (err) {
    const detail = err.message || "雲端硬碟上傳失敗";
    const row = driveTransferRows.find((item) => item.id === transferId) || {};
    if (row.kind === "resumable_upload" && row.session_id) {
      updateDriveTransferRow(transferId, {
        status: "waiting_resume",
        phase: "waiting_resume",
        msg: `${detail}；請按「選同檔續傳」接續已上傳分段`,
        progress_percent: row.progress_percent,
      });
      try { await restoreResumableUploadSessions(); } catch (_) {}
    } else {
      updateDriveTransferRow(transferId, { status: "failed", phase: "failed", msg: detail, progress_percent: 100 });
    }
    input.value = "";
    alert(detail);
  }
}

function syncDriveCsrfFromCookie() {
  try {
    const latestCookieToken = readCookie("csrf_token");
    if (latestCookieToken) setCsrfToken(latestCookieToken);
  } catch (_) {}
  return getCsrfToken() || "";
}

async function currentDriveCsrfToken({ force = false } = {}) {
  const token = await fetchCsrfToken({ force });
  return token || syncDriveCsrfFromCookie() || "";
}

function driveUploadSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms || 0))));
}

function driveUploadRetryDelayMs(result, attempt) {
  const retryAfter = Number(result?.retry_after || result?.json?.retry_after_seconds || 0);
  if (retryAfter > 0) return Math.min(15000, retryAfter * 1000);
  return Math.min(8000, 750 * Math.max(1, attempt));
}

function driveUploadShouldRetryResult(result) {
  const status = Number(result?.status || 0);
  return status === 429 || status === 503 || result?.json?.error === "server_busy" || result?.json?.code === "server_busy";
}

async function xhrUploadWithProgress(url, form, csrf, onProgress, { retryOnCsrf = true } = {}) {
  const sendOnce = (token) => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.withCredentials = true;
    if (token) xhr.setRequestHeader("X-CSRF-Token", token);
    xhr.upload.onprogress = (event) => {
      if (typeof onProgress === "function") onProgress(event);
    };
    xhr.onload = () => {
      let json = {};
      try {
        json = JSON.parse(xhr.responseText || "{}");
      } catch (err) {
        json = {};
      }
      syncDriveCsrfFromCookie();
      resolve({
        status: xhr.status,
        json,
        retry_after: xhr.getResponseHeader("Retry-After") || "",
        backpressure: xhr.getResponseHeader("X-Hackme-Backpressure") || "",
      });
    };
    xhr.onerror = () => reject(new Error("上傳連線失敗"));
    xhr.ontimeout = () => reject(new Error("上傳逾時"));
    xhr.send(form);
  });
  const token = await currentDriveCsrfToken();
  let result = await sendOnce(token || csrf || "");
  if (retryOnCsrf && result.status === 403 && result.json?.error === "csrf_invalid") {
    const retryCsrf = await currentDriveCsrfToken({ force: true });
    if (retryCsrf) result = await sendOnce(retryCsrf);
  }
  return result;
}

async function driveResumableJson(path, { method = "GET", body = null, csrf = "" } = {}) {
  const token = await currentDriveCsrfToken() || csrf || "";
  const headers = { "X-CSRF-Token": token || "" };
  if (body) headers["Content-Type"] = "application/json";
  const res = await apiFetch(API + path, {
    method,
    credentials: "same-origin",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
  return json;
}

async function getDriveResumableUploadStatus(sessionId, csrf = "") {
  if (!sessionId) return null;
  try {
    const json = await driveResumableJson(`/cloud-drive/resumable-upload/${encodeURIComponent(sessionId)}/status`, { csrf });
    return json.session || null;
  } catch (_) {
    return null;
  }
}

async function startDriveResumableUploadSession(payload, csrf = "") {
  const json = await driveResumableJson("/cloud-drive/resumable-upload/start", {
    method: "POST",
    csrf,
    body: payload,
  });
  return json.session;
}

async function completeDriveResumableUpload(sessionId, csrf = "") {
  return driveResumableJson(`/cloud-drive/resumable-upload/${encodeURIComponent(sessionId)}/complete`, {
    method: "POST",
    csrf,
  });
}

function chooseResumableResumeFile(session = {}) {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.style.position = "fixed";
    input.style.left = "-9999px";
    input.setAttribute("aria-hidden", "true");
    input.addEventListener("change", () => {
      const file = input.files && input.files[0] ? input.files[0] : null;
      input.remove();
      resolve(file);
    }, { once: true });
    document.body.appendChild(input);
    input.click();
  });
}

async function resumeResumableUploadSession(sessionId, transferId = "") {
  if (!sessionId) return null;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const session = await getDriveResumableUploadStatus(sessionId, csrf);
  if (!session?.session_id) throw new Error("找不到等待續傳的 upload session");
  const selected = await chooseResumableResumeFile(session);
  if (!selected) return null;
  const expectedName = String(session.filename || "");
  const actualName = String(selected.name || "");
  const expectedBytes = Number(session.total_bytes || 0);
  const actualBytes = Number(selected.size || 0);
  if (expectedName && actualName !== expectedName) {
    alert(`請選擇同一個檔案：${expectedName}`);
    return null;
  }
  if (expectedBytes !== actualBytes) {
    alert(`檔案大小不一致，請重新選擇同一個檔案（需要 ${formatDriveBytes(expectedBytes)}，目前 ${formatDriveBytes(actualBytes)}）`);
    return null;
  }
  const rowId = transferId || findDriveTransferRowIdForSession(sessionId) || resumableUploadTransferId(sessionId);
  updateDriveTransferRow(rowId, {
    kind: "resumable_upload",
    session_id: sessionId,
    source_ref: driveResumableUploadSourceRef(sessionId),
    name: expectedName || actualName || "分段上傳",
    status: "running",
    phase: "resumable_uploading",
    loaded_bytes: session.received_bytes,
    total_bytes: session.total_bytes,
    progress_percent: session.progress_percent,
    msg: "已重新選取同一檔案，準備續傳",
  });
  try {
    const json = await uploadDriveBlobResumable({
      blob: selected,
      sourceFile: selected,
      filename: expectedName || actualName || "upload.bin",
      mimeType: session.mime_type || selected.type || "application/octet-stream",
      privacyMode: session.privacy_mode || "standard_plain",
      target: session.target || "cloud_drive",
      transferId: rowId,
      csrf,
      label: expectedName || actualName || "upload.bin",
      resumeSession: session,
    });
    updateDriveTransferRow(rowId, {
      status: "completed",
      phase: "completed",
      msg: "續傳完成",
      progress_percent: 100,
      loaded_bytes: selected.size,
      total_bytes: selected.size,
      source_ref: json.file?.file_id ? `cloud_file:${json.file.file_id}` : driveResumableUploadSourceRef(sessionId),
    });
    await loadDriveDashboard();
    setTimeout(() => removeDriveTransferRow(rowId), DRIVE_TRANSFER_COMPLETED_VISIBLE_MS);
    return json;
  } catch (err) {
    updateDriveTransferRow(rowId, {
      status: "waiting_resume",
      phase: "waiting_resume",
      msg: err.message || "續傳失敗，請重新選擇同一檔案再試一次",
      progress_percent: session.progress_percent,
    });
    alert(err.message || "續傳失敗");
    return null;
  }
}

async function uploadDriveBlobResumable({
  blob,
  sourceFile = null,
  filename = "upload.bin",
  mimeType = "application/octet-stream",
  privacyMode = "standard_plain",
  fields = {},
  target = "cloud_drive",
  virtualPath = "",
  displayName = "",
  transferId,
  csrf = "",
  aggregateBaseBytes = 0,
  aggregateTotalBytes = 0,
  label = "",
  exposeSessionAsTransfer = true,
  resumeSession = null,
} = {}) {
  if (!blob) throw new Error("缺少要上傳的檔案資料");
  const totalBytes = Number(blob.size || 0);
  const chunkSize = DRIVE_RESUMABLE_UPLOAD_CHUNK_BYTES;
  const totalForDisplay = Number(aggregateTotalBytes || totalBytes);
  const storageKey = driveResumableUploadKey({ file: sourceFile, blob, target, virtualPath, privacyMode });
  let session = resumeSession || null;
  if (session) {
    if (
      session.filename !== filename
      || Number(session.total_bytes || 0) !== totalBytes
      || session.target !== target
      || String(session.privacy_mode || privacyMode || "standard_plain") !== String(privacyMode || "standard_plain")
    ) {
      throw new Error("選取的檔案與等待續傳的 upload session 不一致");
    }
  }
  const remembered = session ? "" : rememberedDriveResumableUpload(storageKey);
  if (remembered) {
    const existing = await getDriveResumableUploadStatus(remembered, csrf);
    if (driveResumableUploadCanResumeSession(existing, { filename, totalBytes, target, privacyMode })) {
      session = existing;
    } else {
      forgetDriveResumableUpload(storageKey);
    }
  }
  if (!session) {
    session = await findDriveResumableUploadSessionForBlob({ filename, totalBytes, target, privacyMode, csrf });
  }
  if (!session) {
    session = await startDriveResumableUploadSession({
      filename,
      mime_type: mimeType || blob.type || "application/octet-stream",
      total_bytes: totalBytes,
      chunk_size: chunkSize,
      privacy_mode: privacyMode,
      target,
      virtual_path: virtualPath,
      display_name: displayName,
      ...fields,
    }, csrf);
  }
  rememberDriveResumableUpload(storageKey, session.session_id);
  if (exposeSessionAsTransfer) {
    updateDriveTransferRow(transferId, {
      kind: "resumable_upload",
      session_id: session.session_id,
      source_ref: driveResumableUploadSourceRef(session.session_id),
      name: filename,
      status: "running",
    });
  }
  const received = new Set((session.received_chunks || []).map((item) => Number(item)));
  const totalChunks = Number(session.total_chunks || Math.ceil(totalBytes / chunkSize));
  let uploadedBytes = Number(session.received_bytes || 0);
  updateDriveTransferRow(transferId, {
    phase: "resumable_uploading",
    loaded_bytes: aggregateBaseBytes + uploadedBytes,
    total_bytes: totalForDisplay,
    progress_percent: totalForDisplay > 0 ? ((aggregateBaseBytes + uploadedBytes) / totalForDisplay) * 100 : null,
    msg: `${label || filename} 分段上傳中${uploadedBytes > 0 ? "（已續傳）" : ""}`,
  });
  for (let index = 0; index < totalChunks; index += 1) {
    if (received.has(index)) continue;
    const start = index * chunkSize;
    const end = Math.min(totalBytes, start + chunkSize);
    const chunk = blob.slice(start, end);
    const form = new FormData();
    form.append("chunk", chunk, `${filename}.part${index}`);
    const uploadedBeforeChunk = uploadedBytes;
    let uploadResult = null;
    for (let attempt = 1; attempt <= 5; attempt += 1) {
      uploadResult = await xhrUploadWithProgress(
        API + `/cloud-drive/resumable-upload/${encodeURIComponent(session.session_id)}/chunks/${index}`,
        form,
        csrf,
        (event) => {
          const chunkLoaded = event.lengthComputable ? Math.min(chunk.size, event.loaded || 0) : 0;
          const loaded = aggregateBaseBytes + uploadedBeforeChunk + chunkLoaded;
          updateDriveTransferRow(transferId, {
            phase: "resumable_uploading",
            loaded_bytes: loaded,
            total_bytes: totalForDisplay,
            progress_percent: totalForDisplay > 0 ? (loaded / totalForDisplay) * 100 : null,
            msg: `${label || filename} 分段 ${index + 1}/${totalChunks} 上傳中`,
          });
        }
      );
      if (!driveUploadShouldRetryResult(uploadResult)) break;
      const delayMs = driveUploadRetryDelayMs(uploadResult, attempt);
      updateDriveTransferRow(transferId, {
        status: "running",
        phase: "retry_wait",
        msg: `${label || filename} 分段 ${index + 1}/${totalChunks} 暫時被限流，${Math.ceil(delayMs / 1000)} 秒後重試`,
      });
      await driveUploadSleep(delayMs);
    }
    const status = Number(uploadResult?.status || 0);
    const json = uploadResult?.json || {};
    if (status < 200 || status >= 300 || !json.ok) {
      throw new Error(json.msg || `分段 ${index + 1} 上傳失敗（HTTP ${status}）`);
    }
    session = json.session || session;
    uploadedBytes = Number(session.received_bytes || Math.min(totalBytes, uploadedBytes + chunk.size));
    updateDriveTransferRow(transferId, {
      phase: "resumable_uploading",
      loaded_bytes: aggregateBaseBytes + uploadedBytes,
      total_bytes: totalForDisplay,
      progress_percent: totalForDisplay > 0 ? ((aggregateBaseBytes + uploadedBytes) / totalForDisplay) * 100 : null,
      msg: `${label || filename} 已上傳分段 ${index + 1}/${totalChunks}`,
    });
  }
  updateDriveTransferRow(transferId, {
    phase: "server_processing",
    loaded_bytes: aggregateBaseBytes + totalBytes,
    total_bytes: totalForDisplay,
    progress_percent: totalForDisplay > 0 ? ((aggregateBaseBytes + totalBytes) / totalForDisplay) * 100 : 100,
    msg: `${label || filename} 分段合併、掃描與保存中`,
  });
  const completed = await completeDriveResumableUpload(session.session_id, csrf);
  forgetDriveResumableUpload(storageKey);
  return completed;
}

async function loadRemoteDownloadCapabilities() {
  const status = $("drive-remote-download-status");
  const torrentButtons = [$("drive-remote-torrent-inline-btn"), $("drive-remote-torrent-btn")].filter(Boolean);
  if (!currentUser || !canAccessModule("privacy_uploads")) return;
  await fetchCsrfToken();
  try {
    const res = await apiFetch(API + "/cloud-drive/remote-download/capabilities", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      driveRemoteDownloadCapabilities = { direct: true, bt_magnet: false, bt_file: false };
      if (status) status.textContent = json.msg || "BT 能力讀取失敗；Direct link 仍可用";
      torrentButtons.forEach((button) => {
        button.disabled = true;
        button.title = "BT 能力讀取失敗，請稍後重新整理";
      });
      scheduleDriveRemoteDownloadCapabilitiesRetry(status, torrentButtons, json.msg || `HTTP ${res.status}`);
      return;
    }
    if (driveRemoteDownloadCapabilitiesRetryTimer) {
      clearTimeout(driveRemoteDownloadCapabilitiesRetryTimer);
      driveRemoteDownloadCapabilitiesRetryTimer = null;
    }
    driveRemoteDownloadCapabilitiesRetryCount = 0;
    const caps = json.capabilities || {};
    driveRemoteDownloadCapabilities = {
      direct: true,
      bt_magnet: !!caps.bt_magnet,
      bt_file: !!caps.bt_file,
    };
    const btReady = driveRemoteDownloadCapabilities.bt_magnet || driveRemoteDownloadCapabilities.bt_file;
    const activeBackend = String(caps.bt_backend_active || caps.bt_backend || "");
    const btBackendLabel = activeBackend === "transmission"
      ? "Transmission RPC"
      : (activeBackend === "aria2" ? (caps.aria2c_path || "aria2c") : (caps.transmission_rpc_available ? "Transmission RPC" : (caps.aria2c_path || "BT backend")));
    torrentButtons.forEach((button) => {
      button.disabled = !btReady;
      button.title = btReady ? `BT 可用：${btBackendLabel}` : "BT 不可用：請設定 Transmission RPC 或安裝 aria2c";
    });
    if (status) {
      status.textContent = btReady
        ? `Direct link 可用；BT/magnet 可用（${btBackendLabel}）`
        : "Direct link 可用；BT/magnet 不可用，請設定 Transmission RPC 或安裝 aria2c";
    }
  } catch (err) {
    driveRemoteDownloadCapabilities = { direct: true, bt_magnet: false, bt_file: false };
    if (status) status.textContent = "BT 能力檢查失敗；Direct link 仍可用";
    torrentButtons.forEach((button) => {
      button.disabled = true;
      button.title = "BT 能力檢查失敗，請稍後重新整理";
    });
    scheduleDriveRemoteDownloadCapabilitiesRetry(status, torrentButtons, err?.message || "");
  }
}

function scheduleDriveRemoteDownloadCapabilitiesRetry(status, torrentButtons, reason = "") {
  if (driveRemoteDownloadCapabilitiesRetryTimer || driveRemoteDownloadCapabilitiesRetryCount >= DRIVE_REMOTE_CAPABILITY_RETRY_LIMIT) return;
  const delayMs = Math.min(8000, 1500 * (2 ** driveRemoteDownloadCapabilitiesRetryCount));
  driveRemoteDownloadCapabilitiesRetryCount += 1;
  if (status) {
    const suffix = reason ? `（${reason}）` : "";
    status.textContent = `BT 能力暫時讀取失敗${suffix}，將自動重試...`;
  }
  torrentButtons.forEach((button) => {
    button.disabled = true;
    button.title = "BT 能力暫時讀取失敗，正在自動重試";
  });
  driveRemoteDownloadCapabilitiesRetryTimer = setTimeout(() => {
    driveRemoteDownloadCapabilitiesRetryTimer = null;
    loadRemoteDownloadCapabilities();
  }, delayMs);
}

function classifyRemoteDownloadInput(rawUrl, { torrentUrlsAsBt = false } = {}) {
  const url = String(rawUrl || "").trim();
  if (!url) return { ok: false, kind: "", label: "", msg: "請輸入 direct link 或 magnet link" };
  if (url.startsWith("magnet:?")) return { ok: true, kind: "magnet", label: "BT magnet" };
  let parsed;
  try {
    parsed = new URL(url);
  } catch (_) {
    return { ok: false, kind: "", label: "", msg: "網址格式不正確，只接受 http、https direct link 或 magnet link" };
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return { ok: false, kind: "", label: "", msg: "只接受 http、https direct link 或 magnet link" };
  }
  if (torrentUrlsAsBt && parsed.pathname.toLowerCase().endsWith(".torrent")) {
    return { ok: true, kind: "torrent_url", label: "BT torrent URL" };
  }
  return { ok: true, kind: "direct", label: "direct link" };
}

function promptRemoteDriveDownloadUrl() {
  const input = $("drive-remote-url");
  const current = (input?.value || "").trim();
  const value = window.prompt("輸入 Direct link URL（http/https）", current);
  if (value === null) return;
  if (input) input.value = value.trim();
  const torrentInput = $("drive-remote-torrent-file");
  if (torrentInput) torrentInput.value = "";
  startRemoteDriveDownload({ source: "url", downloadMode: "direct", triggerButton: $("drive-remote-download-btn") });
}

function openRemoteTorrentPicker() {
  const input = $("drive-remote-torrent-file");
  const caps = driveRemoteDownloadCapabilities || {};
  if (!caps.bt_file && !caps.bt_magnet) {
    alert("BT 功能目前不可用，請確認已設定 Transmission RPC 或安裝 aria2c。");
    return;
  }
  if (caps.bt_magnet) {
    const value = window.prompt("輸入 magnet link 或 .torrent URL；若要上傳 .torrent 檔，請留空後按確定");
    if (value === null) return;
    if (value.trim()) {
      if ($("drive-remote-url")) $("drive-remote-url").value = value.trim();
      if (input) input.value = "";
      startRemoteDriveDownload({ source: "torrent-url", downloadMode: "bt", triggerButton: $("drive-remote-torrent-inline-btn") });
      return;
    }
  }
  if (!input || !caps.bt_file) return;
  input.value = "";
  input.click();
}

async function startRemoteDriveDownload({ source = "auto", downloadMode = "direct", triggerButton = null } = {}) {
  const url = source === "torrent" ? "" : ($("drive-remote-url")?.value || "").trim();
  const torrentInput = $("drive-remote-torrent-file");
  const torrentFile = source === "url" || source === "torrent-url" ? null : (torrentInput?.files?.[0] || null);
  if (!url && !torrentFile) {
    if (source === "auto") return promptRemoteDriveDownloadUrl();
    alert("請輸入下載網址，或上傳 .torrent BT 種子檔");
    return;
  }
  if (url && torrentFile) {
    alert("下載網址和 BT 種子檔請擇一使用");
    return;
  }
  const effectiveMode = torrentFile ? "bt" : (downloadMode === "bt" || source === "torrent-url" ? "bt" : "direct");
  const detected = url
    ? classifyRemoteDownloadInput(url, { torrentUrlsAsBt: effectiveMode === "bt" })
    : { ok: true, kind: "torrent_file", label: "BT torrent file" };
  if (!detected.ok) {
    alert(detected.msg || "下載網址格式不正確");
    return;
  }
  const caps = driveRemoteDownloadCapabilities || {};
  if (detected.kind === "magnet" && !caps.bt_magnet) {
    alert("BT magnet 功能目前不可用，請確認已設定 Transmission RPC 或安裝 aria2c。");
    return;
  }
  if ((detected.kind === "torrent_file" || detected.kind === "torrent_url") && !caps.bt_file) {
    alert("BT torrent 功能目前不可用，請確認已設定 Transmission RPC 或安裝 aria2c。");
    return;
  }
  const options = await askDriveUploadPrivacyOptions({ allowE2ee: false, title: `${detected.label || "遠端下載"}儲存前選擇隱私模式` });
  if (!options) return;
  if ($("drive-remote-privacy-mode")) $("drive-remote-privacy-mode").value = options.privacyMode;
  const transferId = addDriveTransferRow({
    kind: "remote_download",
    name: torrentFile ? torrentFile.name : url,
    source_label: detected.label || "遠端下載",
    loaded_bytes: 0,
    total_bytes: null,
    progress_percent: 0,
    msg: `建立${detected.label || "遠端"}下載任務`,
  });
  const button = triggerButton || $("drive-remote-download-btn");
  const oldText = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "下載中...";
  }
  try {
    await fetchCsrfToken({ force: true });
    let res;
    if (torrentFile) {
      const form = new FormData();
      form.append("torrent_file", torrentFile);
      form.append("privacy_mode", options.privacyMode);
      form.append("virtual_path", $("drive-remote-virtual-path")?.value || "");
      res = await apiFetch(API + "/cloud-drive/remote-download/torrent-tasks", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": getCsrfToken() || "" },
        body: form
      });
    } else {
      const status = $("drive-remote-download-status");
      if (status) status.textContent = `偵測為 ${detected.label}，正在建立下載任務...`;
      res = await apiFetch(API + "/cloud-drive/remote-download/tasks", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
        body: JSON.stringify({
          url,
          download_mode: effectiveMode,
          privacy_mode: options.privacyMode,
          virtual_path: $("drive-remote-virtual-path")?.value || ""
        })
      });
    }
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `遠端下載失敗（HTTP ${res.status}）`);
    const task = json.task || {};
    if (!task.id) throw new Error("遠端下載任務建立失敗");
    updateDriveTransferRow(transferId, {
      id: transferId,
      task_id: task.id,
      status: task.status || "running",
      phase: task.phase || "queued",
      msg: task.msg || "已加入下載佇列",
    });
    if ($("drive-remote-url")) $("drive-remote-url").value = "";
    if (torrentInput) torrentInput.value = "";
    flash($("drive-msg"), json.msg || "遠端下載任務已建立，可繼續操作頁面", true);
    resumeRemoteDownloadTaskPolling(task);
  } catch (err) {
    updateDriveTransferRow(transferId, { status: "failed", phase: "failed", msg: err.message || "遠端下載失敗", progress_percent: 100 });
    alert(err.message || "遠端下載失敗");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = oldText || "開始下載";
    }
  }
}

function driveSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollRemoteDownloadTask(taskId, transferId) {
  if (!taskId) throw new Error("遠端下載任務建立失敗");
  let consecutiveStatusErrors = 0;
  while (true) {
    await driveSleep(900);
    let res;
    let json = {};
    try {
      await fetchCsrfToken();
      res = await apiFetch(API + `/cloud-drive/remote-download/tasks/${encodeURIComponent(taskId)}`, {
        credentials: "same-origin",
        headers: { "X-CSRF-Token": getCsrfToken() || "" }
      });
      json = await res.json().catch(() => ({}));
      if (!res.ok || !json.ok) {
        throw new Error(json.msg || `遠端下載狀態讀取失敗（HTTP ${res.status}）`);
      }
      consecutiveStatusErrors = 0;
    } catch (err) {
      consecutiveStatusErrors += 1;
      updateDriveTransferRow(transferId, {
        status: "running",
        phase: "status_retry",
        msg: `狀態暫時讀取失敗，正在重試（${consecutiveStatusErrors}/${DRIVE_REMOTE_STATUS_RETRY_LIMIT}）`,
      });
      if (consecutiveStatusErrors < DRIVE_REMOTE_STATUS_RETRY_LIMIT) {
        continue;
      }
      const statusError = new Error(err.message || "遠端下載狀態連續讀取失敗");
      statusError.remoteStatusTransient = true;
      throw statusError;
    }
    const task = json.task || {};
    updateDriveTransferRow(transferId, {
      task_id: task.id || taskId,
      name: task.filename || task.url || "遠端下載",
      status: task.status || "running",
      phase: task.phase || "",
      loaded_bytes: task.loaded_bytes,
      total_bytes: task.total_bytes,
      progress_percent: task.progress_percent,
      speed_bytes_per_sec: task.speed_bytes_per_sec,
      msg: task.msg || "",
    });
    const status = $("drive-remote-download-status");
    if (status) {
      const percent = task.progress_percent === null || task.progress_percent === undefined ? "計算中" : `${Math.round(Number(task.progress_percent || 0))}%`;
      const speed = formatDriveSpeed(task.speed_bytes_per_sec);
      status.textContent = `${task.msg || "遠端下載中"} · ${percent}${speed ? ` · ${speed}` : ""}`;
    }
    if (task.status === "completed" || task.status === "paused" || task.status === "cancelled") return task;
    if (task.status === "failed") {
      const failedError = new Error(task.error || task.msg || "遠端下載失敗");
      failedError.remoteTaskFailed = true;
      throw failedError;
    }
  }
}

function remoteTaskTransferId(taskId) {
  return `remote-task-${taskId}`;
}

function applyRemoteDownloadTaskToTransfer(task) {
  if (!task?.id) return null;
  const transferId = findDriveTransferRowIdForTask(task.id) || remoteTaskTransferId(task.id);
  updateDriveTransferRow(transferId, {
    id: transferId,
    task_id: task.id,
    kind: "remote_download",
    name: task.filename || task.torrent_filename || task.url || "遠端下載",
    status: task.status || "running",
    phase: task.phase || "",
    loaded_bytes: task.loaded_bytes,
    total_bytes: task.total_bytes,
    progress_percent: task.progress_percent,
    speed_bytes_per_sec: task.speed_bytes_per_sec,
    availability_score: task.availability_score,
    availability_hint: task.availability_hint,
    msg: task.msg || "",
  });
  return transferId;
}

function resumableUploadTransferId(sessionId) {
  return `resumable-upload-${sessionId}`;
}

function findDriveTransferRowIdForSession(sessionId) {
  if (!sessionId) return "";
  const row = driveTransferRows.find((item) => item.session_id === sessionId);
  return row?.id || "";
}

function applyResumableUploadSessionToTransfer(session) {
  if (!session?.session_id) return null;
  const transferId = findDriveTransferRowIdForSession(session.session_id) || resumableUploadTransferId(session.session_id);
  const existing = driveTransferRows.find((item) => item.id === transferId) || {};
  const restoredStatus = driveResumableSessionTransferStatus(session);
  const browserStillUploading = existing.status === "running" && existing.phase === "resumable_uploading";
  const transferStatus = browserStillUploading ? "running" : restoredStatus;
  updateDriveTransferRow(transferId, {
    id: transferId,
    session_id: session.session_id,
    kind: "resumable_upload",
    name: session.filename || "分段上傳",
    status: transferStatus,
    phase: session.status || transferStatus,
    loaded_bytes: session.received_bytes,
    total_bytes: session.total_bytes,
    progress_percent: session.progress_percent,
    speed_bytes_per_sec: 0,
    source_ref: driveResumableUploadSourceRef(session.session_id),
    msg: driveResumableSessionStatusMessage(session, transferStatus),
    created_at: session.created_at || existing.created_at,
    updated_at: session.updated_at || existing.updated_at,
  });
  return transferId;
}

async function cancelResumableUploadSession(sessionId, transferId) {
  if (!sessionId) return null;
  if (!window.confirm("確定要中止這個分段上傳 session？已上傳分段會被清理。")) return null;
  const json = await driveResumableJson(`/cloud-drive/resumable-upload/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  const session = json.session || {};
  if (session.session_id) applyResumableUploadSessionToTransfer(session);
  if (transferId) updateDriveTransferRow(transferId, {
    status: "cancelled",
    phase: "aborted",
    msg: "分段上傳已中止",
    progress_percent: 100,
  });
  flash($("drive-msg"), "已中止分段上傳", true);
  return session;
}

function resumeRemoteDownloadTaskPolling(task) {
  if (!task?.id || !["queued", "running"].includes(task.status)) return;
  if (driveRemotePollingTaskIds.has(task.id)) return;
  const transferId = applyRemoteDownloadTaskToTransfer(task);
  if (!transferId) return;
  driveRemotePollingTaskIds.add(task.id);
  pollRemoteDownloadTask(task.id, transferId)
    .then(async () => {
      await loadDriveDashboard();
    })
    .catch((err) => {
      if (err?.remoteStatusTransient) {
        updateDriveTransferRow(transferId, {
          status: "running",
          phase: "status_retry_paused",
          msg: `${err.message || "狀態暫時讀取失敗"}；任務仍保留，稍後自動重試`,
          progress_percent: null,
        });
        setTimeout(() => {
          driveRemotePollingTaskIds.delete(task.id);
          resumeRemoteDownloadTaskPolling({ ...task, status: "running" });
        }, 5000);
        return;
      }
      updateDriveTransferRow(transferId, {
        status: "failed",
        phase: "failed",
        msg: err.message || "遠端下載失敗",
        progress_percent: 100,
      });
    })
    .finally(() => {
      driveRemotePollingTaskIds.delete(task.id);
    });
}

async function loadDriveResumableUploadSessions({ csrf = "" } = {}) {
  const token = csrf || getCsrfToken() || await fetchCsrfToken();
  const res = await apiFetch(API + "/cloud-drive/resumable-upload/sessions?limit=20", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": token || "" },
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) return [];
  return Array.isArray(json.sessions) ? json.sessions : [];
}

async function restoreResumableUploadSessions() {
  const sessions = await loadDriveResumableUploadSessions({ csrf: getCsrfToken() || "" });
  let waitingResumeCount = 0;
  sessions.forEach((session) => {
    applyResumableUploadSessionToTransfer(session);
    if (driveResumableSessionTransferStatus(session) === "waiting_resume") waitingResumeCount += 1;
  });
  if (waitingResumeCount > 0) {
    flash($("drive-msg"), `有 ${waitingResumeCount} 個分段上傳等待續傳，請重新選擇同一檔案接續上傳。`, true);
  }
}

async function restoreRemoteDownloadTasks() {
  await fetchCsrfToken();
  const res = await apiFetch(API + "/cloud-drive/remote-download/tasks", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" },
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) return;
  const tasks = Array.isArray(json.tasks) ? json.tasks : [];
  tasks.forEach((task) => {
    const transferId = applyRemoteDownloadTaskToTransfer(task);
    if (!transferId) return;
    if (task.status === "completed" || task.status === "failed" || task.status === "paused" || task.status === "cancelled") {
      return;
    }
    resumeRemoteDownloadTaskPolling(task);
  });
}

async function restoreDriveBackgroundTransfers() {
  try {
    await restoreRemoteDownloadTasks();
  } catch (_) {}
  try {
    await restoreResumableUploadSessions();
  } catch (_) {}
}

function triggerBrowserDownload(url, filename = "") {
  const a = document.createElement("a");
  a.href = url;
  if (filename) a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function downloadDriveFile(fileId, likelyHighRisk) {
  const known = findKnownDriveFile(fileId);
  const isE2ee = driveFileIsE2ee(known);
  const confirmed = likelyHighRisk
    ? window.confirm("此檔案可能高風險、未完整掃描或為 E2EE 密文。請確認你信任來源後再下載。")
    : false;
  if (likelyHighRisk && !confirmed) return;
  const downloadUrl = `${API}/cloud-drive/files/${encodeURIComponent(fileId)}/download${confirmed ? "?confirm_high_risk=1" : ""}`;
  if (!isE2ee) {
    triggerBrowserDownload(downloadUrl, known?.original_filename_plain_for_public || known?.display_name || "");
    return;
  }

  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(downloadUrl, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  if (!res.ok) {
    const json = await res.json().catch(() => ({}));
    alert(json.msg || "下載失敗");
    return;
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="?([^"]+)"?/i);
  let name = match ? match[1] : "download.bin";
  let outputBlob = blob;
  const keyRes = await apiFetch(API + `/cloud-drive/files/${encodeURIComponent(fileId)}/e2ee-key`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  if (keyRes.ok) {
    const keyJson = await keyRes.json().catch(() => ({}));
    if (keyJson.ok && keyJson.e2ee) {
      try {
        const passphrase = await getDriveE2eeSessionPassphrase(
          fileId,
          "請輸入此 E2EE 檔案的加密密碼。密碼不會送到伺服器；本次登入期間會暫存在瀏覽器記憶體。",
          { allowPrompt: true }
        );
        if (!passphrase) return;
        const decrypted = await decryptDriveE2eeBlob(blob, keyJson.e2ee, passphrase);
        rememberDriveE2eeSessionPassphrase(fileId, passphrase);
        outputBlob = decrypted.blob;
        name = decrypted.filename || name;
      } catch (err) {
        forgetDriveE2eeSessionPassphrase(fileId);
        alert(`${err.message || "端到端加密檔案解密失敗"}\n\n請確認輸入的是上傳此檔案時設定的 E2EE 加密密碼；伺服器無法重設或找回此密碼。`);
        return;
      }
    }
  }
  const url = URL.createObjectURL(outputBlob);
  triggerBrowserDownload(url, name);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function deleteDriveFile(fileId) {
  if (!window.confirm("永久刪除此雲端檔案？這會釋放容量並刪除伺服器實體檔案，無法還原。")) return;
  try {
    await storageAction(`/cloud-drive/files/${encodeURIComponent(fileId)}`, "DELETE");
    await loadDriveDashboard();
  } catch (err) {
    alert(err.message || "檔案刪除失敗");
  }
}

async function moveCloudFileToStorage(fileId, name) {
  const requested = window.prompt("移動到 FileManager 路徑", joinStoragePath(currentStoragePath || "/", name || "file"));
  if (requested === null) return;
  const path = normalizeStoragePath(requested, name || "file");
  if (path === "/") {
    alert("請輸入包含檔名的路徑");
    return;
  }
  try {
    await storageAction("/storage/files/attach-existing", "POST", {
      file_id: fileId,
      virtual_path: path,
      display_name: storageBaseName(path),
    });
    currentStoragePath = storageDirName(path);
    await loadDriveDashboard();
  } catch (err) {
    alert(err.message || "移動失敗");
  }
}

let currentDrivePreviewUrl = "";
let currentDrivePreviewHls = null;
let driveHlsLibraryPromise = null;
let selectedAlbumId = "";
let selectedStorageFileId = "";
let selectedStorageFilePath = "";
let selectedStorageFileIds = new Set();
let selectedStorageFolderPaths = new Set();
let currentStoragePath = "/";
let storageFilesCache = [];
let storageFoldersCache = [];
let storageAlbumsCache = [];
let selectedAlbumViewerId = "";
let pendingAlbumPickerResolve = null;
let albumThumbObjectUrls = [];
let currentAlbumFullPreviewUrl = "";
let albumPreviewSequence = [];
let albumPreviewIndex = -1;
let lastDrivePreviewClick = { fileId: "", at: 0 };
const DRIVE_FULLSCREEN_PREVIEW_MS = 450;
const DRIVE_HLS_JS_URL = "/js/hls.light.min.js?v=20260505-hlsjs";

function getAlbumThumbSize() {
  const key = typeof accountScopedStorageKey === "function" ? accountScopedStorageKey("albumThumbSize") : "albumThumbSize";
  const stored = localStorage.getItem(key) || "medium";
  return ["small", "medium", "large"].includes(stored) ? stored : "medium";
}

function setAlbumThumbSize(size) {
  const normalized = ["small", "medium", "large"].includes(size) ? size : "medium";
  const key = typeof accountScopedStorageKey === "function" ? accountScopedStorageKey("albumThumbSize") : "albumThumbSize";
  localStorage.setItem(key, normalized);
  const select = $("album-thumb-size");
  if (select) select.value = normalized;
  const grid = $("album-viewer-files");
  if (grid) {
    grid.classList.remove("album-thumb-small", "album-thumb-medium", "album-thumb-large");
    grid.classList.add(`album-thumb-${normalized}`);
  }
}

function clearAlbumThumbObjectUrls() {
  albumThumbObjectUrls.forEach((url) => URL.revokeObjectURL(url));
  albumThumbObjectUrls = [];
}

function clearDrivePreviewUrl() {
  if (currentDrivePreviewHls && typeof currentDrivePreviewHls.destroy === "function") {
    try {
      currentDrivePreviewHls.destroy();
    } catch (_) {}
  }
  currentDrivePreviewHls = null;
  if (currentDrivePreviewUrl) {
    URL.revokeObjectURL(currentDrivePreviewUrl);
    currentDrivePreviewUrl = "";
  }
}

function drivePreviewContentUrl(fileId) {
  return `${API}/cloud-drive/files/${encodeURIComponent(fileId)}/preview/content`;
}

function findKnownDriveFile(fileId) {
  const target = String(fileId || "");
  const sources = [
    ...(Array.isArray(lastDriveFiles) ? lastDriveFiles : []),
    ...(Array.isArray(storageFilesCache) ? storageFilesCache : []),
    ...(Array.isArray(albumPreviewSequence) ? albumPreviewSequence : []),
  ];
  return sources.find((file) => String(file?.id || file?.file_id || "") === target || String(file?.file_id || "") === target) || null;
}

async function fetchDriveE2eeKey(fileId, csrf) {
  const res = await apiFetch(API + `/cloud-drive/files/${encodeURIComponent(fileId)}/e2ee-key`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok || !json.e2ee) throw new Error(json.msg || "E2EE 解密資訊讀取失敗");
  return json.e2ee;
}

async function fetchDriveE2eeCiphertext(fileId, csrf) {
  const res = await apiFetch(API + `/cloud-drive/files/${encodeURIComponent(fileId)}/preview/content`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  if (!res.ok) {
    const json = await res.json().catch(() => ({}));
    throw new Error(json.msg || "E2EE 密文讀取失敗");
  }
  return res.blob();
}

async function decryptDriveE2eeFileForSession(fileId, csrf, promptText, { promptOnMiss = true } = {}) {
  const e2ee = await fetchDriveE2eeKey(fileId, csrf);
  const ciphertext = await fetchDriveE2eeCiphertext(fileId, csrf);
  const candidates = getDriveE2eeSessionPassphraseCandidates(fileId);
  if (!promptOnMiss && !candidates.length) {
    throw new Error(DRIVE_E2EE_PREVIEW_NO_RECENT_PASSWORD);
  }
  for (const passphrase of candidates) {
    try {
      const decrypted = await decryptDriveE2eeBlob(ciphertext, e2ee, passphrase);
      rememberDriveE2eeSessionPassphrase(fileId, passphrase);
      return decrypted;
    } catch (err) {
      forgetDriveE2eeSessionPassphrase(fileId);
    }
  }
  if (!promptOnMiss) {
    throw new Error(DRIVE_E2EE_PREVIEW_DECRYPT_FAILED);
  }
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const passphrase = await getDriveE2eeSessionPassphrase(fileId, promptText, { force: true, allowPrompt: true });
    if (!passphrase) return null;
    try {
      const decrypted = await decryptDriveE2eeBlob(ciphertext, e2ee, passphrase);
      rememberDriveE2eeSessionPassphrase(fileId, passphrase);
      return decrypted;
    } catch (err) {
      forgetDriveE2eeSessionPassphrase(fileId);
      if (attempt > 0) throw err;
      alert("E2EE 密碼不正確或檔案已損壞，請重新輸入。");
    }
  }
  return null;
}

async function buildDriveE2eePreview(fileId, csrf) {
  const decrypted = await decryptDriveE2eeFileForSession(
    fileId,
    csrf,
    "請輸入此 E2EE 檔案的加密密碼。密碼不會送到伺服器；本次登入期間會暫存在瀏覽器記憶體。",
    { promptOnMiss: true }
  );
  if (!decrypted) return null;
  const known = findKnownDriveFile(fileId) || {};
  const filename = decrypted.filename || known.display_name || known.original_filename_plain_for_public || "download";
  const fileLike = {
    ...known,
    id: fileId,
    display_name: filename,
    original_filename_plain_for_public: filename,
    mime_type_plain_for_public: decrypted.blob.type || known.mime_type_plain_for_public || "",
    privacy_mode: "e2ee",
    size_bytes: decrypted.blob.size,
    risk_level: known.risk_level || "unknown_encrypted",
    scan_status: known.scan_status || "skipped_e2ee",
  };
  const category = driveFileCategory(fileLike);
  const preview = {
    file_id: fileId,
    filename,
    size_bytes: decrypted.blob.size,
    privacy_mode: "e2ee",
    risk_level: fileLike.risk_level,
    scan_status: fileLike.scan_status,
    category,
    mime_type: decrypted.blob.type || fileLike.mime_type_plain_for_public || "application/octet-stream",
    render_mode: "metadata",
    previewable: ["audio", "video", "image", "pdf", "text"].includes(category),
    e2ee_browser_decrypted: true,
  };
  if (["audio", "video", "image", "pdf"].includes(category)) {
    preview.render_mode = "media";
    return { preview, blob: decrypted.blob };
  }
  if (category === "text") {
    const maxBytes = 65536;
    preview.render_mode = "text";
    preview.truncated = decrypted.blob.size > maxBytes;
    preview.text = await decrypted.blob.slice(0, maxBytes).text();
    return { preview, blob: decrypted.blob };
  }
  return { preview, blob: decrypted.blob };
}

function clearAlbumFullPreviewUrl() {
  if (currentDrivePreviewHls && typeof currentDrivePreviewHls.destroy === "function") {
    try {
      currentDrivePreviewHls.destroy();
    } catch (_) {}
  }
  currentDrivePreviewHls = null;
  if (currentAlbumFullPreviewUrl) {
    URL.revokeObjectURL(currentAlbumFullPreviewUrl);
    currentAlbumFullPreviewUrl = "";
  }
}

async function fetchDrivePreviewBlob(fileId, csrf) {
  const res = await apiFetch(API + `/cloud-drive/files/${encodeURIComponent(fileId)}/preview/content`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  if (!res.ok) {
    const json = await res.json().catch(() => ({}));
    throw new Error(json.msg || "預覽內容讀取失敗");
  }
  return res.blob();
}

function normalizeDrivePreviewBlobMime(blob, expectedMime = "") {
  const targetMime = String(expectedMime || "").trim().toLowerCase();
  const currentMime = String(blob?.type || "").trim().toLowerCase();
  if (!blob || !targetMime || targetMime === "application/octet-stream") return blob;
  if (!currentMime || currentMime === "application/octet-stream") {
    return new Blob([blob], { type: targetMime });
  }
  return blob;
}

async function fetchDrivePreviewContent(fileId, csrf, expectedMime = "") {
  const blob = normalizeDrivePreviewBlobMime(await fetchDrivePreviewBlob(fileId, csrf), expectedMime);
  clearDrivePreviewUrl();
  currentDrivePreviewUrl = URL.createObjectURL(blob);
  return currentDrivePreviewUrl;
}

function drivePreviewUsesDirectStream(preview) {
  const category = String(preview?.category || "");
  return category === "audio" || category === "video" || category === "pdf";
}

function drivePreviewStreamAsset(preview) {
  return preview?.stream_asset && typeof preview.stream_asset === "object" ? preview.stream_asset : null;
}

function drivePreviewHasReadyHls(preview) {
  const category = String(preview?.category || "");
  const stream = drivePreviewStreamAsset(preview);
  return ["audio", "video"].includes(category)
    && stream
    && stream.status === "ready"
    && !!stream.master_manifest_ready;
}

function drivePreviewHlsMasterUrl(fileId, preview) {
  const stream = drivePreviewStreamAsset(preview);
  return stream?.master_url || `${API}/cloud-drive/files/${encodeURIComponent(fileId)}/hls/master.m3u8`;
}

function drivePreviewSubtitles(preview) {
  const stream = drivePreviewStreamAsset(preview);
  const tracks = Array.isArray(stream?.subtitles) ? stream.subtitles : [];
  return tracks
    .filter((track) => track && track.name && track.url)
    .map((track) => ({
      name: String(track.name || ""),
      label: String(track.label || track.language || "字幕"),
      language: String(track.language || "und"),
      url: String(track.url || ""),
      isDefault: !!track.is_default,
    }));
}

function drivePreviewAudioTracks(preview) {
  const stream = drivePreviewStreamAsset(preview);
  const tracks = Array.isArray(stream?.audio_tracks) ? stream.audio_tracks : [];
  return tracks
    .filter((track) => track && track.name)
    .map((track) => ({
      name: String(track.name || ""),
      label: String(track.label || track.language || track.name || "音軌"),
      language: String(track.language || "und"),
      playlistUrl: String(track.playlist_url || track.url || ""),
      streamIndex: Number(track.stream_index ?? -1),
      isDefault: !!track.is_default,
    }));
}

function drivePreviewNativeDirectSupport(preview) {
  const category = String(preview?.category || "");
  const filename = String(preview?.filename || preview?.display_name || preview?.name || "");
  const mime = String(preview?.mime_type || preview?.mime_type_plain_for_public || "").toLowerCase();
  const ext = driveFileExtension(filename);
  if (category === "audio") {
    return ["audio/mpeg", "audio/mp4", "audio/aac", "audio/wav", "audio/x-wav", "audio/ogg", "audio/webm"].includes(mime)
      || [".mp3", ".m4a", ".aac", ".wav", ".oga", ".ogg", ".opus", ".weba"].includes(ext);
  }
  if (category === "video") {
    return ["video/mp4", "video/webm", "video/ogg"].includes(mime)
      || [".mp4", ".m4v", ".webm", ".ogv"].includes(ext);
  }
  return true;
}

function drivePreviewDirectStatus(preview) {
  const stream = drivePreviewStreamAsset(preview);
  const status = stream?.direct_browser_playback || preview?.direct_browser_playback || null;
  if (status && typeof status === "object") return status;
  return {
    available: drivePreviewNativeDirectSupport(preview),
    reason: drivePreviewNativeDirectSupport(preview) ? "browser_native_likely" : "unknown_direct_browser_support",
    detail: drivePreviewNativeDirectSupport(preview)
      ? "原始格式屬於瀏覽器原生播放的常見組合。"
      : "原始格式無法判定為瀏覽器原生可播放，請使用即時轉封裝或 HLS。",
  };
}

function drivePreviewDirectAvailable(preview) {
  return !!drivePreviewDirectStatus(preview).available;
}

function drivePreviewDirectCompatibilitySummary(preview) {
  const status = drivePreviewDirectStatus(preview);
  if (status.available) return "最低費率；直接送原始檔，適合瀏覽器原生支援的 MP4/WebM/音訊格式。";
  return status.detail || "原始格式不是瀏覽器穩定支援的直接播放格式，請使用即時轉封裝或 HLS。";
}

function drivePreviewDirectWarningMarkup(preview) {
  const status = drivePreviewDirectStatus(preview);
  if (status.available) return "";
  return `<div class="drive-card-sub drive-preview-stream-warning">${sanitize(status.detail || "此檔案格式無法由瀏覽器直接播放；請改用 Standard 即時轉封裝或 Premium HLS。")}</div>`;
}

function drivePreviewIsAppleNativeMediaClient() {
  const ua = String(navigator.userAgent || "");
  const platform = String(navigator.platform || "");
  return /\b(iPad|iPhone|iPod)\b/i.test(ua) || (platform === "MacIntel" && Number(navigator.maxTouchPoints || 0) > 1);
}

function drivePreviewRealtimeProxyUsable(preview, realtimeAvailable) {
  if (!realtimeAvailable) return false;
  if (drivePreviewIsAppleNativeMediaClient() && String(preview?.privacy_mode || "") === "server_encrypted") return false;
  return true;
}

function drivePreviewNoPlayableModeMarkup(preview) {
  if (preview?.category !== "video" && preview?.category !== "audio") return "";
  return '<div class="drive-empty">此檔案目前沒有符合標準的預覽串流：Direct 僅限瀏覽器原生格式，Realtime Proxy 目前不可用，Prepared HLS 尚未就緒。</div>';
}

function drivePreviewServiceOptions(preview) {
  const category = String(preview?.category || "");
  if (!["audio", "video"].includes(category)) return [];
  const stream = drivePreviewStreamAsset(preview);
  const proxy = stream?.realtime_proxy || {};
  const rawRealtimeAvailable = !!(stream?.realtime_proxy_url || proxy.url) && proxy.available !== false;
  const realtimeAvailable = drivePreviewRealtimeProxyUsable(preview, rawRealtimeAvailable);
  const hlsAvailable = drivePreviewHasReadyHls(preview);
  const directAvailable = drivePreviewDirectAvailable(preview);
  const profilePolicy = stream?.premium_hls_profile_policy || {};
  const profileDriftSuffix = profilePolicy.profile_drift ? " 目前 HLS 資產與現行 Premium profile 不一致，建議排程重建。" : "";
  return [
    {
      mode: "direct",
      label: "Basic · 直接串流",
      available: directAvailable,
      summary: drivePreviewDirectCompatibilitySummary(preview),
    },
    {
      mode: "realtime_proxy",
      label: "Standard · 即時轉封裝",
      available: realtimeAvailable,
      summary: rawRealtimeAvailable && !realtimeAvailable
        ? "此裝置對加密長片的即時 fragmented MP4 支援不穩，請使用 Premium HLS。"
        : "中階費率；用即時 CPU 處理 MKV 或特殊音訊，一次輸出選定音軌。",
    },
    {
      mode: "prepared_hls",
      label: "Premium · 預處理 HLS",
      available: hlsAvailable,
      summary: "最高費率；預先建立分段串流，支援多音軌、多字幕與穩定跳轉。" + profileDriftSuffix,
    },
  ];
}

function drivePreviewServiceModeStorageKey(fileId) {
  return `hackme_web.drive_preview_service_mode.${String(fileId || "")}`;
}

function selectedDrivePreviewServiceMode(fileId, preview) {
  const options = drivePreviewServiceOptions(preview);
  const availableModes = new Set(options.filter((option) => option.available).map((option) => option.mode));
  let saved = "";
  try {
    saved = localStorage.getItem(drivePreviewServiceModeStorageKey(fileId)) || "";
  } catch (_) {
    saved = "";
  }
  if (saved && availableModes.has(saved)) return saved;
  if (availableModes.has("direct")) return "direct";
  if (availableModes.has("realtime_proxy")) return "realtime_proxy";
  if (availableModes.has("prepared_hls")) return "prepared_hls";
  return "";
}

function saveDrivePreviewServiceMode(fileId, mode) {
  try {
    localStorage.setItem(drivePreviewServiceModeStorageKey(fileId), String(mode || ""));
  } catch (_) {}
}

function selectedDrivePreviewAudioTrack(preview, { fullscreen = false } = {}) {
  const tracks = drivePreviewAudioTracks(preview);
  if (!tracks.length) return null;
  const prefix = fullscreen ? "drive-fullscreen" : "drive-preview";
  const select = $(`${prefix}-audio-track-select`);
  if (select) {
    const selected = Math.max(0, Math.min(tracks.length - 1, Number(select.value || 0)));
    return tracks[selected] || tracks[0];
  }
  return tracks.find((track) => track.isDefault) || tracks[0];
}

function drivePreviewRealtimeProxyUrl(fileId, preview, startSeconds = 0, { fullscreen = false } = {}) {
  const stream = drivePreviewStreamAsset(preview);
  const raw = String(stream?.realtime_proxy_url || stream?.realtime_proxy?.url || `${API}/cloud-drive/files/${encodeURIComponent(fileId)}/realtime-proxy`).trim();
  if (!raw) return "";
  const url = new URL(raw, window.location.origin);
  const track = selectedDrivePreviewAudioTrack(preview, { fullscreen });
  if (track?.name) url.searchParams.set("audio", track.name);
  const start = Number(startSeconds || 0);
  if (Number.isFinite(start) && start > 0) url.searchParams.set("start", String(Math.max(0, Math.round(start * 1000) / 1000)));
  return `${url.pathname}${url.search}`;
}

function driveSubtitleShiftStorageKey(fileId) {
  return `hackme_web.drive_subtitle_shift_ms.${String(fileId || "")}`;
}

function clampDriveSubtitleShiftMs(value) {
  const parsed = Number(value || 0);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(-3600000, Math.min(3600000, Math.round(parsed)));
}

function driveSubtitleShiftSecondsValue(ms) {
  const seconds = clampDriveSubtitleShiftMs(ms) / 1000;
  return Number.isInteger(seconds) ? String(seconds) : seconds.toFixed(1).replace(/\.0$/, "");
}

function getDriveSubtitleShiftMs(fileId) {
  try {
    return clampDriveSubtitleShiftMs(localStorage.getItem(driveSubtitleShiftStorageKey(fileId)) || 0);
  } catch (_) {
    return 0;
  }
}

function setDriveSubtitleShiftMs(fileId, value) {
  const offset = clampDriveSubtitleShiftMs(value);
  try {
    const key = driveSubtitleShiftStorageKey(fileId);
    if (offset) localStorage.setItem(key, String(offset));
    else localStorage.removeItem(key);
  } catch (_) {}
  return offset;
}

function driveSubtitleTrackStorageKey(fileId) {
  return `hackme_web.drive_subtitle_track.${String(fileId || "")}`;
}

function defaultDriveSubtitleTrackSelection(preview) {
  const subtitles = drivePreviewSubtitles(preview);
  if (!subtitles.length) return "off";
  const defaultIndex = subtitles.findIndex((track) => track.isDefault);
  return String(defaultIndex >= 0 ? defaultIndex : 0);
}

function getDriveSubtitleTrackSelection(fileId, preview) {
  const subtitles = drivePreviewSubtitles(preview);
  if (!subtitles.length) return "off";
  try {
    const saved = localStorage.getItem(driveSubtitleTrackStorageKey(fileId));
    if (saved === "off") return "off";
    const index = Number(saved);
    if (Number.isInteger(index) && index >= 0 && index < subtitles.length) return String(index);
  } catch (_) {}
  return defaultDriveSubtitleTrackSelection(preview);
}

function setDriveSubtitleTrackSelection(fileId, value) {
  const normalized = String(value || "off");
  try {
    const key = driveSubtitleTrackStorageKey(fileId);
    if (normalized === "off") localStorage.setItem(key, "off");
    else localStorage.setItem(key, normalized);
  } catch (_) {}
  return normalized;
}

function selectedDriveSubtitleTrackIndex(preview, fileId) {
  const selected = getDriveSubtitleTrackSelection(fileId, preview);
  if (selected === "off") return -1;
  const index = Number(selected);
  return Number.isInteger(index) ? index : -1;
}

function driveSubtitleUrlWithShift(url, shiftMs) {
  const raw = String(url || "");
  if (!raw) return raw;
  const offset = clampDriveSubtitleShiftMs(shiftMs);
  try {
    const parsed = new URL(raw, window.location.origin);
    if (offset) parsed.searchParams.set("shift_ms", String(offset));
    else parsed.searchParams.delete("shift_ms");
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch (_) {
    if (!offset) return raw;
    const separator = raw.includes("?") ? "&" : "?";
    return `${raw}${separator}shift_ms=${encodeURIComponent(String(offset))}`;
  }
}

function syncDrivePreviewSubtitleTracks(player, preview, fileId = "") {
  if (!player) return;
  Array.from(player.querySelectorAll('track[data-drive-preview-subtitle="1"]')).forEach((track) => track.remove());
  const shiftMs = getDriveSubtitleShiftMs(fileId);
  const subtitles = drivePreviewSubtitles(preview);
  const selectedIndex = selectedDriveSubtitleTrackIndex(preview, fileId);
  subtitles.forEach((track, index) => {
    const el = document.createElement("track");
    el.kind = "subtitles";
    el.label = track.label || track.language || "字幕";
    el.srclang = track.language || "und";
    el.src = driveSubtitleUrlWithShift(track.url, shiftMs);
    el.dataset.drivePreviewSubtitle = "1";
    if (index === selectedIndex) el.default = true;
    player.appendChild(el);
  });
  const applyModes = () => {
    const tracks = player.textTracks || [];
    for (let index = 0; index < tracks.length; index += 1) {
      tracks[index].mode = index === selectedIndex ? "showing" : "disabled";
    }
  };
  applyModes();
  window.setTimeout(applyModes, 0);
}

function driveBrowserSupportsNativeHls(mediaType = "video") {
  const probe = document.createElement(mediaType === "audio" ? "audio" : "video");
  return !!(probe && typeof probe.canPlayType === "function" && probe.canPlayType("application/vnd.apple.mpegurl"));
}

function loadDriveHlsLibrary() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (driveHlsLibraryPromise) return driveHlsLibraryPromise;
  driveHlsLibraryPromise = new Promise((resolve, reject) => {
    const existing = document.querySelector('script[data-drive-hls-js="1"]');
    if (existing) {
      existing.addEventListener("load", () => resolve(window.Hls || null), { once: true });
      existing.addEventListener("error", () => reject(new Error("HLS.js 載入失敗")), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = DRIVE_HLS_JS_URL;
    script.async = true;
    script.defer = true;
    script.dataset.driveHlsJs = "1";
    script.onload = () => resolve(window.Hls || null);
    script.onerror = () => reject(new Error("HLS.js 載入失敗"));
    document.head.appendChild(script);
  }).catch((err) => {
    driveHlsLibraryPromise = null;
    throw err;
  });
  return driveHlsLibraryPromise;
}

function driveSubtitleShiftControlsMarkup(preview, { fullscreen = false, fileId = "", selectedMode = "" } = {}) {
  const subtitles = drivePreviewSubtitles(preview);
  const audioTracks = drivePreviewAudioTracks(preview);
  const serviceOptions = drivePreviewServiceOptions(preview);
  const mode = selectedMode || selectedDrivePreviewServiceMode(fileId, preview);
  const showAudio = ["prepared_hls", "realtime_proxy"].includes(mode);
  if (serviceOptions.length < 2 && !subtitles.length && (!showAudio || audioTracks.length < 2)) return "";
  const prefix = fullscreen ? "drive-fullscreen" : "drive-preview";
  const selectedOption = serviceOptions.find((option) => option.mode === mode) || serviceOptions[0] || {};
  const serviceMarkup = serviceOptions.length >= 2 ? `
    <div class="video-quality-control drive-service-mode-control">
      <label for="${prefix}-service-mode-select">方案</label>
      <select id="${prefix}-service-mode-select">
        ${serviceOptions.map((option) => `<option value="${sanitize(option.mode)}"${option.mode === mode ? " selected" : ""}${option.available ? "" : " disabled"}>${sanitize(option.label)}</option>`).join("")}
      </select>
      <span class="drive-card-sub">${sanitize(selectedOption.summary || "")}</span>
    </div>
  ` : "";
  const defaultAudioIndex = Math.max(0, audioTracks.findIndex((track) => track.isDefault));
  const audioMarkup = showAudio && audioTracks.length >= 2 ? `
    <div class="video-quality-control drive-audio-track-control">
      <label for="${prefix}-audio-track-select">音軌</label>
      <select id="${prefix}-audio-track-select">
        ${audioTracks.map((track, index) => `<option value="${index}" data-track-name="${sanitize(track.name)}"${index === defaultAudioIndex ? " selected" : ""}>${sanitize(track.label)}</option>`).join("")}
      </select>
      ${mode === "realtime_proxy" ? '<span class="drive-card-sub">Standard 即時轉封裝會重新開啟串流以套用音軌。</span>' : ""}
    </div>
  ` : "";
  const selectedSubtitle = getDriveSubtitleTrackSelection(fileId, preview);
  const subtitleSelectMarkup = subtitles.length ? `
    <div class="video-quality-control drive-subtitle-track-control">
      <label for="${prefix}-subtitle-track-select">字幕</label>
      <select id="${prefix}-subtitle-track-select">
        <option value="off"${selectedSubtitle === "off" ? " selected" : ""}>關閉</option>
        ${subtitles.map((track, index) => `<option value="${index}"${selectedSubtitle === String(index) ? " selected" : ""}>${sanitize(track.label)}</option>`).join("")}
      </select>
    </div>
  ` : "";
  const subtitleMarkup = subtitles.length ? `
    <details class="video-quality-control drive-subtitle-shift-control">
      <summary>字幕延遲微調</summary>
      <label for="${prefix}-subtitle-shift-seconds">Offset</label>
      <button class="btn btn-sm" type="button" data-drive-subtitle-shift-step="-500">-0.5s</button>
      <input id="${prefix}-subtitle-shift-seconds" type="number" min="-3600" max="3600" step="0.1" value="${sanitize(driveSubtitleShiftSecondsValue(getDriveSubtitleShiftMs(fileId)))}" />
      <button class="btn btn-sm" type="button" data-drive-subtitle-shift-step="500">+0.5s</button>
      <button class="btn btn-sm" type="button" data-drive-subtitle-shift-reset="1">重置</button>
    </details>
  ` : "";
  return `
    ${serviceMarkup}
    ${audioMarkup}
    ${subtitleSelectMarkup}
    ${subtitleMarkup}
  `;
}

function driveHlsPlayerMarkup(preview, { fullscreen = false, fileId = "" } = {}) {
  const playerId = fullscreen ? "drive-fullscreen-hls-player" : "drive-preview-hls-player";
  const autoplay = fullscreen ? "autoplay " : "";
  const controls = driveSubtitleShiftControlsMarkup(preview, { fullscreen, fileId, selectedMode: "prepared_hls" });
  if (preview.category === "audio") return `<audio id="${playerId}" controls ${autoplay}preload="metadata"></audio>${controls}`;
  return `<video id="${playerId}" controls ${autoplay}preload="metadata" playsinline></video>${controls}`;
}

function driveDirectPlayerMarkup(fileId, preview, url, { fullscreen = false } = {}) {
  const playerId = fullscreen ? "drive-fullscreen-hls-player" : "drive-preview-hls-player";
  const autoplay = fullscreen ? "autoplay " : "";
  const controls = driveSubtitleShiftControlsMarkup(preview, { fullscreen, fileId, selectedMode: "direct" });
  const warning = drivePreviewDirectWarningMarkup(preview);
  if (preview.category === "audio") return `<audio id="${playerId}" controls ${autoplay}preload="metadata" src="${sanitize(url)}"></audio>${warning}${controls}`;
  return `<video id="${playerId}" controls ${autoplay}preload="metadata" playsinline src="${sanitize(url)}"></video>${warning}${controls}`;
}

function driveRealtimeProxyPlayerMarkup(fileId, preview, { fullscreen = false } = {}) {
  const playerId = fullscreen ? "drive-fullscreen-hls-player" : "drive-preview-hls-player";
  const autoplay = fullscreen ? "autoplay " : "";
  const src = drivePreviewRealtimeProxyUrl(fileId, preview, 0, { fullscreen });
  const controls = driveSubtitleShiftControlsMarkup(preview, { fullscreen, fileId, selectedMode: "realtime_proxy" });
  if (preview.category === "audio") return `<audio id="${playerId}" controls ${autoplay}preload="metadata" src="${sanitize(src)}"></audio>${controls}`;
  return `<video id="${playerId}" controls ${autoplay}preload="metadata" playsinline src="${sanitize(src)}"></video>${controls}`;
}

function attachDrivePlainMediaPreview(fileId, preview, { fullscreen = false } = {}) {
  const player = $(fullscreen ? "drive-fullscreen-hls-player" : "drive-preview-hls-player");
  if (!player) return;
  player.dataset.fileId = String(fileId || "");
  syncDrivePreviewSubtitleTracks(player, preview, fileId);
  bindDriveSubtitleShiftControls(fileId, preview, player, { fullscreen });
  const stream = drivePreviewStreamAsset(preview);
  const proxy = stream?.realtime_proxy || {};
  const canUseProxy = !!(stream?.realtime_proxy_url || proxy.url) && proxy.available !== false;
  if (selectedDrivePreviewServiceMode(fileId, preview) !== "direct" || !canUseProxy || drivePreviewDirectAvailable(preview)) return;
  let switched = false;
  const switchToProxy = () => {
    if (switched) return;
    switched = true;
    saveDrivePreviewServiceMode(fileId, "realtime_proxy");
    if (fullscreen) {
      previewAlbumFileFullscreen(fileId, preview?.filename || "", { skipRepeatCheck: true }).catch((err) => alert(err.message || "預覽失敗"));
    } else {
      previewDriveFile(fileId, { skipRepeatCheck: true, fileName: preview?.filename || "" }).catch((err) => alert(err.message || "預覽失敗"));
    }
  };
  player.addEventListener("error", switchToProxy, { once: true });
  window.setTimeout(() => {
    if (!switched && player.readyState < 2 && !player.paused) switchToProxy();
  }, 8000);
}

async function attachDriveHlsPreview(fileId, preview, { fullscreen = false } = {}) {
  const player = $(fullscreen ? "drive-fullscreen-hls-player" : "drive-preview-hls-player");
  if (!player) return;
  player.dataset.fileId = String(fileId || "");
  const masterUrl = drivePreviewHlsMasterUrl(fileId, preview);
  const mediaType = preview.category === "audio" ? "audio" : "video";
  syncDrivePreviewSubtitleTracks(player, preview, fileId);
  bindDriveSubtitleShiftControls(fileId, preview, player, { fullscreen });
  if (driveBrowserSupportsNativeHls(mediaType)) {
    player.src = masterUrl;
    return;
  }
  const HlsCtor = await loadDriveHlsLibrary();
  if (!HlsCtor || typeof HlsCtor.isSupported !== "function" || !HlsCtor.isSupported()) {
    const stream = drivePreviewStreamAsset(preview);
    const proxy = stream?.realtime_proxy || {};
    if (drivePreviewRealtimeProxyUsable(preview, !!(stream?.realtime_proxy_url || proxy.url) && proxy.available !== false)) {
      player.src = drivePreviewRealtimeProxyUrl(fileId, preview, 0, { fullscreen });
      return;
    }
    player.removeAttribute("src");
    player.load();
    return;
  }
  if (currentDrivePreviewHls && typeof currentDrivePreviewHls.destroy === "function") {
    try {
      currentDrivePreviewHls.destroy();
    } catch (_) {}
  }
  const hls = new HlsCtor({ enableWorker: true, backBufferLength: 30 });
  currentDrivePreviewHls = hls;
  hls.loadSource(masterUrl);
  hls.attachMedia(player);
  if (HlsCtor.Events.AUDIO_TRACKS_UPDATED) {
    hls.on(HlsCtor.Events.AUDIO_TRACKS_UPDATED, () => applyDrivePreviewAudioTrack(preview, { fullscreen, fileId }));
  }
}

function bindDriveSubtitleShiftControls(fileId, preview, player, { fullscreen = false } = {}) {
  const prefix = fullscreen ? "drive-fullscreen" : "drive-preview";
  const serviceSelect = $(`${prefix}-service-mode-select`);
  if (serviceSelect) {
    serviceSelect.addEventListener("change", () => {
      saveDrivePreviewServiceMode(fileId, serviceSelect.value);
      if (fullscreen) {
        previewAlbumFileFullscreen(fileId, preview?.filename || "", { skipRepeatCheck: true }).catch((err) => alert(err.message || "預覽失敗"));
      } else {
        previewDriveFile(fileId, { skipRepeatCheck: true, fileName: preview?.filename || "" }).catch((err) => alert(err.message || "預覽失敗"));
      }
    });
  }
  const audioSelect = $(`${prefix}-audio-track-select`);
  if (audioSelect) {
    audioSelect.dataset.fileId = String(fileId || "");
    audioSelect.addEventListener("change", () => applyDrivePreviewAudioTrack(preview, { fullscreen, fileId }));
  }
  const subtitleSelect = $(`${prefix}-subtitle-track-select`);
  if (subtitleSelect) {
    subtitleSelect.addEventListener("change", () => {
      setDriveSubtitleTrackSelection(fileId, subtitleSelect.value);
      syncDrivePreviewSubtitleTracks(player, preview, fileId);
    });
  }
  const input = $(`${prefix}-subtitle-shift-seconds`);
  if (!input) return;
  const applyShift = (nextMs) => {
    const offset = setDriveSubtitleShiftMs(fileId, nextMs);
    input.value = driveSubtitleShiftSecondsValue(offset);
    syncDrivePreviewSubtitleTracks(player, preview, fileId);
  };
  input.addEventListener("change", () => applyShift(Number(input.value || 0) * 1000));
  const container = input.closest(".drive-subtitle-shift-control") || document;
  container.querySelectorAll("[data-drive-subtitle-shift-step]").forEach((button) => {
    button.addEventListener("click", () => applyShift(getDriveSubtitleShiftMs(fileId) + Number(button.dataset.driveSubtitleShiftStep || 0)));
  });
  const reset = container.querySelector("[data-drive-subtitle-shift-reset]");
  if (reset) reset.addEventListener("click", () => applyShift(0));
}

function applyDrivePreviewAudioTrack(preview, { fullscreen = false, fileId = "" } = {}) {
  const tracks = drivePreviewAudioTracks(preview);
  const prefix = fullscreen ? "drive-fullscreen" : "drive-preview";
  const select = $(`${prefix}-audio-track-select`);
  const player = $(fullscreen ? "drive-fullscreen-hls-player" : "drive-preview-hls-player");
  if (selectedDrivePreviewServiceMode(fileId, preview) === "realtime_proxy") {
    const resolvedFileId = fileId || select?.dataset.fileId || player?.dataset.fileId || "";
    if (!select || tracks.length < 2 || !player || !resolvedFileId) return;
    const selected = Math.max(0, Math.min(tracks.length - 1, Number(select.value || 0)));
    const resumeAt = Number(player.currentTime || 0);
    const wasPaused = player.paused;
    const nextUrl = drivePreviewRealtimeProxyUrl(resolvedFileId, preview, resumeAt, { fullscreen });
    if (!nextUrl) return;
    player.src = nextUrl;
    if (typeof player.load === "function") player.load();
    if (!wasPaused && typeof player.play === "function") player.play().catch(() => {});
    return;
  }
  if (!select || tracks.length < 2) return;
  const selected = Math.max(0, Math.min(tracks.length - 1, Number(select.value || 0)));
  const chosen = tracks[selected];
  const chosenName = String(chosen?.name || "").toLowerCase();
  const chosenLabel = String(chosen?.label || "").toLowerCase();
  const chosenLanguage = String(chosen?.language || "").toLowerCase();
  const chosenUrl = String(chosen?.playlistUrl || "").toLowerCase();
  const chosenPath = (() => {
    try {
      return chosenUrl ? new URL(chosenUrl, window.location.origin).pathname.toLowerCase() : "";
    } catch (_) {
      return chosenUrl;
    }
  })();
  const matchesChosenTrack = (track) => {
    const name = String(track?.name || track?.label || "").toLowerCase();
    const label = String(track?.label || track?.name || "").toLowerCase();
    const lang = String(track?.lang || track?.language || "").toLowerCase();
    const uri = String(track?.url || track?.uri || track?.attrs?.URI || track?.details?.url || "").toLowerCase();
    return (chosenName && (name === chosenName || label === chosenName || uri.includes(`/${chosenName}/`) || uri.endsWith(`${chosenName}/playlist.m3u8`)))
      || (chosenLabel && (name === chosenLabel || label === chosenLabel))
      || (chosenPath && (uri === chosenPath || uri.endsWith(chosenPath) || uri.includes(chosenPath)))
      || (chosenLanguage && chosenLanguage !== "und" && lang === chosenLanguage);
  };
  const hls = currentDrivePreviewHls;
  if (hls && Array.isArray(hls.audioTracks)) {
    let index = hls.audioTracks.findIndex(matchesChosenTrack);
    if (index < 0 && selected >= 0 && selected < hls.audioTracks.length) index = selected;
    if (index >= 0) hls.audioTrack = index;
    return;
  }
  const nativeTracks = player?.audioTracks;
  if (nativeTracks && typeof nativeTracks.length === "number") {
    for (let index = 0; index < nativeTracks.length; index += 1) {
      nativeTracks[index].enabled = !!matchesChosenTrack(nativeTracks[index]);
    }
  }
}

async function resolveDrivePreviewMediaUrl(fileId, csrf, preview, { fullscreen = false } = {}) {
  if (drivePreviewUsesDirectStream(preview)) {
    if (fullscreen) clearAlbumFullPreviewUrl();
    else clearDrivePreviewUrl();
    return drivePreviewContentUrl(fileId);
  }
  return fetchDrivePreviewContent(fileId, csrf, preview?.mime_type || "");
}

function renderDrivePdfPreview(url, title, { encrypted = false } = {}) {
  const safeTitle = sanitize(title || "PDF preview");
  const message = encrypted
    ? "這份 PDF 已在瀏覽器解密。若內嵌檢視器無法開啟，請改用新分頁或直接下載。"
    : "若瀏覽器內建 PDF 檢視器未載入，請改用新分頁開啟或直接下載。";
  return `
    <div class="drive-pdf-preview">
      <iframe src="${url}" title="${safeTitle}" loading="lazy"></iframe>
      <div class="drive-card-sub">${message}</div>
      <div class="drive-file-actions drive-pdf-preview-actions">
        <a class="btn btn-primary" href="${url}" target="_blank" rel="noopener">在新分頁開啟 PDF</a>
        <a class="btn" href="${url}" download>下載 PDF</a>
      </div>
    </div>
  `;
}

function renderDriveDecryptedPreviewMedia(preview, blob, { fullscreen = false } = {}) {
  const normalizedBlob = normalizeDrivePreviewBlobMime(blob, preview?.mime_type || "");
  const url = URL.createObjectURL(normalizedBlob);
  const title = sanitize(preview.filename || "E2EE preview");
  if (fullscreen) {
    clearAlbumFullPreviewUrl();
    currentAlbumFullPreviewUrl = url;
  } else {
    clearDrivePreviewUrl();
    currentDrivePreviewUrl = url;
  }
  if (preview.category === "audio") return `<audio controls ${fullscreen ? "autoplay " : ""}src="${url}"></audio>`;
  if (preview.category === "video") return `<video controls ${fullscreen ? "autoplay " : ""}src="${url}"></video>`;
  if (preview.category === "image") return `<img src="${url}" alt="${title}" />`;
  if (preview.category === "pdf") return renderDrivePdfPreview(url, title, { encrypted: true });
  return `<div class="drive-empty">此 E2EE 檔案已在瀏覽器解密，但目前不支援 inline 預覽。</div>`;
}

function closeDrivePreview() {
  const panel = $("drive-preview-panel");
  const card = $("drive-preview-card");
  clearDrivePreviewUrl();
  if (panel) panel.innerHTML = `<div class="drive-empty">請從左側檔案清單選擇要預覽的檔案</div>`;
  if (card) {
    card.style.display = "";
    card.classList.remove("drive-preview-mobile-dialog");
    card.setAttribute("aria-modal", "false");
  }
  const anyOverlayOpen = document.querySelector(".user-edit-overlay.show, .album-full-preview-overlay.show");
  if (!anyOverlayOpen) document.body.classList.remove("modal-open");
  lastDrivePreviewClick = { fileId: "", at: 0 };
}

function closeAlbumFullPreview() {
  const overlay = $("album-full-preview-overlay");
  const body = $("album-full-preview-body");
  clearAlbumFullPreviewUrl();
  albumPreviewIndex = -1;
  if (body) body.innerHTML = "";
  if (overlay) {
    overlay.classList.remove("show");
    overlay.setAttribute("aria-hidden", "true");
  }
  document.body.classList.remove("modal-open");
}

function albumPreviewFileName(file) {
  return albumFileDisplayName(file || {}) || "圖片預覽";
}

function setAlbumPreviewSequence(files = [], fileId = "") {
  const rows = (Array.isArray(files) ? files : [])
    .filter((file) => file?.file_id && (typeof driveFileIsImage !== "function" || driveFileIsImage(file)));
  albumPreviewSequence = rows;
  albumPreviewIndex = rows.findIndex((file) => String(file.file_id) === String(fileId || ""));
}

function albumPreviewCurrentCountLabel() {
  if (!albumPreviewSequence.length || albumPreviewIndex < 0) return "";
  return `${albumPreviewIndex + 1} / ${albumPreviewSequence.length}`;
}

function updateAlbumPreviewControls() {
  const hasMany = albumPreviewSequence.length > 1 && albumPreviewIndex >= 0;
  document.querySelectorAll("[data-drive-action='album-preview-prev'], [data-drive-action='album-preview-next']").forEach((button) => {
    button.disabled = !hasMany;
    button.classList.toggle("is-disabled", !hasMany);
  });
}

function renderDrivePreviewMetadata(preview, fileId) {
  const panel = $("drive-preview-panel");
  const card = $("drive-preview-card");
  if (!panel || !card) return "";
  showDrivePreviewCard();
  return `
    <div>
      <strong>${sanitize(preview.filename || "preview")}</strong>
      <div class="drive-card-sub">
        ${sanitize(preview.category || "-")} · ${formatDriveBytes(preview.size_bytes || 0)} · ${sanitize(preview.mime_type || "-")}
        · ${sanitize(drivePrivacyModeLabel(preview.privacy_mode))} · risk=${sanitize(preview.risk_level || "-")} · scan=${sanitize(preview.scan_status || "-")}
      </div>
    </div>
    <div class="drive-file-actions" style="justify-content:flex-start;">
      <button class="btn btn-primary" type="button" data-drive-action="download" data-file-id="${sanitize(fileId)}" data-warn="${driveFileNeedsWarning(preview) ? "1" : "0"}">下載</button>
      ${preview.render_mode === "text" ? `<button class="btn" type="button" data-drive-action="edit-text" data-file-id="${sanitize(fileId)}">編輯文字</button>` : ""}
      <button class="btn" type="button" data-drive-action="close-preview">關閉預覽</button>
    </div>
  `;
}

function isDriveE2eeServerPreviewError(response, payload) {
  const msg = String(payload?.msg || payload?.message || "");
  return response?.status === 403 && msg.includes("E2EE") && msg.includes("伺服器預覽");
}

function isDriveMobilePreviewViewport() {
  if (typeof window === "undefined") return false;
  try {
    if (window.matchMedia && window.matchMedia("(max-width: 720px)").matches) return true;
  } catch (err) {
    // Fall back to measured viewport widths below.
  }
  const innerWidth = Number(window.innerWidth || 0);
  const docWidth = typeof document === "undefined" ? 0 : Number(document.documentElement?.clientWidth || 0);
  const widths = [innerWidth, docWidth].filter((width) => Number.isFinite(width) && width > 0);
  return widths.length > 0 && Math.min(...widths) <= 720;
}

function shouldOpenDriveFullscreen(fileId, options = {}) {
  if (options.forceFullscreen) return true;
  if (isDriveMobilePreviewViewport() && !options.forceFullscreen) return false;
  if (options.forceInlinePreview) return false;
  if (options.skipRepeatCheck) return false;
  const now = Date.now();
  const repeated = lastDrivePreviewClick.fileId === String(fileId || "") && now - lastDrivePreviewClick.at <= DRIVE_FULLSCREEN_PREVIEW_MS;
  lastDrivePreviewClick = { fileId: String(fileId || ""), at: now };
  return repeated;
}

function showDrivePreviewCard() {
  const card = $("drive-preview-card");
  if (!card) return null;
  const mobileDialog = isDriveMobilePreviewViewport();
  card.style.display = "";
  card.classList.toggle("drive-preview-mobile-dialog", mobileDialog);
  card.setAttribute("aria-modal", mobileDialog ? "true" : "false");
  if (mobileDialog) document.body.classList.add("modal-open");
  return card;
}

function renderDriveArchiveEntries(entries) {
  const rows = Array.isArray(entries) ? entries : [];
  if (!rows.length) return `<div class="drive-empty">壓縮檔內無可列出的項目</div>`;
  return `
    <div class="drive-archive-list" role="list">
      ${rows.map((entry) => {
        const isDir = !!entry.is_dir;
        const kind = isDir ? "資料夾" : "檔案";
        const name = sanitize(entry.name || "-");
        const size = entry.size === null || entry.size === undefined ? "-" : formatDriveBytes(entry.size || 0);
        const compressed = entry.compressed_size === null || entry.compressed_size === undefined
          ? "-"
          : formatDriveBytes(entry.compressed_size || 0);
        const note = entry.note ? sanitize(entry.note) : "";
        return `
          <div class="drive-archive-entry" role="listitem">
            <div class="drive-archive-entry-main">
              <span class="drive-archive-kind">${kind}</span>
              <strong class="drive-archive-name">${name}</strong>
            </div>
            <div class="drive-archive-entry-meta">
              <span>大小 ${size}</span>
              <span>壓縮後 ${compressed}</span>
              ${note ? `<span>${note}</span>` : ""}
            </div>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

async function previewDriveFile(fileId, options = {}) {
  const knownFile = findKnownDriveFile(fileId);
  if (driveFileIsE2ee(knownFile)) {
    if (shouldOpenDriveFullscreen(fileId, options)) {
      return previewAlbumFileFullscreen(fileId, options.fileName || "");
    }
    return previewDriveE2eeFile(fileId);
  }
  if (shouldOpenDriveFullscreen(fileId, options)) {
    return previewAlbumFileFullscreen(fileId, options.fileName || "");
  }
  const panel = $("drive-preview-panel");
  showDrivePreviewCard();
  if (panel) panel.innerHTML = `<div class="drive-empty">讀取預覽中...</div>`;
  try {
    await fetchCsrfToken();
    const csrf = getCsrfToken();
    const res = await apiFetch(API + `/cloud-drive/files/${encodeURIComponent(fileId)}/preview`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok && isDriveE2eeServerPreviewError(res, json)) {
      return previewDriveE2eeFile(fileId);
    }
    if (!json.ok) throw new Error(json.msg || "預覽失敗");
    const preview = json.preview || {};
    if (!panel) return;
    panel.innerHTML = renderDrivePreviewMetadata(preview, fileId);
    if (preview.render_mode === "text") {
      panel.innerHTML += `${driveRenderTextPreview(preview)}${preview.truncated ? '<div class="drive-card-sub">內容過長，已截斷顯示。</div>' : ""}`;
      return;
    }
    if (preview.render_mode === "archive") {
      const entries = Array.isArray(preview.entries) ? preview.entries : [];
      panel.innerHTML += `<div class="drive-preview-archive">${renderDriveArchiveEntries(entries)}</div>${preview.truncated ? '<div class="drive-card-sub">項目過多，已截斷顯示。</div>' : ""}`;
      return;
    }
    if (preview.render_mode === "media") {
      const serviceMode = selectedDrivePreviewServiceMode(fileId, preview);
      if (["audio", "video"].includes(preview.category) && !serviceMode) {
        panel.innerHTML += drivePreviewNoPlayableModeMarkup(preview);
        return;
      }
      if (["audio", "video"].includes(preview.category) && serviceMode === "prepared_hls" && drivePreviewHasReadyHls(preview)) {
        panel.innerHTML += driveHlsPlayerMarkup(preview, { fileId });
        await attachDriveHlsPreview(fileId, preview);
        return;
      }
      if (["audio", "video"].includes(preview.category) && serviceMode === "realtime_proxy") {
        panel.innerHTML += driveRealtimeProxyPlayerMarkup(fileId, preview);
        attachDrivePlainMediaPreview(fileId, preview);
        return;
      }
      const url = await resolveDrivePreviewMediaUrl(fileId, csrf, preview);
      if (preview.category === "audio") {
        panel.innerHTML += driveDirectPlayerMarkup(fileId, preview, url);
        attachDrivePlainMediaPreview(fileId, preview);
      }
      else if (preview.category === "video") {
        panel.innerHTML += driveDirectPlayerMarkup(fileId, preview, url);
        attachDrivePlainMediaPreview(fileId, preview);
      }
      else if (preview.category === "image") panel.innerHTML += `<img src="${url}" alt="${sanitize(preview.filename || "image preview")}" />`;
      else if (preview.category === "pdf") panel.innerHTML += renderDrivePdfPreview(url, preview.filename || "PDF preview");
      else panel.innerHTML += `<div class="drive-empty">此檔案無可用預覽。</div>`;
      return;
    }
    panel.innerHTML += `<div class="drive-empty">此檔案類型目前只提供 metadata，不支援 inline 預覽。</div>`;
  } catch (err) {
    clearDrivePreviewUrl();
    if (panel) panel.innerHTML = `<div class="drive-empty">${sanitize(err.message || "預覽失敗")}</div>`;
  }
}

async function previewDriveE2eeFile(fileId) {
  const panel = $("drive-preview-panel");
  showDrivePreviewCard();
  if (panel) panel.innerHTML = `<div class="drive-empty">正在使用最近輸入過的 E2EE 密碼嘗試預覽...</div>`;
  try {
    await fetchCsrfToken();
    const csrf = getCsrfToken();
    const decrypted = await buildDriveE2eePreview(fileId, csrf);
    if (!decrypted || !panel) return;
    const { preview, blob } = decrypted;
    panel.innerHTML = renderDrivePreviewMetadata(preview, fileId);
    if (preview.render_mode === "text") {
      panel.innerHTML += `${driveRenderTextPreview(preview)}${preview.truncated ? '<div class="drive-card-sub">內容過長，已截斷顯示。</div>' : ""}`;
      return;
    }
    if (preview.render_mode === "media") {
      panel.innerHTML += renderDriveDecryptedPreviewMedia(preview, blob);
      return;
    }
    panel.innerHTML += `<div class="drive-empty">此 E2EE 檔案已在瀏覽器解密，但目前只提供 metadata 預覽。</div>`;
  } catch (err) {
    clearDrivePreviewUrl();
    if (panel) panel.innerHTML = `<div class="drive-empty">${sanitize(err.message || "E2EE 預覽失敗")}</div>`;
  }
}

async function previewAlbumFileFullscreen(fileId, fileName = "", options = {}) {
  if (Array.isArray(options.files)) {
    setAlbumPreviewSequence(options.files, fileId);
  } else if (!albumPreviewSequence.some((file) => String(file.file_id) === String(fileId || ""))) {
    setAlbumPreviewSequence([], fileId);
  } else {
    albumPreviewIndex = albumPreviewSequence.findIndex((file) => String(file.file_id) === String(fileId || ""));
  }
  const overlay = $("album-full-preview-overlay");
  const title = $("album-full-preview-title");
  const meta = $("album-full-preview-meta");
  const body = $("album-full-preview-body");
  if (!overlay || !body) return previewDriveFile(fileId, { skipRepeatCheck: true, forceInlinePreview: true, fileName });
  clearAlbumFullPreviewUrl();
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  if (title) title.textContent = fileName || albumPreviewFileName(albumPreviewSequence[albumPreviewIndex]) || "檔案預覽";
  if (meta) meta.textContent = "讀取檔案中...";
  updateAlbumPreviewControls();
  body.innerHTML = `<div class="drive-empty">讀取檔案中...</div>`;
  try {
    await fetchCsrfToken();
    const csrf = getCsrfToken();
    const knownFile = findKnownDriveFile(fileId);
    if (driveFileIsE2ee(knownFile)) {
      const decrypted = await buildDriveE2eePreview(fileId, csrf);
      if (!decrypted) return;
      const { preview, blob } = decrypted;
      if (title) title.textContent = preview.filename || fileName || "E2EE 預覽";
      const countLabel = albumPreviewCurrentCountLabel();
      const baseMeta = `${countLabel ? `${countLabel} · ` : ""}${formatDriveBytes(preview.size_bytes || 0)} · ${preview.mime_type || blob.type || "-"} · E2EE 瀏覽器解密`;
      if (meta) meta.textContent = baseMeta;
      if (preview.render_mode === "text") {
        body.innerHTML = `${driveRenderTextPreview(preview)}${preview.truncated ? '<div class="drive-card-sub">內容過長，已截斷顯示。</div>' : ""}`;
        return;
      }
      if (preview.render_mode === "media") {
        body.innerHTML = renderDriveDecryptedPreviewMedia(preview, blob, { fullscreen: true });
        return;
      }
      body.innerHTML = `<div class="drive-empty">此 E2EE 檔案已在瀏覽器解密，但目前只提供 metadata 預覽。</div>`;
      return;
    }
    const res = await apiFetch(API + `/cloud-drive/files/${encodeURIComponent(fileId)}/preview`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(json.msg || "預覽失敗");
    const preview = json.preview || {};
    if (title) title.textContent = preview.filename || fileName || albumPreviewFileName(albumPreviewSequence[albumPreviewIndex]) || "檔案預覽";
    const countLabel = albumPreviewCurrentCountLabel();
    const baseMeta = `${countLabel ? `${countLabel} · ` : ""}${formatDriveBytes(preview.size_bytes || 0)} · ${preview.mime_type || "-"} · scan=${preview.scan_status || "-"}`;
    if (preview.render_mode === "text") {
      if (meta) meta.textContent = baseMeta;
      body.innerHTML = `${driveRenderTextPreview(preview)}${preview.truncated ? '<div class="drive-card-sub">內容過長，已截斷顯示。</div>' : ""}`;
      return;
    }
    if (preview.render_mode === "archive") {
      const entries = Array.isArray(preview.entries) ? preview.entries : [];
      if (meta) meta.textContent = baseMeta;
      body.innerHTML = `<div class="drive-preview-archive">${renderDriveArchiveEntries(entries)}</div>${preview.truncated ? '<div class="drive-card-sub">項目過多，已截斷顯示。</div>' : ""}`;
      return;
    }
    if (preview.render_mode !== "media") {
      throw new Error("這個檔案類型目前只提供右側 metadata 預覽");
    }
    const serviceMode = selectedDrivePreviewServiceMode(fileId, preview);
    if (["audio", "video"].includes(preview.category) && !serviceMode) {
      body.innerHTML = drivePreviewNoPlayableModeMarkup(preview);
      return;
    }
    if (["audio", "video"].includes(preview.category) && serviceMode === "prepared_hls" && drivePreviewHasReadyHls(preview)) {
      if (meta) meta.textContent = `${formatDriveBytes(preview.size_bytes || 0)} · ${preview.mime_type || "-"} · HLS 串流已就緒 · 字幕 ${drivePreviewSubtitles(preview).length} 軌`;
      body.innerHTML = driveHlsPlayerMarkup(preview, { fullscreen: true, fileId });
      await attachDriveHlsPreview(fileId, preview, { fullscreen: true });
      return;
    }
    if (["audio", "video"].includes(preview.category) && serviceMode === "realtime_proxy") {
      if (meta) meta.textContent = `${formatDriveBytes(preview.size_bytes || 0)} · ${preview.mime_type || "-"} · Standard 即時轉封裝`;
      body.innerHTML = driveRealtimeProxyPlayerMarkup(fileId, preview, { fullscreen: true });
      attachDrivePlainMediaPreview(fileId, preview, { fullscreen: true });
      return;
    }
    const url = await resolveDrivePreviewMediaUrl(fileId, csrf, preview, { fullscreen: true });
    currentAlbumFullPreviewUrl = drivePreviewUsesDirectStream(preview) ? "" : url;
    if (meta) meta.textContent = `${formatDriveBytes(preview.size_bytes || 0)} · ${preview.mime_type || "-"} · scan=${preview.scan_status || "-"}`;
    if (preview.category === "image") {
      body.innerHTML = `<img src="${url}" alt="${sanitize(preview.filename || fileName || "image preview")}" />`;
    } else if (preview.category === "video") {
      body.innerHTML = driveDirectPlayerMarkup(fileId, preview, url, { fullscreen: true });
      attachDrivePlainMediaPreview(fileId, preview, { fullscreen: true });
    } else if (preview.category === "audio") {
      body.innerHTML = driveDirectPlayerMarkup(fileId, preview, url, { fullscreen: true });
      attachDrivePlainMediaPreview(fileId, preview, { fullscreen: true });
    } else if (preview.category === "pdf") {
      body.innerHTML = renderDrivePdfPreview(url, preview.filename || fileName || "PDF preview");
    } else {
      throw new Error("這個檔案類型目前只支援右側預覽");
    }
  } catch (err) {
    clearAlbumFullPreviewUrl();
    updateAlbumPreviewControls();
    if (meta) meta.textContent = "";
    body.innerHTML = `<div class="drive-empty">${sanitize(err.message || "預覽失敗")}</div>`;
  }
}

function stepAlbumPreview(direction) {
  if (!albumPreviewSequence.length || albumPreviewIndex < 0) return;
  const nextIndex = (albumPreviewIndex + direction + albumPreviewSequence.length) % albumPreviewSequence.length;
  const nextFile = albumPreviewSequence[nextIndex];
  if (!nextFile?.file_id) return;
  albumPreviewIndex = nextIndex;
  previewAlbumFileFullscreen(nextFile.file_id, albumPreviewFileName(nextFile)).catch((err) => alert(err.message || "預覽失敗"));
}

async function editDriveTextFile(fileId) {
  const panel = $("drive-preview-panel");
  showDrivePreviewCard();
  if (panel) panel.innerHTML = `<div class="drive-empty">讀取文字內容中...</div>`;
  try {
    await fetchCsrfToken();
    const csrf = getCsrfToken();
    const res = await apiFetch(API + `/cloud-drive/files/${encodeURIComponent(fileId)}/preview`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(json.msg || "讀取失敗");
    const preview = json.preview || {};
    if (preview.render_mode !== "text") throw new Error("目前只支援文字類檔案線上修改");
    if (panel) {
      panel.innerHTML = `
        <div><strong>${sanitize(preview.filename || "text")}</strong></div>
        <textarea id="drive-text-editor" rows="14">${sanitize(preview.text || "")}</textarea>
        <div class="drive-file-actions" style="justify-content:flex-start;">
          <button class="btn btn-primary" type="button" data-drive-action="save-text" data-file-id="${sanitize(fileId)}">儲存修改</button>
          <button class="btn" type="button" data-drive-action="preview" data-file-id="${sanitize(fileId)}">取消</button>
        </div>
      `;
    }
  } catch (err) {
    if (panel) panel.innerHTML = `<div class="drive-empty">${sanitize(err.message || "讀取失敗")}</div>`;
  }
}

async function saveDriveTextFile(fileId) {
  const editor = $("drive-text-editor");
  if (!editor) return;
  try {
    await storageAction(`/cloud-drive/files/${encodeURIComponent(fileId)}/text`, "PUT", { content: editor.value });
    await loadDriveDashboard();
    await previewDriveFile(fileId, { skipRepeatCheck: true });
  } catch (err) {
    alert(err.message || "儲存失敗");
  }
}

function openDriveTextDocumentModal() {
  const overlay = $("drive-new-doc-overlay");
  if (!overlay) return;
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  const msg = $("drive-new-doc-msg");
  if (msg) msg.textContent = "";
  setTimeout(() => $("drive-new-doc-name")?.focus?.(), 0);
}

function closeDriveTextDocumentModal({ clear = false } = {}) {
  const overlay = $("drive-new-doc-overlay");
  if (!overlay) return;
  overlay.classList.remove("show");
  overlay.setAttribute("aria-hidden", "true");
  const anyOverlayOpen = document.querySelector(".user-edit-overlay.show, .album-full-preview-overlay.show");
  if (!anyOverlayOpen) document.body.classList.remove("modal-open");
  const msg = $("drive-new-doc-msg");
  if (msg) msg.textContent = "";
  if (clear) {
    if ($("drive-new-doc-name")) $("drive-new-doc-name").value = "";
    if ($("drive-new-doc-content")) $("drive-new-doc-content").value = "";
  }
}

async function createDriveTextDocument() {
  const filename = ($("drive-new-doc-name")?.value || "").trim() || "untitled.txt";
  const content = $("drive-new-doc-content")?.value || "";
  const privacyMode = $("drive-new-doc-privacy-mode")?.value || "standard_plain";
  const msg = $("drive-new-doc-msg");
  try {
    const json = await storageAction("/cloud-drive/files/text", "POST", {
      filename,
      content,
      privacy_mode: privacyMode,
      virtual_path: joinStoragePath(currentStoragePath, filename),
    });
    if ($("drive-new-doc-name")) $("drive-new-doc-name").value = "";
    if ($("drive-new-doc-content")) $("drive-new-doc-content").value = "";
    if (msg) flash(msg, "文檔已建立", true);
    await loadDriveDashboard();
    closeDriveTextDocumentModal({ clear: true });
    const fileId = json.file?.file_id || json.file?.id;
    if (fileId) await previewDriveFile(fileId, { skipRepeatCheck: true });
  } catch (err) {
    if (msg) flash(msg, err.message || "建立文檔失敗", false);
    else alert(err.message || "建立文檔失敗");
  }
}

function renderContextAttachmentRefs(targetId, refs) {
  const list = $(targetId);
  if (!list) return;
  if (!Array.isArray(refs) || !refs.length) {
    list.innerHTML = `<div class="drive-empty">尚無附件</div>`;
    return;
  }
  list.innerHTML = refs.map((ref) => {
    const name = ref.original_filename_plain_for_public || ref.file_id || "download.bin";
    const warn = driveFileNeedsWarning(ref);
    const canRemove = typeof canRemoveContextAttachment === "function"
      ? canRemoveContextAttachment(ref)
      : !!(ref && (ref.can_remove === true || ref.can_remove === 1 || ref.can_remove === "1" || ref.can_remove === "true"));
    const removeButton = canRemove
      ? `<button class="btn btn-danger" type="button" data-drive-action="delete-context-attachment" data-ref-id="${sanitize(ref.id)}" data-context-type="${sanitize(ref.context_type || "")}" data-context-id="${sanitize(ref.context_id || "")}" data-target-id="${sanitize(targetId)}">移除附件</button>`
      : "";
    const imagePreview = driveFileIsImage(ref)
      ? `<button class="chat-message-image-preview" type="button" data-drive-action="album-full-preview" data-file-id="${sanitize(ref.file_id)}" data-name="${sanitize(name)}"><img src="${sanitize(drivePreviewContentUrl(ref.file_id))}" alt="${sanitize(name)}" loading="lazy" /></button>`
      : "";
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(name)}</strong>
          <div class="drive-card-sub">${formatDriveBytes(ref.size_bytes || 0)} · ${sanitize(ref.context_type || "-")}#${sanitize(ref.context_id || "-")} · risk=${sanitize(ref.risk_level || "-")} · scan=${sanitize(ref.scan_status || "-")}</div>
          ${imagePreview}
        </div>
        <div class="drive-file-actions">
          <button class="btn" type="button" data-drive-action="preview" data-file-id="${sanitize(ref.file_id)}">預覽</button>
          <button class="btn ${warn ? "btn-danger" : "btn-primary"}" type="button" data-drive-action="download" data-file-id="${sanitize(ref.file_id)}" data-warn="${warn ? "1" : "0"}">下載</button>
          ${removeButton}
        </div>
      </div>
    `;
  }).join("");
}

async function deleteContextAttachment(refId, contextType, contextId, targetId) {
  if (!refId) {
    alert("附件編號讀取失敗，請重新整理後再試。");
    return;
  }
  if (!window.confirm("將此附件從目前項目移除？原雲端檔案不會被刪除。")) return;
  await storageAction(`/cloud-drive/refs/${encodeURIComponent(refId)}/delete`, "POST");
  if (contextType === "chat_message" && typeof loadChatMessages === "function" && selectedChatRoomId) {
    await loadChatMessages(selectedChatRoomId, false);
  } else if (contextType && contextId && targetId) {
    await loadContextAttachments(contextType, contextId, targetId);
  }
  await ensureAttachmentFileOptionsLoaded({ force: true });
}

async function loadContextAttachments(contextType, contextId, targetId) {
  if (!contextType || !contextId || !targetId) return;
  const csrf = await fetchCsrfToken();
  const res = await apiFetch(API + `/cloud-drive/refs?context_type=${encodeURIComponent(contextType)}&context_id=${encodeURIComponent(contextId)}`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    const list = $(targetId);
    if (list) list.innerHTML = `<div class="drive-empty">${sanitize(json.msg || "附件讀取失敗")}</div>`;
    return null;
  }
  renderContextAttachmentRefs(targetId, json.refs || []);
  return json.refs || [];
}

async function uploadContextAttachment({ fileInputId, contextType, contextId, grantUserIds = [], grantRole = null, refresh }) {
  const input = $(fileInputId);
  const selectedFile = input?.files?.[0];
  if (!selectedFile) {
    alert("請先選擇附件檔案");
    return;
  }
  if (!contextId) {
    alert("請先選擇對話、聊天室或公告");
    return;
  }
  if (!(await preflightDriveUploadSize(selectedFile.size, `附件「${selectedFile.name || "attachment.bin"}」`))) {
    input.value = "";
    return;
  }
  await fetchCsrfToken();
  const csrf = getCsrfToken();
  const form = new FormData();
  form.append("file", selectedFile);
  form.append("privacy_mode", "standard_plain");
  form.append("virtual_path", attachmentStoragePath(selectedFile, contextType || "attachment"));
  form.append("display_name", selectedFile.name || "attachment.bin");
  form.append("context_type", contextType);
  form.append("context_id", String(contextId));
  grantUserIds.forEach((id) => form.append("grant_user_ids", String(id)));
  if (grantRole) form.append("grant_role", grantRole);
  const res = await apiFetch(API + "/cloud-drive/upload", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" },
    body: form
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    alert(json.error_code ? `${json.msg || "附件上傳失敗"}（${json.error_code}）` : (json.msg || `附件上傳失敗（HTTP ${res.status}）`));
    return;
  }
  input.value = "";
  if (typeof refresh === "function") await refresh();
}

async function attachExistingContextAttachment({ fileId, contextType, contextId, grantUserIds = [], grantRole = null, refresh }) {
  if (!fileId) {
    alert("請先從下拉選單選擇雲端檔案");
    return;
  }
  if (!contextId) {
    alert("請先選擇對話、聊天室或公告");
    return;
  }
  await storageAction("/cloud-drive/attach-existing", "POST", {
    file_id: fileId,
    context_type: contextType,
    context_id: String(contextId),
    grant_user_ids: grantUserIds,
    grant_role: grantRole,
    can_preview: true
  });
  if (typeof refresh === "function") await refresh();
}

async function uploadPendingChatAttachment() {
  const input = $("chat-attachment-file");
  const selectedFile = input?.files?.[0];
  if (!selectedFile) {
    alert("請先選擇附件檔案");
    return;
  }
  if (!selectedChatRoomId) {
    alert("請先選擇聊天室");
    return;
  }
  if (!(await preflightDriveUploadSize(selectedFile.size, `附件「${selectedFile.name || "attachment.bin"}」`))) {
    input.value = "";
    return;
  }
  const fingerprint = [
    selectedFile.name || "",
    selectedFile.size || 0,
    selectedFile.lastModified || 0,
    selectedChatRoomId || "",
  ].join(":");
  if (chatAttachmentUploadInFlight && chatAttachmentUploadFingerprint === fingerprint) {
    setChatMsg("chat-room-warn", "附件正在加入待送清單，請稍候", true);
    return;
  }
  chatAttachmentUploadInFlight = true;
  chatAttachmentUploadFingerprint = fingerprint;
  if (input) input.disabled = true;
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const form = new FormData();
    form.append("file", selectedFile);
    form.append("privacy_mode", "standard_plain");
    form.append("virtual_path", attachmentStoragePath(selectedFile, "chat"));
    form.append("display_name", selectedFile.name || "attachment.bin");
    const res = await apiFetch(API + "/cloud-drive/upload", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
      body: form
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      alert(json.error_code ? `${json.msg || "附件上傳失敗"}（${json.error_code}）` : (json.msg || `附件上傳失敗（HTTP ${res.status}）`));
      return;
    }
    input.value = "";
    await ensureAttachmentFileOptionsLoaded({ force: true });
    if (typeof addPendingChatAttachment === "function") {
      addPendingChatAttachment(json.file || {});
    }
    setChatMsg("chat-room-warn", "附件已加入待送清單，按送出後會出現在該則訊息下方", true);
  } finally {
    chatAttachmentUploadInFlight = false;
    chatAttachmentUploadFingerprint = "";
    if (input) input.disabled = false;
  }
}

function openChatAttachmentPicker() {
  if (!selectedChatRoomId) {
    alert("請先選擇聊天室");
    return;
  }
  $("chat-attachment-file")?.click?.();
}

async function addExistingChatFileToPending(fileId) {
  if (!fileId) {
    await ensureAttachmentFileOptionsLoaded();
    alert("請先從下拉選單選擇雲端檔案");
    return;
  }
  if (!selectedChatRoomId) {
    alert("請先選擇聊天室");
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/files/${encodeURIComponent(fileId)}/status`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    alert(json.msg || `檔案讀取失敗（HTTP ${res.status}）`);
    return;
  }
  if (typeof addPendingChatAttachment === "function") {
    addPendingChatAttachment(json.file || { file_id: fileId });
  }
  const input = $("chat-attachment-existing-file-id");
  if (input) input.value = "";
  setChatMsg("chat-room-warn", "既有雲端檔已加入待送清單，按送出後會出現在該則訊息下方", true);
}

async function uploadChatAttachment() {
  await uploadPendingChatAttachment();
}

async function attachExistingChatFile() {
  await ensureAttachmentFileOptionsLoaded();
  const selectedFileId = $("chat-attachment-existing-file-id")?.value.trim() || "";
  if (!selectedFileId) return;
  await addExistingChatFileToPending(selectedFileId);
}

function currentDmGrantUserIds() {
  const thread = typeof currentDmThread === "function" ? currentDmThread() : null;
  return thread?.other_user_id ? [thread.other_user_id] : [];
}

async function uploadDmAttachment() {
  await uploadContextAttachment({
    fileInputId: "dm-attachment-file",
    contextType: "dm",
    contextId: selectedDmThreadId,
    grantUserIds: currentDmGrantUserIds(),
    refresh: () => loadContextAttachments("dm", selectedDmThreadId, "dm-attachment-list")
  });
  await ensureAttachmentFileOptionsLoaded({ force: true });
}

async function attachExistingDmFile() {
  await ensureAttachmentFileOptionsLoaded();
  await attachExistingContextAttachment({
    fileId: $("dm-attachment-existing-file-id")?.value.trim() || "",
    contextType: "dm",
    contextId: selectedDmThreadId,
    grantUserIds: currentDmGrantUserIds(),
    refresh: () => loadContextAttachments("dm", selectedDmThreadId, "dm-attachment-list")
  });
}

async function createAnnouncementAttachmentRequest(fileId, announcementId, reason) {
  await storageAction("/cloud-drive/announcement-attachment-requests", "POST", {
    file_id: fileId,
    announcement_id: announcementId,
    reason: reason || "announcement attachment"
  });
  alert("公告附件請求已送出，等待 root 核准");
}

async function uploadAnnouncementAttachmentRequest() {
  const announcementId = Number($("announcement-attachment-announcement-id")?.value || 0);
  const input = $("announcement-attachment-file");
  const selectedFile = input?.files?.[0];
  if (!announcementId) {
    alert("請輸入公告 ID");
    return;
  }
  if (!selectedFile) {
    alert("請先選擇公告附件");
    return;
  }
  if (!(await preflightDriveUploadSize(selectedFile.size, `公告附件「${selectedFile.name || "attachment.bin"}」`))) {
    input.value = "";
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const form = new FormData();
  form.append("file", selectedFile);
  form.append("privacy_mode", "standard_plain");
  form.append("virtual_path", attachmentStoragePath(selectedFile, "announcement"));
  form.append("display_name", selectedFile.name || "attachment.bin");
  const res = await apiFetch(API + "/cloud-drive/upload", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" },
    body: form
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    alert(json.error_code ? `${json.msg || "公告附件上傳失敗"}（${json.error_code}）` : (json.msg || `公告附件上傳失敗（HTTP ${res.status}）`));
    return;
  }
  input.value = "";
  await createAnnouncementAttachmentRequest(json.file?.file_id, announcementId, $("announcement-attachment-reason")?.value || "");
  await ensureAttachmentFileOptionsLoaded({ force: true });
}

async function attachExistingAnnouncementFile() {
  const announcementId = Number($("announcement-attachment-announcement-id")?.value || 0);
  await ensureAttachmentFileOptionsLoaded();
  const fileId = $("announcement-attachment-existing-file-id")?.value.trim() || "";
  if (!announcementId) {
    alert("請輸入公告 ID");
    return;
  }
  if (!fileId) {
    alert("請先從下拉選單選擇雲端檔案");
    return;
  }
  await createAnnouncementAttachmentRequest(fileId, announcementId, $("announcement-attachment-reason")?.value || "");
}

function normalizeStoragePath(path, fallbackName = "") {
  const raw = String(path || fallbackName || "").replace(/\\/g, "/").trim();
  const parts = raw.split("/").map((part) => part.trim()).filter(Boolean);
  return parts.length ? `/${parts.join("/")}` : "/";
}

function storageBaseName(path) {
  const normalized = normalizeStoragePath(path);
  if (normalized === "/") return "我的雲端硬碟";
  return normalized.split("/").filter(Boolean).pop() || normalized;
}

function storageDirName(path) {
  const parts = normalizeStoragePath(path).split("/").filter(Boolean);
  parts.pop();
  return parts.length ? `/${parts.join("/")}` : "/";
}

function joinStoragePath(folder, name) {
  const base = normalizeStoragePath(folder);
  const cleanName = String(name || "").replace(/\\/g, "/").split("/").filter(Boolean).join("/");
  if (!cleanName) return base;
  return base === "/" ? `/${cleanName}` : `${base}/${cleanName}`;
}

function attachmentStoragePath(file, prefix = "attachment") {
  const originalName = String(file?.name || "attachment.bin").replace(/\\/g, "/").split("/").filter(Boolean).pop() || "attachment.bin";
  const normalizedPrefix = String(prefix || "attachment").replace(/[^a-z0-9_-]+/gi, "-").replace(/^-+|-+$/g, "").toLowerCase() || "attachment";
  const uniqueName = `${normalizedPrefix}-${Date.now()}-${originalName}`;
  return joinStoragePath("/attachments", uniqueName);
}

function storageUploadRelativePath(file) {
  const relative = String(file?.webkitRelativePath || file?.relativePath || file?.name || "").replace(/\\/g, "/");
  return relative.split("/").filter(Boolean).join("/");
}

function storageDepth(path) {
  return normalizeStoragePath(path).split("/").filter(Boolean).length;
}

function renderStorageBreadcrumb() {
  const target = $("storage-breadcrumb");
  if (!target) return;
  const parts = currentStoragePath.split("/").filter(Boolean);
  const crumbs = [`<button type="button" data-drive-action="open-storage-folder" data-path="/">我的雲端硬碟</button>`];
  let walk = "";
  parts.forEach((part) => {
    walk += `/${part}`;
    crumbs.push(`<span>/</span><button type="button" data-drive-action="open-storage-folder" data-path="${sanitize(walk)}">${sanitize(part)}</button>`);
  });
  target.innerHTML = crumbs.join("");
}

function setStorageSelection(id = "", path = "") {
  selectedStorageFileId = id || "";
  selectedStorageFilePath = path || "";
  selectedStorageFileIds = new Set(selectedStorageFileId ? [selectedStorageFileId] : []);
  selectedStorageFolderPaths = new Set();
  updateStorageSelectionLabel();
}

function storageSelectionCount() {
  return selectedStorageFileIds.size + selectedStorageFolderPaths.size;
}

function selectedStorageFiles() {
  const byId = new Map((Array.isArray(storageFilesCache) ? storageFilesCache : [])
    .map((file) => [String(file?.id || ""), file])
    .filter(([id]) => Boolean(id)));
  return Array.from(selectedStorageFileIds)
    .map((id) => byId.get(String(id)) || { id, display_name: id, virtual_path: "" })
    .filter((file) => file?.id);
}

function selectedStorageFolders() {
  const byPath = new Map((Array.isArray(storageFoldersCache) ? storageFoldersCache : [])
    .map((folder) => [normalizeStoragePath(folder?.virtual_path || ""), folder])
    .filter(([path]) => path && path !== "/"));
  return Array.from(selectedStorageFolderPaths)
    .map((path) => {
      const normalized = normalizeStoragePath(path);
      return byPath.get(normalized) || { virtual_path: normalized, display_name: storageBaseName(normalized) };
    })
    .filter((folder) => normalizeStoragePath(folder?.virtual_path || "") !== "/");
}

function storageFileDisplayName(file) {
  return file?.display_name || storageBaseName(file?.virtual_path || "") || file?.id || "file";
}

function storageFilePath(file) {
  return normalizeStoragePath(file?.virtual_path || storageFileDisplayName(file));
}

function storageFileIsInsideFolder(file, folderPath) {
  const filePath = storageFilePath(file);
  const normalizedFolder = normalizeStoragePath(folderPath);
  return Boolean(normalizedFolder && normalizedFolder !== "/" && filePath.startsWith(`${normalizedFolder}/`));
}

function storageFilesInSelectedFolders(folders = selectedStorageFolders()) {
  const folderPaths = folders.map((folder) => normalizeStoragePath(folder?.virtual_path || "")).filter((path) => path && path !== "/");
  if (!folderPaths.length) return [];
  return (Array.isArray(storageFilesCache) ? storageFilesCache : [])
    .filter((file) => folderPaths.some((folderPath) => storageFileIsInsideFolder(file, folderPath)));
}

function selectedStorageFilesForShareOrDownload() {
  const byId = new Map();
  selectedStorageFiles().forEach((file) => byId.set(String(file.id || ""), file));
  storageFilesInSelectedFolders().forEach((file) => byId.set(String(file.id || ""), file));
  return Array.from(byId.values()).filter((file) => file?.id);
}

function updateStorageSelectionLabel() {
  const label = $("storage-selection-label");
  if (!label) return;
  const count = storageSelectionCount();
  if (count > 1) {
    label.textContent = `已選取 ${count} 個項目`;
  } else if (selectedStorageFileId) {
    label.textContent = `已選取：${selectedStorageFilePath || selectedStorageFileId}`;
  } else if (selectedStorageFolderPaths.size === 1) {
    label.textContent = `已選取資料夾：${Array.from(selectedStorageFolderPaths)[0]}`;
  } else {
    label.textContent = "未選取檔案";
  }
}

function visibleStorageFiles() {
  return (Array.isArray(storageFilesCache) ? storageFilesCache : [])
    .filter((file) => storageDirName(file.virtual_path || file.display_name || "") === currentStoragePath);
}

function visibleStorageFolders() {
  return (Array.isArray(storageFoldersCache) ? storageFoldersCache : [])
    .filter((folder) => {
      const path = normalizeStoragePath(folder.virtual_path || folder.display_name || "");
      return path !== "/" && storageDirName(path) === currentStoragePath;
    });
}

function renderStorageBulkToolbar() {
  const count = storageSelectionCount();
  const visibleCount = visibleStorageFiles().length + visibleStorageFolders().length;
  const disabled = count ? "" : " disabled";
  return `
    <div class="drive-file-row storage-browser-row storage-browser-bulk-toolbar">
      <div>
        <strong>批次選取</strong>
        <div class="drive-card-sub">本層 ${visibleCount} 個項目；已選取 ${count} 個。</div>
      </div>
      <div class="drive-file-actions">
        <button class="btn btn-sm" type="button" data-drive-action="select-visible-storage">全選本層</button>
        <button class="btn btn-sm" type="button" data-drive-action="clear-storage-selection">清除選取</button>
        <button class="btn btn-sm" type="button" data-drive-action="move-selected-storage"${disabled}>移動</button>
        <button class="btn btn-sm" type="button" data-drive-action="share-selected-storage"${disabled}>分享</button>
        <button class="btn btn-sm" type="button" data-drive-action="download-selected-storage"${disabled}>下載</button>
        <button class="btn btn-danger btn-sm" type="button" data-drive-action="delete-selected-storage"${disabled}>刪除</button>
      </div>
    </div>
  `;
}

function renderStorageBrowser() {
  renderStorageBreadcrumb();
  const list = $("storage-browser-list");
  if (!list) return;
  const rows = [];
  rows.push(...driveTransferRows.map(renderDriveTransferRow));
  if (currentStoragePath !== "/") {
    rows.push(storageParentRow());
  }
  rows.push(renderStorageBulkToolbar());
  rows.push(...storageFolderRows(storageFoldersCache));
  rows.push(...storageFileRows(storageFilesCache));
  list.innerHTML = rows.length ? rows.join("") : `<div class="drive-empty">這個資料夾沒有檔案或資料夾</div>`;
}

function storageParentRow() {
  return `
    <div class="drive-file-row storage-browser-row storage-browser-folder" data-folder-path="${sanitize(storageDirName(currentStoragePath))}">
      <div>
        <strong>上一層</strong>
        <div class="drive-card-sub">資料夾 · ${sanitize(storageDirName(currentStoragePath))}</div>
      </div>
      <div class="drive-file-actions">
        <button class="btn" type="button" data-drive-action="open-storage-folder" data-path="${sanitize(storageDirName(currentStoragePath))}">開啟</button>
      </div>
    </div>
  `;
}

function storageFolderRows(folders) {
  return visibleStorageFolders()
    .map((folder) => {
      const name = storageBaseName(folder.virtual_path || folder.display_name || "folder");
      const folderPath = normalizeStoragePath(folder.virtual_path || "");
      const checked = selectedStorageFolderPaths.has(folderPath) ? " checked" : "";
      return `
        <div class="drive-file-row storage-browser-row storage-browser-folder" data-folder-path="${sanitize(folder.virtual_path || "")}">
          <div>
            <label class="drive-inline-check"><input type="checkbox" data-storage-folder-select="${sanitize(folderPath)}"${checked} /> <strong>${sanitize(name)}</strong></label>
            <div class="drive-card-sub">資料夾 · ${folder.is_explicit ? "已建立" : "由檔案路徑產生"} · 直接 ${Number(folder.file_count || 0)} 個 · 含子資料夾 ${Number(folder.recursive_file_count || 0)} 個</div>
          </div>
          <div class="drive-file-actions">
            <button class="btn btn-primary" type="button" data-drive-action="open-storage-folder" data-path="${sanitize(folder.virtual_path || "")}">開啟</button>
            <button class="btn" type="button" data-drive-action="rename-storage-folder" data-path="${sanitize(folder.virtual_path || "")}" data-name="${sanitize(name)}">重新命名</button>
            <button class="btn" type="button" data-drive-action="share-storage-folder" data-path="${sanitize(folder.virtual_path || "")}" data-name="${sanitize(name)}">分享</button>
            <button class="btn" type="button" data-drive-action="folder-to-album" data-path="${sanitize(folder.virtual_path || "")}" data-name="${sanitize(name)}">設為相簿</button>
            <button class="btn" type="button" data-drive-action="select-storage-folder" data-path="${sanitize(folder.virtual_path || "")}">移動</button>
            <button class="btn btn-danger" type="button" data-drive-action="trash-storage-folder" data-path="${sanitize(folder.virtual_path || "")}">刪除</button>
          </div>
        </div>
      `;
    });
}

function storageFileRows(files) {
  return visibleStorageFiles()
    .map((file) => {
      const primary = drivePrimaryAction(file);
      const e2ee = driveFileIsE2ee(file);
      const rowAction = ` data-drive-action="preview"`;
      const checked = selectedStorageFileIds.has(String(file.id || "")) ? " checked" : "";
      const albumButton = driveFileIsImage(file)
        ? `<button class="btn" type="button" data-drive-action="add-storage-to-album" data-storage-file-id="${sanitize(file.id)}" data-name="${sanitize(file.display_name || storageBaseName(file.virtual_path) || file.id)}">加入相簿</button>`
        : "";
      const videoButton = driveFileIsMedia(file) && file.file_id
        ? `<button class="btn" type="button" data-drive-action="publish-to-video" data-file-id="${sanitize(file.file_id)}" data-name="${sanitize(file.display_name || storageBaseName(file.virtual_path) || file.id)}">分享到影音</button>`
        : "";
      return `
    <div class="drive-file-row storage-browser-row storage-browser-file"${rowAction} data-file-id="${sanitize(file.file_id)}" data-name="${sanitize(file.display_name || storageBaseName(file.virtual_path) || file.id)}">
      <div>
        <label class="drive-inline-check"><input type="checkbox" data-storage-file-select="${sanitize(file.id)}"${checked} /> <strong>${sanitize(file.display_name || storageBaseName(file.virtual_path) || file.id)}</strong></label>
        <div class="drive-card-sub">檔案 · ${formatDriveBytes(file.size_bytes || 0)} · ${sanitize(driveFileCategory(file))}${e2ee ? " · 需密碼預覽" : ""} · scan=${sanitize(file.scan_status || "-")} · ${sanitize(file.virtual_path || "-")}</div>
      </div>
      <div class="drive-file-actions">
        <button class="btn" type="button" data-drive-action="${sanitize(primary.action)}" data-file-id="${sanitize(file.file_id)}">${sanitize(primary.label)}</button>
        ${videoButton}
        <button class="btn" type="button" data-drive-action="share-cloud-file" data-file-id="${sanitize(file.file_id)}" data-storage-file-id="${sanitize(file.id)}" data-name="${sanitize(file.display_name || storageBaseName(file.virtual_path) || file.id)}">${e2ee ? "解密後分享" : "分享"}</button>
        <button class="btn" type="button" data-drive-action="rename-storage-file" data-storage-file-id="${sanitize(file.id)}" data-path="${sanitize(file.virtual_path || "")}" data-name="${sanitize(file.display_name || storageBaseName(file.virtual_path) || file.id)}">重新命名</button>
        <button class="btn" type="button" data-drive-action="move-storage-file" data-storage-file-id="${sanitize(file.id)}" data-path="${sanitize(file.virtual_path || "")}">移動</button>
        <button class="btn" type="button" data-drive-action="download-storage" data-storage-file-id="${sanitize(file.id)}">下載</button>
        ${albumButton}
        <button class="btn btn-danger" type="button" data-drive-action="trash-storage" data-storage-file-id="${sanitize(file.id)}">刪除</button>
      </div>
    </div>
  `;
    });
}

function updateAlbumTargetSelect(albums) {
  const select = $("album-picker-select");
  if (!select) return;
  const previous = select.value || selectedAlbumId || "";
  const liveAlbums = Array.isArray(albums) ? albums : [];
  select.innerHTML = `<option value="">選擇相簿</option>${liveAlbums.map((album) => `
    <option value="${sanitize(album.id)}">${sanitize(album.title || album.id)}（${albumVisibilityLabel(album.visibility)}）</option>
  `).join("")}`;
  const nextValue = previous && liveAlbums.some((album) => album.id === previous)
    ? previous
    : (liveAlbums[0]?.id || "");
  select.value = nextValue;
  renderAlbumPickerCards(liveAlbums, nextValue);
}

function renderAlbumPickerCards(albums, selectedId = "") {
  const grid = $("album-picker-card-grid");
  if (!grid) return;
  const rows = Array.isArray(albums) ? albums : [];
  if (!rows.length) {
    grid.innerHTML = `<div class="drive-empty">目前沒有可選擇的相簿</div>`;
    return;
  }
  grid.innerHTML = rows.map((album) => {
    const id = String(album.id || "");
    const selected = id === String(selectedId || "");
    const title = album.title || album.id || "未命名相簿";
    const count = Number(album.file_count || album.files?.length || 0);
    return `
      <button class="album-picker-card${selected ? " selected" : ""}" type="button" role="radio" aria-checked="${selected ? "true" : "false"}" data-album-picker-card="1" data-album-id="${sanitize(id)}">
        <span class="album-picker-card-title">${sanitize(title)}</span>
        <span class="album-picker-card-meta">${sanitize(albumVisibilityLabel(album.visibility))} · ${count} 個檔案</span>
      </button>
    `;
  }).join("");
}

function setAlbumPickerSelection(albumId) {
  const value = String(albumId || "");
  const select = $("album-picker-select");
  if (select) select.value = value;
  document.querySelectorAll("[data-album-picker-card]").forEach((card) => {
    const selected = String(card.dataset.albumId || "") === value;
    card.classList.toggle("selected", selected);
    card.setAttribute("aria-checked", selected ? "true" : "false");
  });
}

function storageFolderRowPathFromEventTarget(target) {
  const row = target?.closest?.(".storage-browser-folder[data-folder-path]");
  const path = row?.dataset?.folderPath || "";
  return path ? normalizeStoragePath(path) : "";
}

function renderStorageTrash(files) {
  const list = $("storage-trash-list");
  if (!list) return;
  if (!Array.isArray(files) || !files.length) {
    list.innerHTML = `<div class="drive-empty">沒有已刪除檔案</div>`;
    return;
  }
  list.innerHTML = files.map((file) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(file.display_name || file.virtual_path || file.id)}</strong>
        <div class="drive-card-sub">${sanitize(file.virtual_path || "-")} · ${formatDriveBytes(file.size_bytes || 0)}</div>
      </div>
      <div class="drive-file-actions">
        <button class="btn" type="button" data-drive-action="restore-storage" data-storage-file-id="${sanitize(file.id)}">還原</button>
        <button class="btn btn-danger" type="button" data-drive-action="purge-storage" data-storage-file-id="${sanitize(file.id)}">永久移除</button>
      </div>
    </div>
  `).join("");
}

function renderAlbums(albums) {
  const list = $("album-list");
  if (!list) return;
  storageAlbumsCache = Array.isArray(albums) ? albums : [];
  updateAlbumTargetSelect(storageAlbumsCache);
  if (!Array.isArray(albums) || !albums.length) {
    list.innerHTML = `<div class="drive-empty">尚無相簿</div>`;
    closeAlbumDetail();
    return;
  }
  list.innerHTML = albums.map((album) => `
    <div class="drive-file-row">
      <div>
        <strong>${sanitize(album.title || album.id)}</strong>
        <div class="drive-card-sub">${sanitize(albumVisibilityLabel(album.visibility))} · ${Number(album.file_count || 0)} 個檔案${album.description ? ` · ${sanitize(album.description)}` : ""}</div>
      </div>
      <div class="drive-file-actions">
        ${albumShareButtonMarkup(album)}
        <button class="btn btn-primary" type="button" data-drive-action="open-album" data-album-id="${sanitize(album.id)}">預覽</button>
        <button class="btn btn-danger" type="button" data-drive-action="delete-album" data-album-id="${sanitize(album.id)}">刪除</button>
      </div>
    </div>
  `).join("");
}

function storageAlbumsFeatureEnabled() {
  if (typeof isFeatureEnabledForUi === "function") return isFeatureEnabledForUi("feature_storage_albums_enabled", false);
  if (!siteConfig || typeof siteConfig !== "object") return true;
  return siteConfig.feature_storage_albums_enabled !== false;
}

function renderStorageFeatureDisabled() {
  storageFilesCache = [];
  storageFoldersCache = [];
  storageAlbumsCache = [];
  selectedStorageFileId = "";
  selectedStorageFileIds = new Set();
  selectedStorageFolderPaths = new Set();
  selectedAlbumId = "";
  const message = "Storage / 相簿目前未啟用。請由 root 到設定 > 功能開關，至少一起開啟「隱私分級上傳 / E2EE」與「Storage / 相簿」。";
  if ($("storage-selection-label")) $("storage-selection-label").textContent = message;
  if ($("storage-browser-list")) $("storage-browser-list").innerHTML = `<div class="drive-empty">${sanitize(message)}</div>`;
  if ($("storage-trash-list")) $("storage-trash-list").innerHTML = `<div class="drive-empty">Storage 未啟用，因此沒有可顯示的已刪除檔案。</div>`;
  if ($("album-list")) $("album-list").innerHTML = `<div class="drive-empty">${sanitize(message)}</div>`;
  if ($("album-gallery-list")) $("album-gallery-list").innerHTML = `<div class="drive-empty">${sanitize(message)}</div>`;
  closeAlbumDetail();
}

async function loadStorageFiles(csrf) {
  if (!storageAlbumsFeatureEnabled()) {
    renderStorageFeatureDisabled();
    return;
  }
  const headers = { "X-CSRF-Token": csrf || "" };
  const [filesRes, trashRes, foldersRes, albumsRes] = await Promise.all([
    apiFetch(API + "/storage/files", { credentials: "same-origin", headers }),
    apiFetch(API + "/storage/trash", { credentials: "same-origin", headers }),
    apiFetch(API + "/storage/folders", { credentials: "same-origin", headers }),
    apiFetch(API + "/storage/albums", { credentials: "same-origin", headers })
  ]);
  const filesJson = await filesRes.json().catch(() => ({}));
  const trashJson = await trashRes.json().catch(() => ({}));
  const foldersJson = await foldersRes.json().catch(() => ({}));
  const albumsJson = await albumsRes.json().catch(() => ({}));
  storageFilesCache = filesJson.ok ? filesJson.files || [] : [];
  storageFoldersCache = foldersJson.ok ? foldersJson.folders || [] : [];
  renderStorageBrowser();
  renderStorageTrash(trashJson.ok ? trashJson.files || [] : []);
  renderAlbums(albumsJson.ok ? albumsJson.albums || [] : []);
  if (selectedAlbumId && (albumsJson.ok ? (albumsJson.albums || []).some((album) => album.id === selectedAlbumId) : false)) {
    await openAlbum(selectedAlbumId, { quiet: true });
  }
}

async function createStorageFolder() {
  const input = $("storage-folder-path");
  const requested = input?.value || window.prompt("新增資料夾名稱", "");
  if (requested === null) return;
  if (!String(requested).trim()) return;
  const path = requested && requested.startsWith("/") ? requested : joinStoragePath(currentStoragePath, requested || "");
  if (!path.trim()) {
    alert("請輸入資料夾路徑");
    return;
  }
  try {
    await storageAction("/storage/folders", "POST", { path });
    if (input) input.value = "";
    await loadDriveDashboard();
  } catch (err) { alert(err.message || "建立資料夾失敗"); }
}

function selectStorageFileForOrganize(id, path) {
  setStorageSelection(id, path);
}

function selectVisibleStorageItems() {
  visibleStorageFiles().forEach((file) => selectedStorageFileIds.add(String(file.id || "")));
  visibleStorageFolders().forEach((folder) => selectedStorageFolderPaths.add(normalizeStoragePath(folder.virtual_path || "")));
  selectedStorageFileId = selectedStorageFileIds.size === 1 ? Array.from(selectedStorageFileIds)[0] : "";
  selectedStorageFilePath = "";
  updateStorageSelectionLabel();
  renderStorageBrowser();
}

function clearStorageSelection() {
  selectedStorageFileId = "";
  selectedStorageFilePath = "";
  selectedStorageFileIds = new Set();
  selectedStorageFolderPaths = new Set();
  updateStorageSelectionLabel();
  renderStorageBrowser();
}

async function trashSelectedStorageItems() {
  return deleteSelectedStorageItems();
}

async function deleteSelectedStorageItems() {
  const fileIds = Array.from(selectedStorageFileIds).filter(Boolean);
  const folderPaths = Array.from(selectedStorageFolderPaths).filter(Boolean);
  const total = fileIds.length + folderPaths.length;
  if (!total) return;
  if (!window.confirm(`刪除已選取的 ${total} 個檔案/資料夾？可在已刪除檔案中還原。`)) return;
  try {
    for (const path of folderPaths) await storageAction("/storage/folders/trash", "POST", { path });
    for (const id of fileIds) await storageAction(`/storage/files/${encodeURIComponent(id)}`, "DELETE");
    clearStorageSelection();
    await loadDriveDashboard();
  } catch (err) {
    alert(err.message || "批次刪除失敗");
  }
}

async function moveSelectedStorageItems() {
  const folders = selectedStorageFolders();
  const selectedFolderPaths = folders.map((folder) => normalizeStoragePath(folder?.virtual_path || "")).filter(Boolean);
  const files = selectedStorageFiles()
    .filter((file) => !selectedFolderPaths.some((folderPath) => storageFileIsInsideFolder(file, folderPath)));
  const total = files.length + folders.length;
  if (!total) return;
  const requested = window.prompt("移動已選取項目到資料夾", currentStoragePath || "/");
  if (requested === null) return;
  const targetFolder = requested && requested.startsWith("/")
    ? normalizeStoragePath(requested)
    : joinStoragePath(currentStoragePath, requested || "");
  if (!targetFolder.trim()) {
    alert("請輸入目標資料夾");
    return;
  }
  try {
    for (const folder of folders) {
      const oldPath = normalizeStoragePath(folder?.virtual_path || "");
      if (!oldPath || oldPath === "/") continue;
      if (targetFolder === oldPath || targetFolder.startsWith(`${oldPath}/`)) {
        throw new Error(`不能將資料夾「${oldPath}」移到自己或子資料夾內`);
      }
      const newPath = joinStoragePath(targetFolder, storageBaseName(oldPath) || "folder");
      if (newPath !== oldPath) {
        await storageAction("/storage/folders/move", "PUT", { old_path: oldPath, new_path: newPath });
      }
    }
    for (const file of files) {
      const id = String(file?.id || "");
      if (!id) continue;
      const newPath = joinStoragePath(targetFolder, storageFileDisplayName(file));
      await storageAction(`/storage/files/${encodeURIComponent(id)}/organize`, "PUT", { virtual_path: newPath });
    }
    clearStorageSelection();
    currentStoragePath = targetFolder;
    await loadDriveDashboard();
  } catch (err) {
    alert(err.message || "批次移動失敗");
  }
}

async function shareSelectedStorageItems() {
  const files = selectedStorageFilesForShareOrDownload();
  const folders = selectedStorageFolders();
  if (!files.length && folders.length === 1) {
    const folder = folders[0];
    return shareStorageFolder(folder.virtual_path || "", folder.display_name || storageBaseName(folder.virtual_path || ""));
  }
  if (!files.length) {
    alert("選取的資料夾目前沒有可分享的檔案");
    return;
  }
  if (files.length === 1 && !folders.length) {
    const file = files[0];
    if (!file.file_id) {
      alert("這個檔案目前無法建立分享");
      return;
    }
    return openDriveShareDialogAfterDecrypt(file.file_id, storageFileDisplayName(file), file.id);
  }
  const defaultTitle = currentStoragePath && currentStoragePath !== "/"
    ? `${storageBaseName(currentStoragePath)} 批次分享`
    : "雲端硬碟批次分享";
  const title = window.prompt("批次分享名稱", defaultTitle);
  if (title === null) return;
  const cleanTitle = title.trim();
  if (!cleanTitle) {
    alert("分享名稱不可為空");
    return;
  }
  try {
    const albumJson = await storageAction("/storage/albums", "POST", {
      title: cleanTitle,
      description: `由雲端硬碟批次選取 ${files.length} 個檔案建立`,
      visibility: "private"
    });
    const albumId = albumJson.album?.id || "";
    if (!albumId) throw new Error("相簿建立失敗");
    for (const file of files) {
      await storageAction(`/storage/albums/${encodeURIComponent(albumId)}/files`, "POST", { storage_file_id: file.id });
    }
    selectedAlbumId = albumId;
    selectedAlbumViewerId = albumId;
    clearStorageSelection();
    if (typeof shareAlbum === "function") {
      await shareAlbum(albumId);
    } else {
      await loadDriveDashboard();
      await loadAlbumGallery();
    }
  } catch (err) {
    alert(err.message || "批次分享建立失敗");
  }
}

async function downloadSelectedStorageItems() {
  const files = selectedStorageFilesForShareOrDownload();
  if (!files.length) {
    alert("選取的資料夾目前沒有可下載的檔案");
    return;
  }
  if (files.length > 5 && !window.confirm(`將分別下載 ${files.length} 個檔案，瀏覽器可能會詢問是否允許多檔下載。`)) return;
  files.forEach((file, index) => {
    window.setTimeout(() => {
      triggerBrowserDownload(`${API}/storage/files/${encodeURIComponent(file.id)}/download`, storageFileDisplayName(file));
    }, index * 350);
  });
}

async function organizeSelectedStorageFile() {
  if (!selectedStorageFileId) {
    alert("請先在 Storage 檔案列表選取檔案");
    return;
  }
  const requested = $("storage-organize-path")?.value || window.prompt("移動或重新命名到", selectedStorageFilePath || currentStoragePath);
  if (requested === null) return;
  if (!String(requested).trim()) return;
  const path = requested && requested.startsWith("/") ? requested : joinStoragePath(currentStoragePath, requested || "");
  if (!path.trim()) {
    alert("請輸入新路徑");
    return;
  }
  try {
    await storageAction(`/storage/files/${encodeURIComponent(selectedStorageFileId)}/organize`, "PUT", { virtual_path: path });
    setStorageSelection("", "");
    if ($("storage-organize-path")) $("storage-organize-path").value = "";
    currentStoragePath = storageDirName(path);
    await loadDriveDashboard();
  } catch (err) { alert(err.message || "整理檔案失敗"); }
}

async function moveStorageFileFromRow(id, currentPath) {
  setStorageSelection(id, currentPath);
  const requested = window.prompt("移動或重新命名到", currentPath || currentStoragePath);
  if (requested === null) return;
  if (!String(requested).trim()) return;
  const path = requested && requested.startsWith("/") ? requested : joinStoragePath(currentStoragePath, requested || "");
  if (!path.trim() || path === "/") {
    alert("請輸入包含檔名的新路徑");
    return;
  }
  try {
    await storageAction(`/storage/files/${encodeURIComponent(id)}/organize`, "PUT", { virtual_path: path });
    setStorageSelection("", "");
    currentStoragePath = storageDirName(path);
    await loadDriveDashboard();
  } catch (err) {
    alert(err.message || "移動檔案失敗");
  }
}

async function renameStorageFile(id, currentPath, currentName = "") {
  const oldPath = normalizeStoragePath(currentPath);
  const currentDir = storageDirName(oldPath);
  const fallbackName = currentName || storageBaseName(oldPath) || "file";
  const requested = window.prompt("重新命名檔案", fallbackName);
  if (requested === null) return;
  const cleanName = String(requested).trim();
  if (!cleanName) {
    alert("檔名不可為空");
    return;
  }
  if (cleanName.includes("/")) {
    alert("重新命名只接受檔名，不可包含 /");
    return;
  }
  const path = joinStoragePath(currentDir, cleanName);
  try {
    await storageAction(`/storage/files/${encodeURIComponent(id)}/organize`, "PUT", { virtual_path: path });
    setStorageSelection("", "");
    await loadDriveDashboard();
  } catch (err) {
    alert(err.message || "重新命名檔案失敗");
  }
}

function selectStorageFolderForMove(path) {
  const oldPath = normalizeStoragePath(path);
  const requested = window.prompt("移動資料夾到", oldPath);
  if (!requested) return;
  if ($("storage-folder-move-old")) $("storage-folder-move-old").value = oldPath;
  if ($("storage-folder-move-new")) $("storage-folder-move-new").value = requested.startsWith("/") ? requested : joinStoragePath(storageDirName(oldPath), requested);
  moveStorageFolder();
}

async function renameStorageFolder(path, currentName = "") {
  const oldPath = normalizeStoragePath(path);
  const currentDir = storageDirName(oldPath);
  const fallbackName = currentName || storageBaseName(oldPath) || "folder";
  const requested = window.prompt("重新命名資料夾", fallbackName);
  if (requested === null) return;
  const cleanName = String(requested).trim();
  if (!cleanName) {
    alert("資料夾名稱不可為空");
    return;
  }
  if (cleanName.includes("/")) {
    alert("重新命名只接受資料夾名稱，不可包含 /");
    return;
  }
  const newPath = joinStoragePath(currentDir, cleanName);
  try {
    await storageAction("/storage/folders/move", "PUT", { old_path: oldPath, new_path: newPath });
    if (currentStoragePath === oldPath || currentStoragePath.startsWith(`${oldPath}/`)) {
      currentStoragePath = currentStoragePath === oldPath
        ? newPath
        : currentStoragePath.replace(oldPath, newPath);
    }
    await loadDriveDashboard();
  } catch (err) {
    alert(err.message || "重新命名資料夾失敗");
  }
}

async function moveStorageFolder() {
  const oldPath = $("storage-folder-move-old")?.value || "";
  const newPath = $("storage-folder-move-new")?.value || "";
  if (!oldPath.trim() || !newPath.trim()) {
    alert("請輸入原資料夾與新資料夾路徑");
    return;
  }
  try {
    await storageAction("/storage/folders/move", "PUT", { old_path: oldPath, new_path: newPath });
    if ($("storage-folder-move-old")) $("storage-folder-move-old").value = "";
    if ($("storage-folder-move-new")) $("storage-folder-move-new").value = "";
    currentStoragePath = normalizeStoragePath(newPath);
    await loadDriveDashboard();
  } catch (err) { alert(err.message || "移動資料夾失敗"); }
}

async function trashStorageFolder(path) {
  const folderPath = normalizeStoragePath(path);
  if (!window.confirm(`刪除資料夾「${folderPath}」與其中檔案？可在已刪除檔案中還原。`)) return;
  try {
    await storageAction("/storage/folders/trash", "POST", { path: folderPath });
    if (currentStoragePath === folderPath || currentStoragePath.startsWith(`${folderPath}/`)) {
      currentStoragePath = storageDirName(folderPath);
    }
    await loadDriveDashboard();
  } catch (err) { alert(err.message || "刪除資料夾失敗"); }
}

async function createAlbumFromFolder(path, name = "") {
  const folderPath = normalizeStoragePath(path);
  const defaultTitle = name || storageBaseName(folderPath) || "資料夾相簿";
  const title = window.prompt("建立相簿名稱", defaultTitle);
  if (title === null) return;
  const cleanTitle = title.trim();
  if (!cleanTitle) {
    alert("相簿名稱不可為空");
    return;
  }
  try {
    const json = await storageAction("/storage/folders/album", "POST", {
      path: folderPath,
      title: cleanTitle,
      visibility: "private"
    });
    storageAlbumsCache = [];
    selectedAlbumId = json.album?.id || selectedAlbumId;
    await loadDriveDashboard();
    alert(`已建立相簿「${json.album?.title || cleanTitle}」，加入 ${Number(json.album?.added_count || json.album?.files?.length || 0)} 個檔案`);
  } catch (err) {
    alert(err.message || "資料夾設為相簿失敗");
  }
}

async function shareStorageFolder(path, name = "") {
  const folderPath = normalizeStoragePath(path);
  const defaultTitle = name || storageBaseName(folderPath) || "資料夾分享";
  const title = window.prompt("資料夾分享名稱", defaultTitle);
  if (title === null) return;
  const cleanTitle = title.trim();
  if (!cleanTitle) {
    alert("分享名稱不可為空");
    return;
  }
  try {
    const json = await storageAction("/storage/folders/album", "POST", {
      path: folderPath,
      title: cleanTitle,
      visibility: "unlisted"
    });
    const album = json.album || {};
    const shareId = album?.share_link?.id || "";
    storageAlbumsCache = [];
    selectedAlbumId = album.id || selectedAlbumId;
    selectedAlbumViewerId = album.id || selectedAlbumViewerId;
    await loadDriveDashboard();
    await loadAlbumGallery();
    if (typeof switchModuleTab === "function") switchModuleTab("shares");
    if (shareId && typeof openShareCenterEditor === "function") {
      await openShareCenterEditor("album", shareId);
    } else if (typeof loadShareCenter === "function") {
      await loadShareCenter();
    }
  } catch (err) {
    alert(err.message || "資料夾分享建立失敗");
  }
}

function openStorageFolder(path) {
  currentStoragePath = normalizeStoragePath(path);
  setStorageSelection("", "");
  renderStorageBrowser();
}

function openStorageUploadPicker() {
  const input = $("storage-upload-file");
  if (input) input.click();
}

function openStorageFolderUploadPicker() {
  const input = $("storage-upload-folder");
  if (input) input.click();
}

function findDuplicateDriveUploadCandidate(file, privacyMode) {
  const mode = String(privacyMode || "standard_plain");
  if (mode === "e2ee") return null;
  const name = String(file?.name || "").trim().toLowerCase();
  const size = Number(file?.size || -1);
  if (!name || size < 0) return null;
  return (Array.isArray(lastDriveFiles) ? lastDriveFiles : []).find((item) => {
    const itemName = String(item?.original_filename_plain_for_public || item?.display_name || item?.filename || "").trim().toLowerCase();
    return itemName === name && Number(item?.size_bytes || -2) === size && String(item?.privacy_mode || "standard_plain") === mode && !item?.deleted_at;
  }) || null;
}

function findStorageFileByVirtualPath(virtualPath) {
  const target = normalizeStoragePath(virtualPath);
  if (!target) return null;
  return (Array.isArray(storageFilesCache) ? storageFilesCache : []).find((item) => {
    return normalizeStoragePath(item?.virtual_path || "") === target && !item?.deleted_at && !item?.is_trashed;
  }) || null;
}

async function confirmStorageUploadReplacement(existing, options, csrf, virtualPath) {
  if (!existing) return {};
  const label = existing.display_name || storageBaseName(existing.virtual_path || virtualPath) || "既有檔案";
  const ok = window.confirm(`同一路徑已有檔案「${label}」。要覆蓋並更新此檔案嗎？`);
  if (!ok) throw new Error("已取消覆蓋上傳");
  const fields = { replace_existing_confirmed: "1" };
  const existingIsE2ee = driveFileIsE2ee(existing);
  const nextIsE2ee = isDriveE2eeMode(options?.privacyMode);
  if (!existingIsE2ee && !nextIsE2ee) return fields;
  if (existingIsE2ee) {
    const fileId = existing.file_id || existing.id;
    const e2ee = await fetchDriveE2eeKey(fileId, csrf);
    let verified = false;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const passphrase = await getDriveE2eeSessionPassphrase(
        fileId,
        "覆蓋或變更 E2EE 檔案前，請輸入原始檔案密碼。密碼不會送到伺服器。",
        { force: true, allowPrompt: true }
      );
      if (!passphrase) throw new Error("覆蓋 E2EE 檔案需要輸入原始密碼");
      try {
        await unwrapDriveFileKey(e2ee.encrypted_file_key, passphrase);
        rememberDriveE2eeSessionPassphrase(fileId, passphrase);
        verified = true;
        break;
      } catch (err) {
        forgetDriveE2eeSessionPassphrase(fileId);
        if (attempt > 0) throw new Error("E2EE 原始密碼不正確，無法覆蓋檔案");
        alert("E2EE 原始密碼不正確，請重新輸入。");
      }
    }
    if (!verified) throw new Error("覆蓋 E2EE 檔案需要驗證原始密碼");
  }
  if (nextIsE2ee && !String(options?.passphrase || "")) {
    throw new Error("上傳為 E2EE 需要輸入新的檔案密碼");
  }
  fields.replace_existing_password_confirmed = "1";
  return fields;
}

async function uploadStorageFile() {
  const input = $("storage-upload-file");
  const pathInput = $("storage-upload-path");
  if (!input || !input.files || !input.files[0]) {
    alert("請先選擇檔案");
    return;
  }
  const file = input.files[0];
  if (!(await preflightDriveUploadSize(file.size, `檔案「${file.name || "upload.bin"}」`))) {
    input.value = "";
    return;
  }
  const options = await askDriveUploadPrivacyOptions({ allowE2ee: true, title: `上傳「${file.name}」前選擇隱私模式` });
  if (!options) {
    input.value = "";
    return;
  }
  if ($("drive-upload-privacy-mode")) $("drive-upload-privacy-mode").value = options.privacyMode;
  const transferId = addDriveTransferRow({
    kind: "upload",
    name: file.name,
    loaded_bytes: 0,
    total_bytes: file.size,
    progress_percent: 0,
    msg: "等待上傳",
  });
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const form = new FormData();
  try {
    let uploadBlob = file;
    let uploadFilename = file.name || "upload.bin";
    let uploadMimeType = file.type || "application/octet-stream";
    let uploadFields = {};
    const virtualPath = pathInput?.value || joinStoragePath(currentStoragePath, file.name);
    const existingStorageFile = findStorageFileByVirtualPath(virtualPath);
    const replacementFields = await confirmStorageUploadReplacement(existingStorageFile, options, csrf, virtualPath);
    uploadFields = { ...uploadFields, ...replacementFields };
    const duplicate = existingStorageFile ? null : findDuplicateDriveUploadCandidate(file, options.privacyMode);
    if (duplicate?.id) {
      updateDriveTransferRow(transferId, { phase: "deduplicated", msg: "偵測到既有雲端檔案，改用既有版本", progress_percent: 100, status: "completed" });
      await storageAction("/storage/files/attach-existing", "POST", {
        file_id: duplicate.id,
        virtual_path: virtualPath,
        display_name: file.name || storageBaseName(virtualPath),
      });
      input.value = "";
      await loadDriveDashboard();
      setTimeout(() => removeDriveTransferRow(transferId), DRIVE_TRANSFER_COMPLETED_VISIBLE_MS);
      return;
    }
    if (isDriveE2eeMode(options.privacyMode)) {
      updateDriveTransferRow(transferId, { phase: "encrypting", msg: "瀏覽器端加密中", progress_percent: null });
      const encrypted = await prepareDriveE2eeUpload(file, options.passphrase);
      const encryptedQuotaError = driveUploadQuotaError(encrypted.blob.size, `加密後檔案「${file.name || encrypted.filename}」`);
      if (encryptedQuotaError) throw new Error(encryptedQuotaError);
      uploadBlob = encrypted.blob;
      uploadFilename = encrypted.filename;
      uploadMimeType = encrypted.blob.type || "application/octet-stream";
      uploadFields = { ...uploadFields, ...driveEncryptedUploadFields(encrypted) };
      updateDriveTransferRow(transferId, { phase: "uploading", msg: "加密完成，開始上傳密文", progress_percent: 0 });
    }
    let json = null;
    if (shouldUseDriveResumableUpload(uploadBlob)) {
      json = await uploadDriveBlobResumable({
        blob: uploadBlob,
        sourceFile: file,
        filename: uploadFilename,
        mimeType: uploadMimeType,
        privacyMode: options.privacyMode,
        fields: uploadFields,
        target: "storage",
        virtualPath,
        displayName: file.name || uploadFilename,
        transferId,
        csrf,
        label: uploadFilename,
      });
    } else {
      form.append("file", uploadBlob, uploadFilename);
      appendDriveUploadFields(form, uploadFields);
      form.append("privacy_mode", options.privacyMode);
      form.append("virtual_path", virtualPath);
      const upload = await xhrUploadWithProgress(API + "/storage/files", form, csrf, (event) => {
        if (event.lengthComputable) {
          updateDriveTransferRow(transferId, {
            loaded_bytes: event.loaded,
            total_bytes: event.total,
            progress_percent: (event.loaded / event.total) * 100,
            phase: event.loaded >= event.total ? "server_processing" : "uploading",
            msg: event.loaded >= event.total ? drivePostUploadProcessingMessage(options.privacyMode) : "上傳中",
          });
        } else {
          updateDriveTransferRow(transferId, {
            loaded_bytes: event.loaded || 0,
            total_bytes: null,
            progress_percent: null,
            msg: "上傳中",
          });
        }
      });
      json = upload.json || {};
      if (upload.status < 200 || upload.status >= 300 || !json.ok) {
        const detail = json.msg || `Storage 上傳失敗（HTTP ${upload.status}）`;
        updateDriveTransferRow(transferId, { status: "failed", phase: "failed", msg: detail, progress_percent: 100 });
        alert(detail);
        return;
      }
    }
    updateDriveTransferRow(transferId, {
      status: "completed",
      phase: "completed",
      msg: "上傳完成",
      progress_percent: 100,
      loaded_bytes: uploadBlob.size,
      total_bytes: uploadBlob.size,
      source_ref: json.file?.file_id ? `cloud_file:${json.file.file_id}` : "",
    });
    input.value = "";
    if (pathInput) pathInput.value = "";
    await loadDriveDashboard();
    setTimeout(() => removeDriveTransferRow(transferId), DRIVE_TRANSFER_COMPLETED_VISIBLE_MS);
  } catch (err) {
    const detail = err.message || "Storage 上傳失敗";
    updateDriveTransferRow(transferId, { status: "failed", phase: "failed", msg: detail, progress_percent: 100 });
    alert(detail);
  }
}

async function uploadStorageFolder() {
  const input = $("storage-upload-folder");
  const files = Array.from(input?.files || []).filter((file) => file && file.name);
  if (!input || !files.length) {
    alert("請先選擇資料夾");
    return;
  }
  const totalBytes = files.reduce((sum, file) => sum + Number(file.size || 0), 0);
  if (!(await preflightDriveUploadSize(totalBytes, `資料夾總大小`, { checkMaxFile: false }))) {
    input.value = "";
    return;
  }
  const quota = await ensureDriveUploadQuota();
  const oversizedFile = files.find((file) => driveUploadQuotaError(Number(file.size || 0), `檔案「${storageUploadRelativePath(file) || file.name}」`, { quota }));
  if (oversizedFile) {
    const detail = driveUploadQuotaError(Number(oversizedFile.size || 0), `檔案「${storageUploadRelativePath(oversizedFile) || oversizedFile.name}」`, { quota });
    const msg = $("drive-msg");
    if (msg) flash(msg, detail, false);
    alert(detail);
    input.value = "";
    return;
  }
  const options = await askDriveUploadPrivacyOptions({ allowE2ee: true, title: `上傳資料夾（${files.length} 個檔案）前選擇隱私模式` });
  if (!options) {
    input.value = "";
    return;
  }
  if ($("drive-upload-privacy-mode")) $("drive-upload-privacy-mode").value = options.privacyMode;
  const transferId = addDriveTransferRow({
    kind: "folder_upload",
    name: `${storageBaseName(storageUploadRelativePath(files[0]).split("/")[0] || "資料夾")}（${files.length} 個檔案）`,
    loaded_bytes: 0,
    total_bytes: totalBytes,
    progress_percent: 0,
    msg: "資料夾上傳準備中",
  });
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  let uploadedBytes = 0;
  let progressTotalBytes = totalBytes;
  let okCount = 0;
  const failures = [];
  for (const file of files) {
    const relativePath = storageUploadRelativePath(file);
    const virtualPath = joinStoragePath(currentStoragePath, relativePath || file.name);
    const fileSize = Number(file.size || 0);
    updateDriveTransferRow(transferId, {
      loaded_bytes: uploadedBytes,
      total_bytes: progressTotalBytes,
      progress_percent: progressTotalBytes > 0 ? (uploadedBytes / progressTotalBytes) * 100 : null,
      msg: `上傳中：${relativePath || file.name}`,
    });
    const form = new FormData();
    let uploadBlob = file;
    let uploadFilename = file.name || "upload.bin";
    let uploadMimeType = file.type || "application/octet-stream";
    let uploadFields = {};
    let uploadDisplayBytes = fileSize;
    try {
      if (isDriveE2eeMode(options.privacyMode)) {
        updateDriveTransferRow(transferId, {
          loaded_bytes: uploadedBytes,
          total_bytes: progressTotalBytes,
          progress_percent: progressTotalBytes > 0 ? (uploadedBytes / progressTotalBytes) * 100 : null,
          phase: "encrypting",
          msg: `瀏覽器端加密中：${relativePath || file.name}`,
        });
        const encrypted = await prepareDriveE2eeUpload(file, options.passphrase);
        const encryptedQuotaError = driveUploadQuotaError(encrypted.blob.size, `加密後檔案「${relativePath || file.name}」`);
        if (encryptedQuotaError) throw new Error(encryptedQuotaError);
        uploadBlob = encrypted.blob;
        uploadFilename = encrypted.filename;
        uploadMimeType = encrypted.blob.type || "application/octet-stream";
        uploadFields = driveEncryptedUploadFields(encrypted);
        uploadDisplayBytes = Number(uploadBlob.size || 0);
        progressTotalBytes = Math.max(0, progressTotalBytes + uploadDisplayBytes - fileSize);
        updateDriveTransferRow(transferId, {
          loaded_bytes: uploadedBytes,
          total_bytes: progressTotalBytes,
          progress_percent: progressTotalBytes > 0 ? (uploadedBytes / progressTotalBytes) * 100 : null,
          phase: "uploading",
          msg: `加密完成，開始上傳：${relativePath || file.name}`,
        });
      }
    } catch (err) {
      failures.push(`${relativePath || file.name}: ${err.message || "加密失敗"}`);
      uploadedBytes += Number(file.size || 0);
      continue;
    }
    try {
      if (shouldUseDriveResumableUpload(uploadBlob)) {
        await uploadDriveBlobResumable({
          blob: uploadBlob,
          sourceFile: file,
          filename: uploadFilename,
          mimeType: uploadMimeType,
          privacyMode: options.privacyMode,
          fields: uploadFields,
          target: "storage",
          virtualPath,
          displayName: relativePath || file.name,
          transferId,
          csrf,
          aggregateBaseBytes: uploadedBytes,
          aggregateTotalBytes: progressTotalBytes,
          label: relativePath || file.name,
          exposeSessionAsTransfer: false,
        });
        okCount += 1;
      } else {
        form.append("file", uploadBlob, uploadFilename);
        appendDriveUploadFields(form, uploadFields);
        form.append("privacy_mode", options.privacyMode);
        form.append("virtual_path", virtualPath);
      const { status, json } = await xhrUploadWithProgress(API + "/storage/files", form, csrf, (event) => {
        const currentLoaded = event.lengthComputable ? Math.min(uploadDisplayBytes, event.loaded || 0) : 0;
        const aggregateLoaded = uploadedBytes + currentLoaded;
        updateDriveTransferRow(transferId, {
          loaded_bytes: aggregateLoaded,
          total_bytes: progressTotalBytes,
          progress_percent: progressTotalBytes > 0 ? (aggregateLoaded / progressTotalBytes) * 100 : null,
          phase: event.lengthComputable && event.loaded >= event.total ? "server_processing" : "uploading",
          msg: event.lengthComputable
            ? (event.loaded >= event.total ? `${drivePostUploadProcessingMessage(options.privacyMode)}：${relativePath || file.name}` : `上傳中：${relativePath || file.name}`)
            : `上傳中：${relativePath || file.name}（等待瀏覽器回報大小）`,
        });
      });
      if (status < 200 || status >= 300 || !json.ok) {
        failures.push(`${relativePath || file.name}: ${json.msg || `HTTP ${status}`}`);
      } else {
        okCount += 1;
      }
      }
    } catch (err) {
      failures.push(`${relativePath || file.name}: ${err.message || "上傳失敗"}`);
    }
    uploadedBytes += uploadDisplayBytes;
  }
  updateDriveTransferRow(transferId, {
    status: failures.length ? "failed" : "completed",
    phase: failures.length ? "failed" : "completed",
    loaded_bytes: uploadedBytes,
    total_bytes: progressTotalBytes,
    progress_percent: 100,
    msg: failures.length ? `完成 ${okCount}/${files.length}，失敗 ${failures.length}` : `已上傳 ${okCount} 個檔案`,
  });
  input.value = "";
  await loadDriveDashboard();
  if (failures.length) {
    alert(`資料夾上傳完成，但有 ${failures.length} 個檔案失敗：\n${failures.slice(0, 5).join("\n")}${failures.length > 5 ? "\n..." : ""}`);
  } else {
    setTimeout(() => removeDriveTransferRow(transferId), DRIVE_TRANSFER_COMPLETED_VISIBLE_MS);
  }
}

async function storageAction(path, method = "POST", body = null) {
  const upperMethod = String(method || "GET").toUpperCase();
  const csrf = await fetchCsrfToken({ force: upperMethod !== "GET" });
  const headers = { "X-CSRF-Token": csrf || "" };
  if (body) headers["Content-Type"] = "application/json";
  const options = {
    method: upperMethod,
    credentials: "same-origin",
    cache: "no-store",
    headers,
    body: body ? JSON.stringify(body) : undefined
  };
  let res;
  try {
    res = await apiFetch(API + path, options);
  } catch (err) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    try {
      res = await apiFetch(API + path, options);
    } catch (retryErr) {
      throw new Error(`連線失敗：${retryErr.message || err.message || "無法連到 API"}`);
    }
  }
  const text = await res.text().catch(() => "");
  let json = {};
  try {
    json = text ? JSON.parse(text) : {};
  } catch (err) {
    json = {};
  }
  if (!res.ok || !json.ok) {
    const fallback = res.ok ? "操作失敗" : `操作失敗（HTTP ${res.status}）`;
    throw new Error(json.msg || fallback);
  }
  return json;
}

async function trashStorageFile(id) {
  try {
    await storageAction(`/storage/files/${encodeURIComponent(id)}`, "DELETE");
    await loadDriveDashboard();
  } catch (err) { alert(err.message); }
}

async function restoreStorageFile(id) {
  try {
    await storageAction(`/storage/files/${encodeURIComponent(id)}/restore`, "POST");
    await loadDriveDashboard();
  } catch (err) { alert(err.message); }
}

async function purgeStorageFile(id) {
  if (!window.confirm("永久刪除此已刪除項目並釋放容量？對應的雲端硬碟檔案與實體檔案也會刪除。")) return;
  try {
    await storageAction(`/storage/files/${encodeURIComponent(id)}/purge`, "DELETE");
    await loadDriveDashboard();
  } catch (err) { alert(err.message); }
}

async function restoreStorageTrash() {
  try {
    await storageAction("/storage/trash/restore", "POST");
    await loadDriveDashboard();
  } catch (err) { alert(err.message || "還原已刪除檔案失敗"); }
}

async function purgeStorageTrash() {
  if (!window.confirm("清空已刪除檔案並釋放容量？其中對應的雲端硬碟檔案與實體檔案也會刪除。")) return;
  try {
    await storageAction("/storage/trash/purge", "DELETE");
    await loadDriveDashboard();
  } catch (err) { alert(err.message || "清空已刪除檔案失敗"); }
}

async function downloadStorageFile(id) {
  const known = (Array.isArray(storageFilesCache) ? storageFilesCache : []).find((file) => String(file?.id || "") === String(id || ""));
  triggerBrowserDownload(`${API}/storage/files/${encodeURIComponent(id)}/download`, known?.display_name || "");
}

async function ensureAlbumChoicesLoaded() {
  if (!storageAlbumsFeatureEnabled()) throw new Error("Storage / 相簿目前未啟用，請先由 root 開啟完整雲端硬碟組合。");
  if (storageAlbumsCache.length) return storageAlbumsCache;
  const json = await storageAction("/storage/albums", "GET");
  storageAlbumsCache = Array.isArray(json.albums) ? json.albums : [];
  updateAlbumTargetSelect(storageAlbumsCache);
  return storageAlbumsCache;
}

function closeAlbumPicker(value = "") {
  const overlay = $("album-picker-overlay");
  if (overlay) overlay.classList.remove("show");
  if (pendingAlbumPickerResolve) {
    pendingAlbumPickerResolve(value);
    pendingAlbumPickerResolve = null;
  }
}

async function chooseAlbumForFile(fileLabel = "") {
  const albums = await ensureAlbumChoicesLoaded();
  if (!albums.length) {
    alert("目前沒有相簿，請先到相簿分頁建立相簿");
    return "";
  }
  updateAlbumTargetSelect(albums);
  const label = $("album-picker-file-label");
  if (label) label.textContent = fileLabel ? `將「${fileLabel}」加入` : "選擇要加入的相簿";
  const msg = $("album-picker-msg");
  if (msg) msg.textContent = "";
  setAlbumPickerSelection(selectedAlbumId && albums.some((album) => album.id === selectedAlbumId) ? selectedAlbumId : albums[0].id);
  const overlay = $("album-picker-overlay");
  if (overlay) overlay.classList.add("show");
  return new Promise((resolve) => {
    pendingAlbumPickerResolve = resolve;
  });
}

async function createAlbum() {
  const title = $("album-create-title")?.value || "";
  const description = $("album-create-description")?.value || "";
  const visibility = $("album-create-visibility")?.value || "private";
  if (!title.trim()) {
    alert("請輸入相簿名稱");
    return;
  }
  try {
    const payload = { title, description, visibility };
    const json = await storageAction("/storage/albums", "POST", payload);
    $("album-create-title").value = "";
    if ($("album-create-description")) $("album-create-description").value = "";
    selectedAlbumId = json.album?.id || "";
    await loadDriveDashboard();
    await loadAlbumGallery();
  } catch (err) { alert(err.message); }
}

async function smartOrganizeAlbums() {
  const strategy = $("album-smart-strategy")?.value || "folder";
  const msg = $("album-smart-organize-msg") || $("album-gallery-msg");
  const button = document.querySelector("[data-drive-action='smart-organize-albums']");
  if (button) button.disabled = true;
  if (msg) flash(msg, "正在整理相簿...", true);
  try {
    const json = await storageAction("/storage/albums/smart-organize", "POST", {
      strategy,
      visibility: "private"
    });
    const result = json.result || {};
    const text = Number(result.media_count || 0)
      ? `智慧整理完成：掃描 ${Number(result.media_count || 0)} 個媒體檔，建立 ${Number(result.created_count || 0)} 本、更新 ${Number(result.updated_count || 0)} 本，新增 ${Number(result.added_count || 0)} 個相簿項目。`
      : "沒有找到可整理的圖片或影片。";
    if (msg) flash(msg, text, true);
    await loadDriveDashboard();
    await loadAlbumGallery();
  } catch (err) {
    if (msg) flash(msg, err.message || "智慧整理失敗", false);
  } finally {
    if (button) button.disabled = false;
  }
}

async function deleteAlbum(id) {
  if (!window.confirm("刪除此相簿？不會刪除原始檔案。")) return;
  try {
    await storageAction(`/storage/albums/${encodeURIComponent(id)}`, "DELETE");
    if (selectedAlbumId === id) closeAlbumDetail();
    if (selectedAlbumViewerId === id) closeAlbumViewer();
    await loadDriveDashboard();
    await loadAlbumGallery();
  } catch (err) { alert(err.message); }
}

async function addCloudFileToAlbum(fileId, fileLabel = "") {
  const albumId = await chooseAlbumForFile(fileLabel);
  if (!albumId) {
    return;
  }
  try {
    await storageAction(`/storage/albums/${encodeURIComponent(albumId)}/files`, "POST", { file_id: fileId });
    selectedAlbumId = albumId;
    await loadDriveDashboard();
    await loadAlbumGallery();
  } catch (err) { alert(err.message); }
}

async function addStorageFileToAlbum(storageFileId, fileLabel = "") {
  const albumId = await chooseAlbumForFile(fileLabel);
  if (!albumId) {
    return;
  }
  try {
    await storageAction(`/storage/albums/${encodeURIComponent(albumId)}/files`, "POST", { storage_file_id: storageFileId });
    selectedAlbumId = albumId;
    await loadDriveDashboard();
    await loadAlbumGallery();
  } catch (err) { alert(err.message); }
}

function setDriveActivePage(page = "files") {
  const rootAllowed = currentUser === "root";
  const rootTab = $("drive-root-admin-tab");
  if (rootTab) rootTab.hidden = !rootAllowed;
  const selected = page === "root-admin" && rootAllowed ? "root-admin" : (page === "capacity" ? "capacity" : "files");
  document.querySelectorAll("[data-drive-page-tab]").forEach((tab) => {
    const active = tab.dataset.drivePageTab === selected;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-drive-page-panel]").forEach((panel) => {
    const active = panel.dataset.drivePagePanel === selected;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  if (selected === "root-admin") {
    if (typeof loadSettings === "function") loadSettings();
    if (typeof loadRootStorageUsers === "function") loadRootStorageUsers();
  }
}

function bindDriveSectionTabs() {
  document.querySelectorAll("[data-drive-page-tab]").forEach((tab) => {
    if (tab.dataset.drivePageBound === "1") return;
    tab.dataset.drivePageBound = "1";
    tab.addEventListener("click", () => setDriveActivePage(tab.dataset.drivePageTab || "files"));
  });
  setDriveActivePage(document.querySelector("[data-drive-page-tab].active")?.dataset.drivePageTab || "files");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bindDriveSectionTabs);
} else {
  bindDriveSectionTabs();
}

// Album / preview / share block moved to 35-drive-preview-share.js

document.addEventListener("click", (event) => {
  if (event.target?.id === "drive-storage-upgrade-overlay") {
    closeStorageUpgradePanel();
    return;
  }
  const pickerCard = event.target?.closest?.("[data-album-picker-card]");
  if (pickerCard) {
    event.preventDefault();
    setAlbumPickerSelection(pickerCard.dataset.albumId || "");
    return;
  }
  const pickerConfirm = event.target?.closest?.("#album-picker-confirm");
  if (pickerConfirm) {
    event.preventDefault();
    const albumId = $("album-picker-select")?.value || "";
    if (!albumId) {
      const msg = $("album-picker-msg");
      if (msg) flash(msg, "請選擇相簿", false);
      return;
    }
    closeAlbumPicker(albumId);
    return;
  }
  const pickerCancel = event.target?.closest?.("#album-picker-cancel");
  if (pickerCancel) {
    event.preventDefault();
    closeAlbumPicker("");
    return;
  }
  const button = event.target?.closest?.("[data-drive-action]");
  if (!button) return;
  event.preventDefault();
  const action = button.dataset.driveAction;
  const fileId = button.dataset.fileId || "";
  const storageFileId = button.dataset.storageFileId || "";
  const albumId = button.dataset.albumId || "";
  const albumFileId = button.dataset.albumFileId || "";
  const refId = button.dataset.refId || "";
  const contextType = button.dataset.contextType || "";
  const contextId = button.dataset.contextId || "";
  const targetId = button.dataset.targetId || "";
  const path = button.dataset.path || "";
  const name = button.dataset.name || "";
  const shareUrl = button.dataset.shareUrl || "";
  const transferId = button.dataset.transferId || "";
  const taskId = button.dataset.taskId || "";
  const sessionId = button.dataset.sessionId || "";
  const warn = button.dataset.warn === "1";
  const albumSequence = button.dataset.albumSequence || "";
  (async () => {
    if (action === "preview") return previewDriveFile(fileId, { fileName: name });
    if (action === "dismiss-transfer") return dismissRemoteDownloadTask(taskId, transferId);
    if (action === "pause-remote-download") return pauseRemoteDownloadTask(taskId, transferId);
    if (action === "resume-remote-download") return resumeRemoteDownloadTask(taskId, transferId);
    if (action === "cancel-remote-download") return cancelRemoteDownloadTask(taskId, transferId);
    if (action === "recover-remote-download") return recoverRemoteDownloadTask(taskId, transferId);
    if (action === "resume-resumable-upload") return resumeResumableUploadSession(sessionId, transferId);
    if (action === "cancel-resumable-upload") return cancelResumableUploadSession(sessionId, transferId);
    if (action === "album-full-preview") return previewAlbumFileFullscreen(fileId, name, albumSequence === "viewer" ? { files: albumPreviewSequence } : {});
    if (action === "album-preview-prev") return stepAlbumPreview(-1);
    if (action === "album-preview-next") return stepAlbumPreview(1);
    if (action === "open-storage-upgrade") return openStorageUpgradePanel();
    if (action === "close-storage-upgrade") return closeStorageUpgradePanel();
    if (action === "purchase-storage-upgrade") return purchaseStorageUpgrade();
    if (action === "open-text-document-modal") return openDriveTextDocumentModal();
    if (action === "close-text-document-modal") return closeDriveTextDocumentModal();
    if (action === "create-text-document") return createDriveTextDocument();
    if (action === "edit-text") return editDriveTextFile(fileId);
    if (action === "save-text") return saveDriveTextFile(fileId);
    if (action === "download") return downloadDriveFile(fileId, warn);
    if (action === "publish-to-video") return openDriveFileInVideoPublish(fileId, name);
    if (action === "share-cloud-file") return openDriveShareDialogAfterDecrypt(fileId, name, storageFileId);
    if (action === "close-share-dialog") return closeDriveShareDialog();
    if (action === "create-share-link") return createDriveShareLink();
    if (action === "copy-drive-share-link") return copyDriveShareUrl(shareUrl, { button, requiresFragment: button.dataset.shareRequiresFragment === "1" });
    if (action === "open-share-center") {
      closeDriveShareDialog();
      if (typeof switchModuleTab === "function") switchModuleTab("shares");
      if (typeof loadShareCenter === "function") return loadShareCenter();
      return undefined;
    }
    if (action === "move-cloud-to-storage") return moveCloudFileToStorage(fileId, name);
    if (action === "add-cloud-to-album") return addCloudFileToAlbum(fileId, name);
    if (action === "delete-cloud") return deleteDriveFile(fileId);
    if (action === "delete-context-attachment") return deleteContextAttachment(refId, contextType, contextId, targetId);
    if (action === "close-preview") return closeDrivePreview();
    if (action === "close-album-full-preview") return closeAlbumFullPreview();
    if (action === "download-storage") return downloadStorageFile(storageFileId);
    if (action === "rename-storage-file") return renameStorageFile(storageFileId, path, name);
    if (action === "select-storage-file") return selectStorageFileForOrganize(storageFileId, path);
    if (action === "select-visible-storage") return selectVisibleStorageItems();
    if (action === "clear-storage-selection") return clearStorageSelection();
    if (action === "trash-selected-storage") return trashSelectedStorageItems();
    if (action === "delete-selected-storage") return deleteSelectedStorageItems();
    if (action === "move-selected-storage") return moveSelectedStorageItems();
    if (action === "share-selected-storage") return shareSelectedStorageItems();
    if (action === "download-selected-storage") return downloadSelectedStorageItems();
    if (action === "set-drive-e2ee-session-passphrase") return setDriveE2eeSessionPassphraseFromUi();
    if (action === "clear-drive-e2ee-session-passphrase") return clearDriveE2eeSessionPassphraseFromUi();
    if (action === "move-storage-file") return moveStorageFileFromRow(storageFileId, path);
    if (action === "open-storage-folder") return openStorageFolder(path);
    if (action === "add-storage-to-album") return addStorageFileToAlbum(storageFileId, name);
    if (action === "trash-storage") return trashStorageFile(storageFileId);
    if (action === "restore-storage") return restoreStorageFile(storageFileId);
    if (action === "purge-storage") return purgeStorageFile(storageFileId);
    if (action === "rename-storage-folder") return renameStorageFolder(path, name);
    if (action === "trash-storage-folder") return trashStorageFolder(path);
    if (action === "share-storage-folder") return shareStorageFolder(path, name);
    if (action === "folder-to-album") return createAlbumFromFolder(path, name);
    if (action === "restore-storage-trash") return restoreStorageTrash();
    if (action === "purge-storage-trash") return purgeStorageTrash();
    if (action === "select-storage-folder") return selectStorageFolderForMove(path);
    if (action === "open-album") return openAlbum(albumId);
    if (action === "share-album") return shareAlbum(albumId);
    if (action === "delete-album") return deleteAlbum(albumId);
    if (action === "close-album-detail") return closeAlbumDetail();
    if (action === "save-album-detail") return saveAlbumDetail();
    if (action === "remove-album-file") return removeAlbumFile(albumId, albumFileId);
    if (action === "open-album-viewer") return openAlbumViewer(albumId);
    if (action === "close-album-viewer") return closeAlbumViewer();
    if (action === "refresh-albums") return loadAlbumGallery();
    if (action === "smart-organize-albums") return smartOrganizeAlbums();
  })().catch((err) => alert(err.message || "操作失敗"));
});

document.addEventListener("dblclick", (event) => {
  const target = event.target;
  if (!target?.closest) return;
  if (target.closest(".drive-file-actions")) return;
  if (target.closest("[data-drive-action]")) return;
  const folderPath = storageFolderRowPathFromEventTarget(target);
  if (!folderPath) return;
  event.preventDefault();
  openStorageFolder(folderPath).catch((err) => alert(err.message || "開啟資料夾失敗"));
});

document.addEventListener("focusin", (event) => {
  if (event.target?.matches?.(ATTACHMENT_FILE_SELECT_IDS.map((id) => `#${id}`).join(","))) {
    ensureAttachmentFileOptionsLoaded().catch(() => {});
  }
});

document.addEventListener("click", (event) => {
  if (event.target?.closest?.("[data-storage-file-select], [data-storage-folder-select]")) {
    event.stopPropagation();
  }
}, true);

document.addEventListener("change", (event) => {
  if (event.target?.id === "drive-upload-privacy-mode") {
    updateDriveE2eePassphraseVisibility();
  }
  if (event.target?.id === "drive-share-scope") {
    updateDriveShareScopeFields();
  }
  const fileSelect = event.target?.closest?.("[data-storage-file-select]");
  if (fileSelect) {
    const id = String(fileSelect.dataset.storageFileSelect || "");
    if (fileSelect.checked) selectedStorageFileIds.add(id);
    else selectedStorageFileIds.delete(id);
    selectedStorageFileId = selectedStorageFileIds.size === 1 ? Array.from(selectedStorageFileIds)[0] : "";
    selectedStorageFilePath = "";
    updateStorageSelectionLabel();
    renderStorageBrowser();
  }
  const folderSelect = event.target?.closest?.("[data-storage-folder-select]");
  if (folderSelect) {
    const folderPath = normalizeStoragePath(folderSelect.dataset.storageFolderSelect || "");
    if (folderSelect.checked) selectedStorageFolderPaths.add(folderPath);
    else selectedStorageFolderPaths.delete(folderPath);
    selectedStorageFileId = selectedStorageFileIds.size === 1 ? Array.from(selectedStorageFileIds)[0] : "";
    selectedStorageFilePath = "";
    updateStorageSelectionLabel();
    renderStorageBrowser();
  }
});

document.addEventListener("keydown", (event) => {
  const overlayOpen = $("album-full-preview-overlay")?.classList.contains("show");
  const docOverlayOpen = $("drive-new-doc-overlay")?.classList.contains("show");
  const e2eePromptOpen = $("drive-e2ee-passphrase-overlay")?.classList.contains("show");
  const shareDialogOpen = $("drive-share-overlay")?.classList.contains("show");
  const uploadModeOpen = $("drive-upload-mode-overlay")?.classList.contains("show");
  const storageUpgradeOpen = $("drive-storage-upgrade-overlay")?.classList.contains("show");
  if (event.key === "Escape" && overlayOpen) {
    closeAlbumFullPreview();
  } else if (event.key === "Escape" && docOverlayOpen) {
    closeDriveTextDocumentModal();
  } else if (event.key === "Escape" && storageUpgradeOpen) {
    closeStorageUpgradePanel();
  } else if (event.key === "Escape" && shareDialogOpen) {
    closeDriveShareDialog();
  } else if (event.key === "Escape" && uploadModeOpen) {
    $("drive-upload-mode-cancel-btn")?.click?.();
  } else if (event.key === "Escape" && e2eePromptOpen) {
    $("drive-e2ee-passphrase-cancel-btn")?.click?.();
  } else if (overlayOpen && event.key === "ArrowLeft") {
    event.preventDefault();
    stepAlbumPreview(-1);
  } else if (overlayOpen && event.key === "ArrowRight") {
    event.preventDefault();
    stepAlbumPreview(1);
  }
});
