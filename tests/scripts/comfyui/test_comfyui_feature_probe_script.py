import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "comfyui" / "feature_probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("comfyui_feature_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_comfyui_feature_probe_help_lists_supported_modes():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--base-url" in result.stdout
    assert "--controlnet-type" in result.stdout
    assert "--model" in result.stdout
    assert "--checkpoint-model" in result.stdout
    assert "--upscale-model" in result.stdout
    assert "--controlnet-model" in result.stdout
    assert "--controlnet-preprocessor" in result.stdout
    assert "--cancel-grace" in result.stdout
    assert "--http-timeout" in result.stdout


def test_comfyui_feature_probe_mentions_core_generation_modes():
    text = SCRIPT.read_text(encoding="utf-8")
    for keyword in ("txt2img", "img2img", "inpaint", "outpaint", "upscale", "history_rerun", "controlnet"):
        assert keyword in text


def test_checkpoint_selection_uses_explicit_exact_model_not_first_inventory_item():
    module = _load_module()
    payload = {
        "_http_status": 200,
        "ok": True,
        "models": ["unsafe-large.safetensors", "approved-small.safetensors"],
    }

    selected = module.select_checkpoint_model(payload, "approved-small.safetensors")

    assert selected == "approved-small.safetensors"


@pytest.mark.parametrize(
    ("payload", "requested", "message"),
    [
        ({"_http_status": 200, "ok": True, "models": ["first.safetensors"]}, "", "--model"),
        (
            {"_http_status": 200, "ok": True, "models": ["first.safetensors"]},
            "missing.safetensors",
            "不在 ComfyUI models inventory",
        ),
        ({"_http_status": 503, "ok": False, "models": []}, "first.safetensors", "inventory 不可用"),
        (
            {"_http_status": 200, "ok": True, "models": ["first.safetensors", None]},
            "first.safetensors",
            "非字串或空白",
        ),
    ],
)
def test_checkpoint_selection_fails_closed(payload, requested, message):
    module = _load_module()

    with pytest.raises(module.ProbeError, match=message):
        module.select_checkpoint_model(payload, requested)


def test_unknown_explicit_model_stops_before_any_generation_request(monkeypatch):
    module = _load_module()

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            self.paths = []

        def login(self, *_args, **_kwargs):
            return {"ok": True}

        def get_json(self, path, **_kwargs):
            self.paths.append(path)
            if path == "/api/comfyui/status":
                return {"_http_status": 200, "available": True}
            if path == "/api/comfyui/models":
                return {
                    "_http_status": 200,
                    "ok": True,
                    "models": ["unsafe-first.safetensors"],
                    "upscale_models": [],
                    "controlnet_models": [],
                }
            raise AssertionError(f"unexpected GET after failed model selection: {path}")

        def post_json(self, *_args, **_kwargs):
            raise AssertionError("generation must not be queued when the requested model is absent")

        def post_multipart(self, *_args, **_kwargs):
            raise AssertionError("generation must not be queued when the requested model is absent")

    fake = FakeClient()
    monkeypatch.setattr(module, "WebClient", lambda *_args, **_kwargs: fake)
    args = argparse.Namespace(
        base_url="https://127.0.0.1:59999",
        insecure=True,
        username="root",
        password="unused",
        model="approved-small.safetensors",
        timeout=5,
        controlnet_type="canny",
    )

    report = module.run_probe(args)

    assert report["ok"] is False
    assert fake.paths == ["/api/comfyui/status", "/api/comfyui/models"]
    models_result = report["results"][-1]
    assert models_result["name"] == "models"
    assert models_result["status"] == "fail"
    assert models_result["payload"]["selected_model"] == ""
    assert models_result["payload"]["selection_rule"] == "explicit_exact_inventory_match_no_fallback"


def test_sync_ok_without_job_is_never_accepted_and_attempts_bounded_interrupt():
    module = _load_module()

    class FakeClient:
        def post_json(self, path, _payload, **_kwargs):
            assert path == "/api/comfyui/interrupt"
            return {
                "_http_status": 200,
                "ok": True,
                "interrupt": {"backend_interrupted": False, "reason": "no_owned_generation"},
            }

    row, evidence = module.execute_generation_step(
        FakeClient(),
        step_name="txt2img",
        submit=lambda: {"_http_status": 200, "ok": True},
        timeout_seconds=5,
        cancel_grace_seconds=1,
        validation_kwargs={},
    )

    assert evidence is None
    assert row["status"] == "fail"
    assert "禁止同步 ok/no-job PASS" in row["detail"]
    assert row["payload"]["abort_receipt"]["attempted"] is True
    assert row["payload"]["abort_receipt"]["ok"] is False


