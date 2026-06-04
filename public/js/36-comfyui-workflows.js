let comfyuiTemplatePromptShareMode = "independent";
const COMFYUI_TEMPLATE_MEDIA_BINDING_KINDS = new Set(["image", "video"]);
const COMFYUI_TEMPLATE_VIDEO_ACCEPT = "video/mp4,video/webm,video/quicktime,video/x-matroska,video/x-msvideo,.mp4,.webm,.mov,.mkv,.avi";
const COMFYUI_BUILTIN_VAE_LABEL = "使用各自大模型內建 VAE";
const COMFYUI_COMPARE_TWO_CHECKPOINTS_ID = "origin_compare_2checkpoints";
const COMFYUI_MULTI_COMPARE_CHECKPOINTS_TEST_ID = "origin_multi_compare_checkpoints_test";
const COMFYUI_MULTI_METHOD_UPSCALE_ID = "origin_multi_method_upscale";
const COMFYUI_MULTI_METHOD_UPSCALE_MODE_TEST_ID = "origin_multi_method_upscale_mode_test";
const COMFYUI_COMPARE_SHARED_KSAMPLER_INPUTS = new Set(["seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"]);
const COMFYUI_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX = "official-template-media:";
const COMFYUI_MULTI_COMPARE_MAX_CHECKPOINTS = 14;
const COMFYUI_UPSCALE_BREAKPOINT_DEFAULT = "first_upscale";
const COMFYUI_UPSCALE_MODE_DEFAULT = "combined_upscale";

function comfyuiMultiCompareMaxLoras() {
  return typeof COMFYUI_MAX_LORAS === "number" ? COMFYUI_MAX_LORAS : 8;
}

function comfyuiWorkflowPresetById(presetId) {
  return comfyuiWorkflowPresets.find((item) => Number(item?.id) === Number(presetId)) || null;
}

function comfyuiWorkflowPaidApiNodes(detail) {
  const nodes = detail?.paid_api_nodes;
  return nodes && nodes.required && Array.isArray(nodes.nodes) ? nodes.nodes : [];
}

function comfyuiWorkflowPaidApiWarningHtml(detail) {
  const nodes = comfyuiWorkflowPaidApiNodes(detail);
  if (!nodes.length) return "";
  const labels = nodes.map((node) => `${node.node_id || "-"}:${node.class_type || node.title || "API node"}`).slice(0, 6);
  return `
    <div class="comfyui-workflow-paid-api-warning">
      可能使用 ComfyUI 付費/API node，執行前會要求確認。節點：${sanitize(labels.join(", "))}${nodes.length > labels.length ? `，另 ${sanitize(String(nodes.length - labels.length))} 個` : ""}
    </div>
  `;
}

function setComfyuiWorkflowStatus(text) {
  const status = $("comfyui-workflow-status");
  if (status) status.textContent = text || "";
}

function resetComfyuiWorkflowEditor({ keepStatus = false } = {}) {
  comfyuiWorkflowCurrentPresetId = null;
  comfyuiWorkflowEditorDefaults = null;
  setComfyuiFieldValue("comfyui-workflow-title", "");
  setComfyuiFieldValue("comfyui-workflow-description", "");
  setComfyuiFieldValue("comfyui-workflow-visibility", "private");
  setComfyuiFieldValue("comfyui-workflow-purpose", "txt2img");
  setComfyuiFieldValue("comfyui-workflow-comfyui-version", "");
  setComfyuiFieldValue("comfyui-workflow-project-version", "");
  setComfyuiFieldValue("comfyui-workflow-schema-version", "1");
  setComfyuiFieldValue("comfyui-workflow-json", "");
  setComfyuiFieldValue("comfyui-workflow-layout-json", "");
  const defaultInput = $("comfyui-workflow-is-default");
  if (defaultInput) defaultInput.checked = false;
  const fileInput = $("comfyui-workflow-file");
  if (fileInput) fileInput.value = "";
  const updateBtn = $("comfyui-workflow-update-btn");
  if (updateBtn) updateBtn.disabled = true;
  renderComfyuiWorkflowBuilderPreview();
  if (!keepStatus) setComfyuiWorkflowStatus("尚未選取 workflow preset");
}

function markComfyuiWorkflowEditorDirty() {
  const note = $("comfyui-workflow-editor-note");
  if (note) note.textContent = "有未儲存的版面修改；請按「新增版面」或「更新目前選擇」才會保存。";
}

const COMFYUI_WORKFLOW_NODE_TEMPLATES = {
  checkpoint_loader: {
    class_type: "CheckpointLoaderSimple",
    label: "Checkpoint Loader",
    inputs: { ckpt_name: "" },
  },
  positive_prompt: {
    class_type: "CLIPTextEncode",
    label: "Positive Prompt",
    inputs: { text: "masterpiece, best quality", clip: "" },
  },
  negative_prompt: {
    class_type: "CLIPTextEncode",
    label: "Negative Prompt",
    inputs: { text: "low quality, blurry", clip: "" },
  },
  ksampler: {
    class_type: "KSampler",
    label: "KSampler",
    inputs: { seed: 0, steps: 20, cfg: 7, sampler_name: "euler", scheduler: "normal" },
  },
  vae_decode: {
    class_type: "VAEDecode",
    label: "VAE Decode",
    inputs: { samples: "", vae: "" },
  },
  save_image: {
    class_type: "SaveImage",
    label: "Save Image",
    inputs: { filename_prefix: "hackme_web" },
  },
  load_image: {
    class_type: "LoadImage",
    label: "Load Image",
    inputs: { image: "" },
  },
  load_image_mask: {
    class_type: "LoadImageMask",
    label: "Load Mask",
    inputs: { image: "", channel: "alpha" },
  },
  vae_encode: {
    class_type: "VAEEncode",
    label: "VAE Encode",
    inputs: { pixels: "", vae: "" },
  },
  vae_encode_for_inpaint: {
    class_type: "VAEEncodeForInpaint",
    label: "VAE Encode Inpaint",
    inputs: { pixels: "", vae: "", mask: "", grow_mask_by: 6 },
  },
  image_pad_for_outpaint: {
    class_type: "ImagePadForOutpaint",
    label: "Outpaint Pad",
    inputs: { image: "", left: 0, top: 0, right: 0, bottom: 0, feathering: 40 },
  },
  lora_loader: {
    class_type: "LoraLoader",
    label: "LoRA Loader",
    inputs: { lora_name: "", strength_model: 1, strength_clip: 1, model: "", clip: "" },
  },
  ksampler_advanced: {
    class_type: "KSamplerAdvanced",
    label: "KSampler Advanced",
    inputs: { add_noise: "enable", noise_seed: 0, steps: 20, cfg: 7, sampler_name: "euler", scheduler: "normal", start_at_step: 0, end_at_step: 20, return_with_leftover_noise: "enable", model: "", positive: "", negative: "", latent_image: "" },
  },
  controlnet_loader: {
    class_type: "ControlNetLoader",
    label: "ControlNet Loader",
    inputs: { control_net_name: "" },
  },
  controlnet_apply_advanced: {
    class_type: "ControlNetApplyAdvanced",
    label: "ControlNet Apply Advanced",
    inputs: { positive: "", negative: "", control_net: "", image: "", strength: 1, start_percent: 0, end_percent: 1 },
  },
  upscale_model_loader: {
    class_type: "UpscaleModelLoader",
    label: "Upscale Model Loader",
    inputs: { model_name: "" },
  },
  image_upscale: {
    class_type: "ImageUpscaleWithModel",
    label: "Image Upscale",
    inputs: { upscale_model: "", image: "" },
  },
};

function parseComfyuiWorkflowEditorJson(fieldId, fallback) {
  const text = String($(fieldId)?.value || "").trim();
  if (!text) return fallback;
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : fallback;
  } catch (err) {
    throw new Error(`${fieldId === "comfyui-workflow-layout-json" ? "UI Layout JSON" : "Workflow JSON"} 格式錯誤：${err.message || err}`);
  }
}

function setComfyuiWorkflowEditorJson(workflow, layout) {
  setComfyuiFieldValue("comfyui-workflow-json", JSON.stringify(workflow || {}, null, 2));
  setComfyuiFieldValue("comfyui-workflow-layout-json", JSON.stringify(layout || {}, null, 2));
  renderComfyuiWorkflowBuilderPreview();
  markComfyuiWorkflowEditorDirty();
}

function nextComfyuiWorkflowNodeId(workflow) {
  const ids = Object.keys(workflow || {})
    .map((key) => Number(key))
    .filter((value) => Number.isFinite(value) && value > 0);
  return String((ids.length ? Math.max(...ids) : 0) + 1);
}

function normalizeComfyuiLayoutJson(layout) {
  const normalized = layout && typeof layout === "object" && !Array.isArray(layout) ? { ...layout } : {};
  if (!Array.isArray(normalized.node_order)) normalized.node_order = [];
  if (!normalized.node_positions || typeof normalized.node_positions !== "object" || Array.isArray(normalized.node_positions)) {
    normalized.node_positions = {};
  }
  if (!normalized.field_overrides || typeof normalized.field_overrides !== "object" || Array.isArray(normalized.field_overrides)) {
    normalized.field_overrides = {};
  }
  normalized.layout_schema_version = String(normalized.layout_schema_version || "1");
  return normalized;
}

function addComfyuiWorkflowNode(templateKey, label = "") {
  const template = COMFYUI_WORKFLOW_NODE_TEMPLATES[templateKey];
  if (!template) throw new Error("請選擇要追加的節點類型。");
  const workflow = parseComfyuiWorkflowEditorJson("comfyui-workflow-json", {});
  const layout = normalizeComfyuiLayoutJson(parseComfyuiWorkflowEditorJson("comfyui-workflow-layout-json", {}));
  const nodeId = nextComfyuiWorkflowNodeId(workflow);
  workflow[nodeId] = {
    class_type: template.class_type,
    inputs: { ...(template.inputs || {}) },
  };
  const cleanLabel = String(label || template.label || template.class_type).trim().slice(0, 80);
  if (cleanLabel) {
    workflow[nodeId]._meta = { title: cleanLabel };
    layout.field_overrides[nodeId] = { label: cleanLabel };
  }
  layout.node_order = layout.node_order.filter((item) => String(item) !== nodeId).concat([nodeId]);
  const index = layout.node_order.length - 1;
  layout.node_positions[nodeId] = [40 + (index % 3) * 280, 40 + Math.floor(index / 3) * 180];
  setComfyuiWorkflowEditorJson(workflow, layout);
  setComfyuiMessage(`已追加 ${template.label || template.class_type} 節點；尚未儲存。`, true);
}

function createBlankComfyuiWorkflowLayout() {
  comfyuiWorkflowCurrentPresetId = null;
  comfyuiWorkflowEditorDefaults = null;
  if (!$("comfyui-workflow-title")?.value) setComfyuiFieldValue("comfyui-workflow-title", "我的 ComfyUI 工作流版面");
  setComfyuiFieldValue("comfyui-workflow-purpose", "custom");
  setComfyuiWorkflowEditorJson({}, {
    layout_schema_version: "1",
    node_order: [],
    node_positions: {},
    field_overrides: {},
  });
  const updateBtn = $("comfyui-workflow-update-btn");
  if (updateBtn) updateBtn.disabled = true;
  setComfyuiWorkflowStatus("已建立空白版面草稿；追加節點或貼上 workflow JSON 後可新增保存。");
}

function createTxt2ImgComfyuiStarterWorkflow() {
  comfyuiWorkflowCurrentPresetId = null;
  comfyuiWorkflowEditorDefaults = {
    generation_mode: "txt2img",
    steps: 20,
    cfg: 7,
    sampler_name: "euler",
    scheduler: "normal",
    width: 1024,
    height: 1024,
  };
  if (!$("comfyui-workflow-title")?.value) setComfyuiFieldValue("comfyui-workflow-title", "txt2img 起始工作流");
  setComfyuiFieldValue("comfyui-workflow-purpose", "txt2img");
  const workflow = {
    "1": { class_type: "CheckpointLoaderSimple", inputs: { ckpt_name: "" }, _meta: { title: "主模型" } },
    "2": { class_type: "CLIPTextEncode", inputs: { clip: ["1", 1], text: "masterpiece, best quality" }, _meta: { title: "正向提示詞" } },
    "3": { class_type: "CLIPTextEncode", inputs: { clip: ["1", 1], text: "low quality, blurry" }, _meta: { title: "負向提示詞" } },
    "4": { class_type: "EmptyLatentImage", inputs: { width: 1024, height: 1024, batch_size: 1 }, _meta: { title: "畫布尺寸" } },
    "5": { class_type: "KSampler", inputs: { model: ["1", 0], positive: ["2", 0], negative: ["3", 0], latent_image: ["4", 0], seed: 0, steps: 20, cfg: 7, sampler_name: "euler", scheduler: "normal", denoise: 1 }, _meta: { title: "採樣器" } },
    "6": { class_type: "VAEDecode", inputs: { samples: ["5", 0], vae: ["1", 2] }, _meta: { title: "VAE 解碼" } },
    "7": { class_type: "SaveImage", inputs: { images: ["6", 0], filename_prefix: "hackme_web" }, _meta: { title: "儲存圖片" } },
  };
  const layout = {
    layout_schema_version: "1",
    node_order: ["1", "2", "3", "4", "5", "6", "7"],
    node_positions: {
      "1": [40, 40],
      "2": [320, 20],
      "3": [320, 200],
      "4": [320, 380],
      "5": [620, 160],
      "6": [900, 160],
      "7": [1180, 160],
    },
    field_overrides: {
      "1": { label: "主模型" },
      "2": { label: "正向提示詞" },
      "3": { label: "負向提示詞" },
      "4": { label: "畫布尺寸" },
      "5": { label: "Sampler / Scheduler / Steps / CFG / Seed" },
      "6": { label: "VAE 解碼" },
      "7": { label: "輸出檔名" },
    },
  };
  setComfyuiWorkflowEditorJson(workflow, layout);
  const updateBtn = $("comfyui-workflow-update-btn");
  if (updateBtn) updateBtn.disabled = true;
  setComfyuiWorkflowStatus("已建立 txt2img 起始版草稿；可繼續追加節點或新增保存。");
}

function renderComfyuiWorkflowBuilderPreview() {
  const preview = $("comfyui-workflow-builder-preview");
  if (!preview) return;
  let workflow = {};
  let layout = {};
  try {
    workflow = parseComfyuiWorkflowEditorJson("comfyui-workflow-json", {});
    layout = normalizeComfyuiLayoutJson(parseComfyuiWorkflowEditorJson("comfyui-workflow-layout-json", {}));
  } catch (err) {
    preview.textContent = err.message || "Workflow JSON 尚無法預覽。";
    return;
  }
  const ids = Object.keys(workflow || {});
  if (!ids.length) {
    preview.textContent = "目前尚未建立節點。";
    return;
  }
  const ordered = (layout.node_order || []).filter((id) => workflow[id]).concat(ids.filter((id) => !(layout.node_order || []).includes(id)));
  preview.innerHTML = `
    <div class="comfyui-workflow-flags">
      <span class="comfyui-workflow-chip">節點 ${sanitize(String(ids.length))}</span>
      <span class="comfyui-workflow-chip">版面位置 ${sanitize(String(Object.keys(layout.node_positions || {}).length))}</span>
    </div>
    <div class="comfyui-workflow-builder-node-list">
      ${ordered.slice(0, 12).map((id) => {
        const node = workflow[id] || {};
        const title = node?._meta?.title || layout.field_overrides?.[id]?.label || node.class_type || `Node ${id}`;
        return `<span class="comfyui-workflow-chip">${sanitize(id)} · ${sanitize(title)}</span>`;
      }).join("")}
      ${ordered.length > 12 ? `<span class="comfyui-workflow-chip">另 ${sanitize(String(ordered.length - 12))} 個</span>` : ""}
    </div>
  `;
}

function comfyuiWorkflowEditorPayload() {
  return {
    title: $("comfyui-workflow-title")?.value || "",
    description: $("comfyui-workflow-description")?.value || "",
    visibility: $("comfyui-workflow-visibility")?.value || "private",
    purpose: $("comfyui-workflow-purpose")?.value || "custom",
    comfyui_version: $("comfyui-workflow-comfyui-version")?.value || "",
    project_version: $("comfyui-workflow-project-version")?.value || "",
    workflow_schema_version: $("comfyui-workflow-schema-version")?.value || "1",
    layout_json: $("comfyui-workflow-layout-json")?.value || undefined,
    is_default: !!$("comfyui-workflow-is-default")?.checked,
  };
}

