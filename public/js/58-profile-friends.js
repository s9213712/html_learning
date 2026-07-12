'use strict';

let currentProfileTab = "home";
let profilePanelCache = null;
let currentProfileViewedUserId = null;
let currentProfileIsViewingSelf = true;
let profileFriendsLoaded = false;
let profileQuickCustomizeOpen = false;
let profileEditFormDirty = false;
let profileAppearancePreviewTimer = 0;
let profileAccountGeneration = 0;
const targetOptionCache = new Map();
const PROFILE_AVATAR_DEFAULT_ZOOM = 1;
const PROFILE_AVATAR_MIN_ZOOM = 0.1;
const PROFILE_AVATAR_MAX_ZOOM = 10;
const PROFILE_AVATAR_ROTATION_STEP = 90;
const PROFILE_AVATAR_MAX_DISPLAY_SIZE = 600;
const PROFILE_AVATAR_MIN_DISPLAY_SIZE = 100;
const PROFILE_TEMPLATE_KEYS = ["classic", "creator", "compact", "showcase", "gallery", "neon"];
const PROFILE_ACCENT_KEYS = ["default", "ocean", "sunrise", "forest", "mono", "violet", "ruby"];
const PROFILE_DENSITY_KEYS = ["comfortable", "compact"];
const PROFILE_STYLE_FIELD_MAP = {
  "profile-edit-banner": "banner",
  "profile-edit-background-tone": "background_tone",
  "profile-edit-avatar-frame": "avatar_frame",
  "profile-edit-avatar-shape": "avatar_shape",
  "profile-edit-avatar-mask": "avatar_mask",
  "profile-edit-avatar-size": "avatar_size",
  "profile-edit-name-font": "name_font",
  "profile-edit-name-size": "name_size",
  "profile-edit-sticker": "sticker",
  "profile-edit-decoration": "decoration",
};
const PROFILE_STYLE_DEFAULTS = {
  banner: "none",
  background_tone: "standard",
  avatar_frame: "soft_ring",
  avatar_shape: "circle",
  avatar_mask: "",
  avatar_size: "140",
  name_font: "system",
  name_size: "large",
  sticker: "none",
  decoration: "minimal",
};
const PROFILE_STYLE_ALLOWED = {
  banner: ["none", "aurora", "neon_grid", "paper", "night_sky", "terminal"],
  background_tone: ["soft", "standard", "bold"],
  avatar_frame: ["none", "soft_ring", "neon", "pixel", "botanical", "crown"],
  avatar_shape: ["circle", "rounded", "squircle", "square", "star", "custom"],
  avatar_size: [...Array.from({ length: 101 }, (_, index) => String(100 + index * 5)), "large", "xl", "hero"],
  name_font: ["system", "rounded", "serif", "mono", "display"],
  name_size: ["normal", "large", "hero"],
  sticker: ["none", "sparkles", "star", "heart", "music", "game", "code", "crown"],
  decoration: ["none", "minimal", "badges", "ribbon", "constellation"],
};
const PROFILE_STICKER_SYMBOLS = {
  sparkles: "✦ ✧",
  star: "★",
  heart: "♥",
  music: "♪",
  game: "GAME",
  code: "</>",
  crown: "♛",
};
const PROFILE_PUBLIC_ACCOUNT_FIELDS = ["nickname", "real_name", "birthdate", "phone"];
const PROFILE_PUBLIC_ACCOUNT_FIELD_LABELS = {
  nickname: "暱稱",
  real_name: "真實姓名",
  birthdate: "生日",
  phone: "電話",
};
const PROFILE_PUBLIC_INFO_MAX_ITEMS = 20;
let profileAvatarCloudFiles = [];
let profileAvatarCloudFilesLoadedAt = 0;
const profileAvatarCropState = {
  objectUrl: "",
  cloudFileId: "",
  cloudFileName: "",
  hasImage: false,
  naturalWidth: 0,
  naturalHeight: 0,
  baseScale: 1,
  zoom: 1,
  rotation: 0,
  offsetX: 0,
  offsetY: 0,
  dragging: false,
  pointerId: null,
  startX: 0,
  startY: 0,
  startOffsetX: 0,
  startOffsetY: 0,
};

function profileOperationContext() {
  return {
    generation: profileAccountGeneration,
    scope: typeof getCurrentAccountStorageScope === "function"
      ? getCurrentAccountStorageScope()
      : String(currentUserId || currentUser || "anonymous"),
  };
}

function profileOperationIsCurrent(operation) {
  if (!operation || Number(operation.generation) !== profileAccountGeneration) return false;
  const scope = typeof getCurrentAccountStorageScope === "function"
    ? getCurrentAccountStorageScope()
    : String(currentUserId || currentUser || "anonymous");
  return operation.scope === scope;
}

function profileAssertOperationCurrent(operation) {
  if (profileOperationIsCurrent(operation)) return;
  const err = new Error("Account context changed");
  err.name = "AbortError";
  throw err;
}

