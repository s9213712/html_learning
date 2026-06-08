"""Hugging Face metadata helpers for the Diffusers backend."""

from __future__ import annotations

import hashlib
import html
import importlib.util
import os
import re
import time
from pathlib import PurePosixPath
from urllib import request as urllib_request

from services.comfyui.settings import normalize_huggingface_repo_id


_MODEL_FILE_EXTENSIONS = {".safetensors", ".bin", ".ckpt", ".pt", ".pth", ".gguf"}
_UNSUPPORTED_PIPELINE_TAGS = {
    "audio-to-audio",
    "automatic-speech-recognition",
    "image-to-video",
    "text-to-audio",
    "text-to-speech",
    "text-to-video",
    "video-to-video",
}
_HF_PIPELINE_MODE_TAGS = {
    "text-to-image": "txt2img",
    "unconditional-image-generation": "txt2img",
    "image-to-image": "img2img",
    "image-to-text": "i2t",
    "image-text-to-text": "i2t",
    "image-classification": "i2t",
    "visual-question-answering": "i2t",
    "document-question-answering": "i2t",
    "text2text-generation": "t2t",
    "text-generation": "t2t",
    "summarization": "t2t",
    "translation": "t2t",
    "text-to-video": "t2v",
    "image-to-video": "i2v",
    "video-to-video": "v2v",
    "text-to-audio": "t2s",
    "text-to-speech": "t2s",
}
_HF_MODE_ORDER = ["txt2img", "img2img", "inpaint", "t2t", "i2t", "t2v", "i2v", "v2v", "t2s", "t2sv"]
_DIFFUSERS_IMAGE_GENERATION_MODES = {"txt2img", "img2img", "inpaint"}
_HF_MAX_DIFFUSERS_VARIANT_BYTES = 16 * 1024**3
_PRECISION_LABELS = {
    "default": "預設精度",
    "fp16": "FP16 / half",
    "bf16": "BF16",
    "fp32": "FP32",
}
_PRECISION_ORDER = {"default": 0, "fp16": 1, "bf16": 2, "fp32": 3}
_DIFFUSERS_COMPONENT_DIRS = {
    "controlnet",
    "prior",
    "text_conditioner",
    "text_encoder",
    "text_encoder_2",
    "text_encoder_3",
    "transformer",
    "unet",
    "vae",
}
_HF_REPO_INSPECT_CACHE = {}
_HF_REPO_INSPECT_CACHE_TTL_SECONDS = max(
    0,
    min(3600, int(os.environ.get("COMFYUI_HF_REPO_INSPECT_CACHE_SECONDS", "60") or "60")),
)
_FROM_PRETRAINED_RE = re.compile(
    r"(?P<class>[A-Za-z_][A-Za-z0-9_]*Pipeline)\.from_pretrained\(\s*"
    r"(?P<quote>['\"])(?P<repo>[^'\"]+)(?P=quote)(?P<args>.*?)\)",
    re.DOTALL,
)


def _hf_repo_inspect_cache_key(repo_id, token, mode):
    token_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16] if token else ""
    return (str(repo_id or ""), token_hash, str(mode or "txt2img").strip().lower() or "txt2img")


def _get_hf_repo_inspect_cache(key):
    if _HF_REPO_INSPECT_CACHE_TTL_SECONDS <= 0:
        return None
    cached = _HF_REPO_INSPECT_CACHE.get(key)
    if not cached:
        return None
    if time.time() - float(cached.get("at") or 0) > _HF_REPO_INSPECT_CACHE_TTL_SECONDS:
        _HF_REPO_INSPECT_CACHE.pop(key, None)
        return None
    payload = dict(cached.get("payload") or {})
    if payload:
        payload["cache"] = {"hit": True, "ttl_seconds": _HF_REPO_INSPECT_CACHE_TTL_SECONDS}
    return payload or None


