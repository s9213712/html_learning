import importlib.util
from types import SimpleNamespace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO_ROOT / "scripts" / "comfyui" / "official_workflow_probe.py"


def _load_probe_module():
    spec = importlib.util.spec_from_file_location("official_workflow_probe", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_preflight_ignores_model_inputs_connected_from_other_nodes():
    probe = _load_probe_module()
    object_info = {
        "UNETLoader": {
            "input": {
                "required": {
                    "unet_name": [["available.safetensors"], {}],
                },
            },
        },
    }
    workflow = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": ["122", 0]},
        },
    }

    result = probe._preflight("linked_model_input", workflow, object_info)

    assert result["runnable"] is True
    assert result["missing_models"] == []


def test_output_quality_flags_all_black_png():
    probe = _load_probe_module()
    black_png = probe._png_rgba(32, 32, (0, 0, 0, 255))

    issues = probe._output_quality_issues({"data": black_png})

    assert issues
    assert "almost entirely black" in issues[0]


def test_output_quality_accepts_non_solid_png():
    probe = _load_probe_module()
    from PIL import Image
    from io import BytesIO

    image = Image.new("RGB", (32, 32), (0, 0, 0))
    for x in range(16, 32):
        for y in range(32):
            image.putpixel((x, y), (255, 128, 64))
    out = BytesIO()
    image.save(out, format="PNG")

    assert probe._output_quality_issues({"data": out.getvalue()}) == []


def test_preflight_reports_literal_missing_model_inputs():
    probe = _load_probe_module()
    object_info = {
        "UNETLoader": {
            "input": {
                "required": {
                    "unet_name": [["available.safetensors"], {}],
                },
            },
        },
    }
    workflow = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "missing.safetensors"},
        },
    }

    result = probe._preflight("missing_literal_model", workflow, object_info)

    assert result["runnable"] is False
    assert result["missing_models"] == [
        {
            "node_id": "1",
            "class_type": "UNETLoader",
            "input": "unet_name",
            "value": "missing.safetensors",
        },
    ]


def test_preflight_reports_literal_missing_comfyui_gguf_unet_inputs():
    probe = _load_probe_module()
    object_info = {
        "UnetLoaderGGUF": {
            "input": {
                "required": {
                    "unet_name": [["available.gguf"], {}],
                },
            },
        },
    }
    workflow = {
        "4": {
            "class_type": "UnetLoaderGGUF",
            "inputs": {"unet_name": "missing.gguf"},
        },
    }

    result = probe._preflight("missing_gguf_unet", workflow, object_info)

    assert result["runnable"] is False
    assert result["missing_models"] == [
        {
            "node_id": "4",
            "class_type": "UnetLoaderGGUF",
            "input": "unet_name",
            "value": "missing.gguf",
        },
    ]


def test_preflight_does_not_treat_loadvideo_media_as_a_model_dependency():
    probe = _load_probe_module()
    object_info = {
        "LoadVideo": {
            "input": {
                "required": {
                    "file": ["COMBO", {"options": ["available.mp4"]}],
                },
            },
        },
    }
    workflow = {
        "1": {
            "class_type": "LoadVideo",
            "inputs": {"file": "missing.mp4"},
        },
    }

    result = probe._preflight("missing_video", workflow, object_info)

    assert result["runnable"] is True
    assert result["missing_models"] == []


def test_preflight_reports_model_input_when_comfyui_option_list_is_empty():
    probe = _load_probe_module()
    object_info = {
        "LatentUpscaleModelLoader": {
            "input": {
                "required": {
                    "model_name": ["COMBO", {"options": []}],
                },
            },
        },
    }
    workflow = {
        "303": {
            "class_type": "LatentUpscaleModelLoader",
            "inputs": {"model_name": "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"},
        },
    }

    result = probe._preflight("missing_latent_upscaler", workflow, object_info)

    assert result["runnable"] is False
    assert result["missing_models"] == [
        {
            "node_id": "303",
            "class_type": "LatentUpscaleModelLoader",
            "input": "model_name",
            "value": "ltx-2.3-spatial-upscaler-x2-1.1.safetensors",
        },
    ]


