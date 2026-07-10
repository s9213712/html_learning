import json
import mimetypes
import re
import inspect
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


def register_comfyui_image_routes(app, ctx):
    base64 = ctx["base64"]
    send_file = ctx.get("send_file")
    request = ctx["request"]
    json_resp = ctx["json_resp"]
    require_csrf = ctx["require_csrf"]
    get_db = ctx["get_db"]
    get_client_ip = ctx["get_client_ip"]
    get_ua = ctx["get_ua"]
    audit = ctx["audit"]
    attach_existing_file = ctx["attach_existing_file"]
    can_download_file = ctx["can_download_file"]
    datetime = ctx["datetime"]
    ComfyUIError = ctx["ComfyUIError"]
    _active_generation_snapshot = ctx["active_generation_snapshot"]
    _actor_or_401 = ctx["actor_or_401"]
    _actor_value = ctx["actor_value"]
    _assert_reasonable_image_size = ctx["assert_reasonable_image_size"]
    _client = ctx["client"]
    _client_for_url = ctx["client_for_url"]
    _comfyui_binding = ctx["comfyui_binding"]
    _compose_comfyui_share_content = ctx["compose_comfyui_share_content"]
    _configured_comfyui_base_dir = ctx["configured_comfyui_base_dir"]
    _configured_comfyui_project_dir = ctx["configured_comfyui_project_dir"]
    _existing_saved_image = ctx["existing_saved_image"]
    _find_or_create_comfyui_board = ctx["find_or_create_comfyui_board"]
    _generation_owner_id = ctx["generation_owner_id"]
    _image_ref_payload = ctx["image_ref_payload"]
    _interrupt_policy = ctx["interrupt_policy"]
    _is_root = ctx["is_root"]
    _json_error_from_comfy = ctx["json_error_from_comfy"]
    _load_comfyui_image_ref_record = ctx["load_comfyui_image_ref_record"]
    _list_generation_history = ctx["list_generation_history"]
    _normalize_comfyui_backend_url = ctx["normalize_comfyui_backend_url"]
    _register_comfyui_image_refs = ctx["register_comfyui_image_refs"]
    resolve_file_storage_path = ctx["resolve_file_storage_path"]
    _safe_text = ctx["safe_text"]
    _save_fetched_image = ctx["save_fetched_image"]
    _configured_civitai_api_key = ctx.get("configured_civitai_api_key")
    _civitai_headers = ctx.get("civitai_headers")
    _fetch_json = ctx.get("fetch_json")
    _safe_civitai_media_url = ctx.get("safe_civitai_media_url")
    _fetch_civitai_media = ctx.get("fetch_civitai_media")
    _validate_image_upload = ctx["validate_image_upload"]
    _validate_video_upload = ctx["validate_video_upload"]
    storage_root = ctx["storage_root"]
    COMFYUI_ALLOWED_IMAGE_EXTENSIONS = ctx["COMFYUI_ALLOWED_IMAGE_EXTENSIONS"]
    COMFYUI_ALLOWED_IMAGE_MIME_TYPES = ctx["COMFYUI_ALLOWED_IMAGE_MIME_TYPES"]
    COMFYUI_ALLOWED_VIDEO_EXTENSIONS = ctx["COMFYUI_ALLOWED_VIDEO_EXTENSIONS"]
    COMFYUI_ALLOWED_VIDEO_MIME_TYPES = ctx["COMFYUI_ALLOWED_VIDEO_MIME_TYPES"]
    MAX_COMFYUI_FETCH_IMAGE_BYTES = ctx["MAX_COMFYUI_FETCH_IMAGE_BYTES"]
    MAX_COMFYUI_FETCH_VIDEO_BYTES = ctx["MAX_COMFYUI_FETCH_VIDEO_BYTES"]
    COMFYUI_INTERRUPT_TIMEOUT_SECONDS = ctx.get("COMFYUI_INTERRUPT_TIMEOUT_SECONDS", 2.0)
    CIVITAI_API_BASE = str(ctx.get("CIVITAI_API_BASE") or "https://civitai.com/api/v1").rstrip("/")
    CIVITAI_API_BASES = [str(item).rstrip("/") for item in list(ctx.get("CIVITAI_API_BASES") or [CIVITAI_API_BASE]) if str(item or "").strip()]

    def _actor_id(actor):
        return int(_actor_value(actor, "id"))

    def _parse_json_object(raw, fallback=None):
        if fallback is None:
            fallback = {}
        if isinstance(raw, dict):
            return raw
        if not raw:
            return dict(fallback)
        try:
            parsed = json.loads(raw)
        except Exception:
            return dict(fallback)
        return parsed if isinstance(parsed, dict) else dict(fallback)

    def _json_dumps(value, fallback=None):
        if fallback is None:
            fallback = {}
        try:
            return json.dumps(value if isinstance(value, (dict, list)) else fallback, ensure_ascii=False, sort_keys=True)
        except Exception:
            return json.dumps(fallback, ensure_ascii=False, sort_keys=True)

    def _text(value, limit=3000):
        return str(value or "").strip()[:limit]

    def _nullable_int(value):
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except Exception:
            return None

    def _nullable_float(value):
        if value is None or value == "":
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _ensure_comfyui_image_favorite_schema(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comfyui_image_favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'manual',
                title TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                civitai_image_id INTEGER,
                image_url TEXT NOT NULL DEFAULT '',
                backend_url TEXT NOT NULL DEFAULT '',
                image_ref_json TEXT NOT NULL DEFAULT '{}',
                cloud_file_id TEXT NOT NULL DEFAULT '',
                storage_file_id TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                prompt TEXT NOT NULL DEFAULT '',
                negative_prompt TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                vae TEXT NOT NULL DEFAULT '',
                sampler_name TEXT NOT NULL DEFAULT '',
                scheduler TEXT NOT NULL DEFAULT '',
                seed TEXT NOT NULL DEFAULT '',
                steps INTEGER,
                cfg REAL,
                width INTEGER,
                height INTEGER,
                params_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(comfyui_image_favorites)").fetchall()}
        definitions = {
            "source_type": "TEXT NOT NULL DEFAULT 'manual'",
            "title": "TEXT NOT NULL DEFAULT ''",
            "note": "TEXT NOT NULL DEFAULT ''",
            "source_url": "TEXT NOT NULL DEFAULT ''",
            "civitai_image_id": "INTEGER",
            "image_url": "TEXT NOT NULL DEFAULT ''",
            "backend_url": "TEXT NOT NULL DEFAULT ''",
            "image_ref_json": "TEXT NOT NULL DEFAULT '{}'",
            "cloud_file_id": "TEXT NOT NULL DEFAULT ''",
            "storage_file_id": "TEXT NOT NULL DEFAULT ''",
            "filename": "TEXT NOT NULL DEFAULT ''",
            "mime_type": "TEXT NOT NULL DEFAULT ''",
            "size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "prompt": "TEXT NOT NULL DEFAULT ''",
            "negative_prompt": "TEXT NOT NULL DEFAULT ''",
            "model": "TEXT NOT NULL DEFAULT ''",
            "vae": "TEXT NOT NULL DEFAULT ''",
            "sampler_name": "TEXT NOT NULL DEFAULT ''",
            "scheduler": "TEXT NOT NULL DEFAULT ''",
            "seed": "TEXT NOT NULL DEFAULT ''",
            "steps": "INTEGER",
            "cfg": "REAL",
            "width": "INTEGER",
            "height": "INTEGER",
            "params_json": "TEXT NOT NULL DEFAULT '{}'",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in definitions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE comfyui_image_favorites ADD COLUMN {name} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comfyui_image_favorites_owner ON comfyui_image_favorites(owner_user_id, created_at DESC)")
        try:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_comfyui_image_favorites_civitai_owner
                ON comfyui_image_favorites(owner_user_id, civitai_image_id)
                WHERE civitai_image_id IS NOT NULL
                """
            )
        except Exception:
            pass

    def _favorite_row_payload(row):
        params = _parse_json_object(row["params_json"], {})
        metadata = _parse_json_object(row["metadata_json"], {})
        image_ref = _image_ref_payload(_parse_json_object(row["image_ref_json"], {})) or {}
        canonical = {
            "generation_mode": params.get("generation_mode") or "",
            "prompt": row["prompt"] or "",
            "negative_prompt": row["negative_prompt"] or "",
            "model": row["model"] or "",
            "vae": row["vae"] or "",
            "sampler_name": row["sampler_name"] or "",
            "scheduler": row["scheduler"] or "",
            "seed": row["seed"] or "",
            "steps": row["steps"],
            "cfg": row["cfg"],
            "width": row["width"],
            "height": row["height"],
        }
        merged_params = dict(params)
        merged_params.update({key: value for key, value in canonical.items() if value not in (None, "")})
        return {
            "id": int(row["id"]),
            "source_type": row["source_type"] or "manual",
            "title": row["title"] or "",
            "note": row["note"] or "",
            "source_url": row["source_url"] or "",
            "civitai_image_id": row["civitai_image_id"],
            "image_url": row["image_url"] or "",
            "preview_url": f"/api/comfyui/image-favorites/{int(row['id'])}/preview",
            "filename": row["filename"] or "",
            "mime_type": row["mime_type"] or "",
            "size_bytes": int(row["size_bytes"] or 0),
            "cloud_file_id": row["cloud_file_id"] or "",
            "storage_file_id": row["storage_file_id"] or "",
            "image_ref": image_ref,
            "params": merged_params,
            "metadata": metadata,
            "created_at": row["created_at"] or "",
            "updated_at": row["updated_at"] or "",
        }

    def _favorite_fields_from_mapping(data, *, source_type="manual", image_ref=None, backend_url="", cloud_file_id="", storage_file_id="", filename="", mime_type="", size_bytes=0, image_url="", source_url="", civitai_image_id=None, metadata=None):
        data = data if isinstance(data, dict) else {}
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        metadata = metadata if isinstance(metadata, dict) else (data.get("metadata") if isinstance(data.get("metadata"), dict) else {})

        def pick(*keys):
            for key in keys:
                if key in data and data.get(key) not in (None, ""):
                    return data.get(key)
                if key in params and params.get(key) not in (None, ""):
                    return params.get(key)
            return ""

        prompt = _text(pick("prompt", "positive_prompt"), 3000)
        negative_prompt = _text(pick("negative_prompt", "negativePrompt"), 3000)
        model = _text(pick("model", "checkpoint", "ckpt_name", "diffusers_model_repo"), 240)
        title = _safe_text(data.get("title"), 120) or _safe_text(model, 120) or _safe_text(prompt, 80) or "圖片收藏"
        canonical = {
            "generation_mode": _text(pick("generation_mode"), 40),
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "model": model,
            "diffusers_model_repo": _text(pick("diffusers_model_repo"), 240),
            "diffusers_model_variant": _text(pick("diffusers_model_variant"), 120),
            "vae": _text(pick("vae"), 240),
            "sampler_name": _text(pick("sampler_name", "sampler"), 120),
            "scheduler": _text(pick("scheduler"), 120),
            "seed": _text(pick("seed"), 80),
            "steps": _nullable_int(pick("steps")),
            "cfg": _nullable_float(pick("cfg", "cfg_scale", "cfgScale")),
            "width": _nullable_int(pick("width")),
            "height": _nullable_int(pick("height")),
        }
        merged_params = dict(params)
        merged_params.update({key: value for key, value in canonical.items() if value not in (None, "")})
        for key in (
            "workflow_preset_id",
            "workflow_preset_title",
            "workflow_system_bundle_id",
            "workflow_bundle_id",
            "preset_id",
            "preset_title",
            "skip_refiner",
            "sdxl_refiner",
            "seed_after_generate",
            "run_count",
        ):
            value = pick(key)
            if value not in (None, ""):
                merged_params[key] = value
        loras = pick("loras")
        if isinstance(loras, list):
            merged_params["loras"] = loras
        return {
            "source_type": _text(source_type or data.get("source_type") or "manual", 40) or "manual",
            "title": title,
            "note": _safe_text(data.get("note"), 1200),
            "source_url": _text(source_url or data.get("source_url"), 800),
            "civitai_image_id": _nullable_int(civitai_image_id),
            "image_url": _text(image_url or data.get("image_url"), 1200),
            "backend_url": _text(backend_url, 600),
            "image_ref": _image_ref_payload(image_ref or data.get("image_ref")) or {},
            "cloud_file_id": _text(cloud_file_id or data.get("cloud_file_id") or data.get("file_id"), 80),
            "storage_file_id": _text(storage_file_id or data.get("storage_file_id"), 80),
            "filename": _safe_text(filename or data.get("filename"), 180),
            "mime_type": _text(mime_type or data.get("mime_type"), 100),
            "size_bytes": max(0, int(size_bytes or data.get("size_bytes") or 0)),
            "prompt": canonical["prompt"],
            "negative_prompt": canonical["negative_prompt"],
            "model": canonical["model"],
            "vae": canonical["vae"],
            "sampler_name": canonical["sampler_name"],
            "scheduler": canonical["scheduler"],
            "seed": canonical["seed"],
            "steps": canonical["steps"],
            "cfg": canonical["cfg"],
            "width": canonical["width"],
            "height": canonical["height"],
            "params": merged_params,
            "metadata": metadata,
        }

    def _civitai_workflow_hint(params):
        model = str(params.get("model") or "").lower()
        model_name = str(params.get("source_model_name") or "").lower()
        base_model = str((params.get("civitai") or {}).get("base_model") or "").lower()
        civitai_payload = params.get("civitai") if isinstance(params.get("civitai"), dict) else {}
        resource_names = []
        for resource in (civitai_payload.get("model_version_resources") or []):
            if isinstance(resource, dict):
                resource_names.append(str(resource.get("model_name") or "").lower())
                resource_names.append(str(resource.get("version_name") or "").lower())
        resource_names = " ".join(name for name in resource_names if name)
        family_text = f"{model} {model_name} {base_model} {resource_names}"
        family_compact = re.sub(r"[^a-z0-9]+", "", family_text.lower())
        if ("zit" in family_text or "z_image_turbo" in model or
                "zimagebase" in family_compact or "zimage" in family_compact):
            return {
                "workflow_system_bundle_id": "origin_zit_txt2img",
                "workflow_preset_title": "ZIT Text-to-Image",
            }
        if "anima" in family_text:
            return {
                "workflow_system_bundle_id": "origin_anima_txt2img",
                "workflow_preset_title": "ANIMA Text-to-Image",
            }
        if "flux" in family_text or "flux1-" in model or "flux-1" in model:
            return {
                "workflow_system_bundle_id": "origin_flux_dev_txt2img",
                "workflow_preset_title": "Flux Dev Full Text-to-Image",
            }
        if "netayume" in family_text:
            return {
                "workflow_system_bundle_id": "origin_netayume_txt2img",
                "workflow_preset_title": "NetaYume Text-to-Image",
            }
        if "sd3.5" in family_text or "sd 3.5" in family_text or "sd3_5" in family_text:
            return {
                "workflow_system_bundle_id": "origin_sd35_txt2img",
                "workflow_preset_title": "SD3.5 Text-to-Image",
            }
        if any(token in family_text for token in ("sdxl", "sd xl", "illustrious", "pony", "noob", "xl")):
            return {
                "workflow_system_bundle_id": "origin_sdxl_txt2img",
                "workflow_preset_title": "SDXL Text-to-Image",
            }
        return {}

    def _insert_or_update_image_favorite(conn, actor, fields):
        _ensure_comfyui_image_favorite_schema(conn)
        now = datetime.now().isoformat()
        image_ref = _image_ref_payload(fields.get("image_ref")) or {}
        values = {
            "owner_user_id": _actor_id(actor),
            "source_type": fields.get("source_type") or "manual",
            "title": fields.get("title") or "圖片收藏",
            "note": fields.get("note") or "",
            "source_url": fields.get("source_url") or "",
            "civitai_image_id": fields.get("civitai_image_id"),
            "image_url": fields.get("image_url") or "",
            "backend_url": fields.get("backend_url") or "",
            "image_ref_json": _json_dumps(image_ref),
            "cloud_file_id": fields.get("cloud_file_id") or "",
            "storage_file_id": fields.get("storage_file_id") or "",
            "filename": fields.get("filename") or "",
            "mime_type": fields.get("mime_type") or "",
            "size_bytes": int(fields.get("size_bytes") or 0),
            "prompt": fields.get("prompt") or "",
            "negative_prompt": fields.get("negative_prompt") or "",
            "model": fields.get("model") or "",
            "vae": fields.get("vae") or "",
            "sampler_name": fields.get("sampler_name") or "",
            "scheduler": fields.get("scheduler") or "",
            "seed": fields.get("seed") or "",
            "steps": fields.get("steps"),
            "cfg": fields.get("cfg"),
            "width": fields.get("width"),
            "height": fields.get("height"),
            "params_json": _json_dumps(fields.get("params") if isinstance(fields.get("params"), dict) else {}),
            "metadata_json": _json_dumps(fields.get("metadata") if isinstance(fields.get("metadata"), dict) else {}),
            "created_at": now,
            "updated_at": now,
        }
        favorite_id = None
        if values["civitai_image_id"] is not None:
            existing = conn.execute(
                "SELECT id FROM comfyui_image_favorites WHERE owner_user_id=? AND civitai_image_id=? LIMIT 1",
                (values["owner_user_id"], values["civitai_image_id"]),
            ).fetchone()
            if existing:
                favorite_id = int(existing["id"])
                update_columns = [key for key in values if key not in {"owner_user_id", "created_at"}]
                assignments = ", ".join(f"{key}=?" for key in update_columns)
                conn.execute(
                    f"UPDATE comfyui_image_favorites SET {assignments} WHERE id=? AND owner_user_id=?",
                    tuple(values[key] for key in update_columns) + (favorite_id, values["owner_user_id"]),
                )
        if favorite_id is None:
            columns = list(values.keys())
            placeholders = ", ".join("?" for _ in columns)
            cur = conn.execute(
                f"INSERT INTO comfyui_image_favorites ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values[key] for key in columns),
            )
            favorite_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT * FROM comfyui_image_favorites WHERE id=? AND owner_user_id=?",
            (favorite_id, values["owner_user_id"]),
        ).fetchone()
        return _favorite_row_payload(row)

    def _meta_get(mapping, *keys):
        if not isinstance(mapping, dict):
            return ""
        lowered = {str(key).strip().lower(): value for key, value in mapping.items()}
        for key in keys:
            if key in mapping and mapping.get(key) not in (None, ""):
                return mapping.get(key)
            value = lowered.get(str(key).strip().lower())
            if value not in (None, ""):
                return value
        return ""

    def _civitai_generation_meta(image_entry):
        raw_meta = image_entry.get("meta") if isinstance(image_entry, dict) else {}
        if not isinstance(raw_meta, dict):
            return {}
        nested = raw_meta.get("meta")
        if isinstance(nested, dict):
            return nested
        return raw_meta

    def _parse_civitai_image_reference(page_url):
        raw = str(page_url or "").strip()
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if parsed.scheme != "https" or host not in {"civitai.com", "civitai.red", "civitai.green"}:
            return None, "只接受 Civitai 圖片頁網址"
        image_id = None
        path_match = re.search(r"/images/(\d+)", parsed.path or "", re.IGNORECASE)
        if path_match:
            image_id = int(path_match.group(1))
        else:
            query = parse_qs(parsed.query or "")
            for key in ("imageId", "image_id", "id"):
                raw_value = str((query.get(key) or [""])[0] or "").strip()
                if raw_value.isdigit():
                    image_id = int(raw_value)
                    break
        if not image_id:
            return None, "無法從網址解析 Civitai image id"
        return {
            "image_id": image_id,
            "page_url": urlunparse(parsed._replace(fragment="")),
            "source_site": host,
        }, None

    def _civitai_api_base_for_site(source_site):
        site = str(source_site or "civitai.com").strip().lower()
        if site.startswith("www."):
            site = site[4:]
        for base in CIVITAI_API_BASES:
            parsed = urlparse(base)
            host = (parsed.hostname or "").lower()
            if host == site:
                return base
        return f"https://{site}/api/v1"

    def _parse_size_text(value):
        match = re.search(r"(\d{2,5})\s*[xX×*]\s*(\d{2,5})", str(value or ""))
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def _civitai_source_model_name(image_entry, meta):
        resources = meta.get("resources") if isinstance(meta, dict) else []
        if isinstance(resources, list):
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                kind = str(resource.get("type") or "").strip().lower()
                if kind in {"model", "checkpoint", "base_model"}:
                    name = _text(resource.get("name") or resource.get("modelName"), 240)
                    if name:
                        return name
        for container_key in ("modelVersion", "model"):
            container = image_entry.get(container_key)
            if isinstance(container, dict):
                name = _text(container.get("name"), 240)
                if name:
                    return name
        return _text(_meta_get(meta, "Model", "model", "checkpoint", "Checkpoint"), 240)

    def _looks_like_model_filename(value):
        return bool(re.search(r"\.(safetensors|ckpt|pt|pth|bin|gguf)$", str(value or "").strip(), re.IGNORECASE))

    def _is_civitai_checkpoint_resource(resource):
        model_type = str(resource.get("model_type") or "").strip().lower()
        air = str(resource.get("air") or "").strip().lower()
        return (
            model_type in {"checkpoint", "model"}
            or "checkpoint" in model_type
            or ":checkpoint:" in air
        )

    def _civitai_checkpoint_model_file(version_resources, source_model_name=""):
        for resource in version_resources or []:
            if not isinstance(resource, dict) or not _is_civitai_checkpoint_resource(resource):
                continue
            primary = resource.get("primary_file") if isinstance(resource.get("primary_file"), dict) else {}
            filename = _text(primary.get("name"), 240)
            if filename:
                return filename
        source = _text(source_model_name, 240)
        return source if _looks_like_model_filename(source) else ""

    def _primary_civitai_version_file(version_entry):
        files = version_entry.get("files") if isinstance(version_entry, dict) else []
        files = files if isinstance(files, list) else []
        if not files:
            return {}
        primary = next((item for item in files if isinstance(item, dict) and item.get("primary")), None)
        if primary is None:
            primary = next((item for item in files if isinstance(item, dict)), None)
        if not isinstance(primary, dict):
            return {}
        hashes = primary.get("hashes") if isinstance(primary.get("hashes"), dict) else {}
        return {
            "id": primary.get("id"),
            "name": _text(primary.get("name"), 240),
            "type": _text(primary.get("type"), 80),
            "size_kb": primary.get("sizeKB"),
            "hashes": {str(key): str(value) for key, value in hashes.items() if value},
            "download_url": _text(primary.get("downloadUrl") or version_entry.get("downloadUrl"), 800),
        }

    def _serialize_civitai_model_version_resource(version_entry):
        if not isinstance(version_entry, dict):
            return {}
        model = version_entry.get("model") if isinstance(version_entry.get("model"), dict) else {}
        return {
            "model_version_id": version_entry.get("id"),
            "model_id": version_entry.get("modelId"),
            "version_name": _text(version_entry.get("name"), 240),
            "base_model": _text(version_entry.get("baseModel"), 120),
            "air": _text(version_entry.get("air"), 400),
            "model_name": _text(model.get("name"), 240),
            "model_type": _text(model.get("type"), 80),
            "trained_words": [_text(item, 120) for item in list(version_entry.get("trainedWords") or [])[:30] if _text(item, 120)],
            "primary_file": _primary_civitai_version_file(version_entry),
        }

    def _fetch_civitai_model_version_resources(version_ids, api_base, headers):
        resources = []
        seen = set()
        for raw_id in list(version_ids or [])[:8]:
            version_id = _nullable_int(raw_id)
            if not version_id or version_id in seen:
                continue
            seen.add(version_id)
            try:
                payload = _fetch_json(f"{api_base}/model-versions/{version_id}", headers=headers, timeout=20)
            except Exception as exc:
                resources.append({
                    "model_version_id": version_id,
                    "error": f"{str(exc)[:180]}",
                })
                continue
            resource = _serialize_civitai_model_version_resource(payload)
            if resource:
                resources.append(resource)
        return resources

    def _is_civitai_embedding_resource(resource):
        model_type = str(resource.get("model_type") or "").strip().lower()
        air = str(resource.get("air") or "").strip().lower()
        primary = resource.get("primary_file") if isinstance(resource.get("primary_file"), dict) else {}
        primary_type = str(primary.get("type") or "").strip().lower()
        return (
            model_type in {"textualinversion", "textual inversion", "embedding", "embeddings"}
            or "textualinversion" in model_type
            or "embedding" in model_type
            or ":embedding:" in air
            or primary_type in {"textualinversion", "textual inversion", "embedding"}
        )

    def _civitai_embedding_file_replacements(version_resources):
        replacements = []
        embedding_files = []
        seen_words = set()
        seen_files = set()
        for resource in version_resources or []:
            if not isinstance(resource, dict) or not _is_civitai_embedding_resource(resource):
                continue
            primary = resource.get("primary_file") if isinstance(resource.get("primary_file"), dict) else {}
            embedding_file = _text(primary.get("name") or resource.get("version_name") or resource.get("model_name"), 240)
            if not embedding_file:
                continue
            file_key = embedding_file.lower()
            if file_key not in seen_files:
                seen_files.add(file_key)
                embedding_files.append(embedding_file)
            for raw_word in list(resource.get("trained_words") or [])[:30]:
                word = _text(raw_word, 120)
                if not word:
                    continue
                word_key = word.lower()
                if word_key in seen_words:
                    continue
                seen_words.add(word_key)
                replacements.append((word, f"<embeddings:{embedding_file}>"))
        return embedding_files, replacements

    def _replace_civitai_embedding_trained_words(text, replacements):
        result = _text(text, 3000)
        if not result or not replacements:
            return result
        for word, token in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
            if not word or not token or token in result:
                continue
            pattern = re.compile(rf"(?<![\w:<>/.\-]){re.escape(word)}(?![\w:<>/.\-])", re.IGNORECASE)
            result = pattern.sub(token, result)
        return result

    def _normalize_civitai_image_entry(image_entry, reference, *, version_resources=None):
        if not isinstance(image_entry, dict):
            return None, "Civitai API 沒有回傳圖片資料"
        meta = _civitai_generation_meta(image_entry)
        image_url = _safe_civitai_media_url(image_entry.get("url")) if _safe_civitai_media_url else ""
        if not image_url:
            return None, "Civitai 圖片網址不在允許的圖片主機"
        raw_vae = _text(_meta_get(meta, "VAE", "vae"), 240)
        raw_vae = "" if raw_vae.lower() in {"", "auto", "automatic", "builtin", "default", "__checkpoint_builtin__", "__checkpoint__builtin__", "none", "null", "n/a", "na"} else raw_vae
        parsed_width, parsed_height = _parse_size_text(_meta_get(meta, "Size", "size"))
        width = _nullable_int(_meta_get(meta, "width", "Width")) or parsed_width or _nullable_int(image_entry.get("width"))
        height = _nullable_int(_meta_get(meta, "height", "Height")) or parsed_height or _nullable_int(image_entry.get("height"))
        if not width or not height:
            width = width or parsed_width
            height = height or parsed_height
        cfg_value = _meta_get(meta, "cfgScale", "CFG scale", "cfg_scale", "cfg", "CFG")
        raw_version_ids = image_entry.get("modelVersionIds") if isinstance(image_entry.get("modelVersionIds"), list) else []
        raw_resources = meta.get("resources") if isinstance(meta.get("resources"), list) else []
        version_resources = list(version_resources or [])
        source_model_name = _civitai_source_model_name(image_entry, meta)
        checkpoint_model = _civitai_checkpoint_model_file(version_resources, source_model_name)
        embedding_files, embedding_replacements = _civitai_embedding_file_replacements(version_resources)
        params = {
            "generation_mode": "txt2img",
            "prompt": _replace_civitai_embedding_trained_words(_meta_get(meta, "prompt", "Prompt") or image_entry.get("prompt"), embedding_replacements),
            "negative_prompt": _replace_civitai_embedding_trained_words(_meta_get(meta, "negativePrompt", "Negative prompt", "negative_prompt"), embedding_replacements),
            "model": checkpoint_model,
            "source_model_name": source_model_name,
            "vae": raw_vae,
            "sampler_name": _text(_meta_get(meta, "sampler", "Sampler", "sampler_name"), 120),
            "scheduler": _text(_meta_get(meta, "scheduler", "Scheduler"), 120),
            "seed": _text(_meta_get(meta, "seed", "Seed"), 80),
            "steps": _nullable_int(_meta_get(meta, "steps", "Steps")),
            "cfg": _nullable_float(cfg_value),
            "width": width,
            "height": height,
            "civitai": {
                "image_id": int(image_entry.get("id") or reference.get("image_id") or 0),
                "model_id": image_entry.get("modelId"),
                "model_version_id": image_entry.get("modelVersionId"),
                "model_version_ids": [_nullable_int(item) for item in raw_version_ids if _nullable_int(item)],
                "source_site": reference.get("source_site") or "civitai.com",
                "base_model": _text(image_entry.get("baseModel") or _meta_get(meta, "baseModel", "Base model"), 120),
                "source_model_name": source_model_name,
                "resources": raw_resources,
                "model_version_resources": version_resources,
            },
        }
        workflow_hint = _civitai_workflow_hint(params)
        if embedding_files and workflow_hint.get("workflow_system_bundle_id") not in {"origin_anima_txt2img", "origin_zit_txt2img"}:
            params["embeddings"] = embedding_files
        params.update(workflow_hint)
        fields = _favorite_fields_from_mapping(
            {"title": f"Civitai #{params['civitai']['image_id']}", "params": params},
            source_type="civitai",
            image_url=image_url,
            source_url=reference.get("page_url") or f"https://{reference.get('source_site') or 'civitai.com'}/images/{params['civitai']['image_id']}",
            civitai_image_id=params["civitai"]["image_id"],
            filename=Path(urlparse(image_url).path).name or f"civitai_{params['civitai']['image_id']}.jpg",
            mime_type=mimetypes.guess_type(urlparse(image_url).path)[0] or "",
            metadata={
                "civitai_image": {
                    "id": image_entry.get("id"),
                    "url": image_entry.get("url"),
                    "width": image_entry.get("width"),
                    "height": image_entry.get("height"),
                    "nsfw": image_entry.get("nsfw"),
                    "nsfwLevel": image_entry.get("nsfwLevel"),
                    "createdAt": image_entry.get("createdAt"),
                    "postId": image_entry.get("postId"),
                    "username": image_entry.get("username"),
                    "baseModel": image_entry.get("baseModel"),
                    "modelVersionIds": raw_version_ids,
                    "meta": meta,
                },
                "imported_from": reference.get("page_url") or "",
                "metadata_source": "civitai_api",
            },
        )
        return fields, None

    def _cloud_image_row_payload(row, *, storage_row=None):
        filename = row["original_filename_plain_for_public"] or "image.png"
        return {
            "source": "cloud_drive",
            "file_id": row["id"],
            "storage_file_id": (storage_row or {}).get("id") if isinstance(storage_row, dict) else None,
            "filename": filename,
            "virtual_path": (storage_row or {}).get("virtual_path") if isinstance(storage_row, dict) else "",
            "mime_type": row["mime_type_plain_for_public"] or "",
            "size_bytes": int(row["size_bytes"] or 0),
            "scan_status": row["scan_status"] or "",
            "risk_level": row["risk_level"] or "",
            "created_at": row["created_at"] or "",
        }

    def _list_cloud_drive_image_candidates(conn, actor, *, limit=80):
        rows = conn.execute(
            """
            SELECT *
            FROM uploaded_files
            WHERE owner_user_id=? AND deleted_at IS NULL
                  AND privacy_mode='standard_plain'
                  AND lower(COALESCE(mime_type_plain_for_public, '')) IN ('image/png', 'image/jpeg', 'image/webp')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(_actor_value(actor, "id")), int(limit)),
        ).fetchall()
        candidates = []
        for row in rows:
            filename = row["original_filename_plain_for_public"] or ""
            if Path(filename).suffix.lower() not in COMFYUI_ALLOWED_IMAGE_EXTENSIONS:
                continue
            try:
                path = resolve_file_storage_path(storage_root, row)
            except Exception:
                continue
            if not path.exists() or not path.is_file():
                continue
            allowed, _reason, _download_row = can_download_file(conn, actor=actor, file_id=row["id"], action="preview")
            if not allowed:
                continue
            storage_row = conn.execute(
                """
                SELECT id, virtual_path, display_name
                FROM storage_files
                WHERE owner_user_id=? AND file_id=? AND deleted_at IS NULL AND COALESCE(is_trashed, 0)=0
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (int(_actor_value(actor, "id")), row["id"]),
            ).fetchone()
            candidates.append(_cloud_image_row_payload(row, storage_row=dict(storage_row) if storage_row else None))
        return candidates

    def _list_history_image_candidates(conn, actor, *, limit=30):
        history = _list_generation_history(conn, actor=actor, limit=limit)
        candidates = []
        for item in history:
            result = item.get("result") if isinstance(item, dict) else {}
            images = result.get("images") if isinstance(result, dict) else []
            for index, image in enumerate(images if isinstance(images, list) else []):
                image_ref = _image_ref_payload((image or {}).get("image_ref"))
                if not image_ref:
                    continue
                candidates.append({
                    "source": "history",
                    "history_id": item.get("id"),
                    "batch_index": index,
                    "generation_mode": item.get("generation_mode") or "",
                    "created_at": item.get("created_at") or "",
                    "filename": image_ref["filename"],
                    "prompt": ((item.get("payload") or {}).get("prompt") or "")[:180],
                    "image_ref": image_ref,
                    "mime_type": (image or {}).get("mime_type") or "image/png",
                    "size_bytes": int((image or {}).get("size_bytes") or 0),
                })
        return candidates

    @app.route("/api/comfyui/input-image-candidates", methods=["GET"])
    @require_csrf
    def comfyui_input_image_candidates():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            history = _list_history_image_candidates(conn, actor, limit=30)
            cloud_drive = _list_cloud_drive_image_candidates(conn, actor, limit=80)
        finally:
            conn.close()
        return json_resp({"ok": True, "history": history, "cloud_drive": cloud_drive})

    @app.route("/api/comfyui/image-favorites", methods=["GET"])
    @require_csrf
    def comfyui_image_favorites_list():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            limit = max(1, min(200, int(request.args.get("limit") or 80)))
        except Exception:
            limit = 80
        conn = get_db()
        try:
            _ensure_comfyui_image_favorite_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM comfyui_image_favorites
                WHERE owner_user_id=?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (_actor_id(actor), limit),
            ).fetchall()
            favorites = [_favorite_row_payload(row) for row in rows]
        finally:
            conn.close()
        return json_resp({"ok": True, "favorites": favorites})

    @app.route("/api/comfyui/image-favorites", methods=["POST"])
    @require_csrf
    def comfyui_image_favorites_create():
        actor, err = _actor_or_401()
        if err:
            return err
        if request.files:
            data = dict(request.form or {})
            params = {}
            raw_params = data.get("params_json") or data.get("params") or ""
            if raw_params:
                try:
                    parsed = json.loads(raw_params)
                    if isinstance(parsed, dict):
                        params = parsed
                except Exception:
                    params = {}
            for key in ("prompt", "negative_prompt", "model", "vae", "sampler_name", "scheduler", "seed", "steps", "cfg", "width", "height", "generation_mode"):
                if data.get(key) not in (None, ""):
                    params[key] = data.get(key)
            payload, msg = _validate_image_upload(request.files.get("image"), label="收藏圖片")
            if msg:
                return json_resp({"ok": False, "msg": msg}), 400
            if not payload:
                return json_resp({"ok": False, "msg": "缺少要收藏的圖片"}), 400
            filename = payload["filename"]
            mime_type = payload["mime_type"]
            raw = payload["data"]
            conn = get_db()
            try:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                image = SimpleNamespace(data=raw, filename=filename, mime_type=mime_type)
                upload_result, storage_file, _album, msg = _save_fetched_image(
                    conn,
                    actor=actor,
                    data={
                        "display_name": data.get("title") or filename,
                        "virtual_path": f"/favorites/comfyui/{stamp}_{filename}",
                    },
                    image=image,
                )
                if msg:
                    conn.rollback()
                    return json_resp({"ok": False, "msg": msg}), 400
                fields = _favorite_fields_from_mapping(
                    {**data, "params": params},
                    source_type="upload",
                    cloud_file_id=upload_result["file_id"],
                    storage_file_id=(storage_file or {}).get("id"),
                    filename=filename,
                    mime_type=mime_type,
                    size_bytes=len(raw),
                    metadata={"metadata_source": "manual_upload"},
                )
                favorite = _insert_or_update_image_favorite(conn, actor, fields)
                conn.commit()
            finally:
                conn.close()
            audit("COMFYUI_IMAGE_FAVORITE_UPLOAD", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"favorite_id={favorite['id']}, file_id={favorite.get('cloud_file_id')}")
            return json_resp({"ok": True, "favorite": favorite, "msg": "已收藏上傳圖片"})

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        image_ref = _image_ref_payload(data.get("image_ref"))
        if not image_ref:
            return json_resp({"ok": False, "msg": "缺少可收藏的產圖 image_ref"}), 400

        conn = get_db()
        try:
            ref_row = _load_comfyui_image_ref_record(conn, actor=actor, image_ref=image_ref, prompt_id=data.get("prompt_id"))
        finally:
            conn.close()
        if not ref_row:
            audit("COMFYUI_IMAGE_REF_DENIED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(), detail=f"action=favorite,file={image_ref.get('filename', '-')}")
            return json_resp({"ok": False, "msg": "找不到可收藏的產圖預覽"}), 404

        binding = _comfyui_binding(actor, backend_url=(ref_row or {}).get("backend_url"))
        active_client = _client_for_url(binding.get("url"))
        try:
            fetched = active_client.fetch_image(image_ref)
            _assert_reasonable_image_size(fetched)
        except ComfyUIError as exc:
            return _json_error_from_comfy(exc, active_client)

        conn = get_db()
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            display_name = data.get("title") or fetched.filename or image_ref.get("filename") or "comfyui-favorite.png"
            upload_result, storage_file, _album, msg = _save_fetched_image(
                conn,
                actor=actor,
                data={
                    "display_name": display_name,
                    "virtual_path": f"/favorites/comfyui/{stamp}_{fetched.filename or image_ref.get('filename') or 'comfyui-favorite.png'}",
                },
                image=fetched,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            fields = _favorite_fields_from_mapping(
                data,
                source_type=data.get("source_type") or "generated",
                image_ref=image_ref,
                backend_url=(ref_row or {}).get("backend_url") or binding.get("url") or "",
                cloud_file_id=upload_result["file_id"],
                storage_file_id=(storage_file or {}).get("id"),
                filename=fetched.filename or image_ref.get("filename") or "comfyui-favorite.png",
                mime_type=fetched.mime_type,
                size_bytes=len(fetched.data),
                metadata={
                    "metadata_source": "comfyui_generation",
                    "prompt_id": data.get("prompt_id") or "",
                    "selected_image_index": data.get("selected_image_index"),
                    "output_label": data.get("output_label") or "",
                },
            )
            favorite = _insert_or_update_image_favorite(conn, actor, fields)
            conn.commit()
        finally:
            conn.close()
        audit("COMFYUI_IMAGE_FAVORITE_GENERATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"favorite_id={favorite['id']}, file_id={favorite.get('cloud_file_id')}")
        return json_resp({"ok": True, "favorite": favorite, "msg": "已收藏這張產圖"})

    @app.route("/api/comfyui/image-favorites/import-civitai", methods=["POST"])
    @require_csrf
    def comfyui_image_favorites_import_civitai():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        reference, msg = _parse_civitai_image_reference(data.get("url") or data.get("source_url"))
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        if not _fetch_json:
            return json_resp({"ok": False, "msg": "Civitai API helper 尚未載入"}), 500
        token = _configured_civitai_api_key() if callable(_configured_civitai_api_key) else ""
        headers = _civitai_headers(token) if callable(_civitai_headers) else {"User-Agent": "hackme_web-civitai-image-favorites/1.0", "Accept": "application/json"}
        api_base = _civitai_api_base_for_site(reference.get("source_site"))
        query = urlencode({"imageId": int(reference["image_id"]), "limit": 1, "withMeta": "true"})
        try:
            payload = _fetch_json(f"{api_base}/images?{query}", headers=headers, timeout=20)
        except Exception as exc:
            return json_resp({"ok": False, "msg": f"Civitai API 連線失敗：{str(exc)[:180]}"}), 502
        items = payload.get("items") if isinstance(payload, dict) else None
        image_entry = None
        if isinstance(items, list) and items:
            image_entry = next((item for item in items if int(item.get("id") or 0) == int(reference["image_id"])), None)
        elif isinstance(payload, dict) and int(payload.get("id") or 0) == int(reference["image_id"]):
            image_entry = payload
        if not isinstance(image_entry, dict):
            return json_resp({"ok": False, "msg": f"Civitai API 沒有回傳 imageId={reference['image_id']} 的圖片"}), 404
        version_ids = image_entry.get("modelVersionIds") if isinstance(image_entry.get("modelVersionIds"), list) else []
        version_resources = _fetch_civitai_model_version_resources(version_ids, api_base, headers) if version_ids else []
        fields, msg = _normalize_civitai_image_entry(image_entry, reference, version_resources=version_resources)
        if msg:
            return json_resp({"ok": False, "msg": msg}), 502
        fields["title"] = _safe_text(data.get("title"), 120) or fields["title"]
        fields["note"] = _safe_text(data.get("note"), 1200) or fields["note"]
        conn = get_db()
        try:
            favorite = _insert_or_update_image_favorite(conn, actor, fields)
            conn.commit()
        finally:
            conn.close()
        audit("COMFYUI_IMAGE_FAVORITE_CIVITAI_IMPORT", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"favorite_id={favorite['id']}, civitai_image_id={favorite.get('civitai_image_id')}")
        return json_resp({"ok": True, "favorite": favorite, "msg": "已從 Civitai API 匯入圖片收藏"})

    @app.route("/api/comfyui/image-favorites/<int:favorite_id>/preview", methods=["GET"])
    @require_csrf
    def comfyui_image_favorites_preview(favorite_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            _ensure_comfyui_image_favorite_schema(conn)
            row = conn.execute(
                "SELECT * FROM comfyui_image_favorites WHERE id=? AND owner_user_id=?",
                (int(favorite_id), _actor_id(actor)),
            ).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到圖片收藏"}), 404
            row = dict(row)
            cloud_file_id = str(row.get("cloud_file_id") or "").strip()
            if cloud_file_id:
                allowed, _reason, file_row = can_download_file(conn, actor=actor, file_id=cloud_file_id, action="preview")
                if allowed and file_row:
                    path = resolve_file_storage_path(storage_root, file_row)
                    if path.exists() and path.is_file():
                        mime_type = row.get("mime_type") or file_row["mime_type_plain_for_public"] or mimetypes.guess_type(path.name)[0] or "image/png"
                        download_name = row.get("filename") or file_row["original_filename_plain_for_public"] or path.name
                        return send_file(path, mimetype=mime_type, download_name=download_name)
            image_ref = _image_ref_payload(_parse_json_object(row.get("image_ref_json"), {}))
            ref_row = None
            if image_ref:
                ref_row = _load_comfyui_image_ref_record(conn, actor=actor, image_ref=image_ref)
        finally:
            conn.close()

        if image_ref and ref_row:
            active_client = _client_for_url(_comfyui_binding(actor, backend_url=(row.get("backend_url") or ref_row.get("backend_url"))).get("url"))
            try:
                image = active_client.fetch_image(image_ref)
                _assert_reasonable_image_size(image)
            except ComfyUIError as exc:
                return _json_error_from_comfy(exc, active_client)
            return send_file(
                BytesIO(image.data),
                mimetype=image.mime_type,
                download_name=image.filename or image_ref.get("filename") or "comfyui-favorite.png",
            )

        image_url = row.get("image_url") or ""
        if image_url and callable(_fetch_civitai_media):
            data, mime_type, msg = _fetch_civitai_media(image_url, max_bytes=MAX_COMFYUI_FETCH_IMAGE_BYTES)
            if msg:
                return json_resp({"ok": False, "msg": msg}), 502
            ext = mimetypes.guess_extension(mime_type or "") or Path(urlparse(image_url).path).suffix or ".jpg"
            filename = row.get("filename") or f"civitai_{row.get('civitai_image_id') or favorite_id}{ext}"
            return send_file(BytesIO(data), mimetype=mime_type or "image/jpeg", download_name=filename)
        return json_resp({"ok": False, "msg": "這筆收藏沒有可預覽的圖片來源"}), 404

    @app.route("/api/comfyui/image-favorites/<int:favorite_id>", methods=["DELETE"])
    @require_csrf
    def comfyui_image_favorites_delete(favorite_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            _ensure_comfyui_image_favorite_schema(conn)
            cur = conn.execute(
                "DELETE FROM comfyui_image_favorites WHERE id=? AND owner_user_id=?",
                (int(favorite_id), _actor_id(actor)),
            )
            if cur.rowcount <= 0:
                conn.rollback()
                return json_resp({"ok": False, "msg": "找不到圖片收藏"}), 404
            conn.commit()
        finally:
            conn.close()
        audit("COMFYUI_IMAGE_FAVORITE_DELETE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"favorite_id={favorite_id}")
        return json_resp({"ok": True, "msg": "已刪除圖片收藏"})

    @app.route("/api/comfyui/import-drive-image", methods=["POST"])
    @require_csrf
    def comfyui_import_drive_image():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        file_id = str(data.get("file_id") or "").strip()
        if not file_id:
            return json_resp({"ok": False, "msg": "缺少 file_id"}), 400
        conn = get_db()
        try:
            allowed, reason, row = can_download_file(conn, actor=actor, file_id=file_id, action="preview")
            if not row:
                return json_resp({"ok": False, "msg": "找不到雲端硬碟圖片"}), 404
            if not allowed:
                return json_resp({"ok": False, "msg": "沒有預覽權限或檔案尚未通過安全檢查", "reason": reason}), 403
            filename = row["original_filename_plain_for_public"] or "image.png"
            mime_type = (row["mime_type_plain_for_public"] or "").lower()
            if row["privacy_mode"] != "standard_plain":
                return json_resp({"ok": False, "msg": "目前只能匯入 standard_plain 圖片到 ComfyUI。"}), 409
            if mime_type not in COMFYUI_ALLOWED_IMAGE_MIME_TYPES or Path(filename).suffix.lower() not in COMFYUI_ALLOWED_IMAGE_EXTENSIONS:
                return json_resp({"ok": False, "msg": "只支援 PNG / JPG / WEBP 圖片"}), 415
            size_bytes = int(row["size_bytes"] or 0)
            if size_bytes <= 0 or size_bytes > MAX_COMFYUI_FETCH_IMAGE_BYTES:
                return json_resp({"ok": False, "msg": "圖片大小不適合匯入 ComfyUI"}), 413
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists() or not path.is_file():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            raw = path.read_bytes()
        finally:
            conn.close()
        backend_url = request.form.get("backend_url") or request.form.get("comfyui_backend_url") or ""
        binding = _comfyui_binding(actor, backend_url=backend_url)
        active_client = _client_for_url(binding["url"])
        try:
            image_ref = active_client.upload_image_bytes(raw, filename, image_type="input", overwrite=False)
        except ComfyUIError as exc:
            return _json_error_from_comfy(exc, active_client)
        conn = get_db()
        try:
            _register_comfyui_image_refs(conn, actor=actor, images=[{"image_ref": image_ref, "prompt_id": ""}], backend_url=binding.get("url"))
            conn.commit()
        finally:
            conn.close()
        audit("COMFYUI_IMPORT_DRIVE_IMAGE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file_id={file_id}, image={image_ref.get('filename')}")
        return json_resp({
            "ok": True,
            "image": {
                "image_ref": image_ref,
                "cloud_file_id": file_id,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(raw),
                "data_url": f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}",
            },
        })

    @app.route("/api/comfyui/import-uploaded-image", methods=["POST"])
    @require_csrf
    def comfyui_import_uploaded_image():
        actor, err = _actor_or_401()
        if err:
            return err
        payload, msg = _validate_image_upload(request.files.get("image"), label="模板圖片")
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        if not payload:
            return json_resp({"ok": False, "msg": "缺少要匯入的模板圖片"}), 400

        filename = payload["filename"]
        mime_type = payload["mime_type"]
        raw = payload["data"]
        backend_url = request.form.get("backend_url") or request.form.get("comfyui_backend_url") or ""
        binding = _comfyui_binding(actor, backend_url=backend_url)
        active_client = _client_for_url(binding["url"])
        conn = get_db()
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            image = SimpleNamespace(data=raw, filename=filename, mime_type=mime_type)
            upload_result, storage_file, _album, msg = _save_fetched_image(
                conn,
                actor=actor,
                data={
                    "display_name": filename,
                    "virtual_path": f"/input/comfyui/{stamp}_{filename}",
                },
                image=image,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            try:
                imported_ref = active_client.upload_image_bytes(
                    raw,
                    filename,
                    image_type="input",
                    overwrite=False,
                )
            except ComfyUIError as exc:
                conn.rollback()
                return _json_error_from_comfy(exc, active_client)
            _register_comfyui_image_refs(
                conn,
                actor=actor,
                images=[{"image_ref": imported_ref, "prompt_id": ""}],
                backend_url=binding.get("url"),
            )
            conn.commit()
        finally:
            conn.close()
        audit(
            "COMFYUI_IMPORT_UPLOADED_IMAGE",
            get_client_ip(),
            user=actor["username"],
            success=True,
            ua=get_ua(),
            detail=f"file_id={upload_result['file_id']}, image={imported_ref.get('filename')}",
        )
        return json_resp({
            "ok": True,
            "image": {
                "image_ref": imported_ref,
                "cloud_file_id": upload_result["file_id"],
                "storage_file_id": (storage_file or {}).get("id"),
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(raw),
                "data_url": f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}",
            },
        })

    @app.route("/api/comfyui/import-uploaded-video", methods=["POST"])
    @require_csrf
    def comfyui_import_uploaded_video():
        actor, err = _actor_or_401()
        if err:
            return err
        payload, msg = _validate_video_upload(request.files.get("video"), label="模板影片")
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        if not payload:
            return json_resp({"ok": False, "msg": "缺少要匯入的模板影片"}), 400

        filename = payload["filename"]
        mime_type = payload["mime_type"]
        raw = payload["data"]
        conn = get_db()
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            media = SimpleNamespace(data=raw, filename=filename, mime_type=mime_type)
            upload_result, storage_file, _album, msg = _save_fetched_image(
                conn,
                actor=actor,
                data={
                    "display_name": filename,
                    "virtual_path": f"/input/comfyui/{stamp}_{filename}",
                },
                image=media,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
        finally:
            conn.close()
        audit(
            "COMFYUI_IMPORT_UPLOADED_VIDEO",
            get_client_ip(),
            user=actor["username"],
            success=True,
            ua=get_ua(),
            detail=f"file_id={upload_result['file_id']}, filename={filename}",
        )
        return json_resp({
            "ok": True,
            "media": {
                "media_ref": {"filename": filename, "type": "input"},
                "cloud_file_id": upload_result["file_id"],
                "storage_file_id": (storage_file or {}).get("id"),
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(raw),
            },
        })

    @app.route("/api/comfyui/import-history-image", methods=["POST"])
    @require_csrf
    def comfyui_import_history_image():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        image_ref = _image_ref_payload(data.get("image_ref"))
        if not image_ref:
            return json_resp({"ok": False, "msg": "圖片引用不合法"}), 400
        conn = get_db()
        try:
            ref_row = _load_comfyui_image_ref_record(conn, actor=actor, image_ref=image_ref)
        finally:
            conn.close()
        if not ref_row:
            return json_resp({"ok": False, "msg": "無權讀取這張 ComfyUI 圖片"}), 403
        binding = _comfyui_binding(actor, backend_url=(ref_row or {}).get("backend_url"))
        active_client = _client_for_url(binding.get("url"))
        try:
            fetched = active_client.fetch_image(image_ref)
            _assert_reasonable_image_size(fetched)
        except ComfyUIError as exc:
            return _json_error_from_comfy(exc, active_client)
        conn = get_db()
        try:
            upload_result, storage_file, _album, msg = _save_fetched_image(
                conn,
                actor=actor,
                data={
                    "display_name": fetched.filename or image_ref.get("filename") or "comfyui-history.png",
                    "virtual_path": f"/output/inputs/{fetched.filename or image_ref.get('filename') or 'comfyui-history.png'}",
                },
                image=fetched,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            try:
                imported_ref = active_client.upload_image_bytes(
                    fetched.data,
                    fetched.filename or image_ref.get("filename") or "comfyui-history.png",
                    image_type="input",
                    overwrite=False,
                )
            except ComfyUIError as exc:
                conn.rollback()
                return _json_error_from_comfy(exc, active_client)
            _register_comfyui_image_refs(
                conn,
                actor=actor,
                images=[{"image_ref": imported_ref, "prompt_id": ""}],
                backend_url=binding.get("url"),
            )
            conn.commit()
        finally:
            conn.close()
        audit(
            "COMFYUI_IMPORT_HISTORY_IMAGE",
            get_client_ip(),
            user=actor["username"],
            success=True,
            ua=get_ua(),
            detail=f"source={image_ref.get('filename')}, file_id={upload_result['file_id']}, image={imported_ref.get('filename')}",
        )
        return json_resp({
            "ok": True,
            "image": {
                "image_ref": imported_ref,
                "cloud_file_id": upload_result["file_id"],
                "storage_file_id": (storage_file or {}).get("id"),
                "filename": fetched.filename or image_ref.get("filename") or "comfyui-history.png",
                "mime_type": fetched.mime_type,
                "size_bytes": len(fetched.data),
                "data_url": f"data:{fetched.mime_type};base64,{base64.b64encode(fetched.data).decode('ascii')}",
            },
        })

    @app.route("/api/comfyui/image-preview", methods=["POST"])
    @require_csrf
    def comfyui_image_preview():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        image_ref = _image_ref_payload(data.get("image_ref"))
        if not image_ref:
            return json_resp({"ok": False, "msg": "圖片引用不合法"}), 400
        conn = get_db()
        try:
            ref_row = _load_comfyui_image_ref_record(conn, actor=actor, image_ref=image_ref)
        finally:
            conn.close()
        if not ref_row:
            return json_resp({"ok": False, "msg": "無權讀取這張 ComfyUI 圖片"}), 403
        active_client = _client_for_url(_comfyui_binding(actor, backend_url=(ref_row or {}).get("backend_url")).get("url"))
        try:
            image = active_client.fetch_image(image_ref)
            _assert_reasonable_image_size(image)
        except ComfyUIError as exc:
            return _json_error_from_comfy(exc, active_client)
        return json_resp({
            "ok": True,
            "image": {
                "image_ref": image_ref,
                "mime_type": image.mime_type,
                "size_bytes": len(image.data),
                "data_url": f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}",
            },
        })

    @app.route("/api/comfyui/interrupt", methods=["POST"])
    @require_csrf
    def comfyui_interrupt():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True, silent=True)
        except TypeError:
            data = None
        data = data if isinstance(data, dict) else {}
        allowed, reason, summary = _interrupt_policy(actor)
        if not allowed:
            audit(
                "COMFYUI_INTERRUPT_SKIPPED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"reason={reason}, summary={summary}",
            )
            msg = "已中斷本頁等待；未送出 ComfyUI 全域中斷，避免影響其他使用者的產圖。"
            if reason == "no_owned_generation":
                msg = "目前沒有偵測到你的後端產圖任務；已中斷本頁等待。"
            return json_resp({
                "ok": True,
                "msg": msg,
                "interrupt": {
                    "interrupted": False,
                    "backend_interrupted": False,
                    "reason": reason,
                    **summary,
                },
            })
        active_client = _client(actor)
        if _is_root(actor):
            own_active = [
                item for item in _active_generation_snapshot()
                if int(item.get("user_id") or 0) == int(_generation_owner_id(actor) or 0)
            ]
            own_backends = {
                _normalize_comfyui_backend_url(item.get("backend_url"))
                for item in own_active
                if _normalize_comfyui_backend_url(item.get("backend_url"))
            }
            if len(own_backends) == 1:
                active_client = _client_for_url(next(iter(own_backends)))
        try:
            if not hasattr(active_client, "interrupt"):
                return json_resp({"ok": False, "msg": "ComfyUI 中斷產圖不支援"}), 501
            try:
                signature = inspect.signature(active_client.interrupt)
                accepts_timeout = (
                    "timeout_seconds" in signature.parameters
                    or any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
                )
            except (TypeError, ValueError):
                accepts_timeout = True
            if accepts_timeout:
                result = active_client.interrupt(timeout_seconds=COMFYUI_INTERRUPT_TIMEOUT_SECONDS)
            else:
                result = active_client.interrupt()
        except ComfyUIError as exc:
            audit("COMFYUI_INTERRUPT_ERROR", get_client_ip(), user=actor["username"], success=False, ua=get_ua(), detail=str(exc)[:180])
            return _json_error_from_comfy(exc, active_client)
        payload = result if isinstance(result, dict) else {}
        payload.setdefault("interrupted", True)
        payload["backend_interrupted"] = True
        payload["reason"] = reason
        payload.update(summary)
        audit("COMFYUI_INTERRUPT", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"interrupt requested, reason={reason}, summary={summary}")
        return json_resp({"ok": True, "msg": "已送出中斷產圖請求", "interrupt": payload})

    @app.route("/api/comfyui/save", methods=["POST"])
    @require_csrf
    def comfyui_save():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        image_ref = data.get("image_ref")
        if not isinstance(image_ref, dict):
            return json_resp({"ok": False, "msg": "缺少 image_ref"}), 400
        conn = get_db()
        try:
            ref_row = _load_comfyui_image_ref_record(conn, actor=actor, image_ref=image_ref)
            if not ref_row:
                audit("COMFYUI_IMAGE_REF_DENIED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(), detail=f"action=save,file={image_ref.get('filename', '-')}")
                return json_resp({"ok": False, "msg": "找不到可存取的產圖預覽"}), 404
            active_client = _client_for_url(_comfyui_binding(actor, backend_url=ref_row.get("backend_url")).get("url"))
            try:
                image = active_client.fetch_image(image_ref)
                _assert_reasonable_image_size(image)
            except ComfyUIError as exc:
                return _json_error_from_comfy(exc, active_client)
            upload_result, storage_file, album, msg = _save_fetched_image(conn, actor=actor, data=data, image=image)
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("COMFYUI_SAVE_TO_DRIVE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file_id={upload_result['file_id']}, storage_file_id={storage_file['id']}")
            return json_resp({"ok": True, "file": upload_result, "storage_file": storage_file, "album": album})
        finally:
            conn.close()

    @app.route("/api/comfyui/discard", methods=["POST"])
    @require_csrf
    def comfyui_discard():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        image_ref = data.get("image_ref")
        if not isinstance(image_ref, dict):
            return json_resp({"ok": False, "msg": "缺少 image_ref"}), 400
        conn = get_db()
        try:
            ref_row = _load_comfyui_image_ref_record(conn, actor=actor, image_ref=image_ref, prompt_id=data.get("prompt_id"))
            if not ref_row:
                audit("COMFYUI_IMAGE_REF_DENIED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(), detail=f"action=discard,file={image_ref.get('filename', '-')}")
                return json_resp({"ok": False, "msg": "找不到可丟棄的產圖預覽"}), 404
            conn.commit()
        finally:
            conn.close()
        image_binding = _comfyui_binding(actor, backend_url=(ref_row or {}).get("backend_url"))
        if image_binding["connection_mode"] != "local":
            result = {
                "file_deleted": False,
                "file_missing": False,
                "file_delete_supported": False,
                "history_deleted": False,
                "remote_preview_only": True,
            }
            audit("COMFYUI_DISCARD_REMOTE_PREVIEW_ONLY", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file={image_ref.get('filename')}")
            return json_resp({
                "ok": True,
                "msg": "已移除網頁上的預覽；遠端 ComfyUI API 不支援刪除 output 原始檔。",
                "discard": result,
                "warning": "source_file_not_deleted",
            })
        active_client = _client_for_url(image_binding["url"])
        try:
            if not hasattr(active_client, "discard_image"):
                return json_resp({"ok": False, "msg": "ComfyUI 原始檔刪除不支援"}), 501
            result = active_client.discard_image(
                image_ref,
                prompt_id=data.get("prompt_id"),
                local_base_dir=str(_configured_comfyui_project_dir() or _configured_comfyui_base_dir() or ""),
                allow_api_delete=False,
            )
        except ComfyUIError as exc:
            audit("COMFYUI_DISCARD_ERROR", get_client_ip(), user=actor["username"], success=False, ua=get_ua(), detail=str(exc)[:180])
            return _json_error_from_comfy(exc, active_client)
        if not (result.get("file_deleted") or result.get("file_missing")):
            msg = "已丟棄前端預覽；ComfyUI 未提供刪除 output 檔案端點，原始檔可能仍留在 ComfyUI output。若要同步刪原檔，請設定 COMFYUI_OUTPUT_DIR 或 COMFYUI_BASE_DIR。"
            audit("COMFYUI_DISCARD_UNSUPPORTED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=str(result)[:180])
            return json_resp({"ok": True, "msg": msg, "discard": result, "warning": "source_file_not_deleted"})
        audit("COMFYUI_DISCARD", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file={image_ref.get('filename')}, result={result}")
        return json_resp({"ok": True, "msg": "已丟棄預覽並刪除 ComfyUI 原始檔", "discard": result})

    @app.route("/api/comfyui/share", methods=["POST"])
    @require_csrf
    def comfyui_share():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        image_ref = data.get("image_ref")
        conn = get_db()
        try:
            existing = _existing_saved_image(conn, actor=actor, data=data)
            if existing:
                upload_result, storage_file, album, msg = existing
                if msg:
                    conn.rollback()
                    return json_resp({"ok": False, "msg": msg}), 400
            else:
                if not isinstance(image_ref, dict):
                    return json_resp({"ok": False, "msg": "缺少 image_ref"}), 400
                ref_row = _load_comfyui_image_ref_record(conn, actor=actor, image_ref=image_ref)
                if not ref_row:
                    audit("COMFYUI_IMAGE_REF_DENIED", get_client_ip(), user=actor["username"], success=False, ua=get_ua(), detail=f"action=share,file={image_ref.get('filename', '-')}")
                    conn.rollback()
                    return json_resp({"ok": False, "msg": "找不到可分享的產圖預覽"}), 404
                active_client = _client_for_url(_comfyui_binding(actor, backend_url=ref_row.get("backend_url")).get("url"))
                try:
                    image = active_client.fetch_image(image_ref)
                    _assert_reasonable_image_size(image)
                except ComfyUIError as exc:
                    return _json_error_from_comfy(exc, active_client)
                upload_result, storage_file, album, msg = _save_fetched_image(conn, actor=actor, data=data, image=image)
                if msg:
                    conn.rollback()
                    return json_resp({"ok": False, "msg": msg}), 400
            board = _find_or_create_comfyui_board(conn, actor)
            title = _safe_text(data.get("title"), 120) or "ComfyUI 產圖分享"
            content = _compose_comfyui_share_content(
                data,
                file_id=upload_result["file_id"],
                storage_file=storage_file or {},
            )
            if not content.strip():
                conn.rollback()
                return json_resp({"ok": False, "msg": "分享內容不可為空"}), 400
            level = _actor_value(actor, "effective_level") or _actor_value(actor, "base_level") or _actor_value(actor, "member_level") or "normal"
            role = _actor_value(actor, "role", "user")
            status = "pending" if role == "user" and level == "newbie" else "approved"
            now = datetime.now().isoformat()
            cur = conn.execute(
                """
                INSERT INTO forum_threads (
                    board_id, title, content, status, post_type, author_user_id,
                    author_username, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'normal', ?, ?, ?, ?)
                """,
                (board["id"], title, content, status, int(_actor_value(actor, "id")), _actor_value(actor, "username"), now, now),
            )
            thread_id = cur.lastrowid
            conn.execute("UPDATE forum_boards SET last_activity_at=?, updated_at=? WHERE id=?", (now, now, board["id"]))
            attached, msg = attach_existing_file(
                conn,
                actor=actor,
                file_id=upload_result["file_id"],
                context_type="forum_thread",
                context_id=thread_id,
                grant_role="user",
                can_preview=True,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit(
                "COMFYUI_SHARE_TO_COMMUNITY",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"thread_id={thread_id}, file_id={upload_result['file_id']}, board_id={board['id']}",
            )
            return json_resp({
                "ok": True,
                "msg": "已分享到 ComfyUI 專區" if status == "approved" else "已送出分享，待審核後公開",
                "thread": {"id": thread_id, "board_id": board["id"], "title": title, "status": status},
                "file": upload_result,
                "storage_file": storage_file,
                "album": album,
                "attachment": attached,
            })
        finally:
            conn.close()
