"""§10 5-gate run regression for the ComfyUI template importer."""

import pytest

from services.comfyui.template.capability import reset_object_info_cache
from services.comfyui.template.run_gate import (
    RunGateFailure,
    run_workflow_through_gates,
)


@pytest.fixture(autouse=True)
def _isolate_object_info_cache():
    """Capability cache is keyed by client.base_url; reset so each test's
    stub client doesn't see another test's cached /object_info payload."""
    reset_object_info_cache()
    yield
    reset_object_info_cache()


TXT2IMG = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "cat", "clip": ["4", 1]}},
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


def _stub_client(*, classes, models=None, vaes=None, diffusion_models=None, clips=None, latent_upscale_models=None, embeddings=None):
    info = {cls: {"input": {"required": {}}} for cls in classes}
    if "CheckpointLoaderSimple" in info and models is not None:
        info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [list(models)]
    if "VAELoader" in info and vaes is not None:
        info["VAELoader"]["input"]["required"]["vae_name"] = [list(vaes)]
    if "UNETLoader" in info and diffusion_models is not None:
        info["UNETLoader"]["input"]["required"]["unet_name"] = [list(diffusion_models)]
    if "CLIPLoader" in info and clips is not None:
        info["CLIPLoader"]["input"]["required"]["clip_name"] = [list(clips)]
    if "LatentUpscaleModelLoader" in info and latent_upscale_models is not None:
        info["LatentUpscaleModelLoader"]["input"]["required"]["model_name"] = [list(latent_upscale_models)]
    if "KSampler" in info:
        info["KSampler"]["input"]["required"]["sampler_name"] = [["euler"]]
        info["KSampler"]["input"]["required"]["scheduler"] = [["normal"]]

    class _Stub:
        base_url = "http://stub"
        def get_object_info(self):
            return info
        def get_embeddings(self):
            return list(embeddings or [])
    return _Stub()


def _ok_client():
    return _stub_client(
        classes={
            "CheckpointLoaderSimple", "EmptyLatentImage", "CLIPTextEncode",
            "KSampler", "VAEDecode", "SaveImage", "LoadImage", "LoadImageMask",
            "LoadVideo",
        },
        models=["v1-5.safetensors"],
    )


def _stub_upload(*, file_row, target_filename, run_id):
    return {"filename": target_filename, "subfolder": run_id, "type": "input"}


# Required inputs for txt2img: KSampler {seed, steps, cfg, denoise,
# sampler_name, scheduler}, EmptyLatentImage {width, height, batch_size},
# CLIPTextEncode×2 {text}, CheckpointLoaderSimple {ckpt_name}.
def _full_user_inputs(*, prompt="cat", negative="low quality"):
    return {
        "3": {
            "seed": 42,
            "steps": 20,
            "cfg": 7.5,
            "denoise": 1.0,
            "sampler_name": "euler",
            "scheduler": "normal",
        },
        "4": {"ckpt_name": "v1-5.safetensors"},
        "5": {"width": 512, "height": 512, "batch_size": 1},
        "6": {"text": prompt},
        "7": {"text": negative},
    }


def test_happy_path_all_gates_pass():
    result = run_workflow_through_gates(
        raw_workflow=TXT2IMG,
        user_inputs=_full_user_inputs(),
        image_field_assignments={},
        actor={"id": 1, "username": "alice"},
        user_id=1,
        run_id="run123",
        conn=None,
        comfyui_client=_ok_client(),
        upload_callback=_stub_upload,
    )
    # SaveImage prefix rewritten to hackme/<user>/<run_id>
    assert result.workflow["9"]["inputs"]["filename_prefix"] == "hackme/1/run123"
    # User-provided seed propagated
    assert result.workflow["3"]["inputs"]["seed"] == 42
    # KSampler text inputs landed
    assert result.workflow["6"]["inputs"]["text"] == "cat"
    assert result.capability.overall == "SUPPORTED"
    assert result.audit_metadata["overall"] == "SUPPORTED"