def test_job_timeout_interrupts_and_verifies_terminal_state(monkeypatch):
    module = _load_module()

    class Clock:
        value = 0.0

        def monotonic(self):
            self.value += 0.6
            return self.value

    class FakeClient:
        interrupted = False

        def get_json(self, path, **_kwargs):
            assert path == "/api/comfyui/jobs/job-1"
            return {
                "_http_status": 200,
                "ok": True,
                "job": {
                    "job_id": "job-1",
                    "status": "cancelled" if self.interrupted else "running",
                },
            }

        def post_json(self, path, _payload, **_kwargs):
            assert path == "/api/comfyui/interrupt"
            self.interrupted = True
            return {
                "_http_status": 200,
                "ok": True,
                "interrupt": {"backend_interrupted": True},
            }

    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    with pytest.raises(module.JobExecutionError) as captured:
        module.wait_for_job(FakeClient(), "job-1", timeout_seconds=1, cancel_grace_seconds=1)

    receipt = captured.value.abort_receipt
    assert receipt["attempted"] is True
    assert receipt["backend_interrupted"] is True
    assert receipt["terminal_observed"] is True
    assert receipt["terminal_status"] == "cancelled"
    assert receipt["bounded_stop_acknowledged"] is True
    assert receipt["backend_queue_absence_verified"] is False
    assert receipt["exact"] is False
    assert receipt["ok"] is False


