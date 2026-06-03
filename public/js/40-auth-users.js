function safeLoginReturnToPath() {
  let raw = "";
  try {
    raw = new URLSearchParams(window.location.search || "").get("return_to") || "";
  } catch (err) {
    return "";
  }
  raw = String(raw || "").trim();
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) return "";
  let target = null;
  try {
    target = new URL(raw, window.location.origin);
  } catch (err) {
    return "";
  }
  if (target.origin !== window.location.origin) return "";
  if (!target.pathname.startsWith("/shared/")) return "";
  return `${target.pathname}${target.search}${target.hash}`;
}

function redirectToLoginReturnToIfNeeded(loginJson = {}) {
  if (loginJson?.must_change_password) return false;
  const returnTo = safeLoginReturnToPath();
  if (!returnTo) return false;
  window.location.assign(returnTo);
  return true;
}

let loginRequestBusy = false;

async function doLogin() {
  if (loginRequestBusy) return;
  const user = sanitize($("li-user").value.trim());
  const pw   = $("li-pw").value;
  const devLoginToken = isInternalTestLoginMode() ? ($("li-internal-test-token")?.value || "") : "";
  if (!user || (!pw && !devLoginToken)) { flash($("li-msg"), "請填寫帳號與密碼，或輸入 root 提供的測試 token", false); return; }

  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  if (!csrf) {
    flash($("li-msg"), "安全驗證狀態失效，請重新整理頁面", false);
    return;
  }
  loginRequestBusy = true;
  setLoading("li-btn", "li-spinner", true);
  clearMsg();

  try {
    const loginPayload = { username: user, password: pw, csrf_token: csrf };
    if (devLoginToken) loginPayload.login_token = devLoginToken;
    const res = await apiFetch(API + "/login", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf || ""
      },
      body: JSON.stringify(loginPayload)
    });
    const json = await res.json();
    if (!json.ok) {
      setCsrfToken(null);
      flash($("li-msg"), json.msg || "登入失敗", false);
      return;
    }
    clearIdleTimeoutLogoutPending();
    setCsrfToken(null);
    if (redirectToLoginReturnToIfNeeded(json)) return;
    const meRes = await apiFetch(API + "/me", { credentials: "same-origin" });
    const me = await meRes.json();
    if (me.ok) setAuthState(me, true);
    else setAuthState({ username: user, role: "user", role_label: "一般用戶", nickname: "-" }, true);
    if (json.birthday_gift?.created || json.birthday_gift?.storage_quota_gift?.created) {
      const amount = Number(json.birthday_gift.amount || 1000);
      const storageGift = json.birthday_gift?.storage_quota_gift ? "，1GB 雲端硬碟 7 日已入帳" : "";
      const message = `生日禮金 ${Number.isFinite(amount) ? amount : 1000} 點已入帳${storageGift}`;
      if (typeof showAppToast === "function") showAppToast(message, true);
      else flash($("li-msg"), message, true);
    } else if (json.signup_bonus?.created) {
      const message = "註冊禮已入帳官方熱錢包";
      if (typeof showAppToast === "function") showAppToast(message, true);
      else flash($("li-msg"), message, true);
    } else if (json.wallet_onboarding?.required) {
      const message = "請到積分系統完成 PointsChain 錢包設定。";
      if (typeof showAppToast === "function") showAppToast(message, true);
      else flash($("li-msg"), message, true);
    }
  } catch (e) {
    flash($("li-msg"), "網路錯誤，請稍後再試", false);
  } finally {
    loginRequestBusy = false;
    setLoading("li-btn", "li-spinner", false);
  }
}

const REGISTER_FIELD_ID_MAP = {
  username: "reg-user",
  password: "reg-pw",
  password_confirm: "reg-pw-confirm",
  nickname: "reg-nickname",
  email: "reg-email",
  real_name: "reg-realname",
  birthdate: "reg-birthdate",
  phone: "reg-phone",
};

function clearRegisterFieldErrors() {
  Object.values(REGISTER_FIELD_ID_MAP).forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.classList.remove("field-error");
    el.removeAttribute("aria-invalid");
  });
}

function markRegisterFieldError(field) {
  const el = $(REGISTER_FIELD_ID_MAP[field] || "");
  if (!el) return null;
  el.classList.add("field-error");
  el.setAttribute("aria-invalid", "true");
  return el;
}

function guessRegisterFieldFromMessage(message) {
  const text = String(message || "");
  if (/帳號|username/i.test(text)) return "username";
  if (/暱稱/.test(text)) return "nickname";
  if (/email/i.test(text)) return "email";
  if (/生日/.test(text)) return "birthdate";
  if (/電話/.test(text)) return "phone";
  if (/兩次.*密碼|確認密碼/.test(text)) return "password_confirm";
  if (/密碼|password/i.test(text)) return "password";
  return "";
}

function shouldClearRegisterPasswords(field, message) {
  if (field === "password" || field === "password_confirm") return true;
  return /密碼|password/i.test(String(message || ""));
}

function showRegisterError(message, field = "", { focus = true } = {}) {
  clearRegisterFieldErrors();
  const targetField = field || guessRegisterFieldFromMessage(message);
  const target = targetField ? markRegisterFieldError(targetField) : null;
  if (focus && target && typeof target.focus === "function") target.focus();
  if (shouldClearRegisterPasswords(targetField, message)) {
    if ($("reg-pw")) $("reg-pw").value = "";
    if ($("reg-pw-confirm")) $("reg-pw-confirm").value = "";
  }
  flash($("reg-msg"), message || "註冊失敗", false);
}

const USER_APPEARANCE_FIELD_MAP = {
  site_theme_mode: "edit-user-theme-mode",
  site_font_family: "edit-user-site-font-family",
  site_background_style: "edit-user-site-background-style",
  site_panel_style: "edit-user-site-panel-style",
  site_sidebar_width: "edit-user-site-sidebar-width",
  site_bg: "edit-user-site-bg",
  site_surface: "edit-user-site-surface",
  site_accent: "edit-user-site-accent",
  site_accent2: "edit-user-site-accent2",
  site_text: "edit-user-site-text",
  site_muted: "edit-user-site-muted",
  site_layout_mode: "edit-user-site-layout-mode",
  site_density: "edit-user-site-density",
  site_radius_px: "edit-user-site-radius-px",
  site_font_scale: "edit-user-site-font-scale",
  site_content_width: "edit-user-site-content-width",
};
const USER_APPEARANCE_PRESETS = {
  midnight: {
    site_theme_mode: "dark",
    site_font_family: "system",
    site_background_style: "aurora",
    site_panel_style: "glass",
    site_sidebar_width: "standard",
    site_bg: "#11131d",
    site_surface: "#1b2030",
    site_accent: "#7a7bdc",
    site_accent2: "#43b6a0",
    site_text: "#eceef8",
    site_muted: "#aeb8cc",
    site_layout_mode: "centered",
    site_density: "comfortable",
    site_radius_px: 12,
    site_font_scale: 1,
    site_content_width: 1380,
  },
  daylight: {
    site_theme_mode: "light",
    site_font_family: "system",
    site_background_style: "flat",
    site_panel_style: "solid",
    site_sidebar_width: "standard",
    site_bg: "#eef2f6",
    site_surface: "#fbfcfe",
    site_accent: "#3267a8",
    site_accent2: "#447a62",
    site_text: "#243041",
    site_muted: "#5c6678",
    site_layout_mode: "centered",
    site_density: "comfortable",
    site_radius_px: 8,
    site_font_scale: 1,
    site_content_width: 1380,
  },
  sunset: {
    site_theme_mode: "dark",
    site_font_family: "rounded",
    site_background_style: "aurora",
    site_panel_style: "glass",
    site_sidebar_width: "wide",
    site_bg: "#1a1218",
    site_surface: "#2a1b23",
    site_accent: "#d97958",
    site_accent2: "#c89b52",
    site_text: "#f4e9e3",
    site_muted: "#c6a99f",
    site_layout_mode: "wide",
    site_density: "comfortable",
    site_radius_px: 18,
    site_font_scale: 1.08,
    site_content_width: 1600,
  },
  forest: {
    site_theme_mode: "dark",
    site_font_family: "system",
    site_background_style: "grid",
    site_panel_style: "matte",
    site_sidebar_width: "wide",
    site_bg: "#101812",
    site_surface: "#1d2a20",
    site_accent: "#6fb886",
    site_accent2: "#a2bb72",
    site_text: "#eff4eb",
    site_muted: "#a7b79f",
    site_layout_mode: "wide",
    site_density: "comfortable",
    site_radius_px: 12,
    site_font_scale: 1,
    site_content_width: 1380,
  },
  paper: {
    site_theme_mode: "light",
    site_font_family: "serif",
    site_background_style: "flat",
    site_panel_style: "solid",
    site_sidebar_width: "compact",
    site_bg: "#ede7dc",
    site_surface: "#f7f1e6",
    site_accent: "#4b618f",
    site_accent2: "#a8683f",
    site_text: "#2d2a24",
    site_muted: "#625b51",
    site_layout_mode: "centered",
    site_density: "comfortable",
    site_radius_px: 8,
    site_font_scale: 1,
    site_content_width: 1180,
  },
  ocean: {
    site_theme_mode: "dark",
    site_font_family: "system",
    site_background_style: "aurora",
    site_panel_style: "matte",
    site_sidebar_width: "wide",
    site_bg: "#08171c",
    site_surface: "#142831",
    site_accent: "#4fa3c7",
    site_accent2: "#58b99b",
    site_text: "#e8f3f5",
    site_muted: "#9fb9c0",
    site_layout_mode: "wide",
    site_density: "comfortable",
    site_radius_px: 12,
    site_font_scale: 1,
    site_content_width: 1600,
  },
  terminal: {
    site_theme_mode: "dark",
    site_font_family: "mono",
    site_background_style: "grid",
    site_panel_style: "solid",
    site_sidebar_width: "compact",
    site_bg: "#080b0a",
    site_surface: "#111815",
    site_accent: "#78c690",
    site_accent2: "#6aa2c5",
    site_text: "#d9efe0",
    site_muted: "#8faa9a",
    site_layout_mode: "wide",
    site_density: "compact",
    site_radius_px: 8,
    site_font_scale: 0.95,
    site_content_width: 1380,
  },
};
const USER_APPEARANCE_THEME_NEUTRAL_KEYS = ["site_bg", "site_surface", "site_text", "site_muted"];
const USER_APPEARANCE_THEME_PALETTES = {
  dark: {
    site_bg: "#11131d",
    site_surface: "#1b2030",
    site_text: "#eceef8",
    site_muted: "#aeb8cc",
  },
  light: {
    site_bg: "#eef2f6",
    site_surface: "#fbfcfe",
    site_text: "#243041",
    site_muted: "#5c6678",
  },
};
let editingUserOriginalAppearance = {};
let userAppearanceResetPending = false;
const AVATAR_CROPPER_MIN_ZOOM = 0.5;
const AVATAR_CROPPER_MAX_ZOOM = 6;
const avatarCropState = {
  bound: false,
  objectUrl: "",
  hasImage: false,
  naturalWidth: 0,
  naturalHeight: 0,
  baseScale: 1,
  zoom: 1,
  offsetX: 0,
  offsetY: 0,
  dragging: false,
  pointerId: null,
  startX: 0,
  startY: 0,
  startOffsetX: 0,
  startOffsetY: 0,
};