def test_boolean_template_inputs_are_validated_and_applied():
    workflow = {
        "1": {
            "class_type": "SDPoseDrawKeypoints",
            "inputs": {
                "keypoints": ["0", 0],
                "draw_body": True,
                "draw_hands": True,
                "draw_face": True,
                "draw_feet": True,
                "stick_width": 4,
                "face_point_size": 2,
                "score_threshold": 0.5,
            },
        },
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["1", 0]}},
    }

    result = run_workflow_through_gates(
        raw_workflow=workflow,
        user_inputs={
            "1": {
                "draw_body": False,
                "draw_hands": True,
                "draw_face": False,
                "draw_feet": True,
                "stick_width": 5,
                "face_point_size": 3,
                "score_threshold": 0.7,
            }
        },
        image_field_assignments={},
        actor={"id": 1, "username": "alice"},
        user_id=1,
        run_id="pose123",
        conn=None,
        comfyui_client=_stub_client(classes={"SDPoseDrawKeypoints", "SaveImage"}),
        upload_callback=_stub_upload,
    )

    assert result.workflow["1"]["inputs"]["draw_body"] is False
    assert result.workflow["1"]["inputs"]["draw_face"] is False
    assert result.workflow["1"]["inputs"]["stick_width"] == 5


def test_save_audio_filename_prefix_is_system_rewritten_not_required():
    workflow = {
        **TXT2IMG,
        "107": {"class_type": "SaveAudioMP3", "inputs": {"audio": ["8", 0], "filename_prefix": "audio/ace_step", "quality": "V0"}},
    }
    result = run_workflow_through_gates(
        raw_workflow=workflow,
        user_inputs=_full_user_inputs(),
        image_field_assignments={},
        actor={"id": 1, "username": "alice"},
        user_id=1,
        run_id="ace123",
        conn=None,
        comfyui_client=_stub_client(
            classes={
                "CheckpointLoaderSimple", "EmptyLatentImage", "CLIPTextEncode",
                "KSampler", "VAEDecode", "SaveImage", "SaveAudioMP3",
            },
            models=["v1-5.safetensors"],
        ),
        upload_callback=_stub_upload,
    )

    assert result.workflow["107"]["inputs"]["filename_prefix"] == "hackme/1/ace123"
    assert result.workflow["107"]["inputs"]["quality"] == "V0"


def test_locked_model_loader_fields_are_optional_but_can_be_overridden():
    workflow = {
        "61": {"class_type": "CLIPLoader", "inputs": {"clip_name": "missing_clip.safetensors"}},
        "62": {"class_type": "VAELoader", "inputs": {"vae_name": "missing_vae.safetensors"}},
        "64": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "65": {"class_type": "CLIPTextEncode", "inputs": {"text": "low quality", "clip": ["61", 0]}},
        "66": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 42,
                "steps": 30,
                "cfg": 4,
                "denoise": 1,
                "sampler_name": "euler",
                "scheduler": "normal",
                "model": ["68", 0],
                "positive": ["67", 0],
                "negative": ["65", 0],
                "latent_image": ["64", 0],
            },
        },
        "67": {"class_type": "CLIPTextEncode", "inputs": {"text": "anime", "clip": ["61", 0]}},
        "68": {"class_type": "UNETLoader", "inputs": {"unet_name": "missing_unet.safetensors"}},
        "69": {"class_type": "VAEDecode", "inputs": {"samples": ["66", 0], "vae": ["62", 0]}},
        "70": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["69", 0]}},
    }
    client = _stub_client(
        classes={"CLIPLoader", "VAELoader", "EmptyLatentImage", "CLIPTextEncode", "KSampler", "UNETLoader", "VAEDecode", "SaveImage"},
        vaes=["anime_vae.safetensors"],
        diffusion_models=["anima-preview2.safetensors"],
        clips=["qwen_3_06b_base.safetensors"],
    )
    result = run_workflow_through_gates(
        raw_workflow=workflow,
        user_inputs={
            "61": {"clip_name": "qwen_3_06b_base.safetensors"},
            "62": {"vae_name": "anime_vae.safetensors"},
            "64": {"width": 1024, "height": 1024, "batch_size": 1},
            "65": {"text": "low quality"},
            "66": {"seed": 42, "steps": 30, "cfg": 4, "denoise": 1, "sampler_name": "euler", "scheduler": "normal"},
            "67": {"text": "anime"},
            "68": {"unet_name": "anima-preview2.safetensors"},
        },
        image_field_assignments={},
        actor={"id": 1, "username": "alice"},
        user_id=1,
        run_id="run123",
        conn=None,
        comfyui_client=client,
        upload_callback=_stub_upload,
    )

    assert result.workflow["61"]["inputs"]["clip_name"] == "qwen_3_06b_base.safetensors"
    assert result.workflow["62"]["inputs"]["vae_name"] == "anime_vae.safetensors"
    assert result.workflow["68"]["inputs"]["unet_name"] == "anima-preview2.safetensors"