def test_preflight_reports_missing_ltx_text_encoder_dependency():
    probe = _load_probe_module()
    object_info = {
        "LTXAVTextEncoderLoader": {
            "input": {
                "required": {
                    "text_encoder": [["available-gemma.safetensors"], {}],
                    "ckpt_name": [["available-ltx.safetensors"], {}],
                },
            },
        },
    }
    workflow = {
        "303": {
            "class_type": "LTXAVTextEncoderLoader",
            "inputs": {
                "text_encoder": "missing-gemma.safetensors",
                "ckpt_name": "available-ltx.safetensors",
            },
        },
    }

    result = probe._preflight("missing_ltx_text_encoder", workflow, object_info)

    assert result["runnable"] is False
    assert result["missing_models"] == [{
        "node_id": "303",
        "class_type": "LTXAVTextEncoderLoader",
        "input": "text_encoder",
        "value": "missing-gemma.safetensors",
    }]


def test_preflight_accepts_equivalent_subfolder_model_paths():
    probe = _load_probe_module()
    object_info = {
        "ControlNetLoader": {
            "input": {
                "required": {
                    "control_net_name": [["QWEN/Qwen-Image-2512-Fun-Controlnet-Union-2602.safetensors"], {}],
                },
            },
        },
    }
    workflow = {
        "135": {
            "class_type": "ControlNetLoader",
            "inputs": {"control_net_name": "QWEN\\Qwen-Image-2512-Fun-Controlnet-Union-2602.safetensors"},
        },
    }

    result = probe._preflight("qwen_controlnet", workflow, object_info)

    assert result["runnable"] is True
    assert result["missing_models"] == []


def test_formal_params_preserve_generation_inputs_but_remap_probe_files():
    probe = _load_probe_module()
    workflow = {
        "1": {
            "class_type": "KSampler",
            "inputs": {
                "steps": 30,
                "seed": 123,
                "cfg": 7.0,
                "filename_prefix": "formal/original",
            },
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "original prompt"},
        },
        "3": {
            "class_type": "LoadImage",
            "inputs": {"image": "missing_input.png", "upload": "image"},
        },
    }

    patched = probe._patch_for_probe(
        workflow,
        "formal_case",
        width=256,
        height=256,
        steps=1,
        prompt="smoke prompt",
        negative_prompt="smoke negative",
        checkpoint_model="",
        source_image_name="probe_source.png",
        mask_image_name="probe_mask.png",
        parameter_mode="formal",
    )

    assert patched["1"]["inputs"]["steps"] == 30
    assert patched["1"]["inputs"]["seed"] == 123
    assert patched["1"]["inputs"]["cfg"] == 7.0
    assert patched["2"]["inputs"]["text"] == "original prompt"
    assert patched["1"]["inputs"]["filename_prefix"] == "probe/hackme_official_probe/formal_case"
    assert patched["3"]["inputs"]["image"] == "probe_source.png"


def test_custom_params_apply_only_explicit_overrides():
    probe = _load_probe_module()
    workflow = {
        "1": {
            "class_type": "KSampler",
            "inputs": {
                "steps": 30,
                "seed": 123,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
            },
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "original prompt"},
        },
        "3": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "original.safetensors"},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
        },
    }

    patched = probe._patch_for_probe(
        workflow,
        "custom_case",
        width=256,
        height=256,
        steps=1,
        prompt="smoke prompt",
        negative_prompt="smoke negative",
        checkpoint_model="",
        source_image_name="probe_source.png",
        mask_image_name="probe_mask.png",
        parameter_mode="custom",
        custom_params={
            "prompt": "custom prompt",
            "seed": 999,
            "steps": 12,
            "checkpoint_model": "custom.safetensors",
            "node_inputs": {"4": {"width": 768}},
        },
    )

    assert patched["1"]["inputs"]["steps"] == 12
    assert patched["1"]["inputs"]["seed"] == 999
    assert patched["1"]["inputs"]["cfg"] == 7.0
    assert patched["2"]["inputs"]["text"] == "custom prompt"
    assert patched["3"]["inputs"]["ckpt_name"] == "custom.safetensors"
    assert patched["4"]["inputs"]["width"] == 768
    assert patched["4"]["inputs"]["height"] == 1024


