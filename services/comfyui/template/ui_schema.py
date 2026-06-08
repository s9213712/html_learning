"""§9 UI Schema — derive a 6-panel layout from a WorkflowAnalysis.

Frontend renders one section per panel; each panel surfaces only the
user-editable inputs of its category. Image inputs are gathered onto the
``image`` panel as upload slots. The compatibility panel is read-only and
gets populated separately from the CapabilityCheck result.

Spec reference: docs/comfyui/COMFYUI_TEMPLATE_IMPORTER_PLAN.md §9.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from services.comfyui.template.analyzer import (
    FieldCategory,
    InputField,
    WorkflowAnalysis,
)
from services.comfyui.template.capability import CapabilityCheck


# ----------------------------------------------------------------------------
# Panel field shape
# ----------------------------------------------------------------------------


def _field_id(field_obj: InputField) -> str:
    return f"node:{field_obj.node_id}:{field_obj.input_name}"


def _input_type_for_category(category: FieldCategory) -> str:
    return {
        FieldCategory.TEXT: "textarea",
        FieldCategory.IMAGE: "file_picker",
        FieldCategory.VIDEO: "video_file_picker",
        FieldCategory.MODEL: "select",
        FieldCategory.NUMERIC: "number",
        FieldCategory.BOOLEAN: "checkbox",
        FieldCategory.SAMPLER: "select",
        FieldCategory.UNKNOWN: "textarea",
    }[category]


# Default UI hints per (class_type, input_name); merged into per-field
# constraints. Values are advisory — caller may override based on capability
# check results (e.g., narrow `options` on `ckpt_name` to local files only).
_FIELD_CONSTRAINT_HINTS: dict[tuple[str, str], dict[str, Any]] = {
    ("CLIPTextEncode", "text"): {"max_length": 2000, "rows": 4},
    ("TextEncodeQwenImageEditPlus", "prompt"): {"max_length": 4000, "rows": 5},
    ("TextEncodeQwenImageEditPlusCustom_lrzjason", "prompt"): {"max_length": 4000, "rows": 5},
    ("CR Text", "text"): {"max_length": 4000, "rows": 5},
    ("CR Prompt Text", "prompt"): {"max_length": 4000, "rows": 5},
    ("ByteDanceSeedreamNode", "prompt"): {"max_length": 4000, "rows": 5},
    ("GrokImageEditNode", "prompt"): {"max_length": 4000, "rows": 5},
    ("StringConcatenate", "string_a"): {"max_length": 3000, "rows": 3},
    ("StringConcatenate", "string_b"): {"max_length": 3000, "rows": 5},
    ("TextEncodeAceStepAudio1.5", "tags"): {"max_length": 2000, "rows": 4},
    ("TextEncodeAceStepAudio1.5", "lyrics"): {"max_length": 5000, "rows": 8},
    ("KSampler", "seed"): {"min": 0, "max": 2 ** 53, "step": 1},
    ("KSampler", "steps"): {"min": 1, "max": 150, "step": 1},
    ("KSampler", "cfg"): {"min": 0.5, "max": 30.0, "step": 0.1},
    ("KSampler", "denoise"): {"min": 0.0, "max": 1.0, "step": 0.05},
    ("KSamplerAdvanced", "noise_seed"): {"min": 0, "max": 2 ** 53, "step": 1},
    ("KSamplerAdvanced", "steps"): {"min": 1, "max": 150, "step": 1},
    ("KSamplerAdvanced", "cfg"): {"min": 0.5, "max": 30.0, "step": 0.1},
    ("KSamplerAdvanced", "start_at_step"): {"min": 0, "max": 150, "step": 1},
    ("KSamplerAdvanced", "end_at_step"): {"min": 0, "max": 150, "step": 1},
    ("LoraLoader", "strength_model"): {"min": -2.0, "max": 2.0, "step": 0.05},
    ("LoraLoader", "strength_clip"): {"min": -2.0, "max": 2.0, "step": 0.05},
    ("LoraLoaderModelOnly", "strength_model"): {"min": -2.0, "max": 2.0, "step": 0.05},
    ("ControlNetApplyAdvanced", "strength"): {"min": 0.0, "max": 2.0, "step": 0.05},
    ("ControlNetApplyAdvanced", "start_percent"): {"min": 0.0, "max": 1.0, "step": 0.05},
    ("ControlNetApplyAdvanced", "end_percent"): {"min": 0.0, "max": 1.0, "step": 0.05},
    ("EmptyLatentImage", "width"): {"min": 64, "max": 4096, "step": 8},
    ("EmptyLatentImage", "height"): {"min": 64, "max": 4096, "step": 8},
    ("EmptyLatentImage", "batch_size"): {"min": 1, "max": 16, "step": 1},
    ("EmptySD3LatentImage", "width"): {"min": 64, "max": 4096, "step": 8},
    ("EmptySD3LatentImage", "height"): {"min": 64, "max": 4096, "step": 8},
    ("EmptySD3LatentImage", "batch_size"): {"min": 1, "max": 16, "step": 1},
    ("EmptyFlux2LatentImage", "width"): {"min": 64, "max": 4096, "step": 8},
    ("EmptyFlux2LatentImage", "height"): {"min": 64, "max": 4096, "step": 8},
    ("EmptyFlux2LatentImage", "batch_size"): {"min": 1, "max": 16, "step": 1},
    ("ModelSamplingSD3", "shift"): {"min": 0.0, "max": 20.0, "step": 0.1},
    ("ModelSamplingAuraFlow", "shift"): {"min": 0.0, "max": 20.0, "step": 0.1},
    ("Flux2Scheduler", "steps"): {"min": 1, "max": 150, "step": 1},
    ("Flux2Scheduler", "width"): {"min": 64, "max": 4096, "step": 8},
    ("Flux2Scheduler", "height"): {"min": 64, "max": 4096, "step": 8},
    ("CreateVideo", "fps"): {"min": 1, "max": 120, "step": 1},
    ("WanVaceToVideo", "width"): {"min": 64, "max": 4096, "step": 8},
    ("WanVaceToVideo", "height"): {"min": 64, "max": 4096, "step": 8},
    ("WanVaceToVideo", "length"): {"min": 1, "max": 512, "step": 1},
    ("WanVaceToVideo", "batch_size"): {"min": 1, "max": 16, "step": 1},
    ("WanVaceToVideo", "strength"): {"min": 0.0, "max": 2.0, "step": 0.05},
    ("HunyuanVideo15ImageToVideo", "width"): {"min": 64, "max": 4096, "step": 8},
    ("HunyuanVideo15ImageToVideo", "height"): {"min": 64, "max": 4096, "step": 8},
    ("HunyuanVideo15ImageToVideo", "length"): {"min": 1, "max": 512, "step": 1},
    ("HunyuanVideo15ImageToVideo", "batch_size"): {"min": 1, "max": 16, "step": 1},
    ("ImageScaleToTotalPixels", "megapixels"): {"min": 0.1, "max": 64.0, "step": 0.1},
    ("ImageScaleToTotalPixels", "divisible_by"): {"min": 1, "max": 256, "step": 1},
    ("ResizeImageMaskNode", "resize_type"): {
        "options": [
            "scale dimensions",
            "scale total pixels",
            "scale longer dimension",
            "scale shorter dimension",
            "scale by multiplier",
            "scale width",
            "scale height",
            "scale to multiple",
        ]
    },
    ("ResizeImageMaskNode", "scale_method"): {"options": ["nearest-exact", "bilinear", "area", "bicubic", "lanczos"]},
    ("ResizeImageMaskNode", "resize_type.longer_size"): {"min": 16, "max": 8192, "step": 8},
    ("ResizeImageMaskNode", "resize_type.shorter_size"): {"min": 16, "max": 8192, "step": 8},
    ("ResizeImageMaskNode", "resize_type.width"): {"min": 16, "max": 8192, "step": 8},
    ("ResizeImageMaskNode", "resize_type.height"): {"min": 16, "max": 8192, "step": 8},
    ("ResizeImageMaskNode", "resize_type.megapixels"): {"min": 0.1, "max": 64.0, "step": 0.1},
    ("ResizeImageMaskNode", "resize_type.multiplier"): {"min": 0.05, "max": 16.0, "step": 0.05},
    ("ResizeImageMaskNode", "resize_type.multiple"): {"min": 1, "max": 512, "step": 1},
    ("ImageBlend", "blend_factor"): {"min": 0.0, "max": 1.0, "step": 0.05},
    ("ImageBlend", "blend_mode"): {"options": ["normal", "multiply", "screen", "overlay", "soft_light", "difference"]},
    ("LatentUpscaleBy", "scale_by"): {"min": 1.0, "max": 8.0, "step": 0.1},
    ("SDPoseKeypointExtractor", "batch_size"): {"min": 1, "max": 64, "step": 1},
    ("SDPoseDrawKeypoints", "stick_width"): {"min": 1, "max": 64, "step": 1},
    ("SDPoseDrawKeypoints", "face_point_size"): {"min": 1, "max": 32, "step": 1},
    ("SDPoseDrawKeypoints", "score_threshold"): {"min": 0.0, "max": 1.0, "step": 0.05},
    ("RTDETR_detect", "threshold"): {"min": 0.0, "max": 1.0, "step": 0.05},
    ("RTDETR_detect", "max_detections"): {"min": 1, "max": 100, "step": 1},
    ("TextEncodeAceStepAudio1.5", "duration"): {"min": 1, "max": 600, "step": 1},
    ("TextEncodeAceStepAudio1.5", "bpm"): {"min": 1, "max": 300, "step": 1},
    ("TextEncodeAceStepAudio1.5", "cfg_scale"): {"min": 0.0, "max": 20.0, "step": 0.1},
    ("TextEncodeAceStepAudio1.5", "temperature"): {"min": 0.0, "max": 2.0, "step": 0.01},
    ("TextEncodeAceStepAudio1.5", "top_p"): {"min": 0.0, "max": 1.0, "step": 0.01},
    ("TextEncodeAceStepAudio1.5", "top_k"): {"min": 0, "max": 1000, "step": 1},
    ("TextEncodeAceStepAudio1.5", "min_p"): {"min": 0.0, "max": 1.0, "step": 0.01},
    ("EmptyAceStep1.5LatentAudio", "seconds"): {"min": 1, "max": 600, "step": 1},
    ("ByteDanceSeedreamNode", "width"): {"min": 256, "max": 4096, "step": 64},
    ("ByteDanceSeedreamNode", "height"): {"min": 256, "max": 4096, "step": 64},
    ("ByteDanceSeedreamNode", "max_images"): {"min": 1, "max": 8, "step": 1},
    ("GrokImageEditNode", "number_of_images"): {"min": 1, "max": 8, "step": 1},
    ("ImagePadForOutpaint", "left"): {"min": 0, "max": 4096, "step": 8},
    ("ImagePadForOutpaint", "top"): {"min": 0, "max": 4096, "step": 8},
    ("ImagePadForOutpaint", "right"): {"min": 0, "max": 4096, "step": 8},
    ("ImagePadForOutpaint", "bottom"): {"min": 0, "max": 4096, "step": 8},
    ("ImagePadForOutpaint", "feathering"): {"min": 0, "max": 256, "step": 1},
    ("VAEEncodeForInpaint", "grow_mask_by"): {"min": 0, "max": 64, "step": 1},
    ("LoadImage", "image"): {"accept_mime": ["image/png", "image/jpeg", "image/webp"]},
    ("LoadImageMask", "image"): {"accept_mime": ["image/png", "image/jpeg", "image/webp"]},
    ("LoadImageMask", "channel"): {"options": ["alpha", "red", "green", "blue"]},
    ("LoadVideo", "file"): {"accept_mime": ["video/mp4", "video/webm", "video/quicktime", "video/x-matroska"]},
}


_GENERIC_NODE_TITLES = {
    "",
    "load model",
    "model loader",
    "load checkpoint",
    "載入模型",
    "模型載入",
    "文字輸入",
    "text input",
    "prompt",
}


def _clean_title(title: str) -> str:
    text = str(title or "").strip()
    return "" if text.lower() in _GENERIC_NODE_TITLES else text


def _prompt_role_label(field_obj: InputField, label_context: dict[str, Any] | None) -> str | None:
    roles = set((label_context or {}).get("prompt_roles", {}).get(field_obj.node_id, []))
    title = f"{field_obj.node_title} {field_obj.raw_value}".lower()
    if "positive" in roles and "negative" in roles:
        return "正負共用提示詞"
    if "negative" in roles or "負" in title or "negative" in title or "neg" in title:
        return "負面提示詞"
    if "positive" in roles or "正" in title or "positive" in title or "pos" in title:
        return "正向提示詞"
    if any(token in title for token in ("low quality", "worst quality", "bad anatomy", "blurry", "deformed")):
        return "負面提示詞"
    return None


def _model_noise_label(field_obj: InputField) -> str:
    text = f"{field_obj.node_title} {field_obj.raw_value}".lower()
    if "high_noise" in text or "high noise" in text or "高噪" in text:
        return "High Noise"
    if "low_noise" in text or "low noise" in text or "低噪" in text:
        return "Low Noise"
    return ""


def _model_label_with_role(field_obj: InputField, base_label: str) -> str:
    role = _model_noise_label(field_obj)
    title = _clean_title(field_obj.node_title)
    if role:
        return f"{base_label}（{role}）"
    if title and title not in base_label:
        return f"{base_label}（{title}）"
    return base_label


_STAGE_AWARE_LABEL_CLASSES = {
    "ImageScaleToTotalPixels",
    "LatentUpscaleBy",
}


def _label_with_stage(field_obj: InputField, base_label: str) -> str:
    title = _clean_title(field_obj.node_title)
    if title and title not in base_label:
        return f"{base_label}（{title}）"
    return base_label


def _label_zh(field_obj: InputField, label_context: dict[str, Any] | None = None) -> str:
    """Best-effort 繁中 label; falls back to the raw input name."""
    prompt_role = _prompt_role_label(field_obj, label_context)
    if prompt_role and (
        (
            field_obj.class_type in {"CLIPTextEncode", "CLIPTextEncodeFlux", "CR Text"}
            and field_obj.input_name == "text"
        )
        or (
            field_obj.class_type in {
                "TextEncodeQwenImageEditPlus",
                "TextEncodeQwenImageEditPlusCustom_lrzjason",
                "CR Prompt Text",
            }
            and field_obj.input_name == "prompt"
        )
        or (
            field_obj.class_type == "TextEncodeAceStepAudio1.5"
            and field_obj.input_name == "tags"
        )
    ):
        return prompt_role
    table: dict[tuple[str, str], str] = {
        ("CLIPTextEncode", "text"): "提示詞",
        ("CLIPTextEncodeFlux", "text"): "提示詞",
        ("TextEncodeQwenImageEditPlus", "prompt"): "Qwen 編輯提示詞",
        ("TextEncodeQwenImageEditPlusCustom_lrzjason", "prompt"): "Qwen 編輯提示詞",
        ("CR Text", "text"): "提示詞",
        ("CR Prompt Text", "prompt"): "提示詞",
        ("LoadImage", "image"): "上傳圖片",
        ("LoadImageMask", "image"): "上傳遮罩",
        ("LoadImageMask", "channel"): "遮罩通道",
        ("LoadVideo", "file"): "載入影片",
        ("CheckpointLoaderSimple", "ckpt_name"): "Checkpoint / 大模型",
        ("VAELoader", "vae_name"): "VAE",
        ("CLIPLoader", "clip_name"): "CLIP / 文字編碼器",
        ("DualCLIPLoader", "clip_name1"): "CLIP-L 文字編碼器",
        ("DualCLIPLoader", "clip_name2"): "T5 / 第二文字編碼器",
        ("TripleCLIPLoader", "clip_name1"): "CLIP-L 文字編碼器",
        ("TripleCLIPLoader", "clip_name2"): "CLIP-G 文字編碼器",
        ("TripleCLIPLoader", "clip_name3"): "T5 / 第三文字編碼器",
        ("CLIPLoaderGGUF", "clip_name"): "GGUF CLIP / 文字編碼器",
        ("DualCLIPLoaderGGUF", "clip_name1"): "GGUF CLIP-L 文字編碼器",
        ("DualCLIPLoaderGGUF", "clip_name2"): "GGUF CLIP-G / 第二文字編碼器",
        ("TripleCLIPLoaderGGUF", "clip_name1"): "GGUF CLIP-L 文字編碼器",
        ("TripleCLIPLoaderGGUF", "clip_name2"): "GGUF CLIP-G 文字編碼器",
        ("TripleCLIPLoaderGGUF", "clip_name3"): "GGUF T5 / 第三文字編碼器",
        ("CLIPVisionLoader", "clip_name"): "CLIP Vision 模型",
        ("UNETLoader", "unet_name"): "Diffusion / UNet 大模型",
        ("UnetLoaderGGUF", "unet_name"): "GGUF Diffusion / UNet 大模型",
        ("UnetLoaderGGUFAdvanced", "unet_name"): "GGUF Diffusion / UNet 大模型",
        ("LoraLoader", "lora_name"): "LoRA 模型",
        ("LoraLoaderModelOnly", "lora_name"): "LoRA 模型（Model-only）",
        ("LoraLoader", "strength_model"): "LoRA 強度（model）",
        ("LoraLoader", "strength_clip"): "LoRA 強度（clip）",
        ("LoraLoaderModelOnly", "strength_model"): "LoRA 強度",
        ("ControlNetLoader", "control_net_name"): "ControlNet 模型",
        ("UpscaleModelLoader", "model_name"): "放大 / Upscale 模型",
        ("LatentUpscaleModelLoader", "model_name"): "Latent 放大模型",
        ("LTXVAudioVAELoader", "ckpt_name"): "LTX Audio VAE Checkpoint",
        ("LTXAVTextEncoderLoader", "ckpt_name"): "LTX Text Encoder Checkpoint",
        ("KSampler", "seed"): "種子",
        ("KSampler", "steps"): "步數",
        ("KSampler", "cfg"): "CFG",
        ("KSampler", "denoise"): "Denoise",
        ("KSampler", "sampler_name"): "取樣器",
        ("KSampler", "scheduler"): "排程器",
        ("KSamplerSelect", "sampler_name"): "取樣器",
        ("BasicScheduler", "scheduler"): "排程器",
        ("BasicScheduler", "steps"): "步數",
        ("BasicScheduler", "denoise"): "Denoise",
        ("RandomNoise", "noise_seed"): "種子",
        ("FluxGuidance", "guidance"): "Flux Guidance",
        ("Flux2Scheduler", "steps"): "步數",
        ("Flux2Scheduler", "width"): "寬度",
        ("Flux2Scheduler", "height"): "高度",
        ("ModelSamplingSD3", "shift"): "Model Shift",
        ("ModelSamplingAuraFlow", "shift"): "Model Shift",
        ("KSamplerAdvanced", "add_noise"): "加入雜訊",
        ("KSamplerAdvanced", "noise_seed"): "雜訊種子",
        ("KSamplerAdvanced", "steps"): "步數",
        ("KSamplerAdvanced", "cfg"): "CFG",
        ("KSamplerAdvanced", "sampler_name"): "取樣器",
        ("KSamplerAdvanced", "scheduler"): "排程器",
        ("KSamplerAdvanced", "start_at_step"): "起始步數",
        ("KSamplerAdvanced", "end_at_step"): "結束步數",
        ("KSamplerAdvanced", "return_with_leftover_noise"): "保留剩餘雜訊",
        ("EmptyLatentImage", "width"): "寬度",
        ("EmptyLatentImage", "height"): "高度",
        ("EmptyLatentImage", "batch_size"): "批次大小",
        ("EmptySD3LatentImage", "width"): "寬度",
        ("EmptySD3LatentImage", "height"): "高度",
        ("EmptySD3LatentImage", "batch_size"): "批次大小",
        ("EmptyFlux2LatentImage", "width"): "寬度",
        ("EmptyFlux2LatentImage", "height"): "高度",
        ("EmptyFlux2LatentImage", "batch_size"): "批次大小",
        ("ControlNetApplyAdvanced", "strength"): "ControlNet 強度",
        ("ControlNetApplyAdvanced", "start_percent"): "ControlNet 起始 %",
        ("ControlNetApplyAdvanced", "end_percent"): "ControlNet 結束 %",
        ("ImagePadForOutpaint", "left"): "外擴 - 左",
        ("ImagePadForOutpaint", "top"): "外擴 - 上",
        ("ImagePadForOutpaint", "right"): "外擴 - 右",
        ("ImagePadForOutpaint", "bottom"): "外擴 - 下",
        ("ImagePadForOutpaint", "feathering"): "羽化",
        ("WanImageToVideo", "width"): "影片寬度",
        ("WanImageToVideo", "height"): "影片高度",
        ("WanImageToVideo", "length"): "影片幀數",
        ("WanImageToVideo", "batch_size"): "影片批次大小",
        ("WanVaceToVideo", "width"): "影片寬度",
        ("WanVaceToVideo", "height"): "影片高度",
        ("WanVaceToVideo", "length"): "影片幀數",
        ("WanVaceToVideo", "batch_size"): "影片批次大小",
        ("WanVaceToVideo", "strength"): "VACE 強度",
        ("HunyuanVideo15ImageToVideo", "width"): "影片寬度",
        ("HunyuanVideo15ImageToVideo", "height"): "影片高度",
        ("HunyuanVideo15ImageToVideo", "length"): "影片幀數",
        ("HunyuanVideo15ImageToVideo", "batch_size"): "影片批次大小",
        ("VAEEncodeForInpaint", "grow_mask_by"): "遮罩外擴",
        ("SaveImage", "filename_prefix"): "輸出檔名前綴（系統會改寫）",
        ("SaveVideo", "filename_prefix"): "影片輸出檔名前綴（系統會改寫）",
        ("SaveAudio", "filename_prefix"): "音訊輸出檔名前綴（系統會改寫）",
        ("SaveAudioMP3", "filename_prefix"): "音訊輸出檔名前綴（系統會改寫）",
        ("ByteDanceSeedreamNode", "prompt"): "生成提示詞",
        ("ByteDanceSeedreamNode", "model"): "Seedream 模型",
        ("ByteDanceSeedreamNode", "size_preset"): "尺寸預設",
        ("ByteDanceSeedreamNode", "width"): "寬度",
        ("ByteDanceSeedreamNode", "height"): "高度",
        ("ByteDanceSeedreamNode", "max_images"): "張數",
        ("ByteDanceSeedreamNode", "seed"): "種子",
        ("ByteDanceSeedreamNode", "watermark"): "浮水印",
        ("ByteDanceSeedreamNode", "fail_on_partial"): "部分失敗時停止",
        ("GrokImageEditNode", "prompt"): "編輯提示詞",
        ("GrokImageEditNode", "model"): "Grok 模型",
        ("Qwen3_VQA", "model"): "Qwen3 VQA 模型",
        ("GrokImageEditNode", "resolution"): "解析度",
        ("GrokImageEditNode", "number_of_images"): "張數",
        ("GrokImageEditNode", "seed"): "種子",
        ("GrokImageEditNode", "aspect_ratio"): "長寬比",
        ("StringConcatenate", "string_a"): "固定提示前綴",
        ("StringConcatenate", "string_b"): "提示詞",
        ("TextEncodeAceStepAudio1.5", "tags"): "音樂標籤",
        ("TextEncodeAceStepAudio1.5", "lyrics"): "歌詞",
        ("TextEncodeAceStepAudio1.5", "duration"): "秒數",
        ("TextEncodeAceStepAudio1.5", "bpm"): "BPM",
        ("TextEncodeAceStepAudio1.5", "timesignature"): "拍號",
        ("TextEncodeAceStepAudio1.5", "language"): "語言",
        ("TextEncodeAceStepAudio1.5", "keyscale"): "調式",
        ("TextEncodeAceStepAudio1.5", "generate_audio_codes"): "生成 audio codes",
        ("TextEncodeAceStepAudio1.5", "cfg_scale"): "CFG",
        ("TextEncodeAceStepAudio1.5", "temperature"): "Temperature",
        ("TextEncodeAceStepAudio1.5", "top_p"): "Top P",
        ("TextEncodeAceStepAudio1.5", "top_k"): "Top K",
        ("TextEncodeAceStepAudio1.5", "min_p"): "Min P",
        ("EmptyAceStep1.5LatentAudio", "seconds"): "音訊秒數",
        ("ImageScaleToTotalPixels", "upscale_method"): "縮放方式",
        ("ImageScaleToTotalPixels", "megapixels"): "目標百萬像素",
        ("ImageScaleToTotalPixels", "divisible_by"): "尺寸整除",
        ("LatentUpscaleBy", "upscale_method"): "Latent 放大方式",
        ("LatentUpscaleBy", "scale_by"): "Latent 放大倍率",
        ("ResizeImageMaskNode", "resize_type"): "縮放模式",
        ("ResizeImageMaskNode", "scale_method"): "縮放演算法",
        ("ResizeImageMaskNode", "resize_type.longer_size"): "長邊尺寸",
        ("ResizeImageMaskNode", "resize_type.shorter_size"): "短邊尺寸",
        ("ResizeImageMaskNode", "resize_type.width"): "縮放寬度",
        ("ResizeImageMaskNode", "resize_type.height"): "縮放高度",
        ("ResizeImageMaskNode", "resize_type.megapixels"): "目標百萬像素",
        ("ResizeImageMaskNode", "resize_type.multiplier"): "縮放倍率",
        ("ResizeImageMaskNode", "resize_type.multiple"): "尺寸倍數",
        ("ImageBlend", "blend_factor"): "預覽混合比例",
        ("ImageBlend", "blend_mode"): "預覽混合模式",
        ("SDPoseKeypointExtractor", "batch_size"): "姿態批次大小",
        ("SDPoseDrawKeypoints", "draw_body"): "繪製身體",
        ("SDPoseDrawKeypoints", "draw_hands"): "繪製手部",
        ("SDPoseDrawKeypoints", "draw_face"): "繪製臉部",
        ("SDPoseDrawKeypoints", "draw_feet"): "繪製腳部",
        ("SDPoseDrawKeypoints", "stick_width"): "骨架線寬",
        ("SDPoseDrawKeypoints", "face_point_size"): "臉部點大小",
        ("SDPoseDrawKeypoints", "score_threshold"): "姿態分數門檻",
        ("RTDETR_detect", "threshold"): "人物偵測門檻",
        ("RTDETR_detect", "class_name"): "偵測類別",
        ("RTDETR_detect", "max_detections"): "最大偵測數",
        ("CreateVideo", "fps"): "FPS",
    }
    label = table.get(
        (field_obj.class_type, field_obj.input_name),
        f"{field_obj.class_type}.{field_obj.input_name}",
    )
    if field_obj.category == FieldCategory.MODEL:
        return _model_label_with_role(field_obj, label)
    if field_obj.class_type in _STAGE_AWARE_LABEL_CLASSES:
        return _label_with_stage(field_obj, label)
    return label


def _field_disambiguation(field_obj: InputField) -> str:
    title = _clean_title(field_obj.node_title)
    if title:
        return title
    return f"Node {field_obj.node_id}"


def _serialize_field(field_obj: InputField, label_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the §9.1 panel-field record for one InputField."""
    constraints = dict(_FIELD_CONSTRAINT_HINTS.get((field_obj.class_type, field_obj.input_name), {}))
    label = _label_zh(field_obj, label_context)
    label_counts = (label_context or {}).get("label_counts", {})
    if label_counts.get(label, 0) > 1:
        disambiguation = _field_disambiguation(field_obj)
        if disambiguation and f"（{disambiguation}）" not in label:
            label = f"{label}（{disambiguation}）"
        else:
            ordinal = (label_context or {}).get("label_ordinals", {}).get(_field_id(field_obj))
            label = f"{label}（#{ordinal}）" if ordinal else f"{label}（{field_obj.node_id}）"
    payload = {
        "id": _field_id(field_obj),
        "node_id": field_obj.node_id,
        "class_type": field_obj.class_type,
        "input_name": field_obj.input_name,
        "category": field_obj.category.value,
        "label": label,
        "input_type": _input_type_for_category(field_obj.category),
        "required": True,
        "current_value": _safe_current_value(field_obj.raw_value),
    }
    if _is_template_locked_model_field(field_obj):
        payload["required"] = False
        payload["read_only"] = True
        payload["locked"] = True
        payload["lock_reason"] = "template_default_model"
    if field_obj.node_title:
        payload["node_title"] = field_obj.node_title
    if field_obj.class_type == "RTDETR_detect" and field_obj.input_name == "class_name":
        payload["input_type"] = "text"
    if constraints:
        payload["constraints"] = constraints
    return payload


