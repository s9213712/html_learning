import base64
import hashlib
import inspect
import json
import mimetypes
import os
import ipaddress
import re
import secrets
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse
import urllib.error
import urllib.request

from routes.comfyui_sections.admin_helpers import build_comfyui_admin_helpers
from routes.comfyui_sections.billing_helpers import build_comfyui_billing_helpers
from routes.comfyui_sections import (
    register_comfyui_admin_routes,
    register_comfyui_image_routes,
    register_comfyui_runtime_routes,
    register_comfyui_template_routes,
    register_comfyui_workflow_routes,
)

from flask import request, send_file

from services.comfyui.settings import (
    DEFAULT_COMFYUI_PORT,
    DEFAULT_COMFYUI_REMOTE_API_URL,
    normalize_comfyui_connection_mode,
    normalize_huggingface_repo_id,
    validate_comfyui_api_host,
    validate_comfyui_api_port,
    validate_comfyui_api_url,
)
from services.comfyui.api_nodes import build_comfyui_account_extra_data, detect_paid_api_nodes
from services.comfyui.node_catalog import build_node_catalog
from services.storage.cloud_drive import (
    attach_existing_file,
    can_download_file,
    ensure_cloud_drive_attachment_schema,
    resolve_file_storage_path,
    store_cloud_upload,
)
from services.platform.admin_validation import (
    validate_comfyui_api_host as shared_validate_comfyui_api_host,
    validate_comfyui_api_url as shared_validate_comfyui_api_url,
)
from services.comfyui.client import (
    CONTROLNET_TYPE_DEFINITIONS,
    GENERATION_MODE_DEFINITIONS,
    ComfyUIClient,
    ComfyUIError,
)
from services.comfyui.files import normalize_file_ref as normalize_comfyui_file_ref
from services.comfyui.gguf_profiles import (
    gguf_profile_unavailable_message,
    installed_gguf_inventory,
    public_gguf_profiles,
    resolve_official_gguf_selection,
)
from services.comfyui.huggingface import normalize_diffusers_variant, normalize_huggingface_repo_file
from services.comfyui.workflow.compat import apply_workflow_compatibility_fixes
from services.comfyui.workflows import (
    WorkflowValidationError,
    extract_workflow_summary,
    sanitize_workflow_json,
    workflow_json_to_pretty_text,
)
from services.comfyui.template import (
    analyze_workflow_json,
    build_ui_schema,
    check_workflow_capability,
    embedding_option_available,
    model_option_available,
    resolve_model_option,
    runtime_comfyui_dir,
)
from services.comfyui.template.seeding import SYSTEM_WORKFLOW_IDS
from services.comfyui.validation.rules import (
    WORKFLOW_ABSOLUTE_PATH_RE,
    WORKFLOW_BLOCKED_COMMAND_RE,
    WORKFLOW_URL_RE,
)
from services.platform.release_info import APP_RELEASE_ID
from services.system.notifications import create_notification_if_enabled
from services.storage.storage_albums import (
    add_album_file,
    create_storage_file_entry,
    ensure_output_album,
    ensure_storage_album_schema,
)


DEFAULT_COMFYUI_URL = DEFAULT_COMFYUI_REMOTE_API_URL
COMFYUI_LOCAL_START_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "comfyui" / "comfyui_run_in_linux.template.sh"
SAFE_SAMPLER_FALLBACK = "euler"
SAFE_SCHEDULER_FALLBACK = "normal"


def _env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _env_timeout_seconds(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except Exception:
        value = default
    if value <= 0:
        return 0
    return max(minimum, min(maximum, value))


def _env_float(name, default, minimum, maximum):
    try:
        value = float(os.environ.get(name, str(default)))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


DEFAULT_GENERATION_TIMEOUT_SECONDS = _env_timeout_seconds("COMFYUI_GENERATION_TIMEOUT_SECONDS", 0, 30, 6 * 3600)
MAX_GENERATION_TIMEOUT_SECONDS = _env_int("COMFYUI_GENERATION_MAX_TIMEOUT_SECONDS", 6 * 3600, 60, 24 * 3600)
COMFYUI_BACKEND_REQUEST_TIMEOUT_SECONDS = _env_int("COMFYUI_BACKEND_REQUEST_TIMEOUT_SECONDS", 8, 2, 30)
COMFYUI_STATUS_TIMEOUT_SECONDS = _env_float("COMFYUI_STATUS_TIMEOUT_SECONDS", 2.0, 0.5, 10.0)
COMFYUI_INTERRUPT_TIMEOUT_SECONDS = _env_float("COMFYUI_INTERRUPT_TIMEOUT_SECONDS", 2.0, 0.5, 10.0)
COMFYUI_JOB_STALE_SECONDS = _env_float("COMFYUI_JOB_STALE_SECONDS", 1500.0, 10.0, 4 * 3600.0)
COMFYUI_BACKEND_UNRESPONSIVE_FAIL_SECONDS = _env_float(
    "COMFYUI_BACKEND_UNRESPONSIVE_FAIL_SECONDS",
    7200.0,
    60.0,
    24 * 3600.0,
)
COMFYUI_JOB_PROGRESS_DB_THROTTLE_SECONDS = _env_float("COMFYUI_JOB_PROGRESS_DB_THROTTLE_SECONDS", 2.0, 0.0, 30.0)
COMFYUI_BASIC_PRICE_ITEM_KEY = "comfyui_txt2img_basic"
MAX_COMFYUI_FETCH_IMAGE_BYTES = 50 * 1024 * 1024
MAX_COMFYUI_FETCH_VIDEO_BYTES = 512 * 1024 * 1024
MAX_COMFYUI_LORAS_PER_PROMPT = 8
COMFYUI_LORA_EXTRA_PRICE_POINTS = 1
COMFYUI_VAE_BUILTIN = "__checkpoint_builtin__"
COMFYUI_MODEL_DOWNLOAD_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"}
COMFYUI_MODEL_DOWNLOAD_TYPES = {
    "checkpoint": ("checkpoints", "Checkpoint"),
    "diffusion_model": ("diffusion_models", "Diffusion Model / UNet"),
    "unet": ("diffusion_models", "Diffusion Model / UNet"),
    "text_encoder": ("text_encoders", "Text Encoder / CLIP / T5 / Qwen"),
    "clip": ("clip", "CLIP / Text Encoder (legacy)"),
    "clip_vision": ("clip_vision", "CLIP Vision"),
    "lora": ("loras", "LoRA"),
    "embedding": ("embeddings", "Embedding / Textual Inversion"),
    "vae": ("vae", "VAE"),
    "audio": ("audio", "Audio / TTS"),
    "video": ("video", "Video / Motion"),
    "controlnet": ("controlnet", "ControlNet"),
    "upscale": ("upscale_models", "放大模型"),
    "latent_upscale": ("latent_upscale_models", "Latent 放大模型"),
}
COMFYUI_SUPPORTED_LORA_BASE_MODEL_FAMILIES = {"sdxl", "pony", "illustrious", "noob"}
MAX_COMFYUI_MODEL_DOWNLOAD_BYTES = int(os.environ.get("COMFYUI_MODEL_DOWNLOAD_MAX_BYTES", str(20 * 1024 * 1024 * 1024)))
CIVITAI_ALLOWED_HOSTS = {
    "civitai.com",
    "www.civitai.com",
    "civitai.red",
    "www.civitai.red",
    "civitai.green",
    "www.civitai.green",
}
CIVITAI_API_BASE = os.environ.get("CIVITAI_API_BASE", "https://civitai.com/api/v1").rstrip("/")
def _configured_civitai_api_bases():
    raw = os.environ.get("CIVITAI_API_BASES", "")
    if raw.strip():
        candidates = [item.strip().rstrip("/") for item in raw.split(",")]
    else:
        candidates = [CIVITAI_API_BASE, "https://civitai.red/api/v1"]
    bases = []
    seen = set()
    for item in candidates:
        if not item or item in seen:
            continue
        parsed = urlparse(item)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in CIVITAI_ALLOWED_HOSTS:
            continue
        bases.append(item)
        seen.add(item)
    return bases or [CIVITAI_API_BASE]

CIVITAI_API_BASES = _configured_civitai_api_bases()
CIVITAI_MODEL_TYPE_TO_DOWNLOAD_TYPE = {
    "checkpoint": "checkpoint",
    "model": "checkpoint",
    "diffusionmodel": "diffusion_model",
    "diffusion_model": "diffusion_model",
    "unet": "diffusion_model",
    "textencoder": "text_encoder",
    "text_encoder": "text_encoder",
    "clip": "clip",
    "clipvision": "clip_vision",
    "clip_vision": "clip_vision",
    "lora": "lora",
    "textualinversion": "embedding",
    "embedding": "embedding",
    "vae": "vae",
    "audio": "audio",
    "video": "video",
    "controlnet": "controlnet",
    "upscaler": "upscale",
    "upscale": "upscale",
    "latentupscale": "latent_upscale",
    "latentupscaler": "latent_upscale",
    "latentupscalemodel": "latent_upscale",
    "latent_upscale": "latent_upscale",
}
CIVITAI_SEARCH_TYPE_TO_API = {
    "checkpoint": "Checkpoint",
    "lora": "LORA",
    "embedding": "TextualInversion",
    "controlnet": "Controlnet",
    "upscale": "Upscaler",
}
COMFYUI_EMBEDDING_TOKEN_RE = re.compile(r"<\s*embeddings\s*:\s*([^<>]+?)\s*>", re.IGNORECASE)
COMFYUI_ALLOWED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
}
COMFYUI_ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
COMFYUI_ALLOWED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-matroska",
    "video/x-msvideo",
    "application/octet-stream",
}
COMFYUI_ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
COMFYUI_HISTORY_LIMIT = 20
COMFYUI_WORKFLOW_PRESET_LIMIT = 50
COMFYUI_WORKFLOW_RUN_LIMIT = 8
COMFYUI_WORKFLOW_VISIBILITY_VALUES = {"private", "public"}
COMFYUI_WORKFLOW_PURPOSE_VALUES = {
    "txt2img",
    "img2img",
    "inpaint",
    "outpaint",
    "upscale",
    "t2v",
    "i2v",
    "v2v",
    "t2s",
    "t2sv",
    "controlnet",
    "custom",
}
COMFYUI_WORKFLOW_SCHEMA_VERSION = "1"
COMFYUI_WORKFLOW_LAYOUT_MAX_JSON_BYTES = 64_000
COMFYUI_CORE_WORKFLOW_NODE_CLASSES = {
    "CheckpointLoaderSimple",
    "CLIPTextEncode",
    "CLIPSetLastLayer",
    "ConditioningConcat",
    "ConditioningAverage",
    "ConditioningCombine",
    "ConditioningZeroOut",
    "VAELoader",
    "VAEEncode",
    "VAEEncodeForInpaint",
    "VAEDecode",
    "KSampler",
    "KSamplerAdvanced",
    "EmptyLatentImage",
    "LatentUpscale",
    "LatentUpscaleBy",
    "LoadImage",
    "LoadImageMask",
    "SaveImage",
    "PreviewImage",
    "LoraLoader",
    "ControlNetLoader",
    "ControlNetApply",
    "ControlNetApplyAdvanced",
    "ImagePadForOutpaint",
    "UpscaleModelLoader",
    "ImageUpscaleWithModel",
}


class _MemoryFile:
    def __init__(self, data, filename, mimetype):
        self.stream = BytesIO(data)
        self.filename = filename
        self.mimetype = mimetype


