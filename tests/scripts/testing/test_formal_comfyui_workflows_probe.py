from __future__ import annotations

from argparse import Namespace
from io import BytesIO
import json
import os
import sys
from pathlib import Path

import pytest

import scripts.testing.formal_comfyui_workflows_probe as formal_probe

from scripts.testing.formal_comfyui_workflows_probe import (
    REQUIRED_FEATURE_ROWS,
    ProbeFailure,
    contract_status,
    require_campaign_comfyui_url,
    run_child,
    select_safe_gguf_profile,
    validate_feature_report,
    validate_final_model_safety_receipt,
    validate_image_bytes,
    validate_official_report,
    validate_workflow_input_cleanup,
    wait_job,
)
from services.comfyui.template.seeding import SYSTEM_WORKFLOW_IDS
from services.comfyui.workflow.final_model_safety import verify_final_graph_model_safety


def test_campaign_comfyui_url_is_mandatory_and_origin_only():
    assert require_campaign_comfyui_url({"HACKME_CAMPAIGN_COMFYUI_API_URL": "https://127.0.0.1:8188/"}) == "https://127.0.0.1:8188"

    for value in ("", "ftp://127.0.0.1:8188", "http://user:pass@127.0.0.1:8188", "http://127.0.0.1:8188/api", "http://127.0.0.1:99999"):
        with pytest.raises(ProbeFailure):
            require_campaign_comfyui_url({"HACKME_CAMPAIGN_COMFYUI_API_URL": value})


def test_main_rejects_unsafe_all_official_model_plan_before_live_backend_or_canary(
    tmp_path,
    monkeypatch,
):
    out_dir = tmp_path / "formal-early-model-reject"
    models_root = tmp_path / "models"
    models_root.mkdir()
    args = Namespace(
        out_dir=str(out_dir),
        base_url="https://127.0.0.1:59999",
        safe_gguf_max_bytes=formal_probe.SAFE_MODEL_MAX_FILE_BYTES,
        safety_min_mem_available_bytes=formal_probe.GIB,
        safety_min_disk_free_bytes=20 * formal_probe.GIB,
        safety_max_queue_depth=1,
        safety_cancel_grace_seconds=45,
        safe_canary_only=False,
    )
    unsafe = {
        "schema_version": "hackme.formal-comfyui-model-safety/v1",
        "ok": False,
        "safe_workflow_count": 0,
        "expected_workflow_count": len(SYSTEM_WORKFLOW_IDS),
        "unsafe_workflows": list(SYSTEM_WORKFLOW_IDS),
    }
    live_calls = []
    monkeypatch.setattr(formal_probe, "parse_args", lambda: args)
    monkeypatch.setattr(
        formal_probe,
        "require_campaign_comfyui_url",
        lambda: "http://127.0.0.1:8188",
    )
    monkeypatch.setattr(formal_probe, "require_comfyui_models_root", lambda: models_root)
    monkeypatch.setattr(
        formal_probe,
        "audit_official_workflow_model_safety",
        lambda _root: dict(unsafe),
    )
    monkeypatch.setattr(
        formal_probe,
        "require_backend_scope_evidence",
        lambda *_args, **_kwargs: live_calls.append("backend_scope"),
    )
    monkeypatch.setattr(
        formal_probe,
        "run_safe_gguf_canary",
        lambda *_args, **_kwargs: live_calls.append("canary"),
    )

    assert formal_probe.main() == 1
    assert live_calls == []
    report = json.loads(
        (out_dir / "formal_comfyui_workflows_probe.json").read_text(encoding="utf-8")
    )
    assert report["sections"]["model_safety_preflight"] == unsafe
    assert "before live canary" in report["errors"][0]["message"]