def _supports_embedding_shortcuts(field: dict[str, Any]) -> bool:
    class_type = str(field.get("class_type") or "")
    input_name = str(field.get("input_name") or "")
    return (
        (class_type in {"CLIPTextEncode", "CLIPTextEncodeFlux", "CR Text"} and input_name == "text")
        or (
            class_type
            in {
                "TextEncodeQwenImageEditPlus",
                "TextEncodeQwenImageEditPlusCustom_lrzjason",
                "CR Prompt Text",
            }
            and input_name == "prompt"
        )
        or (class_type == "TextEncodeAceStepAudio1.5" and input_name == "tags")
    )


def _embedding_shortcuts_field(text_fields: Iterable[dict[str, Any]]) -> dict[str, Any]:
    field_ids = [str(field.get("id") or "") for field in text_fields if field.get("id")]
    return {
        "id": "text:embeddings",
        "node_id": "",
        "class_type": "EmbeddingShortcuts",
        "input_name": "embeddings",
        "category": FieldCategory.TEXT.value,
        "label": "Embedding 快速插入",
        "input_type": "embedding_shortcuts",
        "required": False,
        "current_value": "",
        "synthetic": True,
        "parent_category": "text",
        "constraints": {
            "target_field_ids": field_ids,
            "token_prefix": "<embeddings:",
        },
    }