function avatarCropperElements() {
  return {
    cropper: $("edit-avatar-cropper"),
    stage: $("edit-avatar-crop-stage"),
    image: $("edit-avatar-crop-image"),
    box: $("edit-avatar-crop-box"),
    zoom: $("edit-avatar-crop-zoom"),
    center: $("edit-avatar-crop-center"),
  };
}

function clampAvatarValue(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function avatarCroppedUploadFilename(sourceName = "") {
  const base = String(sourceName || "avatar")
    .replace(/\.[^.]+$/, "")
    .replace(/[^\w.-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 80) || "avatar";
  return `${base}_cropped.png`;
}

function normalizedAvatarCropForImage(image, crop = {}) {
  const naturalWidth = Number(image?.naturalWidth || 0);
  const naturalHeight = Number(image?.naturalHeight || 0);
  if (!naturalWidth || !naturalHeight) return null;
  const sideLimit = Math.min(naturalWidth, naturalHeight);
  const rawWidth = Number(crop.width || 0);
  const rawHeight = Number(crop.height || 0);
  const side = clampAvatarValue(Math.floor(Math.min(rawWidth || sideLimit, rawHeight || sideLimit)), 1, sideLimit);
  const x = clampAvatarValue(Math.floor(Number(crop.x || 0)), 0, Math.max(0, naturalWidth - side));
  const y = clampAvatarValue(Math.floor(Number(crop.y || 0)), 0, Math.max(0, naturalHeight - side));
  return { x, y, width: side, height: side };
}

function avatarCanvasToBlob(canvas, type = "image/png", quality = 0.92) {
  return new Promise((resolve, reject) => {
    if (!canvas || typeof canvas.toBlob !== "function") {
      reject(new Error("此瀏覽器不支援頭像裁切輸出"));
      return;
    }
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("無法產生裁切後頭像"));
    }, type, quality);
  });
}

async function buildCroppedAvatarUpload(image, crop = {}, { sourceName = "" } = {}) {
  if (!image || !image.complete) return null;
  const normalizedCrop = normalizedAvatarCropForImage(image, crop);
  if (!normalizedCrop) return null;
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 512;
  const ctx = canvas.getContext("2d", { alpha: true });
  if (!ctx) throw new Error("此瀏覽器不支援頭像裁切輸出");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(
    image,
    normalizedCrop.x,
    normalizedCrop.y,
    normalizedCrop.width,
    normalizedCrop.height,
    0,
    0,
    canvas.width,
    canvas.height
  );
  const blob = await avatarCanvasToBlob(canvas, "image/png");
  return {
    blob,
    filename: avatarCroppedUploadFilename(sourceName),
    serverCrop: { x: 0, y: 0, width: 512, height: 512 },
  };
}

window.buildCroppedAvatarUpload = buildCroppedAvatarUpload;

function setAvatarCropHidden(crop = {}) {
  setUserEditField("edit-avatar-crop-x", crop.x || 0);
  setUserEditField("edit-avatar-crop-y", crop.y || 0);
  setUserEditField("edit-avatar-crop-width", crop.width || 0);
  setUserEditField("edit-avatar-crop-height", crop.height || 0);
}

function resetAvatarCropper() {
  const els = avatarCropperElements();
  if (avatarCropState.objectUrl) {
    URL.revokeObjectURL(avatarCropState.objectUrl);
  }
  avatarCropState.objectUrl = "";
  avatarCropState.hasImage = false;
  avatarCropState.naturalWidth = 0;
  avatarCropState.naturalHeight = 0;
  avatarCropState.baseScale = 1;
  avatarCropState.zoom = 1;
  avatarCropState.offsetX = 0;
  avatarCropState.offsetY = 0;
  avatarCropState.dragging = false;
  avatarCropState.pointerId = null;
  if (els.cropper) els.cropper.hidden = true;
  if (els.image) {
    els.image.removeAttribute("src");
    els.image.style.transform = "";
    els.image.style.left = "";
    els.image.style.top = "";
    els.image.style.width = "";
    els.image.style.height = "";
  }
  if (els.stage) els.stage.classList.remove("is-dragging");
  if (els.zoom) els.zoom.value = "1";
  setAvatarCropHidden();
}

