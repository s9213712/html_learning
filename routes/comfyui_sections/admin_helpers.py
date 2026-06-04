"""ComfyUI backend/admin helper factory.

This module keeps the large backend configuration, local runtime, Civitai,
and model-download helpers outside routes/comfyui.py while preserving the
existing closure-based dependency injection contract.
"""

import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import signal
import shlex
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse

from services.comfyui.client import ComfyUIClient, ComfyUIError
from services.comfyui.diffusers_client import DiffusersClient, diffusers_backend_url, repo_id_from_diffusers_url
from services.comfyui.settings import (
    DEFAULT_COMFYUI_PORT,
    normalize_comfyui_connection_mode,
    normalize_huggingface_repo_id,
    validate_comfyui_api_host,
    validate_comfyui_api_port,
    validate_comfyui_api_url,
)
from services.platform.admin_validation import (
    validate_comfyui_api_host as shared_validate_comfyui_api_host,
    validate_comfyui_api_url as shared_validate_comfyui_api_url,
)
from services.platform.settings import load_system_settings_from_db
from services.comfyui.template import runtime_comfyui_dir


def _int_range(value, default, minimum, maximum, *, multiple_of=None):
    try:
        number = int(value)
    except Exception:
        number = default
    number = max(minimum, min(maximum, number))
    if multiple_of:
        number = max(minimum, (number // multiple_of) * multiple_of)
    return number


def build_comfyui_admin_helpers(ctx):
    globals().update({
        "_MemoryFile": ctx["MemoryFile"],
        "_actor_value": ctx["actor_value"],
        "DEFAULT_COMFYUI_URL": ctx["DEFAULT_COMFYUI_URL"],
        "SAFE_SAMPLER_FALLBACK": ctx["SAFE_SAMPLER_FALLBACK"],
        "SAFE_SCHEDULER_FALLBACK": ctx["SAFE_SCHEDULER_FALLBACK"],
        "COMFYUI_LOCAL_START_TEMPLATE_PATH": ctx["COMFYUI_LOCAL_START_TEMPLATE_PATH"],
        "COMFYUI_MODEL_DOWNLOAD_EXTENSIONS": ctx["COMFYUI_MODEL_DOWNLOAD_EXTENSIONS"],
        "COMFYUI_MODEL_DOWNLOAD_TYPES": ctx["COMFYUI_MODEL_DOWNLOAD_TYPES"],
        "COMFYUI_SUPPORTED_LORA_BASE_MODEL_FAMILIES": ctx["COMFYUI_SUPPORTED_LORA_BASE_MODEL_FAMILIES"],
        "MAX_COMFYUI_MODEL_DOWNLOAD_BYTES": ctx["MAX_COMFYUI_MODEL_DOWNLOAD_BYTES"],
        "COMFYUI_BACKEND_REQUEST_TIMEOUT_SECONDS": ctx.get("COMFYUI_BACKEND_REQUEST_TIMEOUT_SECONDS", 8),
        "CIVITAI_ALLOWED_HOSTS": ctx["CIVITAI_ALLOWED_HOSTS"],
        "CIVITAI_API_BASE": ctx["CIVITAI_API_BASE"],
        "CIVITAI_API_BASES": ctx.get("CIVITAI_API_BASES") or [ctx["CIVITAI_API_BASE"]],
        "CIVITAI_MODEL_TYPE_TO_DOWNLOAD_TYPE": ctx["CIVITAI_MODEL_TYPE_TO_DOWNLOAD_TYPE"],
        "CIVITAI_SEARCH_TYPE_TO_API": ctx["CIVITAI_SEARCH_TYPE_TO_API"],
        "MemoryFile": ctx["MemoryFile"],
        "actor_value": ctx["actor_value"],
        "audit": ctx["audit"],
        "deps": ctx["deps"],
        "get_client_ip": ctx["get_client_ip"],
        "generation_owner_id": ctx["generation_owner_id"],
        "get_system_settings": ctx["get_system_settings"],
        "get_ua": ctx["get_ua"],
        "injected_client": ctx["injected_client"],
        "json_resp": ctx["json_resp"],
        "model_download_jobs": ctx["model_download_jobs"],
        "model_download_jobs_lock": ctx["model_download_jobs_lock"],
    })
    global _generation_owner_id
    _generation_owner_id = ctx["generation_owner_id"]
    return {
        "_validate_comfyui_host": _validate_comfyui_host,
        "_parse_comfyui_endpoint": _parse_comfyui_endpoint,
        "_validate_comfyui_api_url": _validate_comfyui_api_url,
        "_normalize_comfyui_backend_url": _normalize_comfyui_backend_url,
        "_configured_connection_mode": _configured_connection_mode,
        "_configured_comfyui_url": _configured_comfyui_url,
        "_comfyui_binding": _comfyui_binding,
        "_configured_local_start_script": _configured_local_start_script,
        "_configured_comfyui_port": _configured_comfyui_port,
        "_local_comfyui_state_path": _local_comfyui_state_path,
        "_write_local_comfyui_state": _write_local_comfyui_state,
        "_read_local_comfyui_state": _read_local_comfyui_state,
        "_clear_local_comfyui_state": _clear_local_comfyui_state,
        "_tail_text_lines": _tail_text_lines,
        "_local_comfyui_runtime_status": _local_comfyui_runtime_status,
        "_pid_exists": _pid_exists,
        "_pid_cmdline": _pid_cmdline,
        "_listener_pids_for_port": _listener_pids_for_port,
        "_proc_scan_comfyui_pids": _proc_scan_comfyui_pids,
        "_looks_like_comfyui_process": _looks_like_comfyui_process,
        "_terminate_local_comfyui_targets": _terminate_local_comfyui_targets,
        "_stop_local_comfyui": _stop_local_comfyui,
        "_local_start_script_status": _local_start_script_status,
        "_start_local_comfyui": _start_local_comfyui,
        "_configured_max_batch_size": _configured_max_batch_size,
        "_configured_default_dimensions": _configured_default_dimensions,
        "_configured_comfyui_base_dir": _configured_comfyui_base_dir,
        "_configured_comfyui_project_dir": _configured_comfyui_project_dir,
        "_configured_civitai_api_key": _configured_civitai_api_key,
        "_public_download_host": _public_download_host,
        "_safe_model_filename": _safe_model_filename,
        "_normalize_download_model_type": _normalize_download_model_type,
        "_filename_from_content_disposition": _filename_from_content_disposition,
        "_append_civitai_token": _append_civitai_token,
        "_civitai_headers": _civitai_headers,
        "_comfyui_model_sidecar_path": _comfyui_model_sidecar_path,
        "_normalize_model_relative_dir": _normalize_model_relative_dir,
        "_split_model_relative_name": _split_model_relative_name,
        "_resolve_model_destination_dir": _resolve_model_destination_dir,
        "_comfyui_model_sidecar_path_with_relative": _comfyui_model_sidecar_path_with_relative,
        "_write_comfyui_model_sidecar": _write_comfyui_model_sidecar,
        "_read_comfyui_model_sidecar": _read_comfyui_model_sidecar,
        "_normalize_lora_base_model_family": _normalize_lora_base_model_family,
        "_lora_support_payload": _lora_support_payload,
        "_build_lora_details": _build_lora_details,
        "_public_or_civitai_host": _public_or_civitai_host,
        "_parse_civitai_reference": _parse_civitai_reference,
        "_fetch_json": _fetch_json,
        "_civitai_api_get": _civitai_api_get,
        "_normalize_civitai_search_type": _normalize_civitai_search_type,
        "_normalize_civitai_nsfw_mode": _normalize_civitai_nsfw_mode,
        "_serialize_civitai_file": _serialize_civitai_file,
        "_serialize_civitai_versions": _serialize_civitai_versions,
        "_build_civitai_page_url": _build_civitai_page_url,
        "_safe_civitai_media_url": _safe_civitai_media_url,
        "_fetch_civitai_media": _fetch_civitai_media,
        "_serialize_civitai_search_results": _serialize_civitai_search_results,
        "_search_civitai_models": _search_civitai_models,
        "_inspect_civitai_model": _inspect_civitai_model,
        "_create_model_download_job": _create_model_download_job,
        "_update_model_download_job": _update_model_download_job,
        "_update_model_download_progress": _update_model_download_progress,
        "_get_model_download_job": _get_model_download_job,
        "_assert_model_download_job_owner": _assert_model_download_job_owner,
        "_parse_civitai_download_request": _parse_civitai_download_request,
        "_download_comfyui_model_file": _download_comfyui_model_file,
        "_download_civitai_model_selection": _download_civitai_model_selection,
        "_upload_comfyui_model_file": _upload_comfyui_model_file,
        "_client": _client,
        "_client_for_url": _client_for_url,
    }

def _validate_comfyui_host(value):
    return validate_comfyui_api_host(value)

def _parse_comfyui_endpoint(data):
    mode = normalize_comfyui_connection_mode(
        (data or {}).get("mode") or (data or {}).get("comfyui_connection_mode") or _configured_connection_mode()
    ) or "remote"
    if mode == "diffusers":
        settings = _live_comfyui_settings()
        repo_id = normalize_huggingface_repo_id(
            (data or {}).get("diffusers_model_repo")
            or (data or {}).get("comfyui_diffusers_model_repo")
            or settings.get("comfyui_diffusers_model_repo"),
            allow_blank=True,
        )
        if not repo_id:
            return None, None, "Diffusers 模式請先填 Hugging Face model repo，例如 dhead/waiIllustriousSDXL_v150"
        url = diffusers_backend_url(repo_id)
        return url, {"mode": "diffusers", "model_repo": repo_id}, None
    if mode == "remote":
        raw_url = str((data or {}).get("api_url") or (data or {}).get("comfyui_remote_api_url") or "").strip()
        if raw_url:
            url, msg = _validate_comfyui_api_url(raw_url)
            if msg:
                return None, None, msg
            parsed = urlparse(url)
            return url, {"mode": "remote", "api_url": url, "host": parsed.hostname, "port": parsed.port or (443 if parsed.scheme == "https" else 80)}, None
    default_url = urlparse(DEFAULT_COMFYUI_URL)
    host = _validate_comfyui_host(data.get("host") or data.get("comfyui_api_host") or default_url.hostname or "localhost")
    if host is None:
        return None, None, "ComfyUI Host / IP 必須是主機名稱或 IP，不可包含 http://、路徑、帳密或特殊字元"
    port = validate_comfyui_api_port(data.get("port") or data.get("comfyui_api_port") or default_url.port or DEFAULT_COMFYUI_PORT)
    if port is None:
        return None, None, "ComfyUI Port 必須是 1-65535"
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{display_host}:{port}", {"mode": mode, "host": host, "port": port}, None

def _validate_comfyui_api_url(value):
    # Prefer shared_validate_comfyui_api_url with return_error=True so the
    # specific reject reason (credentials / path / shape) surfaces in the UI
    # instead of being collapsed into one catch-all message.
    raw, reason = shared_validate_comfyui_api_url(value, allow_blank=True, return_error=True)
    if raw == "":
        return None, "ComfyUI API 位址不可空白"
    if raw is None:
        if reason == "credentials":
            return None, "ComfyUI API 位址不可包含帳密"
        # path-or-query and shape errors share the same generic prompt because
        # the user-recoverable answer is the same: drop everything but host:port.
        return None, "ComfyUI API 位址只需填主機與 port，不要包含路徑或參數"
    return raw, None

def _normalize_comfyui_backend_url(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme == "diffusers" and parsed.netloc == "frontend" and not parsed.path.strip("/") and not parsed.query and not parsed.fragment:
        return diffusers_backend_url("")
    if parsed.scheme == "diffusers" and parsed.netloc == "local" and not parsed.query and not parsed.fragment:
        repo_id = repo_id_from_diffusers_url(raw)
        normalized_repo_id = normalize_huggingface_repo_id(repo_id, allow_blank=True)
        if normalized_repo_id is not None:
            return diffusers_backend_url(normalized_repo_id)
        if not repo_id:
            return diffusers_backend_url(repo_id)
        return ""
    url, msg = _validate_comfyui_api_url(raw)
    return "" if msg else url

def _configured_connection_mode():
    settings = _live_comfyui_settings()
    return normalize_comfyui_connection_mode(settings.get("comfyui_connection_mode")) or "remote"

def _configured_comfyui_url():
    settings = _live_comfyui_settings()
    mode = _configured_connection_mode()
    if mode == "diffusers":
        repo_id = normalize_huggingface_repo_id(settings.get("comfyui_diffusers_model_repo"), allow_blank=True) or ""
        return diffusers_backend_url(repo_id)
    if mode == "remote":
        configured_url = str(settings.get("comfyui_remote_api_url") or "").strip()
        if configured_url:
            url, msg = _validate_comfyui_api_url(configured_url)
            if not msg:
                return url
        host_value = str(settings.get("comfyui_api_host") or "").strip().strip("[]").lower()
        try:
            port_value = int(settings.get("comfyui_api_port") or 0)
        except Exception:
            port_value = 0
        if host_value in {"", "localhost", "127.0.0.1"} and port_value in {0, DEFAULT_COMFYUI_PORT}:
            return DEFAULT_COMFYUI_URL
    default_url = urlparse(DEFAULT_COMFYUI_URL)
    host = str(settings.get("comfyui_api_host") or default_url.hostname or os.environ.get("COMFYUI_API_HOST") or "localhost").strip()
    host = host.strip("[]")
    if not host:
        host = "localhost"
    try:
        port = int(settings.get("comfyui_api_port") or default_url.port or DEFAULT_COMFYUI_PORT)
    except Exception:
        port = DEFAULT_COMFYUI_PORT
    port = min(65535, max(1, port))
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{display_host}:{port}"

def _comfyui_binding(actor=None, *, backend_url=None):
    primary_mode = _configured_connection_mode()
    primary_url = _configured_comfyui_url()
    explicit_url = _normalize_comfyui_backend_url(backend_url)
    if explicit_url:
        if explicit_url == _normalize_comfyui_backend_url(primary_url):
            return {
                "url": primary_url,
                "connection_mode": primary_mode,
                "backend_scope": "primary",
            }
        return {
            "url": explicit_url,
            "connection_mode": "diffusers" if explicit_url.startswith("diffusers://") else "remote",
            "backend_scope": "custom",
        }
    return {
        "url": primary_url,
        "connection_mode": primary_mode,
        "backend_scope": "primary",
    }

def _configured_local_start_script(value=None, *, base_dir=None):
    raw = str(value or _live_comfyui_settings().get("comfyui_local_start_script") or "").strip()
    if not raw:
        return None, None
    base = _configured_comfyui_base_dir(base_dir)
    if not base:
        return None, "請先設定 ComfyUI 本地資料夾"
    try:
        if raw.startswith("/") or raw.startswith("\\"):
            script = Path(raw).expanduser().resolve()
        else:
            if ".." in raw.replace("\\", "/").split("/"):
                return None, "ComfyUI 啟動腳本必須在本地資料夾內"
            script = (base / raw).resolve()
        script.relative_to(base)
    except Exception:
        return None, "ComfyUI 啟動腳本超出允許資料夾"
    if not script.exists() or not script.is_file():
        return None, f"找不到 ComfyUI 啟動腳本：{raw}"
    return script, None

def _configured_comfyui_port(url=None):
    try:
        parsed = urlparse(url or _configured_comfyui_url())
        port = int(parsed.port or DEFAULT_COMFYUI_PORT)
    except Exception:
        port = DEFAULT_COMFYUI_PORT
    return min(65535, max(1, port))


COMFYUI_LOCAL_VRAM_FLAGS = {
    "gpu_only": "--gpu-only",
    "highvram": "--highvram",
    "lowvram": "--lowvram",
    "novram": "--novram",
    "cpu": "--cpu",
}
COMFYUI_LOCAL_PRECISION_FLAGS = {
    "force_fp16": "--force-fp16",
    "force_fp32": "--force-fp32",
}
COMFYUI_LOCAL_UNET_DTYPE_FLAGS = {
    "fp32": "--fp32-unet",
    "fp64": "--fp64-unet",
    "bf16": "--bf16-unet",
    "fp16": "--fp16-unet",
    "fp8_e4m3fn": "--fp8_e4m3fn-unet",
    "fp8_e5m2": "--fp8_e5m2-unet",
    "fp8_e8m0fnu": "--fp8_e8m0fnu-unet",
}
COMFYUI_LOCAL_VAE_DTYPE_FLAGS = {
    "fp16": "--fp16-vae",
    "fp32": "--fp32-vae",
    "bf16": "--bf16-vae",
}
COMFYUI_LOCAL_TEXT_ENCODER_DTYPE_FLAGS = {
    "fp8_e4m3fn": "--fp8_e4m3fn-text-enc",
    "fp8_e5m2": "--fp8_e5m2-text-enc",
    "fp16": "--fp16-text-enc",
    "fp32": "--fp32-text-enc",
    "bf16": "--bf16-text-enc",
}
COMFYUI_LOCAL_ATTENTION_FLAGS = {
    "split": "--use-split-cross-attention",
    "quad": "--use-quad-cross-attention",
    "pytorch": "--use-pytorch-cross-attention",
    "sage": "--use-sage-attention",
    "flash": "--use-flash-attention",
    "disable_xformers": "--disable-xformers",
}
COMFYUI_LOCAL_UPCAST_ATTENTION_FLAGS = {
    "force": "--force-upcast-attention",
    "dont": "--dont-upcast-attention",
}
COMFYUI_LOCAL_CUDA_MALLOC_FLAGS = {
    "enable": "--cuda-malloc",
    "disable": "--disable-cuda-malloc",
}
COMFYUI_LOCAL_ASYNC_OFFLOAD_FLAGS = {
    "enable": "--async-offload",
    "disable": "--disable-async-offload",
}
COMFYUI_LOCAL_CACHE_FLAGS = {
    "ram": "--cache-ram",
    "classic": "--cache-classic",
    "none": "--cache-none",
}


def _build_local_comfyui_main_args(settings=None):
    settings = dict(settings or _live_comfyui_settings() or {})
    args = []

    def add_choice(setting_key, mapping):
        flag = mapping.get(str(settings.get(setting_key) or "auto").strip().lower())
        if flag:
            args.append(flag)

    add_choice("comfyui_local_vram_mode", COMFYUI_LOCAL_VRAM_FLAGS)
    add_choice("comfyui_local_precision", COMFYUI_LOCAL_PRECISION_FLAGS)
    add_choice("comfyui_local_unet_dtype", COMFYUI_LOCAL_UNET_DTYPE_FLAGS)
    add_choice("comfyui_local_vae_dtype", COMFYUI_LOCAL_VAE_DTYPE_FLAGS)
    add_choice("comfyui_local_text_encoder_dtype", COMFYUI_LOCAL_TEXT_ENCODER_DTYPE_FLAGS)
    if settings.get("comfyui_local_cpu_vae"):
        args.append("--cpu-vae")
    add_choice("comfyui_local_attention_mode", COMFYUI_LOCAL_ATTENTION_FLAGS)
    add_choice("comfyui_local_upcast_attention", COMFYUI_LOCAL_UPCAST_ATTENTION_FLAGS)
    add_choice("comfyui_local_cuda_malloc", COMFYUI_LOCAL_CUDA_MALLOC_FLAGS)
    if settings.get("comfyui_local_disable_smart_memory"):
        args.append("--disable-smart-memory")
    if settings.get("comfyui_local_deterministic"):
        args.append("--deterministic")
    add_choice("comfyui_local_async_offload", COMFYUI_LOCAL_ASYNC_OFFLOAD_FLAGS)

    cache_mode = str(settings.get("comfyui_local_cache_mode") or "auto").strip().lower()
    if cache_mode == "lru":
        try:
            cache_lru = int(settings.get("comfyui_local_cache_lru") or 0)
        except Exception:
            cache_lru = 0
        args.extend(["--cache-lru", str(max(1, min(10000, cache_lru or 128)))])
    elif cache_mode in COMFYUI_LOCAL_CACHE_FLAGS:
        args.append(COMFYUI_LOCAL_CACHE_FLAGS[cache_mode])

    reserve_vram = str(settings.get("comfyui_local_reserve_vram_gb") or "").strip()
    if reserve_vram:
        try:
            reserve_value = float(reserve_vram)
        except Exception:
            reserve_value = None
        if reserve_value is not None and 0 <= reserve_value <= 128:
            args.extend(["--reserve-vram", ("%0.3f" % reserve_value).rstrip("0").rstrip(".")])
    return args

def _local_comfyui_state_path(port=None):
    safe_port = _configured_comfyui_port() if port is None else _configured_comfyui_port(f"http://localhost:{port}")
    return Path(tempfile.gettempdir()) / f"hackme_web_comfyui_local_{safe_port}.json"

def _write_local_comfyui_state(payload):
    try:
        path = _local_comfyui_state_path(payload.get("port"))
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    except Exception:
        return False
    return True

def _read_local_comfyui_state(port=None):
    path = _local_comfyui_state_path(port)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def _clear_local_comfyui_state(port=None):
    try:
        _local_comfyui_state_path(port).unlink(missing_ok=True)
    except Exception:
        pass

def _tail_text_lines(path, limit=8):
    if not path:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()][-limit:]

def _local_comfyui_runtime_status(port=None):
    state = _read_local_comfyui_state(port)
    if not state:
        return None
    pid = int(state.get("pid") or 0)
    if pid <= 0 or not _pid_exists(pid):
        return None
    log_lines = _tail_text_lines(state.get("log_path"))
    joined = "\n".join(log_lines)
    starting_markers = [
        "Starting server",
        "To see the GUI go to:",
        "FETCH ComfyRegistry Data:",
        "Checkpoint files will always be loaded safely.",
        "Using split optimization for attention",
    ]
    waiting_markers = [
        "FETCH ComfyRegistry Data:",
        "Import times for custom nodes:",
    ]
    starting = any(marker in joined for marker in starting_markers)
    waiting_on_registry = any(marker in joined for marker in waiting_markers)
    if waiting_on_registry:
        message = "ComfyUI 主程式已啟動，正在載入自訂節點 / Registry，API 尚未就緒"
    elif starting:
        message = "ComfyUI 主程式已啟動，正在初始化，API 尚未就緒"
    else:
        message = "ComfyUI 進程仍在執行，但 API 尚未回應"
    return {
        "pid": pid,
        "pgid": int(state.get("pgid") or 0),
        "port": int(state.get("port") or 0),
        "base_dir": state.get("base_dir") or "",
        "script": state.get("script") or "",
        "log_path": state.get("log_path") or "",
        "starting": starting,
        "waiting_on_registry": waiting_on_registry,
        "startup_log_tail": log_lines,
        "message": message,
    }

def _pid_exists(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False

def _pid_cmdline(pid):
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except Exception:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()

def _listener_pids_for_port(port):
    candidates = set()
    commands = [
        ["ss", "-ltnp", f"sport = :{int(port)}"],
        ["lsof", "-tiTCP:%s" % int(port), "-sTCP:LISTEN"],
    ]
    for command in commands:
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        except Exception:
            continue
        if result.returncode not in (0, 1):
            continue
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        for match in re.findall(r"pid=(\d+)", output):
            candidates.add(int(match))
        if command and command[0] == "lsof":
            for line in (result.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    candidates.add(int(line))
    return sorted(candidates)

def _proc_scan_comfyui_pids(*, port=None, base_dir=None, script=None):
    candidates = []
    port_token = str(int(port)) if port else ""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline = _pid_cmdline(pid)
        if not cmdline:
            continue
        lower = cmdline.lower()
        if "comfyui" not in lower and "main.py" not in lower:
            continue
        if port_token:
            if f"--port {port_token}" not in lower and f"--port={port_token}" not in lower and f":{port_token}" not in lower:
                continue
        if not _looks_like_comfyui_process(pid, base_dir=base_dir, script=script):
            continue
        candidates.append(pid)
    return sorted(set(candidates))

def _looks_like_comfyui_process(pid, *, base_dir=None, script=None):
    cmdline = _pid_cmdline(pid).lower()
    if not cmdline:
        return False
    if "comfyui" in cmdline:
        return True
    if "main.py" in cmdline and "python" in cmdline:
        return True
    if script and Path(str(script)).name.lower() in cmdline:
        return True
    if base_dir and str(base_dir).lower() in cmdline:
        return True
    return False

def _terminate_local_comfyui_targets(targets):
    killed = []
    failed = []
    for target in targets:
        pid = int(target.get("pid") or 0)
        if pid <= 0:
            continue
        pgid = int(target.get("pgid") or 0)
        try:
            if pgid > 0:
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            continue
        except Exception as exc:
            failed.append({"pid": pid, "error": str(exc)})
    deadline = time.time() + 8
    while time.time() < deadline:
        remaining = [pid for pid in killed if _pid_exists(pid)]
        if not remaining:
            break
        time.sleep(0.3)
    for pid in list(killed):
        if not _pid_exists(pid):
            continue
        try:
            pgid = os.getpgid(pid)
        except Exception:
            pgid = 0
        try:
            if pgid > 0:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except Exception as exc:
            failed.append({"pid": pid, "error": str(exc)})
    return {"killed_pids": sorted(set(killed)), "errors": failed}

def _stop_local_comfyui(actor):
    url = _configured_comfyui_url()
    port = _configured_comfyui_port(url)
    active_client = _client_for_url(url)
    mode = _configured_connection_mode()
    if mode != "local":
        return None, "只有本地模式可以從網頁停止 ComfyUI"
    state = _read_local_comfyui_state(port) or {}
    base = _configured_comfyui_base_dir(state.get("base_dir"))
    script, _ = _configured_local_start_script(state.get("script"), base_dir=str(base) if base else None)
    targets = []
    tracked_pid = int(state.get("pid") or 0)
    if tracked_pid > 0 and _pid_exists(tracked_pid):
        targets.append({"pid": tracked_pid, "pgid": int(state.get("pgid") or 0)})
    if not targets:
        for pid in _listener_pids_for_port(port):
            if _looks_like_comfyui_process(pid, base_dir=base, script=script):
                targets.append({"pid": pid})
    if not targets:
        for pid in _proc_scan_comfyui_pids(port=port, base_dir=base, script=script):
            targets.append({"pid": pid})
    if not targets:
        try:
            active_client.health_check(timeout=2)
        except Exception:
            _clear_local_comfyui_state(port)
            return {
                "stopped": False,
                "already_stopped": True,
                "port": port,
                "killed_pids": [],
            }, None
        return None, "找不到可停止的本地 ComfyUI 進程"
    result = _terminate_local_comfyui_targets(targets)
    time.sleep(0.5)
    try:
        active_client.health_check(timeout=2)
        audit(
            "COMFYUI_LOCAL_STOP_ERROR",
            get_client_ip(),
            user=_actor_value(actor, "username"),
            success=False,
            ua=get_ua(),
            detail=f"port={port}, pids={result['killed_pids']}, errors={result['errors']}",
        )
        return None, "ComfyUI 停止請求已送出，但服務仍在執行"
    except Exception:
        _clear_local_comfyui_state(port)
        audit(
            "COMFYUI_LOCAL_STOP",
            get_client_ip(),
            user=_actor_value(actor, "username"),
            success=True,
            ua=get_ua(),
            detail=f"port={port}, pids={result['killed_pids']}",
        )
        return {
            "stopped": True,
            "port": port,
            "killed_pids": result["killed_pids"],
            "errors": result["errors"],
        }, None

def _local_start_script_status(data):
    raw_script = str((data or {}).get("local_start_script") or (data or {}).get("comfyui_local_start_script") or "").strip()
    raw_base = str((data or {}).get("base_dir") or (data or {}).get("comfyui_base_dir") or "").strip()
    if not raw_script:
        raw_script = str((get_system_settings() or {}).get("comfyui_local_start_script") or "").strip()
    script, msg = _configured_local_start_script(raw_script, base_dir=raw_base or None)
    status = {
        "configured": bool(raw_script),
        "exists": bool(script),
        "syntax_ok": None,
        "message": msg or "",
    }
    if script:
        status["filename"] = script.name
        status["relative_path"] = script.relative_to(_configured_comfyui_base_dir(raw_base or None)).as_posix()
        if script.suffix.lower() == ".sh":
            try:
                check = subprocess.run(["bash", "-n", str(script)], cwd=str(script.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
                status["syntax_ok"] = check.returncode == 0
                if check.returncode != 0:
                    status["message"] = (check.stderr or check.stdout or "啟動腳本語法檢查失敗")[:400]
            except Exception as exc:
                status["syntax_ok"] = False
                status["message"] = str(exc)[:400]
    return status

def _start_local_comfyui(actor, *, wait_seconds=2, data=None):
    url, endpoint, endpoint_msg = _parse_comfyui_endpoint(data or {})
    mode = endpoint.get("mode") if isinstance(endpoint, dict) else _configured_connection_mode()
    if mode != "local":
        return None, "只有本地模式可以從網頁啟動 ComfyUI"
    if injected_client is not None:
        return {"started": False, "already_running": True, "testing": True}, None
    if endpoint_msg:
        return None, endpoint_msg
    active_client = _client_for_url(url or _configured_comfyui_url())
    try:
        active_client.health_check(timeout=2)
        return {"started": False, "already_running": True, "comfyui_url": getattr(active_client, "base_url", url or _configured_comfyui_url())}, None
    except Exception:
        pass
    raw_base = (data or {}).get("base_dir") or (data or {}).get("comfyui_base_dir")
    raw_script = (data or {}).get("local_start_script") or (data or {}).get("comfyui_local_start_script")
    script, msg = _configured_local_start_script(raw_script, base_dir=raw_base or None)
    if msg or not script:
        audit("COMFYUI_LOCAL_AUTOSTART_SKIPPED", get_client_ip(), user=_actor_value(actor, "username"), success=False, ua=get_ua(), detail=msg or "no script configured")
        return None, msg or "尚未設定 ComfyUI 本地啟動腳本"
    base = _configured_comfyui_base_dir(raw_base or None)
    project_dir = _configured_comfyui_project_dir(raw_base or None)
    local_settings = dict(_live_comfyui_settings() or {})
    local_settings.update({
        key: value for key, value in (data or {}).items()
        if str(key).startswith("comfyui_local_")
    })
    extra_args = _build_local_comfyui_main_args(local_settings)
    command = [str(script), *extra_args]
    if script.suffix.lower() == ".sh":
        command = ["bash", str(script), *extra_args]
    env = os.environ.copy()
    try:
        configured_port = urlparse(url or _configured_comfyui_url()).port or DEFAULT_COMFYUI_PORT
    except Exception:
        configured_port = DEFAULT_COMFYUI_PORT
    env.update({
        "PORT": str(configured_port),
        "AUTO_PORT_SCAN": "0",
        "COMFYUI_DIR": str(project_dir or base),
        "COMFYUI_EXTRA_ARGS": shlex.join(extra_args),
        "COMFYUI_EXTRA_ARGS_JSON": json.dumps(extra_args, ensure_ascii=False),
    })
    start_log = None
    try:
        log_fd, log_path = tempfile.mkstemp(prefix="comfyui_local_start_", suffix=".log")
        os.close(log_fd)
        start_log = Path(log_path)
        with open(start_log, "ab") as log_handle:
            proc = subprocess.Popen(
                command,
                cwd=str(base),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            proc_pgid = int(os.getpgid(proc.pid))
        except Exception:
            proc_pgid = 0
        _write_local_comfyui_state({
            "pid": int(proc.pid),
            "pgid": proc_pgid,
            "port": int(configured_port),
            "base_dir": str(base),
            "script": str(script),
            "extra_args": extra_args,
            "log_path": str(start_log) if start_log else "",
            "started_at": datetime.now().isoformat(),
        })
        time.sleep(1)
        return_code = proc.poll()
        if return_code not in (None, 0):
            _clear_local_comfyui_state(configured_port)
            detail = ""
            try:
                lines = start_log.read_text(encoding="utf-8", errors="ignore").splitlines()
                if lines:
                    detail = "；" + " / ".join(line.strip() for line in lines[-6:] if line.strip())[:500]
            except Exception:
                pass
            msg = f"本地 ComfyUI 啟動腳本已結束（exit {return_code}）{detail}"
            audit("COMFYUI_LOCAL_AUTOSTART_ERROR", get_client_ip(), user=_actor_value(actor, "username"), success=False, ua=get_ua(), detail=msg[:180])
            return None, msg
        audit("COMFYUI_LOCAL_AUTOSTART", get_client_ip(), user=_actor_value(actor, "username"), success=True, ua=get_ua(), detail=f"script={script.name}, args={shlex.join(extra_args)[:120]}")
        deadline = time.time() + max(0, int(wait_seconds or 0))
        while time.time() < deadline:
            try:
                active_client.health_check(timeout=2)
                return {"started": True, "available": True, "comfyui_url": getattr(active_client, "base_url", url or _configured_comfyui_url())}, None
            except Exception:
                time.sleep(1)
        return {
            "started": True,
            "available": False,
            "comfyui_url": getattr(active_client, "base_url", url or _configured_comfyui_url()),
            "message": "已啟動背景流程；若是第一次安裝依賴，可能需要數分鐘，稍後請按重新整理模型。",
            "startup_log_tail": (
                start_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-8:]
                if start_log and start_log.exists()
                else []
            ),
        }, None
    except Exception as exc:
        audit("COMFYUI_LOCAL_AUTOSTART_ERROR", get_client_ip(), user=_actor_value(actor, "username"), success=False, ua=get_ua(), detail=str(exc)[:180])
        return None, str(exc)

def _configured_max_batch_size():
    settings = get_system_settings() or {}
    return _int_range(settings.get("comfyui_max_batch_size"), 1, 1, 8)

def _configured_default_dimensions():
    settings = get_system_settings() or {}
    return {
        "width": _int_range(settings.get("comfyui_default_width"), 1024, 64, 2048, multiple_of=8),
        "height": _int_range(settings.get("comfyui_default_height"), 1024, 64, 2048, multiple_of=8),
    }

def _configured_comfyui_base_dir(value=None):
    raw = str(value or (get_system_settings() or {}).get("comfyui_base_dir") or os.environ.get("COMFYUI_BASE_DIR") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        return path.resolve()
    except Exception:
        return None

def _configured_comfyui_project_dir(value=None):
    base = _configured_comfyui_base_dir(value)
    if not base:
        return None
    direct = (base / "main.py").resolve()
    nested = (base / "ComfyUI" / "main.py").resolve()
    if direct.exists():
        return base
    if nested.exists():
        return nested.parent
    return base

def _configured_civitai_api_key():
    return str((get_system_settings() or {}).get("comfyui_civitai_api_key") or os.environ.get("CIVITAI_API_KEY") or "").strip()

def _public_download_host(url):
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None, "下載網址只支援 http/https"
    if parsed.username or parsed.password:
        return None, "下載網址不可包含帳密"
    try:
        resolved = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None, "下載網址無法解析主機"
    for item in resolved:
        ip_text = item[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            return None, "下載網址解析到不合法 IP"
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return None, "下載網址不可指向 localhost、內網或保留位址"
    return parsed, None

def _safe_model_filename(url, fallback):
    parsed = urlparse(str(url or ""))
    name = Path(parsed.path or "").name or fallback
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    if not name:
        name = fallback
    suffix = Path(name).suffix.lower()
    if suffix not in COMFYUI_MODEL_DOWNLOAD_EXTENSIONS:
        raise ValueError(f"模型副檔名必須是 {', '.join(sorted(COMFYUI_MODEL_DOWNLOAD_EXTENSIONS))}")
    return name[:180]

def _normalize_download_model_type(value):
    key = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    return CIVITAI_MODEL_TYPE_TO_DOWNLOAD_TYPE.get(key, key)

def _filename_from_content_disposition(header_value):
    header = str(header_value or "").strip()
    if not header:
        return ""
    match = re.search(r'filename\*=UTF-8\'\'([^;]+)', header, re.IGNORECASE)
    if match:
        return unquote(match.group(1)).strip().strip('"')
    match = re.search(r'filename="?([^";]+)"?', header, re.IGNORECASE)
    return (match.group(1).strip() if match else "")

def _append_civitai_token(url, auth_value):
    if not auth_value:
        return str(url or "")
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(url or "")
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["token"] = [auth_value]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

def _civitai_headers(auth_value):
    headers = {
        "User-Agent": "hackme_web-comfyui-model-downloader/1.0",
        "Accept": "application/json",
    }
    if auth_value:
        headers["Authorization"] = f"Bearer {auth_value}"
    return headers

def _comfyui_model_sidecar_path(*, model_type, filename, base_dir=None):
    return _comfyui_model_sidecar_path_with_relative(model_type=model_type, filename=filename, base_dir=base_dir)

def _normalize_model_relative_dir(value):
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("/") or raw.startswith("../") or raw == "..":
        raise ValueError("模型相對路徑必須位於 ComfyUI/models/ 之下")
    parts = []
    for part in raw.split("/"):
        part = part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("模型相對路徑不允許 ..")
        parts.append(part)
    return "/".join(parts)

def _split_model_relative_name(value):
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in raw.split("/"):
        return "", raw
    if "/" not in raw:
        return "", raw
    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if not parts:
        return "", ""
    return "/".join(parts[:-1]), parts[-1]

def _resolve_model_destination_dir(*, model_type, base_dir=None, relative_dir=None):
    normalized_type = _normalize_download_model_type(model_type)
    mapping = COMFYUI_MODEL_DOWNLOAD_TYPES.get(normalized_type)
    if not mapping:
        return None, None, None, "模型類型不支援"
    base = _configured_comfyui_base_dir(base_dir)
    if not base:
        return None, None, None, "請先設定 COMFYUI_BASE_DIR 或在本面板輸入 ComfyUI 專案資料夾"
    project_dir = _configured_comfyui_project_dir(base_dir) or base
    models_root = (project_dir / "models").resolve()
    default_relative_dir, label = mapping
    try:
        safe_relative_dir = _normalize_model_relative_dir(relative_dir) or default_relative_dir
    except ValueError as exc:
        return None, None, None, str(exc)
    destination_dir = (models_root / safe_relative_dir).resolve()
    try:
        destination_dir.relative_to(models_root)
    except ValueError:
        return None, None, None, "模型相對路徑超出 ComfyUI/models 範圍"
    return destination_dir, safe_relative_dir, label, None

def _comfyui_model_sidecar_path_with_relative(*, model_type, filename, base_dir=None, relative_dir=None):
    safe_name = str(filename or "").strip()
    if not safe_name or "/" in safe_name or "\\" in safe_name or ".." in safe_name:
        return None
    model_dir, _relative_dir, _label, msg = _resolve_model_destination_dir(
        model_type=model_type,
        base_dir=base_dir,
        relative_dir=relative_dir,
    )
    if msg or not model_dir:
        return None
    sidecar = (model_dir / f"{safe_name}.civitai.json").resolve()
    try:
        sidecar.relative_to(model_dir)
    except ValueError:
        return None
    return sidecar

def _write_comfyui_model_sidecar(*, model_type, filename, base_dir=None, relative_dir=None, payload=None):
    sidecar = _comfyui_model_sidecar_path_with_relative(
        model_type=model_type,
        filename=filename,
        base_dir=base_dir,
        relative_dir=relative_dir,
    )
    if not sidecar:
        return False
    data = payload if isinstance(payload, dict) else {}
    try:
        sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return False
    return True

def _read_comfyui_model_sidecar(*, model_type, filename, base_dir=None, relative_dir=None):
    sidecar = _comfyui_model_sidecar_path_with_relative(
        model_type=model_type,
        filename=filename,
        base_dir=base_dir,
        relative_dir=relative_dir,
    )
    if not sidecar or not sidecar.exists():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def _normalize_lora_base_model_family(value):
    raw = str(value or "").strip()
    normalized = re.sub(r"\s+", " ", raw).lower()
    if not normalized:
        return raw, "unknown"
    if "pony" in normalized:
        return raw, "pony"
    if "illustrious" in normalized:
        return raw, "illustrious"
    if "noob" in normalized:
        return raw, "noob"
    if "sdxl" in normalized or "sd xl" in normalized:
        return raw, "sdxl"
    if "flux" in normalized:
        return raw, "flux"
    if (
        "sd1.5" in normalized
        or "sd 1.5" in normalized
        or "sd15" in normalized
        or "stable diffusion 1.5" in normalized
        or "v1-5" in normalized
    ):
        return raw, "sd15"
    return raw, "other"

def _lora_support_payload(base_model):
    raw_base_model, family = _normalize_lora_base_model_family(base_model)
    supported = family in COMFYUI_SUPPORTED_LORA_BASE_MODEL_FAMILIES
    if supported:
        support_message = "目前生圖介面支援這個 LoRA base model。"
    elif raw_base_model:
        support_message = (
            f"{raw_base_model} LoRA 目前不支援；"
            "目前只允許 SDXL、Pony、Illustrious、Noob 系列 LoRA。"
        )
    else:
        support_message = (
            "這個 LoRA 沒有可辨識的 base model metadata；"
            "目前只允許 SDXL、Pony、Illustrious、Noob 系列 LoRA。"
        )
    return {
        "base_model": raw_base_model,
        "base_model_family": family,
        "supported": supported,
        "support_message": support_message,
    }

def _build_lora_details(lora_names, *, base_dir=None):
    details = {}
    for name in list(lora_names or []):
        clean_name = str(name or "").strip()
        if not clean_name:
            continue
        relative_dir, filename = _split_model_relative_name(clean_name)
        meta = _read_comfyui_model_sidecar(model_type="lora", filename=filename, base_dir=base_dir, relative_dir=relative_dir)
        support = _lora_support_payload(meta.get("base_model"))
        details[clean_name] = {
            "name": clean_name,
            "trained_words": [
                str(item).strip()
                for item in list(meta.get("trained_words") or [])
                if str(item).strip()
            ],
            "source": str(meta.get("source") or "").strip(),
            "version_name": str(meta.get("version_name") or "").strip(),
            **support,
        }
    return details

def _public_or_civitai_host(url, *, allow_civitai_only=False):
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if allow_civitai_only:
        if parsed.scheme != "https" or host not in CIVITAI_ALLOWED_HOSTS:
            return None, "只接受 Civitai 模型頁網址"
        return parsed, None
    return _public_download_host(url)

def _civitai_site_from_host(host):
    clean_host = str(host or "").strip().lower()
    if clean_host.startswith("www."):
        clean_host = clean_host[4:]
    if clean_host in {"civitai.com", "civitai.red", "civitai.green"}:
        return clean_host
    return "civitai.com"

def _civitai_site_from_api_base(api_base):
    parsed = urlparse(str(api_base or ""))
    return _civitai_site_from_host(parsed.hostname)

def _civitai_api_base_for_site(source_site):
    site = _civitai_site_from_host(source_site)
    for base in list(CIVITAI_API_BASES or []):
        if _civitai_site_from_api_base(base) == site:
            return str(base).rstrip("/")
    return f"https://{site}/api/v1"

def _parse_civitai_reference(page_url):
    parsed, msg = _public_or_civitai_host(page_url, allow_civitai_only=True)
    if msg:
        return None, msg
    path_match = re.search(r"/models/(\d+)", parsed.path or "", re.IGNORECASE)
    if not path_match:
        return None, "無法從網址解析 Civitai modelId"
    query = parse_qs(parsed.query or "")
    version_id = None
    if query.get("modelVersionId"):
        raw = str(query.get("modelVersionId")[0] or "").strip()
        if raw.isdigit():
            version_id = int(raw)
    return {
        "page_url": urlunparse(parsed._replace(fragment="")),
        "model_id": int(path_match.group(1)),
        "version_id": version_id,
        "source_site": _civitai_site_from_host(parsed.hostname),
    }, None

def _fetch_json(url, *, headers=None, timeout=20):
    request_obj = urllib.request.Request(str(url), headers=headers or {"User-Agent": "hackme_web/1.0"})
    with urllib.request.urlopen(request_obj, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return json.loads(resp.read().decode(charset, errors="replace"))

def _civitai_api_get(path, *, auth_value, api_base=None):
    if not auth_value:
        return None, "請先在 root 設定填入 Civitai API Key"
    base_url = str(api_base or CIVITAI_API_BASE).rstrip("/")
    url = f"{base_url}/{path.lstrip('/')}"
    try:
        return _fetch_json(url, headers=_civitai_headers(auth_value), timeout=20), None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            detail = ""
        return None, f"Civitai API 失敗：HTTP {exc.code}{f' {detail}' if detail else ''}"
    except urllib.error.URLError as exc:
        return None, f"Civitai API 連線失敗：{getattr(exc, 'reason', exc)}"
    except Exception as exc:
        return None, str(exc)

def _normalize_civitai_search_type(value):
    key = _normalize_download_model_type(value)
    return key if key in CIVITAI_SEARCH_TYPE_TO_API else ""

def _normalize_civitai_nsfw_mode(value):
    raw = str(value or "safe").strip().lower()
    if raw in {"safe", "all", "nsfw"}:
        return raw
    return "safe"

def _serialize_civitai_file(file_entry, fallback_download_url):
    if not isinstance(file_entry, dict):
        return None
    filename = str(file_entry.get("name") or "").strip()
    if not filename:
        return None
    suffix = Path(filename).suffix.lower()
    if suffix not in COMFYUI_MODEL_DOWNLOAD_EXTENSIONS:
        return None
    try:
        size_kb = float(file_entry.get("sizeKB")) if file_entry.get("sizeKB") is not None else None
    except Exception:
        size_kb = None
    return {
        "id": int(file_entry.get("id") or 0) or None,
        "name": filename,
        "size_kb": size_kb,
        "size_bytes": int(round(size_kb * 1024)) if size_kb is not None else None,
        "download_url": str(file_entry.get("downloadUrl") or fallback_download_url or "").strip(),
        "metadata": dict(file_entry.get("metadata") or {}),
        "hashes": {
            str(key).strip().lower(): str(value).strip()
            for key, value in dict(file_entry.get("hashes") or {}).items()
            if str(key).strip() and str(value).strip()
        },
        "pickle_scan_result": file_entry.get("pickleScanResult"),
        "virus_scan_result": file_entry.get("virusScanResult"),
        "type": str(file_entry.get("type") or "").strip(),
    }

def _safe_civitai_media_url(value):
    url = str(value or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    hostname = (parsed.hostname or "").lower()
    allowed_hosts = {
        "image.civitai.com",
        "image.civitai.red",
        "image.civitai.green",
    }
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or hostname not in allowed_hosts
    ):
        return ""
    return urlunparse(parsed)

def _civitai_media_proxy_url(media_url):
    safe_url = _safe_civitai_media_url(media_url)
    if not safe_url:
        return ""
    return f"/api/root/comfyui/civitai/media?url={quote(safe_url, safe='')}"

def _fetch_civitai_media(media_url, *, max_bytes=5 * 1024 * 1024):
    safe_url = _safe_civitai_media_url(media_url)
    if not safe_url:
        return None, None, "Civitai 縮圖網址不安全或不支援"
    request_obj = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": "hackme_web-civitai-preview/1.0",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=12) as resp:
            content_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}:
                return None, None, "Civitai 縮圖不是允許的圖片格式"
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        return None, None, f"Civitai 縮圖讀取失敗：HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, None, f"Civitai 縮圖連線失敗：{getattr(exc, 'reason', exc)}"
    except Exception as exc:
        return None, None, f"Civitai 縮圖讀取失敗：{exc}"
    if len(data) > max_bytes:
        return None, None, "Civitai 縮圖超過大小限制"
    return data, content_type, None

def _serialize_civitai_image(image_entry):
    if not isinstance(image_entry, dict):
        return None
    image_url = _safe_civitai_media_url(image_entry.get("url"))
    if not image_url:
        return None
    def _positive_int(value):
        try:
            number = int(value)
        except Exception:
            return None
        return number if number > 0 else None
    return {
        "url": image_url,
        "proxy_url": _civitai_media_proxy_url(image_url),
        "nsfw": image_entry.get("nsfw"),
        "width": _positive_int(image_entry.get("width")),
        "height": _positive_int(image_entry.get("height")),
        "hash": str(image_entry.get("hash") or "").strip(),
    }

def _serialize_civitai_versions(model_data, preferred_version_id=None):
    versions = []
    for version in list((model_data or {}).get("modelVersions") or []):
        version_id = int(version.get("id") or 0) or None
        files = []
        for file_entry in list(version.get("files") or []):
            payload = _serialize_civitai_file(file_entry, version.get("downloadUrl"))
            if payload:
                files.append(payload)
        if not files:
            continue
        images = []
        for image_entry in list(version.get("images") or []):
            payload = _serialize_civitai_image(image_entry)
            if payload:
                images.append(payload)
        versions.append({
            "id": version_id,
            "name": str(version.get("name") or f"Version {version_id or '?'}").strip(),
            "created_at": version.get("createdAt"),
            "base_model": version.get("baseModel"),
            "trained_words": list(version.get("trainedWords") or []),
            "download_url": str(version.get("downloadUrl") or "").strip(),
            "files": files,
            "images": images[:6],
            "thumbnail_url": images[0]["url"] if images else "",
        })
    selected_version_id = None
    if preferred_version_id:
        for item in versions:
            if item["id"] == int(preferred_version_id):
                selected_version_id = item["id"]
                break
    if selected_version_id is None and versions:
        selected_version_id = versions[0]["id"]
    return versions, selected_version_id

def _build_civitai_page_url(model_id, version_id=None, *, source_site="civitai.com"):
    site = _civitai_site_from_host(source_site)
    base_url = f"https://{site}/models/{int(model_id)}"
    if version_id:
        return f"{base_url}?modelVersionId={int(version_id)}"
    return base_url

def _serialize_civitai_search_results(search_data, *, source_site="civitai.com"):
    results = []
    source_site = _civitai_site_from_host(source_site)
    items = list((search_data or {}).get("items") or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = int(item.get("id") or 0) or None
        if not model_id:
            continue
        versions, selected_version_id = _serialize_civitai_versions(item)
        if not versions:
            continue
        compatible_models = []
        for version in versions:
            base_model = str(version.get("base_model") or "").strip()
            if base_model and base_model not in compatible_models:
                compatible_models.append(base_model)
        latest_version = versions[0]
        primary_file = dict((latest_version.get("files") or [None])[0] or {})
        thumbnail_url = str(latest_version.get("thumbnail_url") or "").strip()
        suggested_model_type = _normalize_download_model_type(item.get("type"))
        if suggested_model_type not in COMFYUI_MODEL_DOWNLOAD_TYPES:
            suggested_model_type = "checkpoint"
        thumbnail_proxy_url = _civitai_media_proxy_url(thumbnail_url)
        results.append({
            "model_id": model_id,
            "source_site": source_site,
            "source_label": source_site,
            "select_key": f"{source_site}:{model_id}",
            "page_url": _build_civitai_page_url(model_id, source_site=source_site),
            "selected_page_url": _build_civitai_page_url(model_id, selected_version_id, source_site=source_site),
            "name": str(item.get("name") or f"Model {model_id}").strip(),
            "type": str(item.get("type") or "").strip(),
            "suggested_model_type": suggested_model_type,
            "creator": str(((item.get("creator") or {}).get("username") or "")).strip(),
            "nsfw": bool(item.get("nsfw")),
            "thumbnail_url": thumbnail_url,
            "thumbnail_proxy_url": thumbnail_proxy_url,
            "version_count": len(versions),
            "compatible_models": compatible_models,
            "selected_version_id": selected_version_id,
            "latest_version": {
                "id": latest_version.get("id"),
                "name": latest_version.get("name"),
                "created_at": latest_version.get("created_at"),
                "base_model": latest_version.get("base_model"),
                "trained_words": list(latest_version.get("trained_words") or []),
                "file_count": len(latest_version.get("files") or []),
                "primary_file": primary_file or None,
                "thumbnail_url": thumbnail_url,
                "thumbnail_proxy_url": thumbnail_proxy_url,
                "images": list(latest_version.get("images") or []),
            },
        })
    metadata = dict((search_data or {}).get("metadata") or {})
    return {
        "results": results,
        "total_items": int(metadata.get("totalItems") or 0) if str(metadata.get("totalItems") or "").isdigit() else len(results),
        "current_page": int(metadata.get("currentPage") or 1) if str(metadata.get("currentPage") or "").isdigit() else 1,
        "page_size": int(metadata.get("pageSize") or len(results) or 0) if str(metadata.get("pageSize") or "").isdigit() else len(results),
    }

def _search_civitai_models(query="", *, base_model="", model_type="", nsfw_mode="safe", limit=12):
    safe_query = str(query or "").strip()[:120]
    safe_base_model = str(base_model or "").strip()[:80]
    safe_model_type = _normalize_civitai_search_type(model_type)
    safe_nsfw_mode = _normalize_civitai_nsfw_mode(nsfw_mode)
    try:
        safe_limit = max(1, min(24, int(limit or 12)))
    except Exception:
        safe_limit = 12
    params = [("limit", safe_limit)]
    if safe_query:
        params.append(("query", safe_query))
    if safe_model_type:
        params.append(("types", CIVITAI_SEARCH_TYPE_TO_API[safe_model_type]))
    if safe_base_model:
        params.append(("baseModels", safe_base_model))
    if safe_nsfw_mode == "safe":
        params.append(("nsfw", "false"))
    elif safe_nsfw_mode == "nsfw":
        params.append(("nsfw", "true"))
    auth_value = _configured_civitai_api_key()
    path = f"models?{urlencode(params, doseq=True)}"
    results = []
    source_errors = []
    source_payloads = []
    for api_base in list(CIVITAI_API_BASES or [CIVITAI_API_BASE]):
        source_site = _civitai_site_from_api_base(api_base)
        search_data, err = _civitai_api_get(path, auth_value=auth_value, api_base=api_base)
        if err:
            source_errors.append({"source_site": source_site, "error": err})
            continue
        payload_part = _serialize_civitai_search_results(search_data, source_site=source_site)
        source_payloads.append({
            "source_site": source_site,
            "total_items": payload_part.get("total_items") or 0,
            "current_page": payload_part.get("current_page") or 1,
            "page_size": payload_part.get("page_size") or 0,
        })
        results.extend(payload_part.get("results") or [])
    if not source_payloads and source_errors:
        joined = "；".join(f"{item['source_site']}: {item['error']}" for item in source_errors[:3])
        return None, f"Civitai API 失敗：{joined}"
    payload = {
        "results": results,
        "total_items": sum(int(item.get("total_items") or 0) for item in source_payloads) or len(results),
        "current_page": 1,
        "page_size": sum(int(item.get("page_size") or 0) for item in source_payloads) or len(results),
        "search_sources": source_payloads,
        "source_errors": source_errors,
    }
    if safe_model_type:
        payload["results"] = [
            item for item in payload.get("results", [])
            if item.get("suggested_model_type") == safe_model_type
        ]
        payload["total_items"] = len(payload["results"])
    payload["filters"] = {
        "query": safe_query,
        "base_model": safe_base_model,
        "model_type": safe_model_type,
        "nsfw_mode": safe_nsfw_mode,
        "limit": safe_limit,
    }
    return payload, None

def _inspect_civitai_model(page_url):
    ref, msg = _parse_civitai_reference(page_url)
    if msg:
        return None, msg
    api_base = _civitai_api_base_for_site(ref.get("source_site"))
    model_data, err = _civitai_api_get(f"models/{ref['model_id']}", auth_value=_configured_civitai_api_key(), api_base=api_base)
    if err:
        return None, err
    versions, selected_version_id = _serialize_civitai_versions(model_data, preferred_version_id=ref.get("version_id"))
    if not versions:
        return None, "這個模型目前沒有可下載的版本或檔案"
    model_type = _normalize_download_model_type((model_data or {}).get("type"))
    if model_type not in COMFYUI_MODEL_DOWNLOAD_TYPES:
        model_type = "checkpoint"
    return {
        "page_url": ref["page_url"],
        "model_id": ref["model_id"],
        "source_site": ref.get("source_site") or "civitai.com",
        "source_label": ref.get("source_site") or "civitai.com",
        "name": str((model_data or {}).get("name") or f"Model {ref['model_id']}").strip(),
        "type": str((model_data or {}).get("type") or "").strip(),
        "suggested_model_type": model_type,
        "creator": ((model_data or {}).get("creator") or {}).get("username") or "",
        "nsfw": bool((model_data or {}).get("nsfw")),
        "selected_version_id": selected_version_id,
        "versions": versions,
    }, None

def _create_model_download_job(actor):
    job_id = secrets.token_hex(12)
    job = {
        "job_id": job_id,
        "owner_user_id": _generation_owner_id(actor),
        "owner_username": _actor_value(actor, "username", ""),
        "status": "queued",
        "error": "",
        "result": None,
        "progress": {
            "phase": "queued",
            "percent": 0,
            "bytes_written": 0,
            "total_bytes": 0,
            "detail": "已建立模型下載工作",
            "completed": False,
            "updated_at": time.time(),
        },
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with model_download_jobs_lock:
        model_download_jobs[job_id] = job
    return job_id

def _update_model_download_job(job_id, **changes):
    with model_download_jobs_lock:
        job = model_download_jobs.get(job_id)
        if not job:
            return None
        for key, value in changes.items():
            job[key] = value
        job["updated_at"] = time.time()
        return dict(job)

def _update_model_download_progress(job_id, progress):
    with model_download_jobs_lock:
        job = model_download_jobs.get(job_id)
        if not job:
            return None
        job["progress"] = {
            **(job.get("progress") or {}),
            **(progress or {}),
            "updated_at": time.time(),
        }
        if job["status"] in {"queued", "running"}:
            job["status"] = "running"
        job["updated_at"] = time.time()
        return dict(job["progress"])

def _get_model_download_job(job_id):
    with model_download_jobs_lock:
        job = model_download_jobs.get(str(job_id))
        return dict(job) if job else None

def _assert_model_download_job_owner(job_id, actor):
    job = _get_model_download_job(job_id)
    if not job:
        return None, json_resp({"ok": False, "msg": "找不到 ComfyUI 模型下載工作"}, 404)
    if int(job.get("owner_user_id") or 0) != int(_generation_owner_id(actor) or 0):
        return None, json_resp({"ok": False, "msg": "無權查看此 ComfyUI 模型下載工作"}, 403)
    return job, None

def _parse_civitai_download_request(data):
    page_url = str(data.get("page_url") or data.get("url") or "").strip()
    try:
        version_id = int(data.get("version_id") or data.get("model_version_id") or 0)
    except Exception:
        version_id = 0
    try:
        file_id = int(data.get("file_id") or 0) or None
    except Exception:
        file_id = None
    model_type = str(data.get("type") or data.get("model_type") or "").strip().lower()
    if not page_url or version_id <= 0:
        return None, "請先輸入 Civitai 模型頁網址並選擇版本"
    return {
        "page_url": page_url,
        "version_id": version_id,
        "file_id": file_id,
        "model_type": model_type,
        "base_dir": data.get("base_dir"),
        "relative_dir": data.get("relative_dir") or data.get("model_relative_path") or "",
    }, None

def _download_comfyui_model_file(*, url, model_type, base_dir, relative_dir=None, filename_hint=None, auth_value=None, progress_callback=None):
    parsed, msg = _public_or_civitai_host(url)
    if msg:
        return None, msg
    model_type = _normalize_download_model_type(model_type)
    if model_type not in COMFYUI_MODEL_DOWNLOAD_TYPES:
        return None, "模型類型不支援"
    destination_dir, effective_relative_dir, label, msg = _resolve_model_destination_dir(
        model_type=model_type,
        base_dir=base_dir,
        relative_dir=relative_dir,
    )
    if msg:
        return None, msg
    try:
        filename = _safe_model_filename(filename_hint or url, f"downloaded_{model_type}.safetensors")
    except ValueError as exc:
        return None, str(exc)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = (destination_dir / filename).resolve()
    try:
        destination.relative_to(destination_dir)
    except ValueError:
        return None, "模型檔名不合法"
    if destination.exists():
        return None, f"{label} 檔案已存在：{filename}"
    request_obj = urllib.request.Request(
        _append_civitai_token(str(url), auth_value),
        headers=_civitai_headers(auth_value),
    )
    written = 0
    temp_path = None
    try:
        with urllib.request.urlopen(request_obj, timeout=30) as resp:
            try:
                total_bytes = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                total_bytes = 0
            final_url = resp.geturl()
            _final_parsed, final_msg = _public_or_civitai_host(final_url)
            if final_msg:
                return None, final_msg
            content_name = _filename_from_content_disposition(resp.headers.get("Content-Disposition"))
            if content_name:
                filename = _safe_model_filename(content_name, filename)
                destination = (destination_dir / filename).resolve()
                if destination.exists():
                    return None, f"{label} 檔案已存在：{filename}"
            if progress_callback:
                progress_callback({
                    "phase": "downloading",
                    "percent": 0,
                    "bytes_written": 0,
                    "total_bytes": total_bytes,
                    "detail": f"開始下載 {label}：{filename}",
                    "completed": False,
                })
            with tempfile.NamedTemporaryFile(prefix=f".{filename}.", suffix=".part", dir=str(destination_dir), delete=False) as tmp:
                temp_path = Path(tmp.name)
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_COMFYUI_MODEL_DOWNLOAD_BYTES:
                        raise ValueError("模型檔案超過下載大小上限")
                    tmp.write(chunk)
                    if progress_callback:
                        percent = 0
                        if total_bytes > 0:
                            percent = max(0, min(99, round((written / total_bytes) * 100)))
                        progress_callback({
                            "phase": "downloading",
                            "percent": percent,
                            "bytes_written": written,
                            "total_bytes": total_bytes,
                            "detail": f"正在下載 {label}：{filename}",
                            "completed": False,
                        })
        if written <= 0:
            raise ValueError("下載內容為空")
        temp_path.replace(destination)
    except urllib.error.URLError as exc:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return None, f"模型下載中斷或連線失敗：{getattr(exc, 'reason', exc)}"
    except Exception as exc:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        if isinstance(exc, TimeoutError):
            return None, "模型下載逾時，請稍後再試"
        if exc.__class__.__name__ == "IncompleteRead":
            return None, "模型下載中斷，請稍後再試"
        return None, str(exc)
    return {
        "type": model_type,
        "label": label,
        "filename": filename,
        "size_bytes": written,
        "relative_dir": effective_relative_dir,
        "saved_path": str(destination),
    }, None

def _download_civitai_model_selection(*, page_url, version_id, file_id, model_type, base_dir, relative_dir=None, progress_callback=None):
    inspection, msg = _inspect_civitai_model(page_url)
    if msg:
        return None, msg
    chosen_version = None
    for version in inspection["versions"]:
        if version["id"] == int(version_id or 0):
            chosen_version = version
            break
    if not chosen_version:
        return None, "找不到指定版本"
    chosen_file = None
    if file_id:
        for file_payload in chosen_version["files"]:
            if file_payload.get("id") == int(file_id):
                chosen_file = file_payload
                break
        if not chosen_file:
            return None, "找不到指定檔案"
    else:
        chosen_file = chosen_version["files"][0]
    download_url = str(chosen_file.get("download_url") or chosen_version.get("download_url") or "").strip()
    if not download_url:
        download_url = f"https://civitai.com/api/download/models/{chosen_version['id']}"
    result, err = _download_comfyui_model_file(
        url=download_url,
        model_type=model_type or inspection.get("suggested_model_type"),
        base_dir=base_dir,
        relative_dir=relative_dir,
        filename_hint=chosen_file.get("name"),
        auth_value=_configured_civitai_api_key(),
        progress_callback=progress_callback,
    )
    if err:
        return None, err
    result["civitai"] = {
        "model_id": inspection["model_id"],
        "model_name": inspection["name"],
        "version_id": chosen_version["id"],
        "version_name": chosen_version["name"],
        "base_model": str(chosen_version.get("base_model") or "").strip(),
        "trained_words": list(chosen_version.get("trained_words") or []),
        "file_id": chosen_file.get("id"),
        "file_name": chosen_file.get("name"),
        "source_url": inspection["page_url"],
    }
    _write_comfyui_model_sidecar(
        model_type=result.get("type") or model_type or inspection.get("suggested_model_type"),
        filename=result.get("filename"),
        base_dir=base_dir,
        relative_dir=result.get("relative_dir"),
        payload={
            "source": "civitai",
            "model_id": inspection["model_id"],
            "model_name": inspection["name"],
            "version_id": chosen_version["id"],
            "version_name": chosen_version["name"],
            "base_model": str(chosen_version.get("base_model") or "").strip(),
            "file_id": chosen_file.get("id"),
            "file_name": chosen_file.get("name"),
            "trained_words": list(chosen_version.get("trained_words") or []),
            "source_url": inspection["page_url"],
            "saved_filename": result.get("filename"),
            "relative_dir": result.get("relative_dir") or "",
        },
    )
    return result, None

def _upload_comfyui_model_file(*, uploaded_file, model_type, base_dir, relative_dir=None, actor=None):
    model_type = _normalize_download_model_type(model_type)
    if model_type not in COMFYUI_MODEL_DOWNLOAD_TYPES:
        return None, "模型類型不支援"
    if _configured_connection_mode() != "local":
        return None, "目前是遠端模式，不提供本地 ComfyUI 模型匯入"
    if uploaded_file is None:
        return None, "請先選擇要上傳的模型檔案"
    original_name = str(getattr(uploaded_file, "filename", "") or "").strip()
    if not original_name:
        return None, "請先選擇要上傳的模型檔案"
    destination_dir, effective_relative_dir, label, msg = _resolve_model_destination_dir(
        model_type=model_type,
        base_dir=base_dir,
        relative_dir=relative_dir,
    )
    if msg:
        return None, msg
    try:
        filename = _safe_model_filename(original_name, f"uploaded_{model_type}.safetensors")
    except ValueError as exc:
        return None, str(exc)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = (destination_dir / filename).resolve()
    try:
        destination.relative_to(destination_dir)
    except ValueError:
        return None, "模型檔名不合法"
    if destination.exists():
        return None, f"{label} 檔案已存在：{filename}"
    temp_path = None
    written = 0
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{filename}.", suffix=".part", dir=str(destination_dir), delete=False) as tmp:
            temp_path = Path(tmp.name)
            stream = getattr(uploaded_file, "stream", uploaded_file)
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_COMFYUI_MODEL_DOWNLOAD_BYTES:
                    raise ValueError("模型檔案超過上傳大小上限")
                tmp.write(chunk)
        if written <= 0:
            raise ValueError("上傳內容為空")
        temp_path.replace(destination)
    except Exception as exc:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return None, str(exc)
    _write_comfyui_model_sidecar(
        model_type=model_type,
        filename=filename,
        base_dir=base_dir,
        relative_dir=effective_relative_dir,
        payload={
            "source": "manual_upload",
            "original_filename": original_name,
            "saved_filename": filename,
            "relative_dir": effective_relative_dir,
            "uploaded_at": datetime.now().isoformat(),
            "uploaded_by": _actor_value(actor, "username", "") if actor else "",
            "size_bytes": written,
        },
    )
    return {
        "type": model_type,
        "label": label,
        "filename": filename,
        "size_bytes": written,
        "relative_dir": effective_relative_dir,
        "saved_path": str(destination),
        "source": "manual_upload",
    }, None

def _client(actor=None, *, backend_url=None):
    binding = _comfyui_binding(actor, backend_url=backend_url)
    return _client_for_url(binding["url"])

def _client_for_url(url):
    if injected_client is not None:
        return injected_client
    factory = deps.get("comfyui_client_factory")
    if factory:
        return factory(url)
    if str(url or "").startswith("diffusers://"):
        return DiffusersClient.from_settings(
            _live_comfyui_settings(),
            storage_root=deps.get("STORAGE_DIR") or ".",
            backend_url=url,
        )
    return ComfyUIClient(url, timeout=COMFYUI_BACKEND_REQUEST_TIMEOUT_SECONDS)


def _live_comfyui_settings():
    try:
        settings = load_system_settings_from_db()
    except Exception:
        settings = get_system_settings() or {}
    return settings or {}


def _live_diffusers_settings():
    return _live_comfyui_settings()
