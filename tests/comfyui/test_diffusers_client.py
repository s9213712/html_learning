import io
import importlib.util
import logging
import os
import sys
import types
import warnings

import pytest

from services.comfyui.client import ComfyUIError
from services.comfyui.diffusers_client import DiffusersClient, diffusers_backend_url, repo_id_from_diffusers_url
from services.comfyui.huggingface import build_diffusers_variant_options, detect_diffusers_supported_modes
from services.comfyui.huggingface import inspect_huggingface_diffusers_repo
from services.comfyui.huggingface import parse_diffusers_model_card_hints
from services.comfyui import huggingface as huggingface_service
from services.comfyui.settings import normalize_huggingface_repo_id


def test_diffusers_backend_url_round_trips_repo_id():
    url = diffusers_backend_url("dhead/waiIllustriousSDXL_v150")

    assert url == "diffusers://local/dhead%2FwaiIllustriousSDXL_v150"
    assert repo_id_from_diffusers_url(url) == "dhead/waiIllustriousSDXL_v150"


def test_huggingface_repo_normalizer_accepts_repo_id_and_model_page_url():
    assert normalize_huggingface_repo_id("dhead/waiIllustriousSDXL_v150") == "dhead/waiIllustriousSDXL_v150"
    assert (
        normalize_huggingface_repo_id("https://huggingface.co/dhead/waiIllustriousSDXL_v150/tree/main")
        == "dhead/waiIllustriousSDXL_v150"
    )
    assert normalize_huggingface_repo_id("https://example.com/dhead/waiIllustriousSDXL_v150") is None


def test_huggingface_repo_normalizer_rejects_model_file_names():
    assert normalize_huggingface_repo_id("xxx.safetensors") is None
    assert normalize_huggingface_repo_id("owner/model.safetensors") is None
    assert normalize_huggingface_repo_id("owner/model.gguf") is None
    assert (
        normalize_huggingface_repo_id("https://huggingface.co/owner/model-repo/blob/main/model.safetensors")
        == "owner/model-repo"
    )


def test_huggingface_diffusers_metadata_groups_precision_variants_by_size():
    options = build_diffusers_variant_options([
        {"rfilename": "unet/diffusion_pytorch_model.safetensors", "size": 1000},
        {"rfilename": "vae/diffusion_pytorch_model.safetensors", "size": 200},
        {"rfilename": "unet/diffusion_pytorch_model.fp16.safetensors", "size": 520},
        {"rfilename": "vae/diffusion_pytorch_model.fp16.safetensors", "size": 110},
    ])

    assert [item["value"] for item in options] == ["__default__", "fp16"]
    assert options[0]["size_bytes"] == 1200
    assert options[1]["size_bytes"] == 630


def test_huggingface_diffusers_metadata_ignores_root_checkpoints_when_components_exist():
    options = build_diffusers_variant_options([
        {"rfilename": "animagine-xl-4.0.safetensors", "size": 7_000},
        {"rfilename": "animagine-xl-4.0-opt.safetensors", "size": 7_000},
        {"rfilename": "text_encoder/model.safetensors", "size": 100},
        {"rfilename": "text_encoder_2/model.safetensors", "size": 200},
        {"rfilename": "unet/diffusion_pytorch_model.safetensors", "size": 1000},
        {"rfilename": "vae/diffusion_pytorch_model.safetensors", "size": 50},
    ])

    assert len(options) == 1
    assert options[0]["value"] == "__default__"
    assert options[0]["size_bytes"] == 1350
    assert "animagine-xl-4.0.safetensors" not in options[0]["files"]


def test_huggingface_diffusers_metadata_lists_gguf_files_as_selectable_options():
    options = build_diffusers_variant_options([
        {"rfilename": "WAI-illustrious-SDXL-v140-Q8_0.gguf", "size": 3_200_000_000},
        {"rfilename": "WAI-illustrious-SDXL-v140-Q5_K_M.gguf", "size": 2_100_000_000},
    ])

    assert [item["kind"] for item in options] == ["gguf", "gguf"]
    assert options[0]["value"].startswith("gguf::")
    assert options[0]["gguf_file"].endswith(".gguf")
    assert options[0]["requires_base_repo"] is True


def test_huggingface_diffusers_metadata_detects_non_image_pipeline_capability():
    assert detect_diffusers_supported_modes(
        repo_id="owner/video-model",
        pipeline_tag="text-to-video",
        library_name="diffusers",
        tags=["diffusers"],
        siblings=[{"rfilename": "model_index.json"}],
    ) == ["t2v"]


def test_huggingface_diffusers_metadata_detects_text_to_text_pipeline_capability():
    assert detect_diffusers_supported_modes(
        repo_id="owner/text-model",
        pipeline_tag="text2text-generation",
        library_name="transformers",
        tags=["text2text-generation"],
        siblings=[{"rfilename": "config.json"}],
    ) == ["t2t"]


