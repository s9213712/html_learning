import copy
from dataclasses import dataclass
from typing import Any, Mapping

from services.comfyui.gguf_profiles import (
    gguf_profile_unavailable_message,
    public_gguf_profiles,
    resolve_official_gguf_selection,
)


GGUF_WORKFLOW_ID = "origin_sdxl_gguf_txt2img"
GGUF_MODEL_NODE_IDS = {"4", "10", "11"}
UNET_NODE_ID = "4"
SAMPLER_NODE_ID = "3"
CLIP_NODE_ID = "10"
VAE_NODE_ID = "11"


class GgufWorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class GgufWorkflowSelection:
    workflow: dict[str, Any]
    user_inputs: dict[str, dict[str, Any]]
    profile: dict[str, Any] | None
    variant: dict[str, Any] | None


def is_gguf_workflow_id(bundle_id: Any) -> bool:
    return str(bundle_id or "").strip() == GGUF_WORKFLOW_ID


def _copy_user_inputs(user_inputs: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    copied: dict[str, dict[str, Any]] = {}
    for node_id, patch in (user_inputs or {}).items():
        if isinstance(patch, Mapping):
            copied[str(node_id)] = dict(patch)
    return copied


def _node(workflow: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    node = workflow.get(node_id)
    if not isinstance(node, dict):
        raise GgufWorkflowError(f"ComfyUI-GGUF workflow 缺少節點 {node_id}，無法套用官方 GGUF profile。")
    inputs = node.setdefault("inputs", {})
    if not isinstance(inputs, dict):
        node["inputs"] = {}
    return node


def _companion_map(profile: Mapping[str, Any]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for companion in profile.get("companions") or []:
        if not isinstance(companion, Mapping):
            continue
        slot = str(companion.get("slot") or "").strip()
        filename = str(companion.get("filename") or "").strip()
        if slot and filename:
            resolved[slot] = filename
    return resolved


def _set_default(user_inputs: dict[str, dict[str, Any]], node_id: str, key: str, value: Any, *, overwrite: bool = True) -> None:
    if value is None:
        return
    patch = user_inputs.setdefault(str(node_id), {})
    if overwrite or key not in patch or patch.get(key) in {None, ""}:
        patch[key] = value


def normalize_gguf_workflow_spec(spec: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(spec, Mapping):
        return {"profile_id": "", "variant_id": ""}
    return {
        "profile_id": str(spec.get("profile_id") or spec.get("profile") or "").strip(),
        "variant_id": str(spec.get("variant_id") or spec.get("variant") or "").strip(),
    }



def _first_node_input(workflow: Mapping[str, Any] | None, node_id: str, *keys: str) -> str:
    node = (workflow or {}).get(str(node_id)) if isinstance(workflow, Mapping) else None
    inputs = node.get("inputs") if isinstance(node, Mapping) and isinstance(node.get("inputs"), Mapping) else {}
    for key in keys:
        value = inputs.get(key)
        if value is not None:
            text = str(value or "").strip()
            if text:
                return text
    return ""


def needs_gguf_workflow_snapshot_repair(workflow: Mapping[str, Any] | None) -> bool:
    if not isinstance(workflow, Mapping):
        return False
    node = workflow.get(UNET_NODE_ID)
    if not isinstance(node, Mapping):
        return False
    class_type = str(node.get("class_type") or "").strip()
    inputs = node.get("inputs") if isinstance(node.get("inputs"), Mapping) else {}
    ckpt_name = str(inputs.get("ckpt_name") or "").strip().lower()
    return class_type == "CheckpointLoaderSimple" and ckpt_name.endswith(".gguf")


def infer_gguf_workflow_spec_from_snapshot(
    workflow: Mapping[str, Any] | None,
    params: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    params = params if isinstance(params, Mapping) else {}
    explicit = normalize_gguf_workflow_spec({
        "profile_id": params.get("gguf_profile") or params.get("diffusers_gguf_profile"),
        "variant_id": params.get("gguf_variant") or params.get("diffusers_gguf_variant"),
    })
    if explicit["profile_id"] or explicit["variant_id"]:
        return explicit
    gguf_file = (
        str(params.get("diffusion_model") or params.get("model") or params.get("diffusers_gguf_file") or "").strip()
        or _first_node_input(workflow, UNET_NODE_ID, "unet_name", "ckpt_name")
    )
    if not gguf_file:
        return {"profile_id": "", "variant_id": ""}
    for profile in public_gguf_profiles():
        for variant in profile.get("variants") or []:
            if str(variant.get("gguf_file") or variant.get("filename") or "").strip() == gguf_file:
                return {
                    "profile_id": str(profile.get("id") or ""),
                    "variant_id": str(variant.get("id") or ""),
                }
    profile, variant = resolve_official_gguf_selection("", "", gguf_file=gguf_file, require_enabled=False)
    return {
        "profile_id": str((profile or {}).get("id") or ""),
        "variant_id": str((variant or {}).get("id") or ""),
    }

def apply_gguf_workflow_profile(
    workflow: Mapping[str, Any],
    user_inputs: Mapping[str, Any] | None,
    spec: Mapping[str, Any] | None,
) -> GgufWorkflowSelection:
    normalized = normalize_gguf_workflow_spec(spec)
    patched_workflow = copy.deepcopy(dict(workflow or {}))
    patched_inputs = _copy_user_inputs(user_inputs)
    profile_id = normalized["profile_id"]
    variant_id = normalized["variant_id"]
    if not profile_id and not variant_id:
        return GgufWorkflowSelection(patched_workflow, patched_inputs, None, None)

    profile, variant = resolve_official_gguf_selection(profile_id, variant_id, require_enabled=False)
    if not profile or not variant:
        raise GgufWorkflowError("GGUF 只允許官方已建檔 profile，請從 SDXL GGUF workflow 模板中的官方 GGUF profile 選單選擇模型與精度。")
    if not profile.get("enabled") or not variant.get("enabled"):
        raise GgufWorkflowError(gguf_profile_unavailable_message(profile, variant))

    unet_node = _node(patched_workflow, UNET_NODE_ID)
    clip_node = _node(patched_workflow, CLIP_NODE_ID)
    vae_node = _node(patched_workflow, VAE_NODE_ID)
    sampler_node = _node(patched_workflow, SAMPLER_NODE_ID)

    companions = _companion_map(profile)
    gguf_file = str(variant.get("gguf_file") or variant.get("filename") or "").strip()
    if not gguf_file:
        raise GgufWorkflowError("官方 GGUF profile 缺少 GGUF 檔名，無法套用。")

    clip_loader_class = str(profile.get("clip_loader_class") or clip_node.get("class_type") or "DualCLIPLoader").strip() or "DualCLIPLoader"
    clip_inputs = clip_node.setdefault("inputs", {})
    if not isinstance(clip_inputs, dict):
        clip_inputs = {}
        clip_node["inputs"] = clip_inputs
    clip_node["class_type"] = clip_loader_class
    clip_inputs["clip_name1"] = companions.get("clip_name1", clip_inputs.get("clip_name1", ""))
    clip_inputs["clip_name2"] = companions.get("clip_name2", clip_inputs.get("clip_name2", ""))
    if companions.get("clip_name3") or clip_loader_class.startswith("TripleCLIPLoader"):
        clip_inputs["clip_name3"] = companions.get("clip_name3", clip_inputs.get("clip_name3", ""))
    if clip_loader_class in {"DualCLIPLoader", "DualCLIPLoaderGGUF"}:
        clip_inputs["type"] = str(profile.get("clip_type") or clip_inputs.get("type") or "sdxl").strip() or "sdxl"
        if clip_loader_class == "DualCLIPLoader":
            clip_inputs["device"] = str(clip_inputs.get("device") or "default").strip() or "default"

    unet_node.setdefault("inputs", {})["unet_name"] = gguf_file
    vae_node.setdefault("inputs", {})["vae_name"] = companions.get("vae_name", vae_node.setdefault("inputs", {}).get("vae_name", ""))

    sampler_defaults = profile.get("sampler_defaults") if isinstance(profile.get("sampler_defaults"), Mapping) else {}
    sampler_inputs = sampler_node.setdefault("inputs", {})
    if not isinstance(sampler_inputs, dict):
        sampler_inputs = {}
        sampler_node["inputs"] = sampler_inputs
    for key in ("sampler_name", "scheduler", "cfg", "steps"):
        if sampler_defaults.get(key) is not None:
            sampler_inputs[key] = sampler_defaults.get(key)
            _set_default(patched_inputs, SAMPLER_NODE_ID, key, sampler_defaults.get(key), overwrite=False)

    _set_default(patched_inputs, UNET_NODE_ID, "unet_name", gguf_file)
    for key in ("clip_name1", "clip_name2", "clip_name3", "type", "device"):
        if clip_inputs.get(key) is not None:
            _set_default(patched_inputs, CLIP_NODE_ID, key, clip_inputs.get(key))
    vae_name = vae_node.setdefault("inputs", {}).get("vae_name")
    if vae_name:
        _set_default(patched_inputs, VAE_NODE_ID, "vae_name", vae_name)

    return GgufWorkflowSelection(patched_workflow, patched_inputs, profile, variant)
