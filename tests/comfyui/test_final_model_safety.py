from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from services.comfyui.execution import generate_from_workflow, queue_prompt_with_client_id
from services.comfyui.template.seeding import SYSTEM_WORKFLOW_IDS
from services.comfyui.workflow import final_model_safety as safety


def _write_model(models_root: Path, folder: str, name: str, data: bytes = b"safe-model") -> Path:
    path = models_root / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _checkpoint_graph(name: str = "safe.safetensors") -> dict:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": name},
        },
    }


def _receipt_digest(receipt: dict) -> str:
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _QueueClient:
    timeout = 1

    def __init__(self, base_url: str = "http://127.0.0.1:8188") -> None:
        self.base_url = base_url
        self.calls = []

    def _json_request(self, path, *, method="GET", payload=None, timeout=None, allow_non_json=False):
        self.calls.append({"path": path, "method": method, "payload": payload})
        return {"prompt_id": "prompt-safe"}


def _enable_campaign(monkeypatch, models_root: Path, url: str = "http://127.0.0.1:8188") -> None:
    monkeypatch.setenv(safety.CAMPAIGN_COMFYUI_API_URL_ENV, url)
    monkeypatch.setenv(safety.CAMPAIGN_COMFYUI_MODELS_ROOT_ENV, str(models_root))


def test_non_campaign_submission_behavior_is_unchanged(monkeypatch):
    monkeypatch.delenv(safety.CAMPAIGN_COMFYUI_API_URL_ENV, raising=False)
    monkeypatch.delenv(safety.CAMPAIGN_COMFYUI_MODELS_ROOT_ENV, raising=False)
    client = _QueueClient()
    graph = _checkpoint_graph("ordinary-production-model.safetensors")

    queued = queue_prompt_with_client_id(
        client,
        graph,
        client_id="ordinary-client",
        extra_data={"existing": "value"},
        error_cls=RuntimeError,
    )

    assert queued["final_model_safety"] is None
    assert client.calls[0]["payload"] == {
        "prompt": graph,
        "client_id": "ordinary-client",
        "extra_data": {"existing": "value"},
    }


