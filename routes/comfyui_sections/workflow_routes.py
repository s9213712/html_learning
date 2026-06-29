import json
import secrets
from datetime import datetime
from pathlib import Path

from flask import send_file

from services.comfyui.template import errors as template_errors
from services.comfyui.template.capability import rewrite_workflow_model_inputs_to_local_options
from services.comfyui.template.gguf_workflow import (
    GgufWorkflowError,
    apply_gguf_workflow_profile,
    is_gguf_workflow_id,
)
from services.comfyui.template.multi_compare import (
    MultiCompareWorkflowError,
    expand_multi_compare_workflow,
    is_multi_compare_workflow_id,
)
from services.comfyui.template.sdxl_refiner import (
    SdxlRefinerWorkflowError,
    apply_sdxl_refiner_option,
    is_sdxl_refiner_workflow_id,
)
from services.comfyui.template.upscale_breakpoint import (
    UpscaleBreakpointError,
    apply_upscale_breakpoint,
    is_upscale_breakpoint_workflow_id,
)
from services.comfyui.template.run_gate import (
    RunGateFailure,
    run_workflow_through_gates,
)
from services.comfyui.workflow.compat import apply_workflow_compatibility_fixes
from services.platform.settings import is_feature_enabled


_OFFICIAL_TEMPLATE_MEDIA_DIR = Path(__file__).resolve().parents[2] / "workflows" / "comfyui" / "assets"
_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX = "official-template-media:"
_OFFICIAL_TEMPLATE_MEDIA_ALIASES = {
    # Keep a small alias table for historical renamed assets. The normal path
    # is exact basename lookup under workflows/comfyui/assets.
    "image_qwen_image_edit_2509_input_image.png": "image_qwen_image_edit_2509_input_image.png",
}
_OFFICIAL_TEMPLATE_MEDIA_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}


def _is_qwen_image_edit_2509_family(system_bundle_id):
    value = str(system_bundle_id or "").strip()
    return value == "origin_qwen_image_edit_2509" or value.startswith("origin_qwen_image_edit_2509_")


def _apply_qwen_2512_controlnet_pose_mode(workflow_json, body):
    system_bundle_id = str((body or {}).get("system_bundle_id") or "").strip()
    if system_bundle_id != "origin_qwen_image_controlnet_2512":
        return workflow_json, False
    control_type = str((body or {}).get("controlnet_type") or "").strip().lower()
    preprocessor = str((body or {}).get("controlnet_preprocessor") or "").strip().lower()
    if control_type not in {"pose", "openpose", "sdpose"} and preprocessor not in {"none", "passthrough", "pose", "openpose", "sdpose"}:
        return workflow_json, False
    workflow = json.loads(json.dumps(workflow_json or {}))
    apply_node = workflow.get("131") if isinstance(workflow.get("131"), dict) else {}
    apply_inputs = apply_node.get("inputs") if isinstance(apply_node.get("inputs"), dict) else {}
    if not apply_inputs or apply_inputs.get("image") == ["123", 0]:
        return workflow, False
    apply_inputs["image"] = ["123", 0]
    return workflow, True


def _official_template_media_path(name):
    clean_name = Path(str(name or "").strip()).name
    if not clean_name:
        return None
    lookup_names = []
    alias = _OFFICIAL_TEMPLATE_MEDIA_ALIASES.get(clean_name)
    if alias:
        lookup_names.append(Path(alias).name)
    lookup_names.append(clean_name)
    seen_names = set()
    for lookup_name in lookup_names:
        if lookup_name in seen_names:
            continue
        seen_names.add(lookup_name)
        direct_path = _OFFICIAL_TEMPLATE_MEDIA_DIR / lookup_name
        if direct_path.is_file():
            return direct_path
        if not _OFFICIAL_TEMPLATE_MEDIA_DIR.is_dir():
            continue
        matches = sorted(
            path
            for path in _OFFICIAL_TEMPLATE_MEDIA_DIR.rglob("*")
            if path.is_file() and path.name == lookup_name
        )
        if len(matches) == 1:
            return matches[0]
    return None


def _official_template_media_row(actor, media_name):
    media_path = _official_template_media_path(media_name)
    if media_path is None:
        return None
    ext = media_path.suffix.lower()
    mime_type = _OFFICIAL_TEMPLATE_MEDIA_MIME_BY_EXT.get(ext)
    if not mime_type:
        return None
    try:
        actor_id = int(actor.get("id") if hasattr(actor, "get") else actor["id"])
    except Exception:
        actor_id = 0
    if actor_id <= 0:
        return None
    try:
        size_bytes = int(media_path.stat().st_size)
    except OSError:
        return None
    if size_bytes <= 0:
        return None
    clean_name = media_path.name
    return {
        "id": f"{_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX}{clean_name}",
        "owner_user_id": actor_id,
        "storage_path": str(media_path),
        "privacy_mode": "standard_plain",
        "risk_level": "low",
        "scan_status": "clean",
        "original_filename_plain_for_public": clean_name,
        "mime_type_plain_for_public": mime_type,
        "size_bytes": size_bytes,
        "deleted_at": None,
    }