def test_huggingface_diffusers_metadata_detects_image_to_text_pipeline_capability():
    assert detect_diffusers_supported_modes(
        repo_id="owner/vision-language-model",
        pipeline_tag="image-to-text",
        library_name="transformers",
        tags=["image-to-text"],
        siblings=[{"rfilename": "config.json"}],
    ) == ["i2t"]


def test_huggingface_diffusers_metadata_detects_image_text_to_text_pipeline_capability():
    assert detect_diffusers_supported_modes(
        repo_id="Qwen/Qwen2.5-VL-3B-Instruct",
        pipeline_tag="image-text-to-text",
        library_name="transformers",
        tags=["image-text-to-text", "multimodal"],
        siblings=[{"rfilename": "config.json"}],
    ) == ["i2t"]


def test_huggingface_diffusers_metadata_accumulates_multiple_pipeline_capabilities():
    assert detect_diffusers_supported_modes(
        repo_id="owner/multi-capability-model",
        pipeline_tag="text-to-image",
        library_name="diffusers",
        tags=["diffusers", "image-to-image", "text2text-generation", "image-to-text"],
        siblings=[{"rfilename": "model_index.json"}],
    ) == ["txt2img", "img2img", "t2t", "i2t"]


def test_huggingface_diffusers_metadata_detects_gguf_text_to_image_only():
    assert detect_diffusers_supported_modes(
        repo_id="sothmik/Wai-NSFW-Illustrious-v140-Q8-GGUF",
        pipeline_tag="text-to-image",
        library_name="gguf",
        tags=["GGUF"],
        siblings=[{"rfilename": "WAI-NSFW-Illustrious-v140-Q8_0.gguf"}],
    ) == ["txt2img"]


def test_huggingface_diffusers_metadata_requires_diffusers_marker_for_regular_repos():
    assert detect_diffusers_supported_modes(
        repo_id="owner/plain-safetensors",
        pipeline_tag="text-to-image",
        library_name="safetensors",
        tags=["text-to-image"],
        siblings=[{"rfilename": "model.safetensors"}],
    ) == []
    assert detect_diffusers_supported_modes(
        repo_id="owner/diffusers-model",
        pipeline_tag="text-to-image",
        library_name="diffusers",
        tags=["text-to-image", "diffusers"],
        siblings=[{"rfilename": "model_index.json"}],
    ) == ["txt2img"]


def test_huggingface_diffusers_metadata_does_not_imply_img2img_only_from_txt2img_tag():
    assert detect_diffusers_supported_modes(
        repo_id="owner/diffusers-only-txt2img",
        pipeline_tag="text-to-image",
        library_name="diffusers",
        tags=["text-to-image", "diffusers"],
        siblings=[{"rfilename": "model_index.json"}],
    ) == ["txt2img"]


def test_huggingface_diffusers_metadata_accepts_modular_model_index():
    assert detect_diffusers_supported_modes(
        repo_id="owner/modular-diffusers-model",
        pipeline_tag="text-to-image",
        library_name="diffusers",
        tags=["text-to-image", "diffusers"],
        siblings=[{"rfilename": "modular_model_index.json"}],
    ) == ["txt2img"]


def test_huggingface_model_card_hints_parse_official_diffusers_snippet():
    hints = parse_diffusers_model_card_hints(
        '''
        import torch
        from diffusers import DiffusionPipeline
        pipe = DiffusionPipeline.from_pretrained(
            "circlestone-labs/Anima-Base-v1.0-Diffusers",
            dtype=torch.bfloat16,
            device_map="cuda",
        )
        ''',
        "circlestone-labs/Anima-Base-v1.0-Diffusers",
    )

    assert hints["pipeline_loader"] == "diffusion"
    assert hints["dtype"] == "bfloat16"
    assert hints["dtype_kwarg"] == "dtype"
    assert hints["device_map"] == "cuda"


def test_huggingface_diffusers_repo_inspection_uses_short_metadata_cache(monkeypatch):
    huggingface_service._HF_REPO_INSPECT_CACHE.clear()
    calls = {"count": 0}

    class FakeApi:
        def model_info(self, repo_id, token=None, files_metadata=True):
            calls["count"] += 1
            return types.SimpleNamespace(
                siblings=[{"rfilename": "model_index.json"}],
                pipeline_tag="text-to-image",
                library_name="diffusers",
                tags=["diffusers"],
                cardData={},
                config={},
            )

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "huggingface_hub" else None)
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(HfApi=lambda: FakeApi()))

    first = inspect_huggingface_diffusers_repo("owner/model", mode="txt2img")
    second = inspect_huggingface_diffusers_repo("owner/model", mode="txt2img")

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["cache"]["hit"] is True
    assert calls["count"] == 1


