from services.comfyui.client import ComfyUIClient


def test_flux_fill_outpaint_builder_uses_source_preserving_official_graph():
    workflow = ComfyUIClient("http://fake-comfyui").build_outpaint_workflow({
        "outpaint_workflow_family": "flux_fill_gguf",
        "model": "ignored-checkpoint.safetensors",
        "prompt": "extend the existing scene seamlessly",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 20,
        "cfg": 7.0,
        "sampler_name": "dpmpp_2m",
        "scheduler": "karras",
        "seed": 123,
        "batch_size": 1,
        "source_image_ref": {"filename": "source.png", "subfolder": "", "type": "input"},
        "outpaint": {"left": 128, "top": 64, "right": 128, "bottom": 64, "feathering": 48},
    })

    assert workflow["17"]["inputs"]["image"] == "source.png"
    assert workflow["44"]["inputs"] == {
        "image": ["17", 0], "left": 128, "top": 64, "right": 128, "bottom": 64, "feathering": 48,
    }
    assert workflow["31"]["class_type"] == "UnetLoaderGGUF"
    assert workflow["31"]["inputs"]["unet_name"] == "flux1-fill-dev-Q3_K_S.gguf"
    assert workflow["34"]["inputs"]["type"] == "flux"
    assert workflow["38"]["class_type"] == "InpaintModelConditioning"
    assert workflow["38"]["inputs"]["pixels"] == ["44", 0]
    assert workflow["39"]["class_type"] == "DifferentialDiffusion"
    assert workflow["3"]["inputs"]["model"] == ["39", 0]
    assert workflow["3"]["inputs"]["cfg"] == 1.0
    assert workflow["3"]["inputs"]["denoise"] == 1.0
    assert "ImageCompositeMasked" not in {node["class_type"] for node in workflow.values()}


def test_flux_fill_sam3_subject_outpaint_emits_clean_background_and_rgba_subject_bundle():
    workflow = ComfyUIClient("http://fake-comfyui").build_outpaint_workflow({
        "outpaint_workflow_family": "flux_fill_sam3_subject_gguf",
        "prompt": "extend the existing beach scene seamlessly",
        "outpaint_subject_prompt": "full body person",
        "steps": 20,
        "seed": 123,
        "source_image_ref": {"filename": "source.png", "subfolder": "", "type": "input"},
        "outpaint": {"left": 128, "top": 64, "right": 128, "bottom": 64, "feathering": 48},
        "outpaint_canvas_width": 784,
        "outpaint_canvas_height": 1568,
    })

    assert workflow["115"]["class_type"] == "SAM3_Detect"
    assert workflow["114"]["inputs"]["text"] == "full body person"
    assert workflow["116"]["inputs"]["ckpt_name"] == "sam3.1_multiplex_fp16.safetensors"
    assert workflow["117"]["class_type"] == "InvertMask"
    assert workflow["118"]["inputs"] == {"width": 784, "height": 1568, "batch_size": 1, "color": 0}
    assert workflow["120"]["inputs"] == {"value": 1.0, "width": 784, "height": 1568}
    assert workflow["38"]["inputs"]["pixels"] == ["118", 0]
    assert workflow["38"]["inputs"]["mask"] == ["120", 0]
    assert workflow["121"]["inputs"] == {
        "image": ["17", 0], "alpha": ["117", 0],
    }
    assert workflow["9"]["inputs"]["images"] == ["8", 0]
    assert workflow["124"]["inputs"]["images"] == ["121", 0]
    classes = {node["class_type"] for node in workflow.values()}
    assert "ImageCompositeMasked" not in classes
    assert "ImagePadForOutpaint" not in classes
    assert "GrowMask" not in classes
    assert "ThresholdMask" not in classes
