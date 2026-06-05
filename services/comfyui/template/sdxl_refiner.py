import copy
from dataclasses import dataclass
from typing import Any, Mapping


SDXL_REFINER_WORKFLOW_ID = "origin_sdxl_txt2img"
REFINER_NODE_IDS = {"11", "12", "15", "16"}
BASE_SAMPLER_NODE_ID = "10"
BASE_CHECKPOINT_NODE_ID = "4"
DECODE_NODE_ID = "17"


class SdxlRefinerWorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class SdxlRefinerSelection:
    workflow: dict[str, Any]
    user_inputs: dict[str, dict[str, Any]]
    skip_refiner: bool


def is_sdxl_refiner_workflow_id(bundle_id: Any) -> bool:
    return str(bundle_id or "").strip() == SDXL_REFINER_WORKFLOW_ID


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "skip"}


def normalize_sdxl_refiner_spec(spec: Mapping[str, Any] | None) -> dict[str, bool]:
    if not isinstance(spec, Mapping):
        return {"skip_refiner": False}
    return {"skip_refiner": _truthy(spec.get("skip_refiner") or spec.get("skip"))}


def _copy_user_inputs(user_inputs: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    copied: dict[str, dict[str, Any]] = {}
    for node_id, patch in (user_inputs or {}).items():
        if isinstance(patch, Mapping):
            copied[str(node_id)] = dict(patch)
    return copied


def _numeric_step(value: Any, fallback: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = fallback
    return max(1, parsed)


def _node_inputs(workflow: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    node = workflow.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise SdxlRefinerWorkflowError(f"SDXL Text-to-Image workflow 缺少節點 {node_id}，無法跳過 refiner。")
    return node["inputs"]


def apply_sdxl_refiner_option(
    workflow: Mapping[str, Any],
    user_inputs: Mapping[str, Any] | None,
    spec: Mapping[str, Any] | None,
) -> SdxlRefinerSelection:
    normalized = normalize_sdxl_refiner_spec(spec)
    patched_workflow = copy.deepcopy(dict(workflow or {}))
    patched_inputs = _copy_user_inputs(user_inputs)
    if not normalized["skip_refiner"]:
        return SdxlRefinerSelection(
            workflow=patched_workflow,
            user_inputs=patched_inputs,
            skip_refiner=False,
        )

    sampler_inputs = _node_inputs(patched_workflow, BASE_SAMPLER_NODE_ID)
    decoder_inputs = _node_inputs(patched_workflow, DECODE_NODE_ID)
    _node_inputs(patched_workflow, BASE_CHECKPOINT_NODE_ID)

    base_user_inputs = patched_inputs.setdefault(BASE_SAMPLER_NODE_ID, {})
    requested_steps = base_user_inputs.get("steps", sampler_inputs.get("steps"))
    steps = _numeric_step(requested_steps, _numeric_step(sampler_inputs.get("steps"), 25))

    # Refiner mode normally stops the base sampler at a high-noise breakpoint and
    # passes leftover noise to the refiner. Base-only mode must denoise to the
    # final step, otherwise the VAE receives an unfinished latent.
    sampler_inputs["add_noise"] = sampler_inputs.get("add_noise") or "enable"
    sampler_inputs["start_at_step"] = 0
    sampler_inputs["steps"] = steps
    sampler_inputs["end_at_step"] = steps
    sampler_inputs["return_with_leftover_noise"] = "disable"
    decoder_inputs["samples"] = [BASE_SAMPLER_NODE_ID, 0]
    decoder_inputs["vae"] = [BASE_CHECKPOINT_NODE_ID, 2]

    for node_id in REFINER_NODE_IDS:
        patched_workflow.pop(node_id, None)
        patched_inputs.pop(node_id, None)

    base_user_inputs["steps"] = steps
    base_user_inputs["start_at_step"] = 0
    base_user_inputs["end_at_step"] = steps
    base_user_inputs["return_with_leftover_noise"] = "disable"

    return SdxlRefinerSelection(
        workflow=patched_workflow,
        user_inputs=patched_inputs,
        skip_refiner=True,
    )