def test_final_graph_receipt_binds_canonical_graph_exact_stat_and_hash(tmp_path):
    models_root = tmp_path / "models"
    model_path = _write_model(models_root, "checkpoints", "safe.safetensors", b"exact-safe-model")
    graph = _checkpoint_graph()

    submitted, receipt = safety.verify_final_graph_model_safety(
        graph,
        models_root=models_root,
        backend_url="http://127.0.0.1:8188",
    )

    canonical = json.dumps(
        submitted,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert submitted == graph
    assert submitted is not graph
    assert receipt["schema_version"] == safety.FINAL_MODEL_SAFETY_SCHEMA_VERSION
    assert receipt["ok"] is True
    assert receipt["graph_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert receipt["receipt_sha256"] == _receipt_digest(receipt)
    assert receipt["distinct_model_total_bytes"] == model_path.stat().st_size
    assert receipt["model_files"] == [{
        "relative_path": "checkpoints/safe.safetensors",
        "size_bytes": model_path.stat().st_size,
        "sha256": hashlib.sha256(b"exact-safe-model").hexdigest(),
        "stat": {
            "device": model_path.stat().st_dev,
            "inode": model_path.stat().st_ino,
            "mode": model_path.stat().st_mode,
            "link_count": model_path.stat().st_nlink,
            "size_bytes": model_path.stat().st_size,
            "mtime_ns": model_path.stat().st_mtime_ns,
            "ctime_ns": model_path.stat().st_ctime_ns,
        },
    }]


def test_queue_binds_receipt_marker_to_exact_prompt_graph_before_network(monkeypatch, tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "safe.safetensors")
    _enable_campaign(monkeypatch, models_root)
    client = _QueueClient()
    graph = _checkpoint_graph()

    queued = queue_prompt_with_client_id(
        client,
        graph,
        client_id="client-safe",
        extra_data={"existing": "value"},
        error_cls=RuntimeError,
    )

    assert queued["final_model_safety"]["graph_sha256"]
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["path"] == "/prompt"
    assert call["payload"]["prompt"] == graph
    marker = call["payload"]["extra_data"][safety.FINAL_MODEL_SAFETY_EXTRA_DATA_KEY]
    assert marker == safety.final_model_safety_prompt_marker(queued["final_model_safety"])
    assert marker["graph_sha256"] == queued["final_model_safety"]["graph_sha256"]
    assert call["payload"]["extra_data"]["existing"] == "value"


def test_backend_history_binding_recomputes_graph_and_exact_marker(tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "safe.safetensors")
    submitted, receipt = safety.verify_final_graph_model_safety(
        _checkpoint_graph(),
        models_root=models_root,
        backend_url="http://127.0.0.1:8188",
    )
    marker = safety.final_model_safety_prompt_marker(receipt)
    record = {
        "prompt": [
            1,
            "prompt-safe",
            submitted,
            {safety.FINAL_MODEL_SAFETY_EXTRA_DATA_KEY: marker, "client_id": "client-safe"},
        ],
    }

    binding = safety.verify_final_model_safety_backend_history_binding(
        record,
        receipt,
        prompt_id="prompt-safe",
    )

    assert binding["ok"] is True
    assert binding["graph_sha256"] == receipt["graph_sha256"]
    assert binding["receipt_sha256"] == receipt["receipt_sha256"]
    assert binding["history_graph_verified"] is True
    assert binding["history_marker_verified"] is True


def test_prompt_marker_rejects_receipt_modified_after_signing(tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "safe.safetensors")
    _submitted, receipt = safety.verify_final_graph_model_safety(
        _checkpoint_graph(),
        models_root=models_root,
        backend_url="http://127.0.0.1:8188",
    )
    receipt["distinct_model_total_bytes"] += 1

    with pytest.raises(safety.FinalModelSafetyError, match="tampered"):
        safety.final_model_safety_prompt_marker(receipt)


@pytest.mark.parametrize("tamper", ["missing_marker", "changed_graph", "changed_receipt"])
def test_backend_history_binding_fails_closed_on_missing_or_changed_proof(tmp_path, tamper):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "safe.safetensors")
    submitted, receipt = safety.verify_final_graph_model_safety(
        _checkpoint_graph(),
        models_root=models_root,
        backend_url="http://127.0.0.1:8188",
    )
    marker = safety.final_model_safety_prompt_marker(receipt)
    extra_data = {safety.FINAL_MODEL_SAFETY_EXTRA_DATA_KEY: marker}
    if tamper == "missing_marker":
        extra_data = {}
    elif tamper == "changed_graph":
        submitted["1"]["inputs"]["ckpt_name"] = "changed.safetensors"
    else:
        marker = dict(marker)
        marker["receipt_sha256"] = "0" * 64
        extra_data = {safety.FINAL_MODEL_SAFETY_EXTRA_DATA_KEY: marker}
    record = {"prompt": [1, "prompt-safe", submitted, extra_data]}

    with pytest.raises(safety.FinalModelSafetyError, match="history prompt"):
        safety.verify_final_model_safety_backend_history_binding(
            record,
            receipt,
            prompt_id="prompt-safe",
        )


def test_campaign_generation_rejects_completed_history_without_receipt_marker(
    monkeypatch,
    tmp_path,
):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "safe.safetensors")
    _enable_campaign(monkeypatch, models_root)

    class MissingMarkerClient(_QueueClient):
        def _json_request(self, path, *, method="GET", payload=None, timeout=None, allow_non_json=False):
            self.calls.append({"path": path, "method": method, "payload": payload})
            if path == "/prompt":
                self.submitted_payload = payload
                return {"prompt_id": "prompt-safe"}
            if path == "/history/prompt-safe":
                return {
                    "prompt-safe": {
                        "prompt": [1, "prompt-safe", self.submitted_payload["prompt"], {}],
                        "status": {"completed": True},
                        "outputs": {
                            "2": {
                                "images": [{
                                    "filename": "safe.png",
                                    "subfolder": "",
                                    "type": "output",
                                }],
                            },
                        },
                    },
                }
            raise AssertionError(path)

    graph = {
        **_checkpoint_graph(),
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    client = MissingMarkerClient()

    with pytest.raises(RuntimeError, match="backend history binding failed"):
        generate_from_workflow(
            client,
            graph,
            fetch_outputs=False,
            error_cls=RuntimeError,
            image_fetcher=lambda _ref: None,
        )


def test_campaign_generation_propagates_exact_backend_history_binding(monkeypatch, tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "safe.safetensors")
    _enable_campaign(monkeypatch, models_root)

    class EchoBindingClient(_QueueClient):
        def _json_request(self, path, *, method="GET", payload=None, timeout=None, allow_non_json=False):
            self.calls.append({"path": path, "method": method, "payload": payload})
            if path == "/prompt":
                self.submitted_payload = payload
                return {"prompt_id": "prompt-safe"}
            if path == "/history/prompt-safe":
                return {
                    "prompt-safe": {
                        "prompt": [
                            1,
                            "prompt-safe",
                            self.submitted_payload["prompt"],
                            self.submitted_payload["extra_data"],
                        ],
                        "status": {"completed": True},
                        "outputs": {
                            "2": {
                                "images": [{
                                    "filename": "safe.png",
                                    "subfolder": "",
                                    "type": "output",
                                }],
                            },
                        },
                    },
                }
            raise AssertionError(path)

    graph = {
        **_checkpoint_graph(),
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    result = generate_from_workflow(
        EchoBindingClient(),
        graph,
        fetch_outputs=False,
        error_cls=RuntimeError,
        image_fetcher=lambda _ref: None,
    )

    binding = result["final_model_safety_backend_binding"]
    assert binding["ok"] is True
    assert binding["prompt_id"] == result["prompt_id"]
    assert binding["graph_sha256"] == result["final_model_safety"]["graph_sha256"]
    assert binding["receipt_sha256"] == result["final_model_safety"]["receipt_sha256"]


@pytest.mark.parametrize(
    "missing_env",
    [safety.CAMPAIGN_COMFYUI_API_URL_ENV, safety.CAMPAIGN_COMFYUI_MODELS_ROOT_ENV],
)
def test_campaign_binding_cannot_be_bypassed_by_removing_one_environment_half(
    monkeypatch,
    tmp_path,
    missing_env,
):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "safe.safetensors")
    _enable_campaign(monkeypatch, models_root)
    monkeypatch.delenv(missing_env)
    client = _QueueClient()

    with pytest.raises(RuntimeError, match="binding is incomplete"):
        queue_prompt_with_client_id(client, _checkpoint_graph(), error_cls=RuntimeError)

    assert client.calls == []


def test_campaign_backend_origin_mismatch_fails_before_prompt(monkeypatch, tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "safe.safetensors")
    _enable_campaign(monkeypatch, models_root, "http://127.0.0.1:8188")
    client = _QueueClient("http://127.0.0.1:8288")

    with pytest.raises(RuntimeError, match="does not match campaign binding"):
        queue_prompt_with_client_id(client, _checkpoint_graph(), error_cls=RuntimeError)

    assert client.calls == []


def test_reserved_prompt_marker_cannot_be_injected_by_caller(monkeypatch, tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "safe.safetensors")
    _enable_campaign(monkeypatch, models_root)
    client = _QueueClient()

    with pytest.raises(RuntimeError, match="reserved extra_data key collision"):
        queue_prompt_with_client_id(
            client,
            _checkpoint_graph(),
            extra_data={safety.FINAL_MODEL_SAFETY_EXTRA_DATA_KEY: {"ok": True}},
            error_cls=RuntimeError,
        )

    assert client.calls == []


@pytest.mark.parametrize("input_value", ["unsafe.safetensors", ["9", 0]])
def test_unknown_loader_dependency_input_cannot_bypass_final_graph_gate(tmp_path, input_value):
    models_root = tmp_path / "models"
    models_root.mkdir()
    graph = {
        "1": {
            "class_type": "FutureModelLoader",
            "inputs": {"model": input_value},
        },
    }

    with pytest.raises(safety.FinalModelSafetyError, match="(dependency contract failed|unmapped loader)"):
        safety.verify_final_graph_model_safety(
            graph,
            models_root=models_root,
            backend_url="http://127.0.0.1:8188",
        )


@pytest.mark.parametrize(
    ("class_type", "input_name", "input_value"),
    [
        ("ModelSamplingSD3", "model_name", "hidden.safetensors"),
        ("LayerMask: LoadBiRefNetModelV2", "model", ["99", 0]),
    ],
)
def test_allowlisted_nonstandard_model_loader_cannot_hide_an_unmapped_dependency(
    tmp_path,
    class_type,
    input_name,
    input_value,
):
    models_root = tmp_path / "models"
    models_root.mkdir()
    graph = {
        "1": {
            "class_type": class_type,
            "inputs": {input_name: input_value},
        },
    }

    with pytest.raises(safety.FinalModelSafetyError, match="unmapped loader dependency"):
        safety.verify_final_graph_model_safety(
            graph,
            models_root=models_root,
            backend_url="http://127.0.0.1:8188",
        )


@pytest.mark.parametrize(
    ("class_type", "dependency_input", "upstream_inputs"),
    [
        ("LoraLoader", "lora_name", {"model": ["9", 0], "clip": ["9", 1]}),
        ("LoraLoaderModelOnly", "lora_name", {"model": ["9", 0]}),
    ],
)
def test_known_mapped_loader_allows_normal_upstream_node_links(
    tmp_path,
    class_type,
    dependency_input,
    upstream_inputs,
):
    models_root = tmp_path / "models"
    _write_model(models_root, "loras", "style.safetensors", b"lora")
    graph = {
        "1": {
            "class_type": class_type,
            "inputs": {
                dependency_input: "style.safetensors",
                **upstream_inputs,
            },
        },
    }

    _submitted, receipt = safety.verify_final_graph_model_safety(
        graph,
        models_root=models_root,
        backend_url="http://127.0.0.1:8188",
    )

    assert receipt["reference_count"] == 1
    assert receipt["references"][0]["relative_path"] == "loras/style.safetensors"


def test_checked_in_official_graphs_have_no_hidden_final_model_dependencies():
    repo_root = Path(__file__).resolve().parents[2]
    failures = {}
    for workflow_id in SYSTEM_WORKFLOW_IDS:
        graph = json.loads(
            (repo_root / "workflows" / "comfyui" / workflow_id / "workflow.json").read_text(
                encoding="utf-8"
            )
        )
        try:
            safety._final_graph_references(graph)
        except safety.FinalModelSafetyError as exc:
            failures[workflow_id] = str(exc)

    # This official graph selects a Qwen3 VQA model by an unmapped literal.
    # It must remain blocked until that external/cache dependency has an exact
    # models-root path, size and hash contract; node-link false positives from
    # known LoRA loaders must not add any other workflow to this list.
    assert failures == {
        "origin_one_click_anime_to_real": (
            "unmapped loader dependency input in final graph: "
            "node 316 Qwen3_VQA.model"
        ),
    }


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../outside.safetensors",
        "/tmp/outside.safetensors",
        r"C:\\models\\outside.safetensors",
        "https://example.invalid/model.safetensors",
        "nested//outside.safetensors",
    ],
)
def test_user_materialized_model_path_cannot_escape_or_use_noncanonical_path(tmp_path, unsafe_name):
    models_root = tmp_path / "models"
    models_root.mkdir()

    with pytest.raises(safety.FinalModelSafetyError, match="(unsafe|dependency contract failed)"):
        safety.verify_final_graph_model_safety(
            _checkpoint_graph(unsafe_name),
            models_root=models_root,
            backend_url="http://127.0.0.1:8188",
        )


