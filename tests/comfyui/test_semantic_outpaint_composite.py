from io import BytesIO

import pytest
from PIL import Image

from services.comfyui import execution
from services.comfyui.client import ComfyUIClient, ComfyUIImage
from services.comfyui.semantic_composite import (
    SEMANTIC_BACKGROUND_OUTPUT_NODE_ID,
    SEMANTIC_FOREGROUND_OUTPUT_NODE_ID,
    compose_sam3_outpaint_bundle,
    finalize_sam3_outpaint_result,
)
from tests.comfyui._integration_suite import _await_comfyui_result, _build_app, _init_db


def _png(size, color, *, alpha=None):
    image = Image.new("RGBA", size, color)
    if alpha is not None:
        image.putalpha(alpha)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _foreground_png():
    alpha = Image.new("L", (2, 2), 0)
    alpha.putpixel((0, 0), 255)
    return _png((2, 2), (220, 30, 20, 255), alpha=alpha)


def _paired_result(*, background=None, foreground=None):
    background = background or _png((4, 4), (20, 40, 180, 255))
    foreground = foreground or _foreground_png()
    return {
        "prompt_id": "semantic-prompt",
        "image_ref": {"filename": "background.png", "subfolder": "", "type": "output"},
        "mime_type": "image/png",
        "data": background,
        "images": [
            {
                "image_ref": {"filename": "background.png", "subfolder": "", "type": "output"},
                "mime_type": "image/png",
                "data": background,
                "output_node_id": SEMANTIC_BACKGROUND_OUTPUT_NODE_ID,
            },
            {
                "image_ref": {"filename": "foreground.png", "subfolder": "", "type": "output"},
                "mime_type": "image/png",
                "data": foreground,
                "output_node_id": SEMANTIC_FOREGROUND_OUTPUT_NODE_ID,
            },
        ],
    }


def _erosion_safe_paired_result():
    alpha = Image.new("L", (4, 4), 0)
    for y in range(3):
        for x in range(3):
            alpha.putpixel((x, y), 255)
    foreground = _png((4, 4), (220, 30, 20, 255), alpha=alpha)
    background = _png((6, 6), (20, 40, 180, 255))
    return _paired_result(background=background, foreground=foreground)


class _UploadClient:
    def __init__(self):
        self.uploads = []

    def upload_image_bytes(self, data, filename, *, image_type="input", overwrite=False, subfolder=""):
        self.uploads.append({"data": data, "filename": filename, "image_type": image_type})
        return {"filename": filename, "subfolder": subfolder, "type": image_type}


def _params():
    return {
        "outpaint_workflow_family": "flux_fill_sam3_subject_gguf",
        "filename_prefix": "outpaint",
        "outpaint": {"left": 1, "top": 1, "right": 1, "bottom": 1},
    }


def test_strict_semantic_composite_places_only_foreground_at_the_requested_offset():
    output, report = compose_sam3_outpaint_bundle(
        _png((4, 4), (20, 40, 180, 255)),
        _foreground_png(),
        left=1,
        top=1,
        right=1,
        bottom=1,
        erode_pixels=0,
    )

    with Image.open(BytesIO(output)) as image:
        assert image.size == (4, 4)
        assert image.getpixel((1, 1)) == (220, 30, 20)
        assert image.getpixel((2, 2)) == (20, 40, 180)
    assert report["foreground_coverage"] == 0.25
    assert report["erode_pixels"] == 0


def test_strict_semantic_composite_rejects_unsafe_alpha_or_canvas_before_uploading():
    client = _UploadClient()
    opaque_foreground = _png((2, 2), (220, 30, 20, 255))

    with pytest.raises(RuntimeError, match="覆蓋率不合理"):
        finalize_sam3_outpaint_result(client, _paired_result(foreground=opaque_foreground), _params(), error_cls=RuntimeError)
    assert client.uploads == []

    with pytest.raises(RuntimeError, match="背景尺寸不符"):
        compose_sam3_outpaint_bundle(
            _png((12, 4), (20, 40, 180, 255)),
            _foreground_png(),
            left=1,
            top=1,
            right=1,
            bottom=1,
        )


def test_strict_semantic_composite_accepts_subject_safe_upward_latent_alignment():
    alpha = Image.new("L", (9, 9), 0)
    for y in range(3):
        for x in range(3):
            alpha.putpixel((x, y), 255)
    foreground = _png((9, 9), (220, 30, 20, 255), alpha=alpha)

    output, report = compose_sam3_outpaint_bundle(
        _png((16, 16), (20, 40, 180, 255)),
        foreground,
        left=0,
        top=0,
        right=0,
        bottom=0,
        erode_pixels=0,
    )

    with Image.open(BytesIO(output)) as image:
        assert image.size == (16, 16)
    assert report["background_size"] == [16, 16]


def test_execution_hook_replaces_intermediate_outputs_with_uploaded_semantic_product():
    client = _UploadClient()
    result = execution.generate_image(
        client,
        _params(),
        build_generation_workflow_func=lambda _params: {"9": {"class_type": "SaveImage", "inputs": {}}},
        generate_from_workflow_func=lambda _workflow, **_kwargs: _erosion_safe_paired_result(),
        error_cls=RuntimeError,
    )

    assert len(client.uploads) == 1
    assert client.uploads[0]["image_type"] == "output"
    assert result["image_ref"]["filename"].startswith("outpaint_semantic_outpaint_")
    assert [item["output_node_id"] for item in result["images"]] == ["semantic_composite"]
    assert result["semantic_outpaint"]["source_output_nodes"] == ["9", "124"]
    assert result["semantic_outpaint"]["erode_pixels"] == 2