def _set_hf_repo_inspect_cache(key, payload):
    if _HF_REPO_INSPECT_CACHE_TTL_SECONDS <= 0 or not isinstance(payload, dict):
        return
    _HF_REPO_INSPECT_CACHE[key] = {"at": time.time(), "payload": dict(payload)}
    if len(_HF_REPO_INSPECT_CACHE) > 128:
        oldest_key = min(_HF_REPO_INSPECT_CACHE, key=lambda item: _HF_REPO_INSPECT_CACHE[item].get("at") or 0)
        _HF_REPO_INSPECT_CACHE.pop(oldest_key, None)


def normalize_diffusers_variant(value, *, allow_blank=True):
    raw = str(value or "").strip()
    if not raw or raw in {"__default__", "default"}:
        return "" if allow_blank else None
    if len(raw) > 64 or not re.match(r"^[A-Za-z0-9._-]+$", raw):
        return None
    return raw


def normalize_huggingface_repo_file(value, *, allow_blank=True):
    raw = str(value or "").strip()
    if not raw:
        return "" if allow_blank else None
    if len(raw) > 260 or raw.startswith(("/", "\\")) or "\\" in raw:
        return None
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    if any(".." in part for part in parts):
        return None
    return "/".join(parts)


def _sibling_filename(sibling):
    if isinstance(sibling, dict):
        return str(sibling.get("rfilename") or sibling.get("filename") or "").strip()
    return str(getattr(sibling, "rfilename", "") or getattr(sibling, "filename", "") or "").strip()


def _sibling_size(sibling):
    for attr in ("size", "blob_size"):
        value = sibling.get(attr) if isinstance(sibling, dict) else getattr(sibling, attr, None)
        if isinstance(value, int) and value >= 0:
            return value
    lfs = sibling.get("lfs") if isinstance(sibling, dict) else getattr(sibling, "lfs", None)
    if isinstance(lfs, dict):
        value = lfs.get("size")
    else:
        value = getattr(lfs, "size", None)
    return value if isinstance(value, int) and value >= 0 else None


def _arg_value(call_args, name):
    match = re.search(rf"\b{re.escape(name)}\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|[^,\n)]+)", str(call_args or ""))
    return match.group("value").strip() if match else ""