@pytest.mark.parametrize("symlink_kind", ["file", "directory", "root"])
def test_symlink_and_cross_root_dependency_paths_fail_closed(tmp_path, symlink_kind):
    real_root = tmp_path / "real-models"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "safe.safetensors").write_bytes(b"outside")
    if symlink_kind == "root":
        real_root.mkdir()
        models_root = tmp_path / "models-link"
        models_root.symlink_to(real_root, target_is_directory=True)
    else:
        models_root = real_root
        models_root.mkdir()
        if symlink_kind == "directory":
            (models_root / "checkpoints").symlink_to(outside, target_is_directory=True)
        else:
            (models_root / "checkpoints").mkdir()
            (models_root / "checkpoints" / "safe.safetensors").symlink_to(
                outside / "safe.safetensors"
            )

    with pytest.raises(safety.FinalModelSafetyError, match="(symlink|canonical)"):
        safety.verify_final_graph_model_safety(
            _checkpoint_graph(),
            models_root=models_root,
            backend_url="http://127.0.0.1:8188",
        )


def test_per_file_two_gib_cap_runs_before_hash(tmp_path, monkeypatch):
    models_root = tmp_path / "models"
    model_path = _write_model(models_root, "checkpoints", "huge.safetensors", b"")
    with model_path.open("r+b") as handle:
        handle.truncate(safety.FINAL_MODEL_MAX_FILE_BYTES + 1)
    monkeypatch.setattr(
        safety,
        "_hash_exact_regular_file",
        lambda *_args, **_kwargs: pytest.fail("oversized file must be rejected before hashing"),
    )

    with pytest.raises(safety.FinalModelSafetyError, match="model file exceeds"):
        safety.verify_final_graph_model_safety(
            _checkpoint_graph("huge.safetensors"),
            models_root=models_root,
            backend_url="http://127.0.0.1:8188",
        )