def test_non_success_terminal_job_still_requests_bounded_backend_interrupt(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    class FakeClient:
        interrupted = 0

        def get_json(self, path, **_kwargs):
            assert path == "/api/comfyui/jobs/job-error"
            return {
                "_http_status": 200,
                "ok": True,
                "job": {"job_id": "job-error", "status": "error", "error": "backend failed"},
            }

        def post_json(self, path, _payload, **_kwargs):
            assert path == "/api/comfyui/interrupt"
            self.interrupted += 1
            return {
                "_http_status": 200,
                "ok": True,
                "interrupt": {"backend_interrupted": True},
            }

    client = FakeClient()
    row, evidence = module.execute_generation_step(
        client,
        step_name="txt2img",
        submit=lambda: {
            "_http_status": 200,
            "ok": True,
            "async": True,
            "job": {"job_id": "job-error", "status": "queued"},
        },
        timeout_seconds=5,
        cancel_grace_seconds=1,
        validation_kwargs={},
    )

    assert evidence is None
    assert row["status"] == "fail"
    assert "non-success terminal state" in row["detail"]
    assert client.interrupted == 1
    assert row["payload"]["abort_receipt"]["bounded_stop_acknowledged"] is True
    assert row["payload"]["abort_receipt"]["exact"] is False


def _terminal_payload(module, *, history_id=17):
    image_ref = {"filename": "probe-output.png", "subfolder": "", "type": "output"}
    image = {
        "prompt_id": "prompt-17",
        "image_ref": image_ref,
        "mime_type": "image/png",
        "size_bytes": len(module.SOURCE_PNG),
    }
    return {
        "_http_status": 200,
        "ok": True,
        "job": {
            "job_id": "job-17",
            "status": "completed",
            "result": {
                "history_id": history_id,
                "image": image,
                "images": [image],
            },
        },
    }


def _correlated_history(module, *, history_id=17):
    output_ref = {"filename": "probe-output.png", "subfolder": "", "type": "output"}
    input_ref = {"filename": "backend-prefix_unique-source.png", "subfolder": "", "type": "input"}
    return {
        "id": history_id,
        "generation_mode": "img2img",
        "payload": {
            "model": "approved.safetensors",
            "prompt": "hackme_web img2img probe run-17",
        },
        "input_assets": {
            "source_image_ref": input_ref,
            "mask_image_ref": None,
            "control_image_ref": None,
        },
        "controlnet": {},
        "result": {
            "prompt_id": "prompt-17",
            "images": [{"image_ref": output_ref, "mime_type": "image/png", "size_bytes": len(module.SOURCE_PNG)}],
        },
    }


def test_terminal_generation_requires_exact_job_history_prompt_output_and_input_correlation():
    module = _load_module()
    history = _correlated_history(module)

    class FakeClient:
        def get_json(self, path, **_kwargs):
            assert path == "/api/comfyui/history"
            return {"_http_status": 200, "ok": True, "history": [history]}

        def post_json(self, path, payload, **_kwargs):
            assert path == "/api/comfyui/image-preview"
            assert payload["image_ref"]["filename"] == "probe-output.png"
            return {
                "_http_status": 200,
                "ok": True,
                "image": {
                    "image_ref": payload["image_ref"],
                    "mime_type": "image/png",
                    "size_bytes": len(module.SOURCE_PNG),
                    "data_url": "data:image/png;base64," + module.base64.b64encode(module.SOURCE_PNG).decode("ascii"),
                },
            }

    evidence = module.validate_terminal_generation(
        FakeClient(),
        _terminal_payload(module),
        job_id="job-17",
        step_name="img2img",
        expected_mode="img2img",
        expected_model="approved.safetensors",
        expected_prompt="hackme_web img2img probe run-17",
        baseline_history_ids={1, 2},
        expected_inputs={"source_image_ref": "unique-source.png"},
    )

    assert evidence["history_id"] == 17
    assert evidence["prompt_id"] == "prompt-17"
    assert evidence["output_count"] == 1
    assert all(evidence["correlation"].values())
    assert evidence["outputs"][0]["decoded"] is True


def test_terminal_generation_rejects_an_arbitrary_unrelated_history_row():
    module = _load_module()
    unrelated = _correlated_history(module, history_id=99)

    class FakeClient:
        def get_json(self, _path, **_kwargs):
            return {"_http_status": 200, "ok": True, "history": [unrelated]}

        def post_json(self, _path, payload, **_kwargs):
            return {
                "_http_status": 200,
                "ok": True,
                "image": {
                    "image_ref": payload["image_ref"],
                    "mime_type": "image/png",
                    "size_bytes": len(module.SOURCE_PNG),
                    "data_url": "data:image/png;base64," + module.base64.b64encode(module.SOURCE_PNG).decode("ascii"),
                },
            }

    with pytest.raises(module.ProbeError, match="精確匹配數不是 1"):
        module.validate_terminal_generation(
            FakeClient(),
            _terminal_payload(module, history_id=17),
            job_id="job-17",
            step_name="img2img",
            expected_mode="img2img",
            expected_model="approved.safetensors",
            expected_prompt="hackme_web img2img probe run-17",
            baseline_history_ids=set(),
            expected_inputs={"source_image_ref": "unique-source.png"},
        )


def test_preview_magic_bytes_without_a_decodable_image_fail_closed():
    module = _load_module()
    corrupt = b"\x89PNG\r\n\x1a\n" + (b"not-a-real-png" * 8)
    image_ref = {"filename": "corrupt.png", "subfolder": "", "type": "output"}

    class FakeClient:
        def post_json(self, _path, payload, **_kwargs):
            return {
                "_http_status": 200,
                "ok": True,
                "image": {
                    "image_ref": payload["image_ref"],
                    "mime_type": "image/png",
                    "size_bytes": len(corrupt),
                    "data_url": "data:image/png;base64," + module.base64.b64encode(corrupt).decode("ascii"),
                },
            }

    with pytest.raises(module.ProbeError, match="image decoder"):
        module._validate_preview(FakeClient(), image_ref=image_ref, expected_size=len(corrupt))


def test_remote_input_cleanup_is_explicit_immutable_residual_and_not_exact():
    module = _load_module()

    class FakeClient:
        def post_json(self, path, _payload, **_kwargs):
            assert path == "/api/comfyui/discard"
            return {
                "_http_status": 200,
                "ok": True,
                "warning": "source_file_not_deleted",
                "discard": {
                    "file_deleted": False,
                    "file_missing": False,
                    "remote_preview_only": True,
                },
            }

    cleanup = module.cleanup_probe_inputs(
        FakeClient(),
        [{
            "step": "img2img",
            "field": "source_image",
            "correlated": True,
            "image_ref": {"filename": "unique-source.png", "subfolder": "", "type": "input"},
        }],
    )

    assert cleanup["exact"] is False
    assert cleanup["exact_deleted_or_missing_count"] == 0
    assert len(cleanup["immutable_residuals"]) == 1
    assert cleanup["rows"][0]["immutable_residual"] is True


def test_input_cleanup_rejects_delete_ack_without_absence_proof():
    module = _load_module()

    class FakeClient:
        def post_json(self, path, _payload, **_kwargs):
            assert path == "/api/comfyui/discard"
            return {
                "_http_status": 200,
                "ok": True,
                "discard": {
                    "file_deleted": True,
                    "file_missing": False,
                    "absence_verified": False,
                },
            }

    cleanup = module.cleanup_probe_inputs(
        FakeClient(),
        [{
            "step": "img2img",
            "field": "source_image",
            "correlated": True,
            "image_ref": {"filename": "unique-source.png", "subfolder": "", "type": "input"},
        }],
    )

    assert cleanup["exact"] is False
    assert cleanup["exact_deleted_or_missing_count"] == 0


def test_controlnet_and_upscale_dependencies_require_explicit_exact_inventory_values():
    module = _load_module()
    models = {
        "_http_status": 200,
        "ok": True,
        "upscale_models": ["large.pth", "safe.pth"],
        "controlnet_types": {
            "canny": {
                "available": True,
                "matching_models": ["unsafe-control.safetensors", "safe-control.safetensors"],
                "available_preprocessors": ["CannyEdgePreprocessor"],
            },
        },
    }

    assert module.select_exact_inventory_value(
        models,
        "safe.pth",
        inventory_key="upscale_models",
        label="upscale model",
    ) == "safe.pth"
    selected = module.select_controlnet_dependencies(
        models,
        controlnet_type="canny",
        model_name="safe-control.safetensors",
        preprocessor="CannyEdgePreprocessor",
    )
    assert selected["model_name"] == "safe-control.safetensors"
    with pytest.raises(module.ProbeError, match="禁止後端自動挑第一個"):
        module.select_controlnet_dependencies(
            models,
            controlnet_type="canny",
            model_name="",
            preprocessor="",
        )


def test_full_probe_contract_passes_only_with_correlated_terminal_outputs_and_exact_cleanup(monkeypatch):
    module = _load_module()

    class FakeClient:
        def __init__(self):
            self.jobs = {}
            self.histories = []
            self.next_id = 1

        def login(self, *_args, **_kwargs):
            return {"ok": True}

        def _submit(self, payload, files):
            history_id = self.next_id
            self.next_id += 1
            job_id = f"job-{history_id}"
            prompt_id = f"prompt-{history_id}"
            output_ref = {"filename": f"output-{history_id}.png", "subfolder": "", "type": "output"}
            image = {
                "prompt_id": prompt_id,
                "image_ref": output_ref,
                "mime_type": "image/png",
                "size_bytes": len(module.SOURCE_PNG),
            }
            input_assets = {"source_image_ref": None, "mask_image_ref": None, "control_image_ref": None}
            field_map = {
                "source_image": "source_image_ref",
                "mask_image": "mask_image_ref",
                "control_image": "control_image_ref",
            }
            for item in files:
                input_assets[field_map[item["field"]]] = {
                    "filename": f"backend-prefix_{item['filename']}",
                    "subfolder": "",
                    "type": "input",
                }
            controlnet = {}
            if payload.get("controlnet_enabled"):
                controlnet = {
                    "type": payload["controlnet_type"],
                    "model_name": payload["controlnet_model"],
                    "preprocessor": payload["controlnet_preprocessor"],
                }
            history = {
                "id": history_id,
                "generation_mode": payload.get("generation_mode") or "txt2img",
                "payload": {
                    "model": payload["model"],
                    "prompt": payload["prompt"],
                    "upscale_model": payload.get("upscale_model") or "",
                },
                "input_assets": input_assets,
                "controlnet": controlnet,
                "result": {
                    "prompt_id": prompt_id,
                    "images": [{
                        "image_ref": output_ref,
                        "mime_type": "image/png",
                        "size_bytes": len(module.SOURCE_PNG),
                    }],
                },
            }
            self.histories.insert(0, history)
            self.jobs[job_id] = {
                "_http_status": 200,
                "ok": True,
                "job": {
                    "job_id": job_id,
                    "status": "completed",
                    "result": {
                        "history_id": history_id,
                        "image": image,
                        "images": [image],
                    },
                },
            }
            return {
                "_http_status": 200,
                "ok": True,
                "async": True,
                "job": {"job_id": job_id, "status": "queued"},
            }

        def get_json(self, path, **_kwargs):
            if path == "/api/comfyui/status":
                return {"_http_status": 200, "ok": True, "available": True, "comfyui_url": "http://comfy.test"}
            if path == "/api/comfyui/models":
                return {
                    "_http_status": 200,
                    "ok": True,
                    "models": ["huge.safetensors", "safe.safetensors"],
                    "upscale_models": ["huge.pth", "safe.pth"],
                    "controlnet_models": ["safe-control.safetensors"],
                    "controlnet_types": {
                        "canny": {
                            "available": True,
                            "matching_models": ["safe-control.safetensors"],
                            "available_preprocessors": ["CannyEdgePreprocessor"],
                        },
                    },
                }
            if path == "/api/comfyui/history":
                return {"_http_status": 200, "ok": True, "history": list(self.histories)}
            if path.startswith("/api/comfyui/jobs/"):
                return self.jobs[path.rsplit("/", 1)[-1]]
            raise AssertionError(f"unexpected GET {path}")

        def post_json(self, path, payload, **_kwargs):
            if path == "/api/comfyui/generate":
                return self._submit(dict(payload), [])
            if path.startswith("/api/comfyui/history/") and path.endswith("/rerun"):
                return self._submit(dict(payload), [])
            if path == "/api/comfyui/image-preview":
                return {
                    "_http_status": 200,
                    "ok": True,
                    "image": {
                        "image_ref": payload["image_ref"],
                        "mime_type": "image/png",
                        "size_bytes": len(module.SOURCE_PNG),
                        "data_url": "data:image/png;base64," + module.base64.b64encode(module.SOURCE_PNG).decode("ascii"),
                    },
                }
            if path == "/api/comfyui/discard":
                return {
                    "_http_status": 200,
                    "ok": True,
                    "discard": {
                        "file_deleted": True,
                        "file_missing": False,
                        "absence_verified": True,
                        "verification": "http_404",
                        "remote_preview_only": False,
                        "local_binding": {"binding_verified": False},
                    },
                }
            if path == "/api/comfyui/interrupt":
                raise AssertionError("happy path must not interrupt")
            raise AssertionError(f"unexpected POST {path}")

        def post_multipart(self, path, *, fields, files, **_kwargs):
            assert path == "/api/comfyui/generate"
            return self._submit(dict(fields), list(files))

    fake = FakeClient()
    monkeypatch.setattr(module, "WebClient", lambda *_args, **_kwargs: fake)
    args = argparse.Namespace(
        base_url="https://127.0.0.1:59999",
        insecure=True,
        username="root",
        password="unused",
        model="safe.safetensors",
        upscale_model="safe.pth",
        controlnet_type="canny",
        controlnet_model="safe-control.safetensors",
        controlnet_preprocessor="CannyEdgePreprocessor",
        probe_run_id="unit-contract",
        timeout=5,
        cancel_grace=1,
        http_timeout=1,
    )

    report = module.run_probe(args)

    assert report["ok"] is True, report
    assert report["summary"]["overall_ok"] is True
    assert report["summary"]["created_history_ids"] == [1, 2, 3, 4, 5, 6, 7]
    assert report["summary"]["input_cleanup"]["exact"] is True
    by_name = {item["name"]: item for item in report["results"]}
    for name in ("txt2img", "img2img", "inpaint", "outpaint", "upscale", "controlnet", "history_rerun"):
        assert by_name[name]["status"] == "pass"
        assert by_name[name]["payload"]["evidence"]["correlation"]["result_to_history"] is True
    assert by_name["history_rerun"]["payload"]["rerun_source_history_id"] == 1
    assert by_name["input_cleanup"]["status"] == "pass"