def _safe_current_value(raw_value: Any) -> Any:
    """JSON-friendly clone of `raw_value` for the panel `current_value` slot."""
    try:
        json.dumps(raw_value)
        return raw_value
    except (TypeError, ValueError):
        return str(raw_value)


_SYSTEM_REWRITTEN_FIELDS = {
    ("SaveImage", "filename_prefix"),
    ("SaveVideo", "filename_prefix"),
    ("SaveAudio", "filename_prefix"),
    ("SaveAudioMP3", "filename_prefix"),
}


_TEMPLATE_LOCKED_MODEL_FIELDS = {
    ("VAELoader", "vae_name"),
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
    ("CLIPVisionLoader", "clip_name"),
    ("UNETLoader", "unet_name"),
    ("UnetLoaderGGUF", "unet_name"),
    ("UnetLoaderGGUFAdvanced", "unet_name"),
    ("LatentUpscaleModelLoader", "model_name"),
}


def _is_template_locked_model_field(field_obj: InputField) -> bool:
    return (field_obj.class_type, field_obj.input_name) in _TEMPLATE_LOCKED_MODEL_FIELDS


def _is_user_visible_field(field_obj: InputField) -> bool:
    if (field_obj.class_type, field_obj.input_name) in _SYSTEM_REWRITTEN_FIELDS:
        return False
    return field_obj.category != FieldCategory.UNKNOWN


