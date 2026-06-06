"""UI schema regression for the ComfyUI template importer (§9)."""

import json
from pathlib import Path

import pytest

from services.comfyui.template.analyzer import FieldCategory, analyze_workflow_json, classify_input_field
from services.comfyui.template.capability import CapabilityCheck
from services.comfyui.template.ui_schema import (
    build_ui_schema,
    required_user_inputs,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


# Same minimal txt2img.
TXT2IMG = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "low quality", "clip": ["4", 1]}},
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0, "steps": 20, "cfg": 7.5, "denoise": 1.0,
            "sampler_name": "euler", "scheduler": "normal",
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
        },
    },
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]}},
}


def _panel_ids(schema):
    return [p["id"] for p in schema.panels]


def test_ui_schema_panels_in_canonical_order():
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG))
    ids = _panel_ids(schema)
    # Panel order per §9.2: text, image, model, sampler, numeric, compatibility, raw.
    # Image / numeric panels with no fields are dropped; compatibility / raw always present.
    assert ids[0] == "text"
    assert ids[-2:] == ["compatibility", "raw"]
    assert "model" in ids
    assert "sampler" in ids


def test_ui_schema_groups_text_inputs_onto_text_panel():
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG))
    text_panel = next(p for p in schema.panels if p["id"] == "text")
    field_ids = {f["id"] for f in text_panel["fields"]}
    assert "node:6:text" in field_ids
    assert "node:7:text" in field_ids


def test_ui_schema_labels_positive_and_negative_prompt_fields_by_sampler_links():
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG))
    text_panel = next(p for p in schema.panels if p["id"] == "text")
    labels = {f["id"]: f["label"] for f in text_panel["fields"]}
    assert labels["node:6:text"] == "正向提示詞"
    assert labels["node:7:text"] == "負面提示詞"


def test_ui_schema_labels_prompt_used_by_both_roles_as_shared():
    workflow = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "shared scene prompt", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "low quality", "clip": ["4", 1]}},
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 1,
                "steps": 4,
                "cfg": 1,
                "denoise": 1,
                "sampler_name": "euler",
                "scheduler": "normal",
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["6", 0],
                "latent_image": ["5", 0],
            },
        },
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 2,
                "steps": 4,
                "cfg": 1,
                "denoise": 1,
                "sampler_name": "euler",
                "scheduler": "normal",
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow), raw_workflow=workflow)
    text_panel = next(p for p in schema.panels if p["id"] == "text")
    labels = {f["id"]: f["label"] for f in text_panel["fields"]}

    assert labels["node:6:text"] == "正負共用提示詞"
    assert labels["node:7:text"] == "負面提示詞"


def test_ui_schema_labels_wrapped_qwen_reference_latent_prompt_fields():
    workflow = {
        "1": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"prompt": "replace cat"}},
        "2": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"prompt": ""}},
        "3": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["1", 0], "latent": ["5", 0]}},
        "4": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["2", 0], "latent": ["5", 0]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 1,
                "steps": 4,
                "cfg": 1,
                "denoise": 1,
                "sampler_name": "euler",
                "scheduler": "simple",
                "model": ["7", 0],
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": ["5", 0],
            },
        },
    }
    schema = build_ui_schema(
        analysis=analyze_workflow_json(workflow),
        raw_workflow=workflow,
    )
    text_panel = next(p for p in schema.panels if p["id"] == "text")
    labels = {f["id"]: f["label"] for f in text_panel["fields"]}

    assert labels["node:1:prompt"] == "正向提示詞"
    assert labels["node:2:prompt"] == "負面提示詞"


def test_ui_schema_adds_embeddings_as_text_child_when_prompt_fields_exist():
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG))
    text_panel = next(p for p in schema.panels if p["id"] == "text")
    embedding_field = next(f for f in text_panel["fields"] if f["id"] == "text:embeddings")
    assert embedding_field["input_type"] == "embedding_shortcuts"
    assert embedding_field["synthetic"] is True
    assert embedding_field["parent_category"] == "text"
    assert embedding_field["constraints"]["target_field_ids"] == ["node:6:text", "node:7:text"]