def test_huggingface_diffusers_repo_inspection_separates_supported_and_runnable_modes(monkeypatch):
    huggingface_service._HF_REPO_INSPECT_CACHE.clear()

    class FakeApi:
        def model_info(self, repo_id, token=None, files_metadata=True):
            return types.SimpleNamespace(
                siblings=[{"rfilename": "config.json"}],
                pipeline_tag="image-to-text",
                library_name="transformers",
                tags=["image-to-text"],
                cardData={},
                config={},
            )

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "huggingface_hub" else None)
    monkeypatch.setattr(huggingface_service, "_load_model_card_hints", lambda repo_id, token: {})
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(HfApi=lambda: FakeApi()))

    result = inspect_huggingface_diffusers_repo("owner/vision-language-model", mode="img2img")

    assert result["ok"] is True
    assert result["supported_modes"] == ["i2t"]
    assert result["runnable_modes"] == []
    assert result["supported_for_mode"] is False
    assert any("目前本站 HF / Diffusers 生圖器只支援" in item for item in result["warnings"])


def test_huggingface_diffusers_repo_inspection_keeps_multi_mode_supported_and_runnable(monkeypatch):
    huggingface_service._HF_REPO_INSPECT_CACHE.clear()

    class FakeApi:
        def model_info(self, repo_id, token=None, files_metadata=True):
            return types.SimpleNamespace(
                siblings=[{"rfilename": "model_index.json"}],
                pipeline_tag="text-to-image",
                library_name="diffusers",
                tags=["diffusers", "image-to-image", "text2text-generation", "image-to-text"],
                cardData={},
                config={},
            )

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "huggingface_hub" else None)
    monkeypatch.setattr(huggingface_service, "_load_model_card_hints", lambda repo_id, token: {})
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(HfApi=lambda: FakeApi()))

    result = inspect_huggingface_diffusers_repo("owner/multi-capability-model", mode="img2img")

    assert result["ok"] is True
    assert result["supported_modes"] == ["txt2img", "img2img", "t2t", "i2t"]
    assert result["runnable_modes"] == ["txt2img", "img2img"]
    assert result["supported_for_mode"] is True
    assert any("t2t, i2t" in item for item in result["warnings"])


def test_huggingface_diffusers_repo_inspection_returns_model_card_hints(monkeypatch, tmp_path):
    huggingface_service._HF_REPO_INSPECT_CACHE.clear()
    readme = tmp_path / "README.md"
    readme.write_text(
        '''
        from diffusers import DiffusionPipeline
        pipe = DiffusionPipeline.from_pretrained(
            "circlestone-labs/Anima-Base-v1.0-Diffusers",
            dtype=torch.bfloat16,
            device_map="cuda",
        )
        ''',
        encoding="utf-8",
    )

    class FakeApi:
        def model_info(self, repo_id, token=None, files_metadata=True):
            return types.SimpleNamespace(
                siblings=[
                    {"rfilename": "modular_model_index.json"},
                    {"rfilename": "transformer/diffusion_pytorch_model.safetensors", "size": 1000},
                    {"rfilename": "vae/diffusion_pytorch_model.safetensors", "size": 200},
                ],
                pipeline_tag="text-to-image",
                library_name="diffusers",
                tags=["diffusers"],
                cardData={},
                config={},
            )

    fake_hf = types.ModuleType("huggingface_hub")
    fake_hf.HfApi = lambda: FakeApi()
    fake_hf.hf_hub_download = lambda **kwargs: str(readme)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "huggingface_hub" else None)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    result = inspect_huggingface_diffusers_repo("circlestone-labs/Anima-Base-v1.0-Diffusers", mode="txt2img")

    assert result["ok"] is True
    assert result["supported_for_mode"] is False
    assert result["requires_modular_pipeline"] is True
    assert result["model_card_hints"]["dtype"] == "bfloat16"
    assert result["model_card_hints"]["dtype_kwarg"] == "dtype"
    assert result["model_card_hints"]["device_map"] == "cuda"


def test_huggingface_diffusers_inspect_uses_saved_token_for_metadata_and_card(tmp_path, monkeypatch):
    huggingface_service._HF_REPO_INSPECT_CACHE.clear()
    readme = tmp_path / "README.md"
    readme.write_text(
        'from diffusers import DiffusionPipeline\n'
        'pipe = DiffusionPipeline.from_pretrained("owner/tokened-model", torch_dtype=torch.float16)\n',
        encoding="utf-8",
    )
    captured = {}

    class FakeApi:
        def model_info(self, repo_id, token=None, files_metadata=True):
            captured["model_info"] = {
                "repo_id": repo_id,
                "token": token,
                "files_metadata": files_metadata,
            }
            return types.SimpleNamespace(
                siblings=[
                    {"rfilename": "model_index.json"},
                    {"rfilename": "unet/diffusion_pytorch_model.safetensors", "size": 1000},
                ],
                pipeline_tag="text-to-image",
                library_name="diffusers",
                tags=["diffusers"],
                cardData={},
                config={},
            )

    def fake_hf_hub_download(**kwargs):
        captured["hf_hub_download"] = kwargs
        return str(readme)

    fake_hf = types.ModuleType("huggingface_hub")
    fake_hf.HfApi = lambda: FakeApi()
    fake_hf.hf_hub_download = fake_hf_hub_download
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "huggingface_hub" else None)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    result = inspect_huggingface_diffusers_repo("owner/tokened-model", token="hf_saved_token", mode="txt2img")

    assert result["ok"] is True
    assert captured["model_info"]["token"] == "hf_saved_token"
    assert captured["model_info"]["files_metadata"] is True
    assert captured["hf_hub_download"]["token"] == "hf_saved_token"