def _workflow_template_fetch_file_row(conn, cloud_file_id, *, actor):
    cloud_file_id = str(cloud_file_id or "").strip()
    if cloud_file_id.startswith(_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX):
        return _official_template_media_row(
            actor,
            cloud_file_id[len(_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX) :],
        )
    row = conn.execute(
        """
        SELECT id, owner_user_id, storage_path, privacy_mode, risk_level,
               scan_status, original_filename_plain_for_public,
               mime_type_plain_for_public, size_bytes, deleted_at
        FROM uploaded_files
        WHERE id = ? AND deleted_at IS NULL
        """,
        (cloud_file_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _official_template_media_mime_type(path):
    return _OFFICIAL_TEMPLATE_MEDIA_MIME_BY_EXT.get(Path(path).suffix.lower()) or "application/octet-stream"


def _official_template_media_fallback(file_row, *, raw_path):
    if not raw_path or Path(raw_path).is_absolute():
        return None
    if hasattr(file_row, "get"):
        owner_user_id = file_row.get("owner_user_id")
        original_name = str(file_row.get("original_filename_plain_for_public") or "").strip()
    else:
        try:
            owner_user_id = file_row["owner_user_id"]
        except Exception:
            owner_user_id = None
        try:
            original_name = str(file_row["original_filename_plain_for_public"] or "").strip()
        except Exception:
            original_name = ""
    try:
        owner_id = int(owner_user_id or 0)
    except (TypeError, ValueError):
        owner_id = 0
    if owner_id != 1 or not raw_path.startswith("users/1/"):
        return None

    names = [Path(raw_path).name]
    if original_name:
        names.append(Path(original_name).name)
    for name in names:
        fallback_path = _official_template_media_path(name)
        if fallback_path is not None:
            return fallback_path
    return None


def _resolve_upload_source_path(file_row, *, storage_root=None, resolve_file_storage_path=None):
    storage_path = file_row.get("storage_path") if hasattr(file_row, "get") else file_row["storage_path"]
    raw_path = str(storage_path or "").strip()
    source_path = Path(raw_path)
    candidates = []
    if source_path.is_absolute():
        candidates.append(source_path)
    else:
        if storage_root and resolve_file_storage_path:
            try:
                candidates.append(resolve_file_storage_path(storage_root, file_row))
            except Exception:
                pass
        elif storage_root:
            candidates.append(Path(storage_root) / raw_path)
        candidates.append(Path.cwd() / "runtime" / "storage" / raw_path)
        candidates.append(Path.cwd() / raw_path)

    seen = set()
    for candidate in candidates:
        candidate = Path(candidate)
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file():
            return candidate

    fallback = _official_template_media_fallback(file_row, raw_path=raw_path)
    if fallback is not None:
        return fallback

    attempted = ", ".join(str(path) for path in candidates) or raw_path or "<empty>"
    raise FileNotFoundError(f"雲端檔案實體不存在：{raw_path}；已嘗試 {attempted}")


def _default_upload_callback(active_client, *, storage_root=None, resolve_file_storage_path=None):
    """Return an UploadCallback that pushes bytes into ComfyUI input/<run_id>/.

    Upload errors are intentionally surfaced to Gate 5. Pretending an upload
    succeeded leaves media loader nodes pointing at files ComfyUI never received.
    """
    def _cb(*, file_row, target_filename, run_id):
        if active_client is None:
            raise RuntimeError("尚未連線到 ComfyUI，無法上傳 workflow 圖片")
        try:
            source_path = _resolve_upload_source_path(
                file_row,
                storage_root=storage_root,
                resolve_file_storage_path=resolve_file_storage_path,
            )
            with open(source_path, "rb") as fh:
                data = fh.read()
        except Exception as exc:
            raise RuntimeError(f"讀取雲端媒體失敗：{exc}") from exc
        if not data:
            raise RuntimeError("雲端媒體內容為空，無法上傳到 ComfyUI")
        if hasattr(active_client, "upload_image_bytes"):
            return active_client.upload_image_bytes(
                data,
                target_filename,
                image_type="input",
                overwrite=False,
                subfolder=run_id,
            )
        try:
            from services.comfyui.files import upload_image_bytes
            from services.comfyui.client import ComfyUIError
        except Exception as exc:  # pragma: no cover - defensive import guard
            raise RuntimeError(f"ComfyUI 上傳模組載入失敗：{exc}") from exc
        return upload_image_bytes(
            active_client,
            data,
            target_filename,
            image_type="input",
            overwrite=False,
            subfolder=run_id,
            error_cls=ComfyUIError,
        )
    return _cb


def register_comfyui_workflow_routes(app, ctx):
    actor_or_401 = ctx["actor_or_401"]
    root_or_403 = ctx["root_or_403"]
    actor_value = ctx["actor_value"]
    json_resp = ctx["json_resp"]
    require_csrf = ctx["require_csrf"]
    require_csrf_safe = ctx["require_csrf_safe"]
    get_db = ctx["get_db"]
    get_client_ip = ctx["get_client_ip"]
    get_ua = ctx["get_ua"]
    audit = ctx["audit"]
    comfyui_binding = ctx["comfyui_binding"]
    client_for_url = ctx["client_for_url"]
    load_workflow_preset = ctx["load_workflow_preset"]
    workflow_preset_summary = ctx["workflow_preset_summary"]
    workflow_manifest_for_row = ctx.get("workflow_manifest_for_row")
    parse_json_field = ctx["parse_json_field"]
    extract_workflow_payload = ctx["extract_workflow_payload"]
    normalize_workflow_default_params = ctx["normalize_workflow_default_params"]
    upsert_workflow_preset = ctx["upsert_workflow_preset"]
    load_workflow_preset_row = ctx["load_workflow_preset_row"]
    WorkflowValidationError = ctx["WorkflowValidationError"]
    list_workflow_presets = ctx["list_workflow_presets"]
    workflow_dependency_status = ctx["workflow_dependency_status"]
    list_workflow_runs = ctx["list_workflow_runs"]
    normalize_generation_payload = ctx["normalize_generation_payload"]
    validate_generation_capabilities = ctx["validate_generation_capabilities"]
    sanitize_workflow_json = ctx["sanitize_workflow_json"]
    workflow_json_to_pretty_text = ctx["workflow_json_to_pretty_text"]
    analyze_workflow_json = ctx["analyze_workflow_json"]
    build_ui_schema = ctx["build_ui_schema"]
    check_workflow_capability = ctx["check_workflow_capability"]
    assert_workflow_dependencies_or_error = ctx["assert_workflow_dependencies_or_error"]
    create_workflow_run = ctx["create_workflow_run"]
    create_generation_job = ctx["create_generation_job"]
    capture_request_audit_meta = ctx["capture_request_audit_meta"]
    run_comfyui_workflow_preset_job = ctx["run_comfyui_workflow_preset_job"]
    comfyui_paid_api_policy = ctx.get("comfyui_paid_api_policy")
    DEFAULT_GENERATION_TIMEOUT_SECONDS = ctx["DEFAULT_GENERATION_TIMEOUT_SECONDS"]
    safe_text = ctx["safe_text"]
    threading = ctx["threading"]
    resolve_file_storage_path = ctx.get("resolve_file_storage_path")
    storage_root = ctx.get("storage_root")

    def _apply_legacy_workflow_user_inputs(workflow_json, user_inputs):
        if not isinstance(workflow_json, dict) or not isinstance(user_inputs, dict):
            return workflow_json
        patched = json.loads(json.dumps(workflow_json))
        for node_id, patch in user_inputs.items():
            if not isinstance(patch, dict):
                continue
            node = patched.get(str(node_id))
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for input_name, value in patch.items():
                key = str(input_name)
                if key not in inputs or isinstance(inputs.get(key), list):
                    continue
                inputs[key] = value
        return patched

    def _apply_qwen_edit_reference_policy(workflow_json, *, system_bundle_id, image_field_assignments):
        if not _is_qwen_image_edit_2509_family(system_bundle_id):
            return workflow_json, False
        if not isinstance(workflow_json, dict):
            return workflow_json, False
        prompt_node = workflow_json.get("494")
        prompt_inputs = prompt_node.get("inputs") if isinstance(prompt_node, dict) else None
        if not isinstance(prompt_inputs, dict):
            return workflow_json, False
        has_reference_image = "79" in {str(key) for key in (image_field_assignments or {}).keys()}
        if has_reference_image:
            if prompt_inputs.get("image2") == ["79", 0]:
                return workflow_json, False
            patched = json.loads(json.dumps(workflow_json))
            patched["494"].setdefault("inputs", {})["image2"] = ["79", 0]
            return patched, True
        if "image2" not in prompt_inputs and "79" not in workflow_json:
            return workflow_json, False
        patched = json.loads(json.dumps(workflow_json))
        patched["494"].setdefault("inputs", {}).pop("image2", None)
        patched.pop("79", None)
        return patched, True

    def _workflow_request_int(value, fallback, minimum, maximum):
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            parsed = fallback
        return max(minimum, min(maximum, parsed))

    def _normalize_workflow_seed_after_generate(value):
        mode = str(value or "random").strip().lower()
        return mode if mode in {"random", "fixed", "increment", "decrement"} else "random"

    def _randomize_workflow_seed_inputs(workflow_json, user_inputs):
        if not isinstance(workflow_json, dict):
            return workflow_json, user_inputs, None
        patched_inputs = {
            str(node_id): dict(patch)
            for node_id, patch in (user_inputs or {}).items()
            if isinstance(patch, dict)
        }
        seed = secrets.randbits(32)
        changed = False
        for node_id, node in workflow_json.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type") or "")
            if class_type not in {"KSampler", "KSamplerAdvanced"}:
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            seed_keys = []
            if "noise_seed" in inputs or class_type == "KSamplerAdvanced":
                seed_keys.append("noise_seed")
            if "seed" in inputs or class_type == "KSampler":
                seed_keys.append("seed")
            for key in seed_keys:
                input_patch = patched_inputs.get(str(node_id), {})
                if key not in inputs and key not in input_patch:
                    continue
                patched_inputs.setdefault(str(node_id), {})[key] = seed
                changed = True
        return workflow_json, patched_inputs, seed if changed else None

    def _normalize_workflow_vae_name(value):
        text = str(value or "").strip().replace("\\", "/")
        if not text or text == "__checkpoint_builtin__":
            return ""
        parts = [part for part in text.split("/") if part]
        if text.startswith("/") or "\x00" in text or not parts or any(part == ".." for part in parts):
            return None
        if any(ch in text for ch in "\r\n<>|?*"):
            return None
        return text[:240]

    def _apply_workflow_vae_override(workflow_json, vae_name):
        if not vae_name or not isinstance(workflow_json, dict):
            return workflow_json, False
        patched = json.loads(json.dumps(workflow_json))
        loader_id = ""
        changed = False
        for node_id, node in patched.items():
            if not isinstance(node, dict):
                continue
            if str(node.get("class_type") or "") != "VAELoader":
                continue
            inputs = node.setdefault("inputs", {})
            if not isinstance(inputs, dict):
                inputs = {}
                node["inputs"] = inputs
            if inputs.get("vae_name") != vae_name:
                changed = True
            inputs["vae_name"] = vae_name
            loader_id = str(node_id)
            break
        has_vae_consumer = False
        for node in patched.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            for input_name, value in inputs.items():
                if str(input_name) == "vae" and isinstance(value, list):
                    has_vae_consumer = True
                    break
            if has_vae_consumer:
                break
        if not has_vae_consumer:
            return workflow_json, False
        if not loader_id:
            next_id = 90000
            while str(next_id) in patched:
                next_id += 1
            loader_id = str(next_id)
            patched[loader_id] = {
                "class_type": "VAELoader",
                "inputs": {"vae_name": vae_name},
                "_meta": {"title": "使用者選擇 VAE"},
            }
            changed = True
        for node in patched.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            for input_name, value in list(inputs.items()):
                if str(input_name) == "vae" and isinstance(value, list):
                    if value != [loader_id, 0]:
                        inputs[input_name] = [loader_id, 0]
                        changed = True
        return patched, changed

    def _workflow_snapshot_params(default_params, workflow_json):
        params = dict(default_params or {})
        if not isinstance(workflow_json, dict):
            return params
        prompts = []
        negatives = []
        checkpoint_names = []
        unet_names = []
        vae_names = []
        unet_loader_types = {"UNETLoader", "UNetLoader", "UnetLoader", "UnetLoaderGGUF", "UnetLoaderGGUFAdvanced"}
        for node in workflow_json.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            class_type = str(node.get("class_type") or "")
            if class_type == "CheckpointLoaderSimple" and inputs.get("ckpt_name"):
                checkpoint_names.append(str(inputs.get("ckpt_name") or ""))
            if class_type in unet_loader_types and inputs.get("unet_name"):
                unet_names.append(str(inputs.get("unet_name") or ""))
            if class_type == "VAELoader" and inputs.get("vae_name"):
                vae_names.append(str(inputs.get("vae_name") or ""))
            if class_type in {"KSampler", "KSamplerAdvanced"}:
                if inputs.get("noise_seed") is not None:
                    params["seed"] = inputs.get("noise_seed")
                elif inputs.get("seed") is not None:
                    params["seed"] = inputs.get("seed")
                for src, dst in (("steps", "steps"), ("cfg", "cfg"), ("sampler_name", "sampler_name"), ("scheduler", "scheduler")):
                    if inputs.get(src) is not None:
                        params[dst] = inputs.get(src)
            if class_type == "EmptyLatentImage":
                for key in ("width", "height", "batch_size"):
                    if inputs.get(key) is not None:
                        params[key] = inputs.get(key)
            if class_type in {"CLIPTextEncode", "CLIPTextEncodeFlux"}:
                text = str(inputs.get("text") or "").strip()
                if not text:
                    continue
                meta_title = ""
                if isinstance(node.get("_meta"), dict):
                    meta_title = str(node.get("_meta", {}).get("title") or "")
                haystack = f"{meta_title} {text}".lower()
                if any(marker in haystack for marker in ("negative", "負", "low quality", "worst quality", "watermark")):
                    negatives.append(text)
                else:
                    prompts.append(text)
        checkpoint_names = [name for name in checkpoint_names if name]
        unet_names = [name for name in unet_names if name]
        candidates = list(checkpoint_names) + list(unet_names)
        current_model = str(params.get("model") or params.get("checkpoint") or params.get("diffusion_model") or "").strip()
        if candidates:
            current_normalized = current_model.replace("\\", "/").strip().lower()
            current_base = current_normalized.rsplit("/", 1)[-1]
            selected_model = ""
            for candidate in candidates:
                candidate_normalized = str(candidate).replace("\\", "/").strip().lower()
                candidate_base = candidate_normalized.rsplit("/", 1)[-1]
                if candidate_normalized == current_normalized or candidate_base == current_base:
                    selected_model = candidate
                    break
            if not selected_model:
                selected_model = candidates[0]
            params["model"] = selected_model
            if selected_model in checkpoint_names:
                params["checkpoint"] = selected_model
        vae_names = [name for name in vae_names if name]
        if vae_names:
            params["vae"] = vae_names[0]
        if prompts:
            params["prompt"] = prompts[0]
        if negatives:
            params["negative_prompt"] = negatives[0]
        return params

    def _workflow_output_kinds(workflow_json):
        output_class_kinds = {
            "SaveImage": "image",
            "PreviewImage": "image",
            "MaskPreview": "image",
            "SaveVideo": "video",
            "VHS_VideoCombine": "video",
            "SaveAudio": "audio",
            "SaveAudioMP3": "audio",
        }
        found = {
            output_class_kinds.get(str((node or {}).get("class_type") or "").strip())
            for node in (workflow_json or {}).values()
            if isinstance(node, dict)
        }
        classes = {
            str((node or {}).get("class_type") or "").strip()
            for node in (workflow_json or {}).values()
            if isinstance(node, dict)
        }
        if found and "video" in found and "SaveImage" not in classes:
            found.discard("image")
        output_kinds = [kind for kind in ("image", "video", "audio") if kind in found]
        if not output_kinds:
            output_kinds.append("image")
        return output_kinds

    def _workflow_validation_stage(exc):
        message = str(exc or "")
        if any(token in message for token in ("絕對路徑", "外部 URL", "命令片段", "路徑穿越", "敏感路徑", "不允許的節點")):
            return "sanitize"
        return "schema_validation"

    def _decode_workflow_jsonish(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise WorkflowValidationError("workflow JSON 格式不正確") from exc
        return value

    def _extract_layout_import_data(data):
        workflow_candidate = data.get("workflow_json") if "workflow_json" in data else data.get("workflow")
        if workflow_candidate in (None, ""):
            raise WorkflowValidationError("請提供 workflow JSON")
        decoded = _decode_workflow_jsonish(workflow_candidate)
        wrapper = decoded
        if isinstance(decoded, dict) and isinstance(decoded.get("workflow_preset_json"), dict):
            wrapper = decoded.get("workflow_preset_json")
        if isinstance(wrapper, dict) and "workflow_json" in wrapper and not all(
            isinstance(value, dict) and "class_type" in value for value in wrapper.values()
        ):
            workflow_candidate = wrapper.get("workflow_json")
            metadata = wrapper
        else:
            workflow_candidate = decoded
            metadata = {}
        merged = dict(metadata)
        for key, value in data.items():
            if value not in (None, ""):
                merged[key] = value
        return workflow_candidate, merged

    def _workflow_layout_versions(conn, *, preset_id, limit=8):
        rows = conn.execute(
            """
            SELECT version_no, created_by_user_id, workflow_hash, project_version,
                   comfyui_version, workflow_schema_version, created_at
            FROM comfyui_workflow_layout_versions
            WHERE preset_id=?
            ORDER BY version_no DESC
            LIMIT ?
            """,
            (int(preset_id), int(limit)),
        ).fetchall()
        return [{
            "version_no": int(row["version_no"]),
            "created_by_user_id": int(row["created_by_user_id"]),
            "workflow_hash": row["workflow_hash"] or "",
            "project_version": row["project_version"] or "",
            "comfyui_version": row["comfyui_version"] or "",
            "workflow_schema_version": row["workflow_schema_version"] or "",
            "created_at": row["created_at"],
        } for row in rows]

    def _workflow_preset_export_package(row, workflow_json):
        layout_json = parse_json_field(row["layout_json"], {}) or {}
        required_models = parse_json_field(row["required_models_json"], []) or []
        required_loras = parse_json_field(row["required_loras_json"], []) or []
        required_controlnets = parse_json_field(row["required_controlnets_json"], []) or []
        required_custom_nodes = parse_json_field(row["required_custom_nodes_json"], []) or []
        default_params = parse_json_field(row["default_params_json"], {}) or {}
        preset_json = {
            "format": "hackme_web_comfyui_workflow_preset",
            "format_version": 1,
            "name": row["title"] or f"Workflow #{row['id']}",
            "description": row["description"] or "",
            "purpose": row["purpose"] or "custom",
            "project_version": row["project_version"] or "",
            "comfyui_version": row["comfyui_version"] or "",
            "workflow_schema_version": row["workflow_schema_version"] or "1",
            "workflow_json": workflow_json,
            "layout_json": layout_json,
            "required_models": required_models,
            "required_loras": required_loras,
            "required_controlnets": required_controlnets,
            "required_custom_nodes": required_custom_nodes,
            "default_params": default_params,
            "workflow_hash": row["workflow_hash"] or "",
            "visibility": row["visibility"] or "private",
            "is_official": bool(row["is_official"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return {
            "raw_workflow_json": workflow_json,
            "workflow_json": workflow_json,
            "workflow_preset_json": preset_json,
            "layout_json": layout_json,
            "required_models": required_models,
            "required_loras": required_loras,
            "required_controlnets": required_controlnets,
            "required_custom_nodes": required_custom_nodes,
        }

    @app.route("/api/comfyui/workflows/official-media/<path:filename>", methods=["GET"])
    @require_csrf_safe
    def comfyui_official_workflow_media(filename):
        actor, err = actor_or_401()
        if err:
            return err
        media_path = _official_template_media_path(filename)
        if media_path is None:
            return json_resp({"ok": False, "msg": "找不到官方模板範例媒體"}), 404
        response = send_file(
            media_path,
            mimetype=_official_template_media_mime_type(media_path),
            as_attachment=False,
            download_name=media_path.name,
            conditional=True,
            max_age=3600,
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/comfyui/workflow-layouts", methods=["GET"])
    @app.route("/api/comfyui/workflows", methods=["GET"])
    @require_csrf_safe
    def comfyui_workflow_presets():
        actor, err = actor_or_401()
        if err:
            return err
        binding = comfyui_binding(actor)
        active_client = None
        dependency_warning = ""
        try:
            active_client = client_for_url(binding["url"])
            if hasattr(active_client, "health_check"):
                active_client.health_check(timeout=3)
        except Exception as exc:
            dependency_warning = str(exc)
            active_client = None
        conn = get_db()
        try:
            presets = list_workflow_presets(conn, actor=actor, active_client=active_client)
        finally:
            conn.close()
        return json_resp({
            "ok": True,
            "presets": presets,
            "official_presets": [item for item in presets if item.get("is_official")],
            "my_presets": [item for item in presets if int(item.get("owner_user_id") or 0) == int(actor_value(actor, "id")) and not item.get("is_official")],
            "shared_presets": [item for item in presets if int(item.get("owner_user_id") or 0) != int(actor_value(actor, "id")) and not item.get("is_official")],
            "can_publish_official": actor_value(actor, "username") == "root",
            "dependency_warning": dependency_warning,
        })

    @app.route("/api/comfyui/workflow-layouts", methods=["POST"])
    @app.route("/api/comfyui/workflow-layouts/import", methods=["POST"])
    @app.route("/api/comfyui/workflows/import", methods=["POST"])
    @require_csrf
    def comfyui_workflow_import():
        actor, err = actor_or_401()
        if err:
            return err
        try:
            data = ctx["request"].get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤", "stage": "schema_validation"}), 400
        data = data if isinstance(data, dict) else {}
        try:
            workflow_candidate, layout_data = _extract_layout_import_data(data)
            workflow_payload, extracted_defaults = extract_workflow_payload(workflow_candidate)
            default_params = (
                normalize_workflow_default_params(layout_data.get("default_params_json") if "default_params_json" in layout_data else layout_data.get("default_params"))
                if ("default_params_json" in layout_data or "default_params" in layout_data)
                else extracted_defaults
            )
        except WorkflowValidationError as exc:
            return json_resp({"ok": False, "msg": str(exc), "stage": _workflow_validation_stage(exc)}), 400
        title = safe_text(layout_data.get("title") or layout_data.get("name") or f"Workflow {datetime.now().strftime('%Y-%m-%d %H:%M')}", 120)
        conn = get_db()
        try:
            preset_id = upsert_workflow_preset(
                conn,
                actor=actor,
                title=title,
                description=layout_data.get("description") or "",
                visibility=layout_data.get("visibility") or "private",
                workflow_payload=workflow_payload,
                default_params=default_params,
                purpose=layout_data.get("purpose"),
                comfyui_version=layout_data.get("comfyui_version"),
                project_version=layout_data.get("project_version"),
                workflow_schema_version=layout_data.get("workflow_schema_version"),
                layout_json=layout_data.get("layout_json"),
                required_custom_nodes=layout_data.get("required_custom_nodes"),
                is_default=bool(layout_data.get("is_default")),
            )
            row = load_workflow_preset_row(conn, preset_id=preset_id)
            conn.commit()
        finally:
            conn.close()
        audit("COMFYUI_WORKFLOW_IMPORT", get_client_ip(), user=actor_value(actor, "username"), success=True, ua=get_ua(), detail=f"preset_id={preset_id}, title={title}")
        return json_resp({"ok": True, "preset": workflow_preset_summary(row, actor=actor), "msg": "已匯入 workflow preset"})

    @app.route("/api/comfyui/workflow-layouts/export-current", methods=["POST"])
    @app.route("/api/comfyui/workflows/export-current", methods=["POST"])
    @require_csrf
    def comfyui_workflow_export_current():
        actor, err = actor_or_401()
        if err:
            return err
        try:
            data = ctx["request"].get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤", "stage": "schema_validation"}), 400
        data = data if isinstance(data, dict) else {}
        params, msg = normalize_generation_payload(data)
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        active_client = client_for_url(comfyui_binding(actor)["url"])
        try:
            capabilities, capability_msg = validate_generation_capabilities(active_client, params)
            if capability_msg:
                return json_resp({"ok": False, "msg": capability_msg, "capabilities": capabilities or {}}), 409
            workflow = active_client.build_generation_workflow(params)
            workflow_payload = sanitize_workflow_json(workflow)
        except (ctx["ComfyUIError"], WorkflowValidationError) as exc:
            return json_resp({"ok": False, "msg": str(exc), "stage": _workflow_validation_stage(exc)}), 400
        layout_json = {
            "layout_schema_version": "1",
            "node_order": list(workflow_payload["workflow_json"].keys()),
            "node_positions": {},
            "field_overrides": {},
        }
        workflow_preset_json = {
            "format": "hackme_web_comfyui_workflow_preset",
            "format_version": 1,
            "name": data.get("title") or f"Workflow {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "description": data.get("description") or "",
            "purpose": params.get("generation_mode") or "txt2img",
            "project_version": ctx.get("APP_RELEASE_ID", ""),
            "comfyui_version": data.get("comfyui_version") or "",
            "workflow_schema_version": ctx.get("COMFYUI_WORKFLOW_SCHEMA_VERSION", "1"),
            "workflow_json": workflow_payload["workflow_json"],
            "layout_json": layout_json,
            "required_models": workflow_payload["required_models"],
            "required_loras": workflow_payload["required_loras"],
            "required_controlnets": workflow_payload["required_controlnets"],
            "required_custom_nodes": [],
            "default_params": params,
            "workflow_hash": workflow_payload["workflow_hash"],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        return json_resp({
            "ok": True,
            "workflow_json": workflow_payload["workflow_json"],
            "workflow_text": workflow_json_to_pretty_text(workflow_payload["workflow_json"]),
            "layout_json": layout_json,
            "layout_text": workflow_json_to_pretty_text(layout_json),
            "workflow_preset_json": workflow_preset_json,
            "workflow_preset_text": workflow_json_to_pretty_text(workflow_preset_json),
            "workflow_hash": workflow_payload["workflow_hash"],
            "required_models": workflow_payload["required_models"],
            "required_loras": workflow_payload["required_loras"],
            "required_controlnets": workflow_payload["required_controlnets"],
            "required_custom_nodes": [],
            "default_params": params,
        })

    @app.route("/api/comfyui/workflow-layouts/<int:preset_id>", methods=["GET"])
    @app.route("/api/comfyui/workflows/<int:preset_id>", methods=["GET"])
    @require_csrf_safe
    def comfyui_workflow_detail(preset_id):
        actor, err = actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            row, err_resp = load_workflow_preset(conn, preset_id=preset_id, actor=actor)
            if err_resp:
                return err_resp
            active_client = None
            try:
                active_client = client_for_url(comfyui_binding(actor)["url"])
                if hasattr(active_client, "health_check"):
                    active_client.health_check(timeout=3)
            except Exception:
                active_client = None
            dependency_status = workflow_dependency_status(active_client, row) if active_client is not None else None
            recent_runs = list_workflow_runs(conn, preset_id=preset_id, limit=ctx["COMFYUI_WORKFLOW_RUN_LIMIT"])
            payload = workflow_preset_summary(row, dependency_status=dependency_status, recent_runs=recent_runs, actor=actor)
            workflow_json = apply_workflow_compatibility_fixes(parse_json_field(row["workflow_json"], {}) or {})
            payload["workflow_json"] = workflow_json
            payload["layout_json"] = parse_json_field(row["layout_json"], {}) or {}
            payload["manifest_json"] = workflow_manifest_for_row(row) if workflow_manifest_for_row else None
            payload["layout_versions"] = _workflow_layout_versions(conn, preset_id=preset_id)
            try:
                analysis = analyze_workflow_json(workflow_json)
                capability = check_workflow_capability(analysis, client=active_client)
                payload["ui_schema"] = build_ui_schema(
                    analysis=analysis,
                    capability=capability,
                    raw_workflow=workflow_json,
                ).to_dict()
                payload["capability"] = capability.to_dict()
            except Exception:
                payload["ui_schema"] = None
            payload["output_kinds"] = _workflow_output_kinds(workflow_json)
        finally:
            conn.close()
        return json_resp({"ok": True, "preset": payload})

    @app.route("/api/comfyui/workflow-layouts/<int:preset_id>", methods=["PUT"])
    @app.route("/api/comfyui/workflows/<int:preset_id>", methods=["PUT"])
    @require_csrf
    def comfyui_workflow_update(preset_id):
        actor, err = actor_or_401()
        if err:
            return err
        try:
            data = ctx["request"].get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤", "stage": "schema_validation"}), 400
        data = data if isinstance(data, dict) else {}
        conn = get_db()
        try:
            row, err_resp = load_workflow_preset(conn, preset_id=preset_id, actor=actor, require_write=True)
            if err_resp:
                return err_resp
            before = workflow_preset_summary(row, actor=actor)
            if "workflow_json" in data or "workflow" in data:
                workflow_candidate, layout_data = _extract_layout_import_data(data)
            else:
                workflow_candidate, layout_data = parse_json_field(row["workflow_json"], {}) or {}, dict(data)
            workflow_payload, extracted_defaults = extract_workflow_payload(workflow_candidate)
            if "default_params_json" in layout_data or "default_params" in layout_data:
                default_params = normalize_workflow_default_params(layout_data.get("default_params_json") if "default_params_json" in layout_data else layout_data.get("default_params"))
            elif "workflow_json" in data or "workflow" in data:
                default_params = extracted_defaults
            else:
                default_params = parse_json_field(row["default_params_json"], {}) or {}
            updated_id = upsert_workflow_preset(
                conn,
                preset_id=preset_id,
                actor=actor,
                title=layout_data.get("title") or row["title"],
                description=layout_data.get("description") if "description" in layout_data else row["description"],
                visibility=layout_data.get("visibility") if "visibility" in layout_data else row["visibility"],
                workflow_payload=workflow_payload,
                default_params=default_params,
                purpose=layout_data.get("purpose") if "purpose" in layout_data else row["purpose"],
                comfyui_version=layout_data.get("comfyui_version") if "comfyui_version" in layout_data else row["comfyui_version"],
                project_version=layout_data.get("project_version") if "project_version" in layout_data else row["project_version"],
                workflow_schema_version=layout_data.get("workflow_schema_version") if "workflow_schema_version" in layout_data else row["workflow_schema_version"],
                layout_json=layout_data.get("layout_json") if "layout_json" in layout_data else parse_json_field(row["layout_json"], {}) or {},
                required_custom_nodes=layout_data.get("required_custom_nodes") if "required_custom_nodes" in layout_data else parse_json_field(row["required_custom_nodes_json"], []) or [],
                is_default=bool(layout_data.get("is_default")) if "is_default" in layout_data else bool(row["is_default"]),
                is_official=bool(row["is_official"]),
                published_by_user_id=row["published_by_user_id"],
                system_bundle_id=row["system_bundle_id"],
            )
            row = load_workflow_preset_row(conn, preset_id=updated_id)
            conn.commit()
        except WorkflowValidationError as exc:
            conn.rollback()
            return json_resp({"ok": False, "msg": str(exc), "stage": _workflow_validation_stage(exc)}), 400
        finally:
            conn.close()
        after = workflow_preset_summary(row, actor=actor)
        audit(
            "COMFYUI_WORKFLOW_UPDATE",
            get_client_ip(),
            user=actor_value(actor, "username"),
            success=True,
            ua=get_ua(),
            detail=f"preset_id={preset_id}, before={json.dumps(before, ensure_ascii=False)[:180]}, after={json.dumps(after, ensure_ascii=False)[:180]}",
        )
        return json_resp({"ok": True, "preset": after, "msg": "已更新 workflow preset"})

    @app.route("/api/comfyui/workflow-layouts/<int:preset_id>", methods=["DELETE"])
    @app.route("/api/comfyui/workflows/<int:preset_id>", methods=["DELETE"])
    @require_csrf
    def comfyui_workflow_delete(preset_id):
        actor, err = actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            row, err_resp = load_workflow_preset(conn, preset_id=preset_id, actor=actor, require_write=True)
            if err_resp:
                return err_resp
            conn.execute("DELETE FROM comfyui_workflow_runs WHERE preset_id=?", (int(preset_id),))
            conn.execute("DELETE FROM comfyui_workflow_layout_versions WHERE preset_id=?", (int(preset_id),))
            conn.execute("DELETE FROM comfyui_workflow_presets WHERE id=? AND owner_user_id=?", (int(preset_id), int(actor_value(actor, "id"))))
            conn.commit()
        finally:
            conn.close()
        audit("COMFYUI_WORKFLOW_DELETE", get_client_ip(), user=actor_value(actor, "username"), success=True, ua=get_ua(), detail=f"preset_id={preset_id}")
        return json_resp({"ok": True, "msg": "已刪除 workflow preset"})

    @app.route("/api/comfyui/workflow-layouts/<int:preset_id>/run", methods=["POST"])
    @app.route("/api/comfyui/workflows/<int:preset_id>/run", methods=["POST"])
    @require_csrf
    def comfyui_workflow_run(preset_id):
        actor, err = actor_or_401()
        if err:
            return err
        # Strict mode (§15.7 / Phase 6): when feature_comfyui_template_importer_strict
        # is on, every /run goes through the §10 5-gate. Body may carry
        # user_inputs (per-node patch dict) and image_field_assignments
        # (LoadImage node_id → cloud_file_id) so the gate can validate +
        # remap. Legacy callers without these fields are still subject to
        # gate validation against the preset's stored workflow.
        try:
            strict_mode = is_feature_enabled("feature_comfyui_template_importer_strict")
        except Exception:
            # Settings DB not initialized (test fixtures, fresh boot before
            # init_db); fall back to legacy behavior so the existing run
            # tests stay green.
            strict_mode = False
        try:
            body = ctx["request"].get_json(force=True, silent=True) or {}
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        user_inputs = body.get("user_inputs") if isinstance(body.get("user_inputs"), dict) else {}
        image_field_assignments = (
            body.get("image_field_assignments")
            if isinstance(body.get("image_field_assignments"), dict)
            else {}
        )
        multi_compare = body.get("multi_compare") if isinstance(body.get("multi_compare"), dict) else {}
        upscale_breakpoint = (
            body.get("upscale_breakpoint")
            if isinstance(body.get("upscale_breakpoint"), dict)
            else {}
        )
        sdxl_refiner = body.get("sdxl_refiner") if isinstance(body.get("sdxl_refiner"), dict) else {}
        gguf_workflow = body.get("gguf_workflow") if isinstance(body.get("gguf_workflow"), dict) else {}
        selected_vae = _normalize_workflow_vae_name(body.get("vae") or body.get("vae_name"))
        if selected_vae is None:
            return json_resp({"ok": False, "msg": "VAE 名稱格式不合法", "stage": "vae_validation"}), 400
        conn = get_db()
        try:
            row, err_resp = load_workflow_preset(conn, preset_id=preset_id, actor=actor)
            if err_resp:
                return err_resp
            comfyui_url = (comfyui_binding(actor) or {}).get("url")
            active_client = client_for_url(comfyui_url) if comfyui_url else None
            default_params = parse_json_field(row["default_params_json"], {}) or {}
            preserved_seed = default_params.get("seed")
            workflow_json = apply_workflow_compatibility_fixes(parse_json_field(row["workflow_json"], {}) or {})
            body_with_bundle = dict(body)
            body_with_bundle["system_bundle_id"] = row["system_bundle_id"]
            workflow_json, qwen_pose_control_changed = _apply_qwen_2512_controlnet_pose_mode(
                workflow_json,
                body_with_bundle,
            )
            workflow_json, qwen_reference_changed = _apply_qwen_edit_reference_policy(
                workflow_json,
                system_bundle_id=row["system_bundle_id"],
                image_field_assignments=image_field_assignments,
            )
            runtime_dependency_row = row
            runtime_workflow_changed = bool(qwen_reference_changed or qwen_pose_control_changed)
            if is_upscale_breakpoint_workflow_id(row["system_bundle_id"]):
                if not upscale_breakpoint:
                    default_upscale_mode = str(default_params.get("upscale_mode") or default_params.get("upscale_breakpoint") or "").strip()
                    if default_upscale_mode:
                        upscale_breakpoint = {"mode": default_upscale_mode}
                try:
                    selection = apply_upscale_breakpoint(
                        workflow_json,
                        user_inputs,
                        upscale_breakpoint,
                    )
                except UpscaleBreakpointError as exc:
                    return json_resp({"ok": False, "msg": str(exc), "stage": "upscale_breakpoint_validation"}), 400
                workflow_json = selection.workflow
                user_inputs = selection.user_inputs
                default_params = dict(default_params)
                default_params["upscale_breakpoint"] = selection.stage
                default_params["upscale_mode"] = selection.stage
                runtime_workflow_changed = True
            if is_multi_compare_workflow_id(row["system_bundle_id"]) and multi_compare:
                try:
                    expansion = expand_multi_compare_workflow(
                        workflow_json,
                        user_inputs,
                        multi_compare,
                    )
                except MultiCompareWorkflowError as exc:
                    return json_resp({"ok": False, "msg": str(exc), "stage": "multi_compare_validation"}), 400
                workflow_json = expansion.workflow
                user_inputs = expansion.user_inputs
                runtime_workflow_changed = True
            if is_sdxl_refiner_workflow_id(row["system_bundle_id"]) and sdxl_refiner:
                try:
                    selection = apply_sdxl_refiner_option(
                        workflow_json,
                        user_inputs,
                        sdxl_refiner,
                    )
                except SdxlRefinerWorkflowError as exc:
                    return json_resp({"ok": False, "msg": str(exc), "stage": "sdxl_refiner_validation"}), 400
                workflow_json = selection.workflow
                user_inputs = selection.user_inputs
                if selection.skip_refiner:
                    default_params = dict(default_params)
                    default_params["skip_refiner"] = True
                    base_model = (workflow_json.get("4") or {}).get("inputs", {}).get("ckpt_name")
                    if base_model:
                        default_params["model"] = base_model
                        default_params["checkpoint"] = base_model
                    runtime_workflow_changed = True

            if is_gguf_workflow_id(row["system_bundle_id"]) and gguf_workflow:
                try:
                    selection = apply_gguf_workflow_profile(
                        workflow_json,
                        user_inputs,
                        gguf_workflow,
                    )
                except GgufWorkflowError as exc:
                    return json_resp({"ok": False, "msg": str(exc), "stage": "gguf_workflow_validation"}), 400
                workflow_json = selection.workflow
                user_inputs = selection.user_inputs
                if selection.profile and selection.variant:
                    default_params = dict(default_params)
                    default_params["gguf_profile"] = selection.profile.get("id") or ""
                    default_params["gguf_variant"] = selection.variant.get("id") or ""
                    default_params["model"] = selection.variant.get("gguf_file") or selection.variant.get("filename") or default_params.get("model")
                    default_params["diffusion_model"] = default_params["model"]
                    default_params["clip_loader_class"] = selection.profile.get("clip_loader_class") or ""
                    runtime_workflow_changed = True

            if selected_vae:
                workflow_json, vae_changed = _apply_workflow_vae_override(workflow_json, selected_vae)
                default_params = dict(default_params)
                default_params["vae"] = selected_vae
                if vae_changed:
                    runtime_workflow_changed = True

            run_count = _workflow_request_int(body.get("run_count") or body.get("history_run_count"), 1, 1, 10)
            seed_after_generate = _normalize_workflow_seed_after_generate(
                body.get("seed_after_generate") or body.get("seed_after_generate_mode")
            )
            if (
                "seed_after_generate" not in (body or {})
                and "seed_after_generate_mode" not in (body or {})
            ):
                configured_mode = _normalize_workflow_seed_after_generate(default_params.get("seed_after_generate"))
                if configured_mode in {"fixed", "increment", "decrement", "random"}:
                    seed_after_generate = configured_mode
                elif default_params.get("seed") is not None:
                    seed_after_generate = "fixed"
            default_params = dict(default_params)
            default_params["run_count"] = run_count
            default_params["seed_after_generate"] = seed_after_generate
            has_seed_patch = bool(
                any(
                    "noise_seed" in patch or "seed" in patch
                    for patch in user_inputs.values()
                    if isinstance(patch, dict)
                )
            )
            if seed_after_generate == "random":
                workflow_json, user_inputs, random_seed = _randomize_workflow_seed_inputs(workflow_json, user_inputs)
                if random_seed is not None:
                    default_params["seed"] = random_seed
                    runtime_workflow_changed = True

            if runtime_workflow_changed:
                runtime_dependency_row = dict(row)
                runtime_dependency_row["workflow_json"] = json.dumps(workflow_json or {}, ensure_ascii=False, sort_keys=True)
                runtime_dependency_row["required_models_json"] = "[]"
                runtime_dependency_row["required_loras_json"] = "[]"
                runtime_dependency_row["required_controlnets_json"] = "[]"
                runtime_dependency_row["required_custom_nodes_json"] = "[]"
            dependency_status, dependency_msg = assert_workflow_dependencies_or_error(active_client, runtime_dependency_row)
            if dependency_msg and dependency_status.get("missing_nodes"):
                return json_resp({"ok": False, "msg": dependency_msg, "stage": "unknown_node", "dependency_status": dependency_status}), 409

            # Model, LoRA, and ControlNet availability may change after user_inputs
            # replace template defaults. Let the strict run gate validate the final
            # patched workflow instead of blocking on stale official defaults.

            # 5-gate enforcement before any job is created — failed gates
            # never produce a job_id so the user gets immediate feedback
            # instead of polling status.
            if strict_mode:
                import uuid as _uuid
                gate_run_id = _uuid.uuid4().hex
                try:
                    gate_result = run_workflow_through_gates(
                        raw_workflow=workflow_json,
                        user_inputs=user_inputs,
                        image_field_assignments=image_field_assignments,
                        actor=dict(actor),
                        user_id=int(actor_value(actor, "id")),
                        run_id=gate_run_id,
                        conn=conn,
                        comfyui_client=active_client,
                        upload_callback=_default_upload_callback(
                            active_client,
                            storage_root=storage_root,
                            resolve_file_storage_path=resolve_file_storage_path,
                        ),
                        fetch_file_row=lambda gate_conn, cloud_file_id: _workflow_template_fetch_file_row(
                            gate_conn,
                            cloud_file_id,
                            actor=actor,
                        ),
                    )
                except RunGateFailure as exc:
                    audit(
                        "COMFYUI_TEMPLATE_RUN_GATE_FAIL",
                        get_client_ip(),
                        user=actor_value(actor, "username") or "-",
                        success=False,
                        ua=get_ua(),
                        detail=(
                            f"preset_id={preset_id} run_id={gate_run_id} "
                            f"gate={exc.gate} stage={exc.stage} reason={exc.msg}"
                        ),
                    )
                    return json_resp({
                        "ok": False,
                        "msg": exc.msg,
                        "stage": exc.stage,
                        "gate": exc.gate,
                        "audit_detail": exc.audit_detail,
                    }), exc.http_status
                workflow_json = gate_result.workflow
                final_dependency_row = dict(row)
                final_dependency_row["workflow_json"] = json.dumps(workflow_json or {}, ensure_ascii=False, sort_keys=True)
                final_dependency_row["required_models_json"] = "[]"
                final_dependency_row["required_loras_json"] = "[]"
                final_dependency_row["required_controlnets_json"] = "[]"
                final_dependency_row["required_custom_nodes_json"] = "[]"
                dependency_status = workflow_dependency_status(active_client, final_dependency_row)
                audit(
                    "COMFYUI_TEMPLATE_RUN_GATE_PASS",
                    get_client_ip(),
                    user=actor_value(actor, "username") or "-",
                    success=True,
                    ua=get_ua(),
                    detail=(
                        f"preset_id={preset_id} run_id={gate_run_id} "
                        f"node_count={gate_result.audit_metadata.get('node_count')} "
                        f"image_remapped={gate_result.audit_metadata.get('image_remapped')}"
                    ),
                )
            elif user_inputs:
                workflow_json = _apply_legacy_workflow_user_inputs(workflow_json, user_inputs)
                workflow_json = apply_workflow_compatibility_fixes(workflow_json)
            if not strict_mode:
                workflow_json = rewrite_workflow_model_inputs_to_local_options(
                    workflow_json,
                    client=active_client,
                )
                final_dependency_row = dict(row)
                final_dependency_row["workflow_json"] = json.dumps(workflow_json or {}, ensure_ascii=False, sort_keys=True)
                final_dependency_row["required_models_json"] = "[]"
                final_dependency_row["required_loras_json"] = "[]"
                final_dependency_row["required_controlnets_json"] = "[]"
                final_dependency_row["required_custom_nodes_json"] = "[]"
                final_dependency_status, final_dependency_msg = assert_workflow_dependencies_or_error(
                    active_client,
                    final_dependency_row,
                )
                if final_dependency_msg:
                    dependency_status = final_dependency_status
                    stage = "unknown_node" if final_dependency_status.get("missing_nodes") else "missing_model"
                    return json_resp({
                        "ok": False,
                        "msg": final_dependency_msg,
                        "stage": stage,
                        "dependency_status": final_dependency_status,
                    }), 409

            prompt_extra_data = {}
            if comfyui_paid_api_policy:
                prompt_extra_data, paid_api_error = comfyui_paid_api_policy(
                    workflow_json,
                    confirm=bool(body.get("confirm_paid_api_nodes")),
                )
                if paid_api_error:
                    return paid_api_error

            workflow_run_params = _workflow_snapshot_params(default_params, workflow_json)
            requested_width = _workflow_request_int(
                body.get("requested_width") or body.get("output_width") or body.get("width"),
                0,
                0,
                4096,
            )
            requested_height = _workflow_request_int(
                body.get("requested_height") or body.get("output_height") or body.get("height"),
                0,
                0,
                4096,
            )
            if requested_width and requested_height:
                workflow_run_params["requested_width"] = requested_width
                workflow_run_params["requested_height"] = requested_height
                workflow_run_params["output_width"] = requested_width
                workflow_run_params["output_height"] = requested_height
            if seed_after_generate != "random" and preserved_seed is not None and not has_seed_patch:
                workflow_run_params["seed"] = preserved_seed
            workflow_run_params["seed_after_generate"] = seed_after_generate
            workflow_run_params["workflow_preset_id"] = int(preset_id)
            workflow_run_params["workflow_preset_title"] = row["title"] or "Workflow"
            workflow_run_params["workflow_system_bundle_id"] = row["system_bundle_id"] or ""
            run_id = create_workflow_run(
                conn,
                preset_id=preset_id,
                actor=actor,
                prompt=workflow_run_params.get("prompt") or "",
                negative_prompt=workflow_run_params.get("negative_prompt") or "",
                params_json=workflow_run_params,
                workflow_json=workflow_json,
            )
            conn.commit()
        finally:
            conn.close()
        job_id = create_generation_job(actor)
        request_meta = capture_request_audit_meta()
        worker = threading.Thread(
            target=run_comfyui_workflow_preset_job,
            args=(job_id, dict(actor), dict(row), run_id, DEFAULT_GENERATION_TIMEOUT_SECONDS, request_meta, prompt_extra_data, workflow_json),
            daemon=True,
        )
        worker.start()
        return json_resp({
            "ok": True,
            "async": True,
            "workflow_run_id": run_id,
            "dependency_status": dependency_status,
            "strict_mode": bool(strict_mode),
            "job": {
                "job_id": job_id,
                "status": "queued",
                "progress": {
                    "phase": "queued",
                    "percent": 0,
                    "detail": "已建立 workflow 執行工作",
                    "timeout_seconds": int(DEFAULT_GENERATION_TIMEOUT_SECONDS or 0),
                    "timeout_unlimited": int(DEFAULT_GENERATION_TIMEOUT_SECONDS or 0) <= 0,
                },
            },
        })

    @app.route("/api/comfyui/workflow-layouts/<int:preset_id>/export", methods=["POST"])
    @app.route("/api/comfyui/workflows/<int:preset_id>/export", methods=["POST"])
    @require_csrf
    def comfyui_workflow_export(preset_id):
        actor, err = actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            row, err_resp = load_workflow_preset(conn, preset_id=preset_id, actor=actor)
            if err_resp:
                return err_resp
            workflow_json = apply_workflow_compatibility_fixes(parse_json_field(row["workflow_json"], {}) or {})
            package = _workflow_preset_export_package(row, workflow_json)
        finally:
            conn.close()
        return json_resp({
            "ok": True,
            "filename": f"comfyui-workflow-layout-{preset_id}.json",
            "workflow_hash": row["workflow_hash"] or "",
            "workflow_text": workflow_json_to_pretty_text(workflow_json),
            "workflow_preset_text": workflow_json_to_pretty_text(package["workflow_preset_json"]),
            **package,
        })

    @app.route("/api/admin/comfyui/workflows/<int:preset_id>/publish-official", methods=["POST"])
    @require_csrf
    def comfyui_workflow_publish_official(preset_id):
        actor, err = root_or_403()
        if err:
            return err
        conn = get_db()
        try:
            row, err_resp = load_workflow_preset(conn, preset_id=preset_id, actor=actor, require_write=True)
            if err_resp:
                return err_resp
            updated_id = upsert_workflow_preset(
                conn,
                preset_id=preset_id,
                actor=actor,
                title=row["title"],
                description=row["description"],
                visibility="public",
                workflow_payload={
                    "workflow_json": parse_json_field(row["workflow_json"], {}) or {},
                    "workflow_hash": row["workflow_hash"] or "",
                    "required_models": parse_json_field(row["required_models_json"], []) or [],
                    "required_loras": parse_json_field(row["required_loras_json"], []) or [],
                    "required_controlnets": parse_json_field(row["required_controlnets_json"], []) or [],
                    "default_params": parse_json_field(row["default_params_json"], {}) or {},
                },
                default_params=parse_json_field(row["default_params_json"], {}) or {},
                purpose=row["purpose"],
                comfyui_version=row["comfyui_version"],
                project_version=row["project_version"],
                workflow_schema_version=row["workflow_schema_version"],
                layout_json=parse_json_field(row["layout_json"], {}) or {},
                required_custom_nodes=parse_json_field(row["required_custom_nodes_json"], []) or [],
                is_default=bool(row["is_default"]),
                is_official=True,
                published_by_user_id=actor_value(actor, "id"),
                system_bundle_id=row["system_bundle_id"],
            )
            row = load_workflow_preset_row(conn, preset_id=updated_id)
            conn.commit()
        finally:
            conn.close()
        audit("COMFYUI_WORKFLOW_PUBLISH_OFFICIAL", get_client_ip(), user=actor_value(actor, "username"), success=True, ua=get_ua(), detail=f"preset_id={preset_id}")
        return json_resp({"ok": True, "preset": workflow_preset_summary(row, actor=actor), "msg": "已發布為官方 preset"})