function avatarCropStageMetrics() {
  const { stage } = avatarCropperElements();
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

function avatarCropMinimumZoom(metrics) {
  if (!metrics || !avatarCropState.naturalWidth || !avatarCropState.naturalHeight || !avatarCropState.baseScale) {
    return AVATAR_CROPPER_MIN_ZOOM;
  }
  const minScale = Math.max(
    metrics.cropSize / avatarCropState.naturalWidth,
    metrics.cropSize / avatarCropState.naturalHeight
  );
  return clampAvatarValue(minScale / avatarCropState.baseScale, AVATAR_CROPPER_MIN_ZOOM, 1);
}

function clampAvatarCropOffsets(metrics = avatarCropStageMetrics()) {
  if (!metrics || !avatarCropState.hasImage) return;
  const scale = avatarCropState.baseScale * avatarCropState.zoom;
  const imageWidth = avatarCropState.naturalWidth * scale;
  const imageHeight = avatarCropState.naturalHeight * scale;
  const maxX = Math.max(0, (imageWidth - metrics.cropSize) / 2);
  const maxY = Math.max(0, (imageHeight - metrics.cropSize) / 2);
  avatarCropState.offsetX = clampAvatarValue(avatarCropState.offsetX, -maxX, maxX);
  avatarCropState.offsetY = clampAvatarValue(avatarCropState.offsetY, -maxY, maxY);
}

function syncAvatarCropPayloadFromVisual() {
  const metrics = avatarCropStageMetrics();
  if (!metrics || !avatarCropState.hasImage || !avatarCropState.naturalWidth || !avatarCropState.naturalHeight) {
    return currentAvatarCropPayloadFromFields();
  }
  const scale = avatarCropState.baseScale * avatarCropState.zoom;
  const imageWidth = avatarCropState.naturalWidth * scale;
  const imageHeight = avatarCropState.naturalHeight * scale;
  const imageLeft = (metrics.width / 2 + avatarCropState.offsetX) - imageWidth / 2;
  const imageTop = (metrics.height / 2 + avatarCropState.offsetY) - imageHeight / 2;
  let cropX = Math.round((metrics.cropLeft - imageLeft) / scale);
  let cropY = Math.round((metrics.cropTop - imageTop) / scale);
  let cropSide = Math.round(metrics.cropSize / scale);
  cropSide = clampAvatarValue(cropSide, 1, Math.min(avatarCropState.naturalWidth, avatarCropState.naturalHeight));
  cropX = clampAvatarValue(cropX, 0, Math.max(0, avatarCropState.naturalWidth - cropSide));
  cropY = clampAvatarValue(cropY, 0, Math.max(0, avatarCropState.naturalHeight - cropSide));
  const crop = { x: cropX, y: cropY, width: cropSide, height: cropSide };
  setAvatarCropHidden(crop);
  return crop;
}

function renderAvatarCropper() {
  const els = avatarCropperElements();
  const metrics = avatarCropStageMetrics();
  if (!els.image || !els.box || !metrics || !avatarCropState.hasImage) return;
  avatarCropState.baseScale = Math.max(
    metrics.width / avatarCropState.naturalWidth,
    metrics.height / avatarCropState.naturalHeight
  );
  const minZoom = avatarCropMinimumZoom(metrics);
  if (els.zoom) {
    els.zoom.min = minZoom.toFixed(2);
  }
  avatarCropState.zoom = clampAvatarValue(Number(avatarCropState.zoom) || 1, minZoom, AVATAR_CROPPER_MAX_ZOOM);
  if (els.zoom && Number(els.zoom.value || 1) < minZoom) {
    els.zoom.value = minZoom.toFixed(2);
  }
  clampAvatarCropOffsets(metrics);
  const scale = avatarCropState.baseScale * avatarCropState.zoom;
  const imageWidth = avatarCropState.naturalWidth * scale;
  const imageHeight = avatarCropState.naturalHeight * scale;
  els.image.style.width = `${imageWidth}px`;
  els.image.style.height = `${imageHeight}px`;
  els.image.style.left = `${(metrics.width / 2 + avatarCropState.offsetX) - imageWidth / 2}px`;
  els.image.style.top = `${(metrics.height / 2 + avatarCropState.offsetY) - imageHeight / 2}px`;
  els.image.style.transform = "none";
  els.box.style.width = `${metrics.cropSize}px`;
  els.box.style.height = `${metrics.cropSize}px`;
  els.box.style.left = `${metrics.cropLeft}px`;
  els.box.style.top = `${metrics.cropTop}px`;
  syncAvatarCropPayloadFromVisual();
}

function loadAvatarCropperFromFile(file) {
  if (!file) {
    resetAvatarCropper();
    return;
  }
  if (!/^image\/(png|jpe?g|gif)$/i.test(file.type || "")) {
    resetAvatarCropper();
    setUserEditMsg("頭像僅支援 JPEG / PNG / GIF", false);
    return;
  }
  const els = avatarCropperElements();
  if (!els.cropper || !els.image) return;
  const nextUrl = URL.createObjectURL(file);
  if (avatarCropState.objectUrl) URL.revokeObjectURL(avatarCropState.objectUrl);
  avatarCropState.objectUrl = nextUrl;
  avatarCropState.hasImage = false;
  els.image.onload = () => {
    avatarCropState.naturalWidth = els.image.naturalWidth || 1;
    avatarCropState.naturalHeight = els.image.naturalHeight || 1;
    avatarCropState.zoom = 1;
    avatarCropState.offsetX = 0;
    avatarCropState.offsetY = 0;
    avatarCropState.hasImage = true;
    if (els.zoom) els.zoom.value = "1";
    els.cropper.hidden = false;
    requestAnimationFrame(renderAvatarCropper);
  };
  els.image.onerror = () => {
    resetAvatarCropper();
    setUserEditMsg("無法讀取頭像預覽，請換一張圖片", false);
  };
  els.image.src = nextUrl;
}

function handleAvatarCropperPointerStart(event) {
  const { stage } = avatarCropperElements();
  if (!stage || !avatarCropState.hasImage) return;
  event.preventDefault();
  avatarCropState.dragging = true;
  avatarCropState.pointerId = event.pointerId;
  avatarCropState.startX = event.clientX;
  avatarCropState.startY = event.clientY;
  avatarCropState.startOffsetX = avatarCropState.offsetX;
  avatarCropState.startOffsetY = avatarCropState.offsetY;
  stage.classList.add("is-dragging");
  if (typeof stage.setPointerCapture === "function") {
    stage.setPointerCapture(event.pointerId);
  }
}

function handleAvatarCropperPointerMove(event) {
  if (!avatarCropState.dragging || event.pointerId !== avatarCropState.pointerId) return;
  event.preventDefault();
  avatarCropState.offsetX = avatarCropState.startOffsetX + (event.clientX - avatarCropState.startX);
  avatarCropState.offsetY = avatarCropState.startOffsetY + (event.clientY - avatarCropState.startY);
  renderAvatarCropper();
}

function handleAvatarCropperPointerEnd(event) {
  const { stage } = avatarCropperElements();
  if (!avatarCropState.dragging || event.pointerId !== avatarCropState.pointerId) return;
  avatarCropState.dragging = false;
  avatarCropState.pointerId = null;
  if (stage) stage.classList.remove("is-dragging");
}

function bindAvatarCropperUi() {
  if (avatarCropState.bound) return;
  avatarCropState.bound = true;
  const input = $("edit-user-avatar-file");
  const els = avatarCropperElements();
  if (input) {
    input.addEventListener("change", () => loadAvatarCropperFromFile(input.files?.[0] || null));
  }
  if (els.stage) {
    els.stage.addEventListener("pointerdown", handleAvatarCropperPointerStart);
    els.stage.addEventListener("pointermove", handleAvatarCropperPointerMove);
    els.stage.addEventListener("pointerup", handleAvatarCropperPointerEnd);
    els.stage.addEventListener("pointercancel", handleAvatarCropperPointerEnd);
  }
  if (els.zoom) {
    els.zoom.addEventListener("input", () => {
      avatarCropState.zoom = clampAvatarValue(
        parseFloat(els.zoom.value || "1") || 1,
        avatarCropMinimumZoom(avatarCropStageMetrics()),
        AVATAR_CROPPER_MAX_ZOOM
      );
      renderAvatarCropper();
    });
  }
  if (els.center) {
    els.center.addEventListener("click", () => {
      avatarCropState.zoom = 1;
      avatarCropState.offsetX = 0;
      avatarCropState.offsetY = 0;
      if (els.zoom) els.zoom.value = "1";
      renderAvatarCropper();
    });
  }
  window.addEventListener("resize", () => {
    if (avatarCropState.hasImage) requestAnimationFrame(renderAvatarCropper);
  });
}

function userAppearanceEditorVisible() {
  return !!currentUser && editingUserIsSelf && !!$("edit-user-appearance-section");
}

function userAppearanceFeatureEnabled() {
  return !isFeatureEnabledForUi || isFeatureEnabledForUi("feature_personalization_enabled", true);
}

function baseAppearanceSettings() {
  if (typeof extractSiteAppearanceConfig === "function") {
    return extractSiteAppearanceConfig(globalSiteConfig || siteConfig || {});
  }
  return {};
}

function normalizedUserAppearanceSettings(settings = {}) {
  const base = baseAppearanceSettings();
  const overrides = typeof extractSiteAppearanceConfig === "function"
    ? extractSiteAppearanceConfig(settings)
    : (settings || {});
  return { ...base, ...overrides };
}

function setUserAppearanceFieldValue(key, value) {
  const el = $(USER_APPEARANCE_FIELD_MAP[key]);
  if (!el || value === undefined || value === null || value === "") return;
  el.value = String(value);
}

function populateUserAppearanceEditor(settings = {}) {
  const merged = normalizedUserAppearanceSettings(settings);
  Object.keys(USER_APPEARANCE_FIELD_MAP).forEach((key) => setUserAppearanceFieldValue(key, merged[key]));
  const preset = $("edit-user-appearance-preset");
  if (preset) preset.value = "custom";
}

function collectUserAppearanceSettingsFromEditor() {
  return {
    site_theme_mode: $("edit-user-theme-mode")?.value || "custom",
    site_font_family: $("edit-user-site-font-family")?.value || "system",
    site_background_style: $("edit-user-site-background-style")?.value || "flat",
    site_panel_style: $("edit-user-site-panel-style")?.value || "glass",
    site_sidebar_width: $("edit-user-site-sidebar-width")?.value || "standard",
    site_bg: $("edit-user-site-bg")?.value || "#0f0f1a",
    site_surface: $("edit-user-site-surface")?.value || "#1a1a2e",
    site_accent: $("edit-user-site-accent")?.value || "#6c63ff",
    site_accent2: $("edit-user-site-accent2")?.value || "#00d4aa",
    site_text: $("edit-user-site-text")?.value || "#e0e0f0",
    site_muted: $("edit-user-site-muted")?.value || "#8888aa",
    site_layout_mode: $("edit-user-site-layout-mode")?.value || "centered",
    site_density: $("edit-user-site-density")?.value || "comfortable",
    site_radius_px: parseInt($("edit-user-site-radius-px")?.value || "12", 10) || 12,
    site_font_scale: Number($("edit-user-site-font-scale")?.value || 1) || 1,
    site_content_width: parseInt($("edit-user-site-content-width")?.value || "1380", 10) || 1380,
  };
}

function userQuickThemeTargetMode() {
  const mode = typeof getEffectiveSiteThemeMode === "function"
    ? getEffectiveSiteThemeMode()
    : (siteConfig?.site_theme_mode || "dark");
  return mode === "light" ? "dark" : "light";
}

function updateThemeQuickToggleUi() {
  const button = $("theme-quick-toggle");
  if (!button) return;
  const target = userQuickThemeTargetMode();
  const label = target === "light" ? "切換淺色模式" : "切換夜色模式";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.dataset.nextThemeMode = target;
  const icon = button.querySelector("[data-theme-toggle-icon]");
  if (icon) icon.textContent = target === "light" ? "☀" : "☾";
}

async function toggleUserThemeModeQuickly() {
  if (!currentUser) return;
  if (!userAppearanceFeatureEnabled()) {
    flash($("msg"), "目前 root 已暫停個人外觀覆寫。", false);
    return;
  }
  const button = $("theme-quick-toggle");
  const target = button?.dataset.nextThemeMode || userQuickThemeTargetMode();
  const current = typeof getUserAppearanceConfig === "function" ? getUserAppearanceConfig() : {};
  const nextAppearance = { ...current, site_theme_mode: target };
  USER_APPEARANCE_THEME_NEUTRAL_KEYS.forEach((key) => delete nextAppearance[key]);
  if (button) button.disabled = true;
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/me/appearance", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
      body: JSON.stringify(nextAppearance),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || "外觀切換失敗");
    editingUserOriginalAppearance = json.appearance_settings || nextAppearance;
    if (typeof applySiteConfig === "function") applySiteConfig(editingUserOriginalAppearance, { scope: "user" });
    populateUserAppearanceEditor(editingUserOriginalAppearance);
    updateThemeQuickToggleUi();
    flash($("msg"), target === "light" ? "已切換淺色模式" : "已切換夜色模式", true);
  } catch (err) {
    flash($("msg"), err.message || "外觀切換失敗", false);
  } finally {
    if (button) button.disabled = false;
  }
}