def test_feature_report_requires_every_named_row_to_be_an_exact_pass():
    report = {
        "ok": True,
        "results": [
            {"name": name, "ok": True, "status": "pass"}
            for name in sorted(REQUIRED_FEATURE_ROWS)
        ],
    }
    assert validate_feature_report(report)["ok"] is True

    report["results"][0]["status"] = "expected_unavailable"
    report["results"][0]["ok"] = True
    result = validate_feature_report(report)
    assert result["ok"] is False
    assert result["non_pass"]


def test_official_report_requires_the_exact_system_registry_and_zero_diagnostics():
    report = {
        "summary": {
            "template_count": len(SYSTEM_WORKFLOW_IDS),
            "passed": len(SYSTEM_WORKFLOW_IDS),
            "failed": 0,
            "completed_with_issues": 0,
        },
        "results": [
            {
                "bundle_id": bundle_id,
                "status": "passed",
                "issues": [],
                "page_errors": [],
                "run_response": {
                    "json": {
                        "media_remap_run_id": "",
                        "input_assignment_count": 0,
                    },
                },
                "job": {
                    "result": {
                        "input_assignment_count": 0,
                        "input_cleanup": {
                            "schema_version": 1,
                            "run_id": "",
                            "ok": True,
                            "absence_verified": True,
                            "detail": "no_temp_inputs",
                            "input_ref_count": 0,
                        },
                    },
                },
            }
            for bundle_id in SYSTEM_WORKFLOW_IDS
        ],
        "console_events": [],
        "page_errors": [],
        "network_errors": [],
        "connection": {"status": 200, "ok": True, "body": {"ok": True}},
    }
    assert validate_official_report(report)["ok"] is True

    report["results"][-1]["status"] = "skipped_heavy"
    assert validate_official_report(report)["ok"] is False
    report["results"][-1]["status"] = "passed"
    report["network_errors"] = [{"url": "http://backend/output", "failure": "reset"}]
    assert validate_official_report(report)["ok"] is False


def test_official_report_malformed_row_fails_closed_instead_of_crashing():
    result = validate_official_report({
        "summary": {},
        "results": [None],
        "console_events": [],
        "page_errors": [],
        "network_errors": [],
    })
    assert result["ok"] is False
    assert result["bad_status"]["row_0"]["status"] == "malformed"


def test_workflow_input_cleanup_correlates_acceptance_terminal_and_absence_proof():
    run_id = "run-input-abc"
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "ok": True,
        "absence_verified": True,
        "input_ref_count": 1,
        "cleanup": {
            "ok": True,
            "absence_verified": True,
            "method": "remote_delete_and_get",
            "binding_verified": False,
            "local_binding": {"binding_verified": False},
            "directory_absent": None,
            "refs": [{
                "ref": {"filename": "source.png", "subfolder": run_id, "type": "input"},
                "absent": True,
                "verification": "http_404",
            }],
        },
    }
    accepted = {"media_remap_run_id": run_id, "input_assignment_count": 1}
    job = {"result": {"input_assignment_count": 1, "input_cleanup": receipt}}

    assert validate_workflow_input_cleanup(accepted, job)["ok"] is True

    receipt["cleanup"]["refs"][0]["absent"] = False
    failed = validate_workflow_input_cleanup(accepted, job)
    assert failed["ok"] is False
    assert "cleanup_ref_0_not_exact" in failed["reasons"]

    receipt["cleanup"]["refs"][0]["absent"] = True
    receipt["cleanup"]["local_binding"] = {"binding_verified": True}
    failed = validate_workflow_input_cleanup(accepted, job)
    assert failed["ok"] is False
    assert "remote_cleanup_binding_evidence_invalid" in failed["reasons"]


