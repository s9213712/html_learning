import json
from pathlib import Path

from services.comfyui.template.gguf_workflow import apply_gguf_workflow_profile
from services.comfyui.template.sdxl_refiner import apply_sdxl_refiner_option


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / "workflows" / "comfyui"


def _workflow(workflow_id):
    return json.loads((WORKFLOW_DIR / workflow_id / "workflow.json").read_text(encoding="utf-8"))


def test_sdxl_skip_refiner_keeps_base_prompt_nodes_and_rewires_decode():
    workflow = _workflow("origin_sdxl_txt2img")
    user_inputs = {
        "6": {"text": "2girls, girls love"},
        "7": {"text": "low quality"},
        "15": {"text": "refiner prompt should be removed"},
        "16": {"text": "refiner negative should be removed"},
        "10": {"steps": 30},
    }

    result = apply_sdxl_refiner_option(workflow, user_inputs, {"skip_refiner": True})

    assert result.skip_refiner is True
    assert "6" in result.workflow and "7" in result.workflow
    assert result.workflow["10"]["inputs"]["positive"] == ["6", 0]
    assert result.workflow["10"]["inputs"]["negative"] == ["7", 0]
    assert result.user_inputs["6"]["text"] == "2girls, girls love"
    assert result.user_inputs["7"]["text"] == "low quality"
    for node_id in ("11", "12", "15", "16"):
        assert node_id not in result.workflow
        assert node_id not in result.user_inputs
    assert result.workflow["17"]["inputs"]["samples"] == ["10", 0]
    assert result.workflow["17"]["inputs"]["vae"] == ["4", 2]
    assert result.workflow["10"]["inputs"]["steps"] == 30
    assert result.workflow["10"]["inputs"]["end_at_step"] == 30
    assert result.workflow["10"]["inputs"]["return_with_leftover_noise"] == "disable"


def test_gguf_workflow_profile_applies_model_nodes_and_sampler_defaults_without_prompt_override():
    workflow = _workflow("origin_sdxl_gguf_txt2img")
    user_inputs = {
        "6": {"text": "2girls, girls love"},
        "7": {"text": "low quality"},
        "3": {"steps": 12},
    }

    result = apply_gguf_workflow_profile(
        workflow,
        user_inputs,
        {"profile_id": "calcuis_illustrious_sdxl", "variant_id": "q4_0"},
    )

    assert result.profile["id"] == "calcuis_illustrious_sdxl"
    assert result.variant["gguf_file"] == "illustrious-q4_0.gguf"
    assert result.workflow["4"]["inputs"]["unet_name"] == "illustrious-q4_0.gguf"
    assert result.workflow["10"]["class_type"] == "DualCLIPLoader"
    assert result.workflow["10"]["inputs"]["clip_name1"] == "illustrious_clip_l.safetensors"
    assert result.workflow["10"]["inputs"]["clip_name2"] == "illustrious_clip_g.safetensors"
    assert result.workflow["11"]["inputs"]["vae_name"] == "illustrious_vae.safetensors"
    assert result.workflow["3"]["inputs"]["cfg"] == 8.0
    assert result.workflow["3"]["inputs"]["steps"] == 20
    assert result.user_inputs["6"]["text"] == "2girls, girls love"
    assert result.user_inputs["7"]["text"] == "low quality"
    assert result.user_inputs["3"]["steps"] == 12
    assert result.user_inputs["4"]["unet_name"] == "illustrious-q4_0.gguf"
    assert result.user_inputs["10"]["clip_name1"] == "illustrious_clip_l.safetensors"
    assert result.user_inputs["11"]["vae_name"] == "illustrious_vae.safetensors"