function setUserAppearanceEditorDisabled(disabled) {
  const section = $("edit-user-appearance-section");
  if (!section) return;
  section.querySelectorAll("input, select, button").forEach((el) => {
    if (el.id === "edit-user-appearance-section") return;
    el.disabled = !!disabled;
  });
}

function userAppearanceSignature(settings = {}) {
  const normalized = typeof extractSiteAppearanceConfig === "function"
    ? extractSiteAppearanceConfig(settings)
    : (settings || {});
  return JSON.stringify(Object.keys(USER_APPEARANCE_FIELD_MAP).reduce((acc, key) => {
    if (normalized[key] !== undefined) acc[key] = normalized[key];
    return acc;
  }, {}));
}

function updateUserAppearanceEditorVisibility() {
  const section = $("edit-user-appearance-section");
  const resetBtn = $("edit-user-appearance-reset");
  if (!section) return;
  const status = $("edit-user-appearance-status");
  if (!userAppearanceEditorVisible()) {
    section.style.display = "none";
    if (resetBtn) resetBtn.style.display = "none";
    if (status) status.textContent = "";
    return;
  }
  section.style.display = "";
  section.open = true;
  if (resetBtn) resetBtn.style.display = "";
  const enabled = userAppearanceFeatureEnabled();
  setUserAppearanceEditorDisabled(!enabled);
  if (resetBtn) resetBtn.disabled = !enabled;
  if (status) {
    status.textContent = enabled
      ? "這些設定只會覆寫你自己的畫面；root 仍控制全站預設外觀。"
      : "目前 root 已暫停個人外觀覆寫；你現在仍會看到既有個人外觀，但暫時不能修改或重設。";
    status.style.color = enabled ? "var(--muted)" : "#ffb74d";
  }
}

function previewUserAppearanceEditor() {
  if (!userAppearanceEditorVisible() || !userAppearanceFeatureEnabled()) return;
  if (userAppearanceResetPending) {
    if (typeof clearUserAppearanceConfig === "function") clearUserAppearanceConfig();
    return;
  }
  if (typeof applySiteConfig === "function") {
    applySiteConfig(collectUserAppearanceSettingsFromEditor(), { scope: "user" });
  }
}

function restoreUserAppearancePreviewIfNeeded() {
  if (!editingUserIsSelf || !currentUser) return;
  if (userAppearanceSignature(editingUserOriginalAppearance)) {
    if (typeof applySiteConfig === "function") {
      applySiteConfig(editingUserOriginalAppearance, { scope: "user" });
    }
  } else if (typeof clearUserAppearanceConfig === "function") {
    clearUserAppearanceConfig();
  }
}

function markUserAppearanceEditorChanged() {
  if (!userAppearanceEditorVisible() || !userAppearanceFeatureEnabled()) return;
  userAppearanceResetPending = false;
  const preset = $("edit-user-appearance-preset");
  if (preset) preset.value = "custom";
  previewUserAppearanceEditor();
}

function applyUserThemeModeSelection() {
  if (!userAppearanceEditorVisible() || !userAppearanceFeatureEnabled()) return;
  const mode = $("edit-user-theme-mode")?.value || "custom";
  const palette = USER_APPEARANCE_THEME_PALETTES[mode];
  if (palette) {
    USER_APPEARANCE_THEME_NEUTRAL_KEYS.forEach((key) => setUserAppearanceFieldValue(key, palette[key]));
  }
  markUserAppearanceEditorChanged();
}

function applyUserAppearancePresetSelection() {
  if (!userAppearanceFeatureEnabled()) return;
  const presetKey = $("edit-user-appearance-preset")?.value || "custom";
  if (presetKey === "custom") return;
  const preset = USER_APPEARANCE_PRESETS[presetKey];
  if (!preset) return;
  userAppearanceResetPending = false;
  populateUserAppearanceEditor(preset);
  previewUserAppearanceEditor();
}

function resetUserAppearanceEditorToGlobal() {
  if (!userAppearanceEditorVisible() || !userAppearanceFeatureEnabled()) return;
  userAppearanceResetPending = true;
  populateUserAppearanceEditor({});
  if (typeof clearUserAppearanceConfig === "function") clearUserAppearanceConfig();
  setUserEditMsg("已切回全站預設外觀；按視窗底部的「儲存」後才會寫入帳號。", true);
}

async function saveUserAppearanceSettings() {
  if (!userAppearanceEditorVisible()) return { ok: true, changed: false };
  if (!userAppearanceFeatureEnabled()) {
    return { ok: false, msg: "此功能目前已由 root 關閉：允許使用者覆寫個人外觀" };
  }
  const originalSignature = userAppearanceSignature(editingUserOriginalAppearance);
  const nextAppearance = userAppearanceResetPending ? {} : collectUserAppearanceSettingsFromEditor();
  const nextSignature = userAppearanceSignature(nextAppearance);
  if (originalSignature === nextSignature) return { ok: true, changed: false };
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/me/appearance", {
    method: userAppearanceResetPending ? "DELETE" : "PUT",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf || ""
    },
    body: userAppearanceResetPending ? undefined : JSON.stringify(nextAppearance)
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) return { ok: false, msg: json.msg || "個人外觀儲存失敗" };
  editingUserOriginalAppearance = json.appearance_settings || {};
  userAppearanceResetPending = false;
  if (userAppearanceSignature(editingUserOriginalAppearance)) {
    if (typeof applySiteConfig === "function") {
      applySiteConfig(editingUserOriginalAppearance, { scope: "user" });
    }
  } else if (typeof clearUserAppearanceConfig === "function") {
    clearUserAppearanceConfig();
  }
  return { ok: true, changed: true, reset: !userAppearanceSignature(editingUserOriginalAppearance) };
}

function selectedUserDisplayTimezone() {
  if (typeof normalizeUserDisplayTimezone === "function") {
    return normalizeUserDisplayTimezone($("edit-user-display-timezone")?.value || "auto");
  }
  return $("edit-user-display-timezone")?.value || "auto";
}

function setUserDisplayTimezoneEditorValue(value) {
  const timezone = typeof normalizeUserDisplayTimezone === "function"
    ? normalizeUserDisplayTimezone(value)
    : (value || "auto");
  const accountField = $("edit-user-display-timezone");
  if (accountField) accountField.value = timezone;
  const profileField = $("profile-edit-display-timezone");
  if (profileField) profileField.value = timezone;
  if (typeof setUserDisplayTimezone === "function") setUserDisplayTimezone(timezone);
}

function updateUserPreferencesEditorVisibility() {
  const section = $("edit-user-preferences-section");
  if (!section) return;
  section.style.display = editingUserIsSelf ? "" : "none";
  if (editingUserIsSelf) section.open = true;
}

async function saveUserDisplayTimezoneSetting(nextTimezone) {
  if (!editingUserIsSelf) return { ok: true, changed: false };
  const timezone = typeof normalizeUserDisplayTimezone === "function"
    ? normalizeUserDisplayTimezone(nextTimezone)
    : (nextTimezone || "auto");
  const original = typeof normalizeUserDisplayTimezone === "function"
    ? normalizeUserDisplayTimezone(editingUserOriginal.display_timezone || "auto")
    : (editingUserOriginal.display_timezone || "auto");
  if (timezone === original) return { ok: true, changed: false };
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/users/me/profile", {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ display_timezone: timezone }),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) return { ok: false, msg: json.msg || "顯示時區儲存失敗" };
  const savedTimezone = json.profile?.display_timezone || timezone;
  editingUserOriginal.display_timezone = savedTimezone;
  setUserDisplayTimezoneEditorValue(savedTimezone);
  if (typeof profilePanelCache !== "undefined" && profilePanelCache) {
    profilePanelCache.display_timezone = savedTimezone;
  }
  return { ok: true, changed: true, timezone: savedTimezone };
}