def _is_required_user_input_field(field_obj: InputField) -> bool:
    if not _is_user_visible_field(field_obj):
        return False
    if _is_template_locked_model_field(field_obj):
        return False
    return True


def _linked_node_ref(raw_workflow: dict[str, Any] | None, value: Any) -> tuple[str, dict[str, Any], int] | None:
    if not isinstance(raw_workflow, dict):
        return None
    if not (isinstance(value, list) and len(value) == 2):
        return None
    node_id = str(value[0])
    node = raw_workflow.get(node_id)
    if not isinstance(node, dict):
        return None
    try:
        output_slot = int(value[1])
    except (TypeError, ValueError):
        output_slot = 0
    return node_id, node, output_slot


def _text_role_source_nodes(
    raw_workflow: dict[str, Any] | None,
    value: Any,
    *,
    depth: int = 0,
) -> set[str]:
    if depth > 8:
        return set()
    linked = _linked_node_ref(raw_workflow, value)
    if not linked:
        return set()
    node_id, node, output_slot = linked
    class_type = str(node.get("class_type") or "")
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    if class_type in {"CLIPTextEncode", "CLIPTextEncodeFlux", "TextEncodeAceStepAudio1.5"}:
        return {node_id}
    if class_type in {"TextEncodeQwenImageEditPlus", "TextEncodeQwenImageEditPlusCustom_lrzjason"}:
        sources = {node_id}
        sources.update(_text_role_source_nodes(raw_workflow, inputs.get("prompt"), depth=depth + 1))
        return sources
    if class_type in {"CR Text", "CR Prompt Text"}:
        return {node_id}
    if class_type == "ConditioningZeroOut":
        return set()
    if class_type in {
        "ControlNetApplyAdvanced",
        "ControlNetApplySD3",
        "CFGGuider",
        "HunyuanVideo15ImageToVideo",
        "LTXVConditioning",
        "LTXVCropGuides",
        "WanImageToVideo",
        "WanImageToVideoApi",
        "WanVaceToVideo",
    }:
        input_name = "negative" if output_slot == 1 else "positive"
        return _text_role_source_nodes(raw_workflow, inputs.get(input_name), depth=depth + 1)
    if class_type == "ReferenceLatent":
        return _text_role_source_nodes(raw_workflow, inputs.get("conditioning"), depth=depth + 1)
    return set()