def test_workflow_input_cleanup_accepts_only_exact_bound_local_listener_receipt():
    run_id = "run-input-local"
    project_dir = "/srv/ComfyUI"
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "ok": True,
        "absence_verified": True,
        "input_ref_count": 1,
        "cleanup": {
            "ok": True,
            "absence_verified": True,
            "method": "local_filesystem",
            "binding_verified": True,
            "listener_pid": 4321,
            "listener_inode": "98765",
            "listener_cwd": project_dir,
            "directory_absent": True,
            "local_binding": {
                "binding_verified": True,
                "project_dir": project_dir,
                "listeners": [{
                    "pid": 4321,
                    "inode": "98765",
                    "cwd": project_dir,
                    "cwd_matches_project": True,
                }],
            },
            "refs": [{
                "ref": {"filename": "source.png", "subfolder": run_id, "type": "input"},
                "absent": True,
                "verification": "local_lstat",
            }],
        },
    }
    accepted = {"media_remap_run_id": run_id, "input_assignment_count": 1}
    job = {"result": {"input_assignment_count": 1, "input_cleanup": receipt}}

    assert validate_workflow_input_cleanup(accepted, job)["ok"] is True

    receipt["cleanup"]["local_binding"]["listeners"][0]["cwd_matches_project"] = False
    failed = validate_workflow_input_cleanup(accepted, job)
    assert failed["ok"] is False
    assert "local_backend_binding_or_directory_absence_invalid" in failed["reasons"]


def test_final_model_safety_receipt_validator_recomputes_hash_and_rejects_tampering(tmp_path):
    models_root = tmp_path / "models"
    model_path = models_root / "checkpoints" / "safe.safetensors"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"formal-final-model")
    _submitted, receipt = verify_final_graph_model_safety(
        {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "safe.safetensors"},
            },
        },
        models_root=models_root,
        backend_url="http://127.0.0.1:8188",
    )
    job = {
        "result": {
            "prompt_id": "prompt-safe",
            "backend_url": "http://127.0.0.1:8188",
            "final_model_safety": receipt,
            "final_model_safety_backend_binding": {
                "schema_version": "hackme.comfyui-final-model-safety-backend-binding/v1",
                "ok": True,
                "prompt_id": "prompt-safe",
                "graph_sha256": receipt["graph_sha256"],
                "receipt_sha256": receipt["receipt_sha256"],
                "history_prompt_tuple_minimum_fields": 4,
                "history_graph_verified": True,
                "history_marker_verified": True,
            },
        },
    }
    env = {"HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT": str(models_root)}

    valid = validate_final_model_safety_receipt(
        job,
        expected_backend_url="http://127.0.0.1:8188",
        environ=env,
    )
    assert valid["ok"] is True
    assert valid["receipt_sha256"] == valid["recomputed_receipt_sha256"]
    assert valid["terminal_model_files_unchanged"] is True
    assert valid["terminal_model_file_revalidated_count"] == 1
    assert valid["backend_history_binding_verified"] is True

    model_path.write_bytes(b"formal-MUTATED-model")
    changed = validate_final_model_safety_receipt(
        job,
        expected_backend_url="http://127.0.0.1:8188",
        environ=env,
    )
    assert changed["ok"] is False
    assert changed["terminal_model_files_unchanged"] is False
    assert any(
        error.startswith("terminal_model_file_revalidation_failed:")
        for error in changed["errors"]
    )
    model_path.write_bytes(b"formal-final-model")

    receipt["model_files"][0]["sha256"] = "0" * 64
    tampered = validate_final_model_safety_receipt(
        job,
        expected_backend_url="http://127.0.0.1:8188",
        environ=env,
    )
    assert tampered["ok"] is False
    assert "receipt_sha256_mismatch" in tampered["errors"]

    receipt["model_files"][0]["stat"]["inode"] = "not-an-integer"
    malformed = validate_final_model_safety_receipt(
        job,
        expected_backend_url="http://127.0.0.1:8188",
        environ=env,
    )
    assert malformed["ok"] is False
    assert "model_file_0_stat_receipt_invalid" in malformed["errors"]