def test_diffusers_health_allows_blank_default_repo_for_generation_page_override(tmp_path, monkeypatch):
    client = DiffusersClient(storage_root=tmp_path)

    monkeypatch.setattr(client, "_missing_dependency_names", lambda: [])

    health = client.health_check()

    assert health["ok"] is True
    assert health["model_repo"] == ""


def test_diffusers_client_requires_effective_model_repo_before_dependency_check(tmp_path, monkeypatch):
    monkeypatch.setenv("HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS", "1")
    client = DiffusersClient(storage_root=tmp_path)

    with pytest.raises(ComfyUIError) as exc:
        client.generate_image({"generation_mode": "txt2img", "prompt": "test"})

    assert "尚未設定 Hugging Face model repo" in str(exc.value)


def test_diffusers_client_allows_in_process_when_root_setting_confirms_risk(tmp_path, monkeypatch):
    monkeypatch.delenv("HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS", raising=False)
    client = DiffusersClient.from_settings(
        {"comfyui_allow_in_process_diffusers": True},
        storage_root=tmp_path,
    )

    with pytest.raises(ComfyUIError) as exc:
        client.generate_image({"generation_mode": "txt2img", "prompt": "test"})

    assert "尚未設定 Hugging Face model repo" in str(exc.value)


def test_diffusers_generate_reports_download_preparation_before_heavy_loading(tmp_path, monkeypatch):
    monkeypatch.setenv("HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS", "1")
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    events = []
    monkeypatch.setattr(client, "_resolve_diffusers_variant", lambda *args, **kwargs: "")

    def stop_before_loading(*args, **kwargs):
        logging.getLogger("diffusers").info("loading owner/model with hf_1234567890abcdef")
        raise ComfyUIError("stop before loading")

    monkeypatch.setattr(client, "_load_pipeline", stop_before_loading)

    with pytest.raises(ComfyUIError) as exc:
        client.generate_image({"generation_mode": "txt2img", "prompt": "test"}, progress_callback=events.append)

    assert "stop before loading" in str(exc.value)
    prep_event = next(event for event in events if event.get("step") == "準備 Diffusers model")
    assert prep_event["phase"] == "downloading"
    assert prep_event["percent"] == 3
    assert prep_event["backend_kind"] == "diffusers"
    assert "下載 Diffusers model：owner/model" in prep_event["detail"]
    assert any(
        "Diffusers Python runtime starting: repo=owner/model" in line
        for event in events
        for line in event.get("python_log_tail", [])
    )
    assert any("loading owner/model" in line for event in events for line in event.get("python_log_tail", []))
    assert not any("hf_1234567890abcdef" in line for event in events for line in event.get("python_log_tail", []))
    assert any(event.get("phase") == "error" and "stop before loading" in event.get("detail", "") for event in events)
    assert any(event.get("error_message") == "stop before loading" for event in events)


def test_diffusers_generate_preserves_unsupported_gguf_error_step(tmp_path, monkeypatch):
    monkeypatch.setenv("HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS", "1")
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    events = []

    def fail_with_unsupported_gguf(*args, **kwargs):
        raise ComfyUIError("model.gguf 是 ComfyUI-GGUF 原生 UNet GGUF，請使用 Unet Loader (GGUF)")

    monkeypatch.setattr(client, "_load_pipeline", fail_with_unsupported_gguf)

    with pytest.raises(ComfyUIError):
        client.generate_image(
            {
                "generation_mode": "txt2img",
                "prompt": "test",
                "diffusers_gguf_file": "model.gguf",
                "diffusers_model_variant": "gguf::model.gguf",
                "diffusers_gguf_base_repo": "base/model",
            },
            progress_callback=events.append,
        )

    assert events[-1]["phase"] == "error"
    assert events[-1]["step"] == "GGUF 格式不支援 Diffusers backend"
    assert "ComfyUI-GGUF 原生 UNet GGUF" in events[-1]["error_message"]


def test_diffusers_generate_streams_python_runtime_output_to_progress_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS", "1")
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    events = []
    monkeypatch.setattr(client, "_resolve_diffusers_variant", lambda *args, **kwargs: "")

    def stop_after_runtime_logs(*args, **kwargs):
        print("model_index.json: 100% 712/712 [00:00<00:00, 78.9kB/s]")
        sys.stderr.write("\x1b[AFetching 18 files: 100% 18/18 [01:13<00:00, 5.04s/it]\n")
        warnings.warn("The secret `HF_TOKEN` does not exist in your Colab secrets.", UserWarning)
        logging.getLogger("huggingface_hub.utils._http").warning(
            "Warning: unauthenticated hf_1234567890abcdef requests"
        )
        raise ComfyUIError("stop after runtime logs")

    monkeypatch.setattr(client, "_load_pipeline", stop_after_runtime_logs)

    with pytest.warns(UserWarning, match="HF_TOKEN"), pytest.raises(ComfyUIError):
        client.generate_image({"generation_mode": "txt2img", "prompt": "test"}, progress_callback=events.append)

    lines = [line for event in events for line in event.get("python_log_tail", [])]
    joined = "\n".join(lines)
    assert "model_index.json: 100%" in joined
    assert "Fetching 18 files: 100%" in joined
    assert "HF_TOKEN" in joined
    assert "huggingface_hub.utils._http" in joined
    assert "\x1b[" not in joined
    assert "hf_1234567890abcdef" not in joined
    assert "hf_***" in joined