def test_ui_schema_adds_embeddings_for_flux_text_fields():
    workflow = {
        "1": {"class_type": "CLIPTextEncodeFlux", "inputs": {"text": "cat", "clip": ["2", 0]}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "clip.safetensors"}},
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow))
    text_panel = next(p for p in schema.panels if p["id"] == "text")
    embedding_field = next(f for f in text_panel["fields"] if f["id"] == "text:embeddings")
    assert embedding_field["constraints"]["target_field_ids"] == ["node:1:text"]


def test_ui_schema_routes_ksampler_numeric_to_sampler_panel():
    """KSampler.{seed, steps, cfg, denoise} sit on the sampler panel for cohesion."""
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG))
    sampler_panel = next(p for p in schema.panels if p["id"] == "sampler")
    fids = {f["id"] for f in sampler_panel["fields"]}
    assert "node:3:seed" in fids
    assert "node:3:steps" in fids
    assert "node:3:sampler_name" in fids
    assert "node:3:scheduler" in fids


def test_ui_schema_routes_ksampler_advanced_fields_to_sampler_panel():
    workflow = {
        **TXT2IMG,
        "10": {
            "class_type": "KSamplerAdvanced",
            "inputs": {
                "noise_seed": 123,
                "steps": 30,
                "cfg": 6.5,
                "sampler_name": "euler",
                "scheduler": "normal",
                "add_noise": "enable",
                "start_at_step": 0,
                "end_at_step": 30,
                "return_with_leftover_noise": "disable",
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow))
    sampler_panel = next(p for p in schema.panels if p["id"] == "sampler")
    fids = {f["id"] for f in sampler_panel["fields"]}
    assert "node:10:noise_seed" in fids
    assert "node:10:sampler_name" in fids
    assert "node:10:start_at_step" in fids


def test_ui_schema_routes_non_ksampler_numeric_to_advanced_panel():
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG))
    panel_ids_present = _panel_ids(schema)
    if "numeric" in panel_ids_present:
        advanced = next(p for p in schema.panels if p["id"] == "numeric")
        fids = {f["id"] for f in advanced["fields"]}
        # EmptyLatentImage.width/height/batch_size are NUMERIC but not on KSampler
        assert "node:5:width" in fids
        assert "node:5:height" in fids
        assert "node:5:batch_size" in fids


def test_ui_schema_drops_save_image_filename_prefix():
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG))
    all_field_ids = {f["id"] for p in schema.panels for f in p.get("fields", [])}
    assert "node:9:filename_prefix" not in all_field_ids


def test_ui_schema_drops_save_video_filename_prefix():
    workflow = {
        **TXT2IMG,
        "10": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "movie", "images": ["8", 0]}},
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow))
    all_field_ids = {f["id"] for p in schema.panels for f in p.get("fields", [])}
    assert "node:10:filename_prefix" not in all_field_ids


def test_ui_schema_drops_save_audio_filename_prefix():
    workflow = {
        **TXT2IMG,
        "107": {"class_type": "SaveAudioMP3", "inputs": {"filename_prefix": "audio/ace", "audio": ["18", 0], "quality": "V0"}},
        "108": {"class_type": "SaveAudio", "inputs": {"filename_prefix": "audio/wav", "audio": ["18", 0]}},
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow))
    all_field_ids = {f["id"] for p in schema.panels for f in p.get("fields", [])}
    assert "node:107:filename_prefix" not in all_field_ids
    assert "node:108:filename_prefix" not in all_field_ids


def test_ui_schema_carries_capability_payload_when_provided():
    cap = CapabilityCheck(
        supported=["KSampler"],
        unsupported=[],
        missing_models={"ckpt": ["x.safetensors"]},
        sampler_options={"KSampler.sampler_name": ["euler", "dpmpp_2m"]},
        overall="PARTIALLY_SUPPORTED",
        blockers=["缺 ckpt: x.safetensors"],
    )
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG), capability=cap)
    assert schema.capability["overall"] == "PARTIALLY_SUPPORTED"
    assert schema.capability["missing_models"] == {"ckpt": ["x.safetensors"]}