function downloadComfyuiWorkflowText(filename, text) {
  const blob = new Blob([String(text || "")], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "comfyui-workflow.json";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function comfyuiWorkflowEditorStorageKey(key) {
  if (typeof comfyuiUserStorageKey === "function") return comfyuiUserStorageKey(key);
  if (typeof accountScopedStorageKey === "function") return accountScopedStorageKey(key);
  const id = Number(currentUserId || 0);
  const scope = Number.isFinite(id) && id > 0
    ? `user:${id}`
    : (currentUser ? `name:${String(currentUser).trim().toLowerCase()}` : "anonymous");
  return `hackme_web:${scope}:${String(key || "state")}`;
}

function loadComfyuiVisualWorkflowEditorResult() {
  let payload = null;
  try {
    payload = JSON.parse(localStorage.getItem(comfyuiWorkflowEditorStorageKey("hackme_comfyui_workflow_editor_result")) || "null");
  } catch (_) {
    payload = null;
  }
  if (!payload || typeof payload !== "object" || !payload.workflow_json) {
    setComfyuiMessage("尚未找到視覺 Workflow 編輯器結果。請先開啟視覺編輯器並按「送回主頁」。", false);
    return;
  }
  setComfyuiFieldValue("comfyui-workflow-title", payload.name || payload.title || "");
  setComfyuiFieldValue("comfyui-workflow-description", payload.description || "");
  setComfyuiFieldValue("comfyui-workflow-purpose", payload.purpose || "custom");
  setComfyuiFieldValue("comfyui-workflow-schema-version", payload.workflow_schema_version || "1");
  setComfyuiFieldValue("comfyui-workflow-json", JSON.stringify(payload.workflow_json || {}, null, 2));
  setComfyuiFieldValue("comfyui-workflow-layout-json", JSON.stringify(payload.layout_json || {}, null, 2));
  comfyuiWorkflowEditorDefaults = null;
  renderComfyuiWorkflowBuilderPreview();
  markComfyuiWorkflowEditorDirty();
  setComfyuiMessage("已載入視覺 Workflow 編輯器結果；按「新增版面」即可保存。", true);
}

function prepareComfyuiVisualWorkflowEditorInput() {
  let workflow = {};
  let layout = {};
  try {
    workflow = parseComfyuiWorkflowEditorJson("comfyui-workflow-json", {});
    layout = normalizeComfyuiLayoutJson(parseComfyuiWorkflowEditorJson("comfyui-workflow-layout-json", {}));
  } catch (err) {
    setComfyuiMessage(err.message || "目前 workflow JSON 無法送入視覺編輯器", false);
    return;
  }
  const payload = {
    name: $("comfyui-workflow-title")?.value || "ComfyUI 工作流版面",
    description: $("comfyui-workflow-description")?.value || "",
    purpose: $("comfyui-workflow-purpose")?.value || "custom",
    project_version: $("comfyui-workflow-project-version")?.value || "",
    comfyui_version: $("comfyui-workflow-comfyui-version")?.value || "",
    workflow_schema_version: $("comfyui-workflow-schema-version")?.value || "1",
    workflow_json: workflow,
    layout_json: layout,
  };
  try {
    localStorage.setItem(comfyuiWorkflowEditorStorageKey("hackme_comfyui_workflow_editor_input"), JSON.stringify(payload));
  } catch (_) {
    setComfyuiMessage("瀏覽器無法暫存 workflow 給視覺編輯器；請改用編輯器內匯入 JSON。", false);
    return;
  }
  setComfyuiWorkflowStatus("已把目前 workflow 暫存給視覺節點編輯器。");
}

function comfyuiWorkflowDependencyHtml(status) {
  if (!status) return '<div class="drive-card-sub">尚未檢查目前節點與模型依賴。</div>';
  const chips = [];
  if (status.available) {
    chips.push('<span class="comfyui-workflow-chip">依賴可用</span>');
  } else {
    chips.push('<span class="comfyui-workflow-chip bad">缺少依賴</span>');
  }
  if (Array.isArray(status.missing_nodes) && status.missing_nodes.length) {
    chips.push(`<span class="comfyui-workflow-chip bad">缺少 node ${sanitize(status.missing_nodes.length)}</span>`);
  }
  if (Array.isArray(status.missing_models) && status.missing_models.length) {
    chips.push(`<span class="comfyui-workflow-chip bad">缺少模型 ${sanitize(status.missing_models.length)}</span>`);
  }
  if (Array.isArray(status.missing_loras) && status.missing_loras.length) {
    chips.push(`<span class="comfyui-workflow-chip bad">缺少 LoRA ${sanitize(status.missing_loras.length)}</span>`);
  }
  if (Array.isArray(status.missing_controlnets) && status.missing_controlnets.length) {
    chips.push(`<span class="comfyui-workflow-chip bad">缺少 ControlNet ${sanitize(status.missing_controlnets.length)}</span>`);
  }
  const issues = Array.isArray(status.issues) && status.issues.length
    ? `<div class="drive-card-sub">${sanitize(status.issues.join("；"))}</div>`
    : '<div class="drive-card-sub">目前沒有偵測到缺少的 workflow node、模型、LoRA 或 ControlNet。</div>';
  return `<div class="comfyui-workflow-flags">${chips.join("")}</div>${issues}`;
}

function renderComfyuiWorkflowRunList(runs = []) {
  if (!Array.isArray(runs) || !runs.length) {
    return '<div class="drive-card-sub">尚無最近執行結果</div>';
  }
  return `<div class="comfyui-workflow-run-list">${runs.map((run) => {
    const params = run?.params || {};
    const summary = [
      params.seed !== undefined && params.seed !== null ? `seed ${params.seed}` : "",
      params.steps ? `steps ${params.steps}` : "",
      params.cfg ? `CFG ${params.cfg}` : "",
      params.controlnet?.type ? `ControlNet ${String(params.controlnet.type).toUpperCase()}` : "",
    ].filter(Boolean).join(" · ");
    return `
      <div class="comfyui-workflow-run-item">
        <strong>${sanitize(String(run.status || "queued"))}</strong>
        <span> · ${sanitize(String(run.created_at || "").replace("T", " ").slice(0, 16))}</span>
        <div>${sanitize(summary || "未保存額外參數摘要")}</div>
        ${run.error ? `<div class="drive-card-sub">${sanitize(run.error)}</div>` : ""}
      </div>
    `;
  }).join("")}</div>`;
}

function comfyuiTemplateSelectGroups(payload = {}) {
  return [
    { label: "官方模板", items: Array.isArray(payload.official_presets) ? payload.official_presets : [] },
    { label: "我的模板", items: Array.isArray(payload.my_presets) ? payload.my_presets : [] },
    { label: "公開模板", items: Array.isArray(payload.shared_presets) ? payload.shared_presets : [] },
  ].filter((group) => group.items.length);
}

let comfyuiTemplateRenderTimer = null;
let comfyuiTemplateLoraOverrides = {};
let comfyuiTemplateFieldOverrides = {};
let comfyuiTemplateEditableModelFields = {};
let comfyuiMultiCompareState = { bundleId: "", checkpoints: [], loras: [] };
let comfyuiUpscaleBreakpointState = { bundleId: "", stage: COMFYUI_UPSCALE_BREAKPOINT_DEFAULT };

function queueRenderSelectedComfyuiTemplate() {
  if (comfyuiTemplateRenderTimer) clearTimeout(comfyuiTemplateRenderTimer);
  comfyuiTemplateRenderTimer = setTimeout(() => {
    comfyuiTemplateRenderTimer = null;
    renderSelectedComfyuiTemplate();
  }, 0);
}

function renderComfyuiTemplateSelector(payload = {}, { silentReload = true } = {}) {
  const select = $("comfyui-template-select");
  if (!select) return;
  const previous = String(comfyuiSelectedTemplatePresetId || select.value || "").trim();
  const groups = comfyuiTemplateSelectGroups(payload);
  const options = ['<option value="">先選擇模板</option>'].concat(groups.map((group) => `
    <optgroup label="${sanitize(group.label)}">
      ${group.items.map((item) => `
        <option value="${sanitize(String(item.id))}">
          ${sanitize(item.title || `Workflow #${item.id}`)}
        </option>
      `).join("")}
    </optgroup>
  `));
  select.innerHTML = options.join("");
  const exists = comfyuiWorkflowPresets.some((item) => String(item?.id || "") === previous);
  select.value = exists ? previous : "";
  comfyuiSelectedTemplatePresetId = select.value ? Number(select.value) : null;
  if (!comfyuiSelectedTemplatePresetId) {
    comfyuiSelectedTemplateDetail = null;
  }
  if (select.dataset.comfyuiTemplateBound !== "1") {
    select.dataset.comfyuiTemplateBound = "1";
    select.addEventListener("change", () => {
      const presetId = Number(select.value || 0);
      if (!presetId) {
        comfyuiSelectedTemplatePresetId = null;
        comfyuiSelectedTemplateDetail = null;
        comfyuiTemplateLoraOverrides = {};
        comfyuiTemplateFieldOverrides = {};
        comfyuiTemplateEditableModelFields = {};
        comfyuiMultiCompareState = { bundleId: "", checkpoints: [], loras: [] };
        comfyuiUpscaleBreakpointState = { bundleId: "", stage: COMFYUI_UPSCALE_BREAKPOINT_DEFAULT };
        comfyuiTemplatePromptShareMode = "independent";
        if (typeof updateComfyuiPreviewCardForOutputKinds === "function") updateComfyuiPreviewCardForOutputKinds(["image"]);
        renderSelectedComfyuiTemplate();
        return;
      }
      loadComfyuiSelectedTemplateDetail(presetId, { silent: false }).catch((err) => {
        setComfyuiMessage(err.message || "模板讀取失敗", false);
      });
    });
  }
  if (comfyuiSelectedTemplatePresetId) {
    const isSameDetail = Number(comfyuiSelectedTemplateDetail?.id || 0) === Number(comfyuiSelectedTemplatePresetId || 0);
    if (silentReload && isSameDetail) {
      renderSelectedComfyuiTemplate();
    } else {
      loadComfyuiSelectedTemplateDetail(comfyuiSelectedTemplatePresetId, {
        silent: silentReload,
        applyDefaults: !silentReload,
      }).catch(() => {});
    }
  } else {
    if (typeof updateComfyuiPreviewCardForOutputKinds === "function") updateComfyuiPreviewCardForOutputKinds(["image"]);
    renderSelectedComfyuiTemplate();
  }
}

async function loadComfyuiSelectedTemplateDetail(presetId, { silent = false, applyDefaults = true } = {}) {
  if (!presetId) return;
  await fetchCsrfToken();
  const res = await apiFetch(API + `/comfyui/workflows/${encodeURIComponent(presetId)}`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `模板讀取失敗（HTTP ${res.status}）`);
  comfyuiSelectedTemplatePresetId = Number(presetId);
  comfyuiTemplateLoraOverrides = {};
  comfyuiTemplateFieldOverrides = {};
  comfyuiTemplateEditableModelFields = {};
  comfyuiSelectedTemplateDetail = json.preset || null;
  resetComfyuiMultiCompareState(comfyuiSelectedTemplateDetail);
  resetComfyuiUpscaleBreakpointState(comfyuiSelectedTemplateDetail);
  comfyuiTemplatePromptShareMode = comfyuiTemplateNeedsPromptSharingChoice(comfyuiSelectedTemplateDetail) ? "shared" : "independent";
  if (applyDefaults) {
    applyComfyuiWorkflowPresetDefaults(comfyuiSelectedTemplateDetail?.default_params || {});
  }
  if (typeof updateComfyuiPreviewCardForOutputKinds === "function") updateComfyuiPreviewCardForOutputKinds();
  renderSelectedComfyuiTemplate();
  if (!silent) {
    setComfyuiMessage(`已切換到模板「${comfyuiSelectedTemplateDetail?.title || `Workflow #${presetId}`}」`, true);
  }
}

function comfyuiWorkflowPresetMode(item = {}) {
  const mode = String(item?.default_params?.generation_mode || item?.purpose || "").trim().toLowerCase();
  if (typeof normalizeComfyuiGenerationModeAlias === "function") return normalizeComfyuiGenerationModeAlias(mode);
  return mode === "t2a" ? "t2s" : mode;
}

function comfyuiWorkflowOutputKind(kind) {
  if (typeof comfyuiNormalizeOutputKind === "function") return comfyuiNormalizeOutputKind(kind);
  const normalized = String(kind || "").trim().toLowerCase();
  if (["audio", "audios", "music", "song", "songs", "sound", "sounds"].includes(normalized)) return "audio";
  if (["video", "videos", "gif", "gifs"].includes(normalized)) return "video";
  if (["image", "images"].includes(normalized)) return "image";
  return normalized;
}

function comfyuiWorkflowPresetOutputKinds(item = {}) {
  return Array.isArray(item?.output_kinds)
    ? item.output_kinds.map((kind) => comfyuiWorkflowOutputKind(kind)).filter(Boolean)
    : [];
}

function comfyuiWorkflowPresetForegroundTimeoutSeconds(item = {}) {
  const baseTimeout = typeof COMFYUI_GENERATION_TIMEOUT_SECONDS === "number" ? COMFYUI_GENERATION_TIMEOUT_SECONDS : 0;
  const videoTimeout = typeof COMFYUI_VIDEO_FOREGROUND_TIMEOUT_SECONDS === "number"
    ? COMFYUI_VIDEO_FOREGROUND_TIMEOUT_SECONDS
    : baseTimeout;
  const outputs = comfyuiWorkflowPresetOutputKinds(item);
  return outputs.includes("video") ? videoTimeout : baseTimeout;
}

function comfyuiWorkflowPresetSupportsMode(item = {}, mode = "") {
  const normalized = typeof normalizeComfyuiGenerationModeAlias === "function"
    ? normalizeComfyuiGenerationModeAlias(mode)
    : String(mode || "").trim().toLowerCase();
  const presetMode = comfyuiWorkflowPresetMode(item);
  if (presetMode && presetMode !== "custom") return presetMode === normalized;
  const outputs = comfyuiWorkflowPresetOutputKinds(item);
  if (["t2v", "i2v", "v2v", "t2sv"].includes(normalized)) return outputs.includes("video");
  if (normalized === "t2s") return outputs.includes("audio");
  return false;
}

function comfyuiWorkflowPresetsForMode(mode) {
  return comfyuiWorkflowPresets.filter((item) => comfyuiWorkflowPresetSupportsMode(item, mode));
}

async function runSelectedComfyuiWorkflowTemplateFromGenerate(mode) {
  let normalized = typeof normalizeComfyuiGenerationModeAlias === "function"
    ? normalizeComfyuiGenerationModeAlias(mode)
    : String(mode || "").trim().toLowerCase();
  let label = comfyuiReadableModeLabel(normalized);
  const select = $("comfyui-template-select");
  if (!comfyuiWorkflowPresets.length && typeof loadComfyuiWorkflowPresets === "function") {
    await loadComfyuiWorkflowPresets({ silentTemplateReload: true });
  }
  let presetId = Number(comfyuiSelectedTemplatePresetId || select?.value || 0);
  let autoSelected = false;
  if (!presetId) {
    const matches = comfyuiWorkflowPresetsForMode(normalized);
    if (matches.length === 1) {
      presetId = Number(matches[0]?.id || 0);
      autoSelected = !!presetId;
      comfyuiSelectedTemplatePresetId = presetId || null;
      if (select && presetId) select.value = String(presetId);
    }
  }
  if (!presetId) {
    setComfyuiMessage(`「${label}」需要先在上方 Workflow 模板選擇支援的大模型工作流，再按產生。`, false);
    if (select) select.focus();
    return false;
  }
  if (Number(comfyuiSelectedTemplateDetail?.id || 0) !== Number(presetId)) {
    await loadComfyuiSelectedTemplateDetail(presetId, { silent: !autoSelected, applyDefaults: autoSelected });
  }
  const detail = Number(comfyuiSelectedTemplateDetail?.id || 0) === Number(presetId)
    ? comfyuiSelectedTemplateDetail
    : comfyuiWorkflowPresetById(presetId);
  const detailMode = comfyuiWorkflowPresetMode(detail);
  if (detailMode && detailMode !== "custom") {
    normalized = detailMode;
    label = comfyuiReadableModeLabel(normalized);
    const hiddenMode = $("comfyui-generation-mode");
    if (hiddenMode) hiddenMode.value = normalized;
  } else {
    const outputs = comfyuiWorkflowPresetOutputKinds(detail);
    if (outputs.includes("audio")) normalized = outputs.includes("video") ? "t2sv" : "t2s";
    else if (outputs.includes("video")) normalized = typeof comfyuiHasInputAsset === "function" && comfyuiHasInputAsset("source") ? "i2v" : "t2v";
    label = comfyuiReadableModeLabel(normalized);
  }
  if (!comfyuiWorkflowPresetSupportsMode(detail, normalized)) {
    const selectedMode = comfyuiWorkflowPresetMode(detail);
    const selectedLabel = selectedMode ? comfyuiReadableModeLabel(selectedMode) : "未標示模式";
    setComfyuiMessage(`目前選擇的 Workflow 模板是「${selectedLabel}」，不能執行「${label}」。請改選支援「${label}」的模板。`, false);
    if (select) select.focus();
    return false;
  }
  await runComfyuiWorkflowPreset(presetId);
  return true;
}

function comfyuiTemplateInputKinds(detail) {
  const counts = { text: 0, image: 0, video: 0, parameter: 0 };
  const panels = Array.isArray(detail?.ui_schema?.panels) ? detail.ui_schema.panels : [];
  panels.forEach((panel) => {
    (panel?.fields || []).forEach((field) => {
      if (field?.synthetic || field?.input_type === "embedding_shortcuts") return;
      const category = String(field?.category || "").trim().toUpperCase();
      if (category === "TEXT") counts.text += 1;
      else if (category === "IMAGE" || field?.input_type === "file_picker") counts.image += 1;
      else if (category === "VIDEO" || field?.input_type === "video_file_picker") counts.video += 1;
      else counts.parameter += 1;
    });
  });
  return [
    counts.text ? `文字 ${counts.text}` : "",
    counts.image ? `圖片 ${counts.image}` : "",
    counts.video ? `影片 ${counts.video}` : "",
    counts.parameter ? `參數 ${counts.parameter}` : "",
  ].filter(Boolean);
}

function comfyuiWorkflowModelKindLabel(kind = "") {
  const normalized = String(kind || "").trim().toLowerCase();
  if (["ckpt", "checkpoint", "model"].includes(normalized)) return "Checkpoint";
  if (["diffusion_model", "unet"].includes(normalized)) return "Diffusion / UNet";
  if (["clip", "text_encoder"].includes(normalized)) return "Text Encoder / CLIP";
  if (["clip_vision", "clipvision"].includes(normalized)) return "CLIP Vision";
  if (normalized === "vae") return "VAE";
  if (normalized === "lora") return "LoRA";
  if (normalized === "controlnet") return "ControlNet";
  if (normalized === "embedding") return "Embedding";
  if (["upscale", "upscale_model", "upscale_models"].includes(normalized)) return "Upscale";
  if (["latent_upscale", "latent_upscale_model", "latent_upscale_models"].includes(normalized)) return "Latent Upscale";
  return kind ? String(kind) : "模型";
}

function comfyuiWorkflowDefaultModelEntries(item = {}) {
  const defaults = item?.default_params || {};
  const entries = [];
  const seen = new Set();
  const addEntry = (kind, value) => {
    const text = String(value || "").trim();
    if (!text) return;
    const key = `${String(kind || "").toLowerCase()}:${text.toLowerCase()}`;
    if (seen.has(key)) return;
    seen.add(key);
    entries.push({ kind: comfyuiWorkflowModelKindLabel(kind), name: text });
  };
  const add = (kind, value) => {
    const text = String(value || "").trim();
    if (!text || text === COMFYUI_VAE_BUILTIN) return;
    addEntry(kind, text);
  };
  add("checkpoint", defaults.model);
  add("diffusion_model", defaults.diffusion_model);
  add("clip", defaults.clip);
  if (comfyuiWorkflowUsesBuiltinVae(defaults)) addEntry("vae", COMFYUI_BUILTIN_VAE_LABEL);
  else add("vae", defaults.vae);
  add("upscale", defaults.upscale_model);
  if (defaults.controlnet?.model_name) add("controlnet", defaults.controlnet.model_name);
  (Array.isArray(defaults.loras) ? defaults.loras : []).forEach((entry) => add("lora", entry?.name || entry));
  (Array.isArray(item?.required_models) ? item.required_models : []).forEach((entry) => add(entry?.kind || "model", entry?.name || entry));
  (Array.isArray(item?.required_loras) ? item.required_loras : []).forEach((entry) => add("lora", entry?.name || entry));
  (Array.isArray(item?.required_controlnets) ? item.required_controlnets : []).forEach((entry) => add("controlnet", entry?.name || entry));
  return entries;
}

function comfyuiWorkflowUsesBuiltinVae(defaults = {}) {
  if (!defaults || typeof defaults !== "object") return false;
  const rawVae = String(defaults.vae ?? "").trim();
  if (rawVae && rawVae !== COMFYUI_VAE_BUILTIN) return false;
  return !!String(defaults.model || defaults.checkpoint || defaults.diffusion_model || "").trim();
}

function comfyuiWorkflowDefaultModelSummaryText(item = {}, limit = 4) {
  const entries = comfyuiWorkflowDefaultModelEntries(item);
  if (!entries.length) return "預設模型：未在模板中標示";
  const visible = entries.slice(0, Math.max(1, Number(limit) || 4));
  const text = visible.map((entry) => `${entry.kind}: ${entry.name}`).join("、");
  const hiddenCount = entries.length - visible.length;
  return `預設模型：${text}${hiddenCount > 0 ? `，另 ${hiddenCount} 個` : ""}`;
}

function comfyuiWorkflowDefaultModelNoticeHtml(item = {}, limit = 4) {
  return `<div class="comfyui-workflow-default-model-notice">${sanitize(comfyuiWorkflowDefaultModelSummaryText(item, limit))}</div>`;
}

function comfyuiTemplateBundleId(detail = comfyuiSelectedTemplateDetail) {
  return String(detail?.system_bundle_id || detail?.manifest_json?.id || "").trim();
}

function comfyuiTemplateIsCompareTwoCheckpoints(detail = comfyuiSelectedTemplateDetail) {
  return comfyuiTemplateBundleId(detail) === COMFYUI_COMPARE_TWO_CHECKPOINTS_ID
    || String(detail?.title || detail?.name || "").trim() === "Compare Two Checkpoints";
}

function comfyuiTemplateIsMultiCompareCheckpoints(detail = comfyuiSelectedTemplateDetail) {
  return comfyuiTemplateBundleId(detail) === COMFYUI_MULTI_COMPARE_CHECKPOINTS_TEST_ID
    || String(detail?.title || detail?.name || "").trim() === "Multi-Compare Checkpoints Test";
}

function comfyuiTemplateIsMultiMethodUpscale(detail = comfyuiSelectedTemplateDetail) {
  return comfyuiTemplateBundleId(detail) === COMFYUI_MULTI_METHOD_UPSCALE_ID
    || comfyuiTemplateBundleId(detail) === COMFYUI_MULTI_METHOD_UPSCALE_MODE_TEST_ID
    || String(detail?.title || detail?.name || "").trim() === "Multi-Method Upscale Utility";
}

function comfyuiTemplateIsMultiMethodUpscaleModeTest(detail = comfyuiSelectedTemplateDetail) {
  return comfyuiTemplateBundleId(detail) === COMFYUI_MULTI_METHOD_UPSCALE_MODE_TEST_ID
    || String(detail?.title || detail?.name || "").trim() === "Multi-Method Upscale Utility - Mode Test";
}

function normalizeComfyuiUpscaleBreakpointValue(detail, value) {
  const raw = String(value || "").trim();
  if (comfyuiTemplateIsMultiMethodUpscaleModeTest(detail)) {
    if (["model_upscale", "latent_upscale", "combined_upscale"].includes(raw)) return raw;
    if (raw === "first_upscale") return "latent_upscale";
    if (raw === "second_upscale") return "combined_upscale";
    return COMFYUI_UPSCALE_MODE_DEFAULT;
  }
  return raw === "second_upscale" ? "second_upscale" : COMFYUI_UPSCALE_BREAKPOINT_DEFAULT;
}

function resetComfyuiUpscaleBreakpointState(detail = comfyuiSelectedTemplateDetail) {
  const bundleId = comfyuiTemplateBundleId(detail);
  if (!comfyuiTemplateIsMultiMethodUpscale(detail)) {
    comfyuiUpscaleBreakpointState = { bundleId: "", stage: COMFYUI_UPSCALE_BREAKPOINT_DEFAULT };
    return;
  }
  const existingStage = String(detail?.default_params?.upscale_mode || detail?.default_params?.upscale_breakpoint || "");
  comfyuiUpscaleBreakpointState = {
    bundleId,
    stage: normalizeComfyuiUpscaleBreakpointValue(detail, existingStage),
  };
}

function ensureComfyuiUpscaleBreakpointState(detail = comfyuiSelectedTemplateDetail) {
  const bundleId = comfyuiTemplateBundleId(detail);
  if (!comfyuiTemplateIsMultiMethodUpscale(detail)) return null;
  if (comfyuiUpscaleBreakpointState.bundleId !== bundleId) resetComfyuiUpscaleBreakpointState(detail);
  return comfyuiUpscaleBreakpointState;
}

function comfyuiUpscaleBreakpointStage(detail = comfyuiSelectedTemplateDetail) {
  return ensureComfyuiUpscaleBreakpointState(detail)?.stage || COMFYUI_UPSCALE_BREAKPOINT_DEFAULT;
}

function comfyuiTemplateCheckpointFields(detail = comfyuiSelectedTemplateDetail) {
  return comfyuiTemplateAllFields(detail).filter((field) => (
    field?.class_type === "CheckpointLoaderSimple" && field?.input_name === "ckpt_name"
  ));
}

function resetComfyuiMultiCompareState(detail = comfyuiSelectedTemplateDetail) {
  const bundleId = comfyuiTemplateBundleId(detail);
  if (!comfyuiTemplateIsMultiCompareCheckpoints(detail)) {
    comfyuiMultiCompareState = { bundleId: "", checkpoints: [], loras: [] };
    return;
  }
  const checkpointFields = comfyuiTemplateCheckpointFields(detail);
  const checkpoints = checkpointFields
    .map((field) => String(field?.current_value || "").trim())
    .filter(Boolean)
    .slice(0, COMFYUI_MULTI_COMPARE_MAX_CHECKPOINTS);
  while (checkpoints.length < 2) checkpoints.push("");
  comfyuiMultiCompareState = { bundleId, checkpoints, loras: [] };
}

function ensureComfyuiMultiCompareState(detail = comfyuiSelectedTemplateDetail) {
  const bundleId = comfyuiTemplateBundleId(detail);
  if (!comfyuiTemplateIsMultiCompareCheckpoints(detail)) return null;
  if (comfyuiMultiCompareState.bundleId !== bundleId || comfyuiMultiCompareState.checkpoints.length < 2) {
    resetComfyuiMultiCompareState(detail);
  }
  return comfyuiMultiCompareState;
}

function comfyuiWorkflowLoraNamesForPromptSync() {
  const names = [];
  const addName = (name) => {
    const cleanName = typeof normalizeComfyuiLoraName === "function"
      ? normalizeComfyuiLoraName(name)
      : String(name || "").trim();
    if (cleanName) names.push(cleanName);
  };
  if (comfyuiTemplateIsMultiCompareCheckpoints(comfyuiSelectedTemplateDetail)) {
    const state = ensureComfyuiMultiCompareState(comfyuiSelectedTemplateDetail);
    (state?.loras || []).forEach((item) => addName(item?.name));
  }
  return names;
}

function comfyuiMultiCompareLoraTrainedWords(name) {
  const cleanName = typeof normalizeComfyuiLoraName === "function"
    ? normalizeComfyuiLoraName(name)
    : String(name || "").trim();
  const detail = cleanName ? (comfyuiLoraDetails?.[cleanName] || {}) : {};
  return Array.isArray(detail.trained_words)
    ? detail.trained_words.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
}

function comfyuiActiveLoraTriggerWords() {
  const needed = new Set();
  const addName = (name) => {
    comfyuiMultiCompareLoraTrainedWords(name).forEach((term) => {
      needed.add(term.toLowerCase());
    });
  };
  (Array.isArray(comfyuiSelectedLoras) ? comfyuiSelectedLoras : []).forEach((item) => addName(item?.name));
  Object.values(comfyuiTemplateLoraOverrides || {}).forEach((name) => addName(name));
  comfyuiWorkflowLoraNamesForPromptSync().forEach((name) => addName(name));
  return needed;
}

function applyComfyuiMultiCompareLoraPromptTerms(name) {
  if (typeof applyComfyuiPromptTerms !== "function") return [];
  return applyComfyuiPromptTerms(comfyuiMultiCompareLoraTrainedWords(name));
}

function removeComfyuiMultiCompareLoraPromptTerms(name) {
  if (typeof removeComfyuiPromptTerms !== "function") return [];
  const trainedWords = comfyuiMultiCompareLoraTrainedWords(name);
  if (!trainedWords.length) return [];
  const stillNeeded = comfyuiActiveLoraTriggerWords();
  const removableTerms = trainedWords.filter((term) => !stillNeeded.has(term.toLowerCase()));
  return removableTerms.length ? removeComfyuiPromptTerms(removableTerms, { promptType: "prompt" }) : [];
}

function comfyuiDefaultMultiCompareLoraName(state) {
  const selectedNames = new Set((state?.loras || []).map((item) => (
    typeof normalizeComfyuiLoraName === "function"
      ? normalizeComfyuiLoraName(item?.name)
      : String(item?.name || "").trim()
  )).filter(Boolean));
  const currentSelectName = typeof normalizeComfyuiLoraName === "function"
    ? normalizeComfyuiLoraName($("comfyui-lora-select")?.value || "")
    : String($("comfyui-lora-select")?.value || "").trim();
  if (currentSelectName && !selectedNames.has(currentSelectName)) return currentSelectName;
  const options = comfyuiTemplateLoraSelectOptions({});
  const available = options.filter((option) => option.value && !option.disabled);
  return available.find((option) => !selectedNames.has(option.value))?.value
    || available[0]?.value
    || "";
}

function comfyuiMultiCompareLoraMessage(name, insertedTerms = [], removedTerms = [], action = "已更新") {
  const cleanName = typeof normalizeComfyuiLoraName === "function"
    ? normalizeComfyuiLoraName(name)
    : String(name || "").trim();
  const hint = cleanName && typeof comfyuiLoraCompatibilityHint === "function"
    ? comfyuiLoraCompatibilityHint(cleanName)
    : "";
  const insertedText = insertedTerms.length ? `，並自動補上 trigger words：${insertedTerms.join(", ")}` : "";
  const removedText = removedTerms.length ? `；已移除不再使用的 trigger words：${removedTerms.join(", ")}` : "";
  const hintText = hint ? ` 提醒：${hint}；若模型不相容，ComfyUI 可能產圖失敗或效果異常。` : "";
  if (!cleanName) {
    setComfyuiMessage(`${action} Multi-Compare LoRA 欄位，請選擇要比較的 LoRA${removedText}。`, true);
    return;
  }
  setComfyuiMessage(`${action} Multi-Compare LoRA：${cleanName}${insertedText}${removedText}。${hintText}`, !hint);
}

function setComfyuiMultiCompareLoraName(detail, index, name, { notify = true } = {}) {
  const state = ensureComfyuiMultiCompareState(detail);
  if (!state || !Number.isInteger(index) || !state.loras[index]) return false;
  const nextName = typeof normalizeComfyuiLoraName === "function"
    ? normalizeComfyuiLoraName(name)
    : String(name || "").trim();
  const previousName = typeof normalizeComfyuiLoraName === "function"
    ? normalizeComfyuiLoraName(state.loras[index].name)
    : String(state.loras[index].name || "").trim();
  if (previousName === nextName) return true;
  state.loras[index].name = nextName;
  const removedTerms = previousName ? removeComfyuiMultiCompareLoraPromptTerms(previousName) : [];
  const insertedTerms = nextName ? applyComfyuiMultiCompareLoraPromptTerms(nextName) : [];
  writeComfyuiDraft();
  if (notify) comfyuiMultiCompareLoraMessage(nextName, insertedTerms, removedTerms, "已更新");
  return true;
}

function addComfyuiMultiCompareLora(detail) {
  const state = ensureComfyuiMultiCompareState(detail);
  if (!state || state.loras.length >= comfyuiMultiCompareMaxLoras()) return false;
  const name = comfyuiDefaultMultiCompareLoraName(state);
  state.loras.push({ name, strength_model: 1, strength_clip: 1 });
  const insertedTerms = name ? applyComfyuiMultiCompareLoraPromptTerms(name) : [];
  writeComfyuiDraft();
  comfyuiMultiCompareLoraMessage(name, insertedTerms, [], "已加入");
  renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
  return true;
}

function removeComfyuiMultiCompareLoraAt(detail, index) {
  const state = ensureComfyuiMultiCompareState(detail);
  if (!state || !Number.isInteger(index) || !state.loras[index]) return false;
  const removed = state.loras.splice(index, 1)[0] || {};
  const removedName = typeof normalizeComfyuiLoraName === "function"
    ? normalizeComfyuiLoraName(removed.name)
    : String(removed.name || "").trim();
  const removedTerms = removedName ? removeComfyuiMultiCompareLoraPromptTerms(removedName) : [];
  writeComfyuiDraft();
  setComfyuiMessage(
    removedTerms.length
      ? `已移除 Multi-Compare LoRA，並移除不再使用的 trigger words：${removedTerms.join(", ")}。`
      : "已移除 Multi-Compare LoRA。",
    true
  );
  renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
  return true;
}

function comfyuiTemplateCompareSharedParamKey(detail, field = {}) {
  if (!comfyuiTemplateIsCompareTwoCheckpoints(detail) && !comfyuiTemplateIsMultiCompareCheckpoints(detail)) return "";
  if (String(field?.class_type || "") !== "KSampler") return "";
  const inputName = String(field?.input_name || "").trim();
  return COMFYUI_COMPARE_SHARED_KSAMPLER_INPUTS.has(inputName) ? inputName : "";
}

function comfyuiTemplateAllFields(detail = comfyuiSelectedTemplateDetail) {
  const fields = [];
  (Array.isArray(detail?.ui_schema?.panels) ? detail.ui_schema.panels : []).forEach((panel) => {
    (panel?.fields || []).forEach((field) => {
      if (field && !field.synthetic && field.input_type !== "embedding_shortcuts") fields.push(field);
    });
  });
  return fields;
}

function comfyuiTemplateCompareSharedSourceField(detail, field = {}) {
  const key = comfyuiTemplateCompareSharedParamKey(detail, field);
  if (!key) return null;
  return comfyuiTemplateAllFields(detail).find((candidate) => (
    comfyuiTemplateCompareSharedParamKey(detail, candidate) === key
  )) || null;
}

function comfyuiTemplateIsHiddenCompareSharedField(detail, field = {}) {
  const source = comfyuiTemplateCompareSharedSourceField(detail, field);
  return !!source && String(source.id || "") !== String(field?.id || "");
}

function comfyuiTemplateSharedPromptSourceField(detail, field = {}) {
  if (comfyuiTemplatePromptSharingMode(detail) !== "shared" || !comfyuiTemplateIsPromptTextField(field)) return null;
  const role = comfyuiTemplatePromptRole(field);
  const fields = comfyuiTemplatePromptFieldsByRole(detail)[role] || [];
  return fields[0] || null;
}

function comfyuiTemplateIsHiddenSharedPromptField(detail, field = {}) {
  const source = comfyuiTemplateSharedPromptSourceField(detail, field);
  return !!source && String(source.id || "") !== String(field?.id || "");
}

function comfyuiTemplateIsMultiCompareCheckpointField(detail, field = {}) {
  return comfyuiTemplateIsMultiCompareCheckpoints(detail)
    && field?.class_type === "CheckpointLoaderSimple"
    && field?.input_name === "ckpt_name";
}

function comfyuiTemplateIsHiddenUpscaleBreakpointField(detail, field = {}) {
  if (!comfyuiTemplateIsMultiMethodUpscale(detail)) return false;
  const stage = comfyuiUpscaleBreakpointStage(detail);
  const nodeId = String(field?.node_id || "");
  if (comfyuiTemplateIsMultiMethodUpscaleModeTest(detail)) {
    if (stage === "latent_upscale" && nodeId === "77") return true;
    if (stage === "model_upscale" && ["61", "63"].includes(nodeId)) return true;
    return false;
  }
  return stage === "first_upscale" && nodeId === "77";
}

function comfyuiTemplateIsHiddenField(detail, field = {}) {
  return comfyuiTemplateIsHiddenSharedPromptField(detail, field)
    || comfyuiTemplateIsHiddenCompareSharedField(detail, field)
    || comfyuiTemplateIsMultiCompareCheckpointField(detail, field)
    || comfyuiTemplateIsHiddenUpscaleBreakpointField(detail, field);
}

function comfyuiTemplateCompareSharedRuntimeValue(detail, field = {}) {
  const source = comfyuiTemplateCompareSharedSourceField(detail, field) || field;
  const binding = comfyuiTemplateFieldBinding(source, detail, { textFieldIndex: 0, loadImageIndex: 0 });
  return comfyuiTemplateRuntimeValue(binding, source);
}

function comfyuiTemplateCompareSharedLabel(inputName = "") {
  return {
    seed: "共同種子",
    steps: "共同步數",
    cfg: "共同 CFG",
    sampler_name: "共同取樣器",
    scheduler: "共同排程器",
    denoise: "共同 Denoise",
  }[String(inputName || "")] || "";
}

function comfyuiTemplateOfficialMediaFilename(field = {}, detail = comfyuiSelectedTemplateDetail) {
  if (!detail?.is_official && !detail?.system_bundle_id) return "";
  if (!COMFYUI_TEMPLATE_MEDIA_BINDING_KINDS.has(comfyuiTemplateFieldBinding(field, detail, { textFieldIndex: 0, loadImageIndex: 0 }).kind)) return "";
  const raw = String(field?.current_value || "").trim();
  if (!raw || raw.includes("://")) return "";
  const filename = raw.split(/[\\/]/).pop().trim();
  return /\.(png|jpe?g|webp|mp4|mov|webm|mkv|avi)$/i.test(filename) ? filename : "";
}

function comfyuiTemplateOfficialMediaAssignment(field = {}, detail = comfyuiSelectedTemplateDetail) {
  const filename = comfyuiTemplateOfficialMediaFilename(field, detail);
  return filename ? `${COMFYUI_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX}${filename}` : "";
}

function comfyuiTemplateOfficialMediaPreviewUrl(filename = "") {
  const cleanName = String(filename || "").trim().split(/[\\/]/).pop();
  return cleanName ? `${API}/comfyui/workflows/official-media/${encodeURIComponent(cleanName)}` : "";
}

function comfyuiTemplateSummaryMarkup(detail) {
  if (!detail) {
    return "";
  }
  const mode = comfyuiReadableModeLabel(detail?.default_params?.generation_mode || "txt2img");
  const outputs = Array.isArray(detail?.output_kinds) && detail.output_kinds.length ? detail.output_kinds : ["image"];
  const inputKinds = comfyuiTemplateInputKinds(detail);
  const dependency = detail?.dependency_status || detail?.capability || null;
  const blockerItems = Array.isArray(dependency?.blockers) ? dependency.blockers : [];
  const requirementBits = []
    .concat((detail?.required_models || []).map((item) => `${item.kind || "model"}:${item.name || ""}`))
    .concat((detail?.required_controlnets || []).map((item) => `controlnet:${item.name || item}`))
    .concat((detail?.required_loras || []).map((item) => `lora:${item.name || item}`))
    .filter(Boolean);
  return `
    <details class="comfyui-template-detail-panel">
      <summary>
    <div class="comfyui-template-summary-head">
      <div class="comfyui-template-summary-title">
        <strong>${sanitize(detail?.title || `Workflow #${detail?.id || ""}`)}</strong>
        <div class="drive-card-sub">${sanitize(mode)} · ${sanitize(outputs.join(", "))}</div>
      </div>
      <div class="comfyui-template-summary-flags">
        <span class="comfyui-workflow-chip">${sanitize(mode)}</span>
        ${detail?.is_official ? '<span class="comfyui-workflow-chip">官方</span>' : ""}
        <span class="comfyui-workflow-chip">${sanitize(detail?.visibility || "private")}</span>
      </div>
    </div>
      </summary>
    <div class="drive-card-sub">${sanitize(detail?.description || "未填寫模板說明")}</div>
    ${comfyuiWorkflowDefaultModelNoticeHtml(detail, 8)}
    <div class="drive-card-sub">這個模板會根據 workflow manifest 只顯示需要的欄位；執行時只使用下方模板卡片的值。</div>
    ${requirementBits.length ? `<div class="drive-card-sub" style="margin-top:.35rem;">依賴：${sanitize(requirementBits.join("、"))}</div>` : ""}
    ${comfyuiWorkflowPaidApiWarningHtml(detail)}
    ${inputKinds.length ? `<div class="comfyui-template-output-list">${inputKinds.map((kind) => `<span class="comfyui-workflow-chip">輸入 ${sanitize(kind)}</span>`).join("")}</div>` : ""}
    <div class="comfyui-template-output-list">
      ${outputs.map((kind) => `<span class="comfyui-workflow-chip">輸出 ${sanitize(String(kind))}</span>`).join("")}
      ${dependency?.available === false ? '<span class="comfyui-workflow-chip bad">目前依賴不完整</span>' : ""}
    </div>
    ${blockerItems.length ? `<div class="drive-card-sub" style="margin-top:.45rem;color:#ffd2dc;">${sanitize(blockerItems.join("；"))}</div>` : ""}
    </details>
  `;
}

function comfyuiTemplateSelectOptions(targetId, field = {}) {
  const target = $(targetId);
  const options = [];
  const seen = new Set();
  const addOption = (value, label = value, disabled = false) => {
    const cleanValue = String(value || "");
    if (seen.has(cleanValue)) return;
    seen.add(cleanValue);
    options.push({
      value: cleanValue,
      label: String(label || cleanValue),
      disabled: !!disabled,
    });
  };
  const current = field?.current_value !== undefined && field?.current_value !== null
    ? String(field.current_value || "")
    : "";
  if (target && target.options && target.options.length) {
    Array.from(target.options).forEach((option) => {
      addOption(
        option.value || "",
        option.textContent || option.label || option.value || "",
        option.disabled
      );
    });
    if (current && !seen.has(current)) {
      addOption(current, `${current}（目前遠端未列出）`, true);
    }
  } else if (Array.isArray(field?.constraints?.options)) {
    field.constraints.options.forEach((value) => {
      addOption(value, value, false);
    });
    if (current && !seen.has(current)) addOption(current, current, false);
  } else if (current) {
    addOption(current, current, false);
  }
  return options;
}

function comfyuiTemplateSelectCurrentValue(targetId, field = {}, options = []) {
  const current = String(field?.current_value ?? "");
  if (current && options.some((option) => option.value === current && !option.disabled)) return current;
  const fallback = options.find((option) => option.value && !option.disabled)?.value || "";
  if (targetId === "comfyui-model-select" && field?.class_type === "CheckpointLoaderSimple" && fallback) return fallback;
  if (targetId === "comfyui-vae-select" && fallback) return fallback;
  return current;
}

function comfyuiTemplateSelectFallbackHint(targetId, field = {}, current = "") {
  const original = String(field?.current_value ?? "").trim();
  const replacement = String(current || "").trim();
  if (!original || !replacement || original === replacement) return "";
  if (targetId === "comfyui-model-select" && field?.class_type === "CheckpointLoaderSimple") {
    return `模板預設 ${original} 目前不存在；已改用遠端可用大模型 ${replacement}。若此欄原本是 refiner，也可用同一個 checkpoint 跳過 refiner 專用模型。`;
  }
  return "";
}

function comfyuiTemplateLoraSelectOptions(field = {}) {
  const seen = new Set();
  const options = [];
  const addOption = (value, label = value, disabled = false) => {
    const cleanValue = typeof normalizeComfyuiLoraName === "function"
      ? normalizeComfyuiLoraName(value)
      : String(value || "").trim();
    if (seen.has(cleanValue)) return;
    seen.add(cleanValue);
    const cleanLabel = String(label || cleanValue).trim();
    options.push({ value: cleanValue, label: cleanLabel || cleanValue, disabled: !!disabled });
  };
  addOption("", "不使用 LoRA（可略過）");
  const current = typeof normalizeComfyuiLoraName === "function"
    ? normalizeComfyuiLoraName(field?.current_value)
    : String(field?.current_value || "").trim();
  if (current) addOption(current, current, false);
  const source = $("comfyui-lora-select");
  if (source && source.options && source.options.length) {
    Array.from(source.options).forEach((option) => {
      addOption(option.value || "", option.textContent || option.label || option.value || "", false);
    });
  }
  (Array.isArray(comfyuiAvailableLoras) ? comfyuiAvailableLoras : []).forEach((name) => {
    const hint = typeof comfyuiLoraCompatibilityHint === "function" ? comfyuiLoraCompatibilityHint(name) : "";
    addOption(name, hint ? `${name}（提醒：${hint}）` : name, false);
  });
  return options;
}

function comfyuiSelectedLoraIndexForTemplateNode(nodeId) {
  const cleanNodeId = String(nodeId || "");
  if (!cleanNodeId) return -1;
  return comfyuiSelectedLoras.findIndex((item) => String(item?.template_node_id || "") === cleanNodeId);
}

function comfyuiSelectedLoraForTemplateNode(nodeId) {
  const index = comfyuiSelectedLoraIndexForTemplateNode(nodeId);
  return index >= 0 ? comfyuiSelectedLoras[index] : null;
}

function upsertComfyuiTemplateLora(nodeId, name, { notify = true } = {}) {
  const cleanNodeId = String(nodeId || "");
  const cleanName = typeof normalizeComfyuiLoraName === "function"
    ? normalizeComfyuiLoraName(name)
    : String(name || "").trim();
  if (cleanNodeId) comfyuiTemplateLoraOverrides[cleanNodeId] = cleanName;
  const existingIndex = comfyuiSelectedLoraIndexForTemplateNode(cleanNodeId);
  if (!cleanName) {
    if (existingIndex >= 0) removeComfyuiSelectedLoraByIndex(existingIndex);
    else renderComfyuiSelectedLoras();
    writeComfyuiDraft();
    return true;
  }
  if (comfyuiSelectedLoras.some((item, index) => {
    const itemName = typeof normalizeComfyuiLoraName === "function"
      ? normalizeComfyuiLoraName(item?.name)
      : String(item?.name || "").trim();
    return index !== existingIndex && itemName === cleanName;
  })) {
    setComfyuiMessage("這個 LoRA 已經加入。", false);
    return false;
  }
  const detail = comfyuiLoraDetails?.[cleanName] || {};
  const hint = typeof comfyuiLoraCompatibilityHint === "function" ? comfyuiLoraCompatibilityHint(cleanName) : "";
  if (existingIndex < 0 && comfyuiSelectedLoras.length >= COMFYUI_MAX_LORAS) {
    setComfyuiMessage(`已達 LoRA 數量上限 ${COMFYUI_MAX_LORAS} 個。`, false);
    return false;
  }
  if (existingIndex >= 0 && comfyuiSelectedLoras[existingIndex]?.name !== cleanName) {
    removeComfyuiSelectedLoraByIndex(existingIndex);
  }
  const nextIndex = comfyuiSelectedLoraIndexForTemplateNode(cleanNodeId);
  const item = {
    name: cleanName,
    strength_model: nextIndex >= 0 ? (comfyuiSelectedLoras[nextIndex].strength_model ?? 1) : 1,
    strength_clip: nextIndex >= 0 ? (comfyuiSelectedLoras[nextIndex].strength_clip ?? 1) : 1,
    template_node_id: cleanNodeId,
  };
  if (nextIndex >= 0) comfyuiSelectedLoras[nextIndex] = item;
  else comfyuiSelectedLoras.push(item);
  const insertedTerms = applyComfyuiPromptTerms(detail.trained_words || []);
  renderComfyuiSelectedLoras();
  writeComfyuiDraft();
  if (notify) {
    const triggerText = insertedTerms.length ? `，並自動補上 trigger words：${insertedTerms.join(", ")}` : "。";
    const warningText = hint ? `提醒：${hint}；若模型不相容，ComfyUI 可能產圖失敗或效果異常。` : "";
    setComfyuiMessage(`已加入 LoRA${triggerText}${warningText ? ` ${warningText}` : ""}`, !hint);
  }
  return true;
}

function updateComfyuiTemplateLoraStrength(nodeId, fieldName, rawValue) {
  const index = comfyuiSelectedLoraIndexForTemplateNode(nodeId);
  if (index < 0 || !comfyuiSelectedLoras[index]) {
    setComfyuiMessage("請先選擇 LoRA，再調整權重。", false);
    return null;
  }
  const field = fieldName === "strength_clip" ? "strength_clip" : "strength_model";
  const value = Number(rawValue);
  const normalized = Math.max(-2, Math.min(2, Number.isFinite(value) ? value : 1));
  comfyuiSelectedLoras[index][field] = Math.round(normalized * 100) / 100;
  renderComfyuiSelectedLoras();
  writeComfyuiDraft();
  return comfyuiSelectedLoras[index][field];
}

function comfyuiTemplateIsPromptTextField(field = {}) {
  const classType = String(field?.class_type || "");
  const inputName = String(field?.input_name || "").trim().toLowerCase();
  if (["CLIPTextEncode", "CLIPTextEncodeFlux"].includes(classType) && inputName === "text") return true;
  if (String(field?.category || "").toUpperCase() !== "TEXT") return false;
  const labelText = `${field?.label || ""} ${field?.node_title || ""}`.toLowerCase();
  if (["prompt", "tags", "caption"].includes(inputName)) return true;
  if (inputName === "string_b" && /prompt|tag|text|positive|negative|提示詞|正向|負面/i.test(labelText)) return true;
  return false;
}

function comfyuiTemplatePromptRole(field = {}, fallbackIndex = 0) {
  if (!comfyuiTemplateIsPromptTextField(field)) return "";
  const inputName = String(field?.input_name || "").trim().toLowerCase();
  const text = `${field?.label || ""} ${field?.node_title || ""} ${field?.current_value || ""}`.toLowerCase();
  if (text.includes("negative") || text.includes("負") || text.includes("low quality") || text.includes("worst quality") || text.includes("bad anatomy") || text.includes("blurry")) return "negative";
  if (text.includes("positive") || text.includes("正")) return "positive";
  if (["prompt", "tags", "caption", "string_b"].includes(inputName)) return "positive";
  return fallbackIndex === 0 ? "positive" : "negative";
}

function comfyuiTemplatePromptFieldsByRole(detail = comfyuiSelectedTemplateDetail) {
  const roles = { positive: [], negative: [] };
  const ctx = { textFieldIndex: 0 };
  (Array.isArray(detail?.ui_schema?.panels) ? detail.ui_schema.panels : []).forEach((panel) => {
    (panel?.fields || []).forEach((field) => {
      if (!field || field.synthetic || !comfyuiTemplateIsPromptTextField(field)) return;
      const role = comfyuiTemplatePromptRole(field, ctx.textFieldIndex);
      ctx.textFieldIndex += 1;
      if (role === "negative") roles.negative.push(field);
      else roles.positive.push(field);
    });
  });
  return roles;
}

function comfyuiTemplateNeedsPromptSharingChoice(detail = comfyuiSelectedTemplateDetail) {
  const roles = comfyuiTemplatePromptFieldsByRole(detail);
  return roles.positive.length > 1 || roles.negative.length > 1;
}

function comfyuiTemplatePromptSharingMode(detail = comfyuiSelectedTemplateDetail) {
  if (!comfyuiTemplateNeedsPromptSharingChoice(detail)) return "independent";
  return ["ask", "shared", "independent"].includes(comfyuiTemplatePromptShareMode)
    ? comfyuiTemplatePromptShareMode
    : "shared";
}

function comfyuiTemplatePromptFieldDirectValue(field = {}) {
  if (comfyuiTemplateHasFieldOverride(field)) return comfyuiTemplateFieldOverrideValue(field);
  const el = $(`tmpl-${field.id || ""}`);
  return el ? comfyuiTemplateElementValue(el) : field?.current_value;
}

function comfyuiTemplateElementValue(el) {
  if (!el) return "";
  if (el.type === "checkbox") return !!el.checked;
  return el.value;
}

function comfyuiTemplateSharedPromptValue(role, detail = comfyuiSelectedTemplateDetail) {
  const fields = comfyuiTemplatePromptFieldsByRole(detail)[role] || [];
  const field = fields[0] || null;
  return field ? comfyuiTemplatePromptFieldDirectValue(field) : "";
}

function syncComfyuiTemplateSharedPromptFields(role, value) {
  if (!role) return;
  const fields = comfyuiTemplatePromptFieldsByRole(comfyuiSelectedTemplateDetail)[role] || [];
  fields.forEach((field) => {
    if (!field?.id) return;
    comfyuiTemplateFieldOverrides[String(field.id)] = value;
    const input = $(`tmpl-${field.id}`);
    if (input && input.value !== String(value ?? "")) input.value = String(value ?? "");
  });
}

function comfyuiTemplateCanEditLockedModelField(field = {}) {
  const category = String(field?.category || "").toUpperCase();
  if (category !== "MODEL" || !field?.node_id || !field?.input_name) return false;
  if ((field?.class_type === "LoraLoader" || field?.class_type === "LoraLoaderModelOnly") && field?.input_name === "lora_name") return false;
  if ((field?.class_type === "LoraLoader" || field?.class_type === "LoraLoaderModelOnly") && String(field?.input_name || "").startsWith("strength_")) return false;
  if (field?.class_type === "LoraLoaderModelOnly" && field?.input_name === "model") return false;
  if (field?.class_type === "LoraLoader" && ["model", "clip"].includes(field?.input_name)) return false;
  if (field?.class_type === "ControlNetApplyAdvanced" && field?.input_name === "control_net") return false;
  return !!(field?.locked || field?.read_only || field?.lock_reason === "template_default_model");
}

function comfyuiTemplateLockedModelFieldIsEditing(field = {}) {
  const key = comfyuiTemplateOverrideKey(field);
  return !!key && comfyuiTemplateEditableModelFields[key] === true;
}

function comfyuiTemplateFieldBinding(field, detail, ctx) {
  const mode = String(detail?.default_params?.generation_mode || "txt2img").trim().toLowerCase();
  const sharedCompareParam = comfyuiTemplateCompareSharedParamKey(detail, field);
  const withCompareSharing = (binding) => (
    sharedCompareParam ? { ...binding, sharedCompareParam } : binding
  );
  if (comfyuiTemplateIsPromptTextField(field)) {
    const role = comfyuiTemplatePromptRole(field, ctx.textFieldIndex);
    const binding = { kind: "field", targetId: role === "negative" ? "comfyui-negative-prompt" : "comfyui-prompt", promptRole: role };
    ctx.textFieldIndex += 1;
    return binding;
  }
  if (comfyuiTemplateCanEditLockedModelField(field) && comfyuiTemplateLockedModelFieldIsEditing(field)) {
    return { kind: "direct", fieldId: field.id, editableLockedModel: true };
  }
  if (comfyuiTemplateCanEditLockedModelField(field)) {
    return { kind: "readonly", editableLockedModel: true };
  }
  if (
    (field?.class_type === "VAELoader" && field?.input_name === "vae_name") ||
    (field?.class_type === "CLIPLoader" && field?.input_name === "clip_name") ||
    (field?.class_type === "DualCLIPLoader" && ["clip_name1", "clip_name2"].includes(field?.input_name)) ||
    (field?.class_type === "TripleCLIPLoader" && ["clip_name1", "clip_name2", "clip_name3"].includes(field?.input_name)) ||
    (field?.class_type === "CLIPVisionLoader" && field?.input_name === "clip_name") ||
    (field?.class_type === "UNETLoader" && field?.input_name === "unet_name")
  ) return { kind: "readonly", editableLockedModel: comfyuiTemplateCanEditLockedModelField(field) };
  if (field?.class_type === "CheckpointLoaderSimple" && field?.input_name === "ckpt_name") return { kind: "field", targetId: "comfyui-model-select" };
  if ((field?.class_type === "LoraLoader" || field?.class_type === "LoraLoaderModelOnly") && field?.input_name === "lora_name") return { kind: "lora", nodeId: field.node_id };
  if ((field?.class_type === "LoraLoader" || field?.class_type === "LoraLoaderModelOnly") && (field?.input_name === "strength_model" || field?.input_name === "strength_clip")) {
    return { kind: "lora_strength", nodeId: field.node_id, strengthField: field.input_name };
  }
  if (field?.class_type === "UpscaleModelLoader" && field?.input_name === "model_name") return { kind: "field", targetId: "comfyui-upscale-model" };
  if (field?.class_type === "ControlNetLoader" && field?.input_name === "control_net_name") return { kind: "field", targetId: "comfyui-controlnet-model", enableControlnet: true };
  if (field?.class_type === "KSampler" && field?.input_name === "seed") return withCompareSharing({ kind: "field", targetId: "comfyui-seed" });
  if (field?.class_type === "KSampler" && field?.input_name === "steps") return withCompareSharing({ kind: "field", targetId: "comfyui-steps" });
  if (field?.class_type === "KSampler" && field?.input_name === "cfg") return withCompareSharing({ kind: "field", targetId: "comfyui-cfg" });
  if (field?.class_type === "KSampler" && field?.input_name === "sampler_name") return withCompareSharing({ kind: "field", targetId: "comfyui-sampler" });
  if (field?.class_type === "KSampler" && field?.input_name === "scheduler") return withCompareSharing({ kind: "field", targetId: "comfyui-scheduler" });
  if (field?.class_type === "KSampler" && field?.input_name === "denoise") return withCompareSharing({ kind: "field", targetId: "comfyui-denoise-strength" });
  if (field?.class_type === "EmptyLatentImage" && field?.input_name === "width") return { kind: "field", targetId: "comfyui-width" };
  if (field?.class_type === "EmptyLatentImage" && field?.input_name === "height") return { kind: "field", targetId: "comfyui-height" };
  if (field?.class_type === "EmptyLatentImage" && field?.input_name === "batch_size") return { kind: "field", targetId: "comfyui-batch-size" };
  if (field?.class_type === "ControlNetApplyAdvanced" && field?.input_name === "strength") return { kind: "field", targetId: "comfyui-control-strength", enableControlnet: true };
  if (field?.class_type === "ControlNetApplyAdvanced" && field?.input_name === "start_percent") return { kind: "field", targetId: "comfyui-control-start", enableControlnet: true };
  if (field?.class_type === "ControlNetApplyAdvanced" && field?.input_name === "end_percent") return { kind: "field", targetId: "comfyui-control-end", enableControlnet: true };
  if (field?.class_type === "ImagePadForOutpaint" && field?.input_name === "left") return { kind: "field", targetId: "comfyui-outpaint-left" };
  if (field?.class_type === "ImagePadForOutpaint" && field?.input_name === "top") return { kind: "field", targetId: "comfyui-outpaint-top" };
  if (field?.class_type === "ImagePadForOutpaint" && field?.input_name === "right") return { kind: "field", targetId: "comfyui-outpaint-right" };
  if (field?.class_type === "ImagePadForOutpaint" && field?.input_name === "bottom") return { kind: "field", targetId: "comfyui-outpaint-bottom" };
  if (field?.class_type === "ImagePadForOutpaint" && field?.input_name === "feathering") return { kind: "field", targetId: "comfyui-outpaint-feathering" };
  if (field?.class_type === "LoadImageMask" && field?.input_name === "image") return { kind: "image", assetKey: "mask", nodeId: field.node_id };
  if (field?.class_type === "LoadImageMask" && field?.input_name === "channel") return { kind: "readonly" };
  if (field?.class_type === "LoadVideo" && field?.input_name === "file") return { kind: "video", assetKey: "video", nodeId: field.node_id };
  if (field?.class_type === "LoadImage" && field?.input_name === "image") {
    const hasControlnet = !!detail?.default_params?.controlnet?.type;
    const usesSource = ["img2img", "inpaint", "outpaint", "upscale"].includes(mode);
    let assetKey = "source";
    if (usesSource && ctx.loadImageIndex === 0) assetKey = "source";
    else if (hasControlnet) assetKey = "control";
    else if (!usesSource) assetKey = "control";
    ctx.loadImageIndex += 1;
    return { kind: "image", assetKey, nodeId: field.node_id };
  }
  if (field?.category && field.category !== "UNKNOWN") return { kind: "direct", fieldId: field.id };
  return { kind: "readonly" };
}

function comfyuiTemplateLabelNoiseSuffix(field = {}) {
  const text = `${field?.label || ""} ${field?.node_title || ""} ${field?.current_value || ""}`.toLowerCase();
  if (text.includes("high_noise") || text.includes("high noise") || text.includes("高噪")) return "High Noise";
  if (text.includes("low_noise") || text.includes("low noise") || text.includes("低噪")) return "Low Noise";
  return "";
}

function comfyuiTemplateLabelWithNoise(field, baseLabel) {
  const suffix = comfyuiTemplateLabelNoiseSuffix(field);
  return suffix ? `${baseLabel}（${suffix}）` : baseLabel;
}

function comfyuiTemplateFieldLabel(field = {}, binding = null) {
  const rawLabel = String(field?.label || "").trim();
  const classType = String(field?.class_type || "");
  const inputName = String(field?.input_name || "");
  if (binding?.sharedCompareParam) {
    return comfyuiTemplateCompareSharedLabel(binding.sharedCompareParam) || rawLabel || inputName || "共同參數";
  }
  if ((classType === "CLIPTextEncode" || classType === "CLIPTextEncodeFlux") && inputName === "text") {
    if (rawLabel.includes("正向提示詞") || rawLabel.includes("負面提示詞")) return rawLabel;
    const role = binding?.promptRole || comfyuiTemplatePromptRole(field, binding?.targetId === "comfyui-negative-prompt" ? 1 : 0);
    if (role === "negative") return "負面提示詞";
    if (role === "positive") return "正向提示詞";
    return rawLabel && !["文字輸入", "提示詞"].includes(rawLabel) ? rawLabel : "提示詞";
  }
  if (classType === "CheckpointLoaderSimple" && inputName === "ckpt_name") return "Checkpoint / 大模型";
  if (classType === "UNETLoader" && inputName === "unet_name") return comfyuiTemplateLabelWithNoise(field, "Diffusion / UNet 大模型");
  if (classType === "VAELoader" && inputName === "vae_name") return "VAE";
  if (classType === "CLIPLoader" && inputName === "clip_name") return "CLIP / 文字編碼器";
  if (classType === "DualCLIPLoader" && inputName === "clip_name1") return "CLIP-L 文字編碼器";
  if (classType === "DualCLIPLoader" && inputName === "clip_name2") return "T5 / 第二文字編碼器";
  if (classType === "TripleCLIPLoader" && inputName === "clip_name1") return "CLIP-L 文字編碼器";
  if (classType === "TripleCLIPLoader" && inputName === "clip_name2") return "CLIP-G 文字編碼器";
  if (classType === "TripleCLIPLoader" && inputName === "clip_name3") return "T5 / 第三文字編碼器";
  if (classType === "CLIPVisionLoader" && inputName === "clip_name") return "CLIP Vision 模型";
  if (classType === "LoraLoader" && inputName === "lora_name") return comfyuiTemplateLabelWithNoise(field, "LoRA 模型");
  if (classType === "LoraLoaderModelOnly" && inputName === "lora_name") return comfyuiTemplateLabelWithNoise(field, "LoRA 模型（Model-only）");
  if (classType === "UpscaleModelLoader" && inputName === "model_name") return "放大 / Upscale 模型";
  if (classType === "ControlNetLoader" && inputName === "control_net_name") return "ControlNet 模型";
  if (classType === "LoadVideo" && inputName === "file") return "載入影片";
  return rawLabel || inputName || "欄位";
}

function comfyuiTemplateDisplayValue(field = {}, value = "") {
  const classType = String(field?.class_type || "");
  const inputName = String(field?.input_name || "");
  const text = String(value ?? "").trim();
  if (classType === "VAELoader" && inputName === "vae_name" && (!text || text === COMFYUI_VAE_BUILTIN)) {
    return COMFYUI_BUILTIN_VAE_LABEL;
  }
  return String(value ?? "");
}

function comfyuiTemplateOverrideKey(field = {}) {
  return String(field?.id || "");
}

function comfyuiTemplateHasFieldOverride(field = {}) {
  const key = comfyuiTemplateOverrideKey(field);
  return !!key && Object.prototype.hasOwnProperty.call(comfyuiTemplateFieldOverrides, key);
}

function comfyuiTemplateFieldOverrideValue(field = {}) {
  return comfyuiTemplateFieldOverrides[comfyuiTemplateOverrideKey(field)];
}

function setComfyuiTemplateFieldOverride(field = {}, value) {
  const key = comfyuiTemplateOverrideKey(field);
  if (!key) return;
  comfyuiTemplateFieldOverrides[key] = value;
}

function comfyuiTemplateFieldValue(binding, field = {}) {
  if (binding?.promptRole && comfyuiTemplatePromptSharingMode() === "shared") {
    return comfyuiTemplateSharedPromptValue(binding.promptRole);
  }
  if (comfyuiTemplateHasFieldOverride(field)) return comfyuiTemplateFieldOverrideValue(field);
  if (binding.kind === "field") {
    const el = $(`tmpl-${field.id || ""}`);
    const value = el ? comfyuiTemplateElementValue(el) : field?.current_value;
    return field?.input_type === "checkbox" ? !!value : String(value ?? "");
  }
  if (COMFYUI_TEMPLATE_MEDIA_BINDING_KINDS.has(binding.kind)) {
    return comfyuiAssetState(binding.assetKey);
  }
  if (binding.kind === "direct") {
    const el = $(`tmpl-${field.id || ""}`);
    const value = el ? comfyuiTemplateElementValue(el) : field?.current_value;
    return field?.input_type === "checkbox" ? !!value : String(value ?? "");
  }
  return field?.current_value;
}

function comfyuiTemplateRuntimeValue(binding, field = {}) {
  if (binding?.promptRole && comfyuiTemplatePromptSharingMode() === "shared") {
    return comfyuiTemplateSharedPromptValue(binding.promptRole);
  }
  if (comfyuiTemplateHasFieldOverride(field)) return comfyuiTemplateFieldOverrideValue(field);
  if (binding.kind === "field") {
    const el = $(`tmpl-${field.id || ""}`);
    return el ? comfyuiTemplateElementValue(el) : field?.current_value;
  }
  if (binding.kind === "lora") {
    const selected = comfyuiSelectedLoraForTemplateNode(binding.nodeId);
    if (Object.prototype.hasOwnProperty.call(comfyuiTemplateLoraOverrides, String(binding.nodeId || ""))) {
      return comfyuiTemplateLoraOverrides[String(binding.nodeId || "")];
    }
    return selected?.name || field?.current_value || "";
  }
  if (binding.kind === "lora_strength") {
    const selected = comfyuiSelectedLoraForTemplateNode(binding.nodeId);
    return selected?.[binding.strengthField] ?? field?.current_value ?? 1;
  }
  if (binding.kind === "direct") {
    const el = $(`tmpl-${field.id || ""}`);
    return el ? comfyuiTemplateElementValue(el) : field?.current_value;
  }
  return field?.current_value;
}

function comfyuiTemplateDirectHint(field = {}) {
  const category = String(field?.category || "").toUpperCase();
  const classType = String(field?.class_type || "");
  const inputName = String(field?.input_name || "");
  if (category === "MODEL") {
    if (classType === "VAELoader" && inputName === "vae_name") return "留白代表使用各自大模型內建 VAE；若要覆蓋，請填 ComfyUI models/vae 內實際檔名。";
    if (classType === "CLIPVisionLoader") return "填 ComfyUI models/clip_vision 內實際檔名；例如 Capybara 使用的 sigclip_vision_patch14_384.safetensors。";
    if (classType === "LatentUpscaleModelLoader") return "填 ComfyUI models/latent_upscale_models 內實際檔名；若檔案在子資料夾，系統會嘗試用檔名自動對應。";
    if (/clip/i.test(inputName)) return "填 ComfyUI models/clip 或 text_encoders 內實際檔名；缺檔時相容性檢查會提示。";
    if (/unet/i.test(inputName)) return "填 ComfyUI models/diffusion_models 或 unet 內實際檔名；請對應 Flux、SD3.5、Wan 等模型。";
    return "填已安裝模型檔名；若本地或遠端 ComfyUI 找不到，送出前會提示缺少依賴。";
  }
  if (category === "SAMPLER") return "使用 ComfyUI 節點支援的取樣器或排程器名稱。";
  if (classType === "WanImageToVideo" && ["width", "height", "length"].includes(inputName)) return "Wan 影片尺寸與幀數會直接影響 VRAM、速度與輸出長度。";
  if (category === "NUMERIC") return "這是該模型節點的進階數值；不了解時可先保留預設。";
  return "";
}

function normalizeComfyuiTemplateRuntimeValue(field, value) {
  if (field?.category === "BOOLEAN" || field?.input_type === "checkbox") {
    return value === true || value === "true" || value === "1" || value === 1;
  }
  if (field?.category === "NUMERIC" || field?.input_type === "number") {
    const number = Number(value);
    return Number.isFinite(number) ? number : Number(field?.current_value || 0);
  }
  return value === undefined || value === null ? "" : String(value);
}

function collectComfyuiTemplateUserInputs(detail) {
  const userInputs = {};
  const ctx = { textFieldIndex: 0, loadImageIndex: 0 };
  const panels = Array.isArray(detail?.ui_schema?.panels) ? detail.ui_schema.panels : [];
  panels.forEach((panel) => {
    (panel?.fields || []).forEach((field) => {
      if (!field || field.synthetic || field.input_type === "embedding_shortcuts" || !field.node_id || !field.input_name) return;
      if (comfyuiTemplateIsMultiCompareCheckpointField(detail, field)) return;
      if (comfyuiTemplateIsHiddenUpscaleBreakpointField(detail, field)) return;
      const binding = comfyuiTemplateFieldBinding(field, detail, ctx);
      if (COMFYUI_TEMPLATE_MEDIA_BINDING_KINDS.has(binding.kind) || binding.kind === "readonly") return;
      const rawValue = comfyuiTemplateIsHiddenSharedPromptField(detail, field)
        ? comfyuiTemplateSharedPromptValue(comfyuiTemplatePromptRole(field), detail)
        : (comfyuiTemplateIsHiddenCompareSharedField(detail, field)
          ? comfyuiTemplateCompareSharedRuntimeValue(detail, field)
          : comfyuiTemplateRuntimeValue(binding, field));
      if (!userInputs[field.node_id]) userInputs[field.node_id] = {};
      userInputs[field.node_id][field.input_name] = normalizeComfyuiTemplateRuntimeValue(field, rawValue);
    });
  });
  if (comfyuiTemplateIsMultiCompareCheckpoints(detail)) {
    const spec = comfyuiMultiCompareRunSpec(detail);
    comfyuiTemplateCheckpointFields(detail).slice(0, 2).forEach((field, index) => {
      const checkpoint = spec.checkpoints[index] || "";
      if (!checkpoint || !field?.node_id || !field?.input_name) return;
      if (!userInputs[field.node_id]) userInputs[field.node_id] = {};
      userInputs[field.node_id][field.input_name] = checkpoint;
    });
  }
  return userInputs;
}

function comfyuiMultiCompareRunSpec(detail = comfyuiSelectedTemplateDetail) {
  const state = ensureComfyuiMultiCompareState(detail);
  if (!state) return null;
  const checkpoints = state.checkpoints
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .slice(0, COMFYUI_MULTI_COMPARE_MAX_CHECKPOINTS);
  const loras = state.loras
    .map((item) => ({
      name: typeof normalizeComfyuiLoraName === "function" ? normalizeComfyuiLoraName(item?.name) : String(item?.name || "").trim(),
      strength_model: Number.isFinite(Number(item?.strength_model)) ? Number(item.strength_model) : 1,
      strength_clip: Number.isFinite(Number(item?.strength_clip)) ? Number(item.strength_clip) : 1,
    }))
    .filter((item) => item.name)
    .slice(0, comfyuiMultiCompareMaxLoras());
  return {
    enabled: true,
    bundle_id: comfyuiTemplateBundleId(detail),
    checkpoints,
    loras,
  };
}

function comfyuiUpscaleBreakpointRunSpec(detail = comfyuiSelectedTemplateDetail) {
  if (!comfyuiTemplateIsMultiMethodUpscale(detail)) return null;
  const stage = comfyuiUpscaleBreakpointStage(detail);
  if (comfyuiTemplateIsMultiMethodUpscaleModeTest(detail)) {
    return {
      enabled: true,
      mode: normalizeComfyuiUpscaleBreakpointValue(detail, stage),
    };
  }
  return {
    enabled: true,
    stage: stage === "second_upscale" ? "second_upscale" : "first_upscale",
  };
}

function ensureComfyuiTemplatePromptSharingChoice(detail) {
  if (!comfyuiTemplateNeedsPromptSharingChoice(detail)) return true;
  if (comfyuiTemplatePromptSharingMode(detail) !== "ask") return true;
  setComfyuiMessage("這個 workflow 有多個正向或負面提示詞欄位，請先選擇是否全域共用提示詞。", false);
  const select = document.querySelector("[data-comfyui-template-prompt-sharing]");
  if (select) select.focus();
  return false;
}

function collectComfyuiTemplateImageAssignments(detail) {
  const assignments = {};
  const missing = [];
  const ctx = { textFieldIndex: 0, loadImageIndex: 0 };
  const panels = Array.isArray(detail?.ui_schema?.panels) ? detail.ui_schema.panels : [];
  panels.forEach((panel) => {
    (panel?.fields || []).forEach((field) => {
      if (!field || field.synthetic || field.input_type === "embedding_shortcuts" || !field.node_id || !field.input_name) return;
      const binding = comfyuiTemplateFieldBinding(field, detail, ctx);
      if (!COMFYUI_TEMPLATE_MEDIA_BINDING_KINDS.has(binding.kind)) return;
      const asset = comfyuiAssetState(binding.assetKey);
      if (asset?.cloudFileId) {
        assignments[String(field.node_id)] = String(asset.cloudFileId);
      } else {
        const officialAssignment = comfyuiTemplateOfficialMediaAssignment(field, detail);
        if (officialAssignment) {
          assignments[String(field.node_id)] = officialAssignment;
          return;
        }
        missing.push({
          nodeId: String(field.node_id),
          label: comfyuiTemplateFieldLabel(field, binding) || field.input_name || `Node ${field.node_id}`,
          assetKey: binding.assetKey,
          mediaKind: binding.kind,
          hasLocalFile: !!asset?.file,
          hasImageRef: !!asset?.imageRef,
        });
      }
    });
  });
  return { assignments, missing };
}

async function ensureComfyuiTemplateImageAssignments(detail) {
  let imageAssignmentState = collectComfyuiTemplateImageAssignments(detail);
  const localAssetKeys = Array.from(new Set(
    imageAssignmentState.missing
      .filter((item) => item.hasLocalFile && item.assetKey)
      .map((item) => item.assetKey)
  ));
  if (!localAssetKeys.length) return imageAssignmentState;
  const importer = typeof importComfyuiUploadedMedia === "function"
    ? importComfyuiUploadedMedia
    : (typeof importComfyuiUploadedImage === "function" ? importComfyuiUploadedImage : null);
  if (!importer) return imageAssignmentState;
  const assetMeta = typeof COMFYUI_INPUT_ASSET_META === "object" ? COMFYUI_INPUT_ASSET_META : {};
  const titleByKey = (key) => assetMeta[key]?.title || key;
  setComfyuiMessage(`正在將本機媒體匯入雲端硬碟供 workflow 安全重映射：${localAssetKeys.map(titleByKey).join("、")}`, true);
  for (const assetKey of localAssetKeys) {
    await importer(assetKey);
  }
  renderSelectedComfyuiTemplate();
  return collectComfyuiTemplateImageAssignments(detail);
}

function comfyuiTemplateUpdateField(binding, field, rawValue) {
  if (binding.enableControlnet && $("comfyui-controlnet-enabled")) {
    $("comfyui-controlnet-enabled").checked = true;
  }
  if (binding.kind === "field") {
    setComfyuiTemplateFieldOverride(field, rawValue);
    writeComfyuiDraft();
  }
}

function comfyuiTemplateEmbeddingTargetIds(field = {}) {
  const values = Array.isArray(field?.constraints?.target_field_ids) ? field.constraints.target_field_ids : [];
  return values.map((value) => String(value || "").trim()).filter(Boolean);
}

function comfyuiTemplateLocalTextTargets(targetFieldIds = []) {
  return targetFieldIds
    .map((fieldId) => $(`tmpl-${fieldId}`))
    .filter((el) => el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT"));
}

function comfyuiTemplateLooksNegativeTextTarget(el) {
  const text = `${el?.dataset?.comfyuiTemplateLabel || ""} ${el?.value || ""}`.toLowerCase();
  return text.includes("負") || text.includes("negative") || text.includes("low quality") || text.includes("worst quality");
}

function insertComfyuiTemplateEmbeddingToken(name, targetFieldIds = []) {
  const cleanName = String(name || "").trim();
  if (!cleanName) return;
  const localTargets = comfyuiTemplateLocalTextTargets(targetFieldIds);
  if (!localTargets.length) {
    if (typeof insertComfyuiEmbeddingToken === "function") insertComfyuiEmbeddingToken(cleanName);
    return;
  }
  const wantsNegative = typeof isNegativeComfyuiEmbedding === "function" && isNegativeComfyuiEmbedding(cleanName);
  const target = wantsNegative
    ? (localTargets.find((el) => comfyuiTemplateLooksNegativeTextTarget(el)) || localTargets[1] || localTargets[0])
    : (localTargets.find((el) => !comfyuiTemplateLooksNegativeTextTarget(el)) || localTargets[0]);
  const embeddingTag = `<embeddings:${cleanName}>`;
  const existingTarget = localTargets.find((el) => (
    typeof removeComfyuiEmbeddingTokenFromInput === "function"
      ? removeComfyuiEmbeddingTokenFromInput(el, cleanName).length
      : false
  ));
  if (existingTarget) {
    existingTarget.focus();
    existingTarget.dispatchEvent(new Event("input", { bubbles: true }));
    existingTarget.dispatchEvent(new Event("change", { bubbles: true }));
    writeComfyuiDraft();
    if (typeof setComfyuiMessage === "function") setComfyuiMessage(`已從提示詞移除 ${cleanName}。`, true);
    return;
  }
  const raw = target.value || "";
  const start = Number.isInteger(target.selectionStart) ? target.selectionStart : raw.length;
  const end = Number.isInteger(target.selectionEnd) ? target.selectionEnd : raw.length;
  const prefix = start > 0 && !/[\s,\n]$/.test(raw.slice(0, start)) ? ", " : "";
  const suffix = end < raw.length && !/^[\s,]/.test(raw.slice(end)) ? " " : "";
  target.value = `${raw.slice(0, start)}${prefix}${embeddingTag}${suffix}${raw.slice(end)}`;
  const cursor = start + prefix.length + embeddingTag.length + suffix.length;
  target.focus();
  if (typeof target.setSelectionRange === "function") target.setSelectionRange(cursor, cursor);
  target.dispatchEvent(new Event("input", { bubbles: true }));
  target.dispatchEvent(new Event("change", { bubbles: true }));
  writeComfyuiDraft();
  if (typeof setComfyuiMessage === "function") setComfyuiMessage(`已把 ${cleanName} 插入提示詞。`, true);
}

function updateSelectedComfyuiTemplateSeedFields(seedValue) {
  if (!comfyuiSelectedTemplateDetail?.ui_schema?.panels) return false;
  const seed = normalizeComfyuiSeedForUi(seedValue);
  if (seed === null) return false;
  let updated = false;
  (comfyuiSelectedTemplateDetail.ui_schema.panels || []).forEach((panel) => {
    (panel?.fields || []).forEach((field) => {
      if (!field || field.synthetic || !["seed", "noise_seed"].includes(String(field.input_name || ""))) return;
      if (String(field.category || "").toUpperCase() !== "NUMERIC") return;
      field.current_value = seed;
      comfyuiTemplateFieldOverrides[String(field.id || "")] = seed;
      const input = $(`tmpl-${field.id || ""}`);
      if (input) input.value = String(seed);
      updated = true;
    });
  });
  return updated;
}

function comfyuiTemplateFieldIsSeed(field = {}) {
  const inputName = String(field?.input_name || "").trim();
  if (!["seed", "noise_seed"].includes(inputName)) return false;
  return String(field?.category || "").toUpperCase() === "NUMERIC" || field?.input_type === "number";
}

function currentSelectedComfyuiTemplateSeedValue() {
  if (!comfyuiSelectedTemplateDetail?.ui_schema?.panels) return null;
  const panels = comfyuiSelectedTemplateDetail.ui_schema.panels || [];
  for (const panel of panels) {
    for (const field of (panel?.fields || [])) {
      if (!field || field.synthetic || !comfyuiTemplateFieldIsSeed(field)) continue;
      const input = $(`tmpl-${field.id || ""}`);
      const value = input ? input.value : (comfyuiTemplateHasFieldOverride(field) ? comfyuiTemplateFieldOverrideValue(field) : field.current_value);
      const seed = normalizeComfyuiSeedForUi(value);
      if (seed !== null) return seed;
    }
  }
  return null;
}

function renderComfyuiTemplateSeedAfterGenerateControl() {
  const mode = typeof comfyuiSeedAfterGenerateMode === "function"
    ? comfyuiSeedAfterGenerateMode()
    : ($("comfyui-seed-after-generate")?.value || "fixed");
  const options = [
    ["random", "隨機"],
    ["fixed", "固定"],
    ["increment", "+1"],
    ["decrement", "-1"],
  ];
  return `
    <div class="comfyui-template-seed-after-control">
      <label>任務後 Seed</label>
      <select data-comfyui-template-seed-after-generate="1">
        ${options.map(([value, label]) => `<option value="${sanitize(value)}"${value === mode ? " selected" : ""}>${sanitize(label)}</option>`).join("")}
      </select>
    </div>
  `;
}

function renderComfyuiTemplatePromptSharingControl(detail = comfyuiSelectedTemplateDetail) {
  if (!comfyuiTemplateNeedsPromptSharingChoice(detail)) return "";
  const roles = comfyuiTemplatePromptFieldsByRole(detail);
  const mode = comfyuiTemplatePromptSharingMode(detail);
  const parts = [];
  if (roles.positive.length > 1) parts.push(`正向 ${roles.positive.length} 個`);
  if (roles.negative.length > 1) parts.push(`負面 ${roles.negative.length} 個`);
  return `
    <div class="comfyui-template-prompt-sharing">
      <div class="comfyui-template-prompt-sharing-text">
        <div class="drive-card-title">提示詞共用</div>
        <div class="drive-card-sub">偵測到多個提示詞欄位：${sanitize(parts.join("、"))}。</div>
      </div>
      <select data-comfyui-template-prompt-sharing="1">
        <option value="ask"${mode === "ask" ? " selected" : ""}>請選擇是否全域共用</option>
        <option value="shared"${mode === "shared" ? " selected" : ""}>全域共用正負向提示詞</option>
        <option value="independent"${mode === "independent" ? " selected" : ""}>各欄位獨立設定</option>
      </select>
    </div>
  `;
}

function renderComfyuiMultiCompareControl(detail = comfyuiSelectedTemplateDetail) {
  const state = ensureComfyuiMultiCompareState(detail);
  if (!state) return "";
  const checkpointField = comfyuiTemplateCheckpointFields(detail)[0] || {};
  const modelOptions = comfyuiTemplateSelectOptions("comfyui-model-select", checkpointField);
  const loraOptions = comfyuiTemplateLoraSelectOptions({});
  const maxLoras = comfyuiMultiCompareMaxLoras();
  const checkpointRows = state.checkpoints.map((checkpoint, index) => `
    <div class="comfyui-multi-compare-row">
      <label>大模型 #${index + 1}</label>
      <select data-comfyui-multi-compare-checkpoint="${index}">
        ${modelOptions.map((option) => `<option value="${sanitize(option.value)}"${option.value === checkpoint ? " selected" : ""}${option.disabled ? ' disabled="disabled"' : ""}>${sanitize(option.label)}</option>`).join("")}
      </select>
      <button class="btn btn-sm" type="button" data-comfyui-multi-compare-remove-checkpoint="${index}"${state.checkpoints.length <= 2 ? ' disabled="disabled"' : ""}>移除</button>
    </div>
  `).join("");
  const loraRows = state.loras.length
    ? state.loras.map((lora, index) => `
      <div class="comfyui-multi-compare-row is-lora">
        <label>LoRA #${index + 1}</label>
        <select data-comfyui-multi-compare-lora="${index}">
          ${loraOptions.map((option) => `<option value="${sanitize(option.value)}"${option.value === lora.name ? " selected" : ""}${option.disabled ? ' disabled="disabled"' : ""}>${sanitize(option.label)}</option>`).join("")}
        </select>
        <input type="number" min="-2" max="2" step="0.05" value="${sanitize(String(lora.strength_model ?? 1))}" data-comfyui-multi-compare-lora-strength-model="${index}" aria-label="LoRA #${index + 1} Model 權重" />
        <input type="number" min="-2" max="2" step="0.05" value="${sanitize(String(lora.strength_clip ?? 1))}" data-comfyui-multi-compare-lora-strength-clip="${index}" aria-label="LoRA #${index + 1} CLIP 權重" />
        <button class="btn btn-sm" type="button" data-comfyui-multi-compare-remove-lora="${index}">移除</button>
      </div>
    `).join("")
    : '<div class="drive-card-sub">尚未加入 LoRA；不加入時就是純 checkpoint 比較。</div>';
  return `
    <section class="comfyui-multi-compare-card">
      <div class="comfyui-multi-compare-head">
        <div>
          <div class="drive-card-title">Multi-Compare 測試</div>
          <div class="drive-card-sub">最低比較 2 個大模型；新增後會在執行前自動衍生 KSampler、VAEDecode、PreviewImage 節點與連線。</div>
        </div>
        <div class="drive-file-actions">
          <button class="btn btn-sm" type="button" data-comfyui-multi-compare-add-checkpoint="1"${state.checkpoints.length >= COMFYUI_MULTI_COMPARE_MAX_CHECKPOINTS ? ' disabled="disabled"' : ""}>新增大模型</button>
          <button class="btn btn-sm" type="button" data-comfyui-multi-compare-add-lora="1"${state.loras.length >= maxLoras ? ' disabled="disabled"' : ""}>新增 LoRA</button>
        </div>
      </div>
      <div class="comfyui-multi-compare-rows">${checkpointRows}</div>
      <div class="comfyui-multi-compare-loras">
        <div class="drive-card-sub">共用 LoRA：會套到每個比較分支，方便固定 LoRA 條件下比較 checkpoint 差異。</div>
        ${loraRows}
      </div>
    </section>
  `;
}

function renderComfyuiUpscaleBreakpointControl(detail = comfyuiSelectedTemplateDetail) {
  const state = ensureComfyuiUpscaleBreakpointState(detail);
  if (!state) return "";
  if (comfyuiTemplateIsMultiMethodUpscaleModeTest(detail)) {
    const mode = normalizeComfyuiUpscaleBreakpointValue(detail, state.stage);
    return `
      <section class="comfyui-upscale-breakpoint-card">
        <div>
          <div class="drive-card-title">放大方式</div>
          <div class="drive-card-sub">選擇這次要跑模型放大、Latent 放大，或兩者組合；系統只顯示並送出對應參數。</div>
        </div>
        <select data-comfyui-upscale-breakpoint="1" aria-label="放大方式">
          <option value="model_upscale"${mode === "model_upscale" ? " selected" : ""}>模型放大：Origin 圖片直接套 Upscale 模型</option>
          <option value="latent_upscale"${mode === "latent_upscale" ? " selected" : ""}>Latent 放大：LatentUpscaleBy 後重繪輸出</option>
          <option value="combined_upscale"${mode === "combined_upscale" ? " selected" : ""}>組合使用：Latent 放大後再套 Upscale 模型</option>
        </select>
      </section>
    `;
  }
  const stage = state.stage === "second_upscale" ? "second_upscale" : "first_upscale";
  return `
    <section class="comfyui-upscale-breakpoint-card">
      <div>
        <div class="drive-card-title">放大斷點</div>
        <div class="drive-card-sub">選擇這次執行停在哪個階段；系統只保留對應輸出節點，不會同時跑 Origin、一次放大與二次放大。</div>
      </div>
      <select data-comfyui-upscale-breakpoint="1" aria-label="放大斷點">
        <option value="first_upscale"${stage === "first_upscale" ? " selected" : ""}>一次放大：停在 latent 放大與重繪後</option>
        <option value="second_upscale"${stage === "second_upscale" ? " selected" : ""}>二次放大：再套用 Upscale 模型</option>
      </select>
    </section>
  `;
}

function renderComfyuiTemplateEmbeddingShortcuts(field) {
  const values = Array.isArray(comfyuiAvailableEmbeddings) ? comfyuiAvailableEmbeddings : [];
  if (!values.length) return "";
  const targetAttr = comfyuiTemplateEmbeddingTargetIds(field).join("|");
  const content = values.map((value) => (
    `<button class="comfyui-embedding-chip" type="button" data-comfyui-template-embedding="${sanitize(value)}" data-comfyui-template-embedding-targets="${sanitize(targetAttr)}" title="插入 / 移除 ${sanitize(value)}">${sanitize(value)}</button>`
  )).join("");
  return `
    <div class="comfyui-template-field-card is-wide">
      <label>${sanitize(field?.label || "Embedding 快速插入")}</label>
      <div class="comfyui-embedding-shortcuts">${content}</div>
      <div class="drive-card-sub">點一下插入，再點一次會從提示詞移除。</div>
    </div>
  `;
}

function renderComfyuiTemplateField(field, detail, ctx) {
  if (field?.input_type === "embedding_shortcuts") {
    return renderComfyuiTemplateEmbeddingShortcuts(field);
  }
  const binding = comfyuiTemplateFieldBinding(field, detail, ctx);
  if (comfyuiTemplateIsHiddenField(detail, field)) return "";
  const fieldLabel = comfyuiTemplateFieldLabel(field, binding);
  const isMediaField = COMFYUI_TEMPLATE_MEDIA_BINDING_KINDS.has(binding.kind);
  const cardClass = field?.input_type === "textarea" || isMediaField ? "comfyui-template-field-card is-wide" : "comfyui-template-field-card";
  const promptRoleAttr = binding.promptRole ? ` data-comfyui-template-prompt-role="${sanitize(binding.promptRole)}"` : "";
  if (isMediaField) {
    const asset = comfyuiTemplateFieldValue(binding, field) || {};
    const isVideo = binding.kind === "video";
    const meta = COMFYUI_INPUT_ASSET_META[binding.assetKey] || {};
    const officialMediaFilename = comfyuiTemplateOfficialMediaFilename(field, detail);
    const officialMediaPreviewUrl = comfyuiTemplateOfficialMediaPreviewUrl(officialMediaFilename);
    const accept = isVideo
      ? (Array.isArray(field?.constraints?.accept_mime) ? field.constraints.accept_mime.join(",") : COMFYUI_TEMPLATE_VIDEO_ACCEPT)
      : "image/png,image/jpeg,image/webp";
    const previewHtml = asset.previewUrl
      ? (isVideo
        ? `<video src="${sanitize(asset.previewUrl)}" controls muted preload="metadata"></video>`
        : `<img src="${sanitize(asset.previewUrl)}" alt="${sanitize(fieldLabel || "圖片預覽")}" />`)
      : officialMediaPreviewUrl
        ? (isVideo
          ? `<video src="${sanitize(officialMediaPreviewUrl)}" controls muted preload="metadata"></video>`
          : `<img src="${sanitize(officialMediaPreviewUrl)}" alt="${sanitize(fieldLabel || "模板範例圖片")}" />`)
      : `<span class="drive-card-sub">${sanitize(meta.emptyText || (isVideo ? "尚未選擇影片" : "尚未選擇圖片"))}</span>`;
    const metaText = asset.file
      ? `已選擇本地檔：${asset.filename || asset.file.name || (isVideo ? "未命名影片" : "未命名圖片")}`
      : asset.imageRef?.filename
        ? `使用已保存${isVideo ? "影片" : "圖片"}：${asset.filename || asset.imageRef.filename}`
        : officialMediaFilename
          ? `使用模板範例${isVideo ? "影片" : "圖片"}：${officialMediaFilename}`
        : (meta.emptyText || (isVideo ? "尚未選擇影片" : "尚未選擇圖片"));
    const pickerButtons = isVideo
      ? `<button class="btn btn-sm" type="button" data-comfyui-template-media-clear="${sanitize(binding.assetKey)}">清除影片</button>`
      : `
          <button class="btn btn-sm" type="button" data-comfyui-template-image-picker="${sanitize(binding.assetKey)}">選擇既有圖片</button>
          <button class="btn btn-sm" type="button" data-comfyui-template-mask-editor="1">編輯遮罩</button>
          <button class="btn btn-sm" type="button" data-comfyui-template-media-clear="${sanitize(binding.assetKey)}">清除圖片</button>
        `;
    return `
      <div class="${cardClass}">
        <label>${sanitize(fieldLabel || (isVideo ? "影片" : "圖片"))}</label>
        <input type="file" ${isVideo ? "data-comfyui-template-video" : "data-comfyui-template-image"}="${sanitize(binding.assetKey)}" accept="${sanitize(accept)}" />
        <div class="comfyui-input-preview" style="margin-top:.55rem;">${previewHtml}</div>
        <div class="drive-card-sub" style="margin-top:.45rem;">${sanitize(metaText)}</div>
        <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.45rem;">
          ${pickerButtons}
        </div>
      </div>
    `;
  }
  if (binding.kind === "readonly") {
    const editAction = binding.editableLockedModel
      ? `
        <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.45rem;">
          <button class="btn btn-sm" type="button" data-comfyui-template-model-edit="${sanitize(field.id || "")}">編輯</button>
        </div>
        <div class="comfyui-template-direct-hint">保留模板預設可維持官方建議；需要客製化時可改成你 ComfyUI 內實際安裝的模型檔名。</div>
      `
      : "";
    return `
      <div class="${cardClass}">
        <label>${sanitize(fieldLabel)}</label>
        <div class="comfyui-template-readonly">這個欄位目前沿用模板預設值：${sanitize(comfyuiTemplateDisplayValue(field, field?.current_value))}</div>
        ${editAction}
      </div>
    `;
  }
  if (binding.kind === "direct") {
    const value = comfyuiTemplateFieldValue(binding, field);
    if (field?.input_type === "checkbox") {
      const checked = value === true || value === "true" || value === "1" || value === 1;
      const hint = comfyuiTemplateDirectHint(field);
      return `
        <div class="${cardClass}">
          <label for="tmpl-${sanitize(field.id || "")}">${sanitize(fieldLabel)}</label>
          <label class="comfyui-template-checkbox">
            <input id="tmpl-${sanitize(field.id || "")}" type="checkbox"${checked ? " checked" : ""} data-comfyui-template-direct-field="${sanitize(field.id || "")}" data-comfyui-template-label="${sanitize(fieldLabel)}" />
            <span>啟用</span>
          </label>
          ${hint ? `<div class="comfyui-template-direct-hint">${sanitize(hint)}</div>` : ""}
        </div>
      `;
    }
    const inputType = field?.input_type === "number" ? "number" : "text";
    const minAttr = field?.constraints?.min !== undefined ? ` min="${sanitize(String(field.constraints.min))}"` : "";
    const maxAttr = field?.constraints?.max !== undefined ? ` max="${sanitize(String(field.constraints.max))}"` : "";
    const stepAttr = field?.constraints?.step !== undefined ? ` step="${sanitize(String(field.constraints.step))}"` : "";
    const hint = comfyuiTemplateDirectHint(field);
    const seedAfterControl = comfyuiTemplateFieldIsSeed(field) ? renderComfyuiTemplateSeedAfterGenerateControl() : "";
    const modelEditActions = binding.editableLockedModel
      ? `
        <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.45rem;">
          <button class="btn btn-sm" type="button" data-comfyui-template-model-reset="${sanitize(field.id || "")}">恢復模板預設</button>
        </div>
      `
      : "";
    return `
      <div class="${cardClass}">
        <label for="tmpl-${sanitize(field.id || "")}">${sanitize(fieldLabel)}</label>
        <input id="tmpl-${sanitize(field.id || "")}" type="${sanitize(inputType)}" value="${sanitize(String(value ?? ""))}"${minAttr}${maxAttr}${stepAttr} data-comfyui-template-direct-field="${sanitize(field.id || "")}" data-comfyui-template-label="${sanitize(fieldLabel)}" />
        ${seedAfterControl}
        ${hint ? `<div class="comfyui-template-direct-hint">${sanitize(hint)}</div>` : ""}
        ${modelEditActions}
      </div>
    `;
  }
  if (binding.kind === "lora") {
    const selected = comfyuiSelectedLoraForTemplateNode(binding.nodeId);
    const overrideKey = String(binding.nodeId || "");
    const rawCurrent = Object.prototype.hasOwnProperty.call(comfyuiTemplateLoraOverrides, overrideKey)
      ? comfyuiTemplateLoraOverrides[overrideKey]
      : (selected?.name || String(field?.current_value || ""));
    const current = typeof normalizeComfyuiLoraName === "function"
      ? normalizeComfyuiLoraName(rawCurrent)
      : String(rawCurrent || "").trim();
    const options = comfyuiTemplateLoraSelectOptions(field);
    return `
      <div class="${cardClass}">
        <label for="tmpl-${sanitize(field.id || "")}">${sanitize(fieldLabel || "LoRA 模型")}</label>
        <select id="tmpl-${sanitize(field.id || "")}" data-comfyui-template-lora-node="${sanitize(binding.nodeId)}">
          ${options.map((option) => `<option value="${sanitize(option.value)}"${option.value === current ? " selected" : ""}${option.disabled ? ' disabled="disabled"' : ""}>${sanitize(option.label)}</option>`).join("")}
        </select>
        <div class="drive-card-sub">選擇後會加入 LoRA 清單，並自動把 Civitai trigger words 補到正向提示詞。</div>
      </div>
    `;
  }
  if (binding.kind === "lora_strength") {
    const selected = comfyuiSelectedLoraForTemplateNode(binding.nodeId);
    const value = selected?.[binding.strengthField] ?? field?.current_value ?? 1;
    const minAttr = field?.constraints?.min !== undefined ? ` min="${sanitize(String(field.constraints.min))}"` : "";
    const maxAttr = field?.constraints?.max !== undefined ? ` max="${sanitize(String(field.constraints.max))}"` : "";
    const stepAttr = field?.constraints?.step !== undefined ? ` step="${sanitize(String(field.constraints.step))}"` : "";
    return `
      <div class="${cardClass}">
        <label for="tmpl-${sanitize(field.id || "")}">${sanitize(fieldLabel || "LoRA 權重")}</label>
        <input id="tmpl-${sanitize(field.id || "")}" type="number" value="${sanitize(String(value ?? 1))}"${minAttr}${maxAttr}${stepAttr} data-comfyui-template-lora-strength="${sanitize(binding.nodeId)}" data-comfyui-template-lora-strength-field="${sanitize(binding.strengthField)}" />
      </div>
    `;
  }
  const value = comfyuiTemplateFieldValue(binding, field);
  if (field?.input_type === "textarea") {
    return `
      <div class="${cardClass}">
        <label for="tmpl-${sanitize(field.id || "")}">${sanitize(fieldLabel)}</label>
        <textarea id="tmpl-${sanitize(field.id || "")}" rows="${sanitize(String(field?.constraints?.rows || 4))}" data-comfyui-template-target="${sanitize(binding.targetId)}" data-comfyui-template-label="${sanitize(fieldLabel)}"${promptRoleAttr}>${sanitize(value)}</textarea>
      </div>
    `;
  }
  if (field?.input_type === "select") {
    const options = comfyuiTemplateSelectOptions(binding.targetId, field);
    const current = comfyuiTemplateSelectCurrentValue(binding.targetId, field, options);
    const fallbackHint = comfyuiTemplateSelectFallbackHint(binding.targetId, field, current);
    return `
      <div class="${cardClass}">
        <label for="tmpl-${sanitize(field.id || "")}">${sanitize(fieldLabel)}</label>
        <select id="tmpl-${sanitize(field.id || "")}" data-comfyui-template-target="${sanitize(binding.targetId)}"${promptRoleAttr}>
          ${options.map((option) => `<option value="${sanitize(option.value)}"${option.value === current ? " selected" : ""}${option.disabled ? ' disabled="disabled"' : ""}>${sanitize(option.label)}</option>`).join("")}
        </select>
        ${fallbackHint ? `<div class="comfyui-template-direct-hint">${sanitize(fallbackHint)}</div>` : ""}
      </div>
    `;
  }
  const inputType = field?.input_type === "number" ? "number" : "text";
  const minAttr = field?.constraints?.min !== undefined ? ` min="${sanitize(String(field.constraints.min))}"` : "";
  const maxAttr = field?.constraints?.max !== undefined ? ` max="${sanitize(String(field.constraints.max))}"` : "";
  const stepAttr = field?.constraints?.step !== undefined ? ` step="${sanitize(String(field.constraints.step))}"` : "";
  const seedAfterControl = comfyuiTemplateFieldIsSeed(field) ? renderComfyuiTemplateSeedAfterGenerateControl() : "";
  return `
    <div class="${cardClass}">
      <label for="tmpl-${sanitize(field.id || "")}">${sanitize(fieldLabel)}</label>
      <input id="tmpl-${sanitize(field.id || "")}" type="${sanitize(inputType)}" value="${sanitize(String(value ?? ""))}"${minAttr}${maxAttr}${stepAttr} data-comfyui-template-target="${sanitize(binding.targetId)}" data-comfyui-template-label="${sanitize(fieldLabel)}"${promptRoleAttr} />
      ${seedAfterControl}
    </div>
  `;
}

function bindRenderedComfyuiTemplateFields(detail) {
  const host = $("comfyui-template-panels");
  if (!host) return;
  host.querySelectorAll("[data-comfyui-template-prompt-sharing]").forEach((select) => {
    if (select.dataset.boundComfyuiTemplate === "1") return;
    select.dataset.boundComfyuiTemplate = "1";
    select.addEventListener("change", () => {
      comfyuiTemplatePromptShareMode = ["shared", "independent"].includes(select.value) ? select.value : "ask";
      if (comfyuiTemplatePromptShareMode === "shared") {
        ["positive", "negative"].forEach((role) => {
          syncComfyuiTemplateSharedPromptFields(role, comfyuiTemplateSharedPromptValue(role, detail));
        });
      }
      writeComfyuiDraft();
      renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
    });
  });
  host.querySelectorAll("[data-comfyui-template-target]").forEach((el) => {
    const fieldId = el.id && el.id.startsWith("tmpl-") ? el.id.slice("tmpl-".length) : "";
    if (el.dataset.boundComfyuiTemplate === "1") return;
    el.dataset.boundComfyuiTemplate = "1";
    const sync = () => {
      if (fieldId) comfyuiTemplateFieldOverrides[fieldId] = comfyuiTemplateElementValue(el);
      const field = (detail?.ui_schema?.panels || [])
        .flatMap((panel) => panel?.fields || [])
        .find((item) => String(item?.id || "") === fieldId);
      if (comfyuiTemplatePromptSharingMode(detail) === "shared" && comfyuiTemplateIsPromptTextField(field)) {
        syncComfyuiTemplateSharedPromptFields(el.getAttribute("data-comfyui-template-prompt-role") || comfyuiTemplatePromptRole(field), el.value);
      }
      writeComfyuiDraft();
    };
    el.addEventListener("input", sync);
    el.addEventListener("change", sync);
  });
  host.querySelectorAll("[data-comfyui-template-direct-field]").forEach((el) => {
    if (el.dataset.boundComfyuiTemplate === "1") return;
    el.dataset.boundComfyuiTemplate = "1";
    const fieldId = el.getAttribute("data-comfyui-template-direct-field") || "";
    const sync = () => {
      if (fieldId) comfyuiTemplateFieldOverrides[fieldId] = comfyuiTemplateElementValue(el);
      writeComfyuiDraft();
    };
    el.addEventListener("input", sync);
    el.addEventListener("change", sync);
  });
  host.querySelectorAll("[data-comfyui-template-model-edit]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => {
      const fieldId = button.getAttribute("data-comfyui-template-model-edit") || "";
      if (!fieldId) return;
      comfyuiTemplateEditableModelFields[fieldId] = true;
      renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
    });
  });
  host.querySelectorAll("[data-comfyui-template-model-reset]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => {
      const fieldId = button.getAttribute("data-comfyui-template-model-reset") || "";
      if (!fieldId) return;
      delete comfyuiTemplateEditableModelFields[fieldId];
      delete comfyuiTemplateFieldOverrides[fieldId];
      writeComfyuiDraft();
      renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
    });
  });
  host.querySelectorAll("[data-comfyui-template-seed-after-generate]").forEach((select) => {
    if (select.dataset.boundComfyuiTemplate === "1") return;
    select.dataset.boundComfyuiTemplate = "1";
    select.addEventListener("change", () => {
      if (typeof setComfyuiSeedAfterGenerateMode === "function") {
        setComfyuiSeedAfterGenerateMode(select.value);
      } else if ($("comfyui-seed-after-generate")) {
        $("comfyui-seed-after-generate").value = select.value;
        writeComfyuiDraft();
      }
      host.querySelectorAll("[data-comfyui-template-seed-after-generate]").forEach((other) => {
        other.value = select.value;
      });
    });
  });
  host.querySelectorAll("[data-comfyui-upscale-breakpoint]").forEach((select) => {
    if (select.dataset.boundComfyuiTemplate === "1") return;
    select.dataset.boundComfyuiTemplate = "1";
    select.addEventListener("change", () => {
      const state = ensureComfyuiUpscaleBreakpointState(detail);
      if (!state) return;
      state.stage = normalizeComfyuiUpscaleBreakpointValue(detail, select.value);
      writeComfyuiDraft();
      renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
    });
  });
  host.querySelectorAll("[data-comfyui-multi-compare-add-checkpoint]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => {
      const state = ensureComfyuiMultiCompareState(detail);
      if (!state || state.checkpoints.length >= COMFYUI_MULTI_COMPARE_MAX_CHECKPOINTS) return;
      const fallback = state.checkpoints[state.checkpoints.length - 1] || $("comfyui-model-select")?.value || "";
      state.checkpoints.push(fallback);
      renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
    });
  });
  host.querySelectorAll("[data-comfyui-multi-compare-remove-checkpoint]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => {
      const state = ensureComfyuiMultiCompareState(detail);
      const index = Number(button.getAttribute("data-comfyui-multi-compare-remove-checkpoint"));
      if (!state || state.checkpoints.length <= 2 || !Number.isInteger(index)) return;
      state.checkpoints.splice(index, 1);
      renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
    });
  });
  host.querySelectorAll("[data-comfyui-multi-compare-checkpoint]").forEach((select) => {
    if (select.dataset.boundComfyuiTemplate === "1") return;
    select.dataset.boundComfyuiTemplate = "1";
    select.addEventListener("change", () => {
      const state = ensureComfyuiMultiCompareState(detail);
      const index = Number(select.getAttribute("data-comfyui-multi-compare-checkpoint"));
      if (!state || !Number.isInteger(index)) return;
      state.checkpoints[index] = select.value || "";
    });
  });
  host.querySelectorAll("[data-comfyui-multi-compare-add-lora]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => {
      addComfyuiMultiCompareLora(detail);
    });
  });
  host.querySelectorAll("[data-comfyui-multi-compare-remove-lora]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => {
      const index = Number(button.getAttribute("data-comfyui-multi-compare-remove-lora"));
      removeComfyuiMultiCompareLoraAt(detail, index);
    });
  });
  host.querySelectorAll("[data-comfyui-multi-compare-lora]").forEach((select) => {
    if (select.dataset.boundComfyuiTemplate === "1") return;
    select.dataset.boundComfyuiTemplate = "1";
    select.addEventListener("change", () => {
      const index = Number(select.getAttribute("data-comfyui-multi-compare-lora"));
      if (setComfyuiMultiCompareLoraName(detail, index, select.value)) {
        renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
      }
    });
  });
  host.querySelectorAll("[data-comfyui-multi-compare-lora-strength-model],[data-comfyui-multi-compare-lora-strength-clip]").forEach((input) => {
    if (input.dataset.boundComfyuiTemplate === "1") return;
    input.dataset.boundComfyuiTemplate = "1";
    const sync = () => {
      const modelIndex = input.getAttribute("data-comfyui-multi-compare-lora-strength-model");
      const clipIndex = input.getAttribute("data-comfyui-multi-compare-lora-strength-clip");
      const index = Number(modelIndex !== null ? modelIndex : clipIndex);
      const field = modelIndex !== null ? "strength_model" : "strength_clip";
      const state = ensureComfyuiMultiCompareState(detail);
      if (!state || !Number.isInteger(index) || !state.loras[index]) return;
      const value = Number(input.value || 1);
      const normalized = Math.round(Math.max(-2, Math.min(2, Number.isFinite(value) ? value : 1)) * 100) / 100;
      state.loras[index][field] = normalized;
      input.value = String(normalized);
    };
    input.addEventListener("input", sync);
    input.addEventListener("change", sync);
  });
  host.querySelectorAll("[data-comfyui-template-image]").forEach((input) => {
    if (input.dataset.boundComfyuiTemplate === "1") return;
    input.dataset.boundComfyuiTemplate = "1";
    input.addEventListener("change", () => {
      const assetKey = input.getAttribute("data-comfyui-template-image");
      const file = input.files && input.files[0] ? input.files[0] : null;
      if (!file) {
        clearComfyuiInputAsset(assetKey);
        renderSelectedComfyuiTemplate();
        return;
      }
      if (!/^image\/(png|jpeg|webp)$/i.test(file.type || "")) {
        setComfyuiMessage("模板圖片欄位只支援 PNG、JPG、WEBP。", false);
        input.value = "";
        return;
      }
      setComfyuiInputAssetFromFile(assetKey, file);
      renderSelectedComfyuiTemplate();
    });
  });
  host.querySelectorAll("[data-comfyui-template-video]").forEach((input) => {
    if (input.dataset.boundComfyuiTemplate === "1") return;
    input.dataset.boundComfyuiTemplate = "1";
    input.addEventListener("change", () => {
      const assetKey = input.getAttribute("data-comfyui-template-video");
      const file = input.files && input.files[0] ? input.files[0] : null;
      if (!file) {
        clearComfyuiInputAsset(assetKey);
        renderSelectedComfyuiTemplate();
        return;
      }
      const name = String(file.name || "").toLowerCase();
      const validExt = /\.(mp4|webm|mov|mkv|avi)$/.test(name);
      const validMime = !file.type || /^video\/(mp4|webm|quicktime|x-matroska|x-msvideo)$/i.test(file.type || "") || file.type === "application/octet-stream";
      if (!validExt || !validMime) {
        setComfyuiMessage("模板影片欄位只支援 MP4、WEBM、MOV、MKV、AVI。", false);
        input.value = "";
        return;
      }
      setComfyuiInputAssetFromFile(assetKey, file);
      renderSelectedComfyuiTemplate();
    });
  });
  host.querySelectorAll("[data-comfyui-template-media-clear],[data-comfyui-template-image-clear]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => {
      clearComfyuiInputAsset(button.getAttribute("data-comfyui-template-media-clear") || button.getAttribute("data-comfyui-template-image-clear"));
      renderSelectedComfyuiTemplate();
    });
  });
  host.querySelectorAll("[data-comfyui-template-image-picker]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => {
      openComfyuiImagePicker(button.getAttribute("data-comfyui-template-image-picker"))
        .catch((err) => setComfyuiMessage(err.message || "圖片選擇器開啟失敗", false));
    });
  });
  host.querySelectorAll("[data-comfyui-template-mask-editor]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => openComfyuiMaskEditor());
  });
  host.querySelectorAll("[data-comfyui-template-lora-node]").forEach((select) => {
    if (select.dataset.boundComfyuiTemplate === "1") return;
    select.dataset.boundComfyuiTemplate = "1";
    select.addEventListener("change", () => {
      const nodeId = select.getAttribute("data-comfyui-template-lora-node");
      if (upsertComfyuiTemplateLora(nodeId, select.value)) {
        renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
      }
    });
  });
  host.querySelectorAll("[data-comfyui-template-lora-strength]").forEach((input) => {
    if (input.dataset.boundComfyuiTemplate === "1") return;
    input.dataset.boundComfyuiTemplate = "1";
    const sync = () => {
      const nodeId = input.getAttribute("data-comfyui-template-lora-strength");
      const field = input.getAttribute("data-comfyui-template-lora-strength-field");
      const normalized = updateComfyuiTemplateLoraStrength(nodeId, field, input.value);
      if (normalized !== null) input.value = String(normalized);
    };
    input.addEventListener("input", sync);
    input.addEventListener("change", sync);
  });
  host.querySelectorAll("[data-comfyui-template-embedding]").forEach((button) => {
    if (button.dataset.boundComfyuiTemplate === "1") return;
    button.dataset.boundComfyuiTemplate = "1";
    button.addEventListener("click", () => {
      const targets = String(button.getAttribute("data-comfyui-template-embedding-targets") || "")
        .split("|")
        .map((value) => value.trim())
        .filter(Boolean);
      insertComfyuiTemplateEmbeddingToken(button.getAttribute("data-comfyui-template-embedding"), targets);
      renderSelectedComfyuiTemplate({ preserveOpenPanels: true });
    });
  });
}

function renderSelectedComfyuiTemplate({ preserveOpenPanels = false } = {}) {
  const summary = $("comfyui-template-summary");
  const host = $("comfyui-template-panels");
  const legacy = $("comfyui-legacy-form-panel");
  if (summary) summary.innerHTML = comfyuiTemplateSummaryMarkup(comfyuiSelectedTemplateDetail);
  if (!host) return;
  if (!comfyuiSelectedTemplateDetail?.ui_schema?.panels) {
    if (summary) summary.hidden = true;
    host.hidden = true;
    host.innerHTML = "";
    if (legacy) legacy.style.display = "none";
    if (typeof updateComfyuiDiffusersUi === "function") updateComfyuiDiffusersUi();
    return;
  }
  if (summary) summary.hidden = false;
  host.hidden = false;
  const detail = comfyuiSelectedTemplateDetail;
  const ctx = { textFieldIndex: 0, loadImageIndex: 0 };
  const panels = (detail.ui_schema.panels || []).filter((panel) => !["compatibility", "raw"].includes(String(panel?.id || "")));
  const openPanelIds = preserveOpenPanels
    ? new Set(Array.from(host.querySelectorAll("[data-comfyui-template-panel-id]"))
      .filter((section) => section.open)
      .map((section) => section.getAttribute("data-comfyui-template-panel-id")))
    : new Set();
  const promptSharingHtml = renderComfyuiTemplatePromptSharingControl(detail);
  const multiCompareHtml = renderComfyuiMultiCompareControl(detail);
  const upscaleBreakpointHtml = renderComfyuiUpscaleBreakpointControl(detail);
  host.innerHTML = promptSharingHtml + multiCompareHtml + upscaleBreakpointHtml + panels.map((panel) => {
    const panelId = String(panel?.id || "");
    const isOpen = preserveOpenPanels ? openPanelIds.has(panelId) : !panel?.collapsed_default;
    const visibleFieldCount = (panel?.fields || []).filter((field) => (
      !field?.synthetic
      && field?.input_type !== "embedding_shortcuts"
      && !comfyuiTemplateIsHiddenField(detail, field)
    )).length;
    return `
    <details class="drive-collapsible-panel settings-collapse comfyui-template-render-card" data-comfyui-template-panel-id="${sanitize(panelId)}"${isOpen ? " open" : ""}>
      <summary>
        <div>
          <div class="drive-card-title">${sanitize(panel?.label || panel?.id || "模板區塊")}</div>
          <div class="drive-card-sub">${sanitize(String(visibleFieldCount || 0))} 個欄位</div>
        </div>
      </summary>
      <div class="drive-collapsible-body">
        <div class="comfyui-template-panel-grid">
          ${(panel?.fields || []).map((field) => renderComfyuiTemplateField(field, detail, ctx)).join("")}
        </div>
      </div>
    </details>
  `;
  }).join("");
  bindRenderedComfyuiTemplateFields(detail);
  if (legacy) legacy.style.display = "none";
  if (typeof updateComfyuiDiffusersUi === "function") updateComfyuiDiffusersUi();
}

function renderComfyuiWorkflowPresetList(targetId, items, emptyText) {
  const list = $(targetId);
  if (!list) return;
  if (!Array.isArray(items) || !items.length) {
    list.innerHTML = `<div class="drive-empty">${sanitize(emptyText)}</div>`;
    return;
  }
  list.innerHTML = items.map((item) => {
    const dependency = item?.dependency_status || null;
    const dependencyHtml = comfyuiWorkflowDependencyHtml(dependency);
    const defaultModelNotice = comfyuiWorkflowDefaultModelNoticeHtml(item, 4);
    const models = Array.isArray(item?.required_models) ? item.required_models.map((entry) => `${entry.kind || "model"}:${entry.name || ""}`) : [];
    const loras = Array.isArray(item?.required_loras) ? item.required_loras.map((entry) => entry.name || entry) : [];
    const controlnets = Array.isArray(item?.required_controlnets) ? item.required_controlnets.map((entry) => entry.name || entry) : [];
    const customNodes = Array.isArray(item?.required_custom_nodes) ? item.required_custom_nodes : [];
    const manifest = item?.manifest_summary || {};
    const mode = item?.default_params?.generation_mode ? comfyuiReadableModeLabel(item.default_params.generation_mode) : "Workflow";
    const purpose = item?.purpose || item?.default_params?.generation_mode || "custom";
    const versionWarnings = Array.isArray(item?.version_warnings) ? item.version_warnings : [];
    return `
      <details class="comfyui-workflow-item">
        <summary class="comfyui-workflow-item-summary">
          <span class="comfyui-workflow-expand-indicator" aria-hidden="true"></span>
          <div class="comfyui-workflow-item-head">
          <div class="comfyui-workflow-item-title">
            <strong>${sanitize(item?.title || `Workflow #${item?.id || ""}`)}</strong>
            <span>${sanitize(mode)} · ${sanitize(purpose)} · ${sanitize(String(item?.updated_at || "").replace("T", " ").slice(0, 16))}</span>
            ${defaultModelNotice}
          </div>
          <div class="comfyui-workflow-flags">
            ${item?.is_official ? '<span class="comfyui-workflow-chip">官方</span>' : ""}
            ${item?.is_default ? '<span class="comfyui-workflow-chip">預設</span>' : ""}
            <span class="comfyui-workflow-chip">${sanitize(item?.visibility || "private")}</span>
            ${dependency?.available === false ? '<span class="comfyui-workflow-chip bad">缺少依賴</span>' : ""}
            ${models.length ? `<span class="comfyui-workflow-chip">模型 ${sanitize(String(models.length))}</span>` : ""}
            ${loras.length ? `<span class="comfyui-workflow-chip">LoRA ${sanitize(String(loras.length))}</span>` : ""}
            ${controlnets.length ? `<span class="comfyui-workflow-chip">ControlNet ${sanitize(String(controlnets.length))}</span>` : ""}
            ${customNodes.length ? `<span class="comfyui-workflow-chip warn">Custom nodes ${sanitize(String(customNodes.length))}</span>` : ""}
          </div>
        </div>
        </summary>
        <div class="comfyui-workflow-item-body">
        <div class="drive-card-sub">${sanitize(item?.description || "未填寫說明")}</div>
        ${comfyuiWorkflowDefaultModelNoticeHtml(item, 12)}
        <div class="comfyui-workflow-meta">
          <span class="comfyui-workflow-chip">Project ${sanitize(item?.project_version || "-")}</span>
          <span class="comfyui-workflow-chip">ComfyUI ${sanitize(item?.comfyui_version || "-")}</span>
          ${manifest?.available ? `<span class="comfyui-workflow-chip">Manifest ${sanitize(String(manifest.panel_count || 0))} panels</span>` : ""}
          <span class="comfyui-workflow-chip">${sanitize(String((item?.workflow_hash || "").slice(0, 12) || "-"))}</span>
          ${models.length ? `<span class="comfyui-workflow-chip">模型 ${sanitize(String(models.length))}</span>` : ""}
          ${loras.length ? `<span class="comfyui-workflow-chip">LoRA ${sanitize(String(loras.length))}</span>` : ""}
          ${controlnets.length ? `<span class="comfyui-workflow-chip">ControlNet ${sanitize(String(controlnets.length))}</span>` : ""}
          ${customNodes.length ? `<span class="comfyui-workflow-chip warn">Custom nodes ${sanitize(String(customNodes.length))}</span>` : ""}
        </div>
        ${versionWarnings.length ? `<div class="drive-card-sub" style="margin-top:.4rem;color:#ffe08a;">版本警告：${sanitize(versionWarnings.join("；"))}</div>` : ""}
        ${comfyuiWorkflowPaidApiWarningHtml(item)}
        ${dependencyHtml}
        <div class="drive-card-sub">所需模型：${sanitize(models.join(", ") || "無")}</div>
        <div class="drive-card-sub">所需 LoRA：${sanitize(loras.join(", ") || "無")}</div>
        <div class="drive-card-sub">所需 ControlNet：${sanitize(controlnets.join(", ") || "無")}</div>
        <div class="drive-card-sub">所需 Custom nodes：${sanitize(customNodes.join(", ") || "無")}</div>
        ${renderComfyuiWorkflowRunList(item?.recent_runs || [])}
        <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.55rem;">
          <button class="btn btn-sm" type="button" data-comfyui-workflow-apply="${item.id}">套回表單</button>
          <button class="btn btn-sm" type="button" data-comfyui-workflow-run="${item.id}">執行</button>
          <button class="btn btn-sm" type="button" data-comfyui-workflow-export="${item.id}">匯出 JSON</button>
          <button class="btn btn-sm" type="button" data-comfyui-workflow-edit="${item.id}">載入編輯</button>
          <button class="btn btn-sm" type="button" data-comfyui-workflow-duplicate="${item.id}">複製</button>
          ${item?.can_edit ? `<button class="btn btn-sm" type="button" data-comfyui-workflow-default="${item.id}">設為預設</button>` : ""}
          ${item?.can_publish_official && !item?.is_official ? `<button class="btn btn-sm" type="button" data-comfyui-workflow-publish="${item.id}">發布官方</button>` : ""}
          ${item?.can_edit ? `<button class="btn btn-sm" type="button" data-comfyui-workflow-delete="${item.id}">刪除</button>` : ""}
        </div>
        </div>
      </details>
    `;
  }).join("");
  list.querySelectorAll("[data-comfyui-workflow-apply]").forEach((button) => {
    button.addEventListener("click", () => applyComfyuiWorkflowPresetToForm(Number(button.getAttribute("data-comfyui-workflow-apply"))));
  });
  list.querySelectorAll("[data-comfyui-workflow-run]").forEach((button) => {
    button.addEventListener("click", () => {
      runComfyuiWorkflowPreset(Number(button.getAttribute("data-comfyui-workflow-run"))).catch((err) => setComfyuiMessage(err.message || "workflow 執行失敗", false));
    });
  });
  list.querySelectorAll("[data-comfyui-workflow-export]").forEach((button) => {
    button.addEventListener("click", () => {
      exportComfyuiWorkflowPreset(Number(button.getAttribute("data-comfyui-workflow-export"))).catch((err) => setComfyuiMessage(err.message || "workflow 匯出失敗", false));
    });
  });
  list.querySelectorAll("[data-comfyui-workflow-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      loadComfyuiWorkflowPresetIntoEditor(Number(button.getAttribute("data-comfyui-workflow-edit"))).catch((err) => setComfyuiMessage(err.message || "workflow 讀取失敗", false));
    });
  });
  list.querySelectorAll("[data-comfyui-workflow-duplicate]").forEach((button) => {
    button.addEventListener("click", () => {
      duplicateComfyuiWorkflowPreset(Number(button.getAttribute("data-comfyui-workflow-duplicate"))).catch((err) => setComfyuiMessage(err.message || "workflow 複製失敗", false));
    });
  });
  list.querySelectorAll("[data-comfyui-workflow-default]").forEach((button) => {
    button.addEventListener("click", () => {
      setDefaultComfyuiWorkflowPreset(Number(button.getAttribute("data-comfyui-workflow-default"))).catch((err) => setComfyuiMessage(err.message || "預設版面設定失敗", false));
    });
  });
  list.querySelectorAll("[data-comfyui-workflow-publish]").forEach((button) => {
    button.addEventListener("click", () => {
      publishComfyuiWorkflowPresetOfficial(Number(button.getAttribute("data-comfyui-workflow-publish"))).catch((err) => setComfyuiMessage(err.message || "官方 preset 發布失敗", false));
    });
  });
  list.querySelectorAll("[data-comfyui-workflow-delete]").forEach((button) => {
    button.addEventListener("click", () => {
      deleteComfyuiWorkflowPreset(Number(button.getAttribute("data-comfyui-workflow-delete"))).catch((err) => setComfyuiMessage(err.message || "workflow 刪除失敗", false));
    });
  });
}

function renderComfyuiWorkflowPresets(payload = {}, { silentTemplateReload = true } = {}) {
  comfyuiWorkflowPresets = Array.isArray(payload.presets) ? payload.presets : [];
  renderComfyuiWorkflowPresetList("comfyui-workflow-my-list", payload.my_presets || [], "尚無個人工作流版面");
  renderComfyuiWorkflowPresetList("comfyui-workflow-official-list", payload.official_presets || [], "尚無官方工作流版面");
  renderComfyuiWorkflowPresetList("comfyui-workflow-shared-list", payload.shared_presets || [], "尚無其他可讀工作流版面");
  renderComfyuiTemplateSelector(payload, { silentReload: silentTemplateReload });
  const total = comfyuiWorkflowPresets.length;
  const warning = payload.dependency_warning ? `；依賴檢查警告：${payload.dependency_warning}` : "";
  setComfyuiWorkflowStatus(`目前可見 ${total} 個 workflow 版面${warning}`);
}

async function loadComfyuiWorkflowPresets() {
  const { silentTemplateReload = true } = arguments[0] || {};
  if (!currentUser || !canAccessModule("comfyui")) return [];
  setComfyuiWorkflowStatus("正在讀取 workflow preset...");
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/comfyui/workflows", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    const message = json.msg || `workflow preset 讀取失敗（HTTP ${res.status}）`;
    setComfyuiWorkflowStatus(message);
    throw new Error(message);
  }
  renderComfyuiWorkflowPresets(json, { silentTemplateReload });
  return comfyuiWorkflowPresets;
}

function applyComfyuiWorkflowPresetDefaults(defaults = {}) {
  const payload = defaults || {};
  const controlnet = payload.controlnet || {};
  const loras = Array.isArray(payload.loras) ? payload.loras : [];
  comfyuiSelectedLoras = loras
    .filter((entry) => entry && typeof entry === "object" && entry.name)
    .slice(0, COMFYUI_MAX_LORAS)
    .map((entry) => ({
      name: String(entry.name),
      strength_model: Number.isFinite(Number(entry.strength_model)) ? Number(entry.strength_model) : 1,
      strength_clip: Number.isFinite(Number(entry.strength_clip)) ? Number(entry.strength_clip) : 1,
    }));
  renderComfyuiSelectedLoras();
  [
    ["comfyui-generation-mode", payload.generation_mode || "txt2img"],
    ["comfyui-model-select", payload.model || ""],
    ["comfyui-vae-select", COMFYUI_VAE_BUILTIN],
    ["comfyui-prompt", payload.prompt || ""],
    ["comfyui-negative-prompt", payload.negative_prompt || ""],
    ["comfyui-width", payload.width || comfyuiDefaultWidth],
    ["comfyui-height", payload.height || comfyuiDefaultHeight],
    ["comfyui-steps", payload.steps || 20],
    ["comfyui-cfg", payload.cfg || 7],
    ["comfyui-batch-size", payload.batch_size || 1],
    ["comfyui-seed", payload.seed ?? ""],
    ["comfyui-sampler", payload.sampler_name || "euler"],
    ["comfyui-scheduler", payload.scheduler || "normal"],
    ["comfyui-denoise-strength", payload.denoise_strength ?? 0.65],
    ["comfyui-upscale-model", payload.upscale_model || ""],
    ["comfyui-controlnet-type", controlnet.type || "canny"],
    ["comfyui-controlnet-model", controlnet.model_name || ""],
    ["comfyui-controlnet-preprocessor", controlnet.preprocessor || ""],
    ["comfyui-control-strength", controlnet.strength ?? 1],
    ["comfyui-control-start", controlnet.start_percent ?? 0],
    ["comfyui-control-end", controlnet.end_percent ?? 1],
  ].forEach(([id, value]) => setComfyuiFieldValue(id, value));
  if ($("comfyui-controlnet-enabled")) $("comfyui-controlnet-enabled").checked = !!controlnet?.type;
  updateComfyuiModeVisibility();
  writeComfyuiDraft();
}

function applyComfyuiWorkflowPresetToForm(presetId) {
  const item = comfyuiWorkflowPresetById(presetId);
  if (!item) {
    setComfyuiMessage("找不到這個 workflow preset。", false);
    return;
  }
  applyComfyuiWorkflowPresetDefaults(item.default_params || {});
  setComfyuiMessage(`已套用「${item.title || `Workflow #${presetId}`}」的預設參數；若 workflow 需要來源圖、遮罩或控制圖，請另外確認目前表單已提供。`, true);
}

async function loadComfyuiWorkflowPresetIntoEditor(presetId) {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + `/comfyui/workflows/${encodeURIComponent(presetId)}`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `workflow 讀取失敗（HTTP ${res.status}）`);
  const preset = json.preset || {};
  comfyuiWorkflowCurrentPresetId = Number(preset.id) || null;
  comfyuiWorkflowEditorDefaults = preset.default_params || null;
  setComfyuiFieldValue("comfyui-workflow-title", preset.title || "");
  setComfyuiFieldValue("comfyui-workflow-description", preset.description || "");
  setComfyuiFieldValue("comfyui-workflow-visibility", preset.visibility || "private");
  setComfyuiFieldValue("comfyui-workflow-purpose", preset.purpose || preset.default_params?.generation_mode || "custom");
  setComfyuiFieldValue("comfyui-workflow-comfyui-version", preset.comfyui_version || "");
  setComfyuiFieldValue("comfyui-workflow-project-version", preset.project_version || "");
  setComfyuiFieldValue("comfyui-workflow-schema-version", preset.workflow_schema_version || "1");
  setComfyuiFieldValue("comfyui-workflow-json", JSON.stringify(preset.workflow_json || {}, null, 2));
  setComfyuiFieldValue("comfyui-workflow-layout-json", JSON.stringify(preset.layout_json || {}, null, 2));
  renderComfyuiWorkflowBuilderPreview();
  const defaultInput = $("comfyui-workflow-is-default");
  if (defaultInput) defaultInput.checked = !!preset.is_default;
  const updateBtn = $("comfyui-workflow-update-btn");
  if (updateBtn) updateBtn.disabled = !preset.can_edit;
  const versionCount = Array.isArray(preset.layout_versions) ? preset.layout_versions.length : 0;
  setComfyuiWorkflowStatus(`正在編輯 #${preset.id} ${preset.title || ""}${versionCount ? `；保留 ${versionCount} 筆版本紀錄` : ""}`);
  const note = $("comfyui-workflow-editor-note");
  if (note) note.textContent = "已載入版面。修改後必須按「更新目前選擇」才會保存。";
}

function comfyuiCurrentWorkflowExportable() {
  const mode = comfyuiGenerationMode();
  if (comfyuiModeUsesSourceImage(mode) && comfyuiInputAssets.source?.file && !comfyuiInputAssets.source?.imageRef) {
    return "目前來源圖尚未有可重用 image_ref；若要匯出 img2img / inpaint / outpaint / upscale workflow，請先使用已上傳來源圖或套用歷史紀錄。";
  }
  if (comfyuiModeUsesMaskImage(mode) && comfyuiInputAssets.mask?.file && !comfyuiInputAssets.mask?.imageRef) {
    return "目前遮罩尚未有可重用 image_ref；請先使用已上傳遮罩或套用歷史紀錄。";
  }
  if (isComfyuiControlnetEnabled() && comfyuiInputAssets.control?.file && !comfyuiInputAssets.control?.imageRef) {
    return "目前控制圖尚未有可重用 image_ref；請先使用已上傳控制圖或套用歷史紀錄。";
  }
  return "";
}

async function exportCurrentComfyuiWorkflow() {
  const blocking = comfyuiCurrentWorkflowExportable();
  if (blocking) {
    setComfyuiMessage(blocking, false);
    return;
  }
  const payload = comfyuiPayload();
  const uiMessage = comfyuiValidatePayloadForUi(payload);
  if (uiMessage) {
    setComfyuiMessage(uiMessage, false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/comfyui/workflows/export-current", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || "",
    },
    body: JSON.stringify(payload),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `workflow 匯出失敗（HTTP ${res.status}）`);
  comfyuiWorkflowEditorDefaults = json.default_params || payload;
  setComfyuiFieldValue("comfyui-workflow-json", json.workflow_text || JSON.stringify(json.workflow_json || {}, null, 2));
  setComfyuiFieldValue("comfyui-workflow-layout-json", json.layout_text || JSON.stringify(json.layout_json || {}, null, 2));
  if (!$("comfyui-workflow-purpose")?.value && json.default_params?.generation_mode) setComfyuiFieldValue("comfyui-workflow-purpose", json.default_params.generation_mode);
  if (json.workflow_preset_json?.project_version) setComfyuiFieldValue("comfyui-workflow-project-version", json.workflow_preset_json.project_version);
  if (json.workflow_preset_json?.workflow_schema_version) setComfyuiFieldValue("comfyui-workflow-schema-version", json.workflow_preset_json.workflow_schema_version);
  setComfyuiWorkflowStatus(`已匯出目前 workflow，hash ${String((json.workflow_hash || "").slice(0, 12) || "-")}`);
  setComfyuiMessage("已把目前表單轉成 workflow 與 layout JSON，可直接保存成自訂版面。", true);
  downloadComfyuiWorkflowText(`comfyui-current-workflow-layout-${Date.now()}.json`, json.workflow_preset_text || JSON.stringify(json.workflow_preset_json || json.workflow_json || {}, null, 2));
}

async function importComfyuiWorkflowPreset() {
  const workflowText = String($("comfyui-workflow-json")?.value || "").trim();
  if (!workflowText) {
    setComfyuiMessage("請先貼上 workflow JSON，或先匯出目前 workflow。", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/comfyui/workflows/import", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || "",
    },
    body: JSON.stringify({
      ...comfyuiWorkflowEditorPayload(),
      workflow_json: workflowText,
      default_params: comfyuiWorkflowEditorDefaults || undefined,
    }),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `workflow 匯入失敗（HTTP ${res.status}）`);
  await loadComfyuiWorkflowPresets();
  setComfyuiMessage(json.msg || "已匯入 workflow preset。", true);
}

async function updateComfyuiWorkflowPreset() {
  if (!comfyuiWorkflowCurrentPresetId) {
    setComfyuiMessage("目前沒有選到可更新的 workflow preset。", false);
    return;
  }
  const workflowText = String($("comfyui-workflow-json")?.value || "").trim();
  if (!workflowText) {
    setComfyuiMessage("workflow JSON 不可為空。", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + `/comfyui/workflows/${encodeURIComponent(comfyuiWorkflowCurrentPresetId)}`, {
    method: "PUT",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || "",
    },
    body: JSON.stringify({
      ...comfyuiWorkflowEditorPayload(),
      workflow_json: workflowText,
      default_params: comfyuiWorkflowEditorDefaults || undefined,
    }),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `workflow 更新失敗（HTTP ${res.status}）`);
  await loadComfyuiWorkflowPresets();
  setComfyuiMessage(json.msg || "已更新 workflow preset。", true);
}

async function runComfyuiWorkflowPreset(presetId) {
  if (!presetId) return;
  const preset = comfyuiWorkflowPresetById(presetId);
  const templateDetail = Number(comfyuiSelectedTemplateDetail?.id || 0) === Number(presetId) ? comfyuiSelectedTemplateDetail : null;
  if (templateDetail && !ensureComfyuiTemplatePromptSharingChoice(templateDetail)) return;
  const userInputs = templateDetail ? collectComfyuiTemplateUserInputs(templateDetail) : {};
  let imageAssignmentState = { assignments: {}, missing: [] };
  try {
    imageAssignmentState = templateDetail ? await ensureComfyuiTemplateImageAssignments(templateDetail) : imageAssignmentState;
  } catch (err) {
    setComfyuiMessage(err.message || "模板媒體匯入失敗，請重新選擇本機檔案。", false);
    return;
  }
  if (imageAssignmentState.missing.length) {
    const labels = imageAssignmentState.missing.map((item) => item.label || `Node ${item.nodeId}`).slice(0, 4).join("、");
    setComfyuiMessage(`這個 workflow 有圖片或影片欄位尚未指定可安全重映射的雲端檔案：${labels}。請先上傳或選擇必要媒體後再執行。`, false);
    return;
  }
  const multiCompareSpec = templateDetail && comfyuiTemplateIsMultiCompareCheckpoints(templateDetail)
    ? comfyuiMultiCompareRunSpec(templateDetail)
    : null;
  if (multiCompareSpec && multiCompareSpec.checkpoints.length < 2) {
    setComfyuiMessage("Multi-Compare 至少需要選擇 2 個大模型。", false);
    return;
  }
  const upscaleBreakpointSpec = templateDetail && comfyuiTemplateIsMultiMethodUpscale(templateDetail)
    ? comfyuiUpscaleBreakpointRunSpec(templateDetail)
    : null;
  const paidApiNodes = comfyuiWorkflowPaidApiNodes(preset);
  let confirmPaidApiNodes = false;
  if (paidApiNodes.length) {
    const labels = paidApiNodes.map((node) => `${node.node_id || "-"}:${node.class_type || node.title || "API node"}`).slice(0, 8);
    confirmPaidApiNodes = window.confirm(
      `這個 workflow 可能會消耗 ComfyUI 官方 credits，不會扣本站積分。\n\n節點：${labels.join(", ")}${paidApiNodes.length > labels.length ? `，另 ${paidApiNodes.length - labels.length} 個` : ""}\n\n餘額與購買請到 ComfyUI UI 的 Settings / Credits 查看。\n\n要繼續執行嗎？`
    );
    if (!confirmPaidApiNodes) return;
  }
  await fetchCsrfToken({ force: true });
  const expectedOutputKinds = comfyuiWorkflowPresetOutputKinds(templateDetail || preset);
  if (typeof updateComfyuiPreviewCardForOutputKinds === "function") updateComfyuiPreviewCardForOutputKinds(expectedOutputKinds);
  const preview = $("comfyui-preview");
  if (preview && typeof comfyuiPreviewPendingText === "function") {
    preview.innerHTML = `<div class="drive-empty">${sanitize(comfyuiPreviewPendingText(expectedOutputKinds))}</div>`;
  }
  const meta = $("comfyui-result-meta");
  if (meta) meta.textContent = "";
  comfyuiCurrentImage = null;
  comfyuiGeneratedImages = [];
  comfyuiGeneratedMedia = [];
  comfyuiSelectedImageIndex = 0;
  comfyuiSavedResult = null;
  updateComfyuiResultButtons(false);
  setComfyuiBusy(true);
  setComfyuiMessage("正在建立 workflow 執行工作...", true);
  const workflowTimeoutSeconds = comfyuiWorkflowPresetForegroundTimeoutSeconds(preset);
  startComfyuiProgress(workflowTimeoutSeconds);
  const controller = new AbortController();
  comfyuiGenerateAbortController = controller;
  const runRequest = (confirmed) => apiFetch(API + `/comfyui/workflows/${encodeURIComponent(presetId)}/run`, {
    method: "POST",
    credentials: "same-origin",
    signal: controller.signal,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || "",
    },
    body: JSON.stringify({
      confirm_paid_api_nodes: !!confirmed,
      user_inputs: userInputs,
      image_field_assignments: imageAssignmentState.assignments,
      multi_compare: multiCompareSpec || undefined,
      upscale_breakpoint: upscaleBreakpointSpec || undefined,
    }),
  });
  const partialRenderState = { signature: "" };
  const partialExpectedCount = Array.isArray(multiCompareSpec?.checkpoints) ? multiCompareSpec.checkpoints.length : 0;
  const partialImageSignature = (images = []) => (Array.isArray(images) ? images : [])
    .map((image) => {
      const ref = image?.image_ref || image;
      return [
        image?.output_node_id || ref?.output_node_id || "",
        ref?.filename || "",
        ref?.subfolder || "",
        ref?.type || "",
      ].join("|");
    })
    .join("\n");
  const renderPartialWorkflowResult = async (partialResult) => {
    if (!multiCompareSpec) return;
    const rawImages = Array.isArray(partialResult?.images) && partialResult.images.length
      ? partialResult.images
      : [partialResult?.image].filter(Boolean);
    const signature = partialImageSignature(rawImages);
    if (!signature || signature === partialRenderState.signature) return;
    partialRenderState.signature = signature;
    const images = await hydrateComfyuiGeneratedImages(rawImages);
    if (!images.length) return;
    const selectedIndex = Math.max(0, Math.min(comfyuiSelectedImageIndex || 0, images.length - 1));
    comfyuiGeneratedImages = images;
    comfyuiGeneratedMedia = [];
    renderComfyuiGeneratedImages(comfyuiGeneratedImages);
    setComfyuiSelectedImage(selectedIndex);
    updateComfyuiResultButtons(true);
    const suffix = partialExpectedCount ? ` / ${partialExpectedCount}` : "";
    setComfyuiMessage(`Multi-Compare 已先顯示 ${images.length}${suffix} 張完成圖片，剩餘分支仍在生成。`, true);
  };
  try {
    let res = await runRequest(confirmPaidApiNodes);
    let json = await res.json().catch(() => ({}));
    if ((!res.ok || !json.ok) && json.stage === "paid_api_confirmation_required") {
      const nodes = Array.isArray(json.paid_api_nodes?.nodes) ? json.paid_api_nodes.nodes : [];
      const labels = nodes.map((node) => `${node.node_id || "-"}:${node.class_type || node.title || "API node"}`).slice(0, 8);
      if (!window.confirm(`這個 workflow 可能會消耗 ComfyUI 官方 credits，不會扣本站積分。\n\n節點：${labels.join(", ") || "API node"}\n\n餘額與購買請到 ComfyUI UI 的 Settings / Credits 查看。\n\n要繼續執行嗎？`)) {
        throw new Error("已取消付費/API node workflow 執行");
      }
      res = await runRequest(true);
      json = await res.json().catch(() => ({}));
    }
    if (!res.ok || !json.ok) throw new Error(json.msg || `workflow 執行失敗（HTTP ${res.status}）`);
    const jobId = json.job?.job_id;
    const result = await pollComfyuiJobUntilDone(jobId, controller, workflowTimeoutSeconds, {
      onPartialResult: renderPartialWorkflowResult,
      onPartialError: (err) => {
        console.warn("Multi-Compare partial preview failed", err);
      },
    });
    const rawImages = Array.isArray(result.images) && result.images.length ? result.images : [result.image].filter(Boolean);
    const images = await hydrateComfyuiGeneratedImages(rawImages);
    const media = await hydrateComfyuiGeneratedMedia(Array.isArray(result.media) ? result.media : [], jobId);
    comfyuiGeneratedImages = images;
    comfyuiGeneratedMedia = media;
    if (images.length) {
      renderComfyuiGeneratedImages(comfyuiGeneratedImages);
      setComfyuiSelectedImage(0);
    } else if (media.length) {
      renderComfyuiGeneratedMedia(comfyuiGeneratedMedia);
    } else {
      renderComfyuiGeneratedImages([]);
    }
    stopComfyuiProgress({ complete: true });
    updateComfyuiResultButtons(!!images.length);
    await loadComfyuiWorkflowPresets();
    if (typeof applyComfyuiSeedAfterGenerate === "function") applyComfyuiSeedAfterGenerate(images[images.length - 1]?.seed);
    setComfyuiMessage(`已執行 workflow preset #${presetId}，輸出 ${images.length} 張圖片、${media.length} 個媒體檔。`, true);
  } catch (err) {
    const timedOut = typeof isComfyuiForegroundTimeoutError === "function" && isComfyuiForegroundTimeoutError(err);
    const message = timedOut && typeof comfyuiForegroundTimeoutMessage === "function"
      ? comfyuiForegroundTimeoutMessage(err)
      : (err.message || "workflow 執行失敗");
    stopComfyuiProgress({
      error: message,
      label: timedOut ? "已停止前台等待" : "產圖失敗"
    });
    setComfyuiMessage(message, false);
  } finally {
    if (comfyuiGenerateAbortController === controller) comfyuiGenerateAbortController = null;
    setComfyuiBusy(false);
  }
}

async function exportComfyuiWorkflowPreset(presetId) {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + `/comfyui/workflows/${encodeURIComponent(presetId)}/export`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || "",
    },
    body: JSON.stringify({}),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `workflow 匯出失敗（HTTP ${res.status}）`);
  downloadComfyuiWorkflowText(json.filename || `comfyui-workflow-layout-${presetId}.json`, json.workflow_preset_text || JSON.stringify(json.workflow_preset_json || json.workflow_json || {}, null, 2));
  setComfyuiMessage(`已匯出 workflow 版面 #${presetId}，內含原始 workflow、本專案 preset 包裝與 UI layout。`, true);
}

async function duplicateComfyuiWorkflowPreset(presetId) {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + `/comfyui/workflows/${encodeURIComponent(presetId)}`, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `workflow 讀取失敗（HTTP ${res.status}）`);
  const preset = json.preset || {};
  const create = await apiFetch(API + "/comfyui/workflow-layouts", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || "",
    },
    body: JSON.stringify({
      title: `${preset.title || `Workflow #${presetId}`} copy`,
      description: preset.description || "",
      visibility: "private",
      purpose: preset.purpose || "custom",
      comfyui_version: preset.comfyui_version || "",
      project_version: preset.project_version || "",
      workflow_schema_version: preset.workflow_schema_version || "1",
      layout_json: preset.layout_json || {},
      workflow_json: preset.workflow_json || {},
      default_params: preset.default_params || {},
      required_custom_nodes: preset.required_custom_nodes || [],
    }),
  });
  const created = await create.json().catch(() => ({}));
  if (!create.ok || !created.ok) throw new Error(created.msg || `workflow 複製失敗（HTTP ${create.status}）`);
  await loadComfyuiWorkflowPresets();
  setComfyuiMessage(created.msg || "已複製為新的私人工作流版面。", true);
}