function renderCaptchaChallenge(captcha) {
  const field = $("captcha-field");
  const question = $("captcha-question");
  const image = $("captcha-image");
  const answer = $("captcha-answer");
  const idInput = $("captcha-id");
  const hint = $("captcha-hint");
  if (!field) return;
  const mode = captcha?.mode || "none";
  if (!captcha?.required || mode === "none") {
    field.style.display = "none";
    if (idInput) idInput.value = "";
    if (answer) answer.value = "";
    return;
  }
  field.style.display = "block";
  if (idInput) idInput.value = captcha.captcha_id || "";
  if (answer) answer.value = "";
  if (question) {
    if (mode === "turnstile") question.textContent = "Turnstile 驗證已啟用，請使用部署的 Turnstile widget 完成驗證。";
    else question.textContent = captcha.question || "請完成驗證";
  }
  if (image) {
    if (captcha.image_data_uri) {
      image.src = captcha.image_data_uri;
      image.style.display = "block";
    } else {
      image.removeAttribute("src");
      image.style.display = "none";
    }
  }
  if (hint) {
    if (mode === "turnstile") {
      hint.textContent = captcha.site_key ? "服務端仍需 TURNSTILE_SECRET_KEY；未設定時註冊會提示 root 調整。" : "尚未設定 Turnstile site key。";
    } else {
      hint.textContent = captcha.expires_at ? "驗證碼會過期，送出前若失敗請重新取得。" : "";
    }
  }
}

async function loadCaptchaChallenge() {
  try {
    const res = await apiFetch(API + "/captcha/challenge", { credentials: "same-origin" });
    const json = await res.json().catch(() => ({}));
    if (json.ok) renderCaptchaChallenge(json.captcha || { required: false, mode: "none" });
  } catch (_) {
    const hint = $("captcha-hint");
    if (hint) hint.textContent = "目前無法取得驗證碼，請稍後重試。";
  }
}

async function forceDefaultPasswordChange() {
  if (!currentUserId) return;
  setForcedPasswordChangeState(true);
  await editUser(currentUserId);
  setUserEditMsg("此預設帳號初次登入必須先變更密碼。請輸入目前密碼與新密碼，變更完成後請用新密碼重新登入。", false);
  const cancelBtn = $("user-edit-cancel");
  if (cancelBtn) cancelBtn.style.display = "none";
  const currentPasswordInput = $("edit-user-current-pw");
  if (currentPasswordInput) currentPasswordInput.focus();
}

async function doRegister() {
  const user = $("reg-user").value.trim();
  const pw   = $("reg-pw").value;
  const pwConfirm = $("reg-pw-confirm").value;
  const nickname = $("reg-nickname").value.trim();
  const email = $("reg-email")?.value.trim() || "";
  const realName = $("reg-realname").value.trim();
  const birth = $("reg-birthdate").value;
  const phone = $("reg-phone").value.trim();

  clearRegisterFieldErrors();
  if (!user) { showRegisterError("請填寫帳號", "username"); return; }
  if (user.length < 3) { showRegisterError("帳號至少 3 字元", "username"); return; }
  if (!pw) { showRegisterError("請輸入密碼", "password"); return; }
  if (!pwConfirm) { showRegisterError("請再次輸入密碼", "password_confirm"); return; }
  if (pw !== pwConfirm) { showRegisterError("兩次密碼輸入不一致", "password_confirm"); return; }
  if (!nickname) { showRegisterError("暱稱不可為空", "nickname"); return; }

  if (!/^[a-zA-Z0-9_\-]+$/.test(user)) {
    showRegisterError("帳號只能包含英文、數字、底線、減號", "username");
    return;
  }

  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
    if (!csrf) {
    showRegisterError("安全驗證狀態失效，請重新整理頁面", "", { focus: false });
    setLoading("reg-btn", "reg-spinner", false);
    return;
  }
  setLoading("reg-btn", "reg-spinner", true);
  clearMsg();

  try {
    const res = await apiFetch(API + "/register", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({
        username: user,
        password: pw,
        password_confirm: pwConfirm,
        nickname,
        email,
        real_name: realName,
        birthdate: birth,
        phone,
        captcha_id: $("captcha-id")?.value || "",
        captcha_answer: $("captcha-answer")?.value || "",
        captcha_turnstile_token: $("captcha-turnstile-token")?.value || "",
        csrf_token: csrf
      })
    });
    const json = await res.json();
    if (json.ok) {
      setCsrfToken(null);
      clearRegisterFieldErrors();
      flash($("reg-msg"), "✓ " + sanitize(json.msg), true, { persistent: true });
      setTimeout(() => {
        $("reg-pw").value = "";
        $("reg-pw-confirm").value = "";
        $("reg-pw-hint").textContent = "";
        $("reg-pw-confirm-hint").textContent = "";
      }, 1500);
    } else {
      setCsrfToken(null);
      showRegisterError(json.msg || "註冊失敗", json.field || "");
      loadCaptchaChallenge();
    }
  } catch (e) {
    showRegisterError("網路錯誤，請稍後再試", "", { focus: false });
    loadCaptchaChallenge();
  } finally {
    setLoading("reg-btn", "reg-spinner", false);
  }
}

function bindRegisterFieldHelpers() {
  Object.values(REGISTER_FIELD_ID_MAP).forEach((id) => {
    const el = $(id);
    if (!el || el.dataset.registerFieldBound === "1") return;
    const clearHandler = () => {
      el.classList.remove("field-error");
      el.removeAttribute("aria-invalid");
    };
    el.addEventListener("input", clearHandler);
    el.addEventListener("change", clearHandler);
    el.dataset.registerFieldBound = "1";
  });
  bindRegisterAutofillGuards();
}

function bindRegisterAutofillGuards() {
  ["reg-user", "reg-pw", "reg-pw-confirm"].forEach((id) => {
    const input = $(id);
    if (!input) return;
    input.autocomplete = "off";
    input.setAttribute("data-lpignore", "true");
    input.setAttribute("data-1p-ignore", "true");
    input.dataset.formType = "other";
    if (input.dataset.registerAutofillGuardBound === "1") return;
    const unlock = () => {
      input.readOnly = false;
    };
    input.readOnly = true;
    input.addEventListener("focus", unlock);
    input.addEventListener("pointerdown", unlock);
    input.addEventListener("keydown", unlock);
    input.dataset.registerAutofillGuardBound = "1";
  });
}

function setRecoveryMsg(text, ok) {
  flash($("recovery-msg"), text, ok);
}

function updateRecoveryModeUi() {
  const mode = (siteConfig && siteConfig.password_reset_mode) || "admin_review";
  const emailTokenMode = mode === "email_token";
  const requestBtn = $("reset-request-btn");
  if (requestBtn) requestBtn.textContent = emailTokenMode ? "寄送重設密碼驗證碼" : "送出重設密碼審核";
  ["reset-token-field", "reset-new-pw-field", "reset-new-pw-confirm-field", "reset-confirm-btn"].forEach((id) => {
    const el = $(id);
    if (el) el.style.display = emailTokenMode ? "" : "none";
  });
}

function toggleRecoveryPanel() {
  const panel = $("recovery-panel");
  if (!panel) return;
  panel.classList.toggle("show");
  const source = $("li-user");
  const target = $("recovery-identifier");
  if (panel.classList.contains("show") && source && target && !target.value.trim()) {
    target.value = source.value.trim();
  }
}

async function postRecoveryAction(path, payload) {
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  if (!csrf) {
    setRecoveryMsg("安全驗證狀態失效，請重新整理頁面", false);
    return null;
  }
  const res = await apiFetch(API + path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ ...payload, csrf_token: csrf })
  });
  setCsrfToken(null);
  return res.json().catch(() => ({ ok: false, msg: "回應格式錯誤" }));
}

async function requestPasswordReset() {
  const identifier = $("recovery-identifier")?.value.trim() || "";
  if (!identifier) {
    setRecoveryMsg("請填寫帳號或 Email", false);
    return;
  }
  try {
    const json = await postRecoveryAction("/password-reset/request", { username_or_email: identifier });
    if (!json) return;
    const fallback = ((siteConfig && siteConfig.password_reset_mode) === "email_token")
      ? "如果資料符合，系統會寄出後續操作通知"
      : "如果資料符合，系統會建立密碼重設審核申請";
    setRecoveryMsg(json.msg || fallback, Boolean(json.ok));
  } catch (_) {
    setRecoveryMsg("網路錯誤，請稍後再試", false);
  }
}

