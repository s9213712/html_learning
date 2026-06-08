'use strict';

let comfyuiCurrentImage = null;
let comfyuiGeneratedImages = [];
let comfyuiGeneratedMedia = [];
let comfyuiSelectedImageIndex = 0;
let comfyuiSavedResult = null;
let comfyuiModelsLoaded = false;
let comfyuiModelsLoadPromise = null;
let comfyuiModelsLastLoadedAt = 0;
let comfyuiModelsLastStatusText = "";
let comfyuiServerAvailable = null;
let comfyuiAlbumsLoaded = false;
let comfyuiProgressTimer = null;
let comfyuiProgressStartedAt = 0;
let comfyuiProgressPythonLogTail = [];
let comfyuiProgressBackendKind = "";
let comfyuiResourceDashboardTimer = null;
let comfyuiResourceDashboardInFlight = false;
let comfyuiGenerateAbortController = null;
let comfyuiActiveJobId = null;
let comfyuiMaxBatchSize = 1;
let comfyuiBillingQuote = null;
let comfyuiDefaultWidth = 1024;
let comfyuiDefaultHeight = 1024;
let comfyuiAvailableCheckpoints = [];
let comfyuiAvailableLoras = [];
let comfyuiLoraDetails = {};
let comfyuiAvailableEmbeddings = [];
let comfyuiAvailableVaes = [];
let comfyuiAvailableDiffusionModels = [];
let comfyuiAvailableClipModels = [];
let comfyuiAvailableClipVisionModels = [];
let comfyuiAvailableLatentUpscaleModels = [];
let comfyuiAvailableControlnetModels = [];
let comfyuiSelectedLoras = [];
let comfyuiConnectionMode = "remote";
let comfyuiActiveBackendFamily = "comfyui";
let comfyuiLocalRuntimeActive = false;
let comfyuiLocalStartPollTimer = null;
let comfyuiCivitaiInspection = null;
let comfyuiCivitaiSearchResults = [];
let comfyuiControlnetTypes = {};
let comfyuiUpscaleModels = [];
let comfyuiGenerationModes = [];
let comfyuiModelFamilies = [];
let comfyuiDiffusersInspection = null;
let comfyuiGgufProfiles = [];
let comfyuiInstalledGgufModels = [];
let comfyuiGgufProfilesLoadPromise = null;
const comfyuiDiffusersInspectCache = new Map();
const comfyuiDiffusersInspectInflight = new Map();
let comfyuiHistoryItems = [];
let comfyuiImageFavorites = [];
let comfyuiWorkflowPresets = [];
let comfyuiWorkflowCurrentPresetId = null;
let comfyuiWorkflowEditorDefaults = null;
let comfyuiSelectedTemplatePresetId = null;
let comfyuiSelectedTemplateDetail = null;
let comfyuiLastFocusedPromptType = "prompt";
let comfyuiInputAssets = {
  source: { file: null, imageRef: null, previewUrl: "", filename: "", cloudFileId: "" },
  mask: { file: null, imageRef: null, previewUrl: "", filename: "", cloudFileId: "" },
  control: { file: null, imageRef: null, previewUrl: "", filename: "", cloudFileId: "" },
};
let comfyuiMaskEditorState = {
  open: false,
  mode: "paint",
  drawing: false,
  lastPoint: null,
  pointerId: null,
  width: 0,
  height: 0,
  hasSource: false,
};
let comfyuiImagePickerState = {
  open: false,
  targetKey: "source",
  history: [],
  cloudDrive: [],
};
const COMFYUI_GENERATION_TIMEOUT_SECONDS = 0;
const COMFYUI_VIDEO_FOREGROUND_TIMEOUT_SECONDS = 0;
const COMFYUI_INTERRUPT_TIMEOUT_SECONDS = 15;
const COMFYUI_QUEUE_TIMEOUT_EXTENSION_SECONDS = 1800;
const COMFYUI_QUEUE_MAX_TIMEOUT_SECONDS = 21600;
const COMFYUI_MODELS_CACHE_MS = 15000;
const COMFYUI_DIFFUSERS_INSPECT_CACHE_MS = 60000;
const COMFYUI_MAX_LORAS = 8;
const COMFYUI_LORA_EXTRA_PRICE = 1;
const COMFYUI_VAE_BUILTIN = "__checkpoint_builtin__";
const COMFYUI_VIEW_STORAGE_KEY = "hackme_web.comfyui.active_view";
const COMFYUI_IMAGE_ASSET_KEYS = ["source", "mask", "control"];
const COMFYUI_HF_MODEL_FILE_EXT_RE = /\.(?:safetensors|ckpt|pt|pth|bin|gguf)$/i;
  const COMFYUI_DIFFUSERS_BUILTIN_COMMON_REPOS = [
    { repo: "dhead/wai-nsfw-illustrious-sdxl-v140-sdxl", label: "dhead WAI Illustrious SDXL v140" },
    { repo: "dhead/waiIllustriousSDXL_v150", label: "dhead WAI Illustrious SDXL v150" },
    { repo: "Heartsync/NSFW-Uncensored", label: "Heartsync NSFW Uncensored" },
    { repo: "John6666/janku-v5-nsfw-trained-noobai-rou-wei-illustrious-xl-v50-sdxl", label: "John6666 JanKu V50（SDXL）" },
    { repo: "stabilityai/sd-turbo", label: "SD Turbo（txt2img）" },
  { repo: "stabilityai/sdxl-turbo", label: "SDXL Turbo（txt2img）" },
  { repo: "stable-diffusion-v1-5/stable-diffusion-v1-5", label: "Stable Diffusion 1.5（txt2img）" },
  { repo: "RunDiffusion/Juggernaut-XL-v9", label: "Juggernaut XL v9（SDXL）" },
  { repo: "SG161222/RealVisXL_V4.0", label: "RealVisXL V4.0（SDXL）" },
  { repo: "cagliostrolab/animagine-xl-4.0", label: "Animagine XL 4.0（Anime SDXL）" },
  { repo: "cagliostrolab/animagine-xl-3.1", label: "Animagine XL 3.1（Anime SDXL）" },
  { repo: "stablediffusionapi/animapencil-xl-v3", label: "AnimaPencil XL v3（Anime SDXL）" },
  { repo: "Lykon/AAM_XL_AnimeMix", label: "AAM XL AnimeMix（Anime SDXL）" },
  { repo: "black-forest-labs/FLUX.1-schnell", label: "FLUX.1 schnell（txt2img，需 HF 授權）" },
    { repo: "Qwen/Qwen-Image", label: "Qwen Image（txt2img）" },
    { repo: "Qwen/Qwen-Image-Edit", label: "Qwen Image Edit（I2I）" },
    { repo: "Tongyi-MAI/Z-Image-Turbo", label: "Z-Image Turbo（txt2img）" },
    { repo: "stabilityai/stable-diffusion-xl-refiner-1.0", label: "SDXL Refiner（I2I）" },
  ];
const COMFYUI_DIFFUSERS_MAX_CUSTOM_COMMON_REPOS = 20;
const COMFYUI_CONTROLNET_TIPS = {
  canny: "適合保留邊緣與輪廓，常用於重畫原圖構圖。",
  depth: "適合保留場景深度與立體關係。",
  openpose: "適合固定人物姿勢與骨架。",
  lineart: "適合線稿上色或乾淨描線重建。",
  scribble: "適合塗鴉快速構圖。",
  softedge: "適合柔和邊緣與輪廓引導。",
  tile: "適合局部細節補強與放大。",
};

function comfyuiJobPollMs() {
  const seconds = Number(siteConfig?.comfyui_job_poll_seconds || 1);
  return Math.max(1, Math.min(60, Number.isFinite(seconds) ? seconds : 1)) * 1000;
}

function comfyuiUserStorageKey(key) {
  if (typeof accountScopedStorageKey === "function") return accountScopedStorageKey(key);
  const id = Number(currentUserId || 0);
  const scope = Number.isFinite(id) && id > 0
    ? `user:${id}`
    : (currentUser ? `name:${String(currentUser).trim().toLowerCase()}` : "anonymous");
  return `hackme_web:${scope}:${String(key || "state")}`;
}
const COMFYUI_DRAFT_FIELD_IDS = [
  "comfyui-model-download-type",
  "comfyui-model-relative-path",
  "comfyui-diffusers-model-repo",
  "comfyui-diffusers-model-variant",
  "comfyui-model-select",
  "comfyui-vae-select",
  "comfyui-prompt",
  "comfyui-negative-prompt",
  "comfyui-width",
  "comfyui-height",
  "comfyui-steps",
  "comfyui-cfg",
  "comfyui-batch-size",
  "comfyui-run-count",
  "comfyui-seed",
  "comfyui-seed-after-generate",
  "comfyui-sampler",
  "comfyui-scheduler",
  "comfyui-denoise-strength",
  "comfyui-upscale-model",
  "comfyui-controlnet-enabled",
  "comfyui-controlnet-type",
  "comfyui-controlnet-model",
  "comfyui-controlnet-preprocessor",
  "comfyui-control-strength",
  "comfyui-control-start",
  "comfyui-control-end",
  "comfyui-outpaint-left",
  "comfyui-outpaint-top",
  "comfyui-outpaint-right",
  "comfyui-outpaint-bottom",
  "comfyui-outpaint-feathering",
  "comfyui-save-path",
  "comfyui-album-select",
];
const COMFYUI_DYNAMIC_SELECT_IDS = [
  "comfyui-model-select",
  "comfyui-vae-select",
  "comfyui-sampler",
  "comfyui-scheduler",
  "comfyui-album-select",
  "comfyui-controlnet-type",
  "comfyui-controlnet-model",
  "comfyui-controlnet-preprocessor",
  "comfyui-upscale-model",
];
const COMFYUI_RANDOM_SEED_MAX = 0xFFFFFFFF;
const COMFYUI_UI_SEED_MAX = Number.MAX_SAFE_INTEGER;
const COMFYUI_INPUT_ASSET_META = {
  source: {
    fileInputId: "comfyui-source-image-file",
    previewId: "comfyui-source-image-preview",
    metaId: "comfyui-source-image-meta",
    clearBtnId: "comfyui-source-image-clear-btn",
    pickerBtnId: "comfyui-source-image-picker-btn",
    cardId: "comfyui-source-image-card",
    emptyText: "尚未選擇來源圖片",
    title: "來源圖片",
    mediaType: "image",
  },
  mask: {
    fileInputId: "comfyui-mask-image-file",
    previewId: "comfyui-mask-image-preview",
    metaId: "comfyui-mask-image-meta",
    clearBtnId: "comfyui-mask-image-clear-btn",
    pickerBtnId: "comfyui-mask-image-picker-btn",
    editBtnId: "comfyui-mask-editor-open-btn",
    cardId: "comfyui-mask-image-card",
    emptyText: "尚未選擇遮罩圖片",
    title: "遮罩圖片",
    mediaType: "image",
  },
  control: {
    fileInputId: "comfyui-control-image-file",
    previewId: "comfyui-control-image-preview",
    metaId: "comfyui-control-image-meta",
    clearBtnId: "comfyui-control-image-clear-btn",
    pickerBtnId: "comfyui-control-image-picker-btn",
    cardId: "comfyui-control-image-card",
    emptyText: "尚未選擇控制圖",
    title: "控制圖",
    mediaType: "image",
  },
  video: {
    fileInputId: "comfyui-template-video-file",
    previewId: "comfyui-template-video-preview",
    metaId: "comfyui-template-video-meta",
    clearBtnId: "comfyui-template-video-clear-btn",
    cardId: "comfyui-template-video-card",
    emptyText: "尚未選擇來源影片",
    title: "來源影片",
    mediaType: "video",
  },
};

function setComfyuiTabAvailability(available, detail = "") {
  comfyuiServerAvailable = available === null ? null : !!available;
  const tab = $("tab-module-comfyui");
  if (tab) {
    tab.disabled = false;
    tab.classList.remove("disabled");
    tab.setAttribute("aria-disabled", "false");
    tab.title = detail || "";
  }
  const status = $("comfyui-status");
  if (status && comfyuiServerAvailable === false) {
    status.textContent = detail || "ComfyUI 伺服器未連線";
  }
  updateComfyuiRootPanelVisibility();
  updateComfyuiStartButton();
}

function isComfyuiAvailableForNavigation() {
  return true;
}

function comfyuiEffectiveConnectionMode(modeOverride = null) {
  if (modeOverride) {
    const normalized = String(modeOverride || "remote").trim().toLowerCase();
    return normalized === "local" || normalized === "diffusers" ? normalized : "remote";
  }
  if (comfyuiActiveBackendFamily === "hf") return "diffusers";
  const normalized = String(comfyuiConnectionMode || "remote").trim().toLowerCase();
  return normalized === "local" ? "local" : "remote";
}

function comfyuiFrontendBackendUrl() {
  return comfyuiActiveBackendFamily === "hf" ? "diffusers://frontend" : "";
}

function syncComfyuiPrimaryConnectionModeFromResponse(json = {}) {
  if (json && json.backend_scope === "custom") return;
  const mode = String(json?.connection_mode || "").trim().toLowerCase();
  if (mode === "local" || mode === "remote") comfyuiConnectionMode = mode;
}

function comfyuiRequestQuery() {
  const backendUrl = comfyuiFrontendBackendUrl();
  if (!backendUrl) return "";
  const query = new URLSearchParams();
  query.set("backend_url", backendUrl);
  return `?${query.toString()}`;
}

function comfyuiRequestPayloadExtras() {
  const backendUrl = comfyuiFrontendBackendUrl();
  return backendUrl ? { backend_url: backendUrl } : {};
}

function comfyuiBackendLabel(payload = {}) {
  return "";
}

function comfyuiPaidApiStatusText(payload = {}) {
  const paid = payload?.paid_api_nodes;
  if (!paid || typeof paid !== "object") return "";
  if (!paid.enabled) return "；付費/API nodes 未啟用";
  if (!paid.key_configured) return "；付費/API nodes 已啟用但尚未設定 Account API Key";
  const creditText = paid.credit_balance_available && paid.credit_balance !== null && paid.credit_balance !== undefined
    ? `，credits ${paid.credit_balance}`
    : "，官方 credits 請至 ComfyUI UI 的 Settings / Credits 查看";
  return `；付費/API nodes 可用${creditText}；ComfyUI credits 不是本站積分`;
}

function comfyuiStorageWarningText(payload = {}) {
  const warnings = Array.isArray(payload.storage_warnings) ? payload.storage_warnings : [];
  const warning = warnings.find((item) => item && item.code === "windows_mount_model_storage") || warnings[0];
  if (!warning) return "";
  return `；儲存路徑警告：${warning.message || "模型位於較慢掛載路徑，建議移到 Linux native storage"}`;
}

function comfyuiDiffusersStatusText() {
  return "HF / Diffusers 模式：使用 Hugging Face repo 生圖，不使用 ComfyUI server 模型清單；請確認 repo / variant 後送出。";
}

function comfyuiModelsStatusText(payload = {}) {
  if (isComfyuiDiffusersMode()) return comfyuiDiffusersStatusText();
  return `已連線 ${payload.comfyui_url || "ComfyUI"}${comfyuiBackendLabel(payload)}，模型 ${Number((payload.models || []).length)} 個，LoRA ${Number((payload.loras || []).length)} 個，Embedding ${Number((payload.embeddings || []).length)} 個，VAE ${Number((payload.vaes || []).length)} 個${comfyuiPaidApiStatusText(payload)}${comfyuiStorageWarningText(payload)}`;
}

function updateComfyuiStatusForActiveBackend() {
  const status = $("comfyui-status");
  if (!status || !isComfyuiDiffusersMode()) return;
  status.textContent = comfyuiDiffusersStatusText();
}

function comfyuiConnectionModeLabel(mode = comfyuiConnectionMode) {
  const normalized = String(mode || "remote").trim().toLowerCase();
  if (normalized === "local") return "本地 ComfyUI";
  if (normalized === "diffusers") return "Hugging Face Diffusers";
  return "雲端 / 遠端 API";
}

function comfyuiConnectionModeDetail(mode = comfyuiConnectionMode) {
  const normalized = String(mode || "remote").trim().toLowerCase();
  if (normalized === "local") {
    return "目前是本地模式：可由 root 啟動 / 停止本地 ComfyUI，且 root 可在下方折疊區管理本地模型下載。";
  }
  if (normalized === "diffusers") {
    return "目前是 Diffusers 模式：後端會直接載入 Hugging Face repo 生圖，不經過 ComfyUI server；root 仍可使用 Civitai 模型匯入區。";
  }
  return "目前是雲端 / 遠端模式：此頁會直接呼叫遠端 ComfyUI API 生圖；root 仍可使用 Civitai 模型匯入區，檔案會寫入本站設定的 ComfyUI models 目錄。";
}

function updateComfyuiModeNote(modeOverride = null) {
  const normalizedMode = comfyuiEffectiveConnectionMode(modeOverride);
  const note = $("comfyui-mode-note");
  const badge = $("comfyui-mode-badge");
  const detail = $("comfyui-mode-detail");
  const title = $("comfyui-backend-title");
  if (title) title.textContent = normalizedMode === "diffusers" ? "HF / Diffusers" : "ComfyUI / GGUF";
  if (note) note.textContent = `目前模式：${comfyuiConnectionModeLabel(normalizedMode)}`;
  if (badge) {
    badge.textContent = normalizedMode === "local" ? "本地模式" : (normalizedMode === "diffusers" ? "Diffusers 模式" : "雲端 / 遠端模式");
    badge.dataset.mode = normalizedMode;
  }
  if (detail) detail.textContent = comfyuiConnectionModeDetail(normalizedMode);
}

function setComfyuiMessage(text, ok = true) {
  const msg = $("comfyui-msg");
  if (!msg) return;
  if (!text) {
    msg.className = "msg";
    msg.textContent = "";
    return;
  }
  flash(msg, text, ok);
}

function setComfyuiIdleSuspend(reason, active, label) {
  if (typeof setInactivitySuspendState === "function") {
    const backendLabel = isComfyuiDiffusersMode() ? "Diffusers" : "ComfyUI";
    setInactivitySuspendState(reason, !!active, label || `${backendLabel} 工作中`);
  }
}

function setComfyuiBusy(busy) {
  const generate = $("comfyui-generate-btn");
  const interrupt = $("comfyui-interrupt-btn");
  const refresh = $("comfyui-refresh-btn");
  const start = $("comfyui-start-btn");
  const stop = $("comfyui-stop-btn");
  const unavailable = comfyuiServerAvailable === false;
  if (generate) {
    generate.disabled = !!busy;
    generate.textContent = busy ? "產生中..." : "產生圖片";
    generate.title = unavailable
      ? "ComfyUI 目前標記為未連線；按下會重新檢查連線並嘗試載入模型。"
      : "";
  }
  if (interrupt) interrupt.disabled = !busy;
  if (refresh) refresh.disabled = !!busy;
  if (start) start.disabled = !!busy;
  if (stop) stop.disabled = !!busy;
  setComfyuiIdleSuspend("comfyui_generate", !!busy, `${isComfyuiDiffusersMode() ? "Diffusers" : "ComfyUI"} 產圖中`);
}

function updateComfyuiStartButton() {
  const start = $("comfyui-start-btn");
  const stop = $("comfyui-stop-btn");
  const isRoot = currentUser === "root";
  const localMode = comfyuiEffectiveConnectionMode() === "local";
  const showLocalRuntimeStop = isRoot && localMode && (comfyuiServerAvailable === true || comfyuiLocalRuntimeActive);
  updateComfyuiModeNote();
  if (start) {
    start.style.display = localMode && comfyuiServerAvailable !== true && !comfyuiLocalRuntimeActive ? "" : "none";
    start.disabled = !!comfyuiGenerateAbortController;
  }
  if (stop) {
    stop.style.display = showLocalRuntimeStop ? "" : "none";
    stop.disabled = !!comfyuiGenerateAbortController;
  }
}

function stopComfyuiLocalStartPolling() {
  if (comfyuiLocalStartPollTimer) {
    clearTimeout(comfyuiLocalStartPollTimer);
    comfyuiLocalStartPollTimer = null;
  }
  setComfyuiIdleSuspend("comfyui_start_local", false, "ComfyUI 啟動中");
}

function scheduleComfyuiLocalStartPolling({ attemptsLeft = 120, delayMs = 5000 } = {}) {
  stopComfyuiLocalStartPolling();
  if (comfyuiEffectiveConnectionMode() !== "local" || attemptsLeft <= 0) return;
  setComfyuiIdleSuspend("comfyui_start_local", true, "ComfyUI 啟動中");
  comfyuiLocalStartPollTimer = setTimeout(async () => {
    comfyuiLocalStartPollTimer = null;
    const ok = await refreshComfyuiStatus({ switchAway: false });
    if (ok) {
      setComfyuiMessage("本地 ComfyUI 已啟動完成。", true);
      await loadComfyuiModels({ forceRefresh: true });
      return;
    }
    scheduleComfyuiLocalStartPolling({ attemptsLeft: attemptsLeft - 1, delayMs });
  }, delayMs);
}

function applyComfyuiRuntimeLimits(payload = {}) {
  const parsed = Number(payload.max_batch_size || 1);
  comfyuiMaxBatchSize = Math.max(1, Math.min(8, Number.isFinite(parsed) ? parsed : 1));
  const defaultWidth = Number(payload.default_width || payload.comfyui_default_width || 1024);
  const defaultHeight = Number(payload.default_height || payload.comfyui_default_height || 1024);
  comfyuiDefaultWidth = Math.max(64, Math.min(2048, Number.isFinite(defaultWidth) ? defaultWidth : 1024));
  comfyuiDefaultHeight = Math.max(64, Math.min(2048, Number.isFinite(defaultHeight) ? defaultHeight : 1024));
  const draft = readComfyuiDraft();
  const widthInput = $("comfyui-width");
  const heightInput = $("comfyui-height");
  if (widthInput && !draft["comfyui-width"]) widthInput.value = String(comfyuiDefaultWidth);
  if (heightInput && !draft["comfyui-height"]) heightInput.value = String(comfyuiDefaultHeight);
  const input = $("comfyui-batch-size");
  if (!input) return;
  input.min = "1";
  input.max = String(comfyuiMaxBatchSize);
  if (!input.value || Number(input.value) > comfyuiMaxBatchSize) input.value = String(comfyuiMaxBatchSize);
  if (comfyuiMaxBatchSize === 1) input.value = "1";
  input.disabled = comfyuiMaxBatchSize <= 1;
  input.title = comfyuiMaxBatchSize <= 1
    ? "目前系統限制單次只能產生 1 張，root 可在安全中心調整"
    : `目前單次最多 ${comfyuiMaxBatchSize} 張`;
  comfyuiBillingQuote = payload.billing || null;
  if (payload.lora_extra_unit_price !== undefined) {
    comfyuiBillingQuote = { ...(comfyuiBillingQuote || {}), lora_extra_unit_price: Number(payload.lora_extra_unit_price || COMFYUI_LORA_EXTRA_PRICE) };
  }
  const generate = $("comfyui-generate-btn");
  if (generate && comfyuiBillingQuote?.unit_price) {
    generate.title = `非 root 帳號成功產圖後每張扣 ${comfyuiBillingQuote.unit_price} 點；每個 LoRA 每張額外 +${comfyuiBillingQuote.lora_extra_unit_price || COMFYUI_LORA_EXTRA_PRICE} 點；產圖失敗不扣點，丟棄預覽不退款`;
  }
  updateComfyuiRootPanelVisibility();
}

function comfyuiHasInputAsset(key) {
  const asset = comfyuiAssetState(key);
  return !!asset?.file || !!asset?.imageRef;
}

function comfyuiNormalizeOutputKind(kind) {
  const normalized = String(kind || "").trim().toLowerCase();
  if (["image", "images", "preview", "mask"].includes(normalized)) return "image";
  if (["video", "videos", "gif", "gifs", "movie", "movies"].includes(normalized)) return "video";
  if (["audio", "audios", "music", "song", "songs", "sound", "sounds", "voice", "voices"].includes(normalized)) return "audio";
  if (["file", "files", "binary", "download"].includes(normalized)) return "file";
  return "";
}

function comfyuiUniqueOutputKinds(kinds = []) {
  const seen = new Set();
  (Array.isArray(kinds) ? kinds : []).forEach((kind) => {
    const normalized = comfyuiNormalizeOutputKind(kind);
    if (normalized) seen.add(normalized);
  });
  const order = ["image", "video", "audio", "file"];
  return order.filter((kind) => seen.has(kind));
}

function comfyuiSelectedTemplateOutputKinds() {
  const detail = comfyuiSelectedTemplateDetail;
  const active = detail && Number(detail?.id || 0) === Number(comfyuiSelectedTemplatePresetId || 0);
  const declared = active && Array.isArray(detail?.output_kinds) ? comfyuiUniqueOutputKinds(detail.output_kinds) : [];
  return declared.length ? declared : ["image"];
}

function comfyuiMediaItemKind(item = {}) {
  const mimeType = String(item?.mime_type || "").toLowerCase();
  if (mimeType.startsWith("image/")) return "image";
  if (mimeType.startsWith("video/")) return "video";
  if (mimeType.startsWith("audio/")) return "audio";
  const filename = String(item?.file_ref?.filename || item?.filename || "").toLowerCase();
  if (/\.(png|jpe?g|webp|gif|bmp|tiff?)$/.test(filename)) return "image";
  if (/\.(mp4|mov|webm|mkv|avi|m4v|gif)$/.test(filename)) return "video";
  if (/\.(mp3|wav|flac|ogg|m4a|aac)$/.test(filename)) return "audio";
  return comfyuiNormalizeOutputKind(item?.media_kind || item?.kind || item?.output_kind) || "file";
}

function comfyuiOutputKindsFromItems(images = [], media = []) {
  const kinds = [];
  if (Array.isArray(images) && images.length) kinds.push("image");
  (Array.isArray(media) ? media : []).forEach((item) => kinds.push(comfyuiMediaItemKind(item)));
  return comfyuiUniqueOutputKinds(kinds);
}

function comfyuiCurrentPreviewOutputKinds() {
  const actual = comfyuiOutputKindsFromItems(comfyuiGeneratedImages, comfyuiGeneratedMedia);
  return actual.length ? actual : comfyuiSelectedTemplateOutputKinds();
}

function comfyuiOutputKindLabel(kind) {
  const normalized = comfyuiNormalizeOutputKind(kind);
  if (normalized === "video") return "影片";
  if (normalized === "audio") return "音訊";
  if (normalized === "file") return "檔案";
  return "圖片";
}

function comfyuiOutputKindsLabel(kinds = null) {
  const normalized = comfyuiUniqueOutputKinds(Array.isArray(kinds) ? kinds : comfyuiCurrentPreviewOutputKinds());
  const labels = (normalized.length ? normalized : ["image"]).map((kind) => comfyuiOutputKindLabel(kind));
  if (labels.length <= 1) return labels[0] || "圖片";
  if (labels.length === 2) return `${labels[0]}與${labels[1]}`;
  return `${labels.slice(0, -1).join("、")}與${labels[labels.length - 1]}`;
}

function comfyuiPreviewEmptyText(kinds = null) {
  return `尚未產生${comfyuiOutputKindsLabel(kinds)}`;
}

function comfyuiPreviewPendingText(kinds = null) {
  return `產生${comfyuiOutputKindsLabel(kinds)}中...`;
}

function updateComfyuiPreviewCardForOutputKinds(kinds = null) {
  const normalized = comfyuiUniqueOutputKinds(Array.isArray(kinds) ? kinds : comfyuiCurrentPreviewOutputKinds());
  const effective = normalized.length ? normalized : ["image"];
  const label = comfyuiOutputKindsLabel(effective);
  const backendLabel = isComfyuiDiffusersMode() ? "Diffusers" : "ComfyUI";
  const title = $("comfyui-preview-card-title");
  const sub = $("comfyui-preview-card-sub");
  if (title) title.textContent = `${label}結果`;
  if (sub) sub.textContent = `等待 ${backendLabel} 產生${label}。`;
  const preview = $("comfyui-preview");
  const empty = preview?.querySelector(".drive-empty");
  if (empty && /^尚未產生/.test(String(empty.textContent || "").trim())) {
    empty.textContent = comfyuiPreviewEmptyText(effective);
  }
}

function comfyuiSelectedTemplateMode() {
  const detail = comfyuiSelectedTemplateDetail;
  if (detail && Number(detail?.id || 0) === Number(comfyuiSelectedTemplatePresetId || 0)) {
    const mode = String(detail?.default_params?.generation_mode || detail?.purpose || "").trim().toLowerCase();
    if (mode && mode !== "custom") return normalizeComfyuiGenerationModeAlias(mode);
    const outputs = comfyuiSelectedTemplateOutputKinds();
    if (outputs.includes("audio")) return outputs.includes("video") ? "t2sv" : "t2s";
    if (outputs.includes("video")) return comfyuiHasInputAsset("source") ? "i2v" : "t2v";
  }
  return "";
}

function comfyuiExplicitSpecialMode() {
  const mode = normalizeComfyuiGenerationModeAlias($("comfyui-generation-mode")?.value || "");
  return mode === "outpaint" ? mode : "";
}

function inferComfyuiGenerationMode() {
  const templateMode = comfyuiSelectedTemplateMode();
  if (templateMode) return templateMode;
  const explicit = comfyuiExplicitSpecialMode();
  if (explicit) return explicit;
  if (comfyuiDiffusersTextOnlyMode()) return "txt2img";
  if (comfyuiHasInputAsset("source")) {
    if (comfyuiHasInputAsset("mask")) return "inpaint";
    if (String($("comfyui-upscale-model")?.value || "").trim()) return "upscale";
    return "img2img";
  }
  return "txt2img";
}

function syncComfyuiGenerationMode() {
  const mode = inferComfyuiGenerationMode();
  const field = $("comfyui-generation-mode");
  if (field && field.value !== mode) field.value = mode;
  const auto = $("comfyui-generation-mode-auto");
  if (auto) {
    const imageInputHint = isComfyuiDiffusersMode()
      && mode === "txt2img"
      && comfyuiDiffusersSupportsImageInputMode()
      && !comfyuiHasInputAsset("source")
      ? "（repo 支援圖生圖，上傳來源圖後自動切換）"
      : "";
    auto.textContent = `系統判斷：${comfyuiReadableModeLabel(mode)}${imageInputHint}`;
  }
  return mode;
}

function comfyuiGenerationMode() {
  return syncComfyuiGenerationMode();
}

function isComfyuiDiffusersMode(mode = null) {
  return comfyuiEffectiveConnectionMode(mode) === "diffusers";
}

function comfyuiCurrentDiffusersRepoInput() {
  return normalizeComfyuiHuggingFaceRepoInput($("comfyui-diffusers-model-repo")?.value || "");
}

function normalizeComfyuiHuggingFaceRepoInput(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const withScheme = raw.startsWith("http://") || raw.startsWith("https://") ? raw : (/^huggingface\.co\//i.test(raw) ? `https://${raw}` : "");
  if (withScheme) {
    try {
      const parsed = new URL(withScheme);
      const host = String(parsed.hostname || "").toLowerCase();
      if (host === "huggingface.co" || host === "www.huggingface.co") {
        const parts = parsed.pathname.split("/").filter(Boolean);
        const tail = parts[parts.length - 1] || "";
        if (COMFYUI_HF_MODEL_FILE_EXT_RE.test(tail)) return "";
        if (parts.length >= 2) return `${parts[0]}/${parts[1]}`;
      }
    } catch (_err) {}
  }
  const normalized = raw.replace(/^\/+|\/+$/g, "");
  const tail = normalized.replace(/\\/g, "/").split("/").pop() || "";
  if (COMFYUI_HF_MODEL_FILE_EXT_RE.test(tail)) return "";
  return normalized;
}

function isComfyuiHuggingFaceRepoLike(value) {
  const repo = normalizeComfyuiHuggingFaceRepoInput(value);
  if (!repo) return false;
  const parts = repo.split("/");
  if (parts.length !== 2 || !parts[0] || !parts[1]) return false;
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/.test(parts[0])
    && /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/.test(parts[1]);
}

function comfyuiHuggingFaceRepoInputLooksLikeModelFile(value) {
  const raw = String(value || "").trim();
  if (!raw) return false;
  const tail = raw.replace(/[?#].*$/, "").replace(/\\/g, "/").split("/").pop() || "";
  return COMFYUI_HF_MODEL_FILE_EXT_RE.test(tail);
}

function sanitizeComfyuiDiffusersRepoField({ strict = false, quiet = true } = {}) {
  const input = $("comfyui-diffusers-model-repo");
  if (!input) return "";
  const raw = String(input.value || "").trim();
  if (!raw) return "";
  const repo = normalizeComfyuiHuggingFaceRepoInput(raw);
  if (repo && isComfyuiHuggingFaceRepoLike(repo)) {
    if (input.value !== repo) input.value = repo;
    return repo;
  }
  if (strict || comfyuiHuggingFaceRepoInputLooksLikeModelFile(raw)) {
    input.value = "";
    clearComfyuiDiffusersInspection();
    if (!quiet) {
      setComfyuiMessage("Hugging Face Repo 請填 namespace/model 或模型頁網址，不要填 .safetensors / .gguf 權重檔名。", false);
    }
  }
  return "";
}

function comfyuiDiffusersVariantLabel(option) {
  const label = option?.label || option?.precision || "精度版本";
  const size = Number(option?.size_bytes || 0);
  const sizeText = size > 0 && typeof formatDriveBytes === "function" ? ` · ${formatDriveBytes(size)}` : "";
  const fileText = Number(option?.file_count || 0) > 0 ? ` · ${Number(option.file_count)} files` : "";
  return `${label}${sizeText}${fileText}`;
}

function comfyuiSelectedDiffusersVariantOption() {
  const selected = $("comfyui-diffusers-model-variant")?.value || "";
  const options = Array.isArray(comfyuiDiffusersInspection?.data?.variant_options)
    ? comfyuiDiffusersInspection.data.variant_options
    : [];
  return options.find((option) => String(option?.value || option?.variant || "") === selected) || null;
}

function comfyuiDiffusersInspectionMatchesCurrentRepo() {
  const inspection = comfyuiDiffusersInspection;
  if (!inspection || inspection.loading || inspection.error || !inspection.data) return false;
  const currentRepo = comfyuiCurrentDiffusersRepoInput();
  if (!currentRepo) return false;
  return String(inspection.repo || inspection.data?.repo_id || "").trim() === currentRepo
    || String(inspection.data?.repo_id || "").trim() === currentRepo;
}

function comfyuiDiffusersInspectionSupportedModes() {
  if (!comfyuiDiffusersInspectionMatchesCurrentRepo()) return [];
  return Array.isArray(comfyuiDiffusersInspection?.data?.supported_modes)
    ? comfyuiDiffusersInspection.data.supported_modes.map((mode) => normalizeComfyuiGenerationModeAlias(mode)).filter(Boolean)
    : [];
}

function comfyuiDiffusersInspectionRunnableModes() {
  if (!comfyuiDiffusersInspectionMatchesCurrentRepo()) return [];
  const rawModes = Array.isArray(comfyuiDiffusersInspection?.data?.runnable_modes)
    ? comfyuiDiffusersInspection.data.runnable_modes
    : comfyuiDiffusersInspectionSupportedModes().filter((mode) => ["txt2img", "img2img", "inpaint"].includes(mode));
  return rawModes.map((mode) => normalizeComfyuiGenerationModeAlias(mode)).filter(Boolean);
}

function comfyuiDiffusersSupportsImageInputMode() {
  if (!isComfyuiDiffusersMode()) return false;
  return comfyuiDiffusersInspectionRunnableModes().some((mode) => ["img2img", "inpaint"].includes(mode));
}

function comfyuiDiffusersTextOnlyMode() {
  if (!isComfyuiDiffusersMode()) return false;
  const modes = comfyuiDiffusersInspectionRunnableModes();
  return modes.length > 0 && modes.every((mode) => mode === "txt2img");
}

function resetComfyuiInputAssetState(key) {
  const asset = comfyuiAssetState(key);
  revokeComfyuiAssetPreview(asset);
  comfyuiInputAssets[key] = { file: null, imageRef: null, previewUrl: "", filename: "", cloudFileId: "" };
  const meta = COMFYUI_INPUT_ASSET_META[key];
  const fileInput = meta?.fileInputId ? $(meta.fileInputId) : null;
  if (fileInput) fileInput.value = "";
  renderComfyuiInputAsset(key);
}

function clearComfyuiDiffusersImageAssetsForTextOnly() {
  if (!comfyuiDiffusersTextOnlyMode()) return false;
  let changed = false;
  ["source", "mask", "control"].forEach((key) => {
    const asset = comfyuiAssetState(key);
    if (asset.file || asset.imageRef || asset.previewUrl || asset.filename || asset.cloudFileId) {
      resetComfyuiInputAssetState(key);
      changed = true;
    }
  });
  const controlCheckbox = $("comfyui-controlnet-enabled");
  if (controlCheckbox?.checked) {
    controlCheckbox.checked = false;
    changed = true;
  }
  const field = $("comfyui-generation-mode");
  if (field && field.value !== "txt2img") {
    field.value = "txt2img";
    changed = true;
  }
  return changed;
}

function comfyuiCanUseInputAssetForCurrentMode(key) {
  if (!["source", "mask", "control"].includes(String(key || ""))) return true;
  return !comfyuiDiffusersTextOnlyMode();
}

function comfyuiSelectedGgufProfile() {
  const selected = $("comfyui-diffusers-gguf-profile")?.value || "";
  return comfyuiGgufProfiles.find((profile) => String(profile?.id || "") === selected) || null;
}

function comfyuiSelectedGgufVariant() {
  const selected = $("comfyui-diffusers-gguf-variant")?.value || "";
  const profile = comfyuiSelectedGgufProfile();
  const variants = Array.isArray(profile?.variants) ? profile.variants : [];
  return variants.find((variant) => String(variant?.id || "") === selected) || null;
}

function comfyuiGgufVariantLabel(variant) {
  const label = variant?.label || variant?.id || "精度版本";
  const size = Number(variant?.size_bytes || 0);
  const sizeText = size > 0 && typeof formatDriveBytes === "function" ? ` · ${formatDriveBytes(size)}` : "";
  const status = variant?.enabled ? "" : ` · ${variant?.status || "未驗證"}`;
  return `${label}${sizeText}${status}`;
}

function renderComfyuiGgufProfileHint() {
  const hint = $("comfyui-diffusers-gguf-profile-hint");
  if (!hint) return;
  const profile = comfyuiSelectedGgufProfile();
  const variant = comfyuiSelectedGgufVariant();
  if (!profile) {
    hint.textContent = "官方 profile 會帶入對應 workflow、文字編碼器、VAE 與已驗證精度。";
    return;
  }
  const parts = [];
  if (profile.prompt_style_hint) parts.push(String(profile.prompt_style_hint));
  const status = [profile.status, variant?.status].filter(Boolean).join(" / ");
  if (status) parts.push(`狀態：${status}`);
  if (profile.family) parts.push(`family：${profile.family}`);
  hint.textContent = parts.join(" ");
}

function comfyuiSelectedTemplateBundleIdForGgufInventory() {
  const detail = comfyuiSelectedTemplateDetail;
  if (!detail || Number(detail?.id || 0) !== Number(comfyuiSelectedTemplatePresetId || 0)) return "";
  return String(detail?.system_bundle_id || detail?.manifest_json?.id || "").trim();
}

function shouldShowComfyuiInstalledGgufModels() {
  const detail = comfyuiSelectedTemplateDetail;
  if (!detail || Number(detail?.id || 0) !== Number(comfyuiSelectedTemplatePresetId || 0)) return false;
  return comfyuiSelectedTemplateBundleIdForGgufInventory() === "origin_sdxl_gguf_txt2img"
    || String(detail?.title || detail?.name || "").trim() === "SDXL GGUF Text-to-Image (ComfyUI-GGUF)";
}

function updateComfyuiInstalledGgufVisibility() {
  const box = $("comfyui-installed-gguf-list");
  if (!box) return;
  box.hidden = !shouldShowComfyuiInstalledGgufModels();
}

function renderComfyuiInstalledGgufModels(items = []) {
  const box = $("comfyui-installed-gguf-list");
  if (!box) return;
  updateComfyuiInstalledGgufVisibility();
  const rows = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!rows.length) {
    box.innerHTML = '<div class="drive-empty">尚未偵測到已安裝 GGUF UNet。</div>';
    return;
  }
  box.innerHTML = rows.map((item) => {
    const name = item.basename || item.name || item.option || "GGUF";
    const mapped = item.official_profile ? "官方 profile" : "未建檔";
    const profile = item.profile_label || item.profile_id || "";
    const variant = item.variant_label || item.variant_id || "";
    const status = item.enabled ? "可用" : (item.status || "未開放");
    const size = Number(item.size_bytes || 0);
    const sizeText = size > 0 && typeof formatDriveBytes === "function" ? ` · ${formatDriveBytes(size)}` : "";
    const meta = [mapped, profile, variant, `${status}${sizeText}`].filter(Boolean).join(" · ");
    return `<div class="drive-file-row">
      <div>
        <strong>${sanitize(name)}</strong>
        <div class="drive-card-sub">${sanitize(meta)}</div>
      </div>
    </div>`;
  }).join("");
}

function fillComfyuiGgufVariants(profileId = "") {
  const select = $("comfyui-diffusers-gguf-variant");
  if (!select) return;
  const profile = comfyuiGgufProfiles.find((item) => String(item?.id || "") === String(profileId || "")) || null;
  const variants = Array.isArray(profile?.variants) ? profile.variants : [];
  if (!profile) {
    select.innerHTML = '<option value="">先選擇官方 GGUF profile</option>';
    select.disabled = true;
    renderComfyuiGgufProfileHint();
    return;
  }
  select.disabled = false;
  select.innerHTML = variants.length
    ? variants.map((variant) => {
        const disabled = profile.enabled && variant?.enabled ? "" : ' disabled="disabled"';
        return `<option value="${sanitize(variant.id || "")}"${disabled}>${sanitize(comfyuiGgufVariantLabel(variant))}</option>`;
      }).join("")
    : '<option value="">這個 profile 尚無精度版本</option>';
  const enabledDefault = variants.find((variant) => profile.enabled && variant?.enabled);
  if (enabledDefault && !variants.some((variant) => String(variant?.id || "") === select.value && variant?.enabled)) {
    select.value = enabledDefault.id || "";
  }
  renderComfyuiGgufProfileHint();
}

async function ensureComfyuiGgufProfilesLoaded({ force = false } = {}) {
  if (!force && Array.isArray(comfyuiGgufProfiles) && comfyuiGgufProfiles.length) return true;
  if (!currentUser || !canAccessModule("comfyui")) return false;
  if (!force && comfyuiGgufProfilesLoadPromise) return comfyuiGgufProfilesLoadPromise;
  comfyuiGgufProfilesLoadPromise = (async () => {
    await fetchCsrfToken();
    const res = await apiFetch(API + "/comfyui/installed-gguf" + comfyuiRequestQuery(), {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" }
    });
    const json = await res.json().catch(() => ({}));
    syncComfyuiPrimaryConnectionModeFromResponse(json);
    if (!res.ok || !json.ok) throw new Error(json.msg || `GGUF profile 讀取失敗（HTTP ${res.status}）`);
    fillComfyuiGgufProfiles(json.gguf_profiles || []);
    comfyuiInstalledGgufModels = Array.isArray(json.installed_gguf_models) ? json.installed_gguf_models : [];
    renderComfyuiInstalledGgufModels(comfyuiInstalledGgufModels);
    return comfyuiGgufProfiles.length > 0;
  })();
  try {
    return await comfyuiGgufProfilesLoadPromise;
  } finally {
    comfyuiGgufProfilesLoadPromise = null;
  }
}

function fillComfyuiGgufProfiles(profiles = []) {
  comfyuiGgufProfiles = Array.isArray(profiles) ? profiles.filter((item) => item && item.id) : [];
  const select = $("comfyui-diffusers-gguf-profile");
  if (!select) return;
  const previous = select.value || "";
  const options = ['<option value="">不使用官方 GGUF</option>']
    .concat(comfyuiGgufProfiles.map((profile) => {
      const status = profile.enabled ? "" : `（${profile.status || "未開放"}）`;
      return `<option value="${sanitize(profile.id || "")}"${profile.enabled ? "" : ' disabled="disabled"'}>${sanitize((profile.label || profile.id || "GGUF profile") + status)}</option>`;
    }));
  select.innerHTML = options.join("");
  if (previous && comfyuiGgufProfiles.some((profile) => profile.id === previous && profile.enabled)) {
    select.value = previous;
  }
  fillComfyuiGgufVariants(select.value || "");
  renderComfyuiGgufProfileHint();
}

function updateComfyuiGgufProfileSelection({ syncRepo = true } = {}) {
  const profile = comfyuiSelectedGgufProfile();
  const variant = comfyuiSelectedGgufVariant();
  const repoInput = $("comfyui-diffusers-model-repo");
  const baseInput = $("comfyui-diffusers-gguf-base-repo");
  const variantSelect = $("comfyui-diffusers-model-variant");
  if (profile && syncRepo) {
    if (repoInput) repoInput.value = profile.repo_id || "";
    if (baseInput) baseInput.value = profile.base_repo || "";
    if (variantSelect) variantSelect.value = "";
    clearComfyuiDiffusersInspection();
  }
  if (profile && variant && baseInput && !baseInput.value) {
    baseInput.value = profile.base_repo || "";
  }
  renderComfyuiGgufProfileHint();
  updateComfyuiDiffusersGgufOptions();
}

function updateComfyuiDiffusersGgufOptions() {
  const panel = $("comfyui-diffusers-gguf-options");
  const input = $("comfyui-diffusers-gguf-base-repo");
  const profile = comfyuiSelectedGgufProfile();
  if (panel) panel.style.display = "none";
  if (!profile && input) input.value = "";
  if (profile && input && !input.value) {
    input.value = profile.base_repo || "";
  }
}

function comfyuiDiffusersCommonRepoStorageKey() {
  return comfyuiUserStorageKey("comfyui:diffusers-common-repos");
}

function normalizeComfyuiDiffusersCommonRepo(value) {
  const repo = normalizeComfyuiHuggingFaceRepoInput(value || "");
  return repo && isComfyuiHuggingFaceRepoLike(repo) ? repo : "";
}

function comfyuiDiffusersBuiltinRepoSet() {
  return new Set(COMFYUI_DIFFUSERS_BUILTIN_COMMON_REPOS.map((item) => item.repo));
}

function readComfyuiDiffusersCustomRepos() {
  let raw = [];
  try {
    raw = JSON.parse(localStorage.getItem(comfyuiDiffusersCommonRepoStorageKey()) || "[]") || [];
  } catch (err) {
    raw = [];
  }
  const seen = comfyuiDiffusersBuiltinRepoSet();
  const repos = [];
  (Array.isArray(raw) ? raw : []).forEach((item) => {
    const repo = normalizeComfyuiDiffusersCommonRepo(typeof item === "string" ? item : item?.repo);
    if (!repo || seen.has(repo)) return;
    seen.add(repo);
    repos.push({ repo, label: repo, custom: true });
  });
  return repos.slice(0, COMFYUI_DIFFUSERS_MAX_CUSTOM_COMMON_REPOS);
}

function writeComfyuiDiffusersCustomRepos(items) {
  const seen = comfyuiDiffusersBuiltinRepoSet();
  const repos = [];
  (Array.isArray(items) ? items : []).forEach((item) => {
    const repo = normalizeComfyuiDiffusersCommonRepo(typeof item === "string" ? item : item?.repo);
    if (!repo || seen.has(repo)) return;
    seen.add(repo);
    repos.push({ repo });
  });
  try {
    localStorage.setItem(
      comfyuiDiffusersCommonRepoStorageKey(),
      JSON.stringify(repos.slice(0, COMFYUI_DIFFUSERS_MAX_CUSTOM_COMMON_REPOS))
    );
  } catch (err) {
    // Local storage can be unavailable in private mode; the live repo field still works.
  }
  return repos;
}

function allComfyuiDiffusersCommonRepos() {
  return COMFYUI_DIFFUSERS_BUILTIN_COMMON_REPOS
    .map((item) => ({ ...item, custom: false }))
    .concat(readComfyuiDiffusersCustomRepos());
}

function selectedComfyuiDiffusersCommonRepo() {
  return $("comfyui-diffusers-common-repo")?.value || "";
}

function renderComfyuiDiffusersCommonRepos() {
  const select = $("comfyui-diffusers-common-repo");
  const removeBtn = $("comfyui-diffusers-remove-common-repo");
  if (!select) return;
  const currentRepo = normalizeComfyuiDiffusersCommonRepo($("comfyui-diffusers-model-repo")?.value || "");
  select.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "常用 repo";
  select.appendChild(placeholder);
  allComfyuiDiffusersCommonRepos().forEach((item) => {
    const option = document.createElement("option");
    option.value = item.repo;
    option.textContent = item.custom ? `我的：${item.label || item.repo}` : (item.label || item.repo);
    option.dataset.custom = item.custom ? "1" : "0";
    select.appendChild(option);
  });
  select.value = currentRepo && Array.from(select.options).some((option) => option.value === currentRepo)
    ? currentRepo
    : "";
  if (removeBtn) {
    const selectedOption = select.selectedOptions?.[0];
    removeBtn.disabled = !select.value || selectedOption?.dataset.custom !== "1";
  }
}

function clearComfyuiDiffusersGgufProfileIfRepoChanged(repo) {
  const profile = comfyuiSelectedGgufProfile();
  if (profile && repo !== String(profile.repo_id || "")) {
    const profileSelect = $("comfyui-diffusers-gguf-profile");
    if (profileSelect) profileSelect.value = "";
    fillComfyuiGgufVariants("");
    updateComfyuiDiffusersGgufOptions();
  }
}

function setComfyuiDiffusersRepo(repo, { inspect = true } = {}) {
  const input = $("comfyui-diffusers-model-repo");
  const normalized = normalizeComfyuiDiffusersCommonRepo(repo);
  if (!input || !normalized) return false;
  input.value = normalized;
  clearComfyuiDiffusersGgufProfileIfRepoChanged(normalized);
  clearComfyuiDiffusersInspection();
  renderComfyuiDiffusersCommonRepos();
  updateComfyuiModeVisibility();
  writeComfyuiDraft();
  if (inspect) inspectComfyuiDiffusersRepo({ quiet: true });
  return true;
}

function addCurrentComfyuiDiffusersCommonRepo() {
  const repo = sanitizeComfyuiDiffusersRepoField({ strict: true, quiet: true });
  if (!repo) {
    setComfyuiMessage("請先輸入有效的 Hugging Face repo。", false);
    return;
  }
  if (comfyuiDiffusersBuiltinRepoSet().has(repo)) {
    renderComfyuiDiffusersCommonRepos();
    setComfyuiMessage("這個 repo 已在內建常用清單。", true);
    return;
  }
  const custom = readComfyuiDiffusersCustomRepos().filter((item) => item.repo !== repo);
  custom.unshift({ repo, label: repo, custom: true });
  writeComfyuiDiffusersCustomRepos(custom);
  renderComfyuiDiffusersCommonRepos();
  const select = $("comfyui-diffusers-common-repo");
  if (select) select.value = repo;
  setComfyuiMessage("已加入 HF 常用 repo。", true);
}

function removeSelectedComfyuiDiffusersCommonRepo() {
  const repo = selectedComfyuiDiffusersCommonRepo();
  if (!repo || comfyuiDiffusersBuiltinRepoSet().has(repo)) {
    renderComfyuiDiffusersCommonRepos();
    return;
  }
  writeComfyuiDiffusersCustomRepos(readComfyuiDiffusersCustomRepos().filter((item) => item.repo !== repo));
  renderComfyuiDiffusersCommonRepos();
  setComfyuiMessage("已移除 HF 常用 repo。", true);
}

function renderComfyuiDiffusersInspection() {
  const status = $("comfyui-diffusers-repo-status");
  const variantSelect = $("comfyui-diffusers-model-variant");
  const inspection = comfyuiDiffusersInspection;
  if (!variantSelect) return;
  variantSelect.innerHTML = '<option value="">檢查後選擇精度版本</option>';
  variantSelect.disabled = true;
  if (!inspection) {
    updateComfyuiDiffusersGgufOptions();
    if (status) status.textContent = "貼上 Hugging Face repo id 後會先檢查支援模式與可下載精度版本；.safetensors 是權重檔名，不是 repo。";
    return;
  }
  if (inspection.loading) {
    updateComfyuiDiffusersGgufOptions();
    if (status) status.textContent = "正在檢查 Hugging Face repo metadata...";
    return;
  }
  if (inspection.error) {
    updateComfyuiDiffusersGgufOptions();
    if (status) status.textContent = inspection.error;
    return;
  }
  const data = inspection.data || {};
  const textOnly = comfyuiDiffusersTextOnlyMode();
  if (textOnly) clearComfyuiDiffusersImageAssetsForTextOnly();
  const allOptions = Array.isArray(data.variant_options) ? data.variant_options : [];
  const options = allOptions.filter((option) => option?.kind !== "gguf");
  const warnings = Array.isArray(data.warnings) ? data.warnings.filter(Boolean) : [];
  const supportedModes = Array.isArray(data.supported_modes)
    ? data.supported_modes.map((mode) => normalizeComfyuiGenerationModeAlias(mode)).filter(Boolean)
    : [];
  const supportedModeLabels = supportedModes.map((mode) => comfyuiReadableModeLabel(mode)).filter(Boolean);
  const runnableModes = Array.isArray(data.runnable_modes)
    ? data.runnable_modes.map((mode) => normalizeComfyuiGenerationModeAlias(mode)).filter(Boolean)
    : supportedModes.filter((mode) => ["txt2img", "img2img", "inpaint"].includes(mode));
  const runnableModeLabels = runnableModes.map((mode) => comfyuiReadableModeLabel(mode)).filter(Boolean);
  const modeText = data.supported_for_mode ? `支援 ${data.requested_mode || comfyuiGenerationMode()}` : `不支援 ${data.requested_mode || comfyuiGenerationMode()}`;
  if (options.length > 1) {
    variantSelect.disabled = false;
    variantSelect.innerHTML = ['<option value="">請選擇 Diffusers 精度版本（避免重複下載）</option>']
      .concat(options.map((option) => `<option value="${sanitize(option.value || option.variant || "")}">${sanitize(comfyuiDiffusersVariantLabel(option))}</option>`))
      .join("");
  } else if (options.length === 1) {
    variantSelect.disabled = false;
    variantSelect.innerHTML = `<option value="${sanitize(options[0].value || options[0].variant || "__default__")}">${sanitize(comfyuiDiffusersVariantLabel(options[0]))}</option>`;
    variantSelect.selectedIndex = 0;
  }
  updateComfyuiDiffusersGgufOptions();
  if (status) {
    const pipeline = data.pipeline_tag ? ` · pipeline=${data.pipeline_tag}` : "";
    const variantNote = options.length > 1 ? " · 請選擇 Diffusers 精度版本" : "";
    const supportedModeText = supportedModeLabels.length ? ` · 可用模式：${supportedModeLabels.join(" / ")}` : "";
    const runnableModeText = runnableModeLabels.length && runnableModeLabels.join("/") !== supportedModeLabels.join("/")
      ? ` · 本站可執行：${runnableModeLabels.join(" / ")}`
      : "";
    const imageInputNote = runnableModes.some((mode) => ["img2img", "inpaint"].includes(mode)) && !comfyuiHasInputAsset("source")
      ? " · 若要圖生圖，請上傳來源圖片"
      : "";
    const unsupportedPipelineNote = supportedModes.length && !runnableModes.length ? " · 此 HF pipeline 目前只能辨識，不能直接由本站 HF 生圖器執行" : "";
    const textOnlyNote = textOnly ? " · 已判斷為文字生圖，已隱藏來源圖片/遮罩卡片" : "";
    status.textContent = `${data.repo_id || inspection.repo || ""}：${modeText}${pipeline}${variantNote}${supportedModeText}${runnableModeText}${imageInputNote}${unsupportedPipelineNote}${textOnlyNote}${warnings.length ? `。${warnings.join(" ")}` : ""}`;
  }
  updateComfyuiModeVisibility();
}

function clearComfyuiDiffusersInspection() {
  comfyuiDiffusersInspection = null;
  renderComfyuiDiffusersInspection();
}

function comfyuiDiffusersInspectionMatches(repo, mode) {
  const inspection = comfyuiDiffusersInspection;
  if (!inspection || inspection.loading || inspection.error || !inspection.data) return false;
  return inspection.repo === repo && inspection.mode === mode;
}

function comfyuiDiffusersInspectCacheKey(repo, mode) {
  return `${String(repo || "").trim()}|${String(mode || "txt2img").trim()}|${comfyuiRequestQuery()}`;
}

function getCachedComfyuiDiffusersInspection(cacheKey) {
  const cached = comfyuiDiffusersInspectCache.get(cacheKey);
  if (!cached || Date.now() - Number(cached.at || 0) > COMFYUI_DIFFUSERS_INSPECT_CACHE_MS) {
    comfyuiDiffusersInspectCache.delete(cacheKey);
    return null;
  }
  return cached.data || null;
}

function cacheComfyuiDiffusersInspection(cacheKey, data) {
  if (!data) return;
  comfyuiDiffusersInspectCache.set(cacheKey, { at: Date.now(), data });
  if (comfyuiDiffusersInspectCache.size > 40) {
    const oldestKey = comfyuiDiffusersInspectCache.keys().next().value;
    if (oldestKey) comfyuiDiffusersInspectCache.delete(oldestKey);
  }
}

async function inspectComfyuiDiffusersRepo({ quiet = false } = {}) {
  const repo = sanitizeComfyuiDiffusersRepoField({ strict: true, quiet });
  const mode = comfyuiGenerationMode();
  if (!repo) {
    clearComfyuiDiffusersInspection();
    if (!quiet) setComfyuiMessage("請先輸入 Hugging Face repo id 或模型頁網址；不要填權重檔名。", false);
    return null;
  }
  const cacheKey = comfyuiDiffusersInspectCacheKey(repo, mode);
  const cached = getCachedComfyuiDiffusersInspection(cacheKey);
  if (cached) {
    comfyuiDiffusersInspection = { repo: cached.repo_id || repo, mode, data: cached, cached: true };
    renderComfyuiDiffusersInspection();
    if (!cached.supported_for_mode && !quiet) {
      setComfyuiMessage(`這個 Hugging Face repo 不支援「${comfyuiReadableModeLabel(mode)}」，尚未開始下載。`, false);
    }
    return cached;
  }
  if (comfyuiDiffusersInspectInflight.has(cacheKey)) {
    return comfyuiDiffusersInspectInflight.get(cacheKey);
  }
  comfyuiDiffusersInspection = { repo, mode, loading: true };
  renderComfyuiDiffusersInspection();
  const requestPromise = (async () => {
    const query = new URLSearchParams({
      diffusers_model_repo: repo,
      generation_mode: mode,
    });
    const suffix = comfyuiRequestQuery();
    if (suffix.startsWith("?")) {
      new URLSearchParams(suffix.slice(1)).forEach((value, key) => query.set(key, value));
    }
    const res = await apiFetch(API + "/comfyui/diffusers/inspect?" + query.toString(), {
      credentials: "same-origin"
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `Hugging Face repo 檢查失敗（HTTP ${res.status}）`);
    cacheComfyuiDiffusersInspection(cacheKey, json);
    comfyuiDiffusersInspection = { repo: json.repo_id || repo, mode, data: json };
    renderComfyuiDiffusersInspection();
    if (!json.supported_for_mode && !quiet) {
      setComfyuiMessage(`這個 Hugging Face repo 不支援「${comfyuiReadableModeLabel(mode)}」，尚未開始下載。`, false);
    }
    return json;
  })();
  comfyuiDiffusersInspectInflight.set(cacheKey, requestPromise);
  try {
    return await requestPromise;
  } catch (err) {
    comfyuiDiffusersInspection = { repo, mode, error: err.message || "Hugging Face repo 檢查失敗，尚未開始下載。" };
    renderComfyuiDiffusersInspection();
    if (!quiet) setComfyuiMessage(comfyuiDiffusersInspection.error, false);
    return null;
  } finally {
    comfyuiDiffusersInspectInflight.delete(cacheKey);
  }
}

function comfyuiModeUsesSourceImage(mode = comfyuiGenerationMode()) {
  return ["img2img", "inpaint", "outpaint", "upscale", "i2v"].includes(String(mode || "").trim().toLowerCase());
}

function comfyuiShouldShowSourceImageCard(mode = comfyuiGenerationMode()) {
  if (comfyuiDiffusersTextOnlyMode()) return false;
  if (!isComfyuiDiffusersMode()) return true;
  return comfyuiModeUsesSourceImage(mode) || comfyuiHasInputAsset("source") || comfyuiHasInputAsset("mask");
}

function comfyuiModeUsesMaskImage(mode = comfyuiGenerationMode()) {
  return String(mode || "").trim().toLowerCase() === "inpaint";
}

function comfyuiModeUsesOutpaint(mode = comfyuiGenerationMode()) {
  return String(mode || "").trim().toLowerCase() === "outpaint";
}

function comfyuiModeUsesUpscale(mode = comfyuiGenerationMode()) {
  return String(mode || "").trim().toLowerCase() === "upscale";
}

function normalizeComfyuiGenerationModeAlias(mode) {
  const normalized = String(mode || "").trim().toLowerCase();
  return {
    txt2image: "txt2img",
    text2img: "txt2img",
    text2image: "txt2img",
    "text-to-img": "txt2img",
    "text-to-image": "txt2img",
    image2image: "img2img",
    "image-to-image": "img2img",
    text2text: "t2t",
    "text-to-text": "t2t",
    "text2text-generation": "t2t",
    "text-generation": "t2t",
    summarization: "t2t",
    translation: "t2t",
    image2text: "i2t",
    "image-to-text": "i2t",
    "image-text-to-text": "i2t",
    "visual-question-answering": "i2t",
    "document-question-answering": "i2t",
    "image-classification": "i2t",
    t2a: "t2s",
    text2audio: "t2s",
    "text-to-audio": "t2s",
    text2speech: "t2s",
    "text-to-speech": "t2s",
  }[normalized] || normalized;
}

function comfyuiModeRequiresWorkflowTemplate(mode = comfyuiGenerationMode()) {
  if (isComfyuiDiffusersMode()) return false;
  return ["t2v", "i2v", "v2v", "t2s", "t2sv", "t2t", "i2t"].includes(normalizeComfyuiGenerationModeAlias(mode));
}

function isComfyuiControlnetEnabled() {
  return !!$("comfyui-controlnet-enabled")?.checked;
}

function comfyuiModeTip(mode = comfyuiGenerationMode()) {
  switch (String(mode || "").trim().toLowerCase()) {
    case "img2img":
      return "圖生圖：以上傳來源圖為基底重畫，適合保留構圖與色塊。";
    case "inpaint":
      return "局部重繪：需要來源圖與遮罩圖，只重畫你標出的區域。";
    case "outpaint":
      return "向外延展：以來源圖為中心向外擴邊，讓模型補完畫面外框。";
    case "upscale":
      return "放大修復：使用 scale model 先放大，再保留原圖細節。";
    case "t2v":
      return "文字生影片：請在上方 Workflow 模板選擇 Wan / AnimateDiff / 影片模型工作流後執行。";
    case "i2v":
      return "圖生影片：先選來源圖片，再在上方 Workflow 模板選擇支援 I2V 的工作流後執行。";
    case "v2v":
      return "影片生影片：需使用含影片讀取節點的 workflow 模板，目前一般產圖按鈕不直接建立影片工作流。";
    case "t2s":
      return "文字轉語音：需使用 TTS workflow 模板，輸出會以音訊媒體顯示。";
    case "t2sv":
      return "文字生成語音影片：需使用同時輸出音訊/影片的 workflow 模板。";
    case "t2t":
      return "文字轉文字：此類 HF repo 目前只做 metadata 辨識；若要執行需使用對應 workflow 或文字模型後端。";
    case "i2t":
      return "圖片轉文字：此類 HF repo 目前只做 metadata 辨識；若要執行需使用對應 workflow 或視覺語言模型後端。";
    default:
      return "文字生圖：只需要提示詞，不需要來源圖片。";
  }
}

function comfyuiReadableModeLabel(mode = comfyuiGenerationMode()) {
  const normalized = normalizeComfyuiGenerationModeAlias(mode);
  const hit = comfyuiGenerationModes.find((item) => String(item?.key || "").trim().toLowerCase() === normalized);
  if (hit?.label) return String(hit.label);
  return {
    txt2img: "文字生圖",
    img2img: "圖生圖",
    inpaint: "局部重繪",
    outpaint: "向外延展",
    upscale: "放大修復",
    t2v: "文字生影片",
    i2v: "圖生影片",
    v2v: "影片生影片",
    t2s: "文字轉語音",
    t2sv: "文字生成語音影片",
    t2t: "文字轉文字",
    i2t: "圖片轉文字",
  }[normalized] || normalized || "文字生圖";
}

function fillComfyuiGenerationModes(values = []) {
  const previous = $("comfyui-generation-mode")?.value || "";
  comfyuiGenerationModes = Array.isArray(values) && values.length
    ? values
      .filter((item) => item && typeof item === "object" && item.key)
      .map((item) => ({
        key: String(item.key),
        label: String(item.label || item.key),
        available: item.available !== false,
        workflow_only: item.workflow_only === true,
        output_kind: item.output_kind || "image",
        source_kind: item.source_kind || "",
      }))
    : [
        { key: "txt2img", label: "文字生圖", available: true },
        { key: "img2img", label: "圖生圖", available: true },
        { key: "inpaint", label: "局部重繪", available: true },
        { key: "outpaint", label: "向外延展", available: true },
        { key: "upscale", label: "放大修復", available: true },
        { key: "t2v", label: "文字生影片", available: true, workflow_only: true },
        { key: "i2v", label: "圖生影片", available: true, workflow_only: true },
        { key: "v2v", label: "影片生影片", available: true, workflow_only: true },
        { key: "t2s", label: "文字轉語音", available: true, workflow_only: true },
        { key: "t2sv", label: "文字生成語音影片", available: true, workflow_only: true },
      ];
  const select = $("comfyui-generation-mode");
  if (!select) return;
  select.innerHTML = comfyuiGenerationModes.map((item) => (
    `<option value="${sanitize(item.key)}"${item.available ? "" : ' disabled="disabled"'}>${sanitize(item.label)}${item.workflow_only ? "（workflow）" : ""}</option>`
  )).join("");
  if (previous) select.value = previous;
  const selectedOption = select.selectedOptions && select.selectedOptions[0] ? select.selectedOptions[0] : null;
  if (!select.value || selectedOption?.disabled) {
    const preferred = comfyuiGenerationModes.find((item) => item.available && item.key === "txt2img")
      || comfyuiGenerationModes.find((item) => item.available);
    if (preferred) select.value = preferred.key;
  }
}

function renderComfyuiModelFamilyHints(values = []) {
  comfyuiModelFamilies = Array.isArray(values) ? values : [];
  const host = $("comfyui-model-family-hint");
  if (!host) return;
  if (isComfyuiDiffusersMode()) {
    host.textContent = "Diffusers 模式使用上方 Hugging Face repo 欄位；可輸入 namespace/model 或模型頁網址。";
    return;
  }
  if (!comfyuiModelFamilies.length) {
    host.textContent = "支援一般 Checkpoint；Flux、SD3.5、Wan、Anima、NetaYume 等大模型請用對應 workflow 模板與模型目錄。";
    return;
  }
  host.innerHTML = comfyuiModelFamilies.map((item) => {
    const installed = item?.installed === true;
    const matches = Array.isArray(item?.matching_models) ? item.matching_models : [];
    const title = matches.length ? ` title="${sanitize(matches.join(", "))}"` : "";
    return `<span class="comfyui-model-family-chip${installed ? " installed" : ""}"${title}>${sanitize(item?.label || item?.key || "model")}${installed ? " · 已偵測" : ""}</span>`;
  }).join("");
}

function normalizeComfyuiView(view) {
  const value = String(view || "").trim().toLowerCase();
  return ["generate", "hf", "history", "workflow", "models", "favorites", "settings"].includes(value) ? value : "generate";
}

function canManageComfyuiLocalModels(modeOverride = null) {
  return currentUser === "root";
}


function setComfyuiView(view, { persist = true } = {}) {
  const selected = normalizeComfyuiView(view);
  const settingsTab = document.querySelector('[data-comfyui-view="settings"]');
  if (settingsTab) settingsTab.hidden = currentUser !== "root";
  const settingsUnavailable = selected === "settings" && currentUser !== "root";
  const activeView = settingsUnavailable ? "generate" : selected;
  comfyuiActiveBackendFamily = activeView === "hf" ? "hf" : "comfyui";
  document.querySelectorAll("[data-comfyui-view]").forEach((button) => {
    const isActive = button.dataset.comfyuiView === activeView;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-selected", isActive ? "true" : "false");
  });
  document.querySelectorAll("[data-comfyui-view-panel]").forEach((panel) => {
    const panelView = panel.dataset.comfyuiViewPanel;
    const isActive = panelView === activeView || (activeView === "hf" && panelView === "generate");
    panel.hidden = !isActive;
    panel.classList.toggle("active", isActive);
  });
  if (persist) {
    try { localStorage.setItem(comfyuiUserStorageKey(COMFYUI_VIEW_STORAGE_KEY), activeView); } catch (_) {}
  }
  if (activeView === "history") {
    loadComfyuiHistory().catch((err) => setComfyuiHistoryActionMessage(err?.message || "ComfyUI 歷史讀取失敗", false));
  }
  if (activeView === "favorites") {
    loadComfyuiImageFavorites().catch((err) => setComfyuiMessage(err?.message || "圖片收藏讀取失敗", false));
  }
  if (activeView === "workflow") {
    loadComfyuiWorkflowPresets().catch((err) => {
      if (typeof setComfyuiWorkflowStatus === "function") {
        setComfyuiWorkflowStatus(err?.message || "Workflow preset 讀取失敗");
      } else {
        setComfyuiMessage(err?.message || "Workflow preset 讀取失敗", false);
      }
    });
  }
  if (activeView === "settings" && typeof loadSettings === "function") {
    loadSettings();
  }
  if (currentUser && (activeView === "generate" || activeView === "hf")) {
    startComfyuiResourceDashboardPolling();
  } else {
    stopComfyuiResourceDashboardPolling();
  }
  updateComfyuiModeNote();
  updateComfyuiStartButton();
  updateComfyuiDiffusersUi();
  updateComfyuiRootPanelVisibility();
  updateComfyuiStatusForActiveBackend();
}

function bindComfyuiSubnav() {
  document.querySelectorAll("[data-comfyui-view]").forEach((button) => {
    if (button.dataset.comfyuiViewBound === "1") return;
    button.dataset.comfyuiViewBound = "1";
    button.addEventListener("click", () => setComfyuiView(button.dataset.comfyuiView));
  });
  let stored = "";
  try { stored = localStorage.getItem(comfyuiUserStorageKey(COMFYUI_VIEW_STORAGE_KEY)) || ""; } catch (_) {}
  setComfyuiView(stored || "generate", { persist: false });
}

function updateComfyuiDiffusersUi() {
  const diffusers = isComfyuiDiffusersMode();
  const textOnly = comfyuiDiffusersTextOnlyMode();
  const templateSelector = document.querySelector("#module-comfyui .comfyui-template-selector");
  const importBtn = $("comfyui-template-import-btn");
  const legacy = $("comfyui-legacy-form-panel");
  const repoField = $("comfyui-diffusers-repo-field");
  const repoInput = $("comfyui-diffusers-model-repo");
  const modelSelect = $("comfyui-model-select");
  const modelField = modelSelect?.closest(".field");
  const vaeField = $("comfyui-vae-select")?.closest(".field");
  const controlPanel = document.querySelector("#module-comfyui .comfyui-controlnet-panel");
  const loraPanel = document.querySelector("#module-comfyui .comfyui-lora-panel");
  const loraNote = $("comfyui-lora-price-note");
  if (templateSelector) templateSelector.style.display = diffusers ? "none" : "";
  if (importBtn) importBtn.style.display = diffusers ? "none" : "";
  if (legacy && diffusers) {
    legacy.style.display = "";
    legacy.open = true;
  }
  if (repoField) repoField.style.display = diffusers ? "" : "none";
  if (repoInput && diffusers && repoInput.value) sanitizeComfyuiDiffusersRepoField({ strict: false, quiet: true });
  if (repoInput && diffusers && !repoInput.value && modelSelect?.value) {
    const candidateRepo = normalizeComfyuiHuggingFaceRepoInput(modelSelect.value);
    if (candidateRepo && isComfyuiHuggingFaceRepoLike(candidateRepo)) {
      repoInput.value = candidateRepo;
    }
  }
  if (modelField) modelField.style.display = diffusers ? "none" : "";
  if (vaeField) vaeField.style.display = diffusers ? "none" : "";
  if (controlPanel) controlPanel.style.display = diffusers ? "none" : "";
  if (loraPanel) loraPanel.style.display = diffusers ? "none" : "";
  if (loraNote) loraNote.style.display = diffusers ? "none" : "";
  if (diffusers) {
    if (textOnly) clearComfyuiDiffusersImageAssetsForTextOnly();
    const selectedMode = comfyuiGenerationModes.find((item) => item.key === comfyuiGenerationMode());
    if (selectedMode && selectedMode.available === false) {
      const firstAvailable = comfyuiGenerationModes.find((item) => item.available);
      if (firstAvailable && $("comfyui-generation-mode")) $("comfyui-generation-mode").value = firstAvailable.key;
    }
    const controlCheckbox = $("comfyui-controlnet-enabled");
    if (controlCheckbox?.checked) controlCheckbox.checked = false;
  }
  updateComfyuiDiffusersGgufOptions();
}

function fillComfyuiControlnetTypes(types = {}) {
  comfyuiControlnetTypes = types && typeof types === "object" ? types : {};
  const select = $("comfyui-controlnet-type");
  if (!select) return;
  const options = Object.entries(comfyuiControlnetTypes).length
    ? Object.entries(comfyuiControlnetTypes).map(([key, item]) => {
        const available = item && item.available === true;
        const label = available
          ? (item.label || key)
          : `${item?.label || key}（缺少 nodes / models）`;
        return `<option value="${sanitize(key)}"${available ? "" : ' disabled="disabled"'}>${sanitize(label)}</option>`;
      }).join("")
    : Object.entries(COMFYUI_CONTROLNET_TIPS).map(([key]) => `<option value="${sanitize(key)}">${sanitize(key)}</option>`).join("");
  select.innerHTML = options;
}

function comfyuiSelectedControlnetType() {
  return String($("comfyui-controlnet-type")?.value || "canny").trim().toLowerCase() || "canny";
}

function fillComfyuiControlnetModelOptions() {
  const select = $("comfyui-controlnet-model");
  if (!select) return;
  const type = comfyuiSelectedControlnetType();
  const info = comfyuiControlnetTypes?.[type] || {};
  const models = Array.isArray(info.matching_models) ? info.matching_models : [];
  const previous = select.value || "";
  select.innerHTML = ['<option value="">自動選擇可用模型</option>']
    .concat(models.map((model) => `<option value="${sanitize(model)}">${sanitize(model)}</option>`))
    .join("");
  if (previous && models.includes(previous)) select.value = previous;
}

function fillComfyuiControlnetPreprocessorOptions() {
  const select = $("comfyui-controlnet-preprocessor");
  if (!select) return;
  const type = comfyuiSelectedControlnetType();
  const info = comfyuiControlnetTypes?.[type] || {};
  const preprocessors = Array.isArray(info.available_preprocessors) ? info.available_preprocessors : [];
  const previous = select.value || "";
  select.innerHTML = ['<option value="">自動選擇</option>']
    .concat(preprocessors.map((name) => `<option value="${sanitize(name)}">${sanitize(name)}</option>`))
    .join("");
  if (previous && preprocessors.includes(previous)) {
    select.value = previous;
  } else if (!previous && info.default_preprocessor) {
    select.value = info.default_preprocessor;
  }
}

function fillComfyuiUpscaleModels(values = []) {
  comfyuiUpscaleModels = normalizeComfyuiModelOptionValues(values);
  const select = $("comfyui-upscale-model");
  if (!select) return;
  const previous = select.value || "";
  const options = comfyuiUpscaleModels.length
    ? ['<option value="">不使用放大模型</option>'].concat(comfyuiUpscaleModels.map((value) => `<option value="${sanitize(value)}">${sanitize(value)}</option>`))
    : ['<option value="">目前沒有可用的放大模型</option>'];
  select.innerHTML = options.join("");
  if (previous && comfyuiUpscaleModels.includes(previous)) select.value = previous;
  updateComfyuiModeVisibility();
}

function updateComfyuiControlnetTip() {
  const tip = $("comfyui-controlnet-tip");
  if (!tip) return;
  const type = comfyuiSelectedControlnetType();
  const typeInfo = comfyuiControlnetTypes?.[type] || {};
  const available = typeInfo.available === true;
  const tail = available ? "" : " 目前缺少對應 nodes 或 models，無法建立工作。";
  tip.textContent = `${COMFYUI_CONTROLNET_TIPS[type] || "可利用控制圖約束構圖與細節。"}${tail}`;
}

function updateComfyuiModeVisibility() {
  const mode = syncComfyuiGenerationMode();
  const diffusersTextOnly = comfyuiDiffusersTextOnlyMode();
  const modeTip = $("comfyui-generation-mode-tip");
  const denoiseField = $("comfyui-denoise-field");
  const upscaleField = $("comfyui-upscale-model-field");
  const outpaintPanel = $("comfyui-outpaint-panel");
  const controlFields = $("comfyui-controlnet-fields");
  const sourceCard = $(COMFYUI_INPUT_ASSET_META.source.cardId);
  const controlCard = $(COMFYUI_INPUT_ASSET_META.control.cardId);
  const maskCard = $(COMFYUI_INPUT_ASSET_META.mask.cardId);
  if (modeTip) modeTip.textContent = comfyuiModeTip(mode);
  if (denoiseField) denoiseField.style.display = mode === "txt2img" || mode === "upscale" ? "none" : "";
  if (upscaleField) upscaleField.style.display = comfyuiHasInputAsset("source") || comfyuiModeUsesUpscale(mode) ? "" : "none";
  if (outpaintPanel) outpaintPanel.style.display = comfyuiModeUsesOutpaint(mode) ? "" : "none";
  if (sourceCard) sourceCard.style.display = comfyuiShouldShowSourceImageCard(mode) || comfyuiDiffusersSupportsImageInputMode() ? "" : "none";
  if (maskCard) maskCard.style.display = diffusersTextOnly ? "none" : (comfyuiHasInputAsset("source") || comfyuiHasInputAsset("mask") || comfyuiModeUsesMaskImage(mode) ? "" : "none");
  if (controlFields) controlFields.style.display = isComfyuiControlnetEnabled() ? "" : "none";
  if (controlCard) controlCard.style.display = diffusersTextOnly ? "none" : (isComfyuiControlnetEnabled() ? "" : "none");
  updateComfyuiControlnetTip();
  updateComfyuiDiffusersUi();
}

function comfyuiAssetState(key) {
  return comfyuiInputAssets[key] || { file: null, imageRef: null, previewUrl: "", filename: "", cloudFileId: "" };
}

function revokeComfyuiAssetPreview(asset) {
  if (asset?.previewUrl && String(asset.previewUrl).startsWith("blob:")) {
    try { URL.revokeObjectURL(asset.previewUrl); } catch (_err) {}
  }
}

function renderComfyuiInputAsset(key) {
  const meta = COMFYUI_INPUT_ASSET_META[key];
  if (!meta) return;
  const preview = $(meta.previewId);
  const status = $(meta.metaId);
  const clearBtn = $(meta.clearBtnId);
  const editBtn = meta.editBtnId ? $(meta.editBtnId) : null;
  const fileInput = $(meta.fileInputId);
  const asset = comfyuiAssetState(key);
  if (preview) {
    if (asset.previewUrl) {
      preview.innerHTML = meta.mediaType === "video"
        ? `<video src="${sanitize(asset.previewUrl)}" controls muted preload="metadata"></video>`
        : `<img src="${sanitize(asset.previewUrl)}" alt="${sanitize(meta.title)}預覽" />`;
    } else {
      preview.innerHTML = `<span class="drive-card-sub">${sanitize(meta.emptyText)}</span>`;
    }
  }
  if (status) {
    if (asset.file) {
      status.textContent = `已選擇本地檔：${asset.filename || asset.file.name || "未命名檔案"} · ${formatDriveBytes(asset.file.size || 0)}`;
    } else if (asset.imageRef?.filename) {
      const sourceText = asset.cloudFileId ? "雲端硬碟 / 歷史匯入" : "已保存";
      status.textContent = `使用${sourceText}的 ${meta.title}：${asset.filename || asset.imageRef.filename}`;
    } else {
      status.textContent = meta.mediaType === "video"
        ? "可上傳 MP4、WEBM、MOV、MKV、AVI。"
        : (key === "mask" ? "建議與來源圖片尺寸一致。" : (key === "control" ? "控制圖只在啟用 ControlNet 時送出。" : "可上傳 PNG、JPG、WEBP。"));
    }
  }
  if (clearBtn) clearBtn.disabled = !asset.file && !asset.imageRef;
  if (editBtn) {
    const canEdit = comfyuiCanOpenMaskEditor();
    editBtn.disabled = !canEdit;
    editBtn.title = comfyuiAssetState("source").previewUrl
      ? "在來源圖片上直接畫出 inpaint 遮罩"
      : "尚未選來源圖時會開啟空白遮罩畫布。";
  }
  if (fileInput && !asset.file) fileInput.value = "";
}

function setComfyuiInputAssetFromFile(key, file) {
  if (!comfyuiCanUseInputAssetForCurrentMode(key)) {
    const meta = COMFYUI_INPUT_ASSET_META[key];
    const fileInput = meta?.fileInputId ? $(meta.fileInputId) : null;
    if (fileInput) fileInput.value = "";
    setComfyuiMessage("這個 HF repo 已判斷為文字生圖，不需要來源圖片或遮罩。", false);
    return;
  }
  const asset = comfyuiAssetState(key);
  revokeComfyuiAssetPreview(asset);
  comfyuiInputAssets[key] = {
    file,
    imageRef: null,
    previewUrl: file ? URL.createObjectURL(file) : "",
    filename: file?.name || "",
    cloudFileId: "",
  };
  renderComfyuiInputAsset(key);
  if (key === "source") renderComfyuiInputAsset("mask");
  updateComfyuiModeVisibility();
  if (typeof queueRenderSelectedComfyuiTemplate === "function") queueRenderSelectedComfyuiTemplate();
}

function setComfyuiInputAssetFromRef(key, imageRef, previewUrl = "", filename = "", options = {}) {
  if (!comfyuiCanUseInputAssetForCurrentMode(key)) {
    setComfyuiMessage("這個 HF repo 已判斷為文字生圖，不需要來源圖片或遮罩。", false);
    return;
  }
  const asset = comfyuiAssetState(key);
  if (!asset?.previewUrl || asset.previewUrl !== previewUrl) revokeComfyuiAssetPreview(asset);
  comfyuiInputAssets[key] = {
    file: null,
    imageRef: imageRef || null,
    previewUrl: previewUrl || "",
    filename: filename || imageRef?.filename || "",
    cloudFileId: options.cloudFileId || "",
  };
  renderComfyuiInputAsset(key);
  if (key === "source") renderComfyuiInputAsset("mask");
  updateComfyuiModeVisibility();
  if (typeof queueRenderSelectedComfyuiTemplate === "function") queueRenderSelectedComfyuiTemplate();
}

function clearComfyuiInputAsset(key) {
  const asset = comfyuiAssetState(key);
  revokeComfyuiAssetPreview(asset);
  comfyuiInputAssets[key] = { file: null, imageRef: null, previewUrl: "", filename: "", cloudFileId: "" };
  renderComfyuiInputAsset(key);
  if (key === "source") renderComfyuiInputAsset("mask");
  updateComfyuiModeVisibility();
  if (typeof queueRenderSelectedComfyuiTemplate === "function") queueRenderSelectedComfyuiTemplate();
}

function comfyuiCanOpenMaskEditor() {
  return true;
}

function comfyuiMaskEditorElements() {
  return {
    modal: $("comfyui-mask-editor-modal"),
    sourceCanvas: $("comfyui-mask-editor-source-canvas"),
    maskCanvas: $("comfyui-mask-editor-mask-canvas"),
    stage: $("comfyui-mask-editor-stage"),
    meta: $("comfyui-mask-editor-meta"),
    brush: $("comfyui-mask-editor-brush"),
    brushLabel: $("comfyui-mask-editor-brush-label"),
    widthInput: $("comfyui-mask-editor-width"),
    heightInput: $("comfyui-mask-editor-height"),
    paintBtn: $("comfyui-mask-editor-paint-btn"),
    eraseBtn: $("comfyui-mask-editor-erase-btn"),
  };
}

function loadComfyuiMaskEditorImage(src) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("來源圖片無法載入遮罩編輯器。"));
    image.src = src;
  });
}

function updateComfyuiMaskEditorToolbar() {
  const { brush, brushLabel, paintBtn, eraseBtn } = comfyuiMaskEditorElements();
  const size = Math.max(4, Math.min(256, Number(brush?.value || 56)));
  if (brushLabel) brushLabel.textContent = `${Math.round(size)}px`;
  if (paintBtn) paintBtn.setAttribute("aria-pressed", comfyuiMaskEditorState.mode === "paint" ? "true" : "false");
  if (eraseBtn) eraseBtn.setAttribute("aria-pressed", comfyuiMaskEditorState.mode === "erase" ? "true" : "false");
}

function setComfyuiMaskEditorMode(mode) {
  comfyuiMaskEditorState.mode = mode === "erase" ? "erase" : "paint";
  updateComfyuiMaskEditorToolbar();
}

function comfyuiMaskEditorDimension(value, fallback) {
  const numeric = Number(value);
  const base = Number.isFinite(numeric) ? numeric : fallback;
  const rounded = Math.round(Number(base || 1024) / 8) * 8;
  return Math.max(64, Math.min(2048, rounded || 1024));
}

function fillComfyuiMaskEditorBlankSource(width, height) {
  const { sourceCanvas } = comfyuiMaskEditorElements();
  if (!sourceCanvas) return;
  const ctx = sourceCanvas.getContext("2d");
  ctx.clearRect(0, 0, sourceCanvas.width, sourceCanvas.height);
  ctx.fillStyle = "#1f2937";
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = "rgba(255,255,255,.18)";
  ctx.lineWidth = 2;
  ctx.setLineDash([12, 10]);
  ctx.strokeRect(8, 8, Math.max(0, width - 16), Math.max(0, height - 16));
  ctx.setLineDash([]);
  ctx.fillStyle = "rgba(255,255,255,.72)";
  ctx.font = `${Math.max(18, Math.min(34, Math.round(width / 28)))}px system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText("空白遮罩畫布", width / 2, height / 2);
}

function setComfyuiMaskEditorDimensionInputs(width, height, disabled = false) {
  const { widthInput, heightInput } = comfyuiMaskEditorElements();
  if (widthInput) {
    widthInput.value = String(width);
    widthInput.disabled = !!disabled;
    widthInput.title = disabled ? "已依來源圖片尺寸鎖定。" : "空白遮罩尺寸。";
  }
  if (heightInput) {
    heightInput.value = String(height);
    heightInput.disabled = !!disabled;
    heightInput.title = disabled ? "已依來源圖片尺寸鎖定。" : "空白遮罩尺寸。";
  }
}

function resizeComfyuiMaskEditorCanvases(width, height, options = {}) {
  const { sourceCanvas, maskCanvas } = comfyuiMaskEditorElements();
  const safeWidth = comfyuiMaskEditorDimension(width, comfyuiDefaultWidth);
  const safeHeight = comfyuiMaskEditorDimension(height, comfyuiDefaultHeight);
  [sourceCanvas, maskCanvas].forEach((canvas) => {
    if (!canvas) return;
    canvas.width = safeWidth;
    canvas.height = safeHeight;
  });
  comfyuiMaskEditorState.width = safeWidth;
  comfyuiMaskEditorState.height = safeHeight;
  if (options.syncInputs !== false) setComfyuiMaskEditorDimensionInputs(safeWidth, safeHeight, !!options.lockInputs);
}

function drawComfyuiExistingMaskIntoEditor(maskImage) {
  const { maskCanvas } = comfyuiMaskEditorElements();
  if (!maskCanvas || !maskImage) return;
  const ctx = maskCanvas.getContext("2d");
  const temp = document.createElement("canvas");
  temp.width = maskCanvas.width;
  temp.height = maskCanvas.height;
  const tempCtx = temp.getContext("2d");
  tempCtx.drawImage(maskImage, 0, 0, temp.width, temp.height);
  const imageData = tempCtx.getImageData(0, 0, temp.width, temp.height);
  const data = imageData.data;
  for (let i = 0; i < data.length; i += 4) {
    const alpha = data[i + 3] / 255;
    const luminance = (data[i] * 0.299) + (data[i + 1] * 0.587) + (data[i + 2] * 0.114);
    data[i] = 255;
    data[i + 1] = 255;
    data[i + 2] = 255;
    data[i + 3] = Math.max(0, Math.min(255, Math.round(luminance * alpha)));
  }
  ctx.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
  ctx.putImageData(imageData, 0, 0);
}

async function openComfyuiMaskEditor() {
  const sourceAsset = comfyuiAssetState("source");
  const { modal, sourceCanvas, maskCanvas, meta } = comfyuiMaskEditorElements();
  if (!modal || !sourceCanvas || !maskCanvas) {
    setComfyuiMessage("遮罩編輯器尚未載入，請重新整理頁面。", false);
    return;
  }
  try {
    let width = comfyuiMaskEditorDimension($("comfyui-width")?.value || comfyuiDefaultWidth, comfyuiDefaultWidth);
    let height = comfyuiMaskEditorDimension($("comfyui-height")?.value || comfyuiDefaultHeight, comfyuiDefaultHeight);
    let sourceImage = null;
    if (sourceAsset.previewUrl) {
      sourceImage = await loadComfyuiMaskEditorImage(sourceAsset.previewUrl);
      width = comfyuiMaskEditorDimension(sourceImage.naturalWidth || sourceImage.width || width, width);
      height = comfyuiMaskEditorDimension(sourceImage.naturalHeight || sourceImage.height || height, height);
    }
    resizeComfyuiMaskEditorCanvases(width, height, { lockInputs: !!sourceImage });
    if (sourceImage) {
      sourceCanvas.getContext("2d").drawImage(sourceImage, 0, 0, width, height);
    } else {
      fillComfyuiMaskEditorBlankSource(width, height);
    }
    maskCanvas.getContext("2d").clearRect(0, 0, width, height);
    const maskAsset = comfyuiAssetState("mask");
    if (maskAsset.previewUrl) {
      try {
        const maskImage = await loadComfyuiMaskEditorImage(maskAsset.previewUrl);
        drawComfyuiExistingMaskIntoEditor(maskImage);
      } catch (_) {}
    }
    if (meta) {
      meta.textContent = sourceImage
        ? `${width} x ${height}；白色區域會交給 inpaint 重繪。`
        : `${width} x ${height}；尚未選來源圖，請確認遮罩尺寸要和稍後來源圖一致。`;
    }
    comfyuiMaskEditorState.open = true;
    comfyuiMaskEditorState.drawing = false;
    comfyuiMaskEditorState.lastPoint = null;
    comfyuiMaskEditorState.pointerId = null;
    comfyuiMaskEditorState.hasSource = !!sourceImage;
    setComfyuiMaskEditorMode("paint");
    modal.hidden = false;
    updateComfyuiMaskEditorToolbar();
  } catch (err) {
    setComfyuiMessage(err.message || "遮罩編輯器開啟失敗。", false);
  }
}

function closeComfyuiMaskEditor() {
  const { modal } = comfyuiMaskEditorElements();
  comfyuiMaskEditorState.open = false;
  comfyuiMaskEditorState.drawing = false;
  comfyuiMaskEditorState.lastPoint = null;
  comfyuiMaskEditorState.pointerId = null;
  comfyuiMaskEditorState.hasSource = false;
  if (modal) modal.hidden = true;
}

function applyComfyuiMaskEditorSize() {
  const { maskCanvas, meta, widthInput, heightInput } = comfyuiMaskEditorElements();
  if (!maskCanvas || comfyuiMaskEditorState.hasSource) return;
  const previous = document.createElement("canvas");
  previous.width = maskCanvas.width || 1;
  previous.height = maskCanvas.height || 1;
  previous.getContext("2d").drawImage(maskCanvas, 0, 0);
  const width = comfyuiMaskEditorDimension(widthInput?.value || comfyuiDefaultWidth, comfyuiDefaultWidth);
  const height = comfyuiMaskEditorDimension(heightInput?.value || comfyuiDefaultHeight, comfyuiDefaultHeight);
  resizeComfyuiMaskEditorCanvases(width, height, { lockInputs: false });
  fillComfyuiMaskEditorBlankSource(width, height);
  maskCanvas.getContext("2d").drawImage(previous, 0, 0, width, height);
  if (meta) meta.textContent = `${width} x ${height}；空白遮罩畫布，請確認尺寸與來源圖一致。`;
}

function comfyuiMaskEditorPoint(event) {
  const { maskCanvas } = comfyuiMaskEditorElements();
  if (!maskCanvas) return null;
  const rect = maskCanvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return {
    x: Math.max(0, Math.min(maskCanvas.width, (event.clientX - rect.left) * (maskCanvas.width / rect.width))),
    y: Math.max(0, Math.min(maskCanvas.height, (event.clientY - rect.top) * (maskCanvas.height / rect.height))),
  };
}

function drawComfyuiMaskStroke(point) {
  const { maskCanvas, brush } = comfyuiMaskEditorElements();
  if (!maskCanvas || !point) return;
  const ctx = maskCanvas.getContext("2d");
  const size = Math.max(4, Math.min(256, Number(brush?.value || 56)));
  const last = comfyuiMaskEditorState.lastPoint || point;
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.lineWidth = size;
  ctx.globalCompositeOperation = comfyuiMaskEditorState.mode === "erase" ? "destination-out" : "source-over";
  ctx.strokeStyle = "rgba(255,255,255,1)";
  ctx.fillStyle = "rgba(255,255,255,1)";
  ctx.beginPath();
  ctx.moveTo(last.x, last.y);
  ctx.lineTo(point.x, point.y);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(point.x, point.y, size / 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
  comfyuiMaskEditorState.lastPoint = point;
}

function handleComfyuiMaskPointerDown(event) {
  if (!comfyuiMaskEditorState.open) return;
  const { maskCanvas } = comfyuiMaskEditorElements();
  const point = comfyuiMaskEditorPoint(event);
  if (!maskCanvas || !point) return;
  event.preventDefault();
  comfyuiMaskEditorState.drawing = true;
  comfyuiMaskEditorState.pointerId = event.pointerId;
  comfyuiMaskEditorState.lastPoint = point;
  try { maskCanvas.setPointerCapture(event.pointerId); } catch (_) {}
  drawComfyuiMaskStroke(point);
}

function handleComfyuiMaskPointerMove(event) {
  if (!comfyuiMaskEditorState.drawing || comfyuiMaskEditorState.pointerId !== event.pointerId) return;
  const point = comfyuiMaskEditorPoint(event);
  if (!point) return;
  event.preventDefault();
  drawComfyuiMaskStroke(point);
}

function stopComfyuiMaskPointer(event) {
  const { maskCanvas } = comfyuiMaskEditorElements();
  if (maskCanvas && comfyuiMaskEditorState.pointerId !== null) {
    try { maskCanvas.releasePointerCapture(comfyuiMaskEditorState.pointerId); } catch (_) {}
  }
  comfyuiMaskEditorState.drawing = false;
  comfyuiMaskEditorState.lastPoint = null;
  comfyuiMaskEditorState.pointerId = null;
  if (event) event.preventDefault();
}

function clearComfyuiMaskEditorCanvas(fill = false) {
  const { maskCanvas } = comfyuiMaskEditorElements();
  if (!maskCanvas) return;
  const ctx = maskCanvas.getContext("2d");
  ctx.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
  if (fill) {
    ctx.fillStyle = "rgba(255,255,255,1)";
    ctx.fillRect(0, 0, maskCanvas.width, maskCanvas.height);
  }
}

function invertComfyuiMaskEditorCanvas() {
  const { maskCanvas } = comfyuiMaskEditorElements();
  if (!maskCanvas) return;
  const ctx = maskCanvas.getContext("2d");
  const imageData = ctx.getImageData(0, 0, maskCanvas.width, maskCanvas.height);
  const data = imageData.data;
  for (let i = 0; i < data.length; i += 4) {
    data[i] = 255;
    data[i + 1] = 255;
    data[i + 2] = 255;
    data[i + 3] = 255 - data[i + 3];
  }
  ctx.putImageData(imageData, 0, 0);
}

function comfyuiMaskEditorBlob() {
  const { maskCanvas } = comfyuiMaskEditorElements();
  if (!maskCanvas) return Promise.reject(new Error("遮罩畫布不存在。"));
  const output = document.createElement("canvas");
  output.width = maskCanvas.width;
  output.height = maskCanvas.height;
  const ctx = output.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, output.width, output.height);
  ctx.drawImage(maskCanvas, 0, 0);
  return new Promise((resolve, reject) => {
    output.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("遮罩輸出失敗。"));
    }, "image/png");
  });
}

async function applyComfyuiMaskEditor() {
  try {
    const blob = await comfyuiMaskEditorBlob();
    const sourceName = String(comfyuiAssetState("source").filename || "source").replace(/\.[^.]+$/, "");
    const filename = `${sourceName || "source"}-mask.png`;
    let file;
    try {
      file = new File([blob], filename, { type: "image/png", lastModified: Date.now() });
    } catch (_) {
      file = blob;
      file.name = filename;
    }
    setComfyuiInputAssetFromFile("mask", file);
    closeComfyuiMaskEditor();
    setComfyuiMessage("已套用遮罩；送出時會以 mask_image 傳給 ComfyUI。", true);
  } catch (err) {
    setComfyuiMessage(err.message || "遮罩套用失敗。", false);
  }
}

async function loadComfyuiImageRefPreview(imageRef) {
  const res = await apiFetch(API + "/comfyui/image-preview", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || ""
    },
    body: JSON.stringify({ image_ref: imageRef })
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 圖片預覽讀取失敗（HTTP ${res.status}）`);
  return json.image || {};
}

function comfyuiImageRefCacheKey(imageRef) {
  if (!imageRef?.filename) return "";
  return [
    String(imageRef.type || "output"),
    String(imageRef.subfolder || ""),
    String(imageRef.filename || ""),
  ].join("/");
}

const comfyuiOutputPreviewCache = new Map();
const comfyuiMediaPreviewCache = new Map();
const comfyuiMediaObjectUrlCache = new Map();

function comfyuiDataUrlToBlobUrl(dataUrl, cacheKey = "") {
  const text = String(dataUrl || "");
  if (!text.startsWith("data:") || typeof URL === "undefined" || typeof Blob === "undefined") return "";
  const key = cacheKey || text.slice(0, 160);
  if (key && comfyuiMediaObjectUrlCache.has(key)) return comfyuiMediaObjectUrlCache.get(key);
  const match = text.match(/^data:([^;,]*)(;base64)?,(.*)$/s);
  if (!match) return "";
  const mimeType = match[1] || "application/octet-stream";
  const encoded = match[3] || "";
  let blob;
  try {
    if (match[2]) {
      const binary = atob(encoded);
      const chunks = [];
      for (let offset = 0; offset < binary.length; offset += 8192) {
        const slice = binary.slice(offset, offset + 8192);
        const bytes = new Uint8Array(slice.length);
        for (let index = 0; index < slice.length; index += 1) bytes[index] = slice.charCodeAt(index);
        chunks.push(bytes);
      }
      blob = new Blob(chunks, { type: mimeType });
    } else {
      blob = new Blob([decodeURIComponent(encoded)], { type: mimeType });
    }
    const objectUrl = URL.createObjectURL(blob);
    if (key) comfyuiMediaObjectUrlCache.set(key, objectUrl);
    return objectUrl;
  } catch (err) {
    return "";
  }
}

async function hydrateComfyuiOutputImage(image) {
  if (!image || image.data_url || !image.image_ref?.filename) return image;
  const cacheKey = comfyuiImageRefCacheKey(image.image_ref);
  if (cacheKey && comfyuiOutputPreviewCache.has(cacheKey)) {
    return { ...image, ...comfyuiOutputPreviewCache.get(cacheKey) };
  }
  await fetchCsrfToken();
  const preview = await loadComfyuiImageRefPreview(image.image_ref);
  const hydrated = {
    data_url: preview.data_url || "",
    mime_type: preview.mime_type || image.mime_type || "image/png",
    size_bytes: Number(preview.size_bytes || image.size_bytes || 0),
  };
  if (cacheKey) comfyuiOutputPreviewCache.set(cacheKey, hydrated);
  return { ...image, ...hydrated };
}

async function hydrateComfyuiGeneratedImages(images = []) {
  const items = Array.isArray(images) ? images.filter(Boolean) : [];
  const hydrated = [];
  for (const image of items) {
    hydrated.push(await hydrateComfyuiOutputImage(image));
  }
  return hydrated;
}

async function loadComfyuiMediaPreview(fileRef, jobId) {
  const res = await apiFetch(API + "/comfyui/media-preview", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || ""
    },
    body: JSON.stringify({ file_ref: fileRef, job_id: jobId })
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 媒體預覽讀取失敗（HTTP ${res.status}）`);
  return json.media || {};
}

async function hydrateComfyuiOutputMedia(item, jobId) {
  if (!item) return item;
  if (item.data_url) {
    const existingKey = item.file_ref?.filename
      ? `${jobId || "inline"}:${comfyuiImageRefCacheKey(item.file_ref)}`
      : `inline:${String(item.data_url).slice(0, 160)}`;
    return {
      ...item,
      media_kind: comfyuiMediaItemKind(item),
      preview_url: item.preview_url || comfyuiDataUrlToBlobUrl(item.data_url, existingKey),
    };
  }
  if (!item.file_ref?.filename || !jobId) return item;
  const cacheKey = `${jobId}:${comfyuiImageRefCacheKey(item.file_ref)}`;
  if (cacheKey && comfyuiMediaPreviewCache.has(cacheKey)) {
    return { ...item, ...comfyuiMediaPreviewCache.get(cacheKey) };
  }
  try {
    await fetchCsrfToken({ force: true });
    const preview = await loadComfyuiMediaPreview(item.file_ref, jobId);
    const dataUrl = preview.data_url || "";
    const hydrated = {
      data_url: dataUrl,
      preview_url: dataUrl ? comfyuiDataUrlToBlobUrl(dataUrl, cacheKey) : "",
      mime_type: preview.mime_type || item.mime_type || "application/octet-stream",
      media_kind: comfyuiMediaItemKind({ ...item, ...preview }),
      size_bytes: Number(preview.size_bytes || item.size_bytes || 0),
      file_ref: preview.file_ref || item.file_ref,
    };
    if (cacheKey) comfyuiMediaPreviewCache.set(cacheKey, hydrated);
    return { ...item, ...hydrated };
  } catch (err) {
    return { ...item, preview_error: err.message || "媒體預覽讀取失敗" };
  }
}

async function hydrateComfyuiGeneratedMedia(mediaItems = [], jobId = "") {
  const items = Array.isArray(mediaItems) ? mediaItems.filter(Boolean) : [];
  const hydrated = [];
  for (const item of items) {
    hydrated.push(await hydrateComfyuiOutputMedia(item, jobId));
  }
  return hydrated;
}

async function hydrateComfyuiInputAssetFromRef(key, imageRef) {
  if (!imageRef?.filename) {
    clearComfyuiInputAsset(key);
    return;
  }
  try {
    await fetchCsrfToken({ force: true });
    const preview = await loadComfyuiImageRefPreview(imageRef);
    setComfyuiInputAssetFromRef(key, imageRef, preview.data_url || "", imageRef.filename || "");
  } catch (err) {
    setComfyuiInputAssetFromRef(key, imageRef, "", imageRef.filename || "");
    setComfyuiMessage(err.message || "圖片預覽讀取失敗", false);
  }
}

function comfyuiImagePickerElements() {
  return {
    modal: $("comfyui-image-picker-modal"),
    list: $("comfyui-image-picker-list"),
    status: $("comfyui-image-picker-status"),
    title: $("comfyui-image-picker-title"),
  };
}

function closeComfyuiImagePicker() {
  const { modal } = comfyuiImagePickerElements();
  comfyuiImagePickerState.open = false;
  if (modal) modal.hidden = true;
}

function renderComfyuiImagePickerList() {
  const { list, status, title } = comfyuiImagePickerElements();
  if (!list) return;
  const targetKey = comfyuiImagePickerState.targetKey || "source";
  const targetTitle = COMFYUI_INPUT_ASSET_META[targetKey]?.title || "圖片";
  if (title) title.textContent = `選擇${targetTitle}`;
  const history = Array.isArray(comfyuiImagePickerState.history) ? comfyuiImagePickerState.history : [];
  const cloudDrive = Array.isArray(comfyuiImagePickerState.cloudDrive) ? comfyuiImagePickerState.cloudDrive : [];
  if (status) status.textContent = `可選 ${history.length} 張歷史產圖、${cloudDrive.length} 張雲端硬碟圖片。`;
  const renderItem = (item, index, source) => {
    const filename = item.filename || item.virtual_path || item.file_id || item.history_id || "image";
    const sub = source === "history"
      ? `歷史 #${item.history_id || "-"} · ${String(item.created_at || "").replace("T", " ").slice(0, 16)} · ${comfyuiReadableModeLabel(item.generation_mode || "")}`
      : `${item.virtual_path || "雲端硬碟"} · ${formatDriveBytes(item.size_bytes || 0)} · ${item.scan_status || "-"}`;
    return `
      <div class="comfyui-image-picker-item">
        <div>
          <strong>${sanitize(filename)}</strong>
          <div class="drive-card-sub">${sanitize(sub)}</div>
          ${item.prompt ? `<div class="drive-card-sub">${sanitize(String(item.prompt).slice(0, 120))}</div>` : ""}
        </div>
        <button class="btn btn-sm" type="button" data-comfyui-picker-source="${sanitize(source)}" data-comfyui-picker-index="${index}">使用</button>
      </div>
    `;
  };
  list.innerHTML = `
    <div class="comfyui-image-picker-section">
      <div class="drive-card-title">之前生成的圖片</div>
      ${history.length ? history.map((item, index) => renderItem(item, index, "history")).join("") : '<div class="drive-empty">尚無可用歷史產圖</div>'}
    </div>
    <div class="comfyui-image-picker-section">
      <div class="drive-card-title">雲端硬碟圖片</div>
      ${cloudDrive.length ? cloudDrive.map((item, index) => renderItem(item, index, "cloud_drive")).join("") : '<div class="drive-empty">尚無可用雲端硬碟圖片</div>'}
    </div>
  `;
  list.querySelectorAll("[data-comfyui-picker-source]").forEach((button) => {
    button.addEventListener("click", () => {
      const source = button.getAttribute("data-comfyui-picker-source");
      const index = Number(button.getAttribute("data-comfyui-picker-index") || 0);
      applyComfyuiImagePickerSelection(source, index).catch((err) => setComfyuiMessage(err.message || "圖片選擇失敗", false));
    });
  });
}

async function loadComfyuiImagePickerCandidates() {
  await fetchCsrfToken();
  const res = await apiFetch(API + "/comfyui/input-image-candidates", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `既有圖片清單讀取失敗（HTTP ${res.status}）`);
  comfyuiImagePickerState.history = Array.isArray(json.history) ? json.history : [];
  comfyuiImagePickerState.cloudDrive = Array.isArray(json.cloud_drive) ? json.cloud_drive : [];
}

async function importComfyuiDriveImage(fileId) {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/comfyui/import-drive-image", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || ""
    },
    body: JSON.stringify({ file_id: fileId })
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `雲端硬碟圖片匯入失敗（HTTP ${res.status}）`);
  return json.image || {};
}

async function importComfyuiUploadedImage(assetKey = "source") {
  const asset = comfyuiAssetState(assetKey);
  if (!asset?.file) throw new Error("尚未選擇要匯入的本機圖片。");
  await fetchCsrfToken({ force: true });
  const form = new FormData();
  form.append("image", asset.file, asset.filename || asset.file.name || "image.png");
  form.append("asset_key", assetKey);
  const res = await apiFetch(API + "/comfyui/import-uploaded-image", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "X-CSRF-Token": getCsrfToken() || ""
    },
    body: form
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `本機圖片匯入失敗（HTTP ${res.status}）`);
  const image = json.image || {};
  setComfyuiInputAssetFromRef(assetKey, image.image_ref, image.data_url || asset.previewUrl || "", image.filename || asset.filename || "", {
    cloudFileId: image.cloud_file_id || "",
  });
  return image;
}

async function importComfyuiUploadedVideo(assetKey = "video") {
  const asset = comfyuiAssetState(assetKey);
  if (!asset?.file) throw new Error("尚未選擇要匯入的本機影片。");
  await fetchCsrfToken({ force: true });
  const form = new FormData();
  form.append("video", asset.file, asset.filename || asset.file.name || "video.mp4");
  form.append("asset_key", assetKey);
  const res = await apiFetch(API + "/comfyui/import-uploaded-video", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "X-CSRF-Token": getCsrfToken() || ""
    },
    body: form
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `本機影片匯入失敗（HTTP ${res.status}）`);
  const media = json.media || {};
  setComfyuiInputAssetFromRef(assetKey, media.media_ref || { filename: media.filename || asset.filename || "" }, asset.previewUrl || "", media.filename || asset.filename || "", {
    cloudFileId: media.cloud_file_id || "",
  });
  return media;
}

async function importComfyuiUploadedMedia(assetKey = "source") {
  const asset = comfyuiAssetState(assetKey);
  const meta = COMFYUI_INPUT_ASSET_META[assetKey] || {};
  if (meta.mediaType === "video" || /^video\//i.test(asset?.file?.type || "")) {
    return importComfyuiUploadedVideo(assetKey);
  }
  return importComfyuiUploadedImage(assetKey);
}

async function importComfyuiHistoryImage(imageRef) {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/comfyui/import-history-image", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || ""
    },
    body: JSON.stringify({ image_ref: imageRef })
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `歷史圖片匯入失敗（HTTP ${res.status}）`);
  return json.image || {};
}

async function applyComfyuiImagePickerSelection(source, index) {
  const targetKey = comfyuiImagePickerState.targetKey || "source";
  if (source === "history") {
    const item = (comfyuiImagePickerState.history || [])[index];
    if (!item?.image_ref) throw new Error("歷史圖片引用不存在。");
    const image = await importComfyuiHistoryImage(item.image_ref);
    setComfyuiInputAssetFromRef(targetKey, image.image_ref, image.data_url || "", image.filename || item.filename || "", {
      cloudFileId: image.cloud_file_id || "",
    });
    closeComfyuiImagePicker();
    setComfyuiMessage(`已把歷史圖片匯入並套用為${COMFYUI_INPUT_ASSET_META[targetKey]?.title || "圖片"}。`, true);
    return;
  }
  if (source === "cloud_drive") {
    const item = (comfyuiImagePickerState.cloudDrive || [])[index];
    if (!item?.file_id) throw new Error("雲端硬碟圖片不存在。");
    const image = await importComfyuiDriveImage(item.file_id);
    setComfyuiInputAssetFromRef(targetKey, image.image_ref, image.data_url || "", image.filename || item.filename || "", {
      cloudFileId: image.cloud_file_id || item.file_id || "",
    });
    closeComfyuiImagePicker();
    setComfyuiMessage(`已匯入雲端硬碟圖片作為${COMFYUI_INPUT_ASSET_META[targetKey]?.title || "圖片"}。`, true);
  }
}

async function openComfyuiImagePicker(targetKey = "source") {
  if (!COMFYUI_INPUT_ASSET_META[targetKey]) targetKey = "source";
  const { modal, list, status } = comfyuiImagePickerElements();
  if (!modal) return;
  comfyuiImagePickerState.open = true;
  comfyuiImagePickerState.targetKey = targetKey;
  modal.hidden = false;
  if (list) list.innerHTML = '<div class="drive-empty">正在讀取歷史產圖與雲端硬碟圖片...</div>';
  if (status) status.textContent = "正在讀取圖片清單...";
  try {
    await loadComfyuiImagePickerCandidates();
    renderComfyuiImagePickerList();
  } catch (err) {
    if (status) status.textContent = err.message || "圖片清單讀取失敗";
    if (list) list.innerHTML = `<div class="drive-empty">${sanitize(err.message || "圖片清單讀取失敗")}</div>`;
  }
}

function fillComfyuiSelect(id, values, fallback) {
  const select = $(id);
  if (!select) return;
  const options = Array.isArray(values) && values.length ? values : [fallback].filter(Boolean);
  select.innerHTML = options.map((value) => `<option value="${sanitize(value)}">${sanitize(value)}</option>`).join("");
}

function normalizeComfyuiModelOptionValues(values = []) {
  const seen = new Set();
  const options = [];
  (Array.isArray(values) ? values : []).forEach((value) => {
    const text = String(value || "").trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    options.push(text);
  });
  return options;
}

function fillComfyuiVaeSelect(values = []) {
  comfyuiAvailableVaes = normalizeComfyuiModelOptionValues(values);
  const select = $("comfyui-vae-select");
  if (!select) return;
  const options = [`<option value="${COMFYUI_VAE_BUILTIN}">使用各自大模型內建 VAE</option>`]
    .concat(comfyuiAvailableVaes.map((value) => `<option value="${sanitize(value)}">${sanitize(value)}</option>`));
  select.innerHTML = options.join("");
}

function updateComfyuiResultButtons(hasImage) {
  const save = $("comfyui-save-btn");
  const favorite = $("comfyui-favorite-btn");
  const discard = $("comfyui-discard-btn");
  const share = $("comfyui-share-btn");
  if (save) save.disabled = !hasImage;
  if (favorite) favorite.disabled = !hasImage;
  if (discard) discard.disabled = !hasImage;
  if (share) share.disabled = !hasImage;
}

function formatComfyuiDuration(seconds) {
  const safe = Math.max(0, Math.floor(Number(seconds) || 0));
  const mins = Math.floor(safe / 60);
  const secs = safe % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function comfyuiResourcePercent(value) {
  const percent = Number(value);
  if (!Number.isFinite(percent) || percent < 0) return null;
  return Math.max(0, Math.min(100, percent));
}

function comfyuiResourceDetailBytes(used, total) {
  const usedBytes = Number(used || 0);
  const totalBytes = Number(total || 0);
  if (totalBytes > 0 && typeof formatDriveBytes === "function") {
    return `${formatDriveBytes(usedBytes)} / ${formatDriveBytes(totalBytes)}`;
  }
  return "";
}

function comfyuiResourceMetricMarkup({ label, percent, detail = "", available = true }) {
  const safePercent = comfyuiResourcePercent(percent);
  const unavailable = available === false || safePercent === null;
  const display = unavailable ? "--" : `${Math.round(safePercent)}%`;
  const level = unavailable ? "muted" : (safePercent >= 90 ? "danger" : (safePercent >= 75 ? "warn" : "ok"));
  return `
    <div class="comfyui-resource-metric ${level}">
      <span>${sanitize(label || "-")}</span>
      <strong>${sanitize(display)}</strong>
      <small>${sanitize(detail || (unavailable ? "未偵測" : "使用中"))}</small>
    </div>
  `;
}

function renderComfyuiResourceDashboard(resource = {}, { error = "" } = {}) {
  const host = $("comfyui-resource-dashboard");
  if (!host) return;
  if (error) {
    host.innerHTML = [
      comfyuiResourceMetricMarkup({ label: "RAM", percent: null, available: false, detail: error }),
      comfyuiResourceMetricMarkup({ label: "GPU Load", percent: null, available: false, detail: "等待資源資料" }),
      comfyuiResourceMetricMarkup({ label: "VRAM", percent: null, available: false, detail: "等待資源資料" }),
      comfyuiResourceMetricMarkup({ label: "GPU Temp", percent: null, available: false, detail: "等待資源資料" }),
    ].join("");
    return;
  }
  const ram = resource.ram || {};
  const gpu = resource.gpu || {};
  const vram = resource.vram || {};
  const gpus = Array.isArray(gpu.gpus) ? gpu.gpus : [];
  const tempValues = gpus
    .map((item) => Number(item.temperature_c))
    .filter((value) => Number.isFinite(value) && value >= 0);
  const maxTemp = tempValues.length ? Math.max(...tempValues) : null;
  const gpuNames = gpus.length ? gpus.map((item) => item.name || `GPU ${item.index ?? ""}`).join(" / ") : "";
  host.innerHTML = [
    comfyuiResourceMetricMarkup({
      label: "RAM",
      percent: ram.percent,
      detail: comfyuiResourceDetailBytes(ram.used_bytes, ram.total_bytes) || "系統記憶體",
    }),
    comfyuiResourceMetricMarkup({
      label: "GPU Load",
      percent: gpu.percent,
      available: gpu.available,
      detail: gpu.available ? (gpuNames || "GPU 使用率") : (gpu.error || "未偵測到 GPU"),
    }),
    comfyuiResourceMetricMarkup({
      label: "VRAM",
      percent: vram.percent,
      available: vram.available,
      detail: comfyuiResourceDetailBytes(vram.used_bytes, vram.total_bytes) || "顯示記憶體",
    }),
    comfyuiResourceMetricMarkup({
      label: "GPU Temp",
      percent: maxTemp === null ? null : Math.min(100, maxTemp),
      available: maxTemp !== null,
      detail: maxTemp === null ? "未偵測溫度" : `${Math.round(maxTemp)} C`,
    }),
  ].join("");
}

async function refreshComfyuiResourceDashboard() {
  if (comfyuiResourceDashboardInFlight || !$("comfyui-resource-dashboard")) return;
  if (!currentUser) {
    renderComfyuiResourceDashboard({}, { error: "尚未登入" });
    return;
  }
  comfyuiResourceDashboardInFlight = true;
  try {
    const csrf = await fetchCsrfToken();
    const res = await apiFetch(API + "/comfyui/resources", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || "資源 dashboard 讀取失敗");
    renderComfyuiResourceDashboard(json.resource_usage || {});
  } catch (err) {
    renderComfyuiResourceDashboard({}, { error: err?.message || "資源 dashboard 讀取失敗" });
  } finally {
    comfyuiResourceDashboardInFlight = false;
  }
}

function startComfyuiResourceDashboardPolling() {
  if (!$("comfyui-resource-dashboard") || !currentUser) return;
  if (!comfyuiResourceDashboardTimer) {
    comfyuiResourceDashboardTimer = setInterval(refreshComfyuiResourceDashboard, 5000);
  }
  refreshComfyuiResourceDashboard();
}

function stopComfyuiResourceDashboardPolling() {
  if (comfyuiResourceDashboardTimer) {
    clearInterval(comfyuiResourceDashboardTimer);
    comfyuiResourceDashboardTimer = null;
  }
}

function setComfyuiProgress({ visible = true, running = false, percent = 0, label = "", detail = "", pythonLogTail = null, backendKind = "", showPythonLog = false, pythonLogPlaceholder = "" } = {}) {
  const panel = $("comfyui-progress-panel");
  const bar = $("comfyui-progress-bar");
  const labelEl = $("comfyui-progress-label");
  const percentEl = $("comfyui-progress-percent");
  const detailEl = $("comfyui-progress-detail");
  const pythonLogEl = $("comfyui-progress-python-log");
  if (!panel) return;
  panel.style.display = visible ? "" : "none";
  panel.classList.toggle("running", !!running);
  if (!visible) {
    comfyuiProgressPythonLogTail = [];
    comfyuiProgressBackendKind = "";
    if (pythonLogEl) {
      pythonLogEl.style.display = "none";
      pythonLogEl.textContent = "";
    }
  } else if (Array.isArray(pythonLogTail)) {
    comfyuiProgressPythonLogTail = pythonLogTail.filter(Boolean).map(String).slice(-80);
  }
  if (backendKind) comfyuiProgressBackendKind = String(backendKind).toLowerCase();
  const safePercent = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  if (bar) bar.style.width = `${safePercent}%`;
  if (labelEl) labelEl.textContent = label || (isComfyuiDiffusersMode() ? "等待 Diffusers" : "等待 ComfyUI");
  if (percentEl) percentEl.textContent = `${safePercent}%`;
  if (detailEl) detailEl.textContent = detail || "";
  if (pythonLogEl && visible) {
    const shouldShowPythonLog = !!showPythonLog || comfyuiProgressPythonLogTail.length > 0;
    pythonLogEl.style.display = shouldShowPythonLog ? "" : "none";
    const defaultPythonLogPlaceholder = (comfyuiProgressBackendKind === "diffusers" || isComfyuiDiffusersMode())
      ? "Diffusers Python log 尚未輸出；下載、載入或推論訊息會顯示在這裡。"
      : "ComfyUI 後端沒有提供 Python log；若遠端 API 逾時，請檢查遠端 ComfyUI 的 /queue、/system_stats，或重啟遠端 ComfyUI。";
    pythonLogEl.textContent = comfyuiProgressPythonLogTail.length
      ? comfyuiProgressPythonLogTail.join("\n")
      : (pythonLogPlaceholder || defaultPythonLogPlaceholder);
    pythonLogEl.scrollTop = pythonLogEl.scrollHeight;
  }
}

function stopComfyuiProgress({ complete = false, error = "", label = "" } = {}) {
  if (comfyuiProgressTimer) {
    clearInterval(comfyuiProgressTimer);
    comfyuiProgressTimer = null;
  }
  if (complete) {
    const label = comfyuiOutputKindsLabel();
    setComfyuiProgress({
      visible: true,
      running: false,
      percent: 100,
      label: `${label}已完成`,
      detail: `ComfyUI 已回傳${label}結果`,
      showPythonLog: comfyuiProgressBackendKind === "diffusers"
    });
  } else if (error) {
    const showDiffusersLog = comfyuiProgressBackendKind === "diffusers" || isComfyuiDiffusersMode();
    setComfyuiProgress({
      visible: true,
      running: false,
      percent: 100,
      label: label || "產圖失敗",
      detail: error,
      backendKind: showDiffusersLog ? "diffusers" : (comfyuiConnectionMode || "comfyui"),
      showPythonLog: true,
      pythonLogPlaceholder: showDiffusersLog
        ? "Diffusers Python log 尚未輸出；請看上方失敗原因。"
        : "遠端/本地 ComfyUI 沒有回傳即時 logs；若顯示連線逾時，請直接檢查遠端 ComfyUI API、queue 或重啟 ComfyUI。"
    });
  } else {
    setComfyuiProgress({ visible: false });
  }
}

function resetComfyuiIdleUi() {
  updateComfyuiPreviewCardForOutputKinds();
  comfyuiActiveJobId = null;
  if (!comfyuiGenerateAbortController) {
    setComfyuiBusy(false);
  }
  stopComfyuiProgress();
  const preview = $("comfyui-preview");
  if (preview && !comfyuiCurrentImage && !comfyuiGeneratedImages.length && !comfyuiGeneratedMedia.length) {
    preview.innerHTML = `<div class="drive-empty">${sanitize(comfyuiPreviewEmptyText())}</div>`;
  }
}

function startComfyuiProgress(timeoutSeconds = COMFYUI_GENERATION_TIMEOUT_SECONDS) {
  comfyuiProgressStartedAt = Date.now();
  const hasLimit = Number(timeoutSeconds) > 0;
  setComfyuiProgress({
    visible: true,
    running: true,
    percent: 0,
    label: "已送出產圖請求",
    detail: hasLimit ? `已等待 00:00 / 上限 ${formatComfyuiDuration(timeoutSeconds)}` : "已等待 00:00 / 不設最長等待上限",
    pythonLogTail: [],
    backendKind: isComfyuiDiffusersMode() ? "diffusers" : "",
    showPythonLog: isComfyuiDiffusersMode(),
    pythonLogPlaceholder: "等待 Diffusers Python log..."
  });
}

function comfyuiBuildJobFailureMessage(job = {}) {
  const progress = job.progress || {};
  const isDiffusersProgress = isComfyuiDiffusersMode() || String(progress.backend_kind || "").toLowerCase() === "diffusers";
  const parts = [
    job.error,
    progress.error_message,
    progress.error,
    progress.detail,
    job.msg,
  ]
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .filter((value, index, list) => list.indexOf(value) === index);
  if (!parts.length) {
    return isDiffusersProgress
      ? "Diffusers 產圖失敗，後端未回傳詳細原因；請查看下方 Python logs。"
      : "ComfyUI 產圖失敗，後端未回傳詳細原因。";
  }
  return parts.join("；");
}

function applyComfyuiJobProgress(progress = {}, timeoutSeconds = COMFYUI_GENERATION_TIMEOUT_SECONDS) {
  const elapsed = Math.max(0, Math.floor((Date.now() - comfyuiProgressStartedAt) / 1000));
  const percent = Math.max(0, Math.min(100, Math.round(Number(progress.percent) || 0)));
  const phase = String(progress.phase || "").toLowerCase();
  const isDiffusersProgress = isComfyuiDiffusersMode() || String(progress.backend_kind || "").toLowerCase() === "diffusers";
  let label = isDiffusersProgress ? "等待 Diffusers" : "等待 ComfyUI";
  if (phase === "queued") label = "排隊中";
  else if (phase === "downloading") label = isDiffusersProgress ? "下載 Diffusers model" : "下載 Hugging Face 模型";
  else if (phase === "loading") label = isDiffusersProgress ? "載入 Diffusers 模型" : "載入模型";
  else if (phase === "running") label = isDiffusersProgress ? "Diffusers 推論中" : "ComfyUI 執行中";
  else if (phase === "backend_unresponsive") label = isDiffusersProgress ? "Diffusers 暫無新進度" : "ComfyUI 後端無回應";
  else if (phase === "completed") label = `${comfyuiOutputKindsLabel()}已完成`;
  else if (phase === "error") label = "產圖失敗";
  const queueText = progress.queue_remaining !== null && progress.queue_remaining !== undefined
    ? `，佇列剩餘 ${progress.queue_remaining}`
    : "";
  const nodeText = progress.current_node ? `，節點 ${progress.current_node}` : "";
  const writtenBytes = Number(progress.bytes_written || progress.downloaded_bytes || 0);
  const totalBytes = Number(progress.total_bytes || 0);
  let baseDetail = progress.detail || "等待進度資料";
  if (phase === "downloading" && isDiffusersProgress && percent > 0 && !/%/.test(baseDetail)) {
    baseDetail = `${baseDetail}（${percent}%）`;
  }
  const writtenByteText = writtenBytes > 0 && typeof formatDriveBytes === "function" ? formatDriveBytes(writtenBytes) : "";
  const totalByteText = totalBytes > 0 && typeof formatDriveBytes === "function" ? formatDriveBytes(totalBytes) : "";
  const speedBytes = Number(progress.speed_bytes_per_sec || progress.download_speed_bytes_per_sec || 0);
  const speedText = speedBytes > 0 && typeof formatDriveBytes === "function" ? `，速度 ${formatDriveBytes(speedBytes)}/s` : "";
  const fileText = progress.current_file ? `，檔案 ${progress.current_file}` : "";
  const stepText = progress.step ? `，步驟 ${progress.step}` : "";
  const byteText = writtenByteText && !baseDetail.includes(writtenByteText)
    ? `，下載 ${writtenByteText}${totalByteText ? ` / ${totalByteText}` : ""}`
    : "";
  const hasLimit = Number(timeoutSeconds) > 0;
  const waitText = hasLimit
    ? `；已等待 ${formatComfyuiDuration(elapsed)} / 上限 ${formatComfyuiDuration(timeoutSeconds)}`
    : `；已等待 ${formatComfyuiDuration(elapsed)} / 不設最長等待上限`;
  const detail = `${baseDetail}${queueText}${nodeText}${stepText}${fileText}${byteText}${speedText}${waitText}`;
  setComfyuiProgress({
    visible: true,
    running: phase !== "completed" && phase !== "error",
    percent,
    label,
    detail,
    pythonLogTail: Array.isArray(progress.python_log_tail) ? progress.python_log_tail : null,
    backendKind: isDiffusersProgress ? "diffusers" : (comfyuiConnectionMode || "comfyui"),
    showPythonLog: isDiffusersProgress || phase === "error" || phase === "backend_unresponsive",
    pythonLogPlaceholder: isDiffusersProgress
      ? (phase === "error" ? "Diffusers Python log 尚未輸出；請看上方失敗原因。" : "等待 Diffusers Python log...")
      : "遠端/本地 ComfyUI 未回傳即時 logs；若長時間無回應，請檢查遠端 /queue、/system_stats 或重啟 ComfyUI。"
  });
}

function isComfyuiJobQueued(job = {}) {
  const phase = String(job?.progress?.phase || "").toLowerCase();
  return phase === "queued" || String(job?.status || "").toLowerCase() === "queued";
}

function extendComfyuiDeadlineForQueue(deadline, startedAt) {
  const now = Date.now();
  const refreshWindowMs = Math.max(5000, Math.min(60000, COMFYUI_QUEUE_TIMEOUT_EXTENSION_SECONDS * 100));
  if (deadline - now > refreshWindowMs) return deadline;
  const maxDeadline = startedAt + COMFYUI_QUEUE_MAX_TIMEOUT_SECONDS * 1000 + 15000;
  const extended = Math.min(maxDeadline, now + COMFYUI_QUEUE_TIMEOUT_EXTENSION_SECONDS * 1000 + 15000);
  return Math.max(deadline, extended);
}

function createComfyuiForegroundTimeoutError(jobId) {
  const suffix = jobId ? `（job id：${jobId}）` : "";
  const err = new Error(`ComfyUI 前台等待逾時；後端工作可能仍在執行${suffix}。稍後可從歷史紀錄查看結果。`);
  err.comfyuiForegroundTimeout = true;
  err.jobId = jobId || "";
  return err;
}

function isComfyuiForegroundTimeoutError(err) {
  if (err?.comfyuiForegroundTimeout) return true;
  return /前台等待逾時|進度查詢逾時/.test(String(err?.message || ""));
}

function comfyuiForegroundTimeoutMessage(err) {
  const jobId = err?.jobId || comfyuiActiveJobId || "";
  const suffix = jobId ? `（job id：${jobId}）` : "";
  return `已停止前台等待；後端工作可能仍在執行${suffix}。完成後請到歷史紀錄查看或稍後重新整理 Workflow。`;
}

async function pollComfyuiJobUntilDone(jobId, controller, timeoutSeconds, options = {}) {
  comfyuiActiveJobId = jobId;
  const startedAt = Date.now();
  const unlimited = Number(timeoutSeconds) <= 0;
  let deadline = unlimited ? Number.POSITIVE_INFINITY : startedAt + timeoutSeconds * 1000 + 15000;
  let displayTimeoutSeconds = unlimited ? 0 : timeoutSeconds;
  while (unlimited || Date.now() < deadline) {
    if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
    const res = await apiFetch(API + `/comfyui/jobs/${encodeURIComponent(jobId)}`, {
      credentials: "same-origin",
      signal: controller.signal,
      headers: { "X-CSRF-Token": getCsrfToken() || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 工作狀態讀取失敗（HTTP ${res.status}）`);
    const job = json.job || {};
    if (!unlimited && isComfyuiJobQueued(job)) {
      deadline = extendComfyuiDeadlineForQueue(deadline, startedAt);
    }
    if (!unlimited) {
      displayTimeoutSeconds = Math.max(
        displayTimeoutSeconds,
        Number(job.progress?.timeout_seconds) || 0,
        Math.max(timeoutSeconds, Math.floor((deadline - startedAt - 15000) / 1000))
      );
    }
    applyComfyuiJobProgress(job.progress || {}, displayTimeoutSeconds);
    if (job.status !== "completed" && job.result && typeof options.onPartialResult === "function") {
      try {
        await options.onPartialResult(job.result, job);
      } catch (err) {
        if (typeof options.onPartialError === "function") options.onPartialError(err, job);
      }
    }
    if (job.status === "completed" && job.result) return job.result;
    if (job.status === "error") throw new Error(comfyuiBuildJobFailureMessage(job));
    await new Promise((resolve) => setTimeout(resolve, comfyuiJobPollMs()));
  }
  throw createComfyuiForegroundTimeoutError(jobId);
}

function selectedComfyuiAlbumId() {
  const value = $("comfyui-album-select")?.value || "";
  return value || null;
}

function comfyuiDraftStorageKey() {
  return comfyuiUserStorageKey("comfyui:draft");
}

function readComfyuiDraft() {
  try {
    return JSON.parse(localStorage.getItem(comfyuiDraftStorageKey()) || "{}") || {};
  } catch (err) {
    return {};
  }
}

function writeComfyuiDraft() {
  const draft = {};
  COMFYUI_DRAFT_FIELD_IDS.forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (id === "comfyui-diffusers-model-repo") {
      const repo = normalizeComfyuiHuggingFaceRepoInput(el.value || "");
      draft[id] = repo && isComfyuiHuggingFaceRepoLike(repo) ? repo : "";
    } else {
      draft[id] = el.value;
    }
  });
  draft.selected_loras = comfyuiSelectedLoras;
  try {
    localStorage.setItem(comfyuiDraftStorageKey(), JSON.stringify(draft));
  } catch (err) {
    // Storage may be unavailable in private mode; keep the live DOM values.
  }
}

function setComfyuiFieldValue(id, value, { preserveMissingOption = false } = {}) {
  const el = $(id);
  if (!el || value === undefined || value === null) return;
  if (id === "comfyui-diffusers-model-repo") {
    const repo = normalizeComfyuiHuggingFaceRepoInput(value);
    if (!repo || !isComfyuiHuggingFaceRepoLike(repo)) return;
    value = repo;
  }
  if (el.tagName === "SELECT") {
    const exists = Array.from(el.options || []).some((option) => option.value === String(value));
    if (!exists && String(value)) {
      if (!preserveMissingOption) return;
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = `歷史：${String(value)}`;
      option.dataset.historyValue = "1";
      el.appendChild(option);
    }
  }
  el.value = String(value);
}

function comfyuiSelectOptionValues(id, { includeHistoryOptions = false } = {}) {
  const select = $(id);
  if (!select) return [];
  return Array.from(select.options || [])
    .filter((option) => includeHistoryOptions || option.dataset.historyValue !== "1")
    .map((option) => String(option.value || "").trim())
    .filter(Boolean);
}

function comfyuiComparableOptionKey(value = "") {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/＋/g, "+")
    .replace(/\+\+/g, "pp")
    .replace(/\+/g, "p")
    .replace(/[_-]+/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function comfyuiUniqueValues(values = []) {
  const seen = new Set();
  const out = [];
  values.forEach((value) => {
    const text = String(value || "").trim();
    if (!text) return;
    const key = comfyuiComparableOptionKey(text);
    if (seen.has(key)) return;
    seen.add(key);
    out.push(text);
  });
  return out;
}

function comfyuiPickAvailableSelectValue(id, candidates = [], fallbacks = []) {
  const values = comfyuiSelectOptionValues(id);
  if (!values.length) return "";
  const exact = new Map(values.map((value) => [String(value), value]));
  const keyed = new Map();
  values.forEach((value) => {
    const key = comfyuiComparableOptionKey(value);
    if (key && !keyed.has(key)) keyed.set(key, value);
  });
  for (const candidate of comfyuiUniqueValues([...(candidates || []), ...(fallbacks || [])])) {
    if (exact.has(candidate)) return exact.get(candidate);
    const key = comfyuiComparableOptionKey(candidate);
    if (keyed.has(key)) return keyed.get(key);
  }
  return "";
}

function comfyuiSamplerCandidateValues(value = "") {
  const raw = String(value || "").trim();
  const key = comfyuiComparableOptionKey(raw);
  const aliases = {
    "euler a": ["euler_ancestral", "euler a"],
    "euler ancestral": ["euler_ancestral"],
    "euler": ["euler"],
    "lms": ["lms"],
    "heun": ["heun"],
    "dpm 2": ["dpm_2"],
    "dpm2": ["dpm_2"],
    "dpm 2 a": ["dpm_2_ancestral"],
    "dpm2 a": ["dpm_2_ancestral"],
    "dpm fast": ["dpm_fast"],
    "dpm adaptive": ["dpm_adaptive"],
    "dpmpp 2s a": ["dpmpp_2s_ancestral"],
    "dpmpp 2m": ["dpmpp_2m"],
    "dpmpp 2m sde": ["dpmpp_2m_sde"],
    "dpmpp sde": ["dpmpp_sde"],
    "dpmpp 3m sde": ["dpmpp_3m_sde"],
    "ddim": ["ddim"],
    "uni pc": ["uni_pc"],
    "unipc": ["uni_pc"],
    "lcm": ["lcm"],
    "ipndm": ["ipndm"],
    "deis": ["deis"],
    "ddpm": ["ddpm"],
  };
  const schedulerWords = /\b(?:karras|exponential|normal|simple|beta|sgm uniform|ddim uniform|linear quadratic|kl optimal)\b/g;
  const strippedKey = key.replace(schedulerWords, " ").replace(/\s+/g, " ").trim();
  return comfyuiUniqueValues([
    raw,
    ...(aliases[key] || []),
    ...(strippedKey && strippedKey !== key ? aliases[strippedKey] || [strippedKey] : []),
  ]);
}

function comfyuiSchedulerCandidateValues(scheduler = "", sampler = "") {
  const rawScheduler = String(scheduler || "").trim();
  const rawSampler = String(sampler || "").trim();
  const keys = [
    comfyuiComparableOptionKey(rawScheduler),
    comfyuiComparableOptionKey(rawSampler),
    comfyuiComparableOptionKey(`${rawSampler} ${rawScheduler}`),
  ].filter(Boolean);
  const candidates = [rawScheduler];
  const hasToken = (pattern) => keys.some((key) => pattern.test(key));
  if (hasToken(/\bsgm uniform\b/)) candidates.push("sgm_uniform");
  if (hasToken(/\bddim uniform\b/)) candidates.push("ddim_uniform");
  if (hasToken(/\blinear quadratic\b/)) candidates.push("linear_quadratic");
  if (hasToken(/\bkl optimal\b/)) candidates.push("kl_optimal");
  if (hasToken(/\bkarras\b/)) candidates.push("karras");
  if (hasToken(/\bexponential\b/)) candidates.push("exponential");
  if (hasToken(/\bbeta\b/)) candidates.push("beta");
  if (hasToken(/\bsimple\b/)) candidates.push("simple");
  if (hasToken(/\bnormal\b/)) candidates.push("normal");
  return comfyuiUniqueValues(candidates);
}

function comfyuiResolvedSamplerValue(value = "") {
  return comfyuiPickAvailableSelectValue("comfyui-sampler", comfyuiSamplerCandidateValues(value), ["euler"]);
}

function comfyuiResolvedSchedulerValue(value = "", sampler = "") {
  return comfyuiPickAvailableSelectValue("comfyui-scheduler", comfyuiSchedulerCandidateValues(value, sampler), ["normal", "simple", "karras"]);
}

function setComfyuiSamplingFieldsFromValues(samplerName = "", scheduler = "") {
  const rawSampler = String(samplerName || "").trim();
  const resolvedSampler = comfyuiResolvedSamplerValue(rawSampler || "euler")
    || comfyuiPickAvailableSelectValue("comfyui-sampler", [], ["euler"]);
  if (resolvedSampler) setComfyuiFieldValue("comfyui-sampler", resolvedSampler);
  const resolvedScheduler = comfyuiResolvedSchedulerValue(scheduler || "normal", rawSampler || resolvedSampler)
    || comfyuiPickAvailableSelectValue("comfyui-scheduler", [], ["normal", "simple", "karras"]);
  if (resolvedScheduler) setComfyuiFieldValue("comfyui-scheduler", resolvedScheduler);
  const samplerFallback = comfyuiSamplerCandidateValues(rawSampler || "euler").find((value) => value !== rawSampler) || rawSampler || "euler";
  const schedulerFallback = comfyuiSchedulerCandidateValues(scheduler || "normal", rawSampler || samplerFallback).find(Boolean) || "normal";
  return {
    sampler_name: resolvedSampler || samplerFallback,
    scheduler: resolvedScheduler || schedulerFallback,
  };
}

function comfyuiCurrentSamplingFieldsForPayload() {
  return setComfyuiSamplingFieldsFromValues($("comfyui-sampler")?.value || "euler", $("comfyui-scheduler")?.value || "normal");
}

function normalizeComfyuiLoraName(name) {
  return String(name || "").trim().replace(/（提醒：[^）]*）$/u, "").trim();
}

function restoreComfyuiDraft({ includeDynamicSelects = true } = {}) {
  const draft = readComfyuiDraft();
  if (Array.isArray(draft.selected_loras)) {
    comfyuiSelectedLoras = draft.selected_loras
      .filter((item) => item && typeof item === "object" && normalizeComfyuiLoraName(item.name))
      .slice(0, COMFYUI_MAX_LORAS)
      .map((item) => ({
        name: normalizeComfyuiLoraName(item.name),
        strength_model: Number.isFinite(Number(item.strength_model)) ? Number(item.strength_model) : 1,
        strength_clip: Number.isFinite(Number(item.strength_clip)) ? Number(item.strength_clip) : 1,
        template_node_id: item.template_node_id ? String(item.template_node_id) : "",
      }));
    renderComfyuiSelectedLoras();
  }
  COMFYUI_DRAFT_FIELD_IDS.forEach((id) => {
    if (!includeDynamicSelects && COMFYUI_DYNAMIC_SELECT_IDS.includes(id)) {
      return;
    }
    setComfyuiFieldValue(id, draft[id]);
  });
  if (includeDynamicSelects) {
    fillComfyuiGgufVariants($("comfyui-diffusers-gguf-profile")?.value || "");
    setComfyuiFieldValue("comfyui-diffusers-gguf-variant", draft["comfyui-diffusers-gguf-variant"]);
    updateComfyuiGgufProfileSelection({ syncRepo: false });
  }
  updateComfyuiModeVisibility();
}

function updateComfyuiRootPanelVisibility(modeOverride = null) {
  const panel = $("comfyui-root-model-panel");
  const accessNote = $("comfyui-root-model-access-note");
  const settingsPanel = $("comfyui-root-settings-panel");
  const details = $("comfyui-root-model-details");
  const hint = $("comfyui-root-model-mode-hint");
  const modelsTab = document.querySelector('[data-comfyui-view="models"]');
  const settingsTab = document.querySelector('[data-comfyui-view="settings"]');
  const mode = comfyuiEffectiveConnectionMode(modeOverride);
  const localReady = mode === "local";
  const showLocalModels = canManageComfyuiLocalModels(mode);
  const canManageSettings = currentUser === "root";
  updateComfyuiModeNote(mode);
  if (accessNote) accessNote.style.display = showLocalModels ? "none" : "";
  if (panel) panel.style.display = showLocalModels ? "" : "none";
  if (modelsTab) modelsTab.hidden = false;
  if (settingsPanel) settingsPanel.style.display = canManageSettings ? "" : "none";
  if (settingsTab) settingsTab.hidden = !canManageSettings;
  if (!canManageSettings && document.querySelector('[data-comfyui-view-panel="settings"]')?.classList.contains("active")) {
    setComfyuiView("generate");
  }
  if (details) details.open = !!showLocalModels;
  if (!panel) return;
  panel.querySelectorAll("input, select, button").forEach((el) => {
    el.disabled = !showLocalModels;
  });
  panel.dataset.mode = mode === "diffusers" ? "diffusers" : (localReady ? "local" : "remote");
  if (hint) {
    hint.textContent = localReady
      ? "目前是本地模式，可在這裡用 Civitai 下載或直接上傳模型檔。"
      : (mode === "diffusers"
        ? "目前是 Diffusers 模式；Civitai 匯入區仍可使用，但下載會寫入本站設定的 ComfyUI models 目錄，不會寫入 Hugging Face cache。"
        : "目前是雲端 / 遠端模式；Civitai 匯入區仍可使用，但下載會寫入本站設定的 ComfyUI models 目錄。若遠端主機不是同一路徑，請在遠端自行同步或掛載。");
  }
  updateComfyuiModelSourceMode();
}

function openComfyuiCivitaiPanel() {
  setComfyuiView("models");
  if (!canManageComfyuiLocalModels()) return;
  const details = $("comfyui-root-model-details");
  if (details) details.open = true;
  const sourceMode = $("comfyui-model-source-mode");
  if (sourceMode) sourceMode.value = "civitai";
  updateComfyuiModelSourceMode();
  $("comfyui-civitai-url")?.focus();
}

function renderComfyuiSelectedLoras() {
  const box = $("comfyui-selected-loras");
  const count = $("comfyui-lora-count");
  if (!box) return;
  comfyuiSelectedLoras = comfyuiSelectedLoras
    .filter((item) => item && typeof item === "object")
    .map((item) => ({ ...item, name: normalizeComfyuiLoraName(item.name) }))
    .filter((item) => item.name)
    .slice(0, COMFYUI_MAX_LORAS);
  if (count) count.textContent = `${comfyuiSelectedLoras.length} / ${COMFYUI_MAX_LORAS}`;
  if (!comfyuiSelectedLoras.length) {
    box.innerHTML = '<span class="drive-card-sub">尚未選擇 LoRA</span>';
    return;
  }
  box.innerHTML = comfyuiSelectedLoras.map((item, index) => {
    const hint = comfyuiLoraCompatibilityHint(item?.name);
    return `
      <div class="comfyui-lora-chip" title="${sanitize(item.name)}">
        <div class="comfyui-lora-chip-head">
          <span>${sanitize(item.name)}</span>
          <button type="button" data-comfyui-remove-lora="${index}" aria-label="移除 ${sanitize(item.name)}">×</button>
        </div>
        <div class="comfyui-lora-weight-grid">
          <label>
            <span>Model</span>
            <input type="number" min="-2" max="2" step="0.05" data-comfyui-lora-strength-model="${index}" value="${sanitize(String(item.strength_model ?? 1))}" />
          </label>
          <label>
            <span>CLIP</span>
            <input type="number" min="-2" max="2" step="0.05" data-comfyui-lora-strength-clip="${index}" value="${sanitize(String(item.strength_clip ?? 1))}" />
          </label>
        </div>
        ${hint ? `<div class="comfyui-lora-compat-hint">提醒：${sanitize(hint)}</div>` : ""}
      </div>
    `;
  }).join("");
  box.querySelectorAll("[data-comfyui-remove-lora]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.getAttribute("data-comfyui-remove-lora"));
      removeComfyuiSelectedLoraByIndex(index);
    });
  });
  box.querySelectorAll("[data-comfyui-lora-strength-model]").forEach((input) => {
    input.addEventListener("input", () => updateComfyuiSelectedLoraStrength(input, "strength_model"));
    input.addEventListener("change", () => updateComfyuiSelectedLoraStrength(input, "strength_model"));
  });
  box.querySelectorAll("[data-comfyui-lora-strength-clip]").forEach((input) => {
    input.addEventListener("input", () => updateComfyuiSelectedLoraStrength(input, "strength_clip"));
    input.addEventListener("change", () => updateComfyuiSelectedLoraStrength(input, "strength_clip"));
  });
}

function updateComfyuiSelectedLoraStrength(input, field) {
  const datasetKey = field === "strength_clip" ? "comfyuiLoraStrengthClip" : "comfyuiLoraStrengthModel";
  const index = Number(input?.dataset?.[datasetKey]);
  if (!Number.isInteger(index) || !comfyuiSelectedLoras[index]) return;
  const value = Number(input.value || 1);
  const normalized = Math.max(-2, Math.min(2, Number.isFinite(value) ? value : 1));
  comfyuiSelectedLoras[index][field] = Math.round(normalized * 100) / 100;
  input.value = String(comfyuiSelectedLoras[index][field]);
  writeComfyuiDraft();
}

function fillComfyuiLoraSelect(values = []) {
  const seen = new Set();
  comfyuiAvailableLoras = (Array.isArray(values) ? values : [])
    .map(normalizeComfyuiLoraName)
    .filter((value) => {
      if (!value || seen.has(value)) return false;
      seen.add(value);
      return true;
    });
  const select = $("comfyui-lora-select");
  if (!select) return;
  const options = ['<option value="">不使用 LoRA（可略過）</option>']
    .concat(comfyuiAvailableLoras.map((value) => {
      const hint = comfyuiLoraCompatibilityHint(value);
      const label = hint ? `${value}（提醒：${hint}）` : value;
      return `<option value="${sanitize(value)}">${sanitize(label)}</option>`;
    }));
  select.innerHTML = options.join("");
}

function pruneUnsupportedComfyuiSelectedLoras({ notify = false } = {}) {
  if (notify) {
    const warnings = uniqueComfyuiLoraCompatibilityHints(comfyuiSelectedLoras);
    if (warnings.length) setComfyuiMessage(`已保留已選 LoRA；提醒：${warnings.slice(0, 3).join("；")}`, false);
  }
  return [];
}

function uniqueComfyuiLoraCompatibilityHints(loras = []) {
  const seen = new Set();
  const warnings = [];
  (Array.isArray(loras) ? loras : []).forEach((item) => {
    const name = normalizeComfyuiLoraName(item?.name);
    const hint = name ? comfyuiLoraCompatibilityHint(name) : "";
    if (!hint || seen.has(hint)) return;
    seen.add(hint);
    warnings.push(hint);
  });
  return warnings;
}

function comfyuiLoraCompatibilityHint(name) {
  const cleanName = normalizeComfyuiLoraName(name);
  const detail = comfyuiLoraDetails?.[cleanName] || {};
  if (!cleanName || detail.supported === true) return "";
  if (detail.base_model) return `${detail.base_model} LoRA 可能需要搭配同系列模型`;
  return "base model metadata 未知，請確認和目前模型相容";
}

function applyComfyuiPromptTerms(terms = []) {
  const prompt = $("comfyui-prompt");
  if (!prompt) return [];
  const existing = String(prompt.value || "");
  const existingLower = existing.toLowerCase();
  const normalizedTerms = Array.isArray(terms)
    ? terms.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  const missingTerms = normalizedTerms.filter((term) => !existingLower.includes(term.toLowerCase()));
  if (!missingTerms.length) return [];
  const separator = existing.trim() ? ", " : "";
  prompt.value = `${existing}${separator}${missingTerms.join(", ")}`;
  writeComfyuiDraft();
  return missingTerms;
}

function comfyuiPromptField(promptType = "prompt") {
  return $(promptType === "negative" ? "comfyui-negative-prompt" : "comfyui-prompt");
}

function comfyuiPromptTypeForElement(el) {
  const id = String(el?.id || "");
  if (id === "comfyui-negative-prompt") return "negative";
  if (id === "comfyui-prompt") return "prompt";
  return "";
}

function rememberComfyuiPromptFocus(el) {
  const promptType = comfyuiPromptTypeForElement(el);
  if (promptType) comfyuiLastFocusedPromptType = promptType;
}

function bindComfyuiPromptCursorTracking() {
  ["comfyui-prompt", "comfyui-negative-prompt"].forEach((id) => {
    const el = $(id);
    if (!el || el.dataset.comfyuiPromptCursorBound === "1") return;
    el.dataset.comfyuiPromptCursorBound = "1";
    ["focus", "click", "keyup", "select", "mouseup", "touchend"].forEach((eventName) => {
      el.addEventListener(eventName, () => rememberComfyuiPromptFocus(el));
    });
  });
}

function currentComfyuiPromptTypeForInsertion() {
  return comfyuiPromptTypeForElement(document.activeElement) || comfyuiLastFocusedPromptType || "prompt";
}

function comfyuiPromptTypeLabel(promptType = "prompt") {
  return promptType === "negative" ? "負面" : "正向";
}

function splitComfyuiPromptTerms(text) {
  return String(text || "")
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function joinComfyuiPromptTerms(terms = []) {
  return Array.isArray(terms)
    ? terms.map((item) => String(item || "").trim()).filter(Boolean).join(", ")
    : "";
}

function removeComfyuiPromptTermsFromInput(input, terms = []) {
  if (!input) return [];
  const normalizedTerms = Array.isArray(terms)
    ? terms.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  if (!normalizedTerms.length) return [];
  const removalSet = new Set(normalizedTerms.map((item) => item.toLowerCase()));
  const removed = [];
  const keptTerms = splitComfyuiPromptTerms(input.value).filter((term) => {
    const shouldRemove = removalSet.has(term.toLowerCase());
    if (shouldRemove) removed.push(term);
    return !shouldRemove;
  });
  if (!removed.length) return [];
  input.value = joinComfyuiPromptTerms(keptTerms);
  return removed;
}

function removeComfyuiPromptTerms(terms = [], { promptType = "prompt" } = {}) {
  const prompt = comfyuiPromptField(promptType);
  if (!prompt) return [];
  const removed = removeComfyuiPromptTermsFromInput(prompt, terms);
  if (!removed.length) return [];
  writeComfyuiDraft();
  return removed;
}

function comfyuiEmbeddingTokenVariants(name) {
  const cleanName = String(name || "").trim();
  if (!cleanName) return [];
  return [
    `<embeddings:${cleanName}>`,
    `<embedding:${cleanName}>`,
    `embedding:${cleanName}`,
  ];
}

function comfyuiEscapeRegExp(text) {
  return String(text || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function cleanupComfyuiPromptTextAfterRemoval(text) {
  return String(text || "")
    .replace(/(?:[ \t]*,[ \t]*){2,}/g, ", ")
    .replace(/^[\s,]+|[\s,]+$/g, "")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/\s*,\s*/g, ", ");
}

function removeComfyuiEmbeddingTokenFromInput(input, name) {
  if (!input) return [];
  const variants = comfyuiEmbeddingTokenVariants(name).sort((a, b) => b.length - a.length);
  if (!variants.length) return [];
  let next = String(input.value || "");
  const removed = [];
  variants.forEach((variant) => {
    const tokenPattern = comfyuiEscapeRegExp(variant);
    const re = new RegExp(`(^|[\\s,])(${tokenPattern})(?=$|[\\s,])`, "gi");
    next = next.replace(re, (_match, leading, token) => {
      removed.push(token);
      return leading || "";
    });
  });
  if (!removed.length) return [];
  input.value = cleanupComfyuiPromptTextAfterRemoval(next);
  return removed;
}

function removeComfyuiEmbeddingTokenFromPrompt(name, { promptType = "prompt" } = {}) {
  const prompt = comfyuiPromptField(promptType);
  const removed = removeComfyuiEmbeddingTokenFromInput(prompt, name);
  if (removed.length) writeComfyuiDraft();
  return removed;
}

function insertComfyuiEmbeddingToken(name) {
  const cleanName = String(name || "").trim();
  const promptType = currentComfyuiPromptTypeForInsertion();
  const prompt = comfyuiPromptField(promptType);
  if (!prompt || !cleanName) return;
  const embeddingTag = `<embeddings:${cleanName}>`;
  const removed = removeComfyuiEmbeddingTokenFromPrompt(cleanName, { promptType });
  if (removed.length) {
    setComfyuiMessage(`已從${comfyuiPromptTypeLabel(promptType)}提示詞移除 ${cleanName}。`, true);
    prompt.focus();
    return;
  }
  const raw = prompt.value || "";
  const start = Number.isInteger(prompt.selectionStart) ? prompt.selectionStart : raw.length;
  const end = Number.isInteger(prompt.selectionEnd) ? prompt.selectionEnd : raw.length;
  const prefix = start > 0 && !/[\s,\n]$/.test(raw.slice(0, start)) ? ", " : "";
  const suffix = end < raw.length && !/^[\s,]/.test(raw.slice(end)) ? " " : "";
  prompt.value = `${raw.slice(0, start)}${prefix}${embeddingTag}${suffix}${raw.slice(end)}`;
  const cursor = start + prefix.length + embeddingTag.length + suffix.length;
  prompt.focus();
  if (typeof prompt.setSelectionRange === "function") prompt.setSelectionRange(cursor, cursor);
  writeComfyuiDraft();
  setComfyuiMessage(`已把 ${cleanName} 插入${comfyuiPromptTypeLabel(promptType)}提示詞。`, true);
}

function renderComfyuiEmbeddingShortcuts(values = []) {
  comfyuiAvailableEmbeddings = Array.isArray(values) ? values.filter(Boolean).map(String) : [];
  const box = $("comfyui-embedding-shortcuts");
  if (!box) return;
  const field = $("comfyui-embedding-shortcuts-field") || box.closest(".field");
  const supportsEmbeddingShortcut = typeof comfyuiTemplateSupportsEmbeddingShortcuts === "function"
    ? comfyuiTemplateSupportsEmbeddingShortcuts()
    : true;
  if (field) field.style.display = comfyuiAvailableEmbeddings.length ? "" : "none";
  if (!supportsEmbeddingShortcut || !comfyuiAvailableEmbeddings.length) {
    box.innerHTML = "";
    if (field) field.style.display = "none";
    return;
  }
  if (field) field.style.display = "";
  box.innerHTML = comfyuiAvailableEmbeddings.map((value) => (
    `<button class="comfyui-embedding-chip" type="button" data-comfyui-embedding="${sanitize(value)}" title="插入 / 移除 ${sanitize(value)}">${sanitize(value)}</button>`
  )).join("");
  box.querySelectorAll("[data-comfyui-embedding]").forEach((button) => {
    button.addEventListener("click", () => insertComfyuiEmbeddingToken(button.getAttribute("data-comfyui-embedding")));
  });
}

function loraTermsStillNeededAfterRemoving(nameToRemove) {
  const needed = new Set();
  const removedName = normalizeComfyuiLoraName(nameToRemove);
  const addNeededTerms = (name) => {
    const cleanName = normalizeComfyuiLoraName(name);
    if (!cleanName) return;
    const detail = comfyuiLoraDetails?.[cleanName] || {};
    (detail.trained_words || []).forEach((term) => {
      const cleanTerm = String(term || "").trim();
      if (cleanTerm) needed.add(cleanTerm.toLowerCase());
    });
  };
  comfyuiSelectedLoras.forEach((item) => {
    const name = normalizeComfyuiLoraName(item?.name);
    if (!name || name === removedName) return;
    addNeededTerms(name);
  });
  if (typeof comfyuiWorkflowLoraNamesForPromptSync === "function") {
    comfyuiWorkflowLoraNamesForPromptSync().forEach((name) => addNeededTerms(name));
  }
  return needed;
}

function removeComfyuiSelectedLoraByIndex(index) {
  const current = comfyuiSelectedLoras[index];
  if (!current) return;
  const currentName = normalizeComfyuiLoraName(current.name);
  const detail = comfyuiLoraDetails?.[currentName] || {};
  const trainedWords = Array.isArray(detail.trained_words)
    ? detail.trained_words.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  const stillNeeded = loraTermsStillNeededAfterRemoving(currentName);
  const removableTerms = trainedWords.filter((term) => !stillNeeded.has(term.toLowerCase()));
  comfyuiSelectedLoras.splice(index, 1);
  if (removableTerms.length) removeComfyuiPromptTerms(removableTerms, { promptType: "prompt" });
  renderComfyuiSelectedLoras();
  writeComfyuiDraft();
}

function clearSelectedComfyuiLoras() {
  if (!comfyuiSelectedLoras.length) {
    setComfyuiMessage("目前沒有已加入的 LoRA。", false);
    return;
  }
  const removableTerms = [];
  comfyuiSelectedLoras.forEach((item) => {
    const detail = comfyuiLoraDetails?.[normalizeComfyuiLoraName(item?.name)] || {};
    (detail.trained_words || []).forEach((term) => {
      const cleanTerm = String(term || "").trim();
      if (cleanTerm) removableTerms.push(cleanTerm);
    });
  });
  comfyuiSelectedLoras = [];
  if (removableTerms.length) removeComfyuiPromptTerms(removableTerms, { promptType: "prompt" });
  renderComfyuiSelectedLoras();
  writeComfyuiDraft();
  setComfyuiMessage("已清空已選 LoRA，並移除相關 trigger words。", true);
}

function addSelectedComfyuiLora() {
  const name = normalizeComfyuiLoraName($("comfyui-lora-select")?.value || "");
  if (!name) {
    clearSelectedComfyuiLoras();
    return;
  }
  if (comfyuiSelectedLoras.length >= COMFYUI_MAX_LORAS) {
    setComfyuiMessage(`已達 LoRA 數量上限 ${COMFYUI_MAX_LORAS} 個。`, false);
    return;
  }
  if (comfyuiSelectedLoras.some((item) => normalizeComfyuiLoraName(item?.name) === name)) {
    setComfyuiMessage("這個 LoRA 已經加入。", false);
    return;
  }
  const detail = comfyuiLoraDetails?.[name] || {};
  const hint = comfyuiLoraCompatibilityHint(name);
  comfyuiSelectedLoras.push({ name, strength_model: 1, strength_clip: 1 });
  const insertedTerms = applyComfyuiPromptTerms(detail.trained_words || []);
  renderComfyuiSelectedLoras();
  writeComfyuiDraft();
  const triggerText = insertedTerms.length ? `，並自動補上 trigger words：${insertedTerms.join(", ")}` : "。";
  const warningText = hint ? `提醒：${hint}；若模型不相容，ComfyUI 可能產圖失敗或效果異常。` : "";
  setComfyuiMessage(`已加入 LoRA${triggerText}${warningText ? ` ${warningText}` : ""}`, !hint);
}

function bindComfyuiDraftPersistence() {
  restoreComfyuiDraft({ includeDynamicSelects: false });
  COMFYUI_DRAFT_FIELD_IDS.forEach((id) => {
    const el = $(id);
    if (!el || el.dataset.comfyuiDraftBound === "1") return;
    el.dataset.comfyuiDraftBound = "1";
    if (id === "comfyui-save-path") {
      el.addEventListener("input", () => {
        el.dataset.comfyuiAutoPath = "0";
      });
    }
    const persist = () => {
      writeComfyuiDraft();
      if (typeof queueRenderSelectedComfyuiTemplate === "function") queueRenderSelectedComfyuiTemplate();
    };
    el.addEventListener("input", persist);
    el.addEventListener("change", persist);
  });
}

function bindComfyuiAdvancedUi() {
  bindComfyuiPromptCursorTracking();
  const modeSelect = $("comfyui-generation-mode");
  if (modeSelect && modeSelect.dataset.comfyuiBound !== "1") {
    modeSelect.dataset.comfyuiBound = "1";
    modeSelect.addEventListener("change", () => {
      clearComfyuiDiffusersInspection();
      updateComfyuiModeVisibility();
      writeComfyuiDraft();
    });
  }
  const diffusersRepoInput = $("comfyui-diffusers-model-repo");
  if (diffusersRepoInput && diffusersRepoInput.dataset.comfyuiBound !== "1") {
    diffusersRepoInput.dataset.comfyuiBound = "1";
    diffusersRepoInput.addEventListener("input", () => {
      const repo = normalizeComfyuiHuggingFaceRepoInput(diffusersRepoInput.value || "");
      clearComfyuiDiffusersGgufProfileIfRepoChanged(repo);
      clearComfyuiDiffusersInspection();
      updateComfyuiModeVisibility();
      renderComfyuiDiffusersCommonRepos();
    });
    diffusersRepoInput.addEventListener("change", () => {
      if (sanitizeComfyuiDiffusersRepoField({ strict: true, quiet: true })) inspectComfyuiDiffusersRepo({ quiet: true });
      renderComfyuiDiffusersCommonRepos();
    });
    diffusersRepoInput.addEventListener("blur", () => {
      if (sanitizeComfyuiDiffusersRepoField({ strict: true, quiet: true })) inspectComfyuiDiffusersRepo({ quiet: true });
      renderComfyuiDiffusersCommonRepos();
    });
  }
  renderComfyuiDiffusersCommonRepos();
  const diffusersCommonRepoSelect = $("comfyui-diffusers-common-repo");
  if (diffusersCommonRepoSelect && diffusersCommonRepoSelect.dataset.comfyuiBound !== "1") {
    diffusersCommonRepoSelect.dataset.comfyuiBound = "1";
    diffusersCommonRepoSelect.addEventListener("change", () => {
      const repo = diffusersCommonRepoSelect.value || "";
      if (repo) setComfyuiDiffusersRepo(repo, { inspect: true });
      else {
        updateComfyuiModeVisibility();
        renderComfyuiDiffusersCommonRepos();
      }
    });
  }
  const diffusersCommonRepoAddBtn = $("comfyui-diffusers-add-common-repo");
  if (diffusersCommonRepoAddBtn && diffusersCommonRepoAddBtn.dataset.comfyuiBound !== "1") {
    diffusersCommonRepoAddBtn.dataset.comfyuiBound = "1";
    diffusersCommonRepoAddBtn.addEventListener("click", addCurrentComfyuiDiffusersCommonRepo);
  }
  const diffusersCommonRepoRemoveBtn = $("comfyui-diffusers-remove-common-repo");
  if (diffusersCommonRepoRemoveBtn && diffusersCommonRepoRemoveBtn.dataset.comfyuiBound !== "1") {
    diffusersCommonRepoRemoveBtn.dataset.comfyuiBound = "1";
    diffusersCommonRepoRemoveBtn.addEventListener("click", removeSelectedComfyuiDiffusersCommonRepo);
  }
  const diffusersInspectBtn = $("comfyui-diffusers-inspect-btn");
  if (diffusersInspectBtn && diffusersInspectBtn.dataset.comfyuiBound !== "1") {
    diffusersInspectBtn.dataset.comfyuiBound = "1";
    diffusersInspectBtn.addEventListener("click", () => inspectComfyuiDiffusersRepo({ quiet: false }));
  }
  const diffusersVariantSelect = $("comfyui-diffusers-model-variant");
  if (diffusersVariantSelect && diffusersVariantSelect.dataset.comfyuiBound !== "1") {
    diffusersVariantSelect.dataset.comfyuiBound = "1";
    diffusersVariantSelect.addEventListener("change", () => {
      if (diffusersVariantSelect.value && $("comfyui-diffusers-gguf-profile")) {
        $("comfyui-diffusers-gguf-profile").value = "";
        fillComfyuiGgufVariants("");
      }
      updateComfyuiDiffusersGgufOptions();
      writeComfyuiDraft();
    });
  }
  const ggufProfileSelect = $("comfyui-diffusers-gguf-profile");
  if (ggufProfileSelect && ggufProfileSelect.dataset.comfyuiBound !== "1") {
    ggufProfileSelect.dataset.comfyuiBound = "1";
    ggufProfileSelect.addEventListener("change", () => {
      fillComfyuiGgufVariants(ggufProfileSelect.value || "");
      updateComfyuiGgufProfileSelection({ syncRepo: true });
      writeComfyuiDraft();
    });
  }
  const ggufVariantSelect = $("comfyui-diffusers-gguf-variant");
  if (ggufVariantSelect && ggufVariantSelect.dataset.comfyuiBound !== "1") {
    ggufVariantSelect.dataset.comfyuiBound = "1";
    ggufVariantSelect.addEventListener("change", () => {
      updateComfyuiGgufProfileSelection({ syncRepo: true });
      writeComfyuiDraft();
    });
  }
  const controlEnabled = $("comfyui-controlnet-enabled");
  if (controlEnabled && controlEnabled.dataset.comfyuiBound !== "1") {
    controlEnabled.dataset.comfyuiBound = "1";
    controlEnabled.addEventListener("change", () => {
      updateComfyuiModeVisibility();
      writeComfyuiDraft();
    });
  }
  const controlType = $("comfyui-controlnet-type");
  if (controlType && controlType.dataset.comfyuiBound !== "1") {
    controlType.dataset.comfyuiBound = "1";
    controlType.addEventListener("change", () => {
      fillComfyuiControlnetModelOptions();
      fillComfyuiControlnetPreprocessorOptions();
      updateComfyuiControlnetTip();
      writeComfyuiDraft();
    });
  }
  const upscaleModel = $("comfyui-upscale-model");
  if (upscaleModel && upscaleModel.dataset.comfyuiBound !== "1") {
    upscaleModel.dataset.comfyuiBound = "1";
    upscaleModel.addEventListener("change", () => {
      updateComfyuiModeVisibility();
      writeComfyuiDraft();
    });
  }
  const modelSourceMode = $("comfyui-model-source-mode");
  if (modelSourceMode && modelSourceMode.dataset.comfyuiBound !== "1") {
    modelSourceMode.dataset.comfyuiBound = "1";
    modelSourceMode.addEventListener("change", updateComfyuiModelSourceMode);
  }
  const modelDownloadType = $("comfyui-model-download-type");
  if (modelDownloadType && modelDownloadType.dataset.comfyuiBound !== "1") {
    modelDownloadType.dataset.comfyuiBound = "1";
    modelDownloadType.addEventListener("change", () => {
      updateComfyuiModelRelativePathHint();
      writeComfyuiDraft();
    });
  }
  const modelRelativePath = $("comfyui-model-relative-path");
  if (modelRelativePath && modelRelativePath.dataset.comfyuiBound !== "1") {
    modelRelativePath.dataset.comfyuiBound = "1";
    modelRelativePath.addEventListener("input", () => {
      updateComfyuiModelRelativePathHint();
      writeComfyuiDraft();
    });
  }
  const uploadBtn = $("comfyui-model-upload-btn");
  if (uploadBtn && uploadBtn.dataset.comfyuiBound !== "1") {
    uploadBtn.dataset.comfyuiBound = "1";
    uploadBtn.addEventListener("click", () => {
      uploadComfyuiModelFile().catch((err) => setComfyuiMessage(err.message || "模型上傳失敗", false));
    });
  }
  const favoriteBtn = $("comfyui-favorite-btn");
  if (favoriteBtn && favoriteBtn.dataset.comfyuiBound !== "1") {
    favoriteBtn.dataset.comfyuiBound = "1";
    favoriteBtn.addEventListener("click", () => {
      favoriteComfyuiGeneratedImage().catch((err) => setComfyuiMessage(err.message || "圖片收藏失敗", false));
    });
  }
  const favoritesRefreshBtn = $("comfyui-favorites-refresh-btn");
  if (favoritesRefreshBtn && favoritesRefreshBtn.dataset.comfyuiBound !== "1") {
    favoritesRefreshBtn.dataset.comfyuiBound = "1";
    favoritesRefreshBtn.addEventListener("click", () => {
      loadComfyuiImageFavorites().catch((err) => setComfyuiMessage(err.message || "圖片收藏讀取失敗", false));
    });
  }
  const favoriteCivitaiOpenBtn = $("comfyui-favorite-civitai-open-btn");
  if (favoriteCivitaiOpenBtn && favoriteCivitaiOpenBtn.dataset.comfyuiBound !== "1") {
    favoriteCivitaiOpenBtn.dataset.comfyuiBound = "1";
    favoriteCivitaiOpenBtn.addEventListener("click", () => openComfyuiFavoriteModal("civitai"));
  }
  const favoriteUploadOpenBtn = $("comfyui-favorite-upload-open-btn");
  if (favoriteUploadOpenBtn && favoriteUploadOpenBtn.dataset.comfyuiBound !== "1") {
    favoriteUploadOpenBtn.dataset.comfyuiBound = "1";
    favoriteUploadOpenBtn.addEventListener("click", () => openComfyuiFavoriteModal("upload"));
  }
  const favoriteModalCloseBtn = $("comfyui-favorite-modal-close-btn");
  if (favoriteModalCloseBtn && favoriteModalCloseBtn.dataset.comfyuiBound !== "1") {
    favoriteModalCloseBtn.dataset.comfyuiBound = "1";
    favoriteModalCloseBtn.addEventListener("click", closeComfyuiFavoriteModal);
  }
  const favoriteModal = $("comfyui-favorite-modal");
  if (favoriteModal && favoriteModal.dataset.comfyuiBound !== "1") {
    favoriteModal.dataset.comfyuiBound = "1";
    favoriteModal.addEventListener("click", (event) => {
      if (event.target === favoriteModal) closeComfyuiFavoriteModal();
    });
  }
  document.querySelectorAll("[data-comfyui-favorite-mode]").forEach((button) => {
    if (button.dataset.comfyuiBound === "1") return;
    button.dataset.comfyuiBound = "1";
    button.addEventListener("click", () => setComfyuiFavoriteModalMode(button.getAttribute("data-comfyui-favorite-mode") || "civitai"));
  });
  if (!document.body.dataset.comfyuiFavoriteEscapeBound) {
    document.body.dataset.comfyuiFavoriteEscapeBound = "1";
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeComfyuiFavoriteModal();
        closeComfyuiFavoritePreviewModal();
      }
    });
  }
  const favoriteCivitaiImportBtn = $("comfyui-favorite-civitai-import-btn");
  if (favoriteCivitaiImportBtn && favoriteCivitaiImportBtn.dataset.comfyuiBound !== "1") {
    favoriteCivitaiImportBtn.dataset.comfyuiBound = "1";
    favoriteCivitaiImportBtn.addEventListener("click", () => {
      importComfyuiFavoriteFromCivitai().catch((err) => setComfyuiMessage(err.message || "Civitai 圖片匯入失敗", false));
    });
  }
  const favoriteUploadSaveBtn = $("comfyui-favorite-upload-save-btn");
  if (favoriteUploadSaveBtn && favoriteUploadSaveBtn.dataset.comfyuiBound !== "1") {
    favoriteUploadSaveBtn.dataset.comfyuiBound = "1";
    favoriteUploadSaveBtn.addEventListener("click", () => {
      saveUploadedComfyuiFavorite().catch((err) => setComfyuiMessage(err.message || "上傳收藏失敗", false));
    });
  }
  const civitaiSearchBtn = $("comfyui-civitai-search-btn");
  if (civitaiSearchBtn && civitaiSearchBtn.dataset.comfyuiBound !== "1") {
    civitaiSearchBtn.dataset.comfyuiBound = "1";
    civitaiSearchBtn.addEventListener("click", () => {
      searchComfyuiCivitaiModels().catch((err) => setComfyuiMessage(err.message || "Civitai 搜尋失敗", false));
    });
  }
  ["model", "preprocessor"].forEach((name) => {
    const el = $(`comfyui-controlnet-${name}`);
    if (!el || el.dataset.comfyuiBound === "1") return;
    el.dataset.comfyuiBound = "1";
    el.addEventListener("change", writeComfyuiDraft);
  });
  Object.entries(COMFYUI_INPUT_ASSET_META).forEach(([key, meta]) => {
    const fileInput = $(meta.fileInputId);
    if (fileInput && fileInput.dataset.comfyuiBound !== "1") {
      fileInput.dataset.comfyuiBound = "1";
      fileInput.addEventListener("change", () => {
        const file = fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
        if (!file) {
          clearComfyuiInputAsset(key);
          return;
        }
        if (!comfyuiCanUseInputAssetForCurrentMode(key)) {
          setComfyuiMessage("這個 HF repo 已判斷為文字生圖，不需要來源圖片或遮罩。", false);
          fileInput.value = "";
          return;
        }
        if (!/^image\/(png|jpeg|webp)$/i.test(file.type || "")) {
          setComfyuiMessage("控制圖、遮罩圖與來源圖只支援 PNG、JPG、WEBP。", false);
          fileInput.value = "";
          return;
        }
        setComfyuiInputAssetFromFile(key, file);
      });
    }
    const clearBtn = $(meta.clearBtnId);
    if (clearBtn && clearBtn.dataset.comfyuiBound !== "1") {
      clearBtn.dataset.comfyuiBound = "1";
      clearBtn.addEventListener("click", () => clearComfyuiInputAsset(key));
    }
    const pickerBtn = meta.pickerBtnId ? $(meta.pickerBtnId) : null;
    if (pickerBtn && pickerBtn.dataset.comfyuiBound !== "1") {
      pickerBtn.dataset.comfyuiBound = "1";
      pickerBtn.addEventListener("click", () => {
        if (!comfyuiCanUseInputAssetForCurrentMode(key)) {
          setComfyuiMessage("這個 HF repo 已判斷為文字生圖，不需要來源圖片或遮罩。", false);
          return;
        }
        openComfyuiImagePicker(key).catch((err) => setComfyuiMessage(err.message || "圖片選擇器開啟失敗", false));
      });
    }
  });
  const imagePickerCloseBtn = $("comfyui-image-picker-close-btn");
  if (imagePickerCloseBtn && imagePickerCloseBtn.dataset.comfyuiBound !== "1") {
    imagePickerCloseBtn.dataset.comfyuiBound = "1";
    imagePickerCloseBtn.addEventListener("click", closeComfyuiImagePicker);
  }
  const maskEditorOpenBtn = $("comfyui-mask-editor-open-btn");
  if (maskEditorOpenBtn && maskEditorOpenBtn.dataset.comfyuiBound !== "1") {
    maskEditorOpenBtn.dataset.comfyuiBound = "1";
    maskEditorOpenBtn.addEventListener("click", () => openComfyuiMaskEditor());
  }
  const maskEditorCloseBtn = $("comfyui-mask-editor-close-btn");
  const maskEditorCancelBtn = $("comfyui-mask-editor-cancel-btn");
  [maskEditorCloseBtn, maskEditorCancelBtn].forEach((button) => {
    if (!button || button.dataset.comfyuiBound === "1") return;
    button.dataset.comfyuiBound = "1";
    button.addEventListener("click", closeComfyuiMaskEditor);
  });
  const maskEditorPaintBtn = $("comfyui-mask-editor-paint-btn");
  if (maskEditorPaintBtn && maskEditorPaintBtn.dataset.comfyuiBound !== "1") {
    maskEditorPaintBtn.dataset.comfyuiBound = "1";
    maskEditorPaintBtn.addEventListener("click", () => setComfyuiMaskEditorMode("paint"));
  }
  const maskEditorEraseBtn = $("comfyui-mask-editor-erase-btn");
  if (maskEditorEraseBtn && maskEditorEraseBtn.dataset.comfyuiBound !== "1") {
    maskEditorEraseBtn.dataset.comfyuiBound = "1";
    maskEditorEraseBtn.addEventListener("click", () => setComfyuiMaskEditorMode("erase"));
  }
  const maskEditorBrush = $("comfyui-mask-editor-brush");
  if (maskEditorBrush && maskEditorBrush.dataset.comfyuiBound !== "1") {
    maskEditorBrush.dataset.comfyuiBound = "1";
    maskEditorBrush.addEventListener("input", updateComfyuiMaskEditorToolbar);
  }
  const maskEditorResizeBtn = $("comfyui-mask-editor-resize-btn");
  if (maskEditorResizeBtn && maskEditorResizeBtn.dataset.comfyuiBound !== "1") {
    maskEditorResizeBtn.dataset.comfyuiBound = "1";
    maskEditorResizeBtn.addEventListener("click", applyComfyuiMaskEditorSize);
  }
  const maskEditorClearBtn = $("comfyui-mask-editor-clear-btn");
  if (maskEditorClearBtn && maskEditorClearBtn.dataset.comfyuiBound !== "1") {
    maskEditorClearBtn.dataset.comfyuiBound = "1";
    maskEditorClearBtn.addEventListener("click", () => clearComfyuiMaskEditorCanvas(false));
  }
  const maskEditorFillBtn = $("comfyui-mask-editor-fill-btn");
  if (maskEditorFillBtn && maskEditorFillBtn.dataset.comfyuiBound !== "1") {
    maskEditorFillBtn.dataset.comfyuiBound = "1";
    maskEditorFillBtn.addEventListener("click", () => clearComfyuiMaskEditorCanvas(true));
  }
  const maskEditorInvertBtn = $("comfyui-mask-editor-invert-btn");
  if (maskEditorInvertBtn && maskEditorInvertBtn.dataset.comfyuiBound !== "1") {
    maskEditorInvertBtn.dataset.comfyuiBound = "1";
    maskEditorInvertBtn.addEventListener("click", invertComfyuiMaskEditorCanvas);
  }
  const maskEditorApplyBtn = $("comfyui-mask-editor-apply-btn");
  if (maskEditorApplyBtn && maskEditorApplyBtn.dataset.comfyuiBound !== "1") {
    maskEditorApplyBtn.dataset.comfyuiBound = "1";
    maskEditorApplyBtn.addEventListener("click", () => applyComfyuiMaskEditor());
  }
  const maskEditorCanvas = $("comfyui-mask-editor-mask-canvas");
  if (maskEditorCanvas && maskEditorCanvas.dataset.comfyuiBound !== "1") {
    maskEditorCanvas.dataset.comfyuiBound = "1";
    maskEditorCanvas.addEventListener("pointerdown", handleComfyuiMaskPointerDown);
    maskEditorCanvas.addEventListener("pointermove", handleComfyuiMaskPointerMove);
    maskEditorCanvas.addEventListener("pointerup", stopComfyuiMaskPointer);
    maskEditorCanvas.addEventListener("pointercancel", stopComfyuiMaskPointer);
    maskEditorCanvas.addEventListener("pointerleave", (event) => {
      if (comfyuiMaskEditorState.drawing) stopComfyuiMaskPointer(event);
    });
  }
  const refreshBtn = $("comfyui-history-refresh-btn");
  if (refreshBtn && refreshBtn.dataset.comfyuiBound !== "1") {
    refreshBtn.dataset.comfyuiBound = "1";
    refreshBtn.addEventListener("click", () => {
      loadComfyuiHistory().catch((err) => setComfyuiMessage(err.message || "ComfyUI 歷史紀錄讀取失敗", false));
    });
  }
  const workflowRefreshBtn = $("comfyui-workflows-refresh-btn");
  if (workflowRefreshBtn && workflowRefreshBtn.dataset.comfyuiBound !== "1") {
    workflowRefreshBtn.dataset.comfyuiBound = "1";
    workflowRefreshBtn.addEventListener("click", () => {
      loadComfyuiWorkflowPresets().catch((err) => setComfyuiMessage(err.message || "workflow preset 讀取失敗", false));
    });
  }
  const workflowFile = $("comfyui-workflow-file");
  if (workflowFile && workflowFile.dataset.comfyuiBound !== "1") {
    workflowFile.dataset.comfyuiBound = "1";
    workflowFile.addEventListener("change", () => {
      loadComfyuiWorkflowFile().catch((err) => setComfyuiMessage(err.message || "workflow 檔案讀取失敗", false));
    });
  }
  const workflowExportCurrentBtn = $("comfyui-workflow-export-current-btn");
  if (workflowExportCurrentBtn && workflowExportCurrentBtn.dataset.comfyuiBound !== "1") {
    workflowExportCurrentBtn.dataset.comfyuiBound = "1";
    workflowExportCurrentBtn.addEventListener("click", () => {
      exportCurrentComfyuiWorkflow().catch((err) => setComfyuiMessage(err.message || "workflow 匯出失敗", false));
    });
  }
  const workflowLoadVisualBtn = $("comfyui-workflow-load-visual-btn");
  if (workflowLoadVisualBtn && workflowLoadVisualBtn.dataset.comfyuiBound !== "1") {
    workflowLoadVisualBtn.dataset.comfyuiBound = "1";
    workflowLoadVisualBtn.addEventListener("click", () => loadComfyuiVisualWorkflowEditorResult());
  }
  const workflowOpenVisualBtn = $("comfyui-workflow-open-visual-btn");
  if (workflowOpenVisualBtn && workflowOpenVisualBtn.dataset.comfyuiBound !== "1") {
    workflowOpenVisualBtn.dataset.comfyuiBound = "1";
    workflowOpenVisualBtn.addEventListener("click", () => prepareComfyuiVisualWorkflowEditorInput());
  }
  const workflowImportBtn = $("comfyui-workflow-import-btn");
  if (workflowImportBtn && workflowImportBtn.dataset.comfyuiBound !== "1") {
    workflowImportBtn.dataset.comfyuiBound = "1";
    workflowImportBtn.addEventListener("click", () => {
      importComfyuiWorkflowPreset().catch((err) => setComfyuiMessage(err.message || "workflow 匯入失敗", false));
    });
  }
  const workflowUpdateBtn = $("comfyui-workflow-update-btn");
  if (workflowUpdateBtn && workflowUpdateBtn.dataset.comfyuiBound !== "1") {
    workflowUpdateBtn.dataset.comfyuiBound = "1";
    workflowUpdateBtn.addEventListener("click", () => {
      updateComfyuiWorkflowPreset().catch((err) => setComfyuiMessage(err.message || "workflow 更新失敗", false));
    });
  }
  const workflowResetBtn = $("comfyui-workflow-reset-btn");
  if (workflowResetBtn && workflowResetBtn.dataset.comfyuiBound !== "1") {
    workflowResetBtn.dataset.comfyuiBound = "1";
    workflowResetBtn.addEventListener("click", () => resetComfyuiWorkflowEditor());
  }
  const workflowNewBtn = $("comfyui-workflow-new-btn");
  if (workflowNewBtn && workflowNewBtn.dataset.comfyuiBound !== "1") {
    workflowNewBtn.dataset.comfyuiBound = "1";
    workflowNewBtn.addEventListener("click", () => {
      createBlankComfyuiWorkflowLayout();
    });
  }
  const workflowStarterTxt2ImgBtn = $("comfyui-workflow-starter-txt2img-btn");
  if (workflowStarterTxt2ImgBtn && workflowStarterTxt2ImgBtn.dataset.comfyuiBound !== "1") {
    workflowStarterTxt2ImgBtn.dataset.comfyuiBound = "1";
    workflowStarterTxt2ImgBtn.addEventListener("click", () => {
      createTxt2ImgComfyuiStarterWorkflow();
    });
  }
  const workflowAddNodeBtn = $("comfyui-workflow-add-node-btn");
  if (workflowAddNodeBtn && workflowAddNodeBtn.dataset.comfyuiBound !== "1") {
    workflowAddNodeBtn.dataset.comfyuiBound = "1";
    workflowAddNodeBtn.addEventListener("click", () => {
      try {
        addComfyuiWorkflowNode(
          $("comfyui-workflow-node-template")?.value || "",
          $("comfyui-workflow-node-label")?.value || "",
        );
      } catch (err) {
        setComfyuiMessage(err.message || "追加 workflow 節點失敗", false);
      }
    });
  }
  [
    "comfyui-workflow-title",
    "comfyui-workflow-description",
    "comfyui-workflow-visibility",
    "comfyui-workflow-purpose",
    "comfyui-workflow-comfyui-version",
    "comfyui-workflow-project-version",
    "comfyui-workflow-schema-version",
    "comfyui-workflow-json",
    "comfyui-workflow-layout-json",
    "comfyui-workflow-is-default",
  ].forEach((id) => {
    const field = $(id);
    if (field && field.dataset.comfyuiWorkflowDirtyBound !== "1") {
      field.dataset.comfyuiWorkflowDirtyBound = "1";
      field.addEventListener("input", markComfyuiWorkflowEditorDirty);
      field.addEventListener("change", () => {
        markComfyuiWorkflowEditorDirty();
        if (id === "comfyui-workflow-json" || id === "comfyui-workflow-layout-json") {
          renderComfyuiWorkflowBuilderPreview();
        }
      });
    }
  });
  bindComfyuiSubnav();
  if (!document.body.dataset.comfyuiAccountScopeBound) {
    document.body.dataset.comfyuiAccountScopeBound = "1";
    document.addEventListener("hackme:account-context-changed", () => {
      updateComfyuiRootPanelVisibility();
    });
  }
  updateComfyuiModeVisibility();
  updateComfyuiModelSourceMode();
  Object.keys(COMFYUI_INPUT_ASSET_META).forEach((key) => renderComfyuiInputAsset(key));
}

async function loadComfyuiLastSettings() {
  const draft = readComfyuiDraft();
  if (!Object.keys(draft).length) {
    setComfyuiMessage("目前沒有保存過的 ComfyUI 設定", false);
    return;
  }
  if (!comfyuiModelsLoaded && comfyuiServerAvailable !== false) {
    await loadComfyuiModels();
  }
  restoreComfyuiDraft();
  setComfyuiMessage("已載入上次 ComfyUI 設定", true);
}

async function loadComfyuiAlbums({ force = false } = {}) {
  const select = $("comfyui-album-select");
  if (!select) return [];
  if (comfyuiAlbumsLoaded && !force) {
    restoreComfyuiDraft();
    return Array.from(select.options || []);
  }
  select.innerHTML = '<option value="">不加入相簿</option><option value="" disabled>讀取相簿中...</option>';
  try {
    const json = typeof storageAction === "function"
      ? await storageAction("/storage/albums", "GET")
      : await (async () => {
          await fetchCsrfToken();
          const res = await apiFetch(API + "/storage/albums", {
            credentials: "same-origin",
            headers: { "X-CSRF-Token": getCsrfToken() || "" }
          });
          const body = await res.json().catch(() => ({}));
          if (!res.ok || !body.ok) throw new Error(body.msg || `相簿讀取失敗（HTTP ${res.status}）`);
          return body;
        })();
    const albums = Array.isArray(json.albums) ? json.albums : [];
    select.innerHTML = '<option value="">不加入相簿</option>' + albums.map((album) => (
      `<option value="${sanitize(String(album.id))}">${sanitize(album.title || `相簿 ${album.id}`)}</option>`
    )).join("");
    comfyuiAlbumsLoaded = true;
    restoreComfyuiDraft();
    return albums;
  } catch (err) {
    select.innerHTML = '<option value="">相簿讀取失敗</option>';
    comfyuiAlbumsLoaded = false;
    setComfyuiMessage(err.message || "相簿讀取失敗", false);
    return [];
  }
}

function comfyuiHistoryItemId(item) {
  const raw = item?.id ?? item?.history_id ?? item?.historyId;
  const text = String(raw || "").trim();
  if (text.startsWith("workflow-")) return text;
  const id = Number(raw);
  return Number.isFinite(id) && id > 0 ? String(id) : "";
}

function comfyuiHistoryItemById(historyId) {
  const targetId = String(historyId || "").trim();
  if (!targetId) return null;
  return comfyuiHistoryItems.find((item) => comfyuiHistoryItemId(item) === targetId) || null;
}

function comfyuiHistoryFirstImage(item = {}) {
  const result = item?.result && typeof item.result === "object" ? item.result : {};
  const images = Array.isArray(result.images) && result.images.length ? result.images : [result.image].filter(Boolean);
  return images.find((image) => image && (image.data_url || image.preview_url || image.image_ref?.filename)) || null;
}

function comfyuiHistoryCanFavorite(item = {}) {
  return !!comfyuiHistoryFirstImage(item)?.image_ref?.filename;
}

function comfyuiHistoryImageCount(item = {}, payload = null) {
  const result = item?.result && typeof item.result === "object" ? item.result : {};
  const images = Array.isArray(result.images) ? result.images.filter(Boolean) : [];
  if (images.length) return images.length;
  const source = payload || comfyuiHistoryPayload(item);
  const runCount = Math.max(1, Number(source.run_count || 1));
  const batchSize = Math.max(1, Number(source.ui_batch_size || source.batch_size || 1));
  return Math.max(1, Math.floor(runCount * batchSize));
}

function comfyuiHistoryPreviewMarkup(item = {}) {
  const image = comfyuiHistoryFirstImage(item);
  if (!image) {
    return '<div class="comfyui-history-thumb is-empty">無預覽</div>';
  }
  const src = image.preview_url || image.data_url || "";
  const historyId = comfyuiHistoryItemId(item);
  const label = item?.preset_title || item?.payload?.model || "ComfyUI 歷史預覽";
  if (src) {
    return `<div class="comfyui-history-thumb"><img src="${sanitize(src)}" alt="${sanitize(label)}" loading="lazy" /></div>`;
  }
  return `<div class="comfyui-history-thumb is-loading" data-comfyui-history-preview="${sanitize(historyId)}">讀取預覽中</div>`;
}

async function hydrateComfyuiHistoryPreviews() {
  const targets = Array.from(document.querySelectorAll("[data-comfyui-history-preview]"));
  if (!targets.length) return;
  await fetchCsrfToken().catch(() => {});
  for (const target of targets) {
    const historyId = target.getAttribute("data-comfyui-history-preview") || "";
    const item = comfyuiHistoryItemById(historyId);
    const image = comfyuiHistoryFirstImage(item);
    if (!image?.image_ref?.filename) {
      target.textContent = "無預覽";
      target.classList.remove("is-loading");
      target.classList.add("is-empty");
      continue;
    }
    try {
      const hydrated = await hydrateComfyuiOutputImage(image);
      const src = hydrated.preview_url || hydrated.data_url || "";
      if (!src) throw new Error("預覽資料為空");
      target.classList.remove("is-loading", "is-empty");
      target.innerHTML = `<img src="${sanitize(src)}" alt="${sanitize(item?.preset_title || item?.payload?.model || "ComfyUI 歷史預覽")}" loading="lazy" />`;
    } catch (err) {
      target.textContent = err?.message || "預覽讀取失敗";
      target.classList.remove("is-loading");
      target.classList.add("is-empty");
    }
  }
}

function setComfyuiHistoryActionMessage(text, ok = true) {
  const status = $("comfyui-history-status");
  if (status && text) status.textContent = text;
  setComfyuiMessage(text, ok);
}

function renderComfyuiHistory() {
  const list = $("comfyui-history-list");
  const status = $("comfyui-history-status");
  if (!list) return;
  if (!Array.isArray(comfyuiHistoryItems) || !comfyuiHistoryItems.length) {
    list.innerHTML = '<div class="drive-empty">尚無 ComfyUI 歷史紀錄</div>';
    if (status) status.textContent = "尚未找到可重跑的 ComfyUI 歷史紀錄";
    return;
  }
  if (status) status.textContent = `最近 ${comfyuiHistoryItems.length} 筆 ComfyUI 歷史紀錄，可直接套回或重跑。`;
  list.innerHTML = comfyuiHistoryItems.map((item) => {
    const historyId = comfyuiHistoryItemId(item);
    const payload = item?.payload || {};
    const control = item?.controlnet || {};
    const mode = comfyuiReadableModeLabel(item?.generation_mode || payload?.generation_mode || "txt2img");
    const controlLabel = control?.type ? ` · ControlNet ${String(control.type).toUpperCase()}` : "";
    const model = payload?.model ? ` · ${payload.model}` : "";
    const prompt = sanitize(String(payload?.prompt || "").slice(0, 140) || "（無提示詞）");
    const createdAt = sanitize(String(item?.created_at || "").replace("T", " ").slice(0, 16));
    const disabled = historyId ? "" : " disabled";
    const favoriteDisabled = historyId && comfyuiHistoryCanFavorite(item) ? "" : " disabled";
    const idLabel = item?.history_source === "workflow"
      ? `Workflow run #${item.workflow_run_id || "-"}`
      : (historyId ? `ID #${historyId}` : "ID 未取得");
    const sourceLabel = item?.history_source === "workflow" ? ` · ${item.preset_title || "Workflow"}` : "";
    const imageCount = comfyuiHistoryImageCount(item, payload);
    return `
      <div class="comfyui-history-item">
        ${comfyuiHistoryPreviewMarkup(item)}
        <div class="comfyui-history-body">
          <div class="comfyui-history-head">
            <strong>${sanitize(mode)}</strong>
            <span>${createdAt}</span>
          </div>
          <div class="drive-card-sub">${sanitize(`${idLabel}${sourceLabel}${model}${controlLabel}`)}</div>
          <div class="comfyui-history-prompt">${prompt}</div>
          <div class="drive-card-sub">
            ${sanitize(`步數 ${payload.steps || "-"} · CFG ${payload.cfg || "-"} · Seed ${payload.seed ?? "random"} · 張數 ${imageCount}`)}
          </div>
          <div class="drive-file-actions" style="justify-content:flex-start;">
            <button class="btn btn-sm" type="button" data-comfyui-history-apply="${sanitize(historyId)}"${disabled}>套回表單</button>
            <button class="btn btn-sm" type="button" data-comfyui-history-rerun="${sanitize(historyId)}"${disabled}>一鍵重跑</button>
            <button class="btn btn-sm" type="button" data-comfyui-history-favorite="${sanitize(historyId)}"${favoriteDisabled}>收藏</button>
            <button class="btn btn-sm btn-danger" type="button" data-comfyui-history-delete="${sanitize(historyId)}"${disabled}>刪除</button>
          </div>
        </div>
      </div>
    `;
  }).join("");
  hydrateComfyuiHistoryPreviews().catch(() => {});
  list.querySelectorAll("[data-comfyui-history-apply]").forEach((button) => {
    button.addEventListener("click", () => {
      const historyId = button.getAttribute("data-comfyui-history-apply") || "";
      applyComfyuiHistoryToForm(historyId).catch((err) => {
        setComfyuiHistoryActionMessage(err.message || "ComfyUI 歷史套回表單失敗", false);
      });
    });
  });
  list.querySelectorAll("[data-comfyui-history-rerun]").forEach((button) => {
    button.addEventListener("click", () => {
      const historyId = button.getAttribute("data-comfyui-history-rerun") || "";
      rerunComfyuiHistory(historyId).catch((err) => {
        setComfyuiHistoryActionMessage(err.message || "ComfyUI 歷史重跑失敗", false);
      });
    });
  });
  list.querySelectorAll("[data-comfyui-history-favorite]").forEach((button) => {
    button.addEventListener("click", () => {
      const historyId = button.getAttribute("data-comfyui-history-favorite") || "";
      favoriteComfyuiHistoryImage(historyId).catch((err) => {
        setComfyuiHistoryActionMessage(err.message || "ComfyUI 歷史收藏失敗", false);
      });
    });
  });
  list.querySelectorAll("[data-comfyui-history-delete]").forEach((button) => {
    button.addEventListener("click", () => {
      const historyId = button.getAttribute("data-comfyui-history-delete") || "";
      deleteComfyuiHistory(historyId).catch((err) => {
        setComfyuiHistoryActionMessage(err.message || "ComfyUI 歷史刪除失敗", false);
      });
    });
  });
}

async function loadComfyuiHistory() {
  if (!currentUser || !canAccessModule("comfyui")) return [];
  const status = $("comfyui-history-status");
  if (status) status.textContent = "正在讀取 ComfyUI 歷史紀錄...";
  await fetchCsrfToken();
  const res = await apiFetch(API + "/comfyui/history", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    const message = json.msg || `ComfyUI 歷史紀錄讀取失敗（HTTP ${res.status}）`;
    if (status) status.textContent = message;
    throw new Error(message);
  }
  comfyuiHistoryItems = Array.isArray(json.history) ? json.history : [];
  renderComfyuiHistory();
  return comfyuiHistoryItems;
}

async function applyComfyuiHistoryAssets(inputAssets = {}) {
  await Promise.all([
    inputAssets?.source_image_ref
      ? hydrateComfyuiInputAssetFromRef("source", inputAssets.source_image_ref)
      : Promise.resolve(clearComfyuiInputAsset("source")),
    inputAssets?.mask_image_ref
      ? hydrateComfyuiInputAssetFromRef("mask", inputAssets.mask_image_ref)
      : Promise.resolve(clearComfyuiInputAsset("mask")),
    inputAssets?.control_image_ref
      ? hydrateComfyuiInputAssetFromRef("control", inputAssets.control_image_ref)
      : Promise.resolve(clearComfyuiInputAsset("control")),
  ]);
}

function comfyuiHistoryPayload(item = {}) {
  const params = item?.params && typeof item.params === "object" ? item.params : {};
  const payload = item?.payload && typeof item.payload === "object" ? { ...params, ...item.payload } : { ...params };
  const inputAssets = item?.input_assets && typeof item.input_assets === "object" ? item.input_assets : {};
  if (!payload.generation_mode && item?.generation_mode) payload.generation_mode = item.generation_mode;
  if (!payload.workflow_preset_id && item?.preset_id) payload.workflow_preset_id = item.preset_id;
  if (!payload.workflow_run_id && item?.workflow_run_id) payload.workflow_run_id = item.workflow_run_id;
  if (!payload.source_image_ref && inputAssets.source_image_ref) payload.source_image_ref = inputAssets.source_image_ref;
  if (!payload.mask_image_ref && inputAssets.mask_image_ref) payload.mask_image_ref = inputAssets.mask_image_ref;
  const controlnet = payload.controlnet && typeof payload.controlnet === "object"
    ? { ...payload.controlnet }
    : (item?.controlnet && typeof item.controlnet === "object" ? { ...item.controlnet } : {});
  if (Object.keys(controlnet).length) {
    if (!controlnet.image_ref && inputAssets.control_image_ref) controlnet.image_ref = inputAssets.control_image_ref;
    payload.controlnet = controlnet;
  }
  return payload;
}

function comfyuiHistoryFavoriteParams(item = {}) {
  const params = comfyuiHistoryPayload(item);
  if (item?.history_source === "workflow") {
    params.workflow_preset_id = Number(item.preset_id || params.workflow_preset_id || 0);
    params.workflow_run_id = Number(item.workflow_run_id || params.workflow_run_id || 0);
    params.workflow_preset_title = params.workflow_preset_title || item.preset_title || "";
    params.workflow_system_bundle_id = params.workflow_system_bundle_id || item.workflow_system_bundle_id || "";
    const originalParams = item?.params && typeof item.params === "object" ? item.params : {};
    if (!originalParams.model && params.model && params.model === item.preset_title) {
      delete params.model;
    }
  }
  return params;
}

function comfyuiHistoryHasConcreteSeed(payload = {}) {
  const value = payload?.seed;
  if (value === undefined || value === null || value === "") return false;
  const seed = Number(value);
  return Number.isFinite(seed) && seed >= 0;
}

function comfyuiHistorySeedModeForApply(payload = {}, workflowPresetId = 0) {
  if (comfyuiHistoryHasConcreteSeed(payload)) return "fixed";
  const rawMode = String(payload?.seed_after_generate || payload?.seed_after_generate_mode || "").trim();
  return rawMode ? normalizeComfyuiSeedAfterGenerateMode(rawMode) : (workflowPresetId > 0 ? "random" : "fixed");
}

function ensureComfyuiHistoryWorkflowSelectOption(presetId, label = "") {
  const select = $("comfyui-template-select");
  if (!select || !presetId) return;
  const value = String(presetId);
  const exists = Array.from(select.options || []).some((option) => String(option.value || "") === value);
  if (!exists) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label || `Workflow #${value}`;
    option.dataset.historyValue = "1";
    select.appendChild(option);
  }
  select.value = value;
}

function comfyuiHistoryInputAssets(item = {}, payload = null) {
  const source = item?.input_assets && typeof item.input_assets === "object" ? { ...item.input_assets } : {};
  const mergedPayload = payload || comfyuiHistoryPayload(item);
  if (!source.source_image_ref && mergedPayload?.source_image_ref) source.source_image_ref = mergedPayload.source_image_ref;
  if (!source.mask_image_ref && mergedPayload?.mask_image_ref) source.mask_image_ref = mergedPayload.mask_image_ref;
  if (!source.control_image_ref && mergedPayload?.controlnet?.image_ref) source.control_image_ref = mergedPayload.controlnet.image_ref;
  return source;
}

async function applyComfyuiHistoryToForm(historyId) {
  const item = comfyuiHistoryItemById(historyId);
  if (!item) {
    setComfyuiHistoryActionMessage("找不到這筆 ComfyUI 歷史紀錄，請重新整理歷史。", false);
    return;
  }
  const displayId = comfyuiHistoryItemId(item) || historyId;
  const payload = comfyuiHistoryPayload(item);
  const workflowPresetId = item.history_source === "workflow" ? Number(item.preset_id || payload.workflow_preset_id || 0) : 0;
  const historyWorkflowJson = item?.params?.workflow_json && typeof item.params.workflow_json === "object" && !Array.isArray(item.params.workflow_json)
    ? item.params.workflow_json
    : item?.workflow_json && typeof item.workflow_json === "object" && !Array.isArray(item.workflow_json)
      ? item.workflow_json
      : (comfyuiSelectedTemplateDetail?.workflow_json && typeof comfyuiSelectedTemplateDetail.workflow_json === "object" && !Array.isArray(comfyuiSelectedTemplateDetail.workflow_json)
        ? comfyuiSelectedTemplateDetail.workflow_json
        : {});
  const hasHistoryWorkflowJson = !!(historyWorkflowJson && Object.keys(historyWorkflowJson).length);
  const shouldApplyHistoryWorkflowSnapshot = workflowPresetId > 0 && hasHistoryWorkflowJson;
  const controlnet = payload.controlnet || {};
  const loras = Array.isArray(payload.loras) ? payload.loras : [];
  const historySeedMode = comfyuiHistorySeedModeForApply(payload, workflowPresetId);
  if (workflowPresetId > 0) {
    try {
      const needsGgufProfiles = payload.gguf_profile || payload.gguf_variant || payload.diffusion_model || payload.diffusers_gguf_file
        || (item.workflow_json && typeof item.workflow_json === "object" && item.workflow_json["4"]);
      if (needsGgufProfiles && typeof ensureComfyuiGgufProfilesLoaded === "function") {
        await ensureComfyuiGgufProfilesLoaded();
      }
      if (typeof loadComfyuiWorkflowPresets === "function" && Array.isArray(comfyuiWorkflowPresets) && !comfyuiWorkflowPresets.length) {
        await loadComfyuiWorkflowPresets({ silentTemplateReload: true });
      }
      ensureComfyuiHistoryWorkflowSelectOption(workflowPresetId, item.preset_title || payload.workflow_preset_title || "");
      if (typeof loadComfyuiSelectedTemplateDetail === "function") {
        await loadComfyuiSelectedTemplateDetail(workflowPresetId, { silent: true, applyDefaults: false });
      }
      ensureComfyuiHistoryWorkflowSelectOption(workflowPresetId, item.preset_title || payload.workflow_preset_title || "");
    } catch (err) {
      setComfyuiHistoryActionMessage(err.message || "Workflow 模板讀取失敗，無法完整套回歷史。", false);
      throw err;
    }
  }
  comfyuiSelectedLoras = loras
    .filter((entry) => entry && typeof entry === "object" && normalizeComfyuiLoraName(entry.name))
    .slice(0, COMFYUI_MAX_LORAS)
    .map((entry) => ({
      name: normalizeComfyuiLoraName(entry.name),
      strength_model: Number.isFinite(Number(entry.strength_model)) ? Number(entry.strength_model) : 1,
      strength_clip: Number.isFinite(Number(entry.strength_clip)) ? Number(entry.strength_clip) : 1,
      template_node_id: entry.template_node_id ? String(entry.template_node_id) : "",
    }));
  renderComfyuiSelectedLoras();
  const historyFieldRestores = [
    ["comfyui-generation-mode", payload.generation_mode || item.generation_mode || "txt2img"],
    ["comfyui-model-select", payload.model || "", true],
    ["comfyui-diffusers-model-repo", payload.diffusers_model_repo || ""],
    ["comfyui-diffusers-model-variant", payload.diffusers_model_variant || "", true],
    ["comfyui-vae-select", payload.vae || COMFYUI_VAE_BUILTIN, true],
    ["comfyui-prompt", payload.prompt || ""],
    ["comfyui-negative-prompt", payload.negative_prompt || ""],
    ["comfyui-width", payload.width || comfyuiDefaultWidth],
    ["comfyui-height", payload.height || comfyuiDefaultHeight],
    ["comfyui-steps", payload.steps || 20],
    ["comfyui-cfg", payload.cfg || 7],
    ["comfyui-batch-size", payload.ui_batch_size || payload.batch_size || 1],
    ["comfyui-run-count", payload.run_count || 1],
    ["comfyui-seed", payload.seed ?? ""],
    ["comfyui-seed-after-generate", historySeedMode],
    ["comfyui-denoise-strength", payload.denoise_strength ?? 0.65],
    ["comfyui-upscale-model", payload.upscale_model || "", true],
    ["comfyui-controlnet-type", controlnet.type || "canny", true],
    ["comfyui-control-strength", controlnet.strength ?? 1],
    ["comfyui-control-start", controlnet.start_percent ?? 0],
    ["comfyui-control-end", controlnet.end_percent ?? 1],
    ["comfyui-outpaint-left", payload.outpaint?.left ?? 128],
    ["comfyui-outpaint-top", payload.outpaint?.top ?? 128],
    ["comfyui-outpaint-right", payload.outpaint?.right ?? 128],
    ["comfyui-outpaint-bottom", payload.outpaint?.bottom ?? 128],
    ["comfyui-outpaint-feathering", payload.outpaint?.feathering ?? 48],
  ];
  historyFieldRestores
    .filter(([id]) => !(workflowPresetId > 0 && id === "comfyui-model-select"))
    .forEach(([id, value, preserveMissingOption]) => setComfyuiFieldValue(id, value, { preserveMissingOption: !!preserveMissingOption }));
  setComfyuiSamplingFieldsFromValues(payload.sampler_name || "euler", payload.scheduler || "normal");
  if (shouldApplyHistoryWorkflowSnapshot && typeof applyComfyuiTemplateHistorySnapshotToForm === "function") {
    applyComfyuiTemplateHistorySnapshotToForm(item.workflow_json || {}, payload);
    if (!item.workflow_json && hasHistoryWorkflowJson) {
      applyComfyuiTemplateHistorySnapshotToForm(historyWorkflowJson, payload);
    }
    if (!payload.diffusers_model_repo && workflowPresetId) {
      setComfyuiFieldValue("comfyui-model-select", payload.model || "", { preserveMissingOption: true });
    }
  }
  const targetView = payload.diffusers_model_repo ? "hf" : "generate";
  setComfyuiView(targetView);
  const controlEnabled = !!(controlnet?.enabled || controlnet?.type);
  if ($("comfyui-controlnet-enabled")) $("comfyui-controlnet-enabled").checked = targetView !== "hf" && controlEnabled;
  setComfyuiSeedAfterGenerateMode(historySeedMode);
  fillComfyuiControlnetModelOptions();
  fillComfyuiControlnetPreprocessorOptions();
  setComfyuiFieldValue("comfyui-controlnet-model", controlnet.model_name || "", { preserveMissingOption: true });
  setComfyuiFieldValue("comfyui-controlnet-preprocessor", controlnet.preprocessor || "", { preserveMissingOption: true });
  if (shouldApplyHistoryWorkflowSnapshot && typeof applyComfyuiTemplateHistorySnapshotToForm === "function") {
    applyComfyuiTemplateHistorySnapshotToForm(item.workflow_json || {}, payload);
    if (!item.workflow_json && hasHistoryWorkflowJson) {
      applyComfyuiTemplateHistorySnapshotToForm(historyWorkflowJson, payload);
    }
  }
  await applyComfyuiHistoryAssets(comfyuiHistoryInputAssets(item, payload));
  updateComfyuiModeVisibility();
  writeComfyuiDraft();
  setComfyuiMessage(`已套回第 ${displayId} 筆 ComfyUI 歷史，可直接再調整後重跑。`, true);
}

async function deleteComfyuiHistory(historyId) {
  const targetId = String(historyId || "").trim();
  if (!targetId) {
    setComfyuiHistoryActionMessage("這筆 ComfyUI 歷史缺少可刪除 ID，請重新整理歷史。", false);
    return;
  }
  const historyItem = comfyuiHistoryItemById(targetId);
  const workflowRunId = historyItem?.history_source === "workflow"
    ? Number(historyItem?.workflow_run_id || 0)
    : (targetId.startsWith("workflow-") ? Number(targetId.slice("workflow-".length)) : 0);
  const legacyId = workflowRunId > 0 ? 0 : Number(targetId);
  if (workflowRunId <= 0 && (!Number.isFinite(legacyId) || legacyId <= 0)) {
    setComfyuiHistoryActionMessage("這筆 ComfyUI 歷史 ID 格式不正確，請重新整理歷史。", false);
    return;
  }
  const label = workflowRunId > 0 ? `workflow run #${workflowRunId}` : `第 ${legacyId} 筆 ComfyUI 歷史`;
  if (!window.confirm(`刪除${label}？只會移除本站歷史列表，不會刪除 ComfyUI 輸出檔。`)) return;
  await fetchCsrfToken({ force: true });
  setComfyuiHistoryActionMessage("正在刪除 ComfyUI 歷史紀錄...", true);
  const res = await apiFetch(workflowRunId > 0
    ? API + `/comfyui/workflow-runs/${encodeURIComponent(workflowRunId)}`
    : API + `/comfyui/history/${encodeURIComponent(legacyId)}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 歷史刪除失敗（HTTP ${res.status}）`);
  comfyuiHistoryItems = comfyuiHistoryItems.filter((item) => comfyuiHistoryItemId(item) !== targetId);
  renderComfyuiHistory();
  loadComfyuiHistory().catch((err) => setComfyuiHistoryActionMessage(err?.message || "ComfyUI 歷史重新整理失敗", false));
  setComfyuiHistoryActionMessage(`已刪除${label}。`, true);
}

async function favoriteComfyuiHistoryImage(historyId) {
  const targetId = String(historyId || "").trim();
  if (!targetId) {
    setComfyuiHistoryActionMessage("這筆 ComfyUI 歷史缺少可收藏 ID，請重新整理歷史。", false);
    return;
  }
  const item = comfyuiHistoryItemById(targetId);
  if (!item) {
    setComfyuiHistoryActionMessage("找不到這筆 ComfyUI 歷史紀錄，請重新整理歷史。", false);
    return;
  }
  const image = comfyuiHistoryFirstImage(item);
  if (!image?.image_ref?.filename) {
    setComfyuiHistoryActionMessage("這筆 ComfyUI 歷史沒有可收藏的圖片預覽。", false);
    return;
  }
  const button = document.querySelector(`[data-comfyui-history-favorite="${CSS.escape(targetId)}"]`);
  if (button) {
    button.disabled = true;
    button.textContent = "收藏中...";
  }
  try {
    await fetchCsrfToken({ force: true });
    const params = comfyuiHistoryFavoriteParams(item);
    const mode = comfyuiReadableModeLabel(params.generation_mode || item.generation_mode || "txt2img");
    const createdAt = String(item.created_at || "").replace("T", " ").slice(0, 16);
    const title = `歷史收藏 - ${mode}${createdAt ? ` ${createdAt}` : ""}`.slice(0, 120);
    const res = await apiFetch(API + "/comfyui/image-favorites", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || "",
      },
      body: JSON.stringify({
        source_type: item.history_source === "workflow" ? "workflow_history" : "history",
        title,
        image_ref: image.image_ref,
        prompt_id: image.prompt_id || item?.result?.prompt_id || "",
        selected_image_index: Number.isFinite(Number(image.batch_index)) ? Number(image.batch_index) : 0,
        output_label: image.output_label || "",
        params,
      }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 歷史收藏失敗（HTTP ${res.status}）`);
    setComfyuiHistoryActionMessage(json.msg || "已收藏這張歷史產圖", true);
    if ($("comfyui-view-favorites")?.classList.contains("active")) {
      await loadComfyuiImageFavorites();
    }
  } finally {
    if (button) {
      button.disabled = !comfyuiHistoryCanFavorite(item);
      button.textContent = "收藏";
    }
  }
}

async function rerunComfyuiHistory(historyId) {
  const targetId = String(historyId || "").trim();
  if (!targetId) {
    setComfyuiHistoryActionMessage("這筆 ComfyUI 歷史缺少可重跑 ID，請重新整理歷史。", false);
    return;
  }
  const historyItem = comfyuiHistoryItemById(targetId);
  setComfyuiView("generate");
  await fetchCsrfToken({ force: true });
  setComfyuiBusy(true);
  setComfyuiMessage("正在建立 ComfyUI 重跑工作...", true);
  startComfyuiProgress(COMFYUI_GENERATION_TIMEOUT_SECONDS);
  const controller = new AbortController();
  comfyuiGenerateAbortController = controller;
  try {
    const workflowRunId = historyItem?.history_source === "workflow" ? Number(historyItem?.workflow_run_id || 0) : 0;
    const res = await apiFetch(workflowRunId > 0
      ? API + `/comfyui/workflow-runs/${encodeURIComponent(workflowRunId)}/rerun`
      : API + `/comfyui/history/${encodeURIComponent(targetId)}/rerun`, {
      method: "POST",
      credentials: "same-origin",
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || ""
      },
      body: JSON.stringify({})
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 歷史重跑失敗（HTTP ${res.status}）`);
    const jobId = json.job?.job_id;
    const result = await pollComfyuiJobUntilDone(jobId, controller, COMFYUI_GENERATION_TIMEOUT_SECONDS);
    const rawImages = Array.isArray(result.images) && result.images.length ? result.images : [result.image].filter(Boolean);
    const images = await hydrateComfyuiGeneratedImages(rawImages);
    comfyuiGeneratedImages = images;
    comfyuiGeneratedMedia = await hydrateComfyuiGeneratedMedia(Array.isArray(result.media) ? result.media : [], jobId);
    comfyuiCurrentImage = images[0] || null;
    if (images.length) {
      renderComfyuiGeneratedImages(comfyuiGeneratedImages);
      setComfyuiSelectedImage(0);
    } else if (comfyuiGeneratedMedia.length) {
      renderComfyuiGeneratedMedia(comfyuiGeneratedMedia);
    } else {
      renderComfyuiGeneratedImages([]);
    }
    stopComfyuiProgress({ complete: true });
    updateComfyuiResultButtons(!!images.length);
    loadComfyuiHistory().catch((err) => setComfyuiHistoryActionMessage(err?.message || "ComfyUI 歷史重新整理失敗", false));
    setComfyuiMessage(historyItem?.history_source === "workflow"
      ? `已重跑 workflow run #${historyItem.workflow_run_id || targetId}。`
      : `已重跑第 ${targetId} 筆 ComfyUI 歷史。`, true);
  } catch (err) {
    stopComfyuiProgress({ error: err.message || "ComfyUI 歷史重跑失敗" });
    setComfyuiMessage(err.message || "ComfyUI 歷史重跑失敗", false);
  } finally {
    if (comfyuiGenerateAbortController === controller) comfyuiGenerateAbortController = null;
    setComfyuiBusy(false);
  }
}

// Workflow preset/editor block moved to 36-comfyui-workflows.js

function comfyuiSaveRequestPayload() {
  return {
    image_ref: comfyuiCurrentImage?.image_ref,
    virtual_path: $("comfyui-save-path")?.value || "",
    album_id: selectedComfyuiAlbumId()
  };
}

function setComfyuiSelectedImage(index) {
  const nextIndex = Math.max(0, Math.min(Number(index) || 0, comfyuiGeneratedImages.length - 1));
  comfyuiSelectedImageIndex = nextIndex;
  comfyuiCurrentImage = comfyuiGeneratedImages[nextIndex] || null;
  comfyuiSavedResult = null;
  const meta = $("comfyui-result-meta");
  if (meta && comfyuiCurrentImage) {
    const total = comfyuiGeneratedImages.length;
    const batchLabel = total > 1 ? ` · 第 ${nextIndex + 1}/${total} 張` : "";
    const outputLabel = comfyuiGeneratedImageLabel(comfyuiCurrentImage, nextIndex);
    meta.textContent = `${outputLabel ? `${outputLabel} · ` : ""}model=${comfyuiCurrentImage.model || "-"} · seed=${comfyuiCurrentImage.seed ?? "-"}${batchLabel} · ${formatDriveBytes(comfyuiCurrentImage.size_bytes || 0)}`;
  }
  const savePath = $("comfyui-save-path");
  if (savePath && comfyuiCurrentImage?.image_ref?.filename && (!savePath.value.trim() || savePath.dataset.comfyuiAutoPath === "1")) {
    savePath.value = `/output/${comfyuiCurrentImage.image_ref.filename}`;
    savePath.dataset.comfyuiAutoPath = "1";
    writeComfyuiDraft();
  }
  updateComfyuiResultButtons(!!comfyuiCurrentImage?.image_ref);
}

function comfyuiGeneratedImageLabel(image, index = 0) {
  const label = String(image?.output_label || image?.compare_label || "").trim();
  const model = String(image?.model || "").trim();
  const modelName = model ? model.replace(/\\/g, "/").split("/").pop() : "";
  if (label) {
    const labelHasModel = modelName && label.includes(modelName);
    const labelLooksSpecific = /比較|checkpoint|ckpt|模型|\.safetensors|\.ckpt/i.test(label);
    return modelName && !labelHasModel && !labelLooksSpecific ? `${label} · 模型：${modelName}` : label;
  }
  if (model) {
    const prefix = Array.isArray(comfyuiGeneratedImages) && comfyuiGeneratedImages.length > 1 ? `第 ${Number(index) + 1} 張 · ` : "";
    return `${prefix}模型：${modelName}`;
  }
  return "";
}

function comfyuiGeneratedMediaMarkup(mediaItems = []) {
  const items = Array.isArray(mediaItems) ? mediaItems.filter(Boolean) : [];
  if (!items.length) return "";
  return `
    <div class="comfyui-generated-media">
      ${items.map((item) => {
        const kind = comfyuiMediaItemKind(item);
        const src = item.preview_url || item.data_url || "";
        const filename = item.file_ref?.filename || "output";
        const player = !src
          ? `<div class="drive-empty">${sanitize(item.preview_error || "媒體檔已完成，正在讀取預覽。")}</div>`
          : (kind === "audio"
            ? `<audio controls preload="metadata"><source src="${sanitize(src)}" type="${sanitize(item.mime_type || "audio/mpeg")}"></audio>`
            : (kind === "video" ? `<video controls preload="metadata" playsinline><source src="${sanitize(src)}" type="${sanitize(item.mime_type || "video/mp4")}"></video>` : (kind === "image" ? `<img src="${sanitize(src)}" alt="${sanitize(filename)}" />` : `<a class="btn btn-sm" href="${sanitize(src)}" download="${sanitize(filename)}">開啟輸出檔</a>`)));
        return `
          <div class="comfyui-generated-media-card">
            ${player}
            <div class="drive-card-sub">${sanitize(kind.toUpperCase())} · ${sanitize(filename)} · ${formatDriveBytes(item.size_bytes || 0)}</div>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderComfyuiGeneratedMedia(mediaItems = []) {
  const preview = $("comfyui-preview");
  if (!preview) return;
  const items = Array.isArray(mediaItems) ? mediaItems.filter(Boolean) : [];
  const outputKinds = comfyuiOutputKindsFromItems([], items);
  updateComfyuiPreviewCardForOutputKinds(outputKinds.length ? outputKinds : null);
  if (!items.length) {
    preview.innerHTML = `<div class="drive-empty">${sanitize(comfyuiPreviewEmptyText())}</div>`;
    return;
  }
  preview.innerHTML = comfyuiGeneratedMediaMarkup(items);
}

function openComfyuiGeneratedImage(index = comfyuiSelectedImageIndex) {
  const images = Array.isArray(comfyuiGeneratedImages) && comfyuiGeneratedImages.length
    ? comfyuiGeneratedImages
    : [comfyuiCurrentImage].filter(Boolean);
  const safeIndex = Math.max(0, Math.min(Number(index) || 0, images.length - 1));
  const image = images[safeIndex] || comfyuiCurrentImage;
  if (!image?.data_url) {
    setComfyuiMessage("圖片預覽尚未載入完成", false);
    return;
  }
  let overlay = $("comfyui-image-lightbox");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "comfyui-image-lightbox";
    overlay.className = "comfyui-image-lightbox";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-hidden", "true");
    overlay.innerHTML = `
      <div class="comfyui-image-lightbox-bar">
        <div>
          <strong id="comfyui-image-lightbox-title">ComfyUI 圖片</strong>
          <div class="drive-card-sub" id="comfyui-image-lightbox-meta"></div>
        </div>
        <button class="btn btn-sm" type="button" data-comfyui-lightbox-close="1">關閉</button>
      </div>
      <button class="comfyui-lightbox-nav prev" type="button" data-comfyui-lightbox-prev="1" aria-label="上一張">‹</button>
      <div class="comfyui-image-lightbox-body" data-comfyui-lightbox-close="1">
        <img id="comfyui-image-lightbox-img" alt="ComfyUI generated image enlarged" />
      </div>
      <button class="comfyui-lightbox-nav next" type="button" data-comfyui-lightbox-next="1" aria-label="下一張">›</button>
    `;
    document.body.appendChild(overlay);
    overlay.querySelectorAll("[data-comfyui-lightbox-close]").forEach((el) => {
      el.addEventListener("click", (event) => {
        if (event.target === el) closeComfyuiGeneratedImageLightbox();
      });
    });
    overlay.querySelector("[data-comfyui-lightbox-prev]")?.addEventListener("click", () => {
      openComfyuiGeneratedImage((Number(overlay.dataset.index) || 0) - 1);
    });
    overlay.querySelector("[data-comfyui-lightbox-next]")?.addEventListener("click", () => {
      openComfyuiGeneratedImage((Number(overlay.dataset.index) || 0) + 1);
    });
  }
  overlay.dataset.index = String(safeIndex);
  const img = overlay.querySelector("#comfyui-image-lightbox-img");
  const title = overlay.querySelector("#comfyui-image-lightbox-title");
  const meta = overlay.querySelector("#comfyui-image-lightbox-meta");
  const label = comfyuiGeneratedImageLabel(image, safeIndex) || `第 ${safeIndex + 1} 張`;
  if (img) img.src = image.data_url;
  if (title) title.textContent = label;
  if (meta) meta.textContent = `${safeIndex + 1} / ${Math.max(images.length, 1)}${image.size_bytes ? ` · ${formatDriveBytes(image.size_bytes)}` : ""}`;
  overlay.querySelectorAll(".comfyui-lightbox-nav").forEach((button) => {
    button.hidden = images.length <= 1;
  });
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  overlay.querySelector("[data-comfyui-lightbox-close]")?.focus();
}

function closeComfyuiGeneratedImageLightbox() {
  const overlay = $("comfyui-image-lightbox");
  if (!overlay) return;
  overlay.classList.remove("show");
  overlay.setAttribute("aria-hidden", "true");
  const img = overlay.querySelector("#comfyui-image-lightbox-img");
  if (img) img.removeAttribute("src");
  document.body.classList.remove("modal-open");
}

document.addEventListener("keydown", (event) => {
  const overlay = $("comfyui-image-lightbox");
  if (!overlay?.classList.contains("show")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeComfyuiGeneratedImageLightbox();
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    openComfyuiGeneratedImage((Number(overlay.dataset.index) || 0) - 1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    openComfyuiGeneratedImage((Number(overlay.dataset.index) || 0) + 1);
  }
});

function renderComfyuiGeneratedImages(images) {
  const preview = $("comfyui-preview");
  if (!preview) return;
  if (!Array.isArray(images) || !images.length) {
    if (Array.isArray(comfyuiGeneratedMedia) && comfyuiGeneratedMedia.length) {
      renderComfyuiGeneratedMedia(comfyuiGeneratedMedia);
      return;
    }
    updateComfyuiPreviewCardForOutputKinds();
    preview.innerHTML = `<div class="drive-empty">${sanitize(comfyuiPreviewEmptyText())}</div>`;
    return;
  }
  const relatedMedia = Array.isArray(comfyuiGeneratedMedia) ? comfyuiGeneratedMedia : [];
  updateComfyuiPreviewCardForOutputKinds(comfyuiOutputKindsFromItems(images, relatedMedia));
  if (images.length === 1) {
    if (!images[0].data_url) {
      preview.innerHTML = `<div class="drive-empty">圖片已完成，正在讀取預覽。</div>`;
      return;
    }
    const singleLabel = comfyuiGeneratedImageLabel(images[0], 0);
    const mediaMarkup = comfyuiGeneratedMediaMarkup(relatedMedia);
    preview.innerHTML = `
      ${singleLabel ? `<div class="comfyui-output-label">${sanitize(singleLabel)}</div>` : ""}
      <button class="comfyui-output-main" type="button" data-comfyui-open-image="0" title="開啟大圖">
        <img loading="lazy" src="${sanitize(images[0].data_url || "")}" alt="ComfyUI generated image" />
      </button>
      ${mediaMarkup}
    `;
    preview.querySelector("[data-comfyui-open-image]")?.addEventListener("click", () => openComfyuiGeneratedImage(0));
    return;
  }
  const selectedIndex = Math.max(0, Math.min(comfyuiSelectedImageIndex || 0, images.length - 1));
  const selected = images[selectedIndex] || images[0];
  const selectedLabel = comfyuiGeneratedImageLabel(selected, selectedIndex);
  preview.innerHTML = `
    <div class="comfyui-output-gallery">
      ${selectedLabel ? `<div class="comfyui-output-label">${sanitize(selectedLabel)}</div>` : ""}
      <button class="comfyui-output-main" type="button" data-comfyui-open-image="${selectedIndex}" title="開啟第 ${selectedIndex + 1} 張大圖">
        ${selected?.data_url
          ? `<img loading="lazy" src="${sanitize(selected.data_url || "")}" alt="ComfyUI generated image ${selectedIndex + 1}" />`
          : `<span class="drive-empty">圖片已完成，正在讀取預覽。</span>`}
      </button>
      <div class="comfyui-batch-grid is-strip">
        ${images.map((image, index) => `
          <button class="comfyui-batch-item${index === selectedIndex ? " active" : ""}" type="button" data-comfyui-image-index="${index}" title="選擇第 ${index + 1} 張">
            ${image.data_url
              ? `<img loading="lazy" src="${sanitize(image.data_url || "")}" alt="ComfyUI generated image ${index + 1}" />`
              : `<span class="drive-empty">讀取預覽中</span>`}
            <span>${sanitize(comfyuiGeneratedImageLabel(image, index) || `第 ${index + 1} 張`)}</span>
          </button>
        `).join("")}
      </div>
      ${comfyuiGeneratedMediaMarkup(relatedMedia)}
    </div>
  `;
  preview.querySelector("[data-comfyui-open-image]")?.addEventListener("click", () => {
    openComfyuiGeneratedImage(selectedIndex);
  });
  preview.querySelectorAll("[data-comfyui-image-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextIndex = parseInt(button.getAttribute("data-comfyui-image-index"), 10);
      setComfyuiSelectedImage(nextIndex);
      renderComfyuiGeneratedImages(comfyuiGeneratedImages);
    });
  });
}

function clearComfyuiModelsCache() {
  comfyuiModelsLastLoadedAt = 0;
  comfyuiModelsLastStatusText = "";
}

async function loadComfyuiModels(options = {}) {
  const forceRefresh = options === true || !!(options && options.forceRefresh);
  if (!currentUser || !canAccessModule("comfyui")) return;
  const status = $("comfyui-status");
  if (!forceRefresh && comfyuiModelsLoaded && Date.now() - comfyuiModelsLastLoadedAt < COMFYUI_MODELS_CACHE_MS) {
    if (status) status.textContent = isComfyuiDiffusersMode() ? comfyuiDiffusersStatusText() : comfyuiModelsLastStatusText;
    return true;
  }
  if (!forceRefresh && comfyuiModelsLoadPromise) return comfyuiModelsLoadPromise;
  if (status) status.textContent = isComfyuiDiffusersMode() ? "檢查 HF / Diffusers 設定中..." : "連線 ComfyUI 中...";
  setComfyuiMessage("");
  comfyuiModelsLoadPromise = (async () => {
    await fetchCsrfToken();
    const requestPath = "/comfyui/models" + (forceRefresh
      ? (comfyuiRequestQuery() ? `${comfyuiRequestQuery()}&refresh=1` : "?refresh=1")
      : comfyuiRequestQuery());
    const res = await apiFetch(API + requestPath, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" }
    });
    const json = await res.json().catch(() => ({}));
    syncComfyuiPrimaryConnectionModeFromResponse(json);
    if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 連線失敗（HTTP ${res.status}）`);
    syncComfyuiPrimaryConnectionModeFromResponse(json);
    comfyuiAvailableCheckpoints = normalizeComfyuiModelOptionValues(json.models || []);
    comfyuiAvailableDiffusionModels = normalizeComfyuiModelOptionValues(json.diffusion_models || []);
    comfyuiAvailableClipModels = normalizeComfyuiModelOptionValues(json.clip_models || []);
    comfyuiAvailableClipVisionModels = normalizeComfyuiModelOptionValues(json.clip_vision_models || []);
    comfyuiAvailableLatentUpscaleModels = normalizeComfyuiModelOptionValues(json.latent_upscale_models || []);
    comfyuiAvailableControlnetModels = normalizeComfyuiModelOptionValues(json.controlnet_models || []);
    fillComfyuiSelect("comfyui-model-select", comfyuiAvailableCheckpoints, "");
    comfyuiLoraDetails = json.lora_details && typeof json.lora_details === "object" ? json.lora_details : {};
    fillComfyuiLoraSelect(json.loras || []);
    fillComfyuiVaeSelect(json.vaes || []);
    renderComfyuiEmbeddingShortcuts(json.embeddings || []);
    fillComfyuiSelect("comfyui-sampler", json.samplers || [], "euler");
    fillComfyuiSelect("comfyui-scheduler", json.schedulers || [], "normal");
    fillComfyuiGenerationModes(json.generation_modes || []);
    renderComfyuiModelFamilyHints(json.model_families || []);
    fillComfyuiGgufProfiles(json.gguf_profiles || []);
    comfyuiInstalledGgufModels = Array.isArray(json.installed_gguf_models) ? json.installed_gguf_models : [];
    renderComfyuiInstalledGgufModels(comfyuiInstalledGgufModels);
    fillComfyuiControlnetTypes(json.controlnet_types || {});
    fillComfyuiUpscaleModels(json.upscale_models || []);
    fillComfyuiControlnetModelOptions();
    fillComfyuiControlnetPreprocessorOptions();
    restoreComfyuiDraft();
    pruneUnsupportedComfyuiSelectedLoras({ notify: false });
    updateComfyuiModeVisibility();
    updateComfyuiDiffusersUi();
    applyComfyuiRuntimeLimits(json);
    comfyuiModelsLoaded = true;
    loadComfyuiAlbums({ force: true }).catch((err) => setComfyuiMessage(err?.message || "相簿讀取失敗", false));
    loadComfyuiHistory().catch((err) => setComfyuiHistoryActionMessage(err?.message || "ComfyUI 歷史讀取失敗", false));
    loadComfyuiWorkflowPresets().catch((err) => {
      if (typeof setComfyuiWorkflowStatus === "function") {
        setComfyuiWorkflowStatus(err?.message || "Workflow preset 讀取失敗");
      } else {
        setComfyuiMessage(err?.message || "Workflow preset 讀取失敗", false);
      }
    });
    setComfyuiTabAvailability(true);
    comfyuiModelsLastLoadedAt = Date.now();
    const activeStatusText = comfyuiModelsStatusText(json);
    if (!isComfyuiDiffusersMode()) comfyuiModelsLastStatusText = activeStatusText;
    if (status) status.textContent = activeStatusText;
    return true;
  })();
  try {
    return await comfyuiModelsLoadPromise;
  } catch (err) {
    comfyuiModelsLoaded = false;
    clearComfyuiModelsCache();
    comfyuiLoraDetails = {};
    setComfyuiTabAvailability(false, err.message || "ComfyUI 伺服器未連線");
    if (status) status.textContent = "ComfyUI 未連線";
    const effectiveMode = comfyuiEffectiveConnectionMode();
    const startHint = effectiveMode === "local"
      ? "。若使用本地模式，請先按「啟動 ComfyUI」。"
      : (effectiveMode === "diffusers" ? "。若使用 Diffusers 模式，請確認 root 已設定 Hugging Face repo 且後端已安裝 diffusers / torch。" : "");
    setComfyuiMessage((err.message || "ComfyUI 模型讀取失敗") + startHint, false);
    return false;
  } finally {
    comfyuiModelsLoadPromise = null;
  }
}

async function refreshComfyuiStatus({ switchAway = true } = {}) {
  if (!currentUser || !canAccessModule("comfyui")) {
    setComfyuiTabAvailability(null);
    return false;
  }
  const status = $("comfyui-status");
  if (status) status.textContent = "檢測 ComfyUI 伺服器中...";
  try {
    await fetchCsrfToken();
    const res = await apiFetch(API + "/comfyui/status" + comfyuiRequestQuery(), {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" }
    });
    const json = await res.json().catch(() => ({}));
    syncComfyuiPrimaryConnectionModeFromResponse(json);
    if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 狀態檢測失敗（HTTP ${res.status}）`);
    syncComfyuiPrimaryConnectionModeFromResponse(json);
    const available = !!json.available;
    const starting = !!json.starting;
    comfyuiLocalRuntimeActive = comfyuiEffectiveConnectionMode() === "local" && (available || starting || !!json.local_runtime);
    applyComfyuiRuntimeLimits(json);
    const detail = available
      ? `已偵測 ${json.comfyui_url || "ComfyUI"}${comfyuiBackendLabel(json)}${comfyuiPaidApiStatusText(json)}${comfyuiStorageWarningText(json)}`
      : (json.msg || `找不到 ${json.comfyui_url || "ComfyUI"} 伺服器`);
    setComfyuiTabAvailability(available, detail);
    if (available) stopComfyuiLocalStartPolling();
    else if (starting && comfyuiEffectiveConnectionMode() === "local") scheduleComfyuiLocalStartPolling();
    else stopComfyuiLocalStartPolling();
    if (status) status.textContent = detail;
    if (!available) {
      comfyuiModelsLoaded = false;
      setComfyuiBusy(false);
    }
    return available;
  } catch (err) {
    const message = err.message || "ComfyUI 伺服器未連線";
    comfyuiLocalRuntimeActive = false;
    setComfyuiTabAvailability(false, message);
    comfyuiModelsLoaded = false;
    setComfyuiBusy(false);
    return false;
  }
}

async function startLocalComfyui() {
  const start = $("comfyui-start-btn");
  const status = $("comfyui-status");
  let keepIdleSuspend = false;
  if (start) {
    start.disabled = true;
    start.textContent = "啟動中...";
  }
  setComfyuiIdleSuspend("comfyui_start_local", true, "ComfyUI 啟動中");
  setComfyuiMessage("正在送出本地 ComfyUI 啟動請求。第一次安裝依賴可能需要數分鐘。", true);
  if (status) status.textContent = "正在啟動本地 ComfyUI...";
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/comfyui/start", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || ""
      },
      body: JSON.stringify({})
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 啟動失敗（HTTP ${res.status}）`);
    comfyuiConnectionMode = json.connection_mode || comfyuiConnectionMode || "local";
    const info = json.start || {};
    setComfyuiMessage(json.msg || "已送出 ComfyUI 啟動請求。", true);
    if (status) status.textContent = info.already_running ? "ComfyUI 已在執行中" : "已送出啟動請求，正在重新檢查連線...";
    if (info.started && info.available === false) {
      comfyuiServerAvailable = false;
      comfyuiLocalRuntimeActive = true;
      setComfyuiTabAvailability(false, info.message || "ComfyUI 正在背景啟動中");
      if (status) status.textContent = info.message || "ComfyUI 正在背景啟動中，稍後請按重新整理模型";
      keepIdleSuspend = true;
      scheduleComfyuiLocalStartPolling();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await loadComfyuiModels({ forceRefresh: true });
  } catch (err) {
    setComfyuiMessage(err.message || "ComfyUI 啟動失敗", false);
    if (status) status.textContent = "ComfyUI 啟動失敗";
  } finally {
    if (!keepIdleSuspend) setComfyuiIdleSuspend("comfyui_start_local", false, "ComfyUI 啟動中");
    if (start) {
      start.disabled = false;
      start.textContent = "啟動 ComfyUI";
    }
    updateComfyuiStartButton();
  }
}

async function stopLocalComfyui() {
  const stop = $("comfyui-stop-btn");
  const status = $("comfyui-status");
  if (currentUser !== "root") {
    setComfyuiMessage("只有 root 可以停止本地 ComfyUI。", false);
    return;
  }
  if (stop) {
    stop.disabled = true;
    stop.textContent = "停止中...";
  }
  setComfyuiMessage("正在停止本地 ComfyUI...", true);
  if (status) status.textContent = "正在停止本地 ComfyUI...";
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/root/comfyui/stop", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || ""
      },
      body: JSON.stringify({})
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `ComfyUI 停止失敗（HTTP ${res.status}）`);
    comfyuiConnectionMode = json.connection_mode || comfyuiConnectionMode || "local";
    comfyuiModelsLoaded = false;
    comfyuiLocalRuntimeActive = false;
    setComfyuiTabAvailability(false, json.msg || "ComfyUI 已停止");
    if (status) status.textContent = json.msg || "ComfyUI 已停止";
    setComfyuiMessage(json.msg || "已停止本地 ComfyUI。", true);
    stopComfyuiLocalStartPolling();
  } catch (err) {
    setComfyuiMessage(err.message || "ComfyUI 停止失敗", false);
    if (status) status.textContent = "ComfyUI 停止失敗";
  } finally {
    if (stop) {
      stop.disabled = false;
      stop.textContent = "停止 ComfyUI";
    }
    updateComfyuiStartButton();
  }
}

function comfyuiNumberValue(id, fallback) {
  const raw = $(id)?.value;
  if (raw === "" || raw === null || raw === undefined) return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

function normalizeComfyuiSeedForUi(value) {
  if (value === "" || value === null || value === undefined) return null;
  const seed = Math.trunc(Number(value));
  if (!Number.isFinite(seed)) return null;
  return Math.max(0, Math.min(COMFYUI_UI_SEED_MAX, seed));
}

function randomComfyuiSeedForUi() {
  if (globalThis.crypto?.getRandomValues) {
    const values = new Uint32Array(1);
    globalThis.crypto.getRandomValues(values);
    return values[0];
  }
  return Math.floor(Math.random() * (COMFYUI_RANDOM_SEED_MAX + 1));
}

function normalizeComfyuiSeedAfterGenerateMode(value) {
  const mode = String(value || "").trim().toLowerCase();
  return ["random", "fixed", "increment", "decrement"].includes(mode) ? mode : "fixed";
}

function comfyuiSeedAfterGenerateMode() {
  const templateSelect = document.querySelector("[data-comfyui-template-seed-after-generate]");
  return normalizeComfyuiSeedAfterGenerateMode(templateSelect?.value || $("comfyui-seed-after-generate")?.value || "fixed");
}

function setComfyuiSeedAfterGenerateMode(value) {
  const mode = normalizeComfyuiSeedAfterGenerateMode(value);
  const globalSelect = $("comfyui-seed-after-generate");
  if (globalSelect) globalSelect.value = mode;
  document.querySelectorAll("[data-comfyui-template-seed-after-generate]").forEach((select) => {
    select.value = mode;
  });
  writeComfyuiDraft();
}

function applyComfyuiSeedAfterGenerate(completedSeed = null) {
  const seedInput = $("comfyui-seed");
  if (!seedInput) return;
  const mode = comfyuiSeedAfterGenerateMode();
  if (mode === "fixed") return;
  let nextSeed = null;
  if (mode === "random") {
    nextSeed = randomComfyuiSeedForUi();
  } else {
    const baseSeed = normalizeComfyuiSeedForUi(completedSeed)
      ?? normalizeComfyuiSeedForUi(typeof currentSelectedComfyuiTemplateSeedValue === "function" ? currentSelectedComfyuiTemplateSeedValue() : null)
      ?? normalizeComfyuiSeedForUi(seedInput.value)
      ?? normalizeComfyuiSeedForUi(comfyuiCurrentImage?.seed)
      ?? randomComfyuiSeedForUi();
    if (mode === "increment") nextSeed = Math.min(COMFYUI_UI_SEED_MAX, baseSeed + 1);
    else if (mode === "decrement") nextSeed = Math.max(0, baseSeed - 1);
  }
  if (nextSeed === null) return;
  seedInput.value = String(nextSeed);
  if (typeof updateSelectedComfyuiTemplateSeedFields === "function") {
    updateSelectedComfyuiTemplateSeedFields(nextSeed);
  }
  writeComfyuiDraft();
  if (typeof queueRenderSelectedComfyuiTemplate === "function") queueRenderSelectedComfyuiTemplate();
}

function comfyuiPayload() {
  const mode = comfyuiGenerationMode();
  const sampling = comfyuiCurrentSamplingFieldsForPayload();
  const diffusersMode = isComfyuiDiffusersMode();
  const vae = diffusersMode ? "" : $("comfyui-vae-select")?.value || COMFYUI_VAE_BUILTIN;
  const diffusersRepo = isComfyuiDiffusersMode()
    ? sanitizeComfyuiDiffusersRepoField({ strict: true, quiet: true })
    : normalizeComfyuiHuggingFaceRepoInput($("comfyui-diffusers-model-repo")?.value || "");
  const diffusersVariant = $("comfyui-diffusers-model-variant")?.value || "";
  const batchSize = Math.max(1, Math.min(comfyuiMaxBatchSize, comfyuiNumberValue("comfyui-batch-size", 1)));
  const payload = {
    generation_mode: mode,
    model: diffusersMode && diffusersRepo ? diffusersRepo : ($("comfyui-model-select")?.value || ""),
    diffusers_model_repo: diffusersMode ? diffusersRepo : "",
    diffusers_model_variant: diffusersMode ? diffusersVariant : "",
    diffusers_gguf_file: "",
    diffusers_gguf_base_repo: "",
    diffusers_gguf_profile: "",
    diffusers_gguf_variant: "",
    seed_after_generate: comfyuiSeedAfterGenerateMode(),
    run_count: comfyuiRunCount(),
    prompt: $("comfyui-prompt")?.value || "",
    negative_prompt: $("comfyui-negative-prompt")?.value || "",
    width: comfyuiNumberValue("comfyui-width", comfyuiDefaultWidth),
    height: comfyuiNumberValue("comfyui-height", comfyuiDefaultHeight),
    steps: comfyuiNumberValue("comfyui-steps", 20),
    cfg: comfyuiNumberValue("comfyui-cfg", 7),
    batch_size: batchSize,
    ui_batch_size: batchSize,
    seed: $("comfyui-seed")?.value ? comfyuiNumberValue("comfyui-seed", 0) : undefined,
    sampler_name: sampling.sampler_name,
    scheduler: sampling.scheduler,
    vae: vae === COMFYUI_VAE_BUILTIN ? "" : vae,
    loras: diffusersMode ? [] : comfyuiSelectedLoras.slice(0, COMFYUI_MAX_LORAS),
    denoise_strength: comfyuiNumberValue("comfyui-denoise-strength", 0.65),
    filename_prefix: "hackme_web",
  };
  const includeImageAssets = !(diffusersMode && comfyuiDiffusersTextOnlyMode());
  if (includeImageAssets && comfyuiInputAssets.source?.imageRef) payload.source_image_ref = comfyuiInputAssets.source.imageRef;
  if (includeImageAssets && comfyuiInputAssets.mask?.imageRef) payload.mask_image_ref = comfyuiInputAssets.mask.imageRef;
  if (!diffusersMode && isComfyuiControlnetEnabled()) {
    payload.controlnet = {
      type: comfyuiSelectedControlnetType(),
      model_name: $("comfyui-controlnet-model")?.value || "",
      preprocessor: $("comfyui-controlnet-preprocessor")?.value || "",
      strength: comfyuiNumberValue("comfyui-control-strength", 1),
      start_percent: comfyuiNumberValue("comfyui-control-start", 0),
      end_percent: comfyuiNumberValue("comfyui-control-end", 1),
    };
    if (comfyuiInputAssets.control?.imageRef) payload.controlnet.image_ref = comfyuiInputAssets.control.imageRef;
  }
  if (mode === "outpaint") {
    payload.outpaint = {
      left: Math.max(0, Math.floor(comfyuiNumberValue("comfyui-outpaint-left", 128))),
      top: Math.max(0, Math.floor(comfyuiNumberValue("comfyui-outpaint-top", 128))),
      right: Math.max(0, Math.floor(comfyuiNumberValue("comfyui-outpaint-right", 128))),
      bottom: Math.max(0, Math.floor(comfyuiNumberValue("comfyui-outpaint-bottom", 128))),
      feathering: Math.max(0, Math.floor(comfyuiNumberValue("comfyui-outpaint-feathering", 48))),
    };
  }
  if (mode === "upscale") {
    payload.upscale_model = $("comfyui-upscale-model")?.value || "";
  }
  return payload;
}

function comfyuiValidatePayloadForUi(payload) {
  const mode = String(payload?.generation_mode || "").trim().toLowerCase();
  if (isComfyuiDiffusersMode()) {
    if (!["txt2img", "img2img", "inpaint"].includes(mode)) {
      return "Hugging Face Diffusers 模式目前支援文字生圖、圖生圖與局部重繪；影片、語音、放大與 workflow 模板請切回 ComfyUI 後端。";
    }
    if (!String(payload?.diffusers_model_repo || payload?.model || "").trim()) {
      return "請輸入 Hugging Face repo，例如 dhead/waiIllustriousSDXL_v150 或 Heartsync/NSFW-Uncensored；不要填權重檔名。";
    }
    const repo = normalizeComfyuiHuggingFaceRepoInput(payload.diffusers_model_repo || payload.model || "");
    if (comfyuiDiffusersInspectionMatches(repo, mode)) {
      const inspection = comfyuiDiffusersInspection?.data || {};
      if (!inspection.supported_for_mode) {
        return `這個 Hugging Face repo 不支援「${comfyuiReadableModeLabel(mode)}」，尚未開始下載。`;
      }
      const variants = Array.isArray(inspection.variant_options) ? inspection.variant_options.filter((option) => option?.kind !== "gguf") : [];
      if (variants.length > 1 && !String(payload.diffusers_model_variant || "").trim()) {
        return "這個 Hugging Face repo 有多個 Diffusers 精度版本，請先選擇要下載/載入的版本。";
      }
    }
  }
  if (comfyuiModeRequiresWorkflowTemplate(mode)) {
    return `「${comfyuiReadableModeLabel(mode)}」需要使用 ComfyUI workflow 模板執行；請在上方 Workflow 模板選擇支援的大模型工作流。`;
  }
  const needsSource = comfyuiModeUsesSourceImage(mode);
  const needsMask = comfyuiModeUsesMaskImage(mode);
  if (needsSource && !comfyuiInputAssets.source?.file && !payload.source_image_ref) {
    return `「${comfyuiReadableModeLabel(mode)}」需要來源圖片。`;
  }
  if (needsMask && !comfyuiInputAssets.mask?.file && !payload.mask_image_ref) {
    return "局部重繪需要遮罩圖片。";
  }
  if (isComfyuiControlnetEnabled() && !comfyuiInputAssets.control?.file && !payload.controlnet?.image_ref) {
    return "啟用 ControlNet 時需要控制圖。";
  }
  if (mode === "upscale" && !String(payload.upscale_model || "").trim()) {
    return "放大修復需要選擇 scale model。";
  }
  return "";
}

function comfyuiBuildGenerateRequest(payload) {
  const allowImageAssets = !(isComfyuiDiffusersMode() && comfyuiDiffusersTextOnlyMode());
  const useMultipart = allowImageAssets && COMFYUI_IMAGE_ASSET_KEYS.some((key) => comfyuiInputAssets[key]?.file);
  if (!useMultipart) {
    return {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    };
  }
  const form = new FormData();
  const appendScalar = (key, value) => {
    if (value === undefined || value === null || value === "") return;
    form.append(key, String(value));
  };
  appendScalar("generation_mode", payload.generation_mode);
  appendScalar("model", payload.model);
  appendScalar("diffusers_model_repo", payload.diffusers_model_repo);
  appendScalar("diffusers_model_variant", payload.diffusers_model_variant);
  appendScalar("diffusers_gguf_file", payload.diffusers_gguf_file);
  appendScalar("diffusers_gguf_base_repo", payload.diffusers_gguf_base_repo);
  appendScalar("diffusers_gguf_profile", payload.diffusers_gguf_profile);
  appendScalar("diffusers_gguf_variant", payload.diffusers_gguf_variant);
  appendScalar("seed_after_generate", payload.seed_after_generate);
  appendScalar("run_count", payload.run_count);
  appendScalar("ui_batch_size", payload.ui_batch_size);
  appendScalar("prompt", payload.prompt);
  appendScalar("negative_prompt", payload.negative_prompt);
  appendScalar("width", payload.width);
  appendScalar("height", payload.height);
  appendScalar("steps", payload.steps);
  appendScalar("cfg", payload.cfg);
  appendScalar("batch_size", payload.batch_size);
  appendScalar("seed", payload.seed);
  appendScalar("sampler_name", payload.sampler_name);
  appendScalar("scheduler", payload.scheduler);
  appendScalar("vae", payload.vae);
  appendScalar("denoise_strength", payload.denoise_strength);
  appendScalar("upscale_model", payload.upscale_model);
  appendScalar("filename_prefix", payload.filename_prefix);
  if (payload.source_image_ref) form.append("source_image_ref", JSON.stringify(payload.source_image_ref));
  if (payload.mask_image_ref) form.append("mask_image_ref", JSON.stringify(payload.mask_image_ref));
  if (Array.isArray(payload.loras) && payload.loras.length) form.append("loras_json", JSON.stringify(payload.loras));
  if (payload.controlnet) {
    appendScalar("controlnet_enabled", true);
    appendScalar("controlnet_type", payload.controlnet.type);
    appendScalar("controlnet_model", payload.controlnet.model_name || "");
    appendScalar("controlnet_preprocessor", payload.controlnet.preprocessor || "");
    appendScalar("control_strength", payload.controlnet.strength);
    appendScalar("control_start", payload.controlnet.start_percent);
    appendScalar("control_end", payload.controlnet.end_percent);
    if (payload.controlnet.image_ref) form.append("control_image_ref", JSON.stringify(payload.controlnet.image_ref));
  }
  if (payload.outpaint) {
    appendScalar("outpaint_left", payload.outpaint.left);
    appendScalar("outpaint_top", payload.outpaint.top);
    appendScalar("outpaint_right", payload.outpaint.right);
    appendScalar("outpaint_bottom", payload.outpaint.bottom);
    appendScalar("outpaint_feathering", payload.outpaint.feathering);
  }
  if (allowImageAssets && comfyuiInputAssets.source?.file) form.append("source_image", comfyuiInputAssets.source.file, comfyuiInputAssets.source.file.name || "source.png");
  if (allowImageAssets && comfyuiInputAssets.mask?.file) form.append("mask_image", comfyuiInputAssets.mask.file, comfyuiInputAssets.mask.file.name || "mask.png");
  if (allowImageAssets && comfyuiInputAssets.control?.file) form.append("control_image", comfyuiInputAssets.control.file, comfyuiInputAssets.control.file.name || "control.png");
  return { headers: {}, body: form };
}

function comfyuiRunCount() {
  return Math.max(1, Math.min(10, Math.floor(comfyuiNumberValue("comfyui-run-count", 1))));
}

function comfyuiShareGenerationPayload() {
  const payload = comfyuiPayload();
  const selectedTemplateId = Number(comfyuiSelectedTemplatePresetId || $("comfyui-template-select")?.value || 0);
  if (!isComfyuiDiffusersMode() && selectedTemplateId > 0) {
    payload.workflow_preset_id = selectedTemplateId;
    payload.workflow_preset_title = comfyuiSelectedTemplateDetail?.title || "";
    payload.workflow_system_bundle_id = comfyuiSelectedTemplateDetail?.system_bundle_id || "";
    if (comfyuiSelectedTemplateDetail?.workflow_json && typeof comfyuiSelectedTemplateDetail.workflow_json === "object") {
      payload.workflow_json = comfyuiSelectedTemplateDetail.workflow_json;
    }
    if (typeof comfyuiTemplateSdxlSkipRefiner !== "undefined") {
      payload.skip_refiner = !!comfyuiTemplateSdxlSkipRefiner;
    }
  }
  if (comfyuiCurrentImage && comfyuiCurrentImage.seed !== undefined && comfyuiCurrentImage.seed !== null) {
    payload.seed = comfyuiCurrentImage.seed;
  }
  if (comfyuiCurrentImage?.model) {
    payload.model = comfyuiCurrentImage.model;
  }
  const outputLabel = comfyuiGeneratedImageLabel(comfyuiCurrentImage, comfyuiSelectedImageIndex);
  if (outputLabel) payload.output_label = outputLabel;
  return payload;
}

function confirmComfyuiBilling(payload) {
  if (currentUser === "root") return { confirmed: true, required: false };
  if (!comfyuiBillingQuote?.unit_price) return { confirmed: true, required: false };
  const unitPrice = Number(comfyuiBillingQuote.unit_price || 0);
  const batchSize = Math.max(1, Math.min(comfyuiMaxBatchSize, Number(payload?.batch_size || 1)));
  const runCount = comfyuiRunCount();
  const totalImages = batchSize * runCount;
  const loraCount = Array.isArray(payload?.loras) ? payload.loras.length : 0;
  const loraUnitPrice = Number(comfyuiBillingQuote.lora_extra_unit_price || COMFYUI_LORA_EXTRA_PRICE);
  const loraExtra = loraCount * loraUnitPrice * totalImages;
  const totalPrice = unitPrice * totalImages + loraExtra;
  const loraText = loraCount > 0 ? `\nLoRA 加價：${loraCount} 個 x ${loraUnitPrice} 點 x ${totalImages} 張 = ${loraExtra} 點。` : "";
  const confirmed = window.confirm(
    `本次成功產圖最多將扣 ${totalPrice} 點（基礎 ${unitPrice} 點 x ${batchSize} 張 x ${runCount} 次）。${loraText}\n` +
    "產圖失敗不扣點；丟棄預覽不退款。\n\n是否確認送出？"
  );
  return { confirmed, required: true, totalPrice, unitPrice, batchSize, runCount, totalImages, loraCount, loraExtra };
}

async function preflightComfyuiBilling(payload, runCount, billingConfirmation) {
  if (!billingConfirmation?.required) return null;
  const res = await apiFetch(API + "/comfyui/billing-quote", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || ""
    },
    body: JSON.stringify({
      ...payload,
      run_count: runCount,
      ...comfyuiRequestPayloadExtras()
    })
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    throw new Error(json.msg || `ComfyUI 扣點預檢失敗（HTTP ${res.status}）`);
  }
  return json.billing || null;
}

async function generateComfyuiImage() {
  if (comfyuiServerAvailable === false) {
    setComfyuiMessage("正在重新檢查 ComfyUI 連線...", true);
    const available = await refreshComfyuiStatus({ switchAway: false });
    if (!available) {
      const effectiveMode = comfyuiEffectiveConnectionMode();
      const hint = effectiveMode === "local"
        ? "請先按「啟動 ComfyUI」，或確認已有其他使用者啟動服務。"
        : (effectiveMode === "diffusers"
          ? "Diffusers 後端尚未就緒，請確認 Hugging Face repo、token 與 Python 套件。"
          : "ComfyUI 伺服器未連線，無法產圖。");
      setComfyuiMessage(hint, false);
      resetComfyuiIdleUi();
      return;
    }
  }
  if (!comfyuiModelsLoaded) {
    setComfyuiMessage("正在載入 ComfyUI 模型清單...", true);
    await loadComfyuiModels({ forceRefresh: true });
  }
  if (!comfyuiModelsLoaded) {
    setComfyuiMessage("ComfyUI 模型尚未載入，請按「重新整理模型」查看詳細錯誤。", false);
    resetComfyuiIdleUi();
    return;
  }
  let payload = comfyuiPayload();
  if (isComfyuiDiffusersMode()) {
    const repo = normalizeComfyuiHuggingFaceRepoInput(payload.diffusers_model_repo || payload.model || "");
    if (repo && !comfyuiDiffusersInspectionMatches(repo, payload.generation_mode)) {
      const inspection = await inspectComfyuiDiffusersRepo({ quiet: false });
      if (!inspection) return;
      payload = comfyuiPayload();
    }
  }
  const selectedTemplateId = Number(comfyuiSelectedTemplatePresetId || $("comfyui-template-select")?.value || 0);
  if (!isComfyuiDiffusersMode() && selectedTemplateId) {
    if (typeof runSelectedComfyuiWorkflowTemplateFromGenerate !== "function") {
      setComfyuiMessage("已選擇 Workflow 模板，但模板執行模組尚未載入。請重新整理頁面後再試。", false);
      return;
    }
    try {
      await runSelectedComfyuiWorkflowTemplateFromGenerate(payload.generation_mode);
    } catch (err) {
      setComfyuiMessage(err.message || "Workflow 模板執行失敗", false);
    }
    return;
  }
  const validationMessage = comfyuiValidatePayloadForUi(payload);
  if (comfyuiModeRequiresWorkflowTemplate(payload.generation_mode)) {
    if (typeof runSelectedComfyuiWorkflowTemplateFromGenerate !== "function") {
      setComfyuiMessage(`「${comfyuiReadableModeLabel(payload.generation_mode)}」需要使用 Workflow 模板執行，但模板模組尚未載入。請重新整理頁面後再試。`, false);
      return;
    }
    try {
      await runSelectedComfyuiWorkflowTemplateFromGenerate(payload.generation_mode);
    } catch (err) {
      setComfyuiMessage(err.message || "Workflow 模板執行失敗", false);
    }
    return;
  }
  if (validationMessage) {
    setComfyuiMessage(validationMessage, false);
    return;
  }
  const runCount = comfyuiRunCount();
  const billingConfirmation = confirmComfyuiBilling(payload);
  if (!billingConfirmation.confirmed) {
    setComfyuiMessage("已取消產圖扣點確認", false);
    return;
  }
  updateComfyuiPreviewCardForOutputKinds(["image"]);
  const preview = $("comfyui-preview");
  const meta = $("comfyui-result-meta");
  if (preview) preview.innerHTML = `<div class="drive-empty">${sanitize(comfyuiPreviewPendingText(["image"]))}</div>`;
  if (meta) meta.textContent = "";
  comfyuiCurrentImage = null;
  comfyuiGeneratedImages = [];
  comfyuiGeneratedMedia = [];
  comfyuiSelectedImageIndex = 0;
  comfyuiSavedResult = null;
  updateComfyuiResultButtons(false);
  setComfyuiBusy(true);
  setComfyuiMessage("");
  const controller = new AbortController();
  comfyuiGenerateAbortController = controller;
  try {
    await fetchCsrfToken({ force: true });
    const preflightBilling = await preflightComfyuiBilling(payload, runCount, billingConfirmation);
    if (preflightBilling) {
      comfyuiBillingQuote = { ...(comfyuiBillingQuote || {}), ...preflightBilling };
    }
    const generateRequest = comfyuiBuildGenerateRequest(payload);
    startComfyuiProgress(COMFYUI_GENERATION_TIMEOUT_SECONDS * runCount);
    let totalCharged = 0;
    const generated = [];
    const generatedMedia = [];
    const requestedBatchSize = Math.max(1, Math.min(comfyuiMaxBatchSize, Number(payload.batch_size || 1)));
    const totalRequests = runCount * requestedBatchSize;
    for (let requestIndex = 0; requestIndex < totalRequests; requestIndex += 1) {
      if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
      const runIndex = Math.floor(requestIndex / requestedBatchSize);
      const batchIndex = requestIndex % requestedBatchSize;
      setComfyuiMessage(`正在產生第 ${requestIndex + 1} / ${totalRequests} 張圖片...`, true);
      const startRes = await apiFetch(API + "/comfyui/generate", {
        method: "POST",
        credentials: "same-origin",
        signal: controller.signal,
        headers: {
          "X-CSRF-Token": getCsrfToken() || "",
          ...(generateRequest.headers || {})
        },
        body: (() => {
          if (generateRequest.body instanceof FormData) {
            const body = new FormData();
            generateRequest.body.forEach((value, key) => body.append(key, value));
            body.append("batch_size", "1");
            body.append("async_progress", "true");
            body.append("confirm_billing", billingConfirmation.required ? "true" : "false");
            body.append("timeout_seconds", String(COMFYUI_GENERATION_TIMEOUT_SECONDS));
            Object.entries(comfyuiRequestPayloadExtras() || {}).forEach(([key, value]) => {
              if (value !== undefined && value !== null && value !== "") body.append(key, String(value));
            });
            return body;
          }
          return JSON.stringify({
            ...payload,
            batch_size: 1,
            async_progress: true,
            confirm_billing: billingConfirmation.required,
            timeout_seconds: COMFYUI_GENERATION_TIMEOUT_SECONDS,
            ...comfyuiRequestPayloadExtras()
          });
        })()
      });
      const startJson = await startRes.json().catch(() => ({}));
      if (!startRes.ok || !startJson.ok) throw new Error(startJson.msg || `第 ${requestIndex + 1} 張產圖失敗（HTTP ${startRes.status}）`);
      const jobId = startJson.job?.job_id;
      if (!jobId) throw new Error("ComfyUI 未回傳工作編號");
      const json = await pollComfyuiJobUntilDone(jobId, controller, COMFYUI_GENERATION_TIMEOUT_SECONDS);
      const rawRunImages = Array.isArray(json.images) && json.images.length ? json.images : [json.image].filter(Boolean);
      const runImages = await hydrateComfyuiGeneratedImages(rawRunImages);
      const runMedia = await hydrateComfyuiGeneratedMedia(Array.isArray(json.media) ? json.media : [], jobId);
      runMedia.forEach((item) => {
        generatedMedia.push({ ...item, run_index: runIndex, batch_index: batchIndex, run_count: runCount });
      });
      runImages.forEach((image) => {
        generated.push({ ...image, run_index: runIndex, batch_index: batchIndex, run_count: runCount });
      });
      comfyuiGeneratedImages = generated.slice();
      comfyuiGeneratedMedia = generatedMedia.slice();
      if (comfyuiGeneratedImages.length) {
        comfyuiSelectedImageIndex = Math.max(0, comfyuiGeneratedImages.length - 1);
        comfyuiCurrentImage = comfyuiGeneratedImages[comfyuiSelectedImageIndex];
        renderComfyuiGeneratedImages(comfyuiGeneratedImages);
        setComfyuiSelectedImage(comfyuiSelectedImageIndex);
        updateComfyuiResultButtons(true);
      } else if (comfyuiGeneratedMedia.length) {
        renderComfyuiGeneratedMedia(comfyuiGeneratedMedia);
        updateComfyuiResultButtons(false);
      }
      if (json.billing?.charged) totalCharged += Number(json.billing.total_price || 0);
    }
    comfyuiGeneratedImages = generated;
    comfyuiGeneratedMedia = generatedMedia;
    comfyuiCurrentImage = comfyuiGeneratedImages[0] || null;
    if (!comfyuiCurrentImage?.image_ref) {
      if (Array.isArray(comfyuiGeneratedMedia) && comfyuiGeneratedMedia.length) {
        renderComfyuiGeneratedMedia(comfyuiGeneratedMedia);
        stopComfyuiProgress({ complete: true });
        updateComfyuiResultButtons(false);
        setComfyuiMessage(`已執行 ${runCount} 次，共產生 ${comfyuiGeneratedMedia.length} 個媒體檔。`, true);
        return;
      }
      throw new Error("ComfyUI 未回傳圖片");
    }
    renderComfyuiGeneratedImages(comfyuiGeneratedImages);
    setComfyuiSelectedImage(0);
    stopComfyuiProgress({ complete: true });
    updateComfyuiResultButtons(true);
    applyComfyuiSeedAfterGenerate(generated[generated.length - 1]?.seed);
    loadComfyuiHistory().catch((err) => setComfyuiHistoryActionMessage(err?.message || "ComfyUI 歷史重新整理失敗", false));
    const billingText = totalCharged > 0
      ? `已扣 ${totalCharged} 點。`
      : "";
    setComfyuiMessage(`已執行 ${runCount} 次，共產生 ${comfyuiGeneratedImages.length} 張圖片；${billingText}請選擇要儲存或分享的圖片。`, true);
  } catch (err) {
    const interrupted = err?.name === "AbortError";
    const message = interrupted ? "已中斷產圖" : (err.message || "產圖失敗");
    if (preview) preview.innerHTML = `<div class="drive-empty">${sanitize(message)}</div>`;
    stopComfyuiProgress({ error: message });
    setComfyuiMessage(message, interrupted);
  } finally {
    comfyuiActiveJobId = null;
    if (comfyuiGenerateAbortController === controller) comfyuiGenerateAbortController = null;
    setComfyuiBusy(false);
  }
}

async function interruptComfyuiGeneration() {
  if (!comfyuiGenerateAbortController) {
    setComfyuiMessage("目前沒有進行中的產圖可中斷", false);
    return;
  }
  const interruptedJobId = comfyuiActiveJobId || "";
  const interruptBtn = $("comfyui-interrupt-btn");
  if (interruptBtn) {
    interruptBtn.disabled = true;
    interruptBtn.textContent = "中斷中...";
  }
  setComfyuiProgress({
    visible: true,
    running: false,
    percent: 100,
    label: "正在中斷產圖",
    detail: interruptedJobId
      ? `已停止前端等待 job ${interruptedJobId}，後端會在不影響其他使用者時才送出 ComfyUI 中斷`
      : "已停止前端等待，後端會在不影響其他使用者時才送出 ComfyUI 中斷"
  });
  comfyuiGenerateAbortController.abort();
  const interruptRequestController = new AbortController();
  let interruptTimeoutId = null;
  const interruptTimeout = new Promise((_, reject) => {
    interruptTimeoutId = window.setTimeout(() => {
      interruptRequestController.abort();
      const err = new Error("ComfyUI 中斷請求逾時");
      err.name = "AbortError";
      reject(err);
    }, COMFYUI_INTERRUPT_TIMEOUT_SECONDS * 1000);
  });
  const interruptRequest = (async () => {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/comfyui/interrupt", {
      method: "POST",
      credentials: "same-origin",
      signal: interruptRequestController.signal,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || ""
      },
      body: JSON.stringify({
        job_id: interruptedJobId,
        ...comfyuiRequestPayloadExtras()
      })
    });
    const json = await res.json().catch(() => ({}));
    return { res, json };
  })();
  interruptRequest.catch(() => {});
  try {
    const { res, json } = await Promise.race([interruptRequest, interruptTimeout]);
    if (!res.ok || !json.ok) throw new Error(json.msg || `中斷產圖失敗（HTTP ${res.status}）`);
    setComfyuiMessage(json.msg || "已送出中斷產圖請求", true);
  } catch (err) {
    const timedOut = err?.name === "AbortError";
    const suffix = interruptedJobId ? `（job id：${interruptedJobId}）` : "";
    setComfyuiMessage(
      timedOut
        ? `已停止等待中斷回應${suffix}；後端可能仍在收尾，稍後可從歷史紀錄查看結果。`
        : (err.message || "中斷產圖失敗"),
      timedOut
    );
  } finally {
    if (interruptTimeoutId) window.clearTimeout(interruptTimeoutId);
    if (interruptBtn) {
      interruptBtn.textContent = "中斷產圖";
    }
  }
}

async function saveComfyuiImageToDrive() {
  if (!comfyuiCurrentImage?.image_ref) {
    setComfyuiMessage("目前沒有可儲存的產圖結果", false);
    return;
  }
  const saveBtn = $("comfyui-save-btn");
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = "儲存中...";
  }
  setComfyuiMessage("");
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/comfyui/save", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || ""
      },
      body: JSON.stringify(comfyuiSaveRequestPayload())
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `儲存失敗（HTTP ${res.status}）`);
    comfyuiSavedResult = json;
    const albumText = json.album ? "，並加入相簿" : "";
    setComfyuiMessage(`已存到雲端硬碟${albumText}：${json.storage_file?.virtual_path || json.file?.file_id || ""}`, true);
    if (typeof loadDriveDashboard === "function") await loadDriveDashboard();
    await loadComfyuiAlbums({ force: true });
  } catch (err) {
    setComfyuiMessage(err.message || "儲存失敗", false);
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = "存到雲端硬碟";
    }
  }
}

async function discardComfyuiImage() {
  const imagesToDiscard = comfyuiGeneratedImages.length ? comfyuiGeneratedImages : [comfyuiCurrentImage].filter(Boolean);
  if (imagesToDiscard.some((image) => image?.image_ref)) {
    const discardBtn = $("comfyui-discard-btn");
    if (discardBtn) {
      discardBtn.disabled = true;
      discardBtn.textContent = "刪除中...";
    }
    try {
      await fetchCsrfToken({ force: true });
      for (const image of imagesToDiscard) {
        if (!image?.image_ref) continue;
        const res = await apiFetch(API + "/comfyui/discard", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": getCsrfToken() || ""
          },
          body: JSON.stringify({
            image_ref: image.image_ref,
            prompt_id: image.prompt_id || ""
          })
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || !json.ok) throw new Error(json.msg || `刪除 ComfyUI 原始檔失敗（HTTP ${res.status}）`);
        if (json.warning === "source_file_not_deleted") {
          setComfyuiMessage(json.msg || "已丟棄預覽；ComfyUI 原始檔未刪除。", false);
        }
      }
    } catch (err) {
      if (discardBtn) {
        discardBtn.disabled = false;
        discardBtn.textContent = "丟棄預覽";
      }
      setComfyuiMessage(err.message || "刪除 ComfyUI 原始檔失敗", false);
      return;
    }
  }
  comfyuiCurrentImage = null;
  comfyuiGeneratedImages = [];
  comfyuiGeneratedMedia = [];
  comfyuiSelectedImageIndex = 0;
  comfyuiSavedResult = null;
  const preview = $("comfyui-preview");
  const meta = $("comfyui-result-meta");
  if (preview) preview.innerHTML = `<div class="drive-empty">已丟棄這次預覽</div>`;
  if (meta) meta.textContent = "";
  stopComfyuiProgress();
  updateComfyuiResultButtons(false);
  const discardBtn = $("comfyui-discard-btn");
  if (discardBtn) discardBtn.textContent = "丟棄預覽";
  if (!$("comfyui-msg")?.textContent) {
    setComfyuiMessage("已丟棄預覽，ComfyUI 原始檔也已刪除。", true);
  }
}

async function shareComfyuiToCommunity() {
  if (!comfyuiCurrentImage?.image_ref && !comfyuiSavedResult?.file?.file_id) {
    setComfyuiMessage("目前沒有可分享的產圖結果", false);
    return;
  }
  const shareMeta = promptComfyuiShareMetadata();
  if (!shareMeta) return;
  const shareBtn = $("comfyui-share-btn");
  if (shareBtn) {
    shareBtn.disabled = true;
    shareBtn.textContent = "分享中...";
  }
  setComfyuiMessage("");
  try {
    await fetchCsrfToken({ force: true });
    const savedFile = comfyuiSavedResult?.file || {};
    const savedStorageFile = comfyuiSavedResult?.storage_file || {};
    const res = await apiFetch(API + "/comfyui/share", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || ""
      },
      body: JSON.stringify({
        ...comfyuiSaveRequestPayload(),
        file_id: savedFile.file_id || "",
        storage_file_id: savedStorageFile.id || "",
        title: shareMeta.title,
        note: shareMeta.note,
        generation: comfyuiShareGenerationPayload()
      })
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `分享失敗（HTTP ${res.status}）`);
    comfyuiSavedResult = {
      file: json.file,
      storage_file: json.storage_file,
      album: json.album
    };
    setComfyuiMessage(`${json.msg || "已分享到 ComfyUI 專區"}：${json.thread?.title || ""}`, true);
    if (json.thread?.board_id && typeof switchModuleTab === "function") {
      if (typeof loadCommunityBoards === "function") loadCommunityBoards().catch(() => {});
    }
    if (typeof loadDriveDashboard === "function") await loadDriveDashboard();
    await loadComfyuiAlbums({ force: true });
  } catch (err) {
    setComfyuiMessage(err.message || "分享失敗", false);
  } finally {
    if (shareBtn) {
      shareBtn.disabled = !comfyuiCurrentImage?.image_ref;
      shareBtn.textContent = "分享到 ComfyUI 專區";
    }
  }
}

function promptComfyuiShareMetadata() {
  const outputLabel = comfyuiGeneratedImageLabel(comfyuiCurrentImage, comfyuiSelectedImageIndex);
  const defaultTitle = outputLabel ? `ComfyUI 產圖分享 - ${outputLabel}`.slice(0, 120) : "ComfyUI 產圖分享";
  const title = window.prompt("分享標題", defaultTitle);
  if (title === null) return null;
  const cleanTitle = String(title || "").trim().slice(0, 120) || defaultTitle;
  const note = window.prompt("心得留言（可留空）", "");
  if (note === null) return null;
  return {
    title: cleanTitle,
    note: String(note || "").trim().slice(0, 900),
  };
}

function setComfyuiFavoritesStatus(text) {
  const status = $("comfyui-favorites-status");
  if (status) status.textContent = text || "";
}

function setComfyuiFavoriteModalMode(mode = "civitai") {
  const normalized = mode === "upload" ? "upload" : "civitai";
  document.querySelectorAll("[data-comfyui-favorite-mode]").forEach((button) => {
    const active = (button.getAttribute("data-comfyui-favorite-mode") || "") === normalized;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-comfyui-favorite-panel]").forEach((panel) => {
    panel.hidden = (panel.getAttribute("data-comfyui-favorite-panel") || "") !== normalized;
  });
}

function openComfyuiFavoriteModal(mode = "civitai") {
  setComfyuiFavoriteModalMode(mode);
  const modal = $("comfyui-favorite-modal");
  if (!modal) return;
  modal.hidden = false;
  updateComfyuiFavoriteModalBodyState();
  const focusTarget = mode === "upload" ? $("comfyui-favorite-upload-file") : $("comfyui-favorite-civitai-url");
  window.setTimeout(() => focusTarget?.focus?.(), 0);
}

function updateComfyuiFavoriteModalBodyState() {
  const favoriteModal = $("comfyui-favorite-modal");
  const previewModal = $("comfyui-favorite-preview-modal");
  const favoriteModalOpen = !!favoriteModal && !favoriteModal.hidden;
  const previewModalOpen = !!previewModal && !previewModal.hidden;
  document.body.classList.toggle("modal-open", !!(favoriteModalOpen || previewModalOpen));
}

function closeComfyuiFavoriteModal() {
  const modal = $("comfyui-favorite-modal");
  if (modal) modal.hidden = true;
  updateComfyuiFavoriteModalBodyState();
}

function comfyuiFavoriteParamSummary(params = {}) {
  const normalized = comfyuiNormalizeFavoriteParamsForRestore(params);
  const values = [];
  const model = params.model || params.diffusers_model_repo || "";
  if (model) values.push(`模型：${model}`);
  else if (params.source_model_name) values.push(`來源模型：${params.source_model_name}`);
  if (params.workflow_preset_title || params.workflow_system_bundle_id) {
    values.push(`Workflow：${params.workflow_preset_title || params.workflow_system_bundle_id}`);
  }
  if (params.vae) values.push(`VAE：${params.vae}`);
  if (normalized.width || normalized.height) values.push(`尺寸：${normalized.width || "?"}x${normalized.height || "?"}`);
  if (params.steps) values.push(`Steps：${params.steps}`);
  if (normalized.cfg !== null) values.push(`CFG：${normalized.cfg}`);
  if (params.seed !== undefined && params.seed !== null && String(params.seed)) values.push(`Seed：${params.seed}`);
  if (params.sampler_name) values.push(`Sampler：${params.sampler_name}`);
  if (params.scheduler) values.push(`Scheduler：${params.scheduler}`);
  if (params.skip_refiner === true || params.skip_refiner === "true" || params.skip_refiner === 1 || params.skip_refiner === "1") {
    values.push("跳過 Refiner");
  }
  return values.slice(0, 8);
}

function comfyuiNormalizeFavoriteValueForRestore(value) {
  if (value === undefined || value === null || value === "") return null;
  const num = Number(String(value).trim().replace(/,/g, ""));
  return Number.isFinite(num) ? num : null;
}

function comfyuiParseFavoriteSizeText(value = "") {
  const match = String(value || "").match(/(\d{2,5})\s*[xX×*]\s*(\d{2,5})/);
  if (!match) return [null, null];
  const width = Number(match[1]);
  const height = Number(match[2]);
  return [Number.isFinite(width) ? width : null, Number.isFinite(height) ? height : null];
}

function comfyuiNormalizeFavoriteParamsForRestore(params = {}) {
  const candidates = (keys = []) => {
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(params, key) && params[key] !== undefined && params[key] !== null && `${params[key]}`.trim() !== "") return params[key];
    }
    return "";
  };
  const fromWidth = candidates(["width", "image_width", "size_width", "img_width", "resolution_width", "output_width"]);
  const fromHeight = candidates(["height", "image_height", "size_height", "img_height", "resolution_height", "output_height"]);
  const sizeText = candidates(["size", "image_size", "Size", "image_size_text", "resolution"]);
  const parsedFromSize = comfyuiParseFavoriteSizeText(sizeText);
  const width = comfyuiNormalizeFavoriteValueForRestore(fromWidth)
    ?? (parsedFromSize[0] != null ? parsedFromSize[0] : null);
  const height = comfyuiNormalizeFavoriteValueForRestore(fromHeight)
    ?? (parsedFromSize[1] != null ? parsedFromSize[1] : null);
  const cfg = comfyuiNormalizeFavoriteValueForRestore(
    candidates(["cfg", "cfg_scale", "cfgScale", "cfgscale", "cfg-scale", "CFG", "CFGScale", "CFG scale"])
  );
  return { width, height, cfg };
}

function comfyuiFavoritePromptText(params = {}, key = "prompt", limit = 900) {
  const text = String(params?.[key] || "").trim();
  if (!text) return "";
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
}

function comfyuiFavoriteRequirementKind(value = "") {
  const raw = String(value || "").trim().toLowerCase();
  if (raw.includes("vae")) return "VAE";
  if (raw.includes("lora")) return "LoRA";
  if (raw.includes("textual") || raw.includes("embedding") || raw.includes("embed")) return "Embedding";
  if (raw.includes("checkpoint") || raw.includes("model") || raw.includes("base")) return "Checkpoint";
  return "Model";
}

function comfyuiFavoriteRequirementTokens(value = "") {
  const raw = String(value || "").trim();
  if (!raw) return [];
  const noQuery = raw.split(/[?#]/, 1)[0];
  const base = noQuery.split(/[\\/]/).pop() || noQuery;
  const noExt = base.replace(/\.(?:safetensors|ckpt|pt|pth|bin|gguf)$/i, "");
  return [raw, noQuery, base, noExt]
    .map((item) => String(item || "").trim().toLowerCase())
    .filter(Boolean);
}

function comfyuiFavoriteRequirementExists(item = {}) {
  const type = String(item.type || "").toLowerCase();
  const pools = [];
  if (type === "checkpoint" || type === "model") pools.push(comfyuiAvailableCheckpoints);
  if (type === "lora") pools.push(comfyuiAvailableLoras);
  if (type === "embedding") pools.push(comfyuiAvailableEmbeddings);
  if (type === "vae") pools.push(comfyuiAvailableVaes);
  const requiredTokens = new Set([
    ...comfyuiFavoriteRequirementTokens(item.label),
    ...comfyuiFavoriteRequirementTokens(item.version),
  ]);
  if (!requiredTokens.size || !pools.length) return false;
  const availableTokens = new Set();
  pools.flat().forEach((value) => comfyuiFavoriteRequirementTokens(value).forEach((token) => availableTokens.add(token)));
  return [...requiredTokens].some((token) => availableTokens.has(token));
}

function comfyuiFavoriteRequirementItems(params = {}) {
  const civitai = params?.civitai && typeof params.civitai === "object" ? params.civitai : {};
  const items = [];
  const seen = new Map();
  const pushItem = ({ kind = "", name = "", version = "", file = "", url = "" }) => {
    const label = String(file || name || version || "").trim();
    if (!label) return;
    const type = comfyuiFavoriteRequirementKind(kind || file || name);
    const href = String(url || "").trim();
    const key = `${type}|${label}`;
    const normalized = { type, label, version: String(version || "").trim(), url: href };
    if (seen.has(key)) {
      const existing = seen.get(key);
      if (!existing.url && href) existing.url = href;
      return;
    }
    seen.set(key, normalized);
    items.push(normalized);
  };
  if (params.model || params.diffusers_model_repo) {
    pushItem({
      kind: "checkpoint",
      name: params.model || params.diffusers_model_repo,
      url: params.diffusers_model_repo ? `https://huggingface.co/${params.diffusers_model_repo}` : "",
    });
  }
  if (Array.isArray(params.loras)) {
    params.loras.forEach((item) => pushItem({
      kind: "lora",
      name: item?.name || item?.lora_name || "",
    }));
  }
  if (Array.isArray(params.embeddings)) {
    params.embeddings.forEach((item) => {
      if (item && typeof item === "object") {
        pushItem({
          kind: "embedding",
          name: item.name || item.embedding_name || item.file || "",
          url: item.url || item.download_url || "",
        });
      } else {
        pushItem({ kind: "embedding", name: item });
      }
    });
  }
  if (params.vae && params.vae !== COMFYUI_VAE_BUILTIN) {
    pushItem({
      kind: "vae",
      name: params.vae,
    });
  }
  (Array.isArray(civitai.model_version_resources) ? civitai.model_version_resources : []).forEach((resource) => {
    const primary = resource?.primary_file && typeof resource.primary_file === "object" ? resource.primary_file : {};
    const versionId = resource?.model_version_id || resource?.id || "";
    pushItem({
      kind: resource?.model_type || primary?.type || "",
      name: resource?.model_name || "",
      version: resource?.version_name || "",
      file: primary?.name || "",
      url: primary?.download_url || resource?.download_url || (versionId ? `https://civitai.com/api/download/models/${encodeURIComponent(versionId)}` : ""),
    });
  });
  (Array.isArray(civitai.resources) ? civitai.resources : []).forEach((resource) => {
    const versionId = resource?.modelVersionId || resource?.model_version_id || resource?.id || "";
    const kind = resource?.type || resource?.modelType || resource?.model_type || "";
    const kindKey = String(kind || "").trim().toLowerCase();
    const file = resource?.fileName || resource?.filename || "";
    const directUrl = resource?.downloadUrl || resource?.download_url || resource?.url || "";
    if (["model", "checkpoint", "base_model"].includes(kindKey) && !versionId && !file && !directUrl) {
      return;
    }
    pushItem({
      kind,
      name: resource?.name || resource?.modelName || "",
      version: resource?.versionName || resource?.modelVersionName || "",
      file,
      url: directUrl || (versionId ? `https://civitai.com/api/download/models/${encodeURIComponent(versionId)}` : ""),
    });
  });
  return items.slice(0, 24);
}

function comfyuiFavoriteRequirementsMarkup(params = {}) {
  const requirements = comfyuiFavoriteRequirementItems(params);
  if (!requirements.length) return "";
  return `
    <section class="comfyui-favorite-preview-section comfyui-favorite-requirements">
      <h4>模型需求</h4>
      <div class="comfyui-favorite-requirement-list">
        ${requirements.map((item) => {
          const exists = comfyuiFavoriteRequirementExists(item);
          const statusLabel = exists ? "已存在" : "未找到";
          return `
          <div class="comfyui-favorite-requirement-item ${exists ? "exists" : "missing"}" data-model-exists="${exists ? "1" : "0"}">
            <div class="comfyui-favorite-requirement-main">
              <span class="comfyui-favorite-requirement-status" aria-label="${statusLabel}" title="${statusLabel}">${exists ? "✓" : "×"}</span>
              <span class="comfyui-favorite-requirement-type">${sanitize(item.type)}</span>
              ${item.url
                ? `<a href="${sanitize(item.url)}" target="_blank" rel="noopener noreferrer">${sanitize(item.label)}</a>`
                : `<strong>${sanitize(item.label)}</strong>`}
            </div>
            ${item.version ? `<small>${sanitize(item.version)}</small>` : ""}
          </div>
        `;
        }).join("")}
      </div>
    </section>
  `;
}

async function copyComfyuiFavoritePromptText(text, button, label = "提示詞") {
  const value = String(text || "").trim();
  if (!value) {
    setComfyuiMessage(`沒有可複製的${label}。`, false);
    return;
  }
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else if (typeof showCopyFallbackDialog === "function") {
      await showCopyFallbackDialog(value, `複製${label}`);
    } else {
      window.prompt(`請手動複製${label}`, value);
    }
    if (typeof showCopyLinkFeedback === "function") {
      showCopyLinkFeedback(button || document.activeElement, "已完成複製", true);
    }
    setComfyuiMessage(`${label}已複製。`, true);
  } catch (err) {
    if (typeof showCopyLinkFeedback === "function") {
      showCopyLinkFeedback(button || document.activeElement, "請手動複製", false);
    }
    setComfyuiMessage(`${label}複製失敗，請手動選取內容。`, false);
  }
}

function comfyuiFavoritePromptSectionMarkup(title, text, copyKey) {
  if (!text) return "";
  return `
    <section class="comfyui-favorite-preview-section comfyui-favorite-prompt-section">
      <div class="comfyui-favorite-preview-section-head">
        <h4>${sanitize(title)}</h4>
        <button class="btn btn-sm comfyui-favorite-copy-btn" type="button" data-comfyui-favorite-copy="${sanitize(copyKey)}" title="複製${sanitize(title)}" aria-label="複製${sanitize(title)}">⧉</button>
      </div>
      <p>${sanitize(text)}</p>
    </section>
  `;
}

function ensureComfyuiFavoritePreviewModal() {
  let modal = $("comfyui-favorite-preview-modal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "comfyui-favorite-preview-modal";
  modal.className = "comfyui-favorite-preview-modal";
  modal.dataset.globalModalClose = "none";
  modal.hidden = true;
  modal.innerHTML = `
    <div class="comfyui-favorite-preview-card" role="dialog" aria-modal="true" aria-labelledby="comfyui-favorite-preview-title" data-global-modal-close="none">
      <button class="btn btn-sm comfyui-favorite-preview-close" type="button" data-comfyui-favorite-preview-close="1" aria-label="關閉" title="關閉">×</button>
      <div class="comfyui-favorite-preview-image" id="comfyui-favorite-preview-image"></div>
      <div class="comfyui-favorite-preview-detail" id="comfyui-favorite-preview-detail"></div>
    </div>
  `;
  modal.addEventListener("click", (event) => {
    if (event.target === modal || event.target.closest("[data-comfyui-favorite-preview-close]")) {
      closeComfyuiFavoritePreviewModal();
    }
  });
  document.body.appendChild(modal);
  return modal;
}

function closeComfyuiFavoritePreviewModal() {
  const modal = $("comfyui-favorite-preview-modal");
  if (modal) modal.hidden = true;
  updateComfyuiFavoriteModalBodyState();
}

function openComfyuiFavoritePreview(favoriteId) {
  const item = comfyuiImageFavorites.find((favorite) => String(favorite?.id || "") === String(favoriteId || ""));
  if (!item) {
    setComfyuiMessage("找不到這筆圖片收藏。", false);
    return;
  }
  const params = item?.params && typeof item.params === "object" ? item.params : {};
  const summary = comfyuiFavoriteParamSummary(params);
  const sourceUrl = item?.source_url || "";
  const prompt = comfyuiFavoritePromptText(params, "prompt", 2200);
  const negative = comfyuiFavoritePromptText(params, "negative_prompt", 1600);
  const requirements = comfyuiFavoriteRequirementsMarkup(params);
  const modal = ensureComfyuiFavoritePreviewModal();
  const image = $("comfyui-favorite-preview-image");
  const detail = $("comfyui-favorite-preview-detail");
  if (image) {
    image.innerHTML = `<img src="${sanitize(item.preview_url || "")}" alt="${sanitize(item.title || "圖片收藏")}" />`;
  }
  if (detail) {
    detail.innerHTML = `
      <div class="comfyui-favorite-preview-head">
        <strong id="comfyui-favorite-preview-title">${sanitize(item.title || "圖片收藏")}</strong>
        <span>${sanitize(item.source_type || "manual")}${item.created_at ? ` · ${sanitize(new Date(item.created_at).toLocaleString())}` : ""}</span>
      </div>
      <div class="comfyui-favorite-chips">
        ${summary.length ? summary.map((value) => `<span>${sanitize(value)}</span>`).join("") : "<span>尚無參數</span>"}
      </div>
      ${requirements}
      ${comfyuiFavoritePromptSectionMarkup("提示詞", prompt, "prompt")}
      ${comfyuiFavoritePromptSectionMarkup("負面提示詞", negative, "negative")}
      ${item.note ? `<section class="comfyui-favorite-preview-section"><h4>備註</h4><p>${sanitize(item.note)}</p></section>` : ""}
      <div class="drive-file-actions comfyui-favorite-actions">
        <button class="btn btn-primary" type="button" data-comfyui-favorite-preview-apply="${sanitize(String(item.id || ""))}">套回表單</button>
        ${sourceUrl ? `<a class="btn" href="${sanitize(sourceUrl)}" target="_blank" rel="noopener noreferrer">來源</a>` : ""}
        <button class="btn btn-danger" type="button" data-comfyui-favorite-preview-delete="${sanitize(String(item.id || ""))}">刪除</button>
      </div>
    `;
    detail.querySelector("[data-comfyui-favorite-preview-apply]")?.addEventListener("click", () => {
      applyComfyuiFavoriteToForm(item.id).catch((err) => setComfyuiMessage(err.message || "圖片收藏套回表單失敗", false));
      closeComfyuiFavoritePreviewModal();
    });
    detail.querySelectorAll("[data-comfyui-favorite-copy]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.getAttribute("data-comfyui-favorite-copy");
        copyComfyuiFavoritePromptText(key === "negative" ? negative : prompt, button, key === "negative" ? "負面提示詞" : "提示詞");
      });
    });
    detail.querySelector("[data-comfyui-favorite-preview-delete]")?.addEventListener("click", () => {
      deleteComfyuiImageFavorite(item.id).catch((err) => setComfyuiMessage(err.message || "圖片收藏刪除失敗", false));
    });
  }
  modal.hidden = false;
  updateComfyuiFavoriteModalBodyState();
}

function renderComfyuiImageFavorites(favorites = []) {
  const list = $("comfyui-favorites-list");
  if (!list) return;
  comfyuiImageFavorites = Array.isArray(favorites) ? favorites.slice() : [];
  if (!comfyuiImageFavorites.length) {
    list.innerHTML = '<div class="drive-empty">尚無圖片收藏</div>';
    setComfyuiFavoritesStatus("尚無圖片收藏。");
    return;
  }
  list.innerHTML = comfyuiImageFavorites.map((item) => {
    const params = item?.params && typeof item.params === "object" ? item.params : {};
    const summary = comfyuiFavoriteParamSummary(params);
    const model = params.model || params.diffusers_model_repo || "";
    return `
      <article class="comfyui-favorite-card" data-comfyui-favorite-card="${sanitize(String(item.id || ""))}">
        <button class="comfyui-favorite-thumb" type="button" data-comfyui-favorite-open="${sanitize(String(item.id || ""))}" aria-label="${sanitize(item.title || "圖片收藏")}">
          <img src="${sanitize(item.preview_url || "")}" alt="${sanitize(item.title || "圖片收藏")}" loading="lazy" />
          <span class="comfyui-favorite-image-caption">
            <strong>${sanitize(item.title || "圖片收藏")}</strong>
            ${model ? `<small>${sanitize(model)}</small>` : ""}
          </span>
          <span class="comfyui-favorite-image-meta">${summary.length ? sanitize(summary.slice(0, 2).join(" · ")) : ""}</span>
        </button>
      </article>
    `;
  }).join("");
  list.querySelectorAll("[data-comfyui-favorite-open]").forEach((button) => {
    let lastTouch = 0;
    button.addEventListener("click", () => {
      if (window.matchMedia?.("(pointer: coarse)")?.matches) {
        openComfyuiFavoritePreview(button.getAttribute("data-comfyui-favorite-open"));
        return;
      }
      const now = Date.now();
      if (now - lastTouch < 420) {
        openComfyuiFavoritePreview(button.getAttribute("data-comfyui-favorite-open"));
      }
      lastTouch = now;
    });
    button.addEventListener("dblclick", () => openComfyuiFavoritePreview(button.getAttribute("data-comfyui-favorite-open")));
    button.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openComfyuiFavoritePreview(button.getAttribute("data-comfyui-favorite-open"));
      }
    });
  });
  setComfyuiFavoritesStatus(`已讀取 ${comfyuiImageFavorites.length} 筆圖片收藏。`);
}

async function loadComfyuiImageFavorites() {
  const refreshBtn = $("comfyui-favorites-refresh-btn");
  if (refreshBtn) {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "讀取中...";
  }
  setComfyuiFavoritesStatus("正在讀取圖片收藏...");
  try {
    await fetchCsrfToken();
    const res = await apiFetch(API + "/comfyui/image-favorites", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" },
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `圖片收藏讀取失敗（HTTP ${res.status}）`);
    renderComfyuiImageFavorites(json.favorites || []);
  } finally {
    if (refreshBtn) {
      refreshBtn.disabled = false;
      refreshBtn.textContent = "重新整理收藏";
    }
  }
}

function comfyuiFavoriteWorkflowBundleHint(params = {}) {
  const explicit = String(params.workflow_system_bundle_id || params.workflow_bundle_id || "").trim();
  if (explicit) return explicit;
  const baseModel = String(params?.civitai?.base_model || "").toLowerCase();
  const model = String(params.model || params.diffusers_model_repo || "").toLowerCase();
  const sourceModelName = String(params?.civitai?.source_model_name || "").toLowerCase();
  const resourceNames = Array.isArray(params?.civitai?.model_version_resources)
    ? params.civitai.model_version_resources.map((resource) => String(resource?.model_name || resource?.version_name || "").toLowerCase()).join(" ")
    : "";
  const family = `${model} ${sourceModelName} ${baseModel} ${resourceNames}`;
  const familyCompact = family.replace(/[^a-z0-9]/g, "");
  if (family.includes("zit") || familyCompact.includes("zimagebase") || familyCompact.includes("zimage") || model.includes("z_image_turbo")) {
    return "origin_zit_txt2img";
  }
  if (family.includes("anima")) {
    return "origin_anima_txt2img";
  }
  if (family.includes("flux") || model.includes("flux1-") || model.includes("flux-1")) {
    return "origin_flux_dev_txt2img";
  }
  if (family.includes("netayume")) {
    return "origin_netayume_txt2img";
  }
  if (family.includes("sd3.5") || family.includes("sd 3.5") || family.includes("sd3_5")) {
    return "origin_sd35_txt2img";
  }
  if (["sdxl", "sd xl", "illustrious", "pony", "noob", "xl"].some((token) => family.includes(token))) {
    return "origin_sdxl_txt2img";
  }
  return "";
}

async function resolveComfyuiFavoriteWorkflow(params = {}) {
  const directId = Number(params.workflow_preset_id || params.preset_id || 0);
  if (Number.isFinite(directId) && directId > 0) {
    return {
      id: directId,
      title: params.workflow_preset_title || params.preset_title || `Workflow #${directId}`,
      bundleId: params.workflow_system_bundle_id || params.workflow_bundle_id || "",
    };
  }
  const bundleId = comfyuiFavoriteWorkflowBundleHint(params);
  if (!bundleId) return { id: 0, title: "", bundleId: "" };
  if (typeof loadComfyuiWorkflowPresets === "function") {
    try {
      await loadComfyuiWorkflowPresets({ silentTemplateReload: true });
    } catch (err) {
      console.warn("Failed to load workflow presets for favorite restore", err);
    }
  }
  const presets = Array.isArray(comfyuiWorkflowPresets) ? comfyuiWorkflowPresets : [];
  const preset = presets.find((item) => String(item?.system_bundle_id || "") === bundleId)
    || presets.find((item) => String(item?.title || "").toLowerCase().includes(bundleId.replace(/^origin_/, "").replace(/_/g, " ").toLowerCase()));
  return preset
    ? { id: Number(preset.id || 0), title: preset.title || params.workflow_preset_title || "", bundleId }
    : { id: 0, title: params.workflow_preset_title || bundleId, bundleId };
}

async function applyComfyuiFavoriteToForm(favoriteId) {
  const item = comfyuiImageFavorites.find((favorite) => String(favorite?.id || "") === String(favoriteId || ""));
  if (!item) {
    setComfyuiMessage("找不到這筆圖片收藏。", false);
    return;
  }
  const params = item.params && typeof item.params === "object" ? item.params : {};
  const normalized = comfyuiNormalizeFavoriteParamsForRestore(params);
  const diffusersRepo = params.diffusers_model_repo || "";
  const workflow = diffusersRepo ? { id: 0, title: "", bundleId: "" } : await resolveComfyuiFavoriteWorkflow(params);
  const workflowPresetId = Number(workflow.id || 0);
  const favoriteWorkflowJson = item?.params?.workflow_json && typeof item.params.workflow_json === "object" && !Array.isArray(item.params.workflow_json)
    ? item.params.workflow_json
    : item?.workflow_json && typeof item.workflow_json === "object" && !Array.isArray(item.workflow_json)
      ? item.workflow_json
      : (comfyuiSelectedTemplateDetail?.workflow_json && typeof comfyuiSelectedTemplateDetail.workflow_json === "object" && !Array.isArray(comfyuiSelectedTemplateDetail.workflow_json)
        ? comfyuiSelectedTemplateDetail.workflow_json
        : {});
  const hasFavoriteWorkflowJson = !!(favoriteWorkflowJson && Object.keys(favoriteWorkflowJson).length);
  const shouldApplyWorkflowSnapshot = workflowPresetId > 0 && hasFavoriteWorkflowJson;
  setComfyuiView(diffusersRepo ? "hf" : "generate");
  if (workflowPresetId > 0) {
    if (typeof ensureComfyuiHistoryWorkflowSelectOption === "function") {
      ensureComfyuiHistoryWorkflowSelectOption(workflowPresetId, workflow.title || params.workflow_preset_title || "");
    }
    if (typeof loadComfyuiSelectedTemplateDetail === "function") {
      await loadComfyuiSelectedTemplateDetail(workflowPresetId, { silent: true, applyDefaults: false });
    }
    if (typeof ensureComfyuiHistoryWorkflowSelectOption === "function") {
      ensureComfyuiHistoryWorkflowSelectOption(workflowPresetId, workflow.title || params.workflow_preset_title || "");
    }
  }
  const generationMode = params.generation_mode || "txt2img";
  const isDiffusersMode = isComfyuiDiffusersMode();
  [
    ["comfyui-generation-mode", generationMode],
    ["comfyui-model-select", params.model || "", true],
    ["comfyui-prompt", params.prompt || ""],
    ["comfyui-negative-prompt", params.negative_prompt || ""],
    ["comfyui-width", normalized.width ?? ""],
    ["comfyui-height", normalized.height ?? ""],
    ["comfyui-steps", params.steps || ""],
    ["comfyui-cfg", normalized.cfg ?? ""],
    ["comfyui-run-count", params.run_count || 1],
    ["comfyui-seed", params.seed || ""],
  ]
    .filter(([id]) => !(workflowPresetId > 0 && id === "comfyui-model-select"))
    .forEach(([id, value, preserveMissingOption]) => setComfyuiFieldValue(id, value, { preserveMissingOption: !!preserveMissingOption }));
  setComfyuiSamplingFieldsFromValues(params.sampler_name || "euler", params.scheduler || "normal");
  if (shouldApplyWorkflowSnapshot && typeof applyComfyuiTemplateHistorySnapshotToForm === "function") {
    applyComfyuiTemplateHistorySnapshotToForm(favoriteWorkflowJson, params);
    if (!diffusersRepo && workflowPresetId) {
      setComfyuiFieldValue("comfyui-model-select", params.model || "", { preserveMissingOption: true });
    }
  }
  if (diffusersRepo) {
    setComfyuiFieldValue("comfyui-diffusers-model-repo", diffusersRepo);
    setComfyuiFieldValue("comfyui-diffusers-model-variant", params.diffusers_model_variant || "", { preserveMissingOption: true });
  } else if (workflowPresetId) {
    setComfyuiFieldValue("comfyui-model-select", params.model || "", { preserveMissingOption: true });
  } else if (!workflowPresetId) {
    setComfyuiFieldValue("comfyui-model-select", params.model || "", { preserveMissingOption: true });
  }
  if (params.vae && !isDiffusersMode) {
    setComfyuiFieldValue("comfyui-vae-select", params.vae, { preserveMissingOption: true });
  }
  if (params.seed !== undefined && params.seed !== null && String(params.seed).trim()) {
    setComfyuiSeedAfterGenerateMode("fixed");
  } else if (params.seed_after_generate) {
    setComfyuiSeedAfterGenerateMode(params.seed_after_generate);
  }
  if (Array.isArray(params.loras)) {
    comfyuiSelectedLoras = params.loras.slice(0, COMFYUI_MAX_LORAS).map((item) => ({
      name: String(item?.name || item?.lora_name || "").trim(),
      strength_model: Number.isFinite(Number(item?.strength_model)) ? Number(item.strength_model) : 1,
      strength_clip: Number.isFinite(Number(item?.strength_clip)) ? Number(item.strength_clip) : 1,
    })).filter((item) => item.name);
    renderComfyuiSelectedLoras();
  }
  updateComfyuiModeVisibility();
  updateComfyuiDiffusersUi();
  writeComfyuiDraft();
  setComfyuiMessage(`已套回圖片收藏「${item.title || item.id}」${workflowPresetId ? `與 workflow「${workflow.title || workflowPresetId}」` : ""}。`, true);
}

async function deleteComfyuiImageFavorite(favoriteId) {
  if (!favoriteId) return;
  const confirmed = window.confirm("刪除這筆圖片收藏？");
  if (!confirmed) return;
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + `/comfyui/image-favorites/${encodeURIComponent(favoriteId)}`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" },
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `圖片收藏刪除失敗（HTTP ${res.status}）`);
    await loadComfyuiImageFavorites();
    setComfyuiMessage(json.msg || "已刪除圖片收藏", true);
  } catch (err) {
    setComfyuiMessage(err.message || "圖片收藏刪除失敗", false);
  }
}

function comfyuiFavoriteCurrentPayload() {
  const params = comfyuiShareGenerationPayload();
  const outputLabel = comfyuiGeneratedImageLabel(comfyuiCurrentImage, comfyuiSelectedImageIndex);
  return {
    source_type: "generated",
    title: outputLabel ? `產圖收藏 - ${outputLabel}` : "產圖收藏",
    image_ref: comfyuiCurrentImage?.image_ref,
    prompt_id: comfyuiCurrentImage?.prompt_id || "",
    selected_image_index: comfyuiSelectedImageIndex,
    output_label: outputLabel,
    params,
  };
}

async function favoriteComfyuiGeneratedImage() {
  if (!comfyuiCurrentImage?.image_ref) {
    setComfyuiMessage("目前沒有可收藏的產圖結果", false);
    return;
  }
  const favoriteBtn = $("comfyui-favorite-btn");
  if (favoriteBtn) {
    favoriteBtn.disabled = true;
    favoriteBtn.textContent = "收藏中...";
  }
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/comfyui/image-favorites", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || "",
      },
      body: JSON.stringify(comfyuiFavoriteCurrentPayload()),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `圖片收藏失敗（HTTP ${res.status}）`);
    setComfyuiMessage(json.msg || "已收藏這張產圖", true);
    if ($("comfyui-view-favorites")?.classList.contains("active")) {
      await loadComfyuiImageFavorites();
    }
  } catch (err) {
    setComfyuiMessage(err.message || "圖片收藏失敗", false);
  } finally {
    if (favoriteBtn) {
      favoriteBtn.disabled = !comfyuiCurrentImage?.image_ref;
      favoriteBtn.textContent = "收藏";
    }
  }
}

async function importComfyuiFavoriteFromCivitai() {
  const input = $("comfyui-favorite-civitai-url");
  const button = $("comfyui-favorite-civitai-import-btn");
  const status = $("comfyui-favorite-civitai-status");
  const url = (input?.value || "").trim();
  if (!url) {
    setComfyuiMessage("請輸入 Civitai 圖片頁 URL。", false);
    return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = "匯入中...";
  }
  if (status) status.textContent = "正在從 Civitai API 匯入...";
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/comfyui/image-favorites/import-civitai", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || "",
      },
      body: JSON.stringify({ url }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `Civitai 圖片匯入失敗（HTTP ${res.status}）`);
    if (status) status.textContent = json.msg || "已匯入 Civitai 圖片收藏。";
    input.value = "";
    await loadComfyuiImageFavorites();
    closeComfyuiFavoriteModal();
    setComfyuiMessage(json.msg || "已匯入 Civitai 圖片收藏", true);
  } catch (err) {
    if (status) status.textContent = err.message || "Civitai 圖片匯入失敗";
    setComfyuiMessage(err.message || "Civitai 圖片匯入失敗", false);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "從 Civitai 匯入";
    }
  }
}

function comfyuiFavoriteUploadParams() {
  return {
    generation_mode: "txt2img",
    prompt: $("comfyui-favorite-prompt")?.value || "",
    negative_prompt: $("comfyui-favorite-negative-prompt")?.value || "",
    model: $("comfyui-favorite-model")?.value || "",
    vae: $("comfyui-favorite-vae")?.value || "",
    seed: $("comfyui-favorite-seed")?.value || "",
    width: $("comfyui-favorite-width")?.value || "",
    height: $("comfyui-favorite-height")?.value || "",
    steps: $("comfyui-favorite-steps")?.value || "",
    cfg: $("comfyui-favorite-cfg")?.value || "",
    sampler_name: $("comfyui-favorite-sampler")?.value || "",
    scheduler: $("comfyui-favorite-scheduler")?.value || "",
  };
}

async function saveUploadedComfyuiFavorite() {
  const fileInput = $("comfyui-favorite-upload-file");
  const button = $("comfyui-favorite-upload-save-btn");
  const status = $("comfyui-favorite-upload-status");
  const file = fileInput?.files && fileInput.files[0] ? fileInput.files[0] : null;
  if (!file) {
    setComfyuiMessage("請先選擇要收藏的圖片。", false);
    return;
  }
  if (!/^image\/(png|jpeg|webp)$/i.test(file.type || "")) {
    setComfyuiMessage("收藏圖片只支援 PNG、JPG、WEBP。", false);
    return;
  }
  const form = new FormData();
  form.append("image", file, file.name || "favorite.png");
  form.append("title", $("comfyui-favorite-title")?.value || "");
  form.append("note", $("comfyui-favorite-note")?.value || "");
  form.append("params_json", JSON.stringify(comfyuiFavoriteUploadParams()));
  if (button) {
    button.disabled = true;
    button.textContent = "儲存中...";
  }
  if (status) status.textContent = "正在儲存圖片收藏...";
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/comfyui/image-favorites", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" },
      body: form,
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `上傳收藏失敗（HTTP ${res.status}）`);
    if (status) status.textContent = json.msg || "已儲存上傳收藏。";
    if (fileInput) fileInput.value = "";
    await loadComfyuiImageFavorites();
    closeComfyuiFavoriteModal();
    setComfyuiMessage(json.msg || "已儲存上傳收藏", true);
  } catch (err) {
    if (status) status.textContent = err.message || "上傳收藏失敗";
    setComfyuiMessage(err.message || "上傳收藏失敗", false);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "儲存上傳收藏";
    }
  }
}

function formatComfyuiCivitaiFileSize(file) {
  const sizeBytes = Number(file?.size_bytes || 0);
  if (sizeBytes > 0 && typeof formatDriveBytes === "function") {
    return formatDriveBytes(sizeBytes);
  }
  const sizeKb = Number(file?.size_kb || 0);
  if (sizeKb > 0 && typeof formatDriveBytes === "function") {
    return formatDriveBytes(Math.round(sizeKb * 1024));
  }
  return "";
}

function summarizeComfyuiCivitaiHashes(file) {
  const hashes = file && typeof file.hashes === "object" ? file.hashes : {};
  return Object.entries(hashes)
    .filter(([key, value]) => String(key || "").trim() && String(value || "").trim())
    .slice(0, 3)
    .map(([key, value]) => `${String(key).toUpperCase()}: ${String(value)}`);
}

function renderComfyuiCivitaiSearchResults(results) {
  const box = $("comfyui-civitai-search-results");
  if (!box) return;
  comfyuiCivitaiSearchResults = Array.isArray(results) ? results.slice() : [];
  if (!comfyuiCivitaiSearchResults.length) {
    box.innerHTML = '<div class="drive-card-sub">沒有符合條件的 Civitai 模型。可調整關鍵字、base model、類型或 Safe/NSFW 篩選。</div>';
    return;
  }
  box.innerHTML = comfyuiCivitaiSearchResults.map((item) => {
    const latestVersion = item.latest_version || {};
    const primaryFile = latestVersion.primary_file || {};
    const hashSummary = summarizeComfyuiCivitaiHashes(primaryFile);
    const compatibleModels = Array.isArray(item.compatible_models) ? item.compatible_models.filter(Boolean) : [];
    const sizeLabel = formatComfyuiCivitaiFileSize(primaryFile);
    const createdAt = latestVersion.created_at ? new Date(latestVersion.created_at).toLocaleString() : "";
    const pageUrl = item.selected_page_url || item.page_url || "";
    const thumbnailUrl = item.thumbnail_proxy_url || latestVersion.thumbnail_proxy_url || item.thumbnail_url || latestVersion.thumbnail_url || "";
    const nsfwChip = item.nsfw ? '<span class="comfyui-civitai-search-chip warn">NSFW</span>' : '<span class="comfyui-civitai-search-chip">Safe</span>';
    const sourceLabel = item.source_label || item.source_site || "civitai.com";
    const selectKey = item.select_key || `${sourceLabel}:${item.model_id || ""}`;
    return `
      <div class="comfyui-civitai-search-card">
        <div class="comfyui-civitai-search-thumb">
          ${thumbnailUrl
            ? `<img src="${sanitize(thumbnailUrl)}" alt="${sanitize(item.name || "Civitai model")} preview" loading="lazy" referrerpolicy="no-referrer" />`
            : '<div class="comfyui-civitai-search-thumb-empty">無縮圖</div>'}
        </div>
        <div class="comfyui-civitai-search-content">
          <div class="comfyui-civitai-search-head">
            <div class="comfyui-civitai-search-title">
              <strong>${sanitize(item.name || "未命名模型")}</strong>
              <span>${sanitize(item.creator ? `by ${item.creator}` : "官方未提供作者")} · ${sanitize(item.type || "未知類型")} · ${sanitize(`版本 ${item.version_count || 0}`)}</span>
            </div>
            <div class="comfyui-civitai-search-flags">
              <span class="comfyui-civitai-search-chip">${sanitize(sourceLabel)}</span>
              <span class="comfyui-civitai-search-chip">${sanitize(item.suggested_model_type || "checkpoint")}</span>
              ${nsfwChip}
            </div>
          </div>
          <div class="comfyui-civitai-search-meta">
            <span>最新版本：${sanitize(latestVersion.name || "未命名版本")}</span>
            ${latestVersion.base_model ? `<span>Base model：${sanitize(latestVersion.base_model)}</span>` : ""}
            ${createdAt ? `<span>更新時間：${sanitize(createdAt)}</span>` : ""}
          </div>
          <div class="comfyui-civitai-search-meta">
            ${compatibleModels.length ? `<span>相容模型：${sanitize(compatibleModels.join(", "))}</span>` : '<span>相容模型：官方未標示</span>'}
            ${primaryFile.name ? `<span>檔案：${sanitize(primaryFile.name)}</span>` : ""}
            ${sizeLabel ? `<span>大小：${sanitize(sizeLabel)}</span>` : ""}
          </div>
          <div class="comfyui-civitai-search-link">
            ${pageUrl ? `<a href="${sanitize(pageUrl)}" target="_blank" rel="noopener noreferrer">開啟 Civitai 頁面</a>` : '<span>官方頁面：未提供</span>'}
          </div>
          <div class="comfyui-civitai-search-hashes">
            ${hashSummary.length ? hashSummary.map((value) => `<span>${sanitize(value)}</span>`).join("") : '<span>hash：官方未提供</span>'}
          </div>
          <div class="drive-card-sub">搜尋結果使用固定版本摘要顯示；若顯示下載前資料不足，可先帶入下載區讀取完整版本與檔案清單。</div>
          <div class="drive-file-actions" style="justify-content:flex-start;">
            <button class="btn" type="button" data-comfyui-civitai-select="${sanitize(String(selectKey || ""))}">帶入下載區</button>
          </div>
        </div>
      </div>
    `;
  }).join("");
  box.querySelectorAll("[data-comfyui-civitai-select]").forEach((button) => {
    button.addEventListener("click", () => {
      const modelId = button.getAttribute("data-comfyui-civitai-select") || "";
      useComfyuiCivitaiSearchResult(modelId).catch((err) => setComfyuiMessage(err.message || "帶入 Civitai 模型失敗", false));
    });
  });
}

async function useComfyuiCivitaiSearchResult(modelId) {
  const selected = comfyuiCivitaiSearchResults.find((item) => {
    const sourceLabel = item.source_label || item.source_site || "civitai.com";
    const selectKey = item.select_key || `${sourceLabel}:${item.model_id || ""}`;
    return String(selectKey || "") === String(modelId || "") || String(item.model_id || "") === String(modelId || "");
  });
  if (!selected) {
    setComfyuiMessage("找不到要帶入的 Civitai 搜尋結果。", false);
    return;
  }
  if ($("comfyui-civitai-url")) $("comfyui-civitai-url").value = selected.selected_page_url || selected.page_url || "";
  if ($("comfyui-model-download-type") && selected.suggested_model_type) {
    $("comfyui-model-download-type").value = selected.suggested_model_type;
  }
  updateComfyuiModelRelativePathHint();
  const status = $("comfyui-civitai-search-status");
  if (status) status.textContent = `已帶入 ${selected.name}，正在讀取完整版本與檔案資訊...`;
  await inspectComfyuiCivitaiModel();
}

async function searchComfyuiCivitaiModels() {
  if (currentUser !== "root") {
    setComfyuiMessage("只有 root 可以搜尋 Civitai 模型。", false);
    return;
  }
  const button = $("comfyui-civitai-search-btn");
  const status = $("comfyui-civitai-search-status");
  const query = ($("comfyui-civitai-search-query")?.value || "").trim();
  const baseModel = ($("comfyui-civitai-search-base-model")?.value || "").trim();
  const modelType = ($("comfyui-civitai-search-type")?.value || "").trim();
  const nsfwMode = ($("comfyui-civitai-search-nsfw")?.value || "safe").trim() || "safe";
  if (button) {
    button.disabled = true;
    button.textContent = "搜尋中...";
  }
  if (status) status.textContent = "正在搜尋 Civitai 模型...";
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/root/comfyui/civitai/search", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || "",
      },
      body: JSON.stringify({
        query,
        base_model: baseModel,
        model_type: modelType,
        nsfw_mode: nsfwMode,
        limit: 12,
      }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `Civitai 搜尋失敗（HTTP ${res.status}）`);
    renderComfyuiCivitaiSearchResults(json.results || []);
    if (status) status.textContent = json.msg || "已取得 Civitai 搜尋結果。";
    setComfyuiMessage(json.msg || "已取得 Civitai 搜尋結果。", true);
  } catch (err) {
    renderComfyuiCivitaiSearchResults([]);
    if (status) status.textContent = err.message || "Civitai 搜尋失敗";
    setComfyuiMessage(err.message || "Civitai 搜尋失敗", false);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "搜尋 Civitai";
    }
  }
}

function renderComfyuiCivitaiFiles(versionId) {
  const fileSelect = $("comfyui-civitai-file");
  if (!fileSelect) return;
  const versions = comfyuiCivitaiInspection?.versions || [];
  const selectedVersion = versions.find((item) => String(item.id) === String(versionId || "")) || null;
  const files = selectedVersion?.files || [];
  fileSelect.innerHTML = files.length
    ? files.map((file) => {
        const sizeLabel = file.size_kb ? ` · ${Math.round(Number(file.size_kb) / 1024 * 10) / 10} MB` : "";
        return `<option value="${sanitize(String(file.id || ""))}">${sanitize(file.name || "未命名檔案")}${sanitize(sizeLabel)}</option>`;
      }).join("")
    : `<option value="">這個版本沒有可下載檔案</option>`;
}

function renderComfyuiCivitaiTrainedWords(versionId) {
  const box = $("comfyui-civitai-trained-words");
  if (!box) return;
  const versions = comfyuiCivitaiInspection?.versions || [];
  const selectedVersion = versions.find((item) => String(item.id) === String(versionId || "")) || null;
  const trainedWords = Array.isArray(selectedVersion?.trained_words)
    ? selectedVersion.trained_words.filter(Boolean).map(String)
    : [];
  if (!trainedWords.length) {
    box.innerHTML = '<span class="drive-card-sub">這個版本沒有提供 trigger words，或官方沒有填寫。</span>';
    return;
  }
  box.innerHTML = trainedWords.map((value) => (
    `<button class="comfyui-embedding-chip" type="button" data-comfyui-trained-word="${sanitize(value)}" title="Trigger word：${sanitize(value)}">${sanitize(value)}</button>`
  )).join("");
  box.querySelectorAll("[data-comfyui-trained-word]").forEach((button) => {
    button.addEventListener("click", () => {
      const triggerWord = button.getAttribute("data-comfyui-trained-word") || "";
      if (!triggerWord) return;
      setComfyuiMessage(`這個版本的 trigger word：${triggerWord}`, true);
    });
  });
}

function renderComfyuiCivitaiInspection(model) {
  const versionSelect = $("comfyui-civitai-version");
  const status = $("comfyui-model-download-status");
  if (!versionSelect) return;
  comfyuiCivitaiInspection = model || null;
  const versions = comfyuiCivitaiInspection?.versions || [];
  versionSelect.innerHTML = versions.length
    ? versions.map((version) => {
        const labelBits = [version.name || `Version ${version.id}`];
        if (version.base_model) labelBits.push(version.base_model);
        return `<option value="${sanitize(String(version.id || ""))}">${sanitize(labelBits.join(" · "))}</option>`;
      }).join("")
    : `<option value="">請先讀取模型</option>`;
  const selectedVersionId = comfyuiCivitaiInspection?.selected_version_id || versions[0]?.id || "";
  versionSelect.value = selectedVersionId ? String(selectedVersionId) : "";
  renderComfyuiCivitaiFiles(versionSelect.value);
  renderComfyuiCivitaiTrainedWords(versionSelect.value);
  if ($("comfyui-model-download-type") && comfyuiCivitaiInspection?.suggested_model_type) {
    $("comfyui-model-download-type").value = comfyuiCivitaiInspection.suggested_model_type;
  }
  updateComfyuiModelRelativePathHint();
  if (status && model) {
    const creator = model.creator ? ` · by ${model.creator}` : "";
    status.textContent = `已讀取 ${model.name}${creator}，請選擇版本與檔案。`;
  }
}

function comfyuiSelectedModelSourceMode() {
  return String($("comfyui-model-source-mode")?.value || "civitai").trim().toLowerCase() || "civitai";
}

function comfyuiSelectedModelDownloadType() {
  return String($("comfyui-model-download-type")?.value || "checkpoint").trim().toLowerCase() || "checkpoint";
}

function comfyuiDefaultModelRelativeDir(type = comfyuiSelectedModelDownloadType()) {
  const normalized = String(type || "checkpoint").trim().toLowerCase() || "checkpoint";
  if (normalized === "diffusion_model" || normalized === "unet") return "diffusion_models";
  if (normalized === "text_encoder") return "text_encoders";
  if (normalized === "clip") return "clip";
  if (normalized === "clip_vision") return "clip_vision";
  if (normalized === "lora") return "loras";
  if (normalized === "embedding") return "embeddings";
  if (normalized === "vae") return "vae";
  if (normalized === "audio") return "audio";
  if (normalized === "video") return "video";
  if (normalized === "controlnet") return "controlnet";
  if (normalized === "upscale") return "upscale_models";
  if (normalized === "latent_upscale") return "latent_upscale_models";
  return "checkpoints";
}

function updateComfyuiModelRelativePathHint() {
  const hint = $("comfyui-model-relative-path-hint");
  if (!hint) return;
  const relativePath = ($("comfyui-model-relative-path")?.value || "").trim();
  const defaultDir = comfyuiDefaultModelRelativeDir();
  const effectiveDir = relativePath || defaultDir;
  hint.innerHTML = [
    `不填時會使用目前模型類型的預設資料夾 <code>ComfyUI/models/${sanitize(defaultDir)}</code>。`,
    `若要自訂，請填寫 <code>ComfyUI/models/</code> 底下的相對路徑；目前會寫入 <code>ComfyUI/models/${sanitize(effectiveDir)}</code>。`,
  ].join(" ");
}

function updateComfyuiModelSourceMode() {
  const mode = comfyuiSelectedModelSourceMode();
  const civitai = $("comfyui-model-source-civitai");
  const upload = $("comfyui-model-source-upload");
  if (civitai) civitai.style.display = mode === "upload" ? "none" : "";
  if (upload) upload.style.display = mode === "upload" ? "" : "none";
  updateComfyuiModelRelativePathHint();
}

function onComfyuiCivitaiVersionChange() {
  const versionId = $("comfyui-civitai-version")?.value || "";
  renderComfyuiCivitaiFiles(versionId);
  renderComfyuiCivitaiTrainedWords(versionId);
}

function setComfyuiModelDownloadProgress({ visible = true, running = false, percent = 0, label = "", detail = "" } = {}) {
  const panel = $("comfyui-model-download-progress");
  const bar = $("comfyui-model-download-progress-bar");
  const labelEl = $("comfyui-model-download-progress-label");
  const percentEl = $("comfyui-model-download-progress-percent");
  const detailEl = $("comfyui-model-download-progress-detail");
  if (!panel) return;
  panel.style.display = visible ? "" : "none";
  panel.classList.toggle("running", !!running);
  const safePercent = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  if (bar) bar.style.width = `${safePercent}%`;
  if (labelEl) labelEl.textContent = label || "模型下載中";
  if (percentEl) percentEl.textContent = `${safePercent}%`;
  if (detailEl) detailEl.textContent = detail || "";
}

async function pollComfyuiModelDownloadJob(jobId) {
  setComfyuiIdleSuspend("comfyui_model_download", true, "ComfyUI 模型下載中");
  try {
    while (true) {
      const res = await apiFetch(API + `/root/comfyui/download-jobs/${encodeURIComponent(jobId)}`, {
        credentials: "same-origin",
        headers: { "X-CSRF-Token": getCsrfToken() || "" }
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok || !json.ok) throw new Error(json.msg || `模型下載進度讀取失敗（HTTP ${res.status}）`);
      const job = json.job || {};
      const progress = job.progress || {};
      const totalBytes = Number(progress.total_bytes || 0);
      const writtenBytes = Number(progress.bytes_written || 0);
      const sizeText = totalBytes > 0
        ? `${formatDriveBytes(writtenBytes)} / ${formatDriveBytes(totalBytes)}`
        : `${formatDriveBytes(writtenBytes)}`;
      const detail = `${progress.detail || "等待下載進度"}${writtenBytes > 0 ? `；${sizeText}` : ""}`;
      setComfyuiModelDownloadProgress({
        visible: true,
        running: job.status !== "completed" && job.status !== "error",
        percent: Number(progress.percent || 0),
        label: job.status === "completed" ? "模型已下載" : (job.status === "error" ? "模型下載失敗" : "模型下載中"),
        detail,
      });
      if (job.status === "completed") return job.result || {};
      if (job.status === "error") throw new Error(job.error || progress.detail || "模型下載失敗");
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  } finally {
    setComfyuiIdleSuspend("comfyui_model_download", false, "ComfyUI 模型下載中");
  }
}

async function inspectComfyuiCivitaiModel() {
  if (currentUser !== "root") {
    setComfyuiMessage("只有 root 可以下載 ComfyUI 模型。", false);
    return;
  }
  const inspectBtn = $("comfyui-civitai-inspect-btn");
  const status = $("comfyui-model-download-status");
  const pageUrl = ($("comfyui-civitai-url")?.value || "").trim();
  if (!pageUrl) {
    if (status) status.textContent = "請輸入 Civitai 模型頁網址。";
    setComfyuiMessage("請輸入 Civitai 模型頁網址。", false);
    return;
  }
  if (inspectBtn) {
    inspectBtn.disabled = true;
    inspectBtn.textContent = "讀取中...";
  }
  if (status) status.textContent = "正在讀取 Civitai 官方模型資訊...";
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/root/comfyui/civitai/inspect", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || ""
      },
      body: JSON.stringify({ page_url: pageUrl })
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `模型資訊讀取失敗（HTTP ${res.status}）`);
    renderComfyuiCivitaiInspection(json.model || null);
    setComfyuiMessage(json.msg || "已讀取 Civitai 模型資訊。", true);
  } catch (err) {
    comfyuiCivitaiInspection = null;
    renderComfyuiCivitaiInspection(null);
    if (status) status.textContent = err.message || "模型資訊讀取失敗";
    setComfyuiMessage(err.message || "模型資訊讀取失敗", false);
  } finally {
    if (inspectBtn) {
      inspectBtn.disabled = false;
      inspectBtn.textContent = "讀取模型資訊";
    }
  }
}

async function downloadComfyuiCivitaiModel() {
  if (currentUser !== "root") {
    setComfyuiMessage("只有 root 可以下載 ComfyUI 模型。", false);
    return;
  }
  const button = $("comfyui-model-download-btn");
  const status = $("comfyui-model-download-status");
  const pageUrl = ($("comfyui-civitai-url")?.value || "").trim();
  const type = $("comfyui-model-download-type")?.value || "checkpoint";
  const relativeDir = ($("comfyui-model-relative-path")?.value || "").trim();
  const baseDir = ($("comfyui-model-base-dir")?.value || "").trim();
  const versionId = $("comfyui-civitai-version")?.value || "";
  const fileId = $("comfyui-civitai-file")?.value || "";
  if (!pageUrl) {
    if (status) status.textContent = "請輸入 Civitai 模型頁網址。";
    setComfyuiMessage("請先輸入 Civitai 模型頁網址。", false);
    return;
  }
  if (!versionId) {
    if (status) status.textContent = "請先讀取模型資訊並選擇版本。";
    setComfyuiMessage("請先讀取模型資訊並選擇版本。", false);
    return;
  }
  const versions = Array.isArray(comfyuiCivitaiInspection?.versions) ? comfyuiCivitaiInspection.versions : [];
  const selectedVersion = versions.find((item) => String(item.id || "") === String(versionId)) || null;
  const selectedFile = Array.isArray(selectedVersion?.files)
    ? selectedVersion.files.find((item) => String(item.id || "") === String(fileId || "")) || selectedVersion.files[0] || null
    : null;
  const confirmBits = [
    `模型：${comfyuiCivitaiInspection?.name || "未命名模型"}`,
    `版本：${selectedVersion?.name || versionId}`,
    `檔案：${selectedFile?.name || "未命名檔案"}`,
  ];
  const sizeLabel = formatComfyuiCivitaiFileSize(selectedFile);
  if (sizeLabel) confirmBits.push(`大小：${sizeLabel}`);
  const hashSummary = summarizeComfyuiCivitaiHashes(selectedFile);
  if (hashSummary.length) confirmBits.push(`Hash：${hashSummary.join(" / ")}`);
  const compatibleModels = Array.isArray(comfyuiCivitaiInspection?.versions)
    ? [...new Set(comfyuiCivitaiInspection.versions.map((item) => String(item.base_model || "").trim()).filter(Boolean))]
    : [];
  if (compatibleModels.length) confirmBits.push(`相容模型：${compatibleModels.join(", ")}`);
  confirmBits.push(`儲存路徑：ComfyUI/models/${relativeDir || comfyuiDefaultModelRelativeDir(type)}`);
  confirmBits.push("下載後會直接寫入本地 ComfyUI models 目錄。");
  if (!window.confirm(`請再次確認要下載這個 Civitai 模型：\n\n${confirmBits.join("\n")}`)) {
    if (status) status.textContent = "已取消模型下載。";
    setComfyuiMessage("已取消模型下載。", false);
    return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = "下載中...";
  }
  if (status) status.textContent = "正在建立模型下載工作...";
  setComfyuiModelDownloadProgress({
    visible: true,
    running: true,
    percent: 0,
    label: "準備下載",
    detail: "正在建立模型下載工作..."
  });
  try {
    await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/root/comfyui/civitai/download", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken() || ""
      },
      body: JSON.stringify({
        page_url: pageUrl,
        version_id: versionId,
        file_id: fileId,
        type,
        base_dir: baseDir,
        relative_dir: relativeDir,
        async_progress: true,
      })
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `模型下載失敗（HTTP ${res.status}）`);
    const jobId = json.job?.job_id;
    if (!jobId) throw new Error("ComfyUI 未回傳模型下載工作編號");
    const info = await pollComfyuiModelDownloadJob(jobId);
    if (status) {
      const versionLabel = info?.civitai?.version_name ? ` · ${info.civitai.version_name}` : "";
      const trainedWords = Array.isArray(info?.civitai?.trained_words) ? info.civitai.trained_words.filter(Boolean) : [];
      const trainedText = trainedWords.length ? `；trigger words：${trainedWords.join(", ")}` : "";
      status.textContent = `${json.msg || "模型已下載"}${versionLabel}（${formatDriveBytes(info.size_bytes || 0)}）${trainedText}`;
    }
    setComfyuiModelDownloadProgress({
      visible: true,
      running: false,
      percent: 100,
      label: "模型已下載",
      detail: `${info.filename || ""} · ${formatDriveBytes(info.size_bytes || 0)}`
    });
    setComfyuiMessage(json.msg || "模型已下載。請重新整理模型清單。", true);
    await loadComfyuiModels({ forceRefresh: true });
  } catch (err) {
    if (status) status.textContent = err.message || "模型下載失敗";
    setComfyuiModelDownloadProgress({
      visible: true,
      running: false,
      percent: 100,
      label: "模型下載失敗",
      detail: err.message || "模型下載失敗"
    });
    setComfyuiMessage(err.message || "模型下載失敗", false);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "下載選定版本";
    }
  }
}

async function downloadComfyuiModelFromUrl() {
  return downloadComfyuiCivitaiModel();
}

async function uploadComfyuiModelFile() {
  if (currentUser !== "root") {
    setComfyuiMessage("只有 root 可以匯入 ComfyUI 模型。", false);
    return;
  }
  const button = $("comfyui-model-upload-btn");
  const status = $("comfyui-model-download-status");
  const fileInput = $("comfyui-model-upload-file");
  const file = fileInput?.files?.[0] || null;
  const type = $("comfyui-model-download-type")?.value || "checkpoint";
  const relativeDir = ($("comfyui-model-relative-path")?.value || "").trim();
  const baseDir = ($("comfyui-model-base-dir")?.value || "").trim();
  if (!file) {
    if (status) status.textContent = "請先選擇要上傳的模型檔案。";
    setComfyuiMessage("請先選擇要上傳的模型檔案。", false);
    return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = "上傳中...";
  }
  if (status) status.textContent = "正在上傳模型檔...";
  setComfyuiModelDownloadProgress({
    visible: true,
    running: true,
    percent: 0,
    label: "準備匯入",
    detail: `正在上傳 ${file.name} ...`
  });
  try {
    await fetchCsrfToken({ force: true });
    const form = new FormData();
    form.append("type", type);
    form.append("base_dir", baseDir);
    form.append("relative_dir", relativeDir);
    form.append("model_file", file);
    const res = await apiFetch(API + "/root/comfyui/model-upload", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": getCsrfToken() || "" },
      body: form,
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `模型上傳失敗（HTTP ${res.status}）`);
    const info = json.upload || {};
    if (status) status.textContent = `${json.msg || "模型已匯入"}（${formatDriveBytes(info.size_bytes || 0)}）`;
    setComfyuiModelDownloadProgress({
      visible: true,
      running: false,
      percent: 100,
      label: "模型已匯入",
      detail: `${info.filename || file.name} · ${formatDriveBytes(info.size_bytes || 0)}`
    });
    setComfyuiMessage(json.msg || "模型已匯入。請重新整理模型清單。", true);
    if (fileInput) fileInput.value = "";
    await loadComfyuiModels({ forceRefresh: true });
  } catch (err) {
    if (status) status.textContent = err.message || "模型上傳失敗";
    setComfyuiModelDownloadProgress({
      visible: true,
      running: false,
      percent: 100,
      label: "模型上傳失敗",
      detail: err.message || "模型上傳失敗"
    });
    setComfyuiMessage(err.message || "模型上傳失敗", false);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "上傳模型檔";
    }
  }
}

// =====================================================================
// ComfyUI Template Importer (Phase 1 of docs/comfyui/COMFYUI_TEMPLATE_IMPORTER_PLAN.md)
//
// Adds a small modal flow that uploads a ComfyUI workflow JSON,
// renders the §9 6-panel preview, and persists the result via /import.
//
// Wires into the existing #comfyui-template-import-btn button (index.html).
// All DOM is created on demand so this block is safe to append at the bottom
// of 36-comfyui.js without touching the existing form.
// =====================================================================

const ComfyUITemplateImporter = (() => {
  let modalEl = null;
  let currentToken = null;
  let currentCapability = null;
  let lastFocusedImporterTextInput = null;

  function ensureModal() {
    if (modalEl && document.body.contains(modalEl)) return modalEl;
    modalEl = document.createElement("div");
    modalEl.id = "comfyui-template-importer-modal";
    modalEl.className = "modal hidden";
    modalEl.style.position = "fixed";
    modalEl.style.inset = "0";
    modalEl.style.background = "rgba(0,0,0,0.55)";
    modalEl.style.zIndex = "9999";
    modalEl.style.display = "none";
    modalEl.innerHTML = `
      <div class="modal-card" style="max-width:720px;margin:5vh auto;background:var(--surface,#fff);color:var(--text,#000);border-radius:8px;max-height:88vh;overflow:auto;">
        <header style="padding:16px 20px;border-bottom:1px solid rgba(127,127,127,0.2);display:flex;justify-content:space-between;align-items:center;">
          <strong>匯入 ComfyUI workflow</strong>
          <button type="button" class="btn" id="comfyui-template-importer-close">關閉</button>
        </header>
        <section style="padding:16px 20px;">
          <p style="margin:0 0 8px;">支援 ComfyUI API format 與原生 workflow JSON（含 nodes/links）；系統會自動轉成可分析的 workflow。</p>
          <div style="display:flex;gap:8px;align-items:center;">
            <input type="file" id="comfyui-template-importer-file" accept=".json,application/json" />
            <button type="button" class="btn btn-primary" id="comfyui-template-importer-preview-btn">分析 workflow</button>
          </div>
          <div id="comfyui-template-importer-status" style="margin-top:8px;color:var(--muted,#666);"></div>
        </section>
        <section id="comfyui-template-importer-panels" style="padding:0 20px 16px;display:none;">
          <div id="comfyui-template-importer-capability" style="padding:8px 12px;border-radius:6px;margin-bottom:12px;"></div>
          <div id="comfyui-template-importer-panel-container"></div>
        </section>
        <footer id="comfyui-template-importer-footer" style="padding:12px 20px;border-top:1px solid rgba(127,127,127,0.2);display:none;justify-content:flex-end;gap:8px;">
          <input type="text" id="comfyui-template-importer-title" placeholder="preset 標題" maxlength="120" style="flex:1;padding:6px 10px;" />
          <button type="button" class="btn btn-primary" id="comfyui-template-importer-import-btn" disabled>儲存為 preset</button>
        </footer>
      </div>
    `;
    document.body.appendChild(modalEl);
    modalEl.querySelector("#comfyui-template-importer-close").addEventListener("click", closeImportModal);
    modalEl.querySelector("#comfyui-template-importer-preview-btn").addEventListener("click", submitTemplatePreview);
    modalEl.querySelector("#comfyui-template-importer-import-btn").addEventListener("click", submitTemplateImport);
    return modalEl;
  }

  function openImportModal() {
    ensureModal();
    currentToken = null;
    currentCapability = null;
    modalEl.style.display = "block";
    modalEl.classList.remove("hidden");
    setStatus("");
    const panels = modalEl.querySelector("#comfyui-template-importer-panels");
    const footer = modalEl.querySelector("#comfyui-template-importer-footer");
    if (panels) panels.style.display = "none";
    if (footer) footer.style.display = "none";
    const fileInput = modalEl.querySelector("#comfyui-template-importer-file");
    if (fileInput) fileInput.value = "";
    const titleInput = modalEl.querySelector("#comfyui-template-importer-title");
    if (titleInput) titleInput.value = "";
  }

  function closeImportModal() {
    if (!modalEl) return;
    modalEl.style.display = "none";
    modalEl.classList.add("hidden");
  }

  function setStatus(text, isError = false) {
    if (!modalEl) return;
    const el = modalEl.querySelector("#comfyui-template-importer-status");
    if (el) {
      el.textContent = text || "";
      el.style.color = isError ? "#c0392b" : "var(--muted,#666)";
    }
  }

  async function submitTemplatePreview() {
    if (!modalEl) return;
    const fileInput = modalEl.querySelector("#comfyui-template-importer-file");
    const file = fileInput && fileInput.files && fileInput.files[0];
    if (!file) {
      setStatus("請先選擇 workflow JSON 檔案", true);
      return;
    }
    setStatus("分析中...");
    try {
      await fetchCsrfToken({ force: true });
      const formData = new FormData();
      formData.append("workflow", file);
      const res = await apiFetch("/api/comfyui/templates/preview", {
        method: "POST",
        headers: { "X-CSRF-Token": getCsrfToken() || "" },
        body: formData,
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok || !json.ok) {
        throw new Error(json.msg || `分析失敗（HTTP ${res.status}）`);
      }
      currentToken = json.preview_token;
      currentCapability = json.capability;
      renderTemplatePanels(json.ui_schema || {}, json.capability || {});
      setStatus("分析完成；填好欄位後可匯入");
    } catch (err) {
      setStatus(err.message || "分析失敗", true);
    }
  }

  function insertTemplateModalEmbeddingToken(name) {
    if (!modalEl) return;
    const cleanName = String(name || "").trim();
    if (!cleanName) return;
    const textInputs = Array.from(modalEl.querySelectorAll("[data-comfyui-template-importer-input='1']"))
      .filter((el) => el.dataset.category === "TEXT" && el.dataset.inputName !== "filename_prefix");
    if (!textInputs.length) return;
    const active = document.activeElement;
    const target = textInputs.includes(active)
      ? active
      : (textInputs.includes(lastFocusedImporterTextInput) ? lastFocusedImporterTextInput : textInputs[0]);
    const embeddingTag = `<embeddings:${cleanName}>`;
    const removed = typeof removeComfyuiEmbeddingTokenFromInput === "function"
      ? removeComfyuiEmbeddingTokenFromInput(target, cleanName)
      : [];
    if (removed.length) {
      lastFocusedImporterTextInput = target;
      target.focus();
      return;
    }
    const raw = target.value || "";
    const start = Number.isInteger(target.selectionStart) ? target.selectionStart : raw.length;
    const end = Number.isInteger(target.selectionEnd) ? target.selectionEnd : raw.length;
    const prefix = start > 0 && !/[\s,\n]$/.test(raw.slice(0, start)) ? ", " : "";
    const suffix = end < raw.length && !/^[\s,]/.test(raw.slice(end)) ? " " : "";
    target.value = `${raw.slice(0, start)}${prefix}${embeddingTag}${suffix}${raw.slice(end)}`;
    const cursor = start + prefix.length + embeddingTag.length + suffix.length;
    lastFocusedImporterTextInput = target;
    target.focus();
    if (typeof target.setSelectionRange === "function") target.setSelectionRange(cursor, cursor);
  }

  function renderTemplatePanels(uiSchema, capability) {
    if (!modalEl) return;
    const panels = modalEl.querySelector("#comfyui-template-importer-panels");
    const container = modalEl.querySelector("#comfyui-template-importer-panel-container");
    const capEl = modalEl.querySelector("#comfyui-template-importer-capability");
    const importBtn = modalEl.querySelector("#comfyui-template-importer-import-btn");
    const footer = modalEl.querySelector("#comfyui-template-importer-footer");
    if (!container || !capEl || !panels || !footer || !importBtn) return;

    const overall = (capability && capability.overall) || "UNSUPPORTED";
    const capColor = overall === "SUPPORTED" ? "#27ae60"
      : overall === "PARTIALLY_SUPPORTED" ? "#e67e22"
      : "#c0392b";
    capEl.style.background = capColor + "22";
    capEl.style.borderLeft = `4px solid ${capColor}`;
    const blockers = (capability && capability.blockers) || [];
    capEl.innerHTML = `<strong>相容性：${overall}</strong>` +
      (blockers.length ? `<ul style="margin:6px 0 0 16px;">${blockers.map(b => `<li>${escapeHtmlSafe(b)}</li>`).join("")}</ul>` : "");

    container.innerHTML = "";
    const list = (uiSchema && uiSchema.panels) || [];
    list.forEach(panel => {
      const section = document.createElement("details");
      section.open = !panel.collapsed_default;
      section.style.borderBottom = "1px solid rgba(127,127,127,0.18)";
      section.style.padding = "8px 0";
      const summary = document.createElement("summary");
      summary.style.cursor = "pointer";
      summary.style.fontWeight = "600";
      summary.textContent = panel.label || panel.id;
      section.appendChild(summary);
      const fields = panel.fields || [];
      if (fields.length === 0 && panel.id !== "compatibility" && panel.id !== "raw") {
        const note = document.createElement("div");
        note.style.color = "var(--muted,#666)";
        note.style.padding = "6px 0";
        note.textContent = "（無可編輯欄位）";
        section.appendChild(note);
      }
      fields.forEach(field => {
        const row = document.createElement("div");
        if (field.input_type === "embedding_shortcuts") {
          const values = Array.isArray(comfyuiAvailableEmbeddings) ? comfyuiAvailableEmbeddings : [];
          if (!values.length) return;
          row.style.display = "block";
          row.style.padding = "6px 0";
          row.innerHTML = `<label style="display:block;margin-bottom:6px;color:var(--muted,#666);">${escapeHtmlSafe(field.label || "Embedding 快速插入")}</label>` +
            `<div class="comfyui-embedding-shortcuts">` +
            values.map(value => `<button class="comfyui-embedding-chip" type="button" data-comfyui-template-importer-embedding="${escapeHtmlSafe(value)}" title="插入 ${escapeHtmlSafe(value)}">${escapeHtmlSafe(value)}</button>`).join("") +
            `</div>`;
          section.appendChild(row);
          return;
        }
        row.style.display = "flex";
        row.style.gap = "8px";
        row.style.alignItems = "center";
        row.style.padding = "4px 0";
        const labelEl = document.createElement("label");
        labelEl.style.minWidth = "180px";
        labelEl.textContent = field.label || `${field.class_type}.${field.input_name}`;
        const inputEl = document.createElement(field.input_type === "textarea" ? "textarea" : "input");
        if (inputEl.tagName !== "TEXTAREA") {
          inputEl.type = field.input_type === "number" ? "number" : "text";
        } else {
          inputEl.rows = field?.constraints?.rows || 4;
        }
        inputEl.dataset.comfyuiTemplateImporterInput = "1";
        inputEl.dataset.fieldId = field.id || "";
        inputEl.dataset.nodeId = field.node_id || "";
        inputEl.dataset.classType = field.class_type || "";
        inputEl.dataset.inputName = field.input_name || "";
        inputEl.dataset.category = field.category || "";
        inputEl.dataset.label = field.label || "";
        inputEl.value = field.current_value != null ? String(field.current_value) : "";
        inputEl.style.flex = "1";
        inputEl.style.padding = "4px 8px";
        ["focus", "click", "keyup", "select", "mouseup", "touchend"].forEach((eventName) => {
          inputEl.addEventListener(eventName, () => {
            lastFocusedImporterTextInput = inputEl;
          });
        });
        row.appendChild(labelEl);
        row.appendChild(inputEl);
        section.appendChild(row);
      });
      container.appendChild(section);
    });
    container.querySelectorAll("[data-comfyui-template-importer-embedding]").forEach((button) => {
      button.addEventListener("click", () => insertTemplateModalEmbeddingToken(button.getAttribute("data-comfyui-template-importer-embedding")));
    });

    panels.style.display = "block";
    footer.style.display = "flex";
    importBtn.disabled = overall === "UNSUPPORTED";
  }

  async function submitTemplateImport() {
    if (!modalEl) return;
    if (!currentToken) {
      setStatus("尚未取得 preview_token，請先按「分析 workflow」", true);
      return;
    }
    const titleInput = modalEl.querySelector("#comfyui-template-importer-title");
    const title = (titleInput && titleInput.value || "").trim();
    if (!title) {
      setStatus("title 不可為空", true);
      return;
    }
    setStatus("匯入中...");
    try {
      await fetchCsrfToken({ force: true });
      const res = await apiFetch("/api/comfyui/templates/import", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": getCsrfToken() || "",
        },
        body: JSON.stringify({ preview_token: currentToken, title }),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok || !json.ok) {
        throw new Error(json.msg || `匯入失敗（HTTP ${res.status}）`);
      }
      const bundleId = json.bundle && json.bundle.id ? `，bundle ${json.bundle.id}` : "";
      setStatus(`已建立 preset #${json.preset_id}${bundleId}`);
      currentToken = null;
      // Refresh the existing preset list if the helper exists.
      if (typeof loadComfyuiWorkflowPresets === "function") {
        try { loadComfyuiWorkflowPresets(); } catch (_) {}
      }
      setTimeout(closeImportModal, 1200);
    } catch (err) {
      setStatus(err.message || "匯入失敗", true);
    }
  }

  function escapeHtmlSafe(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function bindButton() {
    const btn = document.getElementById("comfyui-template-import-btn");
    if (!btn || btn.dataset.cuiTemplateBound === "1") return;
    btn.dataset.cuiTemplateBound = "1";
    btn.addEventListener("click", openImportModal);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindButton);
  } else {
    bindButton();
  }

  return { openImportModal, closeImportModal, submitTemplatePreview, submitTemplateImport };
})();