def _build_label_context(
    analysis: WorkflowAnalysis,
    raw_workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prompt_roles: dict[str, set[str]] = {}
    for node in analysis.nodes:
        for input_field in node.inputs:
            if not input_field.is_link or input_field.input_name not in {"positive", "negative"}:
                continue
            source_node = str(input_field.raw_value[0]) if isinstance(input_field.raw_value, list) and input_field.raw_value else ""
            if source_node:
                prompt_roles.setdefault(source_node, set()).add(input_field.input_name)
            for text_node_id in _text_role_source_nodes(raw_workflow, input_field.raw_value):
                prompt_roles.setdefault(text_node_id, set()).add(input_field.input_name)

    context: dict[str, Any] = {
        "prompt_roles": {key: sorted(value) for key, value in prompt_roles.items()},
        "label_counts": {},
        "label_ordinals": {},
    }
    label_counts: dict[str, int] = {}
    label_ordinals: dict[str, int] = {}
    for field_obj in analysis.user_inputs:
        if not _is_user_visible_field(field_obj) or _panel_key_for_field(field_obj) is None:
            continue
        label = _label_zh(field_obj, context)
        label_counts[label] = label_counts.get(label, 0) + 1
        label_ordinals[_field_id(field_obj)] = label_counts[label]
    context["label_counts"] = label_counts
    context["label_ordinals"] = label_ordinals
    return context


def _workflow_has_vae_loader_node(analysis: WorkflowAnalysis) -> bool:
    return any(
        field.class_type == "VAELoader" and field.input_name == "vae_name"
        for field in analysis.user_inputs
    )


def _workflow_has_vae_consumer(raw_workflow: dict[str, Any] | None) -> bool:
    if not isinstance(raw_workflow, dict):
        return False
    for node in raw_workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for input_name, value in inputs.items():
            if str(input_name) != "vae":
                continue
            if isinstance(value, list) and len(value) == 2:
                return True
    return False


def _build_synthetic_vae_field(
    analysis: WorkflowAnalysis,
    raw_workflow: dict[str, Any] | None,
) -> InputField | None:
    """Create a synthetic editable VAE field for templates that consume `vae` input."""
    if _workflow_has_vae_loader_node(analysis) or not _workflow_has_vae_consumer(raw_workflow):
        return None
    return InputField(
        node_id="template",
        class_type="VAELoader",
        input_name="vae_name",
        raw_value="",
        category=FieldCategory.MODEL,
        is_link=False,
        node_title="Template VAE",
    )


# ----------------------------------------------------------------------------
# Public API: build_ui_schema
# ----------------------------------------------------------------------------


@dataclass
class UISchema:
    panels: list[dict[str, Any]] = field(default_factory=list)
    capability: dict[str, Any] = field(default_factory=dict)
    raw_workflow: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "panels": list(self.panels),
            "capability": dict(self.capability),
            "raw_workflow": dict(self.raw_workflow),
        }


