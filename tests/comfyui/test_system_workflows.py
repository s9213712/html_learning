"""Validate every materialized workflow shipped under workflows/comfyui/.

The canonical source set is workflows/comfyui/origin/*/*/*.json. Each raw
workflow must have a direct system bundle with workflow.json + manifest.json so
first-boot seeding can copy it into runtime/comfyui/.
"""

import json
from pathlib import Path

import pytest

from services.comfyui.template.analyzer import analyze_workflow_json
from services.comfyui.template.seeding import SYSTEM_WORKFLOW_IDS
from services.comfyui.validation.sanitize import sanitize_workflow_json
from services.comfyui.workflow.summary import validate_manifest_dependency_contract
from routes.comfyui_sections.workflow_routes import (
    _apply_qwen_2512_controlnet_pose_mode,
    _is_qwen_image_edit_2509_family,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_DIR = REPO_ROOT / "workflows" / "comfyui"
ORIGIN_DIR = SYSTEM_DIR / "origin"
ASSETS_DIR = SYSTEM_DIR / "assets"

EXPECTED_ORIGIN_WORKFLOW_IDS = {
    "origin_audio_ace_step_15_xl_base",
    "origin_qwen_image_controlnet_2512",
    "origin_sd35_large_canny_controlnet",
    "origin_sd35_large_depth_controlnet",
    "origin_capybara_image_edit",
    "origin_qwen_image_edit_2509",
    "origin_flux_fill_inpaint",
    "origin_one_click_anime_to_real",
    "origin_flux_fill_outpaint",
    "origin_anima_txt2img",
    "origin_sd35_txt2img",
    "origin_sdxl_txt2img",
    "origin_sdxl_gguf_txt2img",
    "origin_zit_txt2img",
    "origin_flux_dev_txt2img",
    "origin_qwen_image_txt2img",
    "origin_netayume_txt2img",
    "origin_compare_2checkpoints",
    "origin_multi_compare_checkpoints_test",
    "origin_sdpose_multi_person",
    "origin_sam3_segmentation",
    "origin_multi_method_upscale",
    "origin_multi_method_upscale_mode_test",
    "origin_capybara_video_edit",
    "origin_wan_vace_inpainting",
    "origin_wan22_14b_i2v_subgraphed",
    "origin_ltx23_t2v",
}

EXPECTED_SYSTEM_WORKFLOW_IDS = EXPECTED_ORIGIN_WORKFLOW_IDS | {
    "origin_flux_fill_inpaint_gguf_q3",
    "origin_flux_fill_outpaint_gguf_q3",
    "origin_qwen_image_edit_2509_anything2real",
    "origin_qwen_image_edit_gguf_lite",
    "origin_sdxl_checkpoint_inpaint",
}

WORKFLOWS_WITH_CLASSIFIED_LORA_OR_CONTROLNET_DEPENDENCIES = {
    "origin_ltx23_t2v",
    "origin_multi_method_upscale",
    "origin_multi_method_upscale_mode_test",
    "origin_one_click_anime_to_real",
    "origin_qwen_image_controlnet_2512",
    "origin_qwen_image_edit_2509",
    "origin_qwen_image_edit_2509_anything2real",
    "origin_qwen_image_txt2img",
    "origin_sd35_large_canny_controlnet",
    "origin_sd35_large_depth_controlnet",
    "origin_wan22_14b_i2v_subgraphed",
    "origin_wan_vace_inpainting",
}


def _system_ids():
    if not SYSTEM_DIR.exists():
        return []
    return sorted(
        p.name
        for p in SYSTEM_DIR.iterdir()
        if p.is_dir()
        and not p.name.startswith("imported_")
        and (p / "workflow.json").is_file()
        and (p / "manifest.json").is_file()
    )


def _manifest(workflow_id):
    return json.loads((SYSTEM_DIR / workflow_id / "manifest.json").read_text(encoding="utf-8"))


def _workflow(workflow_id):
    return json.loads((SYSTEM_DIR / workflow_id / "workflow.json").read_text(encoding="utf-8"))


def test_qwen_2512_pose_controlnet_bypasses_canny_preprocessor():
    workflow = _workflow("origin_qwen_image_controlnet_2512")
    patched, changed = _apply_qwen_2512_controlnet_pose_mode(
        workflow,
        {
            "system_bundle_id": "origin_qwen_image_controlnet_2512",
            "controlnet_type": "pose",
        },
    )

    assert changed is True
    assert patched["131"]["inputs"]["image"] == ["123", 0]
    assert workflow["131"]["inputs"]["image"] == ["122", 0]


def test_qwen_2512_canny_controlnet_keeps_canny_preprocessor():
    workflow = _workflow("origin_qwen_image_controlnet_2512")
    patched, changed = _apply_qwen_2512_controlnet_pose_mode(
        workflow,
        {
            "system_bundle_id": "origin_qwen_image_controlnet_2512",
            "controlnet_type": "canny",
        },
    )

    assert changed is False
    assert patched["131"]["inputs"]["image"] == ["122", 0]


def _normalize_output_kind(kind):
    value = str(kind or "").strip().lower()
    if value in {"image", "images", "preview", "mask"}:
        return "image"
    if value in {"video", "videos", "gif", "gifs", "movie", "movies"}:
        return "video"
    if value in {"audio", "audios", "music", "song", "songs", "sound", "sounds"}:
        return "audio"
    if value in {"file", "files", "binary", "download"}:
        return "file"
    return ""


def _ordered_output_kinds(kinds):
    values = {_normalize_output_kind(kind) for kind in kinds}
    values.discard("")
    return [kind for kind in ("image", "video", "audio", "file") if kind in values]


def _manifest_output_kinds(manifest):
    return _ordered_output_kinds(manifest.get("output_kinds") or [])


def _workflow_output_kinds(workflow):
    output_class_kinds = {
        "SaveImage": "image",
        "PreviewImage": "image",
        "MaskPreview": "image",
        "SaveVideo": "video",
        "VHS_VideoCombine": "video",
        "SaveAudio": "audio",
        "SaveAudioMP3": "audio",
    }
    kinds = []
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        kind = output_class_kinds.get(str(node.get("class_type") or ""))
        if kind:
            kinds.append(kind)
    classes = {
        str((node or {}).get("class_type") or "").strip()
        for node in workflow.values()
        if isinstance(node, dict)
    }
    if "video" in kinds and "SaveImage" not in classes:
        kinds = [kind for kind in kinds if kind != "image"]
    return _ordered_output_kinds(kinds)


def _workflow_media_refs(workflow_id):
    media_inputs = {
        "LoadImage": ("image", "image"),
        "LoadImageMask": ("image", "image"),
        "LoadVideo": ("file", "video"),
        "VHS_LoadVideo": ("file", "video"),
    }
    media_exts = (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".avi")
    for node_id, node in _workflow(workflow_id).items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        media_input = media_inputs.get(class_type)
        if not media_input:
            continue
        input_name, media_kind = media_input
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        value = inputs.get(input_name)
        if isinstance(value, str) and value.lower().endswith(media_exts):
            yield {
                "workflow_id": workflow_id,
                "node_id": str(node_id),
                "class_type": class_type,
                "input_name": input_name,
                "media_kind": media_kind,
                "filename": Path(value).name,
            }


def _ui_fields(manifest):
    return {
        field["id"]: field
        for panel in (manifest.get("ui") or {}).get("panels", [])
        for field in panel.get("fields", [])
    }


def test_system_registry_matches_origin_workflow_ids():
    assert set(SYSTEM_WORKFLOW_IDS) == EXPECTED_SYSTEM_WORKFLOW_IDS


def test_qwen_edit_reference_policy_applies_to_2509_family_workflows():
    assert _is_qwen_image_edit_2509_family("origin_qwen_image_edit_2509")
    assert _is_qwen_image_edit_2509_family("origin_qwen_image_edit_2509_anything2real")
    assert not _is_qwen_image_edit_2509_family("origin_qwen_image_controlnet_2512")
    assert not _is_qwen_image_edit_2509_family("origin_qwen_image_edit_gguf_lite")


def test_system_dir_has_materialized_origin_workflows():
    ids = set(_system_ids())
    missing = EXPECTED_SYSTEM_WORKFLOW_IDS - ids
    assert not missing, f"missing system workflows: {missing}"


def test_every_origin_workflow_has_converted_bundle():
    origin_paths = {
        path.relative_to(ORIGIN_DIR).as_posix()
        for path in sorted(ORIGIN_DIR.glob("*/*/*.json"))
    }
    converted_paths = {
        _manifest(workflow_id)["conversion"]["source_path"]
        for workflow_id in EXPECTED_ORIGIN_WORKFLOW_IDS
    }
    assert converted_paths == origin_paths


def test_system_workflow_default_media_assets_are_present():
    missing = []
    duplicate = []
    checked = []
    for workflow_id in sorted(EXPECTED_SYSTEM_WORKFLOW_IDS):
        for ref in _workflow_media_refs(workflow_id):
            checked.append(ref)
            matches = sorted(
                path
                for path in ASSETS_DIR.rglob("*")
                if path.is_file() and path.name == ref["filename"]
            )
            label = (
                f"{ref['workflow_id']} node {ref['node_id']} "
                f"{ref['class_type']}.{ref['input_name']} -> {ref['filename']}"
            )
            if not matches:
                missing.append(label)
            elif len(matches) > 1:
                duplicate.append(label)
            elif matches[0].stat().st_size <= 0:
                missing.append(f"{label} (empty file)")

    assert checked, "no system workflow media inputs were discovered"
    assert not missing, "missing default media assets:\n" + "\n".join(missing)
    assert not duplicate, "duplicate default media asset basenames:\n" + "\n".join(duplicate)


@pytest.mark.parametrize("workflow_id", sorted(EXPECTED_SYSTEM_WORKFLOW_IDS))
def test_system_workflow_files_present(workflow_id):
    base = SYSTEM_DIR / workflow_id
    assert (base / "workflow.json").is_file(), f"{workflow_id}/workflow.json missing"
    assert (base / "manifest.json").is_file(), f"{workflow_id}/manifest.json missing"
    assert (base / "README.md").is_file(), f"{workflow_id}/README.md missing"


@pytest.mark.parametrize("workflow_id", sorted(EXPECTED_SYSTEM_WORKFLOW_IDS))
def test_system_workflow_passes_sanitize_and_analyze(workflow_id):
    sanitized = sanitize_workflow_json(_workflow(workflow_id))["workflow_json"]
    analysis = analyze_workflow_json(sanitized)
    assert not analysis.denied_classes, f"{workflow_id} uses denied classes: {analysis.denied_classes}"


@pytest.mark.parametrize("workflow_id", sorted(EXPECTED_SYSTEM_WORKFLOW_IDS))
def test_system_manifest_schema_basics(workflow_id):
    manifest = _manifest(workflow_id)
    assert manifest["schema_version"] == 1
    assert manifest["id"] == workflow_id
    assert manifest["workflow_file"] == "workflow.json"
    expected_source = "project_local" if workflow_id == "origin_sdxl_checkpoint_inpaint" else "official_origin"
    assert manifest["source"] == expected_source
    assert manifest["origin_source_path"].endswith(".json")
    assert isinstance(manifest.get("name"), str) and manifest["name"]
    assert isinstance(manifest.get("description"), str)
    assert manifest.get("source_format") in {"api_prompt", "ui_graph"}
    assert manifest.get("output_kinds")
    assert (manifest.get("default_params") or {}).get("generation_mode")
    ui = manifest.get("ui") or {}
    assert isinstance(ui.get("panels"), list) and ui["panels"], (
        f"{workflow_id} has no UI panels"
    )


@pytest.mark.parametrize("workflow_id", sorted(EXPECTED_SYSTEM_WORKFLOW_IDS))
def test_manifest_output_kinds_match_workflow_output_nodes(workflow_id):
    manifest = _manifest(workflow_id)
    workflow = _workflow(workflow_id)

    assert _manifest_output_kinds(manifest) == _workflow_output_kinds(workflow)


@pytest.mark.parametrize("workflow_id", sorted(EXPECTED_SYSTEM_WORKFLOW_IDS))
def test_system_manifest_matches_graph_loader_dependencies_and_explicit_custom_node_packages(workflow_id):
    result = validate_manifest_dependency_contract(_workflow(workflow_id), _manifest(workflow_id))

    assert result["scope"]["custom_nodes"] == "explicit_class_to_package_mapping_only"
    assert result["custom_node_evidence"]["limitation"]
    assert result["ok"] is True, json.dumps(result, ensure_ascii=False, indent=2)


def test_twelve_lora_controlnet_workflows_are_not_required_model_mismatches():
    classified = set()
    for workflow_id in sorted(EXPECTED_SYSTEM_WORKFLOW_IDS):
        result = validate_manifest_dependency_contract(_workflow(workflow_id), _manifest(workflow_id))
        graph = result["graph"]
        non_model_names = set(graph["loras"]) | set(graph["controlnets"])
        model_names = {item["name"] for item in graph["models"]}
        if non_model_names:
            classified.add(workflow_id)
        assert not (model_names & non_model_names), workflow_id
        assert result["ok"] is True, json.dumps(result, ensure_ascii=False, indent=2)

    assert classified == WORKFLOWS_WITH_CLASSIFIED_LORA_OR_CONTROLNET_DEPENDENCIES


def test_frontend_workflow_routes_infer_output_kinds_from_output_nodes_only():
    route_sources = [
        REPO_ROOT / "routes/comfyui_sections/workflow_routes.py",
        REPO_ROOT / "routes/comfyui_sections/template_routes.py",
    ]
    for path in route_sources:
        source = path.read_text(encoding="utf-8")
        assert '"SaveVideo": "video"' in source
        assert '"SaveAudioMP3"' in source
        assert 'found.discard("image")' in source
        assert 'any("video" in name.lower()' not in source
        assert 'token in name.lower()' not in source


@pytest.mark.parametrize("workflow_id", sorted(EXPECTED_ORIGIN_WORKFLOW_IDS))
def test_origin_conversion_status_is_recorded(workflow_id):
    conversion = _manifest(workflow_id).get("conversion") or {}
    assert conversion["structural_status"] == "pass"
    assert conversion["source_format"] in {"api_prompt", "ui_graph"}
    assert conversion["allowlist_status"] in {"allowlisted", "custom_nodes_required"}
    if conversion["allowlist_status"] == "allowlisted":
        assert conversion["unknown_classes"] == []
    else:
        assert conversion["unknown_classes"]


def test_all_origin_workflows_clear_static_allowlist():
    statuses = {
        _manifest(workflow_id)["conversion"]["allowlist_status"]
        for workflow_id in EXPECTED_ORIGIN_WORKFLOW_IDS
    }
    assert statuses == {"allowlisted"}


def test_anima_origin_workflow_defaults_keep_model_stack_aligned():
    workflow = _workflow("origin_anima_txt2img")
    manifest = _manifest("origin_anima_txt2img")
    defaults = manifest["default_params"]

    assert workflow["68"]["inputs"]["unet_name"] == "anima-preview3-base.safetensors"
    assert defaults["model"] == "anima-preview3-base.safetensors"
    assert defaults["diffusion_model"] == "anima-preview3-base.safetensors"
    assert defaults["clip"] == "qwen_3_06b_base.safetensors"
    assert defaults["vae"] == "qwen_image_vae.safetensors"
    assert defaults["prompt"].startswith("masterpiece, best quality")
    assert defaults["negative_prompt"].startswith("worst quality, low quality")


def test_text_to_audio_origin_workflow_uses_text_to_speech_mode():
    manifest = _manifest("origin_audio_ace_step_15_xl_base")
    workflow = _workflow("origin_audio_ace_step_15_xl_base")
    classes = {node["class_type"] for node in workflow.values()}

    assert manifest["default_params"]["generation_mode"] == "t2s"
    assert manifest["output_kinds"] == ["music"]
    assert "TextEncodeAceStepAudio1.5" in classes


@pytest.mark.parametrize("workflow_id", sorted(EXPECTED_SYSTEM_WORKFLOW_IDS))
def test_origin_manifest_core_numeric_defaults_are_usable(workflow_id):
    defaults = _manifest(workflow_id)["default_params"]

    for key in ("width", "height", "batch_size", "steps", "cfg"):
        assert isinstance(defaults.get(key), (int, float)), f"{workflow_id}.{key} is not numeric"
        assert defaults[key] > 0, f"{workflow_id}.{key} should not use an internal zero fallback"
    assert isinstance(defaults.get("seed"), (int, float)), f"{workflow_id}.seed is not numeric"
    assert defaults["seed"] >= 0


def test_origin_defaults_preserve_zero_seed_when_origin_uses_zero():
    assert _manifest("origin_audio_ace_step_15_xl_base")["default_params"]["seed"] == 0
    assert _manifest("origin_netayume_txt2img")["default_params"]["seed"] == 0
    assert _manifest("origin_wan22_14b_i2v_subgraphed")["default_params"]["seed"] == 0


def test_qwen_controlnet_defaults_follow_graph_roles_and_switches():
    defaults = _manifest("origin_qwen_image_controlnet_2512")["default_params"]

    assert defaults["prompt"].startswith("A woman with curly hair")
    assert defaults["negative_prompt"].startswith("低分辨率")
    assert defaults["steps"] == 50
    assert defaults["cfg"] == 4


def test_non_sdxl_full_text_defaults_do_not_trigger_plain_embedding_names():
    for workflow_id in (
        "origin_flux_dev_txt2img",
        "origin_zit_txt2img",
        "origin_qwen_image_txt2img",
    ):
        manifest = _manifest(workflow_id)
        workflow = _workflow(workflow_id)
        prompt = manifest["default_params"]["prompt"]
        text_values = [
            str((node.get("inputs") or {}).get("text") or "")
            for node in workflow.values()
            if isinstance(node, dict)
        ]

        assert "by ogipote" not in prompt
        assert all("by ogipote" not in text for text in text_values)
        assert "embedding:" not in prompt.lower()


def test_qwen_edit_defaults_keep_reference_node_optional():
    manifest = _manifest("origin_qwen_image_edit_2509")
    workflow = _workflow("origin_qwen_image_edit_2509")
    defaults = manifest["default_params"]
    text_panel = next(panel for panel in manifest["ui"]["panels"] if panel["id"] == "text")
    fields = text_panel["fields"]

    assert defaults["prompt"] == "Replace the cat with a dalmatian, keeping the environment and scene consistent"
    assert defaults["steps"] == 4
    assert defaults["cfg"] == 1
    assert any(
        field["label"].startswith("負面提示詞")
        and field["class_type"] == "TextEncodeQwenImageEditPlus"
        for field in fields
    )
    assert any(
        field["label"].startswith("正向提示詞")
        and field["class_type"] == "TextEncodeQwenImageEditPlus"
        for field in fields
    )
    assert workflow["79"]["class_type"] == "LoadImage"
    assert workflow["79"]["inputs"]["image"] == "image_qwen_image_edit_2509_reference_image.png"
    assert "image2" not in workflow["494"]["inputs"]


def test_wrapped_qwen_prompt_text_fields_are_visible_and_labeled():
    manifest = _manifest("origin_one_click_anime_to_real")
    text_panel = next(panel for panel in manifest["ui"]["panels"] if panel["id"] == "text")
    labels = {field["id"]: field["label"] for field in text_panel["fields"]}

    assert manifest["default_params"]["prompt"].startswith("根据图像，动漫转写实真人")
    assert labels["node:342:prompt"] == "正向提示詞"
    assert labels["node:333:prompt"] == "負面提示詞"


def test_one_click_anime_to_real_checkpoint_field_is_runtime_editable():
    manifest = _manifest("origin_one_click_anime_to_real")
    fields = _ui_fields(manifest)
    field = fields["node:312:ckpt_name"]

    assert field["class_type"] == "CheckpointLoaderSimple"
    assert field["input_name"] == "ckpt_name"
    assert field["current_value"] == "ZIT-AIO\\Full-Red-Z-image_15AIO_ bf16.safetensors"
    assert field["required"] is True
    assert field.get("locked") is not True
    assert field.get("read_only") is not True
    assert field.get("lock_reason") != "template_default_model"


def test_multi_method_upscale_keeps_origin_first_and_second_upscale_stages():
    workflow = _workflow("origin_multi_method_upscale")
    manifest = _manifest("origin_multi_method_upscale")
    fields = _ui_fields(manifest)
    required = {(item["kind"], item["name"]) for item in manifest["required_models"]}

    assert workflow["3"]["_meta"]["title"] == "Origin"
    assert workflow["61"]["_meta"]["title"] == "一次放大"
    assert workflow["63"]["_meta"]["title"] == "一次放大"
    assert workflow["77"]["_meta"]["title"] == "二次放大"
    assert workflow["63"]["inputs"]["upscale_method"] == "nearest-exact"
    assert workflow["63"]["inputs"]["scale_by"] == 2
    assert workflow["77"]["inputs"]["model_name"] == "ESRGAN\\OmniSR_X4_DIV2K.safetensors"

    assert fields["node:61:denoise"]["label"] == "Denoise（一次放大）"
    assert fields["node:63:upscale_method"]["label"] == "Latent 放大方式（一次放大）"
    assert fields["node:63:scale_by"]["label"] == "Latent 放大倍率（一次放大）"
    assert fields["node:77:model_name"]["label"] == "放大 / Upscale 模型（二次放大）"
    assert ("embedding", "lazy") not in required
    assert ("embedding", "lazy series\\IL\\lazyneg") in required


def test_multi_method_upscale_mode_test_exposes_mode_specific_labels():
    workflow = _workflow("origin_multi_method_upscale_mode_test")
    manifest = _manifest("origin_multi_method_upscale_mode_test")
    fields = _ui_fields(manifest)

    assert manifest["name"] == "Multi-Method Upscale Utility - Mode Test"
    assert manifest["default_params"]["upscale_mode"] == "combined_upscale"
    assert workflow["61"]["_meta"]["title"] == "Latent 放大"
    assert workflow["63"]["_meta"]["title"] == "Latent 放大"
    assert workflow["77"]["_meta"]["title"] == "模型放大"
    assert fields["node:61:denoise"]["label"] == "Denoise（Latent 重繪）"
    assert fields["node:63:upscale_method"]["label"] == "Latent 放大方式"
    assert fields["node:63:scale_by"]["label"] == "Latent 放大倍率"
    assert fields["node:77:model_name"]["label"] == "放大 / Upscale 模型（模型放大）"


def test_ltx23_latent_upscale_model_is_not_treated_as_regular_upscaler():
    manifest = _manifest("origin_ltx23_t2v")
    fields = _ui_fields(manifest)
    required = {(item["kind"], item["name"]) for item in manifest["required_models"]}

    assert manifest["default_params"]["upscale_model"] == ""
    assert ("latent_upscale", "ltx-2.3-spatial-upscaler-x2-1.1.safetensors") in required
    assert ("upscale", "ltx-2.3-spatial-upscaler-x2-1.1.safetensors") not in required
    assert fields["node:303:model_name"]["label"] == "Latent 放大模型"
    assert fields["node:303:model_name"]["locked"] is True


def test_wan_vace_inpainting_exposes_load_video_input():
    manifest = _manifest("origin_wan_vace_inpainting")
    fields = _ui_fields(manifest)
    video_panel = next(panel for panel in manifest["ui"]["panels"] if panel["id"] == "video")

    assert video_panel["label"] == "影片輸入"
    assert fields["node:209:file"]["class_type"] == "LoadVideo"
    assert fields["node:209:file"]["label"] == "載入影片"
    assert fields["node:209:file"]["input_type"] == "video_file_picker"


def test_sdpose_multi_person_exposes_required_group_photo_input():
    manifest = _manifest("origin_sdpose_multi_person")
    workflow = _workflow("origin_sdpose_multi_person")
    fields = _ui_fields(manifest)
    image_panel = next(panel for panel in manifest["ui"]["panels"] if panel["id"] == "image")
    numeric_panel = next(panel for panel in manifest["ui"]["panels"] if panel["id"] == "numeric")

    assert image_panel["label"] == "圖片輸入"
    assert numeric_panel["label"] == "進階數值參數"
    assert workflow["679"]["class_type"] == "LoadImage"
    assert workflow["679"]["inputs"]["image"] == "group_photo.png"
    assert workflow["697"]["inputs"]["max_detections"] == 1
    assert fields["node:679:image"]["class_type"] == "LoadImage"
    assert fields["node:679:image"]["label"] == "上傳圖片"
    assert fields["node:679:image"]["required"] is True
    assert fields["node:697:max_detections"]["label"] == "最大偵測數"
    assert fields["node:697:max_detections"]["input_type"] == "number"
    assert fields["node:697:threshold"]["label"] == "人物偵測門檻"
    assert fields["node:697:class_name"]["label"] == "偵測類別"
    assert fields["node:694:draw_body"]["input_type"] == "checkbox"
    assert fields["node:694:draw_hands"]["input_type"] == "checkbox"
    assert fields["node:694:score_threshold"]["label"] == "姿態分數門檻"
    assert fields["node:692:batch_size"]["label"] == "姿態批次大小"


def test_compare_workflows_use_checkpoint_builtin_vae():
    for workflow_id in ("origin_compare_2checkpoints", "origin_multi_compare_checkpoints_test"):
        workflow = _workflow(workflow_id)
        manifest = _manifest(workflow_id)
        serialized = json.dumps({"workflow": workflow, "manifest": manifest}, ensure_ascii=False)
        assert "vae-ft-mse-840000-ema-pruned.safetensors" not in serialized
        assert "11" not in workflow
        assert not any(item.get("kind") == "vae" for item in manifest.get("required_models", []))
        assert not (manifest.get("default_params", {}) or {}).get("vae")
