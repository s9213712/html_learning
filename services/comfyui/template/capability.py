"""§6 Capability check — match a WorkflowAnalysis against the local ComfyUI.

Given a sanitized & analyzed workflow, this module asks the running ComfyUI
instance two questions:

1. Are all class_types in the workflow registered locally? (covers custom
   nodes the user installed locally vs. the workflow author's machine.)
2. Are the required model files (ckpt / vae / lora / controlnet / upscale)
   present in the local ComfyUI's catalog?

The result is a ``CapabilityCheck`` whose ``overall`` field is one of
``SUPPORTED`` / ``PARTIALLY_SUPPORTED`` / ``UNSUPPORTED``. The §10 run gate
treats UNSUPPORTED as a hard block; PARTIALLY_SUPPORTED (only models
missing) is allowed to import but not run until the operator downloads
the missing models.

Spec reference: docs/comfyui/COMFYUI_TEMPLATE_IMPORTER_PLAN.md §6.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Protocol

from services.comfyui.template.analyzer import WorkflowAnalysis


# ----------------------------------------------------------------------------
# Process-local cache for /object_info (§6 — 5-minute TTL).
#
# Rationale: a single user opening a workflow may trigger 5–10 capability
# checks within seconds. We don't hammer the local ComfyUI with that many
# /object_info requests. The cache is intentionally process-local; multi-
# worker deployments simply pay the cache miss per worker, which is fine
# (object_info changes only when ComfyUI restarts or installs custom nodes).
# ----------------------------------------------------------------------------

_OBJECT_INFO_TTL_SECONDS = 300.0
_object_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_object_info_lock = threading.Lock()


def _cache_key(client) -> str:
    base_url = getattr(client, "base_url", None) or ""
    return str(base_url)


def _get_object_info_cached(client) -> dict[str, Any]:
    """Return ``client.get_object_info()`` payload, cached for 5 minutes."""
    now = time.monotonic()
    key = _cache_key(client)
    with _object_info_lock:
        cached = _object_info_cache.get(key)
        if cached and (now - cached[0]) < _OBJECT_INFO_TTL_SECONDS:
            return cached[1]
    info = client.get_object_info()
    if not isinstance(info, dict):
        info = {}
    with _object_info_lock:
        _object_info_cache[key] = (now, info)
    return info


def reset_object_info_cache() -> None:
    """Test helper / admin endpoint hook — drop the cached /object_info payload."""
    with _object_info_lock:
        _object_info_cache.clear()


# ----------------------------------------------------------------------------
# CapabilityCheck dataclass + computation.
# ----------------------------------------------------------------------------


CapabilityOverall = Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED"]


@dataclass
class CapabilityCheck:
    """Result of matching a workflow against a local ComfyUI."""

    supported: list[str] = field(default_factory=list)              # class_types present locally
    partial: list[tuple[str, str]] = field(default_factory=list)    # (class_type, reason)
    unsupported: list[str] = field(default_factory=list)             # class_types missing locally
    missing_models: dict[str, list[str]] = field(default_factory=dict)
    sampler_options: dict[str, list[str]] = field(default_factory=dict)
    overall: CapabilityOverall = "UNSUPPORTED"
    blockers: list[str] = field(default_factory=list)               # human-readable Chinese strings

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view for /api/comfyui/templates/preview responses."""
        return {
            "supported": list(self.supported),
            "partial": [{"class_type": cls, "reason": reason} for cls, reason in self.partial],
            "unsupported": list(self.unsupported),
            "missing_models": {k: list(v) for k, v in self.missing_models.items()},
            "sampler_options": {k: list(v) for k, v in self.sampler_options.items()},
            "overall": self.overall,
            "blockers": list(self.blockers),
        }