def test_empty_model_file_fails_before_hash(tmp_path, monkeypatch):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "empty.safetensors", b"")
    monkeypatch.setattr(
        safety,
        "_hash_exact_regular_file",
        lambda *_args, **_kwargs: pytest.fail("empty file must be rejected before hashing"),
    )

    with pytest.raises(safety.FinalModelSafetyError, match="model file is empty"):
        safety.verify_final_graph_model_safety(
            _checkpoint_graph("empty.safetensors"),
            models_root=models_root,
            backend_url="http://127.0.0.1:8188",
        )


def test_dynamic_multi_compare_four_gib_total_cannot_bypass_before_hash(tmp_path, monkeypatch):
    models_root = tmp_path / "models"
    names = [f"branch-{index}.safetensors" for index in range(3)]
    for name in names:
        path = _write_model(models_root, "checkpoints", name, b"")
        with path.open("r+b") as handle:
            handle.truncate(1536 * 1024 * 1024)
    graph = {
        str(index): {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": name},
        }
        for index, name in enumerate(names, start=1)
    }
    monkeypatch.setattr(
        safety,
        "_hash_exact_regular_file",
        lambda *_args, **_kwargs: pytest.fail("over-total graph must be rejected before hashing"),
    )

    with pytest.raises(safety.FinalModelSafetyError, match="distinct model total exceeds"):
        safety.verify_final_graph_model_safety(
            graph,
            models_root=models_root,
            backend_url="http://127.0.0.1:8188",
        )