def test_image_validation_decodes_real_pixels_and_rejects_corruption():
    image_module = pytest.importorskip("PIL.Image")
    image = image_module.new("RGB", (64, 64), "black")
    for x in range(32, 64):
        for y in range(64):
            image.putpixel((x, y), (255, 80, 10))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    result = validate_image_bytes(buffer.getvalue(), expected_mime="image/png")
    assert result["format"] == "PNG"
    assert result["width"] == 64
    assert result["height"] == 64
    assert len(result["sha256"]) == 64

    with pytest.raises(ProbeFailure):
        validate_image_bytes(b"not-an-image", expected_mime="image/png")


def test_contract_status_has_no_implicit_success_for_missing_sections():
    empty = contract_status({"sections": {}})
    assert empty
    assert all(value is False for value in empty.values())

    sections = {
        "real_backend": {"ok": True},
        "safety": {"ok": True},
        "feature_probe": {"ok": True},
        "dependency_preflight": {"ok": True},
        "official_templates": {"ok": True},
        "custom_workflow": {"ok": True},
        "ai_agent_generation": {"ok": True},
        "workflow_ui": {"ok": True},
        "offline_failure": {"ok": True},
    }
    assert all(contract_status({"sections": sections}).values())


def test_safe_gguf_selection_uses_exact_allowlist_not_inventory_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Client:
        def __init__(self, payload):
            self.payload = payload

        def json(self, method, path, payload=None):
            assert (method, path, payload) == ("GET", "/api/comfyui/installed-gguf", None)
            return 200, self.payload

    profile = {
        "id": "diving_illustrious_flat_anime_sdxl",
        "enabled": True,
        "status": "verified_q4_smoke",
        "variants": [{
            "id": "q4_k_m",
            "enabled": True,
            "status": "verified_q4_smoke",
            "gguf_file": "diving-illustrious-flat-anime-paradigm-shift.Q4_K_M.gguf",
            "size_bytes": 1_446_633_120,
        }],
        "companions": [
            {"slot": "clip_name1", "filename": "clip_l.safetensors"},
            {"slot": "clip_name2", "filename": "clip_g.safetensors"},
        ],
    }
    approved = {
        "installed": True,
        "official_profile": True,
        "enabled": True,
        "profile_id": profile["id"],
        "variant_id": "q4_k_m",
        "gguf_file": profile["variants"][0]["gguf_file"],
        "size_bytes": 1_446_633_120,
        "name": "models/unet/" + profile["variants"][0]["gguf_file"],
    }
    payload = {
        "ok": True,
        "comfyui_url": "http://127.0.0.1:8188",
        "installed_gguf_models": [
            {
                "installed": True,
                "official_profile": False,
                "enabled": True,
                "name": "first-but-unapproved.gguf",
                "size_bytes": 1,
            },
            approved,
        ],
        "gguf_profiles": [profile],
    }

    models_root = tmp_path / "models"
    exact_files = {
        models_root / "diffusion_models" / profile["variants"][0]["gguf_file"]: 1_446_633_120,
        models_root / "text_encoders" / "clip_l.safetensors": 246_144_378,
        models_root / "text_encoders" / "clip_g.safetensors": 1_389_363_370,
        models_root / "vae" / "illustrious_vae.safetensors": 167_340_358,
    }
    for path, size in exact_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(size)
    monkeypatch.setattr(formal_probe, "sha256_file", lambda _path: "a" * 64)

    selected = select_safe_gguf_profile(
        Client(payload),
        expected_comfyui_url="http://127.0.0.1:8188",
        max_size_bytes=2 * 1024 * 1024 * 1024,
        models_root=models_root,
    )

    assert selected["profile_id"] == profile["id"]
    assert selected["variant_id"] == "q4_k_m"
    assert selected["selection_rule"] == "first_exact_match_in_versioned_allowlist"

    payload["installed_gguf_models"][1] = {**approved, "size_bytes": 1_446_633_121}
    with pytest.raises(ProbeFailure, match="explicit formal allowlist"):
        select_safe_gguf_profile(
            Client(payload),
            expected_comfyui_url="http://127.0.0.1:8188",
            max_size_bytes=2 * 1024 * 1024 * 1024,
            models_root=models_root,
        )


