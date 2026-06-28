"""Regression for services.comfyui.execution.generate_image()."""

import json

import pytest

from services.comfyui import execution as comfy_execution


def test_backend_unresponsive_default_allows_low_vram_qwen_steps():
    assert comfy_execution.BACKEND_UNRESPONSIVE_FAIL_SECONDS >= 7200


class _GeneratedImage:
    filename = "done.png"
    subfolder = ""
    type = "output"
    mime_type = "image/png"
    data = b"png"


def test_generate_image_accepts_generate_from_workflow_func():
    called = {}

    def _build_generation_workflow(params):
        called["params"] = dict(params)
        return {"3": {"class_type": "KSampler", "inputs": {"steps": params["steps"]}}}

    def _generate_from_workflow(workflow, *, timeout_seconds=1800, expected_count=1, progress_callback=None):
        called["workflow"] = workflow
        called["timeout_seconds"] = timeout_seconds
        called["expected_count"] = expected_count
        if progress_callback:
            progress_callback({"phase": "running", "percent": 50})
        return {"prompt_id": "p1", "images": [{"image_ref": {"filename": "x.png", "subfolder": "", "type": "output"}}]}

    progress_events = []
    result = comfy_execution.generate_image(
        client=object(),
        params={"steps": 30, "batch_size": 2},
        timeout_seconds=77,
        progress_callback=progress_events.append,
        build_generation_workflow_func=_build_generation_workflow,
        generate_from_workflow_func=_generate_from_workflow,
        error_cls=RuntimeError,
    )

    assert called["params"] == {"steps": 30, "batch_size": 2}
    assert called["workflow"]["3"]["inputs"]["steps"] == 30
    assert called["timeout_seconds"] == 77
    assert called["expected_count"] == 2
    assert progress_events == [{"phase": "running", "percent": 50}]
    assert result["prompt_id"] == "p1"


def test_wait_for_images_treats_transient_history_timeout_as_recoverable(monkeypatch):
    clock = {"now": 0.0}
    progress_events = []

    class FlakyHistoryClient:
        timeout = 1

        def __init__(self):
            self.calls = 0

        def _json_request(self, path, *, timeout=None):
            assert path == "/history/prompt-1"
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("ComfyUI 連線失敗：timed out")
            return {
                "prompt-1": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {"9": {"images": [{"filename": "done.png", "subfolder": "", "type": "output"}]}},
                }
            }

    def fake_time():
        return clock["now"]

    def fake_sleep(seconds):
        clock["now"] += max(float(seconds), 0.15)

    monkeypatch.setattr(comfy_execution.time, "time", fake_time)
    monkeypatch.setattr(comfy_execution.time, "sleep", fake_sleep)

    images = comfy_execution.wait_for_images(
        FlakyHistoryClient(),
        "prompt-1",
        timeout_seconds=10,
        poll_interval=0.5,
        expected_count=1,
        error_cls=RuntimeError,
        progress_callback=progress_events.append,
    )

    assert images == [{"filename": "done.png", "subfolder": "", "type": "output"}]
    assert any(event.get("backend_unresponsive") is True for event in progress_events)
    assert any(event.get("phase") == "completed" for event in progress_events)


def test_wait_for_images_fails_after_backend_unresponsive_limit(monkeypatch):
    clock = {"now": 0.0}
    progress_events = []

    class AlwaysTimeoutHistoryClient:
        timeout = 1

        def _json_request(self, path, *, timeout=None):
            assert path == "/history/prompt-1"
            raise RuntimeError("ComfyUI 連線失敗：timed out")

    class FakeWebSocketModule:
        class WebSocketTimeoutException(Exception):
            pass

        class WebSocketConnectionClosedException(Exception):
            pass

    class FakeWebSocket:
        def __init__(self):
            self.sent = False

        def recv(self):
            if not self.sent:
                self.sent = True
                return json.dumps({
                    "type": "progress",
                    "data": {"prompt_id": "prompt-1", "node": "499", "value": 1, "max": 4},
                })
            raise FakeWebSocketModule.WebSocketTimeoutException()

    def fake_time():
        return clock["now"]

    def fake_sleep(seconds):
        clock["now"] += max(float(seconds), 0.15)

    monkeypatch.setattr(comfy_execution, "BACKEND_UNRESPONSIVE_FAIL_SECONDS", 1)
    monkeypatch.setattr(comfy_execution.time, "time", fake_time)
    monkeypatch.setattr(comfy_execution.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError) as exc:
        comfy_execution.wait_for_images(
            AlwaysTimeoutHistoryClient(),
            "prompt-1",
            timeout_seconds=0,
            poll_interval=0.5,
            expected_count=1,
            error_cls=RuntimeError,
            progress_callback=progress_events.append,
            websocket_conn=FakeWebSocket(),
            websocket_module=FakeWebSocketModule,
        )

    assert "連續" in str(exc.value)
    assert "沒有回覆 history 查詢" in str(exc.value)
    assert any(event.get("phase") == "running" for event in progress_events)
    assert any(event.get("backend_unresponsive_seconds", 0) >= 1 for event in progress_events)