def test_ambiguous_unet_path_fails_closed(tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "diffusion_models", "same.gguf")
    _write_model(models_root, "unet", "same.gguf")
    graph = {
        "1": {
            "class_type": "UnetLoaderGGUF",
            "inputs": {"unet_name": "same.gguf"},
        },
    }

    with pytest.raises(safety.FinalModelSafetyError, match="ambiguous dependency path"):
        safety.verify_final_graph_model_safety(
            graph,
            models_root=models_root,
            backend_url="http://127.0.0.1:8188",
        )


def test_final_gguf_profile_graph_binds_all_materialized_companion_files(tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "unet", "profile.gguf", b"gguf")
    _write_model(models_root, "text_encoders", "clip-one.safetensors", b"clip-one")
    _write_model(models_root, "text_encoders", "clip-two.safetensors", b"clip-two")
    _write_model(models_root, "vae", "profile-vae.safetensors", b"vae")
    graph = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "profile.gguf"}},
        "2": {
            "class_type": "DualCLIPLoader",
            "inputs": {
                "clip_name1": "clip-one.safetensors",
                "clip_name2": "clip-two.safetensors",
                "type": "sdxl",
            },
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "profile-vae.safetensors"}},
    }

    _submitted, receipt = safety.verify_final_graph_model_safety(
        graph,
        models_root=models_root,
        backend_url="http://127.0.0.1:8188",
    )

    assert receipt["distinct_model_file_count"] == 4
    assert {row["relative_path"] for row in receipt["model_files"]} == {
        "unet/profile.gguf",
        "text_encoders/clip-one.safetensors",
        "text_encoders/clip-two.safetensors",
        "vae/profile-vae.safetensors",
    }


def test_lora_controlnet_and_prompt_embedding_are_all_exact_dependencies(tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "loras", "style.safetensors", b"lora")
    _write_model(models_root, "controlnet", "pose.safetensors", b"control")
    _write_model(models_root, "embeddings", "negative.pt", b"embedding")
    graph = {
        "1": {"class_type": "LoraLoader", "inputs": {"lora_name": "style.safetensors"}},
        "2": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "pose.safetensors"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "embedding:negative.pt"}},
    }

    _submitted, receipt = safety.verify_final_graph_model_safety(
        graph,
        models_root=models_root,
        backend_url="http://127.0.0.1:8188",
    )

    assert {row["kind"] for row in receipt["references"]} == {
        "lora", "controlnet", "embedding"
    }
    assert {row["relative_path"] for row in receipt["model_files"]} == {
        "loras/style.safetensors",
        "controlnet/pose.safetensors",
        "embeddings/negative.pt",
    }