def test_workflow_user_inputs_normalize_embedding_shortcut_syntax():
    user_inputs = _full_user_inputs(prompt="portrait, <embeddings:badhandv4.pt>")
    result = run_workflow_through_gates(
        raw_workflow=TXT2IMG,
        user_inputs=user_inputs,
        image_field_assignments={},
        actor={"id": 1, "username": "alice"},
        user_id=1,
        run_id="run123",
        conn=None,
        comfyui_client=_stub_client(
            classes={
                "CheckpointLoaderSimple", "EmptyLatentImage", "CLIPTextEncode",
                "KSampler", "VAEDecode", "SaveImage",
            },
            models=["v1-5.safetensors"],
            embeddings=["badhandv4.pt"],
        ),
        upload_callback=_stub_upload,
    )

    assert result.workflow["6"]["inputs"]["text"] == "portrait, embedding:badhandv4.pt"


def test_gate1_rejects_ui_graph_format():
    """Gate 1 sanitize must reject UI graph (the layered defense the spec calls for)."""
    ui_graph = {"nodes": [], "links": []}  # not API format
    with pytest.raises(RunGateFailure) as excinfo:
        run_workflow_through_gates(
            raw_workflow=ui_graph,
            user_inputs={},
            image_field_assignments={},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=_ok_client(),
            upload_callback=_stub_upload,
        )
    assert excinfo.value.gate == 1
    assert excinfo.value.stage.startswith("gate1_")


def test_gate2_capability_unsupported_blocks():
    """Local ComfyUI missing a class → Gate 2 fail."""
    minimal_client = _stub_client(
        classes={"CheckpointLoaderSimple", "KSampler"},
        models=["v1-5.safetensors"],
    )
    with pytest.raises(RunGateFailure) as excinfo:
        run_workflow_through_gates(
            raw_workflow=TXT2IMG,
            user_inputs=_full_user_inputs(),
            image_field_assignments={},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=minimal_client,
            upload_callback=_stub_upload,
        )
    assert excinfo.value.gate == 2
    assert excinfo.value.stage == "gate2_capability"


def test_gate2_missing_models_blocks_after_user_inputs_are_applied():
    """Class supported but final model file not on disk → Gate 2 model fail."""
    no_model_client = _stub_client(
        classes={
            "CheckpointLoaderSimple", "EmptyLatentImage", "CLIPTextEncode",
            "KSampler", "VAEDecode", "SaveImage",
        },
        models=["other.safetensors"],
    )
    with pytest.raises(RunGateFailure) as excinfo:
        run_workflow_through_gates(
            raw_workflow=TXT2IMG,
            user_inputs=_full_user_inputs(),
            image_field_assignments={},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=no_model_client,
            upload_callback=_stub_upload,
        )
    assert excinfo.value.gate == 2
    assert excinfo.value.stage == "gate2_models"


def test_gate2_rechecks_model_fields_after_user_inputs():
    """A stale template UI model override must fail before the job reaches ComfyUI."""
    user_inputs = _full_user_inputs()
    user_inputs["4"]["ckpt_name"] = "missing-after-patch.safetensors"
    with pytest.raises(RunGateFailure) as excinfo:
        run_workflow_through_gates(
            raw_workflow=TXT2IMG,
            user_inputs=user_inputs,
            image_field_assignments={},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=_ok_client(),
            upload_callback=_stub_upload,
        )
    assert excinfo.value.gate == 2
    assert excinfo.value.stage == "gate2_models"
    assert excinfo.value.audit_detail["post_user_inputs"] is True
    assert excinfo.value.audit_detail["missing_models"] == {
        "ckpt": ["missing-after-patch.safetensors"]
    }


