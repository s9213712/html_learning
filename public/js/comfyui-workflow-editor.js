'use strict';

(function () {
  const STORAGE_KEY = "hackme_comfyui_workflow_visual_builder";
  const RESULT_KEY = "hackme_comfyui_workflow_editor_result";
  const INPUT_KEY = "hackme_comfyui_workflow_editor_input";
  const ACCOUNT_SCOPE_STORAGE_KEY = "hackme_web.account.active_scope";
  const UNKNOWN_NODE_TYPE = "__UnknownCustomNode__";
  const $ = (id) => document.getElementById(id);

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
    const clearEditor = () => {
      if (!document.body) return;
      document.body.replaceChildren();
      const notice = document.createElement("main");
      notice.className = "workflow-editor-session-ended";
      notice.setAttribute("role", "alert");
      notice.textContent = "帳戶已切換，這個 Workflow 編輯器已關閉。";
      document.body.appendChild(notice);
    };
    clearEditor();
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

  const NODE_DEFS = {
    CheckpointLoaderSimple: {
      label: "Checkpoint Loader",
      inputs: { ckpt_name: { type: "text", label: "Checkpoint" } },
      outputs: ["MODEL", "CLIP", "VAE"],
    },
    CLIPTextEncode: {
      label: "Prompt Encoder",
      inputs: { clip: { type: "link", label: "CLIP" }, text: { type: "textarea", label: "提示詞" } },
      outputs: ["CONDITIONING"],
    },
    LoraLoader: {
      label: "LoRA Loader",
      inputs: {
        model: { type: "link", label: "MODEL" },
        clip: { type: "link", label: "CLIP" },
        lora_name: { type: "text", label: "LoRA" },
        strength_model: { type: "number", label: "Model 強度", step: "0.05" },
        strength_clip: { type: "number", label: "CLIP 強度", step: "0.05" },
      },
      outputs: ["MODEL", "CLIP"],
    },
    VAELoader: {
      label: "VAE Loader",
      inputs: { vae_name: { type: "text", label: "VAE" } },
      outputs: ["VAE"],
    },
    EmptyLatentImage: {
      label: "Empty Latent",
      inputs: {
        width: { type: "number", label: "寬", step: "8" },
        height: { type: "number", label: "高", step: "8" },
        batch_size: { type: "number", label: "張數", step: "1" },
      },
      outputs: ["LATENT"],
    },
    LoadImage: {
      label: "Load Image",
      inputs: { image: { type: "text", label: "圖片檔名 / image ref" } },
      outputs: ["IMAGE", "MASK"],
    },
    LoadImageMask: {
      label: "Load Mask",
      inputs: {
        image: { type: "text", label: "遮罩檔名 / mask ref" },
        channel: { type: "text", label: "Channel" },
      },
      outputs: ["IMAGE", "MASK"],
    },
    VAEEncode: {
      label: "VAE Encode",
      inputs: { pixels: { type: "link", label: "IMAGE" }, vae: { type: "link", label: "VAE" } },
      outputs: ["LATENT"],
    },
    VAEEncodeForInpaint: {
      label: "VAE Encode Inpaint",
      inputs: {
        pixels: { type: "link", label: "IMAGE" },
        vae: { type: "link", label: "VAE" },
        mask: { type: "link", label: "MASK" },
        grow_mask_by: { type: "number", label: "Mask grow", step: "1" },
      },
      outputs: ["LATENT"],
    },
    KSampler: {
      label: "KSampler",
      inputs: {
        model: { type: "link", label: "MODEL" },
        positive: { type: "link", label: "正向 CONDITIONING" },
        negative: { type: "link", label: "負向 CONDITIONING" },
        latent_image: { type: "link", label: "LATENT" },
        seed: { type: "number", label: "Seed", step: "1" },
        steps: { type: "number", label: "Steps", step: "1" },
        cfg: { type: "number", label: "CFG", step: "0.5" },
        sampler_name: { type: "text", label: "Sampler" },
        scheduler: { type: "text", label: "Scheduler" },
        denoise: { type: "number", label: "Denoise", step: "0.05" },
      },
      outputs: ["LATENT"],
    },
    KSamplerAdvanced: {
      label: "KSampler Advanced",
      inputs: {
        model: { type: "link", label: "MODEL" },
        positive: { type: "link", label: "正向 CONDITIONING" },
        negative: { type: "link", label: "負向 CONDITIONING" },
        latent_image: { type: "link", label: "LATENT" },
        add_noise: { type: "text", label: "Add noise" },
        noise_seed: { type: "number", label: "Noise seed", step: "1" },
        steps: { type: "number", label: "Steps", step: "1" },
        cfg: { type: "number", label: "CFG", step: "0.5" },
        sampler_name: { type: "text", label: "Sampler" },
        scheduler: { type: "text", label: "Scheduler" },
        start_at_step: { type: "number", label: "Start step", step: "1" },
        end_at_step: { type: "number", label: "End step", step: "1" },
        return_with_leftover_noise: { type: "text", label: "Return leftover noise" },
      },
      outputs: ["LATENT"],
    },
    VAEDecode: {
      label: "VAE Decode",
      inputs: { samples: { type: "link", label: "LATENT" }, vae: { type: "link", label: "VAE" } },
      outputs: ["IMAGE"],
    },
    SaveImage: {
      label: "Save Image",
      inputs: { images: { type: "link", label: "IMAGE" }, filename_prefix: { type: "text", label: "檔名前綴" } },
      outputs: [],
    },
    ControlNetLoader: {
      label: "ControlNet Loader",
      inputs: { control_net_name: { type: "text", label: "ControlNet" } },
      outputs: ["CONTROL_NET"],
    },
    ControlNetApplyAdvanced: {
      label: "ControlNet Apply Advanced",
      inputs: {
        positive: { type: "link", label: "正向 CONDITIONING" },
        negative: { type: "link", label: "負向 CONDITIONING" },
        control_net: { type: "link", label: "CONTROL_NET" },
        image: { type: "link", label: "IMAGE" },
        strength: { type: "number", label: "強度", step: "0.05" },
        start_percent: { type: "number", label: "起始比例", step: "0.05" },
        end_percent: { type: "number", label: "結束比例", step: "0.05" },
      },
      outputs: ["positive", "negative"],
    },
    ImagePadForOutpaint: {
      label: "Outpaint Pad",
      inputs: {
        image: { type: "link", label: "IMAGE" },
        left: { type: "number", label: "Left", step: "8" },
        top: { type: "number", label: "Top", step: "8" },
        right: { type: "number", label: "Right", step: "8" },
        bottom: { type: "number", label: "Bottom", step: "8" },
        feathering: { type: "number", label: "Feathering", step: "1" },
      },
      outputs: ["IMAGE"],
    },
    UpscaleModelLoader: {
      label: "Upscale Loader",
      inputs: { model_name: { type: "text", label: "Upscale 模型" } },
      outputs: ["UPSCALE_MODEL"],
    },
    ImageUpscaleWithModel: {
      label: "Image Upscale",
      inputs: { upscale_model: { type: "link", label: "UPSCALE_MODEL" }, image: { type: "link", label: "IMAGE" } },
      outputs: ["IMAGE"],
    },
  };

  let selectedId = null;
  let dragState = null;
  let connectState = null;
  let lastImportWarnings = [];
  let nodeCatalog = [];
  let workflow = loadState();
  const AUTO_LAYOUT_X = 70;
  const AUTO_LAYOUT_Y = 80;
  const AUTO_LAYOUT_COLUMN_GAP = 300;
  const AUTO_LAYOUT_ROW_GAP = 165;
  const AUTO_LAYOUT_NODE_WIDTH = 220;
  const AUTO_LAYOUT_NODE_HEIGHT = 132;
  const AUTO_LAYOUT_MIN_WIDTH = 1480;
  const AUTO_LAYOUT_MIN_HEIGHT = 820;

  function isUnknownNode(node) {
    return node?.type === UNKNOWN_NODE_TYPE;
  }

  function nodeDef(nodeOrType) {
    const node = typeof nodeOrType === "object" ? nodeOrType : null;
    const type = node ? node.type : String(nodeOrType || "");
    if (type === UNKNOWN_NODE_TYPE) {
      return {
        label: `Custom: ${node?.originalType || "Unknown"}`,
        inputs: node?.inputSpecs || {},
        outputs: Array.isArray(node?.outputs) && node.outputs.length ? node.outputs : ["OUT0", "OUT1", "OUT2", "OUT3"],
      };
    }
    return NODE_DEFS[type] || null;
  }

  function html(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch]));
  }

  function cssIdent(value) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function uid() {
    return `n_${Math.random().toString(36).slice(2, 9)}`;
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function unknownInputSpecs(rawInputs = {}) {
    const specs = {};
    Object.entries(rawInputs || {}).forEach(([key, value]) => {
      specs[key] = Array.isArray(value)
        ? { type: "link", label: key }
        : { type: typeof value === "number" ? "number" : "text", label: key };
    });
    return specs;
  }

  function unknownOutputs(maxIndex = 3) {
    const count = Math.max(4, Math.min(12, Number(maxIndex || 0) + 1));
    return Array.from({ length: count }, (_item, index) => `OUT${index}`);
  }

  function defaultInputs(type, node = null) {
    if (type === UNKNOWN_NODE_TYPE) return { ...(node?.inputs || {}) };
    const inputs = {};
    Object.entries(NODE_DEFS[type]?.inputs || {}).forEach(([key, spec]) => {
      if (spec.type === "link") return;
      if (key === "text") inputs[key] = "";
      else if (key === "width" || key === "height") inputs[key] = 1024;
      else if (key === "batch_size") inputs[key] = 1;
      else if (key === "steps") inputs[key] = 20;
      else if (key === "cfg") inputs[key] = 7;
      else if (key === "seed") inputs[key] = 0;
      else if (key === "noise_seed") inputs[key] = 0;
      else if (key === "start_at_step") inputs[key] = 0;
      else if (key === "end_at_step") inputs[key] = 20;
      else if (key === "denoise") inputs[key] = 1;
      else if (key === "strength" || key === "strength_model" || key === "strength_clip" || key === "end_percent") inputs[key] = 1;
      else if (key === "start_percent") inputs[key] = 0;
      else if (key === "grow_mask_by") inputs[key] = 6;
      else if (key === "left" || key === "top" || key === "right" || key === "bottom") inputs[key] = 0;
      else if (key === "feathering") inputs[key] = 40;
      else if (key === "add_noise" || key === "return_with_leftover_noise") inputs[key] = "enable";
      else if (key === "channel") inputs[key] = "alpha";
      else if (key === "sampler_name") inputs[key] = "euler";
      else if (key === "scheduler") inputs[key] = "normal";
      else if (key === "filename_prefix") inputs[key] = "hackme_web";
      else inputs[key] = "";
    });
    return inputs;
  }

  function defaultValueForSpec(key, spec = {}) {
    if (spec.type === "link") return undefined;
    if (spec.type === "checkbox") return false;
    if (spec.type === "number") {
      if (key === "width" || key === "height") return 1024;
      if (key === "batch_size") return 1;
      if (key === "steps") return 20;
      if (key === "cfg") return 7;
      if (key === "seed" || key === "noise_seed") return 0;
      if (key === "denoise" || key === "strength" || key === "strength_model" || key === "strength_clip" || key === "end_percent") return 1;
      if (key === "start_percent") return 0;
      return 0;
    }
    if (spec.type === "select") return Array.isArray(spec.options) && spec.options.length ? spec.options[0] : "";
    return "";
  }

  function defaultInputsFromSpecs(specs = {}) {
    const inputs = {};
    Object.entries(specs || {}).forEach(([key, spec]) => {
      const value = defaultValueForSpec(key, spec || {});
      if (value !== undefined) inputs[key] = value;
    });
    return inputs;
  }

  function normalizeSchemaName(raw, fallback = "") {
    const value = String(raw || "").trim().replace(/\s+/g, "_").replace(/[^\w.-]/g, "_").replace(/^_+|_+$/g, "");
    return (value || fallback).slice(0, 80);
  }

  function customSchemaInputTypeLabel(type) {
    return {
      text: "文字",
      textarea: "多行文字",
      number: "數字",
      select: "下拉選單",
      checkbox: "核取方塊",
      link: "連線 input",
    }[type] || "文字";
  }

  function parseOptionList(raw) {
    return String(raw || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 40);
  }

  function normalizeInputSpec(spec = {}) {
    const type = ["text", "textarea", "number", "select", "checkbox", "link"].includes(spec.type) ? spec.type : "text";
    const normalized = {
      type,
      label: String(spec.label || ""),
    };
    if (type === "number" && spec.step) normalized.step = String(spec.step);
    if (type === "select") normalized.options = Array.isArray(spec.options) ? spec.options.map((item) => String(item)).filter(Boolean).slice(0, 40) : [];
    return normalized;
  }

  function emptyState() {
    return { name: "", description: "", project_version: "", comfyui_version: "", workflow_schema_version: "1", nodes: [], edges: [], warnings: [] };
  }

  function normalizeState(raw) {
    const safe = raw && typeof raw === "object" ? raw : {};
    const normalizedNodes = Array.isArray(safe.nodes) ? safe.nodes.filter((node) => node && (NODE_DEFS[node.type] || node.type === UNKNOWN_NODE_TYPE)).map((node) => {
      const type = String(node.type);
      const def = nodeDef(node);
      const normalized = {
        id: String(node.id || uid()),
        type,
        label: String(node.label || def?.label || type),
        x: Number.isFinite(Number(node.x)) ? Number(node.x) : 80,
        y: Number.isFinite(Number(node.y)) ? Number(node.y) : 80,
        inputs: { ...defaultInputs(type, node), ...(node.inputs || {}) },
      };
      if (type === UNKNOWN_NODE_TYPE) {
        normalized.originalType = String(node.originalType || "UnknownCustomNode");
        normalized.inputSpecs = node.inputSpecs && typeof node.inputSpecs === "object"
          ? Object.fromEntries(Object.entries(node.inputSpecs).map(([key, spec]) => [String(key), normalizeInputSpec(spec)]))
          : unknownInputSpecs(node.inputs || {});
        normalized.outputs = Array.isArray(node.outputs) ? node.outputs.map((item) => String(item || "")).filter(Boolean) : unknownOutputs(3);
      }
      return normalized;
    }) : [];
    return {
      name: String(safe.name || ""),
      description: String(safe.description || ""),
      project_version: String(safe.project_version || ""),
      comfyui_version: String(safe.comfyui_version || ""),
      workflow_schema_version: String(safe.workflow_schema_version || "1"),
      nodes: normalizedNodes,
      edges: Array.isArray(safe.edges) ? safe.edges.map((edge) => ({
        id: String(edge.id || uid()),
        from: String(edge.from || ""),
        output: String(edge.output || ""),
        to: String(edge.to || ""),
        input: String(edge.input || ""),
        warning: String(edge.warning || ""),
      })).filter((edge) => edge.from && edge.to && edge.input) : [],
      warnings: Array.isArray(safe.warnings) ? safe.warnings.map((item) => String(item || "")).filter(Boolean).slice(0, 50) : [],
    };
  }

  function loadState() {
    try {
      return normalizeState(JSON.parse(localStorage.getItem(editorScopedStorageKey(STORAGE_KEY)) || "{}"));
    } catch (_) {
      return emptyState();
    }
  }

  function saveState() {
    workflow.name = $("workflowName")?.value || workflow.name || "";
    workflow.description = $("workflowDescription")?.value || workflow.description || "";
    localStorage.setItem(editorScopedStorageKey(STORAGE_KEY), JSON.stringify(workflow));
  }

  function takePendingInput() {
    let payload = null;
    try {
      payload = JSON.parse(localStorage.getItem(editorScopedStorageKey(INPUT_KEY)) || "null");
    } catch (_) {
      payload = null;
    }
    if (!payload || typeof payload !== "object") return false;
    localStorage.removeItem(editorScopedStorageKey(INPUT_KEY));
    const imported = stateFromPackage(payload);
    workflow = normalizeState(imported.state);
    selectedId = workflow.nodes[0]?.id || null;
    lastImportWarnings = imported.warnings;
    render();
    setStatus(imported.warnings.length ? `已載入 workflow，但有 ${imported.warnings.length} 個提醒。` : "已載入既有 workflow，可直接編輯節點與線路。", !imported.warnings.length);
    return true;
  }

  function addNode(type, x, y, label) {
    const def = NODE_DEFS[type];
    if (!def && type !== UNKNOWN_NODE_TYPE) return;
    const isCustom = type === UNKNOWN_NODE_TYPE;
    const customInputs = { prompt: "" };
    const node = {
      id: uid(),
      type,
      label: label || (isCustom ? "Custom / API Node" : def.label),
      x: Number.isFinite(x) ? x : 100 + (workflow.nodes.length % 4) * 260,
      y: Number.isFinite(y) ? y : 100 + Math.floor(workflow.nodes.length / 4) * 170,
      inputs: isCustom ? customInputs : defaultInputs(type),
    };
    if (isCustom) {
      node.originalType = "FluxProUltraImageNode";
      node.inputSpecs = unknownInputSpecs(customInputs);
      node.outputs = unknownOutputs(3);
    }
    workflow.nodes.push(node);
    selectedId = node.id;
    render();
    if (isCustom) setStatus("已新增 Custom / API node；請在右側填入實際 class_type。API Key 不要寫進 inputs，執行時由後端注入。");
  }

  function addCatalogNode(classType) {
    const item = nodeCatalog.find((node) => node.class_type === classType);
    if (!item) {
      setStatus("找不到這個 ComfyUI 節點；請重新載入節點目錄。", false);
      return;
    }
    const inputSpecs = item.inputs && typeof item.inputs === "object"
      ? Object.fromEntries(Object.entries(item.inputs).map(([key, spec]) => [String(key), normalizeInputSpec(spec)]))
      : {};
    const node = {
      id: uid(),
      type: UNKNOWN_NODE_TYPE,
      label: item.display_name || item.class_type,
      originalType: item.class_type,
      x: 100 + (workflow.nodes.length % 4) * 260,
      y: 100 + Math.floor(workflow.nodes.length / 4) * 170,
      inputs: defaultInputsFromSpecs(inputSpecs),
      inputSpecs,
      outputs: Array.isArray(item.outputs) && item.outputs.length ? item.outputs : unknownOutputs(3),
      catalogCategory: item.category || "",
      paidApiRequired: !!item.paid_api_required,
    };
    workflow.nodes.push(node);
    selectedId = node.id;
    render();
    setStatus(item.paid_api_required
      ? `已新增 ${item.class_type}。這可能是付費/API node；API Key 由後端設定注入，不要寫進 workflow。`
      : `已新增 ${item.class_type}。`);
  }

  function nodeById(id) {
    return workflow.nodes.find((node) => node.id === id) || null;
  }

  function outputIndex(node, outputName) {
    const outputs = nodeDef(node)?.outputs || [];
    if (isUnknownNode(node) && /^OUT\d+$/i.test(String(outputName || ""))) {
      return Number(String(outputName).replace(/\D/g, "")) || 0;
    }
    return Math.max(0, outputs.indexOf(outputName));
  }

  function outputNameForIndex(node, index) {
    const outputs = nodeDef(node)?.outputs || [];
    return outputs[Number(index) || 0] || outputs[0] || "OUTPUT";
  }

  function layoutPosition(layout, id, index) {
    const raw = layout?.node_positions?.[id] || layout?.node_positions?.[String(id)];
    if (Array.isArray(raw) && raw.length >= 2 && Number.isFinite(Number(raw[0])) && Number.isFinite(Number(raw[1]))) {
      return [Number(raw[0]), Number(raw[1])];
    }
    return [70 + (index % 4) * 290, 80 + Math.floor(index / 4) * 190];
  }

  function nodeLabelFromLayout(nodeId, node, layout) {
    return String(
      node?._meta?.title ||
      layout?.field_overrides?.[nodeId]?.label ||
      NODE_DEFS[node?.class_type]?.label ||
      node?.class_type ||
      `Node ${nodeId}`
    );
  }

  function stateFromPackage(payload) {
    const source = payload && typeof payload === "object" ? payload : {};
    const prompt = source.workflow_json || source.prompt || source.workflow || source;
    const layout = source.layout_json || source.ui_layout_json || {};
    const warnings = [];
    if (!prompt || typeof prompt !== "object" || Array.isArray(prompt)) {
      return { state: emptyState(), warnings: ["Workflow JSON 必須是物件格式。"] };
    }
    const ids = Object.keys(prompt);
    const layoutOrder = Array.isArray(layout.node_order) ? layout.node_order.map((item) => String(item)) : [];
    const orderedIds = layoutOrder.filter((id) => prompt[id]).concat(ids.filter((id) => !layoutOrder.includes(id)));
    const nodes = [];
    const idSet = new Set();
    orderedIds.forEach((id, index) => {
      const raw = prompt[id] || {};
      const rawType = String(raw.class_type || "");
      const known = !!NODE_DEFS[rawType];
      const type = known ? rawType : UNKNOWN_NODE_TYPE;
      let maxOutputIndex = 3;
      const [x, y] = layoutPosition(layout, id, index);
      const inputs = known ? defaultInputs(type) : {};
      const inputSpecs = known ? null : unknownInputSpecs(raw.inputs || {});
      Object.entries(raw.inputs || {}).forEach(([key, value]) => {
        if (Array.isArray(value)) {
          if (!known) inputSpecs[key] = { type: "link", label: key };
          return;
        }
        if (Object.prototype.hasOwnProperty.call(inputs, key)) inputs[key] = value;
        else if (!known) inputs[key] = value;
      });
      Object.values(raw.inputs || {}).forEach((value) => {
        if (Array.isArray(value) && Number.isFinite(Number(value[1]))) maxOutputIndex = Math.max(maxOutputIndex, Number(value[1]));
      });
      const parsedNode = {
        id: String(id),
        type,
        label: known ? nodeLabelFromLayout(id, raw, layout) : nodeLabelFromLayout(id, { ...raw, class_type: rawType || "UnknownCustomNode" }, layout),
        x,
        y,
        inputs,
      };
      if (!known) {
        parsedNode.originalType = rawType || "UnknownCustomNode";
        parsedNode.inputSpecs = inputSpecs;
        parsedNode.outputs = unknownOutputs(maxOutputIndex);
        warnings.push(`未知/custom node ${id}: ${parsedNode.originalType}，已保留為 placeholder。`);
      }
      nodes.push(parsedNode);
      idSet.add(String(id));
    });
    const edges = [];
    orderedIds.forEach((id) => {
      const raw = prompt[id] || {};
      const target = nodes.find((node) => node.id === String(id));
      if (!target) return;
      Object.entries(raw.inputs || {}).forEach(([key, value]) => {
        if (!Array.isArray(value) || value.length < 1) return;
        const sourceId = String(value[0]);
        const source = nodes.find((node) => node.id === sourceId);
        if (!source || !idSet.has(sourceId)) {
          warnings.push(`連線 ${sourceId} -> ${id}.${key} 找不到來源節點，已略過。`);
          return;
        }
        const inputSpec = nodeDef(target)?.inputs?.[key];
        if (!inputSpec || inputSpec.type !== "link") {
          warnings.push(`連線 ${sourceId} -> ${id}.${key} 指向非連線欄位，已略過。`);
          return;
        }
        const output = outputNameForIndex(source, value[1]);
        edges.push({ id: uid(), from: sourceId, output, to: String(id), input: key, warning: connectionWarningForNodes(source, output, target, key) });
      });
    });
    const state = {
      name: String(source.name || source.title || "匯入的 ComfyUI 工作流"),
      description: String(source.description || ""),
      project_version: String(source.project_version || ""),
      comfyui_version: String(source.comfyui_version || ""),
      workflow_schema_version: String(source.workflow_schema_version || layout.workflow_schema_version || "1"),
      nodes,
      edges,
      warnings,
    };
    return { state, warnings };
  }

  function exportPackage() {
    const idMap = Object.fromEntries(workflow.nodes.map((node, index) => [node.id, String(index + 1)]));
    const layout = {
      layout_schema_version: "1",
      visual_builder_version: "1",
      node_order: workflow.nodes.map((node) => idMap[node.id]),
      node_positions: Object.fromEntries(workflow.nodes.map((node) => [idMap[node.id], [Math.round(node.x), Math.round(node.y)]])),
      field_overrides: Object.fromEntries(workflow.nodes.map((node) => [idMap[node.id], { label: node.label }])),
      edges: workflow.edges
        .filter((edge) => idMap[edge.from] && idMap[edge.to])
        .map((edge) => ({ from: idMap[edge.from], output: edge.output, to: idMap[edge.to], input: edge.input })),
    };
    const prompt = {};
    workflow.nodes.forEach((node) => {
      const promptInputs = clone(node.inputs || {});
      workflow.edges.filter((edge) => edge.to === node.id).forEach((edge) => {
        const source = nodeById(edge.from);
        if (source) promptInputs[edge.input] = [idMap[source.id], outputIndex(source, edge.output)];
      });
      prompt[idMap[node.id]] = {
        class_type: isUnknownNode(node) ? (node.originalType || "UnknownCustomNode") : node.type,
        inputs: promptInputs,
        _meta: { title: node.label || NODE_DEFS[node.type].label },
      };
    });
    return {
      name: workflow.name || "ComfyUI 視覺工作流",
      description: workflow.description || "",
      purpose: inferPurpose(),
      project_version: workflow.project_version || "",
      comfyui_version: workflow.comfyui_version || "",
      workflow_schema_version: workflow.workflow_schema_version || "1",
      workflow_json: prompt,
      layout_json: layout,
      required_models: collectRequiredModels(),
      required_custom_nodes: collectRequiredCustomNodes(),
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
  }

  function newWorkflowId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return `hackme-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  function widgetInputEntries(node) {
    return Object.entries(nodeDef(node)?.inputs || {}).filter(([, spec]) => spec.type !== "link");
  }

  function nodeInputEntries(node) {
    return Object.entries(nodeDef(node)?.inputs || {});
  }

  function liteGraphInputType(spec = {}, key = "") {
    if (spec.type === "link") return normalizedPortKind(spec.label || key) || "UNKNOWN";
    if (spec.type === "number") {
      const lowered = String(key || "").toLowerCase();
      return lowered.includes("cfg") || lowered.includes("denoise") || lowered.includes("strength") || lowered.includes("percent")
        ? "FLOAT"
        : "INT";
    }
    if (spec.type === "checkbox") return "BOOLEAN";
    if (spec.type === "select") return "COMBO";
    return "STRING";
  }

  function liteGraphNodeSize(node) {
    const inputCount = nodeInputEntries(node).length;
    const outputCount = nodeDef(node)?.outputs?.length || 0;
    const widgetCount = widgetInputEntries(node).length;
    const height = Math.max(96, 72 + inputCount * 20 + outputCount * 16 + widgetCount * 10);
    const width = isUnknownNode(node) ? 360 : node.type === "SaveImage" ? 420 : node.type === "CLIPTextEncode" ? 425 : 315;
    return [width, Math.min(620, height)];
  }

  function uiWidgetValues(node) {
    const inputs = node.inputs || {};
    if (node.type === "KSampler") {
      return [
        inputs.seed ?? 0,
        "randomize",
        inputs.steps ?? 20,
        inputs.cfg ?? 7,
        inputs.sampler_name || "euler",
        inputs.scheduler || "normal",
        inputs.denoise ?? 1,
      ];
    }
    if (node.type === "KSamplerAdvanced") {
      return [
        inputs.add_noise || "enable",
        inputs.noise_seed ?? 0,
        "randomize",
        inputs.steps ?? 20,
        inputs.cfg ?? 7,
        inputs.sampler_name || "euler",
        inputs.scheduler || "normal",
        inputs.start_at_step ?? 0,
        inputs.end_at_step ?? 20,
        inputs.return_with_leftover_noise || "enable",
      ];
    }
    return widgetInputEntries(node).map(([key, spec]) => inputs[key] ?? defaultValueForSpec(key, spec || {}));
  }

  function exportComfyUiWorkflowGraph() {
    const idMap = Object.fromEntries(workflow.nodes.map((node, index) => [node.id, index + 1]));
    const linkIds = new Map();
    workflow.edges
      .filter((edge) => idMap[edge.from] && idMap[edge.to])
      .forEach((edge, index) => linkIds.set(edge.id, index + 1));
    const links = workflow.edges
      .filter((edge) => idMap[edge.from] && idMap[edge.to])
      .map((edge) => {
        const source = nodeById(edge.from);
        const target = nodeById(edge.to);
        const inputSlot = Math.max(0, nodeInputEntries(target).findIndex(([key]) => key === edge.input));
        return [
          linkIds.get(edge.id),
          idMap[edge.from],
          outputIndex(source, edge.output),
          idMap[edge.to],
          inputSlot,
          normalizedPortKind(edge.output) || edge.output || "*",
        ];
      });
    const outputLinksByNodeSlot = {};
    const inputLinkByNodeInput = {};
    workflow.edges.forEach((edge) => {
      if (!idMap[edge.from] || !idMap[edge.to]) return;
      const source = nodeById(edge.from);
      const slot = outputIndex(source, edge.output);
      const sourceKey = `${idMap[edge.from]}:${slot}`;
      const targetKey = `${idMap[edge.to]}:${edge.input}`;
      outputLinksByNodeSlot[sourceKey] = (outputLinksByNodeSlot[sourceKey] || []).concat(linkIds.get(edge.id));
      inputLinkByNodeInput[targetKey] = linkIds.get(edge.id);
    });
    const nodes = workflow.nodes.map((node, index) => {
      const nodeId = idMap[node.id];
      const classType = isUnknownNode(node) ? (node.originalType || "UnknownCustomNode") : node.type;
      const inputs = nodeInputEntries(node).map(([key, spec]) => {
        const link = inputLinkByNodeInput[`${nodeId}:${key}`] || null;
        const item = {
          name: key,
          type: liteGraphInputType(spec, key),
          link,
        };
        if (spec.type !== "link") item.widget = { name: key };
        return item;
      });
      const outputs = (nodeDef(node)?.outputs || []).map((name, slot) => ({
        name,
        type: normalizedPortKind(name) || name || "UNKNOWN",
        slot_index: slot,
        links: outputLinksByNodeSlot[`${nodeId}:${slot}`] || [],
      }));
      return {
        id: nodeId,
        type: classType,
        pos: [Math.round(node.x), Math.round(node.y)],
        size: liteGraphNodeSize(node),
        flags: {},
        order: index,
        mode: 0,
        inputs,
        outputs,
        properties: {
          "Node name for S&R": classType,
        },
        widgets_values: uiWidgetValues(node),
        title: node.label || classType,
      };
    });
    return {
      id: newWorkflowId(),
      revision: 0,
      last_node_id: workflow.nodes.length,
      last_link_id: links.length,
      nodes,
      links,
      groups: [],
      config: {},
      extra: {
        ds: {
          scale: 1,
          offset: [0, 0],
        },
      },
      version: 0.4,
    };
  }

  function exportComfyUiApiPrompt() {
    return exportPackage().workflow_json;
  }

  function normalizedPortKind(name) {
    const raw = String(name || "").toUpperCase();
    if (raw === "POSITIVE" || raw === "NEGATIVE") return "CONDITIONING";
    if (raw.includes("CONDITIONING")) return "CONDITIONING";
    if (raw.includes("CONTROL_NET")) return "CONTROL_NET";
    if (raw.includes("UPSCALE_MODEL")) return "UPSCALE_MODEL";
    if (raw.includes("MODEL")) return "MODEL";
    if (raw.includes("CLIP")) return "CLIP";
    if (raw.includes("VAE")) return "VAE";
    if (raw.includes("LATENT")) return "LATENT";
    if (raw.includes("MASK")) return "MASK";
    if (raw.includes("IMAGE") || raw === "PIXELS") return "IMAGE";
    return raw;
  }

  function connectionWarning(from, output, to, input) {
    return connectionWarningForNodes(nodeById(from), output, nodeById(to), input);
  }

  function connectionWarningForNodes(source, output, target, input) {
    const inputLabel = nodeDef(target)?.inputs?.[input]?.label || input;
    const outKind = normalizedPortKind(output);
    const inKind = normalizedPortKind(inputLabel);
    if (!outKind || !inKind || outKind === inKind) return "";
    return `${output} 連到 ${input} 型別可能不相容`;
  }

  function inferPurpose() {
    if (workflow.nodes.some((node) => node.type === "ControlNetApplyAdvanced")) return "controlnet";
    if (workflow.nodes.some((node) => node.type === "ImageUpscaleWithModel")) return "upscale";
    if (workflow.nodes.some((node) => node.type === "ImagePadForOutpaint")) return "outpaint";
    if (workflow.nodes.some((node) => node.type === "VAEEncodeForInpaint" || node.type === "LoadImageMask")) return "inpaint";
    if (workflow.nodes.some((node) => node.type === "LoadImage")) return "img2img";
    return "txt2img";
  }

  function collectRequiredModels() {
    const models = [];
    workflow.nodes.forEach((node) => {
      if (node.type === "CheckpointLoaderSimple" && node.inputs.ckpt_name) models.push({ kind: "checkpoint", name: node.inputs.ckpt_name });
      if (node.type === "LoraLoader" && node.inputs.lora_name) models.push({ kind: "lora", name: node.inputs.lora_name });
      if (node.type === "ControlNetLoader" && node.inputs.control_net_name) models.push({ kind: "controlnet", name: node.inputs.control_net_name });
      if (node.type === "UpscaleModelLoader" && node.inputs.model_name) models.push({ kind: "upscale", name: node.inputs.model_name });
      if (node.type === "VAELoader" && node.inputs.vae_name) models.push({ kind: "vae", name: node.inputs.vae_name });
    });
    return models;
  }

  function collectRequiredCustomNodes() {
    const seen = new Set();
    const items = [];
    workflow.nodes.filter(isUnknownNode).forEach((node) => {
      const classType = String(node.originalType || "").trim();
      if (!classType || classType === "UnknownCustomNode" || seen.has(classType)) return;
      seen.add(classType);
      items.push({
        class_type: classType,
        display_name: node.label || classType,
        category: node.catalogCategory || "",
        paid_api_required: !!node.paidApiRequired || nodeLooksLikePaidApi(node),
      });
    });
    return items;
  }

  function setStatus(message, good = true) {
    const el = $("status");
    if (!el) return;
    el.textContent = message || "";
    el.style.color = good ? "var(--muted)" : "var(--red)";
  }

  function render() {
    saveState();
    if ($("workflowName")) $("workflowName").value = workflow.name || "";
    if ($("workflowDescription")) $("workflowDescription").value = workflow.description || "";
    fitCanvasToWorkflow();
    renderNodes();
    renderEdges();
    renderInspector();
    renderConnectionPanel();
    renderValidationPanel();
    renderJson();
    const badges = $("summaryBadges");
    if (badges) {
      const warnings = workflowWarnings();
      badges.innerHTML = `
        <span class="badge">${workflow.nodes.length} nodes</span>
        <span class="badge">${workflow.edges.length} edges</span>
        <span class="badge">${html(inferPurpose())}</span>
        ${workflow.project_version ? `<span class="badge">project ${html(workflow.project_version)}</span>` : ""}
        ${workflow.comfyui_version ? `<span class="badge">ComfyUI ${html(workflow.comfyui_version)}</span>` : ""}
        ${warnings.length ? `<span class="badge warn">${warnings.length} warnings</span>` : '<span class="badge ok">ready</span>'}
      `;
    }
  }

  function workflowWarnings() {
    return []
      .concat(workflow.warnings || [])
      .concat(workflow.edges.map((edge) => edge.warning || "").filter(Boolean))
      .concat(workflowValidationIssues().filter((item) => item.level !== "info").map((item) => item.message))
      .filter(Boolean);
  }

  const PAID_API_NODE_MARKERS = [
    "api",
    "api_key",
    "apikey",
    "comfyapi",
    "fluxpro",
    "openai",
    "stability",
    "runway",
    "kling",
    "luma",
    "minimax",
    "ideogram",
    "recraft",
    "pixverse",
    "veo",
  ];

  function nodeLooksLikePaidApi(node) {
    if (node?.paidApiRequired) return true;
    const type = String(isUnknownNode(node) ? node.originalType : node.type || "").toLowerCase().replace(/[\s_-]+/g, "");
    const label = String(node.label || "").toLowerCase();
    const keys = Object.keys(node.inputs || {}).join(" ").toLowerCase().replace(/[\s_-]+/g, "");
    return PAID_API_NODE_MARKERS.some((marker) => {
      const normalized = String(marker).toLowerCase().replace(/[\s_-]+/g, "");
      return type.includes(normalized) || label.includes(marker) || keys.includes(normalized);
    });
  }

  function workflowValidationIssues() {
    const issues = [];
    const classes = new Set(workflow.nodes.map((node) => isUnknownNode(node) ? node.originalType : node.type));
    const models = collectRequiredModels();
    models.forEach((item) => {
      if (!String(item.name || "").trim()) {
        issues.push({ level: "warn", message: `${item.kind} 模型尚未指定名稱。` });
      }
    });
    workflow.nodes.filter(isUnknownNode).forEach((node) => {
      issues.push({ level: "warn", message: `Custom node 需要 ComfyUI object_info 驗證：${node.originalType || node.label}` });
    });
    workflow.nodes.filter(nodeLooksLikePaidApi).forEach((node) => {
      issues.push({ level: "warn", message: `可能是付費/API node，執行前需 root 啟用並確認：${isUnknownNode(node) ? node.originalType : node.type}` });
    });
    if (!classes.has("SaveImage")) {
      issues.push({ level: "warn", message: "workflow 沒有 SaveImage 節點，執行後可能沒有可保存的輸出。" });
    }
    if (!workflow.edges.length && workflow.nodes.length > 1) {
      issues.push({ level: "warn", message: "目前沒有線路，節點可能不會形成可執行 workflow。" });
    }
    workflow.nodes.forEach((node) => {
      const def = nodeDef(node);
      Object.entries(def?.inputs || {}).forEach(([key, spec]) => {
        if (spec.type === "link") {
          const hasEdge = workflow.edges.some((edge) => edge.to === node.id && edge.input === key);
          if (!hasEdge) issues.push({ level: "info", message: `${node.label || node.id}.${key} 尚未接線。` });
        }
      });
    });
    if (!workflow.project_version) issues.push({ level: "info", message: "未記錄本專案版本；保存時主頁可補上。" });
    if (!workflow.comfyui_version) issues.push({ level: "info", message: "未記錄 ComfyUI 版本；跨機器匯入時建議補上。" });
    return issues;
  }

  function renderValidationPanel() {
    const panel = $("validationPanel");
    const badge = $("validationBadge");
    if (!panel) return;
    const issues = workflowValidationIssues();
    const blocking = issues.filter((item) => item.level === "warn");
    if (badge) {
      badge.textContent = blocking.length ? `${blocking.length} warn` : "ready";
      badge.className = `badge ${blocking.length ? "warn" : "ok"}`;
    }
    const models = collectRequiredModels();
    const customNodes = workflow.nodes.filter(isUnknownNode);
    const paidApiNodes = workflow.nodes.filter(nodeLooksLikePaidApi);
    panel.innerHTML = `
      <div class="dependency-list">
        <div class="dependency-row">
          <strong>模型依賴</strong>
          <span>${models.length ? html(models.map((item) => `${item.kind}:${item.name || "(未指定)"}`).join("、")) : "目前沒有已填名稱的模型依賴"}</span>
        </div>
        <div class="dependency-row">
          <strong>Custom nodes</strong>
          <span>${customNodes.length ? html(customNodes.map((node) => node.originalType || node.label).join("、")) : "未偵測到 placeholder"}</span>
        </div>
        <div class="dependency-row">
          <strong>付費/API nodes</strong>
          <span>${paidApiNodes.length ? html(paidApiNodes.map((node) => isUnknownNode(node) ? node.originalType : node.type).join("、")) : "未偵測到"}</span>
        </div>
        <div class="dependency-row">
          <strong>版本</strong>
          <span>project ${html(workflow.project_version || "-")} · ComfyUI ${html(workflow.comfyui_version || "-")} · schema ${html(workflow.workflow_schema_version || "1")}</span>
        </div>
      </div>
      ${issues.length ? `
        <div class="validation-issue-list">
          ${issues.slice(0, 12).map((item) => `<div class="${item.level === "warn" ? "warn" : "info"}">${html(item.message)}</div>`).join("")}
          ${issues.length > 12 ? `<div class="info">另 ${html(String(issues.length - 12))} 個提示</div>` : ""}
        </div>
      ` : '<div class="empty">目前沒有發現依賴或結構提醒。</div>'}
    `;
  }

  function renderNodes() {
    const layer = $("nodeLayer");
    if (!layer) return;
    layer.innerHTML = workflow.nodes.map((node) => {
      const def = nodeDef(node);
      const inputRows = Object.entries(def.inputs || {});
      const linkInputs = inputRows.filter(([, spec]) => spec.type === "link");
      const valueInputs = inputRows.filter(([, spec]) => spec.type !== "link");
      return `
        <div class="wf-node ${node.id === selectedId ? "selected" : ""} ${isUnknownNode(node) ? "unknown" : ""}" data-node-id="${html(node.id)}" data-drag-node="${html(node.id)}" style="left:${Math.round(node.x)}px;top:${Math.round(node.y)}px;">
          <div class="wf-node-head" data-drag-node="${html(node.id)}">
            <strong>${html(node.label || def.label)}</strong>
            <span class="wf-node-kind">${html(isUnknownNode(node) ? node.originalType : node.type)}</span>
          </div>
          <div class="wf-node-body">
            <div class="port-row input-row">
              ${linkInputs.map(([key, spec]) => `<span class="port input" role="button" tabindex="0" title="input: ${html(spec.label || key)}" data-port-node="${html(node.id)}" data-port-kind="input" data-port-name="${html(key)}">${html(key)}</span>`).join("") || '<span class="port muted">no link input</span>'}
            </div>
            ${valueInputs.length ? `<div class="port-row value-row">${valueInputs.slice(0, 4).map(([key]) => `<span class="port value">${html(key)}</span>`).join("")}</div>` : ""}
            <div class="port-row output-row">
              ${(def.outputs || []).map((key) => `<span class="port output" role="button" tabindex="0" title="output: ${html(key)}" data-port-node="${html(node.id)}" data-port-kind="output" data-port-name="${html(key)}">${html(key)}</span>`).join("") || '<span class="port muted">final</span>'}
            </div>
          </div>
          <div class="node-actions">
            <button type="button" data-select-node="${html(node.id)}">設定</button>
            <button class="danger" type="button" data-delete-node="${html(node.id)}">刪除</button>
          </div>
        </div>
      `;
    }).join("");
    layer.querySelectorAll("[data-select-node]").forEach((button) => {
      button.addEventListener("click", () => {
        selectedId = button.getAttribute("data-select-node");
        render();
      });
    });
    layer.querySelectorAll("[data-delete-node]").forEach((button) => {
      button.addEventListener("click", () => deleteNode(button.getAttribute("data-delete-node")));
    });
    layer.querySelectorAll("[data-drag-node]").forEach((head) => {
      head.addEventListener("pointerdown", startDrag);
      head.addEventListener("mousedown", startDrag);
    });
    layer.querySelectorAll('.port.output[data-port-node]').forEach((port) => {
      port.addEventListener("pointerdown", startConnection);
      port.addEventListener("mousedown", startConnection);
      port.addEventListener("keydown", startConnectionFromKeyboard);
    });
    layer.querySelectorAll('.port.input[data-port-node]').forEach((port) => {
      port.addEventListener("pointerup", completeConnection);
      port.addEventListener("mouseup", completeConnection);
      port.addEventListener("keydown", completeConnectionFromKeyboard);
    });
  }

  function deleteNode(id) {
    workflow.nodes = workflow.nodes.filter((node) => node.id !== id);
    workflow.edges = workflow.edges.filter((edge) => edge.from !== id && edge.to !== id);
    if (selectedId === id) selectedId = workflow.nodes[0]?.id || null;
    render();
  }

  function startDrag(event) {
    if (dragState) return;
    if (event.target && event.target.closest && event.target.closest("button, input, select, textarea, a, .port")) return;
    const id = event.currentTarget.getAttribute("data-drag-node");
    const node = nodeById(id);
    if (!node) return;
    selectedId = id;
    document.querySelectorAll(".wf-node.selected").forEach((item) => item.classList.remove("selected"));
    const nodeEl = document.querySelector(`[data-node-id="${cssIdent(id)}"]`);
    nodeEl?.classList.add("selected");
    nodeEl?.classList.add("dragging");
    renderInspector();
    renderConnectionPanel();
    renderJson();
    dragState = { id, startX: event.clientX, startY: event.clientY, nodeX: node.x, nodeY: node.y };
    event.preventDefault();
    try {
      if (event.pointerId !== undefined && event.currentTarget.setPointerCapture) {
        event.currentTarget.setPointerCapture(event.pointerId);
      }
    } catch (_) {
      // Mouse fallback below keeps dragging available when pointer capture is unavailable.
    }
    window.addEventListener("pointermove", onDrag);
    window.addEventListener("mousemove", onDrag);
    window.addEventListener("pointerup", endDrag, { once: true });
    window.addEventListener("mouseup", endDrag, { once: true });
    window.addEventListener("blur", endDrag, { once: true });
  }

  function onDrag(event) {
    if (!dragState) return;
    const node = nodeById(dragState.id);
    if (!node) return;
    node.x = Math.max(0, dragState.nodeX + event.clientX - dragState.startX);
    node.y = Math.max(0, dragState.nodeY + event.clientY - dragState.startY);
    const el = document.querySelector(`[data-node-id="${cssIdent(node.id)}"]`);
    if (el) {
      el.style.left = `${Math.round(node.x)}px`;
      el.style.top = `${Math.round(node.y)}px`;
    }
    renderEdges();
  }

  function endDrag() {
    const id = dragState?.id;
    dragState = null;
    window.removeEventListener("pointermove", onDrag);
    window.removeEventListener("mousemove", onDrag);
    window.removeEventListener("blur", endDrag);
    if (id) document.querySelector(`[data-node-id="${cssIdent(id)}"]`)?.classList.remove("dragging");
    render();
  }

  function workflowFlowLayers() {
    const ids = workflow.nodes.map((node) => node.id);
    const idSet = new Set(ids);
    const originalIndex = new Map(ids.map((id, index) => [id, index]));
    const incomingCount = new Map(ids.map((id) => [id, 0]));
    const outgoing = new Map(ids.map((id) => [id, []]));
    workflow.edges.forEach((edge) => {
      if (!idSet.has(edge.from) || !idSet.has(edge.to) || edge.from === edge.to) return;
      outgoing.get(edge.from).push(edge);
      incomingCount.set(edge.to, (incomingCount.get(edge.to) || 0) + 1);
    });
    outgoing.forEach((edges) => {
      edges.sort((a, b) =>
        (originalIndex.get(a.to) ?? 0) - (originalIndex.get(b.to) ?? 0) ||
        String(a.output || "").localeCompare(String(b.output || "")) ||
        String(a.input || "").localeCompare(String(b.input || "")));
    });
    const remainingIncoming = new Map(incomingCount);
    const depth = new Map(ids.map((id) => [id, 0]));
    const processed = new Set();
    const queue = ids.filter((id) => (incomingCount.get(id) || 0) === 0);
    const sortByFlow = (a, b) => (depth.get(a) || 0) - (depth.get(b) || 0) || (originalIndex.get(a) ?? 0) - (originalIndex.get(b) ?? 0);
    while (queue.length) {
      queue.sort(sortByFlow);
      const id = queue.shift();
      if (!id || processed.has(id)) continue;
      processed.add(id);
      (outgoing.get(id) || []).forEach((edge) => {
        const next = edge.to;
        depth.set(next, Math.max(depth.get(next) || 0, (depth.get(id) || 0) + 1));
        remainingIncoming.set(next, Math.max(0, (remainingIncoming.get(next) || 0) - 1));
        if ((remainingIncoming.get(next) || 0) === 0) queue.push(next);
      });
    }
    ids.filter((id) => !processed.has(id)).sort((a, b) => (originalIndex.get(a) ?? 0) - (originalIndex.get(b) ?? 0)).forEach((id) => {
      const upstreamDepth = workflow.edges
        .filter((edge) => edge.to === id && idSet.has(edge.from) && edge.from !== id)
        .map((edge) => (depth.get(edge.from) || 0) + 1);
      if (upstreamDepth.length) depth.set(id, Math.max(depth.get(id) || 0, ...upstreamDepth));
      processed.add(id);
    });
    const layers = new Map();
    ids.forEach((id) => {
      const layer = Math.max(0, depth.get(id) || 0);
      if (!layers.has(layer)) layers.set(layer, []);
      layers.get(layer).push(id);
    });
    return Array.from(layers.entries())
      .sort(([a], [b]) => a - b)
      .map(([, layerIds]) => layerIds.sort((a, b) => (originalIndex.get(a) ?? 0) - (originalIndex.get(b) ?? 0)));
  }

  function fitCanvasToWorkflow() {
    const canvas = $("canvas");
    if (!canvas) return;
    if (!workflow.nodes.length) {
      canvas.style.removeProperty("width");
      canvas.style.removeProperty("min-height");
      return;
    }
    const maxX = Math.max(...workflow.nodes.map((node) => Number(node.x) || 0));
    const maxY = Math.max(...workflow.nodes.map((node) => Number(node.y) || 0));
    const desiredWidth = Math.ceil(maxX + AUTO_LAYOUT_NODE_WIDTH + 110);
    const desiredHeight = Math.ceil(maxY + AUTO_LAYOUT_NODE_HEIGHT + 110);
    if (desiredWidth > AUTO_LAYOUT_MIN_WIDTH) canvas.style.width = `${desiredWidth}px`;
    else canvas.style.removeProperty("width");
    if (desiredHeight > AUTO_LAYOUT_MIN_HEIGHT) canvas.style.minHeight = `${desiredHeight}px`;
    else canvas.style.removeProperty("min-height");
  }

  function autoLayoutNodes() {
    if (!workflow.nodes.length) return;
    const layers = workflowFlowLayers();
    layers.forEach((layer, layerIndex) => {
      layer.forEach((id, rowIndex) => {
        const node = nodeById(id);
        if (!node) return;
        node.x = AUTO_LAYOUT_X + layerIndex * AUTO_LAYOUT_COLUMN_GAP;
        node.y = AUTO_LAYOUT_Y + rowIndex * AUTO_LAYOUT_ROW_GAP;
      });
    });
    const nodeMap = new Map(workflow.nodes.map((node) => [node.id, node]));
    workflow.nodes = layers.flat().map((id) => nodeMap.get(id)).filter(Boolean);
    render();
    setStatus(`已依 workflow 流程順序整理 ${workflow.nodes.length} 個節點：來源在左、輸出在右。`);
  }

  function canvasPoint(event) {
    const canvas = $("canvas");
    const rect = canvas?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function portPoint(nodeId, kind, name) {
    const canvas = $("canvas");
    const selector = `.port[data-port-node="${cssIdent(nodeId)}"][data-port-kind="${cssIdent(kind)}"][data-port-name="${cssIdent(name)}"]`;
    const port = document.querySelector(selector);
    const canvasRect = canvas?.getBoundingClientRect();
    if (port && canvasRect) {
      const rect = port.getBoundingClientRect();
      return {
        x: rect.left - canvasRect.left + (kind === "output" ? rect.width : 0),
        y: rect.top - canvasRect.top + rect.height / 2,
      };
    }
    const node = nodeById(nodeId);
    const nodeEl = node ? document.querySelector(`[data-node-id="${cssIdent(node.id)}"]`) : null;
    const width = nodeEl?.offsetWidth || 220;
    const height = nodeEl?.offsetHeight || 116;
    return {
      x: (node?.x || 0) + (kind === "output" ? width : 0),
      y: (node?.y || 0) + height / 2,
    };
  }

  function edgePath(start, end) {
    const mid = Math.max(70, Math.abs(end.x - start.x) / 2);
    return `M ${start.x} ${start.y} C ${start.x + mid} ${start.y}, ${end.x - mid} ${end.y}, ${end.x} ${end.y}`;
  }

  function renderEdges() {
    const svg = $("edgeLayer");
    if (!svg) return;
    const parts = [];
    workflow.edges.forEach((edge) => {
      const from = nodeById(edge.from);
      const to = nodeById(edge.to);
      if (!from || !to) return;
      const start = portPoint(edge.from, "output", edge.output);
      const end = portPoint(edge.to, "input", edge.input);
      const path = edgePath(start, end);
      parts.push(`<path class="edge-path ${edge.warning ? "warn" : ""}" data-edge-id="${html(edge.id)}" d="${path}"></path>`);
      parts.push(`<circle class="edge-dot output" cx="${start.x}" cy="${start.y}" r="4"></circle>`);
      parts.push(`<circle class="edge-dot input" cx="${end.x}" cy="${end.y}" r="4"></circle>`);
      parts.push(`<text class="edge-label ${edge.warning ? "warn" : ""}" x="${(start.x + end.x) / 2}" y="${(start.y + end.y) / 2 - 7}">${html(edge.output)} → ${html(edge.input)}</text>`);
    });
    if (connectState) {
      const start = portPoint(connectState.from, "output", connectState.output);
      const end = connectState.current || start;
      parts.push(`<path class="edge-path temp" d="${edgePath(start, end)}"></path>`);
    }
    svg.innerHTML = parts.join("");
  }

  function markConnectionPorts() {
    document.querySelectorAll(".port.connecting, .port.compatible").forEach((port) => {
      port.classList.remove("connecting", "compatible");
    });
    if (!connectState) return;
    document.querySelector(`.port.output[data-port-node="${cssIdent(connectState.from)}"][data-port-name="${cssIdent(connectState.output)}"]`)?.classList.add("connecting");
    document.querySelectorAll('.port.input[data-port-node]').forEach((port) => {
      if (port.getAttribute("data-port-node") !== connectState.from) port.classList.add("compatible");
    });
  }

  function startConnection(event) {
    if (event.button !== undefined && event.button !== 0) return;
    const port = event.currentTarget?.closest?.(".port.output");
    if (!port) return;
    const from = port.getAttribute("data-port-node") || "";
    const output = port.getAttribute("data-port-name") || "";
    if (!from || !output) return;
    selectedId = from;
    connectState = { from, output, current: portPoint(from, "output", output) };
    markConnectionPorts();
    renderInspector();
    renderConnectionPanel();
    renderEdges();
    setStatus(`正在連線：${output}。拉到紫色 input 放開即可建立連線。`);
    event.preventDefault();
    event.stopPropagation();
    window.addEventListener("pointermove", onConnectionMove);
    window.addEventListener("mousemove", onConnectionMove);
    window.addEventListener("pointerup", onConnectionPointerUp, { once: true });
    window.addEventListener("mouseup", onConnectionPointerUp, { once: true });
    window.addEventListener("keydown", cancelConnectionOnEscape);
  }

  function startConnectionFromKeyboard(event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    const port = event.currentTarget?.closest?.(".port.output");
    if (!port) return;
    const from = port.getAttribute("data-port-node") || "";
    const output = port.getAttribute("data-port-name") || "";
    if (!from || !output) return;
    selectedId = from;
    connectState = { from, output, current: portPoint(from, "output", output) };
    markConnectionPorts();
    renderInspector();
    renderConnectionPanel();
    renderEdges();
    setStatus(`已選取 output：${output}。移到紫色 input 按 Enter 建立連線。`);
    event.preventDefault();
    event.stopPropagation();
    window.addEventListener("keydown", cancelConnectionOnEscape);
  }

  function onConnectionMove(event) {
    if (!connectState) return;
    connectState.current = canvasPoint(event);
    renderEdges();
  }

  function onConnectionPointerUp(event) {
    const input = event.target?.closest?.(".port.input[data-port-node]");
    if (input) {
      completeConnection({ currentTarget: input, preventDefault: () => event.preventDefault(), stopPropagation: () => event.stopPropagation() });
      return;
    }
    cancelConnection("連線已取消。");
  }

  function cancelConnectionOnEscape(event) {
    if (event.key === "Escape") cancelConnection("連線已取消。");
  }

  function cancelConnection(message = "") {
    connectState = null;
    window.removeEventListener("pointermove", onConnectionMove);
    window.removeEventListener("mousemove", onConnectionMove);
    window.removeEventListener("keydown", cancelConnectionOnEscape);
    markConnectionPorts();
    renderEdges();
    if (message) setStatus(message);
  }

  function completeConnection(event) {
    if (!connectState) return;
    const port = event.currentTarget?.closest?.(".port.input");
    const to = port?.getAttribute("data-port-node") || "";
    const input = port?.getAttribute("data-port-name") || "";
    const output = connectState.output;
    if (!to || !input || to === connectState.from) {
      cancelConnection("不能把節點連到自己。");
      return;
    }
    const ok = addEdge({ from: connectState.from, output: connectState.output, to, input });
    event.preventDefault();
    event.stopPropagation();
    cancelConnection();
    if (!ok) {
      setStatus("這條連線無效，請確認目標是可連接的 input。", false);
      return;
    }
    selectedId = to;
    render();
    setStatus(`已連線：${output} → ${input}`);
  }

  function completeConnectionFromKeyboard(event) {
    if (!connectState || (event.key !== "Enter" && event.key !== " ")) return;
    completeConnection({ currentTarget: event.currentTarget, preventDefault: () => event.preventDefault(), stopPropagation: () => event.stopPropagation() });
  }

  function customSchemaEditorMarkup(node) {
    const inputSpecs = Object.entries(node.inputSpecs || {});
    const outputs = Array.isArray(node.outputs) ? node.outputs : [];
    return `
      <div class="schema-editor">
        <div class="schema-editor-head">
          <strong>Custom node 欄位設計</strong>
          <span>用表單新增 input/output，不必手寫 JSON。</span>
        </div>
        <div class="schema-editor-block">
          <div class="schema-mini-grid">
            <input id="customInputName" maxlength="80" placeholder="input 名稱，例如 prompt">
            <select id="customInputType">
              <option value="text">文字</option>
              <option value="textarea">多行文字</option>
              <option value="number">數字</option>
              <option value="select">下拉選單</option>
              <option value="checkbox">核取方塊</option>
              <option value="link">連線 input</option>
            </select>
            <input id="customInputLabel" maxlength="120" placeholder="顯示名稱，可留空">
            <input id="customInputOptions" maxlength="240" placeholder="下拉選項，用逗號分隔">
            <button class="primary" id="addCustomInputBtn" type="button">新增輸入</button>
          </div>
          <div class="schema-list">
            ${inputSpecs.length ? inputSpecs.map(([key, spec]) => `
              <div class="schema-row">
                <div>
                  <strong>${html(key)}</strong>
                  <span>${html(customSchemaInputTypeLabel(spec.type))}${spec.label ? ` · ${html(spec.label)}` : ""}${Array.isArray(spec.options) && spec.options.length ? ` · ${html(spec.options.join(", "))}` : ""}</span>
                </div>
                <button class="danger" type="button" data-remove-custom-input="${html(key)}">刪除</button>
              </div>
            `).join("") : '<div class="empty">尚未定義 input。可新增文字欄位、下拉選單，或連線 input。</div>'}
          </div>
        </div>
        <div class="schema-editor-block">
          <div class="schema-mini-grid two">
            <input id="customOutputName" maxlength="80" placeholder="output 名稱，例如 IMAGE">
            <button class="primary" id="addCustomOutputBtn" type="button">新增輸出</button>
          </div>
          <div class="schema-list">
            ${outputs.length ? outputs.map((name) => `
              <div class="schema-row">
                <div>
                  <strong>${html(name)}</strong>
                  <span>output port</span>
                </div>
                <button class="danger" type="button" data-remove-custom-output="${html(name)}">刪除</button>
              </div>
            `).join("") : '<div class="empty">尚未定義 output；此節點會視為終點或 side-effect node。</div>'}
          </div>
        </div>
      </div>
    `;
  }

  function addCustomInputFromEditor(node) {
    const name = normalizeSchemaName($("customInputName")?.value || "");
    if (!name) {
      setStatus("請先填 input 名稱。", false);
      return;
    }
    if (node.inputSpecs?.[name]) {
      setStatus(`input ${name} 已存在；請先刪除或換一個名稱。`, false);
      return;
    }
    const spec = normalizeInputSpec({
      type: $("customInputType")?.value || "text",
      label: $("customInputLabel")?.value || name,
      options: parseOptionList($("customInputOptions")?.value || ""),
    });
    if (spec.type === "select" && !spec.options.length) {
      setStatus("下拉選單至少需要一個選項，請用逗號分隔。", false);
      return;
    }
    node.inputSpecs = { ...(node.inputSpecs || {}), [name]: spec };
    if (spec.type === "link") delete node.inputs[name];
    else node.inputs[name] = defaultValueForSpec(name, spec);
    render();
    setStatus(`已新增 custom input：${name}`);
  }

  function removeCustomInput(node, name) {
    const key = String(name || "");
    if (!key || !node.inputSpecs?.[key]) return;
    delete node.inputSpecs[key];
    delete node.inputs[key];
    workflow.edges = workflow.edges.filter((edge) => !(edge.to === node.id && edge.input === key));
    render();
    setStatus(`已刪除 custom input：${key}`);
  }

  function addCustomOutputFromEditor(node) {
    const name = normalizeSchemaName($("customOutputName")?.value || "").toUpperCase();
    if (!name) {
      setStatus("請先填 output 名稱。", false);
      return;
    }
    const outputs = Array.isArray(node.outputs) ? node.outputs : [];
    if (outputs.includes(name)) {
      setStatus(`output ${name} 已存在。`, false);
      return;
    }
    node.outputs = outputs.concat(name).slice(0, 32);
    render();
    setStatus(`已新增 custom output：${name}`);
  }

  function removeCustomOutput(node, name) {
    const output = String(name || "");
    if (!output) return;
    node.outputs = (node.outputs || []).filter((item) => item !== output);
    workflow.edges = workflow.edges.filter((edge) => !(edge.from === node.id && edge.output === output));
    render();
    setStatus(`已刪除 custom output：${output}`);
  }

  function bindCustomSchemaEditor(node) {
    $("addCustomInputBtn")?.addEventListener("click", () => addCustomInputFromEditor(node));
    $("addCustomOutputBtn")?.addEventListener("click", () => addCustomOutputFromEditor(node));
    document.querySelectorAll("[data-remove-custom-input]").forEach((button) => {
      button.addEventListener("click", () => removeCustomInput(node, button.getAttribute("data-remove-custom-input")));
    });
    document.querySelectorAll("[data-remove-custom-output]").forEach((button) => {
      button.addEventListener("click", () => removeCustomOutput(node, button.getAttribute("data-remove-custom-output")));
    });
  }

  function renderInspector() {
    const node = nodeById(selectedId);
    const badge = $("selectedBadge");
    const box = $("inspector");
    if (badge) badge.textContent = node ? node.type : "未選取";
    if (!box) return;
    if (!node) {
      box.innerHTML = '<div class="empty">先從左側新增節點，再選取節點調整屬性。</div>';
      return;
    }
    const def = nodeDef(node);
    if (isUnknownNode(node)) {
      const fields = Object.entries(def.inputs || {}).filter(([, spec]) => spec.type !== "link");
      box.innerHTML = `
        <div class="warning-list">
          <div>這是未知/custom node placeholder。會保留原始 class_type 與 inputs；input/output schema 需等連上 ComfyUI object_info 才能嚴格驗證。</div>
          <div>不要把 API Key、token、secret 寫進 inputs；ComfyUI Account API Key 由後端在執行時注入。</div>
        </div>
        <div class="field">
          <label>節點名稱</label>
          <input id="nodeLabelInput" value="${html(node.label || def.label)}" maxlength="80">
        </div>
        <div class="field">
          <label>原始 class_type</label>
          <input id="unknownClassInput" value="${html(node.originalType || "")}" maxlength="160">
        </div>
        ${customSchemaEditorMarkup(node)}
        <div class="inspector-grid">
          ${fields.map(([key, spec]) => inspectorInputMarkup(node, key, spec)).join("") || '<div class="empty">這個 custom node 沒有可直接編輯的值；連線欄位請用下方連線面板處理。</div>'}
        </div>
        <details class="advanced-json">
          <summary>進階：原始 inputs JSON</summary>
          <textarea id="unknownInputsInput" rows="10" spellcheck="false">${html(JSON.stringify(node.inputs || {}, null, 2))}</textarea>
        </details>
      `;
      $("nodeLabelInput")?.addEventListener("input", () => {
        node.label = $("nodeLabelInput").value;
        render();
      });
      $("unknownClassInput")?.addEventListener("input", () => {
        node.originalType = $("unknownClassInput").value.trim() || "UnknownCustomNode";
        render();
      });
      bindCustomSchemaEditor(node);
      bindInspectorValueFields(fields, node);
      $("unknownInputsInput")?.addEventListener("input", () => {
        try {
          const parsed = JSON.parse($("unknownInputsInput").value || "{}");
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            node.inputs = parsed;
            node.inputSpecs = unknownInputSpecs(parsed);
            render();
            setStatus("Custom node inputs 已更新。");
          }
        } catch (err) {
          setStatus(`Custom node inputs JSON 格式錯誤：${err.message || err}`, false);
        }
      });
      return;
    }
    const fields = Object.entries(def.inputs || {}).filter(([, spec]) => spec.type !== "link");
    box.innerHTML = `
      <div class="field">
        <label>節點名稱</label>
        <input id="nodeLabelInput" value="${html(node.label || def.label)}" maxlength="80">
      </div>
      <div class="inspector-grid">
        ${fields.map(([key, spec]) => inspectorInputMarkup(node, key, spec)).join("") || '<div class="empty">這個節點沒有可直接編輯的值；請用下方連線面板接 input。</div>'}
      </div>
    `;
    const labelInput = $("nodeLabelInput");
    if (labelInput) labelInput.addEventListener("input", () => {
      node.label = labelInput.value;
      render();
    });
    bindInspectorValueFields(fields, node);
  }

  function bindInspectorValueFields(fields, node) {
    fields.forEach(([key]) => {
      const input = $(`nodeInput-${key}`);
      if (!input) return;
      input.addEventListener("input", () => {
        node.inputs[key] = input.type === "number" ? Number(input.value) : input.type === "checkbox" ? input.checked : input.value;
        renderJson();
      });
      if (input.type === "checkbox") {
        input.addEventListener("change", () => {
          node.inputs[key] = input.checked;
          renderJson();
        });
      }
    });
  }

  function inspectorInputMarkup(node, key, spec) {
    const value = node.inputs?.[key] ?? "";
    if (spec.type === "select") {
      const options = Array.isArray(spec.options) ? spec.options : [];
      return `<div class="field"><label>${html(spec.label || key)}</label><select id="nodeInput-${html(key)}">${options.map((item) => `<option value="${html(item)}" ${String(item) === String(value) ? "selected" : ""}>${html(item)}</option>`).join("")}</select></div>`;
    }
    if (spec.type === "checkbox") {
      return `<div class="field checkbox-field"><label><input id="nodeInput-${html(key)}" type="checkbox" ${value ? "checked" : ""}> ${html(spec.label || key)}</label></div>`;
    }
    if (spec.type === "textarea") {
      return `<div class="field"><label>${html(spec.label || key)}</label><textarea id="nodeInput-${html(key)}" rows="4">${html(value)}</textarea></div>`;
    }
    return `<div class="field"><label>${html(spec.label || key)}</label><input id="nodeInput-${html(key)}" type="${spec.type === "number" ? "number" : "text"}" step="${html(spec.step || "1")}" value="${html(String(value))}"></div>`;
  }

  function renderConnectionPanel() {
    const panel = $("connectionPanel");
    const badge = $("connectionBadge");
    const source = nodeById(selectedId);
    if (badge) badge.textContent = source ? "可連線" : "-";
    if (!panel) return;
    if (!source) {
      panel.innerHTML = '<div class="empty">選取來源節點後，可把它的 output 連到其他節點 input。</div>';
      return;
    }
    const outputs = nodeDef(source)?.outputs || [];
    const targets = workflow.nodes.filter((node) => node.id !== source.id);
    const target = targets[0] || null;
    const selectedEdges = workflow.edges.filter((edge) => edge.from === source.id || edge.to === source.id);
    const allWarnings = workflowWarnings();
    panel.innerHTML = `
      ${allWarnings.length ? `
        <div class="warning-list">
          ${allWarnings.slice(0, 6).map((warning) => `<div>${html(warning)}</div>`).join("")}
          ${allWarnings.length > 6 ? `<div>另 ${html(String(allWarnings.length - 6))} 個提醒</div>` : ""}
        </div>
      ` : '<div class="empty">從綠色 output 拉到紫色 input 可建立線路；同一個 input 只會保留最新一條線。</div>'}
      <div class="field">
        <label>來源 output</label>
        <select id="edgeOutput">${outputs.map((name) => `<option value="${html(name)}">${html(name)}</option>`).join("")}</select>
      </div>
      <div class="field">
        <label>目標節點</label>
        <select id="edgeTarget">${targets.map((node) => `<option value="${html(node.id)}">${html(node.label || nodeDef(node)?.label || node.type)}</option>`).join("")}</select>
      </div>
      <div class="field">
        <label>目標 input</label>
        <select id="edgeInput">${targetInputOptions(target).join("")}</select>
      </div>
      <div class="row-actions">
        <button class="primary" id="addEdgeBtn" type="button" ${!outputs.length || !targets.length ? "disabled" : ""}>建立連線</button>
        <button id="removeSelectedEdgesBtn" type="button">移除此節點連線</button>
      </div>
      <div class="edge-list">
        ${selectedEdges.length ? selectedEdges.map((edge) => edgeRow(edge)).join("") : '<div class="empty">目前選取節點沒有連線。</div>'}
      </div>
    `;
    const targetSelect = $("edgeTarget");
    if (targetSelect) targetSelect.addEventListener("change", () => {
      const inputSelect = $("edgeInput");
      if (inputSelect) inputSelect.innerHTML = targetInputOptions(nodeById(targetSelect.value)).join("");
    });
    $("addEdgeBtn")?.addEventListener("click", addEdgeFromPanel);
    $("removeSelectedEdgesBtn")?.addEventListener("click", () => {
      workflow.edges = workflow.edges.filter((edge) => edge.from !== source.id && edge.to !== source.id);
      render();
    });
    panel.querySelectorAll("[data-delete-edge]").forEach((button) => {
      button.addEventListener("click", () => {
        deleteEdge(button.getAttribute("data-delete-edge"));
      });
    });
  }

  function edgeRow(edge) {
    const from = nodeById(edge.from);
    const to = nodeById(edge.to);
    return `
      <div class="edge-list-row ${edge.warning ? "warn" : ""}">
        <div>
          <strong>${html(from?.label || edge.from)}</strong>
          <span>${html(edge.output)} → ${html(to?.label || edge.to)}.${html(edge.input)}</span>
          ${edge.warning ? `<small>${html(edge.warning)}</small>` : ""}
        </div>
        <button class="danger" type="button" data-delete-edge="${html(edge.id)}">刪除線</button>
      </div>
    `;
  }

  function deleteEdge(id) {
    workflow.edges = workflow.edges.filter((edge) => edge.id !== id);
    render();
    setStatus("已刪除線路。");
  }

  function targetInputOptions(node) {
    if (!node) return [];
    return Object.entries(nodeDef(node)?.inputs || {})
      .filter(([, spec]) => spec.type === "link")
      .map(([key, spec]) => `<option value="${html(key)}">${html(spec.label || key)}</option>`);
  }

  function addEdgeFromPanel() {
    const from = nodeById(selectedId);
    const to = nodeById($("edgeTarget")?.value || "");
    const output = $("edgeOutput")?.value || "";
    const input = $("edgeInput")?.value || "";
    if (!from || !to || !output || !input) return;
    addEdge({ from: from.id, output, to: to.id, input });
    render();
  }

  function addEdge({ from, output, to, input }) {
    const source = nodeById(from);
    const target = nodeById(to);
    if (!source || !target || source.id === target.id || !output || !input) return false;
    const targetInput = nodeDef(target)?.inputs?.[input];
    if (!targetInput || targetInput.type !== "link") return false;
    workflow.edges = workflow.edges.filter((edge) => !(edge.to === target.id && edge.input === input));
    workflow.edges.push({ id: uid(), from: source.id, output, to: target.id, input, warning: connectionWarning(source.id, output, target.id, input) });
    return true;
  }

  function renderJson() {
    const out = $("jsonOut");
    if (out) out.value = JSON.stringify(exportPackage(), null, 2);
  }

  function createTxt2ImgStarter() {
    workflow = emptyState();
    workflow.name = $("workflowName")?.value || "txt2img 起始工作流";
    workflow.description = $("workflowDescription")?.value || "視覺編輯器建立的 txt2img 基礎 workflow";
    const specs = [
      ["CheckpointLoaderSimple", 70, 80, "主模型", { ckpt_name: "" }],
      ["CLIPTextEncode", 350, 35, "正向提示詞", { text: "masterpiece, best quality" }],
      ["CLIPTextEncode", 350, 205, "負向提示詞", { text: "low quality, blurry" }],
      ["EmptyLatentImage", 350, 375, "畫布尺寸", { width: 1024, height: 1024, batch_size: 1 }],
      ["KSampler", 660, 190, "採樣器", { seed: 0, steps: 20, cfg: 7, sampler_name: "euler", scheduler: "normal", denoise: 1 }],
      ["VAEDecode", 950, 190, "VAE 解碼", {}],
      ["SaveImage", 1220, 190, "儲存圖片", { filename_prefix: "hackme_web" }],
    ];
    specs.forEach(([type, x, y, label, inputs]) => {
      const node = { id: uid(), type, label, x, y, inputs: { ...defaultInputs(type), ...(inputs || {}) } };
      workflow.nodes.push(node);
    });
    const [ckpt, pos, neg, latent, sampler, decode, save] = workflow.nodes;
    workflow.edges = [
      { id: uid(), from: ckpt.id, output: "CLIP", to: pos.id, input: "clip" },
      { id: uid(), from: ckpt.id, output: "CLIP", to: neg.id, input: "clip" },
      { id: uid(), from: ckpt.id, output: "MODEL", to: sampler.id, input: "model" },
      { id: uid(), from: pos.id, output: "CONDITIONING", to: sampler.id, input: "positive" },
      { id: uid(), from: neg.id, output: "CONDITIONING", to: sampler.id, input: "negative" },
      { id: uid(), from: latent.id, output: "LATENT", to: sampler.id, input: "latent_image" },
      { id: uid(), from: sampler.id, output: "LATENT", to: decode.id, input: "samples" },
      { id: uid(), from: ckpt.id, output: "VAE", to: decode.id, input: "vae" },
      { id: uid(), from: decode.id, output: "IMAGE", to: save.id, input: "images" },
    ];
    selectedId = ckpt.id;
    render();
    setStatus("已建立 txt2img 節點圖，可拖曳節點、調整屬性或新增連線。");
  }

  function sendBackToMainPage() {
    const payload = exportPackage();
    localStorage.setItem(editorScopedStorageKey(RESULT_KEY), JSON.stringify(payload));
    setStatus("已送回主頁。回到 ComfyUI 頁按「載入視覺編輯器結果」即可保存。");
  }

  async function copyJson() {
    const text = JSON.stringify(exportComfyUiWorkflowGraph(), null, 2);
    try {
      await navigator.clipboard.writeText(text);
      setStatus("已複製 ComfyUI workflow JSON，可貼到 ComfyUI 或另存為 .json 後載入。");
    } catch (_) {
      $("jsonOut").value = text;
      $("jsonOut").select();
      setStatus("無法直接寫入剪貼簿，已選取 JSON。", false);
    }
  }

  function workflowExportFileName(suffix = "") {
    const base = String(workflow.name || "comfyui-workflow")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9\u4e00-\u9fff._-]+/gi, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 80) || "comfyui-workflow";
    return `${base}${suffix}.json`;
  }

  function downloadJsonObject(payload, filename, statusMessage) {
    const text = JSON.stringify(payload, null, 2);
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setStatus(statusMessage);
  }

  function downloadJson() {
    downloadJsonObject(
      exportComfyUiWorkflowGraph(),
      workflowExportFileName(),
      "已下載 ComfyUI workflow JSON，可用 ComfyUI 介面的 Load 載入。"
    );
  }

  function downloadApiJson() {
    downloadJsonObject(
      exportComfyUiApiPrompt(),
      workflowExportFileName(".api-prompt"),
      "已下載 ComfyUI API prompt JSON，適合送到 /prompt 或本站後端執行。"
    );
  }

  function downloadPresetJson() {
    downloadJsonObject(
      exportPackage(),
      workflowExportFileName(".hackme-preset"),
      "已下載本站 preset JSON；這份包含 layout/metadata，ComfyUI 不能直接載入。"
    );
  }

  async function importJsonFile(event) {
    const file = event.target?.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      assertEditorAccountScope();
      if (event.target?.files?.[0] !== file) return;
      const payload = JSON.parse(text);
      const imported = stateFromPackage(payload);
      if (!imported.state.nodes.length) {
        setStatus("匯入失敗：沒有可視覺化的 allowlist 節點。", false);
        return;
      }
      workflow = normalizeState(imported.state);
      selectedId = workflow.nodes[0]?.id || null;
      lastImportWarnings = imported.warnings;
      render();
      setStatus(imported.warnings.length ? `已匯入 JSON，但有 ${imported.warnings.length} 個提醒。` : "已匯入 JSON 並轉成節點圖。", !imported.warnings.length);
    } catch (err) {
      if (editorAccountInvalidated || editorAccountStorageScope() !== EDITOR_INITIAL_ACCOUNT_SCOPE) return;
      setStatus(`匯入失敗：${err.message || err}`, false);
    } finally {
      if (event.target) event.target.value = "";
    }
  }

  function renderNodeCatalogList() {
    const list = $("dynamicNodeCatalogList");
    const group = $("dynamicNodeCatalogGroup");
    if (!list || !group) return;
    const query = String($("nodeSearchInput")?.value || "").trim().toLowerCase();
    const items = nodeCatalog
      .filter((node) => {
        const haystack = `${node.class_type || ""} ${node.display_name || ""} ${node.category || ""}`.toLowerCase();
        return !query || haystack.includes(query);
      })
      .slice(0, 120);
    group.hidden = !nodeCatalog.length;
    group.classList.toggle("is-hidden", !items.length && !!query);
    if (query && items.length) group.open = true;
    const count = $("dynamicNodeCatalogCount");
    if (count) count.textContent = String(query ? items.length : nodeCatalog.length);
    const groups = new Map();
    items.forEach((node) => {
      const category = String(node.category || "未分類").trim() || "未分類";
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push(node);
    });
    list.innerHTML = items.length ? Array.from(groups.entries()).map(([category, categoryItems]) => `
      <details class="catalog-category"${query ? " open" : ""}>
        <summary><span>${html(category)}</span><span class="tool-count">${html(String(categoryItems.length))}</span></summary>
        <div class="tool-grid">
          ${categoryItems.map((node) => {
            const searchText = `${node.class_type || ""} ${node.display_name || ""} ${category}`;
            return `
            <button data-add-catalog-node="${html(node.class_type)}" data-search-text="${html(searchText)}" type="button" class="${node.paid_api_required ? "warn" : ""}">
              <b>${html(node.display_name || node.class_type)}</b>
              <span>${html(node.class_type)}${node.paid_api_required ? " · 付費/API" : ""}</span>
            </button>
          `;
          }).join("")}
        </div>
      </details>
    `).join("") : '<div class="empty">沒有符合搜尋的 ComfyUI 節點。</div>';
    list.querySelectorAll("[data-add-catalog-node]").forEach((button) => {
      button.addEventListener("click", () => addCatalogNode(button.getAttribute("data-add-catalog-node")));
    });
  }

  async function loadNodeCatalog() {
    const status = $("nodeCatalogStatus");
    if (status) status.textContent = "正在連線 ComfyUI /object_info...";
    try {
      const res = await editorFetch("/api/comfyui/node-catalog", { credentials: "same-origin" });
      const json = await res.json().catch(() => ({}));
      assertEditorAccountScope();
      if (!res.ok || !json.ok) {
        const msg = json.msg || `節點目錄載入失敗（HTTP ${res.status}）`;
        if (status) status.textContent = msg;
        setStatus(msg, false);
        return;
      }
      nodeCatalog = Array.isArray(json.nodes) ? json.nodes : [];
      renderNodeCatalogList();
      const paidCount = nodeCatalog.filter((node) => node.paid_api_required).length;
      const text = `已載入 ${nodeCatalog.length} 個節點${paidCount ? `，其中 ${paidCount} 個可能需要付費/API Key` : ""}。`;
      if (status) status.textContent = text;
      setStatus(text, true);
    } catch (err) {
      if (err?.name === "AbortError") return;
      const msg = `節點目錄載入失敗：${err.message || err}`;
      if (status) status.textContent = msg;
      setStatus(msg, false);
    }
  }

  function filterNodePalette() {
    const query = String($("nodeSearchInput")?.value || "").trim().toLowerCase();
    let visibleCount = 0;
    renderNodeCatalogList();
    document.querySelectorAll("[data-add-node], [data-add-catalog-node]").forEach((button) => {
      const groupTitle = button.closest(".tool-group")?.querySelector(".tool-title")?.textContent || "";
      const categoryTitle = button.closest(".catalog-category")?.querySelector("summary")?.textContent || "";
      const haystack = [
        button.getAttribute("data-add-node") || "",
        button.getAttribute("data-add-catalog-node") || "",
        button.getAttribute("data-search-text") || "",
        groupTitle,
        categoryTitle,
        button.textContent || "",
      ].join(" ").toLowerCase();
      const visible = !query || haystack.includes(query);
      button.classList.toggle("is-hidden", !visible);
      if (visible) visibleCount += 1;
    });
    document.querySelectorAll(".tool-group").forEach((group) => {
      if (group.classList.contains("catalog-loader")) return;
      const hasVisible = !!group.querySelector("[data-add-node]:not(.is-hidden), [data-add-catalog-node]:not(.is-hidden)");
      group.classList.toggle("is-hidden", !hasVisible);
      if (query && hasVisible) group.open = true;
    });
    if (query) setStatus(visibleCount ? `節點搜尋：${visibleCount} 個結果。` : "節點搜尋沒有結果。", !!visibleCount);
  }

  function clearAll() {
    if (!confirm("清空目前畫布？")) return;
    workflow = emptyState();
    selectedId = null;
    render();
  }

  function bind() {
    document.querySelectorAll("[data-add-node]").forEach((button) => {
      button.addEventListener("click", () => addNode(button.getAttribute("data-add-node")));
    });
    $("loadNodeCatalogBtn")?.addEventListener("click", loadNodeCatalog);
    $("nodeSearchInput")?.addEventListener("input", filterNodePalette);
    $("starterTxt2ImgBtn")?.addEventListener("click", createTxt2ImgStarter);
    $("autoLayoutBtn")?.addEventListener("click", autoLayoutNodes);
    $("importJsonFile")?.addEventListener("change", (event) => importJsonFile(event));
    $("clearBtn")?.addEventListener("click", clearAll);
    $("sendBackBtn")?.addEventListener("click", sendBackToMainPage);
    $("downloadJsonBtn")?.addEventListener("click", downloadJson);
    $("downloadApiJsonBtn")?.addEventListener("click", downloadApiJson);
    $("downloadPresetJsonBtn")?.addEventListener("click", downloadPresetJson);
    $("copyJsonBtn")?.addEventListener("click", copyJson);
    $("workflowName")?.addEventListener("input", () => { workflow.name = $("workflowName").value; renderJson(); saveState(); });
    $("workflowDescription")?.addEventListener("input", () => { workflow.description = $("workflowDescription").value; renderJson(); saveState(); });
  }

  bind();
  if (takePendingInput()) {
    // Loaded from main page or saved preset handoff.
  } else if (!workflow.nodes.length) createTxt2ImgStarter();
  else {
    selectedId = workflow.nodes[0]?.id || null;
    render();
  }
})();
