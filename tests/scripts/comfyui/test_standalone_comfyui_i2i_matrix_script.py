import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "comfyui" / "standalone_comfyui_i2i_matrix.py"


def load_matrix_module():
    spec = importlib.util.spec_from_file_location("standalone_comfyui_i2i_matrix", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow_args(**overrides):
    values = {
        "prompt": "positive",
        "negative_prompt": "negative",
        "seed": 1,
        "steps": 4,
        "cfg": 5.0,
        "sampler": "euler",
        "scheduler": "normal",
        "upscale_factor": 1.25,
        "upscale_denoise": 0.2,
        "blend_factor": 0.5,
        "blend_denoise": 0.4,
        "ipadapter_preset": "PLUS (high strength)",
        "outpaint": 128,
        "outpaint_left": None,
        "outpaint_top": None,
        "outpaint_right": None,
        "outpaint_bottom": None,
        "outpaint_feathering": 48,
        "outpaint_source_feather": 16,
        "outpaint_method": "auto",
        "outpaint_seam_prefill": "off",
        "outpaint_prefill_model": "MAT_Places512_G_fp16.safetensors",
        "outpaint_preserve_source": True,
        "outpaint_denoise": 0.9,
        "inpaint_method": "auto",
        "inpaint_noise_mask": True,
        "differential_diffusion": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_standalone_comfyui_i2i_matrix_help_lists_core_modes():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--interactive" in result.stdout
    assert "--comfyui-url" in result.stdout
    assert "--controlnet-type" in result.stdout
    assert "--controlnet-model" in result.stdout
    assert "--upscale-factor" in result.stdout
    assert "--outpaint-top" in result.stdout
    assert "--outpaint-bottom" in result.stdout
    assert "--only-case" in result.stdout
    assert "--source-image-path" in result.stdout
    assert "--case-prompt" in result.stdout
    assert "--case-denoise" in result.stdout
    assert "--mask-shape" in result.stdout
    assert "--inpaint-method" in result.stdout
    assert "--differential-diffusion" in result.stdout
    assert "--blend-image-path" in result.stdout
    assert "--blend-denoise" in result.stdout
    assert "--style-image-path" in result.stdout
    assert "--ipadapter-preset" in result.stdout
    assert "--approve-semantic-review" in result.stdout
    assert "--outpaint-preserve-source" in result.stdout
    assert "kimono_clothes" in result.stdout


def test_standalone_comfyui_i2i_matrix_documents_i2i_cases():
    text = SCRIPT.read_text(encoding="utf-8")
    for keyword in (
        "img2img_redraw_sunset",
        "inpaint_remove_repair",
        "inpaint_replace_edit",
        "outpaint_expand_beach",
        "controlnet_copy_composition",
        "upscale_redraw_imagescale",
        "two_image_blend_mix",
        "ipadapter_style_reference",
        "ipadapter_inpaint_reference",
    ):
        assert keyword in text


def test_upscale_uses_imported_source_dimensions_and_preserves_aspect():
    module = load_matrix_module()
    args = workflow_args()
    expected = module.scaled_dimensions(1080, 1920, args.upscale_factor)
    workflow = module.build_upscale_redraw(
        args,
        {},
        "model.safetensors",
        source_ref={"filename": "source.png"},
        source_size=(1080, 1920),
        prompt="redraw",
        prefix="test",
    )

    assert expected == (1352, 2400)
    assert workflow["5"]["inputs"]["width"] == expected[0]
    assert workflow["5"]["inputs"]["height"] == expected[1]
    assert abs((expected[0] / expected[1]) - (1080 / 1920)) < 0.001


def test_blend_uses_ipadapter_conditioning_not_pixel_overlay():
    module = load_matrix_module()
    workflow = module.build_two_image_blend(
        workflow_args(),
        {},
        "model.safetensors",
        source_ref={"filename": "source.png"},
        blend_ref={"filename": "style.png"},
        prompt="semantic blend",
        prefix="test",
    )

    classes = {node["class_type"] for node in workflow.values()}
    assert "IPAdapterUnifiedLoader" in classes
    assert "IPAdapterStyleComposition" in classes
    assert "ImageBlend" not in classes
    assert workflow["7"]["inputs"]["image_style"] == ["5", 0]
    assert workflow["7"]["inputs"]["image_composition"] == ["6", 0]


def test_outpaint_auto_redraws_generic_checkpoint_padding_before_feathered_source_restore():
    module = load_matrix_module()
    workflow = module.build_outpaint(
        workflow_args(),
        {"VAEEncodeForInpaint": {}},
        "model.safetensors",
        source_ref={"filename": "source.png"},
        source_size=(512, 768),
        prompt="extend scene",
        prefix="test",
    )

    assert workflow["6"]["class_type"] == "VAEEncode"
    assert workflow["6"]["inputs"]["pixels"] == ["5", 0]
    assert "mask" not in workflow["6"]["inputs"]


def test_outpaint_can_explicitly_use_noise_masked_vae_latent_for_inpaint_checkpoints():
    module = load_matrix_module()
    workflow = module.build_outpaint(
        workflow_args(outpaint_method="vae_encode"),
        {"VAEEncodeForInpaint": {}},
        "inpaint.safetensors",
        source_ref={"filename": "source.png"},
        source_size=(512, 768),
        prompt="extend scene",
        prefix="test",
    )

    assert workflow["6"]["class_type"] == "VAEEncodeForInpaint"
    assert workflow["6"]["inputs"]["mask"] == ["5", 1]


def test_outpaint_prefill_is_opt_in_even_when_extension_nodes_are_installed():
    module = load_matrix_module()
    workflow = module.build_outpaint(
        workflow_args(),
        {
            "VAEEncodeForInpaint": {},
            "INPAINT_LoadInpaintModel": {},
            "INPAINT_InpaintWithModel": {},
        },
        "model.safetensors",
        source_ref={"filename": "source.png"},
        source_size=(512, 768),
        prompt="extend scene",
        prefix="test",
    )

    assert workflow["6"]["class_type"] == "VAEEncode"
    assert "INPAINT_LoadInpaintModel" not in {node["class_type"] for node in workflow.values()}


def test_outpaint_prefill_cli_defaults_to_off():
    module = load_matrix_module()
    args = module.parse_args([])

    assert args.outpaint_method == "auto"
    assert args.outpaint_seam_prefill == "off"
    assert args.outpaint_feathering == 64
    assert args.outpaint_source_feather == 128
    assert args.outpaint_denoise == 1.0


def test_outpaint_can_skip_source_composite_for_inpaint_model_blending():
    module = load_matrix_module()
    workflow = module.build_outpaint(
        workflow_args(outpaint_preserve_source=False),
        {"VAEEncodeForInpaint": {}},
        "inpaint.safetensors",
        source_ref={"filename": "source.png"},
        source_size=(512, 768),
        prompt="extend scene",
        prefix="test",
    )

    assert "ImageCompositeMasked" not in {node["class_type"] for node in workflow.values()}
    assert workflow["9"]["class_type"] == "SaveImage"
    assert workflow["9"]["inputs"]["images"] == ["8", 0]


def test_outpaint_prefills_gray_padding_before_latent_sampling_when_available():
    module = load_matrix_module()
    workflow = module.build_outpaint(
        workflow_args(outpaint_seam_prefill="auto", outpaint_prefill_model="mat.safetensors"),
        {
            "VAEEncodeForInpaint": {},
            "INPAINT_LoadInpaintModel": {},
            "INPAINT_InpaintWithModel": {},
        },
        "model.safetensors",
        source_ref={"filename": "source.png"},
        source_size=(512, 768),
        prompt="extend scene",
        prefix="test",
    )

    assert workflow["6"]["class_type"] == "INPAINT_LoadInpaintModel"
    assert workflow["6"]["inputs"]["model_name"] == "mat.safetensors"
    assert workflow["7"]["class_type"] == "INPAINT_InpaintWithModel"
    assert workflow["8"]["class_type"] == "VAEEncode"
    assert workflow["8"]["inputs"]["pixels"] == ["7", 0]
    assert "mask" not in workflow["8"]["inputs"]


def test_outpaint_required_prefill_fails_clearly_when_nodes_are_unavailable():
    module = load_matrix_module()

    with pytest.raises(module.ProbeError, match="outpaint seam prefill requires"):
        module.build_outpaint(
            workflow_args(outpaint_seam_prefill="on"),
            {"VAEEncodeForInpaint": {}},
            "model.safetensors",
            source_ref={"filename": "source.png"},
            source_size=(512, 768),
            prompt="extend scene",
            prefix="test",
        )


def test_outpaint_gray_canvas_is_a_machine_failure(tmp_path):
    module = load_matrix_module()
    source = tmp_path / "source.png"
    bad = tmp_path / "bad.png"
    good = tmp_path / "good.png"
    Image.new("RGB", (32, 48), (220, 40, 30)).save(source)
    padding = {"left": 8, "top": 8, "right": 8, "bottom": 8}

    bad_image = Image.new("RGB", (48, 64), (128, 128, 128))
    bad_image.paste(Image.open(source), (8, 8))
    bad_image.save(bad)
    good_image = Image.new("RGB", (48, 64), (220, 40, 30))
    good_image.paste(Image.open(source), (8, 8))
    good_image.save(good)

    assert module.outpaint_border_check(source, bad, padding)["passed"] is False
    assert module.outpaint_border_check(source, good, padding)["passed"] is True
