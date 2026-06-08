"""Hugging Face Diffusers backend for the ComfyUI module.

The public routes still speak the existing ComfyUI-shaped contract. This
client only replaces the execution backend when root selects diffusers mode.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import uuid
import warnings
from collections import deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from urllib import request as urllib_request
from urllib.parse import quote, unquote, urlparse

from services.comfyui.client import ComfyUIError, ComfyUIImage
from services.comfyui.constants import GENERATION_MODE_DEFINITIONS, detect_model_families
from services.comfyui.huggingface import (
    infer_gguf_base_repo,
    inspect_huggingface_diffusers_repo,
    normalize_diffusers_variant,
    normalize_huggingface_repo_file,
)
from services.comfyui.settings import normalize_huggingface_repo_id


DIFFUSERS_BACKEND_SCHEME = "diffusers"
DIFFUSERS_BACKEND_NETLOC = "local"
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]{8,}")
_BEARER_RE = re.compile(r"(authorization\s*[:=]\s*bearer\s+|bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DIFFUSERS_LOGGER_NAMES = (
    "diffusers",
    "huggingface_hub",
    "transformers",
    "accelerate",
    "torch",
    "safetensors",
    "py.warnings",
)


def _env_flag(name, *, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _settings_flag(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _format_bytes(value):
    size = max(0, int(value or 0))
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} {unit}"
    return f"{amount:.1f} {unit}"


def _format_speed(value):
    speed = max(0, float(value or 0))
    if speed <= 0:
        return ""
    return f"{_format_bytes(speed)}/s"


def _sanitize_runtime_log_line(value):
    text = _ANSI_ESCAPE_RE.sub("", str(value or "")).replace("\r", "\n").strip()
    text = _HF_TOKEN_RE.sub("hf_***", text)
    text = _BEARER_RE.sub(lambda match: match.group(1) + "***", text)
    return text[:700]


class _DiffusersProgressLogHandler(logging.Handler):
    def __init__(self, owner):
        super().__init__(level=logging.INFO)
        self.owner = owner

    def emit(self, record):
        self.owner.emit_record(record)


class _DiffusersStreamTee:
    def __init__(self, original, owner, *, stream_name="stdout"):
        self.original = original
        self.owner = owner
        self.stream_name = stream_name
        self.buffer = ""
        self.encoding = getattr(original, "encoding", None) or "utf-8"
        self.errors = getattr(original, "errors", None) or "replace"

    def write(self, value):
        text = str(value or "")
        try:
            self.original.write(text)
        except Exception:
            pass
        self._capture(text, final=False)
        return len(text)

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass
        self._capture("", final=True)

    def isatty(self):
        try:
            return bool(self.original.isatty())
        except Exception:
            return False

    def fileno(self):
        return self.original.fileno()

    def _capture(self, text, *, final=False):
        if text:
            self.buffer += text
        normalized = self.buffer.replace("\r", "\n")
        if "\n" not in normalized and not final and len(normalized) < 500:
            return
        parts = normalized.split("\n")
        complete = parts if final else parts[:-1]
        self.buffer = "" if final else parts[-1]
        for line in complete:
            self.owner.append_line(line)


class _DiffusersRuntimeLogCapture:
    def __init__(self, progress_callback, *, max_lines=80):
        self.progress_callback = progress_callback
        self.max_lines = max(1, int(max_lines or 80))
        self.tail = deque(maxlen=self.max_lines)
        self.lock = threading.Lock()
        self.handler = None
        self.previous_levels = []
        self.previous_stdout = None
        self.previous_stderr = None
        self.previous_showwarning = None
        self.stdout_proxy = None
        self.stderr_proxy = None
        self.active = False
        self.last_emit = 0.0
        self.formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
        self.transient_paths = []

    def __enter__(self):
        if self.active or not self.progress_callback:
            return self
        self.handler = _DiffusersProgressLogHandler(self)
        self.handler.setFormatter(self.formatter)
        self.previous_levels = []
        for name in _DIFFUSERS_LOGGER_NAMES:
            logger = logging.getLogger(name)
            self.previous_levels.append((logger, logger.level))
            if logger.level == logging.NOTSET or logger.level > logging.INFO:
                logger.setLevel(logging.INFO)
            logger.addHandler(self.handler)
        self.previous_stdout = sys.stdout
        self.previous_stderr = sys.stderr
        self.stdout_proxy = _DiffusersStreamTee(self.previous_stdout, self, stream_name="stdout")
        self.stderr_proxy = _DiffusersStreamTee(self.previous_stderr, self, stream_name="stderr")
        sys.stdout = self.stdout_proxy
        sys.stderr = self.stderr_proxy
        self.previous_showwarning = warnings.showwarning
        owner = self

        def capture_warning(message, category, filename, lineno, file=None, line=None):
            try:
                formatted = warnings.formatwarning(message, category, filename, lineno, line)
                owner.append_line(formatted)
            except Exception:
                owner.append_line(message)
            target_file = file if file is not None else owner.previous_stderr
            return owner.previous_showwarning(message, category, filename, lineno, file=target_file, line=line)

        warnings.showwarning = capture_warning
        self.active = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.active:
            return False
        for proxy in (self.stdout_proxy, self.stderr_proxy):
            try:
                proxy.flush()
            except Exception:
                pass
        if sys.stdout is self.stdout_proxy and self.previous_stdout is not None:
            sys.stdout = self.previous_stdout
        if sys.stderr is self.stderr_proxy and self.previous_stderr is not None:
            sys.stderr = self.previous_stderr
        if self.previous_showwarning is not None and warnings.showwarning is not self.previous_showwarning:
            warnings.showwarning = self.previous_showwarning
        for logger, level in self.previous_levels:
            try:
                logger.removeHandler(self.handler)
                logger.setLevel(level)
            except Exception:
                pass
        self.previous_levels = []
        self.handler = None
        self.previous_stdout = None
        self.previous_stderr = None
        self.previous_showwarning = None
        self.stdout_proxy = None
        self.stderr_proxy = None
        self.active = False
        self.emit_tail(force=True)
        return False

    def register_transient_path(self, path):
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            return
        if not resolved:
            return
        self.transient_paths.append(resolved)

    def cleanup_transient_paths(self):
        paths = list(self.transient_paths)
        self.transient_paths = []
        for path in paths:
            try:
                if not path.exists():
                    continue
                self.append_line(f"Removing transient Diffusers download cache: {path}")
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except Exception as exc:
                self.append_line(f"Unable to remove transient Diffusers download cache {path}: {exc}")

    def append_line(self, value, *, force=False):
        line = _sanitize_runtime_log_line(value)
        if not line:
            return
        with self.lock:
            self.tail.append(line)
        self.emit_tail(force=force)

    def emit_record(self, record):
        try:
            line = _sanitize_runtime_log_line(self.formatter.format(record))
        except Exception:
            line = _sanitize_runtime_log_line(record.getMessage())
        self.append_line(line)

    def emit_tail(self, *, force=False):
        if not self.progress_callback:
            return
        now = time.monotonic()
        if not force and now - self.last_emit < 0.5:
            return
        with self.lock:
            lines = list(self.tail)
        if not lines:
            return
        self.last_emit = now
        self.progress_callback({
            "backend_kind": "diffusers",
            "python_log_tail": lines,
        })

    def progress(self, payload):
        if not self.progress_callback:
            return
        data = dict(payload or {})
        data.setdefault("backend_kind", "diffusers")
        with self.lock:
            lines = list(self.tail)
        if lines:
            data["python_log_tail"] = lines
        self.progress_callback(data)

    @contextmanager
    def heartbeat(self, *, phase, percent, step, detail, interval=15, extra_payload=None):
        if not self.progress_callback:
            yield
            return
        stop_event = threading.Event()
        interval = max(5, int(interval or 15))

        def emit_loop():
            elapsed = 0
            while not stop_event.wait(interval):
                elapsed += interval
                self.append_line(f"{step}: still running after {elapsed}s", force=True)
                payload = {
                    "phase": phase,
                    "percent": percent,
                    "step": step,
                    "detail": f"{detail}；仍在執行 {elapsed}s",
                    "backend_kind": "diffusers",
                }
                if callable(extra_payload):
                    try:
                        payload.update(extra_payload() or {})
                    except Exception:
                        pass
                self.progress(payload)

        thread = threading.Thread(target=emit_loop, name="diffusers-progress-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=1)

    def error(self, message, *, step="Diffusers 失敗"):
        self.progress({
            "phase": "error",
            "percent": 100,
            "backend_kind": "diffusers",
            "step": step,
            "detail": str(message or "Diffusers 產圖失敗"),
            "error_message": str(message or "Diffusers 產圖失敗"),
            "completed": False,
        })


def diffusers_backend_url(repo_id=""):
    repo_id = str(repo_id or "").strip()
    suffix = f"/{quote(repo_id, safe='')}" if repo_id else ""
    return f"{DIFFUSERS_BACKEND_SCHEME}://{DIFFUSERS_BACKEND_NETLOC}{suffix}"


def repo_id_from_diffusers_url(value):
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme != DIFFUSERS_BACKEND_SCHEME or parsed.netloc != DIFFUSERS_BACKEND_NETLOC:
        return ""
    return unquote(parsed.path.lstrip("/")).strip()


@dataclass(frozen=True)
class _PipelineCacheKey:
    model_repo: str
    variant: str
    gguf_file: str
    gguf_base_repo: str
    mode: str
    device: str
    dtype: str
    device_map: str
    low_cpu_mem_usage: bool
    token_fingerprint: str


class DiffusersClient:
    backend_kind = "diffusers"
    _pipeline_cache = {}
    _pipeline_cache_lock = threading.RLock()

    def __init__(
        self,
        *,
        model_repo="",
        token="",
        storage_root=".",
        device="auto",
        dtype="auto",
        device_map="auto",
        low_cpu_mem_usage=True,
        cuda_fallback_to_cpu=True,
        base_url="",
        allow_in_process_runtime=False,
        keep_downloaded_models=True,
        disable_xet=True,
        huggingface_cache_root="",
    ):
        self.model_repo = str(model_repo or "").strip()
        self.token = str(token or "").strip()
        self.storage_root = Path(storage_root or ".").expanduser()
        self.runtime_root = self.storage_root / "_runtime" / "comfyui_diffusers"
        cache_root = str(huggingface_cache_root or "").strip()
        self.huggingface_cache_root = Path(cache_root).expanduser() if cache_root else None
        self.device_setting = str(device or "auto").strip().lower()
        self.dtype_setting = str(dtype or "auto").strip().lower()
        self.device_map_setting = str(device_map or "auto").strip().lower()
        if self.device_map_setting == "none":
            self.device_map_setting = "disabled"
        self.low_cpu_mem_usage = bool(low_cpu_mem_usage)
        self.cuda_fallback_to_cpu = bool(cuda_fallback_to_cpu)
        self.base_url = str(base_url or diffusers_backend_url(self.model_repo))
        self.allow_in_process_runtime = bool(allow_in_process_runtime)
        self.keep_downloaded_models = bool(keep_downloaded_models)
        self.disable_xet = bool(disable_xet)
        self.timeout = 30

    @classmethod
    def from_settings(cls, settings=None, *, storage_root=".", backend_url=""):
        settings = settings or {}
        repo_from_url = repo_id_from_diffusers_url(backend_url)
        model_repo = normalize_huggingface_repo_id(
            repo_from_url or settings.get("comfyui_diffusers_model_repo"),
            allow_blank=True,
        ) or ""
        return cls(
            model_repo=model_repo,
            token=settings.get("comfyui_huggingface_api_token") or "",
            storage_root=storage_root,
            device=settings.get("comfyui_diffusers_device") or "auto",
            dtype=settings.get("comfyui_diffusers_dtype") or "auto",
            device_map=settings.get("comfyui_diffusers_device_map") or "auto",
            low_cpu_mem_usage=_settings_flag(settings.get("comfyui_diffusers_low_cpu_mem_usage", True)),
            cuda_fallback_to_cpu=_settings_flag(settings.get("comfyui_diffusers_cuda_fallback_to_cpu", True)),
            base_url=backend_url or diffusers_backend_url(model_repo),
            allow_in_process_runtime=_settings_flag(settings.get("comfyui_allow_in_process_diffusers")),
            keep_downloaded_models=_settings_flag(settings.get("comfyui_diffusers_keep_downloaded_models", True)),
            disable_xet=_settings_flag(settings.get("comfyui_diffusers_disable_xet", True)),
            huggingface_cache_root=settings.get("comfyui_huggingface_cache_root") or "",
        )

    def _effective_model_repo(self, params=None):
        params = params or {}
        return normalize_huggingface_repo_id(
            params.get("diffusers_model_repo")
            or params.get("huggingface_model_repo")
            or params.get("hf_model_repo")
            or params.get("model")
            or self.model_repo,
            allow_blank=True,
        ) or ""

    def _ensure_configured(self, model_repo=None):
        if not str(model_repo or self.model_repo or "").strip():
            raise ComfyUIError("Diffusers 模式尚未設定 Hugging Face model repo，例如 dhead/waiIllustriousSDXL_v150 或 Heartsync/NSFW-Uncensored")

    def _ensure_in_process_runtime_allowed(self):
        if self.allow_in_process_runtime or _env_flag("HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS", default=False):
            return
        raise ComfyUIError(
            "Diffusers 模式會在 Flask 主程序內載入模型並執行推論，可能占用大量 RAM/VRAM/CPU；"
            "本站預設停用這條路徑。請改用外部 ComfyUI local/remote backend。"
            "若 root 明確接受主程序資源風險，請在右上角 AI 產圖快速設定勾選確認，"
            "或設定 HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS=1。"
        )

    def _effective_model_variant(self, params=None):
        params = params or {}
        raw = str(params.get("diffusers_model_variant") or "").strip()
        if raw.startswith("gguf::"):
            return ""
        variant = normalize_diffusers_variant(raw, allow_blank=True)
        if variant is None:
            raise ComfyUIError("Diffusers 模型精度版本名稱不合法")
        return variant

    def _effective_gguf_file(self, params=None):
        params = params or {}
        selected = str(params.get("diffusers_model_variant") or "").strip()
        raw = params.get("diffusers_gguf_file") or ""
        if selected.startswith("gguf::"):
            raw = selected.split("::", 1)[1]
        gguf_file = normalize_huggingface_repo_file(raw, allow_blank=True)
        if gguf_file is None or (gguf_file and not gguf_file.lower().endswith(".gguf")):
            raise ComfyUIError("GGUF 檔案路徑不合法")
        return gguf_file

    def _effective_gguf_base_repo(self, params=None, *, model_repo=""):
        params = params or {}
        base_repo = normalize_huggingface_repo_id(params.get("diffusers_gguf_base_repo"), allow_blank=True)
        if base_repo is None:
            raise ComfyUIError("GGUF base Diffusers repo 格式不合法")
        if base_repo:
            return base_repo
        if not model_repo:
            model_repo = self._effective_model_repo(params)
        inspection = inspect_huggingface_diffusers_repo(model_repo, token=self.token, mode=params.get("generation_mode") or "txt2img")
        if inspection.get("suggested_base_repo"):
            return inspection["suggested_base_repo"]
        inferred = infer_gguf_base_repo(model_repo)
        if inferred:
            return inferred
        raise ComfyUIError("GGUF 需要設定 base Diffusers repo，例如 stabilityai/stable-diffusion-xl-base-1.0")

    def _resolve_diffusers_variant(self, model_repo, *, explicit_variant="", mode="txt2img"):
        explicit_variant = str(explicit_variant or "").strip()
        normalized = normalize_diffusers_variant(explicit_variant, allow_blank=True)
        if normalized:
            return normalized
        if not model_repo:
            return ""
        inspection = inspect_huggingface_diffusers_repo(model_repo, token=self.token, mode=mode)
        if not inspection.get("ok"):
            raise ComfyUIError(inspection.get("msg") or "Hugging Face repo 檢查失敗，尚未開始下載。")
        variant_options = inspection.get("variant_options") or []
        diffusers_options = [
            item
            for item in variant_options
            if str(item.get("kind") or "diffusers") != "gguf"
        ]
        if len(diffusers_options) > 1:
            raise ComfyUIError("這個 Hugging Face repo 有多個精度版本，請先在生圖頁面選擇要下載/載入的版本，避免重複下載。")
        if len(diffusers_options) == 1:
            return normalize_diffusers_variant(diffusers_options[0].get("variant") or "", allow_blank=True) or ""
        return ""

    def _missing_dependency_names(self):
        required = {
            "diffusers": "diffusers",
            "torch": "torch",
            "PIL": "Pillow",
        }
        missing = []
        for module_name, package_name in required.items():
            if importlib.util.find_spec(module_name) is None:
                missing.append(package_name)
        return missing

    def _ensure_dependencies(self):
        missing = self._missing_dependency_names()
        if missing:
            raise ComfyUIError(
                "Diffusers 模式需要先安裝 Python 套件："
                + ", ".join(missing)
                + "。請用 ./test_for_develop.sh 選 requirements-hf.txt / requirements-comfyui.txt 補齊，或執行 python3 -m pip install -r scripts/comfyui/generation_probe_requirements.txt。"
            )

    def _ensure_gguf_dependencies(self):
        self._ensure_dependencies()
        missing = []
        for module_name, package_name in {"huggingface_hub": "huggingface-hub", "gguf": "gguf"}.items():
            if importlib.util.find_spec(module_name) is None:
                missing.append(package_name)
        if missing:
            raise ComfyUIError("GGUF 模式需要先安裝 Python 套件：" + ", ".join(missing))

    def _ensure_gguf_metadata_dependencies(self):
        if importlib.util.find_spec("gguf") is None:
            raise ComfyUIError("GGUF metadata 判斷需要先安裝 Python 套件：gguf")

    def _configure_huggingface_download_backend(self, *, log_capture=None):
        if self.huggingface_cache_root:
            hf_home = self.huggingface_cache_root
            hub_cache = hf_home / "hub"
            hf_home.mkdir(parents=True, exist_ok=True)
            hub_cache.mkdir(parents=True, exist_ok=True)
            os.environ["HF_HOME"] = str(hf_home)
            os.environ["HF_HUB_CACHE"] = str(hub_cache)
            os.environ["HUGGINGFACE_HUB_CACHE"] = str(hub_cache)
            if log_capture:
                log_capture.append_line(f"Hugging Face cache root set to {hf_home}")
        if self.disable_xet:
            os.environ["HF_HUB_DISABLE_XET"] = "1"
            if log_capture:
                log_capture.append_line("Hugging Face Xet backend disabled for Diffusers downloads")
        else:
            os.environ.pop("HF_HUB_DISABLE_XET", None)
        if importlib.util.find_spec("hf_transfer") is None:
            return {
                "hf_transfer_enabled": False,
                "xet_disabled": os.environ.get("HF_HUB_DISABLE_XET") == "1",
                "cache_root": str(self.huggingface_cache_root or ""),
                "hub_cache": os.environ.get("HF_HUB_CACHE", ""),
            }
        os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
        return {
            "hf_transfer_enabled": True,
            "xet_disabled": os.environ.get("HF_HUB_DISABLE_XET") == "1",
            "cache_root": str(self.huggingface_cache_root or ""),
            "hub_cache": os.environ.get("HF_HUB_CACHE", ""),
        }

    def _huggingface_repo_cache_dir(self, model_repo):
        repo_id = str(model_repo or "").strip()
        if not repo_id:
            return None
        if self.huggingface_cache_root:
            hub_cache = self.huggingface_cache_root / "hub"
            return hub_cache / ("models--" + repo_id.replace("/", "--"))
        repo_cache_name = "models--" + repo_id.replace("/", "--")
        hub_candidates = []
        cache_root = os.environ.get("HF_HUB_CACHE")
        if cache_root:
            hub_candidates.append(Path(cache_root).expanduser())
        legacy_cache_root = os.environ.get("HUGGINGFACE_HUB_CACHE")
        if legacy_cache_root:
            legacy_cache_path = Path(legacy_cache_root).expanduser()
            if legacy_cache_path not in hub_candidates:
                hub_candidates.append(legacy_cache_path)
        hf_home = Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser()
        hub_candidates.append(hf_home / "hub")
        for hub_cache in hub_candidates:
            candidate = hub_cache / repo_cache_name
            if candidate.exists():
                return candidate
        return hub_candidates[0] / repo_cache_name

    def _largest_incomplete_download_bytes(self, model_repo):
        cache_dir = self._huggingface_repo_cache_dir(model_repo)
        if not cache_dir:
            return 0
        blob_dir = cache_dir / "blobs"
        if not blob_dir.exists():
            return 0
        largest = 0
        try:
            for path in blob_dir.glob("*.incomplete"):
                try:
                    largest = max(largest, int(path.stat().st_size))
                except OSError:
                    continue
        except OSError:
            return 0
        return largest

    def _download_heartbeat_payload(self, tracker, *, current_file="", model_repo=""):
        tracker = tracker or {}
        current = int(tracker.get("bytes_written") or 0)
        total = int(tracker.get("total_bytes") or 0)
        external_current = self._largest_incomplete_download_bytes(model_repo)
        if external_current > current:
            current = external_current
            tracker["external_bytes_written"] = external_current
        if total > 0:
            tracker["external_total_bytes"] = max(int(tracker.get("external_total_bytes") or 0), total)
        payload = {
            "current_file": current_file,
            "bytes_written": current,
            "total_bytes": total,
        }
        if total > 0:
            payload["percent"] = min(99, round(6 + 16 * min(1, current / total), 1))
        return payload

    def _new_transient_download_dir(self, label):
        safe_label = _SAFE_FILENAME_RE.sub("_", str(label or "model")).strip("._") or "model"
        root = self.runtime_root / "transient_downloads"
        path = root / f"{safe_label}_{uuid.uuid4().hex}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _huggingface_file_url(self, model_repo, filename):
        repo_path = "/".join(quote(part, safe="") for part in str(model_repo or "").strip().split("/") if part)
        file_path = quote(str(filename or "").strip().lstrip("/"), safe="/")
        return f"https://huggingface.co/{repo_path}/resolve/main/{file_path}"

    def _download_target_path(self, model_repo, filename, *, log_capture=None):
        parts = [part for part in str(filename or "").replace("\\", "/").split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise ComfyUIError("Hugging Face 檔案路徑不安全")
        if self.keep_downloaded_models:
            safe_repo = _SAFE_FILENAME_RE.sub("_", str(model_repo or "model").replace("/", "__")).strip("._") or "model"
            base_dir = self.runtime_root / "hf_downloads" / safe_repo
        else:
            base_dir = self._new_transient_download_dir(f"hf_file_{str(model_repo or 'model').replace('/', '_')}")
            if log_capture:
                log_capture.register_transient_path(base_dir)
                log_capture.append_line(f"Diffusers keep-downloaded-models disabled; using transient HF file dir={base_dir}")
        target = (base_dir / Path(*parts)).resolve()
        try:
            target.relative_to(base_dir.resolve())
        except Exception as exc:
            raise ComfyUIError("Hugging Face 檔案路徑超出允許範圍") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _stream_huggingface_file_download(
        self,
        model_repo,
        filename,
        *,
        progress_callback=None,
        log_capture=None,
        backend=None,
        base_percent=6,
        span_percent=16,
    ):
        target = self._download_target_path(model_repo, filename, log_capture=log_capture)
        if target.exists() and target.stat().st_size > 0:
            size = int(target.stat().st_size)
            if log_capture:
                log_capture.append_line(f"GGUF cache hit: {target} ({_format_bytes(size)})", force=True)
            return str(target), {"cache_hit": True, "bytes_written": size, "total_bytes": size}

        incomplete = target.with_name(target.name + ".incomplete")
        existing = int(incomplete.stat().st_size) if incomplete.exists() else 0
        headers = {"User-Agent": "hackme_web-diffusers/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"
        url = self._huggingface_file_url(model_repo, filename)
        if log_capture:
            resume_text = f"; resume from {_format_bytes(existing)}" if existing else ""
            log_capture.append_line(f"Manual Hugging Face file download: {url}{resume_text}", force=True)

        def emit(downloaded, total, *, speed=0, force=False):
            if not progress_callback:
                return
            ratio = (downloaded / total) if total > 0 else 0
            percent = min(99, round(float(base_percent) + float(span_percent) * min(1, ratio), 1))
            detail = (
                f"下載 {filename}：{_format_bytes(downloaded)} / {_format_bytes(total)}"
                if total
                else f"下載 {filename}：{_format_bytes(downloaded)}"
            )
            if speed:
                detail += f"，{_format_speed(speed)}"
            progress_callback({
                "phase": "downloading",
                "percent": percent,
                "step": "Hugging Face GGUF 串流下載",
                "current_file": filename,
                "detail": detail,
                "bytes_written": int(downloaded),
                "total_bytes": int(total or 0),
                "speed_bytes_per_sec": int(speed or 0),
                **(backend or {}),
            })
            if force and log_capture:
                log_capture.append_line(detail, force=True)

        try:
            request = urllib_request.Request(url, headers=headers)
            with urllib_request.urlopen(request, timeout=60) as response:
                status = int(getattr(response, "status", 200) or 200)
                content_length = int(response.headers.get("Content-Length") or 0)
                content_range = str(response.headers.get("Content-Range") or "")
                total = 0
                range_match = re.search(r"/(\d+)\s*$", content_range)
                if status == 206 and range_match:
                    total = int(range_match.group(1))
                elif content_length:
                    total = content_length
                mode = "ab" if existing and status == 206 else "wb"
                downloaded = existing if mode == "ab" else 0
                if mode == "wb":
                    existing = 0
                start_at = time.monotonic()
                last_emit = 0.0
                emit(downloaded, total, force=True)
                with incomplete.open(mode) as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_emit >= 0.5:
                            elapsed = max(0.001, now - start_at)
                            emit(downloaded, total, speed=(downloaded - existing) / elapsed)
                            last_emit = now
                if total and downloaded < total:
                    raise ComfyUIError(
                        f"Hugging Face 檔案下載未完整：{_format_bytes(downloaded)} / {_format_bytes(total)}"
                    )
                incomplete.replace(target)
                emit(downloaded, total or downloaded, force=True)
                return str(target), {"cache_hit": False, "bytes_written": downloaded, "total_bytes": total or downloaded}
        except ComfyUIError:
            raise
        except Exception as exc:
            raise ComfyUIError(f"Hugging Face 檔案串流下載失敗：{exc}") from exc

    def _inspect_gguf_file_metadata(self, path):
        try:
            from gguf import GGUFReader
        except Exception as exc:
            raise ComfyUIError(f"GGUF metadata 讀取失敗：缺少 gguf 套件：{exc}") from exc
        try:
            reader = GGUFReader(str(path))
        except Exception as exc:
            raise ComfyUIError(f"GGUF metadata 讀取失敗：{exc}") from exc
        fields = set(getattr(reader, "fields", {}) or {})
        tensors = list(getattr(reader, "tensors", []) or [])
        tensor_names = [str(getattr(tensor, "name", "") or "") for tensor in tensors[:80]]
        has_comfy_metadata = any(name.startswith("comfy.gguf.") for name in fields)
        has_original_unet_names = any(
            name.startswith(("input_blocks.", "middle_block.", "output_blocks.", "out."))
            for name in tensor_names
        )
        return {
            "field_count": len(fields),
            "tensor_count": len(tensors),
            "has_comfy_metadata": has_comfy_metadata,
            "has_original_unet_names": has_original_unet_names,
            "sample_tensors": tensor_names[:12],
        }

    def _classify_gguf_backend(self, metadata):
        metadata = metadata if isinstance(metadata, dict) else {}
        if metadata.get("has_comfy_metadata") and metadata.get("has_original_unet_names"):
            return "comfyui_gguf"
        return "diffusers"

    def prepare_gguf_file_for_backend(
        self,
        model_repo,
        gguf_file,
        *,
        progress_callback=None,
        log_capture=None,
    ):
        """Download a selected GGUF file and classify which backend should own it.

        This deliberately stops before importing torch / diffusers. Native
        ComfyUI-GGUF UNet files only need download + metadata inspection so the
        route layer can hand them to a ComfyUI workflow instead of failing inside
        Diffusers pipeline loading.
        """
        model_repo = self._effective_model_repo({"diffusers_model_repo": model_repo, "model": model_repo})
        gguf_file = self._effective_gguf_file({"diffusers_gguf_file": gguf_file})
        self._ensure_configured(model_repo)
        self._ensure_gguf_metadata_dependencies()
        backend = self._configure_huggingface_download_backend(log_capture=log_capture)
        if progress_callback:
            progress_callback({
                "phase": "downloading",
                "percent": 5,
                "backend_kind": "diffusers",
                "step": "判斷 GGUF backend",
                "current_file": gguf_file,
                "detail": (
                    f"正在下載/檢查 {gguf_file}，完成後會自動判斷留在 Diffusers 或改走 ComfyUI-GGUF workflow"
                    + ("；已啟用 hf_transfer 加速" if backend["hf_transfer_enabled"] else "；使用 Hugging Face 串流下載")
                    + ("；已停用 hf_xet" if backend["xet_disabled"] else "")
                ),
                "token_used": bool(self.token),
                **backend,
            })
        heartbeat = (
            log_capture.heartbeat(
                phase="downloading",
                percent=5,
                step="Hugging Face GGUF 檔案下載",
                detail=f"正在下載/檢查 GGUF 檔案 {gguf_file}",
            )
            if log_capture
            else nullcontext()
        )
        with heartbeat:
            gguf_path, download_stats = self._stream_huggingface_file_download(
                model_repo,
                gguf_file,
                progress_callback=progress_callback,
                log_capture=log_capture,
                backend=backend,
                base_percent=5,
                span_percent=16,
            )
        metadata = self._inspect_gguf_file_metadata(gguf_path)
        suggested_backend = self._classify_gguf_backend(metadata)
        if log_capture:
            log_capture.append_line(
                "GGUF backend classification: "
                f"backend={suggested_backend}, "
                f"tensors={metadata.get('tensor_count')}, "
                f"comfy_metadata={metadata.get('has_comfy_metadata')}, "
                f"original_unet_names={metadata.get('has_original_unet_names')}",
                force=True,
            )
        if progress_callback:
            progress_callback({
                "phase": "routing",
                "percent": 23,
                "backend_kind": suggested_backend,
                "step": "GGUF backend 已判斷",
                "current_file": gguf_file,
                "detail": (
                    "偵測為 ComfyUI-GGUF 原生 UNet，將改用 ComfyUI-GGUF workflow"
                    if suggested_backend == "comfyui_gguf"
                    else "偵測為 Diffusers 相容 GGUF component，將繼續使用 Diffusers backend"
                ),
                "cache_hit": bool((download_stats or {}).get("cache_hit")),
                "bytes_written": int((download_stats or {}).get("bytes_written") or 0),
                "total_bytes": int((download_stats or {}).get("total_bytes") or 0),
                "gguf_metadata": metadata,
            })
        return {
            "path": str(gguf_path),
            "stats": dict(download_stats or {}),
            "metadata": metadata,
            "suggested_backend": suggested_backend,
            "model_repo": model_repo,
            "gguf_file": gguf_file,
        }

    def prepare_huggingface_file(
        self,
        model_repo,
        filename,
        *,
        progress_callback=None,
        log_capture=None,
        label="Hugging Face 檔案",
        base_percent=24,
        span_percent=4,
    ):
        model_repo = normalize_huggingface_repo_id(model_repo)
        filename = str(filename or "").strip().replace("\\", "/")
        if not model_repo:
            raise ComfyUIError("Hugging Face repo 格式不合法。")
        if not filename or filename.startswith("/") or ".." in filename.split("/"):
            raise ComfyUIError("Hugging Face 檔名不合法。")
        backend = self._configure_huggingface_download_backend(log_capture=log_capture)
        if progress_callback:
            progress_callback({
                "phase": "downloading",
                "percent": base_percent,
                "backend_kind": "diffusers",
                "step": f"下載 {label}",
                "current_file": filename,
                "detail": f"正在下載/檢查 {filename}",
                "token_used": bool(self.token),
                **backend,
            })
        path, stats = self._stream_huggingface_file_download(
            model_repo,
            filename,
            progress_callback=progress_callback,
            log_capture=log_capture,
            backend=backend,
            base_percent=base_percent,
            span_percent=span_percent,
        )
        return {
            "path": str(path),
            "stats": dict(stats or {}),
            "model_repo": model_repo,
            "filename": filename,
        }

    def _raise_if_unsupported_diffusers_gguf(self, gguf_path, *, gguf_file="", progress_callback=None, log_capture=None):
        metadata = self._inspect_gguf_file_metadata(gguf_path)
        if log_capture:
            log_capture.append_line(
                "GGUF metadata: "
                f"tensors={metadata['tensor_count']}, "
                f"comfy_metadata={metadata['has_comfy_metadata']}, "
                f"original_unet_names={metadata['has_original_unet_names']}",
                force=True,
            )
        if metadata["has_comfy_metadata"] and metadata["has_original_unet_names"]:
            sample = ", ".join(metadata["sample_tensors"][:4])
            message = (
                f"{gguf_file or Path(gguf_path).name} 是 ComfyUI-GGUF 原生 UNet GGUF "
                "（metadata 含 comfy.gguf，tensor 命名為 input_blocks/output_blocks）。"
                "本站 HF Diffusers backend 只能安全載入 Diffusers 相容 component GGUF；"
                "此類模型請改用 ComfyUI local/remote backend，並在 workflow 使用 ComfyUI-GGUF 的 "
                "Unet Loader (GGUF) 節點，或改選 Diffusers-format GGUF。"
                + (f" 範例 tensor：{sample}" if sample else "")
            )
            if progress_callback:
                progress_callback({
                    "phase": "error",
                    "percent": 100,
                    "backend_kind": "diffusers",
                    "step": "GGUF 格式不支援 Diffusers backend",
                    "current_file": gguf_file,
                    "detail": message,
                    "error_message": message,
                    "completed": False,
                    "gguf_metadata": metadata,
                })
            raise ComfyUIError(message)
        return metadata

    def health_check(self, *, timeout=3):
        self._ensure_dependencies()
        return {
            "ok": True,
            "backend": "diffusers",
            "base_url": self.base_url,
            "model_repo": self.model_repo,
            "token_configured": bool(self.token),
            "huggingface_cache_root": str(self.huggingface_cache_root or ""),
            "huggingface_cache_root_configured": bool(self.huggingface_cache_root),
            "system": {
                "backend": "diffusers",
                "model_repo": self.model_repo,
                "device": self.device_setting,
                "dtype": self.dtype_setting,
            },
        }

    def get_models(self):
        return [self.model_repo] if self.model_repo else []

    def get_loras(self):
        return []

    def get_vaes(self):
        return []

    def get_embeddings(self):
        return []

    def get_sampler_options(self):
        return {"samplers": ["diffusers-auto"], "schedulers": ["default"]}

    def get_object_info(self, node_class=None):
        return {}

    def list_node_classes(self):
        return []

    def get_capabilities(self):
        model_families = detect_model_families([self.model_repo] if self.model_repo else [])
        supported_modes = {"txt2img", "img2img", "inpaint"}
        generation_modes = []
        for key, value in GENERATION_MODE_DEFINITIONS.items():
            available = key in supported_modes
            generation_modes.append({
                "key": key,
                "label": value.get("label") or key,
                "available": available,
                "workflow_only": bool(value.get("workflow_only")) if available else bool(value.get("workflow_only")),
                "output_kind": value.get("output_kind") or "image",
                "source_kind": value.get("source_kind") or "",
                "recommended_model_families": list(value.get("recommended_model_families") or []),
                "unavailable_reason": "" if available else "Diffusers 後端目前只支援文字生圖、圖生圖與局部重繪。",
            })
        return {
            "backend_kind": "diffusers",
            "available_nodes": [],
            "models": [self.model_repo] if self.model_repo else [],
            "loras": [],
            "vaes": [],
            "diffusion_models": [],
            "clip_models": [],
            "embeddings": [],
            "samplers": ["diffusers-auto"],
            "schedulers": ["default"],
            "controlnet_models": [],
            "upscale_models": [],
            "latent_upscale_models": [],
            "controlnet_types": {},
            "generation_modes": generation_modes,
            "model_families": model_families,
            "model_repo": self.model_repo,
            "token_configured": bool(self.token),
            "huggingface_cache_root": str(self.huggingface_cache_root or ""),
            "huggingface_cache_root_configured": bool(self.huggingface_cache_root),
        }

    def inspect_model_repo(self, repo_value, *, mode="txt2img"):
        return inspect_huggingface_diffusers_repo(repo_value, token=self.token, mode=mode)

    def _dir_for_type(self, file_type):
        normalized = str(file_type or "output").strip().lower()
        if normalized not in {"input", "output", "temp"}:
            normalized = "output"
        path = self.runtime_root / normalized
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _safe_filename(self, filename, *, fallback="image.png"):
        name = Path(str(filename or "")).name.strip()
        if not name:
            name = fallback
        name = _SAFE_FILENAME_RE.sub("_", name)[:160].strip("._")
        if not name:
            name = fallback
        return name

    def upload_image_bytes(self, data, filename, *, image_type="input", overwrite=False, subfolder=""):
        target_dir = self._safe_ref_dir({"type": image_type, "subfolder": subfolder})
        safe_name = self._safe_filename(filename)
        if not overwrite:
            safe_name = f"{uuid.uuid4().hex}_{safe_name}"
        path = target_dir / safe_name
        path.write_bytes(data or b"")
        return {"filename": safe_name, "subfolder": str(subfolder or "").strip(), "type": str(image_type or "input")}

    def _safe_ref_dir(self, file_ref):
        file_type = str((file_ref or {}).get("type") or "output").strip().lower()
        root = self._dir_for_type(file_type)
        subfolder = str((file_ref or {}).get("subfolder") or "").strip().replace("\\", "/")
        if not subfolder:
            return root
        parts = [part for part in subfolder.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            raise ComfyUIError("Diffusers 檔案路徑不安全")
        target = (root / Path(*parts)).resolve()
        try:
            target.relative_to(root.resolve())
        except Exception as exc:
            raise ComfyUIError("Diffusers 檔案路徑超出允許範圍") from exc
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _safe_ref_path(self, file_ref):
        if not isinstance(file_ref, dict):
            raise ComfyUIError("Diffusers 檔案參照格式錯誤")
        filename = self._safe_filename(file_ref.get("filename"), fallback="")
        if not filename:
            raise ComfyUIError("Diffusers 檔案參照缺少檔名")
        path = (self._safe_ref_dir(file_ref) / filename).resolve()
        try:
            path.relative_to(self.runtime_root.resolve())
        except Exception as exc:
            raise ComfyUIError("Diffusers 檔案路徑超出允許範圍") from exc
        return path

    def fetch_file(self, file_ref):
        path = self._safe_ref_path(file_ref)
        if not path.is_file():
            raise ComfyUIError("Diffusers 檔案不存在")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return ComfyUIImage(
            filename=path.name,
            subfolder=str((file_ref or {}).get("subfolder") or ""),
            type=str((file_ref or {}).get("type") or "output"),
            mime_type=mime_type,
            data=path.read_bytes(),
        )

    def fetch_image(self, image_ref):
        return self.fetch_file(image_ref)

    def discard_image(self, image_ref, *, prompt_id=None, local_base_dir=None, allow_api_delete=True):
        try:
            path = self._safe_ref_path(image_ref)
        except ComfyUIError:
            return {"file_deleted": False, "file_missing": True, "file_delete_supported": True, "history_deleted": False}
        if not path.exists():
            return {"file_deleted": False, "file_missing": True, "file_delete_supported": True, "history_deleted": False}
        try:
            path.unlink()
        except Exception as exc:
            raise ComfyUIError(f"Diffusers 預覽檔案刪除失敗：{exc}") from exc
        return {"file_deleted": True, "file_missing": False, "file_delete_supported": True, "history_deleted": bool(prompt_id)}

    def interrupt(self, *, timeout_seconds=None):
        return {"interrupted": False, "message": "Diffusers 後端目前不支援中斷已進入推論中的工作"}

    def _token_fingerprint(self):
        if not self.token:
            return ""
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:16]

    def _resolve_device(self, torch):
        requested = self.device_setting
        if requested in {"cpu", "cuda", "mps"}:
            return requested
        if torch.cuda.is_available():
            return "cuda"
        mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
        if mps_backend is not None and mps_backend.is_available():
            return "mps"
        return "cpu"

    def _resolve_dtype(self, torch, device):
        requested = self.dtype_setting
        if requested == "float16":
            return torch.float16
        if requested == "bfloat16":
            return torch.bfloat16
        if requested == "float32":
            return torch.float32
        return torch.float16 if device == "cuda" else torch.float32

    def _dtype_from_hint(self, torch, value):
        requested = str(value or "").strip().lower()
        if requested in {"float16", "fp16", "half"}:
            return torch.float16
        if requested in {"bfloat16", "bf16"}:
            return torch.bfloat16
        if requested in {"float32", "fp32"}:
            return torch.float32
        return None

    def _accelerate_available(self):
        return importlib.util.find_spec("accelerate") is not None

    def _resolve_device_map(self, device, cuda_memory=None):
        requested = (self.device_map_setting or "auto").strip().lower()
        if requested in {"disabled", "none", "off", "false", "0"}:
            return ""
        if requested == "auto":
            if device != "cuda" or not self._accelerate_available():
                return ""
            total_bytes = int((cuda_memory or {}).get("cuda_total_bytes") or 0)
            if total_bytes and total_bytes < 8 * 1024 * 1024 * 1024:
                return "balanced"
            return "cuda"
        if requested in {"cuda", "balanced", "balanced_low_0", "sequential"}:
            return requested if self._accelerate_available() else ""
        return ""

    def _cuda_memory_payload(self, torch):
        if importlib.util.find_spec("torch") is None:
            return {}
        if not getattr(torch, "cuda", None) or not torch.cuda.is_available():
            return {}
        payload = {}
        try:
            payload["cuda_device_name"] = torch.cuda.get_device_name(0)
        except Exception:
            payload["cuda_device_name"] = "cuda"
        try:
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            payload["cuda_free_bytes"] = int(free_bytes)
            payload["cuda_total_bytes"] = int(total_bytes)
        except Exception:
            pass
        return payload

    def _should_fallback_to_cpu_for_low_vram(self, device, cuda_memory):
        if not self.cuda_fallback_to_cpu or self.device_setting != "auto" or device != "cuda":
            return False
        total_bytes = int((cuda_memory or {}).get("cuda_total_bytes") or 0)
        return bool(total_bytes and total_bytes < 8 * 1024 * 1024 * 1024)

    def _log_cuda_memory(self, torch, *, log_capture=None, progress_callback=None):
        memory = self._cuda_memory_payload(torch)
        if not memory:
            return {}
        free_bytes = int(memory.get("cuda_free_bytes") or 0)
        total_bytes = int(memory.get("cuda_total_bytes") or 0)
        name = memory.get("cuda_device_name") or "cuda"
        if log_capture:
            if total_bytes:
                log_capture.append_line(
                    f"CUDA device: {name}; VRAM free {_format_bytes(free_bytes)} / total {_format_bytes(total_bytes)}",
                    force=True,
                )
            else:
                log_capture.append_line(f"CUDA device: {name}; VRAM size unavailable", force=True)
            if total_bytes and total_bytes < 8 * 1024 * 1024 * 1024:
                log_capture.append_line(
                    "WARNING: CUDA VRAM is below 8GB; SDXL or large Diffusers models may load very slowly or fail. "
                    "Root can adjust Diffusers device/device_map/dtype, choose a smaller model, or use an external ComfyUI backend.",
                    force=True,
                )
        if progress_callback and total_bytes:
            progress_callback({
                "phase": "loading",
                "percent": 4,
                "step": "檢查 CUDA VRAM",
                "detail": f"CUDA {name}：VRAM 可用 {_format_bytes(free_bytes)} / {_format_bytes(total_bytes)}",
                **memory,
            })
        return memory

    def _huggingface_progress_tqdm_class(
        self,
        progress_callback,
        *,
        label="",
        base_percent=5,
        span_percent=20,
        log_capture=None,
        download_tracker=None,
    ):
        if not progress_callback:
            return None
        try:
            from huggingface_hub.utils import tqdm as hf_tqdm
        except Exception:
            return None

        lock = threading.Lock()
        state = {"last_emit": 0.0}
        outer_label = str(label or "Hugging Face 模型")

        def emit(bar, *, force=False):
            now = time.monotonic()
            if not force and now - state["last_emit"] < 0.35:
                return
            with lock:
                state["last_emit"] = now
            current = max(0, int(getattr(bar, "_hackme_progress_n", 0) or 0))
            total = max(0, int(getattr(bar, "_hackme_progress_total", 0) or 0))
            if download_tracker is not None:
                current = max(current, int(download_tracker.get("external_bytes_written") or 0))
                total = max(total, int(download_tracker.get("external_total_bytes") or 0))
            last_current = max(0, int(getattr(bar, "_hackme_progress_last_n", 0) or 0))
            last_at = float(getattr(bar, "_hackme_progress_last_at", 0.0) or 0.0)
            previous_speed = float(getattr(bar, "_hackme_progress_speed_bytes_per_second", 0.0) or 0.0)
            delta_t = now - last_at if last_at else 0.0
            instant_speed = ((current - last_current) / delta_t) if delta_t > 0 and current >= last_current else 0.0
            speed = (previous_speed * 0.65 + instant_speed * 0.35) if previous_speed and instant_speed else (instant_speed or previous_speed)
            bar._hackme_progress_last_n = current
            bar._hackme_progress_last_at = now
            bar._hackme_progress_speed_bytes_per_second = speed
            ratio = (current / total) if total > 0 else 0
            percent = min(99, round(float(base_percent) + float(span_percent) * ratio, 1))
            unit = str(getattr(bar, "_hackme_progress_unit", "") or "")
            desc = str(getattr(bar, "_hackme_progress_desc", "") or "").strip() or outer_label
            current_file = desc
            if unit.upper() == "B" or total > 1024 * 1024:
                if download_tracker is not None:
                    download_tracker["saw_any"] = True
                    download_tracker["saw_byte_bar"] = True
                    download_tracker["bytes_written"] = max(int(download_tracker.get("bytes_written") or 0), current)
                    download_tracker["total_bytes"] = max(int(download_tracker.get("total_bytes") or 0), total)
                size_text = f"{_format_bytes(current)} / {_format_bytes(total)}" if total else _format_bytes(current)
                speed_text = _format_speed(speed)
                cache_check = total <= 0 and current <= 0
                detail = (
                    f"檢查 {desc}：尚未偵測到下載位元組，可能是 Hugging Face cache hit 或 metadata 檢查"
                    if cache_check
                    else f"下載 {desc}：{size_text}{f'，{speed_text}' if speed_text else ''}"
                )
                payload = {
                    "phase": "downloading",
                    "percent": percent,
                    "step": "Hugging Face cache / download 檢查" if cache_check else "Hugging Face 檔案下載",
                    "current_file": current_file,
                    "detail": detail,
                    "bytes_written": current,
                    "total_bytes": total,
                    "speed_bytes_per_sec": int(speed) if speed > 0 else 0,
                }
                if cache_check:
                    payload["cache_check"] = True
            else:
                if download_tracker is not None:
                    download_tracker["saw_any"] = True
                    download_tracker["saw_item_bar"] = True
                    download_tracker["items_current"] = max(int(download_tracker.get("items_current") or 0), current)
                    download_tracker["items_total"] = max(int(download_tracker.get("items_total") or 0), total)
                total_text = f"/{total}" if total else ""
                detail = f"下載 {desc}：{current}{total_text} {unit or 'items'}"
                payload = {
                    "phase": "downloading",
                    "percent": percent,
                    "step": "Hugging Face 檔案清單",
                    "current_file": current_file,
                    "detail": detail,
                    "current": current,
                    "max": total,
                }
            progress_callback(payload)

        class HuggingFaceProgressTqdm(hf_tqdm):
            def __init__(self, *args, **kwargs):
                initial = int(kwargs.get("initial") or 0)
                total = int(kwargs.get("total") or 0)
                desc = str(kwargs.get("desc") or outer_label)
                unit = str(kwargs.get("unit") or "")
                kwargs["disable"] = False if log_capture else True
                super().__init__(*args, **kwargs)
                self._hackme_progress_n = initial
                self._hackme_progress_total = total
                self._hackme_progress_desc = desc
                self._hackme_progress_unit = unit
                self._hackme_progress_last_n = initial
                self._hackme_progress_last_at = time.monotonic()
                self._hackme_progress_speed_bytes_per_second = 0.0
                emit(self, force=True)

            def update(self, n=1):
                try:
                    self._hackme_progress_n = max(0, int(getattr(self, "_hackme_progress_n", 0) or 0) + int(n or 0))
                except Exception:
                    pass
                result = super().update(n)
                emit(self)
                return result

            def __iter__(self):
                iterable = getattr(self, "iterable", None)
                if iterable is None:
                    return
                for item in iterable:
                    yield item
                    self.update(1)

            def close(self):
                emit(self, force=True)
                return super().close()

        return HuggingFaceProgressTqdm

    def _download_tracker_cache_hit(self, tracker):
        tracker = tracker or {}
        if not tracker.get("saw_any"):
            return False
        return int(tracker.get("bytes_written") or 0) <= 0 and int(tracker.get("total_bytes") or 0) <= 0

    def _diffusers_snapshot_patterns(self, variant=""):
        base_patterns = [
            "*.json",
            "**/*.json",
            "*.txt",
            "**/*.txt",
            "*.model",
            "**/*.model",
            "*.spm",
            "**/*.spm",
            "*.py",
            "**/*.py",
        ]
        weight_extensions = ("safetensors", "bin")
        component_dirs = (
            "controlnet",
            "prior",
            "text_conditioner",
            "text_encoder",
            "text_encoder_2",
            "text_encoder_3",
            "transformer",
            "unet",
            "vae",
        )
        def component_weights(pattern):
            return [
                f"{component}/{pattern}"
                for component in component_dirs
            ] + [
                f"{component}/**/{pattern}"
                for component in component_dirs
            ]
        if variant:
            weights = []
            for ext in weight_extensions:
                weights.extend(component_weights(f"*.{variant}.{ext}"))
            return base_patterns + weights, None
        weights = []
        for ext in weight_extensions:
            weights.extend(component_weights(f"*.{ext}"))
        ignore = []
        for precision in ("fp16", "float16", "half", "bf16", "bfloat16", "fp32", "float32"):
            ignore.extend([f"*.{precision}.*", f"**/*.{precision}.*"])
        return base_patterns + weights, ignore

    def _prefetch_diffusers_snapshot(
        self,
        model_repo,
        *,
        variant="",
        progress_callback=None,
        base_percent=5,
        span_percent=20,
        log_capture=None,
    ):
        if not progress_callback:
            return ""
        backend = self._configure_huggingface_download_backend(log_capture=log_capture)
        try:
            from huggingface_hub import snapshot_download
        except Exception:
            return ""
        allow_patterns, ignore_patterns = self._diffusers_snapshot_patterns(variant)
        variant_text = f"（{variant}）" if variant else ""
        progress_callback({
            "phase": "downloading",
            "percent": base_percent,
            "step": "Hugging Face metadata / cache 檢查",
            "current_file": "",
            "detail": (
                f"準備下載 Hugging Face 模型 {model_repo}{variant_text}"
                + ("；已啟用 hf_transfer 加速" if backend["hf_transfer_enabled"] else "；使用 huggingface_hub 下載")
                + ("；已停用 hf_xet" if backend["xet_disabled"] else "")
            ),
            "token_used": bool(self.token),
            **backend,
        })
        download_tracker = {}
        tqdm_class = self._huggingface_progress_tqdm_class(
            progress_callback,
            label=f"{model_repo}{variant_text}",
            base_percent=base_percent,
            span_percent=span_percent,
            log_capture=log_capture,
            download_tracker=download_tracker,
        )
        kwargs = {
            "repo_id": model_repo,
            "token": (self.token or None),
            "allow_patterns": allow_patterns,
            "tqdm_class": tqdm_class,
        }
        if not self.keep_downloaded_models:
            transient_dir = self._new_transient_download_dir(f"snapshot_{model_repo.replace('/', '_')}")
            kwargs["local_dir"] = str(transient_dir)
            if log_capture:
                log_capture.register_transient_path(transient_dir)
                log_capture.append_line(f"Diffusers keep-downloaded-models disabled; using transient local_dir={transient_dir}")
        if ignore_patterns:
            kwargs["ignore_patterns"] = ignore_patterns
        try:
            heartbeat = (
                log_capture.heartbeat(
                    phase="downloading",
                    percent=base_percent,
                    step="Hugging Face 檔案下載",
                    detail=f"正在下載/檢查 Hugging Face 模型 {model_repo}{variant_text}",
                    extra_payload=lambda: {
                        **self._download_heartbeat_payload(download_tracker, model_repo=model_repo),
                        **backend,
                    },
                )
                if log_capture
                else nullcontext()
            )
            with heartbeat:
                snapshot_path = snapshot_download(**kwargs)
        except TypeError:
            kwargs.pop("tqdm_class", None)
            heartbeat = (
                log_capture.heartbeat(
                    phase="downloading",
                    percent=base_percent,
                    step="Hugging Face 檔案下載",
                    detail=f"正在下載/檢查 Hugging Face 模型 {model_repo}{variant_text}",
                    extra_payload=lambda: {
                        **self._download_heartbeat_payload(download_tracker, model_repo=model_repo),
                        **backend,
                    },
                )
                if log_capture
                else nullcontext()
            )
            with heartbeat:
                snapshot_path = snapshot_download(**kwargs)
        except Exception as exc:
            raise ComfyUIError(f"Hugging Face 模型下載失敗：{exc}") from exc
        cache_hit = self._download_tracker_cache_hit(download_tracker)
        if log_capture:
            suffix = " (cache hit; no network bytes reported)" if cache_hit else ""
            log_capture.append_line(f"Download complete: {model_repo}{variant_text}{suffix}")
        detail = (
            f"Hugging Face cache hit：{model_repo}{variant_text} 已在本機快取，未偵測到網路下載位元組，正在載入 pipeline"
            if cache_hit
            else f"Hugging Face 模型已下載到本機快取，正在載入 {model_repo}{variant_text}"
        )
        progress_callback({
            "phase": "loading",
            "percent": min(99, base_percent + span_percent),
            "step": "載入 Diffusers pipeline",
            "current_file": "",
            "detail": detail,
            "cache_hit": cache_hit,
            "bytes_written": int(download_tracker.get("bytes_written") or 0),
            "total_bytes": int(download_tracker.get("total_bytes") or 0),
        })
        return str(snapshot_path or "")

    def _pipeline_class(self, mode):
        if mode == "txt2img":
            from diffusers import DiffusionPipeline

            return DiffusionPipeline
        if mode == "img2img":
            from diffusers import AutoPipelineForImage2Image

            return AutoPipelineForImage2Image
        if mode == "inpaint":
            from diffusers import AutoPipelineForInpainting

            return AutoPipelineForInpainting
        raise ComfyUIError("Diffusers 後端目前只支援文字生圖、圖生圖與局部重繪")

    def _load_pipeline(
        self,
        mode,
        progress_callback=None,
        model_repo=None,
        variant="",
        gguf_file="",
        gguf_base_repo="",
        log_capture=None,
        force_device=None,
        cpu_fallback_attempted=False,
        model_card_hints=None,
    ):
        model_repo = self._effective_model_repo({"model": model_repo})
        variant = self._effective_model_variant({"diffusers_model_variant": variant})
        gguf_file = self._effective_gguf_file({"diffusers_gguf_file": gguf_file})
        if gguf_file:
            gguf_base_repo = self._effective_gguf_base_repo(
                {"diffusers_gguf_base_repo": gguf_base_repo, "generation_mode": mode},
                model_repo=model_repo,
            )
        self._ensure_configured(model_repo)
        if gguf_file:
            if log_capture:
                log_capture.append_line("Checking Python dependencies for GGUF Diffusers runtime", force=True)
            self._ensure_gguf_dependencies()
        else:
            if log_capture:
                log_capture.append_line("Checking Python dependencies: diffusers, torch, Pillow", force=True)
            self._ensure_dependencies()
        if log_capture:
            log_capture.append_line("Python dependency check complete")

        prefetched_gguf_path = ""
        prefetched_gguf_stats = None
        if gguf_file:
            backend = self._configure_huggingface_download_backend(log_capture=log_capture)
            if progress_callback:
                progress_callback({
                    "phase": "downloading",
                    "percent": 6,
                    "step": "下載 GGUF component",
                    "current_file": gguf_file,
                    "detail": (
                        f"準備下載 {gguf_file}"
                        + ("；已啟用 hf_transfer 加速" if backend["hf_transfer_enabled"] else "；使用 Hugging Face 串流下載")
                        + ("；已停用 hf_xet" if backend["xet_disabled"] else "")
                    ),
                    "token_used": bool(self.token),
                    **backend,
                })
            heartbeat = (
                log_capture.heartbeat(
                    phase="downloading",
                    percent=6,
                    step="Hugging Face GGUF 檔案下載",
                    detail=f"正在下載 GGUF 檔案 {gguf_file}",
                )
                if log_capture
                else nullcontext()
            )
            with heartbeat:
                prefetched_gguf_path, prefetched_gguf_stats = self._stream_huggingface_file_download(
                    model_repo,
                    gguf_file,
                    progress_callback=progress_callback,
                    log_capture=log_capture,
                    backend=backend,
                    base_percent=6,
                    span_percent=16,
                )
            self._raise_if_unsupported_diffusers_gguf(
                prefetched_gguf_path,
                gguf_file=gguf_file,
                progress_callback=progress_callback,
                log_capture=log_capture,
            )
            if log_capture:
                cache_hit = bool((prefetched_gguf_stats or {}).get("cache_hit"))
                suffix = " (cache hit)" if cache_hit else ""
                log_capture.append_line(f"Download complete: {model_repo}/{gguf_file}{suffix}", force=True)
            if progress_callback:
                progress_callback({
                    "phase": "loading",
                    "percent": 22,
                    "step": "準備載入 GGUF runtime",
                    "current_file": gguf_file,
                    "detail": "GGUF 檔案已準備完成，正在載入 torch / Diffusers runtime",
                    "cache_hit": bool((prefetched_gguf_stats or {}).get("cache_hit")),
                    "bytes_written": int((prefetched_gguf_stats or {}).get("bytes_written") or 0),
                    "total_bytes": int((prefetched_gguf_stats or {}).get("total_bytes") or 0),
                })

        if log_capture:
            log_capture.append_line("Importing torch", force=True)
        torch_import_heartbeat = (
            log_capture.heartbeat(
                phase="loading",
                percent=4,
                step="Importing torch",
                detail="正在載入 torch / CUDA runtime",
            )
            if log_capture
            else nullcontext()
        )
        with torch_import_heartbeat:
            import torch
        if log_capture:
            log_capture.append_line(f"Imported torch {getattr(torch, '__version__', 'unknown')}")

        if log_capture:
            log_capture.append_line("Resolving Diffusers device and dtype")
        device = force_device or self._resolve_device(torch)
        dtype = self._resolve_dtype(torch, device)
        hints = model_card_hints if isinstance(model_card_hints, dict) else {}
        if not gguf_file and self.dtype_setting == "auto" and hints.get("dtype"):
            hinted_dtype = self._dtype_from_hint(torch, hints.get("dtype"))
            if hinted_dtype is not None:
                dtype = hinted_dtype
                if log_capture:
                    log_capture.append_line(f"Applied model-card dtype hint: {hints.get('dtype')}", force=True)
        cuda_memory = self._log_cuda_memory(torch, log_capture=log_capture, progress_callback=progress_callback) if device == "cuda" else {}
        if self._should_fallback_to_cpu_for_low_vram(device, cuda_memory):
            if log_capture:
                log_capture.append_line(
                    "CUDA VRAM is below Diffusers auto threshold; falling back to CPU because root enabled CUDA fallback",
                    force=True,
                )
            if progress_callback:
                progress_callback({
                    "phase": "loading",
                    "percent": 5,
                    "step": "CUDA fallback to CPU",
                    "detail": (
                        "CUDA VRAM 低於 8GB，Diffusers auto 已改用 CPU；"
                        "root 可在快速設定改為 cuda 強制使用 GPU，或關閉 GPU 失敗自動 CPU。"
                    ),
                    "token_used": bool(self.token),
                    **cuda_memory,
                })
            device = "cpu"
            dtype = self._resolve_dtype(torch, device)
            cuda_memory = {}
        if log_capture:
            log_capture.append_line(
                f"Diffusers environment ready: repo={model_repo}, mode={mode}, device={device}, dtype={dtype}"
            )
        resolved_device_map = self._resolve_device_map(device, cuda_memory=cuda_memory)
        if (
            not gguf_file
            and self.device_map_setting == "auto"
            and str(hints.get("device_map") or "").strip().lower() in {"cuda", "auto", "balanced", "balanced_low_0", "sequential"}
            and self._accelerate_available()
        ):
            resolved_device_map = str(hints.get("device_map") or "").strip().lower()
            if log_capture:
                log_capture.append_line(f"Applied model-card device_map hint: {resolved_device_map}", force=True)
        cache_key = _PipelineCacheKey(
            model_repo=model_repo,
            variant=variant,
            gguf_file=gguf_file,
            gguf_base_repo=gguf_base_repo,
            mode=mode,
            device=device,
            dtype=str(dtype),
            device_map=resolved_device_map,
            low_cpu_mem_usage=bool(self.low_cpu_mem_usage),
            token_fingerprint=self._token_fingerprint(),
        )

        def retry_cpu_after_cuda_failure(exc, *, step):
            if device != "cuda" or cpu_fallback_attempted or not self.cuda_fallback_to_cpu:
                return None
            reason = str(exc or "CUDA backend failed")
            if log_capture:
                log_capture.append_line(f"{step}: CUDA failed; falling back to CPU. Reason: {reason}", force=True)
            if progress_callback:
                progress_callback({
                    "phase": "loading",
                    "percent": 24,
                    "step": "CUDA fallback to CPU",
                    "detail": f"{step} 失敗，已依 root 設定改用 CPU 重試：{reason}",
                    "token_used": bool(self.token),
                    **cuda_memory,
                })
            return self._load_pipeline(
                mode,
                progress_callback=progress_callback,
                model_repo=model_repo,
                variant=variant,
                gguf_file=gguf_file,
                gguf_base_repo=gguf_base_repo,
                log_capture=log_capture,
                force_device="cpu",
                cpu_fallback_attempted=True,
                model_card_hints=model_card_hints,
            )

        with self._pipeline_cache_lock:
            cached = self._pipeline_cache.get(cache_key)
            if cached is not None:
                if progress_callback:
                    progress_callback({
                        "phase": "loading",
                        "percent": 24,
                        "step": "使用已載入模型快取",
                        "detail": f"已使用記憶體中的 Diffusers pipeline：{model_repo}",
                        "current_file": "",
                    })
                return cached, torch, device
            if gguf_file:
                try:
                    pipe = self._load_gguf_pipeline(
                        mode,
                        model_repo=model_repo,
                        gguf_file=gguf_file,
                        gguf_base_repo=gguf_base_repo,
                        dtype=dtype,
                        device=device,
                        progress_callback=progress_callback,
                        log_capture=log_capture,
                        gguf_path=prefetched_gguf_path,
                        download_stats=prefetched_gguf_stats,
                    )
                except Exception as exc:
                    fallback_result = retry_cpu_after_cuda_failure(exc, step="GGUF Diffusers pipeline 載入")
                    if fallback_result is not None:
                        return fallback_result
                    raise
                self._pipeline_cache[cache_key] = pipe
                return pipe, torch, device
            if progress_callback:
                variant_text = f"（{variant}）" if variant else ""
                progress_callback({
                    "phase": "loading",
                    "percent": 5,
                    "step": "解析模型設定",
                    "detail": f"正在載入 Hugging Face 模型 {model_repo}{variant_text}",
                    "current_file": "",
                    "token_used": bool(self.token),
                })
            if log_capture:
                log_capture.append_line(f"Importing Diffusers pipeline class for mode={mode}", force=True)
            pipeline_import_heartbeat = (
                log_capture.heartbeat(
                    phase="loading",
                    percent=5,
                    step="Importing Diffusers pipeline class",
                    detail=f"正在載入 Diffusers pipeline class：{mode}",
                )
                if log_capture
                else nullcontext()
            )
            with pipeline_import_heartbeat:
                pipeline_cls = self._pipeline_class(mode)
            if log_capture:
                log_capture.append_line(f"Selected Diffusers pipeline class: {pipeline_cls.__name__}")
            dtype_kwarg = "dtype" if hints.get("dtype_kwarg") == "dtype" else "torch_dtype"
            kwargs = {
                dtype_kwarg: dtype,
                "use_safetensors": True,
            }
            device_map = resolved_device_map
            if device_map:
                kwargs["device_map"] = device_map
            if self.low_cpu_mem_usage and (device_map or self._accelerate_available()):
                kwargs["low_cpu_mem_usage"] = True
            if log_capture and self.device_map_setting not in {"auto", "disabled", "none"} and not device_map:
                log_capture.append_line(
                    f"Diffusers device_map={self.device_map_setting} requested but accelerate is unavailable; falling back to manual .to({device})",
                    force=True,
                )
            if variant:
                kwargs["variant"] = variant
            for key in ("revision", "subfolder", "custom_pipeline"):
                value = str(hints.get(key) or "").strip()
                if value:
                    kwargs[key] = value
            if hints.get("trust_remote_code") is True:
                kwargs["trust_remote_code"] = True
            if self.token:
                kwargs["token"] = self.token
            if log_capture:
                log_capture.append_line(
                    f"snapshot_download(repo_id={model_repo!r}, variant={variant or 'default'!r}, "
                    f"token={'set' if self.token else 'not_set'})",
                    force=True,
                )
            snapshot_path = self._prefetch_diffusers_snapshot(
                model_repo,
                variant=variant,
                progress_callback=progress_callback,
                base_percent=6,
                span_percent=18,
                log_capture=log_capture,
            )
            load_target = snapshot_path or model_repo
            if snapshot_path:
                kwargs["local_files_only"] = True
            if log_capture:
                source = "local snapshot" if snapshot_path else "remote repo"
                log_capture.append_line(
                    f"{pipeline_cls.__name__}.from_pretrained({source}, {dtype_kwarg}={dtype}, "
                    f"local_files_only={bool(snapshot_path)}, device_map={kwargs.get('device_map') or 'none'}, "
                    f"low_cpu_mem_usage={bool(kwargs.get('low_cpu_mem_usage'))})",
                    force=True,
                )
            if progress_callback:
                progress_callback({
                    "phase": "loading",
                    "percent": 24,
                    "step": "載入 Diffusers pipeline",
                    "current_file": "",
                    "detail": (
                        f"正在載入 {pipeline_cls.__name__} 權重：{model_repo}{f'（{variant}）' if variant else ''}"
                        + (f"，device_map={kwargs.get('device_map')}" if kwargs.get("device_map") else "")
                        + (f"，low_cpu_mem_usage={bool(kwargs.get('low_cpu_mem_usage'))}")
                        + (
                            f"，VRAM {_format_bytes(cuda_memory.get('cuda_free_bytes'))} / {_format_bytes(cuda_memory.get('cuda_total_bytes'))}"
                            if cuda_memory.get("cuda_total_bytes")
                            else ""
                        )
                    ),
                    "token_used": bool(self.token),
                    **cuda_memory,
                })
            load_heartbeat = (
                log_capture.heartbeat(
                    phase="loading",
                    percent=24,
                    step="載入 Diffusers pipeline",
                    detail=f"正在載入 {pipeline_cls.__name__} 權重",
                    extra_payload=lambda: self._cuda_memory_payload(torch) if device == "cuda" else {},
                )
                if log_capture
                else nullcontext()
            )
            with load_heartbeat:
                try:
                    pipe = pipeline_cls.from_pretrained(load_target, **kwargs)
                except TypeError:
                    if self.token:
                        kwargs["use_auth_token"] = kwargs.pop("token")
                    try:
                        pipe = pipeline_cls.from_pretrained(load_target, **kwargs)
                    except Exception as exc:
                        fallback_result = retry_cpu_after_cuda_failure(exc, step="Diffusers 模型載入")
                        if fallback_result is not None:
                            return fallback_result
                        raise
                    except Exception:
                        fallback_kwargs = dict(kwargs)
                        fallback_kwargs.pop("use_safetensors", None)
                        try:
                            pipe = pipeline_cls.from_pretrained(load_target, **fallback_kwargs)
                        except Exception as exc:
                            fallback_result = retry_cpu_after_cuda_failure(exc, step="Diffusers 模型載入")
                            if fallback_result is not None:
                                return fallback_result
                            if not snapshot_path:
                                raise ComfyUIError(f"Diffusers 模型載入失敗：{exc}") from exc
                            if progress_callback:
                                progress_callback({
                                    "phase": "downloading",
                                    "percent": 24,
                                    "step": "補齊 Diffusers 缺漏檔案",
                                    "current_file": "",
                                    "detail": "本機快取缺少部分檔案，改由 Diffusers 補齊 Hugging Face 檔案。",
                                    "token_used": bool(self.token),
                                })
                            remote_kwargs = dict(kwargs)
                            remote_kwargs.pop("local_files_only", None)
                            try:
                                pipe = pipeline_cls.from_pretrained(model_repo, **remote_kwargs)
                            except TypeError:
                                if self.token and "token" in remote_kwargs:
                                    remote_kwargs["use_auth_token"] = remote_kwargs.pop("token")
                                try:
                                    pipe = pipeline_cls.from_pretrained(model_repo, **remote_kwargs)
                                except Exception as remote_exc:
                                    fallback_result = retry_cpu_after_cuda_failure(remote_exc, step="Diffusers 遠端補檔載入")
                                    if fallback_result is not None:
                                        return fallback_result
                                    raise ComfyUIError(f"Diffusers 模型載入失敗：{remote_exc}") from remote_exc
                            except Exception:
                                remote_fallback_kwargs = dict(remote_kwargs)
                                remote_fallback_kwargs.pop("use_safetensors", None)
                                try:
                                    pipe = pipeline_cls.from_pretrained(model_repo, **remote_fallback_kwargs)
                                except Exception as remote_exc:
                                    fallback_result = retry_cpu_after_cuda_failure(remote_exc, step="Diffusers 遠端補檔載入")
                                    if fallback_result is not None:
                                        return fallback_result
                                    raise ComfyUIError(f"Diffusers 模型載入失敗：{remote_exc}") from remote_exc
            try:
                if kwargs.get("device_map") or getattr(pipe, "hf_device_map", None):
                    if progress_callback:
                        progress_callback({
                            "phase": "loading",
                            "percent": 24,
                            "step": f"模型已分配到 {device}",
                            "current_file": "",
                            "detail": f"Diffusers pipeline 已透過 device_map 載入到 {device}",
                        })
                else:
                    if progress_callback:
                        progress_callback({
                            "phase": "loading",
                            "percent": 24,
                            "step": f"移動模型到 {device}",
                            "current_file": "",
                            "detail": f"Diffusers pipeline 已載入，正在移至 {device}",
                        })
                    pipe.to(device)
            except Exception as exc:
                fallback_result = retry_cpu_after_cuda_failure(exc, step=f"Diffusers 模型移至 {device}")
                if fallback_result is not None:
                    return fallback_result
                raise ComfyUIError(f"Diffusers 模型移至裝置 {device} 失敗：{exc}") from exc
            if hasattr(pipe, "set_progress_bar_config"):
                try:
                    pipe.set_progress_bar_config(disable=False)
                except Exception:
                    pass
            if hasattr(pipe, "enable_attention_slicing"):
                try:
                    pipe.enable_attention_slicing()
                except Exception:
                    pass
            self._pipeline_cache[cache_key] = pipe
            return pipe, torch, device

    def _load_gguf_pipeline(
        self,
        mode,
        *,
        model_repo,
        gguf_file,
        gguf_base_repo,
        dtype,
        device,
        progress_callback=None,
        log_capture=None,
        gguf_path="",
        download_stats=None,
    ):
        if mode != "txt2img":
            raise ComfyUIError("GGUF Diffusers 模式目前只支援文字生圖；圖生圖與局部重繪請改用一般 Diffusers repo 或 ComfyUI workflow。")
        if progress_callback:
            progress_callback({
                "phase": "loading",
                "percent": 5,
                "step": "準備 GGUF component",
                "current_file": gguf_file,
                "detail": f"正在下載/載入 GGUF 檔案 {gguf_file}",
                "token_used": bool(self.token),
            })
        backend = self._configure_huggingface_download_backend(log_capture=log_capture)
        try:
            from diffusers import GGUFQuantizationConfig
        except Exception as exc:
            raise ComfyUIError(f"GGUF 依賴載入失敗：{exc}") from exc
        try:
            if gguf_path:
                download_stats = dict(download_stats or {})
                cache_hit = bool(download_stats.get("cache_hit"))
                if progress_callback:
                    progress_callback({
                        "phase": "loading",
                        "percent": 22,
                        "step": "載入 GGUF pipeline",
                        "current_file": gguf_file,
                        "detail": f"GGUF 檔案已準備完成，正在載入 {gguf_file}",
                        "cache_hit": cache_hit,
                        "bytes_written": int(download_stats.get("bytes_written") or 0),
                        "total_bytes": int(download_stats.get("total_bytes") or 0),
                    })
            elif progress_callback:
                progress_callback({
                    "phase": "downloading",
                    "percent": 6,
                    "step": "下載 GGUF component",
                    "current_file": gguf_file,
                    "detail": (
                        f"準備下載 {gguf_file}"
                        + ("；已啟用 hf_transfer 加速" if backend["hf_transfer_enabled"] else "；使用 huggingface_hub 下載")
                        + ("；已停用 hf_xet" if backend["xet_disabled"] else "")
                    ),
                    "token_used": bool(self.token),
                    **backend,
                })
            if not gguf_path:
                heartbeat = (
                    log_capture.heartbeat(
                        phase="downloading",
                        percent=6,
                        step="Hugging Face GGUF 檔案下載",
                        detail=f"正在下載 GGUF 檔案 {gguf_file}",
                    )
                    if log_capture
                    else nullcontext()
                )
                with heartbeat:
                    gguf_path, download_stats = self._stream_huggingface_file_download(
                        model_repo,
                        gguf_file,
                        progress_callback=progress_callback,
                        log_capture=log_capture,
                        backend=backend,
                        base_percent=6,
                        span_percent=16,
                    )
                self._raise_if_unsupported_diffusers_gguf(
                    gguf_path,
                    gguf_file=gguf_file,
                    progress_callback=progress_callback,
                    log_capture=log_capture,
                )
                cache_hit = bool(download_stats.get("cache_hit"))
                if log_capture:
                    suffix = " (cache hit; no network bytes reported)" if cache_hit else ""
                    log_capture.append_line(f"Download complete: {model_repo}/{gguf_file}{suffix}")
                if progress_callback:
                    progress_callback({
                        "phase": "loading",
                        "percent": 22,
                        "step": "載入 GGUF pipeline",
                        "current_file": gguf_file,
                        "detail": (
                            f"Hugging Face cache hit：{gguf_file} 已在本機快取，未偵測到網路下載位元組，正在載入 GGUF pipeline"
                            if cache_hit
                            else f"GGUF 檔案已下載到本機快取，正在載入 {gguf_file}"
                        ),
                        "cache_hit": cache_hit,
                        "bytes_written": int(download_stats.get("bytes_written") or 0),
                        "total_bytes": int(download_stats.get("total_bytes") or 0),
                    })
        except ComfyUIError:
            raise
        except Exception as exc:
            raise ComfyUIError(f"GGUF 檔案下載失敗，尚未載入模型：{exc}") from exc

        text = f"{model_repo}/{gguf_file}/{gguf_base_repo}".lower()
        quantization_config = GGUFQuantizationConfig(compute_dtype=dtype)
        common_kwargs = {"torch_dtype": dtype}
        if self.token:
            common_kwargs["token"] = self.token
        try:
            load_heartbeat = (
                log_capture.heartbeat(
                    phase="loading",
                    percent=24,
                    step="載入 GGUF pipeline",
                    detail=f"正在載入 GGUF component {gguf_file}",
                )
                if log_capture
                else nullcontext()
            )
            with load_heartbeat:
                if "flux" in text:
                    from diffusers import FluxPipeline, FluxTransformer2DModel

                    transformer = FluxTransformer2DModel.from_single_file(
                        gguf_path,
                        quantization_config=quantization_config,
                        config=gguf_base_repo,
                        subfolder="transformer",
                        torch_dtype=dtype,
                    )
                    pipe = FluxPipeline.from_pretrained(gguf_base_repo, transformer=transformer, **common_kwargs)
                else:
                    from diffusers import StableDiffusionXLPipeline, UNet2DConditionModel

                    unet = UNet2DConditionModel.from_single_file(
                        gguf_path,
                        quantization_config=quantization_config,
                        config=gguf_base_repo,
                        subfolder="unet",
                        torch_dtype=dtype,
                    )
                    pipe = StableDiffusionXLPipeline.from_pretrained(gguf_base_repo, unet=unet, **common_kwargs)
        except Exception as exc:
            raise ComfyUIError(
                "GGUF 模型載入失敗：Diffusers GGUF 需要可用的 base Diffusers repo 與相容的 GGUF component。"
                f"目前 base={gguf_base_repo}，檔案={gguf_file}。原始錯誤：{exc}"
            ) from exc
        try:
            pipe.to(device)
        except Exception as exc:
            raise ComfyUIError(f"GGUF pipeline 移至裝置 {device} 失敗：{exc}") from exc
        if hasattr(pipe, "set_progress_bar_config"):
            try:
                pipe.set_progress_bar_config(disable=False)
            except Exception:
                pass
        return pipe

    def _load_ref_image(self, image_ref, *, mode="RGB", size=None):
        from PIL import Image, ImageOps

        image = self.fetch_image(image_ref)
        try:
            loaded = Image.open(io.BytesIO(image.data))
            loaded = ImageOps.exif_transpose(loaded).convert(mode)
        except Exception as exc:
            raise ComfyUIError(f"Diffusers 圖片讀取失敗：{exc}") from exc
        if size:
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            loaded = loaded.resize(size, resampling)
        return loaded

    def _save_output_image(self, image, *, index=0):
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        filename = f"hackme_web_diffusers_{uuid.uuid4().hex}_{index + 1:02d}.png"
        path = self._dir_for_type("output") / filename
        path.write_bytes(data)
        return {
            "image_ref": {"filename": filename, "subfolder": "", "type": "output"},
            "mime_type": "image/png",
            "data": data,
        }

    def generate_image(self, params, *, timeout_seconds=1800, progress_callback=None, extra_data=None):
        self._ensure_in_process_runtime_allowed()
        download_backend = self._configure_huggingface_download_backend()
        params = params or {}
        mode = str(params.get("generation_mode") or "txt2img").strip().lower()
        if mode not in {"txt2img", "img2img", "inpaint"}:
            raise ComfyUIError("Diffusers 後端目前只支援文字生圖、圖生圖與局部重繪")
        if params.get("loras"):
            raise ComfyUIError("Diffusers 後端目前不支援本站 ComfyUI LoRA 選擇，請改用 ComfyUI 本地/遠端模式")
        if params.get("controlnet"):
            raise ComfyUIError("Diffusers 後端目前不支援本站 ControlNet 快捷模式，請改用 ComfyUI 本地/遠端模式")
        model_repo = self._effective_model_repo(params)
        gguf_file = self._effective_gguf_file(params)
        if gguf_file:
            variant = ""
        else:
            variant = self._resolve_diffusers_variant(model_repo, explicit_variant=self._effective_model_variant(params), mode=mode)
        gguf_base_repo = ""
        if gguf_file:
            gguf_base_repo = self._effective_gguf_base_repo(params, model_repo=model_repo)
            variant = ""
        params["diffusers_model_repo"] = model_repo
        params["model"] = model_repo
        params["diffusers_model_variant"] = variant
        params["diffusers_gguf_file"] = gguf_file
        params["diffusers_gguf_base_repo"] = gguf_base_repo
        runtime_logs = _DiffusersRuntimeLogCapture(progress_callback)
        job_progress = runtime_logs.progress if progress_callback else None
        if job_progress:
            selected_label = gguf_file or variant or "default"
            runtime_logs.append_line(
                f"Diffusers Python runtime starting: repo={model_repo}, mode={mode}, variant={selected_label}"
            )
            job_progress({
                "phase": "downloading",
                "percent": 3,
                "backend_kind": "diffusers",
                "step": "準備 Diffusers model",
                "current_file": gguf_file or "",
                "detail": f"下載 Diffusers model：{model_repo}（{selected_label}），正在檢查 Hugging Face cache / metadata",
                "token_used": bool(self.token),
                **download_backend,
            })
        try:
            with runtime_logs:
                pipe, torch, device = self._load_pipeline(
                    mode,
                    progress_callback=job_progress,
                    model_repo=model_repo,
                    variant=variant,
                    gguf_file=gguf_file,
                    gguf_base_repo=gguf_base_repo,
                    log_capture=runtime_logs,
                    model_card_hints=params.get("diffusers_model_card_hints"),
                )
        except ComfyUIError as exc:
            error_step = (
                "GGUF 格式不支援 Diffusers backend"
                if "ComfyUI-GGUF 原生 UNet GGUF" in str(exc)
                else "Diffusers 模型載入失敗"
            )
            runtime_logs.error(str(exc), step=error_step)
            runtime_logs.cleanup_transient_paths()
            raise
        except Exception as exc:
            runtime_logs.error(f"Diffusers 模型載入失敗：{exc}", step="Diffusers 模型載入失敗")
            runtime_logs.cleanup_transient_paths()
            raise
        width = max(64, int(params.get("width") or 1024))
        height = max(64, int(params.get("height") or 1024))
        size = (width, height)
        prompt = str(params.get("prompt") or "").strip()
        negative_prompt = str(params.get("negative_prompt") or "").strip()
        batch_size = max(1, int(params.get("batch_size") or 1))
        seed = int(params.get("seed") or 0)
        generator_device = "cuda" if device == "cuda" else "cpu"
        generator = torch.Generator(device=generator_device)
        generator.manual_seed(seed)
        call_kwargs = {
            "prompt": prompt,
            "num_inference_steps": max(1, int(params.get("steps") or 20)),
            "guidance_scale": float(params.get("cfg") or 7.0),
            "num_images_per_prompt": batch_size,
            "generator": generator,
        }
        if negative_prompt:
            call_kwargs["negative_prompt"] = negative_prompt
        if mode == "txt2img":
            call_kwargs.update({"width": width, "height": height})
        elif mode == "img2img":
            source_ref = params.get("source_image_ref")
            if not source_ref:
                runtime_logs.cleanup_transient_paths()
                raise ComfyUIError("Diffusers 圖生圖需要來源圖片")
            call_kwargs["image"] = self._load_ref_image(source_ref, size=size)
            call_kwargs["strength"] = float(params.get("denoise_strength") or 0.65)
        elif mode == "inpaint":
            source_ref = params.get("source_image_ref")
            mask_ref = params.get("mask_image_ref")
            if not source_ref or not mask_ref:
                runtime_logs.cleanup_transient_paths()
                raise ComfyUIError("Diffusers 局部重繪需要來源圖片與遮罩圖片")
            call_kwargs["image"] = self._load_ref_image(source_ref, size=size)
            call_kwargs["mask_image"] = self._load_ref_image(mask_ref, mode="L", size=size)
            call_kwargs["strength"] = float(params.get("denoise_strength") or 0.65)
        if job_progress:
            job_progress({
                "phase": "running",
                "percent": 25,
                "backend_kind": "diffusers",
                "step": f"Diffusers {mode} 推論",
                "current_file": "",
                "detail": f"Diffusers 推論中：steps={call_kwargs['num_inference_steps']}，device={device}",
            })
        try:
            with runtime_logs:
                with runtime_logs.heartbeat(
                    phase="running",
                    percent=25,
                    step=f"Diffusers {mode} 推論",
                    detail=f"Diffusers 推論中：steps={call_kwargs['num_inference_steps']}，device={device}",
                    extra_payload=lambda: self._cuda_memory_payload(torch) if device == "cuda" else {},
                ):
                    with torch.inference_mode():
                        output = pipe(**call_kwargs)
        except TypeError as exc:
            runtime_logs.error(f"Diffusers pipeline 參數不相容：{exc}", step="Diffusers pipeline 參數錯誤")
            runtime_logs.cleanup_transient_paths()
            raise ComfyUIError(f"Diffusers pipeline 參數不相容：{exc}") from exc
        except Exception as exc:
            if device == "cuda" and self.cuda_fallback_to_cpu:
                runtime_logs.append_line(f"Diffusers inference failed on CUDA; falling back to CPU. Reason: {exc}", force=True)
                if job_progress:
                    job_progress({
                        "phase": "loading",
                        "percent": 24,
                        "backend_kind": "diffusers",
                        "step": "CUDA fallback to CPU",
                        "detail": f"CUDA 推論失敗，已依 root 設定改用 CPU 重試：{exc}",
                    })
                try:
                    with runtime_logs:
                        pipe, torch, device = self._load_pipeline(
                            mode,
                            progress_callback=job_progress,
                            model_repo=model_repo,
                            variant=variant,
                            gguf_file=gguf_file,
                            gguf_base_repo=gguf_base_repo,
                            log_capture=runtime_logs,
                            force_device="cpu",
                            cpu_fallback_attempted=True,
                        )
                    generator = torch.Generator(device="cpu")
                    generator.manual_seed(seed)
                    call_kwargs["generator"] = generator
                    with runtime_logs:
                        with runtime_logs.heartbeat(
                            phase="running",
                            percent=25,
                            step=f"Diffusers {mode} CPU 推論",
                            detail=f"CUDA 失敗後改用 CPU 推論：steps={call_kwargs['num_inference_steps']}",
                        ):
                            with torch.inference_mode():
                                output = pipe(**call_kwargs)
                except Exception as cpu_exc:
                    runtime_logs.error(f"Diffusers CPU fallback 產圖失敗：{cpu_exc}", step="Diffusers CPU fallback 失敗")
                    runtime_logs.cleanup_transient_paths()
                    raise ComfyUIError(f"Diffusers 產圖失敗，CPU fallback 也失敗：{cpu_exc}") from cpu_exc
            else:
                runtime_logs.error(f"Diffusers 產圖失敗：{exc}", step="Diffusers 推論失敗")
                runtime_logs.cleanup_transient_paths()
                raise ComfyUIError(f"Diffusers 產圖失敗：{exc}") from exc
        generated_images = list(getattr(output, "images", []) or [])
        if not generated_images:
            runtime_logs.error("Diffusers 產圖完成但沒有輸出圖片", step="Diffusers 輸出檢查失敗")
            runtime_logs.cleanup_transient_paths()
            raise ComfyUIError("Diffusers 產圖完成但沒有輸出圖片")
        if job_progress:
            job_progress({
                "phase": "running",
                "percent": 92,
                "backend_kind": "diffusers",
                "step": "保存輸出圖片",
                "current_file": "",
                "detail": f"Diffusers 推論完成，正在保存 {len(generated_images)} 張圖片",
            })
        try:
            images = [self._save_output_image(image, index=index) for index, image in enumerate(generated_images)]
        except Exception:
            runtime_logs.cleanup_transient_paths()
            raise
        runtime_logs.cleanup_transient_paths()
        prompt_id = f"diffusers-{uuid.uuid4().hex}"
        if job_progress:
            job_progress({"phase": "completed", "percent": 100, "backend_kind": "diffusers", "completed": True, "detail": f"Diffusers 已完成，共 {len(images)} 張"})
        return {
            "prompt_id": prompt_id,
            "image_ref": images[0]["image_ref"],
            "mime_type": images[0]["mime_type"],
            "data": images[0]["data"],
            "images": images,
        }
