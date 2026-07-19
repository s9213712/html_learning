import base64
import sqlite3
import threading

import pytest

from services.comfyui.client import ComfyUIError
from services.comfyui.template.cleanup import reset_registry
from services.security.upload_security import create_uploaded_file_record
from tests.comfyui._integration_suite import (
    FakeComfyUIClient,
    _actor,
    _await_comfyui_job,
    _await_comfyui_result,
    _build_app,
    _import_workflow_preset,
    _init_db,
)


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def _isolate_cleanup_registry():
    reset_registry()
    FakeComfyUIClient.uploaded_images = []
    FakeComfyUIClient.discarded = []
    yield
    reset_registry()


def _load_image_workflow(*, two_inputs=False):
    workflow = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "author-value-must-not-run.png"}},
        "9": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}},
    }
    if two_inputs:
        workflow["2"] = {"class_type": "LoadImage", "inputs": {"image": "second-author-value.png"}}
    return workflow


def _create_cloud_image(db_path, storage_root, *, name="input.png"):
    relative_path = f"users/1/test/{name}"
    disk_path = storage_root / relative_path
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_bytes(_PNG_1X1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        upload = create_uploaded_file_record(
            conn,
            owner_user_id=1,
            storage_path=relative_path,
            privacy_mode="standard_plain",
            size_bytes=len(_PNG_1X1),
            original_filename=name,
            mime_type="image/png",
            user=_actor(),
        )
        conn.execute(
            "UPDATE uploaded_files SET scan_status='not_required' WHERE id=?",
            (upload["file_id"],),
        )
        conn.commit()
        return upload["file_id"]
    finally:
        conn.close()


def _app_client(tmp_path, *, comfyui_client=None, settings=None):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    app = _build_app(
        db_path,
        storage_root,
        comfyui_client=comfyui_client,
        settings=settings,
    )
    return app.test_client(), db_path, storage_root


def _prove_remote_absence(monkeypatch):
    monkeypatch.setattr(
        "services.comfyui.template.cleanup._remote_ref_absent",
        lambda _client, _ref: (True, "test_http_404"),
    )


def test_async_success_is_not_terminal_until_input_cleanup_is_proven(tmp_path, monkeypatch):
    _prove_remote_absence(monkeypatch)
    client, db_path, storage_root = _app_client(tmp_path)
    file_id = _create_cloud_image(db_path, storage_root)
    preset = _import_workflow_preset(client, _load_image_workflow())

    started = client.post(
        f"/api/comfyui/workflows/{preset['id']}/run",
        json={"image_field_assignments": {"1": file_id}},
    )
    accepted = started.get_json()
    result = _await_comfyui_result(client, started)

    receipt = result["input_cleanup"]
    assert accepted["media_remap_run_id"] == receipt["run_id"]
    assert accepted["input_assignment_count"] == result["input_assignment_count"] == 1
    assert receipt["ok"] is True
    assert receipt["absence_verified"] is True
    assert receipt["input_ref_count"] == 1
    assert receipt["run_id"]
    assert receipt["cleanup"]["refs"][0]["ref"]["subfolder"] == receipt["run_id"]
    assert receipt["cleanup"]["refs"][0]["absent"] is True
    assert FakeComfyUIClient.discarded

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT params_json, output_refs_json, status FROM comfyui_workflow_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    params = __import__("json").loads(row[0])
    output_refs = __import__("json").loads(row[1])
    assert row[2] == "completed"
    assert params["media_remap_run_id"] == receipt["run_id"]
    assert output_refs["input_cleanup"]["absence_verified"] is True


def test_async_worker_error_still_publishes_cleanup_receipt(tmp_path, monkeypatch):
    class FailingWorkflowClient(FakeComfyUIClient):
        def generate_from_workflow(self, *args, **kwargs):
            raise ComfyUIError("injected workflow failure")

    _prove_remote_absence(monkeypatch)
    client, db_path, storage_root = _app_client(tmp_path, comfyui_client=FailingWorkflowClient())
    file_id = _create_cloud_image(db_path, storage_root)
    preset = _import_workflow_preset(client, _load_image_workflow())

    started = client.post(
        f"/api/comfyui/workflows/{preset['id']}/run",
        json={"image_field_assignments": {"1": file_id}},
    )
    job = _await_comfyui_job(client, started, expected_status="error")

    receipt = job["result"]["input_cleanup"]
    assert "injected workflow failure" in job["error"]
    assert receipt["ok"] is True
    assert receipt["absence_verified"] is True
    assert receipt["reason"] == "async_terminal_comfyui_error"
    assert FakeComfyUIClient.discarded


def test_partial_sync_remap_failure_returns_cleanup_receipt(tmp_path, monkeypatch):
    _prove_remote_absence(monkeypatch)
    client, db_path, storage_root = _app_client(tmp_path)
    file_id = _create_cloud_image(db_path, storage_root)
    preset = _import_workflow_preset(client, _load_image_workflow(two_inputs=True))

    response = client.post(
        f"/api/comfyui/workflows/{preset['id']}/run",
        json={
            "image_field_assignments": {
                "1": file_id,
                "2": "missing-cloud-file",
            }
        },
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["stage"] == "media_remap_failed"
    receipt = body["input_cleanup"]
    assert receipt["ok"] is True
    assert receipt["absence_verified"] is True
    assert receipt["reason"] == "sync_media_remap_failure"
    assert receipt["input_ref_count"] == 1
    assert FakeComfyUIClient.discarded


def test_strict_gate_partial_upload_failure_returns_cleanup_receipt(tmp_path, monkeypatch):
    class StrictWorkflowClient(FakeComfyUIClient):
        def get_object_info(self):
            return {
                "LoadImage": {"input": {"required": {}}},
                "PreviewImage": {"input": {"required": {}}},
            }

    _prove_remote_absence(monkeypatch)
    monkeypatch.setattr(
        "routes.comfyui_sections.workflow_routes.is_feature_enabled",
        lambda key: key == "feature_comfyui_template_importer_strict",
    )
    client, db_path, storage_root = _app_client(
        tmp_path,
        comfyui_client=StrictWorkflowClient(),
        settings={"feature_comfyui_template_importer_strict": True},
    )
    file_id = _create_cloud_image(db_path, storage_root)
    preset = _import_workflow_preset(client, _load_image_workflow(two_inputs=True))

    response = client.post(
        f"/api/comfyui/workflows/{preset['id']}/run",
        json={
            "image_field_assignments": {
                "1": file_id,
                "2": "missing-cloud-file",
            }
        },
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["gate"] == 5, body
    receipt = body["input_cleanup"]
    assert receipt["ok"] is True
    assert receipt["absence_verified"] is True
    assert receipt["reason"].startswith("sync_gate_failure:")
    assert receipt["input_ref_count"] == 1
    assert FakeComfyUIClient.discarded


def test_absence_verification_failure_forces_async_job_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "services.comfyui.template.cleanup._remote_ref_absent",
        lambda _client, _ref: (False, "still_present"),
    )
    client, db_path, storage_root = _app_client(tmp_path)
    file_id = _create_cloud_image(db_path, storage_root)
    preset = _import_workflow_preset(client, _load_image_workflow())

    started = client.post(
        f"/api/comfyui/workflows/{preset['id']}/run",
        json={"image_field_assignments": {"1": file_id}},
    )
    job = _await_comfyui_job(client, started, expected_status="error")

    receipt = job["result"]["input_cleanup"]
    assert receipt["ok"] is False
    assert receipt["absence_verified"] is False
    assert "input_cleanup_not_verified" in job["error"]


def test_sync_worker_start_failure_cleans_input_and_marks_created_job_error(tmp_path, monkeypatch):
    _prove_remote_absence(monkeypatch)
    client, db_path, storage_root = _app_client(tmp_path)
    file_id = _create_cloud_image(db_path, storage_root)
    preset = _import_workflow_preset(client, _load_image_workflow())

    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _thread: (_ for _ in ()).throw(RuntimeError("injected thread start failure")),
    )
    response = client.post(
        f"/api/comfyui/workflows/{preset['id']}/run",
        json={"image_field_assignments": {"1": file_id}},
    )

    assert response.status_code == 500
    body = response.get_json()
    assert body["stage"] == "workflow_worker_start_failed"
    assert body["input_cleanup"]["ok"] is True
    assert body["input_cleanup"]["absence_verified"] is True
    conn = sqlite3.connect(db_path)
    try:
        workflow_status = conn.execute(
            "SELECT status FROM comfyui_workflow_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        job_status, result_json = conn.execute(
            "SELECT status, result_json FROM comfyui_generation_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert workflow_status == "error"
    assert job_status == "error"
    assert __import__("json").loads(result_json)["input_cleanup"]["absence_verified"] is True