def test_diffusers_generate_cleans_transient_downloads_after_output_save(tmp_path, monkeypatch):
    monkeypatch.setenv("HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS", "1")
    client = DiffusersClient(
        model_repo="owner/model",
        storage_root=tmp_path,
        keep_downloaded_models=False,
    )
    events = []
    transient_paths = []
    monkeypatch.setattr(client, "_resolve_diffusers_variant", lambda *args, **kwargs: "")

    class FakeGenerator:
        def __init__(self, device=None):
            self.device = device

        def manual_seed(self, seed):
            return self

    class FakeInferenceMode:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeTorch:
        Generator = FakeGenerator

        @staticmethod
        def inference_mode():
            return FakeInferenceMode()

    class FakeImage:
        def save(self, buffer, format="PNG"):
            buffer.write(b"fake-png")

    class FakePipe:
        def __call__(self, **kwargs):
            return type("Output", (), {"images": [FakeImage()]})()

    def fake_load_pipeline(*args, **kwargs):
        transient_dir = client._new_transient_download_dir("unit_test")
        (transient_dir / "model.bin").write_text("downloaded", encoding="utf-8")
        transient_paths.append(transient_dir)
        kwargs["log_capture"].register_transient_path(transient_dir)
        return FakePipe(), FakeTorch, "cpu"

    monkeypatch.setattr(client, "_load_pipeline", fake_load_pipeline)

    result = client.generate_image(
        {"generation_mode": "txt2img", "prompt": "test", "width": 64, "height": 64, "steps": 1},
        progress_callback=events.append,
    )

    assert result["images"]
    assert transient_paths
    assert not transient_paths[0].exists()
    assert any(
        "Removing transient Diffusers download cache" in line
        for event in events
        for line in event.get("python_log_tail", [])
    )


def test_diffusers_device_map_setting_is_root_controlled(tmp_path, monkeypatch):
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path, device_map="disabled")
    monkeypatch.setattr(client, "_accelerate_available", lambda: True)

    assert client._resolve_device_map("cuda") == ""

    client.device_map_setting = "auto"
    assert client._resolve_device_map("cuda") == "cuda"
    assert client._resolve_device_map("cuda", cuda_memory={"cuda_total_bytes": 4 * 1024 * 1024 * 1024}) == "balanced"
    assert client._resolve_device_map("cpu") == ""

    client.device_map_setting = "balanced_low_0"
    assert client._resolve_device_map("cuda") == "balanced_low_0"

    monkeypatch.setattr(client, "_accelerate_available", lambda: False)
    assert client._resolve_device_map("cuda") == ""


def test_diffusers_cuda_fallback_to_cpu_is_root_controlled(tmp_path):
    client = DiffusersClient.from_settings(
        {"comfyui_diffusers_cuda_fallback_to_cpu": False},
        storage_root=tmp_path,
    )
    assert client.cuda_fallback_to_cpu is False
    assert client._should_fallback_to_cpu_for_low_vram("cuda", {"cuda_total_bytes": 4 * 1024 * 1024 * 1024}) is False

    client = DiffusersClient.from_settings(
        {"comfyui_diffusers_cuda_fallback_to_cpu": True, "comfyui_diffusers_device": "auto"},
        storage_root=tmp_path,
    )
    assert client._should_fallback_to_cpu_for_low_vram("cuda", {"cuda_total_bytes": 4 * 1024 * 1024 * 1024}) is True
    client.device_setting = "cuda"
    assert client._should_fallback_to_cpu_for_low_vram("cuda", {"cuda_total_bytes": 4 * 1024 * 1024 * 1024}) is False
    assert client._should_fallback_to_cpu_for_low_vram("cpu", {"cuda_total_bytes": 4 * 1024 * 1024 * 1024}) is False