def test_wait_for_outputs_reports_comfyui_execution_error_detail():
    class ErrorHistoryClient:
        timeout = 1

        def _json_request(self, path, *, timeout=None):
            assert path == "/history/prompt-error"
            return {
                "prompt-error": {
                    "status": {
                        "completed": False,
                        "status_str": "error",
                        "messages": [
                            [
                                "execution_error",
                                {
                                    "exception_type": "ValidationError",
                                    "exception_message": "ckpt_name not in list",
                                    "node_id": "4",
                                    "node_type": "CheckpointLoaderSimple",
                                },
                            ]
                        ],
                    },
                    "outputs": {},
                }
            }

    with pytest.raises(RuntimeError) as exc:
        comfy_execution.wait_for_outputs(
            ErrorHistoryClient(),
            "prompt-error",
            timeout_seconds=10,
            poll_interval=0.5,
            expected_count=1,
            error_cls=RuntimeError,
        )

    assert "ValidationError: ckpt_name not in list" in str(exc.value)
    assert "node 4 CheckpointLoaderSimple" in str(exc.value)


def test_wait_for_outputs_keeps_all_completed_workflow_images():
    class CompletedCompareClient:
        timeout = 1

        def _json_request(self, path, *, timeout=None):
            assert path == "/history/prompt-compare"
            return {
                "prompt-compare": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "50": {"images": [{"filename": "checkpoint_a.png", "subfolder": "", "type": "output"}]},
                        "51": {"images": [{"filename": "checkpoint_b.png", "subfolder": "", "type": "output"}]},
                    },
                }
            }

    outputs = comfy_execution.wait_for_outputs(
        CompletedCompareClient(),
        "prompt-compare",
        timeout_seconds=10,
        poll_interval=0.5,
        expected_count=1,
        error_cls=RuntimeError,
    )

    assert [item["filename"] for item in outputs["images"]] == ["checkpoint_a.png", "checkpoint_b.png"]


def test_wait_for_outputs_buckets_save_video_images_as_video_media():
    class CompletedVideoClient:
        timeout = 1

        def _json_request(self, path, *, timeout=None):
            assert path == "/history/prompt-ltx"
            return {
                "prompt-ltx": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "75": {
                            "images": [
                                {
                                    "filename": "ltx_preview_00001_.mp4",
                                    "subfolder": "hackme\\1",
                                    "type": "output",
                                }
                            ],
                            "animated": [True],
                        }
                    },
                }
            }

    outputs = comfy_execution.wait_for_outputs(
        CompletedVideoClient(),
        "prompt-ltx",
        timeout_seconds=10,
        poll_interval=0.5,
        expected_count=1,
        wait_until_completed=True,
        workflow={
            "75": {"class_type": "SaveVideo", "inputs": {}},
        },
        error_cls=RuntimeError,
    )

    assert outputs["images"] == []
    assert outputs["videos"][0]["filename"] == "ltx_preview_00001_.mp4"
    assert outputs["videos"][0]["subfolder"] == "hackme\\1"
    assert outputs["videos"][0]["output_node_id"] == "75"