# Map MODEL bucket names (from analyzer.required_models) to the ComfyUI
# (class_type, input_name) pair we look up to enumerate what's available
# locally. Aligned with services/comfyui/client.py helpers (get_models,
# get_loras, get_vaes …).
_MODEL_BUCKET_OBJECT_INFO_PATHS: dict[str, tuple[tuple[str, str], ...]] = {
    "ckpt": (
        ("CheckpointLoaderSimple", "ckpt_name"),
        ("LTXVAudioVAELoader", "ckpt_name"),
        ("LTXAVTextEncoderLoader", "ckpt_name"),
    ),
    "vae": (("VAELoader", "vae_name"),),
    "lora": (("LoraLoader", "lora_name"), ("LoraLoaderModelOnly", "lora_name")),
    "controlnet": (("ControlNetLoader", "control_net_name"),),
    "upscale_model": (("UpscaleModelLoader", "model_name"),),
    "latent_upscale_model": (("LatentUpscaleModelLoader", "model_name"),),
    "diffusion_model": (
        ("UNETLoader", "unet_name"),
        ("UnetLoaderGGUF", "unet_name"),
        ("UnetLoaderGGUFAdvanced", "unet_name"),
    ),
    "clip": (
        ("CLIPLoader", "clip_name"),
        ("DualCLIPLoader", "clip_name1"),
        ("DualCLIPLoader", "clip_name2"),
        ("TripleCLIPLoader", "clip_name1"),
        ("TripleCLIPLoader", "clip_name2"),
        ("TripleCLIPLoader", "clip_name3"),
        ("CLIPLoaderGGUF", "clip_name"),
        ("DualCLIPLoaderGGUF", "clip_name1"),
        ("DualCLIPLoaderGGUF", "clip_name2"),
        ("TripleCLIPLoaderGGUF", "clip_name1"),
        ("TripleCLIPLoaderGGUF", "clip_name2"),
        ("TripleCLIPLoaderGGUF", "clip_name3"),
    ),
    "clip_vision": (("CLIPVisionLoader", "clip_name"),),
}

_MODEL_BUCKETS_NOT_LOCAL_FILES = frozenset({"api_model"})
_VAE_BUILTIN_SENTINEL = "__checkpoint_builtin__"

_MODEL_FIELD_OBJECT_INFO_PATHS: dict[tuple[str, str], tuple[str, str]] = {
    (class_type, input_name): (class_type, input_name)
    for paths in _MODEL_BUCKET_OBJECT_INFO_PATHS.values()
    for class_type, input_name in paths
}
_MODEL_FIELD_BUCKETS: dict[tuple[str, str], str] = {
    (class_type, input_name): bucket
    for bucket, paths in _MODEL_BUCKET_OBJECT_INFO_PATHS.items()
    for class_type, input_name in paths
}
_MODEL_BUCKET_CLIENT_METHODS: dict[str, str] = {
    "ckpt": "get_models",
    "vae": "get_vaes",
    "lora": "get_loras",
    "controlnet": "get_controlnet_models",
    "upscale_model": "get_upscale_models",
    "latent_upscale_model": "get_latent_upscale_models",
    "clip_vision": "get_clip_vision_models",
}


def _node_input_options(info: dict[str, Any], class_type: str, input_name: str) -> list[str]:
    """Mirror of `client._list_node_input_options` shape, but on a cached payload."""
    node = info.get(class_type)
    if not isinstance(node, dict):
        return []
    required = ((node.get("input") or {}).get("required") or {})
    raw = required.get(input_name) or []
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, list):
            return [str(x) for x in first if str(x).strip()]
        if isinstance(first, str) and len(raw) > 1 and isinstance(raw[1], dict):
            options = raw[1].get("options") or []
            if isinstance(options, list):
                return [str(x) for x in options if str(x).strip()]
    return []


def _model_basename(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1].lower()