def test_diffusers_hf_xet_download_backend_is_root_controlled(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    client = DiffusersClient.from_settings(
        {"comfyui_diffusers_disable_xet": True},
        storage_root=tmp_path,
    )

    backend = client._configure_huggingface_download_backend()

    assert client.disable_xet is True
    assert backend["xet_disabled"] is True
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"

    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    client = DiffusersClient.from_settings(
        {"comfyui_diffusers_disable_xet": False},
        storage_root=tmp_path,
    )

    backend = client._configure_huggingface_download_backend()

    assert client.disable_xet is False
    assert backend["xet_disabled"] is False
    assert "HF_HUB_DISABLE_XET" not in os.environ


def test_diffusers_cuda_memory_warning_is_streamed_for_small_vram(tmp_path):
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    events = []

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def get_device_name(index):
            return "Small CUDA"

        @staticmethod
        def mem_get_info():
            return 2 * 1024 * 1024 * 1024, 4 * 1024 * 1024 * 1024

    class FakeTorch:
        cuda = FakeCuda

    client._log_cuda_memory(FakeTorch, log_capture=None, progress_callback=events.append)

    assert events
    assert events[-1]["cuda_device_name"] == "Small CUDA"
    assert events[-1]["cuda_total_bytes"] == 4 * 1024 * 1024 * 1024
    assert "VRAM 可用 2.0 GB / 4.0 GB" in events[-1]["detail"]


def test_diffusers_snapshot_reports_cache_hit_when_no_download_bytes(tmp_path, monkeypatch):
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    events = []

    class FakeTqdm:
        def __init__(self, *args, **kwargs):
            self.iterable = kwargs.get("iterable")

        def update(self, n=1):
            return None

        def close(self):
            return None

    def fake_snapshot_download(**kwargs):
        tqdm_class = kwargs.get("tqdm_class")
        assert tqdm_class is not None
        bar = tqdm_class(total=18, desc="Fetching 18 files", unit="it")
        bar.update(18)
        bar.close()
        return str(tmp_path / "hf-cache" / "owner-model")

    fake_hf = types.ModuleType("huggingface_hub")
    fake_hf.snapshot_download = fake_snapshot_download
    fake_utils = types.ModuleType("huggingface_hub.utils")
    fake_utils.tqdm = FakeTqdm
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", fake_utils)

    snapshot_path = client._prefetch_diffusers_snapshot(
        "owner/model",
        progress_callback=events.append,
        log_capture=None,
    )

    assert snapshot_path.endswith("owner-model")
    loading = events[-1]
    assert loading["phase"] == "loading"
    assert loading["cache_hit"] is True
    assert loading["bytes_written"] == 0
    assert "cache hit" in loading["detail"]
    assert "未偵測到網路下載位元組" in loading["detail"]


def test_diffusers_prefetch_snapshot_uses_only_selected_precision_patterns(tmp_path, monkeypatch):
    cache_root = tmp_path / "hf-cache"
    client = DiffusersClient(
        model_repo="owner/model",
        storage_root=tmp_path,
        huggingface_cache_root=str(cache_root),
    )
    events = []
    calls = []

    class FakeTqdm:
        def __init__(self, *args, **kwargs):
            self.iterable = kwargs.get("iterable")

        def update(self, n=1):
            return None

        def close(self):
            return None

    def fake_snapshot_download(**kwargs):
        calls.append(kwargs)
        return str(cache_root / "hub" / "models--owner--model" / "snapshots" / "abc")

    fake_hf = types.ModuleType("huggingface_hub")
    fake_hf.snapshot_download = fake_snapshot_download
    fake_utils = types.ModuleType("huggingface_hub.utils")
    fake_utils.tqdm = FakeTqdm
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", fake_utils)

    snapshot_path = client._prefetch_diffusers_snapshot(
        "owner/model",
        variant="fp16",
        progress_callback=events.append,
        log_capture=None,
    )

    assert snapshot_path.endswith("snapshots/abc")
    assert calls
    allow_patterns = calls[0]["allow_patterns"]
    assert "unet/*.fp16.safetensors" in allow_patterns
    assert "text_encoder/**/*.fp16.bin" in allow_patterns
    assert "*.fp16.safetensors" not in allow_patterns
    assert "*.safetensors" not in allow_patterns
    assert calls[0].get("ignore_patterns") is None
    assert os.environ["HF_HOME"] == str(cache_root)
    assert os.environ["HF_HUB_CACHE"] == str(cache_root / "hub")
    assert os.environ["HUGGINGFACE_HUB_CACHE"] == str(cache_root / "hub")
    assert events[-1]["phase"] == "loading"


def test_diffusers_client_upload_fetch_and_discard_round_trip(tmp_path):
    client = DiffusersClient(model_repo="dhead/waiIllustriousSDXL_v150", storage_root=tmp_path)

    image_ref = client.upload_image_bytes(b"png-bytes", "source.png", image_type="input")
    image = client.fetch_image(image_ref)
    discarded = client.discard_image(image_ref)

    assert image.filename.endswith("source.png")
    assert image.type == "input"
    assert image.data == b"png-bytes"
    assert discarded["file_deleted"] is True


def test_diffusers_huggingface_progress_tqdm_reports_download_bytes(tmp_path):
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    events = []

    tqdm_cls = client._huggingface_progress_tqdm_class(
        events.append,
        label="model.safetensors",
        base_percent=10,
        span_percent=30,
    )
    assert tqdm_cls is not None

    bar = tqdm_cls(total=100, unit="B", desc="model.safetensors")
    bar.update(40)
    bar.close()

    assert events
    assert events[-1]["phase"] == "downloading"
    assert events[-1]["bytes_written"] == 40
    assert events[-1]["total_bytes"] == 100
    assert events[-1]["percent"] == 22
    assert events[-1]["current_file"] == "model.safetensors"
    assert "speed_bytes_per_sec" in events[-1]
    assert events[-1]["step"] == "Hugging Face 檔案下載"
    assert "model.safetensors" in events[-1]["detail"]


def test_diffusers_download_heartbeat_uses_incomplete_cache_bytes(tmp_path, monkeypatch):
    hf_home = tmp_path / "hf"
    cache_dir = hf_home / "hub" / "models--owner--model" / "blobs"
    cache_dir.mkdir(parents=True)
    (cache_dir / "download.incomplete").write_bytes(b"x" * 123)
    monkeypatch.setenv("HF_HOME", str(hf_home))
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    tracker = {"bytes_written": 40, "total_bytes": 1000}

    payload = client._download_heartbeat_payload(tracker, current_file="model.gguf", model_repo="owner/model")

    assert payload["bytes_written"] == 123
    assert payload["total_bytes"] == 1000
    assert tracker["external_bytes_written"] == 123
    assert payload["current_file"] == "model.gguf"


def test_diffusers_client_configures_root_huggingface_cache_env(tmp_path, monkeypatch):
    cache_root = tmp_path / "hf-cache"
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    client = DiffusersClient(
        model_repo="owner/model",
        storage_root=tmp_path,
        huggingface_cache_root=str(cache_root),
    )

    backend = client._configure_huggingface_download_backend()

    assert os.environ["HF_HOME"] == str(cache_root)
    assert os.environ["HF_HUB_CACHE"] == str(cache_root / "hub")
    assert os.environ["HUGGINGFACE_HUB_CACHE"] == str(cache_root / "hub")
    assert backend["cache_root"] == str(cache_root)
    assert backend["hub_cache"] == str(cache_root / "hub")


def test_diffusers_stream_huggingface_file_download_reports_bytes(tmp_path, monkeypatch):
    client = DiffusersClient(model_repo="owner/model", token="hf_download_token", storage_root=tmp_path)
    events = []
    requests = []

    class FakeResponse:
        status = 200
        headers = {"Content-Length": "6"}

        def __init__(self):
            self._body = io.BytesIO(b"abcdef")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            return self._body.read(size)

    def fake_urlopen(request, timeout=60):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr("services.comfyui.diffusers_client.urllib_request.urlopen", fake_urlopen)

    path, stats = client._stream_huggingface_file_download(
        "owner/model",
        "model.gguf",
        progress_callback=events.append,
        backend={"xet_disabled": True, "hf_transfer_enabled": False},
    )

    assert requests
    assert requests[0].full_url == "https://huggingface.co/owner/model/resolve/main/model.gguf"
    assert requests[0].headers["Authorization"] == "Bearer hf_download_token"
    with open(path, "rb") as handle:
        assert handle.read() == b"abcdef"
    assert stats == {"cache_hit": False, "bytes_written": 6, "total_bytes": 6}
    assert events[-1]["phase"] == "downloading"
    assert events[-1]["step"] == "Hugging Face GGUF 串流下載"
    assert events[-1]["bytes_written"] == 6
    assert events[-1]["total_bytes"] == 6
    assert events[-1]["xet_disabled"] is True


def test_diffusers_rejects_comfyui_native_gguf_before_pipeline_load(tmp_path, monkeypatch):
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"fake")
    events = []

    monkeypatch.setattr(
        client,
        "_inspect_gguf_file_metadata",
        lambda path: {
            "field_count": 2,
            "tensor_count": 1,
            "has_comfy_metadata": True,
            "has_original_unet_names": True,
            "sample_tensors": ["input_blocks.0.0.weight"],
        },
    )

    with pytest.raises(ComfyUIError) as exc:
        client._raise_if_unsupported_diffusers_gguf(
            gguf_path,
            gguf_file="model.gguf",
            progress_callback=events.append,
        )

    assert "ComfyUI-GGUF 原生 UNet GGUF" in str(exc.value)
    assert "Unet Loader (GGUF)" in str(exc.value)
    assert events[-1]["phase"] == "error"
    assert events[-1]["step"] == "GGUF 格式不支援 Diffusers backend"
    assert events[-1]["gguf_metadata"]["has_comfy_metadata"] is True