def test_wait_for_outputs_suppresses_preview_images_for_video_only_workflow():
    class CompletedWanVaceClient:
        timeout = 1

        def _json_request(self, path, *, timeout=None):
            assert path == "/history/prompt-wan-vace"
            return {
                "prompt-wan-vace": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "214": {
                            "images": [
                                {"filename": "mask_preview_00001_.png", "subfolder": "", "type": "temp"},
                                {"filename": "mask_preview_00002_.png", "subfolder": "", "type": "temp"},
                            ]
                        },
                        "69": {
                            "images": [
                                {"filename": "wan_vace_00001_.mp4", "subfolder": "hackme\\1", "type": "output"}
                            ]
                        },
                    },
                }
            }

    outputs = comfy_execution.wait_for_outputs(
        CompletedWanVaceClient(),
        "prompt-wan-vace",
        timeout_seconds=10,
        poll_interval=0.5,
        expected_count=1,
        wait_until_completed=True,
        workflow={
            "69": {"class_type": "SaveVideo", "inputs": {}},
            "214": {"class_type": "PreviewImage", "inputs": {}},
        },
        error_cls=RuntimeError,
    )

    assert outputs["images"] == []
    assert [item["filename"] for item in outputs["videos"]] == ["wan_vace_00001_.mp4"]


def test_wait_for_outputs_can_wait_for_completed_prompt_before_returning_first_preview(monkeypatch):
    clock = {"now": 0.0}

    class EarlyMaskClient:
        timeout = 1

        def __init__(self):
            self.calls = 0

        def _json_request(self, path, *, timeout=None):
            assert path == "/history/prompt-sam3"
            self.calls += 1
            if self.calls == 1:
                return {
                    "prompt-sam3": {
                        "status": {"completed": False, "status_str": "running"},
                        "outputs": {
                            "95": {"images": [{"filename": "mask_preview.png", "subfolder": "", "type": "temp"}]},
                        },
                    }
                }
            return {
                "prompt-sam3": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "95": {"images": [{"filename": "mask_preview.png", "subfolder": "", "type": "temp"}]},
                        "106": {"images": [{"filename": "segmented_image.png", "subfolder": "", "type": "temp"}]},
                    },
                }
            }

    def fake_time():
        return clock["now"]

    def fake_sleep(seconds):
        clock["now"] += max(float(seconds), 0.15)

    monkeypatch.setattr(comfy_execution.time, "time", fake_time)
    monkeypatch.setattr(comfy_execution.time, "sleep", fake_sleep)

    client = EarlyMaskClient()
    outputs = comfy_execution.wait_for_outputs(
        client,
        "prompt-sam3",
        timeout_seconds=10,
        poll_interval=0.5,
        expected_count=1,
        wait_until_completed=True,
        workflow={
            "95": {"class_type": "MaskPreview", "inputs": {}},
            "106": {"class_type": "PreviewImage", "inputs": {}},
        },
        error_cls=RuntimeError,
    )

    assert client.calls == 2
    assert [item["filename"] for item in outputs["images"]] == ["segmented_image.png", "mask_preview.png"]


def test_ws_node_progress_is_scaled_to_total_workflow_progress():
    snapshot = {
        "prompt_id": "prompt-1",
        "phase": "queued",
        "percent": 0,
        "current": 0,
        "max": 0,
        "completed": False,
    }

    comfy_execution.apply_ws_message_to_progress(
        snapshot,
        {"type": "executing", "data": {"prompt_id": "prompt-1", "node": "10"}},
        "prompt-1",
        total_node_count=4,
    )
    comfy_execution.apply_ws_message_to_progress(
        snapshot,
        {"type": "progress", "data": {"prompt_id": "prompt-1", "node": "10", "value": 100, "max": 100}},
        "prompt-1",
        total_node_count=4,
    )
    first_node_done = snapshot["percent"]

    comfy_execution.apply_ws_message_to_progress(
        snapshot,
        {"type": "executing", "data": {"prompt_id": "prompt-1", "node": "11"}},
        "prompt-1",
        total_node_count=4,
    )
    comfy_execution.apply_ws_message_to_progress(
        snapshot,
        {"type": "progress", "data": {"prompt_id": "prompt-1", "node": "11", "value": 5, "max": 100}},
        "prompt-1",
        total_node_count=4,
    )

    assert first_node_done == 25
    assert snapshot["node_percent"] == 5
    assert snapshot["percent"] >= first_node_done
    assert snapshot["percent"] < 30