def _string_literal_value(value):
    raw = str(value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return raw[1:-1]
    return ""


def _bool_literal_value(value):
    raw = str(value or "").strip().lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    return None


def _dtype_hint(value):
    lowered = str(value or "").lower()
    if "bfloat16" in lowered or "bf16" in lowered:
        return "bfloat16"
    if "float16" in lowered or "fp16" in lowered or "torch.half" in lowered:
        return "float16"
    if "float32" in lowered or "fp32" in lowered:
        return "float32"
    return ""


def parse_diffusers_model_card_hints(card_text, repo_id):
    hints = {}
    for match in _FROM_PRETRAINED_RE.finditer(str(card_text or "")):
        if match.group("repo") != repo_id:
            continue
        class_name = match.group("class")
        call_args = match.group("args") or ""
        if class_name == "DiffusionPipeline":
            hints["pipeline_loader"] = "diffusion"
        elif class_name == "AutoPipelineForText2Image":
            hints["pipeline_loader"] = "auto"
        for dtype_kwarg in ("dtype", "torch_dtype"):
            dtype = _dtype_hint(_arg_value(call_args, dtype_kwarg))
            if dtype:
                hints["dtype"] = dtype
                hints["dtype_kwarg"] = dtype_kwarg
                break
        for name in ("device_map", "variant", "revision", "subfolder", "custom_pipeline"):
            value = _string_literal_value(_arg_value(call_args, name))
            if value:
                hints[name] = value
        trust_remote_code = _bool_literal_value(_arg_value(call_args, "trust_remote_code"))
        if trust_remote_code is not None:
            hints["trust_remote_code"] = trust_remote_code
        if hints:
            hints["source"] = "model_card"
            hints["class_name"] = class_name
            break
    return hints


def _load_model_card_hints(repo_id, token):
    try:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo_id=repo_id, filename="README.md", repo_type="model", token=(token or None))
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            hints = parse_diffusers_model_card_hints(fh.read(), repo_id)
        if hints:
            return hints
    except Exception:
        pass
    try:
        url = f"https://huggingface.co/{repo_id}"
        headers = {"User-Agent": "hackme-hf-diffusers-inspect/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib_request.Request(url, headers=headers)
        with urllib_request.urlopen(req, timeout=20) as response:
            page_text = response.read().decode("utf-8", errors="replace")
        hints = parse_diffusers_model_card_hints(html.unescape(page_text), repo_id)
        if hints:
            hints["source"] = "model_page"
            return hints
    except Exception:
        pass
    return {}


def _precision_from_filename(filename):
    lowered = str(filename or "").lower()
    if re.search(r"(^|[._/-])(fp16|float16|half)([._/-]|$)", lowered):
        return "fp16"
    if re.search(r"(^|[._/-])(bf16|bfloat16)([._/-]|$)", lowered):
        return "bf16"
    if re.search(r"(^|[._/-])(fp32|float32)([._/-]|$)", lowered):
        return "fp32"
    return "default"


def _format_size_bytes(size):
    value = float(int(size))
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    suffix = units[index]
    return f"{value:.2f} {suffix}" if index > 0 else f"{int(value)} {suffix}"


def _gguf_quant_from_filename(filename):
    stem = PurePosixPath(str(filename or "")).name
    match = re.search(r"(Q[2-8](?:_[01])?|Q[2-6]_K(?:_[SM])?|BF16|F16|FP16|F32|FP32)", stem, re.IGNORECASE)
    return match.group(1).upper() if match else "GGUF"


def _sorted_hf_modes(modes):
    order = {mode: index for index, mode in enumerate(_HF_MODE_ORDER)}
    return sorted(
        {str(mode or "").strip().lower() for mode in modes if str(mode or "").strip()},
        key=lambda mode: (order.get(mode, len(order)), mode),
    )


def _option_sort_key(option):
    if option.get("kind") == "gguf":
        return (0, str(option.get("label") or ""))
    return (1, _PRECISION_ORDER.get(str(option.get("precision") or ""), 50), str(option.get("label") or ""))


def build_diffusers_variant_options(siblings):
    groups = {}
    gguf_options = []
    sibling_names = [_sibling_filename(sibling) for sibling in (siblings or [])]
    has_component_weights = any(
        PurePosixPath(name).suffix.lower() in {".safetensors", ".bin"}
        and PurePosixPath(name).parts
        and PurePosixPath(name).parts[0] in _DIFFUSERS_COMPONENT_DIRS
        for name in sibling_names
    )
    for sibling in siblings or []:
        filename = _sibling_filename(sibling)
        if not filename:
            continue
        path = PurePosixPath(filename)
        if path.suffix.lower() not in _MODEL_FILE_EXTENSIONS:
            continue
        size = _sibling_size(sibling)
        if path.suffix.lower() == ".gguf":
            quant = _gguf_quant_from_filename(filename)
            gguf_options.append({
                "kind": "gguf",
                "value": f"gguf::{filename}",
                "variant": "",
                "gguf_file": filename,
                "precision": quant.lower(),
                "label": f"GGUF {quant} · {path.name}",
                "size_bytes": int(size or 0),
                "file_count": 1,
                "files": [filename],
                "requires_base_repo": True,
            })
            continue
        lowered = filename.lower()
        if any(skip in lowered for skip in ("optimizer", "scheduler", "training_args")):
            continue
        if has_component_weights and ((not path.parts) or path.parts[0] not in _DIFFUSERS_COMPONENT_DIRS):
            continue
        precision = _precision_from_filename(filename)
        group = groups.setdefault(precision, {"precision": precision, "size_bytes": 0, "file_count": 0, "files": []})
        if size is not None:
            group["size_bytes"] += size
        group["file_count"] += 1
        if len(group["files"]) < 8:
            group["files"].append(filename)
    options = []
    options.extend(gguf_options)
    for precision, group in sorted(groups.items(), key=lambda item: (_PRECISION_ORDER.get(item[0], 50), item[0])):
        options.append({
            "kind": "diffusers",
            "value": "__default__" if precision == "default" else precision,
            "variant": "" if precision == "default" else precision,
            "precision": precision,
            "label": _PRECISION_LABELS.get(precision, precision),
            "size_bytes": int(group["size_bytes"] or 0),
            "file_count": int(group["file_count"] or 0),
            "files": list(group["files"]),
        })
    return sorted(options, key=_option_sort_key)


def _info_value(info, name, default=None):
    if isinstance(info, dict):
        return info.get(name, default)
    return getattr(info, name, default)


def _card_data_values(card_data, key):
    if not card_data:
        return []
    value = card_data.get(key) if isinstance(card_data, dict) else getattr(card_data, key, None)
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def infer_gguf_base_repo(repo_id="", *, tags=None, card_data=None):
    candidates = []
    candidates.extend(_card_data_values(card_data, "base_model"))
    candidates.extend(_card_data_values(card_data, "base_model_name"))
    for tag in tags or []:
        text = str(tag or "").strip()
        if text.lower().startswith("base_model:"):
            candidates.append(text.split(":", 1)[1].strip())
    for candidate in candidates:
        normalized = normalize_huggingface_repo_id(candidate, allow_blank=True)
        if normalized:
            return normalized
    name = str(repo_id or "").split("/")[-1].lower()
    if any(token in name for token in ("sdxl", "illustrious", "pony", "noob")):
        return "stabilityai/stable-diffusion-xl-base-1.0"
    if "flux" in name:
        return "black-forest-labs/FLUX.1-schnell"
    return ""


def detect_diffusers_supported_modes(*, repo_id="", pipeline_tag="", library_name="", tags=None, config=None, siblings=None):
    repo_text = str(repo_id or "").lower()
    tag = str(pipeline_tag or "").strip().lower()
    library = str(library_name or "").strip().lower()
    tag_set = {str(item or "").strip().lower() for item in (tags or []) if str(item or "").strip()}
    config = config if isinstance(config, dict) else {}
    class_name = str(config.get("_class_name") or config.get("pipeline_class") or "").lower()
    sibling_names = [_sibling_filename(item).lower() for item in (siblings or [])]
    has_regular_model_index = any(PurePosixPath(name).name == "model_index.json" for name in sibling_names)
    has_modular_model_index = any(PurePosixPath(name).name == "modular_model_index.json" for name in sibling_names)
    has_model_index = has_regular_model_index or has_modular_model_index
    has_gguf = any(name.endswith(".gguf") for name in sibling_names)
    is_diffusers = library == "diffusers" or "diffusers" in tag_set or has_model_index
    inpaint_hint = any("inpaint" in item for item in [repo_text, tag, class_name, *tag_set])

    supported = set()
    for item in [tag, *tag_set]:
        if item in _HF_PIPELINE_MODE_TAGS:
            supported.add(_HF_PIPELINE_MODE_TAGS[item])
    if has_gguf:
        return _sorted_hf_modes(supported or (["txt2img"] if tag in {"", "text-to-image", "unconditional-image-generation"} else []))
    if not is_diffusers:
        return _sorted_hf_modes(mode for mode in supported if mode not in _DIFFUSERS_IMAGE_GENERATION_MODES)
    if tag in {"text-to-image", "unconditional-image-generation"}:
        supported.add("txt2img")
    if tag == "image-to-image":
        supported.add("img2img")
    if inpaint_hint:
        supported.add("inpaint")
    if is_diffusers and tag == "":
        supported.add("txt2img")
    if is_diffusers and any(name in class_name for name in ("text2image", "texttoimage", "text-to-image", "pipeline")):
        supported.add("txt2img")
    if is_diffusers and any(name in class_name for name in ("image2image", "imagetoimage", "img2img")):
        supported.add("img2img")
    if is_diffusers and any(name in class_name for name in ("text2text", "texttotext")):
        supported.add("t2t")
    if is_diffusers and any(name in class_name for name in ("image2text", "imagetotext")):
        supported.add("i2t")
    if is_diffusers and inpaint_hint:
        supported.add("inpaint")
    return _sorted_hf_modes(supported)


def inspect_huggingface_diffusers_repo(repo_value, *, token="", mode="txt2img"):
    repo_id = normalize_huggingface_repo_id(repo_value, allow_blank=True)
    if repo_id is None:
        return {"ok": False, "checked": False, "msg": "Hugging Face repo 格式不合法，請填 namespace/model 或模型頁網址"}
    if not repo_id:
        return {"ok": False, "checked": False, "msg": "請輸入 Hugging Face repo"}
    requested_mode = str(mode or "txt2img").strip().lower() or "txt2img"
    cache_key = _hf_repo_inspect_cache_key(repo_id, token, requested_mode)
    cached = _get_hf_repo_inspect_cache(cache_key)
    if cached:
        return cached
    if importlib.util.find_spec("huggingface_hub") is None:
        return {"ok": False, "checked": False, "repo_id": repo_id, "msg": "缺少 huggingface_hub 套件，無法在下載前檢查模型 metadata"}
    try:
        from huggingface_hub import HfApi
    except Exception as exc:
        return {"ok": False, "checked": False, "repo_id": repo_id, "msg": f"Hugging Face metadata 工具載入失敗：{exc}"}
    try:
        info = HfApi().model_info(repo_id, token=(token or None), files_metadata=True)
    except Exception as exc:
        return {"ok": False, "checked": False, "repo_id": repo_id, "msg": f"Hugging Face repo 檢查失敗，尚未開始下載：{exc}"}

    siblings = list(_info_value(info, "siblings", []) or [])
    sibling_names = [_sibling_filename(item).lower() for item in siblings]
    has_regular_model_index = any(PurePosixPath(name).name == "model_index.json" for name in sibling_names)
    has_modular_model_index = any(PurePosixPath(name).name == "modular_model_index.json" for name in sibling_names)
    pipeline_tag = str(_info_value(info, "pipeline_tag", "") or "")
    library_name = str(_info_value(info, "library_name", "") or "")
    tags = list(_info_value(info, "tags", []) or [])
    card_data = _info_value(info, "cardData", None) or _info_value(info, "card_data", None)
    config = _info_value(info, "config", {}) or {}
    supported_modes = detect_diffusers_supported_modes(
        repo_id=repo_id,
        pipeline_tag=pipeline_tag,
        library_name=library_name,
        tags=tags,
        config=config,
        siblings=siblings,
    )
    requires_modular_pipeline = bool(has_modular_model_index and not has_regular_model_index)
    if requires_modular_pipeline:
        supported_modes = []
    runnable_modes = [mode for mode in supported_modes if mode in _DIFFUSERS_IMAGE_GENERATION_MODES]
    variant_options = build_diffusers_variant_options(siblings)
    oversized_variants = []
    if _HF_MAX_DIFFUSERS_VARIANT_BYTES > 0:
        oversized_variants = [option for option in variant_options if int(option.get("size_bytes") or 0) > _HF_MAX_DIFFUSERS_VARIANT_BYTES]
        if oversized_variants:
            variant_options = [option for option in variant_options if int(option.get("size_bytes") or 0) <= _HF_MAX_DIFFUSERS_VARIANT_BYTES]
    runnable_modes = [mode for mode in supported_modes if mode in _DIFFUSERS_IMAGE_GENERATION_MODES]
    model_card_hints = _load_model_card_hints(repo_id, token)
    has_gguf_options = any(item.get("kind") == "gguf" for item in variant_options)
    suggested_base_repo = infer_gguf_base_repo(repo_id, tags=tags, card_data=card_data) if has_gguf_options else ""
    has_diffusers_metadata = (
        library_name.lower() == "diffusers"
        or "diffusers" in {str(item or "").lower() for item in tags}
        or any(
            PurePosixPath(_sibling_filename(item).lower()).name == "model_index.json"
            or PurePosixPath(_sibling_filename(item).lower()).name == "modular_model_index.json"
            for item in siblings
        )
        or bool(model_card_hints)
    )
    warnings = []
    if pipeline_tag.lower() in _UNSUPPORTED_PIPELINE_TAGS:
        warnings.append(f"此 repo 的 Hugging Face pipeline 是 {pipeline_tag}，不是本站 Diffusers 生圖模式。")
    non_runnable_modes = [mode for mode in supported_modes if mode not in _DIFFUSERS_IMAGE_GENERATION_MODES]
    if non_runnable_modes:
        warnings.append(
            "此 repo 偵測到 Hugging Face 模式 "
            + ", ".join(non_runnable_modes)
            + "；目前本站 HF / Diffusers 生圖器只支援文字生圖、圖生圖與局部重繪。"
        )
    if not has_diffusers_metadata and not has_gguf_options and not supported_modes:
        warnings.append("此 repo 的 Hugging Face metadata 沒有 Diffusers；請只使用模型頁 Use this model 內有 Diffusers 的 repo。")
    elif not has_diffusers_metadata and not has_gguf_options:
        warnings.append("此 repo 有 Hugging Face pipeline metadata，但不是本站 HF / Diffusers 生圖 repo。")
    if not supported_modes:
        warnings.append("沒有偵測到可用的 Hugging Face pipeline / Diffusers t2i / i2i metadata；為避免下載無法使用的模型，請改用支援的 repo。")
    elif not runnable_modes:
        warnings.append("此 repo 不屬於本站目前可直接執行的 Diffusers 生圖模式。")
    elif requested_mode not in runnable_modes:
        warnings.append(f"此 repo 目前偵測本站可執行 {', '.join(runnable_modes)}，不支援 {requested_mode}。")
    if oversized_variants:
        warnings.append(
            "此 repo 檢測到超過本專案上限（"
            + f"{_format_size_bytes(_HF_MAX_DIFFUSERS_VARIANT_BYTES)}）的精度版本已自動過濾："
            + "、".join([str(option.get("label", "未知")) for option in oversized_variants])
            + "。"
        )
    if oversized_variants and not variant_options:
        warnings.append("本專案暫不支援此 repo 的 HF/Diffusers 直接下載（可用版本皆超過單精度上限），請改用可同時支援 local/remote 的流程。")
        runnable_modes = []
    if len(variant_options) > 1:
        warnings.append("偵測到多個精度版本，請先選擇要下載/載入的版本，避免同一模型重複下載。")
    if model_card_hints:
        hint_parts = []
        for key in ("dtype", "dtype_kwarg", "device_map", "pipeline_loader", "variant", "revision", "subfolder"):
            if model_card_hints.get(key):
                hint_parts.append(f"{key}={model_card_hints[key]}")
        if hint_parts:
            warnings.append("已從 model card Diffusers 範例偵測官方載入參數：" + ", ".join(hint_parts) + "。")
    if requires_modular_pipeline:
        warnings.append(
            "此 repo 只有 modular_model_index.json、沒有 model_index.json；"
            "目前本站 DiffusionPipeline 通用路徑不支援 ModularPipeline repo。"
        )
    if has_gguf_options:
        warnings.append(
            "GGUF 需要選擇檔案；Diffusers component 需搭配 base repo，"
            "ComfyUI-GGUF 原生 UNet 會自動改走 ComfyUI workflow。"
            "本地 ComfyUI 可由本站從 HF cache 接入，遠端 ComfyUI 需管理人先放入 models/unet。"
        )
        if suggested_base_repo:
            warnings.append(f"已推定 base repo：{suggested_base_repo}。")
    if not variant_options and not oversized_variants:
        warnings.append("沒有取得模型檔大小；若這不是 Diffusers repo，生成前會被阻擋。")
    payload = {
        "ok": True,
        "checked": True,
        "repo_id": repo_id,
        "pipeline_tag": pipeline_tag,
        "library_name": library_name,
        "supported_modes": supported_modes,
        "runnable_modes": runnable_modes,
        "requested_mode": requested_mode,
        "supported_for_mode": requested_mode in runnable_modes,
        "variant_options": variant_options,
        "has_gguf": has_gguf_options,
        "requires_modular_pipeline": requires_modular_pipeline,
        "suggested_base_repo": suggested_base_repo,
        "model_card_hints": model_card_hints,
        "warnings": warnings,
    }
    _set_hf_repo_inspect_cache(cache_key, payload)
    return payload