def test_diffusers_prepare_gguf_file_classifies_comfyui_native_backend(tmp_path, monkeypatch):
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"fake-gguf")
    events = []

    monkeypatch.setattr(client, "_ensure_gguf_metadata_dependencies", lambda: None)
    monkeypatch.setattr(
        client,
        "_stream_huggingface_file_download",
        lambda *args, **kwargs: (
            str(gguf_path),
            {"cache_hit": False, "bytes_written": gguf_path.stat().st_size, "total_bytes": gguf_path.stat().st_size},
        ),
    )
    monkeypatch.setattr(
        client,
        "_inspect_gguf_file_metadata",
        lambda path: {
            "field_count": 3,
            "tensor_count": 12,
            "has_comfy_metadata": True,
            "has_original_unet_names": True,
            "sample_tensors": ["input_blocks.0.weight"],
        },
    )

    result = client.prepare_gguf_file_for_backend("owner/model", "model.gguf", progress_callback=events.append)

    assert result["suggested_backend"] == "comfyui_gguf"
    assert result["path"] == str(gguf_path)
    assert events[-1]["backend_kind"] == "comfyui_gguf"
    assert events[-1]["step"] == "GGUF backend 已判斷"


def test_diffusers_gguf_loader_preserves_unsupported_format_error(tmp_path, monkeypatch):
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"fake")
    fake_diffusers = types.ModuleType("diffusers")
    fake_diffusers.GGUFQuantizationConfig = object
    monkeypatch.setitem(sys.modules, "diffusers", fake_diffusers)
    monkeypatch.setattr(
        client,
        "_stream_huggingface_file_download",
        lambda *args, **kwargs: (str(gguf_path), {"cache_hit": True, "bytes_written": 4, "total_bytes": 4}),
    )
    monkeypatch.setattr(
        client,
        "_inspect_gguf_file_metadata",
        lambda path: {
            "field_count": 2,
            "tensor_count": 1,
            "has_comfy_metadata": True,
            "has_original_unet_names": True,
            "sample_tensors": ["input_blocks.0.0.weight"],
        },
    )

    with pytest.raises(ComfyUIError) as exc:
        client._load_gguf_pipeline(
            "txt2img",
            model_repo="owner/model",
            gguf_file="model.gguf",
            gguf_base_repo="base/model",
            dtype=object(),
            device="cpu",
            progress_callback=lambda event: None,
        )

    assert "ComfyUI-GGUF 原生 UNet GGUF" in str(exc.value)
    assert "GGUF 檔案下載失敗" not in str(exc.value)