def test_ws_executed_message_exposes_partial_output_refs():
    snapshot = {
        "prompt_id": "prompt-compare",
        "phase": "running",
        "percent": 0,
        "current": 0,
        "max": 0,
        "completed": False,
    }
    workflow = {
        "51": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["8", 0]},
            "_meta": {"title": "比較 #1: base-a.safetensors"},
        }
    }

    updated = comfy_execution.apply_ws_message_to_progress(
        snapshot,
        {
            "type": "executed",
            "data": {
                "prompt_id": "prompt-compare",
                "node": "51",
                "output": {"images": [{"filename": "partial.png", "subfolder": "", "type": "temp"}]},
            },
        },
        "prompt-compare",
        total_node_count=4,
        workflow=workflow,
    )

    assert updated is True
    assert snapshot["partial_output_count"] == 1
    assert snapshot["partial_outputs"]["prompt_id"] == "prompt-compare"
    assert snapshot["partial_outputs"]["images"][0]["filename"] == "partial.png"
    assert snapshot["partial_outputs"]["images"][0]["output_node_id"] == "51"
    assert snapshot["partial_outputs"]["images"][0]["output_label"] == "比較 #1: base-a.safetensors"


def test_generate_from_workflow_retries_transient_output_fetch(monkeypatch):
    progress_events = []

    class ReadyClient:
        timeout = 1

        def _json_request(self, path, *, method="GET", payload=None, timeout=None):
            if path == "/prompt":
                return {"prompt_id": "prompt-2"}
            assert path == "/history/prompt-2"
            return {
                "prompt-2": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {"9": {"images": [{"filename": "done.png", "subfolder": "", "type": "output"}]}},
                }
            }

    fetch_calls = {"count": 0}

    def flaky_fetcher(image_ref):
        fetch_calls["count"] += 1
        if fetch_calls["count"] == 1:
            raise RuntimeError("ComfyUI 連線失敗：timed out")
        assert image_ref == {"filename": "done.png", "subfolder": "", "type": "output"}
        return _GeneratedImage()

    monkeypatch.setattr(comfy_execution.time, "sleep", lambda _seconds: None)

    result = comfy_execution.generate_from_workflow(
        ReadyClient(),
        {"3": {"class_type": "KSampler", "inputs": {}}},
        timeout_seconds=10,
        expected_count=1,
        progress_callback=progress_events.append,
        error_cls=RuntimeError,
        image_fetcher=flaky_fetcher,
    )

    assert fetch_calls["count"] == 2
    assert result["prompt_id"] == "prompt-2"
    assert result["images"][0]["image_ref"] == {"filename": "done.png", "subfolder": "", "type": "output"}
    assert any(event.get("phase") == "fetching_output" for event in progress_events)


def test_generate_from_workflow_can_skip_output_fetching(monkeypatch):
    class ReadyClient:
        timeout = 1

        def _json_request(self, path, *, method="GET", payload=None, timeout=None):
            if path == "/prompt":
                return {"prompt_id": "prompt-3"}
            assert path == "/history/prompt-3"
            return {
                "prompt-3": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {"9": {"images": [{"filename": "done.png", "subfolder": "", "type": "output"}]}},
                }
            }

    def forbidden_fetcher(_image_ref):
        raise AssertionError("fetch_outputs=False must not pull image bytes into the web job result")

    monkeypatch.setattr(comfy_execution.time, "sleep", lambda _seconds: None)

    result = comfy_execution.generate_from_workflow(
        ReadyClient(),
        {"3": {"class_type": "KSampler", "inputs": {}}},
        timeout_seconds=10,
        expected_count=1,
        fetch_outputs=False,
        error_cls=RuntimeError,
        image_fetcher=forbidden_fetcher,
    )

    assert result["prompt_id"] == "prompt-3"
    assert result["image_ref"] == {"filename": "done.png", "subfolder": "", "type": "output"}
    assert result["data"] == b""
    assert result["images"] == [{
        "image_ref": {"filename": "done.png", "subfolder": "", "type": "output"},
        "mime_type": "image/png",
        "data": b"",
        "size_bytes": 0,
    }]


def test_delete_queue_items_deletes_only_supplied_prompt_ids():
    calls = []

    class QueueClient:
        def _json_request(self, path, *, method="GET", payload=None, timeout=None, allow_non_json=False):
            calls.append({
                "path": path,
                "method": method,
                "payload": payload,
                "timeout": timeout,
                "allow_non_json": allow_non_json,
            })
            return {}

    result = comfy_execution.delete_queue_items(
        QueueClient(),
        ["prompt-1", "", None, "prompt-2"],
        timeout_seconds=7,
    )

    assert result == {}
    assert calls == [{
        "path": "/queue",
        "method": "POST",
        "payload": {"delete": ["prompt-1", "prompt-2"]},
        "timeout": 7,
        "allow_non_json": True,
    }]