async function confirmPasswordReset() {
  const resetCode = $("reset-token")?.value.trim() || "";
  const newPasswordValue = $("reset-new-pw")?.value || "";
  const passwordConfirm = $("reset-new-pw-confirm")?.value || "";
  if (!resetCode) {
    setRecoveryMsg("請填寫重設密碼驗證碼", false);
    return;
  }
  if (!newPasswordValue || !passwordConfirm) {
    setRecoveryMsg("請填寫新密碼與確認密碼", false);
    return;
  }
  if (newPasswordValue !== passwordConfirm) {
    setRecoveryMsg("兩次密碼輸入不一致", false);
    return;
  }
  try {
    const json = await postRecoveryAction("/password-reset/confirm", {
      token: resetCode,
      password: newPasswordValue,
      password_confirm: passwordConfirm
    });
    if (!json) return;
    setRecoveryMsg(json.msg || (json.ok ? "密碼已重設" : "重設失敗"), Boolean(json.ok));
    if (json.ok) {
      $("reset-token").value = "";
      $("reset-new-pw").value = "";
      $("reset-new-pw-confirm").value = "";
    }
  } catch (_) {
    setRecoveryMsg("網路錯誤，請稍後再試", false);
  }
}

async function requestEmailVerification() {
  const identifier = $("recovery-identifier")?.value.trim() || "";
  if (!identifier) {
    setRecoveryMsg("請填寫帳號或 Email", false);
    return;
  }
  try {
    const json = await postRecoveryAction("/email-verification/request", { username_or_email: identifier });
    if (!json) return;
    setRecoveryMsg(json.msg || "如果資料符合，系統會寄出後續操作通知", Boolean(json.ok));
  } catch (_) {
    setRecoveryMsg("網路錯誤，請稍後再試", false);
  }
}

async function confirmEmailVerification() {
  const verifyCode = $("verify-token")?.value.trim() || "";
  if (!verifyCode) {
    setRecoveryMsg("請填寫 Email 驗證碼", false);
    return;
  }
  try {
    const json = await postRecoveryAction("/email-verification/confirm", { token: verifyCode });
    if (!json) return;
    setRecoveryMsg(json.msg || (json.ok ? "Email 已完成驗證" : "驗證失敗"), Boolean(json.ok));
    if (json.ok) $("verify-token").value = "";
  } catch (_) {
    setRecoveryMsg("網路錯誤，請稍後再試", false);
  }
}

function bindAuthRecoveryControls() {
  const bindings = [
    ["recovery-toggle", toggleRecoveryPanel],
    ["reset-request-btn", requestPasswordReset],
    ["reset-confirm-btn", confirmPasswordReset],
    ["verify-request-btn", requestEmailVerification],
    ["verify-confirm-btn", confirmEmailVerification],
  ];
  bindings.forEach(([id, handler]) => {
    const el = $(id);
    if (!el || el.dataset.authRecoveryBound === "1") return;
    el.dataset.authRecoveryBound = "1";
    el.addEventListener("click", handler);
  });
  if (typeof setupPwToggle === "function") {
    setupPwToggle("reset-new-pw", "reset-new-pw-toggle");
    setupPwToggle("reset-new-pw-confirm", "reset-new-pw-confirm-toggle");
  }
  updateRecoveryModeUi();
}

bindAuthRecoveryControls();

async function doLogout(options = {}) {
  const immediate = !!(options && options.immediate === true);
  if (!immediate && typeof hasActiveDriveUploads === "function" && hasActiveDriveUploads()) {
    const ok = confirm("目前仍有檔案上傳中。瀏覽器限制導致登出後上傳無法可靠完成，確定要登出嗎？");
    if (!ok) return;
  }
  if (immediate) showLoginScreen();
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    if (!res.ok && !immediate) {
      flash($("li-msg"), "登出失敗，請稍後再試", false);
    }
  } catch (_) {}
  setCsrfToken(null);
  if (typeof clearDriveE2eeSessionPassphrases === "function") clearDriveE2eeSessionPassphrases();
  resetAuthState();
}

async function forceIdleTimeoutLogout() {
  markIdleTimeoutLogoutPending();
  showLoginScreen();
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/session/idle-timeout", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "X-Idle-Timeout-Logout": "1",
        "X-CSRF-Token": getCsrfToken() || "",
      }
    });
    if (res.ok) clearIdleTimeoutLogoutPending();
  } catch (_) {}
  setCsrfToken(null);
  resetAuthState();
}

async function toggleBlock(userId, isBlocked) {
  if (!currentUser) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const body = isBlocked ? { action: "unblock" } : { action: "block", minutes: 30 };
  const res = await apiFetch(API + `/admin/users/${userId}/block`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(body)
  });
  const json = await res.json().catch(() => ({}));
  if (json && json.ok) {
    loadUsers();
  } else {
    flash($("li-msg"), json.msg || "操作失敗", false);
  }
}

async function editUser(userId) {
  const target = users.find((u) => String(u.id) === String(userId));
  if (!target && String(currentUserId || "") !== String(userId)) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();

  let source = target || {};
  let detailRes = null;
  if (csrf) {
    detailRes = await apiFetch(API + `/admin/users/${userId}`, {
      method: "GET",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    }).then((r) => r.json().catch(() => ({})));
    if (detailRes && detailRes.ok && detailRes.user) {
      source = detailRes.user;
    }
  }

  editingUserIsSelf = String(currentUserId || "") === String(userId);
  const current = {
    username: source.username || currentUser || "",
    nickname: source.nickname || "",
    real_name: source.real_name || "",
    id_number: source.id_number || "",
    birthdate: source.birthdate || "",
    phone: source.phone || "",
    avatar_file_id: source.avatar_file_id || "",
    avatar_crop: source.avatar_crop || {},
    role: source.role || "user",
    status: source.status || "active",
    base_level: source.base_level || source.member_level || "normal",
    effective_level: source.effective_level || source.base_level || source.member_level || "normal",
    sanction_status: source.sanction_status || "none",
    sanction_until: source.sanction_until || "",
    display_timezone: source.display_timezone || (editingUserIsSelf && typeof getUserDisplayTimezone === "function" ? getUserDisplayTimezone() : "auto"),
  };

  editingUserId = userId;
  editingUserOriginal.nickname = current.nickname;
  editingUserOriginal.real_name = current.real_name;
  editingUserOriginal.id_number = current.id_number;
  editingUserOriginal.birthdate = current.birthdate;
  editingUserOriginal.phone = current.phone;
  editingUserOriginal.role = current.role;
  editingUserOriginal.status = current.status;
  editingUserOriginal.base_level = current.base_level;
  editingUserOriginal.sanction_status = current.sanction_status;
  editingUserOriginal.sanction_until = current.sanction_until;
  editingUserOriginal.display_timezone = current.display_timezone || "auto";
  editingUserOriginalAppearance = detailRes?.appearance_settings || {};
  userAppearanceResetPending = false;

  const usernameEl = $("user-edit-username");
  if (usernameEl) usernameEl.textContent = current.username || String(userId);
  const memberLevelEl = $("user-edit-member-level");
  if (memberLevelEl) {
    memberLevelEl.textContent = current.base_level === current.effective_level
      ? current.effective_level
      : `${current.effective_level}（原始等級：${current.base_level}）`;
  }
  setUserEditField("edit-user-nickname", current.nickname);
  setUserEditField("edit-user-realname", current.real_name);
  setUserEditField("edit-user-idno", current.id_number);
  setUserEditField("edit-user-birthdate", current.birthdate);
  setUserEditField("edit-user-phone", current.phone);
  setUserEditField("edit-avatar-crop-x", current.avatar_crop.x ?? 0);
  setUserEditField("edit-avatar-crop-y", current.avatar_crop.y ?? 0);
  setUserEditField("edit-avatar-crop-width", current.avatar_crop.width ?? 0);
  setUserEditField("edit-avatar-crop-height", current.avatar_crop.height ?? 0);
  setUserEditField("edit-user-avatar-file", "");
  resetAvatarCropper();
  const avatarStatus = $("edit-user-avatar-status");
  if (avatarStatus) avatarStatus.textContent = current.avatar_file_id ? `目前頭像 file_id: ${current.avatar_file_id}` : "尚未設定頭像";
  const editRole = $("edit-user-role");
  if (editRole) editRole.value = current.role;
  const editStatus = $("edit-user-status");
  if (editStatus) editStatus.value = current.status;
  const roleField = $("edit-user-role-field");
  const statusField = $("edit-user-status-field");
  const currentPwField = $("edit-user-current-pw-field");
  if (roleField) roleField.style.display = editingUserIsSelf || !canManageUsers ? "none" : "";
  if (statusField) statusField.style.display = editingUserIsSelf || !canManageUsers ? "none" : "";
  if (currentPwField) currentPwField.style.display = editingUserIsSelf ? "" : "none";
  updateUserPreferencesEditorVisibility();
  if (editingUserIsSelf) setUserDisplayTimezoneEditorValue(current.display_timezone || "auto");
  updateUserAppearanceEditorVisibility();
  populateUserAppearanceEditor(editingUserOriginalAppearance);
  const memberFields = $("edit-user-member-level-fields");
  if (memberFields) memberFields.style.display = editingUserIsSelf || currentUser !== "root" ? "none" : "";
  const governanceDispositionFields = $("edit-user-governance-disposition-fields");
  if (governanceDispositionFields) {
    governanceDispositionFields.style.display = editingUserIsSelf || !["manager", "super_admin"].includes(currentRole) ? "none" : "";
  }
  setUserEditField("edit-user-base-level", current.base_level);
  setUserEditField("edit-user-sanction-status", current.sanction_status);
  setUserEditField("edit-user-sanction-until", current.sanction_until ? current.sanction_until.slice(0, 16) : "");
  setUserEditField("edit-user-level-reason", "");
  setUserEditField("edit-user-restriction-features", "");
  setUserEditField("edit-user-fine-amount", "");
  setUserEditField("edit-user-fine-due-hours", "72");
  setUserEditField("edit-user-governance-disposition-reason", "");
  const cancelBtn = $("user-edit-cancel");
  if (cancelBtn) cancelBtn.style.display = forcedPasswordChangeMode ? "none" : "";
  setUserEditField("edit-user-current-pw", "");
  setUserEditField("edit-user-pw", "");
  setUserEditField("edit-user-pw-confirm", "");
  setUserEditMsg("");

  const overlay = $("user-edit-overlay");
  if (overlay) overlay.classList.add("show");
  const firstField = $("edit-user-nickname");
  if (firstField) firstField.focus();
}