def test_run_gate_rewrites_latent_upscale_model_to_local_subfolder_option():
    workflow = {
        "303": {
            "class_type": "LatentUpscaleModelLoader",
            "inputs": {"model_name": "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"},
        },
    }
    client = _stub_client(
        classes={"LatentUpscaleModelLoader"},
        latent_upscale_models=["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"],
    )

    result = run_workflow_through_gates(
        raw_workflow=workflow,
        user_inputs={},
        image_field_assignments={},
        actor={"id": 1},
        user_id=1,
        run_id="r",
        conn=None,
        comfyui_client=client,
        upload_callback=_stub_upload,
    )

    assert result.workflow["303"]["inputs"]["model_name"] == "3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"


def test_gate3_allowlist_blocks_unknown_class():
    """Unknown class type passes capability if local ComfyUI has it, but Gate 3 still rejects."""
    bad = {**TXT2IMG, "10": {"class_type": "FancyCommunityNode", "inputs": {}}}
    client = _stub_client(
        classes={
            "CheckpointLoaderSimple", "EmptyLatentImage", "CLIPTextEncode",
            "KSampler", "VAEDecode", "SaveImage", "FancyCommunityNode",
        },
        models=["v1-5.safetensors"],
    )
    with pytest.raises(RunGateFailure) as excinfo:
        run_workflow_through_gates(
            raw_workflow=bad,
            user_inputs={},
            image_field_assignments={},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=client,
            upload_callback=_stub_upload,
        )
    assert excinfo.value.gate == 3
    assert excinfo.value.stage == "gate3_allowlist"
    assert "FancyCommunityNode" in excinfo.value.audit_detail["unknown_or_denied"]


def test_gate4_required_input_unfilled_blocks():
    """Don't supply seed → Gate 4 says missing inputs."""
    user_inputs = _full_user_inputs()
    del user_inputs["3"]["seed"]
    with pytest.raises(RunGateFailure) as excinfo:
        run_workflow_through_gates(
            raw_workflow=TXT2IMG,
            user_inputs=user_inputs,
            image_field_assignments={},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=_ok_client(),
            upload_callback=_stub_upload,
        )
    assert excinfo.value.gate == 4
    assert excinfo.value.stage == "gate4_inputs"
    assert ("3", "seed") in excinfo.value.audit_detail["missing"]


def test_gate4_numeric_field_string_value_rejected():
    """Sending "20" instead of 20 for KSampler.steps → Gate 4 type error."""
    user_inputs = _full_user_inputs()
    user_inputs["3"]["steps"] = "20"  # wrong type
    with pytest.raises(RunGateFailure) as excinfo:
        run_workflow_through_gates(
            raw_workflow=TXT2IMG,
            user_inputs=user_inputs,
            image_field_assignments={},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=_ok_client(),
            upload_callback=_stub_upload,
        )
    assert excinfo.value.gate == 4
    assert excinfo.value.stage == "gate4_constraints"


def test_gate4_text_too_long_rejected():
    """CLIPTextEncode.text > 4000 chars rejected at Gate 4."""
    user_inputs = _full_user_inputs(prompt="x" * 5000)
    with pytest.raises(RunGateFailure) as excinfo:
        run_workflow_through_gates(
            raw_workflow=TXT2IMG,
            user_inputs=user_inputs,
            image_field_assignments={},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=_ok_client(),
            upload_callback=_stub_upload,
        )
    assert excinfo.value.gate == 4
    assert excinfo.value.stage == "gate4_constraints"


def test_gate5_protected_input_overwrite_attempt_rejected():
    """User tries to set LoadImage.image via user_inputs → Gate 5 hard-fails."""
    workflow_with_load = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "cat", "clip": ["4", 1]}},
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

    file_row = {
        "id": "f-1",
        "owner_user_id": 1,
        "storage_path": "/tmp/x.png",
        "privacy_mode": "standard_plain",
        "scan_status": "clean",
        "original_filename_plain_for_public": "x.png",
        "mime_type_plain_for_public": "image/png",
        "size_bytes": 1024,
        "deleted_at": None,
    }
    user_inputs = _full_user_inputs()
    # Adversarial: also try to overwrite LoadImage.image via user_inputs
    user_inputs["1"] = {"image": "/etc/passwd"}

    with pytest.raises(RunGateFailure) as excinfo:
        run_workflow_through_gates(
            raw_workflow=workflow_with_load,
            user_inputs=user_inputs,
            image_field_assignments={"1": "f-1"},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=_ok_client(),
            upload_callback=_stub_upload,
            fetch_file_row=lambda _conn, _id: file_row,
        )
    # §10.3.3 PROTECTED_IMAGE_INPUTS is enforced inside Gate 5's apply_user_inputs.
    # Gate 4 lets the patch through because (LoadImage, image) lives in
    # analysis.user_inputs but is filtered out of the required-keys set; it has
    # no numeric/text constraint to fail against either.
    assert excinfo.value.gate == 5
    assert excinfo.value.stage == "gate5_safety"
    assert "受保護" in excinfo.value.msg or "保護" in excinfo.value.msg