_PANEL_ORDER: list[tuple[str, FieldCategory, str]] = [
    ("text", FieldCategory.TEXT, "文字輸入"),
    ("image", FieldCategory.IMAGE, "圖片輸入"),
    ("video", FieldCategory.VIDEO, "影片輸入"),
    ("model", FieldCategory.MODEL, "模型需求"),
    ("sampler", FieldCategory.SAMPLER, "採樣設定"),
    ("numeric", FieldCategory.NUMERIC, "進階數值參數"),
]


def build_ui_schema(
    *,
    analysis: WorkflowAnalysis,
    capability: CapabilityCheck | None = None,
    raw_workflow: dict[str, Any] | None = None,
) -> UISchema:
    """Group user-editable inputs onto the §9.2 panel layout.

    Sampler enums (sampler_name / scheduler) are tagged into the ``sampler``
    panel so the UI can render them as dropdowns alongside numeric KSampler
    fields. NUMERIC fields that *also* sit on KSampler (seed/steps/cfg/denoise)
    appear on the ``sampler`` panel; everything else NUMERIC moves to the
    ``numeric`` advanced panel.
    """
    schema = UISchema()
    by_panel: dict[str, list[dict[str, Any]]] = {key: [] for key, _, _ in _PANEL_ORDER}
    label_context = _build_label_context(analysis, raw_workflow=raw_workflow)

    synthetic_vae_field = _build_synthetic_vae_field(analysis, raw_workflow)
    synthetic_vae_payload = (
        _serialize_field(synthetic_vae_field, label_context)
        if synthetic_vae_field is not None
        else None
    )
    if synthetic_vae_payload is not None:
        synthetic_vae_payload["synthetic"] = True

    for field_obj in analysis.user_inputs:
        # Output filename prefixes are overwritten by §7.2; not user-editable.
        if not _is_user_visible_field(field_obj):
            continue
        panel_key = _panel_key_for_field(field_obj)
        if panel_key is None:
            continue
        by_panel[panel_key].append(_serialize_field(field_obj, label_context))
    if synthetic_vae_payload:
        by_panel["model"].append(synthetic_vae_payload)

    text_fields = by_panel.get("text", [])
    embedding_targets = [field for field in text_fields if _supports_embedding_shortcuts(field)]
    if embedding_targets:
        text_fields.append(_embedding_shortcuts_field(embedding_targets))

    for key, _category, label in _PANEL_ORDER:
        fields = by_panel.get(key, [])
        if not fields:
            continue
        schema.panels.append(
            {
                "id": key,
                "label": label,
                "collapsed_default": True,
                "fields": fields,
            }
        )

    schema.panels.append(
        {
            "id": "compatibility",
            "label": "相容性報告",
            "collapsed_default": False,
            "read_only": True,
            "fields": [],
        }
    )
    schema.panels.append(
        {
            "id": "raw",
            "label": "原始 workflow",
            "collapsed_default": True,
            "read_only": True,
            "fields": [],
        }
    )

    if capability is not None:
        schema.capability = capability.to_dict()
    if raw_workflow is not None:
        schema.raw_workflow = dict(raw_workflow)

    return schema