async function setDefaultComfyuiWorkflowPreset(presetId) {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + `/comfyui/workflows/${encodeURIComponent(presetId)}`, {
    method: "PUT",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || "",
    },
    body: JSON.stringify({ is_default: true }),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `預設版面設定失敗（HTTP ${res.status}）`);
  await loadComfyuiWorkflowPresets();
  setComfyuiMessage("已設為我的預設工作流版面。", true);
}

async function deleteComfyuiWorkflowPreset(presetId) {
  if (!window.confirm("刪除 workflow preset 後，對應的 preset 與最近執行結果會一併移除。要繼續嗎？")) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + `/comfyui/workflows/${encodeURIComponent(presetId)}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || "",
    },
    body: JSON.stringify({}),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `workflow 刪除失敗（HTTP ${res.status}）`);
  if (Number(comfyuiWorkflowCurrentPresetId) === Number(presetId)) resetComfyuiWorkflowEditor({ keepStatus: true });
  await loadComfyuiWorkflowPresets();
  setComfyuiMessage(json.msg || "已刪除 workflow preset。", true);
}

async function publishComfyuiWorkflowPresetOfficial(presetId) {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + `/admin/comfyui/workflows/${encodeURIComponent(presetId)}/publish-official`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken() || "",
    },
    body: JSON.stringify({}),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `官方 preset 發布失敗（HTTP ${res.status}）`);
  await loadComfyuiWorkflowPresets();
  setComfyuiMessage(json.msg || "已發布為官方 preset。", true);
}

async function loadComfyuiWorkflowFile() {
  const file = $("comfyui-workflow-file")?.files?.[0] || null;
  if (!file) return;
  const text = await file.text();
  setComfyuiFieldValue("comfyui-workflow-json", text);
  try {
    const parsed = JSON.parse(text);
    const wrapped = parsed?.workflow_preset_json || parsed;
    if (wrapped && typeof wrapped === "object" && wrapped.workflow_json) {
      setComfyuiFieldValue("comfyui-workflow-json", JSON.stringify(wrapped.workflow_json || {}, null, 2));
      setComfyuiFieldValue("comfyui-workflow-layout-json", JSON.stringify(wrapped.layout_json || {}, null, 2));
      setComfyuiFieldValue("comfyui-workflow-title", wrapped.name || wrapped.title || "");
      setComfyuiFieldValue("comfyui-workflow-description", wrapped.description || "");
      setComfyuiFieldValue("comfyui-workflow-purpose", wrapped.purpose || "custom");
      setComfyuiFieldValue("comfyui-workflow-comfyui-version", wrapped.comfyui_version || "");
      setComfyuiFieldValue("comfyui-workflow-project-version", wrapped.project_version || "");
      setComfyuiFieldValue("comfyui-workflow-schema-version", wrapped.workflow_schema_version || "1");
    }
  } catch (_) {
    // Keep raw text in the workflow editor; backend will return a schema_validation stage.
  }
  comfyuiWorkflowEditorDefaults = null;
  renderComfyuiWorkflowBuilderPreview();
  markComfyuiWorkflowEditorDirty();
  setComfyuiMessage(`已載入 workflow 檔：${file.name}`, true);
}
