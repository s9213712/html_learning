"""Remap regression for the ComfyUI template importer (§7.3 + §10.3.3)."""

import pytest

from routes.comfyui_sections import workflow_routes
from routes.comfyui_sections.workflow_routes import (
    _OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX,
    _default_upload_callback,
    _workflow_template_fetch_file_row,
)
from services.comfyui.template.remap import (
    PROTECTED_IMAGE_INPUTS,
    PROTECTED_MEDIA_INPUTS,
    PROTECTED_VIDEO_INPUTS,
    SafetyError,
    remap_load_image_to_cloud_file,
)
from services.storage.cloud_drive import resolve_file_storage_path


def _row(**overrides):
    """Build a minimal uploaded_files row with §7.3-compatible defaults."""
    base = {
        "id": "f-1",
        "owner_user_id": 7,
        "storage_path": "/tmp/uploaded/f-1.png",
        "privacy_mode": "standard_plain",
        "risk_level": "low",
        "scan_status": "clean",
        "original_filename_plain_for_public": "cat.png",
        "mime_type_plain_for_public": "image/png",
        "size_bytes": 1024,
        "deleted_at": None,
    }
    base.update(overrides)
    return base


def _stub_fetch(rows_by_id):
    """Return a fetch_file_row callable that reads from a dict."""
    def _fetch(_conn, cloud_file_id):
        return rows_by_id.get(cloud_file_id)
    return _fetch


def _stub_upload(*, file_row, target_filename, run_id):
    """Pretend ComfyUI accepted the upload and returned its filename."""
    return {"filename": target_filename, "subfolder": run_id, "type": "input"}


WORKFLOW_WITH_LOAD_IMAGE = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "user_path.png"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "cat", "clip": ["X", 1]}},
    "3": {"class_type": "VAEEncode", "inputs": {"pixels": ["1", 0], "vae": ["X", 0]}},
}

WORKFLOW_WITH_LOAD_VIDEO = {
    "1": {"class_type": "LoadVideo", "inputs": {"file": "user_video.mp4"}},
    "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
}


def test_protected_inputs_set_matches_spec():
    """Mirror the spec's PROTECTED_INPUTS list (§10.3.3)."""
    assert PROTECTED_IMAGE_INPUTS == frozenset({
        ("LoadImage", "image"),
        ("LoadImageMask", "image"),
        ("LoadImageMask", "mask"),
    })
    assert PROTECTED_VIDEO_INPUTS == frozenset({("LoadVideo", "file")})
    assert PROTECTED_MEDIA_INPUTS == PROTECTED_IMAGE_INPUTS | PROTECTED_VIDEO_INPUTS


def test_remap_replaces_load_image_value_and_does_not_mutate_input():
    rows = {"f-1": _row()}
    out = remap_load_image_to_cloud_file(
        WORKFLOW_WITH_LOAD_IMAGE,
        image_field_assignments={"1": "f-1"},
        actor={"id": 7},
        conn=None,
        run_id="run9",
        upload_callback=_stub_upload,
        fetch_file_row=_stub_fetch(rows),
    )
    assert out["1"]["inputs"]["image"] == "run9/7_run9_1.png"
    # original unchanged
    assert WORKFLOW_WITH_LOAD_IMAGE["1"]["inputs"]["image"] == "user_path.png"


def test_remap_replaces_load_video_file_and_preserves_extension():
    rows = {
        "v-1": _row(
            id="v-1",
            storage_path="/tmp/uploaded/v-1.mp4",
            original_filename_plain_for_public="clip.mp4",
            mime_type_plain_for_public="video/mp4",
            size_bytes=2048,
        )
    }
    out = remap_load_image_to_cloud_file(
        WORKFLOW_WITH_LOAD_VIDEO,
        image_field_assignments={"1": "v-1"},
        actor={"id": 7},
        conn=None,
        run_id="runv",
        upload_callback=_stub_upload,
        fetch_file_row=_stub_fetch(rows),
    )

    assert out["1"]["inputs"]["file"] == "runv/7_runv_1.mp4"
    assert WORKFLOW_WITH_LOAD_VIDEO["1"]["inputs"]["file"] == "user_video.mp4"