function selectedUserAvatarFile() {
  return $("edit-user-avatar-file")?.files?.[0] || null;
}

function currentAvatarCropPayloadFromFields() {
  return {
    x: parseInt($("edit-avatar-crop-x")?.value || "0", 10) || 0,
    y: parseInt($("edit-avatar-crop-y")?.value || "0", 10) || 0,
    width: parseInt($("edit-avatar-crop-width")?.value || "0", 10) || 0,
    height: parseInt($("edit-avatar-crop-height")?.value || "0", 10) || 0,
  };
}

function currentAvatarCropPayload() {
  return avatarCropState.hasImage ? syncAvatarCropPayloadFromVisual() : currentAvatarCropPayloadFromFields();
}

async function submitUserAvatarUpload({ reloadUsers = true } = {}) {
  if (!editingUserId) return;
  const input = $("edit-user-avatar-file");
  const file = selectedUserAvatarFile();
  if (!file) {
    setUserEditMsg("請先選擇頭像檔案", false);
    return { ok: false, msg: "請先選擇頭像檔案" };
  }
  const crop = currentAvatarCropPayload();
  const image = avatarCropperElements().image;
  if (file && !avatarCropState.hasImage) {
    setUserEditMsg("請等待頭像裁切預覽載入完成後再上傳", false);
    return { ok: false, msg: "請等待頭像裁切預覽載入完成後再上傳" };
  }
  const cropped = avatarCropState.hasImage
    ? await buildCroppedAvatarUpload(image, crop, { sourceName: file.name || "avatar.png" })
    : null;
  if (avatarCropState.hasImage && !cropped) {
    setUserEditMsg("無法產生裁切後頭像，請重新選擇圖片", false);
    return { ok: false, msg: "無法產生裁切後頭像" };
  }
  const form = new FormData();
  if (cropped) {
    form.append("file", cropped.blob, cropped.filename);
    form.append("crop_json", JSON.stringify(cropped.serverCrop));
    form.append("avatar_client_cropped", "1");
  } else {
    form.append("file", file);
    form.append("crop_json", JSON.stringify(crop));
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/admin/users/${editingUserId}/avatar`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" },
    body: form
  });
  const json = await res.json().catch(() => ({}));
  const status = $("edit-user-avatar-status");
  if (json && json.ok) {
    if (status) status.textContent = `頭像已更新 file_id: ${json.avatar_file_id}`;
    if (input) input.value = "";
    resetAvatarCropper();
    markUserAvatarUpdated(editingUserId, json.avatar_file_id || "");
    setUserEditMsg("頭像已更新", true);
    if (reloadUsers && ["manager", "super_admin"].includes(currentRole)) loadUsers();
  } else {
    setUserEditMsg(json.msg || "頭像上傳失敗", false);
  }
  return json;
}

async function uploadUserAvatar() {
  await submitUserAvatarUpload({ reloadUsers: true });
}

async function submitEditUser() {
  if (!editingUserId) return;

  const payload = {};
  const nickname = $("edit-user-nickname")?.value.trim() || "";
  const realName = $("edit-user-realname")?.value.trim() || "";
  const idNoField = $("edit-user-idno");
  const idNo = idNoField ? idNoField.value.trim() : (editingUserOriginal.id_number || "");
  const birthdate = $("edit-user-birthdate")?.value || "";
  const phone = $("edit-user-phone")?.value.trim() || "";
  const role = $("edit-user-role")?.value || "";
  const status = $("edit-user-status")?.value || "";
  const baseLevel = $("edit-user-base-level")?.value || "";
  const sanctionStatus = $("edit-user-sanction-status")?.value || "none";
  const sanctionUntil = $("edit-user-sanction-until")?.value || "";
  const levelReason = $("edit-user-level-reason")?.value.trim() || "";
  const restrictionFeaturesRaw = $("edit-user-restriction-features")?.value.trim() || "";
  const fineAmountRaw = $("edit-user-fine-amount")?.value || "";
  const fineDueHoursRaw = $("edit-user-fine-due-hours")?.value || "";
  const governanceDispositionReason = $("edit-user-governance-disposition-reason")?.value.trim() || "";
  const currentPassword = $("edit-user-current-pw")?.value || "";
  const nextPasswordValue = $("edit-user-pw")?.value || "";
  const passwordConfirm = $("edit-user-pw-confirm")?.value || "";
  const avatarFile = selectedUserAvatarFile();
  const timezoneEditor = $("edit-user-display-timezone");
  const displayTimezone = timezoneEditor ? selectedUserDisplayTimezone() : (editingUserOriginal.display_timezone || "auto");
  const appearanceChanged = userAppearanceEditorVisible() && editingUserIsSelf && (
    userAppearanceResetPending ||
    userAppearanceSignature(collectUserAppearanceSettingsFromEditor()) !== userAppearanceSignature(editingUserOriginalAppearance)
  );
  const timezoneChanged = !!timezoneEditor && editingUserIsSelf && displayTimezone !== (
    typeof normalizeUserDisplayTimezone === "function"
      ? normalizeUserDisplayTimezone(editingUserOriginal.display_timezone || "auto")
      : (editingUserOriginal.display_timezone || "auto")
  );

  if (nickname !== editingUserOriginal.nickname) payload.nickname = nickname;
  if (realName !== editingUserOriginal.real_name) payload.real_name = realName;
  if (idNoField && idNo !== editingUserOriginal.id_number) payload.id_number = idNo;
  if (birthdate !== editingUserOriginal.birthdate) payload.birthdate = birthdate;
  if (phone !== editingUserOriginal.phone) payload.phone = phone;
  if (!editingUserIsSelf && canManageUsers && role !== editingUserOriginal.role) payload.role = role;
  if (!editingUserIsSelf && canManageUsers && status !== editingUserOriginal.status) payload.status = status;
  if (!editingUserIsSelf && currentUser === "root") {
    if (baseLevel && baseLevel !== editingUserOriginal.base_level) payload.base_level = baseLevel;
    if (sanctionStatus !== editingUserOriginal.sanction_status) payload.sanction_status = sanctionStatus;
    const normalizedOriginalUntil = (editingUserOriginal.sanction_until || "").slice(0, 16);
    if (sanctionUntil !== normalizedOriginalUntil) payload.sanction_until = sanctionUntil ? new Date(sanctionUntil).toISOString() : "";
    if (payload.base_level || payload.sanction_status || Object.prototype.hasOwnProperty.call(payload, "sanction_until")) {
      payload.level_update_reason = levelReason || "root manual member level update";
    }
  }
  if (!editingUserIsSelf && ["manager", "super_admin"].includes(currentRole)) {
    const restrictionFeatures = restrictionFeaturesRaw
      ? restrictionFeaturesRaw.split(",").map((item) => item.trim()).filter(Boolean)
      : [];
    const fineAmount = parseInt(fineAmountRaw, 10) || 0;
    const fineDueHours = parseInt(fineDueHoursRaw, 10) || 72;
    if (restrictionFeatures.length) payload.restriction_features = restrictionFeatures;
    if (fineAmount > 0) {
      payload.fine_amount_points = fineAmount;
      payload.fine_due_hours = fineDueHours;
    }
    if ((restrictionFeatures.length || fineAmount > 0) && governanceDispositionReason) {
      payload.governance_disposition_reason = governanceDispositionReason;
    }
  }

  if (nextPasswordValue || passwordConfirm) {
    if (nextPasswordValue !== passwordConfirm) {
      setUserEditMsg("兩次密碼輸入不一致", false);
      return;
    }
    if (!nextPasswordValue) {
      setUserEditMsg("若要修改密碼，兩次都要輸入", false);
      return;
    }
    if (editingUserIsSelf && !currentPassword) {
      setUserEditMsg("修改自己的密碼時必須輸入目前密碼", false);
      return;
    }
    const passwordField = "password";
    payload[passwordField] = nextPasswordValue;
    payload.password_confirm = passwordConfirm;
    if (editingUserIsSelf) payload.current_password = currentPassword;
  }

  if (forcedPasswordChangeMode && !payload.password) {
    setUserEditMsg("初次登入必須設定新密碼", false);
    return;
  }

  if (!Object.keys(payload).length && !avatarFile && !appearanceChanged && !timezoneChanged) {
    setUserEditMsg("未變更任何欄位", false);
    return;
  }

  if (Object.keys(payload).length) {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + `/admin/users/${editingUserId}`, {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    if (!json || !json.ok) {
      setUserEditMsg(json.msg || "修改失敗", false);
      return;
    }
    if (forcedPasswordChangeMode) {
      setForcedPasswordChangeState(false);
      alert("密碼已更新，請使用新密碼重新登入。");
      resetAuthState();
      return;
    }
  }

  if (appearanceChanged) {
    const appearanceJson = await saveUserAppearanceSettings();
    if (!appearanceJson.ok) {
      setUserEditMsg(appearanceJson.msg || "個人外觀儲存失敗", false);
      return;
    }
  }

  if (timezoneChanged) {
    const timezoneJson = await saveUserDisplayTimezoneSetting(displayTimezone);
    if (!timezoneJson.ok) {
      setUserEditMsg(timezoneJson.msg || "顯示時區儲存失敗", false);
      return;
    }
  }

  if (avatarFile) {
    const avatarJson = await submitUserAvatarUpload({ reloadUsers: false });
    if (!avatarJson || !avatarJson.ok) return;
  }

  hideUserEditDialog();
  if (["manager", "super_admin"].includes(currentRole)) loadUsers();
}

async function removeUser(userId) {
  const target = Array.isArray(users) ? users.find((u) => String(u.id) === String(userId)) : null;
  const label = target?.username ? `「${target.username}」` : "這個帳號";
  const pending = target?.status === "pending";
  const confirmText = pending
    ? `確定要刪除待審核帳號 ${label}？此操作會移除該註冊申請。`
    : `確定要刪除帳號 ${label}？`;
  if (!window.confirm(confirmText)) return;
  const msgEl = typeof adminUsersMsgEl === "function" ? adminUsersMsgEl() : $("li-msg");
  try {
    flash(msgEl, "正在刪除帳號...", true);
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + `/admin/users/${userId}`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (json && json.ok) {
      await loadUsers();
      flash(msgEl, json.msg || "帳號已刪除", true);
    } else {
      flash(msgEl, json.msg || `刪除失敗（HTTP ${res.status}）`, false);
    }
  } catch (err) {
    flash(msgEl, err.message || "刪除失敗，請稍後再試", false);
  }
}

async function createUserByAdmin() {
  const pageMsgEl = typeof adminUsersMsgEl === "function" ? adminUsersMsgEl() : $("li-msg");
  const dialogMsgEl = adminAddMsgEl();
  const payload = {
    username: sanitize($("admin-add-user").value.trim()),
    password: $("admin-add-pw").value,
    password_confirm: $("admin-add-pw-confirm").value,
    nickname: $("admin-add-nickname").value.trim(),
    real_name: $("admin-add-realname").value.trim(),
    id_number: $("admin-add-idno").value.trim(),
    birthdate: $("admin-add-birthdate").value,
    phone: $("admin-add-phone").value.trim(),
    role: "user",
    status: "active"
  };
  if (!payload.username || !payload.password || !payload.password_confirm || !payload.nickname) {
    const missingField = !payload.username ? $("admin-add-user")
      : !payload.password ? $("admin-add-pw")
        : !payload.password_confirm ? $("admin-add-pw-confirm")
          : $("admin-add-nickname");
    flash(dialogMsgEl || pageMsgEl, "請至少填寫帳號、密碼、確認密碼與暱稱", false);
    if (missingField) missingField.focus();
    return;
  }
  if (payload.password !== payload.password_confirm) {
    flash(dialogMsgEl || pageMsgEl, "兩次輸入的密碼不一致", false);
    const confirmField = $("admin-add-pw-confirm");
    if (confirmField) confirmField.focus();
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/users", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(payload)
  });
  const json = await res.json().catch(() => ({}));
  if (json && json.ok) {
    ["admin-add-user", "admin-add-pw", "admin-add-pw-confirm", "admin-add-nickname", "admin-add-realname", "admin-add-idno", "admin-add-birthdate", "admin-add-phone"]
      .forEach((id) => { const el = $(id); if (el) el.value = ""; });
    const adminAddHint = $("admin-add-pw-confirm-hint");
    if (adminAddHint) adminAddHint.textContent = "";
    clearAdminAddMsg();
    hideAdminAddDialog();
    await loadUsers();
    flash(pageMsgEl, json.msg || "帳號已建立", true);
  } else {
    flash(dialogMsgEl || pageMsgEl, json.msg || "建立帳號失敗", false);
  }
}

function adminAddMsgEl() {
  return $("admin-add-msg");
}

function clearAdminAddMsg() {
  const el = adminAddMsgEl();
  if (!el) return;
  el.textContent = "";
  el.className = "msg";
}

function showAdminAddDialog() {
  const overlay = $("admin-add-overlay");
  if (!overlay) return;
  clearAdminAddMsg();
  overlay.classList.add("show");
  const firstField = $("admin-add-user");
  if (firstField) firstField.focus();
}

function hideAdminAddDialog() {
  const overlay = $("admin-add-overlay");
  if (!overlay) return;
  overlay.classList.remove("show");
  clearAdminAddMsg();
}

async function reviewRegistration(userId, action) {
  const label = action === "approve" ? "核准" : "駁回";
  if (!confirm(`確定要${label}這筆註冊申請？`)) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/admin/users/${userId}/review-registration`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ action })
  });
  const json = await res.json().catch(() => ({}));
  if (json && json.ok) {
    await loadUsers();
    flash(typeof adminUsersMsgEl === "function" ? adminUsersMsgEl() : $("li-msg"), json.msg || `${label}完成`, true);
  } else {
    flash(typeof adminUsersMsgEl === "function" ? adminUsersMsgEl() : $("li-msg"), json.msg || "審核失敗", false);
  }
}