def test_gate5_image_remap_invokes_upload_callback():
    """LoadImage assignment runs through remap which calls upload_callback."""
    workflow_with_load = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "cat", "clip": ["4", 1]}},
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
    file_row = {
        "id": "f-1",
        "owner_user_id": 1,
        "storage_path": "/tmp/x.png",
        "privacy_mode": "standard_plain",
        "scan_status": "clean",
        "original_filename_plain_for_public": "x.png",
        "mime_type_plain_for_public": "image/png",
        "size_bytes": 1024,
        "deleted_at": None,
    }
    upload_calls = []

    def _capture_upload(*, file_row, target_filename, run_id):
        upload_calls.append((target_filename, run_id))
        return {"filename": target_filename, "subfolder": run_id}

    result = run_workflow_through_gates(
        raw_workflow=workflow_with_load,
        user_inputs=_full_user_inputs(),
        image_field_assignments={"1": "f-1"},
        actor={"id": 1, "username": "alice"},
        user_id=1,
        run_id="run9",
        conn=None,
        comfyui_client=_ok_client(),
        upload_callback=_capture_upload,
        fetch_file_row=lambda _conn, _id: file_row,
    )
    assert len(upload_calls) == 1
    assert upload_calls[0] == ("1_run9_1.png", "run9")
    # Workflow's LoadImage.image was rewritten to ComfyUI subfolder/filename
    assert result.workflow["1"]["inputs"]["image"] == "run9/1_run9_1.png"
    assert result.audit_metadata["image_remapped"] == 1


def test_gate5_video_remap_invokes_upload_callback():
    """LoadVideo.file uses the same protected media remap path as LoadImage."""
    workflow_with_video = {
        **TXT2IMG,
        "1": {"class_type": "LoadVideo", "inputs": {"file": "clip.webm"}},
    }
    file_row = {
        "id": "v-1",
        "owner_user_id": 1,
        "storage_path": "/tmp/clip.webm",
        "privacy_mode": "standard_plain",
        "scan_status": "clean",
        "original_filename_plain_for_public": "clip.webm",
        "mime_type_plain_for_public": "video/webm",
        "size_bytes": 2048,
        "deleted_at": None,
    }
    upload_calls = []

    def _capture_upload(*, file_row, target_filename, run_id):
        upload_calls.append((target_filename, run_id))
        return {"filename": target_filename, "subfolder": run_id}

    result = run_workflow_through_gates(
        raw_workflow=workflow_with_video,
        user_inputs=_full_user_inputs(),
        image_field_assignments={"1": "v-1"},
        actor={"id": 1, "username": "alice"},
        user_id=1,
        run_id="runv",
        conn=None,
        comfyui_client=_ok_client(),
        upload_callback=_capture_upload,
        fetch_file_row=lambda _conn, _id: file_row,
    )

    assert upload_calls == [("1_runv_1.webm", "runv")]
    assert result.workflow["1"]["inputs"]["file"] == "runv/1_runv_1.webm"
    assert result.audit_metadata["image_remapped"] == 1


def test_run_gate_failure_carries_audit_detail():
    """Auditor-friendly: RunGateFailure exposes structured audit metadata."""
    minimal_client = _stub_client(
        classes={"CheckpointLoaderSimple"},  # missing most classes
        models=[],
    )
    try:
        run_workflow_through_gates(
            raw_workflow=TXT2IMG,
            user_inputs={},
            image_field_assignments={},
            actor={"id": 1},
            user_id=1,
            run_id="r",
            conn=None,
            comfyui_client=minimal_client,
            upload_callback=_stub_upload,
        )
    except RunGateFailure as exc:
        assert exc.gate == 2
        assert "unsupported" in exc.audit_detail
        assert isinstance(exc.audit_detail["unsupported"], list)
    else:
        pytest.fail("RunGateFailure expected")