def resolve_model_option(model_name: str, local_options: Iterable[str]) -> str:
    """Return the ComfyUI combo value matching a model literal.

    ComfyUI reports files inside model subfolders as `subdir/file.safetensors`.
    Many upstream workflows only store the basename. Treat a unique basename
    match as the local option so dependency checks and run-gate rewrites agree.
    """
    required = str(model_name or "").strip()
    if not required:
        return ""
    options = [str(option or "").strip() for option in local_options if str(option or "").strip()]
    for option in options:
        if option == required:
            return option
    required_path = required.replace("\\", "/")
    for option in options:
        if option.replace("\\", "/") == required_path:
            return option
    required_base = _model_basename(required)
    matches = [option for option in options if _model_basename(option) == required_base]
    unique_matches = sorted(set(matches))
    return unique_matches[0] if len(unique_matches) == 1 else ""


def model_option_available(model_name: str, local_options: Iterable[str]) -> bool:
    return bool(resolve_model_option(model_name, local_options))


def _client_model_options(client: Any, bucket: str) -> list[str]:
    method_name = _MODEL_BUCKET_CLIENT_METHODS.get(bucket)
    method = getattr(client, method_name or "", None)
    if not callable(method):
        return []
    try:
        values = method()
    except Exception:
        return []
    return [str(item or "").strip() for item in values or [] if str(item or "").strip()]


def _model_options_for_bucket(info: dict[str, Any], client: Any, bucket: str) -> list[str]:
    options: list[str] = []
    for class_type, input_name in _MODEL_BUCKET_OBJECT_INFO_PATHS.get(bucket, ()):
        options.extend(_node_input_options(info, class_type, input_name))
    if not options:
        options.extend(_client_model_options(client, bucket))
    return options


def _embedding_match_keys(value: Any) -> set[str]:
    text = str(value or "").strip().replace("\\", "/").rstrip("/")
    if not text:
        return set()
    base = text.rsplit("/", 1)[-1]
    path_no_ext = text
    base_no_ext = base
    for ext in (".safetensors", ".pt", ".pth", ".bin"):
        if path_no_ext.lower().endswith(ext):
            path_no_ext = path_no_ext[: -len(ext)]
        if base_no_ext.lower().endswith(ext):
            base_no_ext = base_no_ext[: -len(ext)]
    return {item.lower() for item in {text, base, path_no_ext, base_no_ext} if item}


def embedding_option_available(embedding_name: str, local_options: Iterable[str]) -> bool:
    required_keys = _embedding_match_keys(embedding_name)
    if not required_keys:
        return False
    for option in local_options:
        if required_keys & _embedding_match_keys(option):
            return True
    return False


def rewrite_workflow_model_inputs_to_local_options(
    workflow: dict[str, Any],
    *,
    client: _ObjectInfoClient | None,
) -> dict[str, Any]:
    """Patch model literals to exact local ComfyUI combo values when possible."""
    if client is None or not isinstance(workflow, dict):
        return workflow
    try:
        info = _get_object_info_cached(client)
    except Exception:
        return workflow
    patched = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        for input_name, raw_value in inputs.items():
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            option_ref = _MODEL_FIELD_OBJECT_INFO_PATHS.get((class_type, str(input_name)))
            if not option_ref:
                continue
            bucket = _MODEL_FIELD_BUCKETS.get((class_type, str(input_name)))
            options = _model_options_for_bucket(info, client, bucket) if bucket else _node_input_options(info, option_ref[0], option_ref[1])
            resolved = resolve_model_option(raw_value, options)
            if not resolved or resolved == raw_value:
                continue
            if patched is None:
                patched = copy.deepcopy(workflow)
            patched[str(node_id)]["inputs"][str(input_name)] = resolved
    return patched if patched is not None else workflow


class _ObjectInfoClient(Protocol):
    """The minimal interface this module needs from a ComfyUI client object."""

    def get_object_info(self) -> dict[str, Any]:
        ...