def test_wait_job_hard_stop_invokes_bounded_abort_before_failing():
    class Client:
        def json(self, method, path, payload=None):
            assert method == "GET"
            return 200, {"ok": True, "job": {"job_id": "job-1", "status": "running"}}

    class Monitor:
        def __init__(self):
            self.abort_calls = []

        def sample(self, phase, **kwargs):
            return {
                "missing_fields": [],
                "collector_errors": [],
                "hard_limit_state": {"ok": False, "reasons": ["host_mem_available_below_limit"]},
            }

        def abort_site_work(self, client, *, reason, job_id=""):
            self.abort_calls.append((reason, job_id))
            return {"ok": True, "terminal_verified": True, "queue_empty_verified": True}

    monitor = Monitor()
    with pytest.raises(ProbeFailure, match="stopped by ComfyUI safety monitor"):
        wait_job(
            Client(),
            "job-1",
            timeout_seconds=30,
            safety_monitor=monitor,
            phase="unit_job",
        )

    assert monitor.abort_calls == [("unit_job_resource_or_collector_hard_stop", "job-1")]


def test_wait_job_poll_exception_attempts_bounded_abort_before_failing():
    class Client:
        def json(self, method, path, payload=None):
            raise TimeoutError("poll deadline")

    class Monitor:
        def __init__(self):
            self.abort_calls = []

        def abort_site_work(self, client, *, reason, job_id=""):
            self.abort_calls.append((reason, job_id))
            return {"ok": True, "terminal_verified": True, "queue_empty_verified": True}

    monitor = Monitor()
    with pytest.raises(ProbeFailure, match="poll raised TimeoutError"):
        wait_job(
            Client(),
            "job-poll-timeout",
            timeout_seconds=30,
            safety_monitor=monitor,
            phase="unit_job",
        )

    assert monitor.abort_calls == [("unit_job_job_poll_exception", "job-poll-timeout")]


def test_run_child_uses_an_isolated_process_group_and_leaves_none(tmp_path):
    result = run_child(
        [sys.executable, "-c", "print('formal-child-ok')"],
        env=dict(os.environ),
        log_path=tmp_path / "child.log",
        timeout_seconds=60,
        monitor_phase="unit_child",
    )

    assert result["exit_code"] == 0
    assert result["process_group_isolated"] is True
    assert result["cleanup"]["group_gone"] is True
    assert (tmp_path / "child.log").read_text(encoding="utf-8").strip() == "formal-child-ok"


def test_run_child_collector_exception_kills_process_group_and_aborts_site(tmp_path):
    pid_path = tmp_path / "child.pid"

    class Monitor:
        max_queue_depth = 1

        def __init__(self):
            self.samples = 0
            self.abort_calls = []

        def sample(self, phase, **kwargs):
            self.samples += 1
            if self.samples > 1:
                raise RuntimeError("collector exploded")
            return {
                "missing_fields": [],
                "collector_errors": [],
                "hard_limit_state": {"ok": True},
            }

        def abort_site_work(self, client, *, reason, job_id=""):
            self.abort_calls.append((reason, job_id))
            return {"ok": True, "terminal_verified": True, "queue_empty_verified": True}

    monitor = Monitor()
    code = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    with pytest.raises(ProbeFailure, match="safety collector raised RuntimeError"):
        run_child(
            [sys.executable, "-c", code],
            env=dict(os.environ),
            log_path=tmp_path / "collector-child.log",
            timeout_seconds=60,
            safety_monitor=monitor,
            site_client=object(),
            monitor_phase="unit_child",
        )

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert monitor.abort_calls == [("unit_child_collector_exception", "")]
