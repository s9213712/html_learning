"""ComfyUI-specific settings defaults and validation helpers.

這一層只放 ComfyUI 專屬設定：
- 預設值
- key 清單
- admin/settings 會共用的驗證 helper

全站的 system settings 儲存、feature flag 與跨模組依賴規則，仍留在
``services.platform.settings``。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_COMFYUI_REMOTE_API_URL = os.environ.get("COMFYUI_API_URL", "http://192.168.18.19:8188").rstrip("/")
DEFAULT_COMFYUI_PORT = 8192
DEFAULT_COMFYUI_MAX_BATCH_SIZE = 1
DEFAULT_COMFYUI_WIDTH = 1024
DEFAULT_COMFYUI_HEIGHT = 1024


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _env_choice(name, default, allowed):
    raw = str(os.environ.get(name, default) or default).strip().lower()
    return raw if raw in allowed else default


def _env_int_range(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except Exception:
        return default
    return min(maximum, max(minimum, value))


def _env_float_text(name, default=""):
    raw = str(os.environ.get(name, default) or "").strip()
    if not raw:
        return ""
    try:
        value = float(raw)
    except Exception:
        return ""
    if value < 0 or value > 128:
        return ""
    return ("%0.3f" % value).rstrip("0").rstrip(".")


def _first_env_text(*names):
    for name in names:
        raw = str(os.environ.get(name, "") or "").strip()
        if raw:
            return raw
    return ""


COMFYUI_LOCAL_VRAM_MODES = {"auto", "gpu_only", "highvram", "normalvram", "lowvram", "novram", "cpu"}
COMFYUI_LOCAL_PRECISION_MODES = {"auto", "force_fp16", "force_fp32"}
COMFYUI_LOCAL_UNET_DTYPES = {
    "auto",
    "fp32",
    "fp64",
    "bf16",
    "fp16",
    "fp8_e4m3fn",
    "fp8_e5m2",
    "fp8_e8m0fnu",
}
COMFYUI_LOCAL_VAE_DTYPES = {"auto", "fp16", "fp32", "bf16"}
COMFYUI_LOCAL_TEXT_ENCODER_DTYPES = {"auto", "fp8_e4m3fn", "fp8_e5m2", "fp16", "fp32", "bf16"}
COMFYUI_LOCAL_ATTENTION_MODES = {
    "auto",
    "split",
    "quad",
    "pytorch",
    "sage",
    "flash",
    "disable_xformers",
}
COMFYUI_LOCAL_UPCAST_ATTENTION_MODES = {"auto", "force", "dont"}
COMFYUI_LOCAL_CUDA_MALLOC_MODES = {"auto", "enable", "disable"}
COMFYUI_LOCAL_ASYNC_OFFLOAD_MODES = {"auto", "enable", "disable"}
COMFYUI_LOCAL_CACHE_MODES = {"auto", "ram", "classic", "lru", "none"}


COMFYUI_DEFAULT_SETTINGS = {
    "comfyui_connection_mode": os.environ.get("COMFYUI_CONNECTION_MODE", "remote"),
    "comfyui_remote_api_url": DEFAULT_COMFYUI_REMOTE_API_URL,
    "comfyui_base_dir": os.environ.get("COMFYUI_BASE_DIR", ""),
    "comfyui_local_start_script": os.environ.get("COMFYUI_START_SCRIPT", ""),
    "comfyui_api_host": os.environ.get("COMFYUI_API_HOST", "localhost"),
    "comfyui_api_port": DEFAULT_COMFYUI_PORT,
    "comfyui_local_vram_mode": _env_choice("COMFYUI_LOCAL_VRAM_MODE", "auto", COMFYUI_LOCAL_VRAM_MODES),
    "comfyui_local_precision": _env_choice("COMFYUI_LOCAL_PRECISION", "auto", COMFYUI_LOCAL_PRECISION_MODES),
    "comfyui_local_unet_dtype": _env_choice("COMFYUI_LOCAL_UNET_DTYPE", "auto", COMFYUI_LOCAL_UNET_DTYPES),
    "comfyui_local_vae_dtype": _env_choice("COMFYUI_LOCAL_VAE_DTYPE", "auto", COMFYUI_LOCAL_VAE_DTYPES),
    "comfyui_local_text_encoder_dtype": _env_choice(
        "COMFYUI_LOCAL_TEXT_ENCODER_DTYPE",
        "auto",
        COMFYUI_LOCAL_TEXT_ENCODER_DTYPES,
    ),
    "comfyui_local_cpu_vae": _env_bool("COMFYUI_LOCAL_CPU_VAE", False),
    "comfyui_local_attention_mode": _env_choice(
        "COMFYUI_LOCAL_ATTENTION_MODE",
        "auto",
        COMFYUI_LOCAL_ATTENTION_MODES,
    ),
    "comfyui_local_upcast_attention": _env_choice(
        "COMFYUI_LOCAL_UPCAST_ATTENTION",
        "auto",
        COMFYUI_LOCAL_UPCAST_ATTENTION_MODES,
    ),
    "comfyui_local_cuda_malloc": _env_choice(
        "COMFYUI_LOCAL_CUDA_MALLOC",
        "auto",
        COMFYUI_LOCAL_CUDA_MALLOC_MODES,
    ),
    "comfyui_local_disable_smart_memory": _env_bool("COMFYUI_LOCAL_DISABLE_SMART_MEMORY", False),
    "comfyui_local_deterministic": _env_bool("COMFYUI_LOCAL_DETERMINISTIC", False),
    "comfyui_local_async_offload": _env_choice(
        "COMFYUI_LOCAL_ASYNC_OFFLOAD",
        "auto",
        COMFYUI_LOCAL_ASYNC_OFFLOAD_MODES,
    ),
    "comfyui_local_cache_mode": _env_choice("COMFYUI_LOCAL_CACHE_MODE", "auto", COMFYUI_LOCAL_CACHE_MODES),
    "comfyui_local_cache_lru": _env_int_range("COMFYUI_LOCAL_CACHE_LRU", 0, 0, 10000),
    "comfyui_local_reserve_vram_gb": _env_float_text("COMFYUI_LOCAL_RESERVE_VRAM_GB", ""),
    "comfyui_civitai_api_key": os.environ.get("CIVITAI_API_KEY", ""),
    "comfyui_paid_api_nodes_enabled": False,
    "comfyui_account_api_key": os.environ.get("COMFYUI_ACCOUNT_API_KEY", ""),
    "comfyui_max_batch_size": DEFAULT_COMFYUI_MAX_BATCH_SIZE,
    "comfyui_default_width": DEFAULT_COMFYUI_WIDTH,
    "comfyui_default_height": DEFAULT_COMFYUI_HEIGHT,
    "comfyui_diffusers_model_repo": (
        os.environ.get("COMFYUI_DIFFUSERS_MODEL_REPO")
        or os.environ.get("HF_DIFFUSERS_MODEL_REPO")
        or ""
    ),
    "comfyui_huggingface_api_token": (
        os.environ.get("COMFYUI_HUGGINGFACE_API_TOKEN")
        or os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        or ""
    ),
    "comfyui_huggingface_cache_root": _first_env_text(
        "COMFYUI_HUGGINGFACE_CACHE_ROOT",
        "HACKME_HUGGINGFACE_CACHE_ROOT",
    ),
    "comfyui_diffusers_device": os.environ.get("COMFYUI_DIFFUSERS_DEVICE", "auto"),
    "comfyui_diffusers_dtype": os.environ.get("COMFYUI_DIFFUSERS_DTYPE", "auto"),
    "comfyui_diffusers_device_map": os.environ.get("COMFYUI_DIFFUSERS_DEVICE_MAP", "auto"),
    "comfyui_diffusers_low_cpu_mem_usage": _env_bool("COMFYUI_DIFFUSERS_LOW_CPU_MEM_USAGE", True),
    "comfyui_diffusers_cuda_fallback_to_cpu": _env_bool("COMFYUI_DIFFUSERS_CUDA_FALLBACK_TO_CPU", True),
    "comfyui_allow_in_process_diffusers": False,
    "comfyui_diffusers_keep_downloaded_models": _env_bool("COMFYUI_DIFFUSERS_KEEP_DOWNLOADED_MODELS", True),
    "comfyui_diffusers_disable_xet": _env_bool("COMFYUI_DIFFUSERS_DISABLE_XET", True),
}

COMFYUI_SETTING_KEYS = tuple(COMFYUI_DEFAULT_SETTINGS)
COMFYUI_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
HUGGINGFACE_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
HUGGINGFACE_MODEL_FILE_EXTENSIONS = {".safetensors", ".bin", ".ckpt", ".pt", ".pth", ".gguf"}


def normalize_comfyui_connection_mode(value):
    mode = str(value or "").strip().lower()
    if mode in {"local", "remote", "diffusers"}:
        return mode
    return None


def validate_huggingface_repo_id(value, *, allow_blank=False):
    repo_id = str(value or "").strip()
    if not repo_id:
        return "" if allow_blank else None
    if len(repo_id) > 200:
        return None
    if "\\" in repo_id or repo_id.startswith(("/", ".")) or ".." in repo_id.split("/"):
        return None
    if not HUGGINGFACE_REPO_ID_RE.match(repo_id):
        return None
    repo_name = repo_id.rsplit("/", 1)[-1].lower()
    if any(repo_name.endswith(ext) for ext in HUGGINGFACE_MODEL_FILE_EXTENSIONS):
        return None
    return repo_id


def normalize_huggingface_repo_id(value, *, allow_blank=False):
    raw = str(value or "").strip()
    if not raw:
        return "" if allow_blank else None
    if raw.startswith(("http://", "https://")) or raw.lower().startswith("huggingface.co/"):
        parsed = urlparse(raw if raw.startswith(("http://", "https://")) else f"https://{raw}")
        host = (parsed.hostname or "").lower()
        if host not in {"huggingface.co", "www.huggingface.co"}:
            return None
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < 2:
            return None
        raw = f"{parts[0]}/{parts[1]}"
    return validate_huggingface_repo_id(raw, allow_blank=allow_blank)


def validate_huggingface_api_token(value, *, allow_blank=False):
    token = str(value or "").strip()
    if not token:
        return "" if allow_blank else None
    if len(token) > 2048 or any(ch.isspace() for ch in token):
        return None
    return token


def validate_comfyui_diffusers_device(value):
    device = str(value or "auto").strip().lower()
    if device in {"auto", "cpu", "cuda", "mps"}:
        return device
    return None


def validate_comfyui_diffusers_dtype(value):
    dtype = str(value or "auto").strip().lower()
    if dtype in {"auto", "float16", "bfloat16", "float32"}:
        return dtype
    return None


def validate_comfyui_diffusers_device_map(value):
    device_map = str(value or "auto").strip().lower()
    if device_map in {"auto", "disabled", "none", "cuda", "balanced", "balanced_low_0", "sequential"}:
        return "disabled" if device_map == "none" else device_map
    return None


def _validate_comfyui_choice(value, allowed, default="auto"):
    normalized = str(value if value is not None else default).strip().lower()
    if normalized in allowed:
        return normalized
    return None


def validate_comfyui_local_vram_mode(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_VRAM_MODES)


def validate_comfyui_local_precision(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_PRECISION_MODES)


def validate_comfyui_local_unet_dtype(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_UNET_DTYPES)


def validate_comfyui_local_vae_dtype(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_VAE_DTYPES)


def validate_comfyui_local_text_encoder_dtype(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_TEXT_ENCODER_DTYPES)


def validate_comfyui_local_attention_mode(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_ATTENTION_MODES)


def validate_comfyui_local_upcast_attention(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_UPCAST_ATTENTION_MODES)


def validate_comfyui_local_cuda_malloc(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_CUDA_MALLOC_MODES)


def validate_comfyui_local_async_offload(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_ASYNC_OFFLOAD_MODES)


def validate_comfyui_local_cache_mode(value):
    return _validate_comfyui_choice(value, COMFYUI_LOCAL_CACHE_MODES)


def validate_comfyui_local_cache_lru(value):
    try:
        count = int(value)
    except Exception:
        return None
    if count < 0 or count > 10000:
        return None
    return count


def validate_comfyui_local_reserve_vram_gb(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        amount = float(raw)
    except Exception:
        return None
    if amount < 0 or amount > 128:
        return None
    return ("%0.3f" % amount).rstrip("0").rstrip(".")


def validate_comfyui_api_host(value):
    host = str(value or "").strip().strip("[]")
    if not host:
        return None
    if len(host) > 253:
        return None
    forbidden = ("://", "/", "\\", "@", "?", "#", "%", " ")
    if any(part in host for part in forbidden):
        return None
    if not COMFYUI_HOST_RE.match(host):
        return None
    return host


def validate_comfyui_api_url(value, *, allow_blank=False):
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return "" if allow_blank else None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.port is None:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    return raw


def validate_comfyui_relative_script(value, *, base_dir=None):
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 240:
        return None
    try:
        if raw.startswith("/") or raw.startswith("\\"):
            if not base_dir:
                return None
            base = Path(str(base_dir)).expanduser().resolve()
            target = Path(raw).expanduser().resolve()
            rel = target.relative_to(base)
            parts = rel.as_posix().split("/")
            if not parts or any(part in {"", ".", ".."} for part in parts):
                return None
            return rel.as_posix()
        parts = raw.replace("\\", "/").split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return None
        return "/".join(parts)
    except Exception:
        return None


def validate_comfyui_api_port(value):
    try:
        port = int(value)
    except Exception:
        return None
    if port < 1 or port > 65535:
        return None
    return port


def validate_comfyui_batch_size(value):
    try:
        batch_size = int(value)
    except Exception:
        return None
    if batch_size < 1 or batch_size > 8:
        return None
    return batch_size


def validate_comfyui_dimension(value):
    try:
        size = int(value)
    except Exception:
        return None
    if size < 64 or size > 2048 or size % 8 != 0:
        return None
    return size