def check_workflow_capability(
    analysis: WorkflowAnalysis,
    *,
    client: _ObjectInfoClient | None,
) -> CapabilityCheck:
    """Compare the workflow's classes + models against a live ComfyUI.

    `client=None` is treated as "ComfyUI not configured / unreachable" — every
    class is reported as unsupported and overall=UNSUPPORTED, so the route
    layer can surface a clear "please connect ComfyUI first" error.
    """
    cap = CapabilityCheck()
    if client is None:
        for cls in sorted(analysis.class_types):
            cap.unsupported.append(cls)
        cap.blockers.append("尚未連線到本地 ComfyUI；無法判斷 workflow 是否可執行")
        cap.overall = "UNSUPPORTED"
        return cap

    try:
        info = _get_object_info_cached(client)
    except Exception as exc:  # ComfyUIError, network errors, etc.
        for cls in sorted(analysis.class_types):
            cap.unsupported.append(cls)
        cap.blockers.append(f"無法取得本地 ComfyUI 節點清單：{exc}")
        cap.overall = "UNSUPPORTED"
        return cap

    local_classes = set(info.keys())

    # Classify each class_type the workflow uses.
    for cls in sorted(analysis.class_types):
        if cls in analysis.denied_classes:
            cap.unsupported.append(cls)
            cap.blockers.append(f"節點 {cls} 在 hackme_web 的明確拒絕清單上（§4.3）")
            continue
        if cls in local_classes:
            cap.supported.append(cls)
        else:
            cap.unsupported.append(cls)
            cap.blockers.append(f"本地 ComfyUI 沒有節點 {cls}")

    # For each required model bucket the analyzer recorded, ask the local
    # ComfyUI which files it actually has and compute the diff.
    for bucket, names in (analysis.required_models or {}).items():
        if bucket == "vae":
            names = [n for n in names if str(n or "").strip() != _VAE_BUILTIN_SENTINEL]
        if bucket == "embedding":
            try:
                local_options = client.get_embeddings() if hasattr(client, "get_embeddings") else []
            except Exception:
                local_options = []
            missing = sorted({n for n in names if n and not embedding_option_available(n, local_options)})
            if missing:
                cap.missing_models[bucket] = missing
                cap.blockers.append(f"本地 ComfyUI 缺少 embedding：{missing}")
            continue
        paths = _MODEL_BUCKET_OBJECT_INFO_PATHS.get(bucket)
        if not paths:
            if bucket in _MODEL_BUCKETS_NOT_LOCAL_FILES:
                continue
            # Unknown bucket — fall back to MODEL bucket name as-is.
            cap.missing_models[bucket] = sorted(set(names))
            continue
        local_options = _model_options_for_bucket(info, client, bucket)
        missing = sorted({n for n in names if n and not model_option_available(n, local_options)})
        if missing:
            cap.missing_models[bucket] = missing
            cap.blockers.append(
                f"本地 ComfyUI 缺少 {bucket} 模型：{missing}"
            )

    # Capture sampler enums for the UI, even when unrelated to capability.
    for class_type in ("KSampler", "KSamplerAdvanced"):
        cap.sampler_options[f"{class_type}.sampler_name"] = _node_input_options(
            info, class_type, "sampler_name"
        )
        cap.sampler_options[f"{class_type}.scheduler"] = _node_input_options(
            info, class_type, "scheduler"
        )

    # Decide overall verdict.
    if cap.unsupported:
        cap.overall = "UNSUPPORTED"
    elif cap.missing_models:
        cap.overall = "PARTIALLY_SUPPORTED"
    else:
        cap.overall = "SUPPORTED"

    return cap


def iter_required_models(analysis: WorkflowAnalysis) -> Iterable[tuple[str, str]]:
    """Yield (bucket, model_name) for every required model in `analysis`."""
    for bucket, names in (analysis.required_models or {}).items():
        for name in names:
            if name:
                yield bucket, name


__all__ = [
    "CapabilityCheck",
    "CapabilityOverall",
    "check_workflow_capability",
    "embedding_option_available",
    "iter_required_models",
    "model_option_available",
    "reset_object_info_cache",
    "resolve_model_option",
    "rewrite_workflow_model_inputs_to_local_options",
]
