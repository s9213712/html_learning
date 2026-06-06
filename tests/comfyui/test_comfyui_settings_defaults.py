import os

from services.comfyui.settings import COMFYUI_DEFAULT_SETTINGS, DEFAULT_COMFYUI_REMOTE_API_URL
from services.platform.settings import DEFAULT_SETTINGS


def test_comfyui_defaults_use_localhost_remote_api_mode():
    expected_url = os.environ.get("COMFYUI_API_URL", "http://127.0.0.1:8188").rstrip("/")
    assert DEFAULT_COMFYUI_REMOTE_API_URL == expected_url
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_connection_mode"] == "remote"
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_remote_api_url"] == DEFAULT_COMFYUI_REMOTE_API_URL
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_allow_in_process_diffusers"] is False
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_diffusers_device_map"] == "auto"
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_diffusers_low_cpu_mem_usage"] is True
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_diffusers_cuda_fallback_to_cpu"] is True
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_diffusers_keep_downloaded_models"] is True
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_diffusers_disable_xet"] is True
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_huggingface_cache_root"] == (
        os.environ.get("COMFYUI_HUGGINGFACE_CACHE_ROOT") or os.environ.get("HACKME_HUGGINGFACE_CACHE_ROOT") or ""
    )
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_local_vram_mode"] == "auto"
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_local_precision"] == "auto"
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_local_unet_dtype"] == "auto"
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_local_vae_dtype"] == "auto"
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_local_text_encoder_dtype"] == "auto"
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_local_cpu_vae"] is False
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_local_attention_mode"] == "auto"
    assert COMFYUI_DEFAULT_SETTINGS["comfyui_local_cache_mode"] == "auto"
    assert DEFAULT_SETTINGS["comfyui_remote_api_url"] == DEFAULT_COMFYUI_REMOTE_API_URL
    assert DEFAULT_SETTINGS["comfyui_allow_in_process_diffusers"] is False
    assert DEFAULT_SETTINGS["comfyui_diffusers_device_map"] == "auto"
    assert DEFAULT_SETTINGS["comfyui_diffusers_low_cpu_mem_usage"] is True
    assert DEFAULT_SETTINGS["comfyui_diffusers_cuda_fallback_to_cpu"] is True
    assert DEFAULT_SETTINGS["comfyui_diffusers_keep_downloaded_models"] is True
    assert DEFAULT_SETTINGS["comfyui_diffusers_disable_xet"] is True
    assert DEFAULT_SETTINGS["comfyui_huggingface_cache_root"] == COMFYUI_DEFAULT_SETTINGS["comfyui_huggingface_cache_root"]
    assert DEFAULT_SETTINGS["comfyui_local_vram_mode"] == "auto"
    assert DEFAULT_SETTINGS["comfyui_local_cpu_vae"] is False