class _RouteSemanticClient(ComfyUIClient):
    def __init__(self):
        super().__init__("http://semantic-outpaint-test")
        self.workflow = None
        self.uploads = []

    def health_check(self, *, timeout=3):
        return {"ok": True, "system": {"backend": "semantic-test"}}

    def get_capabilities(self):
        return {
            "available_nodes": [
                "LoadImage", "CLIPTextEncode", "FluxGuidance", "KSampler", "UnetLoaderGGUF", "VAELoader",
                "DualCLIPLoader", "InpaintModelConditioning", "DifferentialDiffusion", "EmptyImage", "SolidMask",
                "ConditioningZeroOut", "VAEDecode", "SaveImage", "CheckpointLoaderSimple", "SAM3_Detect",
                "InvertMask", "JoinImageWithAlpha",
            ],
            "models": ["sam3.1_multiplex_fp16.safetensors"],
            "diffusion_models": ["flux1-fill-dev-Q3_K_S.gguf"],
            "clip_models": ["clip_l.safetensors", "t5xxl_fp8_e4m3fn_scaled.safetensors"],
            "vaes": ["ae.safetensors"],
            "samplers": ["euler"],
            "schedulers": ["normal"],
        }

    def get_models(self):
        return ["sam3.1_multiplex_fp16.safetensors"]

    def fetch_image(self, image_ref):
        return ComfyUIImage(
            filename=image_ref.get("filename") or "source.png",
            subfolder=image_ref.get("subfolder") or "",
            type=image_ref.get("type") or "input",
            mime_type="image/png",
            data=_png((80, 80), (255, 255, 255, 255)),
        )

    def generate_from_workflow(
        self,
        workflow,
        *,
        timeout_seconds=1800,
        expected_count=1,
        progress_callback=None,
        extra_data=None,
        fetch_outputs=True,
    ):
        self.workflow = workflow
        if progress_callback:
            progress_callback({"phase": "running", "percent": 60, "detail": "semantic test"})
        return _erosion_safe_paired_result()

    def upload_image_bytes(self, data, filename, *, image_type="input", overwrite=False, subfolder=""):
        self.uploads.append({"data": data, "filename": filename, "image_type": image_type})
        return {"filename": filename, "subfolder": subfolder, "type": image_type}


class _MissingBlankCanvasNodeClient(_RouteSemanticClient):
    def get_capabilities(self):
        payload = super().get_capabilities()
        payload["available_nodes"].remove("EmptyImage")
        return payload


def _direct_outpaint_payload():
    return {
        "generation_mode": "outpaint",
        "model": "ignored-by-flux-fill-route.safetensors",
        "prompt": "an empty beach and sky, no person",
        "source_image_ref": {"filename": "source.png", "subfolder": "", "type": "input"},
        "outpaint_left": 1,
        "outpaint_top": 1,
        "outpaint_right": 1,
        "outpaint_bottom": 1,
        "outpaint_subject_prompt": "full body person",
        "skip_asset_validation": True,
        "confirm_billing": True,
    }


def test_direct_outpaint_route_uses_strict_semantic_bundle_and_delivers_only_composite(tmp_path):
    db_path = tmp_path / "semantic-outpaint.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    backend = _RouteSemanticClient()
    app_client = _build_app(db_path, storage_root, comfyui_client=backend).test_client()

    started = app_client.post(
        "/api/comfyui/generate",
        json=_direct_outpaint_payload(),
    )
    result = _await_comfyui_result(app_client, started)

    assert len(backend.uploads) == 1
    assert result["images"] == [result["image"]]
    assert result["image"]["output_node_id"] == "semantic_composite"
    assert result["image"]["image_ref"]["filename"].startswith("hackme_web_semantic_outpaint_")
    classes = {node["class_type"] for node in backend.workflow.values()}
    assert {"SAM3_Detect", "JoinImageWithAlpha", "EmptyImage", "SolidMask"}.issubset(classes)
    assert "ImageCompositeMasked" not in classes
    assert "ThresholdMask" not in classes
    assert backend.workflow["38"]["inputs"]["pixels"] == ["118", 0]
    blank_inputs = backend.workflow["118"]["inputs"]
    assert blank_inputs["width"] % 8 == 0 and blank_inputs["width"] >= 80
    assert blank_inputs["height"] % 8 == 0 and blank_inputs["height"] >= 80
    assert blank_inputs["batch_size"] == 1 and blank_inputs["color"] == 0


def test_direct_outpaint_route_fails_closed_when_blank_canvas_node_is_unavailable(tmp_path):
    db_path = tmp_path / "semantic-outpaint-missing-node.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    backend = _MissingBlankCanvasNodeClient()
    app_client = _build_app(db_path, storage_root, comfyui_client=backend).test_client()

    response = app_client.post("/api/comfyui/generate", json=_direct_outpaint_payload())

    assert response.status_code == 409
    assert "EmptyImage" in response.get_json()["msg"]
    assert backend.workflow is None
