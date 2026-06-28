"""§5 Workflow analyzer — input field categorization + structural summary.

Produces a ``WorkflowAnalysis`` from a sanitized ComfyUI API-format workflow JSON.
The analyzer **does not** raise on unknown classes (per §4); it categorizes them
and lets downstream gates decide. It does raise on shape errors that
sanitize_workflow_json() would have caught — but as a defensive double-check,
since the run-time gate (§10 Gate 1) re-runs sanitize before analyze.

Spec reference: docs/comfyui/COMFYUI_TEMPLATE_IMPORTER_PLAN.md §5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from services.comfyui.template.allowlist import (
    CORE_ALLOWLIST,
    CONTROLNET_PREPROCESSOR_ALLOWLIST,
    EXPLICIT_DENYLIST,
    is_allowed_class,
    is_explicitly_denied_class,
)
from services.comfyui.validation.rules import WorkflowValidationError


class FieldCategory(str, Enum):
    TEXT = "TEXT"  # CLIPTextEncode.text, etc.
    IMAGE = "IMAGE"  # LoadImage.image, LoadImageMask.image
    VIDEO = "VIDEO"  # LoadVideo.file
    MODEL = "MODEL"  # ckpt_name / vae_name / lora_name / control_net_name / model_name
    NUMERIC = "NUMERIC"  # KSampler seed / steps / cfg / denoise / etc.
    BOOLEAN = "BOOLEAN"  # on/off node options
    SAMPLER = "SAMPLER"  # KSampler.sampler_name / scheduler (enum)
    UNKNOWN = "UNKNOWN"  # not classifiable, leave for caller


# (class_type, input_name) → category
_FIELD_CATEGORY_TABLE: dict[tuple[str, str], FieldCategory] = {
    # Text
    ("CLIPTextEncode", "text"): FieldCategory.TEXT,
    ("CLIPTextEncodeFlux", "text"): FieldCategory.TEXT,
    ("TextEncodeQwenImageEditPlus", "prompt"): FieldCategory.TEXT,
    ("TextEncodeQwenImageEditPlusCustom_lrzjason", "prompt"): FieldCategory.TEXT,
    ("CR Text", "text"): FieldCategory.TEXT,
    ("CR Prompt Text", "prompt"): FieldCategory.TEXT,
    # Image / Mask
    ("LoadImage", "image"): FieldCategory.IMAGE,
    ("LoadImageMask", "image"): FieldCategory.IMAGE,
    ("LoadImageMask", "channel"): FieldCategory.TEXT,  # alpha/red/green/blue enum
    # Video
    ("LoadVideo", "file"): FieldCategory.VIDEO,
    # Models
    ("CheckpointLoaderSimple", "ckpt_name"): FieldCategory.MODEL,
    ("VAELoader", "vae_name"): FieldCategory.MODEL,
    ("CLIPLoader", "clip_name"): FieldCategory.MODEL,
    ("DualCLIPLoader", "clip_name1"): FieldCategory.MODEL,
    ("DualCLIPLoader", "clip_name2"): FieldCategory.MODEL,
    ("TripleCLIPLoader", "clip_name1"): FieldCategory.MODEL,
    ("TripleCLIPLoader", "clip_name2"): FieldCategory.MODEL,
    ("TripleCLIPLoader", "clip_name3"): FieldCategory.MODEL,
    ("CLIPLoaderGGUF", "clip_name"): FieldCategory.MODEL,
    ("DualCLIPLoaderGGUF", "clip_name1"): FieldCategory.MODEL,
    ("DualCLIPLoaderGGUF", "clip_name2"): FieldCategory.MODEL,
    ("TripleCLIPLoaderGGUF", "clip_name1"): FieldCategory.MODEL,
    ("TripleCLIPLoaderGGUF", "clip_name2"): FieldCategory.MODEL,
    ("TripleCLIPLoaderGGUF", "clip_name3"): FieldCategory.MODEL,
    ("CLIPVisionLoader", "clip_name"): FieldCategory.MODEL,
    ("UNETLoader", "unet_name"): FieldCategory.MODEL,
    ("UnetLoaderGGUF", "unet_name"): FieldCategory.MODEL,
    ("UnetLoaderGGUFAdvanced", "unet_name"): FieldCategory.MODEL,
    ("LoraLoader", "lora_name"): FieldCategory.MODEL,
    ("LoraLoaderModelOnly", "lora_name"): FieldCategory.MODEL,
    ("ControlNetLoader", "control_net_name"): FieldCategory.MODEL,
    ("UpscaleModelLoader", "model_name"): FieldCategory.MODEL,
    ("LatentUpscaleModelLoader", "model_name"): FieldCategory.MODEL,
    ("LTXVAudioVAELoader", "ckpt_name"): FieldCategory.MODEL,
    ("LTXAVTextEncoderLoader", "ckpt_name"): FieldCategory.MODEL,
    ("ByteDanceSeedreamNode", "model"): FieldCategory.MODEL,
    ("GrokImageEditNode", "model"): FieldCategory.MODEL,
    ("Qwen3_VQA", "model"): FieldCategory.MODEL,
    # Numeric — KSampler
    ("KSampler", "seed"): FieldCategory.NUMERIC,
    ("KSampler", "steps"): FieldCategory.NUMERIC,
    ("KSampler", "cfg"): FieldCategory.NUMERIC,
    ("KSampler", "denoise"): FieldCategory.NUMERIC,
    ("KSampler", "control_after_generate"): FieldCategory.SAMPLER,
    # Sampler enum
    ("KSampler", "sampler_name"): FieldCategory.SAMPLER,
    ("KSampler", "scheduler"): FieldCategory.SAMPLER,
    ("KSamplerSelect", "sampler_name"): FieldCategory.SAMPLER,
    ("BasicScheduler", "scheduler"): FieldCategory.SAMPLER,
    ("BasicScheduler", "steps"): FieldCategory.NUMERIC,
    ("BasicScheduler", "denoise"): FieldCategory.NUMERIC,
    ("RandomNoise", "noise_seed"): FieldCategory.NUMERIC,
    ("FluxGuidance", "guidance"): FieldCategory.NUMERIC,
    ("Flux2Scheduler", "steps"): FieldCategory.NUMERIC,
    ("Flux2Scheduler", "width"): FieldCategory.NUMERIC,
    ("Flux2Scheduler", "height"): FieldCategory.NUMERIC,
    ("WanImageToVideo", "width"): FieldCategory.NUMERIC,
    ("WanImageToVideo", "height"): FieldCategory.NUMERIC,
    ("WanImageToVideo", "length"): FieldCategory.NUMERIC,
    ("WanImageToVideo", "batch_size"): FieldCategory.NUMERIC,
    ("WanVaceToVideo", "width"): FieldCategory.NUMERIC,
    ("WanVaceToVideo", "height"): FieldCategory.NUMERIC,
    ("WanVaceToVideo", "length"): FieldCategory.NUMERIC,
    ("WanVaceToVideo", "batch_size"): FieldCategory.NUMERIC,
    ("WanVaceToVideo", "strength"): FieldCategory.NUMERIC,
    ("HunyuanVideo15ImageToVideo", "width"): FieldCategory.NUMERIC,
    ("HunyuanVideo15ImageToVideo", "height"): FieldCategory.NUMERIC,
    ("HunyuanVideo15ImageToVideo", "length"): FieldCategory.NUMERIC,
    ("HunyuanVideo15ImageToVideo", "batch_size"): FieldCategory.NUMERIC,
    ("CreateVideo", "fps"): FieldCategory.NUMERIC,
    ("ImageScaleToTotalPixels", "megapixels"): FieldCategory.NUMERIC,
    ("ImageScaleToTotalPixels", "divisible_by"): FieldCategory.NUMERIC,
    ("ResizeImageMaskNode", "resize_type.longer_size"): FieldCategory.NUMERIC,
    ("ResizeImageMaskNode", "resize_type.shorter_size"): FieldCategory.NUMERIC,
    ("ResizeImageMaskNode", "resize_type.width"): FieldCategory.NUMERIC,
    ("ResizeImageMaskNode", "resize_type.height"): FieldCategory.NUMERIC,
    ("ResizeImageMaskNode", "resize_type.megapixels"): FieldCategory.NUMERIC,
    ("ResizeImageMaskNode", "resize_type.multiplier"): FieldCategory.NUMERIC,
    ("ResizeImageMaskNode", "resize_type.multiple"): FieldCategory.NUMERIC,
    ("ImageBlend", "blend_factor"): FieldCategory.NUMERIC,
    ("LatentUpscaleBy", "scale_by"): FieldCategory.NUMERIC,
    ("EmptyAceStep1.5LatentAudio", "seconds"): FieldCategory.NUMERIC,
    ("SDPoseKeypointExtractor", "batch_size"): FieldCategory.NUMERIC,
    ("SDPoseDrawKeypoints", "stick_width"): FieldCategory.NUMERIC,
    ("SDPoseDrawKeypoints", "face_point_size"): FieldCategory.NUMERIC,
    ("SDPoseDrawKeypoints", "score_threshold"): FieldCategory.NUMERIC,
    ("RTDETR_detect", "threshold"): FieldCategory.NUMERIC,
    ("RTDETR_detect", "max_detections"): FieldCategory.NUMERIC,
    ("TextEncodeAceStepAudio1.5", "seed"): FieldCategory.NUMERIC,
    ("TextEncodeAceStepAudio1.5", "duration"): FieldCategory.NUMERIC,
    ("TextEncodeAceStepAudio1.5", "bpm"): FieldCategory.NUMERIC,
    ("TextEncodeAceStepAudio1.5", "cfg_scale"): FieldCategory.NUMERIC,
    ("TextEncodeAceStepAudio1.5", "temperature"): FieldCategory.NUMERIC,
    ("TextEncodeAceStepAudio1.5", "top_p"): FieldCategory.NUMERIC,
    ("TextEncodeAceStepAudio1.5", "top_k"): FieldCategory.NUMERIC,
    ("TextEncodeAceStepAudio1.5", "min_p"): FieldCategory.NUMERIC,
    ("ByteDanceSeedreamNode", "width"): FieldCategory.NUMERIC,
    ("ByteDanceSeedreamNode", "height"): FieldCategory.NUMERIC,
    ("ByteDanceSeedreamNode", "max_images"): FieldCategory.NUMERIC,
    ("ByteDanceSeedreamNode", "seed"): FieldCategory.NUMERIC,
    ("GrokImageEditNode", "number_of_images"): FieldCategory.NUMERIC,
    ("GrokImageEditNode", "seed"): FieldCategory.NUMERIC,
    # Numeric / enum — KSamplerAdvanced
    ("KSamplerAdvanced", "noise_seed"): FieldCategory.NUMERIC,
    ("KSamplerAdvanced", "steps"): FieldCategory.NUMERIC,
    ("KSamplerAdvanced", "cfg"): FieldCategory.NUMERIC,
    ("KSamplerAdvanced", "start_at_step"): FieldCategory.NUMERIC,
    ("KSamplerAdvanced", "end_at_step"): FieldCategory.NUMERIC,
    ("KSamplerAdvanced", "add_noise"): FieldCategory.SAMPLER,
    ("KSamplerAdvanced", "sampler_name"): FieldCategory.SAMPLER,
    ("KSamplerAdvanced", "scheduler"): FieldCategory.SAMPLER,
    ("KSamplerAdvanced", "return_with_leftover_noise"): FieldCategory.SAMPLER,
    ("KSamplerAdvanced", "control_after_generate"): FieldCategory.SAMPLER,
    # Numeric — LoraLoader strengths
    ("LoraLoader", "strength_model"): FieldCategory.NUMERIC,
    ("LoraLoader", "strength_clip"): FieldCategory.NUMERIC,
    ("LoraLoaderModelOnly", "strength_model"): FieldCategory.NUMERIC,
    # Numeric — ControlNet apply
    ("ControlNetApplyAdvanced", "strength"): FieldCategory.NUMERIC,
    ("ControlNetApplyAdvanced", "start_percent"): FieldCategory.NUMERIC,
    ("ControlNetApplyAdvanced", "end_percent"): FieldCategory.NUMERIC,
    # Numeric — Latent / Outpaint
    ("EmptyLatentImage", "width"): FieldCategory.NUMERIC,
    ("EmptyLatentImage", "height"): FieldCategory.NUMERIC,
    ("EmptyLatentImage", "batch_size"): FieldCategory.NUMERIC,
    ("EmptySD3LatentImage", "width"): FieldCategory.NUMERIC,
    ("EmptySD3LatentImage", "height"): FieldCategory.NUMERIC,
    ("EmptySD3LatentImage", "batch_size"): FieldCategory.NUMERIC,
    ("EmptyFlux2LatentImage", "width"): FieldCategory.NUMERIC,
    ("EmptyFlux2LatentImage", "height"): FieldCategory.NUMERIC,
    ("EmptyFlux2LatentImage", "batch_size"): FieldCategory.NUMERIC,
    ("ModelSamplingSD3", "shift"): FieldCategory.NUMERIC,
    ("ModelSamplingAuraFlow", "shift"): FieldCategory.NUMERIC,
    ("ImagePadForOutpaint", "left"): FieldCategory.NUMERIC,
    ("ImagePadForOutpaint", "top"): FieldCategory.NUMERIC,
    ("ImagePadForOutpaint", "right"): FieldCategory.NUMERIC,
    ("ImagePadForOutpaint", "bottom"): FieldCategory.NUMERIC,
    ("ImagePadForOutpaint", "feathering"): FieldCategory.NUMERIC,
    # Save filename — text but not user-editable in UI (overwritten by §7.2)
    ("SaveImage", "filename_prefix"): FieldCategory.TEXT,
    ("SaveVideo", "filename_prefix"): FieldCategory.TEXT,
    ("SaveAudio", "filename_prefix"): FieldCategory.TEXT,
    ("SaveAudioMP3", "filename_prefix"): FieldCategory.TEXT,
    ("ByteDanceSeedreamNode", "prompt"): FieldCategory.TEXT,
    ("GrokImageEditNode", "prompt"): FieldCategory.TEXT,
    ("StringConcatenate", "string_a"): FieldCategory.TEXT,
    ("StringConcatenate", "string_b"): FieldCategory.TEXT,
    ("TextEncodeAceStepAudio1.5", "tags"): FieldCategory.TEXT,
    ("TextEncodeAceStepAudio1.5", "lyrics"): FieldCategory.TEXT,
    # Sampler / enum-ish official nodes
    ("ImageScaleToTotalPixels", "upscale_method"): FieldCategory.SAMPLER,
    ("LatentUpscaleBy", "upscale_method"): FieldCategory.SAMPLER,
    ("TextEncodeAceStepAudio1.5", "timesignature"): FieldCategory.SAMPLER,
    ("TextEncodeAceStepAudio1.5", "language"): FieldCategory.SAMPLER,
    ("TextEncodeAceStepAudio1.5", "keyscale"): FieldCategory.SAMPLER,
    ("TextEncodeAceStepAudio1.5", "generate_audio_codes"): FieldCategory.SAMPLER,
    ("ByteDanceSeedreamNode", "size_preset"): FieldCategory.SAMPLER,
    ("ByteDanceSeedreamNode", "sequential_image_generation"): FieldCategory.SAMPLER,
    ("ByteDanceSeedreamNode", "watermark"): FieldCategory.SAMPLER,
    ("ByteDanceSeedreamNode", "fail_on_partial"): FieldCategory.SAMPLER,
    ("GrokImageEditNode", "resolution"): FieldCategory.SAMPLER,
    ("GrokImageEditNode", "aspect_ratio"): FieldCategory.SAMPLER,
    ("ComfySwitchNode", "switch"): FieldCategory.BOOLEAN,
    ("ResizeImageMaskNode", "resize_type"): FieldCategory.SAMPLER,
    ("ResizeImageMaskNode", "scale_method"): FieldCategory.SAMPLER,
    ("ImageBlend", "blend_mode"): FieldCategory.SAMPLER,
    ("RTDETR_detect", "class_name"): FieldCategory.TEXT,
    ("SDPoseDrawKeypoints", "draw_body"): FieldCategory.BOOLEAN,
    ("SDPoseDrawKeypoints", "draw_hands"): FieldCategory.BOOLEAN,
    ("SDPoseDrawKeypoints", "draw_face"): FieldCategory.BOOLEAN,
    ("SDPoseDrawKeypoints", "draw_feet"): FieldCategory.BOOLEAN,
    # VAEEncodeForInpaint mask grow (numeric)
    ("VAEEncodeForInpaint", "grow_mask_by"): FieldCategory.NUMERIC,
}


def classify_input_field(class_type: str, input_name: str) -> FieldCategory:
    """Map (class_type, input_name) to a FieldCategory; UNKNOWN if unmapped."""
    key = (class_type or "", input_name or "")
    return _FIELD_CATEGORY_TABLE.get(key, FieldCategory.UNKNOWN)


@dataclass(frozen=True)
class InputField:
    """One input slot on a node.

    `is_link=True` when the workflow JSON encodes this input as
    [source_node_id, output_index] (an internal graph wire); user-editable
    inputs are scalar values (str / int / float / bool).
    """

    node_id: str
    class_type: str
    input_name: str
    raw_value: Any
    category: FieldCategory
    is_link: bool
    node_title: str = ""


@dataclass
class NodeAnalysis:
    node_id: str
    class_type: str
    title: str = ""
    inputs: list[InputField] = field(default_factory=list)
    is_allowed: bool = False
    is_explicitly_denied: bool = False
    is_unknown: bool = False  # not in allowlist *and* not in denylist


@dataclass
class WorkflowAnalysis:
    nodes: list[NodeAnalysis] = field(default_factory=list)
    class_types: set[str] = field(default_factory=set)
    allowed_classes: set[str] = field(default_factory=set)
    denied_classes: set[str] = field(default_factory=set)
    unknown_classes: set[str] = field(default_factory=set)
    user_inputs: list[InputField] = field(default_factory=list)
    # Quick lookup: required model files keyed by category, e.g. {"ckpt": ["v1-5.safetensors"], ...}
    required_models: dict[str, list[str]] = field(default_factory=dict)

    def has_blocking_classes(self) -> bool:
        """True iff any class is explicitly denied — preview-stage hard fail."""
        return bool(self.denied_classes)


# Map MODEL input field names to a category bucket used in `required_models`.
_MODEL_CATEGORY_BUCKETS = {
    "ckpt_name": "ckpt",
    "vae_name": "vae",
    "lora_name": "lora",
    "unet_name": "diffusion_model",
    "control_net_name": "controlnet",
    "model_name": "upscale_model",
    "clip_name": "clip",
    "clip_name1": "clip",
    "clip_name2": "clip",
    "clip_name3": "clip",
    "model": "api_model",
}

_MODEL_CLASS_INPUT_BUCKETS = {
    ("CLIPVisionLoader", "clip_name"): "clip_vision",
    ("LatentUpscaleModelLoader", "model_name"): "latent_upscale_model",
}

_EMBEDDING_TAG_RE = re.compile(r"<\s*embeddings?\s*:\s*([^<>]+?)\s*>", re.IGNORECASE)
_EMBEDDING_PREFIX_RE = re.compile(r"(?<![\w/])embedding:", re.IGNORECASE)
_EMBEDDING_PREFIX_STOP_RE = re.compile(r"[,;<>\r\n]")
_EMBEDDING_FILE_EXT_RE = re.compile(r"\.(?:safetensors|pt|pth|bin)\b", re.IGNORECASE)


def _clean_embedding_name(name: str) -> str:
    return str(name or "").strip().strip(".,;")


def _extract_prefixed_embedding_names(text: str) -> list[str]:
    source = str(text or "")
    matches = list(_EMBEDDING_PREFIX_RE.finditer(source))
    names: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        stop = _EMBEDDING_PREFIX_STOP_RE.search(source, start, next_start)
        end = stop.start() if stop else next_start
        chunk = source[start:end].strip()
        if not chunk:
            continue
        ext_match = _EMBEDDING_FILE_EXT_RE.search(chunk)
        if ext_match:
            name = chunk[:ext_match.end()]
        elif "/" in chunk or "\\" in chunk:
            name = chunk
        else:
            name = chunk.split(maxsplit=1)[0]
        name = _clean_embedding_name(name)
        if name:
            names.append(name)
    return names


def _extract_embedding_names(text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    tag_names = [_clean_embedding_name(match.group(1)) for match in _EMBEDDING_TAG_RE.finditer(str(text or ""))]
    for name in tag_names + _extract_prefixed_embedding_names(text):
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def analyze_workflow_json(workflow: dict[str, Any]) -> WorkflowAnalysis:
    """Walk a sanitized API-format workflow and produce structural analysis.

    Raises:
      WorkflowValidationError: if the top-level shape is not a dict-of-nodes
        or a node entry is malformed. (Sanitize should have caught these,
        but we double-check at run time per §10 Gate 1.)
    """
    if not isinstance(workflow, dict) or not workflow:
        raise WorkflowValidationError("workflow 必須是非空的 ComfyUI API-format 物件")

    analysis = WorkflowAnalysis()
    required_models: dict[str, list[str]] = {}

    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not node_id.strip():
            raise WorkflowValidationError(
                f"workflow node id 必須是非空字串：'{node_id}' 不合法"
            )
        if not isinstance(node, dict):
            raise WorkflowValidationError(
                f"workflow node {node_id} 必須是物件，目前型別為 {type(node).__name__}"
            )
        class_type = str(node.get("class_type") or "").strip()
        if not class_type:
            raise WorkflowValidationError(
                f"workflow node {node_id} 缺少 class_type 欄位"
            )

        # Reject explicit non-dict (e.g., list) before the `or {}` coerce path,
        # otherwise an `inputs=[]` body would be silently treated as no-inputs.
        if "inputs" in node and node["inputs"] is not None and not isinstance(node["inputs"], dict):
            raise WorkflowValidationError(
                f"workflow node {node_id}.inputs 必須是物件，目前型別為 "
                f"{type(node['inputs']).__name__}"
            )
        inputs_raw = node.get("inputs") or {}

        node_meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        node_title = str(
            node_meta.get("title") or node_meta.get("group_title") or node.get("title") or ""
        ).strip()
        node_analysis = NodeAnalysis(node_id=node_id, class_type=class_type, title=node_title)
        node_analysis.is_allowed = is_allowed_class(class_type)
        node_analysis.is_explicitly_denied = is_explicitly_denied_class(class_type)
        node_analysis.is_unknown = not (
            node_analysis.is_allowed or node_analysis.is_explicitly_denied
        )

        for input_name, raw_value in inputs_raw.items():
            is_link = isinstance(raw_value, list) and len(raw_value) == 2 and isinstance(
                raw_value[0], (str, int)
            )
            field_obj = InputField(
                node_id=node_id,
                class_type=class_type,
                input_name=str(input_name),
                raw_value=raw_value,
                category=classify_input_field(class_type, str(input_name)),
                is_link=is_link,
                node_title=node_title,
            )
            node_analysis.inputs.append(field_obj)
            if not is_link:
                analysis.user_inputs.append(field_obj)
            if (
                field_obj.category == FieldCategory.MODEL
                and not is_link
                and isinstance(raw_value, str)
                and raw_value
            ):
                bucket = _MODEL_CLASS_INPUT_BUCKETS.get(
                    (field_obj.class_type, field_obj.input_name),
                    _MODEL_CATEGORY_BUCKETS.get(input_name, "model"),
                )
                required_models.setdefault(bucket, []).append(raw_value)
            if field_obj.category == FieldCategory.TEXT and not is_link and isinstance(raw_value, str):
                embeddings = _extract_embedding_names(raw_value)
                if embeddings:
                    required_models.setdefault("embedding", []).extend(embeddings)

        analysis.nodes.append(node_analysis)
        analysis.class_types.add(class_type)
        if node_analysis.is_explicitly_denied:
            analysis.denied_classes.add(class_type)
        elif node_analysis.is_allowed:
            analysis.allowed_classes.add(class_type)
        else:
            analysis.unknown_classes.add(class_type)

    # Dedup per bucket while preserving first-seen order
    analysis.required_models = {
        bucket: sorted(set(names)) for bucket, names in required_models.items()
    }
    return analysis


__all__ = [
    "FieldCategory",
    "InputField",
    "NodeAnalysis",
    "WorkflowAnalysis",
    "analyze_workflow_json",
    "classify_input_field",
]
