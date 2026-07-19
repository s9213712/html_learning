from services.comfyui.workflow.summary import (
    extract_workflow_summary,
    validate_manifest_dependency_contract,
)


def test_workflow_summary_detects_unet_clip_and_embedding_dependencies():
    workflow = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "anima-preview2.safetensors"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_06b_base.safetensors"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "5": {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": "sigclip_vision_patch14_384.safetensors"}},
        "6": {"class_type": "LatentUpscaleModelLoader", "inputs": {"model_name": "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"}},
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["2", 0], "text": "anime, <embeddings:badhandv4.pt>, embedding:lazy series\\IL\\lazyneg"},
        },
    }

    summary = extract_workflow_summary(workflow)
    required = {(item["kind"], item["name"]) for item in summary["required_models"]}

    assert ("diffusion_model", "anima-preview2.safetensors") in required
    assert ("clip", "qwen_3_06b_base.safetensors") in required
    assert ("clip_vision", "sigclip_vision_patch14_384.safetensors") in required
    assert ("latent_upscale", "ltx-2.3-spatial-upscaler-x2-1.1.safetensors") in required
    assert ("vae", "qwen_image_vae.safetensors") in required
    assert ("embedding", "badhandv4.pt") in required
    assert ("embedding", "lazy series\\IL\\lazyneg") in required
    assert summary["default_params"]["diffusion_model"] == "anima-preview2.safetensors"
    assert summary["default_params"]["clip"] == "qwen_3_06b_base.safetensors"
    assert summary["default_params"]["upscale_model"] == ""


def test_workflow_summary_embedding_parser_stops_before_prompt_words():
    workflow = {
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["2", 0],
                "text": "embedding:lazyneg blurry, embedding:lazypos embedding:lazywet embedding:lazyloli",
            },
        },
    }

    summary = extract_workflow_summary(workflow)
    required = {(item["kind"], item["name"]) for item in summary["required_models"]}

    assert ("embedding", "lazyneg") in required
    assert ("embedding", "lazypos") in required
    assert ("embedding", "lazywet") in required
    assert ("embedding", "lazyloli") in required
    assert ("embedding", "lazyneg blurry") not in required
    assert ("embedding", "lazypos embedding:lazywet embedding:lazyloli") not in required


def test_workflow_summary_detects_ltx_text_encoder_dependency():
    workflow = {
        "1": {
            "class_type": "LTXAVTextEncoderLoader",
            "inputs": {
                "ckpt_name": "ltx-2.3-22b-dev-fp8.safetensors",
                "text_encoder": "gemma_3_12B_it_fp4_mixed.safetensors",
                "device": "default",
            },
        },
    }

    summary = extract_workflow_summary(workflow)
    required = {(item["kind"], item["name"]) for item in summary["required_models"]}

    assert ("checkpoint", "ltx-2.3-22b-dev-fp8.safetensors") in required
    assert ("clip", "gemma_3_12B_it_fp4_mixed.safetensors") in required
    assert summary["default_params"]["clip"] == "gemma_3_12B_it_fp4_mixed.safetensors"


def test_manifest_dependency_contract_fails_unknown_loader_model_field():
    result = validate_manifest_dependency_contract(
        {
            "1": {
                "class_type": "FutureModelLoader",
                "inputs": {"model_name": "future.safetensors"},
            },
        },
        {
            "required_models": [],
            "required_loras": [],
            "required_controlnets": [],
            "required_custom_nodes": [],
        },
    )

    assert result["ok"] is False
    assert any("unmapped loader dependency input" in error for error in result["errors"])


def test_manifest_dependency_contract_fails_unknown_loader_generic_model_field():
    result = validate_manifest_dependency_contract(
        {
            "1": {
                "class_type": "FutureModelLoader",
                "inputs": {"model": "future.safetensors"},
            },
        },
        {
            "required_models": [],
            "required_loras": [],
            "required_controlnets": [],
            "required_custom_nodes": [],
        },
    )

    assert result["ok"] is False
    assert any("FutureModelLoader.model" in error for error in result["errors"])


def test_manifest_dependency_contract_rejects_windows_drive_dependency_path():
    result = validate_manifest_dependency_contract(
        {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": r"C:\\models\\unsafe.safetensors"},
            },
        },
        {
            "required_models": [],
            "required_loras": [],
            "required_controlnets": [],
            "required_custom_nodes": [],
        },
    )

    assert result["ok"] is False
    assert any("not a safe relative path" in error for error in result["errors"])


def test_manifest_dependency_contract_fails_duplicates_and_category_pollution():
    workflow = {
        "1": {"class_type": "LoraLoader", "inputs": {"lora_name": "styles/example.safetensors"}},
    }
    result = validate_manifest_dependency_contract(
        workflow,
        {
            "required_models": [
                {"kind": "checkpoint", "name": "styles/example.safetensors"},
            ],
            "required_loras": [
                {"name": "styles/example.safetensors"},
                {"name": "styles\\example.safetensors"},
            ],
            "required_controlnets": [],
            "required_custom_nodes": [],
        },
    )

    assert result["ok"] is False
    assert any("duplicate dependency" in error for error in result["errors"])
    assert any("category overlap models_loras" in error for error in result["errors"])
    assert result["differences"]["models"]["extra_in_manifest"]