function profileSetMsg(text, bad = false) {
  const el = $("profile-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = text ? "msg show" + (bad ? " err" : " ok") : "msg";
  el.setAttribute("role", bad ? "alert" : "status");
  el.setAttribute("aria-live", bad ? "assertive" : "polite");
  el.setAttribute("aria-atomic", "true");
  if (typeof scheduleInlineMessageClear === "function") scheduleInlineMessageClear(el, text, !bad);
}

function profileConfirm(message, options = {}) {
  if (typeof showAppConfirm === "function") return showAppConfirm(message, options);
  return Promise.resolve(window.confirm(message));
}

async function profileReadJson(url, options = {}) {
  const res = await apiFetch(url, options);
  const json = await res.json().catch(() => ({}));
  if (!res.ok || json.ok === false) {
    const msg = json.msg || json.error || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return json;
}

function profileAvatarSetMsg(text, bad = false) {
  const el = $("profile-avatar-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = text ? "msg show" + (bad ? " err" : " ok") : "msg";
  el.setAttribute("role", bad ? "alert" : "status");
  el.setAttribute("aria-live", bad ? "assertive" : "polite");
  if (text && typeof scheduleInlineMessageClear === "function") {
    scheduleInlineMessageClear(el, text, !bad, { duration: bad ? 4200 : 2200 });
  }
}

function markProfileEditFormDirty() {
  profileEditFormDirty = true;
}

function clearProfileEditFormDirty() {
  profileEditFormDirty = false;
}

function profileAvatarCropperElements() {
  return {
    overlay: $("profile-avatar-overlay"),
    cropper: $("profile-avatar-cropper"),
    stage: $("profile-avatar-crop-stage"),
    image: $("profile-avatar-crop-image"),
    box: $("profile-avatar-crop-box"),
    shape: $("profile-avatar-crop-shape"),
    zoom: $("profile-avatar-crop-zoom"),
    zoomValue: $("profile-avatar-crop-zoom-value"),
    zoomSteps: Array.from(document.querySelectorAll("[data-profile-avatar-zoom-step]")),
    rotation: $("profile-avatar-crop-rotation"),
    rotationValue: $("profile-avatar-crop-rotation-value"),
    rotationSteps: Array.from(document.querySelectorAll("[data-profile-avatar-rotate-step]")),
    center: $("profile-avatar-crop-center"),
    file: $("profile-avatar-file"),
    cloudSelect: $("profile-avatar-cloud-file"),
    cloudGallery: $("profile-avatar-cloud-gallery"),
    cloudRefresh: $("profile-avatar-cloud-refresh"),
    cloudUse: $("profile-avatar-cloud-use"),
  };
}

function profileAvatarClamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function normalizeProfileAvatarZoom(value) {
  const numeric = Number.parseFloat(String(value ?? ""));
  const safe = Number.isFinite(numeric) ? numeric : PROFILE_AVATAR_DEFAULT_ZOOM;
  return profileAvatarClamp(safe, PROFILE_AVATAR_MIN_ZOOM, PROFILE_AVATAR_MAX_ZOOM);
}

function profileAvatarZoomLabel(value) {
  return `${normalizeProfileAvatarZoom(value).toFixed(2)}x`;
}

function normalizeProfileAvatarRotation(value) {
  const numeric = Number.parseInt(String(value ?? ""), 10);
  const safe = Number.isFinite(numeric) ? numeric : 0;
  return ((Math.round(safe / PROFILE_AVATAR_ROTATION_STEP) * PROFILE_AVATAR_ROTATION_STEP) % 360 + 360) % 360;
}

function profileAvatarRotationLabel(value) {
  return `${normalizeProfileAvatarRotation(value)}°`;
}

function syncProfileAvatarZoomControl(value = profileAvatarCropState.zoom) {
  const els = profileAvatarCropperElements();
  const zoom = normalizeProfileAvatarZoom(value);
  profileAvatarCropState.zoom = zoom;
  if (els.zoom) els.zoom.value = String(zoom);
  if (els.zoomValue) els.zoomValue.textContent = profileAvatarZoomLabel(zoom);
  return zoom;
}

function syncProfileAvatarRotationControl(value = profileAvatarCropState.rotation) {
  const els = profileAvatarCropperElements();
  const rotation = normalizeProfileAvatarRotation(value);
  profileAvatarCropState.rotation = rotation;
  if (els.rotation) els.rotation.value = String(rotation);
  if (els.rotationValue) els.rotationValue.textContent = profileAvatarRotationLabel(rotation);
  return rotation;
}

function profileAvatarDisplayNaturalSize() {
  const rotation = normalizeProfileAvatarRotation(profileAvatarCropState.rotation);
  const swapped = rotation === 90 || rotation === 270;
  return {
    width: swapped ? profileAvatarCropState.naturalHeight : profileAvatarCropState.naturalWidth,
    height: swapped ? profileAvatarCropState.naturalWidth : profileAvatarCropState.naturalHeight,
  };
}

function currentProfileAvatarShape() {
  return profileChoice(
    $("profile-avatar-crop-shape")?.value || $("profile-edit-avatar-shape")?.value,
    PROFILE_STYLE_ALLOWED.avatar_shape,
    PROFILE_STYLE_DEFAULTS.avatar_shape
  );
}

function syncProfileAvatarShapeControls(shape = currentProfileAvatarShape(), { preview = true } = {}) {
  const normalized = profileChoice(shape, PROFILE_STYLE_ALLOWED.avatar_shape, PROFILE_STYLE_DEFAULTS.avatar_shape);
  const editShape = $("profile-edit-avatar-shape");
  const cropShape = $("profile-avatar-crop-shape");
  if (editShape && editShape.value !== normalized) editShape.value = normalized;
  if (cropShape && cropShape.value !== normalized) cropShape.value = normalized;
  const { box } = profileAvatarCropperElements();
  if (box) {
    box.classList.remove(...PROFILE_STYLE_ALLOWED.avatar_shape.map((key) => `avatar-crop-shape-${key}`));
    box.classList.add(`avatar-crop-shape-${normalized}`);
    const mask = normalizeProfileAvatarMask($("profile-edit-avatar-mask")?.value || "");
    if (normalized === "custom" && mask) box.style.setProperty("--profile-avatar-mask", mask);
    else box.style.removeProperty("--profile-avatar-mask");
  }
  syncProfileAvatarMaskField(normalized);
  if (preview) previewProfileAppearanceFromForm();
  return normalized;
}

function setProfileAvatarCropHidden(crop = {}) {
  const fields = {
    "profile-avatar-crop-x": crop.x || 0,
    "profile-avatar-crop-y": crop.y || 0,
    "profile-avatar-crop-width": crop.width || 0,
    "profile-avatar-crop-height": crop.height || 0,
    "profile-avatar-crop-rotation": normalizeProfileAvatarRotation(crop.rotation ?? profileAvatarCropState.rotation),
  };
  Object.entries(fields).forEach(([id, value]) => {
    const el = $(id);
    if (el) el.value = String(value);
  });
}

function resetProfileAvatarCropper({ keepFile = false } = {}) {
  const els = profileAvatarCropperElements();
  if (profileAvatarCropState.objectUrl) URL.revokeObjectURL(profileAvatarCropState.objectUrl);
  profileAvatarCropState.objectUrl = "";
  profileAvatarCropState.cloudFileId = "";
  profileAvatarCropState.cloudFileName = "";
  profileAvatarCropState.hasImage = false;
  profileAvatarCropState.naturalWidth = 0;
  profileAvatarCropState.naturalHeight = 0;
  profileAvatarCropState.baseScale = 1;
  profileAvatarCropState.zoom = 1;
  profileAvatarCropState.rotation = 0;
  profileAvatarCropState.offsetX = 0;
  profileAvatarCropState.offsetY = 0;
  profileAvatarCropState.dragging = false;
  profileAvatarCropState.pointerId = null;
  if (!keepFile && els.file) els.file.value = "";
  if (els.cropper) els.cropper.hidden = true;
  if (els.image) {
    els.image.removeAttribute("src");
    els.image.style.left = "";
    els.image.style.top = "";
    els.image.style.width = "";
    els.image.style.height = "";
    els.image.style.transform = "";
  }
  if (els.stage) els.stage.classList.remove("is-dragging");
  syncProfileAvatarZoomControl(PROFILE_AVATAR_DEFAULT_ZOOM);
  syncProfileAvatarRotationControl(0);
  syncProfileAvatarShapeControls(currentProfileAvatarShape(), { preview: false });
  setProfileAvatarCropHidden();
}

function profileAvatarStageMetrics() {
  const { stage } = profileAvatarCropperElements();
  if (!stage) return null;
  const rect = stage.getBoundingClientRect();
  const width = Math.max(0, rect.width || 0);
  const height = Math.max(0, rect.height || 0);
  if (!width || !height) return null;
  const minDimension = Math.min(width, height);
  const cropSize = Math.min(
    Math.max(96, minDimension - 18),
    Math.max(160, minDimension * 0.9)
  );
  return {
    width,
    height,
    cropSize,
    cropLeft: (width - cropSize) / 2,
    cropTop: (height - cropSize) / 2,
  };
}

function clampProfileAvatarOffsets(metrics = profileAvatarStageMetrics()) {
  if (!metrics || !profileAvatarCropState.hasImage) return;
  const scale = profileAvatarCropState.baseScale * normalizeProfileAvatarZoom(profileAvatarCropState.zoom);
  const displaySize = profileAvatarDisplayNaturalSize();
  const imageWidth = displaySize.width * scale;
  const imageHeight = displaySize.height * scale;
  const maxX = Math.max(0, (imageWidth - metrics.cropSize) / 2);
  const maxY = Math.max(0, (imageHeight - metrics.cropSize) / 2);
  profileAvatarCropState.offsetX = profileAvatarClamp(profileAvatarCropState.offsetX, -maxX, maxX);
  profileAvatarCropState.offsetY = profileAvatarClamp(profileAvatarCropState.offsetY, -maxY, maxY);
}

function currentProfileAvatarCropPayload() {
  const metrics = profileAvatarStageMetrics();
  if (!metrics || !profileAvatarCropState.hasImage || !profileAvatarCropState.naturalWidth || !profileAvatarCropState.naturalHeight) {
    return {
      x: parseInt($("profile-avatar-crop-x")?.value || "0", 10) || 0,
      y: parseInt($("profile-avatar-crop-y")?.value || "0", 10) || 0,
      width: parseInt($("profile-avatar-crop-width")?.value || "0", 10) || 0,
      height: parseInt($("profile-avatar-crop-height")?.value || "0", 10) || 0,
    };
  }
  const rotation = syncProfileAvatarRotationControl(profileAvatarCropState.rotation);
  const displaySize = profileAvatarDisplayNaturalSize();
  const scale = profileAvatarCropState.baseScale * normalizeProfileAvatarZoom(profileAvatarCropState.zoom);
  const imageWidth = displaySize.width * scale;
  const imageHeight = displaySize.height * scale;
  const imageLeft = (metrics.width / 2 + profileAvatarCropState.offsetX) - imageWidth / 2;
  const imageTop = (metrics.height / 2 + profileAvatarCropState.offsetY) - imageHeight / 2;
  let cropX = Math.round((metrics.cropLeft - imageLeft) / scale);
  let cropY = Math.round((metrics.cropTop - imageTop) / scale);
  let cropSide = Math.round(metrics.cropSize / scale);
  cropSide = profileAvatarClamp(cropSide, 1, Math.min(displaySize.width, displaySize.height));
  cropX = profileAvatarClamp(cropX, 0, Math.max(0, displaySize.width - cropSide));
  cropY = profileAvatarClamp(cropY, 0, Math.max(0, displaySize.height - cropSide));
  const crop = { x: cropX, y: cropY, width: cropSide, height: cropSide, rotation };
  setProfileAvatarCropHidden(crop);
  return crop;
}

function renderProfileAvatarCropper() {
  const els = profileAvatarCropperElements();
  const zoom = syncProfileAvatarZoomControl(profileAvatarCropState.zoom);
  const rotation = syncProfileAvatarRotationControl(profileAvatarCropState.rotation);
  const metrics = profileAvatarStageMetrics();
  if (!els.image || !els.box || !metrics || !profileAvatarCropState.hasImage) return;
  const displaySize = profileAvatarDisplayNaturalSize();
  profileAvatarCropState.baseScale = Math.max(
    metrics.width / displaySize.width,
    metrics.height / displaySize.height
  );
  clampProfileAvatarOffsets(metrics);
  const scale = profileAvatarCropState.baseScale * zoom;
  const rawWidth = profileAvatarCropState.naturalWidth * scale;
  const rawHeight = profileAvatarCropState.naturalHeight * scale;
  els.image.style.width = `${rawWidth}px`;
  els.image.style.height = `${rawHeight}px`;
  els.image.style.left = `${(metrics.width / 2 + profileAvatarCropState.offsetX) - rawWidth / 2}px`;
  els.image.style.top = `${(metrics.height / 2 + profileAvatarCropState.offsetY) - rawHeight / 2}px`;
  els.image.style.transform = rotation ? `rotate(${rotation}deg)` : "none";
  els.box.style.width = `${metrics.cropSize}px`;
  els.box.style.height = `${metrics.cropSize}px`;
  els.box.style.left = `${metrics.cropLeft}px`;
  els.box.style.top = `${metrics.cropTop}px`;
  syncProfileAvatarShapeControls(currentProfileAvatarShape(), { preview: false });
  currentProfileAvatarCropPayload();
}

function loadProfileAvatarFile(file) {
  if (!file) {
    resetProfileAvatarCropper({ keepFile: true });
    return;
  }
  if (!/^image\/(png|jpe?g|gif)$/i.test(file.type || "")) {
    resetProfileAvatarCropper();
    profileAvatarSetMsg("頭像僅支援 JPEG / PNG / GIF", true);
    return;
  }
  const els = profileAvatarCropperElements();
  if (!els.cropper || !els.image) return;
  profileAvatarCropState.cloudFileId = "";
  profileAvatarCropState.cloudFileName = "";
  const nextUrl = URL.createObjectURL(file);
  loadProfileAvatarObjectUrl(nextUrl);
}

function loadProfileAvatarObjectUrl(nextUrl, cloudFile = null, operation = profileOperationContext()) {
  profileAssertOperationCurrent(operation);
  const els = profileAvatarCropperElements();
  if (!els.cropper || !els.image || !nextUrl) return;
  if (profileAvatarCropState.objectUrl) URL.revokeObjectURL(profileAvatarCropState.objectUrl);
  profileAvatarCropState.objectUrl = nextUrl;
  profileAvatarCropState.cloudFileId = cloudFile ? String(cloudFile.id || cloudFile.file_id || "") : "";
  profileAvatarCropState.cloudFileName = cloudFile ? profileAvatarCloudFileName(cloudFile) : "";
  profileAvatarCropState.hasImage = false;
  els.image.onload = () => {
    if (
      !profileOperationIsCurrent(operation)
      || profileAvatarCropState.objectUrl !== nextUrl
    ) return;
    profileAvatarCropState.naturalWidth = els.image.naturalWidth || 1;
    profileAvatarCropState.naturalHeight = els.image.naturalHeight || 1;
    profileAvatarCropState.zoom = 1;
    profileAvatarCropState.rotation = 0;
    profileAvatarCropState.offsetX = 0;
    profileAvatarCropState.offsetY = 0;
    profileAvatarCropState.hasImage = true;
    syncProfileAvatarZoomControl(PROFILE_AVATAR_DEFAULT_ZOOM);
    els.cropper.hidden = false;
    requestAnimationFrame(() => {
      if (
        profileOperationIsCurrent(operation)
        && profileAvatarCropState.objectUrl === nextUrl
      ) renderProfileAvatarCropper();
    });
  };
  els.image.onerror = () => {
    if (
      !profileOperationIsCurrent(operation)
      || profileAvatarCropState.objectUrl !== nextUrl
    ) return;
    resetProfileAvatarCropper();
    const name = cloudFile ? profileAvatarCloudFileName(cloudFile) : ($("profile-avatar-file")?.files?.[0]?.name || "這張圖片");
    profileAvatarSetMsg(`無法讀取頭像預覽：${name}。請確認檔案未損壞，或改用 JPEG / PNG。`, true);
  };
  els.image.src = nextUrl;
}

function profileAvatarCloudFileName(file) {
  const displayName = String(file?.display_name || file?.storage_display_name || "").trim();
  const virtualPath = String(file?.virtual_path || file?.storage_virtual_path || "").trim();
  const virtualName = virtualPath.split("/").filter(Boolean).pop() || "";
  const originalName = String(file?.original_filename_plain_for_public || file?.filename || "").trim();
  return String(displayName || virtualName || originalName || file?.file_id || file?.id || "雲端圖片");
}

function profileAvatarCloudFileIsUsable(file) {
  const mime = String(file?.mime_type_plain_for_public || file?.mime_type || "").toLowerCase();
  const name = profileAvatarCloudFileName(file).toLowerCase();
  const privacyMode = String(file?.privacy_mode || "standard_plain");
  const scanStatus = String(file?.scan_status || "");
  const imageLike = ["image/jpeg", "image/png", "image/gif"].includes(mime) || /\.(png|jpe?g|gif)$/.test(name);
  if (!imageLike) return false;
  if (privacyMode === "e2ee") return ["skipped_e2ee", "unknown_encrypted", "not_required", ""].includes(scanStatus);
  return ["standard_plain", "server_encrypted"].includes(privacyMode) && ["clean", "not_required"].includes(scanStatus);
}

function profileAvatarCloudSizeText(file) {
  if (typeof formatDriveBytes === "function") return formatDriveBytes(file?.size_bytes || 0);
  const bytes = Number(file?.size_bytes || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${Math.round(bytes)} B`;
}

function profileAvatarCloudPrivacyLabel(file) {
  const mode = String(file?.privacy_mode || "standard_plain");
  if (mode === "e2ee") return "E2EE 圖片，點選後在瀏覽器解密";
  if (mode === "server_encrypted") return "伺服器加密圖片";
  return "一般圖片";
}

function profileAvatarCloudPreviewUrl(file) {
  if (String(file?.privacy_mode || "") === "e2ee") return "";
  const id = String(file?.id || file?.file_id || "");
  return id ? `${API}/cloud-drive/files/${encodeURIComponent(id)}/preview/content` : "";
}

function selectProfileAvatarCloudFile(fileId, { loadPreview = false } = {}) {
  const selectedId = String(fileId || "");
  const { cloudSelect, cloudGallery } = profileAvatarCropperElements();
  if (cloudSelect && cloudSelect.value !== selectedId) cloudSelect.value = selectedId;
  if (cloudGallery) {
    cloudGallery.querySelectorAll("[data-profile-cloud-avatar-id]").forEach((card) => {
      const active = String(card.dataset.profileCloudAvatarId || "") === selectedId;
      card.classList.toggle("active", active);
      card.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }
  if (loadPreview) useSelectedProfileCloudAvatar();
}

function renderProfileAvatarCloudOptions() {
  const { cloudSelect, cloudGallery } = profileAvatarCropperElements();
  if (!cloudSelect && !cloudGallery) return;
  const usable = profileAvatarCloudFiles.filter(profileAvatarCloudFileIsUsable);
  if (!usable.length) {
    if (cloudSelect) cloudSelect.innerHTML = `<option value="">目前沒有可用的雲端圖片</option>`;
    if (cloudGallery) cloudGallery.innerHTML = `<div class="drive-empty profile-avatar-cloud-empty">目前沒有可用的雲端圖片</div>`;
    return;
  }
  const selectedId = String(cloudSelect?.value || "");
  if (cloudSelect) cloudSelect.innerHTML = `<option value="">請選擇雲端圖片</option>` + usable.map((file) => {
    const id = String(file.id || file.file_id || "");
    const name = `${profileAvatarCloudFileName(file)} · ${profileAvatarCloudSizeText(file)}`;
    return `<option value="${sanitize(id)}">${sanitize(name)}</option>`;
  }).join("");
  if (cloudGallery) {
    cloudGallery.innerHTML = usable.map((file) => {
      const id = String(file.id || file.file_id || "");
      const name = profileAvatarCloudFileName(file);
      const previewUrl = profileAvatarCloudPreviewUrl(file);
      const active = id && id === selectedId;
      const thumb = previewUrl
        ? `<img src="${sanitize(previewUrl)}" alt="${sanitize(name)} 預覽" loading="lazy" />`
        : `<span class="profile-avatar-cloud-thumb-fallback">E2EE</span>`;
      return `
        <button class="profile-avatar-cloud-card${active ? " active" : ""}" type="button" data-profile-cloud-avatar-id="${sanitize(id)}" aria-pressed="${active ? "true" : "false"}" title="使用 ${sanitize(name)} 作為頭像來源">
          <span class="profile-avatar-cloud-thumb">${thumb}</span>
          <span class="profile-avatar-cloud-card-main">
            <strong>${sanitize(name)}</strong>
            <span>${sanitize(profileAvatarCloudSizeText(file))} · ${sanitize(profileAvatarCloudPrivacyLabel(file))}</span>
          </span>
        </button>
      `;
    }).join("");
  }
}

async function loadProfileAvatarCloudFiles({ force = false } = {}) {
  const operation = profileOperationContext();
  const { cloudSelect, cloudGallery } = profileAvatarCropperElements();
  if (!currentUserId) return [];
  const fresh = profileAvatarCloudFilesLoadedAt && Date.now() - profileAvatarCloudFilesLoadedAt < 30000;
  if (!force && fresh) {
    renderProfileAvatarCloudOptions();
    return profileAvatarCloudFiles;
  }
  if (cloudSelect) cloudSelect.innerHTML = `<option value="">讀取雲端圖片中...</option>`;
  if (cloudGallery) cloudGallery.innerHTML = `<div class="drive-empty profile-avatar-cloud-empty">讀取雲端圖片中...</div>`;
  try {
    await fetchCsrfToken({ force: true });
    profileAssertOperationCurrent(operation);
    const csrf = getCsrfToken();
    const res = await apiFetch(API + `/cloud-drive/files?user_id=${encodeURIComponent(currentUserId)}`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await res.json().catch(() => ({}));
    profileAssertOperationCurrent(operation);
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    profileAvatarCloudFiles = Array.isArray(json.files) ? json.files : [];
    profileAvatarCloudFilesLoadedAt = Date.now();
    renderProfileAvatarCloudOptions();
    return profileAvatarCloudFiles;
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return [];
    profileAvatarCloudFiles = [];
    profileAvatarCloudFilesLoadedAt = 0;
    if (cloudSelect) cloudSelect.innerHTML = `<option value="">雲端圖片讀取失敗</option>`;
    if (cloudGallery) cloudGallery.innerHTML = `<div class="drive-empty profile-avatar-cloud-empty">雲端圖片讀取失敗</div>`;
    profileAvatarSetMsg(err.message || "雲端圖片讀取失敗", true);
    return [];
  }
}

async function useSelectedProfileCloudAvatar() {
  const operation = profileOperationContext();
  const { cloudSelect, file } = profileAvatarCropperElements();
  const selectedId = String(cloudSelect?.value || "");
  if (!selectedId) {
    profileAvatarSetMsg("請先選擇雲端圖片", true);
    return;
  }
  const cloudFile = profileAvatarCloudFiles.find((item) => String(item.id || item.file_id || "") === selectedId);
  if (!cloudFile || !profileAvatarCloudFileIsUsable(cloudFile)) {
    profileAvatarSetMsg("這個雲端檔案不能作為公開頭像", true);
    return;
  }
  try {
    await fetchCsrfToken({ force: true });
    profileAssertOperationCurrent(operation);
    const csrf = getCsrfToken();
    let blob = null;
    if (String(cloudFile.privacy_mode || "") === "e2ee") {
      if (typeof decryptDriveE2eeFileForSession !== "function") {
        throw new Error("此頁面尚未載入 E2EE 瀏覽器解密模組，請重新整理後再試");
      }
      const decrypted = await decryptDriveE2eeFileForSession(
        selectedId,
        csrf,
        "請輸入此 E2EE 圖片的加密密碼。密碼只在瀏覽器端用來解密頭像來源。",
        { promptOnMiss: true }
      );
      profileAssertOperationCurrent(operation);
      if (!decrypted?.blob) return;
      blob = decrypted.blob;
    } else {
      const res = await apiFetch(API + `/cloud-drive/files/${encodeURIComponent(selectedId)}/preview/content`, {
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf || "" },
      });
      if (!res.ok) {
        const json = await res.json().catch(() => ({}));
        throw new Error(json.msg || `HTTP ${res.status}`);
      }
      blob = await res.blob();
      profileAssertOperationCurrent(operation);
    }
    const mime = String(blob.type || cloudFile.mime_type_plain_for_public || "").toLowerCase();
    if (!/^image\/(png|jpe?g|gif)$/i.test(mime)) throw new Error("雲端檔案不是可用的頭像圖片");
    if (file) file.value = "";
    profileAssertOperationCurrent(operation);
    loadProfileAvatarObjectUrl(URL.createObjectURL(blob), cloudFile, operation);
    profileAvatarSetMsg(`已載入雲端圖片：${profileAvatarCloudFileName(cloudFile)}`);
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileAvatarSetMsg(err.message || "雲端圖片載入失敗", true);
  }
}

function closeProfileAvatarUploader() {
  const { overlay } = profileAvatarCropperElements();
  if (overlay) {
    overlay.classList.remove("show");
    overlay.setAttribute("aria-hidden", "true");
  }
  resetProfileAvatarCropper();
  profileAvatarSetMsg("");
}

function openProfileAvatarUploader({ pickFile = false } = {}) {
  const els = profileAvatarCropperElements();
  if (!els.overlay) return;
  resetProfileAvatarCropper();
  syncProfileAvatarShapeControls($("profile-edit-avatar-shape")?.value || currentProfileAvatarShape(), { preview: false });
  profileAvatarSetMsg("");
  els.overlay.classList.add("show");
  els.overlay.setAttribute("aria-hidden", "false");
  loadProfileAvatarCloudFiles().catch((err) => {
    reportFrontendFailure("profile-avatar-cloud-files", err);
    profileAvatarSetMsg(err.message || "雲端圖片讀取失敗", true);
  });
  if (pickFile && els.file && typeof els.file.click === "function") {
    els.file.click();
  } else if (els.file) {
    els.file.focus();
  }
}

function handleProfileAvatarPointerStart(event) {
  const { stage } = profileAvatarCropperElements();
  if (!stage || !profileAvatarCropState.hasImage) return;
  event.preventDefault();
  profileAvatarCropState.dragging = true;
  profileAvatarCropState.pointerId = event.pointerId;
  profileAvatarCropState.startX = event.clientX;
  profileAvatarCropState.startY = event.clientY;
  profileAvatarCropState.startOffsetX = profileAvatarCropState.offsetX;
  profileAvatarCropState.startOffsetY = profileAvatarCropState.offsetY;
  stage.classList.add("is-dragging");
  if (typeof stage.setPointerCapture === "function") stage.setPointerCapture(event.pointerId);
}

function handleProfileAvatarPointerMove(event) {
  if (!profileAvatarCropState.dragging || event.pointerId !== profileAvatarCropState.pointerId) return;
  event.preventDefault();
  profileAvatarCropState.offsetX = profileAvatarCropState.startOffsetX + (event.clientX - profileAvatarCropState.startX);
  profileAvatarCropState.offsetY = profileAvatarCropState.startOffsetY + (event.clientY - profileAvatarCropState.startY);
  renderProfileAvatarCropper();
}

function handleProfileAvatarPointerEnd(event) {
  const { stage } = profileAvatarCropperElements();
  if (!profileAvatarCropState.dragging || event.pointerId !== profileAvatarCropState.pointerId) return;
  profileAvatarCropState.dragging = false;
  profileAvatarCropState.pointerId = null;
  if (stage) stage.classList.remove("is-dragging");
}

async function uploadProfileAvatar() {
  const operation = profileOperationContext();
  const targetUserId = currentUserId;
  const file = $("profile-avatar-file")?.files?.[0] || null;
  const cloudFileId = profileAvatarCropState.cloudFileId || "";
  if (!currentUserId) {
    profileAvatarSetMsg("尚未登入，無法更新頭像", true);
    return;
  }
  if (!file && !cloudFileId) {
    profileAvatarSetMsg("請先選擇頭像圖片，或從雲端硬碟選一張圖片", true);
    return;
  }
  if ((file || cloudFileId) && !profileAvatarCropState.hasImage) {
    profileAvatarSetMsg("請等待頭像裁切預覽載入完成後再上傳", true);
    return;
  }
  const button = $("profile-avatar-upload-btn");
  const previousText = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "上傳中...";
  }
  try {
    const crop = currentProfileAvatarCropPayload();
    const form = new FormData();
    const image = profileAvatarCropperElements().image;
    const cropped = profileAvatarCropState.hasImage && typeof buildCroppedAvatarUpload === "function"
      ? await buildCroppedAvatarUpload(image, crop, {
          sourceName: file?.name || profileAvatarCropState.cloudFileName || "avatar.png",
          rotation: profileAvatarCropState.rotation,
        })
      : null;
    profileAssertOperationCurrent(operation);
    if (!cropped) {
      throw new Error("無法產生裁切後頭像，請重新選擇圖片");
    }
    if (cropped) {
      form.append("file", cropped.blob, cropped.filename);
      form.append("crop_json", JSON.stringify(cropped.serverCrop));
      form.append("avatar_client_cropped", "1");
    } else {
      if (file) form.append("file", file);
      else form.append("cloud_file_id", cloudFileId);
      form.append("crop_json", JSON.stringify(crop));
    }
    await fetchCsrfToken({ force: true });
    profileAssertOperationCurrent(operation);
    const csrf = getCsrfToken();
    const res = await apiFetch(API + `/admin/users/${encodeURIComponent(targetUserId)}/avatar`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
      body: form,
    });
    const json = await res.json().catch(() => ({}));
    profileAssertOperationCurrent(operation);
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    if (profilePanelCache) profilePanelCache.avatar_file_id = json.avatar_file_id || "";
    if (typeof markUserAvatarUpdated === "function") markUserAvatarUpdated(targetUserId, json.avatar_file_id || "");
    profileAvatarSetMsg("頭像已更新");
    await loadMyProfile({ quiet: true });
    profileAssertOperationCurrent(operation);
    closeProfileAvatarUploader();
    profileSetMsg("頭像已更新");
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileAvatarSetMsg(err.message || "頭像上傳失敗", true);
  } finally {
    if (button && profileOperationIsCurrent(operation)) {
      button.disabled = false;
      button.textContent = previousText || "上傳頭像";
    }
  }
}

function profileFriendStatusLabel(status) {
  return {
    self: "本人",
    none: "尚未成為好友",
    pending_outgoing: "等待對方同意",
    pending_incoming: "收到好友申請",
    accepted: "已成為好友",
    rejected: "已拒絕",
    blocked: "已封鎖",
    blocked_by_them: "無法互動",
    anonymous: "未登入",
  }[status] || status || "-";
}

function renderProfileAvatar(targetId, profile, { editable = true } = {}) {
  const el = $(targetId);
  if (!el) return;
  el.innerHTML = userAvatarInnerMarkup(profile?.id, profile?.username || "", profile?.avatar_file_id || "");
  if (targetId === "profile-home-avatar") {
    el.title = editable ? "點擊上傳或更換頭像" : "點擊放大預覽頭像";
    el.setAttribute("aria-label", editable ? "上傳或更換頭像" : "放大預覽頭像");
    el.classList.toggle("profile-avatar-upload-button", !!editable);
    el.classList.toggle("profile-avatar-preview-button", !editable);
  }
  bindAvatarFallbacks(el);
}

function ensureProfileAvatarPreviewOverlay() {
  let overlay = $("profile-avatar-preview-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "profile-avatar-preview-overlay";
  overlay.className = "user-edit-overlay profile-avatar-preview-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "profile-avatar-preview-title");
  overlay.innerHTML = `
    <div class="user-edit-modal profile-avatar-preview-modal">
      <div class="drive-card-heading compact-heading">
        <div>
          <div class="mini-title" id="profile-avatar-preview-title">頭像預覽</div>
          <div class="drive-card-sub" id="profile-avatar-preview-subtitle"></div>
        </div>
        <button type="button" class="btn btn-sm" id="profile-avatar-preview-close">關閉</button>
      </div>
      <div class="profile-avatar-preview-frame" id="profile-avatar-preview-frame"></div>
    </div>
  `;
  document.body.appendChild(overlay);
  $("profile-avatar-preview-close")?.addEventListener("click", closeProfileAvatarPreview);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeProfileAvatarPreview();
  });
  return overlay;
}

function openProfileAvatarPreview(profile = profilePanelCache) {
  if (!profile || currentProfileIsViewingSelf) return;
  const overlay = ensureProfileAvatarPreviewOverlay();
  const title = profile.display_name || profile.username || "使用者";
  const subtitle = $("profile-avatar-preview-subtitle");
  const frame = $("profile-avatar-preview-frame");
  if (subtitle) subtitle.textContent = `@${profile.username || "-"}`;
  if (frame) {
    const url = avatarUrlForUser(profile.id, profile.avatar_file_id || "");
    frame.innerHTML = url
      ? `<img src="${sanitize(url)}" alt="${sanitize(title)} 的頭像" />`
      : `<div class="profile-avatar-preview-fallback">${sanitize(avatarInitial(profile.username || title))}</div>`;
    bindAvatarFallbacks(frame);
  }
  overlay.classList.add("show");
  document.body.classList.add("modal-open");
}

function closeProfileAvatarPreview() {
  const overlay = $("profile-avatar-preview-overlay");
  if (!overlay) return;
  overlay.classList.remove("show");
  document.body.classList.remove("modal-open");
}

function profileChoice(value, allowed, fallback) {
  const normalized = String(value || fallback || "").trim().toLowerCase();
  return Array.isArray(allowed) && allowed.includes(normalized) ? normalized : fallback;
}

function normalizeProfileAvatarMask(value) {
  const text = String(value || "").trim();
  if (!text || text.length > 240) return "";
  const lower = text.toLowerCase();
  if (["url(", "var(", "@import", ";", "{", "}", "<", ">"].some((token) => lower.includes(token))) return "";
  if (!["polygon(", "circle(", "ellipse(", "inset(", "path("].some((prefix) => lower.startsWith(prefix))) return "";
  if ((text.match(/\(/g) || []).length !== (text.match(/\)/g) || []).length) return "";
  return /^[\w\s%#.,()+\-'"/_-]+$/u.test(text) ? text : "";
}

function syncProfileAvatarMaskField(shape = currentProfileAvatarShape()) {
  const field = $("profile-edit-avatar-mask-field");
  const input = $("profile-edit-avatar-mask");
  const isCustom = shape === "custom";
  if (field) field.hidden = !isCustom;
  if (input) input.disabled = !isCustom;
}

function profileAvatarSizeValue(value) {
  const legacy = {
    large: "140",
    xl: "180",
    hero: "220",
  };
  const raw = String(value || "").trim().toLowerCase();
  const mapped = legacy[raw] || raw || PROFILE_STYLE_DEFAULTS.avatar_size;
  let numeric = Math.round((Number(mapped) || Number(PROFILE_STYLE_DEFAULTS.avatar_size)) / 5) * 5;
  if (!legacy[raw] && numeric > 0 && numeric <= 90) {
    numeric = Math.round((numeric * 2) / 5) * 5;
  }
  return String(Math.max(PROFILE_AVATAR_MIN_DISPLAY_SIZE, Math.min(PROFILE_AVATAR_MAX_DISPLAY_SIZE, numeric)));
}

function applyProfileAvatarElementSize(avatar, value) {
  if (!avatar) return "";
  const size = profileAvatarSizeValue(value);
  const px = `${size}px`;
  const numericSize = Number(size) || Number(PROFILE_STYLE_DEFAULTS.avatar_size);
  const textScale = profileAvatarClamp(numericSize / Number(PROFILE_STYLE_DEFAULTS.avatar_size), 1, 2.4);
  const mobileTextScale = profileAvatarClamp(Math.min(numericSize, 320) / Number(PROFILE_STYLE_DEFAULTS.avatar_size), 1, 2.15);
  const summary = avatar.closest?.(".profile-summary");
  if (summary) {
    summary.style.setProperty("--profile-avatar-size", px);
    summary.style.setProperty("--profile-avatar-text-scale", textScale.toFixed(3));
    summary.style.setProperty("--profile-avatar-mobile-text-scale", mobileTextScale.toFixed(3));
  }
  avatar.style.setProperty("--profile-avatar-custom-size", px);
  avatar.style.setProperty("--profile-avatar-size", px);
  avatar.style.width = px;
  avatar.style.height = px;
  avatar.style.inlineSize = px;
  avatar.style.blockSize = px;
  avatar.style.minWidth = px;
  avatar.style.minHeight = px;
  avatar.style.minInlineSize = px;
  avatar.style.minBlockSize = px;
  avatar.style.maxWidth = px;
  avatar.style.maxHeight = px;
  avatar.style.maxInlineSize = px;
  avatar.style.maxBlockSize = px;
  avatar.style.flexBasis = px;
  avatar.dataset.avatarSize = size;
  return size;
}

function updateProfileAvatarSizeControl(value) {
  const normalized = profileAvatarSizeValue(value);
  const input = $("profile-edit-avatar-size");
  const output = $("profile-edit-avatar-size-value");
  if (input) input.value = normalized;
  if (output) output.textContent = `${normalized}px`;
  document.querySelectorAll("[data-profile-avatar-size]").forEach((button) => {
    button.classList.toggle("btn-primary", profileAvatarSizeValue(button.dataset.profileAvatarSize) === normalized);
    button.setAttribute("aria-pressed", profileAvatarSizeValue(button.dataset.profileAvatarSize) === normalized ? "true" : "false");
  });
  if (typeof window.applyLiveProfileAvatarSize === "function") {
    window.applyLiveProfileAvatarSize(normalized, { dispatch: false });
  }
}

function profileStyleFromProfile(profile = {}) {
  const raw = profile?.profile_style && typeof profile.profile_style === "object" ? profile.profile_style : {};
  const style = {};
  Object.entries(PROFILE_STYLE_DEFAULTS).forEach(([key, fallback]) => {
    style[key] = key === "avatar_mask"
      ? normalizeProfileAvatarMask(raw[key])
      : profileChoice(raw[key], PROFILE_STYLE_ALLOWED[key], fallback);
  });
  return style;
}

function collectProfileStyleFromForm() {
  const style = {};
  Object.entries(PROFILE_STYLE_FIELD_MAP).forEach(([id, key]) => {
    if (key === "avatar_mask") {
      style[key] = normalizeProfileAvatarMask($(id)?.value || "");
      return;
    }
    const raw = id === "profile-edit-avatar-size" ? profileAvatarSizeValue($(id)?.value) : $(id)?.value;
    style[key] = profileChoice(raw, PROFILE_STYLE_ALLOWED[key], PROFILE_STYLE_DEFAULTS[key]);
  });
  if (style.avatar_shape !== "custom") style.avatar_mask = "";
  return style;
}

function draftProfileFromAppearanceForm() {
  const base = profilePanelCache && typeof profilePanelCache === "object" ? { ...profilePanelCache } : {};
  return {
    ...base,
    profile_template: profileChoice($("profile-edit-template")?.value, PROFILE_TEMPLATE_KEYS, "classic"),
    profile_accent: profileChoice($("profile-edit-accent")?.value, PROFILE_ACCENT_KEYS, "default"),
    profile_density: profileChoice($("profile-edit-density")?.value, PROFILE_DENSITY_KEYS, "comfortable"),
    profile_style: collectProfileStyleFromForm(),
  };
}

function previewProfileAppearanceFromForm() {
  if (!currentProfileIsViewingSelf) return;
  const draft = draftProfileFromAppearanceForm();
  syncProfileAvatarShapeControls(draft.profile_style?.avatar_shape, { preview: false });
  if (profileAvatarCropState.hasImage) renderProfileAvatarCropper();
  renderProfileHome(draft);
  const quick = $("profile-quick-customize-card");
  if (quick) quick.open = true;
  updateProfileAvatarSizeControl(draft.profile_style?.avatar_size);
  const signal = $("profile-appearance-preview-signal");
  if (signal) {
    const style = draft.profile_style || {};
    signal.hidden = false;
    signal.textContent = `預覽中：頭像 ${profileAvatarSizeValue(style.avatar_size)}px · ${style.avatar_shape || "circle"} · ${draft.profile_template || "classic"} · ${draft.profile_accent || "default"} · ${style.banner || "none"}`;
  }
  const summary = $("profile-pane-home")?.querySelector(".profile-summary");
  if (summary) {
    summary.classList.add("profile-appearance-previewing");
    window.clearTimeout(profileAppearancePreviewTimer);
    profileAppearancePreviewTimer = window.setTimeout(() => {
      summary.classList.remove("profile-appearance-previewing");
    }, 1200);
  }
  profileQuickCustomizeOpen = true;
  if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
}

function setProfileQuickCustomizeOpen(open, { focus = false } = {}) {
  const quick = $("profile-quick-customize-card");
  profileQuickCustomizeOpen = !!open && currentProfileIsViewingSelf;
  if (!quick) return;
  quick.hidden = !currentProfileIsViewingSelf;
  quick.open = profileQuickCustomizeOpen;
  if (focus && profileQuickCustomizeOpen) {
    requestAnimationFrame(() => quick.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  }
}

function updateProfileTabVisibility() {
  const selfOnlyTabs = new Set(["edit", "friends"]);
  document.querySelectorAll("[data-profile-tab]").forEach((btn) => {
    const tab = btn.dataset.profileTab || "home";
    const hidden = selfOnlyTabs.has(tab) && !currentProfileIsViewingSelf;
    btn.hidden = hidden;
    btn.style.display = hidden ? "none" : "";
    btn.setAttribute("aria-hidden", hidden ? "true" : "false");
    btn.tabIndex = hidden ? -1 : 0;
    if (tab === "home") {
      btn.textContent = currentProfileIsViewingSelf ? "我的主頁" : "使用者主頁";
    }
  });
  if (!currentProfileIsViewingSelf && selfOnlyTabs.has(currentProfileTab)) {
    currentProfileTab = "home";
  }
  if (!currentProfileIsViewingSelf) {
    ["home", "edit", "friends"].forEach((key) => {
      const pane = $("profile-pane-" + key);
      if (pane) pane.classList.toggle("active", key === "home");
    });
  }
  setProfileQuickCustomizeOpen(profileQuickCustomizeOpen);
}

function applyProfilePresentation(profile) {
  const home = $("profile-pane-home");
  const summary = home?.querySelector(".profile-summary");
  if (!home || !summary) return;
  const template = profileChoice(profile?.profile_template, PROFILE_TEMPLATE_KEYS, "classic");
  const accent = profileChoice(profile?.profile_accent, PROFILE_ACCENT_KEYS, "default");
  const density = profileChoice(profile?.profile_density, PROFILE_DENSITY_KEYS, "comfortable");
  const style = profileStyleFromProfile(profile || {});
  const classes = [
    ...PROFILE_TEMPLATE_KEYS.map((key) => `profile-template-${key}`),
    ...PROFILE_ACCENT_KEYS.map((key) => `profile-accent-${key}`),
    ...PROFILE_DENSITY_KEYS.map((key) => `profile-density-${key}`),
    ...PROFILE_STYLE_ALLOWED.banner.map((key) => `profile-banner-${key}`),
    ...PROFILE_STYLE_ALLOWED.background_tone.map((key) => `profile-bg-tone-${key}`),
    ...PROFILE_STYLE_ALLOWED.decoration.map((key) => `profile-decoration-${key}`),
  ];
  home.classList.remove(...classes);
  summary.classList.remove(...classes);
  const nextClasses = [
    `profile-template-${template}`,
    `profile-accent-${accent}`,
    `profile-density-${density}`,
    `profile-banner-${style.banner}`,
    `profile-bg-tone-${style.background_tone}`,
    `profile-decoration-${style.decoration}`,
  ];
  home.classList.add(...nextClasses);
  summary.classList.add(...nextClasses);
  const avatar = $("profile-home-avatar");
  if (avatar) {
    avatar.classList.remove(
      ...PROFILE_STYLE_ALLOWED.avatar_frame.map((key) => `profile-avatar-frame-${key}`),
      ...PROFILE_STYLE_ALLOWED.avatar_shape.map((key) => `profile-avatar-shape-${key}`),
      ...PROFILE_STYLE_ALLOWED.avatar_size.map((key) => `profile-avatar-size-${key}`)
    );
    const size = profileAvatarSizeValue(style.avatar_size);
    avatar.classList.add(`profile-avatar-frame-${style.avatar_frame}`, `profile-avatar-shape-${style.avatar_shape}`);
    const mask = normalizeProfileAvatarMask(style.avatar_mask);
    if (style.avatar_shape === "custom" && mask) avatar.style.setProperty("--profile-avatar-mask", mask);
    else avatar.style.removeProperty("--profile-avatar-mask");
    applyProfileAvatarElementSize(avatar, size);
  }
  const name = $("profile-home-name");
  if (name) {
    name.classList.remove(
      ...PROFILE_STYLE_ALLOWED.name_font.map((key) => `profile-name-font-${key}`),
      ...PROFILE_STYLE_ALLOWED.name_size.map((key) => `profile-name-size-${key}`)
    );
    name.classList.add(`profile-name-font-${style.name_font}`, `profile-name-size-${style.name_size}`);
  }
  const decoration = $("profile-home-decoration");
  if (decoration) decoration.textContent = style.decoration === "badges" ? "● ● ●" : style.decoration === "ribbon" ? "PROFILE" : style.decoration === "constellation" ? "✦ ─ ✦" : "";
  const stickers = $("profile-home-stickers");
  if (stickers) stickers.textContent = PROFILE_STICKER_SYMBOLS[style.sticker] || "";
}

function renderProfileHome(profile) {
  currentProfileIsViewingSelf = String(profile?.id || "") === String(currentUserId || "");
  currentProfileViewedUserId = profile?.id || currentUserId || null;
  updateProfileTabVisibility();
  applyProfilePresentation(profile || {});
  renderProfileAvatar("profile-home-avatar", profile, { editable: currentProfileIsViewingSelf });
  const name = profile?.display_name || profile?.username || "-";
  const nameEl = $("profile-home-name");
  if (nameEl) nameEl.textContent = name;
  const meta = $("profile-home-meta");
  if (meta) {
    const role = profile?.role_label || profile?.role || "使用者";
    const level = profile?.member_level ? ` · ${profile.member_level}` : "";
    meta.textContent = `@${profile?.username || "-"} · ${role}${level}`;
  }
  const bio = $("profile-home-bio");
  if (bio) bio.textContent = profile?.bio || "這個人很懶什麼都沒寫";
  const signature = $("profile-home-signature");
  if (signature) signature.textContent = profile?.signature || "";
  renderProfileHomePublicAccountFields(profile);
  const status = $("profile-home-friend-status");
  if (status) status.textContent = profileFriendStatusLabel(profile?.friend_status || "self");
  const visibility = $("profile-home-visibility");
  if (visibility) visibility.textContent = profile?.profile_visibility || "public";
  const code = $("profile-home-friend-code");
  if (code) code.textContent = currentProfileIsViewingSelf ? (profile?.friend_code || "-") : "非本人不顯示";
  const friendCount = $("profile-home-friend-count");
  if (friendCount) friendCount.textContent = String(profile?.friend_count ?? 0);
  const followerCount = $("profile-home-follower-count");
  if (followerCount) followerCount.textContent = String(profile?.follower_count ?? 0);
  const followingCount = $("profile-home-following-count");
  if (followingCount) followingCount.textContent = String(profile?.following_count ?? 0);
  const actionRow = $("profile-home-actions");
  if (actionRow) {
    const targetId = Number(profile?.id || 0);
    if (!targetId || currentProfileIsViewingSelf) {
      actionRow.innerHTML = `
        <button class="btn btn-primary" type="button" data-profile-edit-self>編輯主頁資料</button>
        <button class="btn" type="button" data-profile-customize-self>調整主頁外觀</button>
        <button class="btn" type="button" data-profile-account-settings-self>帳號安全</button>
      `;
    } else {
      const actions = [];
      if (profile?.can_request_friend) actions.push(`<button class="btn" type="button" data-profile-request-viewed="${targetId}">加好友</button>`);
      if (profile?.can_accept_friend && profile?.friend_request_id) actions.push(`<button class="btn btn-primary" type="button" data-profile-accept-viewed="${sanitize(String(profile.friend_request_id))}">接受好友</button>`);
      if (profile?.can_follow) actions.push(`<button class="btn btn-primary" type="button" data-profile-follow="${targetId}">追蹤</button>`);
      if (profile?.can_unfollow) actions.push(`<button class="btn" type="button" data-profile-unfollow="${targetId}">取消追蹤</button>`);
      if (profile?.can_pm) actions.push(`<button class="btn" type="button" data-profile-pm="${sanitize(profile.username || "")}">私訊</button>`);
      actionRow.innerHTML = actions.length ? actions.join("") : `<span class="drive-card-sub">目前沒有可執行的互動操作</span>`;
    }
  }
}

function renderProfileHomePublicAccountFields(profile) {
  const host = $("profile-home-public-account-fields");
  if (!host) return;
  const fields = profile?.public_account_fields && typeof profile.public_account_fields === "object"
    ? profile.public_account_fields
    : {};
  const accountRows = PROFILE_PUBLIC_ACCOUNT_FIELDS
    .filter((key) => String(fields[key] || "").trim())
    .map((key) => `
      <div class="profile-public-info-item">
        <span class="drive-card-sub">${sanitize(PROFILE_PUBLIC_ACCOUNT_FIELD_LABELS[key] || key)}</span>
        <strong>${sanitize(fields[key])}</strong>
      </div>
    `);
  const customRows = normalizedProfilePublicInfo(profile?.profile_public_info)
    .map((item) => `
      <div class="profile-public-info-item">
        <span class="drive-card-sub">${sanitize(item.label)}</span>
        <strong>${sanitize(item.value)}</strong>
      </div>
    `);
  const rows = [...accountRows, ...customRows];
  host.innerHTML = rows.length ? rows.join("") : "";
  host.style.display = rows.length ? "" : "none";
}

function profilePublicAccountFieldsFromForm() {
  return PROFILE_PUBLIC_ACCOUNT_FIELDS.filter((key) => !!$(`profile-public-account-${key}`)?.checked);
}

function setProfilePublicAccountFields(keys) {
  const selected = new Set(Array.isArray(keys) ? keys : []);
  PROFILE_PUBLIC_ACCOUNT_FIELDS.forEach((key) => {
    const el = $(`profile-public-account-${key}`);
    if (el) el.checked = selected.has(key);
  });
}

function normalizedProfilePublicInfo(items) {
  const rows = Array.isArray(items) ? items : [];
  return rows
    .map((item) => ({
      label: String(item?.label || "").trim().slice(0, 80),
      value: String(item?.value || "").trim().slice(0, 500),
    }))
    .filter((item) => item.label && item.value)
    .slice(0, PROFILE_PUBLIC_INFO_MAX_ITEMS);
}

function profilePublicInfoRowMarkup(item = {}, index = 0) {
  return `
    <div class="profile-public-info-editor-row" data-profile-public-info-row>
      <label class="field">
        <span>欄位</span>
        <input type="text" data-profile-public-info-label maxlength="80" value="${sanitize(item.label || "")}" />
      </label>
      <label class="field">
        <span>內容</span>
        <input type="text" data-profile-public-info-value maxlength="500" value="${sanitize(item.value || "")}" />
      </label>
      <button class="btn btn-sm" type="button" data-profile-public-info-remove="${index}">刪除</button>
    </div>
  `;
}

function renderProfilePublicInfoEditor(items = []) {
  const host = $("profile-public-info-editor-list");
  if (!host) return;
  const rows = normalizedProfilePublicInfo(items);
  host.innerHTML = rows.map((item, index) => profilePublicInfoRowMarkup(item, index)).join("");
}

function addProfilePublicInfoEditorRow(item = {}) {
  const host = $("profile-public-info-editor-list");
  if (!host) return;
  const count = host.querySelectorAll("[data-profile-public-info-row]").length;
  if (count >= PROFILE_PUBLIC_INFO_MAX_ITEMS) return;
  host.insertAdjacentHTML("beforeend", profilePublicInfoRowMarkup(item, count));
}

function profilePublicInfoFromForm() {
  return normalizedProfilePublicInfo(
    Array.from(document.querySelectorAll("[data-profile-public-info-row]")).map((row) => ({
      label: row.querySelector("[data-profile-public-info-label]")?.value || "",
      value: row.querySelector("[data-profile-public-info-value]")?.value || "",
    }))
  );
}

function fillProfileEdit(profile, { force = false } = {}) {
  if (profileEditFormDirty && !force) return;
  const style = profileStyleFromProfile(profile || {});
  const fields = {
    "profile-edit-display-name": profile?.display_name || "",
    "profile-edit-location": profile?.location || "",
    "profile-edit-website": profile?.website || "",
    "profile-edit-bio": profile?.bio || "",
    "profile-edit-signature": profile?.signature || "",
    "profile-edit-visibility": profile?.profile_visibility || "public",
    "profile-edit-display-timezone": profile?.display_timezone || "auto",
    "profile-edit-template": profile?.profile_template || "classic",
    "profile-edit-accent": profile?.profile_accent || "default",
    "profile-edit-density": profile?.profile_density || "comfortable",
    "profile-edit-banner": style.banner,
    "profile-edit-background-tone": style.background_tone,
    "profile-edit-avatar-frame": style.avatar_frame,
    "profile-edit-avatar-shape": style.avatar_shape,
    "profile-edit-avatar-mask": style.avatar_mask,
    "profile-edit-avatar-size": profileAvatarSizeValue(style.avatar_size),
    "profile-edit-name-font": style.name_font,
    "profile-edit-name-size": style.name_size,
    "profile-edit-sticker": style.sticker,
    "profile-edit-decoration": style.decoration,
    "profile-friend-code": profile?.friend_code || "",
  };
  Object.entries(fields).forEach(([id, value]) => {
    const el = $(id);
    if (el) el.value = value;
  });
  updateProfileAvatarSizeControl(style.avatar_size);
  syncProfileAvatarMaskField(style.avatar_shape);
  if (typeof setUserDisplayTimezone === "function") {
    setUserDisplayTimezone(profile?.display_timezone || "auto");
  }
  setProfilePublicAccountFields(profile?.profile_public_account_fields || []);
  renderProfilePublicInfoEditor(profile?.profile_public_info || []);
}

async function loadMyProfile({ quiet = false } = {}) {
  const operation = profileOperationContext();
  try {
    const json = await profileReadJson(API + "/users/me/profile");
    profileAssertOperationCurrent(operation);
    const preserveDraft = profileEditFormDirty && currentProfileIsViewingSelf;
    profilePanelCache = json.profile || {};
    currentProfileViewedUserId = profilePanelCache.id || currentUserId || null;
    if (preserveDraft) {
      previewProfileAppearanceFromForm();
    } else {
      renderProfileHome(profilePanelCache);
      fillProfileEdit(profilePanelCache);
    }
    if (!quiet) profileSetMsg("");
    return profilePanelCache;
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return null;
    profileSetMsg(err.message || "個人資料讀取失敗", true);
    return null;
  }
}

async function loadUserProfile(userId, { quiet = false } = {}) {
  const targetId = Number(userId || 0);
  if (!targetId || String(targetId) === String(currentUserId || "")) {
    return loadMyProfile({ quiet });
  }
  const operation = profileOperationContext();
  try {
    const json = await profileReadJson(API + `/users/${encodeURIComponent(targetId)}/profile`);
    profileAssertOperationCurrent(operation);
    profilePanelCache = json.profile || {};
    renderProfileHome(profilePanelCache);
    if (!quiet) profileSetMsg("");
    return profilePanelCache;
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return null;
    profileSetMsg(err.message || "個人主頁讀取失敗", true);
    return null;
  }
}

async function saveMyProfile() {
  const operation = profileOperationContext();
  const payload = {
    display_name: $("profile-edit-display-name")?.value || "",
    location: $("profile-edit-location")?.value || "",
    website: $("profile-edit-website")?.value || "",
    bio: $("profile-edit-bio")?.value || "",
    signature: $("profile-edit-signature")?.value || "",
    profile_visibility: $("profile-edit-visibility")?.value || "public",
    display_timezone: $("profile-edit-display-timezone")?.value || "auto",
    profile_template: $("profile-edit-template")?.value || "classic",
    profile_accent: $("profile-edit-accent")?.value || "default",
    profile_density: $("profile-edit-density")?.value || "comfortable",
    profile_style: collectProfileStyleFromForm(),
    public_account_fields: profilePublicAccountFieldsFromForm(),
    profile_public_info: profilePublicInfoFromForm(),
  };
  try {
    const json = await profileReadJson(API + "/users/me/profile", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    profileAssertOperationCurrent(operation);
    clearProfileEditFormDirty();
    profilePanelCache = json.profile || {};
    if (typeof setUserDisplayTimezone === "function") {
      setUserDisplayTimezone(profilePanelCache.display_timezone || payload.display_timezone || "auto");
    }
    renderProfileHome(profilePanelCache);
    fillProfileEdit(profilePanelCache, { force: true });
    const signal = $("profile-appearance-preview-signal");
    if (signal) {
      signal.hidden = false;
      signal.textContent = "已儲存，外觀設定已套用到公開主頁。";
    }
    profileSetMsg(json.msg || "個人資料已更新");
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "個人資料更新失敗", true);
  }
}

function renderProfileFriendRows(containerId, rows, mode) {
  const el = $(containerId);
  if (!el) return;
  if (!rows || !rows.length) {
    el.innerHTML = `<div class="drive-empty">目前沒有資料</div>`;
    return;
  }
  el.innerHTML = rows.map((item) => {
    const badge = item.other_is_official ? `<span class="profile-official-badge">官方</span>` : "";
    const display = sanitize(item.other_display_name || item.other_username || "-");
    const username = sanitize(item.other_username || "-");
    let actions = "";
    if (mode === "friends") {
      actions = `
        <button class="btn chat-sticker-btn" type="button" data-profile-pm="${username}">私訊</button>
        <button class="btn chat-sticker-btn" type="button" data-profile-remove="${item.other_user_id}">解除</button>
        <button class="btn btn-danger chat-sticker-btn" type="button" data-profile-block="${item.other_user_id}">封鎖</button>
      `;
    } else if (mode === "incoming") {
      actions = `
        <button class="btn chat-sticker-btn" type="button" data-profile-review="${item.id}" data-decision="accept">接受</button>
        <button class="btn chat-sticker-btn" type="button" data-profile-review="${item.id}" data-decision="reject">拒絕</button>
      `;
    } else if (mode === "blocked") {
      actions = `<button class="btn chat-sticker-btn" type="button" data-profile-unblock="${item.other_user_id}">解除封鎖</button>`;
    } else {
      actions = `<span class="drive-card-sub">等待 ${username} 回覆</span>`;
    }
    return `
      <div class="profile-friend-row">
        <div>
          <strong>${display}</strong>
          ${badge}
          <span>@${username}</span>
        </div>
        <div class="profile-friend-actions">${actions}</div>
      </div>
    `;
  }).join("");
  el.querySelectorAll("[data-profile-pm]").forEach((btn) => {
    btn.addEventListener("click", () => openPmWithUser(btn.dataset.profilePm || ""));
  });
  el.querySelectorAll("[data-profile-remove]").forEach((btn) => {
    btn.addEventListener("click", () => removeProfileFriend(btn.dataset.profileRemove));
  });
  el.querySelectorAll("[data-profile-block]").forEach((btn) => {
    btn.addEventListener("click", () => blockProfileUser(btn.dataset.profileBlock));
  });
  el.querySelectorAll("[data-profile-unblock]").forEach((btn) => {
    btn.addEventListener("click", () => unblockProfileUser(btn.dataset.profileUnblock));
  });
  el.querySelectorAll("[data-profile-review]").forEach((btn) => {
    btn.addEventListener("click", () => reviewProfileFriend(btn.dataset.profileReview, btn.dataset.decision));
  });
}

async function loadProfileFriends({ quiet = false } = {}) {
  const operation = profileOperationContext();
  try {
    const json = await profileReadJson(API + "/friends");
    profileAssertOperationCurrent(operation);
    profileFriendsLoaded = true;
    renderProfileFriendRows("profile-friend-list", json.friends || [], "friends");
    renderProfileFriendRows("profile-incoming-list", json.incoming || [], "incoming");
    renderProfileFriendRows("profile-outgoing-list", json.outgoing || [], "outgoing");
    renderProfileFriendRows("profile-blocked-list", json.blocked || [], "blocked");
    if (!quiet) profileSetMsg("");
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "好友資料讀取失敗", true);
  }
}

async function requestProfileFriend() {
  const operation = profileOperationContext();
  const raw = ($("profile-request-user-input")?.value || "").trim();
  if (!raw) {
    profileSetMsg("請輸入帳號或 user id", true);
    return;
  }
  const payload = /^\d+$/.test(raw) ? { user_id: Number(raw) } : { username: raw };
  try {
    const json = await profileReadJson(API + "/friends/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "好友邀請已送出");
    if ($("profile-request-user-input")) $("profile-request-user-input").value = "";
    await loadProfileFriends({ quiet: true });
    profileAssertOperationCurrent(operation);
    if (typeof loadChatFriends === "function") loadChatFriends();
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "好友邀請失敗", true);
  }
}

async function requestProfileFriendForUser(userId) {
  const operation = profileOperationContext();
  const targetId = Number(userId || 0);
  if (!targetId) return;
  try {
    const json = await profileReadJson(API + "/friends/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: targetId }),
    });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "好友邀請已送出");
    await loadUserProfile(targetId, { quiet: true });
    profileAssertOperationCurrent(operation);
    if (typeof loadChatFriends === "function") loadChatFriends();
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "好友邀請失敗", true);
  }
}

async function acceptProfileFriendRequest(requestId) {
  const operation = profileOperationContext();
  if (!requestId) return;
  try {
    const json = await profileReadJson(API + `/friends/requests/${encodeURIComponent(requestId)}/accept`, {
      method: "POST",
    });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "已接受好友邀請");
    await loadProfilePanel();
    profileAssertOperationCurrent(operation);
    if (typeof loadChatFriends === "function") loadChatFriends();
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "好友申請處理失敗", true);
  }
}

async function followProfileUser(userId) {
  const operation = profileOperationContext();
  const targetId = Number(userId || 0);
  if (!targetId) return;
  try {
    const json = await profileReadJson(API + `/users/${encodeURIComponent(targetId)}/follow`, { method: "POST" });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "已追蹤");
    if (json.profile) renderProfileHome(json.profile);
    else {
      await loadUserProfile(targetId, { quiet: true });
      profileAssertOperationCurrent(operation);
    }
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "追蹤失敗", true);
  }
}

async function unfollowProfileUser(userId) {
  const operation = profileOperationContext();
  const targetId = Number(userId || 0);
  if (!targetId) return;
  try {
    const json = await profileReadJson(API + `/users/${encodeURIComponent(targetId)}/follow`, { method: "DELETE" });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "已取消追蹤");
    if (json.profile) renderProfileHome(json.profile);
    else {
      await loadUserProfile(targetId, { quiet: true });
      profileAssertOperationCurrent(operation);
    }
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "取消追蹤失敗", true);
  }
}

async function addProfileFriendByCode() {
  const operation = profileOperationContext();
  const code = ($("profile-add-code-input")?.value || "").trim();
  if (!code) {
    profileSetMsg("請輸入好友代碼", true);
    return;
  }
  try {
    const json = await profileReadJson(API + "/friends/add-by-code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ friend_code: code }),
    });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "已加入好友");
    if ($("profile-add-code-input")) $("profile-add-code-input").value = "";
    await loadProfileFriends({ quiet: true });
    profileAssertOperationCurrent(operation);
    if (typeof loadChatFriends === "function") loadChatFriends();
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "加入好友失敗", true);
  }
}

async function reviewProfileFriend(requestId, decision) {
  const operation = profileOperationContext();
  if (!requestId || !decision) return;
  try {
    const json = await profileReadJson(API + `/friends/requests/${encodeURIComponent(requestId)}/${encodeURIComponent(decision)}`, {
      method: "POST",
    });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "好友申請已處理");
    await loadProfileFriends({ quiet: true });
    profileAssertOperationCurrent(operation);
    if (typeof loadChatFriends === "function") loadChatFriends();
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "好友申請處理失敗", true);
  }
}

async function removeProfileFriend(friendUserId) {
  const operation = profileOperationContext();
  if (!friendUserId) return;
  if (!(await profileConfirm("確定要解除這位好友嗎？解除後需要重新申請才能恢復好友互動。", {
    title: "解除好友",
    confirmLabel: "解除",
    danger: true,
  }))) return;
  if (!profileOperationIsCurrent(operation)) return;
  try {
    const json = await profileReadJson(API + `/friends/${encodeURIComponent(friendUserId)}`, { method: "DELETE" });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "已解除好友");
    await loadProfileFriends({ quiet: true });
    profileAssertOperationCurrent(operation);
    if (typeof loadChatFriends === "function") loadChatFriends();
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "解除好友失敗", true);
  }
}

async function blockProfileUser(friendUserId) {
  const operation = profileOperationContext();
  if (!friendUserId) return;
  if (!(await profileConfirm("確定要封鎖這位使用者？封鎖後對方不能再與你私訊、邀請遊戲或重新送出好友申請。", {
    title: "封鎖使用者",
    confirmLabel: "封鎖",
    danger: true,
  }))) return;
  if (!profileOperationIsCurrent(operation)) return;
  try {
    const json = await profileReadJson(API + `/friends/${encodeURIComponent(friendUserId)}/block`, { method: "POST" });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "已封鎖使用者");
    await loadProfileFriends({ quiet: true });
    profileAssertOperationCurrent(operation);
    if (typeof loadChatFriends === "function") loadChatFriends();
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "封鎖失敗", true);
  }
}

async function unblockProfileUser(friendUserId) {
  const operation = profileOperationContext();
  if (!friendUserId) return;
  if (!(await profileConfirm("確定要解除封鎖？解除後不會自動恢復好友關係。", {
    title: "解除封鎖",
    confirmLabel: "解除封鎖",
  }))) return;
  if (!profileOperationIsCurrent(operation)) return;
  try {
    const json = await profileReadJson(API + `/friends/${encodeURIComponent(friendUserId)}/block`, { method: "DELETE" });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "已解除封鎖");
    await loadProfileFriends({ quiet: true });
    profileAssertOperationCurrent(operation);
    if (typeof loadChatFriends === "function") loadChatFriends();
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "解除封鎖失敗", true);
  }
}

async function rotateProfileFriendCode() {
  const operation = profileOperationContext();
  if (!(await profileConfirm("重新產生好友代碼後，舊代碼會失效。確定要繼續嗎？", {
    title: "重新產生好友代碼",
    confirmLabel: "重新產生",
  }))) return;
  if (!profileOperationIsCurrent(operation)) return;
  try {
    const json = await profileReadJson(API + "/users/me/friend-code/rotate", { method: "POST" });
    profileAssertOperationCurrent(operation);
    profileSetMsg(json.msg || "好友代碼已重新產生");
    await loadMyProfile({ quiet: true });
    profileAssertOperationCurrent(operation);
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    profileSetMsg(err.message || "好友代碼更新失敗", true);
  }
}

async function copyTextToClipboard(text) {
  const value = String(text || "");
  if (!value) return false;
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return true;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.left = "-1000px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, value.length);
  try {
    return document.execCommand("copy");
  } finally {
    textarea.remove();
  }
}

function setProfileCopyButtonFeedback(button) {
  if (!button) return;
  const original = button.dataset.originalText || button.textContent || "複製";
  button.dataset.originalText = original;
  button.textContent = "已複製";
  button.disabled = true;
  setTimeout(() => {
    button.textContent = original;
    button.disabled = false;
  }, 1400);
}

async function copyProfileFriendCode(event) {
  const operation = profileOperationContext();
  const button = event?.currentTarget || $("profile-copy-code-btn");
  const code = $("profile-friend-code")?.value || profilePanelCache?.friend_code || "";
  if (!code) {
    profileSetMsg("目前沒有可複製的好友代碼", true);
    return;
  }
  try {
    const copied = await copyTextToClipboard(code);
    profileAssertOperationCurrent(operation);
    if (!copied) throw new Error("copy failed");
    setProfileCopyButtonFeedback(button);
    showActionFeedback(button || document.activeElement, "好友代碼已複製", true, { skipToast: true });
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return;
    const input = $("profile-friend-code");
    if (input) {
      input.focus();
      input.select();
    }
    showActionFeedback(button || document.activeElement, "請手動複製好友代碼", false, { skipToast: true });
  }
}

function switchProfileTab(tab = "home") {
  const quickCustomize = tab === "appearance";
  if (quickCustomize) {
    currentProfileViewedUserId = currentUserId || null;
    currentProfileIsViewingSelf = true;
  }
  const requested = quickCustomize ? "home" : (["home", "edit", "friends"].includes(tab) ? tab : "home");
  const next = !currentProfileIsViewingSelf && ["edit", "friends"].includes(requested) ? "home" : requested;
  profileQuickCustomizeOpen = quickCustomize && next === "home" && currentProfileIsViewingSelf;
  currentProfileTab = next;
  if (next !== "home") {
    currentProfileViewedUserId = currentUserId || null;
    currentProfileIsViewingSelf = true;
    profileQuickCustomizeOpen = false;
  }
  updateProfileTabVisibility();
  document.querySelectorAll("[data-profile-tab]").forEach((btn) => {
    const active = btn.dataset.profileTab === next;
    btn.classList.toggle("btn-primary", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  ["home", "edit", "friends"].forEach((key) => {
    const pane = $("profile-pane-" + key);
    if (pane) pane.classList.toggle("active", key === next);
  });
  if (currentModuleTab === "profile") {
    if (next === "friends") {
      loadMyProfile({ quiet: true });
      loadProfileFriends();
    } else {
      loadMyProfile();
    }
  }
  setProfileQuickCustomizeOpen(profileQuickCustomizeOpen, { focus: quickCustomize });
  if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
}

function loadProfilePanel() {
  if (currentProfileTab === "friends") {
    loadMyProfile({ quiet: true });
    loadProfileFriends({ quiet: !profileFriendsLoaded });
    return;
  }
  if (currentProfileTab === "home" && currentProfileViewedUserId && String(currentProfileViewedUserId) !== String(currentUserId || "")) {
    loadUserProfile(currentProfileViewedUserId);
    return;
  }
  loadMyProfile();
}

function openMyProfilePanel(tab = "home") {
  currentProfileViewedUserId = currentUserId || null;
  currentProfileIsViewingSelf = true;
  if (typeof switchModuleTab === "function") switchModuleTab("profile");
  switchProfileTab(tab);
}

function openUserProfile(userId) {
  const targetId = Number(userId || 0);
  if (!targetId) return;
  if (String(targetId) === String(currentUserId || "")) {
    openMyProfilePanel("home");
    return;
  }
  currentProfileViewedUserId = targetId;
  currentProfileIsViewingSelf = false;
  currentProfileTab = "home";
  if (typeof switchModuleTab === "function") switchModuleTab("profile");
  switchProfileTab("home");
  loadUserProfile(targetId);
}

window.openUserProfile = openUserProfile;

function bindProfileAvatarUploaderControls() {
  if (window.__profileAvatarUploaderBound) return;
  window.__profileAvatarUploaderBound = true;
  const els = profileAvatarCropperElements();
  if (els.file) {
    els.file.addEventListener("change", () => loadProfileAvatarFile(els.file.files?.[0] || null));
  }
  if (els.cloudRefresh) {
    els.cloudRefresh.addEventListener("click", () => loadProfileAvatarCloudFiles({ force: true }));
  }
  if (els.cloudUse) {
    els.cloudUse.addEventListener("click", useSelectedProfileCloudAvatar);
  }
  if (els.cloudSelect) {
    els.cloudSelect.addEventListener("change", () => selectProfileAvatarCloudFile(els.cloudSelect.value));
  }
  if (els.cloudGallery) {
    els.cloudGallery.addEventListener("click", (event) => {
      const card = event.target.closest("[data-profile-cloud-avatar-id]");
      if (!card) return;
      selectProfileAvatarCloudFile(card.dataset.profileCloudAvatarId || "", { loadPreview: true });
    });
  }
  if (els.stage) {
    els.stage.addEventListener("pointerdown", handleProfileAvatarPointerStart);
    els.stage.addEventListener("pointermove", handleProfileAvatarPointerMove);
    els.stage.addEventListener("pointerup", handleProfileAvatarPointerEnd);
    els.stage.addEventListener("pointercancel", handleProfileAvatarPointerEnd);
  }
  if (els.zoom) {
    els.zoom.addEventListener("input", () => {
      syncProfileAvatarZoomControl(els.zoom.value);
      renderProfileAvatarCropper();
    });
  }
  els.zoomSteps.forEach((button) => {
    button.addEventListener("click", () => {
      const delta = Number.parseFloat(button.dataset.profileAvatarZoomStep || "0");
      if (!Number.isFinite(delta) || delta === 0) return;
      syncProfileAvatarZoomControl(profileAvatarCropState.zoom + delta);
      renderProfileAvatarCropper();
    });
  });
  if (els.shape) {
    els.shape.addEventListener("change", () => {
      syncProfileAvatarShapeControls(els.shape.value, { preview: true });
      renderProfileAvatarCropper();
    });
  }
  if (els.center) {
    els.center.addEventListener("click", () => {
      syncProfileAvatarZoomControl(PROFILE_AVATAR_DEFAULT_ZOOM);
      profileAvatarCropState.offsetX = 0;
      profileAvatarCropState.offsetY = 0;
      renderProfileAvatarCropper();
    });
  }
  els.rotationSteps.forEach((button) => {
    button.addEventListener("click", () => {
      const delta = Number.parseInt(button.dataset.profileAvatarRotateStep || "0", 10);
      if (!Number.isFinite(delta) || delta === 0) return;
      syncProfileAvatarRotationControl(profileAvatarCropState.rotation + delta);
      profileAvatarCropState.offsetX = 0;
      profileAvatarCropState.offsetY = 0;
      renderProfileAvatarCropper();
    });
  });
  const upload = $("profile-avatar-upload-btn");
  if (upload) upload.addEventListener("click", uploadProfileAvatar);
  const cancel = $("profile-avatar-cancel-btn");
  if (cancel) cancel.addEventListener("click", closeProfileAvatarUploader);
  if (els.overlay) {
    els.overlay.addEventListener("click", (event) => {
      if (event.target === els.overlay) closeProfileAvatarUploader();
    });
  }
  window.addEventListener("resize", () => {
    if (profileAvatarCropState.hasImage) requestAnimationFrame(renderProfileAvatarCropper);
  }, { passive: true });
  window.addEventListener("keydown", (event) => {
    const overlay = $("profile-avatar-overlay");
    if (event.key === "Escape" && overlay && overlay.classList.contains("show")) {
      closeProfileAvatarUploader();
    }
  });
}

function bindProfileFriendsControls() {
  if (window.__profileFriendsBound) return;
  window.__profileFriendsBound = true;
  document.querySelectorAll("[data-profile-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchProfileTab(btn.dataset.profileTab || "home"));
  });
  const refresh = $("profile-refresh-btn");
  if (refresh) refresh.addEventListener("click", loadProfilePanel);
  const save = $("profile-save-btn");
  if (save) save.addEventListener("click", saveMyProfile);
  const appearanceSave = $("profile-appearance-save-btn");
  if (appearanceSave) appearanceSave.addEventListener("click", saveMyProfile);
  const quickCustomize = $("profile-quick-customize-card");
  if (quickCustomize) {
    quickCustomize.addEventListener("toggle", () => {
      profileQuickCustomizeOpen = quickCustomize.open && currentProfileIsViewingSelf;
      if (typeof updateSidebarActiveState === "function") updateSidebarActiveState();
    });
  }
  const publicInfoAdd = $("profile-public-info-add-btn");
  if (publicInfoAdd) {
    publicInfoAdd.addEventListener("click", () => {
      markProfileEditFormDirty();
      addProfilePublicInfoEditorRow();
    });
  }
  const publicInfoList = $("profile-public-info-editor-list");
  if (publicInfoList) {
    publicInfoList.addEventListener("input", markProfileEditFormDirty);
    publicInfoList.addEventListener("change", markProfileEditFormDirty);
    publicInfoList.addEventListener("click", (event) => {
      const remove = event.target?.closest?.("[data-profile-public-info-remove]");
      if (!remove) return;
      event.preventDefault();
      markProfileEditFormDirty();
      remove.closest("[data-profile-public-info-row]")?.remove();
    });
  }
  ["profile-quick-customize-card", "profile-pane-edit"].forEach((id) => {
    const host = $(id);
    if (!host || host.dataset.profileDirtyBound === "1") return;
    host.dataset.profileDirtyBound = "1";
    host.addEventListener("input", markProfileEditFormDirty);
    host.addEventListener("change", markProfileEditFormDirty);
  });
  [
    "profile-edit-template",
    "profile-edit-accent",
    "profile-edit-density",
    ...Object.keys(PROFILE_STYLE_FIELD_MAP),
  ].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("change", previewProfileAppearanceFromForm);
    el.addEventListener("input", previewProfileAppearanceFromForm);
  });
  document.querySelectorAll("[data-profile-avatar-size]").forEach((button) => {
    button.addEventListener("click", () => {
      markProfileEditFormDirty();
      updateProfileAvatarSizeControl(button.dataset.profileAvatarSize);
      previewProfileAppearanceFromForm();
    });
  });
  const addByCode = $("profile-add-code-btn");
  if (addByCode) addByCode.addEventListener("click", addProfileFriendByCode);
  const requestBtn = $("profile-request-user-btn");
  if (requestBtn) requestBtn.addEventListener("click", requestProfileFriend);
  const copyCode = $("profile-copy-code-btn");
  if (copyCode) copyCode.addEventListener("click", copyProfileFriendCode);
  const rotateCode = $("profile-rotate-code-btn");
  if (rotateCode) rotateCode.addEventListener("click", rotateProfileFriendCode);
  const avatarButton = $("profile-home-avatar");
  if (avatarButton) avatarButton.addEventListener("click", () => {
    if (currentProfileIsViewingSelf) openProfileAvatarUploader({ pickFile: true });
    else openProfileAvatarPreview(profilePanelCache);
  });
  bindProfileAvatarUploaderControls();
  const sidebarCard = $("sidebar-user-card");
  if (sidebarCard) {
    sidebarCard.addEventListener("click", () => {
      if (currentUser) openMyProfilePanel("home");
    });
    sidebarCard.addEventListener("keydown", (event) => {
      if (!currentUser) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openMyProfilePanel("home");
      }
    });
  }
  document.addEventListener("hackme:module-changed", (event) => {
    if (event?.detail?.current === "profile") loadProfilePanel();
  });
  document.addEventListener("click", (event) => {
    const profileLink = event.target.closest("[data-open-user-profile]");
    if (profileLink) {
      event.preventDefault();
      openUserProfile(profileLink.dataset.openUserProfile);
      return;
    }
    const editSelf = event.target.closest("[data-profile-edit-self]");
    if (editSelf) {
      switchProfileTab("edit");
      return;
    }
    const accountSettingsSelf = event.target.closest("[data-profile-account-settings-self]");
    if (accountSettingsSelf) {
      if (currentUserId && typeof editUser === "function") editUser(currentUserId);
      return;
    }
    const customizeSelf = event.target.closest("[data-profile-customize-self]");
    if (customizeSelf) {
      switchProfileTab("appearance");
      return;
    }
    const requestViewed = event.target.closest("[data-profile-request-viewed]");
    if (requestViewed) {
      requestProfileFriendForUser(requestViewed.dataset.profileRequestViewed);
      return;
    }
    const acceptViewed = event.target.closest("[data-profile-accept-viewed]");
    if (acceptViewed) {
      acceptProfileFriendRequest(acceptViewed.dataset.profileAcceptViewed);
      return;
    }
    const follow = event.target.closest("[data-profile-follow]");
    if (follow) {
      followProfileUser(follow.dataset.profileFollow);
      return;
    }
    const unfollow = event.target.closest("[data-profile-unfollow]");
    if (unfollow) {
      unfollowProfileUser(unfollow.dataset.profileUnfollow);
      return;
    }
    const pm = event.target.closest("[data-profile-pm]");
    if (pm) {
      openPmWithUser(pm.dataset.profilePm || "");
    }
  });
  document.addEventListener("keydown", (event) => {
    const profileLink = event.target.closest?.("[data-open-user-profile]");
    if (!profileLink) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openUserProfile(profileLink.dataset.openUserProfile);
    }
  });
  bindTargetUserOptionInputs();
}

function ensureTargetOptionsDatalist(context = "personal") {
  const safeContext = String(context || "personal").replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
  const id = safeContext === "personal" ? "target-options-personal" : `target-options-${safeContext}`;
  let el = $(id);
  if (!el) {
    el = document.createElement("datalist");
    el.id = id;
    document.body.appendChild(el);
  }
  return el;
}

function renderTargetOptions(context, users) {
  const datalist = ensureTargetOptionsDatalist(context);
  datalist.innerHTML = (Array.isArray(users) ? users : []).map((user) => {
    const value = sanitize(user.username || "");
    const label = sanitize(user.label || user.username || "");
    return value ? `<option value="${value}" label="${label}"></option>` : "";
  }).join("");
}

async function loadTargetUserOptions(context = "personal", { force = false } = {}) {
  const operation = profileOperationContext();
  const key = String(context || "personal");
  if (!force && targetOptionCache.has(key)) {
    renderTargetOptions(key, targetOptionCache.get(key));
    return targetOptionCache.get(key);
  }
  try {
    const json = await profileReadJson(API + `/users/target-options?context=${encodeURIComponent(key)}&limit=160`);
    profileAssertOperationCurrent(operation);
    const users = Array.isArray(json.users) ? json.users : [];
    targetOptionCache.set(key, users);
    renderTargetOptions(key, users);
    return users;
  } catch (err) {
    if (!profileOperationIsCurrent(operation) || err?.name === "AbortError") return [];
    renderTargetOptions(key, []);
    return [];
  }
}

function bindTargetUserOptionInputs() {
  [
    ["chat-room-target-user", "pm"],
    ["chat-room-invite-users", "private_group"],
    ["chat-room-invite-more-users", "private_group"],
    ["drive-share-account", "cloud_drive_share"],
  ].forEach(([id, context]) => {
    const input = $(id);
    if (!input || input.dataset.targetOptionsBound === "1") return;
    input.dataset.targetOptionsBound = "1";
    input.setAttribute("list", ensureTargetOptionsDatalist(context).id);
    input.addEventListener("focus", () => loadTargetUserOptions(context));
    input.addEventListener("input", () => loadTargetUserOptions(context));
  });
}

function resetProfileAccountState() {
  profileAccountGeneration += 1;
  if (profileAppearancePreviewTimer) clearTimeout(profileAppearancePreviewTimer);
  profileAppearancePreviewTimer = 0;
  profilePanelCache = null;
  currentProfileViewedUserId = null;
  currentProfileIsViewingSelf = true;
  profileFriendsLoaded = false;
  profileQuickCustomizeOpen = false;
  profileEditFormDirty = false;
  profileAvatarCloudFiles = [];
  profileAvatarCloudFilesLoadedAt = 0;
  targetOptionCache.clear();
  document.querySelectorAll('datalist[id^="target-options-"]').forEach((list) => list.replaceChildren());
  closeProfileAvatarUploader();
  closeProfileAvatarPreview();
  resetProfileAvatarCropper();
  [
    "profile-edit-display-name",
    "profile-edit-location",
    "profile-edit-website",
    "profile-edit-bio",
    "profile-edit-signature",
    "profile-friend-code",
    "profile-add-code-input",
    "profile-request-user-input",
  ].forEach((id) => {
    const input = $(id);
    if (input) input.value = "";
  });
  [
    "profile-public-account-nickname",
    "profile-public-account-real_name",
    "profile-public-account-birthdate",
    "profile-public-account-phone",
  ].forEach((id) => {
    const input = $(id);
    if (input) input.checked = false;
  });
  const placeholders = {
    "profile-home-name": "-",
    "profile-home-meta": "-",
    "profile-home-bio": "尚未載入個人主頁",
    "profile-home-signature": "",
    "profile-home-friend-count": "0",
    "profile-home-follower-count": "0",
    "profile-home-following-count": "0",
    "profile-home-friend-status": "-",
    "profile-home-friend-code": "-",
  };
  Object.entries(placeholders).forEach(([id, value]) => {
    const element = $(id);
    if (element) element.textContent = value;
  });
  [
    "profile-home-public-account-fields",
    "profile-home-actions",
    "profile-public-info-editor-list",
    "profile-friend-list",
    "profile-incoming-list",
    "profile-outgoing-list",
    "profile-blocked-list",
    "profile-avatar-cloud-gallery",
  ].forEach((id) => {
    const element = $(id);
    if (element) element.replaceChildren();
  });
  const cloudSelect = $("profile-avatar-cloud-file");
  if (cloudSelect) cloudSelect.replaceChildren();
}

document.addEventListener("hackme:account-context-changed", resetProfileAccountState);

(() => {
  const MIN_PROFILE_AVATAR_SIZE = PROFILE_AVATAR_MIN_DISPLAY_SIZE;
  const MAX_PROFILE_AVATAR_SIZE = PROFILE_AVATAR_MAX_DISPLAY_SIZE;
  const DEFAULT_PROFILE_AVATAR_SIZE = 140;

  function profileAvatarSizeInput() {
    return document.getElementById("profile-edit-avatar-size");
  }

  function normalizeLiveProfileAvatarSize(value) {
    const numeric = Number.parseInt(String(value || ""), 10);
    if (!Number.isFinite(numeric)) return DEFAULT_PROFILE_AVATAR_SIZE;
    return Math.min(MAX_PROFILE_AVATAR_SIZE, Math.max(MIN_PROFILE_AVATAR_SIZE, numeric));
  }

  function setProfileAvatarSizeOutput(size) {
    const output = document.getElementById("profile-edit-avatar-size-value");
    if (!output) return;
    output.value = `${size}px`;
    output.textContent = `${size}px`;
  }

  function profileAvatarPreviewTargets() {
    const selectors = [
      "#profile-appearance-preview .profile-avatar",
      "#profile-appearance-preview .profile-avatar-large",
      "#profile-edit-preview .profile-avatar",
      "#profile-edit-preview .profile-avatar-large",
      ".profile-appearance-preview .profile-avatar",
      ".profile-appearance-preview .profile-avatar-large",
      ".profile-editor-preview .profile-avatar",
      ".profile-editor-preview .profile-avatar-large",
      "#profile-home-avatar",
      "#profile-pane-home .profile-avatar-large",
      ".profile-summary .profile-avatar-large",
      "[data-profile-preview-avatar]",
    ];
    return Array.from(document.querySelectorAll(selectors.join(",")));
  }

  function markProfileAvatarPreset(size) {
    document.querySelectorAll("[data-profile-avatar-size]").forEach((button) => {
      const active = normalizeLiveProfileAvatarSize(button.dataset.profileAvatarSize) === size;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function applyLiveProfileAvatarSize(value, options = {}) {
    const size = normalizeLiveProfileAvatarSize(value);
    const input = profileAvatarSizeInput();
    if (input && String(input.value) !== String(size)) {
      input.value = String(size);
    }
    document.documentElement.style.setProperty("--profile-avatar-live-size", `${size}px`);
    setProfileAvatarSizeOutput(size);
    markProfileAvatarPreset(size);
    profileAvatarPreviewTargets().forEach((avatar) => {
      applyProfileAvatarElementSize(avatar, size);
    });
    const signal = document.getElementById("profile-appearance-preview-signal");
    if (signal) {
      signal.dataset.changed = String(Date.now());
    }
    if (input && options.dispatch !== false) {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return size;
  }

  document.addEventListener("input", (event) => {
    if (event.target && event.target.id === "profile-edit-avatar-size") {
      applyLiveProfileAvatarSize(event.target.value, { dispatch: false });
    }
  });

  document.addEventListener("click", (event) => {
    const button = event.target?.closest?.("[data-profile-avatar-size]");
    if (!button) return;
    event.preventDefault();
    const size = applyLiveProfileAvatarSize(button.dataset.profileAvatarSize);
    const input = profileAvatarSizeInput();
    if (input) input.dispatchEvent(new Event("input", { bubbles: true }));
    markProfileAvatarPreset(size);
  });

  document.addEventListener("submit", (event) => {
    const input = profileAvatarSizeInput();
    if (!input || !event.target?.contains?.(input)) return;
    applyLiveProfileAvatarSize(input.value, { dispatch: false });
  }, true);

  document.addEventListener("DOMContentLoaded", () => {
    const input = profileAvatarSizeInput();
    if (input) applyLiveProfileAvatarSize(input.value, { dispatch: false });
  });

  window.applyLiveProfileAvatarSize = applyLiveProfileAvatarSize;
})();
