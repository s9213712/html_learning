def register_comfyui_runtime_routes(app, ctx):
    request = ctx["request"]
    json_resp = ctx["json_resp"]
    require_csrf = ctx["require_csrf"]
    require_csrf_safe = ctx["require_csrf_safe"]
    get_db = ctx["get_db"]
    get_client_ip = ctx["get_client_ip"]
    get_ua = ctx["get_ua"]
    audit = ctx["audit"]
    threading = ctx["threading"]
    ComfyUIError = ctx["ComfyUIError"]
    SAFE_SAMPLER_FALLBACK = ctx["SAFE_SAMPLER_FALLBACK"]
    SAFE_SCHEDULER_FALLBACK = ctx["SAFE_SCHEDULER_FALLBACK"]
    COMFYUI_LORA_EXTRA_PRICE_POINTS = ctx["COMFYUI_LORA_EXTRA_PRICE_POINTS"]
    COMFYUI_HISTORY_LIMIT = ctx["COMFYUI_HISTORY_LIMIT"]
    DEFAULT_GENERATION_TIMEOUT_SECONDS = ctx["DEFAULT_GENERATION_TIMEOUT_SECONDS"]
    MAX_GENERATION_TIMEOUT_SECONDS = ctx["MAX_GENERATION_TIMEOUT_SECONDS"]
    COMFYUI_STATUS_TIMEOUT_SECONDS = ctx.get("COMFYUI_STATUS_TIMEOUT_SECONDS", 2.0)
    _actor_or_401 = ctx["actor_or_401"]
    _root_or_403 = ctx["root_or_403"]
    _actor_value = ctx["actor_value"]
    _assert_generation_job_owner = ctx["assert_generation_job_owner"]
    _build_lora_details = ctx["build_lora_details"]
    _capture_request_audit_meta = ctx["capture_request_audit_meta"]
    _charge_comfyui_generation = ctx["charge_comfyui_generation"]
    _client_for_url = ctx["client_for_url"]
    _coerce_bool = ctx["coerce_bool"]
    _comfyui_binding = ctx["comfyui_binding"]
    _comfyui_charge_required = ctx["comfyui_charge_required"]
    _comfyui_lora_count = ctx["comfyui_lora_count"]
    _comfyui_price_quote = ctx["comfyui_price_quote"]
    _comfyui_total_quantity = ctx["comfyui_total_quantity"]
    _comfyui_unavailable_payload = ctx["comfyui_unavailable_payload"]
    _comfyui_wallet_payload = ctx["comfyui_wallet_payload"]
    _comfyui_storage_warnings = ctx.get("comfyui_storage_warnings", lambda: [])
    _comfyui_paid_api_status_payload = ctx.get("comfyui_paid_api_status_payload", lambda: {})
    _official_gguf_profiles = ctx.get("official_gguf_profiles", lambda: [])
    _installed_gguf_inventory = ctx.get("installed_gguf_inventory", lambda values: [])
    _build_node_catalog = ctx.get("build_node_catalog")
    _configured_comfyui_port = ctx["configured_comfyui_port"]
    _configured_comfyui_url = ctx["configured_comfyui_url"]
    _configured_connection_mode = ctx["configured_connection_mode"]
    _configured_default_dimensions = ctx["configured_default_dimensions"]
    _configured_max_batch_size = ctx["configured_max_batch_size"]
    _create_generation_job = ctx["create_generation_job"]
    _ensure_comfyui_balance = ctx["ensure_comfyui_balance"]
    _finalize_generation_records = ctx["finalize_generation_records"]
    _hydrate_generation_assets = ctx["hydrate_generation_assets"]
    _int_range = ctx["int_range"]
    _json_error_from_comfy = ctx["json_error_from_comfy"]
    _list_generation_history = ctx["list_generation_history"]
    _load_generation_history = ctx["load_generation_history"]
    _ensure_comfyui_workflow_schema = ctx.get("ensure_comfyui_workflow_schema")
    _local_comfyui_runtime_status = ctx["local_comfyui_runtime_status"]
    _normalize_generation_payload = ctx["normalize_generation_payload"]
    _normalize_generation_timeout = ctx["normalize_generation_timeout"]
    _parse_generation_request = ctx["parse_generation_request"]
    _parse_json_field = ctx.get("parse_json_field")
    _record_generation_history = ctx["record_generation_history"]
    _register_active_generation = ctx["register_active_generation"]
    _run_comfyui_generation_job = ctx["run_comfyui_generation_job"]
    _create_workflow_run = ctx.get("create_workflow_run")
    _ensure_comfyui_workflow_schema = ctx.get("ensure_comfyui_workflow_schema")
    _run_comfyui_workflow_preset_job = ctx.get("run_comfyui_workflow_preset_job")
    _generation_job_payload = ctx.get("generation_job_payload")
    _initial_generation_progress = ctx.get("initial_generation_progress")
    _update_generation_job = ctx.get("update_generation_job")
    _update_generation_job_progress = ctx.get("update_generation_job_progress")
    _media_ref_payload = ctx.get("image_ref_payload")
    _start_local_comfyui = ctx["start_local_comfyui"]
    _stop_local_comfyui = ctx["stop_local_comfyui"]
    _unregister_active_generation = ctx["unregister_active_generation"]
    _validate_generation_capabilities = ctx["validate_generation_capabilities"]

    def _job_media_output_item(job_result, file_ref):
        if not callable(_media_ref_payload) or not isinstance(job_result, dict) or not file_ref:
            return None
        media = job_result.get("media")
        records = []
        image = job_result.get("image")
        if isinstance(image, dict):
            records.append(image)
        images = job_result.get("images")
        if isinstance(images, list):
            records.extend(images)
        if isinstance(media, list):
            records.extend(media)
        elif isinstance(media, dict):
            for items in media.values():
                if isinstance(items, list):
                    records.extend(items)
        for item in records:
            if not isinstance(item, dict):
                continue
            candidate = item.get("file_ref") if isinstance(item.get("file_ref"), dict) else item.get("image_ref")
            if not candidate:
                continue
            try:
                if _media_ref_payload(candidate) == file_ref:
                    return item
            except Exception:
                continue
        return None

    def _media_mime_type(mime_type, filename):
        import mimetypes

        raw = str(mime_type or "").strip()
        normalized = raw.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_type(str(filename or ""))[0]
        if not normalized or normalized == "application/octet-stream":
            return guessed or raw or "application/octet-stream"
        return raw

    def _runtime_parse_json_field(value, fallback):
        if callable(_parse_json_field):
            return _parse_json_field(value, fallback)
        if value in (None, ""):
            return fallback
        try:
            import json

            parsed = json.loads(value) if isinstance(value, str) else value
        except Exception:
            return fallback
        return parsed if isinstance(parsed, type(fallback)) else fallback

    def _runtime_meminfo_kb():
        result = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as file_obj:
                for line in file_obj:
                    if ":" not in line:
                        continue
                    key, rest = line.split(":", 1)
                    parts = rest.strip().split()
                    if not parts:
                        continue
                    try:
                        result[key] = int(parts[0])
                    except Exception:
                        continue
        except Exception:
            return {}
        return result

    def _runtime_format_bytes(value):
        try:
            size = float(value)
        except Exception:
            return "0 B"
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        index = 0
        while size >= 1024 and index < len(units) - 1:
            size /= 1024.0
            index += 1
        return f"{size:.1f} {units[index]}" if index else f"{int(size)} {units[index]}"

    def _runtime_checkpoint_candidate_paths(name):
        import os
        from pathlib import Path

        raw = str(name or "").strip().replace("\\", "/")
        if not raw:
            return []
        candidates = []
        raw_path = Path(raw)
        if raw_path.is_absolute():
            candidates.append(raw_path)
        roots = []
        for value in (
            os.environ.get("COMFYUI_CHECKPOINT_DIR"),
            os.environ.get("COMFYUI_MODELS_DIR"),
            os.environ.get("HACKME_COMFYUI_MODELS_DIR"),
        ):
            if value:
                roots.append(Path(value))
        roots.extend([
            Path("/mnt/c/share/ComfyUI/models"),
            Path("/mnt/c/ComfyUI_windows_portable/ComfyUI/models"),
        ])
        rel_values = [raw]
        base = raw.rsplit("/", 1)[-1]
        if base and base != raw:
            rel_values.append(base)
        for root in roots:
            for rel in rel_values:
                candidates.append(root / "checkpoints" / rel)
                candidates.append(root / rel)
        seen = set()
        unique = []
        for path in candidates:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    def _runtime_checkpoint_file_info(name):
        for path in _runtime_checkpoint_candidate_paths(name):
            try:
                if path.is_file():
                    return {"path": str(path), "size_bytes": int(path.stat().st_size)}
            except Exception:
                continue
        return None

    def _local_checkpoint_memory_preflight(binding, params):
        if str((binding or {}).get("connection_mode") or "").strip().lower() != "local":
            return None
        checkpoint = str(
            (params or {}).get("checkpoint")
            or (params or {}).get("model")
            or (params or {}).get("checkpoint_name")
            or ""
        ).strip()
        if not checkpoint:
            return None
        file_info = _runtime_checkpoint_file_info(checkpoint)
        if not file_info:
            return None
        meminfo = _runtime_meminfo_kb()
        available_bytes = int(meminfo.get("MemAvailable", 0) + meminfo.get("SwapFree", 0)) * 1024
        size_bytes = int(file_info["size_bytes"])
        required_bytes = int(size_bytes * 1.15)
        if available_bytes >= required_bytes:
            return None
        return {
            "checkpoint": checkpoint,
            "checkpoint_path": file_info["path"],
            "checkpoint_size_bytes": size_bytes,
            "available_memory_bytes": available_bytes,
            "required_memory_bytes": required_bytes,
            "msg": (
                "ComfyUI 本機記憶體不足，已取消送出產圖："
                f"checkpoint {checkpoint} 大小約 {_runtime_format_bytes(size_bytes)}，"
                f"目前 RAM+swap 可用約 {_runtime_format_bytes(available_bytes)}。"
                "這會在 CheckpointLoaderSimple 載入 safetensors/mmap 時失敗；"
                "請增加 WSL RAM/swap、改用較小 checkpoint，或先卸載其他占用記憶體的程序。"
            ),
        }

    def _runtime_randomize_workflow_seed_inputs(workflow_json, params):
        if str((params or {}).get("seed_after_generate") or "").strip().lower() != "random":
            return workflow_json, params
        if not isinstance(workflow_json, dict):
            return workflow_json, params
        import json
        import secrets

        patched_workflow = json.loads(json.dumps(workflow_json))
        seed = secrets.randbits(32)
        changed = False
        for node in patched_workflow.values():
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
                if key not in inputs:
                    continue
                inputs[key] = seed
                changed = True
        if not changed:
            return workflow_json, params
        next_params = dict(params or {})
        next_params["seed"] = seed
        return patched_workflow, next_params

    def _runtime_workflow_snapshot_params(params, workflow_json):
        merged = dict(params or {})
        if not isinstance(workflow_json, dict):
            return merged
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
                    merged["seed"] = inputs.get("noise_seed")
                elif inputs.get("seed") is not None:
                    merged["seed"] = inputs.get("seed")
                for src, dst in (
                    ("steps", "steps"),
                    ("cfg", "cfg"),
                    ("sampler_name", "sampler_name"),
                    ("scheduler", "scheduler"),
                    ("denoise", "denoise_strength"),
                ):
                    if inputs.get(src) is not None:
                        merged[dst] = inputs.get(src)
            if class_type == "EmptyLatentImage":
                for key in ("width", "height", "batch_size"):
                    if inputs.get(key) is not None:
                        merged[key] = inputs.get(key)
                if inputs.get("batch_size") is not None and not merged.get("ui_batch_size"):
                    merged["ui_batch_size"] = inputs.get("batch_size")
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
        current_model = str(merged.get("model") or merged.get("checkpoint") or merged.get("diffusion_model") or "").strip()
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
            merged["model"] = selected_model
            if selected_model in checkpoint_names:
                merged["checkpoint"] = selected_model
        vae_names = [name for name in vae_names if name]
        if vae_names:
            merged["vae"] = vae_names[0]
        if prompts:
            merged["prompt"] = prompts[0]
        if negatives:
            merged["negative_prompt"] = negatives[0]
        return merged

    def _runtime_workflow_rerun_seed_mode(params, request_body):
        body = request_body if isinstance(request_body, dict) else {}
        requested = str(body.get("seed_after_generate") or body.get("seed_after_generate_mode") or "").strip().lower()
        if requested in {"random", "fixed", "increment", "decrement"}:
            return requested
        saved = str((params or {}).get("seed_after_generate") or "").strip().lower()
        if saved in {"fixed", "increment", "decrement"}:
            return saved
        if (params or {}).get("seed") not in (None, ""):
            return "fixed"
        return "random"

    def _resource_percent(value):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(100.0, parsed))

    def _proc_meminfo_snapshot():
        meminfo = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    if ":" not in line:
                        continue
                    key, rest = line.split(":", 1)
                    parts = rest.strip().split()
                    if not parts:
                        continue
                    meminfo[key] = int(float(parts[0])) * 1024
        except Exception:
            return {"available": False}
        total = int(meminfo.get("MemTotal") or 0)
        available = int(meminfo.get("MemAvailable") or 0)
        used = max(0, total - available) if total else 0
        percent = round((used / total) * 100, 1) if total else None
        return {
            "available": bool(total),
            "used_bytes": used,
            "total_bytes": total,
            "percent": percent,
        }

    def _nvidia_smi_resource_snapshot():
        import subprocess

        from services.system.gpu_probe import find_nvidia_smi

        nvidia_smi = find_nvidia_smi()
        if not nvidia_smi:
            return {"available": False, "gpus": [], "error": "nvidia-smi not found"}
        try:
            proc = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2.5,
            )
        except Exception as exc:
            return {"available": False, "gpus": [], "error": str(exc)}
        if proc.returncode != 0:
            return {"available": False, "gpus": [], "error": (proc.stderr or proc.stdout or "").strip()[:240]}
        gpus = []
        for line in (proc.stdout or "").splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 6:
                continue
            try:
                used_mb = float(parts[3])
                total_mb = float(parts[4])
            except (TypeError, ValueError):
                used_mb = 0.0
                total_mb = 0.0
            gpus.append({
                "index": parts[0],
                "name": parts[1],
                "percent": _resource_percent(parts[2]),
                "vram_used_bytes": int(max(0.0, used_mb) * 1024 * 1024),
                "vram_total_bytes": int(max(0.0, total_mb) * 1024 * 1024),
                "temperature_c": _resource_percent(parts[5]),
            })
        vram_total = sum(int(gpu.get("vram_total_bytes") or 0) for gpu in gpus)
        vram_used = sum(int(gpu.get("vram_used_bytes") or 0) for gpu in gpus)
        gpu_values = [float(gpu["percent"]) for gpu in gpus if gpu.get("percent") is not None]
        temp_values = [float(gpu["temperature_c"]) for gpu in gpus if gpu.get("temperature_c") is not None]
        return {
            "available": bool(gpus),
            "percent": round(sum(gpu_values) / len(gpu_values), 1) if gpu_values else None,
            "temperature_c": round(max(temp_values), 1) if temp_values else None,
            "vram": {
                "available": bool(gpus),
                "used_bytes": vram_used,
                "total_bytes": vram_total,
                "percent": round((vram_used / vram_total) * 100, 1) if vram_total else None,
            },
            "gpus": gpus,
            "error": "" if gpus else "no gpu rows",
        }

    def _comfyui_resource_usage_snapshot():
        from datetime import datetime

        gpu = _nvidia_smi_resource_snapshot()
        return {
            "sampled_at": datetime.now().replace(microsecond=0).isoformat(),
            "ram": _proc_meminfo_snapshot(),
            "gpu": gpu,
            "vram": gpu.get("vram") if isinstance(gpu, dict) else {"percent": None},
        }

    @app.route("/api/comfyui/resources", methods=["GET"])
    @require_csrf_safe
    def comfyui_resource_dashboard():
        actor, err = _actor_or_401()
        if err:
            return err
        return json_resp({"ok": True, "resource_usage": _comfyui_resource_usage_snapshot()})

    @app.route("/api/comfyui/status", methods=["GET"])
    @require_csrf_safe
    def comfyui_status():
        actor, err = _actor_or_401()
        if err:
            return err
        binding = _comfyui_binding(actor, backend_url=request.args.get("backend_url"))
        active_client = _client_for_url(binding["url"])
        try:
            if hasattr(active_client, "health_check"):
                status = active_client.health_check(timeout=COMFYUI_STATUS_TIMEOUT_SECONDS)
            else:
                active_client.get_models()
                status = {"ok": True}
        except ComfyUIError as exc:
            runtime = _local_comfyui_runtime_status(_configured_comfyui_port()) if binding["connection_mode"] == "local" else None
            if runtime:
                return json_resp({
                    "ok": True,
                    "available": False,
                    "starting": True,
                    "msg": runtime["message"],
                    "startup_log_tail": runtime["startup_log_tail"],
                    "connection_mode": binding["connection_mode"],
                    "backend_scope": binding["backend_scope"],
                    "comfyui_url": getattr(active_client, "base_url", binding["url"]),
                    "max_batch_size": _configured_max_batch_size(),
                    "default_width": _configured_default_dimensions()["width"],
                    "default_height": _configured_default_dimensions()["height"],
                    "billing": None if not _comfyui_charge_required(actor) else (_comfyui_price_quote(1)[0] or {}),
                    "wallet": _comfyui_wallet_payload(actor),
                    "lora_extra_unit_price": COMFYUI_LORA_EXTRA_PRICE_POINTS,
                    "paid_api_nodes": _comfyui_paid_api_status_payload(),
                    "local_runtime": runtime,
                    "storage_warnings": _comfyui_storage_warnings(),
                })
            unavailable = _comfyui_unavailable_payload(exc, active_client)
            unavailable["storage_warnings"] = _comfyui_storage_warnings()
            return json_resp(unavailable)
        return json_resp({
            "ok": True,
            "available": True,
            "connection_mode": binding["connection_mode"],
            "backend_scope": binding["backend_scope"],
            "comfyui_url": getattr(active_client, "base_url", binding["url"]),
            "max_batch_size": _configured_max_batch_size(),
            "default_width": _configured_default_dimensions()["width"],
            "default_height": _configured_default_dimensions()["height"],
            "billing": None if not _comfyui_charge_required(actor) else (_comfyui_price_quote(1)[0] or {}),
            "wallet": _comfyui_wallet_payload(actor),
            "lora_extra_unit_price": COMFYUI_LORA_EXTRA_PRICE_POINTS,
            "paid_api_nodes": _comfyui_paid_api_status_payload(),
            "system": status.get("system") if isinstance(status, dict) else {},
            "storage_warnings": _comfyui_storage_warnings(),
        })

    @app.route("/api/comfyui/start", methods=["POST"])
    @require_csrf
    def comfyui_start_local():
        actor, err = _actor_or_401()
        if err:
            return err
        result, msg = _start_local_comfyui(actor, wait_seconds=2)
        if msg:
            return json_resp({"ok": False, "msg": msg, "connection_mode": _configured_connection_mode()}), 400
        return json_resp({
            "ok": True,
            "connection_mode": _configured_connection_mode(),
            "comfyui_url": _configured_comfyui_url(),
            "start": result,
            "msg": (result or {}).get("message") or ("ComfyUI 已在執行中" if (result or {}).get("already_running") else "已送出 ComfyUI 啟動請求"),
        })

    @app.route("/api/root/comfyui/stop", methods=["POST"])
    @require_csrf
    def root_comfyui_stop():
        actor, err = _root_or_403()
        if err:
            return err
        result, msg = _stop_local_comfyui(actor)
        if msg:
            return json_resp({"ok": False, "msg": msg, "connection_mode": _configured_connection_mode()}), 400
        return json_resp({
            "ok": True,
            "connection_mode": _configured_connection_mode(),
            "comfyui_url": _configured_comfyui_url(),
            "stop": result,
            "msg": "已停止本地 ComfyUI" if not (result or {}).get("already_stopped") else "ComfyUI 目前未在執行",
        })

    @app.route("/api/comfyui/models", methods=["GET"])
    @require_csrf_safe
    def comfyui_models():
        actor, err = _actor_or_401()
        if err:
            return err
        binding = _comfyui_binding(actor, backend_url=request.args.get("backend_url"))
        active_client = _client_for_url(binding["url"])
        try:
            capabilities = active_client.get_capabilities() if hasattr(active_client, "get_capabilities") else {}
            models = list((capabilities or {}).get("models") or [])
            loras = list((capabilities or {}).get("loras") or [])
            options = {
                "samplers": list((capabilities or {}).get("samplers") or []),
                "schedulers": list((capabilities or {}).get("schedulers") or []),
            }
            if not models and hasattr(active_client, "get_models"):
                models = active_client.get_models()
            if not options["samplers"] and hasattr(active_client, "get_sampler_options"):
                options = active_client.get_sampler_options()
            if not loras and hasattr(active_client, "get_loras"):
                loras = active_client.get_loras()
        except ComfyUIError as exc:
            unavailable = _comfyui_unavailable_payload(exc, active_client)
            return json_resp({
                **unavailable,
                "models": [],
                "loras": [],
                "lora_details": [],
                "vaes": [],
                "embeddings": [],
                "samplers": [SAFE_SAMPLER_FALLBACK],
                "schedulers": [SAFE_SCHEDULER_FALLBACK],
                "max_batch_size": _configured_max_batch_size(),
                "default_width": _configured_default_dimensions()["width"],
                "default_height": _configured_default_dimensions()["height"],
                "controlnet_models": [],
                "upscale_models": [],
                "latent_upscale_models": [],
                "clip_vision_models": [],
                "diffusion_models": [],
                "installed_gguf_models": [],
                "clip_models": [],
                "controlnet_types": {},
                "generation_modes": [],
                "model_families": [],
                "gguf_profiles": _official_gguf_profiles(),
                "billing": None if not _comfyui_charge_required(actor) else (_comfyui_price_quote(1)[0] or {}),
                "wallet": _comfyui_wallet_payload(actor),
                "lora_extra_unit_price": COMFYUI_LORA_EXTRA_PRICE_POINTS,
                "paid_api_nodes": _comfyui_paid_api_status_payload(),
                "storage_warnings": _comfyui_storage_warnings(),
            })
        try:
            vaes = list((capabilities or {}).get("vaes") or [])
            if not vaes and hasattr(active_client, "get_vaes"):
                vaes = active_client.get_vaes()
        except ComfyUIError:
            vaes = []
        try:
            embeddings = active_client.get_embeddings() if hasattr(active_client, "get_embeddings") else []
        except ComfyUIError:
            embeddings = []
        try:
            latent_upscale_models = list((capabilities or {}).get("latent_upscale_models") or [])
            if not latent_upscale_models and hasattr(active_client, "get_latent_upscale_models"):
                latent_upscale_models = active_client.get_latent_upscale_models()
        except ComfyUIError:
            latent_upscale_models = []
        try:
            clip_vision_models = list((capabilities or {}).get("clip_vision_models") or [])
            if not clip_vision_models and hasattr(active_client, "get_clip_vision_models"):
                clip_vision_models = active_client.get_clip_vision_models()
        except ComfyUIError:
            clip_vision_models = []
        lora_details = _build_lora_details(loras)
        model_families = (capabilities or {}).get("model_families") or []
        diffusion_models = list((capabilities or {}).get("diffusion_models") or [])
        return json_resp({
            "ok": True,
            "models": models,
            "loras": loras,
            "lora_details": lora_details,
            "vaes": vaes,
            "embeddings": embeddings,
            "connection_mode": binding["connection_mode"],
            "backend_scope": binding["backend_scope"],
            "samplers": options.get("samplers") or [SAFE_SAMPLER_FALLBACK],
            "schedulers": options.get("schedulers") or [SAFE_SCHEDULER_FALLBACK],
            "comfyui_url": getattr(active_client, "base_url", binding["url"]),
            "max_batch_size": _configured_max_batch_size(),
            "default_width": _configured_default_dimensions()["width"],
            "default_height": _configured_default_dimensions()["height"],
            "controlnet_models": (capabilities or {}).get("controlnet_models") or [],
            "upscale_models": (capabilities or {}).get("upscale_models") or [],
            "latent_upscale_models": latent_upscale_models,
            "clip_vision_models": clip_vision_models,
            "diffusion_models": diffusion_models,
            "installed_gguf_models": _installed_gguf_inventory(diffusion_models),
            "clip_models": (capabilities or {}).get("clip_models") or [],
            "controlnet_types": (capabilities or {}).get("controlnet_types") or {},
            "generation_modes": (capabilities or {}).get("generation_modes") or [],
            "model_families": model_families,
            "gguf_profiles": _official_gguf_profiles(),
            "billing": None if not _comfyui_charge_required(actor) else (_comfyui_price_quote(1)[0] or {}),
            "wallet": _comfyui_wallet_payload(actor),
            "lora_extra_unit_price": COMFYUI_LORA_EXTRA_PRICE_POINTS,
            "paid_api_nodes": _comfyui_paid_api_status_payload(),
            "storage_warnings": _comfyui_storage_warnings(),
        })

    @app.route("/api/comfyui/installed-gguf", methods=["GET"])
    @require_csrf_safe
    def comfyui_installed_gguf():
        actor, err = _actor_or_401()
        if err:
            return err
        binding = _comfyui_binding(actor)
        active_client = _client_for_url(binding["url"])
        try:
            capabilities = active_client.get_capabilities() if hasattr(active_client, "get_capabilities") else {}
        except ComfyUIError as exc:
            unavailable = _comfyui_unavailable_payload(exc, active_client)
            return json_resp({
                **unavailable,
                "installed_gguf_models": [],
                "gguf_profiles": _official_gguf_profiles(),
                "connection_mode": binding["connection_mode"],
                "backend_scope": binding["backend_scope"],
                "comfyui_url": getattr(active_client, "base_url", binding["url"]),
            }, 503)
        diffusion_models = list((capabilities or {}).get("diffusion_models") or [])
        return json_resp({
            "ok": True,
            "installed_gguf_models": _installed_gguf_inventory(diffusion_models),
            "diffusion_models": diffusion_models,
            "gguf_profiles": _official_gguf_profiles(),
            "connection_mode": binding["connection_mode"],
            "backend_scope": binding["backend_scope"],
            "comfyui_url": getattr(active_client, "base_url", binding["url"]),
        })

    @app.route("/api/comfyui/diffusers/inspect", methods=["GET", "POST"])
    @require_csrf_safe
    def comfyui_diffusers_inspect():
        actor, err = _actor_or_401()
        if err:
            return err
        if request.method == "GET":
            data = {
                "diffusers_model_repo": request.args.get("diffusers_model_repo") or request.args.get("model") or request.args.get("repo"),
                "generation_mode": request.args.get("generation_mode") or "txt2img",
                "backend_url": request.args.get("backend_url") or request.args.get("comfyui_backend_url"),
            }
        else:
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            data = data if isinstance(data, dict) else {}
        binding = _comfyui_binding(actor, backend_url=data.get("backend_url") or data.get("comfyui_backend_url"))
        active_client = _client_for_url(binding["url"])
        if not hasattr(active_client, "inspect_model_repo"):
            return json_resp({
                "ok": False,
                "msg": "目前後端不是 Hugging Face Diffusers 模式，無法檢查 repo。",
                "connection_mode": binding["connection_mode"],
            }), 400
        inspection = active_client.inspect_model_repo(
            data.get("diffusers_model_repo") or data.get("model") or data.get("repo"),
            mode=data.get("generation_mode") or "txt2img",
        )
        status = 200 if inspection.get("ok") else 400
        return json_resp({
            **inspection,
            "connection_mode": binding["connection_mode"],
            "backend_scope": binding["backend_scope"],
            "gguf_profiles": _official_gguf_profiles(),
        }, status)

    @app.route("/api/comfyui/node-catalog", methods=["GET"])
    @require_csrf_safe
    def comfyui_node_catalog():
        actor, err = _actor_or_401()
        if err:
            return err
        if not _build_node_catalog:
            return json_resp({"ok": False, "msg": "ComfyUI 節點目錄摘要器未載入", "stage": "node_catalog_unavailable"}), 503
        binding = _comfyui_binding(actor)
        active_client = _client_for_url(binding["url"])
        try:
            object_info = active_client.get_object_info()
            catalog = _build_node_catalog(object_info)
        except ComfyUIError as exc:
            return _json_error_from_comfy(exc, active_client)
        return json_resp({
            "ok": True,
            "nodes": catalog["nodes"],
            "count": catalog["count"],
            "truncated": catalog["truncated"],
            "connection_mode": binding["connection_mode"],
            "backend_scope": binding["backend_scope"],
            "comfyui_url": getattr(active_client, "base_url", binding["url"]),
            "paid_api_nodes": _comfyui_paid_api_status_payload(),
        })

    @app.route("/api/comfyui/billing-quote", methods=["POST"])
    @require_csrf
    def comfyui_billing_quote():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        data = {**data, "skip_asset_validation": True}
        params, msg = _normalize_generation_payload(data)
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        if not _comfyui_charge_required(actor):
            return json_resp({"ok": True, "billing": {"charged": False, "exempt": "root"}, "wallet": _comfyui_wallet_payload(actor)})
        total_quantity, run_count = _comfyui_total_quantity(data, params)
        quote, msg = _comfyui_price_quote(total_quantity, lora_count=_comfyui_lora_count(params))
        if msg:
            return json_resp({"ok": False, "msg": msg}), 503
        quote = {**quote, "batch_size": params["batch_size"], "run_count": run_count}
        msg = _ensure_comfyui_balance(actor, quote)
        if msg:
            return json_resp({"ok": False, "msg": msg, "billing": quote, "wallet": _comfyui_wallet_payload(actor)}), 409
        return json_resp({"ok": True, "billing": quote, "wallet": _comfyui_wallet_payload(actor)})

    @app.route("/api/comfyui/generate", methods=["POST"])
    @require_csrf
    def comfyui_generate():
        actor, err = _actor_or_401()
        if err:
            return err
        data, uploaded_assets, request_msg = _parse_generation_request()
        if request_msg:
            return json_resp({"ok": False, "msg": request_msg}), 400
        request_data = data if isinstance(data, dict) else {}
        if uploaded_assets:
            request_data = {**request_data, "skip_asset_validation": True}
        params, msg = _normalize_generation_payload(request_data)
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        backend_binding = _comfyui_binding(actor, backend_url=request_data.get("backend_url") or request_data.get("comfyui_backend_url"))
        active_client = _client_for_url(backend_binding["url"])
        try:
            params = _hydrate_generation_assets(actor, active_client, params, uploaded_assets)
            capabilities, capability_msg = _validate_generation_capabilities(active_client, params)
            if capability_msg:
                return json_resp({"ok": False, "msg": capability_msg, "capabilities": capabilities or {}}), 409
        except ComfyUIError as exc:
            return _json_error_from_comfy(exc, active_client)
        quote = None
        timeout_seconds = _normalize_generation_timeout(data.get("timeout_seconds"))
        if _comfyui_charge_required(actor):
            quote, msg = _comfyui_price_quote(params["batch_size"], lora_count=_comfyui_lora_count(params))
            if msg:
                return json_resp({"ok": False, "msg": msg}), 503
            msg = _ensure_comfyui_balance(actor, quote)
            if msg:
                return json_resp({"ok": False, "msg": msg}), 409
            if not _coerce_bool(data.get("confirm_billing")):
                return json_resp({
                    "ok": False,
                    "msg": (
                        f"請先確認扣點：本次成功產圖將扣 {quote['total_price']} 點；"
                        "產圖失敗不扣點，丟棄預覽不退款。"
                    ),
                    "billing": {**quote, "confirmation_required": True},
                }), 409
        job_id = _create_generation_job(actor)
        initial_progress = (
            _initial_generation_progress(active_client, params, timeout_seconds)
            if callable(_initial_generation_progress)
            else {
                "phase": "queued",
                "percent": 0,
                "detail": "已建立產圖工作",
                "timeout_seconds": timeout_seconds,
                "timeout_unlimited": int(timeout_seconds or 0) <= 0,
            }
        )
        if callable(_update_generation_job_progress) and str(initial_progress.get("phase") or "") != "queued":
            _update_generation_job_progress(job_id, initial_progress)
        initial_status = "running" if str(initial_progress.get("phase") or "") != "queued" else "queued"
        request_meta = _capture_request_audit_meta()
        worker = threading.Thread(
            target=_run_comfyui_generation_job,
            args=(job_id, dict(actor), params, quote, timeout_seconds, request_meta, backend_binding),
            daemon=True,
        )
        worker.start()
        return json_resp({
            "ok": True,
            "async": True,
            "job": {
                "job_id": job_id,
                "status": initial_status,
                "progress": initial_progress,
            },
        })

    @app.route("/api/comfyui/jobs/<job_id>", methods=["GET"])
    @require_csrf_safe
    def comfyui_generation_job_status(job_id):
        actor, err = _actor_or_401()
        if err:
            return err
        job, err = _assert_generation_job_owner(job_id, actor)
        if err:
            return err
        public_job = _generation_job_payload(job, actor=actor) if _generation_job_payload else {
            "job_id": job["job_id"],
            "status": job["status"],
            "progress": job.get("progress") or {},
            "error": job.get("error") or "",
            "result": job.get("result"),
        }
        return json_resp({"ok": True, "job": public_job})

    @app.route("/api/comfyui/jobs/<job_id>/review", methods=["POST"])
    @require_csrf
    def comfyui_generation_job_review(job_id):
        actor, err = _actor_or_401()
        if err:
            return err
        job, err = _assert_generation_job_owner(job_id, actor)
        if err:
            return err
        if str(job.get("status") or "") != "completed" or not isinstance(job.get("result"), dict):
            return json_resp({"ok": False, "msg": "產圖結果完成後才能記錄視覺驗收"}), 409
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict) or "pass" not in data:
            return json_resp({"ok": False, "msg": "視覺驗收必須提供 pass"}), 400
        try:
            score = max(0.0, min(1.0, float(data.get("score") or 0)))
        except Exception:
            return json_resp({"ok": False, "msg": "視覺驗收 score 格式錯誤"}), 400
        hard_fail = _coerce_bool(data.get("hard_fail"))
        result = dict(job["result"])
        contract = result.get("review_contract") if isinstance(result.get("review_contract"), dict) else {}
        try:
            threshold = max(0.5, min(1.0, float(contract.get("agent_review_pass_threshold") or 0.8)))
        except Exception:
            threshold = 0.8

        def _text_list(name):
            values = data.get(name) if isinstance(data.get(name), list) else []
            return [str(item or "").strip()[:500] for item in values[:20] if str(item or "").strip()]

        requested_pass = data.get("pass") is True
        passed_gates = _text_list("passed_gates")
        failed_gates = _text_list("failed_gates")
        required_preservation = [
            str(item or "").strip()
            for item in (contract.get("preservation_checks") or [])
            if str(item or "").strip()
        ]
        missing_preservation = [item for item in required_preservation if item not in passed_gates]
        if missing_preservation:
            failed_gates.extend(f"preservation:{item}" for item in missing_preservation)
            hard_fail = True
        passed = bool(requested_pass and not hard_fail and score >= threshold)

        review = {
            "pass": passed,
            "requested_pass": requested_pass,
            "score": score,
            "threshold": threshold,
            "hard_fail": hard_fail,
            "issues": _text_list("issues"),
            "passed_gates": passed_gates,
            "failed_gates": failed_gates,
            "source": str(data.get("source") or "ai_agent_vision_client").strip()[:80],
        }
        result.update({
            "delivery_status": "approved" if passed else "review_failed",
            "review_required": not passed,
            "delivery_pass": passed,
            "semantic_quality_pass": passed,
            "review_result": review,
        })
        if not callable(_update_generation_job):
            return json_resp({"ok": False, "msg": "視覺驗收持久化服務未載入"}), 503
        updated = _update_generation_job(job_id, result=result)
        if not updated or updated.get("persistence_error"):
            return json_resp({"ok": False, "msg": "視覺驗收無法持久化，結果仍視為未通過"}), 503
        audit(
            "COMFYUI_GENERATION_REVIEW",
            get_client_ip(),
            user=_actor_value(actor, "username", "-"),
            success=passed,
            ua=get_ua(),
            detail=f"job_id={job_id},pass={passed},score={score:.3f},threshold={threshold:.3f},hard_fail={hard_fail}",
        )
        return json_resp({"ok": True, "job_id": job_id, "delivery": result, "review": review})

    @app.route("/api/comfyui/media-preview", methods=["POST"])
    @require_csrf
    def comfyui_media_preview():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            return json_resp({"ok": False, "msg": "缺少 ComfyUI 工作編號"}), 400
        if not callable(_media_ref_payload):
            return json_resp({"ok": False, "msg": "ComfyUI 媒體預覽功能未載入"}), 503
        try:
            file_ref = _media_ref_payload(data.get("file_ref"))
        except Exception:
            return json_resp({"ok": False, "msg": "媒體檔案引用不合法"}), 400
        job, err = _assert_generation_job_owner(job_id, actor)
        if err:
            return err
        media_item = _job_media_output_item(job.get("result"), file_ref)
        if not media_item:
            audit(
                "COMFYUI_MEDIA_REF_DENIED",
                get_client_ip(),
                user=actor["username"],
                success=False,
                ua=get_ua(),
                detail=f"job_id={job_id},file={file_ref.get('filename', '-')}",
            )
            return json_resp({"ok": False, "msg": "無權讀取這個 ComfyUI 媒體輸出"}), 403
        job_result = job.get("result") if isinstance(job.get("result"), dict) else {}
        media_backend_url = (
            media_item.get("backend_url")
            or job_result.get("backend_url")
            or job_result.get("comfyui_url")
            or ""
        )
        active_client = _client_for_url(_comfyui_binding(actor, backend_url=media_backend_url).get("url"))
        fetch_file = getattr(active_client, "fetch_file", None)
        if not callable(fetch_file):
            return json_resp({"ok": False, "msg": "目前 ComfyUI 後端不支援媒體檔預覽"}), 503
        try:
            media_file = fetch_file(file_ref)
        except ComfyUIError as exc:
            return _json_error_from_comfy(exc, active_client)
        import base64

        mime_type = _media_mime_type(
            getattr(media_file, "mime_type", "") or media_item.get("mime_type"),
            getattr(media_file, "filename", "") or file_ref.get("filename"),
        )
        media_ref = {
            "filename": getattr(media_file, "filename", "") or file_ref.get("filename"),
            "subfolder": getattr(media_file, "subfolder", "") or file_ref.get("subfolder") or "",
            "type": getattr(media_file, "type", "") or file_ref.get("type") or "output",
        }
        data_bytes = getattr(media_file, "data", b"") or b""
        return json_resp({
            "ok": True,
            "media": {
                "file_ref": media_ref,
                "mime_type": mime_type,
                "size_bytes": len(data_bytes),
                "data_url": f"data:{mime_type};base64,{base64.b64encode(data_bytes).decode('ascii')}",
            },
        })

    @app.route("/api/comfyui/background-composite", methods=["POST"])
    @require_csrf
    def comfyui_background_composite():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        source_ref = data.get("source_image_ref") or data.get("source_image_ref_json")
        background_ref = (
            data.get("background_image_ref")
            or data.get("background_image_ref_json")
            or data.get("reference_image_ref")
            or data.get("reference_image_ref_json")
        )
        if not isinstance(source_ref, dict):
            return json_resp({"ok": False, "msg": "缺少來源圖片引用 source_image_ref"}), 400
        if not isinstance(background_ref, dict):
            return json_resp({"ok": False, "msg": "缺少背景參考圖片引用 background_image_ref"}), 400
        try:
            width = int(float(data.get("width") or 0))
            height = int(float(data.get("height") or 0))
        except Exception:
            width = 0
            height = 0
        if width and (width < 64 or width > 4096):
            return json_resp({"ok": False, "msg": "width 超出允許範圍 64-4096"}), 400
        if height and (height < 64 or height > 4096):
            return json_resp({"ok": False, "msg": "height 超出允許範圍 64-4096"}), 400

        active_client = _client_for_url(_comfyui_binding(actor).get("url"))
        params = {
            "source_image_ref": source_ref,
            "reference_image_ref": background_ref,
            "seed": 0,
            "model": "exact_background_plate_composite",
            "batch_size": 1,
        }
        mask_ref = data.get("mask_image_ref") or data.get("mask_image_ref_json")
        if isinstance(mask_ref, dict):
            params["mask_image_ref"] = mask_ref
        try:
            params = _hydrate_generation_assets(actor, active_client, params, {})
            source_file = active_client.fetch_image(params["source_image_ref"])
            background_file = active_client.fetch_image(params["reference_image_ref"])
            mask_file = active_client.fetch_image(params["mask_image_ref"]) if isinstance(params.get("mask_image_ref"), dict) else None
        except ComfyUIError as exc:
            return _json_error_from_comfy(exc, active_client)
        except Exception as exc:
            return json_resp({"ok": False, "msg": f"背景合成圖片讀取失敗：{exc}"}), 503

        try:
            import hashlib
            from io import BytesIO
            from PIL import Image, ImageDraw, ImageFilter, ImageOps

            source_image = Image.open(BytesIO(getattr(source_file, "data", b"") or b"")).convert("RGBA")
            background_image = Image.open(BytesIO(getattr(background_file, "data", b"") or b"")).convert("RGB")
            target_width = width or source_image.width
            target_height = height or source_image.height

            def _cover_resize(image, size):
                tw, th = size
                scale = max(tw / max(1, image.width), th / max(1, image.height))
                resized = image.resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                left = max(0, (resized.width - tw) // 2)
                top = max(0, (resized.height - th) // 2)
                return resized.crop((left, top, left + tw, top + th))

            background_plate = _cover_resize(background_image, (target_width, target_height)).convert("RGBA")
            source_layer = _cover_resize(source_image, (target_width, target_height)).convert("RGBA")
            segmentation_method = "heuristic_center_subject_mask"
            if mask_file is not None:
                mask_image = Image.open(BytesIO(getattr(mask_file, "data", b"") or b"")).convert("L")
                subject_mask = _cover_resize(mask_image, (target_width, target_height)).convert("L")
                segmentation_method = "provided_mask_image_ref"
            elif source_layer.getextrema()[3][0] < 255:
                subject_mask = source_layer.getchannel("A")
                segmentation_method = "source_alpha_channel"
            else:
                subject_mask = Image.new("L", (target_width, target_height), 0)
                draw = ImageDraw.Draw(subject_mask)
                w, h = target_width, target_height
                draw.ellipse((int(w * 0.18), int(h * 0.02), int(w * 0.82), int(h * 0.50)), fill=255)
                draw.rounded_rectangle(
                    (int(w * 0.23), int(h * 0.26), int(w * 0.77), int(h * 0.98)),
                    radius=max(8, int(w * 0.08)),
                    fill=255,
                )
                draw.polygon([
                    (int(w * 0.28), int(h * 0.34)),
                    (int(w * 0.05), int(h * 0.88)),
                    (int(w * 0.25), int(h * 0.96)),
                    (int(w * 0.44), int(h * 0.46)),
                ], fill=255)
                draw.polygon([
                    (int(w * 0.72), int(h * 0.34)),
                    (int(w * 0.95), int(h * 0.88)),
                    (int(w * 0.75), int(h * 0.96)),
                    (int(w * 0.56), int(h * 0.46)),
                ], fill=255)
                subject_mask = subject_mask.filter(ImageFilter.GaussianBlur(max(6, int(min(w, h) * 0.018))))
                subject_mask = ImageOps.autocontrast(subject_mask)

            output_image = background_plate.copy()
            output_image.paste(source_layer, (0, 0), subject_mask)
            output_rgb = output_image.convert("RGB")
            out = BytesIO()
            output_rgb.save(out, format="PNG", optimize=True)
            output_bytes = out.getvalue()
            digest = hashlib.sha1(output_bytes).hexdigest()[:12]
            filename = f"exact_background_composite_{target_width}x{target_height}_{digest}.png"
            uploaded_ref = active_client.upload_image_bytes(
                output_bytes,
                filename,
                image_type="output",
                overwrite=False,
                subfolder=f"hackme/{_actor_value(actor, 'id', '')}".strip("/"),
            )
        except Exception as exc:
            return json_resp({"ok": False, "msg": f"背景合成失敗：{exc}"}), 500

        result = {
            "prompt_id": f"background-composite-{digest}",
            "image_ref": uploaded_ref,
            "mime_type": "image/png",
            "data": output_bytes,
            "images": [{
                "prompt_id": f"background-composite-{digest}",
                "image_ref": uploaded_ref,
                "mime_type": "image/png",
                "size_bytes": len(output_bytes),
                "seed": 0,
                "model": "exact_background_plate_composite",
                "batch_size": 1,
                "batch_index": 0,
                "output_label": "Exact background plate composite",
            }],
        }
        images = _finalize_generation_records(
            actor,
            params,
            result,
            backend_url=getattr(active_client, "base_url", ""),
            notify=False,
        )
        payload = {
            "ok": True,
            "method": "exact_background_plate_composite",
            "segmentation_method": segmentation_method,
            "delivery_pass": False,
            "review_required": True,
            "quality_gate": {
                "status": "candidate_requires_visual_review",
                "reason": (
                    "Exact background plate compositing can preserve pixels from the reference, "
                    "but it cannot prove that the reference background is clean or that subject masking is acceptable. "
                    "A human or vision gate must review foreground residue, old-background halos, and reference-subject leakage."
                ),
            },
            "image": images[0] if images else result["images"][0],
            "images": images or result["images"],
            "source_image_ref": params.get("source_image_ref"),
            "background_image_ref": params.get("reference_image_ref"),
            "width": target_width,
            "height": target_height,
            "limitations": [
                "background plate is copied from the reference crop instead of model-redrawn",
                "without a supplied mask, foreground segmentation uses a conservative center-subject heuristic",
                "reference foreground objects outside the source subject mask may remain because exact copy and object removal are conflicting goals",
            ],
            "msg": "已產生 exact background plate composite 候選圖；此結果需目視 review，不能直接視為通過。若需要去除參考圖中的人物/物件，請提供乾淨背景板、背景 mask，或改走下一輪 inpaint。",
        }
        audit(
            "COMFYUI_BACKGROUND_COMPOSITE",
            get_client_ip(),
            user=_actor_value(actor, "username", "-"),
            success=True,
            ua=get_ua(),
            detail=f"file={uploaded_ref.get('filename')},size={target_width}x{target_height},segmentation={segmentation_method}",
        )
        return json_resp(payload)

    @app.route("/api/comfyui/history", methods=["GET"])
    @require_csrf_safe
    def comfyui_generation_history():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            items = _list_generation_history(conn, actor=actor, limit=COMFYUI_HISTORY_LIMIT)
            if callable(_ensure_comfyui_workflow_schema):
                _ensure_comfyui_workflow_schema(conn)
            has_workflow_tables = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comfyui_workflow_runs'"
            ).fetchone() and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comfyui_workflow_presets'"
            ).fetchone()
            workflow_rows = []
            if has_workflow_tables:
                workflow_rows = conn.execute(
                    """
                    SELECT r.id, r.preset_id, r.prompt, r.negative_prompt, r.params_json,
                           r.workflow_json, r.output_refs_json, r.status, r.error, r.created_at, r.updated_at,
                           p.title AS preset_title, p.purpose AS preset_purpose, p.visibility,
                           p.owner_user_id AS preset_owner_user_id, p.is_official
                    FROM comfyui_workflow_runs r
                    JOIN comfyui_workflow_presets p ON p.id = r.preset_id
                    WHERE r.actor_user_id=?
                      AND (p.owner_user_id=? OR p.is_official=1 OR p.visibility='public')
                    ORDER BY r.created_at DESC, r.id DESC
                    LIMIT ?
                    """,
                    (int(actor.get("id") or 0), int(actor.get("id") or 0), int(COMFYUI_HISTORY_LIMIT)),
                ).fetchall()
            for row in workflow_rows:
                workflow_json = _runtime_parse_json_field(row["workflow_json"], {})
                params = _runtime_workflow_snapshot_params(
                    _runtime_parse_json_field(row["params_json"], {}),
                    workflow_json,
                )
                output_refs = _runtime_parse_json_field(row["output_refs_json"], {})
                prompt = params.get("prompt") or row["prompt"] or row["preset_title"] or ""
                negative_prompt = params.get("negative_prompt") or row["negative_prompt"] or ""
                items.append({
                    "id": f"workflow-{int(row['id'])}",
                    "history_source": "workflow",
                    "workflow_run_id": int(row["id"]),
                    "preset_id": int(row["preset_id"]),
                    "preset_title": row["preset_title"] or "Workflow",
                    "generation_mode": params.get("generation_mode") or row["preset_purpose"] or "workflow",
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "payload": {
                        **params,
                        "workflow_preset_id": int(row["preset_id"]),
                        "workflow_preset_title": row["preset_title"] or "Workflow",
                        "prompt": prompt,
                        "negative_prompt": negative_prompt,
                        "model": params.get("model") or row["preset_title"] or "Workflow",
                    },
                    "params": params,
                    "input_assets": {},
                    "controlnet": {},
                    "workflow_json": workflow_json,
                    "result": output_refs,
                    "status": row["status"] or "queued",
                    "error": row["error"] or "",
                })
            items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            items = items[:COMFYUI_HISTORY_LIMIT]
        finally:
            conn.close()
        return json_resp({"ok": True, "history": items})

    @app.route("/api/comfyui/history/<int:history_id>", methods=["DELETE"])
    @require_csrf
    def comfyui_generation_history_delete(history_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            item = _load_generation_history(conn, actor=actor, history_id=history_id)
            if not item:
                return json_resp({"ok": False, "msg": "找不到這筆 ComfyUI 歷史紀錄"}), 404
            conn.execute(
                "DELETE FROM comfyui_generation_history WHERE id=? AND owner_user_id=?",
                (int(history_id), int(actor.get("id") or 0)),
            )
            conn.commit()
        finally:
            conn.close()
        audit(
            "COMFYUI_HISTORY_DELETE",
            get_client_ip(),
            user=_actor_value(actor, "username"),
            success=True,
            ua=get_ua(),
            detail=f"history_id={int(history_id)}",
        )
        return json_resp({"ok": True, "deleted_id": int(history_id), "msg": "已刪除 ComfyUI 歷史紀錄"})

    @app.route("/api/comfyui/history/<int:history_id>/rerun", methods=["POST"])
    @require_csrf
    def comfyui_generation_history_rerun(history_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            item = _load_generation_history(conn, actor=actor, history_id=history_id)
        finally:
            conn.close()
        if not item:
            return json_resp({"ok": False, "msg": "找不到這筆 ComfyUI 歷史紀錄"}), 404
        payload = dict(item.get("payload") or {})
        input_assets = dict(item.get("input_assets") or {})
        controlnet = dict(item.get("controlnet") or {})
        if controlnet:
            if input_assets.get("control_image_ref") and not controlnet.get("image_ref"):
                controlnet["image_ref"] = input_assets.get("control_image_ref")
            payload["controlnet"] = controlnet
        if input_assets.get("source_image_ref") and not payload.get("source_image_ref"):
            payload["source_image_ref"] = input_assets.get("source_image_ref")
        if input_assets.get("mask_image_ref") and not payload.get("mask_image_ref"):
            payload["mask_image_ref"] = input_assets.get("mask_image_ref")
        payload["async_progress"] = True
        payload["confirm_billing"] = True
        payload["timeout_seconds"] = DEFAULT_GENERATION_TIMEOUT_SECONDS
        active_client = _client_for_url(_comfyui_binding(actor, backend_url=item.get("backend_url")).get("url"))
        try:
            capabilities, capability_msg = _validate_generation_capabilities(active_client, payload)
            if capability_msg:
                return json_resp({"ok": False, "msg": capability_msg, "capabilities": capabilities or {}}), 409
        except ComfyUIError as exc:
            return _json_error_from_comfy(exc, active_client)
        quote = None
        if _comfyui_charge_required(actor):
            quote, msg = _comfyui_price_quote(payload.get("batch_size") or 1, lora_count=_comfyui_lora_count(payload))
            if msg:
                return json_resp({"ok": False, "msg": msg}), 503
            msg = _ensure_comfyui_balance(actor, quote)
            if msg:
                return json_resp({"ok": False, "msg": msg}), 409
        job_id = _create_generation_job(actor)
        request_meta = _capture_request_audit_meta()
        worker = threading.Thread(
            target=_run_comfyui_generation_job,
            args=(job_id, dict(actor), payload, quote, DEFAULT_GENERATION_TIMEOUT_SECONDS, request_meta, _comfyui_binding(actor, backend_url=item.get("backend_url"))),
            daemon=True,
        )
        worker.start()
        return json_resp({
            "ok": True,
            "async": True,
            "job": {
                "job_id": job_id,
                "status": "queued",
                "progress": {"phase": "queued", "percent": 0, "detail": "已建立重跑工作"},
            },
        })

    @app.route("/api/comfyui/workflow-runs/<int:run_id>", methods=["DELETE"])
    @require_csrf
    def comfyui_workflow_run_history_delete(run_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            if callable(_ensure_comfyui_workflow_schema):
                _ensure_comfyui_workflow_schema(conn)
            has_tables = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comfyui_workflow_runs'"
            ).fetchone()
            if not has_tables:
                return json_resp({"ok": False, "msg": "找不到 workflow run 歷史資料表"}), 404
            row = conn.execute(
                "SELECT id, actor_user_id FROM comfyui_workflow_runs WHERE id=?",
                (int(run_id),),
            ).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到這筆 workflow run 歷史紀錄"}), 404
            actor_id = int(actor.get("id") or 0)
            if int(row["actor_user_id"] or 0) != actor_id:
                return json_resp({"ok": False, "msg": "你沒有權限刪除這筆 workflow run"}), 403
            conn.execute(
                "DELETE FROM comfyui_workflow_runs WHERE id=? AND actor_user_id=?",
                (int(run_id), actor_id),
            )
            conn.commit()
        finally:
            conn.close()
        audit(
            "COMFYUI_WORKFLOW_RUN_DELETE",
            get_client_ip(),
            user=_actor_value(actor, "username"),
            success=True,
            ua=get_ua(),
            detail=f"run_id={int(run_id)}",
        )
        return json_resp({"ok": True, "deleted_id": int(run_id), "msg": "已刪除 workflow run 歷史紀錄"})

    @app.route("/api/comfyui/workflow-runs/<int:run_id>/rerun", methods=["POST"])
    @require_csrf
    def comfyui_workflow_run_history_rerun(run_id):
        actor, err = _actor_or_401()
        if err:
            return err
        if not callable(_create_workflow_run) or not callable(_run_comfyui_workflow_preset_job):
            return json_resp({"ok": False, "msg": "workflow 重跑服務尚未初始化"}), 503
        conn = get_db()
        try:
            if callable(_ensure_comfyui_workflow_schema):
                _ensure_comfyui_workflow_schema(conn)
            has_tables = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comfyui_workflow_runs'"
            ).fetchone() and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comfyui_workflow_presets'"
            ).fetchone()
            if not has_tables:
                return json_resp({"ok": False, "msg": "找不到 workflow run 歷史資料表"}), 404
            row = conn.execute(
                """
                SELECT r.id, r.preset_id, r.actor_user_id, r.prompt, r.negative_prompt,
                       r.params_json, r.workflow_json,
                       p.workflow_json AS preset_workflow_json, p.system_bundle_id,
                       p.title AS preset_title, p.visibility, p.owner_user_id, p.is_official
                FROM comfyui_workflow_runs r
                JOIN comfyui_workflow_presets p ON p.id = r.preset_id
                WHERE r.id=?
                """,
                (int(run_id),),
            ).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到這筆 workflow run 歷史紀錄"}), 404
            actor_id = int(actor.get("id") or 0)
            can_rerun = int(row["actor_user_id"] or 0) == actor_id
            if not can_rerun:
                return json_resp({"ok": False, "msg": "你沒有權限重跑這筆 workflow run"}), 403
            params = _runtime_parse_json_field(row["params_json"], {}) or {}
            workflow_json = _runtime_parse_json_field(row["workflow_json"], {}) or {}
            params = _runtime_workflow_snapshot_params(params, workflow_json)
            try:
                from services.comfyui.template.gguf_workflow import (
                    apply_gguf_workflow_profile,
                    infer_gguf_workflow_spec_from_snapshot,
                    is_gguf_workflow_id,
                    needs_gguf_workflow_snapshot_repair,
                )
                if is_gguf_workflow_id(row["system_bundle_id"]) and needs_gguf_workflow_snapshot_repair(workflow_json):
                    preset_workflow = _parse_json_field(row["preset_workflow_json"], {}) or {}
                    spec = infer_gguf_workflow_spec_from_snapshot(workflow_json, params)
                    selection = apply_gguf_workflow_profile(preset_workflow or workflow_json, {}, spec)
                    repaired = selection.workflow
                    for node_id, node in (workflow_json or {}).items():
                        if not isinstance(node, dict):
                            continue
                        target = repaired.get(str(node_id))
                        if not isinstance(target, dict):
                            continue
                        src_inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
                        dst_inputs = target.get("inputs") if isinstance(target.get("inputs"), dict) else {}
                        for input_name, value in src_inputs.items():
                            if isinstance(value, list) or input_name not in dst_inputs:
                                continue
                            if str(input_name) in {"ckpt_name", "unet_name", "clip_name1", "clip_name2", "clip_name3", "vae_name"}:
                                continue
                            dst_inputs[str(input_name)] = value
                    workflow_json = repaired
                    params = dict(params)
                    if selection.profile and selection.variant:
                        params["gguf_profile"] = selection.profile.get("id") or params.get("gguf_profile") or ""
                        params["gguf_variant"] = selection.variant.get("id") or params.get("gguf_variant") or ""
                        params["model"] = selection.variant.get("gguf_file") or params.get("model") or ""
                        params["diffusion_model"] = params["model"]
            except Exception:
                pass
            request_body = request.get_json(force=True, silent=True) or {}
            params = dict(params)
            params["seed_after_generate"] = _runtime_workflow_rerun_seed_mode(params, request_body)
            workflow_json, params = _runtime_randomize_workflow_seed_inputs(workflow_json, params)
            new_run_id = _create_workflow_run(
                conn,
                preset_id=int(row["preset_id"]),
                actor=actor,
                prompt=params.get("prompt") or row["prompt"] or row["preset_title"] or "",
                negative_prompt=params.get("negative_prompt") or row["negative_prompt"] or "",
                params_json=params,
                workflow_json=workflow_json,
            )
            conn.commit()
        finally:
            conn.close()
        import json as _json
        job_id = _create_generation_job(actor)
        request_meta = _capture_request_audit_meta()
        worker_row = {
            "id": int(row["preset_id"]),
            "default_params_json": _json.dumps(params, ensure_ascii=False, sort_keys=True),
            "workflow_json": _json.dumps(workflow_json, ensure_ascii=False, sort_keys=True),
        }
        worker = threading.Thread(
            target=_run_comfyui_workflow_preset_job,
            args=(job_id, dict(actor), worker_row, new_run_id, DEFAULT_GENERATION_TIMEOUT_SECONDS, request_meta, None, workflow_json),
            daemon=True,
        )
        worker.start()
        return json_resp({
            "ok": True,
            "async": True,
            "workflow_run_id": new_run_id,
            "job": {
                "job_id": job_id,
                "status": "queued",
                "progress": {"phase": "queued", "percent": 0, "detail": "已用歷史 workflow 快照建立重跑工作"},
            },
        })