def test_custom_params_apply_negative_prompt_through_reference_latent_prompt_inputs():
    probe = _load_probe_module()
    workflow = {
        "1": {
            "class_type": "KSampler",
            "inputs": {
                "positive": ["5", 0],
                "negative": ["6", 0],
                "steps": 4,
            },
        },
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "clip.safetensors"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "vae.safetensors"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": "source.png", "upload": "image"}},
        "5": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["7", 0], "latent": ["9", 0]}},
        "6": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["8", 0], "latent": ["9", 0]}},
        "7": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"clip": ["2", 0], "image1": ["4", 0], "prompt": "original positive", "vae": ["3", 0]},
        },
        "8": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"clip": ["2", 0], "image1": ["4", 0], "prompt": "", "vae": ["3", 0]},
        },
        "9": {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["3", 0]}},
    }

    patched = probe._patch_for_probe(
        workflow,
        "qwen_edit_reference_latent",
        width=256,
        height=256,
        steps=1,
        prompt="smoke prompt",
        negative_prompt="smoke negative",
        checkpoint_model="",
        source_image_name="probe_source.png",
        mask_image_name="probe_mask.png",
        parameter_mode="custom",
        custom_params={
            "prompt": "adult portrait edit",
            "negative_prompt": "text, watermark, black image",
        },
    )

    assert patched["7"]["inputs"]["prompt"] == "adult portrait edit"
    assert patched["8"]["inputs"]["prompt"] == "text, watermark, black image"
    assert patched["4"]["inputs"]["image"] == "probe_source.png"


def test_custom_params_can_be_loaded_from_aliases_and_explicit_flags():
    probe = _load_probe_module()
    args = SimpleNamespace(
        custom_params=True,
        custom_param_file="",
        custom_param_json='{"seed": 111, "class_inputs": {"KSampler": {"cfg": 5.5}}}',
        prompt="alias prompt",
        negative_prompt=None,
        steps=9,
        width=None,
        height=None,
        checkpoint_model="",
        custom_prompt="explicit prompt",
        custom_negative_prompt="explicit negative",
        custom_seed=None,
        custom_steps=None,
        custom_width=640,
        custom_height=None,
        custom_cfg=None,
        custom_sampler_name=None,
        custom_scheduler=None,
        custom_batch_size=None,
        custom_checkpoint_model=None,
        custom_diffusion_model=None,
        custom_clip_model=None,
        custom_vae_model=None,
        custom_lora_model=None,
        custom_lora_strength_model=None,
        custom_lora_strength_clip=None,
        custom_controlnet_model=None,
        custom_upscale_model=None,
    )

    params = probe._load_custom_params(args)

    assert params["prompt"] == "explicit prompt"
    assert params["negative_prompt"] == "explicit negative"
    assert params["steps"] == 9
    assert params["width"] == 640
    assert params["seed"] == 111
    assert params["class_inputs"] == {"KSampler": {"cfg": 5.5}}


def test_prompt_safety_blocks_sexualized_minor_or_age_ambiguous_prompt():
    probe = _load_probe_module()
    workflow = {
        "83": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "A girl with cat ears wearing underwear laying on the bed."},
        },
    }

    detail = probe._prompt_safety_issue(workflow)

    assert detail
    assert "83.text" in detail


def test_prompt_safety_allows_explicit_adult_non_minor_prompt():
    probe = _load_probe_module()
    workflow = {
        "83": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "An adult woman with cat ears wearing a cozy costume in a bedroom."},
        },
    }

    assert probe._prompt_safety_issue(workflow) == ""