def test_diffusers_snapshot_patterns_avoid_duplicate_precision_downloads(tmp_path):
    client = DiffusersClient(model_repo="owner/model", storage_root=tmp_path)

    default_allow, default_ignore = client._diffusers_snapshot_patterns()
    fp16_allow, fp16_ignore = client._diffusers_snapshot_patterns("fp16")

    assert "unet/*.safetensors" in default_allow
    assert "vae/**/*.bin" in default_allow
    assert "*.safetensors" not in default_allow
    assert "*.fp16.*" in default_ignore
    assert "**/*.bf16.*" in default_ignore
    assert "unet/*.fp16.safetensors" in fp16_allow
    assert "text_encoder_2/**/*.fp16.bin" in fp16_allow
    assert "*.fp16.safetensors" not in fp16_allow
    assert fp16_ignore is None


def test_diffusers_backend_client_uses_live_settings_over_worker_cache(monkeypatch):
    import routes.comfyui_sections.admin_helpers as admin_helpers

    monkeypatch.setattr(
        admin_helpers,
        "load_system_settings_from_db",
        lambda: {
            "comfyui_diffusers_device_map": "balanced_low_0",
            "comfyui_diffusers_low_cpu_mem_usage": True,
        },
    )
    monkeypatch.setattr(
        admin_helpers,
        "get_system_settings",
        lambda: {
            "comfyui_diffusers_device_map": "disabled",
            "comfyui_diffusers_low_cpu_mem_usage": False,
        },
        raising=False,
    )

    settings = admin_helpers._live_diffusers_settings()

    assert settings["comfyui_diffusers_device_map"] == "balanced_low_0"
    assert settings["comfyui_diffusers_low_cpu_mem_usage"] is True


def test_comfyui_binding_uses_live_connection_mode_over_worker_cache(monkeypatch):
    import routes.comfyui_sections.admin_helpers as admin_helpers

    monkeypatch.setattr(
        admin_helpers,
        "load_system_settings_from_db",
        lambda: {
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": "owner/live-model",
            "comfyui_remote_api_url": "http://stale-remote:8188",
        },
    )
    monkeypatch.setattr(
        admin_helpers,
        "get_system_settings",
        lambda: {
            "comfyui_connection_mode": "remote",
            "comfyui_diffusers_model_repo": "owner/stale-model",
            "comfyui_remote_api_url": "http://stale-remote:8188",
        },
        raising=False,
    )

    binding = admin_helpers._comfyui_binding()

    assert binding["connection_mode"] == "diffusers"
    assert binding["url"] == diffusers_backend_url("owner/live-model")
    assert binding["backend_scope"] == "primary"


def test_diffusers_backend_client_falls_back_to_cached_settings(monkeypatch):
    import routes.comfyui_sections.admin_helpers as admin_helpers

    def fail_live_load():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(admin_helpers, "load_system_settings_from_db", fail_live_load)
    monkeypatch.setattr(
        admin_helpers,
        "get_system_settings",
        lambda: {"comfyui_diffusers_device_map": "disabled"},
        raising=False,
    )

    settings = admin_helpers._live_diffusers_settings()

    assert settings["comfyui_diffusers_device_map"] == "disabled"