def _panel_key_for_field(field_obj: InputField) -> str | None:
    """Bucket a field onto a §9.2 panel."""
    cat = field_obj.category
    if cat == FieldCategory.TEXT:
        return "text"
    if cat == FieldCategory.IMAGE:
        return "image"
    if cat == FieldCategory.VIDEO:
        return "video"
    if cat == FieldCategory.MODEL:
        return "model"
    if cat == FieldCategory.SAMPLER:
        return "sampler"
    if cat == FieldCategory.NUMERIC:
        # KSampler fields go onto the sampler panel for cohesion; everything
        # else (latent dims, controlnet strength, outpaint padding, lora
        # strength) goes onto the advanced numeric panel.
        if field_obj.class_type in {"KSampler", "KSamplerAdvanced"}:
            return "sampler"
        return "numeric"
    if cat == FieldCategory.BOOLEAN:
        return "numeric"
    # FieldCategory.UNKNOWN — keep them out of UI so the user can't break the
    # workflow by editing fields whose semantics we don't model.
    return None


def required_user_inputs(analysis: WorkflowAnalysis) -> list[str]:
    """Field IDs that must be filled in before /run; consumed by §10 Gate 4."""
    ids: list[str] = []
    for field_obj in analysis.user_inputs:
        if not _is_required_user_input_field(field_obj):
            continue
        ids.append(_field_id(field_obj))
    return ids


__all__ = [
    "UISchema",
    "build_ui_schema",
    "required_user_inputs",
]