@pytest.mark.parametrize(
    ("node_class", "input_name", "custom_arg"),
    (
        ("CheckpointLoaderSimple", "ckpt_name", "custom_checkpoint_model"),
        ("UNETLoader", "unet_name", "custom_diffusion_model"),
    ),
)
def test_run_probe_preflights_custom_model_override_not_checked_in_default(
    monkeypatch,
    node_class,
    input_name,
    custom_arg,
):
    probe = _load_probe_module()

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_object_info(self):
            return {
                node_class: {
                    "input": {
                        "required": {
                            input_name: [["approved-safe-model"], {}],
                        },
                    },
                },
            }

        def upload_image_bytes(self, _data, filename, **_kwargs):
            raise AssertionError(f"preflight-only must not upload {filename}")

    monkeypatch.setattr(probe, "ComfyUIClient", FakeClient)
    monkeypatch.setattr(probe, "SYSTEM_WORKFLOW_IDS", ("patched_preflight",))
    monkeypatch.setattr(
        probe,
        "_load_workflow",
        lambda _bundle_id: {
            "1": {
                "class_type": node_class,
                "inputs": {input_name: "missing-checked-in-default"},
            },
        },
    )
    dependency_kind = "checkpoint" if node_class == "CheckpointLoaderSimple" else "diffusion_model"
    monkeypatch.setattr(
        probe,
        "_load_manifest",
        lambda _bundle_id: {
            "required_models": [{
                "kind": dependency_kind,
                "name": "missing-checked-in-default",
            }],
            "required_loras": [],
            "required_controlnets": [],
            "required_custom_nodes": [],
        },
    )
    args = SimpleNamespace(
        comfyui_url="http://127.0.0.1:8188",
        request_timeout=5,
        image_size=8,
        only="",
        custom_params=True,
        custom_param_file="",
        custom_param_json="",
        formal_params=False,
        preflight_only=True,
        force_run=False,
        continue_on_fail=False,
        include_heavy=False,
        width=None,
        height=None,
        steps=None,
        prompt=None,
        negative_prompt=None,
        checkpoint_model="",
        no_fetch_outputs=False,
        acceptance_only=False,
        **{custom_arg: "approved-safe-model"},
    )

    report = probe.run_probe(args)

    assert report["ok"] is True
    assert report["results"][0]["status"] == "preflight_pass"
    assert report["results"][0]["preflight"]["missing_models"] == []
    assert report["results"][0]["preflight"]["source_dependency_contract_valid"] is True


def test_run_probe_never_force_runs_invalid_source_dependency_contract(monkeypatch):
    probe = _load_probe_module()

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_object_info(self):
            return {
                "CheckpointLoaderSimple": {
                    "input": {"required": {"ckpt_name": [["model.safetensors"], {}]}},
                },
            }

        def generate_from_workflow(self, *_args, **_kwargs):
            raise AssertionError("an invalid source contract must never be queued")

    workflow = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "model.safetensors"},
        },
    }
    monkeypatch.setattr(probe, "ComfyUIClient", FakeClient)
    monkeypatch.setattr(probe, "SYSTEM_WORKFLOW_IDS", ("invalid_contract",))
    monkeypatch.setattr(probe, "_load_workflow", lambda _bundle_id: workflow)
    monkeypatch.setattr(
        probe,
        "_load_manifest",
        lambda _bundle_id: {
            "required_models": [],
            "required_loras": [],
            "required_controlnets": [],
            "required_custom_nodes": [],
        },
    )
    args = SimpleNamespace(
        comfyui_url="http://127.0.0.1:8188",
        request_timeout=5,
        image_size=8,
        only="",
        custom_params=False,
        custom_param_file="",
        custom_param_json="",
        formal_params=False,
        preflight_only=False,
        force_run=True,
        continue_on_fail=False,
        include_heavy=True,
        width=None,
        height=None,
        steps=None,
        prompt=None,
        negative_prompt=None,
        checkpoint_model="",
        no_fetch_outputs=False,
        acceptance_only=False,
        custom_checkpoint_model="",
        custom_diffusion_model="",
    )

    report = probe.run_probe(args)

    assert report["ok"] is False
    assert report["results"][0]["status"] == "preflight_failed"
    contract = report["results"][0]["preflight"]["dependency_contract"]
    assert contract["ok"] is False
    assert contract["differences"]["models"]["missing_from_manifest"]