def test_user_override_missing_final_model_fails_even_if_template_model_exists(monkeypatch, tmp_path):
    models_root = tmp_path / "models"
    _write_model(models_root, "checkpoints", "template-safe.safetensors")
    _enable_campaign(monkeypatch, models_root)
    client = _QueueClient()
    final_graph = _checkpoint_graph("user-overridden-missing.safetensors")

    with pytest.raises(RuntimeError, match="cannot resolve exact dependency path"):
        queue_prompt_with_client_id(client, final_graph, error_cls=RuntimeError)

    assert client.calls == []


def test_dependency_mutation_during_hash_fails_before_prompt(monkeypatch, tmp_path):
    models_root = tmp_path / "models"
    model_path = _write_model(models_root, "checkpoints", "safe.safetensors", b"before")
    _enable_campaign(monkeypatch, models_root)
    client = _QueueClient()
    original_hash = safety._hash_exact_regular_file

    def mutate_then_hash(path, expected, *, models_root):
        model_path.write_bytes(b"after-change")
        return original_hash(path, expected, models_root=models_root)

    monkeypatch.setattr(safety, "_hash_exact_regular_file", mutate_then_hash)

    with pytest.raises(RuntimeError, match="changed before hashing"):
        queue_prompt_with_client_id(client, _checkpoint_graph(), error_cls=RuntimeError)

    assert client.calls == []


def test_earlier_dependency_mutation_while_later_file_hashes_fails_final_snapshot(
    monkeypatch,
    tmp_path,
):
    models_root = tmp_path / "models"
    first = _write_model(models_root, "checkpoints", "a.safetensors", b"ORIGINAL-A")
    _write_model(models_root, "checkpoints", "b.safetensors", b"ORIGINAL-B")
    graph = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "a.safetensors"},
        },
        "2": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "b.safetensors"},
        },
    }
    original_hash = safety._hash_exact_regular_file
    hash_calls = []

    def mutate_first_while_hashing_second(path, expected, *, models_root):
        if hash_calls:
            first.write_bytes(b"MUTATED-A!")  # same byte length, different contents
        result = original_hash(path, expected, models_root=models_root)
        hash_calls.append(path.name)
        return result

    monkeypatch.setattr(safety, "_hash_exact_regular_file", mutate_first_while_hashing_second)

    with pytest.raises(
        safety.FinalModelSafetyError,
        match="(file|content) changed after complete graph hashing",
    ):
        safety.verify_final_graph_model_safety(
            graph,
            models_root=models_root,
            backend_url="http://127.0.0.1:8188",
        )


def test_terminal_receipt_file_revalidation_rejects_post_queue_mutation(tmp_path):
    models_root = tmp_path / "models"
    model_path = _write_model(
        models_root,
        "checkpoints",
        "safe.safetensors",
        b"ORIGINAL-MODEL",
    )
    _submitted, receipt = safety.verify_final_graph_model_safety(
        _checkpoint_graph(),
        models_root=models_root,
        backend_url="http://127.0.0.1:8188",
    )

    model_path.write_bytes(b"MUTATED!-MODEL")  # same byte length

    with pytest.raises(
        safety.FinalModelSafetyError,
        match="(file|content hash) changed after receipt creation",
    ):
        safety.revalidate_final_model_safety_receipt_files(
            receipt,
            models_root=models_root,
        )


def test_intermediate_directory_symlink_swap_cannot_reuse_same_model_inode(
    monkeypatch,
    tmp_path,
):
    models_root = tmp_path / "models"
    model_path = _write_model(models_root, "checkpoints", "safe.safetensors", b"same-inode")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.link(model_path, outside / model_path.name)
    _enable_campaign(monkeypatch, models_root)
    client = _QueueClient()
    original_hash = safety._hash_exact_regular_file

    def swap_directory_then_hash(path, expected, *, models_root):
        checkpoint_dir = models_root / "checkpoints"
        checkpoint_dir.rename(models_root / "checkpoints-original")
        checkpoint_dir.symlink_to(outside, target_is_directory=True)
        return original_hash(path, expected, models_root=models_root)

    monkeypatch.setattr(safety, "_hash_exact_regular_file", swap_directory_then_hash)

    with pytest.raises(RuntimeError, match="symlink"):
        queue_prompt_with_client_id(client, _checkpoint_graph(), error_cls=RuntimeError)

    assert client.calls == []