async function bulkReviewRegistrations(action) {
  const ids = [...selectedPendingUserIds];
  if (!ids.length) return;
  const label = action === "approve" ? "核准" : "駁回";
  if (!confirm(`確定要${label}這 ${ids.length} 筆註冊申請？`)) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  let success = 0;
  let failed = 0;
  for (const userId of ids) {
    try {
      const res = await apiFetch(API + `/admin/users/${userId}/review-registration`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
        body: JSON.stringify({ action })
      });
      const json = await res.json().catch(() => ({}));
      if (json && json.ok) success += 1;
      else failed += 1;
    } catch (_) {
      failed += 1;
    }
  }
  selectedPendingUserIds.clear();
  await loadUsers();
  if (failed === 0) {
    flash(typeof adminUsersMsgEl === "function" ? adminUsersMsgEl() : $("li-msg"), `${label}完成，共 ${success} 筆`, true);
  } else {
    flash(typeof adminUsersMsgEl === "function" ? adminUsersMsgEl() : $("li-msg"), `${label}完成 ${success} 筆，失敗 ${failed} 筆`, false);
  }
}

async function promoteUser(userId, username) {
  const msgEl = typeof adminUsersMsgEl === "function" ? adminUsersMsgEl() : $("li-msg");
  if (!confirm(`確定要將「${username}」升級為管理者？`)) return;
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/admin/users/" + userId + "/promote", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({})
    });
    const json = await res.json().catch(() => ({}));
    if (json.ok) {
      await loadUsers();
      flash(msgEl, json.msg || "升級完成", true);
    } else {
      flash(msgEl, json.msg || `升級失敗（HTTP ${res.status}）`, false);
    }
  } catch (err) {
    flash(msgEl, err.message || "升級失敗，請稍後再試", false);
  }
}

async function updateUserMemberLevel(userId, username) {
  const select = $(`member-level-select-${userId}`);
  const level = select?.value || "";
  const allowed = currentRole === "super_admin"
    ? ["newbie", "normal", "trusted", "vip", "restricted", "suspended"]
    : ["newbie", "normal", "trusted", "vip"];
  if (!allowed.includes(level)) {
    alert("請選擇有效的會員等級");
    return;
  }
  if (!confirm(`確定要將「${username}」的會員等級調整為 ${level}？`)) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/admin/users/${userId}`, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify(
      ["restricted", "suspended"].includes(level)
        ? { sanction_status: level, level_update_reason: `root sanction change to ${level}` }
        : { base_level: level, sanction_status: currentRole === "super_admin" ? "none" : undefined, level_update_reason: `admin member level change to ${level}` }
    )
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    await loadUsers();
    return;
  }
  alert(json.msg || "會員等級更新失敗");
}

async function demoteUser(userId, username, currentRole) {
  const msg = currentRole === "manager"
    ? `確定要將「${username}」降級為一般用戶？`
    : `確定要刪除「${username}」（一般用戶，達違規上限）？`;
  if (!confirm(msg)) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/users/" + userId + "/demote", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({})
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    loadUsers();
  } else {
    alert(json.msg || "降級失敗");
  }
}

// ── Module / admin tab switching ─────────────────────────────────────
let currentAdminTab = "users";
