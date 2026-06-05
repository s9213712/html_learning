import json
import sqlite3
from pathlib import Path

from tests.comfyui._integration_suite import (
    FakeComfyUIClient,
    _await_comfyui_result,
    _build_app,
    _import_workflow_preset,
    _init_db,
)


ROOT = Path(__file__).resolve().parents[3]


def _actor(user_id, username):
    return {
        "id": user_id,
        "username": username,
        "role": "user",
        "member_level": "trusted",
        "effective_level": "trusted",
        "sanction_status": "none",
    }


def _add_user(db_path, user_id, username):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, username, role) VALUES (?, ?, 'user')",
            (int(user_id), username),
        )
        conn.commit()
    finally:
        conn.close()


def test_comfyui_generation_history_delete_is_owner_scoped(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    _add_user(db_path, 2, "bob")

    alice = _build_app(db_path, storage_root, actor=lambda: _actor(1, "alice")).test_client()
    bob = _build_app(db_path, storage_root, actor=lambda: _actor(2, "bob")).test_client()

    generated = alice.post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "owner scoped history",
            "width": 512,
            "height": 512,
            "steps": 12,
            "cfg": 6.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "seed": 123,
            "batch_size": 1,
            "confirm_billing": True,
        },
    )
    history_id = _await_comfyui_result(alice, generated)["history_id"]

    bob_history = bob.get("/api/comfyui/history")
    assert bob_history.status_code == 200
    assert all(str(item.get("id")) != str(history_id) for item in bob_history.get_json()["history"])
    assert bob.post(f"/api/comfyui/history/{history_id}/rerun", json={}).status_code == 404
    assert bob.delete(f"/api/comfyui/history/{history_id}").status_code == 404

    deleted = alice.delete(f"/api/comfyui/history/{history_id}")
    assert deleted.status_code == 200, deleted.get_json()
    assert deleted.get_json()["deleted_id"] == history_id
    alice_history = alice.get("/api/comfyui/history")
    assert alice_history.status_code == 200
    assert all(str(item.get("id")) != str(history_id) for item in alice_history.get_json()["history"])


def test_comfyui_workflow_run_delete_is_owner_scoped(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    _add_user(db_path, 2, "bob")

    alice = _build_app(db_path, storage_root, actor=lambda: _actor(1, "alice")).test_client()
    bob = _build_app(db_path, storage_root, actor=lambda: _actor(2, "bob")).test_client()
    workflow = FakeComfyUIClient().build_generation_workflow({
        "generation_mode": "txt2img",
        "model": "dream.safetensors",
        "prompt": "workflow owner scoped history",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 12,
        "cfg": 6.5,
        "seed": 123,
        "batch_size": 1,
        "sampler_name": "euler",
        "scheduler": "normal",
        "filename_prefix": "owner_scoped",
    })
    preset = _import_workflow_preset(alice, workflow, title="Owner Scoped Workflow", visibility="public")

    started = alice.post(f"/api/comfyui/workflows/{preset['id']}/run", json={})
    assert started.status_code == 200, started.get_json()
    run_id = started.get_json()["workflow_run_id"]
    _await_comfyui_result(alice, started)

    bob_history = bob.get("/api/comfyui/history")
    assert bob_history.status_code == 200
    assert all(item.get("workflow_run_id") != run_id for item in bob_history.get_json()["history"])
    assert bob.post(f"/api/comfyui/workflow-runs/{run_id}/rerun", json={}).status_code == 403
    assert bob.delete(f"/api/comfyui/workflow-runs/{run_id}").status_code == 403

    deleted = alice.delete(f"/api/comfyui/workflow-runs/{run_id}")
    assert deleted.status_code == 200, deleted.get_json()
    assert deleted.get_json()["deleted_id"] == run_id
    alice_history = alice.get("/api/comfyui/history")
    assert alice_history.status_code == 200
    assert all(item.get("workflow_run_id") != run_id for item in alice_history.get_json()["history"])


def test_sdxl_gguf_workflow_run_preserves_positive_prompt_verbatim(tmp_path):
    class PromptGgufClient(FakeComfyUIClient):
        def get_capabilities(self):
            payload = super().get_capabilities()
            payload["available_nodes"] = sorted(set(payload["available_nodes"]) | {
                "UnetLoaderGGUF",
                "DualCLIPLoader",
                "VAELoader",
                "CLIPTextEncode",
                "EmptyLatentImage",
                "KSampler",
                "VAEDecode",
                "SaveImage",
            })
            payload["diffusion_models"] = [
                "WAI-NSFW-Illustrious-v140-Q8_0.gguf",
                "waiNSFWIllustrious_v140-Q8_0.gguf",
                "illustrious-q4_0.gguf",
            ]
            payload["clip_models"] = [
                "clip_l.safetensors",
                "clip_g.safetensors",
                "illustrious_clip_l.safetensors",
                "illustrious_clip_g.safetensors",
            ]
            payload["vaes"] = ["sdxl_vae.safetensors", "illustrious_vae.safetensors"]
            payload["samplers"] = ["euler", "dpmpp_2m"]
            payload["schedulers"] = ["normal", "karras"]
            return payload

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, comfyui_client=PromptGgufClient()).test_client()
    workflow_dir = ROOT / "workflows" / "comfyui" / "origin_sdxl_gguf_txt2img"
    workflow = json.loads((workflow_dir / "workflow.json").read_text(encoding="utf-8"))
    manifest = json.loads((workflow_dir / "manifest.json").read_text(encoding="utf-8"))
    preset = _import_workflow_preset(
        client,
        workflow,
        title="GGUF Prompt Fidelity",
        default_params=manifest["default_params"],
    )
    prompt = "by Ogipote, exactly 2girls, girls love, cat girls, bedroom, hug"
    negative = "solo, 1girl, 3girls, extra people, low quality"
    FakeComfyUIClient.last_workflow = {}

    started = client.post(
        f"/api/comfyui/workflows/{preset['id']}/run",
        json={
            "user_inputs": {
                "3": {"seed": 123, "steps": 20, "cfg": 7, "sampler_name": "euler", "scheduler": "normal"},
                "5": {"width": 1024, "height": 1024, "batch_size": 1},
                "6": {"text": prompt},
                "7": {"text": negative},
            }
        },
    )
    assert started.status_code == 200, started.get_json()
    run_id = started.get_json()["workflow_run_id"]
    _await_comfyui_result(client, started)

    assert FakeComfyUIClient.last_workflow["6"]["inputs"]["text"] == prompt
    assert FakeComfyUIClient.last_workflow["7"]["inputs"]["text"] == negative
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT prompt, negative_prompt, params_json, workflow_json FROM comfyui_workflow_runs WHERE id=?",
            (int(run_id),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["prompt"] == prompt
    assert row["negative_prompt"] == negative
    params = json.loads(row["params_json"])
    workflow_snapshot = json.loads(row["workflow_json"])
    assert params["prompt"] == prompt
    assert workflow_snapshot["6"]["inputs"]["text"] == prompt