def test_ui_schema_text_field_constraints_max_length():
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG))
    text_panel = next(p for p in schema.panels if p["id"] == "text")
    pos = next(f for f in text_panel["fields"] if f["id"] == "node:6:text")
    assert pos["constraints"]["max_length"] == 2000


def test_ui_schema_image_panel_has_accept_mime():
    workflow = {
        **TXT2IMG,
        "10": {"class_type": "LoadImage", "inputs": {"image": "user_uploaded.png"}},
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow))
    image_panel = next(p for p in schema.panels if p["id"] == "image")
    img_field = next(f for f in image_panel["fields"] if f["id"] == "node:10:image")
    assert "image/png" in img_field["constraints"]["accept_mime"]


def test_ui_schema_video_panel_has_load_video_file_picker():
    workflow = {
        **TXT2IMG,
        "10": {"class_type": "LoadVideo", "inputs": {"file": "input.mp4"}},
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow))
    video_panel = next(p for p in schema.panels if p["id"] == "video")
    video_field = next(f for f in video_panel["fields"] if f["id"] == "node:10:file")

    assert video_panel["label"] == "影片輸入"
    assert video_field["label"] == "載入影片"
    assert video_field["input_type"] == "video_file_picker"
    assert "video/mp4" in video_field["constraints"]["accept_mime"]


def test_ui_schema_shows_template_locked_model_loader_fields_as_readonly():
    workflow = {
        **TXT2IMG,
        "20": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"},
            "_meta": {"title": "載入模型"},
        },
        "21": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": "qwen_3_06b_base.safetensors"},
        },
        "22": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "qwen_image_vae.safetensors"},
        },
        "23": {
            "class_type": "CLIPVisionLoader",
            "inputs": {"clip_name": "sigclip_vision_patch14_384.safetensors"},
        },
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow))
    all_fields = {f["id"]: f for p in schema.panels for f in p.get("fields", [])}
    ids = required_user_inputs(analyze_workflow_json(workflow))

    assert all_fields["node:20:unet_name"]["read_only"] is True
    assert all_fields["node:20:unet_name"]["locked"] is True
    assert all_fields["node:20:unet_name"]["required"] is False
    assert all_fields["node:21:clip_name"]["read_only"] is True
    assert all_fields["node:22:vae_name"]["read_only"] is True
    assert all_fields["node:23:clip_name"]["label"] == "CLIP Vision 模型"
    assert all_fields["node:23:clip_name"]["read_only"] is True
    assert "node:20:unet_name" not in ids
    assert "node:21:clip_name" not in ids
    assert "node:22:vae_name" not in ids
    assert "node:23:clip_name" not in ids


def test_ui_schema_classifies_nonstandard_template_model_fields():
    workflow = {
        "10": {"class_type": "LTXVAudioVAELoader", "inputs": {"ckpt_name": "ltx-2.3-22b-dev-fp8.safetensors"}},
        "11": {"class_type": "LTXAVTextEncoderLoader", "inputs": {"ckpt_name": "ltx-2.3-22b-dev-fp8.safetensors"}},
        "12": {"class_type": "Qwen3_VQA", "inputs": {"model": "Qwen3-VL-4B-Instruct"}},
    }
    analysis = analyze_workflow_json(workflow)
    schema = build_ui_schema(analysis=analysis)
    all_fields = {f["id"]: f for p in schema.panels for f in p.get("fields", [])}

    assert analysis.required_models["ckpt"] == ["ltx-2.3-22b-dev-fp8.safetensors"]
    assert analysis.required_models["api_model"] == ["Qwen3-VL-4B-Instruct"]
    assert all_fields["node:10:ckpt_name"]["category"] == "MODEL"
    assert all_fields["node:10:ckpt_name"]["input_type"] == "select"
    assert all_fields["node:11:ckpt_name"]["label"] == "LTX Text Encoder Checkpoint"
    assert all_fields["node:12:model"]["label"] == "Qwen3 VQA 模型"