def register_comfyui_routes(app, deps):
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_db = deps["get_db"]
    get_member_level_rule = deps["get_member_level_rule"]
    get_client_ip = deps.get("get_client_ip", lambda: "")
    get_ua = deps.get("get_ua", lambda: "")
    audit = deps.get("audit", lambda *args, **kwargs: None)
    json_resp = deps["json_resp"]
    require_csrf = deps.get("require_csrf", deps["require_csrf_safe"])
    require_csrf_safe = deps["require_csrf_safe"]
    storage_root = deps.get("STORAGE_DIR", ".")
    get_system_settings = deps.get("get_system_settings", lambda: {})
    injected_client = deps.get("comfyui_client")
    points_service = deps.get("points_service")
    active_generations = deps.get("comfyui_active_generations") or {}
    active_generations_lock = deps.get("comfyui_active_generations_lock") or threading.Lock()
    generation_jobs = deps.get("comfyui_generation_jobs") or {}
    generation_jobs_lock = deps.get("comfyui_generation_jobs_lock") or threading.Lock()
    generation_jobs_schema_lock = threading.Lock()
    generation_jobs_schema_ready = {"ready": False}
    model_download_jobs = deps.get("comfyui_model_download_jobs") or {}
    model_download_jobs_lock = deps.get("comfyui_model_download_jobs_lock") or threading.Lock()

    def _actor_or_401():
        actor = get_current_user_ctx()
        if not actor:
            return None, json_resp({"ok": False, "msg": "請先登入"}, 401)
        settings = get_system_settings() or {}
        if settings.get("feature_comfyui_enabled") is False:
            return None, json_resp({"ok": False, "msg": "ComfyUI 產圖功能目前未啟用"}, 403)
        return actor, None

    def _actor_value(actor, key, default=None):
        if not actor:
            return default
        try:
            return actor[key]
        except Exception:
            return actor.get(key, default) if hasattr(actor, "get") else default

    def _image_ref_payload(image_ref):
        try:
            payload = normalize_comfyui_file_ref(image_ref, error_cls=ValueError, empty_label="ComfyUI 圖片")
        except Exception:
            return None
        return payload

    def _image_ref_key(image_ref):
        payload = _image_ref_payload(image_ref)
        if payload is None:
            return None
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _ensure_comfyui_image_ref_schema(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comfyui_image_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_key TEXT NOT NULL UNIQUE,
                owner_user_id INTEGER NOT NULL,
                prompt_id TEXT,
                backend_url TEXT NOT NULL DEFAULT '',
                image_ref_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(comfyui_image_refs)").fetchall()}
        if "backend_url" not in columns:
            conn.execute("ALTER TABLE comfyui_image_refs ADD COLUMN backend_url TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comfyui_image_refs_owner ON comfyui_image_refs(owner_user_id, created_at)")

    def _register_comfyui_image_refs(conn, *, actor, images, backend_url=""):
        _ensure_comfyui_image_ref_schema(conn)
        now = datetime.now().isoformat()
        normalized_backend_url = _normalize_comfyui_backend_url(backend_url)
        for item in images:
            image_ref = item.get("image_ref") if isinstance(item, dict) else None
            payload = _image_ref_payload(image_ref)
            ref_key = _image_ref_key(payload)
            if not payload or not ref_key:
                continue
            conn.execute(
                """
                INSERT INTO comfyui_image_refs (ref_key, owner_user_id, prompt_id, backend_url, image_ref_json, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ref_key) DO UPDATE SET
                    owner_user_id=excluded.owner_user_id,
                    prompt_id=excluded.prompt_id,
                    backend_url=excluded.backend_url,
                    image_ref_json=excluded.image_ref_json,
                    last_used_at=excluded.last_used_at
                """,
                (
                    ref_key,
                    int(_actor_value(actor, "id")),
                    str(item.get("prompt_id") or ""),
                    normalized_backend_url,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )

    def _load_comfyui_image_ref_record(conn, *, actor, image_ref, prompt_id=None):
        _ensure_comfyui_image_ref_schema(conn)
        ref_key = _image_ref_key(image_ref)
        if not ref_key:
            return None
        row = conn.execute(
            "SELECT owner_user_id, prompt_id, backend_url FROM comfyui_image_refs WHERE ref_key=?",
            (ref_key,),
        ).fetchone()
        if not row or int(row["owner_user_id"]) != int(_actor_value(actor, "id")):
            return None
        if prompt_id and row["prompt_id"] and str(row["prompt_id"]) != str(prompt_id):
            return None
        conn.execute("UPDATE comfyui_image_refs SET last_used_at=? WHERE ref_key=?", (datetime.now().isoformat(), ref_key))
        return dict(row)

    def _verify_comfyui_image_ref_owner(conn, *, actor, image_ref, prompt_id=None):
        return bool(_load_comfyui_image_ref_record(conn, actor=actor, image_ref=image_ref, prompt_id=prompt_id))

    def _ensure_comfyui_generation_history_schema(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comfyui_generation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                backend_url TEXT NOT NULL DEFAULT '',
                generation_mode TEXT NOT NULL DEFAULT 'txt2img',
                payload_json TEXT NOT NULL,
                input_assets_json TEXT NOT NULL DEFAULT '{}',
                controlnet_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comfyui_generation_history_owner ON comfyui_generation_history(owner_user_id, created_at DESC)"
        )

    def _record_generation_history(conn, *, actor, params, backend_url="", result_payload=None):
        _ensure_comfyui_generation_history_schema(conn)
        now = datetime.now().isoformat()
        params = dict(params or {})
        try:
            payload = json.loads(json.dumps(params, ensure_ascii=False, default=str))
        except Exception:
            payload = {}
        payload.update({
            "generation_mode": params.get("generation_mode") or "txt2img",
            "model": params.get("model") or "",
            "diffusers_model_repo": params.get("diffusers_model_repo") or "",
            "diffusers_model_variant": params.get("diffusers_model_variant") or "",
            "diffusers_gguf_file": params.get("diffusers_gguf_file") or "",
            "diffusers_gguf_base_repo": params.get("diffusers_gguf_base_repo") or "",
            "diffusers_gguf_profile": params.get("diffusers_gguf_profile") or "",
            "diffusers_gguf_variant": params.get("diffusers_gguf_variant") or "",
            "prompt": params.get("prompt") or "",
            "negative_prompt": params.get("negative_prompt") or "",
            "width": int(params.get("width") or 0),
            "height": int(params.get("height") or 0),
            "steps": int(params.get("steps") or 0),
            "cfg": float(params.get("cfg") or 0),
            "sampler_name": params.get("sampler_name") or "",
            "scheduler": params.get("scheduler") or "",
            "seed": int(params.get("seed") or 0),
            "batch_size": int(params.get("batch_size") or 1),
            "ui_batch_size": int(params.get("ui_batch_size") or params.get("batch_size") or 1),
            "run_count": int(params.get("run_count") or 1),
            "seed_after_generate": params.get("seed_after_generate") or "fixed",
            "filename_prefix": params.get("filename_prefix") or "hackme_web",
            "loras": list(params.get("loras") or []),
            "vae": params.get("vae") or "",
            "denoise_strength": float(params.get("denoise_strength") or 0),
            "upscale_model": params.get("upscale_model") or "",
            "outpaint": dict(params.get("outpaint") or {}),
        })
        input_assets = {
            "source_image_ref": params.get("source_image_ref"),
            "mask_image_ref": params.get("mask_image_ref"),
            "control_image_ref": ((params.get("controlnet") or {}).get("image_ref") if isinstance(params.get("controlnet"), dict) else None),
        }
        cur = conn.execute(
            """
            INSERT INTO comfyui_generation_history (
                owner_user_id, backend_url, generation_mode, payload_json,
                input_assets_json, controlnet_json, result_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(_actor_value(actor, "id")),
                _normalize_comfyui_backend_url(backend_url),
                str(payload["generation_mode"] or "txt2img"),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                json.dumps(input_assets, ensure_ascii=False, sort_keys=True),
                json.dumps(params.get("controlnet") or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(result_payload or {}, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        return cur.lastrowid

    def _list_generation_history(conn, *, actor, limit=COMFYUI_HISTORY_LIMIT):
        _ensure_comfyui_generation_history_schema(conn)
        rows = conn.execute(
            """
            SELECT * FROM comfyui_generation_history
            WHERE owner_user_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (int(_actor_value(actor, "id")), int(limit)),
        ).fetchall()
        items = []
        for row in rows:
            payload = _parse_json_field(row["payload_json"], {})
            input_assets = _parse_json_field(row["input_assets_json"], {})
            controlnet = _parse_json_field(row["controlnet_json"], {})
            result_json = _parse_json_field(row["result_json"], {})
            items.append({
                "id": int(row["id"]),
                "generation_mode": row["generation_mode"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "payload": payload,
                "input_assets": input_assets,
                "controlnet": controlnet,
                "result": result_json,
            })
        return items

    def _load_generation_history(conn, *, actor, history_id):
        _ensure_comfyui_generation_history_schema(conn)
        row = conn.execute(
            "SELECT * FROM comfyui_generation_history WHERE id=? AND owner_user_id=?",
            (int(history_id), int(_actor_value(actor, "id"))),
        ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "backend_url": row["backend_url"] or "",
            "generation_mode": row["generation_mode"] or "txt2img",
            "payload": _parse_json_field(row["payload_json"], {}),
            "input_assets": _parse_json_field(row["input_assets_json"], {}),
            "controlnet": _parse_json_field(row["controlnet_json"], {}),
            "result": _parse_json_field(row["result_json"], {}),
        }

    def _ensure_comfyui_workflow_schema(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comfyui_workflow_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'private',
                is_official INTEGER NOT NULL DEFAULT 0,
                system_bundle_id TEXT,
                purpose TEXT NOT NULL DEFAULT 'custom',
                comfyui_version TEXT NOT NULL DEFAULT '',
                project_version TEXT NOT NULL DEFAULT '',
                workflow_schema_version TEXT NOT NULL DEFAULT '1',
                workflow_json TEXT NOT NULL,
                workflow_hash TEXT NOT NULL DEFAULT '',
                layout_json TEXT NOT NULL DEFAULT '{}',
                required_models_json TEXT NOT NULL DEFAULT '[]',
                required_loras_json TEXT NOT NULL DEFAULT '[]',
                required_controlnets_json TEXT NOT NULL DEFAULT '[]',
                required_custom_nodes_json TEXT NOT NULL DEFAULT '[]',
                default_params_json TEXT NOT NULL DEFAULT '{}',
                is_default INTEGER NOT NULL DEFAULT 0,
                published_by_user_id INTEGER,
                published_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comfyui_workflow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preset_id INTEGER NOT NULL,
                actor_user_id INTEGER NOT NULL,
                prompt TEXT NOT NULL DEFAULT '',
                negative_prompt TEXT NOT NULL DEFAULT '',
                params_json TEXT NOT NULL DEFAULT '{}',
                workflow_json TEXT NOT NULL,
                output_refs_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comfyui_workflow_layout_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preset_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                created_by_user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT 'custom',
                comfyui_version TEXT NOT NULL DEFAULT '',
                project_version TEXT NOT NULL DEFAULT '',
                workflow_schema_version TEXT NOT NULL DEFAULT '1',
                workflow_json TEXT NOT NULL,
                workflow_hash TEXT NOT NULL DEFAULT '',
                layout_json TEXT NOT NULL DEFAULT '{}',
                required_models_json TEXT NOT NULL DEFAULT '[]',
                required_loras_json TEXT NOT NULL DEFAULT '[]',
                required_controlnets_json TEXT NOT NULL DEFAULT '[]',
                required_custom_nodes_json TEXT NOT NULL DEFAULT '[]',
                default_params_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(preset_id, version_no)
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(comfyui_workflow_presets)").fetchall()}
        if "published_by_user_id" not in columns:
            conn.execute("ALTER TABLE comfyui_workflow_presets ADD COLUMN published_by_user_id INTEGER")
        if "published_at" not in columns:
            conn.execute("ALTER TABLE comfyui_workflow_presets ADD COLUMN published_at TEXT")
        if "system_bundle_id" not in columns:
            conn.execute("ALTER TABLE comfyui_workflow_presets ADD COLUMN system_bundle_id TEXT")
        for column_name, ddl in {
            "purpose": "ALTER TABLE comfyui_workflow_presets ADD COLUMN purpose TEXT NOT NULL DEFAULT 'custom'",
            "comfyui_version": "ALTER TABLE comfyui_workflow_presets ADD COLUMN comfyui_version TEXT NOT NULL DEFAULT ''",
            "project_version": "ALTER TABLE comfyui_workflow_presets ADD COLUMN project_version TEXT NOT NULL DEFAULT ''",
            "workflow_schema_version": "ALTER TABLE comfyui_workflow_presets ADD COLUMN workflow_schema_version TEXT NOT NULL DEFAULT '1'",
            "layout_json": "ALTER TABLE comfyui_workflow_presets ADD COLUMN layout_json TEXT NOT NULL DEFAULT '{}'",
            "required_custom_nodes_json": "ALTER TABLE comfyui_workflow_presets ADD COLUMN required_custom_nodes_json TEXT NOT NULL DEFAULT '[]'",
            "is_default": "ALTER TABLE comfyui_workflow_presets ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column_name not in columns:
                conn.execute(ddl)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comfyui_workflow_presets_owner ON comfyui_workflow_presets(owner_user_id, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comfyui_workflow_presets_official ON comfyui_workflow_presets(is_official, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comfyui_workflow_presets_system_bundle ON comfyui_workflow_presets(system_bundle_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comfyui_workflow_runs_preset ON comfyui_workflow_runs(preset_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comfyui_workflow_layout_versions_preset ON comfyui_workflow_layout_versions(preset_id, version_no DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comfyui_workflow_presets_default ON comfyui_workflow_presets(owner_user_id, is_default, updated_at DESC)"
        )

    def _normalize_workflow_visibility(value):
        text = str(value or "private").strip().lower() or "private"
        return text if text in COMFYUI_WORKFLOW_VISIBILITY_VALUES else "private"

    def _normalize_workflow_purpose(value, default_params=None):
        text = str(value or "").strip().lower()
        if text in COMFYUI_WORKFLOW_PURPOSE_VALUES:
            return text
        mode = str((default_params or {}).get("generation_mode") or "").strip().lower()
        if mode in COMFYUI_WORKFLOW_PURPOSE_VALUES:
            return mode
        return "custom"

    def _normalize_workflow_version(value, fallback=""):
        return _safe_text(value if value not in (None, "") else fallback, 80)

    def _sanitize_workflow_layout_json(value, workflow_json=None):
        if value in (None, ""):
            return {
                "layout_schema_version": "1",
                "node_order": list((workflow_json or {}).keys()) if isinstance(workflow_json, dict) else [],
                "node_positions": {},
                "field_overrides": {},
            }
        candidate = value
        if isinstance(candidate, str):
            if len(candidate.encode("utf-8")) > COMFYUI_WORKFLOW_LAYOUT_MAX_JSON_BYTES:
                raise WorkflowValidationError("layout JSON 過大")
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise WorkflowValidationError("layout JSON 格式不正確") from exc
        if not isinstance(candidate, dict):
            raise WorkflowValidationError("layout JSON 必須是物件")

        def sanitize_value(item, *, path="layout", depth=0):
            if depth > 8:
                raise WorkflowValidationError(f"{path} 巢狀層級過深")
            if isinstance(item, dict):
                result = {}
                for key, child in item.items():
                    key_text = str(key or "").strip()
                    if not key_text:
                        raise WorkflowValidationError(f"{path} 含有空白欄位名稱")
                    result[key_text[:80]] = sanitize_value(child, path=f"{path}.{key_text[:80]}", depth=depth + 1)
                return result
            if isinstance(item, list):
                return [sanitize_value(child, path=f"{path}[{index}]", depth=depth + 1) for index, child in enumerate(item[:500])]
            if isinstance(item, str):
                text = item.strip()
                if WORKFLOW_BLOCKED_COMMAND_RE.search(text):
                    raise WorkflowValidationError(f"{path} 含有不允許的命令片段")
                if WORKFLOW_URL_RE.match(text):
                    raise WorkflowValidationError(f"{path} 不可包含外部 URL")
                if WORKFLOW_ABSOLUTE_PATH_RE.match(text):
                    raise WorkflowValidationError(f"{path} 不可包含絕對路徑")
                return item[:2000]
            if isinstance(item, (int, float, bool)) or item is None:
                return item
            raise WorkflowValidationError(f"{path} 類型不支援")

        sanitized = sanitize_value(candidate)
        if len(json.dumps(sanitized, ensure_ascii=False, sort_keys=True).encode("utf-8")) > COMFYUI_WORKFLOW_LAYOUT_MAX_JSON_BYTES:
            raise WorkflowValidationError("layout JSON 過大")
        return sanitized

    def _infer_required_custom_nodes(workflow_json):
        nodes = []
        if not isinstance(workflow_json, dict):
            return nodes
        for node in workflow_json.values():
            class_type = str((node or {}).get("class_type") or "").strip()
            if class_type and class_type not in COMFYUI_CORE_WORKFLOW_NODE_CLASSES and class_type not in nodes:
                nodes.append(class_type)
        return sorted(nodes)

    def _workflow_version_warnings(row):
        warnings = []
        schema_version = str(row["workflow_schema_version"] or COMFYUI_WORKFLOW_SCHEMA_VERSION)
        if schema_version != COMFYUI_WORKFLOW_SCHEMA_VERSION:
            warnings.append(f"workflow schema 版本 {schema_version} 與目前支援版本 {COMFYUI_WORKFLOW_SCHEMA_VERSION} 不一致")
        return warnings

    def _workflow_manifest_for_row(row):
        bundle_id = str(row["system_bundle_id"] or "").strip()
        if not bundle_id or not re.match(r"^[a-z][a-z0-9_]{0,63}$", bundle_id):
            return None
        manifest_path = runtime_comfyui_dir() / bundle_id / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            if manifest_path.stat().st_size > 64 * 1024:
                return None
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(manifest, dict):
            return None
        if str(manifest.get("id") or "").strip() != bundle_id:
            return None
        return manifest

    def _workflow_manifest_summary(row):
        manifest = _workflow_manifest_for_row(row)
        if not manifest:
            return {"available": False}
        panels = ((manifest.get("ui") or {}).get("panels") or [])
        return {
            "available": True,
            "id": str(manifest.get("id") or ""),
            "schema_version": int(manifest.get("schema_version") or 1),
            "name": str(manifest.get("name") or ""),
            "description": str(manifest.get("description") or ""),
            "workflow_file": str(manifest.get("workflow_file") or "workflow.json"),
            "panel_count": len(panels) if isinstance(panels, list) else 0,
            "source": str(manifest.get("source") or "system"),
        }

    def _workflow_preset_summary(row, *, dependency_status=None, recent_runs=None, actor=None):
        default_params = _parse_json_field(row["default_params_json"], {}) or {}
        workflow_json = _parse_json_field(row["workflow_json"], {}) or {}
        try:
            inferred_summary = extract_workflow_summary(apply_workflow_compatibility_fixes(workflow_json))
        except Exception:
            inferred_summary = {}

        def _merged_summary_items(stored, inferred_items, key_names):
            merged = []
            seen = set()
            for item in list(stored or []) + list(inferred_items or []):
                if not isinstance(item, dict):
                    continue
                key = tuple(str(item.get(name) or "").strip().lower() for name in key_names)
                if not any(key) or key in seen:
                    continue
                seen.add(key)
                merged.append(item)
            return merged

        paid_api_nodes = detect_paid_api_nodes(workflow_json)
        result = {
            "id": int(row["id"]),
            "owner_user_id": int(row["owner_user_id"]),
            "title": row["title"] or f"Workflow #{row['id']}",
            "description": row["description"] or "",
            "visibility": row["visibility"] or "private",
            "is_official": bool(row["is_official"]),
            "system_bundle_id": row["system_bundle_id"] or "",
            "purpose": row["purpose"] or "custom",
            "comfyui_version": row["comfyui_version"] or "",
            "project_version": row["project_version"] or "",
            "workflow_schema_version": row["workflow_schema_version"] or COMFYUI_WORKFLOW_SCHEMA_VERSION,
            "workflow_hash": row["workflow_hash"] or "",
            "layout_json": _parse_json_field(row["layout_json"], {}) or {},
            "required_models": _merged_summary_items(
                _parse_json_field(row["required_models_json"], []) or [],
                inferred_summary.get("required_models") or [],
                ("kind", "name"),
            ),
            "required_loras": _merged_summary_items(
                _parse_json_field(row["required_loras_json"], []) or [],
                inferred_summary.get("required_loras") or [],
                ("name",),
            ),
            "required_controlnets": _merged_summary_items(
                _parse_json_field(row["required_controlnets_json"], []) or [],
                inferred_summary.get("required_controlnets") or [],
                ("name", "type"),
            ),
            "required_custom_nodes": _parse_json_field(row["required_custom_nodes_json"], []) or [],
            "default_params": default_params,
            "is_default": bool(row["is_default"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "published_at": row["published_at"],
            "published_by_user_id": row["published_by_user_id"],
            "can_edit": actor is not None and int(row["owner_user_id"]) == int(_actor_value(actor, "id")),
            "can_publish_official": bool(
                actor is not None
                and _actor_value(actor, "username") == "root"
                and int(row["owner_user_id"]) == int(_actor_value(actor, "id"))
            ),
            "version_warnings": _workflow_version_warnings(row),
            "paid_api_nodes": paid_api_nodes,
            "requires_paid_api_confirmation": bool(paid_api_nodes.get("required")),
            "manifest_summary": _workflow_manifest_summary(row),
        }
        if dependency_status is not None:
            result["dependency_status"] = dependency_status
        if recent_runs is not None:
            result["recent_runs"] = recent_runs
        return result

    def _load_workflow_preset_row(conn, *, preset_id):
        _ensure_comfyui_workflow_schema(conn)
        return conn.execute("SELECT * FROM comfyui_workflow_presets WHERE id=?", (int(preset_id),)).fetchone()

    def _can_read_workflow_preset(row, actor):
        if not row or not actor:
            return False
        actor_id = int(_actor_value(actor, "id"))
        return (
            int(row["owner_user_id"]) == actor_id
            or bool(row["is_official"])
            or str(row["visibility"] or "private").strip().lower() == "public"
        )

    def _can_write_workflow_preset(row, actor):
        return bool(row and actor and int(row["owner_user_id"]) == int(_actor_value(actor, "id")))

    def _load_workflow_preset(conn, *, preset_id, actor, require_write=False):
        row = _load_workflow_preset_row(conn, preset_id=preset_id)
        if not row:
            return None, json_resp({"ok": False, "msg": "找不到這個 workflow preset"}, 404)
        if require_write:
            if not _can_write_workflow_preset(row, actor):
                return None, json_resp({"ok": False, "msg": "你沒有權限修改這個 workflow preset"}, 403)
        elif not _can_read_workflow_preset(row, actor):
            return None, json_resp({"ok": False, "msg": "你沒有權限查看這個 workflow preset"}, 403)
        return row, None

    def _list_workflow_runs(conn, *, preset_id, limit=COMFYUI_WORKFLOW_RUN_LIMIT):
        _ensure_comfyui_workflow_schema(conn)
        rows = conn.execute(
            """
            SELECT id, prompt, negative_prompt, params_json, workflow_json, output_refs_json, status, error, created_at, updated_at
            FROM comfyui_workflow_runs
            WHERE preset_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (int(preset_id), int(limit)),
        ).fetchall()
        return [{
            "id": int(row["id"]),
            "prompt": row["prompt"] or "",
            "negative_prompt": row["negative_prompt"] or "",
            "params": _parse_json_field(row["params_json"], {}) or {},
            "workflow_json": _parse_json_field(row["workflow_json"], {}) or {},
            "output_refs": _parse_json_field(row["output_refs_json"], {}) or {},
            "status": row["status"] or "queued",
            "error": row["error"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        } for row in rows]

    def _load_workflow_run_snapshot(conn, *, run_id):
        _ensure_comfyui_workflow_schema(conn)
        row = conn.execute(
            """
            SELECT params_json, workflow_json
            FROM comfyui_workflow_runs
            WHERE id=?
            """,
            (int(run_id),),
        ).fetchone()
        if not row:
            return None
        return {
            "params": _parse_json_field(row["params_json"], {}) or {},
            "workflow_json": _parse_json_field(row["workflow_json"], {}) or {},
        }

    def _list_workflow_presets(conn, *, actor, active_client=None):
        _ensure_comfyui_workflow_schema(conn)
        if _sync_runtime_official_workflow_presets(conn):
            conn.commit()
        rows = conn.execute(
            """
            SELECT *
            FROM comfyui_workflow_presets
            WHERE owner_user_id=? OR is_official=1 OR visibility='public'
            ORDER BY is_official DESC, updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(_actor_value(actor, "id")), COMFYUI_WORKFLOW_PRESET_LIMIT),
        ).fetchall()
        dependency_cache = {}
        items = []
        for row in rows:
            dependency_status = None
            if active_client is not None:
                dependency_status = _workflow_dependency_status(active_client, row)
            recent_runs = _list_workflow_runs(conn, preset_id=row["id"], limit=3)
            items.append(_workflow_preset_summary(row, dependency_status=dependency_status, recent_runs=recent_runs, actor=actor))
        return items

    def _official_workflow_owner_user_id(conn):
        row = conn.execute(
            "SELECT id FROM users WHERE username='root' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row:
            return int(row["id"])
        row = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
        if row:
            return int(row["id"])
        return 1

    def _sync_runtime_official_workflow_presets(conn):
        _ensure_comfyui_workflow_schema(conn)
        runtime_dir = runtime_comfyui_dir()
        if not runtime_dir.is_dir():
            return []

        owner_user_id = _official_workflow_owner_user_id(conn)
        bundle_rows = conn.execute(
            """
            SELECT *
            FROM comfyui_workflow_presets
            WHERE is_official=1
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
        by_bundle_id = {}
        by_title = {}
        by_hash = {}
        for row in bundle_rows:
            bundle_id = str(row["system_bundle_id"] or "").strip()
            if bundle_id and bundle_id not in by_bundle_id:
                by_bundle_id[bundle_id] = row
            title = str(row["title"] or "").strip()
            if title and title not in by_title:
                by_title[title] = row
            workflow_hash = str(row["workflow_hash"] or "").strip()
            if workflow_hash and workflow_hash not in by_hash:
                by_hash[workflow_hash] = row

        synced = []
        for bundle_id in SYSTEM_WORKFLOW_IDS:
            bundle_dir = runtime_dir / bundle_id
            workflow_path = bundle_dir / "workflow.json"
            manifest_path = bundle_dir / "manifest.json"
            if not workflow_path.is_file() or not manifest_path.is_file():
                continue
            try:
                workflow_candidate = json.loads(workflow_path.read_text(encoding="utf-8"))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            try:
                workflow_payload = sanitize_workflow_json(workflow_candidate)
            except WorkflowValidationError:
                continue
            manifest_bundle_id = str(manifest.get("id") or bundle_dir.name or "").strip()
            if manifest_bundle_id != bundle_id:
                continue
            title = _safe_text(manifest.get("name") or bundle_id, 120)
            description = _safe_text(manifest.get("description") or "", 1200)
            default_params = manifest.get("default_params")
            if not isinstance(default_params, dict):
                default_params = workflow_payload.get("default_params") or {}
            existing = (
                by_bundle_id.get(bundle_id)
                or by_hash.get(str(workflow_payload.get("workflow_hash") or "").strip())
                or by_title.get(title)
            )
            update_actor_id = int(existing["owner_user_id"]) if existing else owner_user_id
            preset_id = _upsert_workflow_preset(
                conn,
                preset_id=int(existing["id"]) if existing else None,
                actor={"id": update_actor_id},
                title=title,
                description=description,
                visibility="public",
                workflow_payload=workflow_payload,
                default_params=default_params,
                is_official=True,
                published_by_user_id=owner_user_id,
                system_bundle_id=bundle_id,
            )
            synced.append({"bundle_id": bundle_id, "preset_id": int(preset_id)})
        return synced

    def _normalize_workflow_default_params(data):
        candidate = data
        if candidate in (None, ""):
            return {}
        if isinstance(candidate, str):
            candidate = _parse_json_field(candidate, None)
        if not isinstance(candidate, dict):
            raise WorkflowValidationError("default params 必須是物件")
        normalized_candidate = dict(candidate)
        normalized_candidate["skip_asset_validation"] = True
        params, msg = _normalize_generation_payload(normalized_candidate)
        if msg:
            raise WorkflowValidationError(msg)
        return params

    def _extract_workflow_payload(data):
        try:
            parsed = sanitize_workflow_json(data)
        except WorkflowValidationError:
            raise
        default_params = parsed.get("default_params") or {}
        return parsed, default_params

    def _record_workflow_layout_version(conn, row, *, actor, now=None):
        if not row:
            return
        now = now or datetime.now().isoformat()
        preset_id = int(row["id"])
        existing = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS max_version FROM comfyui_workflow_layout_versions WHERE preset_id=?",
            (preset_id,),
        ).fetchone()
        version_no = int(existing["max_version"] if existing and existing["max_version"] is not None else 0) + 1
        conn.execute(
            """
            INSERT INTO comfyui_workflow_layout_versions (
                preset_id, version_no, created_by_user_id, title, description, purpose,
                comfyui_version, project_version, workflow_schema_version, workflow_json,
                workflow_hash, layout_json, required_models_json, required_loras_json,
                required_controlnets_json, required_custom_nodes_json, default_params_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                preset_id,
                version_no,
                int(_actor_value(actor, "id")),
                row["title"] or "",
                row["description"] or "",
                row["purpose"] or "custom",
                row["comfyui_version"] or "",
                row["project_version"] or "",
                row["workflow_schema_version"] or COMFYUI_WORKFLOW_SCHEMA_VERSION,
                row["workflow_json"] or "{}",
                row["workflow_hash"] or "",
                row["layout_json"] or "{}",
                row["required_models_json"] or "[]",
                row["required_loras_json"] or "[]",
                row["required_controlnets_json"] or "[]",
                row["required_custom_nodes_json"] or "[]",
                row["default_params_json"] or "{}",
                now,
            ),
        )

    def _workflow_preset_revision_changed(before, after):
        if before is None:
            return True
        keys = (
            "title",
            "description",
            "visibility",
            "is_official",
            "system_bundle_id",
            "purpose",
            "comfyui_version",
            "project_version",
            "workflow_schema_version",
            "workflow_json",
            "workflow_hash",
            "layout_json",
            "required_models_json",
            "required_loras_json",
            "required_controlnets_json",
            "required_custom_nodes_json",
            "default_params_json",
            "is_default",
        )
        return any(str(before[key] if before[key] is not None else "") != str(after[key] if after[key] is not None else "") for key in keys)

    def _upsert_workflow_preset(
        conn,
        *,
        preset_id=None,
        actor,
        title,
        description,
        visibility,
        workflow_payload,
        default_params,
        purpose=None,
        comfyui_version=None,
        project_version=None,
        workflow_schema_version=None,
        layout_json=None,
        required_custom_nodes=None,
        is_default=False,
        is_official=False,
        published_by_user_id=None,
        system_bundle_id=None,
    ):
        _ensure_comfyui_workflow_schema(conn)
        now = datetime.now().isoformat()
        workflow_json = workflow_payload["workflow_json"]
        workflow_hash = workflow_payload["workflow_hash"]
        required_models = workflow_payload["required_models"]
        required_loras = workflow_payload["required_loras"]
        required_controlnets = workflow_payload["required_controlnets"]
        default_payload = default_params or workflow_payload["default_params"] or {}
        safe_layout = _sanitize_workflow_layout_json(layout_json, workflow_json=workflow_json)
        safe_custom_nodes = (
            [str(item).strip()[:160] for item in required_custom_nodes if str(item or "").strip()]
            if isinstance(required_custom_nodes, list)
            else _infer_required_custom_nodes(workflow_json)
        )
        safe_purpose = _normalize_workflow_purpose(purpose, default_payload)
        safe_project_version = _normalize_workflow_version(project_version, APP_RELEASE_ID)
        safe_comfyui_version = _normalize_workflow_version(comfyui_version, "")
        safe_schema_version = _normalize_workflow_version(workflow_schema_version, COMFYUI_WORKFLOW_SCHEMA_VERSION)
        safe_is_default = 1 if is_default else 0
        args = (
            title.strip()[:120],
            _safe_text(description, 1200),
            _normalize_workflow_visibility(visibility),
            1 if is_official else 0,
            safe_purpose,
            safe_comfyui_version,
            safe_project_version,
            safe_schema_version,
            json.dumps(workflow_json, ensure_ascii=False, sort_keys=True),
            workflow_hash,
            json.dumps(safe_layout, ensure_ascii=False, sort_keys=True),
            json.dumps(required_models, ensure_ascii=False, sort_keys=True),
            json.dumps(required_loras, ensure_ascii=False, sort_keys=True),
            json.dumps(required_controlnets, ensure_ascii=False, sort_keys=True),
            json.dumps(safe_custom_nodes, ensure_ascii=False, sort_keys=True),
            json.dumps(default_payload, ensure_ascii=False, sort_keys=True),
            safe_is_default,
            int(published_by_user_id) if published_by_user_id else None,
            now if is_official else None,
            str(system_bundle_id or "").strip() or None,
            now,
        )
        if preset_id is None:
            cur = conn.execute(
                """
                INSERT INTO comfyui_workflow_presets (
                    owner_user_id, title, description, visibility, is_official,
                    purpose, comfyui_version, project_version, workflow_schema_version,
                    workflow_json, workflow_hash, layout_json, required_models_json, required_loras_json,
                    required_controlnets_json, required_custom_nodes_json, default_params_json, is_default,
                    published_by_user_id, published_at, system_bundle_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(_actor_value(actor, "id")),
                    *args,
                    now,
                ),
            )
            preset_id = int(cur.lastrowid)
            if safe_is_default:
                conn.execute(
                    "UPDATE comfyui_workflow_presets SET is_default=0 WHERE owner_user_id=? AND id<>?",
                    (int(_actor_value(actor, "id")), preset_id),
                )
            row = _load_workflow_preset_row(conn, preset_id=preset_id)
            _record_workflow_layout_version(conn, row, actor=actor, now=now)
            return preset_id
        before_row = _load_workflow_preset_row(conn, preset_id=preset_id)
        conn.execute(
                """
                UPDATE comfyui_workflow_presets
                SET title=?, description=?, visibility=?, is_official=?, purpose=?, comfyui_version=?,
                    project_version=?, workflow_schema_version=?, workflow_json=?, workflow_hash=?,
                    layout_json=?, required_models_json=?, required_loras_json=?, required_controlnets_json=?,
                    required_custom_nodes_json=?, default_params_json=?, is_default=?, published_by_user_id=?,
                    published_at=?, system_bundle_id=?, updated_at=?
                WHERE id=? AND owner_user_id=?
                """,
                (
                *args,
                int(preset_id),
                int(_actor_value(actor, "id")),
            ),
        )
        if safe_is_default:
            conn.execute(
                "UPDATE comfyui_workflow_presets SET is_default=0 WHERE owner_user_id=? AND id<>?",
                (int(_actor_value(actor, "id")), int(preset_id)),
            )
        after_row = _load_workflow_preset_row(conn, preset_id=preset_id)
        if _workflow_preset_revision_changed(before_row, after_row):
            _record_workflow_layout_version(conn, after_row, actor=actor, now=now)
        return int(preset_id)

    def _create_workflow_run(conn, *, preset_id, actor, prompt, negative_prompt, params_json, workflow_json):
        _ensure_comfyui_workflow_schema(conn)
        now = datetime.now().isoformat()
        cur = conn.execute(
            """
            INSERT INTO comfyui_workflow_runs (
                preset_id, actor_user_id, prompt, negative_prompt, params_json,
                workflow_json, output_refs_json, status, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '{}', 'queued', '', ?, ?)
            """,
            (
                int(preset_id),
                int(_actor_value(actor, "id")),
                _safe_text(prompt, 3000),
                _safe_text(negative_prompt, 3000),
                json.dumps(params_json or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(workflow_json or {}, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        return cur.lastrowid

    def _update_workflow_run(conn, *, run_id, status, output_refs=None, error=""):
        _ensure_comfyui_workflow_schema(conn)
        conn.execute(
            """
            UPDATE comfyui_workflow_runs
            SET status=?, output_refs_json=?, error=?, updated_at=?
            WHERE id=?
            """,
            (
                str(status or "queued"),
                json.dumps(output_refs or {}, ensure_ascii=False, sort_keys=True),
                _safe_text(error, 500),
                datetime.now().isoformat(),
                int(run_id),
            ),
        )

    def _workflow_expected_image_count(workflow_json, default_params):
        try:
            batch_size = max(1, int((default_params or {}).get("batch_size") or 1))
        except Exception:
            batch_size = 1
        image_output_nodes = 0
        if isinstance(workflow_json, dict):
            for node in workflow_json.values():
                if not isinstance(node, dict):
                    continue
                class_type = str(node.get("class_type") or "").strip()
                inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
                if class_type in {"SaveImage", "PreviewImage"} and "images" in inputs:
                    image_output_nodes += 1
        return max(batch_size, image_output_nodes, 1)

    def _active_client_dependency_sets(active_client):
        capabilities = active_client.get_capabilities() if hasattr(active_client, "get_capabilities") else {}
        try:
            models = set(active_client.get_models() if hasattr(active_client, "get_models") else [])
        except Exception:
            models = set()
        try:
            vaes = set(active_client.get_vaes() if hasattr(active_client, "get_vaes") else [])
        except Exception:
            vaes = set()
        try:
            loras = set(active_client.get_loras() if hasattr(active_client, "get_loras") else [])
        except Exception:
            loras = set()
        try:
            embeddings = set(active_client.get_embeddings() if hasattr(active_client, "get_embeddings") else [])
        except Exception:
            embeddings = set()
        try:
            latent_upscale_models = set(active_client.get_latent_upscale_models() if hasattr(active_client, "get_latent_upscale_models") else [])
        except Exception:
            latent_upscale_models = set()
        try:
            clip_vision_models = set(active_client.get_clip_vision_models() if hasattr(active_client, "get_clip_vision_models") else [])
        except Exception:
            clip_vision_models = set()
        return {
            "models": models,
            "vaes": vaes,
            "loras": loras,
            "embeddings": embeddings,
            "diffusion_models": set((capabilities or {}).get("diffusion_models") or []),
            "clip_models": set((capabilities or {}).get("clip_models") or []),
            "clip_vision_models": set((capabilities or {}).get("clip_vision_models") or []) or clip_vision_models,
            "controlnets": set((capabilities or {}).get("controlnet_models") or []),
            "upscale_models": set((capabilities or {}).get("upscale_models") or []),
            "latent_upscale_models": set((capabilities or {}).get("latent_upscale_models") or []) or latent_upscale_models,
            "available_nodes": set((capabilities or {}).get("available_nodes") or []),
            "controlnet_types": (capabilities or {}).get("controlnet_types") or {},
        }

    def _workflow_dependency_status(active_client, row):
        payload = apply_workflow_compatibility_fixes(_parse_json_field(row["workflow_json"], {}) or {})
        required_models = _parse_json_field(row["required_models_json"], []) or []
        required_loras = _parse_json_field(row["required_loras_json"], []) or []
        required_controlnets = _parse_json_field(row["required_controlnets_json"], []) or []
        try:
            inferred = extract_workflow_summary(payload)
        except Exception:
            inferred = {}

        def _merge_required(existing, inferred_items, key_names):
            merged = []
            seen = set()
            for item in list(existing or []) + list(inferred_items or []):
                if not isinstance(item, dict):
                    continue
                key = tuple(str(item.get(name) or "").strip().lower() for name in key_names)
                if not any(key) or key in seen:
                    continue
                seen.add(key)
                merged.append(item)
            return merged

        inferred_models = inferred.get("required_models") or []
        inferred_embedding_items = [
            item for item in inferred_models
            if isinstance(item, dict) and str(item.get("kind") or "").strip().lower() == "embedding"
        ]
        if inferred_embedding_items:
            required_models = [
                item for item in required_models
                if not (
                    isinstance(item, dict)
                    and str(item.get("kind") or "").strip().lower() == "embedding"
                )
            ]
        required_models = _merge_required(required_models, inferred.get("required_models") or [], ("kind", "name"))
        required_loras = _merge_required(required_loras, inferred.get("required_loras") or [], ("name",))
        required_controlnets = _merge_required(required_controlnets, inferred.get("required_controlnets") or [], ("name", "type"))
        sets = _active_client_dependency_sets(active_client)
        missing_models = []
        missing_loras = []
        missing_controlnets = []
        missing_nodes = []
        for node in payload.values():
            class_type = str((node or {}).get("class_type") or "").strip()
            if class_type and sets["available_nodes"] and class_type not in sets["available_nodes"]:
                missing_nodes.append(class_type)
        for item in required_models:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            kind = str(item.get("kind") or "checkpoint").strip().lower()
            if not name:
                continue
            if kind == "vae" and name == COMFYUI_VAE_BUILTIN:
                continue
            if kind == "vae":
                if not model_option_available(name, sets["vaes"]):
                    missing_models.append({"kind": kind, "name": name})
            elif kind == "upscale":
                if not model_option_available(name, sets["upscale_models"]):
                    missing_models.append({"kind": kind, "name": name})
            elif kind in {"latent_upscale", "latent_upscale_model"}:
                if not model_option_available(name, sets["latent_upscale_models"]):
                    missing_models.append({"kind": kind, "name": name})
            elif kind in {"diffusion_model", "unet"}:
                if not model_option_available(name, sets["diffusion_models"]):
                    missing_models.append({"kind": kind, "name": name})
            elif kind in {"clip", "text_encoder"}:
                if not model_option_available(name, sets["clip_models"]):
                    missing_models.append({"kind": kind, "name": name})
            elif kind in {"clip_vision", "clipvision"}:
                if not model_option_available(name, sets["clip_vision_models"]):
                    missing_models.append({"kind": kind, "name": name})
            elif kind == "embedding":
                if not embedding_option_available(name, sets["embeddings"]):
                    missing_models.append({"kind": kind, "name": name})
            elif not model_option_available(name, sets["models"]):
                missing_models.append({"kind": kind, "name": name})
        for item in required_loras:
            name = str((item or {}).get("name") or "").strip() if isinstance(item, dict) else str(item or "").strip()
            if name and not model_option_available(name, sets["loras"]):
                missing_loras.append({"name": name})
        for item in required_controlnets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            control_type = str(item.get("type") or "").strip().lower()
            if name and not model_option_available(name, sets["controlnets"]):
                missing_controlnets.append({"name": name, "type": control_type})
            elif control_type and not ((sets["controlnet_types"].get(control_type) or {}).get("available")):
                missing_controlnets.append({"name": name, "type": control_type})
        available = not (missing_models or missing_loras or missing_controlnets or missing_nodes)
        issues = []
        if missing_nodes:
            issues.append(f"缺少 workflow node：{', '.join(sorted(set(missing_nodes)))}")
        if missing_models:
            issues.append("缺少模型：" + ", ".join(sorted({item['name'] for item in missing_models})))
            if (
                any(str(item.get("kind") or "").strip().lower() in {"latent_upscale", "latent_upscale_model"} for item in missing_models)
                and not sets["latent_upscale_models"]
            ):
                issues.append(
                    "ComfyUI API 目前未列出任何 latent_upscale_models；"
                    "若檔案剛放入遠端 models/latent_upscale_models，請重啟或刷新遠端 ComfyUI 的模型清單"
                )
        if missing_loras:
            issues.append("缺少 LoRA：" + ", ".join(sorted({item['name'] for item in missing_loras})))
        if missing_controlnets:
            issues.append("缺少 ControlNet：" + ", ".join(sorted({item['name'] for item in missing_controlnets})))
        return {
            "available": available,
            "missing_nodes": sorted(set(missing_nodes)),
            "missing_models": missing_models,
            "missing_loras": missing_loras,
            "missing_controlnets": missing_controlnets,
            "issues": issues,
        }

    def _assert_workflow_dependencies_or_error(active_client, row):
        status = _workflow_dependency_status(active_client, row)
        if status["available"]:
            return status, None
        message = "；".join(status["issues"]) if status["issues"] else "workflow 依賴檢查失敗"
        return status, message

    def _parse_generation_request():
        uploaded_assets = {}
        if request.content_type and "multipart/form-data" in request.content_type.lower():
            data = request.form.to_dict(flat=True)
            files = request.files or {}
            for field_name, label in (
                ("source_image", "來源圖片"),
                ("mask_image", "遮罩圖片"),
                ("control_image", "控制圖"),
            ):
                payload, msg = _validate_image_upload(files.get(field_name), label=label)
                if msg:
                    return None, None, msg
                if payload:
                    uploaded_assets[field_name] = payload
            return data, uploaded_assets, None
        try:
            data = request.get_json(force=True)
        except Exception:
            return None, None, "請求 JSON 格式錯誤"
        if not isinstance(data, dict):
            return None, None, "請求內容格式錯誤"
        return data, uploaded_assets, None

    def _materialize_generation_image_ref(actor, active_client, image_ref, *, label):
        normalized = _image_ref_payload(image_ref)
        if not normalized:
            return None
        if str(normalized.get("type") or "output").strip().lower() == "input":
            return normalized

        conn = get_db()
        try:
            ref_row = _load_comfyui_image_ref_record(conn, actor=actor, image_ref=normalized)
            conn.commit()
        finally:
            conn.close()
        if not ref_row:
            raise ComfyUIError(f"找不到可用的{label}圖片引用")

        source_client = active_client
        source_backend_url = str((ref_row or {}).get("backend_url") or "").strip()
        if source_backend_url:
            source_binding = _comfyui_binding(actor, backend_url=source_backend_url)
            source_client = _client_for_url(source_binding.get("url"))
        try:
            fetched = source_client.fetch_image(normalized)
            _assert_reasonable_image_size(fetched)
            data = getattr(fetched, "data", b"") or b""
            filename = getattr(fetched, "filename", "") or normalized.get("filename") or f"{label}.png"
            return active_client.upload_image_bytes(
                data,
                filename,
                image_type="input",
                overwrite=False,
            )
        except ComfyUIError:
            raise
        except Exception as exc:
            raise ComfyUIError(f"{label}圖片轉入 ComfyUI input 失敗：{exc}") from exc

    def _hydrate_generation_assets(actor, active_client, params, uploaded_assets):
        params = dict(params or {})
        uploaded_assets = dict(uploaded_assets or {})
        image_ref_records = []
        for field_name, params_key in (
            ("source_image", "source_image_ref"),
            ("mask_image", "mask_image_ref"),
            ("control_image", "control_image_ref"),
        ):
            asset = uploaded_assets.get(field_name)
            if not asset:
                continue
            image_ref = active_client.upload_image_bytes(
                asset["data"],
                asset["filename"],
                image_type="input",
                overwrite=False,
            )
            image_ref_records.append({"image_ref": image_ref, "prompt_id": ""})
            if params_key == "control_image_ref":
                control = dict(params.get("controlnet") or {})
                control["image_ref"] = image_ref
                params["controlnet"] = control
            else:
                params[params_key] = image_ref
        for params_key, label in (
            ("source_image_ref", "來源"),
            ("mask_image_ref", "遮罩"),
            ("reference_image_ref", "參考"),
        ):
            image_ref = params.get(params_key)
            materialized_ref = _materialize_generation_image_ref(actor, active_client, image_ref, label=label)
            if materialized_ref and materialized_ref != image_ref:
                params[params_key] = materialized_ref
                image_ref_records.append({"image_ref": materialized_ref, "prompt_id": ""})
        control = params.get("controlnet") if isinstance(params.get("controlnet"), dict) else None
        if control and isinstance(control.get("image_ref"), dict):
            materialized_ref = _materialize_generation_image_ref(actor, active_client, control.get("image_ref"), label="控制")
            if materialized_ref and materialized_ref != control.get("image_ref"):
                control = dict(control)
                control["image_ref"] = materialized_ref
                params["controlnet"] = control
                image_ref_records.append({"image_ref": materialized_ref, "prompt_id": ""})
        if image_ref_records:
            conn = get_db()
            try:
                _register_comfyui_image_refs(conn, actor=actor, images=image_ref_records, backend_url=getattr(active_client, "base_url", ""))
                conn.commit()
            finally:
                conn.close()
        return params

    def _validate_generation_capabilities(active_client, params):
        if not hasattr(active_client, "get_capabilities"):
            return {}, None
        capabilities = active_client.get_capabilities() if hasattr(active_client, "get_capabilities") else {}
        available_nodes = set((capabilities or {}).get("available_nodes") or [])
        mode = str((params or {}).get("generation_mode") or "txt2img").strip().lower()
        mode_definition = GENERATION_MODE_DEFINITIONS.get(mode) or {}
        backend_kind = str(getattr(active_client, "backend_kind", "") or (capabilities or {}).get("backend_kind") or "").strip().lower()
        if backend_kind == "diffusers":
            supported_modes = {
                str(item.get("key") or "").strip().lower()
                for item in (capabilities or {}).get("generation_modes") or []
                if item.get("available")
            }
            if mode_definition.get("workflow_only") or mode not in supported_modes:
                return capabilities, "Diffusers 後端目前只支援文字生圖、圖生圖與局部重繪；影片、語音、放大與 workflow 模板仍請使用 ComfyUI 後端。"
            requested_repo = normalize_huggingface_repo_id(
                (params or {}).get("diffusers_model_repo") or (params or {}).get("model"),
                allow_blank=True,
            )
            if requested_repo is None:
                return capabilities, "Hugging Face repo 格式不合法，請填 namespace/model 或 huggingface.co 的模型頁網址；不要填 .safetensors / .gguf 權重檔名。"
            model_repo = requested_repo or str((capabilities or {}).get("model_repo") or getattr(active_client, "model_repo", "") or "").strip()
            model_repo = normalize_huggingface_repo_id(model_repo, allow_blank=True)
            if model_repo is None:
                return capabilities, "root 預設 Hugging Face repo 格式不合法，請改填 namespace/model；不要填 .safetensors / .gguf 權重檔名。"
            if not model_repo:
                return capabilities, "請在生圖頁面輸入 Hugging Face repo，例如 dhead/waiIllustriousSDXL_v150 或 Heartsync/NSFW-Uncensored。"
            selected_gguf_file = normalize_huggingface_repo_file(params.get("diffusers_gguf_file"), allow_blank=True)
            if selected_gguf_file is None:
                return capabilities, "GGUF 檔案路徑不合法。"
            selected_gguf_profile = str(params.get("diffusers_gguf_profile") or "").strip()
            selected_gguf_variant_id = str(params.get("diffusers_gguf_variant") or "").strip()
            if selected_gguf_file or selected_gguf_profile or selected_gguf_variant_id:
                profile, profile_variant = resolve_official_gguf_selection(
                    selected_gguf_profile,
                    selected_gguf_variant_id,
                    repo_id=model_repo,
                    gguf_file=selected_gguf_file,
                    require_enabled=False,
                )
                if not profile or not profile_variant:
                    return capabilities, "GGUF 只允許官方已建檔 profile，請從官方 GGUF 下拉選單選擇模型與精度。"
                if not profile.get("enabled") or not profile_variant.get("enabled"):
                    return capabilities, gguf_profile_unavailable_message(profile, profile_variant)
                model_repo = str(profile.get("repo_id") or model_repo).strip()
                selected_gguf_file = str(profile_variant.get("gguf_file") or selected_gguf_file).strip()
                params["diffusers_gguf_profile"] = str(profile.get("id") or "")
                params["diffusers_gguf_variant"] = str(profile_variant.get("id") or "")
                params["diffusers_gguf_file"] = selected_gguf_file
                params["diffusers_gguf_base_repo"] = str(profile.get("base_repo") or params.get("diffusers_gguf_base_repo") or "").strip()
            params["diffusers_model_repo"] = model_repo
            params["model"] = model_repo
            inspection = None
            if hasattr(active_client, "inspect_model_repo"):
                inspection = active_client.inspect_model_repo(model_repo, mode=mode)
                if not inspection.get("ok"):
                    return {**(capabilities or {}), "diffusers_inspection": inspection}, inspection.get("msg") or "Hugging Face repo 檢查失敗，尚未開始下載。"
                if not inspection.get("supported_for_mode"):
                    supported = ", ".join(inspection.get("supported_modes") or []) or "無"
                    return (
                        {**(capabilities or {}), "diffusers_inspection": inspection},
                        f"這個 Hugging Face repo 不支援「{mode}」，偵測到的支援模式：{supported}；尚未開始下載。",
                    )
                variant_options = inspection.get("variant_options") or []
                valid_variants = {str(item.get("variant") or "") for item in variant_options}
                valid_gguf_files = {str(item.get("gguf_file") or "") for item in variant_options if item.get("kind") == "gguf"}
                selected_variant = normalize_diffusers_variant(params.get("diffusers_model_variant"), allow_blank=True)
                if selected_variant is None:
                    return {**(capabilities or {}), "diffusers_inspection": inspection}, "Diffusers 模型精度版本名稱不合法。"
                if not selected_gguf_file and not selected_variant and len(variant_options) == 1 and variant_options[0].get("kind") == "gguf":
                    selected_gguf_file = str(variant_options[0].get("gguf_file") or "")
                    params["diffusers_gguf_file"] = selected_gguf_file
                    profile, profile_variant = resolve_official_gguf_selection(
                        params.get("diffusers_gguf_profile"),
                        params.get("diffusers_gguf_variant"),
                        repo_id=model_repo,
                        gguf_file=selected_gguf_file,
                        require_enabled=False,
                    )
                    if not profile or not profile_variant:
                        return {**(capabilities or {}), "diffusers_inspection": inspection}, "GGUF 只允許官方已建檔 profile，請從官方 GGUF 下拉選單選擇模型與精度。"
                    if not profile.get("enabled") or not profile_variant.get("enabled"):
                        return {**(capabilities or {}), "diffusers_inspection": inspection}, gguf_profile_unavailable_message(profile, profile_variant)
                    params["diffusers_gguf_profile"] = str(profile.get("id") or "")
                    params["diffusers_gguf_variant"] = str(profile_variant.get("id") or "")
                    params["diffusers_gguf_base_repo"] = str(profile.get("base_repo") or params.get("diffusers_gguf_base_repo") or "").strip()
                if len(variant_options) > 1 and not selected_variant and not selected_gguf_file:
                    return (
                        {**(capabilities or {}), "diffusers_inspection": inspection},
                        "這個 Hugging Face repo 有多個精度版本，請先在生圖頁面選擇要下載/載入的版本，避免重複下載。",
                    )
                if selected_gguf_file and selected_gguf_file not in valid_gguf_files:
                    return {**(capabilities or {}), "diffusers_inspection": inspection}, "選擇的 GGUF 檔案不在 Hugging Face repo metadata 中。"
                if not selected_gguf_file and variant_options and selected_variant not in valid_variants:
                    return {**(capabilities or {}), "diffusers_inspection": inspection}, "選擇的模型精度版本不在 Hugging Face repo metadata 中。"
                params["diffusers_model_variant"] = selected_variant
                params["diffusers_gguf_file"] = selected_gguf_file
                params["diffusers_model_card_hints"] = inspection.get("model_card_hints") or {}
                if selected_gguf_file:
                    base_repo = normalize_huggingface_repo_id(
                        params.get("diffusers_gguf_base_repo") or inspection.get("suggested_base_repo"),
                        allow_blank=True,
                    )
                    if base_repo is None:
                        return {**(capabilities or {}), "diffusers_inspection": inspection}, "GGUF base Diffusers repo 格式不合法。"
                    params["diffusers_gguf_base_repo"] = base_repo
            if params.get("loras"):
                return capabilities, "Diffusers 後端目前不支援本站 ComfyUI LoRA 選擇；請改用本地或遠端 ComfyUI 模式。"
            control = (params or {}).get("controlnet") if isinstance((params or {}).get("controlnet"), dict) else None
            if control:
                return capabilities, "Diffusers 後端目前不支援本站 ControlNet 快捷模式；請改用本地或遠端 ComfyUI 模式。"
            return capabilities, None
        if mode_definition.get("workflow_only"):
            return capabilities, "這個模式需要透過支援的大模型 workflow 模板執行，請先匯入或選擇對應 workflow。"
        required_nodes = {"CheckpointLoaderSimple", "CLIPTextEncode", "KSampler", "VAEDecode", "SaveImage"}
        if mode == "img2img":
            required_nodes.update({"LoadImage", "VAEEncode"})
        elif mode == "inpaint":
            required_nodes.update({"LoadImage", "LoadImageMask", "VAEEncodeForInpaint"})
        elif mode == "outpaint":
            required_nodes.update({"LoadImage", "ImagePadForOutpaint", "VAEEncodeForInpaint"})
        elif mode == "upscale":
            required_nodes = {"LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "SaveImage"}
            upscale_models = list((capabilities or {}).get("upscale_models") or [])
            resolved_upscale_model = resolve_model_option(
                str((params or {}).get("upscale_model") or "").strip(),
                upscale_models,
            )
            if not resolved_upscale_model:
                return None, "缺少對應的放大模型，請先安裝 scale model"
            params["upscale_model"] = resolved_upscale_model
        missing_nodes = sorted(node for node in required_nodes if node not in available_nodes)
        if missing_nodes:
            return None, f"ComfyUI 缺少必要 workflow node：{', '.join(missing_nodes)}"
        control = (params or {}).get("controlnet") if isinstance((params or {}).get("controlnet"), dict) else None
        if control:
            control_type = str(control.get("type") or "").strip().lower()
            type_info = ((capabilities or {}).get("controlnet_types") or {}).get(control_type) or {}
            if not type_info.get("available"):
                return None, f"ControlNet {CONTROLNET_TYPE_DEFINITIONS.get(control_type, {}).get('label', control_type)} 缺少對應 nodes 或 models"
            chosen_preprocessor = str(control.get("preprocessor") or type_info.get("default_preprocessor") or "").strip()
            if not chosen_preprocessor:
                return None, "找不到可用的 ControlNet preprocessor"
            if chosen_preprocessor not in set(type_info.get("available_preprocessors") or []):
                return None, f"ControlNet preprocessor 不可用：{chosen_preprocessor}"
            chosen_model = str(control.get("model_name") or "").strip()
            if not chosen_model:
                matching_models = list(type_info.get("matching_models") or [])
                if not matching_models:
                    return None, "缺少對應的 ControlNet 模型"
                control["model_name"] = matching_models[0]
            else:
                resolved_controlnet_model = resolve_model_option(
                    chosen_model,
                    type_info.get("matching_models") or [],
                )
                if not resolved_controlnet_model:
                    return None, f"ControlNet 模型不可用：{chosen_model}"
                control["model_name"] = resolved_controlnet_model
            control["preprocessor"] = chosen_preprocessor
            params["controlnet"] = control
        lora_options = list((capabilities or {}).get("loras") or [])
        if not lora_options and hasattr(active_client, "get_loras"):
            try:
                lora_options = list(active_client.get_loras() or [])
            except Exception:
                lora_options = []
        for lora in list((params or {}).get("loras") or []):
            if not isinstance(lora, dict):
                continue
            lora_name = str(lora.get("name") or "").strip()
            if not lora_name:
                continue
            resolved_lora = resolve_model_option(lora_name, lora_options) if lora_options else lora_name
            if lora_options and not resolved_lora:
                return None, f"LoRA 模型不可用：{lora_name}"
            lora["name"] = resolved_lora
        vae_name = str((params or {}).get("vae") or "").strip()
        if vae_name:
            vae_options = list((capabilities or {}).get("vaes") or [])
            if not vae_options and hasattr(active_client, "get_vaes"):
                try:
                    vae_options = list(active_client.get_vaes() or [])
                except Exception:
                    vae_options = []
            resolved_vae = resolve_model_option(vae_name, vae_options) if vae_options else vae_name
            if vae_options and not resolved_vae:
                return None, f"VAE 模型不可用：{vae_name}"
            params["vae"] = resolved_vae
        return capabilities, None

    def _assert_reasonable_image_size(image):
        size = len(getattr(image, "data", b"") or b"")
        if size > MAX_COMFYUI_FETCH_IMAGE_BYTES:
            raise ComfyUIError(f"ComfyUI image too large: {size} bytes")

    def _root_or_403():
        actor, err = _actor_or_401()
        if err:
            return None, err
        if _actor_value(actor, "username") != "root":
            return None, json_resp({"ok": False, "msg": "只有 root 可執行此操作"}, 403)
        return actor, None

    def _can_interrupt(actor):
        return (
            _actor_value(actor, "username") == "root"
            or _actor_value(actor, "role") in {"manager", "super_admin"}
        )

    def _generation_owner_id(actor):
        try:
            return int(_actor_value(actor, "id"))
        except Exception:
            return None

    def _coerce_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "y", "t"}
        return False

    def _maybe_fetch_outputs_kwarg(func, **kwargs):
        try:
            signature = inspect.signature(func)
            accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
        except (TypeError, ValueError):
            accepts_kwargs = True
            signature = None
        if signature is not None and not accepts_kwargs:
            for key in list(kwargs.keys()):
                if key not in signature.parameters:
                    kwargs.pop(key, None)
        return kwargs

    def _strip_comfyui_inline_data_urls(value):
        if isinstance(value, list):
            return [_strip_comfyui_inline_data_urls(item) for item in value]
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                if key == "data_url":
                    continue
                cleaned[key] = _strip_comfyui_inline_data_urls(item)
            return cleaned
        return value

    def _prune_generation_job_inline_payloads(conn, *, limit=50):
        try:
            rows = conn.execute(
                """
                SELECT job_id, result_json
                FROM comfyui_generation_jobs
                WHERE result_json LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                ('%"data_url"%', int(limit)),
            ).fetchall()
        except Exception:
            return
        for row in rows:
            result = _parse_json_field(row["result_json"], None)
            if result is None:
                continue
            stripped = _strip_comfyui_inline_data_urls(result)
            if stripped == result:
                continue
            conn.execute(
                "UPDATE comfyui_generation_jobs SET result_json=? WHERE job_id=?",
                (json.dumps(stripped, ensure_ascii=False, sort_keys=True), row["job_id"]),
            )

    def _ensure_generation_job_schema(conn):
        if generation_jobs_schema_ready["ready"]:
            return
        with generation_jobs_schema_lock:
            if generation_jobs_schema_ready["ready"]:
                return
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS comfyui_generation_jobs (
                    job_id TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    owner_username TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    error TEXT NOT NULL DEFAULT '',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comfyui_generation_jobs_owner "
                "ON comfyui_generation_jobs(owner_user_id, updated_at DESC)"
            )
            _prune_generation_job_inline_payloads(conn)
            conn.commit()
            generation_jobs_schema_ready["ready"] = True

    def _generation_job_from_row(row):
        if not row:
            return None
        progress = _parse_json_field(row["progress_json"], {}) or {}
        result = _parse_json_field(row["result_json"], None) if row["result_json"] else None
        result = _strip_comfyui_inline_data_urls(result) if result is not None else None
        return {
            "job_id": row["job_id"],
            "owner_user_id": int(row["owner_user_id"]),
            "owner_username": row["owner_username"] or "",
            "status": row["status"] or "queued",
            "error": row["error"] or "",
            "progress": progress if isinstance(progress, dict) else {},
            "result": result,
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }

    def _persist_generation_job(job):
        if not isinstance(job, dict) or not job.get("job_id"):
            return False
        result_payload = _strip_comfyui_inline_data_urls(job.get("result")) if job.get("result") is not None else None
        conn = get_db()
        try:
            _ensure_generation_job_schema(conn)
            conn.execute(
                """
                INSERT INTO comfyui_generation_jobs (
                    job_id, owner_user_id, owner_username, status, error,
                    progress_json, result_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    owner_user_id=excluded.owner_user_id,
                    owner_username=excluded.owner_username,
                    status=excluded.status,
                    error=excluded.error,
                    progress_json=excluded.progress_json,
                    result_json=excluded.result_json,
                    updated_at=excluded.updated_at
                """,
                (
                    str(job["job_id"]),
                    int(job.get("owner_user_id") or 0),
                    str(job.get("owner_username") or ""),
                    str(job.get("status") or "queued"),
                    str(job.get("error") or ""),
                    json.dumps(job.get("progress") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(result_payload, ensure_ascii=False, sort_keys=True) if result_payload is not None else None,
                    float(job.get("created_at") or time.time()),
                    float(job.get("updated_at") or time.time()),
                ),
            )
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return False
        finally:
            conn.close()

    def _load_generation_job_from_db(job_id):
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        conn = get_db()
        try:
            _ensure_generation_job_schema(conn)
            row = conn.execute(
                """
                SELECT job_id, owner_user_id, owner_username, status, error,
                       progress_json, result_json, created_at, updated_at
                FROM comfyui_generation_jobs
                WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            return _generation_job_from_row(row)
        finally:
            conn.close()

    def _register_active_generation(actor, *, backend_url="", backend_scope="primary"):
        generation_key = secrets.token_hex(12)
        with active_generations_lock:
            active_generations[generation_key] = {
                "user_id": _generation_owner_id(actor),
                "username": _actor_value(actor, "username", ""),
                "role": _actor_value(actor, "role", ""),
                "backend_url": _normalize_comfyui_backend_url(backend_url),
                "backend_scope": str(backend_scope or "primary"),
                "started_at": time.time(),
            }
        return generation_key

    def _unregister_active_generation(token):
        with active_generations_lock:
            active_generations.pop(token, None)

    def _create_generation_job(actor):
        job_id = secrets.token_hex(12)
        job = {
            "job_id": job_id,
            "owner_user_id": _generation_owner_id(actor),
            "owner_username": _actor_value(actor, "username", ""),
            "status": "queued",
            "error": "",
            "progress": {
                "phase": "queued",
                "percent": 0,
                "current": 0,
                "max": 0,
                "current_node": None,
                "queue_remaining": None,
                "detail": "已建立產圖工作",
                "completed": False,
                "updated_at": time.time(),
            },
            "result": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with generation_jobs_lock:
            generation_jobs[job_id] = job
        _persist_generation_job(job)
        try:
            from services.job_center import create_job as create_platform_job

            conn = get_db()
            try:
                create_platform_job(
                    conn,
                    owner_user_id=_generation_owner_id(actor),
                    created_by_user_id=_generation_owner_id(actor),
                    job_type="comfyui.generate",
                    title="ComfyUI 產圖",
                    description="ComfyUI 產圖 / workflow 執行工作",
                    source_module="comfyui",
                    source_ref=job_id,
                    status="queued",
                    progress_percent=0,
                    stage="queued",
                    stage_detail="已建立產圖工作",
                    cancellable=False,
                    metadata={"comfyui_job_id": job_id},
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        return job_id

    def _capture_request_audit_meta():
        try:
            client_ip = get_client_ip() or "-"
        except Exception:
            client_ip = "-"
        try:
            user_agent = get_ua() or "-"
        except Exception:
            user_agent = "-"
        return {
            "client_ip": client_ip,
            "user_agent": user_agent,
        }

    def _update_generation_job(job_id, **changes):
        job_id = str(job_id or "")
        with generation_jobs_lock:
            job = generation_jobs.get(job_id)
        if not job:
            job = _load_generation_job_from_db(job_id)
            if job:
                with generation_jobs_lock:
                    generation_jobs[job_id] = job
        if not job:
            return None
        with generation_jobs_lock:
            job = generation_jobs.get(job_id, job)
            if not job:
                return None
            for key, value in changes.items():
                if key == "result":
                    value = _strip_comfyui_inline_data_urls(value)
                job[key] = value
            job["updated_at"] = time.time()
            updated = dict(job)
        _persist_generation_job(updated)
        try:
            from services.job_center import add_job_event, get_job_by_source, update_job as update_platform_job

            conn = get_db()
            try:
                platform_job = get_job_by_source(conn, "comfyui", job_id)
                if platform_job:
                    status_map = {"completed": "succeeded", "error": "failed", "running": "running", "queued": "queued"}
                    next_status = status_map.get(str(updated.get("status") or ""), None)
                    payload = {}
                    if next_status:
                        payload["status"] = next_status
                        payload["stage"] = next_status
                    if next_status in {"succeeded", "failed", "cancelled", "expired"}:
                        payload["finished_at"] = __import__("datetime").datetime.utcnow().replace(microsecond=0).isoformat()
                    if updated.get("error"):
                        payload["error_message"] = str(updated.get("error") or "")[:1000]
                        payload["error_stage"] = "comfyui"
                    defer_progress = next_status not in {"succeeded", "failed", "cancelled", "expired"}
                    update_platform_job(conn, platform_job["job_uuid"], defer_progress=defer_progress, **payload)
                    add_job_event(
                        conn,
                        platform_job["job_uuid"],
                        event_type="updated",
                        stage=payload.get("stage"),
                        message=payload.get("error_message") or "ComfyUI 任務狀態更新",
                        defer_progress=defer_progress,
                    )
                    conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        return updated

    def _update_generation_job_progress(job_id, progress):
        job_id = str(job_id or "")
        now = time.time()
        with generation_jobs_lock:
            job = generation_jobs.get(job_id)
        if not job:
            job = _load_generation_job_from_db(job_id)
            if job:
                with generation_jobs_lock:
                    generation_jobs[job_id] = job
        if not job:
            return None
        with generation_jobs_lock:
            job = generation_jobs.get(job_id, job)
            if not job:
                return None
            job["progress"] = {
                **(job.get("progress") or {}),
                **(progress or {}),
                "updated_at": now,
            }
            if job["status"] in {"queued", "running"}:
                job["status"] = "running"
            job["updated_at"] = now
            previous_platform_progress = float(job.get("_last_platform_progress_at") or 0)
            previous_platform_signature = job.get("_last_platform_progress_signature")
            progress_data = job.get("progress") or {}
            progress_signature = (
                str(progress_data.get("phase") or ""),
                int(float(progress_data.get("percent") or 0)),
                str(progress_data.get("detail") or "")[:240],
            )
            should_write_platform_progress = (
                not previous_platform_progress
                or progress_signature != previous_platform_signature
                or COMFYUI_JOB_PROGRESS_DB_THROTTLE_SECONDS <= 0
                or now - previous_platform_progress >= COMFYUI_JOB_PROGRESS_DB_THROTTLE_SECONDS
            )
            if should_write_platform_progress:
                job["_last_platform_progress_at"] = now
                job["_last_platform_progress_signature"] = progress_signature
            updated = dict(job)
        _persist_generation_job(updated)
        if not should_write_platform_progress:
            return updated
        try:
            from services.job_center import add_job_event, get_job_by_source, update_job as update_platform_job

            conn = get_db()
            try:
                platform_job = get_job_by_source(conn, "comfyui", job_id)
                if platform_job:
                    progress_data = updated.get("progress") or {}
                    percent = int(float(progress_data.get("percent") or 0))
                    stage = str(progress_data.get("phase") or "running")[:80]
                    detail = str(progress_data.get("detail") or "")[:1000]
                    terminal_status_map = {
                        "completed": "succeeded",
                        "succeeded": "succeeded",
                        "error": "failed",
                        "failed": "failed",
                        "cancelled": "cancelled",
                        "canceled": "cancelled",
                        "expired": "expired",
                    }
                    next_status = terminal_status_map.get(stage, "running")
                    update_payload = {
                        "status": next_status,
                        "progress_percent": percent,
                        "stage": stage,
                        "stage_detail": detail,
                    }
                    if next_status in {"succeeded", "failed", "cancelled", "expired"}:
                        update_payload["finished_at"] = __import__("datetime").datetime.utcnow().replace(microsecond=0).isoformat()
                    if next_status == "failed":
                        update_payload["error_message"] = str(progress_data.get("error_message") or detail or "ComfyUI 產圖失敗")[:1000]
                        update_payload["error_stage"] = "comfyui"
                    update_platform_job(
                        conn,
                        platform_job["job_uuid"],
                        defer_progress=next_status == "running",
                        **update_payload,
                    )
                    add_job_event(
                        conn,
                        platform_job["job_uuid"],
                        event_type="progress",
                        stage=stage,
                        message=detail,
                        progress_percent=percent,
                        payload=progress_data,
                        defer_progress=True,
                    )
                    conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        return updated

    def _generation_job_progress_snapshot(job_id):
        job_id = str(job_id or "")
        with generation_jobs_lock:
            job = generation_jobs.get(job_id)
            if job:
                return dict(job.get("progress") or {})
        job = _load_generation_job_from_db(job_id)
        return dict((job or {}).get("progress") or {})

    def _diffusers_error_progress(job_id, exc):
        previous = _generation_job_progress_snapshot(job_id)
        previous_step = str(previous.get("step") or "").strip()
        progress = {
            "phase": "error",
            "percent": 100,
            "detail": str(exc),
            "error_message": str(exc),
            "completed": False,
            "backend_kind": "diffusers",
            "step": previous_step if previous.get("phase") == "error" and previous_step else "Diffusers 產圖失敗",
        }
        python_log_tail = previous.get("python_log_tail")
        if isinstance(python_log_tail, list):
            progress["python_log_tail"] = python_log_tail[-200:]
        return progress

    def _get_generation_job(job_id):
        job_id = str(job_id or "")
        with generation_jobs_lock:
            job = generation_jobs.get(job_id)
            if job and str(job.get("status") or "") not in {"queued", "running"}:
                return dict(job)
            cached_job = dict(job) if job else None
        db_job = _load_generation_job_from_db(job_id)
        if db_job:
            with generation_jobs_lock:
                current = generation_jobs.get(job_id)
                if (
                    not current
                    or float(db_job.get("updated_at") or 0) >= float(current.get("updated_at") or 0)
                    or str(current.get("status") or "") in {"queued", "running"}
                ):
                    generation_jobs[job_id] = db_job
                    return dict(db_job)
                return dict(current)
        return cached_job

    def _assert_generation_job_owner(job_id, actor):
        job = _get_generation_job(job_id)
        if not job:
            return None, json_resp({"ok": False, "msg": "找不到 ComfyUI 產圖工作"}, 404)
        if int(job.get("owner_user_id") or 0) != int(_generation_owner_id(actor) or 0):
            return None, json_resp({"ok": False, "msg": "無權查看此 ComfyUI 工作"}, 403)
        return job, None

    def _comfyui_prompt_runtime_presence(actor, prompt_id):
        prompt_id = str(prompt_id or "").strip()
        if not actor or not prompt_id:
            return "unknown", {"reason": "missing_actor_or_prompt_id"}
        try:
            binding = _comfyui_binding(actor)
            client = _client_for_url(binding["url"])
            history = client._json_request(f"/history/{quote(prompt_id, safe='')}", timeout=3)
            if isinstance(history, dict) and isinstance(history.get(prompt_id), dict):
                return "history", {"backend_url": binding.get("url")}
            queue = client._json_request("/queue", timeout=3)
            if prompt_id in json.dumps(queue, ensure_ascii=False):
                return "queue", {"backend_url": binding.get("url")}
            return "missing", {"backend_url": binding.get("url")}
        except Exception as exc:
            return "unknown", {"error": str(exc)[:300]}

    def _generation_job_payload(job, actor=None):
        payload = {
            "job_id": job["job_id"],
            "status": job["status"],
            "progress": dict(job.get("progress") or {}),
            "error": job.get("error") or "",
            "result": _strip_comfyui_inline_data_urls(job.get("result")),
        }
        if payload["status"] == "completed":
            progress = payload["progress"]
            progress["phase"] = "completed"
            progress["percent"] = 100
            progress["completed"] = True
        if payload["status"] in {"queued", "running"}:
            progress = payload["progress"]
            progress_phase = str(progress.get("phase") or "").strip().lower()
            if progress.get("completed") is True or progress_phase in {"completed", "succeeded"}:
                progress["result_pending"] = payload.get("result") is None
                if progress["result_pending"]:
                    progress["detail"] = progress.get("detail") or "ComfyUI 已完成輸出，正在整理站內結果"
                return payload
            updated_at = float(progress.get("updated_at") or job.get("updated_at") or job.get("created_at") or 0)
            stale_seconds = int(time.time() - updated_at) if updated_at else 0
            if updated_at and stale_seconds >= COMFYUI_JOB_STALE_SECONDS:
                prompt_id = str(progress.get("prompt_id") or "").strip()
                presence, presence_meta = _comfyui_prompt_runtime_presence(actor, prompt_id)
                if prompt_id and presence == "missing":
                    detail = (
                        "ComfyUI 工作已超過 stale 門檻且 prompt 不在 history/queue 中；"
                        "可能已被 interrupt、後端重啟或 ComfyUI 清空佇列。"
                    )
                    terminal_progress = {
                        **progress,
                        "phase": "error",
                        "detail": detail,
                        "error_message": detail,
                        "completed": False,
                        "terminal_reason": "comfyui_prompt_missing_from_queue_history",
                        "stale_seconds": stale_seconds,
                        "prompt_runtime_presence": presence,
                        "prompt_runtime_presence_meta": presence_meta,
                    }
                    _update_generation_job_progress(job["job_id"], terminal_progress)
                    _update_generation_job(job["job_id"], status="error", error=detail, result=job.get("result"))
                    payload["status"] = "error"
                    payload["error"] = detail
                    payload["progress"] = terminal_progress
                    return payload
                is_diffusers = str(progress.get("backend_kind") or "").strip().lower() == "diffusers"
                if is_diffusers:
                    progress_step = str(progress.get("step") or "")
                    python_tail = "\n".join(str(line or "") for line in progress.get("python_log_tail") or [])
                    bytes_written = int(progress.get("bytes_written") or 0)
                    total_bytes = int(progress.get("total_bytes") or 0)
                    if (
                        progress.get("cache_hit") is True
                        or (bytes_written <= 0 and total_bytes <= 0 and "Download complete" in python_tail)
                    ):
                        stale_detail = (
                            "Diffusers / Hugging Face 暫時沒有回報新進度；前一階段已命中本機 cache，"
                            "未偵測到網路下載位元組，目前較可能正在載入 pipeline、初始化 GPU，"
                            "或被磁碟/VRAM 壓力拖慢。"
                        )
                    elif progress_phase == "loading" or "pipeline" in progress_step.lower() or "Loading pipeline components" in python_tail:
                        stale_detail = (
                            "Diffusers / Hugging Face 暫時沒有回報新進度，目前正在載入 pipeline、初始化 GPU，"
                            "或被磁碟/VRAM 壓力拖慢；這不代表仍在網路下載。"
                        )
                    elif bytes_written > 0 or total_bytes > 0:
                        stale_detail = (
                            "Diffusers / Hugging Face 暫時沒有回報新進度，可能仍在下載大型模型，"
                            "或被網路/磁碟壓力拖慢。"
                        )
                    else:
                        stale_detail = (
                            "Diffusers / Hugging Face 暫時沒有回報新進度，可能正在檢查 Hugging Face cache、"
                            "載入 pipeline，或被磁碟/VRAM 壓力拖慢。"
                        )
                progress["phase"] = "backend_unresponsive"
                progress["detail"] = stale_detail if is_diffusers else "ComfyUI 後端暫時沒有回報進度，可能正在載入大模型或被磁碟/VRAM 壓力拖慢"
                progress["backend_unresponsive"] = True
                progress["stale_seconds"] = int(time.time() - updated_at)
                progress["timeout_seconds"] = int(progress.get("timeout_seconds") or DEFAULT_GENERATION_TIMEOUT_SECONDS)
                progress["timeout_unlimited"] = int(progress.get("timeout_seconds") or 0) <= 0
                if (
                    progress.get("backend_unresponsive") is True
                    and progress["stale_seconds"] >= COMFYUI_BACKEND_UNRESPONSIVE_FAIL_SECONDS
                ):
                    detail = (
                        "ComfyUI 後端已超過 "
                        f"{int(COMFYUI_BACKEND_UNRESPONSIVE_FAIL_SECONDS)} 秒沒有回覆；"
                        "此工作已標記失敗，請檢查或重啟 ComfyUI 後重新送出。"
                    )
                    terminal_progress = {
                        **progress,
                        "phase": "error",
                        "detail": detail,
                        "error_message": detail,
                        "completed": False,
                        "terminal_reason": "backend_unresponsive_timeout",
                    }
                    _update_generation_job_progress(job["job_id"], terminal_progress)
                    _update_generation_job(job["job_id"], status="error", error=detail, result=job.get("result"))
                    payload["status"] = "error"
                    payload["error"] = detail
                    payload["progress"] = terminal_progress
        return payload

    def _active_generation_snapshot():
        with active_generations_lock:
            return list(active_generations.values())

    def _interrupt_policy(actor):
        if _actor_value(actor, "username") == "root":
            return True, "root_force", {}
        actor_id = _generation_owner_id(actor)
        binding = _comfyui_binding(actor)
        target_backend_url = _normalize_comfyui_backend_url(binding.get("url"))
        active = _active_generation_snapshot()
        own = [item for item in active if item.get("user_id") == actor_id]
        own_backend_urls = {
            _normalize_comfyui_backend_url(item.get("backend_url"))
            for item in own
            if _normalize_comfyui_backend_url(item.get("backend_url"))
        }
        if not own_backend_urls and target_backend_url:
            own_backend_urls.add(target_backend_url)
        others = [
            item for item in active
            if item.get("user_id") != actor_id
            and _normalize_comfyui_backend_url(item.get("backend_url")) in own_backend_urls
        ]
        summary = {
            "own_active": len(own),
            "other_active_same_backend": len(others),
            "total_active": len(active),
        }
        if not own:
            return False, "no_owned_generation", summary
        if others:
            return False, "shared_backend_busy", summary
        return True, "owned_generation_only", summary

    _admin_helpers = build_comfyui_admin_helpers({
        "DEFAULT_COMFYUI_URL": DEFAULT_COMFYUI_URL,
        "SAFE_SAMPLER_FALLBACK": SAFE_SAMPLER_FALLBACK,
        "SAFE_SCHEDULER_FALLBACK": SAFE_SCHEDULER_FALLBACK,
        "COMFYUI_LOCAL_START_TEMPLATE_PATH": COMFYUI_LOCAL_START_TEMPLATE_PATH,
        "COMFYUI_MODEL_DOWNLOAD_EXTENSIONS": COMFYUI_MODEL_DOWNLOAD_EXTENSIONS,
        "COMFYUI_MODEL_DOWNLOAD_TYPES": COMFYUI_MODEL_DOWNLOAD_TYPES,
        "COMFYUI_SUPPORTED_LORA_BASE_MODEL_FAMILIES": COMFYUI_SUPPORTED_LORA_BASE_MODEL_FAMILIES,
        "MAX_COMFYUI_MODEL_DOWNLOAD_BYTES": MAX_COMFYUI_MODEL_DOWNLOAD_BYTES,
        "COMFYUI_BACKEND_REQUEST_TIMEOUT_SECONDS": COMFYUI_BACKEND_REQUEST_TIMEOUT_SECONDS,
        "CIVITAI_ALLOWED_HOSTS": CIVITAI_ALLOWED_HOSTS,
        "CIVITAI_API_BASE": CIVITAI_API_BASE,
        "CIVITAI_API_BASES": CIVITAI_API_BASES,
        "CIVITAI_MODEL_TYPE_TO_DOWNLOAD_TYPE": CIVITAI_MODEL_TYPE_TO_DOWNLOAD_TYPE,
        "CIVITAI_SEARCH_TYPE_TO_API": CIVITAI_SEARCH_TYPE_TO_API,
        "MemoryFile": _MemoryFile,
        "actor_value": _actor_value,
        "audit": audit,
        "deps": deps,
        "get_client_ip": get_client_ip,
        "generation_owner_id": _generation_owner_id,
        "get_system_settings": get_system_settings,
        "get_ua": get_ua,
        "injected_client": injected_client,
        "json_resp": json_resp,
        "model_download_jobs": model_download_jobs,
        "model_download_jobs_lock": model_download_jobs_lock,
    })
    _validate_comfyui_host = _admin_helpers["_validate_comfyui_host"]
    _parse_comfyui_endpoint = _admin_helpers["_parse_comfyui_endpoint"]
    _validate_comfyui_api_url = _admin_helpers["_validate_comfyui_api_url"]
    _normalize_comfyui_backend_url = _admin_helpers["_normalize_comfyui_backend_url"]
    _configured_connection_mode = _admin_helpers["_configured_connection_mode"]
    _configured_comfyui_url = _admin_helpers["_configured_comfyui_url"]
    _comfyui_binding = _admin_helpers["_comfyui_binding"]
    _configured_local_start_script = _admin_helpers["_configured_local_start_script"]
    _configured_comfyui_port = _admin_helpers["_configured_comfyui_port"]
    _local_comfyui_state_path = _admin_helpers["_local_comfyui_state_path"]
    _write_local_comfyui_state = _admin_helpers["_write_local_comfyui_state"]
    _read_local_comfyui_state = _admin_helpers["_read_local_comfyui_state"]
    _clear_local_comfyui_state = _admin_helpers["_clear_local_comfyui_state"]
    _tail_text_lines = _admin_helpers["_tail_text_lines"]
    _local_comfyui_runtime_status = _admin_helpers["_local_comfyui_runtime_status"]
    _pid_exists = _admin_helpers["_pid_exists"]
    _pid_cmdline = _admin_helpers["_pid_cmdline"]
    _listener_pids_for_port = _admin_helpers["_listener_pids_for_port"]
    _proc_scan_comfyui_pids = _admin_helpers["_proc_scan_comfyui_pids"]
    _looks_like_comfyui_process = _admin_helpers["_looks_like_comfyui_process"]
    _terminate_local_comfyui_targets = _admin_helpers["_terminate_local_comfyui_targets"]
    _stop_local_comfyui = _admin_helpers["_stop_local_comfyui"]
    _local_start_script_status = _admin_helpers["_local_start_script_status"]
    _start_local_comfyui = _admin_helpers["_start_local_comfyui"]
    _configured_max_batch_size = _admin_helpers["_configured_max_batch_size"]
    _configured_default_dimensions = _admin_helpers["_configured_default_dimensions"]
    _configured_comfyui_base_dir = _admin_helpers["_configured_comfyui_base_dir"]
    _configured_comfyui_project_dir = _admin_helpers["_configured_comfyui_project_dir"]
    _configured_civitai_api_key = _admin_helpers["_configured_civitai_api_key"]
    _public_download_host = _admin_helpers["_public_download_host"]
    _safe_model_filename = _admin_helpers["_safe_model_filename"]
    _normalize_download_model_type = _admin_helpers["_normalize_download_model_type"]
    _filename_from_content_disposition = _admin_helpers["_filename_from_content_disposition"]
    _append_civitai_token = _admin_helpers["_append_civitai_token"]
    _civitai_headers = _admin_helpers["_civitai_headers"]
    _comfyui_model_sidecar_path = _admin_helpers["_comfyui_model_sidecar_path"]
    _normalize_model_relative_dir = _admin_helpers["_normalize_model_relative_dir"]
    _split_model_relative_name = _admin_helpers["_split_model_relative_name"]
    _resolve_model_destination_dir = _admin_helpers["_resolve_model_destination_dir"]
    _comfyui_model_sidecar_path_with_relative = _admin_helpers["_comfyui_model_sidecar_path_with_relative"]
    _write_comfyui_model_sidecar = _admin_helpers["_write_comfyui_model_sidecar"]
    _read_comfyui_model_sidecar = _admin_helpers["_read_comfyui_model_sidecar"]
    _normalize_lora_base_model_family = _admin_helpers["_normalize_lora_base_model_family"]
    _lora_support_payload = _admin_helpers["_lora_support_payload"]
    _build_lora_details = _admin_helpers["_build_lora_details"]
    _public_or_civitai_host = _admin_helpers["_public_or_civitai_host"]
    _parse_civitai_reference = _admin_helpers["_parse_civitai_reference"]
    _fetch_json = _admin_helpers["_fetch_json"]
    _civitai_api_get = _admin_helpers["_civitai_api_get"]
    _normalize_civitai_search_type = _admin_helpers["_normalize_civitai_search_type"]
    _normalize_civitai_nsfw_mode = _admin_helpers["_normalize_civitai_nsfw_mode"]
    _serialize_civitai_file = _admin_helpers["_serialize_civitai_file"]
    _serialize_civitai_versions = _admin_helpers["_serialize_civitai_versions"]
    _build_civitai_page_url = _admin_helpers["_build_civitai_page_url"]
    _safe_civitai_media_url = _admin_helpers["_safe_civitai_media_url"]
    _fetch_civitai_media = _admin_helpers["_fetch_civitai_media"]
    _serialize_civitai_search_results = _admin_helpers["_serialize_civitai_search_results"]
    _search_civitai_models = _admin_helpers["_search_civitai_models"]
    _inspect_civitai_model = _admin_helpers["_inspect_civitai_model"]
    _create_model_download_job = _admin_helpers["_create_model_download_job"]
    _update_model_download_job = _admin_helpers["_update_model_download_job"]
    _update_model_download_progress = _admin_helpers["_update_model_download_progress"]
    _get_model_download_job = _admin_helpers["_get_model_download_job"]
    _assert_model_download_job_owner = _admin_helpers["_assert_model_download_job_owner"]
    _parse_civitai_download_request = _admin_helpers["_parse_civitai_download_request"]
    _download_comfyui_model_file = _admin_helpers["_download_comfyui_model_file"]
    _download_civitai_model_selection = _admin_helpers["_download_civitai_model_selection"]
    _upload_comfyui_model_file = _admin_helpers["_upload_comfyui_model_file"]
    _client = _admin_helpers["_client"]
    _client_for_url = _admin_helpers["_client_for_url"]

    def _comfyui_storage_warnings():
        warnings = []
        candidates = [
            ("base_dir", _configured_comfyui_base_dir()),
            ("project_dir", _configured_comfyui_project_dir()),
        ]
        seen = set()
        for label, raw_path in candidates:
            path_text = str(raw_path or "").strip()
            if not path_text or path_text in seen:
                continue
            seen.add(path_text)
            normalized = Path(path_text).as_posix()
            if normalized.startswith("/mnt/"):
                warnings.append({
                    "code": "windows_mount_model_storage",
                    "path": path_text,
                    "label": label,
                    "message": "ComfyUI 模型或專案位於 /mnt/* Windows 掛載路徑；大型模型載入可能拖慢小主機，建議把常用模型移到 WSL/Linux native storage。",
                })
        return warnings
    del _admin_helpers

    _billing_helpers = build_comfyui_billing_helpers({
        "COMFYUI_BASIC_PRICE_ITEM_KEY": COMFYUI_BASIC_PRICE_ITEM_KEY,
        "COMFYUI_LORA_EXTRA_PRICE_POINTS": COMFYUI_LORA_EXTRA_PRICE_POINTS,
        "COMFYUI_VAE_BUILTIN": COMFYUI_VAE_BUILTIN,
        "actor_value": _actor_value,
        "get_member_level_rule": get_member_level_rule,
        "points_service": points_service,
    })
    _is_root = _billing_helpers["_is_root"]
    _comfyui_charge_required = _billing_helpers["_comfyui_charge_required"]
    _comfyui_wallet_payload = _billing_helpers["_comfyui_wallet_payload"]
    _comfyui_lora_count = _billing_helpers["_comfyui_lora_count"]
    _comfyui_price_quote = _billing_helpers["_comfyui_price_quote"]
    _comfyui_total_quantity = _billing_helpers["_comfyui_total_quantity"]
    _ensure_comfyui_balance = _billing_helpers["_ensure_comfyui_balance"]
    _charge_comfyui_generation = _billing_helpers["_charge_comfyui_generation"]
    del _billing_helpers

    def _json_error_from_comfy(exc, active_client=None):
        active_client = active_client or _client()
        return json_resp({
            "ok": False,
            "msg": str(exc),
            "connection_mode": _configured_connection_mode(),
            "comfyui_url": getattr(active_client, "base_url", _configured_comfyui_url()),
        }), 503

    def _serialize_generation_result(actor, params, result, billing):
        result_images = result.get("images") if isinstance(result.get("images"), list) else []
        if not result_images:
            result_images = [{
                "image_ref": result["image_ref"],
                "mime_type": result["mime_type"],
                "data": result["data"],
            }]
        images = []
        for index, item in enumerate(result_images):
            raw_data = item.get("data") or b""
            mime_type = item.get("mime_type") or result.get("mime_type") or "image/png"
            image_ref_item = item.get("image_ref") if isinstance(item.get("image_ref"), dict) else result["image_ref"]
            images.append({
                "prompt_id": result["prompt_id"],
                "image_ref": image_ref_item,
                "mime_type": mime_type,
                "size_bytes": len(raw_data),
                "seed": params["seed"],
                "model": params["model"],
                "batch_size": params["batch_size"],
                "batch_index": index,
            })
        image = images[0]
        return {
            "image": image,
            "images": images,
            "billing": billing,
        }

    def _finalize_generation_records(actor, params, result, *, backend_url="", notify=True):
        result_images = result.get("images") if isinstance(result.get("images"), list) else []
        if not result_images:
            result_images = [{
                "image_ref": result["image_ref"],
                "mime_type": result["mime_type"],
                "data": result["data"],
            }]
        images = []
        for index, item in enumerate(result_images):
            raw_data = item.get("data") or b""
            mime_type = item.get("mime_type") or result.get("mime_type") or "image/png"
            image_ref_item = item.get("image_ref") if isinstance(item.get("image_ref"), dict) else result["image_ref"]
            size_bytes = int(item.get("size_bytes") or len(raw_data) or 0)
            output_node_id = str(item.get("output_node_id") or image_ref_item.get("output_node_id") or "").strip()
            output_label = _safe_text(item.get("output_label") or image_ref_item.get("output_label") or "", 260)
            images.append({
                "prompt_id": result["prompt_id"],
                "image_ref": image_ref_item,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "seed": params.get("seed"),
                "model": params.get("model") or params.get("checkpoint") or params.get("diffusion_model") or "",
                "batch_size": params.get("batch_size") or len(result_images) or 1,
                "batch_index": index,
                "output_node_id": output_node_id,
                "output_label": output_label,
            })
        conn = get_db()
        try:
            _register_comfyui_image_refs(conn, actor=actor, images=images, backend_url=backend_url)
            if notify:
                create_notification_if_enabled(
                    conn,
                    user_id=_actor_value(actor, "id"),
                    type="comfyui_generation_completed",
                    title="ComfyUI 產圖完成",
                    body=f"你的 ComfyUI 產圖已完成，共產生 {len(images)} 張圖片。",
                    link="/comfyui",
                )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
        return images

    def _looks_like_decodable_image_bytes(data):
        if not isinstance(data, (bytes, bytearray)) or not data:
            return False
        header = bytes(data[:16])
        return (
            header.startswith(b"\x89PNG\r\n\x1a\n")
            or header.startswith(b"\xff\xd8\xff")
            or header.startswith(b"RIFF") and header[8:12] == b"WEBP"
            or header.startswith(b"GIF87a")
            or header.startswith(b"GIF89a")
        )

    def _comfyui_image_quality_issue(data):
        if not isinstance(data, (bytes, bytearray)) or not data:
            return "圖片內容為空"
        if not _looks_like_decodable_image_bytes(data):
            return ""
        try:
            from PIL import Image, ImageStat

            with Image.open(BytesIO(bytes(data))) as image:
                sample = image.convert("RGB")
                sample.thumbnail((256, 256))
                stat = ImageStat.Stat(sample)
        except Exception as exc:
            return f"圖片無法解析：{exc}"
        extrema = stat.extrema or []
        means = stat.mean or []
        if not extrema or not means:
            return "圖片統計資訊為空"
        max_channel = max((pair[1] for pair in extrema), default=0)
        min_channel = min((pair[0] for pair in extrema), default=0)
        dynamic_range = max_channel - min_channel
        mean_value = sum(float(value or 0) for value in means) / max(len(means), 1)
        if max_channel <= 2 and mean_value <= 1.5:
            return "圖片幾乎全黑"
        if min_channel >= 253 and mean_value >= 253:
            return "圖片幾乎全白"
        if dynamic_range <= 2:
            return "圖片幾乎是單一純色"
        return ""

    def _assert_comfyui_final_images_usable(result):
        result_images = result.get("images") if isinstance(result.get("images"), list) else []
        if not result_images and result.get("image_ref"):
            result_images = [result]
        if not result_images:
            return
        issues = []
        for index, item in enumerate(result_images, start=1):
            if not isinstance(item, dict):
                continue
            image_ref = item.get("image_ref") if isinstance(item.get("image_ref"), dict) else {}
            filename = str(image_ref.get("filename") or f"#{index}")
            issue = _comfyui_image_quality_issue(item.get("data") or b"")
            if issue:
                issues.append(f"{filename}: {issue}")
        if issues:
            raise ComfyUIError("ComfyUI 輸出品質檢查失敗：" + "；".join(issues[:5]))

    def _is_qwen_edit_workflow_family_id(value):
        workflow_id = str(value or "").strip()
        return workflow_id == "origin_qwen_image_edit_2509" or workflow_id.startswith("origin_qwen_image_edit_2509_")

    def _workflow_requested_output_size(params):
        try:
            width = int(float(
                (params or {}).get("requested_width")
                or (params or {}).get("output_width")
                or (params or {}).get("width")
                or 0
            ))
            height = int(float(
                (params or {}).get("requested_height")
                or (params or {}).get("output_height")
                or (params or {}).get("height")
                or 0
            ))
        except Exception:
            return 0, 0
        if width < 64 or height < 64 or width > 4096 or height > 4096:
            return 0, 0
        return width, height

    def _resize_qwen_workflow_result_images_if_needed(active_client, result, params, *, actor=None, job_id, audit_ip="-", audit_ua="-"):
        workflow_id = str((params or {}).get("workflow_system_bundle_id") or "").strip()
        if not _is_qwen_edit_workflow_family_id(workflow_id):
            return result
        target_width, target_height = _workflow_requested_output_size(params)
        if not target_width or not target_height:
            return result
        result_images = result.get("images") if isinstance(result.get("images"), list) else []
        if not result_images:
            return result
        try:
            from PIL import Image
        except Exception as exc:
            raise ComfyUIError(f"Qwen Edit 輸出尺寸保真需要 Pillow，但載入失敗：{exc}") from exc
        changed = False
        next_images = []
        for index, item in enumerate(result_images, start=1):
            if not isinstance(item, dict):
                next_images.append(item)
                continue
            raw_data = item.get("data") or b""
            if not raw_data:
                next_images.append(item)
                continue
            try:
                with Image.open(BytesIO(bytes(raw_data))) as image:
                    original_size = tuple(image.size)
                    if original_size == (target_width, target_height):
                        next_images.append(item)
                        continue
                    resized = image.convert("RGB").resize((target_width, target_height), Image.Resampling.LANCZOS)
                    out = BytesIO()
                    resized.save(out, format="PNG", optimize=True)
                    resized_data = out.getvalue()
            except Exception as exc:
                raise ComfyUIError(f"Qwen Edit 輸出尺寸檢查/調整失敗：{exc}") from exc
            filename = f"qwen_edit_output_{target_width}x{target_height}_{hashlib.sha1(resized_data).hexdigest()[:12]}.png"
            try:
                uploaded_ref = active_client.upload_image_bytes(
                    resized_data,
                    filename,
                    image_type="output",
                    overwrite=False,
                    subfolder=f"hackme/{_actor_value(actor or {}, 'id', '')}".strip("/"),
                )
            except Exception as exc:
                raise ComfyUIError(f"Qwen Edit {target_width}x{target_height} 輸出回寫 ComfyUI 失敗：{exc}") from exc
            previous_ref = item.get("image_ref") if isinstance(item.get("image_ref"), dict) else {}
            next_ref = dict(uploaded_ref or {})
            for key in ("output_node_id", "output_label"):
                value = item.get(key) or previous_ref.get(key)
                if value:
                    next_ref[key] = value
            next_item = dict(item)
            next_item["data"] = resized_data
            next_item["mime_type"] = "image/png"
            next_item["size_bytes"] = len(resized_data)
            next_item["image_ref"] = next_ref
            next_images.append(next_item)
            changed = True
            audit(
                "COMFYUI_QWEN_EDIT_OUTPUT_RESIZE",
                audit_ip,
                user=_actor_value(actor or {}, "username", "-"),
                success=True,
                ua=audit_ua,
                detail=f"job_id={job_id}, index={index}, original={original_size[0]}x{original_size[1]}, target={target_width}x{target_height}, file={next_ref.get('filename')}",
            )
        if not changed:
            return result
        next_result = dict(result)
        next_result["images"] = next_images
        if next_images:
            next_result["image_ref"] = next_images[0].get("image_ref") or result.get("image_ref")
            next_result["mime_type"] = next_images[0].get("mime_type") or result.get("mime_type")
            next_result["data"] = next_images[0].get("data") or result.get("data")
        return next_result

    def _serialize_comfyui_media_records(result):
        import mimetypes

        media = result.get("media") if isinstance(result.get("media"), dict) else {}
        items = []
        for media_kind, records in media.items():
            if media_kind not in {"videos", "audio", "other"} or not isinstance(records, list):
                continue
            for index, item in enumerate(records):
                raw_data = item.get("data") or b""
                file_ref = item.get("file_ref") if isinstance(item.get("file_ref"), dict) else item.get("image_ref")
                if not file_ref:
                    continue
                filename = str(file_ref.get("filename") or "")
                guessed_mime = mimetypes.guess_type(filename)[0]
                fallback_mime = "video/mp4" if media_kind == "videos" else ("audio/mpeg" if media_kind == "audio" else "application/octet-stream")
                mime_type = item.get("mime_type") or guessed_mime or fallback_mime
                if str(mime_type).split(";", 1)[0].strip().lower() == "application/octet-stream":
                    mime_type = guessed_mime or fallback_mime
                size_bytes = int(item.get("size_bytes") or len(raw_data) or 0)
                items.append({
                    "prompt_id": result.get("prompt_id") or "",
                    "media_kind": "video" if media_kind == "videos" else ("audio" if media_kind == "audio" else "file"),
                    "file_ref": file_ref,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                    "batch_index": index,
                })
        return items

    def _initial_generation_progress(active_client, params=None, timeout_seconds=0):
        params = params if isinstance(params, dict) else {}
        timeout_value = int(timeout_seconds or 0)
        if getattr(active_client, "backend_kind", "") == "diffusers":
            repo = str(params.get("diffusers_model_repo") or params.get("model") or getattr(active_client, "model_repo", "") or "").strip()
            variant = str(params.get("diffusers_gguf_file") or params.get("diffusers_model_variant") or "default").strip() or "default"
            if str(params.get("diffusers_gguf_file") or "").strip():
                return {
                    "phase": "routing",
                    "percent": 1,
                    "backend_kind": "diffusers",
                    "detail": f"準備 GGUF profile：{repo or 'Hugging Face repo'}（{variant}），正在檢查遠端模型或本地匯入條件",
                    "step": "建立 GGUF 檢查工作",
                    "current_file": str(params.get("diffusers_gguf_file") or ""),
                    "timeout_seconds": timeout_value,
                    "timeout_unlimited": timeout_value <= 0,
                }
            return {
                "phase": "downloading",
                "percent": 1,
                "backend_kind": "diffusers",
                "detail": f"下載 Diffusers model：{repo or 'Hugging Face repo'}（{variant}），正在準備模型快取",
                "step": "建立 Diffusers 下載工作",
                "current_file": str(params.get("diffusers_gguf_file") or ""),
                "timeout_seconds": timeout_value,
                "timeout_unlimited": timeout_value <= 0,
            }
        return {
            "phase": "queued",
            "percent": 0,
            "detail": "已送出至 ComfyUI 背景工作",
            "timeout_seconds": timeout_value,
            "timeout_unlimited": timeout_value <= 0,
        }

    def _configured_native_comfyui_url():
        if _configured_connection_mode() != "diffusers":
            return _configured_comfyui_url()
        settings = get_system_settings() or {}
        configured_url = str(settings.get("comfyui_remote_api_url") or "").strip()
        if configured_url:
            url, msg = _validate_comfyui_api_url(configured_url)
            if not msg:
                return url
        default_url = urlparse(DEFAULT_COMFYUI_URL)
        host = str(settings.get("comfyui_api_host") or default_url.hostname or os.environ.get("COMFYUI_API_HOST") or "localhost").strip()
        host = host.strip("[]") or "localhost"
        try:
            port = int(settings.get("comfyui_api_port") or default_url.port or DEFAULT_COMFYUI_PORT)
        except Exception:
            port = DEFAULT_COMFYUI_PORT
        port = min(65535, max(1, port))
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"http://{display_host}:{port}"

    def _native_comfyui_binding_for_gguf(actor):
        native_url = _configured_native_comfyui_url()
        binding = _comfyui_binding(actor, backend_url=native_url)
        binding["backend_scope"] = "auto_comfyui_gguf"
        return binding

    def _is_local_comfyui_url(url):
        try:
            host = str(urlparse(str(url or "")).hostname or "").strip().lower()
        except Exception:
            return False
        return host in {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}

    def _select_exact_or_basename(candidates, options):
        for candidate in candidates:
            resolved = resolve_model_option(candidate, options or [])
            if resolved:
                return resolved
        return ""

    def _select_enum_option(current, options, preferred):
        values = [str(item or "").strip() for item in options or [] if str(item or "").strip()]
        current = str(current or "").strip()
        if current and current in values:
            return current
        for candidate in preferred:
            if candidate in values:
                return candidate
        return values[0] if values else (preferred[0] if preferred else "")

    def _select_gguf_profile_runtime_models(native_client, capabilities, profile, variant, gguf_file):
        available_nodes = set((capabilities or {}).get("available_nodes") or [])
        clip_loader_class = str((profile or {}).get("clip_loader_class") or "DualCLIPLoaderGGUF").strip() or "DualCLIPLoaderGGUF"
        workflow_family = str((profile or {}).get("workflow_family") or "").strip()
        required_nodes = {
            "UnetLoaderGGUF",
            clip_loader_class,
            "CLIPTextEncode",
            "KSampler",
            "VAEDecode",
            "VAELoader",
            "SaveImage",
        }
        if workflow_family == "sd3_triple_clip_gguf":
            required_nodes.update({
                "ModelSamplingSD3",
                "EmptySD3LatentImage",
                "ConditioningZeroOut",
                "ConditioningSetTimestepRange",
                "ConditioningCombine",
                "ImageScale",
            })
        else:
            required_nodes.add("EmptyLatentImage")
        missing_nodes = sorted(required_nodes - available_nodes)
        if missing_nodes:
            raise ComfyUIError(
                "ComfyUI-GGUF 自動路由缺少 workflow 節點："
                + ", ".join(missing_nodes)
                + "。請在 ComfyUI 安裝/啟用 ComfyUI-GGUF custom node，並確認 profile 對應節點可用。"
            )
        diffusion_options = list((capabilities or {}).get("diffusion_models") or [])
        unet_name = resolve_model_option(gguf_file or (variant or {}).get("gguf_file"), diffusion_options)
        clip_options = list((capabilities or {}).get("clip_models") or [])
        vae_options = list((capabilities or {}).get("vaes") or [])
        resolved = {}
        missing = []
        profile_label = str((profile or {}).get("label") or (profile or {}).get("id") or "官方 GGUF profile")
        if not unet_name:
            missing.append(f"GGUF UNet：{gguf_file}（需位於 ComfyUI models/unet 或 diffusion_models 可列出的資料夾）")
        for companion in (profile or {}).get("companions") or []:
            if not isinstance(companion, dict):
                continue
            filename = str(companion.get("filename") or "").strip()
            slot = str(companion.get("slot") or "").strip()
            role = str(companion.get("role") or slot or "model").strip()
            model_type = str(companion.get("model_type") or "").strip().lower()
            if not filename or not slot:
                continue
            options = vae_options if model_type == "vae" or slot == "vae_name" else clip_options
            selected = _select_exact_or_basename([filename], options)
            if selected:
                resolved[slot] = selected
            else:
                missing.append(f"{role}：{filename}")
        if missing:
            raise ComfyUIError(f"ComfyUI-GGUF 自動路由缺少 {profile_label} 模型：" + "；".join(missing))
        sampler_defaults = (profile or {}).get("sampler_defaults") if isinstance((profile or {}).get("sampler_defaults"), dict) else {}
        native_policy = (profile or {}).get("native_resolution_policy") if isinstance((profile or {}).get("native_resolution_policy"), dict) else {}
        return {
            "unet_name": unet_name,
            "workflow_family": workflow_family,
            "clip_loader_class": clip_loader_class,
            "clip_type": str((profile or {}).get("clip_type") or "").strip(),
            "clip_name1": resolved.get("clip_name1", ""),
            "clip_name2": resolved.get("clip_name2", ""),
            "clip_name3": resolved.get("clip_name3", ""),
            "vae_name": resolved.get("vae_name", ""),
            "sampler_name": _select_enum_option(
                None,
                (capabilities or {}).get("samplers") or [],
                [str(sampler_defaults.get("sampler_name") or ""), SAFE_SAMPLER_FALLBACK, "euler", "dpmpp_2m"],
            ),
            "scheduler": _select_enum_option(
                None,
                (capabilities or {}).get("schedulers") or [],
                [str(sampler_defaults.get("scheduler") or ""), SAFE_SCHEDULER_FALLBACK, "normal", "karras"],
            ),
            "cfg": sampler_defaults.get("cfg"),
            "steps": sampler_defaults.get("steps"),
            "sd3_shift": sampler_defaults.get("sd3_shift"),
            "sd3_negative_split": sampler_defaults.get("sd3_negative_split"),
            "native_max_megapixels": native_policy.get("max_megapixels"),
            "native_multiple_of": native_policy.get("multiple_of"),
            "output_upscale_method": native_policy.get("output_upscale_method"),
        }

    def _fit_native_generation_dimensions(width, height, *, max_megapixels=1.05, multiple_of=64):
        requested_width = max(64, int(width or 1024))
        requested_height = max(64, int(height or 1024))
        multiple = max(8, int(multiple_of or 64))
        max_pixels = max(multiple * multiple, int(float(max_megapixels or 1.05) * 1_000_000))
        scale = 1.0
        pixels = requested_width * requested_height
        if pixels > max_pixels:
            scale = (max_pixels / float(pixels)) ** 0.5
        native_width = max(multiple, int((requested_width * scale) // multiple) * multiple)
        native_height = max(multiple, int((requested_height * scale) // multiple) * multiple)
        while native_width * native_height > max_pixels and (native_width > multiple or native_height > multiple):
            if native_width >= native_height and native_width > multiple:
                native_width -= multiple
            elif native_height > multiple:
                native_height -= multiple
            else:
                break
        return native_width, native_height

    def _install_cached_gguf_to_local_comfyui(prepared, native_binding):
        gguf_path = Path(str((prepared or {}).get("path") or ""))
        gguf_file = str((prepared or {}).get("gguf_file") or gguf_path.name or "").strip()
        filename = Path(gguf_file.replace("\\", "/")).name
        if not filename.lower().endswith(".gguf"):
            raise ComfyUIError("GGUF 檔名不合法，無法匯入 ComfyUI models/unet")
        if not gguf_path.is_file():
            raise ComfyUIError(f"GGUF 快取檔不存在，無法匯入 ComfyUI：{gguf_path}")
        if not _is_local_comfyui_url(native_binding.get("url")):
            raise ComfyUIError(
                f"{gguf_file} 是 ComfyUI-GGUF 原生 UNet GGUF；遠端 ComfyUI API 無法由本站直接寫入模型檔。"
                f"請聯絡遠端 ComfyUI 管理人把檔案放到 models/unet/{filename}，"
                f"或切到本地 ComfyUI 並設定 COMFYUI_BASE_DIR，讓本站從 Hugging Face cache 自動接入。"
            )
        destination_dir, relative_dir, _label, msg = _resolve_model_destination_dir(
            model_type="unet",
            relative_dir="unet",
        )
        if msg or not destination_dir:
            raise ComfyUIError(
                f"{gguf_file} 是 ComfyUI-GGUF 原生 UNet GGUF；自動匯入本地 ComfyUI 失敗：{msg or '找不到 models/unet 目錄'}。"
                f"請設定 COMFYUI_BASE_DIR，或手動放到 ComfyUI/models/unet/{filename}。"
            )
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = (destination_dir / filename).resolve()
        try:
            destination.relative_to(destination_dir.resolve())
        except ValueError as exc:
            raise ComfyUIError("GGUF 匯入目的地超出 ComfyUI models/unet") from exc
        if not destination.exists() or destination.stat().st_size != gguf_path.stat().st_size:
            shutil.copy2(gguf_path, destination)
        return filename

    def _install_cached_hf_model_file_to_local_comfyui(prepared, native_binding, *, model_type, relative_dir, filename, role):
        model_path = Path(str((prepared or {}).get("path") or ""))
        raw_filename = str(filename or (prepared or {}).get("filename") or model_path.name or "").strip()
        safe_filename = Path(raw_filename.replace("\\", "/")).name
        role_label = str(role or safe_filename or "模型").strip()
        if not safe_filename or Path(safe_filename).suffix.lower() not in COMFYUI_MODEL_DOWNLOAD_EXTENSIONS:
            raise ComfyUIError(f"{role_label} 檔名不合法，無法匯入 ComfyUI models")
        if not model_path.is_file():
            raise ComfyUIError(f"{role_label} 快取檔不存在，無法匯入 ComfyUI：{model_path}")
        if not _is_local_comfyui_url(native_binding.get("url")):
            raise ComfyUIError(
                f"{role_label} 需要匯入 ComfyUI 模型目錄；遠端 ComfyUI API 無法由本站直接寫入模型檔。"
                f"請聯絡遠端 ComfyUI 管理人把檔案放到 models/{relative_dir or model_type}/{safe_filename}，"
                "或切到本地 ComfyUI 並設定 COMFYUI_BASE_DIR，讓本站從 Hugging Face cache 自動接入。"
            )
        destination_dir, safe_relative_dir, _label, msg = _resolve_model_destination_dir(
            model_type=model_type,
            relative_dir=relative_dir,
        )
        if msg or not destination_dir:
            raise ComfyUIError(
                f"{role_label} 自動匯入本地 ComfyUI 失敗：{msg or '找不到模型目錄'}。"
                f"請設定 COMFYUI_BASE_DIR，或手動放到 ComfyUI/models/{relative_dir or model_type}/{safe_filename}。"
            )
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = (destination_dir / safe_filename).resolve()
        try:
            destination.relative_to(destination_dir.resolve())
        except ValueError as exc:
            raise ComfyUIError("模型匯入目的地超出 ComfyUI models 目錄") from exc
        if not destination.exists() or destination.stat().st_size != model_path.stat().st_size:
            shutil.copy2(model_path, destination)
        return safe_filename, safe_relative_dir

    def _gguf_companion_capability_key(companion):
        model_type = str((companion or {}).get("model_type") or "").strip().lower()
        slot = str((companion or {}).get("slot") or "").strip()
        if model_type == "vae" or slot == "vae_name":
            return "vaes"
        return "clip_models"

    def _maybe_prepare_gguf_profile_companions(job_id, active_client, profile, native_binding, capabilities):
        companions = [item for item in (profile or {}).get("companions") or [] if isinstance(item, dict)]
        if not companions:
            return capabilities
        capabilities = dict(capabilities or {})
        prepare_file = getattr(active_client, "prepare_huggingface_file", None)
        if not callable(prepare_file):
            return capabilities
        updated = False
        for index, companion in enumerate(companions):
            filename = str(companion.get("filename") or "").strip()
            if not filename:
                continue
            capability_key = _gguf_companion_capability_key(companion)
            if _select_exact_or_basename([filename], list(capabilities.get(capability_key) or [])):
                continue
            role = str(companion.get("role") or companion.get("slot") or filename).strip()
            repo_id = str(companion.get("repo_id") or (profile or {}).get("repo_id") or "").strip()
            prepared = prepare_file(
                repo_id,
                filename,
                progress_callback=lambda progress: _update_generation_job_progress(job_id, progress),
                label=f"GGUF companion {role}",
                base_percent=min(88, 28 + index * 4),
                span_percent=4,
            )
            model_type = str(companion.get("model_type") or "").strip().lower() or ("vae" if capability_key == "vaes" else "text_encoder")
            installed_name, safe_relative_dir = _install_cached_hf_model_file_to_local_comfyui(
                prepared,
                native_binding,
                model_type=model_type,
                relative_dir=str(companion.get("install_subdir") or "").strip(),
                filename=filename,
                role=role,
            )
            existing = list(capabilities.get(capability_key) or [])
            extra = [installed_name]
            if safe_relative_dir:
                extra.append(f"{safe_relative_dir}/{installed_name}")
            capabilities[capability_key] = sorted(set(existing + extra))
            updated = True
            _update_generation_job_progress(job_id, {
                "phase": "routing",
                "percent": min(92, 32 + index * 4),
                "backend_kind": "comfyui_gguf",
                "step": "GGUF companion 已匯入",
                "current_file": installed_name,
                "detail": f"已匯入 {role}：{safe_relative_dir}/{installed_name}",
            })
        return capabilities if updated else capabilities

    def _maybe_prepare_diffusers_gguf_auto_route(job_id, actor, active_client, params, backend_binding):
        if getattr(active_client, "backend_kind", "") != "diffusers":
            return active_client, backend_binding, params
        gguf_file = str((params or {}).get("diffusers_gguf_file") or "").strip()
        if not gguf_file:
            return active_client, backend_binding, params
        if str((params or {}).get("generation_mode") or "txt2img").strip().lower() != "txt2img":
            raise ComfyUIError("GGUF 模式目前只支援文字生圖；影片、圖生圖或局部重繪請使用 ComfyUI workflow 模板。")
        profile, profile_variant = resolve_official_gguf_selection(
            params.get("diffusers_gguf_profile"),
            params.get("diffusers_gguf_variant"),
            repo_id=params.get("diffusers_model_repo") or params.get("model"),
            gguf_file=gguf_file,
            require_enabled=False,
        )
        if not profile or not profile_variant:
            raise ComfyUIError("GGUF 只允許官方已建檔 profile，請從官方 GGUF 下拉選單選擇模型與精度。")
        if not profile.get("enabled") or not profile_variant.get("enabled"):
            raise ComfyUIError(gguf_profile_unavailable_message(profile, profile_variant))
        params["diffusers_gguf_profile"] = str(profile.get("id") or "")
        params["diffusers_gguf_variant"] = str(profile_variant.get("id") or "")
        params["diffusers_model_repo"] = str(profile.get("repo_id") or params.get("diffusers_model_repo") or "").strip()
        params["model"] = params["diffusers_model_repo"] or params.get("model")
        params["diffusers_gguf_file"] = str(profile_variant.get("gguf_file") or gguf_file).strip()
        params["diffusers_gguf_base_repo"] = str(profile.get("base_repo") or params.get("diffusers_gguf_base_repo") or "").strip()
        gguf_file = params["diffusers_gguf_file"]

        native_binding = _native_comfyui_binding_for_gguf(actor)
        native_client = _client_for_url(native_binding["url"])
        try:
            capabilities = native_client.get_capabilities()
        except Exception as exc:
            raise ComfyUIError(f"ComfyUI-GGUF 自動路由無法連線到 ComfyUI backend：{exc}") from exc
        diffusion_options = list((capabilities or {}).get("diffusion_models") or [])
        gguf_option = resolve_model_option(gguf_file, diffusion_options)
        if native_binding.get("connection_mode") == "remote" and not gguf_option:
            filename = Path(gguf_file.replace("\\", "/")).name
            raise ComfyUIError(
                f"遠端 ComfyUI 未安裝 GGUF UNet：{gguf_file}。"
                "遠端 ComfyUI API 無法由本站直接寫入模型檔。"
                f"請聯絡遠端 ComfyUI 管理人把檔案放到 models/unet/{filename} 後再執行。"
            )
        if native_binding.get("connection_mode") != "remote" and gguf_option:
            prepared = {
                "suggested_backend": "comfyui_gguf",
                "model_repo": params.get("diffusers_model_repo") or params.get("model"),
                "gguf_file": gguf_file,
                "already_installed": True,
            }
            _update_generation_job_progress(job_id, {
                "phase": "routing",
                "percent": 24,
                "backend_kind": "comfyui_gguf",
                "step": "GGUF UNet 已安裝",
                "current_file": gguf_file,
                "detail": f"ComfyUI 已列出 GGUF UNet：{gguf_option}，跳過 Hugging Face UNet 下載。",
            })
        elif native_binding.get("connection_mode") != "remote":
            prepare = getattr(active_client, "prepare_gguf_file_for_backend", None)
            if not callable(prepare):
                return active_client, backend_binding, params
            prepared = prepare(
                params.get("diffusers_model_repo") or params.get("model"),
                gguf_file,
                progress_callback=lambda progress: _update_generation_job_progress(job_id, progress),
            )
            if str((prepared or {}).get("suggested_backend") or "") != "comfyui_gguf":
                base_repo = normalize_huggingface_repo_id(params.get("diffusers_gguf_base_repo"), allow_blank=True)
                if base_repo is None:
                    raise ComfyUIError("GGUF base Diffusers repo 格式不合法。")
                if not base_repo:
                    raise ComfyUIError("GGUF 需要設定 base Diffusers repo，例如 stabilityai/stable-diffusion-xl-base-1.0。")
                params["diffusers_gguf_base_repo"] = base_repo
                return active_client, backend_binding, params
        else:
            prepared = {
                "suggested_backend": "comfyui_gguf",
                "model_repo": params.get("diffusers_model_repo") or params.get("model"),
                "gguf_file": gguf_file,
            }
        if not gguf_option:
            installed_name = _install_cached_gguf_to_local_comfyui(prepared, native_binding)
            try:
                capabilities = native_client.get_capabilities()
            except Exception:
                capabilities = dict(capabilities or {})
            capabilities = dict(capabilities or {})
            refreshed_options = list((capabilities or {}).get("diffusion_models") or [])
            if not resolve_model_option(installed_name, refreshed_options):
                capabilities["diffusion_models"] = sorted(set(refreshed_options + diffusion_options + [installed_name]))
            gguf_option = resolve_model_option(installed_name, (capabilities or {}).get("diffusion_models") or []) or installed_name
        if native_binding.get("connection_mode") != "remote":
            capabilities = _maybe_prepare_gguf_profile_companions(
                job_id,
                active_client,
                profile,
                native_binding,
                capabilities,
            )
        runtime_models = _select_gguf_profile_runtime_models(native_client, capabilities, profile, profile_variant, gguf_option)
        requested_width = int(params.get("width") or 1024)
        requested_height = int(params.get("height") or 1024)
        native_width = requested_width
        native_height = requested_height
        if runtime_models.get("workflow_family") == "sd3_triple_clip_gguf":
            native_width, native_height = _fit_native_generation_dimensions(
                requested_width,
                requested_height,
                max_megapixels=float(runtime_models.get("native_max_megapixels") or 1.05),
                multiple_of=int(runtime_models.get("native_multiple_of") or 64),
            )
        routed_params = dict(params)
        routed_params.update({
            "backend_kind": "comfyui_gguf",
            "generation_mode": "txt2img",
            "model": runtime_models["unet_name"],
            "diffusion_model": runtime_models["unet_name"],
            "comfyui_gguf_unet_name": runtime_models["unet_name"],
            "workflow_family": runtime_models.get("workflow_family") or "",
            "clip_loader_class": runtime_models["clip_loader_class"],
            "clip_type": runtime_models["clip_type"],
            "clip": runtime_models["clip_name1"],
            "clip2": runtime_models["clip_name2"],
            "clip3": runtime_models.get("clip_name3") or "",
            "vae": runtime_models["vae_name"],
            "sampler_name": _select_enum_option(params.get("sampler_name"), (capabilities or {}).get("samplers") or [], [runtime_models["sampler_name"]]),
            "scheduler": _select_enum_option(params.get("scheduler"), (capabilities or {}).get("schedulers") or [], [runtime_models["scheduler"]]),
            "steps": _int_range(params.get("steps"), int(runtime_models.get("steps") or 24), 1, 80),
            "cfg": _float_range(params.get("cfg"), float(runtime_models.get("cfg") or 5.0), 1.0, 30.0),
            "sd3_shift": _float_range(params.get("sd3_shift"), float(runtime_models.get("sd3_shift") or 3.0), 0.0, 20.0),
            "sd3_negative_split": _float_range(params.get("sd3_negative_split"), float(runtime_models.get("sd3_negative_split") or 0.1), 0.0, 1.0),
            "sd3_native_width": native_width,
            "sd3_native_height": native_height,
            "output_width": requested_width,
            "output_height": requested_height,
            "output_upscale_method": str(runtime_models.get("output_upscale_method") or "lanczos").strip() or "lanczos",
            "filename_prefix": params.get("filename_prefix") or "hackme_web_gguf",
        })
        _update_generation_job_progress(job_id, {
            "phase": "routing",
            "percent": 28,
            "backend_kind": "comfyui_gguf",
            "step": "切換到 ComfyUI-GGUF workflow",
            "current_file": gguf_file,
            "detail": f"已偵測為 ComfyUI-GGUF 原生 UNet，將使用 {native_binding.get('url')} 執行 UnetLoaderGGUF workflow。",
            "comfyui_url": native_binding.get("url"),
        })
        return native_client, native_binding, routed_params

    def _run_comfyui_generation_job(job_id, actor, params, quote, timeout_seconds, request_meta=None, backend_binding=None):
        backend_binding = backend_binding if isinstance(backend_binding, dict) else _comfyui_binding(actor)
        active_client = _client_for_url(backend_binding["url"])
        generation_token = None
        request_meta = request_meta if isinstance(request_meta, dict) else {}
        audit_ip = request_meta.get("client_ip") or "-"
        audit_ua = request_meta.get("user_agent") or "-"
        _update_generation_job(job_id, status="running")
        _update_generation_job_progress(job_id, _initial_generation_progress(active_client, params, timeout_seconds))
        try:
            active_client, backend_binding, params = _maybe_prepare_diffusers_gguf_auto_route(
                job_id,
                actor,
                active_client,
                params,
                backend_binding,
            )
            generation_token = _register_active_generation(
                actor,
                backend_url=backend_binding.get("url"),
                backend_scope=backend_binding.get("backend_scope"),
            )
            result = active_client.generate_image(
                params,
                **_maybe_fetch_outputs_kwarg(
                    active_client.generate_image,
                    timeout_seconds=timeout_seconds,
                    progress_callback=lambda progress: _update_generation_job_progress(job_id, progress),
                    fetch_outputs=True,
                ),
            )
            _assert_comfyui_final_images_usable(result)
            billing = {"charged": False, "exempt": "root"} if not quote else None
            if quote:
                billing = _charge_comfyui_generation(actor, quote, prompt_id=result.get("prompt_id"))
            images = _finalize_generation_records(actor, params, result, backend_url=backend_binding.get("url"))
            history_id = None
            conn = get_db()
            try:
                history_id = _record_generation_history(
                    conn,
                    actor=actor,
                    params=params,
                    backend_url=backend_binding.get("url"),
                    result_payload={
                        "prompt_id": result.get("prompt_id") or "",
                        "images": [
                            {
                                "image_ref": item.get("image_ref"),
                                "mime_type": item.get("mime_type"),
                                "size_bytes": item.get("size_bytes"),
                            }
                            for item in images
                        ],
                    },
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                conn.close()
            payload = {
                "image": images[0],
                "images": images,
                "billing": billing,
                "history_id": history_id,
                "wallet": (billing or {}).get("wallet") or _comfyui_wallet_payload(actor),
            }
            audit(
                "COMFYUI_GENERATE",
                audit_ip,
                user=_actor_value(actor, "username"),
                success=True,
                ua=audit_ua,
                detail=f"job_id={job_id}, prompt_id={result['prompt_id']}, file={result['image_ref'].get('filename')}, batch={len(images)}",
            )
            _update_generation_job_progress(job_id, {
                "phase": "completed",
                "percent": 100,
                "completed": True,
                "detail": f"已完成，共 {len(images)} 張",
            })
            _update_generation_job(
                job_id,
                status="completed",
                result=payload,
                error="",
            )
        except ComfyUIError as exc:
            error_progress = {
                "phase": "error",
                "percent": 100,
                "detail": str(exc),
                "error_message": str(exc),
                "completed": False,
            }
            if getattr(active_client, "backend_kind", "") == "diffusers":
                error_progress = _diffusers_error_progress(job_id, exc)
            _update_generation_job_progress(job_id, error_progress)
            _update_generation_job(job_id, status="error", error=str(exc), result=None)
            audit("COMFYUI_GENERATE_ERROR", audit_ip, user=_actor_value(actor, "username"), success=False, ua=audit_ua, detail=str(exc)[:180])
        except Exception as exc:
            error_progress = {
                "phase": "error",
                "percent": 100,
                "detail": str(exc),
                "error_message": str(exc),
                "completed": False,
            }
            if getattr(active_client, "backend_kind", "") == "diffusers":
                error_progress = _diffusers_error_progress(job_id, exc)
            _update_generation_job_progress(job_id, error_progress)
            _update_generation_job(job_id, status="error", error=str(exc), result=None)
            audit("COMFYUI_GENERATE_ERROR", audit_ip, user=_actor_value(actor, "username"), success=False, ua=audit_ua, detail=str(exc)[:180])
        finally:
            if generation_token:
                _unregister_active_generation(generation_token)

    def _comfyui_account_api_key():
        return str((get_system_settings() or {}).get("comfyui_account_api_key") or os.environ.get("COMFYUI_ACCOUNT_API_KEY") or "").strip()

    def _comfyui_paid_api_status_payload():
        settings = get_system_settings() or {}
        return {
            "enabled": bool(settings.get("comfyui_paid_api_nodes_enabled")),
            "key_configured": bool(_comfyui_account_api_key()),
            "credit_balance_available": False,
            "credit_balance": None,
            "credits_msg": "ComfyUI credits 目前沒有穩定官方 REST endpoint；請在 ComfyUI UI 的 Settings / Credits 查看餘額。",
        }

    def _comfyui_paid_api_policy(workflow_json, *, confirm=False, object_info=None):
        paid_api = detect_paid_api_nodes(workflow_json, object_info=object_info)
        if not paid_api.get("required"):
            return {}, None
        settings = get_system_settings() or {}
        if not bool(settings.get("comfyui_paid_api_nodes_enabled")):
            return None, (
                json_resp({
                    "ok": False,
                    "msg": "這個 workflow 含有可能需要付費的 ComfyUI API node，但伺服器尚未允許付費/API nodes。",
                    "stage": "paid_api_nodes_disabled",
                    "paid_api_nodes": paid_api,
                }),
                409,
            )
        api_key = _comfyui_account_api_key()
        if not api_key:
            return None, (
                json_resp({
                    "ok": False,
                    "msg": "這個 workflow 需要 ComfyUI Account API Key；請先由 root 在伺服器設定中保存 key。",
                    "stage": "paid_api_key_missing",
                    "paid_api_nodes": paid_api,
                }),
                409,
            )
        if not confirm:
            return None, (
                json_resp({
                    "ok": False,
                    "msg": "這個 workflow 可能消耗 ComfyUI API credits，請先確認後再執行。",
                    "stage": "paid_api_confirmation_required",
                    "paid_api_nodes": paid_api,
                    "confirmation_required": True,
                }),
                409,
            )
        return build_comfyui_account_extra_data(api_key), None

    def _run_comfyui_workflow_preset_job(job_id, actor, row, run_id, timeout_seconds, request_meta=None, prompt_extra_data=None, workflow_override=None):
        backend_binding = _comfyui_binding(actor)
        active_client = _client_for_url(backend_binding["url"])
        request_meta = request_meta if isinstance(request_meta, dict) else {}
        audit_ip = request_meta.get("client_ip") or "-"
        audit_ua = request_meta.get("user_agent") or "-"
        _update_generation_job(job_id, status="running")
        timeout_value = int(timeout_seconds or 0)
        _update_generation_job_progress(job_id, {
            "phase": "queued",
            "percent": 0,
            "detail": "已送出 workflow 至 ComfyUI 背景工作",
            "timeout_seconds": timeout_value,
            "timeout_unlimited": timeout_value <= 0,
        })
        workflow_json = workflow_override if isinstance(workflow_override, dict) else (_parse_json_field(row["workflow_json"], {}) or {})
        default_params = _parse_json_field(row["default_params_json"], {}) or {}
        try:
            conn = get_db()
            try:
                snapshot = _load_workflow_run_snapshot(conn, run_id=run_id)
            finally:
                conn.close()
        except Exception:
            snapshot = None
        if isinstance(snapshot, dict):
            snapshot_params = snapshot.get("params") if isinstance(snapshot.get("params"), dict) else {}
            snapshot_workflow = snapshot.get("workflow_json") if isinstance(snapshot.get("workflow_json"), dict) else {}
            if snapshot_params:
                merged_params = dict(default_params)
                merged_params.update(snapshot_params)
                default_params = merged_params
            if snapshot_workflow:
                workflow_json = snapshot_workflow
        prompt = str(default_params.get("prompt") or "")
        negative_prompt = str(default_params.get("negative_prompt") or "")
        expected_count = _workflow_expected_image_count(workflow_json, default_params)
        partial_state = {"signature": ""}

        def _partial_output_signature(refs):
            parts = []
            for item in refs:
                if not isinstance(item, dict):
                    continue
                parts.append("|".join([
                    str(item.get("output_node_id") or ""),
                    str(item.get("filename") or ""),
                    str(item.get("subfolder") or ""),
                    str(item.get("type") or ""),
                ]))
            return "\n".join(parts)

        def _publish_partial_workflow_outputs(progress):
            partial_outputs = progress.get("partial_outputs") if isinstance(progress, dict) else {}
            if not isinstance(partial_outputs, dict):
                return
            image_refs = partial_outputs.get("images") if isinstance(partial_outputs.get("images"), list) else []
            if not image_refs:
                return
            signature = _partial_output_signature(image_refs)
            if not signature or signature == partial_state.get("signature"):
                return
            partial_state["signature"] = signature
            prompt_id = str(partial_outputs.get("prompt_id") or progress.get("prompt_id") or "")
            image_items = [
                {
                    "image_ref": ref,
                    "mime_type": "image/png",
                    "data": b"",
                    "size_bytes": 0,
                    "output_node_id": ref.get("output_node_id") or "",
                    "output_label": ref.get("output_label") or "",
                }
                for ref in image_refs
                if isinstance(ref, dict)
            ]
            if not image_items:
                return
            result_stub = {
                "prompt_id": prompt_id,
                "image_ref": image_items[0]["image_ref"],
                "mime_type": "image/png",
                "data": b"",
                "images": image_items,
                "media": {},
            }
            try:
                images = _finalize_generation_records(
                    actor,
                    default_params,
                    result_stub,
                    backend_url=backend_binding.get("url"),
                    notify=False,
                )
            except Exception:
                return
            payload = {
                "image": images[0] if images else None,
                "images": images,
                "media": [],
                "workflow_run_id": run_id,
                "preset_id": int(row["id"]),
                "partial": True,
                "expected_image_count": expected_count,
            }
            _update_generation_job(job_id, result=payload)

        def _workflow_progress_callback(progress):
            _update_generation_job_progress(job_id, progress)
            _publish_partial_workflow_outputs(progress or {})

        try:
            run_kwargs = {
                "timeout_seconds": timeout_seconds,
                "expected_count": expected_count,
                "progress_callback": _workflow_progress_callback,
                "wait_until_completed": not _is_qwen_edit_workflow_family_id(default_params.get("workflow_system_bundle_id")),
            }
            if prompt_extra_data:
                run_kwargs["extra_data"] = prompt_extra_data
            run_kwargs = _maybe_fetch_outputs_kwarg(active_client.generate_from_workflow, **run_kwargs, fetch_outputs=True)
            result = active_client.generate_from_workflow(workflow_json, **run_kwargs)
            result = _resize_qwen_workflow_result_images_if_needed(
                active_client,
                result,
                default_params,
                actor=actor,
                job_id=job_id,
                audit_ip=audit_ip,
                audit_ua=audit_ua,
            )
            _assert_comfyui_final_images_usable(result)
            images = (
                _finalize_generation_records(actor, default_params, result, backend_url=backend_binding.get("url"))
                if isinstance(result.get("images"), list) and result.get("images")
                else []
            )
            media = _serialize_comfyui_media_records(result)
            output_refs = {
                "prompt_id": result.get("prompt_id") or "",
                "images": [
                    {
                        "image_ref": item.get("image_ref"),
                        "mime_type": item.get("mime_type"),
                        "size_bytes": item.get("size_bytes"),
                        "output_node_id": item.get("output_node_id") or "",
                        "output_label": item.get("output_label") or "",
                    }
                    for item in images
                ],
                "media": [
                    {
                        "media_kind": item.get("media_kind"),
                        "file_ref": item.get("file_ref"),
                        "mime_type": item.get("mime_type"),
                        "size_bytes": item.get("size_bytes"),
                    }
                    for item in media
                ],
            }
            conn = get_db()
            try:
                _update_workflow_run(conn, run_id=run_id, status="completed", output_refs=output_refs, error="")
                conn.commit()
            finally:
                conn.close()
            payload = {
                "image": images[0] if images else None,
                "images": images,
                "media": media,
                "backend_url": backend_binding.get("url"),
                "workflow_run_id": run_id,
                "preset_id": int(row["id"]),
            }
            _update_generation_job(job_id, status="completed", result=payload, error="")
            _update_generation_job_progress(job_id, {
                "phase": "completed",
                "percent": 100,
                "completed": True,
                "detail": f"已完成，共 {len(images)} 張圖片、{len(media)} 個媒體輸出",
            })
            audit(
                "COMFYUI_WORKFLOW_RUN",
                audit_ip,
                user=_actor_value(actor, "username"),
                success=True,
                ua=audit_ua,
                detail=f"job_id={job_id}, preset_id={row['id']}, run_id={run_id}, prompt_id={result.get('prompt_id') or ''}",
            )
        except ComfyUIError as exc:
            conn = get_db()
            try:
                _update_workflow_run(conn, run_id=run_id, status="error", output_refs={}, error=str(exc))
                conn.commit()
            finally:
                conn.close()
            _update_generation_job(job_id, status="error", error=str(exc), result=None)
            _update_generation_job_progress(job_id, {"phase": "error", "detail": str(exc), "completed": False})
            audit("COMFYUI_WORKFLOW_RUN_ERROR", audit_ip, user=_actor_value(actor, "username"), success=False, ua=audit_ua, detail=str(exc)[:180])
        except Exception as exc:
            conn = get_db()
            try:
                _update_workflow_run(conn, run_id=run_id, status="error", output_refs={}, error=str(exc))
                conn.commit()
            finally:
                conn.close()
            _update_generation_job(job_id, status="error", error=str(exc), result=None)
            _update_generation_job_progress(job_id, {"phase": "error", "detail": str(exc), "completed": False})
            audit("COMFYUI_WORKFLOW_RUN_ERROR", audit_ip, user=_actor_value(actor, "username"), success=False, ua=audit_ua, detail=str(exc)[:180])

    def _run_comfyui_model_download_job(job_id, actor, request_data, request_meta=None):
        request_data = dict(request_data or {})
        request_meta = request_meta if isinstance(request_meta, dict) else {}
        audit_ip = request_meta.get("client_ip") or "-"
        audit_ua = request_meta.get("user_agent") or "-"
        _update_model_download_job(job_id, status="running")
        parsed_request, msg = _parse_civitai_download_request(request_data)
        if msg:
            _update_model_download_job(job_id, status="error", error=msg, result=None)
            _update_model_download_progress(job_id, {
                "phase": "error",
                "detail": msg,
                "completed": False,
            })
            return
        try:
            result, msg = _download_civitai_model_selection(
                page_url=parsed_request["page_url"],
                version_id=parsed_request["version_id"],
                file_id=parsed_request["file_id"],
                model_type=parsed_request["model_type"],
                base_dir=parsed_request["base_dir"],
                relative_dir=parsed_request["relative_dir"],
                progress_callback=lambda progress: _update_model_download_progress(job_id, progress),
            )
            if msg:
                raise ValueError(msg)
            _update_model_download_job(job_id, status="completed", error="", result=result)
            _update_model_download_progress(job_id, {
                "phase": "completed",
                "percent": 100,
                "bytes_written": int((result or {}).get("size_bytes") or 0),
                "total_bytes": int((result or {}).get("size_bytes") or 0),
                "detail": f"已下載 {(result or {}).get('filename') or ''}",
                "completed": True,
            })
            audit(
                "COMFYUI_CIVITAI_DOWNLOAD",
                audit_ip,
                user=_actor_value(actor, "username"),
                success=True,
                ua=audit_ua,
                detail=f"async_job={job_id}, type={(result or {}).get('type') or ''}, filename={(result or {}).get('filename') or ''}",
            )
        except Exception as exc:
            _update_model_download_job(job_id, status="error", error=str(exc), result=None)
            _update_model_download_progress(job_id, {
                "phase": "error",
                "detail": str(exc),
                "completed": False,
            })
            audit("COMFYUI_CIVITAI_DOWNLOAD", audit_ip, user=_actor_value(actor, "username"), success=False, ua=audit_ua, detail=f"async_job={job_id}, error={str(exc)[:220]}")

    def _comfyui_unavailable_payload(exc, active_client=None):
        active_client = active_client or _client()
        return {
            "ok": True,
            "available": False,
            "msg": str(exc),
            "connection_mode": _configured_connection_mode(),
            "comfyui_url": getattr(active_client, "base_url", _configured_comfyui_url()),
        }

    def _int_range(value, default, minimum, maximum, *, multiple_of=None):
        try:
            number = int(value)
        except Exception:
            number = default
        number = max(minimum, min(maximum, number))
        if multiple_of:
            number = max(minimum, (number // multiple_of) * multiple_of)
        return number

    def _normalize_generation_timeout(value):
        if value in (None, ""):
            return DEFAULT_GENERATION_TIMEOUT_SECONDS
        try:
            number = int(value)
        except Exception:
            number = DEFAULT_GENERATION_TIMEOUT_SECONDS
        if number <= 0:
            return 0
        return _int_range(number, DEFAULT_GENERATION_TIMEOUT_SECONDS, 30, MAX_GENERATION_TIMEOUT_SECONDS)

    def _float_range(value, default, minimum, maximum):
        try:
            number = float(value)
        except Exception:
            number = default
        return max(minimum, min(maximum, number))

    def _normalize_comfyui_model_option_name(value, *, limit=180):
        text = str(value or "").strip()
        if not text:
            return ""
        normalized_path = text.replace("\\", "/")
        if "\x00" in text or normalized_path.startswith("/") or re.match(r"^[A-Za-z]:", normalized_path):
            return None
        parts = normalized_path.split("/")
        if any(not part.strip() or part.strip() in {".", ".."} for part in parts):
            return None
        return text[:limit]

    def _normalize_loras(data):
        raw_loras = data.get("loras") if isinstance(data, dict) else []
        if raw_loras in (None, ""):
            return [], None
        if not isinstance(raw_loras, list):
            return None, "LoRA 參數格式不正確"
        normalized = []
        seen = set()
        for item in raw_loras[:MAX_COMFYUI_LORAS_PER_PROMPT]:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                return None, "LoRA 參數格式不正確"
            name = str(item.get("name") or item.get("lora_name") or "").strip()
            if not name:
                continue
            name = _normalize_comfyui_model_option_name(name)
            if name is None:
                return None, "LoRA 名稱不合法"
            dedupe_key = name.replace("\\", "/").lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            normalized.append({
                "name": name[:180],
                "strength_model": _float_range(item.get("strength_model"), 1.0, -2.0, 2.0),
                "strength_clip": _float_range(item.get("strength_clip"), 1.0, -2.0, 2.0),
            })
        return normalized, None

    def _normalize_comfyui_prompt_text(value):
        text = str(value or "").strip()
        if not text:
            return ""
        text = COMFYUI_EMBEDDING_TOKEN_RE.sub(
            lambda match: f"embedding:{match.group(1).strip()}",
            text,
        )
        return text

    def _normalize_comfyui_vae_name(value):
        text = str(value or "").strip()
        if not text or text == COMFYUI_VAE_BUILTIN:
            return ""
        return _normalize_comfyui_model_option_name(text)

    def _clean_filename(name, fallback="comfyui.png"):
        text = str(name or "").strip()
        text = text.split("/")[-1].split("\\")[-1]
        text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
        if not text:
            text = fallback
        if "." not in text:
            text += ".png"
        return text[:120]

    def _parse_json_field(value, fallback):
        if value in (None, ""):
            return fallback
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except Exception:
            return fallback

    def _number_from_text(value, *, label, numeric_type=float):
        if value in (None, ""):
            return None, None
        try:
            return numeric_type(value), None
        except Exception:
            return None, f"{label} 格式不正確"

    def _normalized_generation_mode(value):
        mode = str(value or "txt2img").strip().lower()
        mode = {
            "t2a": "t2s",
            "text2audio": "t2s",
            "text-to-audio": "t2s",
            "text2speech": "t2s",
            "text-to-speech": "t2s",
        }.get(mode, mode)
        return mode if mode in GENERATION_MODE_DEFINITIONS else None

    def _validate_image_upload(file_storage, *, label):
        if not file_storage:
            return None, None
        filename = str(getattr(file_storage, "filename", "") or "").strip()
        mime_type = str(getattr(file_storage, "mimetype", "") or "").strip().lower()
        ext = Path(filename).suffix.lower()
        if ext not in COMFYUI_ALLOWED_IMAGE_EXTENSIONS or mime_type not in COMFYUI_ALLOWED_IMAGE_MIME_TYPES:
            return None, f"{label} 只支援 PNG / JPG / WEBP"
        data = file_storage.read()
        try:
            file_storage.stream.seek(0)
        except Exception:
            pass
        if not data:
            return None, f"{label} 內容不可為空"
        if len(data) > MAX_COMFYUI_FETCH_IMAGE_BYTES:
            return None, f"{label} 超過大小上限"
        return {
            "filename": _clean_filename(filename, fallback="image.png"),
            "mime_type": mime_type,
            "data": data,
        }, None

    def _validate_video_upload(file_storage, *, label):
        if not file_storage:
            return None, None
        filename = str(getattr(file_storage, "filename", "") or "").strip()
        mime_type = str(getattr(file_storage, "mimetype", "") or "").strip().lower()
        ext = Path(filename).suffix.lower()
        if ext not in COMFYUI_ALLOWED_VIDEO_EXTENSIONS:
            return None, f"{label} 只支援 MP4 / WEBM / MOV / MKV / AVI"
        if mime_type and mime_type not in COMFYUI_ALLOWED_VIDEO_MIME_TYPES:
            return None, f"{label} MIME 不支援：{mime_type}"
        data = file_storage.read()
        try:
            file_storage.stream.seek(0)
        except Exception:
            pass
        if not data:
            return None, f"{label} 內容不可為空"
        if len(data) > MAX_COMFYUI_FETCH_VIDEO_BYTES:
            return None, f"{label} 超過大小上限"
        guessed_mime = mime_type or mimetypes.guess_type(filename)[0] or "video/mp4"
        return {
            "filename": _clean_filename(filename, fallback="video.mp4"),
            "mime_type": guessed_mime,
            "data": data,
        }, None

    def _normalize_image_ref_field(value):
        payload = _parse_json_field(value, None)
        if not isinstance(payload, dict):
            return None
        return _image_ref_payload(payload)

    def _normalize_controlnet_payload(data):
        enabled = _coerce_bool(data.get("controlnet_enabled"))
        if not enabled and isinstance(data.get("controlnet"), dict):
            enabled = True
        if not enabled:
            return None, None
        control_type = str(
            data.get("controlnet_type")
            or ((data.get("controlnet") or {}).get("type") if isinstance(data.get("controlnet"), dict) else "")
            or ""
        ).strip().lower()
        if control_type not in CONTROLNET_TYPE_DEFINITIONS:
            return None, "請選擇有效的 ControlNet 類型"
        strength, err = _number_from_text(
            data.get("control_strength")
            if "control_strength" in data
            else ((data.get("controlnet") or {}).get("strength") if isinstance(data.get("controlnet"), dict) else None),
            label="Control strength",
            numeric_type=float,
        )
        if err:
            return None, err
        start_percent, err = _number_from_text(
            data.get("control_start")
            if "control_start" in data
            else ((data.get("controlnet") or {}).get("start_percent") if isinstance(data.get("controlnet"), dict) else None),
            label="Control start",
            numeric_type=float,
        )
        if err:
            return None, err
        end_percent, err = _number_from_text(
            data.get("control_end")
            if "control_end" in data
            else ((data.get("controlnet") or {}).get("end_percent") if isinstance(data.get("controlnet"), dict) else None),
            label="Control end",
            numeric_type=float,
        )
        if err:
            return None, err
        strength = 1.0 if strength is None else strength
        start_percent = 0.0 if start_percent is None else start_percent
        end_percent = 1.0 if end_percent is None else end_percent
        if strength < 0 or strength > 2:
            return None, "Control strength 必須介於 0 到 2"
        if start_percent < 0 or start_percent > 1 or end_percent < 0 or end_percent > 1 or start_percent > end_percent:
            return None, "Control start / end 必須介於 0 到 1，且 start 不可大於 end"
        preprocessor = str(
            data.get("controlnet_preprocessor")
            or ((data.get("controlnet") or {}).get("preprocessor") if isinstance(data.get("controlnet"), dict) else "")
            or ""
        ).strip()
        model_name = str(
            data.get("controlnet_model")
            or ((data.get("controlnet") or {}).get("model_name") if isinstance(data.get("controlnet"), dict) else "")
            or ""
        ).strip()
        return {
            "enabled": True,
            "type": control_type,
            "strength": round(float(strength), 4),
            "start_percent": round(float(start_percent), 4),
            "end_percent": round(float(end_percent), 4),
            "preprocessor": preprocessor,
            "model_name": model_name,
        }, None

    def _normalize_outpaint_payload(data):
        return {
            "left": _int_range(data.get("outpaint_left"), 0, 0, 2048),
            "top": _int_range(data.get("outpaint_top"), 0, 0, 2048),
            "right": _int_range(data.get("outpaint_right"), 0, 0, 2048),
            "bottom": _int_range(data.get("outpaint_bottom"), 0, 0, 2048),
            "feathering": _int_range(data.get("outpaint_feathering"), 24, 0, 256),
        }

    def _normalize_seed_after_generate(value):
        mode = str(value or "fixed").strip().lower()
        return mode if mode in {"random", "fixed", "increment", "decrement"} else "fixed"

    def _normalize_generation_payload(data):
        mode = _normalized_generation_mode(data.get("generation_mode"))
        if not mode:
            return None, "ComfyUI 產圖模式不支援"
        mode_definition = GENERATION_MODE_DEFINITIONS.get(mode) or {}
        workflow_only = bool(mode_definition.get("workflow_only"))
        prompt = _normalize_comfyui_prompt_text(data.get("prompt"))
        if mode != "upscale" and mode != "v2v" and not prompt:
            return None, "請輸入提示詞"
        if len(prompt) > 3000:
            return None, "提示詞最多 3000 字"
        negative = _normalize_comfyui_prompt_text(data.get("negative_prompt"))
        if len(negative) > 3000:
            return None, "負面提示詞最多 3000 字"
        diffusers_model_repo = normalize_huggingface_repo_id(
            data.get("diffusers_model_repo")
            or data.get("huggingface_model_repo")
            or data.get("hf_model_repo"),
            allow_blank=True,
        )
        if diffusers_model_repo is None:
            return None, "Hugging Face repo 格式不合法，請填 namespace/model 或 huggingface.co 的模型頁網址；不要填 .safetensors / .gguf 權重檔名"
        diffusers_gguf_profile = str(data.get("diffusers_gguf_profile") or "").strip()
        diffusers_gguf_variant = str(data.get("diffusers_gguf_variant") or "").strip()
        model = str(data.get("model") or diffusers_model_repo or "").strip()
        raw_diffusers_variant = str(data.get("diffusers_model_variant") or "").strip()
        raw_gguf_file = str(data.get("diffusers_gguf_file") or "").strip()
        if raw_diffusers_variant.startswith("gguf_profile::"):
            parts = raw_diffusers_variant.split("::", 2)
            diffusers_gguf_profile = parts[1].strip() if len(parts) > 1 else ""
            diffusers_gguf_variant = parts[2].strip() if len(parts) > 2 else ""
            raw_diffusers_variant = ""
        if raw_diffusers_variant.startswith("gguf::"):
            raw_gguf_file = raw_diffusers_variant.split("::", 1)[1]
            diffusers_model_variant = ""
        else:
            diffusers_model_variant = normalize_diffusers_variant(raw_diffusers_variant, allow_blank=True)
            if diffusers_model_variant is None:
                return None, "Diffusers 模型精度版本名稱不合法"
        diffusers_gguf_file = normalize_huggingface_repo_file(raw_gguf_file, allow_blank=True)
        if diffusers_gguf_file is None or (diffusers_gguf_file and not diffusers_gguf_file.lower().endswith(".gguf")):
            return None, "GGUF 檔案路徑不合法"
        diffusers_gguf_base_repo = normalize_huggingface_repo_id(data.get("diffusers_gguf_base_repo"), allow_blank=True)
        if diffusers_gguf_base_repo is None:
            return None, "GGUF base Diffusers repo 格式不合法"
        if diffusers_gguf_profile or diffusers_gguf_variant:
            profile, profile_variant = resolve_official_gguf_selection(
                diffusers_gguf_profile,
                diffusers_gguf_variant,
                repo_id=diffusers_model_repo or model,
                gguf_file=diffusers_gguf_file,
                require_enabled=False,
            )
            if not profile or not profile_variant:
                return None, "GGUF 只允許官方已建檔 profile，請從官方 GGUF 下拉選單選擇模型與精度"
            if not profile.get("enabled") or not profile_variant.get("enabled"):
                return None, gguf_profile_unavailable_message(profile, profile_variant).rstrip("。")
            diffusers_gguf_profile = str(profile.get("id") or "")
            diffusers_gguf_variant = str(profile_variant.get("id") or "")
            diffusers_model_repo = str(profile.get("repo_id") or "").strip()
            model = diffusers_model_repo
            diffusers_gguf_file = str(profile_variant.get("gguf_file") or "").strip()
            diffusers_gguf_base_repo = str(profile.get("base_repo") or diffusers_gguf_base_repo or "").strip()
            diffusers_model_variant = ""
        if mode != "upscale" and not workflow_only and not model:
            return None, "請選擇模型"
        vae = _normalize_comfyui_vae_name(data.get("vae"))
        if vae is None:
            return None, "VAE 名稱不合法"
        loras_source = dict(data)
        if loras_source.get("loras") in (None, "") and loras_source.get("loras_json") not in (None, ""):
            loras_source["loras"] = _parse_json_field(loras_source.get("loras_json"), [])
        loras, lora_msg = _normalize_loras(loras_source)
        if lora_msg:
            return None, lora_msg
        seed = _int_range(data.get("seed"), secrets.randbits(32), 0, 2**63 - 1)
        default_dimensions = _configured_default_dimensions()
        controlnet, controlnet_msg = _normalize_controlnet_payload(data)
        if controlnet_msg:
            return None, controlnet_msg
        denoise_strength, denoise_err = _number_from_text(data.get("denoise_strength"), label="Denoise strength", numeric_type=float)
        if denoise_err:
            return None, denoise_err
        if denoise_strength is not None and (denoise_strength < 0 or denoise_strength > 1):
            return None, "Denoise strength 必須介於 0 到 1"
        params = {
            "generation_mode": mode,
            "model": model,
            "diffusers_model_repo": diffusers_model_repo,
            "diffusers_model_variant": diffusers_model_variant,
            "diffusers_model_variant_selected": bool(raw_diffusers_variant),
            "diffusers_gguf_file": diffusers_gguf_file,
            "diffusers_gguf_base_repo": diffusers_gguf_base_repo,
            "diffusers_gguf_profile": diffusers_gguf_profile,
            "diffusers_gguf_variant": diffusers_gguf_variant,
            "prompt": prompt,
            "negative_prompt": negative,
            "width": _int_range(data.get("width"), default_dimensions["width"], 64, 2048, multiple_of=8),
            "height": _int_range(data.get("height"), default_dimensions["height"], 64, 2048, multiple_of=8),
            "steps": _int_range(data.get("steps"), 20, 1, 80),
            "cfg": _float_range(data.get("cfg"), 7.0, 1.0, 30.0),
            "sampler_name": str(data.get("sampler_name") or SAFE_SAMPLER_FALLBACK).strip() or SAFE_SAMPLER_FALLBACK,
            "scheduler": str(data.get("scheduler") or SAFE_SCHEDULER_FALLBACK).strip() or SAFE_SCHEDULER_FALLBACK,
            "seed": seed,
            "batch_size": _int_range(data.get("batch_size"), 1, 1, _configured_max_batch_size()),
            "ui_batch_size": _int_range(
                data.get("ui_batch_size") or data.get("requested_batch_size") or data.get("history_batch_size") or data.get("batch_size"),
                1,
                1,
                _configured_max_batch_size(),
            ),
            "run_count": _int_range(data.get("run_count") or data.get("history_run_count"), 1, 1, 10),
            "seed_after_generate": _normalize_seed_after_generate(data.get("seed_after_generate") or data.get("seed_after_generate_mode")),
            "filename_prefix": _clean_filename(data.get("filename_prefix") or "hackme_web", fallback="hackme_web").rsplit(".", 1)[0],
            "loras": loras,
            "vae": vae,
            "denoise_strength": 0.65 if denoise_strength is None else round(float(denoise_strength), 4),
            "controlnet": controlnet,
            "source_image_ref": _normalize_image_ref_field(data.get("source_image_ref") or data.get("source_image_ref_json")),
            "mask_image_ref": _normalize_image_ref_field(data.get("mask_image_ref") or data.get("mask_image_ref_json")),
            "reference_image_ref": _normalize_image_ref_field(data.get("reference_image_ref") or data.get("reference_image_ref_json") or data.get("pose_reference_image_ref")),
            "upscale_model": str(data.get("upscale_model") or "").strip(),
            "outpaint": _normalize_outpaint_payload(data),
        }
        skip_asset_validation = _coerce_bool(data.get("skip_asset_validation"))
        if not skip_asset_validation and mode == "img2img" and not params["source_image_ref"]:
            return None, "圖生圖需要來源圖片"
        if not skip_asset_validation and mode == "inpaint":
            if not params["source_image_ref"]:
                return None, "局部重繪需要來源圖片"
            if not params["mask_image_ref"]:
                return None, "局部重繪需要遮罩圖片"
        if not skip_asset_validation and mode == "outpaint" and not params["source_image_ref"]:
            return None, "向外延展需要來源圖片"
        if not skip_asset_validation and mode == "upscale":
            if not params["source_image_ref"]:
                return None, "放大修復需要來源圖片"
            if not params["upscale_model"]:
                return None, "請選擇放大模型"
        if not skip_asset_validation and mode == "i2v" and not params["source_image_ref"]:
            return None, "圖生影片需要來源圖片"
        return params, None

    def _safe_text(value, limit):
        text = str(value or "").strip()
        text = re.sub(r"\r\n?", "\n", text)
        return text[:limit]

    def _ensure_comfyui_share_schema(conn):
        now = None
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS forum_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                sort_order INTEGER NOT NULL DEFAULT 100,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS forum_boards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER REFERENCES forum_categories(id) ON DELETE SET NULL,
                slug TEXT UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                rules TEXT,
                visibility TEXT NOT NULL DEFAULT 'public',
                sort_order INTEGER NOT NULL DEFAULT 100,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_activity_at TEXT,
                owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                owner_username TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'approved',
                review_note TEXT,
                reviewed_by TEXT,
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS forum_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id INTEGER NOT NULL REFERENCES forum_boards(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'approved',
                review_note TEXT,
                reviewed_by TEXT,
                reviewed_at TEXT,
                post_type TEXT NOT NULL DEFAULT 'normal',
                is_sticky INTEGER NOT NULL DEFAULT 0,
                is_locked INTEGER NOT NULL DEFAULT 0,
                is_curated INTEGER NOT NULL DEFAULT 0,
                view_count INTEGER NOT NULL DEFAULT 0,
                edited_at TEXT,
                edited_by TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT,
                deleted_by TEXT,
                delete_reason TEXT,
                author_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                author_username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        return now

    def _find_or_create_comfyui_board(conn, actor):
        _ensure_comfyui_share_schema(conn)
        row = conn.execute(
            """
            SELECT id, title FROM forum_boards
            WHERE is_active=1 AND status='approved' AND (title='ComfyUI專區' OR title LIKE '%ComfyUI%')
            ORDER BY CASE WHEN title='ComfyUI專區' THEN 0 ELSE 1 END, sort_order ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if row:
            return dict(row)
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO forum_categories (name, description, sort_order, is_active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
            ("交流討論", "預設討論分類", 10, now, now),
        )
        category = conn.execute("SELECT id FROM forum_categories WHERE name=?", ("交流討論",)).fetchone()
        cur = conn.execute(
            """
            INSERT INTO forum_boards (
                category_id, slug, title, description, rules, visibility, sort_order, is_active,
                last_activity_at, owner_user_id, owner_username, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'public', 30, 1, ?, ?, ?, 'approved', ?, ?)
            """,
            (
                category["id"] if category else None,
                "comfyui",
                "ComfyUI專區",
                "ComfyUI 工作流、模型、節點與生成參數交流。",
                "分享工作流時請標註來源與使用限制。",
                now,
                int(_actor_value(actor, "id")),
                _actor_value(actor, "username", "system"),
                now,
                now,
            ),
        )
        return {"id": cur.lastrowid, "title": "ComfyUI專區"}

    def _maybe_add_to_album(conn, *, actor, album_id, storage_file_id, file_id, caption=""):
        normalized_album_id = str(album_id or "").strip()
        if not normalized_album_id:
            return None, None
        album, msg = add_album_file(
            conn,
            actor=actor,
            album_id=normalized_album_id,
            storage_file_id=storage_file_id,
            file_id=file_id,
            caption=caption,
        )
        if msg and msg != "檔案已在相簿內":
            return None, msg
        if album is None:
            album = {"id": normalized_album_id, "already_exists": True}
        return album, None

    def _find_or_create_output_album(conn, *, actor):
        return ensure_output_album(conn, actor=actor)

    def _save_fetched_image(conn, *, actor, data, image):
        filename = _clean_filename(data.get("display_name") or image.filename)
        guessed_mime = mimetypes.guess_type(filename)[0] or image.mime_type or "image/png"
        memory_file = _MemoryFile(image.data, filename, guessed_mime)
        ensure_cloud_drive_attachment_schema(conn)
        ensure_storage_album_schema(conn)
        rule = get_member_level_rule(conn, _actor_value(actor, "effective_level") or _actor_value(actor, "member_level"))
        upload_result, msg = store_cloud_upload(
            conn,
            actor=actor,
            member_rule=rule,
            storage_root=storage_root,
            file_storage=memory_file,
            privacy_mode="standard_plain",
            scan_now=True,
        )
        if msg:
            return None, None, None, msg
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (upload_result["file_id"],)).fetchone()
        virtual_path = str(data.get("virtual_path") or "").strip()
        if not virtual_path:
            virtual_path = f"/output/{filename}"
        default_output_album = None
        if virtual_path.replace("\\", "/").strip().lower().startswith("/output/"):
            default_output_album, msg = _find_or_create_output_album(conn, actor=actor)
            if msg:
                return None, None, None, msg
        storage_file, msg = create_storage_file_entry(
            conn,
            actor=actor,
            file_row=file_row,
            virtual_path=virtual_path,
            display_name=filename,
            source="comfyui",
        )
        if msg:
            return None, None, None, msg
        selected_album_id = str(data.get("album_id") or "").strip()
        output_album_id = str((default_output_album or {}).get("id") or "").strip()
        album = None
        if output_album_id:
            album, msg = _maybe_add_to_album(
                conn,
                actor=actor,
                album_id=output_album_id,
                storage_file_id=storage_file["id"],
                file_id=upload_result["file_id"],
                caption="ComfyUI 產圖",
            )
            if msg:
                return None, None, None, msg
        if selected_album_id and selected_album_id != output_album_id:
            album, msg = _maybe_add_to_album(
                conn,
                actor=actor,
                album_id=selected_album_id,
                storage_file_id=storage_file["id"],
                file_id=upload_result["file_id"],
                caption="ComfyUI 產圖",
            )
            if msg:
                return None, None, None, msg
        return upload_result, storage_file, album, None

    def _existing_saved_image(conn, *, actor, data):
        file_id = str(data.get("file_id") or data.get("saved_file_id") or "").strip()
        if not file_id:
            return None
        ensure_cloud_drive_attachment_schema(conn)
        ensure_storage_album_schema(conn)
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=? AND deleted_at IS NULL", (file_id,)).fetchone()
        if not file_row or int(file_row["owner_user_id"]) != int(_actor_value(actor, "id")):
            return None
        storage_file_id = str(data.get("storage_file_id") or "").strip()
        params = [file_id, int(_actor_value(actor, "id"))]
        where = "file_id=? AND owner_user_id=? AND deleted_at IS NULL AND COALESCE(is_trashed, 0)=0"
        if storage_file_id:
            where += " AND id=?"
            params.append(storage_file_id)
        storage_row = conn.execute(
            f"SELECT * FROM storage_files WHERE {where} ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        upload_result = {"file_id": file_id}
        storage_file = dict(storage_row) if storage_row else None
        album, msg = _maybe_add_to_album(
            conn,
            actor=actor,
            album_id=data.get("album_id"),
            storage_file_id=storage_file["id"] if storage_file else None,
            file_id=file_id,
            caption="ComfyUI 產圖",
        )
        if msg:
            return upload_result, storage_file, album, msg
        return upload_result, storage_file, album, None

    def _compose_comfyui_share_content(data, *, file_id, storage_file):
        params = data.get("generation") if isinstance(data.get("generation"), dict) else {}
        note = _safe_text(data.get("note"), 900)
        prompt = _safe_text(params.get("prompt") or data.get("prompt"), 1400)
        negative = _safe_text(params.get("negative_prompt") or data.get("negative_prompt"), 700)
        model = _safe_text(params.get("model"), 180)
        sampler = _safe_text(params.get("sampler_name"), 80)
        scheduler = _safe_text(params.get("scheduler"), 80)
        vae = _safe_text(params.get("vae"), 180)
        output_label = _safe_text(params.get("output_label") or params.get("compare_label"), 260)
        size = f"{params.get('width') or '-'} x {params.get('height') or '-'}"
        loras = params.get("loras") if isinstance(params.get("loras"), list) else []
        lora_text = ", ".join(
            _safe_text((item or {}).get("name"), 120)
            for item in loras
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        )
        lines = []
        if note:
            lines.extend(["心得", note, ""])
        lines.extend([
            f"[[comfyui-image:{file_id}]]",
            f"圖片檔案：{_safe_text((storage_file or {}).get('display_name') or (storage_file or {}).get('virtual_path') or file_id, 160)}",
            "",
            "提示詞",
            prompt or "-",
            "",
            "負面提示詞",
            negative or "-",
            "",
            "產圖參數",
            f"輸出標籤：{output_label or '-'}",
            f"模型：{model or '-'}",
            f"尺寸：{size}",
            f"步數：{params.get('steps') or '-'}",
            f"CFG：{params.get('cfg') or '-'}",
            f"張數：{params.get('batch_size') or 1}",
            f"Seed：{params.get('seed') if params.get('seed') is not None else '-'}",
            f"Sampler：{sampler or '-'}",
            f"Scheduler：{scheduler or '-'}",
            f"VAE：{vae or '使用各自大模型內建 VAE'}",
            f"LoRA：{lora_text or '-'}",
        ])
        return "\n".join(lines)[:3900]

    register_comfyui_runtime_routes(app, {
        "request": request,
        "json_resp": json_resp,
        "require_csrf": require_csrf,
        "require_csrf_safe": require_csrf_safe,
        "get_db": get_db,
        "get_client_ip": get_client_ip,
        "get_ua": get_ua,
        "audit": audit,
        "threading": threading,
        "ComfyUIError": ComfyUIError,
        "SAFE_SAMPLER_FALLBACK": SAFE_SAMPLER_FALLBACK,
        "SAFE_SCHEDULER_FALLBACK": SAFE_SCHEDULER_FALLBACK,
        "COMFYUI_LORA_EXTRA_PRICE_POINTS": COMFYUI_LORA_EXTRA_PRICE_POINTS,
        "COMFYUI_HISTORY_LIMIT": COMFYUI_HISTORY_LIMIT,
        "DEFAULT_GENERATION_TIMEOUT_SECONDS": DEFAULT_GENERATION_TIMEOUT_SECONDS,
        "MAX_GENERATION_TIMEOUT_SECONDS": MAX_GENERATION_TIMEOUT_SECONDS,
        "COMFYUI_STATUS_TIMEOUT_SECONDS": COMFYUI_STATUS_TIMEOUT_SECONDS,
        "actor_or_401": _actor_or_401,
        "root_or_403": _root_or_403,
        "actor_value": _actor_value,
        "assert_generation_job_owner": _assert_generation_job_owner,
        "build_lora_details": _build_lora_details,
        "capture_request_audit_meta": _capture_request_audit_meta,
        "charge_comfyui_generation": _charge_comfyui_generation,
        "client_for_url": _client_for_url,
        "coerce_bool": _coerce_bool,
        "comfyui_binding": _comfyui_binding,
        "comfyui_charge_required": _comfyui_charge_required,
        "comfyui_lora_count": _comfyui_lora_count,
        "comfyui_price_quote": _comfyui_price_quote,
        "comfyui_total_quantity": _comfyui_total_quantity,
        "comfyui_unavailable_payload": _comfyui_unavailable_payload,
        "comfyui_wallet_payload": _comfyui_wallet_payload,
        "configured_comfyui_port": _configured_comfyui_port,
        "configured_comfyui_url": _configured_comfyui_url,
        "configured_connection_mode": _configured_connection_mode,
        "configured_default_dimensions": _configured_default_dimensions,
        "configured_max_batch_size": _configured_max_batch_size,
        "comfyui_storage_warnings": _comfyui_storage_warnings,
        "create_generation_job": _create_generation_job,
        "ensure_comfyui_balance": _ensure_comfyui_balance,
        "finalize_generation_records": _finalize_generation_records,
        "hydrate_generation_assets": _hydrate_generation_assets,
        "int_range": _int_range,
        "json_error_from_comfy": _json_error_from_comfy,
        "list_generation_history": _list_generation_history,
        "load_generation_history": _load_generation_history,
        "ensure_comfyui_workflow_schema": _ensure_comfyui_workflow_schema,
        "local_comfyui_runtime_status": _local_comfyui_runtime_status,
        "comfyui_paid_api_status_payload": _comfyui_paid_api_status_payload,
        "official_gguf_profiles": public_gguf_profiles,
        "installed_gguf_inventory": installed_gguf_inventory,
        "build_node_catalog": build_node_catalog,
        "normalize_generation_payload": _normalize_generation_payload,
        "normalize_generation_timeout": _normalize_generation_timeout,
        "parse_generation_request": _parse_generation_request,
        "record_generation_history": _record_generation_history,
        "parse_json_field": _parse_json_field,
        "create_workflow_run": _create_workflow_run,
        "run_comfyui_workflow_preset_job": _run_comfyui_workflow_preset_job,
        "register_active_generation": _register_active_generation,
        "generation_job_payload": _generation_job_payload,
        "image_ref_payload": _image_ref_payload,
        "initial_generation_progress": _initial_generation_progress,
        "update_generation_job_progress": _update_generation_job_progress,
        "run_comfyui_generation_job": _run_comfyui_generation_job,
        "ensure_comfyui_workflow_schema": _ensure_comfyui_workflow_schema,
        "start_local_comfyui": _start_local_comfyui,
        "stop_local_comfyui": _stop_local_comfyui,
        "unregister_active_generation": _unregister_active_generation,
        "validate_generation_capabilities": _validate_generation_capabilities,
    })

    register_comfyui_admin_routes(app, {
        "request": request,
        "root_or_403": _root_or_403,
        "actor_value": _actor_value,
        "json_resp": json_resp,
        "require_csrf": require_csrf,
        "require_csrf_safe": require_csrf_safe,
        "get_client_ip": get_client_ip,
        "get_ua": get_ua,
        "audit": audit,
        "parse_comfyui_endpoint": _parse_comfyui_endpoint,
        "local_start_script_status": _local_start_script_status,
        "client_for_url": _client_for_url,
        "ComfyUIError": ComfyUIError,
        "configured_connection_mode": _configured_connection_mode,
        "start_local_comfyui": _start_local_comfyui,
        "local_comfyui_runtime_status": _local_comfyui_runtime_status,
        "normalize_civitai_nsfw_mode": _normalize_civitai_nsfw_mode,
        "normalize_civitai_search_type": _normalize_civitai_search_type,
        "inspect_civitai_model": _inspect_civitai_model,
        "search_civitai_models": _search_civitai_models,
        "fetch_civitai_media": _fetch_civitai_media,
        "parse_civitai_download_request": _parse_civitai_download_request,
        "coerce_bool": _coerce_bool,
        "create_model_download_job": _create_model_download_job,
        "capture_request_audit_meta": _capture_request_audit_meta,
        "run_comfyui_model_download_job": _run_comfyui_model_download_job,
        "download_civitai_model_selection": _download_civitai_model_selection,
        "upload_comfyui_model_file": _upload_comfyui_model_file,
        "assert_model_download_job_owner": _assert_model_download_job_owner,
        "local_start_template_path": COMFYUI_LOCAL_START_TEMPLATE_PATH,
        "send_file": send_file,
        "threading": threading,
    })

    register_comfyui_template_routes(app, {
        "request": request,
        "actor_or_401": _actor_or_401,
        "actor_value": _actor_value,
        "json_resp": json_resp,
        "require_csrf": require_csrf,
        "get_client_ip": get_client_ip,
        "get_ua": get_ua,
        "audit": audit,
        "comfyui_binding": _comfyui_binding,
        "client_for_url": _client_for_url,
        "get_db": get_db,
        "upsert_workflow_preset": _upsert_workflow_preset,
        "load_workflow_preset_row": _load_workflow_preset_row,
        "workflow_preset_summary": _workflow_preset_summary,
    })

    register_comfyui_workflow_routes(app, {
        "request": request,
        "actor_or_401": _actor_or_401,
        "root_or_403": _root_or_403,
        "actor_value": _actor_value,
        "json_resp": json_resp,
        "require_csrf": require_csrf,
        "require_csrf_safe": require_csrf_safe,
        "get_db": get_db,
        "get_client_ip": get_client_ip,
        "get_ua": get_ua,
        "audit": audit,
        "comfyui_binding": _comfyui_binding,
        "client_for_url": _client_for_url,
        "load_workflow_preset": _load_workflow_preset,
        "workflow_preset_summary": _workflow_preset_summary,
        "workflow_manifest_for_row": _workflow_manifest_for_row,
        "parse_json_field": _parse_json_field,
        "extract_workflow_payload": _extract_workflow_payload,
        "normalize_workflow_default_params": _normalize_workflow_default_params,
        "upsert_workflow_preset": _upsert_workflow_preset,
        "load_workflow_preset_row": _load_workflow_preset_row,
        "WorkflowValidationError": WorkflowValidationError,
        "ComfyUIError": ComfyUIError,
        "list_workflow_presets": _list_workflow_presets,
        "workflow_dependency_status": _workflow_dependency_status,
        "list_workflow_runs": _list_workflow_runs,
        "resolve_file_storage_path": resolve_file_storage_path,
        "storage_root": storage_root,
        "normalize_generation_payload": _normalize_generation_payload,
        "validate_generation_capabilities": _validate_generation_capabilities,
        "sanitize_workflow_json": sanitize_workflow_json,
        "workflow_json_to_pretty_text": workflow_json_to_pretty_text,
        "analyze_workflow_json": analyze_workflow_json,
        "build_ui_schema": build_ui_schema,
        "check_workflow_capability": check_workflow_capability,
        "assert_workflow_dependencies_or_error": _assert_workflow_dependencies_or_error,
        "create_workflow_run": _create_workflow_run,
        "create_generation_job": _create_generation_job,
        "capture_request_audit_meta": _capture_request_audit_meta,
        "run_comfyui_workflow_preset_job": _run_comfyui_workflow_preset_job,
        "comfyui_paid_api_policy": _comfyui_paid_api_policy,
        "DEFAULT_GENERATION_TIMEOUT_SECONDS": DEFAULT_GENERATION_TIMEOUT_SECONDS,
        "COMFYUI_WORKFLOW_RUN_LIMIT": COMFYUI_WORKFLOW_RUN_LIMIT,
        "COMFYUI_WORKFLOW_SCHEMA_VERSION": COMFYUI_WORKFLOW_SCHEMA_VERSION,
        "APP_RELEASE_ID": APP_RELEASE_ID,
        "safe_text": _safe_text,
        "threading": threading,
    })

    register_comfyui_image_routes(app, {
        "base64": base64,
        "send_file": send_file,
        "request": request,
        "json_resp": json_resp,
        "require_csrf": require_csrf,
        "get_db": get_db,
        "get_client_ip": get_client_ip,
        "get_ua": get_ua,
        "audit": audit,
        "attach_existing_file": attach_existing_file,
        "can_download_file": can_download_file,
        "datetime": datetime,
        "ComfyUIError": ComfyUIError,
        "active_generation_snapshot": _active_generation_snapshot,
        "actor_or_401": _actor_or_401,
        "actor_value": _actor_value,
        "assert_reasonable_image_size": _assert_reasonable_image_size,
        "client": _client,
        "client_for_url": _client_for_url,
        "comfyui_binding": _comfyui_binding,
        "compose_comfyui_share_content": _compose_comfyui_share_content,
        "configured_comfyui_base_dir": _configured_comfyui_base_dir,
        "configured_comfyui_project_dir": _configured_comfyui_project_dir,
        "existing_saved_image": _existing_saved_image,
        "find_or_create_comfyui_board": _find_or_create_comfyui_board,
        "generation_owner_id": _generation_owner_id,
        "image_ref_payload": _image_ref_payload,
        "interrupt_policy": _interrupt_policy,
        "is_root": _is_root,
        "json_error_from_comfy": _json_error_from_comfy,
        "load_comfyui_image_ref_record": _load_comfyui_image_ref_record,
        "list_generation_history": _list_generation_history,
        "normalize_comfyui_backend_url": _normalize_comfyui_backend_url,
        "register_comfyui_image_refs": _register_comfyui_image_refs,
        "resolve_file_storage_path": resolve_file_storage_path,
        "safe_text": _safe_text,
        "save_fetched_image": _save_fetched_image,
        "configured_civitai_api_key": _configured_civitai_api_key,
        "civitai_headers": _civitai_headers,
        "fetch_json": _fetch_json,
        "safe_civitai_media_url": _safe_civitai_media_url,
        "fetch_civitai_media": _fetch_civitai_media,
        "CIVITAI_API_BASE": CIVITAI_API_BASE,
        "CIVITAI_API_BASES": CIVITAI_API_BASES,
        "validate_image_upload": _validate_image_upload,
        "validate_video_upload": _validate_video_upload,
        "storage_root": storage_root,
        "COMFYUI_ALLOWED_IMAGE_EXTENSIONS": COMFYUI_ALLOWED_IMAGE_EXTENSIONS,
        "COMFYUI_ALLOWED_IMAGE_MIME_TYPES": COMFYUI_ALLOWED_IMAGE_MIME_TYPES,
        "COMFYUI_ALLOWED_VIDEO_EXTENSIONS": COMFYUI_ALLOWED_VIDEO_EXTENSIONS,
        "COMFYUI_ALLOWED_VIDEO_MIME_TYPES": COMFYUI_ALLOWED_VIDEO_MIME_TYPES,
        "MAX_COMFYUI_FETCH_IMAGE_BYTES": MAX_COMFYUI_FETCH_IMAGE_BYTES,
        "MAX_COMFYUI_FETCH_VIDEO_BYTES": MAX_COMFYUI_FETCH_VIDEO_BYTES,
        "COMFYUI_INTERRUPT_TIMEOUT_SECONDS": COMFYUI_INTERRUPT_TIMEOUT_SECONDS,
    })