def test_remap_rejects_workflow_with_unfilled_protected_node():
    rows = {"f-1": _row()}
    with pytest.raises(SafetyError, match="沒有指定媒體來源"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={},  # node "1" needs assignment but didn't get one
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_rejects_assignment_for_non_load_image_node():
    rows = {"f-1": _row()}
    with pytest.raises(SafetyError, match="不存在或非 LoadImage"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1", "2": "f-1"},  # "2" is CLIPTextEncode
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_rejects_other_owner():
    rows = {"f-1": _row(owner_user_id=99)}
    with pytest.raises(SafetyError, match="不屬於你"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_rejects_missing_file_row():
    with pytest.raises(SafetyError, match="不存在或已刪除"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "missing-id"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch({}),
        )


def test_remap_rejects_deleted_file():
    rows = {"f-1": _row(deleted_at="2026-05-08T12:00:00")}
    with pytest.raises(SafetyError, match="已刪除"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_rejects_non_standard_plain_privacy_mode():
    rows = {"f-1": _row(privacy_mode="e2ee")}
    with pytest.raises(SafetyError, match="standard_plain"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_rejects_disallowed_mime():
    rows = {"f-1": _row(mime_type_plain_for_public="image/heic")}
    with pytest.raises(SafetyError, match="MIME"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_rejects_disallowed_extension():
    rows = {"f-1": _row(original_filename_plain_for_public="cat.heic")}
    with pytest.raises(SafetyError, match="副檔名"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_rejects_oversize_file():
    rows = {"f-1": _row(size_bytes=99 * 1024 * 1024)}  # 99 MiB
    with pytest.raises(SafetyError, match="超過上限"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_official_template_media_image_uses_fixture_sized_limit():
    asset_id = f"{_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX}2.png"
    rows = {
        asset_id: _row(
            id=asset_id,
            size_bytes=15_096_725,
            original_filename_plain_for_public="2.png",
        )
    }
    out = remap_load_image_to_cloud_file(
        WORKFLOW_WITH_LOAD_IMAGE,
        image_field_assignments={"1": asset_id},
        actor={"id": 7},
        conn=None,
        run_id="anime",
        upload_callback=_stub_upload,
        fetch_file_row=_stub_fetch(rows),
    )

    assert out["1"]["inputs"]["image"] == "anime/7_anime_1.png"


def test_remap_rejects_unscanned_when_skip_not_allowed():
    rows = {"f-1": _row(scan_status="skipped")}
    with pytest.raises(SafetyError, match="未通過安全掃描"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
            upload_scan_skip_allowed=False,
        )


def test_remap_accepts_not_required_scan_status():
    rows = {"f-1": _row(scan_status="not_required")}
    out = remap_load_image_to_cloud_file(
        WORKFLOW_WITH_LOAD_IMAGE,
        image_field_assignments={"1": "f-1"},
        actor={"id": 7},
        conn=None,
        run_id="r",
        upload_callback=_stub_upload,
        fetch_file_row=_stub_fetch(rows),
        upload_scan_skip_allowed=False,
    )
    assert out["1"]["inputs"]["image"].startswith("r/7_r_1.")


def test_remap_accepts_skipped_scan_when_root_opted_in():
    rows = {"f-1": _row(scan_status="skipped")}
    out = remap_load_image_to_cloud_file(
        WORKFLOW_WITH_LOAD_IMAGE,
        image_field_assignments={"1": "f-1"},
        actor={"id": 7},
        conn=None,
        run_id="r",
        upload_callback=_stub_upload,
        fetch_file_row=_stub_fetch(rows),
        upload_scan_skip_allowed=True,
    )
    assert out["1"]["inputs"]["image"].startswith("r/7_r_1.")


def test_remap_decode_failure_propagates_as_safety_error():
    rows = {"f-1": _row()}

    def _bad_decoder(_bytes):
        raise ValueError("not an image")

    with pytest.raises(SafetyError, match="解碼失敗"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
            image_decoder=_bad_decoder,
            file_bytes_loader=lambda _row: b"\x00\x01",
        )


def test_remap_rejects_empty_run_id():
    rows = {"f-1": _row()}
    with pytest.raises(SafetyError, match="run_id"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_rejects_missing_actor_id():
    rows = {"f-1": _row()}
    with pytest.raises(SafetyError, match="actor.id"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={},
            conn=None,
            run_id="r",
            upload_callback=_stub_upload,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_rejects_upload_callback_returning_no_filename():
    rows = {"f-1": _row()}

    def _bad_callback(*, file_row, target_filename, run_id):
        return {"filename": "", "subfolder": run_id}

    with pytest.raises(SafetyError, match="未取得檔名"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_bad_callback,
            fetch_file_row=_stub_fetch(rows),
        )


def test_remap_converts_upload_callback_error_to_safety_error():
    rows = {"f-1": _row()}

    def _failing_callback(*, file_row, target_filename, run_id):
        raise RuntimeError("connection reset")

    with pytest.raises(SafetyError, match="上傳圖片到 ComfyUI 失敗"):
        remap_load_image_to_cloud_file(
            WORKFLOW_WITH_LOAD_IMAGE,
            image_field_assignments={"1": "f-1"},
            actor={"id": 7},
            conn=None,
            run_id="r",
            upload_callback=_failing_callback,
            fetch_file_row=_stub_fetch(rows),
        )


def test_default_workflow_upload_callback_surfaces_comfyui_errors(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"png-bytes")

    class _FailingClient:
        def upload_image_bytes(self, *args, **kwargs):
            raise RuntimeError("connection reset")

    callback = _default_upload_callback(_FailingClient())
    with pytest.raises(RuntimeError, match="connection reset"):
        callback(
            file_row={"storage_path": str(source)},
            target_filename="target.png",
            run_id="run1",
        )


def test_default_workflow_upload_callback_rejects_empty_source(tmp_path):
    source = tmp_path / "empty.png"
    source.write_bytes(b"")
    callback = _default_upload_callback(object())
    with pytest.raises(RuntimeError, match="內容為空"):
        callback(
            file_row={"storage_path": str(source)},
            target_filename="target.png",
            run_id="run1",
        )


def test_default_workflow_upload_callback_reads_relative_cloud_storage_path(tmp_path):
    storage_root = tmp_path / "storage"
    source = storage_root / "users" / "1" / "abc" / "source.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png-bytes")

    class _CapturingClient:
        def __init__(self):
            self.calls = []

        def upload_image_bytes(self, data, filename, **kwargs):
            self.calls.append((data, filename, kwargs))
            return {"filename": filename, "subfolder": kwargs.get("subfolder") or "", "type": "input"}

    client = _CapturingClient()
    callback = _default_upload_callback(
        client,
        storage_root=storage_root,
        resolve_file_storage_path=resolve_file_storage_path,
    )
    result = callback(
        file_row={"storage_path": "users/1/abc/source.png"},
        target_filename="target.png",
        run_id="run1",
    )

    assert result == {"filename": "target.png", "subfolder": "run1", "type": "input"}
    assert client.calls == [
        (b"png-bytes", "target.png", {"image_type": "input", "overwrite": False, "subfolder": "run1"})
    ]


def test_default_workflow_upload_callback_falls_back_to_runtime_storage(tmp_path, monkeypatch):
    source = tmp_path / "runtime" / "storage" / "users" / "1" / "abc" / "source.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png-bytes")
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(tmp_path / "runtime"))

    class _CapturingClient:
        def __init__(self):
            self.calls = []

        def upload_image_bytes(self, data, filename, **kwargs):
            self.calls.append((data, filename, kwargs))
            return {"filename": filename, "subfolder": kwargs.get("subfolder") or "", "type": "input"}

    client = _CapturingClient()
    callback = _default_upload_callback(client)
    result = callback(
        file_row={"storage_path": "users/1/abc/source.png"},
        target_filename="target.png",
        run_id="run1",
    )

    assert result == {"filename": "target.png", "subfolder": "run1", "type": "input"}
    assert client.calls == [
        (b"png-bytes", "target.png", {"image_type": "input", "overwrite": False, "subfolder": "run1"})
    ]


def test_default_workflow_upload_callback_reports_missing_cloud_file(tmp_path):
    callback = _default_upload_callback(object(), storage_root=tmp_path, resolve_file_storage_path=resolve_file_storage_path)
    with pytest.raises(RuntimeError, match="雲端檔案實體不存在"):
        callback(
            file_row={"storage_path": "users/1/missing/source.png"},
            target_filename="target.png",
            run_id="run1",
        )


def test_default_workflow_upload_callback_recovers_root_official_template_media(tmp_path):
    class _CapturingClient:
        def __init__(self):
            self.calls = []

        def upload_image_bytes(self, data, filename, **kwargs):
            self.calls.append((data, filename, kwargs))
            return {"filename": filename, "subfolder": kwargs.get("subfolder") or "", "type": "input"}

    client = _CapturingClient()
    callback = _default_upload_callback(
        client,
        storage_root=tmp_path,
        resolve_file_storage_path=resolve_file_storage_path,
    )
    result = callback(
        file_row={
            "owner_user_id": 1,
            "storage_path": "users/1/a31ee23f29ab4cb4acbcee03056521ab/image_qwen_image_edit_2509_input_image.png",
            "original_filename_plain_for_public": "image_qwen_image_edit_2509_input_image.png",
        },
        target_filename="target.png",
        run_id="run1",
    )

    assert result == {"filename": "target.png", "subfolder": "run1", "type": "input"}
    assert client.calls[0][0].startswith(b"\x89PNG\r\n\x1a\n")
    assert client.calls[0][1:] == (
        "target.png",
        {"image_type": "input", "overwrite": False, "subfolder": "run1"},
    )


def test_default_workflow_upload_callback_recovers_generic_official_template_media(tmp_path, monkeypatch):
    media_dir = tmp_path / "assets"
    media_dir.mkdir()
    (media_dir / "group_photo.png").write_bytes(b"official-template-image")
    monkeypatch.setattr(workflow_routes, "_OFFICIAL_TEMPLATE_MEDIA_DIR", media_dir)

    class _CapturingClient:
        def __init__(self):
            self.calls = []

        def upload_image_bytes(self, data, filename, **kwargs):
            self.calls.append((data, filename, kwargs))
            return {"filename": filename, "subfolder": kwargs.get("subfolder") or "", "type": "input"}

    client = _CapturingClient()
    callback = _default_upload_callback(
        client,
        storage_root=tmp_path,
        resolve_file_storage_path=resolve_file_storage_path,
    )
    result = callback(
        file_row={
            "owner_user_id": 1,
            "storage_path": "users/1/97f28f873d14474c97cc5b264a89306d/group_photo.png",
            "original_filename_plain_for_public": "group_photo.png",
        },
        target_filename="target.png",
        run_id="run1",
    )

    assert result == {"filename": "target.png", "subfolder": "run1", "type": "input"}
    assert client.calls == [
        (
            b"official-template-image",
            "target.png",
            {"image_type": "input", "overwrite": False, "subfolder": "run1"},
        )
    ]


def test_official_template_media_assignment_fetches_asset_as_actor_owned_row(tmp_path, monkeypatch):
    media_dir = tmp_path / "assets"
    media_dir.mkdir()
    asset_path = media_dir / "horse_running.mp4"
    asset_path.write_bytes(b"official-template-video")
    monkeypatch.setattr(workflow_routes, "_OFFICIAL_TEMPLATE_MEDIA_DIR", media_dir)

    row = _workflow_template_fetch_file_row(
        None,
        f"{_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX}horse_running.mp4",
        actor={"id": 7},
    )

    assert row["owner_user_id"] == 7
    assert row["storage_path"] == str(asset_path)
    assert row["privacy_mode"] == "standard_plain"
    assert row["scan_status"] == "clean"
    assert row["mime_type_plain_for_public"] == "video/mp4"
    assert row["size_bytes"] == len(b"official-template-video")


def test_official_template_media_assignment_remaps_load_video(tmp_path, monkeypatch):
    media_dir = tmp_path / "assets"
    media_dir.mkdir()
    (media_dir / "horse_running.mp4").write_bytes(b"official-template-video")
    monkeypatch.setattr(workflow_routes, "_OFFICIAL_TEMPLATE_MEDIA_DIR", media_dir)

    actor = {"id": 7}
    out = remap_load_image_to_cloud_file(
        WORKFLOW_WITH_LOAD_VIDEO,
        image_field_assignments={
            "1": f"{_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX}horse_running.mp4",
        },
        actor=actor,
        conn=None,
        run_id="run9",
        upload_callback=_stub_upload,
        fetch_file_row=lambda conn, file_id: _workflow_template_fetch_file_row(conn, file_id, actor=actor),
    )

    assert out["1"]["inputs"]["file"] == "run9/7_run9_1.mp4"


def test_default_workflow_upload_callback_does_not_recover_user_file_by_name(tmp_path):
    callback = _default_upload_callback(object(), storage_root=tmp_path, resolve_file_storage_path=resolve_file_storage_path)
    with pytest.raises(RuntimeError, match="雲端檔案實體不存在"):
        callback(
            file_row={
                "owner_user_id": 2,
                "storage_path": "users/2/a31ee23f29ab4cb4acbcee03056521ab/image_qwen_image_edit_2509_input_image.png",
                "original_filename_plain_for_public": "image_qwen_image_edit_2509_input_image.png",
            },
            target_filename="target.png",
            run_id="run1",
        )


def test_remap_handles_load_image_mask_node():
    workflow = {
        "1": {"class_type": "LoadImageMask", "inputs": {"image": "x.png", "channel": "alpha"}},
        "2": {"class_type": "VAEEncodeForInpaint", "inputs": {"pixels": ["1", 0], "vae": ["X", 0]}},
    }
    rows = {"m-1": _row(id="m-1")}
    out = remap_load_image_to_cloud_file(
        workflow,
        image_field_assignments={"1": "m-1"},
        actor={"id": 7},
        conn=None,
        run_id="abc",
        upload_callback=_stub_upload,
        fetch_file_row=_stub_fetch(rows),
    )
    assert out["1"]["inputs"]["image"] == "abc/7_abc_1.png"
    # channel input untouched
    assert out["1"]["inputs"]["channel"] == "alpha"