def test_official_workflow_modelish_inputs_are_classified():
    modelish_input_names = {
        "ckpt_name",
        "vae_name",
        "lora_name",
        "unet_name",
        "control_net_name",
        "model_name",
        "clip_name",
        "clip_name1",
        "clip_name2",
        "clip_name3",
        "model",
    }
    missing = []
    for workflow_path in sorted((PROJECT_ROOT / "workflows" / "comfyui").glob("origin_*/workflow.json")):
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type") or "")
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            for input_name, raw_value in inputs.items():
                if input_name not in modelish_input_names:
                    continue
                if isinstance(raw_value, list) and len(raw_value) == 2:
                    continue
                if classify_input_field(class_type, input_name) != FieldCategory.MODEL:
                    missing.append(f"{workflow_path.parent.name}:{node_id}:{class_type}.{input_name}")
    assert missing == []


def test_ui_schema_model_labels_distinguish_large_lora_and_upscale_models():
    workflow = {
        **TXT2IMG,
        "22": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": "WAN/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors", "strength_model": 1},
        },
        "23": {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": "4x-UltraSharp.pth"},
        },
        "24": {
            "class_type": "LatentUpscaleModelLoader",
            "inputs": {"model_name": "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"},
        },
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow))
    model_panel = next(p for p in schema.panels if p["id"] == "model")
    labels = {f["id"]: f["label"] for f in model_panel["fields"]}
    fields = {f["id"]: f for f in model_panel["fields"]}

    assert labels["node:4:ckpt_name"] == "Checkpoint / 大模型"
    assert labels["node:22:lora_name"] == "LoRA 模型（Model-only）（High Noise）"
    assert labels["node:23:model_name"] == "放大 / Upscale 模型"
    assert labels["node:24:model_name"] == "Latent 放大模型"
    assert fields["node:24:model_name"]["locked"] is True


def test_ui_schema_uses_ordinals_when_duplicate_labels_share_same_stage_title():
    workflow = {
        **TXT2IMG,
        "22": {
            "class_type": "LoraLoader",
            "inputs": {"lora_name": "first.safetensors", "strength_model": 1, "strength_clip": 1},
            "_meta": {"title": "Lora"},
        },
        "23": {
            "class_type": "LoraLoader",
            "inputs": {"lora_name": "second.safetensors", "strength_model": 1, "strength_clip": 1},
            "_meta": {"title": "Lora"},
        },
    }
    schema = build_ui_schema(analysis=analyze_workflow_json(workflow))
    model_panel = next(p for p in schema.panels if p["id"] == "model")
    labels = {f["id"]: f["label"] for f in model_panel["fields"]}

    assert labels["node:22:lora_name"] == "LoRA 模型（Lora）（#1）"
    assert labels["node:23:lora_name"] == "LoRA 模型（Lora）（#2）"


def test_required_user_inputs_excludes_save_image_prefix():
    ids = required_user_inputs(analyze_workflow_json(TXT2IMG))
    assert "node:9:filename_prefix" not in ids
    # All others remain
    assert "node:6:text" in ids
    assert "node:3:seed" in ids


def test_required_user_inputs_excludes_save_video_prefix():
    workflow = {
        **TXT2IMG,
        "10": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "movie", "images": ["8", 0]}},
    }
    ids = required_user_inputs(analyze_workflow_json(workflow))
    assert "node:10:filename_prefix" not in ids


def test_required_user_inputs_skips_unknown_category_fields():
    workflow = {
        **TXT2IMG,
        "10": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "y.safetensors", "weird_extra": "abc"}},
    }
    ids = required_user_inputs(analyze_workflow_json(workflow))
    # weird_extra is UNKNOWN for CheckpointLoaderSimple — should not show up
    assert "node:10:weird_extra" not in ids


def test_ui_schema_to_dict_is_json_friendly():
    schema = build_ui_schema(analysis=analyze_workflow_json(TXT2IMG))
    payload = schema.to_dict()
    assert isinstance(payload["panels"], list)
    assert isinstance(payload["capability"], dict)
    assert isinstance(payload["raw_workflow"], dict)
